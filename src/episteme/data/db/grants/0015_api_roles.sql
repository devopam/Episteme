-- Grants file 0015 -- the roles and privileges of the SQL lookup API (migration 0015).
-- Design: docs/superpowers/specs/2026-11-17-graphrag-api-layer-design.md, section 7.
--
-- Apply as a SUPERUSER (MCPg run_ddl refuses GRANT), to episteme_test and to episteme,
-- after migration 0015, for example:
--   psql -X -v ON_ERROR_STOP=1 -U <superuser> -d <database> -f src/episteme/data/db/grants/0015_api_roles.sql
-- then run `python -m episteme.reference.api_settings sync` (it sets the reader's
-- statement_timeout, config EPISTEME_API_READER_STATEMENT_TIMEOUT_S).
-- Idempotent: run it again
--   * after each AACT load (the owner may read only the latest aact_r<release> schema);
--   * after a migration that adds a function to episteme_api or episteme_api_internal;
--   * after a change to the lists below.
-- Plain SQL, no psql meta-commands. One transaction.
--
-- No password here. The operator sets the reader's password to the value of the .env key
-- EPISTEME_API_READER_PASSWORD (psql: \password episteme_api_reader).
--
-- Roles (roles are global to the server; the privileges are per database):
--   * episteme_api_owner  (NOLOGIN) owns every function of episteme_api and
--     episteme_api_internal (SECURITY DEFINER: the functions run with its privileges)
--     and the settings tables; SELECT on exactly the relations the functions read.
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
GRANT USAGE ON SCHEMA episteme, episteme_api, episteme_api_internal TO episteme_api_owner;
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

-- ---------------------------------------------------------------------------
-- The owner's SELECT on episteme: exactly the relations below
-- ---------------------------------------------------------------------------
-- c_read: every relation that a function body of migration 0015 reads
-- (FROM/JOIN episteme.<name>, including the dynamic SQL); a test derives the same list
-- from the migration. Partitioned tables need the grant on the parent only: a query
-- through the parent does not check the partitions' privileges, and no function names a
-- partition. code_current is a view (owner's rights, migration 0013), so the grant on the
-- view is enough.
-- c_faers_acl: the FAERS loader swaps faers_report and the signal tables by generation
-- and refuses a swap when a signal table's privileges differ from faers_report's
-- (faers.generation._check_access); the new tables copy the live grants
-- (generation.copy_access). So the owner gets SELECT on all of them, also on those no
-- function reads (faers_report, faers_pair_quarter, faers_batch_coverage,
-- faers_batch_quarter, faers_term_quarter): the ACLs stay equal.
-- Grants on relations off these lists are revoked. A relation that does not exist yet is
-- skipped (run the file again after its migration).
DO $$
DECLARE
    c_read constant text[] := ARRAY[
        'article_body', 'article_chemicals', 'article_cites', 'article_mesh', 'articles',
        'code_current', 'faers_event_quarter', 'faers_moiety_quarter',
        'faers_moiety_quarter_outcome', 'faers_quarter_totals', 'faers_term',
        'label_section', 'label_substance', 'mesh_tree', 'reg_record', 'reg_release',
        'substance', 'substance_xref'];
    c_faers_acl constant text[] := ARRAY[
        'faers_report', 'faers_pair_quarter', 'faers_event_quarter', 'faers_quarter_totals',
        'faers_batch_coverage', 'faers_batch_quarter', 'faers_moiety_quarter',
        'faers_term_quarter', 'faers_moiety_quarter_outcome'];
    v_all text[] := c_read || c_faers_acl;
    r record;
    t text;
BEGIN
    FOR r IN
        SELECT c.oid::regclass AS rel
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'episteme'
           AND NOT (c.relname = ANY (v_all))
           AND EXISTS (SELECT 1 FROM aclexplode(c.relacl) a
                        WHERE a.grantee = 'episteme_api_owner'::regrole)
    LOOP
        EXECUTE format('REVOKE ALL ON %s FROM episteme_api_owner', r.rel);
    END LOOP;
    FOREACH t IN ARRAY v_all LOOP
        IF to_regclass(format('episteme.%I', t)) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT ON episteme.%I TO episteme_api_owner', t);
        END IF;
    END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- AACT: the owner reads the release schema aact_r<release_id> of the is_latest 'aact'
-- release (trials, graph_neighbours 'trial'); older release schemas lose the grants.
-- After an AACT load the new schema has none until this file runs again: the two
-- functions then fail with "permission denied for schema aact_r<release>" (loud).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    c_aact constant text[] := ARRAY['studies', 'intervention_substance', 'study_references',
                                    '_load_state'];
    v_release text;
    v_schema  text;
    r record;
    t text;
BEGIN
    IF to_regclass('episteme.reg_release') IS NOT NULL THEN
        SELECT rr.release_id INTO v_release
          FROM episteme.reg_release rr
         WHERE rr.dataset = 'aact' AND rr.is_latest;
    END IF;
    IF v_release ~ '^[0-9]{8}$' AND to_regnamespace('aact_r' || v_release) IS NOT NULL THEN
        v_schema := 'aact_r' || v_release;
    END IF;

    FOR r IN
        SELECT n.nspname
          FROM pg_namespace n
         WHERE n.nspname ~ '^aact_r[0-9]{8}$' AND n.nspname IS DISTINCT FROM v_schema
           AND EXISTS (SELECT 1 FROM aclexplode(n.nspacl) a
                        WHERE a.grantee = 'episteme_api_owner'::regrole)
    LOOP
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM episteme_api_owner',
                       r.nspname);
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM episteme_api_owner', r.nspname);
    END LOOP;

    IF v_schema IS NULL THEN
        RAISE NOTICE 'no loaded is_latest aact release: no AACT grants';
        RETURN;
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO episteme_api_owner', v_schema);
    FOR r IN
        SELECT c.oid::regclass AS rel
          FROM pg_class c
         WHERE c.relnamespace = v_schema::regnamespace
           AND NOT (c.relname = ANY (c_aact))
           AND EXISTS (SELECT 1 FROM aclexplode(c.relacl) a
                        WHERE a.grantee = 'episteme_api_owner'::regrole)
    LOOP
        EXECUTE format('REVOKE ALL ON %s FROM episteme_api_owner', r.rel);
    END LOOP;
    FOREACH t IN ARRAY c_aact LOOP
        IF to_regclass(format('%I.%I', v_schema, t)) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT ON %I.%I TO episteme_api_owner', v_schema, t);
        END IF;
    END LOOP;
END
$$;

COMMIT;
