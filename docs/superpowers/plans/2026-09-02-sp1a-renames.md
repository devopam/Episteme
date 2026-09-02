# SP1-α — schema/ops/writer renames + PMC extract merge + DATA_ROOT — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the three shared data modules to their spelled-out names, wire `EPISTEME_DATA_ROOT` as the single relocatable storage knob, and replace the `extract_pmc.py` stub with the real (renamed-imports) logic from the upstream `extract.py` — all with `pytest -q` green throughout and no Postgres.

**Architecture:** Pure refactor + one config addition. `git mv` every module move so history follows. No behaviour change except the new `EPISTEME_DATA_ROOT` derivation. The Postgres storage core (`db/`, `audit_trail`, `postgres_loader`, `graph_builder`, `corpus_materializer`, `enrich`, proving slice) is **SP1-β**, a separate plan.

**Tech Stack:** Python 3.10+, pytest, ruff, `python-dotenv`. No database, no new dependency.

**Spec:** `docs/superpowers/specs/2026-09-02-sp1-storage-core.md` (SP1); conventions frozen in `docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md` §4.

## Global Constraints

- **Branch:** `sp1-storage-core` (already checked out).
- **`git mv`** for every module move — history must follow (`git log --follow` proves it).
- **Do NOT touch** anything Postgres-related — `db/`, `audit_trail.py` beyond leaving its stub, `postgres_loader`, etc. are SP1-β.
- **Naming:** `schema.py`→`article_schema.py`, `ops.py`→`checkpoint_markers.py`, `writer.py`→`staging_writer.py`. All under `src/episteme/data/`.
- **Config rule:** `src/episteme/config.py` stays the ONLY module reading `os.environ`.
- **`pytest -q` green after every task.** Current baseline: 19 passed (run it in Task 0 to confirm).
- **Python for everything:** `.venv/Scripts/python.exe`.
- **Commit trailers** — every commit message body ends with:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```

---

## File-structure map

**Moved (`git mv` + edited):**
- `src/episteme/data/schema.py` → `src/episteme/data/article_schema.py`
- `src/episteme/data/ops.py` → `src/episteme/data/checkpoint_markers.py`
- `src/episteme/data/writer.py` → `src/episteme/data/staging_writer.py`

**Modified:**
- `src/episteme/config.py` — add `EPISTEME_DATA_ROOT` + derived roots
- `src/episteme/data/apollo/extract.py`, `src/episteme/data/pmc/extract.py`, `src/episteme/data/pubmed/extract.py`, `src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py` — import-path updates
- `src/episteme/data/checkpoint_markers.py` — its own `from episteme.data.schema import …` line; add `load_success_marker_path` / `graph_success_marker_path`
- `src/episteme/data/staging_writer.py` — staging path root → `02_processed/staging/<source>/`
- `src/episteme/data/pmc/extract_pmc.py` — replace the `NotImplementedError` stub with the real logic
- `tests/test_config.py` — `EPISTEME_DATA_ROOT` cases
- `scripts/extract_pmc_oa_comm.sh` — module path (if it names `episteme.data.pmc.extract`)

**Created:**
- `tests/data/test_article_schema.py`, `tests/data/test_checkpoint_markers.py`, `tests/data/test_staging_writer.py`, `tests/data/test_extract_pmc.py`
- `tests/data/fixtures/pmc/PMCFIX0001.1.xml` (tiny JATS fixture)

**Deleted:**
- `src/episteme/data/pmc/extract.py` (its logic moves into `extract_pmc.py`)

---

## Task 0: Confirm baseline

**Files:** none.

- [ ] **Step 1: Confirm green + on the right branch**

Run:
```bash
git branch --show-current            # must print: sp1-storage-core
.venv/Scripts/python.exe -m pytest -q
```
Expected: `sp1-storage-core`; `19 passed` (1 torch warning is fine).

- [ ] **Step 2: Snapshot the current module surfaces** (reference for the renames)

Run:
```bash
grep -rn "from episteme.data.schema\|from episteme.data.ops\|from episteme.data.writer\|import episteme.data.schema\|import episteme.data.ops\|import episteme.data.writer" src/ tests/
```
Expected: matches in `data/apollo/extract.py`, `data/pmc/extract.py`, `data/pubmed/extract.py`, `data/europepmc/preprints/extract_europepmc_preprints.py`, `data/ops.py`, `data/writer.py`. Note them — every one gets updated in Tasks 2-4.

---

## Task 1: `EPISTEME_DATA_ROOT` in config.py

**Files:**
- Modify: `src/episteme/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings` gains `data_root: Path`. `raw_root` / `processed_root` / `corpus_root` still exist and are still `Path`, but now default to `<data_root>/01_raw` | `/02_processed` | `/03_corpus` when their own env var is unset.

- [ ] **Step 1: Write the failing tests**

In `tests/test_config.py`, add (after the existing tests, using the existing `fresh_config` fixture):

```python
def test_data_root_derives_the_three_roots(fresh_config):
    cfg = fresh_config("EPISTEME_DATA_ROOT=/mnt/ssd\n")
    s = cfg.get_settings()
    assert s.data_root == Path("/mnt/ssd")
    assert s.raw_root == Path("/mnt/ssd/01_raw")
    assert s.processed_root == Path("/mnt/ssd/02_processed")
    assert s.corpus_root == Path("/mnt/ssd/03_corpus")


