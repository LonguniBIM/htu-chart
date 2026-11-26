import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
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
import type { ChartPaneSyncHandle, LayoutSyncController } from "../layout/syncController";
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
    if (legendRef.current !== null) {
      legendRef.current.textContent = formatLegendText(
        symbolRef.current,
        timeframeRef.current,
        bar,
      );
    }
  }, []);

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
      if (typeof param.time === "number") {
        syncControllerRef.current?.onTimeClick(paneIdRef.current, param.time as UTCTimestamp);
      }
    };
    chart.subscribeClick(handleClick);

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
      }
    });
    resizeObserver.observe(container);

    return () => {
      cancelPendingRangeSuppressionRaf();
      suppressRangeEchoRef.current = false;
      resizeObserver.disconnect();
      chart.unsubscribeCrosshairMove(handleCrosshair);
      chart.unsubscribeClick(handleClick);
      timeScale.unsubscribeVisibleTimeRangeChange(handleVisibleTimeRangeChange);
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      barsRef.current = [];
      barCloseByTimeRef.current.clear();
      latestBarRef.current = null;
      lastBarTimeRef.current = null;
    };
  }, [cancelPendingRangeSuppressionRaf]);

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
  ]);

  const isModeMismatch = activeFeedLive !== null && pane.live_mode !== activeFeedLive;
  const isLiveTimeframe = isLiveWsTimeframe(pane.timeframe);
  const canSubscribeWs =
    activeFeedLive !== null && pane.live_mode === activeFeedLive && isLiveTimeframe;

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
    const removeResyncListener = wsClient.onResyncRequired((msg) => {
      if (
        msg.contract_id === null ||
        msg.contract_id.toUpperCase() === resolvedContractId.toUpperCase()
      ) {
        setResyncNonce((n) => n + 1);
      }
    });

    return () => {
      removeBarListener();
      removeSubscribedListener();
      removeResyncListener();
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
