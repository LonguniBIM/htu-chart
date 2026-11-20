"""Value-first tests for F05 ParquetStore locking, atomicity, precedence, and CME resample."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app.engine.instruments import StorageDatasetKey
from app.engine.ohlcv import OHLCVBar
from app.engine.session_calendar import NY_TZ
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import SqliteStore

NOW_TS = int(datetime(2026, 11, 15, 0, 0, tzinfo=UTC).timestamp())


def _bar(
    ts: int,
    o: float,
    h: float,
    low: float,
    c: float,
    v: float,
    *,
    closed: bool = True,
) -> OHLCVBar:
    return OHLCVBar(time=ts, open=o, high=h, low=low, close=c, volume=v, is_closed=closed)


async def test_f05_concurrent_upserts_serialize_without_lost_bars_and_isolate_datasets(
    tmp_path: Path,
) -> None:
    """Contract: 10 concurrent upserts to one dataset lose zero bars and isolate sim/live/expiries.

    Defect caught: Concurrent read-modify-write races losing bars or mixing contracts/modes.
    """
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        store = ParquetStore(tmp_path / "data", sqlite_store=sqlite_store)
        key_z26_sim = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")
        key_z26_live = StorageDatasetKey("topstepx", True, "CON.F.US.MES.Z26", "1m")
        key_h27_sim = StorageDatasetKey("topstepx", False, "CON.F.US.MES.H27", "1m")

        base_t = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
        slices = [
            [
                _bar(base_t + (i * 10 + j) * 60, 5900 + j, 5902 + j, 5899 + j, 5901 + j, 10 + j)
                for j in range(10)
            ]
            for i in range(10)
        ]

        await asyncio.gather(
            *(store.upsert_bars(key_z26_sim, s, root_symbol="MES", now_utc=NOW_TS) for s in slices)
        )
        await store.upsert_bars(
            key_z26_live,
            [_bar(base_t, 6000.0, 6001.0, 5999.0, 6000.5, 5.0)],
            root_symbol="MES",
            now_utc=NOW_TS,
        )
        await store.upsert_bars(
            key_h27_sim,
            [_bar(base_t, 6100.0, 6101.0, 6099.0, 6100.5, 7.0)],
            root_symbol="MES",
            now_utc=NOW_TS,
        )

        sim_bars = await store.read_bars(key_z26_sim)
        assert len(sim_bars) == 100
        assert [b.time for b in sim_bars] == [base_t + k * 60 for k in range(100)]
        assert (await store.read_bars(key_z26_live))[0].open == 6000.0
        assert (await store.read_bars(key_h27_sim))[0].open == 6100.0

        # Verify zero leftover .tmp files anywhere under parquet root
        tmp_files = list((tmp_path / "data" / "parquet").rglob("*.tmp"))
        assert tmp_files == []

        meta = await sqlite_store.get_dataset_metadata(key_z26_sim)
        assert meta is not None
        assert meta["row_count"] == 100
        assert meta["root_symbol"] == "MES"
    finally:
        sqlite_store.close()


async def test_f05_atomic_replace_failure_preserves_original_and_cleans_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Failed os.replace leaves existing Parquet intact and leaves zero orphan .tmp files.

    Defect caught: Partial write corruption or leaked .tmp files on Windows replacement failure.
    """
    store = ParquetStore(tmp_path / "data")
    key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")
    t0 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
    orig_bar = _bar(t0, 5900.0, 5901.0, 5899.0, 5900.5, 25.0)
    await store.upsert_bars(key, [orig_bar], now_utc=NOW_TS)

    def fail_replace(_src: object, _dst: object) -> None:
        raise OSError("Simulated replace failure")

    monkeypatch.setattr("app.storage.parquet_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="Simulated replace failure"):
        await store.upsert_bars(
            key,
            [_bar(t0 + 60, 5901.0, 5902.0, 5900.0, 5901.5, 30.0)],
            now_utc=NOW_TS,
        )

    monkeypatch.undo()
    bars = await store.read_bars(key)
    assert bars == [orig_bar]
    assert list((tmp_path / "data" / "parquet").rglob("*.tmp")) == []


async def test_f05_arrow_metadata_persisted_and_mismatch_raises_value_error(
    tmp_path: Path,
) -> None:
    """Contract: Parquet files embed dataset identity metadata and reject mismatched reads/merges.

    Defect caught: Silently reading or merging a mislabeled contract/mode Parquet file.
    """
    store = ParquetStore(tmp_path / "data")
    key = StorageDatasetKey("topstepx", False, "CON.F.US.MNQ.Z26", "5m")
    t0 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
    await store.upsert_bars(
        key,
        [_bar(t0, 21000.0, 21010.0, 20995.0, 21005.0, 12.0)],
        root_symbol="MNQ",
        now_utc=NOW_TS,
    )

    target = store.resolve_path(key)
    schema_meta = pq.read_schema(target).metadata
    assert schema_meta[b"topstep_provider"] == b"topstepx"
    assert schema_meta[b"topstep_live_mode"] == b"sim"
    assert schema_meta[b"topstep_contract_id"] == b"CON.F.US.MNQ.Z26"
    assert schema_meta[b"topstep_timeframe"] == b"5m"
    assert schema_meta[b"topstep_root_symbol"] == b"MNQ"

    # Overwrite Arrow metadata with wrong contract_id and verify read/upsert fail closed
    table = pq.read_table(target)
    corrupted = table.replace_schema_metadata(
        {
            b"topstep_provider": b"topstepx",
            b"topstep_live_mode": b"sim",
            b"topstep_contract_id": b"CON.F.US.MES.Z26",
            b"topstep_timeframe": b"5m",
            b"topstep_root_symbol": b"MES",
        }
    )
    pq.write_table(corrupted, target, compression="zstd")

    with pytest.raises(ValueError, match="Parquet metadata mismatch"):
        await store.read_bars(key)

    with pytest.raises(ValueError, match="Parquet metadata mismatch"):
        await store.upsert_bars(
            key,
            [_bar(t0 + 300, 21005.0, 21015.0, 21000.0, 21010.0, 8.0)],
            root_symbol="MNQ",
            now_utc=NOW_TS,
        )
    after_table = pq.read_table(target)
    assert after_table.num_rows == 1
    assert after_table.schema.metadata[b"topstep_contract_id"] == b"CON.F.US.MES.Z26"

    # Also test invalid bar rejection inside upsert_bars defense-in-depth
    clean_key = StorageDatasetKey("topstepx", False, "CON.F.US.MYM.Z26", "1m")
    up = await store.upsert_bars(
        clean_key,
        [
            {
                "time": t0,
                "open": 44000.0,
                "high": 43900.0,
                "low": 43990.0,
                "close": 44005.0,
                "volume": 1.0,
                "is_closed": True,
            },
            _bar(t0 + 60, 44005.0, 44015.0, 44000.0, 44010.0, 5.0),
        ],
        now_utc=NOW_TS,
    )
    assert up.accepted_count == 1
    assert len(up.rejected) == 1
    assert up.rejected[0].issue_code == "ohlc_bounds_violation"
    stored_bars = await store.read_bars(clean_key, include_provisional=True)
    assert [b.time for b in stored_bars] == [t0 + 60]


async def test_f05_provisional_bar_precedence_and_read_filtering(tmp_path: Path) -> None:
    """Contract: Closed bars never downgrade to provisional; provisional bars hidden by default.

    Defect caught: Live tick/partial bar overwriting finalized candle or leaking into closed reads.
    """
    store = ParquetStore(tmp_path / "data")
    key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "5m")
    t0 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
    t1 = t0 + 300

    # Provisional t0=100, then updated provisional t0=101
    await store.upsert_bars(
        key,
        [_bar(t0, 5900.0, 5901.0, 5899.0, 5900.0, 10.0, closed=False)],
        now_utc=NOW_TS,
    )
    await store.upsert_bars(
        key,
        [_bar(t0, 5900.0, 5903.0, 5899.0, 5902.0, 20.0, closed=False)],
        now_utc=NOW_TS,
    )
    assert await store.read_bars(key, include_provisional=False) == []
    prov = await store.read_bars(key, include_provisional=True)
    assert len(prov) == 1 and prov[0].close == 5902.0 and prov[0].is_closed is False

    # Upgrade provisional t0 to closed, add provisional t1
    await store.upsert_bars(
        key,
        [
            _bar(t0, 5900.0, 5904.0, 5898.0, 5903.5, 35.0, closed=True),
            _bar(t1, 5903.5, 5905.0, 5903.0, 5904.0, 5.0, closed=False),
        ],
        now_utc=NOW_TS,
    )

    # Attempt to downgrade t0 back to provisional -> must be ignored
    await store.upsert_bars(
        key,
        [_bar(t0, 5900.0, 5900.0, 5900.0, 5900.0, 1.0, closed=False)],
        now_utc=NOW_TS,
    )

    closed_only = await store.read_bars(key, include_provisional=False)
    assert len(closed_only) == 1
    assert closed_only[0].close == 5903.5
    assert closed_only[0].is_closed is True

    with_prov = await store.read_bars(key, include_provisional=True)
    assert [(b.time, b.is_closed) for b in with_prov] == [(t0, True), (t1, False)]


