-- Migration 0015 -- SQL lookup API: schemas episteme_api (public, capped functions)
-- and episteme_api_internal (settings, helpers, complete functions).
-- Idempotent. Apply as episteme_sys_admin (owner). One transaction.
-- Design: docs/superpowers/specs/2026-11-17-graphrag-api-layer-design.md
--
-- This file grows task by task. Task 1 adds the schemas, the two row types, the
-- settings tables (filled by `python -m episteme.reference.api_settings sync`) and
-- three helpers. No roles and no grants here: the superuser grants file does that.

BEGIN;

CREATE SCHEMA IF NOT EXISTS episteme_api;
CREATE SCHEMA IF NOT EXISTS episteme_api_internal;

-- Row types returned by the lookup functions (CREATE TYPE has no IF NOT EXISTS).
DO $$
BEGIN
    IF to_regtype('episteme_api_internal.claim_row') IS NULL THEN
        CREATE TYPE episteme_api_internal.claim_row AS (
            kind              text,
            claim_group       int,
            alt_rank          int,
            text              text,
            value             text,
            method            text,
            confidence        double precision,
            confidence_basis  text,
            source            text,
            release_id        text,
            source_file       text,
            record_key        text,
            jurisdiction      text,
            licence           text
        );
    END IF;
    IF to_regtype('episteme_api_internal.article_row') IS NULL THEN
        CREATE TYPE episteme_api_internal.article_row AS (
            kind        text,
            article_id  text,
            source      text,
            title       text,
            year        int,
            score       real,
            relation    text,
            release_id  text,
            licence     text,
            note        text
        );
    END IF;
END
$$;

-- claim_row.confidence was `real` in the first 0015 draft; a database that has that
-- shape gets double precision (no table column uses this type, so ALTER is safe).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute a
         WHERE a.attrelid = 'episteme_api_internal.claim_row'::regclass
           AND a.attname = 'confidence' AND NOT a.attisdropped
           AND a.atttypid = 'real'::regtype) THEN
        ALTER TYPE episteme_api_internal.claim_row
            ALTER ATTRIBUTE confidence TYPE double precision;
    END IF;
END
$$;

-- Licence class of each lookup source (copy of config.LOOKUP_SOURCE_SUBSETS).
CREATE TABLE IF NOT EXISTS episteme_api_internal.source_subset (
    source  text PRIMARY KEY,
    subset  text NOT NULL CHECK (subset IN ('commercial', 'pending', 'open_metadata'))
);

-- Row caps of the public functions (copy of config.api_result_limits()).
CREATE TABLE IF NOT EXISTS episteme_api_internal.result_limit (
    family         text PRIMARY KEY,
    default_limit  int NOT NULL,
    max_limit      int NOT NULL,
    CHECK (0 < default_limit AND default_limit <= max_limit)
);