def test_explicit_root_overrides_data_root(fresh_config):
    cfg = fresh_config(
        "EPISTEME_DATA_ROOT=/mnt/ssd\n"
        "EPISTEME_RAW_ROOT=/other/raw\n"
    )
    s = cfg.get_settings()
    assert s.raw_root == Path("/other/raw")            # explicit wins
    assert s.processed_root == Path("/mnt/ssd/02_processed")   # derived


def test_data_root_defaults_to_dot(fresh_config):
    cfg = fresh_config("")
    s = cfg.get_settings()
    assert s.data_root == Path(".")
    assert s.raw_root == Path("01_raw")               # Path(".") / "01_raw"
```

- [ ] **Step 2: Run them, watch them fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_config.py -k data_root`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'data_root'` (and the derived-path asserts fail).

- [ ] **Step 3: Implement in `config.py`**

In the `Settings` dataclass, add `data_root: Path` as the first field. In `get_settings()`, before building `Settings(...)`:

```python
    data_root = Path(_get("EPISTEME_DATA_ROOT", "."))
    raw_root = Path(_get("EPISTEME_RAW_ROOT") or (data_root / "01_raw"))
    processed_root = Path(_get("EPISTEME_PROCESSED_ROOT") or (data_root / "02_processed"))
    corpus_root = Path(_get("EPISTEME_CORPUS_ROOT") or (data_root / "03_corpus"))
```

and pass `data_root=data_root, raw_root=raw_root, processed_root=processed_root,
corpus_root=corpus_root` into `Settings(...)` (replacing the current three
`Path(_get("EPISTEME_RAW_ROOT", "./01_raw"))` lines). `_get` already returns `None` for
unset/empty, so `_get(...) or (data_root / ...)` gives the derivation.

- [ ] **Step 4: Run the config tests**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_config.py`
Expected: all pass (the 3 new + the pre-existing ones).

- [ ] **Step 5: Full suite + commit**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `22 passed`.

