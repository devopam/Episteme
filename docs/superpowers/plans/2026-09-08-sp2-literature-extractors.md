# SP2 — Literature Extractors — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the downloaded literature (`pubmed`, `apollo`, `europepmc_manuscript`, `europepmc_preprint`, `guidelines`, `bookshelf`) into `episteme.articles` rows + graph edges + `03_corpus` shards, one source at a time, each gated by a field-shape report and a schema sign-off; plus `europepmc/id_mappings` → `episteme.id_map` and `europepmc/lite_metadata` enrichment.

**Architecture:** Three phases on one branch. **Phase A** does the schema/graph groundwork (`article_schema` v1.4, `migrations/0002`, `episteme.article_parts` edge table, `graph_builder` third derivation) proven against a bookshelf fixture. **Phase B** reconciles-or-writes six `extract_<source>.py` modules to the post-SP1-β `extract_pmc.py` shape and wires each into `run_pipeline.sh`. **Phase C** does the two enrichment feeds. JATS/NXML parsing is shared through a new `src/episteme/data/jats.py`.

**Tech Stack:** Python 3.10+ (`defusedxml` streaming `iterparse`, `pyarrow`, `polars` lazy — no pandas per roadmap §4.10), PostgreSQL 19beta3 (`localhost:5433`, partitioned per ADR-0002), bash (`_lib/common.sh` `exec "$PY" -m` wrappers), pytest (`pg` / `not pg` marks).

**Spec:** `docs/superpowers/specs/2026-09-08-sp2-literature-extractors-design.md` (SP2 spec-delta) — refines `docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md` §3/§4.2 (frozen) + `docs/superpowers/specs/2026-09-02-sp1-storage-core.md`. Read both.

## Global Constraints

- **Branch:** `sp2-literature-extractors` (already checked out; the spec landed on it as `cffe845`). Base `main` @ `b66c7af`.
- **`config.py` is the ONLY module in `src/episteme/` that reads `os.environ` / `os.getenv`.** Audit run-id comes from `get_settings().run_id` (`EPISTEME_RUN_ID`), never a direct env read in an extractor.
- **`article_schema.ARTICLE_COLUMNS` is append-only.** New columns (`container_id`, `book_meta`) go at the END; existing sources' COPY column order is unchanged. `SCHEMA_VERSION` → `"1.4"`.
- **Download only for non-pmc-non-literature.** SP2 flips `run_pipeline.sh` to a full stage chain for the SIX literature sources only; every other non-pmc source keeps `die "… SP2 / SP4" 3`.
- **`--dry-run` is rejected on any `extract`/`load`/`graph`/`enrich` stage, every source** (they write the DB) — generalise SP3's pmc-only C2 guard.
- **Migrations are idempotent** (`IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`), applied by `migrate_database.sh` after `0001`, tracked in `episteme._migrations`.
- **`jats.py` extraction is behaviour-preserving for pmc** — `extract_pmc.py` keeps working through it; the existing pmc tests must stay green with zero changes to their assertions.
- **Every extractor:** importable `extract_<source>(raw_dir, processed_dir, *, max_files=0, force=False, workers=1, verbose=False) -> {"inputs","ok","failed","rows"}` + `main(argv)` with `--raw-dir --processed-dir --max-files --force --workers --verbose --report` (§4.7). `--report` prints a field-shape table (null-rate per column, `extract_status` histogram, `subset`/`license` breakdown) and writes NO rows.
- **Per-source gate:** a source's full load task does NOT complete until its field-shape `--report` output is in the ledger with a **human sign-off line** (`Ledger: <source> field-shape signed-off — <who> — <date>`), then a real end-to-end runs (`--max-files 1` at download, ~5–20 row cap at extract for archive sources; `--max-files 2` for per-doc sources).
- **`pytest -q -m "not pg"` stays green** (currently 42 core + 25 net-gated dispatch = 67). SP2 adds unit tests per extractor + `test_migrations.py` + `test_jats.py`. **`pytest -q` (pg, `.env` sourced)** currently 72 passed / 2 skipped; SP2 adds the bookshelf `pg` end-to-end.
- **`pg`-marked tests target `episteme_test`** (`TEST_PG_DSN` from `.env`), NEVER the real `episteme` DB. Any manual DB verification uses `PGDATABASE=episteme_test`.
- **`bash -n` clean on every `.sh` touched;** `shellcheck --severity=warning` (CI) clean; `ruff` clean (pre-commit enforces).
- **Commits end with:**
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```

---

## File-structure map

**Created:**
- `src/episteme/data/jats.py` — shared JATS/NXML primitives (`local_name`, `child_text`, `itertext`, `parse_jats_fields`, `iter_book_parts`) lifted from `extract_pmc.py`.
- `src/episteme/data/db/migrations/0002_container_and_book_parts.sql` — `container_id`/`book_meta` columns, `articles_bookshelf*` + `article_body_bookshelf` partitions, `episteme.article_parts` HASH×8 edge table, PGQ edge def.
- `src/episteme/data/europepmc/manuscripts/extract_europepmc_manuscripts.py` — tar-walk → rows.
- `src/episteme/data/europepmc/preprints/download_europepmc_preprints.py` — **replaces the stub**; REST harvest off `pprid.txt.gz`.
- `src/episteme/data/guidelines/__init__.py`, `src/episteme/data/guidelines/extract_guidelines.py`.
- `src/episteme/data/bookshelf/__init__.py`, `src/episteme/data/bookshelf/extract_bookshelf.py`.
- `src/episteme/data/europepmc/id_mappings/load_id_mappings.py` — gzip-integrity + COPY into `id_map`.
- `src/episteme/data/europepmc/lite_metadata/enrich_from_lite.py` — UPDATE `articles` by pmcid.
- `scripts/data/guidelines/download_guidelines.sh` — thin `hf_fetch` wrapper.
- `scripts/data/<source>/{extract,load,graph}_<source>.sh` for the six literature sources (thin `exec "$PY" -m` wrappers).
- `scripts/data/europepmc/id_mappings/load_europepmc_id_mappings.sh`, `scripts/data/europepmc/lite_metadata/enrich_europepmc_lite.sh`.
- `tests/test_jats.py`, `tests/test_migrations.py`, `tests/data/test_extract_{pubmed,apollo,europepmc_manuscript,europepmc_preprint,guidelines,bookshelf}.py`, `tests/data/test_bookshelf_end_to_end.py` (`pg`).
- `tests/fixtures/sp2/…` — one tiny real-shaped input per source.

**Modified:**
- `src/episteme/data/article_schema.py` — `+container_id`, `+book_meta`, `BOOK_META_KEYS`, `SCHEMA_VERSION="1.4"`, `build_row` pass-through, `decide_extract_status` book-row rule.
- `src/episteme/data/pmc/extract_pmc.py` — import the four helpers from `jats.py` (delete the local copies).
- `src/episteme/data/pubmed/extract_pubmed.py`, `src/episteme/data/apollo/extract_apollo.py` — **replace the 16-line stubs** with the reconciled real modules; **`git rm`** `src/episteme/data/pubmed/extract.py`, `src/episteme/data/apollo/extract.py`.
- `src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py` — `discover()` globs per-ID `*.xml`; module shape aligned to §4.7.
- `src/episteme/data/graph_builder.py` — third derivation (`article_parts` from loaded rows), `neighbours(kind="part")`, `_neighbours_cte`/`_neighbours_pgq` `part` branch, `build()` returns `parts`.
- `src/episteme/data/postgres_loader.py` / `load_articles.py` / `corpus_materializer.py` — project the two new columns (mechanism unchanged).
- `src/episteme/data/db/schema.sql` — `article_parts` PGQ edge def in the `CREATE PROPERTY GRAPH` block (columns/partitions stay in `0002` for a fresh DB via `init_database.sh` which applies `schema.sql` then migrations… **decision: put the full `article_parts` + `container_id` DDL in `0002` only; `schema.sql` gets just the PGQ edge line guarded so a fresh apply that has already run `0002` doesn't double-create**). See Task 2.
- `scripts/data/run_pipeline.sh` — six literature sources get the extract/load/graph/all chain; `[guidelines]` in `WRAPPER`; `--dry-run` rejected on write stages for every source; `corpus materialize` alias.
- `scripts/data/europepmc/preprints/download_europepmc_preprint.sh` — flip to `exec "$PY" -m`.
- `docs/project-incubation-baseline.md` — SP2 drift-log entry (final task).

---

## Phase A — schema & graph groundwork

### Task 1: `jats.py` — shared parsing primitives

**Files:**
- Create: `src/episteme/data/jats.py`
- Modify: `src/episteme/data/pmc/extract_pmc.py:70-172` (delete local `_local`/`_child_text`/`_itertext`/`parse_jats_fields`, import from `jats`)
- Test: `tests/test_jats.py`

**Interfaces:**
- Produces: `local_name(tag) -> str`, `child_text(parent, name) -> str`, `itertext(el) -> str`, `parse_jats_fields(xml_path: Path) -> dict[str, Any]`, `iter_book_parts(root) -> Iterator[Element]` (new — yields `<book-part>` elements, recursing, for bookshelf/manuscripts NXML).
- Consumed by: `extract_pmc.py` (unchanged behaviour), and Tasks 5–10.

- [ ] **Step 1: Write the failing test** — `tests/test_jats.py`

```python
from pathlib import Path
from episteme.data.jats import local_name, child_text, itertext, parse_jats_fields, iter_book_parts
import defusedxml.ElementTree as ET

