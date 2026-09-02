-- Per-client currency symbol for the revenue figures in the report.
--
-- GA4 reports purchaseRevenue in the analytics property's own currency and the
-- collector sheet does not carry it, so the report template hardcoded "₴".
-- Default matches that so existing reports render unchanged; US/EU clients get
-- their symbol set from the dashboard.
ALTER TABLE "Dashboard_ReportBuilder_clients"
    ADD COLUMN IF NOT EXISTS report_currency text NOT NULL DEFAULT '₴';
