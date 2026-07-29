export type ReportBlockType = {
  key: string;
  display_name: string;
  source: string;
  render_style: string;
  ai_visibility_window: string | null;
  ai_visibility_model: string | null;
};

export type BlockCatalogResponse = {
  blocks: ReportBlockType[];
};

export type Client = {
  id: string;
  name: string;
  domain: string;
  ga4_sheet_id: string | null;
  clickup_list_id: string | null;
  se_ranking_target: string | null;
  created_at: string;
};

export type ClientListResponse = {
  clients: Client[];
};

export type GeneratedBlock = {
  block_type_key: string;
  status: string; // "ok" | "unavailable"
  data: Record<string, unknown> | null;
  unavailable_reason: string | null;
  comment?: string;
};

export type ReportPanelConfig = {
  scale: "small" | "normal" | "large";
  headingWeight: "normal" | "bold";
  bodyWeight: "normal" | "bold";
};

export type ReportCustomization = {
  accent: string | null;
  charts: Record<string, string>;
  panels: Record<string, ReportPanelConfig>;
};

export type GenerateReportResponse = {
  client_id: string;
  period_label: string;
  default_comparison: string;
  blocks: GeneratedBlock[];
};

export type ReportSummary = {
  id: string;
  client_id: string;
  period_label: string;
  default_comparison: string;
  customization: ReportCustomization | null;
  generated_by: string;
  generated_at: string;
  created_at: string;
  updated_at: string;
};

export type ReportListResponse = {
  reports: ReportSummary[];
};

export type ReportDetail = ReportSummary & {
  blocks: GeneratedBlock[];
};

/** Claude's draft comment per block key, written before the preview is shown. */
export type AiCommentsResponse = {
  comments: Record<string, string>;
  model: string;
};

/** Claude's executive summary, written from the submitted report. */
export type AiSummaryResponse = {
  summary: string;
  model: string;
  block_type_key: string;
};

export type ReportSettingsStatus = {
  clickup_configured: boolean;
  clickup_token_hint: string | null;
  clickup_username?: string | null;
};

export type ReportType = "monthly" | "yearly";

/** The reporting window a report covers, ending with the last completed month. */
export type PeriodPreset = "last_month" | "last_3_months";

/**
 * A comparison the report offers: the previous window of the same length ("mom")
 * or the same window twelve months back ("yoy"). Several may be chosen — each
 * becomes a toggle in the exported report.
 */
export type ComparisonMode = "mom" | "yoy";

export type ReportSelection = {
  block_keys: string[];
  period_preset: string | null;
  comparisons: string[];
  /** Legacy single-choice preset key, kept for selections saved before the split. */
  comparison: string | null;
  report_type: string;
  date_from: string | null;
  date_to: string | null;
};

export type PlannedWorkMode = "clickup" | "manual";
