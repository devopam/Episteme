-- Migration 0001 -- add the vector / full-text columns to episteme.chunks.
--
-- Apply once pgvector (`vector`) and `pg_search` are installed on the server
-- (they are absent on the PG19beta3/Windows build schema.sql was authored
-- against). Run as episteme_sys_admin against the target database, after
-- schema.sql.

ALTER TABLE episteme.chunks ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE episteme.chunks ADD COLUMN IF NOT EXISTS chunk_tsv tsvector;

-- HNSW (embedding) and BM25 (chunk_text / chunk_tsv) indexes are created
-- per partition when the Phase 1 RAG retrieval work needs them.
