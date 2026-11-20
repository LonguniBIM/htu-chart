"""Strict read-only TopstepX REST client, contract resolver, and R03 resilience controls."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings
from app.engine.ohlcv import (
    DEFAULT_MAX_FUTURE_SKEW_SEC,
    DEFAULT_MIN_BAR_TIMESTAMP,
    extract_raw_bar_timestamp,
    validate_query_timestamp,
)
from app.engine.session_calendar import (
    bucket_open_utc,
    first_bucket_open_at_or_after,
    has_open_session_in_range,
    normalize_timeframe,
    timeframe_to_topstep_unit,
)
from app.security import redact_secrets
from app.topstep.auth import CredentialRedactionScope, TopstepAuthService
from app.topstep.errors import (
    AmbiguousContractError,
    ContractNotFoundError,
    UpstreamHTTPError,
    UpstreamTimeoutError,
)

ALLOWED_READONLY_ENDPOINTS = frozenset({
    "/api/Contract/search",
    "/api/History/retrieveBars",
})


@dataclass(frozen=True)
class PaginatedBarsResult:
    """Result of bounded backward window pagination over /api/History/retrieveBars."""

    raw_bars: list[dict[str, Any]]
    pages_fetched: int
    truncated: bool
    provider_gap: bool
    earliest_bar_time: int | None
    latest_bar_time: int | None


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_contract(root_symbol: str, contracts: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve a root futures symbol strictly on verified contract metadata (F02).

    Never falls back to unrelated contracts. Matches only when:
    - ``symbolId`` ends with ``.{ROOT}`` (case-insensitive), OR
    - ``id`` contains ``.{ROOT}.`` with explicit segment boundaries.
    """
    root = root_symbol.strip().upper()
    if not root or "." in root:
        raise ContractNotFoundError(f"Invalid root symbol: {root_symbol!r}")

    symbol_suffix = f".{root}"
    id_segment = f".{root}."

    exact_matches: list[dict[str, Any]] = []
    for contract in contracts:
        sym_id = str(contract.get("symbolId") or "").strip().upper()
        contract_id = str(contract.get("id") or "").strip().upper()
        if sym_id.endswith(symbol_suffix) or id_segment in contract_id:
            exact_matches.append(contract)

    if not exact_matches:
        raise ContractNotFoundError(f"No exact contract found for root symbol '{root}'")

    active_matches = [c for c in exact_matches if c.get("activeContract") is True]
    if len(active_matches) == 1:
        return active_matches[0]
    if len(active_matches) > 1:
        raise AmbiguousContractError(
            f"Multiple active contracts matched '{root}': "
            f"{[c.get('id') for c in active_matches]}"
        )

    if len(exact_matches) == 1:
        return exact_matches[0]

    raise AmbiguousContractError(
        f"Multiple inactive contracts matched '{root}' with none marked active: "
        f"{[c.get('id') for c in exact_matches]}"
    )


