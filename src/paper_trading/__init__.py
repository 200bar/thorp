"""
Paper trading primitives — exchange-agnostic building blocks for simulated trading.

Public API:
    PaperPosition: simulated position with PnL and TP/SL helpers
    PaperConfig:   dataclass with paper trading defaults (size, TP/SL, lookback)
    with_retry, with_retry_async: retry/backoff helpers (sync + async)
    PaperTraderBase: shared base with paper_buy/paper_sell, log, stats, summary

Top-level scripts (paper_trader.py, paper_trader_ws.py, paper_trader_ai.py)
import from here and add their own strategy logic on top.
"""

from .config import PaperConfig
from .executors import OrderExecutor, TakerExecutor, make_executor
from .position import PaperPosition
from .retry import with_retry, with_retry_async
from .trader import PaperTraderBase

__all__ = [
    "OrderExecutor",
    "PaperConfig",
    "PaperPosition",
    "PaperTraderBase",
    "TakerExecutor",
    "make_executor",
    "with_retry",
    "with_retry_async",
]
