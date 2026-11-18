-- Grants file 0015 -- the roles and privileges of the SQL lookup API (migration 0015).
-- Design: docs/superpowers/specs/2026-11-17-graphrag-api-layer-design.md, section 7.
--
-- Apply as a SUPERUSER (MCPg run_ddl refuses GRANT), to episteme_test and to episteme,
-- after migration 0015, for example:
--   psql -X -v ON_ERROR_STOP=1 -U <superuser> -d <database> -f src/episteme/data/db/grants/0015_api_roles.sql
-- then run `python -m episteme.reference.api_settings sync`. The sync applies the
-- companion file grants/0015_api_tables.sql (the owner's USAGE on episteme and SELECT on
-- exactly the relations the functions read, and the latest AACT release schema; the
-- table owner may run it, no superuser) and sets the reader's statement_timeout
-- (config EPISTEME_API_READER_STATEMENT_TIMEOUT_S).
-- Idempotent: run this file again after a migration that adds a function to
-- episteme_api or episteme_api_internal. Run `api_settings sync` again after each AACT
-- load and after any rebuild of a table the functions read.
-- Plain SQL, no psql meta-commands. One transaction.
--
-- No password here. The operator sets the reader's password to the value of the .env key
-- EPISTEME_API_READER_PASSWORD (psql: \password episteme_api_reader).
--
-- Roles (roles are global to the server; the privileges are per database):
--   * episteme_api_owner  (NOLOGIN) owns every function of episteme_api and
--     episteme_api_internal (SECURITY DEFINER: the functions run with its privileges)
--     and the settings tables. Its reads: grants/0015_api_tables.sql.
--   * episteme_api_reader (LOGIN) CONNECT, USAGE on episteme_api and EXECUTE on its
--     functions; nothing in episteme, episteme_api_internal or the AACT schemas.
--   * episteme_sys_admin (the migration role) becomes a member of episteme_api_owner
--     (INHERIT, no SET): re-applying migration 0015 replaces functions it no longer owns,
--     and `api_settings sync` writes the settings tables. It gets ADMIN on
--     episteme_api_reader (no INHERIT, no SET): ALTER ROLE ... SET statement_timeout.
--   * episteme_app (the pipeline role) keeps EXECUTE on the complete functions the Python
--     lookups call (reference._api.INTERNAL_FUNCTIONS) and on fts_doc: the expression of
--     the full-text indexes, evaluated when the pipeline writes article_body.

BEGIN;
SET LOCAL search_path = pg_catalog;

-- ---------------------------------------------------------------------------
-- Roles
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regrole('episteme_api_owner') IS NULL THEN
        CREATE ROLE episteme_api_owner NOLOGIN;
    END IF;
    IF to_regrole('episteme_api_reader') IS NULL THEN
        CREATE ROLE episteme_api_reader LOGIN;
    END IF;
END
$$;
ALTER ROLE episteme_api_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE episteme_api_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

DO $$
BEGIN
    IF to_regrole('episteme_sys_admin') IS NOT NULL THEN
        GRANT episteme_api_owner TO episteme_sys_admin WITH INHERIT TRUE, SET FALSE;
        GRANT episteme_api_reader TO episteme_sys_admin WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO episteme_api_reader', current_database());
END
$$;

-- ---------------------------------------------------------------------------
-- Schemas
-- ---------------------------------------------------------------------------
REVOKE ALL ON SCHEMA episteme_api, episteme_api_internal FROM PUBLIC;
REVOKE ALL ON SCHEMA episteme, episteme_api_internal FROM episteme_api_reader;
GRANT USAGE ON SCHEMA episteme_api, episteme_api_internal TO episteme_api_owner;
GRANT USAGE ON SCHEMA episteme_api TO episteme_api_reader;

-- ---------------------------------------------------------------------------
-- Functions and settings tables: owner, PUBLIC, reader
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT p.oid::regprocedure AS sig
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname IN ('episteme_api', 'episteme_api_internal')
           AND p.proowner <> 'episteme_api_owner'::regrole
         ORDER BY 1
    LOOP
        EXECUTE format('ALTER ROUTINE %s OWNER TO episteme_api_owner', r.sig);
    END LOOP;
END
$$;

ALTER TABLE episteme_api_internal.source_subset OWNER TO episteme_api_owner;
ALTER TABLE episteme_api_internal.result_limit OWNER TO episteme_api_owner;
REVOKE ALL ON ALL TABLES IN SCHEMA episteme_api, episteme_api_internal FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA episteme_api, episteme_api_internal FROM episteme_api_reader;

-- No function of either schema is executable by PUBLIC; the reader may execute the
-- public functions, and none of the internal ones.
REVOKE ALL ON ALL ROUTINES IN SCHEMA episteme_api, episteme_api_internal FROM PUBLIC;
REVOKE ALL ON ALL ROUTINES IN SCHEMA episteme_api_internal FROM episteme_api_reader;
GRANT EXECUTE ON ALL ROUTINES IN SCHEMA episteme_api TO episteme_api_reader;

-- Functions created later: not executable by PUBLIC. A per-schema default can only add
-- privileges, so this is the global default of the roles that create functions (the
-- migrations run as episteme_sys_admin; no other schema holds a function of theirs).
-- A new episteme_api function needs this file again before the reader can call it.
ALTER DEFAULT PRIVILEGES FOR ROLE episteme_api_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
DO $$
BEGIN
    IF to_regrole('episteme_sys_admin') IS NOT NULL THEN
        ALTER DEFAULT PRIVILEGES FOR ROLE episteme_sys_admin
            REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
    END IF;
END
$$;

-- The pipeline role: the complete functions of reference._api.INTERNAL_FUNCTIONS (the
-- Python lookups) and fts_doc (index expression). Nothing else of the API.
DO $$
DECLARE
    c_app constant text[] := ARRAY[
        'resolve_entity', 'substance_family', 'approval_status', 'safety_actions',
        'current_label', 'label_section_text', 'trials', 'faers_counts',
        'literature_by_mesh', 'graph_neighbours', 'search_text', 'lookup_code',
        'fts_doc'];
    r record;
BEGIN
    IF to_regrole('episteme_app') IS NULL THEN
        RETURN;
    END IF;
    REVOKE ALL ON ALL ROUTINES IN SCHEMA episteme_api, episteme_api_internal FROM episteme_app;
    GRANT USAGE ON SCHEMA episteme_api_internal TO episteme_app;
    FOR r IN
        SELECT p.oid::regprocedure AS sig
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'episteme_api_internal' AND p.proname = ANY (c_app)
         ORDER BY 1
    LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO episteme_app', r.sig);
    END LOOP;
END
$$;

COMMIT;
