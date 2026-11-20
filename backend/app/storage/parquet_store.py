"""Windows-safe locked atomic ParquetStore and CME-aligned resampling (F02, F04, F05, R05)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from app.engine.instruments import StorageDatasetKey, validate_root_symbol
from app.engine.ohlcv import OHLCVBar, ValidationResult, validate_ohlcv_bar
from app.engine.session_calendar import (
    TIMEFRAME_SECONDS,
    HolidaySpec,
    assign_bucket_open_utc,
    bucket_bounds,
    bucket_open_utc,
    is_bar_closed,
    normalize_timeframe,
)

if TYPE_CHECKING:
    from app.storage.sqlite_store import SqliteStore

_T = TypeVar("_T")

PARQUET_ARROW_SCHEMA = pa.schema(
    [
        pa.field("time", pa.int64()),
        pa.field("open", pa.float64()),
        pa.field("high", pa.float64()),
        pa.field("low", pa.float64()),
        pa.field("close", pa.float64()),
        pa.field("volume", pa.float64()),
        pa.field("is_closed", pa.bool_()),
    ]
)

_POLARS_SCHEMA: dict[str, pl.DataType] = {
    "time": pl.Int64(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "is_closed": pl.Boolean(),
}


@dataclass(frozen=True)
class UpsertResult:
    """Result summary from ParquetStore.upsert_bars()."""

    key: StorageDatasetKey
    written_rows: int
    accepted_count: int
    rejected: list[ValidationResult]
    min_bar_time: int | None
    max_bar_time: int | None
    last_closed_bar_time: int | None


def validate_resample_path(source_tf: str, target_tf: str) -> tuple[str, str]:
    """Validate and return normalized (source_tf, target_tf) for coarsening."""
    src = normalize_timeframe(source_tf)
    dst = normalize_timeframe(target_tf)
    if dst == src or dst == "1d":
        return src, dst
    if src == "1d":
        raise ValueError(f"Cannot resample from {src!r} to finer timeframe {dst!r}")
    src_sec = TIMEFRAME_SECONDS[src]
    dst_sec = TIMEFRAME_SECONDS[dst]
    if dst_sec < src_sec or dst_sec % src_sec != 0:
        raise ValueError(f"Cannot resample from {src!r} ({src_sec}s) to {dst!r} ({dst_sec}s)")
    return src, dst


def _strip_win_extended_prefix(path: Path) -> Path:
    if os.name == "nt":
        raw = str(path)
        if raw.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + raw[8:])
        if raw.startswith("\\\\?\\"):
            return Path(raw[4:])
    return path


def _atomic_replace_with_retry(src: Path, dst: Path) -> None:
    attempts = 5 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.02 * (attempt + 1))


def _verify_arrow_metadata(table: pa.Table, key: StorageDatasetKey) -> str | None:
    meta = table.schema.metadata or {}
    expected = {
        b"topstep_provider": key.provider.encode("utf-8"),
        b"topstep_live_mode": key.mode_label.encode("utf-8"),
        b"topstep_contract_id": key.contract_id.encode("utf-8"),
        b"topstep_timeframe": key.timeframe.encode("utf-8"),
    }
    for meta_key, expected_val in expected.items():
        if meta_key in meta and meta[meta_key] != expected_val:
            raise ValueError(
                f"Parquet metadata mismatch for {meta_key.decode('utf-8')}: "
                f"expected {expected_val.decode('utf-8')!r}, got {meta[meta_key]!r}"
            )
    raw_root = meta.get(b"topstep_root_symbol")
    if raw_root:
        decoded = raw_root.decode("utf-8").strip()
        return decoded or None
    return None


def _df_to_bars(df: pl.DataFrame) -> list[OHLCVBar]:
    if df.is_empty():
        return []
    return [
        OHLCVBar(
            time=int(row["time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            is_closed=bool(row["is_closed"]),
        )
        for row in df.iter_rows(named=True)
    ]


class ParquetStore:
    """Per-dataset locked, Windows-safe atomic Parquet cache and CME resampler."""

    def __init__(
        self,
        data_dir: Path,
        *,
        sqlite_store: SqliteStore | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._parquet_root = (self._data_dir / "parquet").resolve()
        self._sqlite_store = sqlite_store
        self._locks: dict[str, asyncio.Lock] = {}
        self._thread_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _path_lock_key(self, key: StorageDatasetKey) -> str:
        return os.path.normcase(str(self.resolve_path(key)))

    def _get_lock(self, key: StorageDatasetKey) -> asyncio.Lock:
        lock_key = self._path_lock_key(key)
        with self._locks_guard:
            lock = self._locks.get(lock_key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[lock_key] = lock
            return lock

    def _get_thread_lock(self, key: StorageDatasetKey) -> threading.Lock:
        lock_key = self._path_lock_key(key)
        with self._locks_guard:
            lock = self._thread_locks.get(lock_key)
            if lock is None:
                lock = threading.Lock()
                self._thread_locks[lock_key] = lock
            return lock

    async def _run_locked(
        self,
        key: StorageDatasetKey,
        work: Callable[[], Awaitable[_T]],
    ) -> _T:
        async with self._get_lock(key):
            task = asyncio.create_task(work())
            cancelled_exc: asyncio.CancelledError | None = None
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    if cancelled_exc is None:
                        cancelled_exc = exc
                except BaseException:
                    if cancelled_exc is not None:
                        break
                    raise
            if cancelled_exc is not None:
                if not task.cancelled():
                    _ = task.exception()
                raise cancelled_exc
            return task.result()

    def resolve_path(self, key: StorageDatasetKey) -> Path:
        """Resolve and path-traversal-verify the target Parquet file path."""
        root = _strip_win_extended_prefix(self._parquet_root.resolve())
        target = _strip_win_extended_prefix((root / key.relative_parquet_path()).resolve())
        if not target.is_relative_to(root):
            raise ValueError(f"Resolved Parquet path escapes root: {target}")
        return target

    def _read_table_sync(
        self, target: Path, key: StorageDatasetKey
    ) -> tuple[pl.DataFrame, str | None]:
        table = pq.read_table(target)
        existing_root = _verify_arrow_metadata(table, key)
        if table.num_rows == 0:
            return pl.DataFrame(schema=_POLARS_SCHEMA), existing_root
        df = pl.from_arrow(table)
        if not isinstance(df, pl.DataFrame):
            raise TypeError(f"Expected Polars DataFrame from Arrow table, got {type(df).__name__}")
        return df.cast(_POLARS_SCHEMA), existing_root

    def _upsert_sync(
        self,
        target: Path,
        key: StorageDatasetKey,
        valid_bars: list[OHLCVBar],
        root_symbol: str | None,
    ) -> tuple[int, int | None, int | None, int | None, str | None]:
        with self._get_thread_lock(key):
            existing_df = pl.DataFrame(schema=_POLARS_SCHEMA)
            existing_root: str | None = None
            if target.exists():
                existing_df, existing_root = self._read_table_sync(target, key)

            eff_root = validate_root_symbol(
                key.contract_id,
                root_symbol,
                stored_root_symbol=existing_root,
            )

            if not valid_bars:
                if existing_df.is_empty():
                    return 0, None, None, None, eff_root
                min_t = int(existing_df["time"].min())
                max_t = int(existing_df["time"].max())
                closed_df = existing_df.filter(pl.col("is_closed"))
                last_closed = int(closed_df["time"].max()) if not closed_df.is_empty() else None
                return existing_df.height, min_t, max_t, last_closed, eff_root

            incoming_df = pl.DataFrame(
                {
                    "time": [b.time for b in valid_bars],
                    "open": [b.open for b in valid_bars],
                    "high": [b.high for b in valid_bars],
                    "low": [b.low for b in valid_bars],
                    "close": [b.close for b in valid_bars],
                    "volume": [b.volume for b in valid_bars],
                    "is_closed": [b.is_closed for b in valid_bars],
                },
                schema=_POLARS_SCHEMA,
            ).with_columns(
                pl.col("is_closed").cast(pl.Int8).alias("_closed_rank"),
                pl.arange(1, len(valid_bars) + 1, dtype=pl.Int64).alias("_source_order"),
            )

            if existing_df.is_empty():
                combined = incoming_df
            else:
                existing_tagged = existing_df.with_columns(
                    pl.col("is_closed").cast(pl.Int8).alias("_closed_rank"),
                    pl.lit(0, dtype=pl.Int64).alias("_source_order"),
                )
                combined = pl.concat([existing_tagged, incoming_df], how="vertical")

            merged = (
                combined.sort(["time", "_closed_rank", "_source_order"])
                .unique(subset=["time"], keep="last")
                .drop(["_closed_rank", "_source_order"])
                .sort("time")
            )

            arrow_table = merged.to_arrow().cast(PARQUET_ARROW_SCHEMA)
            metadata = {
                b"topstep_provider": key.provider.encode("utf-8"),
                b"topstep_live_mode": key.mode_label.encode("utf-8"),
                b"topstep_contract_id": key.contract_id.encode("utf-8"),
                b"topstep_timeframe": key.timeframe.encode("utf-8"),
                b"topstep_root_symbol": (eff_root or "").encode("utf-8"),
            }
            arrow_table = arrow_table.replace_schema_metadata(metadata)

            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=f".{target.stem}.",
                suffix=".tmp",
                delete=False,
            )
            tmp_path = Path(tmp_file.name)
            tmp_file.close()

            try:
                pq.write_table(arrow_table, tmp_path, compression="zstd")
                _atomic_replace_with_retry(tmp_path, target)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)

            min_t = int(merged["time"].min()) if not merged.is_empty() else None
            max_t = int(merged["time"].max()) if not merged.is_empty() else None
            closed_df = merged.filter(pl.col("is_closed"))
            last_closed = int(closed_df["time"].max()) if not closed_df.is_empty() else None
            return merged.height, min_t, max_t, last_closed, eff_root

    async def upsert_bars(
        self,
        key: StorageDatasetKey,
        bars: Sequence[OHLCVBar | Mapping[str, Any]],
        *,
        root_symbol: str | None = None,
        now_utc: int | None = None,
    ) -> UpsertResult:
        """Validate, merge with provisional-bar precedence, and atomically persist bars."""
        normalize_timeframe(key.timeframe)
        validate_root_symbol(key.contract_id, root_symbol)
        target = self.resolve_path(key)

        valid_bars: list[OHLCVBar] = []
        rejected: list[ValidationResult] = []
        for item in bars:
            if isinstance(item, OHLCVBar):
                candidate: Any = {
                    "time": item.time,
                    "open": item.open,
                    "high": item.high,
                    "low": item.low,
                    "close": item.close,
                    "volume": item.volume,
                    "is_closed": item.is_closed,
                }
                res = validate_ohlcv_bar(
                    candidate,
                    is_closed=item.is_closed,
                    now_utc=now_utc,
                )
            else:
                res = validate_ohlcv_bar(item, now_utc=now_utc)
            if res.is_valid and res.bar is not None:
                valid_bars.append(res.bar)
            else:
                rejected.append(res)

        async def _work() -> tuple[int, int | None, int | None, int | None]:
            written_rows, min_t, max_t, last_closed, eff_root = await asyncio.to_thread(
                self._upsert_sync,
                target,
                key,
                valid_bars,
                root_symbol,
            )
            if self._sqlite_store is not None and (valid_bars or target.exists()):
                await self._sqlite_store.upsert_dataset_metadata(
                    key,
                    root_symbol=eff_root,
                    parquet_rel_path=key.relative_parquet_path().as_posix(),
                    row_count=written_rows,
                    min_bar_time=min_t,
                    max_bar_time=max_t,
                    last_closed_bar_time=last_closed,
                )
            return written_rows, min_t, max_t, last_closed

        written_rows, min_t, max_t, last_closed = await self._run_locked(key, _work)

        return UpsertResult(
            key=key,
            written_rows=written_rows,
            accepted_count=len(valid_bars),
            rejected=rejected,
            min_bar_time=min_t,
            max_bar_time=max_t,
            last_closed_bar_time=last_closed,
        )

    def _read_bars_sync(
        self,
        target: Path,
        key: StorageDatasetKey,
        *,
        start_ts: int | None,
        end_ts: int | None,
        include_provisional: bool,
        limit: int | None,
    ) -> list[OHLCVBar]:
        with self._get_thread_lock(key):
            if not target.exists():
                return []
            df, _ = self._read_table_sync(target, key)
            if df.is_empty():
                return []
            if not include_provisional:
                df = df.filter(pl.col("is_closed"))
            if start_ts is not None:
                df = df.filter(pl.col("time") >= start_ts)
            if end_ts is not None:
                df = df.filter(pl.col("time") <= end_ts)
            df = df.sort("time")
            if limit is not None:
                if limit <= 0:
                    return []
                df = df.tail(limit)
            return _df_to_bars(df)

    async def read_bars(
        self,
        key: StorageDatasetKey,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
        include_provisional: bool = False,
        limit: int | None = None,
    ) -> list[OHLCVBar]:
        """Read cached bars under dataset lock, excluding provisional bars by default."""
        target = self.resolve_path(key)

        async def _work() -> list[OHLCVBar]:
            return await asyncio.to_thread(
                self._read_bars_sync,
                target,
                key,
                start_ts=start_ts,
                end_ts=end_ts,
                include_provisional=include_provisional,
                limit=limit,
            )

        return await self._run_locked(key, _work)

    def _resample_sync(
        self,
        target: Path,
        source_key: StorageDatasetKey,
        target_tf: str,
        *,
        start_ts: int | None,
        end_ts: int | None,
        include_provisional: bool,
        now_utc: int,
        root_symbol: str | None,
        holiday_overrides: Mapping[date, HolidaySpec] | None,
        limit: int | None,
    ) -> list[OHLCVBar]:
        with self._get_thread_lock(source_key):
            if not target.exists():
                return []
            df, existing_root = self._read_table_sync(target, source_key)
            eff_root = validate_root_symbol(
                source_key.contract_id,
                root_symbol,
                stored_root_symbol=existing_root,
            )

            if df.is_empty():
                return []
            if start_ts is not None:
                aligned_start = bucket_open_utc(start_ts, target_tf)
                df = df.filter(pl.col("time") >= aligned_start)
            if end_ts is not None:
                _, aligned_end = bucket_bounds(
                    end_ts,
                    target_tf,
                    root_symbol=eff_root,
                    holiday_overrides=holiday_overrides,
                )
                df = df.filter(pl.col("time") < aligned_end)
            if df.is_empty():
                return []

            df = df.sort("time")
            times = [int(t) for t in df["time"].to_list()]
            bucket_opens = assign_bucket_open_utc(times, target_tf)

            grouped = (
                df.with_columns(pl.Series("bucket_open", bucket_opens, dtype=pl.Int64))
                .group_by("bucket_open", maintain_order=True)
                .agg(
                    pl.col("open").first().alias("open"),
                    pl.col("high").max().alias("high"),
                    pl.col("low").min().alias("low"),
                    pl.col("close").last().alias("close"),
                    pl.col("volume").sum().alias("volume"),
                    pl.col("is_closed").all().alias("all_inputs_closed"),
                )
                .sort("bucket_open")
            )

            out: list[OHLCVBar] = []
            for row in grouped.iter_rows(named=True):
                b_open = int(row["bucket_open"])
                _, b_end = bucket_bounds(
                    b_open,
                    target_tf,
                    root_symbol=eff_root,
                    holiday_overrides=holiday_overrides,
                )
                if start_ts is not None and b_end <= start_ts:
                    continue
                if end_ts is not None and b_open > end_ts:
                    continue
                bar_closed = bool(row["all_inputs_closed"]) and is_bar_closed(
                    b_open,
                    target_tf,
                    now_utc,
                    root_symbol=eff_root,
                    holiday_overrides=holiday_overrides,
                )
                if not include_provisional and not bar_closed:
                    continue
                out.append(
                    OHLCVBar(
                        time=b_open,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        is_closed=bar_closed,
                    )
                )
            if limit is not None:
                if limit <= 0:
                    return []
                out = out[-limit:]
            return out

    async def resample(
        self,
        source_key: StorageDatasetKey,
        target_timeframe: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
        include_provisional: bool = False,
        now_utc: int | None = None,
        root_symbol: str | None = None,
        holiday_overrides: Mapping[date, HolidaySpec] | None = None,
        limit: int | None = None,
    ) -> list[OHLCVBar]:
        """Resample cached source bars onto CME Globex 18:00 ET buckets in Polars."""
        _, target_tf = validate_resample_path(source_key.timeframe, target_timeframe)
        validate_root_symbol(source_key.contract_id, root_symbol)
        target = self.resolve_path(source_key)
        eff_now = now_utc if now_utc is not None else int(datetime.now(UTC).timestamp())

        async def _work() -> list[OHLCVBar]:
            return await asyncio.to_thread(
                self._resample_sync,
                target,
                source_key,
                target_tf,
                start_ts=start_ts,
                end_ts=end_ts,
                include_provisional=include_provisional,
                now_utc=eff_now,
                root_symbol=root_symbol,
                holiday_overrides=holiday_overrides,
                limit=limit,
            )

        return await self._run_locked(source_key, _work)
