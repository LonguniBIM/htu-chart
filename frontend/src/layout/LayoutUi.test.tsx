// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type FetchBarsParams } from "../api/client";
import type { BarsResponse, LayoutPayload, LayoutRecord, Timeframe } from "../api/types";
import type { MarketWsClient } from "../api/wsClient";
import { PaneFetchCoordinator } from "../chart/fetchGuard";
import { useLayoutStore } from "../store/layoutStore";
import { LayoutPicker } from "./LayoutPicker";
import { SyncControls } from "./SyncControls";
import { TopToolbar } from "./TopToolbar";
import { LayoutSyncController } from "./syncController";
import { WorkspaceGrid } from "./WorkspaceGrid";

interface MockChartInstance {
  candleSeries: {
    setData: ReturnType<typeof vi.fn>;
    update: ReturnType<typeof vi.fn>;
    priceScale: () => { applyOptions: ReturnType<typeof vi.fn> };
  };
  setCrosshairPosition: ReturnType<typeof vi.fn>;
  clearCrosshairPosition: ReturnType<typeof vi.fn>;
  getVisibleRange: ReturnType<typeof vi.fn<() => { from: number; to: number } | null>>;
  setVisibleRange: ReturnType<typeof vi.fn>;
  crosshairHandlers: Array<(param: unknown) => void>;
  clickHandlers: Array<(param: unknown) => void>;
  visibleRangeHandlers: Array<(range: { from: number; to: number } | null) => void>;
}

const mockChartInstances: MockChartInstance[] = [];

vi.mock("lightweight-charts", () => {
  const CandlestickSeries = Symbol("CandlestickSeries");
  const HistogramSeries = Symbol("HistogramSeries");
  const LineSeries = Symbol("LineSeries");
  return {
    CandlestickSeries,
    HistogramSeries,
    LineSeries,
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    createSeriesMarkers: vi.fn(() => ({
      setMarkers: vi.fn(),
      markers: () => [],
      detach: vi.fn(),
    })),
    createChart: vi.fn(() => {
      const candleSeries = {
        setData: vi.fn(),
        update: vi.fn(),
        priceScale: () => ({ applyOptions: vi.fn() }),
      };
      const volumeSeries = {
        setData: vi.fn(),
        update: vi.fn(),
        priceScale: () => ({ applyOptions: vi.fn() }),
      };
      const setCrosshairPosition = vi.fn();
      const clearCrosshairPosition = vi.fn();
      const getVisibleRange = vi.fn<() => { from: number; to: number } | null>(() => null);
      const setVisibleRange = vi.fn();
      const instance: MockChartInstance = {
        candleSeries,
        setCrosshairPosition,
        clearCrosshairPosition,
        getVisibleRange,
        setVisibleRange,
        crosshairHandlers: [],
        clickHandlers: [],
        visibleRangeHandlers: [],
      };
      mockChartInstances.push(instance);
      return {
        addSeries: vi.fn((seriesType: unknown) =>
          seriesType === CandlestickSeries ? candleSeries : volumeSeries,
        ),
        subscribeCrosshairMove: vi.fn((handler: (param: unknown) => void) => {
          instance.crosshairHandlers.push(handler);
        }),
        unsubscribeCrosshairMove: vi.fn((handler: (param: unknown) => void) => {
          instance.crosshairHandlers = instance.crosshairHandlers.filter((h) => h !== handler);
        }),
        subscribeClick: vi.fn((handler: (param: unknown) => void) => {
          instance.clickHandlers.push(handler);
        }),
        unsubscribeClick: vi.fn((handler: (param: unknown) => void) => {
          instance.clickHandlers = instance.clickHandlers.filter((h) => h !== handler);
        }),
        setCrosshairPosition,
        clearCrosshairPosition,
        applyOptions: vi.fn(),
        remove: vi.fn(),
        timeScale: () => ({
          fitContent: vi.fn(),
          subscribeVisibleTimeRangeChange: vi.fn(
            (handler: (range: { from: number; to: number } | null) => void) => {
              instance.visibleRangeHandlers.push(handler);
            },
          ),
          unsubscribeVisibleTimeRangeChange: vi.fn(
            (handler: (range: { from: number; to: number } | null) => void) => {
              instance.visibleRangeHandlers = instance.visibleRangeHandlers.filter(
                (h) => h !== handler,
              );
            },
          ),
          getVisibleRange,
          setVisibleRange,
        }),
      };
    }),
  };
});

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

const createLayoutMock = vi.fn<
  (payload: LayoutPayload, signal?: AbortSignal) => Promise<LayoutRecord>
