-- =============================================================================
-- episteme storage core -- PostgreSQL DDL (partitioned data model per ADR-0002)
-- =============================================================================
--
-- Apply while connected to the target database (episteme or episteme_test) as
-- role `episteme_sys_admin`. Every object below is therefore owned by that role
-- automatically. This file contains NO `CREATE ROLE` / `CREATE DATABASE`
-- statements -- roles and databases are provisioned out of band.
--
-- Order of application:  extensions.sql  ->  schema.sql  ->  migrations/0001.
-- Consumed by scripts/data/db/init_database.sh (Task 5) and every `pg` test.
--
-- Partitioning summary (ADR-0002):
--   articles          LIST (source) -> RANGE (year)          hot narrow table
--   article_body      LIST (source)                          text, TOAST-compressed
--   article_cites     HASH (src_pmid)          8 buckets     citation graph edges
--   article_mesh      HASH (pmid)              8 buckets      MeSH graph edges
--   chunks            HASH (article_id)        8 buckets      Phase 1 RAG scaffold
--   _audit            RANGE (recorded_at)      monthly        append-only trail
--   id_map, _runs, _lineage                    unpartitioned  small / append-only
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS episteme;


-- -----------------------------------------------------------------------------
-- episteme.articles -- narrow hot table, LIST (source) -> RANGE (year)
--   Columns mirror article_schema.ARTICLE_COLUMNS MINUS the text columns
--   (title, abstract, body_text, text_override, virtual text) which live in episteme.article_body.
--   No PRIMARY KEY: a unique constraint on a partitioned table must include
--   every partition-key column -> (id, source, year); the brief neither asks
--   for that nor for a unique index on id, so it is intentionally omitted.
-- -----------------------------------------------------------------------------
CREATE TABLE episteme.articles (
    id                 text,
    source             text NOT NULL,
    source_file        text,
    source_record_id   text,
    pmid               text,
    pmcid              text,
    doi                text,
    authors            text[],
    journal            text,
    year               int,
    mesh               text[],
    publication_types  text[],
    language           text,
    license            text,
    license_url        text,
    license_raw        text,
    subset             text,
    is_retracted       boolean,
    extract_status     text,
    extract_notes      text,
    retrieved_at       timestamptz,
    content_hash       text,
    pmc_version        text,
    is_manuscript      boolean,
    is_historical_ocr  boolean,
    pdf_url            text,
    container_id       text,   -- SP2: parent container (book) id for bookshelf part rows; also in migration 0002
    book_meta          jsonb   -- SP2: book-level metadata blob (see article_schema.BOOK_META_KEYS)
) PARTITION BY LIST (source);

-- pmc list-partition, sub-partitioned by publication year.
CREATE TABLE episteme.articles_pmc
    PARTITION OF episteme.articles
    FOR VALUES IN ('pmc')
    PARTITION BY RANGE (year);

-- Year ladder for `pmc`. year=0 is the unknown/sentinel bucket: finalize_row
-- leaves year NULL and the loader maps NULL -> 0 before insert.
CREATE TABLE episteme.articles_pmc_y0
    PARTITION OF episteme.articles_pmc FOR VALUES FROM (0) TO (1);
CREATE TABLE episteme.articles_pmc_pre1990
    PARTITION OF episteme.articles_pmc FOR VALUES FROM (1) TO (1990);
CREATE TABLE episteme.articles_pmc_1990_1995
    PARTITION OF episteme.articles_pmc FOR VALUES FROM (1990) TO (1995);
CREATE TABLE episteme.articles_pmc_1995_2000
    PARTITION OF episteme.articles_pmc FOR VALUES FROM (1995) TO (2000);
CREATE TABLE episteme.articles_pmc_2000_2005
    PARTITION OF episteme.articles_pmc FOR VALUES FROM (2000) TO (2005);
CREATE TABLE episteme.articles_pmc_2005_2010
    PARTITION OF episteme.articles_pmc FOR VALUES FROM (2005) TO (2010);
CREATE TABLE episteme.articles_pmc_2010_2015
    PARTITION OF episteme.articles_pmc FOR VALUES FROM (2010) TO (2015);
CREATE TABLE episteme.articles_pmc_2015_2020
    PARTITION OF episteme.articles_pmc FOR VALUES FROM (2015) TO (2020);
CREATE TABLE episteme.articles_pmc_2020_2025
    PARTITION OF episteme.articles_pmc FOR VALUES FROM (2020) TO (2025);
CREATE TABLE episteme.articles_pmc_2025_2030
    PARTITION OF episteme.articles_pmc FOR VALUES FROM (2025) TO (2030);
CREATE TABLE episteme.articles_pmc_future
    PARTITION OF episteme.articles_pmc FOR VALUES FROM (2030) TO (10000);

-- Catch-all for any source whose list-partition has not been created yet.
CREATE TABLE episteme.articles_default
    PARTITION OF episteme.articles DEFAULT;

-- Indexes (ADR-0002). On a partitioned parent these cascade to all partitions.
CREATE INDEX articles_pmid_idx   ON episteme.articles (pmid)  WHERE pmid  IS NOT NULL;
CREATE INDEX articles_pmcid_idx  ON episteme.articles (pmcid) WHERE pmcid IS NOT NULL;
CREATE INDEX articles_doi_idx    ON episteme.articles (doi)   WHERE doi   IS NOT NULL;
CREATE INDEX articles_retrieved_at_brin ON episteme.articles USING brin (retrieved_at);
CREATE INDEX articles_mesh_gin              ON episteme.articles USING gin (mesh);
CREATE INDEX articles_authors_gin           ON episteme.articles USING gin (authors);
CREATE INDEX articles_publication_types_gin ON episteme.articles USING gin (publication_types);


-- -----------------------------------------------------------------------------
-- episteme.article_body -- bulky text columns, LIST (source), TOAST-compressed.
--   Mirrors articles' LIST (source) split for partition-wise joins; no year
--   sub-partitioning (a single `pmc` partition is enough for the text table).
--   PRIMARY KEY is (article_id, source): a partitioned table's PK must contain
--   the partition key, so the brief's bare `article_id PRIMARY KEY` is widened
--   by the partition column.
-- -----------------------------------------------------------------------------
CREATE TABLE episteme.article_body (
    article_id  text NOT NULL,
    source      text NOT NULL,
    year        int,
    title       text,
    abstract    text,
    body_text   text,
    text_override text,
    -- text (migration 0014): text_override where set ('' means NULL), else the
    -- join of the non-blank parts (article_schema.TEXT_JOIN_SQL, verbatim). A
    -- virtual column has no storage: the duplicate of title + abstract + body is
    -- not stored.
    text        text GENERATED ALWAYS AS (CASE WHEN text_override = '' THEN NULL ELSE coalesce(text_override, nullif((CASE WHEN coalesce(btrim(title, E' \t\r\n'), '') = '' THEN '' ELSE title END || CASE WHEN coalesce(btrim(abstract, E' \t\r\n'), '') = '' THEN '' ELSE CASE WHEN coalesce(btrim(title, E' \t\r\n'), '') = '' THEN '' ELSE E'\n\n' END || abstract END || CASE WHEN coalesce(btrim(body_text, E' \t\r\n'), '') = '' THEN '' ELSE CASE WHEN coalesce(btrim(title, E' \t\r\n'), '') = '' AND coalesce(btrim(abstract, E' \t\r\n'), '') = '' THEN '' ELSE E'\n\n' END || body_text END), '')) END) VIRTUAL,
    PRIMARY KEY (article_id, source)
) PARTITION BY LIST (source);

-- ADR-0002 asks for zstd TOAST compression on body_text / text_override (the
-- stored text; the virtual text column has no storage). The PG19beta3
-- build here rejects `zstd` (SQLSTATE 22023, invalid compression method), so
-- fall back to lz4 and record the deviation. Compression set on the parent is
-- inherited by partitions created afterwards.
DO $$ BEGIN
    EXECUTE 'ALTER TABLE episteme.article_body ALTER COLUMN body_text SET COMPRESSION zstd';
    EXECUTE 'ALTER TABLE episteme.article_body ALTER COLUMN text_override SET COMPRESSION zstd';
    RAISE NOTICE 'article_body.body_text/text_override: zstd TOAST compression (ADR-0002)';
EXCEPTION WHEN invalid_parameter_value OR feature_not_supported THEN
    EXECUTE 'ALTER TABLE episteme.article_body ALTER COLUMN body_text SET COMPRESSION lz4';
    EXECUTE 'ALTER TABLE episteme.article_body ALTER COLUMN text_override SET COMPRESSION lz4';
    RAISE NOTICE 'zstd unavailable on this build - article_body.body_text/text_override fell back to lz4 (ADR-0002 specifies zstd)';
END $$;

CREATE TABLE episteme.article_body_pmc
    PARTITION OF episteme.article_body FOR VALUES IN ('pmc');
CREATE TABLE episteme.article_body_default
    PARTITION OF episteme.article_body DEFAULT;


-- -----------------------------------------------------------------------------
-- episteme.id_map -- cross-file identifier enrichment (not a text source).
-- -----------------------------------------------------------------------------
CREATE TABLE episteme.id_map (
    pmid          text,
    pmcid         text,
    doi           text,
    source_file   text,
    retrieved_at  timestamptz
);

CREATE UNIQUE INDEX id_map_ids_uq
    ON episteme.id_map (coalesce(pmid, ''), coalesce(pmcid, ''), coalesce(doi, ''));


