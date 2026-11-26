// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type FetchBarsParams } from "../api/client";
import type {
  BarsResponse,
  DrawingCommandRequest,
  DrawingCommandResponse,
  DrawingScopeResponse,
  OhlcvBar,
  PaneConfig,
  RootSymbol,
  Timeframe,
  WsBarUpdateMessage,
  WsResyncRequiredMessage,
  WsSubscribedMessage,
} from "../api/types";
import type { MarketWsClient, WsConnectionState } from "../api/wsClient";
import { LayoutSyncController } from "../layout/syncController";
import { buildDrawingScopeKey, useDrawingStore } from "../store/drawingStore";
import { ChartPane } from "./ChartPane";
import { PaneFetchCoordinator } from "./fetchGuard";

const candleSetData = vi.fn();
const candleUpdate = vi.fn();
const volumeSetData = vi.fn();
const volumeUpdate = vi.fn();
const fitContentMock = vi.fn();
const applyOptionsMock = vi.fn();
const attachPrimitiveMock = vi.fn();
const detachPrimitiveMock = vi.fn();
const priceToCoordinateMock = vi.fn<(price: number) => number | null>((price) => (5050 - price) * 4);
const coordinateToPriceMock = vi.fn<(y: number) => number | null>((y) => 5050 - y / 4);
const timeToCoordinateMock = vi.fn<(time: number) => number | null>((time) => (time - 1700000000) / 10 + 100);
const coordinateToTimeMock = vi.fn<(x: number) => number | null>(() => null);
const logicalToCoordinateMock = vi.fn<(logical: number) => number | null>((logical) => 100 + logical * 30);
const coordinateToLogicalMock = vi.fn<(x: number) => number | null>((x) => (x - 100) / 30);
const timeScaleWidthMock = vi.fn<() => number>(() => 530);
const timeScaleHeightMock = vi.fn<() => number>(() => 28);
const setCrosshairPositionMock = vi.fn();
const clearCrosshairPositionMock = vi.fn();
const getVisibleRangeMock = vi.fn<() => { from: number; to: number } | null>(() => null);
const setVisibleRangeMock = vi.fn<(range: { from: number; to: number }) => void>();
let crosshairMoveHandlers: Array<(param: unknown) => void> = [];
let clickHandlers: Array<(param: unknown) => void> = [];
let visibleRangeHandlers: Array<(range: { from: number; to: number } | null) => void> = [];

vi.mock("lightweight-charts", () => {
  const CandlestickSeries = Symbol("CandlestickSeries");
  const HistogramSeries = Symbol("HistogramSeries");
  return {
    CandlestickSeries,
    HistogramSeries,
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    createChart: vi.fn(() => {
      let candleSeriesApi: unknown;
      const timeScaleApi = {
        fitContent: fitContentMock,
        width: timeScaleWidthMock,
        height: timeScaleHeightMock,
        timeToCoordinate: timeToCoordinateMock,
        coordinateToTime: coordinateToTimeMock,
        logicalToCoordinate: logicalToCoordinateMock,
        coordinateToLogical: coordinateToLogicalMock,
        subscribeVisibleTimeRangeChange: vi.fn(
          (handler: (range: { from: number; to: number } | null) => void) => {
            visibleRangeHandlers.push(handler);
          },
        ),
        unsubscribeVisibleTimeRangeChange: vi.fn(
          (handler: (range: { from: number; to: number } | null) => void) => {
            visibleRangeHandlers = visibleRangeHandlers.filter((h) => h !== handler);
          },
        ),
        getVisibleRange: getVisibleRangeMock,
        setVisibleRange: setVisibleRangeMock,
      };
      const chartApi = {
        addSeries: vi.fn((seriesType: unknown) => {
          if (seriesType === CandlestickSeries) {
            candleSeriesApi = {
              setData: candleSetData,
              update: candleUpdate,
              attachPrimitive: vi.fn((primitive: { attached?: (param: unknown) => void }) => {
                attachPrimitiveMock(primitive);
                primitive.attached?.({
                  chart: chartApi,
                  series: candleSeriesApi,
                  requestUpdate: vi.fn(),
                  horzScaleBehavior: {},
                });
              }),
              detachPrimitive: vi.fn((primitive: { detached?: () => void }) => {
                detachPrimitiveMock(primitive);
                primitive.detached?.();
              }),
              priceToCoordinate: priceToCoordinateMock,
              coordinateToPrice: coordinateToPriceMock,
              priceScale: () => ({ applyOptions: vi.fn() }),
            };
            return candleSeriesApi;
          }
          return {
            setData: volumeSetData,
            update: volumeUpdate,
            priceScale: () => ({ applyOptions: vi.fn() }),
          };
        }),
        subscribeCrosshairMove: vi.fn((handler: (param: unknown) => void) => {
          crosshairMoveHandlers.push(handler);
        }),
        unsubscribeCrosshairMove: vi.fn((handler: (param: unknown) => void) => {
          crosshairMoveHandlers = crosshairMoveHandlers.filter((h) => h !== handler);
        }),
        subscribeClick: vi.fn((handler: (param: unknown) => void) => {
          clickHandlers.push(handler);
        }),
        unsubscribeClick: vi.fn((handler: (param: unknown) => void) => {
          clickHandlers = clickHandlers.filter((h) => h !== handler);
        }),
        setCrosshairPosition: setCrosshairPositionMock,
        clearCrosshairPosition: clearCrosshairPositionMock,
        applyOptions: applyOptionsMock,
        remove: vi.fn(),
        timeScale: () => timeScaleApi,
      };
      return chartApi;
    }),
  };
});

const fetchBarsMock = vi.fn<
  (params: FetchBarsParams, signal?: AbortSignal) => Promise<BarsResponse>
>();
const fetchDrawingsMock = vi.fn<
  (
    symbol: RootSymbol,
    params?: { contract_id?: string | null; pane_id?: string | null },
    signal?: AbortSignal,
  ) => Promise<DrawingScopeResponse>
>();
const postDrawingCommandMock = vi.fn<
  (
    symbol: RootSymbol,
    command: DrawingCommandRequest,
    signal?: AbortSignal,
  ) => Promise<DrawingCommandResponse>
>();

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    fetchBars: (params: FetchBarsParams, signal?: AbortSignal) =>
      fetchBarsMock(params, signal),
    fetchDrawings: (
      symbol: RootSymbol,
      params?: { contract_id?: string | null; pane_id?: string | null },
      signal?: AbortSignal,
    ) => fetchDrawingsMock(symbol, params, signal),
    postDrawingCommand: (
      symbol: RootSymbol,
      command: DrawingCommandRequest,
      signal?: AbortSignal,
    ) => postDrawingCommandMock(symbol, command, signal),
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

