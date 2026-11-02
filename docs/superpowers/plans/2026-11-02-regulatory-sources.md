# Regulatory Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load DailyMed, openFDA (FAERS, recalls, NDC, Drugs@FDA, Orange Book, shortages, substances) and EMA (feeds, Article 57, EPAR text) into one pluggable, efficiently partitioned and indexed model, joined on the active moiety.

**Architecture:** Reference feeds go through one config file and one partitioned `reg_record` table, one snapshot per release. FAERS gets typed tables by received year, refreshed by generation swap, with precomputed signal tables. Labels and EPAR text become `articles` rows. `substance` and `substance_xref`, anchored on FDA's substance data, join everything at query time.

**Tech Stack:** Python 3.13, psycopg 3, pyarrow, ijson (new), openpyxl (new), pypdf (new), PostgreSQL 19beta3, pytest.

**Spec:** `docs/superpowers/specs/2026-11-02-regulatory-sources-design.md` (approved 2026-11-02).

## Global Constraints

- `src/episteme/config.py` is the only module that reads `os.environ`.
- Endpoint URLs live only in `scripts/data/_lib/sources.env`; this prints nothing: `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'`. `feeds.toml` holds env key names, never URLs.
- Marker keys and stored `source_file` values follow `input_key`, or a name built from it, with a collision test.
- Tunable values live in config, not source constants. `feeds.toml` is outside every stage code hash; its values go into the run record.
- Reference feeds: one full snapshot per release. FAERS and text sources: latest copy only. No code moves partitions between disks.
- Composition means the active moiety by default. Signal tables are keyed on `moiety_key`.
- No fuzzy name matching. Unmatched names are counted, and the coverage goes into the gate report.
- No table stores a resolved key in a fact row except through the small bridge tables named in the spec; an xref improvement never rewrites a large table.
- Bounded memory: every file over a batch is streamed. `other/substance` is one 2.4 GB JSON and is never loaded whole. FAERS zips stay zipped.
- Migrations continue at `0010`; each new one gets markers in `src/episteme/ops/gate_migrations.py`.
- Non-pg tests: `uv run pytest -m "not pg" -q`. Pg tests are written by implementers and run by the controller only: `set -a; . ../Episteme/.env; set +a; EPISTEME_DB_MODE=restricted PGDATABASE=episteme_test uv run pytest -m pg -q <files>`.
- Implementers never touch the D: drive or any database. Tests use `tmp_path` fixtures. Run `ruff format` and `ruff check` before each commit; `git add` explicit paths; never `--no-verify`.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z`.

## Review Focus

1. EMA answers with its human-verification HTML page and status 200 instead of JSON or PDF → the stage stops and reports; it never stores the page as data (Task 2 test `test_ema_verification_page_stops_the_stage`).
2. A feed truncated mid-download (seen: curl error 18 on the non-EPAR feed) or with rows differing from `meta.total_records` → the release is not published and `is_latest` does not move (Task 3 test `test_short_feed_does_not_become_latest`).
3. A FAERS report with no usable `receivedate` → it goes to the default partition, is counted, and does not fail the unit (Task 5 test `test_report_without_date_goes_to_default_year`).
4. A recall whose `code_info` holds no recognisable lot → it stays in the recall data, adds no `recall_lot` row, and is counted as "no extractable lot" (Task 6 test `test_recall_without_lot_is_counted`).
5. A DailyMed inner zip that is corrupt, or with no XML → that label is listed as failed and the bucket completes (Task 7 test `test_corrupt_inner_zip_fails_one_label_only`).

---

### Task 1: Folded items and the schema

**Files:**
- Create: `src/episteme/data/db/migrations/0010_regulatory_sources.sql`
- Modify: `src/episteme/ops/gate_migrations.py`, `src/episteme/data/article_schema.py` (`SOURCES`), `src/episteme/config.py`, `src/episteme/data/pmc/extract_pmc.py`, `src/episteme/data/pmc/buckets.py`, `src/episteme/data/postgres_loader.py`, `src/episteme/data/staging_writer.py`, `src/episteme/ops/gate_slice.py`, `scripts/mcpg-episteme.sh`
- Test: `tests/data/test_regulatory_schema.py` (pg), `tests/test_config_tunables.py`

**Interfaces:**
- Produces, in `config.Settings` with env keys and today's values as defaults: `load_batch_bytes` (`EPISTEME_LOAD_BATCH_BYTES`, 64 MiB), `write_batch_bytes` (`EPISTEME_WRITE_BATCH_BYTES`, 64 MiB), `pmc_bucket_ids` (`EPISTEME_PMC_BUCKET_IDS`, 50000), and the slice caps as `EPISTEME_SLICE_MAX_BYTES_<SOURCE>`.
- Produces tables, exactly as spec sections 5.2–5.5: `reg_release`, `reg_record` (LIST dataset, then LIST release_id), `substance`, `substance_xref`, `faers_report`, `faers_drug`, `faers_reaction` (RANGE `receive_year`, 2004–2030 plus default), `faers_drug_entry`, `faers_entry_substance`, `faers_term`, `faers_pair_quarter`, `faers_event_quarter`, `faers_quarter_totals`, `faers_batch_coverage`, `faers_batch_quarter`, `recall_lot`, `label_section`, `label_substance`; LIST partitions `articles_dailymed`, `articles_ema_epar`, and the matching `article_body` partitions. `faers_drug` carries `batch_number` and `batch_norm`. The FAERS parents hold no indexes; indexes are built per generation (Task 5).
- `SOURCES` gains `dailymed` and `ema_epar`.

- [ ] **Step 1: Write the failing tests:**
  - `test_tunables_default_to_todays_values`: with no env, `get_settings().load_batch_bytes == 64 * 1024 * 1024` and `pmc_bucket_ids == 50000`.
  - `test_tunables_read_from_env`.
  - pg `test_migration_0010_is_idempotent`: apply twice, no error.
  - pg `test_only_one_latest_release_per_dataset`: a second `is_latest` row for one dataset raises `UniqueViolation`.
  - pg `test_latest_release_prunes_to_one_partition`: `EXPLAIN` with literal `dataset` and `release_id` names one leaf partition.
  - pg `test_faers_year_prunes`: `EXPLAIN` of `faers_drug` with `receive_year >= 2024` names only the partitions from 2024 up.
- [ ] **Step 2: Run them** and see them fail.
- [ ] **Step 3: Implement** the migration and its markers. Read the constants from `get_settings()` at the point of use, keeping the existing names importable. The existing tests must pass unedited.
- [ ] **Step 4: Fix the PMC prologue logging:** the discover, group and manifest INFO lines must reach the run log. Test `test_prologue_phases_are_logged` asserts with `capfd` that they reach stderr when run through `_main`.
- [ ] **Step 5: Bring in the launcher edit.** In the `--admin` branch of `scripts/mcpg-episteme.sh`, after `unset MCPG_SECONDARY_DATABASE_URLS`, set `MCPG_SECONDARY_DATABASE_URLS` to `episteme_test` as `episteme_sys_admin`, when `primary` is `episteme` and `EPISTEME_SYS_ADMIN_PASSWORD` is set. The controller has the same edit uncommitted in the main checkout and removes it there after the merge.
- [ ] **Step 6: Run** `uv run pytest -m "not pg" -q tests/data tests/ops tests/test_config_tunables.py` → pass; `shellcheck scripts/mcpg-episteme.sh` → clean.
- [ ] **Step 7: Commit:** `feat(reg): schema for regulatory sources; tunables in config; prologue logging`.

### Task 2: Feeds, parsers and downloads

**Files:**
- Create: `src/episteme/data/regulatory/__init__.py`, `src/episteme/data/regulatory/feeds.toml`, `src/episteme/data/regulatory/feeds.py`, `src/episteme/data/regulatory/parsers.py`, `src/episteme/data/regulatory/download_ema.py`, `scripts/data/ema/download_ema.sh`
- Modify: `scripts/data/_lib/sources.env`, `scripts/data/openfda/download_openfda.sh`, `scripts/data/run_pipeline.sh` (`WRAPPER[ema]`), `pyproject.toml`, `uv.lock` (add `ijson`, `openpyxl`)
- Test: `tests/data/test_regulatory_feeds.py`, `tests/data/test_download_ema.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Feed: name; url_key; format; jurisdiction; record_key: tuple[str, ...]; substance_fields: tuple[str, ...]; product_fields: tuple[str, ...]; date_field: str | None; cadence: str`.
- Produces: `load_feeds(path: Path | None = None) -> dict[str, Feed]`.
- Produces: `parse(feed: Feed, path: Path) -> Iterator[dict]`, dispatching on `format`: `ema_json`, `article57_xlsx`, `openfda_json`.
- Produces: `class FeedVerificationError(RuntimeError)`, `fetch_ema_feeds(raw_dir: Path, feeds: dict[str, Feed]) -> dict`.
- `feeds.toml` entries, one per feed: `ema_medicines`, `ema_post_authorisation`, `ema_orphan`, `ema_pip`, `ema_psusa`, `ema_referrals`, `ema_dhpc`, `ema_shortages`, `ema_mrl`, `ema_herbal`, `ema_outside_eu`, `ema_article57`, `ema_epar_documents`, `fda_enforcement`, `fda_ndc`, `fda_drugsfda`, `fda_orangebook`, `fda_shortages`. Field names as measured in the spec, section 3.

- [ ] **Step 1: Write the failing tests:**
  - `test_every_feed_has_a_url_key_in_sources_env`.
  - `test_feeds_toml_holds_no_urls`: no value contains `://`.
  - `test_ema_json_parser_reads_data_rows_and_checks_total`.
  - `test_article57_parser_finds_the_header_row_and_streams`: a fixture workbook with the header at row 7; uses `openpyxl` read-only mode.
  - `test_openfda_json_parser_reads_results_from_the_zip`.
  - `test_ema_verification_page_stops_the_stage`: a fake response of HTML with status 200 raises `FeedVerificationError`, stores no file, and writes a failed marker.
  - `test_truncated_download_is_retried_once_then_fails`.
  - `test_substance_mode_selects_only_other_substance`: the shell `want()` selection, run through `bash` on a fixture catalogue.
