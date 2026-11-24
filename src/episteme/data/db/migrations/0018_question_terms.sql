-- Migration 0018 -- question to MeSH terms: mesh_term and mesh_term_vec tables, the
-- term_norm helper, and the new result_limit families.
-- Idempotent. Apply after 0017, as episteme_sys_admin (owner). One transaction.
-- Design: docs/superpowers/specs/2026-11-24-question-to-mesh-terms-design.md.
--
-- This file grows task by task. This part adds the families, the two tables and
-- term_norm. Run `python -m episteme.reference.api_settings sync` after it (it applies
-- the EPISTEME_API_LIMIT_<FAMILY> overrides and the owner grants). The `mesh terms` and
-- `mesh embed` steps fill the tables.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION '0018 needs the vector extension (scripts/data/db/install_extensions.sh)';
    END IF;
END
$$;

-- Seed values equal config.API_RESULT_LIMITS (a test compares them).
INSERT INTO episteme_api_internal.result_limit (family, default_limit, max_limit)
VALUES ('match_ngram_max', 8, 8),
       ('suggest', 10, 50),
       ('suggest_ef_search', 200, 200)
ON CONFLICT (family) DO NOTHING;

-- MeSH terms: one row per descriptor name or entry term (and per concept relation).
CREATE TABLE IF NOT EXISTS episteme.mesh_term (
    descriptor_ui    text NOT NULL,
    term             text NOT NULL,
    term_norm        text NOT NULL,
    term_kind        text NOT NULL CHECK (term_kind IN ('name', 'entry')),
    is_preferred     boolean NOT NULL,
    concept_ui       text,
    concept_relation text CHECK (concept_relation IN ('NRW', 'BRD', 'REL')),
    mesh_release     text NOT NULL,
    source_file      text NOT NULL
);
CREATE INDEX IF NOT EXISTS mesh_term_term_norm_idx ON episteme.mesh_term (term_norm);
CREATE INDEX IF NOT EXISTS mesh_term_descriptor_idx ON episteme.mesh_term (descriptor_ui);

-- MeSH term vectors (MedCPT article encoder, 768 dimensions, inner product).
CREATE TABLE IF NOT EXISTS episteme.mesh_term_vec (
    descriptor_ui  text NOT NULL,
    text_kind      text NOT NULL CHECK (text_kind IN ('descriptor', 'entry')),
    text           text NOT NULL,
    embedding      halfvec(768) NOT NULL,
    model          text NOT NULL,
    model_revision text NOT NULL,
    mesh_release   text NOT NULL
);
CREATE INDEX IF NOT EXISTS mesh_term_vec_hnsw_idx
    ON episteme.mesh_term_vec USING hnsw (embedding halfvec_ip_ops);
CREATE INDEX IF NOT EXISTS mesh_term_vec_descriptor_idx ON episteme.mesh_term_vec (descriptor_ui);

-- Term key used to match question words to MeSH terms: lower case, every character that
-- is not a letter or a digit becomes a space, runs of spaces collapse, trimmed. The
-- Python twin is episteme.reference.term_norm.term_norm. NULL in, NULL out.
CREATE OR REPLACE FUNCTION episteme_api_internal.term_norm(p text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$ SELECT btrim(regexp_replace(regexp_replace(lower(p), '[^[:alnum:]]+', ' ', 'g'), ' +', ' ', 'g')) $$;

COMMIT;
