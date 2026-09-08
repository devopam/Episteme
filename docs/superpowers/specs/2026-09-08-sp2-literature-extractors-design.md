# SP2 — Remaining Literature Extractors (spec-delta)

**Parent:** `docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md` — this is the
SP2 spec-delta. Roadmap §3 (SP2 charter) and §4.2 (`article_schema` refinement
protocol) are the frozen contract this refines; §4.6/§4.7 (`_lib` + Python CLI)
bind verbatim. Records the decisions from the 2026-09-08 brainstorm.

**Depends on:** SP1 (merged, PR #1) — `article_schema` v1.3, `staging_writer`,
`checkpoint_markers`, `postgres_loader`, `load_articles`, `graph_builder`,
`corpus_materializer`, `audit_trail`, `db/schema.sql` + `migrations/0001`,
`scripts/data/pmc/*.sh`, `run_pipeline.sh` (pmc chain + SP3's 22-token download
table). SP3 (merged, PR #2) — `_lib` fetch engine, `sources.env`, every source's
`download_<source>.sh`.

**Scope in one line:** turn the downloaded literature — `pubmed`, `apollo`,
`europepmc_manuscript`, `europepmc_preprint`, `guidelines`, `bookshelf` — into
`episteme.articles` rows + graph edges + `03_corpus` shards, one source at a
time, each gated by a field-shape report + schema sign-off. Plus
`europepmc/id_mappings` → `episteme.id_map` and `europepmc/lite_metadata`
enrichment. **No structured-source (SP4) work; no SFT/eval datasets; no
`chunks`/RAG.**

---

## 1. Architecture — one branch, one PR, three phases

Phased in dependency order (~16–18 tasks). Phase A must be proven against a small
`bookshelf` fixture before any source loads at scale.

- **Phase A — schema & graph groundwork** (§2): `article_schema` v1.3→v1.4
  (`container_id`, `book_meta`), `migrations/0002`, `episteme.article_parts` edge
  table, `articles_bookshelf` partitions, `graph_builder` third derivation,
  loader/materializer column pass-through.
- **Phase B — the six source extractors** (§3): each reconciles-or-writes
  `extract_<source>.py` to the post-SP1-β `extract_pmc.py` shape, retires the old
  `extract.py` name where one exists, wires into `run_pipeline.sh`, produces + gets
  sign-off on a field-shape report, runs one real end-to-end.
- **Phase C — enrichment feeds** (§4): `id_mappings` → `id_map` (post gzip
  integrity check); `lite_metadata` → an UPDATE pass on `articles` by PMCID.

---

## 2. Schema & graph groundwork (Phase A)

### 2.1 `article_schema` v1.3 → v1.4

Two nullable columns **appended** to `ARTICLE_COLUMNS` (append-only keeps the
COPY column order stable for existing sources):

- **`container_id text`** — NULL for every normal article and for a book's own
  row; set to the book's `id` (`bookshelf:<NBKid>`) on each book-part row.
  Deliberately generic (not `book_id`) so a future nested source can reuse it.
- **`book_meta jsonb`** — NULL except on book rows, where it holds
  `{isbn, editors, publisher, edition, n_parts}`. A jsonb bag, not flat columns:
  one source's concern, shape may shift. `article_schema` gains a
  `BOOK_META_KEYS = ("isbn", "editors", "publisher", "edition", "n_parts")`
  tuple used for validation only, not a rigid struct.

`SCHEMA_VERSION` → `"1.4"`. `build_row` passes both through. `decide_extract_status`:
a book row (has `book_meta`, `text` = TOC + `<front>`, may be short) is `ok` when
`text` ≥ `MIN_OK_TEXT_LEN` (200), else `partial` — **never `empty`**.

### 2.2 `migrations/0002_container_and_book_parts.sql`

Idempotent (`IF NOT EXISTS` throughout), applied after `0001` by
`init_database.sh` / `migrate_database.sh`.

- `ALTER TABLE episteme.articles ADD COLUMN IF NOT EXISTS container_id text`,
  `... ADD COLUMN IF NOT EXISTS book_meta jsonb`.
- `episteme.articles_bookshelf` — LIST partition of `episteme.articles` FOR
  VALUES IN (`'bookshelf'`), itself `PARTITION BY RANGE (year)` with
  `articles_bookshelf_y0` FROM (0) TO (1) + the same decade ranges as
  `articles_pmc` + a `_future` and the parent `articles_default` still catching
  everything else. The loader coerces `year None → 0` for bookshelf exactly as it
  does for PMC.
- `episteme.article_body_bookshelf` — LIST partition FOR VALUES IN
  (`'bookshelf'`); book row text + part row text both land here.
- `episteme.article_parts` — `container_id text NOT NULL`, `part_id text NOT
  NULL`, `source_file text NOT NULL`; `PARTITION BY HASH (container_id)` ×8
  (`_h0..h7`, MODULUS 8); `CREATE UNIQUE INDEX article_parts_uq ON
  episteme.article_parts (container_id, part_id)`. It **cannot** reuse
  `article_cites` — that is PMID-keyed (`src_pmid`/`dst_pmid`) and book-parts
  have no PMID. Grants mirror `article_cites`: `episteme_app` gets INSERT +
  SELECT only.
- The SQL/PGQ `CREATE PROPERTY GRAPH` block gains an `article_parts` edge def
  (`SOURCE KEY (container_id) REFERENCES episteme.articles (id)` /
  `DESTINATION KEY (part_id) REFERENCES episteme.articles (id)`). Per SP1-β,
  no property graph parses on this PG build — the recursive-CTE path is the
  tested one; PGQ stays code-complete-untested.

### 2.3 `graph_builder.build()`

After the existing `article_cites` + `article_mesh` derivations, a **third**:
for `source == "bookshelf"` (generically: any source that loaded rows with a
non-NULL `container_id`), `DELETE FROM episteme.article_parts WHERE source_file =
%s` then `INSERT` one `(container_id, part_id, source_file)` per loaded book-part
row — **read from `episteme.articles`, not raw XML** (unlike cites/mesh, which
parse JATS). `neighbours(conn, id, hops, kind="part")`: given a book id → its
parts; given a part id → its container then its siblings. The build return dict
gains a `parts` count. `_neighbours_cte` grows a `part` branch; `_neighbours_pgq`
likewise (untested).

### 2.4 loader / materializer

`postgres_loader`, `load_articles`, `corpus_materializer` carry `container_id` +
`book_meta` through the COPY / SELECT unchanged in mechanism (they already
project `ARTICLE_COLUMNS`). **Book rows (TOC + front-matter `text`) are INCLUDED
in the pretraining corpus** — front matter is useful context; no filtering. This
is a conscious call, recorded in the drift log.

---

## 3. The six source extractors (Phase B)

### 3.1 Common shape

Each module exposes:

```python
def extract_<source>(raw_dir: Path, processed_dir: Path, *,
                     max_files: int = 0, force: bool = False,
                     workers: int = 1, verbose: bool = False) -> dict
    # -> {"inputs": n, "ok": n, "failed": n, "rows": n}
```

plus a §4.7 `main(argv)` with `--raw-dir` / `--processed-dir` / `--max-files` /
`--force` / `--workers` / `--verbose` / `--report`. Audit bracket via
`get_settings().run_id` (`EPISTEME_RUN_ID`), `write_run_manifest` on completion
(best-effort), rows built with `build_text` / `content_hash` /
`decide_extract_status` / `subset_from_license`.

**Shared JATS/NXML primitives** — `_local`, `_child_text`, `_itertext`,
`parse_jats_fields` are lifted out of `extract_pmc.py` into
`src/episteme/data/jats.py`; `extract_pmc.py` keeps working through the new
module (no behaviour change — verified by the existing pmc tests). 4 of 6 SP2
sources parse JATS/NXML through it.

### 3.2 Per source

| source | input unit | row mapping | `extract_status` | reconcile / new |
|---|---|---|---|---|
| **pubmed** | one `pubmed26nNNNN.xml.gz`, streamed `iterparse` over `<PubmedArticle>` | 1 row/article; `text` = title + abstract (PubMed is abstract-only, no body); `pmid`/`doi`/`journal`/`year`; `mesh` from `<MeshHeadingList>`; `publication_types` from `<PublicationTypeList>` | `ok` if abstract present & `text ≥ 200`; `empty` if no abstract; `partial` if structured-abstract-only / truncated | **reconcile** `pubmed/extract.py` (442 lines) → `extract_pubmed.py`; retire `extract.py`; confirm/add `iterparse` streaming; optional md5 pre-check hook reusing `verify_pubmed.sh`'s logic |
| **apollo** | one file from `ApolloCorpus` (multilingual medical text; JSONL or per-doc) | 1 row/document; `text` = document body; `language` from the corpus lang tag; `title` synthesised from first line or NULL; `license` per Apollo's card → `subset` via `subset_from_license` | `ok` if `text ≥ 200`; `dropped` for non-medical / boilerplate per a length + heuristic gate the field-shape report tunes | **reconcile** `apollo/extract.py` (344 lines) → `extract_apollo.py`; retire `extract.py` |
| **europepmc_manuscript** | one `author_manuscript_{txt,xml}.PMC0NN…tar.gz`; walk members | 1 row per manuscript file in the tar; `is_manuscript = true`; `pmcid` from filename/member; `license` = "text mining / applicable copyright" → **`subset = text_mining`**; `text` from the `.txt` member, or JATS `<body>` for the `.xml` format | `ok` if body present; `partial` if only front matter; **never** placed in a `commercial` corpus shard | **new** — write `extract_europepmc_manuscripts.py` (dual txt/xml path); no pre-existing module |
| **europepmc_preprint** | one per-ID `PPR{n}.xml` from the REST harvest | 1 row/preprint; `source_record_id = PPR{n}`; JATS `<body>`; `doi` present, `pmid` usually absent; `license` per-preprint (CC-BY common) | `ok` if full-text XML returned & body present; `empty` if API returned metadata-only; `dropped` on 404 | **reconcile** `extract_europepmc_preprints.py` (353 lines) — `discover()` changes to glob per-ID `.xml` (not range `.xml.gz`); **new** download module (REST crawl, §3.3); wrapper flips to `exec "$PY" -m` |
| **guidelines** | the `epfl-llm/guidelines` HF dataset (one parquet/jsonl per split) | 1 row/guideline doc; `text` = guideline body; `title` from the doc field; `journal` ← issuing body (NICE/CDC/WHO/…); `year` if present; `license` per the dataset card → `subset` (settled at field-shape) | `ok` if `text ≥ 200`; `partial` for stub/redirect entries | **new** — `scripts/data/guidelines/download_guidelines.sh` + `[guidelines]` in the WRAPPER table + `extract_guidelines.py`; **pretraining prose split only** — skip any QA-formatted split |
| **bookshelf** | one per-book `.tar.gz` (book NXML + assets) | **1 book row** (`container_id` NULL, `book_meta = {isbn, editors, publisher, edition, n_parts}`, `text` = `<toc>` + `<front>`) **+ N book-part rows** (`container_id` = book id, `id = bookshelf:<NBKid>:<part-id>`, `title` = part title, `text` = `<body>` of that `<book-part>`), skipping parts whose only content is `<toc>` / copyright / `<index>` | book row: `ok` if front+TOC `text ≥ 200` else `partial`, never `empty`; part row: standard rules | **new** — `extract_bookshelf.py`, NXML `<book>` / `<book-part>` walk via `jats.py` |

### 3.3 `europepmc_preprint` download pivot (acquisition-layer delta)

EBI discontinued the bulk `.xml.gz` preprint feed (SP3 whole-branch review /
2026-09-08 spike: `ftp.ebi.ac.uk/pub/databases/pmc/preprints/` now holds only
`pprid.txt.gz` — 73,326 IDs — and a privacy notice; no bulk full-text anywhere
under `/pub/databases/pmc/`). SP2 replaces the download side with a REST harvest:

- New `src/episteme/data/europepmc/preprints/download_europepmc_preprints.py`
  (functional, not a stub): fetch `pprid.txt.gz` from `$EUROPEPMC_PREPRINT_BASE`
  → for each `PPR{n}` not already on disk, `GET
  {EUROPEPMC_BASE}/{PPRid}/fullTextXML` with exponential backoff + a 429/503
  retry budget → write `01_raw/europepmc/preprints/PPR{n}.xml`. A resume-state
  file (`.harvest_state`) tracks the last completed ID; `--max-files N` caps the
  batch. `EUROPEPMC_BASE` (the REST var SP3 kept) is now live for this feed.
- `scripts/data/europepmc/preprints/download_europepmc_preprint.sh` flips from a
  bash→`_lib` wrapper to the `exec "$PY" -m …` shape (the `$PY`-discovery block
  from `pmc/*.sh`). Its `run_pipeline.sh` table entry is unchanged.
- SP2 wires only the **capped** harvest; a full 73k-ID crawl is an ops run,
  carried forward.

---

## 4. `run_pipeline.sh` wiring & orchestration

SP3 left every non-pmc source at `die "… SP2 / SP4" 3`. SP2 flips that for the
**six literature sources** only.

- **Per-source stage chain.** SP2 creates thin `scripts/data/<source>/{extract,
  load,graph}_<source>.sh` wrappers (the `$PY`-discovery shape from `pmc/*.sh`).
  Only **`extract`** is a per-source module (`episteme.data.<...>.extract_<source>`).
  **`load`** and **`graph`** reuse SP1-β's generic modules —
  `load_<source>.sh` → `exec "$PY" -m episteme.data.load_articles --source
  <source>`; `graph_<source>.sh` → `exec "$PY" -m
  episteme.data.graph_builder --source <source> --raw-dir …` (the raw-dir arg is
  a no-op for the `article_parts` derivation, which reads `articles`; still
  passed for the cites/mesh derivations where the source has JATS). `<lit-source>
  all` = download → extract → load → graph. Non-literature non-pmc sources keep
  the `die 3`.
- **`materialize` / `enrich` stay pmc-only in the per-source table** — the
  corpus shard is rebuilt across ALL loaded sources by one pass. SP2 adds a
  non-source-scoped `run_pipeline.sh corpus materialize` alias
  (→ `materialize_corpus.sh`).
- **`--max-files N`** threads to `extract` (`extract_args`), and now also to
  `download` for `europepmc_preprint` (harvest cap).
- **`id_mappings` / `lite_metadata`** — not article sources, so **not** in the
  stage-chain table:
  - `run_pipeline.sh europepmc_id_mappings load` →
    `exec "$PY" -m episteme.data.europepmc.id_mappings.load_id_mappings`
    (gzip-integrity check per `docs/10` §5 → COPY/upsert into `episteme.id_map`).
  - `run_pipeline.sh europepmc_lite enrich` →
    `exec "$PY" -m episteme.data.europepmc.lite_metadata.enrich_from_lite`
    (UPDATE `articles.journal/year/mesh` gaps by `pmcid`; not a text source).
  - Both refuse `extract` / `graph` with a clear one-line message (not a
    traceback).
- **`WRAPPER` table** gains `[guidelines]="guidelines/download_guidelines.sh"`
  (→ 22 tokens). `[europepmc_preprint]` path unchanged; wrapper body flips to
  `exec "$PY" -m`.
- **`--dry-run` generalisation.** The SP3 C2 guard (`pmc <non-download> --dry-run
  → die 3`) generalises: `--dry-run` is rejected on any
  `extract`/`load`/`graph`/`enrich` stage for **every** source (they write the
  DB), not just pmc. `download --dry-run` unchanged. Audit bracket,
  `EPISTEME_RUN_ID` export, DR-1 skip: unchanged.

---

## 5. Testing & sign-off

- **`pytest -q -m "not pg"`** stays green. SP2 adds: per-extractor unit tests
  (fixture XML/JSONL → expected row dict; every `extract_status` branch; the
  bookshelf `container_id` / `book_meta` / `part_of` mapping); `test_migrations.py`
  (apply `0001`+`0002` to a scratch DB → assert `container_id`/`book_meta`
  columns, `articles_bookshelf*` partitions, `article_parts*` partitions +
  unique index exist).
- **`pytest -q` (pg)** gains a bookshelf end-to-end (`pg`-marked): fixture book
  `.tar.gz` → extract → load → `articles` has 1 book + N parts (correct
  `container_id`), `article_parts` has N edges, `graph_builder.neighbours(book_id,
  kind="part")` returns the parts.
- **Per-source gate (a plan task boundary):** fixture → `main --report`
  field-shape → **human sign-off recorded in the task ledger** (null-rates,
  `subset`/`license` mapping, `apollo`/`guidelines` drop-heuristic values, any
  v1.4→v1.x column delta) → real end-to-end → `articles` rows with correct
  `source`/`subset`/`license`/`extract_status` + a `03_corpus` shard + `_audit`
  chain intact + JSONL mirror.
  - **Real-slice sizing.** A source's "file" (unit of `--max-files`) is one
    upstream archive, which for `pubmed` / `bookshelf` / `europepmc_manuscript`
    is *thousands* of rows, not one. The real end-to-end therefore uses the
    smallest real unit that yields ≥1 row: `--max-files 1` at the `download`
    stage (one archive / one book / one manuscript tar), then the extractor's
    own `--max-files` (row cap) set to ~5–20 so the load + shard + audit chain is
    exercised without a multi-GB pull. `apollo` / `guidelines` are per-doc, so
    `--max-files 2` at extract behaves like the PMC slice.
- **Drift-log entry** at the end: SP2 landed, schema v1.4, the
  `container_id`/`article_parts` model, per-source row counts, the preprints REST
  pivot + guidelines wrapper as acquisition-layer deltas, deferred items.

---

## 6. Non-goals

- Structured sources (chembl/uniprot/pubchem/clinvar/reactome/mesh/ontologies/
  openalex) — SP4.
- SFT / eval datasets; `guidelines` QA-formatted splits.
- `chunks` population / any Phase-1 RAG.
- `pgvector` / `pg_search` (still need a PG build where the extensions install;
  `migrations/0001` remains queued).
- A full `europepmc_preprint` 73k-ID backfill — SP2 wires the capped harvest;
  the full crawl is an ops run.
- `mesh` descriptor-tree → `article_mesh` (that rides SP4's `serialize_mesh`).
- Rewriting `docs/07–11` — SP5.
- `decontaminate_benchmarks` with real HF eval sets (SP1-β carry — still
  `sample_only=True` until the eval sets are downloaded).

---

## 7. Open items carried forward

| # | Item | Owner |
|---|---|---|
| 1 | Full `europepmc_preprint` 73k-ID REST backfill | ops, post-SP2 |
| 2 | `apollo` / `guidelines` non-medical drop-heuristic final thresholds | resolved at each source's field-shape sign-off |
| 3 | `guidelines` licence → `subset` mapping (dataset card is per-source) | resolved at `guidelines` field-shape sign-off |
| 4 | `book_meta` key set may grow past `BOOK_META_KEYS` once real Bookshelf NXML is seen | resolved at `bookshelf` field-shape sign-off |
| 5 | PGQ path for `article_parts` untested (no property graph parses on this PG build) | when a PG build with working SQL/PGQ is available |
| 6 | `corpus materialize` alias is not source-scoped — a partial SP2 (some sources loaded, others not) still rebuilds the whole shard | acceptable; noted so a reviewer isn't surprised |

---

## Document control

| Version | Date | Notes |
|---|---|---|
| v1 | 2026-09-08 | SP2 spec-delta from the 2026-09-08 literature-extractor brainstorm. |
