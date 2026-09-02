-- Drop the per-client currency column added by 017.
--
-- 017 held the report's currency symbol as a per-client dashboard setting,
-- picked from a dropdown. The client sheets turned out to carry the currency
-- themselves, in a Currency column beside Revenue on the ecommerce tabs — the
-- same place GA4 reports the revenue from — so the report reads it there and
-- falls back to US dollars when a sheet has no such column. Nothing reads this
-- column any more, and keeping it invites the two answers disagreeing.
--
-- IF EXISTS because 017 was removed from the repo: a database created after
-- that never had the column, while one 017 already ran against does.
ALTER TABLE "Dashboard_ReportBuilder_clients"
    DROP COLUMN IF EXISTS report_currency;