- [ ] **Step 2: Run them** and see them fail.
- [ ] **Step 3: Implement.** Download with a configured delay (`EPISTEME_EMA_DELAY_SECONDS`, default 2), one file at a time, written atomically, and marker per feed. Add mode `substance` to `download_openfda.sh`, selecting `other/substance`.
- [ ] **Step 4: Run** `uv run pytest -m "not pg" -q tests/data/test_regulatory_feeds.py tests/data/test_download_ema.py` → pass; the URL guard prints nothing.
- [ ] **Step 5: Commit:** `feat(reg): pluggable feed config, parsers, EMA download`.

### Task 3: `reg_record` loader and normalisers

**Files:**
- Create: `src/episteme/data/regulatory/normalize.py`, `src/episteme/data/regulatory/load_reg.py`, `scripts/data/ema/load_ema.sh`, `scripts/data/openfda/load_openfda.sh`
- Modify: `scripts/data/run_pipeline.sh`, `src/episteme/ops/gate.py`
- Test: `tests/data/test_regulatory_normalize.py`, `tests/data/test_load_reg.py` (pg)

**Interfaces:**
- Consumes: Task 2 `Feed`, `parse`.
- Produces in `normalize.py`: `norm_name(s: str) -> str`, `split_names(s: str) -> list[str]` (splits on `|` and `;`), `norm_ndc(s: str) -> str | None` (11 digits, 5-4-2), `norm_application_number(s: str) -> str | None` (no `NDA`/`ANDA`/`BLA` prefix, keeps digits), `norm_lot(s: str) -> str | None`.
- Produces in `load_reg.py`: `load_release(conn, feed: Feed, path: Path, release_id: str, *, run_id: str) -> dict`, returning `{"rows", "duplicates", "status"}`. It creates the release partition, `COPY`s, builds the indexes (unique `record_key`, GIN `substance_names`, GIN `product_keys`, `event_date`), `ANALYZE`s, validates, and only then sets `is_latest`.
- Produces: `latest_release(conn, dataset: str) -> str | None`.

