# SP6 Hardening Continuation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the deferred bucket left after SP4.1/SP5: dependency cleanup, audit-trail hardening, two pipeline correctness threads (a small `download_pmc.py` bug and the SP2-extractor identity migration), and the cdisc_bc licence posture.

**Architecture:** Four phases (A dependency, B audit, C correctness, D cdisc_bc), each a sequence of small, independently reviewable tasks. Phase C.3 (the SP2-extractor migration) repeats the exact `discover_input_files`/`input_key` pattern SP4.1 Task 3 already shipped for the 8 structured serializers — same helper functions, same test shape, one extractor per task.

**Tech Stack:** Python 3.10+, pytest, bash (Git Bash / PowerShell), PostgreSQL 19beta3 (`episteme_test`), `uv` for lockfile management.

**Spec:** `docs/superpowers/specs/2026-09-22-sp6-hardening-continuation-design.md`.

## Global Constraints

- **Restartability and duplicate detection are a project-wide thumb rule.** Every Phase C.3 task ends with a test that proves a second run over the same (or a renamed-but-colliding) input does not double-count or silently drop rows.
- **Frozen interfaces reused, not redefined:** `discover_input_files(raw_root, patterns) -> list[Path]`, `input_key(path, raw_root) -> str`, `find_input_by_key(raw_root, key) -> Path | None` (all in `src/episteme/data/checkpoint_markers.py`, shipped in SP4.1 Task 3 — read them, do not reimplement).
- **`.env` hygiene:** never read, print, or stage `.env`; only `config.py` reads `os.environ` directly. Docs may name env KEYS, never values.
- **DB targeting:** every hand-run pipeline/`python -m episteme.*` command is prefixed `PGDATABASE=episteme_test`; before any DB-touching step, confirm `select current_setting('server_version')` reports `19beta3`. `pg`-marked tests use `TEST_PG_DSN`.
- **Endpoint URLs** stay in `scripts/data/_lib/sources.env` only; grep gate `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'` must stay empty.
- **`git add` explicit paths only.** Never stage the unstaged `.gitignore` `.gstack/` line. Never `--no-verify`.
- Run `.venv/Scripts/python.exe -m ruff format` and `ruff check` on every changed `.py` file before committing.
- **cdisc_bc constraint (Phase D, but binding everywhere):** no serializer for `cdisc_bc` may be added in this plan; it stays download-only.
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml`, `uv.lock` | drop `pyeuropepmc`, `nltk` |
| `scripts/data/db/ensure_audit_partitions.sh` | new: rolling monthly `_audit` partitions + REVOKE |
| `scripts/data/rotate_audit_logs.sh` | new: gzip old JSONL mirror files, best-effort `chattr +a` |
| `src/episteme/audit_trail.py` | `reason` enforcement for 2 event types (docstring only touched in SP5; now a real behaviour change) |
| `src/episteme/data/{pmc,bookshelf,pubmed,apollo}/extract_*.py`, `src/episteme/data/europepmc/{manuscripts,preprints}/extract_*.py`, `src/episteme/data/guidelines/extract_guidelines.py` | identity migration + silent-degradation logging fix |
| `src/episteme/data/enrich_openmetadata.py` | silent-degradation logging fix only |
| `src/episteme/data/pmc/download_pmc.py` | raw-root default fix |
| `scripts/data/cdisc_bc/download_cdisc_bc.sh`, `docs/09`, `docs/10`, `docs/12` | licence-posture wording |
| `docs/11-gxp-data-integrity.md` | describe the Phase B fixes instead of the gaps |
| `docs/project-incubation-baseline.md` | drift-log entry |

---

### Task 1 (Phase A): Drop unused dependencies

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Test: none new (the existing full suite is the regression check)

**Interfaces:** none (no code consumes either package).

- [ ] **Step 1: Confirm no live import** (re-verify — do not trust this plan's word for it):
  ```bash
  grep -rn "^import pyeuropepmc\|^from pyeuropepmc\| import pyeuropepmc\b" src scripts tests --include=*.py
  grep -rn "\bnltk\b" src scripts tests pyproject.toml
  ```
  Expected: `pyeuropepmc` appears only in `src/episteme/data/_legacy_download.py` (a module whose own docstring says `"""PARKED ... Not imported anywhere ..."""`), guarded by `try/except ImportError`. `nltk` appears only in `pyproject.toml`'s `data` extra. If either shows up anywhere else, STOP this task and report — the spec's premise is wrong for that package and it must not be removed blind.

- [ ] **Step 2: Remove both lines** from the `data` extra in `pyproject.toml` (currently `"nltk>=3.8.0",` and `"pyeuropepmc>=0.1.0",`).

- [ ] **Step 3: Regenerate the lockfile**
  ```bash
  uv lock
  ```
  Read the diff (`git diff uv.lock`). Expect `pyeuropepmc`, `nltk`, and `diskcache` (pyeuropepmc's only consumer) to disappear, along with any package that only pyeuropepmc/nltk pulled in and nothing else needs. If `uv lock` also adds unrelated packages (SP5's ledger noted a prior `uv lock` run added `chardet`, `fastobo`, `pronto` unprompted — that was reverted then), read `pyproject.toml` to see if the `data`/`model`/`dev` extras already declare them; if they are new and unexplained, do NOT commit them — report instead of guessing why they appeared.

- [ ] **Step 4: Reinstall and run the full suite**
  ```bash
  uv sync --extra data --extra model --extra dev
  .venv/Scripts/python.exe -m pytest -q -m "not pg" -p no:cacheprovider
  ```
  Expected: same pass count as the last known-green run (313 passed, 6 skipped, as of SP5) or better; no new failures. If `_legacy_download.py`'s own (non-existent — it is not imported anywhere, confirmed in Step 1) import path were somehow exercised by a test, that test would now fail with `ModuleNotFoundError` inside its `except ImportError` branch, which is exactly what that branch is for — this is expected to be a no-op.

- [ ] **Step 5: Commit**
  ```bash
  git add pyproject.toml uv.lock
  git commit -m "chore(sp6): drop unused pyeuropepmc and nltk (closes 6 of 7 Dependabot alerts)"
  ```

---

### Task 2 (Phase B): `ensure_audit_partitions.sh`

**Files:**
- Create: `scripts/data/db/ensure_audit_partitions.sh` (mode 100755)
- Test: `tests/test_ensure_audit_partitions.sh` — actually, per repo convention, DB-touching shell behaviour is tested with a `pg`-marked Python test that shells out. Create: `tests/test_ensure_audit_partitions.py` (marked `pg`, uses `pg_conn`/`TEST_PG_DSN`)

**Interfaces:**
- Produces: a script callable as `bash scripts/data/db/ensure_audit_partitions.sh [target_db] [months_ahead]` (default `months_ahead=6`), idempotent, exit 0 on success.

- [ ] **Step 1: Read the existing convention** in `scripts/data/db/migrate_database.sh` (PSQL discovery loop, `require_env PGHOST PGPORT EPISTEME_SYS_ADMIN_PASSWORD`, `sa_psql()` helper, `set -uo pipefail`) and `src/episteme/data/db/schema.sql`'s `_audit` section (lines ~253-284: the three existing partitions `_audit_202609`, `_audit_202610`, `_audit_default`, and the `REVOKE UPDATE, DELETE ... FROM episteme_app` block at lines ~335-338). Mirror the same discovery and `sa_psql` pattern exactly.

- [ ] **Step 2: Write the failing test**

  ```python
  # tests/test_ensure_audit_partitions.py
  import subprocess
  from pathlib import Path

  import pytest

  REPO = Path(__file__).resolve().parents[1]
  SCRIPT = REPO / "scripts" / "data" / "db" / "ensure_audit_partitions.sh"


  def _bash() -> str:
      for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
          if Path(c).exists():
              return c
      pytest.skip("no POSIX bash")


  @pytest.mark.pg
  def test_creates_future_partition_and_revokes(pg_conn):
      dsn_parts = pg_conn.info.get_parameters()
      env = {
          "PGHOST": dsn_parts.get("host", "localhost"),
          "PGPORT": dsn_parts.get("port", "5433"),
          "PGDATABASE": "episteme_test",
          "EPISTEME_SYS_ADMIN_PASSWORD": __import__("os").environ["EPISTEME_SYS_ADMIN_PASSWORD"],
          "PATH": __import__("os").environ["PATH"],
      }
      proc = subprocess.run(
          [_bash(), str(SCRIPT), "episteme_test", "8"],
          env=env,
          capture_output=True,
          text=True,
          timeout=60,
      )
      assert proc.returncode == 0, proc.stderr

      with pg_conn.cursor() as cur:
          cur.execute(
              "select relname from pg_class where relname like 'episteme._audit_2%' "
              "or relname ~ '^_audit_2[0-9]{5}$'"
          )
          names = {r[0] for r in cur.fetchall()}
      # a partition ~8 months out from "now" (whatever "now" is when this runs) must exist
      assert any(names), "no monthly _audit partitions found after running the script"


  @pytest.mark.pg
  def test_new_partition_denies_update_delete_to_episteme_app(pg_conn):
      # After Step 1's run, pick any _audit_YYYYMM partition created by it (not the
      # pre-existing 202609/202610/default ones) and confirm episteme_app cannot
      # UPDATE or DELETE it -- the REVOKE must be re-applied per new partition,
      # since ALTER DEFAULT PRIVILEGES only grants.
      with pg_conn.cursor() as cur:
          cur.execute(
              "select has_table_privilege('episteme_app', c.oid, 'UPDATE') as can_update, "
              "       has_table_privilege('episteme_app', c.oid, 'DELETE') as can_delete, "
              "       c.relname "
              "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
              "where n.nspname = 'episteme' and c.relname ~ '^_audit_2[0-9]{5}$' "
              "  and c.relname not in ('_audit_202609', '_audit_202610')"
          )
          rows = cur.fetchall()
      assert rows, "expected at least one newly created partition"
      for can_update, can_delete, name in rows:
          assert not can_update, f"{name} still grants UPDATE to episteme_app"
          assert not can_delete, f"{name} still grants DELETE to episteme_app"
  ```

  Adjust the `pg_conn`-derived connection parameters if `pg_conn.info.get_parameters()` doesn't expose `host`/`port` the way this sketch assumes — read `tests/data/conftest.py`'s `pg_conn` fixture first and use whatever it actually gives you (it may be simpler to read `TEST_PG_DSN` directly via `psycopg.conninfo.conninfo_to_dict` and pull `host`/`port`/`password` from there, or to just pass `TEST_PG_DSN`'s components straight from `os.environ`). Do not guess; read the fixture before finalizing this test.

- [ ] **Step 3: Run to confirm RED** — `.venv/Scripts/python.exe -m pytest tests/test_ensure_audit_partitions.py -q -m pg` → FAIL (script does not exist).

- [ ] **Step 4: Write the script.** Behaviour:
  - Args: `[target_db] [months_ahead]`, defaulting to `${PGDATABASE:-episteme}` and `6`.
  - For each of the next `months_ahead` calendar months starting from the current month (compute with `date -u`), partition name `_audit_YYYYMM`, bounds `FOR VALUES FROM ('YYYY-MM-01') TO ('<next-month>-01')`.
  - For each month: `CREATE TABLE IF NOT EXISTS episteme._audit_YYYYMM PARTITION OF episteme._audit FOR VALUES FROM (...) TO (...);` — idempotent by construction (`IF NOT EXISTS`).
  - **After creating a partition** (check via `SELECT 1 FROM pg_class WHERE relname = '_audit_YYYYMM'` before vs. after, or simply always re-run the REVOKE — it is idempotent and cheap): `REVOKE UPDATE, DELETE ON episteme._audit_YYYYMM FROM episteme_app;`. Always re-running the REVOKE on every partition the script touches (not just newly created ones) is simpler and safe — do that, rather than tracking "was this new" state.
  - Log one INFO line per partition ensured (`log INFO "ensure_audit_partitions: ensured episteme._audit_YYYYMM"`).
  - Exit 0 on success; `die "..."` (common.sh convention) on any `sa_psql` failure.

- [ ] **Step 5: Run to confirm GREEN**, then `bash -n scripts/data/db/ensure_audit_partitions.sh`, `chmod +x` + `git update-index --chmod=+x`, ruff format/check on the test file, commit:
  ```bash
  git add scripts/data/db/ensure_audit_partitions.sh tests/test_ensure_audit_partitions.py
  git commit -m "feat(sp6): ensure_audit_partitions.sh - rolling monthly _audit partitions with REVOKE re-applied"
  ```

---

### Task 3 (Phase B): `reason` enforcement for `manual_correction` and `schema_migration`

**Files:**
- Modify: `src/episteme/audit_trail.py`
- Test: `tests/data/test_audit_trail.py`

**Interfaces:**
- `record()`'s signature is unchanged; only its validation body gains one more check, after the existing `EVENT_TYPES` check and before the cursor is touched (same ordering discipline as the existing `EVENT_TYPES` check, which a comment already says matters for `test_bad_event_type_rejected`).

- [ ] **Step 1: Write the failing tests** (append to `tests/data/test_audit_trail.py`, next to the existing `test_bad_event_type_rejected`):

  ```python
  def test_manual_correction_requires_reason(pg_conn, monkeypatch):
      monkeypatch.setenv("EPISTEME_ACTOR", "x")
      import episteme.config as cfg

      cfg.get_settings.cache_clear()
      from episteme import audit_trail

      with pytest.raises(ValueError, match="reason"):
          audit_trail.record("manual_correction", conn=pg_conn, reason=None)


  def test_schema_migration_requires_reason(pg_conn, monkeypatch):
      monkeypatch.setenv("EPISTEME_ACTOR", "x")
      import episteme.config as cfg

      cfg.get_settings.cache_clear()
      from episteme import audit_trail

      with pytest.raises(ValueError, match="reason"):
          audit_trail.record("schema_migration", conn=pg_conn, reason="")


  def test_force_override_still_requires_no_new_enforcement_at_record_level(pg_conn, monkeypatch):
      # record() itself does not enforce reason for force_override (that stays a
      # CLI-level rule in load_articles.py); this pins that record() does not
      # newly break the existing force_override call site by demanding a reason
      # it doesn't otherwise validate at this layer.
      monkeypatch.setenv("EPISTEME_ACTOR", "x")
      import episteme.config as cfg

      cfg.get_settings.cache_clear()
      from episteme import audit_trail

      audit_trail.record("force_override", conn=pg_conn, reason=None)  # must not raise
      pg_conn.rollback()
  ```

  Match these against the real fixture names and imports already used elsewhere in `tests/data/test_audit_trail.py` (e.g. `test_bad_event_type_rejected`'s exact pattern for `pg_conn`/`monkeypatch`/settings-reload) — read that test first and copy its idiom rather than inventing a new one.

- [ ] **Step 2: Run to confirm RED** (both new "requires reason" tests fail because no such check exists yet).

- [ ] **Step 3: Implement.** In `record()`, immediately after the existing `if event_type not in EVENT_TYPES:` block:
  ```python
  if event_type in ("manual_correction", "schema_migration") and not reason:
      raise ValueError(f"{event_type!r} requires a non-empty reason")
  ```

- [ ] **Step 4: Run to confirm GREEN.** Also run the full `tests/data/test_audit_trail.py -m pg` file to confirm no existing test regresses (there are currently no live call sites for these two event types per the SP5 review, so this is expected to be additive-only).

- [ ] **Step 5: Commit**
  ```bash
  git add src/episteme/audit_trail.py tests/data/test_audit_trail.py
  git commit -m "feat(sp6): enforce reason for manual_correction and schema_migration audit events"
  ```

---

### Task 4 (Phase B): `rotate_audit_logs.sh`

**Files:**
- Create: `scripts/data/rotate_audit_logs.sh` (mode 100755)
- Test: `tests/test_rotate_audit_logs.py` (no `pg` marker needed — this script only touches the filesystem)

**Interfaces:**
- `bash scripts/data/rotate_audit_logs.sh [ops_dir]`, default `ops_dir` = `${EPISTEME_PROCESSED_ROOT:-./02_processed}/_ops/_audit` (match `audit_trail.py`'s `_mirror_dir()` — read it first to get the exact path it uses, and use the same one here, not a guess).

- [ ] **Step 1: Read `audit_trail.py`'s `_mirror_dir()`/`_mirror_path()`** to get the exact mirror directory and the `audit-YYYYMMDD.jsonl` filename pattern verbatim. Read `scripts/data/verify_audit_trail.sh` and confirm (do not assume) that its glob already covers `.jsonl.gz` — the ledger says it does (`mdir.glob("audit-*.jsonl")` inside `episteme.audit_trail.verify()`, actually check whether `verify()`'s glob pattern is `audit-*.jsonl` only or also matches `.gz`; if it is `.jsonl`-only, a gzip'd file would silently stop counting toward `mirror_lines`, which would make rotation *break* verification instead of just archiving it — if this is the case, STOP and report rather than shipping rotation that breaks the mirror-parity check; this is a real risk to check before writing the script, not after).

- [ ] **Step 2: Write the failing test**

  ```python
  # tests/test_rotate_audit_logs.py
  import gzip
  import subprocess
  from pathlib import Path

  import pytest

  REPO = Path(__file__).resolve().parents[1]
  SCRIPT = REPO / "scripts" / "data" / "rotate_audit_logs.sh"


  def _bash() -> str:
      for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
          if Path(c).exists():
              return c
      pytest.skip("no POSIX bash")


  def _run(ops_dir):
      return subprocess.run(
          [_bash(), str(SCRIPT), str(ops_dir)],
          capture_output=True,
          text=True,
          timeout=30,
      )


  def test_gzips_files_older_than_30_days_leaves_recent_alone(tmp_path):
      import os
      import time

      audit_dir = tmp_path / "_audit"
      audit_dir.mkdir()
      old = audit_dir / "audit-20260101.jsonl"
      old.write_text('{"a": 1}\n', encoding="utf-8")
      recent = audit_dir / "audit-20260921.jsonl"
      recent.write_text('{"b": 2}\n', encoding="utf-8")
      old_ts = time.time() - 40 * 86400
      os.utime(old, (old_ts, old_ts))

      proc = _run(audit_dir)
      assert proc.returncode == 0, proc.stderr

      assert not old.exists()
      assert (audit_dir / "audit-20260101.jsonl.gz").is_file()
      with gzip.open(audit_dir / "audit-20260101.jsonl.gz", "rt", encoding="utf-8") as f:
          assert f.read() == '{"a": 1}\n'
      assert recent.is_file()  # untouched
      assert not (audit_dir / "audit-20260921.jsonl.gz").exists()


  def test_idempotent_on_already_rotated_file(tmp_path):
      audit_dir = tmp_path / "_audit"
      audit_dir.mkdir()
      (audit_dir / "audit-20260101.jsonl.gz").write_bytes(b"already-gzipped")

      proc = _run(audit_dir)
      assert proc.returncode == 0, proc.stderr
      assert (audit_dir / "audit-20260101.jsonl.gz").read_bytes() == b"already-gzipped"


  def test_missing_dir_is_a_noop_not_an_error(tmp_path):
      proc = _run(tmp_path / "does_not_exist")
      assert proc.returncode == 0, proc.stderr
  ```

- [ ] **Step 3: Run to confirm RED.**

- [ ] **Step 4: Write the script.** `find "$ops_dir" -maxdepth 1 -name 'audit-*.jsonl' -mtime +30 -print0 | while IFS= read -r -d '' f; do gzip "$f"; done` (or equivalent portable to Git-for-Windows bash — `find`'s `-mtime` is available there; verify with `command -v find`). Missing `ops_dir` → log INFO and exit 0, not `die`. After gzipping today's *previous* day's rotated files, best-effort `chattr +a` the **current** day's still-open mirror file if `command -v chattr` succeeds (Linux only): `chattr +a "$ops_dir/audit-$(date -u +%Y%m%d).jsonl" 2>/dev/null || true` — this must never fail the script (Windows/macOS have no `chattr`).

- [ ] **Step 5: If Step 1 found `verify()`'s glob is `.jsonl`-only:** fix `episteme.audit_trail.verify()`'s mirror-counting glob to also match `audit-*.jsonl.gz` (open each with `gzip.open(..., "rt")` instead of `open(...)`), add a `tests/data/test_audit_trail.py` case with one plain and one gzip'd mirror file both counting toward `mirror_lines`, and mention this fix in the commit. If Step 1 found it already handles `.gz`, skip this step entirely — do not touch `audit_trail.py`'s `verify()` for no reason.

- [ ] **Step 6: Run to confirm GREEN**, `bash -n`, `chmod +x` + `git update-index --chmod=+x`, ruff format/check, commit:
  ```bash
  git add scripts/data/rotate_audit_logs.sh tests/test_rotate_audit_logs.py
  git commit -m "feat(sp6): rotate_audit_logs.sh - gzip mirror files older than 30 days, best-effort chattr +a"
  ```

---

### Task 5 (Phase B): Silent audit-degradation becomes a logged warning

**Files:**
- Modify: every file listed by `grep -rln "_best_effort_audit\|def main" src/episteme/data --include=*.py` that contains the `except Exception: pass  # last-resort fallback` pattern documented in the spec (verified list at plan-writing time: `apollo/extract_apollo.py`, `bookshelf/extract_bookshelf.py`, `chembl/serialize_chembl.py`, `clinvar/serialize_clinvar.py`, `enrich_openmetadata.py`, `europepmc/manuscripts/extract_europepmc_manuscripts.py`, `europepmc/preprints/extract_europepmc_preprints.py`, `guidelines/extract_guidelines.py`, `mesh/serialize_mesh.py`, `ontologies/serialize_ontologies.py`, `openalex/serialize_openalex.py`, `pubchem/serialize_pubchem.py`, `pubmed/extract_pubmed.py`, `reactome/serialize_reactome.py`, `uniprot/serialize_uniprot.py`, plus `pmc/extract_pmc.py`'s inline version) — **re-run the grep yourself before starting; this list is a snapshot, not a guarantee.**
- Test: one representative test in `tests/data/test_serializer_identity.py` or a new small `tests/data/test_audit_degradation_logging.py` (your call — pick whichever fits without duplicating existing parametrized coverage).

