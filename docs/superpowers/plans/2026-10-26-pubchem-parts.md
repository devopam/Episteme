# pubchem in bounded parts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serialize `pubchem` with a streaming merge join into bounded parts that resume by CID range, and gate it with a cut sample slice and rules.

**Architecture:** `serialize_pubchem.py` replaces its DuckDB join with a line-by-line merge join over four CID-sorted gz files and writes one staging shard, marker and audit row per part. The gate gets a cut-file slice for pubchem, part-aware load measures, and two new rule keys.

**Tech Stack:** Python 3.13, `episteme.data.gz.open_gz` (isal), pyarrow staging via `staging_writer.write_rows`, DuckDB (gate measures only), pytest.

**Spec:** `docs/superpowers/specs/2026-10-26-pubchem-parts-design.md`

## Global Constraints

- Part caps come from `episteme.ops.profile.get_profile()` (`part_rows`, `part_bytes`; env `EPISTEME_PART_ROWS`, `EPISTEME_PART_BYTES`). No new env key.
- Part `source_file` and shard stem: `f"{input_key}__part{n:05d}"`, n from 1.
- Byte estimate per row: `len(row["text"].encode("utf-8")) + PART_ROW_OVERHEAD_BYTES`, with `PART_ROW_OVERHEAD_BYTES = 256` defined once in `src/episteme/ops/profile.py`.
- Rows for the same CID and input stay byte-identical to the DuckDB version (`pubchem_row` and `_build_text` do not change).
- No speed rule (`rows_per_s` is reported only). The full load is a plain `load`, not `--bulk` (spec §8 decision 2; ops step, no code).
- `src/episteme/config.py` is the only module that reads `os.environ`. Endpoint URLs only in `scripts/data/_lib/sources.env`.
- Tests: `uv run pytest -m "not pg"`. Every pg test command is prefixed `EPISTEME_DB_MODE=restricted PGDATABASE=episteme_test` (the shell `.env` is `unrestricted`; a bare pg test writes to production).
- No real-data run on `D:` while a production run is going (check `D:/episteme/_runlogs/*/SUMMARY.txt`). Tasks use fixtures only.
- Python files written with LF line endings (`write_bytes` or `newline="\n"`); `ruff format` and `ruff check` before each commit; `git add` explicit paths.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z`.

## Review Focus

1. A run killed in the middle of part k leaves a half-written shard `…__part0000k.parquet` without a marker: the restart must delete it and rewrite part k, and the loader must never see it as complete (Task 2, `test_resume_after_crash_mid_part`).
2. A restart with different caps (`part_rows` 1M → 2M): no CID lost or written twice across old and new parts (Task 2, `test_resume_with_different_caps_loses_no_cid`).
3. A sibling file that is unsorted far from its start (not in the first lines): the run fails with the file name and line number, and kept parts stay (Task 1, `test_unsorted_sibling_late_in_file_fails_fast`).
4. A gzip file with a CRLF line ending or a trailing tab: the CID and the value parse as for LF (Task 1, `test_crlf_and_trailing_tab_lines`).
5. The sample load check for a multi-part input: `rows_loaded` counts the part `source_file`s, not the input key (Task 3, `test_load_measures_use_staging_source_files`).

---

### Task 1: Streaming merge join

**Files:**
- Modify: `src/episteme/data/pubchem/serialize_pubchem.py` (remove `_columns_literal`, `_build_query`, the DuckDB use in `iter_rows_from_file`; update the module docstring's join paragraph)
- Test: `tests/data/test_serialize_pubchem.py`

**Interfaces:**
- Produces:
  - `class PubchemInputError(ValueError)` — a permanent error; message `"{path.name} line {lineno}: CID {raw!r} is not an integer"` or `"{path.name} line {lineno}: CID {cid} after {prev}: the file is not sorted by CID with one row per CID"`.
  - `_iter_cid_lines(path: Path, n_fields: int, stats: dict[str, int] | None = None) -> Iterator[tuple[int, list[str | None]]]` — strips `\r\n`, splits on tab, pads/truncates to `n_fields`, `""` → None; empty CID → `stats["skipped_no_id"] += 1` and skipped.
  - `iter_merged_records(primary_path: Path, sibling_paths: dict[str, Path], stats: dict[str, int] | None = None) -> Iterator[dict[str, Any]]` — keys `cid`, `smiles`, and per sibling the same record keys the DuckDB query produced (`iupac_name`, `title`, `formula`, `monoisotopic_mass`, `exact_mass`; masses as `float` or None).
  - `iter_rows_from_file(path, *, source_file=None, stats=None)` keeps its signature.

- [ ] **Step 1: Capture the golden rows with the current code.** Before any change, run the existing fixture of `test_pubchem_serialize_rows` through `iter_rows_from_file` and save the rows as `tests/data/fixtures/pubchem_golden_rows.json` (sorted keys, LF). Commit it alone: `test(pubchem): golden rows of the DuckDB join`.
- [ ] **Step 2: Write the failing tests** in `tests/data/test_serialize_pubchem.py`:
  - `test_merge_join_matches_golden_rows` — rows from `iter_rows_from_file` on the same fixture equal the golden JSON.
  - `test_sibling_with_gaps_and_extra_cids` — sibling lacks CID 2 and has CID 99 not in primary → CID 2 has None for that sibling, no row for 99.
  - `test_unsorted_primary_fails_fast` — primary `1,3,2` → `PubchemInputError` with `"CID-SMILES.tsv line 3: CID 2 after 3"` in the message.
  - `test_duplicate_cid_fails_fast` — `1,1` → `PubchemInputError`.
  - `test_non_integer_cid_fails_fast` — `"abc\tC"` → message contains `"CID 'abc' is not an integer"`.
  - `test_unsorted_sibling_late_in_file_fails_fast` — sibling sorted for 1,000 lines then one step back → error names the sibling file and line 1001; via `process_one` the result is `ok: False` and a failed marker exists.
  - `test_crlf_and_trailing_tab_lines` — `b"1\tC\r\n2\tCC\t\r\n"` (gz) → smiles `"C"`, `"CC"`.
  - `test_memory_is_streaming` — monkeypatch `duckdb_conn.connect` to raise; serialize still works (no DuckDB use).
- [ ] **Step 3: Run them** — `uv run pytest tests/data/test_serialize_pubchem.py -q`. Expected: the new tests FAIL (golden may pass already).
- [ ] **Step 4: Implement** the three functions and switch `iter_rows_from_file` to `iter_merged_records` + `pubchem_row`. Read through `open_gz(path, "rt", encoding="utf-8", errors="replace", newline="")` for `.gz`, `open(...)` with the same args otherwise. Sibling advance: move each sibling cursor while its CID < the primary CID; the sibling cursor validates its own order. Mass columns: `float(x)` when not None.
- [ ] **Step 5: Run** `uv run pytest tests/data/test_serialize_pubchem.py -q`. Expected: all PASS.
- [ ] **Step 6: Commit** `feat(pubchem): streaming merge join replaces the DuckDB join`.

### Task 2: Parts and resume by CID range

**Files:**
- Modify: `src/episteme/data/pubchem/serialize_pubchem.py` (`process_one`, module docstring "Bounded units of work")
- Modify: `src/episteme/ops/profile.py` (add `PART_ROW_OVERHEAD_BYTES = 256` near the part-cap rule)
- Test: `tests/data/test_serialize_pubchem_parts.py` (new)

**Interfaces:**
- Consumes: `iter_merged_records`, `pubchem_row` (Task 1); `get_profile().part_rows`, `.part_bytes`.
- Produces:
  - `part_source_file(key: str, n: int) -> str` → `f"{key}__part{n:05d}"`.
  - Part success marker stats: `n_rows`, `extract_status_counts`, `first_cid`, `last_cid`, `part_rows`, `part_bytes`, `est_bytes`, `write`.
  - Primary success marker stats: `n_rows`, `parts`, `parts_written`, `parts_kept`, `part_rows`, `part_bytes`, `extract_status_counts`, `elapsed_sec`, plus `iter_stats`.
  - Tests set caps by monkeypatching `serialize_pubchem._part_caps` → `(rows, bytes)`; define `_part_caps() -> tuple[int, int]` returning `(get_profile().part_rows, get_profile().part_bytes)`.

- [ ] **Step 1: Write the failing tests** (fixture: a 10-CID primary + siblings in `tmp_path`):
  - `test_parts_by_row_cap` — caps `(4, 10**9)` → shards `…__part00001..00003` with 4/4/2 rows; markers carry `first_cid`/`last_cid` 1/4, 5/8, 9/10; primary marker `parts == 3`; one `serialize_commit` audit call per part (patch `_best_effort_audit`).
  - `test_parts_by_byte_cap` — caps `(10**6, 2 * (PART_ROW_OVERHEAD_BYTES + <one row's text bytes>))` → 2 rows per part.
  - `test_resume_after_crash_mid_part` — make `write_rows` raise on part 2 after writing part 1; re-run with the real `write_rows` → part 1 not rewritten (mtime unchanged), part 2 rewritten, the stray half shard of part 2 replaced, total rows 10, each CID once.
  - `test_resume_with_different_caps_loses_no_cid` — run with `(4, …)`, kill after part 1 (delete primary marker and parts ≥2), re-run with `(3, …)` → kept part 1 = CIDs 1–4, new parts cover 5–10, union of CIDs over all shards is 1..10 with no duplicate; a warning is logged.
  - `test_force_rewrites_all_parts` — `--force` removes old parts and primary marker first; a stale `…__part00009` from a longer run is gone.
  - `test_parts_above_a_gap_are_dropped` — markers for parts 1 and 3 but not 2 → part 3 deleted, parts 2+ rewritten.
  - `test_primary_marker_skips_unit` — second run without `--force` returns `skipped: True`.
  - `test_input_key_collision_parts` — two primary files with the same basename in different dirs → part names differ (project collision rule).
- [ ] **Step 2: Run** `uv run pytest tests/data/test_serialize_pubchem_parts.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement `process_one`.** Resume algorithm (the part the signature does not fix):
  ```
  kept = longest prefix 1..j of part markers whose ranges chain (first_cid(i+1) > last_cid(i))
  delete shards+markers of every part > j (glob staging/pubchem/{key}__part*.*)
  if kept caps != current caps: _LOG.warning(...)
  skip records with cid <= last_cid(j) without building rows
  write parts j+1.. : a part ends when rows == part_rows or est_bytes >= part_bytes
  ```
  A record that `pubchem_row` drops counts in `skipped_no_id` and not in the caps. Keep the `TransientUnitError` / `mark_failed` handling as today; the failed marker's stats add `failed_part`.
- [ ] **Step 4: Run** `uv run pytest tests/data/test_serialize_pubchem.py tests/data/test_serialize_pubchem_parts.py -q`. Expected: PASS (fix older tests that read one shard name, e.g. `_read_shard`, to read all parts).
- [ ] **Step 5: Commit** `feat(pubchem): bounded parts with resume by CID range`.

### Task 3: Gate measures, rule keys and the pubchem stage

**Files:**
- Modify: `src/episteme/ops/gate_report.py` (`measure_staging`)
- Modify: `src/episteme/ops/gate.py` (`_EXTRACT_MODULES`/`STAGE_MODULES`, `_KNOWN_KEYS`, `_SIMPLE`, `evaluate`, `_cmd_report`)
- Test: `tests/ops/test_gate_report.py`, `tests/ops/test_gate.py`

**Interfaces:**
- Produces:
  - `measure_staging(...)` adds `source_files: list[str]` (sorted distinct `source_file`), `max_rows_per_source_file: int | None`, `max_est_bytes_per_source_file: int | None` (max over source_file of `sum(octet_length(text)) + count(*) * PART_ROW_OVERHEAD_BYTES`; None when no rows).
  - `STAGE_MODULES[("pubchem", "serialize")] == ("episteme.data.pubchem.serialize_pubchem", "pubchem/serialize_pubchem.sh")`.
  - Rule `max_peak_rss_mb` → `_max_rule` on `peak_rss_mb`.
  - Rule `parts_within_caps: true` → fails with `"parts_within_caps: a part has {n} rows, over the cap of {cap}"` or the bytes form; `_missing` when a measure or a cap is absent. `_cmd_report` puts `part_rows` and `part_bytes` from `get_profile()` into the measures before `evaluate`.
  - `_cmd_report` load: `source_files = measures["source_files"]` when present, else the old `input_key` list.

- [ ] **Step 1: Write the failing tests:**
  - `test_measure_staging_lists_source_files_and_part_maxima` — two shards with source_files `k__part00001` (3 rows) and `k__part00002` (1 row) → `source_files == ["k__part00001", "k__part00002"]`, `max_rows_per_source_file == 3`.
  - `test_load_measures_use_staging_source_files` — `_cmd_report` for a load with part shards calls `measure_load` with the part names (patch `measure_load` and `_connect`).
  - `test_parts_within_caps_pass_and_fail` — `evaluate({"max_rows_per_source_file": 5, "max_est_bytes_per_source_file": 10, "part_rows": 4, "part_bytes": 100}, {"parts_within_caps": True})` → one failure line naming 5 and 4; with 4 → `[]`.
  - `test_max_peak_rss_mb_rule` — 1,100 against 1,024 → one failure.
  - `test_pubchem_serialize_stage_is_known` — `stage_module("pubchem", "serialize")` returns the pair above, and the code hash of that stage includes `serialize_pubchem.py`.
- [ ] **Step 2: Run** `uv run pytest tests/ops/test_gate.py tests/ops/test_gate_report.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the changes listed under Interfaces.
- [ ] **Step 4: Run** `uv run pytest tests/ops -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gate): part-aware measures, parts_within_caps and max_peak_rss_mb`.

### Task 4: The pubchem cut slice

**Files:**
- Modify: `src/episteme/ops/gate_slice.py` (`SliceSpec` gains `build`; `prepare` uses it; `SLICES["pubchem"]`)
- Test: `tests/ops/test_gate_slice.py`

**Interfaces:**
- Consumes: `serialize_pubchem._iter_cid_lines`, `PubchemInputError`, `_PRIMARY_STEM`, `_SIBLING_DEFS` (Task 1); `get_profile().part_rows`.
- Produces:
  - `SliceSpec.build: Callable[[Path, Path], dict] | None = None` — `(raw_dir, dest_dir) -> {"files": [...], "inputs": [...], "bytes": int, **extra}`; when set, `prepare` calls it instead of select-and-link (same containment check and `slice.json` write; `linked = 0`, `copied = len(files)`).
  - `SLICES["pubchem"] = SliceSpec("pubchem", 1 * GB, <unused select>, Path("pubchem"), build=_build_pubchem)`; `max_bytes` applies to the written files.
  - `_build_pubchem(raw: Path, dest: Path, part_rows: int | None = None) -> dict` — extra keys `n_lines`, `ranges: [[lo, hi], [lo, hi]]` (inclusive CIDs).

- [ ] **Step 1: Write the failing tests** (fixture: primary with CIDs 1..100 in `Compound/Extras/CID-SMILES.gz`, siblings with gaps; `part_rows=10`):
  - `test_pubchem_slice_ranges` — ranges `[[1, 20], [51, 60]]` (line ⌊100/2⌋ = 50 → the 51st CID), `n_lines == 100`, `inputs == ["Compound/Extras/CID-SMILES.gz"]`.
  - `test_pubchem_slice_cuts_siblings_to_the_same_cids` — every written sibling holds only CIDs in the ranges, and every such CID the original sibling had.
  - `test_pubchem_slice_unsorted_line_fails` — a step back inside the read region → `SliceError` (wrapping `PubchemInputError`), no `slice.json` written.
  - `test_pubchem_slice_missing_primary_refuses` — no CID-SMILES → `SliceError`.
  - `test_pubchem_slice_round_trip` — `serialize_pubchem` on the slice yields 30 rows in 3 parts with `part_rows=10`.
- [ ] **Step 2: Run** `uv run pytest tests/ops/test_gate_slice.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Pass 1 counts primary lines. Pass 2 per file writes lines whose CID is in a range and stops after the last CID of R2; output through `gzip.open(dest, "wt", encoding="utf-8", newline="")`. Siblings missing from the raw dir are left out (the gate's `partial` rule catches it). R2 starts after skipping ⌊N/2⌋ lines; when R2 would start inside R1 (a small file), R2 starts after R1, and when the file ends first, R2 is shorter.
- [ ] **Step 4: Run** `uv run pytest tests/ops -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gate): pubchem slice cut to the same CID ranges in every file`.

### Task 5: Rules, pg load test and docs

**Files:**
- Create: `src/episteme/ops/gate_rules/pubchem.json`
- Modify: `tests/ops/test_gate_rules_sources.py` (pubchem in the list of sources with rules)
- Test (pg): `tests/data/test_load_articles.py` (add `test_load_multi_part_staging` and `test_reload_rewritten_part`)
- Modify: `docs/10-data-sources-runbook.md` (§4.9 gate table row for pubchem, remove "`pubchem` has none"; the pubchem source section: parts, resume, `--force`, full load with a plain `load` and why not `--bulk` (spec §8 decision 2)), `docs/project-incubation-baseline.md` (drift log entry: Finding, Fix, Decisions — no speed rule, plain load not `--bulk`; Deferred — first rule values to confirm on the first real sample, and the per-source `--bulk` index subset (spec §8 item 3); Rollout)

**Interfaces:**
- Consumes: rule keys from Task 3.

- [ ] **Step 1: Write `pubchem.json`:**
  ```json
  {
    "serialize": {
      "max_failed_inputs": 0,
      "max_duplicate_ids": 0,
      "max_empty_text_pct": 0,
      "allowed_licenses": ["public_domain"],
      "max_status_pct": {"partial": 20.0},
      "max_peak_rss_mb": 1024,
      "parts_within_caps": true
    },
    "load": {
      "max_orphan_bodies": 0,
      "rows_loaded_equals_staging": true
    }
  }
  ```
- [ ] **Step 2: Write the pg tests:** a staging dir with `k__part00001` (3 rows) and `k__part00002` (2 rows) loads 5 rows with two `load_success` markers; rewriting part 2 with a moved row (CID 4 now in part 1) and `--force` reload leaves 5 rows, CID 4 once.
- [ ] **Step 3: Run** `uv run pytest -m "not pg" -q` → PASS; then `EPISTEME_DB_MODE=restricted PGDATABASE=episteme_test uv run pytest -m pg tests/data/test_load_articles.py -q` → PASS (first confirm `select current_database(), current_setting('server_version')` is `('episteme_test','19beta3')`).
- [ ] **Step 4: Update the docs** listed above.
- [ ] **Step 5: Run the URL guard** `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'` → no output.
- [ ] **Step 6: Commit** `feat(gate): pubchem rules; docs and drift log for sub-project 2`.

## After the plan (ops, not tasks)

1. PR, review, user merges.
2. When no production run is going: `run_pipeline.sh pubchem serialize --sample`, then `pubchem load --sample`; set the `partial` limit from the report; commit if it changes.
3. With the user's go-ahead: `pubchem serialize`, then `pubchem load` (plain, no `--bulk`).
