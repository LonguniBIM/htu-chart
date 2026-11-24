// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FetchBarsParams } from "../api/client";
import type {
  BarsResponse,
  OhlcvBar,
  PaneConfig,
  Timeframe,
  WsBarUpdateMessage,
  WsResyncRequiredMessage,
} from "../api/types";
import type { MarketWsClient } from "../api/wsClient";
import { ChartPane } from "./ChartPane";
import { PaneFetchCoordinator } from "./fetchGuard";

const candleSetData = vi.fn();
const candleUpdate = vi.fn();
const volumeSetData = vi.fn();
const volumeUpdate = vi.fn();
const fitContentMock = vi.fn();

vi.mock("lightweight-charts", () => {
  const CandlestickSeries = Symbol("CandlestickSeries");
  const HistogramSeries = Symbol("HistogramSeries");
  return {
    CandlestickSeries,
    HistogramSeries,
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    createChart: vi.fn(() => ({
      addSeries: vi.fn((seriesType: unknown) => {
        if (seriesType === CandlestickSeries) {
          return {
            setData: candleSetData,
            update: candleUpdate,
            priceScale: () => ({ applyOptions: vi.fn() }),
          };
        }
        return {
          setData: volumeSetData,
          update: volumeUpdate,
          priceScale: () => ({ applyOptions: vi.fn() }),
        };
      }),
      subscribeCrosshairMove: vi.fn(),
      unsubscribeCrosshairMove: vi.fn(),
      applyOptions: vi.fn(),
      remove: vi.fn(),
      timeScale: () => ({ fitContent: fitContentMock }),
    })),
  };
});

const fetchBarsMock = vi.fn<
  (params: FetchBarsParams, signal?: AbortSignal) => Promise<BarsResponse>
>();

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    fetchBars: (params: FetchBarsParams, signal?: AbortSignal) =>
      fetchBarsMock(params, signal),
  };
});

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

function createDeferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makeBar(time: number, close: number): OhlcvBar {
  return {
    time,
    open: close - 1,
    high: close + 1,
    low: close - 2,
    close,
    volume: 25,
    is_closed: true,
  };
}

function makeBarsResponse(contractId: string, bars: OhlcvBar[]): BarsResponse {
  return {
    provider: "topstepx",
    live: false,
    contract_id: contractId,
    root_symbol: "MES",
    timeframe: "5m",
    start_time: bars[0]?.time ?? 0,
    end_time: bars[bars.length - 1]?.time ?? 0,
    count: bars.length,
    include_partial: true,
    quality: {
      is_complete: true,
      truncated: false,
      provider_gap: false,
      quarantined_count: 0,
    },
    bars,
  };
}

function createFakeWsClient() {
  const barListeners = new Set<(msg: WsBarUpdateMessage) => void>();
  const resyncListeners = new Set<(msg: WsResyncRequiredMessage) => void>();
  const subscribe = vi.fn<(contractId: string, timeframe: Timeframe) => () => void>(
    () => vi.fn(),
  );

  const stub = {
    subscribe,
    onBarUpdate: (listener: (msg: WsBarUpdateMessage) => void) => {
      barListeners.add(listener);
      return () => {
        barListeners.delete(listener);
      };
    },
    onResyncRequired: (listener: (msg: WsResyncRequiredMessage) => void) => {
      resyncListeners.add(listener);
      return () => {
        resyncListeners.delete(listener);
      };
    },
  };

  return {
    client: stub as unknown as MarketWsClient,
    subscribe,
    emitBarUpdate: (msg: WsBarUpdateMessage) => {
      for (const listener of barListeners) {
        listener(msg);
      }
    },
  };
}

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

