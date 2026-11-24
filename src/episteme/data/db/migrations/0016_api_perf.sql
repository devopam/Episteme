-- Migration 0016 -- SQL lookup API: performance follow-up of 0015.
-- Idempotent. Apply after 0015, as episteme_sys_admin (owner). One transaction.
-- Design: docs/superpowers/specs/2026-11-24-api-layer-perf-design.md (addendum to
-- 2026-11-17-graphrag-api-layer-design.md).
--
-- The functions keep their 0015 signatures and return types; only the bodies change.
-- schema.sql carries the same bodies. New result_limit families
-- ('literature_window', 'search_rank_chars'): this file seeds their default rows
-- (ON CONFLICT DO NOTHING), so the functions work before `api_settings sync`. Run the
-- sync after this file anyway: it applies the EPISTEME_API_LIMIT_<FAMILY> overrides.

BEGIN;

-- Seed values equal config.API_RESULT_LIMITS (a test compares them).
INSERT INTO episteme_api_internal.result_limit (family, default_limit, max_limit)
VALUES ('literature_window', 5, 5), ('search_rank_chars', 10000, 10000)
ON CONFLICT (family) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Descending year windows for literature_by_mesh. Bounds: lo_min = coalesce(p_from, 1),
-- hi_max = coalesce(p_to, 2147483647) (no year is above it); no row when lo_min > hi_max. The first window ends
-- at hi_max and starts p_first_width years before least(hi_max, p_now + 1) (at least
-- lo_min). Each next window ends one year below the previous lo, is twice as wide as
-- the previous width, and stops at lo_min. n numbers the windows from 1, newest
-- first. p_now is a parameter (the caller passes the current year), so the function
-- is IMMUTABLE. A width below 1 counts as 1. The arithmetic is bigint, so an extreme
-- bound cannot overflow.
CREATE OR REPLACE FUNCTION episteme_api_internal.year_windows(
    p_from int, p_to int, p_first_width int, p_now int)
RETURNS TABLE (lo int, hi int, n int)
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $$
    WITH RECURSIVE b AS (
        SELECT coalesce(p_from, 1)::bigint AS lo_min,
               coalesce(p_to, 2147483647)::bigint AS hi_max,
               greatest(coalesce(p_first_width, 1), 1)::bigint AS width
    ), w (lo, hi, width, n) AS (
        SELECT greatest(b.lo_min, least(b.hi_max, p_now::bigint + 1) - b.width + 1),
               b.hi_max, b.width, 1
          FROM b
         WHERE b.lo_min <= b.hi_max
        UNION ALL
        SELECT greatest(b.lo_min, w.lo - 2 * w.width), w.lo - 1, 2 * w.width, w.n + 1
          FROM w CROSS JOIN b
         WHERE w.lo > b.lo_min
    )
    SELECT w.lo::int, w.hi::int, w.n::int FROM w ORDER BY w.n
$$;

