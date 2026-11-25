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
  WsErrorMessage,
  WsFeedStatusMessage,
  WsResyncRequiredMessage,
  WsSubscribedMessage,
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
let errorListeners = new Set<(msg: WsErrorMessage) => void>();
let subscribedListeners = new Set<(msg: WsSubscribedMessage) => void>();
let sessionExpiredListeners = new Set<(reason: string) => void>();
let useRealWsClient = false;

vi.mock("./api/wsClient", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/wsClient")>();
  return {
    ...actual,
    MarketWsClient: class MockMarketWsClient {
      connect = mockWsConnect;
      disconnect = mockWsDisconnect;
      subscribe = mockWsSubscribe;

      constructor(
        urlFactory?: () => string,
        verifySession?: (signal: AbortSignal) => Promise<boolean>,
      ) {
        if (useRealWsClient) {
          const real = new actual.MarketWsClient(urlFactory, verifySession);
          const origDisconnect = real.disconnect.bind(real);
          vi.spyOn(real, "disconnect").mockImplementation(() => {
            mockWsDisconnect();
            origDisconnect();
          });
          return real as unknown as MockMarketWsClient;
        }
      }

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

    onError(listener: (msg: WsErrorMessage) => void) {
      errorListeners.add(listener);
      return () => {
        errorListeners.delete(listener);
      };
    }

    onSubscribed(listener: (msg: WsSubscribedMessage) => void) {
      subscribedListeners.add(listener);
      return () => {
        subscribedListeners.delete(listener);
      };
    }

      onSessionExpired(listener: (reason: string) => void) {
        sessionExpiredListeners.add(listener);
        return () => {
          sessionExpiredListeners.delete(listener);
        };
      }
    },
  };
});

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
const logoutMock = vi.fn<(signal?: AbortSignal) => Promise<{ authenticated: boolean }>>();
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
    logout: (signal?: AbortSignal) => logoutMock(signal),
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
    logoutMock.mockReset();
    fetchSessionMock.mockReset();
    fetchStatusMock.mockReset();
    listLayoutsMock.mockReset();
    fetchContractsMock.mockReset();
    fetchBarsMock.mockReset();
    feedStatusListeners = new Set();
    connectionStateListeners = new Set();
    barUpdateListeners = new Set();
    resyncListeners = new Set();
    errorListeners = new Set();
    subscribedListeners = new Set();
    sessionExpiredListeners = new Set();
    useRealWsClient = false;
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

  it("aborts and epoch-guards pending layout-switch fetchContracts across Logout -> Login (P1-02)", async () => {
    /**
     * Contract: Switching layouts passes a dedicated layoutResolveControllerRef AbortSignal to
     * fetchContractsForModes and binds completion to sessionEpochRef. Across Logout -> Login,
     * the stale signal is aborted and a late-resolving stale response never overwrites new
     * session contracts, loadError, or contractsPendingRetry.
     * Defect caught: Un-cancelled layout switch request mutating state after session change.
     */
    const liveLayout: LayoutRecord = {
      ...SAMPLE_LAYOUT,
      id: "layout-live",
      name: "Live MNQ",
      is_default: false,
      panes: [
        {
          pane_id: "pane-live",
          root_symbol: "MNQ",
          timeframe: "5m",
          live_mode: true,
          playback_mode: "live",
        },
      ],
    };
    const newSessionLayout: LayoutRecord = {
      ...SAMPLE_LAYOUT,
      id: "layout-new-session",
      name: "New Session Live",
      is_default: true,
      panes: [
        {
          pane_id: "pane-new",
          root_symbol: "MNQ",
          timeframe: "5m",
          live_mode: true,
          playback_mode: "live",
        },
      ],
    };

    fetchSessionMock.mockResolvedValue({
      authenticated: true,
      csrf_token: "csrf-token-session-1",
    });
    fetchStatusMock.mockResolvedValue({
      connected: true,
      live: true,
      feed_state: "LIVE",
    });
    listLayoutsMock
      .mockResolvedValueOnce([SAMPLE_LAYOUT, liveLayout])
      .mockResolvedValueOnce([newSessionLayout]);
    logoutMock.mockResolvedValue({ authenticated: false });
    loginMock.mockResolvedValue({
      authenticated: true,
      csrf_token: "csrf-token-session-2",
    });

    let staleSignal: AbortSignal | undefined;
    let resolveStaleLayout!: (value: ContractsResponse) => void;
    fetchContractsMock
      .mockResolvedValueOnce({
        live: false,
        contracts: {
          MES: {
            selected: { id: "CON.F.US.MES.H26" },
            error_type: null,
            error: null,
          },
        },
      })
      .mockImplementationOnce((_symbols, _live, signal) => {
        staleSignal = signal;
        return new Promise<ContractsResponse>((resolve) => {
          resolveStaleLayout = resolve;
        });
      })
      .mockResolvedValueOnce({
        live: true,
        contracts: {
          MNQ: {
            selected: { id: "CON.F.US.MNQ.NEW" },
            error_type: null,
            error: null,
          },
        },
      });
    fetchBarsMock.mockResolvedValue({
      provider: "topstepx",
      live: true,
      contract_id: "CON.F.US.MNQ.NEW",
      root_symbol: "MNQ",
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

    // Switch layout -> starts pending fetchContracts for live_mode=true
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Active layout"), {
        target: { value: "layout-live" },
      });
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(2);
    expect(staleSignal).toBeDefined();
    expect(staleSignal?.aborted).toBe(false);

    // Logout -> aborts staleSignal and advances sessionEpochRef
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Logout" }));
      await flushMicrotasks();
    });
    expect(staleSignal?.aborted).toBe(true);

    // Login into new session -> resolves CON.F.US.MNQ.NEW
    fireEvent.change(screen.getByLabelText("Terminal password"), {
      target: { value: "terminal-secret" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Unlock Terminal" }));
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(3);
    expect(screen.getByText(/CON\.F\.US\.MNQ\.NEW/)).toBeTruthy();

    // Late resolution of stale layout switch with partial failure & stale contract ID
    await act(async () => {
      resolveStaleLayout({
        live: true,
        contracts: {
          MNQ: {
            selected: { id: "CON.F.US.MNQ.STALE" },
            error_type: null,
            error: null,
          },
          MYM: {
            selected: null,
            error_type: "StaleLayoutError",
            error: "Should be ignored across sessions",
          },
        },
      });
      await flushMicrotasks();
    });

    expect(staleSignal?.aborted).toBe(true);
    expect(screen.getByText(/CON\.F\.US\.MNQ\.NEW/)).toBeTruthy();
    expect(screen.queryByText(/CON\.F\.US\.MNQ\.STALE/)).toBeNull();
    expect(screen.queryByText(/StaleLayoutError/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry Contracts" })).toBeNull();
  });

  it("surfaces inbound WS error frames via wsClient.onError in loadError (P1-01)", async () => {
    /**
     * Contract: When /ws/market emits { type: "error", detail }, App surfaces detail
     * visibly in the header error banner via wsClient.onError.
     * Defect caught: Server WS error frames silently dropped without user visibility.
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
    fetchContractsMock.mockResolvedValue({
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

    act(() => {
      for (const listener of errorListeners) {
        listener({ type: "error", detail: "Timeframe '3m' is not enabled" });
      }
    });

    expect(screen.getByText("Timeframe '3m' is not enabled")).toBeTruthy();
  });

  it("mutually aborts and generation-guards retryUnresolvedContracts vs layout-switch fetchContracts (P1-R5-01)", async () => {
    /**
     * Contract: Starting a layout-switch contract fetch aborts an in-flight retryUnresolvedContracts
     * request and advances contractResolveGenerationRef so that a late-resolving oldRetryDeferred
     * (even if transport ignores AbortSignal) cannot clobber fresh layout-switch contracts,
     * cleared error banner, or cleared Retry Contracts state.
     * Defect caught: Out-of-order retryUnresolvedContracts vs layout-switch overwriting fresh contracts.
     */
    const liveLayout: LayoutRecord = {
      ...SAMPLE_LAYOUT,
      id: "layout-live",
      name: "Live MNQ",
      is_default: false,
      panes: [
        {
          pane_id: "pane-live",
          root_symbol: "MNQ",
          timeframe: "5m",
          live_mode: true,
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
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT, liveLayout]);

    let oldRetrySignal: AbortSignal | undefined;
    let resolveOldRetry!: (value: ContractsResponse) => void;
    let liveSwitchSignal: AbortSignal | undefined;

    fetchContractsMock
      .mockResolvedValueOnce({
        live: false,
        contracts: {
          MES: {
            selected: null,
            error_type: "InitialTimeout",
            error: "Timed out querying MES",
          },
        },
      })
      .mockImplementationOnce((_symbols, _live, signal) => {
        oldRetrySignal = signal;
        return new Promise<ContractsResponse>((resolve) => {
          resolveOldRetry = resolve;
        });
      })
      .mockImplementationOnce((_symbols, _live, signal) => {
        liveSwitchSignal = signal;
        return new Promise<ContractsResponse>(() => {
          // Aborted before resolving when user switches back to SAMPLE_LAYOUT
        });
      })
      .mockResolvedValueOnce({
        live: false,
        contracts: {
          MES: {
            selected: { id: "CON.F.US.MES.FRESH" },
            error_type: null,
            error: null,
          },
        },
      });

    fetchBarsMock.mockResolvedValue({
      provider: "topstepx",
      live: false,
      contract_id: "CON.F.US.MES.FRESH",
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

    expect(screen.getByRole("button", { name: "Retry Contracts" })).toBeTruthy();
    expect(screen.getByText(/MES \(InitialTimeout\)/)).toBeTruthy();

    // Start retryUnresolvedContracts (pending oldRetryDeferred)
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry Contracts" }));
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(2);
    expect(oldRetrySignal?.aborted).toBe(false);

    // Switch to Live MNQ layout, then immediately back to Default Sim layout
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Active layout"), {
        target: { value: "layout-live" },
      });
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(3);
    expect(oldRetrySignal?.aborted).toBe(true);
    expect(liveSwitchSignal?.aborted).toBe(false);

    await act(async () => {
      fireEvent.change(screen.getByLabelText("Active layout"), {
        target: { value: "layout-1" },
      });
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(4);
    expect(liveSwitchSignal?.aborted).toBe(true);
    expect(screen.getByText(/CON\.F\.US\.MES\.FRESH/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry Contracts" })).toBeNull();
    expect(screen.queryByText(/Incomplete contracts/)).toBeNull();

    // Late resolution of oldRetryDeferred with stale contract ID and partial selected=null failure
    await act(async () => {
      resolveOldRetry({
        live: false,
        contracts: {
          MES: {
            selected: { id: "CON.F.US.MES.STALE" },
            error_type: null,
            error: null,
          },
          MNQ: {
            selected: null,
            error_type: "LateRetryError",
            error: "Stale retry partial failure",
          },
        },
      });
      await flushMicrotasks();
    });

    expect(screen.getByText(/CON\.F\.US\.MES\.FRESH/)).toBeTruthy();
    expect(screen.queryByText(/CON\.F\.US\.MES\.STALE/)).toBeNull();
    expect(screen.queryByText(/LateRetryError/)).toBeNull();
    expect(screen.queryByText(/Incomplete contracts/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry Contracts" })).toBeNull();
  });

  it("adopts active Live layout unresolved modes when retryUnresolvedContracts aborts layout switch (P1-R6-01)", async () => {
    /**
     * Contract: When Sim bootstrap is incomplete (unresolvedModesRef has false) and user switches
     * to a Live layout whose fetchContracts is in flight, the 2000ms auto-retry timer aborting
     * layoutResolveControllerRef requests BOTH Sim (live=false) and Live (live=true) and resolves
     * the active Live layout cleanly.
     * Defect caught: retryUnresolvedContracts dropping active layout mode on mutual cancellation.
     */
    vi.useFakeTimers();
    const liveLayout: LayoutRecord = {
      ...SAMPLE_LAYOUT,
      id: "layout-live",
      name: "Live MNQ",
      is_default: false,
      panes: [
        {
          pane_id: "pane-live",
          root_symbol: "MNQ",
          timeframe: "5m",
          live_mode: true,
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
      live: true,
      feed_state: "LIVE",
    });
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT, liveLayout]);

    let liveSwitchSignal: AbortSignal | undefined;
    const requestedLiveModes: boolean[] = [];

    fetchContractsMock
      .mockResolvedValueOnce({
        live: false,
        contracts: {
          MES: {
            selected: null,
            error_type: "InitialTimeout",
            error: "Timed out querying MES",
          },
        },
      })
      .mockImplementationOnce((_symbols, live, signal) => {
        liveSwitchSignal = signal;
        expect(live).toBe(true);
        return new Promise<ContractsResponse>((_resolve, reject) => {
          signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        });
      })
      .mockImplementation(async (_symbols, live) => {
        const isLive = Boolean(live);
        requestedLiveModes.push(isLive);
        if (!isLive) {
          return {
            live: false,
            contracts: {
              MES: {
                selected: { id: "CON.F.US.MES.H26" },
                error_type: null,
                error: null,
              },
            },
          };
        }
        return {
          live: true,
          contracts: {
            MNQ: {
              selected: { id: "CON.F.US.MNQ.H26" },
              error_type: null,
              error: null,
            },
          },
        };
      });

    fetchBarsMock.mockResolvedValue({
      provider: "topstepx",
      live: true,
      contract_id: "CON.F.US.MNQ.H26",
      root_symbol: "MNQ",
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

    expect(screen.getByRole("button", { name: "Retry Contracts" })).toBeTruthy();

    await act(async () => {
      fireEvent.change(screen.getByLabelText("Active layout"), {
        target: { value: "layout-live" },
      });
      await flushMicrotasks();
    });
    expect(liveSwitchSignal?.aborted).toBe(false);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    });

    expect(liveSwitchSignal?.aborted).toBe(true);
    expect(requestedLiveModes).toContain(false);
    expect(requestedLiveModes).toContain(true);
    expect(screen.getByText(/CON\.F\.US\.MNQ\.H26/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry Contracts" })).toBeNull();
    expect(screen.queryByText(/Incomplete contracts/)).toBeNull();
  });

  it("retains unresolved mode and auto-retries when switching to Live fails with network TypeError (P1-R8-01, P2-001)", async () => {
    /**
     * Contract: When Sim bootstrap succeeds and switching to a Live layout fails with a transient
     * network TypeError("Failed to fetch"), App surfaces the error message alongside "Retry Contracts",
     * records live=true in unresolvedModesRef, and autonomously retries live=true after 2000ms.
     * Defect caught: applyContractResolutionError ignoring non-503 transient errors and stranding Retry Contracts.
     */
    vi.useFakeTimers();
    const liveLayout: LayoutRecord = {
      ...SAMPLE_LAYOUT,
      id: "layout-live",
      name: "Live MNQ",
      is_default: false,
      panes: [
        {
          pane_id: "pane-live",
          root_symbol: "MNQ",
          timeframe: "5m",
          live_mode: true,
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
      live: true,
      feed_state: "LIVE",
    });
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT, liveLayout]);

    fetchContractsMock
      .mockResolvedValueOnce({
        live: false,
        contracts: {
          MES: {
            selected: { id: "CON.F.US.MES.H26" },
            error_type: null,
            error: null,
          },
        },
      })
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce({
        live: true,
        contracts: {
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
    expect(screen.queryByRole("button", { name: "Retry Contracts" })).toBeNull();

    await act(async () => {
      fireEvent.change(screen.getByLabelText("Active layout"), {
        target: { value: "layout-live" },
      });
      await flushMicrotasks();
    });

    expect(fetchContractsMock).toHaveBeenCalledTimes(2);
    expect(fetchContractsMock.mock.calls[1]?.[1]).toBe(true);
    expect(screen.getByText(/Live: Failed to fetch/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry Contracts" })).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    });

    expect(fetchContractsMock).toHaveBeenCalledTimes(3);
    expect(fetchContractsMock.mock.calls[2]?.[1]).toBe(true);
    expect(screen.getByText("CON.F.US.MNQ.H26")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry Contracts" })).toBeNull();
    expect(screen.queryByText(/Live: Failed to fetch/)).toBeNull();
  });

  it("isolates per-mode failures in fetchContractsForModes so Live resolves when Sim rejects (P1-R10-01)", async () => {
    /**
     * Contract: When initial Sim bootstrap is incomplete, switching to a Live layout is pending,
     * and the 2000ms auto-retry runs [false, true], a TypeError("Sim transport error") on Sim
     * (live=false) does not abort Live (live=true): Live still requests and resolves
     * CON.F.US.MNQ.H26 onto the active Live pane, "Sim: Sim transport error" surfaces, and the
     * subsequent 4000ms auto-retry requests only Sim (live=false) while Live stays resolved.
     * Defect caught: Per-mode exception in fetchContractsForModes aborting remaining modes in batch.
     */
    vi.useFakeTimers();
    const liveLayout: LayoutRecord = {
      ...SAMPLE_LAYOUT,
      id: "layout-live",
      name: "Live MNQ",
      is_default: false,
      panes: [
        {
          pane_id: "pane-live",
          root_symbol: "MNQ",
          timeframe: "5m",
          live_mode: true,
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
      live: true,
      feed_state: "LIVE",
    });
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT, liveLayout]);

    let liveSwitchSignal: AbortSignal | undefined;
    const retry1Modes: boolean[] = [];
    const retry2Modes: boolean[] = [];

    fetchContractsMock
      .mockResolvedValueOnce({
        live: false,
        contracts: {
          MES: {
            selected: null,
            error_type: "InitialTimeout",
            error: "Timed out querying MES",
          },
        },
      })
      .mockImplementationOnce((_symbols, live, signal) => {
        liveSwitchSignal = signal;
        expect(live).toBe(true);
        return new Promise<ContractsResponse>((_resolve, reject) => {
          signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        });
      })
      .mockImplementationOnce(async (_symbols, live) => {
        const isLive = Boolean(live);
        retry1Modes.push(isLive);
        expect(isLive).toBe(false);
        throw new TypeError("Sim transport error");
      })
      .mockImplementationOnce(async (_symbols, live) => {
        const isLive = Boolean(live);
        retry1Modes.push(isLive);
        expect(isLive).toBe(true);
        return {
          live: true,
          contracts: {
            MNQ: {
              selected: { id: "CON.F.US.MNQ.H26" },
              error_type: null,
              error: null,
            },
          },
        };
      })
      .mockImplementationOnce(async (_symbols, live) => {
        const isLive = Boolean(live);
        retry2Modes.push(isLive);
        expect(isLive).toBe(false);
        return {
          live: false,
          contracts: {
            MES: {
              selected: { id: "CON.F.US.MES.H26" },
              error_type: null,
              error: null,
            },
          },
        };
      });

    fetchBarsMock.mockResolvedValue({
      provider: "topstepx",
      live: true,
      contract_id: "CON.F.US.MNQ.H26",
      root_symbol: "MNQ",
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

    expect(screen.getByRole("button", { name: "Retry Contracts" })).toBeTruthy();

    await act(async () => {
      fireEvent.change(screen.getByLabelText("Active layout"), {
        target: { value: "layout-live" },
      });
      await flushMicrotasks();
    });
    expect(liveSwitchSignal?.aborted).toBe(false);

    // First auto-retry (2000ms): Sim rejects with TypeError, Live still resolves MNQ
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    });

    expect(liveSwitchSignal?.aborted).toBe(true);
    expect(retry1Modes).toEqual([false, true]);
    expect(screen.getByText(/CON\.F\.US\.MNQ\.H26/)).toBeTruthy();
    expect(screen.getByText(/Sim: Sim transport error/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry Contracts" })).toBeTruthy();

    // Subsequent auto-retry (4000ms): only Sim (live=false) is requested; Live remains resolved
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
      await flushMicrotasks();
    });

    expect(retry2Modes).toEqual([false]);
    expect(fetchContractsMock).toHaveBeenCalledTimes(5);
    expect(screen.getByText(/CON\.F\.US\.MNQ\.H26/)).toBeTruthy();
    expect(screen.queryByText(/Sim: Sim transport error/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry Contracts" })).toBeNull();
  });

  it("invalidates session without calling wsClient.connect or scheduling retries when bootstrap fetchContracts returns 401 (P1-R11-01)", async () => {
    /**
     * Contract: When fetchSession, fetchStatus, and listLayouts succeed during mount bootstrap
     * but fetchContracts rejects with ApiError(401), App re-throws past wsClient.connect(),
     * clears partial bootstrap state via invalidateSessionLocally, renders the login gate,
     * and schedules zero further fetchContracts retries.
     * Defect caught: bootstrapWorkspace swallowing 401 from fetchContracts and opening WS anyway.
     */
    vi.useFakeTimers();
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
    fetchContractsMock.mockRejectedValueOnce(new ApiError(401, "Session expired"));

    render(<App />);
    await act(async () => {
      await flushMicrotasks();
    });

    expect(screen.getByPlaceholderText("Enter terminal password")).toBeTruthy();
    expect(mockWsConnect).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Retry Contracts" })).toBeNull();
    expect(fetchContractsMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(1);
  });

  it("invalidates session and surfaces 403 error when handleLogin bootstrap fetchContracts returns 403 (P1-R11-01)", async () => {
    /**
     * Contract: When login() succeeds and bootstrapWorkspace fetchContracts rejects with
     * ApiError(403), handleLogin calls invalidateSessionLocally(err.message), never calls
     * wsClient.connect(), returns to the login gate with the 403 error, and schedules zero retries.
     * Defect caught: handleLogin bootstrap swallowing 403 and leaving authenticated/retry state.
     */
    vi.useFakeTimers();
    fetchSessionMock.mockRejectedValueOnce(new ApiError(401, "Authentication required"));
    loginMock.mockResolvedValueOnce({
      authenticated: true,
      csrf_token: "csrf-token-login-123",
    });
    fetchStatusMock.mockResolvedValue({
      connected: true,
      live: false,
      feed_state: "LIVE",
    });
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT]);
    fetchContractsMock.mockRejectedValueOnce(new ApiError(403, "Invalid CSRF token"));

    render(<App />);
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

    expect(screen.getByPlaceholderText("Enter terminal password")).toBeTruthy();
    expect(screen.getByText(/Invalid CSRF token/)).toBeTruthy();
    expect(mockWsConnect).not.toHaveBeenCalled();
    expect(fetchContractsMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(1);
  });

  it("stops auto-retry, disconnects WS, and returns to login when 503 retry receives 401 (P2-R11-001)", async () => {
    /**
     * Contract: When initial bootstrap receives 503 on fetchContracts (scheduling auto-retry)
     * and the 2000ms retry attempt receives ApiError(401), App immediately calls
     * invalidateSessionLocally, disconnects WS, clears retry state, returns to the login gate,
     * and triggers zero further retry requests.
     * Defect caught: retryUnresolvedContracts leaving authenticated workspace/retry loop on 401.
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
    fetchContractsMock
      .mockRejectedValueOnce(new ApiError(503, "TopstepX is not connected"))
      .mockRejectedValueOnce(new ApiError(401, "Session expired"));

    render(<App />);
    await act(async () => {
      await flushMicrotasks();
    });

    expect(fetchContractsMock).toHaveBeenCalledTimes(1);
    expect(mockWsConnect).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Retry Contracts" })).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    });

    expect(fetchContractsMock).toHaveBeenCalledTimes(2);
    expect(screen.getByPlaceholderText("Enter terminal password")).toBeTruthy();
    expect(mockWsDisconnect).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60000);
      await flushMicrotasks();
    });
    expect(fetchContractsMock).toHaveBeenCalledTimes(2);
  });

  it("fails fast on fetchStatus 401 in retryUnresolvedContracts and aborts pending fetchContracts (P1-R12-002)", async () => {
    /**
     * Contract: When retryUnresolvedContracts runs fetchStatus and fetchContracts in parallel and
     * fetchStatus rejects with ApiError(401) while fetchContracts remains pending, App immediately
     * invalidates the session, disconnects WS, renders login gate, and aborts fetchContracts'
     * AbortSignal BEFORE the pending fetchContracts promise resolves.
     * Defect caught: Waiting on Promise.allSettled before handling 401/403 from the faster sibling.
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
    fetchStatusMock.mockImplementationOnce(
      () =>
        new Promise<StatusResponse>((_resolve, reject) => {
          rejectStatus = reject;
        }),
    );
    let contractsSignal: AbortSignal | undefined;
    let resolveContracts!: (value: ContractsResponse) => void;
    fetchContractsMock.mockImplementationOnce((_symbols, _live, signal) => {
      contractsSignal = signal;
      return new Promise<ContractsResponse>((resolve) => {
        resolveContracts = resolve;
      });
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry Contracts" }));
      await flushMicrotasks();
    });
    expect(contractsSignal?.aborted).toBe(false);

    await act(async () => {
      rejectStatus(new ApiError(401, "Session expired"));
      await flushMicrotasks();
    });

    expect(contractsSignal?.aborted).toBe(true);
    expect(mockWsDisconnect).toHaveBeenCalledTimes(1);
    expect(screen.getByPlaceholderText("Enter terminal password")).toBeTruthy();

    await act(async () => {
      resolveContracts({
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
    expect(screen.getByPlaceholderText("Enter terminal password")).toBeTruthy();
  });

  it("fails fast on fetchContracts 401 in retryUnresolvedContracts and aborts pending fetchStatus (P1-R12-002)", async () => {
    /**
     * Contract: When retryUnresolvedContracts runs fetchStatus and fetchContracts in parallel and
     * fetchContracts rejects with ApiError(401) while fetchStatus remains pending, App immediately
     * invalidates the session, disconnects WS, renders login gate, and aborts fetchStatus's
     * AbortSignal BEFORE the pending fetchStatus promise resolves.
     * Defect caught: Waiting on Promise.allSettled before handling 401/403 from fetchContracts.
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

    let statusSignal: AbortSignal | undefined;
    let resolveStatus!: (value: StatusResponse) => void;
    fetchStatusMock.mockImplementationOnce((signal) => {
      statusSignal = signal;
      return new Promise<StatusResponse>((resolve) => {
        resolveStatus = resolve;
      });
    });
    let rejectContracts!: (reason?: unknown) => void;
    fetchContractsMock.mockImplementationOnce(
      () =>
        new Promise<ContractsResponse>((_resolve, reject) => {
          rejectContracts = reject;
        }),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry Contracts" }));
      await flushMicrotasks();
    });
    expect(statusSignal?.aborted).toBe(false);

    await act(async () => {
      rejectContracts(new ApiError(401, "Session expired"));
      await flushMicrotasks();
    });

    expect(statusSignal?.aborted).toBe(true);
    expect(mockWsDisconnect).toHaveBeenCalledTimes(1);
    expect(screen.getByPlaceholderText("Enter terminal password")).toBeTruthy();

    await act(async () => {
      resolveStatus({
        connected: true,
        live: false,
        feed_state: "LIVE",
      });
      await flushMicrotasks();
    });
    expect(screen.getByPlaceholderText("Enter terminal password")).toBeTruthy();
  });

  it("serializes handleLogin behind an in-flight logout() request (P1-R12-003)", async () => {
    /**
     * Contract: When user clicks Logout and immediately submits Login while logout() is still
     * pending, handleLogin must await pendingLogoutPromiseRef before calling login() and only
     * proceed to bootstrapWorkspace after logout() settles.
     * Defect caught: Concurrent login() racing with in-flight logout() and losing new session/CSRF.
     */
    fetchSessionMock.mockResolvedValue({
      authenticated: true,
      csrf_token: "csrf-token-session-1",
    });
    fetchStatusMock.mockResolvedValue({
      connected: true,
      live: false,
      feed_state: "LIVE",
    });
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT]);
    fetchContractsMock.mockResolvedValue({
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

    let resolveLogout!: (value: { authenticated: boolean }) => void;
    logoutMock.mockImplementationOnce(
      () =>
        new Promise<{ authenticated: boolean }>((resolve) => {
          resolveLogout = resolve;
        }),
    );
    loginMock.mockResolvedValueOnce({
      authenticated: true,
      csrf_token: "csrf-token-session-2",
    });

    render(<App />);
    await act(async () => {
      await flushMicrotasks();
    });

    expect(screen.getByText("CON.F.US.MES.H26")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Logout" }));
      await flushMicrotasks();
    });
    expect(logoutMock).toHaveBeenCalledTimes(1);
    expect(screen.getByPlaceholderText("Enter terminal password")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Terminal password"), {
      target: { value: "terminal-secret" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Unlock Terminal" }));
      await flushMicrotasks();
    });

    expect(loginMock).not.toHaveBeenCalled();

    await act(async () => {
      resolveLogout({ authenticated: false });
      await flushMicrotasks();
    });

    expect(loginMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText("CON.F.US.MES.H26")).toBeTruthy();
  });

  it("guards in-memory CSRF token with monotonic authGeneration across logout/login/clearCsrfToken (P1-R12-003)", async () => {
    /**
     * Contract: client.ts sends the existing X-CSRF-Token on POST /api/auth/logout, prevents an
     * older in-flight logout() finally block from clearing a newer login()'s CSRF token, and
     * clearCsrfToken() prevents a stale in-flight fetchSession() from restoring CSRF memory.
     * Defect caught: Overlapping auth calls or stale logout finally clobbering newer CSRF token.
     */
    const actualClient = await vi.importActual<typeof import("./api/client")>("./api/client");
    actualClient.clearCsrfToken();

    let resolveLogoutFetch!: (res: Response) => void;
    let resolveStaleSessionFetch!: (res: Response) => void;
    const seenHeaders: Array<{ url: string; csrf: string | null }> = [];

    const fetchStub = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const headers = (init?.headers ?? {}) as Record<string, string>;
      seenHeaders.push({ url, csrf: headers["X-CSRF-Token"] ?? null });

      if (url === "/api/auth/login") {
        const body = JSON.parse(String(init?.body ?? "{}")) as { password?: string };
        const token = body.password === "pw-new" ? "csrf-new" : "csrf-old";
        return Promise.resolve(
          new Response(JSON.stringify({ authenticated: true, csrf_token: token }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (url === "/api/auth/logout") {
        return new Promise<Response>((resolve) => {
          resolveLogoutFetch = resolve;
        });
      }
      if (url === "/api/auth/session") {
        return new Promise<Response>((resolve) => {
          resolveStaleSessionFetch = resolve;
        });
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });
    vi.stubGlobal("fetch", fetchStub);

    await actualClient.login("pw-old");
    expect(actualClient.getCsrfToken()).toBe("csrf-old");

    const pendingLogout = actualClient.logout();
    expect(seenHeaders.find((h) => h.url === "/api/auth/logout")?.csrf).toBe("csrf-old");

    // Newer login completes while older logout is still in flight
    await actualClient.login("pw-new");
    expect(actualClient.getCsrfToken()).toBe("csrf-new");

    resolveLogoutFetch(
      new Response(JSON.stringify({ authenticated: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await pendingLogout;
    expect(actualClient.getCsrfToken()).toBe("csrf-new");

    // Stale fetchSession cannot restore token after clearCsrfToken()
    const pendingSession = actualClient.fetchSession();
    actualClient.clearCsrfToken();
    expect(actualClient.getCsrfToken()).toBeNull();

    resolveStaleSessionFetch(
      new Response(JSON.stringify({ authenticated: true, csrf_token: "csrf-stale" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await pendingSession;
    expect(actualClient.getCsrfToken()).toBeNull();
  });

  it("invalidates session, disconnects WS, and renders login gate on WS close 1008 (P1-R12-004)", async () => {
    /**
     * Contract: When wsClient emits onSessionExpired (from WS close 1008 "Session expired"),
     * App calls invalidateSessionLocally, disconnects WS, clears workspace state, and shows
     * the login gate.
     * Defect caught: WS close 1008 leaving authenticated workspace open until a manual action.
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
    fetchContractsMock.mockResolvedValue({
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

    act(() => {
      for (const listener of sessionExpiredListeners) {
        listener("Session expired");
      }
    });

    expect(screen.getByPlaceholderText("Enter terminal password")).toBeTruthy();
    expect(screen.queryByText("CON.F.US.MES.H26")).toBeNull();
    expect(mockWsDisconnect).toHaveBeenCalledTimes(1);
  });

  it("invalidates session, disconnects WS, and schedules zero retries when ChartPane fetchBars fails with 401 (P1-R12-001)", async () => {
    /**
     * Contract: When bootstrap succeeds and ChartPane's fetchBars rejects with ApiError(401),
     * App invalidates the session via onAuthError, renders the login screen, disconnects WS,
     * and fires zero fetchBars retries when timers advance.
     * Defect caught: Pane /api/bars 401 leaving App authenticated and retrying bars indefinitely.
     */
    vi.useFakeTimers();
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
    fetchContractsMock.mockResolvedValue({
      live: false,
      contracts: {
        MES: {
          selected: { id: "CON.F.US.MES.H26" },
          error_type: null,
          error: null,
        },
      },
    });
    fetchBarsMock.mockRejectedValueOnce(new ApiError(401, "Session expired"));

    render(<App />);
    await act(async () => {
      await flushMicrotasks();
    });

    expect(fetchBarsMock).toHaveBeenCalledTimes(1);
    expect(screen.getByPlaceholderText("Enter terminal password")).toBeTruthy();
    expect(mockWsDisconnect).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
      await flushMicrotasks();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(1);
  });

  it("transitions to login gate on pre-open WS reconnect close 1006 when fetchSession returns 401 (P1-R13-001, P2-R13-001)", async () => {
    /**
     * Contract: When an authenticated session loses WS and the reconnect socket closes with 1006
     * before open (pre-accept HTTP 403), App's MarketWsClient verifySession calls fetchSession(signal);
     * on ApiError(401), App transitions to the login gate, clears workspace state, disconnects WS,
     * and schedules zero further WS reconnects.
     * Defect caught: Pre-open WS close 1006 looping reconnects forever without checking HTTP session.
     */
    class FakeWebSocket {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSING = 2;
      static readonly CLOSED = 3;
      static instances: FakeWebSocket[] = [];

      readonly url: string;
      readyState = FakeWebSocket.CONNECTING;
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

      send(): void {}

      close(): void {
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

    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeWebSocket);
    useRealWsClient = true;

    fetchSessionMock
      .mockResolvedValueOnce({
        authenticated: true,
        csrf_token: "csrf-token-1234567890",
      })
      .mockRejectedValueOnce(new ApiError(401, "Session expired"));
    fetchStatusMock.mockResolvedValue({
      connected: true,
      live: false,
      feed_state: "LIVE",
    });
    listLayoutsMock.mockResolvedValue([SAMPLE_LAYOUT]);
    fetchContractsMock.mockResolvedValue({
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
    expect(FakeWebSocket.instances).toHaveLength(1);
    const ws1 = FakeWebSocket.instances[0]!;

    // Open initial socket, then simulate unexpected drop after open
    act(() => {
      ws1.emitOpen();
    });
    act(() => {
      ws1.emitClose(1006, "network_drop");
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
      await flushMicrotasks();
    });
    expect(FakeWebSocket.instances).toHaveLength(2);
    const ws2 = FakeWebSocket.instances[1]!;

    // Reconnect socket closes before open (browser close 1006 on HTTP 403 upgrade rejection)
    await act(async () => {
      ws2.emitClose(1006, "");
      await flushMicrotasks();
    });

    expect(fetchSessionMock).toHaveBeenCalledTimes(2);
    expect(screen.getByPlaceholderText("Enter terminal password")).toBeTruthy();
    expect(screen.queryByText("CON.F.US.MES.H26")).toBeNull();
    expect(mockWsDisconnect).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
      await flushMicrotasks();
    });
    expect(FakeWebSocket.instances).toHaveLength(2);
  });
});
