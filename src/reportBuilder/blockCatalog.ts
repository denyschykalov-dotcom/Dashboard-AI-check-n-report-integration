// Display helpers for grouping the block catalog in the selection UI.
// The authoritative catalog is always fetched from the API
// (GET /api/report-builder/block-catalog); this only provides presentation
// labels/ordering for the source groups.

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

export const SOURCE_ORDER: string[] = [
  "static",
  "editorial",
  "ahrefs",
  "ga4_sheet",
  "gsc_sheet",
  "se_ranking",
  "clickup",
  "ai_visibility",
];

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}
