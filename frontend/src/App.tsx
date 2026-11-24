import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
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
import { ChartPane } from "./chart/ChartPane";
import { PaneFetchCoordinator } from "./chart/fetchGuard";

type AuthStage = "checking" | "unauthenticated" | "authenticated";

const DEFAULT_CONTRACT_SYMBOLS: readonly RootSymbol[] = ["MES", "MNQ", "MYM", "MGC"];
const CONTRACT_RETRY_DELAYS_MS = [2000, 4000, 8000, 15000] as const;
const MAX_AUTO_CONTRACT_RETRIES = 12;

interface FetchContractsBatchResult {
  updates: Record<string, ContractResolutionEntry>;
  fullyResolvedModes: boolean[];
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

function contractCacheKey(liveMode: boolean, rootSymbol: RootSymbol): string {
  return `${liveMode ? "live" : "sim"}:${rootSymbol}`;
}

function distinctPaneLiveModes(panes: readonly PaneConfig[]): boolean[] {
  const modes = Array.from(new Set(panes.map((pane) => pane.live_mode)));
  return modes.length > 0 ? modes : [false];
}

function resolveContractId(entry: ContractResolutionEntry | undefined): string | null {
  if (entry === undefined || entry.selected === null) {
    return null;
  }
  return entry.selected.id ?? entry.selected.contractId ?? null;
}

export function App() {
  const [authStage, setAuthStage] = useState<AuthStage>("checking");
  const [passwordInput, setPasswordInput] = useState<string>("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [statusData, setStatusData] = useState<StatusResponse | null>(null);
  const [layouts, setLayouts] = useState<LayoutRecord[]>([]);
  const [activeLayout, setActiveLayout] = useState<LayoutRecord | null>(null);
  const [contracts, setContracts] = useState<Record<string, ContractResolutionEntry>>({});
  const [wsState, setWsState] = useState<WsConnectionState>("disconnected");
  const [loadError, setLoadError] = useState<string | null>(null);

  const wsClient = useMemo(() => new MarketWsClient(), []);
  const coordinator = useMemo(() => new PaneFetchCoordinator(), []);
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
  const retryAbortControllerRef = useRef<AbortController | null>(null);
  const activeLayoutRef = useRef<LayoutRecord | null>(null);
  activeLayoutRef.current = activeLayout;

  useEffect(() => {
    return () => {
      activeAuthControllerRef.current?.abort();
      activeAuthControllerRef.current = null;
      retryAbortControllerRef.current?.abort();
      retryAbortControllerRef.current = null;
      wsClient.disconnect();
      coordinator.dispose();
    };
  }, [coordinator, wsClient]);

  const fetchContractsForModes = useCallback(
    async (
      modes: readonly boolean[],
      signal?: AbortSignal,
    ): Promise<FetchContractsBatchResult> => {
      const updates: Record<string, ContractResolutionEntry> = {};
      const fullyResolvedModes: boolean[] = [];
      const incompleteModes: boolean[] = [];
      const failureParts: string[] = [];

      for (const liveMode of modes) {
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
          resolvedModesRef.current.add(effectiveLive);
          resolvedModesRef.current.add(liveMode);
          fullyResolvedModes.push(liveMode);
        } else {
          resolvedModesRef.current.delete(effectiveLive);
          resolvedModesRef.current.delete(liveMode);
          incompleteModes.push(liveMode);
          if (failedForMode.length > 0) {
            failureParts.push(`${modeLabel}: ${failedForMode.join(", ")}`);
          } else {
            failureParts.push(`${modeLabel}: no contracts returned`);
          }
        }
      }

      return {
        updates,
        fullyResolvedModes,
        incompleteModes,
        partialFailureSummary:
          failureParts.length > 0
            ? `Incomplete contracts (${failureParts.join("; ")})`
            : null,
      };
    },
    [],
  );

  const retryUnresolvedContracts = useCallback(
    async (options?: { countAsAutoAttempt?: boolean; resetAttempt?: boolean }) => {
      retryAbortControllerRef.current?.abort();
      const controller = new AbortController();
      retryAbortControllerRef.current = controller;
      setIsRetryingContracts(true);
      if (options?.resetAttempt) {
        setRetrySchedule((prev) => ({ attempt: 0, nonce: prev.nonce + 1 }));
      }

      const modes =
        unresolvedModesRef.current.size > 0
          ? Array.from(unresolvedModesRef.current)
          : distinctPaneLiveModes(activeLayoutRef.current?.panes ?? []);

      try {
        const [statusResult, contractsResult] = await Promise.allSettled([
          fetchStatus(controller.signal),
          fetchContractsForModes(modes, controller.signal),
        ]);

        if (controller.signal.aborted) {
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
          const result = contractsResult.value;
          setContracts((prev) => mergeResolvedContracts(prev, result.updates));
          for (const mode of result.fullyResolvedModes) {
            unresolvedModesRef.current.delete(mode);
          }
          for (const mode of result.incompleteModes) {
            unresolvedModesRef.current.add(mode);
          }
          if (
            unresolvedModesRef.current.size === 0 &&
            result.incompleteModes.length === 0
          ) {
            contractsRetryNeededRef.current = false;
            setContractsPendingRetry(false);
            setRetrySchedule((prev) => ({ attempt: 0, nonce: prev.nonce + 1 }));
            setLoadError(null);
          } else {
            contractsRetryNeededRef.current = true;
            setContractsPendingRetry(true);
            setLoadError(result.partialFailureSummary);
          }
          return;
        }

        const err: unknown = contractsResult.reason;
        if (err instanceof DOMException && err.name === "AbortError") {
          return;
        }
        if (err instanceof ApiError && err.status === 503) {
          contractsRetryNeededRef.current = true;
          for (const mode of modes) {
            unresolvedModesRef.current.add(mode);
          }
          setContractsPendingRetry(true);
          return;
        }
        setLoadError(err instanceof Error ? err.message : "Failed to resolve contracts");
      } finally {
        if (retryAbortControllerRef.current === controller) {
          retryAbortControllerRef.current = null;
          setIsRetryingContracts(false);
          if (options?.countAsAutoAttempt && contractsRetryNeededRef.current) {
            setRetrySchedule((prev) => ({ attempt: prev.attempt + 1, nonce: prev.nonce }));
          }
        }
      }
    },
    [fetchContractsForModes],
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
    return () => {
      removeStateListener();
      removeStatusListener();
    };
  }, [retryUnresolvedContracts, wsClient]);