-- -----------------------------------------------------------------------------
-- episteme.article_cites -- citation graph edges, HASH (src_pmid), 8 buckets.
-- -----------------------------------------------------------------------------
CREATE TABLE episteme.article_cites (
    src_pmid     text NOT NULL,
    dst_pmid     text NOT NULL,
    source_file  text
) PARTITION BY HASH (src_pmid);

CREATE TABLE episteme.article_cites_h0 PARTITION OF episteme.article_cites FOR VALUES WITH (MODULUS 8, REMAINDER 0);
CREATE TABLE episteme.article_cites_h1 PARTITION OF episteme.article_cites FOR VALUES WITH (MODULUS 8, REMAINDER 1);
CREATE TABLE episteme.article_cites_h2 PARTITION OF episteme.article_cites FOR VALUES WITH (MODULUS 8, REMAINDER 2);
CREATE TABLE episteme.article_cites_h3 PARTITION OF episteme.article_cites FOR VALUES WITH (MODULUS 8, REMAINDER 3);
CREATE TABLE episteme.article_cites_h4 PARTITION OF episteme.article_cites FOR VALUES WITH (MODULUS 8, REMAINDER 4);
CREATE TABLE episteme.article_cites_h5 PARTITION OF episteme.article_cites FOR VALUES WITH (MODULUS 8, REMAINDER 5);
CREATE TABLE episteme.article_cites_h6 PARTITION OF episteme.article_cites FOR VALUES WITH (MODULUS 8, REMAINDER 6);
CREATE TABLE episteme.article_cites_h7 PARTITION OF episteme.article_cites FOR VALUES WITH (MODULUS 8, REMAINDER 7);


-- -----------------------------------------------------------------------------
-- episteme.article_mesh -- MeSH graph edges, HASH (pmid), 8 buckets.
-- -----------------------------------------------------------------------------
CREATE TABLE episteme.article_mesh (
    pmid             text NOT NULL,
    descriptor_ui    text,
    descriptor_name  text,
    major_topic      boolean,
    qualifiers       text[],
    source_file      text
) PARTITION BY HASH (pmid);

CREATE TABLE episteme.article_mesh_h0 PARTITION OF episteme.article_mesh FOR VALUES WITH (MODULUS 8, REMAINDER 0);
CREATE TABLE episteme.article_mesh_h1 PARTITION OF episteme.article_mesh FOR VALUES WITH (MODULUS 8, REMAINDER 1);
CREATE TABLE episteme.article_mesh_h2 PARTITION OF episteme.article_mesh FOR VALUES WITH (MODULUS 8, REMAINDER 2);
CREATE TABLE episteme.article_mesh_h3 PARTITION OF episteme.article_mesh FOR VALUES WITH (MODULUS 8, REMAINDER 3);
CREATE TABLE episteme.article_mesh_h4 PARTITION OF episteme.article_mesh FOR VALUES WITH (MODULUS 8, REMAINDER 4);
CREATE TABLE episteme.article_mesh_h5 PARTITION OF episteme.article_mesh FOR VALUES WITH (MODULUS 8, REMAINDER 5);
CREATE TABLE episteme.article_mesh_h6 PARTITION OF episteme.article_mesh FOR VALUES WITH (MODULUS 8, REMAINDER 6);
CREATE TABLE episteme.article_mesh_h7 PARTITION OF episteme.article_mesh FOR VALUES WITH (MODULUS 8, REMAINDER 7);


-- -----------------------------------------------------------------------------
-- episteme.chunks -- Phase 1 RAG scaffold, HASH (article_id), 8 buckets.
--   embedding vector(768) + chunk_tsv tsvector are added by migrations/0001
--   once pgvector / pg_search are installed on the server.
-- -----------------------------------------------------------------------------
CREATE TABLE episteme.chunks (
    article_id  text NOT NULL,
    chunk_no    int  NOT NULL,
    chunk_text  text
) PARTITION BY HASH (article_id);

CREATE TABLE episteme.chunks_h0 PARTITION OF episteme.chunks FOR VALUES WITH (MODULUS 8, REMAINDER 0);
CREATE TABLE episteme.chunks_h1 PARTITION OF episteme.chunks FOR VALUES WITH (MODULUS 8, REMAINDER 1);
CREATE TABLE episteme.chunks_h2 PARTITION OF episteme.chunks FOR VALUES WITH (MODULUS 8, REMAINDER 2);
CREATE TABLE episteme.chunks_h3 PARTITION OF episteme.chunks FOR VALUES WITH (MODULUS 8, REMAINDER 3);
CREATE TABLE episteme.chunks_h4 PARTITION OF episteme.chunks FOR VALUES WITH (MODULUS 8, REMAINDER 4);
CREATE TABLE episteme.chunks_h5 PARTITION OF episteme.chunks FOR VALUES WITH (MODULUS 8, REMAINDER 5);
CREATE TABLE episteme.chunks_h6 PARTITION OF episteme.chunks FOR VALUES WITH (MODULUS 8, REMAINDER 6);
CREATE TABLE episteme.chunks_h7 PARTITION OF episteme.chunks FOR VALUES WITH (MODULUS 8, REMAINDER 7);


-- -----------------------------------------------------------------------------
-- episteme._runs -- one row per extraction run. Append-only, fillfactor 100.
-- -----------------------------------------------------------------------------
CREATE TABLE episteme._runs (
    run_id      text PRIMARY KEY,
    source      text,
    config      jsonb,
    totals      jsonb,
    started_at  timestamptz DEFAULT now()
) WITH (fillfactor = 100);


-- -----------------------------------------------------------------------------
-- episteme._lineage -- one row per load stage per source. Append-only, ff 100.
-- -----------------------------------------------------------------------------
CREATE TABLE episteme._lineage (
    source              text,
    source_file         text,
    run_id              text,
    input_content_hash  text,
    rows_inserted       int,
    rows_deleted        int,
    loaded_at           timestamptz DEFAULT now()
) WITH (fillfactor = 100);


-- -----------------------------------------------------------------------------
-- episteme._audit -- append-only hash-chained audit trail, RANGE (recorded_at)
--   with monthly partitions. episteme_app gets INSERT + SELECT only (grants
--   below). ADR-0002 names episteme.create_audit_partition() as the
--   partition-creation mechanism; that was never built as a plpgsql
--   function -- scripts/data/db/ensure_audit_partitions.sh is the actual
--   rotation/partition-creation script (run periodically by an operator or
--   external scheduler, see docs/11 section 8) -- the DEFAULT partition
--   below absorbs inserts for any month that hasn't had its partition
--   created yet (script never run, or run outside its rolling window).
--   The two static partition bounds below carry an explicit '+00' (UTC)
--   offset, not a bare date -- a bare date literal is parsed as midnight in
--   whatever TimeZone the session loading this file happens to have (this
--   dev server's own default is Asia/Calcutta, not UTC), which would give
--   these two partitions a different UTC instant than every later partition
--   ensure_audit_partitions.sh creates (that script pins PGTZ=UTC), leaving
--   a gap or an overlap right at the seam between them. Pinning both to an
--   explicit UTC offset here removes the loading session's TimeZone as a
--   variable entirely.
-- -----------------------------------------------------------------------------
CREATE TABLE episteme._audit (
    seq                 bigserial,
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    actor               text NOT NULL,
    host                text,
    pid                 int,
    run_id              text,
    code_version        text,
    event_type          text NOT NULL,
    object              text,
    input_content_hash  text,
    rows_affected       int,
    old_value           jsonb,
    new_value           jsonb,
    reason              text,
    prev_hash           text NOT NULL,
    record_hash         text NOT NULL,
    PRIMARY KEY (seq, recorded_at)
) PARTITION BY RANGE (recorded_at);

CREATE TABLE episteme._audit_202609 PARTITION OF episteme._audit
    FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');
CREATE TABLE episteme._audit_202610 PARTITION OF episteme._audit
    FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00');
CREATE TABLE episteme._audit_default PARTITION OF episteme._audit DEFAULT;


-- -----------------------------------------------------------------------------
-- Property graph (SQL/PGQ). CREATE PROPERTY GRAPH exists on this PG19beta3
-- build, but the brief's statement uses schema-qualified names in the
-- REFERENCES clauses, which the current grammar rejects (syntax error). Per
-- SP1-beta Task 3 resolution 3 the statement is kept verbatim and wrapped in a
-- guard; the NOTICE path is acceptable and Task 8 refines the graph. EXECUTE
-- defers parsing to run time so the syntax error is catchable here rather than
-- aborting the whole file.
-- -----------------------------------------------------------------------------
DO $$ BEGIN
    EXECUTE $graph$
    CREATE PROPERTY GRAPH episteme_graph
      VERTEX TABLES (episteme.articles KEY (id))
      EDGE TABLES (
        episteme.article_cites KEY (src_pmid, dst_pmid)
          SOURCE KEY (src_pmid) REFERENCES episteme.articles (pmid)
          DESTINATION KEY (dst_pmid) REFERENCES episteme.articles (pmid),
        episteme.article_mesh KEY (pmid, descriptor_ui)
          SOURCE KEY (pmid) REFERENCES episteme.articles (pmid)
          DESTINATION KEY (descriptor_ui) REFERENCES episteme.articles (pmid),  -- refine in SP1-beta task 8
        episteme.article_parts KEY (container_id, part_id)  -- SP2: book -> part edges (table in migration 0002)
          SOURCE KEY (container_id) REFERENCES episteme.articles (id)
          DESTINATION KEY (part_id) REFERENCES episteme.articles (id)
      )
    $graph$;
    RAISE NOTICE 'episteme_graph property graph created';
EXCEPTION WHEN syntax_error OR feature_not_supported THEN
    RAISE NOTICE 'SQL/PGQ unavailable - graph_builder uses the CTE path';
END $$;


-- -----------------------------------------------------------------------------
-- SQL lookup API (migration 0015): schemas, row types, settings tables, helpers.
-- Roles and grants for it live in the superuser grants file, not here.
-- -----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS episteme_api;
CREATE SCHEMA IF NOT EXISTS episteme_api_internal;

-- Row types returned by the lookup functions (CREATE TYPE has no IF NOT EXISTS).
DO $$
BEGIN
    IF to_regtype('episteme_api_internal.claim_row') IS NULL THEN
        CREATE TYPE episteme_api_internal.claim_row AS (
            kind              text,
            claim_group       int,
            alt_rank          int,
            text              text,
            value             text,
            method            text,
            confidence        double precision,
            confidence_basis  text,
            source            text,
            release_id        text,
            source_file       text,
            record_key        text,
            jurisdiction      text,
            licence           text
        );
    END IF;
    IF to_regtype('episteme_api_internal.article_row') IS NULL THEN
        CREATE TYPE episteme_api_internal.article_row AS (
            kind        text,
            article_id  text,
            source      text,
            title       text,
            year        int,
            score       real,
            relation    text,
            release_id  text,
            licence     text,
            note        text
        );
    END IF;
END
$$;

-- claim_row.confidence was `real` in the first 0015 draft; a database that has that
-- shape gets double precision (no table column uses this type, so ALTER is safe).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute a
         WHERE a.attrelid = 'episteme_api_internal.claim_row'::regclass
           AND a.attname = 'confidence' AND NOT a.attisdropped
           AND a.atttypid = 'real'::regtype) THEN
        ALTER TYPE episteme_api_internal.claim_row
            ALTER ATTRIBUTE confidence TYPE double precision;
    END IF;
