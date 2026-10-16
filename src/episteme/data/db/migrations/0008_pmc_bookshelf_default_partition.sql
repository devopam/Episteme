-- Migration 0008 -- DEFAULT year partitions for pmc and bookshelf.
-- Idempotent. Apply as episteme_sys_admin (owner). One transaction (see 0005).
--
-- articles_pmc (schema.sql) and articles_bookshelf (migration 0002) are split by
-- RANGE (year) with no DEFAULT, so a row whose year is NULL or outside every range
-- (below 0, or 10000 and over) has no partition and fails its whole batch. The loader
-- maps a NULL year to 0, but an UPDATE can still produce one (enrich_from_lite set a
-- year-0 row's year to the feed's NULL until this change; it now stores 0). Migration
-- 0007 gave articles_pubmed the same catch-all.
--
-- A new DEFAULT partition needs no scan of the existing year partitions, and the
-- parent indexes cascade to it. Grants: DML through the parent checks the parent's
-- privileges only, and schema.sql's ALTER DEFAULT PRIVILEGES covers the new tables.

BEGIN;

CREATE TABLE IF NOT EXISTS episteme.articles_pmc_default
    PARTITION OF episteme.articles_pmc DEFAULT;
CREATE TABLE IF NOT EXISTS episteme.articles_bookshelf_default
    PARTITION OF episteme.articles_bookshelf DEFAULT;

COMMIT;
