# GraphRAG design: SFT model and deterministic reference lookup (as built)

Status: as built on branch `docs/sft-reference-design`, 2026-11-10; the SQL lookup API (section 3.2), MCPg exposure and full-text search as built on branch `docs/graphrag-api-layer`, 2026-11-19 (spec `superpowers/specs/2026-11-17-graphrag-api-layer-design.md`). Design source: `superpowers/specs/2026-11-09-sft-and-reference-lookup-design.md` (the spec). Plan: `superpowers/plans/2026-11-09-sft-and-reference-lookup.md`. This document replaces the uploaded draft of `13-graphrag-design.md`. Where the code differs from the spec, this document states the code and marks the difference with **As built**.

Operator steps: `10-data-sources-runbook.md` section 5.17 (reference documents), section 5.19 (SQL lookup API, full-text indexes, reader MCPg instance) and section 6.4 (SFT data and training handoff). Licence classes: `02-data-sources.md` sections 2.2.1, 2.7 and 2.8. Open and deferred items: the drift log in `project-incubation-baseline.md` (entries 2026-11-10 and 2026-11-17).

## 1. Two parts and one rule

Episteme builds two parts:

1. **An SFT model as a GGUF.** A small open-weight model (1 to 4B) that writes medical text in the expected structure and style, calls tools, and abstains when it cannot settle a claim. Training runs on rented GPU hardware. This repo builds the training data and the evaluation, not the training run.
2. **A deterministic reference lookup.** Read-only functions that return exact, versioned facts and rules from the loaded public sources. Each fact carries its provenance.

**The route follows the type of claim.** A claim goes to the lookup when it names an entity that resolves (substance, product, trial or another keyed record) and asks for a field that the reference store holds (approval status, label section, safety action, trial status, FAERS count). All other claims stay with the model: mechanisms, clinical reasoning, terminology, structure and style. The model never overrides a lookup value. A claim that neither side can settle is returned as `unverified`. No confidence threshold for this class exists yet; it is set later from benchmark measurement.

Exact values (dates, codes, counts, identifiers, citations) come from the lookup, never from the model's weights. DailyMed labels and EPAR text stay out of the training corpus (`corpus_excluded_sources`). AACT stays out because it writes no `articles` rows (docs/02 section 2.2.1). For versioned facts the lookup is the only authority.

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
| Labels | `label_section`, `label_substance` (DailyMed and the EU labels of `ema_epar`), `articles`, `article_body` |
| Trials | the latest `aact_r<id>` release schema (the `aact` views point at it) |
| Adverse events | `faers_*` tables, the per-quarter signal tables and `faers_moiety_quarter_outcome` (seriousness and outcome per moiety and quarter) |
| Literature | `article_mesh`, `mesh_tree`, `article_cites`, `article_chemicals`, `articles`, and the full-text indexes on `article_body` |
| Code lists | `code_current` (migration 0013) |

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

**As built (2026-11-19).** Each Python lookup now calls its SQL function in `episteme_api_internal` (section 3.2) and maps the rows to `Claim` and `LookupResult`. The signatures and result shapes did not change. The harness tools still run in-process through `episteme.harness.loop.make_call_tool`. MCPg reaches the same functions through the read-only reader instance (section 3.3).

**Changed on purpose with the SQL functions:** the UNII arm of `resolve_entity` applies only when `id_type` is NULL or `unii`; `release` is filled for resolve, FAERS and literature claims (from `reg_release`), and `source_file` for trials (from the AACT `_load_state`); `faers_counts` adds the seriousness and outcome split and the per-term counts; `literature` expands the topic to its descendant descriptors (`mesh_tree`); `current_label` also returns the EU labels of `ema_epar` (jurisdiction `EU`, record key `<article_id>#<code>#<ordinal>`); `lookup_code` adds the OID of the code system to the claim text. Name normalisation is one SQL function (`norm_name`: NFKC, then `lower`); `lower` differs from Python's `casefold` for a few characters (for example `ß`), an accepted change.

### 3.2 SQL lookup API (`episteme_api`, migration 0015)

