"""SQLite metadata, interval coverage, and OHLCV quarantine store (F04, F05, R04, ARCH-002)."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.engine.instruments import StorageDatasetKey

CoverageStatus = Literal["COMPLETE", "CORRUPTED", "TRUNCATED", "PROVIDER_GAP"]
_VALID_COVERAGE_STATUSES = frozenset({"COMPLETE", "CORRUPTED", "TRUNCATED", "PROVIDER_GAP"})
AlertStatus = Literal["PENDING", "DELIVERED", "EXPIRED", "FAILED"]
_VALID_ALERT_STATUSES = frozenset({"PENDING", "DELIVERED", "EXPIRED", "FAILED"})
_MAX_RAW_PAYLOAD_CHARS = 2048
_MAX_ALERT_ERROR_CHARS = 512
_SQLITE_INT64_MIN = -(2**63)
_SQLITE_INT64_MAX = 2**63 - 1
_LAYOUT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
DEFAULT_LAYOUT_ID = "layout-4-future"
DEFAULT_LAYOUT_NAME = "4 Future"
DEFAULT_LAYOUT_TEMPLATE_ID = "4-grid-2x2"
DEFAULT_LAYOUT_PANES: tuple[dict[str, Any], ...] = (
    {
        "pane_id": "pane-1",
        "root_symbol": "MES",
        "timeframe": "5m",
        "live_mode": False,
        "playback_mode": "live",
    },
    {
        "pane_id": "pane-2",
        "root_symbol": "MNQ",
        "timeframe": "5m",
        "live_mode": False,
        "playback_mode": "live",
    },
    {
        "pane_id": "pane-3",
        "root_symbol": "MYM",
        "timeframe": "5m",
        "live_mode": False,
        "playback_mode": "live",
    },
    {
        "pane_id": "pane-4",
        "root_symbol": "MGC",
        "timeframe": "5m",
        "live_mode": False,
        "playback_mode": "live",
    },
)
DEFAULT_LAYOUT_SYNC_CONFIG: dict[str, bool] = {
    "symbol": False,
    "interval": True,
    "crosshair": True,
    "time": False,
    "dateRange": False,
}


def _clamp_sqlite_int64(value: int | None) -> int | None:
    if value is None:
        return None
    if _SQLITE_INT64_MIN <= value <= _SQLITE_INT64_MAX:
        return value
    return None


def _validate_sqlite_int64(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer, got {type(value).__name__}")
    if not (_SQLITE_INT64_MIN <= value <= _SQLITE_INT64_MAX):
        raise ValueError(f"{field_name} out of SQLite int64 range: {value}")
    return value


def _validate_non_empty_str(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class CoverageInterval:
    """Persisted interval coverage record for a single dataset key."""

    provider: str
    live_mode: bool
    contract_id: str
    timeframe: str
    start_ts: int
    end_ts: int
    status: CoverageStatus
    quarantined_count: int
    truncated: bool
    provider_gap: bool
    updated_at_utc: int


@dataclass(frozen=True)
class QualityIssueInput:
    """Quarantined OHLCV bar/interval issue payload to persist in SQLite."""

    bar_time: int | None
    interval_start: int | None
    interval_end: int | None
    issue_code: str
    detail: str
    raw_payload: str


@dataclass(frozen=True)
class FeedWatermark:
    """Persistent closed-bar watermark for SignalR reconnect reconciliation (F03)."""

    provider: str
    live_mode: bool
    contract_id: str
    timeframe: str
    last_closed_bar_time: int
    updated_at_utc: int


@dataclass(frozen=True)
class AlertOutboxRecord:
    """Persisted transactional alert outbox row (F07)."""

    id: int
    strategy_id: str
    strategy_version: str
    live_mode: bool
    contract_id: str
    timeframe: str
    bar_time: int
    direction: str
    alert_kind: str
    payload_text: str
    status: AlertStatus
    attempt_count: int
    max_attempts: int
    created_at_utc: int
    expires_at_utc: int
    next_attempt_at_utc: int
    delivered_at_utc: int | None
    updated_at_utc: int
    last_error: str | None


@dataclass(frozen=True)
class EnqueueAlertResult:
    """Result of transactional outbox enqueue (inserted=False on duplicate key)."""

    inserted: bool
    record: AlertOutboxRecord


@dataclass(frozen=True)
class LayoutRecord:
    """Persisted named multi-pane layout configuration."""

    id: str
    name: str
    template_id: str
    panes: list[dict[str, Any]]
    sync_config: dict[str, bool]
    is_default: bool
    created_at_utc: int
    updated_at_utc: int


def _subtract_interval(
    pieces: list[tuple[int, int]],
    cut_start: int,
    cut_end: int,
) -> list[tuple[int, int]]:
    if cut_start >= cut_end:
        return pieces
    result: list[tuple[int, int]] = []
    for p_start, p_end in pieces:
        if p_end <= cut_start or p_start >= cut_end:
            result.append((p_start, p_end))
        else:
            if p_start < cut_start:
                result.append((p_start, cut_start))
            if cut_end < p_end:
                result.append((cut_end, p_end))
    return result


def _coalesce_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    valid = sorted((s, e) for s, e in intervals if s < e)
    if not valid:
        return []
    merged: list[tuple[int, int]] = [valid[0]]
    for s, e in valid[1:]:
        prev_s, prev_e = merged[-1]
        if s <= prev_e:
            merged[-1] = (prev_s, max(prev_e, e))
        else:
            merged.append((s, e))
    return merged


class SqliteStore:
    """Thread-safe SQLite metadata, coverage, and quarantine store using WAL mode."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        """Open SQLite connection with WAL mode and create required schema tables."""
        with self._lock:
            self._ensure_conn_locked()

    def close(self) -> None:
        """Close the SQLite connection cleanly."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _ensure_conn_locked(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS datasets (
                provider TEXT NOT NULL,
                live_mode INTEGER NOT NULL,
                contract_id TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                root_symbol TEXT,
                parquet_rel_path TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                min_bar_time INTEGER,
                max_bar_time INTEGER,
                last_closed_bar_time INTEGER,
                updated_at_utc INTEGER NOT NULL,
                PRIMARY KEY (provider, live_mode, contract_id, timeframe)
            );

            CREATE TABLE IF NOT EXISTS coverage_intervals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                live_mode INTEGER NOT NULL,
                contract_id TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER NOT NULL,
                status TEXT NOT NULL,
                quarantined_count INTEGER NOT NULL DEFAULT 0,
                truncated INTEGER NOT NULL DEFAULT 0,
                provider_gap INTEGER NOT NULL DEFAULT 0,
                updated_at_utc INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_coverage_key
                ON coverage_intervals (provider, live_mode, contract_id, timeframe, start_ts);

            CREATE TABLE IF NOT EXISTS data_quality_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                live_mode INTEGER NOT NULL,
                contract_id TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                bar_time INTEGER,
                interval_start INTEGER,
                interval_end INTEGER,
                issue_code TEXT NOT NULL,
                detail TEXT NOT NULL,
                raw_payload TEXT NOT NULL,
                created_at_utc INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_quality_key
                ON data_quality_issues (
                    provider, live_mode, contract_id, timeframe, created_at_utc
                );

            CREATE TABLE IF NOT EXISTS feed_watermarks (
                provider TEXT NOT NULL,
                live_mode INTEGER NOT NULL,
                contract_id TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                last_closed_bar_time INTEGER NOT NULL,
                updated_at_utc INTEGER NOT NULL,
                PRIMARY KEY (provider, live_mode, contract_id, timeframe)
            );

            CREATE TABLE IF NOT EXISTS alert_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                live_mode INTEGER NOT NULL,
                contract_id TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                bar_time INTEGER NOT NULL,
                direction TEXT NOT NULL,
                alert_kind TEXT NOT NULL DEFAULT 'ENTRY',
                payload_text TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('PENDING', 'DELIVERED', 'EXPIRED', 'FAILED')
                ),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 4,
                created_at_utc INTEGER NOT NULL,
                expires_at_utc INTEGER NOT NULL,
                next_attempt_at_utc INTEGER NOT NULL,
                delivered_at_utc INTEGER,
                updated_at_utc INTEGER NOT NULL,
                last_error TEXT,
                UNIQUE (
                    strategy_id,
                    strategy_version,
                    live_mode,
                    contract_id,
                    timeframe,
                    bar_time,
                    direction
                )
            );

            CREATE INDEX IF NOT EXISTS idx_alert_outbox_status_next
                ON alert_outbox (status, next_attempt_at_utc, id);

            CREATE TABLE IF NOT EXISTS layouts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                template_id TEXT NOT NULL,
                panes_json TEXT NOT NULL,
                sync_config_json TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at_utc INTEGER NOT NULL,
                updated_at_utc INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_layouts_default_created
                ON layouts (is_default DESC, created_at_utc ASC, id ASC);
            """
        )
        conn.commit()
        self._conn = conn
        return conn

    @staticmethod
    def _now_utc() -> int:
        return int(datetime.now(UTC).timestamp())

    @staticmethod
    def _key_params(key: StorageDatasetKey) -> tuple[str, int, str, str]:
        return (key.provider, int(key.live_mode), key.contract_id, key.timeframe)

    def _upsert_dataset_metadata_sync(
        self,
        key: StorageDatasetKey,
        *,
        root_symbol: str | None,
        parquet_rel_path: str,
        row_count: int,
        min_bar_time: int | None,
        max_bar_time: int | None,
        last_closed_bar_time: int | None,
    ) -> None:
        now_ts = self._now_utc()
        with self._lock:
            conn = self._ensure_conn_locked()
            conn.execute(
                """
                INSERT INTO datasets (
                    provider, live_mode, contract_id, timeframe,
                    root_symbol, parquet_rel_path, row_count,
                    min_bar_time, max_bar_time, last_closed_bar_time, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, live_mode, contract_id, timeframe) DO UPDATE SET
                    root_symbol = COALESCE(excluded.root_symbol, datasets.root_symbol),
                    parquet_rel_path = excluded.parquet_rel_path,
                    row_count = excluded.row_count,
                    min_bar_time = excluded.min_bar_time,
                    max_bar_time = excluded.max_bar_time,
                    last_closed_bar_time = excluded.last_closed_bar_time,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    *self._key_params(key),
                    root_symbol,
                    parquet_rel_path,
                    row_count,
                    min_bar_time,
                    max_bar_time,
                    last_closed_bar_time,
                    now_ts,
                ),
            )
            conn.commit()

    async def upsert_dataset_metadata(
        self,
        key: StorageDatasetKey,
        *,
        root_symbol: str | None,
        parquet_rel_path: str,
        row_count: int,
        min_bar_time: int | None,
        max_bar_time: int | None,
        last_closed_bar_time: int | None,
    ) -> None:
        """Insert or update Parquet dataset summary metadata."""
        await asyncio.to_thread(
            self._upsert_dataset_metadata_sync,
            key,
            root_symbol=root_symbol,
            parquet_rel_path=parquet_rel_path,
            row_count=row_count,
            min_bar_time=min_bar_time,
            max_bar_time=max_bar_time,
            last_closed_bar_time=last_closed_bar_time,
        )

    def _get_dataset_metadata_sync(self, key: StorageDatasetKey) -> dict[str, Any] | None:
        with self._lock:
            conn = self._ensure_conn_locked()
            row = conn.execute(
                """
                SELECT provider, live_mode, contract_id, timeframe,
                       root_symbol, parquet_rel_path, row_count,
                       min_bar_time, max_bar_time, last_closed_bar_time, updated_at_utc
                FROM datasets
                WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                """,
                self._key_params(key),
            ).fetchone()
            if row is None:
                return None
            return {
                "provider": row["provider"],
                "live_mode": bool(row["live_mode"]),
                "contract_id": row["contract_id"],
                "timeframe": row["timeframe"],
                "root_symbol": row["root_symbol"],
                "parquet_rel_path": row["parquet_rel_path"],
                "row_count": int(row["row_count"]),
                "min_bar_time": row["min_bar_time"],
                "max_bar_time": row["max_bar_time"],
                "last_closed_bar_time": row["last_closed_bar_time"],
                "updated_at_utc": int(row["updated_at_utc"]),
            }

    async def get_dataset_metadata(self, key: StorageDatasetKey) -> dict[str, Any] | None:
        """Fetch dataset summary metadata for a StorageDatasetKey, or None if absent."""
        return await asyncio.to_thread(self._get_dataset_metadata_sync, key)

    @staticmethod
    def _trim_non_complete_rows_locked(
        conn: sqlite3.Connection,
        key_params: tuple[str, int, str, str],
        non_complete_rows: list[sqlite3.Row],
        cut_start: int,
        cut_end: int,
        now_ts: int,
    ) -> None:
        if cut_start >= cut_end:
            return
        for row in non_complete_rows:
            rem = _subtract_interval(
                [(int(row["start_ts"]), int(row["end_ts"]))],
                cut_start,
                cut_end,
            )
            if not rem:
                conn.execute("DELETE FROM coverage_intervals WHERE id = ?", (row["id"],))
            elif len(rem) == 1:
                if rem[0] != (int(row["start_ts"]), int(row["end_ts"])):
                    conn.execute(
                        """
                        UPDATE coverage_intervals
                        SET start_ts = ?, end_ts = ?, updated_at_utc = ?
                        WHERE id = ?
                        """,
                        (rem[0][0], rem[0][1], now_ts, row["id"]),
                    )
            else:
                conn.execute("DELETE FROM coverage_intervals WHERE id = ?", (row["id"],))
                for r_s, r_e in rem:
                    conn.execute(
                        """
                        INSERT INTO coverage_intervals (
                            provider, live_mode, contract_id, timeframe,
                            start_ts, end_ts, status, quarantined_count,
                            truncated, provider_gap, updated_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            *key_params,
                            r_s,
                            r_e,
                            row["status"],
                            int(row["quarantined_count"]),
                            int(row["truncated"]),
                            int(row["provider_gap"]),
                            now_ts,
                        ),
                    )

    def _record_coverage_sync(
        self,
        key: StorageDatasetKey,
        start_ts: int,
        end_ts: int,
        *,
        status: CoverageStatus,
        quarantined_count: int = 0,
        truncated: bool = False,
        provider_gap: bool = False,
        heal_existing: bool = False,
        skip_if_complete: bool = False,
    ) -> None:
        if start_ts > end_ts:
            raise ValueError(f"start_ts ({start_ts}) must be <= end_ts ({end_ts})")
        if status not in _VALID_COVERAGE_STATUSES:
            raise ValueError(f"Invalid coverage status: {status!r}")
        if status == "COMPLETE" and (quarantined_count > 0 or truncated or provider_gap):
            raise ValueError(
                "COMPLETE coverage cannot have quarantined_count > 0, truncated, or provider_gap"
            )

        now_ts = self._now_utc()
        key_params = self._key_params(key)

        with self._lock:
            conn = self._ensure_conn_locked()
            rows = conn.execute(
                """
                SELECT id, start_ts, end_ts, status, quarantined_count, truncated, provider_gap
                FROM coverage_intervals
                WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                ORDER BY start_ts ASC, id ASC
                """,
                key_params,
            ).fetchall()

            complete_ranges: list[tuple[int, int]] = []
            non_complete_rows: list[sqlite3.Row] = []
            for row in rows:
                if row["status"] == "COMPLETE":
                    complete_ranges.append((int(row["start_ts"]), int(row["end_ts"])))
                else:
                    non_complete_rows.append(row)

            if status == "COMPLETE":
                if heal_existing:
                    self._trim_non_complete_rows_locked(
                        conn, key_params, non_complete_rows, start_ts, end_ts, now_ts
                    )
                    candidate_pieces = [(start_ts, end_ts)] if start_ts < end_ts else []
                else:
                    candidate_pieces = [(start_ts, end_ts)] if start_ts < end_ts else []
                    for row in non_complete_rows:
                        candidate_pieces = _subtract_interval(
                            candidate_pieces,
                            int(row["start_ts"]),
                            int(row["end_ts"]),
                        )

                coalesced = _coalesce_intervals(complete_ranges + candidate_pieces)
                conn.execute(
                    """
                    DELETE FROM coverage_intervals
                    WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                      AND status = 'COMPLETE'
                    """,
                    key_params,
                )
                for c_s, c_e in coalesced:
                    conn.execute(
                        """
                        INSERT INTO coverage_intervals (
                            provider, live_mode, contract_id, timeframe,
                            start_ts, end_ts, status, quarantined_count,
                            truncated, provider_gap, updated_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, 'COMPLETE', 0, 0, 0, ?)
                        """,
                        (*key_params, c_s, c_e, now_ts),
                    )
            else:
                if skip_if_complete and start_ts < end_ts:
                    uncovered = [(start_ts, end_ts)]
                    for c_s, c_e in complete_ranges:
                        uncovered = _subtract_interval(uncovered, c_s, c_e)
                        if not uncovered:
                            return
                self._trim_non_complete_rows_locked(
                    conn, key_params, non_complete_rows, start_ts, end_ts, now_ts
                )
                trimmed_complete: list[tuple[int, int]] = []
                for c_s, c_e in complete_ranges:
                    trimmed_complete.extend(_subtract_interval([(c_s, c_e)], start_ts, end_ts))
                coalesced_complete = _coalesce_intervals(trimmed_complete)
                conn.execute(
                    """
                    DELETE FROM coverage_intervals
                    WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                      AND status = 'COMPLETE'
                    """,
                    key_params,
                )
                for c_s, c_e in coalesced_complete:
                    conn.execute(
                        """
                        INSERT INTO coverage_intervals (
                            provider, live_mode, contract_id, timeframe,
                            start_ts, end_ts, status, quarantined_count,
                            truncated, provider_gap, updated_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, 'COMPLETE', 0, 0, 0, ?)
                        """,
                        (*key_params, c_s, c_e, now_ts),
                    )
                conn.execute(
                    """
                    INSERT INTO coverage_intervals (
                        provider, live_mode, contract_id, timeframe,
                        start_ts, end_ts, status, quarantined_count,
                        truncated, provider_gap, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *key_params,
                        start_ts,
                        end_ts,
                        status,
                        int(quarantined_count),
                        int(truncated),
                        int(provider_gap),
                        now_ts,
                    ),
                )

            conn.commit()

    async def record_coverage(
        self,
        key: StorageDatasetKey,
        start_ts: int,
        end_ts: int,
        *,
        status: CoverageStatus,
        quarantined_count: int = 0,
        truncated: bool = False,
        provider_gap: bool = False,
        heal_existing: bool = False,
        skip_if_complete: bool = False,
    ) -> None:
        """Record a coverage interval while preserving non-COMPLETE integrity boundaries."""
        await asyncio.to_thread(
            self._record_coverage_sync,
            key,
            start_ts,
            end_ts,
            status=status,
            quarantined_count=quarantined_count,
            truncated=truncated,
            provider_gap=provider_gap,
            heal_existing=heal_existing,
            skip_if_complete=skip_if_complete,
        )

    def _get_coverage_intervals_sync(self, key: StorageDatasetKey) -> list[CoverageInterval]:
        with self._lock:
            conn = self._ensure_conn_locked()
            rows = conn.execute(
                """
                SELECT provider, live_mode, contract_id, timeframe,
                       start_ts, end_ts, status, quarantined_count,
                       truncated, provider_gap, updated_at_utc
                FROM coverage_intervals
                WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                ORDER BY start_ts ASC, end_ts ASC, id ASC
                """,
                self._key_params(key),
            ).fetchall()
            return [
                CoverageInterval(
                    provider=row["provider"],
                    live_mode=bool(row["live_mode"]),
                    contract_id=row["contract_id"],
                    timeframe=row["timeframe"],
                    start_ts=int(row["start_ts"]),
                    end_ts=int(row["end_ts"]),
                    status=row["status"],
                    quarantined_count=int(row["quarantined_count"]),
                    truncated=bool(row["truncated"]),
                    provider_gap=bool(row["provider_gap"]),
                    updated_at_utc=int(row["updated_at_utc"]),
                )
                for row in rows
            ]

    async def get_coverage_intervals(self, key: StorageDatasetKey) -> list[CoverageInterval]:
        """Return sorted coverage intervals for the dataset key."""
        return await asyncio.to_thread(self._get_coverage_intervals_sync, key)

    def _compute_missing_ranges_sync(
        self,
        key: StorageDatasetKey,
        start_ts: int,
        end_ts: int,
    ) -> list[tuple[int, int]]:
        if start_ts >= end_ts:
            return []
        with self._lock:
            conn = self._ensure_conn_locked()
            rows = conn.execute(
                """
                SELECT start_ts, end_ts
                FROM coverage_intervals
                WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                  AND status = 'COMPLETE'
                ORDER BY start_ts ASC, end_ts ASC
                """,
                self._key_params(key),
            ).fetchall()

        missing = [(start_ts, end_ts)]
        for row in rows:
            missing = _subtract_interval(missing, int(row["start_ts"]), int(row["end_ts"]))
            if not missing:
                break
        return missing

    async def compute_missing_ranges(
        self,
        key: StorageDatasetKey,
        start_ts: int,
        end_ts: int,
    ) -> list[tuple[int, int]]:
        """Return sorted sub-ranges of [start_ts, end_ts] not covered by COMPLETE intervals."""
        return await asyncio.to_thread(self._compute_missing_ranges_sync, key, start_ts, end_ts)

    def _is_interval_complete_sync(
        self,
        key: StorageDatasetKey,
        start_ts: int,
        end_ts: int,
    ) -> bool:
        if start_ts > end_ts:
            return False
        if start_ts < end_ts and self._compute_missing_ranges_sync(key, start_ts, end_ts):
            return False
        with self._lock:
            conn = self._ensure_conn_locked()
            if start_ts < end_ts:
                row = conn.execute(
                    """
                    SELECT 1 FROM coverage_intervals
                    WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                      AND status != 'COMPLETE'
                      AND start_ts < ? AND end_ts > ?
                    LIMIT 1
                    """,
                    (*self._key_params(key), end_ts, start_ts),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT 1 FROM coverage_intervals
                    WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                      AND status != 'COMPLETE'
                      AND start_ts < ? AND end_ts > ?
                    LIMIT 1
                    """,
                    (*self._key_params(key), start_ts, start_ts),
                ).fetchone()
        return row is None

    async def is_interval_complete(
        self,
        key: StorageDatasetKey,
        start_ts: int,
        end_ts: int,
    ) -> bool:
        """Return True iff [start_ts, end_ts] is fully COMPLETE with zero non-COMPLETE overlap."""
        return await asyncio.to_thread(self._is_interval_complete_sync, key, start_ts, end_ts)

    def _record_quality_issues_sync(
        self,
        key: StorageDatasetKey,
        issues: Sequence[QualityIssueInput],
        *,
        deduplicate: bool = False,
    ) -> int:
        if not issues:
            return 0
        now_ts = self._now_utc()
        key_params = self._key_params(key)
        inserted = 0
        with self._lock:
            conn = self._ensure_conn_locked()
            rows_to_insert: list[tuple[Any, ...]] = []
            for issue in issues:
                safe_bar_time = _clamp_sqlite_int64(issue.bar_time)
                if deduplicate:
                    existing = conn.execute(
                        """
                        SELECT 1 FROM data_quality_issues
                        WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                          AND bar_time IS ? AND issue_code = ?
                        LIMIT 1
                        """,
                        (*key_params, safe_bar_time, issue.issue_code),
                    ).fetchone()
                    if existing is not None:
                        continue
                rows_to_insert.append(
                    (
                        *key_params,
                        safe_bar_time,
                        _clamp_sqlite_int64(issue.interval_start),
                        _clamp_sqlite_int64(issue.interval_end),
                        issue.issue_code,
                        issue.detail,
                        issue.raw_payload[:_MAX_RAW_PAYLOAD_CHARS],
                        now_ts,
                    )
                )
            if rows_to_insert:
                conn.executemany(
                    """
                    INSERT INTO data_quality_issues (
                        provider, live_mode, contract_id, timeframe,
                        bar_time, interval_start, interval_end,
                        issue_code, detail, raw_payload, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
                conn.commit()
                inserted = len(rows_to_insert)
        return inserted

    async def record_quality_issues(
        self,
        key: StorageDatasetKey,
        issues: Sequence[QualityIssueInput],
        *,
        deduplicate: bool = False,
    ) -> int:
        """Persist quarantined OHLCV validation issues for auditability."""
        return await asyncio.to_thread(
            self._record_quality_issues_sync,
            key,
            issues,
            deduplicate=deduplicate,
        )

    def _list_quality_issues_sync(
        self,
        key: StorageDatasetKey | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, int(limit))
        with self._lock:
            conn = self._ensure_conn_locked()
            if key is not None:
                rows = conn.execute(
                    """
                    SELECT id, provider, live_mode, contract_id, timeframe,
                           bar_time, interval_start, interval_end,
                           issue_code, detail, raw_payload, created_at_utc
                    FROM data_quality_issues
                    WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (*self._key_params(key), safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, provider, live_mode, contract_id, timeframe,
                           bar_time, interval_start, interval_end,
                           issue_code, detail, raw_payload, created_at_utc
                    FROM data_quality_issues
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            return [
                {
                    "id": int(row["id"]),
                    "provider": row["provider"],
                    "live_mode": bool(row["live_mode"]),
                    "contract_id": row["contract_id"],
                    "timeframe": row["timeframe"],
                    "bar_time": row["bar_time"],
                    "interval_start": row["interval_start"],
                    "interval_end": row["interval_end"],
                    "issue_code": row["issue_code"],
                    "detail": row["detail"],
                    "raw_payload": row["raw_payload"],
                    "created_at_utc": int(row["created_at_utc"]),
                }
                for row in rows
            ]

    async def list_quality_issues(
        self,
        key: StorageDatasetKey | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List recent quarantined OHLCV quality issues."""
        return await asyncio.to_thread(self._list_quality_issues_sync, key, limit)

    def _get_quality_summary_sync(self) -> dict[str, Any]:
        with self._lock:
            conn = self._ensure_conn_locked()
            quarantined_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM data_quality_issues"
            ).fetchone()
            cov_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM coverage_intervals
                WHERE status IN ('CORRUPTED', 'TRUNCATED', 'PROVIDER_GAP')
                GROUP BY status
                """
            ).fetchall()
            code_rows = conn.execute(
                """
                SELECT issue_code, COUNT(*) AS cnt
                FROM data_quality_issues
                GROUP BY issue_code
                ORDER BY issue_code ASC
                """
            ).fetchall()

        status_counts = {str(row["status"]): int(row["cnt"]) for row in cov_rows}
        by_issue_code = {str(row["issue_code"]): int(row["cnt"]) for row in code_rows}
        return {
            "quarantined_bars": int(quarantined_row["cnt"]) if quarantined_row else 0,
            "corrupted_intervals": status_counts.get("CORRUPTED", 0),
            "truncated_intervals": status_counts.get("TRUNCATED", 0),
            "provider_gap_intervals": status_counts.get("PROVIDER_GAP", 0),
            "by_issue_code": by_issue_code,
        }

    async def get_quality_summary(self) -> dict[str, Any]:
        """Return aggregated data quality counters for /api/status."""
        return await asyncio.to_thread(self._get_quality_summary_sync)

    def _get_watermark_sync(self, key: StorageDatasetKey) -> FeedWatermark | None:
        with self._lock:
            conn = self._ensure_conn_locked()
            row = conn.execute(
                """
                SELECT provider, live_mode, contract_id, timeframe,
                       last_closed_bar_time, updated_at_utc
                FROM feed_watermarks
                WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                """,
                self._key_params(key),
            ).fetchone()
            if row is None:
                return None
            return FeedWatermark(
                provider=str(row["provider"]),
                live_mode=bool(row["live_mode"]),
                contract_id=str(row["contract_id"]),
                timeframe=str(row["timeframe"]),
                last_closed_bar_time=int(row["last_closed_bar_time"]),
                updated_at_utc=int(row["updated_at_utc"]),
            )

    async def get_watermark(self, key: StorageDatasetKey) -> FeedWatermark | None:
        """Return the persistent closed-bar watermark for a dataset key, or None."""
        return await asyncio.to_thread(self._get_watermark_sync, key)

    def _upsert_watermark_sync(
        self,
        key: StorageDatasetKey,
        last_closed_bar_time: int,
        *,
        force: bool = False,
    ) -> FeedWatermark:
        if isinstance(last_closed_bar_time, bool) or not isinstance(last_closed_bar_time, int):
            raise ValueError("last_closed_bar_time must be an integer Unix timestamp")
        if not (_SQLITE_INT64_MIN <= last_closed_bar_time <= _SQLITE_INT64_MAX):
            raise ValueError("last_closed_bar_time out of SQLite int64 range")

        now_ts = self._now_utc()
        key_params = self._key_params(key)
        if force:
            upsert_sql = """
                INSERT INTO feed_watermarks (
                    provider, live_mode, contract_id, timeframe,
                    last_closed_bar_time, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, live_mode, contract_id, timeframe) DO UPDATE SET
                    last_closed_bar_time = excluded.last_closed_bar_time,
                    updated_at_utc = excluded.updated_at_utc
                """
        else:
            upsert_sql = """
                INSERT INTO feed_watermarks (
                    provider, live_mode, contract_id, timeframe,
                    last_closed_bar_time, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, live_mode, contract_id, timeframe) DO UPDATE SET
                    last_closed_bar_time = MAX(
                        feed_watermarks.last_closed_bar_time,
                        excluded.last_closed_bar_time
                    ),
                    updated_at_utc = excluded.updated_at_utc
                """
        with self._lock:
            conn = self._ensure_conn_locked()
            conn.execute(upsert_sql, (*key_params, last_closed_bar_time, now_ts))
            conn.commit()
            row = conn.execute(
                """
                SELECT provider, live_mode, contract_id, timeframe,
                       last_closed_bar_time, updated_at_utc
                FROM feed_watermarks
                WHERE provider = ? AND live_mode = ? AND contract_id = ? AND timeframe = ?
                """,
                key_params,
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to read persisted feed watermark after upsert")
            return FeedWatermark(
                provider=str(row["provider"]),
                live_mode=bool(row["live_mode"]),
                contract_id=str(row["contract_id"]),
                timeframe=str(row["timeframe"]),
                last_closed_bar_time=int(row["last_closed_bar_time"]),
                updated_at_utc=int(row["updated_at_utc"]),
            )

    async def upsert_watermark(
        self,
        key: StorageDatasetKey,
        last_closed_bar_time: int,
        *,
        force: bool = False,
    ) -> FeedWatermark:
        """Persist a closed-bar watermark (advancing monotonically unless force=True)."""
        return await asyncio.to_thread(
            self._upsert_watermark_sync,
            key,
            last_closed_bar_time,
            force=force,
        )

    def _list_watermarks_sync(
        self,
        *,
        provider: str | None = None,
        live_mode: bool | None = None,
        contract_id: str | None = None,
    ) -> list[FeedWatermark]:
        norm_provider = provider.strip().lower() if provider is not None else None
        norm_live = int(bool(live_mode)) if live_mode is not None else None
        norm_contract = contract_id.strip().upper() if contract_id is not None else None

        with self._lock:
            conn = self._ensure_conn_locked()
            rows = conn.execute(
                """
                SELECT provider, live_mode, contract_id, timeframe,
                       last_closed_bar_time, updated_at_utc
                FROM feed_watermarks
                WHERE (? IS NULL OR provider = ?)
                  AND (? IS NULL OR live_mode = ?)
                  AND (? IS NULL OR contract_id = ?)
                ORDER BY provider ASC, live_mode ASC, contract_id ASC, timeframe ASC
                """,
                (
                    norm_provider,
                    norm_provider,
                    norm_live,
                    norm_live,
                    norm_contract,
                    norm_contract,
                ),
            ).fetchall()
            return [
                FeedWatermark(
                    provider=str(row["provider"]),
                    live_mode=bool(row["live_mode"]),
                    contract_id=str(row["contract_id"]),
                    timeframe=str(row["timeframe"]),
                    last_closed_bar_time=int(row["last_closed_bar_time"]),
                    updated_at_utc=int(row["updated_at_utc"]),
                )
                for row in rows
            ]

    async def list_watermarks(
        self,
        *,
        provider: str | None = None,
        live_mode: bool | None = None,
        contract_id: str | None = None,
    ) -> list[FeedWatermark]:
        """List persisted closed-bar watermarks with optional filters."""
        return await asyncio.to_thread(
            self._list_watermarks_sync,
            provider=provider,
            live_mode=live_mode,
            contract_id=contract_id,
        )

    @staticmethod
    def _row_to_alert_outbox_record(row: sqlite3.Row) -> AlertOutboxRecord:
        delivered_raw = row["delivered_at_utc"]
        return AlertOutboxRecord(
            id=int(row["id"]),
            strategy_id=str(row["strategy_id"]),
            strategy_version=str(row["strategy_version"]),
            live_mode=bool(row["live_mode"]),
            contract_id=str(row["contract_id"]),
            timeframe=str(row["timeframe"]),
            bar_time=int(row["bar_time"]),
            direction=str(row["direction"]),
            alert_kind=str(row["alert_kind"]),
            payload_text=str(row["payload_text"]),
            status=row["status"],
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            created_at_utc=int(row["created_at_utc"]),
            expires_at_utc=int(row["expires_at_utc"]),
            next_attempt_at_utc=int(row["next_attempt_at_utc"]),
            delivered_at_utc=int(delivered_raw) if delivered_raw is not None else None,
            updated_at_utc=int(row["updated_at_utc"]),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        )

    _ALERT_OUTBOX_SELECT = """
        SELECT id, strategy_id, strategy_version, live_mode, contract_id, timeframe,
               bar_time, direction, alert_kind, payload_text, status, attempt_count,
               max_attempts, created_at_utc, expires_at_utc, next_attempt_at_utc,
               delivered_at_utc, updated_at_utc, last_error
        FROM alert_outbox
    """

    def _enqueue_alert_outbox_sync(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        live_mode: bool,
        contract_id: str,
        timeframe: str,
        bar_time: int,
        direction: str,
        payload_text: str,
        created_at_utc: int,
        expires_at_utc: int,
        alert_kind: str = "ENTRY",
        max_attempts: int = 4,
        next_attempt_at_utc: int | None = None,
    ) -> EnqueueAlertResult:
        if not isinstance(live_mode, bool):
            raise ValueError(f"live_mode must be a bool, got {type(live_mode).__name__}")
        clean_strategy = _validate_non_empty_str(strategy_id, "strategy_id")
        clean_version = _validate_non_empty_str(strategy_version, "strategy_version")
        clean_contract = _validate_non_empty_str(contract_id, "contract_id").upper()
        clean_tf = _validate_non_empty_str(timeframe, "timeframe").lower()
        clean_dir = _validate_non_empty_str(direction, "direction").upper()
        clean_kind = _validate_non_empty_str(alert_kind, "alert_kind").upper()
        if not isinstance(payload_text, str) or not payload_text.strip():
            raise ValueError("payload_text must be a non-empty string")

        safe_bar_time = _validate_sqlite_int64(bar_time, "bar_time")
        safe_created = _validate_sqlite_int64(created_at_utc, "created_at_utc")
        safe_expires = _validate_sqlite_int64(expires_at_utc, "expires_at_utc")
        if safe_expires <= safe_created:
            raise ValueError("expires_at_utc must be strictly greater than created_at_utc")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be an integer >= 1")
        raw_next = safe_created if next_attempt_at_utc is None else next_attempt_at_utc
        safe_next = _validate_sqlite_int64(raw_next, "next_attempt_at_utc")

        with self._lock:
            conn = self._ensure_conn_locked()
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO alert_outbox (
                    strategy_id, strategy_version, live_mode, contract_id, timeframe,
                    bar_time, direction, alert_kind, payload_text, status,
                    attempt_count, max_attempts, created_at_utc, expires_at_utc,
                    next_attempt_at_utc, delivered_at_utc, updated_at_utc, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?, ?, NULL, ?, NULL)
                """,
                (
                    clean_strategy,
                    clean_version,
                    int(live_mode),
                    clean_contract,
                    clean_tf,
                    safe_bar_time,
                    clean_dir,
                    clean_kind,
                    payload_text,
                    max_attempts,
                    safe_created,
                    safe_expires,
                    safe_next,
                    safe_created,
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted and cursor.lastrowid is not None:
                row = conn.execute(
                    f"{self._ALERT_OUTBOX_SELECT} WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            else:
                row = conn.execute(
                    f"""
                    {self._ALERT_OUTBOX_SELECT}
                    WHERE strategy_id = ? AND strategy_version = ? AND live_mode = ?
                      AND contract_id = ? AND timeframe = ? AND bar_time = ? AND direction = ?
                    """,
                    (
                        clean_strategy,
                        clean_version,
                        int(live_mode),
                        clean_contract,
                        clean_tf,
                        safe_bar_time,
                        clean_dir,
                    ),
                ).fetchone()
            conn.commit()
            if row is None:
                raise RuntimeError("Failed to read alert_outbox row after enqueue")
            return EnqueueAlertResult(
                inserted=inserted,
                record=self._row_to_alert_outbox_record(row),
            )

    async def enqueue_alert_outbox(
        self,
        strategy_id: str,
        strategy_version: str,
        live_mode: bool,
        contract_id: str,
        timeframe: str,
        bar_time: int,
        direction: str,
        payload_text: str,
        created_at_utc: int,
        expires_at_utc: int,
        *,
        alert_kind: str = "ENTRY",
        max_attempts: int = 4,
        next_attempt_at_utc: int | None = None,
    ) -> EnqueueAlertResult:
        """Transactionally enqueue an alert into alert_outbox with UNIQUE dedup."""
        return await asyncio.to_thread(
            self._enqueue_alert_outbox_sync,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            live_mode=live_mode,
            contract_id=contract_id,
            timeframe=timeframe,
            bar_time=bar_time,
            direction=direction,
            payload_text=payload_text,
            created_at_utc=created_at_utc,
            expires_at_utc=expires_at_utc,
            alert_kind=alert_kind,
            max_attempts=max_attempts,
            next_attempt_at_utc=next_attempt_at_utc,
        )

    def _expire_stale_outbox_alerts_sync(self, now_utc: int) -> int:
        safe_now = _validate_sqlite_int64(now_utc, "now_utc")
        with self._lock:
            conn = self._ensure_conn_locked()
            cursor = conn.execute(
                """
                UPDATE alert_outbox
                SET status = 'EXPIRED', updated_at_utc = ?
                WHERE status = 'PENDING' AND expires_at_utc <= ?
                """,
                (safe_now, safe_now),
            )
            conn.commit()
            return int(cursor.rowcount)

    async def expire_stale_outbox_alerts(self, now_utc: int) -> int:
        """Atomically expire all PENDING outbox alerts whose TTL has elapsed."""
        return await asyncio.to_thread(self._expire_stale_outbox_alerts_sync, now_utc)

    def _claim_due_outbox_alerts_sync(
        self,
        now_utc: int,
        limit: int = 20,
        *,
        exclude_ids: Collection[int] = (),
    ) -> list[AlertOutboxRecord]:
        safe_now = _validate_sqlite_int64(now_utc, "now_utc")
        safe_limit = max(1, min(int(limit), 500))
        clean_excludes = sorted(
            {_validate_sqlite_int64(eid, "exclude_id") for eid in exclude_ids}
        )
        with self._lock:
            conn = self._ensure_conn_locked()
            conn.execute(
                """
                UPDATE alert_outbox
                SET status = 'EXPIRED', updated_at_utc = ?
                WHERE status = 'PENDING' AND expires_at_utc <= ?
                """,
                (safe_now, safe_now),
            )
            if clean_excludes:
                excludes_json = json.dumps(clean_excludes)
                rows = conn.execute(
                    """
                    SELECT id, strategy_id, strategy_version, live_mode, contract_id, timeframe,
                           bar_time, direction, alert_kind, payload_text, status, attempt_count,
                           max_attempts, created_at_utc, expires_at_utc, next_attempt_at_utc,
                           delivered_at_utc, updated_at_utc, last_error
                    FROM alert_outbox
                    WHERE status = 'PENDING'
                      AND expires_at_utc > ?
                      AND next_attempt_at_utc <= ?
                      AND id NOT IN (SELECT value FROM json_each(?))
                    ORDER BY next_attempt_at_utc ASC, id ASC
                    LIMIT ?
                    """,
                    (safe_now, safe_now, excludes_json, safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    {self._ALERT_OUTBOX_SELECT}
                    WHERE status = 'PENDING'
                      AND expires_at_utc > ?
                      AND next_attempt_at_utc <= ?
                    ORDER BY next_attempt_at_utc ASC, id ASC
                    LIMIT ?
                    """,
                    (safe_now, safe_now, safe_limit),
                ).fetchall()
            conn.commit()
            return [self._row_to_alert_outbox_record(row) for row in rows]

    async def claim_due_outbox_alerts(
        self,
        now_utc: int,
        limit: int = 20,
        *,
        exclude_ids: Collection[int] = (),
    ) -> list[AlertOutboxRecord]:
        """Expire stale rows and return non-expired PENDING alerts due for delivery."""
        return await asyncio.to_thread(
            self._claim_due_outbox_alerts_sync,
            now_utc,
            limit,
            exclude_ids=exclude_ids,
        )

    def _mark_outbox_delivered_sync(self, alert_id: int, now_utc: int) -> bool:
        safe_id = _validate_sqlite_int64(alert_id, "alert_id")
        safe_now = _validate_sqlite_int64(now_utc, "now_utc")
        with self._lock:
            conn = self._ensure_conn_locked()
            cursor = conn.execute(
                """
                UPDATE alert_outbox
                SET status = 'DELIVERED',
                    attempt_count = attempt_count + 1,
                    delivered_at_utc = ?,
                    updated_at_utc = ?,
                    last_error = NULL
                WHERE id = ? AND status = 'PENDING'
                """,
                (safe_now, safe_now, safe_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    async def mark_outbox_delivered(self, alert_id: int, now_utc: int) -> bool:
        """Transition a PENDING outbox alert to terminal DELIVERED state."""
        return await asyncio.to_thread(self._mark_outbox_delivered_sync, alert_id, now_utc)

    def _mark_outbox_retry_sync(
        self,
        alert_id: int,
        now_utc: int,
        next_attempt_at_utc: int,
        error: str,
    ) -> AlertStatus | None:
        safe_id = _validate_sqlite_int64(alert_id, "alert_id")
        safe_now = _validate_sqlite_int64(now_utc, "now_utc")
        safe_next = _validate_sqlite_int64(next_attempt_at_utc, "next_attempt_at_utc")
        bounded_error = str(error)[:_MAX_ALERT_ERROR_CHARS]
        with self._lock:
            conn = self._ensure_conn_locked()
            row = conn.execute(
                """
                SELECT status, attempt_count, max_attempts, expires_at_utc
                FROM alert_outbox
                WHERE id = ?
                """,
                (safe_id,),
            ).fetchone()
            if row is None or row["status"] != "PENDING":
                return None
            next_attempt_count = int(row["attempt_count"]) + 1
            expires_at_utc = int(row["expires_at_utc"])
            max_attempts = int(row["max_attempts"])
            if safe_now >= expires_at_utc:
                new_status: AlertStatus = "EXPIRED"
                effective_next = expires_at_utc
            elif next_attempt_count >= max_attempts:
                new_status = "FAILED"
                effective_next = safe_now
            else:
                new_status = "PENDING"
                effective_next = min(max(safe_next, safe_now), expires_at_utc)

            cursor = conn.execute(
                """
                UPDATE alert_outbox
                SET status = ?,
                    attempt_count = ?,
                    next_attempt_at_utc = ?,
                    updated_at_utc = ?,
                    last_error = ?
                WHERE id = ? AND status = 'PENDING'
                """,
                (new_status, next_attempt_count, effective_next, safe_now, bounded_error, safe_id),
            )
            conn.commit()
            return new_status if cursor.rowcount == 1 else None

    async def mark_outbox_retry(
        self,
        alert_id: int,
        now_utc: int,
        next_attempt_at_utc: int,
        error: str,
    ) -> AlertStatus | None:
        """Record a transient failure, respecting TTL expiry and max_attempts bounds."""
        return await asyncio.to_thread(
            self._mark_outbox_retry_sync,
            alert_id,
            now_utc,
            next_attempt_at_utc,
            error,
        )

    def _mark_outbox_failed_sync(
        self,
        alert_id: int,
        now_utc: int,
        error: str,
        *,
        increment_attempt: bool = True,
    ) -> bool:
        safe_id = _validate_sqlite_int64(alert_id, "alert_id")
        safe_now = _validate_sqlite_int64(now_utc, "now_utc")
        bounded_error = str(error)[:_MAX_ALERT_ERROR_CHARS]
        attempt_delta = 1 if increment_attempt else 0
        with self._lock:
            conn = self._ensure_conn_locked()
            cursor = conn.execute(
                """
                UPDATE alert_outbox
                SET status = 'FAILED',
                    attempt_count = attempt_count + ?,
                    updated_at_utc = ?,
                    last_error = ?
                WHERE id = ? AND status = 'PENDING'
                """,
                (attempt_delta, safe_now, bounded_error, safe_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    async def mark_outbox_failed(
        self,
        alert_id: int,
        now_utc: int,
        error: str,
        *,
        increment_attempt: bool = True,
    ) -> bool:
        """Transition a PENDING outbox alert to terminal FAILED state."""
        return await asyncio.to_thread(
            self._mark_outbox_failed_sync,
            alert_id,
            now_utc,
            error,
            increment_attempt=increment_attempt,
        )

    def _mark_outbox_expired_sync(
        self,
        alert_id: int,
        now_utc: int,
        error: str | None = None,
    ) -> bool:
        safe_id = _validate_sqlite_int64(alert_id, "alert_id")
        safe_now = _validate_sqlite_int64(now_utc, "now_utc")
        bounded_error = str(error)[:_MAX_ALERT_ERROR_CHARS] if error is not None else None
        with self._lock:
            conn = self._ensure_conn_locked()
            cursor = conn.execute(
                """
                UPDATE alert_outbox
                SET status = 'EXPIRED',
                    updated_at_utc = ?,
                    last_error = COALESCE(?, last_error)
                WHERE id = ? AND status = 'PENDING'
                """,
                (safe_now, bounded_error, safe_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    async def mark_outbox_expired(
        self,
        alert_id: int,
        now_utc: int,
        error: str | None = None,
    ) -> bool:
        """Transition a PENDING outbox alert to terminal EXPIRED state."""
        return await asyncio.to_thread(
            self._mark_outbox_expired_sync,
            alert_id,
            now_utc,
            error,
        )

    def _get_outbox_alert_sync(self, alert_id: int) -> AlertOutboxRecord | None:
        safe_id = _validate_sqlite_int64(alert_id, "alert_id")
        with self._lock:
            conn = self._ensure_conn_locked()
            row = conn.execute(
                f"{self._ALERT_OUTBOX_SELECT} WHERE id = ?",
                (safe_id,),
            ).fetchone()
            return self._row_to_alert_outbox_record(row) if row is not None else None

    async def get_outbox_alert(self, alert_id: int) -> AlertOutboxRecord | None:
        """Fetch a single outbox alert record by ID, or None if absent."""
        return await asyncio.to_thread(self._get_outbox_alert_sync, alert_id)

    def _list_outbox_alerts_sync(
        self,
        status: AlertStatus | None = None,
        limit: int = 50,
    ) -> list[AlertOutboxRecord]:
        if status is not None and status not in _VALID_ALERT_STATUSES:
            raise ValueError(f"Invalid alert status: {status!r}")
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            conn = self._ensure_conn_locked()
            if status is not None:
                rows = conn.execute(
                    f"""
                    {self._ALERT_OUTBOX_SELECT}
                    WHERE status = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (status, safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    {self._ALERT_OUTBOX_SELECT}
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            return [self._row_to_alert_outbox_record(row) for row in rows]

    async def list_outbox_alerts(
        self,
        status: AlertStatus | None = None,
        limit: int = 50,
    ) -> list[AlertOutboxRecord]:
        """List alert outbox records ordered newest-first, optionally filtered by status."""
        return await asyncio.to_thread(self._list_outbox_alerts_sync, status, limit)

    def _get_outbox_summary_sync(self) -> dict[str, int]:
        with self._lock:
            conn = self._ensure_conn_locked()
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM alert_outbox
                GROUP BY status
                """
            ).fetchall()
        counts = {str(row["status"]): int(row["cnt"]) for row in rows}
        pending = counts.get("PENDING", 0)
        delivered = counts.get("DELIVERED", 0)
        expired = counts.get("EXPIRED", 0)
        failed = counts.get("FAILED", 0)
        return {
            "pending": pending,
            "delivered": delivered,
            "expired": expired,
            "failed": failed,
            "total": pending + delivered + expired + failed,
        }

    async def get_outbox_summary(self) -> dict[str, int]:
        """Return aggregate alert_outbox row counts by status."""
        return await asyncio.to_thread(self._get_outbox_summary_sync)

    _LAYOUT_SELECT = """
        SELECT id, name, template_id, panes_json, sync_config_json,
               is_default, created_at_utc, updated_at_utc
        FROM layouts
    """

    @staticmethod
    def _validate_layout_id(layout_id: str) -> str:
        if not isinstance(layout_id, str):
            raise ValueError("layout_id must be a string")
        cleaned = layout_id.strip()
        if not cleaned or cleaned in {".", ".."} or ".." in cleaned:
            raise ValueError(f"Invalid layout_id: {layout_id!r}")
        if not _LAYOUT_ID_RE.fullmatch(cleaned):
            raise ValueError(f"Invalid layout_id characters/length: {layout_id!r}")
        return cleaned

    @staticmethod
    def _row_to_layout_record(row: sqlite3.Row) -> LayoutRecord:
        raw_panes = json.loads(str(row["panes_json"]))
        raw_sync = json.loads(str(row["sync_config_json"]))
        return LayoutRecord(
            id=str(row["id"]),
            name=str(row["name"]),
            template_id=str(row["template_id"]),
            panes=[dict(p) for p in raw_panes],
            sync_config={str(k): bool(v) for k, v in raw_sync.items()},
            is_default=bool(row["is_default"]),
            created_at_utc=int(row["created_at_utc"]),
            updated_at_utc=int(row["updated_at_utc"]),
        )

    @staticmethod
    def _begin_tx(conn: sqlite3.Connection) -> None:
        if not conn.in_transaction:
            conn.execute("BEGIN")

    def _ensure_default_layout_sync(self) -> LayoutRecord:
        now_ts = self._now_utc()
        with self._lock:
            conn = self._ensure_conn_locked()
            try:
                self._begin_tx(conn)
                row = conn.execute(
                    f"""
                    {self._LAYOUT_SELECT}
                    ORDER BY is_default DESC, created_at_utc ASC, id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    panes_json = json.dumps([dict(p) for p in DEFAULT_LAYOUT_PANES])
                    sync_json = json.dumps(dict(DEFAULT_LAYOUT_SYNC_CONFIG))
                    conn.execute(
                        """
                        INSERT INTO layouts (
                            id, name, template_id, panes_json, sync_config_json,
                            is_default, created_at_utc, updated_at_utc
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            DEFAULT_LAYOUT_ID,
                            DEFAULT_LAYOUT_NAME,
                            DEFAULT_LAYOUT_TEMPLATE_ID,
                            panes_json,
                            sync_json,
                            now_ts,
                            now_ts,
                        ),
                    )
                    row = conn.execute(
                        f"{self._LAYOUT_SELECT} WHERE id = ?",
                        (DEFAULT_LAYOUT_ID,),
                    ).fetchone()
                elif not bool(row["is_default"]):
                    conn.execute(
                        "UPDATE layouts SET is_default = 1, updated_at_utc = ? WHERE id = ?",
                        (now_ts, str(row["id"])),
                    )
                    row = conn.execute(
                        f"{self._LAYOUT_SELECT} WHERE id = ?",
                        (str(row["id"]),),
                    ).fetchone()
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            if row is None:
                raise RuntimeError("Failed to read default layout after seeding")
            return self._row_to_layout_record(row)

    async def ensure_default_layout(self) -> LayoutRecord:
        """Idempotently seed the default '4 Future' layout when layouts table is empty."""
        return await asyncio.to_thread(self._ensure_default_layout_sync)

    def _list_layouts_sync(self) -> list[LayoutRecord]:
        with self._lock:
            conn = self._ensure_conn_locked()
            rows = conn.execute(
                f"""
                {self._LAYOUT_SELECT}
                ORDER BY is_default DESC, created_at_utc ASC, id ASC
                """
            ).fetchall()
            return [self._row_to_layout_record(row) for row in rows]

    async def list_layouts(self) -> list[LayoutRecord]:
        """Return all layouts ordered by is_default DESC, created_at_utc ASC, id ASC."""
        return await asyncio.to_thread(self._list_layouts_sync)

    def _get_layout_sync(self, layout_id: str) -> LayoutRecord | None:
        if not isinstance(layout_id, str) or not layout_id.strip():
            return None
        clean_id = layout_id.strip()
        with self._lock:
            conn = self._ensure_conn_locked()
            row = conn.execute(
                f"{self._LAYOUT_SELECT} WHERE id = ?",
                (clean_id,),
            ).fetchone()
            return self._row_to_layout_record(row) if row is not None else None

    async def get_layout(self, layout_id: str) -> LayoutRecord | None:
        """Fetch a single layout by ID, or None if absent."""
        return await asyncio.to_thread(self._get_layout_sync, layout_id)

    def _create_layout_sync(
        self,
        *,
        name: str,
        template_id: str,
        panes: Sequence[Mapping[str, Any]],
        sync_config: Mapping[str, bool],
        is_default: bool = False,
        layout_id: str | None = None,
    ) -> LayoutRecord:
        clean_name = _validate_non_empty_str(name, "name")
        clean_template = _validate_non_empty_str(template_id, "template_id")
        clean_id = (
            self._validate_layout_id(layout_id)
            if layout_id is not None
            else uuid.uuid4().hex
        )
        panes_json = json.dumps([dict(p) for p in panes])
        sync_json = json.dumps({str(k): bool(v) for k, v in sync_config.items()})
        now_ts = self._now_utc()

        with self._lock:
            conn = self._ensure_conn_locked()
            try:
                self._begin_tx(conn)
                existing = conn.execute(
                    "SELECT 1 FROM layouts WHERE id = ?",
                    (clean_id,),
                ).fetchone()
                if existing is not None:
                    raise ValueError(f"Layout id {clean_id!r} already exists")
                has_default = conn.execute(
                    "SELECT 1 FROM layouts WHERE is_default = 1 LIMIT 1"
                ).fetchone()
                effective_default = bool(is_default) or (has_default is None)
                if effective_default:
                    conn.execute("UPDATE layouts SET is_default = 0 WHERE is_default != 0")
                conn.execute(
                    """
                    INSERT INTO layouts (
                        id, name, template_id, panes_json, sync_config_json,
                        is_default, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_id,
                        clean_name,
                        clean_template,
                        panes_json,
                        sync_json,
                        int(effective_default),
                        now_ts,
                        now_ts,
                    ),
                )
                row = conn.execute(
                    f"{self._LAYOUT_SELECT} WHERE id = ?",
                    (clean_id,),
                ).fetchone()
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            if row is None:
                raise RuntimeError("Failed to read created layout")
            return self._row_to_layout_record(row)

    async def create_layout(
        self,
        *,
        name: str,
        template_id: str,
        panes: Sequence[Mapping[str, Any]],
        sync_config: Mapping[str, bool],
        is_default: bool = False,
        layout_id: str | None = None,
    ) -> LayoutRecord:
        """Create and persist a named layout atomically inside a SQLite transaction."""
        return await asyncio.to_thread(
            self._create_layout_sync,
            name=name,
            template_id=template_id,
            panes=panes,
            sync_config=sync_config,
            is_default=is_default,
            layout_id=layout_id,
        )

    def _update_layout_sync(
        self,
        layout_id: str,
        *,
        name: str,
        template_id: str,
        panes: Sequence[Mapping[str, Any]],
        sync_config: Mapping[str, bool],
        is_default: bool | None = None,
    ) -> LayoutRecord | None:
        if not isinstance(layout_id, str) or not layout_id.strip():
            return None
        clean_id = layout_id.strip()
        clean_name = _validate_non_empty_str(name, "name")
        clean_template = _validate_non_empty_str(template_id, "template_id")
        panes_json = json.dumps([dict(p) for p in panes])
        sync_json = json.dumps({str(k): bool(v) for k, v in sync_config.items()})
        now_ts = self._now_utc()

        with self._lock:
            conn = self._ensure_conn_locked()
            try:
                self._begin_tx(conn)
                existing = conn.execute(
                    "SELECT is_default FROM layouts WHERE id = ?",
                    (clean_id,),
                ).fetchone()
                if existing is None:
                    conn.rollback()
                    return None
                target_default = (
                    bool(existing["is_default"]) if is_default is None else bool(is_default)
                )
                if target_default:
                    conn.execute(
                        "UPDATE layouts SET is_default = 0 WHERE id != ? AND is_default != 0",
                        (clean_id,),
                    )
                conn.execute(
                    """
                    UPDATE layouts
                    SET name = ?,
                        template_id = ?,
                        panes_json = ?,
                        sync_config_json = ?,
                        is_default = ?,
                        updated_at_utc = ?
                    WHERE id = ?
                    """,
                    (
                        clean_name,
                        clean_template,
                        panes_json,
                        sync_json,
                        int(target_default),
                        now_ts,
                        clean_id,
                    ),
                )
                if not target_default and bool(existing["is_default"]):
                    oldest_other = conn.execute(
                        """
                        SELECT id FROM layouts
                        WHERE id != ?
                        ORDER BY created_at_utc ASC, id ASC
                        LIMIT 1
                        """,
                        (clean_id,),
                    ).fetchone()
                    promote_id = (
                        str(oldest_other["id"]) if oldest_other is not None else clean_id
                    )
                    conn.execute(
                        "UPDATE layouts SET is_default = 1, updated_at_utc = ? WHERE id = ?",
                        (now_ts, promote_id),
                    )
                row = conn.execute(
                    f"{self._LAYOUT_SELECT} WHERE id = ?",
                    (clean_id,),
                ).fetchone()
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            return self._row_to_layout_record(row) if row is not None else None

    async def update_layout(
        self,
        layout_id: str,
        *,
        name: str,
        template_id: str,
        panes: Sequence[Mapping[str, Any]],
        sync_config: Mapping[str, bool],
        is_default: bool | None = None,
    ) -> LayoutRecord | None:
        """Update an existing named layout atomically, or return None if absent."""
        return await asyncio.to_thread(
            self._update_layout_sync,
            layout_id,
            name=name,
            template_id=template_id,
            panes=panes,
            sync_config=sync_config,
            is_default=is_default,
        )

    def _delete_layout_sync(self, layout_id: str) -> bool:
        if not isinstance(layout_id, str) or not layout_id.strip():
            return False
        clean_id = layout_id.strip()
        now_ts = self._now_utc()
        with self._lock:
            conn = self._ensure_conn_locked()
            try:
                self._begin_tx(conn)
                existing = conn.execute(
                    "SELECT id, is_default FROM layouts WHERE id = ?",
                    (clean_id,),
                ).fetchone()
                if existing is None:
                    conn.rollback()
                    return False
                count_row = conn.execute("SELECT COUNT(*) AS cnt FROM layouts").fetchone()
                total_count = int(count_row["cnt"]) if count_row is not None else 0
                if total_count <= 1:
                    raise ValueError("Cannot delete the last remaining layout")
                was_default = bool(existing["is_default"])
                conn.execute("DELETE FROM layouts WHERE id = ?", (clean_id,))
                if was_default:
                    oldest_row = conn.execute(
                        """
                        SELECT id FROM layouts
                        ORDER BY created_at_utc ASC, id ASC
                        LIMIT 1
                        """
                    ).fetchone()
                    if oldest_row is not None:
                        conn.execute(
                            "UPDATE layouts SET is_default = 1, updated_at_utc = ? WHERE id = ?",
                            (now_ts, str(oldest_row["id"])),
                        )
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            return True

    async def delete_layout(self, layout_id: str) -> bool:
        """Delete a layout, promoting oldest remaining if default; guard last layout."""
        return await asyncio.to_thread(self._delete_layout_sync, layout_id)
