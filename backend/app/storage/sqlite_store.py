"""SQLite metadata, interval coverage, and OHLCV quarantine store (F04, F05, R04, ARCH-002)."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.engine.instruments import StorageDatasetKey

CoverageStatus = Literal["COMPLETE", "CORRUPTED", "TRUNCATED", "PROVIDER_GAP"]
_VALID_COVERAGE_STATUSES = frozenset({"COMPLETE", "CORRUPTED", "TRUNCATED", "PROVIDER_GAP"})
AlertStatus = Literal["PENDING", "DELIVERED", "EXPIRED", "FAILED", "SUPPRESSED"]
_VALID_ALERT_STATUSES: tuple[AlertStatus, ...] = (
    "PENDING",
    "DELIVERED",
    "EXPIRED",
    "FAILED",
    "SUPPRESSED",
)
_VALID_ALERT_STATUS_SET = frozenset(_VALID_ALERT_STATUSES)
_MAX_RAW_PAYLOAD_CHARS = 2048
_MAX_ALERT_ERROR_CHARS = 1024
_SQLITE_INT64_MIN = -(2**63)
_SQLITE_INT64_MAX = 2**63 - 1


def _clamp_sqlite_int64(value: int | None) -> int | None:
    if value is None:
        return None
    if _SQLITE_INT64_MIN <= value <= _SQLITE_INT64_MAX:
        return value
    return None


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
    signal_tag: str | None
    status: AlertStatus
    payload_json: str
    created_at_utc: int
    expires_at_utc: int
    next_attempt_at_utc: int
    attempts: int
    last_error: str | None
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
                signal_tag TEXT,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at_utc INTEGER NOT NULL,
                expires_at_utc INTEGER NOT NULL,
                next_attempt_at_utc INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at_utc INTEGER NOT NULL,
                UNIQUE(
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
                ON alert_outbox (status, next_attempt_at_utc, expires_at_utc);
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
    def _row_to_alert_record(row: sqlite3.Row) -> AlertOutboxRecord:
        return AlertOutboxRecord(
            id=int(row["id"]),
            strategy_id=str(row["strategy_id"]),
            strategy_version=str(row["strategy_version"]),
            live_mode=bool(row["live_mode"]),
            contract_id=str(row["contract_id"]),
            timeframe=str(row["timeframe"]),
            bar_time=int(row["bar_time"]),
            direction=str(row["direction"]),
            signal_tag=str(row["signal_tag"]) if row["signal_tag"] is not None else None,
            status=row["status"],
            payload_json=str(row["payload_json"]),
            created_at_utc=int(row["created_at_utc"]),
            expires_at_utc=int(row["expires_at_utc"]),
            next_attempt_at_utc=int(row["next_attempt_at_utc"]),
            attempts=int(row["attempts"]),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
            updated_at_utc=int(row["updated_at_utc"]),
        )

    @staticmethod
    def _validate_int64(value: int, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field_name} must be an integer")
        if not (_SQLITE_INT64_MIN <= value <= _SQLITE_INT64_MAX):
            raise ValueError(f"{field_name} out of SQLite int64 range")
        return value

    def enqueue_alert_sync(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        live_mode: bool,
        contract_id: str,
        timeframe: str,
        bar_time: int,
        direction: str,
        payload_json: str,
        expires_at_utc: int,
        signal_tag: str | None = None,
        status: AlertStatus = "PENDING",
        created_at_utc: int | None = None,
        next_attempt_at_utc: int | None = None,
        last_error: str | None = None,
    ) -> tuple[AlertOutboxRecord, bool]:
        """Transactionally insert a new alert or return the existing deduplicated record."""
        norm_strategy = strategy_id.strip()
        norm_version = strategy_version.strip()
        norm_contract = contract_id.strip().upper()
        norm_tf = timeframe.strip().lower()
        norm_dir = direction.strip().upper()
        if not all((norm_strategy, norm_version, norm_contract, norm_tf, norm_dir)):
            raise ValueError("Alert outbox key fields must be non-empty")
        if status not in _VALID_ALERT_STATUS_SET:
            raise ValueError(f"Invalid alert outbox status: {status!r}")

        eff_bar_time = self._validate_int64(bar_time, "bar_time")
        eff_created = (
            self._validate_int64(created_at_utc, "created_at_utc")
            if created_at_utc is not None
            else self._now_utc()
        )
        eff_expires = self._validate_int64(expires_at_utc, "expires_at_utc")
        eff_next = (
            self._validate_int64(next_attempt_at_utc, "next_attempt_at_utc")
            if next_attempt_at_utc is not None
            else eff_created
        )
        eff_live = int(bool(live_mode))
        safe_error = last_error[:_MAX_ALERT_ERROR_CHARS] if last_error is not None else None

        with self._lock:
            conn = self._ensure_conn_locked()
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO alert_outbox (
                    strategy_id, strategy_version, live_mode, contract_id, timeframe,
                    bar_time, direction, signal_tag, status, payload_json,
                    created_at_utc, expires_at_utc, next_attempt_at_utc,
                    attempts, last_error, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    norm_strategy,
                    norm_version,
                    eff_live,
                    norm_contract,
                    norm_tf,
                    eff_bar_time,
                    norm_dir,
                    signal_tag,
                    status,
                    payload_json,
                    eff_created,
                    eff_expires,
                    eff_next,
                    safe_error,
                    eff_created,
                ),
            )
            created = cursor.rowcount == 1
            if created:
                conn.commit()
            row = conn.execute(
                """
                SELECT id, strategy_id, strategy_version, live_mode, contract_id, timeframe,
                       bar_time, direction, signal_tag, status, payload_json,
                       created_at_utc, expires_at_utc, next_attempt_at_utc,
                       attempts, last_error, updated_at_utc
                FROM alert_outbox
                WHERE strategy_id = ?
                  AND strategy_version = ?
                  AND live_mode = ?
                  AND contract_id = ?
                  AND timeframe = ?
                  AND bar_time = ?
                  AND direction = ?
                """,
                (
                    norm_strategy,
                    norm_version,
                    eff_live,
                    norm_contract,
                    norm_tf,
                    eff_bar_time,
                    norm_dir,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to read alert_outbox row after enqueue")
            return self._row_to_alert_record(row), created

    async def enqueue_alert(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        live_mode: bool,
        contract_id: str,
        timeframe: str,
        bar_time: int,
        direction: str,
        payload_json: str,
        expires_at_utc: int,
        signal_tag: str | None = None,
        status: AlertStatus = "PENDING",
        created_at_utc: int | None = None,
        next_attempt_at_utc: int | None = None,
        last_error: str | None = None,
    ) -> tuple[AlertOutboxRecord, bool]:
        """Enqueue a deduplicated alert into SQLite alert_outbox."""
        return await asyncio.to_thread(
            self.enqueue_alert_sync,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            live_mode=live_mode,
            contract_id=contract_id,
            timeframe=timeframe,
            bar_time=bar_time,
            direction=direction,
            payload_json=payload_json,
            expires_at_utc=expires_at_utc,
            signal_tag=signal_tag,
            status=status,
            created_at_utc=created_at_utc,
            next_attempt_at_utc=next_attempt_at_utc,
            last_error=last_error,
        )

    def claim_due_pending_alerts_sync(
        self,
        now_utc: int,
        limit: int = 20,
    ) -> list[AlertOutboxRecord]:
        """Expire stale PENDING alerts and return due non-expired PENDING alerts."""
        eff_now = self._validate_int64(now_utc, "now_utc")
        safe_limit = max(1, int(limit))
        with self._lock:
            conn = self._ensure_conn_locked()
            conn.execute(
                """
                UPDATE alert_outbox
                SET status = 'EXPIRED',
                    last_error = COALESCE(last_error, 'Alert TTL expired before delivery'),
                    updated_at_utc = ?
                WHERE status = 'PENDING'
                  AND expires_at_utc <= ?
                """,
                (eff_now, eff_now),
            )
            rows = conn.execute(
                """
                SELECT id, strategy_id, strategy_version, live_mode, contract_id, timeframe,
                       bar_time, direction, signal_tag, status, payload_json,
                       created_at_utc, expires_at_utc, next_attempt_at_utc,
                       attempts, last_error, updated_at_utc
                FROM alert_outbox
                WHERE status = 'PENDING'
                  AND next_attempt_at_utc <= ?
                  AND expires_at_utc > ?
                ORDER BY next_attempt_at_utc ASC, id ASC
                LIMIT ?
                """,
                (eff_now, eff_now, safe_limit),
            ).fetchall()
            conn.commit()
            return [self._row_to_alert_record(row) for row in rows]

    async def claim_due_pending_alerts(
        self,
        now_utc: int,
        limit: int = 20,
    ) -> list[AlertOutboxRecord]:
        """Expire stale PENDING alerts and return due non-expired PENDING alerts."""
        return await asyncio.to_thread(self.claim_due_pending_alerts_sync, now_utc, limit)

    def mark_alert_delivered_sync(self, alert_id: int, now_utc: int) -> None:
        """Mark an outbox alert as DELIVERED."""
        eff_now = self._validate_int64(now_utc, "now_utc")
        with self._lock:
            conn = self._ensure_conn_locked()
            conn.execute(
                """
                UPDATE alert_outbox
                SET status = 'DELIVERED',
                    last_error = NULL,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (eff_now, int(alert_id)),
            )
            conn.commit()

    async def mark_alert_delivered(self, alert_id: int, now_utc: int) -> None:
        """Mark an outbox alert as DELIVERED."""
        await asyncio.to_thread(self.mark_alert_delivered_sync, alert_id, now_utc)

    def mark_alert_retry_sync(
        self,
        alert_id: int,
        *,
        next_attempt_at_utc: int,
        last_error: str,
        now_utc: int,
    ) -> None:
        """Schedule a bounded retry for a transient failure while keeping status PENDING."""
        eff_now = self._validate_int64(now_utc, "now_utc")
        eff_next = self._validate_int64(next_attempt_at_utc, "next_attempt_at_utc")
        safe_error = last_error[:_MAX_ALERT_ERROR_CHARS]
        with self._lock:
            conn = self._ensure_conn_locked()
            conn.execute(
                """
                UPDATE alert_outbox
                SET status = 'PENDING',
                    attempts = attempts + 1,
                    next_attempt_at_utc = ?,
                    last_error = ?,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (eff_next, safe_error, eff_now, int(alert_id)),
            )
            conn.commit()

    async def mark_alert_retry(
        self,
        alert_id: int,
        *,
        next_attempt_at_utc: int,
        last_error: str,
        now_utc: int,
    ) -> None:
        """Schedule a bounded retry for a transient failure while keeping status PENDING."""
        await asyncio.to_thread(
            self.mark_alert_retry_sync,
            alert_id,
            next_attempt_at_utc=next_attempt_at_utc,
            last_error=last_error,
            now_utc=now_utc,
        )

    def mark_alert_expired_sync(
        self,
        alert_id: int,
        *,
        last_error: str | None,
        now_utc: int,
    ) -> None:
        """Mark an outbox alert as EXPIRED (staleness TTL elapsed)."""
        eff_now = self._validate_int64(now_utc, "now_utc")
        safe_error = last_error[:_MAX_ALERT_ERROR_CHARS] if last_error is not None else None
        with self._lock:
            conn = self._ensure_conn_locked()
            conn.execute(
                """
                UPDATE alert_outbox
                SET status = 'EXPIRED',
                    last_error = COALESCE(?, last_error),
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (safe_error, eff_now, int(alert_id)),
            )
            conn.commit()

    async def mark_alert_expired(
        self,
        alert_id: int,
        *,
        last_error: str | None,
        now_utc: int,
    ) -> None:
        """Mark an outbox alert as EXPIRED (staleness TTL elapsed)."""
        await asyncio.to_thread(
            self.mark_alert_expired_sync,
            alert_id,
            last_error=last_error,
            now_utc=now_utc,
        )

    def mark_alert_failed_sync(
        self,
        alert_id: int,
        *,
        last_error: str,
        now_utc: int,
    ) -> None:
        """Mark an outbox alert as permanently FAILED and increment attempts."""
        eff_now = self._validate_int64(now_utc, "now_utc")
        safe_error = last_error[:_MAX_ALERT_ERROR_CHARS]
        with self._lock:
            conn = self._ensure_conn_locked()
            conn.execute(
                """
                UPDATE alert_outbox
                SET status = 'FAILED',
                    attempts = attempts + 1,
                    last_error = ?,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (safe_error, eff_now, int(alert_id)),
            )
            conn.commit()

    async def mark_alert_failed(
        self,
        alert_id: int,
        *,
        last_error: str,
        now_utc: int,
    ) -> None:
        """Mark an outbox alert as permanently FAILED and increment attempts."""
        await asyncio.to_thread(
            self.mark_alert_failed_sync,
            alert_id,
            last_error=last_error,
            now_utc=now_utc,
        )

    def list_alerts_sync(
        self,
        limit: int = 50,
        status_filter: str | None = None,
    ) -> list[AlertOutboxRecord]:
        """List recent alert outbox records, optionally filtered by status."""
        safe_limit = max(1, int(limit))
        norm_status: str | None = None
        if status_filter is not None:
            norm_status = status_filter.strip().upper()
            if norm_status not in _VALID_ALERT_STATUS_SET:
                raise ValueError(f"Invalid alert status filter: {status_filter!r}")

        with self._lock:
            conn = self._ensure_conn_locked()
            if norm_status is not None:
                rows = conn.execute(
                    """
                    SELECT id, strategy_id, strategy_version, live_mode, contract_id, timeframe,
                           bar_time, direction, signal_tag, status, payload_json,
                           created_at_utc, expires_at_utc, next_attempt_at_utc,
                           attempts, last_error, updated_at_utc
                    FROM alert_outbox
                    WHERE status = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (norm_status, safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, strategy_id, strategy_version, live_mode, contract_id, timeframe,
                           bar_time, direction, signal_tag, status, payload_json,
                           created_at_utc, expires_at_utc, next_attempt_at_utc,
                           attempts, last_error, updated_at_utc
                    FROM alert_outbox
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            return [self._row_to_alert_record(row) for row in rows]

    async def list_alerts(
        self,
        limit: int = 50,
        status_filter: str | None = None,
    ) -> list[AlertOutboxRecord]:
        """List recent alert outbox records, optionally filtered by status."""
        return await asyncio.to_thread(self.list_alerts_sync, limit, status_filter)

    def get_alert_summary_sync(self) -> dict[str, int]:
        """Return alert outbox row counts keyed by status."""
        summary: dict[str, int] = {status_name: 0 for status_name in _VALID_ALERT_STATUSES}
        with self._lock:
            conn = self._ensure_conn_locked()
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM alert_outbox
                GROUP BY status
                """
            ).fetchall()
        for row in rows:
            key = str(row["status"])
            if key in summary:
                summary[key] = int(row["cnt"])
        return summary

    async def get_alert_summary(self) -> dict[str, int]:
        """Return alert outbox row counts keyed by status."""
        return await asyncio.to_thread(self.get_alert_summary_sync)