- [ ] **Step 1: Write the failing tests:**
  - `test_norm_ndc`: `"0093-7214-01"`, `"93-7214-01"` and `"00093721401"` all give `"00093721401"`; `"abc"` gives `None`.
  - `test_norm_application_number`: `"NDA021436"` gives `"021436"`.
  - `test_norm_name_keeps_salts_distinct`: `norm_name("Metformin HCl ")` is not `norm_name("Metformin")`.
  - `test_split_names_on_article57_separator`.
  - `test_record_key_hash_for_keyless_dataset_is_stable_and_drops_duplicates`.
  - pg `test_release_becomes_latest_only_after_validation`.
  - pg `test_short_feed_does_not_become_latest`: rows differ from `meta.total_records` → status `failed`, the previous latest stays, and the new partition is dropped.
  - pg `test_new_release_keeps_the_old_partition`.
  - pg `test_reload_of_same_release_is_idempotent`.
- [ ] **Step 2: Run them** and see them fail.
- [ ] **Step 3: Implement,** and wire `ema` and `openfda` `load` through `run_pipeline.sh` and `gate.py`.
- [ ] **Step 4: Run** the non-pg tests → pass. The controller runs the pg tests.
- [ ] **Step 5: Commit:** `feat(reg): release-partitioned reference loader and normalisers`.

