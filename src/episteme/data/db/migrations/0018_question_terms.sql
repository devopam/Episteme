-- Migration 0018 -- question to MeSH terms: mesh_term and mesh_term_vec tables, the
-- term_norm helper, and the new result_limit families.
-- Idempotent. Apply after 0017, as episteme_sys_admin (owner). One transaction.
-- Design: docs/superpowers/specs/2026-11-24-question-to-mesh-terms-design.md.
--
-- This file grows task by task. The first part adds the families, the two tables and
-- term_norm; the next part adds term_row, cap_terms, match_terms and suggest_terms (and
-- the release and revision indexes). After it, run the grants file
-- grants/0015_api_roles.sql (the reader gets EXECUTE on the new public functions), then
-- `python -m episteme.reference.api_settings sync` (it applies the
-- EPISTEME_API_LIMIT_<FAMILY> overrides and the owner grants). The `mesh terms` and
-- `mesh embed` steps fill the tables.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION '0018 needs the vector extension (scripts/data/db/install_extensions.sh)';
    END IF;
END
$$;

-- Seed values equal config.API_RESULT_LIMITS (a test compares them).
INSERT INTO episteme_api_internal.result_limit (family, default_limit, max_limit)
VALUES ('match_ngram_max', 8, 8),
       ('suggest', 10, 50),
       ('suggest_ef_search', 200, 200)
ON CONFLICT (family) DO NOTHING;

-- MeSH terms: one row per descriptor name or entry term (and per concept relation).
CREATE TABLE IF NOT EXISTS episteme.mesh_term (
    descriptor_ui    text NOT NULL,
    term             text NOT NULL,
    term_norm        text NOT NULL,
    term_kind        text NOT NULL CHECK (term_kind IN ('name', 'entry')),
    is_preferred     boolean NOT NULL,
    concept_ui       text,
    concept_relation text CHECK (concept_relation IN ('NRW', 'BRD', 'REL')),
    mesh_release     text NOT NULL,
    source_file      text NOT NULL
);
CREATE INDEX IF NOT EXISTS mesh_term_term_norm_idx ON episteme.mesh_term (term_norm);
CREATE INDEX IF NOT EXISTS mesh_term_descriptor_idx ON episteme.mesh_term (descriptor_ui);

-- MeSH term vectors (MedCPT article encoder, 768 dimensions, inner product).
CREATE TABLE IF NOT EXISTS episteme.mesh_term_vec (
    descriptor_ui  text NOT NULL,
    text_kind      text NOT NULL CHECK (text_kind IN ('descriptor', 'entry')),
    text           text NOT NULL,
    embedding      halfvec(768) NOT NULL,
    model          text NOT NULL,
    model_revision text NOT NULL,
    mesh_release   text NOT NULL
);
CREATE INDEX IF NOT EXISTS mesh_term_vec_hnsw_idx
    ON episteme.mesh_term_vec USING hnsw (embedding halfvec_ip_ops);
CREATE INDEX IF NOT EXISTS mesh_term_vec_descriptor_idx ON episteme.mesh_term_vec (descriptor_ui);

