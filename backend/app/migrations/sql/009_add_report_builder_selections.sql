-- Per-user, per-client Report Builder selections: which blocks were checked
-- and the last-used timeframe, so opening a client restores the previous
-- report's starting point.
CREATE TABLE IF NOT EXISTS "Dashboard_ReportBuilder_selections" (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    client_id uuid NOT NULL,
    block_keys text NOT NULL DEFAULT '[]',
    report_type text NOT NULL DEFAULT 'monthly',
    date_from text,
    date_to text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS "idx_reportbuilder_selections_user_client"
    ON "Dashboard_ReportBuilder_selections" (user_id, client_id);
