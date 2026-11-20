"""Paginated history backfill, quarantine, and cached read service (F04, F05, R04, R05)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings
from app.engine.instruments import StorageDatasetKey, validate_root_symbol
from app.engine.ohlcv import (
    DEFAULT_MAX_FUTURE_SKEW_SEC,
    DEFAULT_MIN_BAR_TIMESTAMP,
    MAX_QUERY_TIMESTAMP,
    OHLCVBar,
    extract_raw_bar_timestamp,
    parse_topstep_bar,
    validate_ohlcv_bar,
    validate_query_timestamp,
)
from app.engine.session_calendar import (
    bucket_bounds,
    bucket_open_utc,
    has_open_session_in_range,
    is_bar_closed,
    latest_closed_bucket_end_utc,
    normalize_timeframe,
)
from app.security import redact_secrets
from app.storage.parquet_store import ParquetStore, validate_resample_path
from app.storage.sqlite_store import QualityIssueInput, SqliteStore
from app.topstep.errors import TopstepAuthError
from app.topstep.rest_client import PaginatedBarsResult, TopstepRestClient

_SQLITE_INT64_MIN = -(2**63)
_SQLITE_INT64_MAX = 2**63 - 1


@dataclass(frozen=True)
class HistoryQueryResult:
    """Normalized bars and quality metadata returned by HistoryBackfillService."""

    key: StorageDatasetKey
    timeframe: str
    root_symbol: str | None
    bars: list[OHLCVBar]
    is_complete: bool
    truncated: bool
    provider_gap: bool
    quarantined_count: int
    upstream_requests: int


class HistoryBackfillService:
    """Coordinates coverage checks, paginated TopstepX backfill, quarantine, and Parquet reads."""

    def __init__(
        self,
        settings: Settings,
        rest_client: TopstepRestClient,
        parquet_store: ParquetStore,
        sqlite_store: SqliteStore,
    ) -> None:
        self._settings = settings
        self._rest = rest_client
        self._parquet = parquet_store
        self._sqlite = sqlite_store

    def _redact(self, text: str) -> str:
        extra = (
            self._settings.topstepx_api_key,
            self._settings.app_password_hash,
            self._settings.app_session_secret,
        )
        return redact_secrets(text, extra)

    def _validate_and_quarantine(
        self,
        page_result: PaginatedBarsResult,
        *,
        source_tf: str,
        eff_now: int,
        eff_root: str | None,
        interval_start: int,
        interval_end: int,
    ) -> tuple[list[OHLCVBar], list[QualityIssueInput]]:
        valid_bars: list[OHLCVBar] = []
        issues: list[QualityIssueInput] = []
        max_valid_ts = eff_now + DEFAULT_MAX_FUTURE_SKEW_SEC
        valid_window_start = bucket_open_utc(interval_start, source_tf)

        for raw in page_result.raw_bars:
            if not isinstance(raw, Mapping):
                val = validate_ohlcv_bar(raw, is_closed=False, now_utc=eff_now)
                issues.append(
                    QualityIssueInput(
                        bar_time=None,
                        interval_start=interval_start,
                        interval_end=interval_end,
                        issue_code=val.issue_code or "invalid_record",
                        detail=self._redact(val.issue_detail or "Non-mapping OHLCV bar record"),
                        raw_payload=self._redact(json.dumps(raw, default=str))[:2048],
                    )
                )
                continue

            parsed = parse_topstep_bar(raw)
            bar_ts = extract_raw_bar_timestamp(parsed)
            if bar_ts is not None and DEFAULT_MIN_BAR_TIMESTAMP <= bar_ts <= max_valid_ts:
                bar_closed = is_bar_closed(bar_ts, source_tf, eff_now, root_symbol=eff_root)
            else:
                bar_closed = False
            val = validate_ohlcv_bar(parsed, is_closed=bar_closed, now_utc=eff_now)
            if val.is_valid and val.bar is not None:
                if val.bar.time < valid_window_start or val.bar.time > interval_end:
                    safe_bar_time = (
                        val.bar.time
                        if _SQLITE_INT64_MIN <= val.bar.time <= _SQLITE_INT64_MAX
                        else None
                    )
                    detail = self._redact(
                        f"Bar timestamp {val.bar.time} outside "
                        f"[{valid_window_start}, {interval_end}]"
                    )
                    issues.append(
                        QualityIssueInput(
                            bar_time=safe_bar_time,
                            interval_start=interval_start,
                            interval_end=interval_end,
                            issue_code="out_of_window_timestamp",
                            detail=detail,
                            raw_payload=self._redact(json.dumps(raw, default=str))[:2048],
                        )
                    )
                else:
                    valid_bars.append(val.bar)
            else:
                safe_bar_time = (
                    bar_ts
                    if bar_ts is not None and _SQLITE_INT64_MIN <= bar_ts <= _SQLITE_INT64_MAX
                    else None
                )
                issues.append(
                    QualityIssueInput(
                        bar_time=safe_bar_time,
                        interval_start=interval_start,
                        interval_end=interval_end,
                        issue_code=val.issue_code or "invalid_ohlcv",
                        detail=self._redact(val.issue_detail or "OHLCV validation failed"),
                        raw_payload=self._redact(json.dumps(raw, default=str))[:2048],
                    )
                )

        return valid_bars, issues

    async def sync_and_read_bars(
        self,
        *,
        contract_id: str,
        live: bool,
        timeframe: str,
        start_ts: int,
        end_ts: int,
        include_partial: bool = False,
        limit: int | None = None,
        resample_from: str | None = None,
        root_symbol: str | None = None,
        now_utc: int | None = None,
        allow_upstream: bool = True,
    ) -> HistoryQueryResult:
        """Backfill missing closed intervals, preserve provisional tail, and read bars."""
        validate_query_timestamp(start_ts, "start_ts")
        validate_query_timestamp(end_ts, "end_ts")
        if start_ts >= end_ts:
            raise ValueError(f"start_ts ({start_ts}) must be strictly less than end_ts ({end_ts})")

        tf = normalize_timeframe(timeframe)
        source_tf = normalize_timeframe(resample_from) if resample_from else tf
        is_resampled = bool(resample_from and source_tf != tf)
        if resample_from:
            validate_resample_path(source_tf, tf)

        source_key = StorageDatasetKey("topstepx", bool(live), contract_id, source_tf)
        dataset_meta = await self._sqlite.get_dataset_metadata(source_key)
        stored_root = dataset_meta["root_symbol"] if dataset_meta else None
        eff_root = validate_root_symbol(
            contract_id,
            root_symbol,
            stored_root_symbol=stored_root,
        )

        fetch_start = bucket_open_utc(start_ts, tf) if is_resampled else start_ts
        fetch_end = min(
            MAX_QUERY_TIMESTAMP,
            bucket_bounds(end_ts, tf, root_symbol=eff_root)[1],
        )

        eff_now = now_utc if now_utc is not None else int(datetime.now(UTC).timestamp())
        closed_horizon = min(
            fetch_end,
            latest_closed_bucket_end_utc(eff_now, source_tf, root_symbol=eff_root),
        )

        missing_closed = (
            await self._sqlite.compute_missing_ranges(source_key, fetch_start, closed_horizon)
            if closed_horizon > fetch_start
            else []
        )
        need_live_tail = bool(include_partial and fetch_end > closed_horizon)

        if (missing_closed or need_live_tail) and not allow_upstream:
            raise TopstepAuthError("TopstepX is not connected")

        any_truncated = False
        any_provider_gap = False
        total_quarantined = 0
        upstream_requests = 0

        for r_start, r_end in missing_closed:
            page_res = await self._rest.retrieve_bars_paginated(
                contract_id=contract_id,
                live=bool(live),
                timeframe=source_tf,
                start_ts=r_start,
                end_ts=r_end,
                include_partial_bar=bool(include_partial and r_end == fetch_end),
                root_symbol=eff_root,
            )
            upstream_requests += page_res.pages_fetched
            valid_bars, issues = self._validate_and_quarantine(
                page_res,
                source_tf=source_tf,
                eff_now=eff_now,
                eff_root=eff_root,
                interval_start=r_start,
                interval_end=r_end,
            )

            await self._parquet.upsert_bars(
                source_key,
                valid_bars,
                root_symbol=eff_root,
                now_utc=eff_now,
            )
            if issues:
                await self._sqlite.record_quality_issues(source_key, issues)

            q_count = len(issues)
            total_quarantined += q_count
            if page_res.truncated:
                any_truncated = True
            if page_res.provider_gap:
                any_provider_gap = True

            if q_count > 0:
                await self._sqlite.record_coverage(
                    source_key,
                    r_start,
                    r_end,
                    status="CORRUPTED",
                    quarantined_count=q_count,
                    truncated=page_res.truncated,
                    provider_gap=page_res.provider_gap,
                )
            elif page_res.truncated:
                earliest_valid_ts = min(b.time for b in valid_bars) if valid_bars else None
                if (
                    earliest_valid_ts is not None
                    and r_start < earliest_valid_ts < r_end
                    and not page_res.provider_gap
                ):
                    await self._sqlite.record_coverage(
                        source_key,
                        r_start,
                        earliest_valid_ts,
                        status="TRUNCATED",
                        truncated=True,
                    )
                    await self._sqlite.record_coverage(
                        source_key,
                        earliest_valid_ts,
                        r_end,
                        status="COMPLETE",
                        heal_existing=True,
                    )
                else:
                    await self._sqlite.record_coverage(
                        source_key,
                        r_start,
                        r_end,
                        status="TRUNCATED",
                        truncated=True,
                        provider_gap=page_res.provider_gap,
                    )
            elif page_res.provider_gap:
                await self._sqlite.record_coverage(
                    source_key,
                    r_start,
                    r_end,
                    status="PROVIDER_GAP",
                    provider_gap=True,
                )
            else:
                window_start = bucket_open_utc(r_start, source_tf)
                has_in_range_bar = any(window_start <= b.time < r_end for b in valid_bars)
                if not has_in_range_bar and has_open_session_in_range(
                    r_start, r_end, root_symbol=eff_root
                ):
                    any_provider_gap = True
                    await self._sqlite.record_coverage(
                        source_key,
                        r_start,
                        r_end,
                        status="PROVIDER_GAP",
                        provider_gap=True,
                    )
                else:
                    await self._sqlite.record_coverage(
                        source_key,
                        r_start,
                        r_end,
                        status="COMPLETE",
                        heal_existing=True,
                    )

        tail_truncated = False
        tail_provider_gap = False
        tail_quarantined = 0
        if need_live_tail:
            tail_start = max(fetch_start, closed_horizon)
            tail_res = await self._rest.retrieve_bars_paginated(
                contract_id=contract_id,
                live=bool(live),
                timeframe=source_tf,
                start_ts=tail_start,
                end_ts=fetch_end,
                include_partial_bar=True,
                root_symbol=eff_root,
            )
            upstream_requests += tail_res.pages_fetched
            valid_tail, tail_issues = self._validate_and_quarantine(
                tail_res,
                source_tf=source_tf,
                eff_now=eff_now,
                eff_root=eff_root,
                interval_start=tail_start,
                interval_end=fetch_end,
            )
            await self._parquet.upsert_bars(
                source_key,
                valid_tail,
                root_symbol=eff_root,
                now_utc=eff_now,
            )
            if tail_issues:
                await self._sqlite.record_quality_issues(source_key, tail_issues)
                await self._sqlite.record_coverage(
                    source_key,
                    tail_start,
                    fetch_end,
                    status="CORRUPTED",
                    quarantined_count=len(tail_issues),
                    truncated=tail_res.truncated,
                    provider_gap=tail_res.provider_gap,
                )

            tail_quarantined = len(tail_issues)
            tail_truncated = tail_res.truncated
            tail_provider_gap = tail_res.provider_gap
            total_quarantined += tail_quarantined
            any_truncated = any_truncated or tail_truncated
            any_provider_gap = any_provider_gap or tail_provider_gap

        if is_resampled:
            bars = await self._parquet.resample(
                source_key,
                tf,
                start_ts=start_ts,
                end_ts=end_ts,
                include_provisional=include_partial,
                now_utc=eff_now,
                root_symbol=eff_root,
                limit=limit,
            )
        else:
            bars = await self._parquet.read_bars(
                source_key,
                start_ts=start_ts,
                end_ts=end_ts,
                include_provisional=include_partial,
                limit=limit,
            )

        if fetch_end <= closed_horizon:
            is_complete = await self._sqlite.is_interval_complete(
                source_key, fetch_start, fetch_end
            )
        else:
            is_complete = (
                closed_horizon > fetch_start
                and await self._sqlite.is_interval_complete(
                    source_key, fetch_start, closed_horizon
                )
                and not tail_truncated
                and not tail_provider_gap
                and tail_quarantined == 0
            )

        return HistoryQueryResult(
            key=source_key,
            timeframe=tf,
            root_symbol=eff_root,
            bars=bars,
            is_complete=is_complete,
            truncated=any_truncated,
            provider_gap=any_provider_gap,
            quarantined_count=total_quarantined,
            upstream_requests=upstream_requests,
        )
