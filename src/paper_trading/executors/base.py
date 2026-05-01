"""OrderExecutor — abstract interface for order placement.

Two responsibilities split between trader and executor:
    - Trader (policy): decides WHEN and WHAT to trade (BUY UP, SELL, HOLD).
    - Executor (mechanism): decides HOW the order becomes a fill.

This separation (Unix philosophy R4: separate policy from mechanism) lets us
swap execution strategies — taker for paper sims, maker for live trading
where fees matter, future variants for arbitrage / market making — without
touching the AI/strategy code.
"""

from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..trader import PaperTraderBase
    from ..position import PaperPosition


class OrderExecutor(ABC):
    """Abstract executor. Subclasses implement specific fill mechanics.

    Stateless by default — concrete executors may hold pending-order state
    (e.g., MakerExecutor tracks limit orders waiting for the book to touch).
    """

    @abstractmethod
    def submit_buy(
        self,
        trader: "PaperTraderBase",
        side: str,
        price: float,
        reason: str = "",
        extra_log: str = "",
    ) -> Optional["PaperPosition"]:
        """Submit a buy order.

        Returns the resulting PaperPosition if filled, or None if pending /
        rejected. Trader treats None as "no position opened, continue loop".
        """
        ...

    @abstractmethod
    def submit_sell(
        self,
        trader: "PaperTraderBase",
        price: float,
        reason: str,
        extra_log: str = "",
    ) -> Optional[dict]:
        """Submit a sell order on the open position.

        Returns the closed-trade dict if filled, or None if no position or
        pending. For taker this fills immediately; maker may queue a limit.
        """
        ...

    def on_book_update(self, trader: "PaperTraderBase", snapshot) -> None:
        """Hook called by the trader on every WS book update.

        Default: no-op (taker doesn't have pending orders to check).
        Override in MakerExecutor to inspect pending limit orders against
        the new book snapshot and fill them when the book touches the price.
        """
        return None

    def name(self) -> str:
        """Short label for logging — defaults to class name minus 'Executor'."""
        return self.__class__.__name__.replace("Executor", "").lower()