>();
const updateLayoutMock = vi.fn<
  (layoutId: string, payload: LayoutPayload, signal?: AbortSignal) => Promise<LayoutRecord>
>();
const deleteLayoutMock = vi.fn<
  (layoutId: string, signal?: AbortSignal) => Promise<{ deleted: boolean; id: string }>
>();
const listLayoutsMock = vi.fn<(signal?: AbortSignal) => Promise<LayoutRecord[]>>();
const fetchBarsMock = vi.fn<
  (params: FetchBarsParams, signal?: AbortSignal) => Promise<BarsResponse>
>();

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    createLayout: (payload: LayoutPayload, signal?: AbortSignal) =>
      createLayoutMock(payload, signal),
    updateLayout: (layoutId: string, payload: LayoutPayload, signal?: AbortSignal) =>
      updateLayoutMock(layoutId, payload, signal),
    deleteLayout: (layoutId: string, signal?: AbortSignal) =>
      deleteLayoutMock(layoutId, signal),
    listLayouts: (signal?: AbortSignal) => listLayoutsMock(signal),
    fetchBars: (params: FetchBarsParams, signal?: AbortSignal) =>
      fetchBarsMock(params, signal),
  };
});

function makeDefaultLayout(): LayoutRecord {
  return {
    id: "layout-4-future",
    name: "4 Future",
    template_id: "4-grid-2x2",
    panes: [
      { pane_id: "pane-1", root_symbol: "MES", timeframe: "5m", live_mode: false, playback_mode: "live" },
      { pane_id: "pane-2", root_symbol: "MNQ", timeframe: "5m", live_mode: false, playback_mode: "live" },
      { pane_id: "pane-3", root_symbol: "MYM", timeframe: "5m", live_mode: false, playback_mode: "live" },
      { pane_id: "pane-4", root_symbol: "MGC", timeframe: "5m", live_mode: false, playback_mode: "live" },
    ],
    sync_config: {
      symbol: false,
      interval: true,
      crosshair: true,
      time: false,
      dateRange: false,
    },
    is_default: true,
    created_at_utc: 1700000000,
    updated_at_utc: 1700000000,
  };
}

