"""Retry helpers — fail-loud after exhausting attempts.

Replaces silent `try: ... except Exception: pass` patterns scattered across
paper_trader_*.py (Unix Philosophy Reguła 12 — naprawa: when something must
fail, fail loudly and fast).

Usage:
    # Sync (paper_trader.py)
    market = with_retry(lambda: gamma.get_market_info(coin), log_fn=self.log)

    # Async (paper_trader_ai.py, paper_trader_ws.py)
    decision = await with_retry_async(
        lambda: self.claude.messages.create(...),
        log_fn=self.log,
    )
"""

import asyncio
import time
from typing import Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")
LogFn = Callable[[str, str], None]  # (msg, level) -> None


def with_retry(
    fn: Callable[[], T],
    *,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    log_fn: Optional[LogFn] = None,
) -> T:
    """Execute `fn` with exponential backoff; raise final exception if all retries fail.

    Backoff schedule: 2s, 4s, 8s (with backoff_base=2.0).

    Args:
        fn: Zero-arg callable to execute.
        max_retries: Maximum attempts (>=1). Default 3.
        backoff_base: Exponential base (wait = base ** attempt). Default 2.0.
        log_fn: Optional logger of signature (msg, level). If provided, each
                retry attempt and final failure are logged.

    Returns:
        Whatever `fn` returns.

    Raises:
        Whatever `fn` raised on the last attempt — full exception type and
        traceback preserved (no silent swallow).
    """
    if max_retries < 1:
        raise ValueError(f"max_retries must be >= 1, got {max_retries}")

    last_error: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_error = e
            if attempt == max_retries:
                if log_fn:
                    log_fn(f"Failed after {max_retries} retries: {e}", "WARN")
                raise
            wait = backoff_base ** attempt
            if log_fn:
                log_fn(
                    f"Try {attempt}/{max_retries} failed: {e}. Retry in {wait:.0f}s.",
                    "WARN",
                )
            time.sleep(wait)

    # Unreachable: loop either returns or raises.
    assert last_error is not None
    raise last_error


async def with_retry_async(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    log_fn: Optional[LogFn] = None,
) -> T:
    """Async equivalent of `with_retry` for coroutines.

    Note: `coro_factory` must create a fresh awaitable per attempt
    (a single coroutine cannot be awaited twice).
    """
    if max_retries < 1:
        raise ValueError(f"max_retries must be >= 1, got {max_retries}")

    last_error: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        try:
            return await coro_factory()
        except Exception as e:
            last_error = e
            if attempt == max_retries:
                if log_fn:
                    log_fn(f"Failed after {max_retries} retries: {e}", "WARN")
                raise
            wait = backoff_base ** attempt
            if log_fn:
                log_fn(
                    f"Try {attempt}/{max_retries} failed: {e}. Retry in {wait:.0f}s.",
                    "WARN",
                )
            await asyncio.sleep(wait)

    assert last_error is not None
    raise last_error
