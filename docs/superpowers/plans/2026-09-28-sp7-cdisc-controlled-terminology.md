# SP7 CDISC Controlled Terminology Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add CDISC Controlled Terminology (NCI EVS) as a structured Stream 1 source — download, serialize into one row per codelist per package, load, and keep only the latest release per package in the corpus.

**Architecture:** A bash download wrapper (per-package dated folders, `Content-Type` check), a Python serializer following the SP4 structured-serializer contract (pure parser/row-builder functions plus the standard driver), and a load wrapper that runs the normal loader then a new reusable `postgres_loader.retire_source_files` step. Only Task 4 touches the database.

**Tech Stack:** Python 3.10+, pytest, polars/pyarrow (existing staging writer), bash (Git Bash on Windows; Ubuntu in the cloud sandbox), PostgreSQL 19beta3 (local only).

**Spec:** `docs/superpowers/specs/2026-09-28-sp7-cdisc-controlled-terminology-design.md`

## Global Constraints

- `SOURCE = "cdisc_ct"`; packages in fixed order: `SDTM`, `SEND`, `ADaM`, `Define-XML`, `Protocol`.
- Raw layout: `01_raw/cdisc_ct/<Package>/<YYYY-MM-DD>/<Package>_Terminology.txt` (release date from `Last-Modified`; no spaces in local names).
- Row id: `f"cdisc_ct:{package}:{codelist_code}:p{n}"` (always with part number); `source_record_id = f"{package}:{codelist_code}:p{n}"`.
- Parts: at most **200 terms** per row; every part repeats the codelist header.
- Licence: `license = LICENSE_PUBLIC_DOMAIN` (governance override after `normalize_license`, user decision 2026-09-28); `license_raw` = `"NCI EVS: CDISC Terminology is free to use without licensing restrictions."`; subset via `subset_from_license` (→ `commercial`).
- Expected header (exact, tab-separated): `Code`, `Codelist Code`, `Codelist Extensible (Yes/No)`, `Codelist Name`, `CDISC Submission Value`, `CDISC Synonym(s)`, `CDISC Definition`, `NCI Preferred Term`.
- Parse with `csv.reader(..., delimiter="\t", quoting=csv.QUOTE_NONE)` — NCI text contains literal double quotes.
- Frozen serializer contract: `serialize_cdisc_ct(raw_dir, processed_dir, *, max_files=0, force=False, workers=1, verbose=False) -> {"inputs","ok","failed","rows"}`; audit event `serialize_commit`.
- `config.py` is the only `os.environ` reader; endpoint URL only in `scripts/data/_lib/sources.env`; grep gate `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'` stays empty.
- Never read, print or copy `.env`; never `env | grep`. DB commands (Task 4, Task 6 only) prefix `PGDATABASE=episteme_test` and confirm `select current_setting('server_version')` = `19beta3`; pg tests need `.env` sourced via `set -a; . ./.env; set +a`.
- `git add` explicit paths; never `--no-verify`; `ruff format` + `ruff check` on changed Python; `bash -n` on changed shell; new `.sh` files mode 100755 (`git update-index --chmod=+x`).
- Commit trailer:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```

## Review Focus

1. **Literal double quotes inside definitions** (common in NCI text) must not merge rows or drop columns — pinned in Task 1 (`QUOTE_NONE`).
2. **Truncated or non-CT file** (wrong header, a row with the wrong column count from a partial download) must fail that file via `mark_failed`, never write partial rows — pinned in Task 1 (`CTFormatError`) and Task 2.
3. **Term rows that appear before their codelist header** must still attach (the file is read fully before assembling) — pinned in Task 1.
4. **Retire step with nothing loaded yet** must delete nothing, not "everything not in an empty keep-set" — pinned in Task 4.
5. **A package missing on the server** (HTML fallback, HTTP 200) must be skipped with a warning while the other packages still download — pinned in Task 3.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/episteme/data/cdisc_ct/__init__.py` | package marker |
| `src/episteme/data/cdisc_ct/ct_parse.py` | pure functions: parse a CT text file, assemble codelists, build row dicts (no I/O beyond reading the file) |
| `src/episteme/data/cdisc_ct/serialize_cdisc_ct.py` | driver: discovery (newest release per package), `process_one`, `serialize_cdisc_ct`, `main`, audit |
| `scripts/data/cdisc_ct/download_cdisc_ct.sh` | download wrapper |
| `scripts/data/cdisc_ct/serialize_cdisc_ct.sh` | thin exec wrapper |
| `scripts/data/cdisc_ct/load_cdisc_ct.sh` | load then retire |
| `src/episteme/data/postgres_loader.py` | add `retire_source_files` |
| `tests/fixtures/sp7/cdisc_ct/` | small real-shaped fixture files |
| `tests/data/test_cdisc_ct_parse.py`, `tests/data/test_serialize_cdisc_ct.py`, `tests/test_cdisc_ct_wrapper.py`, `tests/data/test_retire_source_files.py` | tests |