describe("LayoutPicker and SyncControls UI (Phase 3 Part 2)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useLayoutStore.getState().setAuthErrorHandler(null);
    useLayoutStore.getState().reset();
  });

  afterEach(() => {
    cleanup();
  });

  it("renders all 21 1-8 pane templates, selects templates with dirty indicator, and toggles all 5 sync switches", () => {
    /**
     * Contract: LayoutPicker lists all 21 canonical templates with SVG preview rects and
     * aria-pressed state, marks dirty (*) on template switch, and SyncControls toggles all
     * 5 SYNC IN LAYOUT switches independently.
     * Defect caught: Missing template buttons in dropdown or non-functional sync switches.
     */
    useLayoutStore.getState().hydrateLayouts([makeDefaultLayout()]);

    render(
      <div>
        <LayoutPicker />
        <SyncControls />
      </div>,
    );

    const trigger = screen.getByRole("button", { name: "Layout picker" });
    expect(trigger.textContent).toContain("4 Future");
    expect(trigger.textContent).not.toContain("*");

    fireEvent.click(trigger);
    const tplButtons = document.querySelectorAll("[data-template-id]");
    expect(tplButtons).toHaveLength(21);
    expect(
      screen.getByRole("button", { name: "4 Grid (2x2)" }).getAttribute("aria-pressed"),
    ).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "8 Grid (4x2)" }));
    expect(useLayoutStore.getState().activeLayout?.template_id).toBe("8-grid-4x2");
    expect(useLayoutStore.getState().activeLayout?.panes).toHaveLength(8);
    expect(trigger.textContent).toContain("4 Future *");

    // Toggle all 5 SYNC IN LAYOUT switches
    const switches = [
      { name: "Sync Symbol", key: "symbol", initial: false },
      { name: "Sync Interval", key: "interval", initial: true },
      { name: "Sync Crosshair", key: "crosshair", initial: true },
      { name: "Sync Time", key: "time", initial: false },
      { name: "Sync Date range", key: "dateRange", initial: false },
    ] as const;

    for (const item of switches) {
      const btn = screen.getByRole("switch", { name: item.name });
      expect(btn.getAttribute("aria-checked")).toBe(String(item.initial));
      fireEvent.click(btn);
      expect(btn.getAttribute("aria-checked")).toBe(String(!item.initial));
      expect(useLayoutStore.getState().activeLayout?.sync_config[item.key]).toBe(!item.initial);
    }
  });

  it("supports Save As, Rename, Set Default, Delete, and disables Delete on final remaining layout", async () => {
    /**
     * Contract: LayoutPicker disables Delete when only 1 layout remains, enables Save As /
     * Rename / Set Default / Delete when multiple layouts exist, and displays 409 errors inline.
     * Defect caught: Allowing delete of last layout or hiding persistence error feedback.
     */
    const base = makeDefaultLayout();
    const scalp: LayoutRecord = {
      ...base,
      id: "layout-scalp",
      name: "Scalp",
      is_default: false,
    };
    const scalpRenamed: LayoutRecord = {
      ...scalp,
      name: "Scalp V2",
    };

    createLayoutMock.mockResolvedValueOnce(scalp);
    deleteLayoutMock
      .mockRejectedValueOnce(new ApiError(409, "Cannot delete the last remaining layout"))
      .mockResolvedValueOnce({ deleted: true, id: "layout-4-future" });

    useLayoutStore.getState().hydrateLayouts([base]);
    render(<LayoutPicker />);
    fireEvent.click(screen.getByRole("button", { name: "Layout picker" }));

    // Only 1 layout -> Delete button is disabled, Save is disabled while clean
    const deleteBaseBtn = screen.getByRole("button", { name: "Delete 4 Future" }) as HTMLButtonElement;
    expect(deleteBaseBtn.disabled).toBe(true);
    const saveBtn = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(true);

    // Editing template marks dirty -> Save enables, and Save disables again after resolving
    fireEvent.click(screen.getByRole("button", { name: "2 Columns" }));
    expect(saveBtn.disabled).toBe(false);
    const savedBase: LayoutRecord = {
      ...base,
      template_id: "2-cols",
      panes: base.panes.slice(0, 2),
    };
    updateLayoutMock.mockResolvedValueOnce(savedBase);
    await act(async () => {
      fireEvent.click(saveBtn);
    });
    expect(updateLayoutMock).toHaveBeenCalledTimes(1);
    expect(saveBtn.disabled).toBe(true);

    // Save As "Scalp" (non-default)
    fireEvent.change(screen.getByLabelText("Save layout as"), {
      target: { value: "Scalp" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save As" }));
    });
    expect(createLayoutMock).toHaveBeenCalledTimes(1);
    expect(useLayoutStore.getState().activeLayout?.name).toBe("Scalp");
    expect(saveBtn.disabled).toBe(true);

    // Rename "Scalp" -> "Scalp V2"
    updateLayoutMock.mockResolvedValueOnce(scalpRenamed);
    fireEvent.change(screen.getByLabelText("Rename layout"), {
      target: { value: "Scalp V2" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    });
    expect(useLayoutStore.getState().activeLayout?.name).toBe("Scalp V2");

    // Set Default on active non-default layout "Scalp V2"
    const scalpDefault: LayoutRecord = { ...scalpRenamed, is_default: true };
    updateLayoutMock.mockResolvedValueOnce(scalpDefault);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Set Default" }));
    });
    expect(updateLayoutMock).toHaveBeenLastCalledWith(
      "layout-scalp",
      expect.objectContaining({ name: "Scalp V2", is_default: true }),
      expect.any(AbortSignal),
    );
    expect(useLayoutStore.getState().activeLayout?.is_default).toBe(true);
    expect(screen.getByText("Default", { selector: ".terminal-badge" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Set Default" })).toBeNull();

    // Delete 409 surfaces inline alert, then successful Delete removes "4 Future"
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Delete 4 Future" }));
    });
    expect(screen.getByRole("alert").textContent).toContain("409");

    deleteLayoutMock.mockResolvedValueOnce({ deleted: true, id: "layout-4-future" });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Delete 4 Future" }));
    });
    expect(screen.queryByRole("button", { name: "Delete 4 Future" })).toBeNull();
    expect(useLayoutStore.getState().layouts).toHaveLength(1);
  });
});

function makeMockWsClient(): MarketWsClient {
  return {
    subscribe: vi.fn<(_contractId: string, _timeframe: Timeframe) => () => void>(() => () => {}),
    onBarUpdate: vi.fn(() => () => {}),
    onSubscribed: vi.fn(() => () => {}),
    onResyncRequired: vi.fn(() => () => {}),
  } as unknown as MarketWsClient;
}

