# Load Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `pubmed`/`pmc` loads remove the OpenAlex rows they duplicate, EMA downloaders keep their request spacing across runs, and the remaining tuning constants move into `config.py`.

**Architecture:** The sweep is a new step in `postgres_loader.load_source_file`, the mirror of the PR 110 dedup (sorted key tables in `pg_temp`, LATERAL index probes, delete by id). Pacing state is a small new module `episteme/data/pacing.py` used by both downloaders. Tunables follow the existing `DEFAULT_* + Settings field + _get_*_or_default` pattern in `config.py`.

**Tech Stack:** Python 3.13, psycopg 3, PostgreSQL 19beta3, pytest.

**Spec:** `docs/superpowers/specs/2026-11-12-load-followups-design.md` (owner-approved 2026-11-12).

## Global Constraints

- `config.py` stays the only module that reads `os.environ`; every new tunable has env key `EPISTEME_<NAME>` and today's value as default.
- Implementers run only `pytest -m "not pg"`. pg tests are written by implementers and run by the controller on `episteme_test`. Nobody touches the `episteme` database (a full OpenAlex load runs there) or real data on D:.
- `ruff format` and `ruff check` on changed Python; the URL guard `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'` prints nothing.
- No new dependency. No migration.
- `git add` explicit paths only; never `--no-verify`.

## Rulings (controller, before execution)

- **R1 (pacing location).** The spec says `<raw_dir>/_pacing/<host>.json` and "one file per host, shared by both downloaders". The two downloaders use different `raw_dir`s, so a per-`raw_dir` file cannot be shared. Ruling: `get_settings().raw_root / "_pacing" / f"{host}.json"`, with a `pacing_dir: Path | None` parameter as a test seam. Cost if wrong: one path constant.
- **R2 (sweep under `--bulk`).** `bulk_indexes.py` drops the parent `articles_pmid_idx`, `articles_pmcid_idx` and `articles_doi_idx`, which removes the `articles_openalex` copies the sweep probes. Ruling: the sweep's readiness check refuses a `pubmed`/`pmc` shard only when those indexes are missing **and** `articles_openalex` holds at least one row; with no openalex rows the sweep is skipped with `swept_openalex = 0`. A bulk pubmed reload while openalex rows exist is refused with a message naming the rebuild; a post-rebuild bulk sweep is recorded as deferred in the drift log. Cost if wrong: a later bulk pubmed reload needs a small follow-up.

## Review Focus

