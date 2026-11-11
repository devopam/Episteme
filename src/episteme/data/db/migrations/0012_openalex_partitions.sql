-- Migration 0012 -- openalex gets its own LIST partitions of articles and
-- article_body, and pubmed / pmc get a lower(doi) index for the openalex dedup.
-- Idempotent. Apply as episteme_sys_admin (owner). One transaction (see 0005).
--
-- Owner decision (2026-11-11): load only the OpenAlex works that pubmed and pmc
-- do not already hold. The serializer stages ~79.4 million biomedical works; the
-- loader (postgres_loader, openalex only) skips a work whose pmid, else pmcid,
-- else DOI matches a pubmed or pmc row. The rest would land in articles_default
-- next to chembl, clinvar and the rest, where the loader's per-shard deletes
-- probe the whole partition (see 0009). A partition of its own keeps them to
-- openalex rows only.
--
-- PRECONDITION: the default partitions hold no openalex row. CREATE TABLE ...
-- PARTITION OF scans the DEFAULT partition and fails if any row matches the new
-- bound. The DO block below checks articles_default first (the (source,
-- source_file) index of 0004 serves the probe) and stops with a clear message;
-- article_body_default is checked by the CREATE itself (CheckViolation). On a
-- database that already has openalex loaded, purge it first (DELETE ... WHERE
-- source = 'openalex', audited as manual_correction; remove the load markers
-- and _lineage rows of the purged shards so they load again). The scan runs
-- under an ACCESS EXCLUSIVE lock on articles / article_body: apply with no load
-- running.
--
-- DOI index: pubmed and pmc store the DOI as the publisher writes it (mixed
-- case); OpenAlex writes it in lower case. DOIs are case-insensitive, so the
-- dedup compares lower(doi). articles_doi_idx is on the raw doi and cannot serve
-- that, so each of the two partitioned tables gets an expression index of its
-- own. It is NOT on the articles parent: the other sources never need it, and
-- the bulk-load index group (bulk_indexes.COVERED_INDEXES) does not drop it.
-- The build reads every pubmed and pmc row (~40 million on the 5,400 rpm USB
-- disk: expect tens of minutes) and holds a SHARE lock on those partitions,
-- which blocks their writes, not their reads.
--
-- Indexes: every index defined on the parent cascades to the new partitions,
-- including the articles (source, source_file) index that the loader's
-- per-shard deletes use and the article_body primary key (article_id, source).
-- Grants: DML through the parent checks the parent's privileges only, and
-- schema.sql's ALTER DEFAULT PRIVILEGES covers the new tables as well.

BEGIN;

-- The partition DDL below takes ACCESS EXCLUSIVE locks on articles and article_body.
-- Fail after 60 s instead of queueing every reader behind a long wait (runbook 2.3).
SET LOCAL lock_timeout = '60s';

DO $$
BEGIN
    IF to_regclass('episteme.articles_openalex') IS NULL
       AND EXISTS (SELECT 1 FROM episteme.articles_default WHERE source = 'openalex') THEN
        RAISE EXCEPTION 'migration 0012: episteme.articles_default holds openalex rows'
            USING HINT = 'Purge them first (audited DELETE ... WHERE source = ''openalex'', '
                         'then remove the purged shards'' load markers and _lineage rows); '
                         'see docs/10 section 2.3.';
    END IF;
END
$$;

-- No year ladder (as 0009): one LIST partition each.
CREATE TABLE IF NOT EXISTS episteme.articles_openalex
    PARTITION OF episteme.articles FOR VALUES IN ('openalex');
CREATE TABLE IF NOT EXISTS episteme.article_body_openalex
    PARTITION OF episteme.article_body FOR VALUES IN ('openalex');

-- Case-insensitive DOI lookup for the openalex dedup (see the header).
CREATE INDEX IF NOT EXISTS articles_pubmed_doi_lower_idx
    ON episteme.articles_pubmed (lower(doi)) WHERE doi IS NOT NULL;
CREATE INDEX IF NOT EXISTS articles_pmc_doi_lower_idx
    ON episteme.articles_pmc (lower(doi)) WHERE doi IS NOT NULL;

COMMIT;