def test_local_name_strips_namespace():
    assert local_name("{http://x}article") == "article"
    assert local_name("plain") == "plain"

def test_child_text_reads_nested_name(tmp_path):
    p = tmp_path / "a.xml"
    p.write_text('<contrib><name><surname>Doe</surname><given-names>J</given-names></name></contrib>')
    root = ET.parse(str(p)).getroot()
    assert child_text(root, "surname") == "Doe"

def test_parse_jats_fields_pmc_fixture():
    # reuse the SP1-β pmc fixture — parse must yield the same title/abstract it did before jats.py
    fx = Path("tests/fixtures/pmc")  # existing
    xmls = sorted(fx.rglob("*.xml"))
    assert xmls, "pmc fixture xml present"
    fields = parse_jats_fields(xmls[0])
    assert fields.get("title")
    assert "abstract" in fields

def test_iter_book_parts_yields_each_part(tmp_path):
    p = tmp_path / "book.xml"
    p.write_text(
        '<book><book-part id="p1"><book-part-meta><title>Ch1</title></book-part-meta>'
        '<body><p>one</p></body></book-part>'
        '<book-part id="p2"><body><p>two</p></body></book-part></book>'
    )
    root = ET.parse(str(p)).getroot()
    ids = [bp.get("id") for bp in iter_book_parts(root)]
    assert ids == ["p1", "p2"]
```

- [ ] **Step 2: Run — verify it fails**

`.venv/Scripts/python.exe -m pytest -q tests/test_jats.py` → FAIL (`ModuleNotFoundError: episteme.data.jats`).

- [ ] **Step 3: Create `jats.py`**

Move `_local`→`local_name`, `_child_text`→`child_text`, `_itertext`→`itertext`, `parse_jats_fields` **verbatim** out of `extract_pmc.py` into `src/episteme/data/jats.py` (keep the leading `_`-free public names; add thin `_local = local_name` aliases inside `extract_pmc.py`'s import line if any internal call sites use the underscore form — grep first). Add:

```python
from collections.abc import Iterator

def iter_book_parts(root) -> Iterator:
    """Yield every <book-part> element under root, depth-first, including nested."""
    for el in root.iter():
        if local_name(el.tag) == "book-part":
            yield el
```

- [ ] **Step 4: Rewire `extract_pmc.py`**

Replace the four local defs with:
```python
from episteme.data.jats import local_name as _local, child_text as _child_text, itertext as _itertext, parse_jats_fields
```
(keep the `_`-prefixed aliases so no other line in the file changes). Delete lines `70-172`'s bodies.

- [ ] **Step 5: Run — both suites green**

`.venv/Scripts/python.exe -m pytest -q tests/test_jats.py` → PASS.
`.venv/Scripts/python.exe -m pytest -q -m "not pg" -k "pmc or jats"` → PASS (existing pmc extractor tests unchanged).

- [ ] **Step 6: Commit** — `refactor(sp2): lift JATS primitives into episteme.data.jats`

---

### Task 2: `article_schema` v1.4 + `migrations/0002`

**Files:**
- Modify: `src/episteme/data/article_schema.py`
- Create: `src/episteme/data/db/migrations/0002_container_and_book_parts.sql`
- Modify: `src/episteme/data/db/schema.sql` (PGQ edge def only — see Step 4)
- Test: `tests/test_migrations.py`, plus additions to `tests/data/test_article_schema.py` if it exists (grep)

**Interfaces:**
- Produces: `ARTICLE_COLUMNS` with `container_id`, `book_meta` appended; `BOOK_META_KEYS`; `SCHEMA_VERSION == "1.4"`. DB: `episteme.articles.container_id text`, `episteme.articles.book_meta jsonb`, `episteme.articles_bookshelf*` partitions, `episteme.article_body_bookshelf`, `episteme.article_parts` (+`_h0..h7`) with unique `(container_id, part_id)`.
- Consumed by: every Task 3–12.

- [ ] **Step 1: Write the failing test** — `tests/test_migrations.py`

```python
import os, subprocess
from pathlib import Path
import pytest

pytestmark = pytest.mark.pg  # applies 0001+0002 to episteme_test

REPO = Path(__file__).resolve().parents[1]

@pytest.fixture(scope="module")
def sa_conn():
    import psycopg
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c