END
$$;

-- Licence class of each lookup source (copy of config.LOOKUP_SOURCE_SUBSETS).
CREATE TABLE IF NOT EXISTS episteme_api_internal.source_subset (
    source  text PRIMARY KEY,
    subset  text NOT NULL CHECK (subset IN ('commercial', 'pending', 'open_metadata'))
);

-- Row caps of the public functions (copy of config.api_result_limits()).
CREATE TABLE IF NOT EXISTS episteme_api_internal.result_limit (
    family         text PRIMARY KEY,
    default_limit  int NOT NULL,
    max_limit      int NOT NULL,
    CHECK (0 < default_limit AND default_limit <= max_limit)
);

-- Name key used by every name match: NFKC, then lower case. NULL in, NULL out.
CREATE OR REPLACE FUNCTION episteme_api_internal.norm_name(p text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog
AS $$ SELECT lower(normalize(p, NFKC)) $$;

-- Licence class of a source, or 'unlisted'.
CREATE OR REPLACE FUNCTION episteme_api_internal.licence_for(p_source text)
RETURNS text
LANGUAGE sql STABLE
SET search_path = pg_catalog
AS $$
    SELECT coalesce(
        (SELECT s.subset FROM episteme_api_internal.source_subset s WHERE s.source = p_source),
        'unlisted')
$$;

-- Row cap of a function family. An unknown family is an error: a missing cap must
-- not turn into "no cap".
CREATE OR REPLACE FUNCTION episteme_api_internal.limit_for(p_family text)
RETURNS int
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog
AS $$
DECLARE
    v int;
BEGIN
    SELECT r.default_limit INTO v
      FROM episteme_api_internal.result_limit r
     WHERE r.family = p_family;
    IF v IS NULL THEN
        RAISE EXCEPTION 'no result limit for family %', p_family;
    END IF;
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- Task 2: the public-wrapper caps, entity resolution and the substance family.
--
-- Row numbering of claim_row (every function): claim_group starts at 1, one group
-- per claim; alt_rank 0 is the claim, alt_rank > 0 its alternatives. note and
-- missing rows have NULL claim_group and alt_rank; a note's text is in `text`,
-- a missing source's name is in `source`.
--
-- Function bodies that read episteme tables are plpgsql: schema.sql runs before
-- migration 0010 creates those tables, and a LANGUAGE sql body is checked at
-- CREATE time.
-- ---------------------------------------------------------------------------

-- Public wrapper rule, claim rows. Keeps the first limit_for(p_family) claim rows
-- in array order and every note/missing row; when claim rows are cut, appends one
-- note row 'truncated: <kept> of <total>'. A public function calls it as
--   SELECT * FROM episteme_api_internal.cap_claims(
--       ARRAY(SELECT r FROM episteme_api_internal.<f>(<args>) AS r), '<family>')
CREATE OR REPLACE FUNCTION episteme_api_internal.cap_claims(
    p_rows episteme_api_internal.claim_row[], p_family text)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog
AS $$
DECLARE
    v_limit int := episteme_api_internal.limit_for(p_family);
    v_total int := 0;
    v_kept  int := 0;
    r       episteme_api_internal.claim_row;
    v_note  episteme_api_internal.claim_row;
BEGIN
    FOR i IN 1 .. coalesce(array_length(p_rows, 1), 0) LOOP
        r := p_rows[i];
        IF r.kind = 'claim' THEN
            v_total := v_total + 1;
            IF v_kept < v_limit THEN
                v_kept := v_kept + 1;
                RETURN NEXT r;
            END IF;
        ELSE
            RETURN NEXT r;
        END IF;
    END LOOP;
    IF v_total > v_kept THEN
        v_note.kind := 'note';
        v_note.text := format('truncated: %s of %s', v_kept, v_total);
        RETURN NEXT v_note;
    END IF;
END
$$;

-- Public wrapper rule, article rows: the same rule for kind 'article'; the
-- truncation note is in the `note` column.
CREATE OR REPLACE FUNCTION episteme_api_internal.cap_articles(
    p_rows episteme_api_internal.article_row[], p_family text)
RETURNS SETOF episteme_api_internal.article_row
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog
AS $$
DECLARE
    v_limit int := episteme_api_internal.limit_for(p_family);
    v_total int := 0;
    v_kept  int := 0;
    r       episteme_api_internal.article_row;
    v_note  episteme_api_internal.article_row;
BEGIN
    FOR i IN 1 .. coalesce(array_length(p_rows, 1), 0) LOOP
        r := p_rows[i];
        IF r.kind = 'article' THEN
            v_total := v_total + 1;
            IF v_kept < v_limit THEN
                v_kept := v_kept + 1;
                RETURN NEXT r;
            END IF;
        ELSE
            RETURN NEXT r;
        END IF;
    END LOOP;
    IF v_total > v_kept THEN
        v_note.kind := 'note';
        v_note.note := format('truncated: %s of %s', v_kept, v_total);
        RETURN NEXT v_note;
    END IF;
END
$$;

-- The is_latest release of the substance dataset 'fda_substance'
-- (load_substance.DATASET), or NULL (also when reg_release does not exist). The
-- release_id of every substance row (resolve_entity, substance_family).
CREATE OR REPLACE FUNCTION episteme_api_internal.substance_release()
RETURNS text
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog
AS $$
DECLARE
    v text;
BEGIN
    IF to_regclass('episteme.reg_release') IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT rr.release_id INTO v
      FROM episteme.reg_release rr
     WHERE rr.dataset = 'fda_substance' AND rr.is_latest;
    RETURN v;
END
$$;

-- Entity resolution: a drug name or a UNII to substances (SQL form of the former
-- resolve.py query). The input is trimmed; the UNII form is its upper case, the name
-- key is norm_name with inner white space collapsed (load_substance.resolve_key).
-- A substance matches by its own UNII or a substance_xref row, as UNION arms:
--   * substance.unii = UNII form          (when p_id_type is NULL or 'unii')
--   * xref 'unii' = UNII form             (when p_id_type is NULL)
--   * xref 'name' = name key              (when p_id_type is NULL)
--   * xref p_id_type = UNII form or key   (when p_id_type is given)
-- Order: moieties (moiety_key NULL or own key) first, then the lowest key. The first
-- row is the claim (alt_rank 0), the others its alternatives. One match: 'fetched',
-- confidence 1. n matches: 'mapped', confidence 1/n. No match, empty input or no
-- substance tables: one 'missing' row for source 'substance', never an error.
-- release_id: the is_latest reg_release row of dataset 'fda_substance'
-- (load_substance.DATASET), NULL when there is none.
CREATE OR REPLACE FUNCTION episteme_api_internal.resolve_entity(
    p_text text, p_id_type text DEFAULT NULL)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_raw     text := regexp_replace(coalesce(p_text, ''), '^\s+|\s+$', '', 'g');
    v_key     text;
    v_unii    text;
    v_release text := episteme_api_internal.substance_release();
    v_licence text := episteme_api_internal.licence_for('substance');
    v_missing episteme_api_internal.claim_row;
BEGIN
    v_key := btrim(regexp_replace(episteme_api_internal.norm_name(v_raw), '\s+', ' ', 'g'));
    v_unii := upper(v_raw);
    v_missing.kind := 'missing';
    v_missing.source := 'substance';
    v_missing.licence := v_licence;
    IF v_raw = '' OR v_key = ''
       OR to_regclass('episteme.substance') IS NULL
       OR to_regclass('episteme.substance_xref') IS NULL THEN
        RETURN NEXT v_missing;
        RETURN;
    END IF;
    v_release := episteme_api_internal.substance_release();
    RETURN QUERY
    WITH matched AS (
        SELECT s.substance_key FROM episteme.substance s
         WHERE s.unii = v_unii AND (p_id_type IS NULL OR p_id_type = 'unii')
        UNION
        SELECT x.substance_key FROM episteme.substance_xref x
         WHERE p_id_type IS NULL AND x.id_type = 'unii' AND x.id_value = v_unii
        UNION
        SELECT x.substance_key FROM episteme.substance_xref x
         WHERE p_id_type IS NULL AND x.id_type = 'name' AND x.id_value = v_key
        UNION
        SELECT x.substance_key FROM episteme.substance_xref x
         WHERE x.id_type = p_id_type AND x.id_value IN (v_unii, v_key)
    ), ranked AS (
        SELECT s.substance_key AS skey,
               nullif(s.unii, '') AS unii,
               nullif(s.preferred_name, '') AS pname,
               (row_number() OVER (
                    ORDER BY (s.moiety_key IS NULL OR s.moiety_key = s.substance_key) DESC,
                             s.substance_key) - 1)::int AS rk,
               (count(*) OVER ())::int AS n
          FROM episteme.substance s
          JOIN matched m ON m.substance_key = s.substance_key
    )
    SELECT 'claim'::text,
           1,
           k.rk,
           coalesce(k.pname, k.unii, k.skey::text),
           k.skey::text,
           CASE WHEN k.n = 1 THEN 'fetched' ELSE 'mapped' END,
           1.0::double precision / k.n,
           CASE WHEN k.n = 1 THEN 'exact UNII or normalised name match'
                ELSE k.n || ' substances share this name' END,
           'substance'::text,
           v_release,
           NULL::text,
           CASE WHEN k.unii IS NOT NULL THEN 'unii:' || k.unii END,
           NULL::text,
           v_licence
      FROM ranked k
     ORDER BY k.rk;
    IF NOT FOUND THEN
        RETURN NEXT v_missing;
    END IF;
END
$$;

-- The moiety family of a substance (SQL form of _scope.substance_scope): the moiety
-- M = COALESCE(moiety_key, substance_key) of the input, and every substance whose own
-- COALESCE(moiety_key, substance_key) is M. moiety_key is M on every row. Provenance
-- per row: source 'substance', the substance release, licence_for('substance'). No
-- rows when the substance or the substance table does not exist.
-- The first 0015 draft returned no provenance columns; a return type cannot change
-- in place, so an old shape is dropped (both functions) before the CREATE.
DO $$
BEGIN
    IF to_regprocedure('episteme_api_internal.substance_family(bigint)') IS NOT NULL
       AND pg_get_function_result(
               'episteme_api_internal.substance_family(bigint)'::regprocedure)
           NOT LIKE '%licence text%' THEN
        DROP FUNCTION IF EXISTS episteme_api.substance_family(bigint);
        DROP FUNCTION episteme_api_internal.substance_family(bigint);
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION episteme_api_internal.substance_family(p_substance_key bigint)
RETURNS TABLE (substance_key bigint, moiety_key bigint, unii text, preferred_name text,
               source text, release_id text, licence text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
#variable_conflict use_column
DECLARE
    v_m       bigint;
    v_release text := episteme_api_internal.substance_release();
    v_licence text := episteme_api_internal.licence_for('substance');
BEGIN
    IF to_regclass('episteme.substance') IS NULL THEN
        RETURN;
    END IF;
    SELECT coalesce(s.moiety_key, s.substance_key) INTO v_m
      FROM episteme.substance s
     WHERE s.substance_key = p_substance_key;
    IF v_m IS NULL THEN
        RETURN;
    END IF;
    -- Written so that substance_moiety_key_idx and the primary key serve it.
    RETURN QUERY
    SELECT s.substance_key, v_m, s.unii, s.preferred_name,
           'substance'::text, v_release, v_licence
      FROM episteme.substance s
     WHERE s.moiety_key = v_m OR (s.moiety_key IS NULL AND s.substance_key = v_m)
     ORDER BY s.substance_key;
END
$$;

-- Public, capped functions (family 'resolve').
CREATE OR REPLACE FUNCTION episteme_api.resolve_entity(p_text text, p_id_type text DEFAULT NULL)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT * FROM episteme_api_internal.cap_claims(
        ARRAY(SELECT r FROM episteme_api_internal.resolve_entity(p_text, p_id_type) AS r),
        'resolve')
$$;

-- A family is one moiety and its salts and esters: small, so no cap.
CREATE OR REPLACE FUNCTION episteme_api.substance_family(p_substance_key bigint)
RETURNS TABLE (substance_key bigint, moiety_key bigint, unii text, preferred_name text,
               source text, release_id text, licence text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT f.substance_key, f.moiety_key, f.unii, f.preferred_name,
           f.source, f.release_id, f.licence
      FROM episteme_api_internal.substance_family(p_substance_key) AS f
$$;

-- ---------------------------------------------------------------------------
-- Task 3: approval_status and safety_actions (SQL form of the former
-- lookups._reg_lookup, _reg_claim and _get). Family 'regulatory'.
-- ---------------------------------------------------------------------------

-- A note row (the note in `text`) and a missing row (the source in `source`, with
-- its licence). claim_group and alt_rank stay NULL.
CREATE OR REPLACE FUNCTION episteme_api_internal.note_row(p_text text)
RETURNS episteme_api_internal.claim_row
LANGUAGE sql STABLE
SET search_path = pg_catalog
AS $$
    SELECT ROW('note', NULL, NULL, p_text, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
               NULL, NULL, NULL)::episteme_api_internal.claim_row
$$;

CREATE OR REPLACE FUNCTION episteme_api_internal.missing_row(p_source text)
RETURNS episteme_api_internal.claim_row
LANGUAGE sql STABLE
SET search_path = pg_catalog
AS $$
    SELECT ROW('missing', NULL, NULL, NULL, NULL, NULL, NULL, NULL, p_source, NULL, NULL,
               NULL, NULL, episteme_api_internal.licence_for(p_source))
           ::episteme_api_internal.claim_row
$$;

-- Scalar values at a dotted path of a jsonb document (the former lookups._get).
-- At each step one level of array is walked and only objects are read; a missing
-- key and a JSON null are skipped. The values found are flattened one level. A
-- string is its text; a JSON null inside a final array is 'None' and a boolean is
-- 'True' or 'False' (as Python str()); any other value is its jsonb text. Each
-- value is stripped of outer white space; empty values and repeats are dropped,
-- first-seen order kept. Never NULL.
CREATE OR REPLACE FUNCTION episteme_api_internal.json_values(p_doc jsonb, p_path text)
RETURNS text[]
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog
AS $$
DECLARE
    v_vals jsonb[] := ARRAY[p_doc];
    v_next jsonb[];
    v_part text;
    v_val  jsonb;
    v_item jsonb;
    v_s    text;
    v_out  text[] := '{}';
BEGIN
    IF p_doc IS NULL OR p_path IS NULL THEN
        RETURN v_out;
    END IF;
    FOREACH v_part IN ARRAY string_to_array(p_path, '.') LOOP
        v_next := '{}';
        FOREACH v_val IN ARRAY v_vals LOOP
            FOR v_item IN
                SELECT e FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(v_val) = 'array' THEN v_val
                         ELSE jsonb_build_array(v_val) END) AS e
            LOOP
                IF jsonb_typeof(v_item) = 'object'
                   AND (v_item -> v_part) IS NOT NULL
                   AND jsonb_typeof(v_item -> v_part) <> 'null' THEN
                    v_next := array_append(v_next, v_item -> v_part);
                END IF;
            END LOOP;
        END LOOP;
        v_vals := v_next;
    END LOOP;
    FOREACH v_val IN ARRAY v_vals LOOP
        FOR v_item IN
            SELECT e FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(v_val) = 'array' THEN v_val
                     ELSE jsonb_build_array(v_val) END) AS e
        LOOP
            v_s := CASE jsonb_typeof(v_item)
                       WHEN 'string' THEN v_item #>> '{}'
                       WHEN 'null' THEN 'None'
                       WHEN 'boolean' THEN CASE WHEN v_item = 'true'::jsonb
                                                THEN 'True' ELSE 'False' END
                       ELSE v_item::text
                   END;
            v_s := regexp_replace(v_s, '^\s+|\s+$', '', 'g');
            IF v_s <> '' AND NOT (v_s = ANY (v_out)) THEN
                v_out := array_append(v_out, v_s);
            END IF;
        END LOOP;
    END LOOP;
    RETURN v_out;
END
$$;

-- Payload fields of a reg_record claim, per dataset (the former lookups
-- _STATUS_FIELDS, _DETAIL_FIELDS and _NO_STATUS_FIELD). The first status field with
-- a value gives the claim value. status_note: whether a claim with no status value
-- adds '<dataset>: status field not found' (an Article 57 entry is itself the fact,
-- so it has no status field). Article 57 names are checked in
-- parsers.ARTICLE57_COLUMNS. The EMA keys were checked against the loaded 2026-11
-- releases (2026-11-10); the FDA keys are not checked against a downloaded file.
-- The arrays are never NULL.
CREATE OR REPLACE FUNCTION episteme_api_internal.reg_fields(
    p_dataset text,
    OUT status_fields text[], OUT detail_fields text[], OUT status_note boolean)
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT coalesce(v.s, '{}'::text[]), coalesce(v.d, '{}'::text[]),
           p_dataset IS DISTINCT FROM 'ema_article57'
      FROM (SELECT 1) AS one
      LEFT JOIN (VALUES
          ('fda_drugsfda', ARRAY['products.marketing_status'],
                           ARRAY['sponsor_name', 'products.brand_name']),
          ('ema_medicines', ARRAY['medicine_status'],
                            ARRAY['name_of_medicine',
                                  'marketing_authorisation_developer_applicant_holder']),
          ('ema_article57', '{}'::text[],
                            ARRAY['product_name', 'product_authorisation_country',
                                  'marketing_authorisation_holder']),
          ('ema_outside_eu', ARRAY['ema_opinion_status'], ARRAY['name_of_medicine']),
          ('fda_enforcement', ARRAY['classification', 'status'],
                              ARRAY['recall_number', 'reason_for_recall']),
          ('ema_referrals', ARRAY['current_status'],
                            ARRAY['referral_name', 'reference_number']),
          ('ema_dhpc', ARRAY['dhpc_type', 'regulatory_outcome'],
                       ARRAY['name_of_medicine', 'dhpc_type']),
          ('ema_psusa', ARRAY['regulatory_outcome'], ARRAY['procedure_number'])
      ) AS v(ds, s, d) ON v.ds = p_dataset
$$;

-- reg_record claims of a substance's moiety family, one dataset after the other in
-- p_datasets order (the former lookups._reg_lookup).
--   * No substance tables, or an unknown substance: the note 'substance <key> not
--     found' and every dataset as a 'missing' row.
--   * A dataset is a 'missing' row when reg_record or reg_release does not exist or
--     the dataset has no is_latest release (no fallback to an older release).
--   * Names: the family's preferred names (norm_name, inner white space collapsed,
--     as reg_record.substance_names) and the family's 'name' xref rows. With no name,
--     no claims and the note 'substance <key> has no name to match' (last row).
--   * Claims: the records of the latest release whose substance_names overlap the
--     names, ordered by event_date DESC NULLS LAST, record_key. With p_interval, only
--     event_date BETWEEN p_from AND p_to, after the note '<dataset>: <n> records with
--     no date not checked' (n > 0).
--   * A dataset with a claim that has no status value adds the note '<dataset>:
--     status field not found' after its claims (see reg_fields).
-- Dataset and release are SQL literals in the reg_record queries, so that the plan
-- prunes to one leaf partition (migration 0010).
CREATE OR REPLACE FUNCTION episteme_api_internal.reg_lookup(
    p_datasets text[], p_substance_key bigint, p_interval boolean,
    p_from date, p_to date)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_ds        text;
    v_release   text;
    v_names     text[];
    v_keys      bigint[];
    v_has_reg   boolean;
    v_undated   bigint;
    v_sfields   text[];
    v_dfields   text[];
    v_snote     boolean;
    v_no_status boolean;
    v_group     int := 0;
    v_field     text;
    v_vals      text[];
    v_status    text[];
    v_parts     text[];
    v_where     text;
    v_rec       record;
    v_row       episteme_api_internal.claim_row;
