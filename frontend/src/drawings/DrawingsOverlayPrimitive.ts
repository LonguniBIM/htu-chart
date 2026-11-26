import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  ISeriesPrimitiveAxisView,
  SeriesAttachedParameter,
  SeriesOptionsMap,
  SeriesType,
  Time,
} from "lightweight-charts";

type MediaCanvasTarget = Parameters<IPrimitivePaneRenderer["draw"]>[0];
import type { OhlcvBar } from "../api/types";
import {
  computeFvgMidpoint,
  computeMeasureMetrics,
  computePositionMetrics,
  hitTestDrawing,
  timeToX,
  type ProjectedDrawingGeometry,
} from "./geometry";
import type {
  DrawingDraft,
  DrawingHitResult,
  DrawingRecord,
  InstrumentDrawingSpec,
  ProjectedAnchor,
} from "./types";

export interface DrawingsOverlayInput {
  drawings: readonly DrawingRecord[];
  selectedDrawingId: string | null;
  draft: DrawingDraft | null;
  overrideDrawing?: DrawingRecord | null;
  bars: readonly OhlcvBar[];
  timeframeSec: number;
  instrumentSpec: InstrumentDrawingSpec;
}

class DrawingPriceAxisView implements ISeriesPrimitiveAxisView {
  private readonly _coord: number | null;
  private readonly _text: string;
  private readonly _textColor: string;
  private readonly _backColor: string;

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

function drawBadge(
  context: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  backColor: string,
  textColor = "#f8fafc",
): void {
  context.font = "11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
  const metrics = context.measureText(text);
  const padX = 6;
  const boxH = 18;
  const boxW = metrics.width + padX * 2;
  const clampedX = Math.max(2, x);
  const clampedY = Math.max(2, y);
  context.fillStyle = backColor;
  context.fillRect(clampedX, clampedY, boxW, boxH);
  context.fillStyle = textColor;
  context.textBaseline = "middle";
  context.fillText(text, clampedX + padX, clampedY + boxH / 2);
}

function drawAnchorHandles(
  context: CanvasRenderingContext2D,
  anchors: readonly ProjectedAnchor[],
  strokeColor: string,
): void {
  for (const anchor of anchors) {
    context.beginPath();
    context.arc(anchor.x, anchor.y, 4.5, 0, Math.PI * 2);
    context.fillStyle = "#0f172a";
    context.fill();
    context.lineWidth = 2;
    context.strokeStyle = strokeColor;
    context.stroke();
  }
}

function renderProjectedItem(
  context: CanvasRenderingContext2D,
  mediaWidth: number,
  mediaHeight: number,
  item: ProjectedDrawingGeometry,
  options: {
    selected: boolean;
    isDraft: boolean;
    bars: readonly OhlcvBar[];
    timeframeSec: number;
    instrumentSpec: InstrumentDrawingSpec;
    series: ISeriesApi<keyof SeriesOptionsMap, Time>;
  },
): void {
  const { drawing, anchors } = item;
  const { selected, isDraft, bars, timeframeSec, instrumentSpec, series } = options;
  const decimals = priceDecimals(instrumentSpec.tickSize);
  const accentStroke = selected ? "#facc15" : "#38bdf8";

  context.save();
  if (isDraft) {
    context.globalAlpha = 0.85;
  }

  if (drawing.type === "hline" && anchors[0] !== undefined) {
    const level = anchors[0];
    context.beginPath();
    context.strokeStyle = selected ? "#facc15" : "#38bdf8";
    context.lineWidth = selected ? 2 : 1.5;
    context.moveTo(0, level.y);
    context.lineTo(mediaWidth, level.y);
    context.stroke();
    const label =
      typeof drawing.metadata?.label === "string" && drawing.metadata.label.trim()
        ? drawing.metadata.label.trim()
        : level.price.toFixed(decimals);
    drawBadge(context, label, 8, Math.max(4, level.y - 20), "rgba(15, 23, 42, 0.88)");
  } else if (drawing.type === "trendline" && anchors[0] && anchors[1]) {
    const [start, end] = anchors;
    context.beginPath();
    context.strokeStyle = accentStroke;
    context.lineWidth = selected ? 2 : 1.5;
    context.moveTo(start.x, start.y);
    context.lineTo(end.x, end.y);
    context.stroke();
  } else if (drawing.type === "ray" && anchors[0] && anchors[1]) {
    const [start, end] = anchors;
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const mag = Math.hypot(dx, dy);
    const farDist = Math.max(mediaWidth, mediaHeight, 2000) * 2 + Math.abs(start.x) + Math.abs(start.y);
    const rayEndX = mag > 0.001 ? start.x + (dx / mag) * farDist : end.x;
    const rayEndY = mag > 0.001 ? start.y + (dy / mag) * farDist : end.y;
    context.beginPath();
    context.strokeStyle = accentStroke;
    context.lineWidth = selected ? 2 : 1.5;
    context.moveTo(start.x, start.y);
    context.lineTo(rayEndX, rayEndY);
    context.stroke();
  } else if (drawing.type === "fvg_box" && anchors[0] && anchors[1]) {
    const [start, end] = anchors;
    const preset =
      typeof drawing.metadata?.direction === "string"
        ? drawing.metadata.direction
        : typeof drawing.metadata?.preset === "string"
          ? drawing.metadata.preset
          : "auto";
    const isBullish =
      preset === "bullish" ? true : preset === "bearish" ? false : end.price >= start.price;
    const extendRight = drawing.metadata?.extend_right === true;
    const minX = Math.min(start.x, end.x);
    const maxX = extendRight ? Math.max(mediaWidth, start.x, end.x) : Math.max(start.x, end.x);
    const minY = Math.min(start.y, end.y);
    const maxY = Math.max(start.y, end.y);
    const width = Math.max(2, maxX - minX);
    const height = Math.max(2, maxY - minY);

    context.fillStyle = isBullish ? "rgba(34, 197, 94, 0.22)" : "rgba(239, 68, 68, 0.22)";
    context.fillRect(minX, minY, width, height);
    context.strokeStyle = selected ? "#facc15" : isBullish ? "#22c55e" : "#ef4444";
    context.lineWidth = selected ? 2 : 1.25;
    context.strokeRect(minX, minY, width, height);

    const midPrice = computeFvgMidpoint(start.price, end.price);
    const midCoord = series.priceToCoordinate(midPrice);
    const midY = midCoord !== null ? Number(midCoord) : (minY + maxY) / 2;
    context.save();
    context.setLineDash([5, 4]);
    context.beginPath();
    context.strokeStyle = isBullish ? "#4ade80" : "#f87171";
    context.lineWidth = 1;
    context.moveTo(minX, midY);
    context.lineTo(minX + width, midY);
    context.stroke();
    context.restore();

    const ceLabel = `${isBullish ? "Bullish" : "Bearish"} FVG · CE ${midPrice.toFixed(Math.max(2, decimals))}`;
    drawBadge(
      context,
      ceLabel,
      minX + 4,
      Math.max(2, midY - 18),
      isBullish ? "rgba(21, 128, 61, 0.85)" : "rgba(185, 28, 28, 0.85)",
    );
  } else if (drawing.type === "order_block" && anchors[0] && anchors[1]) {
    const [start, end] = anchors;
    const extendRight = drawing.metadata?.extend_right !== false;
    const minX = Math.min(start.x, end.x);
    const maxX = extendRight ? Math.max(mediaWidth, start.x, end.x) : Math.max(start.x, end.x);
    const minY = Math.min(start.y, end.y);
    const maxY = Math.max(start.y, end.y);
    const width = Math.max(2, maxX - minX);
    const height = Math.max(2, maxY - minY);

    context.fillStyle = "rgba(168, 85, 247, 0.20)";
    context.fillRect(minX, minY, width, height);
    context.strokeStyle = selected ? "#facc15" : "#a855f7";
    context.lineWidth = selected ? 2 : 1.25;
    context.strokeRect(minX, minY, width, height);

    const customLabel =
      typeof drawing.metadata?.label === "string" && drawing.metadata.label.trim()
        ? drawing.metadata.label.trim()
        : "OB";
    drawBadge(context, customLabel, minX + 4, minY + 4, "rgba(126, 34, 206, 0.88)");
  } else if (
    (drawing.type === "long_position" || drawing.type === "short_position") &&
    anchors.length >= 3
  ) {
    const entry = anchors.find((a) => a.role === "entry") ?? anchors[0]!;
    const target = anchors.find((a) => a.role === "target") ?? anchors[1]!;
    const stop = anchors.find((a) => a.role === "stop") ?? anchors[2]!;
    const minX = Math.min(entry.x, target.x, stop.x);
    const rawMaxX = Math.max(entry.x, target.x, stop.x);
    const maxX = rawMaxX - minX < 24 ? minX + 80 : rawMaxX;
    const width = maxX - minX;

    // Reward (TP) box
    const tpTop = Math.min(entry.y, target.y);
    const tpHeight = Math.max(2, Math.abs(target.y - entry.y));
    context.fillStyle = "rgba(34, 197, 94, 0.22)";
    context.fillRect(minX, tpTop, width, tpHeight);
    context.strokeStyle = selected ? "#facc15" : "#22c55e";
    context.lineWidth = selected ? 1.75 : 1;
    context.strokeRect(minX, tpTop, width, tpHeight);

    // Risk (SL) box
    const slTop = Math.min(entry.y, stop.y);
    const slHeight = Math.max(2, Math.abs(stop.y - entry.y));
    context.fillStyle = "rgba(239, 68, 68, 0.22)";
    context.fillRect(minX, slTop, width, slHeight);
    context.strokeStyle = selected ? "#facc15" : "#ef4444";
    context.lineWidth = selected ? 1.75 : 1;
    context.strokeRect(minX, slTop, width, slHeight);

    // Entry divider line
    context.beginPath();
    context.strokeStyle = "#e2e8f0";
    context.lineWidth = 1.5;
    context.moveTo(minX, entry.y);
    context.lineTo(maxX, entry.y);
    context.stroke();

    const posMetrics = computePositionMetrics(
      drawing.type,
      entry.price,
      target.price,
      stop.price,
      instrumentSpec,
      1,
    );
    const hudText =
      `R:R ${posMetrics.rrRatio.toFixed(2)} | ` +
      `Risk ${posMetrics.riskTicks.toFixed(0)}t ($${posMetrics.dollarRisk.toFixed(2)}) | ` +
      `Rew ${posMetrics.rewardTicks.toFixed(0)}t ($${posMetrics.dollarReward.toFixed(2)})`;
    drawBadge(
      context,
      hudText,
      minX,
      Math.max(4, Math.min(tpTop, slTop) - 22),
      "rgba(15, 23, 42, 0.90)",
    );
  } else if (drawing.type === "measure" && anchors[0] && anchors[1]) {
    const [start, end] = anchors;
    const minX = Math.min(start.x, end.x);
    const maxX = Math.max(start.x, end.x);
    const minY = Math.min(start.y, end.y);
    const maxY = Math.max(start.y, end.y);
    const width = Math.max(2, maxX - minX);
    const height = Math.max(2, maxY - minY);

    context.fillStyle = "rgba(59, 130, 246, 0.16)";
    context.fillRect(minX, minY, width, height);
    context.strokeStyle = selected ? "#facc15" : "#60a5fa";
    context.lineWidth = selected ? 2 : 1.25;
    context.strokeRect(minX, minY, width, height);

    context.save();
    context.setLineDash([4, 3]);
    context.beginPath();
    context.moveTo(start.x, start.y);
    context.lineTo(end.x, end.y);
    context.stroke();
    context.restore();

    const metrics = computeMeasureMetrics(start, end, bars, timeframeSec, instrumentSpec);
    const sign = metrics.priceDelta >= 0 ? "+" : "";
    const hudText =
      `${sign}${metrics.priceDelta.toFixed(decimals)} (${sign}${metrics.pctChange.toFixed(2)}%) | ` +
      `${sign}${metrics.ticks.toFixed(0)}t | ${metrics.barCount} bars | ${metrics.durationLabel}`;
    drawBadge(
      context,
      hudText,
      minX,
      Math.max(4, minY - 22),
      metrics.priceDelta >= 0 ? "rgba(21, 128, 61, 0.90)" : "rgba(185, 28, 28, 0.90)",
    );
  }

  if (selected || isDraft) {
    drawAnchorHandles(context, anchors, selected ? "#facc15" : "#38bdf8");
  }
  context.restore();
}

class DrawingsPaneRenderer implements IPrimitivePaneRenderer {
  private readonly _overlay: DrawingsOverlayPrimitive;