function makeBar(time: number, close: number, isClosed = true, volume = 25): OhlcvBar {
  return {
    time,
    open: close - 1,
    high: close + 1,
    low: close - 2,
    close,
    volume,
    is_closed: isClosed,
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
  const subscribedListeners = new Set<(msg: WsSubscribedMessage) => void>();
  const connectionStateListeners = new Set<(state: WsConnectionState) => void>();
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
    onSubscribed: (listener: (msg: WsSubscribedMessage) => void) => {
      subscribedListeners.add(listener);
      return () => {
        subscribedListeners.delete(listener);
      };
    },
    onConnectionState: (listener: (state: WsConnectionState) => void) => {
      connectionStateListeners.add(listener);
      return () => {
        connectionStateListeners.delete(listener);
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
    emitSubscribed: (msg: WsSubscribedMessage) => {
      for (const listener of subscribedListeners) {
        listener(msg);
      }
    },
    emitResyncRequired: (msg: WsResyncRequiredMessage) => {
      for (const listener of resyncListeners) {
        listener(msg);
      }
    },
    emitConnectionState: (state: WsConnectionState) => {
      for (const listener of connectionStateListeners) {
        listener(state);
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
    fetchDrawingsMock.mockReset();
    postDrawingCommandMock.mockReset();
    fetchDrawingsMock.mockResolvedValue({
      symbol: "MES",
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      instrument: {
        root_symbol: "MES",
        name: "Micro E-mini S&P 500",
        exchange: "CME",
        tick_size: 0.25,
        tick_value: 1.25,
        currency: "USD",
      },
      scope_revision: 0,
      can_undo: false,
      can_redo: false,
      drawings: [],
    });
    useDrawingStore.getState().setAuthErrorHandler(null);
    useDrawingStore.getState().reset();
    crosshairMoveHandlers = [];
    clickHandlers = [];
    visibleRangeHandlers = [];
    getVisibleRangeMock.mockReset();
    getVisibleRangeMock.mockReturnValue(null);
    setVisibleRangeMock.mockReset();
    setCrosshairPositionMock.mockReset();
    clearCrosshairPositionMock.mockReset();
    vi.stubGlobal("ResizeObserver", MockResizeObserver);
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
    useDrawingStore.getState().reset();
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

  it("triggers a second fetchBars resync when >500 distinct-timestamp WS bars overflow buffer while initial fetch is pending (P1-R3-01)", async () => {
    /**
     * Contract: When more than MAX_PENDING_WS_BARS (500) distinct-timestamp bar_update frames
     * arrive while fetchBars is pending, resolving fetchBars detects bufferOverflowRef and
     * increments resyncNonce to trigger a second fetchBars snapshot request.
     * Defect caught: Silent WS bar eviction without resync when buffer overflows during REST fetch.
     */
    const firstFetch = createDeferred<BarsResponse>();
    const resyncFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(firstFetch.promise).mockReturnValueOnce(resyncFetch.promise);

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

    expect(fetchBarsMock).toHaveBeenCalledTimes(1);

    for (let i = 0; i < 502; i += 1) {
      fakeWs.emitBarUpdate({
        type: "bar_update",
        contract_id: "CON.F.US.MES.H26",
        live_mode: false,
        timeframe: "5m",
        is_recovered_historical: false,
        recovery_tag: null,
        bar: makeBar(2000 + i * 300, 5000 + i),
      });
    }

    await act(async () => {
      firstFetch.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 4999)]));
      await firstFetch.promise;
    });

    expect(fetchBarsMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      resyncFetch.resolve(
        makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 4999), makeBar(152300, 5501)]),
      );
      await resyncFetch.promise;
    });

    expect(screen.getByText("2 bars")).toBeTruthy();
  });

  it("ignores late-resolving MES fetchBars response after MNQ switch even if AbortSignal is ignored (P1-03)", async () => {
    /**
     * Contract: Even when an underlying transport ignores AbortSignal, resolving a stale MES
     * fetchBars promise after switching to MNQ and applying mnqBars must be rejected by
     * coordinator.isResponseCurrent and never overwrite candleSeries or volumeSeries.
     * Defect caught: Out-of-order late fetchBars response overwriting newer symbol chart data.
     */
    const mesDeferred = createDeferred<BarsResponse>();
    const mnqBars = [makeBar(2000, 20001)];
    const mesBars = [makeBar(1000, 5001)];

    fetchBarsMock
      .mockImplementationOnce(() => mesDeferred.promise)
      .mockImplementationOnce(() => Promise.resolve(makeBarsResponse("CON.F.US.MNQ.H26", mnqBars)));

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

    const mnqPane: PaneConfig = { ...mesPane, root_symbol: "MNQ" };
    await act(async () => {
      rerender(
        <ChartPane
          pane={mnqPane}
          contractId="CON.F.US.MNQ.H26"
          activeFeedLive={false}
          wsClient={fakeWs.client}
          coordinator={coordinator}
        />,
      );
      await Promise.resolve();
    });

    await act(async () => {
      mesDeferred.resolve(makeBarsResponse("CON.F.US.MES.H26", mesBars));
      await mesDeferred.promise;
    });

    const lastCandleCall = candleSetData.mock.calls[candleSetData.mock.calls.length - 1]?.[0] as
      | Array<{ time: number; close: number }>
      | undefined;
    const lastVolumeCall = volumeSetData.mock.calls[volumeSetData.mock.calls.length - 1]?.[0] as
      | Array<{ time: number; value: number }>
      | undefined;
    const lastSetDataPrices = lastCandleCall?.map((bar) => bar.close);
    expect(lastSetDataPrices).toEqual([20001]);
    expect(lastVolumeCall?.map((bar) => bar.time)).toEqual([2000]);
  });

  it("skips WS subscription and shows Hist only status for 3m timeframe (P1-01)", async () => {
    /**
     * Contract: Non-aggregated timeframes (3m, 30m) must not subscribe over /ws/market and must
     * display explicit "Hist only (3m no live feed)" status alongside bar count.
     * Defect caught: Subscribing to unsupported 3m/30m live feeds causing server WS error frames.
     */
    const pendingFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(pendingFetch.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const pane3m: PaneConfig = {
      pane_id: "pane-1",
      root_symbol: "MES",
      timeframe: "3m",
      live_mode: false,
      playback_mode: "live",
    };

    render(
      <ChartPane
        pane={pane3m}
        contractId="CON.F.US.MES.H26"
        activeFeedLive={false}
        wsClient={fakeWs.client}
        coordinator={coordinator}
      />,
    );

    expect(fakeWs.subscribe).not.toHaveBeenCalled();
    expect(screen.getByText(/Hist only \(3m no live feed\)/)).toBeTruthy();

    await act(async () => {
      pendingFetch.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000)]));
      await pendingFetch.promise;
    });

    expect(screen.getByText(/Hist only \(3m no live feed\) · 1 bars/)).toBeTruthy();
  });

  it("ignores delayed provisional WS tick when latest chart bar at same timestamp is already closed (P2-001)", async () => {
    /**
     * Contract: When the latest applied bar at timestamp T has is_closed=true, an inbound
     * provisional bar_update at timestamp T (is_closed=false) must be ignored.
     * Defect caught: Stale provisional WS bar overwriting a finalized closed bar on live chart.
     */
    const snapshotFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(snapshotFetch.promise);

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

    await act(async () => {
      snapshotFetch.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5008, true)]));
      await snapshotFetch.promise;
    });

    fakeWs.emitBarUpdate({
      type: "bar_update",
      contract_id: "CON.F.US.MES.H26",
      live_mode: false,
      timeframe: "5m",
      is_recovered_historical: false,
      recovery_tag: null,
      bar: makeBar(1000, 5001, false),
    });

    expect(candleUpdate).not.toHaveBeenCalled();
    expect(volumeUpdate).not.toHaveBeenCalled();
  });

  it("triggers a post-ACK fetchBars snapshot resync when subscribed ACK arrives after initial load (P2-001)", async () => {
    /**
     * Contract: When initial fetchBars resolves with [1000] before the server's WS "subscribed"
     * ACK arrives, receiving { type: "subscribed", contract_id, timeframe } for this pane triggers
     * a second fetchBars snapshot and applies [1000, 1060] to candleSeries.setData.
     * Defect caught: Bar gap between initial REST snapshot and server WS subscription registration.
     */
    const firstFetch = createDeferred<BarsResponse>();
    const resyncFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(firstFetch.promise).mockReturnValueOnce(resyncFetch.promise);

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

    expect(fetchBarsMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      firstFetch.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000)]));
      await firstFetch.promise;
    });

    // Unrelated contract/timeframe subscribed ACKs must not trigger resync
    act(() => {
      fakeWs.emitSubscribed({
        type: "subscribed",
        contract_id: "CON.F.US.MNQ.H26",
        timeframe: "5m",
      });
      fakeWs.emitSubscribed({
        type: "subscribed",
        contract_id: "CON.F.US.MES.H26",
        timeframe: "15m",
      });
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(1);

    // Matching subscribed ACK triggers second snapshot fetch
    act(() => {
      fakeWs.emitSubscribed({
        type: "subscribed",
        contract_id: "CON.F.US.MES.H26",
        timeframe: "5m",
      });
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      resyncFetch.resolve(
        makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000), makeBar(1060, 5012)]),
      );
      await resyncFetch.promise;
    });

    const lastCallBars = candleSetData.mock.calls[candleSetData.mock.calls.length - 1]?.[0] as
      | Array<{ time: number; close: number }>
      | undefined;
    expect(lastCallBars?.map((b) => b.time)).toEqual([1000, 1060]);
    expect(screen.getByText("2 bars")).toBeTruthy();
  });

  it("keeps live bar_update unblocked and retries after post-ACK resync fetchBars rejection (P1-R6-02, P2-002)", async () => {
    /**
     * Contract: When initial snapshot succeeds ([1000]) and a subsequent post-ACK resync fetchBars
     * rejects, ChartPane restores snapshotReadyGenRef so live bar_update (1060) immediately updates
     * candleSeries.update(...) rather than freezing in the buffer, surfaces a Retry button, and
     * schedules a bounded retry via scheduleRetry that recovers on a 3rd fetchBars call.
     * Defect caught: Post-ACK resync failure leaving snapshotReadyGenRef != activeGenerationRef forever.
     */
    vi.useFakeTimers();
    const firstFetch = createDeferred<BarsResponse>();
    const failedResyncFetch = createDeferred<BarsResponse>();
    const retryResyncFetch = createDeferred<BarsResponse>();
    fetchBarsMock
      .mockReturnValueOnce(firstFetch.promise)
      .mockReturnValueOnce(failedResyncFetch.promise)
      .mockReturnValueOnce(retryResyncFetch.promise);

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

    await act(async () => {
      firstFetch.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000)]));
      await firstFetch.promise;
    });

    // Subscribed ACK triggers 2nd fetchBars resync
    act(() => {
      fakeWs.emitSubscribed({
        type: "subscribed",
        contract_id: "CON.F.US.MES.H26",
        timeframe: "5m",
      });
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(2);

    // 2nd fetchBars rejects with transient error
    await act(async () => {
      failedResyncFetch.reject(new Error("Transient upstream 503"));
      await Promise.resolve();
    });

    expect(screen.getByText(/resync error: Transient upstream 503/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();

    // Live bar_update after failed resync must apply immediately (not freeze in buffer)
    fakeWs.emitBarUpdate({
      type: "bar_update",
      contract_id: "CON.F.US.MES.H26",
      live_mode: false,
      timeframe: "5m",
      is_recovered_historical: false,
      recovery_tag: null,
      bar: makeBar(1060, 5015),
    });

    expect(candleUpdate).toHaveBeenCalledWith(
      expect.objectContaining({ time: 1060, close: 5015 }),
    );
    expect(volumeUpdate).toHaveBeenCalledWith(
      expect.objectContaining({ time: 1060, value: 25 }),
    );

    // Advancing fake timers past 1500ms backoff triggers 3rd fetchBars retry
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(3);

    await act(async () => {
      retryResyncFetch.resolve(
        makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000), makeBar(1060, 5015)]),
      );
      await retryResyncFetch.promise;
    });

    expect(screen.getByText("2 bars")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("flushes buffered WS bars in ascending order with closed-bar priority when post-ACK resync fetchBars rejects", async () => {
    /**
     * Contract: When WS bar_update frames (including a closed bar, a stale provisional tick at
     * the same timestamp, and a newer bar) arrive while a post-ACK fetchBars resync is pending,
     * rejecting that resync flushes pendingWsBarsRef onto candleSeries.update in ascending timestamp
     * order while preserving the closed bar's OHLCV over the stale provisional tick.
     * Defect caught: Buffered WS bars dropped or overwritten by stale provisional ticks on resync rejection.
     */
    const firstFetch = createDeferred<BarsResponse>();
    const failedResyncFetch = createDeferred<BarsResponse>();
    fetchBarsMock
      .mockReturnValueOnce(firstFetch.promise)
      .mockReturnValueOnce(failedResyncFetch.promise);

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

    await act(async () => {
      firstFetch.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000, true)]));
      await firstFetch.promise;
    });

    act(() => {
      fakeWs.emitSubscribed({
        type: "subscribed",
        contract_id: "CON.F.US.MES.H26",
        timeframe: "5m",
      });
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(2);

    // Emit newer bar first, then closed bar at 1060, then stale provisional tick at 1060
    fakeWs.emitBarUpdate({
      type: "bar_update",
      contract_id: "CON.F.US.MES.H26",
      live_mode: false,
      timeframe: "5m",
      is_recovered_historical: false,
      recovery_tag: null,
      bar: makeBar(1120, 5030, false, 55),
    });
    fakeWs.emitBarUpdate({
      type: "bar_update",
      contract_id: "CON.F.US.MES.H26",
      live_mode: false,
      timeframe: "5m",
      is_recovered_historical: false,
      recovery_tag: null,
      bar: makeBar(1060, 5020, true, 84),
    });
    fakeWs.emitBarUpdate({
      type: "bar_update",
      contract_id: "CON.F.US.MES.H26",
      live_mode: false,
      timeframe: "5m",
      is_recovered_historical: false,
      recovery_tag: null,
      bar: makeBar(1060, 5010, false, 17),
    });

    expect(candleUpdate).not.toHaveBeenCalled();

    await act(async () => {
      failedResyncFetch.reject(new Error("Transient upstream 503"));
      await Promise.resolve();
    });

    expect(screen.getByText(/resync error: Transient upstream 503/)).toBeTruthy();
    expect(candleUpdate).toHaveBeenCalledTimes(2);
    expect(candleUpdate.mock.calls.map((call) => (call[0] as { time: number }).time)).toEqual([
      1060, 1120,
    ]);
    expect(candleUpdate.mock.calls[0]?.[0]).toEqual({
      time: 1060,
      open: 5019,
      high: 5021,
      low: 5018,
      close: 5020,
    });
    expect(candleUpdate.mock.calls[1]?.[0]).toEqual({
      time: 1120,
      open: 5029,
      high: 5031,
      low: 5028,
      close: 5030,
    });
    expect(volumeUpdate.mock.calls[0]?.[0]).toEqual(
      expect.objectContaining({ time: 1060, value: 84 }),
    );
    expect(volumeUpdate.mock.calls[1]?.[0]).toEqual(
      expect.objectContaining({ time: 1120, value: 55 }),
    );

    // Subsequent live update after flush continues applying immediately
    fakeWs.emitBarUpdate({
      type: "bar_update",
      contract_id: "CON.F.US.MES.H26",
      live_mode: false,
      timeframe: "5m",
      is_recovered_historical: false,
      recovery_tag: null,
      bar: makeBar(1180, 5040, false),
    });
    expect(candleUpdate).toHaveBeenCalledTimes(3);
    expect(candleUpdate.mock.calls[2]?.[0]).toEqual(
      expect.objectContaining({ time: 1180, close: 5040 }),
    );
  });

  it("enforces 1500ms -> 3000ms -> 6000ms automatic retry cap and recovers on manual Retry click", async () => {
    /**
     * Contract: Post-ACK resync rejections schedule automatic retries at [1500, 3000, 6000] ms,
     * stop after 3 automatic retries, and allow manual Retry click to reset the counter and recover.
     * Defect caught: Wrong backoff schedule, unbounded retry loop, or broken manual Retry recovery.
     */
    vi.useFakeTimers();
    const firstFetch = createDeferred<BarsResponse>();
    const resyncReject1 = createDeferred<BarsResponse>();
    const resyncReject2 = createDeferred<BarsResponse>();
    const resyncReject3 = createDeferred<BarsResponse>();
    const resyncReject4 = createDeferred<BarsResponse>();
    const manualSuccess = createDeferred<BarsResponse>();

    fetchBarsMock
      .mockReturnValueOnce(firstFetch.promise)
      .mockReturnValueOnce(resyncReject1.promise)
      .mockReturnValueOnce(resyncReject2.promise)
      .mockReturnValueOnce(resyncReject3.promise)
      .mockReturnValueOnce(resyncReject4.promise)
      .mockReturnValueOnce(manualSuccess.promise);

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

    await act(async () => {
      firstFetch.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000)]));
      await firstFetch.promise;
    });

    act(() => {
      fakeWs.emitSubscribed({
        type: "subscribed",
        contract_id: "CON.F.US.MES.H26",
        timeframe: "5m",
      });
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      resyncReject1.reject(new Error("fail-1"));
      await Promise.resolve();
    });

    // Retry 1 at 1500ms
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1499);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(3);

    await act(async () => {
      resyncReject2.reject(new Error("fail-2"));
      await Promise.resolve();
    });

    // Retry 2 at 3000ms
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2999);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(3);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(4);

    await act(async () => {
      resyncReject3.reject(new Error("fail-3"));
      await Promise.resolve();
    });

    // Retry 3 at 6000ms
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5999);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(4);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(5);

    await act(async () => {
      resyncReject4.reject(new Error("fail-4"));
      await Promise.resolve();
    });

    // Cap reached: advancing timer further triggers zero additional automatic fetches
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60000);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(5);

    // Manual Retry button triggers recovery fetch and clears Retry button
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(6);

    await act(async () => {
      manualSuccess.resolve(
        makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000), makeBar(1060, 5015)]),
      );
      await manualSuccess.promise;
    });

    expect(screen.getByText("2 bars")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("cancels pending retry timers on target change and unmount", async () => {
    /**
     * Contract: Pending snapshot retry timers scheduled by scheduleRetry must be cancelled when
     * the pane target changes (MES -> MNQ) or when ChartPane unmounts, triggering zero stale fetches.
     * Defect caught: Leaked retry timers firing stale fetchBars after symbol switch or unmount.
     */
    vi.useFakeTimers();
    const mesInitial = createDeferred<BarsResponse>();
    const mesResyncFail = createDeferred<BarsResponse>();
    const mnqInitial = createDeferred<BarsResponse>();
    const mnqResyncFail = createDeferred<BarsResponse>();

    fetchBarsMock
      .mockReturnValueOnce(mesInitial.promise)
      .mockReturnValueOnce(mesResyncFail.promise)
      .mockReturnValueOnce(mnqInitial.promise)
      .mockReturnValueOnce(mnqResyncFail.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const mesPane: PaneConfig = {
      pane_id: "pane-1",
      root_symbol: "MES",
      timeframe: "5m",
      live_mode: false,
      playback_mode: "live",
    };

    const { rerender, unmount } = render(
      <ChartPane
        pane={mesPane}
        contractId="CON.F.US.MES.H26"
        activeFeedLive={false}
        wsClient={fakeWs.client}
        coordinator={coordinator}
      />,
    );

    await act(async () => {
      mesInitial.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000)]));
      await mesInitial.promise;
    });

    act(() => {
      fakeWs.emitSubscribed({
        type: "subscribed",
        contract_id: "CON.F.US.MES.H26",
        timeframe: "5m",
      });
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      mesResyncFail.reject(new Error("mes-resync-fail"));
      await Promise.resolve();
    });
    expect(vi.getTimerCount()).toBe(1);

    // Switch target MES -> MNQ while 1500ms MES retry timer is pending
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
    expect(vi.getTimerCount()).toBe(0);
    expect(fetchBarsMock).toHaveBeenCalledTimes(3);

    await act(async () => {
      mnqInitial.resolve(makeBarsResponse("CON.F.US.MNQ.H26", [makeBar(2000, 18000)]));
      await mnqInitial.promise;
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(3);

    // Trigger pending MNQ resync retry timer, then unmount before timer fires
    act(() => {
      fakeWs.emitSubscribed({
        type: "subscribed",
        contract_id: "CON.F.US.MNQ.H26",
        timeframe: "5m",
      });
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(4);

    await act(async () => {
      mnqResyncFail.reject(new Error("mnq-resync-fail"));
      await Promise.resolve();
    });
    expect(vi.getTimerCount()).toBe(1);

    unmount();
    expect(vi.getTimerCount()).toBe(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15000);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(4);
  });

  it("displays BarsQuality warnings alongside valid bars and clears them on clean target switch (P2-R11-002)", async () => {
    /**
     * Contract: When GET /api/bars returns HTTP 200 with degraded quality flags
     * ({ is_complete: false, truncated: true, provider_gap: true, quarantined_count: 2 }),
     * ChartPane renders valid bars (candleSetData + bar count) AND renders a status warning pill
     * ("Warning: truncated, provider gap, 2 quarantined"), and clears the warning immediately on
     * target switch and when switching to a target with clean quality.
     * Defect caught: ChartPane discarding response.quality or leaking stale warnings across switches.
     */
    const mesFetch = createDeferred<BarsResponse>();
    const mnqFetch = createDeferred<BarsResponse>();
    const incompleteFetch = createDeferred<BarsResponse>();
    fetchBarsMock
      .mockReturnValueOnce(mesFetch.promise)
      .mockReturnValueOnce(mnqFetch.promise)
      .mockReturnValueOnce(incompleteFetch.promise);

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
      mesFetch.resolve({
        ...makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000), makeBar(1300, 5010)]),
        quality: {
          is_complete: false,
          truncated: true,
          provider_gap: true,
          quarantined_count: 2,
        },
      });
      await mesFetch.promise;
    });

    const lastCandleCall = candleSetData.mock.calls[candleSetData.mock.calls.length - 1]?.[0] as
      | Array<{ time: number; close: number }>
      | undefined;
    expect(lastCandleCall?.map((b) => b.time)).toEqual([1000, 1300]);
    expect(screen.getByText("2 bars")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe(
      "Warning: truncated, provider gap, 2 quarantined",
    );

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

    expect(screen.queryByRole("status")).toBeNull();

    await act(async () => {
      mnqFetch.resolve(makeBarsResponse("CON.F.US.MNQ.H26", [makeBar(2000, 18000)]));
      await mnqFetch.promise;
    });

    expect(screen.getByText("1 bars")).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();

    const mnq15mPane: PaneConfig = { ...mnqPane, timeframe: "15m" };
    rerender(
      <ChartPane
        pane={mnq15mPane}
        contractId="CON.F.US.MNQ.H26"
        activeFeedLive={false}
        wsClient={fakeWs.client}
        coordinator={coordinator}
      />,
    );

    await act(async () => {
      incompleteFetch.resolve({
        ...makeBarsResponse("CON.F.US.MNQ.H26", [makeBar(2000, 18000)]),
        timeframe: "15m",
        quality: {
          is_complete: false,
          truncated: false,
          provider_gap: false,
          quarantined_count: 0,
        },
      });
      await incompleteFetch.promise;
    });

    expect(screen.getByRole("status").textContent).toBe("Warning: incomplete");
  });

  it("clears series, skips retries, and invokes onAuthError on initial fetchBars 401 (P1-R12-001)", async () => {
    /**
     * Contract: When initial fetchBars rejects with ApiError(401), ChartPane immediately clears
     * candleSeries and volumeSeries, sets auth error status without Retry button, invokes
     * onAuthError(401, message), and schedules zero retry timers.
     * Defect caught: Treating 401 on /api/bars as transient error with retry amplification.
     */
    vi.useFakeTimers();
    const initialFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(initialFetch.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const onAuthError = vi.fn();
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
        onAuthError={onAuthError}
      />,
    );

    candleSetData.mockClear();
    volumeSetData.mockClear();

    await act(async () => {
      initialFetch.reject(new ApiError(401, "Session expired"));
      await Promise.resolve();
    });

    expect(candleSetData).toHaveBeenCalledWith([]);
    expect(volumeSetData).toHaveBeenCalledWith([]);
    expect(onAuthError).toHaveBeenCalledWith(401, expect.stringContaining("Session expired"));
    expect(screen.getByText("auth error")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(vi.getTimerCount()).toBe(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(1);
  });

  it("wipes loaded series, drops buffered WS bars, and invokes onAuthError on post-snapshot resync 403 (P1-R12-001, P2-R12-001)", async () => {
    /**
     * Contract: When a post-snapshot resync fetchBars rejects with ApiError(403), ChartPane clears
     * previously loaded series data and quality warnings, discards buffered WS bars without
     * calling series.update, schedules zero retries, and invokes onAuthError(403, message).
     * Defect caught: Post-snapshot resync 401/403 retaining stale bars and scheduling retries.
     */
    vi.useFakeTimers();
    const initialFetch = createDeferred<BarsResponse>();
    const resyncFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(initialFetch.promise).mockReturnValueOnce(resyncFetch.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const onAuthError = vi.fn();
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
        onAuthError={onAuthError}
      />,
    );

    await act(async () => {
      initialFetch.resolve({
        ...makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000)]),
        quality: {
          is_complete: false,
          truncated: true,
          provider_gap: false,
          quarantined_count: 0,
        },
      });
      await initialFetch.promise;
    });
    expect(screen.getByRole("status").textContent).toBe("Warning: truncated");

    act(() => {
      fakeWs.emitSubscribed({
        type: "subscribed",
        contract_id: "CON.F.US.MES.H26",
        timeframe: "5m",
      });
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(2);

    fakeWs.emitBarUpdate({
      type: "bar_update",
      contract_id: "CON.F.US.MES.H26",
      live_mode: false,
      timeframe: "5m",
      is_recovered_historical: false,
      recovery_tag: null,
      bar: makeBar(1060, 5025),
    });

    candleSetData.mockClear();
    volumeSetData.mockClear();

    await act(async () => {
      resyncFetch.reject(new ApiError(403, "Invalid CSRF token"));
      await Promise.resolve();
    });

    expect(candleSetData).toHaveBeenCalledWith([]);
    expect(volumeSetData).toHaveBeenCalledWith([]);
    expect(candleUpdate).not.toHaveBeenCalled();
    expect(volumeUpdate).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("auth error")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(onAuthError).toHaveBeenCalledWith(403, expect.stringContaining("Invalid CSRF token"));
    expect(vi.getTimerCount()).toBe(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
      await Promise.resolve();
    });
    expect(fetchBarsMock).toHaveBeenCalledTimes(2);
  });

  it("registers imperative LayoutSyncController handle and applies crosshair, range, and centerOnTime with error isolation", async () => {
    /**
     * Contract: ChartPane registers a ChartPaneSyncHandle with LayoutSyncController, resolves
     * bar close price (exact or nearest) for setCrosshairPosition, calls clearCrosshairPosition
     * on null, applies setVisibleRange, computes centerOnTime from getVisibleRange or loaded bars,
     * and isolates chart errors without crashing.
     * Defect caught: Missing imperative sync registration, broken price lookup, or uncaught chart exceptions.
     */
    const initialFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(initialFetch.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const syncController = new LayoutSyncController({
      symbol: false,
      interval: false,
      crosshair: true,
      time: true,
      dateRange: true,
    });
    const pane: PaneConfig = {
      pane_id: "pane-1",
      root_symbol: "MES",
      timeframe: "5m",
      live_mode: false,
      playback_mode: "live",
    };

    const { unmount } = render(
      <ChartPane
        pane={pane}
        contractId="CON.F.US.MES.H26"
        activeFeedLive={false}
        wsClient={fakeWs.client}
        coordinator={coordinator}
        syncController={syncController}
      />,
    );

    await act(async () => {
      initialFetch.resolve(
        makeBarsResponse("CON.F.US.MES.H26", [
          makeBar(1000, 5000),
          makeBar(1300, 5012),
          makeBar(1600, 5025),
        ]),
      );
      await initialFetch.promise;
    });

    // Exact bar close match -> setCrosshairPosition(5012, 1300, series)
    syncController.onCrosshairMove("pane-2", 1300 as never);
    expect(setCrosshairPositionMock).toHaveBeenCalledWith(5012, 1300, expect.any(Object));

    // Nearest bar close match (1400 is closer to 1300 than 1600)
    syncController.onCrosshairMove("pane-2", 1400 as never);
    expect(setCrosshairPositionMock).toHaveBeenLastCalledWith(5012, 1400, expect.any(Object));

    // Null clears crosshair
    syncController.onCrosshairMove("pane-2", null);
    expect(clearCrosshairPositionMock).toHaveBeenCalledTimes(1);

    // Date range sync forwards to timeScale().setVisibleRange
    const targetRange = { from: 1000 as never, to: 1600 as never };
    syncController.onVisibleTimeRangeChange("pane-2", targetRange);
    expect(setVisibleRangeMock).toHaveBeenCalledWith(targetRange);

    // Time click sync with null getVisibleRange derives positive halfSpan from loaded bars ((1600-1000)/2 = 300)
    getVisibleRangeMock.mockReturnValueOnce(null);
    syncController.onTimeClick("pane-2", 1300 as never);
    expect(setVisibleRangeMock).toHaveBeenLastCalledWith({ from: 1000, to: 1600 });

    // Time click sync with existing getVisibleRange preserves halfSpan ((2000-1200)/2 = 400)
    getVisibleRangeMock.mockReturnValueOnce({ from: 1200, to: 2000 });
    syncController.onTimeClick("pane-2", 1500 as never);
    expect(setVisibleRangeMock).toHaveBeenLastCalledWith({ from: 1100, to: 1900 });

    // Chart errors are isolated cleanly
    setCrosshairPositionMock.mockImplementationOnce(() => {
      throw new Error("Out of range");
    });
    expect(() => syncController.onCrosshairMove("pane-2", 1300 as never)).not.toThrow();

    setVisibleRangeMock.mockImplementationOnce(() => {
      throw new Error("No data");
    });
    expect(() => syncController.onVisibleTimeRangeChange("pane-2", targetRange)).not.toThrow();

    // Unmount unregisters handle and unsubscribes listeners
    unmount();
    expect(crosshairMoveHandlers).toHaveLength(0);
    expect(clickHandlers).toHaveLength(0);
    expect(visibleRangeHandlers).toHaveLength(0);
    setCrosshairPositionMock.mockClear();
    syncController.onCrosshairMove("pane-2", 1300 as never);
    expect(setCrosshairPositionMock).not.toHaveBeenCalled();
  });

  it("forwards chart crosshair, click, and visible range events to LayoutSyncController", async () => {
    /**
     * Contract: User crosshair move, click, and visible time range changes inside ChartPane
     * invoke syncController.onCrosshairMove, onTimeClick, and onVisibleTimeRangeChange for other panes.
     * Defect caught: ChartPane chart events failing to propagate to peer panes via syncController.
     */
    const initialFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(initialFetch.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const syncController = new LayoutSyncController({
      symbol: false,
      interval: false,
      crosshair: true,
      time: true,
      dateRange: true,
    });
    const peerHandle = {
      paneId: "pane-2",
      applyCrosshairTime: vi.fn(),
      applyVisibleTimeRange: vi.fn(),
      centerOnTime: vi.fn(),
    };
    syncController.registerPane(peerHandle);

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
        syncController={syncController}
      />,
    );

    await act(async () => {
      initialFetch.resolve(makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000)]));
      await initialFetch.promise;
    });

    const emptySeriesData = new Map();
    crosshairMoveHandlers[0]!({ time: 1000, seriesData: emptySeriesData });
    expect(peerHandle.applyCrosshairTime).toHaveBeenCalledWith(1000);

    crosshairMoveHandlers[0]!({ time: undefined, seriesData: emptySeriesData });
    expect(peerHandle.applyCrosshairTime).toHaveBeenLastCalledWith(null);

    clickHandlers[0]!({ time: 1000, seriesData: emptySeriesData });
    expect(peerHandle.centerOnTime).toHaveBeenCalledWith(1000);

    visibleRangeHandlers[0]!({ from: 900, to: 1200 });
    expect(peerHandle.applyVisibleTimeRange).toHaveBeenCalledWith({ from: 900, to: 1200 });
  });

  it("suppresses async next-frame visibleTimeRangeChange echo after applyVisibleTimeRange without blocking subsequent user pan/zoom (P1-001)", async () => {
    /**
     * Contract: When applyVisibleTimeRange invokes setVisibleRange and the chart asynchronously
     * emits subscribeVisibleTimeRangeChange on the next animation frame (e.g. timeframe-adjusted
     * range), ChartPane suppresses the echo so it never bounces back to the source pane, while
     * subsequent user pan/zoom events still propagate.
     * Defect caught: Lightweight Charts v5 async InvalidateMask echo bypassing LayoutSyncController's synchronous guard.
     */
    let nextId = 1;
    const rafCallbacks = new Map<number, FrameRequestCallback>();
    const rafMock = vi.fn((cb: FrameRequestCallback) => {
      const id = nextId++;
      rafCallbacks.set(id, cb);
      return id;
    });
    const cafMock = vi.fn((id: number) => {
      rafCallbacks.delete(id);
    });
    vi.stubGlobal("requestAnimationFrame", rafMock);
    vi.stubGlobal("cancelAnimationFrame", cafMock);

    const initialFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(initialFetch.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const syncController = new LayoutSyncController({
      symbol: false,
      interval: false,
      crosshair: false,
      time: false,
      dateRange: true,
    });
    const sourcePane = {
      paneId: "pane-2",
      applyCrosshairTime: vi.fn(),
      applyVisibleTimeRange: vi.fn(),
      centerOnTime: vi.fn(),
    };
    syncController.registerPane(sourcePane);

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
        syncController={syncController}
      />,
    );

    await act(async () => {
      initialFetch.resolve(
        makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000), makeBar(1600, 5025)]),
      );
      await initialFetch.promise;
    });

    setVisibleRangeMock.mockImplementationOnce((range) => {
      requestAnimationFrame(() => {
        for (const handler of visibleRangeHandlers) {
          handler({ from: range.from - 60, to: range.to + 60 });
        }
      });
    });

    syncController.onVisibleTimeRangeChange("pane-2", {
      from: 1000 as never,
      to: 1600 as never,
    });
    expect(setVisibleRangeMock).toHaveBeenCalledWith({ from: 1000, to: 1600 });
    expect(sourcePane.applyVisibleTimeRange).not.toHaveBeenCalled();

    const frameEntries = Array.from(rafCallbacks.entries());
    rafCallbacks.clear();
    for (const [, cb] of frameEntries) {
      cb(16);
    }

    expect(sourcePane.applyVisibleTimeRange).not.toHaveBeenCalled();
    expect(cafMock).toHaveBeenCalledTimes(1);

    // Subsequent user pan/zoom propagates normally
    visibleRangeHandlers[0]!({ from: 1100, to: 1500 });
    expect(sourcePane.applyVisibleTimeRange).toHaveBeenCalledTimes(1);
    expect(sourcePane.applyVisibleTimeRange).toHaveBeenCalledWith({ from: 1100, to: 1500 });
  });

  it("suppresses async next-frame visibleTimeRangeChange after centerOnTime when both time and dateRange sync are enabled (P1-001)", async () => {
    /**
     * Contract: When both time and dateRange sync are enabled, an async next-frame
     * visibleTimeRangeChange emitted after centerOnTime is suppressed and does not trigger
     * a dateRange broadcast back to sourcePane.
     * Defect caught: centerOnTime triggering an async dateRange feedback broadcast after activeTimeSource clears.
     */
    let nextId = 1;
    const rafCallbacks = new Map<number, FrameRequestCallback>();
    vi.stubGlobal(
      "requestAnimationFrame",
      vi.fn((cb: FrameRequestCallback) => {
        const id = nextId++;
        rafCallbacks.set(id, cb);
        return id;
      }),
    );
    vi.stubGlobal(
      "cancelAnimationFrame",
      vi.fn((id: number) => {
        rafCallbacks.delete(id);
      }),
    );

    const initialFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(initialFetch.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const syncController = new LayoutSyncController({
      symbol: false,
      interval: false,
      crosshair: false,
      time: true,
      dateRange: true,
    });
    const sourcePane = {
      paneId: "pane-2",
      applyCrosshairTime: vi.fn(),
      applyVisibleTimeRange: vi.fn(),
      centerOnTime: vi.fn(),
    };
    syncController.registerPane(sourcePane);

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
        syncController={syncController}
      />,
    );

    await act(async () => {
      initialFetch.resolve(
        makeBarsResponse("CON.F.US.MES.H26", [
          makeBar(1000, 5000),
          makeBar(1300, 5012),
          makeBar(1600, 5025),
        ]),
      );
      await initialFetch.promise;
    });

    setVisibleRangeMock.mockImplementationOnce((range) => {
      requestAnimationFrame(() => {
        for (const handler of visibleRangeHandlers) {
          handler(range);
        }
      });
    });

    syncController.onTimeClick("pane-2", 1300 as never);
    expect(setVisibleRangeMock).toHaveBeenCalledWith({ from: 1000, to: 1600 });

    const frameEntries = Array.from(rafCallbacks.entries());
    rafCallbacks.clear();
    for (const [, cb] of frameEntries) {
      cb(16);
    }

    expect(sourcePane.applyVisibleTimeRange).not.toHaveBeenCalled();

    // Subsequent user pan/zoom propagates normally
    visibleRangeHandlers[0]!({ from: 1050, to: 1550 });
    expect(sourcePane.applyVisibleTimeRange).toHaveBeenCalledTimes(1);
    expect(sourcePane.applyVisibleTimeRange).toHaveBeenCalledWith({ from: 1050, to: 1550 });
  });

  it("resets echo suppression on the next animation frame when setVisibleRange is a no-op and cancels pending rAF on unmount (P1-001)", async () => {
    /**
     * Contract: When setVisibleRange fires no visibleTimeRangeChange callback (e.g. identical
     * range no-op), the scheduled animation frame resets suppression so subsequent user pan/zoom
     * propagates, and unmounting cancels any pending suppression rAF.
     * Defect caught: Stuck suppressRangeEchoRef swallowing future user pan/zoom after a no-op range update.
     */
    let nextId = 1;
    const rafCallbacks = new Map<number, FrameRequestCallback>();
    const cafMock = vi.fn((id: number) => {
      rafCallbacks.delete(id);
    });
    vi.stubGlobal(
      "requestAnimationFrame",
      vi.fn((cb: FrameRequestCallback) => {
        const id = nextId++;
        rafCallbacks.set(id, cb);
        return id;
      }),
    );
    vi.stubGlobal("cancelAnimationFrame", cafMock);

    const initialFetch = createDeferred<BarsResponse>();
    fetchBarsMock.mockReturnValueOnce(initialFetch.promise);

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const syncController = new LayoutSyncController({
      symbol: false,
      interval: false,
      crosshair: false,
      time: false,
      dateRange: true,
    });
    const sourcePane = {
      paneId: "pane-2",
      applyCrosshairTime: vi.fn(),
      applyVisibleTimeRange: vi.fn(),
      centerOnTime: vi.fn(),
    };
    syncController.registerPane(sourcePane);

    const pane: PaneConfig = {
      pane_id: "pane-1",
      root_symbol: "MES",
      timeframe: "5m",
      live_mode: false,
      playback_mode: "live",
    };

    const { unmount } = render(
      <ChartPane
        pane={pane}
        contractId="CON.F.US.MES.H26"
        activeFeedLive={false}
        wsClient={fakeWs.client}
        coordinator={coordinator}
        syncController={syncController}
      />,
    );

    await act(async () => {
      initialFetch.resolve(
        makeBarsResponse("CON.F.US.MES.H26", [makeBar(1000, 5000), makeBar(1600, 5025)]),
      );
      await initialFetch.promise;
    });

    // No-op setVisibleRange: no visibleRangeHandlers callback fired
    syncController.onVisibleTimeRangeChange("pane-2", {
      from: 1000 as never,
      to: 1600 as never,
    });
    expect(setVisibleRangeMock).toHaveBeenCalledTimes(1);
    expect(rafCallbacks.size).toBe(1);

    // Flush the next animation frame -> clears suppression
    const frameEntries = Array.from(rafCallbacks.entries());
    rafCallbacks.clear();
    for (const [, cb] of frameEntries) {
      cb(16);
    }
    expect(rafCallbacks.size).toBe(0);

    // Subsequent user pan/zoom now propagates
    visibleRangeHandlers[0]!({ from: 1200, to: 1500 });
    expect(sourcePane.applyVisibleTimeRange).toHaveBeenCalledTimes(1);
    expect(sourcePane.applyVisibleTimeRange).toHaveBeenCalledWith({ from: 1200, to: 1500 });

    // Pending rAF from another no-op update is cancelled cleanly on unmount
    syncController.onVisibleTimeRangeChange("pane-2", {
      from: 1000 as never,
      to: 1600 as never,
    });
    expect(rafCallbacks.size).toBe(1);
    unmount();
    expect(cafMock).toHaveBeenCalledTimes(1);
    expect(rafCallbacks.size).toBe(0);
  });

  it("hydrates drawings, attaches/detaches primitive, places drawings with Magnet & distinct ray, drags handles, and suppresses Time sync mid-draw (Phase 4 Part 2)", async () => {
    /**
     * Contract: ChartPane hydrates drawings on target ready and resyncNonce, attaches/detaches
     * DrawingsOverlayPrimitive, supports pointer placement (hline, trendline, ray distinct anchors,
     * Magnet OHLC snap), disables/restores handleScroll/handleScale on handle drag, persists domain
     * (time, price) updates on pointerup, and suppresses syncController.onTimeClick while drawing.
     * Defect caught: Chart scroll hijacking handle drags, raw pixels persisted, or Time sync jumping panes mid-draw.
     */
    const bars = [
      { time: 1700000000, open: 5000, high: 5012, low: 4996, close: 5005, volume: 25, is_closed: true },
      { time: 1700000300, open: 5005, high: 5020, low: 5002, close: 5015, volume: 30, is_closed: true },
    ];
    fetchBarsMock.mockResolvedValue(makeBarsResponse("CON.F.US.MES.H26", bars));
    postDrawingCommandMock.mockImplementation(async (symbol, cmd) => {
      const anchors =
        cmd.action === "create"
          ? cmd.drawing.coordinates.anchors
          : cmd.action === "update" && cmd.drawing.coordinates !== undefined
            ? cmd.drawing.coordinates.anchors
            : [];
      return {
        symbol,
        contract_id: "CON.F.US.MES.H26",
        pane_id: "pane-1",
        instrument: {
          root_symbol: "MES",
          name: "MES",
          exchange: "CME",
          tick_size: 0.25,
          tick_value: 1.25,
          currency: "USD",
        },
        scope_revision: cmd.expected_revision + 1,
        can_undo: true,
        can_redo: false,
        deduplicated: false,
        affected_ids: ["draw-created"],
        drawings:
          cmd.action === "create" || cmd.action === "update"
            ? [
                {
                  id: "draw-created",
                  revision: cmd.expected_revision + 1,
                  type: "ray",
                  symbol: "MES",
                  contract_id: "CON.F.US.MES.H26",
                  pane_id: "pane-1",
                  created_by: "user",
                  source_bar_refs: [],
                  coordinates: { anchors },
                  style: {},
                  metadata: {},
                  created_at_utc: 1700000000,
                  updated_at_utc: 1700000000,
                },
              ]
            : [],
      };
    });

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const syncController = new LayoutSyncController({
      symbol: false,
      interval: false,
      crosshair: false,
      time: true,
      dateRange: false,
    });
    const otherPane = {
      paneId: "pane-2",
      applyCrosshairTime: vi.fn(),
      applyVisibleTimeRange: vi.fn(),
      centerOnTime: vi.fn(),
    };
    syncController.registerPane(otherPane);

    const pane: PaneConfig = {
      pane_id: "pane-1",
      root_symbol: "MES",
      timeframe: "5m",
      live_mode: false,
      playback_mode: "live",
    };

    const { unmount } = render(
      <ChartPane
        pane={pane}
        contractId="CON.F.US.MES.H26"
        activeFeedLive={false}
        wsClient={fakeWs.client}
        coordinator={coordinator}
        syncController={syncController}
      />,
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(attachPrimitiveMock).toHaveBeenCalledTimes(1);
    expect(fetchDrawingsMock).toHaveBeenCalledWith(
      "MES",
      { contract_id: "CON.F.US.MES.H26", pane_id: "pane-1" },
      expect.any(AbortSignal),
    );

    // Resync via subscribed ACK triggers drawings re-hydration
    act(() => {
      fakeWs.emitSubscribed({
        type: "subscribed",
        contract_id: "CON.F.US.MES.H26",
        timeframe: "5m",
      });
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(fetchDrawingsMock).toHaveBeenCalledTimes(2);

    const canvas = document.querySelector(".chart-pane__canvas") as HTMLElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 600,
      bottom: 400,
      width: 600,
      height: 400,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    // Place a Ray with two clicks at identical point -> distinct end.time enforced + Time sync suppressed
    useDrawingStore.getState().setActiveTool("ray");
    useDrawingStore.getState().setMagnetEnabled(true);
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 160, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerUp(canvas, { clientX: 100, clientY: 160, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 160, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerUp(canvas, { clientX: 100, clientY: 160, button: 0, pointerType: "mouse", pointerId: 1 });
      clickHandlers[0]?.({ time: 1700000000 });
      await Promise.resolve();
    });

    // Suppressed Time sync while placing drawing
    expect(otherPane.centerOnTime).not.toHaveBeenCalled();
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(1);
    const rayCreateCmd = postDrawingCommandMock.mock.calls[0]![1];
    expect(rayCreateCmd.action).toBe("create");
    if (rayCreateCmd.action === "create") {
      // Magnet snapped y=160 (price=5010) to nearest OHLC high=5012 on bar 1700000000, and ray end.time !== start.time
      expect(rayCreateCmd.drawing.type).toBe("ray");
      expect(rayCreateCmd.drawing.coordinates.anchors[0]).toEqual({
        role: "start",
        time: 1700000000,
        price: 5012,
      });
      expect(rayCreateCmd.drawing.coordinates.anchors[1]!.time).toBe(1700000300);
    }

    // Drag start anchor handle of the selected ray -> disables handleScroll/handleScale then restores on pointerup
    useDrawingStore.getState().setMagnetEnabled(false);
    const scopeKey = buildDrawingScopeKey("MES", "CON.F.US.MES.H26", "pane-1");
    expect(useDrawingStore.getState().selectedByScope[scopeKey]).toBe("draw-created");

    applyOptionsMock.mockClear();
    // Start handle is at bar 0 (x=100) and price 5012 (y=(5050-5012)*4 = 152)
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 152, button: 0, pointerType: "mouse", pointerId: 2 });
      fireEvent.pointerMove(canvas, { clientX: 130, clientY: 180, button: 0, pointerType: "mouse", pointerId: 2 });
      fireEvent.pointerUp(canvas, { clientX: 130, clientY: 180, button: 0, pointerType: "mouse", pointerId: 2 });
      await Promise.resolve();
    });

    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: false, handleScale: false });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: true, handleScale: true });
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(2);
    const updateCmd = postDrawingCommandMock.mock.calls[1]![1];
    expect(updateCmd.action).toBe("update");
    if (updateCmd.action === "update") {
      expect(updateCmd.drawing.coordinates?.anchors[0]?.time).toBe(1700000300);
      expect(updateCmd.drawing.coordinates?.anchors[0]?.price).toBe(5005);
    }

    // pointercancel mid-drag restores handleScroll/handleScale without persisting update
    applyOptionsMock.mockClear();
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 130, clientY: 180, button: 0, pointerType: "mouse", pointerId: 3 });
      fireEvent.pointerMove(canvas, { clientX: 140, clientY: 190, button: 0, pointerType: "mouse", pointerId: 3 });
      fireEvent.pointerCancel(canvas, { pointerId: 3 });
      await Promise.resolve();
    });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: false, handleScale: false });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: true, handleScale: true });
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(2);

    unmount();
    expect(detachPrimitiveMock).toHaveBeenCalledTimes(1);
  });

  it("persists explicit metadata.direction for auto FVG, places hline/order_block/long_position, discards stale draft/drag across tool & symbol switches, restores empty-pane Time sync, and resyncs 3m panes (Phase 4 Part 2)", async () => {
    /**
     * Contract:
     * 1. Placing fvg_box under "auto" preset persists metadata.direction ("bullish" when end >= start, "bearish" when end < start).
     * 2. hline, order_block, and long_position placement persist exact domain coordinates and directional >=1*tickSize clamping.
     * 3. Switching tools mid-draft (trendline -> fvg_box) clears Anchor 1 so the second tool starts fresh.
     * 4. Switching pane symbol/contract mid-draft/mid-drag (MES -> MYM) aborts stale draft/drag and restores handleScroll/handleScale.
     * 5. Empty-pane click in cursor mode triggers syncController.onTimeClick even after drag or pointercancel.
     * 6. Non-live-WS 3m pane re-fetches drawings on wsClient.onResyncRequired.
     * Defect caught: P1-001, P1-002, P1-003, P2-001, P2-002 regressions.
     */
    const bars = [
      { time: 1700000000, open: 5000, high: 5012, low: 4996, close: 5005, volume: 25, is_closed: true },
      { time: 1700000300, open: 5005, high: 5020, low: 5002, close: 5015, volume: 30, is_closed: true },
    ];
    fetchBarsMock.mockResolvedValue(makeBarsResponse("CON.F.US.MES.H26", bars));
    postDrawingCommandMock.mockImplementation(async (symbol, cmd) => ({
      symbol,
      contract_id: cmd.contract_id ?? null,
      pane_id: cmd.pane_id ?? null,
      instrument: {
        root_symbol: symbol,
        name: symbol,
        exchange: "CME",
        tick_size: symbol === "MYM" ? 1 : 0.25,
        tick_value: 1.25,
        currency: "USD",
      },
      scope_revision: cmd.expected_revision + 1,
      can_undo: true,
      can_redo: false,
      deduplicated: false,
      affected_ids: ["draw-1"],
      drawings:
        cmd.action === "create"
          ? [
              {
                id: "draw-1",
                revision: cmd.expected_revision + 1,
                type: cmd.drawing.type,
                symbol,
                contract_id: cmd.contract_id ?? null,
                pane_id: cmd.pane_id ?? null,
                created_by: "user",
                source_bar_refs: [],
                coordinates: cmd.drawing.coordinates,
                style: {},
                metadata: cmd.drawing.metadata ?? {},
                created_at_utc: 1700000000,
                updated_at_utc: 1700000000,
              },
            ]
          : [],
    }));

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const syncController = new LayoutSyncController({
      symbol: false,
      interval: false,
      crosshair: false,
      time: true,
      dateRange: false,
    });
    const siblingPane = {
      paneId: "pane-2",
      applyCrosshairTime: vi.fn(),
      applyVisibleTimeRange: vi.fn(),
      centerOnTime: vi.fn(),
    };
    syncController.registerPane(siblingPane);

    const mesPane: PaneConfig = {
      pane_id: "pane-1",
      root_symbol: "MES",
      timeframe: "3m",
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
        syncController={syncController}
      />,
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    // 3m non-live-WS pane still re-fetches drawings on wsClient.onResyncRequired (P2-001)
    expect(fetchDrawingsMock).toHaveBeenCalledTimes(1);
    act(() => {
      fakeWs.emitResyncRequired({
        type: "resync_required",
        reason: "slow_consumer_disconnect",
        contract_id: "CON.F.US.MES.H26",
      });
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(fetchDrawingsMock).toHaveBeenCalledTimes(2);

    const canvas = document.querySelector(".chart-pane__canvas") as HTMLElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 600,
      bottom: 400,
      width: 600,
      height: 400,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    // 1. Place bullish auto-FVG (y=200 -> 5000, y=160 -> 5010)
    useDrawingStore.getState().setFvgPreset("auto");
    useDrawingStore.getState().setActiveTool("fvg_box");
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 200, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerMove(canvas, { clientX: 130, clientY: 160, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerUp(canvas, { clientX: 130, clientY: 160, button: 0, pointerType: "mouse", pointerId: 1 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).toHaveBeenLastCalledWith(
      "MES",
      expect.objectContaining({
        action: "create",
        drawing: expect.objectContaining({
          type: "fvg_box",
          metadata: { direction: "bullish" },
        }),
      }),
      expect.any(AbortSignal),
    );

    // 2. Place bearish auto-FVG (y=160 -> 5010, y=200 -> 5000)
    useDrawingStore.getState().setActiveTool("fvg_box");
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 160, button: 0, pointerType: "mouse", pointerId: 2 });
      fireEvent.pointerMove(canvas, { clientX: 130, clientY: 200, button: 0, pointerType: "mouse", pointerId: 2 });
      fireEvent.pointerUp(canvas, { clientX: 130, clientY: 200, button: 0, pointerType: "mouse", pointerId: 2 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).toHaveBeenLastCalledWith(
      "MES",
      expect.objectContaining({
        action: "create",
        drawing: expect.objectContaining({
          type: "fvg_box",
          metadata: { direction: "bearish" },
        }),
      }),
      expect.any(AbortSignal),
    );

    // 3. Place hline, order_block, and long_position
    useDrawingStore.getState().setActiveTool("hline");
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 200, button: 0, pointerType: "mouse", pointerId: 3 });
      fireEvent.pointerUp(canvas, { clientX: 100, clientY: 200, button: 0, pointerType: "mouse", pointerId: 3 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).toHaveBeenLastCalledWith(
      "MES",
      expect.objectContaining({
        action: "create",
        drawing: expect.objectContaining({
          type: "hline",
          coordinates: { anchors: [{ role: "level", time: 1700000000, price: 5000 }] },
        }),
      }),
      expect.any(AbortSignal),
    );

    useDrawingStore.getState().setActiveTool("order_block");
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 200, button: 0, pointerType: "mouse", pointerId: 4 });
      fireEvent.pointerMove(canvas, { clientX: 130, clientY: 180, button: 0, pointerType: "mouse", pointerId: 4 });
      fireEvent.pointerUp(canvas, { clientX: 130, clientY: 180, button: 0, pointerType: "mouse", pointerId: 4 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).toHaveBeenLastCalledWith(
      "MES",
      expect.objectContaining({
        action: "create",
        drawing: expect.objectContaining({
          type: "order_block",
          metadata: { extend_right: true, label: "OB" },
        }),
      }),
      expect.any(AbortSignal),
    );

    useDrawingStore.getState().setActiveTool("long_position");
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 200, button: 0, pointerType: "mouse", pointerId: 5 });
      fireEvent.pointerUp(canvas, { clientX: 100, clientY: 200, button: 0, pointerType: "mouse", pointerId: 5 });
      await Promise.resolve();
    });
    const lastPosCmd = postDrawingCommandMock.mock.calls[postDrawingCommandMock.mock.calls.length - 1]![1];
    expect(lastPosCmd.action).toBe("create");
    if (lastPosCmd.action === "create") {
      expect(lastPosCmd.drawing.type).toBe("long_position");
      const entry = lastPosCmd.drawing.coordinates.anchors.find((a) => a.role === "entry")!;
      const target = lastPosCmd.drawing.coordinates.anchors.find((a) => a.role === "target")!;
      const stop = lastPosCmd.drawing.coordinates.anchors.find((a) => a.role === "stop")!;
      expect(target.price).toBeGreaterThanOrEqual(entry.price + 0.25);
      expect(stop.price).toBeLessThanOrEqual(entry.price - 0.25);
    }

    // 4. Empty-pane click after placement/pointercancel still fires syncController.onTimeClick (P1-003)
    await act(async () => {
      fireEvent.pointerCancel(canvas, { pointerId: 5 });
      fireEvent.pointerDown(canvas, { clientX: 450, clientY: 50, button: 0, pointerType: "mouse", pointerId: 6 });
      fireEvent.pointerUp(canvas, { clientX: 450, clientY: 50, button: 0, pointerType: "mouse", pointerId: 6 });
      clickHandlers[0]?.({ time: 1700000300 });
      await Promise.resolve();
    });
    expect(siblingPane.centerOnTime).toHaveBeenCalledWith(1700000300);

    // 5. Tool-to-tool switch mid-draft discards stale Anchor 1 (P1-001)
    const callsBeforeSwitch = postDrawingCommandMock.mock.calls.length;
    await act(async () => {
      useDrawingStore.getState().setActiveTool("trendline");
      await Promise.resolve();
    });
    act(() => {
      // Click 1 of trendline at x=100, y=220 (price=4995)
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 220, button: 0, pointerType: "mouse", pointerId: 7 });
      fireEvent.pointerUp(canvas, { clientX: 100, clientY: 220, button: 0, pointerType: "mouse", pointerId: 7 });
    });
    await act(async () => {
      useDrawingStore.getState().setActiveTool("fvg_box");
      await Promise.resolve();
    });
    await act(async () => {
      // Click 1 & 2 of fvg_box at (130, 180 -> 5005) and (160, 140 -> 5015)
      fireEvent.pointerDown(canvas, { clientX: 130, clientY: 180, button: 0, pointerType: "mouse", pointerId: 8 });
      fireEvent.pointerUp(canvas, { clientX: 130, clientY: 180, button: 0, pointerType: "mouse", pointerId: 8 });
      fireEvent.pointerDown(canvas, { clientX: 160, clientY: 140, button: 0, pointerType: "mouse", pointerId: 8 });
      fireEvent.pointerUp(canvas, { clientX: 160, clientY: 140, button: 0, pointerType: "mouse", pointerId: 8 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(callsBeforeSwitch + 1);
    const switchedCmd = postDrawingCommandMock.mock.calls[postDrawingCommandMock.mock.calls.length - 1]![1];
    if (switchedCmd.action === "create") {
      expect(switchedCmd.drawing.type).toBe("fvg_box");
      expect(switchedCmd.drawing.coordinates.anchors[0]!.price).toBe(5005);
      expect(switchedCmd.drawing.coordinates.anchors[1]!.price).toBe(5015);
    }

    // 6. Symbol/contract switch mid-drag aborts drag & restores handleScroll/handleScale without leaking update (P2-002)
    const callsBeforeSymbolSwitch = postDrawingCommandMock.mock.calls.length;
    applyOptionsMock.mockClear();
    // Start dragging the newly selected fvg-box anchor at (130, 180)
    act(() => {
      fireEvent.pointerDown(canvas, { clientX: 130, clientY: 180, button: 0, pointerType: "mouse", pointerId: 9 });
      fireEvent.pointerMove(canvas, { clientX: 160, clientY: 160, button: 0, pointerType: "mouse", pointerId: 9 });
    });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: false, handleScale: false });

    const mymPane: PaneConfig = { ...mesPane, root_symbol: "MYM" };
    await act(async () => {
      rerender(
        <ChartPane
          pane={mymPane}
          contractId="CON.F.US.MYM.H26"
          activeFeedLive={false}
          wsClient={fakeWs.client}
          coordinator={coordinator}
          syncController={syncController}
        />,
      );
      await Promise.resolve();
    });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: true, handleScale: true });
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 160, button: 0, pointerType: "mouse", pointerId: 9 });
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(callsBeforeSymbolSwitch);
  });

  it("supports whole-body drag, zoom re-projection, deadzone/jitter guard, mid-drag Escape/Delete cancellation, and axis scale isolation (Phase 4 Part 2)", async () => {
    /**
     * Contract:
     * 1. Whole-body drag translation shifts all anchors by domain (deltaTime, deltaPrice).
     * 2. Zooming updates projected coordinates from domain (time, price) via updateAllViews().
     * 3. 0-2px jitter on body click and 6px off-center anchor click preserve canRedo=true without mutating.
     * 4. Mid-drag Escape deselection or Delete removal cancels drag, restores handleScroll/handleScale, and blocks updateDrawing on pointerUp.
     * 5. Clicks/drags on Right Price Scale (px >= 530) and Bottom Time Scale (py >= 372) are completely ignored.
     * Defect caught: P1-001, P1-002, P1-003 regressions.
     */
    const bars = [
      { time: 1700000000, open: 5000, high: 5012, low: 4996, close: 5005, volume: 25, is_closed: true },
      { time: 1700000300, open: 5005, high: 5020, low: 5002, close: 5015, volume: 30, is_closed: true },
    ];
    const trendlineDrawing = {
      id: "tl-body",
      revision: 1,
      type: "trendline" as const,
      symbol: "MES" as const,
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      created_by: "user" as const,
      source_bar_refs: [],
      coordinates: {
        anchors: [
          { role: "start", time: 1700000000, price: 5000 },
          { role: "end", time: 1700000300, price: 5000 },
        ],
      },
      style: {},
      metadata: {},
      created_at_utc: 1700000000,
      updated_at_utc: 1700000000,
    };
    const hlineDrawing = {
      id: "hl-axis",
      revision: 1,
      type: "hline" as const,
      symbol: "MES" as const,
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      created_by: "user" as const,
      source_bar_refs: [],
      coordinates: { anchors: [{ role: "level", time: 1700000000, price: 4980 }] },
      style: {},
      metadata: {},
      created_at_utc: 1700000000,
      updated_at_utc: 1700000000,
    };

    fetchBarsMock.mockResolvedValue(makeBarsResponse("CON.F.US.MES.H26", bars));
    fetchDrawingsMock.mockResolvedValue({
      symbol: "MES",
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
      scope_revision: 3,
      can_undo: true,
      can_redo: true,
      drawings: [trendlineDrawing, hlineDrawing],
    });
    postDrawingCommandMock.mockImplementation(async (symbol, cmd) => ({
      symbol,
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
      scope_revision: cmd.expected_revision + 1,
      can_undo: true,
      can_redo: false,
      deduplicated: false,
      affected_ids: ["tl-body"],
      drawings: [
        cmd.action === "update" && cmd.drawing.coordinates
          ? { ...trendlineDrawing, coordinates: cmd.drawing.coordinates }
          : trendlineDrawing,
        hlineDrawing,
      ],
    }));

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

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const canvas = document.querySelector(".chart-pane__canvas") as HTMLElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 600,
      bottom: 400,
      width: 600,
      height: 400,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    const scope = { symbol: "MES" as const, contractId: "CON.F.US.MES.H26", paneId: "pane-1" };
    const scopeKey = buildDrawingScopeKey("MES", "CON.F.US.MES.H26", "pane-1");

    // 1. 0-2px jitter on body click (midpoint x=115, y=200) & 6px off-center handle click (x=106, y=200) keep canRedo=true and zero mutations
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 115, clientY: 200, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerMove(canvas, { clientX: 117, clientY: 200, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerUp(canvas, { clientX: 117, clientY: 200, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerDown(canvas, { clientX: 106, clientY: 200, button: 0, pointerType: "mouse", pointerId: 2 });
      fireEvent.pointerMove(canvas, { clientX: 107, clientY: 200, button: 0, pointerType: "mouse", pointerId: 2 });
      fireEvent.pointerUp(canvas, { clientX: 107, clientY: 200, button: 0, pointerType: "mouse", pointerId: 2 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).not.toHaveBeenCalled();
    expect(useDrawingStore.getState().scopes[scopeKey]?.canRedo).toBe(true);

    // 2. Whole-body drag translation (hit midpoint x=115, y=200 -> drag +30px right, -20px up => +300s, +5 price)
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 115, clientY: 200, button: 0, pointerType: "mouse", pointerId: 3 });
      fireEvent.pointerMove(canvas, { clientX: 145, clientY: 180, button: 0, pointerType: "mouse", pointerId: 3 });
      fireEvent.pointerUp(canvas, { clientX: 145, clientY: 180, button: 0, pointerType: "mouse", pointerId: 3 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(1);
    const bodyUpdateCmd = postDrawingCommandMock.mock.calls[0]![1];
    expect(bodyUpdateCmd.action).toBe("update");
    if (bodyUpdateCmd.action === "update") {
      expect(bodyUpdateCmd.drawing.coordinates?.anchors).toEqual([
        { role: "start", time: 1700000300, price: 5005 },
        { role: "end", time: 1700000600, price: 5005 },
      ]);
    }

    // 3. Zoom coordinate scale re-projection from domain (time, price)
    const attachedPrimitive = attachPrimitiveMock.mock.calls[0]![0] as {
      updateAllViews: () => void;
      getRenderSnapshot: () => { projectedDrawings: Array<{ drawing: { id: string }; anchors: Array<{ x: number; y: number }> }> };
    };
    const beforeZoom = attachedPrimitive
      .getRenderSnapshot()
      .projectedDrawings.find((d) => d.drawing.id === "hl-axis")!.anchors[0]!;
    expect(beforeZoom.y).toBe(280);

    priceToCoordinateMock.mockImplementation((price) => (5050 - price) * 8);
    attachedPrimitive.updateAllViews();
    const afterZoom = attachedPrimitive
      .getRenderSnapshot()
      .projectedDrawings.find((d) => d.drawing.id === "hl-axis")!.anchors[0]!;
    expect(afterZoom.y).toBe(560);
    priceToCoordinateMock.mockImplementation((price) => (5050 - price) * 4);
    attachedPrimitive.updateAllViews();

    // 4. Mid-drag Escape deselection & Delete removal cancel drag synchronously and block updateDrawing
    applyOptionsMock.mockClear();
    // Start dragging hline at (200, 280) -> moves > 5px
    act(() => {
      fireEvent.pointerDown(canvas, { clientX: 200, clientY: 280, button: 0, pointerType: "mouse", pointerId: 4 });
      fireEvent.pointerMove(canvas, { clientX: 200, clientY: 240, button: 0, pointerType: "mouse", pointerId: 4 });
    });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: false, handleScale: false });
    // Deselect (Escape) mid-drag -> synchronously restores handleScroll/handleScale and reverts
    act(() => {
      useDrawingStore.getState().setSelectedDrawing(scope, null);
    });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: true, handleScale: true });
    await act(async () => {
      fireEvent.pointerUp(canvas, { clientX: 200, clientY: 240, button: 0, pointerType: "mouse", pointerId: 4 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(1);

    // Now test deletion mid-drag via WS drawings_update
    applyOptionsMock.mockClear();
    act(() => {
      fireEvent.pointerDown(canvas, { clientX: 200, clientY: 280, button: 0, pointerType: "mouse", pointerId: 5 });
      fireEvent.pointerMove(canvas, { clientX: 200, clientY: 240, button: 0, pointerType: "mouse", pointerId: 5 });
      useDrawingStore.getState().applyWsDrawingsUpdate({
        type: "drawings_update",
        symbol: "MES",
        contract_id: "CON.F.US.MES.H26",
        pane_id: "pane-1",
        scope_revision: 10,
        command_type: "delete",
        can_undo: true,
        can_redo: false,
        drawings: [trendlineDrawing],
      });
    });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: true, handleScale: true });
    await act(async () => {
      fireEvent.pointerUp(canvas, { clientX: 200, clientY: 240, button: 0, pointerType: "mouse", pointerId: 5 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(1);

    // 5. Right Price Scale (px=565 >= 530) & Bottom Time Scale (py=385 >= 372) isolation in hline & cursor modes (P1-003)
    applyOptionsMock.mockClear();
    useDrawingStore.getState().setActiveTool("hline");
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 565, clientY: 280, button: 0, pointerType: "mouse", pointerId: 6 });
      fireEvent.pointerUp(canvas, { clientX: 565, clientY: 280, button: 0, pointerType: "mouse", pointerId: 6 });
      fireEvent.pointerDown(canvas, { clientX: 200, clientY: 385, button: 0, pointerType: "mouse", pointerId: 7 });
      fireEvent.pointerUp(canvas, { clientX: 200, clientY: 385, button: 0, pointerType: "mouse", pointerId: 7 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(1);

    // Cursor mode click on Right Price Scale at exact hline Y=280 must NOT hit/drag hline or disable handleScale
    useDrawingStore.getState().setActiveTool("cursor");
    useDrawingStore.getState().applyWsDrawingsUpdate({
      type: "drawings_update",
      symbol: "MES",
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      scope_revision: 11,
      command_type: "create",
      can_undo: true,
      can_redo: false,
      drawings: [hlineDrawing],
    });
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 565, clientY: 280, button: 0, pointerType: "mouse", pointerId: 8 });
      fireEvent.pointerMove(canvas, { clientX: 565, clientY: 140, button: 0, pointerType: "mouse", pointerId: 8 });
      fireEvent.pointerUp(canvas, { clientX: 565, clientY: 140, button: 0, pointerType: "mouse", pointerId: 8 });
      await Promise.resolve();
    });
    expect(applyOptionsMock).not.toHaveBeenCalledWith({ handleScroll: false, handleScale: false });
    expect(useDrawingStore.getState().selectedByScope[scopeKey]).toBeNull();
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(1);
  });

  it("cancels active drag synchronously and blocks updateDrawing when WS drawings_update bumps the dragged drawing revision mid-drag (P1-C01, P2-001)", async () => {
    /**
     * Contract: When a concurrent WS drawings_update edits another anchor on the same drawing
     * (bumping revision 1 -> 2, e.g. end.price 5010 -> 5020) while an anchor drag is in progress
     * (>= 5px before pointerUp), ChartPane immediately restores handleScroll/handleScale, reverts
     * the drag preview, sends zero updateDrawing calls on pointerUp, and preserves end.price === 5020.
     * Defect caught: Stale drag commit clobbering concurrent agent/tab edit to another anchor.
     */
    const bars = [
      { time: 1700000000, open: 5000, high: 5012, low: 4996, close: 5005, volume: 25, is_closed: true },
      { time: 1700000300, open: 5005, high: 5020, low: 5002, close: 5015, volume: 30, is_closed: true },
    ];
    const initialTrendline = {
      id: "tl-concurrent",
      revision: 1,
      type: "trendline" as const,
      symbol: "MES" as const,
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      created_by: "user" as const,
      source_bar_refs: [],
      coordinates: {
        anchors: [
          { role: "start", time: 1700000000, price: 5000 },
          { role: "end", time: 1700000300, price: 5010 },
        ],
      },
      style: {},
      metadata: {},
      created_at_utc: 1700000000,
      updated_at_utc: 1700000000,
    };
    const wsUpdatedTrendline = {
      ...initialTrendline,
      revision: 2,
      coordinates: {
        anchors: [
          { role: "start", time: 1700000000, price: 5000 },
          { role: "end", time: 1700000300, price: 5020 },
        ],
      },
    };

    fetchBarsMock.mockResolvedValue(makeBarsResponse("CON.F.US.MES.H26", bars));
    fetchDrawingsMock.mockResolvedValue({
      symbol: "MES",
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
      scope_revision: 1,
      can_undo: false,
      can_redo: false,
      drawings: [initialTrendline],
    });

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

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const canvas = document.querySelector(".chart-pane__canvas") as HTMLElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 600,
      bottom: 400,
      width: 600,
      height: 400,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    applyOptionsMock.mockClear();
    // Drag start handle at (100, 200) -> move >= 5px to (100, 180)
    act(() => {
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 200, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerMove(canvas, { clientX: 100, clientY: 180, button: 0, pointerType: "mouse", pointerId: 1 });
    });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: false, handleScale: false });

    // Concurrent WS update bumps revision 1 -> 2 and moves end.price to 5020
    act(() => {
      useDrawingStore.getState().applyWsDrawingsUpdate({
        type: "drawings_update",
        symbol: "MES",
        contract_id: "CON.F.US.MES.H26",
        pane_id: "pane-1",
        scope_revision: 2,
        command_type: "update",
        can_undo: true,
        can_redo: false,
        drawings: [wsUpdatedTrendline],
      });
    });

    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: true, handleScale: true });

    await act(async () => {
      fireEvent.pointerUp(canvas, { clientX: 100, clientY: 180, button: 0, pointerType: "mouse", pointerId: 1 });
      await Promise.resolve();
    });

    expect(postDrawingCommandMock).not.toHaveBeenCalled();
    const scopeKey = buildDrawingScopeKey("MES", "CON.F.US.MES.H26", "pane-1");
    const saved = useDrawingStore.getState().scopes[scopeKey]?.drawings[0];
    expect(saved?.coordinates.anchors.find((a) => a.role === "end")?.price).toBe(5020);
  });

  it("keeps open 2-point draft active across axis clicks/drags and omits extend_right on fvg_box drafts when obExtendRight is true (P1-001, P1-002)", async () => {
    /**
     * Contract:
     * 1. After click #1 opens a 2-point fvg_box draft inside the plot, clicking/dragging on the
     *    Right Price Scale or Bottom Time Scale must NOT finalize the draft onto a clamped edge;
     *    the draft remains open and only finalizes when click #2 lands inside the plot.
     * 2. With obExtendRight=true, the fvg_box draft and persisted payload omit metadata.extend_right.
     * Defect caught: Axis pointerup prematurely finalizing open 2-point drafts or obExtendRight leaking onto fvg_box.
     */
    const bars = [
      { time: 1700000000, open: 5000, high: 5012, low: 4996, close: 5005, volume: 25, is_closed: true },
      { time: 1700000300, open: 5005, high: 5020, low: 5002, close: 5015, volume: 30, is_closed: true },
    ];
    fetchBarsMock.mockResolvedValue(makeBarsResponse("CON.F.US.MES.H26", bars));
    postDrawingCommandMock.mockResolvedValue({
      symbol: "MES",
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
      scope_revision: 1,
      can_undo: true,
      can_redo: false,
      deduplicated: false,
      affected_ids: ["fvg-new"],
      drawings: [],
    });

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

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const canvas = document.querySelector(".chart-pane__canvas") as HTMLElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 600,
      bottom: 400,
      width: 600,
      height: 400,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    const attachedPrimitive = attachPrimitiveMock.mock.calls[0]![0] as {
      getRenderSnapshot: () => {
        projectedDraft: { drawing: { type: string; metadata: Record<string, unknown> }; anchors: Array<{ x: number; price: number }> } | null;
      };
    };

    useDrawingStore.getState().setObExtendRight(true);
    useDrawingStore.getState().setActiveTool("fvg_box");

    // Click #1 inside plot (< 5px pointerup) opens fvg_box draft without extendRight
    act(() => {
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 200, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerUp(canvas, { clientX: 100, clientY: 200, button: 0, pointerType: "mouse", pointerId: 1 });
    });
    expect(attachedPrimitive.getRenderSnapshot().projectedDraft?.drawing.type).toBe("fvg_box");
    expect(attachedPrimitive.getRenderSnapshot().projectedDraft?.drawing.metadata.extend_right).toBeUndefined();

    // Pointer gestures on Right Price Scale (565, 200) and Bottom Time Scale (200, 385) must NOT finalize draft
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 565, clientY: 200, button: 0, pointerType: "mouse", pointerId: 2 });
      fireEvent.pointerMove(canvas, { clientX: 580, clientY: 160, button: 0, pointerType: "mouse", pointerId: 2 });
      fireEvent.pointerUp(canvas, { clientX: 580, clientY: 160, button: 0, pointerType: "mouse", pointerId: 2 });
      fireEvent.pointerDown(canvas, { clientX: 200, clientY: 385, button: 0, pointerType: "mouse", pointerId: 3 });
      fireEvent.pointerMove(canvas, { clientX: 240, clientY: 390, button: 0, pointerType: "mouse", pointerId: 3 });
      fireEvent.pointerUp(canvas, { clientX: 240, clientY: 390, button: 0, pointerType: "mouse", pointerId: 3 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).not.toHaveBeenCalled();

    // Draft is still open and previews on subsequent in-plot pointermove
    act(() => {
      fireEvent.pointerMove(canvas, { clientX: 130, clientY: 160, button: 0, pointerType: "mouse", pointerId: 4 });
    });
    expect(attachedPrimitive.getRenderSnapshot().projectedDraft?.anchors[1]).toMatchObject({
      x: 130,
      price: 5010,
    });

    // Click #2 inside plot finalizes fvg_box cleanly without extend_right
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 130, clientY: 160, button: 0, pointerType: "mouse", pointerId: 4 });
      fireEvent.pointerUp(canvas, { clientX: 130, clientY: 160, button: 0, pointerType: "mouse", pointerId: 4 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(1);
    const createdCmd = postDrawingCommandMock.mock.calls[0]![1];
    if (createdCmd.action === "create") {
      expect(createdCmd.drawing.type).toBe("fvg_box");
      expect(createdCmd.drawing.metadata?.extend_right).toBeUndefined();
      expect(createdCmd.drawing.metadata?.direction).toBe("bullish");
    }
  });

  it("isolates active drag and drag-to-place draft gestures by pointerId against secondary pointers (P1-C01)", async () => {
    /**
     * Contract:
     * 1. While pointerId=1 is dragging an hline (>= 5px), secondary pointerId=2 pointerDown/Move/Up
     *    on the Right Price Scale or plot must NOT commit, cancel, or restore chart handleScroll/handleScale;
     *    pointerUp(pointerId=1) restores interaction and commits pointer 1's dragged price.
     * 2. While pointerId=1 is mid-drag-to-place on an fvg_box draft, secondary pointerId=2
     *    pointerDown/Move/Up must NOT prematurely finalize or cancel the draft until pointerId=1 releases.
     * Defect caught: Secondary/stray pointer events clobbering captured drag/draft state or unlocking scroll/scale.
     */
    const bars = [
      { time: 1700000000, open: 5000, high: 5012, low: 4996, close: 5005, volume: 25, is_closed: true },
      { time: 1700000300, open: 5005, high: 5020, low: 5002, close: 5015, volume: 30, is_closed: true },
    ];
    const hlineDrawing = {
      id: "hl-pointer-iso",
      revision: 1,
      type: "hline" as const,
      symbol: "MES" as const,
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      created_by: "user" as const,
      source_bar_refs: [],
      coordinates: { anchors: [{ role: "level", time: 1700000000, price: 5000 }] },
      style: {},
      metadata: {},
      created_at_utc: 1700000000,
      updated_at_utc: 1700000000,
    };

    fetchBarsMock.mockResolvedValue(makeBarsResponse("CON.F.US.MES.H26", bars));
    fetchDrawingsMock.mockResolvedValue({
      symbol: "MES",
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
      scope_revision: 1,
      can_undo: false,
      can_redo: false,
      drawings: [hlineDrawing],
    });
    postDrawingCommandMock.mockResolvedValue({
      symbol: "MES",
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
      scope_revision: 2,
      can_undo: true,
      can_redo: false,
      deduplicated: false,
      affected_ids: ["hl-pointer-iso"],
      drawings: [
        {
          ...hlineDrawing,
          revision: 2,
          coordinates: { anchors: [{ role: "level", time: 1700000000, price: 5005 }] },
        },
      ],
    });

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

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const canvas = document.querySelector(".chart-pane__canvas") as HTMLElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 600,
      bottom: 400,
      width: 600,
      height: 400,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    const attachedPrimitive = attachPrimitiveMock.mock.calls[0]![0] as {
      getRenderSnapshot: () => {
        projectedDraft: { drawing: { type: string }; anchors: Array<{ x: number; price: number }> } | null;
      };
    };

    // 1. Active hline drag with pointerId=1 (5000 -> y=200 moved to y=180 => price=5005)
    applyOptionsMock.mockClear();
    act(() => {
      fireEvent.pointerDown(canvas, { clientX: 160, clientY: 200, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerMove(canvas, { clientX: 160, clientY: 180, button: 0, pointerType: "mouse", pointerId: 1 });
    });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: false, handleScale: false });

    // Secondary pointerId=2 events (on Right Price Scale and inside plot) must be completely ignored
    applyOptionsMock.mockClear();
    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 565, clientY: 200, button: 0, pointerType: "touch", pointerId: 2 });
      fireEvent.pointerMove(canvas, { clientX: 250, clientY: 120, button: 0, pointerType: "touch", pointerId: 2 });
      fireEvent.pointerUp(canvas, { clientX: 250, clientY: 120, button: 0, pointerType: "touch", pointerId: 2 });
      fireEvent.pointerCancel(canvas, { pointerType: "touch", pointerId: 2 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).not.toHaveBeenCalled();
    expect(applyOptionsMock).not.toHaveBeenCalledWith({ handleScroll: true, handleScale: true });

    // Matching pointerId=1 pointerUp restores scroll/scale and commits pointer 1's coordinates (price=5005)
    await act(async () => {
      fireEvent.pointerUp(canvas, { clientX: 160, clientY: 180, button: 0, pointerType: "mouse", pointerId: 1 });
      await Promise.resolve();
    });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: true, handleScale: true });
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(1);
    expect(postDrawingCommandMock.mock.calls[0]![1]).toMatchObject({
      action: "update",
      drawing_id: "hl-pointer-iso",
      drawing: {
        coordinates: {
          anchors: [{ role: "level", price: 5005 }],
        },
      },
    });

    // 2. Active drag-to-place fvg_box draft with pointerId=1 ignores secondary pointerId=2
    postDrawingCommandMock.mockClear();
    postDrawingCommandMock.mockResolvedValueOnce({
      symbol: "MES",
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
      scope_revision: 3,
      can_undo: true,
      can_redo: false,
      deduplicated: false,
      affected_ids: ["fvg-pointer-iso"],
      drawings: [],
    });
    useDrawingStore.getState().setActiveTool("fvg_box");
    applyOptionsMock.mockClear();
    act(() => {
      fireEvent.pointerDown(canvas, { clientX: 100, clientY: 200, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerMove(canvas, { clientX: 130, clientY: 160, button: 0, pointerType: "mouse", pointerId: 1 });
    });
    expect(attachedPrimitive.getRenderSnapshot().projectedDraft?.anchors[1]).toMatchObject({
      x: 130,
      price: 5010,
    });

    await act(async () => {
      fireEvent.pointerDown(canvas, { clientX: 220, clientY: 100, button: 0, pointerType: "touch", pointerId: 2 });
      fireEvent.pointerMove(canvas, { clientX: 260, clientY: 90, button: 0, pointerType: "touch", pointerId: 2 });
      fireEvent.pointerUp(canvas, { clientX: 260, clientY: 90, button: 0, pointerType: "touch", pointerId: 2 });
      fireEvent.pointerCancel(canvas, { pointerType: "touch", pointerId: 2 });
      await Promise.resolve();
    });
    expect(postDrawingCommandMock).not.toHaveBeenCalled();
    expect(applyOptionsMock).not.toHaveBeenCalledWith({ handleScroll: true, handleScale: true });
    // Secondary pointerMove did NOT overwrite draft's second anchor
    expect(attachedPrimitive.getRenderSnapshot().projectedDraft?.anchors[1]).toMatchObject({
      x: 130,
      price: 5010,
    });

    // Releasing matching pointerId=1 finalizes the drag-to-place fvg_box at (130, 160) -> price=5010
    await act(async () => {
      fireEvent.pointerUp(canvas, { clientX: 130, clientY: 160, button: 0, pointerType: "mouse", pointerId: 1 });
      await Promise.resolve();
    });
    expect(applyOptionsMock).toHaveBeenCalledWith({ handleScroll: true, handleScale: true });
    expect(postDrawingCommandMock).toHaveBeenCalledTimes(1);
    expect(postDrawingCommandMock.mock.calls[0]![1]).toMatchObject({
      action: "create",
      drawing: {
        type: "fvg_box",
        coordinates: {
          anchors: [
            { role: "start", time: 1700000000, price: 5000 },
            { role: "end", time: 1700000300, price: 5010 },
          ],
        },
      },
    });
  });

  it("keeps suppressTimeClickRef armed across mid-gesture WS revision and Escape cancellations, then syncs on fresh empty click (P1-002)", async () => {
    /**
     * Contract: When pointerdown hits a drawing (< 5px deadzone) and a concurrent WS revision bump
     * or Escape cancellation clears dragRef/selection before pointerup + Lightweight Charts click,
     * the trailing click must NOT trigger Time-sync centerOnTime; a subsequent empty-plot click DOES sync.
     * Defect caught: Mid-gesture cancellation resetting suppressTimeClickRef prematurely and causing unintended Time sync jump.
     */
    const bars = [
      { time: 1700000000, open: 5000, high: 5012, low: 4996, close: 5005, volume: 25, is_closed: true },
      { time: 1700000300, open: 5005, high: 5020, low: 5002, close: 5015, volume: 30, is_closed: true },
    ];
    const hlineDrawing = {
      id: "hl-time-suppress",
      revision: 1,
      type: "hline" as const,
      symbol: "MES" as const,
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      created_by: "user" as const,
      source_bar_refs: [],
      coordinates: { anchors: [{ role: "level", time: 1700000000, price: 5000 }] },
      style: {},
      metadata: {},
      created_at_utc: 1700000000,
      updated_at_utc: 1700000000,
    };

    fetchBarsMock.mockResolvedValue(makeBarsResponse("CON.F.US.MES.H26", bars));
    fetchDrawingsMock.mockResolvedValue({
      symbol: "MES",
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
      scope_revision: 1,
      can_undo: false,
      can_redo: false,
      drawings: [hlineDrawing],
    });

    const syncController = new LayoutSyncController({
      symbol: false,
      interval: false,
      crosshair: false,
      time: true,
      dateRange: false,
    });
    const peerCenterOnTime = vi.fn();
    syncController.registerPane({
      paneId: "pane-peer",
      applyCrosshairTime: vi.fn(),
      applyVisibleTimeRange: vi.fn(),
      centerOnTime: peerCenterOnTime,
    });
    const onTimeClickSpy = vi.spyOn(syncController, "onTimeClick");

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
        syncController={syncController}
      />,
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const canvas = document.querySelector(".chart-pane__canvas") as HTMLElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 600,
      bottom: 400,
      width: 600,
      height: 400,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    // Case A: pointerdown hits hline (y=200), < 5px move, WS bumps revision 1 -> 2 mid-gesture
    act(() => {
      fireEvent.pointerDown(canvas, { clientX: 160, clientY: 200, button: 0, pointerType: "mouse", pointerId: 1 });
      fireEvent.pointerMove(canvas, { clientX: 161, clientY: 201, button: 0, pointerType: "mouse", pointerId: 1 });
      useDrawingStore.getState().applyWsDrawingsUpdate({
        type: "drawings_update",
        symbol: "MES",
        contract_id: "CON.F.US.MES.H26",
        pane_id: "pane-1",
        scope_revision: 2,
        command_type: "update",
        can_undo: true,
        can_redo: false,
        drawings: [{ ...hlineDrawing, revision: 2 }],
      });
      fireEvent.pointerUp(canvas, { clientX: 161, clientY: 201, button: 0, pointerType: "mouse", pointerId: 1 });
      for (const handler of clickHandlers) {
        handler({ time: 1700000000 });
      }
    });
    expect(onTimeClickSpy).not.toHaveBeenCalled();
    expect(peerCenterOnTime).not.toHaveBeenCalled();

    // Case B: pointerdown hits hline again, simulated Escape clears selection mid-gesture (< 5px) before pointerup + click
    const scopeTarget = { symbol: "MES" as const, contractId: "CON.F.US.MES.H26", paneId: "pane-1" };
    act(() => {
      fireEvent.pointerDown(canvas, { clientX: 160, clientY: 200, button: 0, pointerType: "mouse", pointerId: 2 });
      useDrawingStore.getState().setSelectedDrawing(scopeTarget, null);
      fireEvent.pointerUp(canvas, { clientX: 160, clientY: 200, button: 0, pointerType: "mouse", pointerId: 2 });
      for (const handler of clickHandlers) {
        handler({ time: 1700000000 });
      }
    });
    expect(onTimeClickSpy).not.toHaveBeenCalled();
    expect(peerCenterOnTime).not.toHaveBeenCalled();

    // Fresh empty-plot click (y=60 misses hline at y=200) resets suppressTimeClickRef and triggers Time sync
    act(() => {
      fireEvent.pointerDown(canvas, { clientX: 160, clientY: 60, button: 0, pointerType: "mouse", pointerId: 3 });
      fireEvent.pointerUp(canvas, { clientX: 160, clientY: 60, button: 0, pointerType: "mouse", pointerId: 3 });
      for (const handler of clickHandlers) {
        handler({ time: 1700000000 });
      }
    });
    expect(onTimeClickSpy).toHaveBeenCalledTimes(1);
    expect(onTimeClickSpy).toHaveBeenCalledWith("pane-1", 1700000000);
    expect(peerCenterOnTime).toHaveBeenCalledTimes(1);
    expect(peerCenterOnTime).toHaveBeenCalledWith(1700000000);
  });

  it("re-syncs drawing scope when /ws/market transitions to connected even on historical-only 3m panes (P2-INIT-001)", async () => {
    /**
     * Contract: Historical-only panes (e.g. 3m, !canSubscribeWs) do not receive subscribed ACKs,
     * so ChartPane subscribes to wsClient.onConnectionState and re-runs loadScope() when WS enters
     * 'connected', closing any pre-handshake drawings_update race.
     * Defect caught: Stale drawings on 3m panes when another client/agent mutates before WS handshake completes.
     */
    const bars = [
      { time: 1700000000, open: 5000, high: 5012, low: 4996, close: 5005, volume: 25, is_closed: true },
    ];
    fetchBarsMock.mockResolvedValue({
      ...makeBarsResponse("CON.F.US.MES.H26", bars),
      timeframe: "3m",
    });
    fetchDrawingsMock
      .mockResolvedValueOnce({
        symbol: "MES",
        contract_id: "CON.F.US.MES.H26",
        pane_id: "pane-3m",
        instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
        scope_revision: 1,
        can_undo: false,
        can_redo: false,
        drawings: [],
      })
      .mockResolvedValueOnce({
        symbol: "MES",
        contract_id: "CON.F.US.MES.H26",
        pane_id: "pane-3m",
        instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
        scope_revision: 2,
        can_undo: true,
        can_redo: false,
        drawings: [
          {
            id: "hl-missed-handshake",
            revision: 1,
            type: "hline",
            symbol: "MES",
            contract_id: "CON.F.US.MES.H26",
            pane_id: "pane-3m",
            created_by: "agent",
            source_bar_refs: [],
            coordinates: { anchors: [{ role: "level", time: 1700000000, price: 5002 }] },
            style: {},
            metadata: {},
            created_at_utc: 1700000000,
            updated_at_utc: 1700000000,
          },
        ],
      });

    const coordinator = new PaneFetchCoordinator();
    const fakeWs = createFakeWsClient();
    const pane: PaneConfig = {
      pane_id: "pane-3m",
      root_symbol: "MES",
      timeframe: "3m",
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

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const scopeKey = buildDrawingScopeKey("MES", "CON.F.US.MES.H26", "pane-3m");
    expect(fetchDrawingsMock).toHaveBeenCalledTimes(1);
    expect(useDrawingStore.getState().scopes[scopeKey]?.scopeRevision).toBe(1);

    await act(async () => {
      fakeWs.emitConnectionState("connected");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchDrawingsMock).toHaveBeenCalledTimes(2);
    expect(useDrawingStore.getState().scopes[scopeKey]?.scopeRevision).toBe(2);
    expect(useDrawingStore.getState().scopes[scopeKey]?.drawings.map((d) => d.id)).toEqual([
      "hl-missed-handshake",
    ]);
  });
});
