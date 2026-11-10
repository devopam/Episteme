# GraphRAG design: SFT model and deterministic reference lookup (as built)

Status: as built on branch `docs/sft-reference-design`, 2026-11-10. Design source: `superpowers/specs/2026-11-09-sft-and-reference-lookup-design.md` (the spec). Plan: `superpowers/plans/2026-11-09-sft-and-reference-lookup.md`. This document replaces the uploaded draft of `13-graphrag-design.md`. Where the code differs from the spec, this document states the code and marks the difference with **As built**.

Operator steps: `10-data-sources-runbook.md` section 5.17 (reference documents) and section 6.4 (SFT data and training handoff). Licence classes: `02-data-sources.md` sections 2.2.1, 2.7 and 2.8. Open and deferred items: the drift log in `project-incubation-baseline.md` (entry 2026-11-10).

## 1. Two parts and one rule

Episteme builds two parts:

1. **An SFT model as a GGUF.** A small open-weight model (1 to 4B) that writes medical text in the expected structure and style, calls tools, and abstains when it cannot settle a claim. Training runs on rented GPU hardware. This repo builds the training data and the evaluation, not the training run.
2. **A deterministic reference lookup.** Read-only functions that return exact, versioned facts and rules from the loaded public sources. Each fact carries its provenance.

**The route follows the type of claim.** A claim goes to the lookup when it names an entity that resolves (substance, product, trial or another keyed record) and asks for a field that the reference store holds (approval status, label section, safety action, trial status, FAERS count). All other claims stay with the model: mechanisms, clinical reasoning, terminology, structure and style. The model never overrides a lookup value. A claim that neither side can settle is returned as `unverified`. No confidence threshold for this class exists yet; it is set later from benchmark measurement.

Exact values (dates, codes, counts, identifiers, citations) come from the lookup, never from the model's weights. DailyMed labels, EPAR text and AACT stay out of the training corpus (`corpus_excluded_sources`); for versioned facts the lookup is the only authority.

## 2. Claim records

Every lookup returns a `LookupResult` (`src/episteme/reference/claims.py`):

| Part | Content |
|---|---|
| `Claim` | `text`, `value`, `method` (`fetched`, `computed`, `mapped` or `inferred`), `provenance`, `confidence` in [0, 1] with `confidence_basis`, `alternatives` (only a `mapped` claim may have them) |
| `Provenance` | `source`, `release`, `source_file`, `record_key`, `jurisdiction` |
| `Coverage` | `matched` (count), `missing` (datasets with no data), `notes` (fixed texts such as `change history not determinable`) |

`LookupResult.to_json()` and `LookupResult.from_json()` give the form that the harness sends to the model and that Stage B examples store.

## 3. Reference store as built

The lookup reuses the tables of migration 0010 and the loaders. It adds no parallel tables.

| Need | Structure |
|---|---|
| Entity resolution | `substance`, `substance_xref` (UNII, synonyms, active moiety) |
| Releases | `reg_release`; a lookup reads only the `is_latest` release of a dataset |
| FDA and EMA records | `reg_record`, one partition per dataset and release |
| Labels | `label_section`, `label_substance` (DailyMed), `articles` |
| Trials | the `aact` schema (views over the latest release) |
| Adverse events | `faers_*` tables and the per-quarter signal tables |
| Literature | `article_mesh`, `articles` |

### 3.1 Lookup functions

All functions are read-only (SELECT only) and take keyword arguments `tenant` and `caller`. Version 1 does not use the two arguments; they keep the signatures stable. A missing relation, or a dataset with no `is_latest` release, is named in `coverage.missing`; the other datasets still answer. No function falls back to an older release.

**Substance scope.** Every PBRER lookup covers the whole moiety family of the input: the moiety and every salt or ester whose moiety it is (`src/episteme/reference/_scope.py`). So all sections of one draft cover the same substances.

| Function | Module | Reads | Notes |
|---|---|---|---|
| `resolve_entity` | `reference/resolve.py` | `substance`, `substance_xref` | Exact UNII or exact normalised name gives `fetched`, confidence 1.0. A name shared by n substances gives `mapped`, confidence 1/n, the others as alternatives. No match gives no claim and `missing = ("substance",)` |
| `approval_status` | `reference/lookups.py` | `fda_drugsfda`, `ema_medicines`, `ema_article57`, `ema_outside_eu` | Matches the family's names on `reg_record.substance_names` |
| `safety_actions` | `reference/lookups.py` | `fda_enforcement`, `ema_referrals`, `ema_dhpc`, `ema_psusa` | Filtered on `event_date` in the interval |
| `current_label` | `reference/lookups.py` | DailyMed (`label_section`, `label_substance`) | Claim value is the section title; record key `<article_id>#<loinc_code>`. Coverage note `change history not determinable`: the DailyMed extract keeps only the newest SPL version per `setId` |
| `trials` | `reference/lookups.py` | `aact.intervention_substance`, `aact.studies` | Status, phase and results flag. **As built:** not filtered to the reporting interval |
| `faers_counts` | `reference/lookups.py` | FAERS per-quarter tables | Method `computed`. Every claim text ends with `(US public spontaneous reports (FAERS))`. Quarters that overlap the interval are counted whole |
| `literature` | `reference/literature.py` | `article_mesh`, `articles` | See section 6. **As built:** not filtered to the reporting interval |

