# System Statecharts (`topstep-chart-app`)

> Critical state machines requiring explicit transitions, invariants, and verification before shipping.

---

## 1. SignalR Market Feed & Reconnect Recovery (`F03`, Phase 2)

```mermaid
stateDiagram-v2
    [*] --> DISCONNECTED: Startup (non-blocking R03)
    DISCONNECTED --> RECOVERING: Auth JWT valid & SignalR connected
    RECOVERING --> LIVE: "Buffer ticks + REST backfill [watermark-2, now] + rebuild indicators OK"
    RECOVERING --> DEGRADED: Backfill timeout or unresolved gap
    DEGRADED --> RECOVERING: Retry recovery window succeeds
    LIVE --> DISCONNECTED: Socket drop / heartbeat timeout / contract rollover
    DEGRADED --> DISCONNECTED: Socket drop
    LIVE --> RECOVERING: JWT refreshed / reconnect triggered
```

**Invariants**:
- Live Telegram Strategy Scanner only emits actionable alerts in `LIVE` state.
- Signals detected while reconciling `RECOVERING` history are tagged `RECOVERED_HISTORICAL` and suppressed from live Telegram alerts.

---

## 2. Durable Telegram Alert Outbox (`F07`, Phase 2)

```mermaid
stateDiagram-v2
    [*] --> PENDING: "Transactional INSERT (unique strategy+version+mode+contract+tf+bar+dir)"
    PENDING --> DELIVERED: Telegram send HTTP 200 before expires_at
    PENDING --> PENDING: "Transient failure & now < expires_at (backoff retry)"
    PENDING --> EXPIRED: "now >= expires_at (staleness TTL elapsed)"
    PENDING --> FAILED: Max retry limit exhausted or permanent 4xx error
    DELIVERED --> [*]
    EXPIRED --> [*]
    FAILED --> [*]
```

**Invariants**:
- Duplicate alert triggers on the same unique tuple hit SQLite `UNIQUE` constraint and no-op cleanly across restarts.
- Stale intraday signals (`now >= expires_at`) transition to `EXPIRED` and are never delivered late.

---

## 3. Isolated Backtest Job Worker (`R02`, Phase 5)

```mermaid
stateDiagram-v2
    [*] --> QUEUED: "POST /api/backtest passes bounds & queue depth <= 3 (202 Accepted)"
    QUEUED --> RUNNING: ProcessPoolExecutor(max_workers=1) acquires worker
    QUEUED --> CANCELLED: "DELETE /api/backtest/{job_id}"
    RUNNING --> COMPLETED: Deterministic result serialized within timeout & payload cap
    RUNNING --> CANCELLED: "DELETE /api/backtest/{job_id} terminates worker & respawns"
    RUNNING --> TIMED_OUT: Wall-clock timeout exceeded (kill process tree & respawn)
    RUNNING --> FAILED: Worker crash or invalid strategy runtime error
    COMPLETED --> [*]
    CANCELLED --> [*]
    TIMED_OUT --> [*]
    FAILED --> [*]
```

**Invariants**:
- FastAPI event loop and SignalR heartbeats are never blocked by backtest CPU execution.

---

## 4. S-L-P Task Delivery & Codex Room Review Gate (`G0–G5`)

