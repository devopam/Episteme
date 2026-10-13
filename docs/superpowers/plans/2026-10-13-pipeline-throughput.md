# Pipeline throughput on fixed hardware — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extractors use several CPU cores, gzip reads go through python-isal, the loader drops per-row dict building and reports where its time goes, and a `--bulk` load mode drops and always rebuilds the secondary indexes of `episteme.articles`.

**Architecture:**
- A new `episteme.data.parallel.run_per_file` replaces the eight per-module `ThreadPoolExecutor` blocks. It uses a spawn-context `ProcessPoolExecutor`.
- A new `episteme.data.gz.open_gz` picks isal or gzip.
- `postgres_loader` builds COPY tuples from Arrow columns.
- A new `episteme.data.db.bulk_indexes` module does the bulk index work, wrapped by `scripts/data/db/bulk_load_indexes.sh`. Its state lives in migration 0005's `episteme._bulk_index_state`. `run_pipeline.sh <src> load --bulk` drives it, with an EXIT/INT/TERM trap.

**Tech Stack:** Python 3.13, `concurrent.futures` (spawn), pyarrow, psycopg 3, python-isal (`isal`), Git Bash, PostgreSQL 19beta3, pytest.

**Spec:** `docs/superpowers/specs/2026-10-13-pipeline-throughput-design.md` (approved 2026-10-13). Executors read both.

**Worktree:** `C:\Users\devop\GitHub\Episteme-throughput`, branch `feat/pipeline-throughput`. All paths below are relative to it. Run Python as `.venv/Scripts/python.exe`.

## Global Constraints

- `src/episteme/config.py` is the only module that reads `os.environ`.
- Endpoint URLs live only in `scripts/data/_lib/sources.env`.
- **Secrets:**
  - Never read, print, cat, grep or copy `.env`. Never run `env | grep` or dump the environment.
  - To load it into a shell: `set -a; . ../Episteme/.env; set +a`, never printed.
  - Docs name env keys only, never values.
- **Git:**
  - `git add` explicit paths only. Never stage `.gitignore` or `.env`. Never `--no-verify`.
  - Run `.venv/Scripts/python.exe -m ruff format <files>` and `.venv/Scripts/python.exe -m ruff check <files>` on changed Python before each commit.
  - Every commit message ends with:
    ```
    Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
    Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
    ```
- Worker processes use `multiprocessing.get_context("spawn")`, explicitly.
- **Worker count:**
  - Env key `EPISTEME_EXTRACT_WORKERS`; `Settings.extract_workers` defaults to 3.
  - A non-integer or a value below 1 logs a warning and falls back to 3.
  - CLI `--workers` defaults to `None`, meaning "use the setting".
- **Bulk mode indexes:**
  - Covered, exactly these six: `articles_pmid_idx`, `articles_pmcid_idx`, `articles_doi_idx`, `articles_mesh_gin`, `articles_authors_gin`, `articles_publication_types_gin`.
  - Kept: `articles_source_file_idx`, `articles_id_idx`, `articles_retrieved_at_brin` and any primary keys.
- **Saved index definitions:**
  - A saved definition must match `^CREATE INDEX \S+ ON ONLY episteme\.articles USING `.
  - On rebuild, exactly one ` ON ONLY ` becomes ` ON `.
- New audit event types: `bulk_index_drop` and `bulk_index_rebuild`.
- Migration file: `src/episteme/data/db/migrations/0005_bulk_index_state.sql`. Table: `episteme._bulk_index_state(index_name text PRIMARY KEY, definition text NOT NULL, run_id text, dropped_at timestamptz NOT NULL DEFAULT now())`.
- The bulk entry point connects as `episteme_sys_admin` through `admin_dsn_from_settings()`, which runs `check_database_allowed` first.
- **Test isolation:**
  - Non-`pg` tests must never reach a database or the real audit mirror.
  - Spawned workers do NOT inherit the monkeypatches in `tests/conftest.py`. Every test that starts worker processes or subprocesses uses the `spawn_child_env` fixture from Task 2.
- **`pg` tests:**
  - Local only, against `episteme_test` through `TEST_PG_DSN`.
  - Before the first `pg` run in a session, the controller confirms `('episteme_test','19beta3')` through MCPg (`run_select`: `select current_database(), current_setting('server_version')`).
  - Run them with `.env` sourced: `set -a; . ../Episteme/.env; set +a; .venv/Scripts/python.exe -m pytest -m pg <files>`. Without `TEST_PG_DSN` they skip; say so in the report.
- Do not type `psql` in Bash/PowerShell; a hook blocks it. DB inspection goes through the MCPg tools.

## Review Focus

These are the input classes and failure modes most likely to bite, which the spec's own test list does not pin down. Each has a test in the task that owns the code.

