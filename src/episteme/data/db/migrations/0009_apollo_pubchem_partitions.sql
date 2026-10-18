-- Migration 0009 -- apollo and pubchem get their own LIST partitions of articles
-- and article_body.
-- Idempotent. Apply as episteme_sys_admin (owner). One transaction (see 0005).
--
-- Until now both sources fell into articles_default / article_body_default next to
-- chembl, clinvar, uniprot and the rest (~30 million rows, 8 GB + 15 GB). The
-- loader's per-shard id deletes then probe that whole partition: on the 5,400 rpm
-- USB disk one 400k-row apollo shard spent about an hour in the article_body id
-- delete (one random primary-key probe per id), and with stale statistics the
-- planner chose a full index scan per row instead, which ran for 3 h without
-- finishing (2026-10-18). apollo is ~8.4 million rows and pubchem ~120 million, so
-- both get partitions of their own, as pubmed did in 0007: each shard's deletes
-- then touch only that source's rows.
--
-- PRECONDITION: the default partitions hold no apollo or pubchem row. CREATE TABLE
-- ... PARTITION OF scans the DEFAULT partition and fails if any row matches the new
-- bound, so on a database that already has either loaded, purge it first (DELETE
-- ... WHERE source = '<source>', audited as manual_correction; remove the load
-- markers and _lineage rows of the purged shards so they load again). The scan
-- runs under an ACCESS EXCLUSIVE lock on articles / article_body: apply with no
-- load running.
--
-- Indexes: every index defined on the parent cascades to the new partitions, including
-- the articles (source, source_file) index that the loader's per-shard deletes use and
-- the article_body primary key (article_id, source).
-- Grants: DML through the parent checks the parent's privileges only, and
-- schema.sql's ALTER DEFAULT PRIVILEGES covers the new tables as well.

BEGIN;

-- No year ladder: neither source has a meaningful publication year (the loader
-- maps a missing year to 0), so a single LIST partition each.
CREATE TABLE IF NOT EXISTS episteme.articles_apollo
    PARTITION OF episteme.articles FOR VALUES IN ('apollo');
CREATE TABLE IF NOT EXISTS episteme.articles_pubchem
    PARTITION OF episteme.articles FOR VALUES IN ('pubchem');

CREATE TABLE IF NOT EXISTS episteme.article_body_apollo
    PARTITION OF episteme.article_body FOR VALUES IN ('apollo');
CREATE TABLE IF NOT EXISTS episteme.article_body_pubchem
    PARTITION OF episteme.article_body FOR VALUES IN ('pubchem');

COMMIT;
