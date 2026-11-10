# SFT model and deterministic reference lookup: design

Status: draft for review, 2026-11-09. Written from the brainstorm with the project owner on 2026-11-09. It conforms to the end-consumer programme design (`2026-10-28-end-consumer-programme-design.md`, "the programme") and replaces the uploaded draft `13-graphrag-design.md` as the basis for `docs/13`.

Notation: **Owner** marks a decision the project owner made. **Claude's call** marks a choice Claude made that the owner has not yet confirmed.

## 1. Intent

Two parts, built in this order (Owner):

1. **An SFT model as a GGUF.** It speaks medical language, knows the prevalent published information, writes text aligned to standards and to the expected literary style, and validates input claims as far as it can on its own.
2. **A deterministic reference lookup.** Exact, versioned facts and rules from the loaded public sources, each returned with its provenance.

The two are joined by one rule: **the route follows the type of claim** (Owner, section 4). The model owns language and judgement. The lookup owns versioned facts and rules. This keeps the system safe from stale data in the model's weights.

**Why it matters (Owner):** a small model that writes well and never invents a versioned fact is useful; one that invents them becomes "the butt of all jokes". The efficiency of teaching the model decides its success.

## 2. Goals, non-goals, anti-goals

**Goals**
- A small open-weight model (1–4B) fine-tuned for medical language, document structure and style, tool use and honest abstention, running as a GGUF on a Windows laptop or a Mac (Owner).
- A first document type end to end: the **PSUR/PBRER** (ICH E2C(R2)) from public data only (Owner, section 6).
- A versioned **rule store** of implementation guides (IGs) and guidelines, used to validate inputs and to check outputs (Owner, section 5.3).
- Basic **E2B(R3) ICSR** checks as a second user of the rule store (Owner; expectation marked in section 7).

**Non-goals (v1)**
- Company (MAH) data: exposure, own case tabulations, CCSI. Version 1 works from public data only; contextualisation and finer nuances come later, after due diligence of each use case (Owner).
- A full working E2B(R3) tool: no gateway submission, no acknowledgements, no certification against FDA or EMA validators (Owner: "not expecting it to be a full working tool").
- The other document types (CSR, PLPS, ICF, DSUR, IB, patient narrative, eCRF, GRD = Global Response Document in Medical Affairs). They follow PBRER, one at a time (Owner named them; order after PBRER is open).
- Dense vector retrieval (pgvector). See section 5.4.

**Anti-goals: a failure even if the system technically works**
- A draft in a drastically different reporting format, or with vital sections missing (Owner: "I would definitely wince").
- Any exact value (date, code, count, identifier, citation) produced from the model's weights instead of the lookup (programme 4.1).
- Any training row whose licence does not allow commercial training (section 8).

## 3. Constraints

- Programme rules 4.1–4.8 apply unchanged: exact values are fetched, never generated; every claim has provenance and a confidence basis; gaps are reported; reference data is versioned by date; licence posture follows `docs/02`; everything is audited; personal data is redacted.
- Training runs on rented GPU hardware; the laptop and its USB HDD build the data layer only (existing decision). One heavy job at a time on the HDD.
- Serving in v1: the GGUF on the owner's laptop or Mac; the lookup in the Postgres the owner runs. The model reaches the lookup through tools (MCPg), never by holding the facts (Claude's call).
- PostgreSQL 19beta3 on the build machine has neither pgvector nor pg_search (migration 0001 is deferred).
- Budget is limited; the teacher model is chosen partly on cost (Owner, section 9.3).
- Parameters (batch sizes, budget caps, thresholds, mix weights) live in configuration, not in code (existing rule).

## 4. Routing: the route follows the type of claim (Owner)

A claim goes to the **lookup** when both are true:
1. It names an entity that resolves in the substance crosswalk or the reference store: substance or active moiety (UNII), product (NDC, application number, EU product number), trial (NCT id), or another keyed record.
2. It asks for a field the reference store holds: approval status, label section content, patent or exclusivity date, TE code, recall, shortage, PSUSA outcome, referral, DHPC, trial status or result, FAERS count or signal.

