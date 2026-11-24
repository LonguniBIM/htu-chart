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
  const contractsRetryNeededRef = useRef<boolean>(false);
  const unresolvedModesRef = useRef<Set<boolean>>(new Set());
  const prevFeedStateRef = useRef<string>("DISCONNECTED");
  const retryAbortControllerRef = useRef<AbortController | null>(null);
  const activeLayoutRef = useRef<LayoutRecord | null>(null);
  activeLayoutRef.current = activeLayout;

  useEffect(() => {
    return () => {
      retryAbortControllerRef.current?.abort();
      retryAbortControllerRef.current = null;
      wsClient.disconnect();
      coordinator.dispose();
    };
  }, [coordinator, wsClient]);

  const fetchContractsForModes = useCallback(
    async (modes: readonly boolean[], signal?: AbortSignal): Promise<Record<string, ContractResolutionEntry>> => {
      const updates: Record<string, ContractResolutionEntry> = {};
      for (const liveMode of modes) {
        const resolved = await fetchContracts(
          ["MES", "MNQ", "MYM", "MGC"],
          liveMode,
          signal,
        );
        const effectiveLive = Boolean(resolved.live);
        resolvedModesRef.current.add(effectiveLive);
        for (const [symbol, entry] of Object.entries(resolved.contracts)) {
          updates[`${effectiveLive ? "live" : "sim"}:${symbol}`] = entry;
        }
      }
      return updates;
    },
    [],
  );

  const retryUnresolvedContracts = useCallback(async () => {
    retryAbortControllerRef.current?.abort();
    const controller = new AbortController();
    retryAbortControllerRef.current = controller;

    const modes =
      unresolvedModesRef.current.size > 0
        ? Array.from(unresolvedModesRef.current)
        : distinctPaneLiveModes(activeLayoutRef.current?.panes ?? []);

    try {
      const statusPromise = fetchStatus(controller.signal).then((freshStatus) => {
        if (controller.signal.aborted) {
          return;
        }
        setStatusData(freshStatus);
        if (freshStatus.feed_state !== undefined) {
          prevFeedStateRef.current = String(freshStatus.feed_state);
        }
      });

      const resolvedMap = await fetchContractsForModes(modes, controller.signal);
      await statusPromise.catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") {
          throw err;
        }
      });

      if (controller.signal.aborted) {
        return;
      }

      setContracts((prev) => ({ ...prev, ...resolvedMap }));
      for (const mode of modes) {
        unresolvedModesRef.current.delete(mode);
      }
      if (unresolvedModesRef.current.size === 0) {
        contractsRetryNeededRef.current = false;
        setContractsPendingRetry(false);
      }
      setLoadError(null);
    } catch (err: unknown) {
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
      }
    }
  }, [fetchContractsForModes]);

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
        const resolvedMap = await fetchContractsForModes(distinctModes, statusOpts);
        if (signal?.aborted) {
          return;
        }
        setContracts(resolvedMap);
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
    const controller = new AbortController();
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
      });
    return () => {
      controller.abort();
    };
  }, [bootstrapWorkspace]);

  const handleLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAuthError(null);
    try {
      await login(passwordInput);
      setPasswordInput("");
      await bootstrapWorkspace();
      setAuthStage("authenticated");
    } catch (err: unknown) {
      setAuthError(err instanceof Error ? err.message : "Login failed");
    }
  };

  const handleLogout = async () => {
    retryAbortControllerRef.current?.abort();
    retryAbortControllerRef.current = null;
    contractsRetryNeededRef.current = false;
    unresolvedModesRef.current.clear();
    prevFeedStateRef.current = "DISCONNECTED";
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
                      .then((updates) => {
                        setContracts((prev) => ({ ...prev, ...updates }));
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
                onClick={() => void retryUnresolvedContracts()}
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
