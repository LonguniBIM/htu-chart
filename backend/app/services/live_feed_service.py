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
    is_bar_closed,
    latest_closed_bucket_end_utc,
    normalize_timeframe,
)
from app.security import redact_secrets
from app.services.ws_broadcaster import WsBroadcaster
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import QualityIssueInput, SqliteStore
from app.topstep.auth import TopstepAuthService
from app.topstep.errors import TopstepError
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
        recovery_timeframes: Sequence[str] = ("1m",),
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
        self._recovery_timeframes = tuple(
            dict.fromkeys(normalize_timeframe(tf) for tf in recovery_timeframes)
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

        removed = set(self._contracts) - set(normalized)
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

        await self._hub.update_contracts(list(normalized.keys()), live_mode=eff_live)

        if (
            self._running
            and self._settings.signalr_enabled
            and self._auth.is_connected
            and normalized
        ):
            if not self._hub.is_connected:
                self._hub.start()
            elif added_or_mode_changed or removed:
                await self.trigger_recovery()
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
        """Called when TopstepX auth acquires a JWT; starts SignalR if contracts are active."""
        await self.start()

    async def on_auth_disconnected(self) -> None:
        """Stop active SignalR and recovery tasks when TopstepX auth disconnects."""
        await self.stop()

    async def stop(self) -> None:
        """Stop SignalR, recovery tasks, and watchdog cleanly on shutdown or disconnect."""
        self._running = False
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
        self._transition_all_contracts("DISCONNECTED")

    def handle_hub_event(self, event: SignalREvent) -> None:
        """Handle lifecycle and tick events from SignalRMarketHub without blocking recv."""
        if isinstance(event, SignalRConnectedEvent):
            self._last_event_monotonic = event.connected_monotonic
            self._last_error = None
            for cid in event.contracts:
                self.register_contract(cid, live_mode=event.live_mode)
            self._reset_recovery_queue()
            self._transition_all_contracts("RECOVERING")
            self._start_recovery_task()
            return

        if isinstance(event, SignalRDisconnectedEvent):
            self._last_event_monotonic = event.disconnected_monotonic
            self._last_error = self._redact(event.reason)
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
        self._transition_all_contracts("RECOVERING")
        task = self._start_recovery_task(now_utc=now_utc)
        await task

    def _drain_recovery_queue(self) -> list[SignalRTickEvent]:
        drained: list[SignalRTickEvent] = []
        while True:
            try:
                drained.append(self._recovery_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return drained

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

    async def _run_recovery(self, *, now_utc: int | None = None) -> None:
        started_mono = time.monotonic()
        eff_now = now_utc if now_utc is not None else self._clock()
        buffered_events: list[SignalRTickEvent] = []

        try:
            for cid, c_state in list(self._contracts.items()):
                c_state.state = "RECOVERING"
                contract_had_gap = False

                for tf in self._recovery_timeframes:
                    key = StorageDatasetKey("topstepx", c_state.live_mode, cid, tf)
                    wm = await self._sqlite.get_watermark(key)
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
                    backfill_end = max(eff_now, backfill_start + TIMEFRAME_SECONDS[tf])

                    try:
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
                    except asyncio.CancelledError:
                        raise
                    except (TimeoutError, TopstepError, Exception) as exc:
                        contract_had_gap = True
                        c_state.unresolved_gaps += 1
                        c_state.last_error = self._redact(str(exc) or type(exc).__name__)
                        c_state.state = "DEGRADED"
                        self._last_error = c_state.last_error
                        self._last_degraded_at_mono = time.monotonic()
                        logger.warning(
                            "Recovery backfill failed for %s (%s): %s",
                            cid,
                            tf,
                            c_state.last_error,
                        )
                        break

                    valid_bars, issues = self._validate_rest_bars(
                        page_res.raw_bars,
                        key=key,
                        tf=tf,
                        eff_now=eff_now,
                        eff_root=c_state.root_symbol,
                        interval_start=backfill_start,
                        interval_end=backfill_end,
                    )
                    if issues:
                        await self._sqlite.record_quality_issues(key, issues)

                    if page_res.truncated or page_res.provider_gap:
                        contract_had_gap = True
                        c_state.unresolved_gaps += 1

                    buffered_events.extend(self._drain_recovery_queue())
                    normalized_ticks = [
                        NormalizedTick(
                            contract_id=t.contract_id,
                            live_mode=t.live_mode,
                            timestamp_utc_sec=t.timestamp_utc_sec,
                            price=t.price,
                            volume=t.volume,
                        )
                        for t in buffered_events
                    ]

                    recon = self._aggregator.reconcile_recovery(
                        contract_id=cid,
                        live_mode=c_state.live_mode,
                        timeframe=tf,
                        historical_bars=valid_bars,
                        buffered_ticks=normalized_ticks,
                        now_utc=eff_now,
                        root_symbol=c_state.root_symbol,
                    )

                    closed_bars = [evt.bar for evt in recon.closed_bars]
                    if closed_bars:
                        await self._parquet.upsert_bars(
                            key,
                            closed_bars,
                            root_symbol=c_state.root_symbol,
                            now_utc=eff_now,
                        )

                    if recon.last_closed_bar_time is not None:
                        updated_wm = await self._sqlite.upsert_watermark(
                            key, recon.last_closed_bar_time
                        )
                        c_state.watermarks[tf] = updated_wm.last_closed_bar_time

                    closed_end = latest_closed_bucket_end_utc(
                        eff_now, tf, root_symbol=c_state.root_symbol
                    )
                    if (
                        not page_res.truncated
                        and not page_res.provider_gap
                        and not issues
                        and closed_end > backfill_start
                    ):
                        await self._sqlite.record_coverage(
                            key,
                            backfill_start,
                            closed_end,
                            status="COMPLETE",
                            heal_existing=True,
                        )

                    for closed_evt in recon.closed_bars:
                        self._recovered_historical_count += 1
                        await self._emit_signal(closed_evt, "RECOVERED_HISTORICAL")
                        if self._broadcaster is not None:
                            self._broadcaster.publish_bar(closed_evt)

                    if recon.open_bar is not None and self._broadcaster is not None:
                        self._broadcaster.publish_bar(recon.open_bar)

                if self._recovery_overflowed:
                    contract_had_gap = True

                if contract_had_gap or c_state.state == "DEGRADED":
                    c_state.state = "DEGRADED"
                    self._last_degraded_at_mono = time.monotonic()
                else:
                    c_state.state = "LIVE"
                    c_state.last_error = None

                c_state.recovery_duration_ms = int((time.monotonic() - started_mono) * 1000)

            # Drain any ticks that arrived right as reconciliation finished.
            tail_ticks = self._drain_recovery_queue()
            for tick_evt in tail_ticks:
                await self._process_live_tick(tick_evt, now_utc=eff_now)

            self._recovery_duration_ms = int((time.monotonic() - started_mono) * 1000)
            if self._recovery_overflowed:
                for c_state in self._contracts.values():
                    c_state.state = "DEGRADED"
                self._feed_state = "DEGRADED"
                self._last_degraded_at_mono = time.monotonic()
            else:
                self._recompute_aggregate_state()
                if not self._contracts:
                    self._feed_state = "LIVE"
            self._broadcast_status()
        except asyncio.CancelledError:
            raise

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
        res = self._aggregator.process_tick(tick, now_utc=eff_now, root_symbol=root_sym)
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
                self._persist_and_signal_events(res.events, now_utc=eff_now, broadcast=False)
            )
            self._live_tick_tasks.add(task)
            task.add_done_callback(self._live_tick_tasks.discard)
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
    ) -> None:
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
        if not res.accepted:
            return
        await self._persist_and_signal_events(res.events, now_utc=eff_now, broadcast=True)

    async def _persist_and_signal_events(
        self,
        events: Sequence[AggregatedBarEvent],
        *,
        now_utc: int,
        broadcast: bool = True,
    ) -> None:
        for bar_evt in events:
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
                wm = await self._sqlite.upsert_watermark(key, bar_evt.bar.time)
                c_state = self._contracts.get(bar_evt.contract_id)
                if c_state is not None:
                    c_state.watermarks[bar_evt.timeframe] = wm.last_closed_bar_time
                tag = (
                    None
                    if self.is_contract_live_alerts_enabled(bar_evt.contract_id)
                    else "SUPPRESSED"
                )
                await self._emit_signal(bar_evt, tag)

            if broadcast and self._broadcaster is not None:
                self._broadcaster.publish_bar(bar_evt)

    async def _watchdog_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.5)
            now_mono = time.monotonic()
            now_ts = self._clock()

            flushed = self._aggregator.flush_closed_bars(now_utc=now_ts)
            if flushed:
                await self._persist_and_signal_events(flushed, now_utc=now_ts, broadcast=True)

            if (
                self._feed_state in {"LIVE", "DEGRADED"}
                and self._last_event_monotonic is not None
                and (now_mono - self._last_event_monotonic)
                > self._settings.signalr_heartbeat_timeout_sec
            ):
                self._last_error = "SignalR heartbeat watchdog timeout"
                self._transition_all_contracts("DISCONNECTED")
                await self._hub.trigger_reconnect()
                continue

            if (
                self._feed_state == "LIVE"
                and self._auth.is_connected
                and self._auth.needs_refresh()
            ):
                try:
                    await self._auth.ensure_token()
                    self._transition_all_contracts("RECOVERING")
                    await self._hub.trigger_reconnect()
                except Exception as exc:
                    self._last_error = self._redact(str(exc) or type(exc).__name__)
                    self._transition_all_contracts("DISCONNECTED")
                continue

            if (
                self._feed_state == "DEGRADED"
                and self._hub.is_connected
                and (self._recovery_task is None or self._recovery_task.done())
                and self._last_degraded_at_mono is not None
                and (now_mono - self._last_degraded_at_mono) >= self._degraded_retry_interval_sec
            ):
                self._reset_recovery_queue()
                self._transition_all_contracts("RECOVERING")
                self._start_recovery_task()

    def get_status_snapshot(self) -> dict[str, Any]:
        """Return strictly redacted live-feed telemetry snapshot for /api/status and WS."""
        last_age: float | None = None
        if self._last_event_monotonic is not None:
            last_age = round(max(0.0, time.monotonic() - self._last_event_monotonic), 3)

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
