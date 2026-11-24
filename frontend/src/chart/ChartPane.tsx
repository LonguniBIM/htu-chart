import { useCallback, useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  createChart,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { fetchBars } from "../api/client";
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
  onSymbolChange?: (paneId: string, symbol: RootSymbol) => void;
  onTimeframeChange?: (paneId: string, timeframe: Timeframe) => void;
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

export function ChartPane({
  pane,
  contractId,
  activeFeedLive,
  wsClient,
  coordinator,
  onSymbolChange,
  onTimeframeChange,
}: ChartPaneProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const legendRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
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
      if (legendEl === null) {
        return;
      }
      if (param.time === undefined) {
        legendEl.textContent = formatLegendText(
          symbolRef.current,
          timeframeRef.current,
          latestBarRef.current,
        );
        return;
      }
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
    };
    chart.subscribeCrosshairMove(handleCrosshair);

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
      resizeObserver.disconnect();
      chart.unsubscribeCrosshairMove(handleCrosshair);
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      latestBarRef.current = null;
      lastBarTimeRef.current = null;
    };
  }, []);

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
    <section className="chart-pane" data-pane-id={pane.pane_id}>
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
