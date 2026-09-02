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