Two schemas. `episteme_api` holds the public functions: they call the internal ones, cut the result at the cap of their family and add a `note` row `truncated: <n> of <total>` (or `of more than <n>` when the internal call was already capped). `episteme_api_internal` holds the complete functions (the Python lookups and the PBRER harness use these), the helpers and the two settings tables. Every function is `STABLE`, `SECURITY DEFINER`, owned by `episteme_api_owner`, with `SET search_path = pg_catalog, pg_temp` and schema-qualified bodies. No function writes.

Every row carries its provenance. Claim functions return `claim_row`: `kind` (`claim`, `note` or `missing`), `claim_group`, `alt_rank`, `text`, `value`, `method`, `confidence`, `confidence_basis`, `source`, `release_id`, `source_file`, `record_key`, `jurisdiction`, `licence`. Article functions return `article_row`: `kind`, `article_id`, `source`, `title`, `year`, `score`, `relation`, `release_id`, `licence`, `note`, `source_file`. A `note` row holds a coverage note; a `missing` row names a source that is absent or not loaded. `licence` is the class of `LOOKUP_SOURCE_SUBSETS` (`commercial`, `pending`, `open_metadata`), or `unlisted` for a source not in that list. Rows tagged `pending` are returned with the tag; the SFT gate filters them.

| Function | Returns | Cap family (default, max) | Reads |
|---|---|---|---|
| `resolve_entity(p_text text, p_id_type text DEFAULT NULL)` | claims | `resolve` (10, 50) | `substance`, `substance_xref` |
| `substance_family(p_substance_key bigint)` | `substance_key`, `moiety_key`, `unii`, `preferred_name`, `source`, `release_id`, `licence` | none (one family) | `substance` |
| `approval_status(p_substance_key bigint)` | claims | `regulatory` (200, 1000) | `reg_record`, `reg_release`: `fda_drugsfda`, `ema_medicines`, `ema_article57`, `ema_outside_eu` |
| `safety_actions(p_substance_key bigint, p_from date, p_to date)` | claims | `regulatory` | `reg_record`: `fda_enforcement`, `ema_referrals`, `ema_dhpc`, `ema_psusa` |
| `current_label(p_substance_key bigint)` | claims | `label` (200, 2000) | `label_substance`, `label_section`, `articles` (`dailymed`, `ema_epar`) |
| `label_section_text(p_article_id text, p_section text)` | claims (the section text as value) | `label` | `label_section`, `article_body.body_text`; `p_section` is a code or a title |
| `trials(p_substance_key bigint)` | claims | `trials` (200, 1000) | `aact_r<id>.intervention_substance`, `studies`, `_load_state` of the latest release |
| `faers_counts(p_substance_key bigint, p_from date, p_to date)` | claims | `faers` (500, 5000) | `faers_moiety_quarter`, `faers_quarter_totals`, `faers_moiety_quarter_outcome`, `faers_event_quarter`, `faers_term` |
| `literature_by_mesh(p_substance_key bigint, p_topic text, p_from_year int DEFAULT NULL, p_to_year int DEFAULT NULL)` | articles | `literature` (50, 500) | `article_mesh`, `mesh_tree`, `articles` |
| `graph_neighbours(p_article_id text, p_kind text)` | articles | `graph` (50, 500); scan bound `graph_scan` (20000) | `p_kind`: `cites`, `cited_by` (`article_cites`), `shared_mesh` (major-topic descriptors, rarest first), `trial` (AACT `study_references` by PMID), `label` (`article_chemicals` to the moiety family to `label_substance`) |
| `search_text(p_query text, p_sources text[] DEFAULT NULL)` | articles | `search` (20, 200); scan bound `search_scan` (5000 per partition) | the full-text indexes (section 6.1) |
| `lookup_code(p_system text, p_code text)` | claims | `code` (1, 1) | `code_current` |

**Settings.** `episteme_api_internal.source_subset` (licence class per source) and `result_limit` (cap per family) are filled from `config.py` by `python -m episteme.reference.api_settings sync`, so `config.py` stays the only source. `graph_scan` and `search_scan` are not row caps: they bound the rows one call reads. The tunables are `EPISTEME_API_LIMIT_<FAMILY>=<default>,<max>`.

