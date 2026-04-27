"""Paper position — single source of truth for simulated positions."""

from dataclasses import dataclass


@dataclass
class PaperPosition:
    """Simulated position with PnL and exit-trigger helpers.

    Used by all paper trading variants (sync polling, WebSocket, AI).
    `reason` is optional metadata about what triggered the entry — used by
    AI variant, ignored by flash-crash variants (defaults to empty).
    """

    side: str            # "up" or "down"
    entry_price: float
    size_usdc: float
    shares: float
    entry_time: float
    take_profit: float
    stop_loss: float
    reason: str = ""

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
