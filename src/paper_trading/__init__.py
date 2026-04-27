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

from .position import PaperPosition

__all__ = ["PaperPosition"]