  constructor(overlay: DrawingsOverlayPrimitive) {
    this._overlay = overlay;
  }

  draw(target: MediaCanvasTarget): void {
    const snapshot = this._overlay.getRenderSnapshot();
    if (snapshot.series === null) {
      return;
    }
    const series = snapshot.series;
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      for (const item of snapshot.projectedDrawings) {
        renderProjectedItem(context, mediaSize.width, mediaSize.height, item, {
          selected: item.drawing.id === snapshot.selectedDrawingId,
          isDraft: false,
          bars: snapshot.bars,
          timeframeSec: snapshot.timeframeSec,
          instrumentSpec: snapshot.instrumentSpec,
          series,
        });
      }
      if (snapshot.projectedDraft !== null) {
        renderProjectedItem(context, mediaSize.width, mediaSize.height, snapshot.projectedDraft, {
          selected: false,
          isDraft: true,
          bars: snapshot.bars,
          timeframeSec: snapshot.timeframeSec,
          instrumentSpec: snapshot.instrumentSpec,
          series,
        });
      }
    });
  }
}

class DrawingsPaneView implements IPrimitivePaneView {
  private readonly _renderer: DrawingsPaneRenderer;

  constructor(overlay: DrawingsOverlayPrimitive) {
    this._renderer = new DrawingsPaneRenderer(overlay);
  }

