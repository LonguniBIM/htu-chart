import { useEffect, useRef, useState } from "react";
import { useLayoutStore } from "../store/layoutStore";
import { TEMPLATES_BY_PANE_COUNT, getTemplateDefinition } from "./templates";

export interface LayoutPickerProps {
  onSelectLayout?: ((layoutId: string) => void) | undefined;
}

const PANE_COUNTS = [1, 2, 3, 4, 5, 6, 7, 8] as const;

export function LayoutPicker({ onSelectLayout }: LayoutPickerProps) {
  const layouts = useLayoutStore((state) => state.layouts);
  const activeLayout = useLayoutStore((state) => state.activeLayout);
  const dirty = useLayoutStore((state) => state.dirty);
  const saving = useLayoutStore((state) => state.saving);
  const error = useLayoutStore((state) => state.error);
  const selectLayout = useLayoutStore((state) => state.selectLayout);
  const setTemplate = useLayoutStore((state) => state.setTemplate);
  const saveActiveLayout = useLayoutStore((state) => state.saveActiveLayout);
  const saveLayoutAs = useLayoutStore((state) => state.saveLayoutAs);
  const renameActiveLayout = useLayoutStore((state) => state.renameActiveLayout);
  const setActiveLayoutDefault = useLayoutStore((state) => state.setActiveLayoutDefault);
  const deleteLayoutById = useLayoutStore((state) => state.deleteLayoutById);
  const clearError = useLayoutStore((state) => state.clearError);

  const [isOpen, setIsOpen] = useState(false);
  const [renameInput, setRenameInput] = useState("");
  const [saveAsName, setSaveAsName] = useState("");
  const [saveAsDefault, setSaveAsDefault] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setRenameInput(activeLayout?.name ?? "");
  }, [activeLayout?.id, activeLayout?.name]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsOpen(false);
      }
    };
    const handlePointerDown = (event: MouseEvent) => {
      if (
        rootRef.current !== null &&
        event.target instanceof Node &&
        !rootRef.current.contains(event.target)
      ) {
        setIsOpen(false);
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("mousedown", handlePointerDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("mousedown", handlePointerDown);
    };
  }, [isOpen]);

  const activeTemplateDef =
    activeLayout !== null ? getTemplateDefinition(activeLayout.template_id) : null;

  const handleSelectSavedLayout = (layoutId: string) => {
    clearError();
    if (onSelectLayout !== undefined) {
      onSelectLayout(layoutId);
    } else {
      selectLayout(layoutId);
    }
  };

  const handleSaveAs = async () => {
    const created = await saveLayoutAs(saveAsName, saveAsDefault);
    if (created !== null) {
      setSaveAsName("");
      setSaveAsDefault(false);
    }
  };

  const handleRename = async () => {
    await renameActiveLayout(renameInput);
  };

  return (
    <div ref={rootRef} className="layout-picker">
      <button
        type="button"
        className="terminal-button layout-picker__trigger"
        aria-haspopup="true"
        aria-expanded={isOpen}
        aria-label="Layout picker"
        onClick={() => setIsOpen((prev) => !prev)}
      >
        <svg
          className="layout-picker__icon"
          viewBox="0 0 24 24"
          width="16"
          height="16"
          aria-hidden="true"
        >
          {(activeTemplateDef?.previewRects ?? [{ x: 2, y: 2, width: 20, height: 20 }]).map(
            (rect, idx) => (
              <rect
                key={idx}
                x={rect.x}
                y={rect.y}
                width={rect.width}
                height={rect.height}
                rx="1"
              />
            ),
          )}
        </svg>
        <span className="layout-picker__label">
          {activeLayout !== null ? `${activeLayout.name}${dirty ? " *" : ""}` : "No layout"}
        </span>
      </button>

      {isOpen && (
        <div
          className="layout-picker__dropdown"
          role="dialog"
          aria-label="Layout setup"
        >
          <div className="layout-picker__section">
            <div className="layout-picker__section-title">Grid Templates (1–8 Panes)</div>
            <div className="layout-picker__groups">
              {PANE_COUNTS.map((count) => {
                const templates = TEMPLATES_BY_PANE_COUNT[count] ?? [];
                return (
                  <div key={count} className="layout-picker__group">
                    <span className="layout-picker__group-count">{count}</span>
                    <div className="layout-picker__template-row">
                      {templates.map((tpl) => {
                        const selected = activeLayout?.template_id === tpl.id;
                        return (
                          <button
                            key={tpl.id}
                            type="button"
                            className={
                              selected
                                ? "layout-picker__tpl layout-picker__tpl--active"
                                : "layout-picker__tpl"
                            }
                            data-template-id={tpl.id}
                            aria-label={tpl.label}
                            aria-pressed={selected}
                            title={tpl.label}
                            onClick={() => setTemplate(tpl.id)}
                          >
                            <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
                              {tpl.previewRects.map((rect, idx) => (
                                <rect
                                  key={idx}
                                  x={rect.x}
                                  y={rect.y}
                                  width={rect.width}
                                  height={rect.height}
                                  rx="1"
                                />
                              ))}
                            </svg>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="layout-picker__section">
            <div className="layout-picker__section-title">Saved Layouts</div>
            <ul className="layout-picker__saved-list">
              {layouts.map((item) => {
                const isSelected = item.id === activeLayout?.id;
                return (
                  <li key={item.id} className="layout-picker__saved-item">
                    <button
                      type="button"
                      className={
                        isSelected
                          ? "layout-picker__saved-name layout-picker__saved-name--active"
                          : "layout-picker__saved-name"
                      }
                      aria-pressed={isSelected}
                      onClick={() => handleSelectSavedLayout(item.id)}
                    >
                      {item.name}
                    </button>
                    {item.is_default ? (
                      <span className="terminal-badge">Default</span>
                    ) : (
                      isSelected && (
                        <button
                          type="button"
                          className="terminal-button"
                          disabled={saving}
                          onClick={() => void setActiveLayoutDefault()}
                        >
                          Set Default
                        </button>
                      )
                    )}
                    <button
                      type="button"
                      className="terminal-button"
                      aria-label={`Delete ${item.name}`}
                      disabled={layouts.length <= 1 || saving}
                      onClick={() => void deleteLayoutById(item.id)}
                    >
                      Delete
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>

          <div className="layout-picker__section layout-picker__actions">
            <div className="layout-picker__row">
              <button
                type="button"
                className="terminal-button"
                disabled={activeLayout === null || saving}
                onClick={() => void saveActiveLayout()}
              >
                {saving ? "Saving..." : "Save"}
              </button>
            </div>

            <div className="layout-picker__row">
              <input
                type="text"
                aria-label="Rename layout"
                placeholder="Rename active layout"
                value={renameInput}
                onChange={(e) => setRenameInput(e.target.value)}
              />
              <button
                type="button"
                className="terminal-button"
                disabled={activeLayout === null || saving}
                onClick={() => void handleRename()}
              >
                Rename
              </button>
            </div>

            <div className="layout-picker__row">
              <input
                type="text"
                aria-label="Save layout as"
                placeholder="New layout name"
                value={saveAsName}
                onChange={(e) => setSaveAsName(e.target.value)}
              />
              <label className="layout-picker__checkbox">
                <input
                  type="checkbox"
                  checked={saveAsDefault}
                  onChange={(e) => setSaveAsDefault(e.target.checked)}
                />
                Default
              </label>
              <button
                type="button"
                className="terminal-button"
                disabled={activeLayout === null || saving}
                onClick={() => void handleSaveAs()}
              >
                Save As
              </button>
            </div>

            {error !== null && (
              <div className="layout-picker__error" role="alert">
                {error}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
