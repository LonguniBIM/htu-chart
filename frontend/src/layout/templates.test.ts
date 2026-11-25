import { describe, expect, it } from "vitest";
import { LAYOUT_TEMPLATE_PANE_COUNTS, type LayoutTemplateId } from "../api/types";
import {
  LAYOUT_TEMPLATES,
  TEMPLATES_BY_PANE_COUNT,
  getTemplateDefinition,
} from "./templates";

const GRID_SPAN_RE = /^(\d+)\s*\/\s*(\d+)$/;

function parseGridSpan(span: string): [number, number] {
  const match = GRID_SPAN_RE.exec(span);
  expect(match).not.toBeNull();
  const start = Number(match![1]);
  const end = Number(match![2]);
  expect(start).toBeGreaterThanOrEqual(1);
  expect(end).toBeGreaterThan(start);
  return [start, end];
}

function rectsOverlap(
  a: { x1: number; x2: number; y1: number; y2: number },
  b: { x1: number; x2: number; y1: number; y2: number },
): boolean {
  return a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;
}

describe("Layout templates (Phase 3 Part 2)", () => {
  it("defines all 21 canonical LAYOUT_TEMPLATE_PANE_COUNTS templates with matching paneAreas and previewRects", () => {
    /**
     * Contract: Every template id in LAYOUT_TEMPLATE_PANE_COUNTS is defined in LAYOUT_TEMPLATES
     * and getTemplateDefinition(), with paneAreas and previewRects lengths matching paneCount
     * and previewRects strictly bounded inside the 0..24 SVG viewBox.
     * Defect caught: Missing template id, mismatched paneAreas/previewRects count, or out-of-bounds SVG rects.
     */
    const expectedIds = Object.keys(LAYOUT_TEMPLATE_PANE_COUNTS) as LayoutTemplateId[];
    expect(expectedIds).toHaveLength(21);
    expect(LAYOUT_TEMPLATES).toHaveLength(21);

    const seenIds = new Set<LayoutTemplateId>();
    for (const id of expectedIds) {
      const def = getTemplateDefinition(id);
      const expectedCount = LAYOUT_TEMPLATE_PANE_COUNTS[id];
      expect(def).toBeDefined();
      expect(def.id).toBe(id);
      expect(def.paneCount).toBe(expectedCount);
      expect(def.paneAreas).toHaveLength(expectedCount);
      expect(def.previewRects).toHaveLength(expectedCount);
      expect(def.label.trim().length).toBeGreaterThan(0);
      expect(def.gridTemplateColumns.trim().length).toBeGreaterThan(0);
      expect(def.gridTemplateRows.trim().length).toBeGreaterThan(0);
      seenIds.add(id);

      for (const area of def.paneAreas) {
        const colMatch = GRID_SPAN_RE.exec(area.gridColumn);
        const rowMatch = GRID_SPAN_RE.exec(area.gridRow);
        expect(colMatch).not.toBeNull();
        expect(rowMatch).not.toBeNull();
        expect(Number(colMatch![2])).toBeGreaterThan(Number(colMatch![1]));
        expect(Number(rowMatch![2])).toBeGreaterThan(Number(rowMatch![1]));
      }

      for (const rect of def.previewRects) {
        expect(rect.x).toBeGreaterThanOrEqual(0);
        expect(rect.y).toBeGreaterThanOrEqual(0);
        expect(rect.width).toBeGreaterThan(0);
        expect(rect.height).toBeGreaterThan(0);
        expect(rect.x + rect.width).toBeLessThanOrEqual(24);
        expect(rect.y + rect.height).toBeLessThanOrEqual(24);
      }
    }

    expect(seenIds.size).toBe(21);
  });

  it("groups templates by pane counts 1 through 8 with 4-grid-2x2 in group 4", () => {
    /**
     * Contract: TEMPLATES_BY_PANE_COUNT covers pane counts 1..8, sums to all 21 templates,
     * and places 4-grid-2x2 under count 4.
     * Defect caught: Grouping omission or wrong paneCount bucket in LayoutPicker.
     */
    let total = 0;
    for (let count = 1; count <= 8; count += 1) {
      const bucket = TEMPLATES_BY_PANE_COUNT[count];
      expect(bucket).toBeDefined();
      expect(bucket!.length).toBeGreaterThan(0);
      for (const tpl of bucket!) {
        expect(tpl.paneCount).toBe(count);
        total += 1;
      }
    }
    expect(total).toBe(21);
    expect(TEMPLATES_BY_PANE_COUNT[4]!.some((t) => t.id === "4-grid-2x2")).toBe(true);
  });

  it("ensures pairwise non-overlapping paneAreas and previewRects within all 21 templates", () => {
    /**
     * Contract: Within each of the 21 layout templates, no two paneAreas overlap in grid space
     * and no two previewRects overlap in the 0..24 SVG viewBox.
     * Defect caught: Overlapping grid track spans or colliding SVG preview rectangles.
     */
    expect(LAYOUT_TEMPLATES).toHaveLength(21);

    for (const tpl of LAYOUT_TEMPLATES) {
      const parsedAreas = tpl.paneAreas.map((area) => {
        const [x1, x2] = parseGridSpan(area.gridColumn);
        const [y1, y2] = parseGridSpan(area.gridRow);
        return { x1, x2, y1, y2 };
      });

      for (let i = 0; i < parsedAreas.length; i += 1) {
        for (let j = i + 1; j < parsedAreas.length; j += 1) {
          expect(
            rectsOverlap(parsedAreas[i]!, parsedAreas[j]!),
            `paneAreas overlap in ${tpl.id} between pane ${i} and ${j}`,
          ).toBe(false);
        }
      }

      const parsedPreview = tpl.previewRects.map((rect) => ({
        x1: rect.x,
        x2: rect.x + rect.width,
        y1: rect.y,
        y2: rect.y + rect.height,
      }));

      for (let i = 0; i < parsedPreview.length; i += 1) {
        for (let j = i + 1; j < parsedPreview.length; j += 1) {
          expect(
            rectsOverlap(parsedPreview[i]!, parsedPreview[j]!),
            `previewRects overlap in ${tpl.id} between rect ${i} and ${j}`,
          ).toBe(false);
        }
      }
    }
  });
});
