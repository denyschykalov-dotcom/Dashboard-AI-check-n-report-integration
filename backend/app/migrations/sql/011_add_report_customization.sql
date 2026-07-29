-- Per-report customization for the report builder: a JSON blob holding the
-- accent color, text size/weight, per-block chart variants, and section
-- visibility toggles. NULL means the template defaults apply.
ALTER TABLE "Dashboard_ReportBuilder_reports"
    ADD COLUMN IF NOT EXISTS customization text;