```bash
git add src/episteme/config.py tests/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): single relocatable EPISTEME_DATA_ROOT

raw/processed/corpus roots derive from EPISTEME_DATA_ROOT (default ".")
unless individually overridden by EPISTEME_RAW_ROOT / _PROCESSED_ROOT /
_CORPUS_ROOT. Moving the data tree to an external disk is now one line.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 2: `schema.py` → `article_schema.py`

**Files:**
- Move: `src/episteme/data/schema.py` → `src/episteme/data/article_schema.py`
- Modify: import sites (see below); `src/episteme/data/article_schema.py` body
- Create: `tests/data/test_article_schema.py`

**Interfaces:**
- Produces: `episteme.data.article_schema` with everything `schema.py` exported
  (`SCHEMA_VERSION`, `MIN_OK_TEXT_LEN`, `SOURCES`, `SUBSETS`, `EXTRACT_STATUSES`,
  `ARTICLE_COLUMNS`, `utc_now_iso`, `normalize_whitespace`, `build_text`, `content_hash`,
  `decide_extract_status`, `normalize_license`, `subset_from_license`, `empty_article_row`,
  `finalize_row`) — names unchanged.

- [ ] **Step 1: Move with history**

```bash
git mv src/episteme/data/schema.py src/episteme/data/article_schema.py
```

- [ ] **Step 2: Update import sites**

In each of these files change `from episteme.data.schema import …` → `from episteme.data.article_schema import …`:
- `src/episteme/data/ops.py`
- `src/episteme/data/writer.py`
- `src/episteme/data/apollo/extract.py`
- `src/episteme/data/pmc/extract.py`
- `src/episteme/data/pubmed/extract.py`
- `src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py`

Run `grep -rn "episteme.data.schema" src/ tests/` afterwards — expected: **no matches**.

- [ ] **Step 3: Extend `SOURCES` + add a changelog block**

In `src/episteme/data/article_schema.py`:
- Replace the `SOURCES = (...)` tuple with the roadmap §4.2 list:
  ```python
  SOURCES = (
      "pubmed", "pmc", "bookshelf",
      "europepmc_preprint", "europepmc_manuscript", "europepmc_lite",
      "apollo", "guidelines",
      "chembl", "uniprot", "pubchem", "clinvar", "reactome", "mesh", "ontologies", "openalex",
  )
  ```
- Bump `SCHEMA_VERSION = "1.1"` → `SCHEMA_VERSION = "1.2"` and add, directly under it:
  ```python
  # Schema changelog:
  #   1.1  contract v1.1 (2026-08-31 sample audit): PMC provenance cols, license norm, status rules.
  #   1.2  (SP1-α, 2026-09-02): SOURCES extended to the full Phase-0 roadmap list.
  #        The row shape stays PROVISIONAL — refined per-source against real data before full load
  #        (roadmap §4.2). Bump this + append a line on every refinement.
  ```

- [ ] **Step 4: Write `tests/data/test_article_schema.py`**

```python
from episteme.data.article_schema import (
    SOURCES,
    build_text,
    decide_extract_status,
    normalize_license,
    subset_from_license,
    MIN_OK_TEXT_LEN,
)


def test_sources_covers_roadmap_phase0_set():
    for s in ("pubmed", "pmc", "bookshelf", "apollo", "chembl", "uniprot", "mesh", "openalex"):
        assert s in SOURCES


def test_build_text_joins_present_parts_only():
    assert build_text("T", None, "B") == "T\n\nB"
    assert build_text(None, "A", None) == "A"
    assert build_text("", "", "") == ""


def test_extract_status_ok_when_abstract_present_even_if_text_short():
    status, notes = decide_extract_status(text="short", abstract="a real abstract", body_text=None, has_id=True)
    assert status == "ok"


def test_extract_status_partial_for_title_only_short_text():
    status, notes = decide_extract_status(text="x" * (MIN_OK_TEXT_LEN - 1), abstract=None, body_text=None, has_id=True)
    assert status == "partial"


def test_extract_status_empty_when_id_but_no_content():
    status, notes = decide_extract_status(text="", abstract=None, body_text=None, has_id=True)
    assert status == "empty"


def test_normalize_license_cc_by_nc_is_not_commercial():
    code, url, raw = normalize_license("This article is CC BY-NC 4.0")
    assert code == "CC BY-NC"
    assert subset_from_license(code) == "text_mining"


def test_normalize_license_cc0_is_commercial():
    code, _, _ = normalize_license("CC0 1.0 Universal public domain dedication")
    assert code == "CC0"
    assert subset_from_license(code) == "commercial"
```

> If any assert mismatches the real helper behaviour, read `article_schema.py` and adjust
> the assert to what the code actually does — do not change the helper. These lock current
> behaviour ahead of SP1-β.

- [ ] **Step 5: Run + full suite + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q tests/data/test_article_schema.py
.venv/Scripts/python.exe -m pytest -q
```
Expected: 7 passed for the new file; `29 passed` total.

