import { describe, expect, it } from "vitest";
import type { OhlcvBar } from "../api/types";
import {
  PaneFetchCoordinator,
  appendBoundedWsBar,
  buildPaneTargetKey,
  matchesWsBarTarget,
  reconcileSnapshotWithBufferedBars,
  type FetchTicket,
  type PaneFetchTarget,
} from "./fetchGuard";

function makeBar(time: number, close: number, isClosed = true): OhlcvBar {
  return {
    time,
    open: close - 1,
    high: close + 1,
    low: close - 2,
    close,
    volume: 10,
    is_closed: isClosed,
  };
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function createDeferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("PaneFetchCoordinator (F06)", () => {
  it("aborts superseded requests and rejects out-of-order 1m/5m responses after 15m selection", async () => {
    /**
     * Contract: Switching 1m -> 5m -> 15m aborts prior AbortControllers and rejects
     * late-resolving 1m/5m responses against current 15m pane state.
     * Defect caught: Stale OHLCV bars overwriting newer timeframe or contract selections.
     */
    const guard = new PaneFetchCoordinator();

    const state1m: PaneFetchTarget = {
      pane_id: "pane-1",
      contract_id: "CON.F.US.MES.H26",
      timeframe: "1m",
      live_mode: false,
      playback_mode: "live",
    };
    const state5m: PaneFetchTarget = { ...state1m, timeframe: "5m" };
    const state15m: PaneFetchTarget = { ...state1m, timeframe: "15m" };

    const d1m = createDeferred<string>();
    const d5m = createDeferred<string>();
    const d15m = createDeferred<string>();

    const ticket1m = guard.beginFetch(state1m);
    expect(ticket1m.signal.aborted).toBe(false);

    const ticket5m = guard.beginFetch(state5m);
    expect(ticket1m.signal.aborted).toBe(true);
    expect(ticket5m.signal.aborted).toBe(false);

    const ticket15m = guard.beginFetch(state15m);
    expect(ticket5m.signal.aborted).toBe(true);
    expect(ticket15m.signal.aborted).toBe(false);
    expect(ticket15m.generation).toBeGreaterThan(ticket5m.generation);

    // Resolve 15m first, then resolve stale 5m and 1m out of order
    d15m.resolve("bars-15m");
    await d15m.promise;
    expect(guard.isResponseCurrent(ticket15m, state15m)).toBe(true);

    d5m.resolve("bars-5m");
    await d5m.promise;
    expect(guard.isResponseCurrent(ticket5m, state15m)).toBe(false);

    d1m.resolve("bars-1m");
    await d1m.promise;
    expect(guard.isResponseCurrent(ticket1m, state15m)).toBe(false);
  });

  it("rejects response if contract_id, live_mode, or playback_mode changes even with matching generation", () => {
    /**
     * Contract: isResponseCurrent enforces full tuple ownership across
     * (pane_id, contract_id, timeframe, live_mode, playback_mode, generation).
     * Defect caught: Mode or contract switch accepting a prior non-aborted ticket.
     */
    const guard = new PaneFetchCoordinator();
    const baseState: PaneFetchTarget = {
      pane_id: "pane-2",
      contract_id: "CON.F.US.MNQ.H26",
      timeframe: "5m",
      live_mode: false,
      playback_mode: "live",
    };

    const ticket = guard.beginFetch(baseState);
    expect(guard.isResponseCurrent(ticket, baseState)).toBe(true);
    expect(
      guard.isResponseCurrent(ticket, { ...baseState, contract_id: "CON.F.US.MGC.J26" }),
    ).toBe(false);
    expect(guard.isResponseCurrent(ticket, { ...baseState, live_mode: true })).toBe(false);
    expect(guard.isResponseCurrent(ticket, { ...baseState, playback_mode: "replay" })).toBe(
      false,
    );

    guard.abortPane("pane-2");
    expect(ticket.signal.aborted).toBe(true);
    expect(guard.isResponseCurrent(ticket, baseState)).toBe(false);
  });

  it("rejects an un-aborted ticket when slot generation has advanced for identical target", () => {
    /**
     * Contract: Even when a ticket's AbortSignal is un-aborted and all target fields match,
     * isResponseCurrent must return false if slot.generation !== ticket.generation.
     * Defect caught: Generation-blind equality check accepting stale responses after resync.
     */
    const guard = new PaneFetchCoordinator();
    const target: PaneFetchTarget = {
      pane_id: "pane-1",
      contract_id: "CON.F.US.MES.H26",
      timeframe: "5m",
      live_mode: false,
      playback_mode: "live",
    };

    const ticket1 = guard.beginFetch(target);
    const ticket2 = guard.beginFetch(target);
    expect(ticket1.generation).toBe(1);
    expect(ticket2.generation).toBe(2);

    const staleUnaborted: FetchTicket = {
      generation: 1,
      signal: new AbortController().signal,
      target: { ...target },
    };
    expect(staleUnaborted.signal.aborted).toBe(false);
    expect(guard.isResponseCurrent(staleUnaborted, target)).toBe(false);
    expect(guard.isResponseCurrent(ticket2, target)).toBe(true);
  });
});

describe("ChartPane dataset isolation and REST/WS reconciliation (P1-01, P1-02, P2-002)", () => {
  it("reconcileSnapshotWithBufferedBars preserves newer WS bars and deduplicates overlapping timestamps", () => {
    /**
     * Contract: Merging REST snapshot bars with buffered WS updates preserves newer WS bars
     * (time >= lastRestTime), overwrites the forming tail bar, sorts ascending, and drops older ticks.
     * Defect caught: In-flight WS ticks lost or duplicated when REST snapshot resolves.
     */
    const restBars: OhlcvBar[] = [
      makeBar(900, 5000, true),
      makeBar(1000, 5005, false),
    ];
    const bufferedBars: OhlcvBar[] = [
      makeBar(500, 4990, true), // older than lastRestTime=1000 -> ignored
      makeBar(1000, 5007, false), // forming tail bar update -> overwrites provisional REST bar at 1000
      makeBar(2000, 5012, false), // newer bar arrived while REST in flight -> appended
      makeBar(2000, 5015, true), // later closed tick on 2000 -> upgrades earlier 2000
    ];

    const merged = reconcileSnapshotWithBufferedBars(restBars, bufferedBars);
    expect(merged.map((b) => b.time)).toEqual([900, 1000, 2000]);
    expect(merged.map((b) => b.time)).toContain(2000);
    expect(merged[1]?.close).toBe(5007);
    expect(merged[2]?.close).toBe(5015);
    expect(merged[2]?.is_closed).toBe(true);
  });

  it("preserves closed REST bars over delayed provisional WS bars while upgrading provisional REST bars (P2-001)", () => {
    /**
     * Contract: Finalized closed REST bars (is_closed=true) must never be overwritten by
     * delayed provisional WS bars (is_closed=false) at the same timestamp, whereas a
     * provisional REST bar (is_closed=false) must upgrade to a closed WS bar (is_closed=true).
     * Defect caught: Closed historical/tail bar regressing to stale provisional WS tick.
     */
    const closedRestKept = reconcileSnapshotWithBufferedBars(
      [makeBar(1000, 108, true)],
      [makeBar(1000, 102, false)],
    );
    expect(closedRestKept).toHaveLength(1);
    expect(closedRestKept[0]?.close).toBe(108);
    expect(closedRestKept[0]?.is_closed).toBe(true);

    const provisionalRestUpgraded = reconcileSnapshotWithBufferedBars(
      [makeBar(1000, 102, false)],
      [makeBar(1000, 108, true)],
    );
    expect(provisionalRestUpgraded).toHaveLength(1);
    expect(provisionalRestUpgraded[0]?.close).toBe(108);
    expect(provisionalRestUpgraded[0]?.is_closed).toBe(true);
  });

  it("reconcileSnapshotWithBufferedBars handles empty REST snapshots without dropping buffered WS bars", () => {
    /**
     * Contract: When REST snapshot returns empty bars, buffered WS bars for the active generation
     * are retained in ascending time order and deduplicated.
     * Defect caught: Empty REST response discarding initial live WS bar stream.
     */
    const merged = reconcileSnapshotWithBufferedBars([], [
      makeBar(2000, 102),
      makeBar(1000, 101),
      makeBar(2000, 103),
    ]);
    expect(merged.map((b) => b.time)).toEqual([1000, 2000]);
    expect(merged[1]?.close).toBe(103);
  });

  it("isolates cross-contract WS bars and tracks target-change clear + bounded buffer per generation", () => {
    /**
     * Contract: Target changes alter buildPaneTargetKey immediately, matchesWsBarTarget rejects
     * prior contract/timeframe/mode messages, and appendBoundedWsBar enforces bounded buffering.
     * Defect caught: Prior contract bars contaminating a switched pane before REST fetch completes.
     */
    const mesTarget: PaneFetchTarget = {
      pane_id: "pane-1",
      contract_id: "CON.F.US.MES.H26",
      timeframe: "5m",
      live_mode: false,
      playback_mode: "live",
    };
    const mnqTarget: PaneFetchTarget = {
      ...mesTarget,
      contract_id: "CON.F.US.MNQ.H26",
    };

    expect(buildPaneTargetKey(mesTarget)).not.toBe(buildPaneTargetKey(mnqTarget));
    expect(
      matchesWsBarTarget(
        { contract_id: "CON.F.US.MES.H26", timeframe: "5m", live_mode: false },
        mnqTarget,
      ),
    ).toBe(false);
    expect(
      matchesWsBarTarget(
        { contract_id: "con.f.us.mnq.h26", timeframe: "5m", live_mode: false },
        mnqTarget,
      ),
    ).toBe(true);
    expect(
      matchesWsBarTarget(
        { contract_id: "CON.F.US.MNQ.H26", timeframe: "5m", live_mode: true },
        mnqTarget,
      ),
    ).toBe(false);

    const buffer: OhlcvBar[] = [];
    expect(appendBoundedWsBar(buffer, makeBar(100, 1), 2)).toEqual({ overflowed: false });
    expect(appendBoundedWsBar(buffer, makeBar(200, 2), 2)).toEqual({ overflowed: false });
    expect(appendBoundedWsBar(buffer, makeBar(300, 3), 2)).toEqual({ overflowed: true });
    expect(buffer.map((b) => b.time)).toEqual([200, 300]);
  });

  it("coalesces intrabar updates by bar.time before capacity eviction during pending fetches", () => {
    /**
     * Contract: Hundreds of intrabar ticks on the same timestamp coalesce in place without
     * consuming capacity or evicting earlier closed bars, preserving [60, 120, 180].
     * Defect caught: Intrabar tick bursts overflowing MAX_PENDING_WS_BARS and dropping closed bars.
     */
    const buffer: OhlcvBar[] = [];
    expect(appendBoundedWsBar(buffer, makeBar(120, 5002))).toEqual({ overflowed: false });

    let anyOverflow = false;
    for (let i = 0; i < 500; i += 1) {
      const res = appendBoundedWsBar(buffer, makeBar(180, 5010 + i));
      if (res.overflowed) {
        anyOverflow = true;
      }
    }

    expect(anyOverflow).toBe(false);
    expect(buffer.length).toBe(2);

    const reconciled = reconcileSnapshotWithBufferedBars([makeBar(60, 5000)], buffer);
    expect(reconciled.map((b) => b.time)).toEqual([60, 120, 180]);
    expect(reconciled[2]?.close).toBe(5509);
  });

  it("signals overflowed=true when distinct timestamps exceed maxBars", () => {
    /**
     * Contract: When distinct buffered bar timestamps exceed maxBars, the oldest bar is evicted
     * and appendBoundedWsBar returns { overflowed: true } so ChartPane can trigger resync.
     * Defect caught: Silent historical bar loss when pending WS queue exceeds capacity.
     */
    const buffer: OhlcvBar[] = [];
    expect(appendBoundedWsBar(buffer, makeBar(100, 1), 3)).toEqual({ overflowed: false });
    expect(appendBoundedWsBar(buffer, makeBar(200, 2), 3)).toEqual({ overflowed: false });
    expect(appendBoundedWsBar(buffer, makeBar(200, 2.5), 3)).toEqual({ overflowed: false });
    expect(appendBoundedWsBar(buffer, makeBar(300, 3), 3)).toEqual({ overflowed: false });
    expect(appendBoundedWsBar(buffer, makeBar(400, 4), 3)).toEqual({ overflowed: true });
    expect(buffer.map((b) => b.time)).toEqual([200, 300, 400]);
    expect(buffer[0]?.close).toBe(2.5);
  });
});
