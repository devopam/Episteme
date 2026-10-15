-- Migration 0007 -- pubmed gets its own LIST partition of articles and article_body.
-- Idempotent. Apply as episteme_sys_admin (owner). One transaction (see 0005).
--
-- Until now pubmed rows fell into articles_default / article_body_default next to
-- chembl, clinvar and the rest. A full pubmed reload then had to delete ~40M rows
-- scattered through those tables (~7 min per shard on the 5,400 rpm USB disk).
-- With its own partitions a full reload can TRUNCATE them instead.
--
-- PRECONDITION: the default partitions hold no pubmed row. CREATE TABLE ...
-- PARTITION OF scans the DEFAULT partition and fails if any row matches the new
-- bound, so on a database that already has pubmed loaded, purge pubmed first
-- (DELETE ... WHERE source = 'pubmed', audited as manual_correction). The scan
-- runs under an ACCESS EXCLUSIVE lock on articles / article_body: apply with no
-- load running.
--
-- Indexes: every index defined on the parent cascades to the new partitions.
-- A bulk-load group dropped at the time of applying (episteme._bulk_index_state)
-- is not on the parent, so it reaches these partitions at its rebuild.
-- Grants: DML through the parent checks the parent's privileges only, and
-- schema.sql's ALTER DEFAULT PRIVILEGES covers the new tables as well.

BEGIN;

-- Year ladder: pubmed spans 1781-2027 and ~65% of rows are from 2000 on, so the
-- recent decades get 5-year ranges (as articles_pmc does). year = 0 is the
-- unknown/sentinel bucket (the loader maps NULL -> 0).
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed
    PARTITION OF episteme.articles FOR VALUES IN ('pubmed')
    PARTITION BY RANGE (year);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_y0
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (0) TO (1);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_pre1950
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (1) TO (1950);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_1950_1970
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (1950) TO (1970);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_1970_1980
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (1970) TO (1980);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_1980_1990
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (1980) TO (1990);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_1990_2000
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (1990) TO (2000);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_2000_2005
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (2000) TO (2005);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_2005_2010
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (2005) TO (2010);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_2010_2015
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (2010) TO (2015);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_2015_2020
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (2015) TO (2020);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_2020_2025
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (2020) TO (2025);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_2025_2030
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (2025) TO (2030);
CREATE TABLE IF NOT EXISTS episteme.articles_pubmed_future
    PARTITION OF episteme.articles_pubmed FOR VALUES FROM (2030) TO (10000);

-- article_body: one partition per source, no year split (as pmc / bookshelf).
CREATE TABLE IF NOT EXISTS episteme.article_body_pubmed
    PARTITION OF episteme.article_body FOR VALUES IN ('pubmed');

COMMIT;