1. **Spawned workers escaping test isolation.** A child process imports `episteme` afresh, so `_block_real_db_audit` and `_redirect_audit_mirror` do not reach it. With this machine's `.env`, a non-pg multi-worker test would write `extract_commit` rows into the production audit chain. Expected: children see a guard-refused database and a tmp mirror. Pinned by `test_spawned_child_is_kept_off_any_database` (Task 2).
2. **The production entry point.** Production runs `python -m episteme.data.pubmed.extract_pubmed`, where `process_one` lives in `__main__` and spawn must re-import it as `__mp_main__`. In-process pytest calls never exercise this. Expected: `--workers 2` from the CLI works. Pinned by `test_cli_module_entry_point_with_worker_processes` (Task 4).
3. **Concurrent mirror appends from worker processes.** `mirror_only()` (the extractors' DB-down fallback) takes no lock. Appends from several processes to one JSONL file are not atomic on Windows. Expected: every line whole and parseable. Pinned by Task 3's tests.
4. **A shard missing an optional column** (an older shard, or a writer that omits an all-null column). The dict path gave `None` through `r.get()`. Expected: the column-wise path gives `None` too. Pinned by `test_missing_optional_column_reads_as_none` (Task 6).
5. **A same-named but invalid index at rebuild.** An operator might run a saved `ON ONLY` definition by hand, or a crashed `CREATE INDEX` might leave one behind. Expected: rebuild drops the invalid index and builds a valid one; it does not skip it. Pinned by `test_rebuild_replaces_an_invalid_index_of_the_same_name` (Task 7).

## Rulings on spec gaps (made while planning)

- **`graph_builder`:** the spec says the pubmed graph re-reads XML with gzip. It does not. `graph_builder.py`'s only gzip reader is `_open_mesh_source` (the MeSH graph). Task 5 switches that one; the drift log records the correction.
- **Loader load-equivalence test:** the old loader does not survive the change, so it cannot run side by side in `pg`. Equivalence is proven at the COPY boundary instead: a non-pg test asserts that the column-wise path sends exactly the tuples the old dict path sent (the old path is kept as a test helper). The existing `pg` loader suite (`tests/data/test_postgres_loader.py`) runs unchanged against the new path.
- **Mirror lock:** the spec's audit-safety paragraph covers `record()` only (advisory lock). Task 3 adds a cross-process file lock around every mirror append. The spec implies this is needed but does not list it.
- **`rows_per_sec`:** rows divided by the wall time of the whole `load_source_file` call. `read_sec` and `copy_sec` are the split of the row-streaming phase.
- **Rebuild validation:** `rebuild` validates every saved definition before it builds anything, so one bad row stops the run with nothing half done. A definition that passes validation but fails while building stops the run after the indexes already rebuilt.
- **Rebuild on success:** on a successful load, `run_pipeline.sh` rebuilds before `run_end`, so `bulk_index_rebuild` sits inside the run's audit bracket. The trap is the safety net for failed or interrupted loads.

---

## File map

| File | Status | Responsibility |
|---|---|---|
| `src/episteme/config.py` | modify | `extract_workers`, `sys_admin_password`; lenient positive-int helper |
| `src/episteme/data/db/connection.py` | modify | `admin_dsn(settings)`, `admin_dsn_from_settings()` |
| `src/episteme/data/parallel.py` | create | `run_per_file` |
| `src/episteme/audit_trail.py` | modify | cross-process mirror append lock; two new event types |
| `src/episteme/data/{pubmed,pmc,bookshelf,guidelines,apollo}/extract_*.py`, `europepmc/{preprints,manuscripts}/extract_*.py`, `openalex/serialize_openalex.py` | modify | call `run_per_file`; CLI default from the setting |
| `src/episteme/data/gz.py` | create | `open_gz` |
| `extract_pubmed.py`, `graph_builder.py`, `serialize_mesh.py`, `serialize_uniprot.py`, `serialize_openalex.py` | modify | use `open_gz` |
| `src/episteme/data/postgres_loader.py` | modify | column batches, tuple builder, timing |
| `src/episteme/data/load_articles.py` | modify | print timing fields |
| `src/episteme/data/db/migrations/0005_bulk_index_state.sql` | create | state table |
| `src/episteme/data/db/bulk_indexes.py` | create | `drop` / `rebuild` / `status` / `main` |
| `scripts/data/db/bulk_load_indexes.sh` | create | operator wrapper |
| `scripts/data/run_pipeline.sh` | modify | `--bulk` |
| `pyproject.toml`, `uv.lock` | modify | `isal` in the `data` extra |
| `tests/conftest.py` | modify | block `admin_dsn_from_settings`; `spawn_child_env` fixture |
| tests (new) | create | `tests/test_db_admin_dsn.py`, `tests/data/test_parallel.py`, `tests/data/test_audit_mirror_lock.py`, `tests/data/test_extract_workers.py`, `tests/data/test_gz.py`, `tests/data/test_postgres_loader_columns.py`, `tests/data/test_bulk_indexes_unit.py`, `tests/data/test_bulk_indexes.py` (pg), `tests/test_run_pipeline_bulk.py` |
| tests (modify) | modify | `tests/test_config.py`, `tests/docs/test_gxp_doc.py`, `tests/docs/test_runbook_doc.py` |
| docs | modify | `.env.example`, `docs/10-data-sources-runbook.md`, `docs/08-data-storage-principles.md`, `docs/11-gxp-data-integrity.md`, `docs/project-incubation-baseline.md` |

---

### Task 1: Settings and the admin DSN

**Files:**
- Modify: `src/episteme/config.py`
- Modify: `src/episteme/data/db/connection.py`
- Modify: `tests/conftest.py` (`_block_real_db_audit`)
- Modify: `tests/test_config.py`
- Create: `tests/test_db_admin_dsn.py`
- Modify: `.env.example`

**Interfaces:**
- Produces:
  - `Settings.extract_workers: int` (default 3) and `Settings.sys_admin_password: str` (default `""`).
  - `episteme.config.DEFAULT_EXTRACT_WORKERS = 3`.
  - `episteme.data.db.connection.SYS_ADMIN_ROLE = "episteme_sys_admin"`.
  - `admin_dsn(settings) -> str` (pure: guard, then a conninfo string).
  - `admin_dsn_from_settings() -> str`.

- [ ] **Step 1: Write the failing config tests.** Append to `tests/test_config.py` (it already has `fresh_config`, `pytest`). Add `import logging` at the top:

```python
def test_extract_workers_default(fresh_config):
    assert fresh_config("").get_settings().extract_workers == 3


def test_extract_workers_override(fresh_config):
    assert fresh_config("EPISTEME_EXTRACT_WORKERS=5\n").get_settings().extract_workers == 5


@pytest.mark.parametrize("bad", ["abc", "0", "-2", "2.5"])
def test_extract_workers_invalid_falls_back_with_warning(fresh_config, caplog, bad):
    cfg = fresh_config(f"EPISTEME_EXTRACT_WORKERS={bad}\n")
    with caplog.at_level(logging.WARNING, logger="episteme.config"):
        assert cfg.get_settings().extract_workers == 3
    assert "EPISTEME_EXTRACT_WORKERS" in caplog.text


def test_sys_admin_password_read(fresh_config):
    s = fresh_config("EPISTEME_SYS_ADMIN_PASSWORD=s3cret\n").get_settings()
    assert s.sys_admin_password == "s3cret"


def test_sys_admin_password_default_empty(fresh_config):
    assert fresh_config("").get_settings().sys_admin_password == ""
```

- [ ] **Step 2: Write the failing admin-DSN tests.** Create `tests/test_db_admin_dsn.py`:

```python
"""admin_dsn: the table-owner DSN for bulk-load index work. No DB."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from psycopg.conninfo import conninfo_to_dict

from episteme.config import ConfigError
from episteme.data.db import connection
from episteme.data.db.guard import ProductionDatabaseGuardError


def _settings(**over):
    base = {
        "db_mode": "unrestricted",
        "pg_database": "episteme_test",
        "production_database": "episteme",
        "pg_host": "localhost",
        "pg_port": 5433,
        "sys_admin_password": "p w'x",
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_admin_dsn_uses_sys_admin_role_and_quotes_password():
    d = conninfo_to_dict(connection.admin_dsn(_settings()))
    assert d["user"] == "episteme_sys_admin"
    assert d["password"] == "p w'x"
    assert d["dbname"] == "episteme_test"
    assert d["host"] == "localhost"
    assert d["port"] == "5433"


def test_admin_dsn_runs_the_guard_before_anything_else():
    # empty password too: the guard must fire first
    with pytest.raises(ProductionDatabaseGuardError):
        connection.admin_dsn(
            _settings(db_mode="restricted", pg_database="episteme", sys_admin_password="")
        )


def test_admin_dsn_requires_the_password():
    with pytest.raises(ConfigError, match="EPISTEME_SYS_ADMIN_PASSWORD"):
        connection.admin_dsn(_settings(sys_admin_password=""))


def test_admin_dsn_from_settings_is_blocked_in_non_pg_tests():
    with pytest.raises(RuntimeError, match="blocked"):
        connection.admin_dsn_from_settings()
```

- [ ] **Step 3: Run the tests and confirm they fail.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py tests/test_db_admin_dsn.py -q`
  - Expected: FAIL (`extract_workers` attribute missing; `admin_dsn` missing).

- [ ] **Step 4: Implement `config.py`.**
  - Add `import logging` with the other imports, and `_LOG = logging.getLogger(__name__)` after them.
  - Add `DEFAULT_EXTRACT_WORKERS = 3` next to `DB_MODES`.
  - After `_get_int`, add:

```python
def _get_positive_int_or_default(name: str, default: int) -> int:
    """Like ``_get_int`` but never raises: a non-integer or a value below 1
    logs a warning and returns ``default`` (a bad worker count must not stop
    a run)."""
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        _LOG.warning("%s must be an integer >= 1, got %r; using %d", name, raw, default)
        return default
    return value
```

  - Append two fields at the END of the `Settings` dataclass (after `duckdb_memory_limit`), so existing constructors keep working:

```python
    extract_workers: int = DEFAULT_EXTRACT_WORKERS
    sys_admin_password: str = ""
```

  - In `get_settings()`'s `Settings(...)` call, after `duckdb_memory_limit=...`, add:

```text
        # Worker processes per extract/serialize stage (physical cores minus one).
        extract_workers=_get_positive_int_or_default(
            "EPISTEME_EXTRACT_WORKERS", DEFAULT_EXTRACT_WORKERS
        ),
        # episteme_sys_admin's password: bulk-load index work runs as the table owner.
        sys_admin_password=_get("EPISTEME_SYS_ADMIN_PASSWORD", "") or "",
```

- [ ] **Step 5: Implement `connection.py`.** Add `from psycopg.conninfo import make_conninfo`, and change the config import to `from episteme.config import ConfigError, get_settings`. Then add, after `dsn_from_settings`:

```python
SYS_ADMIN_ROLE = "episteme_sys_admin"


def admin_dsn(settings) -> str:
    """DSN for the table owner, ``episteme_sys_admin``: dropping and creating
    the indexes of ``episteme.articles`` needs it, and ``episteme_app`` cannot.
    Runs the same DB-mode guard as ``dsn_from_settings`` first. ``make_conninfo``
    quotes the password, so special characters are safe."""
    check_database_allowed(settings)
    if not settings.sys_admin_password:
        raise ConfigError(
            "EPISTEME_SYS_ADMIN_PASSWORD is not set (needed to connect as episteme_sys_admin)"
        )
    return make_conninfo(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_database,
        user=SYS_ADMIN_ROLE,
        password=settings.sys_admin_password,
    )


def admin_dsn_from_settings() -> str:
    return admin_dsn(get_settings())
```

- [ ] **Step 6: Block it in non-pg tests.**
  - In `tests/conftest.py::_block_real_db_audit`, after the existing `dsn_from_settings` line, add:

```text
    monkeypatch.setattr(
        "episteme.data.db.connection.admin_dsn_from_settings", _no_db, raising=True
    )
```

  - In that fixture's docstring, change "all three chokepoints" to "all four chokepoints". Add one sentence: "``admin_dsn_from_settings`` is the bulk-index entry point's DSN builder (table-owner role)."

- [ ] **Step 7: Update `.env.example`.** After the `EPISTEME_DUCKDB_MEMORY_LIMIT` line (or, if it is absent, after the other `EPISTEME_*` tuning keys), add one commented line in the file's existing style:

```
# EPISTEME_EXTRACT_WORKERS=3              # worker processes per extract/serialize stage (default 3)
```

- [ ] **Step 8: Run the tests and confirm they pass.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py tests/test_db_admin_dsn.py tests/test_db_guard.py tests/test_conftest_db_guard.py -q`
  - Expected: PASS.

- [ ] **Step 9: Run the whole non-pg suite.**
  - Run: `.venv/Scripts/python.exe -m pytest -m "not pg" -q -x`
  - Expected: PASS (network-dependent modules may skip).

- [ ] **Step 10: Commit.**

```bash
.venv/Scripts/python.exe -m ruff format src/episteme/config.py src/episteme/data/db/connection.py tests/conftest.py tests/test_config.py tests/test_db_admin_dsn.py
.venv/Scripts/python.exe -m ruff check src/episteme/config.py src/episteme/data/db/connection.py tests/conftest.py tests/test_config.py tests/test_db_admin_dsn.py
git add src/episteme/config.py src/episteme/data/db/connection.py tests/conftest.py tests/test_config.py tests/test_db_admin_dsn.py .env.example
git commit -F - <<'EOF'
feat(config): extract worker count and the episteme_sys_admin DSN

EPISTEME_EXTRACT_WORKERS (default 3, invalid values fall back with a
warning) and EPISTEME_SYS_ADMIN_PASSWORD join Settings. admin_dsn()
runs the DB-mode guard, then builds a quoted conninfo for the table
owner; non-pg tests block admin_dsn_from_settings like the other DSN
builders.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
```

---

### Task 2: `run_per_file` and the spawn test fixture

**Files:**
- Create: `src/episteme/data/parallel.py`
- Modify: `tests/conftest.py` (new fixture `spawn_child_env`)
- Create: `tests/data/test_parallel.py`

**Interfaces:**
- Consumes: `episteme.data.checkpoint_markers.input_key(path, raw_dir) -> str`.
- Produces:
  - `run_per_file(fn, files, *, workers, on_result=None, **kwargs) -> list[dict]`.
  - Fixture `spawn_child_env` (returns the `Path` that children see as `processed_root`).

- [ ] **Step 1: Add the fixture.** In `tests/conftest.py`, after `_redirect_audit_mirror`, add:

```python
@pytest.fixture
def spawn_child_env(monkeypatch, tmp_path):
    """Environment for a test that starts worker processes or subprocesses.

    A spawned child imports ``episteme`` afresh: none of this file's
    monkeypatches (``_block_real_db_audit``, ``_redirect_audit_mirror``) reach
    it, and its ``get_settings()`` would load the developer's ``.env`` (which
    may point at the production database). Spawn copies the parent's
    ``os.environ``, and real environment values beat ``.env``, so this pins
    the child to:

    * a database the DB-mode guard refuses before any connection attempt
      (``restricted`` mode, ``PGDATABASE`` == ``EPISTEME_PRODUCTION_DATABASE``),
      on a dead host/port as a second wall -- the extractors' best-effort
      audit then falls back to ``mirror_only``;
    * a ``processed_root`` (and so an audit mirror) under ``tmp_path``.

    Returns that ``processed_root``.
    """
    processed = tmp_path / "spawn_processed"
    monkeypatch.setenv("EPISTEME_DB_MODE", "restricted")
    monkeypatch.setenv("EPISTEME_DB_TARGET", "primary")
    monkeypatch.delenv("PGDATABASE_SECONDARY", raising=False)
    monkeypatch.setenv("PGDATABASE", "episteme_spawn_blocked")
    monkeypatch.setenv("EPISTEME_PRODUCTION_DATABASE", "episteme_spawn_blocked")
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    monkeypatch.setenv("PGPORT", "9")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(processed))
    monkeypatch.setenv("EPISTEME_ACTOR", "pytest-spawn")
    monkeypatch.delenv("EPISTEME_RUN_ID", raising=False)
    return processed
```

- [ ] **Step 2: Write the failing tests.** Create `tests/data/test_parallel.py`:

```python
"""run_per_file: in-process for one worker, spawned processes above one.

Every helper here is module-level: a spawned child imports this module by name.
Every multi-worker test takes ``spawn_child_env`` (see tests/conftest.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from episteme.data.checkpoint_markers import input_key
from episteme.data.parallel import run_per_file


def _echo(path: Path, *, raw_dir: Path, tag: str) -> dict:
    return {"source_file": path.name, "skipped": False, "ok": True, "tag": tag, "pid": os.getpid()}


def _boom_on_b(path: Path, *, raw_dir: Path) -> dict:
    if path.name == "b.txt":
        raise RuntimeError("kaboom")
    return {"source_file": path.name, "skipped": False, "ok": True}


def _exit_on_b(path: Path, *, raw_dir: Path) -> dict:
    if path.name == "b.txt":
        os._exit(3)
    return {"source_file": path.name, "skipped": False, "ok": True}


def _probe_child(path: Path, *, raw_dir: Path) -> dict:
    from episteme.config import get_settings
    from episteme.data.db.connection import dsn_from_settings
    from episteme.data.db.guard import ProductionDatabaseGuardError

    try:
        dsn_from_settings()
        refused = False
    except ProductionDatabaseGuardError:
        refused = True
    return {
        "source_file": path.name,
        "skipped": False,
        "ok": True,
        "refused": refused,
        "processed_root": str(get_settings().processed_root),
        "pid": os.getpid(),
    }


def _files(tmp_path: Path, names=("a.txt", "b.txt", "c.txt", "d.txt")):
    raw = tmp_path / "raw"
    raw.mkdir()
    out = []
    for n in names:
        p = raw / n
        p.write_text(n, encoding="utf-8")
        out.append(p)
    return raw, out


def test_one_worker_runs_in_order_in_process(tmp_path):
    raw, files = _files(tmp_path)
    seen: list[dict] = []
    results = run_per_file(_echo, files, workers=1, on_result=seen.append, raw_dir=raw, tag="t")
    assert [r["source_file"] for r in results] == ["a.txt", "b.txt", "c.txt", "d.txt"]
    assert {r["pid"] for r in results} == {os.getpid()}
    assert seen == results


def test_three_workers_same_results_in_child_processes(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path)
    seen: list[dict] = []
    one = run_per_file(_echo, files, workers=1, raw_dir=raw, tag="t")
    three = run_per_file(_echo, files, workers=3, on_result=seen.append, raw_dir=raw, tag="t")

    def strip(rs):
        return sorted((r["source_file"], r["tag"], r["ok"]) for r in rs)

    assert strip(three) == strip(one)
    assert os.getpid() not in {r["pid"] for r in three}
    # on_result ran in this (parent) process: seen was filled here
    assert strip(seen) == strip(three)


def test_escaped_exception_becomes_a_failed_result(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path)
    results = run_per_file(_boom_on_b, files, workers=3, raw_dir=raw)
    by = {r["source_file"]: r for r in results}
    assert set(by) == {"a.txt", "b.txt", "c.txt", "d.txt"}
    assert by["b.txt"]["ok"] is False
    assert by["b.txt"]["skipped"] is False
    assert by["b.txt"]["error"].startswith("worker: ")
    assert "kaboom" in by["b.txt"]["error"]
    assert all(by[n]["ok"] for n in ("a.txt", "c.txt", "d.txt"))


def test_dead_worker_fails_the_remaining_files_without_raising(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path)
    results = run_per_file(_exit_on_b, files, workers=2, raw_dir=raw)
    assert sorted(r["source_file"] for r in results) == ["a.txt", "b.txt", "c.txt", "d.txt"]
    b = next(r for r in results if r["source_file"] == "b.txt")
    assert b["ok"] is False
    assert b["error"].startswith("worker: ")


def test_failed_result_uses_input_key_relative_to_raw_dir(tmp_path, spawn_child_env):
    raw = tmp_path / "raw"
    (raw / "sub").mkdir(parents=True)
    p = raw / "sub" / "b.txt"
    p.write_text("x", encoding="utf-8")
    [r] = run_per_file(_boom_on_b, [p], workers=2, raw_dir=raw)
    assert r["source_file"] == input_key(p, raw)


def test_no_files_returns_empty_without_starting_a_pool(tmp_path, monkeypatch):
    import episteme.data.parallel as par

    def _no_pool(*_a, **_k):
        raise AssertionError("pool started for zero files")

    monkeypatch.setattr(par, "ProcessPoolExecutor", _no_pool)
    assert run_per_file(_echo, [], workers=3, raw_dir=tmp_path, tag="t") == []


def test_more_workers_than_files(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path, ("a.txt",))
    [r] = run_per_file(_echo, files, workers=8, raw_dir=raw, tag="t")
    assert r["ok"] is True


@pytest.mark.parametrize("bad", [0, -4])
def test_workers_below_one_runs_in_process(tmp_path, bad):
    raw, files = _files(tmp_path, ("a.txt", "b.txt"))
    results = run_per_file(_echo, files, workers=bad, raw_dir=raw, tag="t")
    assert {r["pid"] for r in results} == {os.getpid()}


def test_spawned_child_is_kept_off_any_database(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path, ("a.txt", "b.txt"))
    results = run_per_file(_probe_child, files, workers=2, raw_dir=raw)
    assert len(results) == 2
    for r in results:
        assert r["pid"] != os.getpid()
        assert r["refused"] is True
        assert Path(r["processed_root"]) == spawn_child_env
```

- [ ] **Step 3: Run the tests and confirm they fail.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/data/test_parallel.py -q`
  - Expected: FAIL (`ModuleNotFoundError: episteme.data.parallel`).

- [ ] **Step 4: Implement.** Create `src/episteme/data/parallel.py`:

```python
"""Per-file fan-out for the extractors and the openalex serializer.

Parsing XML and JSON in Python is CPU-bound, so threads stay on one core
(the GIL). ``run_per_file`` runs one call per input file in worker
*processes* instead.

* ``workers <= 1``: in order, in this process (the old single-threaded path).
* ``workers > 1``: a ``ProcessPoolExecutor`` with the ``spawn`` start method --
  the only one on Windows, chosen explicitly so Linux CI behaves the same.

``fn`` must be a module-level function and every argument picklable (``Path``,
``bool``, ``int``, ``str``). Children inherit the environment, so
``get_settings()`` (run id, roots, DB mode) matches the parent's.

An exception that escapes ``fn`` (every ``process_one`` catches its own, so
this means a crash or a pickling error) becomes a failed result and the run
continues, as one failing file does today. A worker that dies breaks the
pool: every file still pending fails the same way, logged once.
"""

from __future__ import annotations

import logging
import multiprocessing
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

from episteme.data.checkpoint_markers import input_key

_LOG = logging.getLogger(__name__)


def _failed_result(path: Path, raw_dir: Path | None, exc: BaseException) -> dict[str, Any]:
    key = input_key(path, raw_dir) if raw_dir is not None else Path(path).name
    return {"source_file": key, "skipped": False, "ok": False, "error": f"worker: {exc!r}"}


def run_per_file(
    fn: Callable[..., dict[str, Any]],
    files: Sequence[Path],
    *,
    workers: int,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Call ``fn(path, **kwargs)`` for every path; return the results.

    With one worker the results come back in ``files`` order; with more, in
    completion order. ``on_result`` always runs in this (the parent) process.
    """
    files = list(files)
    workers = max(1, int(workers))
    results: list[dict[str, Any]] = []

    def _emit(result: dict[str, Any]) -> None:
        results.append(result)
        if on_result is not None:
            on_result(result)

    if not files:
        return results
    if workers == 1:
        for path in files:
            _emit(fn(path, **kwargs))
        return results

    raw_dir = kwargs.get("raw_dir")
    broken_logged = False
    with ProcessPoolExecutor(
        max_workers=min(workers, len(files)),
        mp_context=multiprocessing.get_context("spawn"),
    ) as ex:
        futures = {ex.submit(fn, path, **kwargs): path for path in files}
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                result = fut.result()
            except BrokenProcessPool as exc:
                if not broken_logged:
                    _LOG.error("a worker process died (%r); failing every file still pending", exc)
                    broken_logged = True
                result = _failed_result(path, raw_dir, exc)
            except Exception as exc:  # noqa: BLE001 - one file's failure must not stop the run
                _LOG.error("worker failed on %s: %r", path, exc)
                result = _failed_result(path, raw_dir, exc)
            _emit(result)
    return results
```

- [ ] **Step 5: Run the tests and confirm they pass.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/data/test_parallel.py -q`
  - Expected: PASS. Spawned children take a few seconds to import.

- [ ] **Step 6: Commit.**

```bash
.venv/Scripts/python.exe -m ruff format src/episteme/data/parallel.py tests/conftest.py tests/data/test_parallel.py
.venv/Scripts/python.exe -m ruff check src/episteme/data/parallel.py tests/conftest.py tests/data/test_parallel.py
git add src/episteme/data/parallel.py tests/conftest.py tests/data/test_parallel.py
git commit -F - <<'EOF'
feat(data): run_per_file, per-file fan-out over spawned processes

One worker keeps today's in-order, in-process path; more use a spawn
ProcessPoolExecutor. Escaped exceptions and a dead worker become
ok=False results. The spawn_child_env test fixture keeps spawned
children off any database and the real audit mirror, since conftest
monkeypatches do not reach them.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
```

---

### Task 3: Cross-process lock for audit mirror appends

**Files:**
- Modify: `src/episteme/audit_trail.py`
- Create: `tests/data/test_audit_mirror_lock.py`

**Interfaces:**
- Consumes: `run_per_file` (Task 2), `spawn_child_env` (Task 2).
- Produces: `audit_trail._mirror_append_lock(mirror_dir: Path)`, a context manager. `record()` and `mirror_only()` both append under it.

- [ ] **Step 1: Write the failing tests.** Create `tests/data/test_audit_mirror_lock.py`:

```python
"""Mirror appends from several worker processes stay whole lines."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from episteme.data.parallel import run_per_file


def _hammer(path: Path, *, raw_dir: Path, n: int) -> dict:
    from episteme import audit_trail

    for i in range(n):
        audit_trail.mirror_only("extract_commit", object=f"{path.name} {i}", note="x" * 2000)
    return {"source_file": path.name, "skipped": False, "ok": True}


def test_mirror_only_takes_the_append_lock(monkeypatch):
    from episteme import audit_trail

    calls: list[Path] = []
    real = audit_trail._mirror_append_lock

    @contextmanager
    def _spy(mirror_dir):
        calls.append(mirror_dir)
        with real(mirror_dir):
            yield

    monkeypatch.setattr(audit_trail, "_mirror_append_lock", _spy)
    audit_trail.mirror_only("extract_commit", object="x")
    assert len(calls) == 1


def test_concurrent_mirror_only_appends_stay_whole_lines(tmp_path, spawn_child_env):
    raw = tmp_path / "raw"
    raw.mkdir()
    files = []
    for k in range(4):
        p = raw / f"w{k}"
        p.write_text("x", encoding="utf-8")
        files.append(p)
    results = run_per_file(_hammer, files, workers=4, raw_dir=raw, n=200)
    assert all(r["ok"] for r in results)

    lines: list[str] = []
    for f in sorted((spawn_child_env / "_ops" / "_audit").glob("audit-*.jsonl")):
        lines += f.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 800
    objects = sorted(json.loads(line)["object"] for line in lines)
    assert objects == sorted(f"w{k} {i}" for k in range(4) for i in range(200))
```

- [ ] **Step 2: Run the tests and confirm they fail.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/data/test_audit_mirror_lock.py -q`
  - Expected: `test_mirror_only_takes_the_append_lock` FAILS (`AttributeError: _mirror_append_lock`). The concurrency test may fail or may pass by luck; the spy test is the deterministic red.

- [ ] **Step 3: Implement.** In `src/episteme/audit_trail.py`:
  - Add imports: `import time`, `from collections.abc import Iterator`, `from contextlib import contextmanager`, `from pathlib import Path`. Add only the ones that are missing.
  - After `_mirror_path()`, add:

```python
_MIRROR_LOCK_NAME = ".mirror.lock"
_MIRROR_LOCK_TIMEOUT_SEC = 60.0


@contextmanager
def _mirror_append_lock(mirror_dir: Path) -> Iterator[None]:
    """Exclusive lock, across processes, for one append to the mirror.

    The extractors run in worker processes (``episteme.data.parallel``).
    ``record()``'s advisory lock serialises ``record()`` callers only;
    ``mirror_only()`` never takes it, and an append is not atomic across
    processes on Windows. So every mirror append holds this lock: a one-byte
    lock on a sidecar file in the mirror directory (the ``audit-*.jsonl``
    globs never match it).
    """
    with open(Path(mirror_dir) / _MIRROR_LOCK_NAME, "a+b") as lf:
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + _MIRROR_LOCK_TIMEOUT_SEC
            lf.seek(0)
            while True:
                try:
                    msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.01)
            try:
                yield
            finally:
                lf.seek(0)
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
```

  - In `record()`, replace the mirror `try:` body with:

```python
    try:
        mirror_dir = _mirror_dir()
        mirror_dir.mkdir(parents=True, exist_ok=True)
        with (
            _mirror_append_lock(mirror_dir),
            open(_mirror_path(), "a", encoding="utf-8") as fh,
        ):
            fh.write(_canonical_json(mirror_record) + "\n")
    except OSError:
```

    Keep the `except OSError:` branch as it is.

  - In `mirror_only()`, replace its `try:` body the same way, keeping its `except OSError:` branch as it is.

- [ ] **Step 4: Run the tests and confirm they pass.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/data/test_audit_mirror_lock.py tests/data/test_audit_degradation_logging.py tests/test_audit_mirror_redirect.py tests/test_rotate_audit_logs.py -q`
  - Expected: PASS.

- [ ] **Step 5: Run the pg audit tests (local).**
  - Run: `set -a; . ../Episteme/.env; set +a; .venv/Scripts/python.exe -m pytest -m pg tests/data/test_audit_trail.py -q`
  - Expected: PASS (or SKIP without `TEST_PG_DSN`; say which).

- [ ] **Step 6: Commit.**

```bash
.venv/Scripts/python.exe -m ruff format src/episteme/audit_trail.py tests/data/test_audit_mirror_lock.py
.venv/Scripts/python.exe -m ruff check src/episteme/audit_trail.py tests/data/test_audit_mirror_lock.py
git add src/episteme/audit_trail.py tests/data/test_audit_mirror_lock.py
git commit -F - <<'EOF'
fix(audit): lock mirror appends across processes

With extractors in worker processes, mirror_only() appends (no advisory
lock) could interleave on Windows. Every mirror append now holds a byte
lock on a sidecar file (msvcrt on Windows, flock elsewhere).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
```

---

### Task 4: The eight callers use worker processes

**Files (each gets the same edit):**
- `src/episteme/data/pubmed/extract_pubmed.py`
- `src/episteme/data/pmc/extract_pmc.py`
- `src/episteme/data/bookshelf/extract_bookshelf.py`
- `src/episteme/data/guidelines/extract_guidelines.py`
- `src/episteme/data/apollo/extract_apollo.py`
- `src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py`
- `src/episteme/data/europepmc/manuscripts/extract_europepmc_manuscripts.py`
- `src/episteme/data/openalex/serialize_openalex.py`
- Create: `tests/data/test_extract_workers.py`

**Interfaces:**
- Consumes: `run_per_file` (Task 2), `Settings.extract_workers` (Task 1), `spawn_child_env` (Task 2).
- Produces: nothing new. The importable cores keep `workers: int = 1`, and summaries and manifests are unchanged.

- [ ] **Step 1: Write the failing tests.** Create `tests/data/test_extract_workers.py`:

```python
"""workers=1 (in-process) and workers=3 (spawned processes) write the same rows;
the production `python -m` entry point works with worker processes."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SP2 = REPO / "tests" / "fixtures" / "sp2"
CASES = [
    ("episteme.data.pubmed.extract_pubmed", "extract_pubmed", SP2 / "pubmed", "pubmed"),
    (
        "episteme.data.pmc.extract_pmc",
        "extract_pmc",
        REPO / "tests" / "data" / "fixtures" / "pmc",
        "pmc",
    ),
    (
        "episteme.data.bookshelf.extract_bookshelf",
        "extract_bookshelf",
        SP2 / "bookshelf",
        "bookshelf",
    ),
    (
        "episteme.data.guidelines.extract_guidelines",
        "extract_guidelines",
        SP2 / "guidelines",
        "guidelines",
    ),
    ("episteme.data.apollo.extract_apollo", "extract_apollo", SP2 / "apollo", "apollo"),
    (
        "episteme.data.europepmc.preprints.extract_europepmc_preprints",
        "extract_europepmc_preprints",
        SP2 / "europepmc_preprint",
        "europepmc_preprint",
    ),
    (
        "episteme.data.europepmc.manuscripts.extract_europepmc_manuscripts",
        "extract_europepmc_manuscripts",
        SP2 / "europepmc_manuscript",
        "europepmc_manuscript",
    ),
    (
        "episteme.data.openalex.serialize_openalex",
        "serialize_openalex",
        REPO / "tests" / "fixtures" / "sp4" / "openalex",
        "openalex",
    ),
]
# Wall-clock stamp written into every row; differs between any two runs.
_VOLATILE = {"retrieved_at"}


def _rows(processed: Path, source: str) -> list[dict]:
    import pyarrow.parquet as pq

    staging = processed / "staging" / source
    rows: list[dict] = []
    for p in sorted(staging.glob("*.parquet")):
        rows += pq.read_table(p).to_pylist()
    for p in sorted(staging.glob("*.jsonl")):
        rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    kept = [{k: v for k, v in r.items() if k not in _VOLATILE} for r in rows]
    return sorted(kept, key=lambda r: (r["id"], json.dumps(r, sort_keys=True, default=str)))


@pytest.mark.parametrize("module,func,raw_dir,source", CASES, ids=[c[3] for c in CASES])
def test_workers_3_writes_the_same_rows_as_workers_1(
    tmp_path, spawn_child_env, module, func, raw_dir, source
):
    fn = getattr(importlib.import_module(module), func)
    one = fn(raw_dir, tmp_path / "one", workers=1)
    three = fn(raw_dir, tmp_path / "three", workers=3)
    assert three == one  # {"inputs", "ok", "failed", "rows"}
    rows_one = _rows(tmp_path / "one", source)
    assert rows_one, "fixture produced no rows"
    assert _rows(tmp_path / "three", source) == rows_one


def _cli(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    # cwd=tmp_path: no pyproject.toml above it, so the child loads no .env.
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "episteme.data.pubmed.extract_pubmed",
            "--raw-dir",
            str(SP2 / "pubmed"),
            "--processed-dir",
            str(tmp_path / "cli"),
            *extra,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_cli_module_entry_point_with_worker_processes(tmp_path, spawn_child_env):
    """Production runs `python -m ...`: process_one then lives in __main__ and
    spawn must re-import it in each worker."""
    proc = _cli(tmp_path, "--workers", "2")
    assert proc.returncode == 0, proc.stderr
    assert "workers=2" in proc.stdout
    assert _rows(tmp_path / "cli", "pubmed")


def test_cli_workers_default_comes_from_the_setting(tmp_path, spawn_child_env, monkeypatch):
    monkeypatch.setenv("EPISTEME_EXTRACT_WORKERS", "2")
    proc = _cli(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "workers=2" in proc.stdout
```

- [ ] **Step 2: Run the tests and confirm what fails.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/data/test_extract_workers.py -q`
  - Expected: `test_cli_workers_default_comes_from_the_setting` FAILS (it prints `workers=1`).
  - The equivalence cases may already pass on threads; they become the regression guard.

- [ ] **Step 3: Convert each of the eight modules.** In every file listed above:

  a. Delete `from concurrent.futures import ThreadPoolExecutor, as_completed`. Check with ruff F401/F821 that nothing else in the module used them.

  b. Add, among the other `episteme.data` imports (keep the `# noqa: E402` the module's imports already carry):

```python
from episteme.data.parallel import run_per_file  # noqa: E402
```

  c. In the importable core (`extract_*` / `serialize_openalex`), keep `workers = max(1, int(workers))` and `t0 = time.time()`. Delete `results: list[dict[str, Any]] = []` and the whole `if workers == 1: ... else: with ThreadPoolExecutor ...` block, and put in its place:

```python
    results = run_per_file(
        process_one,
        files,
        workers=workers,
        on_result=_print_verbose if verbose else None,
        raw_dir=raw_dir,
        processed_dir=processed_dir,
        force=force,
    )
```

     These are exactly the keyword arguments each module passes today (`openalex` passes the same three). Check them against each module's `process_one` signature before replacing.

  d. In `main()`, change the `--workers` argument to:

```python
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="worker processes (default: EPISTEME_EXTRACT_WORKERS, 3)",
    )
```

     After `args = p.parse_args(argv)`, add the line below (each `main()` already has `settings = get_settings()`):

```python
    workers = settings.extract_workers if args.workers is None else max(1, args.workers)
```

     Then replace `workers={max(1, args.workers)}` in the `print(...)` with `workers={workers}`, and `workers=args.workers` in the core call with `workers=workers`.

  e. Leave `_AUDIT_LOCK` and its comment as they are. The spec keeps them.

- [ ] **Step 4: Run the tests and confirm they pass.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/data/test_extract_workers.py tests/data/test_extract_pubmed.py tests/data/test_extract_pmc.py tests/data/test_extract_bookshelf.py tests/data/test_extract_guidelines.py tests/data/test_extract_apollo.py tests/data/test_extract_europepmc_preprint.py tests/data/test_extract_europepmc_manuscript.py tests/data/test_serialize_openalex.py tests/data/test_bookshelf_end_to_end.py -q`
  - Expected: PASS.
  - If a column other than `retrieved_at` differs between runs, it is a wall-clock value. Add it to `_VOLATILE` with a one-line comment naming where it is written, and report it as a concern.

- [ ] **Step 5: Run the whole non-pg suite.**
  - Run: `.venv/Scripts/python.exe -m pytest -m "not pg" -q`
  - Expected: PASS.

- [ ] **Step 6: Commit.** Stage the eight module paths and the test explicitly:

```bash
F="src/episteme/data/pubmed/extract_pubmed.py src/episteme/data/pmc/extract_pmc.py src/episteme/data/bookshelf/extract_bookshelf.py src/episteme/data/guidelines/extract_guidelines.py src/episteme/data/apollo/extract_apollo.py src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py src/episteme/data/europepmc/manuscripts/extract_europepmc_manuscripts.py src/episteme/data/openalex/serialize_openalex.py tests/data/test_extract_workers.py"
.venv/Scripts/python.exe -m ruff format $F && .venv/Scripts/python.exe -m ruff check $F
git add $F
git commit -F - <<'EOF'
perf(extract): run per-file extract in worker processes

The seven extractors and the openalex serializer call run_per_file in
place of their ThreadPoolExecutor blocks, so parsing uses several cores.
--workers now defaults to EPISTEME_EXTRACT_WORKERS (3). Shards are
identical with 1 and 3 workers; the python -m entry point is covered.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
```

---

### Task 5: python-isal gzip reads

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Create: `src/episteme/data/gz.py`
- Modify: `src/episteme/data/pubmed/extract_pubmed.py` (`iter_rows_from_file`), `src/episteme/data/graph_builder.py` (`_open_mesh_source`), `src/episteme/data/mesh/serialize_mesh.py` (`_open_source`), `src/episteme/data/uniprot/serialize_uniprot.py` (the FASTA reader's `open_func`), `src/episteme/data/openalex/serialize_openalex.py` (`_open_maybe_gzip`)
- Create: `tests/data/test_gz.py`

**Interfaces:**
- Produces: `episteme.data.gz.open_gz(path, mode="rb", **kwargs)`, with the same arguments as `gzip.open`. The module attribute `_igzip` is `None` when isal is absent.

- [ ] **Step 1: Add the dependency.**
  - In `pyproject.toml`'s `data` extra, add `"isal>=1.8.0",` after `"defusedxml>=0.7",`.
  - Run `uv lock` and then `uv sync --all-extras -q --python "$(cygpath -w ../Episteme/.venv/Scripts/python.exe)"`.
  - Check: `.venv/Scripts/python.exe -c "import isal; print(isal.__version__)"` prints `1.8.0` or later.

- [ ] **Step 2: Write the failing tests.** Create `tests/data/test_gz.py`:

```python
"""open_gz reads the same bytes and text through isal and through gzip."""

from __future__ import annotations

import gzip

import pytest

from episteme.data import gz

PAYLOAD = ("line one\nlíne two — ünïcode\n" * 5000).encode("utf-8")
BACKENDS = ["isal", "gzip"]


def _use(backend: str, monkeypatch) -> None:
    if backend == "isal":
        pytest.importorskip("isal")
        assert gz._igzip is not None
    else:
        monkeypatch.setattr(gz, "_igzip", None)


@pytest.fixture
def gz_file(tmp_path):
    p = tmp_path / "x.gz"
    p.write_bytes(gzip.compress(PAYLOAD))
    return p


@pytest.mark.parametrize("backend", BACKENDS)
def test_binary_read_identical(gz_file, monkeypatch, backend):
    _use(backend, monkeypatch)
    with gz.open_gz(gz_file, "rb") as f:
        assert f.read() == PAYLOAD


@pytest.mark.parametrize("backend", BACKENDS)
def test_text_read_identical(gz_file, monkeypatch, backend):
    _use(backend, monkeypatch)
    with gz.open_gz(gz_file, "rt", encoding="utf-8", errors="replace") as f:
        assert f.read() == PAYLOAD.decode("utf-8")


@pytest.mark.parametrize("backend", BACKENDS)
def test_truncated_file_raises_eof(tmp_path, monkeypatch, backend):
    _use(backend, monkeypatch)
    data = gzip.compress(PAYLOAD)
    p = tmp_path / "t.gz"
    p.write_bytes(data[: len(data) // 2])
    with pytest.raises(EOFError), gz.open_gz(p, "rb") as f:
        f.read()


@pytest.mark.parametrize("backend", BACKENDS)
def test_corrupt_pubmed_file_fails_that_file_only(tmp_path, monkeypatch, backend):
    _use(backend, monkeypatch)
    from episteme.data.pubmed.extract_pubmed import extract_pubmed

    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "pubmed26n9999.xml.gz").write_bytes(b"\x1f\x8b\x08\x00" + b"garbage" * 100)
    res = extract_pubmed(raw, tmp_path / "p", workers=1)
    assert res == {"inputs": 1, "ok": 0, "failed": 1, "rows": 0}
    assert list((tmp_path / "p" / "_ops" / "pubmed" / "failed").glob("*"))
```

- [ ] **Step 3: Run the tests and confirm they fail.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/data/test_gz.py -q`
  - Expected: FAIL (`ImportError: cannot import name 'gz'`).

- [ ] **Step 4: Implement.** Create `src/episteme/data/gz.py`:

```python
"""Read gzip through python-isal (ISA-L, several times faster to decompress)
when it is installed, else through the standard library.

``open_gz`` takes ``gzip.open``'s arguments, binary and text mode alike. A
corrupt member raises ``EOFError`` / ``gzip.BadGzipFile`` from both; isal's
other decode error (``IsalError``) is not ``zlib.error``, so callers catch
``Exception`` (every current caller does).
"""

from __future__ import annotations

import gzip
from typing import Any

try:
    from isal import igzip as _igzip
except ImportError:  # pragma: no cover - isal ships in the data extra
    _igzip = None


def open_gz(path: Any, mode: str = "rb", **kwargs: Any):
    if _igzip is not None:
        return _igzip.open(path, mode, **kwargs)
    return gzip.open(path, mode, **kwargs)
```

- [ ] **Step 5: Switch the five readers.** Import `from episteme.data.gz import open_gz` in each module, with `# noqa: E402` where the module's other `episteme` imports carry it. Then:
  - `extract_pubmed.iter_rows_from_file`: `opener = open_gz if path.name.endswith(".gz") else open`.
  - `graph_builder._open_mesh_source`: `return open_gz(xml_path, "rb") if xml_path.suffix == ".gz" else xml_path.open("rb")`. This is graph_builder's only gzip reader; the pubmed graph does not re-read XML (see the plan's rulings).
  - `serialize_mesh._open_source`: `return open_gz(path, "rb") if path.suffix == ".gz" else path.open("rb")`.
  - `serialize_uniprot` FASTA reader: `open_func = open_gz if path.suffix == ".gz" else open`.
  - `serialize_openalex._open_maybe_gzip`: `return open_gz(path, "rt", encoding="utf-8", errors="replace")`.
  - Remove `import gzip` from any of these modules where nothing else uses it (ruff F401 will say).

- [ ] **Step 6: Run the tests and confirm they pass.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/data/test_gz.py tests/data/test_extract_pubmed.py tests/data/test_graph_builder.py tests/data/test_serialize_mesh.py tests/data/test_serialize_uniprot.py tests/data/test_serialize_openalex.py -q`
  - Expected: PASS. `pg`-marked cases in those files skip under the default marker set; that is fine here.

- [ ] **Step 7: Commit.**

```bash
F="pyproject.toml uv.lock src/episteme/data/gz.py src/episteme/data/pubmed/extract_pubmed.py src/episteme/data/graph_builder.py src/episteme/data/mesh/serialize_mesh.py src/episteme/data/uniprot/serialize_uniprot.py src/episteme/data/openalex/serialize_openalex.py tests/data/test_gz.py"
.venv/Scripts/python.exe -m ruff format src/episteme/data/gz.py src/episteme/data/pubmed/extract_pubmed.py src/episteme/data/graph_builder.py src/episteme/data/mesh/serialize_mesh.py src/episteme/data/uniprot/serialize_uniprot.py src/episteme/data/openalex/serialize_openalex.py tests/data/test_gz.py
.venv/Scripts/python.exe -m ruff check src tests/data/test_gz.py
git add $F
git commit -F - <<'EOF'
perf(data): read gzip through python-isal

open_gz uses isal's igzip when installed (now in the data extra), else
gzip, with identical bytes and text. pubmed extract, the MeSH graph
reader, and the mesh, uniprot and openalex serializers use it. A corrupt
pubmed file still fails only that file, on both backends.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
```

---

### Task 6: Loader — column-wise tuples and per-shard timing

**Files:**
- Modify: `src/episteme/data/postgres_loader.py`
- Modify: `src/episteme/data/load_articles.py` (the `ok ...` print)
- Create: `tests/data/test_postgres_loader_columns.py`

**Interfaces:**
- Produces:
  - `postgres_loader._iter_batches(staging_path, fmt, batch_size)` keeps its name and arguments, but now yields `dict[str, list[Any]]` (column name to one value per row, every `ARTICLE_COLUMNS` key present).
  - `_batch_tuples(cols) -> tuple[list[tuple], list[tuple]]` returns (articles tuples, article_body tuples).
  - `load_source_file` return dict gains `read_sec: float`, `copy_sec: float`, `rows_per_sec: float`.

- [ ] **Step 1: Write the failing tests.** Create `tests/data/test_postgres_loader_columns.py`:

```python
"""Column-wise COPY tuples equal the old dict path's; timing fields present. No DB."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from episteme.data import postgres_loader
from episteme.data.article_schema import empty_article_row, finalize_row
from episteme.data.staging_writer import write_rows


def _rows() -> list[dict]:
    out = []
    for i in range(5):
        out.append(
            finalize_row(
                {
                    **empty_article_row(),
                    "id": f"pmid:{i}",
                    "source": "pubmed",
                    "source_file": "baseline__f.xml.gz",
                    "source_record_id": str(i),
                    "pmid": str(i),
                    "title": f"Title {i}",
                    "abstract": None if i % 2 else f"Abstract {i}",
                    "authors": [f"A {i}", "B"] if i % 2 else None,
                    "mesh": ["Humans", f"M{i}"],
                    "year": None if i == 3 else 2000 + i,
                    "license": "unknown",
                    "subset": "open_metadata",
                }
            )
        )
    return out


def _shard(tmp_path: Path, *, parquet: bool) -> Path:
    info = write_rows(
        _rows(), tmp_path, source="pubmed", source_file="baseline__f.xml.gz", prefer_parquet=parquet
    )
    return Path(info["paths"][0])


def _dicts(shard: Path) -> list[dict]:
    if shard.suffix == ".parquet":
        return pq.read_table(shard).to_pylist()
    return [json.loads(x) for x in shard.read_text(encoding="utf-8").splitlines() if x.strip()]


def _legacy_tuples(rows: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """The pre-change dict path, kept verbatim as the reference."""
    hot = [
        tuple(postgres_loader._hot_value(r, c) for c in postgres_loader.HOT_COLUMNS) for r in rows
    ]
    body = [
        (
            r["id"],
            r.get("source"),
            postgres_loader._coerce_year(r.get("year")),
            r.get("title"),
            r.get("abstract"),
            r.get("body_text"),
            r.get("text"),
        )
        for r in rows
    ]
    return hot, body


class _Copy:
    def __init__(self, sink: list):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write_row(self, row):
        self.sink.append(tuple(row))


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.rowcount = 0

    def copy(self, sql):
        target = sql.split("(")[0].replace("COPY", "").strip()
        return _Copy(self.conn.copied.setdefault(target, []))


class _Conn:
    def __init__(self):
        self.copied: dict[str, list] = {}

    def cursor(self):
        return _Cursor(self)


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr(postgres_loader.audit_trail, "record", lambda *a, **k: "h")


@pytest.mark.parametrize("parquet", [True, False], ids=["parquet", "jsonl"])
def test_column_path_sends_the_same_tuples_as_the_dict_path(tmp_path, parquet):
    shard = _shard(tmp_path, parquet=parquet)
    conn = _Conn()
    result = postgres_loader.load_source_file(
        conn, source="pubmed", staging_path=shard, run_id="r", batch_size=2
    )
    hot, body = _legacy_tuples(_dicts(shard))
    assert conn.copied["episteme.articles"] == hot
    assert conn.copied["episteme.article_body"] == body
    assert result["rows"] == 5


def test_missing_optional_column_reads_as_none(tmp_path):
    shard = _shard(tmp_path, parquet=True)
    table = pq.read_table(shard).drop_columns(["book_meta", "container_id"])
    slim = tmp_path / "slim.parquet"
    pq.write_table(table, slim)
    conn = _Conn()
    postgres_loader.load_source_file(conn, source="pubmed", staging_path=slim, run_id="r")
    hot, body = _legacy_tuples(table.to_pylist())  # dicts lack the keys -> r.get() is None
    assert conn.copied["episteme.articles"] == hot
    assert conn.copied["episteme.article_body"] == body


def test_result_carries_timing_fields(tmp_path):
    shard = _shard(tmp_path, parquet=True)
    result = postgres_loader.load_source_file(
        _Conn(), source="pubmed", staging_path=shard, run_id="r"
    )
    for key in ("read_sec", "copy_sec", "rows_per_sec"):
        assert isinstance(result[key], float), key
        assert result[key] >= 0.0, key
```

- [ ] **Step 2: Run the tests and confirm they fail.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/data/test_postgres_loader_columns.py -q`
  - Expected: `test_result_carries_timing_fields` FAILS (`KeyError: 'read_sec'`). The two equivalence tests pass on the old code; they are the guard for the refactor.

- [ ] **Step 3: Implement in `postgres_loader.py`.**
  - Add imports: `import time`, `from contextlib import closing`.
  - Replace `_iter_batches` with:

```python
def _iter_batches(staging_path: Path, fmt: str, batch_size: int) -> Iterator[dict[str, list[Any]]]:
    """Per batch of at most ``batch_size`` rows: every ``ARTICLE_COLUMNS`` name ->
    its values, one per row (never the whole shard). Parquet columns are
    converted once per column (``to_pylist``), not once per row as a dict. A
    column absent from the shard reads as all ``None``, as ``dict.get`` did.
    ``id`` and ``source_file`` are required (``KeyError`` if absent), as before."""
    columns = article_schema.ARTICLE_COLUMNS
    if fmt == "parquet":
        import pyarrow.parquet as pq

        with pq.ParquetFile(staging_path) as pf:
            names = set(pf.schema_arrow.names)
            for required in ("id", "source_file"):
                if required not in names:
                    raise KeyError(required)
            present = [c for c in columns if c in names]
            for rb in pf.iter_batches(batch_size=batch_size, columns=present):
                n = rb.num_rows
                yield {c: (rb.column(c).to_pylist() if c in names else [None] * n) for c in columns}
        return
    it = _iter_jsonl(staging_path)
    while batch := list(itertools.islice(it, batch_size)):
        cols = {c: [r.get(c) for r in batch] for c in columns}
        cols["id"] = [r["id"] for r in batch]
        cols["source_file"] = [r["source_file"] for r in batch]
        yield cols


def _batch_tuples(cols: dict[str, list[Any]]) -> tuple[list[tuple], list[tuple]]:
    """(episteme.articles tuples in HOT_COLUMNS order, episteme.article_body
    tuples in BODY_COLUMNS order) -- the same values the old per-row dict path
    produced (``_coerce_year``, ``_coerce_retrieved_at`` applied per value)."""
    years = [_coerce_year(v) for v in cols["year"]]
    hot_lists = []
    for c in HOT_COLUMNS:
        if c == "year":
            hot_lists.append(years)
        elif c == "retrieved_at":
            hot_lists.append([_coerce_retrieved_at(v) for v in cols[c]])
        else:
            hot_lists.append(cols[c])
    hot = list(zip(*hot_lists, strict=True))
    body = list(
        zip(
            cols["id"],
            cols["source"],
            years,
            cols["title"],
            cols["abstract"],
            cols["body_text"],
            cols["text"],
            strict=True,
        )
    )
    return hot, body
```

  - In `load_source_file`:
    - Add `t_start = time.perf_counter()` as its first statement after the `batch_size` check.
    - Replace the `(e)+(f)` streaming loop (from `per_file_inserted: Counter[str] = Counter()` through `total += len(batch)`) with:

```python
        per_file_inserted: Counter[str] = Counter()
        total = 0
        read_sec = 0.0
        copy_sec = 0.0
        with closing(_iter_batches(staging_path, fmt, batch_size)) as batches:
            while True:
                t0 = time.perf_counter()
                cols = next(batches, None)
                if cols is None:
                    read_sec += time.perf_counter() - t0
                    break
                hot, body = _batch_tuples(cols)
                t1 = time.perf_counter()
                read_sec += t1 - t0
                with cur.copy(f"COPY episteme.articles ({hot_cols_sql}) FROM STDIN") as cp:
                    for row in hot:
                        cp.write_row(row)
                with cur.copy(f"COPY episteme.article_body ({body_cols_sql}) FROM STDIN") as cp:
                    for row in body:
                        cp.write_row(row)
                copy_sec += time.perf_counter() - t1
                per_file_inserted.update(cols["source_file"])
                total += len(cols["id"])
```

    - Before the audit call, compute `elapsed = time.perf_counter() - t_start`.
    - Add to the returned dict:

```python
        "read_sec": round(read_sec, 3),
        "copy_sec": round(copy_sec, 3),
        "rows_per_sec": round(total / elapsed, 1) if elapsed > 0 else 0.0,
```

    - Update the docstring's "Returns a stats dict" line to list the three new keys, and say that `rows_per_sec` is over the whole call (hash, deletes, COPY).

- [ ] **Step 4: Print the fields.** In `load_articles.py`, change the `ok` print to:

```python
            print(
                f"ok {name} rows={result['rows']} deleted={result['deleted']} "
                f"body_deleted={result['body_deleted']} event={result['event']} "
                f"read_sec={result['read_sec']} copy_sec={result['copy_sec']} "
                f"rows_per_sec={result['rows_per_sec']}"
            )
```

- [ ] **Step 5: Run the tests and confirm they pass.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/data/test_postgres_loader_columns.py tests/data/test_postgres_loader_streaming.py tests/data/test_load_articles.py -q`
  - Expected: PASS. `test_parquet_readers_close_the_file[_iter_batches]` must still pass.

- [ ] **Step 6: Run the pg loader suite (local).**
  - Run: `set -a; . ../Episteme/.env; set +a; .venv/Scripts/python.exe -m pytest -m pg tests/data/test_postgres_loader.py tests/data/test_load_articles.py tests/data/test_retire_source_files.py -q`
  - Expected: PASS (or SKIP without `TEST_PG_DSN`; say which).

- [ ] **Step 7: Commit.**

```bash
F="src/episteme/data/postgres_loader.py src/episteme/data/load_articles.py tests/data/test_postgres_loader_columns.py"
.venv/Scripts/python.exe -m ruff format $F && .venv/Scripts/python.exe -m ruff check $F
git add $F
git commit -F - <<'EOF'
perf(load): column-wise COPY tuples and per-shard timing

The loader converts each Arrow column once and zips the COPY tuples,
in place of a dict per row. The tuples are unchanged (tested against the
old dict path, including shards missing a column). Each shard now
reports read_sec, copy_sec and rows_per_sec on its ok line.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
```

---

### Task 7: Migration 0005, audit events and `bulk_indexes`

**Files:**
- Create: `src/episteme/data/db/migrations/0005_bulk_index_state.sql`
- Modify: `src/episteme/audit_trail.py` (`EVENT_TYPES` and its comment)
- Create: `src/episteme/data/db/bulk_indexes.py`
- Create: `tests/data/test_bulk_indexes_unit.py` (not pg), `tests/data/test_bulk_indexes.py` (pg)
- Modify: `tests/docs/test_gxp_doc.py` (13 becomes 15)
- Modify: `docs/11-gxp-data-integrity.md` (event table), `docs/08-data-storage-principles.md` (table list)

**Interfaces:**
- Consumes: `connection.admin_dsn_from_settings()` (Task 1), `guard.pool_kwargs`, `audit_trail.record`.
- Produces:
  - `bulk_indexes.COVERED_INDEXES: tuple[str, ...]`.
  - `rebuild_sql(definition: str) -> str`.
  - `status(conn) -> list[dict]`.
  - `drop(conn, *, run_id) -> list[str]`.
  - `rebuild(conn, *, run_id) -> list[str]`.
  - `main(argv) -> int`, with exit codes: drop 0/2/1; rebuild 0/1; status 0 none, 1 pending.
  - `BulkIndexError` and `PendingRebuildError`.

- [ ] **Step 1: Write the migration.** Create `src/episteme/data/db/migrations/0005_bulk_index_state.sql`:

```sql
-- Migration 0005 -- bookkeeping for bulk-load mode. Idempotent.
-- scripts/data/db/bulk_load_indexes.sh drop saves here the definition of each
-- secondary index of episteme.articles it drops; rebuild re-creates the index
-- and deletes its row. A row in this table means a rebuild is pending, and
-- `drop` refuses to run until it is done.
-- Apply as episteme_sys_admin (so that role owns the table). Like 0002-0004
-- it is applied by hand and not tracked in episteme._migrations:
--   psql -d <db> -f src/episteme/data/db/migrations/0005_bulk_index_state.sql
-- No grant to episteme_app: only the table owner drops and creates indexes.

CREATE TABLE IF NOT EXISTS episteme._bulk_index_state (
    index_name  text PRIMARY KEY,      -- schema-qualified parent index on episteme.articles
    definition  text NOT NULL,         -- pg_get_indexdef() of the parent index (reads ON ONLY)
    run_id      text,
    dropped_at  timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 2: Add the audit events (test first).**
  - In `tests/docs/test_gxp_doc.py`, change `assert len(EVENT_TYPES) == 13` to `== 15`. Run `.venv/Scripts/python.exe -m pytest tests/docs/test_gxp_doc.py -q` and expect FAIL.
  - In `audit_trail.py`, add `"bulk_index_drop",` and `"bulk_index_rebuild",` at the end of `EVENT_TYPES`. Replace the three-line comment above it with exactly:

```python
# Closed event-type set. The phase-0 data roadmap (Sec 4.8) fixed the first 13
# names; bulk_index_drop / bulk_index_rebuild (bulk-load mode, 2026-10) make 15.
# record() rejects anything else with ValueError *before* any config or DB work.
```
  - In `docs/11-gxp-data-integrity.md`'s event table, after the `load_replace` row, add:

```
| `bulk_index_drop` | bulk-load mode dropped the secondary indexes of `episteme.articles` (object names them) | `bulk_indexes drop` (DB row, in the same transaction as the drop) |
| `bulk_index_rebuild` | bulk-load mode re-created them and ran `ANALYZE` | `bulk_indexes rebuild` (DB row, after the last index) |
```

  - Run `tests/docs/test_gxp_doc.py` again and expect PASS.

- [ ] **Step 3: Write the failing unit tests.** Create `tests/data/test_bulk_indexes_unit.py`:

```python
"""bulk_indexes without a database: definition rewriting and the guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from episteme.data.db import bulk_indexes, connection

PARENT_DEFS = {
    "articles_pmid_idx": "CREATE INDEX articles_pmid_idx ON ONLY episteme.articles USING btree (pmid) WHERE (pmid IS NOT NULL)",
    "articles_mesh_gin": "CREATE INDEX articles_mesh_gin ON ONLY episteme.articles USING gin (mesh)",
}


@pytest.mark.parametrize("name", sorted(PARENT_DEFS))
def test_rebuild_sql_builds_on_every_partition(name):
    out = bulk_indexes.rebuild_sql(PARENT_DEFS[name])
    assert out == PARENT_DEFS[name].replace(" ON ONLY ", " ON ", 1)
    assert "ON ONLY" not in out


@pytest.mark.parametrize(
    "bad",
    [
        "CREATE INDEX x ON episteme.articles USING btree (pmid)",
        "CREATE UNIQUE INDEX x ON ONLY episteme.articles USING btree (pmid)",
        "CREATE INDEX x ON ONLY episteme.other USING btree (pmid)",
        "DROP TABLE episteme.articles; CREATE INDEX x ON ONLY episteme.articles USING gin (mesh)",
        "",
    ],
)
def test_rebuild_sql_refuses_anything_else(bad):
    with pytest.raises(bulk_indexes.BulkIndexError):
        bulk_indexes.rebuild_sql(bad)


def test_covered_indexes_are_exactly_the_six():
    assert bulk_indexes.COVERED_INDEXES == (
        "articles_pmid_idx",
        "articles_pmcid_idx",
        "articles_doi_idx",
        "articles_mesh_gin",
        "articles_authors_gin",
        "articles_publication_types_gin",
    )


def test_main_refuses_under_the_guard_without_connecting(monkeypatch, capsys):
    refused = SimpleNamespace(
        db_mode="restricted",
        pg_database="episteme",
        production_database="episteme",
        pg_host="localhost",
        pg_port=5433,
        sys_admin_password="x",
    )
    monkeypatch.setattr(
        connection, "admin_dsn_from_settings", lambda: connection.admin_dsn(refused)
    )

    def _no_connect(*_a, **_k):
        raise AssertionError("connected despite the guard")

    monkeypatch.setattr(bulk_indexes.psycopg, "connect", _no_connect)
    assert bulk_indexes.main(["drop"]) == 1
    assert "Refusing" in capsys.readouterr().err


def test_main_rejects_an_unknown_action():
    with pytest.raises(SystemExit) as exc:
        bulk_indexes.main(["vacuum"])
    assert exc.value.code == 2
```

- [ ] **Step 4: Write the failing pg tests.** Create `tests/data/test_bulk_indexes.py`:

```python
"""bulk_indexes against episteme_test: drop, rebuild, refusal and resume."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]
MIG = REPO_ROOT / "src" / "episteme" / "data" / "db" / "migrations"
COLUMN_MARKERS = ("(pmid)", "(pmcid)", "(doi)", "(mesh)", "(authors)", "(publication_types)")
SNAPSHOT_SQL = (
    "SELECT c.relname, pg_get_indexdef(c.oid), i.indisvalid "
    "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_class t ON t.oid = i.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE n.nspname = 'episteme' AND (t.relname = 'articles' OR t.relname LIKE 'articles\\_%') "
    "ORDER BY c.relname"
)


@pytest.fixture
def bulk(pg_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test-actor")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    from episteme import audit_trail

    importlib.reload(audit_trail)
    from episteme.data.db import bulk_indexes

    importlib.reload(bulk_indexes)
    with (
        pg_conn.cursor() as cur,
        open(REPO_ROOT / "src/episteme/data/db/extensions.sql") as ext,
        open(REPO_ROOT / "src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
        for m in (
            "0002_container_and_book_parts",
            "0004_articles_load_indexes",
            "0005_bulk_index_state",
        ):
            cur.execute((MIG / f"{m}.sql").read_text(encoding="utf-8"))
    pg_conn.commit()
    return bulk_indexes, audit_trail


def _snapshot(conn):
    with conn.cursor() as cur:
        cur.execute(SNAPSHOT_SQL)
        rows = cur.fetchall()
    conn.commit()
    return rows


def _pending(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT index_name FROM episteme._bulk_index_state ORDER BY index_name")
        rows = [r[0] for r in cur.fetchall()]
    conn.commit()
    return rows


def _events(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT event_type FROM episteme._audit ORDER BY seq")
        rows = [r[0] for r in cur.fetchall()]
    conn.commit()
    return rows


def _set_definition(conn, index_name: str, definition: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE episteme._bulk_index_state SET definition = %s WHERE index_name = %s",
            (definition, index_name),
        )
    conn.commit()


def _definition(conn, index_name: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT definition FROM episteme._bulk_index_state WHERE index_name = %s",
            (index_name,),
        )
        (d,) = cur.fetchone()
    conn.commit()
    return d


def test_drop_then_rebuild_restores_every_index_exactly(pg_conn, bulk):
    bi, audit_trail = bulk
    before = _snapshot(pg_conn)
    assert all(valid for _, _, valid in before)

    assert bi.drop(pg_conn, run_id="r1") == list(bi.COVERED_INDEXES)
    after_drop = _snapshot(pg_conn)
    assert not [d for _, d, _ in after_drop if any(m in d for m in COLUMN_MARKERS)]
    assert len(_pending(pg_conn)) == 6

    assert len(bi.rebuild(pg_conn, run_id="r1")) == 6
    assert _snapshot(pg_conn) == before
    assert _pending(pg_conn) == []

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_inherits WHERE inhparent = 'episteme.articles'::regclass"
        )
        (direct_partitions,) = cur.fetchone()
        cur.execute(
            "SELECT c.relname, count(inh.inhrelid) FROM pg_class c "
            "LEFT JOIN pg_inherits inh ON inh.inhparent = c.oid "
            "WHERE c.relnamespace = 'episteme'::regnamespace AND c.relname = ANY(%s) "
            "GROUP BY c.relname",
            (list(bi.COVERED_INDEXES),),
        )
        attached = dict(cur.fetchall())
    pg_conn.commit()
    assert set(attached) == set(bi.COVERED_INDEXES)
    assert all(n == direct_partitions for n in attached.values())

    assert _events(pg_conn) == ["bulk_index_drop", "bulk_index_rebuild"]
    assert audit_trail.verify(pg_conn) == []


def test_drop_refuses_when_a_rebuild_is_pending(pg_conn, bulk):
    bi, _ = bulk
    bi.drop(pg_conn, run_id="r1")
    after_first = _snapshot(pg_conn)
    with pytest.raises(bi.PendingRebuildError):
        bi.drop(pg_conn, run_id="r2")
    assert _snapshot(pg_conn) == after_first
    assert len(_pending(pg_conn)) == 6
    assert _events(pg_conn) == ["bulk_index_drop"]


def test_drop_refuses_when_a_covered_index_is_missing(pg_conn, bulk):
    bi, _ = bulk
    with pg_conn.cursor() as cur:
        cur.execute("DROP INDEX episteme.articles_doi_idx")
    pg_conn.commit()
    before = _snapshot(pg_conn)
    with pytest.raises(bi.BulkIndexError, match="articles_doi_idx"):
        bi.drop(pg_conn, run_id="r1")
    assert _snapshot(pg_conn) == before
    assert _pending(pg_conn) == []


def test_rebuild_refuses_a_definition_without_on_only(pg_conn, bulk):
    bi, _ = bulk
    bi.drop(pg_conn, run_id="r1")
    name = "episteme.articles_doi_idx"
    _set_definition(pg_conn, name, _definition(pg_conn, name).replace(" ON ONLY ", " ON ", 1))
    with pytest.raises(bi.BulkIndexError):
        bi.rebuild(pg_conn, run_id="r1")
    assert len(_pending(pg_conn)) == 6  # validated up front: nothing rebuilt


def test_interrupted_rebuild_keeps_the_rest_and_resumes(pg_conn, bulk):
    import psycopg

    bi, _ = bulk
    before = _snapshot(pg_conn)
    bi.drop(pg_conn, run_id="r1")
    name = "episteme.articles_doi_idx"  # second in index_name order, after authors
    good = _definition(pg_conn, name)
    _set_definition(
        pg_conn,
        name,
        "CREATE INDEX articles_doi_idx ON ONLY episteme.articles USING btree (no_such_column)",
    )
    with pytest.raises(psycopg.Error):
        bi.rebuild(pg_conn, run_id="r1")
    pending = _pending(pg_conn)
    assert "episteme.articles_authors_gin" not in pending
    assert name in pending
    assert len(pending) == 5

    _set_definition(pg_conn, name, good)
    assert len(bi.rebuild(pg_conn, run_id="r1")) == 5
    assert _snapshot(pg_conn) == before
    assert _pending(pg_conn) == []


def test_rebuild_replaces_an_invalid_index_of_the_same_name(pg_conn, bulk):
    bi, _ = bulk
    before = _snapshot(pg_conn)
    bi.drop(pg_conn, run_id="r1")
    with pg_conn.cursor() as cur:
        # what an operator gets by running the saved text verbatim: an empty,
        # invalid parent index with no partition indexes
        cur.execute(_definition(pg_conn, "episteme.articles_pmid_idx"))
        cur.execute(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = 'episteme.articles_pmid_idx'::regclass"
        )
        assert cur.fetchone()[0] is False
    pg_conn.commit()
    bi.rebuild(pg_conn, run_id="r1")
    assert _snapshot(pg_conn) == before


def test_rebuild_with_nothing_pending_is_a_no_op(pg_conn, bulk):
    bi, _ = bulk
    assert bi.rebuild(pg_conn, run_id="r1") == []
    assert _events(pg_conn) == []
```

- [ ] **Step 5: Run the tests and confirm they fail.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/data/test_bulk_indexes_unit.py -q`
  - Expected: FAIL (`ImportError: bulk_indexes`).

- [ ] **Step 6: Implement.** Create `src/episteme/data/db/bulk_indexes.py`:

```python
"""Bulk-load mode: drop the secondary indexes of ``episteme.articles`` before a
large load and rebuild them after.

    python -m episteme.data.db.bulk_indexes drop|rebuild|status

Operators run ``scripts/data/db/bulk_load_indexes.sh``; ``run_pipeline.sh
<source> load --bulk`` calls it around the load stage. ``main()`` connects as
``episteme_sys_admin``, the table owner (``episteme_app`` cannot drop or create
indexes), through ``admin_dsn_from_settings()``, so the DB-mode guard applies,
and with ``pool_kwargs`` so ``read-only`` mode refuses the writes.

``drop`` saves each covered index's ``pg_get_indexdef()`` in
``episteme._bulk_index_state`` (migration 0005) and drops it, in one
transaction with its audit row. ``rebuild`` re-creates each saved index in its
own transaction and deletes its row there, then runs ``ANALYZE``. A row left in
the table means a rebuild is pending; ``drop`` refuses until it is done.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

import psycopg
from psycopg import sql

from episteme import audit_trail
from episteme.config import ConfigError, get_settings
from episteme.data.db import connection as db_connection
from episteme.data.db.guard import ProductionDatabaseGuardError, pool_kwargs

SCHEMA = "episteme"
# Parent indexes on the partitioned episteme.articles; dropping a parent index
# drops every partition's index. Kept on purpose: the primary keys,
# articles_source_file_idx and articles_id_idx (the loader's deletes use them)
# and articles_retrieved_at_brin (cheap to maintain).
COVERED_INDEXES: tuple[str, ...] = (
    "articles_pmid_idx",
    "articles_pmcid_idx",
    "articles_doi_idx",
    "articles_mesh_gin",
    "articles_authors_gin",
    "articles_publication_types_gin",
)
# pg_get_indexdef() of a partitioned parent index reads "ON ONLY"; run verbatim
# it would create an empty, invalid parent index with no partition indexes.
_PARENT_DEF = re.compile(r"^CREATE INDEX \S+ ON ONLY episteme\.articles USING ")


class BulkIndexError(RuntimeError):
    """A covered index is missing, or a saved definition cannot be rebuilt."""


class PendingRebuildError(BulkIndexError):
    """A previous bulk load's rebuild never finished."""


def rebuild_sql(definition: str) -> str:
    """The saved parent definition with its one ``ON ONLY`` turned into ``ON``,
    which builds the index on every partition and attaches them."""
    if not _PARENT_DEF.match(definition):
        raise BulkIndexError(
            f"saved definition is not a parent index on episteme.articles: {definition!r}"
        )
    return definition.replace(
        " ON ONLY episteme.articles USING ", " ON episteme.articles USING ", 1
    )


def status(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT index_name, definition, run_id, dropped_at "
            "FROM episteme._bulk_index_state ORDER BY index_name"
        )
        rows = cur.fetchall()
    return [
        {"index_name": r[0], "definition": r[1], "run_id": r[2], "dropped_at": r[3]} for r in rows
    ]


def _index_validity(cur: psycopg.Cursor, name: str) -> bool | None:
    """True / False: an index of that name exists and is (in)valid; None: absent."""
    cur.execute(
        "SELECT i.indisvalid FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        (SCHEMA, name),
    )
    row = cur.fetchone()
    return None if row is None else bool(row[0])


def _drop_index(cur: psycopg.Cursor, schema: str, name: str) -> None:
    cur.execute(sql.SQL("DROP INDEX {}.{}").format(sql.Identifier(schema), sql.Identifier(name)))


def drop(conn: psycopg.Connection, *, run_id: str | None) -> list[str]:
    """Save and drop every covered index, in one transaction; commits."""
    try:
        if status(conn):
            raise PendingRebuildError(
                "a previous bulk load's index rebuild never finished; "
                "run `bash scripts/data/db/bulk_load_indexes.sh rebuild` first"
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname, pg_get_indexdef(c.oid) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = 'episteme.articles'::regclass AND c.relname = ANY(%s)",
                (list(COVERED_INDEXES),),
            )
            found = dict(cur.fetchall())
            missing = [n for n in COVERED_INDEXES if n not in found]
            if missing:
                raise BulkIndexError(f"covered index(es) not found on episteme.articles: {missing}")
            for name in COVERED_INDEXES:
                rebuild_sql(found[name])  # never drop what could not be rebuilt
            for name in COVERED_INDEXES:
                cur.execute(
                    "INSERT INTO episteme._bulk_index_state (index_name, definition, run_id) "
                    "VALUES (%s, %s, %s)",
                    (f"{SCHEMA}.{name}", found[name], run_id),
                )
                _drop_index(cur, SCHEMA, name)
        audit_trail.record(
            "bulk_index_drop",
            conn=conn,
            object="episteme.articles " + ",".join(COVERED_INDEXES),
            rows_affected=len(COVERED_INDEXES),
            run_id=run_id,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return list(COVERED_INDEXES)


def rebuild(conn: psycopg.Connection, *, run_id: str | None) -> list[str]:
    """Re-create every saved index, each in its own transaction; idempotent.

    Every saved definition is validated before anything is built. An index of
    the same name that already exists and is valid is kept; an invalid one is
    dropped and rebuilt. Returns the schema-qualified names handled."""
    pending = status(conn)
    conn.commit()
    if not pending:
        return []
    statements = {row["index_name"]: rebuild_sql(row["definition"]) for row in pending}

    done: list[str] = []
    for full_name, statement in statements.items():
        schema, name = full_name.split(".", 1)
        try:
            with conn.cursor() as cur:
                validity = _index_validity(cur, name)
                if validity is False:
                    _drop_index(cur, schema, name)
                if validity is not True:
                    cur.execute(statement)
                cur.execute(
                    "DELETE FROM episteme._bulk_index_state WHERE index_name = %s", (full_name,)
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        done.append(full_name)

    try:
        with conn.cursor() as cur:
            cur.execute("ANALYZE episteme.articles")
        audit_trail.record(
            "bulk_index_rebuild",
            conn=conn,
            object="episteme.articles " + ",".join(n.split(".", 1)[1] for n in done),
            rows_affected=len(done),
            run_id=run_id,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m episteme.data.db.bulk_indexes",
        description="Drop or rebuild the secondary indexes of episteme.articles around a bulk load.",
    )
    p.add_argument("action", choices=("drop", "rebuild", "status"))
    args = p.parse_args(argv)

    settings = get_settings()
    try:
        dsn = db_connection.admin_dsn_from_settings()
    except (ProductionDatabaseGuardError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        with psycopg.connect(dsn, autocommit=False, **pool_kwargs(settings)) as conn:
            if args.action == "status":
                rows = status(conn)
                for r in rows:
                    print(
                        f"pending {r['index_name']} run_id={r['run_id']} dropped_at={r['dropped_at']}"
                    )
                print(f"pending={len(rows)}")
                return 1 if rows else 0
            if args.action == "drop":
                try:
                    names = drop(conn, run_id=settings.run_id)
                except PendingRebuildError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                print(f"dropped {len(names)}: {', '.join(names)}")
                return 0
            names = rebuild(conn, run_id=settings.run_id)
            print(
                f"rebuilt {len(names)}: {', '.join(names)}"
                if names
                else "rebuilt 0 (nothing pending)"
            )
            return 0
    except (BulkIndexError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Name the table in docs/08.** In `docs/08-data-storage-principles.md`'s table that lists `episteme._runs`, `episteme._lineage` and `episteme._audit`, add after the `_lineage` row (match the table's columns):

```
| `episteme._bulk_index_state` | none (migration 0005) | Indexes dropped by bulk-load mode, pending their rebuild; empty except during a `--bulk` load |
```

- [ ] **Step 8: Run the tests and confirm they pass.**
  - Not pg: `.venv/Scripts/python.exe -m pytest tests/data/test_bulk_indexes_unit.py tests/docs -q`. Expected: PASS.
  - pg (local, after the controller's 19beta3 check): `set -a; . ../Episteme/.env; set +a; .venv/Scripts/python.exe -m pytest -m pg tests/data/test_bulk_indexes.py tests/data/test_audit_trail.py tests/data/test_migration_0004.py -q`. Expected: PASS (or SKIP without `TEST_PG_DSN`; report which).

- [ ] **Step 9: Commit.**

```bash
F="src/episteme/data/db/migrations/0005_bulk_index_state.sql src/episteme/audit_trail.py src/episteme/data/db/bulk_indexes.py tests/data/test_bulk_indexes_unit.py tests/data/test_bulk_indexes.py tests/docs/test_gxp_doc.py docs/11-gxp-data-integrity.md docs/08-data-storage-principles.md"
.venv/Scripts/python.exe -m ruff format src/episteme/audit_trail.py src/episteme/data/db/bulk_indexes.py tests/data/test_bulk_indexes_unit.py tests/data/test_bulk_indexes.py tests/docs/test_gxp_doc.py
.venv/Scripts/python.exe -m ruff check src/episteme/audit_trail.py src/episteme/data/db/bulk_indexes.py tests/data/test_bulk_indexes_unit.py tests/data/test_bulk_indexes.py tests/docs/test_gxp_doc.py
git add $F
git commit -F - <<'EOF'
feat(db): bulk-load index drop/rebuild with migration 0005

bulk_indexes drop saves and drops the six secondary indexes of
episteme.articles in one audited transaction; rebuild turns each saved
ON ONLY definition into ON, re-creates it per index (replacing an
invalid leftover), then ANALYZE and an audit row. A pending rebuild
blocks the next drop. New audit events bulk_index_drop and
bulk_index_rebuild.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
```

---

### Task 8: `bulk_load_indexes.sh` and `run_pipeline.sh --bulk`

**Files:**
- Create: `scripts/data/db/bulk_load_indexes.sh`
- Modify: `scripts/data/run_pipeline.sh`
- Create: `tests/test_run_pipeline_bulk.py`
- Modify: `tests/docs/test_runbook_doc.py` (`DISPATCHER_FLAGS`, a `--bulk` rule)

**Interfaces:**
- Consumes: `python -m episteme.data.db.bulk_indexes drop|rebuild|status` (Task 7).
- Produces:
  - `bulk_load_indexes.sh drop|rebuild|status`. Exit codes pass through; usage errors exit 2.
  - `run_pipeline.sh <src> load --bulk`.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_run_pipeline_bulk.py`:

```python
"""run_pipeline.sh --bulk: drop, load, always rebuild -- against fake scripts.

No DB, no network. A copy of scripts/data with fake load_pubmed.sh,
bulk_load_indexes.sh and a fake PYTHON (the audit calls) records the call
order. The copy sits under tmp_path, with no pyproject.toml above it, so
load_dotenv finds no .env."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_bash() -> str:
    """A real POSIX bash -- never Windows' WSL shim in System32/WindowsApps."""
    for cand in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/usr/bin/bash",
        "/bin/bash",
    ):
        if Path(cand).exists():
            return cand
    found = shutil.which("bash")
    if found and "System32" not in found and "WindowsApps" not in found:
        return found
    return ""


BASH = _find_bash()
if not BASH:  # pragma: no cover - environment dependent
    pytest.skip("no usable POSIX bash", allow_module_level=True)

FAKE_LOAD = """#!/usr/bin/env bash
echo "load" >> "$CALLS"
case "${FAKE_LOAD:-ok}" in
    ok) exit 0 ;;
    fail) exit 1 ;;
    term) kill -TERM "$PPID"; sleep 1; exit 0 ;;
esac
"""
FAKE_BULK = """#!/usr/bin/env bash
echo "bulk $1" >> "$CALLS"
case "$1" in
    drop) exit "${FAKE_DROP_RC:-0}" ;;
    rebuild) exit "${FAKE_REBUILD_RC:-0}" ;;
    *) exit 0 ;;
esac
"""
FAKE_PY = """#!/usr/bin/env bash
case "$*" in
    *run_start*) echo "run_start" >> "$CALLS" ;;
    *run_end*) echo "run_end" >> "$CALLS" ;;
esac
exit 0
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.fixture
def tree(tmp_path):
    data = tmp_path / "tree" / "scripts" / "data"
    shutil.copytree(REPO_ROOT / "scripts" / "data", data)
    _write(data / "pubmed" / "load_pubmed.sh", FAKE_LOAD)
    _write(data / "db" / "bulk_load_indexes.sh", FAKE_BULK)
    fake_py = tmp_path / "fakepy"
    _write(fake_py, FAKE_PY)
    return data / "run_pipeline.sh", tmp_path / "calls.log", fake_py


def _run(tree, stage: str, *args: str, **env_over: str):
    script, calls, fake_py = tree
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EPISTEME_", "PG"))}
    env.update(
        EPISTEME_ACTOR="pytest",
        PYTHON=fake_py.as_posix(),
        CALLS=calls.as_posix(),
        EPISTEME_DB_MODE="restricted",
        PGDATABASE="episteme_spawn_blocked",
        EPISTEME_PRODUCTION_DATABASE="episteme_spawn_blocked",
    )
    env.update(env_over)
    proc = subprocess.run(
        [BASH, str(script), "pubmed", stage, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return proc, lines


def test_bulk_success_rebuilds_before_run_end(tree):
    proc, calls = _run(tree, "load", "--bulk")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["run_start", "bulk drop", "load", "bulk rebuild", "run_end"]


def test_bulk_rebuilds_after_a_failed_load_and_keeps_its_exit_code(tree):
    proc, calls = _run(tree, "load", "--bulk", FAKE_LOAD="fail")
    assert proc.returncode == 1, proc.stderr
    assert calls == ["run_start", "bulk drop", "load", "run_end", "bulk rebuild"]


def test_bulk_rebuilds_after_an_interrupted_load(tree):
    proc, calls = _run(tree, "load", "--bulk", FAKE_LOAD="term")
    assert proc.returncode == 143, proc.stderr
    assert calls.count("bulk rebuild") == 1
    assert calls[-1] == "bulk rebuild"
    assert "load" in calls


def test_bulk_rebuild_failure_is_the_exit_code_and_runs_once(tree):
    proc, calls = _run(tree, "load", "--bulk", FAKE_REBUILD_RC="1")
    assert proc.returncode == 1, proc.stderr
    assert calls == ["run_start", "bulk drop", "load", "bulk rebuild", "run_end"]


def test_bulk_drop_failure_never_starts_the_load(tree):
    proc, calls = _run(tree, "load", "--bulk", FAKE_DROP_RC="2")
    assert proc.returncode == 2, proc.stderr
    assert calls == ["run_start", "bulk drop", "run_end"]


def test_load_without_bulk_touches_no_index(tree):
    proc, calls = _run(tree, "load")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["run_start", "load", "run_end"]


def test_bulk_on_another_stage_is_a_usage_error(tree):
    proc, calls = _run(tree, "extract", "--bulk")
    assert proc.returncode == 2
    assert "--bulk" in proc.stderr
    assert calls == []
```

- [ ] **Step 2: Run the tests and confirm they fail.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/test_run_pipeline_bulk.py -q`
  - Expected: FAIL. `--bulk` is passed through as a MODE positional today, so no `bulk` calls appear.

- [ ] **Step 3: Create the wrapper.** Create `scripts/data/db/bulk_load_indexes.sh` (LF line endings):

```bash
#!/usr/bin/env bash
# Bulk-load mode: drop the secondary indexes of episteme.articles before a
# large load, rebuild them after, or show a pending rebuild. Thin shell over
# `python -m episteme.data.db.bulk_indexes`, which connects as
# episteme_sys_admin (the table owner); the DB-mode guard applies.
#   bulk_load_indexes.sh drop      # exit 2 if a previous rebuild is pending
#   bulk_load_indexes.sh rebuild   # idempotent; safe to re-run after a failure
#   bulk_load_indexes.sh status    # exit 1 while a rebuild is pending
# run_pipeline.sh <source> load --bulk runs drop and rebuild around the load.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv

case "${1:-}" in
    drop|rebuild|status) ;;
    *) echo "usage: $0 drop|rebuild|status" >&2; exit 2 ;;
esac
require_env EPISTEME_ACTOR EPISTEME_SYS_ADMIN_PASSWORD

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

exec "$PY" -m episteme.data.db.bulk_indexes "$1"
```

  Then run `git add --chmod=+x scripts/data/db/bulk_load_indexes.sh` when staging (Step 7).

- [ ] **Step 4: Edit `run_pipeline.sh`.**

  a. In `usage()`, change the first echo's flag list to `[--dry-run] [--force] [--reason REASON] [--max-files N] [--bulk]`.

  b. Before the argument loop, next to `FORCE=0`, add `BULK=0`. In the loop's `case`, before `*)`, add:

```bash
        # load stage only: drop the secondary indexes of episteme.articles,
        # load, always rebuild them (scripts/data/db/bulk_load_indexes.sh).
        --bulk) BULK=1; shift ;;
```

  c. After the `--force requires --reason` check, add:

```bash
if [ "$BULK" = "1" ] && [ "$STAGE" != "load" ]; then
    log ERROR "--bulk is only valid with the load stage"
    exit 2
fi
```

  d. Right after the line `bash "$HERE/_lib/check_prereqs.sh" >&2 || true`, add:

```bash
# --bulk: drop the secondary indexes of episteme.articles before the load and
# always rebuild them -- explicitly after a successful load (so the rebuild's
# audit row lands before run_end), and from the EXIT trap after a failed or
# interrupted one. A hard kill skips the trap; the saved state then blocks
# the next --bulk, and `bulk_load_indexes.sh rebuild` finishes the job.
BULK_PENDING=0
_bulk_rebuild() {
    [ "$BULK_PENDING" = "1" ] || return 0
    BULK_PENDING=0
    log INFO "bulk: rebuilding the secondary indexes of episteme.articles"
    bash "$HERE/db/bulk_load_indexes.sh" rebuild
}
_bulk_on_exit() {
    local rc=$?
    trap - EXIT
    _bulk_rebuild
    local rrc=$?
    [ "$rc" -ne 0 ] && exit "$rc"
    exit "$rrc"
}
if [ "$BULK" = "1" ]; then
    log INFO "bulk: dropping the secondary indexes of episteme.articles"
    bash "$HERE/db/bulk_load_indexes.sh" drop
    drop_rc=$?
    if [ "$drop_rc" -ne 0 ]; then
        log ERROR "bulk: index drop failed (rc=$drop_rc); the load was not started (check: bash scripts/data/db/bulk_load_indexes.sh status)"
        "$PY" -m episteme.audit_trail record run_end --object "$SOURCE" --run-id "$RUN_ID" --reason "failed at stage load (bulk index drop)" >/dev/null 2>&1 || true
        exit "$drop_rc"
    fi
    BULK_PENDING=1
    trap _bulk_on_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
fi
```

  e. Immediately before the final `if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then` block that writes `run_end`, add:

```bash
if [ "$BULK" = "1" ]; then
    _bulk_rebuild
    rebuild_rc=$?
    if [ "$rebuild_rc" -ne 0 ]; then
        log ERROR "bulk: index rebuild failed (rc=$rebuild_rc); finish it with: bash scripts/data/db/bulk_load_indexes.sh rebuild"
        "$PY" -m episteme.audit_trail record run_end --object "$SOURCE" --run-id "$RUN_ID" --reason "failed at stage load (bulk index rebuild)" >/dev/null 2>&1 || true
        exit "$rebuild_rc"
    fi
fi
```

- [ ] **Step 5: Update the runbook doc test.** In `tests/docs/test_runbook_doc.py`:
  - Set `DISPATCHER_FLAGS = {"--dry-run", "--force", "--reason", "--max-files", "--bulk"}`.
  - In `test_flags_are_real_and_used_sensibly`, after the `--dry-run` rule, add:

```python
        if "--bulk" in flags:
            assert stage == "load", f"--bulk is load-only: {rest}"
```

- [ ] **Step 6: Run the tests and confirm they pass.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/test_run_pipeline_bulk.py tests/docs/test_runbook_doc.py -q`
  - Expected: PASS.
  - Also run `bash -n scripts/data/run_pipeline.sh scripts/data/db/bulk_load_indexes.sh` (syntax check) and the endpoint-URL guard from CLAUDE.md: `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'`. Expect empty output.

- [ ] **Step 7: Commit.**

```bash
.venv/Scripts/python.exe -m ruff format tests/test_run_pipeline_bulk.py tests/docs/test_runbook_doc.py
.venv/Scripts/python.exe -m ruff check tests/test_run_pipeline_bulk.py tests/docs/test_runbook_doc.py
git add --chmod=+x scripts/data/db/bulk_load_indexes.sh
git add scripts/data/db/bulk_load_indexes.sh scripts/data/run_pipeline.sh tests/test_run_pipeline_bulk.py tests/docs/test_runbook_doc.py
git commit -F - <<'EOF'
feat(pipeline): run_pipeline.sh load --bulk

--bulk drops the secondary indexes of episteme.articles before the load
and always rebuilds them: explicitly before run_end on success, and from
an EXIT/INT/TERM trap after a failed or interrupted load. The exit code
is the load's if it failed, else the rebuild's; a failed drop never
starts the load. --bulk on any other stage exits 2.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
```

---

### Task 9: Runbook and drift log

**Files:**
- Modify: `docs/10-data-sources-runbook.md`
- Modify: `docs/project-incubation-baseline.md`

**Interfaces:** documents Tasks 1–8. Names env keys only, never values.

- [ ] **Step 1: Section 1.2, keys.** After the `EPISTEME_DUCKDB_MEMORY_LIMIT` row, add:

```
| `EPISTEME_EXTRACT_WORKERS` | Worker processes per extract stage and the `openalex` serialize (default 3: the physical cores minus one, about 400 MB each). `--workers N` on a module overrides it. |
```

- [ ] **Step 2: Section 2.3, migrations.**
  - Rename the heading to `### 2.3 Migrations: 0001 blocked, 0002-0005 applied by hand`.
  - Before renaming, `grep -rn "0002-0004" docs tests` and update every reference to the old heading.
  - Add a table row:

```
| `0005_bulk_index_state.sql` | `episteme._bulk_index_state`, the bookkeeping of bulk-load mode (section 4.6) | Required before `load --bulk`. |
```

  - Add `0005_bulk_index_state` to the `for f in ...` list in the apply command.
  - In the check command, add a fifth column `, to_regclass(\$\$episteme._bulk_index_state\$\$) is not null`, and change the expected text to `t|t|t|t|t`, noting that the fifth column is 0005's table.
  - Change "0002, 0003 or 0004" and "those three" to cover 0005.

- [ ] **Step 3: Section 4.3, flags.** Add a row for `--bulk` in the table's existing format. Meaning: `load` only: drop the secondary indexes of `episteme.articles`, load, always rebuild them (section 4.6).

- [ ] **Step 4: New section 4.6, "Bulk-load mode".** Add it after section 4.5:

````markdown
### 4.6 Bulk-load mode (`load --bulk`)

For a large load into `episteme.articles` (the `pubmed` baseline, `openalex`), `--bulk` drops the secondary indexes first and rebuilds them once at the end. Building an index once is much cheaper than updating it for every row:

```bash
bash scripts/data/run_pipeline.sh pubmed load --bulk
```

- **Dropped:** `articles_pmid_idx`, `articles_pmcid_idx`, `articles_doi_idx`, `articles_mesh_gin`, `articles_authors_gin` and `articles_publication_types_gin`, on every partition.
- **Kept:** `articles_source_file_idx` and `articles_id_idx` (the loader's deletes use them) and `articles_retrieved_at_brin`.
- **While the load runs**, queries by PMID, PMCID, DOI or MeSH are slow. Run nothing else against `articles`.
- **Rebuild:** after the load, `rebuild` re-creates each index on every partition, runs `ANALYZE episteme.articles` and writes a `bulk_index_rebuild` audit row. The drop wrote `bulk_index_drop`. Expect the rebuild to take tens of minutes on a full table.
- **When it rebuilds:** always — after a successful, failed or interrupted (Ctrl+C) load. The exit code is the load's if the load failed, else the rebuild's. A failed drop never starts the load.
- **Prerequisites:** migration 0005 (section 2.3) and `EPISTEME_SYS_ADMIN_PASSWORD`. The index work runs as `episteme_sys_admin`, the table owner, and the database-mode guard applies.

**After a hard kill** (Task Manager, `Stop-Process`, a crash), no trap runs and the indexes stay dropped. The saved state survives, and the next `--bulk` refuses to start (exit 2). Check and finish it:

```bash
bash scripts/data/db/bulk_load_indexes.sh status    # exit 1 and a list while a rebuild is pending
bash scripts/data/db/bulk_load_indexes.sh rebuild   # idempotent; safe to re-run
```

**Stopping runs:**

- **Extract (worker processes).** Stop the parent python *and its workers*: `taskkill /T /F /PID <parent pid>`. `Stop-Process` on the parent alone leaves the workers running. Files that finished keep their success markers; a re-run resumes from the rest.
- **A `--bulk` load.** Stop the python load process, not the bash driver, so the driver's trap still rebuilds. If the driver was killed too, run `bulk_load_indexes.sh status`, then `rebuild`.

**Loader timing:** every `ok` line of a load now ends with `read_sec=` (reading the shard and building rows), `copy_sec=` (inside `COPY`) and `rows_per_sec=` (over the whole shard, deletes included). When `read_sec` dominates, the client is the bottleneck; when `copy_sec` does, the database is.
````

- [ ] **Step 5: Drift log.** Append an entry in the existing style under `## Drift log`:

```markdown
- 2026-10-13: Pipeline throughput on fixed hardware (branch `feat/pipeline-throughput`; spec and plan under `docs/superpowers/`).
  - **Landed:**
    - The seven literature extractors and the `openalex` serializer run their per-file work in worker processes through `episteme.data.parallel.run_per_file`, using the spawn context. `EPISTEME_EXTRACT_WORKERS` defaults to 3; an invalid value falls back to 3 with a warning. Shards are identical with 1 and 3 workers.
    - `.gz` reads go through python-isal (`episteme.data.gz.open_gz`; the `isal` dependency is in the `data` extra).
    - The loader builds COPY tuples column-wise and reports `read_sec`, `copy_sec` and `rows_per_sec` per shard.
    - `run_pipeline.sh <source> load --bulk` drops the six secondary indexes of `episteme.articles` and always rebuilds them. The work is audited (`bulk_index_drop` and `bulk_index_rebuild`) and its state is kept in migration 0005's `episteme._bulk_index_state`.
    - Audit mirror appends take a cross-process file lock.
  - **Corrections to the spec:**
    - The `pubmed` graph does not re-read the pubmed XML (it reads the database). `graph_builder`'s only gzip reader is the MeSH one, and that is the one switched to isal.
    - The loader's equivalence is tested at the COPY boundary, not by a side-by-side `pg` load, because the old path no longer exists.
  - **Deferred:**
    - A full Arrow-to-COPY encoder. Decide only from the Stage 2 `read_sec`/`copy_sec` measurements.
    - The other serializers stay as they are (DuckDB already uses every core; mesh, ontologies and cdisc_ct are single-file).
    - `load_articles --workers` stays reserved: loading stays sequential.
```

- [ ] **Step 6: Run the doc tests.**
  - Run: `.venv/Scripts/python.exe -m pytest tests/docs -q`
  - Expected: PASS. `test_flags_are_real_and_used_sensibly` now sees `pubmed load --bulk`; `test_paths_mentioned_exist` checks `scripts/data/db/bulk_load_indexes.sh` exists; `test_env_key_names_only_no_values_or_inline_comments` checks the new key row.

- [ ] **Step 7: Run the full non-pg suite and lint.**
  - Run: `.venv/Scripts/python.exe -m pytest -m "not pg" -q && .venv/Scripts/python.exe -m ruff format --check . && .venv/Scripts/python.exe -m ruff check .`
  - Expected: PASS.

- [ ] **Step 8: Commit.**

```bash
git add docs/10-data-sources-runbook.md docs/project-incubation-baseline.md
git commit -F - <<'EOF'
docs: bulk-load mode, extract workers, migration 0005, drift log

Runbook: EPISTEME_EXTRACT_WORKERS, migration 0005, the --bulk flag and
a bulk-load section (what it drops, hard-kill recovery, stopping
extract workers with taskkill /T, loader timing fields). Drift log entry
for the throughput branch.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
```

---

## After the branch: PR and rollout (controller, local; not subagent tasks)

1. **PR.**
   - Open the PR from `feat/pipeline-throughput`.
   - Wait about 2 minutes for CI and Sourcery.
   - Read `gh pr view N --json reviews,comments` and `gh api repos/devopam/Episteme/pulls/N/comments`.
   - Fix or reply on every thread, and re-check after each push. The user merges.
2. **After the merge, on `episteme_test`:**
   - Confirm `('episteme_test','19beta3')`.
   - Apply 0005 through MCPg `run_ddl` (it runs as `episteme_sys_admin`, so that role owns the table).
   - Run the full `pg` suite.
3. **On `episteme`, with no load running:**
   - Confirm `('episteme','19beta3')`.
   - Apply 0005 through MCPg `run_ddl`.
   - Check through MCPg `list_grants` that `episteme_sys_admin` can INSERT into `episteme._audit` and owns `episteme._bulk_index_state`.
   - Run `bash scripts/data/db/bulk_load_indexes.sh status`; expect exit 0, `pending=0`.
4. **Resume Stage 2:**
   - Run `pubmed extract` with the default 3 workers; it continues from 373 of 1,334 files.
   - Measure files/min against 4.4.
   - Then `pubmed load --bulk`: record rows/s, the `read_sec`/`copy_sec` split and the rebuild time.
   - Then `pubmed graph` and `verify_pubmed.sh all`.
   - Record everything in the drift log.
