import type { Coordinate, ITimeScaleApi, Logical, Time } from "lightweight-charts";
import type { OhlcvBar, Timeframe } from "../api/types";
import type {
  DrawingAnchor,
  DrawingHitResult,
  DrawingRecord,
  InstrumentDrawingSpec,
  MeasureMetrics,
  PositionMetrics,
  ProjectedAnchor,
} from "./types";

export const MIN_DRAWING_TIMESTAMP_SEC = 946684800;

export const TIMEFRAME_SECONDS: Record<Timeframe, number> = {
  "1m": 60,
  "3m": 180,
  "5m": 300,
  "15m": 900,
  "30m": 1800,
  "1h": 3600,
  "4h": 14400,
  "1d": 86400,
};

export type TimeScaleCoordinateApi = Pick<
  ITimeScaleApi<Time>,
  "timeToCoordinate" | "coordinateToTime" | "logicalToCoordinate" | "coordinateToLogical"
>;

function countDecimals(step: number): number {
  const str = step.toString();
  const dot = str.indexOf(".");
  if (dot === -1) {
    return 0;
  }
  return str.length - dot - 1;
}

/**
 * Snap a raw price to the instrument's tickSize without IEEE-754 rounding drift.
 */
export function snapPriceToTick(price: number, tickSize: number): number {
  if (!Number.isFinite(price) || !Number.isFinite(tickSize) || tickSize <= 0) {
    return price;
  }
  const decimals = Math.min(8, countDecimals(tickSize));
  const snapped = Math.round(price / tickSize) * tickSize;
  return Number(snapped.toFixed(decimals));
}

function findNearestBarIndex(bars: readonly OhlcvBar[], rawTime: number): number {
  let lo = 0;
  let hi = bars.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid]!.time < rawTime) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  if (lo === 0) {
    return 0;
  }
  const prevDist = Math.abs(bars[lo - 1]!.time - rawTime);
  const currDist = Math.abs(bars[lo]!.time - rawTime);
  return prevDist <= currDist ? lo - 1 : lo;
}

/**
 * Magnet Mode: snap (rawTime, rawPrice) to the nearest bar's timestamp and closest OHLC price.
 */
export function snapToNearestOhlc(
  rawTime: number,
  rawPrice: number,
  bars: readonly OhlcvBar[],
  tickSize?: number,
): { time: number; price: number } {
  if (bars.length === 0) {
    const fallbackPrice =
      tickSize !== undefined && tickSize > 0 ? snapPriceToTick(rawPrice, tickSize) : rawPrice;
    return {
      time: Math.max(MIN_DRAWING_TIMESTAMP_SEC, Math.round(rawTime)),
      price: fallbackPrice,
    };
  }
  const idx = findNearestBarIndex(bars, rawTime);
  const bar = bars[idx]!;
  const candidates = [bar.open, bar.high, bar.low, bar.close] as const;
  let bestPrice = candidates[0];
  let bestDist = Math.abs(bestPrice - rawPrice);
  for (let i = 1; i < candidates.length; i += 1) {
    const candidate = candidates[i]!;
    const dist = Math.abs(candidate - rawPrice);
    if (dist < bestDist) {
      bestDist = dist;
      bestPrice = candidate;
    }
  }
  return {
    time: bar.time,
    price: bestPrice,
  };
}

/**
 * Convert a domain UTC timestamp to a fractional bar logical index using binary search +
 * interpolation/extrapolation so anchors between bars or in the future margin map cleanly.
 */
