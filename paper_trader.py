"""
Paper Trading Bot — symulacja bez prawdziwych orderów.

Łączy się z Polymarket Gamma API (publiczny, read-only),
monitoruje ceny 15-min rynków i loguje co BY zrobił bot.

Użycie:
    source .venv/bin/activate
    python paper_trader.py --coin ETH --size 10

    --coin: BTC, ETH, SOL, XRP (default: ETH)
    --size: wielkość pozycji w USDC (default: 10)
    --interval: interwał pollingu w sekundach (default: 5)
    --drop: próg flash crash (default: 0.15)
"""

import argparse
import time
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from collections import deque

from src.gamma_client import GammaClient


@dataclass
class PaperPosition:
    """Symulowana pozycja."""
    side: str           # "up" or "down"
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


@dataclass
class PaperTrader:
    """Paper trading engine."""
    coin: str = "ETH"
    size_usdc: float = 10.0
    poll_interval: float = 5.0
    drop_threshold: float = 0.15
    take_profit: float = 0.10
    stop_loss: float = 0.05
    lookback: int = 12  # ile ostatnich cen trzymać (12 * 5s = 60s)

    # State
    position: Optional[PaperPosition] = None
    trades: list = field(default_factory=list)
    price_history: dict = field(default_factory=lambda: {"up": deque(maxlen=12), "down": deque(maxlen=12)})
    current_slug: str = ""

    def log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = {"INFO": " ", "BUY": "+", "SELL": "-", "WIN": "$", "LOSS": "!", "WARN": "?"}
        print(f"[{ts}] [{prefix.get(level, ' ')}] {msg}")

    def detect_flash_crash(self, side: str, current_price: float) -> Optional[float]:
        """Sprawdź czy cena spadła o threshold w oknie lookback."""
        history = self.price_history[side]
        if len(history) < 3:
            return None

        max_recent = max(history)
        drop = max_recent - current_price

        if drop >= self.drop_threshold:
            return drop
        return None

    def paper_buy(self, side: str, price: float, drop: float):
        """Symulowany zakup."""
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
        """Symulowana sprzedaż."""
        if not self.position:
            return

        pnl = self.position.pnl(price)
        pnl_pct = self.position.pnl_pct(price)
        hold_time = time.time() - self.position.entry_time

        trade = {
            "side": self.position.side,
            "entry": self.position.entry_price,
            "exit": price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "hold_seconds": hold_time,
            "reason": reason,
            "time": datetime.now().isoformat(),
        }
        self.trades.append(trade)

        level = "WIN" if pnl >= 0 else "LOSS"
        self.log(
            f"PAPER SELL {self.position.side.upper()} @ {price:.4f} | "
            f"{reason} | PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | "
            f"hold: {hold_time:.0f}s",
            level
        )
        self.position = None

    def check_exits(self, prices: dict):
        """Sprawdź TP/SL."""
        if not self.position:
            return

        current = prices.get(self.position.side, 0)
        if current <= 0:
            return

        if self.position.should_take_profit(current):
            self.paper_sell(current, "TAKE PROFIT")
        elif self.position.should_stop_loss(current):
            self.paper_sell(current, "STOP LOSS")

    def print_status(self, prices: dict, market_question: str):
        """Wyświetl aktualny status."""
        up = prices.get("up", 0)
        down = prices.get("down", 0)

        pos_str = ""
        if self.position:
            current = prices.get(self.position.side, 0)
            pnl = self.position.pnl(current)
            pos_str = f" | POS: {self.position.side.upper()} @ {self.position.entry_price:.4f} PnL: ${pnl:+.2f}"

        stats = self.get_stats()
        stats_str = f"Trades: {stats['total']} | W/L: {stats['wins']}/{stats['losses']} | Total PnL: ${stats['total_pnl']:+.2f}"

        print(
            f"\r[{datetime.now().strftime('%H:%M:%S')}] "
            f"{self.coin} UP={up:.4f} DOWN={down:.4f}"
            f"{pos_str} | {stats_str}    ",
            end="", flush=True
        )

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

    def print_summary(self):
        """Podsumowanie sesji."""
        print("\n")
        self.log("=" * 60)
        self.log("PAPER TRADING SESSION SUMMARY")
        self.log("=" * 60)

        stats = self.get_stats()
        self.log(f"Coin: {self.coin}")
        self.log(f"Trades: {stats['total']}")
        self.log(f"Wins: {stats['wins']} | Losses: {stats['losses']}")
        self.log(f"Win rate: {stats['win_rate']:.1f}%")
        self.log(f"Total PnL: ${stats['total_pnl']:+.2f}")
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

    def run(self):
        """Główna pętla paper tradingu."""
        gamma = GammaClient()

        self.log(f"Paper trader starting: {self.coin}")
        self.log(f"Size: ${self.size_usdc} | Drop threshold: {self.drop_threshold}")
        self.log(f"TP: +{self.take_profit} | SL: -{self.stop_loss}")
        self.log(f"Poll interval: {self.poll_interval}s")
        self.log("Ctrl+C to stop")
        self.log("")

        try:
            while True:
                market = gamma.get_market_info(self.coin)

                if not market:
                    self.log(f"No active 15min market for {self.coin}, waiting...", "WARN")
                    time.sleep(30)
                    continue

                # Nowy rynek?
                slug = market["slug"]
                if slug != self.current_slug:
                    if self.current_slug:
                        self.log(f"Market changed: {slug}")
                        # Zamknij otwartą pozycję przy zmianie rynku
                        if self.position:
                            prices = market["prices"]
                            current = prices.get(self.position.side, 0)
                            if current > 0:
                                self.paper_sell(current, "MARKET ENDED")
                        self.price_history = {"up": deque(maxlen=self.lookback), "down": deque(maxlen=self.lookback)}
                    self.current_slug = slug
                    print()
                    self.log(f"Monitoring: {market['question']}")

                prices = market["prices"]

                # Zapisz historię cen
                for side in ["up", "down"]:
                    p = prices.get(side, 0)
                    if p > 0:
                        self.price_history[side].append(p)

                # Sprawdź TP/SL
                self.check_exits(prices)

                # Szukaj flash crash (jeśli nie mamy pozycji)
                if not self.position:
                    for side in ["up", "down"]:
                        current = prices.get(side, 0)
                        if current <= 0:
                            continue
                        drop = self.detect_flash_crash(side, current)
                        if drop:
                            print()  # nowa linia po status line
                            self.paper_buy(side, current, drop)
                            break

                # Status
                self.print_status(prices, market.get("question", ""))

                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            self.print_summary()


def main():
    parser = argparse.ArgumentParser(description="Polymarket Paper Trader")
    parser.add_argument("--coin", default="ETH", choices=["BTC", "ETH", "SOL", "XRP"])
    parser.add_argument("--size", type=float, default=10.0, help="Position size in USDC")
    parser.add_argument("--interval", type=float, default=5.0, help="Poll interval in seconds")
    parser.add_argument("--drop", type=float, default=0.15, help="Flash crash threshold")
    parser.add_argument("--tp", type=float, default=0.10, help="Take profit")
    parser.add_argument("--sl", type=float, default=0.05, help="Stop loss")
    args = parser.parse_args()

    trader = PaperTrader(
        coin=args.coin,
        size_usdc=args.size,
        poll_interval=args.interval,
        drop_threshold=args.drop,
        take_profit=args.tp,
        stop_loss=args.sl,
    )
    trader.run()


if __name__ == "__main__":
    main()
