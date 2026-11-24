-- Migration 0017 -- SQL lookup API: no timeouts (time budget, topic expansion, search work_mem).
-- Idempotent. Apply after 0016, as episteme_sys_admin (owner). One transaction.
-- Design: docs/superpowers/specs/2026-11-24-api-no-timeouts-design.md (follow-up of
-- 2026-11-24-api-layer-perf-design.md).
--
-- This file creates episteme.mesh_qualifier (the MeSH qualifier names, filled by the
-- mesh graph phase) and seeds the new result_limit families. Apply it after 0016 as
-- episteme_sys_admin; run `python -m episteme.reference.api_settings sync` after it (it
-- applies the EPISTEME_API_LIMIT_<FAMILY> overrides and the owner grants); then run
-- `run_pipeline.sh mesh graph` to fill mesh_qualifier.

BEGIN;

-- Seed values equal config.API_RESULT_LIMITS (a test compares them).
INSERT INTO episteme_api_internal.result_limit (family, default_limit, max_limit)
VALUES ('literature_topic_scan', 3000000, 3000000),
       ('literature_topic_chunk', 500, 500),
       ('time_budget_ms', 20000, 20000),
       ('search_work_mem_mb', 256, 256)
ON CONFLICT (family) DO NOTHING;

-- search_rank_chars: 10000 (0016) becomes 2000, only where the row still holds the 0016
-- seed (an operator value stays).
UPDATE episteme_api_internal.result_limit
   SET default_limit = 2000, max_limit = 2000
 WHERE family = 'search_rank_chars' AND default_limit = 10000 AND max_limit = 10000;

-- MeSH qualifier (subheading) names, one row per qualifier and descriptor release file.
CREATE TABLE IF NOT EXISTS episteme.mesh_qualifier (
    qualifier_ui text NOT NULL, qualifier_name text NOT NULL, source_file text
);
CREATE INDEX IF NOT EXISTS mesh_qualifier_lower_name_idx
    ON episteme.mesh_qualifier (lower(qualifier_name));

-- ---------------------------------------------------------------------------
-- Qualifier side of a literature_by_mesh topic (0017). Candidates: the rows
-- (p_pmid[i], p_year[i]) of one year window (a NULL year is an unknown year). The rows
-- whose PMID is in p_ok (the descriptor side kept them) are kept without a check. Each
-- other row is kept when one of its article_mesh rows holds a qualifier q with
-- lower(q) = p_topic (p_topic is trimmed and lower case). These rows are checked in
-- chunks of p_chunk PMIDs (at least 1), year descending (unknown last), then PMID; the
-- deadline is checked before each chunk. When it is spent, the function returns the
-- rows kept so far, then one row (NULL, the newest year not checked, true), and stops.
-- Every kept row has stopped = false. The check reads article_mesh heap rows per PMID
-- (qualifiers is in no index), so the chunks keep its cost inside the time budget.
CREATE OR REPLACE FUNCTION episteme_api_internal.literature_topic_keep(
    p_pmid text[], p_year int[], p_ok text[], p_topic text, p_chunk int,
    p_deadline timestamptz)
