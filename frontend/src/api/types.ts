export type RootSymbol = "MES" | "MNQ" | "MYM" | "MGC";

export type Timeframe = "1m" | "3m" | "5m" | "15m" | "30m" | "1h" | "4h" | "1d";

export type PlaybackMode = "live" | "replay";

export type LayoutTemplateId =
  | "1-single"
  | "2-cols"
  | "2-rows"
  | "3-cols"
  | "3-rows"
  | "3-left-main"
  | "3-right-main"
  | "3-top-main"
  | "3-bottom-main"
  | "4-grid-2x2"
  | "4-cols"
  | "4-rows"
  | "4-left-main"
  | "4-top-main"
  | "5-top2-bot3"
  | "5-left-main-4"
  | "6-grid-3x2"
  | "6-grid-2x3"
  | "7-top3-bot4"
  | "8-grid-4x2"
  | "8-grid-2x4";

export const ROOT_SYMBOLS: readonly RootSymbol[] = ["MES", "MNQ", "MYM", "MGC"];

export const TIMEFRAMES: readonly Timeframe[] = [
  "1m",
  "3m",
  "5m",
  "15m",
  "30m",
  "1h",
  "4h",
  "1d",
];

export const LIVE_WS_TIMEFRAMES: readonly Timeframe[] = [
  "1m",
  "5m",
  "15m",
  "1h",
  "4h",
  "1d",
];

export function isLiveWsTimeframe(tf: Timeframe): boolean {
  return LIVE_WS_TIMEFRAMES.includes(tf);
}

export const LAYOUT_TEMPLATE_PANE_COUNTS: Record<LayoutTemplateId, number> = {
  "1-single": 1,
  "2-cols": 2,
  "2-rows": 2,
  "3-cols": 3,
  "3-rows": 3,
  "3-left-main": 3,
  "3-right-main": 3,
  "3-top-main": 3,
  "3-bottom-main": 3,
  "4-grid-2x2": 4,
  "4-cols": 4,
  "4-rows": 4,
  "4-left-main": 4,
  "4-top-main": 4,
  "5-top2-bot3": 5,
  "5-left-main-4": 5,
  "6-grid-3x2": 6,
  "6-grid-2x3": 6,
  "7-top3-bot4": 7,
  "8-grid-4x2": 8,
  "8-grid-2x4": 8,
};

export interface SyncConfig {
  symbol: boolean;
  interval: boolean;
  crosshair: boolean;
  time: boolean;
  dateRange: boolean;
}

export interface PaneConfig {
  pane_id: string;
  root_symbol: RootSymbol;
  timeframe: Timeframe;
  live_mode: boolean;
  playback_mode: PlaybackMode;
}

export interface LayoutRecord {
  id: string;
  name: string;
  template_id: LayoutTemplateId;
  panes: PaneConfig[];
  sync_config: SyncConfig;
  is_default: boolean;
  created_at_utc: number;
  updated_at_utc: number;
}

export interface LayoutPayload {
  name: string;
  template_id: LayoutTemplateId;
  panes: PaneConfig[];
  sync_config: SyncConfig;
  is_default?: boolean;
}

export interface OhlcvBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  is_closed: boolean;
}

export interface BarsQuality {
  is_complete: boolean;
  truncated: boolean;
  provider_gap: boolean;
  quarantined_count: number;
}

export interface BarsResponse {
  provider: string;
  live: boolean;
  contract_id: string;
  root_symbol: string | null;
  timeframe: Timeframe;
  start_time: number;
  end_time: number;
  count: number;
  include_partial: boolean;
  quality: BarsQuality;
  bars: OhlcvBar[];
}

export interface ContractInfo {
  id?: string;
  contractId?: string;
  name?: string;
  symbolId?: string;
  description?: string;
  activeContract?: boolean;
}

export interface ContractResolutionEntry {
  selected: ContractInfo | null;
  error_type: string | null;
  error: string | null;
}

export interface ContractsResponse {
  live: boolean;
  contracts: Record<string, ContractResolutionEntry>;
}

export interface StatusResponse {
  connected?: boolean;
  live?: boolean;
  feed_state?: string;
  key_age_days?: number | null;
  key_rotation_days?: number;
  key_rotation_due?: boolean;
  [key: string]: unknown;
}

