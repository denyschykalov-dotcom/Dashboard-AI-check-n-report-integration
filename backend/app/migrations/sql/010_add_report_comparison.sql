-- Comparison-preset support for the report builder.
--   * reports.default_comparison — which comparison ("mom"/"yoy") the exported
--     report opens on, derived from the chosen preset at generate time.
--   * selections.comparison — the last-used comparison preset key, so reopening
--     a client restores the chosen preset (or NULL when the Advanced
--     custom-range / full-year controls were used instead).
ALTER TABLE "Dashboard_ReportBuilder_reports"
    ADD COLUMN IF NOT EXISTS default_comparison text NOT NULL DEFAULT 'mom';

ALTER TABLE "Dashboard_ReportBuilder_selections"
    ADD COLUMN IF NOT EXISTS comparison text;
