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
import { PaneFetchCoordinator } from "./chart/fetchGuard";
import { LayoutSyncController } from "./layout/syncController";
import { TopToolbar } from "./layout/TopToolbar";
import { WorkspaceGrid } from "./layout/WorkspaceGrid";
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
  const prevFeedStateRef = useRef<string>("DISCONNECTED");
  const activeAuthControllerRef = useRef<AbortController | null>(null);
  const pendingLogoutPromiseRef = useRef<Promise<void> | null>(null);
  const retryAbortControllerRef = useRef<AbortController | null>(null);
  const layoutResolveControllerRef = useRef<AbortController | null>(null);
  const sessionEpochRef = useRef<number>(0);
  const contractResolveGenerationRef = useRef<number>(0);
  const activeLayoutRef = useRef<LayoutRecord | null>(null);
  activeLayoutRef.current = activeLayout;

  useEffect(() => {
    if (activeLayout !== null) {
      syncController.setSyncConfig(activeLayout.sync_config);
    }
  }, [activeLayout, syncController]);

  useEffect(() => {
    return () => {
      sessionEpochRef.current += 1;
      contractResolveGenerationRef.current += 1;
      activeAuthControllerRef.current?.abort();
      activeAuthControllerRef.current = null;
      retryAbortControllerRef.current?.abort();
      retryAbortControllerRef.current = null;
      layoutResolveControllerRef.current?.abort();
      layoutResolveControllerRef.current = null;
      wsClient.disconnect();
      coordinator.dispose();
      useLayoutStore.getState().setAuthErrorHandler(null);
      useLayoutStore.getState().reset();
    };
  }, [coordinator, wsClient]);

  const invalidateSessionLocally = useCallback(
    (errorMessage?: string | null, options?: { preserveCsrfForLogout?: boolean }) => {
      if (!options?.preserveCsrfForLogout) {
        clearCsrfToken();
      }
      sessionEpochRef.current += 1;
      contractResolveGenerationRef.current += 1;
      activeAuthControllerRef.current?.abort();
      activeAuthControllerRef.current = null;
      retryAbortControllerRef.current?.abort();
      retryAbortControllerRef.current = null;
      layoutResolveControllerRef.current?.abort();
      layoutResolveControllerRef.current = null;
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
      activeLayoutRef.current = null;
      setContracts({});
      setStatusData(null);
      setAuthStage("unauthenticated");
      setAuthError(errorMessage ?? null);
    },
    [coordinator, wsClient],
  );

  useEffect(() => {
    useLayoutStore.getState().setAuthErrorHandler((status, message) => {
      invalidateSessionLocally(status === 403 ? message : null);
    });
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
      retryAbortControllerRef.current?.abort();
      layoutResolveControllerRef.current?.abort();
      layoutResolveControllerRef.current = null;
      const controller = new AbortController();
      retryAbortControllerRef.current = controller;
      const requestEpoch = sessionEpochRef.current;
      const generation = ++contractResolveGenerationRef.current;
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

      const handleAuthFailFast = (err: unknown): never => {
        if (
          err instanceof ApiError &&
          (err.status === 401 || err.status === 403) &&
          !controller.signal.aborted &&
          requestEpoch === sessionEpochRef.current &&
          generation === contractResolveGenerationRef.current
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
          generation !== contractResolveGenerationRef.current
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
        if (retryAbortControllerRef.current === controller) {
          retryAbortControllerRef.current = null;
          setIsRetryingContracts(false);
          if (
            !controller.signal.aborted &&
            requestEpoch === sessionEpochRef.current &&
            generation === contractResolveGenerationRef.current &&
            options?.countAsAutoAttempt &&
            contractsRetryNeededRef.current
          ) {
            setRetrySchedule((prev) => ({ attempt: prev.attempt + 1, nonce: prev.nonce }));
          }
        }
      }
    },
    [
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
    const removeSessionExpiredListener = wsClient.onSessionExpired(() => {
      invalidateSessionLocally(null);
    });
    return () => {
      removeStateListener();
      removeStatusListener();
      removeErrorListener();
      removeSessionExpiredListener();
    };
  }, [invalidateSessionLocally, retryUnresolvedContracts, wsClient]);

  const bootstrapWorkspace = useCallback(
    async (signal?: AbortSignal) => {
      sessionEpochRef.current += 1;
      const requestEpoch = sessionEpochRef.current;
      const generation = ++contractResolveGenerationRef.current;
      setLoadError(null);
      retryAbortControllerRef.current?.abort();
      retryAbortControllerRef.current = null;
      layoutResolveControllerRef.current?.abort();
      layoutResolveControllerRef.current = null;
      contractsRetryNeededRef.current = false;
      unresolvedModesRef.current.clear();
      setIsRetryingContracts(false);
      setRetrySchedule((prev) => ({ attempt: 0, nonce: prev.nonce + 1 }));
      setContractsPendingRetry(false);

      const isStale = () =>
        Boolean(signal?.aborted) ||
        requestEpoch !== sessionEpochRef.current ||
        generation !== contractResolveGenerationRef.current;

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

      resolvedModesRef.current.clear();
      const distinctModes = distinctPaneLiveModes(defaultLayout?.panes ?? []);
      try {
        const result = await fetchContractsForModes(distinctModes, statusOpts);
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
      }

      if (isStale()) {
        return;
      }
      wsClient.connect();
    },
    [applyContractResolutionError, fetchContractsForModes, wsClient],
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
    contractResolveGenerationRef.current += 1;
    layoutResolveControllerRef.current?.abort();
    layoutResolveControllerRef.current = null;
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

  const handleLayoutChange = useCallback(
    (nextLayoutId: string) => {
      useLayoutStore.getState().selectLayout(nextLayoutId);
      const picked = useLayoutStore.getState().activeLayout;
      activeLayoutRef.current = picked;
      layoutResolveControllerRef.current?.abort();
      layoutResolveControllerRef.current = null;
      if (picked === null) {
        return;
      }
      const missingModes = distinctPaneLiveModes(picked.panes).filter(
        (mode) => !resolvedModesRef.current.has(mode),
      );
      if (missingModes.length === 0) {
        return;
      }
      retryAbortControllerRef.current?.abort();
      retryAbortControllerRef.current = null;
      setIsRetryingContracts(false);
      const controller = new AbortController();
      layoutResolveControllerRef.current = controller;
      const requestEpoch = sessionEpochRef.current;
      const generation = ++contractResolveGenerationRef.current;

      void fetchContractsForModes(missingModes, controller.signal)
        .then((result) => {
          if (
            controller.signal.aborted ||
            requestEpoch !== sessionEpochRef.current ||
            generation !== contractResolveGenerationRef.current
          ) {
            return;
          }
          applyContractResolutionResult(result);
        })
        .catch((err: unknown) => {
          if (
            controller.signal.aborted ||
            requestEpoch !== sessionEpochRef.current ||
            generation !== contractResolveGenerationRef.current ||
            (err instanceof DOMException && err.name === "AbortError")
          ) {
            return;
          }
          applyContractResolutionError(err, missingModes);
        })
        .finally(() => {
          if (layoutResolveControllerRef.current === controller) {
            layoutResolveControllerRef.current = null;
          }
        });
    },
    [applyContractResolutionError, applyContractResolutionResult, fetchContractsForModes],
  );

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
      />
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
    </div>
  );
}
