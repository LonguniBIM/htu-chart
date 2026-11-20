"""Paginated history backfill, quarantine, and cached read service (F04, F05, R04, R05)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings
from app.engine.instruments import INSTRUMENTS, StorageDatasetKey
from app.engine.ohlcv import (
    OHLCVBar,
    extract_raw_bar_timestamp,
    parse_topstep_bar,
    validate_ohlcv_bar,
)
from app.engine.session_calendar import (
    is_bar_closed,
    latest_closed_bucket_end_utc,
    normalize_timeframe,
)
from app.security import redact_secrets
from app.storage.parquet_store import ParquetStore, validate_resample_path
from app.storage.sqlite_store import QualityIssueInput, SqliteStore
from app.topstep.errors import TopstepAuthError
from app.topstep.rest_client import PaginatedBarsResult, TopstepRestClient


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


def infer_root_symbol(contract_id: str) -> str | None:
    """Infer supported CME root symbol from a TopstepX contract ID segment."""
    upper_id = contract_id.strip().upper()
    if not upper_id:
        return None
    for root in INSTRUMENTS:
        if f".{root}." in upper_id or upper_id.endswith(f".{root}"):
            return root
    return None


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

        for raw in page_result.raw_bars:
            if not isinstance(raw, Mapping):
                val = validate_ohlcv_bar(raw, is_closed=False, now_utc=eff_now)
                issues.append(
                    QualityIssueInput(
                        bar_time=None,
                        interval_start=interval_start,
                        interval_end=interval_end,
                        issue_code=val.issue_code or "invalid_record",
                        detail=val.issue_detail or "Non-mapping OHLCV bar record",
                        raw_payload=redact_secrets(json.dumps(raw, default=str))[:2048],
                    )
                )
                continue

            parsed = parse_topstep_bar(raw)
            bar_ts = extract_raw_bar_timestamp(parsed)
            bar_closed = (
                is_bar_closed(bar_ts, source_tf, eff_now, root_symbol=eff_root)
                if bar_ts is not None
                else False
            )
            val = validate_ohlcv_bar(parsed, is_closed=bar_closed, now_utc=eff_now)
            if val.is_valid and val.bar is not None:
                valid_bars.append(val.bar)
            else:
                issues.append(
                    QualityIssueInput(
                        bar_time=bar_ts,
                        interval_start=interval_start,
                        interval_end=interval_end,
                        issue_code=val.issue_code or "invalid_ohlcv",
                        detail=val.issue_detail or "OHLCV validation failed",
                        raw_payload=redact_secrets(json.dumps(raw, default=str))[:2048],
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
        if start_ts >= end_ts:
            raise ValueError(f"start_ts ({start_ts}) must be strictly less than end_ts ({end_ts})")

        tf = normalize_timeframe(timeframe)
        source_tf = normalize_timeframe(resample_from) if resample_from else tf
        if resample_from:
            validate_resample_path(source_tf, tf)

        source_key = StorageDatasetKey("topstepx", bool(live), contract_id, source_tf)
        eff_root = (
            root_symbol.strip().upper()
            if root_symbol and root_symbol.strip()
            else infer_root_symbol(contract_id)
        )
        eff_now = now_utc if now_utc is not None else int(datetime.now(UTC).timestamp())
        closed_horizon = min(
            end_ts,
            latest_closed_bucket_end_utc(eff_now, source_tf, root_symbol=eff_root),
        )

        missing_closed = (
            await self._sqlite.compute_missing_ranges(source_key, start_ts, closed_horizon)
            if closed_horizon > start_ts
            else []
        )
        need_live_tail = bool(include_partial and end_ts > closed_horizon)

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
                include_partial_bar=bool(include_partial and r_end == end_ts),
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
                await self._sqlite.record_coverage(
                    source_key,
                    r_start,
                    r_end,
                    status="COMPLETE",
                )

        tail_truncated = False
        tail_provider_gap = False
        tail_quarantined = 0
        if need_live_tail:
            tail_start = max(start_ts, closed_horizon)
            tail_res = await self._rest.retrieve_bars_paginated(
                contract_id=contract_id,
                live=bool(live),
                timeframe=source_tf,
                start_ts=tail_start,
                end_ts=end_ts,
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
                interval_end=end_ts,
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
                    end_ts,
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

        if resample_from and source_tf != tf:
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

        if end_ts <= closed_horizon:
            is_complete = await self._sqlite.is_interval_complete(source_key, start_ts, end_ts)
        else:
            is_complete = (
                closed_horizon > start_ts
                and await self._sqlite.is_interval_complete(source_key, start_ts, closed_horizon)
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
