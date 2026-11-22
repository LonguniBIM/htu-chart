"""Pure real-time CME Globex OHLCV tick aggregator and reconciler (F03, R04, R05, ARCH-001)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.engine.instruments import infer_root_symbol
from app.engine.ohlcv import (
    DEFAULT_MAX_FUTURE_SKEW_SEC,
    DEFAULT_MIN_BAR_TIMESTAMP,
    OHLCVBar,
    validate_ohlcv_bar,
)
from app.engine.session_calendar import (
    bucket_bounds,
    bucket_open_utc,
    is_bar_closed,
    latest_closed_bucket_end_utc,
    normalize_timeframe,
)

DEFAULT_AGGREGATION_TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d")


@dataclass(frozen=True)
class NormalizedTick:
    """Normalized market tick/quote event consumed by BarAggregator."""

    contract_id: str
    live_mode: bool
    timestamp_utc_sec: int | float
    price: float
    volume: float = 0.0


@dataclass(frozen=True)
class AggregatedBarEvent:
    """Bar update emitted by BarAggregator (provisional open or finalized closed)."""

    contract_id: str
    live_mode: bool
    timeframe: str
    bar: OHLCVBar
    is_recovered_historical: bool = False
    root_symbol: str | None = None


@dataclass(frozen=True)
class TickProcessResult:
    """Outcome of ingesting a single NormalizedTick across configured timeframes."""

    accepted: bool
    reason: str | None
    events: tuple[AggregatedBarEvent, ...]


@dataclass(frozen=True)
class ReconciliationResult:
    """Deterministic merge of overlapping historical closed bars and recovery ticks."""

    contract_id: str
    live_mode: bool
    timeframe: str
    closed_bars: tuple[AggregatedBarEvent, ...]
    open_bar: AggregatedBarEvent | None
    last_closed_bar_time: int | None


def _utc_now_sec() -> int:
    return int(datetime.now(UTC).timestamp())


def _normalize_contract_id(contract_id: str) -> str:
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be a non-empty string")
    return contract_id.strip().upper()


def _validate_tick(
    tick: NormalizedTick,
    *,
    now_utc: int,
) -> tuple[str | None, bool, int | None, float | None, float | None, str | None]:
    if not isinstance(tick, NormalizedTick):
        return None, False, None, None, None, f"Expected NormalizedTick, got {type(tick).__name__}"
    if not isinstance(tick.contract_id, str) or not tick.contract_id.strip():
        return None, False, None, None, None, "Tick contract_id must be non-empty"
    if not isinstance(tick.live_mode, bool):
        return None, False, None, None, None, "Tick live_mode must be bool"

    raw_ts = tick.timestamp_utc_sec
    if isinstance(raw_ts, bool) or not isinstance(raw_ts, (int, float)):
        return None, False, None, None, None, f"Invalid tick timestamp type: {raw_ts!r}"
    if not math.isfinite(float(raw_ts)):
        return None, False, None, None, None, f"Non-finite tick timestamp: {raw_ts!r}"
    ts_sec = math.floor(float(raw_ts))
    max_ts = now_utc + DEFAULT_MAX_FUTURE_SKEW_SEC
    if ts_sec < DEFAULT_MIN_BAR_TIMESTAMP or ts_sec > max_ts:
        return (
            None,
            False,
            None,
            None,
            None,
            f"Tick timestamp {ts_sec} outside [{DEFAULT_MIN_BAR_TIMESTAMP}, {max_ts}]",
        )

    raw_price = tick.price
    if isinstance(raw_price, bool) or not isinstance(raw_price, (int, float)):
        return None, False, None, None, None, f"Invalid tick price type: {raw_price!r}"
    try:
        price = float(raw_price)
    except OverflowError:
        return None, False, None, None, None, f"Overflow tick price: {raw_price!r}"
    if not math.isfinite(price) or price <= 0.0:
        return None, False, None, None, None, f"Invalid tick price: {raw_price!r}"

    raw_vol = tick.volume
    if isinstance(raw_vol, bool) or not isinstance(raw_vol, (int, float)):
        return None, False, None, None, None, f"Invalid tick volume type: {raw_vol!r}"
    try:
        volume = float(raw_vol)
    except OverflowError:
        return None, False, None, None, None, f"Overflow tick volume: {raw_vol!r}"
    if not math.isfinite(volume) or volume < 0.0:
        return None, False, None, None, None, f"Invalid tick volume: {raw_vol!r}"

    return tick.contract_id.strip().upper(), tick.live_mode, ts_sec, price, volume, None


def _bar_to_mapping(bar: OHLCVBar, *, is_closed: bool) -> dict[str, object]:
    return {
        "time": bar.time,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "is_closed": is_closed,
    }


class BarAggregator:
    """Pure stateful tick-to-OHLCV bar aggregator aligned to CME Globex 18:00 ET sessions."""

    def __init__(
        self,
        timeframes: Sequence[str] = DEFAULT_AGGREGATION_TIMEFRAMES,
    ) -> None:
        normalized: list[str] = []
        seen: set[str] = set()
        for tf in timeframes:
            canon = normalize_timeframe(tf)
            if canon not in seen:
                seen.add(canon)
                normalized.append(canon)
        if not normalized:
            raise ValueError("BarAggregator requires at least one timeframe")
        self._timeframes: tuple[str, ...] = tuple(normalized)
        self._open_bars: dict[tuple[str, bool, str], OHLCVBar] = {}
        self._incomplete_open_bars: set[tuple[str, bool, str]] = set()
        self._last_closed_times: dict[tuple[str, bool, str], int] = {}
        self._root_symbols: dict[tuple[str, bool], str | None] = {}

    @property
    def timeframes(self) -> tuple[str, ...]:
        """Configured canonical timeframes."""
        return self._timeframes

    def set_last_closed_time(
        self,
        contract_id: str,
        live_mode: bool,
        timeframe: str,
        bar_time: int,
    ) -> None:
        """Seed or advance the closed-bar watermark for a stream."""
        cid = _normalize_contract_id(contract_id)
        tf = normalize_timeframe(timeframe)
        key = (cid, bool(live_mode), tf)
        prev = self._last_closed_times.get(key)
        if prev is None or bar_time > prev:
            self._last_closed_times[key] = int(bar_time)

    def get_last_closed_time(
        self,
        contract_id: str,
        live_mode: bool,
        timeframe: str,
    ) -> int | None:
        """Return latest closed bar-open UTC timestamp for a stream, if known."""
        cid = _normalize_contract_id(contract_id)
        tf = normalize_timeframe(timeframe)
        return self._last_closed_times.get((cid, bool(live_mode), tf))

    def get_open_bar(
        self,
        contract_id: str,
        live_mode: bool,
        timeframe: str,
    ) -> OHLCVBar | None:
        """Return the current provisional open bar for a stream, if active."""
        cid = _normalize_contract_id(contract_id)
        tf = normalize_timeframe(timeframe)
        return self._open_bars.get((cid, bool(live_mode), tf))

    def reset_contract(self, contract_id: str, live_mode: bool | None = None) -> None:
        """Clear in-memory open bars and watermarks for a contract (e.g. rollover)."""
        cid = _normalize_contract_id(contract_id)
        for key in list(self._open_bars):
            if key[0] == cid and (live_mode is None or key[1] == bool(live_mode)):
                self._open_bars.pop(key, None)
                self._incomplete_open_bars.discard(key)
        for key in list(self._last_closed_times):
            if key[0] == cid and (live_mode is None or key[1] == bool(live_mode)):
                self._last_closed_times.pop(key, None)
        for rkey in list(self._root_symbols):
            if rkey[0] == cid and (live_mode is None or rkey[1] == bool(live_mode)):
                self._root_symbols.pop(rkey, None)

    def _resolve_root(
        self,
        cid: str,
        live_mode: bool,
        root_symbol: str | None,
    ) -> str | None:
        rkey = (cid, live_mode)
        if root_symbol is not None and root_symbol.strip():
            resolved = root_symbol.strip().upper()
            self._root_symbols[rkey] = resolved
            return resolved
        if rkey in self._root_symbols:
            return self._root_symbols[rkey]
        inferred = infer_root_symbol(cid)
        self._root_symbols[rkey] = inferred
        return inferred

    def process_tick(
        self,
        tick: NormalizedTick,
        *,
        now_utc: int | None = None,
        root_symbol: str | None = None,
    ) -> TickProcessResult:
        """Validate and aggregate a single tick into provisional/closed OHLCV bars."""
        eff_now = now_utc if now_utc is not None else _utc_now_sec()
        cid, live_mode, ts_sec, price, volume, err = _validate_tick(tick, now_utc=eff_now)
        if err is not None or cid is None or ts_sec is None or price is None or volume is None:
            return TickProcessResult(accepted=False, reason=err, events=())

        eff_root = self._resolve_root(cid, live_mode, root_symbol)
        emitted: list[AggregatedBarEvent] = []

        for tf in self._timeframes:
            stream_key = (cid, live_mode, tf)
            bucket_ts = bucket_open_utc(ts_sec, tf)
            last_closed = self._last_closed_times.get(stream_key)
            if last_closed is not None and bucket_ts <= last_closed:
                continue

            existing = self._open_bars.get(stream_key)
            if existing is not None:
                if bucket_ts < existing.time:
                    continue
                if bucket_ts > existing.time:
                    is_incomplete_existing = stream_key in self._incomplete_open_bars
                    self._incomplete_open_bars.discard(stream_key)
                    self._open_bars.pop(stream_key, None)
                    if not is_incomplete_existing:
                        closed_res = validate_ohlcv_bar(
                            _bar_to_mapping(existing, is_closed=True),
                            is_closed=True,
                            now_utc=max(eff_now, existing.time),
                        )
                        if closed_res.is_valid and closed_res.bar is not None:
                            self._last_closed_times[stream_key] = closed_res.bar.time
                            emitted.append(
                                AggregatedBarEvent(
                                    contract_id=cid,
                                    live_mode=live_mode,
                                    timeframe=tf,
                                    bar=closed_res.bar,
                                    is_recovered_historical=False,
                                    root_symbol=eff_root,
                                )
                            )
                    open_candidate = {
                        "time": bucket_ts,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": volume,
                        "is_closed": False,
                    }
                    open_res = validate_ohlcv_bar(
                        open_candidate,
                        is_closed=False,
                        now_utc=max(eff_now, bucket_ts),
                    )
                    if open_res.is_valid and open_res.bar is not None:
                        self._open_bars[stream_key] = open_res.bar
                        emitted.append(
                            AggregatedBarEvent(
                                contract_id=cid,
                                live_mode=live_mode,
                                timeframe=tf,
                                bar=open_res.bar,
                                is_recovered_historical=False,
                                root_symbol=eff_root,
                            )
                        )
                else:
                    updated_candidate = {
                        "time": existing.time,
                        "open": existing.open,
                        "high": max(existing.high, price),
                        "low": min(existing.low, price),
                        "close": price,
                        "volume": existing.volume + volume,
                        "is_closed": False,
                    }
                    upd_res = validate_ohlcv_bar(
                        updated_candidate,
                        is_closed=False,
                        now_utc=max(eff_now, existing.time),
                    )
                    if upd_res.is_valid and upd_res.bar is not None:
                        self._open_bars[stream_key] = upd_res.bar
                        emitted.append(
                            AggregatedBarEvent(
                                contract_id=cid,
                                live_mode=live_mode,
                                timeframe=tf,
                                bar=upd_res.bar,
                                is_recovered_historical=False,
                                root_symbol=eff_root,
                            )
                        )
            else:
                new_candidate = {
                    "time": bucket_ts,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume,
                    "is_closed": False,
                }
                new_res = validate_ohlcv_bar(
                    new_candidate,
                    is_closed=False,
                    now_utc=max(eff_now, bucket_ts),
                )
                if new_res.is_valid and new_res.bar is not None:
                    self._open_bars[stream_key] = new_res.bar
                    emitted.append(
                        AggregatedBarEvent(
                            contract_id=cid,
                            live_mode=live_mode,
                            timeframe=tf,
                            bar=new_res.bar,
                            is_recovered_historical=False,
                            root_symbol=eff_root,
                        )
                    )

        return TickProcessResult(accepted=True, reason=None, events=tuple(emitted))

    def flush_closed_bars(
        self,
        now_utc: int | None = None,
        *,
        contract_id: str | None = None,
        live_mode: bool | None = None,
    ) -> list[AggregatedBarEvent]:
        """Finalize any provisional open bars whose CME bucket close has elapsed by now_utc."""
        eff_now = now_utc if now_utc is not None else _utc_now_sec()
        target_cid = _normalize_contract_id(contract_id) if contract_id is not None else None
        flushed: list[AggregatedBarEvent] = []

        for stream_key in list(self._open_bars):
            cid, lmode, tf = stream_key
            if target_cid is not None and cid != target_cid:
                continue
            if live_mode is not None and lmode != bool(live_mode):
                continue
            open_bar = self._open_bars[stream_key]
            eff_root = self._resolve_root(cid, lmode, None)
            if not is_bar_closed(open_bar.time, tf, eff_now, root_symbol=eff_root):
                continue
            is_incomplete = stream_key in self._incomplete_open_bars
            self._incomplete_open_bars.discard(stream_key)
            self._open_bars.pop(stream_key, None)
            if is_incomplete:
                continue
            closed_res = validate_ohlcv_bar(
                _bar_to_mapping(open_bar, is_closed=True),
                is_closed=True,
                now_utc=max(eff_now, open_bar.time),
            )
            if closed_res.is_valid and closed_res.bar is not None:
                self._last_closed_times[stream_key] = closed_res.bar.time
                flushed.append(
                    AggregatedBarEvent(
                        contract_id=cid,
                        live_mode=lmode,
                        timeframe=tf,
                        bar=closed_res.bar,
                        is_recovered_historical=False,
                        root_symbol=eff_root,
                    )
                )

        return flushed

    def reconcile_recovery(
        self,
        *,
        contract_id: str,
        live_mode: bool,
        timeframe: str,
        historical_bars: Sequence[OHLCVBar],
        buffered_ticks: Sequence[NormalizedTick],
        now_utc: int | None = None,
        root_symbol: str | None = None,
    ) -> ReconciliationResult:
        """Merge authoritative historical closed bars with newer buffered live ticks."""
        cid = _normalize_contract_id(contract_id)
        tf = normalize_timeframe(timeframe)
        eff_live = bool(live_mode)
        eff_now = now_utc if now_utc is not None else _utc_now_sec()
        eff_root = self._resolve_root(cid, eff_live, root_symbol)
        stream_key = (cid, eff_live, tf)

        closed_by_time: dict[int, OHLCVBar] = {}
        for bar in historical_bars:
            if not isinstance(bar, OHLCVBar) or not bar.is_closed:
                continue
            val = validate_ohlcv_bar(
                _bar_to_mapping(bar, is_closed=True),
                is_closed=True,
                now_utc=max(eff_now, bar.time),
            )
            if val.is_valid and val.bar is not None:
                closed_by_time[val.bar.time] = val.bar

        latest_hist_closed = max(closed_by_time.keys(), default=None)
        prev_closed = self._last_closed_times.get(stream_key)
        auth_cutoff = max(
            (t for t in (latest_hist_closed, prev_closed) if t is not None),
            default=None,
        )

        # Replay only valid buffered ticks belonging to buckets strictly newer than closed history.
        # Preserve exact float timestamps for sub-second ordering, tie-breaking by arrival index.
        eligible_ticks: list[tuple[float, int, NormalizedTick]] = []
        for idx, tick in enumerate(buffered_ticks):
            t_cid, t_live, ts_sec, _, _, err = _validate_tick(tick, now_utc=eff_now)
            if err is not None or t_cid != cid or t_live != eff_live or ts_sec is None:
                continue
            bucket_ts = bucket_open_utc(ts_sec, tf)
            if auth_cutoff is not None and bucket_ts <= auth_cutoff:
                continue
            eligible_ticks.append((float(tick.timestamp_utc_sec), idx, tick))

        eligible_ticks.sort(key=lambda item: (item[0], item[1]))

        temp_agg = BarAggregator(timeframes=(tf,))
        if auth_cutoff is not None:
            temp_agg.set_last_closed_time(cid, eff_live, tf, auth_cutoff)
        pre_open = self._open_bars.get(stream_key)
        if pre_open is not None and (auth_cutoff is None or pre_open.time > auth_cutoff):
            temp_agg._open_bars[stream_key] = pre_open

        tick_closed_bars: dict[int, OHLCVBar] = {}
        for _, _, tick in eligible_ticks:
            res = temp_agg.process_tick(tick, now_utc=eff_now, root_symbol=eff_root)
            for evt in res.events:
                if evt.bar.is_closed:
                    tick_closed_bars[evt.bar.time] = evt.bar

        for flushed_evt in temp_agg.flush_closed_bars(
            eff_now, contract_id=cid, live_mode=eff_live
        ):
            tick_closed_bars[flushed_evt.bar.time] = flushed_evt.bar

        merged_closed: dict[int, OHLCVBar] = {**tick_closed_bars, **closed_by_time}
        closed_events = tuple(
            AggregatedBarEvent(
                contract_id=cid,
                live_mode=eff_live,
                timeframe=tf,
                bar=merged_closed[t],
                is_recovered_historical=True,
                root_symbol=eff_root,
            )
            for t in sorted(merged_closed)
        )

        rebuilt_open = temp_agg.get_open_bar(cid, eff_live, tf)
        open_event: AggregatedBarEvent | None = None
        self._incomplete_open_bars.discard(stream_key)
        if rebuilt_open is not None:
            self._open_bars[stream_key] = rebuilt_open
            open_event = AggregatedBarEvent(
                contract_id=cid,
                live_mode=eff_live,
                timeframe=tf,
                bar=rebuilt_open,
                is_recovered_historical=False,
                root_symbol=eff_root,
            )
        else:
            self._open_bars.pop(stream_key, None)

        final_last_closed = max(
            (t for t in (*merged_closed.keys(), prev_closed) if t is not None),
            default=None,
        )
        if final_last_closed is not None:
            self._last_closed_times[stream_key] = final_last_closed

        return ReconciliationResult(
            contract_id=cid,
            live_mode=eff_live,
            timeframe=tf,
            closed_bars=closed_events,
            open_bar=open_event,
            last_closed_bar_time=final_last_closed,
        )

    def reconcile_from_1m_bars(
        self,
        *,
        contract_id: str,
        live_mode: bool,
        timeframe: str,
        closed_1m_bars: Sequence[OHLCVBar],
        open_1m_bar: OHLCVBar | None = None,
        now_utc: int | None = None,
        root_symbol: str | None = None,
        covered_start_utc: int | None = None,
    ) -> ReconciliationResult:
        """Deterministically rebuild a higher timeframe from recovered 1m closed + open bars."""
        cid = _normalize_contract_id(contract_id)
        tf = normalize_timeframe(timeframe)
        if tf == "1m":
            raise ValueError("reconcile_from_1m_bars is for higher timeframes than 1m")
        eff_live = bool(live_mode)
        eff_now = now_utc if now_utc is not None else _utc_now_sec()
        eff_root = self._resolve_root(cid, eff_live, root_symbol)
        stream_key = (cid, eff_live, tf)

        prev_closed = self._last_closed_times.get(stream_key)
        pre_open = self._open_bars.get(stream_key)

        dedup_1m: dict[int, OHLCVBar] = {}
        for bar in closed_1m_bars:
            if not isinstance(bar, OHLCVBar) or not bar.is_closed:
                continue
            dedup_1m[bar.time] = bar
        if open_1m_bar is not None and open_1m_bar.time not in dedup_1m:
            dedup_1m[open_1m_bar.time] = open_1m_bar

        if not dedup_1m:
            if pre_open is not None and (prev_closed is None or pre_open.time > prev_closed):
                open_event = AggregatedBarEvent(
                    contract_id=cid,
                    live_mode=eff_live,
                    timeframe=tf,
                    bar=pre_open,
                    is_recovered_historical=False,
                    root_symbol=eff_root,
                )
                return ReconciliationResult(
                    contract_id=cid,
                    live_mode=eff_live,
                    timeframe=tf,
                    closed_bars=(),
                    open_bar=open_event,
                    last_closed_bar_time=prev_closed,
                )
            return ReconciliationResult(
                contract_id=cid,
                live_mode=eff_live,
                timeframe=tf,
                closed_bars=(),
                open_bar=None,
                last_closed_bar_time=prev_closed,
            )

        grouped: dict[int, list[OHLCVBar]] = {}
        for bar_time in sorted(dedup_1m):
            bar = dedup_1m[bar_time]
            htf_open = bucket_open_utc(bar.time, tf)
            grouped.setdefault(htf_open, []).append(bar)

        current_htf_open = bucket_open_utc(eff_now, tf)
        closed_1m_end = latest_closed_bucket_end_utc(eff_now, "1m", root_symbol=eff_root)
        closed_events: list[AggregatedBarEvent] = []
        latest_open_bar: OHLCVBar | None = None
        latest_open_incomplete = False

        for htf_open in sorted(grouped):
            members = grouped[htf_open]
            _, htf_end = bucket_bounds(htf_open, tf, root_symbol=eff_root)
            expected_1m_count = max(1, (htf_end - htf_open) // 60)
            all_inputs_closed = all(b.is_closed for b in members)
            bucket_elapsed = is_bar_closed(htf_open, tf, eff_now, root_symbol=eff_root)
            covered_ok = covered_start_utc is None or covered_start_utc <= htf_open
            full_1m_coverage = (
                covered_ok
                and len(members) == expected_1m_count
                and members[0].time == htf_open
                and members[-1].time == htf_end - 60
            )
            bar_closed = all_inputs_closed and bucket_elapsed and full_1m_coverage

            # Never finalize or publish an elapsed bucket with incomplete 1m source bars.
            if bucket_elapsed and not bar_closed:
                continue

            candidate = {
                "time": htf_open,
                "open": members[0].open,
                "high": max(b.high for b in members),
                "low": min(b.low for b in members),
                "close": members[-1].close,
                "volume": sum(b.volume for b in members),
                "is_closed": bar_closed,
            }
            val = validate_ohlcv_bar(
                candidate,
                is_closed=bar_closed,
                now_utc=max(eff_now, htf_open),
            )
            if not val.is_valid or val.bar is None:
                continue
            if bar_closed:
                closed_events.append(
                    AggregatedBarEvent(
                        contract_id=cid,
                        live_mode=eff_live,
                        timeframe=tf,
                        bar=val.bar,
                        is_recovered_historical=True,
                        root_symbol=eff_root,
                    )
                )
            elif htf_open >= current_htf_open:
                expected_closed_end = min(htf_end, closed_1m_end)
                expected_closed_count = max(0, (expected_closed_end - htf_open) // 60)
                closed_times = {b.time for b in members if b.is_closed}
                all_elapsed_1m_present = (
                    all(
                        (htf_open + (idx * 60)) in closed_times
                        for idx in range(expected_closed_count)
                    )
                    if expected_closed_count > 0
                    else (members[0].time == htf_open)
                )
                latest_open_bar = val.bar
                latest_open_incomplete = not (covered_ok and all_elapsed_1m_present)

        open_event: AggregatedBarEvent | None = None
        if latest_open_bar is not None:
            self._open_bars[stream_key] = latest_open_bar
            if latest_open_incomplete:
                self._incomplete_open_bars.add(stream_key)
            else:
                self._incomplete_open_bars.discard(stream_key)
            open_event = AggregatedBarEvent(
                contract_id=cid,
                live_mode=eff_live,
                timeframe=tf,
                bar=latest_open_bar,
                is_recovered_historical=False,
                root_symbol=eff_root,
            )
        else:
            self._open_bars.pop(stream_key, None)
            self._incomplete_open_bars.discard(stream_key)

        final_last_closed = max(
            (t for t in (*(e.bar.time for e in closed_events), prev_closed) if t is not None),
            default=None,
        )
        if final_last_closed is not None:
            self._last_closed_times[stream_key] = final_last_closed

        return ReconciliationResult(
            contract_id=cid,
            live_mode=eff_live,
            timeframe=tf,
            closed_bars=tuple(closed_events),
            open_bar=open_event,
            last_closed_bar_time=final_last_closed,
        )