### Task 4: Substance anchor and crosswalk

**Files:**
- Create: `src/episteme/data/regulatory/load_substance.py`, `scripts/data/openfda/load_substance.sh`
- Test: `tests/data/test_load_substance.py` (mixed)

**Interfaces:**
- Consumes: `normalize.norm_name`.
- Produces: `iter_substances(path: Path) -> Iterator[dict]`, streaming the zipped JSON with `ijson` (`results.item`).
- Produces: `moiety_of(record: dict) -> str | None`: the related UNII of the `ACTIVE MOIETY` relationship, else the record's own `unii`.
- Produces: `load_substances(conn, path: Path, *, run_id: str) -> dict`: upserts `substance` and `substance_xref` rows for `unii`, `name` (every name), and `rxcui`/`atc` where `codes` has them. Keys are never renumbered: an existing `(id_type, id_value)` keeps its key.
- Produces: `resolve_names(conn, names: Iterable[str], source: str) -> dict`, adding `normalised_name` xref rows for exact normalised matches and counting the rest. Returns `{"matched", "unmatched"}`.

- [ ] **Step 1: Write the failing tests** (fixtures built from the verified shapes):
  - `test_stream_does_not_load_the_whole_file`: `json.loads` patched to raise; a fixture zip of two records still parses.
  - `test_moiety_of_salt`: metformin hydrochloride `786Z46389E` gives `9100L32L2N`.
  - `test_moiety_of_itself_when_no_relationship`.
  - pg `test_synonyms_share_one_key`: `acetaminophen` and `paracetamol` on one record resolve to one `substance_key`.
  - pg `test_keys_are_stable_across_reloads`.
  - pg `test_combination_name_maps_to_two_substances`.
  - pg `test_unmatched_names_are_counted_not_guessed`.
- [ ] **Step 2: Run them** and see them fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the non-pg tests → pass. The controller runs the pg tests.
- [ ] **Step 5: Commit:** `feat(reg): FDA substance anchor, moieties and synonyms`.

### Task 5: FAERS parser and generation loader