RETURNS TABLE (pmid text, year int, stopped boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_chunk  int := greatest(coalesce(p_chunk, 1), 1);
    v_ok     text[] := coalesce(p_ok, ARRAY[]::text[]);
    v_cand_p text[];
    v_cand_y int[];
    v_n      int;
    v_i      int := 1;
BEGIN
    -- The rows the descriptor side kept.
    RETURN QUERY
    SELECT c.cp, c.cy, false
      FROM unnest(p_pmid, p_year) AS c(cp, cy)
     WHERE c.cp IN (SELECT o.op FROM unnest(v_ok) AS o(op));

    -- The other rows, newest first.
    SELECT array_agg(c.cp ORDER BY c.cy DESC NULLS LAST, c.cp),
           array_agg(c.cy ORDER BY c.cy DESC NULLS LAST, c.cp)
      INTO v_cand_p, v_cand_y
      FROM unnest(p_pmid, p_year) AS c(cp, cy)
     WHERE c.cp IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM unnest(v_ok) AS o(op) WHERE o.op = c.cp);
    v_n := coalesce(cardinality(v_cand_p), 0);

    WHILE v_i <= v_n LOOP
        IF clock_timestamp() >= p_deadline THEN
            RETURN QUERY SELECT NULL::text, v_cand_y[v_i], true;
            RETURN;
        END IF;
        RETURN QUERY
        SELECT c.cp, c.cy, false
          FROM unnest(v_cand_p[v_i:v_i + v_chunk - 1],
                      v_cand_y[v_i:v_i + v_chunk - 1]) AS c(cp, cy)
         WHERE EXISTS (
                   SELECT 1 FROM episteme.article_mesh t
                    WHERE t.pmid = c.cp
                      AND EXISTS (SELECT 1 FROM unnest(t.qualifiers) AS q(qn)
                                   WHERE lower(q.qn) = p_topic));
        v_i := v_i + v_chunk;
    END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- literature_by_mesh (family 'literature'). SQL form of the former