---

### Task 1: Parser and row builder (`ct_parse.py`)

**Files:**
- Create: `src/episteme/data/cdisc_ct/__init__.py` (empty), `src/episteme/data/cdisc_ct/ct_parse.py`
- Create: `tests/fixtures/sp7/cdisc_ct/SDTM_Terminology.txt`
- Test: `tests/data/test_cdisc_ct_parse.py`

**Interfaces:**
- Produces:
  - `EXPECTED_HEADER: tuple[str, ...]` (the 8 names above)
  - `class CTFormatError(ValueError)`
  - `@dataclass Term(code: str, submission_value: str, synonyms: str, definition: str, preferred_term: str)`
  - `@dataclass Codelist(code: str, name: str, submission_value: str, extensible: str, definition: str, preferred_term: str, terms: list[Term])`
  - `parse_ct_file(path: Path) -> tuple[list[Codelist], dict[str, int]]` — returns codelists in file order of their header rows, plus counters `{"skipped_no_id": int, "orphan_terms": int}`
  - `build_rows(codelists: list[Codelist], *, package: str, release_date: str, source_file: str, max_terms: int = 200) -> list[dict]` — finalized `episteme.articles` rows

- [ ] **Step 1: Create the fixture.** Take real rows from the current SDTM file (the controller verified the format on 2026-09-28): the two `10-Meter Walk/Run` codelists shown in the spec plus their terms. Then add, by hand:
  - one term row placed **before** its codelist header (for Review Focus 3);
  - one definition containing literal double quotes, e.g. `Measured by "timed up and go" test.` (Review Focus 1);
  - one term row with an empty `Code` (to count `skipped_no_id`);
  - one term row whose `Codelist Code` has no header anywhere (to count `orphan_terms`).
  Write it as UTF-8, tab-separated, with the exact 8-column header line.

- [ ] **Step 2: Write failing tests**

