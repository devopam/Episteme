-- Migration 0006 -- PubMed graph relations, MeSH facets, graph lineage. Idempotent.
-- Apply as episteme_sys_admin (owner). One transaction (see 0005).
-- schema.sql's ALTER DEFAULT PRIVILEGES grants episteme_app DML on new tables,
-- which the pipeline needs. MCPg run_ddl refuses GRANT/REVOKE, so when applying
-- through MCPg the operator runs this file's REVOKE line once as episteme_sys_admin.

BEGIN;

ALTER TABLE episteme.article_mesh  ADD COLUMN IF NOT EXISTS qualifier_uis text[];
ALTER TABLE episteme.article_cites ADD COLUMN IF NOT EXISTS dst_doi  text;
ALTER TABLE episteme.article_cites ADD COLUMN IF NOT EXISTS ref_text text;
ALTER TABLE episteme.article_cites ALTER COLUMN dst_pmid DROP NOT NULL;

CREATE TABLE IF NOT EXISTS episteme.article_comments (
    pmid text NOT NULL, ref_type text, ref_pmid text, ref_text text, source_file text
) PARTITION BY HASH (pmid);
CREATE TABLE IF NOT EXISTS episteme.article_chemicals (
    pmid text NOT NULL, substance_ui text, name text, registry_number text, source_file text
) PARTITION BY HASH (pmid);
CREATE TABLE IF NOT EXISTS episteme.article_keywords (
    pmid text NOT NULL, keyword text, owner text, major_topic boolean, source_file text
) PARTITION BY HASH (pmid);
CREATE TABLE IF NOT EXISTS episteme.article_authors (
    pmid text NOT NULL, position int NOT NULL, last_name text, fore_name text, initials text,
    collective_name text, orcid text, affiliations text[], source_file text
) PARTITION BY HASH (pmid);
CREATE TABLE IF NOT EXISTS episteme.article_facets (
    pmid text NOT NULL, facet text NOT NULL, descriptor_ui text NOT NULL,
    rule_version int NOT NULL, source_file text
) PARTITION BY HASH (pmid);

DO $$
DECLARE t text; i int;
BEGIN
  FOREACH t IN ARRAY ARRAY['article_comments','article_chemicals','article_keywords',
                           'article_authors','article_facets'] LOOP
    FOR i IN 0..7 LOOP
      EXECUTE format(
        'CREATE TABLE IF NOT EXISTS episteme.%I PARTITION OF episteme.%I '
        'FOR VALUES WITH (MODULUS 8, REMAINDER %s)', t || '_h' || i, t, i);
    END LOOP;
  END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS episteme.mesh_tree (
    descriptor_ui text NOT NULL, tree_number text NOT NULL, source_file text
);
CREATE TABLE IF NOT EXISTS episteme.mesh_facet_rule (
    facet text NOT NULL, kind text NOT NULL CHECK (kind IN ('tree_prefix', 'descriptor')),
    value text NOT NULL, rule_version int NOT NULL,
    PRIMARY KEY (facet, kind, value, rule_version)
);
CREATE TABLE IF NOT EXISTS episteme._graph_lineage (
    source text NOT NULL, source_file text NOT NULL, graph_sha256 text NOT NULL,
    rows jsonb NOT NULL, run_id text, graphed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, source_file)
);

