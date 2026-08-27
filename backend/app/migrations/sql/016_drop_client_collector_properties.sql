-- Drop the collector property columns added by 015.
--
-- They were added so the dashboard could hold each client's GA4 and Search
-- Console properties and hand them to the Apps Script collector over an API.
-- That approach was dropped: the collector is a standalone Apps Script with its
-- own SITES list (apps_script/collector.gs), so nothing reads these columns and
-- keeping them invites the two lists drifting apart.
--
-- IF EXISTS on both, because 015 was removed from the repo: a database created
-- after that never had these columns, while the one 015 already ran against
-- does.
ALTER TABLE "Dashboard_ReportBuilder_clients"
    DROP COLUMN IF EXISTS ga4_property_id,
    DROP COLUMN IF EXISTS gsc_property;