-- ---------------------------------------------------------------------------
-- literature_by_mesh (family 'literature'). SQL form of the former
-- literature.literature, with topic expansion.
--
-- Substance side (as before): a PubMed record matches when one of its MeSH
-- descriptors has a lower()-name equal to the norm_name of a preferred name of the
-- substance's moiety family, or to a 'name' xref of a family member.
-- Topic side: p_topic (trimmed, lower case) names a descriptor by
-- lower(descriptor_name). The descriptor is expanded to itself and every descriptor
-- below it in mesh_tree (tree numbers that start with one of its tree numbers plus
-- '.'). A record then needs one descriptor of that set, or one qualifier named like
-- the topic (the v1 behaviour, for example 'adverse effects'). A topic that names no
-- descriptor gives a note row and the qualifier match only. Without mesh_tree the set
-- is the descriptor alone.
-- The descriptor_ui of a name is read from one article_mesh row (LIMIT 1): a name
-- belongs to one descriptor.
-- Year: the largest year of the PMID's 'pubmed' rows in articles (0 or a year below 1 =
-- unknown); a year bound applies to that largest year. One 'pubmed' row per
-- PMID is expected (articles.id = 'pubmed:<pmid>'), so in practice it is the PMID's
-- year. The years of other sources are not used (0016: a europepmc_manuscript row
-- carries the deposit year, not the publication year). A year bound drops records with an unknown
-- year. Order: year descending (unknown last), then article_id. article_id is
-- 'pubmed:<pmid>'. release_id: the is_latest reg_release row of dataset 'pubmed', NULL
-- when there is none. relation: the matched descriptor names, comma separated. score
-- is 1.
-- source_file: the smallest source_file of the matched descriptor rows of the PMID.
-- p_limit (NULL = no limit) keeps a caller from building every hit. The public
-- wrapper passes limit_for('literature') + 1 and p_precapped true, so cap_articles
-- still sees one record more than it keeps and writes the note 'truncated: <n> of
-- more than <n>' (the true total is not built).
--
-- Method (0016): the result is the same as one sort of every hit, but the function
-- reads years for few hits.
--   1. Hits: the PMIDs of the matched descriptor rows (pmid only).
--   2. Year windows (year_windows; the first window is limit_for('literature_window')
--      years wide, each next window twice as wide), newest first. Each window reads
--      the 'pubmed' rows of the hits not seen yet (v_rest) with a year in the window,
--      and keeps max(year) per PMID. A PMID is seen in the newest window that holds
--      one of its 'pubmed' rows, and only there: the window removes it from v_rest
--      (a hash EXCEPT, so a large hit set costs no array scan per row). The topic
--      check runs on the rows of the window.
--      After a window, the function stops when it holds at least p_limit rows: a later
--      window has only older years, and every tie of the window is present.
--   3. Unknown years (no 'pubmed' row, or year 0): only without a year bound, and only
--      when the windows gave fewer than p_limit rows.
--   4. Sort, cut at p_limit, then descriptors and source_file for the kept rows only.
-- Indexes: lower(descriptor_name) on article_mesh serves the name match; with the
-- covering form (lower(descriptor_name)) INCLUDE (descriptor_name, pmid) the hit step
-- is an index-only scan. Step 4 is a pmid lookup bounded by p_limit rows: it reads
-- min(m.source_file), which neither covering index holds. articles_pmid_idx (partial, pmid IS NOT NULL;
-- a.pmid = ANY (...) implies it) serves the window probes, and the year bounds prune
-- the RANGE (year) partitions of articles_pubmed. The article_mesh indexes are NOT
-- created here (article_mesh has ~345M rows): they are in bulk_indexes INDEX_GROUPS
-- (group pubmed_graph); see the runbook for the production build.
-- No substance: a note row and a missing row for 'article_mesh'; no article_mesh,
-- articles or article_mesh row: the missing row only.
CREATE OR REPLACE FUNCTION episteme_api_internal.literature_by_mesh(
    p_substance_key bigint, p_topic text, p_from_year int DEFAULT NULL,
    p_to_year int DEFAULT NULL, p_limit int DEFAULT NULL)
RETURNS SETOF episteme_api_internal.article_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_keys    bigint[];
    v_names   text[];
    v_topic   text := lower(btrim(coalesce(p_topic, '')));
    v_ui      text;
    v_uis     text[] := ARRAY[]::text[];
    v_release text;
    v_licence text := episteme_api_internal.licence_for('pubmed');
    v_row     episteme_api_internal.article_row;
    v_has     boolean;
    v_pmids   text[];
    v_rest    text[];
    v_pmid    text[] := ARRAY[]::text[];
    v_year    int[] := ARRAY[]::int[];
    v_new_s   text[];
    v_new_p   text[];
    v_new_y   int[];
    v_above   text[];
    w         record;