**Changes to these functions.** A migration that adds an API function, or drops and recreates one, must be followed by the roles file and `api_settings sync` (runbook section 5.19.4).

### 3.3 Exposure through MCPg

- **Reader instance.** `scripts/mcpg-episteme.sh <db> --reader` connects as `episteme_api_reader`: restricted mode, DDL and ALTER SYSTEM off, no secondary database. The role has USAGE on `episteme_api` and EXECUTE on its functions, and nothing in `episteme`, `episteme_api_internal` or the AACT schemas. Built-in `pg_catalog` functions stay available.
- **Guards.** The row caps of section 3.2; the scan bounds; a role-level `statement_timeout` (`EPISTEME_API_READER_STATEMENT_TIMEOUT_S`, default 30 s). The reader could raise its own session timeout with `SET`; MCPg's restricted mode is the wall there.
- **Grants.** `src/episteme/data/db/grants/0015_api_roles.sql` (superuser; roles, ownership, PUBLIC revokes), `0015_api_tables.sql` and `0015_api_aact.sql` (the owner role's reads; `api_settings sync` and the AACT load apply them). The reader password is the `.env` key `EPISTEME_API_READER_PASSWORD`, set by the operator.
- **Admin instances.** The everyday and admin MCPg instances are unchanged.

## 4. Versioned rule store

Rules from implementation guides and guidelines are held as packaged JSON, one file per IG version, under `src/episteme/reference/rules/data/`. Each file names its IG, version and source documents with their sha256. Every item keeps the clause of its source. Rule data is read-only after load. `load_rules(ig_id)` loads a file.

| Rule file | Source | Content |
|---|---|---|
| `ich_e2c_r2.json` | `E2C_R2_Guideline.pdf`, the E2C(R2) Q&A, and EU GVP Module VII (Rev 1) | The PBRER sections from the recommended table of contents (E2C(R2) 2.9.2): title page, executive summary, table of contents, sections 1 to 20, appendices A1 to A5, and the EU regional appendix A6 with A6.1 to A6.5 (`region: "eu"`). Each section has `required`, `mah_data` and `clause` |
| `ich_e2b_r3.json` | ICH E2B(R3) IG package v1.11: core data elements and business rules v1.00 (sheet `ICSR`), code lists v2.11, the reference instance | 223 data elements, 27 code lists, the conditional rules that the sheet states in a checkable form, and 71 rules listed under `manual` (not checked). Generated by `python -m episteme.reference.rules.build_e2b`; not edited by hand |

The E2C(R2) file holds the ICH section titles and clause numbers, with notes in our own words. The E2B(R3) file is generated from the ICH sheet and carries its data element names and its rule wording (`condition`, `text`) as published. The ICH notice allows reproduction and adaptation with ICH credit and marked changes, and the owner confirmed the ICH licence (2026-11-09; `02-data-sources.md` section 2.7.1). The `source_docs` of each file give the credit.

Checks:

- `check_sections(rules, headings, region=...)`: missing required section, wrong order (headings not in the longest ordered run of rule positions), duplicate section. Heading match is case-insensitive and punctuation-insensitive; the leading section number is optional.
- `check_mah_markers(rules, sections, strict=...)`: every `mah_data` section must hold the marker `MAH data required`. With `strict=True` (the PBRER gate), the marker must be the whole body.
- `check_icsr(xml_bytes, region=...)`: section 8.

**`mah_data` sections.** 5, 5.1, 5.2, 6, 6.1, 6.2, 6.3, 12, A1, A2, and in the EU appendix A6.3 and A6.4. Sections 6.1, 12, A6.3 and A6.4 go beyond the three groups of the spec (exposure, own case tabulations, CCSI). The source text supports each one; the owner agreed to keep them (2026-11-10).

## 5. Minimal harness

The full agent harness is programme project 6. PBRER v1 needs a minimal one (`src/episteme/harness/`):

- **Model server.** Any server with an OpenAI-compatible chat API and tool calls, for example the llama.cpp server with the GGUF. The URL is `EPISTEME_HARNESS_BASE_URL`.
- **Tool schema.** `TOOLS` in `harness/loop.py`: OpenAI-style function schemas for `resolve_entity` and the six lookups. `tenant` and `caller` are not in the schema: the loop adds them to each call and drops any value the model sends for them. Stage B tool examples use this same schema; their batch manifest records its hash (`TOOLS_SHA256`).
- **Loop.** `run(messages, call_tool=..., post=..., max_tool_calls=..., tenant=..., caller=...)` chats until the model answers. Each tool call counts toward `max_tool_calls`. When the caller does not give it, `run` reads `EPISTEME_HARNESS_MAX_TOOL_CALLS`. When the budget is spent, the loop asks once more with no tools. An argument error (`ToolArgumentError`) or a database error goes back to the model as a fixed text that names the tool; any other error stops the run. `make_call_tool(conn, ...)` needs an autocommit connection. On a connection that is not autocommit, a database error rolls the transaction back before it goes to the model.
- **Post-answer check.** `check_answer(answer, results)` in `harness/check.py` returns every date (`YYYY-MM-DD`, `DD Month YYYY`, `YYYY/MM/DD`, `DD/MM/YYYY` or `MM/DD/YYYY`), NDC, UNII, NCT id, PMID, EMA PSUSA procedure number (with or without `EMEA/H/C/`) and integer count above 9 in the answer that no claim value, claim text, record key, release id, coverage note or missing dataset states. A slash date with the day and month in unknown order passes when one of its two readings is stated.
- **Router.** `route_claim(claim, resolve)` returns `("lookup", <function>)` when the claim names an entity that `resolve` matches and holds a trigger term of `FIELD_TERMS`; else `("model", None)`. It makes no model call. It tries at most `max_candidates` candidate names; when the caller does not give it, it reads `EPISTEME_HARNESS_MAX_CANDIDATES`.

## 6. Literature retrieval without vectors

Exact lookup needs no vectors. **Corrected (2026-11-17):** pgvector 0.8.7 is installed on the PostgreSQL 19beta3 build machine; pg_search is not. No vector column or vector index is built yet (section 6.2).

**As built.** `literature(conn, substance_key, topic, ...)` (SQL function `literature_by_mesh`) matches a PubMed record when one of its MeSH descriptor names (`article_mesh`) equals a preferred name or synonym of a member of the substance family. `topic`, when given, matches the descriptor itself and every descriptor below it in `mesh_tree`, or a qualifier name of the same record (for example `adverse effects`; a topic that is no descriptor gives a note). Only the topic is expanded; the substance side keeps the v1 rule. Results are newest first, with record key `pmid:<pmid>`; `p_from_year` and `p_to_year` filter by year. The lookup needs the operator-built index `article_mesh_lower_name_idx` (runbook section 4.6).

**Citation and other edges.** `graph_neighbours` (section 3.2) goes one hop by live joins: citations both ways, articles that share a major-topic descriptor, trials that cite the PMID, and labels of the substances among the article's MeSH chemicals. No edge table is precomputed; that is decided from the use of this version.

### 6.1 Full-text search

- **Coverage.** Title and abstract for `pubmed`, `pmc`, `openalex` and `bookshelf`; title, abstract and the full stored text for `dailymed` and `ema_epar`. **As built:** `europepmc_preprint` is not covered: its rows are in `article_body_default` with the structured sources, which has no index. `pubchem` and the structured sources are not covered.
- **Mechanism.** One expression GIN index per `article_body` partition over `episteme_api_internal.fts_doc(source, title, abstract, body_text, text_override)` (`english` configuration). No column stores the vector, so no text is stored twice. The function reads the stored columns, because the virtual `text` (migration 0014) cannot be indexed.
- **Query.** `search_text` parses with `websearch_to_tsquery` only (input cut at 1000 characters). It searches only partitions with a valid index (the others give a note and a `missing` row), ranks the first `search_scan` matches of each partition with `ts_rank_cd`, and orders by score, then `article_id`.
- **Build.** One partition at a time, `CREATE INDEX CONCURRENTLY`, smallest first, with a free-space and a quiet-database check (runbook section 5.19.2).

### 6.2 Semantic search: the next spec

Not built. The next spec adds `semantic_search` and a hybrid function to `episteme_api`, with the provenance columns of section 3.2. The embedding pass runs on rented hardware; the caller embeds the query with the same model.

- **Scope measured (2026-11-17).** About 158 million vectors at full scope: titles and abstracts of the literature sources, label sections, and body chunks of PMC and Bookshelf. E: had 1,188 GB free.
- **Bake-off result (local report `D:/episteme/_bench/vecbench/REPORT.md`, 1M-abstract sample).** TurboQuant with re-ranking reached recall 0.996 to 0.999. pgvector `halfvec` HNSW reached 0.95 to 0.98 and needs about 586 GB at full scope. Binary-quantized HNSW stayed at or below 0.88. No TurboQuant extension builds for PostgreSQL 19, so the TurboQuant result is not usable on this server yet.
- **Embeddings.** NCBI publishes MedCPT embeddings of PubMed in the public domain. The next spec decides whether to use them for PubMed instead of embedding it again.

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
- **Licence gate per row.** `grounded_qa` and `abstain` need `subset = commercial` and a source outside `corpus_excluded_sources`. `tool_draft` takes the licence from the data: every source named in the provenance of the tool results must be `commercial` in `config.LOOKUP_SOURCE_SUBSETS` (`02-data-sources.md` section 2.7.2). `rule_check` rows carry `source = pbrer_skeleton`, `subset = commercial` and the tool results of their skeleton; those claim sources pass the same check.
- **Teacher.** An OpenAI-compatible API (`TEACHER_BASE_URL`), DeepSeek by default. A hard token cap (`EPISTEME_TEACHER_BUDGET_TOKENS`) is checked before every request. The API key (`EPISTEME_TEACHER_API_KEY`) never reaches logs, `repr` or exception text. Only public data is sent.
- **Restart.** `requests.jsonl` is an append-only ledger of paid requests. A re-run skips rows that were kept or paid for and never truncates `examples.jsonl`.
- **Manifests.** Every slice, batch and training round writes a manifest (`src/episteme/sft/manifest.py`) with inputs, counts, teacher, budget used and a `config_hash`.

## 10. Evaluation

- **Cases.** `build_cases` in `src/episteme/bench/pbrer_cases.py` builds one case per newest `ema_psusa` record whose substance names all resolve and all lead to one moiety family. Several substances of one family (for example a salt and its base) give one case. Interval: the 12 months before `event_date`. Expected facts and gaps come from the lookups at build time. Case id: `psusa-` plus the first 12 hex digits of the SHA-256 of the record key. A case counts only after the owner sets `reviewed_by_owner: true`.
- **Runner.** `python -m episteme.bench.runner --cases benchmarks/pbrer --out <dir>` scores the reviewed cases and writes `scores.json`: `sections_ok`, `mah_markers_ok`, `invented_values`, `facts_found`, `gaps_reported`.
- **Connection.** The runner connects through `dsn_from_settings` (the DB-mode guard) and `connect_kwargs` (the `application_name` tag), with autocommit and `default_transaction_read_only=on`.
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
- **Semantic search** (section 6.2): the next spec, from the bake-off result.
- **Label history store** (programme project 2), so section 4 can report changes, not only the current label.
- **Regional ICSR rules** (FDA, EU) and an optional XSD step.
- **Other document types** after PBRER, one at a time: CSR, PLPS, ICF, DSUR, IB, patient narrative, eCRF, GRD. The owner chooses the order.
- **Research, later.** Ideas of the uploaded draft such as a fast "System 1" answer mode ("Mode 2") are research items only. This design makes no claim about their performance.

## 13. Document control

| Version | Date | Notes |
|---|---|---|
| v1 | 2026-11-10 | Written from the spec as built (plan Tasks 1 to 13). Replaces the uploaded draft |
| v2 | 2026-11-19 | SQL lookup API (`episteme_api`, migration 0015) as built, MCPg reader instance, full-text search, pgvector 0.8.7 installed, the semantic-search next spec with the bake-off result |