`PBRER_DATASETS` in `lookups.py` names the datasets of each function; the coverage gaps use these names.

**As built.** The tools run in-process through `episteme.harness.loop.make_call_tool` over one psycopg connection. They are not yet exposed through MCPg (programme project 5).

## 4. Versioned rule store

Rules from implementation guides and guidelines are held as packaged JSON, one file per IG version, under `src/episteme/reference/rules/data/`. Each file names its IG, version and source documents with their sha256. Every item keeps the clause of its source. Rule data is read-only after load. `load_rules(ig_id)` loads a file.

| Rule file | Source | Content |
|---|---|---|
| `ich_e2c_r2.json` | `E2C_R2_Guideline.pdf`, the E2C(R2) Q&A, and EU GVP Module VII (Rev 1) | The PBRER sections from the recommended table of contents (E2C(R2) 2.9.2): title page, executive summary, table of contents, sections 1 to 20, appendices A1 to A5, and the EU regional appendix A6 with A6.1 to A6.5 (`region: "eu"`). Each section has `required`, `mah_data` and `clause` |
| `ich_e2b_r3.json` | ICH E2B(R3) IG package v1.11: core data elements and business rules v1.00 (sheet `ICSR`), code lists v2.11, the reference instance | 223 data elements, 27 code lists, the conditional rules that the sheet states in a checkable form, and 71 rules listed under `manual` (not checked). Generated by `python -m episteme.reference.rules.build_e2b`; not edited by hand |

Rules are encoded in our own words: section titles, clause numbers, data element ids and rule conditions. No ICH text is copied. The ICH licence is confirmed by the owner (2026-11-09): use freely, with ICH credit (`02-data-sources.md` section 2.7.1).

Checks:

- `check_sections(rules, headings, region=...)`: missing required section, wrong order (headings not in the longest ordered run of rule positions), duplicate section. Heading match is case-insensitive and punctuation-insensitive; the leading section number is optional.
- `check_mah_markers(rules, sections, strict=...)`: every `mah_data` section must hold the marker `MAH data required`. With `strict=True` (the PBRER gate), the marker must be the whole body.
- `check_icsr(xml_bytes, region=...)`: section 8.

**`mah_data` sections.** 5, 5.1, 5.2, 6, 6.1, 6.2, 6.3, 12, A1, A2, and in the EU appendix A6.3 and A6.4. Sections 6.1, 12, A6.3 and A6.4 go beyond the three groups of the spec (exposure, own case tabulations, CCSI). The source text supports each one; the owner agreed to keep them (2026-11-10).

## 5. Minimal harness

The full agent harness is programme project 6. PBRER v1 needs a minimal one (`src/episteme/harness/`):

- **Model server.** Any server with an OpenAI-compatible chat API and tool calls, for example the llama.cpp server with the GGUF. The URL is `EPISTEME_HARNESS_BASE_URL`.
- **Tool schema.** `TOOLS` in `harness/loop.py`: OpenAI-style function schemas for `resolve_entity` and the six lookups. `tenant` and `caller` are not in the schema: the loop adds them to each call and drops any value the model sends for them. Stage B tool examples use this same schema; their batch manifest records its hash (`TOOLS_SHA256`).
- **Loop.** `run(messages, call_tool=..., post=..., max_tool_calls=..., tenant=..., caller=...)` chats until the model answers. Each tool call counts toward `max_tool_calls` (`EPISTEME_HARNESS_MAX_TOOL_CALLS`). When the budget is spent, the loop asks once more with no tools. An argument error (`ToolArgumentError`) or a database error goes back to the model as a fixed text that names the tool; any other error stops the run.
- **Post-answer check.** `check_answer(answer, results)` in `harness/check.py` returns every date (`YYYY-MM-DD`, `DD Month YYYY`), NDC, UNII, NCT id, PMID, EMA PSUSA procedure number and integer count above 9 in the answer that no claim value, claim text, record key or release id states.
- **Router.** `route_claim(claim, resolve)` returns `("lookup", <function>)` when the claim names an entity that `resolve` matches and holds a trigger term of `FIELD_TERMS`; else `("model", None)`. It makes no model call.

## 6. Literature retrieval without vectors

Exact lookup needs no vectors. PostgreSQL 19beta3 on the build machine has neither pgvector nor pg_search.

