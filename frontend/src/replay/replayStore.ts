import { create } from "zustand";
import type { OhlcvBar, RootSymbol, Timeframe } from "../api/types";
import {
  createReplaySession,
  flattenPaperPosition,
  openPaperPosition,
  stepReplayBack,
  stepReplayForward,
  type OpenPaperPositionInput,
  type ReplaySession,
  type ReplaySpeed,
} from "./replayEngine";

export interface ReplayTarget {
  paneId: string;
  contractId: string;
  rootSymbol: RootSymbol;
  timeframe: Timeframe;
  liveMode: boolean;
}

export interface ReplayStoreState {
  isOpen: boolean;
  pickStartArmed: boolean;
  isPlaying: boolean;
  speed: ReplaySpeed;
  target: ReplayTarget | null;
  barsByPane: Record<string, OhlcvBar[]>;
  session: ReplaySession | null;
  error: string | null;

  togglePanel: () => void;
  armPickStart: () => void;
  cancelPickStart: () => void;
  registerPaneBars: (
    paneId: string,
    bars: readonly OhlcvBar[],
    targetMeta?: ReplayTarget,
  ) => void;
  syncPaneTarget: (target: ReplayTarget) => void;
  unregisterPaneBars: (paneId: string) => void;
  startReplayAtBar: (
    target: ReplayTarget,
    bars: readonly OhlcvBar[],
    startIndex: number,
  ) => void;
  startReplayAtTime: (target: ReplayTarget, clickTime: number) => void;
  play: () => void;
  pause: () => void;
  setSpeed: (speed: ReplaySpeed) => void;
  stepForward: () => void;
  stepBack: () => void;
  openPaperTrade: (input: OpenPaperPositionInput) => void;
  flattenPaperTrade: () => void;
  exitReplay: () => void;
  clearError: () => void;
  reset: () => void;
}

let playbackTimerId: number | null = null;

function clearPlaybackTimer(): void {
  if (playbackTimerId !== null) {
    window.clearInterval(playbackTimerId);
    playbackTimerId = null;
  }
}

function playbackIntervalMs(speed: ReplaySpeed): number {
  return Math.max(100, Math.round(1000 / speed));
}

function targetsEqual(a: ReplayTarget, b: ReplayTarget): boolean {
  return (
    a.paneId === b.paneId &&
    a.contractId.trim().toUpperCase() === b.contractId.trim().toUpperCase() &&
    a.rootSymbol === b.rootSymbol &&
    a.timeframe === b.timeframe &&
    a.liveMode === b.liveMode
  );
}

function barsEqual(a: OhlcvBar, b: OhlcvBar): boolean {
  return (
    a.time === b.time &&
    a.open === b.open &&
    a.high === b.high &&
    a.low === b.low &&
    a.close === b.close &&
    a.volume === b.volume &&
    a.is_closed === b.is_closed
  );
}

function findStartBarIndex(bars: readonly OhlcvBar[], clickTime: number): number {
  if (bars.length === 0) {
    return 0;
  }
  let lo = 0;
  let hi = bars.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid]!.time < clickTime) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  if (lo > 0) {
    const leftDiff = Math.abs(bars[lo - 1]!.time - clickTime);
    const rightDiff = Math.abs(bars[lo]!.time - clickTime);
    return leftDiff <= rightDiff ? lo - 1 : lo;
  }
  return lo;
}

