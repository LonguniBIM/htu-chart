import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WsResyncRequiredMessage } from "./types";
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
});
