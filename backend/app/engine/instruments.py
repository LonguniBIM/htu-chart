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


def infer_root_symbol(contract_id: str) -> str | None:
    """Infer supported CME root symbol from a TopstepX contract ID segment."""
    upper_id = contract_id.strip().upper()
    if not upper_id:
        return None
    for root in INSTRUMENTS:
        if f".{root}." in upper_id or upper_id.endswith(f".{root}"):
            return root
    return None


def validate_root_symbol(
    contract_id: str,
    root_symbol: str | None,
    *,
    stored_root_symbol: str | None = None,
) -> str | None:
    """Validate optional caller/stored root_symbol against contract_id and INSTRUMENTS."""
    inferred = infer_root_symbol(contract_id)
    stored = (
        stored_root_symbol.strip().upper()
        if stored_root_symbol and stored_root_symbol.strip()
        else None
    )
    if stored is not None:
        stored = get_instrument_spec(stored).root_symbol

    if root_symbol is not None and root_symbol.strip():
        candidate = get_instrument_spec(root_symbol).root_symbol
        if inferred is not None and candidate != inferred:
            raise ValueError(
                f"root_symbol {candidate!r} conflicts with contract_id {contract_id!r} "
                f"(expected {inferred!r})"
            )
        if stored is not None and candidate != stored:
            raise ValueError(
                f"root_symbol {candidate!r} conflicts with stored root_symbol {stored!r} "
                f"for contract_id {contract_id!r}"
            )
        return candidate

    if inferred is not None and stored is not None and inferred != stored:
        raise ValueError(
            f"Stored root_symbol {stored!r} conflicts with contract_id {contract_id!r} "
            f"(expected {inferred!r})"
        )
    return inferred or stored


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
        if not isinstance(self.live_mode, bool):
            raise ValueError(f"live_mode must be a bool, got {type(self.live_mode).__name__}")
        provider = _validate_segment("provider", self.provider).lower()
        contract_id = _validate_segment("contract_id", self.contract_id).upper()
        timeframe = _validate_segment("timeframe", self.timeframe).lower()
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "contract_id", contract_id)
        object.__setattr__(self, "timeframe", timeframe)

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
