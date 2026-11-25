import type { UTCTimestamp } from "lightweight-charts";
import { describe, expect, it, vi } from "vitest";
import type { SyncConfig } from "../api/types";
import { LayoutSyncController, type ChartPaneSyncHandle } from "./syncController";

function makeSyncConfig(overrides: Partial<SyncConfig> = {}): SyncConfig {
  return {
    symbol: false,
    interval: false,
    crosshair: false,
    time: false,
    dateRange: false,
    ...overrides,
  };
}

function makeHandle(
  paneId: string,
  hooks?: Partial<Omit<ChartPaneSyncHandle, "paneId">>,
): ChartPaneSyncHandle & {
  applyCrosshairTime: ReturnType<typeof vi.fn>;
  applyVisibleTimeRange: ReturnType<typeof vi.fn>;
  centerOnTime: ReturnType<typeof vi.fn>;
} {
  return {
    paneId,
    applyCrosshairTime: vi.fn(hooks?.applyCrosshairTime),
    applyVisibleTimeRange: vi.fn(hooks?.applyVisibleTimeRange),
    centerOnTime: vi.fn(hooks?.centerOnTime),
  };
}

describe("LayoutSyncController (Phase 3 Part 2)", () => {
  it("propagates crosshair only to other panes when crosshair sync is enabled and supports null clear", () => {
    /**
     * Contract: When syncConfig.crosshair is true, onCrosshairMove calls applyCrosshairTime on
     * every registered pane except sourcePaneId, including null to clear; when false, no calls occur.
     * Defect caught: Crosshair echoing back to source pane or leaking when toggle is disabled.
     */
    const controller = new LayoutSyncController(makeSyncConfig({ crosshair: false }));
    const p1 = makeHandle("pane-1");
    const p2 = makeHandle("pane-2");
    const p3 = makeHandle("pane-3");
    controller.registerPane(p1);
    controller.registerPane(p2);
    controller.registerPane(p3);

    controller.onCrosshairMove("pane-1", 1700000300 as UTCTimestamp);
    expect(p1.applyCrosshairTime).not.toHaveBeenCalled();
    expect(p2.applyCrosshairTime).not.toHaveBeenCalled();
    expect(p3.applyCrosshairTime).not.toHaveBeenCalled();

    controller.setSyncConfig(makeSyncConfig({ crosshair: true }));
    controller.onCrosshairMove("pane-1", 1700000300 as UTCTimestamp);
    expect(p1.applyCrosshairTime).not.toHaveBeenCalled();
    expect(p2.applyCrosshairTime).toHaveBeenCalledWith(1700000300);
    expect(p3.applyCrosshairTime).toHaveBeenCalledWith(1700000300);

    controller.onCrosshairMove("pane-1", null);
    expect(p2.applyCrosshairTime).toHaveBeenLastCalledWith(null);
    expect(p3.applyCrosshairTime).toHaveBeenLastCalledWith(null);
  });

  it("propagates visible time range and time click independently based on toggles", () => {
    /**
     * Contract: dateRange toggle governs onVisibleTimeRangeChange and time toggle governs onTimeClick,
     * keeping the two mechanisms independent across panes.
     * Defect caught: Cross-coupling time and dateRange toggles or ignoring null arguments.
     */
    const controller = new LayoutSyncController(makeSyncConfig({ dateRange: true, time: false }));
    const p1 = makeHandle("pane-1");
    const p2 = makeHandle("pane-2");
    controller.registerPane(p1);
    controller.registerPane(p2);

    const range = {
      from: 1700000000 as UTCTimestamp,
      to: 1700003600 as UTCTimestamp,
    };
    controller.onVisibleTimeRangeChange("pane-1", range);
    controller.onTimeClick("pane-1", 1700001800 as UTCTimestamp);
    expect(p2.applyVisibleTimeRange).toHaveBeenCalledWith(range);
    expect(p2.centerOnTime).not.toHaveBeenCalled();

    controller.setSyncConfig(makeSyncConfig({ dateRange: false, time: true }));
    controller.onVisibleTimeRangeChange("pane-1", range);
    controller.onTimeClick("pane-1", 1700001800 as UTCTimestamp);
    expect(p2.applyVisibleTimeRange).toHaveBeenCalledTimes(1);
    expect(p2.centerOnTime).toHaveBeenCalledWith(1700001800);
  });

  it("blocks synchronous re-entrancy loops for crosshair, dateRange, and time->range cascades", () => {
    /**
     * Contract: Synchronous re-entrant calls fired from inside applyCrosshairTime,
     * applyVisibleTimeRange, or centerOnTime (including centerOnTime -> onVisibleTimeRangeChange)
     * are ignored by the active source guards, and try/finally releases guards even after exceptions.
     * Defect caught: Infinite ping-pong event loops across synchronized Lightweight Charts instances.
     */
    const controller = new LayoutSyncController(
      makeSyncConfig({ crosshair: true, dateRange: true, time: true }),
    );

    const p1 = makeHandle("pane-1");
    const p2 = makeHandle("pane-2", {
      applyCrosshairTime: (time) => {
        controller.onCrosshairMove("pane-2", time);
      },
      applyVisibleTimeRange: (range) => {
        controller.onVisibleTimeRangeChange("pane-2", range);
      },
      centerOnTime: (time) => {
        controller.onTimeClick("pane-2", time);
        controller.onVisibleTimeRangeChange("pane-2", {
          from: (Number(time) - 600) as UTCTimestamp,
          to: (Number(time) + 600) as UTCTimestamp,
        });
      },
    });
    const p3 = makeHandle("pane-3");

    controller.registerPane(p1);
    controller.registerPane(p2);
    controller.registerPane(p3);

    controller.onCrosshairMove("pane-1", 1700000300 as UTCTimestamp);
    expect(p1.applyCrosshairTime).not.toHaveBeenCalled();
    expect(p2.applyCrosshairTime).toHaveBeenCalledTimes(1);
    expect(p3.applyCrosshairTime).toHaveBeenCalledTimes(1);

    controller.onVisibleTimeRangeChange("pane-1", {
      from: 1700000000 as UTCTimestamp,
      to: 1700003600 as UTCTimestamp,
    });
    expect(p1.applyVisibleTimeRange).not.toHaveBeenCalled();
    expect(p2.applyVisibleTimeRange).toHaveBeenCalledTimes(1);
    expect(p3.applyVisibleTimeRange).toHaveBeenCalledTimes(1);

    controller.onTimeClick("pane-1", 1700001800 as UTCTimestamp);
    expect(p1.centerOnTime).not.toHaveBeenCalled();
    expect(p2.centerOnTime).toHaveBeenCalledTimes(1);
    expect(p3.centerOnTime).toHaveBeenCalledTimes(1);
    // centerOnTime inside p2 also triggered onVisibleTimeRangeChange, which MUST be suppressed!
    expect(p1.applyVisibleTimeRange).not.toHaveBeenCalled();
    expect(p3.applyVisibleTimeRange).toHaveBeenCalledTimes(1);
  });

  it("releases synchronous guards in finally after callback errors and unregisters cleanly", () => {
    /**
     * Contract: If a target pane throws during sync propagation, the active guard still resets
     * in finally so future sync events work; unregistering only removes the matching handle.
     * Defect caught: Permanently stuck sync guard after a transient chart exception or stale unregister.
     */
    const controller = new LayoutSyncController(makeSyncConfig({ crosshair: true }));
    const p1 = makeHandle("pane-1");
    const p2Broken = makeHandle("pane-2", {
      applyCrosshairTime: () => {
        throw new Error("Chart disposed");
      },
    });
    const p2Replacement = makeHandle("pane-2");

    controller.registerPane(p1);
    const unregisterOldP2 = controller.registerPane(p2Broken);

    expect(() =>
      controller.onCrosshairMove("pane-1", 1700000300 as UTCTimestamp),
    ).toThrow("Chart disposed");

    // Replace pane-2 before old unregister runs (rapid remount scenario)
    const unregisterNewP2 = controller.registerPane(p2Replacement);
    unregisterOldP2();

    controller.onCrosshairMove("pane-1", 1700000600 as UTCTimestamp);
    expect(p2Replacement.applyCrosshairTime).toHaveBeenCalledWith(1700000600);

    unregisterNewP2();
    controller.onCrosshairMove("pane-1", 1700000900 as UTCTimestamp);
    expect(p2Replacement.applyCrosshairTime).toHaveBeenCalledTimes(1);
  });
});