-- Secondary indexes (bulk group "pubmed_graph" drops/rebuilds these).
CREATE INDEX IF NOT EXISTS article_mesh_pmid_idx            ON episteme.article_mesh (pmid);
CREATE INDEX IF NOT EXISTS article_mesh_descriptor_ui_idx   ON episteme.article_mesh (descriptor_ui);
CREATE INDEX IF NOT EXISTS article_cites_src_pmid_idx       ON episteme.article_cites (src_pmid);
CREATE INDEX IF NOT EXISTS article_cites_dst_pmid_idx       ON episteme.article_cites (dst_pmid);
CREATE INDEX IF NOT EXISTS article_comments_pmid_idx        ON episteme.article_comments (pmid);
CREATE INDEX IF NOT EXISTS article_comments_ref_pmid_idx    ON episteme.article_comments (ref_pmid);
CREATE INDEX IF NOT EXISTS article_chemicals_pmid_idx       ON episteme.article_chemicals (pmid);
CREATE INDEX IF NOT EXISTS article_chemicals_substance_ui_idx ON episteme.article_chemicals (substance_ui);
CREATE INDEX IF NOT EXISTS article_keywords_pmid_idx        ON episteme.article_keywords (pmid);
CREATE INDEX IF NOT EXISTS article_keywords_keyword_idx     ON episteme.article_keywords (lower(keyword));
CREATE INDEX IF NOT EXISTS article_authors_pmid_idx         ON episteme.article_authors (pmid);
CREATE INDEX IF NOT EXISTS article_authors_orcid_idx        ON episteme.article_authors (orcid) WHERE orcid IS NOT NULL;
CREATE INDEX IF NOT EXISTS article_facets_pmid_idx          ON episteme.article_facets (pmid);
CREATE INDEX IF NOT EXISTS article_facets_facet_descriptor_idx ON episteme.article_facets (facet, descriptor_ui);
-- Permanent: per-file deletes; never dropped by bulk mode.
CREATE INDEX IF NOT EXISTS article_mesh_source_file_idx      ON episteme.article_mesh (source_file);
CREATE INDEX IF NOT EXISTS article_cites_source_file_idx     ON episteme.article_cites (source_file);
CREATE INDEX IF NOT EXISTS article_comments_source_file_idx  ON episteme.article_comments (source_file);
CREATE INDEX IF NOT EXISTS article_chemicals_source_file_idx ON episteme.article_chemicals (source_file);
CREATE INDEX IF NOT EXISTS article_keywords_source_file_idx  ON episteme.article_keywords (source_file);
CREATE INDEX IF NOT EXISTS article_authors_source_file_idx   ON episteme.article_authors (source_file);
CREATE INDEX IF NOT EXISTS article_facets_source_file_idx    ON episteme.article_facets (source_file);
CREATE INDEX IF NOT EXISTS mesh_tree_tree_number_idx  ON episteme.mesh_tree (tree_number text_pattern_ops);
CREATE INDEX IF NOT EXISTS mesh_tree_descriptor_ui_idx ON episteme.mesh_tree (descriptor_ui);

-- Facet rules, version 1 (UIs checked against MeSH desc2026). Male/Female have
-- no tree numbers (check tags), so they are 'descriptor' rules.
INSERT INTO episteme.mesh_facet_rule (facet, kind, value, rule_version) VALUES
  ('condition', 'tree_prefix', 'C', 1),
  ('condition', 'tree_prefix', 'F03', 1),
  ('test', 'tree_prefix', 'E01', 1),
  ('test', 'tree_prefix', 'E05', 1),
  ('drug', 'tree_prefix', 'D', 1),
  ('demographic', 'descriptor', 'D006801', 1),
  ('demographic', 'descriptor', 'D000818', 1),
  ('demographic', 'descriptor', 'D008297', 1),
  ('demographic', 'descriptor', 'D005260', 1),
  ('demographic', 'tree_prefix', 'M01.060', 1),
  ('demographic', 'tree_prefix', 'M01.390', 1),
  ('demographic', 'tree_prefix', 'M01.975', 1),
  ('special_population', 'descriptor', 'D011247', 1),
  ('special_population', 'descriptor', 'D037841', 1),
  ('special_population', 'descriptor', 'D007231', 1),
  ('special_population', 'descriptor', 'D007234', 1),
  ('special_population', 'descriptor', 'D000369', 1),
  ('special_population', 'tree_prefix', 'M01.066', 1),
  ('special_population', 'tree_prefix', 'M01.150', 1),
  ('special_population', 'tree_prefix', 'M01.169', 1),
  ('special_population', 'tree_prefix', 'M01.189', 1),
  ('special_population', 'tree_prefix', 'M01.270', 1),
  ('special_population', 'tree_prefix', 'M01.325', 1),
  ('special_population', 'tree_prefix', 'M01.385', 1),
  ('special_population', 'tree_prefix', 'M01.686', 1),
  ('special_population', 'tree_prefix', 'M01.729', 1),
  ('special_population', 'tree_prefix', 'M01.755', 1),
  ('special_population', 'tree_prefix', 'M01.873', 1),
  ('special_population', 'tree_prefix', 'M01.920', 1),
  ('special_population', 'tree_prefix', 'M01.925', 1),
  ('special_population', 'tree_prefix', 'M01.930', 1)
ON CONFLICT DO NOTHING;

-- The pipeline writes as episteme_app: data tables and _graph_lineage keep the
-- default grants (as articles and _lineage do). Only facet rules are read-only.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON episteme.mesh_facet_rule FROM episteme_app;

COMMIT;
