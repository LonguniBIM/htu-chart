import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  clearCsrfToken,
  fetchContracts,
  fetchSession,
  fetchStatus,
  listLayouts,
  login,
  logout,
} from "./api/client";
import type {
  ContractResolutionEntry,
  LayoutRecord,
  PaneConfig,
  RootSymbol,
  StatusResponse,
  Timeframe,
} from "./api/types";
import { MarketWsClient, type WsConnectionState } from "./api/wsClient";
import { BacktestPanel, type BacktestPaneTarget } from "./backtest/BacktestPanel";
import { useBacktestStore } from "./backtest/backtestStore";
import { PaneFetchCoordinator } from "./chart/fetchGuard";
import { DrawingToolbar } from "./drawings/DrawingToolbar";
import { LayoutSyncController } from "./layout/syncController";
import { TopToolbar } from "./layout/TopToolbar";
import { WorkspaceGrid } from "./layout/WorkspaceGrid";
import { ReplayControlBar } from "./replay/ReplayControlBar";
import { useReplayStore, type ReplayTarget } from "./replay/replayStore";
import {
  buildDrawingScopeKey,
  useDrawingStore,
  type DrawingScopeTarget,
} from "./store/drawingStore";
import { useLayoutStore } from "./store/layoutStore";

type AuthStage = "checking" | "unauthenticated" | "authenticated";

const DEFAULT_CONTRACT_SYMBOLS: readonly RootSymbol[] = ["MES", "MNQ", "MYM", "MGC"];
const CONTRACT_RETRY_DELAYS_MS = [2000, 4000, 8000, 15000] as const;
const MAX_AUTO_CONTRACT_RETRIES = 12;

interface FetchContractsBatchResult {
  updates: Record<string, ContractResolutionEntry>;
  resolvedModes: boolean[];
  incompleteModes: boolean[];
  partialFailureSummary: string | null;
}

function mergeResolvedContracts(
  prev: Record<string, ContractResolutionEntry>,
  updates: Record<string, ContractResolutionEntry>,
): Record<string, ContractResolutionEntry> {
  const next = { ...prev };
  for (const [key, entry] of Object.entries(updates)) {
    if (entry.selected !== null || next[key] === undefined) {
      next[key] = entry;
    }
  }
  return next;
}

function distinctPaneLiveModes(panes: readonly PaneConfig[]): boolean[] {
  const modes = Array.from(new Set(panes.map((pane) => pane.live_mode)));
  return modes.length > 0 ? modes : [false];
}

