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
--   Columns mirror article_schema.ARTICLE_COLUMNS MINUS the four text columns
--   (title, abstract, body_text, text) which live in episteme.article_body.
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
    text        text,
    PRIMARY KEY (article_id, source)
) PARTITION BY LIST (source);

-- ADR-0002 asks for zstd TOAST compression on body_text / text. The PG19beta3
-- build here rejects `zstd` (SQLSTATE 22023, invalid compression method), so
-- fall back to lz4 and record the deviation. Compression set on the parent is
-- inherited by partitions created afterwards.
DO $$ BEGIN
    EXECUTE 'ALTER TABLE episteme.article_body ALTER COLUMN body_text SET COMPRESSION zstd';
    EXECUTE 'ALTER TABLE episteme.article_body ALTER COLUMN text      SET COMPRESSION zstd';
    RAISE NOTICE 'article_body.body_text/text: zstd TOAST compression (ADR-0002)';
EXCEPTION WHEN invalid_parameter_value OR feature_not_supported THEN
    EXECUTE 'ALTER TABLE episteme.article_body ALTER COLUMN body_text SET COMPRESSION lz4';
    EXECUTE 'ALTER TABLE episteme.article_body ALTER COLUMN text      SET COMPRESSION lz4';
    RAISE NOTICE 'zstd unavailable on this build - article_body.body_text/text fell back to lz4 (ADR-0002 specifies zstd)';
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
--   below). episteme.create_audit_partition() / a rotation script is not yet
--   built (ADR-0002 names it; tracked as a follow-up) -- the DEFAULT
--   partition below absorbs inserts for any month that hasn't had its
--   partition created yet.
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
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE episteme._audit_202610 PARTITION OF episteme._audit
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
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
