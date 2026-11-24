import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  ApiError,
  fetchContracts,
  fetchStatus,
  listLayouts,
  login,
  logout,
} from "./api/client";
import type {
  ContractResolutionEntry,
  LayoutRecord,
  RootSymbol,
  StatusResponse,
  Timeframe,
} from "./api/types";
import { MarketWsClient, type WsConnectionState } from "./api/wsClient";
import { ChartPane } from "./chart/ChartPane";
import { PaneFetchCoordinator } from "./chart/fetchGuard";

type AuthStage = "checking" | "unauthenticated" | "authenticated";

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

  useEffect(() => {
    return () => {
      wsClient.disconnect();
      coordinator.dispose();
    };
  }, [coordinator, wsClient]);

  useEffect(() => {
    const removeStateListener = wsClient.onConnectionState(setWsState);
    const removeStatusListener = wsClient.onFeedStatus((msg) => {
      setStatusData((prev) => ({
        ...(prev ?? {}),
        ...(msg.feed_state !== undefined ? { feed_state: msg.feed_state } : {}),
      }));
    });
    return () => {
      removeStateListener();
      removeStatusListener();
    };
  }, [wsClient]);

  const bootstrapWorkspace = useCallback(
    async (signal?: AbortSignal) => {
      setLoadError(null);
      const statusOpts = signal !== undefined ? signal : undefined;
      const status = await fetchStatus(statusOpts);
      setStatusData(status);

      const loadedLayouts = await listLayouts(statusOpts);
      setLayouts(loadedLayouts);
      const defaultLayout =
        loadedLayouts.find((item) => item.is_default) ?? loadedLayouts[0] ?? null;
      setActiveLayout(defaultLayout);

      try {
        const resolved = await fetchContracts(
          ["MES", "MNQ", "MYM", "MGC"],
          undefined,
          statusOpts,
        );
        setContracts(resolved.contracts);
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 503) {
          setContracts({});
        } else if (!(err instanceof DOMException && err.name === "AbortError")) {
          setLoadError(err instanceof Error ? err.message : "Failed to resolve contracts");
        }
      }

      wsClient.connect();
    },
    [wsClient],
  );

  useEffect(() => {
    const controller = new AbortController();
    void bootstrapWorkspace(controller.signal)
      .then(() => {
        setAuthStage("authenticated");
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") {
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
    wsClient.disconnect();
    coordinator.dispose();
    try {
      await logout();
    } catch {
      // Clear local UI state even if session already expired
    }
    setActiveLayout(null);
    setLayouts([]);
    setContracts({});
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
            contractId={resolveContractId(contracts[pane.root_symbol])}
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
