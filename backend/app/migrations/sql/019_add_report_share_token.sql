-- Public share links for a finished report.
--
-- Exports were an authenticated file download, so handing a client their report
-- meant emailing an .html or .pdf around. This column holds a random token that
-- serves the same rendered report at /r/<token> with no login. NULL means the
-- report is not shared; clearing it revokes the link.
--
-- Unique so a token can be looked up straight to one report, and so a
-- collision (astronomically unlikely, but the index is the guarantee) is an
-- insert error rather than a report served to the wrong client.
ALTER TABLE "Dashboard_ReportBuilder_reports"
    ADD COLUMN IF NOT EXISTS share_token text;

CREATE UNIQUE INDEX IF NOT EXISTS dashboard_reportbuilder_reports_share_token_key
    ON "Dashboard_ReportBuilder_reports" (share_token);
