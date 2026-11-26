import { create } from "zustand";
import {
  ApiError,
  cancelBacktestJob,
  fetchBacktestStrategies,
  getBacktestJob,
  listBacktestJobs,
  submitBacktestJob,
} from "../api/client";
import type {
  BacktestJobDetail,
  BacktestJobStatus,
  BacktestJobSummary,
  BacktestStrategyInfo,
  BacktestSubmitRequest,
} from "../api/types";
import type { AuthErrorHandler } from "../store/layoutStore";

export type BacktestOverlayFlag =
  | "showZones"
  | "showTradeLines"
  | "showMarkers"
  | "showIndicators";

export interface BacktestStoreState {
  isOpen: boolean;
  strategies: BacktestStrategyInfo[];
  strategiesLoading: boolean;
  strategiesAttempted: boolean;
  activeJob: BacktestJobDetail | null;
  jobs: BacktestJobSummary[];
  submitting: boolean;
  polling: boolean;
  error: string | null;
  showZones: boolean;
  showTradeLines: boolean;
  showMarkers: boolean;
  showIndicators: boolean;
  sessionEpoch: number;
  authErrorHandler: AuthErrorHandler | null;

  setAuthErrorHandler: (handler: AuthErrorHandler | null) => void;
  togglePanel: () => void;
  loadStrategies: () => Promise<void>;
  loadJobs: () => Promise<void>;
  runBacktest: (payload: BacktestSubmitRequest) => Promise<BacktestJobDetail | null>;
  cancelActiveJob: () => Promise<void>;
  selectJob: (jobId: string) => Promise<void>;
  toggleOverlayFlag: (flag: BacktestOverlayFlag) => void;
  clearResult: () => void;
  reset: () => void;
}

const POLL_INTERVAL_MS = 500;
const TERMINAL_STATUSES: ReadonlySet<BacktestJobStatus> = new Set([
  "COMPLETED",
  "CANCELLED",
  "TIMED_OUT",
  "FAILED",
]);

let activePollTimerId: number | null = null;
let activePollController: AbortController | null = null;
let activeActionController: AbortController | null = null;
let activeStrategiesController: AbortController | null = null;
let activePollResolve: ((detail: BacktestJobDetail | null) => void) | null = null;
let pollGeneration = 0;

function clearPollTimer(): void {
  if (activePollTimerId !== null) {
    window.clearTimeout(activePollTimerId);
    activePollTimerId = null;
  }
}

function abortActivePoll(): void {
  pollGeneration += 1;
  clearPollTimer();
  activePollController?.abort();
  activePollController = null;
  const pendingResolve = activePollResolve;
  activePollResolve = null;
  pendingResolve?.(null);
}

function abortAllBacktestControllers(): void {
  abortActivePoll();
  activeActionController?.abort();
  activeActionController = null;
  activeStrategiesController?.abort();
  activeStrategiesController = null;
}

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function isAuthStatusError(err: unknown): err is ApiError {
  return err instanceof ApiError && (err.status === 401 || err.status === 403);
}

function upsertJobSummary(
  jobs: readonly BacktestJobSummary[],
  summary: BacktestJobSummary,
): BacktestJobSummary[] {
  const filtered = jobs.filter((item) => item.job_id !== summary.job_id);
  return [summary, ...filtered].slice(0, 50);
}

function toJobDetail(summary: BacktestJobSummary): BacktestJobDetail {
  return {
    ...summary,
    result: null,
  };
}