1. A pubmed shard whose DOI has upper-case letters (pubmed stores the publisher's case) must still sweep an openalex row whose DOI is the lower-case form — Task 3 test `test_sweep_matches_doi_case_insensitively`.
2. One key matching two openalex rows (two works, one DOI) must remove both — Task 3 `test_sweep_removes_every_match`.
3. A reload (`--force`) of a pubmed shard that was already swept must report `swept_openalex=0` and not fail — Task 3 `test_sweep_on_reload_is_zero`.
4. A pacing file written with a time in the future (clock change) must cap the wait at `min_interval`, never block for hours — Task 2 `test_future_time_caps_wait`.
5. A tunable set to `0` or a non-number in the environment falls back to its default (existing `_get_positive_*` behaviour) — Task 1 `test_new_tunables_bad_values_fall_back`.

---

### Task 1: Tuning values into config

**Files:**
- Modify: `src/episteme/config.py` (new `DEFAULT_*` constants, `Settings` fields, `get_settings` wiring)
- Modify: `src/episteme/data/dailymed/labels.py:74`, `src/episteme/data/pmc/download_pmc.py:89`, `src/episteme/sft/slice.py:24-25`, `src/episteme/data/reference_docs/acquire.py:81-86`, `src/episteme/data/regulatory/download_ema.py:61`, `src/episteme/ops/gate_slice.py:26-29`
- Test: `tests/test_config_tunables.py`

**Interfaces:**
- Produces (Settings fields, env key in brackets, default):
  - `dailymed_copy_batch_rows: int` [`EPISTEME_DAILYMED_COPY_BATCH_ROWS`] 20_000
  - `pmc_submit_batch: int` [`EPISTEME_PMC_SUBMIT_BATCH`] 2_000
  - `sft_slice_batch_rows: int` [`EPISTEME_SFT_SLICE_BATCH_ROWS`] 2_048
  - `sft_slice_rows_per_part: int` [`EPISTEME_SFT_SLICE_ROWS_PER_PART`] 50_000
  - `reference_docs_connect_timeout_s: float` [`EPISTEME_REFERENCE_DOCS_CONNECT_TIMEOUT_S`] 30
  - `reference_docs_read_timeout_s: float` [`EPISTEME_REFERENCE_DOCS_READ_TIMEOUT_S`] 120
  - `reference_docs_max_unpacked_bytes: int` [`EPISTEME_REFERENCE_DOCS_MAX_UNPACKED_BYTES`] 2 * 1024**3
  - `reference_docs_max_wait_s: float` [`EPISTEME_REFERENCE_DOCS_MAX_WAIT_S`] 900
  - `reference_docs_fallback_throttle_wait_s: float` [`EPISTEME_REFERENCE_DOCS_FALLBACK_THROTTLE_WAIT_S`] 30
  - `ema_connect_timeout_s: float` [`EPISTEME_EMA_CONNECT_TIMEOUT_S`] 30
  - `bookshelf_max_package_bytes: int` [`EPISTEME_BOOKSHELF_MAX_PACKAGE_BYTES`] 1 GiB
  - `apollo_min_bytes: int` [`EPISTEME_APOLLO_MIN_BYTES`] 10 MiB
- Module names kept as aliases only where tests import them today (`MAX_UNPACKED_BYTES`, `DEFAULT_MAX_WAIT_S`, `BOOKSHELF_MAX_PACKAGE_BYTES`, `APOLLO_MIN_BYTES`): replace each use with the setting read at call time; keep a module constant only if an existing test or caller imports it (then it equals the `config` default).

- [ ] **Step 1:** Add to `tests/test_config_tunables.py`: `test_new_tunables_default_to_todays_values` (every field above equals its default with no env), `test_new_tunables_read_from_env` (each env key set to a distinct valid value is read), `test_new_tunables_bad_values_fall_back` (`"0"` and `"abc"` give the default).
- [ ] **Step 2:** Run `pytest tests/test_config_tunables.py -q`; expect failures (fields missing).
- [ ] **Step 3:** Add the constants, fields and wiring in `config.py` using `_get_positive_int_or_default` / `_get_positive_float_or_default`.
- [ ] **Step 4:** Replace each module constant use with `get_settings().<field>` read at call time (not at import).
- [ ] **Step 5:** Check `ops/gate_slice.py` values against the gate hash closure (`grep -rn "gate_slice" src/episteme/ops/*hash*` or the code-hash file list); if `config.py` is in a hashed closure, state it in the report; tuning values must not change a stage's code hash beyond what `config.py` already does.
- [ ] **Step 6:** Run `pytest -m "not pg" -q tests/test_config_tunables.py tests/ops tests/data/reference tests/sft` (and any test file that imported a moved constant); expect PASS.
- [ ] **Step 7:** `ruff format` + `ruff check` on changed files; commit `refactor(config): move remaining tuning constants into Settings`.

### Task 2: Persistent pacing for the EMA downloaders

**Files:**
- Create: `src/episteme/data/pacing.py`
- Modify: `src/episteme/data/reference_docs/acquire.py` (`fetch`: `pace()` and the throttle branch)
- Modify: `src/episteme/data/regulatory/download_ema.py` (`fetch_ema_feeds`: the delay before each request and the throttle path)
- Test: `tests/data/test_pacing.py`, plus a case in the existing acquire and download_ema test files

**Interfaces:**
- Produces in `episteme.data.pacing`:
  - `pacing_path(host: str, pacing_dir: Path | None = None) -> Path` — `pacing_dir or get_settings().raw_root / "_pacing"`, file `f"{host}.json"`; `host` validated `^[A-Za-z0-9.-]+$` (ValueError otherwise).
  - `read_last_request(path: Path) -> datetime | None` — UTC-aware; missing file → None silently; empty/corrupt → None and one `logging` warning.
  - `record_request(path: Path, at: datetime) -> None` — writes `{"host": <stem>, "last_request_at": <ISO 8601 UTC>}` atomically (temp file + replace); creates the directory.
  - `wait_seconds(last: datetime | None, now: datetime, min_interval_s: float) -> float` — `0` when `last` is None; `max(0, min_interval_s - (now - last))`, capped at `min_interval_s` (covers a future `last`).
- Consumes: `get_settings().raw_root`.
- Both downloaders take a new keyword `pacing_dir: Path | None = None` and a wall clock seam `now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)`.

- [ ] **Step 1:** Write `tests/data/test_pacing.py`: `test_missing_file_no_wait`, `test_recent_request_waits_remainder` (last 20 s ago, interval 60 → 40), `test_old_request_no_wait` (10 min ago → 0), `test_future_time_caps_wait` (last 3 h ahead → 60), `test_corrupt_file_warns_and_returns_none` (caplog has one WARNING), `test_record_then_read_round_trip`, `test_bad_host_refused`.
- [ ] **Step 2:** Run; expect FAIL (module missing). Implement `pacing.py`; run; expect PASS.
- [ ] **Step 3:** In `acquire.fetch`, before each request wait the larger of the in-memory wait and `wait_seconds(read_last_request(p), now(), min_interval_s)` (host = `urlsplit(ref.url).hostname`); after every request (any status) `record_request(p, now())`; on a throttle with `Retry-After = r` record `now() + timedelta(seconds=max(0, r - min_interval_s))`. The wait still counts against `budget_s` as today.
- [ ] **Step 4:** Same in `download_ema.fetch_ema_feeds` with `delay` as the interval; host from the feed URL.
- [ ] **Step 5:** Tests in the existing acquire / download_ema test files: `test_second_run_waits_remaining_interval` (fake clocks: run 1 records at T, run 2 starting at T+14 s sleeps 46 s before its first request with interval 60), `test_retry_after_is_honoured_by_next_run`, `test_unreadable_pacing_file_does_not_stop_the_run`. Use `tmp_path` as `pacing_dir`; no network (existing fake session).
- [ ] **Step 6:** `pytest -m "not pg" -q tests/data` (the touched test files); expect PASS. ruff; commit `fix(ema): keep request spacing across runs (per-host pacing file)`.

### Task 3: OpenAlex reverse sweep + lower-case OpenAlex DOIs

**Files:**
- Modify: `src/episteme/data/postgres_loader.py` (constants, readiness check, probe SQL, sweep step in `load_source_file`, result/lineage/audit)
- Modify: `src/episteme/data/load_articles.py` (~line 306: `ok` line note)
- Modify: `src/episteme/data/openalex/serialize_openalex.py` (`_bare_doi` returns lower case)
- Test: `tests/data/test_dedup_probe_sql.py` (non-pg), `tests/data/test_openalex_sweep.py` (pg, new; controller runs), serializer test file for `_bare_doi`

**Interfaces:**
- Produces in `postgres_loader`:
  - `SWEEP_INTO: dict[str, str] = {"pubmed": "openalex", "pmc": "openalex"}`
  - `_SWEEP_REQUIRED: tuple[str, ...] = ("episteme.articles_openalex", "episteme.article_body_openalex", "episteme.articles_openalex_pmid_idx", "episteme.articles_openalex_pmcid_idx", "episteme.articles_openalex_doi_idx")`
  - `_check_sweep_ready(cur, target: str) -> bool` — True = sweep runs; False = no `target` rows exist and an index is missing (skip, count 0); raises RuntimeError (message names `bash scripts/data/db/bulk_load_indexes.sh rebuild` and the drift-log deferral) when an index is missing and `target` rows exist (Ruling R2). Missing partitions always raise ("apply migration 0012").
  - `_sweep_probe_sql(key: str, target: str) -> str` — `SELECT a.id FROM pg_temp._sweep_keys k CROSS JOIN LATERAL (SELECT a.id FROM episteme.articles a WHERE a.source = '<target>' AND a.<key> IS NOT NULL AND a.<key> = k.k) a`. No `LIMIT`. For `doi` the key table holds `lower(doi)` of the new rows and the probe compares the raw `a.doi` (OpenAlex DOIs are lower case). `target` validated against `article_schema.SOURCES`; `key` against `DEDUP_KEYS`.
  - `_sweep(cur, source_files: list[str], source: str, target: str) -> int` — per key in `DEDUP_KEYS`: `pg_temp._sweep_keys` = sorted distinct key values of `episteme.articles WHERE source = <source> AND source_file = ANY(<that shard's source_files via a temp table>)` (lower(doi) for doi), unique index + ANALYZE; insert probe hits into `pg_temp._sweep_ids` (distinct); then `DELETE FROM episteme.article_body WHERE source = <target> AND article_id IN (SELECT id FROM pg_temp._sweep_ids)` and the same on `articles` (`id`); returns the `articles` rowcount. Probes run inside `_forced_nested_loops(cur)`.
- `load_source_file` result for a sweeping source gains `"swept_openalex": int`; `_lineage.dedup_counts` of each pubmed/pmc lineage row = `{"swept_openalex": n}` (Jsonb); audit `new_value` = the same dict. Non-sweeping, non-dedup sources: unchanged (no key).
- `load_articles` `ok` line appends ` swept_openalex=<n>` when the key is present.
- `serialize_openalex._bare_doi(raw) -> str | None` returns the bare DOI `.lower()`.

- [ ] **Step 1 (non-pg tests):** In `tests/data/test_dedup_probe_sql.py` add `test_sweep_probe_is_lateral_without_limit` (contains `CROSS JOIN LATERAL`, no `LIMIT`, `a.source = 'openalex'`), `test_sweep_probe_refuses_unknown_target_and_key`, and for `_bare_doi`: `https://doi.org/10.1000/ABC.Def` → `10.1000/abc.def`, `None` → `None`.
- [ ] **Step 2:** Run them; expect FAIL. Implement `_sweep_probe_sql` and the `_bare_doi` change; run; expect PASS.
- [ ] **Step 3 (pg tests, file `tests/data/test_openalex_sweep.py`, `pytestmark = pytest.mark.pg`, same fixtures as `test_openalex_dedup_load.py`):** load openalex rows first (via `load_source_file(source="openalex")` on staged shards), then a pubmed shard; tests:
  - `test_pubmed_load_sweeps_matching_openalex_rows` (match by pmid; the openalex row is gone from `articles` and `article_body`, an unrelated openalex row stays; `r["swept_openalex"] == 1`; `_lineage.dedup_counts == {"swept_openalex": 1}`)
  - `test_sweep_matches_doi_case_insensitively` (pubmed DOI `10.1000/ABC`, openalex `10.1000/abc`)
  - `test_sweep_removes_every_match` (two openalex rows, one DOI → 2)
  - `test_pmc_load_sweeps_too` (match by pmcid)
  - `test_sweep_on_reload_is_zero` (same pubmed shard with `force` path → 0, no error)
  - `test_no_match_reports_zero`
  - `test_non_sweeping_source_has_no_sweep_key` (a dailymed or chembl fixture shard: no `swept_openalex`)
  - `test_sweep_plan_uses_openalex_indexes` (EXPLAIN of each `_sweep_probe_sql` under `_forced_nested_loops`: no `Seq Scan`/`Bitmap`/`Sort`/`Unique`/`Hash` on `articles_openalex`; scans on `articles_openalex` name `_pmid_idx`/`_pmcid_idx`/`_doi_idx`)
  - `test_sweep_refuses_when_indexes_missing_and_openalex_rows_exist` (DROP INDEX `episteme.articles_pmid_idx` uncommitted, then rollback, as in the existing refuse test) and `test_sweep_skips_when_indexes_missing_and_no_openalex_rows`.
  Implementers write these; do not run them (no database). The controller runs them.
- [ ] **Step 4:** Implement `_check_sweep_ready`, `_sweep`, the call in `load_source_file` (after the inserts and the dedup block, before the `_lineage` writes; the readiness check before any work on the shard, next to `_check_dedup_ready`), result/lineage/audit wiring, and the `ok` line.
- [ ] **Step 5:** `pytest -m "not pg" -q tests/data`; expect PASS. ruff; commit `feat(load): pubmed/pmc loads sweep the openalex rows they duplicate`.

### Task 4: Docs and drift log

**Files:**
- Modify: `docs/10-data-sources-runbook.md` (section 5.2 "OpenAlex load": post-load check `SELECT count(*) FROM episteme.articles_openalex WHERE doi <> lower(doi)` must be 0; pubmed/pmc loads sweep openalex and print `swept_openalex`; the `--bulk` refusal of Ruling R2. Reference-docs and EMA sections: the `_pacing/` folder under the raw root.)
- Modify: `docs/09-extraction-contract.md` only if it lists load result or `dedup_counts` keys (add `swept_openalex`).
- Modify: `.env.example` if it exists (new keys, names only, commented defaults).
- Modify: `docs/project-incubation-baseline.md` (new dated entry 2026-11-12 "load follow-ups": what landed, Rulings R1 and R2, deferred: post-rebuild bulk sweep; openalex `_lineage.rows_inserted` stays the load-time count).

- [ ] **Step 1:** Write the doc changes in plain STE style (short sentences, one instruction per step).
- [ ] **Step 2:** Run the URL guard and `pytest -m "not pg" -q tests/docs` (if doc tests exist); expect nothing printed / PASS.
- [ ] **Step 3:** Commit `docs: load follow-ups (sweep, pacing, tunables)`.

### Task 5: refmeta fixes from the PR 111 rollout (added 2026-11-16, controller ruling)

The production rollout of PR 111 (2026-11-16) found two defects. They join this branch (owner rule: no piecemeal PRs). `main` (with PR 111) is merged into this branch (de4f71f1).

**Files:**
- Modify: `src/episteme/data/refmeta/ucum.py`
- Modify: `scripts/data/run_pipeline.sh` (the option parsing that builds `load_args` / `download_args` for `refmeta`)
- Modify: `docs/superpowers/specs/2026-11-15-reference-metadata-design.md` (the `ucum` row of the key table)
- Test: `tests/data/test_refmeta.py`; the existing `run_pipeline` argument tests (`grep -ln "run_pipeline" tests/*.py`)

**Interfaces:**
- `parse_ucum(path, *, notice, source_file, release_id) -> CodeRelease`: unchanged signature. The code becomes `<kind>:<Code attribute>` (case-sensitive; UCUM keeps `Code` unique), falling back to `printSymbol`, then `name`, only when `Code` is absent. `extra` keeps `kind`, `code_attr` and adds `print_symbol`.
- `run_pipeline.sh refmeta load` and `refmeta download` forward `--system <name>` (repeatable) to the wrapper.

- [ ] **Step 1:** Tests: in `tests/data/test_refmeta.py` update `test_ucum_iso639_iso3166_and_loinc` to expect `["prefix:k", "unit:m/s"]` from `Code` (same values as today) and add `test_ucum_codes_use_the_code_attribute`: two units `<unit Code="10*"><printSymbol>10</printSymbol>...` and `<unit Code="10^"><printSymbol>10</printSymbol>...` give codes `unit:10*` and `unit:10^` (no duplicate), with `extra["print_symbol"] == "10"`. A test for the pipeline: `run_pipeline.sh refmeta load --system ucum --system iso639` passes both `--system` options to `load_refmeta.sh` (use the existing dry-run / argument-capture pattern of the run_pipeline tests; no database, no network).
- [ ] **Step 2:** Run; expect FAIL. Implement; run; expect PASS.
- [ ] **Step 3:** Real-file check (read-only, no database): `.venv/Scripts/python.exe -c "..."` parse `D:/episteme/01_raw/refmeta/ucum/dfccea1b5dc2/ucum-essence.xml` with `parse_ucum` and print the code count and the number of distinct codes (must be equal). Put the output in the report.
- [ ] **Step 4:** Spec row: `ucum` key = `<kind>:<Code>` (fallback printSymbol, name).
- [ ] **Step 5:** `pytest -m "not pg" -q` on the touched tests; ruff; commit `fix(refmeta): UCUM codes keyed on the Code attribute; run_pipeline forwards --system`.
