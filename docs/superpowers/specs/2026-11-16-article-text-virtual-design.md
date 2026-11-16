# Article text without duplication: `text` as a virtual column

Date: 2026-11-16. Owner decisions: 2026-10-28 ("storing the text twice is not an inherent good design"); 2026-11-16 (virtual generated column; the `text_override` shape with REPACK partition by partition, after the sampling below). Items marked "Claude's call" may be changed at review.

## 1. Why

`episteme.article_body.text` is today `normalize_whitespace(title + "\n\n" + abstract + "\n\n" + body_text)` for literature rows (`article_schema.build_text`, `finalize_row`). Every such row stores its title, abstract and body twice. About half of the stored text bytes of `openalex` (123 GB on disk), `pubmed` (71 GB), `pmc` (29 GB) and `dailymed` are this copy.

Sampling on 2026-11-16 (TABLESAMPLE, production) of "stored `text` equals the plain SQL join of the three parts":

| Source | equal |
|---|---|
| openalex, pubmed, dailymed, europepmc_preprint | 99.6 to 100 % |
| pmc | 87 % (whitespace normalisation after the join, assumed) |
| pubchem, chembl, apollo, clinvar, uniprot, reactome, europepmc_manuscript, bookshelf, ontologies | 0 to 33 %: the serializer writes its own `text`, often the only content |

So `text` cannot become a pure join. It becomes "the stored value where it differs, else the join".

## 2. Design

### 2.1 Schema (migration 0014)

- `ALTER TABLE episteme.article_body RENAME COLUMN text TO text_override;` (propagates to every partition).
- `ALTER TABLE episteme.article_body ADD COLUMN text text GENERATED ALWAYS AS (<expr>) VIRTUAL;`
- `<expr>` = `CASE WHEN text_override = '' THEN NULL ELSE coalesce(text_override, nullif(<join>, '')) END`, where `<join>` joins the non-blank parts of `title`, `abstract`, `body_text` with `E'\n\n'`. It must be IMMUTABLE: `concat_ws` is refused ("generation expression is not immutable", tested on 19beta3); use `||` and `CASE` only. Tested on 19beta3: a virtual generated column on a LIST-partitioned table, and DROP + ADD of it, work.
- **Blank part (one definition, SQL and Python):** a part is blank when it is NULL or `btrim(part, E' \t\r\n') = ''`. Python mirrors it exactly: `p is None or p.strip(" \t\r\n") == ""`. Nothing else counts as blank (no Unicode whitespace, no NBSP): a part outside this rule stays in the join, and the stored value differs, so the row keeps its override. This keeps the null-out safe: it only nulls an override when the SQL `<join>` equals it byte for byte.
- **NULL text sentinel:** `text_override = ''` (empty string) means "`text` is NULL". `coalesce` alone cannot tell "use the join" from "text was NULL while a part is non-blank". Ruling (2026-11-16): migration 0014 is metadata-only and does not set the sentinel (an UPDATE scanned about 370 GB under ACCESS EXCLUSIVE). Instead, `python -m episteme.data.db.text_override precheck --all` runs BEFORE 0014 and must report zero rows with `text IS NULL` and a non-blank part, and zero rows with `text = ''` (sampled 2026-11-16: none found). The loader writes `''` for a shard row whose `text` is NULL or `''` when the join is not NULL. A shard `text` of `''` reads back as NULL (`finalize_row` never emits `''`).
- `nullif(..., '')`: the corpus materializer selects `b.text IS NOT NULL`; a row with no text must stay NULL.
- **`schema.sql`** (fresh databases and the pg test bootstrap) defines the final shape directly: `text_override` stored, `text` virtual, and the `SET COMPRESSION` statements on `text_override` (a virtual column has no storage). 0014 stays idempotent for existing databases.
- **External readers:** none (owner, 2026-11-16). A data-only dump or `COPY` of `article_body` no longer contains `text`; that is accepted.
- No index uses `text` (checked: none); no view depends on `article_body` (checked: none).
- `lock_timeout = '60s'`, idempotent (skip when `text_override` exists), one transaction; gate markers in `gate_migrations.py`.
- Metadata-only: no table rewrite, seconds. It takes ACCESS EXCLUSIVE on `article_body` briefly: apply with no load running.