async def test_f05_cme_globex_resample_across_est_edt_dst_and_provisional_buckets(
    tmp_path: Path,
) -> None:
    """Contract: Polars resample aligns 1h/4h/1d to 18:00 NY_TZ across DST and validates paths.

    Defect caught: Naive 00:00Z daily aggregation, wrong OHLC aggregation, or open bucket leakage.
    """
    store = ParquetStore(tmp_path / "data")
    key_1m = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")

    # Build 1m bars spanning 2026-11-01 Fall Back (18:00 EDT = 22:00Z; 18:00 EST = 23:00Z)
    # Day 1 (Nov 2 trading date, opens Sun Nov 1 18:00 EST = 23:00Z)
    sun_open_est = int(datetime(2026, 11, 1, 18, 0, tzinfo=NY_TZ).timestamp())  # 23:00Z
    mon_0000z = int(datetime(2026, 11, 2, 0, 0, tzinfo=UTC).timestamp())  # 19:00 EST
    mon_0100z = int(datetime(2026, 11, 2, 1, 0, tzinfo=UTC).timestamp())  # 20:00 EST

    # Earlier EDT session bar before 18:00 EST (Thu/Fri session)
    fri_open_edt = int(datetime(2026, 10, 29, 18, 0, tzinfo=NY_TZ).timestamp())  # 22:00Z
    fri_0030z = int(datetime(2026, 10, 30, 0, 30, tzinfo=UTC).timestamp())  # 20:30 EDT

    # Spring Forward 2026-03-08 18:00 EDT (22:00Z)
    spring_open_edt = int(datetime(2026, 3, 8, 18, 0, tzinfo=NY_TZ).timestamp())

    bars = [
        _bar(fri_0030z, 5800.0, 5805.0, 5798.0, 5802.0, 100.0),
        _bar(spring_open_edt, 5700.0, 5702.0, 5699.0, 5701.0, 50.0),
        _bar(sun_open_est, 5900.0, 5904.0, 5899.0, 5902.0, 10.0),
        _bar(sun_open_est + 60, 5902.0, 5908.0, 5901.0, 5906.0, 15.0),
        _bar(mon_0000z, 5906.0, 5912.0, 5905.0, 5910.0, 20.0),
        _bar(mon_0100z, 5910.0, 5911.0, 5895.0, 5897.0, 25.0),
    ]
    await store.upsert_bars(key_1m, bars, root_symbol="MES", now_utc=NOW_TS)

    # 1m -> 1d must group 23:00Z + 00:00Z + 01:00Z into sun_open_est (18:00 EST), NOT 00:00Z
    daily = await store.resample(key_1m, "1d", now_utc=NOW_TS)
    assert [b.time for b in daily] == [spring_open_edt, fri_open_edt, sun_open_est]
    nov2_bar = daily[2]
    assert nov2_bar.open == 5900.0
    assert nov2_bar.high == 5912.0
    assert nov2_bar.low == 5895.0
    assert nov2_bar.close == 5897.0
    assert nov2_bar.volume == 70.0
    assert nov2_bar.is_closed is True

    # 1m -> 5m at sun_open_est aggregates first 2 bars into 5m bar at sun_open_est
    m5 = await store.resample(
        key_1m,
        "5m",
        start_ts=sun_open_est,
        end_ts=sun_open_est + 240,
        now_utc=NOW_TS,
    )
    assert len(m5) == 1
    assert m5[0].open == 5900.0 and m5[0].high == 5908.0
    assert m5[0].low == 5899.0 and m5[0].close == 5906.0
    assert m5[0].volume == 25.0

    # 1m -> 4h puts sun_open_est..mon_0100z into sun_open_est (18:00-22:00 EST) with full OHLCV
    h4 = await store.resample(key_1m, "4h", start_ts=sun_open_est, end_ts=mon_0100z, now_utc=NOW_TS)
    assert len(h4) == 1
    assert h4[0] == _bar(sun_open_est, 5900.0, 5912.0, 5895.0, 5897.0, 70.0, closed=True)

    # 1m -> 1h splits 23:00Z, 00:00Z, 01:00Z into 3 hourly bars; verify full OHLCV & provisional
    mid_1h_now = mon_0100z + 120
    h1_closed = await store.resample(
        key_1m,
        "1h",
        start_ts=sun_open_est,
        end_ts=mon_0100z,
        include_provisional=False,
        now_utc=mid_1h_now,
    )
    assert h1_closed == [
        _bar(sun_open_est, 5900.0, 5908.0, 5899.0, 5906.0, 25.0, closed=True),
        _bar(mon_0000z, 5906.0, 5912.0, 5905.0, 5910.0, 20.0, closed=True),
    ]

    h1_prov = await store.resample(
        key_1m,
        "1h",
        start_ts=sun_open_est,
        end_ts=mon_0100z,
        include_provisional=True,
        now_utc=mid_1h_now,
    )
    assert h1_prov == [
        _bar(sun_open_est, 5900.0, 5908.0, 5899.0, 5906.0, 25.0, closed=True),
        _bar(mon_0000z, 5906.0, 5912.0, 5905.0, 5910.0, 20.0, closed=True),
        _bar(mon_0100z, 5910.0, 5911.0, 5895.0, 5897.0, 25.0, closed=False),
    ]

    # Boundary-slice resample test: mid-bucket start_ts=14:02..14:04 matches full 14:00..14:04
    b_1400 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
    slice_bars = [
        _bar(b_1400, 6000.0, 6002.0, 5999.0, 6001.0, 10.0),
        _bar(b_1400 + 60, 6001.0, 6003.0, 6000.0, 6002.0, 12.0),
        _bar(b_1400 + 120, 6002.0, 6005.0, 6001.5, 6004.0, 14.0),
        _bar(b_1400 + 180, 6004.0, 6004.5, 5998.0, 5999.5, 16.0),
        _bar(b_1400 + 240, 5999.5, 6006.0, 5999.0, 6005.5, 18.0),
    ]
    await store.upsert_bars(key_1m, slice_bars, root_symbol="MES", now_utc=NOW_TS)
    full_5m = await store.resample(
        key_1m, "5m", start_ts=b_1400, end_ts=b_1400 + 240, now_utc=NOW_TS
    )
    mid_slice_5m = await store.resample(
        key_1m, "5m", start_ts=b_1400 + 120, end_ts=b_1400 + 240, now_utc=NOW_TS
    )
    assert len(full_5m) == 1
    assert mid_slice_5m == full_5m
    assert mid_slice_5m[0] == _bar(b_1400, 6000.0, 6006.0, 5998.0, 6005.5, 70.0, closed=True)

    # Open 1m input bar inside a 5m bucket keeps resampled bar provisional even past bucket close
    b_1405 = b_1400 + 300
    await store.upsert_bars(
        key_1m,
        [
            _bar(b_1405, 6005.5, 6007.0, 6004.0, 6006.0, 10.0, closed=True),
            _bar(b_1405 + 60, 6006.0, 6008.0, 6005.0, 6007.0, 12.0, closed=False),
        ],
        root_symbol="MES",
        now_utc=NOW_TS,
    )
    assert (
        await store.resample(
            key_1m,
            "5m",
            start_ts=b_1405,
            end_ts=b_1405 + 240,
            include_provisional=False,
            now_utc=NOW_TS,
        )
        == []
    )
    prov_5m = await store.resample(
        key_1m,
        "5m",
        start_ts=b_1405,
        end_ts=b_1405 + 240,
        include_provisional=True,
        now_utc=NOW_TS,
    )
    assert prov_5m == [_bar(b_1405, 6005.5, 6008.0, 6004.0, 6007.0, 22.0, closed=False)]

    # Invalid coarsening paths rejected
    key_15m = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "15m")
    key_3m = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "3m")
    key_1d = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1d")
    with pytest.raises(ValueError):
        await store.resample(key_15m, "5m")
    with pytest.raises(ValueError):
        await store.resample(key_3m, "5m")
    with pytest.raises(ValueError):
        await store.resample(key_1d, "4h")


