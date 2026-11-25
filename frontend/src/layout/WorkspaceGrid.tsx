import type {
  ContractResolutionEntry,
  LayoutRecord,
  RootSymbol,
  Timeframe,
} from "../api/types";
import type { MarketWsClient } from "../api/wsClient";
import { ChartPane } from "../chart/ChartPane";
import type { PaneFetchCoordinator } from "../chart/fetchGuard";
import type { LayoutSyncController } from "./syncController";
import { getTemplateDefinition } from "./templates";

export interface WorkspaceGridProps {
  layout: LayoutRecord | null;
  activePaneId: string | null;
  contracts: Record<string, ContractResolutionEntry>;
  activeFeedLive: boolean | null;
  wsClient: MarketWsClient;
  coordinator: PaneFetchCoordinator;
  syncController: LayoutSyncController;
  onSelectPane: (paneId: string) => void;
  onSymbolChange: (paneId: string, symbol: RootSymbol) => void;
  onTimeframeChange: (paneId: string, timeframe: Timeframe) => void;
  onAuthError: (status: number, message: string) => void;
}

function resolveContractId(entry: ContractResolutionEntry | undefined): string | null {
  if (entry === undefined || entry.selected === null) {
    return null;
  }
  return entry.selected.id ?? entry.selected.contractId ?? null;
}

function contractCacheKey(liveMode: boolean, rootSymbol: RootSymbol): string {
  return `${liveMode ? "live" : "sim"}:${rootSymbol}`;
}

export function WorkspaceGrid({
  layout,
  activePaneId,
  contracts,
  activeFeedLive,
  wsClient,
  coordinator,
  syncController,
  onSelectPane,
  onSymbolChange,
  onTimeframeChange,
  onAuthError,
}: WorkspaceGridProps) {
  const panes = layout?.panes ?? [];
  const template = layout !== null ? getTemplateDefinition(layout.template_id) : null;

  return (
    <main
      className="terminal-grid"
      data-pane-count={panes.length}
      data-template-id={template?.id ?? ""}
      style={
        template !== null
          ? {
              gridTemplateColumns: template.gridTemplateColumns,
              gridTemplateRows: template.gridTemplateRows,
            }
          : undefined
      }
    >
      {panes.map((pane, idx) => (
        <ChartPane
          key={pane.pane_id}
          pane={pane}
          contractId={resolveContractId(
            contracts[contractCacheKey(pane.live_mode, pane.root_symbol)],
          )}
          activeFeedLive={activeFeedLive}
          wsClient={wsClient}
          coordinator={coordinator}
          syncController={syncController}
          isActive={pane.pane_id === activePaneId}
          onSelectPane={onSelectPane}
          gridStyle={template?.paneAreas[idx]}
          onSymbolChange={onSymbolChange}
          onTimeframeChange={onTimeframeChange}
          onAuthError={onAuthError}
        />
      ))}
    </main>
  );
}