-- literature.literature, with topic expansion.
--
-- Substance side (as before): a PubMed record matches when one of its MeSH
-- descriptors has a lower()-name equal to the norm_name of a preferred name of the
-- substance's moiety family, or to a 'name' xref of a family member.
-- Topic side (0017): p_topic (trimmed, lower case) is a descriptor name
-- (lower(descriptor_name)), a MeSH qualifier name (episteme.mesh_qualifier), both
-- (for example 'mortality'), or neither.
--   - Descriptor: the descriptor is expanded to itself and every descriptor below it in
--     mesh_tree (tree numbers that start with one of its tree numbers plus '.'). Without
--     mesh_tree the set is the descriptor alone. The descriptor_ui of a name is read
--     from one article_mesh row (LIMIT 1): a name belongs to one descriptor.
--   - Qualifier: a record with one qualifier named like the topic matches (the v1
--     behaviour, for example 'adverse effects').
--   - A record matches a topic that is both when it matches either side.
--   - When mesh_qualifier is missing or empty, every topic counts as a possible
--     qualifier name (the 0016 behaviour).
--   - A topic that names no descriptor gives a note row and the qualifier match only.
--   - Neither (mesh_qualifier filled): one note row 'topic <t> is not a MeSH descriptor
--     or qualifier name', no article rows and no hit scan.
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
-- Method (0016, topic side and time budget 0017): the result is the same as one sort
-- of every hit, but the function reads years for few hits.
--   1. Hits: the PMIDs of the matched descriptor rows (pmid only).
--   2. Descriptor side of a topic: one fenced read of the PMIDs of the descriptor set
--      (an index-only scan of (descriptor_ui, pmid), at most
--      limit_for('literature_topic_scan') + 1 rows), hash-joined with the hits. At most
--      literature_topic_scan rows: the set is exact. When the topic is no qualifier
--      name, the hits become this intersection, and no window runs a topic check.
--      More rows (a very common topic): the set is not used; each window probes its own
--      rows with descriptor_ui = ANY (set) AND pmid = ANY (window PMIDs).
--   3. Year windows (year_windows; the first window is limit_for('literature_window')
--      years wide, each next window twice as wide), newest first, then one step for
--      the unknown years (no 'pubmed' row, or year 0; only without a year bound). Each
--      window reads the 'pubmed' rows of the hits not seen yet (v_rest) with a year in
--      the window, and keeps max(year) per PMID. A PMID is seen in the newest window
--      that holds one of its 'pubmed' rows, and only there: the window removes it from
--      v_rest (a hash EXCEPT, so a large hit set costs no array scan per row). The
--      topic check of the window's rows: the descriptor side (step 2), then, when the
--      topic can be a qualifier name, literature_topic_keep for the other rows (the
--      article_mesh heap rows per PMID, in chunks of limit_for('literature_topic_chunk')
--      PMIDs, newest first).
--      After a window, the function stops when it holds at least p_limit rows: a later
--      window has only older years, and every tie of the window is present.
--   4. Time budget: the deadline is statement_timestamp() plus
--      limit_for('time_budget_ms'). It is checked at the top of each window after the
--      first (the unknown-year step included) and before each qualifier chunk. When it
--      is spent, the function stops, keeps the rows it holds and returns one note:
--      'partial: time budget of <n> ms spent; years from <y> down not searched' (<y> =
--      the newest year not fully searched) or '...; records with an unknown year not
--      searched'.
--   5. Sort, cut at p_limit, then descriptors and source_file for the kept rows only.
-- Note rows come before the article rows.
-- Indexes: lower(descriptor_name) on article_mesh serves the name match; with the
-- covering form (lower(descriptor_name)) INCLUDE (descriptor_name, pmid) the hit step
-- is an index-only scan. (descriptor_ui, pmid) serves step 2 and the window probe as
-- index-only scans. Step 5 is a pmid lookup bounded by p_limit rows: it reads
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
    v_keys     bigint[];
    v_names    text[];
    v_topic    text := lower(btrim(coalesce(p_topic, '')));
    v_ui       text;
    v_uis      text[] := ARRAY[]::text[];
    v_release  text;
    v_licence  text := episteme_api_internal.licence_for('pubmed');
    v_row      episteme_api_internal.article_row;
    v_has      boolean;
    v_pmids    text[];
    v_rest     text[];
    v_pmid     text[] := ARRAY[]::text[];
    v_year     int[] := ARRAY[]::int[];
    v_new_s    text[];
    v_win_y    int[];
    v_new_p    text[];
    v_new_y    int[];
    v_above    text[];
    v_budget   int;
    v_deadline timestamptz;
    v_qual     boolean := false;
    v_scan     int;
    v_chunk    int;
    v_read     bigint;
    v_set      text[];
    v_exact    boolean := false;
    v_check    boolean;
    v_ok       text[];
    v_stopped  boolean;
    v_stop_y   int;
    v_first    boolean := true;
    v_partial  text;
    w          record;
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

    v_budget := episteme_api_internal.limit_for('time_budget_ms');
    v_deadline := statement_timestamp() + make_interval(secs => v_budget / 1000.0);

    IF v_topic <> '' THEN
        -- Qualifier name: from the list; without a list (missing or empty) every topic
        -- can be one (0016).
        IF to_regclass('episteme.mesh_qualifier') IS NULL THEN
            v_qual := true;
        ELSE
            SELECT NOT EXISTS (SELECT 1 FROM episteme.mesh_qualifier)
                   OR EXISTS (SELECT 1 FROM episteme.mesh_qualifier mq
                               WHERE lower(mq.qualifier_name) = v_topic)
              INTO v_qual;
        END IF;
        SELECT m.descriptor_ui INTO v_ui
          FROM episteme.article_mesh m
         WHERE lower(m.descriptor_name) = v_topic AND m.descriptor_ui IS NOT NULL
         LIMIT 1;
        IF v_ui IS NULL AND NOT v_qual THEN
            v_row.kind := 'note';
            v_row.note := format('topic %s is not a MeSH descriptor or qualifier name',
                                 btrim(p_topic));
            RETURN NEXT v_row;
            RETURN;
        ELSIF v_ui IS NULL THEN
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
        IF v_qual THEN
            v_chunk := episteme_api_internal.limit_for('literature_topic_chunk');
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

    -- 2. Descriptor side: one fenced read of the set (the LIMIT keeps the planner off
    -- the two-array form), hash-joined with the hits. v_read counts the rows read.
    IF v_ui IS NOT NULL THEN
        v_scan := episteme_api_internal.limit_for('literature_topic_scan');
        SELECT count(*),
               array_agg(DISTINCT d.pmid ORDER BY d.pmid) FILTER (WHERE h.pmid IS NOT NULL)
          INTO v_read, v_set
          FROM (SELECT m.pmid
                  FROM episteme.article_mesh m
                 WHERE m.descriptor_ui = ANY (v_uis)
                 LIMIT v_scan + 1) d
          LEFT JOIN (SELECT DISTINCT u.pmid FROM unnest(v_pmids) AS u(pmid)) h
            ON h.pmid = d.pmid;
        IF v_read <= v_scan THEN
            v_exact := true;
            v_set := coalesce(v_set, ARRAY[]::text[]);
            IF NOT v_qual THEN
                v_pmids := v_set;
            END IF;
        ELSE
            v_set := NULL;
        END IF;
    END IF;
    -- A window runs a topic check unless there is no topic or the hits are the exact
    -- descriptor set of a topic that is no qualifier name.
    v_check := v_topic <> '' AND NOT (v_exact AND NOT v_qual);

    -- 3. Year windows, newest first, then the unknown years (lo and hi NULL; only
    -- without a year bound). v_rest: the hits no window gave yet (with or without the
    -- topic), so no later window or the unknown step takes one again.
    v_rest := v_pmids;
    FOR w IN
        SELECT y.lo, y.hi, y.n
          FROM episteme_api_internal.year_windows(
                   p_from_year, p_to_year,
                   episteme_api_internal.limit_for('literature_window'),
                   extract(year FROM current_date)::int) AS y
        UNION ALL
        SELECT NULL::int, NULL::int, 2147483647
         WHERE p_from_year IS NULL AND p_to_year IS NULL
        ORDER BY 3
    LOOP
        EXIT WHEN cardinality(v_rest) = 0;
        -- 4. Time budget, at the top of each window after the first.
        IF NOT v_first AND clock_timestamp() >= v_deadline THEN
            IF w.lo IS NULL THEN
                v_partial := 'records with an unknown year not searched';
            ELSE
                v_partial := format('years from %s down not searched', w.hi);
            END IF;
            EXIT;
        END IF;
        v_first := false;
        IF w.lo IS NULL THEN
            v_new_s := ARRAY(SELECT u.pmid FROM unnest(v_rest) AS u(pmid) ORDER BY u.pmid);
            v_win_y := array_fill(NULL::int, ARRAY[cardinality(v_new_s)]);
            v_rest := ARRAY[]::text[];
        ELSE
            SELECT array_agg(g.pmid ORDER BY g.pmid), array_agg(g.year ORDER BY g.pmid)
              INTO v_new_s, v_win_y
              FROM (SELECT a.pmid, max(a.year) AS year
                      FROM episteme.articles a
                     WHERE a.source = 'pubmed'
                       AND a.year BETWEEN w.lo AND w.hi
                       AND a.year > 0
                       AND a.pmid = ANY (v_rest)
                     GROUP BY a.pmid) g;
            IF v_new_s IS NOT NULL THEN
                v_rest := ARRAY(SELECT unnest(v_rest) EXCEPT SELECT unnest(v_new_s));
            END IF;
        END IF;
        v_new_p := v_new_s;
        v_new_y := v_win_y;
        v_stopped := false;

        -- Topic check of the window's rows.
        IF v_check AND v_new_s IS NOT NULL THEN
            IF v_ui IS NULL THEN
                v_ok := ARRAY[]::text[];
            ELSIF v_exact THEN
                v_ok := ARRAY(SELECT u.pmid FROM unnest(v_new_s) AS u(pmid)
                               WHERE u.pmid IN (SELECT e.pmid FROM unnest(v_set) AS e(pmid)));
            ELSE
                v_ok := ARRAY(SELECT DISTINCT t.pmid
                                FROM episteme.article_mesh t
                               WHERE t.descriptor_ui = ANY (v_uis)
                                 AND t.pmid = ANY (v_new_s));
            END IF;
            IF NOT v_qual THEN
                SELECT array_agg(u.p ORDER BY u.p), array_agg(u.y ORDER BY u.p)
                  INTO v_new_p, v_new_y
                  FROM unnest(v_new_s, v_win_y) AS u(p, y)
                 WHERE u.p IN (SELECT o.pmid FROM unnest(v_ok) AS o(pmid));
            ELSE
                SELECT array_agg(k.pmid ORDER BY k.pmid) FILTER (WHERE NOT k.stopped),
                       array_agg(k.year ORDER BY k.pmid) FILTER (WHERE NOT k.stopped),
                       coalesce(bool_or(k.stopped), false),
                       max(k.year) FILTER (WHERE k.stopped)
                  INTO v_new_p, v_new_y, v_stopped, v_stop_y
                  FROM episteme_api_internal.literature_topic_keep(
                           v_new_s, v_win_y, v_ok, v_topic, v_chunk, v_deadline) AS k;
            END IF;
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
        -- The qualifier check ran out of time inside the window: stop here.
        IF v_stopped THEN
            IF w.lo IS NULL THEN
                v_partial := 'records with an unknown year not searched';
            ELSE
                v_partial := format('years from %s down not searched', v_stop_y);
            END IF;
            EXIT;
        END IF;
        EXIT WHEN p_limit IS NOT NULL AND cardinality(v_pmid) >= p_limit;
    END LOOP;

    IF v_partial IS NOT NULL THEN
        v_row := NULL;
        v_row.kind := 'note';
        v_row.note := format('partial: time budget of %s ms spent; %s', v_budget, v_partial);
        RETURN NEXT v_row;
    END IF;

    -- 5. Sort and cut, then descriptors and source_file for the kept rows only.
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
-- source ranks on the full fts_doc, as in 0015: fts_doc reads body_text and
-- text_override only for the label sources, so the raw columns are passed and not
-- detoasted. Contract change in 0017: scores of long labels move (the limit is 2000
-- characters, was 10000).
-- Exact bitmap (0017): work_mem is raised to limit_for('search_work_mem_mb') MB for the
-- candidate scans, so a bitmap heap scan for a common word stays exact (not lossy
-- per page); the old value is set back before every return after the change.
-- Time budget (0017): the deadline is statement_timestamp() + limit_for('time_budget_ms').
-- It is checked before each source of the candidate loop. When it is spent, the sources
-- not yet searched are named in one note 'partial: time budget of <n> ms spent; not
-- searched: <sources>' (returned before the article rows), and the rows found so far
-- are ranked and returned as usual.
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
    v_budget  int := episteme_api_internal.limit_for('time_budget_ms');
    v_deadline timestamptz :=
        pg_catalog.statement_timestamp() + pg_catalog.make_interval(secs => v_budget / 1000.0);
    v_old_wm  text;
    v_i       int := 0;
    v_skipped text[];
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

    -- work_mem for the candidate scans (transaction-local; set back before the final return).
    v_old_wm := pg_catalog.current_setting('work_mem');
    PERFORM pg_catalog.set_config('work_mem',
        episteme_api_internal.limit_for('search_work_mem_mb') || 'MB', true);

    -- Candidates per source: at most v_scan index matches, then ranked.
    FOREACH v_src IN ARRAY v_ready LOOP
        v_i := v_i + 1;
        IF pg_catalog.clock_timestamp() >= v_deadline THEN
            v_skipped := v_ready[v_i:pg_catalog.cardinality(v_ready)];
            EXIT;
        END IF;
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

    IF v_skipped IS NOT NULL THEN
        v_row := NULL;
        v_row.kind := 'note';
        v_row.note := pg_catalog.format('partial: time budget of %s ms spent; not searched: %s',
                                        v_budget, pg_catalog.array_to_string(v_skipped, ', '));
        RETURN NEXT v_row;
    END IF;

    PERFORM pg_catalog.set_config('work_mem', v_old_wm, true);
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
