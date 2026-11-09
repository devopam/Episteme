# SFT model and deterministic reference lookup: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, in this repo, everything the SFT model and PBRER v1 need short of the GPU training itself: licence records, read-only lookup functions that return claim records, a versioned rule store (E2C(R2), basic E2B(R3)), a minimal harness, PSUSA-derived PBRER cases with a benchmark runner, the Stage A slice and the checked Stage B example generator.

**Architecture:** Three new packages. `episteme.reference` holds claim records, entity resolution, lookups and the rule store (read-only SQL over the 0010 tables; rules as packaged JSON per IG version). `episteme.harness` holds the PBRER fact assembly, the model loop against an OpenAI-compatible server, and the post-answer check. `episteme.sft` holds the Stage A slice sampler, the teacher client with its budget cap, the Stage B generator, and the deterministic example checks; every step writes a manifest. Training on rented hardware is documented as a handoff, not coded here.

**Tech Stack:** Python 3.12, psycopg 3, PostgreSQL 19beta3, pyarrow, `requests` (already a dependency), pytest (`pg` marker for DB tests), ruff.

**Spec:** `docs/superpowers/specs/2026-11-09-sft-and-reference-lookup-design.md` (approved by the owner 2026-11-09). It conforms to `docs/superpowers/specs/2026-10-28-end-consumer-programme-design.md`.

## Global Constraints

- `src/episteme/config.py` is the only module that reads `os.environ`. New tunables are `Settings` fields with defaults; secrets (the teacher API key) are read there and never logged or printed.
- Endpoint URLs live only in `scripts/data/_lib/sources.env` (read through `config._get`); no URL literal in `src/` outside config defaults that mirror existing patterns.
- Exact values are fetched, never generated (programme 4.1). The model never overrides a lookup value.
- Every lookup returns claim records: claim, provenance (source, release, `source_file`, record key), method (`fetched` | `computed` | `mapped` | `inferred`), confidence in [0, 1] with its basis, alternatives for `mapped`, coverage (spec 5.2).
- Every lookup function takes `tenant: str` and `caller: str` keyword arguments (spec 5.2).
- Lookups are read-only: no INSERT, UPDATE, DELETE, DDL or temp tables. (`load_substance.resolve_names` writes; do not call it from a lookup. Use `load_substance.resolve_key` for name normalisation.)
- Lookups read the latest release only: `reg_release.is_latest`, AACT views in schema `aact`, live FAERS tables.
- FAERS figures carry the label "US public spontaneous reports (FAERS)"; the fixed marker for missing company data is exactly `MAH data required`; the label-history gap text is exactly `change history not determinable`; the residual-claim status is exactly `unverified`.
- Training data: only `subset = 'commercial'` rows from sources outside `Settings.corpus_excluded_sources` (`dailymed`, `ema_epar`). PubMed (`open_metadata`) is excluded. MedMCQA and MedQA are "use with caution": not in SFT data.
- No real teacher API call in any test; the teacher client is tested against a fake HTTP server or a stubbed session.
- No real-data work on D: by implementers. `pg` tests run only on `episteme_test`, through the existing `pg_conn` and `clean_substance` fixtures; the controller runs them. Never cap a pg run with `timeout`.
- Git: `git add` explicit paths only; `ruff format` and `ruff check` before each commit; never `--no-verify`; never commit `.env`.
- Docs follow the repo style (ASD-STE100 spirit: short direct sentences); env keys are named, never their values.

## Review Focus

1. **A substance name that matches several substances, or none.** Expect: `resolve_entity` returns the best match with the others as alternatives and method `mapped`; no match returns an empty claim list with coverage saying so, never an exception. Pinned in Task 3.
2. **A dataset with no `is_latest` release, or an empty AACT/FAERS schema** (the sample loads are still in progress). Expect: the lookup returns no claims and a coverage gap naming the missing dataset; it does not fall back to an older release. Pinned in Task 4.
3. **A model answer that contains a date, code or count no tool returned** (for example an NDC or a PSUSA procedure number invented in prose). Expect: the post-answer check fails the answer and names the value. Pinned in Task 8.
4. **A teacher response that is malformed JSON, cites a passage it does not quote, or arrives after the budget cap is reached.** Expect: the example is rejected and logged in the manifest; no call is made past the cap. Pinned in Task 11.
5. **A PBRER draft with sections in the wrong order, a duplicated section, or a missing "MAH data required" marker.** Expect: the rule-store gate fails with each finding named by section and IG clause. Pinned in Task 5 and Task 7.

