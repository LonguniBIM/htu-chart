"""Isolated R02 ProcessPoolExecutor(max_workers=1) backtest job manager."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import logging
import multiprocessing
import os
import signal
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow.parquet as pq

from app.config import Settings
from app.engine.backtester import run_backtest
from app.engine.instruments import StorageDatasetKey, get_instrument_spec, validate_root_symbol
from app.engine.ohlcv import OHLCVBar, validate_query_timestamp
from app.engine.session_calendar import normalize_timeframe
from app.engine.strategies import create_strategy
from app.security import redact_secrets
from app.storage.parquet_store import ParquetStore, validate_resample_path
from app.storage.sqlite_store import QualityIssueInput, SqliteStore

logger = logging.getLogger("topstep_chart.backtest_service")

BacktestJobStatus = Literal[
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "CANCELLED",
    "TIMED_OUT",
    "FAILED",
]
_TERMINAL_STATUSES = frozenset({"COMPLETED", "CANCELLED", "TIMED_OUT", "FAILED"})
_ENVELOPE_FLAG = "__backtest_worker_envelope__"


class BacktestServiceError(RuntimeError):
    """Base domain error for BacktestService admission and lifecycle rules."""


class BacktestDisabledError(BacktestServiceError):
    """Raised when BACKTEST_ENABLED=false blocks new backtest submissions."""


class BacktestQueueFullError(BacktestServiceError):
    """Raised when queued job count reaches backtest_max_queue_depth."""


class BacktestJobNotFoundError(BacktestServiceError):
    """Raised when a requested job_id does not exist."""


class BacktestJobNotCancellableError(BacktestServiceError):
    """Raised when DELETE targets a non-cancellable terminal job (COMPLETED/FAILED/TIMED_OUT)."""


def _init_worker_process() -> None:
    """Place POSIX worker processes into a dedicated session/group for tree cleanup."""
    if os.name != "nt" and hasattr(os, "setsid"):
        with contextlib.suppress(OSError):
            os.setsid()


def _measure_result_bytes(result_dict: dict[str, Any]) -> int:
    encoded = json.dumps(result_dict, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return len(encoded)


def execute_backtest_in_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Top-level picklable worker entrypoint executed inside an isolated subprocess.

    Receives strictly primitive OHLCV tuples, instrument/timeframe metadata, and validated
    strategy parameters (zero secrets, locks, sockets, or database handles).
    """
    raw_bars = payload["bars"]
    bars = [
        OHLCVBar(
            time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            is_closed=bool(row[6]),
        )
        for row in raw_bars
    ]
    result = run_backtest(
        bars,
        contract_id=str(payload["contract_id"]),
        root_symbol=str(payload["root_symbol"]),
        timeframe=str(payload["timeframe"]),
        live_mode=bool(payload["live_mode"]),
        strategy_id=str(payload["strategy_id"]),
        params=dict(payload.get("params") or {}),
        include_provisional=bool(payload.get("include_provisional", False)),
    )
    result_dict = result.to_dict()
    result_bytes = _measure_result_bytes(result_dict)
    max_result_bytes = payload.get("max_result_bytes")
    if max_result_bytes is not None and result_bytes > int(max_result_bytes):
        raise ValueError(
            f"Backtest result size ({result_bytes} bytes) exceeds cap "
            f"({int(max_result_bytes)} bytes)"
        )
    return {
        _ENVELOPE_FLAG: True,
        "result": result_dict,
        "result_bytes": result_bytes,
    }


def _read_parquet_stored_root(target: Path) -> str | None:
    if not target.is_file():
        return None
    schema = pq.read_schema(target)
    meta = schema.metadata or {}
    raw_root = meta.get(b"topstep_root_symbol")
    if not raw_root:
        return None
    decoded = raw_root.decode("utf-8", errors="replace").strip()
    return decoded or None


