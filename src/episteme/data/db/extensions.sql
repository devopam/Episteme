-- episteme storage core: extension bootstrap
--
-- Run while connected to the target database as episteme_sys_admin, BEFORE
-- schema.sql. Applied by scripts/data/db/init_database.sh (Task 5) and by every
-- `pg`-marked test's setup.
--
-- ADR-0002 wants pg_stat_statements enabled for slow-query monitoring. It is
-- present on the PG19beta3 build but NOT marked trusted, so CREATE EXTENSION
-- needs a superuser -- and episteme_sys_admin is a non-superuser (LOGIN
-- CREATEDB CREATEROLE only). Every CREATE EXTENSION here is therefore
-- best-effort: it degrades to a NOTICE so this file runs clean as
-- episteme_sys_admin. A superuser (or shared_preload_libraries +
-- template install) enables pg_stat_statements out of band.
--
-- pgvector (`vector`) and `pg_search` are absent on the 19beta3/Windows build;
-- the columns and indexes that depend on them are deferred to migrations/0001.

DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
EXCEPTION WHEN insufficient_privilege OR undefined_file OR feature_not_supported THEN
    RAISE NOTICE 'pg_stat_statements not enabled here (needs superuser) - enable it out of band';
END $$;

DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION WHEN insufficient_privilege OR undefined_file OR feature_not_supported THEN
    RAISE NOTICE 'pgvector unavailable - chunks.embedding deferred (migrate 0001)';
END $$;

DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_search;
EXCEPTION WHEN insufficient_privilege OR undefined_file OR feature_not_supported THEN
    RAISE NOTICE 'pg_search unavailable - BM25 indexes deferred (migrate 0001)';
END $$;
