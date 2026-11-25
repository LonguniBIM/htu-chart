import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  LIVE_WS_TIMEFRAMES,
  isLiveWsTimeframe,
  type WsResyncRequiredMessage,
  type WsSubscribedMessage,
} from "./types";
import { MarketWsClient } from "./wsClient";

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  readyState = FakeWebSocket.CONNECTING;
  readonly sentFrames: Array<Record<string, unknown>> = [];
  readonly closeCalls: Array<{ code: number | undefined; reason: string | undefined }> = [];
  private readonly listeners = new Map<string, Set<(event: unknown) => void>>();

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: (event: unknown) => void): void {
    let bucket = this.listeners.get(type);
    if (bucket === undefined) {
      bucket = new Set();
      this.listeners.set(type, bucket);
    }
    bucket.add(listener);
  }

  send(data: string): void {
    this.sentFrames.push(JSON.parse(data) as Record<string, unknown>);
  }

  close(code?: number, reason?: string): void {
    this.closeCalls.push({ code, reason });
    this.readyState = FakeWebSocket.CLOSED;
  }

  emitOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.dispatch("open", {});
  }

  emitClose(code: number, reason = ""): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.dispatch("close", { code, reason });
  }

  emitMessage(payload: unknown): void {
    this.dispatch("message", { data: JSON.stringify(payload) });
  }

  emitError(): void {
    this.dispatch("error", {});
  }

  private dispatch(type: string, event: unknown): void {
    const bucket = this.listeners.get(type);
    if (bucket === undefined) {
      return;
    }
    for (const listener of bucket) {
      listener(event);
    }
  }
}

