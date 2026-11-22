"""Live SignalR market feed coordinator, reconnect recovery, and telemetry (F03, R03, R04, R05)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from app.config import Settings
from app.engine.bar_aggregator import (
    AggregatedBarEvent,
    BarAggregator,
    NormalizedTick,
)
from app.engine.instruments import StorageDatasetKey, infer_root_symbol
from app.engine.ohlcv import (
    DEFAULT_MAX_FUTURE_SKEW_SEC,
    DEFAULT_MIN_BAR_TIMESTAMP,
    OHLCVBar,
    extract_raw_bar_timestamp,
    parse_topstep_bar,
    validate_ohlcv_bar,
)
from app.engine.session_calendar import (
    TIMEFRAME_SECONDS,
    bucket_bounds,
    bucket_open_utc,
    first_bucket_open_at_or_after,
    has_open_session_in_range,
    is_bar_closed,
    latest_closed_bucket_end_utc,
    normalize_timeframe,
)
from app.security import redact_secrets
from app.services.ws_broadcaster import WsBroadcaster
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import QualityIssueInput, SqliteStore
from app.topstep.auth import TopstepAuthService
from app.topstep.signalr_hub import (
    SignalRConnectedEvent,
    SignalRDisconnectedEvent,
    SignalRErrorEvent,
    SignalREvent,
    SignalRMarketHub,
    SignalRTickEvent,
)

logger = logging.getLogger("topstep_chart.live_feed")

FeedState = Literal["DISCONNECTED", "RECOVERING", "LIVE", "DEGRADED"]
SignalCallback = Callable[[AggregatedBarEvent, str | None], Awaitable[None] | None]


class _StaleGenerationAbort(Exception):
    """Internal control-flow signal when a newer disconnect/recovery supersedes work."""


@dataclass
class ContractFeedRuntime:
    """Mutable per-contract feed status and gap counters."""

    contract_id: str
    live_mode: bool
    root_symbol: str | None
    state: FeedState = "DISCONNECTED"
    unresolved_gaps: int = 0
    recovery_duration_ms: int | None = None
    last_event_monotonic: float | None = None
    last_error: str | None = None
    watermarks: dict[str, int] = field(default_factory=dict)
    unhealed_gap_starts: dict[str, int] = field(default_factory=dict)


class LiveFeedService:
    """Coordinates SignalR Market Hub, bounded recovery queue, REST reconciliation, and status."""

    def __init__(
        self,
        settings: Settings,
        auth_service: TopstepAuthService,
        rest_client: Any,
        parquet_store: ParquetStore,
        sqlite_store: SqliteStore,
        aggregator: BarAggregator | None = None,
        broadcaster: WsBroadcaster | None = None,
        signalr_hub: SignalRMarketHub | None = None,
        *,
        recovery_timeframes: Sequence[str] | None = None,
        initial_backfill_bars: int = 10,
        degraded_retry_interval_sec: float = 15.0,
        on_bar_signal: SignalCallback | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._settings = settings
        self._auth = auth_service
        self._rest = rest_client
        self._parquet = parquet_store
        self._sqlite = sqlite_store
        self._aggregator = aggregator or BarAggregator()
        self._broadcaster = broadcaster
        raw_tfs = (
            self._aggregator.timeframes
            if recovery_timeframes is None
            else recovery_timeframes
        )
        self._recovery_timeframes = tuple(
            dict.fromkeys(normalize_timeframe(tf) for tf in raw_tfs)
        )
        if not self._recovery_timeframes:
            raise ValueError("LiveFeedService requires at least one recovery timeframe")
        self._initial_backfill_bars = max(2, int(initial_backfill_bars))
        self._degraded_retry_interval_sec = max(0.1, float(degraded_retry_interval_sec))
        self._on_bar_signal = on_bar_signal
        self._clock = clock or (lambda: int(datetime.now(UTC).timestamp()))

        self._hub: SignalRMarketHub = signalr_hub or SignalRMarketHub(
            settings,
            auth_service=auth_service,
            on_event=self.handle_hub_event,
        )
        # Ensure callback is attached if caller injected an external hub without on_event.
        if self._hub._on_event is None:
            self._hub._on_event = self.handle_hub_event

        self._contracts: dict[str, ContractFeedRuntime] = {}
        self._explicit_contracts: set[str] = set()
        self._ws_contracts: set[str] = set()
        self._feed_state: FeedState = "DISCONNECTED"
        self._last_event_monotonic: float | None = None
        self._recovery_duration_ms: int | None = None
        self._queue_overflow_count = 0
        self._global_unresolved_gaps = 0
        self._recovered_historical_count = 0
        self._last_error: str | None = None

        self._recovery_queue: asyncio.Queue[SignalRTickEvent] = asyncio.Queue(
            maxsize=settings.signalr_recovery_queue_maxsize
        )
        self._recovery_overflowed = False
        self._recovery_task: asyncio.Task[None] | None = None
        self._live_tick_tasks: set[asyncio.Task[None]] = set()
        self._watchdog_task: asyncio.Task[None] | None = None
        self._running = False
        self._generation = 0
        self._last_connected_utc: int | None = None
        self._last_degraded_at_mono: float | None = None

    @property
    def hub(self) -> SignalRMarketHub:
        """Underlying SignalRMarketHub transport instance."""
        return self._hub

    @property
    def feed_state(self) -> FeedState:
        """Current aggregate live feed state."""
        return self._feed_state

    @property
    def live_alerts_enabled(self) -> bool:
        """True iff aggregate feed is LIVE (suppressed during DISCONNECTED/RECOVERING/DEGRADED)."""
        return self._feed_state == "LIVE"

    @property
    def queue_overflow_count(self) -> int:
        """Number of recovery ticks rejected due to bounded queue overflow."""
        return self._queue_overflow_count

    @property
    def recovered_historical_count(self) -> int:
        """Number of bars tagged RECOVERED_HISTORICAL during recovery reconciliation."""
        return self._recovered_historical_count

    @property
    def active_contracts(self) -> tuple[str, ...]:
        """Ordered list of currently tracked contract IDs."""
        return tuple(self._contracts.keys())

    def is_contract_live_alerts_enabled(self, contract_id: str) -> bool:
        """Return True iff the specific contract is in LIVE state."""
        c_state = self._contracts.get(contract_id.strip().upper())
        return c_state is not None and c_state.state == "LIVE"

    def _redact(self, text: str) -> str:
        secrets = (
            *self._auth.active_redaction_secrets(),
            self._settings.topstepx_api_key,
            self._settings.app_password_hash,
            self._settings.app_session_secret,
        )
        return redact_secrets(text, secrets)

    def _recompute_aggregate_state(self) -> FeedState:
        if not self._contracts:
            return self._feed_state
        states = {c.state for c in self._contracts.values()}
        if "RECOVERING" in states:
            self._feed_state = "RECOVERING"
        elif "DISCONNECTED" in states and not self._hub.is_connected:
            self._feed_state = "DISCONNECTED"
        elif "DEGRADED" in states:
            self._feed_state = "DEGRADED"
        elif "DISCONNECTED" in states:
            self._feed_state = "DISCONNECTED"
        else:
            self._feed_state = "LIVE"
        return self._feed_state

    def _broadcast_status(self) -> None:
        if self._broadcaster is not None:
            self._broadcaster.publish_feed_status(self.get_status_snapshot())

    def _transition_all_contracts(self, new_state: FeedState) -> None:
        for c in self._contracts.values():
            c.state = new_state
        self._feed_state = new_state
        if new_state == "DEGRADED":
            self._last_degraded_at_mono = time.monotonic()
        self._broadcast_status()

    async def set_active_contracts(
        self,
        contracts: Sequence[str | tuple[str, str | None]],
        *,
        live_mode: bool | None = None,
    ) -> None:
        """Register active contracts, reset removed contracts on rollover, and sync SignalR."""
        eff_live = self._auth.live if live_mode is None else bool(live_mode)
        normalized: dict[str, str | None] = {}
        for item in contracts:
            if isinstance(item, tuple):
                cid = item[0].strip().upper()
                root = item[1].strip().upper() if item[1] else infer_root_symbol(cid)
            else:
                cid = str(item).strip().upper()
                root = infer_root_symbol(cid)
            if cid:
                normalized[cid] = root

        removed = (set(self._contracts) - set(normalized)) - self._ws_contracts
        self._explicit_contracts = set(normalized)
        for old_cid in removed:
            self._contracts.pop(old_cid, None)
            self._aggregator.reset_contract(old_cid)

        added_or_mode_changed = False
        for cid, root in normalized.items():
            existing = self._contracts.get(cid)
            if existing is None or existing.live_mode != eff_live:
                if existing is not None:
                    self._aggregator.reset_contract(cid)
                self._contracts[cid] = ContractFeedRuntime(
                    contract_id=cid,
                    live_mode=eff_live,
                    root_symbol=root,
                    state="DISCONNECTED",
                )
                added_or_mode_changed = True
            elif root is not None:
                existing.root_symbol = root

        await self._hub.update_contracts(list(self._contracts.keys()), live_mode=eff_live)

        if (
            self._running
            and self._settings.signalr_enabled
            and self._auth.is_connected
            and self._contracts
        ):
            if not self._hub.is_connected:
                self._hub.start()
            elif added_or_mode_changed or removed:
                await self.trigger_recovery()
        self._broadcast_status()

    async def subscribe_ws_contract(
        self,
        contract_id: str,
        *,
        live_mode: bool | None = None,
    ) -> None:
        """Register a contract subscribed via /ws/market and start/update upstream SignalR."""
        cid = contract_id.strip().upper()
        if not cid:
            raise ValueError("contract_id cannot be empty")
        eff_live = self._auth.live if live_mode is None else bool(live_mode)
        self._ws_contracts.add(cid)
        existing = self._contracts.get(cid)
        newly_added = existing is None or existing.live_mode != eff_live
        self.register_contract(cid, live_mode=eff_live)
        await self._hub.update_contracts(list(self._contracts.keys()), live_mode=eff_live)

        if self._running and self._settings.signalr_enabled and self._auth.is_connected:
            if not self._hub.is_connected:
                self._hub.start()
            elif newly_added:
                self._bump_generation_and_cancel_live_tasks()
                self._transition_all_contracts("RECOVERING")
                self._start_recovery_task()
        self._broadcast_status()

    async def sync_ws_contracts(self, active_ws_contracts: set[str]) -> None:
        """Prune WS-only contracts when all WebSocket subscribers leave."""
        norm_ws = {cid.strip().upper() for cid in active_ws_contracts if cid.strip()}
        self._ws_contracts = norm_ws
        keep = self._explicit_contracts | self._ws_contracts
        removed = [cid for cid in list(self._contracts) if cid not in keep]
        if not removed:
            return
        for old_cid in removed:
            self._contracts.pop(old_cid, None)
            self._aggregator.reset_contract(old_cid)
        await self._hub.update_contracts(list(self._contracts.keys()), live_mode=self._auth.live)
        if not self._contracts and self._hub.is_connected:
            await self._hub.stop()
            self._feed_state = "DISCONNECTED"
        self._broadcast_status()

    def register_contract(
        self,
        contract_id: str,
        *,
        live_mode: bool | None = None,
        root_symbol: str | None = None,
    ) -> ContractFeedRuntime:
        """Synchronously ensure a contract is tracked (e.g. from /ws/market subscribe)."""
        cid = contract_id.strip().upper()
        if not cid:
            raise ValueError("contract_id cannot be empty")
        eff_live = self._auth.live if live_mode is None else bool(live_mode)
        existing = self._contracts.get(cid)
        if existing is None or existing.live_mode != eff_live:
            if existing is not None:
                self._aggregator.reset_contract(cid)
            eff_root = root_symbol.strip().upper() if root_symbol else infer_root_symbol(cid)
            existing = ContractFeedRuntime(
                contract_id=cid,
                live_mode=eff_live,
                root_symbol=eff_root,
                state="DISCONNECTED",
            )
            self._contracts[cid] = existing
        return existing

    async def start(self) -> None:
        """Start background watchdog and SignalR connection (if enabled & auth+contracts ready)."""
        if self._running:
            return
        self._running = True
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(),
                name="live-feed-watchdog",
            )
        if (
            self._settings.signalr_enabled
            and self._auth.is_connected
            and self._contracts
        ):
            self._hub.start()

    async def on_auth_connected(self) -> None:
        """Sync live_mode and start/reconnect SignalR when TopstepX auth acquires a JWT."""
        if not self._running:
            await self.start()
            return

        new_live = self._auth.live
        mode_changed = any(c.live_mode != new_live for c in self._contracts.values()) or (
            self._hub.live_mode != new_live
        )

        if mode_changed:
            self._bump_generation_and_cancel_live_tasks()
            if self._recovery_task is not None and not self._recovery_task.done():
                self._recovery_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._recovery_task
            self._recovery_task = None
            for task in list(self._live_tick_tasks):
                if not task.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            self._live_tick_tasks.clear()
            for cid, c_state in list(self._contracts.items()):
                self._aggregator.reset_contract(cid)
                self._contracts[cid] = ContractFeedRuntime(
                    contract_id=cid,
                    live_mode=new_live,
                    root_symbol=c_state.root_symbol,
                    state="DISCONNECTED",
                )
            self._last_connected_utc = None

        if not self._settings.signalr_enabled or not self._auth.is_connected or not self._contracts:
            if mode_changed:
                await self._hub.update_contracts(list(self._contracts.keys()), live_mode=new_live)
                self._broadcast_status()
            return

        if not self._hub.is_connected:
            await self._hub.update_contracts(list(self._contracts.keys()), live_mode=new_live)
            self._hub.start()
        else:
            if not mode_changed:
                self._bump_generation_and_cancel_live_tasks()
            self._transition_all_contracts("RECOVERING")
            if mode_changed:
                await self._hub.update_contracts(list(self._contracts.keys()), live_mode=new_live)
            else:
                await self._hub.trigger_reconnect()

    async def on_auth_disconnected(self) -> None:
        """Stop active SignalR and recovery tasks when TopstepX auth disconnects."""
        await self.stop()

    async def stop(self) -> None:
        """Stop SignalR, recovery tasks, and watchdog cleanly on shutdown or disconnect."""
        self._running = False
        self._bump_generation_and_cancel_live_tasks()
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
        self._watchdog_task = None

        if self._recovery_task is not None and not self._recovery_task.done():
            self._recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recovery_task
        self._recovery_task = None

        for task in list(self._live_tick_tasks):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._live_tick_tasks.clear()

        await self._hub.stop()
        self._last_connected_utc = None
        self._transition_all_contracts("DISCONNECTED")

    def handle_hub_event(self, event: SignalREvent) -> None:
        """Handle lifecycle and tick events from SignalRMarketHub without blocking recv."""
        if isinstance(event, SignalRConnectedEvent):
            self._last_event_monotonic = event.connected_monotonic
            self._last_connected_utc = self._clock()
            self._last_error = None
            for cid in event.contracts:
                self.register_contract(cid, live_mode=event.live_mode)
            self._bump_generation_and_cancel_live_tasks()
            self._reset_recovery_queue()
            self._transition_all_contracts("RECOVERING")
            self._start_recovery_task()
            return

        if isinstance(event, SignalRDisconnectedEvent):
            self._last_event_monotonic = event.disconnected_monotonic
            self._last_connected_utc = None
            self._last_error = self._redact(event.reason)
            self._bump_generation_and_cancel_live_tasks()
            if self._recovery_task is not None and not self._recovery_task.done():
                self._recovery_task.cancel()
            self._transition_all_contracts("DISCONNECTED")
            return

        if isinstance(event, SignalRErrorEvent):
            self._last_error = self._redact(event.message)
            return

        if isinstance(event, SignalRTickEvent):
            self._last_event_monotonic = event.received_monotonic
            c_state = self._contracts.get(event.contract_id)
            if c_state is not None:
                c_state.last_event_monotonic = event.received_monotonic

            if self._feed_state == "RECOVERING" or (
                c_state is not None and c_state.state == "RECOVERING"
            ):
                try:
                    self._recovery_queue.put_nowait(event)
                except asyncio.QueueFull:
                    self._queue_overflow_count += 1
                    self._recovery_overflowed = True
                    if c_state is not None:
                        c_state.unresolved_gaps += 1
                    else:
                        self._global_unresolved_gaps += 1
                return

            if self._feed_state in {"LIVE", "DEGRADED"}:
                self._ingest_live_tick_sync(event)

    def _bump_generation_and_cancel_live_tasks(self) -> int:
        self._generation += 1
        for task in list(self._live_tick_tasks):
            if not task.done():
                task.cancel()
        return self._generation

    def _reset_recovery_queue(self) -> None:
        self._recovery_queue = asyncio.Queue(
            maxsize=self._settings.signalr_recovery_queue_maxsize
        )
        self._recovery_overflowed = False

    def _start_recovery_task(self, *, now_utc: int | None = None) -> asyncio.Task[None]:
        if self._recovery_task is not None and not self._recovery_task.done():
            self._recovery_task.cancel()
        self._recovery_task = asyncio.create_task(
            self._run_recovery(now_utc=now_utc),
            name="live-feed-recovery",
        )
        return self._recovery_task

    async def trigger_recovery(self, *, now_utc: int | None = None) -> None:
        """Manually trigger RECOVERING reconciliation and await completion."""
        self._bump_generation_and_cancel_live_tasks()
        self._transition_all_contracts("RECOVERING")
        task = self._start_recovery_task(now_utc=now_utc)
        await task

    def _drain_into_contract_buckets(
        self,
        pending_by_contract: dict[str, list[SignalRTickEvent]],
    ) -> None:
        while True:
            try:
                evt = self._recovery_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            pending_by_contract.setdefault(evt.contract_id.upper(), []).append(evt)

    def _effective_last_activity_monotonic(self) -> float | None:
        candidates = [
            t
            for t in (self._last_event_monotonic, self._hub.last_event_monotonic)
            if t is not None
        ]
        return max(candidates) if candidates else None

    async def _emit_signal(
        self,
        event: AggregatedBarEvent,
        tag: str | None,
    ) -> None:
        if self._on_bar_signal is not None:
            res = self._on_bar_signal(event, tag)
            if inspect.isawaitable(res):
                await res

    def _validate_rest_bars(
        self,
        raw_bars: Sequence[Any],
        *,
        key: StorageDatasetKey,
        tf: str,
        eff_now: int,
        eff_root: str | None,
        interval_start: int,
        interval_end: int,
    ) -> tuple[list[OHLCVBar], list[QualityIssueInput]]:
        valid_bars: list[OHLCVBar] = []
        issues: list[QualityIssueInput] = []
        max_ts = eff_now + DEFAULT_MAX_FUTURE_SKEW_SEC

        for raw in raw_bars:
            if not isinstance(raw, dict):
                val = validate_ohlcv_bar(raw, is_closed=False, now_utc=eff_now)
                issues.append(
                    QualityIssueInput(
                        bar_time=None,
                        interval_start=interval_start,
                        interval_end=interval_end,
                        issue_code=val.issue_code or "invalid_record",
                        detail=self._redact(val.issue_detail or "Invalid recovery bar record"),
                        raw_payload=self._redact(json.dumps(raw, default=str))[:2048],
                    )
                )
                continue

            parsed = parse_topstep_bar(raw)
            bar_ts = extract_raw_bar_timestamp(parsed)
            if bar_ts is not None and DEFAULT_MIN_BAR_TIMESTAMP <= bar_ts <= max_ts:
                bar_closed = is_bar_closed(bar_ts, tf, eff_now, root_symbol=eff_root)
            else:
                bar_closed = False
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
                        detail=self._redact(val.issue_detail or "Recovery OHLCV validation failed"),
                        raw_payload=self._redact(json.dumps(raw, default=str))[:2048],
                    )
                )
        return valid_bars, issues

    @staticmethod
    def _find_missing_open_buckets(
        *,
        timeframe: str,
        backfill_start: int,
        closed_end: int,
        present_closed_times: set[int],
        root_symbol: str | None,
    ) -> list[int]:
        if closed_end <= backfill_start:
            return []
        missing: list[int] = []
        cursor = first_bucket_open_at_or_after(backfill_start, timeframe)
        while cursor < closed_end:
            b_start, b_end = bucket_bounds(cursor, timeframe, root_symbol=root_symbol)
            if b_end > closed_end:
                break
            if (
                has_open_session_in_range(b_start, b_end, root_symbol=root_symbol)
                and b_start not in present_closed_times
            ):
                missing.append(b_start)
            cursor = b_end
        return missing

    def _ensure_generation(self, expected_gen: int | None) -> None:
        if expected_gen is not None and self._generation != expected_gen:
            raise _StaleGenerationAbort

    async def _recover_single_timeframe_rest(
        self,
        *,
        cid: str,
        c_state: ContractFeedRuntime,
        tf: str,
        eff_now: int,
        expected_gen: int,
        pending_by_contract: dict[str, list[SignalRTickEvent]],
        align_for_higher_tfs: Sequence[str] = (),
    ) -> tuple[bool, int, Any, bool]:
        self._ensure_generation(expected_gen)
        key = StorageDatasetKey("topstepx", c_state.live_mode, cid, tf)
        wm = await self._sqlite.get_watermark(key)
        self._ensure_generation(expected_gen)
        if wm is not None:
            self._aggregator.set_last_closed_time(
                cid, c_state.live_mode, tf, wm.last_closed_bar_time
            )
            c_state.watermarks[tf] = wm.last_closed_bar_time
            backfill_start = max(
                DEFAULT_MIN_BAR_TIMESTAMP,
                wm.last_closed_bar_time - (2 * TIMEFRAME_SECONDS[tf]),
            )
        else:
            backfill_start = max(
                DEFAULT_MIN_BAR_TIMESTAMP,
                eff_now - (self._initial_backfill_bars * TIMEFRAME_SECONDS[tf]),
            )

        if tf == "1m" and align_for_higher_tfs:
            for htf in align_for_higher_tfs:
                htf_key = StorageDatasetKey("topstepx", c_state.live_mode, cid, htf)
                wm_htf = await self._sqlite.get_watermark(htf_key)
                self._ensure_generation(expected_gen)
                if wm_htf is not None:
                    htf_overlap = max(
                        DEFAULT_MIN_BAR_TIMESTAMP,
                        wm_htf.last_closed_bar_time - (2 * TIMEFRAME_SECONDS[htf]),
                    )
                    backfill_start = min(backfill_start, htf_overlap)

            htf_candidates = [bucket_open_utc(backfill_start, htf) for htf in align_for_higher_tfs]
            htf_candidates.extend(
                bucket_open_utc(c_state.unhealed_gap_starts[htf], htf)
                for htf in align_for_higher_tfs
                if htf in c_state.unhealed_gap_starts
            )
            aligned_htf_start = max(DEFAULT_MIN_BAR_TIMESTAMP, min(htf_candidates))
            if aligned_htf_start < backfill_start:
                missing_ranges = await self._sqlite.compute_missing_ranges(
                    key, aligned_htf_start, backfill_start
                )
                self._ensure_generation(expected_gen)
                if missing_ranges:
                    backfill_start = min(backfill_start, missing_ranges[0][0])

        unhealed_start = c_state.unhealed_gap_starts.get(tf)
        if unhealed_start is not None:
            backfill_start = min(backfill_start, unhealed_start)

        backfill_end = max(eff_now, backfill_start + TIMEFRAME_SECONDS[tf])
        # Track start defensively so any exception during REST/persistence retains gap start.
        c_state.unhealed_gap_starts[tf] = min(
            c_state.unhealed_gap_starts.get(tf, backfill_start),
            backfill_start,
        )

        page_res = await asyncio.wait_for(
            self._rest.retrieve_bars_paginated(
                contract_id=cid,
                live=c_state.live_mode,
                timeframe=tf,
                start_ts=backfill_start,
                end_ts=backfill_end,
                include_partial_bar=False,
                root_symbol=c_state.root_symbol,
            ),
            timeout=self._settings.signalr_recovery_timeout_sec,
        )
        self._ensure_generation(expected_gen)

        valid_bars, issues = self._validate_rest_bars(
            page_res.raw_bars,
            key=key,
            tf=tf,
            eff_now=eff_now,
            eff_root=c_state.root_symbol,
            interval_start=backfill_start,
            interval_end=backfill_end,
        )
        had_gap = False
        if issues:
            await self._sqlite.record_quality_issues(key, issues)
            await self._sqlite.record_coverage(
                key,
                backfill_start,
                backfill_end,
                status="CORRUPTED",
                quarantined_count=len(issues),
                truncated=page_res.truncated,
                provider_gap=page_res.provider_gap,
            )
            had_gap = True
            c_state.unresolved_gaps += 1
        elif page_res.truncated or page_res.provider_gap:
            had_gap = True
            c_state.unresolved_gaps += 1

        self._drain_into_contract_buckets(pending_by_contract)
        contract_ticks = pending_by_contract.pop(cid, [])
        normalized_ticks = [
            NormalizedTick(
                contract_id=t.contract_id,
                live_mode=t.live_mode,
                timestamp_utc_sec=t.timestamp_utc_sec,
                price=t.price,
                volume=t.volume,
            )
            for t in contract_ticks
        ]

        mark_open_inc = (
            self._last_connected_utc is None
            or self._last_connected_utc >= bucket_open_utc(eff_now, tf)
        )
        recon = self._aggregator.reconcile_recovery(
            contract_id=cid,
            live_mode=c_state.live_mode,
            timeframe=tf,
            historical_bars=valid_bars,
            buffered_ticks=normalized_ticks,
            now_utc=eff_now,
            root_symbol=c_state.root_symbol,
            mark_open_incomplete=mark_open_inc,
        )
        # Clear any unconfirmed tick-closed markers emitted strictly inside this recovery merge;
        # completeness check below detects missing REST bars directly.
        self._aggregator.pop_discarded_incomplete_bars(
            contract_id=cid, live_mode=c_state.live_mode
        )

        closed_end = latest_closed_bucket_end_utc(eff_now, tf, root_symbol=c_state.root_symbol)
        cached_closed: Sequence[OHLCVBar] = ()
        if closed_end > backfill_start:
            cached_closed = await self._parquet.read_bars(
                key,
                start_ts=backfill_start,
                end_ts=closed_end - 1,
                include_provisional=False,
            )
            self._ensure_generation(expected_gen)

        # Only authoritative closed REST/cached bars satisfy gap closure (COR-001).
        present_closed_times = {b.time for b in cached_closed if b.is_closed} | {
            b.time for b in valid_bars if b.is_closed
        }
        missing_buckets = self._find_missing_open_buckets(
            timeframe=tf,
            backfill_start=backfill_start,
            closed_end=closed_end,
            present_closed_times=present_closed_times,
            root_symbol=c_state.root_symbol,
        )
        if missing_buckets:
            if not had_gap:
                had_gap = True
                c_state.unresolved_gaps += 1
            if not issues and not page_res.truncated and not page_res.provider_gap:
                c_state.unhealed_gap_starts[tf] = missing_buckets[0]
                await self._sqlite.record_coverage(
                    key,
                    missing_buckets[0],
                    closed_end,
                    status="PROVIDER_GAP",
                    provider_gap=True,
                )

        self._ensure_generation(expected_gen)
        closed_bars = [evt.bar for evt in recon.closed_bars]
        if closed_bars:
            await self._parquet.upsert_bars(
                key,
                closed_bars,
                root_symbol=c_state.root_symbol,
                now_utc=eff_now,
            )
            self._ensure_generation(expected_gen)

        if not had_gap:
            c_state.unhealed_gap_starts.pop(tf, None)
            if recon.last_closed_bar_time is not None:
                updated_wm = await self._sqlite.upsert_watermark(
                    key, recon.last_closed_bar_time
                )
                c_state.watermarks[tf] = updated_wm.last_closed_bar_time

        if not had_gap and closed_end > backfill_start:
            await self._sqlite.record_coverage(
                key,
                backfill_start,
                closed_end,
                status="COMPLETE",
                heal_existing=True,
            )

        for closed_evt in recon.closed_bars:
            self._ensure_generation(expected_gen)
            self._recovered_historical_count += 1
            await self._emit_signal(closed_evt, "RECOVERED_HISTORICAL")

        if recon.open_bar is not None and self._broadcaster is not None:
            self._ensure_generation(expected_gen)
            self._broadcaster.publish_bar(recon.open_bar)

        return had_gap, backfill_start, recon, bool(recon.closed_bars)

    async def _recover_higher_timeframes_from_1m(
        self,
        *,
        cid: str,
        c_state: ContractFeedRuntime,
        higher_tfs: Sequence[str],
        backfill_start_1m: int,
        recon_1m: Any,
        eff_now: int,
        had_1m_gap: bool,
        expected_gen: int,
    ) -> bool:
        self._ensure_generation(expected_gen)
        key_1m = StorageDatasetKey("topstepx", c_state.live_mode, cid, "1m")
        htf_candidates = [bucket_open_utc(backfill_start_1m, htf) for htf in higher_tfs]
        htf_candidates.extend(
            bucket_open_utc(c_state.unhealed_gap_starts[htf], htf)
            for htf in higher_tfs
            if htf in c_state.unhealed_gap_starts
        )
        min_htf_start = min(htf_candidates)
        cached_1m = await self._parquet.read_bars(
            key_1m,
            start_ts=min_htf_start,
            end_ts=eff_now,
            include_provisional=False,
        )
        self._ensure_generation(expected_gen)
        merged_1m: dict[int, OHLCVBar] = {b.time: b for b in cached_1m}
        for evt in recon_1m.closed_bars:
            merged_1m[evt.bar.time] = evt.bar
        all_closed_1m = [merged_1m[t] for t in sorted(merged_1m)]
        open_1m_bar = recon_1m.open_bar.bar if recon_1m.open_bar is not None else None

        closed_1m_end = latest_closed_bucket_end_utc(
            eff_now, "1m", root_symbol=c_state.root_symbol
        )
        if had_1m_gap:
            covered_start_utc = eff_now + 1
        elif await self._sqlite.is_interval_complete(
            key_1m,
            min_htf_start,
            max(min_htf_start, closed_1m_end),
        ):
            covered_start_utc = min_htf_start
        else:
            covered_start_utc = backfill_start_1m

        recovered_any_closed = False
        for htf in higher_tfs:
            self._ensure_generation(expected_gen)
            key_htf = StorageDatasetKey("topstepx", c_state.live_mode, cid, htf)
            wm_htf = await self._sqlite.get_watermark(key_htf)
            self._ensure_generation(expected_gen)
            if wm_htf is not None:
                self._aggregator.set_last_closed_time(
                    cid, c_state.live_mode, htf, wm_htf.last_closed_bar_time
                )
                c_state.watermarks[htf] = wm_htf.last_closed_bar_time

            recon_htf = self._aggregator.reconcile_from_1m_bars(
                contract_id=cid,
                live_mode=c_state.live_mode,
                timeframe=htf,
                closed_1m_bars=all_closed_1m,
                open_1m_bar=open_1m_bar,
                now_utc=eff_now,
                root_symbol=c_state.root_symbol,
                covered_start_utc=covered_start_utc,
            )
            closed_htf = [evt.bar for evt in recon_htf.closed_bars]
            if closed_htf:
                recovered_any_closed = True
                self._ensure_generation(expected_gen)
                await self._parquet.upsert_bars(
                    key_htf,
                    closed_htf,
                    root_symbol=c_state.root_symbol,
                    now_utc=eff_now,
                )
                self._ensure_generation(expected_gen)

            htf_gap_start = c_state.unhealed_gap_starts.get(htf)
            htf_clean = (
                not had_1m_gap
                and "1m" not in c_state.unhealed_gap_starts
                and (
                    htf_gap_start is None
                    or covered_start_utc <= bucket_open_utc(htf_gap_start, htf)
                )
            )
            if htf_clean:
                c_state.unhealed_gap_starts.pop(htf, None)
                if recon_htf.last_closed_bar_time is not None:
                    updated_wm = await self._sqlite.upsert_watermark(
                        key_htf, recon_htf.last_closed_bar_time
                    )
                    c_state.watermarks[htf] = updated_wm.last_closed_bar_time

            for closed_evt in recon_htf.closed_bars:
                self._ensure_generation(expected_gen)
                self._recovered_historical_count += 1
                await self._emit_signal(closed_evt, "RECOVERED_HISTORICAL")

            if recon_htf.open_bar is not None and self._broadcaster is not None:
                self._ensure_generation(expected_gen)
                self._broadcaster.publish_bar(recon_htf.open_bar)
        return recovered_any_closed

    async def _run_recovery(self, *, now_utc: int | None = None) -> None:
        started_mono = time.monotonic()
        eff_now = now_utc if now_utc is not None else self._clock()
        if self._last_connected_utc is None:
            self._last_connected_utc = eff_now
        recovery_gen = self._generation
        prior_tasks = [t for t in self._live_tick_tasks if not t.done()]
        for t in prior_tasks:
            t.cancel()
        if prior_tasks:
            await asyncio.gather(*prior_tasks, return_exceptions=True)

        pending_by_contract: dict[str, list[SignalRTickEvent]] = {}
        contract_degraded: dict[str, bool] = {}

        try:
            self._ensure_generation(recovery_gen)
            for cid, c_state in list(self._contracts.items()):
                self._ensure_generation(recovery_gen)
                c_state.state = "RECOVERING"
                contract_had_gap = False
                recovered_closed = False
                try:
                    if "1m" in self._recovery_timeframes:
                        higher_tfs = [tf for tf in self._recovery_timeframes if tf != "1m"]
                        had_gap, backfill_start_1m, recon_1m, had_1m_closed = (
                            await self._recover_single_timeframe_rest(
                                cid=cid,
                                c_state=c_state,
                                tf="1m",
                                eff_now=eff_now,
                                expected_gen=recovery_gen,
                                pending_by_contract=pending_by_contract,
                                align_for_higher_tfs=higher_tfs,
                            )
                        )
                        if had_gap:
                            contract_had_gap = True
                        if had_1m_closed:
                            recovered_closed = True
                        if higher_tfs:
                            if await self._recover_higher_timeframes_from_1m(
                                cid=cid,
                                c_state=c_state,
                                higher_tfs=higher_tfs,
                                backfill_start_1m=backfill_start_1m,
                                recon_1m=recon_1m,
                                eff_now=eff_now,
                                had_1m_gap=had_gap,
                                expected_gen=recovery_gen,
                            ):
                                recovered_closed = True
                    else:
                        for tf in self._recovery_timeframes:
                            had_gap, _, _, had_tf_closed = (
                                await self._recover_single_timeframe_rest(
                                    cid=cid,
                                    c_state=c_state,
                                    tf=tf,
                                    eff_now=eff_now,
                                    expected_gen=recovery_gen,
                                    pending_by_contract=pending_by_contract,
                                )
                            )
                            if had_gap:
                                contract_had_gap = True
                            if had_tf_closed:
                                recovered_closed = True
                except (asyncio.CancelledError, _StaleGenerationAbort):
                    raise
                except Exception as exc:
                    contract_had_gap = True
                    c_state.unresolved_gaps += 1
                    c_state.last_error = self._redact(str(exc) or type(exc).__name__)
                    self._last_error = c_state.last_error
                    logger.warning(
                        "Recovery failed for %s: %s",
                        cid,
                        c_state.last_error,
                    )

                self._ensure_generation(recovery_gen)
                if recovered_closed and self._broadcaster is not None:
                    self._broadcaster.publish_resync_required(
                        "recovery_complete",
                        contract_id=cid,
                    )

                if self._recovery_overflowed or c_state.unhealed_gap_starts:
                    contract_had_gap = True

                contract_degraded[cid] = contract_had_gap
                if contract_had_gap:
                    c_state.state = "DEGRADED"
                    self._last_degraded_at_mono = time.monotonic()
                else:
                    c_state.last_error = None
                c_state.recovery_duration_ms = int((time.monotonic() - started_mono) * 1000)

            # Atomically drain all remaining/late ticks before leaving RECOVERING.
            while True:
                self._ensure_generation(recovery_gen)
                self._drain_into_contract_buckets(pending_by_contract)
                remaining: list[SignalRTickEvent] = []
                for tick_list in pending_by_contract.values():
                    remaining.extend(tick_list)
                pending_by_contract.clear()

                if not remaining:
                    self._recovery_duration_ms = int((time.monotonic() - started_mono) * 1000)
                    for cid, c_state in self._contracts.items():
                        if (
                            self._recovery_overflowed
                            or contract_degraded.get(cid, False)
                            or c_state.unhealed_gap_starts
                        ):
                            c_state.state = "DEGRADED"
                            self._last_degraded_at_mono = time.monotonic()
                        else:
                            c_state.state = "LIVE"
                    if self._recovery_overflowed:
                        self._feed_state = "DEGRADED"
                        self._last_degraded_at_mono = time.monotonic()
                    else:
                        self._recompute_aggregate_state()
                        if not self._contracts:
                            self._feed_state = "LIVE"
                    break

                remaining.sort(key=lambda t: t.received_monotonic)
                for tick_evt in remaining:
                    await self._process_live_tick(
                        tick_evt,
                        now_utc=eff_now,
                        expected_gen=recovery_gen,
                    )

            self._ensure_generation(recovery_gen)
            self._broadcast_status()
        except _StaleGenerationAbort:
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._generation != recovery_gen:
                return
            self._global_unresolved_gaps += 1
            self._last_error = self._redact(str(exc) or type(exc).__name__)
            self._recovery_duration_ms = int((time.monotonic() - started_mono) * 1000)
            self._transition_all_contracts("DEGRADED")
            logger.warning("Recovery coordinator error: %s", self._last_error)

    def _on_live_task_done(self, task: asyncio.Task[None]) -> None:
        self._live_tick_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("Unexpected live task error: %s", self._redact(str(exc)))

    def _handle_discarded_incomplete_bars(
        self,
        *,
        contract_id: str | None = None,
        live_mode: bool | None = None,
    ) -> bool:
        discarded = self._aggregator.pop_discarded_incomplete_bars(
            contract_id=contract_id,
            live_mode=live_mode,
        )
        if not discarded:
            return False
        for cid, _lmode, tf, bar_time in discarded:
            c_state = self._contracts.get(cid)
            if c_state is not None:
                c_state.unresolved_gaps += 1
                prev_gap = c_state.unhealed_gap_starts.get(tf)
                c_state.unhealed_gap_starts[tf] = (
                    bar_time if prev_gap is None else min(prev_gap, bar_time)
                )
                c_state.last_error = f"Unconfirmed interrupted {tf} bar at {bar_time}"
                c_state.state = "DEGRADED"
        self._last_degraded_at_mono = time.monotonic()
        self._recompute_aggregate_state()
        self._broadcast_status()
        return True

    def _ingest_live_tick_sync(
        self,
        event: SignalRTickEvent,
        *,
        now_utc: int | None = None,
    ) -> list[AggregatedBarEvent]:
        c_state = self._contracts.get(event.contract_id)
        root_sym = (
            c_state.root_symbol
            if c_state is not None
            else infer_root_symbol(event.contract_id)
        )
        tick = NormalizedTick(
            contract_id=event.contract_id,
            live_mode=event.live_mode,
            timestamp_utc_sec=event.timestamp_utc_sec,
            price=event.price,
            volume=event.volume,
        )
        eff_now = now_utc if now_utc is not None else self._clock()
        gen = self._generation
        res = self._aggregator.process_tick(tick, now_utc=eff_now, root_symbol=root_sym)
        self._handle_discarded_incomplete_bars(
            contract_id=event.contract_id,
            live_mode=event.live_mode,
        )
        if not res.accepted or not res.events:
            return []
        for bar_evt in res.events:
            if self._broadcaster is not None:
                self._broadcaster.publish_bar(bar_evt)
        closed_or_signal = [
            evt
            for evt in res.events
            if evt.bar.is_closed or self._on_bar_signal is not None
        ]
        if closed_or_signal:
            task = asyncio.create_task(
                self._safe_persist_and_signal_events(
                    res.events,
                    now_utc=eff_now,
                    broadcast=False,
                    expected_gen=gen,
                )
            )
            self._live_tick_tasks.add(task)
            task.add_done_callback(self._on_live_task_done)
        return list(res.events)

    async def flush_pending_tasks(self) -> None:
        """Await any in-flight recovery or closed-bar persistence tasks."""
        if self._recovery_task is not None and not self._recovery_task.done():
            await self._recovery_task
        if self._live_tick_tasks:
            await asyncio.gather(*tuple(self._live_tick_tasks), return_exceptions=True)

    async def _process_live_tick(
        self,
        event: SignalRTickEvent,
        *,
        now_utc: int | None = None,
        expected_gen: int | None = None,
    ) -> None:
        self._ensure_generation(expected_gen)
        c_state = self._contracts.get(event.contract_id)
        root_sym = (
            c_state.root_symbol
            if c_state is not None
            else infer_root_symbol(event.contract_id)
        )
        tick = NormalizedTick(
            contract_id=event.contract_id,
            live_mode=event.live_mode,
            timestamp_utc_sec=event.timestamp_utc_sec,
            price=event.price,
            volume=event.volume,
        )
        eff_now = now_utc if now_utc is not None else self._clock()
        res = self._aggregator.process_tick(tick, now_utc=eff_now, root_symbol=root_sym)
        self._handle_discarded_incomplete_bars(
            contract_id=event.contract_id,
            live_mode=event.live_mode,
        )
        if not res.accepted:
            return
        await self._safe_persist_and_signal_events(
            res.events,
            now_utc=eff_now,
            broadcast=True,
            expected_gen=expected_gen,
        )

    async def _safe_persist_and_signal_events(
        self,
        events: Sequence[AggregatedBarEvent],
        *,
        now_utc: int,
        broadcast: bool = True,
        expected_gen: int | None = None,
    ) -> None:
        try:
            await self._persist_and_signal_events(
                events,
                now_utc=now_utc,
                broadcast=broadcast,
                expected_gen=expected_gen,
            )
        except (asyncio.CancelledError, _StaleGenerationAbort):
            return
        except Exception as exc:
            if expected_gen is not None and self._generation != expected_gen:
                return
            redacted_err = self._redact(str(exc) or type(exc).__name__)
            self._last_error = redacted_err
            marked_any = False
            for bar_evt in events:
                c_state = self._contracts.get(bar_evt.contract_id)
                if c_state is not None:
                    marked_any = True
                    c_state.unresolved_gaps += 1
                    c_state.last_error = redacted_err
                    c_state.state = "DEGRADED"
                    if bar_evt.bar.is_closed:
                        prev_gap = c_state.unhealed_gap_starts.get(bar_evt.timeframe)
                        c_state.unhealed_gap_starts[bar_evt.timeframe] = (
                            bar_evt.bar.time
                            if prev_gap is None
                            else min(prev_gap, bar_evt.bar.time)
                        )
            if not marked_any:
                self._global_unresolved_gaps += 1
            self._last_degraded_at_mono = time.monotonic()
            self._recompute_aggregate_state()
            self._broadcast_status()
            logger.warning("Live persistence/signal failure: %s", redacted_err)

    async def _persist_and_signal_events(
        self,
        events: Sequence[AggregatedBarEvent],
        *,
        now_utc: int,
        broadcast: bool = True,
        expected_gen: int | None = None,
    ) -> None:
        for bar_evt in events:
            self._ensure_generation(expected_gen)
            if bar_evt.bar.is_closed:
                key = StorageDatasetKey(
                    "topstepx",
                    bar_evt.live_mode,
                    bar_evt.contract_id,
                    bar_evt.timeframe,
                )
                await self._parquet.upsert_bars(
                    key,
                    [bar_evt.bar],
                    root_symbol=bar_evt.root_symbol,
                    now_utc=now_utc,
                )
                self._ensure_generation(expected_gen)
                c_state = self._contracts.get(bar_evt.contract_id)
                blocked_by_gap = False
                if c_state is not None:
                    gap_floor = c_state.unhealed_gap_starts.get(bar_evt.timeframe)
                    gap_1m = c_state.unhealed_gap_starts.get("1m")
                    if gap_floor is not None and bar_evt.bar.time >= gap_floor:
                        blocked_by_gap = True
                    elif (
                        gap_1m is not None
                        and bar_evt.timeframe != "1m"
                        and bucket_bounds(
                            bar_evt.bar.time,
                            bar_evt.timeframe,
                            root_symbol=bar_evt.root_symbol,
                        )[1]
                        > gap_1m
                    ):
                        blocked_by_gap = True
                if not blocked_by_gap:
                    wm = await self._sqlite.upsert_watermark(key, bar_evt.bar.time)
                    self._ensure_generation(expected_gen)
                    if c_state is not None:
                        c_state.watermarks[bar_evt.timeframe] = wm.last_closed_bar_time
                tag = (
                    None
                    if self.is_contract_live_alerts_enabled(bar_evt.contract_id)
                    else "SUPPRESSED"
                )
                await self._emit_signal(bar_evt, tag)

            if broadcast and self._broadcaster is not None:
                self._ensure_generation(expected_gen)
                self._broadcaster.publish_bar(bar_evt)

    async def run_watchdog_tick(self) -> None:
        """Execute one watchdog pass: bar-close flush, auth/rotation check, heartbeat, retry."""
        now_mono = time.monotonic()
        now_ts = self._clock()

        rotation = self._auth.compute_key_rotation()
        rotation_blocked = rotation.key_rotation_enforced and (
            rotation.key_rotation_due or rotation.key_age_days is None
        )
        if rotation_blocked or not self._auth.is_connected:
            if rotation_blocked and self._auth.is_connected:
                with contextlib.suppress(Exception):
                    await self._auth.ensure_token()
            if self._hub.is_connected or self._feed_state != "DISCONNECTED":
                auth_err = self._auth.get_status().get("last_error")
                self._last_error = (
                    self._redact(str(auth_err))
                    if auth_err
                    else "TopstepX authentication invalidated"
                )
                self._bump_generation_and_cancel_live_tasks()
                await self._hub.stop()
                self._transition_all_contracts("DISCONNECTED")
            return

        # Only flush elapsed open bars for LIVE contracts; RECOVERING uses REST + buffered ticks.
        for cid, c_state in list(self._contracts.items()):
            if c_state.state != "LIVE":
                continue
            live_gen = self._generation
            flushed = self._aggregator.flush_closed_bars(
                now_utc=now_ts,
                contract_id=cid,
                live_mode=c_state.live_mode,
            )
            self._handle_discarded_incomplete_bars(
                contract_id=cid,
                live_mode=c_state.live_mode,
            )
            if flushed:
                await self._safe_persist_and_signal_events(
                    flushed,
                    now_utc=now_ts,
                    broadcast=True,
                    expected_gen=live_gen,
                )

        effective_last = self._effective_last_activity_monotonic()
        if (
            self._feed_state in {"LIVE", "DEGRADED"}
            and effective_last is not None
            and (now_mono - effective_last) > self._settings.signalr_heartbeat_timeout_sec
        ):
            self._last_error = "SignalR heartbeat watchdog timeout"
            self._bump_generation_and_cancel_live_tasks()
            self._transition_all_contracts("DISCONNECTED")
            await self._hub.trigger_reconnect()
            return

        if (
            self._feed_state == "LIVE"
            and self._auth.is_connected
            and self._auth.needs_refresh()
        ):
            try:
                await self._auth.ensure_token()
                self._bump_generation_and_cancel_live_tasks()
                self._transition_all_contracts("RECOVERING")
                await self._hub.trigger_reconnect()
            except Exception as exc:
                self._last_error = self._redact(str(exc) or type(exc).__name__)
                self._bump_generation_and_cancel_live_tasks()
                await self._hub.stop()
                self._transition_all_contracts("DISCONNECTED")
            return

        if (
            self._feed_state == "DEGRADED"
            and self._hub.is_connected
            and (self._recovery_task is None or self._recovery_task.done())
            and self._last_degraded_at_mono is not None
            and (now_mono - self._last_degraded_at_mono) >= self._degraded_retry_interval_sec
        ):
            self._bump_generation_and_cancel_live_tasks()
            self._reset_recovery_queue()
            self._transition_all_contracts("RECOVERING")
            self._start_recovery_task()

    async def _watchdog_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.5)
            try:
                await self.run_watchdog_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._global_unresolved_gaps += 1
                self._last_error = self._redact(str(exc) or type(exc).__name__)
                for c_state in self._contracts.values():
                    if c_state.state != "DISCONNECTED":
                        c_state.state = "DEGRADED"
                self._last_degraded_at_mono = time.monotonic()
                self._recompute_aggregate_state()
                self._broadcast_status()
                logger.warning("Watchdog tick failed: %s", self._last_error)

    def get_status_snapshot(self) -> dict[str, Any]:
        """Return strictly redacted live-feed telemetry snapshot for /api/status and WS."""
        last_age: float | None = None
        effective_last = self._effective_last_activity_monotonic()
        if effective_last is not None:
            last_age = round(max(0.0, time.monotonic() - effective_last), 3)

        unresolved_gaps = self._global_unresolved_gaps + sum(
            c.unresolved_gaps for c in self._contracts.values()
        )
        contracts_summary: dict[str, dict[str, Any]] = {}
        for cid, c in sorted(self._contracts.items()):
            contracts_summary[cid] = {
                "contract_id": c.contract_id,
                "live_mode": c.live_mode,
                "root_symbol": c.root_symbol,
                "state": c.state,
                "watermarks": dict(c.watermarks),
                "unresolved_gaps": c.unresolved_gaps,
                "recovery_duration_ms": c.recovery_duration_ms,
                "last_error": self._redact(c.last_error) if c.last_error else None,
            }

        return {
            "feed_state": self._feed_state,
            "last_event_age_sec": last_age,
            "recovery_duration_ms": self._recovery_duration_ms,
            "unresolved_gaps": unresolved_gaps,
            "queue_overflow_count": self._queue_overflow_count,
            "live_alerts_enabled": self.live_alerts_enabled,
            "recovered_historical_count": self._recovered_historical_count,
            "contracts": contracts_summary,
        }