class TopstepRestClient:
    """Read-only TopstepX REST client with timeouts, total deadline, and bounded retries."""

    def __init__(
        self,
        settings: Settings,
        auth_service: TopstepAuthService,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._settings = settings
        self._auth = auth_service
        self._client = client
        self._semaphore = semaphore or asyncio.Semaphore(settings.upstream_max_concurrency)

    def active_redaction_secrets(self) -> tuple[str, ...]:
        """Return active runtime TopstepX credentials for internal redaction only."""
        return self._auth.active_redaction_secrets()

    def redaction_scope(self) -> AbstractContextManager[CredentialRedactionScope]:
        """Return a request-scoped credential redaction context from TopstepAuthService."""
        return self._auth.redaction_scope()

    async def search_contracts(
        self,
        search_text: str,
        live: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Query /api/Contract/search for matching contracts."""
        target_live = self._auth.live if live is None else bool(live)
        data = await self._post_readonly(
            "/api/Contract/search",
            {"live": target_live, "searchText": search_text},
        )
        contracts = data.get("contracts")
        return list(contracts) if isinstance(contracts, list) else []

    async def resolve_symbol(
        self,
        root_symbol: str,
        live: bool | None = None,
    ) -> dict[str, Any]:
        """Search and strictly resolve a single root futures symbol."""
        contracts = await self.search_contracts(root_symbol, live=live)
        return resolve_contract(root_symbol, contracts)

    async def retrieve_bars(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Query /api/History/retrieveBars with read-only deadline and concurrency bounds."""
        return await self._post_readonly("/api/History/retrieveBars", payload)

    async def retrieve_bars_paginated(
        self,
        *,
        contract_id: str,
        live: bool,
        timeframe: str,
        start_ts: int,
        end_ts: int,
        page_limit: int | None = None,
        max_pages: int | None = None,
        include_partial_bar: bool = True,
        root_symbol: str | None = None,
    ) -> PaginatedBarsResult:
        """Paginate /api/History/retrieveBars backward until bounds are covered or capped."""
        validate_query_timestamp(start_ts, "start_ts")
        validate_query_timestamp(end_ts, "end_ts")
        if start_ts >= end_ts:
            raise ValueError(f"start_ts ({start_ts}) must be strictly less than end_ts ({end_ts})")
        tf = normalize_timeframe(timeframe)
        unit, unit_number = timeframe_to_topstep_unit(tf)
        eff_limit = page_limit if page_limit is not None else self._settings.history_page_limit
        eff_max_pages = max_pages if max_pages is not None else self._settings.history_max_pages
        if eff_limit < 1 or eff_max_pages < 1:
            raise ValueError("page_limit and max_pages must be >= 1")

        try:
            return await asyncio.wait_for(
                self._paginate_bars_loop(
                    contract_id=contract_id,
                    live=live,
                    tf=tf,
                    unit=unit,
                    unit_number=unit_number,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    eff_limit=eff_limit,
                    eff_max_pages=eff_max_pages,
                    include_partial_bar=include_partial_bar,
                    root_symbol=root_symbol,
                ),
                timeout=self._settings.history_backfill_timeout_sec,
            )
        except TimeoutError as exc:
            timeout_sec = self._settings.history_backfill_timeout_sec
            raise UpstreamTimeoutError(
                f"Paginated bar backfill for {contract_id} exceeded {timeout_sec}s deadline"
            ) from exc

    async def _paginate_bars_loop(
        self,
        *,
        contract_id: str,
        live: bool,
        tf: str,
        unit: int,
        unit_number: int,
        start_ts: int,
        end_ts: int,
        eff_limit: int,
        eff_max_pages: int,
        include_partial_bar: bool,
        root_symbol: str | None,
    ) -> PaginatedBarsResult:
        raw_bars: list[dict[str, Any]] = []
        pages_fetched = 0
        truncated = False
        provider_gap = False
        earliest_bar_time: int | None = None
        latest_bar_time: int | None = None
        seen_min_ts: int | None = None
        cursor_end = end_ts
        aligned_start = bucket_open_utc(start_ts, tf)
        first_expected_open = first_bucket_open_at_or_after(start_ts, tf)

        for page_idx in range(eff_max_pages):
            payload = {
                "contractId": contract_id,
                "live": bool(live),
                "startTime": _iso_utc(start_ts),
                "endTime": _iso_utc(cursor_end),
                "unit": unit,
                "unitNumber": unit_number,
                "limit": eff_limit,
                "includePartialBar": bool(include_partial_bar if page_idx == 0 else False),
            }
            resp = await self.retrieve_bars(payload)
            page_bars = resp.get("bars")
            if not isinstance(page_bars, list):
                raise UpstreamHTTPError(
                    "Unexpected /api/History/retrieveBars response: 'bars' must be a list",
                    status_code=200,
                )

            pages_fetched += 1
            raw_bars.extend(page_bars)

            max_page_ts = end_ts + DEFAULT_MAX_FUTURE_SKEW_SEC
            page_times = [
                ts
                for item in page_bars
                if isinstance(item, Mapping)
                and (ts := extract_raw_bar_timestamp(item)) is not None
                and DEFAULT_MIN_BAR_TIMESTAMP <= ts <= max_page_ts
            ]
            if page_times:
                page_min_ts = min(page_times)
                page_max_ts = max(page_times)
                earliest_bar_time = (
                    page_min_ts
                    if earliest_bar_time is None
                    else min(earliest_bar_time, page_min_ts)
                )
                latest_bar_time = (
                    page_max_ts
                    if latest_bar_time is None
                    else max(latest_bar_time, page_max_ts)
                )
            else:
                page_min_ts = None
                page_max_ts = None

            if len(page_bars) == 0:
                if first_expected_open <= cursor_end and has_open_session_in_range(
                    first_expected_open, cursor_end + 1, root_symbol=root_symbol
                ):
                    provider_gap = True
                break

            if page_min_ts is None or page_max_ts is None:
                provider_gap = True
                truncated = True
                break

            if (seen_min_ts is not None and page_min_ts >= seen_min_ts) or page_min_ts > cursor_end:
                provider_gap = True
                truncated = True
                break

            if page_max_ts < aligned_start:
                if has_open_session_in_range(start_ts, cursor_end, root_symbol=root_symbol):
                    provider_gap = True
                break

            if page_min_ts <= first_expected_open:
                break

            if len(page_bars) < eff_limit:
                if (
                    page_min_ts >= end_ts
                    and first_expected_open < end_ts
                    and has_open_session_in_range(
                        first_expected_open, end_ts, root_symbol=root_symbol
                    )
                ):
                    provider_gap = True
                break

            if pages_fetched >= eff_max_pages:
                truncated = True
                break

            seen_min_ts = page_min_ts if seen_min_ts is None else min(seen_min_ts, page_min_ts)
            cursor_end = page_min_ts - 1

        return PaginatedBarsResult(
            raw_bars=raw_bars,
            pages_fetched=pages_fetched,
            truncated=truncated,
            provider_gap=provider_gap,
            earliest_bar_time=earliest_bar_time,
            latest_bar_time=latest_bar_time,
        )

    async def _post_readonly(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if endpoint not in ALLOWED_READONLY_ENDPOINTS:
            raise ValueError(f"Endpoint {endpoint!r} is not on the read-only allowlist")

        url = f"{self._settings.topstepx_base_url.rstrip('/')}{endpoint}"

        try:
            return await asyncio.wait_for(
                self._run_under_deadline(url, payload),
                timeout=self._settings.upstream_total_timeout_sec,
            )
        except TimeoutError as exc:
            deadline = self._settings.upstream_total_timeout_sec
            raise UpstreamTimeoutError(
                f"Upstream request to {endpoint} exceeded {deadline}s deadline"
            ) from exc

    async def _run_under_deadline(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._auth.redaction_scope() as scope:
            token = await self._auth.ensure_token()
            scope.record(token)
            return await self._execute_with_retries(url, payload, token, scope)

    async def _execute_with_retries(
        self,
        url: str,
        payload: dict[str, Any],
        token: str,
        scope: CredentialRedactionScope,
    ) -> dict[str, Any]:
        max_attempts = self._settings.upstream_max_retries + 1
        headers = {
            "Accept": "application/json, text/plain",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                async with (
                    self._semaphore,
                    self._client.stream("POST", url, json=payload, headers=headers) as resp,
                ):
                    raw_body = await resp.aread()

                text = raw_body.decode("utf-8", errors="replace")
                if resp.status_code >= 500:
                    last_exc = UpstreamHTTPError(
                        f"Upstream HTTP {resp.status_code}",
                        status_code=resp.status_code,
                    )
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(0.05 * (2**attempt))
                        continue
                    raise last_exc

                if resp.status_code >= 400:
                    safe_detail = redact_secrets(text, scope.secrets())[:300]
                    raise UpstreamHTTPError(
                        f"Upstream client error HTTP {resp.status_code}: {safe_detail}",
                        status_code=resp.status_code,
                    )

                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError as exc:
                    raise UpstreamHTTPError(
                        f"Invalid JSON response (HTTP {resp.status_code})",
                        status_code=resp.status_code,
                    ) from exc

                if not isinstance(data, dict):
                    raise UpstreamHTTPError(
                        f"Unexpected non-object JSON response (HTTP {resp.status_code})",
                        status_code=resp.status_code,
                    )

                if data.get("success") is False:
                    err_msg = data.get("errorMessage") or (
                        f"TopstepX errorCode={data.get('errorCode')}"
                    )
                    msg = redact_secrets(str(err_msg), scope.secrets())
                    raise UpstreamHTTPError(msg, status_code=resp.status_code)

                return data
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    raise UpstreamTimeoutError("Upstream HTTP request timed out") from exc
                raise UpstreamHTTPError(
                    f"Upstream transport error: {redact_secrets(str(exc), scope.secrets())}"
                ) from exc

        if isinstance(last_exc, UpstreamHTTPError):
            raise last_exc
        raise UpstreamTimeoutError("Upstream request failed after bounded retries")