```python
# tests/data/test_cdisc_ct_parse.py
from pathlib import Path

import pytest

from episteme.data import article_schema
from episteme.data.cdisc_ct.ct_parse import (
    EXPECTED_HEADER,
    CTFormatError,
    build_rows,
    parse_ct_file,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp7" / "cdisc_ct" / "SDTM_Terminology.txt"


def _write(tmp_path, lines):
    p = tmp_path / "X_Terminology.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_parses_codelists_and_terms():
    lists, counts = parse_ct_file(FX)
    by_code = {c.code: c for c in lists}
    assert "C141657" in by_code
    tc = by_code["C141657"]
    assert tc.name == "10-Meter Walk/Run Functional Test Test Code"
    assert tc.extensible == "No"
    assert "TENMW101" in [t.submission_value for t in tc.terms]
    assert counts["skipped_no_id"] == 1
    assert counts["orphan_terms"] == 1


def test_term_before_header_still_attaches():
    lists, _ = parse_ct_file(FX)
    # the fixture places one term row above its codelist header (Step 1)
    assert all(c.terms for c in lists if c.code == "C141657")


def test_double_quotes_do_not_break_columns():
    lists, _ = parse_ct_file(FX)
    defs = [t.definition for c in lists for t in c.terms]
    assert any('"timed up and go"' in d for d in defs)


def test_wrong_header_raises(tmp_path):
    p = _write(tmp_path, ["Code\tName", "C1\tx"])
    with pytest.raises(CTFormatError):
        parse_ct_file(p)


def test_wrong_column_count_raises(tmp_path):
    header = "\t".join(EXPECTED_HEADER)
    p = _write(tmp_path, [header, "C1\t\tNo\tList"])  # truncated row
    with pytest.raises(CTFormatError):
        parse_ct_file(p)


def test_header_only_file_yields_nothing(tmp_path):
    p = _write(tmp_path, ["\t".join(EXPECTED_HEADER)])
    lists, counts = parse_ct_file(p)
    assert lists == [] and counts == {"skipped_no_id": 0, "orphan_terms": 0}


def test_build_rows_ids_licence_and_text():
    lists, _ = parse_ct_file(FX)
    rows = build_rows(lists, package="SDTM", release_date="2026-09-25",
                      source_file="SDTM__2026-09-25__SDTM_Terminology.txt")
    r = next(r for r in rows if r["id"] == "cdisc_ct:SDTM:C141657:p1")
    assert r["source"] == "cdisc_ct"
    assert r["source_record_id"] == "SDTM:C141657:p1"
    assert r["license"] == article_schema.LICENSE_PUBLIC_DOMAIN
    assert r["subset"] == "commercial"
    assert "TENMW101" in r["text"] and "2026-09-25" in r["text"] and "SDTM" in r["text"]
    assert set(r) == set(article_schema.ARTICLE_COLUMNS)


def test_large_codelist_splits_with_repeated_header():
    lists, _ = parse_ct_file(FX)
    big = lists[0]
    big.terms = big.terms * 150  # > 200 terms
    rows = build_rows([big], package="SDTM", release_date="2026-09-25",
                      source_file="f", max_terms=200)
    n = -(-len(big.terms) // 200)
    assert [r["id"] for r in rows] == [f"cdisc_ct:SDTM:{big.code}:p{i}" for i in range(1, n + 1)]
    assert all(big.name in r["text"] for r in rows)
    assert all(f"part {i} of {n}" in rows[i - 1]["title"] for i in range(1, n + 1))


def test_same_codelist_in_two_packages_gets_distinct_ids():
    lists, _ = parse_ct_file(FX)
    a = build_rows(lists[:1], package="SDTM", release_date="d", source_file="a")
    b = build_rows(lists[:1], package="SEND", release_date="d", source_file="b")
    assert {r["id"] for r in a}.isdisjoint({r["id"] for r in b})
```

- [ ] **Step 3: Run to confirm RED** — `.venv/Scripts/python.exe -m pytest tests/data/test_cdisc_ct_parse.py -q -p no:cacheprovider` → import error.

- [ ] **Step 4: Implement `ct_parse.py`.** Key rules:
  - Read with `path.open(encoding="utf-8", errors="replace", newline="")` and `csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)`.
  - First row must equal `EXPECTED_HEADER` (strip each cell and a leading BOM), else `raise CTFormatError`.
  - Every later non-blank row must have exactly 8 cells, else `raise CTFormatError(f"line {n}: {len(row)} columns")`.
  - Rows with empty `Code` → `skipped_no_id += 1`.
  - Empty `Codelist Code` → a codelist header (keyed by `Code`); otherwise a term, collected by its codelist code. Assemble **after** reading the whole file. Terms whose codelist has no header → `orphan_terms += 1`.
  - Return codelists in the order their header rows appeared.
  - `build_rows`: split `terms` into chunks of `max_terms` (a codelist with no terms still yields one `p1` row). Text layout per part:
    ```
    CDISC {package} Controlled Terminology (NCI EVS release {release_date}).
    Codelist: {name} (submission value {submission_value}; NCI code {code}; extensible: {extensible}).
    Definition: {definition}
    NCI preferred term: {preferred_term}
    Terms (part {i} of {n}):
    - {submission_value} (NCI {code}): {preferred_term}. Synonyms: {synonyms}. Definition: {definition}
    ```
    (omit "Synonyms:" when empty; omit "(part i of n)" when `n == 1`). `title` = `name` plus ` (part i of n)` when `n > 1`. Row dict follows `serialize_mesh.mesh_row`'s column set (pmid/pmcid/doi/abstract/body_text/authors/journal/year/mesh/publication_types/language/pmc_version/is_manuscript/is_historical_ocr/pdf_url/container_id/book_meta all `None`; `is_retracted=False`), licence as in Global Constraints, then `finalize_row`.

