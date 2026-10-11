-- Migration 0004 -- load-path indexes on episteme.articles. Idempotent. Every
-- non-pmc/bookshelf source lands in the articles_default partition, which had
-- no index on id or source_file, so each per-file load's deletes scanned the
-- whole partition. NOTE (2026-10): on this build migrate_database.sh dies at
-- 0001 (pgvector not installed) and never reaches this file automatically.
-- Apply directly:
--   psql -d <db> -f src/episteme/data/db/migrations/0004_articles_load_indexes.sql
-- (idempotent -- safe to re-run). Untracked in episteme._migrations when
-- applied this way (mirrors 0002/0003's identical caveat, see their headers).
-- Creating the index on the partitioned parent creates one per partition.
-- LOCKING: CREATE INDEX on the partitioned parent blocks writes to every
-- partition until it finishes, and CONCURRENTLY is not possible on a
-- partitioned parent. Apply with no load running.

CREATE INDEX IF NOT EXISTS articles_source_file_idx
    ON episteme.articles (source, source_file);
CREATE INDEX IF NOT EXISTS articles_id_idx
    ON episteme.articles (id);
