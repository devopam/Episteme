-- SP2 migration 0002 -- container_id / book_meta + episteme.article_parts + bookshelf partitions.
-- Idempotent. Applied by migrate_database.sh after 0001, tracked in episteme._migrations.

ALTER TABLE episteme.articles      ADD COLUMN IF NOT EXISTS container_id text;
ALTER TABLE episteme.articles      ADD COLUMN IF NOT EXISTS book_meta    jsonb;

-- bookshelf LIST partition of episteme.articles, RANGE(year) like articles_pmc
CREATE TABLE IF NOT EXISTS episteme.articles_bookshelf
    PARTITION OF episteme.articles FOR VALUES IN ('bookshelf')
    PARTITION BY RANGE (year);
CREATE TABLE IF NOT EXISTS episteme.articles_bookshelf_y0
    PARTITION OF episteme.articles_bookshelf FOR VALUES FROM (0) TO (1);
CREATE TABLE IF NOT EXISTS episteme.articles_bookshelf_pre1990
    PARTITION OF episteme.articles_bookshelf FOR VALUES FROM (1) TO (1990);
CREATE TABLE IF NOT EXISTS episteme.articles_bookshelf_1990_2000
    PARTITION OF episteme.articles_bookshelf FOR VALUES FROM (1990) TO (2000);
CREATE TABLE IF NOT EXISTS episteme.articles_bookshelf_2000_2010
    PARTITION OF episteme.articles_bookshelf FOR VALUES FROM (2000) TO (2010);
CREATE TABLE IF NOT EXISTS episteme.articles_bookshelf_2010_2020
    PARTITION OF episteme.articles_bookshelf FOR VALUES FROM (2010) TO (2020);
CREATE TABLE IF NOT EXISTS episteme.articles_bookshelf_2020_2030
    PARTITION OF episteme.articles_bookshelf FOR VALUES FROM (2020) TO (2030);
CREATE TABLE IF NOT EXISTS episteme.articles_bookshelf_future
    PARTITION OF episteme.articles_bookshelf FOR VALUES FROM (2030) TO (10000);

CREATE TABLE IF NOT EXISTS episteme.article_body_bookshelf
    PARTITION OF episteme.article_body FOR VALUES IN ('bookshelf');

-- book -> part edges. HASH(container_id) x8 to match article_cites/article_mesh.
CREATE TABLE IF NOT EXISTS episteme.article_parts (
    container_id text NOT NULL,
    part_id      text NOT NULL,
    source_file  text NOT NULL
) PARTITION BY HASH (container_id);
DO $$
BEGIN
  FOR i IN 0..7 LOOP
    EXECUTE format(
      'CREATE TABLE IF NOT EXISTS episteme.article_parts_h%s '
      'PARTITION OF episteme.article_parts FOR VALUES WITH (MODULUS 8, REMAINDER %s)', i, i);
  END LOOP;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS article_parts_uq
    ON episteme.article_parts (container_id, part_id);
CREATE INDEX IF NOT EXISTS article_parts_src_idx
    ON episteme.article_parts (source_file);

GRANT SELECT, INSERT ON episteme.article_parts TO episteme_app;
