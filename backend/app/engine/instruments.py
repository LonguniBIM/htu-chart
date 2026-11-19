"""CME futures instrument specs and collision-free storage dataset keys (F02, ARCH-001)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class InstrumentSpec:
    """Immutable CME / TopstepX futures contract specification."""

    root_symbol: str
    name: str
    exchange: str
    tick_size: float
    tick_value: float
    currency: str = "USD"


INSTRUMENTS: dict[str, InstrumentSpec] = {
    "MES": InstrumentSpec(
        root_symbol="MES",
        name="Micro E-mini S&P 500",
        exchange="CME",
        tick_size=0.25,
        tick_value=1.25,
    ),
    "MNQ": InstrumentSpec(
        root_symbol="MNQ",
        name="Micro E-mini Nasdaq-100",
        exchange="CME",
        tick_size=0.25,
        tick_value=0.50,
    ),
    "MYM": InstrumentSpec(
        root_symbol="MYM",
        name="Micro E-mini Dow",
        exchange="CBOT",
        tick_size=1.00,
        tick_value=0.50,
    ),
    "MGC": InstrumentSpec(
        root_symbol="MGC",
        name="Micro Gold",
        exchange="COMEX",
        tick_size=0.10,
        tick_value=1.00,
    ),
}


def get_instrument_spec(root_symbol: str) -> InstrumentSpec:
    """Return immutable CME specification for a supported root symbol."""
    normalized = root_symbol.strip().upper()
    if normalized not in INSTRUMENTS:
        raise ValueError(f"Unsupported root symbol: {root_symbol!r}")
    return INSTRUMENTS[normalized]


def _validate_segment(field_name: str, value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty without surrounding whitespace")
    if ".." in value or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"{field_name} contains forbidden path traversal characters: {value!r}")
    if value in {".", ".."} or not _SAFE_SEGMENT_RE.match(value):
        raise ValueError(f"{field_name} contains unsupported characters: {value!r}")
    return value


@dataclass(frozen=True)
class StorageDatasetKey:
    """Collision-free cache key separating provider, sim/live mode, contract expiry, and TF."""

    provider: str
    live_mode: bool
    contract_id: str
    timeframe: str

    def __post_init__(self) -> None:
        _validate_segment("provider", self.provider)
        _validate_segment("contract_id", self.contract_id)
        _validate_segment("timeframe", self.timeframe)

    @property
    def mode_label(self) -> str:
        """Return 'live' or 'sim' path segment."""
        return "live" if self.live_mode else "sim"

    def relative_parquet_path(self) -> Path:
        """Return relative Parquet path: {provider}/{live|sim}/{contract_id}/{timeframe}.parquet."""
        return (
            Path(self.provider)
            / self.mode_label
            / self.contract_id
            / f"{self.timeframe}.parquet"
        )

    def lock_key(self) -> tuple[str, bool, str, str]:
        """Return canonical hashable tuple for per-dataset asyncio locking."""
        return (self.provider, self.live_mode, self.contract_id, self.timeframe)
