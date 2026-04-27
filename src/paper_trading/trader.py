"""PaperTraderBase — common state and behavior for all paper trader variants.

What's IN this base:
    - State: config, position, trades, price_history, current_prices,
             current_slug, update_count
    - log(msg, level): unified prefixes (INFO/BUY/SELL/WIN/LOSS/WARN/WS/AI)
    - paper_buy(side, price, reason, extra_log): create PaperPosition, log
    - paper_sell(price, reason, extra_log): close position, append to trades,
                                            call _on_trade_closed hook
    - get_stats(): wins/losses/total_pnl/win_rate
    - _emit_status(text): TTY-aware status line (\\r when terminal, newline
                          when piped — fixes pipe-unfriendly behavior)
    - print_summary(): end-of-session report with _summary_extras hook

What's NOT in this base (stays in subclasses, strategy-specific):
    - detect_flash_crash, ask_claude, ai_loop (strategy decision logic)
    - discover_and_subscribe, market_refresh_loop (per-variant market mgmt)
    - run() / async run() (per-variant main loop)
    - WebSocket / Claude / Binance clients (per-variant components)
    - patience cooldown, consecutive_losses tracking (AI-specific)

Extension hooks (override in subclasses):
    - _on_trade_closed(trade): called after paper_sell appends to trades.
                               Used by ClaudeTrader to update consecutive_losses.
    - _summary_extras(): list of additional summary lines.
    - _trade_log_extra(trade): per-trade extra info in summary trade log.
"""

import sys
import time
from collections import deque
from datetime import datetime
from typing import Dict, Optional

from .config import PaperConfig
from .position import PaperPosition


class PaperTraderBase:
    """Shared state and helpers for paper trading variants."""

    # Log level -> single-char prefix for visual scanning in terminal output.
    LOG_PREFIXES: Dict[str, str] = {
        "INFO": " ",
        "BUY": "+",
        "SELL": "-",
        "WIN": "$",
        "LOSS": "!",
        "WARN": "?",
        "WS": "~",
        "AI": "*",
    }

    def __init__(self, config: PaperConfig):
        self.config = config

        # Position / trade state
        self.position: Optional[PaperPosition] = None
        self.trades: list = []

        # Market data state
        self.price_history: Dict[str, deque] = {
            "up": deque(maxlen=config.lookback),
            "down": deque(maxlen=config.lookback),
        }
        self.current_prices: Dict[str, float] = {}
        self.current_slug: str = ""
        self.update_count: int = 0

    # --- Logging --------------------------------------------------------

    def log(self, msg: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = self.LOG_PREFIXES.get(level, " ")
        print(f"[{ts}] [{prefix}] {msg}")

    def _emit_status(self, text: str) -> None:
        """Emit a status line — overwriting (\\r) when stdout is a TTY,
        newline-terminated when piped (so `tee log.txt` produces clean output).
        """
        if sys.stdout.isatty():
            print(f"\r{text}", end="", flush=True)
        else:
            print(text, flush=True)

    # --- Trade actions --------------------------------------------------

    def paper_buy(
        self,
        side: str,
        price: float,
        reason: str = "",
        extra_log: str = "",
    ) -> PaperPosition:
        """Open a simulated position. Records to self.position and logs."""
        shares = self.config.size_usdc / price
        self.position = PaperPosition(
            side=side,
            entry_price=price,
            size_usdc=self.config.size_usdc,
            shares=shares,
            entry_time=time.time(),
            take_profit=self.config.take_profit,
            stop_loss=self.config.stop_loss,
            reason=reason,
        )
        msg = (
            f"PAPER BUY {side.upper()} @ {price:.4f} | "
            f"${self.config.size_usdc:.2f} = {shares:.1f} shares | "
            f"TP @ {price + self.config.take_profit:.4f} | "
            f"SL @ {price - self.config.stop_loss:.4f}"
        )
        if extra_log:
            msg = f"{msg} | {extra_log}"
        self.log(msg, "BUY")
        return self.position

    def paper_sell(
        self,
        price: float,
        reason: str,
        extra_log: str = "",
    ) -> Optional[dict]:
        """Close current position, append trade record, log result.

        Returns the trade dict, or None if no position was open.
        Subclasses can react to trade closure via `_on_trade_closed`.
        """
        if not self.position:
            return None

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
            "entry_reason": self.position.reason,
            "time": datetime.now().isoformat(),
        }
        self.trades.append(trade)

        level = "WIN" if pnl >= 0 else "LOSS"
        msg = (
            f"PAPER SELL {self.position.side.upper()} @ {price:.4f} | "
            f"{reason} | PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | "
            f"hold: {hold_time:.0f}s"
        )
        if extra_log:
            msg = f"{msg} | {extra_log}"
        self.log(msg, level)

        self.position = None
        self._on_trade_closed(trade)
        return trade

    def _on_trade_closed(self, trade: dict) -> None:
        """Hook called after a trade is appended. Override in subclasses
        that need to react (e.g., update consecutive_losses for patience mode).
        """
        return None

    # --- Stats ----------------------------------------------------------

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

    # --- Summary --------------------------------------------------------

    def print_summary(self) -> None:
        """End-of-session summary. Subclasses can extend via the two hooks."""
        print("\n")
        self.log("=" * 60)
        self.log(f"PAPER TRADING SESSION SUMMARY ({self.__class__.__name__})")
        self.log("=" * 60)

        stats = self.get_stats()
        self.log(f"Coin: {self.config.coin}")
        self.log(f"Trades: {stats['total']}")
        self.log(f"Wins: {stats['wins']} | Losses: {stats['losses']}")
        self.log(f"Win rate: {stats['win_rate']:.1f}%")
        self.log(f"Total PnL: ${stats['total_pnl']:+.2f}")
        self.log(f"TP: +{self.config.take_profit} | SL: -{self.config.stop_loss}")

        for line in self._summary_extras():
            self.log(line)

        if self.trades:
            self.log("")
            self.log("Trade log:")
            for i, t in enumerate(self.trades, 1):
                line = (
                    f"  #{i} {t['side'].upper()} "
                    f"entry={t['entry']:.4f} exit={t['exit']:.4f} "
                    f"PnL=${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%) "
                    f"{t['reason']} hold={t['hold_seconds']:.0f}s"
                )
                extra = self._trade_log_extra(t)
                if extra:
                    line = f"{line} | {extra}"
                self.log(line)

    def _summary_extras(self) -> list:
        """Return additional summary lines for strategy-specific info.
        Override in subclasses (default: no extras).
        """
        return []

    def _trade_log_extra(self, trade: dict) -> str:
        """Return extra info for a single trade in summary log.
        Override in subclasses (default: empty).
        """
        return ""
