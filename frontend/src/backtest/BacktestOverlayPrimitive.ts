import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  ISeriesPrimitiveAxisView,
  ITimeScaleApi,
  Logical,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";
import type { BacktestTradeLine, BacktestZone, OhlcvBar } from "../api/types";
import type { ReplayPosition } from "../replay/replayEngine";

type MediaCanvasTarget = Parameters<IPrimitivePaneRenderer["draw"]>[0];

export interface BacktestOverlayInput {
  zones: readonly BacktestZone[];
  tradeLines: readonly BacktestTradeLine[];
  showZones: boolean;
  showTradeLines: boolean;
  replayPosition: ReplayPosition | null;
  bars: readonly OhlcvBar[];
  timeframeSec: number;
  tickSize: number;
}

export interface ProjectedZone {
  zone: BacktestZone;
  x1: number;
  x2: number;
  yTop: number;
  yBottom: number;
  yMid: number | null;
}

export interface ProjectedTradeLine {
  line: BacktestTradeLine;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface ProjectedReplayLevel {
  label: string;
  price: number;
  y: number;
  color: string;
}

class OverlayPriceAxisView implements ISeriesPrimitiveAxisView {
  private readonly _coord: number | null;
  private readonly _text: string;
  private readonly _backColor: string;
  private readonly _textColor: string;

  constructor(
    coord: number | null,
    text: string,
    backColor: string,
    textColor = "#ffffff",
  ) {
    this._coord = coord;
    this._text = text;
    this._backColor = backColor;
    this._textColor = textColor;
  }

  coordinate(): number {
    return this._coord ?? -10000;
  }

  text(): string {
    return this._text;
  }

  textColor(): string {
    return this._textColor;
  }

  backColor(): string {
    return this._backColor;
  }

