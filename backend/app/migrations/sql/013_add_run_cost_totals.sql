-- Per-run spend, stored on the run instead of recomputed from raw outputs.
--
-- The Overview page used to sum four cost columns by loading every
-- "Dashboard_AI_check_outputs" row for every run — full ORM rows, including the
-- raw LLM response text (gpt_output/gem_output/grok_output). Those columns are
-- TOASTed and average ~5.8 KB per row, so a single page view moved ~13 MB and
-- grew with every run ever recorded.
--
-- Storing the total also makes the figure survive cleanup: cleanup_old_outputs()
-- deletes output rows after the retention window, which silently zeroed the
-- historical spend it was the only record of.
--
-- The backfill below runs as part of this migration, so a redeploy over the old
-- version is self-sufficient: every existing run is populated from whatever data
-- is present at that moment, with no separate command to remember.

ALTER TABLE "Dashboard_AI_check_runs"
    ADD COLUMN IF NOT EXISTS total_cost_usd double precision NOT NULL DEFAULT 0;

-- Backfill. Idempotent: it recomputes rather than accumulates, so re-running is
-- harmless. Runs with no surviving outputs settle at 0, which is the honest
-- answer once their raw rows have been cleaned up.
UPDATE "Dashboard_AI_check_runs" AS r
SET total_cost_usd =
      COALESCE((
          SELECT SUM(
                     COALESCE(o.openai_generation_cost_usd, 0)
                   + COALESCE(o.gemini_generation_cost_usd, 0)
                   + COALESCE(o.grok_generation_cost_usd, 0)
                   + COALESCE(o.gemini_analysis_cost_usd, 0)
                 )
          FROM "Dashboard_AI_check_outputs" o
          WHERE o.run_id = r.id
      ), 0)
    + COALESCE((
          SELECT SUM(COALESCE(rr.gemini_sentiment_cost_usd, 0))
          FROM "Dashboard_AI_check_run_results" rr
          WHERE rr.run_id = r.id
      ), 0);
