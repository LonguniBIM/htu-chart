// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ISeriesApi, SeriesType, Time } from "lightweight-charts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type {
  BacktestJobDetail,
  BacktestJobSummary,
  BacktestResultPayload,
  BacktestStrategiesResponse,
  BacktestSubmitRequest,
  OhlcvBar,
} from "../api/types";
import { ReplayControlBar } from "../replay/ReplayControlBar";
import { useReplayStore } from "../replay/replayStore";
import { BacktestOverlayPrimitive } from "./BacktestOverlayPrimitive";
import { BacktestPanel } from "./BacktestPanel";
import { useBacktestStore } from "./backtestStore";

function makeSampleResult(): BacktestResultPayload {
  return {
    strategy_id: "ict_fvg_sweep",
    strategy_version: "1.0.0",
    contract_id: "CON.F.US.MES.H26",
    root_symbol: "MES",
    timeframe: "5m",
    live_mode: false,
    params: {
      swing_lookback: 5,
      sweep_window_bars: 5,
      min_fvg_ticks: 1,
      risk_reward: 2,
      sl_buffer_ticks: 1,
      entry_mode: "market",
      max_wait_bars: 5,
      quantity: 1,
      ema_period: 20,
      include_vwap: true,
    },
    instrument: {
      root_symbol: "MES",
      name: "Micro E-mini S&P 500",
      exchange: "CME",
      tick_size: 0.25,
      tick_value: 1.25,
      currency: "USD",
    },
    bar_count: 10,
    start_time: 1700000000,
    end_time: 1700002700,
    excluded_provisional_count: 0,
    trades: [
      {
        trade_id: "T1",
        direction: "LONG",
        signal_time: 1700000300,
        entry_time: 1700000600,
        entry_price: 5000,
        exit_time: 1700001200,
        exit_price: 4998,
        stop_loss: 4998,
        take_profit: 5004,
        quantity: 1,
        pnl_ticks: -8,
        pnl_dollars: -10,
        exit_reason: "stop_loss",
        ambiguous_fill: true,
        reason: "Bullish FVG + Liquidity Sweep",
      },
    ],
    markers: [
      {
        time: 1700000600,
        position: "belowBar",
        color: "#22c55e",
        shape: "arrowUp",
        text: "LONG @5000",
      },
    ],
    trade_lines: [
      {
        trade_id: "T1",
        direction: "LONG",
        entry_time: 1700000600,
        entry_price: 5000,
        exit_time: 1700001200,
        exit_price: 4998,
        pnl_dollars: -10,
        pnl_ticks: -8,
        color: "#ef4444",
        ambiguous_fill: true,
      },
    ],
    zones: [
      {
        kind: "fvg",
        direction: "bullish",
        start_time: 1700000000,
        end_time: 1700000600,
        detected_at_time: 1700000600,
        top: 5004,
        bottom: 5000,
        midpoint: 5002,
        label: "Bullish FVG",
      },
      {
        kind: "order_block",
        direction: "bearish",
        start_time: 1700000300,
        end_time: 1700000900,
        detected_at_time: 1700000900,
        top: 5008,
        bottom: 5005,
        midpoint: null,
        label: "Bearish OB",
      },
      {
        kind: "liquidity_level",
        direction: "bullish",
        start_time: 1700000000,
        end_time: 1700000600,
        detected_at_time: 1700000600,
        top: 5001,
        bottom: 4999,
        midpoint: 5001,
        label: "SSL Sweep",
      },
    ],
    indicators: [
      {
        id: "ema_20",
        name: "EMA (20)",
        color: "#f59e0b",
        overlay: true,
        points: [{ time: 1700000600, value: 5001 }],
      },
    ],
    summary: {
      net_profit_dollars: -10,
      gross_profit_dollars: 0,
      gross_loss_dollars: 10,
      win_rate_pct: 0,
      profit_factor: 0,
      max_drawdown_dollars: 10,
      total_trades: 1,
      winning_trades: 0,
      losing_trades: 1,
      breakeven_trades: 0,
      ambiguous_fill_count: 1,
      equity_curve: [{ time: 1700001200, equity_dollars: -10, trade_id: "T1" }],
    },
  };
}