describe("ChartPane (F06, P1-R3-01, P1-R3-02)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchBarsMock.mockReset();
    vi.stubGlobal("ResizeObserver", MockResizeObserver);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("clears candle and volume series immediately on MES -> MNQ switch before fetchBars resolves", async () => {
    /**
     * Contract: Switching a mounted ChartPane from MES to MNQ immediately clears
     * candleSeries and volumeSeries with setData([]) before the new fetchBars resolves.
     * Defect caught: Prior symbol's bars lingering on screen during slow/pending switch fetches.
     */
    const mesFetch = createDeferred<BarsResponse>();
    const mnqFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(mesFetch.promise).mockReturnValueOnce(mnqFetch.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const mesPane: PaneConfig = {
      pane_id: "pane-1",
      root_symbol: "MES",
      timeframe: "5m",
      live_mode: false,
      playback_mode: "live",
    };

    const { rerender } = render(
      <ChartPane
        pane={mesPane}
        contractId="CON.F.US.MES.H26"
        activeFeedLive={false}
        wsClient={fakeWs.client}
        coordinator={coordinator}
      />,
    );

    await act(async () => {
      mesFetch.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000)]));
      await mesFetch.promise;
    });

    expect(candleSetData).toHaveBeenCalledTimes(2);
    candleSetData.mockClear();
    volumeSetData.mockClear();

    const mnqPane: PaneConfig = { ...mesPane, root_symbol: "MNQ" };
    rerender(
      <ChartPane
        pane={mnqPane}
        contractId="CON.F.US.MNQ.H26"
        activeFeedLive={false}
        wsClient={fakeWs.client}
        coordinator={coordinator}
      />,
    );

    expect(candleSetData).toHaveBeenCalledWith([]);
    expect(volumeSetData).toHaveBeenCalledWith([]);
    expect(fetchBarsMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      mnqFetch.resolve(makeBarsResponse("CON.F.US.MNQ.H26", [makeBar(2000, 18000)]));
      await mnqFetch.promise;
    });
  });

  it("buffers WS bar_update while fetchBars is pending and reconciles on resolve", async () => {
    /**
     * Contract: WS bar_update messages arriving while fetchBars is in flight are buffered
     * (not applied prematurely via series.update) and merged into candleSeries.setData on resolve.
     * Defect caught: Real-time ticks dropped or misordered while REST snapshot is in flight.
     */
    const pendingFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(pendingFetch.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const pane: PaneConfig = {
      pane_id: "pane-1",
      root_symbol: "MES",
      timeframe: "5m",
      live_mode: false,
      playback_mode: "live",
    };

    render(
      <ChartPane
        pane={pane}
        contractId="CON.F.US.MES.H26"
        activeFeedLive={false}
        wsClient={fakeWs.client}
        coordinator={coordinator}
      />,
    );

    expect(fakeWs.subscribe).toHaveBeenCalledWith("CON.F.US.MES.H26", "5m");

    fakeWs.emitBarUpdate({
      type: "bar_update",
      contract_id: "CON.F.US.MES.H26",
      live_mode: false,
      timeframe: "5m",
      is_recovered_historical: false,
      recovery_tag: null,
      bar: makeBar(2000, 5025),
    });

    // Cross-contract bar must be ignored by matchesWsBarTarget
    fakeWs.emitBarUpdate({
      type: "bar_update",
      contract_id: "CON.F.US.MNQ.H26",
      live_mode: false,
      timeframe: "5m",
      is_recovered_historical: false,
      recovery_tag: null,
      bar: makeBar(2100, 18000),
    });

    expect(candleUpdate).not.toHaveBeenCalled();

    await act(async () => {
      pendingFetch.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000)]));
      await pendingFetch.promise;
    });

    const lastCallBars = candleSetData.mock.calls[candleSetData.mock.calls.length - 1]?.[0] as
      | Array<{ time: number; close: number }>
      | undefined;
    expect(lastCallBars?.map((b) => b.time)).toEqual([1000, 2000]);
    expect(lastCallBars?.map((b) => b.time)).toContain(2000);
  });

  it("skips WS subscription and shows Hist only status when pane.live_mode !== activeFeedLive", async () => {
    /**
     * Contract: When activeFeedLive is known and differs from pane.live_mode, ChartPane must
     * NOT subscribe to /ws/market and must display an explicit Hist only status in the header.
     * Defect caught: Silent WS subscription mismatch leaving panes waiting indefinitely for live bars.
     */
    const pendingFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(pendingFetch.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const livePane: PaneConfig = {
      pane_id: "pane-1",
      root_symbol: "MES",
      timeframe: "5m",
      live_mode: true,
      playback_mode: "live",
    };

    render(
      <ChartPane
        pane={livePane}
        contractId="CON.F.US.MES.H26"
        activeFeedLive={false}
        wsClient={fakeWs.client}
        coordinator={coordinator}
      />,
    );

    expect(fakeWs.subscribe).not.toHaveBeenCalled();
    expect(screen.getByText(/Hist only \(feed: Sim\)/)).toBeTruthy();

    await act(async () => {
      pendingFetch.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000)]));
      await pendingFetch.promise;
    });

    expect(screen.getByText(/Hist only \(feed: Sim\) · 1 bars/)).toBeTruthy();
  });
});
