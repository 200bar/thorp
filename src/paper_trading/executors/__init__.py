"""Order executors — encapsulate the policy of how an order becomes a fill.

The trader holds an OrderExecutor reference and delegates buy/sell to it.
This lets us swap taker (instant fill) vs maker (limit order, may not fill)
vs future variants without rewriting the strategy logic.

Interface lives in `base.py`. Concrete:
    - TakerExecutor — instant fill at requested price (current paper behavior).
    - MakerExecutor — to be added in week 2 (limit order simulation).
"""

from .base import OrderExecutor
from .taker import TakerExecutor


def make_executor(name: str) -> OrderExecutor:
    """Factory: pick executor by name (from YAML config or CLI flag)."""
    name = name.lower().strip()
    if name == "taker":
        return TakerExecutor()
    # Maker executor will land in a follow-up commit (week 2).
    raise ValueError(
        f"Unknown executor '{name}'. Supported: taker. "
        f"(maker coming in week 2 — see ROADMAP.md)"
    )


__all__ = ["OrderExecutor", "TakerExecutor", "make_executor"]