**As built.** `literature(conn, substance_key, topic, ...)` matches a PubMed record when one of its MeSH descriptor names (`article_mesh`) equals a preferred name or synonym of a member of the substance family. `topic`, when given, must also be a descriptor or qualifier name of the same record (for example `adverse effects`). Results are newest first, with record key `pmid:<pmid>`. Full-text search, expansion over `mesh_hierarchy` and citation edges, as the spec describes, are not built yet.

pgvector comes later, when a server build with the extension exists (for example on the NVMe host). It is then used only to resolve free text to a concept, as the programme decides.

## 7. PBRER v1

- **Input:** a substance (the `resolve_entity` result) and a reporting interval.
- **Facts:** `assemble_facts(lookup, substance, start, end, tenant=..., caller=...)` runs the lookups of `SECTION_SOURCES` and keeps one merged result per section. With no resolved substance, no lookup runs and every source section states the gap.
- **Skeleton:** `skeleton(facts, region=...)` gives every E2C(R2) section in rule order. A source section holds claim lines with provenance, or its coverage gaps (`Coverage gap: ...`, `Coverage note: ...`). A `mah_data` section holds `MAH data required` and nothing else. A prose section holds the placeholder `[skeleton] to be drafted from the facts in the sections above.`; a section with no mapped source holds `[skeleton] no public source is mapped to this section in v1.`
- **Gate:** `gate(draft, region=...)` runs `check_sections` and `check_mah_markers` (strict). A draft that fails is not shown as a draft; the findings are shown instead. `gate_parts` gives the two parts apart.

| PBRER section | Source (as built) | Route |
|---|---|---|
| 2 Worldwide marketing approval status | `approval_status` | Lookup |
| 3 Actions taken for safety reasons | `safety_actions` | Lookup |
| 4 Changes to reference safety information | `current_label`, with the history gap | Lookup and gap |
| 7 Clinical trial findings | `trials` (all sponsors; not interval-filtered; a fixed coverage note says so) | Lookup |
| 9 Other clinical trials and sources | `faers_counts`, under the label `US public spontaneous reports (FAERS)` | Lookup, labelled public |
| 11 Literature | `literature` (not interval-filtered; a fixed coverage note says so) | Retrieval with citation |
| 5, 6, 12, A1, A2 and subsections; EU A6.3, A6.4 | none public | `MAH data required` |
| 15 to 19 and subsections | the facts above | Model prose |

**As built.** The gate does not reject `[skeleton]` placeholders in v1: a section not drafted is not an invented value. `unfilled_sections(draft)` lists them.

## 8. E2B(R3) ICSR scope

`check_icsr(xml_bytes, region="ich")` in `reference/rules/icsr.py` checks one ICSR batch against `ich_e2b_r3.json`:

- **In scope:** mandatory data elements; the conditional rules that the ICH sheet states in a checkable form; code lists; lengths and formats; date precision; batch and message header consistency; MedDRA code shape (8 digits) and a present MedDRA version. Malformed XML gives one `parse` finding, not an exception. XML with an external entity is refused.
- **Out of scope:** a full ICSR tool, gateway submission, acknowledgements, certification against FDA or EMA validators, and any MedDRA term lookup (MedDRA is licensed; Episteme holds no licence).
- **As built.** There is no XSD step. The example and reference instances of the IG package are the acceptance set. `region="fda"` and `region="eu"` return one finding each (`FDA regional rules not loaded`, `EU regional rules not loaded`).

## 9. SFT data path

Section 6.4 of `10-data-sources-runbook.md` has the operator steps. In short:

- **Base model.** 1 to 4B, Apache 2.0 or equivalent (`02-data-sources.md` section 2.8). The same evaluation runs on two sizes; size is chosen from measurement.
- **Stage A (continued pre-training).** `build_slice` in `src/episteme/sft/slice.py` samples the materialised corpus shards by weight (`EPISTEME_SFT_MIX_WEIGHTS`; empty means proportional). It refuses shards of `corpus_excluded_sources` and any row whose `subset` is not `commercial`. Tokens are whitespace tokens; the real tokenizer runs on the GPU host.
- **Stage B (instruction examples).** `generate_batch` in `src/episteme/sft/batch.py` writes four kinds: `grounded_qa`, `tool_draft`, `rule_check`, `abstain` (reason `MAH data required` or `unverified`). The teacher writes only the text around the grounding that the input row supplies. Each example passes deterministic checks before it is kept (`src/episteme/sft/checks.py`): format, citation quotes the passage, exact values, rule-store structure. A failure is counted by reason in the batch manifest and is not trained on.
- **Licence gate per row.** `grounded_qa` and `abstain` need `subset = commercial` and a source outside `corpus_excluded_sources`. `tool_draft` takes the licence from the data: every source named in the provenance of the tool results must be `commercial` in `config.LOOKUP_SOURCE_SUBSETS` (`02-data-sources.md` section 2.7.2). `rule_check` rows carry `source = pbrer_skeleton`, `subset = commercial`.
- **Teacher.** An OpenAI-compatible API (`TEACHER_BASE_URL`), DeepSeek by default. A hard token cap (`EPISTEME_TEACHER_BUDGET_TOKENS`) is checked before every request. The API key (`EPISTEME_TEACHER_API_KEY`) never reaches logs, `repr` or exception text. Only public data is sent.
- **Restart.** `requests.jsonl` is an append-only ledger of paid requests. A re-run skips rows that were kept or paid for and never truncates `examples.jsonl`.
- **Manifests.** Every slice, batch and training round writes a manifest (`src/episteme/sft/manifest.py`) with inputs, counts, teacher, budget used and a `config_hash`.