  const bootstrapWorkspace = useCallback(
    async (signal?: AbortSignal) => {
      setLoadError(null);
      retryAbortControllerRef.current?.abort();
      retryAbortControllerRef.current = null;
      contractsRetryNeededRef.current = false;
      unresolvedModesRef.current.clear();
      setIsRetryingContracts(false);
      setRetrySchedule((prev) => ({ attempt: 0, nonce: prev.nonce + 1 }));
      setContractsPendingRetry(false);

      const statusOpts = signal !== undefined ? signal : undefined;
      await fetchSession(statusOpts);
      if (signal?.aborted) {
        return;
      }
      const status = await fetchStatus(statusOpts);
      if (signal?.aborted) {
        return;
      }
      setStatusData(status);
      prevFeedStateRef.current = String(status.feed_state ?? "DISCONNECTED");

      const loadedLayouts = await listLayouts(statusOpts);
      if (signal?.aborted) {
        return;
      }
      setLayouts(loadedLayouts);
      const defaultLayout =
        loadedLayouts.find((item) => item.is_default) ?? loadedLayouts[0] ?? null;
      setActiveLayout(defaultLayout);
      activeLayoutRef.current = defaultLayout;

      resolvedModesRef.current.clear();
      const distinctModes = distinctPaneLiveModes(defaultLayout?.panes ?? []);
      try {
        const result = await fetchContractsForModes(distinctModes, statusOpts);
        if (signal?.aborted) {
          return;
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
        if (signal?.aborted) {
          return;
        }
        if (err instanceof ApiError && err.status === 503) {
          setContracts({});
          contractsRetryNeededRef.current = true;
          unresolvedModesRef.current = new Set(distinctModes);
          setContractsPendingRetry(true);
        } else {
          setLoadError(err instanceof Error ? err.message : "Failed to resolve contracts");
        }
      }

      if (signal?.aborted) {
        return;
      }
      wsClient.connect();
    },
    [fetchContractsForModes, wsClient],
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
        if (err instanceof ApiError && err.status === 401) {
          setAuthStage("unauthenticated");
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
  }, [bootstrapWorkspace]);

  const handleLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAuthError(null);
    activeAuthControllerRef.current?.abort();
    const controller = new AbortController();
    activeAuthControllerRef.current = controller;

    try {
      await login(passwordInput, controller.signal);
      if (controller.signal.aborted) {
        return;
      }
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
      setAuthError(err instanceof Error ? err.message : "Login failed");
    } finally {
      if (activeAuthControllerRef.current === controller) {
        activeAuthControllerRef.current = null;
      }
    }
  };