export const useBacktestStore = create<BacktestStoreState>((set, get) => {
  const handlePossibleAuthError = (err: unknown, requestEpoch: number): boolean => {
    if (isAuthStatusError(err) && requestEpoch === get().sessionEpoch) {
      get().authErrorHandler?.(err.status, err.message);
      return true;
    }
    return false;
  };

  return {
    isOpen: false,
    strategies: [],
    strategiesLoading: false,
    strategiesAttempted: false,
    activeJob: null,
    jobs: [],
    submitting: false,
    polling: false,
    error: null,
    showZones: true,
    showTradeLines: true,
    showMarkers: true,
    showIndicators: true,
    sessionEpoch: 0,
    authErrorHandler: null,

    setAuthErrorHandler: (handler) => {
      set({ authErrorHandler: handler });
    },

    togglePanel: () => {
      set((state) => ({ isOpen: !state.isOpen }));
    },

    loadStrategies: async () => {
      activeStrategiesController?.abort();
      const controller = new AbortController();
      activeStrategiesController = controller;
      const requestEpoch = get().sessionEpoch;
      set({ strategiesLoading: true, error: null });
      try {
        const res = await fetchBacktestStrategies(controller.signal);
        if (controller.signal.aborted || requestEpoch !== get().sessionEpoch) {
          return;
        }
        set({
          strategies: res.strategies,
          strategiesLoading: false,
          strategiesAttempted: true,
        });
      } catch (err: unknown) {
        if (controller.signal.aborted || isAbortError(err)) {
          return;
        }
        if (requestEpoch !== get().sessionEpoch) {
          return;
        }
        set({
          strategiesLoading: false,
          strategiesAttempted: true,
        });
        if (handlePossibleAuthError(err, requestEpoch)) {
          return;
        }
        set({
          error: err instanceof Error ? err.message : "Failed to load backtest strategies",
        });
      } finally {
        if (activeStrategiesController === controller) {
          activeStrategiesController = null;
        }
      }
    },

    loadJobs: async () => {
      const requestEpoch = get().sessionEpoch;
      try {
        const res = await listBacktestJobs({ limit: 20 });
        if (requestEpoch !== get().sessionEpoch) {
          return;
        }
        set({ jobs: res.items });
      } catch (err: unknown) {
        if (requestEpoch !== get().sessionEpoch || isAbortError(err)) {
          return;
        }
        if (handlePossibleAuthError(err, requestEpoch)) {
          return;
        }
        set({
          error: err instanceof Error ? err.message : "Failed to load backtest jobs",
        });
      }
    },

    runBacktest: async (payload) => {
      abortActivePoll();
      activeActionController?.abort();
      const submitController = new AbortController();
      activeActionController = submitController;
      const requestEpoch = get().sessionEpoch;
      const myGen = pollGeneration;

      set({
        submitting: true,
        polling: false,
        error: null,
      });

      try {
        const submitted = await submitBacktestJob(payload, submitController.signal);
        if (
          submitController.signal.aborted ||
          requestEpoch !== get().sessionEpoch ||
          myGen !== pollGeneration
        ) {
          return null;
        }

        const initialDetail = toJobDetail(submitted);
        set((state) => ({
          submitting: false,
          polling: !TERMINAL_STATUSES.has(submitted.status),
          activeJob: initialDetail,
          jobs: upsertJobSummary(state.jobs, submitted),
        }));

        if (TERMINAL_STATUSES.has(submitted.status)) {
          const terminalDetail = await getBacktestJob(submitted.job_id, submitController.signal);
          if (
            submitController.signal.aborted ||
            requestEpoch !== get().sessionEpoch ||
            myGen !== pollGeneration
          ) {
            return null;
          }
          set((state) => ({
            polling: false,
            activeJob: terminalDetail,
            jobs: upsertJobSummary(state.jobs, terminalDetail),
            error:
              terminalDetail.status === "FAILED" || terminalDetail.status === "TIMED_OUT"
                ? (terminalDetail.error ?? `Backtest ${terminalDetail.status.toLowerCase()}`)
                : null,
          }));
          return terminalDetail;
        }

        return await new Promise<BacktestJobDetail | null>((resolve) => {
          activePollResolve = resolve;
          const finish = (value: BacktestJobDetail | null) => {
            if (activePollResolve === resolve) {
              activePollResolve = null;
            }
            resolve(value);
          };

          const pollOnce = async () => {
            if (requestEpoch !== get().sessionEpoch || myGen !== pollGeneration) {
              finish(null);
              return;
            }
            activePollController?.abort();
            const pollController = new AbortController();
            activePollController = pollController;
            try {
              const detail = await getBacktestJob(submitted.job_id, pollController.signal);
              if (
                pollController.signal.aborted ||
                requestEpoch !== get().sessionEpoch ||
                myGen !== pollGeneration
              ) {
                finish(null);
                return;
              }
              if (TERMINAL_STATUSES.has(detail.status)) {
                clearPollTimer();
                set((state) => ({
                  polling: false,
                  activeJob: detail,
                  jobs: upsertJobSummary(state.jobs, detail),
                  error:
                    detail.status === "FAILED" || detail.status === "TIMED_OUT"
                      ? (detail.error ?? `Backtest ${detail.status.toLowerCase()}`)
                      : null,
                }));
                finish(detail);
                return;
              }
              set((state) => ({
                polling: true,
                activeJob: detail,
                jobs: upsertJobSummary(state.jobs, detail),
              }));
              activePollTimerId = window.setTimeout(() => {
                void pollOnce();
              }, POLL_INTERVAL_MS);
            } catch (err: unknown) {
              if (
                pollController.signal.aborted ||
                isAbortError(err) ||
                requestEpoch !== get().sessionEpoch ||
                myGen !== pollGeneration
              ) {
                finish(null);
                return;
              }
              clearPollTimer();
              set({ polling: false });
              if (!handlePossibleAuthError(err, requestEpoch)) {
                set({
                  error: err instanceof Error ? err.message : "Failed to poll backtest job",
                });
              }
              finish(null);
            } finally {
              if (activePollController === pollController) {
                activePollController = null;
              }
            }
          };

          activePollTimerId = window.setTimeout(() => {
            void pollOnce();
          }, POLL_INTERVAL_MS);
        });
      } catch (err: unknown) {
        if (
          submitController.signal.aborted ||
          isAbortError(err) ||
          requestEpoch !== get().sessionEpoch ||
          myGen !== pollGeneration
        ) {
          if (requestEpoch === get().sessionEpoch && myGen === pollGeneration) {
            set({ submitting: false, polling: false });
          }
          return null;
        }
        set({ submitting: false, polling: false });
        if (handlePossibleAuthError(err, requestEpoch)) {
          return null;
        }
        set({
          error: err instanceof Error ? err.message : "Failed to submit backtest job",
        });
        return null;
      } finally {
        if (activeActionController === submitController) {
          activeActionController = null;
        }
      }
    },

    cancelActiveJob: async () => {
      const currentJob = get().activeJob;
      if (currentJob === null) {
        return;
      }
      abortActivePoll();
      activeActionController?.abort();
      const controller = new AbortController();
      activeActionController = controller;
      const requestEpoch = get().sessionEpoch;
      try {
        const cancelled = await cancelBacktestJob(currentJob.job_id, controller.signal);
        if (controller.signal.aborted || requestEpoch !== get().sessionEpoch) {
          return;
        }
        set((state) => ({
          polling: false,
          submitting: false,
          activeJob: { ...cancelled, result: null },
          jobs: upsertJobSummary(state.jobs, cancelled),
          error: cancelled.error,
        }));
      } catch (err: unknown) {
        if (controller.signal.aborted || isAbortError(err) || requestEpoch !== get().sessionEpoch) {
          return;
        }
        set({ polling: false, submitting: false });
        if (err instanceof ApiError && err.status === 409) {
          try {
            const refreshed = await getBacktestJob(currentJob.job_id, controller.signal);
            if (controller.signal.aborted || requestEpoch !== get().sessionEpoch) {
              return;
            }
            set((state) => ({
              polling: false,
              submitting: false,
              activeJob: refreshed,
              jobs: upsertJobSummary(state.jobs, refreshed),
              error: refreshed.error,
            }));
          } catch (refreshErr: unknown) {
            if (
              controller.signal.aborted ||
              isAbortError(refreshErr) ||
              requestEpoch !== get().sessionEpoch
            ) {
              return;
            }
            if (!handlePossibleAuthError(refreshErr, requestEpoch)) {
              set({
                error:
                  refreshErr instanceof Error
                    ? refreshErr.message
                    : "Failed to refresh backtest job after 409 conflict",
              });
            }
          }
          return;
        }
        if (handlePossibleAuthError(err, requestEpoch)) {
          return;
        }
        set({
          error: err instanceof Error ? err.message : "Failed to cancel backtest job",
        });
      } finally {
        if (activeActionController === controller) {
          activeActionController = null;
        }
      }
    },

    selectJob: async (jobId) => {
      abortActivePoll();
      activeActionController?.abort();
      activeActionController = null;
      const controller = new AbortController();
      activeActionController = controller;
      const requestEpoch = get().sessionEpoch;
      set({ submitting: false, polling: false, error: null });
      try {
        const detail = await getBacktestJob(jobId, controller.signal);
        if (controller.signal.aborted || requestEpoch !== get().sessionEpoch) {
          return;
        }
        set((state) => ({
          activeJob: detail,
          jobs: upsertJobSummary(state.jobs, detail),
          submitting: false,
          polling: false,
          error: detail.error,
        }));
      } catch (err: unknown) {
        if (controller.signal.aborted || isAbortError(err) || requestEpoch !== get().sessionEpoch) {
          return;
        }
        if (handlePossibleAuthError(err, requestEpoch)) {
          return;
        }
        set({
          error: err instanceof Error ? err.message : "Failed to load backtest job",
        });
      } finally {
        if (activeActionController === controller) {
          activeActionController = null;
        }
      }
    },

    toggleOverlayFlag: (flag) => {
      set((state) => ({ [flag]: !state[flag] }));
    },

    clearResult: () => {
      abortActivePoll();
      activeActionController?.abort();
      activeActionController = null;
      set({
        activeJob: null,
        submitting: false,
        polling: false,
        error: null,
      });
    },

    reset: () => {
      abortAllBacktestControllers();
      set((state) => ({
        isOpen: false,
        strategies: [],
        strategiesLoading: false,
        strategiesAttempted: false,
        activeJob: null,
        jobs: [],
        submitting: false,
        polling: false,
        error: null,
        showZones: true,
        showTradeLines: true,
        showMarkers: true,
        showIndicators: true,
        sessionEpoch: state.sessionEpoch + 1,
      }));
    },
  };
});