  visible(): boolean {
    return this._coord !== null && Number.isFinite(this._coord);
  }
}

function priceDecimals(tickSize: number): number {
  const str = tickSize.toString();
  const dot = str.indexOf(".");
  return dot === -1 ? 0 : Math.min(4, str.length - dot - 1);
}

function timeToLogical(
  timeSec: number,
  bars: readonly OhlcvBar[],
  timeframeSec: number,
): number | null {
  if (bars.length === 0) {
    return null;
  }
  const lastIdx = bars.length - 1;
  const first = bars[0]!;
  const last = bars[lastIdx]!;
  const step =
    timeframeSec > 0
      ? timeframeSec
      : bars.length >= 2
        ? Math.max(1, bars[1]!.time - first.time)
        : 60;

  if (timeSec <= first.time) {
    return timeSec === first.time ? 0 : (timeSec - first.time) / step;
  }
  if (timeSec >= last.time) {
    return timeSec === last.time ? lastIdx : lastIdx + (timeSec - last.time) / step;
  }

  let lo = 0;
  let hi = lastIdx;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const t = bars[mid]!.time;
    if (t === timeSec) {
      return mid;
    }
    if (t < timeSec) {
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  const leftIdx = Math.max(0, hi);
  const rightIdx = Math.min(lastIdx, lo);
  const leftTime = bars[leftIdx]!.time;
  const rightTime = bars[rightIdx]!.time;
  if (rightTime <= leftTime) {
    return leftIdx;
  }
  return leftIdx + (timeSec - leftTime) / (rightTime - leftTime);
}

function projectTimeToX(
  timeSec: number,
  timeScale: ITimeScaleApi<Time>,
  bars: readonly OhlcvBar[],
  timeframeSec: number,
): number | null {
  if (typeof timeScale.timeToCoordinate === "function") {
    const direct = timeScale.timeToCoordinate(timeSec as Time);
    if (direct !== null && Number.isFinite(direct)) {
      return Number(direct);
    }
  }
  if (typeof timeScale.logicalToCoordinate === "function") {
    const logical = timeToLogical(timeSec, bars, timeframeSec);
    if (logical !== null) {
      const coord = timeScale.logicalToCoordinate(logical as Logical);
      if (coord !== null && Number.isFinite(coord)) {
        return Number(coord);
      }
    }
  }
  return null;
}

function drawBadge(
  context: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  backColor: string,
  textColor = "#f8fafc",
): void {
  context.font = "10px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
  const metrics = context.measureText(text);
  const padX = 5;
  const boxH = 16;
  const boxW = metrics.width + padX * 2;
  const clampedX = Math.max(2, x);
  const clampedY = Math.max(2, y);
  context.fillStyle = backColor;
  context.fillRect(clampedX, clampedY, boxW, boxH);
  context.fillStyle = textColor;
  context.textBaseline = "middle";
  context.fillText(text, clampedX + padX, clampedY + boxH / 2);
}

class BacktestOverlayRenderer implements IPrimitivePaneRenderer {
  private readonly _primitive: BacktestOverlayPrimitive;

  constructor(primitive: BacktestOverlayPrimitive) {
    this._primitive = primitive;
  }

  draw(target: MediaCanvasTarget): void {
    const snap = this._primitive.getRenderSnapshot();
    if (snap.series === null) {
      return;
    }

    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      const clipW =
        snap.viewportWidth > 0
          ? Math.min(mediaSize.width, snap.viewportWidth)
          : mediaSize.width;
      const clipH =
        snap.viewportHeight > 0
          ? Math.min(mediaSize.height, snap.viewportHeight)
          : mediaSize.height;
      if (clipW <= 0 || clipH <= 0) {
        return;
      }

      context.save();
      context.beginPath();
      context.rect(0, 0, clipW, clipH);
      context.clip();

      if (snap.showZones) {
        for (const item of snap.projectedZones) {
          const { zone, x1, x2, yTop, yBottom, yMid } = item;
          const minX = Math.min(x1, x2);
          const width = Math.max(4, Math.abs(x2 - x1));
          const minY = Math.min(yTop, yBottom);
          const height = Math.max(2, Math.abs(yBottom - yTop));
          const isBullish = zone.direction === "bullish";

          if (zone.kind === "fvg") {
            context.fillStyle = isBullish
              ? "rgba(34, 197, 94, 0.16)"
              : "rgba(239, 68, 68, 0.16)";
            context.fillRect(minX, minY, width, height);
            context.strokeStyle = isBullish ? "#22c55e" : "#ef4444";
            context.lineWidth = 1;
            context.strokeRect(minX, minY, width, height);

            const ceY = yMid !== null ? yMid : (minY + minY + height) / 2;
            context.save();
            context.setLineDash([4, 4]);
            context.beginPath();
            context.strokeStyle = isBullish ? "#4ade80" : "#f87171";
            context.lineWidth = 1;
            context.moveTo(minX, ceY);
            context.lineTo(minX + width, ceY);
            context.stroke();
            context.restore();

            const ceText = zone.label ? `${zone.label} (50% CE)` : "FVG 50% CE";
            drawBadge(
              context,
              ceText,
              minX + 3,
              Math.max(2, ceY - 17),
              isBullish ? "rgba(21, 128, 61, 0.85)" : "rgba(185, 28, 28, 0.85)",
            );
          } else if (zone.kind === "order_block") {
            context.fillStyle = "rgba(168, 85, 247, 0.18)";
            context.fillRect(minX, minY, width, height);
            context.strokeStyle = "#a855f7";
            context.lineWidth = 1;
            context.strokeRect(minX, minY, width, height);
            if (zone.label) {
              drawBadge(context, zone.label, minX + 3, minY + 3, "rgba(126, 34, 206, 0.88)");
            }
          } else {
            // liquidity_level
            context.fillStyle = isBullish
              ? "rgba(245, 158, 11, 0.14)"
              : "rgba(56, 189, 248, 0.14)";
            context.fillRect(minX, minY, width, height);
            context.save();
            context.setLineDash([3, 3]);
            context.strokeStyle = isBullish ? "#f59e0b" : "#38bdf8";
            context.lineWidth = 1;
            context.strokeRect(minX, minY, width, height);
            if (yMid !== null) {
              context.beginPath();
              context.moveTo(minX, yMid);
              context.lineTo(minX + width, yMid);
              context.stroke();
            }
            context.restore();
            if (zone.label) {
              drawBadge(
                context,
                zone.label,
                minX + 3,
                Math.max(2, minY - 17),
                "rgba(15, 23, 42, 0.88)",
              );
            }
          }
        }
      }

      if (snap.showTradeLines) {
        for (const item of snap.projectedTradeLines) {
          const { line, x1, y1, x2, y2 } = item;
          context.save();
          context.strokeStyle = line.color;
          context.lineWidth = 1.5;
          context.setLineDash([5, 3]);
          context.beginPath();
          context.moveTo(x1, y1);
          context.lineTo(x2, y2);
          context.stroke();
          context.restore();

          context.beginPath();
          context.arc(x1, y1, 3.5, 0, Math.PI * 2);
          context.fillStyle = line.color;
          context.fill();

          context.beginPath();
          context.arc(x2, y2, 3.5, 0, Math.PI * 2);
          context.fillStyle = line.color;
          context.fill();

          const sign = line.pnl_dollars >= 0 ? "+" : "-";
          const pnlText = `${line.trade_id} ${sign}$${Math.abs(line.pnl_dollars).toFixed(2)}${
            line.ambiguous_fill ? " AMBIGUOUS" : ""
          }`;
          drawBadge(
            context,
            pnlText,
            (x1 + x2) / 2,
            Math.max(2, (y1 + y2) / 2 - 18),
            line.ambiguous_fill ? "rgba(180, 83, 9, 0.9)" : "rgba(15, 23, 42, 0.88)",
          );
        }
      }

      if (snap.projectedReplayLevels.length > 0) {
        const dec = priceDecimals(snap.tickSize);
        for (const lvl of snap.projectedReplayLevels) {
          context.save();
          context.setLineDash([6, 4]);
          context.strokeStyle = lvl.color;
          context.lineWidth = 1.5;
          context.beginPath();
          context.moveTo(0, lvl.y);
          context.lineTo(clipW, lvl.y);
          context.stroke();
          context.restore();
          drawBadge(
            context,
            `${lvl.label} ${lvl.price.toFixed(dec)}`,
            8,
            Math.max(2, lvl.y - 18),
            "rgba(15, 23, 42, 0.9)",
          );
        }
      }

      context.restore();
    });
  }
}

