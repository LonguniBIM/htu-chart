"""Pure strategy plugin interface, CausalBarView guard, and static registry (F08, ARCH-001)."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, overload

from app.engine.instruments import InstrumentSpec
from app.engine.ohlcv import OHLCVBar


class CausalBarView(Sequence[OHLCVBar]):
    """Read-only O(1) prefix view physically preventing look-ahead past bar_index."""

    __slots__ = ("_bars", "_visible_count")

    def __init__(self, bars: Sequence[OHLCVBar], visible_count: int) -> None:
        if isinstance(visible_count, bool) or not isinstance(visible_count, int):
            raise ValueError(f"visible_count must be an int, got {visible_count!r}")
        if visible_count < 0 or visible_count > len(bars):
            raise ValueError(
                f"visible_count ({visible_count}) out of range [0, {len(bars)}]"
            )
        self._bars = bars
        self._visible_count = visible_count

    def __len__(self) -> int:
        return self._visible_count

    @overload
    def __getitem__(self, index: int) -> OHLCVBar: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[OHLCVBar, ...]: ...

    def __getitem__(self, index: int | slice) -> OHLCVBar | tuple[OHLCVBar, ...]:
        if isinstance(index, slice):
            start, stop, step = index.indices(self._visible_count)
            return tuple(self._bars[idx] for idx in range(start, stop, step))
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError(
                f"CausalBarView indices must be integers or slices, not {type(index).__name__}"
            )
        resolved = self._visible_count + index if index < 0 else index
        if resolved < 0 or resolved >= self._visible_count:
            raise IndexError(
                f"CausalBarView index {index} out of range for visible_count={self._visible_count}"
            )
        return self._bars[resolved]

    def __iter__(self) -> Iterator[OHLCVBar]:
        for idx in range(self._visible_count):
            yield self._bars[idx]


@dataclass(frozen=True)
class StrategySignal:
    """Causal entry signal emitted at bar[i] close for execution no earlier than bar[i+1]."""

    direction: Literal["LONG", "SHORT"]
    signal_bar_index: int
    signal_time: int
    stop_loss: float
    take_profit: float
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    quantity: int = 1
    max_wait_bars: int = 5
    reason: str = ""

    def __post_init__(self) -> None:
        if self.direction not in {"LONG", "SHORT"}:
            raise ValueError(f"Invalid signal direction: {self.direction!r}")
        if self.order_type not in {"market", "limit"}:
            raise ValueError(f"Invalid order_type: {self.order_type!r}")
        if isinstance(self.signal_bar_index, bool) or not isinstance(self.signal_bar_index, int):
            raise ValueError("signal_bar_index must be an int")
        if isinstance(self.signal_time, bool) or not isinstance(self.signal_time, int):
            raise ValueError("signal_time must be an int")
        if (
            isinstance(self.quantity, bool)
            or not isinstance(self.quantity, int)
            or self.quantity < 1
        ):
            raise ValueError("quantity must be an int >= 1")
        if (
            isinstance(self.max_wait_bars, bool)
            or not isinstance(self.max_wait_bars, int)
            or self.max_wait_bars < 1
        ):
            raise ValueError("max_wait_bars must be an int >= 1")
        if not math.isfinite(self.stop_loss) or self.stop_loss <= 0.0:
            raise ValueError("stop_loss must be positive and finite")
        if not math.isfinite(self.take_profit) or self.take_profit <= 0.0:
            raise ValueError("take_profit must be positive and finite")
        if self.order_type == "limit":
            if (
                self.limit_price is None
                or not math.isfinite(self.limit_price)
                or self.limit_price <= 0.0
            ):
                raise ValueError("limit_price must be positive and finite for limit orders")
        elif self.limit_price is not None and (
            not math.isfinite(self.limit_price) or self.limit_price <= 0.0
        ):
            raise ValueError("limit_price must be positive and finite when provided")


@dataclass(frozen=True)
class StrategyZone:
    """Detected chart zone with mandatory F08 confirmation timestamp detected_at_time."""

    kind: Literal["fvg", "order_block", "liquidity_level"]
    direction: Literal["bullish", "bearish"]
    start_time: int
    end_time: int
    detected_at_time: int
    top: float
    bottom: float
    midpoint: float | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"fvg", "order_block", "liquidity_level"}:
            raise ValueError(f"Unsupported StrategyZone.kind: {self.kind!r}")
        if self.direction not in {"bullish", "bearish"}:
            raise ValueError(f"Unsupported StrategyZone.direction: {self.direction!r}")
        for field_name, val in (
            ("start_time", self.start_time),
            ("end_time", self.end_time),
            ("detected_at_time", self.detected_at_time),
        ):
            if isinstance(val, bool) or not isinstance(val, int):
                raise ValueError(f"StrategyZone.{field_name} must be an int")
        if not math.isfinite(self.top) or not math.isfinite(self.bottom):
            raise ValueError("StrategyZone top/bottom must be finite")
        if self.bottom > self.top:
            raise ValueError(f"StrategyZone bottom ({self.bottom}) cannot exceed top ({self.top})")
        if self.midpoint is not None and not math.isfinite(self.midpoint):
            raise ValueError("StrategyZone midpoint must be finite")

    def dedup_key(self) -> tuple[Any, ...]:
        """Return canonical deduplication tuple preserving insertion order."""
        return (
            self.kind,
            self.direction,
            self.start_time,
            self.end_time,
            self.detected_at_time,
            self.top,
            self.bottom,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize StrategyZone to a JSON-safe dictionary."""
        return {
            "kind": self.kind,
            "direction": self.direction,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "detected_at_time": self.detected_at_time,
            "top": float(self.top),
            "bottom": float(self.bottom),
            "midpoint": float(self.midpoint) if self.midpoint is not None else None,
            "label": self.label,
        }