export function timeToLogicalIndex(
  timeSec: number,
  bars: readonly OhlcvBar[],
  timeframeSec: number,
): number | null {
  if (bars.length === 0) {
    return null;
  }
  const lastIdx = bars.length - 1;
  const firstBar = bars[0]!;
  const lastBar = bars[lastIdx]!;
  const fallbackStep =
    timeframeSec > 0
      ? timeframeSec
      : bars.length >= 2
        ? Math.max(1, bars[1]!.time - firstBar.time)
        : 60;

  if (timeSec <= firstBar.time) {
    if (timeSec === firstBar.time) {
      return 0;
    }
    return (timeSec - firstBar.time) / fallbackStep;
  }
  if (timeSec >= lastBar.time) {
    if (timeSec === lastBar.time) {
      return lastIdx;
    }
    const rightStep =
      timeframeSec > 0
        ? timeframeSec
        : bars.length >= 2
          ? Math.max(1, lastBar.time - bars[lastIdx - 1]!.time)
          : 60;
    return lastIdx + (timeSec - lastBar.time) / rightStep;
  }

  let lo = 0;
  let hi = lastIdx;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const midTime = bars[mid]!.time;
    if (midTime === timeSec) {
      return mid;
    }
    if (midTime < timeSec) {
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  const rightIdx = lo;
  const leftIdx = Math.max(0, rightIdx - 1);
  const leftTime = bars[leftIdx]!.time;
  const rightTime = bars[rightIdx]!.time;
  const span = rightTime - leftTime;
  if (span <= 0) {
    return leftIdx;
  }
  return leftIdx + (timeSec - leftTime) / span;
}

/**
 * Project domain timestamp (seconds) to chart CSS pixel X coordinate.
 * Uses direct timeToCoordinate for exact bar matches, and seamlessly interpolates/extrapolates
 * via logicalToCoordinate when the anchor lies between higher-TF bars or in future margin.
 */
export function timeToX(
  timeSec: number,
  timeScale: Partial<TimeScaleCoordinateApi>,
  bars: readonly OhlcvBar[],
  timeframeSec: number,
): number | null {
  if (!Number.isFinite(timeSec)) {
    return null;
  }
  if (typeof timeScale.timeToCoordinate === "function") {
    const direct: Coordinate | null = timeScale.timeToCoordinate(timeSec as Time);
    if (direct !== null && Number.isFinite(direct)) {
      return Number(direct);
    }
  }
  if (typeof timeScale.logicalToCoordinate !== "function") {
    return null;
  }
  const logical = timeToLogicalIndex(timeSec, bars, timeframeSec);
  if (logical === null || !Number.isFinite(logical)) {
    return null;
  }
  const coord: Coordinate | null = timeScale.logicalToCoordinate(logical as Logical);
  return coord !== null && Number.isFinite(coord) ? Number(coord) : null;
}

/**
 * Convert chart CSS pixel X coordinate to domain UTC Unix seconds (integer >= 946684800).
 * Supports both on-bar coordinates and right/left whitespace margin extrapolation.
 */
export function xToTime(
  x: number,
  timeScale: Partial<TimeScaleCoordinateApi>,
  bars: readonly OhlcvBar[],
  timeframeSec: number,
): number | null {
  if (!Number.isFinite(x)) {
    return null;
  }
  if (typeof timeScale.coordinateToLogical === "function" && bars.length > 0) {
    const logical = timeScale.coordinateToLogical(x);
    if (logical !== null && Number.isFinite(logical)) {
      const rawLogical = Number(logical);
      let continuousLogical: number | null = null;
      if (Math.abs(rawLogical - Math.round(rawLogical)) > 1e-6) {
        continuousLogical = rawLogical;
      } else {
        const k = Math.round(rawLogical);
        if (typeof timeScale.logicalToCoordinate === "function") {
          const kCoord = timeScale.logicalToCoordinate(k as Logical);
          if (kCoord !== null && Number.isFinite(kCoord)) {
            const kx = Number(kCoord);
            const dx = x - kx;
            if (Math.abs(dx) <= 1e-6) {
              continuousLogical = k;
            } else {
              const nextCoord = timeScale.logicalToCoordinate((k + 1) as Logical);
              const prevCoord = timeScale.logicalToCoordinate((k - 1) as Logical);
              const nextSpacing =
                nextCoord !== null && Number.isFinite(nextCoord) && Number(nextCoord) > kx
                  ? Number(nextCoord) - kx
                  : 0;
              const prevSpacing =
                prevCoord !== null && Number.isFinite(prevCoord) && kx > Number(prevCoord)
                  ? kx - Number(prevCoord)
                  : 0;
              const barSpacing =
                dx > 0
                  ? nextSpacing > 0
                    ? nextSpacing
                    : prevSpacing
                  : prevSpacing > 0
                    ? prevSpacing
                    : nextSpacing;
              continuousLogical = barSpacing > 0 ? k + dx / barSpacing : k;
            }
          } else if (
            k >= 0 &&
            k < bars.length &&
            typeof timeScale.timeToCoordinate === "function"
          ) {
            const barCoord = timeScale.timeToCoordinate(bars[k]!.time as Time);
            if (
              barCoord !== null &&
              Number.isFinite(barCoord) &&
              Math.abs(Number(barCoord) - x) <= 0.5
            ) {
              return Math.max(MIN_DRAWING_TIMESTAMP_SEC, Math.round(bars[k]!.time));
            }
          }
        } else {
          continuousLogical = k;
        }
      }

      if (continuousLogical !== null) {
        const lastIdx = bars.length - 1;
        const nearestIdx = Math.round(continuousLogical);
        if (
          nearestIdx >= 0 &&
          nearestIdx <= lastIdx &&
          Math.abs(continuousLogical - nearestIdx) <= 1e-6
        ) {
          return Math.max(MIN_DRAWING_TIMESTAMP_SEC, Math.round(bars[nearestIdx]!.time));
        }
        let resolvedTime: number;
        if (continuousLogical < 0) {
          const leftStep =
            timeframeSec > 0
              ? timeframeSec
              : bars.length >= 2
                ? Math.max(1, bars[1]!.time - bars[0]!.time)
                : 60;
          resolvedTime = bars[0]!.time + continuousLogical * leftStep;
        } else if (continuousLogical > lastIdx) {
          const rightStep =
            timeframeSec > 0
              ? timeframeSec
              : bars.length >= 2
                ? Math.max(1, bars[lastIdx]!.time - bars[lastIdx - 1]!.time)
                : 60;
          resolvedTime = bars[lastIdx]!.time + (continuousLogical - lastIdx) * rightStep;
        } else {
          const leftIdx = Math.floor(continuousLogical);
          const frac = continuousLogical - leftIdx;
          resolvedTime =
            bars[leftIdx]!.time + frac * (bars[leftIdx + 1]!.time - bars[leftIdx]!.time);
        }
        return Math.max(MIN_DRAWING_TIMESTAMP_SEC, Math.round(resolvedTime));
      }
    }
  }
  if (typeof timeScale.coordinateToTime === "function") {
    const direct = timeScale.coordinateToTime(x);
    if (typeof direct === "number" && Number.isFinite(direct)) {
      if (bars.length === 0 || typeof timeScale.timeToCoordinate !== "function") {
        return Math.max(MIN_DRAWING_TIMESTAMP_SEC, Math.round(direct));
      }
      const barX = timeScale.timeToCoordinate(direct as Time);
      if (barX !== null && Number.isFinite(barX) && Math.abs(Number(barX) - x) <= 0.5) {
        return Math.max(MIN_DRAWING_TIMESTAMP_SEC, Math.round(direct));
      }
    }
  }
  return null;
}

/**
 * Exact 50% Consequent Encroachment midpoint for an FVG box.
 */
export function computeFvgMidpoint(startPrice: number, endPrice: number): number {
  return (startPrice + endPrice) / 2;
}

/**
 * Compute Long/Short position Risk/Reward metrics with zero-division protection.
 */
export function computePositionMetrics(
  type: "long_position" | "short_position",
  entryPrice: number,
  targetPrice: number,
  stopPrice: number,
  spec: InstrumentDrawingSpec,
  contracts = 1,
): PositionMetrics {
  const riskPrice =
    type === "long_position"
      ? Math.max(0, entryPrice - stopPrice)
      : Math.max(0, stopPrice - entryPrice);
  const rewardPrice =
    type === "long_position"
      ? Math.max(0, targetPrice - entryPrice)
      : Math.max(0, entryPrice - targetPrice);
  const tickSize = spec.tickSize > 0 ? spec.tickSize : 0.25;
  const riskTicks = riskPrice / tickSize;
  const rewardTicks = rewardPrice / tickSize;
  const qty = Number.isFinite(contracts) && contracts > 0 ? contracts : 1;
  const dollarRisk = riskTicks * spec.tickValue * qty;
  const dollarReward = rewardTicks * spec.tickValue * qty;
  const rrRatio = riskPrice > 0 ? rewardPrice / riskPrice : 0;

  return {
    riskPrice,
    rewardPrice,
    riskTicks,
    rewardTicks,
    dollarRisk,
    dollarReward,
    rrRatio,
  };
}

export function formatDurationLabel(durationSec: number): string {
  const total = Math.max(0, Math.round(durationSec));
  if (total < 60) {
    return `${total}s`;
  }
  if (total < 3600) {
    const mins = Math.floor(total / 60);
    const secs = total % 60;
    return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
  }
  if (total < 86400) {
    const hours = Math.floor(total / 3600);
    const mins = Math.floor((total % 3600) / 60);
    return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
  }
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
}

/**
 * Compute Measure tool price delta, % change, ticks, bar count, and duration.
 */
export function computeMeasureMetrics(
  startAnchor: Pick<DrawingAnchor, "time" | "price">,
  endAnchor: Pick<DrawingAnchor, "time" | "price">,
  bars: readonly OhlcvBar[],
  timeframeSec: number,
  spec: InstrumentDrawingSpec,
): MeasureMetrics {
  const priceDelta = endAnchor.price - startAnchor.price;
  const pctChange = startAnchor.price !== 0 ? (priceDelta / startAnchor.price) * 100 : 0;
  const tickSize = spec.tickSize > 0 ? spec.tickSize : 0.25;
  const ticks = priceDelta / tickSize;
  const durationSec = Math.abs(endAnchor.time - startAnchor.time);

  let barCount = 0;
  const startLogical = timeToLogicalIndex(startAnchor.time, bars, timeframeSec);
  const endLogical = timeToLogicalIndex(endAnchor.time, bars, timeframeSec);
  if (startLogical !== null && endLogical !== null) {
    barCount = Math.round(Math.abs(endLogical - startLogical));
  } else if (timeframeSec > 0) {
    barCount = Math.round(durationSec / timeframeSec);
  }

  return {
    priceDelta,
    pctChange,
    ticks,
    barCount,
    durationSec,
    durationLabel: formatDurationLabel(durationSec),
  };
}

/**
 * Enforce directional price invariants on long_position and short_position anchors
 * by at least 1 * tickSize so invalid orders/inversions cannot be persisted.
 */
export function clampPositionAnchors(
  type: "long_position" | "short_position",
  anchors: readonly DrawingAnchor[],
  movedRole: "entry" | "target" | "stop" | "body",
  tickSize: number,
): DrawingAnchor[] {
  const step = Number.isFinite(tickSize) && tickSize > 0 ? tickSize : 0.25;
  const entry = anchors.find((a) => a.role === "entry") ?? anchors[0];
  const target = anchors.find((a) => a.role === "target") ?? anchors[1];
  const stop = anchors.find((a) => a.role === "stop") ?? anchors[2];
  if (entry === undefined || target === undefined || stop === undefined) {
    return anchors.map((a) => ({ ...a }));
  }

  let entryPrice = Math.max(step, snapPriceToTick(entry.price, step));
  let targetPrice = Math.max(step, snapPriceToTick(target.price, step));
  let stopPrice = Math.max(step, snapPriceToTick(stop.price, step));

  if (type === "long_position") {
    if (movedRole === "entry") {
      const minEntry = Math.max(step * 2, snapPriceToTick(stopPrice + step, step));
      const maxEntry = Math.max(minEntry, snapPriceToTick(targetPrice - step, step));
      entryPrice = Math.min(Math.max(entryPrice, minEntry), maxEntry);
    }
    if (stopPrice >= entryPrice) {
      if (movedRole === "stop") {
        stopPrice = Math.max(step, snapPriceToTick(entryPrice - step, step));
        if (stopPrice >= entryPrice) {
          entryPrice = snapPriceToTick(stopPrice + step, step);
        }
      } else {
        stopPrice = Math.max(step, snapPriceToTick(entryPrice - step, step));
        if (stopPrice >= entryPrice) {
          entryPrice = snapPriceToTick(stopPrice + step, step);
        }
      }
    }
    if (targetPrice <= entryPrice) {
      targetPrice = snapPriceToTick(entryPrice + step, step);
    }
  } else {
    if (movedRole === "entry") {
      const minEntry = Math.max(step * 2, snapPriceToTick(targetPrice + step, step));
      const maxEntry = Math.max(minEntry, snapPriceToTick(stopPrice - step, step));
      entryPrice = Math.min(Math.max(entryPrice, minEntry), maxEntry);
    }
    if (targetPrice >= entryPrice) {
      targetPrice = Math.max(step, snapPriceToTick(entryPrice - step, step));
      if (targetPrice >= entryPrice) {
        entryPrice = snapPriceToTick(targetPrice + step, step);
      }
    }
    if (stopPrice <= entryPrice) {
      stopPrice = snapPriceToTick(entryPrice + step, step);
    }
  }

  return [
    { ...entry, role: "entry", price: entryPrice },
    { ...target, role: "target", price: targetPrice },
    { ...stop, role: "stop", price: stopPrice },
  ];
}

function pointToSegmentDistance(
  px: number,
  py: number,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  if (lenSq <= 1e-6) {
    return Math.hypot(px - x1, py - y1);
  }
  const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / lenSq));
  const projX = x1 + t * dx;
  const projY = y1 + t * dy;
  return Math.hypot(px - projX, py - projY);
}