```bash
git add -A src/episteme/data tests/data/test_article_schema.py
git commit -m "$(cat <<'EOF'
refactor(data): schema.py -> article_schema.py; extend SOURCES

git mv preserves history. SOURCES grows to the full Phase-0 roadmap list;
SCHEMA_VERSION 1.1 -> 1.2 with a changelog block noting the row shape is
provisional (refined per-source against real data). Import sites updated.
New test_article_schema.py locks current license/status behaviour.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 3: `ops.py` → `checkpoint_markers.py`

**Files:**
- Move: `src/episteme/data/ops.py` → `src/episteme/data/checkpoint_markers.py`
- Modify: import sites; body (add two marker-path helpers)
- Create: `tests/data/test_checkpoint_markers.py`

**Interfaces:**
- Consumes: `episteme.data.article_schema` (updated in Task 2).
- Produces: `episteme.data.checkpoint_markers` with all of `ops.py`'s functions
  (`ops_root`, `success_marker_path`, `failed_marker_path`, `is_success`, `mark_success`,
  `mark_failed`, `write_run_manifest`, `list_input_files`) **plus**:
  - `load_success_marker_path(processed_root, source, input_basename) -> Path`
    → `_ops/<source>/load_success/<basename>.ok`
  - `graph_success_marker_path(processed_root, source, input_basename) -> Path`
    → `_ops/<source>/graph_success/<basename>.ok`

- [ ] **Step 1: Move with history**

```bash
git mv src/episteme/data/ops.py src/episteme/data/checkpoint_markers.py
```

- [ ] **Step 2: Update import sites**

- `src/episteme/data/apollo/extract.py`, `src/episteme/data/pmc/extract.py`,
  `src/episteme/data/pubmed/extract.py`,
  `src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py`:
  `from episteme.data.ops import …` → `from episteme.data.checkpoint_markers import …`
- `grep -rn "episteme.data.ops" src/ tests/` → expected: **no matches**.

- [ ] **Step 3: Add the two marker-path helpers**

In `src/episteme/data/checkpoint_markers.py`, after `failed_marker_path`:

```python
def load_success_marker_path(processed_root: Path, source: str, input_basename: str) -> Path:
    return ops_root(processed_root, source) / "load_success" / f"{input_basename}.ok"


def graph_success_marker_path(processed_root: Path, source: str, input_basename: str) -> Path:
    return ops_root(processed_root, source) / "graph_success" / f"{input_basename}.ok"
```

- [ ] **Step 4: Write `tests/data/test_checkpoint_markers.py`**

```python
import json
from pathlib import Path

from episteme.data.checkpoint_markers import (
    is_success,
    mark_success,
    mark_failed,
    success_marker_path,
    failed_marker_path,
    load_success_marker_path,
    graph_success_marker_path,
)


def test_mark_success_writes_marker_and_clears_failure(tmp_path):
    root = tmp_path / "02_processed"
    mark_failed(root, "pmc", "PMC1.1.xml", error_class="io_error", message="boom")
    assert failed_marker_path(root, "pmc", "PMC1.1.xml").is_file()
    mark_success(root, "pmc", "PMC1.1.xml", stats={"rows": 3})
    assert is_success(root, "pmc", "PMC1.1.xml")
    assert not failed_marker_path(root, "pmc", "PMC1.1.xml").is_file()
    payload = json.loads(success_marker_path(root, "pmc", "PMC1.1.xml").read_text())
    assert payload["stats"] == {"rows": 3}


def test_is_success_false_when_absent(tmp_path):
    assert not is_success(tmp_path / "02_processed", "pmc", "nope.xml")


def test_marker_path_layout(tmp_path):
    root = tmp_path / "02_processed"
    assert success_marker_path(root, "pmc", "f").parent.name == "success"
    assert load_success_marker_path(root, "pmc", "f").parent.name == "load_success"
    assert graph_success_marker_path(root, "pmc", "f").parent.name == "graph_success"
    assert load_success_marker_path(root, "pmc", "f").parts[-3] == "pmc"
```

- [ ] **Step 5: Run + full suite + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q tests/data/test_checkpoint_markers.py
.venv/Scripts/python.exe -m pytest -q
```
Expected: 3 passed for the new file; `32 passed` total.

