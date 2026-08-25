# Project Incubation Baseline

**Project:** Episteme Medical LLM
**Baseline created:** 2026-08-25
**Last audited:** never
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

- **Chosen license:** Apache-2.0
- **Reasoning:** Commercial use allowed, compatible with open-source medical LLM requirements.

## Drift log

- 2026-08-25: Baseline established at inception.