**Files:**
- Create: `src/episteme/data/regulatory/faers/__init__.py`, `.../faers/parse.py`, `.../faers/load.py`, `scripts/data/openfda/load_faers.sh`
- Test: `tests/data/test_faers_parse.py`, `tests/data/test_faers_load.py` (pg)

**Interfaces:**
- Consumes: `normalize.norm_lot`.
- Produces in `parse.py`: `iter_reports(zip_path: Path) -> Iterator[dict]`, streaming, and `report_rows(report: dict) -> tuple[dict, list[dict], list[dict]]` → (report row, drug rows, reaction rows). `receive_year` comes from `receivedate`. Roles: `1` suspect, `2` concomitant, `3` interacting.
- Produces in `load.py`: `load_generation(conn, zips: Sequence[Path], *, raw_dir: Path, processed_dir: Path, run_id: str) -> dict`, following the spec's five steps: new generation tables with no indexes; `COPY` per zip with a marker per zip (keyed on `input_key`); de-duplication (highest version, latest `receiptdate`, latest file; `(safetyreportid, source_file)` delete; counted); indexes per partition plus `ANALYZE`; swap in one transaction, then drop the old generation. Dictionaries `faers_drug_entry` and `faers_term` are filled during `COPY`, in memory bounded by distinct values, with a flush per zip.

- [ ] **Step 1: Write the failing tests** (fixtures shaped like the 2004q2, 2012q2 and 2026q2 samples):
  - `test_old_era_missing_serious_flag_means_no`.
  - `test_new_era_flags_are_read_as_given`.
  - `test_report_without_date_goes_to_default_year`: `receive_year` is `None`, and it is counted.
  - `test_missing_active_substance_falls_back_to_annotation_then_product_name`.
  - `test_multi_drug_report_gives_one_row_per_drug_with_role`.
  - `test_batch_number_kept_raw_and_normalised`.
  - `test_reports_stream_from_the_zip_without_a_whole_file_list`.
  - pg `test_generation_swap_is_atomic`: readers see the old generation until the swap.
  - pg `test_duplicate_id_keeps_highest_version_then_latest_receipt`.
  - pg `test_report_absent_from_new_issue_disappears`.
  - pg `test_resume_skips_completed_zips`.
  - pg `test_indexes_exist_only_after_the_load_step`.
- [ ] **Step 2: Run them** and see them fail.
- [ ] **Step 3: Implement,** and wire `openfda_faers` into `run_pipeline.sh` and `gate.py`.
- [ ] **Step 4: Run** the non-pg tests → pass. The controller runs the pg tests.
- [ ] **Step 5: Commit:** `feat(faers): streaming parser and generation-swap loader`.

### Task 6: Signals and lot matching

**Files:**
- Create: `src/episteme/data/regulatory/faers/signals.py`, `src/episteme/data/regulatory/recall_lots.py`
- Test: `tests/data/test_recall_lots.py`, `tests/data/test_faers_signals.py` (pg)

**Interfaces:**
- Consumes: Task 3 `norm_lot`, `latest_release`; Task 4 keys; Task 5 tables.
- Produces: `bridge_entries(conn) -> dict`, filling `faers_entry_substance` from `faers_drug_entry` (annotation UNII first with `method='annotation'`, then `active_substance`, then product name, by exact normalised match).
- Produces: `rebuild_signals(conn, *, pair_drug_cap: int) -> dict`, building the five signal tables into new tables and swapping them with the generation. `pair_drug_cap` comes from config (`EPISTEME_FAERS_PAIR_DRUG_CAP`, default 30). Reports over the cap are left out of the pair counts and counted.
- Produces: `extract_lots(code_info: str) -> list[str]`, and `build_recall_lots(conn, release_id: str) -> dict`, returning `{"recalls", "with_lot", "no_lot", "lots"}`.
- Produces: `lot_matches(conn, moiety_key: int) -> list[dict]`, matching on the same moiety and the same `lot_norm`, with coverage.

