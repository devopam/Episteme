-- Migration 0017 -- SQL lookup API: no timeouts (time budget, topic expansion, search work_mem).
-- Idempotent. Apply after 0016, as episteme_sys_admin (owner). One transaction.
-- Design: docs/superpowers/specs/2026-11-24-api-no-timeouts-design.md (follow-up of
-- 2026-11-24-api-layer-perf-design.md).
--
-- This file creates episteme.mesh_qualifier (the MeSH qualifier names, filled by the
-- mesh graph phase) and seeds the new result_limit families. Apply it after 0016 as
-- episteme_sys_admin; run `python -m episteme.reference.api_settings sync` after it (it
-- applies the EPISTEME_API_LIMIT_<FAMILY> overrides and the owner grants); then run
-- `run_pipeline.sh mesh graph` to fill mesh_qualifier.

BEGIN;

-- Seed values equal config.API_RESULT_LIMITS (a test compares them).
INSERT INTO episteme_api_internal.result_limit (family, default_limit, max_limit)
VALUES ('literature_topic_scan', 3000000, 3000000),
       ('literature_topic_chunk', 500, 500),
       ('time_budget_ms', 20000, 20000),
       ('search_work_mem_mb', 256, 256)
ON CONFLICT (family) DO NOTHING;

-- search_rank_chars: 10000 (0016) becomes 2000, only where the row still holds the 0016
-- seed (an operator value stays).
UPDATE episteme_api_internal.result_limit
   SET default_limit = 2000, max_limit = 2000
 WHERE family = 'search_rank_chars' AND default_limit = 10000 AND max_limit = 10000;

-- MeSH qualifier (subheading) names, one row per qualifier and descriptor release file.
CREATE TABLE IF NOT EXISTS episteme.mesh_qualifier (
    qualifier_ui text NOT NULL, qualifier_name text NOT NULL, source_file text
);
CREATE INDEX IF NOT EXISTS mesh_qualifier_lower_name_idx
    ON episteme.mesh_qualifier (lower(qualifier_name));

COMMIT;
