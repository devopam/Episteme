-- Grants file 0015 -- the roles and privileges of the SQL lookup API (migration 0015).
-- Design: docs/superpowers/specs/2026-11-17-graphrag-api-layer-design.md, section 7.
--
-- Apply as a SUPERUSER (MCPg run_ddl refuses GRANT), to episteme_test and to episteme,
-- after migration 0015, for example:
--   psql -X -v ON_ERROR_STOP=1 -U <superuser> -d <database> -f src/episteme/data/db/grants/0015_api_roles.sql
-- then run `python -m episteme.reference.api_settings sync`. The sync applies the
-- companion files grants/0015_api_tables.sql (the owner's USAGE on episteme and SELECT
-- on exactly the relations the functions read) and grants/0015_api_aact.sql (the latest
-- AACT release schema); the table owner may run them, no superuser. It also sets the
-- reader's statement_timeout (config EPISTEME_API_READER_STATEMENT_TIMEOUT_S).
-- Idempotent: run this file again after a migration that adds a function to
-- episteme_api or episteme_api_internal, or that drops and recreates one (the new
-- function belongs to episteme_sys_admin until then, and the reader cannot call a new
-- public function). The AACT loader applies only the AACT file itself when it switches
-- to a new release (load_aact._switch); run `api_settings sync` again after any other
-- rebuild of a table the functions read.
-- Plain SQL, no psql meta-commands. One transaction.
--
-- No password here. The operator sets the reader's password to the value of the .env key
-- EPISTEME_API_READER_PASSWORD (psql: \password episteme_api_reader).
--
-- Roles (roles are global to the server; the privileges are per database):
--   * episteme_api_owner  (NOLOGIN) owns every function of episteme_api and
--     episteme_api_internal (SECURITY DEFINER: the functions run with its privileges).
--     It may only read the settings tables (episteme_sys_admin owns and writes them):
--     the definer identity has no write right anywhere. Its reads:
--     grants/0015_api_tables.sql and grants/0015_api_aact.sql. Every function sets
--     search_path = pg_catalog, pg_temp (pg_temp last: a caller's temporary objects
--     never shadow a name in the body).
--   * episteme_api_reader (LOGIN) CONNECT, USAGE on episteme_api and EXECUTE on its
--     functions; nothing in episteme, episteme_api_internal or the AACT schemas.
--   * episteme_sys_admin (the migration role) becomes a member of episteme_api_owner
--     (INHERIT, no SET): re-applying migration 0015 replaces functions it no longer owns.
--     It owns the settings tables, which `api_settings sync` writes. It gets ADMIN on
--     episteme_api_reader (no INHERIT, no SET): ALTER ROLE ... SET statement_timeout.
--   * episteme_app (the pipeline role) keeps EXECUTE on the complete functions the Python
--     lookups call (reference._api.INTERNAL_FUNCTIONS) and on fts_doc: the expression of
--     the full-text indexes, evaluated when the pipeline writes article_body.

BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;

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

-- Settings tables: owned and written by episteme_sys_admin (api_settings sync); the
-- owner role (the identity of every SECURITY DEFINER body) may only read them. Without
-- episteme_sys_admin the definer would keep owning (and writing) them: stop.
DO $$
BEGIN
    IF to_regrole('episteme_sys_admin') IS NULL THEN
        RAISE EXCEPTION 'role episteme_sys_admin does not exist: the settings tables need it as owner';
    END IF;
    ALTER TABLE episteme_api_internal.source_subset OWNER TO episteme_sys_admin;
    ALTER TABLE episteme_api_internal.result_limit OWNER TO episteme_sys_admin;
END
$$;
REVOKE ALL ON episteme_api_internal.source_subset, episteme_api_internal.result_limit
    FROM episteme_api_owner;
GRANT SELECT ON episteme_api_internal.source_subset, episteme_api_internal.result_limit
    TO episteme_api_owner;
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
-- Every function that reads an episteme table is SECURITY DEFINER (a test checks it), so
-- no caller needs a grant on the tables.
ALTER DEFAULT PRIVILEGES FOR ROLE episteme_api_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
DO $$
BEGIN
    IF to_regrole('episteme_sys_admin') IS NOT NULL THEN
        ALTER DEFAULT PRIVILEGES FOR ROLE episteme_sys_admin
            REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
        -- A function that a migration (re)creates is owned by episteme_sys_admin until
        -- this file runs again; the owner role must still be able to call it, because
        -- the SECURITY DEFINER functions it owns call their helpers as it (a recreated
        -- substance_family gave "permission denied for function substance_family").
        ALTER DEFAULT PRIVILEGES FOR ROLE episteme_sys_admin
            IN SCHEMA episteme_api, episteme_api_internal
            GRANT EXECUTE ON FUNCTIONS TO episteme_api_owner;
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