- [ ] **Step 1: Write the failing tests:**
  - `test_extract_lots_from_lot_prefix`: `"Lot #: AB123, Exp 12/25"` gives `["AB123"]`.
  - `test_extract_lots_several`.
  - `test_expiry_and_ndc_are_not_lots`.
  - `test_explicit_range_expands_only_with_both_ends`.
  - `test_recall_without_lot_is_counted`: `extract_lots("All lots")` gives `[]`, and `no_lot` is counted.
  - pg `test_pairs_are_stored_in_both_directions`.
  - pg `test_salt_and_base_count_as_one_moiety`.
  - pg `test_suspect_and_any_role_counts_differ_as_expected`.
  - pg `test_report_over_drug_cap_is_left_out_and_counted`.
  - pg `test_batch_coverage_counts_entries_with_a_batch`.
  - pg `test_lot_match_needs_same_moiety_and_same_lot`.
- [ ] **Step 2: Run them** and see them fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the non-pg tests → pass. The controller runs the pg tests.
- [ ] **Step 5: Commit:** `feat(faers): moiety signal tables and recalled-lot matching`.

### Task 7: DailyMed extractor

**Files:**
- Create: `src/episteme/data/dailymed/__init__.py`, `.../dailymed/spl.py`, `.../dailymed/extract_dailymed.py`, `scripts/data/dailymed/extract_dailymed.sh`, `scripts/data/dailymed/load_dailymed.sh`
- Modify: `scripts/data/run_pipeline.sh` (`LIT_SOURCES`), `src/episteme/ops/gate.py`, `src/episteme/data/load_articles.py` (retire and label tables, through `parts_retire`)
- Test: `tests/data/test_dailymed_spl.py`, `tests/data/test_extract_dailymed.py`

**Interfaces:**
- Produces in `spl.py`: `parse_spl(xml: bytes) -> dict`, with keys `set_id`, `version`, `doc_type`, `title`, `sections: list[dict(loinc, title, text)]`, `uniis: list[str]`, `ndcs: list[str]`. It uses `defusedxml`.
- Produces in `extract_dailymed.py`: `iter_labels(raw_dir: Path, release_kinds: tuple[str, ...]) -> Iterator[tuple[str, bytes]]`, streaming zips of zips; the newest version per `setId` wins; bucket by the first two characters of `setId`; parts capped by `part_rows`/`part_bytes`, reusing the PMC pattern (manifest, fingerprint, retire through `parts_retire` with `unit=<bucket key>`).
- Row: `source='dailymed'`, `id='dailymed:<setId>'`, `source_record_id=<setId>.<version>`, `body_text` = sections joined, and `extra` sidecar shard columns for `label_section` offsets and `label_substance`, loaded by the load stage.

- [ ] **Step 1: Write the failing tests:**
  - `test_parse_spl_reads_set_id_version_codes`: fixture from the real layout; UNII OID `2.16.840.1.113883.4.9`, NDC OID `2.16.840.1.113883.6.69`.
  - `test_sections_keep_loinc_and_text`.
  - `test_section_offsets_point_into_body_text`: `body_text[start:end]` equals the section text.
  - `test_newest_version_per_set_id_wins`.
  - `test_corrupt_inner_zip_fails_one_label_only`.
  - `test_images_in_inner_zip_are_skipped`.
  - `test_identity_collision`: two different inner paths with one `setId` keep one row, and count a duplicate.
