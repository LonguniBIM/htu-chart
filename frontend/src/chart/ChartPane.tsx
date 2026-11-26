import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  createChart,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type IRange,
  type ISeriesApi,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { ApiError, fetchBars } from "../api/client";
import { scheduleRetry } from "../api/retryTimer";
import {
  ROOT_SYMBOLS,
  TIMEFRAMES,
  isLiveWsTimeframe,
  type BarsQuality,
  type OhlcvBar,
  type PaneConfig,
  type RootSymbol,
  type Timeframe,
} from "../api/types";
import type { MarketWsClient } from "../api/wsClient";
import { DrawingsOverlayPrimitive } from "../drawings/DrawingsOverlayPrimitive";
import {
  MIN_DRAWING_TIMESTAMP_SEC,
  TIMEFRAME_SECONDS,
  clampPositionAnchors,
  snapPriceToTick,
  snapToNearestOhlc,
  xToTime,
} from "../drawings/geometry";
import {
  INSTRUMENT_SPECS,
  type DrawingAnchor,
  type DrawingDraft,
  type DrawingRecord,
} from "../drawings/types";
import type { ChartPaneSyncHandle, LayoutSyncController } from "../layout/syncController";
import {
  buildDrawingScopeKey,
  useDrawingStore,
  type DrawingScopeTarget,
} from "../store/drawingStore";
import {
  PaneFetchCoordinator,
  appendBoundedWsBar,
  buildPaneTargetKey,
  matchesWsBarTarget,
  reconcileSnapshotWithBufferedBars,
  type PaneFetchTarget,
} from "./fetchGuard";

export interface ChartPaneProps {
  pane: PaneConfig;
  contractId: string | null;
  activeFeedLive: boolean | null;
  wsClient: MarketWsClient;
  coordinator: PaneFetchCoordinator;
  syncController?: LayoutSyncController | undefined;
  isActive?: boolean | undefined;
  onSelectPane?: ((paneId: string) => void) | undefined;
  gridStyle?: CSSProperties | undefined;
  onSymbolChange?: ((paneId: string, symbol: RootSymbol) => void) | undefined;
  onTimeframeChange?: ((paneId: string, timeframe: Timeframe) => void) | undefined;
  onAuthError?: ((status: number, message: string) => void) | undefined;
}

const SNAPSHOT_RETRY_BACKOFF_MS = [1500, 3000, 6000] as const;

function toCandleDatum(bar: OhlcvBar): CandlestickData<UTCTimestamp> {
  return {
    time: bar.time as UTCTimestamp,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
  };
}

function toVolumeDatum(bar: OhlcvBar): HistogramData<UTCTimestamp> {
  return {
    time: bar.time as UTCTimestamp,
    value: bar.volume,
    color: bar.close >= bar.open ? "rgba(38, 166, 154, 0.45)" : "rgba(239, 83, 80, 0.45)",
  };
}

function formatLegendText(
  symbol: RootSymbol,
  timeframe: Timeframe,
  bar: { open: number; high: number; low: number; close: number; volume?: number } | null,
): string {
  if (bar === null) {
    return `${symbol} · ${timeframe} · --`;
  }
  const volText = bar.volume !== undefined ? ` V ${bar.volume}` : "";
  return (
    `${symbol} · ${timeframe}  ` +
    `O ${bar.open.toFixed(2)}  H ${bar.high.toFixed(2)}  ` +
    `L ${bar.low.toFixed(2)}  C ${bar.close.toFixed(2)}${volText}`
  );
}

function formatQualityWarning(quality: BarsQuality | null): string | null {
  if (quality === null) {
    return null;
  }
  const flags: string[] = [];
  if (quality.truncated) {
    flags.push("truncated");
  }
  if (quality.provider_gap) {
    flags.push("provider gap");
  }
  if (quality.quarantined_count > 0) {
    flags.push(`${quality.quarantined_count} quarantined`);
  }
  if (flags.length === 0 && !quality.is_complete) {
    flags.push("incomplete");
  }
  return flags.length > 0 ? `Warning: ${flags.join(", ")}` : null;
}

function resolveNearestClosePrice(
  bars: readonly OhlcvBar[],
  closeByTime: ReadonlyMap<number, number>,
  timeSec: number,
  fallbackClose: number | undefined,
): number | undefined {
  const exact = closeByTime.get(timeSec);
  if (exact !== undefined) {
    return exact;
  }
  if (bars.length === 0) {
    return fallbackClose;
  }
  let lo = 0;
  let hi = bars.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid]!.time < timeSec) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  const rightBar = bars[lo];
  const leftBar = lo > 0 ? bars[lo - 1] : undefined;
  if (leftBar === undefined) {
    return rightBar?.close ?? fallbackClose;
  }
  if (rightBar === undefined) {
    return leftBar.close;
  }
  return Math.abs(leftBar.time - timeSec) <= Math.abs(rightBar.time - timeSec)
    ? leftBar.close
    : rightBar.close;
}

function computeFallbackHalfSpan(bars: readonly OhlcvBar[]): number {
  if (bars.length >= 2) {
    const span = bars[bars.length - 1]!.time - bars[0]!.time;
    if (Number.isFinite(span) && span > 0) {
      return span / 2;
    }
  }
  return 300;
}

function anchorsEqual(a: readonly DrawingAnchor[], b: readonly DrawingAnchor[]): boolean {
  if (a.length !== b.length) {
    return false;
  }
  for (let i = 0; i < a.length; i += 1) {
    const left = a[i]!;
    const right = b[i]!;
    if (left.role !== right.role || left.time !== right.time || left.price !== right.price) {
      return false;
    }
  }
  return true;
}

