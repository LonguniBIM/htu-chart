"""Strict read-only TopstepX REST client, contract resolver, and R03 resilience controls."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.config import Settings
from app.security import redact_secrets
from app.topstep.auth import TopstepAuthService
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
    ) -> None:
        self._settings = settings
        self._auth = auth_service
        self._client = client
        self._semaphore = asyncio.Semaphore(settings.upstream_max_concurrency)

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
        token = await self._auth.ensure_token()
        async with self._semaphore:
            return await self._execute_with_retries(url, payload, token)

    async def _execute_with_retries(
        self,
        url: str,
        payload: dict[str, Any],
        token: str,
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
                async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
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
                    safe_detail = redact_secrets(text[:300], (token,))
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

                if isinstance(data, dict) and data.get("success") is False:
                    err_msg = data.get("errorMessage") or (
                        f"TopstepX errorCode={data.get('errorCode')}"
                    )
                    msg = redact_secrets(str(err_msg), (token,))
                    raise UpstreamHTTPError(msg, status_code=resp.status_code)

                return data if isinstance(data, dict) else {"data": data}
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    raise UpstreamTimeoutError("Upstream HTTP request timed out") from exc
                raise UpstreamHTTPError(
                    f"Upstream transport error: {redact_secrets(str(exc), (token,))}"
                ) from exc

        if isinstance(last_exc, UpstreamHTTPError):
            raise last_exc
        raise UpstreamTimeoutError("Upstream request failed after bounded retries")