class BacktestOverlayPaneView implements IPrimitivePaneView {
  private readonly _renderer: BacktestOverlayRenderer;

  constructor(primitive: BacktestOverlayPrimitive) {
    this._renderer = new BacktestOverlayRenderer(primitive);
  }

  zOrder(): "normal" {
    return "normal";
  }

  renderer(): IPrimitivePaneRenderer {
    return this._renderer;
  }
}

export class BacktestOverlayPrimitive implements ISeriesPrimitive<Time> {
  private _chart: IChartApiBase<Time> | null = null;
  private _series: ISeriesApi<SeriesType, Time> | null = null;
  private _requestUpdate: (() => void) | null = null;

  private _zones: readonly BacktestZone[] = [];
  private _tradeLines: readonly BacktestTradeLine[] = [];
  private _showZones = true;
  private _showTradeLines = true;
  private _replayPosition: ReplayPosition | null = null;
  private _bars: readonly OhlcvBar[] = [];
  private _timeframeSec = 300;
  private _tickSize = 0.25;
  private _viewportWidth = 0;
  private _viewportHeight = 0;

  private _projectedZones: ProjectedZone[] = [];
  private _projectedTradeLines: ProjectedTradeLine[] = [];
  private _projectedReplayLevels: ProjectedReplayLevel[] = [];
  private _priceAxisViews: ISeriesPrimitiveAxisView[] = [];
  private readonly _paneView: BacktestOverlayPaneView;

  constructor() {
    this._paneView = new BacktestOverlayPaneView(this);
  }

  attached(param: SeriesAttachedParameter<Time, SeriesType>): void {
    this._chart = param.chart;
    this._series = param.series;
    this._requestUpdate = param.requestUpdate;
    this.updateAllViews();
  }

  detached(): void {
    this._chart = null;
    this._series = null;
    this._requestUpdate = null;
    this._projectedZones = [];
    this._projectedTradeLines = [];
    this._projectedReplayLevels = [];
    this._priceAxisViews = [];
  }

  setViewportSize(width: number, height: number): void {
    this._viewportWidth = Number.isFinite(width) && width > 0 ? width : 0;
    this._viewportHeight = Number.isFinite(height) && height > 0 ? height : 0;
    this._requestUpdate?.();
  }

  setData(input: BacktestOverlayInput): void {
    this._zones = input.zones;
    this._tradeLines = input.tradeLines;
    this._showZones = input.showZones;
    this._showTradeLines = input.showTradeLines;
    this._replayPosition = input.replayPosition;
    this._bars = input.bars;
    this._timeframeSec = input.timeframeSec;
    this._tickSize = input.tickSize;
    this.updateAllViews();
    this._requestUpdate?.();
  }

