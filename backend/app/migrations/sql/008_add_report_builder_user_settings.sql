CREATE TABLE IF NOT EXISTS "Dashboard_ReportBuilder_user_settings" (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    clickup_token_encrypted text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS "idx_reportbuilder_user_settings_user_id"
    ON "Dashboard_ReportBuilder_user_settings" (user_id);
