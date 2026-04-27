"""
Paper Trading Bot v2 — flash crash strategy on real-time WebSocket data.

Uses Gamma API for market discovery and CLOB WebSocket for live mid_price
updates. Same flash-crash strategy as paper_trader.py but driven by orderbook
events instead of sync polling.

Usage:
    source .venv/bin/activate
    python paper_trader_ws.py --coin ETH --size 10
"""

import argparse
import asyncio
import logging
from collections import deque
from datetime import datetime
from typing import Dict, Optional

from src.gamma_client import GammaClient
from src.paper_trading import PaperConfig, PaperTraderBase
from src.websocket_client import MarketWebSocket, OrderbookSnapshot

# Silence chatty WS logger
logging.getLogger("src.websocket_client").setLevel(logging.WARNING)


class PaperTraderWS(PaperTraderBase):
    """Flash crash strategy on WebSocket real-time orderbook feed."""

    def __init__(self, config: PaperConfig, drop_threshold: float = 0.15):
        super().__init__(config)
        self.drop_threshold = drop_threshold

        # WS-specific state
        self.token_to_side: Dict[str, str] = {}
        self.gamma = GammaClient()
        self.ws = MarketWebSocket()
        self.needs_reconnect = False

    # --- Strategy logic -------------------------------------------------

    def detect_flash_crash(self, side: str, current_price: float) -> Optional[float]:
        """Return drop magnitude if a flash crash is detected on this side."""
        history = self.price_history[side]
        if len(history) < 5:
            return None
        max_recent = max(history)
        drop = max_recent - current_price
        if drop >= self.drop_threshold:
            return drop
        return None

    def _summary_extras(self) -> list:
        return [
            f"WebSocket updates received: {self.update_count}",
            f"Drop threshold: {self.drop_threshold}",
        ]

    # --- UI -------------------------------------------------------------

    def print_status(self) -> None:
        up = self.current_prices.get("up", 0)
        down = self.current_prices.get("down", 0)
        pos_str = ""
        if self.position:
            current = self.current_prices.get(self.position.side, 0)
            pnl = self.position.pnl(current)
            pos_str = (
                f" | POS: {self.position.side.upper()} @ "
                f"{self.position.entry_price:.4f} PnL: ${pnl:+.2f}"
            )
        stats = self.get_stats()
        text = (
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"{self.config.coin} UP={up:.4f} DOWN={down:.4f}"
            f"{pos_str} | "
            f"W/L: {stats['wins']}/{stats['losses']} "
            f"PnL: ${stats['total_pnl']:+.2f} "
            f"| WS updates: {self.update_count}    "
        )
        self._emit_status(text)

    # --- WS callbacks + market mgmt -------------------------------------

    async def on_book_update(self, snapshot: OrderbookSnapshot) -> None:
        """Callback from WebSocket — new orderbook snapshot."""
        self.update_count += 1
        asset_id = snapshot.asset_id
        side = self.token_to_side.get(asset_id)
        if not side:
            return

        mid = snapshot.mid_price
        self.current_prices[side] = mid
        self.price_history[side].append(mid)

        # Check TP/SL on the active position
        if self.position and self.position.side == side:
            if self.position.should_take_profit(mid):
                print()
                self.paper_sell(mid, "TAKE PROFIT")
            elif self.position.should_stop_loss(mid):
                print()
                self.paper_sell(mid, "STOP LOSS")

        # Detect flash crash if no open position
        if not self.position:
            drop = self.detect_flash_crash(side, mid)
            if drop:
                print()
                self.paper_buy(side, mid, extra_log=f"drop was {drop:.4f}")

        # Status every 10 updates
        if self.update_count % 10 == 0:
            self.print_status()

    async def discover_and_subscribe(self) -> bool:
        """Find current 15min market and prepare token mapping. Triggers
        reconnect (sets needs_reconnect=True) when market changes.
        """
        market = self.gamma.get_market_info(self.config.coin)
        if not market:
            self.log(f"No active 15min market for {self.config.coin}", "WARN")
            return False

        slug = market["slug"]
        token_ids = market["token_ids"]

        if slug != self.current_slug:
            # Close any open position when market ends
            if self.position and self.current_prices.get(self.position.side, 0) > 0:
                print()
                self.paper_sell(
                    self.current_prices[self.position.side],
                    "MARKET ENDED",
                )

            self.current_slug = slug
            self.price_history = {
                "up": deque(maxlen=self.config.lookback),
                "down": deque(maxlen=self.config.lookback),
            }

            self.token_to_side = {}
            for side, token_id in token_ids.items():
                self.token_to_side[token_id] = side

            print()
            self.log(f"Market: {market['question']}")
            self.log(
                f"Token IDs: UP={token_ids.get('up', '?')[:16]}... "
                f"DOWN={token_ids.get('down', '?')[:16]}..."
            )

            # Subscribe with replace doesn't work after market change — full reconnect
            self.needs_reconnect = True

        return True

    async def market_refresh_loop(self) -> None:
        """Every 30s check if the active market changed."""
        while True:
            await asyncio.sleep(30)
            try:
                await self.discover_and_subscribe()
            except Exception as e:
                # Don't kill the bot if a single refresh fails — log and retry
                # next tick. Reguła 12 partial: we log loudly but recover.
                self.log(f"Market refresh error: {e}", "WARN")

    async def run(self) -> None:
        """Main loop — reconnect on market change."""
        self.log(f"Paper trader v2 (WebSocket) starting: {self.config.coin}")
        self.log(
            f"Size: ${self.config.size_usdc} | Drop: {self.drop_threshold} "
            f"| TP: +{self.config.take_profit} | SL: -{self.config.stop_loss}"
        )
        self.log("Ctrl+C to stop")
        self.log("")

        try:
            while True:
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

                all_tokens = list(self.token_to_side.keys())
                await self.ws.subscribe(all_tokens)

                ws_task = asyncio.create_task(self.ws.run(auto_reconnect=True))
                refresh_task = asyncio.create_task(self.market_refresh_loop())

                # Wait until reconnect needed (set by market_refresh_loop)
                while not self.needs_reconnect:
                    await asyncio.sleep(1)

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
    defaults = PaperConfig()

    parser = argparse.ArgumentParser(description="Polymarket Paper Trader (WebSocket)")
    parser.add_argument(
        "--coin",
        default=defaults.coin,
        choices=PaperConfig.supported_coins(),
    )
    parser.add_argument("--size", type=float, default=defaults.size_usdc,
                        help="Position size in USDC")
    parser.add_argument("--drop", type=float, default=0.15,
                        help="Flash crash threshold")
    parser.add_argument("--tp", type=float, default=defaults.take_profit,
                        help="Take profit")
    parser.add_argument("--sl", type=float, default=defaults.stop_loss,
                        help="Stop loss")
    args = parser.parse_args()

    config = PaperConfig(
        coin=args.coin,
        size_usdc=args.size,
        take_profit=args.tp,
        stop_loss=args.sl,
    )
    trader = PaperTraderWS(config=config, drop_threshold=args.drop)

    try:
        await trader.run()
    except KeyboardInterrupt:
        trader.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