@dataclass(frozen=True)
class IndicatorPoint:
    """Single time-value point for a chart indicator series."""

    time: int
    value: float

    def __post_init__(self) -> None:
        if isinstance(self.time, bool) or not isinstance(self.time, int):
            raise ValueError("IndicatorPoint.time must be an int")
        if not math.isfinite(self.value):
            raise ValueError("IndicatorPoint.value must be finite")

    def to_dict(self) -> dict[str, Any]:
        """Serialize IndicatorPoint to a JSON-safe dictionary."""
        return {"time": self.time, "value": float(self.value)}


@dataclass(frozen=True)
class IndicatorSeries:
    """Indicator line series aligned to OHLCV bar timestamps."""

    id: str
    name: str
    color: str
    overlay: bool = True
    points: tuple[IndicatorPoint, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize IndicatorSeries to a JSON-safe dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "overlay": self.overlay,
            "points": [pt.to_dict() for pt in self.points],
        }


@dataclass(frozen=True)
class StrategyStepOutput:
    """Output emitted by BaseStrategy.on_bar() at bar[i] close."""

    signal: StrategySignal | None = None
    zones: tuple[StrategyZone, ...] = ()


@dataclass(frozen=True)
class StrategyContext:
    """Read-only per-bar execution context passed to BaseStrategy.on_bar()."""

    bars: CausalBarView
    bar_index: int
    instrument: InstrumentSpec
    timeframe: str
    has_open_position: bool = False
    has_pending_order: bool = False
    open_direction: Literal["LONG", "SHORT"] | None = None


class BaseStrategy(ABC):
    """Abstract plugin base class for deterministic causal Python strategies."""

    strategy_id: ClassVar[str] = ""
    strategy_version: ClassVar[str] = "1.0.0"
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = self.validate_params(params)

    @classmethod
    @abstractmethod
    def default_params(cls) -> dict[str, Any]:
        """Return default strategy parameters."""
        ...

    @classmethod
    def param_schema(cls) -> dict[str, Any]:
        """Return optional UI/API metadata describing supported parameters."""
        return {}

    @classmethod
    @abstractmethod
    def validate_params(cls, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Validate and normalize strategy parameters, raising ValueError on invalid inputs."""
        ...

    def reset(self) -> None:
        """Reset any internal causal bar-by-bar state prior to a new backtest run."""
        self.params = self.validate_params(self.params)

    @abstractmethod
    def on_bar(self, ctx: StrategyContext) -> StrategyStepOutput:
        """Evaluate bar at ctx.bar_index close using strictly ctx.bars[0..ctx.bar_index]."""
        ...

    @abstractmethod
    def compute_indicators(
        self,
        bars: Sequence[OHLCVBar],
        instrument: InstrumentSpec,
    ) -> list[IndicatorSeries]:
        """Compute chart indicator overlay series over closed bars."""
        ...


def _static_registry() -> dict[str, type[BaseStrategy]]:
    from app.engine.strategies.ict_fvg_sweep import IctFvgSweepStrategy

    return {
        IctFvgSweepStrategy.strategy_id: IctFvgSweepStrategy,
    }


def list_strategies() -> list[dict[str, Any]]:
    """Return metadata for all statically registered strategies."""
    registry = _static_registry()
    items: list[dict[str, Any]] = []
    for strategy_id in sorted(registry):
        cls = registry[strategy_id]
        items.append(
            {
                "strategy_id": cls.strategy_id,
                "strategy_version": cls.strategy_version,
                "name": cls.name,
                "description": cls.description,
                "default_params": cls.default_params(),
                "param_schema": cls.param_schema(),
            }
        )
    return items


def create_strategy(
    strategy_id: str,
    params: Mapping[str, Any] | None = None,
) -> BaseStrategy:
    """Instantiate a statically registered strategy with validated parameters."""
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise ValueError("strategy_id must be a non-empty string")
    normalized_id = strategy_id.strip()
    registry = _static_registry()
    cls = registry.get(normalized_id)
    if cls is None:
        supported = ", ".join(sorted(registry))
        raise ValueError(f"Unknown strategy_id {strategy_id!r} (supported: {supported})")
    return cls(params=params)
