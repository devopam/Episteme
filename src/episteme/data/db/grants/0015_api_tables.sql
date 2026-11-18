-- Grants file 0015, tables part -- what the SQL lookup API owner role
-- (episteme_api_owner, created by grants/0015_api_roles.sql) may read: USAGE on the schema
-- episteme and SELECT on exactly the relations below, plus the release schema of the
-- latest AACT release.
--
-- Run by `python -m episteme.reference.api_settings sync` (as episteme_sys_admin, the owner
-- of these tables and schemas; a superuser may run it too). Idempotent; no-op when the
-- role does not exist. No BEGIN/COMMIT: the sync runs it inside its transaction.
-- Run the sync again after each AACT load (the new aact_r<release> schema has no grant
-- until then: trials and graph_neighbours 'trial' fail with "permission denied for schema")
-- and after any rebuild of the schema episteme or of a table below (a new relation has no
-- grant; the functions then fail with "permission denied").

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

-- ---------------------------------------------------------------------------
-- AACT: the owner reads the release schema aact_r<release_id> of the is_latest 'aact'
-- release (trials, graph_neighbours 'trial'); older release schemas lose the grants.
-- After an AACT load the new schema has none until this file runs again (api_settings
-- sync): the two functions then fail with "permission denied for schema aact_r<release>".
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
    IF to_regrole('episteme_api_owner') IS NULL THEN
        RETURN;
    END IF;
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