function makeJobSummary(
  jobId: string,
  status: BacktestJobSummary["status"],
): BacktestJobSummary {
  return {
    job_id: jobId,
    status,
    contract_id: "CON.F.US.MES.H26",
    root_symbol: "MES",
    timeframe: "5m",
    resample_from: null,
    live_mode: false,
    start_time: 1700000000,
    end_time: 1700002700,
    strategy_id: "ict_fvg_sweep",
    params: {},
    max_bars: 50000,
    created_at_utc: 1700002700,
    started_at_utc: status === "QUEUED" ? null : 1700002701,
    finished_at_utc: status === "COMPLETED" || status === "CANCELLED" ? 1700002702 : null,
    error: null,
    result_bytes: status === "COMPLETED" ? 2048 : null,
    summary: status === "COMPLETED" ? makeSampleResult().summary : null,
  };
}

describe("Backtest Store, Overlay Primitive, Panel, and ReplayControlBar", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
    useBacktestStore.getState().setAuthErrorHandler(null);
    useBacktestStore.getState().reset();
    useReplayStore.getState().reset();
  });

  afterEach(() => {
    cleanup();
    useBacktestStore.getState().reset();
    useReplayStore.getState().reset();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("polls QUEUED -> RUNNING -> COMPLETED, prevents stale poll overwrite on reset, and supports cancel", async () => {
    /**
     * Contract: useBacktestStore.runBacktest submits POST /api/backtest, polls GET /api/backtest/{id}
     * through QUEUED -> RUNNING -> COMPLETED, cancels cleanly via DELETE, and ignores stale polls after reset().
     * Defect caught: Poll timer leaks, race overwrite across runs/logouts, or missing DELETE cancel call.
     */
    const queued = makeJobSummary("job-alpha-111", "QUEUED");
    const running: BacktestJobDetail = { ...makeJobSummary("job-alpha-111", "RUNNING"), result: null };
    const completed: BacktestJobDetail = {
      ...makeJobSummary("job-alpha-111", "COMPLETED"),
      result: makeSampleResult(),
    };

    const jsonResp = (data: unknown, status = 200) =>
      new Response(JSON.stringify(data), {
        status,
        headers: { "Content-Type": "application/json" },
      });

    fetchMock
      .mockResolvedValueOnce(jsonResp(queued, 202))
      .mockResolvedValueOnce(jsonResp(running))
      .mockResolvedValueOnce(jsonResp(completed));

    const payload: BacktestSubmitRequest = {
      contract_id: "CON.F.US.MES.H26",
      root_symbol: "MES",
      timeframe: "5m",
      live_mode: false,
      start_time: 1700000000,
      end_time: 1700002700,
      strategy_id: "ict_fvg_sweep",
    };

    const runPromise = useBacktestStore.getState().runBacktest(payload);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(useBacktestStore.getState().activeJob?.status).toBe("QUEUED");
    expect(useBacktestStore.getState().polling).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(useBacktestStore.getState().activeJob?.status).toBe("RUNNING");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    const finalDetail = await runPromise;
    expect(finalDetail?.status).toBe("COMPLETED");
    expect(useBacktestStore.getState().activeJob?.result?.trades).toHaveLength(1);
    expect(useBacktestStore.getState().polling).toBe(false);

    // Concurrent job + reset() must abort polling and never overwrite reset state
    const queuedBeta = makeJobSummary("job-beta-222", "QUEUED");
    fetchMock.mockResolvedValueOnce(jsonResp(queuedBeta, 202));
    const betaPromise = useBacktestStore.getState().runBacktest(payload);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(useBacktestStore.getState().activeJob?.job_id).toBe("job-beta-222");

    useBacktestStore.getState().reset();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    await betaPromise;
    expect(useBacktestStore.getState().activeJob).toBeNull();
    expect(useBacktestStore.getState().polling).toBe(false);

    // Cancel active job calls DELETE and stops polling
    const queuedGamma = makeJobSummary("job-gamma-333", "QUEUED");
    const cancelledGamma = makeJobSummary("job-gamma-333", "CANCELLED");
    fetchMock
      .mockResolvedValueOnce(jsonResp(queuedGamma, 202))
      .mockResolvedValueOnce(jsonResp(cancelledGamma, 200));
    void useBacktestStore.getState().runBacktest(payload);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      await useBacktestStore.getState().cancelActiveJob();
    });
    expect(useBacktestStore.getState().activeJob?.status).toBe("CANCELLED");
    expect(useBacktestStore.getState().polling).toBe(false);
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).includes("/api/backtest/job-gamma-333") && init?.method === "DELETE",
      ),
    ).toBe(true);
  });

  it("renders BacktestOverlayPrimitive zones (FVG 50% CE), trade lines (AMBIGUOUS), replay levels, and clips to plot bounds", () => {
    /**
     * Contract: BacktestOverlayPrimitive projects zones, trade lines, and replay Entry/SL/TP levels,
     * clips canvas drawing strictly to [0, plotWidth) x [0, plotHeight), and exposes priceAxisViews.
     * Defect caught: Unclipped canvas rendering overflowing onto axis scales or missing 50% CE / AMBIGUOUS / replay levels.
     */
    const primitive = new BacktestOverlayPrimitive();
    const bars: OhlcvBar[] = [
      { time: 1700000000, open: 5000, high: 5005, low: 4998, close: 5002, volume: 10, is_closed: true },
      { time: 1700000300, open: 5002, high: 5008, low: 5000, close: 5004, volume: 12, is_closed: true },
      { time: 1700000600, open: 5004, high: 5006, low: 4999, close: 5000, volume: 15, is_closed: true },
      { time: 1700000900, open: 5000, high: 5009, low: 4998, close: 5005, volume: 11, is_closed: true },
      { time: 1700001200, open: 5005, high: 5006, low: 4997, close: 4998, volume: 18, is_closed: true },
    ];
    const sample = makeSampleResult();

    const fakeChart = {
      timeScale: () => ({
        timeToCoordinate: (t: number) => 50 + (t - 1700000000) / 10,
        logicalToCoordinate: (l: number) => 50 + l * 30,
      }),
    };
    const fakeSeries = {
      priceToCoordinate: (p: number) => (5010 - p) * 10,
    } as unknown as ISeriesApi<SeriesType, Time>;

    primitive.attached({
      chart: fakeChart as never,
      series: fakeSeries as never,
      requestUpdate: vi.fn(),
      horzScaleBehavior: {} as never,
    });
    primitive.setViewportSize(420, 260);
    primitive.setData({
      zones: sample.zones,
      tradeLines: sample.trade_lines,
      showZones: true,
      showTradeLines: true,
      replayPosition: {
        tradeId: "R1",
        direction: "LONG",
        entryBarIndex: 2,
        entryTime: 1700000600,
        entryPrice: 5000,
        quantity: 1,
        stopLoss: 4998,
        takeProfit: 5004,
      },
      bars,
      timeframeSec: 300,
      tickSize: 0.25,
    });

    const snap = primitive.getRenderSnapshot();
    expect(snap.projectedZones).toHaveLength(3);
    expect(snap.projectedTradeLines).toHaveLength(1);
    expect(snap.projectedReplayLevels.map((l) => l.label)).toEqual(["Entry", "SL", "TP"]);
    expect(primitive.priceAxisViews().map((v) => v.text())).toEqual([
      "Entry 5000.00",
      "SL 4998.00",
      "TP 5004.00",
    ]);

    const drawnTexts: string[] = [];
    const mockCtx = {
      save: vi.fn(),
      restore: vi.fn(),
      beginPath: vi.fn(),
      rect: vi.fn(),
      clip: vi.fn(),
      fillRect: vi.fn(),
      strokeRect: vi.fn(),
      setLineDash: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      measureText: vi.fn((text: string) => ({ width: text.length * 6 })),
      fillText: vi.fn((text: string) => {
        drawnTexts.push(text);
      }),
      fillStyle: "",
      strokeStyle: "",
      lineWidth: 1,
      font: "",
      textBaseline: "alphabetic",
    };

    const view = primitive.paneViews()[0];
    const renderer = view?.renderer() ?? null;
    if (renderer === null) {
      throw new Error("Expected BacktestOverlayPrimitive renderer");
    }
    renderer.draw({
      useMediaCoordinateSpace: (
        cb: (scope: {
          context: CanvasRenderingContext2D;
          mediaSize: { width: number; height: number };
        }) => void,
      ) => {
        cb({
          context: mockCtx as unknown as CanvasRenderingContext2D,
          mediaSize: { width: 500, height: 300 },
        });
      },
    } as never);

    expect(mockCtx.rect).toHaveBeenCalledWith(0, 0, 420, 260);
    expect(mockCtx.clip).toHaveBeenCalledTimes(1);
    expect(drawnTexts.some((t) => t.includes("50% CE"))).toBe(true);
    expect(drawnTexts.some((t) => t.includes("AMBIGUOUS"))).toBe(true);
    expect(drawnTexts.some((t) => t.includes("SL 4998.00"))).toBe(true);
  });

  it("renders BacktestPanel and ReplayControlBar interactions cleanly", async () => {
    /**
     * Contract: BacktestPanel loads strategies, submits a backtest, displays metrics and
     * overlay toggles, and ReplayControlBar steps bars and executes local paper positions.
     * Defect caught: Broken form submission, missing metric readouts, or broken paper order controls.
     */
    const strategiesResp: BacktestStrategiesResponse = {
      strategies: [
        {
          strategy_id: "ict_fvg_sweep",
          strategy_version: "1.0.0",
          name: "ICT FVG + Liquidity Sweep",
          description: "ICT sample",
          default_params: {},
          param_schema: {},
        },
      ],
      count: 1,
    };
    const completed: BacktestJobDetail = {
      ...makeJobSummary("job-ui-999", "COMPLETED"),
      result: makeSampleResult(),
    };
    const jsonResp = (data: unknown, status = 200) =>
      new Response(JSON.stringify(data), {
        status,
        headers: { "Content-Type": "application/json" },
      });

    fetchMock
      .mockResolvedValueOnce(jsonResp(strategiesResp))
      .mockResolvedValueOnce(jsonResp(completed, 202))
      .mockResolvedValueOnce(jsonResp(completed));

    const bars: OhlcvBar[] = [
      { time: 1700000000, open: 5000, high: 5002, low: 4999, close: 5000, volume: 10, is_closed: true },
      { time: 1700000300, open: 5000, high: 5003, low: 4999, close: 5002, volume: 12, is_closed: true },
      { time: 1700000600, open: 5002, high: 5004, low: 5001, close: 5003, volume: 15, is_closed: true },
    ];
    useReplayStore.getState().registerPaneBars("pane-1", bars);
    useBacktestStore.getState().togglePanel();
    useReplayStore.getState().togglePanel();

    const target = {
      paneId: "pane-1",
      contractId: "CON.F.US.MES.H26",
      rootSymbol: "MES" as const,
      timeframe: "5m" as const,
      liveMode: false,
    };

    render(
      <div>
        <ReplayControlBar activeTarget={target} />
        <BacktestPanel activeTarget={target} />
      </div>,
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    // Submit backtest
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Run Backtest" }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText("Net Profit")).toBeTruthy();
    expect(screen.getAllByText("-$10.00")).toHaveLength(2);
    expect(screen.getByText("AMBIGUOUS")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Zones" }));
    expect(useBacktestStore.getState().showZones).toBe(false);

    // Start replay, open paper trade, step forward, flatten
    fireEvent.click(screen.getByRole("button", { name: "Start Replay" }));
    expect(useReplayStore.getState().session).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Buy Market" }));
    expect(useReplayStore.getState().session?.openPosition?.direction).toBe("LONG");
    fireEvent.click(screen.getByRole("button", { name: "Step Forward" }));
    fireEvent.click(screen.getByRole("button", { name: "Flatten" }));
    expect(useReplayStore.getState().session?.closedTrades).toHaveLength(1);
    expect(screen.getByText("Trades (1)")).toBeTruthy();
  });

  it("triggers authErrorHandler on 401/403 from backtest API", async () => {
    /**
     * Contract: 401/403 responses during backtest submission invoke registered authErrorHandler.
     * Defect caught: Swallowed session-expiry error leaving stale authenticated backtest UI open.
     */
    const authSpy = vi.fn();
    useBacktestStore.getState().setAuthErrorHandler(authSpy);
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Authentication required" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await act(async () => {
      await useBacktestStore.getState().runBacktest({
        contract_id: "CON.F.US.MES.H26",
        root_symbol: "MES",
        timeframe: "5m",
        live_mode: false,
        start_time: 1700000000,
        end_time: 1700002700,
      });
    });

    expect(authSpy).toHaveBeenCalledWith(401, expect.stringContaining("Authentication required"));
    expect(ApiError).toBeDefined();
  });

  it("halts strategy retry storm on HTTP 500 and recovers via manual Retry (P1-001)", async () => {
    /**
     * Contract: When GET /api/backtest/strategies fails with HTTP 500, BacktestPanel calls
     * fetch once (no 0ms infinite loop), sets strategiesAttempted=true, and recovers on Retry click.
     * Defect caught: Unbounded useEffect retry storm when strategies endpoint returns 500/503 or [].
     */
    const strategiesResp: BacktestStrategiesResponse = {
      strategies: [
        {
          strategy_id: "ict_fvg_sweep",
          strategy_version: "1.0.0",
          name: "ICT FVG + Liquidity Sweep",
          description: "ICT sample",
          default_params: {},
          param_schema: {},
        },
      ],
      count: 1,
    };
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Backtest unavailable" }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(strategiesResp), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );

    useBacktestStore.getState().togglePanel();
    render(
      <BacktestPanel
        activeTarget={{
          paneId: "pane-1",
          contractId: "CON.F.US.MES.H26",
          rootSymbol: "MES",
          timeframe: "5m",
          liveMode: false,
        }}
      />,
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(useBacktestStore.getState().strategiesAttempted).toBe(true);
    expect(screen.getByRole("button", { name: "Retry loading strategies" })).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry loading strategies" }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(useBacktestStore.getState().strategies).toHaveLength(1);
  });

  it("aborts in-flight submit on clearResult/selectJob and refreshes terminal state on cancel 409 (P1-002, P2-005)", async () => {
    /**
     * Contract: clearResult() and selectJob() abort in-flight POST /api/backtest and clear submitting,
     * and cancelActiveJob() refreshes GET /api/backtest/{id} on HTTP 409 Conflict to unlock UI.
     * Defect caught: Stranded submitting=true or wedged RUNNING state after 409 conflict.
     */
    const payload: BacktestSubmitRequest = {
      contract_id: "CON.F.US.MES.H26",
      root_symbol: "MES",
      timeframe: "5m",
      live_mode: false,
      start_time: 1700000000,
      end_time: 1700002700,
    };

    let capturedSignal: AbortSignal | undefined;
    let resolvePendingSubmit!: (resp: Response) => void;
    fetchMock.mockImplementationOnce(
      (_url, init) =>
        new Promise<Response>((resolve) => {
          capturedSignal = init?.signal ?? undefined;
          resolvePendingSubmit = resolve;
        }),
    );

    const p1 = useBacktestStore.getState().runBacktest(payload);
    expect(useBacktestStore.getState().submitting).toBe(true);
    useBacktestStore.getState().clearResult();
    expect(capturedSignal?.aborted).toBe(true);
    expect(useBacktestStore.getState().submitting).toBe(false);
    resolvePendingSubmit(
      new Response(JSON.stringify(makeJobSummary("late-job", "QUEUED")), { status: 202 }),
    );
    await p1;
    expect(useBacktestStore.getState().activeJob).toBeNull();

    // selectJob during pending submit clears submitting and loads selected job
    let resolveSecondSubmit!: (resp: Response) => void;
    const completedDetail: BacktestJobDetail = {
      ...makeJobSummary("selected-job", "COMPLETED"),
      result: makeSampleResult(),
    };
    fetchMock
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveSecondSubmit = resolve;
          }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(completedDetail), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );

    const p2 = useBacktestStore.getState().runBacktest(payload);
    expect(useBacktestStore.getState().submitting).toBe(true);
    await act(async () => {
      await useBacktestStore.getState().selectJob("selected-job");
    });
    expect(useBacktestStore.getState().submitting).toBe(false);
    expect(useBacktestStore.getState().activeJob?.job_id).toBe("selected-job");
    resolveSecondSubmit(
      new Response(JSON.stringify(makeJobSummary("late-2", "QUEUED")), { status: 202 }),
    );
    await p2;

    // cancelActiveJob on HTTP 409 refreshes terminal COMPLETED job
    const runningDetail: BacktestJobDetail = {
      ...makeJobSummary("race-job", "RUNNING"),
      result: null,
    };
    const finishedDetail: BacktestJobDetail = {
      ...makeJobSummary("race-job", "COMPLETED"),
      result: makeSampleResult(),
    };
    useBacktestStore.setState({ activeJob: runningDetail, polling: true });
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Cannot cancel COMPLETED job" }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(finishedDetail), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );

    await act(async () => {
      await useBacktestStore.getState().cancelActiveJob();
    });
    expect(useBacktestStore.getState().polling).toBe(false);
    expect(useBacktestStore.getState().submitting).toBe(false);
    expect(useBacktestStore.getState().activeJob?.status).toBe("COMPLETED");
  });
});