BEGIN
    IF to_regclass('episteme.substance') IS NOT NULL
       AND to_regclass('episteme.substance_xref') IS NOT NULL THEN
        SELECT array_agg(f.substance_key ORDER BY f.substance_key) INTO v_keys
          FROM episteme_api_internal.substance_family(p_substance_key) AS f;
    END IF;
    IF v_keys IS NULL THEN
        RETURN NEXT episteme_api_internal.note_row(
            format('substance %s not found', p_substance_key));
        FOREACH v_ds IN ARRAY p_datasets LOOP
            RETURN NEXT episteme_api_internal.missing_row(v_ds);
        END LOOP;
        RETURN;
    END IF;
    SELECT coalesce(array_agg(DISTINCT n.nm), '{}'::text[]) INTO v_names
      FROM (SELECT btrim(regexp_replace(episteme_api_internal.norm_name(f.preferred_name),
                                        '\s+', ' ', 'g')) AS nm
              FROM episteme_api_internal.substance_family(p_substance_key) AS f
            UNION ALL
            SELECT x.id_value
              FROM episteme.substance_xref x
             WHERE x.substance_key = ANY (v_keys) AND x.id_type = 'name') AS n
     WHERE n.nm IS NOT NULL AND n.nm <> '';
    v_has_reg := to_regclass('episteme.reg_record') IS NOT NULL
                 AND to_regclass('episteme.reg_release') IS NOT NULL;
    FOREACH v_ds IN ARRAY p_datasets LOOP
        v_release := NULL;
        IF v_has_reg THEN
            SELECT rr.release_id INTO v_release
              FROM episteme.reg_release rr
             WHERE rr.dataset = v_ds AND rr.is_latest;
        END IF;
        IF v_release IS NULL THEN
            RETURN NEXT episteme_api_internal.missing_row(v_ds);
            CONTINUE;
        END IF;
        IF cardinality(v_names) = 0 THEN
            CONTINUE;
        END IF;
        v_where := format(
            'rr.dataset = %L AND rr.release_id = %L AND rr.substance_names && $1',
            v_ds, v_release);
        IF p_interval THEN
            EXECUTE 'SELECT count(*) FROM episteme.reg_record rr WHERE ' || v_where
                    || ' AND rr.event_date IS NULL'
               INTO v_undated USING v_names;
            IF v_undated > 0 THEN
                RETURN NEXT episteme_api_internal.note_row(
                    format('%s: %s records with no date not checked', v_ds, v_undated));
            END IF;
            v_where := v_where || ' AND rr.event_date BETWEEN $2 AND $3';
        END IF;
        SELECT f.status_fields, f.detail_fields, f.status_note
          INTO v_sfields, v_dfields, v_snote
          FROM episteme_api_internal.reg_fields(v_ds) AS f;
        v_no_status := false;
        FOR v_rec IN EXECUTE
            'SELECT rr.record_key, rr.jurisdiction, rr.event_date, rr.payload, rr.source_file '
            || 'FROM episteme.reg_record rr WHERE ' || v_where
            || ' ORDER BY rr.event_date DESC NULLS LAST, rr.record_key'
            USING v_names, p_from, p_to
        LOOP
            v_status := '{}';
            FOREACH v_field IN ARRAY v_sfields LOOP
                v_vals := episteme_api_internal.json_values(v_rec.payload, v_field);
                IF cardinality(v_vals) > 0 THEN
                    v_status := v_vals;
                    EXIT;
                END IF;
            END LOOP;
            v_parts := ARRAY[v_ds || ' ' || v_rec.record_key];
            FOREACH v_field IN ARRAY v_dfields LOOP
                v_vals := episteme_api_internal.json_values(v_rec.payload, v_field);
                IF cardinality(v_vals) > 0 THEN
                    v_parts := array_append(v_parts, split_part(v_field, '.', -1) || ': '
                                                     || array_to_string(v_vals, '; '));
                END IF;
            END LOOP;
            IF cardinality(v_status) > 0 THEN
                v_parts := array_append(v_parts, 'status: ' || array_to_string(v_status, '; '));
            ELSE
                v_no_status := true;
            END IF;
            IF v_rec.event_date IS NOT NULL THEN
                v_parts := array_append(v_parts,
                                        'date: ' || to_char(v_rec.event_date, 'YYYY-MM-DD'));
            END IF;
            IF coalesce(v_rec.jurisdiction, '') <> '' THEN
                v_parts := array_append(v_parts, 'jurisdiction: ' || v_rec.jurisdiction);
            END IF;
            v_group := v_group + 1;
            v_row := ROW('claim', v_group, 0,
                         array_to_string(v_parts, ', '),
                         CASE WHEN cardinality(v_status) > 0
                              THEN array_to_string(v_status, '; ') END,
                         'fetched', 1.0::double precision,
                         'exact normalised substance name in the latest release',
                         v_ds, v_release, v_rec.source_file, v_rec.record_key,
                         v_rec.jurisdiction, episteme_api_internal.licence_for(v_ds));
            RETURN NEXT v_row;
        END LOOP;
        IF v_snote AND v_no_status THEN
            RETURN NEXT episteme_api_internal.note_row(
                format('%s: status field not found', v_ds));
        END IF;
    END LOOP;
    IF cardinality(v_names) = 0 THEN
        RETURN NEXT episteme_api_internal.note_row(
            format('substance %s has no name to match', p_substance_key));
    END IF;
