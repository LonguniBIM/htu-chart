// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
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
    cleanup();
    vi.unstubAllGlobals();
  });

  it("retries GET /api/contracts automatically when feed_status transitions to LIVE after initial 503", async () => {
    /**
     * Contract: When initial /api/contracts returns 503 during TopstepX startup auth, App connects
     * WS, exposes manual Retry Contracts, and automatically retries /api/contracts + /api/status
     * once feed_status reports LIVE so charts resolve and load bars.
     * Defect caught: Panes stuck permanently on Unresolved after startup 503 race (P1-R3-03, P2-005).
     */
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
        connected: true,
        live: false,
        feed_state: "LIVE",
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
      for (const listener of feedStatusListeners) {
        listener({ type: "feed_status", feed_state: "LIVE" });
      }
      await flushMicrotasks();
    });

    expect(fetchContractsMock).toHaveBeenCalledTimes(2);
    expect(screen.getByText("CON.F.US.MES.H26")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry Contracts" })).toBeNull();
    expect(fetchBarsMock).toHaveBeenCalled();
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
});