## 10. Evaluation

- **Cases.** `build_cases` in `src/episteme/bench/pbrer_cases.py` builds one case per newest `ema_psusa` record whose substance resolves to exactly one substance. Interval: the 12 months before `event_date`. Expected facts and gaps come from the lookups at build time. Case id: `psusa-` plus the first 12 hex digits of the SHA-256 of the record key. A case counts only after the owner sets `reviewed_by_owner: true`.
- **Runner.** `python -m episteme.bench.runner --cases benchmarks/pbrer --out <dir>` scores the reviewed cases and writes `scores.json`: `sections_ok`, `mah_markers_ok`, `invented_values`, `facts_found`, `gaps_reported`.
- **As built.** The runner's command line scores the deterministic skeleton (no model call). This is the baseline that a model draft has to beat. A producer that drafts with the GGUF through the harness loop is not on the command line yet; code can pass one to `runner.main(produce=...)`.
- **Release gate (spec section 10).** A model ships only if it beats the base model with the same tools on these measures and has zero invented exact values.

## 11. Configuration

All keys are read in `src/episteme/config.py`. Endpoint URLs live in `scripts/data/_lib/sources.env`.

| Area | Keys |
|---|---|
| Teacher | `TEACHER_BASE_URL` (sources.env), `EPISTEME_TEACHER_API_KEY` (secret, in `.env`), `EPISTEME_TEACHER_MODEL`, `EPISTEME_TEACHER_BUDGET_TOKENS`, `EPISTEME_TEACHER_MAX_TOKENS`, `EPISTEME_TEACHER_REQUEST_TIMEOUT_S` |
| Stage A mix | `EPISTEME_SFT_MIX_WEIGHTS` (`source=weight,...`) |
| Harness | `EPISTEME_HARNESS_BASE_URL`, `EPISTEME_HARNESS_MAX_TOOL_CALLS`, `EPISTEME_HARNESS_MAX_CANDIDATES`, `EPISTEME_HARNESS_REQUEST_TIMEOUT_S` |
| Benchmark | `EPISTEME_BENCH_TENANT`, `EPISTEME_BENCH_CALLER` |
| Reference documents | `EPISTEME_EMA_DOC_MIN_INTERVAL_S`, `EPISTEME_REFERENCE_DOCS_MIN_INTERVAL_S`, `EPISTEME_REFERENCE_DOCS_ICH_PAGES`, `EPISTEME_REFERENCE_DOCS_API_PAUSE_S`, `EPISTEME_REFERENCE_DOCS_USER_AGENT`; sources.env: `ICH_API_BASE`, `ICH_FILES_BASE`, `MEDDRA_API_BASE`, `MEDDRA_PTC_TAXONOMY`, `EMA_BASE` |

Two policy constants have no environment key: `CORPUS_EXCLUDED_SOURCES` and `LOOKUP_SOURCE_SUBSETS`.

**As built.** The harness default URL is a code default (`DEFAULT_HARNESS_BASE_URL`, a loopback address); the teacher URL is in `sources.env`. The drift log records this split.

## 12. Later phases

- **Training rounds** on rented hardware: a small checked set, a round, a measurement, then the next batch. A round that scores worse never ships.
- **Full harness** (programme project 6): planner, step limits, MCPg tools.
- **pgvector** for entry-point resolution only, when a server build has the extension.
- **Label history store** (programme project 2), so section 4 can report changes, not only the current label.
- **Regional ICSR rules** (FDA, EU) and an optional XSD step.
- **Other document types** after PBRER, one at a time: CSR, PLPS, ICF, DSUR, IB, patient narrative, eCRF, GRD. The owner chooses the order.
- **Research, later.** Ideas of the uploaded draft such as a fast "System 1" answer mode ("Mode 2") are research items only. This design makes no claim about their performance.

## 13. Document control

| Version | Date | Notes |
|---|---|---|
| v1 | 2026-11-10 | Written from the spec as built (plan Tasks 1 to 13). Replaces the uploaded draft |
