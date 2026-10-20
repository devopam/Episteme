# Ops sample gate (PR 4 of the operating standard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new or changed stage proves itself on a representative sample, checked by data-quality rules, before it runs on the full data. A full run of a gated stage refuses without a valid pass.

**Architecture:**
- **Slice.** `run_pipeline.sh <source> <stage> --sample` builds a fixed slice of the source's raw files under `<processed root>/_sample/<source>/` (a hard link where possible, else a copy).
- **Sample run.** It runs the normal stage wrapper with the raw, processed and corpus roots redirected to that folder, and with the database target set to `episteme_test`. It keeps the production lock folder.
- **Report.** `ops/gate_report.py` measures the output. `ops/gate.py` checks the measures against the source's rules file and writes a pass under the **production** processed root.
- **The pass** holds hashes of the stage's code (its whole `episteme.*` import closure plus its wrapper script) and of the rules file. A code change makes it invalid.
- **Full runs.** A full run of a gated stage checks the pass before `run_start`. `--skip-gate --reason` overrides it, and writes an audited `gate_override` row.

**Tech Stack:** Python 3.13 (requires-python >= 3.10, so no `tomllib`; rules files are JSON), DuckDB (reads the staging shards), psutil (peak RSS of the process tree), psycopg 3 (the sample-load checks on `episteme_test`), Git Bash.

**Spec:** `docs/superpowers/specs/2026-10-19-pipeline-operating-standard-design.md` (sections 4, 7, 8, 9 PR 4).

## Global Constraints

- A sample run writes no production data. Its raw slice, staging, markers, `last_failure.json`, `status.json` and audit mirror go under `<processed root>/_sample/<source>/`, and its database writes go to `episteme_test` (`EPISTEME_DB_TARGET=secondary`) (spec 7).
- A sample run **keeps the production lock folder** (`<data root>/_ops/locks`). It never changes `EPISTEME_DATA_ROOT`, so a sample run and a production heavy job never run at the same time.
- The pass file is `<production processed root>/_ops/<source>/gate/<stage>.pass`. It holds the package version, a hash of the stage's code and a hash of the rules file. A change to either hash makes it invalid (spec 7).
- A full run of a gated (source, stage) without a valid pass refuses **before `run_start`** with exit 2. `--skip-gate --reason "<text>"` overrides it and writes one `gate_override` audit row to production (spec 7).
- A (source, stage) is gated when its rules file exists: `src/episteme/ops/gate_rules/<source>.json` with a section for that stage.
- The report covers:
  - rows in and out, and the `extract_status` mix;
  - the null rate per column;
  - text length p50, p99 and max;
  - id uniqueness;
  - orphan bodies (after a sample load);
  - the licence and subset mix;
  - throughput and peak RSS;
  - the **yield** (text bytes out per input byte, and rows per input file).

  Spec 7 lists all of these except the yield, which this plan adds.
- A dry run is never gated, and `--sample` itself is never gated. `--sample` cannot be combined with `--bulk`, `--skip-gate` or `all`.
- `config.py` is the only module that reads `os.environ`. Endpoint URLs only in `scripts/data/_lib/sources.env`. A captured Python CLI value in a bash script strips a trailing `$'\r'` (the static guard test checks this). No literal CR or TAB bytes in scripts.
- Tests never touch the real data root, lock folder or databases. Every bash or subprocess test sets `EPISTEME_DATA_ROOT`, `EPISTEME_PROCESSED_ROOT`, `EPISTEME_RAW_ROOT`, `EPISTEME_CORPUS_ROOT` and `EPISTEME_LOCK_DIR` to temp paths.
- CI runs `ruff format --check .` (Python blocks in Markdown included), `ruff check .` and `pytest -m "not pg"`.

## Review Focus