  updateAllViews(): void {
    if (this._chart === null || this._series === null) {
      this._projectedZones = [];
      this._projectedTradeLines = [];
      this._projectedReplayLevels = [];
      this._priceAxisViews = [];
      return;
    }

    const timeScale = this._chart.timeScale();
    const series = this._series;

    const nextZones: ProjectedZone[] = [];
    if (this._showZones) {
      for (const zone of this._zones) {
        const x1 = projectTimeToX(zone.start_time, timeScale, this._bars, this._timeframeSec);
        const x2 = projectTimeToX(zone.end_time, timeScale, this._bars, this._timeframeSec);
        const yTop = series.priceToCoordinate(zone.top);
        const yBottom = series.priceToCoordinate(zone.bottom);
        if (
          x1 === null ||
          x2 === null ||
          yTop === null ||
          yBottom === null ||
          !Number.isFinite(yTop) ||
          !Number.isFinite(yBottom)
        ) {
          continue;
        }
        const midPrice = zone.midpoint ?? (zone.top + zone.bottom) / 2;
        const yMidCoord = series.priceToCoordinate(midPrice);
        nextZones.push({
          zone,
          x1,
          x2,
          yTop: Number(yTop),
          yBottom: Number(yBottom),
          yMid: yMidCoord !== null && Number.isFinite(yMidCoord) ? Number(yMidCoord) : null,
        });
      }
    }

    const nextLines: ProjectedTradeLine[] = [];
    if (this._showTradeLines) {
      for (const line of this._tradeLines) {
        const x1 = projectTimeToX(line.entry_time, timeScale, this._bars, this._timeframeSec);
        const x2 = projectTimeToX(line.exit_time, timeScale, this._bars, this._timeframeSec);
        const y1 = series.priceToCoordinate(line.entry_price);
        const y2 = series.priceToCoordinate(line.exit_price);
        if (
          x1 === null ||
          x2 === null ||
          y1 === null ||
          y2 === null ||
          !Number.isFinite(y1) ||
          !Number.isFinite(y2)
        ) {
          continue;
        }
        nextLines.push({
          line,
          x1,
          y1: Number(y1),
          x2,
          y2: Number(y2),
        });
      }
    }

    const nextLevels: ProjectedReplayLevel[] = [];
    const nextAxisViews: ISeriesPrimitiveAxisView[] = [];
    if (this._replayPosition !== null) {
      const dec = priceDecimals(this._tickSize);
      const specs = [
        { label: "Entry", price: this._replayPosition.entryPrice, color: "#38bdf8" },
        { label: "SL", price: this._replayPosition.stopLoss, color: "#ef4444" },
        { label: "TP", price: this._replayPosition.takeProfit, color: "#22c55e" },
      ] as const;
      for (const spec of specs) {
        const y = series.priceToCoordinate(spec.price);
        if (y !== null && Number.isFinite(y)) {
          nextLevels.push({
            label: spec.label,
            price: spec.price,
            y: Number(y),
            color: spec.color,
          });
          nextAxisViews.push(
            new OverlayPriceAxisView(
              Number(y),
              `${spec.label} ${spec.price.toFixed(dec)}`,
              spec.color,
            ),
          );
        }
      }
    }

    this._projectedZones = nextZones;
    this._projectedTradeLines = nextLines;
    this._projectedReplayLevels = nextLevels;
    this._priceAxisViews = nextAxisViews;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return [this._paneView];
  }

  priceAxisViews(): readonly ISeriesPrimitiveAxisView[] {
    return this._priceAxisViews;
  }

  getRenderSnapshot(): {
    series: ISeriesApi<SeriesType, Time> | null;
    projectedZones: readonly ProjectedZone[];
    projectedTradeLines: readonly ProjectedTradeLine[];
    projectedReplayLevels: readonly ProjectedReplayLevel[];
    showZones: boolean;
    showTradeLines: boolean;
    tickSize: number;
    viewportWidth: number;
    viewportHeight: number;
  } {
    return {
      series: this._series,
      projectedZones: this._projectedZones,
      projectedTradeLines: this._projectedTradeLines,
      projectedReplayLevels: this._projectedReplayLevels,
      showZones: this._showZones,
      showTradeLines: this._showTradeLines,
      tickSize: this._tickSize,
      viewportWidth: this._viewportWidth,
      viewportHeight: this._viewportHeight,
    };
  }
}
