CREATE TABLE IF NOT EXISTS "Dashboard_ReportBuilder_clients" (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    domain text NOT NULL,
    ga4_sheet_id text,
    clickup_list_id text,
    se_ranking_target text,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "Dashboard_ReportBuilder_reports" (
    id uuid PRIMARY KEY,
    client_id uuid NOT NULL,
    period_label text NOT NULL,
    generated_by uuid NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "Dashboard_ReportBuilder_report_blocks" (
    id uuid PRIMARY KEY,
    report_id uuid NOT NULL,
    block_type_key text NOT NULL,
    position integer NOT NULL DEFAULT 0,
    data_json text,
    comment text,
    status text NOT NULL DEFAULT 'ok',
    unavailable_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS "idx_reportbuilder_reports_client_id"
    ON "Dashboard_ReportBuilder_reports" (client_id);

CREATE INDEX IF NOT EXISTS "idx_reportbuilder_report_blocks_report_id"
    ON "Dashboard_ReportBuilder_report_blocks" (report_id);