-- Name key used by every name match: NFKC, then lower case. NULL in, NULL out.
CREATE OR REPLACE FUNCTION episteme_api_internal.norm_name(p text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog
AS $$ SELECT lower(normalize(p, NFKC)) $$;

-- Licence class of a source, or 'unlisted'.
CREATE OR REPLACE FUNCTION episteme_api_internal.licence_for(p_source text)
RETURNS text
LANGUAGE sql STABLE
SET search_path = pg_catalog
AS $$
    SELECT coalesce(
        (SELECT s.subset FROM episteme_api_internal.source_subset s WHERE s.source = p_source),
        'unlisted')
$$;

-- Row cap of a function family. An unknown family is an error: a missing cap must
-- not turn into "no cap".
CREATE OR REPLACE FUNCTION episteme_api_internal.limit_for(p_family text)
RETURNS int
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog
AS $$
DECLARE
    v int;
BEGIN
    SELECT r.default_limit INTO v
      FROM episteme_api_internal.result_limit r
     WHERE r.family = p_family;
    IF v IS NULL THEN
        RAISE EXCEPTION 'no result limit for family %', p_family;
    END IF;
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- Task 2: the public-wrapper caps, entity resolution and the substance family.
--
-- Row numbering of claim_row (every function): claim_group starts at 1, one group
-- per claim; alt_rank 0 is the claim, alt_rank > 0 its alternatives. note and
-- missing rows have NULL claim_group and alt_rank; a note's text is in `text`,
-- a missing source's name is in `source`.
--
-- Function bodies that read episteme tables are plpgsql: schema.sql runs before
-- migration 0010 creates those tables, and a LANGUAGE sql body is checked at
-- CREATE time.
-- ---------------------------------------------------------------------------

-- Public wrapper rule, claim rows. Keeps the first limit_for(p_family) claim rows
-- in array order and every note/missing row; when claim rows are cut, appends one
-- note row 'truncated: <kept> of <total>'. A public function calls it as
--   SELECT * FROM episteme_api_internal.cap_claims(
--       ARRAY(SELECT r FROM episteme_api_internal.<f>(<args>) AS r), '<family>')
CREATE OR REPLACE FUNCTION episteme_api_internal.cap_claims(
    p_rows episteme_api_internal.claim_row[], p_family text)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog
AS $$
DECLARE
    v_limit int := episteme_api_internal.limit_for(p_family);
    v_total int := 0;
    v_kept  int := 0;
    r       episteme_api_internal.claim_row;
    v_note  episteme_api_internal.claim_row;
BEGIN
    FOR i IN 1 .. coalesce(array_length(p_rows, 1), 0) LOOP
        r := p_rows[i];
        IF r.kind = 'claim' THEN
            v_total := v_total + 1;
            IF v_kept < v_limit THEN
                v_kept := v_kept + 1;
                RETURN NEXT r;
            END IF;
        ELSE
            RETURN NEXT r;
        END IF;
    END LOOP;
    IF v_total > v_kept THEN
        v_note.kind := 'note';
        v_note.text := format('truncated: %s of %s', v_kept, v_total);
        RETURN NEXT v_note;
    END IF;
END
$$;

-- Public wrapper rule, article rows: the same rule for kind 'article'; the
-- truncation note is in the `note` column.
CREATE OR REPLACE FUNCTION episteme_api_internal.cap_articles(
    p_rows episteme_api_internal.article_row[], p_family text)
RETURNS SETOF episteme_api_internal.article_row
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog
AS $$
DECLARE
    v_limit int := episteme_api_internal.limit_for(p_family);
    v_total int := 0;
    v_kept  int := 0;
    r       episteme_api_internal.article_row;
    v_note  episteme_api_internal.article_row;
BEGIN
    FOR i IN 1 .. coalesce(array_length(p_rows, 1), 0) LOOP
        r := p_rows[i];
        IF r.kind = 'article' THEN
            v_total := v_total + 1;
            IF v_kept < v_limit THEN
                v_kept := v_kept + 1;
                RETURN NEXT r;
            END IF;
        ELSE
            RETURN NEXT r;
        END IF;
    END LOOP;
    IF v_total > v_kept THEN
        v_note.kind := 'note';
        v_note.note := format('truncated: %s of %s', v_kept, v_total);
        RETURN NEXT v_note;
    END IF;
END
$$;

-- The is_latest release of the substance dataset 'fda_substance'
-- (load_substance.DATASET), or NULL (also when reg_release does not exist). The
-- release_id of every substance row (resolve_entity, substance_family).
CREATE OR REPLACE FUNCTION episteme_api_internal.substance_release()
RETURNS text
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog
AS $$
DECLARE
    v text;
BEGIN
    IF to_regclass('episteme.reg_release') IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT rr.release_id INTO v
      FROM episteme.reg_release rr
     WHERE rr.dataset = 'fda_substance' AND rr.is_latest;
    RETURN v;
END
$$;

-- Entity resolution: a drug name or a UNII to substances (SQL form of the former
-- resolve.py query). The input is trimmed; the UNII form is its upper case, the name
-- key is norm_name with inner white space collapsed (load_substance.resolve_key).
-- A substance matches by its own UNII or a substance_xref row, as UNION arms:
--   * substance.unii = UNII form          (when p_id_type is NULL or 'unii')
--   * xref 'unii' = UNII form             (when p_id_type is NULL)
--   * xref 'name' = name key              (when p_id_type is NULL)
--   * xref p_id_type = UNII form or key   (when p_id_type is given)
-- Order: moieties (moiety_key NULL or own key) first, then the lowest key. The first
-- row is the claim (alt_rank 0), the others its alternatives. One match: 'fetched',
-- confidence 1. n matches: 'mapped', confidence 1/n. No match, empty input or no
-- substance tables: one 'missing' row for source 'substance', never an error.
-- release_id: the is_latest reg_release row of dataset 'fda_substance'
-- (load_substance.DATASET), NULL when there is none.
CREATE OR REPLACE FUNCTION episteme_api_internal.resolve_entity(
    p_text text, p_id_type text DEFAULT NULL)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_raw     text := regexp_replace(coalesce(p_text, ''), '^\s+|\s+$', '', 'g');
    v_key     text;
    v_unii    text;
    v_release text := episteme_api_internal.substance_release();
    v_licence text := episteme_api_internal.licence_for('substance');
    v_missing episteme_api_internal.claim_row;
BEGIN
    v_key := btrim(regexp_replace(episteme_api_internal.norm_name(v_raw), '\s+', ' ', 'g'));
    v_unii := upper(v_raw);
    v_missing.kind := 'missing';
    v_missing.source := 'substance';
    v_missing.licence := v_licence;
    IF v_raw = '' OR v_key = ''
       OR to_regclass('episteme.substance') IS NULL
       OR to_regclass('episteme.substance_xref') IS NULL THEN
        RETURN NEXT v_missing;
        RETURN;
    END IF;
    v_release := episteme_api_internal.substance_release();
    RETURN QUERY
    WITH matched AS (
        SELECT s.substance_key FROM episteme.substance s
         WHERE s.unii = v_unii AND (p_id_type IS NULL OR p_id_type = 'unii')
        UNION
        SELECT x.substance_key FROM episteme.substance_xref x
         WHERE p_id_type IS NULL AND x.id_type = 'unii' AND x.id_value = v_unii
        UNION
        SELECT x.substance_key FROM episteme.substance_xref x
         WHERE p_id_type IS NULL AND x.id_type = 'name' AND x.id_value = v_key
        UNION
        SELECT x.substance_key FROM episteme.substance_xref x
         WHERE x.id_type = p_id_type AND x.id_value IN (v_unii, v_key)
    ), ranked AS (
        SELECT s.substance_key AS skey,
               nullif(s.unii, '') AS unii,
               nullif(s.preferred_name, '') AS pname,
               (row_number() OVER (
                    ORDER BY (s.moiety_key IS NULL OR s.moiety_key = s.substance_key) DESC,
                             s.substance_key) - 1)::int AS rk,
               (count(*) OVER ())::int AS n
          FROM episteme.substance s
          JOIN matched m ON m.substance_key = s.substance_key
    )
    SELECT 'claim'::text,
           1,
           k.rk,
           coalesce(k.pname, k.unii, k.skey::text),
           k.skey::text,
           CASE WHEN k.n = 1 THEN 'fetched' ELSE 'mapped' END,
           1.0::double precision / k.n,
           CASE WHEN k.n = 1 THEN 'exact UNII or normalised name match'
                ELSE k.n || ' substances share this name' END,
           'substance'::text,
           v_release,
           NULL::text,
           CASE WHEN k.unii IS NOT NULL THEN 'unii:' || k.unii END,
           NULL::text,
           v_licence
      FROM ranked k
     ORDER BY k.rk;
    IF NOT FOUND THEN
        RETURN NEXT v_missing;
    END IF;
END
$$;

-- The moiety family of a substance (SQL form of _scope.substance_scope): the moiety
-- M = COALESCE(moiety_key, substance_key) of the input, and every substance whose own
-- COALESCE(moiety_key, substance_key) is M. moiety_key is M on every row. Provenance
-- per row: source 'substance', the substance release, licence_for('substance'). No
-- rows when the substance or the substance table does not exist.
-- The first 0015 draft returned no provenance columns; a return type cannot change
-- in place, so an old shape is dropped (both functions) before the CREATE.
DO $$
BEGIN
    IF to_regprocedure('episteme_api_internal.substance_family(bigint)') IS NOT NULL
       AND pg_get_function_result(
               'episteme_api_internal.substance_family(bigint)'::regprocedure)
           NOT LIKE '%licence text%' THEN
        DROP FUNCTION IF EXISTS episteme_api.substance_family(bigint);
        DROP FUNCTION episteme_api_internal.substance_family(bigint);
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION episteme_api_internal.substance_family(p_substance_key bigint)
RETURNS TABLE (substance_key bigint, moiety_key bigint, unii text, preferred_name text,
               source text, release_id text, licence text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
#variable_conflict use_column
DECLARE
    v_m       bigint;
    v_release text := episteme_api_internal.substance_release();
    v_licence text := episteme_api_internal.licence_for('substance');
BEGIN
    IF to_regclass('episteme.substance') IS NULL THEN
        RETURN;
    END IF;
    SELECT coalesce(s.moiety_key, s.substance_key) INTO v_m
      FROM episteme.substance s
     WHERE s.substance_key = p_substance_key;
    IF v_m IS NULL THEN
        RETURN;
    END IF;
    -- Written so that substance_moiety_key_idx and the primary key serve it.
    RETURN QUERY
    SELECT s.substance_key, v_m, s.unii, s.preferred_name,
           'substance'::text, v_release, v_licence
      FROM episteme.substance s
     WHERE s.moiety_key = v_m OR (s.moiety_key IS NULL AND s.substance_key = v_m)
     ORDER BY s.substance_key;
END
$$;

-- Public, capped functions (family 'resolve').
CREATE OR REPLACE FUNCTION episteme_api.resolve_entity(p_text text, p_id_type text DEFAULT NULL)
RETURNS SETOF episteme_api_internal.claim_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT * FROM episteme_api_internal.cap_claims(
        ARRAY(SELECT r FROM episteme_api_internal.resolve_entity(p_text, p_id_type) AS r),
        'resolve')
$$;

-- A family is one moiety and its salts and esters: small, so no cap.
CREATE OR REPLACE FUNCTION episteme_api.substance_family(p_substance_key bigint)
RETURNS TABLE (substance_key bigint, moiety_key bigint, unii text, preferred_name text,
               source text, release_id text, licence text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT f.substance_key, f.moiety_key, f.unii, f.preferred_name,
           f.source, f.release_id, f.licence
      FROM episteme_api_internal.substance_family(p_substance_key) AS f
$$;

COMMIT;