export function App() {
  const [authStage, setAuthStage] = useState<AuthStage>("checking");
  const [passwordInput, setPasswordInput] = useState<string>("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [statusData, setStatusData] = useState<StatusResponse | null>(null);
  const [contracts, setContracts] = useState<Record<string, ContractResolutionEntry>>({});
  const [wsState, setWsState] = useState<WsConnectionState>("disconnected");
  const [loadError, setLoadError] = useState<string | null>(null);

  const activeLayout = useLayoutStore((state) => state.activeLayout);
  const activePaneId = useLayoutStore((state) => state.activePaneId);
  const isReplayOpen = useReplayStore((state) => state.isOpen);
  const isBacktestOpen = useBacktestStore((state) => state.isOpen);

  const wsClient = useMemo(
    () =>
      new MarketWsClient(undefined, async (signal) => {
        try {
          await fetchSession(signal);
          return true;
        } catch (err: unknown) {
          if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
            return false;
          }
          throw err;
        }
      }),
    [],
  );
  const coordinator = useMemo(() => new PaneFetchCoordinator(), []);
  const syncController = useMemo(() => new LayoutSyncController(), []);
  const resolvedModesRef = useRef<Set<boolean>>(new Set());
  const [contractsPendingRetry, setContractsPendingRetry] = useState<boolean>(false);
  const [isRetryingContracts, setIsRetryingContracts] = useState<boolean>(false);
  const [retrySchedule, setRetrySchedule] = useState<{ attempt: number; nonce: number }>({
    attempt: 0,
    nonce: 0,
  });
  const contractsRetryNeededRef = useRef<boolean>(false);
  const unresolvedModesRef = useRef<Set<boolean>>(new Set());
  const inFlightContractControllersRef = useRef<
    Map<AbortController, readonly boolean[]>
  >(new Map());
  const prevFeedStateRef = useRef<string>("DISCONNECTED");
  const activeAuthControllerRef = useRef<AbortController | null>(null);
  const pendingLogoutPromiseRef = useRef<Promise<void> | null>(null);
  const retryAbortControllerRef = useRef<AbortController | null>(null);
  const inFlightLayoutControllersRef = useRef<Set<AbortController>>(new Set());
  const sessionEpochRef = useRef<number>(0);
  const retryGenerationRef = useRef<number>(0);
  const layoutResolveGenerationRef = useRef<number>(0);
  const prevActiveLayoutIdRef = useRef<string | null>(null);
  const activeLayoutRef = useRef<LayoutRecord | null>(null);
  activeLayoutRef.current = activeLayout;

  const isContractModeInFlight = useCallback((mode: boolean): boolean => {
    for (const modes of inFlightContractControllersRef.current.values()) {
      if (modes.includes(mode)) {
        return true;
      }
    }
    return false;
  }, []);

  const abortAndClearController = useCallback(
    (ref: { current: AbortController | null }) => {
      const controller = ref.current;
      if (controller !== null) {
        ref.current = null;
        inFlightContractControllersRef.current.delete(controller);
        controller.abort();
      }
    },
    [],
  );

  const abortAllLayoutControllers = useCallback(() => {
    layoutResolveGenerationRef.current += 1;
    for (const controller of inFlightLayoutControllersRef.current) {
      inFlightContractControllersRef.current.delete(controller);
      controller.abort();
    }
    inFlightLayoutControllersRef.current.clear();
  }, []);

  useEffect(() => {
    if (activeLayout !== null) {
      syncController.setSyncConfig(activeLayout.sync_config);
    }
  }, [activeLayout, syncController]);

  useEffect(() => {
    const inFlightControllers = inFlightContractControllersRef.current;
    return () => {
      sessionEpochRef.current += 1;
      retryGenerationRef.current += 1;
      prevActiveLayoutIdRef.current = null;
      activeAuthControllerRef.current?.abort();
      activeAuthControllerRef.current = null;
      abortAndClearController(retryAbortControllerRef);
      abortAllLayoutControllers();
      inFlightControllers.clear();
      wsClient.disconnect();
      coordinator.dispose();
      useLayoutStore.getState().setAuthErrorHandler(null);
      useLayoutStore.getState().reset();
      useDrawingStore.getState().setAuthErrorHandler(null);
      useDrawingStore.getState().reset();
      useBacktestStore.getState().setAuthErrorHandler(null);
      useBacktestStore.getState().reset();
      useReplayStore.getState().reset();
    };
  }, [abortAllLayoutControllers, abortAndClearController, coordinator, wsClient]);

  const invalidateSessionLocally = useCallback(
    (errorMessage?: string | null, options?: { preserveCsrfForLogout?: boolean }) => {
      if (!options?.preserveCsrfForLogout) {
        clearCsrfToken();
      }
      sessionEpochRef.current += 1;
      retryGenerationRef.current += 1;
      activeAuthControllerRef.current?.abort();
      activeAuthControllerRef.current = null;
      abortAndClearController(retryAbortControllerRef);
      abortAllLayoutControllers();
      inFlightContractControllersRef.current.clear();
      contractsRetryNeededRef.current = false;
      unresolvedModesRef.current.clear();
      resolvedModesRef.current.clear();
      prevFeedStateRef.current = "DISCONNECTED";
      setIsRetryingContracts(false);
      setRetrySchedule((prev) => ({ attempt: 0, nonce: prev.nonce + 1 }));
      setContractsPendingRetry(false);
      setLoadError(null);
      wsClient.disconnect();
      coordinator.dispose();
      useLayoutStore.getState().reset();
      useDrawingStore.getState().reset();
      useBacktestStore.getState().reset();
      useReplayStore.getState().reset();
      activeLayoutRef.current = null;
      prevActiveLayoutIdRef.current = null;
      setContracts({});
      setStatusData(null);
      setAuthStage("unauthenticated");
      setAuthError(errorMessage ?? null);
    },
    [abortAllLayoutControllers, abortAndClearController, coordinator, wsClient],
  );

  useEffect(() => {
    const authHandler = (status: number, message: string) => {
      invalidateSessionLocally(status === 403 ? message : null);
    };
    useLayoutStore.getState().setAuthErrorHandler(authHandler);
    useDrawingStore.getState().setAuthErrorHandler(authHandler);
    useBacktestStore.getState().setAuthErrorHandler(authHandler);
  }, [invalidateSessionLocally]);

  const fetchContractsForModes = useCallback(
    async (
      modes: readonly boolean[],
      signal?: AbortSignal,
    ): Promise<FetchContractsBatchResult> => {
      const updates: Record<string, ContractResolutionEntry> = {};
      const resolvedModesSet = new Set<boolean>();
      const incompleteModesSet = new Set<boolean>();
      const failureParts: string[] = [];

      for (const liveMode of modes) {
        try {
          const resolved = await fetchContracts(
            DEFAULT_CONTRACT_SYMBOLS,
            liveMode,
            signal,
          );
          const effectiveLive = Boolean(resolved.live);
          const modeLabel = effectiveLive ? "Live" : "Sim";
          const entries = Object.entries(resolved.contracts);
          const failedForMode: string[] = [];

          for (const [symbol, entry] of entries) {
            updates[`${effectiveLive ? "live" : "sim"}:${symbol}`] = entry;
            if (entry.selected === null) {
              const reason = entry.error_type ?? entry.error ?? "unresolved";
              failedForMode.push(`${symbol} (${reason})`);
            }
          }

          if (entries.length > 0 && failedForMode.length === 0) {
            resolvedModesSet.add(effectiveLive);
            resolvedModesSet.add(liveMode);
            incompleteModesSet.delete(liveMode);
          } else {
            resolvedModesSet.delete(effectiveLive);
            resolvedModesSet.delete(liveMode);
            incompleteModesSet.add(liveMode);
            if (failedForMode.length > 0) {
              failureParts.push(`${modeLabel}: ${failedForMode.join(", ")}`);
            } else {
              failureParts.push(`${modeLabel}: no contracts returned`);
            }
          }
        } catch (err: unknown) {
          if (err instanceof DOMException && err.name === "AbortError") {
            throw err;
          }
          if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
            throw err;
          }
          incompleteModesSet.add(liveMode);
          resolvedModesSet.delete(liveMode);
          if (!(err instanceof ApiError && err.status === 503)) {
            const modeLabel = liveMode ? "Live" : "Sim";
            const reason =
              err instanceof Error ? err.message : "Failed to resolve contracts";
            failureParts.push(`${modeLabel}: ${reason}`);
          }
        }
      }

      return {
        updates,
        resolvedModes: Array.from(resolvedModesSet),
        incompleteModes: Array.from(incompleteModesSet),
        partialFailureSummary:
          failureParts.length > 0
            ? `Incomplete contracts (${failureParts.join("; ")})`
            : null,
      };
    },
    [],
  );

  const applyContractResolutionResult = useCallback((result: FetchContractsBatchResult) => {
    setContracts((prev) => mergeResolvedContracts(prev, result.updates));
    for (const mode of result.resolvedModes) {
      resolvedModesRef.current.add(mode);
      unresolvedModesRef.current.delete(mode);
    }
    for (const mode of result.incompleteModes) {
      resolvedModesRef.current.delete(mode);
      unresolvedModesRef.current.add(mode);
    }
    if (unresolvedModesRef.current.size === 0 && result.incompleteModes.length === 0) {
      contractsRetryNeededRef.current = false;
      setContractsPendingRetry(false);
      setRetrySchedule((prev) => ({ attempt: 0, nonce: prev.nonce + 1 }));
      setLoadError(null);
    } else {
      contractsRetryNeededRef.current = true;
      setContractsPendingRetry(true);
      setLoadError(result.partialFailureSummary);
    }
  }, []);

  const applyContractResolutionError = useCallback(
    (err: unknown, attemptedModes: readonly boolean[]) => {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        invalidateSessionLocally(err.status === 403 ? err.message : null);
        return;
      }
      contractsRetryNeededRef.current = true;
      for (const mode of attemptedModes) {
        unresolvedModesRef.current.add(mode);
      }
      setContractsPendingRetry(true);
      if (!(err instanceof ApiError && err.status === 503)) {
        setLoadError(err instanceof Error ? err.message : "Failed to resolve contracts");
      }
    },
    [invalidateSessionLocally],
  );

  const retryUnresolvedContracts = useCallback(
    async (options?: { countAsAutoAttempt?: boolean; resetAttempt?: boolean }) => {
      abortAndClearController(retryAbortControllerRef);
      abortAllLayoutControllers();
      const controller = new AbortController();
      retryAbortControllerRef.current = controller;
      const requestEpoch = sessionEpochRef.current;
      const generation = ++retryGenerationRef.current;
      setIsRetryingContracts(true);
      if (options?.resetAttempt) {
        setRetrySchedule((prev) => ({ attempt: 0, nonce: prev.nonce + 1 }));
      }

      const activeMissingModes = distinctPaneLiveModes(
        activeLayoutRef.current?.panes ?? [],
      ).filter((mode) => !resolvedModesRef.current.has(mode));
      const modeSet = new Set<boolean>([
        ...unresolvedModesRef.current,
        ...activeMissingModes,
      ]);
      const modes =
        modeSet.size > 0
          ? Array.from(modeSet)
          : distinctPaneLiveModes(activeLayoutRef.current?.panes ?? []);

      inFlightContractControllersRef.current.set(controller, modes);

      const handleAuthFailFast = (err: unknown): never => {
        if (
          err instanceof ApiError &&
          (err.status === 401 || err.status === 403) &&
          !controller.signal.aborted &&
          requestEpoch === sessionEpochRef.current &&
          generation === retryGenerationRef.current
        ) {
          invalidateSessionLocally(err.status === 403 ? err.message : null);
        }
        throw err;
      };

      try {
        const [statusResult, contractsResult] = await Promise.allSettled([
          fetchStatus(controller.signal).catch(handleAuthFailFast),
          fetchContractsForModes(modes, controller.signal).catch(handleAuthFailFast),
        ]);

        if (
          controller.signal.aborted ||
          requestEpoch !== sessionEpochRef.current ||
          generation !== retryGenerationRef.current
        ) {
          return;
        }

        const statusErr: unknown =
          statusResult.status === "rejected" ? statusResult.reason : null;
        const contractsErr: unknown =
          contractsResult.status === "rejected" ? contractsResult.reason : null;
        const authErr =
          statusErr instanceof ApiError && (statusErr.status === 401 || statusErr.status === 403)
            ? statusErr
            : contractsErr instanceof ApiError &&
                (contractsErr.status === 401 || contractsErr.status === 403)
              ? contractsErr
              : null;
        if (authErr !== null) {
          invalidateSessionLocally(authErr.status === 403 ? authErr.message : null);
          return;
        }

        if (statusResult.status === "fulfilled") {
          const freshStatus = statusResult.value;
          setStatusData(freshStatus);
          if (freshStatus.feed_state !== undefined) {
            prevFeedStateRef.current = String(freshStatus.feed_state);
          }
        }

        if (contractsResult.status === "fulfilled") {
          applyContractResolutionResult(contractsResult.value);
          return;
        }

        const err: unknown = contractsResult.reason;
        if (err instanceof DOMException && err.name === "AbortError") {
          return;
        }
        applyContractResolutionError(err, modes);
      } finally {
        inFlightContractControllersRef.current.delete(controller);
        if (retryAbortControllerRef.current === controller) {
          retryAbortControllerRef.current = null;
          setIsRetryingContracts(false);
          if (
            !controller.signal.aborted &&
            requestEpoch === sessionEpochRef.current &&
            generation === retryGenerationRef.current &&
            options?.countAsAutoAttempt &&
            contractsRetryNeededRef.current
          ) {
            setRetrySchedule((prev) => ({ attempt: prev.attempt + 1, nonce: prev.nonce }));
          }
        }
      }
    },
    [
      abortAllLayoutControllers,
      abortAndClearController,
      applyContractResolutionError,
      applyContractResolutionResult,
      fetchContractsForModes,
      invalidateSessionLocally,
    ],
  );

  useEffect(() => {
    if (
      authStage !== "authenticated" ||
      !contractsPendingRetry ||
      isRetryingContracts ||
      retrySchedule.attempt >= MAX_AUTO_CONTRACT_RETRIES
    ) {
      return;
    }

    const delayMs =
      CONTRACT_RETRY_DELAYS_MS[
        Math.min(retrySchedule.attempt, CONTRACT_RETRY_DELAYS_MS.length - 1)
      ];
    const timerId = window.setTimeout(() => {
      void retryUnresolvedContracts({ countAsAutoAttempt: true });
    }, delayMs);

    return () => {
      window.clearTimeout(timerId);
    };
  }, [
    authStage,
    contractsPendingRetry,
    isRetryingContracts,
    retrySchedule.attempt,
    retrySchedule.nonce,
    retryUnresolvedContracts,
  ]);

  useEffect(() => {
    const removeStateListener = wsClient.onConnectionState(setWsState);
    const removeStatusListener = wsClient.onFeedStatus((msg) => {
      const nextFeedState = typeof msg.feed_state === "string" ? msg.feed_state : undefined;
      const prevFeedState = prevFeedStateRef.current;
      if (nextFeedState !== undefined) {
        prevFeedStateRef.current = nextFeedState;
      }
      setStatusData((prev) => ({
        ...(prev ?? {}),
        ...(nextFeedState !== undefined ? { feed_state: nextFeedState } : {}),
      }));

      const hasUnresolved =
        contractsRetryNeededRef.current || unresolvedModesRef.current.size > 0;
      const transitionedFromDisconnected =
        prevFeedState === "DISCONNECTED" &&
        nextFeedState !== undefined &&
        nextFeedState !== "DISCONNECTED";
      const isActiveFeedState =
        nextFeedState === "RECOVERING" ||
        nextFeedState === "LIVE" ||
        nextFeedState === "DEGRADED";

      if (hasUnresolved && (isActiveFeedState || transitionedFromDisconnected)) {
        void retryUnresolvedContracts();
      }
    });
    const removeErrorListener = wsClient.onError((msg) => {
      setLoadError(msg.detail || "WebSocket error");
    });
    const removeDrawingsListener = wsClient.onDrawingsUpdate((msg) => {
      useDrawingStore.getState().applyWsDrawingsUpdate(msg);
    });
    const removeSessionExpiredListener = wsClient.onSessionExpired(() => {
      invalidateSessionLocally(null);
    });
    return () => {
      removeStateListener();
      removeStatusListener();
      removeErrorListener();
      removeDrawingsListener();
      removeSessionExpiredListener();
    };
  }, [invalidateSessionLocally, retryUnresolvedContracts, wsClient]);

  const bootstrapWorkspace = useCallback(
    async (signal?: AbortSignal) => {
      sessionEpochRef.current += 1;
      const requestEpoch = sessionEpochRef.current;
      const retryGeneration = ++retryGenerationRef.current;
      prevActiveLayoutIdRef.current = null;
      setLoadError(null);
      abortAndClearController(retryAbortControllerRef);
      abortAllLayoutControllers();
      const layoutGeneration = layoutResolveGenerationRef.current;
      inFlightContractControllersRef.current.clear();
      contractsRetryNeededRef.current = false;
      unresolvedModesRef.current.clear();
      setIsRetryingContracts(false);
      setRetrySchedule((prev) => ({ attempt: 0, nonce: prev.nonce + 1 }));
      setContractsPendingRetry(false);

      const isStale = () =>
        Boolean(signal?.aborted) ||
        requestEpoch !== sessionEpochRef.current ||
        retryGeneration !== retryGenerationRef.current ||
        layoutGeneration !== layoutResolveGenerationRef.current;

      const statusOpts = signal !== undefined ? signal : undefined;
      await fetchSession(statusOpts);
      if (isStale()) {
        return;
      }
      const status = await fetchStatus(statusOpts);
      if (isStale()) {
        return;
      }
      setStatusData(status);
      prevFeedStateRef.current = String(status.feed_state ?? "DISCONNECTED");

      const loadedLayouts = await listLayouts(statusOpts);
      if (isStale()) {
        return;
      }
      useLayoutStore.getState().hydrateLayouts(loadedLayouts);
      const defaultLayout = useLayoutStore.getState().activeLayout;
      activeLayoutRef.current = defaultLayout;
      prevActiveLayoutIdRef.current = defaultLayout?.id ?? null;

      resolvedModesRef.current.clear();
      const distinctModes = distinctPaneLiveModes(defaultLayout?.panes ?? []);
      const bootstrapContractsController = new AbortController();
      const onParentAbort = () => {
        inFlightContractControllersRef.current.delete(bootstrapContractsController);
        bootstrapContractsController.abort();
      };
      if (signal !== undefined) {
        if (signal.aborted) {
          return;
        }
        signal.addEventListener("abort", onParentAbort, { once: true });
      }
      inFlightContractControllersRef.current.set(bootstrapContractsController, distinctModes);
      try {
        const result = await fetchContractsForModes(
          distinctModes,
          bootstrapContractsController.signal,
        );
        if (isStale()) {
          return;
        }
        for (const mode of result.resolvedModes) {
          resolvedModesRef.current.add(mode);
        }
        for (const mode of result.incompleteModes) {
          resolvedModesRef.current.delete(mode);
        }
        setContracts(result.updates);
        if (result.incompleteModes.length > 0) {
          contractsRetryNeededRef.current = true;
          unresolvedModesRef.current = new Set(result.incompleteModes);
          setContractsPendingRetry(true);
          setLoadError(result.partialFailureSummary);
        } else {
          contractsRetryNeededRef.current = false;
          unresolvedModesRef.current.clear();
          setContractsPendingRetry(false);
        }
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") {
          throw err;
        }
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          throw err;
        }
        if (isStale()) {
          return;
        }
        if (err instanceof ApiError && err.status === 503) {
          setContracts({});
        }
        applyContractResolutionError(err, distinctModes);
      } finally {
        signal?.removeEventListener("abort", onParentAbort);
        inFlightContractControllersRef.current.delete(bootstrapContractsController);
      }

      if (isStale()) {
        return;
      }
      wsClient.connect();
    },
    [
      abortAllLayoutControllers,
      abortAndClearController,
      applyContractResolutionError,
      fetchContractsForModes,
      wsClient,
    ],
  );

  useEffect(() => {
    activeAuthControllerRef.current?.abort();
    const controller = new AbortController();
    activeAuthControllerRef.current = controller;

    void bootstrapWorkspace(controller.signal)
      .then(() => {
        if (controller.signal.aborted) {
          return;
        }
        setAuthStage("authenticated");
      })
      .catch((err: unknown) => {
        if (
          controller.signal.aborted ||
          (err instanceof DOMException && err.name === "AbortError")
        ) {
          return;
        }
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          invalidateSessionLocally(err.status === 401 ? null : err.message);
          return;
        }
        setAuthStage("unauthenticated");
        setAuthError(err instanceof Error ? err.message : "Unable to reach backend");
      })
      .finally(() => {
        if (activeAuthControllerRef.current === controller) {
          activeAuthControllerRef.current = null;
        }
      });

    return () => {
      controller.abort();
      if (activeAuthControllerRef.current === controller) {
        activeAuthControllerRef.current = null;
      }
    };
  }, [bootstrapWorkspace, invalidateSessionLocally]);

  const handleLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAuthError(null);
    sessionEpochRef.current += 1;
    retryGenerationRef.current += 1;
    abortAndClearController(retryAbortControllerRef);
    abortAllLayoutControllers();
    inFlightContractControllersRef.current.clear();
    activeAuthControllerRef.current?.abort();
    const controller = new AbortController();
    activeAuthControllerRef.current = controller;
    let loginSucceeded = false;

    try {
      if (pendingLogoutPromiseRef.current !== null) {
        await pendingLogoutPromiseRef.current;
        if (controller.signal.aborted) {
          return;
        }
      }
      await login(passwordInput, controller.signal);
      if (controller.signal.aborted) {
        return;
      }
      loginSucceeded = true;
      setPasswordInput("");
      await bootstrapWorkspace(controller.signal);
      if (controller.signal.aborted) {
        return;
      }
      setAuthStage("authenticated");
    } catch (err: unknown) {
      if (
        controller.signal.aborted ||
        (err instanceof DOMException && err.name === "AbortError")
      ) {
        return;
      }
      if (
        loginSucceeded &&
        err instanceof ApiError &&
        (err.status === 401 || err.status === 403)
      ) {
        invalidateSessionLocally(err.status === 401 ? null : err.message);
        return;
      }
      setAuthError(err instanceof Error ? err.message : "Login failed");
    } finally {
      if (activeAuthControllerRef.current === controller) {
        activeAuthControllerRef.current = null;
      }
    }
  };

  const handleLogout = async () => {
    invalidateSessionLocally(null, { preserveCsrfForLogout: true });
    const logoutTask = (async () => {
      try {
        await logout();
      } catch {
        // Clear local UI state even if session already expired
      }
    })();
    pendingLogoutPromiseRef.current = logoutTask;
    try {
      await logoutTask;
    } finally {
      if (pendingLogoutPromiseRef.current === logoutTask) {
        pendingLogoutPromiseRef.current = null;
      }
    }
  };

  const handlePaneAuthError = useCallback(
    (status: number, message: string) => {
      invalidateSessionLocally(status === 403 ? message : null);
    },
    [invalidateSessionLocally],
  );

  const handlePaneSymbolChange = useCallback((paneId: string, nextSymbol: RootSymbol) => {
    useLayoutStore.getState().setPaneSymbol(paneId, nextSymbol);
  }, []);

  const handlePaneTimeframeChange = useCallback((paneId: string, nextTimeframe: Timeframe) => {
    useLayoutStore.getState().setPaneTimeframe(paneId, nextTimeframe);
  }, []);

  const handleSelectPane = useCallback((paneId: string) => {
    useLayoutStore.getState().selectPane(paneId);
  }, []);

  const resolveMissingContractModes = useCallback(
    (
      missingModes: readonly boolean[],
      options?: { cancelRetry?: boolean; replaceExisting?: boolean },
    ) => {
      if (missingModes.length === 0) {
        return;
      }
      if (options?.replaceExisting || options?.cancelRetry) {
        abortAllLayoutControllers();
      }
      const requestEpoch = sessionEpochRef.current;
      const generation = layoutResolveGenerationRef.current;
      if (options?.cancelRetry) {
        retryGenerationRef.current += 1;
        abortAndClearController(retryAbortControllerRef);
        setIsRetryingContracts(false);
      }
      const controller = new AbortController();
      inFlightLayoutControllersRef.current.add(controller);
      inFlightContractControllersRef.current.set(controller, missingModes);

      void fetchContractsForModes(missingModes, controller.signal)
        .then((result) => {
          if (
            controller.signal.aborted ||
            requestEpoch !== sessionEpochRef.current ||
            generation !== layoutResolveGenerationRef.current
          ) {
            return;
          }
          applyContractResolutionResult(result);
        })
        .catch((err: unknown) => {
          if (
            controller.signal.aborted ||
            requestEpoch !== sessionEpochRef.current ||
            generation !== layoutResolveGenerationRef.current ||
            (err instanceof DOMException && err.name === "AbortError")
          ) {
            return;
          }
          applyContractResolutionError(err, missingModes);
        })
        .finally(() => {
          inFlightContractControllersRef.current.delete(controller);
          inFlightLayoutControllersRef.current.delete(controller);
        });
    },
    [
      abortAllLayoutControllers,
      abortAndClearController,
      applyContractResolutionError,
      applyContractResolutionResult,
      fetchContractsForModes,
    ],
  );

  useEffect(() => {
    if (authStage !== "authenticated" || activeLayout === null || activeLayout.panes.length === 0) {
      return;
    }
    const distinctModes = distinctPaneLiveModes(activeLayout.panes);
    const cancelledStillNeededModes = new Set<boolean>();
    for (const controller of Array.from(inFlightLayoutControllersRef.current)) {
      const inFlightModes = inFlightContractControllersRef.current.get(controller) ?? [];
      if (inFlightModes.some((mode) => !distinctModes.includes(mode))) {
        for (const mode of inFlightModes) {
          if (distinctModes.includes(mode) && !resolvedModesRef.current.has(mode)) {
            cancelledStillNeededModes.add(mode);
          }
        }
        inFlightLayoutControllersRef.current.delete(controller);
        inFlightContractControllersRef.current.delete(controller);
        controller.abort();
      }
    }

    const didSwitchLayoutId = prevActiveLayoutIdRef.current !== activeLayout.id;
    prevActiveLayoutIdRef.current = activeLayout.id;

    const modesToFetch = didSwitchLayoutId
      ? distinctModes.filter(
          (mode) => !resolvedModesRef.current.has(mode) && !isContractModeInFlight(mode),
        )
      : distinctModes.filter(
          (mode) =>
            !resolvedModesRef.current.has(mode) &&
            (!unresolvedModesRef.current.has(mode) || cancelledStillNeededModes.has(mode)) &&
            !isContractModeInFlight(mode),
        );

    if (modesToFetch.length > 0) {
      resolveMissingContractModes(modesToFetch);
    }
  }, [
    activeLayout,
    authStage,
    isContractModeInFlight,
    resolveMissingContractModes,
  ]);

  const handleLayoutChange = useCallback(
    (nextLayoutId: string) => {
      useReplayStore.getState().exitReplay();
      useLayoutStore.getState().selectLayout(nextLayoutId);
      const picked = useLayoutStore.getState().activeLayout;
      activeLayoutRef.current = picked;
      prevActiveLayoutIdRef.current = picked?.id ?? null;
      abortAllLayoutControllers();
      if (picked === null) {
        return;
      }
      const missingModes = distinctPaneLiveModes(picked.panes).filter(
        (mode) => !resolvedModesRef.current.has(mode),
      );
      resolveMissingContractModes(missingModes, { cancelRetry: true, replaceExisting: true });
    },
    [abortAllLayoutControllers, resolveMissingContractModes],
  );

  const activeDrawingScope = useMemo<DrawingScopeTarget | null>(() => {
    if (activeLayout === null || activeLayout.panes.length === 0) {
      return null;
    }
    const targetPane =
      activeLayout.panes.find((p) => p.pane_id === activePaneId) ?? activeLayout.panes[0];
    if (targetPane === undefined) {
      return null;
    }
    const cacheKey = `${targetPane.live_mode ? "live" : "sim"}:${targetPane.root_symbol}`;
    const entry = contracts[cacheKey];
    const resolvedContractId =
      entry?.selected !== null && entry?.selected !== undefined
        ? (entry.selected.id ?? entry.selected.contractId ?? null)
        : null;
    return {
      symbol: targetPane.root_symbol,
      contractId: resolvedContractId,
      paneId: targetPane.pane_id,
    };
  }, [activeLayout, activePaneId, contracts]);

  const activePaneTarget = useMemo<BacktestPaneTarget | null>(() => {
    if (activeLayout === null || activeLayout.panes.length === 0) {
      return null;
    }
    const targetPane =
      activeLayout.panes.find((p) => p.pane_id === activePaneId) ?? activeLayout.panes[0];
    if (targetPane === undefined) {
      return null;
    }
    const cacheKey = `${targetPane.live_mode ? "live" : "sim"}:${targetPane.root_symbol}`;
    const entry = contracts[cacheKey];
    const resolvedContractId =
      entry?.selected !== null && entry?.selected !== undefined
        ? (entry.selected.id ?? entry.selected.contractId ?? null)
        : null;
    return {
      paneId: targetPane.pane_id,
      contractId: resolvedContractId,
      rootSymbol: targetPane.root_symbol,
      timeframe: targetPane.timeframe,
      liveMode: targetPane.live_mode,
    };
  }, [activeLayout, activePaneId, contracts]);

  const activeReplayTarget = useMemo<ReplayTarget | null>(() => {
    if (activePaneTarget === null || !activePaneTarget.contractId) {
      return null;
    }
    return {
      paneId: activePaneTarget.paneId,
      contractId: activePaneTarget.contractId,
      rootSymbol: activePaneTarget.rootSymbol,
      timeframe: activePaneTarget.timeframe,
      liveMode: activePaneTarget.liveMode,
    };
  }, [activePaneTarget]);

  useEffect(() => {
    if (authStage !== "authenticated") {
      return;
    }
    const isEditableTarget = (target: EventTarget | null): boolean => {
      if (!(target instanceof HTMLElement)) {
        return false;
      }
      const tag = target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
        return true;
      }
      return target.isContentEditable;
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target)) {
        return;
      }
      const store = useDrawingStore.getState();
      const scope = activeDrawingScope;
      const scopeKey =
        scope !== null
          ? buildDrawingScopeKey(scope.symbol, scope.contractId, scope.paneId)
          : "";
      const scopeState = scopeKey ? store.scopes[scopeKey] : undefined;
      const selectedId = scopeKey ? (store.selectedByScope[scopeKey] ?? null) : null;
      const hasReadyScope =
        scope !== null && Boolean(scope.contractId && scope.contractId.trim().length > 0);

      if (event.key === "Escape") {
        if (store.activeTool !== "cursor") {
          store.setActiveTool("cursor");
        } else if (scope !== null && selectedId !== null) {
          store.setSelectedDrawing(scope, null);
        }
        return;
      }

      if (event.key === "Delete" || event.key === "Backspace") {
        if (hasReadyScope && scope !== null && selectedId !== null && !scopeState?.mutating) {
          event.preventDefault();
          void store.deleteDrawing(scope, selectedId);
        }
        return;
      }

      const mod = event.ctrlKey || event.metaKey;
      if (!mod) {
        return;
      }
      const keyLower = event.key.toLowerCase();
      if (keyLower === "z" && !event.shiftKey) {
        if (hasReadyScope && scope !== null && scopeState?.canUndo && !scopeState.mutating) {
          event.preventDefault();
          void store.undoScope(scope);
        }
        return;
      }
      if (keyLower === "y" || (keyLower === "z" && event.shiftKey)) {
        if (hasReadyScope && scope !== null && scopeState?.canRedo && !scopeState.mutating) {
          event.preventDefault();
          void store.redoScope(scope);
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [activeDrawingScope, authStage]);

  if (authStage === "checking") {
    return (
      <main className="login-gate">
        <div className="login-card">
          <h1>TopstepX Terminal</h1>
          <p>Checking caller session...</p>
        </div>
      </main>
    );
  }

  if (authStage === "unauthenticated") {
    return (
      <main className="login-gate">
        <form className="login-card" onSubmit={(e) => void handleLogin(e)}>
          <h1>TopstepX Terminal</h1>
          <p>Read-Only Futures Market Data &amp; Charting</p>
          <input
            type="password"
            aria-label="Terminal password"
            placeholder="Enter terminal password"
            value={passwordInput}
            onChange={(e) => setPasswordInput(e.target.value)}
            autoComplete="current-password"
            required
          />
          {authError !== null && <div className="login-error">{authError}</div>}
          <button type="submit" className="terminal-button">
            Unlock Terminal
          </button>
        </form>
      </main>
    );
  }

  return (
    <div className="terminal-shell">
      <TopToolbar
        wsState={wsState}
        statusData={statusData}
        contractsPendingRetry={contractsPendingRetry}
        loadError={loadError}
        onSelectLayout={handleLayoutChange}
        onRetryContracts={() => void retryUnresolvedContracts({ resetAttempt: true })}
        onLogout={() => void handleLogout()}
        isReplayOpen={isReplayOpen}
        onToggleReplay={() => useReplayStore.getState().togglePanel()}
        isBacktestOpen={isBacktestOpen}
        onToggleBacktest={() => useBacktestStore.getState().togglePanel()}
      />
      <ReplayControlBar activeTarget={activeReplayTarget} />
      <div className="terminal-body">
        <DrawingToolbar activeScope={activeDrawingScope} />
        <WorkspaceGrid
          layout={activeLayout}
          activePaneId={activePaneId}
          contracts={contracts}
          activeFeedLive={statusData?.live ?? null}
          wsClient={wsClient}
          coordinator={coordinator}
          syncController={syncController}
          onSelectPane={handleSelectPane}
          onSymbolChange={handlePaneSymbolChange}
          onTimeframeChange={handlePaneTimeframeChange}
          onAuthError={handlePaneAuthError}
        />
        <BacktestPanel activeTarget={activePaneTarget} />
      </div>
    </div>
  );
}
