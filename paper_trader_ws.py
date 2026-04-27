"""
Paper Trading Bot v2 — WebSocket (real-time orderbook).

Używa Gamma API do discovery rynku + WebSocket do real-time cen.
Monitoruje mid_price z orderbooka, nie snapshoty.

Użycie:
    source .venv/bin/activate
    python paper_trader_ws.py --coin ETH --size 10

    --coin: BTC, ETH, SOL, XRP (default: ETH)
    --size: wielkość pozycji w USDC (default: 10)
    --drop: próg flash crash (default: 0.15)
    --tp: take profit (default: 0.10)
    --sl: stop loss (default: 0.05)
"""

import argparse
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict
from collections import deque

import logging

from src.gamma_client import GammaClient
from src.websocket_client import MarketWebSocket, OrderbookSnapshot

# Wycisz logi websocket (za głośne)
logging.getLogger("src.websocket_client").setLevel(logging.WARNING)


@dataclass
class PaperPosition:
    """Symulowana pozycja."""
    side: str
    entry_price: float
    size_usdc: float
    shares: float
    entry_time: float
    take_profit: float
    stop_loss: float

    def pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.shares

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price == 0:
            return 0
        return ((current_price - self.entry_price) / self.entry_price) * 100

    def should_take_profit(self, current_price: float) -> bool:
        return current_price >= self.entry_price + self.take_profit

    def should_stop_loss(self, current_price: float) -> bool:
        return current_price <= self.entry_price - self.stop_loss