-- Term key used to match question words to MeSH terms: lower case, every character that
-- is not a letter or a digit becomes a space, runs of spaces collapse, trimmed. The
-- Python twin is episteme.reference.term_norm.term_norm. NULL in, NULL out.
CREATE OR REPLACE FUNCTION episteme_api_internal.term_norm(p text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$ SELECT btrim(regexp_replace(regexp_replace(lower(p), '[^[:alnum:]]+', ' ', 'g'), ' +', ' ', 'g')) $$;

-- ---------------------------------------------------------------------------
-- Task 6: match_terms and suggest_terms (term_row, cap_terms).
-- ---------------------------------------------------------------------------

-- suggest_terms compares the stored model revision and MeSH releases by min and max;
-- these indexes answer that without a scan of the vectors.
CREATE INDEX IF NOT EXISTS mesh_term_release_idx ON episteme.mesh_term (mesh_release);
CREATE INDEX IF NOT EXISTS mesh_term_vec_revision_idx ON episteme.mesh_term_vec (model_revision);
CREATE INDEX IF NOT EXISTS mesh_term_vec_release_idx ON episteme.mesh_term_vec (mesh_release);

-- Row type of match_terms and suggest_terms (CREATE TYPE has no IF NOT EXISTS).
-- kind 'term' (one MeSH descriptor) or 'note' (text in `note`). matched_text: the
-- normalized question words; matched_term: the MeSH term (exact) or the vector text
-- (medcpt); basis 'exact' or 'medcpt'; score: inner product (medcpt only).
DO $$
BEGIN
    IF to_regtype('episteme_api_internal.term_row') IS NULL THEN
        CREATE TYPE episteme_api_internal.term_row AS (
            kind              text,
            descriptor_ui     text,
            descriptor_name   text,
            matched_text      text,
            matched_term      text,
            term_kind         text,
            concept_relation  text,
            basis             text,
            score             real,
            mesh_release      text,
            note              text
        );
    END IF;
END
$$;

-- Public wrapper rule, term rows: the 0015 rule (cap_articles) for kind 'term'; the
-- truncation note is in the `note` column.
CREATE OR REPLACE FUNCTION episteme_api_internal.cap_terms(
    p_rows episteme_api_internal.term_row[], p_family text, p_precapped boolean DEFAULT false)
RETURNS SETOF episteme_api_internal.term_row
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_limit int := episteme_api_internal.limit_for(p_family);
    v_total int := 0;
    v_kept  int := 0;
    r       episteme_api_internal.term_row;
    v_note  episteme_api_internal.term_row;
BEGIN
    FOR i IN 1 .. coalesce(pg_catalog.array_length(p_rows, 1), 0) LOOP
        r := p_rows[i];
        IF r.kind = 'term' THEN
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
        v_note.note := CASE WHEN p_precapped
                            THEN pg_catalog.format('truncated: %s of more than %s', v_kept, v_kept)
                            ELSE pg_catalog.format('truncated: %s of %s', v_kept, v_total) END;
        RETURN NEXT v_note;
    END IF;
END
$$;

-- Exact step: question words to MeSH descriptors.
-- The question (its first 1000 characters) is normalized with term_norm and split into
-- words. One query reads every mesh_term row whose term_norm is an n-gram of the words
-- (n from 1 to limit_for('match_ngram_max')). Then, for n from the largest down to 1 and
-- left to right, a run of n unused words equal to a term_norm is a match: its words
-- become used. Each descriptor of that term_norm gets a match (two descriptors that
-- share a term both appear). One row per descriptor: the first match gives
-- matched_term, term_kind and concept_relation (name before entry, preferred first,
-- then term); the other phrases of the descriptor are appended to matched_text with
-- '; ', in question order. Rows in the order of their first word, then descriptor_ui.
-- A one-word run that is an English stop word is never a candidate.
-- Last, one note 'unmatched: <words>' for the unused words that are not English stop
-- words (to_tsvector('english', word) is empty). A question with no word, or with stop
-- words only, gives the one note 'question has no searchable words'.
CREATE OR REPLACE FUNCTION episteme_api_internal.match_terms(p_question text)
RETURNS SETOF episteme_api_internal.term_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_max    int := episteme_api_internal.limit_for('match_ngram_max');
    v_norm   text := episteme_api_internal.term_norm(pg_catalog.left(p_question, 1000));
    v_words  text[];
    v_nw     int;
    v_top    int;
    v_used   boolean[];
    v_grams  text[] := '{}';
    v_cands  episteme_api_internal.term_row[];
    v_keys   text[];
    v_gram   text;
    v_free   boolean;
    v_seen   text[];
    v_uis    text[] := '{}';
    v_rows   episteme_api_internal.term_row[] := '{}';
    v_first  int[] := '{}';
    v_m_ui   text[] := '{}';
    v_m_pos  int[] := '{}';
    v_m_text text[] := '{}';
    v_left   text[] := '{}';
    v_search boolean := false;
    v_k      int;
    c        episteme_api_internal.term_row;
    r        episteme_api_internal.term_row;
BEGIN
    IF v_norm IS NOT NULL AND v_norm <> '' THEN
        v_words := pg_catalog.string_to_array(v_norm, ' ');
    END IF;
    v_nw := coalesce(pg_catalog.cardinality(v_words), 0);
    FOR i IN 1 .. v_nw LOOP
        IF pg_catalog.length(pg_catalog.to_tsvector('english'::pg_catalog.regconfig,
                                                     v_words[i])) > 0 THEN
            v_search := true;
            EXIT;
        END IF;
    END LOOP;
    IF NOT v_search THEN
        r.kind := 'note';
        r.note := 'question has no searchable words';
        RETURN NEXT r;
        RETURN;
    END IF;

    -- A single stop word ("in", "as", "no") is never a candidate; a longer run may hold
    -- stop words.
    v_top := LEAST(v_max, v_nw);
    FOR n IN 1 .. v_top LOOP
        FOR i IN 1 .. v_nw - n + 1 LOOP
            CONTINUE WHEN n = 1 AND pg_catalog.length(pg_catalog.to_tsvector(
                'english'::pg_catalog.regconfig, v_words[i])) = 0;
            v_grams := v_grams || pg_catalog.array_to_string(v_words[i:i + n - 1], ' ');
        END LOOP;
    END LOOP;

    -- Candidates: matched_text holds the term_norm until the words are assigned.
    SELECT pg_catalog.array_agg(ROW('term', t.descriptor_ui, nm.term, t.term_norm, t.term,
               t.term_kind, t.concept_relation, 'exact', NULL, t.mesh_release, NULL)
               ::episteme_api_internal.term_row
               ORDER BY t.term_norm COLLATE "C", t.descriptor_ui COLLATE "C",
                        (t.term_kind = 'name') DESC, t.is_preferred DESC,
                        t.term COLLATE "C")
      INTO v_cands
      FROM episteme.mesh_term t
      LEFT JOIN LATERAL (
           SELECT d.term FROM episteme.mesh_term d
            WHERE d.descriptor_ui = t.descriptor_ui AND d.term_kind = 'name'
            ORDER BY d.term COLLATE "C"
            LIMIT 1) nm ON true
     WHERE t.term_norm = ANY (v_grams);
    SELECT pg_catalog.array_agg(DISTINCT x.matched_text)
      INTO v_keys
      FROM pg_catalog.unnest(v_cands) AS x;

    v_used := pg_catalog.array_fill(false, ARRAY[v_nw]);
    IF v_keys IS NOT NULL THEN
        FOR n IN REVERSE v_top .. 1 LOOP
            FOR i IN 1 .. v_nw - n + 1 LOOP
                v_free := true;
                FOR j IN i .. i + n - 1 LOOP
                    IF v_used[j] THEN
                        v_free := false;
                        EXIT;
                    END IF;
                END LOOP;
                CONTINUE WHEN NOT v_free;
                v_gram := pg_catalog.array_to_string(v_words[i:i + n - 1], ' ');
                CONTINUE WHEN NOT (v_gram = ANY (v_keys));
                FOR j IN i .. i + n - 1 LOOP
                    v_used[j] := true;
                END LOOP;
                v_seen := '{}';
                FOREACH c IN ARRAY v_cands LOOP
                    CONTINUE WHEN c.matched_text <> v_gram OR c.descriptor_ui = ANY (v_seen);
                    v_seen := v_seen || c.descriptor_ui;
                    v_m_ui := v_m_ui || c.descriptor_ui;
                    v_m_pos := v_m_pos || i;
                    v_m_text := v_m_text || v_gram;
                    v_k := pg_catalog.array_position(v_uis, c.descriptor_ui);
                    IF v_k IS NULL THEN
                        v_uis := v_uis || c.descriptor_ui;
                        v_rows := v_rows || c;
                        v_first := v_first || i;
                    ELSIF i < v_first[v_k] THEN
                        v_first[v_k] := i;
                    END IF;
                END LOOP;
            END LOOP;
        END LOOP;
    END IF;

    -- matched_text: the distinct phrases of each descriptor, in question order. unnest of
    -- several arrays is unqualified: only that spelling is the multi-array form (it
    -- resolves in pg_catalog, first on search_path).
    FOR k IN 1 .. pg_catalog.cardinality(v_uis) LOOP
        r := v_rows[k];
        SELECT pg_catalog.string_agg(s.t, '; ' ORDER BY s.p)
          INTO r.matched_text
          FROM (SELECT m.t, pg_catalog.min(m.p) AS p
                  FROM unnest(v_m_ui, v_m_pos, v_m_text) AS m(u, p, t)
                 WHERE m.u = v_uis[k]
                 GROUP BY m.t) s;
        v_rows[k] := r;
    END LOOP;

    RETURN QUERY
    SELECT x.kind, x.descriptor_ui, x.descriptor_name, x.matched_text, x.matched_term,
           x.term_kind, x.concept_relation, x.basis, x.score, x.mesh_release, x.note
      FROM pg_catalog.unnest(v_rows) WITH ORDINALITY
           AS x(kind, descriptor_ui, descriptor_name, matched_text, matched_term, term_kind,
                concept_relation, basis, score, mesh_release, note, ord)
     ORDER BY v_first[x.ord::int], x.descriptor_ui COLLATE "C";

    FOR i IN 1 .. v_nw LOOP
        IF NOT v_used[i]
           AND pg_catalog.length(pg_catalog.to_tsvector('english'::pg_catalog.regconfig,
                                                        v_words[i])) > 0 THEN
            v_left := v_left || v_words[i];
        END IF;
    END LOOP;
    IF pg_catalog.cardinality(v_left) > 0 THEN
        r := NULL;
        r.kind := 'note';
        r.note := 'unmatched: ' || pg_catalog.array_to_string(v_left, ' ');
        RETURN NEXT r;
    END IF;
END
$$;

-- Public, capped function (family 'resolve').
CREATE OR REPLACE FUNCTION episteme_api.match_terms(p_question text)
RETURNS SETOF episteme_api_internal.term_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT * FROM episteme_api_internal.cap_terms(
        ARRAY(SELECT r FROM episteme_api_internal.match_terms(p_question) AS r),
        'resolve')
$$;

-- Suggestion step: a question vector (MedCPT query encoder) to the nearest MeSH
-- descriptors. Not in schema.sql: it takes a pgvector type, and schema.sql runs without
-- pgvector (as the mesh_term_vec table).
-- Refusals (one note row, no term rows), in this order:
--   * p_vector or p_model_revision is NULL: 'vector and model revision are required';
--   * p_vector does not have 768 dimensions: 'vector must have 768 dimensions, got <n>';
--   * no row in mesh_term_vec: 'no vocabulary vectors; run mesh embed';
--   * the stored model_revision values are not exactly {p_model_revision}:
--     'model revision <r> does not match the stored vectors (<stored, comma list>)';
--   * the mesh_release of mesh_term_vec is not one value equal to the one mesh_release of
--     mesh_term: 'vocabulary vectors are from MeSH <a>, terms are from MeSH <b>; run
--     mesh embed'.
-- The checks read min and max through the btree indexes (no scan of the vectors).
-- Otherwise: hnsw.ef_search is greatest(limit_for('suggest_ef_search'), 4 * limit) for
-- the scan, so the scan LIMIT never exceeds it (set back before the return), the nearest
-- 4 * limit vectors by inner product, the best score
-- per descriptor (score = -(embedding <#> p_vector)), ordered by score descending, then
-- descriptor_ui, at most p_limit rows (NULL: limit_for('suggest')). matched_term is the
-- text of the best vector, term_kind its text_kind ('descriptor' or 'entry').
CREATE OR REPLACE FUNCTION episteme_api_internal.suggest_terms(
    p_vector public.halfvec(768), p_model_revision text, p_limit int DEFAULT NULL)
RETURNS SETOF episteme_api_internal.term_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_limit    int := coalesce(p_limit, episteme_api_internal.limit_for('suggest'));
    v_rev_min  text;
    v_rev_max  text;
    v_vrel_min text;
    v_vrel_max text;
    v_trel_min text;
    v_trel_max text;
    v_old_ef   text;
    r          episteme_api_internal.term_row;
BEGIN
    r.kind := 'note';
    IF p_vector IS NULL OR p_model_revision IS NULL THEN
        r.note := 'vector and model revision are required';
        RETURN NEXT r;
        RETURN;
    END IF;
    IF public.vector_dims(p_vector) <> 768 THEN
        r.note := pg_catalog.format('vector must have 768 dimensions, got %s',
                                    public.vector_dims(p_vector));
        RETURN NEXT r;
        RETURN;
    END IF;
    SELECT pg_catalog.min(v.model_revision), pg_catalog.max(v.model_revision)
      INTO v_rev_min, v_rev_max
      FROM episteme.mesh_term_vec v;
    IF v_rev_min IS NULL THEN
        r.note := 'no vocabulary vectors; run mesh embed';
        RETURN NEXT r;
        RETURN;
    END IF;
    IF v_rev_min <> v_rev_max OR p_model_revision IS DISTINCT FROM v_rev_min THEN
        r.note := pg_catalog.format('model revision %s does not match the stored vectors (%s)',
            p_model_revision,
            CASE WHEN v_rev_min = v_rev_max THEN v_rev_min
                 ELSE v_rev_min || ', ' || v_rev_max END);
        RETURN NEXT r;
        RETURN;
    END IF;
    SELECT pg_catalog.min(v.mesh_release), pg_catalog.max(v.mesh_release)
      INTO v_vrel_min, v_vrel_max
      FROM episteme.mesh_term_vec v;
    SELECT pg_catalog.min(t.mesh_release), pg_catalog.max(t.mesh_release)
      INTO v_trel_min, v_trel_max
      FROM episteme.mesh_term t;
    IF v_vrel_min <> v_vrel_max OR v_trel_min IS DISTINCT FROM v_trel_max
       OR v_trel_min IS DISTINCT FROM v_vrel_min THEN
        r.note := pg_catalog.format(
            'vocabulary vectors are from MeSH %s, terms are from MeSH %s; run mesh embed',
            CASE WHEN v_vrel_min = v_vrel_max THEN v_vrel_min
                 ELSE v_vrel_min || ', ' || v_vrel_max END,
            CASE WHEN v_trel_min IS NULL THEN 'none'
                 WHEN v_trel_min = v_trel_max THEN v_trel_min
                 ELSE v_trel_min || ', ' || v_trel_max END);
        RETURN NEXT r;
        RETURN;
    END IF;
    IF v_limit <= 0 THEN
        RETURN;
    END IF;

    v_old_ef := pg_catalog.current_setting('hnsw.ef_search', true);
    PERFORM pg_catalog.set_config('hnsw.ef_search',
        GREATEST(episteme_api_internal.limit_for('suggest_ef_search'), 4 * v_limit)::text,
        true);
    RETURN QUERY
    SELECT 'term'::text, b.descriptor_ui, nm.term, NULL::text, b.text, b.text_kind,
           NULL::text, 'medcpt'::text, b.score, b.mesh_release, NULL::text
      FROM (SELECT DISTINCT ON (h.descriptor_ui)
                   h.descriptor_ui, h.text, h.text_kind, h.mesh_release, h.score
              FROM (SELECT v.descriptor_ui, v.text, v.text_kind, v.mesh_release,
                           (-(v.embedding OPERATOR(public.<#>) p_vector))::real AS score
                      FROM episteme.mesh_term_vec v
                     ORDER BY v.embedding OPERATOR(public.<#>) p_vector
                     LIMIT 4 * v_limit) h
             ORDER BY h.descriptor_ui, h.score DESC, h.text_kind COLLATE "C",
                      h.text COLLATE "C") b
      LEFT JOIN LATERAL (
           SELECT t.term FROM episteme.mesh_term t
            WHERE t.descriptor_ui = b.descriptor_ui AND t.term_kind = 'name'
            ORDER BY t.term COLLATE "C"
            LIMIT 1) nm ON true
     ORDER BY b.score DESC, b.descriptor_ui COLLATE "C"
     LIMIT v_limit;
    IF v_old_ef IS NOT NULL THEN
        PERFORM pg_catalog.set_config('hnsw.ef_search', v_old_ef, true);
    END IF;
END
$$;

-- Public, capped function (family 'suggest').
CREATE OR REPLACE FUNCTION episteme_api.suggest_terms(
    p_vector public.halfvec(768), p_model_revision text)
RETURNS SETOF episteme_api_internal.term_row
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT * FROM episteme_api_internal.cap_terms(
        ARRAY(SELECT r FROM episteme_api_internal.suggest_terms(
                  p_vector, p_model_revision,
                  episteme_api_internal.limit_for('suggest') + 1) AS r),
        'suggest', true)
$$;

COMMIT;
