"""Static registry of built-in pure Python backtest strategies (ARCH-001)."""

from __future__ import annotations

from app.engine.strategies.ict_fvg_sweep import IctFvgSweepStrategy
from app.engine.strategy_base import BaseStrategy, create_strategy, list_strategies

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    IctFvgSweepStrategy.strategy_id: IctFvgSweepStrategy,
}

__all__ = [
    "STRATEGY_REGISTRY",
    "IctFvgSweepStrategy",
    "create_strategy",
    "list_strategies",
]
