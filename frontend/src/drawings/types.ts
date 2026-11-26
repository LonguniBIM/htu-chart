import type {
  DrawingActor,
  DrawingAnchor,
  DrawingCoordinates,
  DrawingRecord,
  PersistedDrawingType,
  RootSymbol,
} from "../api/types";

export type DrawingToolId = "cursor" | PersistedDrawingType;

export type FvgPreset = "auto" | "bullish" | "bearish";

export interface InstrumentDrawingSpec {
  tickSize: number;
  tickValue: number;
}

/**
 * CME / TopstepX instrument tick geometry mirroring backend/app/engine/instruments.py.
 */
export const INSTRUMENT_SPECS: Record<RootSymbol, InstrumentDrawingSpec> = {
  MES: { tickSize: 0.25, tickValue: 1.25 },
  MNQ: { tickSize: 0.25, tickValue: 0.5 },
  MYM: { tickSize: 1.0, tickValue: 0.5 },
  MGC: { tickSize: 0.1, tickValue: 1.0 },
};

export interface DrawingPoint {
  time: number;
  price: number;
}

export interface DrawingDraft {
  tool: Exclude<DrawingToolId, "cursor">;
  anchors: DrawingAnchor[];
  fvgPreset?: FvgPreset;
  extendRight?: boolean;
}

export interface PositionMetrics {
  riskPrice: number;
  rewardPrice: number;
  riskTicks: number;
  rewardTicks: number;
  dollarRisk: number;
  dollarReward: number;
  rrRatio: number;
}

export interface MeasureMetrics {
  priceDelta: number;
  pctChange: number;
  ticks: number;
  barCount: number;
  durationSec: number;
  durationLabel: string;
}

export interface ProjectedAnchor {
  role: string;
  time: number;
  price: number;
  x: number;
  y: number;
}

export interface DrawingHitResult {
  drawingId: string;
  anchorRole: string | null;
}

export type {
  DrawingActor,
  DrawingAnchor,
  DrawingCoordinates,
  DrawingRecord,
  PersistedDrawingType,
};