class PaperTraderWS:
    """Paper trading engine z WebSocket real-time data."""

    def __init__(
        self,
        coin: str = "ETH",
        size_usdc: float = 10.0,
        drop_threshold: float = 0.15,
        take_profit: float = 0.10,
        stop_loss: float = 0.05,
        lookback: int = 50,
    ):
        self.coin = coin
        self.size_usdc = size_usdc
        self.drop_threshold = drop_threshold
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.lookback = lookback

        # State
        self.position: Optional[PaperPosition] = None
        self.trades: list = []
        self.price_history: Dict[str, deque] = {
            "up": deque(maxlen=lookback),
            "down": deque(maxlen=lookback),
        }
        self.token_to_side: Dict[str, str] = {}
        self.current_prices: Dict[str, float] = {}
        self.current_slug: str = ""
        self.update_count: int = 0

        # Components
        self.gamma = GammaClient()
        self.ws = MarketWebSocket()
        self.needs_reconnect = False

    def log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = {"INFO": " ", "BUY": "+", "SELL": "-", "WIN": "$", "LOSS": "!", "WARN": "?", "WS": "~"}
        print(f"[{ts}] [{prefix.get(level, ' ')}] {msg}")

    def detect_flash_crash(self, side: str, current_price: float) -> Optional[float]:
        history = self.price_history[side]
        if len(history) < 5:
            return None
        max_recent = max(history)
        drop = max_recent - current_price
        if drop >= self.drop_threshold:
            return drop
        return None

    def paper_buy(self, side: str, price: float, drop: float):
        shares = self.size_usdc / price
        self.position = PaperPosition(
            side=side,
            entry_price=price,
            size_usdc=self.size_usdc,
            shares=shares,
            entry_time=time.time(),
            take_profit=self.take_profit,
            stop_loss=self.stop_loss,
        )
        self.log(
            f"PAPER BUY {side.upper()} @ {price:.4f} | "
            f"${self.size_usdc:.2f} = {shares:.1f} shares | "
            f"drop was {drop:.4f} | "
            f"TP @ {price + self.take_profit:.4f} | SL @ {price - self.stop_loss:.4f}",
            "BUY"
        )

    def paper_sell(self, price: float, reason: str):
        if not self.position:
            return
        pnl = self.position.pnl(price)
        pnl_pct = self.position.pnl_pct(price)
        hold_time = time.time() - self.position.entry_time
        self.trades.append({
            "side": self.position.side,
            "entry": self.position.entry_price,
            "exit": price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "hold_seconds": hold_time,
            "reason": reason,
            "time": datetime.now().isoformat(),
        })
        level = "WIN" if pnl >= 0 else "LOSS"
        self.log(
            f"PAPER SELL {self.position.side.upper()} @ {price:.4f} | "
            f"{reason} | PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | "
            f"hold: {hold_time:.0f}s",
            level
        )
        self.position = None

    def get_stats(self) -> dict:
        wins = [t for t in self.trades if t["pnl"] >= 0]
        losses = [t for t in self.trades if t["pnl"] < 0]
        total_pnl = sum(t["pnl"] for t in self.trades)
        return {
            "total": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "total_pnl": total_pnl,
            "win_rate": (len(wins) / len(self.trades) * 100) if self.trades else 0,
        }

    def print_status(self):
        up = self.current_prices.get("up", 0)
        down = self.current_prices.get("down", 0)
        pos_str = ""
        if self.position:
            current = self.current_prices.get(self.position.side, 0)
            pnl = self.position.pnl(current)
            pos_str = f" | POS: {self.position.side.upper()} @ {self.position.entry_price:.4f} PnL: ${pnl:+.2f}"
        stats = self.get_stats()
        print(
            f"\r[{datetime.now().strftime('%H:%M:%S')}] "
            f"{self.coin} UP={up:.4f} DOWN={down:.4f}"
            f"{pos_str} | "
            f"W/L: {stats['wins']}/{stats['losses']} PnL: ${stats['total_pnl']:+.2f} "
            f"| WS updates: {self.update_count}    ",
            end="", flush=True
        )

    def print_summary(self):
        print("\n")
        self.log("=" * 60)
        self.log("PAPER TRADING SESSION SUMMARY (WebSocket)")
        self.log("=" * 60)
        stats = self.get_stats()
        self.log(f"Coin: {self.coin}")
        self.log(f"Trades: {stats['total']}")
        self.log(f"Wins: {stats['wins']} | Losses: {stats['losses']}")
        self.log(f"Win rate: {stats['win_rate']:.1f}%")
        self.log(f"Total PnL: ${stats['total_pnl']:+.2f}")
        self.log(f"WebSocket updates received: {self.update_count}")
        self.log(f"Drop threshold: {self.drop_threshold}")
        self.log(f"TP: +{self.take_profit} | SL: -{self.stop_loss}")
        if self.trades:
            self.log("")
            self.log("Trade log:")
            for i, t in enumerate(self.trades, 1):
                self.log(
                    f"  #{i} {t['side'].upper()} "
                    f"entry={t['entry']:.4f} exit={t['exit']:.4f} "
                    f"PnL=${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%) "
                    f"{t['reason']} hold={t['hold_seconds']:.0f}s"
                )

    async def on_book_update(self, snapshot: OrderbookSnapshot):
        """Callback z WebSocket — nowy orderbook snapshot."""
        self.update_count += 1
        asset_id = snapshot.asset_id
        side = self.token_to_side.get(asset_id)
        if not side:
            return

        mid = snapshot.mid_price
        self.current_prices[side] = mid
        self.price_history[side].append(mid)

        # Check TP/SL
        if self.position and self.position.side == side:
            current = mid
            if self.position.should_take_profit(current):
                print()
                self.paper_sell(current, "TAKE PROFIT")
            elif self.position.should_stop_loss(current):
                print()
                self.paper_sell(current, "STOP LOSS")

        # Detect flash crash (jeśli nie mamy pozycji)
        if not self.position:
            drop = self.detect_flash_crash(side, mid)
            if drop:
                print()
                self.paper_buy(side, mid, drop)

        # Status co 10 updates
        if self.update_count % 10 == 0:
            self.print_status()

    async def discover_and_subscribe(self) -> bool:
        """Znajdź aktywny rynek i subskrybuj WebSocket."""
        market = self.gamma.get_market_info(self.coin)
        if not market:
            self.log(f"No active 15min market for {self.coin}", "WARN")
            return False

        slug = market["slug"]
        token_ids = market["token_ids"]

        if slug != self.current_slug:
            # Zamknij pozycję przy zmianie rynku
            if self.position and self.current_prices.get(self.position.side, 0) > 0:
                print()
                self.paper_sell(
                    self.current_prices[self.position.side],
                    "MARKET ENDED"
                )

            self.current_slug = slug
            self.price_history = {"up": deque(maxlen=self.lookback), "down": deque(maxlen=self.lookback)}

            # Map token IDs to sides
            self.token_to_side = {}
            for side, token_id in token_ids.items():
                self.token_to_side[token_id] = side

            print()
            self.log(f"Market: {market['question']}")
            self.log(f"Token IDs: UP={token_ids.get('up', '?')[:16]}... DOWN={token_ids.get('down', '?')[:16]}...")

            # Reconnect WebSocket z nowymi tokenami
            # (subscribe z replace nie działa po zmianie rynku — trzeba reconnect)
            self.needs_reconnect = True

        return True

    async def market_refresh_loop(self):
        """Co 30s sprawdzaj czy rynek się nie zmienił."""
        while True:
            await asyncio.sleep(30)
            try:
                await self.discover_and_subscribe()
            except Exception as e:
                self.log(f"Market refresh error: {e}", "WARN")

    async def run(self):
        """Główna pętla — reconnect przy zmianie rynku."""
        self.log(f"Paper trader v2 (WebSocket) starting: {self.coin}")
        self.log(f"Size: ${self.size_usdc} | Drop: {self.drop_threshold} | TP: +{self.take_profit} | SL: -{self.stop_loss}")
        self.log("Ctrl+C to stop")
        self.log("")

        try:
            while True:
                # Discover market
                if not await self.discover_and_subscribe():
                    self.log("Cannot find active market, waiting...", "WARN")
                    await asyncio.sleep(10)
                    continue

                self.needs_reconnect = False

                # Fresh WebSocket per market
                self.ws = MarketWebSocket()
                self.ws.on_book(self.on_book_update)

                @self.ws.on_connect
                def on_connect():
                    self.log("WebSocket connected", "WS")

                @self.ws.on_disconnect
                def on_disconnect():
                    self.log("WebSocket disconnected", "WS")

                # Subscribe to current tokens
                all_tokens = list(self.token_to_side.keys())
                await self.ws.subscribe(all_tokens)

                # Run WS + market checker in parallel
                # Market checker sets needs_reconnect=True when market changes
                ws_task = asyncio.create_task(self.ws.run(auto_reconnect=True))
                refresh_task = asyncio.create_task(self.market_refresh_loop())

                # Wait until reconnect needed
                while not self.needs_reconnect:
                    await asyncio.sleep(1)

                # Stop current WS + refresh, then loop back to reconnect
                self.ws.stop()
                refresh_task.cancel()
                try:
                    await ws_task
                except Exception:
                    pass
                try:
                    await refresh_task
                except asyncio.CancelledError:
                    pass

                self.log("Reconnecting for new market...", "WS")

        except asyncio.CancelledError:
            pass
        finally:
            self.print_summary()


async def main():
    parser = argparse.ArgumentParser(description="Polymarket Paper Trader (WebSocket)")
    parser.add_argument("--coin", default="ETH", choices=["BTC", "ETH", "SOL", "XRP"])
    parser.add_argument("--size", type=float, default=10.0, help="Position size in USDC")
    parser.add_argument("--drop", type=float, default=0.15, help="Flash crash threshold")
    parser.add_argument("--tp", type=float, default=0.10, help="Take profit")
    parser.add_argument("--sl", type=float, default=0.05, help="Stop loss")
    args = parser.parse_args()

    trader = PaperTraderWS(
        coin=args.coin,
        size_usdc=args.size,
        drop_threshold=args.drop,
        take_profit=args.tp,
        stop_loss=args.sl,
    )

    try:
        await trader.run()
    except KeyboardInterrupt:
        trader.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