```bash
git add -A src/episteme/data tests/data/test_checkpoint_markers.py
git commit -m "$(cat <<'EOF'
refactor(data): ops.py -> checkpoint_markers.py; add load/graph markers

git mv preserves history. Adds load_success_marker_path /
graph_success_marker_path for the SP1-β load and graph stages. Import
sites updated.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 4: `writer.py` → `staging_writer.py`

**Files:**
- Move: `src/episteme/data/writer.py` → `src/episteme/data/staging_writer.py`
- Modify: import sites; the output path root
- Create: `tests/data/test_staging_writer.py`

**Interfaces:**
- Consumes: `episteme.data.article_schema.ARTICLE_COLUMNS`.
- Produces: `episteme.data.staging_writer` with `write_rows`, `write_parquet_shard`,
  `write_jsonl_shard`, `rows_to_columnar` (names unchanged). Parquet/JSONL shards now write
  under `<warehouse_root>/staging/<source>/…` — see Step 3.

- [ ] **Step 1: Move with history**

```bash
git mv src/episteme/data/writer.py src/episteme/data/staging_writer.py
```

- [ ] **Step 2: Update import sites**

- `src/episteme/data/apollo/extract.py`, `src/episteme/data/pmc/extract.py`,
  `src/episteme/data/pubmed/extract.py`,
  `src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py`:
  `from episteme.data.writer import write_rows` → `from episteme.data.staging_writer import write_rows`
- `grep -rn "episteme.data.writer" src/ tests/` → **no matches**.

- [ ] **Step 3: Re-root the shard path to `staging/<source>/`**

In `staging_writer.py`, `write_parquet_shard` currently builds
`base = Path(warehouse_root) / "episteme" / "articles" / "data"` and
`write_jsonl_shard` builds `… / "episteme" / "articles" / "jsonl" / f"source={source}"`.
Change both to write under a **staging** tree keyed by source:
- `write_parquet_shard`: `base = Path(warehouse_root) / "staging" / source`
  and drop the `source=`/`year=` Hive partition dirs for staging — one shard per input file:
  `out_path = base / f"{stem}.parquet"` (keep the `stem` sanitisation). Remove the
  `by_year` grouping loop; write a single table for all rows of the file.
- `write_jsonl_shard`: `base = Path(warehouse_root) / "staging" / source`;
  `out_path = base / f"{stem}.jsonl"`.
- `write_rows` unchanged in signature; it just calls the two.

> Rationale: staging is transient, one shard per unit-of-work; the Hive `source=/year=`
> layout belongs to `03_corpus` (SP1-β's `corpus_materializer`), not staging.

- [ ] **Step 4: Write `tests/data/test_staging_writer.py`**

```python
import json
from pathlib import Path

from episteme.data.staging_writer import write_rows
from episteme.data.article_schema import empty_article_row


def _row(**kw):
    r = empty_article_row()
    r.update(kw)
    return r


def test_write_rows_parquet_one_shard_per_file(tmp_path):
    rows = [_row(id="pmc:PMC1", source="pmc", text="hello world", year=2024),
            _row(id="pmc:PMC2", source="pmc", text="another", year=None)]
    res = write_rows(rows, tmp_path, source="pmc", source_file="PMC_batch_01.xml")
    assert res["format"] in ("parquet", "jsonl")
    assert res["n_rows"] == 2
    out = Path(res["paths"][0])
    assert out.parent == tmp_path / "staging" / "pmc"
    assert "PMC_batch_01" in out.name


