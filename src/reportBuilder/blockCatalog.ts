// Display helper for the block list in the selection UI: the source tag on each
// row. The authoritative catalog is always fetched from the API
// (GET /api/report-builder/block-catalog), in the report's default block order.

export const SOURCE_LABELS: Record<string, string> = {
  static: "Report header",
  editorial: "Editorial",
  ahrefs: "Ahrefs",
  ga4_sheet: "Google Analytics 4",
  gsc_sheet: "Google Search Console",
  se_ranking: "SE Ranking",
  clickup: "ClickUp",
  ai_visibility: "AI Visibility (dashboard)",
};

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}