END
$$;

-- Marketing records per jurisdiction: Drugs@FDA, EMA medicines, Article 57 and EMA
-- opinions outside the EU (lookups.APPROVAL_DATASETS), latest release of each.
CREATE OR REPLACE FUNCTION episteme_api_internal.approval_status(p_substance_key bigint)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT * FROM episteme_api_internal.reg_lookup(
        ARRAY['fda_drugsfda', 'ema_medicines', 'ema_article57', 'ema_outside_eu'],
        p_substance_key, false, NULL::date, NULL::date)
$$;

-- FDA recalls, EMA referrals, DHPCs and PSUSA outcomes (lookups.SAFETY_DATASETS)
-- whose event_date is in [p_from, p_to]; records with no date are counted in a note.
CREATE OR REPLACE FUNCTION episteme_api_internal.safety_actions(
    p_substance_key bigint, p_from date, p_to date)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT * FROM episteme_api_internal.reg_lookup(
        ARRAY['fda_enforcement', 'ema_referrals', 'ema_dhpc', 'ema_psusa'],
        p_substance_key, true, p_from, p_to)
$$;

-- Public, capped functions (family 'regulatory').
CREATE OR REPLACE FUNCTION episteme_api.approval_status(p_substance_key bigint)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT * FROM episteme_api_internal.cap_claims(
        ARRAY(SELECT r FROM episteme_api_internal.approval_status(p_substance_key) AS r),
        'regulatory')