def test_write_rows_jsonl_fallback_roundtrips(tmp_path, monkeypatch):
    import episteme.data.staging_writer as sw
    monkeypatch.setattr(sw, "write_parquet_shard", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no pyarrow")))
    rows = [_row(id="pmc:PMC1", source="pmc", text="hi")]
    res = write_rows(rows, tmp_path, source="pmc", source_file="f.xml")
    assert res["format"] == "jsonl"
    line = json.loads(Path(res["paths"][0]).read_text().splitlines()[0])
    assert line["id"] == "pmc:PMC1"
```

- [ ] **Step 5: Run + full suite + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q tests/data/test_staging_writer.py
.venv/Scripts/python.exe -m pytest -q
```
Expected: 2 passed for the new file; `34 passed` total.

```bash
git add -A src/episteme/data tests/data/test_staging_writer.py
git commit -m "$(cat <<'EOF'
refactor(data): writer.py -> staging_writer.py; shard under staging/<source>/

git mv preserves history. Staging is transient, one Parquet/JSONL shard
per input file under 02_processed/staging/<source>/ — the Hive source=/
year= layout moves to 03_corpus (SP1-beta). Import sites updated.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 5: PMC extract merge — real `extract_pmc.py`

**Files:**
- Modify: `src/episteme/data/pmc/extract_pmc.py` (replace stub)
- Delete: `src/episteme/data/pmc/extract.py`
- Create: `tests/data/test_extract_pmc.py`, `tests/data/fixtures/pmc/PMCFIX0001.1.xml`
- Modify: `scripts/extract_pmc_oa_comm.sh` (if it names the module)

**Interfaces:**
- Consumes: `article_schema`, `checkpoint_markers`, `staging_writer` (renamed).
- Produces: `episteme.data.pmc.extract_pmc` with `main()` and a callable core
  `extract_pmc(raw_dir: Path, processed_dir: Path, *, max_files: int = 0, force: bool = False,
  workers: int = 1) -> dict` returning `{"inputs": n, "ok": n, "failed": n, "rows": n}`.
  Per-input-file: parse the PMC JATS XML → `article_schema` rows → `staging_writer.write_rows`
  → `checkpoint_markers.mark_success`/`mark_failed`. `audit_trail.record` calls are present
  but SP1-β makes them real; here import `from episteme.audit_trail import record as _audit`
  and call it — the current stub raises `NotImplementedError`, so **wrap each call in
  `try/except NotImplementedError: pass`** with a `# SP1-β: audit becomes mandatory` comment.

- [ ] **Step 1: Read the upstream logic**

Read `src/episteme/data/pmc/extract.py` in full. It is the real PMC JATS extractor (from the
upstream pull). Note its function names, how it enumerates input files, how it maps JATS →
row fields, and its CLI args. The merge preserves that logic; only the imports, the CLI flag
names (roadmap §4.7: `--raw-dir --processed-dir --max-files [--force] [--workers]`), and the
module docstring change.

- [ ] **Step 2: Create the JATS fixture**

`tests/data/fixtures/pmc/PMCFIX0001.1.xml` — a minimal but real-shaped JATS article:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="research-article">
  <front>
    <journal-meta><journal-title-group><journal-title>Test Journal</journal-title></journal-title-group></journal-meta>
    <article-meta>
      <article-id pub-id-type="pmc">PMCFIX0001</article-id>
      <article-id pub-id-type="pmid">40000001</article-id>
      <article-id pub-id-type="doi">10.0000/fix.0001</article-id>
      <article-categories>
        <subj-group subj-group-type="heading"><subject>Research Article</subject></subj-group>
      </article-categories>
      <title-group><article-title>A fixture article about acetylcholinesterase</article-title></title-group>
      <contrib-group>
        <contrib contrib-type="author"><name><surname>Doe</surname><given-names>Jane</given-names></name></contrib>
      </contrib-group>
      <pub-date pub-type="epub"><year>2024</year></pub-date>
      <permissions>
        <license xlink:href="https://creativecommons.org/licenses/by/4.0/">
          <license-p>This is an open access article under the CC BY 4.0 license.</license-p>
        </license>
      </permissions>
      <abstract><p>This fixture abstract is long enough to exceed the minimum ok text length so that the extractor marks it ok rather than partial, and it mentions acetylcholinesterase inhibition for the decontamination path.</p></abstract>
      <kwd-group><kwd>Acetylcholinesterase</kwd><kwd>Fixture</kwd></kwd-group>
    </article-meta>
  </front>
  <body><sec><title>Introduction</title><p>Body text of the fixture article.</p></sec></body>
</article>
```

Adjust element paths only if the upstream extractor reads different ones (Step 1 tells you).
Also add a JSON metadata sidecar if the extractor expects one (match the shape in
`01_raw/pmc/oa_comm/metadata/PMC13525906.1.json`) at
`tests/data/fixtures/pmc/PMCFIX0001.1.json`.

- [ ] **Step 3: Merge the logic into `extract_pmc.py`**

Replace the `extract_pmc.py` stub body with the upstream `extract.py` logic, edited for:
- module docstring → PMC extract, roadmap CLI;
- imports → `episteme.data.article_schema`, `episteme.data.checkpoint_markers`,
  `episteme.data.staging_writer`, and the guarded `_audit` (per Interfaces);
- `argparse` → `--raw-dir --processed-dir --max-files [--force] [--workers]`; read anything
  else (`EPISTEME_SAMPLE_LIMIT` etc.) via `episteme.config.get_settings()`;
- expose `extract_pmc(...)` as the importable core, `main()` as the CLI wrapper.

- [ ] **Step 4: Delete the old module + fix the script**

```bash
git rm src/episteme/data/pmc/extract.py
grep -rn "episteme.data.pmc.extract\b" scripts/ src/ tests/
```
If `scripts/extract_pmc_oa_comm.sh` (or `scripts/extract_pmc_oa_comm.sh`) runs
`python -m episteme.data.pmc.extract`, change it to `python -m episteme.data.pmc.extract_pmc`.

- [ ] **Step 5: Write `tests/data/test_extract_pmc.py`**

```python
from pathlib import Path

from episteme.data.pmc.extract_pmc import extract_pmc

FIX = Path(__file__).parent / "fixtures" / "pmc"


def test_extract_pmc_produces_one_ok_row(tmp_path):
    processed = tmp_path / "02_processed"
    res = extract_pmc(FIX, processed, max_files=1)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] >= 1
    # success marker written
    marks = list((processed / "_ops" / "pmc" / "success").glob("*.ok"))
    assert marks
    # staging shard written
    shards = list((processed / "staging" / "pmc").glob("*"))
    assert shards


