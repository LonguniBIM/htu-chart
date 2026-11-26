import type { DrawingScopeTarget } from "../store/drawingStore";
import { buildDrawingScopeKey, useDrawingStore } from "../store/drawingStore";
import type { DrawingToolId, FvgPreset } from "./types";

export interface DrawingToolbarProps {
  activeScope: DrawingScopeTarget | null;
}

interface ToolButtonDef {
  id: DrawingToolId;
  label: string;
  shortLabel: string;
}

const TOOL_BUTTONS: readonly ToolButtonDef[] = [
  { id: "cursor", label: "Cursor tool", shortLabel: "Cur" },
  { id: "trendline", label: "Trendline tool", shortLabel: "TL" },
  { id: "ray", label: "Ray tool", shortLabel: "Ray" },
  { id: "hline", label: "Horizontal line tool", shortLabel: "HL" },
  { id: "fvg_box", label: "FVG box tool", shortLabel: "FVG" },
  { id: "order_block", label: "Order block tool", shortLabel: "OB" },
  { id: "long_position", label: "Long position tool", shortLabel: "Long" },
  { id: "short_position", label: "Short position tool", shortLabel: "Short" },
  { id: "measure", label: "Measure tool", shortLabel: "Meas" },
];

export function DrawingToolbar({ activeScope }: DrawingToolbarProps) {
  const activeTool = useDrawingStore((s) => s.activeTool);
  const magnetEnabled = useDrawingStore((s) => s.magnetEnabled);
  const fvgPreset = useDrawingStore((s) => s.fvgPreset);
  const obExtendRight = useDrawingStore((s) => s.obExtendRight);
  const setActiveTool = useDrawingStore((s) => s.setActiveTool);
  const toggleMagnet = useDrawingStore((s) => s.toggleMagnet);
  const setFvgPreset = useDrawingStore((s) => s.setFvgPreset);
  const toggleObExtendRight = useDrawingStore((s) => s.toggleObExtendRight);
  const undoScope = useDrawingStore((s) => s.undoScope);
  const redoScope = useDrawingStore((s) => s.redoScope);
  const deleteDrawing = useDrawingStore((s) => s.deleteDrawing);
  const clearScopeDrawings = useDrawingStore((s) => s.clearScopeDrawings);

  const scopeKey =
    activeScope !== null
      ? buildDrawingScopeKey(activeScope.symbol, activeScope.contractId, activeScope.paneId)
      : "";
  const scopeState = useDrawingStore((s) => (scopeKey ? s.scopes[scopeKey] : undefined));
  const selectedDrawingId = useDrawingStore((s) =>
    scopeKey ? (s.selectedByScope[scopeKey] ?? null) : null,
  );

  const hasReadyScope =
    activeScope !== null &&
    Boolean(activeScope.contractId && activeScope.contractId.trim().length > 0);
  const mutating = scopeState?.mutating ?? false;
  const canUndo = hasReadyScope && (scopeState?.canUndo ?? false) && !mutating;
  const canRedo = hasReadyScope && (scopeState?.canRedo ?? false) && !mutating;
  const canDeleteSelected = hasReadyScope && selectedDrawingId !== null && !mutating;
  const canClearPane =
    hasReadyScope && (scopeState?.drawings.length ?? 0) > 0 && !mutating;

  return (
    <aside className="drawing-toolbar" aria-label="Drawing toolbar">
      <div className="drawing-toolbar__group" role="group" aria-label="Drawing tools">
        {TOOL_BUTTONS.map((tool) => {
          const pressed = activeTool === tool.id;
          return (
            <button
              key={tool.id}
              type="button"
              className={
                pressed
                  ? "drawing-toolbar__btn drawing-toolbar__btn--active"
                  : "drawing-toolbar__btn"
              }
              aria-label={tool.label}
              aria-pressed={pressed}
              title={tool.label}
              onClick={() => setActiveTool(tool.id)}
            >
              {tool.shortLabel}
            </button>
          );
        })}
      </div>

      <div className="drawing-toolbar__divider" />

      <div className="drawing-toolbar__group" role="group" aria-label="Drawing options">
        <button
          type="button"
          className={
            magnetEnabled
              ? "drawing-toolbar__btn drawing-toolbar__btn--active"
              : "drawing-toolbar__btn"
          }
          aria-label="Magnet Mode"
          aria-pressed={magnetEnabled}
          title="Magnet Mode (OHLC snap)"
          onClick={() => toggleMagnet()}
        >
          Mag
        </button>

        <select
          aria-label="FVG Preset"
          className="drawing-toolbar__select"
          value={fvgPreset}
          title="FVG Direction Preset"
          onChange={(e) => setFvgPreset(e.target.value as FvgPreset)}
        >
          <option value="auto">Auto</option>
          <option value="bullish">Bull</option>
          <option value="bearish">Bear</option>
        </select>

        <button
          type="button"
          className={
            obExtendRight
              ? "drawing-toolbar__btn drawing-toolbar__btn--active"
              : "drawing-toolbar__btn"
          }
          aria-label="OB Extend Right"
          aria-pressed={obExtendRight}
          title="Order Block Extend Right"
          onClick={() => toggleObExtendRight()}
        >
          Ext
        </button>
      </div>

      <div className="drawing-toolbar__divider" />

      <div className="drawing-toolbar__group" role="group" aria-label="Drawing actions">
        <button
          type="button"
          className="drawing-toolbar__btn"
          aria-label="Undo drawing"
          title="Undo (Ctrl+Z)"
          disabled={!canUndo}
          onClick={() => {
            if (activeScope !== null) {
              void undoScope(activeScope);
            }
          }}
        >
          Undo
        </button>

        <button
          type="button"
          className="drawing-toolbar__btn"
          aria-label="Redo drawing"
          title="Redo (Ctrl+Y)"
          disabled={!canRedo}
          onClick={() => {
            if (activeScope !== null) {
              void redoScope(activeScope);
            }
          }}
        >
          Redo
        </button>

        <button
          type="button"
          className="drawing-toolbar__btn"
          aria-label="Delete Selected"
          title="Delete Selected (Del)"
          disabled={!canDeleteSelected}
          onClick={() => {
            if (activeScope !== null && selectedDrawingId !== null) {
              void deleteDrawing(activeScope, selectedDrawingId);
            }
          }}
        >
          Del
        </button>

        <button
          type="button"
          className="drawing-toolbar__btn drawing-toolbar__btn--danger"
          aria-label="Clear Pane"
          title="Clear all drawings in pane"
          disabled={!canClearPane}
          onClick={() => {
            if (activeScope !== null) {
              void clearScopeDrawings(activeScope);
            }
          }}
        >
          Clr
        </button>
      </div>

      {scopeState?.error !== null && scopeState?.error !== undefined && (
        <div
          className="drawing-toolbar__error"
          role="status"
          title={scopeState.error}
        >
          !
        </div>
      )}
    </aside>
  );
}