describe("MarketWsClient (P1-04, P1-05, P2-003)", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("ref-counts subscribe/unsubscribe across two panes sharing (contract_id, timeframe)", () => {
    /**
     * Contract: Two panes subscribing to the same (contract_id, timeframe) send only one
     * subscribe frame and only unsubscribe when the final pane releases its handle.
     * Defect caught: Duplicate WS subscribe frames or premature unsubscribe while another pane is active.
     */
    const client = new MarketWsClient(() => "ws://127.0.0.1:8000/ws/market");
    client.connect();
    const ws = FakeWebSocket.instances[0];
    expect(ws).toBeDefined();
    ws?.emitOpen();

    const unsub1 = client.subscribe("con.f.us.mes.h26", "5m");
    const unsub2 = client.subscribe("CON.F.US.MES.H26", "5m");
    expect(
      ws?.sentFrames.filter((frame) => frame.type === "subscribe"),
    ).toEqual([
      {
        type: "subscribe",
        contract_id: "CON.F.US.MES.H26",
        timeframe: "5m",
      },
    ]);

    unsub1();
    unsub1(); // idempotent repeat call
    expect(ws?.sentFrames.filter((frame) => frame.type === "unsubscribe")).toEqual([]);

    unsub2();
    expect(
      ws?.sentFrames.filter((frame) => frame.type === "unsubscribe"),
    ).toEqual([
      {
        type: "unsubscribe",
        contract_id: "CON.F.US.MES.H26",
        timeframe: "5m",
      },
    ]);
  });

  it("ignores stale-socket close/message/error events after disconnect() + connect()", () => {
    /**
     * Contract: A replaced socket emitting close after disconnect() + connect() must never
     * clobber the active socket's connected state, clear its heartbeat timer, or open a second socket.
     * Defect caught: Stale WS close handler clearing active pingTimer and scheduling duplicate reconnects (P1-04).
     */
    const client = new MarketWsClient(() => "ws://127.0.0.1:8000/ws/market");
    client.connect();
    const ws1 = FakeWebSocket.instances[0];
    expect(ws1).toBeDefined();
    ws1?.emitOpen();
    expect(client.getConnectionState()).toBe("connected");

    client.disconnect();
    expect(client.getConnectionState()).toBe("disconnected");

    client.connect();
    const ws2 = FakeWebSocket.instances[1];
    expect(ws2).toBeDefined();
    ws2?.emitOpen();
    expect(client.getConnectionState()).toBe("connected");

    // Stale socket fires late error/message/close events
    ws1?.emitError();
    ws1?.emitMessage({ type: "resync_required", reason: "stale", contract_id: null });
    ws1?.emitClose(1006, "stale_socket_close");

    expect(client.getConnectionState()).toBe("connected");

    // Verify active ping timer on ws2 was NOT cleared and no duplicate reconnect socket was opened
    vi.advanceTimersByTime(20_500);
    expect(ws2?.sentFrames).toContainEqual({ type: "ping" });
    expect(FakeWebSocket.instances.length).toBe(2);
  });

  it("emits resync_required (ws_reconnected) and re-subscribes after unexpected drop and reconnect", () => {
    /**
     * Contract: After an unexpected close (1006), reconnecting emits resync_required with
     * reason='ws_reconnected' and re-sends active subscriptions so panes reload missed bars.
     * Defect caught: Silent bar gaps after WS drop + reconnect (P1-05, P2-003).
     */
    const client = new MarketWsClient(() => "ws://127.0.0.1:8000/ws/market");
    const resyncEvents: WsResyncRequiredMessage[] = [];
    client.onResyncRequired((event) => {
      resyncEvents.push(event);
    });

    client.connect();
    const ws1 = FakeWebSocket.instances[0];
    expect(ws1).toBeDefined();
    client.subscribe("CON.F.US.MNQ.H26", "15m");
    ws1?.emitOpen();
    expect(resyncEvents.length).toBe(0);

    ws1?.emitClose(1006, "network_drop");
    expect(client.getConnectionState()).toBe("disconnected");

    vi.advanceTimersByTime(1_000);
    expect(FakeWebSocket.instances.length).toBe(2);
    const ws2 = FakeWebSocket.instances[1];
    expect(ws2).toBeDefined();
    expect(client.getConnectionState()).toBe("connecting");

    ws2?.emitOpen();
    expect(client.getConnectionState()).toBe("connected");
    expect(resyncEvents.length).toBe(1);
    expect(resyncEvents[0]).toEqual({
      type: "resync_required",
      reason: "ws_reconnected",
      contract_id: null,
    });
    expect(ws2?.sentFrames).toContainEqual({
      type: "subscribe",
      contract_id: "CON.F.US.MNQ.H26",
      timeframe: "15m",
    });
  });

  it("ignores stale open and bar_update/feed_status messages from a replaced socket", () => {
    /**
     * Contract: Late open and message events emitted on a replaced socket are completely ignored
     * by onBarUpdate, onFeedStatus, and onResyncRequired listeners.
     * Defect caught: Missing this.socket !== ws guard leaking stale socket bars or feed_status.
     */
    const client = new MarketWsClient(() => "ws://127.0.0.1:8000/ws/market");
    const barListener = vi.fn();
    const feedStatusListener = vi.fn();
    const resyncListener = vi.fn();
    client.onBarUpdate(barListener);
    client.onFeedStatus(feedStatusListener);
    client.onResyncRequired(resyncListener);

    client.connect();
    const ws1 = FakeWebSocket.instances[0];
    expect(ws1).toBeDefined();
    client.subscribe("CON.F.US.MES.H26", "5m");

    client.disconnect();
    client.connect();
    const ws2 = FakeWebSocket.instances[1];
    expect(ws2).toBeDefined();
    ws2?.emitOpen();

    // Stale socket fires late open and inbound bar_update/feed_status messages
    ws1?.emitOpen();
    ws1?.emitMessage({
      type: "bar_update",
      contract_id: "CON.F.US.MES.H26",
      live_mode: false,
      timeframe: "5m",
      is_recovered_historical: false,
      recovery_tag: null,
      bar: {
        time: 1000,
        open: 5000,
        high: 5002,
        low: 4999,
        close: 5001,
        volume: 15,
        is_closed: true,
      },
    });
    ws1?.emitMessage({ type: "feed_status", feed_state: "DISCONNECTED" });

    expect(barListener).not.toHaveBeenCalled();
    expect(feedStatusListener).not.toHaveBeenCalled();
    expect(resyncListener).not.toHaveBeenCalled();

    // Active socket still delivers bar_update and feed_status cleanly
    ws2?.emitMessage({
      type: "bar_update",
      contract_id: "CON.F.US.MES.H26",
      live_mode: false,
      timeframe: "5m",
      is_recovered_historical: false,
      recovery_tag: null,
      bar: {
        time: 2000,
        open: 5001,
        high: 5003,
        low: 5000,
        close: 5002,
        volume: 20,
        is_closed: false,
      },
    });
    ws2?.emitMessage({ type: "feed_status", feed_state: "LIVE" });

    expect(barListener).toHaveBeenCalledTimes(1);
    expect(feedStatusListener).toHaveBeenCalledTimes(1);
  });

  it("dispatches inbound error frames to onError, ignores stale sockets, and classifies live WS timeframes (P1-01)", () => {
    /**
     * Contract: MarketWsClient.onError delivers { type: "error", detail } frames from the
     * active socket while ignoring stale-socket errors, and isLiveWsTimeframe matches
     * BarAggregator DEFAULT_AGGREGATION_TIMEFRAMES.
     * Defect caught: Silent WS error swallowing and subscribing to unsupported 3m/30m feeds.
     */
    expect(LIVE_WS_TIMEFRAMES).toEqual(["1m", "5m", "15m", "1h", "4h", "1d"]);
    expect(isLiveWsTimeframe("3m")).toBe(false);
    expect(isLiveWsTimeframe("30m")).toBe(false);
    for (const tf of LIVE_WS_TIMEFRAMES) {
      expect(isLiveWsTimeframe(tf)).toBe(true);
    }

    const client = new MarketWsClient(() => "ws://127.0.0.1:8000/ws/market");
    const errors: string[] = [];
    const unsubscribe = client.onError((msg) => {
      errors.push(msg.detail);
    });

    client.connect();
    const ws1 = FakeWebSocket.instances[0];
    expect(ws1).toBeDefined();
    ws1?.emitOpen();

    client.disconnect();
    client.connect();
    const ws2 = FakeWebSocket.instances[1];
    expect(ws2).toBeDefined();
    ws2?.emitOpen();

    // Replaced stale socket error must be ignored
    ws1?.emitMessage({ type: "error", detail: "stale socket error" });
    expect(errors).toEqual([]);

    // Active socket error must reach onError listener
    ws2?.emitMessage({ type: "error", detail: "Timeframe '3m' is not enabled" });
    expect(errors).toEqual(["Timeframe '3m' is not enabled"]);

    unsubscribe();
    ws2?.emitMessage({ type: "error", detail: "after unsubscribe" });
    expect(errors).toEqual(["Timeframe '3m' is not enabled"]);
  });

  it("dispatches inbound subscribed ACKs to onSubscribed and ignores stale-socket frames (P2-001)", () => {
    /**
     * Contract: MarketWsClient.onSubscribed delivers { type: "subscribed", contract_id, timeframe }
     * frames from the active socket while ignoring replaced stale-socket frames and unsubscribed frames.
     * Defect caught: Missing onSubscribed listener or stale-socket subscribed ACK triggering resync.
     */
    const client = new MarketWsClient(() => "ws://127.0.0.1:8000/ws/market");
    const subscribedEvents: WsSubscribedMessage[] = [];
    const unsubscribe = client.onSubscribed((msg) => {
      subscribedEvents.push(msg);
    });

    client.connect();
    const ws1 = FakeWebSocket.instances[0];
    expect(ws1).toBeDefined();
    ws1?.emitOpen();

    client.disconnect();
    client.connect();
    const ws2 = FakeWebSocket.instances[1];
    expect(ws2).toBeDefined();
    ws2?.emitOpen();

    // Replaced stale socket subscribed frame must be ignored
    ws1?.emitMessage({
      type: "subscribed",
      contract_id: "CON.F.US.MES.H26",
      timeframe: "5m",
    });
    expect(subscribedEvents).toEqual([]);

    // Active socket unsubscribed frame must not fire onSubscribed
    ws2?.emitMessage({
      type: "unsubscribed",
      contract_id: "CON.F.US.MES.H26",
      timeframe: "5m",
    });
    expect(subscribedEvents).toEqual([]);

    // Active socket subscribed frame reaches listener
    ws2?.emitMessage({
      type: "subscribed",
      contract_id: "CON.F.US.MES.H26",
      timeframe: "5m",
    });
    expect(subscribedEvents).toEqual([
      {
        type: "subscribed",
        contract_id: "CON.F.US.MES.H26",
        timeframe: "5m",
      },
    ]);

    unsubscribe();
    ws2?.emitMessage({
      type: "subscribed",
      contract_id: "CON.F.US.MNQ.H26",
      timeframe: "15m",
    });
    expect(subscribedEvents).toHaveLength(1);
  });

  it("invokes onSessionExpired and schedules zero reconnects on close 1008 (P1-R12-004)", () => {
    /**
     * Contract: When the active socket closes with WS_CLOSE_POLICY_VIOLATION (1008),
     * MarketWsClient invokes onSessionExpired listeners with event.reason (or "Session expired"),
     * ignores stale-socket 1008 closes, and schedules zero reconnect attempts.
     * Defect caught: WS close 1008 silently disconnecting without notifying App to invalidate session.
     */
    const client = new MarketWsClient(() => "ws://127.0.0.1:8000/ws/market");
    const expiredReasons: string[] = [];
    const unsubscribe = client.onSessionExpired((reason) => {
      expiredReasons.push(reason);
    });

    client.connect();
    const ws1 = FakeWebSocket.instances[0];
    expect(ws1).toBeDefined();
    ws1?.emitOpen();

    // Replace socket so ws1 is stale
    client.disconnect();
    client.connect();
    const ws2 = FakeWebSocket.instances[1];
    expect(ws2).toBeDefined();
    ws2?.emitOpen();

    // Stale socket 1008 close must be ignored
    ws1?.emitClose(1008, "stale session expired");
    expect(expiredReasons).toEqual([]);

    // Active socket 1008 close notifies listener and schedules zero reconnects
    ws2?.emitClose(1008, "Session expired");
    expect(client.getConnectionState()).toBe("disconnected");
    expect(expiredReasons).toEqual(["Session expired"]);

    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances.length).toBe(2);

    // Default reason fallback when event.reason is empty and unsubscribe cleanup
    client.connect();
    const ws3 = FakeWebSocket.instances[2];
    expect(ws3).toBeDefined();
    ws3?.emitOpen();
    ws3?.emitClose(1008, "");
    expect(expiredReasons).toEqual(["Session expired", "Session expired"]);

    unsubscribe();
    client.connect();
    const ws4 = FakeWebSocket.instances[3];
    ws4?.emitOpen();
    ws4?.emitClose(1008, "after unsubscribe");
    expect(expiredReasons).toHaveLength(2);
  });

  it("verifies session on pre-open close 1006 and expires session when verifySession returns false (P1-R13-001, P2-R13-001)", async () => {
    /**
     * Contract: Mid-stream closes after socket open reconnect without calling verifySession, whereas
     * a pre-open close (1006 before open) invokes verifySession(signal). When verifySession resolves
     * false, MarketWsClient fires onSessionExpired("Session expired") and schedules zero reconnects.
     * Defect caught: Browser close 1006 from pre-accept HTTP 403 looping reconnects forever.
     */
    const verifySession = vi.fn<(signal: AbortSignal) => Promise<boolean>>();
    const client = new MarketWsClient(() => "ws://127.0.0.1:8000/ws/market", verifySession);
    const expiredReasons: string[] = [];
    client.onSessionExpired((reason) => {
      expiredReasons.push(reason);
    });

    client.connect();
    const ws1 = FakeWebSocket.instances[0];
    expect(ws1).toBeDefined();
    ws1?.emitOpen();

    // Mid-stream drop after open does NOT call verifySession and schedules reconnect
    ws1?.emitClose(1006, "network_drop");
    expect(verifySession).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1_000);
    expect(FakeWebSocket.instances).toHaveLength(2);
    const ws2 = FakeWebSocket.instances[1];
    expect(ws2).toBeDefined();

    // Pre-open close 1006 on reconnect triggers verifySession -> false => session expired
    let capturedSignal: AbortSignal | undefined;
    verifySession.mockImplementationOnce(async (signal) => {
      capturedSignal = signal;
      return false;
    });
    ws2?.emitClose(1006, "");
    expect(verifySession).toHaveBeenCalledTimes(1);
    expect(capturedSignal?.aborted).toBe(false);

    await Promise.resolve();
    await Promise.resolve();

    expect(expiredReasons).toEqual(["Session expired"]);
    expect(client.getConnectionState()).toBe("disconnected");

    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("reconnects after backoff when pre-open close verifySession resolves true or rejects transiently (P1-R13-001)", async () => {
    /**
     * Contract: On a pre-open close 1006, if verifySession resolves true or rejects with a
     * transient error (network/5xx), onSessionExpired does NOT fire and reconnect is scheduled.
     * Defect caught: Transient session probe failures falsely logging the user out or halting reconnects.
     */
    const verifySession = vi
      .fn<(signal: AbortSignal) => Promise<boolean>>()
      .mockResolvedValueOnce(true)
      .mockRejectedValueOnce(new Error("503 Service Unavailable"));
    const client = new MarketWsClient(() => "ws://127.0.0.1:8000/ws/market", verifySession);
    const expiredListener = vi.fn();
    client.onSessionExpired(expiredListener);

    client.connect();
    const ws1 = FakeWebSocket.instances[0];
    expect(ws1).toBeDefined();
    ws1?.emitClose(1006, "");
    expect(verifySession).toHaveBeenCalledTimes(1);

    await Promise.resolve();
    await Promise.resolve();
    expect(expiredListener).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(1_000);
    expect(FakeWebSocket.instances).toHaveLength(2);

    const ws2 = FakeWebSocket.instances[1];
    expect(ws2).toBeDefined();
    ws2?.emitClose(1006, "");
    expect(verifySession).toHaveBeenCalledTimes(2);

    await Promise.resolve();
    await Promise.resolve();
    expect(expiredListener).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(2);

    vi.advanceTimersByTime(2_000);
    expect(FakeWebSocket.instances).toHaveLength(3);
  });

  it("aborts pending verifySession on disconnect() and ignores late settlement across epochs (P1-R13-001)", async () => {
    /**
     * Contract: Calling disconnect() while verifySession is pending aborts its AbortSignal and
     * advances connectionEpoch so late settlement triggers neither onSessionExpired nor reconnect.
     * Defect caught: Stale verifySession settlement logging out or reconnecting a disconnected/new epoch.
     */
    let capturedSignal: AbortSignal | undefined;
    let resolvePending!: (value: boolean) => void;
    const verifySession = vi.fn<(signal: AbortSignal) => Promise<boolean>>((signal) => {
      capturedSignal = signal;
      return new Promise<boolean>((resolve) => {
        resolvePending = resolve;
      });
    });

    const client = new MarketWsClient(() => "ws://127.0.0.1:8000/ws/market", verifySession);
    const expiredListener = vi.fn();
    client.onSessionExpired(expiredListener);

    client.connect();
    const ws1 = FakeWebSocket.instances[0];
    expect(ws1).toBeDefined();
    ws1?.emitClose(1006, "");

    expect(verifySession).toHaveBeenCalledTimes(1);
    expect(capturedSignal?.aborted).toBe(false);

    client.disconnect();
    expect(capturedSignal?.aborted).toBe(true);

    resolvePending(false);
    await Promise.resolve();
    await Promise.resolve();

    expect(expiredListener).not.toHaveBeenCalled();
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