All other claims stay with the **model**: mechanisms, clinical reasoning, terminology, structure and style.

Rules:
- The lookup answers from the **current release** by default, with provenance: source, release, `source_file`, record key (programme 4.3, 4.5).
- The model never overrides a lookup value. It may only phrase the text around it.
- A claim the model cannot settle, and the lookup cannot answer, is returned as **unverified**. No confidence threshold is invented: a threshold for this residual class is set later from measurement on the benchmark (Owner agreed; programme project 1).
- DailyMed labels, EPAR text and AACT stay out of the training corpus (`corpus_excluded_sources`; owner's design, `docs/02` section 2.2.1). For anything versioned, the lookup is the only authority.

Routing itself is deterministic (entity resolution plus field match), so it is testable and auditable (Claude's call on the mechanism; the rule is the Owner's).

## 5. Part 2 in detail: the deterministic reference lookup

Part 2 is described before Part 1 because the SFT examples (section 9) are built against it.

### 5.1 What already exists (no new model of the data)

Migration 0010 and the loaders already provide the fact store. The design reuses it; it does not add parallel tables such as `orange_book` or `trial` (Claude's call, correcting the uploaded draft):

| Need | Existing structure |
|---|---|
| Entity resolution | `substance`, `substance_xref` (UNII, synonyms, active moiety; 161,649 substances) |
| Releases and versioning | `reg_release`; snapshot per release, latest by default (reference-store decisions) |
| FDA and EMA structured records | `reg_record` (feeds in `src/episteme/data/regulatory/feeds.toml`: Drugs@FDA, Orange Book, NDC, recalls, shortages; EMA medicines, post-authorisation, orphan, PIP, PSUSA, referrals, DHPC, shortages, Article 57, outside-EU and others) |
| Labels | `label_section`, `label_substance` (DailyMed); EPAR text in `articles_ema_epar` |
| Trials | the `aact` schema and its views |
| Adverse events | `faers_report`, `faers_drug`, `faers_reaction` and the per-quarter tables (`faers_*_quarter`) |
| Literature and graph | `articles`, `article_body`, `article_cites`, `article_mesh`, `mesh_hierarchy` |

### 5.2 Lookup functions

A small set of typed, read-only functions exposed through MCPg (programme project 5). Each returns claim records in the programme's shape (programme 4.3, Owner): claim, provenance (source, release, `source_file`, record key), method, confidence with its basis, alternatives where the method is `mapped`, and coverage (how many records matched, what was missing). Every function takes `tenant` and `caller` arguments now, even though v1 has one user, so signatures and Stage B examples do not change later (Claude's call). Names and signatures are left to the builder. Minimum set for PBRER v1:
- resolve an entity to its substance or moiety, with the matched synonym and alternatives;
- approval status per jurisdiction;
- safety actions in an interval (recalls, referrals, DHPCs, PSUSA outcomes);
- the current label sections for a substance, with SPL version and effective date. The DailyMed extract keeps only the newest SPL version per `setId`, so v1 cannot compare versions; it reports "change history not determinable" as a gap. A label-history store is later work (programme project 2) (Claude's call);
- trials for a substance, with status and results flags;
- FAERS counts and signal statistics per quarter, labelled as US public spontaneous reports;
- literature for a substance and topic, with citation (section 5.4).

### 5.3 Versioned rule store (Owner)

A third kind of reference data, beside facts and literature: **rules** from implementation guides and guidelines, held as data with version and effective dates.

- Each IG or guideline version becomes checkable items: required sections or elements, their order, conditional rules ("if A then B required"), allowed code lists, field formats and lengths, date precision.
- It has two uses: **validate an input** against the IG version that applies, and **steer and check an output**. The model drafts; a deterministic check confirms the structure before anyone sees the draft. The Owner's wince test (section 2) becomes a hard gate.
- First contents: ICH E2C(R2) PBRER section list; ICH E2B(R3) data elements with the FDA and EU regional rules (section 7).
- Rules are loaded from the published IG documents and schema packages, under the licence each carries (section 8). The CDISC IGs stay red-listed (CLAUDE.md); CDISC CT is already wired.
- How rules are encoded (tables, JSON per version, or both) is left to the builder; every rule keeps a reference to its IG, version and clause.

### 5.4 Literature retrieval without vectors in v1 (Claude's call)

Exact lookup needs no vectors. Literature retrieval in v1 uses PostgreSQL full-text search plus MeSH expansion (`article_mesh`, `mesh_hierarchy`) and citation edges. pgvector is added when a server build with the extension exists (for example on the NVMe host), and then only for entry-point resolution (free text to concept), as the programme already decides (programme section 5).

### 5.5 Minimal harness for v1 (Claude's call)

The programme puts the full agent harness in project 6. PBRER v1 needs a minimal one now: a model server for the GGUF with tool calling (llama.cpp server or an equivalent with an OpenAI-compatible API, chosen in the plan), one fixed tool-call schema and chat template, a loop that runs the lookup calls, and the post-answer check (programme section 5, step 4) plus the rule-store gate. Stage B tool-use examples are written in exactly this template and schema. The full planner and step limits stay in project 6.

### 5.6 Data the lookup needs loaded first

On 2026-11-09 the sample loads (EMA, openFDA, AACT, DailyMed, preprints, FAERS) are running in `episteme_test`; the full loads into `episteme` follow, one at a time. Lookup functions and Stage B examples are built against each source once its full load has passed its gate. The plan orders work so that nothing waits on a load that has not happened.

## 6. PBRER v1: the first document (Owner)

- **Input:** a substance, active moiety or product, and a reporting interval.
- **Output:** a draft PBRER in ICH E2C(R2) section order. Every fact comes from the lookup or the public literature, with provenance.
- Sections that need MAH data appear with a fixed marker, "MAH data required", and no invented content: exposure, own case tabulations, CCSI.
- FAERS figures are labelled as US public spontaneous reports, never as the company's case counts.
- Before the draft is shown, the rule store checks section completeness and order (E2C(R2)). A draft that fails is not shown as a draft; the failures are shown instead.

Section map (Claude's call, from the brainstorm):

| PBRER section | Source | Route |
|---|---|---|
| Worldwide marketing approval status | Drugs@FDA, EMA medicines, Article 57, outside-EU | Lookup |
| Actions taken for safety reasons | FDA recalls, EMA referrals, DHPCs, PSUSA outcomes | Lookup |
| Changes to reference safety information | Current DailyMed label with its SPL version and effective date; the change history is reported as a gap in v1 | Lookup + gap |
| Clinical trial findings | AACT | Lookup |
| Literature | PubMed, PMC, preprints | Retrieval with citation |
| External adverse-event context | FAERS quarterly counts and signals | Lookup, labelled public |
| Exposure, own tabulations, CCSI | none public | "MAH data required" |
| Signal and benefit-risk evaluation, conclusions | the facts above | Model prose |

## 7. E2B(R3) ICSR: expectation marked (Owner)

**In scope: basic checks only.** Mandatory and conditional data elements present; ICH and regional code lists; field lengths and formats; date precision; batch and message header consistency; optionally XSD schema validation as a plain deterministic step.

**Out of scope.** Not a full working ICSR tool. No gateway submission or acknowledgements; no certification against FDA or EMA validators; no MedDRA term validation, because MedDRA is licensed and Episteme holds no licence. The check covers only the shape of a MedDRA code and the version field.

## 8. Licensing gate

Every source, every teacher output and the base model carry one of three classes (Owner: "a deal maker/breaker"):

| Class | Meaning |
|---|---|
| Use freely | Commercial training and RAG allowed, with attribution |
| Use with caution | RAG with citation; out of SFT until the open point is closed |
| Download-only | No corpus and no training |

Assessment of the new sources (2026-11-09, from official pages where found; each to be confirmed in `docs/02` before use):

| Class | Sources |
|---|---|
| Use freely | FDA Purple Book (US government work; FDA's own download); PDB/RCSB (CC0); Reactome (CC0, except the Functional Interaction network files, which carry KEGG data); Open Targets Platform (CC0; upstream terms per source); SureChEMBL (CC BY 4.0); Gene Ontology (CC BY 4.0); PubMedQA (MIT; contexts under PubMed posture); BioASQ (CC BY 2.5 plus NLM terms; registration by the owner); PARHAF (CC BY; embargoed part excluded); 21 CFR |
| Use with caution | MedMCQA and MedQA (MIT repos; exam questions whose rights holders granted nothing); FDA AdComm transcripts (written by contract court reporters); IUPHAR/GtoPdb (ODbL plus CC BY-SA); BindingDB (conflicting statements); UCUM (custom licence; fine as a lookup); Semantic Scholar (datasets ODC-BY, API non-commercial, abstracts belong to publishers); CDC (third-party content excluded); FDA Recognized Consensus Standards (list only; standard texts are copyrighted); LibreTexts (per page; only CC BY pages) |
| Download-only or out | MMedC (CC BY-NC-SA 4.0); WHO publications (CC BY-NC-SA 3.0 IGO; ICD/ICF CC BY-ND); PARCOMED (research-only); MedDRA, WHODrug, SNOMED CT (paid subscription, no sublicensing; Stream 2 only under a deployer's licence); ISO and other standard texts |

To verify before PBRER and E2B work starts (Claude's call): ICH guideline and E2B(R3) package terms; FDA and EU regional ICSR IGs; HL7 v3 ICSR artefacts.

## 9. Part 1 in detail: the SFT model

### 9.1 Base model (Owner: minimal, already decent in conversation)

- Size 1–4B, so the GGUF runs on a laptop or a Mac.
- **Licence gate:** Apache 2.0 or equivalent. Candidates: Qwen3 0.6B/1.7B/4B (Apache 2.0, confirmed on Qwen's release page); Qwen3.5 0.8B/2B/4B (reported Apache 2.0; confirm on the model card); SmolLM3-3B; Ministral 3B. **Excluded:** Qwen2.5-3B (Qwen Research License, non-commercial). Llama 3.2 1B/3B use their own community licence and are a fallback only.
- The same evaluation runs on two sizes (about 2B and about 4B), so size is chosen from measurement (Claude's call).

### 9.2 Two-stage training (Owner)

**Stage A: continued pre-training** on a licence-clean slice of the corpus. Only rows of class "use freely": `subset = commercial` (which covers the `permissive` and `public_domain` licence values), from sources outside `corpus_excluded_sources`. Deduplicated; every row keeps its `_lineage`. Mix: PMC OA commercial full text and other `commercial` literature, open guidelines (ICH, once verified), serialised reference text from training sources. Mix weights are configuration.

Excluded (Owner, 2026-11-09):
- **DailyMed labels and EPAR text** stay out, as already decided (`docs/02` section 2.2.1): they are looked up, not trained on. Regulatory writing style comes from the ICH guidelines and from checked Stage B examples built on lookup results.
- **PubMed abstracts** stay out: their subset is `open_metadata` and the abstracts often belong to publishers. PMC OA commercial full text carries the medical language.

**Stage B: instruction examples** built from the same slice:
1. Grounded Q&A: question, passage, answer with citation.
2. Drafting with tools: for example a PBRER section where the model calls the lookup and writes around the returned facts.
3. Rule checks: E2C(R2) completeness; basic E2B(R3) rules.
4. Abstain cases: "MAH data required" and "unverified".

Every example records the source rows it came from and the teacher that wrote it. A held-out split is reserved for evaluation.

Traceability (Claude's call): every Stage A slice, Stage B batch and training round writes a manifest (inputs with their lineage, teacher, counts kept and rejected, budget used, config hash) and ordinary logs. The hash-chained audit trail gains training event types only when `docs/11` is extended for them; until then the existing `corpus_materialize` event covers the slice.

### 9.3 Teacher model (Owner: commercial API allowed, cost matters)

- A commercial API teacher is allowed once its terms permit training other models. DeepSeek's Open Platform terms explicitly allow outputs to be used for "training other models (such as model distillation)". The open-weight DeepSeek-R1 licence reportedly forbids it, so running R1 weights as the teacher is excluded unless that licence is confirmed otherwise (Claude's call).
- Only public data is sent to the API. Version 1 is public data only, so this holds.
- Risk recorded: some funders or customers restrict DeepSeek-derived data. Every example keeps its teacher name, so a re-teach with another provider stays possible.

### 9.4 Efficient teaching (Claude's call)

- No teacher output is trusted as it comes. Each generated example passes deterministic checks before it is kept: its citation quotes the source passage; every exact value matches the lookup; the structure passes the rule store. Failures are logged, not trained on.
- Quality over volume: a small checked set, a training round, a measurement, then the next batch. Each round is gated on the evaluation (section 10); a round that scores worse never ships.
- Budget: a cap per round in configuration; token use is logged per round.

### 9.5 Output

A merged model quantised to GGUF (Q4_K_M and Q5_K_M), with a model card that names the base model and its licence, the training slice and its licence classes, the teacher, and the evaluation scores.

## 10. Evaluation and release gate

- The benchmark from programme project 1 (`docs/benchmark-questions.md`) plus the held-out split, scored on: exact facts, invented values (target zero), claim provenance, gaps reported (programme measures).
- PBRER-specific: section completeness and order (hard gate, from the rule store); every exact value traced to a lookup row; "MAH data required" markers present where expected.
- PBRER reference cases (Owner: fall back on PSUSA): built from public EMA PSUSA outcomes (`ema_psusa`). Each case names a substance and interval, the expected sections, the facts the lookup must return, the expected gaps and the expected "MAH data required" markers. Claude builds them; the owner checks them before they become gate cases (Claude's call on the review step).
- A model ships only if it beats the base model with the same tools on these measures, and has zero invented exact values on the benchmark (Claude's call).

## 11. Order of work, and the programme's project order

The programme put fine-tuning last (project 7, after the benchmark, reference store, tool layer and harness). The owner now moves SFT forward (Owner, 2026-11-09). To keep the programme's safety rules, this design pulls forward only the minimum each step needs (Claude's call):

1. Licence verification for the new sources and the ICH/E2B packages; `docs/02` updated.
2. Lookup functions for PBRER v1 (section 5.2) and the rule store with E2C(R2) (section 5.3). This is the slice of programme projects 2 and 5 that PBRER needs.
3. PBRER reference cases from PSUSA outcomes, checked by the owner, and a minimal benchmark runner for the PBRER measures (slice of programme project 1).
4. Stage A slice selection and Stage B example generation with the checks (section 9).
5. Training rounds on rented hardware; evaluation; GGUF.
6. E2B(R3) basic rules in the rule store.
7. `docs/13` rewritten from this design; `docs/02` gains the new rows (Purple Book, AdComm transcripts, licensed-vocabulary rule, base models and teacher as licensed inputs).

## 12. Open questions

| Question | Who |
|---|---|
| Which document type follows PBRER | Owner |
| Final base model and size, after measurement | Claude, from data |
| Residual-claim threshold for "unverified" | from benchmark measurement |
| Licence terms of ICH guidelines, E2B(R3) packages, regional IGs, HL7 v3 ICSR | Claude, to verify |
| Whether funders restrict DeepSeek-derived data | Owner, when funding is discussed |

## 13. Left to the builder

- Function names and signatures; rule encoding; table design within the existing schema.
- Stage A mix weights and Stage B example counts per round, from measurement.
- Training hyperparameters, LoRA settings, quantisation choice between Q4 and Q5.
- Test design under the project's existing rules.
