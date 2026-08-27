-- The GA4 and Search Console properties the Apps Script collector pulls from.
--
-- These lived in a hardcoded SITES array inside the collector script, so adding
-- a client or fixing a wrong property meant editing and redeploying the script.
-- Worse, a wrong-but-readable Search Console property string returns HTTP 200
-- with zero rows, which is how yamahaonlineparts.com collected 0 clicks for
-- months while every report block still reported "ok".
--
-- Holding them per client makes the dashboard the single source of truth: the
-- collector reads this list instead of carrying its own copy.
--
-- NULL means "not configured" — the collector skips that site and logs why,
-- rather than collecting a sheet full of zeros. gsc_property may stay NULL on
-- purpose: the collector then probes "sc-domain:<domain>",
-- "https://<domain>/" and "https://www.<domain>/" and uses whichever returns
-- data.
ALTER TABLE "Dashboard_ReportBuilder_clients"
    ADD COLUMN IF NOT EXISTS ga4_property_id text,
    ADD COLUMN IF NOT EXISTS gsc_property text;