---

## File structure

| File | Responsibility |
|---|---|
| `src/episteme/reference/__init__.py` | package |
| `src/episteme/reference/claims.py` | `Claim`, `Provenance`, `Coverage`, `LookupResult` dataclasses; JSON round-trip |
| `src/episteme/reference/resolve.py` | `resolve_entity` (read-only) |
| `src/episteme/reference/lookups.py` | PBRER v1 lookup functions |
| `src/episteme/reference/literature.py` | MeSH-based literature lookup with citations |
| `src/episteme/reference/rules/__init__.py` | rule-store loader and checkers |
| `src/episteme/reference/rules/data/ich_e2c_r2.json` | PBRER section list (one IG version) |
| `src/episteme/reference/rules/data/ich_e2b_r3_basic.json` | basic E2B(R3) element rules |
| `src/episteme/harness/__init__.py` | package |
| `src/episteme/harness/pbrer.py` | fact assembly per PBRER section; draft skeleton |
| `src/episteme/harness/loop.py` | tool schema, chat loop against an OpenAI-compatible server |
| `src/episteme/harness/check.py` | post-answer check |
| `src/episteme/sft/__init__.py` | package |
| `src/episteme/sft/manifest.py` | manifest writer shared by slice, batches, rounds |
| `src/episteme/sft/slice.py` | Stage A mix sampler over materialised corpus shards |
| `src/episteme/sft/teacher.py` | teacher client with budget cap |
| `src/episteme/sft/examples.py` | Stage B example kinds and generator |
| `src/episteme/sft/checks.py` | deterministic example checks |
| `src/episteme/bench/pbrer_cases.py` | PSUSA-derived case builder and case schema |
| `src/episteme/bench/runner.py` | minimal PBRER benchmark runner |
| `src/episteme/config.py` | new `Settings` fields (Task 2) |
| `scripts/data/_lib/sources.env` | `TEACHER_BASE_URL` default (Task 2) |
| `docs/02-data-sources.md`, `docs/13-graphrag-design.md`, `docs/10-data-sources-runbook.md`, `docs/project-incubation-baseline.md` | docs (Tasks 1 and 12) |

---

### Task 1: Licence verification and `docs/02` rows

**Files:**
- Modify: `docs/02-data-sources.md`
- Test: `tests/docs/test_source_inventory_doc.py` (must still pass; no new test)

**Interfaces:** Produces the licence class of each input used by later tasks; Task 6 may encode E2B(R3) rules only if its row here is "use freely".

