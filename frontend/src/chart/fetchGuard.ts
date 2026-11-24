import type { PlaybackMode, Timeframe } from "../api/types";

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
