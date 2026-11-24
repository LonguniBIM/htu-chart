// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api/client";
import type {
  BarsResponse,
  ContractsResponse,
  LayoutRecord,
  RootSymbol,
  StatusResponse,
  Timeframe,
  WsBarUpdateMessage,
  WsFeedStatusMessage,
  WsResyncRequiredMessage,
} from "./api/types";
import type { WsConnectionState } from "./api/wsClient";
import { App } from "./App";

const mockWsConnect = vi.fn();
const mockWsDisconnect = vi.fn();
const mockWsSubscribe = vi.fn<(contractId: string, timeframe: Timeframe) => () => void>(
  () => () => {},
);

let feedStatusListeners = new Set<(msg: WsFeedStatusMessage) => void>();
let connectionStateListeners = new Set<(state: WsConnectionState) => void>();
let barUpdateListeners = new Set<(msg: WsBarUpdateMessage) => void>();
let resyncListeners = new Set<(msg: WsResyncRequiredMessage) => void>();

vi.mock("./api/wsClient", () => ({
  MarketWsClient: class MockMarketWsClient {
    connect = mockWsConnect;
    disconnect = mockWsDisconnect;
    subscribe = mockWsSubscribe;

    onConnectionState(listener: (state: WsConnectionState) => void) {
      connectionStateListeners.add(listener);
      return () => {
        connectionStateListeners.delete(listener);
      };
    }

    onFeedStatus(listener: (msg: WsFeedStatusMessage) => void) {
      feedStatusListeners.add(listener);
      return () => {
        feedStatusListeners.delete(listener);
      };
    }

    onBarUpdate(listener: (msg: WsBarUpdateMessage) => void) {
      barUpdateListeners.add(listener);
      return () => {
        barUpdateListeners.delete(listener);
      };
    }

    onResyncRequired(listener: (msg: WsResyncRequiredMessage) => void) {
      resyncListeners.add(listener);
      return () => {
        resyncListeners.delete(listener);
      };
    }
  },
}));

vi.mock("lightweight-charts", () => {
  const CandlestickSeries = Symbol("CandlestickSeries");
  const HistogramSeries = Symbol("HistogramSeries");
  return {
    CandlestickSeries,
    HistogramSeries,
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    createChart: vi.fn(() => ({
      addSeries: vi.fn(() => ({
        setData: vi.fn(),
        update: vi.fn(),
        priceScale: () => ({ applyOptions: vi.fn() }),
      })),
      subscribeCrosshairMove: vi.fn(),
      unsubscribeCrosshairMove: vi.fn(),
      applyOptions: vi.fn(),
      remove: vi.fn(),
      timeScale: () => ({ fitContent: vi.fn() }),
    })),
  };
});

const loginMock = vi.fn<
  (password: string, signal?: AbortSignal) => Promise<{ authenticated: boolean; csrf_token: string }>
>();
const fetchSessionMock = vi.fn<
  (signal?: AbortSignal) => Promise<{ authenticated: boolean; csrf_token: string }>
>();
const fetchStatusMock = vi.fn<(signal?: AbortSignal) => Promise<StatusResponse>>();
const listLayoutsMock = vi.fn<(signal?: AbortSignal) => Promise<LayoutRecord[]>>();
const fetchContractsMock = vi.fn<
  (
    symbols?: readonly RootSymbol[],
    live?: boolean,
    signal?: AbortSignal,
  ) => Promise<ContractsResponse>
>();
const fetchBarsMock = vi.fn<
  (params: { contract_id: string; timeframe: Timeframe }, signal?: AbortSignal) => Promise<BarsResponse>
>();

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    login: (password: string, signal?: AbortSignal) => loginMock(password, signal),
    fetchSession: (signal?: AbortSignal) => fetchSessionMock(signal),
    fetchStatus: (signal?: AbortSignal) => fetchStatusMock(signal),
    listLayouts: (signal?: AbortSignal) => listLayoutsMock(signal),
    fetchContracts: (
      symbols?: readonly RootSymbol[],
      live?: boolean,
      signal?: AbortSignal,
    ) => fetchContractsMock(symbols, live, signal),
    fetchBars: (
      params: { contract_id: string; timeframe: Timeframe },
      signal?: AbortSignal,
    ) => fetchBarsMock(params, signal),
  };
});

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

const SAMPLE_LAYOUT: LayoutRecord = {
  id: "layout-1",
  name: "Default Sim",
  template_id: "1-single",
  panes: [
    {
      pane_id: "pane-1",
      root_symbol: "MES",
      timeframe: "5m",
      live_mode: false,
      playback_mode: "live",
    },
  ],
  sync_config: {
    symbol: false,
    interval: false,
    crosshair: false,
    time: false,
    dateRange: false,
  },
  is_default: true,
  created_at_utc: 1700000000,
  updated_at_utc: 1700000000,
};