async def test_f05_cancellation_keeps_lock_held_until_worker_exits_and_serializes_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Cancelling an in-flight upsert does not release lock before worker thread finishes.

    Defect caught: asyncio.CancelledError releasing asyncio.Lock early and overlapping writes/reads.
    """
    store = ParquetStore(tmp_path / "data")
    key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")
    t0 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())

    worker1_entered = threading.Event()
    release_worker1 = threading.Event()
    active_workers = 0
    peak_workers = 0
    count_guard = threading.Lock()
    orig_upsert_sync = store._upsert_sync

    def guarded_upsert_sync(
        target: Path,
        k: StorageDatasetKey,
        valid_bars: list[OHLCVBar],
        root_symbol: str | None,
    ) -> tuple[int, int | None, int | None, int | None, str | None]:
        nonlocal active_workers, peak_workers
        with count_guard:
            active_workers += 1
            peak_workers = max(peak_workers, active_workers)
        try:
            if valid_bars and valid_bars[0].time == t0:
                worker1_entered.set()
                release_worker1.wait(timeout=5.0)
            return orig_upsert_sync(target, k, valid_bars, root_symbol)
        finally:
            with count_guard:
                active_workers -= 1

    monkeypatch.setattr(store, "_upsert_sync", guarded_upsert_sync)

    task1 = asyncio.create_task(
        store.upsert_bars(
            key,
            [_bar(t0, 5900.0, 5901.0, 5899.0, 5900.5, 10.0)],
            root_symbol="MES",
            now_utc=NOW_TS,
        )
    )
    await asyncio.to_thread(worker1_entered.wait, 2.0)

    # Cancel task1 while its worker thread is still blocked
    task1.cancel()
    await asyncio.sleep(0.05)

    # Launch concurrent upsert + read on the SAME key; neither may run until worker1 exits
    task2 = asyncio.create_task(
        store.upsert_bars(
            key,
            [_bar(t0 + 60, 5900.5, 5902.0, 5900.0, 5901.5, 20.0)],
            root_symbol="MES",
            now_utc=NOW_TS,
        )
    )
    task3 = asyncio.create_task(store.read_bars(key))
    await asyncio.sleep(0.05)
    assert task2.done() is False
    assert task3.done() is False
    assert peak_workers == 1

    # Unblock worker1; task1 cancels cleanly and task2/task3 complete with both bars intact
    release_worker1.set()
    with pytest.raises(asyncio.CancelledError):
        await task1
    await task2
    bars = await task3
    assert peak_workers == 1
    assert [b.time for b in bars] == [t0, t0 + 60]


async def test_f05_case_alias_keys_unify_lock_and_storage_identity(tmp_path: Path) -> None:
    """Contract: Mixed-case StorageDatasetKey aliases share canonical identity and lock on Windows.

    Defect caught: CON.F.US.MES.Z26 vs con.f.us.mes.z26 bypassing locks on case-insensitive NTFS.
    """
    store = ParquetStore(tmp_path / "data")
    upper_key = StorageDatasetKey("TOPSTEPX", False, "CON.F.US.MES.Z26", "1M")
    lower_key = StorageDatasetKey("topstepx", False, "con.f.us.mes.z26", "1m")
    assert upper_key == lower_key
    assert upper_key.lock_key() == lower_key.lock_key()

    t0 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
    slice_a = [
        _bar(t0 + i * 60, 5900.0 + i, 5902.0 + i, 5899.0 + i, 5901.0 + i, 10.0)
        for i in range(5)
    ]
    slice_b = [
        _bar(t0 + (5 + i) * 60, 5905.0 + i, 5907.0 + i, 5904.0 + i, 5906.0 + i, 12.0)
        for i in range(5)
    ]

    await asyncio.gather(
        store.upsert_bars(upper_key, slice_a, root_symbol="MES", now_utc=NOW_TS),
        store.upsert_bars(lower_key, slice_b, root_symbol="mes", now_utc=NOW_TS),
    )

    merged = await store.read_bars(upper_key)
    assert [b.time for b in merged] == [t0 + i * 60 for i in range(10)]
    meta = pq.read_schema(store.resolve_path(lower_key)).metadata
    assert meta[b"topstep_provider"] == b"topstepx"
    assert meta[b"topstep_contract_id"] == b"CON.F.US.MES.Z26"
    assert meta[b"topstep_timeframe"] == b"1m"
    assert meta[b"topstep_root_symbol"] == b"MES"


async def test_f05_post_resample_volume_overflow_is_omitted_and_quarantined(
    tmp_path: Path,
) -> None:
    """Contract: Post-resample Float64 overflow to +inf is omitted and emitted as ResampleIssue.

    Defect caught: Two 1e308 1m volumes summing to inf causing JSONResponse 500s on resample.
    """
    store = ParquetStore(tmp_path / "data")
    key_1m = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")
    b_1400 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
    b_1405 = b_1400 + 300

    await store.upsert_bars(
        key_1m,
        [
            _bar(b_1400, 5900.0, 5902.0, 5899.0, 5901.0, 1e308),
            _bar(b_1400 + 60, 5901.0, 5903.0, 5900.0, 5902.0, 1e308),
            _bar(b_1405, 5902.0, 5904.0, 5901.0, 5903.0, 25.0),
        ],
        root_symbol="MES",
        now_utc=NOW_TS,
    )

    res = await store.resample_with_issues(
        key_1m, "5m", start_ts=b_1400, end_ts=b_1405, now_utc=NOW_TS
    )
    assert [b.time for b in res.bars] == [b_1405]
    assert res.bars[0] == _bar(b_1405, 5902.0, 5904.0, 5901.0, 5903.0, 25.0, closed=True)
    assert len(res.issues) == 1
    assert res.issues[0].bar_time == b_1400
    assert res.issues[0].issue_code == "non_finite_volume"

    bars_only = await store.resample(
        key_1m, "5m", start_ts=b_1400, end_ts=b_1405, now_utc=NOW_TS
    )
    assert bars_only == res.bars
