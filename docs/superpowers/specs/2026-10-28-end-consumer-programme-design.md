# End-consumer programme: design

Status: draft for review, 2026-10-28. Written from the end-consumer brainstorm with the project owner on 2026-10-28. This is a programme-level design. It sets the purpose, the rules and the order of seven projects. Each project gets its own design, plan and pull request, and must conform to this document.

Notation: **Owner** marks a decision the project owner made. **Claude's call** marks a choice Claude made that the owner has not yet confirmed; a project design may revisit it with a reason.

## 1. Intent

Episteme builds a connected, dated medical knowledge layer from open sources (`docs/02-data-sources.md`). This programme puts that layer in front of people.

**The product (Owner):** a conversational agent that truthfully gives deep insights from connected data. A question that would otherwise take a researcher a few days of information hunting is answered in one conversation.

**Why it is different:**
- Generic medical models (for example BioBERT, PubMedBERT, Meditron) learned from much the same public literature. They do not have the connected, dated reference layer, the computed signals, or the rule to report what the data cannot show.
- The value is in the paths between sources (for example ingredient → target → pathway → adverse event → evidence), which no single database shows.

## 2. Users

| Persona | What they ask | Source |
|---|---|---|
| Medical researcher | Adverse events of a drug by its composition (the active ingredient across all products), and what connects them. Answers they would not get without connected data. | Owner |
| Pharmacovigilance agent user | Precise, specific, traceable answers in the drug-safety domain. | Owner |
| Global regulatory-compliance and quality user | The same global questions on regulatory compliance and quality, in any jurisdiction. Jurisdiction is a dimension of every fact, not a separate product. | Owner |

**Operating model (Owner):** multi-tenant subscription, single operator. One operator runs the service; many subscribing tenants use it.

