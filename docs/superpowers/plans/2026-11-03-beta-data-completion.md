# Beta Data Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load the AACT trials snapshot, add EU SmPC section offsets to EPAR product information, and close three parked defects from PR #99.

**Architecture:** AACT loads one schema per release (`aact_r<YYYYMMDD>`), exposed by views in schema `aact` that switch in one transaction, with a per-release intervention-to-substance table. EPAR sections reuse the label sidecar and `label_section` (generalised by source), with an ordinal and product column added in migration 0011.

**Tech Stack:** Python 3.13, psycopg 3, PostgreSQL 19beta3, pytest.

**Spec:** `docs/superpowers/specs/2026-11-03-beta-data-completion-design.md` (approved 2026-11-03).

## Global Constraints

- `src/episteme/config.py` is the only module that reads `os.environ`. Every cap and share is a `Settings` value.
- Endpoint URLs live only in `scripts/data/_lib/sources.env`; the URL guard prints nothing.
- Markers and `source_file` follow `input_key`, and markers carry the database name (the PR #99 rule), with a collision test.
- No new download. Bounded memory: zip members stream, never unzip to disk; peak RSS under 2048 MB.
- TEXT in staging; typed columns only where the spec names them.
- Migrations continue at `0011`, with markers in `src/episteme/ops/gate_migrations.py`.
- Implementers never touch the D: drive or any database. Pg tests are written, not run; the controller runs them.
- Non-pg tests on the files touched: `uv run pytest -m "not pg" -q <paths>`. venv: `uv sync --extra data --extra dev`.
- `ruff format`, `ruff check`; `git add` explicit paths; never `--no-verify`. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z`.

## Review Focus

1. A text field holding `|` or a line break inside quotes → read as one field (Task 3 `test_quoted_pipe_and_newline_stay_one_field`).
2. A repeated load of a finished release with the same input → skipped; with other content → refused without `--force --reason` (Task 3 `test_repeat_release_skips_or_refuses`).
3. An SmPC heading repeated in the package leaflet ("4. Possible side effects") → not taken (Task 2 `test_leaflet_headings_are_not_sections`).
4. A PDF with two SmPCs (two strengths) → two rows per section, ordinal and product set (Task 2 `test_two_smpcs_give_two_rows_per_section`).
5. A trial whose `nct_id` is missing in `studies` → counted as an orphan; over the limit the switch does not happen (Task 3 `test_orphans_over_limit_keep_old_release`).

---

### Task 1: Schema, source classes, parked fixes

**Files:** Create `src/episteme/data/db/migrations/0011_label_section_ordinal.sql`. Modify `src/episteme/ops/gate_migrations.py`, `src/episteme/data/article_schema.py`, `src/episteme/data/regulatory/faers/load.py` (refusal message), `docs/10-data-sources-runbook.md` (5.12), `docs/superpowers/specs/2026-11-02-regulatory-sources-design.md` (5.5). Test `tests/data/test_source_classes.py`, `tests/data/test_migration_0011.py` (pg).

**Interfaces:** Produces `TRAINING_SOURCES: tuple[str, ...]` and `REFERENCE_SOURCES: tuple[str, ...]` in `article_schema`; `label_section.ordinal integer NULL`, `label_section.product text NULL`.

- [ ] **Step 1: Failing tests:** `test_every_source_is_classified_once` (each of `SOURCES` in exactly one set); `test_reference_sources_are_excluded_from_the_corpus` (`set(REFERENCE_SOURCES) <= set(get_settings().corpus_excluded_sources)`); `test_faers_signal_refusal_names_force`; pg `test_migration_0011_is_idempotent` and `test_label_section_has_ordinal_and_product`.
- [ ] **Step 2: Run** and see them fail.
- [ ] **Step 3: Implement.** `REFERENCE_SOURCES` holds `dailymed` and `ema_epar` (and `pubchem`, `chembl`, `uniprot`, `clinvar`, `reactome`, `mesh`, `ontologies`, `cdisc_ct` only if they are in `corpus_excluded_sources` today; otherwise they are training sources: read `config.CORPUS_EXCLUDED_SOURCES` and do not change corpus behaviour for any existing source). The migration uses `ADD COLUMN IF NOT EXISTS` and `SET LOCAL lock_timeout = '60s'`. The spec edit drops `substance_key` from `label_substance` and says the join goes through `unii`.
- [ ] **Step 4: Run** the non-pg tests → pass.
- [ ] **Step 5: Commit** `feat(schema): label_section ordinal and product; source classes; parked fixes`.

### Task 2: EPAR SmPC sections

**Files:** Create `src/episteme/data/ema/smpc.py`. Modify `src/episteme/data/dailymed/labels.py` (source as parameter), `src/episteme/data/ema/extract_epar.py`, `src/episteme/data/load_articles.py`, the EPAR and DailyMed retire paths. Test `tests/data/test_smpc.py`, `tests/data/test_epar.py`, `tests/data/test_dailymed_load.py`.

**Interfaces:** Produces `smpc_sections(body_text: str) -> list[dict]` with keys `loinc_code`, `title`, `ordinal`, `product`, `start_offset`, `end_offset`; `SMPC_RULE_VERSION: int` (starts at 1); `replace_labels(cur, source: str, shard: Path, ...)` and `delete_labels(cur, source: str, source_files)`. LOINC map exactly as spec section 5; other headings `smpc:<number>`.

- [ ] **Step 1: Failing tests:** `test_sections_from_annex_one_only`; `test_leaflet_headings_are_not_sections`; `test_two_smpcs_give_two_rows_per_section`; `test_offsets_point_into_body_text` (`body_text[start:end]` is the section text); `test_unmapped_heading_gets_smpc_code` (`"4.7"` → `"smpc:4.7"`); `test_no_headings_found_gives_no_rows_and_is_counted`; `test_rule_version_in_fingerprint_of_product_information_only`; `test_dailymed_labels_unchanged` (existing DailyMed tests pass unedited apart from the new `source` argument); pg `test_epar_part_load_writes_its_sections`.
- [ ] **Step 2: Run** and see them fail.
- [ ] **Step 3: Implement.** Build the heading pattern from real EPAR wording; use the 300-row feed sample at `C:\Users\devop\AppData\Local\Temp\claude\C--Users-devop-GitHub-Episteme\a17d7251-3580-49d4-9a91-1342dc06ed99\scratchpad\epar_sample.json` for names only; PDFs are not on disk yet, so tests generate PDFs with `pypdf` in the SmPC layout ("ANNEX I", "SUMMARY OF PRODUCT CHARACTERISTICS", "4.5 Interaction with other medicinal products and other forms of interaction", "ANNEX II"). The extract reports `documents_with_sections` and `documents_without_sections`.
- [ ] **Step 4: Run** the covering non-pg tests → pass.
- [ ] **Step 5: Commit** `feat(ema): SmPC section offsets in label_section`.

### Task 3: AACT loader

**Files:** Create `src/episteme/data/aact/__init__.py`, `src/episteme/data/aact/load_aact.py`, `src/episteme/data/aact/dictionary.py`, `src/episteme/data/aact/aact_dictionary.csv` (a copy of the owner's dictionary file, `C:\Users\devop\Downloads\aact_data_dictionary_2026-11-03.csv`; package data), `scripts/data/aact/load_aact.sh`. Modify `scripts/data/run_pipeline.sh` (`aact load`), `src/episteme/ops/gate.py`, `src/episteme/config.py`. Test `tests/data/test_aact_load.py` (mixed).

**Interfaces:** Consumes `load_substance.resolve_names`, `load_reg.latest_release`. Produces `release_id_from_zip(path: Path) -> str`, `read_dictionary(path: Path | None = None) -> dict[str, list[dict]]`, `iter_table(zip_path: Path, member: str) -> Iterator[list[str]]`, `load_release(conn, zip_path: Path, *, raw_dir: Path, run_id: str, force: bool = False, reason: str | None = None) -> dict`. New settings: `EPISTEME_AACT_MAX_ORPHAN_SHARE` (default 0.05, provisional).

- [ ] **Step 1: Failing tests** (tiny fixture zips built in `tmp_path`): `test_release_id_from_zip_name` (`20261103_export_ctgov.zip` → `"20261103"`; a bad date raises); `test_quoted_pipe_and_newline_stay_one_field`; `test_header_drives_columns_and_mismatches_are_reported`; `test_bad_date_loads_null_and_is_counted`; `test_missing_dictionary_tables_reported_not_fatal`; `test_marker_names_hold_the_database`; pg `test_release_loads_and_views_switch`; pg `test_repeat_release_skips_or_refuses`; pg `test_orphans_over_limit_keep_old_release`; pg `test_partial_load_resumes`; pg `test_intervention_substance_table_maps_names_to_substances`; pg `test_studies_equal_distinct_nct_id`.
- [ ] **Step 2: Run** and see them fail.
- [ ] **Step 3: Implement** per spec 4.2–4.4 and section 9 items 1–3, 9–11. `csv.reader(..., delimiter="|", quotechar='"')` over a text stream of the zip member with `newline=""`. COPY per table in batches capped by `load_batch_bytes`; one transaction and one marker per table; indexes, `ANALYZE`, row checks, `resolve_names`, `intervention_substance`, then views and the `reg_release` row in one transaction with a lock timeout from `EPISTEME_REG_LOCK_TIMEOUT_S`.
- [ ] **Step 4: Run** the non-pg tests → pass; URL guard prints nothing.
- [ ] **Step 5: Commit** `feat(aact): release-schema trials loader with moiety mapping`.

### Task 4: Gates and docs

**Files:** Create `src/episteme/ops/gate_rules/aact.json`. Modify `src/episteme/ops/gate_slice.py`, `src/episteme/ops/gate_report.py`, `src/episteme/ops/gate_rules/ema_epar.json`, `docs/02-data-sources.md`, `docs/08-data-storage-principles.md`, `docs/09-extraction-contract.md`, `docs/10-data-sources-runbook.md`, `docs/12-source-inventory.md`, `docs/project-incubation-baseline.md`. Test `tests/ops/test_gate_slice_regulatory.py`, `tests/ops/test_gate_report_regulatory.py`, `tests/docs/`.

- [ ] **Step 1: Failing tests:** `test_aact_slice_is_the_whole_file`; `test_aact_rules_need_tables_rows_and_orphans_under_limit`; `test_epar_reports_section_share` (report only, no limit); docs tests for `aact`.
- [ ] **Step 2: Run** and see them fail.
- [ ] **Step 3: Implement.** Measures per spec 7: tables with rows, studies equal distinct `nct_id`, orphan share per table, intervention coverage (recorded), `peak_rss_mb` at most 2048. Docs: AACT row in 02 and 12 with the licence posture of spec section 9 item 8; storage and views in 08; runbook sections for the AACT load, release switch, dropping an old release schema, the EPAR section counts, and the beta limits (no version history); drift-log entry with the spec's decisions.
- [ ] **Step 4: Run** `uv run pytest -m "not pg" -q tests/ops tests/docs` → pass.
- [ ] **Step 5: Commit** `feat(gate): AACT gate and docs; EPAR section share`.
