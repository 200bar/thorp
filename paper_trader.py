"""
Paper Trading Bot — flash crash strategy, sync polling.

Connects to Polymarket Gamma API (public, read-only), monitors 15-min market
prices, and logs simulated trades when a price drop exceeds the threshold.

Strategy: simple flash-crash detection. When price drops by `--drop` within
the lookback window, buy that side. Exit on TP/SL or market end.

Usage:
    source .venv/bin/activate
    python paper_trader.py --coin ETH --size 10
"""

import argparse
import time
from collections import deque
from datetime import datetime
from typing import Optional

from src.gamma_client import GammaClient
from src.paper_trading import PaperConfig, PaperTraderBase, with_retry


class PaperTrader(PaperTraderBase):
    """Flash crash strategy on sync polling."""

    def __init__(
        self,
        config: PaperConfig,
        drop_threshold: float = 0.15,
        poll_interval: float = 5.0,
    ):
        super().__init__(config)
        self.drop_threshold = drop_threshold
        self.poll_interval = poll_interval

    # --- Strategy logic -------------------------------------------------

    def detect_flash_crash(self, side: str, current_price: float) -> Optional[float]:
        """Return drop magnitude if a flash crash is detected on this side."""
        history = self.price_history[side]
        if len(history) < 3:
            return None
        max_recent = max(history)
        drop = max_recent - current_price
        if drop >= self.drop_threshold:
            return drop
        return None

    def check_exits(self, prices: dict) -> None:
        """Check TP/SL on the current open position (if any)."""
        if not self.position:
            return
        current = prices.get(self.position.side, 0)
        if current <= 0:
            return
        if self.position.should_take_profit(current):
            self.paper_sell(current, "TAKE PROFIT")
        elif self.position.should_stop_loss(current):
            self.paper_sell(current, "STOP LOSS")

    # --- UI -------------------------------------------------------------

    def print_status(self, prices: dict) -> None:
        up = prices.get("up", 0)
        down = prices.get("down", 0)
        pos_str = ""
        if self.position:
            current = prices.get(self.position.side, 0)
            pnl = self.position.pnl(current)
            pos_str = (
                f" | POS: {self.position.side.upper()} @ "
                f"{self.position.entry_price:.4f} PnL: ${pnl:+.2f}"
            )
        stats = self.get_stats()
        stats_str = (
            f"Trades: {stats['total']} | W/L: {stats['wins']}/{stats['losses']} "
            f"| Total PnL: ${stats['total_pnl']:+.2f}"
        )
        text = (
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"{self.config.coin} UP={up:.4f} DOWN={down:.4f}"
            f"{pos_str} | {stats_str}    "
        )
        self._emit_status(text)

    def _summary_extras(self) -> list:
        return [f"Drop threshold: {self.drop_threshold}"]

    # --- Main loop ------------------------------------------------------

    def run(self) -> None:
        gamma = GammaClient()

        self.log(f"Paper trader starting: {self.config.coin}")
        self.log(f"Size: ${self.config.size_usdc} | Drop threshold: {self.drop_threshold}")
        self.log(f"TP: +{self.config.take_profit} | SL: -{self.config.stop_loss}")
        self.log(f"Poll interval: {self.poll_interval}s")
        self.log("Ctrl+C to stop")
        self.log("")

        try:
            while True:
                # with_retry: 3 attempts with exponential backoff. If gamma stays
                # down past max retries, we re-raise to outer try (KeyboardInterrupt
                # path doesn't apply, so the bot dies loudly — better than silent
                # incorrect behavior).
                market = with_retry(
                    lambda: gamma.get_market_info(self.config.coin),
                    log_fn=self.log,
                )

                if not market:
                    self.log(
                        f"No active 15min market for {self.config.coin}, waiting...",
                        "WARN",
                    )
                    time.sleep(30)
                    continue

                slug = market["slug"]
                if slug != self.current_slug:
                    if self.current_slug:
                        self.log(f"Market changed: {slug}")
                        # Close any open position when market ends
                        if self.position:
                            prices = market["prices"]
                            current = prices.get(self.position.side, 0)
                            if current > 0:
                                self.paper_sell(current, "MARKET ENDED")
                        self.price_history = {
                            "up": deque(maxlen=self.config.lookback),
                            "down": deque(maxlen=self.config.lookback),
                        }
                    self.current_slug = slug
                    print()
                    self.log(f"Monitoring: {market['question']}")

                prices = market["prices"]
                for side in ["up", "down"]:
                    p = prices.get(side, 0)
                    if p > 0:
                        self.price_history[side].append(p)

                self.check_exits(prices)

                # Look for flash crash if no open position
                if not self.position:
                    for side in ["up", "down"]:
                        current = prices.get(side, 0)
                        if current <= 0:
                            continue
                        drop = self.detect_flash_crash(side, current)
                        if drop:
                            print()
                            self.paper_buy(
                                side, current, extra_log=f"drop was {drop:.4f}"
                            )
                            break

                self.print_status(prices)
                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            self.print_summary()


def main():
    defaults = PaperConfig()  # Single source of truth for argparse defaults

    parser = argparse.ArgumentParser(description="Polymarket Paper Trader")
    parser.add_argument(
        "--coin",
        default=defaults.coin,
        choices=PaperConfig.supported_coins(),
    )
    parser.add_argument("--size", type=float, default=defaults.size_usdc,
                        help="Position size in USDC")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="Poll interval in seconds")
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
        # lookback default (50) is fine for sync polling; flash crash needs
        # only ~3 ticks, so deque maxlen rarely matters here.
    )
    trader = PaperTrader(
        config=config,
        drop_threshold=args.drop,
        poll_interval=args.interval,
    )
    trader.run()


if __name__ == "__main__":
    main()