```mermaid
stateDiagram-v2
    [*] --> INTAKE_G0: Coordinator writes self-contained Task Packet
    INTAKE_G0 --> BRIEF_GATE_G05: Run brief_gate.py (write-once readiness.json)
    BRIEF_GATE_G05 --> BLOCKED_BRIEF: Missing anchor, overlapping scope, or excluded model
    BRIEF_GATE_G05 --> ACTIVE_PEER_G1: Dispatch Peer via swarm(workspace="share", gemini-4-hydrogen-high)
    ACTIVE_PEER_G1 --> DEBATE: "Peer posts REOPEN_REQUEST / DEPENDENCY_REQUEST"
    DEBATE --> ACTIVE_PEER_G1: Coordinator sends REJECT_WITH_RATIONALE
    DEBATE --> NEEDS_REPLAN: Coordinator accepts amendment (Packet Version + 1)
    NEEDS_REPLAN --> BRIEF_GATE_G05: Re-validate amended packet within replan budget
    NEEDS_REPLAN --> PAUSED_BUDGET: "max_auto_replans (1) or USD budget exhausted"
    ACTIVE_PEER_G1 --> BLOCKED_INFRA: 0-token startup or runtime crash (circuit-breaker bounded)
    ACTIVE_PEER_G1 --> BLOCKED_AUTH: Missing credentials or auth failure
    ACTIVE_PEER_G1 --> VERIFY_G3: "Peer posts CANDIDATE_READY (plus RED->GREEN proof if bugfix)"
    VERIFY_G3 --> ACTIVE_PEER_G1: "build_verifier.py (frozen-src) FAILED (within candidate cap)"
    VERIFY_G3 --> PAUSED_BUDGET: "Candidate cap (3/version or 6/root) or USD budget exhausted"
    VERIFY_G3 --> REVIEW_G4: "build_verifier.py (frozen-src) VERIFIED on exact candidate SHA"
    REVIEW_G4 --> BLOCKED_INFRA: Primary + canary-qualified fallback reviewers unavailable/quota-exhausted
    REVIEW_G4 --> ACTIVE_PEER_G1: Independent Reviewer Pass 1 or Pass 2 finds OPEN blocking issue
    REVIEW_G4 --> ACCEPT_G5: Both passes PASSED (or blocking findings REFUTED_WITH_EVIDENCE)
    ACCEPT_G5 --> VERIFIED: "receipt.py (write-once) exits 0"
    BLOCKED_BRIEF --> [*]
    BLOCKED_INFRA --> [*]
    BLOCKED_AUTH --> [*]
    PAUSED_BUDGET --> [*]
    VERIFIED --> [*]
```

---

## 5. `/ws/market` Client Admission & Authoritative Subscription (`F01, F03, R06`)

```mermaid
stateDiagram-v2
    [*] --> HANDSHAKE: Upgrade /ws/market
    HANDSHAKE --> REJECTED_1008: "validate_ws_handshake fails (Host, Origin, or session cookie)"
    HANDSHAKE --> REJECTED_1013: "len(_clients) >= ws_max_clients (per-process under --workers 1): accept() -> immediate close(1013)"
    HANDSHAKE --> CONNECTED: "register(defer_sender_start=True) -> accept() -> start_sender() -> feed_status"
    HANDSHAKE --> DISCONNECTED: "accept() raises -> finally unregister() releases slot"
    CONNECTED --> PENDING_ADMISSION: "subscribe: check_can_subscribe() passes (no subscription mutation)"
    PENDING_ADMISSION --> SUBSCRIBED: "subscribe_ws_contract(broadcast_status=False) OK & session live -> subscribe() -> subscribed -> emit_status_snapshot()"
    PENDING_ADMISSION --> ERROR: "Validation or subscribe_ws_contract raises -> selective rollback -> redacted error frame"
    PENDING_ADMISSION --> DISCONNECTED: "Client closed or session expired mid-await -> unregister(1008) & sync_ws_contracts()"
    SUBSCRIBED --> CONNECTED: Ready for next message or bar_update stream
    ERROR --> CONNECTED: Connection stays open (non-fatal)
    CONNECTED --> DISCONNECTED: "WebSocketDisconnect / session invalid / slow-consumer 4008 -> unregister() & sync_ws_contracts()"
    REJECTED_1008 --> [*]
    REJECTED_1013 --> [*]
    DISCONNECTED --> [*]
```

**Invariants**:
- Unauthenticated or foreign-`Origin`/`Host` probes always fail `validate_ws_handshake` with `1008` (HTTP `403` pre-`accept()`) **before** `ws_max_clients` slot reservation (`1013`), so probes never consume capacity (`F01`, `NC-13`).
- `ws_max_clients` (default `32`, bounded `1..256`) is enforced atomically per-process (valid under single-worker `--workers 1` `F03`) before allocating `_CoalescingClientQueue` or `ws-sender-*` tasks; authenticated `(N+1)`th connections hitting `WsCapacityExceededError` complete `accept() -> immediate close(1013)` with zero queue/task allocation so real RFC 6455 clients receive close frame `1013` instead of HTTP `403`. `defer_sender_start=True` ensures accepted clients never receive `ws-sender-*` frames before `accept()` completes, and `try / finally` releases the slot if `accept()` fails.
- `subscribe` never mutates `session.subscriptions` or emits `{"type": "subscribed"}` until `subscribe_ws_contract(..., broadcast_status=False)` succeeds; `"subscribed"` confirms local + hub contract registration and always precedes the post-subscribe `"feed_status"` snapshot, while upstream SignalR connection health is conveyed via `"feed_status"` (`NC-12`).
- Any `BaseException` during `subscribe_ws_contract` rolls back only newly added `_ws_contracts` / `_contracts` / pending tick / aggregator state while preserving pre-existing shared or `_explicit_contracts` entries.