BEGIN
    IF to_regclass('episteme.substance') IS NOT NULL
       AND to_regclass('episteme.substance_xref') IS NOT NULL THEN
        SELECT array_agg(f.substance_key ORDER BY f.substance_key) INTO v_keys
          FROM episteme_api_internal.substance_family(p_substance_key) AS f;
    END IF;
    IF v_keys IS NULL THEN
        v_row.kind := 'note';
        v_row.note := format('substance %s not found', p_substance_key);
        RETURN NEXT v_row;
        RETURN NEXT episteme_api_internal.missing_article('article_mesh', 'pubmed');
        RETURN;
    END IF;
    IF to_regclass('episteme.article_mesh') IS NOT NULL
       AND to_regclass('episteme.articles') IS NOT NULL THEN
        SELECT EXISTS (SELECT 1 FROM episteme.article_mesh) INTO v_has;
    END IF;
    IF NOT coalesce(v_has, false) THEN
        RETURN NEXT episteme_api_internal.missing_article('article_mesh', 'pubmed');
        RETURN;
    END IF;

    SELECT array_agg(DISTINCT s.n) INTO v_names
      FROM (SELECT episteme_api_internal.norm_name(f.preferred_name) AS n
              FROM episteme_api_internal.substance_family(p_substance_key) AS f
             WHERE f.preferred_name IS NOT NULL
            UNION
            SELECT x.id_value
              FROM episteme.substance_xref x
             WHERE x.substance_key = ANY (v_keys) AND x.id_type = 'name') s
     WHERE s.n IS NOT NULL AND s.n <> '';
    IF v_names IS NULL OR coalesce(p_limit, 1) <= 0 THEN
        RETURN;
    END IF;

    IF v_topic <> '' THEN
        SELECT m.descriptor_ui INTO v_ui
          FROM episteme.article_mesh m
         WHERE lower(m.descriptor_name) = v_topic AND m.descriptor_ui IS NOT NULL
         LIMIT 1;
        IF v_ui IS NULL THEN
            v_row.kind := 'note';
            v_row.note := format(
                'topic %s is not a MeSH descriptor name in the indexed records; '
                'matched on qualifier names only', btrim(p_topic));
            RETURN NEXT v_row;
            v_row := NULL;
        ELSE
            v_uis := ARRAY[v_ui];
            IF to_regclass('episteme.mesh_tree') IS NOT NULL THEN
                SELECT array_agg(DISTINCT d.descriptor_ui) INTO v_uis
                  FROM episteme.mesh_tree p
                 CROSS JOIN LATERAL (
                       SELECT t.descriptor_ui FROM episteme.mesh_tree t
                        WHERE t.tree_number = p.tree_number
                           OR (t.tree_number ~>=~ (p.tree_number || '.')
                               AND t.tree_number ~<~ (p.tree_number || '/'))) d
                 WHERE p.descriptor_ui = v_ui;
                v_uis := array_append(coalesce(v_uis, ARRAY[]::text[]), v_ui);
            END IF;
        END IF;
    END IF;

    IF to_regclass('episteme.reg_release') IS NOT NULL THEN
        SELECT rr.release_id INTO v_release
          FROM episteme.reg_release rr
         WHERE rr.dataset = 'pubmed' AND rr.is_latest;
    END IF;

    -- 1. Hits: pmid only (index-only scan on the covering name index).
    v_pmids := ARRAY(SELECT DISTINCT m.pmid
                       FROM episteme.article_mesh m
                      WHERE lower(m.descriptor_name) = ANY (v_names));

    -- 2. Year windows, newest first. v_rest: the hits no window gave yet (with or
    -- without the topic), so no later window or the unknown step takes one again.
    v_rest := v_pmids;
    FOR w IN
        SELECT y.lo, y.hi
          FROM episteme_api_internal.year_windows(
                   p_from_year, p_to_year,
                   episteme_api_internal.limit_for('literature_window'),
                   extract(year FROM current_date)::int) AS y
         ORDER BY y.n
    LOOP
        EXIT WHEN cardinality(v_rest) = 0;
        SELECT array_agg(c.pmid ORDER BY c.pmid),
               array_agg(c.pmid ORDER BY c.pmid) FILTER (WHERE c.ok),
               array_agg(c.year ORDER BY c.pmid) FILTER (WHERE c.ok)
          INTO v_new_s, v_new_p, v_new_y
          FROM (SELECT g.pmid, g.year,
                       (v_topic = ''
                        OR EXISTS (
                            SELECT 1 FROM episteme.article_mesh t
                             WHERE t.pmid = g.pmid
                               AND (t.descriptor_ui = ANY (v_uis)
                                    OR EXISTS (SELECT 1 FROM unnest(t.qualifiers) q
                                                WHERE lower(q) = v_topic)))) AS ok
                  FROM (SELECT a.pmid, max(a.year) AS year
                          FROM episteme.articles a
                         WHERE a.source = 'pubmed'
                           AND a.year BETWEEN w.lo AND w.hi
                           AND a.year > 0
                           AND a.pmid = ANY (v_rest)
                         GROUP BY a.pmid) g) c;
        IF v_new_s IS NOT NULL THEN
            v_rest := ARRAY(SELECT unnest(v_rest) EXCEPT SELECT unnest(v_new_s));
        END IF;
        -- An upper year bound applies to the largest 'pubmed' year of the PMID (as in
        -- 0015): a PMID with a 'pubmed' row above p_to_year is not kept. One array
        -- probe per window, on the partitions above the bound only.
        IF p_to_year IS NOT NULL AND v_new_p IS NOT NULL THEN
            v_above := ARRAY(SELECT DISTINCT a.pmid
                               FROM episteme.articles a
                              WHERE a.source = 'pubmed'
                                AND a.year > p_to_year
                                AND a.pmid = ANY (v_new_p));
            IF cardinality(v_above) > 0 THEN
                SELECT array_agg(u.p ORDER BY u.p), array_agg(u.y ORDER BY u.p)
                  INTO v_new_p, v_new_y
                  FROM unnest(v_new_p, v_new_y) AS u(p, y)
                 WHERE u.p <> ALL (v_above);
            END IF;
        END IF;
        v_pmid := v_pmid || coalesce(v_new_p, ARRAY[]::text[]);
        v_year := v_year || coalesce(v_new_y, ARRAY[]::int[]);
        EXIT WHEN p_limit IS NOT NULL AND cardinality(v_pmid) >= p_limit;
    END LOOP;

    -- 3. Unknown years: no year bound, and the windows gave fewer than p_limit rows.
    IF p_from_year IS NULL AND p_to_year IS NULL
       AND (p_limit IS NULL OR cardinality(v_pmid) < p_limit) THEN
        SELECT array_agg(h.pmid ORDER BY h.pmid) INTO v_new_p
          FROM unnest(v_rest) AS h(pmid)
         WHERE (v_topic = ''
                OR EXISTS (
                    SELECT 1 FROM episteme.article_mesh t
                     WHERE t.pmid = h.pmid
                       AND (t.descriptor_ui = ANY (v_uis)
                            OR EXISTS (SELECT 1 FROM unnest(t.qualifiers) q
                                        WHERE lower(q) = v_topic))));
        v_new_p := coalesce(v_new_p, ARRAY[]::text[]);
        v_pmid := v_pmid || v_new_p;
        v_year := v_year || array_fill(NULL::int, ARRAY[cardinality(v_new_p)]);
    END IF;

    -- 4. Sort and cut, then descriptors and source_file for the kept rows only.
    RETURN QUERY
    SELECT 'article'::text, 'pubmed:' || k.pmid, 'pubmed'::text, NULL::text, k.year,
           1.0::real, array_to_string(d.descriptors, ', '), v_release, v_licence,
           NULL::text, d.source_file
      FROM (SELECT u.pmid, u.year
              FROM unnest(v_pmid, v_year) AS u(pmid, year)
             ORDER BY u.year DESC NULLS LAST, 'pubmed:' || u.pmid
             LIMIT p_limit) k
      CROSS JOIN LATERAL (
           SELECT array_agg(DISTINCT m.descriptor_name ORDER BY m.descriptor_name)
                      AS descriptors,
                  min(m.source_file) AS source_file
             FROM episteme.article_mesh m
            WHERE m.pmid = k.pmid AND lower(m.descriptor_name) = ANY (v_names)) d
     ORDER BY k.year DESC NULLS LAST, 'pubmed:' || k.pmid;