1. **The gate passes a defect that drops most of the data.** Expected: the yield rule fails. Before the PR 79 fix, the `bookshelf` extract had 0 duplicate ids, 0 empty texts and plausible null rates, but it kept only 40 rows and 628 KB from 170 MB of input. Test in Task 6: a fixture shaped like that output fails the `bookshelf` rules.
2. **The pass lands in the sample root.** Expected: the pass is written to and read from the production processed root. A sample run redirects `EPISTEME_PROCESSED_ROOT`, and anything that resolves it after the redirect would land in `_sample/`. Test in Task 5.
3. **A sample run escapes the heavy lock.** Expected: a sample run takes the same heavy lock as production. A test runs a sample while a live foreign `heavy.lock` exists in the shared temp lock folder: the sample waits, or with `--no-wait` refuses with 75. Test in Task 5.
4. **A code change outside the stage module.** Expected: a change in a module the stage imports (for example `jats.py` for `bookshelf`) invalidates the pass. Test in Task 1.
5. **A sample load into a test database that is behind.** Expected: when `episteme_test` lacks a migration that exists in the repo, the sample load refuses with a clear message and writes nothing. Test in Task 5 (fake migration check), plus a `pg` test.

---

## File Structure

- Create:
  - `src/episteme/ops/gate.py`: rules, evaluation, the code hash, the pass, the CLI;
  - `src/episteme/ops/gate_report.py`: the measures;
  - `src/episteme/ops/gate_slice.py`: slice specs and `prepare`;
  - `src/episteme/ops/gate_rules/{bookshelf,europepmc_manuscript,apollo}.json`.
- Modify:
  - `scripts/data/run_pipeline.sh`: `--sample`, gate enforcement and `--skip-gate`;
  - `src/episteme/audit_trail.py`: add `gate_override` to `EVENT_TYPES`;
  - `src/episteme/config.py`: no new env keys are planned; if one is needed, add it here only;
  - `tests/docs/test_runbook_doc.py`: `DISPATCHER_FLAGS`;
  - `tests/docs/test_gxp_doc.py`: the event count.
- Tests: `tests/ops/test_gate.py`, `tests/ops/test_gate_report.py`, `tests/ops/test_gate_slice.py`, `tests/test_run_pipeline_gate.py` (fake stages), and `pg` tests in `tests/ops/test_gate_report.py`.
- Docs: `docs/10-data-sources-runbook.md` (new section "The sample gate"; exit-code table; `--sample`, `--skip-gate`), `docs/11-gxp-data-integrity.md` (`gate_override`), `docs/project-incubation-baseline.md` (drift log).

---

### Task 1: `ops/gate.py`: rules, evaluation, the code hash, the pass

**Files:** Create `src/episteme/ops/gate.py`, `src/episteme/ops/gate_rules/__init__.py` (empty, so the folder ships as package data); Test `tests/ops/test_gate.py`.

**Interfaces — Produces:**
- `gate.RULES_DIR: Path` (the folder `src/episteme/ops/gate_rules`).
- `gate.load_rules(source) -> dict | None`: the parsed JSON, or None when no rules file exists.
- `gate.is_gated(source, stage) -> bool`: True when the rules file has a section for that stage.
- `gate.evaluate(measures: dict, stage_rules: dict) -> list[str]`: the failed rules as readable lines. An empty list means a pass.
- `gate.code_hash(source, stage) -> str`: a SHA-256 over the stage's import closure plus its wrapper script.
- `gate.rules_hash(source) -> str`.
- `gate.pass_path(source, stage, processed_root) -> Path`.
- `gate.write_pass(source, stage, processed_root, report_path) -> Path`.
- `gate.pass_valid(source, stage, processed_root) -> tuple[bool, str]`: the reason is "no pass", "code changed", "rules changed" or "ok".
- `STAGE_MODULES: dict[tuple[str, str], tuple[str, str]]` maps (source, stage) to (the Python module run by the stage, the wrapper script under `scripts/data`). Entries:
  - `("bookshelf","extract")` → (`episteme.data.bookshelf.extract_bookshelf`, `bookshelf/extract_bookshelf.sh`);
  - `("europepmc_manuscript","extract")` → (`episteme.data.europepmc.manuscripts.extract_europepmc_manuscripts`, `europepmc_manuscript/extract_europepmc_manuscript.sh`; check the real wrapper path with `ls scripts/data`);
  - `("apollo","extract")` → (`episteme.data.apollo.extract_apollo`, `apollo/extract_apollo.sh`);
  - `(<any source>,"load")` → (`episteme.data.load_articles`, `<source>/load_<source>.sh`).

