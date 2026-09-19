-- SP4 migration 0003 -- episteme.mesh_hierarchy (MeSH descriptor parent/child
-- tree-number edges). Idempotent. NOTE (2026-09): on this build
-- migrate_database.sh dies at 0001 (pgvector not installed) and never reaches
-- this file automatically. Apply directly:
--   psql -d <db> -f src/episteme/data/db/migrations/0003_mesh_hierarchy.sql
-- (idempotent -- safe to re-run). Untracked in episteme._migrations when
-- applied this way (mirrors 0002's identical caveat, see that file's header).

CREATE TABLE IF NOT EXISTS episteme.mesh_hierarchy (
    parent_descriptor_ui text NOT NULL,
    child_descriptor_ui  text NOT NULL,
    source_file          text NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS mesh_hierarchy_uq
    ON episteme.mesh_hierarchy (parent_descriptor_ui, child_descriptor_ui);
CREATE INDEX IF NOT EXISTS mesh_hierarchy_src_idx
    ON episteme.mesh_hierarchy (source_file);

GRANT SELECT, INSERT, DELETE ON episteme.mesh_hierarchy TO episteme_app;
