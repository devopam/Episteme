# Project Incubation Baseline

**Project:** Episteme Medical LLM
**Baseline created:** 2026-08-25
**Last audited:** 2026-09-01
**project-incubation skill version:** v1

## Project shape

- **Path:** software
- **Purpose:** Build a generic open medical LLM trained on stable foundational knowledge, keeping proprietary and highly dynamic details behind a RAG layer.
- **Team size at incubation:** small team
- **Expected scale / lifespan:** production, long-lived
- **Compliance / regulatory constraints:** HIPAA / healthcare compliance aware (strictly avoiding patient clinical notes)

## Stack category (software path only)

- **Primary category:** Data & Analytics Platforms
- **Reasoning:** Requires complex multi-stage data gathering, processing, serialization of biochemical/genomic data, and model training pipelines.

## Architecture template (software path only)

- **Primary pattern:** modular monolith
- **Overlays / composed elements:** none
- **Reasoning:** Small team, production prototype, modular python package structure.
- **ADR:** none

## Common architecture principles applied

- **Principles doc version referenced:** 2026-08-19
- **LLM/agent component:** yes
  - **Basis:** asked directly
  - **If yes:** the conditional LLM-specific principles section (harness, model-switching, provenance, relevance+confidence) applies.
- **Notable deviations from the standard principle set:** no SLO stated yet, prototype stage.

## Preferred libraries snapshot (software path only)

- **Category reference used:** references/preferred-libraries/data-analytics-platforms.md
- **Snapshot date at incubation:** 2026-08-19
- **Key library choices:** pandas, polars, pyarrow, datasets, pytorch, transformers, accelerate, peft, trl, deepspeed.

## License

- **Chosen license:** MIT
- **Reasoning:** MIT chosen for maximum permissiveness and simplicity; matches the existing LICENSE file.
- **2026-09-01 audit note:** the root `LICENSE` file currently contains the **MIT** license text (`Copyright (c) 2026 Devopam Mittra`), contradicting `pyproject.toml`, `README.md`, and this baseline, all of which say Apache-2.0. Must be reconciled — for a project whose stated non-negotiable is the commercial-use licence boundary, an ambiguous own-licence is blocker-class. Resolved 2026-09-01: standardised on MIT.

## Drift log

- 2026-08-25: Baseline established at inception.
- 2026-09-01: First audit (Audit mode).
  - **LLM/agent status:** unchanged — still `yes` (inferred from repo: `src/episteme/model_pipeline/` trains CPT/SFT/DPO; no direct question asked this pass).
  - **Preferred-libraries staleness:** snapshot date 2026-08-19 is ~2 weeks old, well inside the 6-month re-check threshold — no flags.
  - **Structure gaps vs. `references/project-structure.md`:** missing `CONTRIBUTING.md`, `CHANGELOG.md`, `.gitattributes`, `.editorconfig`, `SECURITY.md`, `CODEOWNERS`, and any CI workflow (`.github/workflows/`). `.gitignore` is hand-rolled. src-layout is correct; `data_pipeline/` and `model_pipeline/` lack `__init__.py` (a clean non-editable `pip install` still works via setuptools auto-discovery, so non-blocking, but non-idiomatic); `src/episteme.egg-info/SOURCES.txt` is stale (omits `apollo/extract.py`).
  - **Principles gaps vs. `references/architecture-principles.md`:** (2) config — pipeline paths are argparse defaults, no `.env.example`, no secret handling for the (planned) NCBI API key; (6) observability — bare `print()` throughout, no structured logging; (5) still no SLO (as noted at inception). LLM-conditional principles (harness / provenance / relevance) are documented as deferred to Phase 1 in `docs/07` — consistent with baseline, not flagged.
  - **Docs-vs-code drift:** `docs/07` and `docs/08` are marked *binding* (Parquet→Iceberg, `00_meta/…99_tmp/` layout, streaming `lxml.iterparse` PubMed→Parquet producing `articles`/`citations`/`mesh_assignments`, OpenMetadata catalog). None of this is implemented yet — `preprocess.py` buffers the whole corpus in memory and writes flat JSONL via `xml.etree`, extracts no MeSH or citation edges, and no `02_processed/` or `03_corpus/` tree exists.
  - **Correctness bug (cross-cutting):** every `--dry_run` / `--sample_only` flag is declared `action="store_true", default=True`, so it cannot be turned off from the CLI and there is no `--no-*` form. All README "real run" commands therefore still execute in mock mode (random-weight models, 30-string datasets, no-op decontamination). Fix before Phase 0 training work begins.
  - No fixes applied this pass — audit is report-only per skill Step 5; user to decide what to action.
- 2026-09-01: Phase-1 restructure (spec `docs/superpowers/specs/2026-09-01-data-taxonomy-and-postgres-restructure-design.md`).
  `src/episteme/{data_pipeline,model_pipeline}/` removed; subject-area taxonomy under
  `data/<source>/` and `model/` with spelled-out `<stage>_<subjectarea>` module names.
  Environment config centralised in `.env` + `episteme.config`. License standardised on MIT.
  Storage decision reversed (Iceberg/OpenMetadata-filesystem -> Postgres hybrid + Parquet
  corpus) — implemented in Plan 2; ADR-0001/0002 to be authored there.
