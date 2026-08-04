-- Which AI-check project a report client's AI-visibility blocks read from.
--
-- Those blocks matched "Dashboard_AI_check_runs".project against the *client's
-- name*, case-insensitively. That only works when the two happen to be spelled
-- the same: a client named "onebyone" matches no project at all, so its
-- AI-visibility blocks always resolved "No AI-visibility runs found".
--
-- NULL keeps the old name-matching behaviour, so existing clients that did line
-- up (e.g. "tarsco" -> project "Tarsco") continue to work untouched.
ALTER TABLE "Dashboard_ReportBuilder_clients"
    ADD COLUMN IF NOT EXISTS ai_visibility_project text;
