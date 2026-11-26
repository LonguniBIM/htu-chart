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
  registerPaneBars: (paneId: string, bars: readonly OhlcvBar[]) => void;
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

    registerPaneBars: (paneId, bars) => {
      const copied = [...bars];
      set((state) => {
        const nextBarsByPane = {
          ...state.barsByPane,
          [paneId]: copied,
        };
        if (
          state.session !== null &&
          state.target?.paneId === paneId &&
          copied.length > state.session.bars.length &&
          state.session.cursorIndex < copied.length
        ) {
          const prefixMatches = state.session.bars.every(
            (bar, idx) => copied[idx]?.time === bar.time,
          );
          if (prefixMatches) {
            return {
              barsByPane: nextBarsByPane,
              session: {
                ...state.session,
                bars: copied,
              },
            };
          }
        }
        return { barsByPane: nextBarsByPane };
      });
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
      const nextSession = stepReplayBack(state.session);
      set({
        session: nextSession,
        isPlaying: false,
        error: null,
      });
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
