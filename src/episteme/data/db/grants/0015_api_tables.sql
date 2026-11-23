-- Grants file 0015, tables part -- what the SQL lookup API owner role
-- (episteme_api_owner, created by grants/0015_api_roles.sql) may read: USAGE on the schema
-- episteme and SELECT on exactly the relations below, and EXECUTE on the API functions.
-- The latest AACT release schema is in the companion file grants/0015_api_aact.sql.
--
-- Applied by episteme.data.db.api_grants.apply_owner_grants (this file, then the AACT
-- file): `python -m episteme.reference.api_settings sync` calls it, as
-- episteme_sys_admin, the owner of these tables and schemas (a superuser may run it too).
-- The AACT loader does NOT run this file; it runs only the AACT file
-- (api_grants.apply_aact_grants). Idempotent; no-op when the role does not exist. No
-- BEGIN/COMMIT: it runs inside the caller's transaction.
-- Run `api_settings sync` again after any rebuild of the schema episteme or of a table
-- below (a new relation has no grant; the functions then fail with "permission
-- denied").

-- ---------------------------------------------------------------------------
-- USAGE on the schemas the functions name
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    s text;
BEGIN
    IF to_regrole('episteme_api_owner') IS NULL THEN
        RAISE NOTICE 'role episteme_api_owner does not exist: apply grants/0015_api_roles.sql';
        RETURN;
    END IF;
    FOREACH s IN ARRAY ARRAY['episteme', 'episteme_api', 'episteme_api_internal'] LOOP
        IF to_regnamespace(s) IS NOT NULL THEN
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO episteme_api_owner', s);
        END IF;
    END LOOP;
    -- The settings tables: read only (licence_for, limit_for run as the owner role).
    FOREACH s IN ARRAY ARRAY['source_subset', 'result_limit'] LOOP
        IF to_regclass(format('episteme_api_internal.%I', s)) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT ON episteme_api_internal.%I TO episteme_api_owner', s);
        END IF;
    END LOOP;
    -- A function a migration recreated is owned by episteme_sys_admin until the roles file
    -- runs again; the owner role calls it from the functions it owns (the default
    -- privileges of the roles file cover new ones; this repairs older ones).
    FOREACH s IN ARRAY ARRAY['episteme_api', 'episteme_api_internal'] LOOP
        IF to_regnamespace(s) IS NOT NULL THEN
            EXECUTE format('GRANT EXECUTE ON ALL ROUTINES IN SCHEMA %I TO episteme_api_owner', s);
        END IF;
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
    IF to_regrole('episteme_api_owner') IS NULL THEN
        RETURN;
    END IF;
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