What this means (Claude's call, to settle in projects 5 and 6):
- **Access per tenant:** every tool call carries the tenant and the caller. The tool layer checks the tenant's entitlements before it runs.
- **Licences per tenant:** licence-bound data (Stream 2, section 4.6, for example MedDRA) is served only to a tenant that holds the licence. The tenant is the licence holder, not the operator. Open data is shared by all tenants, with the attribution its licence requires.
- **Isolation:** the shared reference store and graph are read-only to tenants. Any tenant-specific data (for example a tenant's own documents or configured targeted medical events) is isolated per tenant in the database, for example with row-level security.
- **Audit:** every audit record carries the tenant and the caller identity.

**Regulatory status (Owner):** decision support, **not a GxP-validated system**, at least for now. The benchmark gate (section 5, step 6) is engineering quality control, not formal validation evidence. The design keeps the audit trail and versioned data (4.5, 4.7) so that validation remains possible later, but no project does validation work now (Claude's call).

Delivery (Owner): a conversational agent. Context carries across turns; each turn shows its claims, sources and gaps. The agent may ask the caller to clarify an ambiguous question (Claude's call).

## 3. Scope

**Now:** human medicines. **In due course (Owner):** medical devices, veterinary products, languages beyond English, more jurisdictions. India-specific data is in scope from the first wave of new sources (Owner).

**Question types (Owner):** safety (adverse events, signals), regulatory compliance (approval status per jurisdiction, label differences), quality (recalls, warning letters, batch failures).

## 4. Rules every project must keep

### 4.1 Exact values are fetched, never generated (Owner)
Reference values (for example UCUM units, UNII codes, product identifiers, event terms, counts, dates) come from the database at answer time. The model never produces them from its weights.

The split is by **exactness**, not by rate of change (Claude's call, agreed in discussion): if an answer must be an exact, authoritative value, it is fetched. The rate of change decides only how the value is versioned (section 4.5).

### 4.2 Publications serve two roles (Claude's call)
- **Training:** publication text teaches the model fluency in the domain.
- **Retrieval:** publications are also the evidence behind claims. A citation (for example a PMID) is fetched, never recalled from memory.

### 4.3 Every claim has confidence and provenance, without fail (Owner)
"Correct" is relative in this industry: a correct value can be irrelevant in a given context. Example (Owner): two coders can choose different MedDRA preferred terms for the same input term, depending on context.

Each claim in an answer is a record (Claude's call on the structure):

| Field | Content |
|---|---|
| Claim | The statement. |
| Provenance | Source, release or date, jurisdiction, and the exact record or query behind it. |
| Method | `fetched` (exact lookup), `computed` (a statistic), `mapped` (a coding or mapping choice), `inferred` (a path through the graph). |
| Confidence | A score in [0, 1] with its basis. The basis depends on the method: for `fetched`, the fit to the context; for `computed`, the uncertainty and coverage; for `mapped`, the match score and the alternatives; for `inferred`, the strength and number of independent evidence links. |
| Alternatives | For `mapped` claims: the strongest alternatives with their scores and the reason for the choice. The caller can choose another, and the answer is rebuilt on that choice. |

The confidence comes from the method and the evidence, computed by the tools, not from the model's view of itself. It is calibrated on the benchmark (project 1): claims scored near 0.9 must be right in context about 9 times in 10, as judged by an expert (Claude's call).

### 4.4 Gaps are reported honestly (Owner)
When the data cannot support a pattern, the agent says so, with coverage numbers, and draws no conclusion past it. Example (Owner): lot or batch numbers are rarely present in adverse-event reports; the agent reports, for example, "lot present in 3 of 412 reports" and states that a lot effect can be neither shown nor ruled out. A gap is a valid result. Other gaps include: a source with no data for a jurisdiction, a traversal cut short by a limit (section 6), and data lag.

### 4.5 Reference data is versioned by date (Claude's call)
Each reference table keeps its history (valid from, valid to, source release). An answer can say "as of release X", and an audit can reproduce it.

### 4.6 Licence posture follows `docs/02`
Licence-bound data (for example MedDRA hierarchies, the IME and DME lists, the Indian Pharmacopoeia) is Stream 2: used only where the deployer holds a licence, never in open training data. `cdisc_bc` and `cdisc_usdm` stay download-only (CLAUDE.md).

### 4.7 Everything is audited
The system is decision support, not GxP-validated (section 2), but it keeps data-integrity practice. Every agent step is recorded in the existing hash-chained audit trail (`docs/11-gxp-data-integrity.md`), so any answer can be replayed and explained.

### 4.8 Personal data is redacted before it is recorded (Owner)
Callers can paste case narratives that contain personal data, but the hash-chained audit trail cannot be changed or cleared, while privacy laws (for example GDPR, India's DPDP Act) can require deletion. Decision (Owner): **redaction**, using Presidio (open source, Python; the Owner has contributed to it).

How it applies (Claude's call, to settle in project 6):
- Conversation text is redacted before it reaches the audit trail or any long-lived store. The audit trail holds only redacted text.
- If the original text must be kept (for example for a reviewer), it goes to a separate, encrypted store with a retention limit, with one key per conversation. Destroying the key erases it. The audit trail refers to it by a keyed hash (HMAC), never by a plain hash of the personal data.
- Recognisers cover the domain: medical identifiers and patient details in case narratives, plus jurisdiction-specific identifiers (for example India's).
- Redaction is not perfect. Project 1 measures its recall on test narratives, and the threshold is set from that measurement.

## 5. Architecture

| Layer | Role |
|---|---|
| Model | An open-weight model, fine-tuned for domain fluency, tool use and honest gap reporting (project 7). It phrases what the tools return. |
| Agent harness | Plans the question as typed sub-questions, runs tools within limits, checks every step, composes the answer, applies the post-answer check (project 6). |
| Tool layer | Narrow, typed tools reached through MCPg (for example look up a concept, resolve a UNII, convert a unit, compute a signal, find paths). Each tool returns results with provenance, confidence basis and coverage (project 5). No open SQL for the consumer. |
| Concept graph | A compact graph of concepts (ingredient, product, target, pathway, event term, variant, evidence group) with edges that carry precomputed weights. The large tables (articles, bodies, pubchem rows) sit underneath and are reached only for evidence at the last step (project 4). |
| Reference store | Dated, versioned reference tables with provenance (project 2). |
| Data layer | The existing pipeline: acquire, extract or serialize, load, graph. |

**Harness at every step (Owner: "proper harness present at all times/places"; Claude's call on the steps):**
1. Plan: checked against hop, step and time limits before it runs.
2. Tool call: only allowed tools; each with a timeout and a cost limit; each result validated for shape, coverage and provenance.
3. Each hop: fan-out limit; evidence must carry provenance.
4. Answer: the post-answer check blocks any claim without provenance or confidence, and any exact value that no tool returned.
5. Trace: every step to the audit trail.
6. Offline: the benchmark runs as a regression suite before any change to the model, the tools or the data goes live.

**Free and open-source software (Owner):** the preference is FOSS. Each choice (for example the agent and sub-agent orchestrator, the model server, the open-weight model) is made in its project design from a comparison of two or three candidates, scored on: reliability; longevity (an active community, steady releases); fit with the stack (Python, PostgreSQL, MCPg); observability (traces, metrics and logs that can feed the audit trail); active maintenance. Default components (Claude's call): PostgreSQL as the single store; MCPg as the access layer; PostgreSQL 19 SQL/PGQ property graphs over edge tables; `pgvector` for entry-point resolution only (free text → concept), not for hops.

## 6. Constraints

- **Resources (Owner):** limited. There is **no response-time target**.
- **Tuned database (Owner):** indexes, statistics and settings fitted to the real queries, checked with query plans.
- **Controlled memory footprint (Owner):** every component works within limits set in configuration (batch sizes, cache sizes including the per-conversation cache, worker counts, per-query database memory). Components stream their data and never hold a whole table in memory. This follows the pipeline design principles already in force (bounded units of work, byte-bounded batches).
- **Graph depth (Owner):** at most 5 hops for now, treated as the current PostgreSQL 19 limit; it may grow later. Reaching the cap is reported as a gap, never as "no connection". Project 4 confirms the real limit; the cap stays a setting either way (Claude's call).
- **Bounded traversal (Claude's call):** top results by edge weight at each hop, pruning by source, jurisdiction and date, and a time limit per question.
- **Precompute what is expensive (Claude's call):** signal statistics, co-reporting counts and evidence counts are rebuilt in batch when data loads, never computed from raw reports at question time.
- **No whole-graph summaries built in advance (Claude's call):** summaries of every graph community (as in Microsoft's GraphRAG) are too costly to build and keep current at this scale; targeted path search over precomputed edges is used instead.
- **Hardware:** the laptop and its USB HDD build the data layer. Serving needs SSD-class storage and runs elsewhere (Claude's call). Model training and fine-tuning run on rented hardware (existing decision).
- **One heavy job at a time on the USB HDD** (existing rule).

## 7. Projects and order

Agreed by the Owner on 2026-10-28. The current loads continue alongside. Each project has its own design, plan and pull request.

| # | Project | Delivers | Depends on |
|---|---|---|---|
| 1 | Benchmark and evaluation harness | The question set (started in `docs/benchmark-questions.md`), scoring (exact facts, invented values, gaps reported, paths found, known vs new, confidence calibration), expert-review flow, a runner that scores any model with or without the retrieval layer | none |
| 2 | Reference store design | Dated versioning (4.5); the exact-vs-trained split per source (4.1); stored text without the duplicate (below); placement of pubchem and other reference-type sources | 1 (to measure) |
| 3 | New sources, one at a time | openFDA `drug/event` (FAERS, with the batch field), `drug/enforcement`, `drug/ndc`, `drug/drugsfda`; DailyMed (downloaded, not loaded); EMA Article 57 and medicine data tables; the India first wave (Appendix B) | 2 |
| 4 | Concept graph and precomputed edges | Concept tables, edge tables with weights, PGQ graph, depth cap, refresh in batch | 2, 3 |
| 5 | Tool layer over MCPg | Typed tools returning provenance, confidence basis and coverage | 4 |
| 6 | Agent harness | Planner, step limits, per-step checks, post-answer check, audit trace, orchestrator choice | 5 |
| 7 | Fine-tuning on rented hardware | A fine-tuned open-weight model for fluency, tool use and honest gaps, measured on project 1 | 1, 6 |

**Stored text without the duplicate (Owner: storing the text twice "is not an inherent good design").** Today `text` = `title` + `abstract` + `body_text` (`docs/09` §277), so the body is stored twice. Project 2 removes the duplicate. It touches the extraction contract, every extractor and serializer, the loader, `article_body` and the corpus step.

**Removing the duplicate touches content hashes (Claude's call on how to handle it).** `content_hash` (`docs/09` §5) is computed over the full `text`, so changing `text` changes the hash, dedup, checkpoints and the audit `input_content_hash`. The running loads continue (Owner) and are not stopped for project 2. Project 2 must prefer an in-place database migration with new hashes, recorded in the audit trail, over a full reload; a reload of any source needs the Owner's approval and must respect the one-heavy-job rule.

**Project 3 feeds project 4 in slices (Claude's call).** Project 4 does not wait for all of project 3. The first slice is the sources that benchmark questions Q1 and Q2 need: openFDA `drug/event`, `drug/enforcement` and `drug/ndc`; DailyMed; ChEMBL; UniProt; Reactome. That allows end-to-end work (projects 4–6) early. EMA and India sources, including the scanned PDFs, follow as later slices.

**Benchmark baselines and scope (Claude's call).** Baselines are generative models that can hold a conversation: open medical instruct models (for example Meditron-class) and general open instruct models, each run with and without the Episteme tool layer. Encoder models (for example BioBERT, PubMedBERT) are not conversational baselines; they may be used inside the tools, for example for entity linking. Q3 (devices) and Q4 (veterinary) stay in the list but are not scored until their scope arrives.

**Hand-off from the build machine to the serving host (Claude's call).** The serving copy is built from dated data releases, not edited in place. Project 2 defines the release: what it contains, how it is moved (for example a dump per release or replication), and how the serving copy keeps the version history that "as of release X" needs. Each environment keeps its own audit chain, and a release records the hash of the source chain at the time it was cut.

**Jurisdiction as a dimension (Owner).** Every fact carries its jurisdiction and date. Where a jurisdiction has no data, the agent says so (4.4).

## 8. Data sources: what the recon found (2026-10-28)

Details: Appendices B and C. Summary:
- openFDA: only `drug/label` is on disk (14 files, 1.8 GB). The downloader already supports the other drug endpoints (`drug` and `all_human` modes).
- DailyMed: about 70 GB on disk (full SPL releases, updates, indexing files including substance/UNII and pharmacologic class); not loaded.
- FAERS is the only bulk case-level adverse-event source found. EU data is aggregated (EudraVigilance public portal); India has no public case-level data (PvPI reports go to WHO VigiBase).
- India publishes monthly batch-level drug-quality failures (CDSCO "not of standard quality" alerts), which FAERS lacks.

## 9. Open questions (to settle in the named project)

| Question | Project |
|---|---|
| MedDRA handling: deployer-licensed (Stream 2) only, or event-term name matching without the hierarchy? The IME and DME lists are MedDRA terms. | 2 |
| Licence of each India and EMA source whose site states none (Appendix B) | 3 |
| EDQM Standard Terms reuse terms (online and API only) | 3 |
| PostgreSQL 19's actual graph-depth limit | 4 |
| Serving hardware for the agent | 6 |
| Presidio recogniser set, recall threshold and the store for original text | 1, 6 |
| Which open-weight model, and which orchestrator framework | 6, 7 |
| How "relevance in context" is judged in expert review (who reviews, against what) | 1 |

## 10. Left to each project's builder

- Table and column design, within the rules above.
- The statistic for signal detection and its windows, with a reason.
- Tool names and signatures.
- Limits' default values (hops below the cap, fan-out, timeouts, batch and cache sizes), set from measurement.
- Test design, following the project's existing rules (representative samples covering the largest real unit; `pg` tests on `episteme_test`).

## Appendix A. Benchmark questions

Working list: `docs/benchmark-questions.md` (four questions: Q1 sudden co-reporting and the lot question; Q2 adverse events by composition; Q3 device malfunctions, proposed; Q4 veterinary vs human events, proposed). The full questionnaire comes later (Owner).

## Appendix B. India sources (survey 2026-10-28, web search only)

First wave agreed by the Owner: CDSCO not-of-standard-quality and spurious-drug alerts; CDSCO approved new drugs; NLEM 2022; NPPA ceiling prices; Jan Aushadhi list; ICMR Standard Treatment Workflows; PvPI safety alerts.

| Source | Holds | Format | Licence | Path |
|---|---|---|---|---|
| CDSCO NSQ / spurious alerts | Monthly batch-level failures (brand, manufacturer, batch) | PDF | Not stated | Reference |
| CDSCO approved new drugs | Approvals 1981–2025 (drug, strength, indication, date) | PDF by year | Not stated | Reference |
| PvPI (IPC) | Safety alerts since 2016, newsletters, annual reports | PDF | Not stated | Training + reference |
| NLEM 2022 | 384 medicines, 27 categories, level of care | PDF | Not stated | Reference |
| ICMR Standard Treatment Workflows | 157 workflows, 28 specialties | PDF | Not stated | Training + reference |
| NPPA ceiling prices | Price per scheduled formulation | PDF | Not stated | Reference |
| Jan Aushadhi (PMBJP) | 2,110 generics with drug codes | PDF | Not stated | Reference |
| CTRI | Trials in India since 2007 | One web page per trial; no bulk export found | Check | Reference + training |
| AYUSH portal, CCRAS journals, DRAVYA | About 43.7k records; open-access journals | Web, PDF | Journals open access; check | Training + reference |
| IDSP outbreak reports | Weekly district outbreaks | PDF | Not stated | Reference + training |
| HMIS | Monthly district indicators, API | CSV / API | GODL-India | Analytics |
| IndMED / MedIND | Index of about 100 Indian journals | Web | Check; status unverified | Training |
| NFHS-5 | District factsheets; unit-level via DHS | CSV | GODL / DHS terms | Analytics |
| Indian Pharmacopoeia | Monographs (IP-2026) | Print + online | Paid | Stream 2 only |
| MvPI | Device adverse-event programme | Forms only | — | Gap: no public data |
| TKDL | Traditional formulations | Restricted | Restricted | Excluded |

Notes: nearly all India sources are PDF, often scanned, so extraction needs text recognition and a per-document quality check. Local names and brands must be mapped to global identifiers (for example UNII). data.gov.in uses the Government Open Data Licence – India (2017): commercial and non-commercial use with attribution; personal data excluded.

## Appendix C. EMA, EDQM and openFDA (recon 2026-10-28)

| Item | Finding |
|---|---|
| IME list (EMA) | MedDRA terms for important medical events; updated twice a year with MedDRA; guidance only. Licence follows MedDRA. |
| DME list (EMA) | MedDRA terms for serious, often drug-related conditions; a safety net in signal detection. Licence follows MedDRA. |
| TME | Not an EMA list: targeted medical events are set per product by the company (for example in its risk management plan). The agent would let the user configure them. To confirm. |
| EDQM Standard Terms | Dose forms, routes, containers, units of presentation. Free account; online and API only; reuse terms not found. |
| EMA Article 57 public data | All EEA-authorised medicines: product, active substance, route, holder, country. Excel. |
| EMA medicine data tables | Centrally authorised medicines, post-authorisation procedures, shortages; updated nightly. |
| EMA legal notice | Reuse allowed, commercial and non-commercial, with EMA credited in each copy; third-party content excluded. |
| EudraVigilance public portal | Mostly aggregated counts; no bulk case-level download found. |
| openFDA | Downloads are zipped JSON parts listed in a catalogue with sizes; the FAERS bulk size is to be read from that catalogue. |