def test_extract_pmc_row_fields(tmp_path):
    import json
    processed = tmp_path / "02_processed"
    extract_pmc(FIX, processed, max_files=1)
    shard = next((processed / "staging" / "pmc").glob("*"))
    # read the shard back (parquet or jsonl)
    if shard.suffix == ".parquet":
        import pyarrow.parquet as pq
        rows = pq.read_table(shard).to_pylist()
    else:
        rows = [json.loads(l) for l in shard.read_text().splitlines()]
    r = rows[0]
    assert r["source"] == "pmc"
    assert r["pmcid"] in ("PMC13525906", "PMCFIX0001") or r["pmcid"].startswith("PMC")
    assert "acetylcholinesterase" in (r["text"] or "").lower()
    assert r["license"] == "CC BY"
    assert r["subset"] == "commercial"
    assert r["extract_status"] == "ok"
```

> If the upstream extractor's field mapping differs (e.g. `id` format, how MeSH/kwds land),
> adjust the asserts to the real output after reading Step 1's notes — the intent is: one
> `ok` row, `source=pmc`, CC BY → commercial, text contains the abstract.

- [ ] **Step 6: Run + full suite + commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q tests/data/test_extract_pmc.py
.venv/Scripts/python.exe -m pytest -q
```
Expected: 2 passed for the new file; `36 passed` total.