- [ ] **Step 5: GREEN**, ruff format/check, commit: `feat(sp7): CDISC CT parser and row builder`.

---

### Task 2: Serializer driver

**Files:**
- Create: `src/episteme/data/cdisc_ct/serialize_cdisc_ct.py`, `scripts/data/cdisc_ct/serialize_cdisc_ct.sh`
- Modify: `src/episteme/data/article_schema.py` (add `"cdisc_ct"` to `SOURCES`)
- Test: `tests/data/test_serialize_cdisc_ct.py`

**Interfaces:**
- Consumes: `parse_ct_file`, `build_rows`, `CTFormatError` (Task 1).
- Produces: `SOURCE = "cdisc_ct"`, `PACKAGES = ("SDTM", "SEND", "ADaM", "Define-XML", "Protocol")`, `discover_cdisc_ct_files(raw_dir: Path) -> list[Path]` (newest release per package, package order), `release_date_of(path: Path) -> str` (the parent folder name), `process_one(path, *, processed_dir, raw_dir, force) -> dict`, `serialize_cdisc_ct(...)` (frozen contract), `main(argv=None) -> int`.

- [ ] **Step 1: Failing tests** (build raw trees in `tmp_path` by copying the Task 1 fixture):

```python
# tests/data/test_serialize_cdisc_ct.py
import shutil
from pathlib import Path

import polars as pl

from episteme.data import article_schema
from episteme.data.cdisc_ct.serialize_cdisc_ct import (
    discover_cdisc_ct_files,
    serialize_cdisc_ct,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp7" / "cdisc_ct" / "SDTM_Terminology.txt"


def _place(raw, package, date):
    d = raw / package / date
    d.mkdir(parents=True)
    shutil.copy(FX, d / f"{package}_Terminology.txt")
    return d / f"{package}_Terminology.txt"


def test_source_registered():
    assert "cdisc_ct" in article_schema.SOURCES


def test_discovers_newest_release_per_package(tmp_path):
    raw = tmp_path / "raw"
    _place(raw, "SDTM", "2026-06-26")
    newest = _place(raw, "SDTM", "2026-09-25")
    proto = _place(raw, "Protocol", "2026-07-11")  # different date from SDTM
    found = discover_cdisc_ct_files(raw)
    assert found == [newest, proto]


def test_serializes_and_is_restartable(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    _place(raw, "SDTM", "2026-09-25")
    first = serialize_cdisc_ct(raw, out)
    assert first["inputs"] == 1 and first["ok"] == 1 and first["rows"] > 0
    shards = list((out / "staging" / "cdisc_ct").glob("*"))
    assert len(shards) == 1 and "SDTM__2026-09-25__SDTM_Terminology" in shards[0].name
    df = pl.read_parquet(shards[0])
    assert df["id"].str.starts_with("cdisc_ct:SDTM:").all()
    again = serialize_cdisc_ct(raw, out)
    assert again["rows"] == 0  # skipped via marker


def test_new_release_is_processed(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    _place(raw, "SDTM", "2026-06-26")
    serialize_cdisc_ct(raw, out)
    _place(raw, "SDTM", "2026-09-25")
    res = serialize_cdisc_ct(raw, out)
    assert res["ok"] == 1 and res["rows"] > 0


def test_bad_file_is_marked_failed_not_partial(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    d = raw / "SDTM" / "2026-09-25"
    d.mkdir(parents=True)
    (d / "SDTM_Terminology.txt").write_text("<html>fallback</html>\n", encoding="utf-8")
    res = serialize_cdisc_ct(raw, out)
    assert res["failed"] == 1 and res["rows"] == 0
    assert not list((out / "staging" / "cdisc_ct").glob("*")) if (out / "staging" / "cdisc_ct").exists() else True
```

Adjust the staging path assertion to the staging writer's real layout (read `src/episteme/data/staging_writer.py` first; SP4 serializer tests show the pattern) — do not guess.

- [ ] **Step 2: RED.**