  const handleLogout = async () => {
    activeAuthControllerRef.current?.abort();
    activeAuthControllerRef.current = null;
    retryAbortControllerRef.current?.abort();
    retryAbortControllerRef.current = null;
    contractsRetryNeededRef.current = false;
    unresolvedModesRef.current.clear();
    prevFeedStateRef.current = "DISCONNECTED";
    setIsRetryingContracts(false);
    setRetrySchedule((prev) => ({ attempt: 0, nonce: prev.nonce + 1 }));
    setContractsPendingRetry(false);
    setLoadError(null);
    wsClient.disconnect();
    coordinator.dispose();
    try {
      await logout();
    } catch {
      // Clear local UI state even if session already expired
    }
    setActiveLayout(null);
    activeLayoutRef.current = null;
    setLayouts([]);
    setContracts({});
    resolvedModesRef.current.clear();
    setStatusData(null);
    setAuthStage("unauthenticated");
  };

  const handlePaneSymbolChange = (paneId: string, nextSymbol: RootSymbol) => {
    setActiveLayout((prev) => {
      if (prev === null) {
        return prev;
      }
      return {
        ...prev,
        panes: prev.panes.map((pane) =>
          pane.pane_id === paneId ? { ...pane, root_symbol: nextSymbol } : pane,
        ),
      };
    });
  };

  const handlePaneTimeframeChange = (paneId: string, nextTimeframe: Timeframe) => {
    setActiveLayout((prev) => {
      if (prev === null) {
        return prev;
      }
      return {
        ...prev,
        panes: prev.panes.map((pane) =>
          pane.pane_id === paneId ? { ...pane, timeframe: nextTimeframe } : pane,
        ),
      };
    });
  };

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

  const panes = activeLayout?.panes ?? [];

  return (
    <div className="terminal-shell">
      <header className="terminal-topbar">
        <div className="terminal-topbar__brand">
          <span>TopstepX Terminal</span>
          <span className="terminal-badge">
            {activeLayout !== null
              ? `${activeLayout.name} (${activeLayout.template_id})`
              : "No layout"}
          </span>
          {layouts.length > 1 && (
            <select
              aria-label="Active layout"
              value={activeLayout?.id ?? ""}
              onChange={(e) => {
                const picked = layouts.find((item) => item.id === e.target.value) ?? null;
                setActiveLayout(picked);
                activeLayoutRef.current = picked;
                if (picked !== null) {
                  const missingModes = distinctPaneLiveModes(picked.panes).filter(
                    (mode) => !resolvedModesRef.current.has(mode),
                  );
                  if (missingModes.length > 0) {
                    void fetchContractsForModes(missingModes)
                      .then((result) => {
                        setContracts((prev) => mergeResolvedContracts(prev, result.updates));
                        if (result.incompleteModes.length > 0) {
                          contractsRetryNeededRef.current = true;
                          for (const mode of result.incompleteModes) {
                            unresolvedModesRef.current.add(mode);
                          }
                          setContractsPendingRetry(true);
                          setLoadError(result.partialFailureSummary);
                        }
                      })
                      .catch((err: unknown) => {
                        if (err instanceof ApiError && err.status === 503) {
                          contractsRetryNeededRef.current = true;
                          for (const mode of missingModes) {
                            unresolvedModesRef.current.add(mode);
                          }
                          setContractsPendingRetry(true);
                          return;
                        }
                        if (!(err instanceof DOMException && err.name === "AbortError")) {
                          setLoadError(
                            err instanceof Error ? err.message : "Failed to resolve contracts",
                          );
                        }
                      });
                  }
                }
              }}
            >
              {layouts.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="terminal-topbar__meta">
          <span>WS: {wsState}</span>
          <span>Feed: {String(statusData?.feed_state ?? "DISCONNECTED")}</span>
          {contractsPendingRetry && (
            <>
              <span className="terminal-badge">Contracts pending</span>
              <button
                type="button"
                className="terminal-button"
                onClick={() => void retryUnresolvedContracts({ resetAttempt: true })}
              >
                Retry Contracts
              </button>
            </>
          )}
          {loadError !== null && <span className="login-error">{loadError}</span>}
          <button type="button" className="terminal-button" onClick={() => void handleLogout()}>
            Logout
          </button>
        </div>
      </header>
      <main className="terminal-grid" data-pane-count={panes.length}>
        {panes.map((pane) => (
          <ChartPane
            key={pane.pane_id}
            pane={pane}
            contractId={resolveContractId(
              contracts[contractCacheKey(pane.live_mode, pane.root_symbol)],
            )}
            activeFeedLive={statusData?.live ?? null}
            wsClient={wsClient}
            coordinator={coordinator}
            onSymbolChange={handlePaneSymbolChange}
            onTimeframeChange={handlePaneTimeframeChange}
          />
        ))}
      </main>
    </div>
  );
}
