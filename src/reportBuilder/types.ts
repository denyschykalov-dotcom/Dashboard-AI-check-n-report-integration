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

/**
 * The language a client's reports are delivered in. Reports are always built in
 * English; anything else adds a Claude translation pass over the commentary and
 * swaps the static labels for their cached translations.
 */
export type ReportLanguage = "en" | "uk";

export const REPORT_LANGUAGES: { value: ReportLanguage; label: string }[] = [
  { value: "en", label: "English" },
  { value: "uk", label: "Ukrainian" },
];

export type Client = {
  id: string;
  name: string;
  domain: string;
  /** Sheet the GA4/GSC blocks read; null means auto-lookup by name in Drive. */
  ga4_sheet_id: string | null;
  /** GA4 property the collector pulls from; null means it skips this client. */
  ga4_property_id: string | null;
  /** Search Console property the collector pulls from; null means it probes. */
  gsc_property: string | null;
  clickup_list_id: string | null;
  se_ranking_target: string | null;
  /** AI-check project the AI-visibility blocks read from; null matches on name. */
  ai_visibility_project: string | null;
  report_language: ReportLanguage;
  created_at: string;
};

/** An AI-check project that has runs, offered when linking a client to one. */
export type AiVisibilityProject = {
  project: string;
  runs: number;
  last_run_at: string | null;
};

export type AiVisibilityProjectsResponse = {
  projects: AiVisibilityProject[];
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
  /**
   * ClickUp task ids struck off in the preview, keyed by block key
   * ("work_completed" / "planned_works"). Held as an exclusion list rather than
   * edited into the block's data so a removal stays undoable.
   */
  excludedTasks: Record<string, string[]>;
  /**
   * The dashboard overview screenshot for the client's AI-check project, as an
   * inline JPEG data URL. Captured at generate time with the same code as the
   * overview's "Screenshot" button and stored with the report, so re-opening or
   * re-exporting it shows the picture the report was built with.
   */
  aiVisibilityShot: string | null;
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
  /** The language the comments came back in. */
  language?: ReportLanguage;
};

/** The month's search-landscape intro, researched with web search (~90s). */
export type AiSearchIndustryResponse = {
  text: string;
  block_type_key: string;
  language?: ReportLanguage;
};

/** Claude's executive summary, written from the submitted report. */
export type AiSummaryResponse = {
  summary: string;
  model: string;
  block_type_key: string;
  language?: ReportLanguage;
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