- [ ] **Step 3: Implement** by following `src/episteme/data/mesh/serialize_mesh.py`'s structure exactly for imports, `_best_effort_audit` (including the SP6 logged double-failure fallback: `_LOG.warning("audit mirror_only fallback also failed for %s", basename, exc_info=True)`), `process_one`, `serialize_cdisc_ct`, `_run_report`, and `main` (`--raw-dir` default `get_settings().raw_root / "cdisc_ct"`, `--processed-dir` default `get_settings().processed_root`, plus `--max-files`, `--force`, `--workers`, `--verbose`, `--report`). Differences:
  - discovery:
    ```python
    _DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    def discover_cdisc_ct_files(raw_dir: Path) -> list[Path]:
        out: list[Path] = []
        for pkg in PACKAGES:
            base = Path(raw_dir) / pkg
            if not base.is_dir():
                continue
            dates = sorted(d.name for d in base.iterdir() if d.is_dir() and _DATE.match(d.name))
            for date in reversed(dates):
                f = base / date / f"{pkg}_Terminology.txt"
                if f.is_file():
                    out.append(f)
                    break
        return out
    ```
  - `process_one` gets `package = path.parent.parent.name`, `release_date = path.parent.name`, `basename = input_key(path, raw_dir)`, calls `parse_ct_file` then `build_rows(..., source_file=basename)`, and records `skipped_no_id`/`orphan_terms` in the success stats. `CTFormatError` (and any exception) → `mark_failed`, no shard written.
  - `scripts/data/cdisc_ct/serialize_cdisc_ct.sh`: copy `scripts/data/mesh/serialize_mesh.sh`, changing only the module to `episteme.data.cdisc_ct.serialize_cdisc_ct`. Mode 100755.

- [ ] **Step 4: GREEN** (`tests/data/test_serialize_cdisc_ct.py`, plus `tests/data/test_article_schema.py` still passes); ruff; `bash -n`; commit `feat(sp7): CDISC CT serializer driver`.

---

### Task 3: Download wrapper and registration

**Files:**
- Create: `scripts/data/cdisc_ct/download_cdisc_ct.sh` (100755)
- Modify: `scripts/data/_lib/sources.env` (add `CDISC_CT_BASE=https://evs.nci.nih.gov/ftp1/CDISC`), `scripts/data/run_pipeline.sh` (`WRAPPER` table: `[cdisc_ct]="cdisc_ct/download_cdisc_ct.sh"`), `docs/12-source-inventory.md` (row for `cdisc_ct`; `tests/docs` pins the inventory to the `WRAPPER` table, so it must land in this task), `tests/test_run_pipeline_dispatch.py` (`"cdisc_ct"` in `FAST_TOKENS`)
- Test: `tests/test_cdisc_ct_wrapper.py`

**Interfaces:** consumes nothing from Tasks 1–2; produces the raw layout Task 2 discovers.

- [ ] **Step 1: Failing tests** — model the fake-`curl` shim on `tests/test_cdisc_bc_wrapper.py` (read it: it puts the shim first on `PATH` inside `bash -c`, writes the shim with LF endings, and skips if `aria2c` is installed). The shim must:
  - for `-I`/HEAD requests: answer `Content-Type: text/plain`, `Content-Length`, `Last-Modified: Fri, 25 Sep 2026 15:43:39 GMT` for SDTM/SEND/ADaM/Define-XML, `Last-Modified: Sat, 11 Jul 2026 23:27:50 GMT` for Protocol; and `Content-Type: text/html` for any URL containing `SEND` when the test sets `FAKE_SEND_MISSING=1`;
  - for downloads: write a body whose first line is the exact 8-column header plus one data row.

  Tests:
  ```python
  def test_writes_per_package_dated_folders(tmp_path): ...
      # raw/cdisc_ct/SDTM/2026-09-25/SDTM_Terminology.txt exists; Protocol under 2026-07-11;
      # each release folder has PROVENANCE.txt containing the source URL and "free to use without licensing restrictions"
  def test_html_fallback_package_is_skipped_others_download(tmp_path): ...
      # FAKE_SEND_MISSING=1 -> rc 0, stderr warns about SEND, no SEND folder, SDTM present
  def test_dry_run_writes_nothing(tmp_path): ...
  def test_max_files_limits_packages_in_order(tmp_path): ...
      # --max-files 2 -> only SDTM and SEND folders
  def test_rerun_same_release_skips_download(tmp_path): ...
      # second run makes no GET (only HEADs) per the shim's call log
  def test_bad_header_after_download_is_rejected(tmp_path): ...
      # shim returns text/plain but a body without the header -> file removed, warning, rc 0
  ```
  Write each as a full test (same helper style as `tests/test_cdisc_bc_wrapper.py`), not stubs.

