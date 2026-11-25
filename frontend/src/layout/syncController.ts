import type { UTCTimestamp } from "lightweight-charts";
import type { SyncConfig } from "../api/types";

export interface VisibleTimeRange {
  from: UTCTimestamp;
  to: UTCTimestamp;
}

export interface ChartPaneSyncHandle {
  paneId: string;
  applyCrosshairTime(time: UTCTimestamp | null): void;
  applyVisibleTimeRange(range: VisibleTimeRange): void;
  centerOnTime(time: UTCTimestamp): void;
}

const DEFAULT_SYNC_CONFIG: SyncConfig = {
  symbol: false,
  interval: false,
  crosshair: false,
  time: false,
  dateRange: false,
};

/**
 * Coordinates imperative Crosshair, Time, and Date range synchronization across
 * chart panes using synchronous try/finally source guards so propagated updates
 * never bounce back into ping-pong feedback loops.
 */
export class LayoutSyncController {
  private syncConfig: SyncConfig;
  private readonly panes = new Map<string, ChartPaneSyncHandle>();
  private activeCrosshairSource: string | null = null;
  private activeRangeSource: string | null = null;
  private activeTimeSource: string | null = null;

  constructor(initialConfig: SyncConfig = DEFAULT_SYNC_CONFIG) {
    this.syncConfig = { ...initialConfig };
  }

  setSyncConfig(config: SyncConfig): void {
    this.syncConfig = { ...config };
  }

  getSyncConfig(): SyncConfig {
    return { ...this.syncConfig };
  }

  registerPane(handle: ChartPaneSyncHandle): () => void {
    this.panes.set(handle.paneId, handle);
    return () => {
      if (this.panes.get(handle.paneId) === handle) {
        this.panes.delete(handle.paneId);
      }
    };
  }

  onCrosshairMove(sourcePaneId: string, time: UTCTimestamp | null): void {
    if (!this.syncConfig.crosshair || this.activeCrosshairSource !== null) {
      return;
    }
    this.activeCrosshairSource = sourcePaneId;
    try {
      for (const [paneId, handle] of this.panes) {
        if (paneId !== sourcePaneId) {
          handle.applyCrosshairTime(time);
        }
      }
    } finally {
      this.activeCrosshairSource = null;
    }
  }

  onVisibleTimeRangeChange(sourcePaneId: string, range: VisibleTimeRange | null): void {
    if (
      !this.syncConfig.dateRange ||
      range === null ||
      this.activeRangeSource !== null ||
      this.activeTimeSource !== null
    ) {
      return;
    }
    this.activeRangeSource = sourcePaneId;
    try {
      for (const [paneId, handle] of this.panes) {
        if (paneId !== sourcePaneId) {
          handle.applyVisibleTimeRange(range);
        }
      }
    } finally {
      this.activeRangeSource = null;
    }
  }

  onTimeClick(sourcePaneId: string, time: UTCTimestamp | null): void {
    if (!this.syncConfig.time || time === null || this.activeTimeSource !== null) {
      return;
    }
    this.activeTimeSource = sourcePaneId;
    try {
      for (const [paneId, handle] of this.panes) {
        if (paneId !== sourcePaneId) {
          handle.centerOnTime(time);
        }
      }
    } finally {
      this.activeTimeSource = null;
    }
  }
}