export interface WsBarUpdateMessage {
  type: "bar_update";
  contract_id: string;
  live_mode: boolean;
  timeframe: Timeframe;
  is_recovered_historical: boolean;
  recovery_tag: string | null;
  bar: OhlcvBar;
}

export interface WsFeedStatusMessage {
  type: "feed_status";
  feed_state?: string;
  [key: string]: unknown;
}

export interface WsResyncRequiredMessage {
  type: "resync_required";
  reason: string;
  contract_id: string | null;
}

export interface WsSubscribedMessage {
  type: "subscribed" | "unsubscribed";
  contract_id: string;
  timeframe: Timeframe;
}

export interface WsErrorMessage {
  type: "error";
  detail: string;
}

export interface WsPongMessage {
  type: "pong";
}

export type PersistedDrawingType =
  | "trendline"
  | "ray"
  | "hline"
  | "fvg_box"
  | "order_block"
  | "long_position"
  | "short_position"
  | "measure";

export type DrawingActor = "user" | "agent";

export interface DrawingAnchor {
  role: string;
  time: number;
  price: number;
}

export interface DrawingCoordinates {
  anchors: DrawingAnchor[];
}

export interface DrawingRecord {
  id: string;
  revision: number;
  type: PersistedDrawingType;
  symbol: RootSymbol;
  contract_id: string | null;
  pane_id: string | null;
  created_by: DrawingActor;
  source_bar_refs: number[];
  coordinates: DrawingCoordinates;
  style: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at_utc: number;
  updated_at_utc: number;
}

export interface DrawingInstrumentSpec {
  root_symbol: RootSymbol;
  name: string;
  exchange: string;
  tick_size: number;
  tick_value: number;
  currency: string;
}

export interface DrawingScopeResponse {
  symbol: RootSymbol;
  contract_id: string | null;
  pane_id: string | null;
  instrument: DrawingInstrumentSpec;
  scope_revision: number;
  can_undo: boolean;
  can_redo: boolean;
  drawings: DrawingRecord[];
}

export interface DrawingCommandResponse extends DrawingScopeResponse {
  deduplicated: boolean;
  affected_ids: string[];
}

export interface DrawingCreateInput {
  id?: string;
  type: PersistedDrawingType;
  coordinates: DrawingCoordinates;
  created_by?: DrawingActor;
  source_bar_refs?: number[];
  style?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

export interface DrawingUpdateInput {
  id?: string;
  type?: PersistedDrawingType;
  coordinates?: DrawingCoordinates;
  source_bar_refs?: number[];
  style?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

export type DrawingBatchOperation =
  | {
      action: "create";
      drawing: DrawingCreateInput;
      created_by?: DrawingActor;
    }
  | {
      action: "update";
      drawing_id: string;
      drawing: DrawingUpdateInput;
      created_by?: DrawingActor;
    }
  | {
      action: "delete";
      drawing_id: string;
      created_by?: DrawingActor;
    };

export interface DrawingCommandEnvelopeBase {
  expected_revision: number;
  idempotency_key?: string;
  contract_id?: string | null;
  pane_id?: string | null;
  created_by?: DrawingActor;
}

export type DrawingCommandRequest =
  | (DrawingCommandEnvelopeBase & {
      action: "create";
      drawing: DrawingCreateInput;
    })
  | (DrawingCommandEnvelopeBase & {
      action: "update";
      drawing_id: string;
      drawing: DrawingUpdateInput;
    })
  | (DrawingCommandEnvelopeBase & {
      action: "delete";
      drawing_id: string;
    })
  | (DrawingCommandEnvelopeBase & {
      action: "clear";
    })
  | (DrawingCommandEnvelopeBase & {
      action: "batch";
      operations: DrawingBatchOperation[];
    });

export interface UndoRedoRequest {
  expected_revision: number;
  idempotency_key?: string;
  contract_id?: string | null;
  pane_id?: string | null;
  actor?: DrawingActor;
}

export interface WsDrawingsUpdateMessage {
  type: "drawings_update";
  symbol: RootSymbol;
  contract_id: string | null;
  pane_id: string | null;
  scope_revision: number;
  command_type: string;
  can_undo: boolean;
  can_redo: boolean;
  drawings: DrawingRecord[];
}

export type WsInboundMessage =
  | WsBarUpdateMessage
  | WsFeedStatusMessage
  | WsResyncRequiredMessage
  | WsSubscribedMessage
  | WsDrawingsUpdateMessage
  | WsErrorMessage
  | WsPongMessage;
