-- Cache for external-API pulls that cannot change once fetched.
--
-- Ahrefs bills per request: one report's eight Site Explorer calls cost about
-- 2,300 API units, and every regenerate paid it again — for figures that are
-- point-in-time snapshots of a finished month and therefore identical on the
-- second ask. This table holds one pull per (endpoint, target, month) so the
-- month's reporting round costs a single fetch.
--
-- cache_key is the primary key: what was asked for, spelled out
-- ("ahrefs:v1:domain_analysis:<domain>:<month end>:<mode>"), so a different
-- month or a different mode is a different row rather than a stale hit.
CREATE TABLE IF NOT EXISTS "Dashboard_ReportBuilder_api_cache" (
    cache_key text PRIMARY KEY,
    payload_json text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
