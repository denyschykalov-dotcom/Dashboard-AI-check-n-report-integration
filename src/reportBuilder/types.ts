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

export type GenerateReportResponse = {
  client_id: string;
  period_label: string;
  blocks: GeneratedBlock[];
};

export type ReportSummary = {
  id: string;
  client_id: string;
  period_label: string;
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