- [ ] **Step 1: Verify each licence on its official page** and record the URL and the date read: ICH guideline copyright notice (E2C(R2), E2B(R3), E3, E6); ICH E2B(R3) IG and schema package; FDA E2B(R3) regional IG; EU ICSR IG (EMA); HL7 v3 ICSR artefacts; FDA Purple Book download page; Qwen3 model card licence; DeepSeek Open Platform terms (training other models).
- [ ] **Step 2: Add rows to `docs/02`** with the three classes from spec section 8 (use freely / use with caution / download-only), keeping every existing row. Add: Purple Book and FDA AdComm transcripts to section 2.2; a "Licensed vocabularies" rule (MedDRA, WHODrug, SNOMED CT: Stream 2 only under a deployer's licence); the full new-source table from spec section 8; a new subsection "Model inputs" listing the base model and the teacher with their licences; MedMCQA and MedQA marked "use with caution" (not in SFT data).
- [ ] **Step 3: Run** `uv run pytest tests/docs -q` — Expected: PASS.
- [ ] **Step 4: Commit** `git add docs/02-data-sources.md` then `git commit -m "docs(02): licence classes of new sources, model inputs and IG packages"`.

If a licence cannot be confirmed from an official page, record it as "use with caution" with the reason; do not guess "use freely".

---

### Task 2: Claim records and settings

**Files:**
- Create: `src/episteme/reference/__init__.py`, `src/episteme/reference/claims.py`
- Modify: `src/episteme/config.py`, `scripts/data/_lib/sources.env`
- Test: `tests/reference/test_claims.py`, `tests/test_config_tunables.py`

**Interfaces:**
- Produces:
  - `Provenance(source: str, release: str | None, source_file: str | None, record_key: str | None, jurisdiction: str | None)` (frozen dataclass)
  - `Coverage(matched: int, missing: tuple[str, ...], notes: tuple[str, ...])`
  - `Claim(text: str, value: str | None, method: Literal["fetched","computed","mapped","inferred"], provenance: Provenance, confidence: float, confidence_basis: str, alternatives: tuple["Claim", ...] = ())`
  - `LookupResult(claims: tuple[Claim, ...], coverage: Coverage)` with `to_json() -> dict` and `LookupResult.from_json(d: dict) -> LookupResult`
  - Settings fields: `teacher_base_url: str | None` (env `TEACHER_BASE_URL`, default from sources.env), `teacher_api_key: str | None` (env `EPISTEME_TEACHER_API_KEY`), `teacher_model: str` (env `EPISTEME_TEACHER_MODEL`, default `deepseek-chat`), `teacher_budget_tokens: int` (env `EPISTEME_TEACHER_BUDGET_TOKENS`, default `2000000`), `sft_mix_weights: dict[str, float]` (env `EPISTEME_SFT_MIX_WEIGHTS`, `source=weight,...`, default empty = proportional), `harness_base_url: str` (env `EPISTEME_HARNESS_BASE_URL`, default `http://127.0.0.1:8080/v1`), `harness_max_tool_calls: int` (env `EPISTEME_HARNESS_MAX_TOOL_CALLS`, default `24`).

- [ ] **Step 1: Write failing tests**
  - `test_claim_confidence_must_be_in_unit_interval`: `Claim(..., confidence=1.2, ...)` raises `ValueError`.
  - `test_mapped_claim_keeps_alternatives_and_others_refuse_them`: a `fetched` claim with alternatives raises `ValueError`; a `mapped` claim keeps them.
  - `test_lookup_result_json_round_trip`: `LookupResult.from_json(r.to_json()) == r`.
  - `test_teacher_settings_defaults_and_api_key_not_in_repr`: with `EPISTEME_TEACHER_API_KEY=secret-x` set, `get_settings().teacher_api_key == "secret-x"` and `"secret-x" not in repr(get_settings())`.
  - `test_sft_mix_weights_parse`: `EPISTEME_SFT_MIX_WEIGHTS="pmc=0.7,guidelines=0.3"` gives `{"pmc": 0.7, "guidelines": 0.3}`; a negative weight raises `ConfigError`.
- [ ] **Step 2: Run** `uv run pytest tests/reference/test_claims.py tests/test_config_tunables.py -q` — Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the dataclasses (validation in `__post_init__`) and the settings fields; mark `teacher_api_key` `field(repr=False)`. Add `TEACHER_BASE_URL=https://api.deepseek.com/v1` to `sources.env` with a comment line naming spec section 9.3.
- [ ] **Step 4: Run** the same command — Expected: PASS. Run the endpoint guard from CLAUDE.md — Expected: no output.
- [ ] **Step 5: Commit** the five files: `feat(reference): claim records and SFT/harness settings`.

---

### Task 3: Entity resolution (read-only)

**Files:**
- Create: `src/episteme/reference/resolve.py`
- Test: `tests/reference/test_resolve.py` (`pg`)

**Interfaces:**
- Consumes: Task 2 types; `episteme.data.regulatory.load_substance.resolve_key`.
- Produces: `resolve_entity(conn: psycopg.Connection, text: str, *, tenant: str, caller: str, id_type: str | None = None) -> LookupResult`. Each claim's `value` is the `substance_key` as a string; `text` is the preferred name; provenance source `substance`, record key `unii:<UNII>` when present. Exact UNII or exact normalised name → method `fetched`, confidence 1.0; a name that maps to several substances → best by moiety then lowest key, method `mapped`, others as alternatives, confidence `1/n` with basis `"<n> substances share this name"`.

- [ ] **Step 1: Write failing tests** (fixture inserts a few `substance` and `substance_xref` rows; requests `clean_substance`):
  - `test_unii_resolves_exactly`
  - `test_shared_name_returns_mapped_with_alternatives`: two substances share name `x`; result has one claim, method `mapped`, one alternative, confidence 0.5.
  - `test_unknown_name_returns_empty_with_coverage`: `claims == ()`, `coverage.matched == 0`, `coverage.missing == ("substance",)`.
  - `test_resolve_writes_nothing`: run with the connection in a read-only transaction (`SET TRANSACTION READ ONLY`); no error.
- [ ] **Step 2: Run** (controller) `uv run pytest -m pg tests/reference/test_resolve.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** with one parameterised SELECT over `substance_xref` (`id_type` in `('unii','name')` or the given `id_type`, matching `resolve_key(text)` for names, the raw upper-cased value for UNII) joined to `substance`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit**: `feat(reference): read-only entity resolution`.

---

### Task 4: PBRER v1 lookup functions

**Files:**
- Create: `src/episteme/reference/lookups.py`, `src/episteme/reference/literature.py`
- Test: `tests/reference/test_lookups.py` (`pg`), `tests/reference/test_literature.py` (`pg`)

**Interfaces:**
- Consumes: Task 2 types; Task 3 `resolve_entity`.
- Produces (all keyword-only `tenant`, `caller`; `substance_key: int`; `start: date`, `end: date` inclusive):
  - `approval_status(conn, substance_key, *, tenant, caller) -> LookupResult` — from latest `reg_record` of datasets `fda_drugsfda`, `ema_medicines`, `ema_article57`, `ema_outside_eu`.
  - `safety_actions(conn, substance_key, start, end, *, tenant, caller) -> LookupResult` — `fda_enforcement`, `ema_referrals`, `ema_dhpc`, `ema_psusa`, filtered on `event_date`.
  - `current_label(conn, substance_key, *, tenant, caller) -> LookupResult` — DailyMed `label_section` via `label_substance.unii`; claim `value` is the section title; provenance record key `<article_id>#<loinc_code>`; the SPL version from `articles.book_meta`; one coverage note `change history not determinable`.
  - `trials(conn, substance_key, *, tenant, caller) -> LookupResult` — `aact.intervention_substance` joined to `aact.studies` (status, phase, results flag).
  - `faers_counts(conn, substance_key, start, end, *, tenant, caller) -> LookupResult` — `faers_moiety_quarter` and `faers_quarter_totals`; method `computed`; every claim text ends with `(US public spontaneous reports (FAERS))`.
  - `literature(conn, substance_key, topic: str | None, *, tenant, caller, limit: int = 20) -> LookupResult` — in `literature.py`: substance preferred name and synonyms matched to MeSH descriptor names in `article_mesh`, newest first; provenance record key `pmid:<pmid>`; no full-text scan of `articles`.
  - Module constant `PBRER_DATASETS: dict[str, tuple[str, ...]]` naming the datasets each function reads.

- [ ] **Step 1: Write failing tests** with fixture rows (reuse the `_schema`/`db` pattern of `tests/data/test_load_reg.py`):
  - `test_approval_status_reads_only_latest_release`: two releases, only `is_latest` rows appear.
  - `test_missing_dataset_is_a_coverage_gap`: no `fda_drugsfda` release → `coverage.missing` contains `"fda_drugsfda"`, other datasets still answer.
  - `test_safety_actions_filter_by_interval`
  - `test_current_label_reports_history_gap`: coverage notes contain exactly `change history not determinable`.
  - `test_faers_claims_are_computed_and_labelled`
  - `test_empty_aact_schema_is_a_gap_not_an_error`
  - `test_literature_cites_pmids`
- [ ] **Step 2: Run** (controller) `uv run pytest -m pg tests/reference -q` — Expected: FAIL.
- [ ] **Step 3: Implement.** Substance names come from `substance_xref` rows of the substance and its moiety; match `reg_record.substance_names` with `&&` (GIN index exists). Missing relation or no latest release → coverage gap, checked with `to_regclass` and the `reg_release` row before the query.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit**: `feat(reference): PBRER v1 lookups with provenance and coverage`.

---

### Task 5: Rule store and the E2C(R2) PBRER section list

**Files:**
- Create: `src/episteme/reference/rules/__init__.py`, `src/episteme/reference/rules/data/ich_e2c_r2.json`
- Modify: `pyproject.toml` (package data, if the build does not already include `*.json` under `src/episteme`)
- Test: `tests/reference/test_rules.py`

**Interfaces:**
- Produces:
  - Rule file shape: `{"ig": "ICH E2C(R2)", "version": "Step 4, 2012-12-17", "clause_base": "...", "sections": [{"id": "1", "title": "Introduction", "required": true, "mah_data": false, "clause": "3.1"}, ...]}`. `mah_data: true` marks sections that need company data (exposure, own tabulations, reference safety information as CCSI).
  - `load_rules(ig_id: str) -> RuleSet` (ids: `ich_e2c_r2`, `ich_e2b_r3_basic`).
  - `Finding(rule_id: str, clause: str, message: str)`.
  - `check_sections(rules: RuleSet, headings: list[str]) -> list[Finding]` — missing required section, wrong order, duplicate section.
  - `check_mah_markers(rules: RuleSet, sections: dict[str, str]) -> list[Finding]` — every `mah_data` section body contains `MAH data required` unless the caller supplied data (v1 never does).

- [ ] **Step 1: Write the section list** from the ICH E2C(R2) guideline table of contents (sections 1–20 and appendices), each with its clause number; source the text only after Task 1 marks ICH as "use freely".
- [ ] **Step 2: Write failing tests**: `test_complete_ordered_draft_has_no_findings`; `test_missing_section_is_named_with_clause`; `test_wrong_order_is_reported`; `test_duplicate_section_is_reported`; `test_mah_section_without_marker_fails`; `test_rule_file_ids_are_unique`.
- [ ] **Step 3: Run** `uv run pytest tests/reference/test_rules.py -q` — Expected: FAIL.
- [ ] **Step 4: Implement** the loader (`importlib.resources`) and the two checks. Heading match: case-insensitive, leading section number optional.
- [ ] **Step 5: Run** — Expected: PASS. **Commit**: `feat(reference): versioned rule store with the E2C(R2) PBRER sections`.

---

### Task 6: Basic E2B(R3) ICSR checks

**Files:**
- Create: `src/episteme/reference/rules/data/ich_e2b_r3_basic.json`, `src/episteme/reference/rules/icsr.py`
- Test: `tests/reference/test_icsr.py`, fixtures `tests/fixtures/icsr/valid_min.xml`, `tests/fixtures/icsr/missing_mandatory.xml`

**Interfaces:**
- Consumes: Task 5 `load_rules`, `Finding`.
- Produces: `check_icsr(xml_bytes: bytes, *, region: Literal["ich","fda","eu"] = "ich") -> list[Finding]`. Scope exactly as spec section 7: mandatory and conditional elements, code lists, lengths and formats, date precision, batch/message header consistency. MedDRA codes: only shape (8 digits) and the version element present. No XSD step in this task (left for later; note it in the rule file `"notes"`).

- [ ] **Step 1: Encode** the element rules from the ICH E2B(R3) IG (and the FDA/EU regional deltas for `region`), each with its data element id (for example `C.1.1`) and clause. Only if Task 1 marked the package "use freely"; otherwise stop and report BLOCKED.
- [ ] **Step 2: Write failing tests**: `test_minimal_valid_icsr_passes`; `test_missing_mandatory_element_is_named` (asserts the element id in the finding); `test_conditional_rule_fires`; `test_bad_date_precision_is_reported`; `test_meddra_code_shape_only` (an 8-digit code passes, `"abc"` fails, no term lookup happens); `test_malformed_xml_returns_one_finding_not_an_exception`; `test_xml_with_external_entity_is_refused` (parse with external entities and DTD loading disabled).
- [ ] **Step 3: Run** `uv run pytest tests/reference/test_icsr.py -q` — Expected: FAIL.
- [ ] **Step 4: Implement** with `xml.etree.ElementTree` behind `defusedxml` if it is already a dependency, else ElementTree with a pre-check that refuses any `<!DOCTYPE`.
- [ ] **Step 5: Run** — Expected: PASS. **Commit**: `feat(reference): basic E2B(R3) ICSR checks`.

---

### Task 7: PBRER fact assembly and draft skeleton

**Files:**
- Create: `src/episteme/harness/__init__.py`, `src/episteme/harness/pbrer.py`
- Test: `tests/harness/test_pbrer.py`

**Interfaces:**
- Consumes: Task 4 lookups; Task 5 `load_rules("ich_e2c_r2")`, `check_sections`, `check_mah_markers`.
- Produces:
  - `SECTION_SOURCES: dict[str, tuple[str, ...]]` — section id → lookup function names, exactly the map in spec section 6.
  - `PbrerFacts(substance: LookupResult, interval: tuple[date, date], sections: dict[str, LookupResult])`
  - `assemble_facts(lookup: Callable[[str, dict], LookupResult], substance_key: int, start: date, end: date, *, tenant: str, caller: str) -> PbrerFacts` — `lookup(name, kwargs)` is injected so the harness and tests share one path.
  - `skeleton(facts: PbrerFacts) -> dict[str, str]` — every E2C(R2) section in order; `mah_data` sections hold `MAH data required`; sections with no facts hold their coverage gaps.
  - `gate(draft: dict[str, str]) -> list[Finding]` — `check_sections` + `check_mah_markers`.

- [ ] **Step 1: Write failing tests** with a fake `lookup`: `test_skeleton_has_every_section_in_order`; `test_mah_sections_carry_the_marker`; `test_empty_lookup_section_states_the_gap`; `test_gate_rejects_a_draft_missing_a_section`; `test_faers_section_keeps_the_public_label`.
- [ ] **Step 2: Run** `uv run pytest tests/harness/test_pbrer.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — Expected: PASS. **Commit**: `feat(harness): PBRER fact assembly, skeleton and gate`.

---

### Task 8: Minimal harness loop and post-answer check

**Files:**
- Create: `src/episteme/harness/loop.py`, `src/episteme/harness/check.py`
- Test: `tests/harness/test_loop.py`, `tests/harness/test_check.py`

**Interfaces:**
- Consumes: Task 2 settings (`harness_base_url`, `harness_max_tool_calls`); Task 4 function names; Task 7 `gate`.
- Produces:
  - `TOOLS: list[dict]` — OpenAI-style function schemas for `resolve_entity`, the six Task 4 lookups; `tenant` and `caller` are injected by the loop, not exposed to the model.
  - `run(messages: list[dict], *, call_tool: Callable[[str, dict], LookupResult], post: Callable[[dict], dict], max_tool_calls: int) -> tuple[str, list[LookupResult]]` — `post` sends one chat-completion request (injected; the default posts to `harness_base_url` with `requests`).
  - `check_answer(answer: str, results: list[LookupResult]) -> list[str]` in `check.py` — every date (`YYYY-MM-DD`, `DD Month YYYY`), NDC, UNII, NCT id, PMID, EMA procedure number (`EMEA/H/C/PSUSA/...`) and integer count above 9 in the answer must appear in some claim `value`, `text` or provenance key; returns the offending values.
  - `FIELD_TERMS: dict[str, tuple[str, ...]]` in `check.py` — lookup function name → trigger terms (for example `approval_status`: approved, authorised, marketing authorisation; `safety_actions`: recall, referral, DHPC, PSUSA; `current_label`: label, contraindication, boxed warning, section; `trials`: trial, NCT, phase; `faers_counts`: adverse event report, FAERS, reports).
  - `route_claim(claim: str, resolve: Callable[[str], LookupResult]) -> tuple[Literal["lookup", "model"], str | None]` in `check.py` — spec section 4: `lookup` with the function name when the claim names an entity that `resolve` matches (via a UNII, NDC, NCT id, application number, or a name token) and contains a trigger term; else `("model", None)`. Deterministic: no model call.

- [ ] **Step 1: Write failing tests**: `test_loop_runs_tool_calls_and_returns_answer` (fake `post` returns one tool call then a final answer); `test_loop_stops_at_max_tool_calls`; `test_tenant_and_caller_are_injected_not_model_supplied`; `test_check_flags_an_invented_date`; `test_check_flags_an_invented_psusa_number`; `test_check_passes_values_present_in_results`; `test_claim_with_entity_and_field_routes_to_lookup` ("Is metformin approved in the EU?" with a fake `resolve` matching metformin → `("lookup", "approval_status")`); `test_mechanism_claim_stays_with_model` ("Metformin lowers hepatic glucose output" → `("model", None)`); `test_field_without_resolvable_entity_stays_with_model`.
- [ ] **Step 2: Run** `uv run pytest tests/harness -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — Expected: PASS. **Commit**: `feat(harness): minimal tool loop and post-answer check`.

---

### Task 9: PSUSA-derived PBRER cases and the benchmark runner

**Files:**
- Create: `src/episteme/bench/__init__.py`, `src/episteme/bench/pbrer_cases.py`, `src/episteme/bench/runner.py`
- Test: `tests/bench/test_pbrer_cases.py` (`pg` for the builder), `tests/bench/test_runner.py`

**Interfaces:**
- Consumes: Task 3, 4, 7, 8.
- Produces:
  - `PbrerCase(case_id: str, substance_key: int, substance_name: str, start: date, end: date, psusa_record_key: str, expected_facts: dict[str, list[str]], expected_gaps: dict[str, list[str]], reviewed_by_owner: bool = False)`; JSON file per case under `benchmarks/pbrer/<case_id>.json`.
  - `build_cases(conn, limit: int, *, tenant: str, caller: str) -> list[PbrerCase]` — picks latest `ema_psusa` records whose substance resolves to exactly one substance; interval = the 12 months before `event_date`; expected facts from the Task 4 lookups at build time.
  - `score(case: PbrerCase, draft: dict[str, str], results: list[LookupResult]) -> dict` — keys: `sections_ok` (gate empty), `invented_values` (count from `check_answer`), `facts_found` (share of expected facts present), `gaps_reported` (share of expected gaps stated), `mah_markers_ok`.
  - CLI `python -m episteme.bench.runner --cases benchmarks/pbrer --out <dir>`: runs only cases with `reviewed_by_owner: true`; writes `scores.json`.

- [ ] **Step 1: Write failing tests**: `test_case_round_trips_through_json`; `test_build_cases_skips_ambiguous_substances` (`pg`); `test_score_counts_invented_values`; `test_runner_skips_unreviewed_cases`.
- [ ] **Step 2: Run** `uv run pytest tests/bench -q` and (controller) the `pg` test — Expected: FAIL.
- [ ] **Step 3: Implement.** No case files are committed in this task; the controller builds them after the full loads and the owner reviews them.
- [ ] **Step 4: Run** — Expected: PASS. **Commit**: `feat(bench): PSUSA-derived PBRER cases and scoring runner`.

---

### Task 10: Manifests and the Stage A slice

**Files:**
- Create: `src/episteme/sft/__init__.py`, `src/episteme/sft/manifest.py`, `src/episteme/sft/slice.py`
- Test: `tests/sft/test_manifest.py`, `tests/sft/test_slice.py`

**Interfaces:**
- Consumes: Task 2 `sft_mix_weights`; the parquet shards written by `episteme.data.corpus_materializer` (`<corpus_root>/pretrain/source=<source>/year=<year>/part-*.parquet`).
- Produces:
  - `write_manifest(path: Path, kind: Literal["slice","batch","round"], *, inputs: list[dict], counts: dict[str, int], config: dict, teacher: str | None = None, budget_used: int | None = None) -> str` — writes JSON with a `config_hash` (sha256 of the canonical config JSON) and returns that hash.
  - `build_slice(corpus_root: Path, out_dir: Path, target_tokens: int, *, weights: dict[str, float], excluded: tuple[str, ...], seed: int) -> dict` — samples rows per source by weight (proportional when empty), refuses any shard whose `source=` is in `excluded` and any row whose `subset` is not `commercial`, writes `out_dir/stage_a/part-*.parquet` plus a `slice` manifest listing each input shard with row and token counts. Token count: whitespace tokens (the real tokenizer runs on the GPU host).

- [ ] **Step 1: Write failing tests** with tiny parquet shards in `tmp_path`: `test_excluded_source_shard_is_refused`; `test_non_commercial_row_is_dropped_and_counted`; `test_weights_shape_the_mix`; `test_same_seed_same_slice`; `test_manifest_hash_is_stable_for_equal_config`.
- [ ] **Step 2: Run** `uv run pytest tests/sft -q` — Expected: FAIL.
- [ ] **Step 3: Implement** streaming per shard with `pyarrow.parquet.ParquetFile.iter_batches` (never a whole table in memory).
- [ ] **Step 4: Run** — Expected: PASS. **Commit**: `feat(sft): Stage A slice and manifests`.

---

### Task 11: Teacher client, Stage B generator and example checks

**Files:**
- Create: `src/episteme/sft/teacher.py`, `src/episteme/sft/examples.py`, `src/episteme/sft/checks.py`
- Test: `tests/sft/test_teacher.py`, `tests/sft/test_examples.py`, `tests/sft/test_checks.py`

**Interfaces:**
- Consumes: Task 2 settings; Task 5 rules; Task 7 `skeleton`, `gate`; Task 8 `TOOLS`, `check_answer`; Task 10 `write_manifest`.
- Produces:
  - `Teacher(base_url: str, api_key: str, model: str, budget_tokens: int, session: requests.Session | None = None)` with `complete(messages: list[dict], *, max_tokens: int) -> str`; raises `BudgetExceeded` before any request that could pass the cap (estimate = prompt chars/4 + `max_tokens`); `used_tokens` from the response `usage`; never logs the key or the request headers.
  - Example kinds (dataclasses with `to_chat() -> list[dict]` in the Task 8 tool-call format): `GroundedQA(question, passage, passage_ref, answer)`, `ToolDraft(task, tool_calls, tool_results, answer)`, `RuleCheck(document, findings)`, `Abstain(question, reason)` where `reason` in (`MAH data required`, `unverified`).
  - `generate_batch(teacher: Teacher, passages: Iterable[dict], kind: str, n: int, out_dir: Path) -> dict` — writes kept examples to JSONL and a `batch` manifest with `teacher`, kept, rejected per reason, `budget_used`.
  - Checks in `checks.py`, each `(example) -> list[str]`: `check_citation_quotes_passage` (the answer's quoted span is a substring of the passage after whitespace normalisation), `check_exact_values` (Task 8 `check_answer` against the example's tool results; for `GroundedQA` against the passage), `check_structure` (Task 5/7 gate for PBRER drafts), `check_format` (valid JSON, required fields).

- [ ] **Step 1: Write failing tests** (stub `requests.Session`; no network): `test_budget_cap_refuses_before_request`; `test_usage_tokens_accumulate`; `test_api_key_never_in_logs` (caplog); `test_malformed_teacher_json_is_rejected_and_counted`; `test_citation_not_in_passage_is_rejected`; `test_invented_value_is_rejected`; `test_pbrer_draft_failing_gate_is_rejected`; `test_batch_manifest_records_teacher_and_rejections`; `test_examples_render_in_harness_tool_format`.
- [ ] **Step 2: Run** `uv run pytest tests/sft -q` — Expected: FAIL.
- [ ] **Step 3: Implement.** Prompts for each kind live as module constants in `examples.py`; the teacher must return JSON only.
- [ ] **Step 4: Run** — Expected: PASS. **Commit**: `feat(sft): teacher client with budget cap, Stage B generator and checks`.

---

### Task 12: Docs: `docs/13`, runbook, training handoff, drift log

**Files:**
- Create: `docs/13-graphrag-design.md`
- Modify: `docs/10-data-sources-runbook.md`, `docs/project-incubation-baseline.md`, `docs/02-data-sources.md` (only the cross-reference to `docs/13`)
- Test: `uv run pytest tests/docs -q`

- [ ] **Step 1: Write `docs/13`** from the spec: routing by claim type, the reference store as built (0010 tables), the rule store, the minimal harness, PBRER v1, E2B(R3) scope, literature retrieval without vectors (pgvector deferred), the later phases (full harness, pgvector, label history, other document types). It replaces the uploaded draft; it states the System-1/Mode 2 ideas only as later research, with no performance numbers.
- [ ] **Step 2: Add a runbook section** "SFT data and training handoff": run `corpus materialize`, then `build_slice`, then `generate_batch` per kind with the budget env keys named (not their values); what to copy to the rented GPU host (Stage A parts, Stage B JSONL, manifests); training is out of repo scope this round; GGUF export (Q4_K_M, Q5_K_M) and the model card fields from spec 9.5; then `python -m episteme.bench.runner` on the laptop against the GGUF served by llama.cpp at `EPISTEME_HARNESS_BASE_URL`.
- [ ] **Step 3: Drift log entry** in `docs/project-incubation-baseline.md`: what landed; deferred items (training rounds, owner review of PBRER cases, XSD validation, label history store, other document types); the legacy `src/episteme/model/train_supervised_finetuning.py` and `evaluate_benchmarks.py` load MedMCQA and MedQA, which are now "use with caution": not to be used for SFT data until the open point closes.
- [ ] **Step 4: Run** `uv run pytest tests/docs -q` — Expected: PASS. **Commit** the four files: `docs: GraphRAG design (13), SFT handoff runbook, drift log`.

---

## After the last task

- Controller runs the full non-pg suite, ruff, the endpoint guard, and the new `pg` tests on `episteme_test` (then re-applies test migrations and reloads substance data, per the pg-tests memory).
- Whole-branch review, then a PR to `main`. The owner merges.
- After the full loads: the controller builds PBRER cases (`build_cases`), the owner reviews them, then the first Stage B batch runs with a small budget.
