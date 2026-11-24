import type {
  Timeframe,
  WsBarUpdateMessage,
  WsErrorMessage,
  WsFeedStatusMessage,
  WsInboundMessage,
  WsResyncRequiredMessage,
  WsSubscribedMessage,
} from "./types";

export type WsConnectionState = "disconnected" | "connecting" | "connected";

type BarUpdateListener = (message: WsBarUpdateMessage) => void;
type FeedStatusListener = (message: WsFeedStatusMessage) => void;
type ResyncRequiredListener = (message: WsResyncRequiredMessage) => void;
type SubscribedListener = (message: WsSubscribedMessage) => void;
type ErrorListener = (message: WsErrorMessage) => void;
type ConnectionStateListener = (state: WsConnectionState) => void;

interface SubscriptionEntry {
  contractId: string;
  timeframe: Timeframe;
  count: number;
}

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30000;
const PING_INTERVAL_MS = 20000;
const WS_CLOSE_POLICY_VIOLATION = 1008;
const WS_CLOSE_SLOW_CONSUMER = 4008;

function subscriptionKey(contractId: string, timeframe: Timeframe): string {
  return `${contractId.trim().toUpperCase()}|${timeframe.trim().toLowerCase()}`;
}

export function buildMarketWsUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/market`;
}

export class MarketWsClient {
  private socket: WebSocket | null = null;
  private shouldRun = false;
  private hasConnectedOnce = false;
  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private connectionState: WsConnectionState = "disconnected";

  private readonly subscriptions = new Map<string, SubscriptionEntry>();
  private readonly barListeners = new Set<BarUpdateListener>();
  private readonly feedStatusListeners = new Set<FeedStatusListener>();
  private readonly resyncListeners = new Set<ResyncRequiredListener>();
  private readonly subscribedListeners = new Set<SubscribedListener>();
  private readonly errorListeners = new Set<ErrorListener>();
  private readonly connectionListeners = new Set<ConnectionStateListener>();
  private readonly urlFactory: () => string;

  constructor(urlFactory: () => string = buildMarketWsUrl) {
    this.urlFactory = urlFactory;
  }

  getState(): WsConnectionState {
    return this.connectionState;
  }

  getConnectionState(): WsConnectionState {
    return this.connectionState;
  }

  connect(): void {
    this.shouldRun = true;
    if (
      this.socket !== null &&
      (this.socket.readyState === WebSocket.OPEN ||
        this.socket.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    this.clearReconnectTimer();
    this.openSocket();
  }

  disconnect(): void {
    this.shouldRun = false;
    this.hasConnectedOnce = false;
    this.reconnectAttempt = 0;
    this.clearTimers();
    if (this.socket !== null) {
      const sock = this.socket;
      this.socket = null;
      sock.close(1000, "client_disconnect");
    }
    this.setConnectionState("disconnected");
  }

  subscribe(contractId: string, timeframe: Timeframe): () => void {
    const normalizedId = contractId.trim().toUpperCase();
    const key = subscriptionKey(normalizedId, timeframe);
    const existing = this.subscriptions.get(key);
    if (existing !== undefined) {
      existing.count += 1;
    } else {
      this.subscriptions.set(key, {
        contractId: normalizedId,
        timeframe,
        count: 1,
      });
      this.sendFrame({
        type: "subscribe",
        contract_id: normalizedId,
        timeframe,
      });
    }

    let disposed = false;
    return () => {
      if (disposed) {
        return;
      }
      disposed = true;
      const current = this.subscriptions.get(key);
      if (current === undefined) {
        return;
      }
      current.count -= 1;
      if (current.count <= 0) {
        this.subscriptions.delete(key);
        this.sendFrame({
          type: "unsubscribe",
          contract_id: current.contractId,
          timeframe: current.timeframe,
        });
      }
    };
  }

  onBarUpdate(listener: BarUpdateListener): () => void {
    this.barListeners.add(listener);
    return () => {
      this.barListeners.delete(listener);
    };
  }

  onFeedStatus(listener: FeedStatusListener): () => void {
    this.feedStatusListeners.add(listener);
    return () => {
      this.feedStatusListeners.delete(listener);
    };
  }

  onResyncRequired(listener: ResyncRequiredListener): () => void {
    this.resyncListeners.add(listener);
    return () => {
      this.resyncListeners.delete(listener);
    };
  }

  onSubscribed(listener: SubscribedListener): () => void {
    this.subscribedListeners.add(listener);
    return () => {
      this.subscribedListeners.delete(listener);
    };
  }

  onError(listener: ErrorListener): () => void {
    this.errorListeners.add(listener);
    return () => {
      this.errorListeners.delete(listener);
    };
  }

  onConnectionState(listener: ConnectionStateListener): () => void {
    this.connectionListeners.add(listener);
    return () => {
      this.connectionListeners.delete(listener);
    };
  }

  private setConnectionState(next: WsConnectionState): void {
    if (this.connectionState === next) {
      return;
    }
    this.connectionState = next;
    for (const listener of this.connectionListeners) {
      listener(next);
    }
  }

  private openSocket(): void {
    this.setConnectionState("connecting");
    const ws = new WebSocket(this.urlFactory());
    this.socket = ws;

    ws.addEventListener("open", () => {
      if (this.socket !== ws) {
        return;
      }
      const wasReconnect = this.hasConnectedOnce;
      this.hasConnectedOnce = true;
      this.reconnectAttempt = 0;
      this.setConnectionState("connected");
      for (const entry of this.subscriptions.values()) {
        if (entry.count > 0) {
          this.sendFrame({
            type: "subscribe",
            contract_id: entry.contractId,
            timeframe: entry.timeframe,
          });
        }
      }
      if (wasReconnect) {
        for (const listener of this.resyncListeners) {
          listener({
            type: "resync_required",
            reason: "ws_reconnected",
            contract_id: null,
          });
        }
      }
      this.clearPingTimer();
      this.pingTimer = setInterval(() => {
        this.sendFrame({ type: "ping" });
      }, PING_INTERVAL_MS);
    });

    ws.addEventListener("message", (event: MessageEvent<unknown>) => {
      if (this.socket !== ws) {
        return;
      }
      if (typeof event.data !== "string") {
        return;
      }
      let parsed: WsInboundMessage;
      try {
        parsed = JSON.parse(event.data) as WsInboundMessage;
      } catch {
        return;
      }
      if (parsed.type === "bar_update") {
        for (const listener of this.barListeners) {
          listener(parsed);
        }
      } else if (parsed.type === "feed_status") {
        for (const listener of this.feedStatusListeners) {
          listener(parsed);
        }
      } else if (parsed.type === "resync_required") {
        for (const listener of this.resyncListeners) {
          listener(parsed);
        }
      } else if (parsed.type === "subscribed") {
        for (const listener of this.subscribedListeners) {
          listener(parsed);
        }
      } else if (parsed.type === "error") {
        for (const listener of this.errorListeners) {
          listener(parsed);
        }
      }
    });

    ws.addEventListener("close", (event: CloseEvent) => {
      if (this.socket !== ws) {
        return;
      }
      this.socket = null;
      this.clearPingTimer();
      this.setConnectionState("disconnected");

      if (event.code === WS_CLOSE_SLOW_CONSUMER) {
        for (const listener of this.resyncListeners) {
          listener({
            type: "resync_required",
            reason: event.reason || "slow_consumer_disconnect",
            contract_id: null,
          });
        }
      }

      if (!this.shouldRun || event.code === WS_CLOSE_POLICY_VIOLATION) {
        this.shouldRun = false;
        return;
      }
      this.scheduleReconnect();
    });

    ws.addEventListener("error", () => {
      if (this.socket !== ws) {
        return;
      }
      // Close event handles reconnect scheduling cleanly.
    });
  }

  private sendFrame(payload: Record<string, unknown>): void {
    if (this.socket !== null && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(payload));
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) {
      return;
    }
    const delayMs = Math.min(
      MAX_BACKOFF_MS,
      INITIAL_BACKOFF_MS * 2 ** this.reconnectAttempt,
    );
    this.reconnectAttempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.shouldRun) {
        this.openSocket();
      }
    }, delayMs);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private clearPingTimer(): void {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  private clearTimers(): void {
    this.clearReconnectTimer();
    this.clearPingTimer();
  }
}
