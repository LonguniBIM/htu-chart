import type { StatusResponse } from "../api/types";
import type { WsConnectionState } from "../api/wsClient";
import { useLayoutStore } from "../store/layoutStore";
import { LayoutPicker } from "./LayoutPicker";
import { SyncControls } from "./SyncControls";

export interface TopToolbarProps {
  wsState: WsConnectionState;
  statusData: StatusResponse | null;
  contractsPendingRetry: boolean;
  loadError: string | null;
  onSelectLayout: (layoutId: string) => void;
  onRetryContracts: () => void;
  onLogout: () => void;
}

export function TopToolbar({
  wsState,
  statusData,
  contractsPendingRetry,
  loadError,
  onSelectLayout,
  onRetryContracts,
  onLogout,
}: TopToolbarProps) {
  const layouts = useLayoutStore((state) => state.layouts);
  const activeLayout = useLayoutStore((state) => state.activeLayout);

  return (
    <header className="terminal-topbar">
      <div className="terminal-topbar__brand">
        <span>TopstepX Terminal</span>
        <LayoutPicker onSelectLayout={onSelectLayout} />
        <span className="terminal-badge">
          {activeLayout !== null
            ? `${activeLayout.name} (${activeLayout.template_id})`
            : "No layout"}
        </span>
        {layouts.length > 1 && (
          <select
            aria-label="Active layout"
            className="terminal-topbar__select"
            value={activeLayout?.id ?? ""}
            onChange={(e) => {
              onSelectLayout(e.target.value);
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

      <SyncControls />

      <div className="terminal-topbar__meta">
        <span>WS: {wsState}</span>
        <span>Feed: {String(statusData?.feed_state ?? "DISCONNECTED")}</span>
        {statusData?.key_rotation_due === true && (
          <span className="chart-pane__quality-warning">
            Key age: {statusData.key_age_days ?? "?"}d
          </span>
        )}
        {contractsPendingRetry && (
          <>
            <span className="terminal-badge">Contracts pending</span>
            <button
              type="button"
              className="terminal-button"
              onClick={onRetryContracts}
            >
              Retry Contracts
            </button>
          </>
        )}
        {loadError !== null && <span className="login-error">{loadError}</span>}
        <button type="button" className="terminal-button" onClick={onLogout}>
          Logout
        </button>
      </div>
    </header>
  );
}