- [ ] **Step 2: RED.**

- [ ] **Step 3: Implement the wrapper** following `scripts/data/mesh/download_mesh.sh`'s argument handling (`--dry-run`, `--max-files`, `--force`, `--reason`), `load_dotenv`, `require_env EPISTEME_ACTOR CDISC_CT_BASE`, `resolve_dest cdisc_ct`. Per package in order (respecting `--max-files`):
  1. `url="$CDISC_CT_BASE/$pkg/$pkg%20Terminology.txt"`; `hdr="$(curl -sSI --connect-timeout 20 --max-time 60 "$url" | tr -d '\r')"`.
  2. If `Content-Type` is not `text/plain` → `log WARN "cdisc_ct: $pkg not available (Content-Type ...); skipping"`, continue.
  3. Release date: parse `Last-Modified` with `date -u -d "..." +%Y-%m-%d`; if empty → warn, skip.
  4. `out="$dest/$pkg/$date/${pkg}_Terminology.txt"`. If the file exists with the same size and not `--force` → log "up to date", continue.
  5. Dry run → log what would be fetched, continue. Otherwise fetch with the existing `common.sh` fetch helper (read `http_fetch` to use it correctly), then check the first line equals the expected header (compare with a literal tab-joined string); on mismatch remove the file, warn, continue.
  6. Write `PROVENANCE.txt` in the release folder (URL, Last-Modified, retrieved-at UTC, licence statement).
  7. `write_sync_stamp "$dest/$pkg/$date"`.
  Exit 0 unless a hard failure (missing env) occurs.

