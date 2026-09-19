# SP4.1 Hardening & Acquisition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close SP4's deferred whole-branch-review items (fail-closed DB-mode guard, shared input identity, wrapper gaps, licence mapping, small correctness items), add the CDISC Biomedical Concepts download source, and triage Dependabot alerts.

**Architecture:** One choke point (`dsn_from_settings()`) enforces a `read-only | restricted | unrestricted` DB mode read by `config.py`; `checkpoint_markers` gains one shared, raw-dir-relative input identity used by all eight SP4 serializers (and by `graph_builder`'s mesh phase through an inverse lookup); three SP3 download wrappers are fixed and one added; `normalize_license` gains two URL arms and a source-anchored `public_domain` class.

**Tech Stack:** Python 3.13 / pytest / psycopg 3 + psycopg_pool / DuckDB / bash wrappers over `scripts/data/_lib/common.sh`.

**Spec:** `docs/superpowers/specs/2026-09-19-sp4-1-hardening-design.md` (read it; the plan argues from it).

## Global Constraints

- Branch `sp4-1-hardening` (base `main`@`2482216`; spec committed on it). Local `.gitignore` shows an unrelated unstaged `.gstack/` line — leave it unstaged, never `git add` it.
- **`.env` holds live secrets.** Only Task 1 edits it, only by *appending* the three lines given there. Never `cat`/print/echo it or any value from it; never stage it. If `.env` ever shows in `git status` as staged, stop.
- **Every command you run that touches this codebase's Python/bash entrypoints — `run_pipeline.sh` (any stage, including `download`), `python -m episteme.*` (including `--report`), serializers, loaders — must be prefixed `PGDATABASE=episteme_test`.** Those entrypoints open a best-effort audit connection even when they look DB-free, and the local `.env` points `PGDATABASE` at the real `episteme` DB. SP4 wrote 18 stray audit rows this way. Automated `pg`-marked tests are safe (they use `TEST_PG_DSN`); run them as `set -a; . ./.env; set +a; .venv/Scripts/python.exe -m pytest …`. Once Task 1 lands, `EPISTEME_DB_TARGET=secondary` is an equivalent prefix, but keep using `PGDATABASE=episteme_test`.
- `src/episteme/config.py` stays the only `os.environ` reader. `article_schema.ARTICLE_COLUMNS` is untouched. Endpoint URLs live only in `scripts/data/_lib/sources.env` (the grep gate: `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'` must stay empty).
- The serializer contract is frozen: `serialize_<source>(raw_dir, processed_dir, *, max_files=0, force=False, workers=1, verbose=False) -> {"inputs","ok","failed","rows"}`, `id = f"{source}:{native_id}"`, audit event `"serialize_commit"`, `container_id`/`book_meta` = `None`.
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```
  Pre-commit hooks (ruff, ruff-format, bandit, …) must pass — never `--no-verify`. `git add` explicit paths only, never `-A`/`.`.
- Test commands: non-`pg`: `.venv/Scripts/python.exe -m pytest -q -m "not pg"` (≈6 min full; run targeted files while iterating). Baseline at plan start: 180 passed / 2 skipped (non-pg), 202 passed / 3 skipped (full).
- Real-data end-to-end proofs are bounded (a genuine slice is fine) and run against `episteme_test`; afterwards `git clean -fdx 01_raw/<source> 02_processed` (both gitignored scratch).
- Network tests are dry-run only; no test downloads real data.

---

## Phase 1 — Safety

### Task 1: DB-mode guard core

**Files:**
- Create: `src/episteme/data/db/guard.py`
- Modify: `src/episteme/config.py` (Settings fields + `get_settings()`), `src/episteme/data/db/connection.py`, `.env.example`, local `.env` (append only)
- Test: `tests/test_db_guard.py`

**Interfaces:**
- Produces: `Settings.db_mode: str` (`"read-only"|"restricted"|"unrestricted"`, default `"restricted"`), `Settings.db_target: str` (`"primary"|"secondary"`), `Settings.production_database: str` (default `"episteme"`), `Settings.pg_database_secondary: str | None`; `guard.DB_MODES`, `guard.ProductionDatabaseGuardError(RuntimeError)`, `guard.check_database_allowed(settings) -> None`, `guard.pool_kwargs(settings) -> dict`.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the failing tests** — `tests/test_db_guard.py`:

```python
from dataclasses import replace

import pytest

from episteme.config import ConfigError, get_settings
from episteme.data.db.guard import (
    DB_MODES,
    ProductionDatabaseGuardError,
    check_database_allowed,
    pool_kwargs,
)


def _s(**kw):
    return replace(get_settings(), **kw)


@pytest.fixture
def clean_settings(monkeypatch):
    """Resolve settings from a scrubbed environment (no project .env)."""
    monkeypatch.setattr("episteme.config._find_project_dotenv", lambda: None)
    for k in (
        "EPISTEME_DB_MODE",
        "EPISTEME_DB_TARGET",
        "PGDATABASE",
        "PGDATABASE_SECONDARY",
        "EPISTEME_PRODUCTION_DATABASE",
    ):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_restricted_refuses_production():
    s = _s(db_mode="restricted", pg_database="episteme", production_database="episteme")
    with pytest.raises(ProductionDatabaseGuardError) as ei:
        check_database_allowed(s)
    msg = str(ei.value)
    assert "Refusing to open a connection to production database" in msg
    assert "EPISTEME_DB_MODE=unrestricted" in msg
    assert "EPISTEME_DB_TARGET=secondary" in msg


def test_restricted_allows_non_production():
    check_database_allowed(_s(db_mode="restricted", pg_database="episteme_test"))


def test_unrestricted_allows_production():
    check_database_allowed(_s(db_mode="unrestricted", pg_database="episteme"))


def test_read_only_allows_production_but_sets_pool_option():
    s = _s(db_mode="read-only", pg_database="episteme")
    check_database_allowed(s)
    assert pool_kwargs(s) == {"options": "-c default_transaction_read_only=on"}


def test_pool_kwargs_empty_for_other_modes():
    assert pool_kwargs(_s(db_mode="restricted")) == {}
    assert pool_kwargs(_s(db_mode="unrestricted")) == {}


def test_custom_production_name():
    s = _s(db_mode="restricted", pg_database="prod_db", production_database="prod_db")
    with pytest.raises(ProductionDatabaseGuardError):
        check_database_allowed(s)
    check_database_allowed(_s(db_mode="restricted", pg_database="episteme", production_database="prod_db"))


def test_default_mode_is_restricted(clean_settings):
    s = get_settings()
    assert s.db_mode == "restricted"
    assert s.db_target == "primary"
    assert s.production_database == "episteme"


def test_invalid_mode_is_rejected(clean_settings, monkeypatch):
    monkeypatch.setenv("EPISTEME_DB_MODE", "bogus")
    get_settings.cache_clear()
    with pytest.raises(ConfigError):
        get_settings()


def test_secondary_target_resolves_secondary_database(clean_settings, monkeypatch):
    monkeypatch.setenv("PGDATABASE", "episteme")
    monkeypatch.setenv("PGDATABASE_SECONDARY", "episteme_test")
    monkeypatch.setenv("EPISTEME_DB_TARGET", "secondary")
    get_settings.cache_clear()
    assert get_settings().pg_database == "episteme_test"


def test_secondary_target_without_secondary_is_an_error(clean_settings, monkeypatch):
    monkeypatch.setenv("EPISTEME_DB_TARGET", "secondary")
    get_settings.cache_clear()
    with pytest.raises(ConfigError):
        get_settings()


def test_db_modes_constant():
    assert DB_MODES == ("read-only", "restricted", "unrestricted")


@pytest.mark.pg
def test_read_only_mode_blocks_writes(monkeypatch):
    """Through the real pool, against episteme_test only."""
    import psycopg

    import episteme.data.db.connection as conn_mod

    monkeypatch.setenv("PGDATABASE", "episteme_test")
    monkeypatch.setenv("EPISTEME_DB_MODE", "read-only")
    get_settings.cache_clear()
    monkeypatch.setattr(conn_mod, "_POOL", None)
    try:
        with conn_mod.connection() as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                cur.execute("CREATE TABLE episteme._ro_probe (x int)")
    finally:
        if conn_mod._POOL is not None:
            conn_mod._POOL.close()
        get_settings.cache_clear()
```

- [ ] **Step 2: Run — verify it fails** (`ModuleNotFoundError: episteme.data.db.guard`, and `Settings` has no `db_mode`): `.venv/Scripts/python.exe -m pytest -q tests/test_db_guard.py -m "not pg"`.

- [ ] **Step 3: Implement.**

`src/episteme/data/db/guard.py`:
```python
"""Database-mode guard: one pure check, called from dsn_from_settings().

Modes mirror MCPG_ACCESS_MODE: ``read-only`` (any DB, connections are read-only),
``restricted`` (any DB except the production one), ``unrestricted`` (no refusal).
"""

from __future__ import annotations

import logging
from typing import Any

from episteme.config import DB_MODES  # noqa: F401  (defined in config.py, the base layer; re-exported here)

_LOG = logging.getLogger(__name__)

_WARNED = False


class ProductionDatabaseGuardError(RuntimeError):
    """Raised, before any connection attempt, when the DB mode forbids the target."""


def check_database_allowed(settings: Any) -> None:
    global _WARNED
    if settings.db_mode != "restricted":
        return
    if settings.pg_database != settings.production_database:
        return
    msg = (
        f"Refusing to open a connection to production database "
        f"{settings.pg_database!r} (EPISTEME_DB_MODE=restricted); no connection "
        f"was attempted. To run against production deliberately, prefix the "
        f"command with EPISTEME_DB_MODE=unrestricted; to use the secondary "
        f"database instead, set EPISTEME_DB_TARGET=secondary "
        f"(or PGDATABASE=episteme_test)."
    )
    if not _WARNED:
        _WARNED = True
        _LOG.warning(msg)
    raise ProductionDatabaseGuardError(msg)


def pool_kwargs(settings: Any) -> dict[str, str]:
    """psycopg connection kwargs for the configured mode."""
    if settings.db_mode == "read-only":
        return {"options": "-c default_transaction_read_only=on"}
    return {}
```

`config.py`: first read `_get` (top of file) to confirm it returns `None`/default for unset or empty values. Add a module-level constant next to the other constants: `DB_MODES = ("read-only", "restricted", "unrestricted")` (config is the base layer; `guard.py` re-exports it — config must NOT import from `episteme.data.db`). Append four defaulted fields at the END of the `Settings` dataclass (after `aact_downloads`; defaults must trail — any test that builds `Settings(...)` positionally keeps working):
```python
    db_mode: str = "restricted"
    db_target: str = "primary"
    production_database: str = "episteme"
    pg_database_secondary: str | None = None
```
In `get_settings()`, before `return Settings(`:
```python
    db_mode = (_get("EPISTEME_DB_MODE") or "restricted").strip().lower()
    if db_mode not in DB_MODES:
        raise ConfigError(f"EPISTEME_DB_MODE must be one of {DB_MODES}, got {db_mode!r}")
    db_target = (_get("EPISTEME_DB_TARGET") or "primary").strip().lower()
    if db_target not in ("primary", "secondary"):
        raise ConfigError(f"EPISTEME_DB_TARGET must be primary|secondary, got {db_target!r}")
    secondary = _get("PGDATABASE_SECONDARY") or None
    if db_target == "secondary" and not secondary:
        raise ConfigError("EPISTEME_DB_TARGET=secondary but PGDATABASE_SECONDARY is not set")
    pg_database = secondary if db_target == "secondary" else _get("PGDATABASE", "episteme")
```
and pass `pg_database=pg_database,` (replacing the existing `pg_database=_get("PGDATABASE", "episteme")` line) plus `db_mode=db_mode, db_target=db_target, production_database=_get("EPISTEME_PRODUCTION_DATABASE", "episteme"), pg_database_secondary=secondary`.

`connection.py`:
```python
from episteme.data.db.guard import check_database_allowed, pool_kwargs

def dsn_from_settings() -> str:
    settings = get_settings()
    check_database_allowed(settings)
    return settings.pg_dsn()

def get_pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool(
            dsn_from_settings(),
            kwargs=pool_kwargs(get_settings()),
            min_size=1,
            max_size=8,
            open=True,
        )
    return _POOL
```

`.env.example`: append (placeholders, mirroring its existing comment style):
```
# --- Database mode (SP4.1) ---
# read-only | restricted | unrestricted. Code default when unset is `restricted`
# (refuses the production database; override per command with an
# EPISTEME_DB_MODE=unrestricted prefix). `unrestricted` is a development-phase
# setting -- set it back to `restricted` at go-live.
PGDATABASE_SECONDARY=episteme_test
EPISTEME_DB_MODE=restricted
# EPISTEME_DB_TARGET=primary               # primary | secondary
```

Local `.env` (append only; prints nothing; user-authorized 2026-09-19):
```bash
cd /c/Users/devop/GitHub/Episteme
tail -c1 .env | od -An -c | grep -q '\\n' || printf '\n' >> .env
cat >> .env <<'EOF'

# --- Database mode (SP4.1) ---
# Development-phase setting: set EPISTEME_DB_MODE=restricted at go-live.
PGDATABASE_SECONDARY=episteme_test
EPISTEME_DB_MODE=unrestricted
# EPISTEME_DB_TARGET=primary   (primary | secondary)
EOF
grep -c '^EPISTEME_DB_MODE=unrestricted$' .env  # expect 1 (prints a count only)
git status --short                              # .env must NOT appear
```
**Never put an inline `# comment` after a value in `.env`.** `scripts/data/_lib/common.sh`'s shell `load_dotenv` does not strip them, so the comment text would be exported as part of the value and `config.py` would reject it. Comments go on their own lines.

- [ ] **Step 4: Run — verify pass:** `.venv/Scripts/python.exe -m pytest -q tests/test_db_guard.py -m "not pg"` → all green; then the pg test: `set -a; . ./.env; set +a; .venv/Scripts/python.exe -m pytest -q tests/test_db_guard.py -m pg` → 1 passed (this resolves spec open item 4: report whether the read-only option survives pool reconnects). Then the whole non-pg suite (`-m "not pg"`) → no regressions (the new Settings fields are defaulted).
- [ ] **Step 5: Commit** — `feat(sp4.1): fail-closed DB-mode guard (read-only|restricted|unrestricted)` — `git add src/episteme/data/db/guard.py src/episteme/config.py src/episteme/data/db/connection.py .env.example tests/test_db_guard.py` (never `.env`).

---

### Task 2: Shell-layer integration and dispatch test

**Files:**
- Modify: `scripts/data/run_pipeline.sh` (lines ≈286 and ≈366), `tests/test_run_pipeline_dispatch.py`

**Interfaces:** Consumes Task 1's `ProductionDatabaseGuardError` message text (`"Refusing to open a connection to production database"`).

- [ ] **Step 1: Write the failing test** — append to `tests/test_run_pipeline_dispatch.py` (the file already has `_run`, `BASH`, `SCRIPT`, `REPO_ROOT`, `FAST_TIMEOUT`, `os`, `subprocess`):

```python
def _run_env(args: list[str], tmp_path: Path, timeout: int, **env_over: str):
    env = {
        **os.environ,
        "EPISTEME_ACTOR": "episteme_sys_admin",
        "EPISTEME_DATA_ROOT": str(tmp_path),
        **env_over,
    }
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_restricted_mode_refuses_production_database(tmp_path: Path) -> None:
    # PGPORT=1: even if the guard were ever broken, no real connection to any
    # database can be opened by this test.
    proc = _run_env(
        ["chembl", "serialize"],
        tmp_path,
        FAST_TIMEOUT,
        PGDATABASE="episteme",
        EPISTEME_DB_MODE="restricted",
        PGPORT="1",
    )
    assert "Refusing to open a connection to production database" in proc.stderr
    assert "connection refused" not in proc.stderr.lower()


def test_restricted_mode_allows_secondary_target(tmp_path: Path) -> None:
    proc = _run_env(
        ["chembl", "serialize"],
        tmp_path,
        FAST_TIMEOUT,
        PGDATABASE="episteme",
        PGDATABASE_SECONDARY="episteme_test",
        EPISTEME_DB_TARGET="secondary",
        EPISTEME_DB_MODE="restricted",
        PGPORT="1",
    )
    assert "Refusing to open a connection to production database" not in proc.stderr
```

- [ ] **Step 2: Run — verify it fails** (`… -k restricted_mode`): the refusal string is absent because the shell warning path is untested/unchanged — confirm the FIRST test fails only if the guard message never reaches stderr; if it already passes (Task 1's traceback surfaces through the audit CLI), keep the tests and proceed to Step 3 only for the wording change.
- [ ] **Step 3: Implement** — in `run_pipeline.sh` replace both audit warnings:
  - line ≈286: `|| log WARN "run_start audit failed; proceeding unaudited (DB unreachable, or refused by the DB-mode guard — see EPISTEME_DB_MODE)"`
  - line ≈366: `|| log WARN "run_end audit failed (DB unreachable, or refused by the DB-mode guard — see EPISTEME_DB_MODE)"`
- [ ] **Step 4: Run — pass:** `.venv/Scripts/python.exe -m pytest -q tests/test_run_pipeline_dispatch.py`; `bash -n scripts/data/run_pipeline.sh`.
- [ ] **Step 5: Commit** — `feat(sp4.1): run_pipeline.sh surfaces the DB-mode guard; dispatch tests` — `git add scripts/data/run_pipeline.sh tests/test_run_pipeline_dispatch.py`.

---

## Phase 2 — Identity and correctness

### Task 3: Shared input identity in `checkpoint_markers`; migrate the eight serializers

**Files:**
- Modify: `src/episteme/data/checkpoint_markers.py`; the eight `src/episteme/data/<src>/serialize_<src>.py` (chembl, uniprot, pubchem, clinvar, reactome, mesh, ontologies, openalex); `src/episteme/data/graph_builder.py` (mesh phase, ≈line 362-364)
- Test: `tests/data/test_checkpoint_markers.py` (create or extend), `tests/data/test_serializer_identity.py` (create), extend `tests/data/test_graph_builder.py`, adjust `tests/data/test_serialize_openalex.py`

**Interfaces:**
- Produces (in `checkpoint_markers.py`): `input_key(path: Path, raw_root: Path) -> str`, `discover_input_files(raw_root: Path, patterns: list[str]) -> list[Path]`, `find_input_by_key(raw_root: Path, key: str) -> Path | None`. Legacy `list_input_files` stays (SP2 extractors use it) with a docstring warning.
- Consumed by Tasks 4, 5 (serializer edits) and Task 12.

- [ ] **Step 1: Write the failing helper tests** — `tests/data/test_checkpoint_markers.py`:

```python
from pathlib import Path

from episteme.data.checkpoint_markers import (
    discover_input_files,
    find_input_by_key,
    input_key,
)


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    return p


def test_flat_layout_key_is_the_bare_basename(tmp_path):
    f = _touch(tmp_path / "raw" / "desc2025.gz")
    assert input_key(f, tmp_path / "raw") == "desc2025.gz"


def test_nested_layout_key_is_relative_path_with_double_underscore(tmp_path):
    f = _touch(tmp_path / "raw" / "updated_date=2026-06-25" / "part_0000.gz")
    assert input_key(f, tmp_path / "raw") == "updated_date=2026-06-25__part_0000.gz"


def test_path_outside_root_falls_back_to_basename(tmp_path):
    f = _touch(tmp_path / "elsewhere" / "a.txt")
    assert input_key(f, tmp_path / "raw") == "a.txt"


def test_discover_keeps_same_basename_in_different_directories(tmp_path):
    raw = tmp_path / "raw"
    a = _touch(raw / "a" / "data.tsv")
    b = _touch(raw / "b" / "data.tsv")
    found = discover_input_files(raw, ["*.tsv"])
    assert found == [a, b]
    assert {input_key(p, raw) for p in found} == {"a__data.tsv", "b__data.tsv"}


def test_discover_does_not_duplicate_a_file_matched_by_glob_and_rglob(tmp_path):
    raw = tmp_path / "raw"
    _touch(raw / "one.tsv")
    assert len(discover_input_files(raw, ["*.tsv", "one.*"])) == 1


def test_find_input_by_key_roundtrip(tmp_path):
    raw = tmp_path / "raw"
    f = _touch(raw / "nlm" / "desc2026.gz")
    assert find_input_by_key(raw, input_key(f, raw)) == f
    assert find_input_by_key(raw, "missing.gz") is None


def test_find_input_by_key_handles_filenames_containing_double_underscore(tmp_path):
    raw = tmp_path / "raw"
    f = _touch(raw / "d" / "we__ird.txt")
    key = input_key(f, raw)
    assert key == "d__we__ird.txt"
    assert find_input_by_key(raw, key) == f
```

- [ ] **Step 2: Run — verify it fails** (ImportError for the three names).
- [ ] **Step 3: Implement the helpers** in `checkpoint_markers.py` (keep `list_input_files`, add to its docstring: "Legacy: dedups by BASENAME — two files sharing a name in different directories collapse to one. New code uses discover_input_files + input_key."):

```python
def input_key(path: Path, raw_root: Path) -> str:
    """Stable per-input identity: the path relative to ``raw_root``, joined with
    ``__``. A file directly under ``raw_root`` yields its bare basename, so
    flat layouts keep the keys (markers, shards, ``source_file``) they had
    before this helper existed."""
    p = Path(path).resolve()
    try:
        rel = p.relative_to(Path(raw_root).resolve())
    except ValueError:
        return p.name
    return "__".join(rel.parts)


def discover_input_files(raw_root: Path, patterns: list[str]) -> list[Path]:
    """glob + rglob per pattern; dedup by resolved FULL path (never basename)."""
    root = Path(raw_root)
    seen: set[Path] = set()
    out: list[Path] = []
    for pat in patterns:
        for f in [*root.glob(pat), *root.rglob(pat)]:
            if not f.is_file():
                continue
            rp = f.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            out.append(f)
    return sorted(out, key=lambda p: str(p))


def find_input_by_key(raw_root: Path, key: str) -> Path | None:
    """Inverse of :func:`input_key`: the file under ``raw_root`` whose key is ``key``."""
    root = Path(raw_root)
    parts = key.split("__")
    for i in range(len(parts)):
        name = "__".join(parts[i:])
        for cand in sorted(root.rglob(name)):
            if cand.is_file() and input_key(cand, root) == key:
                return cand
    return None
```
- [ ] **Step 4: Run helper tests — pass.**
- [ ] **Step 5: Write the failing per-serializer regression** — `tests/data/test_serializer_identity.py`. Two directories holding the same fixture files must yield two inputs, two distinct markers, and distinct `source_file` values:

```python
import shutil
from pathlib import Path

import importlib
import pytest

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "sp4"

SOURCES = ["chembl", "uniprot", "pubchem", "clinvar", "reactome", "mesh", "ontologies", "openalex"]


@pytest.mark.parametrize("source", SOURCES)
def test_same_named_inputs_in_two_directories_are_both_processed(source, tmp_path):
    mod = importlib.import_module(f"episteme.data.{source}.serialize_{source}")
    fn = getattr(mod, f"serialize_{source}")
    raw = tmp_path / "raw"
    for d in ("a", "b"):
        shutil.copytree(FIX / source, raw / d)
    res = fn(raw, tmp_path / "processed")
    assert res["inputs"] == 2, res
    assert res["ok"] == 2 and res["failed"] == 0, res
    markers = sorted(p.name for p in (tmp_path / "processed" / "_ops" / source / "success").glob("*.ok"))
    assert len(markers) == 2 and markers[0] != markers[1], markers
    assert all(m.startswith(("a__", "b__")) for m in markers), markers
```
Run: it fails for all eight (`inputs == 1`, basename collision). If a source's fixture directory needs more than a plain copy (e.g. a source whose discovery keeps only one primary per directory by design), record that in the report and adapt the assertion for that source only — do not weaken the others.
- [ ] **Step 6: Migrate each serializer (mechanical template, same in all eight).** Read each module first; the shape is uniform:
  1. Import `discover_input_files, input_key` from `episteme.data.checkpoint_markers` instead of `list_input_files`.
  2. `discover_<src>_files(raw_dir)`: call `discover_input_files(raw_dir, patterns)`; sort with `key=lambda p: input_key(p, raw_dir)` (was `p.name`). For `ontologies` (custom recursive discovery grouping by parent directory and preferring `.obo`) keep its grouping logic; it already dedups by directory, only the sort/keys change. For `openalex`: delete `_qualified_source_file` and the custom body of `discover_openalex_files` (now just `discover_input_files`), and adjust its tests that referenced them (`test_qualified_source_file_flat_layout_keeps_bare_name` → replace with an `input_key` flat-layout assertion; the two-partition regression test stays and must pass unchanged in intent).
  3. `iter_rows_from_file(path, *, source_file: str | None = None)`: `source_file = source_file or path.name` (existing positional callers/tests keep working).
  4. `process_one(path, processed_dir, *, raw_dir, force, …)`: `basename = input_key(path, raw_dir)`; call `iter_rows_from_file(path, source_file=basename)`; update the call in `serialize_<src>()` to pass `raw_dir=raw_dir`. Marker, audit `object`, shard `source_file=` all use `basename` (already do).
  5. `_run_report`: pass `source_file=input_key(path, raw_dir)` when it iterates rows, so the field-shape table shows the same identity.
  6. `serialize_chembl`'s tarball case: the identity is the tarball's key, never the extracted `.db`'s name (already true; keep).
- [ ] **Step 7: graph_builder seam.** In the mesh phase replace the `for cand in Path(raw_dir).rglob(src_file): …` lookup with `xml_path = find_input_by_key(raw_dir, src_file)` (import it from `checkpoint_markers`). Add a `pg` test to `tests/data/test_graph_builder.py` mirroring `test_build_populates_mesh_hierarchy_gzip` but with the raw file at `raw/nlm/desc2026.xml` and `episteme.articles.source_file = 'nlm__desc2026.xml'`; assert `res["mesh_hierarchy"] == 1`. It fails against the old `rglob(src_file)` lookup.
- [ ] **Step 8: Run everything:** helper tests, `test_serializer_identity.py`, every `tests/data/test_serialize_*.py`, `tests/data/test_graph_builder.py` (pg, `.env` sourced), then the full non-pg suite. Flat-layout tests must pass unmodified — that is the compatibility proof; report any test you had to change and why.
- [ ] **Step 9: Commit** — `feat(sp4.1): shared raw-dir-relative input identity across all eight serializers` — `git add src/episteme/data/checkpoint_markers.py src/episteme/data/graph_builder.py src/episteme/data/{chembl,uniprot,pubchem,clinvar,reactome,mesh,ontologies,openalex}/serialize_*.py tests/data/test_checkpoint_markers.py tests/data/test_serializer_identity.py tests/data/test_graph_builder.py tests/data/test_serialize_openalex.py` (plus any test file you legitimately had to adjust — list them in the report).

---

### Task 4: `source:file:unknown` id fallback → counted skip

**Files:**
- Modify: `serialize_chembl.py:219`, `serialize_clinvar.py:293`, `serialize_pubchem.py:335`, `serialize_reactome.py:415`, `serialize_openalex.py:325` (all under `src/episteme/data/<src>/`)
- Test: extend each module's `tests/data/test_serialize_<src>.py`

**Interfaces:** Consumes Task 3's `iter_rows_from_file(path, *, source_file=None)` signature. The return dict of `serialize_<src>()` stays 4-key.

- [ ] **Step 1: Write the failing test (one per module).** Each builds a tiny input in `tmp_path` where exactly one record lacks its native id (blank `VariationID` cell for clinvar; empty `CID`/pathway id/activity id/OpenAlex `id` for the others — inspect each module's row builder for which field is `native_id`), runs `serialize_<src>`, and asserts: (a) no row whose id ends with `:unknown`; (b) the good record's row is present; (c) the success marker's `stats["skipped_no_id"] == 1`. Example for clinvar:

```python
def test_clinvar_record_without_variation_id_is_counted_and_skipped(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    src = (FX / "sample.tsv").read_text(encoding="utf-8").splitlines()
    header, first = src[0], src[1]
    cols = header.lstrip("#").split("\t")
    bad = first.split("\t")
    bad[cols.index("VariationID")] = ""
    (raw / "variant_summary.txt").write_text("\n".join([header, first, "\t".join(bad)]) + "\n", encoding="utf-8")
    res = serialize_clinvar(raw, tmp_path / "processed")
    assert res["ok"] == 1 and res["failed"] == 0
    df = pl.read_parquet(next((tmp_path / "processed" / "staging" / "clinvar").glob("*.parquet")))
    assert not any(str(i).endswith(":unknown") for i in df["id"].to_list())
    marker = json.loads(next((tmp_path / "processed" / "_ops" / "clinvar" / "success").glob("*.ok")).read_text())
    assert marker["stats"]["skipped_no_id"] == 1
```
  Run each — they fail (a synthesized `…:unknown` id is emitted).
- [ ] **Step 2: Implement (same shape in all five).** The row builder returns `None` when `native_id` is falsy (delete the `…:unknown` expression). `iter_rows_from_file` gains an optional `stats: dict | None = None` keyword; on a `None` row it does `stats["skipped_no_id"] = stats.get("skipped_no_id", 0) + 1` (when `stats` is provided) and `continue`s. `process_one` creates `iter_stats: dict = {}`, passes it, and merges it into the `stats` dict written by `mark_success` (`stats.update(iter_stats)`); `--verbose` prints `skipped_no_id=N` when non-zero.
- [ ] **Step 3: Run each module's test file — pass;** full non-pg suite green.
- [ ] **Step 4: Commit** — `fix(sp4.1): count-and-skip records without a native id instead of synthesizing ids` — `git add` the five modules and their five test files.

---

### Task 5: tarfile compatibility; mesh `--report --max-files` test

**Files:**
- Modify: `src/episteme/data/chembl/serialize_chembl.py` (`_resolve_sqlite_db`, ≈line 146)
- Test: `tests/data/test_serialize_chembl.py`, `tests/data/test_serialize_mesh.py`

- [ ] **Step 1: Write the failing tests.**
  - chembl — a crafted tar with an unsafe member must be rejected on BOTH code paths:
```python
import io
import tarfile

import pytest

import episteme.data.chembl.serialize_chembl as sc


def _tar_with(member_name: str, tmp_path):
    p = tmp_path / "evil.tar.gz"
    with tarfile.open(p, "w:gz") as tf:
        data = b"x"
        info = tarfile.TarInfo(member_name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return p


@pytest.mark.parametrize("has_filter", [True, False])
def test_unsafe_tar_member_is_rejected(tmp_path, monkeypatch, has_filter):
    monkeypatch.setattr(sc, "_HAS_DATA_FILTER", has_filter)
    p = _tar_with("../escape.db", tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(Exception):
        sc._safe_extract(p, out)
    assert not (tmp_path / "escape.db").exists()
```
  - mesh — copy the shape of `test_chembl_report_respects_max_files` in `tests/data/test_serialize_chembl.py`: build two distinct-basename descriptor files from the mesh fixture, run `main(["--raw-dir", …, "--report", "--max-files", "1"])` then with `"2"`, assert the printed `files=` counts differ (1 vs 2).
- [ ] **Step 2: Run — chembl test fails (`_safe_extract`/`_HAS_DATA_FILTER` missing); the mesh test may already fail or pass — if it passes, keep it (it closes the coverage gap).**
- [ ] **Step 3: Implement** in `serialize_chembl.py` (replace the `tf.extractall(..., filter="data")` call site with `_safe_extract`):
```python
_HAS_DATA_FILTER = hasattr(tarfile, "data_filter")


def _safe_extract(archive: Path, dest: Path) -> None:
    """Extract ``archive`` into ``dest`` without trusting member paths.

    Uses ``filter="data"`` when this interpreter has it (3.12+, and the
    3.10.12 / 3.11.4 backports); otherwise validates every member itself.
    """
    dest = Path(dest)
    with tarfile.open(archive) as tf:
        if _HAS_DATA_FILTER:
            tf.extractall(dest, filter="data")  # nosec B202 - filter="data" bounds extraction
            return
        root = dest.resolve()
        for m in tf.getmembers():
            target = (root / m.name).resolve()
            if (
                m.name.startswith(("/", "\\"))
                or not target.is_relative_to(root)
                or m.issym()
                or m.islnk()
                or m.isdev()
            ):
                raise RuntimeError(f"unsafe tar member {m.name!r}")
        tf.extractall(dest)  # nosec B202 - every member validated above
```
- [ ] **Step 4: Run — pass;** existing `test_chembl_tarball_extraction` must still pass unchanged.
- [ ] **Step 5: Commit** — `fix(sp4.1): tarfile data_filter compatibility for chembl; mesh report/max-files test` — `git add src/episteme/data/chembl/serialize_chembl.py tests/data/test_serialize_chembl.py tests/data/test_serialize_mesh.py`.

---

## Phase 3 — Acquisition

### Task 6: `download_uniprot.sh` file order

**Files:** Modify `scripts/data/uniprot/download_uniprot.sh` (the `files=(…)` line, ≈line 50); Test `tests/test_download_wrapper_order.py` (create)

- [ ] **Step 1: Write the failing static test:**
```python
import re
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "data" / "uniprot" / "download_uniprot.sh"


def _files() -> list[str]:
    text = WRAPPER.read_text(encoding="utf-8")
    m = re.search(r"^files=\((.*?)\)\s*$", text, re.M)
    assert m, "files=(…) array not found"
    return m.group(1).split()


def test_fasta_precedes_the_941mb_xml_and_is_reachable_with_a_small_cap():
    files = _files()
    assert files.index("uniprot_sprot.fasta.gz") < files.index("uniprot_sprot.xml.gz")
    assert files.index("uniprot_sprot.fasta.gz") <= 3  # reachable with --max-files 4
```
- [ ] **Step 2: Run — fails** (fasta is currently at index 5, after the xml).
- [ ] **Step 3: Implement** — reorder to: `files=(reldate.txt LICENSE README uniprot_sprot.fasta.gz uniprot.xsd uniprot_sprot.dat.gz uniprot_sprot.xml.gz uniprot_sprot_varsplic.fasta.gz)` (ancillaries first, then the small FASTA, then the large files). Keep the `U-1` comment updated to explain the new order.
- [ ] **Step 4: Run — pass;** `bash -n`; `PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh uniprot download --dry-run --max-files 4` exits 0 and its output lists `uniprot_sprot.fasta.gz`.
- [ ] **Step 5: Commit** — `fix(sp4.1): download_uniprot.sh fetches the FASTA before the 941MB XML` — `git add scripts/data/uniprot/download_uniprot.sh tests/test_download_wrapper_order.py`.

### Task 7: `download_mesh.sh` real NLM endpoint

**Files:** Modify `scripts/data/mesh/download_mesh.sh`, `scripts/data/_lib/sources.env` (only if a new base is needed); Test extend `tests/test_run_pipeline_dispatch.py`

- [ ] **Step 1: Discover (spec open item 1).** With network: for candidate URLs `curl -sSI` (never a body download) — start with `$MESH_BASE/MESH_FILES/xmlmesh/desc<year>.gz` and `…/desc<year>.xml` for the current and previous year, and `$MESH_FTP_BASE/xmlmesh/`. Record which return 200 and their `Content-Length` (the 2025 descriptor release is ≈16.8 MB gzipped / 314 MB unzipped, per SP4 Task 10).
- [ ] **Step 2: Write the failing test** — in `test_run_pipeline_dispatch.py`, a network dry-run test asserting the mesh wrapper now resolves ≥ 1 real file (today it logs `resolved 0 files`):
```python
def test_mesh_download_dry_run_resolves_a_descriptor_release(tmp_path):
    proc = _run(["mesh", "download", "--dry-run"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "resolved 0 files" not in proc.stderr
    assert "desc" in proc.stderr + proc.stdout
```
  Fails today. (If NLM is unreachable the existing dispatch tests skip on rc 1; follow that file's idiom — `pytest.skip` on transient upstream failure rather than failing.)
- [ ] **Step 3: Implement** — replace the `cand_dirs` scan with: for `y` in `$YEAR $PREV`, plan `desc$y.gz` from the discovered base when a `curl -sI` http-code check returns 200 (no `-L`, per the file's existing W-3 comment); the first year that resolves wins. Any new base URL goes into `sources.env` (grep gate). Descriptors only (qualifier/supplemental files are not consumed by any serializer). Keep `--max-files`, `--force`, size-skip and `write_sync_stamp` behavior. Destination stays flat `01_raw/mesh/desc<year>.gz`.
- [ ] **Step 4: Real bounded proof:** `PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh mesh download --max-files 1`, then `gzip -t` on the file and a `serialize_mesh --report` on it; record size and result; `git clean -fdx 01_raw/mesh 02_processed`.
- [ ] **Step 5: Run tests, `bash -n`, grep gate. Commit** — `fix(sp4.1): download_mesh.sh resolves the current NLM descriptor release` — `git add scripts/data/mesh/download_mesh.sh scripts/data/_lib/sources.env tests/test_run_pipeline_dispatch.py` (omit `sources.env` if unchanged).

### Task 8: `download_openalex.sh` real bounded `--max-files`

**Files:** Modify `scripts/data/_lib/common.sh` (new `s3_fetch_first_n`), `scripts/data/openalex/download_openalex.sh`; Test `tests/test_openalex_bounded_fetch.py` (create)

**Interfaces:** Produces `s3_fetch_first_n S3_URI DEST_DIR N` in `common.sh`.

- [ ] **Step 1: Write the failing test** using a fake `aws` on `PATH` so nothing touches the network:
```python
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "data" / "openalex" / "download_openalex.sh"


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _fake_aws(bindir: Path, log: Path) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "aws"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log.as_posix()}"\n'
        'if [ "$2" = "ls" ]; then\n'
        "  printf '2026-06-01 00:00:00        100 data/jsonl/works/updated_date=2026-05-01/part_0000.gz\\n'\n"
        "  printf '2026-06-01 00:00:00        100 data/jsonl/works/updated_date=2026-05-02/part_0000.gz\\n'\n"
        "  printf '2026-06-01 00:00:00        100 data/jsonl/works/updated_date=2026-05-03/part_0000.gz\\n'\n"
        'elif [ "$2" = "cp" ]; then\n'
        '  mkdir -p "$(dirname "$5")"; echo data > "$5"\n'
        "fi\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _run(tmp_path, *args):
    log = tmp_path / "aws.log"
    _fake_aws(tmp_path / "bin", log)
    env = {
        **os.environ,
        "PATH": f"{(tmp_path / 'bin').as_posix()}{os.pathsep}{os.environ['PATH']}",
        "EPISTEME_ACTOR": "episteme_sys_admin",
        "EPISTEME_DATA_ROOT": str(tmp_path),
        "PGDATABASE": "episteme_test",
    }
    proc = subprocess.run([_bash(), str(WRAPPER), *args], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def test_max_files_fetches_exactly_n_objects_in_key_order(tmp_path):
    proc, calls = _run(tmp_path, "--max-files", "2")
    assert proc.returncode == 0, proc.stderr[-2000:]
    cps = [c for c in calls if " cp " in f" {c} "]
    assert len(cps) == 2
    assert "updated_date=2026-05-01" in cps[0] and "updated_date=2026-05-02" in cps[1]
    assert not any(" sync " in f" {c} " for c in calls)


def test_no_max_files_still_uses_sync(tmp_path):
    proc, calls = _run(tmp_path)
    assert any(" sync " in f" {c} " for c in calls)
```
  (The fake assumes the invocations `aws s3 ls --no-sign-request --recursive <uri>/` and `aws s3 cp --no-sign-request <src> <dest>`, so `$2` is the subcommand and `$5` is `cp`'s destination; implement `s3_fetch_first_n` with exactly that argv shape.) Fails today (`--max-files` is ignored). Note `s3_sync` prefers `s5cmd` when installed, so `test_no_max_files_still_uses_sync` assumes `s5cmd` is absent — skip it when `shutil.which("s5cmd")`.
- [ ] **Step 2: Implement.** In `common.sh` add `s3_fetch_first_n`: list with `aws s3 ls --no-sign-request --recursive "$uri/"`, take the object key from the last column, `sort` ascending (deterministic; spec open item 3 — lexicographic order, i.e. oldest partitions first; note the choice in a comment), `head -n "$n"`, then for each key `aws s3 cp --no-sign-request "s3://<bucket>/$key" "$dest/<key relative to the prefix>"`, skipping an object whose local size already equals the listed size; under `EPISTEME_DRY_RUN=1` print the planned `n` keys and fetch nothing. In `download_openalex.sh`: when `MAX_FILES` is a positive integer call `s3_fetch_first_n "$src" "$dst" "$MAX_FILES"`, else the existing `s3_sync`; remove the "`--max-files` is ignored" log line.
- [ ] **Step 3: Run — pass;** `bash -n`; `PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh openalex download --dry-run --max-files 2` lists exactly 2 planned objects (real network listing, no fetch).
- [ ] **Step 4: Real bounded proof:** fetch `--max-files 1` for real (public bucket, no credentials), confirm one shard lands under the wrapper's destination, run `serialize_openalex --report` over it; `git clean -fdx 01_raw/openalex 02_processed`.
- [ ] **Step 5: Commit** — `fix(sp4.1): download_openalex.sh honors --max-files with a bounded S3 fetch` — `git add scripts/data/_lib/common.sh scripts/data/openalex/download_openalex.sh tests/test_openalex_bounded_fetch.py`.

### Task 9: New source `cdisc_bc` — CDISC Biomedical Concepts download wrapper

**Files:**
- Create: `scripts/data/cdisc_bc/download_cdisc_bc.sh`
- Modify: `scripts/data/_lib/sources.env`, `scripts/data/run_pipeline.sh` (the `declare -A WRAPPER` table), `src/episteme/config.py` only if a test enforces a `Settings` field per endpoint (check `grep -rn "chembl_base" tests` first and mirror whatever consistency test exists)
- Test: extend `tests/test_run_pipeline_dispatch.py` (`FAST_TOKENS`), create `tests/test_cdisc_bc_wrapper.py`

**Interfaces:** Produces the `cdisc_bc` source token (download-only; not in `LIT_SOURCES` or `STRUCTURED_SOURCES`, so `serialize`/`extract` already die 3 in early validation).

- [ ] **Step 1: Discover (spec open item 2).** `curl -fsS https://api.github.com/repos/cdisc-org/COSMoS/contents/export?ref=main` (unauthenticated: 60 requests/hour) — record the real file names and sizes in `export/`; `curl -fsS …/commits/main` to see where the commit SHA is; confirm the repo `LICENSE` file name. Decide the exact URL bases and put them in `sources.env` as `COSMOS_API_BASE=https://api.github.com/repos/cdisc-org/COSMoS` and `COSMOS_RAW_BASE=https://raw.githubusercontent.com/cdisc-org/COSMoS` (adjust if discovery shows otherwise).
- [ ] **Step 2: Write the failing tests.**
  - Add `"cdisc_bc"` to `FAST_TOKENS` in `test_run_pipeline_dispatch.py` (its parametrized `download --dry-run` test must pass rc 0).
  - `tests/test_cdisc_bc_wrapper.py`: with a fake `curl` on `PATH` (same shim technique as Task 8) serving a canned contents-API JSON (two files) and a canned commit JSON, run the wrapper for real (not dry-run) into a `tmp_path` data root and assert: the two files are fetched to `<raw>/cdisc_bc/export/`; a `PROVENANCE.txt` exists containing the canned commit SHA, a retrieval timestamp, the repo URL and `CC-BY-4.0`; the repo `LICENSE` is copied next to the data; `--max-files 1` fetches only one data file; `--dry-run` fetches nothing and writes no provenance.
  Also `run_pipeline.sh cdisc_bc serialize` → rc 3 (add to the dispatch tests).
- [ ] **Step 3: Implement** the wrapper, modeled on `scripts/data/clinvar/download_clinvar.sh` / `pubchem/download_pubchem.sh`: same argument loop (`--dry-run`, `--max-files`, `--force`, `--reason`, positional MODE `export` (default) | `yaml`), `require_env EPISTEME_ACTOR COSMOS_API_BASE COSMOS_RAW_BASE`, `dest="$(resolve_dest cdisc_bc)"`. List `$COSMOS_API_BASE/contents/export?ref=main` (mode `yaml`: `contents/yaml`), extract `"name"`/`"download_url"` for entries of `"type": "file"` with `grep`/`sed` (no new tooling), cap with `cap_urls`, skip via `size_match_skip`, fetch with `http_fetch`. Provenance: resolve `$COSMOS_API_BASE/commits/main`'s `sha`, then write `$dest/PROVENANCE.txt` (`source_repo`, `commit_sha`, `retrieved_at` UTC, `content_licence: CC-BY-4.0 (repository content); MIT (code)`) and fetch the repo `LICENSE` to `$dest/LICENSE`; both skipped under `--dry-run`. Register `[cdisc_bc]="cdisc_bc/download_cdisc_bc.sh"` in the `WRAPPER` table; `chmod +x` and `git update-index --chmod=+x`.
- [ ] **Step 4: Run tests, `bash -n`, grep gate.** Real bounded proof: `PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh cdisc_bc download --max-files 2`; confirm files + provenance; `git clean -fdx 01_raw/cdisc_bc 02_processed`.
- [ ] **Step 5: Commit** — `feat(sp4.1): cdisc_bc download wrapper (CDISC Biomedical Concepts via COSMoS)` — `git add scripts/data/cdisc_bc/download_cdisc_bc.sh scripts/data/_lib/sources.env scripts/data/run_pipeline.sh tests/test_run_pipeline_dispatch.py tests/test_cdisc_bc_wrapper.py` (+ `src/episteme/config.py` only if it changed).

---

## Phase 4 — Licences

### Task 10: `normalize_license` recognises `creativecommons.org` URLs

**Files:** Modify `src/episteme/data/article_schema.py` (`normalize_license`, ≈line 156-202); Test `tests/data/test_article_schema.py` (extend, or create if absent)

- [ ] **Step 1: Write the failing tests:**
```python
import pytest

from episteme.data.article_schema import normalize_license, subset_from_license


@pytest.mark.parametrize(
    "raw, code",
    [
        ("http://creativecommons.org/licenses/by/4.0/", "CC BY"),
        ("https://creativecommons.org/licenses/by/3.0/us/", "CC BY"),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0"),
        ("https://creativecommons.org/licenses/by-sa/4.0/", "CC BY-SA"),
        ("https://creativecommons.org/licenses/by-nc/4.0/", "CC BY-NC"),
        ("https://creativecommons.org/licenses/by-nd/4.0/", "CC BY-ND"),
        ("https://creativecommons.org/licenses/by-nc-sa/4.0/", "CC BY-NC-SA"),
        ("https://creativecommons.org/licenses/by-nc-nd/4.0/", "CC BY-NC-ND"),
        ("Licensed under https://creativecommons.org/licenses/by/4.0/ with attribution", "CC BY"),
    ],
)
def test_creativecommons_urls_are_recognised(raw, code):
    lic, url, _raw = normalize_license(raw)
    assert lic == code
    assert url and "creativecommons.org" in url


def test_go_mondo_style_bare_url_is_commercial():
    lic, _u, _r = normalize_license("http://creativecommons.org/licenses/by/4.0/")
    assert subset_from_license(lic) == "commercial"


def test_existing_text_token_behavior_is_unchanged():
    assert normalize_license("CC BY-NC 4.0")[0] == "CC BY-NC"
    assert normalize_license("CC BY 4.0")[0] == "CC BY"
    assert normalize_license("some prose with no licence")[0] == "unknown"
    assert normalize_license("https://example.com/licenses/by/4.0/")[0] == "unknown"
```
  Fails for the bare `licenses/by/` and `publicdomain/zero/` cases.
- [ ] **Step 2: Implement.** Extend the URL-capture regex at the top of `normalize_license` to `r"https?://creativecommons\.org/(?:licenses|publicdomain)/[^\s)\"']+"`. Then, immediately after the existing `CC\s*BY` / "CREATIVE COMMONS ATTRIBUTION" arm and before the `TEXT MINING` arm, add:
```python
    if re.search(r"creativecommons\.org/licenses/by/", s, re.I):
        return "CC BY", url, s[:300]
    if re.search(r"creativecommons\.org/publicdomain/zero/", s, re.I):
        return "CC0", url, s[:300]
```
  Do not reorder or edit any existing arm.
- [ ] **Step 3: Run — pass;** the ontologies tests: confirm the GO/MONDO serializer path now yields `CC BY`/`commercial` for a GO-style header (adjust the existing test that asserted `unknown` for that string, and note it in the report); full non-pg suite.
- [ ] **Step 4: Commit** — `feat(sp4.1): normalize_license recognises creativecommons.org licence URLs` — `git add src/episteme/data/article_schema.py tests/data/test_article_schema.py` (+ the adjusted ontologies test).

### Task 11: `public_domain` class for MeSH, PubChem, ClinVar

**Files:** Modify `src/episteme/data/article_schema.py`, `src/episteme/data/{mesh,pubchem,clinvar}/serialize_*.py`; Test `tests/data/test_article_schema.py`, the three serializers' test files

**Interfaces:** Produces `article_schema.LICENSE_PUBLIC_DOMAIN = "public_domain"`; consumes nothing new.

- [ ] **Step 1: Write the failing tests.**
  - `test_article_schema.py`:
```python
from episteme.data.article_schema import LICENSE_PUBLIC_DOMAIN, normalize_license, subset_from_license


def test_public_domain_code_maps_to_commercial():
    assert LICENSE_PUBLIC_DOMAIN == "public_domain"
    assert subset_from_license(LICENSE_PUBLIC_DOMAIN) == "commercial"


def test_normalize_license_never_assigns_public_domain_from_free_text():
    for raw in (
        "This article is in the public domain.",
        "Public Domain",
        "US Government work, public domain in the United States",
    ):
        assert normalize_license(raw)[0] != LICENSE_PUBLIC_DOMAIN
```
  - In each of the mesh, pubchem, clinvar serializer test files, a row-level assertion (extend the existing rows test): `row["license"] == "public_domain"`, `row["subset"] == "commercial"`, and `row["license_raw"]` still contains the real disclaimer text (assert a distinctive substring: `"National Library of Medicine"` for mesh, `"Fair Use"` for pubchem, the ClinVar module's own `_CLINVAR_LICENSE_RAW` key phrase for clinvar — read the constant). These fail today (`unknown`/`open_metadata`) — and update any existing assertion in those files that pinned `unknown`/`open_metadata`.
- [ ] **Step 2: Implement.** In `article_schema.py`: `LICENSE_PUBLIC_DOMAIN = "public_domain"` and add it to the `commercial` tuple in `subset_from_license`. `normalize_license` is NOT changed. In each of the three serializers, right after the existing `lic, lic_url, lic_raw = normalize_license(_<SRC>_LICENSE_RAW)` call add, with this comment (adapt the source name):
```python
    # GOVERNANCE OVERRIDE (SP4.1 spec 3.4, user decision 2026-09-19): this source's
    # own terms are treated as public domain -> commercial-eligible. Source-anchored
    # on purpose: normalize_license() never returns this for free text. PubChem and
    # ClinVar carry contributor-submitted content with per-record terms; the
    # user accepted that risk. license_raw keeps the real disclaimer text.
    lic = LICENSE_PUBLIC_DOMAIN
```
- [ ] **Step 3: Run — pass;** check `finalize_row`/schema validation accepts the new code (fix only if a closed licence set exists and rejects it, and say so); full non-pg suite; `pg` suite for the loader if the licence set is enumerated anywhere in SQL (`grep -rn "license" src/episteme/data/db/schema.sql`).
- [ ] **Step 4: Commit** — `feat(sp4.1): source-anchored public_domain licence class for MeSH, PubChem, ClinVar` — `git add src/episteme/data/article_schema.py src/episteme/data/{mesh,pubchem,clinvar}/serialize_*.py tests/data/test_article_schema.py tests/data/test_serialize_{mesh,pubchem,clinvar}.py`.

---

## Close-out

**Execution order:** Task 13 runs BEFORE Task 12 (ruling recorded in the ledger): a dependency bump changes the tree, so the final sweep and drift entry must come after it.

### Task 12: Full sweep and drift log

**Files:** Modify `docs/project-incubation-baseline.md`; touch nothing else except a test fixture if a genuine gap is found (mirror SP2/SP4's Task 13 discipline).

- [ ] **Step 1: `not pg` suite** → all green; record the count. **Step 2: full suite** (`set -a; . ./.env; set +a; .venv/Scripts/python.exe -m pytest -q`) → green; record the count.
- [ ] **Step 3: `run_pipeline.sh` sweep** (every command prefixed `PGDATABASE=episteme_test`; `--dry-run` skips the audit bracket): for each of `chembl uniprot pubchem clinvar reactome mesh ontologies openalex`: `serialize --dry-run` → rc 3, `download --dry-run` → rc 0; `cdisc_bc download --dry-run` → rc 0; `cdisc_bc serialize` → rc 3; `pmc serialize` → rc 3; `mesh graph --dry-run` → rc 3; `mesh extract` → rc 3. Transcript into the report.
- [ ] **Step 4: `bash -n`** on every new/modified `.sh`. **Step 5: grep gate** (Global Constraints) → empty.
- [ ] **Step 6: Drift log** — one dated entry (match the SP4 entries' style): SP4.1 landed (spec + plan paths); the DB-mode guard (modes, settings, `.env` keys added, code default `restricted`); **the go-live checklist item — set `EPISTEME_DB_MODE=restricted` in `.env` before any production operation (the user's `.env` is `unrestricted` during development, by decision)**; the shared input identity (and the deferred SP2-extractor migration, incl. bookshelf's hashed tree); the wrapper fixes; `cdisc_bc` (source, licence CC-BY-4.0, provenance behavior, download-only); licence mapping incl. the governance record (all three government-hosted sources, PubChem/ClinVar contributor-content risk stated, decision 2026-09-19); Dependabot outcome (from Task 13's report, which has already run); deferred items (streaming shard writes, UCUM parser, full-scale runs, `cdisc_bc` serializer, SP5).
- [ ] **Step 7: Commit** — `docs(sp4.1): drift-log entry — hardening and acquisition follow-ups landed`.

### Task 13: Dependabot triage

**Files:** Modify `pyproject.toml` / the lock file only if a safe bump is applied; write findings into the Task 12 drift entry.

- [ ] **Step 1: List** — `gh api repos/devopam/Episteme/dependabot/alerts --jq '.[] | select(.state=="open") | [.number, .security_advisory.severity, .dependency.package.name, .dependency.scope, (.security_vulnerability.first_patched_version.identifier // "none")] | @tsv'` (at plan time: 7 open — `cryptography` ×4 with patched versions 48.0.1/49.0.0/50.0.0, `nltk`, `accelerate`, `diskcache` with no patched version).
- [ ] **Step 2: Classify** each: is the package a direct dependency (`pyproject.toml`) or transitive (`uv pip show`/`pip show` "Required-by")? Is a fixed version available? Would the fix be patch/minor or a major bump?
- [ ] **Step 3: Apply only** patch/minor bumps of packages whose fix version exists, and only if the full suite (both runs) stays green after the bump; revert anything that breaks. Do NOT apply major bumps or upgrades with no fixed version — report them. No `git add` of anything but `pyproject.toml`/lock if changed.
- [ ] **Step 4: Report** the table (alert → classification → action/reason) in the task report; Task 12 (which runs after this task) folds it into the drift entry. Commit any applied bump as `chore(sp4.1): apply patch-level security bumps (Dependabot triage)`; if nothing was applied, there is nothing to commit.

---

## Self-Review

**Spec coverage** (`2026-09-19-sp4-1-hardening-design.md`):

| Spec item | Task |
|---|---|
| §3.1 DB-mode guard, settings, modes, `.env` keys, `read-only` option | 1 |
| §3.1 shell-layer message, restricted-mode dispatch test | 2 |
| §3.2 Task 3 shared identity, openalex convention removed, graph_builder seam | 3 |
| §3.2 Task 4 id fallback → counted skip | 4 |
| §3.2 Task 5 tarfile compat, mesh test | 5 |
| §3.3 uniprot / mesh / openalex wrappers | 6 / 7 / 8 |
| §3.3 `cdisc_bc` | 9 |
| §3.4 CC URL arms / `public_domain` + governance record | 10 / 11 (record in 12) |
| §3.5 sweep + drift log + go-live item / Dependabot | 12 / 13 |
| §5 non-goals | not implemented — asserted in Task 12's drift entry |
| §7 open items 1–4 | 7, 9, 8, 1 |

**Placeholder scan:** Discovery steps (mesh URL, COSMoS file names, openalex ordering) give concrete commands and a decision rule rather than leaving blanks; Tasks 3–5's per-module edits give the exact template and say "read each module first" because eight files share one shape and the line numbers will shift after Task 3. The shim tests in Tasks 8–9 note that the fake `aws`/`curl` argv positions must be aligned with the implementation.

**Type/name consistency:** `input_key` / `discover_input_files` / `find_input_by_key` (Task 3) are the only new shared names and are used identically in Tasks 3–5; `check_database_allowed` / `pool_kwargs` / `ProductionDatabaseGuardError` / `DB_MODES` (Task 1) are consumed by Task 2's message assertion (`"Refusing to open a connection to production database"`, defined once in `guard.py`); `LICENSE_PUBLIC_DOMAIN` (Task 11) defined once in `article_schema.py`; the frozen serializer return dict is untouched by Task 4 (counters travel through the marker `stats`).

**Risks:** (a) Task 3 touches eight modules plus the shared helper — mitigated by the flat-layout compatibility proof (all existing tests pass unmodified) and the uniform regression test. (b) Task 1 changes the connection choke point every DB path uses — mitigated by the pure-function unit tests, the defaulted `Settings` fields, and the fact that the user's `.env` keeps `unrestricted`. (c) Tasks 7–9 need live network discovery; each has a dry-run/shim test that does not depend on it plus a bounded real proof. (d) Task 13 could break the environment — bounded by "only if the full suite stays green, otherwise revert".

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-19-sp4-1-hardening.md`.

**Subagent-Driven** (standing preference — no mode question): dispatch a fresh implementer per task, task review after each, an opus whole-branch review at the end. `pg`-marked tests need `TEST_PG_DSN` from `.env` and the live PostgreSQL (both in place) and run against `episteme_test`.