  zOrder(): "top" {
    return "top";
  }

  renderer(): IPrimitivePaneRenderer {
    return this._renderer;
  }
}

/**
 * Lightweight Charts v5 ISeriesPrimitive rendering persisted domain-coordinate drawings,
 * active interactive drafts, and right-axis price tags.
 */
export class DrawingsOverlayPrimitive implements ISeriesPrimitive<Time> {
  private _chart: IChartApiBase<Time> | null = null;
  private _series: ISeriesApi<SeriesType, Time> | null = null;
  private _requestUpdate: (() => void) | null = null;
  private readonly _paneView: DrawingsPaneView;

  private _drawings: readonly DrawingRecord[] = [];
  private _selectedDrawingId: string | null = null;
  private _draft: DrawingDraft | null = null;
  private _overrideDrawing: DrawingRecord | null = null;
  private _bars: readonly OhlcvBar[] = [];
  private _timeframeSec = 300;
  private _instrumentSpec: InstrumentDrawingSpec = { tickSize: 0.25, tickValue: 1.25 };
  private _viewportWidth = 800;

  private _projectedDrawings: ProjectedDrawingGeometry[] = [];
  private _projectedDraft: ProjectedDrawingGeometry | null = null;
  private _priceAxisViews: ISeriesPrimitiveAxisView[] = [];