- [ ] **Step 2: Run them** and see them fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -m "not pg" -q tests/data/test_dailymed_spl.py tests/data/test_extract_dailymed.py` → pass.
- [ ] **Step 5: Commit:** `feat(dailymed): SPL extractor in bounded parts with sections and substances`.

### Task 8: EPAR text

**Files:**
- Create: `src/episteme/data/ema/__init__.py`, `.../ema/extract_epar.py`, `.../ema/download_epar.py`, `scripts/data/ema/extract_ema_epar.sh`, `scripts/data/ema/load_ema_epar.sh`
- Modify: `scripts/data/run_pipeline.sh`, `src/episteme/ops/gate.py`, `pyproject.toml`, `uv.lock` (add `pypdf`, BSD-licensed, unless the first sample fails on it; the plan's comparison note goes in the drift log)
- Test: `tests/data/test_epar.py`

**Interfaces:**
- Consumes: Task 2 `FeedVerificationError`; the `ema_epar_documents` feed.
- Produces: `select_documents(rows: Iterable[dict], types: tuple[str, ...]) -> Iterator[dict]`, with types from `feeds.toml`: `product-information`, `assessment-report`, `rmp-summary`.
- Produces: `fetch_documents(raw_dir: Path, docs: Iterable[dict], *, delay_s: float) -> dict`: English only, one at a time; a non-PDF response raises `FeedVerificationError`.
- Produces: `pdf_text(path: Path) -> str`; `extract_epar(raw_dir, processed_dir, *, workers, force) -> dict`: buckets of 500 documents written in parts. Row: `source='ema_epar'`, `id='ema_epar:<document id>'`, with `ema_product_number` in `source_record_id`.

- [ ] **Step 1: Write the failing tests:**
  - `test_select_documents_keeps_only_listed_types_and_english_urls`.
  - `test_verification_page_stops_the_download`.
  - `test_download_resumes_past_existing_files`.
  - `test_pdf_with_no_text_is_failed_and_counted`: a fixture image-only PDF.
  - `test_pdf_text_from_fixture`.
  - `test_document_removed_from_feed_is_retired`.
- [ ] **Step 2: Run them** and see them fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -m "not pg" -q tests/data/test_epar.py` → pass.
- [ ] **Step 5: Commit:** `feat(ema): EPAR PDF download and text extraction`.

### Task 9: Gates, efficiency checks and docs

**Files:**
- Create: `src/episteme/ops/gate_rules/dailymed.json`, `ema_epar.json`, `ema.json`, `openfda.json`, `openfda_faers.json`, `scripts/data/regulatory/check_plans.py`
- Modify: `src/episteme/ops/gate_slice.py`, `src/episteme/ops/gate_report.py`, `docs/02-data-sources.md`, `docs/10-data-sources-runbook.md`, `docs/12-source-inventory.md`, `docs/project-incubation-baseline.md`
- Test: `tests/ops/test_gate_slice_regulatory.py`, `tests/ops/test_gate_rules_sources.py`

**Interfaces:**
- Consumes: all earlier tasks.
- Produces: slice selectors per spec section 7: FAERS (oldest quarter, 2012q3 or nearest, the largest file of the newest quarter); DailyMed (largest label in the Rx release, a label with many products, one OTC label); EMA (all feeds); EPAR (largest PDFs by size).
- Produces: gate measures: `meta_total_equals_rows`, `xref_coverage_pct` per dataset, `max_peak_rss_mb`. Thresholds start provisional; they are set on the first sample.
- Produces: `check_plans.py`, printing `EXPLAIN` for the six queries in spec section 8, and failing on a plan that reads more than the expected partitions.

- [ ] **Step 1: Write the failing tests:**
  - `test_faers_slice_has_three_eras`.
  - `test_dailymed_slice_has_the_largest_label`.
  - `test_regulatory_sources_are_gated_and_rules_use_known_keys`.
  - `test_meta_total_rule_fails_on_a_mismatch`.
  - `test_xref_coverage_rule`.
- [ ] **Step 2: Run them** and see them fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Docs:** `docs/02` catalogue entries for all new sources, with the licence posture and the EMA credit; runbook sections for each stage, including the EMA verification stop and the manual FAERS refresh; inventory rows; a drift-log entry with the rulings and deferred items.
- [ ] **Step 5: Run** the whole `uv run pytest -m "not pg" -q` once → pass; the URL guard prints nothing.
- [ ] **Step 6: Commit:** `feat(gate): regulatory slices, rules, plan checks; docs`.