**Interfaces:** no function signatures change; only the body of each module's last-resort `except Exception: pass` becomes `except Exception: logging.getLogger(__name__).warning("audit mirror_only fallback also failed for %s", basename, exc_info=True)` (or the closest equivalent given each file's existing `import logging` / `log = logging.getLogger(__name__)` convention — read `src/episteme/logging_setup.py`'s module docstring: "Library modules should use `logging.getLogger(__name__)`"; follow it).

- [ ] **Step 1: Re-run the grep** to get the authoritative file list and exact line numbers for this task's edit; do not trust the list above blindly.

- [ ] **Step 2: For each file, make exactly one change:** the innermost `except Exception:` (the one that wraps the `mirror_only(...)` call itself, NOT the outer one that wraps the whole `_pg_connection()`/`record()` attempt — that outer one already falls through to `mirror_only` intentionally and must keep doing so unlogged-at-that-level, since the mirror path succeeding is the normal degraded case) changes from a bare `pass` to a `log.warning(...)` call carrying the source/basename and the exception. Keep the `# noqa: BLE001` comment if ruff would otherwise flag the broad except (check whether `logging.warning` inside the except still needs the noqa — it does, since the except clause itself is still `Exception`).

- [ ] **Step 3: Write one test** that monkeypatches both `connection()` (to raise) and `audit_trail.mirror_only` (to raise) for one representative module (pick `apollo/extract_apollo.py`, since it's small and already has its own test file), and asserts a WARNING-level log record is emitted (use `caplog.set_level(logging.WARNING)` and assert on `caplog.records`), while confirming the extractor's own success/failure marker outcome is UNCHANGED (the audit failure must still never fail the pipeline).

- [ ] **Step 4: Run the relevant test files for every module touched** (`tests/data/test_extract_apollo.py`, `test_extract_bookshelf.py`, `test_extract_europepmc_manuscript.py`, `test_extract_europepmc_preprint.py`, `test_extract_guidelines.py`, `test_extract_pmc.py`, `test_extract_pubmed.py`, `test_serialize_*.py` for the 8 structured sources, `test_enrich_from_lite.py` if `enrich_openmetadata.py` has a corresponding test) plus the new test — all green, no behaviour change beyond the new log line.

- [ ] **Step 5: Commit**
  ```bash
  git add src/episteme/data/**/*.py tests/data/test_audit_degradation_logging.py  # or wherever Step 3's test landed
  git commit -m "fix(sp6): log (not silently swallow) a failed audit mirror fallback"
  ```

---

### Task 6 (Phase B close-out): Update `docs/11` to describe the fixes

**Files:**
- Modify: `docs/11-gxp-data-integrity.md`
- Test: `tests/docs/test_gxp_doc.py` (extend, do not weaken)

- [ ] **Step 1: Read the current §5 (Integrity) and §8 (Go-live checklist)** and Tasks 2-5's actual committed behaviour (do not describe intent — describe what shipped, re-verify against the code you just wrote in this same plan run).
- [ ] **Step 2: Update the doc**: partition auto-creation now exists (`ensure_audit_partitions.sh`, rolling N months, REVOKE re-applied every run — name the actual default from Task 2); mirror rotation and best-effort `chattr +a` now exist (`rotate_audit_logs.sh`); `reason` is now enforced at the `record()` layer for `manual_correction`/`schema_migration` (still CLI-enforced for `force_override`, as before); a failed audit mirror fallback is now logged, not silent (still non-fatal to the pipeline — say so explicitly, this is not a behaviour change to ingestion, only to observability).
- [ ] **Step 3: Extend `tests/docs/test_gxp_doc.py`** with assertions that the new scripts are named and that the doc no longer claims partition creation "is not yet built" (search for and remove/update that exact stale claim if `test_unimplemented_design_items_are_stated_as_absent` currently pins it — read that test first; if it currently asserts `rotate_audit_logs.sh` does NOT exist, that assertion must now be inverted since it exists after Task 4).
- [ ] **Step 4:** `.venv/Scripts/python.exe -m pytest tests/docs -q -p no:cacheprovider` → green.
- [ ] **Step 5: Commit**
  ```bash
  git add docs/11-gxp-data-integrity.md tests/docs/test_gxp_doc.py
  git commit -m "docs(sp6): docs/11 describes the audit-hardening fixes, not the gaps"
  ```

---

### Task 7 (Phase C.1): `download_pmc.py` respects the configured raw root

**Files:**
- Modify: `src/episteme/data/pmc/download_pmc.py`
- Test: `tests/data/test_download_pmc.py`

**Interfaces:** `main()`'s `--output_dir` argparse default changes from the literal `Path("./01_raw/pmc/oa_comm")` to `get_settings().raw_root / "pmc" / "oa_comm"`, matching `extract_pmc.py`'s existing `--raw-dir` default (`settings.raw_root / "pmc" / "oa_comm"` — read that file's `main()` for the exact import/call pattern and copy it).

- [ ] **Step 1: Write the failing test** (add to `tests/data/test_download_pmc.py` — read the existing file first for its dry-run/argparse test idiom and match it):

  ```python
  def test_output_dir_defaults_to_configured_raw_root(tmp_path, monkeypatch):
      monkeypatch.setenv("EPISTEME_DATA_ROOT", str(tmp_path))
      monkeypatch.setenv("EPISTEME_ACTOR", "x")
      import episteme.config as cfg

      cfg.get_settings.cache_clear()
      from episteme.data.pmc import download_pmc

      importlib.reload(download_pmc)  # re-evaluate the argparse default against the new settings
      parser = (
          download_pmc.build_parser()
      )  # or however main() constructs its ArgumentParser -- read main() first
      args = parser.parse_args([])
      assert args.output_dir == tmp_path / "01_raw" / "pmc" / "oa_comm"
  ```

  `download_pmc.py`'s `main()` currently builds the parser inline inside `main()` rather than in a separate `build_parser()` function (confirm by reading it) — if there is no separately callable parser-construction function, either add one (small, safe refactor: extract the existing `argparse.ArgumentParser()` block into `build_parser() -> argparse.ArgumentParser`, called by both `main()` and the test) or test via `subprocess.run([..., "download_pmc.py", "--dry-run"], env={...})` and inspect stdout/stderr for the resolved path if `--dry-run` already prints it (read the dry-run branch first — SP5's runbook work already confirmed `download_pmc.py`'s dry run "prints before any network call"; check what it prints). Prefer the in-process `build_parser()` refactor if it is a clean small change; it is more direct and faster than a subprocess test.

- [ ] **Step 2: Run to confirm RED.**

- [ ] **Step 3: Implement** the default fix (and the `build_parser()` extraction if you chose that route), importing `episteme.config.get_settings` the same way `extract_pmc.py` does.

- [ ] **Step 4: Run to confirm GREEN**, then run the full `tests/data/test_download_pmc.py` file to confirm no regression to the existing dry-run tests.

- [ ] **Step 5: Commit**
  ```bash
  git add src/episteme/data/pmc/download_pmc.py tests/data/test_download_pmc.py
  git commit -m "fix(sp6): download_pmc.py derives its output dir from settings.raw_root, not a hardcoded path"
  ```

---

### Task 8 (Phase C.2): Find and fix the tests/data env leak

**Files:**
- Modify: whichever test file the bisection identifies (unknown at plan-writing time — this is an investigation task)
- Create (if useful): a small pytest plugin or `conftest.py` addition that fails loudly on the next occurrence, so this class of bug cannot recur silently again.

**Interfaces:** none known yet.

- [ ] **Step 1: Bisect.** Write a throwaway pytest plugin (do NOT commit this file under this name — it is scaffolding for Step 1 only) that snapshots `os.environ` at the start of the session and after every test's teardown, and prints any added/changed key whose value doesn't match the snapshot from before that specific test ran:

  ```python
  # /tmp (or your scratchpad) — not committed
  import os

  _BASE = None


  def pytest_collectstart(collector):
      global _BASE
      if _BASE is None:
          _BASE = dict(os.environ)


  def pytest_runtest_teardown(item, nextitem):
      global _BASE
      cur = dict(os.environ)
      added = {k: v for k, v in cur.items() if k not in _BASE}
      changed = {k: v for k, v in cur.items() if k in _BASE and _BASE[k] != v}
      if added or changed:
          print(f"\n[ENVLEAK] after {item.nodeid}: added={added} changed={changed}")
          _BASE = cur
  ```

  Run: `PYTHONPATH=<scratchpad-dir> .venv/Scripts/python.exe -m pytest -q -m "not pg" -p no:cacheprovider -p <plugin_module_name> tests/ 2>&1 | grep -A1 ENVLEAK`. This directly names the offending test(s) — no manual bisection needed if the plugin approach works. (If it was already run by the controller before this task was dispatched, its output will be handed to you in the dispatch — check the brief for a result before re-running this from scratch.)

- [ ] **Step 2: Read the identified test(s).** The leak mechanism will be one of: (a) a test that calls `episteme.config.get_settings()` or `importlib.reload(episteme.config)` after `monkeypatch.setenv(...)` in a way that writes through to `os.environ` via `load_dotenv` without restoring it (the exact SP4.1 `fresh_config` bug, recurring in a different fixture or a test that doesn't use `fresh_config` at all); (b) a test that calls `os.environ[...] =` or `os.environ.update(...)` directly without a corresponding restore; (c) something in a session/module-scoped fixture whose teardown runs too late or not at all. Identify the exact mechanism before fixing it — do not apply the SP4.1 fix pattern blindly if the mechanism here is different.

- [ ] **Step 3: Fix it** using whichever of these fits the diagnosed mechanism: convert direct `os.environ` mutation to `monkeypatch.setenv`/`monkeypatch.delenv` (which auto-restores); or, if the culprit is a fixture that (like `fresh_config` before its SP4.1 fix) calls `load_dotenv()`/reloads `config` and thereby writes into `os.environ` as a side effect, add the same snapshot/restore pattern `fresh_config` now uses (`tests/test_config.py`'s current `fresh_config` — read it as the reference implementation).

- [ ] **Step 4: Add a regression test** in the same file/module that fails if this specific leak recurs (snapshot `os.environ` before the leaking test's setup, assert it is unchanged after its teardown — same shape as `tests/test_config.py::TestFreshConfigNoLeak`, but scoped to this specific fixture/test rather than duplicating that class).

- [ ] **Step 5: Confirm the fix** by re-running the Step 1 bisection plugin over the full non-pg suite once more — zero `[ENVLEAK]` lines expected. Then run the full non-pg suite normally to confirm no unrelated regression.

- [ ] **Step 6: Commit**
  ```bash
  git add <files touched>
  git commit -m "fix(sp6): stop <test/fixture name> from leaking env vars past its own teardown"
  ```
  Name the actual leaking test/fixture in the commit message — do not use a placeholder.

---

### Task 9 (Phase C.3, extractor 1 of 7): `pmc` identity migration

**Files:**
- Modify: `src/episteme/data/pmc/extract_pmc.py`
- Test: `tests/data/test_extract_pmc.py`

**Interfaces:**
- Consumes: `discover_input_files(raw_root, patterns) -> list[Path]`, `input_key(path, raw_root) -> str` from `src/episteme/data/checkpoint_markers.py` (already imported and used by all 8 SP4 structured serializers — read `src/episteme/data/chembl/serialize_chembl.py` as the reference implementation of this exact migration pattern, since it is the same shape).
- Produces: `process_one`'s `basename` becomes `input_key(meta_path, raw_dir)` instead of `meta_path.name`; `discover_meta_files` is replaced by (or wraps) `discover_input_files(raw_dir, ["PMC*.json"])` — note the current function combines `meta_dir.rglob(...)` and `raw_dir.rglob(...)` into one list; check whether `discover_input_files` searching `raw_dir` alone (recursively) already covers files under `raw_dir/metadata/` — it does, since `discover_input_files` uses `root.rglob(pat)`, so the `meta_dir`-specific branch becomes redundant. Confirm this by reading `discover_meta_files`'s full body (already shown in this plan's research) and `discover_input_files`'s body before removing the redundant branch — do not remove it if there is a real behavioural difference you haven't accounted for (e.g. a file present under both trees with different content, which is a real pmc download-layout possibility per the function's own "unique by name" comment — that comment is doing double duty as both a dedup step and a "prefer one tree over the other" step; check which tree's copy currently wins under the old logic and confirm the new logic's tie-break, via `sorted(out, key=lambda p: str(p))`, doesn't silently flip which copy is treated as authoritative for a name that exists in both).

- [ ] **Step 1: Read `chembl/serialize_chembl.py`'s migration shape** (SP4.1 Task 3) as the reference: import line, how `basename` is assigned, how the discovery call is threaded into `process_one`/`main`, how `--raw-dir` is passed through.

- [ ] **Step 2: Write the failing test** — a checkpoint-collision test proving two files with the same basename in different subdirectories (`raw_dir/metadata/foo/PMC1.json` and `raw_dir/PMC1.json`, or whatever nesting `pmc`'s real download layout produces — check `scripts/data/pmc/download_pmc.sh`'s output layout, or the existing `test_extract_pmc.py` fixtures, for a realistic nested case) each get their own checkpoint marker and are both processed, not collapsed into one:

  ```python
  def test_same_basename_different_subdirs_both_processed(tmp_path):
      raw = tmp_path / "01_raw" / "pmc" / "oa_comm"
      (raw / "metadata" / "batch_a").mkdir(parents=True)
      (raw / "metadata" / "batch_b").mkdir(parents=True)
      meta_a = raw / "metadata" / "batch_a" / "PMC1.json"
      meta_b = raw / "metadata" / "batch_b" / "PMC1.json"
      meta_a.write_text(json.dumps({"pmcid": "PMC1", ...}), encoding="utf-8")  # fill with a minimal valid record per the existing fixtures
      meta_b.write_text(json.dumps({"pmcid": "PMC2", ...}), encoding="utf-8")
      # ... run process_one (or main) over both and assert two distinct
      # success markers exist, keyed by their input_key (not both keyed "PMC1.json")
  ```

  Fill in the exact minimal-valid-JSON shape from `tests/data/test_extract_pmc.py`'s existing fixtures — do not invent a schema. If `test_extract_pmc.py` doesn't already have a helper for building a minimal valid meta+xml pair, look at what `row_from_meta_and_xml` requires and construct the smallest input that doesn't raise.

- [ ] **Step 3: Run to confirm RED** (old code collapses the two into one basename, `PMC1.json`, since XML lookup uses `version_id = meta_path.stem` which is the same for both — actually check this: is the COLLISION here in the checkpoint marker only, or does it also mis-resolve the XML? If `version_id` collides too, the test needs to distinguish which meta file's XML got attached to which row — make the test strict enough to catch that, not just marker-file existence).

- [ ] **Step 4: Implement the migration** per the pattern in Step 1, adjusting `discover_meta_files`/`process_one`/`main`'s `--raw-dir` threading.

- [ ] **Step 5: Run to confirm GREEN**, then the full `tests/data/test_extract_pmc.py` file.

- [ ] **Step 6: Commit**
  ```bash
  git add src/episteme/data/pmc/extract_pmc.py tests/data/test_extract_pmc.py
  git commit -m "fix(sp6): pmc extractor uses input_key identity, not basename-only dedup"
  ```

---

### Task 10 (Phase C.3, extractor 2 of 7): `bookshelf` identity migration

**Files:**
- Modify: `src/episteme/data/bookshelf/extract_bookshelf.py`
- Test: `tests/data/test_extract_bookshelf.py`, `tests/data/test_bookshelf_end_to_end.py` (read both; the end-to-end one is `pg`-marked — check whether it needs updating too, or only the unit-level one)

**Interfaces:** same as Task 9's shape — `bookshelf`'s own docstring at line ~420 already says `` `rglob` (not a flat `glob`) -- the real download layout `` nests files, and the SP4.1 ledger explicitly flags "the deferred SP2-extractor migration, incl. bookshelf's hashed tree" as the reason this source is higher-risk than most. Read that docstring and the surrounding `_discover_tarballs`-equivalent function (confirm its real name — this plan's earlier research found `rglob("*.tar.gz")` at line ~424) in full before touching it; this is the most nested/hazardous of the seven.

- [ ] **Step 1: Read the reference migration** (Task 9's commit, and/or `chembl/serialize_chembl.py`).
- [ ] **Step 2: Write a checkpoint-collision test** using bookshelf's real nested layout (two same-named `.tar.gz` files under different container-id subdirectories — check what "hashed tree" means concretely by reading the download wrapper `scripts/data/bookshelf/download_bookshelf.sh` and/or existing fixtures in `tests/data/test_bookshelf_end_to_end.py` for the real directory shape before inventing one).
- [ ] **Step 3: Run to confirm RED.**
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run to confirm GREEN** — both `test_extract_bookshelf.py` (unit) and, if it needs updating, `test_bookshelf_end_to_end.py -m pg` (against `episteme_test`, `PGDATABASE=episteme_test` prefixed).
- [ ] **Step 6: Commit**
  ```bash
  git commit -m "fix(sp6): bookshelf extractor uses input_key identity for its nested tar layout"
  ```

---

### Task 11 (Phase C.3, extractor 3 of 7): `pubmed` identity migration

**Files:**
- Modify: `src/episteme/data/pubmed/extract_pubmed.py`
- Test: `tests/data/test_extract_pubmed.py`

**Interfaces:** this is the module that literally calls the legacy `list_input_files(raw_dir, ["pubmed*.xml.gz", "pubmed*.xml"])` (line ~273) — replace that call with `discover_input_files(raw_dir, ["pubmed*.xml.gz", "pubmed*.xml"])` and change `basename = path.name` (line ~258) to `basename = input_key(path, raw_dir)`.

- [ ] **Step 1: Read the reference migration.**
- [ ] **Step 2: Write a checkpoint-collision test** (two files named identically under different subdirectories of a synthetic `raw_dir`, even if pubmed's real download layout is normally flat — the test proves the code path is correct regardless of today's actual layout, matching the discipline SP4.1 Task 3 used for the structured serializers).
- [ ] **Step 3: Run to confirm RED.**
- [ ] **Step 4: Implement** (swap `list_input_files` import for `discover_input_files` + `input_key`; both are already exported from `checkpoint_markers.py`).
- [ ] **Step 5: Run to confirm GREEN.**
- [ ] **Step 6: Commit**
  ```bash
  git commit -m "fix(sp6): pubmed extractor migrated off legacy basename-dedup list_input_files"
  ```

---

### Task 12 (Phase C.3, extractor 4 of 7): `apollo` identity migration

**Files:**
- Modify: `src/episteme/data/apollo/extract_apollo.py`
- Test: `tests/data/test_extract_apollo.py`

**Interfaces:** same shape as Task 11 — `list_input_files(raw_dir, ["*.jsonl", "*.json"])` (line ~326) → `discover_input_files`; `basename = path.name` (line ~320) → `input_key(path, raw_dir)`. Note: Task 5 already touched this file's audit-degradation logging — this task's diff must not collide with or revert that change; if it hasn't landed yet in your working tree (task order matters — Task 5 runs before Task 12 in this plan), rebase mentally onto it, i.e. read the file as it exists after Task 5's commit, not from a stale copy.

- [ ] **Step 1: Read the reference migration** and confirm Task 5's logging change is present in the current file.
- [ ] **Step 2: Write a checkpoint-collision test.**
- [ ] **Step 3: Run to confirm RED.**
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run to confirm GREEN.**
- [ ] **Step 6: Commit**
  ```bash
  git commit -m "fix(sp6): apollo extractor migrated off legacy basename-dedup list_input_files"
  ```

---

### Task 13 (Phase C.3, extractor 5 of 7): `europepmc_manuscript` identity migration

**Files:**
- Modify: `src/episteme/data/europepmc/manuscripts/extract_europepmc_manuscripts.py`
- Test: `tests/data/test_extract_europepmc_manuscript.py`

**Interfaces:** this module uses a flat, non-recursive `sorted(p for p in raw_dir.glob("*.tar.gz") if p.is_file())` (line ~281) and `source_file = path.name` (line ~248/~325-ish — re-check exact lines after Task 5's edit). Since the current layout is flat, `input_key(path, raw_dir)` for a directly-under-`raw_dir` file is identical to `path.name` per `input_key`'s own docstring ("A file directly under `raw_root` yields its bare basename") — this task is lower-risk (no real collision possible today) but still brings the module in line with the shared scheme for consistency and defense-in-depth, per the spec.

- [ ] **Step 1: Read the reference migration.**
- [ ] **Step 2: Write a test** that still proves correctness even though today's layout can't collide — e.g. assert `input_key` is used (not `.name` directly) by constructing a nested-subdirectory input (even if the real download wrapper never produces one) and confirming the extractor's discovery finds it too (since `discover_input_files` uses `rglob` in addition to `glob`, a nested file would now be found where the old flat `.glob()` would have missed it entirely — decide whether that behavioural WIDENING (finding files in subdirectories it previously ignored) is desired; the spec says "flat layout, no subdirectories expected" — if broadening discovery to include subdirectories is out of scope/undesired here, keep the discovery call as `raw_dir.glob(...)` unchanged and ONLY change the identity computation from `.name` to `input_key(path, raw_dir)`, do NOT swap in `discover_input_files` for this module. Make this judgment call and document it in the commit message.).
- [ ] **Step 3: Run to confirm RED/GREEN** per whichever approach Step 2 settled on.
- [ ] **Step 4: Commit**
  ```bash
  git commit -m "fix(sp6): europepmc_manuscript extractor uses input_key identity (flat layout preserved)"
  ```

---

### Task 14 (Phase C.3, extractor 6 of 7): `europepmc_preprint` identity migration

**Files:**
- Modify: `src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py`
- Test: `tests/data/test_extract_europepmc_preprint.py`

**Interfaces:** same shape and same judgment call as Task 13 (flat `raw_dir.glob("PPR*.xml")`, whose own module docstring explicitly says `` ``PPR*.xml`` ONLY — no ``*.xml.gz`` range archives, no ``rglob`` `` — this is a deliberate design choice already documented in the module; respect it and make the SAME choice Task 13 made (keep `.glob`, only swap `.name` → `input_key`) for consistency between the two nearly-identical EPMC modules, unless Task 13 concluded otherwise for a reason that doesn't apply here — re-read Task 13's commit message before starting this one.

- [ ] **Step 1: Read Task 13's commit and the reference migration.**
- [ ] **Step 2: Write a test, mirroring Task 13's.**
- [ ] **Step 3: Run to confirm RED/GREEN.**
- [ ] **Step 4: Commit**
  ```bash
  git commit -m "fix(sp6): europepmc_preprint extractor uses input_key identity (flat layout preserved)"
  ```

---

### Task 15 (Phase C.3, extractor 7 of 7): `guidelines` identity migration

**Files:**
- Modify: `src/episteme/data/guidelines/extract_guidelines.py`
- Test: `tests/data/test_extract_guidelines.py`

**Interfaces:** same shape as Tasks 13/14 (flat `raw_dir.glob("*.parquet")` + `raw_dir.glob("*.jsonl")`).

- [ ] **Step 1: Read Tasks 13/14's commits and the reference migration.**
- [ ] **Step 2: Write a test, mirroring Tasks 13/14's.**
- [ ] **Step 3: Run to confirm RED/GREEN.**
- [ ] **Step 4: Commit**
  ```bash
  git commit -m "fix(sp6): guidelines extractor uses input_key identity (flat layout preserved)"
  ```

---

### Task 16 (Phase D): cdisc_bc licence posture — lock it down in wording

**Files:**
- Modify: `scripts/data/cdisc_bc/download_cdisc_bc.sh` (header comment + `PROVENANCE.txt` template only — no functional change), `docs/09-extraction-contract.md`, `docs/10-data-sources-runbook.md`, `docs/12-source-inventory.md`
- Test: extend whichever of `tests/test_cdisc_bc_wrapper.py` / `tests/docs/test_*.py` already asserts the PROVENANCE/licence wording (read them first) with the new sentence.

- [ ] **Step 1: Read the current wrapper header and `PROVENANCE.txt` template** in `download_cdisc_bc.sh`, and the current wording in `docs/09`, `docs/10`, `docs/12` (all touched across SP4.1/SP5 — find every place `cdisc_bc`'s licence is described).
- [ ] **Step 2: Add one explicit sentence everywhere the licence is discussed**, in this spirit (adapt to fit each doc's existing tone, do not just paste verbatim into every file): *"No serializer exists or is planned for cdisc_bc until CDISC confirms a licence for the `export/` data files directly; this source must not be included in any training corpus in its current state."* Also add, in `download_cdisc_bc.sh`'s header comment, a one-line pointer to CDISC's site-wide Terms and Conditions (`https://www.cdisc.org/terms-and-conditions` — this is a reference URL in a comment, not a fetched endpoint, so it does not need to go in `sources.env`; confirm this against the grep-gate's own exclusion pattern, which already excludes `:[0-9]+:\s*#` lines) stating that CDISC's general standards terms restrict derivative works and external redistribution, and that this has NOT been confirmed to apply (or not apply) to this specific repository's data exports.
- [ ] **Step 3: Extend the existing test(s)** to assert the new sentence (or a distinctive substring of it) appears in the wrapper header and/or `PROVENANCE.txt` and in the relevant docs.
- [ ] **Step 4: Run** `tests/test_cdisc_bc_wrapper.py` and `tests/docs -q -p no:cacheprovider` → green. `bash -n` the wrapper. Grep gate stays empty (the CDISC URL is in a `#`-prefixed comment line, already excluded by the gate's own pattern — confirm this, don't assume).
- [ ] **Step 5: Commit**
  ```bash
  git add scripts/data/cdisc_bc/download_cdisc_bc.sh docs/09-extraction-contract.md docs/10-data-sources-runbook.md docs/12-source-inventory.md tests/test_cdisc_bc_wrapper.py
  git commit -m "docs(sp6): cdisc_bc licence posture locked to download-only pending CDISC confirmation"
  ```

---

### Task 17 (close-out): Full sweep and drift-log entry

**Files:**
- Modify: `docs/project-incubation-baseline.md`

- [ ] **Step 1: Full suite.** `set -a; . ./.env; set +a; .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider` (confirm server identity is `19beta3` first). Record the pass/skip count.
- [ ] **Step 2: `not pg` suite** alone, record the count.
- [ ] **Step 3: `bash -n`** on every `.sh` file touched or created in this plan. **Grep gate** empty.
- [ ] **Step 4: `run_pipeline.sh` spot-check** for every migrated extractor's `download --dry-run` (rc 0) and `extract --dry-run` if that stage supports dry-run (check `run_pipeline.sh`'s dispatch — extract may not have a dry-run mode; if not, skip and note why) for `pmc`, `bookshelf`, `pubmed`, `apollo`, `europepmc_manuscript`, `europepmc_preprint`, `guidelines`, all `PGDATABASE=episteme_test`-prefixed.
- [ ] **Step 5: Drift-log entry**, dated, matching the SP4.1/SP5 entries' style: SP6 landed (spec + plan paths); dependency cleanup (6/7 Dependabot alerts closed, `accelerate` still open/no fix); audit hardening (partition auto-creation, reason enforcement, mirror rotation, logged degradation — name the actual scripts); `download_pmc.py` raw-root fix; the identified and fixed tests/data env leak (name the actual test/fixture); the 7-extractor identity migration (name them, and which ones structurally couldn't collide vs. which had a real risk); cdisc_bc licence posture locked to download-only. Deferred/open items still remaining after SP6 (if any surfaced during execution — do not invent a clean list if something new turned up; report it honestly).
- [ ] **Step 6: Commit**
  ```bash
  git add docs/project-incubation-baseline.md
  git commit -m "docs(sp6): drift-log entry - hardening continuation landed"
  ```
