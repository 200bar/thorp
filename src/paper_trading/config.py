"""Paper trading config — defaults shared across all paper trader variants.

Strategy-specific tuning (flash-crash drop_threshold, AI confidence_threshold,
patience parameters, etc.) lives in the strategy classes themselves. This
file contains only the universal paper-trading defaults.

Supported coins are sourced from `GammaClient.COIN_SLUGS` — single source
of truth, no more hardcoded ['BTC', 'ETH', 'SOL', 'XRP'] in argparse choices.
"""

from dataclasses import dataclass


@dataclass
class PaperConfig:
    """Universal paper trading parameters.

    Each top-level script (paper_trader.py, paper_trader_ws.py,
    paper_trader_ai.py) constructs PaperConfig from argparse, then
    passes it to its strategy-specific Trader class.
    """

    coin: str = "ETH"
    size_usdc: float = 10.0
    take_profit: float = 0.10
    stop_loss: float = 0.05
    lookback: int = 50

    def __post_init__(self) -> None:
        # Fail-loud on invalid params (Unix Philosophy Reguła 12).
        # Trading bot with size_usdc=0 is a silent bug — surface it now.
        coin_upper = self.coin.upper()
        supported = self.supported_coins()
        if coin_upper not in supported:
            raise ValueError(
                f"Unsupported coin: {self.coin!r}. Supported: {supported}"
            )
        self.coin = coin_upper

        if self.size_usdc <= 0:
            raise ValueError(f"size_usdc must be > 0, got {self.size_usdc}")
        if self.take_profit <= 0:
            raise ValueError(f"take_profit must be > 0, got {self.take_profit}")
        if self.stop_loss <= 0:
            raise ValueError(f"stop_loss must be > 0, got {self.stop_loss}")
        if self.lookback <= 0:
            raise ValueError(f"lookback must be > 0, got {self.lookback}")

    @classmethod
    def supported_coins(cls) -> list[str]:
        """Return list of coin symbols supported by the upstream Gamma API.

        Sourced from GammaClient.COIN_SLUGS — single source of truth.
        Lazy import to avoid circular dependency at module load.
        """
        from src.gamma_client import GammaClient
        return list(GammaClient.COIN_SLUGS.keys())
