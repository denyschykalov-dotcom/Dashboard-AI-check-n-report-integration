-- The language a client's reports are written in. Reports are always generated
-- in English (Claude's commentary, the data labels), then translated as a
-- separate Claude request when this is not 'en'. Per client, because a given
-- client always reads its reports in the same language.
ALTER TABLE "Dashboard_ReportBuilder_clients"
    ADD COLUMN IF NOT EXISTS report_language text NOT NULL DEFAULT 'en';
