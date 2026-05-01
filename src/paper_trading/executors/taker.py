"""TakerExecutor — instant fill at the requested price.

Current paper trader behavior. Simulates a taker order on Polymarket CLOB:
order matches whatever resting liquidity exists at the mid price, fills
immediately, pays the taker fee (which paper trader does NOT yet model —
that's a TODO when we move toward real-money sizing).
"""

import time
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from .base import OrderExecutor
from ..position import PaperPosition

if TYPE_CHECKING:
    from ..trader import PaperTraderBase


class TakerExecutor(OrderExecutor):
    """Instant-fill executor. Direct port of pre-refactor paper_buy/paper_sell."""

    def submit_buy(
        self,
        trader: "PaperTraderBase",
        side: str,
        price: float,
        reason: str = "",
        extra_log: str = "",
    ) -> Optional[PaperPosition]:
        shares = trader.config.size_usdc / price
        trader.position = PaperPosition(
            side=side,
            entry_price=price,
            size_usdc=trader.config.size_usdc,
            shares=shares,
            entry_time=time.time(),
            take_profit=trader.config.take_profit,
            stop_loss=trader.config.stop_loss,
            reason=reason,
        )
        msg = (
            f"PAPER BUY {side.upper()} @ {price:.4f} | "
            f"${trader.config.size_usdc:.2f} = {shares:.1f} shares | "
            f"TP @ {price + trader.config.take_profit:.4f} | "
            f"SL @ {price - trader.config.stop_loss:.4f}"
        )
        if extra_log:
            msg = f"{msg} | {extra_log}"
        trader.log(msg, "BUY")
        return trader.position

    def submit_sell(
        self,
        trader: "PaperTraderBase",
        price: float,
        reason: str,
        extra_log: str = "",
    ) -> Optional[dict]:
        if not trader.position:
            return None

        pnl = trader.position.pnl(price)
        pnl_pct = trader.position.pnl_pct(price)
        hold_time = time.time() - trader.position.entry_time

        trade = {
            "side": trader.position.side,
            "entry": trader.position.entry_price,
            "exit": price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "hold_seconds": hold_time,
            "reason": reason,
            "entry_reason": trader.position.reason,
            "time": datetime.now().isoformat(),
        }
        trader.trades.append(trade)

        level = "WIN" if pnl >= 0 else "LOSS"
        msg = (
            f"PAPER SELL {trader.position.side.upper()} @ {price:.4f} | "
            f"{reason} | PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | "
            f"hold: {hold_time:.0f}s"
        )
        if extra_log:
            msg = f"{msg} | {extra_log}"
        trader.log(msg, level)

        trader.position = None
        trader._on_trade_closed(trade)
        return trade