### 2.2 Loader

- `BODY_COLUMNS` writes `text_override`, never `text` (a virtual column cannot be written).
- The shard contract does not change: staging shards keep `text` (docs/09). The loader sets `text_override = NULL` when the shard's `text` equals `join(title, abstract, body_text)` computed in Python exactly as the SQL `<join>` does, else the shard's `text`.
- One Python function `sql_join(title, abstract, body_text) -> str | None` in `article_schema.py` mirrors `<join>` byte for byte (Claude's call: one place, pg test proves equality with the SQL expression on edge cases: NULLs, empty strings, whitespace-only parts, all empty).
- `content_hash` stays a stored column computed by the extractor from the shard's `text`. Dedup and the corpus order do not change.

### 2.3 Readers

Every reader keeps reading `text` and gets the same value as before for every row: the stored value where it differs from the join, the join where it was equal. Readers found: `corpus_materializer.py` (`b.text`), curate scripts on the materialized corpus (not the table), gate reports and audits that count text. A test proves `text` before = `text` after on a fixture of each shape.

### 2.4 Freeing the space (per partition, one at a time)

After 0014, existing rows still store the duplicate in `text_override`. Per partition:

1. **Null the duplicates.** `UPDATE ... SET text_override = NULL WHERE text_override = <join>` in batches by primary key with a commit per batch (bounded transactions; restartable: the WHERE makes a repeated batch a no-op). Rows where it differs keep their override.
2. **REPACK** the partition (PG 19 `REPACK`, checked present) to return the space. REPACK holds a strong lock: run with no load and no corpus job on that source.

Order (Claude's call): smallest first to measure (`dailymed`, `pmc`), then `pubmed`, then `openalex`. Before each big partition (`pubmed`, `openalex`) the owner gives a go (owner decision 2026-11-16). One heavy job at a time. E: has 1,005 GB free (2026-11-16); REPACK of the largest partition (123 GB) needs at most that partition's new size in free space.

Sources at 0 % equality need no step 1 (nothing to null); skip them.

Claude's call: UPDATE then REPACK, not "copy to a new partition and swap": the swap path needs DETACH/ATTACH of a partition of a table with dependent foreign keys and lineage, more risk for the same single rewrite; the UPDATE bloat is removed by the REPACK that follows anyway.

### 2.5 Measurement

A script reports per partition: rows, rows with `text_override` NULL, on-disk size before and after REPACK. Results go into the drift log.

## 3. Non-goals

- No change to extractors, serializers or the shard contract (docs/09 `text` stays).
- No change to `content_hash` or dedup.
- No search index on `text` (none exists today).
- No change to the 0 %-equality sources' stored content.

## 4. Anti-goals

- Any reader seeing a different `text` value than today for any row.
- A partition left half-processed without a restartable path.
- Two heavy jobs at once on E:.

## 5. Tests

- pg: migration 0014 applies twice (idempotent); after it, `text` of fixture rows of each shape (literature equal, pmc-style whitespace difference, structured text-only, all empty) equals the pre-migration value.
- pg: `sql_join` equals the SQL `<join>` on edge cases.
- pg: a load writes `text_override` NULL for an equal row and the value for a different row; `text` reads back unchanged.
- pg: the batch null-out is restartable and leaves `text` unchanged; REPACK is not run in tests.
- non-pg: `sql_join` unit cases; `BODY_COLUMNS` has no `text`.

## 6. Rollout

1. Merge. Apply 0014 to `episteme_test`, run the pg tests, then apply to `episteme` (no load running).
2. Null-out + REPACK: `dailymed`, then `pmc` (measure), then owner go for `pubmed`, then owner go for `openalex`.
3. Record sizes in the drift log.

## 7. Left to the builder

Batch size (config tunable, default 50,000 rows), helper names, the measurement script's format.