- [ ] **Step 4:** `docs/12` row for `cdisc_ct`: class structured, stages `download` (at this point `cdisc_ct` is only in the `WRAPPER` table, and `tests/docs/test_source_inventory_doc.py` pins the stage column to the dispatcher's rules; Task 4 extends it to `download, serialize, load`), licence `public_domain` governance override (decision 2026-09-28; NCI: free to use without licensing restrictions), cadence quarterly, script path `cdisc_ct/download_cdisc_ct.sh`.

- [ ] **Step 5: GREEN** (`tests/test_cdisc_ct_wrapper.py`, `tests/test_run_pipeline_dispatch.py -k cdisc_ct`, `tests/docs`); `bash -n`; grep gate; commit `feat(sp7): CDISC CT download wrapper and registration`.

---

### Task 4: Load wrapper and retire step (LOCAL — database)

**Files:**
- Modify: `src/episteme/data/postgres_loader.py` (add `retire_source_files`), `scripts/data/run_pipeline.sh` (`cdisc_ct` into `STRUCTURED_SOURCES`), `docs/12-source-inventory.md` (extend the `cdisc_ct` row's stages to `download, serialize, load`)
- Create: `scripts/data/cdisc_ct/load_cdisc_ct.sh` (100755), `src/episteme/data/cdisc_ct/retire.py` (CLI that computes the keep-set and calls the loader function)
- Test: `tests/data/test_retire_source_files.py` (pg-marked)

**Interfaces:**
- Consumes: `discover_cdisc_ct_files` (Task 2), `input_key`, `checkpoint_markers.load_success_marker_path`.
- Produces: `postgres_loader.retire_source_files(conn, *, source: str, keep_source_files: list[str], run_id: str | None = None) -> int` — deletes matching `episteme.article_body` rows (by `article_id` of the rows being retired, scoped by `source`) then `episteme.articles` rows `WHERE source = %s AND NOT (source_file = ANY(%s))`, records one `load_replace` audit event (`object=f"{source} retire"`, `rows_affected=<deleted articles>`) in the caller's transaction, does **not** commit, and returns the deleted-articles count. **If `keep_source_files` is empty it raises `ValueError`** (Review Focus 4).
- `python -m episteme.data.cdisc_ct.retire [--raw-dir ...] [--processed-dir ...]`: keep-set = `input_key` of each `discover_cdisc_ct_files` result; if any of those lacks a load-success marker (`load_success_marker_path(processed_dir, "cdisc_ct", <shard name>)` — read `load_articles.py` for how shard names derive from `source_file`), print which package is not loaded yet and exit 0 without deleting; else open `connection()`, call `retire_source_files`, commit, print the count.

- [ ] **Step 1: Failing pg tests** — follow `tests/data/test_audit_trail.py`'s `pg_conn` + `_setup_schema` pattern. Insert `episteme.articles` rows (and `article_body` rows) for `source='cdisc_ct'` under `SDTM__2026-06-26__...` and `SDTM__2026-09-25__...`, plus one `source='mesh'` row:
  ```python
  def test_retires_old_release_keeps_new_and_other_sources(pg_conn): ...
  def test_article_body_follows(pg_conn): ...
  def test_records_load_replace_audit(pg_conn): ...
  def test_empty_keep_set_raises_and_deletes_nothing(pg_conn): ...
  ```
  Write each fully with explicit inserts and `SELECT count(*)` assertions.

- [ ] **Step 2: RED** — confirm server first: `set -a; . ./.env; set +a; PGDATABASE=episteme_test .venv/Scripts/python.exe -c "import os,psycopg; c=psycopg.connect(os.environ['TEST_PG_DSN']); print(c.execute(\"select current_setting('server_version')\").fetchone()[0])"` → `19beta3`; then `PGDATABASE=episteme_test .venv/Scripts/python.exe -m pytest tests/data/test_retire_source_files.py -m pg -q -p no:cacheprovider`.

- [ ] **Step 3: Implement** `retire_source_files`, `retire.py`, and `load_cdisc_ct.sh` (copy `scripts/data/mesh/load_mesh.sh`; after the `load_articles` call succeeds, run `"$PY" -m episteme.data.cdisc_ct.retire "$@"`; do not `exec` the first call). Add `cdisc_ct` to `STRUCTURED_SOURCES`.

- [ ] **Step 4: GREEN** (pg tests, full `-m pg` suite, `tests/test_run_pipeline_dispatch.py`, `tests/docs`); `bash -n`; commit `feat(sp7): CDISC CT load wrapper and retire step`.

---

### Task 5: Docs

**Files:** Modify `docs/02-data-sources.md` (§2.6 row: CDISC CT now **wired**; backlog row done), `docs/09-extraction-contract.md` (licence vocabulary: `public_domain` also covers `cdisc_ct`, decision 2026-09-28), `docs/10-data-sources-runbook.md` (new section: first-time and quarterly run — `run_pipeline.sh cdisc_ct download`, `serialize`, `load`; note the load step retires the previous release and does nothing until every package's newest shard is loaded; downloads need `evs.nci.nih.gov` reachable).
**Test:** `tests/docs` (runbook commands must be accepted by the dispatcher; licence tests in `tests/docs/test_extraction_contract_doc.py` may need `cdisc_ct` added to the public_domain setter set — read them).

- [ ] Steps: write, run `tests/docs`, commit `docs(sp7): CDISC CT in catalog, contract and runbook`.

---

### Task 6: Close-out (LOCAL)

- [ ] Non-pg suite and pg suite (pg with `.env` sourced; server 19beta3); `bash -n` on the three new scripts; grep gate; ruff on changed files.
- [ ] Real bounded proof against `episteme_test`: `PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh cdisc_ct download` (all five packages, ~18 MB), then `serialize`, then `load`. Record per-package row counts from the serializer output, confirm `select count(*) from episteme.articles where source='cdisc_ct'` matches, re-run `serialize` (0 rows, skipped) and `load` (skipped; retire deletes 0).
- [ ] Drift-log entry in `docs/project-incubation-baseline.md` (SP7 landed, spec/plan paths, counts, licence decision, deferred items). Commit `docs(sp7): drift-log entry - CDISC CT landed`.