  constructor() {
    this._paneView = new DrawingsPaneView(this);
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
    this._projectedDrawings = [];
    this._projectedDraft = null;
    this._priceAxisViews = [];
  }

  setViewportWidth(width: number): void {
    if (Number.isFinite(width) && width > 0) {
      this._viewportWidth = width;
    }
  }

  setData(input: DrawingsOverlayInput): void {
    this._drawings = input.drawings;
    this._selectedDrawingId = input.selectedDrawingId;
    this._draft = input.draft;
    this._overrideDrawing = input.overrideDrawing ?? null;
    this._bars = input.bars;
    this._timeframeSec = input.timeframeSec;
    this._instrumentSpec = input.instrumentSpec;
    this.updateAllViews();
    this._requestUpdate?.();
  }

  private _projectDrawing(drawing: DrawingRecord): ProjectedDrawingGeometry | null {
    if (this._chart === null || this._series === null) {
      return null;
    }
    const timeScale = this._chart.timeScale();
    const series = this._series;
    const projectedAnchors: ProjectedAnchor[] = [];

    for (const anchor of drawing.coordinates.anchors) {
      const yCoord = series.priceToCoordinate(anchor.price);
      if (yCoord === null || !Number.isFinite(yCoord)) {
        return null;
      }
      const xCoord = timeToX(anchor.time, timeScale, this._bars, this._timeframeSec);
      if (xCoord === null || !Number.isFinite(xCoord)) {
        if (drawing.type !== "hline") {
          return null;
        }
        projectedAnchors.push({
          role: anchor.role,
          time: anchor.time,
          price: anchor.price,
          x: this._viewportWidth * 0.5,
          y: Number(yCoord),
        });
      } else {
        projectedAnchors.push({
          role: anchor.role,
          time: anchor.time,
          price: anchor.price,
          x: Number(xCoord),
          y: Number(yCoord),
        });
      }
    }

    return { drawing, anchors: projectedAnchors };
  }