async function flushMicrotasks(rounds = 6): Promise<void> {
  for (let i = 0; i < rounds; i += 1) {
    await Promise.resolve();
  }
}

describe("App bootstrap and R03 503 recovery (P1-R3-03, P1-R3-04, P2-005)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    loginMock.mockReset();
    fetchSessionMock.mockReset();
    fetchStatusMock.mockReset();
    listLayoutsMock.mockReset();
    fetchContractsMock.mockReset();
    fetchBarsMock.mockReset();
    feedStatusListeners = new Set();
    connectionStateListeners = new Set();
    barUpdateListeners = new Set();
    resyncListeners = new Set();
    vi.stubGlobal("ResizeObserver", MockResizeObserver);
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
    vi.unstubAllGlobals();
  });

  it("retries GET /api/contracts via bounded backoff timer without WS feed_status LIVE event (P1-01, P2-001)", async () => {
    /**
     * Contract: When initial /api/contracts returns 503 while feed_status stays DISCONNECTED,
     * App autonomously retries /api/contracts via its bounded exponential-backoff timer (2s initial)
     * without requiring any WS feed_status: LIVE event.
     * Defect caught: Startup 503 deadlock when LiveFeedService._contracts is empty and feed stays DISCONNECTED.
     */
    vi.useFakeTimers();
    fetchSessionMock.mockResolvedValue({
      authenticated: true,
      csrf_token: "csrf-token-1234567890",
    });
    fetchStatusMock
      .mockResolvedValueOnce({
        connected: false,
        live: false,
        feed_state: "DISCONNECTED",
      })
      .mockResolvedValue({
        connected: false,
        live: false,
        feed_state: "DISCONNECTED",
      });
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT]);
    fetchContractsMock
      .mockRejectedValueOnce(new ApiError(503, "TopstepX is not connected"))
      .mockResolvedValueOnce({
        live: false,
        contracts: {
          MES: {
            selected: { id: "CON.F.US.MES.H26" },
            error_type: null,
            error: null,
          },
        },
      });
    fetchBarsMock.mockResolvedValue({
      provider: "topstepx",
      live: false,
      contract_id: "CON.F.US.MES.H26",
      root_symbol: "MES",
      timeframe: "5m",
      start_time: 1000,
      end_time: 1000,
      count: 1,
      include_partial: true,
      quality: {
        is_complete: true,
        truncated: false,
        provider_gap: false,
        quarantined_count: 0,
      },
      bars: [
        {
          time: 1000,
          open: 5000,
          high: 5002,
          low: 4999,
          close: 5001,
          volume: 10,
          is_closed: true,
        },
      ],
    });

    render(<App />);
    await act(async () => {
      await flushMicrotasks();
    });

    expect(fetchContractsMock).toHaveBeenCalledTimes(1);
    expect(mockWsConnect).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Retry Contracts" })).toBeTruthy();
    expect(screen.getByText("Unresolved")).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1999);
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
      await flushMicrotasks();
    });

    expect(fetchContractsMock).toHaveBeenCalledTimes(2);
    expect(screen.getByText("CON.F.US.MES.H26")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry Contracts" })).toBeNull();
    expect(fetchBarsMock).toHaveBeenCalled();
  });

  it("caps automatic 503 retries at 12 attempts with [2s, 4s, 8s, 15s] backoff and resets on manual retry", async () => {
    /**
     * Contract: Auto-retry uses [2000, 4000, 8000, 15000] ms exponential backoff, stops after
     * 12 automatic attempts, and resets the counter when Retry Contracts is clicked manually.
     * Defect caught: Unbounded retry loops or missing counter reset on manual retry.
     */
    vi.useFakeTimers();
    fetchSessionMock.mockResolvedValue({
      authenticated: true,
      csrf_token: "csrf-token-1234567890",
    });
    fetchStatusMock.mockResolvedValue({
      connected: false,
      live: false,
      feed_state: "DISCONNECTED",
    });
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT]);
    fetchContractsMock.mockRejectedValue(new ApiError(503, "TopstepX is not connected"));

    render(<App />);
    await act(async () => {
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(1);

    const expectedDelays = [
      2000, 4000, 8000, 15000, 15000, 15000, 15000, 15000, 15000, 15000, 15000, 15000,
    ];
    for (let i = 0; i < expectedDelays.length; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(expectedDelays[i]!);
        await flushMicrotasks();
      });
      expect(fetchContractsMock).toHaveBeenCalledTimes(i + 2);
    }

    // 12 automatic attempts reached -> no further automatic retries
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60000);
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(13);

    // Manual Retry Contracts resets counter and restarts backoff cycle
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry Contracts" }));
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(14);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(15);
  });

  it("keeps Retry Contracts visible on HTTP 200 with selected=null and resolves on retry (P2-002)", async () => {
    /**
     * Contract: When /api/contracts returns HTTP 200 with selected=null for a symbol, App keeps
     * the mode unresolved, surfaces a per-symbol failure summary, merges resolved symbols, and
     * keeps Retry Contracts visible until subsequent retry resolves all symbols.
     * Defect caught: Partial contract failure treated as fully resolved because HTTP status was 200.
     */
    const twoPaneLayout: LayoutRecord = {
      ...SAMPLE_LAYOUT,
      template_id: "2-cols",
      panes: [
        SAMPLE_LAYOUT.panes[0]!,
        {
          pane_id: "pane-2",
          root_symbol: "MNQ",
          timeframe: "5m",
          live_mode: false,
          playback_mode: "live",
        },
      ],
    };
    fetchSessionMock.mockResolvedValue({
      authenticated: true,
      csrf_token: "csrf-token-1234567890",
    });
    fetchStatusMock.mockResolvedValue({
      connected: true,
      live: false,
      feed_state: "LIVE",
    });
    listLayoutsMock.mockResolvedValue([twoPaneLayout]);
    fetchContractsMock
      .mockResolvedValueOnce({
        live: false,
        contracts: {
          MES: {
            selected: { id: "CON.F.US.MES.H26" },
            error_type: null,
            error: null,
          },
          MNQ: {
            selected: null,
            error_type: "UpstreamTimeoutError",
            error: "Timed out querying MNQ",
          },
        },
      })
      .mockResolvedValueOnce({
        live: false,
        contracts: {
          MES: {
            selected: { id: "CON.F.US.MES.H26" },
            error_type: null,
            error: null,
          },
          MNQ: {
            selected: { id: "CON.F.US.MNQ.H26" },
            error_type: null,
            error: null,
          },
        },
      });
    fetchBarsMock.mockResolvedValue({
      provider: "topstepx",
      live: false,
      contract_id: "CON.F.US.MES.H26",
      root_symbol: "MES",
      timeframe: "5m",
      start_time: 1000,
      end_time: 1000,
      count: 0,
      include_partial: true,
      quality: {
        is_complete: true,
        truncated: false,
        provider_gap: false,
        quarantined_count: 0,
      },
      bars: [],
    });

    render(<App />);
    await act(async () => {
      await flushMicrotasks();
    });

    expect(screen.getByText("CON.F.US.MES.H26")).toBeTruthy();
    expect(screen.getByText("Unresolved")).toBeTruthy();
    expect(screen.getByRole("button", { name: /retry contracts/i })).toBeTruthy();
    expect(screen.getByText(/MNQ \(UpstreamTimeoutError\)/)).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /retry contracts/i }));
      await flushMicrotasks();
    });

    expect(fetchContractsMock).toHaveBeenCalledTimes(2);
    expect(screen.getByText("CON.F.US.MNQ.H26")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /retry contracts/i })).toBeNull();
  });

  it("aborts pending fetchContracts on unmount and never calls wsClient.connect()", async () => {
    /**
     * Contract: Unmounting App while bootstrap fetchContracts is in flight aborts the
     * AbortController signal and propagates AbortError without calling wsClient.connect().
     * Defect caught: Swallowed AbortError leaking WebSocket connections after unmount (P1-R3-04).
     */
    fetchSessionMock.mockResolvedValue({
      authenticated: true,
      csrf_token: "csrf-token-1234567890",
    });
    fetchStatusMock.mockResolvedValue({
      connected: true,
      live: false,
      feed_state: "LIVE",
    });
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT]);

    let capturedSignal: AbortSignal | undefined;
    fetchContractsMock.mockImplementation((_symbols, _live, signal) => {
      capturedSignal = signal;
      return new Promise<ContractsResponse>((_resolve, reject) => {
        signal?.addEventListener("abort", () => {
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      });
    });

    const { unmount } = render(<App />);
    await act(async () => {
      await flushMicrotasks();
    });

    expect(fetchContractsMock).toHaveBeenCalledTimes(1);
    expect(capturedSignal?.aborted).toBe(false);

    await act(async () => {
      unmount();
      await flushMicrotasks();
    });

    expect(capturedSignal?.aborted).toBe(true);
    expect(mockWsConnect).not.toHaveBeenCalled();
  });

  it("aborts handleLogin bootstrap fetchContracts on unmount and never calls wsClient.connect() (P1-02, P2-003)", async () => {
    /**
     * Contract: Starting from 401 and submitting login passes an App-owned AbortSignal through
     * login() and bootstrapWorkspace(), aborts on unmount, and never invokes wsClient.connect().
     * Defect caught: handleLogin running un-cancellable bootstrap that connects WS after unmount.
     */
    fetchSessionMock
      .mockRejectedValueOnce(new ApiError(401, "Authentication required"))
      .mockResolvedValue({
        authenticated: true,
        csrf_token: "csrf-token-login-123",
      });

    let loginSignal: AbortSignal | undefined;
    loginMock.mockImplementation((_password, signal) => {
      loginSignal = signal;
      return Promise.resolve({
        authenticated: true,
        csrf_token: "csrf-token-login-123",
      });
    });
    fetchStatusMock.mockResolvedValue({
      connected: true,
      live: false,
      feed_state: "LIVE",
    });
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT]);

    let capturedSignal: AbortSignal | undefined;
    let resolvePending!: (value: ContractsResponse) => void;
    fetchContractsMock.mockImplementation((_symbols, _live, signal) => {
      capturedSignal = signal;
      return new Promise<ContractsResponse>((resolve) => {
        resolvePending = resolve;
      });
    });

    const { unmount } = render(<App />);
    await act(async () => {
      await flushMicrotasks();
    });

    fireEvent.change(screen.getByLabelText("Terminal password"), {
      target: { value: "terminal-secret" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Unlock Terminal" }));
      await flushMicrotasks();
    });

    expect(loginMock).toHaveBeenCalledTimes(1);
    expect(loginSignal).toBeDefined();
    expect(fetchContractsMock).toHaveBeenCalledTimes(1);
    expect(capturedSignal).toBeDefined();
    expect(capturedSignal?.aborted).toBe(false);

    await act(async () => {
      unmount();
      resolvePending({
        live: false,
        contracts: {
          MES: {
            selected: { id: "CON.F.US.MES.H26" },
            error_type: null,
            error: null,
          },
        },
      });
      await flushMicrotasks();
    });

    expect(capturedSignal?.aborted).toBe(true);
    expect(mockWsConnect).not.toHaveBeenCalled();
  });

  it("observes concurrent fetchStatus and fetchContracts rejections without unhandledrejection (P1-03)", async () => {
    /**
     * Contract: When both fetchStatus and fetchContracts reject concurrently during Retry Contracts,
     * Promise.allSettled attaches handlers synchronously to both promises so zero unhandledrejection
     * events fire.
     * Defect caught: Detached statusPromise leaking unhandledrejection when fetchContracts fails first.
     */
    fetchSessionMock.mockResolvedValue({
      authenticated: true,
      csrf_token: "csrf-token-1234567890",
    });
    fetchStatusMock.mockResolvedValueOnce({
      connected: false,
      live: false,
      feed_state: "DISCONNECTED",
    });
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT]);
    fetchContractsMock.mockRejectedValueOnce(new ApiError(503, "TopstepX is not connected"));

    render(<App />);
    await act(async () => {
      await flushMicrotasks();
    });

    let rejectStatus!: (reason?: unknown) => void;
    let rejectContracts!: (reason?: unknown) => void;
    fetchStatusMock.mockImplementationOnce(
      () =>
        new Promise<StatusResponse>((_resolve, reject) => {
          rejectStatus = reject;
        }),
    );
    fetchContractsMock.mockImplementationOnce(
      () =>
        new Promise<ContractsResponse>((_resolve, reject) => {
          rejectContracts = reject;
        }),
    );

    const unhandledRejections: unknown[] = [];
    const onWindowUnhandled = (event: PromiseRejectionEvent) => {
      unhandledRejections.push(event.reason);
    };
    window.addEventListener("unhandledrejection", onWindowUnhandled);

    try {
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry Contracts" }));
        await flushMicrotasks();
      });

      await act(async () => {
        rejectContracts(new ApiError(503, "contracts unavailable"));
        rejectStatus(new ApiError(503, "status unavailable"));
        await flushMicrotasks();
      });

      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });

      expect(unhandledRejections).toEqual([]);
      expect(screen.getByRole("button", { name: /retry contracts/i })).toBeTruthy();
    } finally {
      window.removeEventListener("unhandledrejection", onWindowUnhandled);
    }
  });
});
