import type { LayoutTemplateId } from "../api/types";

export interface PaneGridArea {
  readonly gridColumn: string;
  readonly gridRow: string;
}

export interface PreviewRect {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

export interface LayoutTemplateDefinition {
  readonly id: LayoutTemplateId;
  readonly label: string;
  readonly paneCount: number;
  readonly gridTemplateColumns: string;
  readonly gridTemplateRows: string;
  readonly paneAreas: readonly PaneGridArea[];
  readonly previewRects: readonly PreviewRect[];
}

interface NormalizedRectSpec {
  readonly colStart: number;
  readonly colSpan: number;
  readonly totalCols: number;
  readonly rowStart: number;
  readonly rowSpan: number;
  readonly totalRows: number;
}

const VIEWBOX_PADDING = 2;
const VIEWBOX_INNER_SIZE = 20;
const PREVIEW_GAP = 1.5;

function toPreviewRect(spec: NormalizedRectSpec): PreviewRect {
  const cellW = (VIEWBOX_INNER_SIZE - PREVIEW_GAP * (spec.totalCols - 1)) / spec.totalCols;
  const cellH = (VIEWBOX_INNER_SIZE - PREVIEW_GAP * (spec.totalRows - 1)) / spec.totalRows;

  const x = VIEWBOX_PADDING + spec.colStart * (cellW + PREVIEW_GAP);
  const y = VIEWBOX_PADDING + spec.rowStart * (cellH + PREVIEW_GAP);
  const width = spec.colSpan * cellW + (spec.colSpan - 1) * PREVIEW_GAP;
  const height = spec.rowSpan * cellH + (spec.rowSpan - 1) * PREVIEW_GAP;

  return {
    x: Number(x.toFixed(2)),
    y: Number(y.toFixed(2)),
    width: Number(width.toFixed(2)),
    height: Number(height.toFixed(2)),
  };
}

function buildUniformGrid(
  id: LayoutTemplateId,
  label: string,
  cols: number,
  rows: number,
): LayoutTemplateDefinition {
  const paneAreas: PaneGridArea[] = [];
  const previewRects: PreviewRect[] = [];
  for (let r = 0; r < rows; r += 1) {
    for (let c = 0; c < cols; c += 1) {
      paneAreas.push({
        gridColumn: `${c + 1} / ${c + 2}`,
        gridRow: `${r + 1} / ${r + 2}`,
      });
      previewRects.push(
        toPreviewRect({
          colStart: c,
          colSpan: 1,
          totalCols: cols,
          rowStart: r,
          rowSpan: 1,
          totalRows: rows,
        }),
      );
    }
  }
  return {
    id,
    label,
    paneCount: cols * rows,
    gridTemplateColumns: cols === 1 ? "1fr" : `repeat(${cols}, minmax(0, 1fr))`,
    gridTemplateRows: rows === 1 ? "1fr" : `repeat(${rows}, minmax(0, 1fr))`,
    paneAreas,
    previewRects,
  };
}

function buildCustomTemplate(
  id: LayoutTemplateId,
  label: string,
  totalCols: number,
  totalRows: number,
  cells: readonly { colStart: number; colSpan: number; rowStart: number; rowSpan: number }[],
): LayoutTemplateDefinition {
  const paneAreas: PaneGridArea[] = cells.map((cell) => ({
    gridColumn: `${cell.colStart + 1} / ${cell.colStart + cell.colSpan + 1}`,
    gridRow: `${cell.rowStart + 1} / ${cell.rowStart + cell.rowSpan + 1}`,
  }));
  const previewRects: PreviewRect[] = cells.map((cell) =>
    toPreviewRect({
      colStart: cell.colStart,
      colSpan: cell.colSpan,
      totalCols,
      rowStart: cell.rowStart,
      rowSpan: cell.rowSpan,
      totalRows,
    }),
  );

  return {
    id,
    label,
    paneCount: cells.length,
    gridTemplateColumns: totalCols === 1 ? "1fr" : `repeat(${totalCols}, minmax(0, 1fr))`,
    gridTemplateRows: totalRows === 1 ? "1fr" : `repeat(${totalRows}, minmax(0, 1fr))`,
    paneAreas,
    previewRects,
  };
}

export const LAYOUT_TEMPLATES: readonly LayoutTemplateDefinition[] = [
  buildUniformGrid("1-single", "1 Pane", 1, 1),
  buildUniformGrid("2-cols", "2 Columns", 2, 1),
  buildUniformGrid("2-rows", "2 Rows", 1, 2),
  buildUniformGrid("3-cols", "3 Columns", 3, 1),
  buildUniformGrid("3-rows", "3 Rows", 1, 3),
  buildCustomTemplate("3-left-main", "3 Left Main", 2, 2, [
    { colStart: 0, colSpan: 1, rowStart: 0, rowSpan: 2 },
    { colStart: 1, colSpan: 1, rowStart: 0, rowSpan: 1 },
    { colStart: 1, colSpan: 1, rowStart: 1, rowSpan: 1 },
  ]),
  buildCustomTemplate("3-right-main", "3 Right Main", 2, 2, [
    { colStart: 0, colSpan: 1, rowStart: 0, rowSpan: 1 },
    { colStart: 0, colSpan: 1, rowStart: 1, rowSpan: 1 },
    { colStart: 1, colSpan: 1, rowStart: 0, rowSpan: 2 },
  ]),
  buildCustomTemplate("3-top-main", "3 Top Main", 2, 2, [
    { colStart: 0, colSpan: 2, rowStart: 0, rowSpan: 1 },
    { colStart: 0, colSpan: 1, rowStart: 1, rowSpan: 1 },
    { colStart: 1, colSpan: 1, rowStart: 1, rowSpan: 1 },
  ]),
  buildCustomTemplate("3-bottom-main", "3 Bottom Main", 2, 2, [
    { colStart: 0, colSpan: 1, rowStart: 0, rowSpan: 1 },
    { colStart: 1, colSpan: 1, rowStart: 0, rowSpan: 1 },
    { colStart: 0, colSpan: 2, rowStart: 1, rowSpan: 1 },
  ]),
  buildUniformGrid("4-grid-2x2", "4 Grid (2x2)", 2, 2),
  buildUniformGrid("4-cols", "4 Columns", 4, 1),
  buildUniformGrid("4-rows", "4 Rows", 1, 4),
  buildCustomTemplate("4-left-main", "4 Left Main", 2, 3, [
    { colStart: 0, colSpan: 1, rowStart: 0, rowSpan: 3 },
    { colStart: 1, colSpan: 1, rowStart: 0, rowSpan: 1 },
    { colStart: 1, colSpan: 1, rowStart: 1, rowSpan: 1 },
    { colStart: 1, colSpan: 1, rowStart: 2, rowSpan: 1 },
  ]),
  buildCustomTemplate("4-top-main", "4 Top Main", 3, 2, [
    { colStart: 0, colSpan: 3, rowStart: 0, rowSpan: 1 },
    { colStart: 0, colSpan: 1, rowStart: 1, rowSpan: 1 },
    { colStart: 1, colSpan: 1, rowStart: 1, rowSpan: 1 },
    { colStart: 2, colSpan: 1, rowStart: 1, rowSpan: 1 },
  ]),
  buildCustomTemplate("5-top2-bot3", "5 Top 2 / Bottom 3", 6, 2, [
    { colStart: 0, colSpan: 3, rowStart: 0, rowSpan: 1 },
    { colStart: 3, colSpan: 3, rowStart: 0, rowSpan: 1 },
    { colStart: 0, colSpan: 2, rowStart: 1, rowSpan: 1 },
    { colStart: 2, colSpan: 2, rowStart: 1, rowSpan: 1 },
    { colStart: 4, colSpan: 2, rowStart: 1, rowSpan: 1 },
  ]),
  buildCustomTemplate("5-left-main-4", "5 Left Main + 4", 3, 2, [
    { colStart: 0, colSpan: 1, rowStart: 0, rowSpan: 2 },
    { colStart: 1, colSpan: 1, rowStart: 0, rowSpan: 1 },
    { colStart: 2, colSpan: 1, rowStart: 0, rowSpan: 1 },
    { colStart: 1, colSpan: 1, rowStart: 1, rowSpan: 1 },
    { colStart: 2, colSpan: 1, rowStart: 1, rowSpan: 1 },
  ]),
  buildUniformGrid("6-grid-3x2", "6 Grid (3x2)", 3, 2),
  buildUniformGrid("6-grid-2x3", "6 Grid (2x3)", 2, 3),
  buildCustomTemplate("7-top3-bot4", "7 Top 3 / Bottom 4", 12, 2, [
    { colStart: 0, colSpan: 4, rowStart: 0, rowSpan: 1 },
    { colStart: 4, colSpan: 4, rowStart: 0, rowSpan: 1 },
    { colStart: 8, colSpan: 4, rowStart: 0, rowSpan: 1 },
    { colStart: 0, colSpan: 3, rowStart: 1, rowSpan: 1 },
    { colStart: 3, colSpan: 3, rowStart: 1, rowSpan: 1 },
    { colStart: 6, colSpan: 3, rowStart: 1, rowSpan: 1 },
    { colStart: 9, colSpan: 3, rowStart: 1, rowSpan: 1 },
  ]),
  buildUniformGrid("8-grid-4x2", "8 Grid (4x2)", 4, 2),
  buildUniformGrid("8-grid-2x4", "8 Grid (2x4)", 2, 4),
];

const TEMPLATE_BY_ID: Readonly<Record<LayoutTemplateId, LayoutTemplateDefinition>> =
  LAYOUT_TEMPLATES.reduce(
    (acc, def) => {
      acc[def.id] = def;
      return acc;
    },
    {} as Record<LayoutTemplateId, LayoutTemplateDefinition>,
  );

export const TEMPLATES_BY_PANE_COUNT: Readonly<Record<number, readonly LayoutTemplateDefinition[]>> =
  (() => {
    const grouped: Record<number, LayoutTemplateDefinition[]> = {};
    for (const template of LAYOUT_TEMPLATES) {
      const bucket = grouped[template.paneCount];
      if (bucket === undefined) {
        grouped[template.paneCount] = [template];
      } else {
        bucket.push(template);
      }
    }
    return grouped;
  })();

export function getTemplateDefinition(id: LayoutTemplateId): LayoutTemplateDefinition {
  return TEMPLATE_BY_ID[id];
}
