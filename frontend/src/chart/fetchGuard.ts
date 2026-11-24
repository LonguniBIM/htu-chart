import type { OhlcvBar, PlaybackMode, Timeframe, WsBarUpdateMessage } from "../api/types";

export const MAX_PENDING_WS_BARS = 500;

export interface PaneFetchTarget {
  pane_id: string;
  contract_id: string;
  timeframe: Timeframe;
  live_mode: boolean;
  playback_mode: PlaybackMode;
}

export interface FetchTicket {
  generation: number;
  signal: AbortSignal;
  target: PaneFetchTarget;
}

interface PaneSlot {
  generation: number;
  controller: AbortController | null;
}

/**
 * F06 stale-response ownership guard: maintains a monotonic requestGeneration
 * and AbortController per pane so rapid symbol/timeframe/mode switches never
 * let late responses overwrite newer pane state.
 */
export class PaneFetchCoordinator {
  private readonly panes = new Map<string, PaneSlot>();

  beginFetch(paneState: PaneFetchTarget): FetchTicket {
    const existing = this.panes.get(paneState.pane_id);
    if (existing?.controller !== null && existing?.controller !== undefined) {
      if (!existing.controller.signal.aborted) {
        existing.controller.abort();
      }
    }

    const nextGeneration = (existing?.generation ?? 0) + 1;
    const controller = new AbortController();
    this.panes.set(paneState.pane_id, {
      generation: nextGeneration,
      controller,
    });

    return {
      generation: nextGeneration,
      signal: controller.signal,
      target: {
        pane_id: paneState.pane_id,
        contract_id: paneState.contract_id,
        timeframe: paneState.timeframe,
        live_mode: paneState.live_mode,
        playback_mode: paneState.playback_mode,
      },
    };
  }

  isResponseCurrent(ticket: FetchTicket, currentPaneState: PaneFetchTarget): boolean {
    if (ticket.signal.aborted) {
      return false;
    }
    const slot = this.panes.get(currentPaneState.pane_id);
    if (slot === undefined || slot.generation !== ticket.generation) {
      return false;
    }
    return (
      ticket.target.pane_id === currentPaneState.pane_id &&
      ticket.target.contract_id === currentPaneState.contract_id &&
      ticket.target.timeframe === currentPaneState.timeframe &&
      ticket.target.live_mode === currentPaneState.live_mode &&
      ticket.target.playback_mode === currentPaneState.playback_mode
    );
  }

  abortPane(paneId: string): void {
    const existing = this.panes.get(paneId);
    if (existing === undefined) {
      return;
    }
    if (existing.controller !== null && !existing.controller.signal.aborted) {
      existing.controller.abort();
    }
    this.panes.set(paneId, {
      generation: existing.generation + 1,
      controller: null,
    });
  }

  dispose(): void {
    for (const paneId of this.panes.keys()) {
      this.abortPane(paneId);
    }
    this.panes.clear();
  }
}

export function buildPaneTargetKey(target: PaneFetchTarget): string {
  return `${target.pane_id}|${target.contract_id}|${target.timeframe}|${target.live_mode}|${target.playback_mode}`;
}

export function matchesWsBarTarget(
  msg: Pick<WsBarUpdateMessage, "contract_id" | "timeframe" | "live_mode">,
  target: PaneFetchTarget,
): boolean {
  if (!target.contract_id) {
    return false;
  }
  return (
    msg.contract_id.toUpperCase() === target.contract_id.toUpperCase() &&
    msg.timeframe === target.timeframe &&
    msg.live_mode === target.live_mode
  );
}

export function appendBoundedWsBar(
  buffer: OhlcvBar[],
  bar: OhlcvBar,
  maxBars: number = MAX_PENDING_WS_BARS,
): { overflowed: boolean } {
  if (maxBars <= 0) {
    return { overflowed: false };
  }
  const existingIndex = buffer.findIndex((entry) => entry.time === bar.time);
  if (existingIndex !== -1) {
    const existing = buffer[existingIndex];
    if (existing?.is_closed === true && !bar.is_closed) {
      return { overflowed: false };
    }
    buffer[existingIndex] = bar;
    return { overflowed: false };
  }
  buffer.push(bar);
  if (buffer.length > maxBars) {
    buffer.shift();
    return { overflowed: true };
  }
  return { overflowed: false };
}

/**
 * Merge REST snapshot bars with buffered WebSocket bar updates collected while
 * fetchBars was in flight. Sorts ascending by `time`, deduplicates by `time`,
 * and lets buffered WS bars overwrite or append when `wsBar.time >= lastRestTime`.
 */
export function reconcileSnapshotWithBufferedBars(
  restBars: readonly OhlcvBar[],
  bufferedBars: readonly OhlcvBar[],
): OhlcvBar[] {
  const byTime = new Map<number, OhlcvBar>();
  let lastRestTime: number | null = null;

  for (const bar of restBars) {
    byTime.set(bar.time, bar);
    if (lastRestTime === null || bar.time > lastRestTime) {
      lastRestTime = bar.time;
    }
  }

  for (const wsBar of bufferedBars) {
    if (lastRestTime === null || wsBar.time >= lastRestTime) {
      const existing = byTime.get(wsBar.time);
      if (existing?.is_closed === true && !wsBar.is_closed) {
        continue;
      }
      byTime.set(wsBar.time, wsBar);
    }
  }

  return Array.from(byTime.values()).sort((a, b) => a.time - b.time);
}