def test_migration_0002_adds_container_columns(sa_conn):
    with sa_conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='episteme' AND table_name='articles'
              AND column_name IN ('container_id','book_meta')
        """)
        cols = {r[0] for r in cur.fetchall()}
    assert cols == {"container_id", "book_meta"}

def test_migration_0002_article_parts_table_and_partitions(sa_conn):
    with sa_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.article_parts')")
        assert cur.fetchone()[0] is not None
        cur.execute("""
            SELECT count(*) FROM pg_inherits
            JOIN pg_class p ON p.oid = inhparent
            WHERE p.relname = 'article_parts'
        """)
        assert cur.fetchone()[0] == 8  # 8 hash buckets

def test_migration_0002_bookshelf_partition(sa_conn):
    with sa_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.articles_bookshelf')")
        assert cur.fetchone()[0] is not None
        cur.execute("SELECT to_regclass('episteme.articles_bookshelf_y0')")
        assert cur.fetchone()[0] is not None
```

Also a `not pg` schema test:
```python
def test_schema_version_and_columns():
    from episteme.data import article_schema as s
    assert s.SCHEMA_VERSION == "1.4"
    assert s.ARTICLE_COLUMNS[-2:] == ["container_id", "book_meta"]
    assert "isbn" in s.BOOK_META_KEYS
```

- [ ] **Step 2: Run — fails**

`.venv/Scripts/python.exe -m pytest -q -m "not pg" tests/test_migrations.py` → FAIL (SCHEMA_VERSION 1.3).

- [ ] **Step 3: Edit `article_schema.py`**

```python
SCHEMA_VERSION = "1.4"   # was "1.3" — +container_id, +book_meta (SP2)
BOOK_META_KEYS = ("isbn", "editors", "publisher", "edition", "n_parts")
```
Append to `ARTICLE_COLUMNS`: `"container_id"`, `"book_meta"` (after `"pdf_url"`).
`build_row`: after the existing field copies, `out.setdefault("container_id", row.get("container_id"))` and `out.setdefault("book_meta", row.get("book_meta"))`.
`decide_extract_status`: at the top, `if row.get("book_meta") is not None:` → return `("ok" if len(text or "") >= MIN_OK_TEXT_LEN else "partial", notes)` — a book row is never `empty`.

- [ ] **Step 4: Write `0002_container_and_book_parts.sql`**

```sql
-- SP2 migration 0002 — container_id / book_meta + episteme.article_parts + bookshelf partitions.
-- Idempotent. Applied by migrate_database.sh after 0001.

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

-- book -> part edges. HASH(container_id) x8 to match article_cites/mesh.
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
```

- [ ] **Step 5: `schema.sql` PGQ edge def**

In the `CREATE PROPERTY GRAPH` block, after the `article_cites` edge, add an `article_parts` edge def (same shape, `REFERENCES episteme.articles (id)` on both keys). Do NOT add the table/partition DDL to `schema.sql` — `0002` owns it; `init_database.sh` runs `schema.sql` then `migrate_database.sh`, so a fresh DB gets it from `0002`. Guard: the `CREATE PROPERTY GRAPH` is already wrapped in a `DO`/exception block in SP1-β (it fails to parse on this build) — the new edge line rides that.

- [ ] **Step 6: Run**

`.venv/Scripts/python.exe -m pytest -q -m "not pg" tests/test_migrations.py::test_schema_version_and_columns` → PASS.
`bash scripts/data/db/migrate_database.sh episteme_test` → applies `0002`, exit 0 (0001 may fail on missing pgvector — run `0002` directly with `psql` if so: `PGPASSWORD=$EPISTEME_SYS_ADMIN_PASSWORD psql -h $PGHOST -p $PGPORT -U episteme_sys_admin -d episteme_test -f src/episteme/data/db/migrations/0002_container_and_book_parts.sql` and record that 0001 is the known pgvector block).
`set -a; . ./.env; set +a; .venv/Scripts/python.exe -m pytest -q tests/test_migrations.py` → 4 PASS (or skip if no `TEST_PG_DSN`).

- [ ] **Step 7: Commit** — `feat(sp2): article_schema v1.4 + migration 0002 (container_id, book_meta, article_parts)`

---

### Task 3: `graph_builder` — `article_parts` derivation + `part` neighbours

**Files:**
- Modify: `src/episteme/data/graph_builder.py`
- Test: additions to `tests/data/test_graph_builder.py` (grep for the file; else create)

**Interfaces:**
- Consumes: `article_schema` v1.4 (`container_id`), `episteme.article_parts` (Task 2).
- Produces: `build(conn, *, source, raw_dir, run_id)` return dict gains `"parts": int`; `neighbours(conn, id, hops=1, kind="part")` returns book↔part ids. Consumed by Task 10 (bookshelf) + Task 4 (`graph_bookshelf.sh`).

- [ ] **Step 1: Failing test**

```python
import pytest
pytestmark = pytest.mark.pg

def test_build_populates_article_parts(pg_conn_with_bookshelf_rows):
    # fixture: 1 book row (id='bookshelf:NBK1') + 2 part rows (container_id='bookshelf:NBK1')
    from episteme.data.graph_builder import build, neighbours
    res = build(pg_conn_with_bookshelf_rows, source="bookshelf", raw_dir="tests/fixtures/sp2/bookshelf", run_id="t")
    assert res["parts"] == 2
    parts = neighbours(pg_conn_with_bookshelf_rows, "bookshelf:NBK1", kind="part")
    assert set(parts) == {"bookshelf:NBK1:p1", "bookshelf:NBK1:p2"}
```

- [ ] **Step 2: Run — fails** (`build` has no `parts` key; `neighbours` rejects `kind="part"`).

- [ ] **Step 3: Implement**

In `build()`, after the cites+mesh loop, add:
```python
# SP2: container -> part edges, read from loaded rows (not raw XML).
part_rows = 0
with conn.cursor() as cur:
    cur.execute("DELETE FROM episteme.article_parts WHERE source_file = %s", (source_file,))
    cur.execute(
        "INSERT INTO episteme.article_parts (container_id, part_id, source_file) "
        "SELECT container_id, id, %s FROM episteme.articles "
        "WHERE source = %s AND source_file = %s AND container_id IS NOT NULL "
        "ON CONFLICT (container_id, part_id) DO NOTHING",
        (source_file, source, source_file),
    )
    part_rows += cur.rowcount
out["parts"] = part_rows
```
(`source_file` is already looped over in `build`; if `build` is not per-`source_file`, adapt to its actual loop var — grep the function.)

`neighbours`: add a `kind == "part"` branch → `_neighbours_cte(conn, id, hops, "part")`. In `_neighbours_cte`, `part` case:
```sql
SELECT part_id FROM episteme.article_parts WHERE container_id = %s
UNION
SELECT container_id FROM episteme.article_parts WHERE part_id = %s
```
`_neighbours_pgq`: add a `part` MATCH pattern (untested, mirror the cites pattern).

- [ ] **Step 4: Run** — the `pg` test passes (`set -a; . ./.env; set +a; pytest -q tests/data/test_graph_builder.py -k part`).

- [ ] **Step 5: Commit** — `feat(sp2): graph_builder derives article_parts + kind="part" neighbours`

---

### Task 4: loader / materializer column pass-through + `run_pipeline.sh` chain skeleton

**Files:**
- Modify: `src/episteme/data/postgres_loader.py`, `src/episteme/data/load_articles.py`, `src/episteme/data/corpus_materializer.py`
- Modify: `scripts/data/run_pipeline.sh`
- Create: `scripts/data/bookshelf/{extract,load,graph}_bookshelf.sh` (the first source's wrappers — the shape the rest copy)
- Test: `tests/test_run_pipeline_dispatch.py` additions

**Interfaces:**
- Consumes: Task 2 columns.
- Produces: `run_pipeline.sh <lit-source> <extract|load|graph|all>` dispatches per-source wrappers; `corpus materialize` alias; `--dry-run` rejected on write stages. `LIT_SOURCES` set in `run_pipeline.sh`.

- [ ] **Step 1: Failing test** — add to `tests/test_run_pipeline_dispatch.py`

```python
def test_bookshelf_extract_dry_run_dies_3(tmp_path):
    # SP2: --dry-run rejected on write stages for every source
    proc = _run(["bookshelf", "extract", "--dry-run"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]

def test_bookshelf_extract_dispatches(tmp_path):
    # no data -> extractor exits 0 (nothing to do) or 1 (no raw dir); NOT 3 (dispatch bug)
    proc = _run(["bookshelf", "extract", "--max-files", "1"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode in (0, 1), proc.stderr[-2000:]

def test_corpus_materialize_alias(tmp_path):
    proc = _run(["corpus", "materialize", "--dry-run"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3  # dry-run on a write stage
```

- [ ] **Step 2: Run — fails** (`bookshelf extract` → currently `die 3` "not in SP3").

- [ ] **Step 3: `run_pipeline.sh`**

- Near the `WRAPPER` table, add `LIT_SOURCES="pubmed apollo europepmc_manuscript europepmc_preprint guidelines bookshelf"` and a helper `_is_lit() { case " $LIT_SOURCES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }`.
- Generalise the C2 guard: replace the `pmc`-specific block with
  ```sh
  case "$STAGE" in
      extract|load|graph|enrich)
          [ "${EPISTEME_DRY_RUN:-0}" != "1" ] || die "$STAGE writes the DB — --dry-run is not supported (use it on download only)" 3 ;;
  esac
  ```
- In the non-pmc dispatch `case "$STAGE"`: add `extract|load|graph)` and widen `all`:
  ```sh
  extract|load|graph)
      _is_lit "$SOURCE" || die "$SOURCE $STAGE is not in SP2 — SP4 (structured serialize)" 3
      w="$HERE/$SOURCE/${STAGE}_${SOURCE}.sh"
      [ -f "$w" ] || die "wrapper not found: $w (not yet implemented?)" 3
      run_stage "$STAGE" bash "$w" "${wrapper_args[@]}" ;;
  ```
  and for `all` on a lit source: download → extract → load → graph (four `run_stage` calls), else the existing download-then-note behaviour.
- Add a `SOURCE == "corpus"` special case before the WRAPPER lookup: `STAGE == "materialize"` → `run_stage materialize bash "$HERE/materialize_corpus.sh" "${wrapper_args[@]}"`; anything else → `die "corpus: only 'materialize' is wired" 3`. (`corpus` is not in `WRAPPER`, so add it to the early-validation allowlist.)
- `[guidelines]="guidelines/download_guidelines.sh"` in `WRAPPER`.

- [ ] **Step 4: Python pass-through**

`postgres_loader` / `load_articles`: they already `SELECT`/`COPY` `ARTICLE_COLUMNS` — verify by grep they build the column list from `article_schema.ARTICLE_COLUMNS` (not a hardcoded list). If hardcoded anywhere, replace with `ARTICLE_COLUMNS`. `corpus_materializer._SELECT` / `_COLUMNS`: `book_meta` and `container_id` are NOT needed in the corpus shard (it's `text`-centric) — leave `_COLUMNS` as-is; just confirm the `SELECT a.* ` style doesn't break on the new columns (it selects explicit columns — fine).

- [ ] **Step 5: `bookshelf/{extract,load,graph}_bookshelf.sh`**

```bash
#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../_lib/common.sh"
load_dotenv
require_env EPISTEME_ACTOR
PY="${PYTHON:-}"; if [ -z "$PY" ]; then for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }; done; fi
[ -n "$PY" ] || die "python not found (set PYTHON=)"
exec "$PY" -m episteme.data.bookshelf.extract_bookshelf "$@"
```
`load_bookshelf.sh` → `exec "$PY" -m episteme.data.load_articles --source bookshelf "$@"`.
`graph_bookshelf.sh` → `exec "$PY" -m episteme.data.graph_builder --source bookshelf --raw-dir "${EPISTEME_RAW_ROOT:-./01_raw}/bookshelf" "$@"`.
`chmod +x` + `git update-index --chmod=+x` all three.

- [ ] **Step 6: Run** — `pytest -q -m "not pg" tests/test_run_pipeline_dispatch.py` (the 3 new + the existing 25 still pass; `bookshelf extract` needs `extract_bookshelf.py` to at least import — so this task also lands a minimal importable stub that `main()` returns 0 on empty input; Task 10 fills it). `bash -n` all three wrappers + `run_pipeline.sh`.

- [ ] **Step 7: Commit** — `feat(sp2): run_pipeline.sh literature stage chain + loader column pass-through`

---

## Phase B — the six source extractors

> Each Phase-B task follows the same shape. Read the spec's §3.2 row for the source + `extract_pmc.py` as the template. The task is NOT complete until the field-shape `--report` is signed off in the ledger and one real end-to-end has run.

### Task 5: `pubmed` — reconcile `extract_pubmed.py`

**Files:**
- Modify: `src/episteme/data/pubmed/extract_pubmed.py` (replace 16-line stub)
- Delete: `git rm src/episteme/data/pubmed/extract.py`
- Modify: `scripts/data/pubmed/` — add `extract_pubmed.sh`, `load_pubmed.sh`, `graph_pubmed.sh` (copy Task 4's bookshelf shape, s/bookshelf/pubmed/)
- Test: `tests/data/test_extract_pubmed.py`, `tests/fixtures/sp2/pubmed/pubmed_sample.xml.gz`

**Interfaces:**
- Consumes: `jats.py`, `article_schema` v1.4, `staging_writer.write_rows`, `checkpoint_markers`.
- Produces: `extract_pubmed(raw_dir, processed_dir, *, max_files, force, workers, verbose) -> {inputs,ok,failed,rows}`; `main(argv)` per §4.7 incl. `--report`.

- [ ] **Step 1: Build the fixture** — take ~3 `<PubmedArticle>` records from a real `pubmed26nNNNN.xml.gz` (or hand-write valid ones: one full abstract, one no-abstract, one structured-abstract-only), gzip to `tests/fixtures/sp2/pubmed/pubmed_sample.xml.gz`.

- [ ] **Step 2: Write the failing test**

```python
from pathlib import Path
from episteme.data.pubmed.extract_pubmed import extract_pubmed
from episteme.data import article_schema

FX = Path("tests/fixtures/sp2/pubmed")

def test_pubmed_extract_rows(tmp_path):
    res = extract_pubmed(FX, tmp_path)
    assert res["inputs"] == 1 and res["rows"] >= 1
    shards = list((tmp_path / "staging" / "pubmed").glob("*.parquet")) or \
             list((tmp_path / "staging" / "pubmed").glob("*.jsonl"))
    assert shards
    # read one row back
    import polars as pl
    df = pl.read_parquet(shards[0]) if shards[0].suffix == ".parquet" else pl.read_ndjson(shards[0])
    r = df.to_dicts()[0]
    assert r["source"] == "pubmed"
    assert r["pmid"]
    assert r["extract_status"] in article_schema.EXTRACT_STATUSES
    assert r["container_id"] is None

def test_pubmed_no_abstract_is_empty(tmp_path):
    res = extract_pubmed(FX, tmp_path)
    import polars as pl
    shards = list((tmp_path / "staging" / "pubmed").glob("*.*"))
    df = pl.read_parquet(shards[0]) if shards[0].suffix == ".parquet" else pl.read_ndjson(shards[0])
    statuses = set(df["extract_status"].to_list())
    assert "empty" in statuses  # the no-abstract fixture record
```

- [ ] **Step 3: Run — fails** (stub raises `NotImplementedError`).

- [ ] **Step 4: Implement** — model on `extract_pmc.py`. Streaming: `defusedxml.ElementTree.iterparse(gzip.open(path), events=("end",))`, act on `</PubmedArticle>`, `elem.clear()` after each. Per record → `parse_pubmed_record(elem) -> dict`: `pmid` from `<PMID>`, `title` from `<ArticleTitle>`, `abstract` from `<Abstract>` (join `<AbstractText>` incl. `@Label`), `journal` from `<Journal><Title>`, `year` from `<PubDate><Year>` (or `<MedlineDate>` first 4 digits, else `None`), `doi` from `<ArticleId IdType="doi">`, `mesh` = list of `<DescriptorName>`, `publication_types` = list of `<PublicationType>`, `language` from `<Language>`. `text = build_text(title, abstract)` (no body). `license`/`license_url` → PubMed has none → `license_raw=None`, `subset=subset_from_license(None)` → `open_metadata`. `decide_extract_status`. `build_row`. Batch → `write_rows(rows, processed_dir, source="pubmed", source_file=path.name)`. `mark_success`/`mark_failed` per input file. `write_run_manifest`. `--report` mode: collect rows in memory, print the field-shape table, write nothing.

- [ ] **Step 5: Run — test green.** `.venv/Scripts/python.exe -m pytest -q tests/data/test_extract_pubmed.py`.

- [ ] **Step 6: `--report` on the fixture + real end-to-end**

`.venv/Scripts/python.exe -m episteme.data.pubmed.extract_pubmed --raw-dir tests/fixtures/sp2/pubmed --report` → paste output into the task report.
Real: `PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed download --max-files 1` (one `pubmed26n0001.xml.gz`, ~19MB), then `PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed extract --max-files 20` → `... load ... graph`. Confirm `episteme_test.articles` has ~20 `source='pubmed'` rows with plausible `pmid`/`year`/`mesh`, `_audit` chain intact. `git clean -fdx 01_raw/pubmed`.

- [ ] **Step 7: Ledger sign-off** — record the `--report` table + `Ledger: pubmed field-shape signed-off — <controller> — <date>` (controller reviews the null-rates; if `authors`/`mesh`/`year` null-rate looks wrong vs real PubMed, fix before proceeding).

- [ ] **Step 8: `git rm` the old file + commit** — `git rm src/episteme/data/pubmed/extract.py`; commit `feat(sp2): pubmed extractor -> episteme.articles (reconciled to extract_pmc shape)`.

---

### Task 6: `apollo` — reconcile `extract_apollo.py`

Same shape as Task 5. Spec §3.2 `apollo` row. `git rm src/episteme/data/apollo/extract.py`. Fixture: ~3 ApolloCorpus docs (one English medical, one non-English, one boilerplate/short → `dropped`). The drop-heuristic (length + a keyword/medical-signal gate) is tuned at the `--report` sign-off. Real end-to-end: `apollo download` (hf_fetch — may pull a chunk; use `--max-files 1` semantics of `hf_corpus`), `apollo extract --max-files 20`, load, graph. Wrappers `scripts/data/apollo/{extract,load,graph}_apollo.sh`. Commit `feat(sp2): apollo extractor -> episteme.articles`.

---

### Task 7: `europepmc_preprint` — REST-harvest download + reconcile extractor

**Files:**
- Create: `src/episteme/data/europepmc/preprints/download_europepmc_preprints.py` (replace stub)
- Modify: `src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py` (`discover()` → per-ID `*.xml`; §4.7 shape)
- Modify: `scripts/data/europepmc/preprints/download_europepmc_preprint.sh` (→ `exec "$PY" -m`)
- Create: `scripts/data/europepmc/preprints/{extract,load,graph}_europepmc_preprint.sh`
- Test: `tests/data/test_extract_europepmc_preprint.py`, `tests/data/test_download_europepmc_preprints.py` (mock `requests`)

**Interfaces:**
- Produces: `download_preprints(raw_dir, *, max_files, since=None) -> {ids, fetched, skipped, errors}`; harvest-state file. Extractor: `extract_europepmc_preprints(...)` per §4.7.

- [ ] **Step 1: Failing test — download (mocked)**

```python
import responses  # or unittest.mock over requests
from episteme.data.europepmc.preprints.download_europepmc_preprints import download_preprints

def test_harvest_writes_per_id_xml(tmp_path, monkeypatch):
    # monkeypatch the pprid list fetch -> "PPR1\nPPR2\n", and GET /{id}/fullTextXML -> "<article/>"
    ...
    res = download_preprints(tmp_path, max_files=2)
    assert res["fetched"] == 2
    assert (tmp_path / "PPR1.xml").exists()
    # re-run: both skipped
    res2 = download_preprints(tmp_path, max_files=2)
    assert res2["skipped"] == 2 and res2["fetched"] == 0
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement `download_preprints`** — `curl`/`requests.get("{EUROPEPMC_PREPRINT_BASE}/pprid.txt.gz")` → gunzip → id list. For each id not on disk and within `max_files`: `requests.get(f"{get_settings().europepmc_base}/{id}/fullTextXML", timeout=30)`; on 200 write `raw_dir/{id}.xml`; on 429/503 backoff `2**n` (cap 60s, budget 5 retries); on 404 record error, skip. Append completed id to `raw_dir/.harvest_state`. `main(argv)` with `--raw-dir --max-files --since` (`--since` = ISO date; filter ids via a `?query=...` API param — if the API has no id-range filter, `--since` is accepted but `log WARN`-noop, documented). Reads NO `os.environ` — endpoint via `get_settings()`.

- [ ] **Step 4: Extractor `discover()`** — `sorted(raw_dir.glob("PPR*.xml"))` (drop the `*.xml.gz` / range globs). Align `main()` to §4.7 (`--processed-dir`, `--report`, return dict keys). Keep the per-XML → row logic.

- [ ] **Step 5: Wrapper flip** — `download_europepmc_preprint.sh` body becomes the `exec "$PY" -m episteme.data.europepmc.preprints.download_europepmc_preprints "$@"` shape. Add `{extract,load,graph}_europepmc_preprint.sh`.

- [ ] **Step 6: Run** — download + extract tests green; `bash -n` + `run_pipeline.sh europepmc_preprint download --dry-run` → rc 0 (the python module needs a `--dry-run` no-op: print "would harvest N ids from {base}", exit 0 — add it, mirrors `download_pmc.py`).

- [ ] **Step 7: `--report` + real end-to-end** — `europepmc_preprint download --max-files 5` (5 real preprints), `extract`, `load`, `graph`. Ledger sign-off.

- [ ] **Step 8: Commit** — `feat(sp2): europepmc_preprint REST harvest + extractor reconcile (EBI bulk feed discontinued)`

---

### Task 8: `europepmc_manuscript` — new extractor

**Files:**
- Create: `src/episteme/data/europepmc/manuscripts/extract_europepmc_manuscripts.py`
- Create: `scripts/data/europepmc/manuscripts/{extract,load,graph}_europepmc_manuscript.sh`
- Test: `tests/data/test_extract_europepmc_manuscript.py`, fixture `tests/fixtures/sp2/europepmc_manuscript/sample.tar.gz` (one `.txt` member + one `.xml` member)

**Interfaces:** `extract_europepmc_manuscripts(...)` per §4.7. Unit of work = one `.tar.gz`; iterate members.

- [ ] **Step 1: Fixture** — a 2-member tar: `PMC001.txt` (a few paragraphs of body text) + `PMC002.xml` (minimal JATS with `<body>`).
- [ ] **Step 2: Failing test** — `extract_europepmc_manuscripts(FX, tmp_path)` → `rows == 2`; both rows `source == "europepmc_manuscript"`, `is_manuscript is True`, `subset == "text_mining"`, `pmcid` set, `container_id is None`.
- [ ] **Step 3: Run — fails.**
- [ ] **Step 4: Implement** — `tarfile.open(path)`, for each member: `.txt` → `text = member_bytes.decode()`; `.xml` → `parse_jats_fields` on a temp-written file (or `ET.fromstring`). `pmcid` from `member.name` (regex `PMC\d+`). `license_raw = "text mining / applicable copyright"`; `subset = "text_mining"` (bypass `subset_from_license` — hardcode, with a comment). `is_manuscript = True`. `decide_extract_status`. `write_rows(..., source="europepmc_manuscript", source_file=path.name)`.
- [ ] **Step 5: Run — green.**
- [ ] **Step 6: `--report` + real end-to-end** — `europepmc_manuscript download --max-files 1` (one `author_manuscript_txt.PMC00Nxxxxxx.baseline.*.tar.gz` — these are large; if >200MB, hand-trim to a fixture-scale tar and note it), extract `--max-files 20`, load, graph. Ledger sign-off — **verify `subset=text_mining` on every row; a manuscript row must never land in a `commercial` corpus shard** (check `corpus_materializer`'s `WHERE a.subset = 'commercial'` filter excludes them).
- [ ] **Step 7: Commit** — `feat(sp2): europepmc_manuscript extractor (txt + JATS, text_mining subset)`

---

### Task 9: `guidelines` — new source (wrapper + extractor)

**Files:**
- Create: `scripts/data/guidelines/download_guidelines.sh`, `src/episteme/data/guidelines/{__init__,extract_guidelines}.py`, `scripts/data/guidelines/{extract,load,graph}_guidelines.sh`
- Modify: `scripts/data/run_pipeline.sh` (`[guidelines]` already added in Task 4 — verify)
- Test: `tests/data/test_extract_guidelines.py`, fixture `tests/fixtures/sp2/guidelines/sample.jsonl` (3 guideline docs)

**Interfaces:** `download_guidelines.sh` = thin `hf_fetch "$GUIDELINES_HF_REPO" dataset "$(resolve_dest guidelines)"` (add `GUIDELINES_HF_REPO=epfl-llm/guidelines` to `sources.env`). `extract_guidelines(...)` per §4.7.

- [ ] **Step 1: `sources.env` + wrapper** — append `GUIDELINES_HF_REPO=epfl-llm/guidelines` to `scripts/data/_lib/sources.env` under the Literature group. `download_guidelines.sh` mirrors `apollo/download_apollo.sh` (repo-pinned hf_fetch). `chmod +x` + track.
- [ ] **Step 2: Fixture + failing test** — 3 docs: `{title, text, source (issuing body), url}`. `extract_guidelines(FX, tmp_path)` → `rows == 3`; `source == "guidelines"`, `journal` = issuing body, `container_id is None`, `subset` per the licence decision (default `other` until sign-off).
- [ ] **Step 3: Run — fails.**
- [ ] **Step 4: Implement** — read the parquet/jsonl split(s) with polars lazy; **skip any split whose schema looks QA-shaped** (`question`/`answer`/`options` columns) — pretraining prose only. Per doc → `title`, `text` = body, `journal` = issuing body field, `year` if present. `license_raw` from the dataset card (Meditron guidelines: mixed — set `license_raw = "see epfl-llm/guidelines dataset card"`, `subset` decided at sign-off). `decide_extract_status`. `write_rows(..., source="guidelines", source_file=<split filename>)`.
- [ ] **Step 5: Run — green.**
- [ ] **Step 6: `--report` + real end-to-end** — `guidelines download` (hf_fetch the dataset — it's ~1GB; acceptable, or use HF's `--include` to grab one split), `extract --max-files 50`, load, graph. Ledger sign-off — **fix the `subset`/`license` mapping here** and re-run extract if it changes.
- [ ] **Step 7: Commit** — `feat(sp2): guidelines source — download wrapper + extractor (pretraining split)`

---

### Task 10: `bookshelf` — book + book-part extractor

**Files:**
- Modify: `src/episteme/data/bookshelf/extract_bookshelf.py` (fill the Task-4 minimal stub)
- Test: `tests/data/test_extract_bookshelf.py`, `tests/data/test_bookshelf_end_to_end.py` (`pg`), fixture `tests/fixtures/sp2/bookshelf/NBK1.tar.gz` (one small book NXML: `<book>` with `<book-meta>` incl. ISBN, `<toc>`, `<front>`, 2 `<book-part>` with `<body>`, 1 `<book-part>` that is index-only)

**Interfaces:** `extract_bookshelf(...)` per §4.7. Per `.tar.gz` → 1 book row + N part rows.

- [ ] **Step 1: Fixture** — hand-write `NBK1/NBK1.nxml` inside the tar: `<book><book-meta><isbn>...</isbn><contrib-group>...</contrib-group><publisher>...</publisher></book-meta><toc>...</toc><front><preface><p>...</p></preface></front><body><book-part id="p1"><book-part-meta><title>Chapter 1</title></book-part-meta><body><p>chapter one text long enough...</p></body></book-part><book-part id="p2">...</book-part><book-part id="idx"><index>...</index></book-part></body></book>`.
- [ ] **Step 2: Failing test**

```python
def test_bookshelf_book_plus_parts(tmp_path):
    from episteme.data.bookshelf.extract_bookshelf import extract_bookshelf
    res = extract_bookshelf(Path("tests/fixtures/sp2/bookshelf"), tmp_path)
    import polars as pl
    shards = list((tmp_path / "staging" / "bookshelf").glob("*.*"))
    rows = (pl.read_parquet(shards[0]) if shards[0].suffix==".parquet" else pl.read_ndjson(shards[0])).to_dicts()
    book = [r for r in rows if r["id"] == "bookshelf:NBK1"][0]
    parts = [r for r in rows if r["container_id"] == "bookshelf:NBK1"]
    assert book["container_id"] is None
    assert book["book_meta"] is not None and "isbn" in book["book_meta"]
    assert "Chapter 1" not in (book["text"] or "")   # book text is TOC+front only
    assert {p["id"] for p in parts} == {"bookshelf:NBK1:p1", "bookshelf:NBK1:p2"}  # idx skipped
    assert all(p["container_id"] == "bookshelf:NBK1" for p in parts)
```

- [ ] **Step 3: Run — fails** (stub returns 0 rows).
- [ ] **Step 4: Implement** — `tarfile` → find the `*.nxml`/`*.xml`. `parse_jats_fields`-style for `<book-meta>` → `book_meta = {isbn, editors (from <contrib-group>), publisher, edition}`. Book row: `id = f"bookshelf:{nbk}"`, `title` = `<book-title>`, `text = itertext(<toc>) + "\n\n" + itertext(<front>)`, `container_id = None`, `book_meta[...]`, `n_parts` filled after the part walk. Parts via `iter_book_parts(root)`: skip a part whose `<body>` is empty or whose only child is `<index>`/`<toc>` or whose title matches `^(copyright|index|table of contents)$` (case-insensitive); each kept part → `id = f"bookshelf:{nbk}:{bp.get('id')}"`, `title` = its `<title>`, `text = itertext(bp/<body>)`, `container_id = f"bookshelf:{nbk}"`. `decide_extract_status` (book row → the v1.4 book-row rule). `write_rows(rows, processed_dir, source="bookshelf", source_file=path.name)` (book + parts in one shard).
- [ ] **Step 5: Run — unit test green.**
- [ ] **Step 6: `pg` end-to-end** — `tests/data/test_bookshelf_end_to_end.py` (`pytestmark = pytest.mark.pg`): load the fixture shard via `load_articles`, then `graph_builder.build(source="bookshelf")`, assert `articles` has 1 book + 2 parts, `article_parts` has 2 rows, `neighbours("bookshelf:NBK1", kind="part")` == the 2 part ids. Run `set -a; . ./.env; set +a; pytest -q tests/data/test_bookshelf_end_to_end.py`.
- [ ] **Step 7: `--report` + real end-to-end** — `bookshelf download --max-files 1` (one real book `.tar.gz` — small, ~1–5MB), `extract`, `load`, `graph`, `corpus materialize`. Confirm the book row's TOC/front text is in the `03_corpus` shard. Ledger sign-off (`book_meta` key set — extend `BOOK_META_KEYS` if real NXML carries more).
- [ ] **Step 8: Commit** — `feat(sp2): bookshelf extractor — book row + container_id-linked part rows`

---

## Phase C — enrichment feeds

### Task 11: `europepmc/id_mappings` → `episteme.id_map`

**Files:**
- Create: `src/episteme/data/europepmc/id_mappings/load_id_mappings.py`, `scripts/data/europepmc/id_mappings/load_europepmc_id_mappings.sh`
- Modify: `scripts/data/run_pipeline.sh` (`europepmc_id_mappings load` path)
- Test: `tests/data/test_load_id_mappings.py` (`pg`), fixture `tests/fixtures/sp2/id_mappings/PMID_PMCID_DOI.csv.gz` (5 rows)

- [ ] **Step 1: Failing test** — `load_id_mappings(gz_path, conn)` → `episteme.id_map` gains 5 rows with matching `(pmid, pmcid, doi)`; a re-run upserts (no dupes; the unique index on `coalesce(...)` triples holds).
- [ ] **Step 2: Run — fails.**
- [ ] **Step 3: Implement** — **gzip integrity check first** (`docs/10` §5: `gzip.open(path).read(1)` in a try, or `zlib` CRC — reuse `is_valid_gzip` from `extract_europepmc_preprints.py` if present). Stream the CSV (`csv.reader` over `gzip.open(text mode)`), `COPY episteme.id_map (pmid, pmcid, doi) FROM STDIN` into a temp table then `INSERT ... ON CONFLICT DO NOTHING` (the unique index is on the coalesced triple — a plain `ON CONFLICT` needs the index columns; use `INSERT ... SELECT ... WHERE NOT EXISTS` if the coalesce-index can't back `ON CONFLICT`). `main(argv)` `--raw-dir --force`. No `os.environ`.
- [ ] **Step 4: Run — `pg` test green.**
- [ ] **Step 5: `run_pipeline.sh`** — `europepmc_id_mappings` + `load` → `run_stage load bash "$HERE/europepmc/id_mappings/load_europepmc_id_mappings.sh"`; `extract`/`graph`/`all` → `die "europepmc_id_mappings is an id-map feed, not an article source — use: run_pipeline.sh europepmc_id_mappings load" 3`.
- [ ] **Step 6: Real** — `europepmc_id_mappings download` (the 342MB `PMID_PMCID_DOI.csv.gz` — or `--max-files`… it's one file, so download it; if too big for the task, trim), `load`. Confirm `select count(*) from episteme.id_map`.
- [ ] **Step 7: Commit** — `feat(sp2): europepmc id_mappings -> episteme.id_map (gzip-checked COPY)`

---

### Task 12: `europepmc/lite_metadata` → enrichment

**Files:**
- Create: `src/episteme/data/europepmc/lite_metadata/enrich_from_lite.py`, `scripts/data/europepmc/lite_metadata/enrich_europepmc_lite.sh`
- Modify: `scripts/data/run_pipeline.sh` (`europepmc_lite enrich` path)
- Test: `tests/data/test_enrich_from_lite.py` (`pg`), fixture `tests/fixtures/sp2/lite_metadata/sample.tgz`

- [ ] **Step 1: Failing test** — seed `episteme_test.articles` with 2 rows that have `pmcid` set but `journal`/`year` NULL; `enrich_from_lite(tgz_path, conn)` fills them from the lite-metadata records keyed by pmcid; rows without a match are untouched; `extract_status` is NOT changed.
- [ ] **Step 2: Run — fails.**
- [ ] **Step 3: Implement** — read `PMCLiteMetadata.tgz` members (they're XML/CSV per-PMC lite records — inspect the real file shape during impl; the fixture mirrors it). Build `{pmcid: {journal, year, mesh}}`. `UPDATE episteme.articles SET journal = coalesce(journal, %s), year = coalesce(year, %s), mesh = CASE WHEN mesh IS NULL OR mesh = '{}' THEN %s ELSE mesh END WHERE pmcid = %s`. Batched. `main(argv)` `--raw-dir`. Report: how many rows matched / updated.
- [ ] **Step 4: Run — `pg` green.**
- [ ] **Step 5: `run_pipeline.sh`** — `europepmc_lite` + `enrich` → the wrapper; other stages → `die "... use: run_pipeline.sh europepmc_lite enrich" 3`. Add `enrich` to the write-stage `--dry-run` reject list (Task 4 covers it if `enrich` is in the `case`).
- [ ] **Step 6: Real** — `europepmc_lite download` (`PMCLiteMetadata.tgz` ~2.2GB — this one is genuinely large; the task may stop at "downloaded, enrich verified against a 100-record slice extracted from it" and note the full run is ops). `enrich`. Confirm N rows updated.
- [ ] **Step 7: Commit** — `feat(sp2): europepmc lite_metadata enrichment pass on episteme.articles`

---

## Phase D — close-out

### Task 13: full sweep + drift log

**Files:** Modify `docs/project-incubation-baseline.md`; touch nothing else except test fixtures if a gap is found.

- [ ] **Step 1: `not pg` suite** — `.venv/Scripts/python.exe -m pytest -q -m "not pg"` → all green; record the count (was 67; SP2 adds the per-extractor unit tests + `test_jats.py` + `test_migrations.py::test_schema_version_and_columns` + dispatch additions).
- [ ] **Step 2: `pg` suite** — `set -a; . ./.env; set +a; .venv/Scripts/python.exe -m pytest -q` → green; record (was 72/2; SP2 adds `test_migrations.py` ×3, `test_graph_builder` part, `test_bookshelf_end_to_end`, `test_load_id_mappings`, `test_enrich_from_lite`).
- [ ] **Step 3: `run_pipeline.sh` sweep** — for each of `pubmed apollo europepmc_manuscript europepmc_preprint guidelines bookshelf`: `bash scripts/data/run_pipeline.sh <s> extract --dry-run` → rc 3 (write-stage reject); `bash scripts/data/run_pipeline.sh <s> download --dry-run` → rc 0. `corpus materialize --dry-run` → rc 3. `europepmc_id_mappings extract` → rc 3 with the id-map message. Full transcript into the report.
- [ ] **Step 4: `bash -n` + shellcheck-predict** — every new/modified `.sh`; note any `# shellcheck disable` added.
- [ ] **Step 5: grep gate** — `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'` → empty (the new `GUIDELINES_HF_REPO` is in `sources.env`; the preprints REST base is `EUROPEPMC_BASE`/`EUROPEPMC_PREPRINT_BASE` from `sources.env`, not a literal in the python — verify).
- [ ] **Step 6: Drift log** — one dated entry in `docs/project-incubation-baseline.md` (match the SP1-β/SP3 entry style): SP2 landed (plan + spec paths), schema v1.4 (`container_id`/`book_meta`), `episteme.article_parts` + the `part_of` graph edge, `jats.py` extraction, the six sources with per-source row counts from the real end-to-ends + their field-shape sign-off dates, `id_map`/`lite_metadata` feeds, the `europepmc_preprint` REST-harvest pivot (EBI discontinued the bulk feed) + full-backfill-is-ops, the `guidelines` new source, deferred items (SP4 structured; `chunks`; `pgvector`; full preprint backfill; `decontaminate_benchmarks` real eval sets).
- [ ] **Step 7: Commit** — `docs(sp2): drift-log entry — literature layer landed`

---

## Self-Review

**Spec coverage (`2026-09-08-sp2-literature-extractors-design.md`):**

| Spec item | Task |
|---|---|
| §2.1 `article_schema` v1.4 (`container_id`, `book_meta`, `BOOK_META_KEYS`, book-row status) | 2 |
| §2.2 `migrations/0002` (columns, `articles_bookshelf*`, `article_body_bookshelf`, `article_parts` ×8 + unique idx, grants) | 2 |
| §2.2 PGQ edge def in `schema.sql` | 2 (Step 5) |
| §2.3 `graph_builder` third derivation + `neighbours(kind="part")` + `parts` count | 3 |
| §2.4 loader/materializer column pass-through | 4 |
| §3.1 shared `jats.py`; every extractor's `extract_<source>()` + §4.7 `main` + `--report` | 1, 5–10 |
| §3.2 pubmed | 5 |
| §3.2 apollo | 6 |
| §3.2 europepmc_manuscript (txt+xml, `text_mining`) | 8 |
| §3.2 europepmc_preprint (discover per-ID) | 7 |
| §3.2 guidelines (pretraining split only) | 9 |
| §3.2 bookshelf (book row + part rows) | 10 |
| §3.3 preprint REST-harvest download + wrapper flip | 7 |
| §4 `run_pipeline.sh` lit stage chain, `[guidelines]`, `--dry-run` write-stage reject, `corpus materialize` alias | 4 (+ per-source wrapper creation in 5–10) |
| §4 `id_mappings` load path | 11 |
| §4 `lite_metadata` enrich path | 12 |
| §5 unit tests per extractor, `test_migrations.py`, `test_jats.py`, bookshelf `pg` e2e | 1–3, 5–12 |
| §5 per-source field-shape `--report` + ledger sign-off + real end-to-end | 5–12 (each task's Steps 6–7) |
| §5 drift-log entry | 13 |
| §6 non-goals (SP4, SFT, chunks, pgvector, full backfill, docs) | not implemented — asserted in 13's drift entry |
| §7 open items 1–6 | carried in 13's drift entry |

**Placeholder scan:** The Phase-B tasks say "model on `extract_pmc.py`" + point at the spec §3.2 row rather than reprinting 300 lines — `extract_pmc.py` is a concrete, merged, tested template and the spec row is the exact per-source delta, same convention SP3's plan used with the batch scripts. Every task has real test code and the specific field/status/subset rules inline. Fixtures are described concretely (record counts + which edge cases). Line-count / size hedges ("if >200MB, hand-trim") are guidance, not gaps.

**Type consistency:** `extract_<source>(raw_dir: Path, processed_dir: Path, *, max_files: int = 0, force: bool = False, workers: int = 1, verbose: bool = False) -> dict` with keys `{"inputs","ok","failed","rows"}` — identical across Tasks 5–10 and matches `extract_pmc.extract_pmc`. `write_rows(rows, processed_dir, *, source, source_file)` — used identically. `graph_builder.build(conn, *, source, raw_dir, run_id) -> dict` gains `"parts"`; `neighbours(conn, id, hops=1, kind=...)` — `kind` extended, not changed. `container_id: str | None`, `book_meta: dict | None`. `article_parts(container_id text, part_id text, source_file text)`. `id = "bookshelf:<NBKid>"` / `"bookshelf:<NBKid>:<part-id>"` consistent Tasks 2/3/10.

**Risks:** (a) `corpus_materializer` includes book rows (TOC/front text) in the pretraining shard — deliberate (spec §2.4), flagged in the drift log; if a reviewer objects it's a one-line `WHERE` addition. (b) The three large enrichment/download files (`PMID_PMCID_DOI.csv.gz` 342MB, `PMCLiteMetadata.tgz` 2.2GB, pubmed baseline archives ~19MB each) — Tasks 11/12 explicitly allow stopping at "downloaded + verified against a trimmed slice" with the full run marked ops. (c) `iterparse` over `gzip.open` for pubmed — memory-safe only if `elem.clear()` (and clearing the root's processed children) is done per record; the test with a multi-record fixture is the canary. (d) `subset` for `guidelines` is unknown until the real dataset card is read at Task 9's sign-off — the task re-runs extract if it changes, so it's a gated unknown not a blocker.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-08-sp2-literature-extractors.md`.

**Subagent-Driven** (standing preference — no mode question): dispatch a fresh implementer per task, task review after each, broad whole-branch review at the end. `pg`-marked tests need `TEST_PG_DSN` from `.env` and the live PostgreSQL from SP1-β (both in place); a task that adds a `pg` test runs it against `episteme_test`.
