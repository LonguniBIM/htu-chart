import type { SyncConfig } from "../api/types";
import { useLayoutStore } from "../store/layoutStore";

interface SyncToggleItem {
  readonly key: keyof SyncConfig;
  readonly label: string;
}

const SYNC_ITEMS: readonly SyncToggleItem[] = [
  { key: "symbol", label: "Symbol" },
  { key: "interval", label: "Interval" },
  { key: "crosshair", label: "Crosshair" },
  { key: "time", label: "Time" },
  { key: "dateRange", label: "Date range" },
];

export function SyncControls() {
  const syncConfig = useLayoutStore((state) => state.activeLayout?.sync_config ?? null);
  const toggleSync = useLayoutStore((state) => state.toggleSync);

  return (
    <div className="sync-controls" role="group" aria-label="SYNC IN LAYOUT">
      <span className="sync-controls__label">SYNC IN LAYOUT</span>
      {SYNC_ITEMS.map((item) => {
        const checked = syncConfig !== null ? Boolean(syncConfig[item.key]) : false;
        return (
          <button
            key={item.key}
            type="button"
            role="switch"
            aria-checked={checked}
            aria-label={`Sync ${item.label}`}
            className={
              checked
                ? "sync-controls__toggle sync-controls__toggle--active"
                : "sync-controls__toggle"
            }
            disabled={syncConfig === null}
            onClick={() => toggleSync(item.key)}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