  updateAllViews(): void {
    if (this._chart === null || this._series === null) {
      this._projectedDrawings = [];
      this._projectedDraft = null;
      this._priceAxisViews = [];
      return;
    }

    const effectiveDrawings = this._drawings.map((d) =>
      this._overrideDrawing !== null && this._overrideDrawing.id === d.id
        ? this._overrideDrawing
        : d,
    );

    const nextProjected: ProjectedDrawingGeometry[] = [];
    const nextAxisViews: ISeriesPrimitiveAxisView[] = [];
    const decimals = priceDecimals(this._instrumentSpec.tickSize);

    for (const drawing of effectiveDrawings) {
      const projected = this._projectDrawing(drawing);
      if (projected !== null) {
        nextProjected.push(projected);
      }

      if (drawing.type === "hline" && drawing.coordinates.anchors[0] !== undefined) {
        const price = drawing.coordinates.anchors[0].price;
        const coord = this._series.priceToCoordinate(price);
        nextAxisViews.push(
          new DrawingPriceAxisView(
            coord !== null ? Number(coord) : null,
            price.toFixed(decimals),
            drawing.id === this._selectedDrawingId ? "#ca8a04" : "#0284c7",
          ),
        );
      }

      if (
        (drawing.type === "long_position" || drawing.type === "short_position") &&
        drawing.id === this._selectedDrawingId
      ) {
        const entry = drawing.coordinates.anchors.find((a) => a.role === "entry");
        const target = drawing.coordinates.anchors.find((a) => a.role === "target");
        const stop = drawing.coordinates.anchors.find((a) => a.role === "stop");
        if (entry !== undefined) {
          const c = this._series.priceToCoordinate(entry.price);
          nextAxisViews.push(
            new DrawingPriceAxisView(
              c !== null ? Number(c) : null,
              `Entry ${entry.price.toFixed(decimals)}`,
              "#475569",
            ),
          );
        }
        if (target !== undefined) {
          const c = this._series.priceToCoordinate(target.price);
          nextAxisViews.push(
            new DrawingPriceAxisView(
              c !== null ? Number(c) : null,
              `TP ${target.price.toFixed(decimals)}`,
              "#16a34a",
            ),
          );
        }
        if (stop !== undefined) {
          const c = this._series.priceToCoordinate(stop.price);
          nextAxisViews.push(
            new DrawingPriceAxisView(
              c !== null ? Number(c) : null,
              `SL ${stop.price.toFixed(decimals)}`,
              "#dc2626",
            ),
          );
        }
      }
    }

    let nextDraft: ProjectedDrawingGeometry | null = null;
    if (this._draft !== null) {
      const draftMetadata: Record<string, unknown> = {};
      if (this._draft.fvgPreset !== undefined) {
        draftMetadata.direction = this._draft.fvgPreset;
      }
      if (this._draft.extendRight !== undefined) {
        draftMetadata.extend_right = this._draft.extendRight;
      }
      const draftRecord: DrawingRecord = {
        id: "__draft__",
        revision: 0,
        type: this._draft.tool,
        symbol: "MES",
        contract_id: null,
        pane_id: null,
        created_by: "user",
        source_bar_refs: [],
        coordinates: { anchors: this._draft.anchors },
        style: {},
        metadata: draftMetadata,
        created_at_utc: 0,
        updated_at_utc: 0,
      };
      nextDraft = this._projectDrawing(draftRecord);
    }

    this._projectedDrawings = nextProjected;
    this._projectedDraft = nextDraft;
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
    projectedDrawings: readonly ProjectedDrawingGeometry[];
    projectedDraft: ProjectedDrawingGeometry | null;
    selectedDrawingId: string | null;
    bars: readonly OhlcvBar[];
    timeframeSec: number;
    instrumentSpec: InstrumentDrawingSpec;
  } {
    return {
      series: this._series,
      projectedDrawings: this._projectedDrawings,
      projectedDraft: this._projectedDraft,
      selectedDrawingId: this._selectedDrawingId,
      bars: this._bars,
      timeframeSec: this._timeframeSec,
      instrumentSpec: this._instrumentSpec,
    };
  }

  hitTestAt(px: number, py: number): DrawingHitResult | null {
    this.updateAllViews();
    return hitTestDrawing(px, py, this._projectedDrawings, this._selectedDrawingId, this._viewportWidth);
  }
}
