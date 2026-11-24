import { describe, expect, it } from "vitest";
import { PaneFetchCoordinator, type PaneFetchTarget } from "./fetchGuard";

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
});