- 2026-09-03: SP1-α verification sweep (plan `docs/superpowers/plans/2026-09-02-sp1a-renames.md`, spec `docs/superpowers/specs/2026-09-02-sp1-storage-core.md`).
  Tasks 1–5 landing: config EPISTEME_DATA_ROOT + Settings.{raw,processed,warehouse}_root added; module renames (schema→article_schema, ops→checkpoint_markers, writer→staging_writer) with pre-branch history verified; extract_pmc.py merged from upstream PMC extractor logic with importable core + guarded audit. All 36 tests pass; import sweep clean; no stale module references; ruff clean on modified source files.
- 2026-09-04/05: SP1-β storage core landed (plan `docs/superpowers/plans/2026-09-03-sp1b-storage-core.md`, spec `docs/superpowers/specs/2026-09-02-sp1-storage-core.md` v1.1) — branch `sp1b-storage-core`, 12 tasks, subagent-driven with controller review + fix-waves on every task.
  - **Postgres 19beta3** (`localhost:5433`) live: `episteme` + `episteme_test` DBs, 3-tier roles (`postgres` bootstrap-only / `episteme_sys_admin` DDL / `episteme_app` runtime, `_audit` INSERT+SELECT-only — verified), partitioned schema (LIST/RANGE `articles`, LIST `article_body` zstd→lz4 TOAST fallback, HASH×8 `article_cites`/`article_mesh`/`chunks`, RANGE monthly `_audit`), `init_database.sh` idempotent. `pgvector`/`pg_search` absent on this build → soft-blocked to `migrations/0001`; SQL/PGQ `CREATE PROPERTY GRAPH` present but the schema-qualified `REFERENCES` form fails to parse → `graph_builder` runs its recursive-CTE path (PGQ path is code-complete, untested — no graph exists to test it against).
  - Real hash-chained `audit_trail.record`/`verify` (SHA-256, JSONL mirror) + `mirror_only` (no-DB fallback for extract's no-txn context) + a `python -m episteme.audit_trail` CLI shim; `postgres_loader` (idempotent per-`source_file` DELETE+COPY, fixed mid-plan: article_body delete must follow the `articles.id` linkage, not just the incoming shard's ids, or a shrinking re-load orphans rows); `graph_builder`; `corpus_materializer` (DuckDB-streamed Postgres→Parquet, MinHash-LSH dedup + 13-gram decontam — **decontam's mock question list needs `sample_only=False` + real HF eval sets before it filters anything meaningful in production**); `openmetadata_manifest`/`enrich_openmetadata`/`sample_audit` (field-shape reporting); the full `scripts/data/pmc/*.sh` + `run_pipeline.sh` orchestration layer.
  - **Proving slice** (`run_pipeline.sh pmc all --max-files 2`) ran end-to-end on the real on-disk PMC sample; all 8 spec §7 exit criteria pass on a clean DB reset. The field-shape report caught exactly the bug the spec's §8a review had predicted (`authors` 100% null — `extract_pmc.py` was reading JATS `<contrib>`'s direct children instead of drilling into `<contrib><name>`) — fixed, `SCHEMA_VERSION` 1.2→1.3. Also found (not spec-predicted) and fixed: the audit trail's `run_id` was fragmenting one pipeline run across 5 independently-generated ids (each CLI module built its own before `run_pipeline.sh`'s orchestrator existed) — threaded a shared `EPISTEME_RUN_ID` through `config.Settings` instead.
  - Mid-plan detour: wired the `MCPg` Postgres MCP server to `episteme`/`episteme_test` (two server entries — MCPg only routes writes to a server's *primary*, so `episteme_test` needed its own `mcpg-test` entry to be writable) and rebuilt `.pre-commit-config.yaml` on MCPg's local-hook (`uv run --no-sync`) pattern — ruff+bandit(+baseline) actually enforced now (they never were before this: the repo had a `.pre-commit-config.yaml` for a full development phase with the hooks never `install`ed). mypy deliberately deferred until the pipeline is functionally complete.
  - **Not carried forward / open for later SPs:** `publication_types`/`license_url` extraction (real JATS data looks available, not attempted); decontamination needs the real HF eval sets downloaded; `pg_search`/`pgvector` need a build where the extensions actually install (migration 0001 is queued and ready); `download_pmc.py`'s ESearch has no stable ordering across invocations (fine for its purpose, worth a runbook note for anyone wanting a byte-reproducible re-run); ad-hoc verification of DB-writing code must target `episteme_test`, never the real `episteme` DB (a dev-testing session left 8 orphaned `_audit` rows in `episteme` before the proving slice's DB reset — caught by the mirror-parity check exactly as designed, not a defect in the mechanism).
  Full suite: 46 passed, 1 skipped. Non-pg: 40. Ledger: `.superpowers/sdd/2026-09-03-sp1b-storage-core/progress.md` (workspace, not committed).