export const useReplayStore = create<ReplayStoreState>((set, get) => {
  const startTimer = (speed: ReplaySpeed) => {
    clearPlaybackTimer();
    playbackTimerId = window.setInterval(() => {
      const current = get();
      if (!current.isPlaying || current.session === null) {
        clearPlaybackTimer();
        return;
      }
      if (current.session.cursorIndex >= current.session.bars.length - 1) {
        clearPlaybackTimer();
        set({ isPlaying: false });
        return;
      }
      const nextSession = stepReplayForward(current.session);
      const reachedEnd = nextSession.cursorIndex >= nextSession.bars.length - 1;
      if (reachedEnd) {
        clearPlaybackTimer();
      }
      set({
        session: nextSession,
        isPlaying: !reachedEnd,
        error: null,
      });
    }, playbackIntervalMs(speed));
  };

  return {
    isOpen: false,
    pickStartArmed: false,
    isPlaying: false,
    speed: 1,
    target: null,
    barsByPane: {},
    session: null,
    error: null,

    togglePanel: () => {
      set((state) => ({
        isOpen: !state.isOpen,
        pickStartArmed: state.isOpen ? false : state.pickStartArmed,
        error: null,
      }));
    },

    armPickStart: () => {
      clearPlaybackTimer();
      set({
        isOpen: true,
        pickStartArmed: true,
        isPlaying: false,
        error: null,
      });
    },

    cancelPickStart: () => {
      set({ pickStartArmed: false });
    },

    registerPaneBars: (paneId, bars, targetMeta) => {
      const copied = bars.map((bar) => ({ ...bar }));
      set((state) => {
        const nextBarsByPane = {
          ...state.barsByPane,
          [paneId]: copied,
        };
        if (state.session !== null && state.target !== null && state.target.paneId === paneId) {
          if (targetMeta !== undefined && !targetsEqual(state.target, targetMeta)) {
            clearPlaybackTimer();
            return {
              barsByPane: nextBarsByPane,
              isPlaying: false,
              pickStartArmed: false,
              target: null,
              session: null,
              error: null,
            };
          }
          const sessionBars = state.session.bars;
          if (
            copied.length > sessionBars.length &&
            state.session.cursorIndex < copied.length
          ) {
            const prefixMatches = sessionBars.every((bar, idx) => {
              const incoming = copied[idx];
              return incoming !== undefined && barsEqual(incoming, bar);
            });
            const lastSessionBar = sessionBars[sessionBars.length - 1];
            if (prefixMatches && lastSessionBar !== undefined) {
              let prevTime = lastSessionBar.time;
              const appended: OhlcvBar[] = [];
              for (const candidate of copied.slice(sessionBars.length)) {
                if (candidate.time > prevTime) {
                  appended.push(candidate);
                  prevTime = candidate.time;
                }
              }
              if (appended.length > 0) {
                return {
                  barsByPane: nextBarsByPane,
                  session: {
                    ...state.session,
                    bars: [...sessionBars, ...appended],
                  },
                };
              }
            }
          }
        }
        return { barsByPane: nextBarsByPane };
      });
    },

    syncPaneTarget: (target) => {
      const state = get();
      if (
        state.target !== null &&
        state.target.paneId === target.paneId &&
        !targetsEqual(state.target, target)
      ) {
        clearPlaybackTimer();
        set({
          isPlaying: false,
          pickStartArmed: false,
          target: null,
          session: null,
          error: null,
        });
      }
    },

    unregisterPaneBars: (paneId) => {
      const state = get();
      const remaining = { ...state.barsByPane };
      delete remaining[paneId];
      if (state.target?.paneId === paneId) {
        clearPlaybackTimer();
        set({
          barsByPane: remaining,
          isPlaying: false,
          pickStartArmed: false,
          target: null,
          session: null,
        });
        return;
      }
      set({ barsByPane: remaining });
    },

    startReplayAtBar: (target, bars, startIndex) => {
      clearPlaybackTimer();
      if (bars.length === 0) {
        set({
          pickStartArmed: false,
          isPlaying: false,
          error: "Load bars on the target pane before starting replay",
        });
        return;
      }
      const session = createReplaySession(bars, target.rootSymbol, startIndex);
      set({
        isOpen: true,
        pickStartArmed: false,
        isPlaying: false,
        target: { ...target },
        session,
        error: null,
      });
    },

    startReplayAtTime: (target, clickTime) => {
      const bars = get().barsByPane[target.paneId] ?? [];
      if (bars.length === 0) {
        set({
          pickStartArmed: false,
          error: "No bars loaded for selected pane",
        });
        return;
      }
      const idx = findStartBarIndex(bars, clickTime);
      get().startReplayAtBar(target, bars, idx);
    },

    play: () => {
      const state = get();
      if (state.session === null) {
        return;
      }
      if (state.session.cursorIndex >= state.session.bars.length - 1) {
        clearPlaybackTimer();
        set({ isPlaying: false });
        return;
      }
      set({ isPlaying: true, error: null });
      startTimer(state.speed);
    },

    pause: () => {
      clearPlaybackTimer();
      set({ isPlaying: false });
    },

    setSpeed: (speed) => {
      const wasPlaying = get().isPlaying;
      set({ speed });
      if (wasPlaying && get().session !== null) {
        startTimer(speed);
      }
    },

    stepForward: () => {
      const state = get();
      if (state.session === null) {
        return;
      }
      const nextSession = stepReplayForward(state.session);
      const atEnd = nextSession.cursorIndex >= nextSession.bars.length - 1;
      if (atEnd) {
        clearPlaybackTimer();
      }
      set({
        session: nextSession,
        isPlaying: atEnd ? false : state.isPlaying,
        error: null,
      });
    },

    stepBack: () => {
      const state = get();
      if (state.session === null) {
        return;
      }
      clearPlaybackTimer();
      try {
        const nextSession = stepReplayBack(state.session);
        set({
          session: nextSession,
          isPlaying: false,
          error: null,
        });
      } catch (err: unknown) {
        set({
          isPlaying: false,
          error: err instanceof Error ? err.message : "Failed to step replay back",
        });
      }
    },

    openPaperTrade: (input) => {
      const state = get();
      if (state.session === null) {
        set({ error: "Start replay mode before submitting a paper trade" });
        return;
      }
      try {
        const nextSession = openPaperPosition(state.session, input);
        set({ session: nextSession, error: null });
      } catch (err: unknown) {
        set({
          error: err instanceof Error ? err.message : "Unable to open paper position",
        });
      }
    },

    flattenPaperTrade: () => {
      const state = get();
      if (state.session === null) {
        set({ error: "No active replay session" });
        return;
      }
      try {
        const nextSession = flattenPaperPosition(state.session);
        set({ session: nextSession, error: null });
      } catch (err: unknown) {
        set({
          error: err instanceof Error ? err.message : "Unable to flatten paper position",
        });
      }
    },

    exitReplay: () => {
      clearPlaybackTimer();
      set({
        isPlaying: false,
        pickStartArmed: false,
        target: null,
        session: null,
        error: null,
      });
    },

    clearError: () => {
      set({ error: null });
    },

    reset: () => {
      clearPlaybackTimer();
      set({
        isOpen: false,
        pickStartArmed: false,
        isPlaying: false,
        speed: 1,
        target: null,
        barsByPane: {},
        session: null,
        error: null,
      });
    },
  };
});