function pointToRayDistance(
  px: number,
  py: number,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  if (lenSq <= 1e-6) {
    return Math.hypot(px - x1, py - y1);
  }
  const t = Math.max(0, ((px - x1) * dx + (py - y1) * dy) / lenSq);
  const projX = x1 + t * dx;
  const projY = y1 + t * dy;
  return Math.hypot(px - projX, py - projY);
}

export function hitTestAnchorHandle(
  px: number,
  py: number,
  anchors: readonly ProjectedAnchor[],
  radiusPx = 8,
): ProjectedAnchor | null {
  let best: ProjectedAnchor | null = null;
  let bestDist = radiusPx;
  for (const anchor of anchors) {
    const dist = Math.hypot(px - anchor.x, py - anchor.y);
    if (dist <= bestDist) {
      bestDist = dist;
      best = anchor;
    }
  }
  return best;
}

export interface ProjectedDrawingGeometry {
  drawing: DrawingRecord;
  anchors: ProjectedAnchor[];
}

function hitTestDrawingBody(
  px: number,
  py: number,
  item: ProjectedDrawingGeometry,
  viewportWidth: number,
  tolerancePx: number,
): boolean {
  const { drawing, anchors } = item;
  if (anchors.length === 0) {
    return false;
  }

  if (drawing.type === "hline") {
    const level = anchors[0]!;
    return px >= 0 && px < viewportWidth && Math.abs(py - level.y) <= tolerancePx;
  }

  if (drawing.type === "trendline") {
    const start = anchors[0];
    const end = anchors[1];
    if (start === undefined || end === undefined) {
      return false;
    }
    return pointToSegmentDistance(px, py, start.x, start.y, end.x, end.y) <= tolerancePx;
  }

  if (drawing.type === "ray") {
    const start = anchors[0];
    const end = anchors[1];
    if (start === undefined || end === undefined) {
      return false;
    }
    return pointToRayDistance(px, py, start.x, start.y, end.x, end.y) <= tolerancePx;
  }

  if (drawing.type === "fvg_box" || drawing.type === "order_block" || drawing.type === "measure") {
    const start = anchors[0];
    const end = anchors[1];
    if (start === undefined || end === undefined) {
      return false;
    }
    const extendRight =
      (drawing.type === "order_block" && drawing.metadata?.extend_right !== false) ||
      (drawing.type === "fvg_box" && drawing.metadata?.extend_right === true);
    const minX = Math.min(start.x, end.x);
    const maxX = extendRight ? viewportWidth : Math.max(start.x, end.x);
    const minY = Math.min(start.y, end.y);
    const maxY = Math.max(start.y, end.y);
    return (
      px >= minX - tolerancePx &&
      px <= maxX + tolerancePx &&
      py >= minY - tolerancePx &&
      py <= maxY + tolerancePx
    );
  }

  if (drawing.type === "long_position" || drawing.type === "short_position") {
    const xs = anchors.map((a) => a.x);
    const ys = anchors.map((a) => a.y);
    const minX = Math.min(...xs);
    const rawMaxX = Math.max(...xs);
    const maxX = rawMaxX - minX < 24 ? minX + 80 : rawMaxX;
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    return (
      px >= minX - tolerancePx &&
      px <= maxX + tolerancePx &&
      py >= minY - tolerancePx &&
      py <= maxY + tolerancePx
    );
  }

  return false;
}

