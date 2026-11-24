// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type FetchBarsParams } from "../api/client";
import type {
  BarsResponse,
  OhlcvBar,
  PaneConfig,
  Timeframe,
  WsBarUpdateMessage,
  WsResyncRequiredMessage,
  WsSubscribedMessage,
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
    vi.useRealTimers();
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
});