END
$$;

-- ---------------------------------------------------------------------------
-- graph_neighbours (0016). Same signature, kinds, rows, order, scores and source_file as
-- 0015; two bodies change:
--   * id lookup (an id that is not a PMID): the smallest articles.pmid (COLLATE "C") of
--     the id, as one min(); no sort of the rows of the id.
--   * shared_mesh: the descriptor count and the hit step read only descriptor_ui and pmid
--     of article_mesh (an index-only scan of the (descriptor_ui, pmid) index). source_file
--     is read only for the returned rows (the smallest of the shared descriptors), and
--     the year only from the 'pubmed' articles row of the PMID (a europepmc_manuscript or
--     pmc row of the same PMID no longer gives the year).
CREATE OR REPLACE FUNCTION episteme_api_internal.graph_neighbours(
    p_article_id text, p_kind text, p_limit int DEFAULT NULL)
RETURNS SETOF episteme_api_internal.article_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_kind    text := lower(btrim(coalesce(p_kind, '')));
    v_id      text := btrim(coalesce(p_article_id, ''));
    v_cap     int;
    v_pmid    text;
    v_release text;
    v_licence text;
    v_schema  text;
    v_sf      text;
    v_has     boolean;
    v_uis     text[];
    v_dfs     bigint[];
    v_seed    int;
    v_skipped int;
    v_row     episteme_api_internal.article_row;