export function ChartPane({
  pane,
  contractId,
  activeFeedLive,
  wsClient,
  coordinator,
  syncController,
  isActive = false,
  onSelectPane,
  gridStyle,
  onSymbolChange,
  onTimeframeChange,
  onAuthError,
}: ChartPaneProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const legendRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const barsRef = useRef<OhlcvBar[]>([]);
  const barCloseByTimeRef = useRef<Map<number, number>>(new Map());
  const latestBarRef = useRef<OhlcvBar | null>(null);
  const lastBarTimeRef = useRef<number | null>(null);
  const activeGenerationRef = useRef<number>(0);
  const snapshotReadyGenRef = useRef<number>(0);
  const pendingWsBarsRef = useRef<OhlcvBar[]>([]);
  const bufferOverflowRef = useRef<boolean>(false);
  const lastTargetKeyRef = useRef<string>("");
  const hasLoadedTargetKeyRef = useRef<string>("");
  const snapshotRetryCountRef = useRef<number>(0);
  const retryCancelRef = useRef<(() => void) | null>(null);
  const suppressRangeEchoRef = useRef(false);
  const suppressRangeRafRef = useRef<number | null>(null);
  const primitiveRef = useRef<DrawingsOverlayPrimitive | null>(null);
  const suppressTimeClickRef = useRef(false);
  const draftRef = useRef<DrawingDraft | null>(null);
  const placementStartPxRef = useRef<{ px: number; py: number } | null>(null);
  const draftGestureStartedInPlotRef = useRef(false);
  const draftPlacementPointerIdRef = useRef<number | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    drawingId: string;
    scope: DrawingScopeTarget;
    scopeKey: string;
    startMutationEpoch: number;
    originalDrawing: DrawingRecord;
    anchorRole: string | null;
    startPx: number;
    startPy: number;
    startTime: number;
    startPrice: number;
    currentAnchors: DrawingAnchor[];
    hasMoved: boolean;
  } | null>(null);
  const [draftState, setDraftState] = useState<DrawingDraft | null>(null);

  const cancelPendingRangeSuppressionRaf = useCallback(() => {
    if (suppressRangeRafRef.current !== null) {
      if (typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(suppressRangeRafRef.current);
      }
      suppressRangeRafRef.current = null;
    }
  }, []);

  const runProgrammaticVisibleRange = useCallback(
    (apply: () => void) => {
      cancelPendingRangeSuppressionRaf();
      suppressRangeEchoRef.current = true;
      try {
        apply();
      } catch {
        suppressRangeEchoRef.current = false;
        return;
      }
      if (suppressRangeEchoRef.current) {
        if (typeof requestAnimationFrame === "function") {
          suppressRangeRafRef.current = requestAnimationFrame(() => {
            suppressRangeRafRef.current = null;
            suppressRangeEchoRef.current = false;
          });
        } else {
          suppressRangeEchoRef.current = false;
        }
      }
    },
    [cancelPendingRangeSuppressionRaf],
  );

  const [loadStatus, setLoadStatus] = useState<string>("idle");
  const [barsQuality, setBarsQuality] = useState<BarsQuality | null>(null);
  const [resyncNonce, setResyncNonce] = useState<number>(0);

  const currentTarget: PaneFetchTarget = {
    pane_id: pane.pane_id,
    contract_id: contractId ?? "",
    timeframe: pane.timeframe,
    live_mode: pane.live_mode,
    playback_mode: pane.playback_mode,
  };
  const currentTargetRef = useRef<PaneFetchTarget>(currentTarget);
  currentTargetRef.current = currentTarget;

  const symbolRef = useRef<RootSymbol>(pane.root_symbol);
  symbolRef.current = pane.root_symbol;
  const timeframeRef = useRef<Timeframe>(pane.timeframe);
  timeframeRef.current = pane.timeframe;
  const paneIdRef = useRef<string>(pane.pane_id);
  paneIdRef.current = pane.pane_id;
  const syncControllerRef = useRef<LayoutSyncController | undefined>(syncController);
  syncControllerRef.current = syncController;
  const onAuthErrorRef = useRef(onAuthError);
  onAuthErrorRef.current = onAuthError;

  const resolvedContractIdForScope = contractId?.trim() ? contractId.trim().toUpperCase() : "";
  const drawingScope: DrawingScopeTarget | null = resolvedContractIdForScope
    ? {
        symbol: pane.root_symbol,
        contractId: resolvedContractIdForScope,
        paneId: pane.pane_id,
      }
    : null;
  const drawingScopeKey =
    drawingScope !== null
      ? buildDrawingScopeKey(drawingScope.symbol, drawingScope.contractId, drawingScope.paneId)
      : "";

  const activeTool = useDrawingStore((s) => s.activeTool);
  const scopeDrawingState = useDrawingStore((s) =>
    drawingScopeKey ? s.scopes[drawingScopeKey] : undefined,
  );
  const selectedDrawingId = useDrawingStore((s) =>
    drawingScopeKey ? (s.selectedByScope[drawingScopeKey] ?? null) : null,
  );
  const persistedDrawings = useMemo(
    () => scopeDrawingState?.drawings ?? [],
    [scopeDrawingState?.drawings],
  );

  const drawingScopeRef = useRef<DrawingScopeTarget | null>(drawingScope);
  drawingScopeRef.current = drawingScope;
  const drawingScopeKeyRef = useRef<string>(drawingScopeKey);
  drawingScopeKeyRef.current = drawingScopeKey;
  const persistedDrawingsRef = useRef<readonly DrawingRecord[]>(persistedDrawings);
  persistedDrawingsRef.current = persistedDrawings;
  const selectedDrawingIdRef = useRef<string | null>(selectedDrawingId);
  selectedDrawingIdRef.current = selectedDrawingId;

  const syncOverlayPrimitive = useCallback((nextDraft?: DrawingDraft | null) => {
    const primitive = primitiveRef.current;
    if (primitive === null) {
      return;
    }
    const activeDrag = dragRef.current;
    const overrideDrawing =
      activeDrag !== null
        ? {
            ...activeDrag.originalDrawing,
            coordinates: { anchors: activeDrag.currentAnchors },
          }
        : null;
    const scope = drawingScopeRef.current;
    const storeState = useDrawingStore.getState();
    const scopeKey =
      scope !== null
        ? buildDrawingScopeKey(scope.symbol, scope.contractId, scope.paneId)
        : "";
    const drawings = scopeKey
      ? (storeState.scopes[scopeKey]?.drawings ?? persistedDrawingsRef.current)
      : persistedDrawingsRef.current;
    const selectedId = scopeKey
      ? (storeState.selectedByScope[scopeKey] ?? null)
      : selectedDrawingIdRef.current;

    primitive.setData({
      drawings,
      selectedDrawingId: selectedId,
      draft: nextDraft !== undefined ? nextDraft : draftRef.current,
      overrideDrawing,
      bars: barsRef.current,
      timeframeSec: TIMEFRAME_SECONDS[timeframeRef.current],
      instrumentSpec: INSTRUMENT_SPECS[symbolRef.current],
    });
  }, []);

  const applyLiveBarUpdate = useCallback((bar: OhlcvBar) => {
    if (lastBarTimeRef.current !== null && bar.time < lastBarTimeRef.current) {
      return;
    }
    if (
      lastBarTimeRef.current !== null &&
      bar.time === lastBarTimeRef.current &&
      latestBarRef.current?.is_closed === true &&
      !bar.is_closed
    ) {
      return;
    }
    candleSeriesRef.current?.update(toCandleDatum(bar));
    volumeSeriesRef.current?.update(toVolumeDatum(bar));
    barCloseByTimeRef.current.set(bar.time, bar.close);
    const existingBars = barsRef.current;
    if (
      existingBars.length > 0 &&
      existingBars[existingBars.length - 1]!.time === bar.time
    ) {
      existingBars[existingBars.length - 1] = bar;
    } else {
      existingBars.push(bar);
    }
    latestBarRef.current = bar;
    lastBarTimeRef.current = bar.time;
    syncOverlayPrimitive();
    if (legendRef.current !== null) {
      legendRef.current.textContent = formatLegendText(
        symbolRef.current,
        timeframeRef.current,
        bar,
      );
    }
  }, [syncOverlayPrimitive]);

  useEffect(() => {
    const container = containerRef.current;
    if (container === null) {
      return;
    }

    const chart = createChart(container, {
      width: Math.max(container.clientWidth, 240),
      height: Math.max(container.clientHeight, 200),
      layout: {
        background: { type: ColorType.Solid, color: "#0d1117" },
        textColor: "#b9c4d4",
      },
      grid: {
        vertLines: { color: "rgba(148, 163, 184, 0.10)" },
        horzLines: { color: "rgba(148, 163, 184, 0.10)" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
      rightPriceScale: {
        borderColor: "rgba(148, 163, 184, 0.22)",
      },
      timeScale: {
        borderColor: "rgba(148, 163, 184, 0.22)",
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: false,
      wickUpColor: "#26a69a",
      wickDownColor: "#ef5350",
    });

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;

    const getPlotBounds = (rectWidth: number, rectHeight: number) => {
      const tsApi = chart.timeScale();
      const tsWidth = typeof tsApi.width === "function" ? tsApi.width() : rectWidth;
      const plotWidth =
        Number.isFinite(tsWidth) && tsWidth > 0 && tsWidth <= rectWidth ? tsWidth : rectWidth;
      const tsHeight = typeof tsApi.height === "function" ? tsApi.height() : 0;
      const plotHeight =
        Number.isFinite(tsHeight) && tsHeight > 0 && tsHeight < rectHeight
          ? rectHeight - tsHeight
          : rectHeight;
      return { plotWidth, plotHeight };
    };

    const initialBounds = getPlotBounds(
      Math.max(container.clientWidth, 240),
      Math.max(container.clientHeight, 200),
    );
    const primitive = new DrawingsOverlayPrimitive();
    primitive.setViewportSize(initialBounds.plotWidth, initialBounds.plotHeight);
    primitiveRef.current = primitive;
    if (typeof candleSeries.attachPrimitive === "function") {
      candleSeries.attachPrimitive(primitive);
    }

    const handleCrosshair = (param: MouseEventParams<Time>) => {
      const legendEl = legendRef.current;
      if (legendEl !== null) {
        if (param.time === undefined) {
          legendEl.textContent = formatLegendText(
            symbolRef.current,
            timeframeRef.current,
            latestBarRef.current,
          );
        } else {
          const candlePoint = param.seriesData.get(candleSeries);
          if (candlePoint !== undefined && "open" in candlePoint) {
            const volPoint = param.seriesData.get(volumeSeries);
            const volValue =
              volPoint !== undefined && "value" in volPoint ? volPoint.value : undefined;
            legendEl.textContent = formatLegendText(symbolRef.current, timeframeRef.current, {
              open: candlePoint.open,
              high: candlePoint.high,
              low: candlePoint.low,
              close: candlePoint.close,
              ...(volValue !== undefined ? { volume: volValue } : {}),
            });
          }
        }
      }

      const syncTime = typeof param.time === "number" ? (param.time as UTCTimestamp) : null;
      syncControllerRef.current?.onCrosshairMove(paneIdRef.current, syncTime);
    };
    chart.subscribeCrosshairMove(handleCrosshair);

    const handleClick = (param: MouseEventParams<Time>) => {
      if (
        suppressTimeClickRef.current ||
        useDrawingStore.getState().activeTool !== "cursor" ||
        dragRef.current !== null ||
        draftRef.current !== null
      ) {
        suppressTimeClickRef.current = false;
        return;
      }
      if (typeof param.time === "number") {
        syncControllerRef.current?.onTimeClick(paneIdRef.current, param.time as UTCTimestamp);
      }
    };
    chart.subscribeClick(handleClick);

    const resolveChartPoint = (
      clientX: number,
      clientY: number,
      clampToPlot = false,
    ): { px: number; py: number; time: number; price: number; plotWidth: number; plotHeight: number } | null => {
      const rect = container.getBoundingClientRect();
      const rawPx = clientX - rect.left;
      const rawPy = clientY - rect.top;
      if (!Number.isFinite(rawPx) || !Number.isFinite(rawPy)) {
        return null;
      }
      const { plotWidth, plotHeight } = getPlotBounds(rect.width, rect.height);
      const px = clampToPlot ? Math.min(Math.max(rawPx, 0), Math.max(0, plotWidth - 1)) : rawPx;
      const py = clampToPlot ? Math.min(Math.max(rawPy, 0), Math.max(0, plotHeight - 1)) : rawPy;
      if (typeof candleSeries.coordinateToPrice !== "function") {
        return null;
      }
      const rawPrice = candleSeries.coordinateToPrice(py);
      if (rawPrice === null || !Number.isFinite(rawPrice) || rawPrice <= 0) {
        return null;
      }
      const tfSec = TIMEFRAME_SECONDS[timeframeRef.current];
      const rawTime = xToTime(px, chart.timeScale(), barsRef.current, tfSec);
      if (rawTime === null) {
        return null;
      }
      const tickSize = INSTRUMENT_SPECS[symbolRef.current].tickSize;
      const snapped = useDrawingStore.getState().magnetEnabled
        ? snapToNearestOhlc(rawTime, Number(rawPrice), barsRef.current, tickSize)
        : {
            time: Math.max(MIN_DRAWING_TIMESTAMP_SEC, Math.round(rawTime)),
            price: snapPriceToTick(Number(rawPrice), tickSize),
          };
      return {
        px,
        py,
        time: Math.max(MIN_DRAWING_TIMESTAMP_SEC, Math.round(snapped.time)),
        price: Math.max(tickSize, snapped.price),
        plotWidth,
        plotHeight,
      };
    };

    const finalizeTwoPointTool = (
      scope: DrawingScopeTarget,
      draft: DrawingDraft,
      endPoint: { time: number; price: number },
    ) => {
      const start = draft.anchors[0]!;
      let endTime = endPoint.time;
      const endPrice = endPoint.price;
      const tfSec = TIMEFRAME_SECONDS[timeframeRef.current];
      if (draft.tool === "ray" && start.time === endTime && start.price === endPrice) {
        endTime = start.time + Math.max(1, tfSec);
      }
      const metadata: Record<string, unknown> = {};
      if (draft.tool === "fvg_box") {
        const resolvedDirection =
          draft.fvgPreset === "bullish" || draft.fvgPreset === "bearish"
            ? draft.fvgPreset
            : endPrice >= start.price
              ? "bullish"
              : "bearish";
        metadata.direction = resolvedDirection;
      }
      if (draft.tool === "order_block") {
        metadata.extend_right = draft.extendRight ?? true;
        metadata.label = "OB";
      }
      draftRef.current = null;
      placementStartPxRef.current = null;
      draftGestureStartedInPlotRef.current = false;
      draftPlacementPointerIdRef.current = null;
      setDraftState(null);
      useDrawingStore.getState().setActiveTool("cursor");
      void useDrawingStore.getState().createDrawing(scope, {
        type: draft.tool,
        coordinates: {
          anchors: [
            { role: "start", time: start.time, price: start.price },
            { role: "end", time: endTime, price: endPrice },
          ],
        },
        ...(Object.keys(metadata).length > 0 ? { metadata } : {}),
      });
    };

    const restoreChartInteraction = () => {
      chart.applyOptions({ handleScroll: true, handleScale: true });
    };

    const releasePointerSafely = (pointerId: number) => {
      try {
        if (typeof container.releasePointerCapture === "function") {
          container.releasePointerCapture(pointerId);
        }
      } catch {
        // Ignore jsdom / synthetic pointer events without active capture
      }
    };

    const handlePointerDown = (event: PointerEvent) => {
      if (event.pointerType === "mouse" && event.button !== 0) {
        return;
      }
      if (dragRef.current !== null || draftPlacementPointerIdRef.current !== null) {
        return;
      }
      const scope = drawingScopeRef.current;
      if (scope === null) {
        return;
      }
      const rect = container.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      const { plotWidth, plotHeight } = getPlotBounds(rect.width, rect.height);
      if (
        !Number.isFinite(px) ||
        !Number.isFinite(py) ||
        px < 0 ||
        py < 0 ||
        px >= plotWidth ||
        py >= plotHeight
      ) {
        return;
      }
      primitive.setViewportSize(plotWidth, plotHeight);

      const pt = resolveChartPoint(event.clientX, event.clientY);
      const storeState = useDrawingStore.getState();
      const tool = storeState.activeTool;
      const scopeKey = buildDrawingScopeKey(scope.symbol, scope.contractId, scope.paneId);
      const liveDrawings = storeState.scopes[scopeKey]?.drawings ?? persistedDrawingsRef.current;
      const liveSelectedId = storeState.selectedByScope[scopeKey] ?? null;

      if (tool === "cursor") {
        const hit = primitive.hitTestAt(px, py);
        if (hit === null) {
          suppressTimeClickRef.current = false;
          if (liveSelectedId !== null) {
            storeState.setSelectedDrawing(scope, null);
          }
          return;
        }
        suppressTimeClickRef.current = true;
        storeState.setSelectedDrawing(scope, hit.drawingId);
        const targetDrawing = liveDrawings.find((d) => d.id === hit.drawingId);
        if (targetDrawing === undefined || pt === null) {
          return;
        }
        dragRef.current = {
          pointerId: event.pointerId,
          drawingId: targetDrawing.id,
          scope,
          scopeKey,
          startMutationEpoch: storeState.scopes[scopeKey]?.mutationEpoch ?? 0,
          originalDrawing: targetDrawing,
          anchorRole: hit.anchorRole,
          startPx: px,
          startPy: py,
          startTime: pt.time,
          startPrice: pt.price,
          currentAnchors: targetDrawing.coordinates.anchors.map((a) => ({ ...a })),
          hasMoved: false,
        };
        chart.applyOptions({ handleScroll: false, handleScale: false });
        try {
          if (typeof container.setPointerCapture === "function") {
            container.setPointerCapture(event.pointerId);
          }
        } catch {
          // Ignore jsdom / synthetic pointer events without active capture
        }
        return;
      }

      if (pt === null) {
        return;
      }
      suppressTimeClickRef.current = true;
      const tickSize = INSTRUMENT_SPECS[symbolRef.current].tickSize;
      const tfSec = TIMEFRAME_SECONDS[timeframeRef.current];

      if (tool === "hline") {
        storeState.setActiveTool("cursor");
        void storeState.createDrawing(scope, {
          type: "hline",
          coordinates: {
            anchors: [{ role: "level", time: pt.time, price: pt.price }],
          },
        });
        return;
      }

      if (tool === "long_position" || tool === "short_position") {
        const widthTime = Math.max(tfSec * 5, 60);
        const rawAnchors: DrawingAnchor[] =
          tool === "long_position"
            ? [
                { role: "entry", time: pt.time, price: pt.price },
                { role: "target", time: pt.time + widthTime, price: pt.price + tickSize * 20 },
                {
                  role: "stop",
                  time: pt.time + widthTime,
                  price: Math.max(tickSize, pt.price - tickSize * 10),
                },
              ]
            : [
                { role: "entry", time: pt.time, price: pt.price },
                {
                  role: "target",
                  time: pt.time + widthTime,
                  price: Math.max(tickSize, pt.price - tickSize * 20),
                },
                { role: "stop", time: pt.time + widthTime, price: pt.price + tickSize * 10 },
              ];
        const clampedAnchors = clampPositionAnchors(tool, rawAnchors, "entry", tickSize);
        storeState.setActiveTool("cursor");
        void storeState.createDrawing(scope, {
          type: tool,
          coordinates: { anchors: clampedAnchors },
        });
        return;
      }

      if (draftRef.current === null) {
        const nextDraft: DrawingDraft = {
          tool,
          anchors: [
            { role: "start", time: pt.time, price: pt.price },
            { role: "end", time: pt.time, price: pt.price },
          ],
          ...(tool === "fvg_box" ? { fvgPreset: storeState.fvgPreset } : {}),
          ...(tool === "order_block" ? { extendRight: storeState.obExtendRight } : {}),
        };
        draftRef.current = nextDraft;
        placementStartPxRef.current = { px: pt.px, py: pt.py };
        draftGestureStartedInPlotRef.current = true;
        draftPlacementPointerIdRef.current = event.pointerId;
        setDraftState(nextDraft);
        syncOverlayPrimitive(nextDraft);
        chart.applyOptions({ handleScroll: false, handleScale: false });
        try {
          if (typeof container.setPointerCapture === "function") {
            container.setPointerCapture(event.pointerId);
          }
        } catch {
          // Ignore jsdom / synthetic pointer events without active capture
        }
      } else {
        finalizeTwoPointTool(scope, draftRef.current, pt);
      }
    };

    const handlePointerMove = (event: PointerEvent) => {
      const activeDrag = dragRef.current;
      if (activeDrag !== null) {
        if (event.pointerId !== activeDrag.pointerId) {
          return;
        }
        if (activeDrag.scopeKey !== drawingScopeKeyRef.current) {
          dragRef.current = null;
          restoreChartInteraction();
          syncOverlayPrimitive(null);
          return;
        }
        const rect = container.getBoundingClientRect();
        const rawPx = event.clientX - rect.left;
        const rawPy = event.clientY - rect.top;
        if (!activeDrag.hasMoved && Math.hypot(rawPx - activeDrag.startPx, rawPy - activeDrag.startPy) < 5) {
          return;
        }
        const pt = resolveChartPoint(event.clientX, event.clientY, true);
        if (pt === null) {
          return;
        }
        const tickSize = INSTRUMENT_SPECS[symbolRef.current].tickSize;
        const tfSec = TIMEFRAME_SECONDS[timeframeRef.current];
        let nextAnchors: DrawingAnchor[];

        if (activeDrag.anchorRole !== null) {
          nextAnchors = activeDrag.originalDrawing.coordinates.anchors.map((a) =>
            a.role === activeDrag.anchorRole
              ? { ...a, time: pt.time, price: pt.price }
              : { ...a },
          );
        } else {
          const deltaTime = pt.time - activeDrag.startTime;
          const deltaPrice = pt.price - activeDrag.startPrice;
          nextAnchors = activeDrag.originalDrawing.coordinates.anchors.map((a) => ({
            ...a,
            time: Math.max(MIN_DRAWING_TIMESTAMP_SEC, Math.round(a.time + deltaTime)),
            price: Math.max(tickSize, snapPriceToTick(a.price + deltaPrice, tickSize)),
          }));
        }

        if (
          activeDrag.originalDrawing.type === "long_position" ||
          activeDrag.originalDrawing.type === "short_position"
        ) {
          const movedRole =
            activeDrag.anchorRole === "entry" ||
            activeDrag.anchorRole === "target" ||
            activeDrag.anchorRole === "stop"
              ? activeDrag.anchorRole
              : "body";
          nextAnchors = clampPositionAnchors(
            activeDrag.originalDrawing.type,
            nextAnchors,
            movedRole,
            tickSize,
          );
        } else if (activeDrag.originalDrawing.type === "ray" && nextAnchors.length >= 2) {
          const start = nextAnchors[0]!;
          const end = nextAnchors[1]!;
          if (start.time === end.time && start.price === end.price) {
            nextAnchors[1] = { ...end, time: start.time + Math.max(1, tfSec) };
          }
        }

        activeDrag.currentAnchors = nextAnchors;
        activeDrag.hasMoved = true;
        syncOverlayPrimitive();
        return;
      }

      if (draftRef.current !== null) {
        if (
          draftPlacementPointerIdRef.current !== null &&
          event.pointerId !== draftPlacementPointerIdRef.current
        ) {
          return;
        }
        const pt = resolveChartPoint(event.clientX, event.clientY, true);
        if (pt === null) {
          return;
        }
        const updatedDraft: DrawingDraft = {
          ...draftRef.current,
          anchors: [draftRef.current.anchors[0]!, { role: "end", time: pt.time, price: pt.price }],
        };
        draftRef.current = updatedDraft;
        setDraftState(updatedDraft);
        syncOverlayPrimitive(updatedDraft);
      }
    };

    const handlePointerUp = (event: PointerEvent) => {
      const finishedDrag = dragRef.current;
      if (finishedDrag !== null) {
        if (event.pointerId !== finishedDrag.pointerId) {
          return;
        }
        dragRef.current = null;
        restoreChartInteraction();
        releasePointerSafely(event.pointerId);
        syncOverlayPrimitive();
        const currentState = useDrawingStore.getState();
        const scopeState = currentState.scopes[finishedDrag.scopeKey];
        const currentDrawings = scopeState?.drawings ?? [];
        const liveDrawing = currentDrawings.find((d) => d.id === finishedDrag.drawingId);
        const stillValid =
          currentState.activeTool === "cursor" &&
          currentState.selectedByScope[finishedDrag.scopeKey] === finishedDrag.drawingId &&
          (scopeState?.mutationEpoch ?? 0) === finishedDrag.startMutationEpoch &&
          liveDrawing !== undefined &&
          liveDrawing.revision === finishedDrag.originalDrawing.revision;
        if (
          stillValid &&
          finishedDrag.hasMoved &&
          finishedDrag.scopeKey === drawingScopeKeyRef.current &&
          !anchorsEqual(
            finishedDrag.originalDrawing.coordinates.anchors,
            finishedDrag.currentAnchors,
          )
        ) {
          void currentState.updateDrawing(
            finishedDrag.scope,
            finishedDrag.drawingId,
            { coordinates: { anchors: finishedDrag.currentAnchors } },
            { expectedDrawingRevision: finishedDrag.originalDrawing.revision },
          );
        }
        return;
      }

      if (
        draftRef.current !== null &&
        draftPlacementPointerIdRef.current !== null &&
        event.pointerId !== draftPlacementPointerIdRef.current
      ) {
        return;
      }

      const wasDraftGesture = draftGestureStartedInPlotRef.current;
      draftGestureStartedInPlotRef.current = false;
      draftPlacementPointerIdRef.current = null;
      restoreChartInteraction();
      releasePointerSafely(event.pointerId);

      if (draftRef.current !== null && placementStartPxRef.current !== null && wasDraftGesture) {
        const rect = container.getBoundingClientRect();
        const px = event.clientX - rect.left;
        const py = event.clientY - rect.top;
        const dist = Math.hypot(
          px - placementStartPxRef.current.px,
          py - placementStartPxRef.current.py,
        );
        if (dist >= 5 && drawingScopeRef.current !== null) {
          const pt = resolveChartPoint(event.clientX, event.clientY, true);
          if (pt !== null) {
            finalizeTwoPointTool(drawingScopeRef.current, draftRef.current, pt);
          }
        }
      }
    };

    const handlePointerCancel = (event: PointerEvent) => {
      if (dragRef.current !== null && event.pointerId !== dragRef.current.pointerId) {
        return;
      }
      if (
        dragRef.current === null &&
        draftRef.current !== null &&
        draftPlacementPointerIdRef.current !== null &&
        event.pointerId !== draftPlacementPointerIdRef.current
      ) {
        return;
      }
      restoreChartInteraction();
      releasePointerSafely(event.pointerId);
      dragRef.current = null;
      draftRef.current = null;
      placementStartPxRef.current = null;
      draftGestureStartedInPlotRef.current = false;
      draftPlacementPointerIdRef.current = null;
      suppressTimeClickRef.current = false;
      setDraftState(null);
      syncOverlayPrimitive(null);
    };

    container.addEventListener("pointerdown", handlePointerDown);
    container.addEventListener("pointermove", handlePointerMove);
    container.addEventListener("pointerup", handlePointerUp);
    container.addEventListener("pointercancel", handlePointerCancel);

    const unsubscribeDrawingStore = useDrawingStore.subscribe((state) => {
      const activeDrag = dragRef.current;
      if (activeDrag === null) {
        return;
      }
      const scopeState = state.scopes[activeDrag.scopeKey];
      const scopeDrawings = scopeState?.drawings ?? [];
      const liveDrawing = scopeDrawings.find((d) => d.id === activeDrag.drawingId);
      const stillSelected = state.selectedByScope[activeDrag.scopeKey] === activeDrag.drawingId;
      const sameMutationEpoch = (scopeState?.mutationEpoch ?? 0) === activeDrag.startMutationEpoch;
      const sameDrawingRevision =
        liveDrawing !== undefined && liveDrawing.revision === activeDrag.originalDrawing.revision;
      if (
        state.activeTool !== "cursor" ||
        !stillSelected ||
        !sameMutationEpoch ||
        !sameDrawingRevision
      ) {
        dragRef.current = null;
        restoreChartInteraction();
        syncOverlayPrimitive(null);
      }
    });

    const timeScale = chart.timeScale();
    const handleVisibleTimeRangeChange = (range: IRange<Time> | null) => {
      if (suppressRangeEchoRef.current) {
        suppressRangeEchoRef.current = false;
        cancelPendingRangeSuppressionRaf();
        return;
      }
      if (
        range !== null &&
        typeof range.from === "number" &&
        typeof range.to === "number"
      ) {
        syncControllerRef.current?.onVisibleTimeRangeChange(paneIdRef.current, {
          from: range.from as UTCTimestamp,
          to: range.to as UTCTimestamp,
        });
      }
    };
    timeScale.subscribeVisibleTimeRangeChange(handleVisibleTimeRangeChange);

    const resizeObserver = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry === undefined) {
        return;
      }
      const nextWidth = Math.floor(entry.contentRect.width);
      const nextHeight = Math.floor(entry.contentRect.height);
      if (nextWidth > 0 && nextHeight > 0) {
        chart.applyOptions({ width: nextWidth, height: nextHeight });
        const nextBounds = getPlotBounds(nextWidth, nextHeight);
        primitive.setViewportSize(nextBounds.plotWidth, nextBounds.plotHeight);
      }
    });
    resizeObserver.observe(container);

    return () => {
      unsubscribeDrawingStore();
      cancelPendingRangeSuppressionRaf();
      suppressRangeEchoRef.current = false;
      suppressTimeClickRef.current = false;
      dragRef.current = null;
      draftRef.current = null;
      placementStartPxRef.current = null;
      draftGestureStartedInPlotRef.current = false;
      draftPlacementPointerIdRef.current = null;
      restoreChartInteraction();
      container.removeEventListener("pointerdown", handlePointerDown);
      container.removeEventListener("pointermove", handlePointerMove);
      container.removeEventListener("pointerup", handlePointerUp);
      container.removeEventListener("pointercancel", handlePointerCancel);
      resizeObserver.disconnect();
      chart.unsubscribeCrosshairMove(handleCrosshair);
      chart.unsubscribeClick(handleClick);
      timeScale.unsubscribeVisibleTimeRangeChange(handleVisibleTimeRangeChange);
      if (typeof candleSeries.detachPrimitive === "function") {
        candleSeries.detachPrimitive(primitive);
      }
      primitiveRef.current = null;
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      barsRef.current = [];
      barCloseByTimeRef.current.clear();
      latestBarRef.current = null;
      lastBarTimeRef.current = null;
    };
  }, [cancelPendingRangeSuppressionRaf, syncOverlayPrimitive]);

  useEffect(() => {
    if (draftRef.current !== null && activeTool !== draftRef.current.tool) {
      draftRef.current = null;
      placementStartPxRef.current = null;
      draftGestureStartedInPlotRef.current = false;
      draftPlacementPointerIdRef.current = null;
      setDraftState(null);
      syncOverlayPrimitive(null);
      chartRef.current?.applyOptions({ handleScroll: true, handleScale: true });
    }
  }, [activeTool, syncOverlayPrimitive]);

  useEffect(() => {
    dragRef.current = null;
    draftRef.current = null;
    placementStartPxRef.current = null;
    draftGestureStartedInPlotRef.current = false;
    draftPlacementPointerIdRef.current = null;
    suppressTimeClickRef.current = false;
    setDraftState(null);
    syncOverlayPrimitive(null);
    chartRef.current?.applyOptions({ handleScroll: true, handleScale: true });
  }, [drawingScopeKey, pane.root_symbol, pane.pane_id, syncOverlayPrimitive]);

  useEffect(() => {
    syncOverlayPrimitive(draftState);
  }, [
    draftState,
    pane.root_symbol,
    pane.timeframe,
    persistedDrawings,
    selectedDrawingId,
    syncOverlayPrimitive,
  ]);

  useEffect(() => {
    if (!resolvedContractIdForScope) {
      return;
    }
    void useDrawingStore.getState().loadScope({
      symbol: pane.root_symbol,
      contractId: resolvedContractIdForScope,
      paneId: pane.pane_id,
    });
  }, [pane.pane_id, pane.root_symbol, resyncNonce, resolvedContractIdForScope]);

  useEffect(() => {
    if (!resolvedContractIdForScope || typeof wsClient.onConnectionState !== "function") {
      return;
    }
    return wsClient.onConnectionState((state) => {
      if (state === "connected") {
        void useDrawingStore.getState().loadScope({
          symbol: pane.root_symbol,
          contractId: resolvedContractIdForScope,
          paneId: pane.pane_id,
        });
      }
    });
  }, [pane.pane_id, pane.root_symbol, resolvedContractIdForScope, wsClient]);

  useEffect(() => {
    if (syncController === undefined) {
      return;
    }
    const handle: ChartPaneSyncHandle = {
      paneId: pane.pane_id,
      applyCrosshairTime: (time) => {
        const chart = chartRef.current;
        if (chart === null) {
          return;
        }
        if (time === null) {
          try {
            chart.clearCrosshairPosition();
          } catch {
            // Ignore chart disposal or uninitialized scale errors
          }
          return;
        }
        const series = candleSeriesRef.current;
        if (series === null || barsRef.current.length === 0) {
          return;
        }
        const price = resolveNearestClosePrice(
          barsRef.current,
          barCloseByTimeRef.current,
          Number(time),
          latestBarRef.current?.close,
        );
        if (price === undefined) {
          return;
        }
        try {
          chart.setCrosshairPosition(price, time, series);
        } catch {
          // Ignore when synced timestamp is outside current chart bounds
        }
      },
      applyVisibleTimeRange: (range) => {
        const chart = chartRef.current;
        if (chart === null) {
          return;
        }
        runProgrammaticVisibleRange(() => {
          chart.timeScale().setVisibleRange(range);
        });
      },
      centerOnTime: (time) => {
        const chart = chartRef.current;
        if (chart === null) {
          return;
        }
        let halfSpan = 0;
        try {
          const current = chart.timeScale().getVisibleRange();
          if (
            current !== null &&
            typeof current.from === "number" &&
            typeof current.to === "number"
          ) {
            const span = current.to - current.from;
            if (Number.isFinite(span) && span > 0) {
              halfSpan = span / 2;
            }
          }
        } catch {
          // Ignore unavailable visible range
        }
        if (halfSpan <= 0) {
          halfSpan = computeFallbackHalfSpan(barsRef.current);
        }
        const center = Number(time);
        runProgrammaticVisibleRange(() => {
          chart.timeScale().setVisibleRange({
            from: (center - halfSpan) as UTCTimestamp,
            to: (center + halfSpan) as UTCTimestamp,
          });
        });
      },
    };
    return syncController.registerPane(handle);
  }, [pane.pane_id, runProgrammaticVisibleRange, syncController]);

  useEffect(() => {
    const resolvedContractId = contractId?.trim() ?? "";
    const target: PaneFetchTarget = {
      pane_id: pane.pane_id,
      contract_id: resolvedContractId,
      timeframe: pane.timeframe,
      live_mode: pane.live_mode,
      playback_mode: pane.playback_mode,
    };
    const nextTargetKey = buildPaneTargetKey(target);
    const isNewTarget = nextTargetKey !== lastTargetKeyRef.current;
    if (isNewTarget) {
      lastTargetKeyRef.current = nextTargetKey;
      hasLoadedTargetKeyRef.current = "";
      snapshotRetryCountRef.current = 0;
      retryCancelRef.current?.();
      retryCancelRef.current = null;
      candleSeriesRef.current?.setData([]);
      volumeSeriesRef.current?.setData([]);
      barsRef.current = [];
      barCloseByTimeRef.current.clear();
      latestBarRef.current = null;
      lastBarTimeRef.current = null;
      setBarsQuality(null);
      if (legendRef.current !== null) {
        legendRef.current.textContent = formatLegendText(pane.root_symbol, pane.timeframe, null);
      }
    }

    if (!resolvedContractId) {
      hasLoadedTargetKeyRef.current = "";
      snapshotRetryCountRef.current = 0;
      retryCancelRef.current?.();
      retryCancelRef.current = null;
      coordinator.abortPane(pane.pane_id);
      activeGenerationRef.current += 1;
      pendingWsBarsRef.current = [];
      bufferOverflowRef.current = false;
      barsRef.current = [];
      barCloseByTimeRef.current.clear();
      setBarsQuality(null);
      if (legendRef.current !== null) {
        legendRef.current.textContent = formatLegendText(pane.root_symbol, pane.timeframe, null);
      }
      setLoadStatus("no-contract");
      return;
    }

    retryCancelRef.current?.();
    retryCancelRef.current = null;
    const ticket = coordinator.beginFetch(target);
    activeGenerationRef.current = ticket.generation;
    pendingWsBarsRef.current = [];
    bufferOverflowRef.current = false;
    setLoadStatus("loading");

    void fetchBars(
      {
        contract_id: resolvedContractId,
        timeframe: pane.timeframe,
        live: pane.live_mode,
        include_partial: true,
        root_symbol: pane.root_symbol,
      },
      ticket.signal,
    )
      .then((response) => {
        if (!coordinator.isResponseCurrent(ticket, currentTargetRef.current)) {
          return;
        }
        const mergedBars = reconcileSnapshotWithBufferedBars(
          response.bars,
          pendingWsBarsRef.current,
        );
        const candles = mergedBars.map(toCandleDatum);
        const volumes = mergedBars.map(toVolumeDatum);
        candleSeriesRef.current?.setData(candles);
        volumeSeriesRef.current?.setData(volumes);
        barsRef.current = mergedBars;
        syncOverlayPrimitive();
        const closeMap = new Map<number, number>();
        for (const bar of mergedBars) {
          closeMap.set(bar.time, bar.close);
        }
        barCloseByTimeRef.current = closeMap;
        snapshotReadyGenRef.current = ticket.generation;
        hasLoadedTargetKeyRef.current = nextTargetKey;
        snapshotRetryCountRef.current = 0;
        retryCancelRef.current?.();
        retryCancelRef.current = null;
        pendingWsBarsRef.current = [];
        const didOverflow = bufferOverflowRef.current;
        bufferOverflowRef.current = false;
        const lastBar = mergedBars[mergedBars.length - 1] ?? null;
        latestBarRef.current = lastBar;
        lastBarTimeRef.current = lastBar?.time ?? null;
        if (legendRef.current !== null) {
          legendRef.current.textContent = formatLegendText(
            pane.root_symbol,
            pane.timeframe,
            lastBar,
          );
        }
        chartRef.current?.timeScale().fitContent();
        setBarsQuality(response.quality ?? null);
        setLoadStatus(`${mergedBars.length} bars`);
        if (didOverflow) {
          setResyncNonce((n) => n + 1);
        }
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") {
          return;
        }
        if (!coordinator.isResponseCurrent(ticket, currentTargetRef.current)) {
          return;
        }
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          candleSeriesRef.current?.setData([]);
          volumeSeriesRef.current?.setData([]);
          pendingWsBarsRef.current = [];
          bufferOverflowRef.current = false;
          hasLoadedTargetKeyRef.current = "";
          barsRef.current = [];
          barCloseByTimeRef.current.clear();
          latestBarRef.current = null;
          lastBarTimeRef.current = null;
          if (legendRef.current !== null) {
            legendRef.current.textContent = formatLegendText(pane.root_symbol, pane.timeframe, null);
          }
          setBarsQuality(null);
          retryCancelRef.current?.();
          retryCancelRef.current = null;
          snapshotRetryCountRef.current = 0;
          setLoadStatus("auth error");
          onAuthErrorRef.current?.(err.status, err.message);
          return;
        }
        const message = err instanceof Error ? err.message : "Failed to load bars";
        if (hasLoadedTargetKeyRef.current === nextTargetKey) {
          const buffered = [...pendingWsBarsRef.current].sort((a, b) => a.time - b.time);
          for (const bar of buffered) {
            applyLiveBarUpdate(bar);
          }
          pendingWsBarsRef.current = [];
          bufferOverflowRef.current = false;
          snapshotReadyGenRef.current = ticket.generation;
          setLoadStatus(`resync error: ${message}`);
        } else {
          setLoadStatus(`error: ${message}`);
        }

        const attempt = snapshotRetryCountRef.current;
        if (attempt < SNAPSHOT_RETRY_BACKOFF_MS.length) {
          snapshotRetryCountRef.current = attempt + 1;
          const delayMs = SNAPSHOT_RETRY_BACKOFF_MS[attempt]!;
          retryCancelRef.current?.();
          retryCancelRef.current = scheduleRetry(() => {
            retryCancelRef.current = null;
            setResyncNonce((n) => n + 1);
          }, delayMs);
        }
      });

    return () => {
      retryCancelRef.current?.();
      retryCancelRef.current = null;
      coordinator.abortPane(pane.pane_id);
    };
  }, [
    applyLiveBarUpdate,
    contractId,
    coordinator,
    pane.live_mode,
    pane.pane_id,
    pane.playback_mode,
    pane.root_symbol,
    pane.timeframe,
    resyncNonce,
    syncOverlayPrimitive,
  ]);

  const isModeMismatch = activeFeedLive !== null && pane.live_mode !== activeFeedLive;
  const isLiveTimeframe = isLiveWsTimeframe(pane.timeframe);
  const canSubscribeWs =
    activeFeedLive !== null && pane.live_mode === activeFeedLive && isLiveTimeframe;

  useEffect(() => {
    const resolvedContractId = contractId?.trim() ?? "";
    if (!resolvedContractId) {
      return;
    }
    return wsClient.onResyncRequired((msg) => {
      if (
        msg.contract_id === null ||
        msg.contract_id.toUpperCase() === resolvedContractId.toUpperCase()
      ) {
        setResyncNonce((n) => n + 1);
      }
    });
  }, [contractId, wsClient]);

  useEffect(() => {
    const resolvedContractId = contractId?.trim() ?? "";
    if (!resolvedContractId || !canSubscribeWs) {
      return;
    }

    const unsubscribeWs = wsClient.subscribe(resolvedContractId, pane.timeframe);
    const removeBarListener = wsClient.onBarUpdate((msg) => {
      if (!matchesWsBarTarget(msg, currentTargetRef.current)) {
        return;
      }
      if (snapshotReadyGenRef.current !== activeGenerationRef.current) {
        const { overflowed } = appendBoundedWsBar(pendingWsBarsRef.current, msg.bar);
        if (overflowed) {
          bufferOverflowRef.current = true;
        }
        return;
      }
      applyLiveBarUpdate(msg.bar);
    });
    const removeSubscribedListener = wsClient.onSubscribed((msg) => {
      if (
        msg.contract_id.trim().toUpperCase() === resolvedContractId.toUpperCase() &&
        msg.timeframe === pane.timeframe
      ) {
        setResyncNonce((n) => n + 1);
      }
    });

    return () => {
      removeBarListener();
      removeSubscribedListener();
      unsubscribeWs();
    };
  }, [applyLiveBarUpdate, canSubscribeWs, contractId, pane.live_mode, pane.timeframe, wsClient]);

  const histOnlyLabel = !isLiveTimeframe
    ? `Hist only (${pane.timeframe} no live feed)`
    : isModeMismatch
      ? `Hist only (feed: ${activeFeedLive ? "Live" : "Sim"})`
      : null;
  const statusText = histOnlyLabel !== null ? `${histOnlyLabel} · ${loadStatus}` : loadStatus;
  const qualityWarning = formatQualityWarning(barsQuality);
  const showRetryButton =
    loadStatus.startsWith("error:") || loadStatus.startsWith("resync error:");

  return (
    <section
      className={isActive ? "chart-pane chart-pane--active" : "chart-pane"}
      data-pane-id={pane.pane_id}
      data-active={isActive ? "true" : "false"}
      style={gridStyle}
      onMouseDown={() => onSelectPane?.(pane.pane_id)}
    >
      <header className="chart-pane__header">
        <div className="chart-pane__controls">
          <select
            aria-label={`${pane.pane_id} symbol`}
            value={pane.root_symbol}
            onChange={(e) => onSymbolChange?.(pane.pane_id, e.target.value as RootSymbol)}
          >
            {ROOT_SYMBOLS.map((sym) => (
              <option key={sym} value={sym}>
                {sym}
              </option>
            ))}
          </select>
          <select
            aria-label={`${pane.pane_id} timeframe`}
            value={pane.timeframe}
            onChange={(e) => onTimeframeChange?.(pane.pane_id, e.target.value as Timeframe)}
          >
            {TIMEFRAMES.map((tf) => (
              <option key={tf} value={tf}>
                {tf}
              </option>
            ))}
          </select>
          <span className="chart-pane__contract">{contractId ?? "Unresolved"}</span>
        </div>
        <div className="chart-pane__controls">
          {qualityWarning !== null && (
            <span className="chart-pane__quality-warning" role="status">
              {qualityWarning}
            </span>
          )}
          <span className="chart-pane__status">{statusText}</span>
          {showRetryButton && (
            <button
              type="button"
              className="terminal-button"
              onClick={() => {
                snapshotRetryCountRef.current = 0;
                retryCancelRef.current?.();
                retryCancelRef.current = null;
                setResyncNonce((n) => n + 1);
              }}
            >
              Retry
            </button>
          )}
        </div>
      </header>
      <div className="chart-pane__canvas-wrap">
        <div ref={legendRef} className="chart-pane__legend">
          {formatLegendText(pane.root_symbol, pane.timeframe, null)}
        </div>
        <div ref={containerRef} className="chart-pane__canvas" />
      </div>
    </section>
  );
}
