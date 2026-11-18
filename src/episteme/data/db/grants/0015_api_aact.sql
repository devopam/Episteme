-- Grants file 0015, AACT part -- the SQL lookup API owner role (episteme_api_owner,
-- created by grants/0015_api_roles.sql) reads the release schema aact_r<release_id> of
-- the is_latest 'aact' release, and nothing in older release schemas.
--
-- Applied by episteme.data.db.api_grants:
--   * apply_aact_grants: the AACT loader (load_aact._switch), in the transaction that
--     makes a release the latest. It runs ONLY this file: it changes the privileges of
--     the aact_r* schemas and their tables, which only the AACT loader changes, and of no
--     episteme relation or function. A concurrent DDL, FAERS swap or api_settings sync
--     on those does not make the switch wait or fail.
--   * apply_owner_grants: `python -m episteme.reference.api_settings sync`, after
--     grants/0015_api_tables.sql.
-- Run as episteme_sys_admin (the owner of the release schemas) or a superuser.
-- Idempotent; no-op when the role does not exist. No BEGIN/COMMIT: it runs inside the
-- caller's transaction.

-- ---------------------------------------------------------------------------
-- AACT: the owner reads the release schema aact_r<release_id> of the is_latest 'aact'
-- release (trials, graph_neighbours 'trial'); older release schemas lose the grants.
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