BEGIN
    IF v_kind NOT IN ('cites', 'cited_by', 'shared_mesh', 'trial', 'label') THEN
        v_row.kind := 'note';
        v_row.note := format('unknown kind %s; allowed kinds: cites, cited_by, shared_mesh, '
                             'trial, label', coalesce(p_kind, '(none)'));
        RETURN NEXT v_row;
        RETURN;
    END IF;

    IF v_id ~* '^((pubmed|pmid):)?[0-9]+$' THEN
        v_pmid := regexp_replace(v_id, '^(pubmed|pmid):', '', 'i');
    ELSIF v_id <> '' AND to_regclass('episteme.articles') IS NOT NULL THEN
        SELECT min(a.pmid COLLATE "C") INTO v_pmid
          FROM episteme.articles a
         WHERE a.id = v_id AND a.pmid <> '';
    END IF;
    IF v_pmid IS NULL THEN
        v_row.kind := 'note';
        v_row.note := format('article %s has no PMID; kind %s needs one', v_id, v_kind);
        RETURN NEXT v_row;
        RETURN;
    END IF;
    IF coalesce(p_limit, 1) <= 0 THEN
        RETURN;
    END IF;

    IF v_kind IN ('cites', 'cited_by', 'shared_mesh')
       AND to_regclass('episteme.reg_release') IS NOT NULL THEN
        SELECT rr.release_id INTO v_release
          FROM episteme.reg_release rr
         WHERE rr.dataset = 'pubmed' AND rr.is_latest;
    END IF;
    v_licence := episteme_api_internal.licence_for('pubmed');

    IF v_kind IN ('cites', 'cited_by') THEN
        IF to_regclass('episteme.article_cites') IS NOT NULL THEN
            SELECT EXISTS (SELECT 1 FROM episteme.article_cites) INTO v_has;
        END IF;
        IF NOT coalesce(v_has, false) OR to_regclass('episteme.articles') IS NULL THEN
            RETURN NEXT episteme_api_internal.missing_article('article_cites', 'pubmed');
            RETURN;
        END IF;
        RETURN QUERY
        WITH e AS (
            SELECT c.dst_pmid AS pmid, min(c.source_file) AS sf
              FROM episteme.article_cites c
             WHERE v_kind = 'cites' AND c.src_pmid = v_pmid
               AND c.dst_pmid IS NOT NULL AND c.dst_pmid <> v_pmid
             GROUP BY c.dst_pmid
            UNION ALL
            SELECT c.src_pmid, min(c.source_file)
              FROM episteme.article_cites c
             WHERE v_kind = 'cited_by' AND c.dst_pmid = v_pmid
               AND c.src_pmid IS NOT NULL AND c.src_pmid <> v_pmid
             GROUP BY c.src_pmid
        ), top AS (
            SELECT e.pmid, e.sf FROM e
             ORDER BY ('pubmed:' || e.pmid) COLLATE "C"
             LIMIT p_limit
        )
        SELECT 'article'::text, 'pubmed:' || t.pmid, 'pubmed'::text, NULL::text, y.year,
               1.0::real, v_kind, v_release, v_licence, NULL::text, t.sf
          FROM top t
          LEFT JOIN LATERAL (
               SELECT NULLIF(max(a.year), 0) AS year
                 FROM episteme.articles a WHERE a.pmid = t.pmid) y ON true
         ORDER BY ('pubmed:' || t.pmid) COLLATE "C";
        RETURN;
    END IF;

    IF v_kind = 'trial' THEN
        IF to_regclass('episteme.reg_release') IS NOT NULL THEN
            SELECT rr.release_id INTO v_release
              FROM episteme.reg_release rr
             WHERE rr.dataset = 'aact' AND rr.is_latest;
        END IF;
        IF v_release IS NULL OR v_release !~ '^[0-9]{8}$' THEN
            RETURN NEXT episteme_api_internal.missing_article('aact', 'aact');
            RETURN;
        END IF;
        v_schema := 'aact_r' || v_release;
        IF to_regclass(format('%I.%I', v_schema, 'study_references')) IS NULL
           OR to_regclass(format('%I.%I', v_schema, 'studies')) IS NULL THEN
            RETURN NEXT episteme_api_internal.missing_article('aact', 'aact');
            RETURN;
        END IF;
        IF to_regclass(format('%I.%I', v_schema, '_load_state')) IS NOT NULL THEN
            EXECUTE format('SELECT st.source_file FROM %I._load_state st '
                           'WHERE st.table_name = %L', v_schema, '_release')
               INTO v_sf;
        END IF;
        RETURN QUERY EXECUTE format(
            'SELECT ''article''::text, ''nct:'' || r.nct_id, ''aact''::text, '
            '       nullif(st.brief_title, '''')::text, NULL::int, 1.0::real, '
            '       ''trial''::text, $2, $3, NULL::text, $4 '
            '  FROM (SELECT DISTINCT sr.nct_id FROM %I.study_references sr '
            '         WHERE sr.pmid = $1 AND sr.nct_id IS NOT NULL) r '
            '  LEFT JOIN %I.studies st ON st.nct_id = r.nct_id '
            ' ORDER BY r.nct_id COLLATE "C" '
            ' LIMIT $5',
            v_schema, v_schema)
            USING v_pmid, v_release, episteme_api_internal.licence_for('aact'), v_sf,
                  p_limit;
        RETURN;
    END IF;

    IF v_kind = 'label' THEN
        IF to_regclass('episteme.article_chemicals') IS NULL THEN
            RETURN NEXT episteme_api_internal.missing_article('article_chemicals', 'pubmed');
            RETURN;
        END IF;
        IF to_regclass('episteme.substance') IS NULL
           OR to_regclass('episteme.substance_xref') IS NULL
           OR to_regclass('episteme.label_substance') IS NULL
           OR to_regclass('episteme.articles') IS NULL THEN
            RETURN NEXT episteme_api_internal.missing_article('dailymed', 'dailymed');
            RETURN;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM episteme.article_chemicals) THEN
            RETURN NEXT episteme_api_internal.missing_article('article_chemicals', 'pubmed');
            RETURN;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM episteme.label_substance) THEN
            RETURN NEXT episteme_api_internal.missing_article('dailymed', 'dailymed');
            RETURN;
        END IF;
        RETURN QUERY
        WITH names AS (
            SELECT DISTINCT btrim(regexp_replace(
                       episteme_api_internal.norm_name(c.name), '\s+', ' ', 'g')) AS k
              FROM episteme.article_chemicals c
             WHERE c.pmid = v_pmid AND c.name IS NOT NULL
        ), k AS (
            SELECT DISTINCT x.substance_key
              FROM names n
              JOIN episteme.substance_xref x
                ON x.id_type = 'name' AND x.id_value = n.k
             WHERE n.k <> ''
        ), u AS (
            SELECT DISTINCT f.unii
              FROM k
             CROSS JOIN LATERAL episteme_api_internal.substance_family(k.substance_key) f
             WHERE coalesce(f.unii, '') <> ''
        ), l AS (
            SELECT DISTINCT ls.article_id
              FROM episteme.label_substance ls
              JOIN u ON u.unii = ls.unii
        ), top AS (
            SELECT l.article_id,
                   coalesce(episteme_api_internal.label_source(l.article_id), 'dailymed')
                       AS src
              FROM l
             ORDER BY l.article_id COLLATE "C"
             LIMIT p_limit
        )
        SELECT 'article'::text, t.article_id, t.src, NULL::text,
               nullif(coalesce(ad.year, ae.year), 0), 1.0::real, 'label'::text,
               CASE WHEN t.src = 'ema_epar'
                    THEN coalesce(ad.book_meta, ae.book_meta) ->> 'last_updated_date'
                    ELSE coalesce(ad.book_meta, ae.book_meta) ->> 'version' END,
               episteme_api_internal.licence_for(t.src), NULL::text,
               coalesce(ad.source_file, ae.source_file)
          FROM top t
          LEFT JOIN episteme.articles ad
                 ON ad.source = 'dailymed' AND ad.id = t.article_id
          LEFT JOIN episteme.articles ae
                 ON ae.source = 'ema_epar' AND ae.id = t.article_id
         ORDER BY t.article_id COLLATE "C";
        RETURN;
    END IF;

    -- shared_mesh
    v_cap := greatest(episteme_api_internal.limit_for('graph_scan'), 1);
    IF to_regclass('episteme.article_mesh') IS NOT NULL THEN
        SELECT EXISTS (SELECT 1 FROM episteme.article_mesh) INTO v_has;
    END IF;
    IF NOT coalesce(v_has, false) OR to_regclass('episteme.articles') IS NULL THEN
        RETURN NEXT episteme_api_internal.missing_article('article_mesh', 'pubmed');
        RETURN;
    END IF;
    SELECT array_agg(df.ui ORDER BY df.ui) FILTER (WHERE df.n_rows <= v_cap),
           array_agg(df.n_pmids ORDER BY df.ui) FILTER (WHERE df.n_rows <= v_cap),
           count(*)::int,
           (count(*) FILTER (WHERE df.n_rows > v_cap))::int
      INTO v_uis, v_dfs, v_seed, v_skipped
      FROM (SELECT s.ui, z.n_rows, z.n_pmids
              FROM (SELECT DISTINCT m.descriptor_ui AS ui
                      FROM episteme.article_mesh m
                     WHERE m.pmid = v_pmid AND m.major_topic
                       AND m.descriptor_ui IS NOT NULL) s
             CROSS JOIN LATERAL (
                   SELECT count(*) AS n_rows, count(DISTINCT b.pmid) AS n_pmids
                     FROM (SELECT m.pmid FROM episteme.article_mesh m
                            WHERE m.descriptor_ui = s.ui
                            LIMIT v_cap + 1) b) z) df;
    IF v_seed = 0 THEN
        v_row.kind := 'note';
        v_row.note := format('article %s has no major-topic MeSH descriptor', v_id);
        RETURN NEXT v_row;
        RETURN;
    END IF;
    IF v_skipped > 0 THEN
        v_row.kind := 'note';
        v_row.note := format(
            '%s of %s major-topic descriptors not used: each is on more than %s '
            'article_mesh rows', v_skipped, v_seed, v_cap);
        RETURN NEXT v_row;
        v_row := NULL;
    END IF;
    IF v_uis IS NULL THEN
        RETURN;
    END IF;
    RETURN QUERY
    WITH d AS (
        SELECT * FROM unnest(v_uis, v_dfs) AS d(ui, n)
    ), hit AS (
        SELECT m.pmid, d.ui, d.n
          FROM d
          JOIN episteme.article_mesh m ON m.descriptor_ui = d.ui
         WHERE m.pmid <> v_pmid
         GROUP BY m.pmid, d.ui, d.n
    ), sc AS (
        SELECT h.pmid, sum(1.0::double precision / h.n) AS score
          FROM hit h
         GROUP BY h.pmid
    ), top AS (
        SELECT sc.* FROM sc
         ORDER BY sc.score DESC, ('pubmed:' || sc.pmid) COLLATE "C"
         LIMIT p_limit
    )
    SELECT 'article'::text, 'pubmed:' || t.pmid, 'pubmed'::text, NULL::text, y.year,
           t.score::real, 'shared_mesh'::text, v_release, v_licence, NULL::text, s.sf
      FROM top t
      LEFT JOIN LATERAL (
           SELECT min(m.source_file) AS sf
             FROM episteme.article_mesh m
            WHERE m.pmid = t.pmid AND m.descriptor_ui = ANY (v_uis)) s ON true
      LEFT JOIN LATERAL (
           SELECT NULLIF(max(a.year), 0) AS year
             FROM episteme.articles a
            WHERE a.source = 'pubmed' AND a.pmid = t.pmid) y ON true
     ORDER BY t.score DESC, ('pubmed:' || t.pmid) COLLATE "C";
END
$$;


-- search_text: articles whose fts_doc matches websearch_to_tsquery('english',
-- left(p_query, 1000)). Raw to_tsquery input is never used.
--   * An empty query, one of stop words only, or one that cannot be parsed: one note
--     row. A query with only negated words ('-word') cannot use the index: one note.
--   * Sources searched: the covered sources (c_covered), or those of p_sources that
--     are covered. p_sources = '{}': one note 'no sources requested'. Requested
--     sources that are not covered are named in one note. A
--     covered source whose partition has no VALID '<partition>_fts_idx' is not
--     searched (that would read the whole partition): one note names them, plus a
--     'missing' row per source.
--   * Rows: kind 'article', score ts_rank_cd, relation 'search', title and year from
--     article_body, source_file from articles (id, source), release_id the is_latest
--     reg_release of the source's dataset, licence licence_for(source). Ordered by
--     score descending, then article_id (COLLATE "C"); p_limit (NULL = no limit).
--   * Bounded work: per source, only the first limit_for('search_scan') index matches
--     (no ORDER BY: whichever the index returns first) are ranked. When a source
--     gives that many, a note says 'ranked within the first <N> matches of <source>'.
-- The match uses the index expression exactly (fts_index.fts_expression, a test checks
-- the text), so the planner uses the index of each partition.
-- Rank input (0016, spec decision 6): for the label sources (dailymed, ema_epar) the
-- rank reads only the first limit_for('search_rank_chars') characters of body_text and
-- of text_override, so a long label is not detoasted whole for each candidate. A label
-- whose query word is only after that point still matches, with score 0. Every other
-- source ranks on the full fts_doc, as in 0015.
CREATE OR REPLACE FUNCTION episteme_api_internal.search_text(
    p_query text, p_sources text[] DEFAULT NULL, p_limit int DEFAULT NULL)
RETURNS SETOF episteme_api_internal.article_row
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
-- Never a sequential scan of a partition: the match must go through its index.
SET enable_seqscan = off
AS $$
DECLARE
    c_covered constant text[] := ARRAY['pubmed', 'pmc', 'openalex', 'bookshelf', 'dailymed', 'ema_epar'];
    v_q       tsquery;
    v_want    text[];
    v_ready   text[] := ARRAY[]::text[];
    v_unbuilt text[] := ARRAY[]::text[];
    v_rels    text[] := ARRAY[]::text[];
    v_rel     text;
    v_src     text;
    v_other   text;
    v_scan    int := episteme_api_internal.limit_for('search_scan');
    v_rank_chars int := episteme_api_internal.limit_for('search_rank_chars');
    v_n       int;
    v_part    episteme_api_internal.article_row[];
    v_cand    episteme_api_internal.article_row[] := ARRAY[]::episteme_api_internal.article_row[];
    v_row     episteme_api_internal.article_row;
BEGIN
    BEGIN
        v_q := pg_catalog.websearch_to_tsquery(
            'english'::pg_catalog.regconfig, pg_catalog.left(coalesce(p_query, ''), 1000));
    EXCEPTION WHEN syntax_error OR program_limit_exceeded THEN
        v_q := NULL;
    END;
    IF v_q IS NULL OR pg_catalog.numnode(v_q) = 0 THEN
        v_row.kind := 'note';
        v_row.note := 'query has no searchable words (empty, stop words only, or not parsed)';
        RETURN NEXT v_row;
        RETURN;
    END IF;
    IF pg_catalog.querytree(v_q) = 'T' THEN
        v_row.kind := 'note';
        v_row.note := 'query needs at least one word that is not negated';
        RETURN NEXT v_row;
        RETURN;
    END IF;

    IF p_sources IS NOT NULL AND pg_catalog.cardinality(p_sources) = 0 THEN
        v_row.kind := 'note';
        v_row.note := 'no sources requested';
        RETURN NEXT v_row;
        RETURN;
    END IF;

    v_want := ARRAY(SELECT u.s FROM pg_catalog.unnest(c_covered) WITH ORDINALITY AS u(s, n)
                     WHERE p_sources IS NULL OR u.s = ANY (p_sources)
                     ORDER BY u.n);
    IF p_sources IS NOT NULL THEN
        SELECT pg_catalog.string_agg(d.s, ', ' ORDER BY d.s COLLATE "C") INTO v_other
          FROM (SELECT DISTINCT x.s FROM pg_catalog.unnest(p_sources) AS x(s)
                 WHERE x.s IS NOT NULL AND NOT (x.s = ANY (c_covered))) d;
        IF v_other IS NOT NULL THEN
            v_row.kind := 'note';
            v_row.note := 'not covered by full-text search: ' || pg_catalog.left(v_other, 500);
            RETURN NEXT v_row;
            v_row := NULL;
        END IF;
    END IF;

    FOREACH v_src IN ARRAY v_want LOOP
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_index i
                    WHERE i.indexrelid = pg_catalog.to_regclass(
                              'episteme.article_body_' || v_src || '_fts_idx')
                      AND i.indrelid = pg_catalog.to_regclass('episteme.article_body_' || v_src)
                      AND i.indisvalid) THEN
            v_rel := NULL;
            IF pg_catalog.to_regclass('episteme.reg_release') IS NOT NULL THEN
                SELECT rr.release_id INTO v_rel
                  FROM episteme.reg_release rr
                 WHERE rr.dataset = v_src AND rr.is_latest
                 LIMIT 1;
            END IF;
            v_ready := v_ready || v_src;
            v_rels := v_rels || v_rel;
        ELSE
            v_unbuilt := v_unbuilt || v_src;
        END IF;
    END LOOP;
    IF pg_catalog.cardinality(v_unbuilt) > 0 THEN
        v_row.kind := 'note';
        v_row.note := 'full-text index not built, not searched: '
                      || pg_catalog.array_to_string(v_unbuilt, ', ');
        RETURN NEXT v_row;
        v_row := NULL;
        FOREACH v_src IN ARRAY v_unbuilt LOOP
            RETURN NEXT episteme_api_internal.missing_article(v_src, v_src);
        END LOOP;
    END IF;
    IF pg_catalog.cardinality(v_ready) = 0 OR coalesce(p_limit, 1) <= 0 THEN
        RETURN;
    END IF;

    -- Candidates per source: at most v_scan index matches, then ranked.
    FOREACH v_src IN ARRAY v_ready LOOP
        SELECT pg_catalog.count(*)::int,
               pg_catalog.array_agg(ROW('article', b.article_id, b.source, b.title, b.year,
                   pg_catalog.ts_rank_cd(
                       CASE WHEN v_src IN ('dailymed', 'ema_epar')
                            THEN episteme_api_internal.fts_doc(b.source, b.title, b.abstract,
                                     pg_catalog.left(b.body_text, v_rank_chars),
                                     pg_catalog.left(b.text_override, v_rank_chars))
                            ELSE episteme_api_internal.fts_doc(b.source, b.title, b.abstract, b.body_text, b.text_override)
                       END,
                       v_q)::real,
                   'search', NULL, NULL, NULL, NULL)::episteme_api_internal.article_row)
          INTO v_n, v_part
          FROM (SELECT b.article_id, b.source, b.title, b.year, b.abstract, b.body_text,
                       b.text_override
                  FROM episteme.article_body b
                 WHERE b.source = v_src
                   AND episteme_api_internal.fts_doc(b.source, b.title, b.abstract, b.body_text, b.text_override) @@ v_q
                 LIMIT v_scan) b;
        IF v_n >= v_scan THEN
            v_row.kind := 'note';
            v_row.note := format('ranked within the first %s matches of %s', v_scan, v_src);
            RETURN NEXT v_row;
            v_row := NULL;
        END IF;
        IF v_n > 0 THEN
            v_cand := v_cand || v_part;
        END IF;
    END LOOP;

    RETURN QUERY
    SELECT 'article'::text, h.article_id, h.source, h.title, h.year, h.score,
           'search'::text, v_rels[pg_catalog.array_position(v_ready, h.source)],
           episteme_api_internal.licence_for(h.source), NULL::text, a.source_file
      FROM (SELECT c.article_id, c.source, c.title, c.year, c.score
              FROM pg_catalog.unnest(v_cand) AS c
             ORDER BY c.score DESC, c.article_id COLLATE "C"
             LIMIT p_limit) h
      LEFT JOIN LATERAL (
           SELECT ar.source_file FROM episteme.articles ar
            WHERE ar.id = h.article_id AND ar.source = h.source
            ORDER BY ar.source_file COLLATE "C"
            LIMIT 1) a ON true
     ORDER BY h.score DESC, h.article_id COLLATE "C";
END
$$;

COMMIT;