**The rules file format** (JSON; every key is optional):

```json
{
  "extract": {
    "min_rows": 100,
    "min_rows_per_input": 3.0,
    "min_text_bytes_per_input_byte": 0.01,
    "max_duplicate_ids": 0,
    "max_empty_text_pct": 0.0,
    "max_status_pct": {"dropped": 2.0, "partial": 10.0},
    "max_null_pct": {"title": 1.0},
    "allowed_licenses": ["CC BY", "CC BY-NC", "CC BY-NC-ND", "unknown"],
    "max_failed_inputs": 0
  },
  "load": {
    "max_orphan_bodies": 0,
    "rows_loaded_equals_staging": true
  }
}
```

**The code hash:**
- Run `python -c` in a **fresh interpreter** (`sys.executable -I`, with the repo's `src` on `sys.path`). It imports the stage module and prints the files of every module in `sys.modules` whose `__file__` lies under `src/episteme`.
- Sort those paths, hash each file's bytes with its path relative to `src`, add the wrapper script's bytes, and hash the result.
- A fresh interpreter is required: the test process has extra modules loaded.

**The pass file content** (JSON): `source`, `stage`, `package_version` (from `importlib.metadata.version("episteme")`, with a fallback to `"unknown"`), `code_hash`, `rules_hash`, `report` (path), `passed_at` (UTC), `git_head` (`git rev-parse HEAD` when available, else null; for the record only, not compared).

- [ ] **Step 1: failing tests:**
  - `evaluate` for each rule: met → no line; broken → one line naming the rule, the threshold and the value.
  - An unknown key in a rules section raises `ValueError`, so a typo never silently disables a rule.
  - `code_hash` is stable across two calls and changes when **a module the stage imports** changes. Build a temp package tree: `pkg/stage.py` imports `pkg/helper.py`. Change `helper.py`, and the hash changes (Review Focus 4). The function takes the module name and the source root as parameters, for testing.
  - `write_pass` then `pass_valid` → ok. A changed code hash → "code changed". A changed rules file → "rules changed". No file → "no pass".
  - `is_gated` follows the rules sections.
- [ ] **Step 2:** implement. **Step 3:** pass, ruff, commit `feat(ops): gate rules, evaluation, code hash and pass file`.

---

### Task 2: `ops/gate_report.py`: the measures

**Files:** Create `src/episteme/ops/gate_report.py`; Test `tests/ops/test_gate_report.py`.

**Interfaces:**
- Consumes: nothing from Task 1; the report is plain data.
- Produces:
  - `gate_report.measure_staging(staging_dir: Path, *, inputs: list[Path]) -> dict`;
  - `gate_report.measure_load(conn, source: str, source_files: list[str]) -> dict` (pg);
  - `gate_report.write_report(measures: dict, failures: list[str], out_dir: Path) -> Path` writes `gate_report.json` and `gate_report.md`.

Measures (DuckDB over the staging `*.parquet` and `*.jsonl` shards, as `sample_audit.py` does with Polars; read it for the shape):
- `rows`, `inputs` (the count of input files), `input_bytes` (their total size), `rows_per_input`;
- `text_bytes` (the sum of `octet_length(text)`), `text_bytes_per_input_byte`;
- `extract_status` counts and percentages;
- the null-or-empty percentage per column;
- text length p50, p99 and max for `title`, `abstract`, `body_text` and `text`;
- `duplicate_ids`, `duplicate_content_hash`;
- the licence and subset counts;
- `empty_text_pct` (rows with no `text`, `body_text` or `abstract`).

From the run record (Task 4): `elapsed_s`, `rows_per_s`, `peak_rss_mb`, `failed_inputs` (from the stage's failure markers or exit code).

`measure_load`, on `episteme_test`:
- `rows_loaded`: `episteme.articles` rows for these `source_file`s;
- `orphan_bodies`: `episteme.article_body` rows of this source with no matching `episteme.articles` row;
- `rows_loaded_equals_staging`.

Read `postgres_loader.py` for the real table and column names before writing the SQL.

- [ ] **Step 1: failing tests** (not pg), with small parquet fixtures written by the test:
  - the counts, percentages and percentiles;
  - the yield values;
  - duplicates;
  - an empty staging folder → `rows = 0` (not an error).

  A `pg` test for `measure_load` loads 2 rows plus 1 orphan body into `episteme_test` inside a transaction that is rolled back, and gets `orphan_bodies = 1`.
- [ ] **Step 2:** implement. **Step 3:** pass, ruff, commit `feat(ops): gate report measures`.

---

### Task 3: `ops/gate_slice.py`: the slices and `prepare`

**Files:** Create `src/episteme/ops/gate_slice.py`; Test `tests/ops/test_gate_slice.py`.

**Interfaces — Produces:**
- `gate_slice.SLICES: dict[str, SliceSpec]`;
- `gate_slice.select(source, raw_source_dir: Path) -> list[Path]` (deterministic);
- `gate_slice.prepare(source, raw_source_dir: Path, sample_root: Path) -> dict`. It wipes and rebuilds `sample_root/01_raw/<source rel>`, links or copies the selected files keeping their paths relative to `raw_source_dir`, and returns `{"files": [...], "bytes": n, "linked": k, "copied": m}`. It also writes `sample_root/slice.json`.

The slice specs, in code:
- **`bookshelf`:** 20 packages spread evenly over the sorted list of `*.tar.gz`, plus the package at the 99th size percentile. Packages over 1 GB are left out. This is the rule used in the 2026-10-20 manual sample.
- **`europepmc_manuscript`:** the first baseline package, the newest baseline package, and the newest incremental package (`*.tar.gz`, each with its `.filelist.csv` and `.filelist.txt`).
- **`apollo`:** the first file under `train/pretrain` and the first under `train/sft`. Find the real layout by listing a real apollo raw folder's structure in the report; do not guess. If a part is missing, that part of the slice is empty and `select` raises with a clear message.

Rules:
- `prepare` refuses unless `sample_root` resolves inside `<processed root>/_sample/`. It asserts this before it deletes anything.
- It tries `os.link` first (the same volume), and falls back to `shutil.copy2`.
- It refuses when the slice exceeds `max_bytes` (a cap per spec: bookshelf 2 GB, europepmc_manuscript 1 GB, apollo 4 GB).
- A selected file that is missing gives a clear error.

- [ ] **Step 1: failing tests** on temp trees:
  - selection is deterministic and spread;
  - packages over 1 GB are left out (fake sizes through a patched stat, or small files with a lowered cap);
  - the 99th-percentile pick is included;
  - the relative paths are kept;
  - a hard link is used when possible (`os.stat(...).st_nlink > 1`);
  - the copy fallback works when `os.link` is patched to raise;
  - a `sample_root` outside `_sample` raises, and nothing is deleted;
  - the cap raises;
  - `slice.json` lists the files.
- [ ] **Step 2:** implement. **Step 3:** pass, ruff, commit `feat(ops): gate slices and prepare`.

---

### Task 4: the gate CLI: `prepare`, `run`, `report`, `check`

**Files:** Modify `src/episteme/ops/gate.py` (CLI); Test `tests/ops/test_gate_cli.py`.

**Interfaces — Produces:** `python -m episteme.ops.gate`:
- `prepare --source S --raw-dir D --sample-root R` prints the sample root and writes `slice.json`.
- `run --record F -- <command...>` runs the command and writes a run record to F: `elapsed_s`, `rc`, and `peak_rss_mb`. Peak RSS is the largest summed RSS of the whole process tree (the venv launcher, the interpreter and pool workers), polled every 0.5 s with psutil. It exits with the command's exit code.
- `report --source S --stage ST --sample-root R --pass-root P --record F`:
  - measures the sample staging (and the load checks for `load`, on `episteme_test`);
  - evaluates the rules and writes the report under `R`;
  - on success, writes the pass under **`P`**, the production processed root, passed explicitly;
  - exits 0 on a pass and 1 on a fail, printing the failed rules.
- `check --source S --stage ST --pass-root P` exits 0 when the pass is valid or the stage is not gated, and 1 otherwise, printing the reason.
- `paths` prints the current processed root (one line, no other output). The runner calls it **before** any redirect to capture the pass root.
- `migrations-check` connects to the configured database (the runner calls it with `EPISTEME_DB_TARGET=secondary`) and lists the migration file names under `src/episteme/data/db/migrations/` that are missing from `episteme._migrations`. It exits 0 when none are missing and 1 otherwise, printing the names.

- [ ] **Step 1: failing tests:**
  - `run` returns the child's exit code, and its record has `peak_rss_mb > 0` (use a child that allocates about 50 MB);
  - `report` with fixtures writes the pass under the given pass root, not the sample root;
  - `report` with a failing rule writes no pass and exits 1;
  - `check` covers all four reasons.
  - `paths` prints the processed root; `migrations-check` with a fake connection reports the missing names and exits 1, and exits 0 when none are missing.
- [ ] **Step 2:** implement. **Step 3:** pass, ruff, commit `feat(ops): gate CLI`.

---

### Task 5: `run_pipeline.sh`: `--sample`, enforcement, `--skip-gate`

**Files:** Modify `scripts/data/run_pipeline.sh`, `src/episteme/audit_trail.py` (`gate_override` in `EVENT_TYPES`), `docs/11-gxp-data-integrity.md` (event row and count), `tests/docs/test_gxp_doc.py` (count), `tests/docs/test_runbook_doc.py` (`DISPATCHER_FLAGS`: `--sample`, `--skip-gate`); Test `tests/test_run_pipeline_gate.py`.

Behaviour:

- **Options:**
  - `--sample`: one named stage only; not with `all`, `--bulk`, `--skip-gate`, `--force` or `--dry-run`. A misuse gives exit 2.
  - `--skip-gate`: needs `--reason`; not with `--sample`.
- **`--sample` flow.** It takes the stage's lock as usual. The lock folder is never redirected; assert `EPISTEME_LOCK_DIR` or the data root is unchanged.
  1. `PASS_ROOT` is the production processed root, captured **before** any redirect with `gate paths` (Task 4).
  2. `SAMPLE_ROOT` is `$PASS_ROOT/_sample/<source>`.
  3. For `extract` or `serialize`: `gate prepare` builds the slice from the production raw folder.
  4. Export the redirects: `EPISTEME_RAW_ROOT=$SAMPLE_ROOT/01_raw`, `EPISTEME_PROCESSED_ROOT=$SAMPLE_ROOT/02_processed`, `EPISTEME_CORPUS_ROOT=$SAMPLE_ROOT/03_corpus` and `EPISTEME_DB_TARGET=secondary`. Keep `EPISTEME_DATA_ROOT` and the lock folder unchanged.
  5. For `load`: the input is the sample's own staging from a previous sample extract. With none, refuse with exit 2 and the message "run the sample extract first".
  6. **Before a sample load:** check that every migration file under `src/episteme/data/db/migrations/` is listed in `episteme_test`'s `episteme._migrations`. Missing names → refuse with exit 2 and name them. Use `gate migrations-check` (Task 4).
  7. Run the stage wrapper through `gate run --record` with the usual `run_stage` handling (lock, status, `run_end`). The audit rows go to `episteme_test`.
  8. Run `gate report --pass-root "$PASS_ROOT"`. Exit 0 on a pass; exit 1 and the failed rules otherwise.
  9. The run's `run_start` and `run_end` also go to `episteme_test`, because the target is redirected for the whole run. Say so in the docs.
- **Enforcement on a full run** (not dry run, not `--sample`):
  - Before `run_start`, for each stage the run will execute (all of them for `all`) that `gate check` reports as gated: with no valid pass, refuse with exit 2 and the reason (`no pass` / `code changed` / `rules changed`), with no audit row.
  - With `--skip-gate --reason R`: write one `gate_override` audit row (object `<source> <stage>`, reason `R`) to **production** for each gated stage that lacks a valid pass, then go on.
- **Bash rules:**
  - Every new Python capture strips a trailing `$'\r'`; the static guard test checks this.
  - Write no literal CR bytes.
  - Merge any new trap into the existing `_on_exit`.

- [ ] **Step 1: failing tests**, with fake stages and fake `gate` responses, or the real gate CLI on fixtures:
  - a full `bookshelf extract` with no pass → exit 2, no `run_start`;
  - with a valid pass → the run proceeds;
  - `--skip-gate --reason x` → one `gate_override` row, then the run proceeds;
  - `--skip-gate` without `--reason` → exit 2;
  - `--sample` redirects the raw, processed and corpus roots and `EPISTEME_DB_TARGET`, but **not** the data root or the lock folder (the fake stage records its env);
  - `--sample` waits on a live foreign `heavy.lock` in the shared lock folder, or with `--no-wait` exits 75 (Review Focus 3);
  - the pass is written under the production processed root (Review Focus 2);
  - a sample load with no sample staging → exit 2;
  - a sample load with a missing migration (fake check) → exit 2, naming it (Review Focus 5);
  - the `--sample` misuse combinations → exit 2;
  - `all` with one gated stage lacking a pass → exit 2 before `run_start`;
  - an ungated source is unaffected.

  A `pg` test runs `gate migrations-check` against `episteme_test`.
- [ ] **Step 2:** implement. **Step 3:** run the new tests and the existing run_pipeline tests (lock_status, skip_unit, bulk; dispatch with `timeout 1800`), `bash -n`, shellcheck, the URL guard and the docs tests. **Step 4:** commit `feat(pipeline): --sample runs and the gate check on full runs; --skip-gate is audited`.

---

### Task 6: The rules for `bookshelf`, `europepmc_manuscript` and `apollo`

**Files:** Create `src/episteme/ops/gate_rules/{bookshelf,europepmc_manuscript,apollo}.json`; Test `tests/ops/test_gate_rules_sources.py`.

The starting thresholds come from the 2026-10-20 samples (the post-fix `bookshelf` run gave 224 rows from 22 packages, 1.8 MB from 170 MB). The controller confirms them on a real sample after this task. Proposed defaults:
- **`bookshelf` extract:**
  - `max_duplicate_ids` 0; `max_empty_text_pct` 0; `max_failed_inputs` 0;
  - `min_rows_per_input` 4.0 (post-fix 10.2; pre-fix 1.8);
  - `min_text_bytes_per_input_byte` 0.02 (post-fix about 0.037; pre-fix about 0.006);
  - `max_status_pct` `{"partial": 15.0}` (post-fix 3.6%; placeholder parts are expected);
  - `allowed_licenses`: the normalized values seen plus `unknown`.
- **`europepmc_manuscript` extract:**
  - `max_duplicate_ids` 2 (re-issues across baseline and incremental, as seen); `max_failed_inputs` 0;
  - `max_empty_text_pct` 0;
  - `max_null_pct` `{"abstract": 15.0, "language": 25.0}`;
  - `min_rows_per_input` 200;
  - `allowed_licenses` `["text_mining"]`, after checking the real license strings in a sample.
- **`apollo` extract:** take the thresholds from a real sample in this task's report (read the existing apollo load result: 43 shards), with the same rule kinds.
- **`load` sections** for each source: `max_orphan_bodies` 0; `rows_loaded_equals_staging` true.

- [ ] **Step 1: failing tests:**
  - each rules file parses and uses only known keys;
  - **a fixture shaped like the pre-fix `bookshelf` output FAILS the `bookshelf` rules:** 40 rows from 22 inputs, text bytes about 0.6% of input bytes, 0 duplicates, 0 empty text. At least the yield rules must fire (Review Focus 1);
  - a fixture shaped like the post-fix output passes.
- [ ] **Step 2:** write the files. **Step 3:** pass, commit `feat(gate): rules for bookshelf, europepmc_manuscript and apollo`.

---

### Task 7: Docs

**Files:** `docs/10-data-sources-runbook.md`, `docs/project-incubation-baseline.md`.

- [ ] **Step 1: runbook**, a new section "The sample gate", in plain direct English:
  - what is gated (a source with a rules file, and the stages in it);
  - how to run a sample (`run_pipeline.sh <source> extract --sample`, then `load --sample`);
  - where the slice, the report and the pass are;
  - what invalidates a pass (any change in the stage's imported code or its wrapper, or in the rules file);
  - what a refusal looks like (exit 2 and the reason);
  - `--skip-gate --reason` and its audit row;
  - that sample runs write their audit rows and loads to `episteme_test`, and keep the production lock;
  - the migration check before a sample load;
  - how to read `gate_report.md`.

  Add `--sample` and `--skip-gate` to the usage line, the option list and the order of checks.
- [ ] **Step 2: drift log**, an entry for operating standard PR 4:
  - **what landed**;
  - **decisions:** JSON rules; the code hash over the import closure; the yield rule; hard link then copy; the pass in the production root; the migration-parity check;
  - **backlog:** pubchem's rules and slice wait for sub-project 2 (parts); the gate does not cover `download`, `graph`, `enrich` or `materialize` yet; already-loaded sources are not gated until they get a rules file.
- [ ] **Step 3:** `ruff format --check .`, `ruff check .`, the docs tests, and the full `timeout 1800 .venv/Scripts/python -m pytest -q -m "not pg"`. Commit `docs: the sample gate (operating standard PR 4)`.

---

## Rollout note

Once merged, a full run of a gated stage (bookshelf, europepmc_manuscript and apollo extract and load) refuses until a valid pass exists. Before the next production run of those sources:
1. Run `--sample` for each.
2. Read the report.
3. Adjust the thresholds only with a written reason.

Do not pull this branch into the main checkout while a pipeline run is in progress.

## Decisions (user, 2026-10-21: "Ok" — all six defaults)

1. **Gated stages:** `extract` (and `serialize`) plus `load`, with the sample load going to `episteme_test` (default), or `extract`/`serialize` only.
2. **`pubchem`:** its slice ("the first 2 parts and 1 from the middle") needs the parts from sub-project 2, so its rules and slice wait for that (default), or a whole-file slice now.
3. **Already-loaded sources** (pubmed, chembl, mesh, …): not gated until they get a rules file (default), or rules for all of them in this PR.
4. **The code-hash scope:** the stage module's whole `episteme.*` import closure plus its wrapper script (default). This is wider than the spec's "hash of the stage module", because the `bookshelf` fix also changed `jats.py` and `connection.py`. The other choice is the stage module only.
5. **Gate refusal exit code:** 2, like the dispatcher's other refusals before `run_start` (default), or a distinct code.
6. **Slice files:** a hard link where possible, else a copy (default), or always a copy.