/**
 * Hit-test drawings in CSS pixel space bounded strictly to the chart plot area.
 * Anchor handles strictly take priority over line/box bodies; selected drawing handles
 * are checked first, followed by all other handles, then drawing bodies top-to-bottom.
 */
export function hitTestDrawing(
  px: number,
  py: number,
  projectedDrawings: readonly ProjectedDrawingGeometry[],
  selectedDrawingId: string | null,
  viewportWidth: number,
  options?: { handleRadiusPx?: number; bodyTolerancePx?: number; viewportHeight?: number },
): DrawingHitResult | null {
  if (
    !Number.isFinite(px) ||
    !Number.isFinite(py) ||
    px < 0 ||
    py < 0 ||
    px >= viewportWidth ||
    (options?.viewportHeight !== undefined && py >= options.viewportHeight)
  ) {
    return null;
  }
  const handleRadiusPx = options?.handleRadiusPx ?? 8;
  const bodyTolerancePx = options?.bodyTolerancePx ?? 6;

  if (selectedDrawingId !== null) {
    const selectedItem = projectedDrawings.find((d) => d.drawing.id === selectedDrawingId);
    if (selectedItem !== undefined) {
      const hitHandle = hitTestAnchorHandle(px, py, selectedItem.anchors, handleRadiusPx);
      if (hitHandle !== null) {
        return { drawingId: selectedItem.drawing.id, anchorRole: hitHandle.role };
      }
    }
  }

  for (let i = projectedDrawings.length - 1; i >= 0; i -= 1) {
    const item = projectedDrawings[i]!;
    const hitHandle = hitTestAnchorHandle(px, py, item.anchors, handleRadiusPx);
    if (hitHandle !== null) {
      return { drawingId: item.drawing.id, anchorRole: hitHandle.role };
    }
  }

  for (let i = projectedDrawings.length - 1; i >= 0; i -= 1) {
    const item = projectedDrawings[i]!;
    if (hitTestDrawingBody(px, py, item, viewportWidth, bodyTolerancePx)) {
      return { drawingId: item.drawing.id, anchorRole: null };
    }
  }

  return null;
}