$$;

CREATE OR REPLACE FUNCTION episteme_api.safety_actions(
    p_substance_key bigint, p_from date, p_to date)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT * FROM episteme_api_internal.cap_claims(
        ARRAY(SELECT r FROM episteme_api_internal.safety_actions(p_substance_key, p_from, p_to)
              AS r),
        'regulatory')
$$;

-- ---------------------------------------------------------------------------
-- Task 4: current_label, label_section_text (family 'label') and trials (family
-- 'trials'). SQL form of the former lookups.current_label and lookups.trials.
-- ---------------------------------------------------------------------------

-- The label source of an article id: 'dailymed' or 'ema_epar' by the id prefix
-- (extract_dailymed and extract_epar write '<source>:<id>'), NULL for another id.
CREATE OR REPLACE FUNCTION episteme_api_internal.label_source(p_article_id text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT CASE WHEN starts_with(p_article_id, 'dailymed:') THEN 'dailymed'
                WHEN starts_with(p_article_id, 'ema_epar:') THEN 'ema_epar' END
$$;

-- The sections of the current labels (DailyMed and the EU labels of ema_epar) whose
-- label_substance.unii is the UNII of a member of the substance's moiety family.
--   * No substance tables, or an unknown substance: the note 'substance <key> not
--     found' and a 'missing' row for 'dailymed'.
--   * label_section, label_substance or articles does not exist, or label_section
--     holds no row: a 'missing' row for 'dailymed'.
--   * Claims: one per section, ordered by article_id, start_offset, loinc_code. The
--     value is the section title. The source is the article-id prefix (label_source;
--     'dailymed' for another id). release_id: the SPL version (book_meta 'version')
--     for DailyMed, the document's last update date (book_meta 'last_updated_date')
--     for ema_epar. record_key '<article_id>#<loinc_code>', plus '#<ordinal>' when the
--     section has an ordinal (ema_epar: one row per section per product).
--   * Every answer except the unknown substance ends with the note 'change history
--     not determinable': the extract keeps only the newest version of each label.
CREATE OR REPLACE FUNCTION episteme_api_internal.current_label(p_substance_key bigint)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_members bigint := 0;
    v_uniis   text[];
    v_ready   boolean;
BEGIN
    IF to_regclass('episteme.substance') IS NOT NULL
       AND to_regclass('episteme.substance_xref') IS NOT NULL THEN
        SELECT count(*), array_agg(f.unii ORDER BY f.substance_key)
                         FILTER (WHERE coalesce(f.unii, '') <> '')
          INTO v_members, v_uniis
          FROM episteme_api_internal.substance_family(p_substance_key) AS f;
    END IF;
    IF v_members = 0 THEN
        RETURN NEXT episteme_api_internal.note_row(
            format('substance %s not found', p_substance_key));
        RETURN NEXT episteme_api_internal.missing_row('dailymed');
        RETURN;
    END IF;
    v_ready := to_regclass('episteme.label_section') IS NOT NULL
               AND to_regclass('episteme.label_substance') IS NOT NULL
               AND to_regclass('episteme.articles') IS NOT NULL;
    IF v_ready THEN
        v_ready := EXISTS (SELECT 1 FROM episteme.label_section);
    END IF;
    IF NOT v_ready THEN
        RETURN NEXT episteme_api_internal.missing_row('dailymed');
    ELSIF coalesce(cardinality(v_uniis), 0) > 0 THEN
        RETURN QUERY
        WITH l AS (
            SELECT DISTINCT ls.article_id
              FROM episteme.label_substance ls
             WHERE ls.unii = ANY (v_uniis)
        ), s AS (
            SELECT sec.article_id, sec.loinc_code, sec.title, sec.start_offset,
                   sec.ordinal,
                   coalesce(episteme_api_internal.label_source(sec.article_id),
                            'dailymed') AS src,
                   coalesce(ad.book_meta, ae.book_meta) AS meta,
                   nullif(coalesce(ad.year, ae.year), 0) AS yr,
                   coalesce(ad.source_file, ae.source_file) AS sf
              FROM l
              JOIN episteme.label_section sec ON sec.article_id = l.article_id
              LEFT JOIN episteme.articles ad
                     ON ad.source = 'dailymed' AND ad.id = l.article_id
              LEFT JOIN episteme.articles ae
                     ON ae.source = 'ema_epar' AND ae.id = l.article_id
        ), k AS (
            SELECT s.*,
                   CASE WHEN s.src = 'ema_epar' THEN s.meta ->> 'last_updated_date'
                        ELSE s.meta ->> 'version' END AS rel,
                   (row_number() OVER (
                        ORDER BY s.article_id, s.start_offset, s.loinc_code,
                                 s.ordinal, s.title))::int AS grp
              FROM s
        )
        SELECT 'claim'::text,
               k.grp,
               0,
               k.article_id || ' section ' || k.loinc_code || ' '
                   || coalesce(nullif(k.title, ''), '(no title)')
                   || CASE WHEN k.src = 'ema_epar' THEN ', EPAR document updated '
                           ELSE ', SPL version ' END
                   || coalesce(nullif(k.rel, ''), 'unknown')
                   || ', effective year ' || coalesce(k.yr::text, 'unknown'),
               k.title,
               'fetched'::text,
               1.0::double precision,
               'exact UNII match in the label''s active ingredients'::text,
               k.src,
               k.rel,
               k.sf,
               k.article_id || '#' || k.loinc_code || coalesce('#' || k.ordinal::text, ''),
               CASE WHEN k.src = 'ema_epar' THEN 'EU' ELSE 'US' END,
               episteme_api_internal.licence_for(k.src)
          FROM k
         ORDER BY k.grp;
    END IF;
    RETURN NEXT episteme_api_internal.note_row('change history not determinable');
END
$$;

-- The text of one section of one label: p_section is the LOINC code (DailyMed, or
-- 'smpc:<n>' for an EU section) or the section title, case-insensitive. One claim
-- per matching section, ordered by start_offset, loinc_code, ordinal. The value is
-- substr(body_text, start_offset + 1, end_offset - start_offset): the producers
-- (spl.py, smpc.py) store the offsets into the stored article_body.body_text, not
-- into the virtual article_body.text (that one starts with the title).
--   * label_section, articles or article_body does not exist: a 'missing' row for
--     the label source.
--   * An id that is not a label id, or a label with no section row: the note
--     'label <id> not found'.
--   * No section matches: the note 'label <id> has no section <p_section>'.
--   * No article_body row: the note 'label <id> has no stored text'.
-- Provenance as in current_label. The source is a SQL literal in the article and
-- article_body queries, so that the plan prunes to one partition.
CREATE OR REPLACE FUNCTION episteme_api_internal.label_section_text(
    p_article_id text, p_section text)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_src      text := episteme_api_internal.label_source(p_article_id);
    v_sec      text := regexp_replace(coalesce(p_section, ''), '^\s+|\s+$', '', 'g');
    v_body     text;
    v_has_body boolean;
    v_meta     jsonb;
    v_year     int;
    v_sf       text;
    v_rel      text;
    v_group    int := 0;
    r          record;
    v_row      episteme_api_internal.claim_row;
BEGIN
    IF to_regclass('episteme.label_section') IS NULL
       OR to_regclass('episteme.articles') IS NULL
       OR to_regclass('episteme.article_body') IS NULL THEN
        RETURN NEXT episteme_api_internal.missing_row(coalesce(v_src, 'dailymed'));
        RETURN;
    END IF;
    IF v_src IS NULL OR NOT EXISTS (
            SELECT 1 FROM episteme.label_section s WHERE s.article_id = p_article_id) THEN
        RETURN NEXT episteme_api_internal.note_row(format('label %s not found', p_article_id));
        RETURN;
    END IF;
    IF NOT EXISTS (
            SELECT 1 FROM episteme.label_section s
             WHERE s.article_id = p_article_id
               AND (s.loinc_code = v_sec OR lower(s.title) = lower(v_sec))) THEN
        RETURN NEXT episteme_api_internal.note_row(
            format('label %s has no section %s', p_article_id, v_sec));
        RETURN;
    END IF;
    EXECUTE format(
        'SELECT b.body_text, true FROM episteme.article_body b '
        'WHERE b.source = %L AND b.article_id = $1', v_src)
       INTO v_body, v_has_body USING p_article_id;
    IF v_has_body IS NULL THEN
        RETURN NEXT episteme_api_internal.note_row(
            format('label %s has no stored text', p_article_id));
        RETURN;
    END IF;
    EXECUTE format(
        'SELECT a.book_meta, nullif(a.year, 0), a.source_file FROM episteme.articles a '
        'WHERE a.source = %L AND a.id = $1 LIMIT 1', v_src)
       INTO v_meta, v_year, v_sf USING p_article_id;
    v_rel := CASE WHEN v_src = 'ema_epar' THEN v_meta ->> 'last_updated_date'
                  ELSE v_meta ->> 'version' END;
    FOR r IN
        SELECT s.loinc_code, s.title, s.start_offset, s.end_offset, s.ordinal
          FROM episteme.label_section s
         WHERE s.article_id = p_article_id
           AND (s.loinc_code = v_sec OR lower(s.title) = lower(v_sec))
         ORDER BY s.start_offset, s.loinc_code, s.ordinal, s.title
    LOOP
        v_group := v_group + 1;
        v_row := ROW(
            'claim', v_group, 0,
            format('%s section %s %s, characters %s to %s', p_article_id, r.loinc_code,
                   coalesce(nullif(r.title, ''), '(no title)'),
                   r.start_offset, r.end_offset),
            substr(v_body, r.start_offset + 1, r.end_offset - r.start_offset),
            'fetched', 1.0::double precision,
            'section offsets stored with the label text',
            v_src, v_rel, v_sf,
            p_article_id || '#' || r.loinc_code || coalesce('#' || r.ordinal::text, ''),
            CASE WHEN v_src = 'ema_epar' THEN 'EU' ELSE 'US' END,
            episteme_api_internal.licence_for(v_src));
        RETURN NEXT v_row;
    END LOOP;
END
$$;

-- AACT studies with an intervention matched to a member of the substance's moiety
-- family (intervention_substance.substance_key), with status, phase and whether
-- results are posted (studies.results_first_posted_date).
--   * No substance tables, or an unknown substance: the note 'substance <key> not
--     found' and a 'missing' row for 'aact'.
--   * The tables are read from the release schema 'aact_r<release_id>'
--     (load_aact.schema_name) of the is_latest 'aact' release in reg_release; the
--     'aact' view schema points at the same tables. No is_latest release, a release
--     id that is not YYYYMMDD, or a schema without studies or intervention_substance:
--     a 'missing' row for 'aact' (no fallback to an older release).
--   * source_file: the zip's input_key from <schema>._load_state, row '_release'
--     (load_aact._switch); NULL when that table or row does not exist.
--   * Claims ordered by nct_id; record_key is the nct_id.
CREATE OR REPLACE FUNCTION episteme_api_internal.trials(p_substance_key bigint)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_keys    bigint[];
    v_release text;
    v_schema  text;
    v_sf      text;
BEGIN
    IF to_regclass('episteme.substance') IS NOT NULL
       AND to_regclass('episteme.substance_xref') IS NOT NULL THEN
        SELECT array_agg(f.substance_key ORDER BY f.substance_key) INTO v_keys
          FROM episteme_api_internal.substance_family(p_substance_key) AS f;
    END IF;
    IF v_keys IS NULL THEN
        RETURN NEXT episteme_api_internal.note_row(
            format('substance %s not found', p_substance_key));
        RETURN NEXT episteme_api_internal.missing_row('aact');
        RETURN;
    END IF;
    IF to_regclass('episteme.reg_release') IS NOT NULL THEN
        SELECT rr.release_id INTO v_release
          FROM episteme.reg_release rr
         WHERE rr.dataset = 'aact' AND rr.is_latest;
    END IF;
    IF v_release IS NULL OR v_release !~ '^[0-9]{8}$' THEN
        RETURN NEXT episteme_api_internal.missing_row('aact');
        RETURN;
    END IF;
    v_schema := 'aact_r' || v_release;
    IF to_regclass(format('%I.%I', v_schema, 'studies')) IS NULL
       OR to_regclass(format('%I.%I', v_schema, 'intervention_substance')) IS NULL THEN
        RETURN NEXT episteme_api_internal.missing_row('aact');
        RETURN;
    END IF;
    IF to_regclass(format('%I.%I', v_schema, '_load_state')) IS NOT NULL THEN
        EXECUTE format('SELECT st.source_file FROM %I._load_state st '
                       'WHERE st.table_name = %L', v_schema, '_release')
           INTO v_sf;
    END IF;
    RETURN QUERY EXECUTE format(
        'SELECT ''claim''::text, '
        '       (row_number() OVER (ORDER BY st.nct_id))::int, 0, '
        '       st.nct_id || '' '' || coalesce(nullif(st.brief_title, ''''), ''(no title)'') '
        '         || '': status '' || coalesce(nullif(st.overall_status, ''''), ''unknown'') '
        '         || '', phase '' || coalesce(nullif(st.phase, ''''), ''unknown'') '
        '         || '', results posted: '' '
        '         || CASE WHEN st.results_first_posted_date IS NOT NULL '
        '                 THEN ''yes'' ELSE ''no'' END, '
        '       st.overall_status::text, ''fetched''::text, 1.0::double precision, '
        '       ''intervention name matched to the substance by exact normalised name''::text, '
        '       ''aact''::text, $2, $3, st.nct_id::text, NULL::text, $4 '
        '  FROM (SELECT DISTINCT i.nct_id FROM %I.intervention_substance i '
        '         WHERE i.substance_key = ANY ($1)) i '
        '  JOIN %I.studies st ON st.nct_id = i.nct_id '
        ' ORDER BY st.nct_id',
        v_schema, v_schema)
        USING v_keys, v_release, v_sf, episteme_api_internal.licence_for('aact');
END
$$;

-- Public, capped functions (families 'label' and 'trials').
CREATE OR REPLACE FUNCTION episteme_api.current_label(p_substance_key bigint)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT * FROM episteme_api_internal.cap_claims(
        ARRAY(SELECT r FROM episteme_api_internal.current_label(p_substance_key) AS r),
        'label')
$$;

CREATE OR REPLACE FUNCTION episteme_api.label_section_text(p_article_id text, p_section text)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT * FROM episteme_api_internal.cap_claims(
        ARRAY(SELECT r FROM episteme_api_internal.label_section_text(p_article_id, p_section)
              AS r),
        'label')
$$;

CREATE OR REPLACE FUNCTION episteme_api.trials(p_substance_key bigint)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT * FROM episteme_api_internal.cap_claims(
        ARRAY(SELECT r FROM episteme_api_internal.trials(p_substance_key) AS r),
        'trials')
$$;

-- ---------------------------------------------------------------------------
-- Task 5: the FAERS seriousness/outcome table and faers_counts (family 'faers').
-- SQL form of the former lookups.faers_counts, with the outcome split and the
-- counts per reaction term. Reaction terms are quoted as faers_term stores them
-- (the preferred term as FDA publishes it); no MedDRA coding or hierarchy.
-- ---------------------------------------------------------------------------

-- The FAERS outcome table (faers_moiety_quarter_outcome) is in migration 0015
-- only: it belongs with the FAERS tables of migration 0010, which this file does
-- not hold.

-- FAERS reports of a substance's moiety, for the quarters that overlap
-- [p_from, p_to] (counted whole). Three groups of claims, in this order:
--   * per quarter (faers_moiety_quarter): reports with the moiety as suspect (the
--     value), in any role, and all reports received in the quarter;
--   * the outcome split per quarter (faers_moiety_quarter_outcome), in code order
--     DE, LT, HO, DS, CA, RI, OT, none, then seriousness serious, non_serious,
--     unknown. The text states FDA's seriousness and the outcome apart: a report
--     classed serious can have no outcome flag;
--   * per reaction term (faers_event_quarter, faers_term): reports with the moiety
--     as suspect (the value) and in any role, summed over the quarters (a report
--     has one quarter, so the sum counts each report once); ordered by the suspect
--     count descending, then the any-role count descending, then the term.
-- Notes: the quarter rule, then 'no FAERS data for quarters: ...' (quarters with no
-- faers_quarter_totals row), then the outcome note and the term note, then a note
-- when the outcome table holds no row (not built yet).
--   * No substance tables, or an unknown substance: the note 'substance <key> not
--     found' and a 'missing' row for 'faers'.
--   * faers_moiety_quarter or faers_quarter_totals does not exist, or the totals
--     hold no row: the quarter rule and a 'missing' row for 'faers'.
-- Provenance: source 'openfda_faers'; release_id the is_latest reg_release row of
-- dataset 'openfda_faers' (NULL when there is none); jurisdiction 'US'; licence
-- licence_for('openfda_faers'), on the 'missing' row too.
CREATE OR REPLACE FUNCTION episteme_api_internal.faers_counts(
    p_substance_key bigint, p_from date, p_to date)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_label   constant text := 'US public spontaneous reports (FAERS)';
    v_rule    constant text := 'quarters that overlap the interval are counted whole';
    v_moiety  bigint;
    v_release text;
    v_licence text := episteme_api_internal.licence_for('openfda_faers');
    v_missing episteme_api_internal.claim_row;
    v_q_from  date := date_trunc('quarter', p_from::timestamp)::date;
    v_q_to    date := date_trunc('quarter', p_to::timestamp)::date;
    v_gaps    text;
    v_groups  int := 0;
    v_n       int;
    v_split   boolean := false;
BEGIN
    v_missing := episteme_api_internal.missing_row('faers');
    v_missing.licence := v_licence;
    IF to_regclass('episteme.substance') IS NOT NULL
       AND to_regclass('episteme.substance_xref') IS NOT NULL THEN
        SELECT f.moiety_key INTO v_moiety
          FROM episteme_api_internal.substance_family(p_substance_key) AS f
         LIMIT 1;
    END IF;
    IF v_moiety IS NULL THEN
        RETURN NEXT episteme_api_internal.note_row(
            format('substance %s not found', p_substance_key));
        RETURN NEXT v_missing;
        RETURN;
    END IF;
    -- Nested IFs: a relation is read only after its to_regclass check.
    IF to_regclass('episteme.faers_moiety_quarter') IS NULL
       OR to_regclass('episteme.faers_quarter_totals') IS NULL THEN
        RETURN NEXT episteme_api_internal.note_row(v_rule);
        RETURN NEXT v_missing;
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM episteme.faers_quarter_totals) THEN
        RETURN NEXT episteme_api_internal.note_row(v_rule);
        RETURN NEXT v_missing;
        RETURN;
    END IF;
    IF to_regclass('episteme.reg_release') IS NOT NULL THEN
        SELECT rr.release_id INTO v_release
          FROM episteme.reg_release rr
         WHERE rr.dataset = 'openfda_faers' AND rr.is_latest;
    END IF;

    -- Per quarter: the former claims, with the same text.
    RETURN QUERY
    SELECT 'claim'::text,
           (row_number() OVER (ORDER BY m.quarter))::int,
           0,
           format('%s: %s reports name the substance as suspect, %s in any role, %s (%s)',
                  to_char(m.quarter::timestamp, 'YYYY"Q"Q'), m.reports_suspect, m.reports_any,
                  CASE WHEN t.reports IS NOT NULL
                       THEN format('of %s reports received', t.reports)
                       ELSE 'total not available' END,
                  v_label),
           m.reports_suspect::text,
           'computed'::text,
           1.0::double precision,
           'exact count of stored FAERS reports per moiety and quarter'::text,
           'openfda_faers'::text,
           v_release,
           NULL::text,
           format('faers_moiety_quarter:%s:%s', v_moiety,
                  to_char(m.quarter::timestamp, 'YYYY-MM-DD')),
           'US'::text,
           v_licence
      FROM episteme.faers_moiety_quarter m
      LEFT JOIN episteme.faers_quarter_totals t ON t.quarter = m.quarter
     WHERE m.substance_key = v_moiety
       AND m.quarter >= v_q_from AND m.quarter <= p_to
     ORDER BY m.quarter;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_groups := v_groups + v_n;

    -- The outcome split per quarter.
    IF to_regclass('episteme.faers_moiety_quarter_outcome') IS NOT NULL THEN
        v_split := EXISTS (SELECT 1 FROM episteme.faers_moiety_quarter_outcome);
    END IF;
    IF v_split THEN
        RETURN QUERY
        SELECT 'claim'::text,
               v_groups + (row_number() OVER (
                   ORDER BY o.quarter,
                            array_position(
                                ARRAY['DE', 'LT', 'HO', 'DS', 'CA', 'RI', 'OT', 'none'],
                                o.outcome),
                            array_position(
                                ARRAY['serious', 'non_serious', 'unknown'],
                                o.seriousness)))::int,
               0,
               format('%s: %s reports name the substance as suspect, %s, %s (%s)',
                      to_char(o.quarter::timestamp, 'YYYY"Q"Q'), o.reports,
                      CASE o.seriousness
                           WHEN 'serious' THEN 'classed serious by the reporter'
                           WHEN 'non_serious' THEN 'classed non-serious by the reporter'
                           ELSE 'seriousness not stated' END,
                      CASE o.outcome
                           WHEN 'DE' THEN 'outcome DE (death)'
                           WHEN 'LT' THEN 'outcome LT (life-threatening)'
                           WHEN 'HO' THEN 'outcome HO (hospitalisation)'
                           WHEN 'DS' THEN 'outcome DS (disability)'
                           WHEN 'CA' THEN 'outcome CA (congenital anomaly)'
                           WHEN 'RI' THEN 'outcome RI (required intervention)'
                           WHEN 'OT' THEN 'outcome OT (other serious outcome)'
                           ELSE 'outcome none stated' END,
                      v_label),
               o.reports::text,
               'computed'::text,
               1.0::double precision,
               ('exact count of stored FAERS reports per moiety, quarter, seriousness '
                'and outcome')::text,
               'openfda_faers'::text,
               v_release,
               NULL::text,
               format('faers_moiety_quarter_outcome:%s:%s:%s:%s', v_moiety,
                      to_char(o.quarter::timestamp, 'YYYY-MM-DD'), o.seriousness,
                      o.outcome),
               'US'::text,
               v_licence
          FROM episteme.faers_moiety_quarter_outcome o
         WHERE o.substance_key = v_moiety
           AND o.quarter >= v_q_from AND o.quarter <= p_to
         ORDER BY 2;
        GET DIAGNOSTICS v_n = ROW_COUNT;
        v_groups := v_groups + v_n;
    END IF;

    -- Per reaction term, as stored (faers_event_quarter holds the lowest term_id of
    -- a case-folded term).
    IF to_regclass('episteme.faers_event_quarter') IS NOT NULL
       AND to_regclass('episteme.faers_term') IS NOT NULL THEN
        RETURN QUERY
        WITH per_term AS (
            SELECT e.term_id,
                   sum(e.reports_suspect) AS suspect,
                   sum(e.reports_any) AS any_role
              FROM episteme.faers_event_quarter e
             WHERE e.substance_key = v_moiety
               AND e.quarter >= v_q_from AND e.quarter <= p_to
             GROUP BY e.term_id
        )
        SELECT 'claim'::text,
               v_groups + (row_number() OVER (
                   ORDER BY p.suspect DESC, p.any_role DESC, ft.term, p.term_id))::int,
               0,
               format('%s: %s reports name the substance as suspect, %s in any role, '
                      '%s to %s (%s)',
                      ft.term, p.suspect, p.any_role,
                      to_char(v_q_from::timestamp, 'YYYY"Q"Q'),
                      to_char(v_q_to::timestamp, 'YYYY"Q"Q'), v_label),
               p.suspect::text,
               'computed'::text,
               1.0::double precision,
               ('exact count of stored FAERS reports per moiety and reaction term, '
                'summed over the quarters')::text,
               'openfda_faers'::text,
               v_release,
               NULL::text,
               format('faers_event_quarter:%s:%s:%s:%s', v_moiety, p.term_id,
                      to_char(v_q_from::timestamp, 'YYYY-MM-DD'),
                      to_char(v_q_to::timestamp, 'YYYY-MM-DD')),
               'US'::text,
               v_licence
          FROM per_term p
          JOIN episteme.faers_term ft ON ft.term_id = p.term_id
         ORDER BY 2;
    END IF;

    RETURN NEXT episteme_api_internal.note_row(v_rule);
    SELECT string_agg(to_char(q, 'YYYY"Q"Q'), ', ' ORDER BY q) INTO v_gaps
      FROM generate_series(date_trunc('quarter', p_from::timestamp), p_to::timestamp,
                           interval '3 months') AS q
     WHERE NOT EXISTS (SELECT 1 FROM episteme.faers_quarter_totals t
                        WHERE t.quarter = q::date);
    IF v_gaps IS NOT NULL THEN
        RETURN NEXT episteme_api_internal.note_row(
            format('no FAERS data for quarters: %s', v_gaps));
    END IF;
    RETURN NEXT episteme_api_internal.note_row(
        'outcome is per report: a report with several outcomes counts once per outcome');
    RETURN NEXT episteme_api_internal.note_row(
        'reaction terms are quoted as FDA publishes them (MedDRA preferred terms), with no term grouping');
    IF NOT v_split THEN
        RETURN NEXT episteme_api_internal.note_row(
            'no FAERS outcome split: faers_moiety_quarter_outcome holds no row');
    END IF;
END
$$;

-- Public, capped function (family 'faers').
CREATE OR REPLACE FUNCTION episteme_api.faers_counts(
    p_substance_key bigint, p_from date, p_to date)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT * FROM episteme_api_internal.cap_claims(
        ARRAY(SELECT r FROM episteme_api_internal.faers_counts(p_substance_key, p_from, p_to)
              AS r),
        'faers')
$$;

-- -----------------------------------------------------------------------------
-- Ownership + grants
--   Ownership follows from running this file as episteme_sys_admin. The runtime
--   role episteme_app gets full DML on every table, then UPDATE/DELETE is
--   revoked on _audit and each of its partitions so the audit trail is
--   append-only (INSERT + SELECT) for the application.
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA episteme TO episteme_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA episteme TO episteme_app;
ALTER DEFAULT PRIVILEGES FOR ROLE episteme_sys_admin IN SCHEMA episteme
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO episteme_app;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA episteme TO episteme_app;
ALTER DEFAULT PRIVILEGES FOR ROLE episteme_sys_admin IN SCHEMA episteme
    GRANT USAGE, SELECT ON SEQUENCES TO episteme_app;

REVOKE UPDATE, DELETE ON episteme._audit           FROM episteme_app;
REVOKE UPDATE, DELETE ON episteme._audit_202609    FROM episteme_app;
REVOKE UPDATE, DELETE ON episteme._audit_202610    FROM episteme_app;
REVOKE UPDATE, DELETE ON episteme._audit_default   FROM episteme_app;
