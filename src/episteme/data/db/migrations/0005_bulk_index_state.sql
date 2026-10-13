-- Migration 0005 -- bookkeeping for bulk-load mode. Idempotent.
-- scripts/data/db/bulk_load_indexes.sh drop saves here the definition of each
-- secondary index of episteme.articles it drops; rebuild re-creates the index
-- and deletes its row. A row in this table means a rebuild is pending, and
-- `drop` refuses to run until it is done.
-- Apply as episteme_sys_admin (so that role owns the table). Like 0002-0004
-- it is applied by hand and not tracked in episteme._migrations:
--   psql -d <db> -f src/episteme/data/db/migrations/0005_bulk_index_state.sql
-- No access for episteme_app: only the table owner drops and creates indexes,
-- and a deleted row would hide a pending rebuild. schema.sql's ALTER DEFAULT
-- PRIVILEGES grants episteme_app SELECT/INSERT/UPDATE/DELETE on every new table
-- episteme_sys_admin creates, so the REVOKE below is required, not decorative.

CREATE TABLE IF NOT EXISTS episteme._bulk_index_state (
    index_name  text PRIMARY KEY,      -- schema-qualified parent index on episteme.articles
    definition  text NOT NULL,         -- pg_get_indexdef() of the parent index (reads ON ONLY)
    run_id      text,
    dropped_at  timestamptz NOT NULL DEFAULT now()
);

REVOKE ALL ON episteme._bulk_index_state FROM episteme_app;