def _win32_descendant_pids(root_pid: int) -> list[int]:
    """Collect descendant PIDs of `root_pid` using Win32 Toolhelp32 without spawning shells."""
    from ctypes import wintypes

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    invalid_handle = ctypes.c_void_p(-1).value
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snap or snap == invalid_handle:
        return []

    children_map: dict[int, list[int]] = {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        if k32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                pid = int(entry.th32ProcessID)
                ppid = int(entry.th32ParentProcessID)
                children_map.setdefault(ppid, []).append(pid)
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    finally:
        k32.CloseHandle(snap)

    descendants: list[int] = []
    queue: deque[int] = deque([root_pid])
    seen: set[int] = {root_pid}
    while queue:
        cur = queue.popleft()
        for child_pid in children_map.get(cur, ()):
            if child_pid not in seen:
                seen.add(child_pid)
                descendants.append(child_pid)
                queue.append(child_pid)
    return descendants


def _win32_terminate_pid(pid: int) -> None:
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    handle = k32.OpenProcess(0x0001, False, int(pid))  # PROCESS_TERMINATE
    if not handle:
        return
    try:
        k32.TerminateProcess(handle, 1)
    finally:
        k32.CloseHandle(handle)


def _posix_descendant_pids(root_pid: int) -> list[int]:
    """Collect descendant PIDs via /proc on Linux without spawning external shells."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    children_map: dict[int, list[int]] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        with contextlib.suppress(OSError, ValueError):
            status_text = (entry / "status").read_text(encoding="utf-8", errors="replace")
            for line in status_text.splitlines():
                if line.startswith("PPid:"):
                    ppid = int(line.split(":", 1)[1].strip())
                    children_map.setdefault(ppid, []).append(pid)
                    break
    descendants: list[int] = []
    queue: deque[int] = deque([root_pid])
    seen: set[int] = {root_pid}
    while queue:
        cur = queue.popleft()
        for child_pid in children_map.get(cur, ()):
            if child_pid not in seen:
                seen.add(child_pid)
                descendants.append(child_pid)
                queue.append(child_pid)
    return descendants


def _terminate_pid_tree(pid: int) -> None:
    """Forcefully terminate `pid`, its process group, and any descendant processes."""
    if os.name == "nt":
        descendants = _win32_descendant_pids(pid)
        for child_pid in reversed(descendants):
            _win32_terminate_pid(child_pid)
        _win32_terminate_pid(pid)
        return

    descendants = _posix_descendant_pids(pid)
    if hasattr(os, "killpg"):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGKILL)
    for child_pid in reversed(descendants):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(child_pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGKILL)


@dataclass
class BacktestJobRecord:
    """In-memory state for a single backtest job following docs/statecharts.md §3."""

    job_id: str
    seq: int
    status: BacktestJobStatus
    contract_id: str
    root_symbol: str
    timeframe: str
    resample_from: str | None
    live_mode: bool
    start_time: int
    end_time: int
    strategy_id: str
    params: dict[str, Any]
    max_bars: int
    created_at_utc: int
    started_at_utc: int | None = None
    finished_at_utc: int | None = None
    error: str | None = None
    result_bytes: int | None = None
    result: dict[str, Any] | None = field(default=None, repr=False)

    def to_summary_dict(self) -> dict[str, Any]:
        """Return compact job metadata suitable for list/submit/cancel responses."""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "contract_id": self.contract_id,
            "root_symbol": self.root_symbol,
            "timeframe": self.timeframe,
            "resample_from": self.resample_from,
            "live_mode": self.live_mode,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "strategy_id": self.strategy_id,
            "params": dict(self.params),
            "max_bars": self.max_bars,
            "created_at_utc": self.created_at_utc,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "error": self.error,
            "result_bytes": self.result_bytes,
            "summary": dict(self.result["summary"]) if self.result is not None else None,
        }

    def to_detail_dict(self) -> dict[str, Any]:
        """Return full job status including backtest result payload when completed."""
        payload = self.to_summary_dict()
        payload["result"] = self.result
        return payload


class BacktestService:
    """Bounded ProcessPoolExecutor(max_workers=1) service managing backtest jobs (R02)."""

    def __init__(
        self,
        settings: Settings,
        parquet_store: ParquetStore,
        *,
        sqlite_store: SqliteStore | None = None,
        clock: Callable[[], int] | None = None,
        worker_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._settings = settings
        self._parquet_store = parquet_store
        self._sqlite_store = sqlite_store
        self._clock = clock or (lambda: int(datetime.now(UTC).timestamp()))
        self._worker_fn = worker_fn or execute_backtest_in_worker
        self._mp_context = multiprocessing.get_context("spawn")
        self._executor: ProcessPoolExecutor | None = None
        self._cleanup_lock = asyncio.Lock()

        self._jobs: dict[str, BacktestJobRecord] = {}
        self._queue: deque[str] = deque()
        self._seq = 0
        self._running_job_id: str | None = None
        self._active_exec_task: asyncio.Task[None] | None = None
        self._orphaned_load_tasks: set[asyncio.Task[Any]] = set()
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self._running = False

    def _track_orphaned_load_task(self, task: asyncio.Task[Any]) -> None:
        self._orphaned_load_tasks.add(task)

        def _on_done(done_task: asyncio.Task[Any]) -> None:
            self._orphaned_load_tasks.discard(done_task)
            if not done_task.cancelled():
                with contextlib.suppress(BaseException):
                    _ = done_task.exception()

        task.add_done_callback(_on_done)

    def _redaction_secrets(self) -> tuple[str, ...]:
        return (
            self._settings.topstepx_api_key,
            self._settings.app_password_hash,
            self._settings.app_session_secret,
            self._settings.telegram_bot_token,
            self._settings.telegram_chat_id,
        )

    def _redact(self, text: str) -> str:
        return redact_secrets(text, self._redaction_secrets())

    def _ensure_executor(self) -> ProcessPoolExecutor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                max_workers=1,
                mp_context=self._mp_context,
                initializer=_init_worker_process,
            )
        return self._executor

    def _terminate_and_reset_executor_sync(self) -> None:
        executor = self._executor
        if executor is None:
            return
        try:
            procs = list(getattr(executor, "_processes", {}).values())
            for proc in procs:
                pid = getattr(proc, "pid", None)
                if pid is not None:
                    _terminate_pid_tree(int(pid))
                with contextlib.suppress(Exception):
                    proc.kill()
            with contextlib.suppress(Exception):
                executor.shutdown(wait=False, cancel_futures=True)
            for proc in procs:
                with contextlib.suppress(Exception):
                    proc.join(timeout=1.0)
        finally:
            if self._executor is executor:
                self._executor = None

    async def _terminate_and_reset_executor(
        self,
        *,
        target_executor: ProcessPoolExecutor | None = None,
    ) -> None:
        async with self._cleanup_lock:
            if target_executor is not None and self._executor is not target_executor:
                return
            if self._executor is None:
                return
            await asyncio.to_thread(self._terminate_and_reset_executor_sync)

    def _queued_count(self) -> int:
        return sum(
            1
            for jid in self._queue
            if self._jobs.get(jid) and self._jobs[jid].status == "QUEUED"
        )

    def _prune_terminal_jobs(self) -> None:
        limit = self._settings.backtest_max_jobs_retained
        if len(self._jobs) <= limit:
            return
        terminal = sorted(
            (job for job in self._jobs.values() if job.status in _TERMINAL_STATUSES),
            key=lambda j: j.seq,
        )
        excess = len(self._jobs) - limit
        for job in terminal[:excess]:
            self._jobs.pop(job.job_id, None)

    async def start(self) -> None:
        """Start the single-worker job dispatcher non-blockingly."""
        if self._running:
            return
        self._running = True
        self._wakeup.clear()
        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._dispatcher_task = asyncio.create_task(
                self._dispatcher_loop(),
                name="backtest-dispatcher",
            )

    async def stop(self) -> None:
        """Stop dispatcher, cancel running job, and clean up subprocess worker cleanly."""
        self._running = False
        self._wakeup.set()

        active_task = self._active_exec_task
        if active_task is not None and not active_task.done():
            if self._running_job_id and self._running_job_id in self._jobs:
                job = self._jobs[self._running_job_id]
                if job.status == "RUNNING":
                    job.status = "CANCELLED"
                    job.finished_at_utc = self._clock()
                    job.error = "Cancelled on service shutdown"
            async with self._cleanup_lock:
                if self._executor is not None:
                    await asyncio.to_thread(self._terminate_and_reset_executor_sync)
                active_task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(active_task, timeout=2.0)

        if self._dispatcher_task is not None and not self._dispatcher_task.done():
            self._dispatcher_task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._dispatcher_task, timeout=2.0)

        orphaned = [t for t in self._orphaned_load_tasks if not t.done()]
        for task in orphaned:
            task.cancel()
        if orphaned:
            with contextlib.suppress(Exception):
                await asyncio.wait(orphaned, timeout=1.0)

        self._dispatcher_task = None
        self._active_exec_task = None
        self._running_job_id = None

        async with self._cleanup_lock:
            if self._executor is not None:
                executor = self._executor
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True),
                        timeout=3.0,
                    )
                    if self._executor is executor:
                        self._executor = None
                except (TimeoutError, Exception):
                    await asyncio.to_thread(self._terminate_and_reset_executor_sync)

    def submit_job(
        self,
        *,
        contract_id: str,
        timeframe: str,
        start_time: int,
        end_time: int,
        root_symbol: str | None = None,
        live_mode: bool = False,
        strategy_id: str = "ict_fvg_sweep",
        params: Mapping[str, Any] | None = None,
        resample_from: str | None = None,
        max_bars: int | None = None,
    ) -> BacktestJobRecord:
        """Validate bounds/admission and enqueue a backtest job into the bounded queue."""
        if not self._settings.backtest_enabled:
            raise BacktestDisabledError(
                "Backtest execution is disabled by configuration (BACKTEST_ENABLED=false)"
            )

        if not isinstance(contract_id, str) or not contract_id.strip():
            raise ValueError("contract_id is required")
        clean_contract = contract_id.strip().upper()
        if isinstance(live_mode, bool) is False:
            raise ValueError("live_mode must be a boolean")

        eff_tf = normalize_timeframe(timeframe)
        eff_resample_from: str | None = None
        if resample_from is not None and str(resample_from).strip():
            eff_resample_from, eff_tf = validate_resample_path(str(resample_from), eff_tf)

        resolved_root = validate_root_symbol(clean_contract, root_symbol)
        if resolved_root is None:
            raise ValueError(
                f"Cannot infer CME root_symbol from contract_id {clean_contract!r}; "
                "provide root_symbol explicitly"
            )
        get_instrument_spec(resolved_root)

        eff_start = validate_query_timestamp(start_time, "start_time")
        eff_end = validate_query_timestamp(end_time, "end_time")
        if eff_start >= eff_end:
            raise ValueError(f"start_time ({eff_start}) must be < end_time ({eff_end})")
        max_span_sec = self._settings.backtest_max_date_range_days * 86400
        if (eff_end - eff_start) > max_span_sec:
            raise ValueError(
                f"Requested date range exceeds max allowed "
                f"{self._settings.backtest_max_date_range_days} days"
            )

        if max_bars is None:
            eff_max_bars = self._settings.backtest_max_bars
        else:
            if isinstance(max_bars, bool) or not isinstance(max_bars, int):
                raise ValueError("max_bars must be an integer")
            if max_bars < 1 or max_bars > self._settings.backtest_max_bars:
                raise ValueError(
                    f"max_bars ({max_bars}) must be within [1, {self._settings.backtest_max_bars}]"
                )
            eff_max_bars = max_bars

        strategy = create_strategy(strategy_id, params)

        if self._queued_count() >= self._settings.backtest_max_queue_depth:
            max_depth = self._settings.backtest_max_queue_depth
            raise BacktestQueueFullError(
                f"Backtest queue is full (max_queue_depth={max_depth})"
            )

        # Validate StorageDatasetKey early to reject path traversal before queuing.
        source_tf = eff_resample_from or eff_tf
        StorageDatasetKey("topstepx", live_mode, clean_contract, source_tf)

        self._seq += 1
        job = BacktestJobRecord(
            job_id=uuid.uuid4().hex,
            seq=self._seq,
            status="QUEUED",
            contract_id=clean_contract,
            root_symbol=resolved_root,
            timeframe=eff_tf,
            resample_from=eff_resample_from,
            live_mode=live_mode,
            start_time=eff_start,
            end_time=eff_end,
            strategy_id=strategy.strategy_id,
            params=dict(strategy.params),
            max_bars=eff_max_bars,
            created_at_utc=self._clock(),
        )
        self._jobs[job.job_id] = job
        self._queue.append(job.job_id)
        self._prune_terminal_jobs()
        self._wakeup.set()
        return job

    def get_job(self, job_id: str) -> BacktestJobRecord:
        """Fetch job record by ID or raise BacktestJobNotFoundError."""
        job = self._jobs.get(job_id)
        if job is None:
            raise BacktestJobNotFoundError(f"Backtest job {job_id!r} not found")
        return job

    def list_jobs(
        self,
        *,
        status: BacktestJobStatus | None = None,
        limit: int = 50,
    ) -> list[BacktestJobRecord]:
        """List recent backtest jobs newest-first, optionally filtered by status."""
        items = sorted(self._jobs.values(), key=lambda j: j.seq, reverse=True)
        if status is not None:
            items = [j for j in items if j.status == status]
        return items[:limit]

    async def cancel_job(self, job_id: str) -> BacktestJobRecord:
        """Cancel a QUEUED or RUNNING job (idempotent on CANCELLED; 409 on other terminal)."""
        job = self.get_job(job_id)
        if job.status == "CANCELLED":
            return job
        if job.status in _TERMINAL_STATUSES:
            raise BacktestJobNotCancellableError(
                f"Cannot cancel backtest job in terminal state {job.status}"
            )

        if job.status == "QUEUED":
            job.status = "CANCELLED"
            job.finished_at_utc = self._clock()
            job.error = "Cancelled by caller"
            with contextlib.suppress(ValueError):
                self._queue.remove(job.job_id)
            self._prune_terminal_jobs()
            return job

        # Running job: transition first and hold _cleanup_lock until worker termination completes
        # so the dispatcher cannot start the next queued job concurrently.
        job.status = "CANCELLED"
        job.finished_at_utc = self._clock()
        job.error = "Cancelled by caller"
        active_task = self._active_exec_task
        async with self._cleanup_lock:
            if self._executor is not None:
                await asyncio.to_thread(self._terminate_and_reset_executor_sync)
            if active_task is not None and not active_task.done():
                active_task.cancel()
        self._prune_terminal_jobs()
        return job

    def get_status_snapshot(self) -> dict[str, Any]:
        """Return redacted backtest worker telemetry for GET /api/status."""
        return {
            "enabled": bool(self._settings.backtest_enabled),
            "running_job_id": self._running_job_id,
            "queue_depth": self._queued_count(),
            "max_queue_depth": self._settings.backtest_max_queue_depth,
        }

    async def _load_closed_bars(self, job: BacktestJobRecord) -> list[OHLCVBar]:
        if job.resample_from is not None:
            source_key = StorageDatasetKey(
                "topstepx",
                job.live_mode,
                job.contract_id,
                job.resample_from,
            )
            resample_res = await self._parquet_store.resample_with_issues(
                source_key,
                job.timeframe,
                start_ts=job.start_time,
                end_ts=job.end_time,
                include_provisional=False,
                now_utc=self._clock(),
                root_symbol=job.root_symbol,
                limit=job.max_bars + 1,
            )
            if resample_res.issues:
                target_key = StorageDatasetKey(
                    "topstepx",
                    job.live_mode,
                    job.contract_id,
                    job.timeframe,
                )
                if self._sqlite_store is not None:
                    await self._sqlite_store.record_quality_issues(
                        target_key,
                        [
                            QualityIssueInput(
                                bar_time=issue.bar_time,
                                interval_start=issue.interval_start,
                                interval_end=issue.interval_end,
                                issue_code=issue.issue_code,
                                detail=self._redact(issue.detail),
                                raw_payload=self._redact(issue.raw_payload)[:2048],
                            )
                            for issue in resample_res.issues
                        ],
                        deduplicate=True,
                    )
                    for issue in resample_res.issues:
                        await self._sqlite_store.record_coverage(
                            target_key,
                            issue.interval_start,
                            issue.interval_end,
                            status="CORRUPTED",
                            quarantined_count=1,
                        )
                raise ValueError(
                    f"Resampling {job.resample_from}->{job.timeframe} encountered "
                    f"{len(resample_res.issues)} corrupted bar(s)"
                )
            return resample_res.bars

        dataset_key = StorageDatasetKey(
            "topstepx",
            job.live_mode,
            job.contract_id,
            job.timeframe,
        )
        target_path = self._parquet_store.resolve_path(dataset_key)
        stored_root = await asyncio.to_thread(_read_parquet_stored_root, target_path)
        validate_root_symbol(
            job.contract_id,
            job.root_symbol,
            stored_root_symbol=stored_root,
        )
        return await self._parquet_store.read_bars(
            dataset_key,
            start_ts=job.start_time,
            end_ts=job.end_time,
            include_provisional=False,
            limit=job.max_bars + 1,
        )

    async def _execute_job(self, job: BacktestJobRecord) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(self._settings.backtest_timeout_sec)

        def _remaining_timeout() -> float:
            return max(0.0, deadline - loop.time())

        try:
            load_task = asyncio.create_task(
                self._load_closed_bars(job),
                name=f"backtest-load-{job.job_id[:8]}",
            )
            try:
                done, _ = await asyncio.wait(
                    {load_task},
                    timeout=_remaining_timeout(),
                )
            except asyncio.CancelledError:
                self._track_orphaned_load_task(load_task)
                load_task.cancel()
                raise

            if load_task not in done:
                self._track_orphaned_load_task(load_task)
                load_task.cancel()
                if job.status == "RUNNING":
                    job.status = "TIMED_OUT"
                    job.finished_at_utc = self._clock()
                    job.error = (
                        f"Backtest timed out after {self._settings.backtest_timeout_sec}s"
                    )
                return

            bars = load_task.result()

            if job.status != "RUNNING":
                return

            if len(bars) > job.max_bars:
                job.status = "FAILED"
                job.finished_at_utc = self._clock()
                job.error = (
                    f"Closed bar count exceeds max_bars cap ({job.max_bars}); "
                    "narrow date range or raise max_bars"
                )
                return

            payload: dict[str, Any] = {
                "bars": [
                    (b.time, b.open, b.high, b.low, b.close, b.volume, b.is_closed)
                    for b in bars
                ],
                "contract_id": job.contract_id,
                "root_symbol": job.root_symbol,
                "timeframe": job.timeframe,
                "live_mode": job.live_mode,
                "strategy_id": job.strategy_id,
                "params": dict(job.params),
                "include_provisional": False,
                "max_result_bytes": self._settings.backtest_max_result_bytes,
            }

            executor = self._ensure_executor()
            exec_future: asyncio.Future[dict[str, Any]] | None = None
            try:
                remaining = _remaining_timeout()
                if remaining <= 0.0:
                    raise TimeoutError
                exec_future = loop.run_in_executor(executor, self._worker_fn, payload)
                raw_output = await asyncio.wait_for(
                    exec_future,
                    timeout=remaining,
                )
            except TimeoutError:
                await self._terminate_and_reset_executor(target_executor=executor)
                if exec_future is not None:
                    with contextlib.suppress(BaseException):
                        await exec_future
                if job.status == "RUNNING":
                    job.status = "TIMED_OUT"
                    job.finished_at_utc = self._clock()
                    job.error = (
                        f"Backtest timed out after {self._settings.backtest_timeout_sec}s"
                    )
                return
            except asyncio.CancelledError:
                await self._terminate_and_reset_executor(target_executor=executor)
                if exec_future is not None:
                    with contextlib.suppress(BaseException):
                        await exec_future
                if job.status == "RUNNING":
                    job.status = "CANCELLED"
                    job.finished_at_utc = self._clock()
                    job.error = "Cancelled by caller"
                raise
            except BrokenProcessPool as exc:
                await self._terminate_and_reset_executor(target_executor=executor)
                if job.status == "RUNNING":
                    job.status = "FAILED"
                    job.finished_at_utc = self._clock()
                    job.error = self._redact(f"Backtest worker terminated unexpectedly: {exc}")
                return

            if job.status != "RUNNING":
                return

            if isinstance(raw_output, dict) and raw_output.get(_ENVELOPE_FLAG) is True:
                result_dict = raw_output["result"]
                result_bytes = int(raw_output["result_bytes"])
            else:
                result_dict = raw_output
                result_bytes = await asyncio.to_thread(_measure_result_bytes, result_dict)

            if result_bytes > self._settings.backtest_max_result_bytes:
                job.status = "FAILED"
                job.finished_at_utc = self._clock()
                job.error = (
                    f"Backtest result size ({result_bytes} bytes) exceeds cap "
                    f"({self._settings.backtest_max_result_bytes} bytes)"
                )
                return

            job.result = result_dict
            job.result_bytes = result_bytes
            job.status = "COMPLETED"
            job.finished_at_utc = self._clock()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if job.status == "RUNNING":
                job.status = "FAILED"
                job.finished_at_utc = self._clock()
                job.error = self._redact(str(exc) or type(exc).__name__)
                logger.warning("Backtest job %s failed: %s", job.job_id, job.error)

    async def _dispatcher_loop(self) -> None:
        while self._running:
            next_job: BacktestJobRecord | None = None
            while self._queue:
                cand_id = self._queue.popleft()
                cand = self._jobs.get(cand_id)
                if cand is not None and cand.status == "QUEUED":
                    next_job = cand
                    break

            if next_job is None:
                self._wakeup.clear()
                if not self._running:
                    break
                if self._queue:
                    continue
                await self._wakeup.wait()
                continue

            next_job.status = "RUNNING"
            next_job.started_at_utc = self._clock()
            self._running_job_id = next_job.job_id
            exec_task = asyncio.create_task(
                self._execute_job(next_job),
                name=f"backtest-job-{next_job.job_id[:8]}",
            )
            self._active_exec_task = exec_task
            try:
                await exec_task
            except asyncio.CancelledError:
                if not self._running:
                    raise
            finally:
                async with self._cleanup_lock:
                    if self._active_exec_task is exec_task:
                        self._active_exec_task = None
                    if self._running_job_id == next_job.job_id:
                        self._running_job_id = None
                    self._prune_terminal_jobs()