```bash
git add -A src/episteme/data/pmc tests/data/ scripts/
git commit -m "$(cat <<'EOF'
feat(pmc): real extract_pmc.py from the upstream JATS extractor

Replaces the NotImplementedError stub with the upstream extract.py logic,
re-imported against article_schema / checkpoint_markers / staging_writer,
roadmap CLI (--raw-dir --processed-dir --max-files [--force --workers]),
exposing extract_pmc() as the importable core. audit_trail hooks present
but guarded (real in SP1-beta). Old extract.py removed; JATS fixture +
tests added.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 6: Sweep + baseline note

**Files:**
- Modify: `docs/project-incubation-baseline.md` (drift-log line)

- [ ] **Step 1: Full import sweep**

Run:
```bash
.venv/Scripts/python.exe -c "import importlib, pkgutil, episteme; [importlib.import_module(m.name) for m in pkgutil.walk_packages(episteme.__path__, 'episteme.')]; print('IMPORT SWEEP OK')"
grep -rn "episteme.data.schema\|episteme.data.ops\|episteme.data.writer\|data.pmc.extract\b" src/ scripts/ tests/
```
Expected: `IMPORT SWEEP OK`; grep returns **nothing**.

- [ ] **Step 2: `git log --follow` on the three renames**

```bash
for f in article_schema checkpoint_markers staging_writer; do
  echo "== $f =="; git log --follow --oneline -- "src/episteme/data/$f.py" | head -3
done
```
Expected: each shows history predating this branch (the old `schema.py`/`ops.py`/`writer.py` commits).

- [ ] **Step 3: ruff on the touched Python**

Run: `.venv/Scripts/python.exe -m ruff check src/episteme/config.py src/episteme/data/article_schema.py src/episteme/data/checkpoint_markers.py src/episteme/data/staging_writer.py src/episteme/data/pmc/extract_pmc.py tests/data/`
Expected: clean on these (pre-existing debt elsewhere is out of scope).

- [ ] **Step 4: Baseline drift-log + commit**

Append under `docs/project-incubation-baseline.md` "## Drift log":
```markdown
- 2026-09-02: SP1-α (spec `docs/superpowers/specs/2026-09-02-sp1-storage-core.md`).
  `schema/ops/writer` → `article_schema/checkpoint_markers/staging_writer`; single
  relocatable `EPISTEME_DATA_ROOT`; real `extract_pmc.py` from the upstream JATS extractor.
  No database yet — that's SP1-β.
```

```bash
git add docs/project-incubation-baseline.md
git commit -m "$(cat <<'EOF'
docs: baseline drift-log for SP1-alpha

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

- [ ] **Step 5: Final gate**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `36 passed`.

---

## Self-Review

**Spec coverage (SP1-α slice of `2026-09-02-sp1-storage-core.md` §3 + §4.1–4.2):**

| Spec item | Task |
|---|---|
| `schema.py`→`article_schema.py` + `SCHEMA_VERSION` changelog + extend `SOURCES` | 2 |
| `ops.py`→`checkpoint_markers.py` + `load_success`/`graph_success` paths | 3 |
| `writer.py`→`staging_writer.py` + staging path root | 4 |
| import-site fixes (apollo/pmc/pubmed/europepmc extract) | 2, 3, 4 |
| `config.py` `EPISTEME_DATA_ROOT` single relocatable root + test | 1 |
| merge upstream `pmc/extract.py` → `extract_pmc.py`; delete old; script fix | 5 |
| `sample_audit.py` | **deferred to SP1-β plan** — it needs staging fixtures from a full extract run and its exit-criterion use (the field-shape report) is part of the β proving slice. Noted in SP1-β plan scope. |
| `pytest -q` green throughout | 0 baseline, held green through 6 |
| history preserved | 6 (`git log --follow`) |

**Placeholder scan:** the `try/except NotImplementedError` around `audit_trail.record` in Task 5
is an explicit, commented bridge (the stub raises until SP1-β), not a plan placeholder. All
test code and edits are spelled out. Task 5 Steps 1/2/5 carry "read the upstream extractor
and adjust asserts to real output" — a verification instruction (the upstream `extract.py`
was not read line-by-line while writing this plan), not a TODO.

**Type consistency:** `Settings.data_root: Path` (Task 1) is only referenced in Task 1.
`extract_pmc(raw_dir, processed_dir, *, max_files, force, workers) -> dict` (Task 5 Interfaces)
matches the test calls in Task 5 Step 5. `load_success_marker_path` / `graph_success_marker_path`
(Task 3) are consumed only in SP1-β. Renamed-module import paths are identical across Tasks
2–6.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-02-sp1a-renames.md`.

Per the standing preference, execution is **subagent-driven** (superpowers:subagent-driven-development) — a fresh implementer per task, task review after each, whole-branch review at the end. No mode question.
