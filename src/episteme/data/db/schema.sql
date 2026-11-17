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
            confidence        real,
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
    v_release text;
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
    IF to_regclass('episteme.reg_release') IS NOT NULL THEN
        SELECT rr.release_id INTO v_release
          FROM episteme.reg_release rr
         WHERE rr.dataset = 'fda_substance' AND rr.is_latest;
    END IF;
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
           (1.0 / k.n)::real,
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
-- COALESCE(moiety_key, substance_key) is M. moiety_key is M on every row. No rows
-- when the substance or the substance table does not exist.
CREATE OR REPLACE FUNCTION episteme_api_internal.substance_family(p_substance_key bigint)
RETURNS TABLE (substance_key bigint, moiety_key bigint, unii text, preferred_name text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
#variable_conflict use_column
DECLARE
    v_m bigint;
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
    SELECT s.substance_key, v_m, s.unii, s.preferred_name
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
RETURNS TABLE (substance_key bigint, moiety_key bigint, unii text, preferred_name text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT f.substance_key, f.moiety_key, f.unii, f.preferred_name
      FROM episteme_api_internal.substance_family(p_substance_key) AS f
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