describe("WorkspaceGrid UI and ChartPane LayoutSyncController integration (Phase 3 Part 2)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockChartInstances.length = 0;
    useLayoutStore.getState().setAuthErrorHandler(null);
    useLayoutStore.getState().reset();
    vi.stubGlobal("ResizeObserver", MockResizeObserver);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders WorkspaceGrid template CSS grid tracks, per-pane gridColumn/gridRow, and updates activePaneId on mouseDown", async () => {
    /**
     * Contract: WorkspaceGrid applies gridTemplateColumns/gridTemplateRows from getTemplateDefinition(),
     * applies per-pane gridColumn/gridRow styles, and dispatches onSelectPane on mouseDown to update
     * activePaneId and data-active="true".
     * Defect caught: Missing grid track/area inline styles or broken mouseDown active pane selection.
     */
    const layout3LeftMain: LayoutRecord = {
      ...makeDefaultLayout(),
      id: "layout-3-left",
      name: "3 Left Main",
      template_id: "3-left-main",
      panes: [
        { pane_id: "pane-1", root_symbol: "MES", timeframe: "5m", live_mode: false, playback_mode: "live" },
        { pane_id: "pane-2", root_symbol: "MNQ", timeframe: "5m", live_mode: false, playback_mode: "live" },
        { pane_id: "pane-3", root_symbol: "MYM", timeframe: "5m", live_mode: false, playback_mode: "live" },
      ],
    };

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
      quality: { is_complete: true, truncated: false, provider_gap: false, quarantined_count: 0 },
      bars: [],
    });

    useLayoutStore.getState().hydrateLayouts([layout3LeftMain]);
    const coordinator = new PaneFetchCoordinator();
    const syncController = new LayoutSyncController();
    const wsClient = makeMockWsClient();

    function GridHarness() {
      const layout = useLayoutStore((state) => state.activeLayout);
      const activePaneId = useLayoutStore((state) => state.activePaneId);
      const selectPane = useLayoutStore((state) => state.selectPane);
      return (
        <WorkspaceGrid
          layout={layout}
          activePaneId={activePaneId}
          contracts={{}}
          activeFeedLive={false}
          wsClient={wsClient}
          coordinator={coordinator}
          syncController={syncController}
          onSelectPane={selectPane}
          onSymbolChange={vi.fn()}
          onTimeframeChange={vi.fn()}
          onAuthError={vi.fn()}
        />
      );
    }

    render(<GridHarness />);
    await act(async () => {
      await Promise.resolve();
    });

    const grid = document.querySelector(".terminal-grid") as HTMLElement;
    expect(grid.style.gridTemplateColumns).toBe("repeat(2, minmax(0px, 1fr))");
    expect(grid.style.gridTemplateRows).toBe("repeat(2, minmax(0px, 1fr))");

    const pane1 = document.querySelector('[data-pane-id="pane-1"]') as HTMLElement;
    const pane2 = document.querySelector('[data-pane-id="pane-2"]') as HTMLElement;
    const pane3 = document.querySelector('[data-pane-id="pane-3"]') as HTMLElement;

    expect(pane1.style.gridColumn).toBe("1 / 2");
    expect(pane1.style.gridRow).toBe("1 / 3");
    expect(pane2.style.gridColumn).toBe("2 / 3");
    expect(pane2.style.gridRow).toBe("1 / 2");
    expect(pane3.style.gridColumn).toBe("2 / 3");
    expect(pane3.style.gridRow).toBe("2 / 3");

    expect(pane1.getAttribute("data-active")).toBe("true");
    expect(pane2.getAttribute("data-active")).toBe("false");

    fireEvent.mouseDown(pane2);
    expect(useLayoutStore.getState().activePaneId).toBe("pane-2");
    expect(pane1.getAttribute("data-active")).toBe("false");
    expect(pane2.getAttribute("data-active")).toBe("true");
  });

  it("synchronizes 2 mounted ChartPane instances via shared LayoutSyncController for crosshair, dateRange, and time", async () => {
    /**
     * Contract: Two ChartPane instances mounted by WorkspaceGrid register with a shared
     * LayoutSyncController; crosshair move, visible range change, and click on pane-1
     * synchronize pane-2's chart instance without echoing back to pane-1.
     * Defect caught: ChartPane failing to wire subscribeCrosshairMove/subscribeClick/timeScale
     * through LayoutSyncController or echoing synced updates back to source pane.
     */
    const twoPaneLayout: LayoutRecord = {
      ...makeDefaultLayout(),
      id: "layout-2-cols",
      name: "2 Cols Sync",
      template_id: "2-cols",
      panes: [
        { pane_id: "pane-1", root_symbol: "MES", timeframe: "5m", live_mode: false, playback_mode: "live" },
        { pane_id: "pane-2", root_symbol: "MNQ", timeframe: "5m", live_mode: false, playback_mode: "live" },
      ],
      sync_config: {
        symbol: false,
        interval: false,
        crosshair: true,
        time: true,
        dateRange: true,
      },
    };

    fetchBarsMock.mockImplementation(async (params) => ({
      provider: "topstepx",
      live: false,
      contract_id: params.contract_id,
      root_symbol: params.root_symbol ?? "MES",
      timeframe: params.timeframe,
      start_time: 1700000000,
      end_time: 1700000600,
      count: 2,
      include_partial: true,
      quality: { is_complete: true, truncated: false, provider_gap: false, quarantined_count: 0 },
      bars: [
        { time: 1700000000, open: 5000, high: 5005, low: 4995, close: 5002, volume: 10, is_closed: true },
        { time: 1700000300, open: 5002, high: 5010, low: 5001, close: 5008, volume: 15, is_closed: true },
      ],
    }));

    const coordinator = new PaneFetchCoordinator();
    const syncController = new LayoutSyncController(twoPaneLayout.sync_config);
    const wsClient = makeMockWsClient();

    render(
      <WorkspaceGrid
        layout={twoPaneLayout}
        activePaneId="pane-1"
        contracts={{
          "sim:MES": { selected: { id: "CON.F.US.MES.H26" }, error_type: null, error: null },
          "sim:MNQ": { selected: { id: "CON.F.US.MNQ.H26" }, error_type: null, error: null },
        }}
        activeFeedLive={false}
        wsClient={wsClient}
        coordinator={coordinator}
        syncController={syncController}
        onSelectPane={vi.fn()}
        onSymbolChange={vi.fn()}
        onTimeframeChange={vi.fn()}
        onAuthError={vi.fn()}
      />,
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockChartInstances).toHaveLength(2);
    const chart1 = mockChartInstances[0]!;
    const chart2 = mockChartInstances[1]!;

    // 1. Crosshair move on pane-1 updates pane-2 only
    act(() => {
      chart1.crosshairHandlers[0]?.({
        time: 1700000300,
        seriesData: new Map(),
      });
    });
    expect(chart2.setCrosshairPosition).toHaveBeenCalledWith(
      5008,
      1700000300,
      chart2.candleSeries,
    );
    expect(chart1.setCrosshairPosition).not.toHaveBeenCalled();

    // 2. Visible range change on pane-1 updates pane-2 only
    const range = { from: 1700000000, to: 1700000600 };
    act(() => {
      chart1.visibleRangeHandlers[0]?.(range);
    });
    expect(chart2.setVisibleRange).toHaveBeenCalledWith(range);
    expect(chart1.setVisibleRange).not.toHaveBeenCalled();

    // 3. Time click on pane-1 centers pane-2's visible range around clicked timestamp
    chart2.getVisibleRange.mockReturnValueOnce({ from: 1700000000, to: 1700000400 });
    act(() => {
      chart1.clickHandlers[0]?.({ time: 1700000300 });
    });
    expect(chart2.setVisibleRange).toHaveBeenLastCalledWith({
      from: 1700000100,
      to: 1700000500,
    });
    expect(chart1.setVisibleRange).not.toHaveBeenCalled();
  });

  it("renders TopToolbar with optional Replay and Backtest toggles cleanly", () => {
    /**
     * Contract: TopToolbar renders cleanly when Replay/Backtest props are omitted and triggers
     * optional toggle callbacks with aria-pressed state when provided.
     * Defect caught: Breaking callers omitting new optional props or wiring toggle buttons wrongly.
     */
    const onToggleReplay = vi.fn();
    const onToggleBacktest = vi.fn();

    const { rerender } = render(
      <TopToolbar
        wsState="connected"
        statusData={{ feed_state: "LIVE" }}
        contractsPendingRetry={false}
        loadError={null}
        onSelectLayout={vi.fn()}
        onRetryContracts={vi.fn()}
        onLogout={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Replay" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Backtest" })).toBeNull();

    rerender(
      <TopToolbar
        wsState="connected"
        statusData={{ feed_state: "LIVE" }}
        contractsPendingRetry={false}
        loadError={null}
        onSelectLayout={vi.fn()}
        onRetryContracts={vi.fn()}
        onLogout={vi.fn()}
        isReplayOpen={true}
        onToggleReplay={onToggleReplay}
        isBacktestOpen={false}
        onToggleBacktest={onToggleBacktest}
      />,
    );

    const replayBtn = screen.getByRole("button", { name: "Replay" });
    const backtestBtn = screen.getByRole("button", { name: "Backtest" });
    expect(replayBtn.getAttribute("aria-pressed")).toBe("true");
    expect(backtestBtn.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(replayBtn);
    fireEvent.click(backtestBtn);
    expect(onToggleReplay).toHaveBeenCalledTimes(1);
    expect(onToggleBacktest).toHaveBeenCalledTimes(1);
  });
});
