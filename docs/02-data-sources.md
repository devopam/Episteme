# Data Sources — Episteme (Full Catalog)

**Last updated:** 2026-09-02
**Purpose:** Exhaustive *working inventory* of sources for **Stream 1** (parametric / SFT) and **Stream 2** (RAG / structured / incremental). Used to plan induction pipelines, rights posture, and pre-SSD sample work.
**Related:** `09-extraction-contract.md` (articles schema), `10-data-sources-runbook.md` (ops for sources already wired), `12-source-inventory.md` (live per-source inventory of what is wired), `superpowers/specs/2026-09-02-phase0-data-roadmap.md` (sequencing — subordinate; this file is the master *list*).
**Status note:** the *Induction status* cells below are a 2026-09-02 snapshot and are only partly refreshed. `12-source-inventory.md` is authoritative for which sources are wired and at which stages; where a cell says *Not started* or *Candidate* for a source listed there, docs/12 governs.

---

## Overview

Episteme uses a **bimodal** data strategy:

| Stream | Role | Typical landing zone |
|--------|------|----------------------|
| **Stream 1** | Pre-training & SFT — open, redistributable, relatively stable text | `episteme.articles` / SFT shards / protein text tables |
| **Stream 2** | RAG, SQL/KG, versioned vocabularies, volatile regulatory data | Postgres / Iceberg / vector index; **RBAC** where licensed |

**Rules of thumb**

- **NC (Non-Commercial)** → not in commercial parametric mixes; optional research track only.
- **Paid / affiliate licenses** (MedDRA, WHODrug, many SNOMED deployments) → **Stream 2 only**, and only when the *deployer* holds a license — not in public open-induction bulk by default.
- **Real patient notes** → red-list.
- Induction before SSD: build **download + sample extract** for each source; full volume after disk arrives (~2026-09-08).

---

## Stream 1 — Parametric memory & SFT

### 1.1 Foundational biomedical literature

| Source | What it contributes | Rights posture (typical) | Bulk / access | Induction status (Episteme) |
|--------|---------------------|--------------------------|---------------|-----------------------------|
| **PubMed** (baseline + daily updates) | Abstracts, MeSH, citation language | NLM redistribution terms; treat as `open_metadata` | NCBI FTP | **In progress** — extractor proven |
| **PMC OA Commercial (`oa_comm`)** | Full text CC0 / CC BY / BY-SA / BY-ND | Commercial-friendly OA only | AWS Open Data `pmc-oa-opendata` | **Sample done**; full on SSD |
| **PMC OA non-commercial / other** | Extra full text | NC or mixed — **exclude from commercial train** | Same bucket families | Track separately if research-only |
| **Europe PMC preprints** | Full-text preprints | Per-article CC; filter NC | EBI FTP (8 bulk files `PPR<a>_PPR<b>.xml.gz`, JATS; per-article licence, about 3% NC/ND) | **Done** (bulk files, read in bounded parts) |
| **Europe PMC author manuscripts** | Accepted manuscripts | Text-mining / copyright notices | EPMC FTP | **Wired** (download, extract, load, graph) — see docs/12 |
| **Europe PMC OA journals** | Overlap with PMC; EU-weighted | License-filter | EPMC / NCBI | Prefer NCBI `oa_comm` to avoid dup |
| **OpenAlex** (biomed slice) | Scholarly graph, OA links, metadata (CC0) | CC0 | `s3://openalex` | **Wired** (download, serialize, load) — see docs/12; strong metadata/KG candidate |
| **Semantic Scholar Open Research Corpus / abstracts** | Extra abstract coverage | Check current ToS/license | API / releases | Candidate |
| **PubChem** (serialized to NL) | Chemistry taxonomy & structure–name language | Public domain (US gov) | PubChem FTP/API | **Wired** (download, serialize, load) — see docs/12 |
| **Europe PMC / PMC ID mappings** | PMID–PMCID–DOI joins | Open | EPMC FTP | **Re-download integrity**; load `id_map` |

### 1.2 Guidelines & educational text

| Source | What it contributes | Rights posture | Bulk / access | Induction status |
|--------|---------------------|----------------|---------------|------------------|
| **EPFL Meditron guidelines** | Clinical practice guideline prose (WHO, CDC, NICE mixes — verify each) | Corpus license on HF; **per-source** restrictions may apply (e.g. NICE outside UK) | `epfl-llm/guidelines` | **Wired** (download, extract, load, graph) — see docs/12; license audit remains open |
| **OpenMedText** | MDPI CC BY articles + open textbooks | CC BY; **exclude NC subdirs** | Project release | **Not started** |
| **WHO guidelines (where CC/open)** | Public health guidance | Mixed — only explicitly open items | WHO IR / publications | Selective |
| **CDC / open government guidance (US)** | Public domain US federal text where applicable | Public domain (US) | cdc.gov / FTP | Selective |
| **Open textbooks** (e.g. selected LibreTexts, NCBI Bookshelf OA) | Pedagogy, definitions | Per-book license | Various | Catalog per title |

### 1.3 Multilingual & synthetic corpora

| Source | What it contributes | Rights posture | Bulk / access | Induction status |
|--------|---------------------|----------------|---------------|------------------|
| **ApolloCorpus** | Multilingual medical books/papers/QA | Corpus license — **diligence before commercial** | HF `FreedomIntelligence/ApolloCorpus` | **Sample done** (~1M+ rows); more shards optional |
| **MMedC** | Large multilingual medical tokens | **Often research/NC-leaning — audit before commercial** | HF `Henrychur/MMedC` | Deferred pending license |
| **PARHAF / PARCOMED** | Synthetic French clinical narratives (fictitious patients) | CC BY / Etalab-class | Project releases | Candidate (privacy-safe synthetic) |
| **Other synthetic clinical** (e.g. open synthetic EHR-style where truly non-PHI) | Instruction-style clinical language | Per dataset | HF / papers | Case-by-case |

### 1.4 SFT / reasoning datasets

| Source | What it contributes | Rights posture | Access | Induction status |
|--------|---------------------|----------------|--------|------------------|
| **MedMCQA** | Exam-style MCQ + rationales | **Use with caution** (2026-11-09): MIT repo, but the exam questions have rights holders who granted nothing. Not in SFT data | HF | **Not started** |
| **PubMedQA** | Yes/no/maybe + context | Open research norms / license on release | HF / official | **Not started** |
| **MedQA / USMLE-style open sets** | Clinical exam reasoning | **Use with caution** (2026-11-09): MIT repo, but the exam questions have rights holders who granted nothing. Not in SFT data | HF | Candidate |
| **mmlu / medical subsets / MedGENIE-style** | Broad + medical QA | Per release | HF | Candidate |
| **Medprompt-style CoT/ToT collections** | Explicit reasoning chains | Depends on underlying items | Constructed | Build only from Stream-1-clean sources |
| **BioASQ** (where redistributable) | Biomedical QA | Task licenses | BioASQ | Candidate |

### 1.5 Molecular narrative (text for model, not only SQL)

| Source | What it contributes | Rights posture | Access | Induction status |
|--------|---------------------|----------------|--------|------------------|
| **UniProtKB Swiss-Prot** | Curated protein function, names, annotation narrative | UniProt license — generally open with attribution | `ftp.uniprot.org` | **Wired** as `uniprot` (download, serialize, load) — see docs/12 |
| **UniProtKB TrEMBL** (selective) | Broader sequences/annotation | Same family; prefer reference proteomes post-2026 reshaping | FTP | Later / selective |
| **Gene Ontology** (annotations + definitions) | Function vocabulary in text form | CC BY 4.0 (GO) | geneontology.org | Candidate |
| **Reactome** (pathway summaries) | Pathway biology language | CC0 / open (confirm current) | reactome.org | Candidate |
| **Open Targets** (text fields) | Target–disease evidence narratives | Open / EMBL-EBI terms | Platform download | Candidate |
| **GUIDE TO PHARMACOLOGY (IUPHAR/BPS)** | Receptor/drug class expert summaries | Check database terms | Keep as Stream 1 or 2 per license | Candidate |

---

## Stream 2 — RAG, relational, versioned knowledge

*Ingest to DB / Iceberg / indexes; incremental updates; not default full-weight pretrain.*

### 2.1 Pharmacology & biochemistry (structured)

| Source | What it contributes | Update pattern | Access | Induction status |
|--------|---------------------|----------------|--------|------------------|
| **ChEMBL** | Bioactivities, structures, assays | Periodic Postgres dumps | EBI FTP | **Wired** (download, serialize, load) — see docs/12 |
| **SureChEMBL** | Patent chemistry | Parquet / periodic | EBI | Candidate |
| **PubChem** (structured) | Compounds, assays, synonyms | Frequent | NCBI | API + bulk |
| **UniProt** (structured cross-refs) | Accession graphs, features | ~8-week releases | FTP | Pair with Swiss-Prot text |
| **PDB / RCSB** | Structures, metadata | Continuous | wwPDB | Metadata + optional text |
| **BindingDB** (if license OK) | Binding affinities | Periodic | Site | Check terms |
| **ClinVar** | Variant–phenotype | Regular | NCBI FTP | VCF/XML pipelines |
| **dbSNP** (summary use) | Variant catalog | Periodic | NCBI | Selective |

### 2.2 Trials, labels, regulatory (volatile)

| Source | What it contributes | Update pattern | Access | Induction status |
|--------|---------------------|----------------|--------|------------------|
| **AACT** (ClinicalTrials.gov) | Full trials relational DB | Monthly / daily dumps | CTTI downloads | **High value** — `pg_restore` sample |
| **ClinicalTrials.gov** API prose | Public study descriptions | Continuous | API | Complement AACT |
| **DailyMed** (SPL XML) | US labeling | Daily/weekly/monthly zips | NLM | **High value** — sample ZIP parse |
| **openFDA** | Labels, FAERS, recalls, NDC | API / bulk | open.fda.gov | Incremental JSON |
| **Drugs@FDA / Orange Book** | Approval & exclusivity | Periodic | FDA | Structured RAG |
| **EMA open data** (selected) | EU assessment / shortages where open | Varies | EMA | Jurisdiction-specific |
| **FAERS** (public) | Adverse event reports | Quarterly / openFDA | FDA | RAG analytics |
| **FDA Purple Book** | Licensed biological products, biosimilar and interchangeable status, exclusivity | Monthly CSV and XLSX files, each with the changes and a full snapshot | `purplebooksearch.fda.gov/downloads` | Candidate. **Use freely:** FDA's own download; FDA states its web content is public domain unless noted (read 2026-11-09). The downloads page itself states no terms |
| **FDA AdComm transcripts** | Advisory committee meeting transcripts | Per meeting | FDA | Candidate. **Use with caution:** contract court reporters wrote the text; rights not confirmed. RAG with citation only |

#### 2.2.1 Regulatory sources wired for the first cut (2026-11)

Design: `superpowers/specs/2026-11-02-regulatory-sources-design.md`. Stages and scripts: `12-source-inventory.md`. Operator steps: `10-data-sources-runbook.md` sections 5.8 to 5.15. The licence posture is a project ruling for each source, not a legal determination; it is recorded here first.

| Source (`source` key) | What it contributes | Stored in | Update pattern | Licence posture |
|---|---|---|---|---|
| **DailyMed** (`dailymed`): SPL labels, prescription and OTC kinds | Label text and sections (LOINC-coded, as offsets into the body), the substances and NDC products of each label | `episteme.articles` and `article_body` (source `dailymed`); `label_section`, `label_substance` | Full release zips plus monthly, weekly and daily update zips | US government work: `license = public_domain`, `subset = commercial` (set by the extractor; `license_raw` keeps the real text). Animal, homeopathic and remainder releases wait |
| **EMA feeds** (`ema`): one dataset per feed: `ema_medicines`, `ema_post_authorisation`, `ema_orphan`, `ema_pip`, `ema_psusa`, `ema_referrals`, `ema_dhpc`, `ema_shortages`, `ema_mrl`, `ema_herbal`, `ema_outside_eu`; `ema_article57` (Excel); `ema_epar_documents` (the document index) | Reference records of EU medicines, designations, safety communications, shortages, residue limits, herbal and non-EU opinions; the product and substance names that join to the US data | `episteme.reg_record`, one partition per dataset and release | EMA publishes the JSON feeds about twice a day; a release is cut per the `cadence` of `feeds.toml` | EMA legal notice: reuse allowed, commercial and non-commercial. **Credit EMA as the source in each copy** (use the wording of EMA's legal notice when a product ships). Third-party content is excluded. Reference data, no `license` column |
| **EMA EPAR documents** (`ema_epar`): English PDFs of type product information, assessment report and RMP summary | The text of the EPARs | `episteme.articles` and `article_body` (source `ema_epar`) | Follows the `ema_epar_documents` feed; one PDF at a time, with a delay | Same EMA terms: `license = permissive` (nearest value of the vocabulary), `license_raw` holds the terms, `subset = commercial`. **Credit EMA** in each copy; third-party content excluded. The owner approved loading EMA |
| **openFDA `drug/*`** (`openfda`): `enforcement` (recalls, with lot numbers in `code_info`), `ndc`, `drugsfda`, `orangebook`, `shortages` | US recalls, NDC products, approvals, exclusivity and shortages, keyed to substances | `episteme.reg_record` | Monthly or as openFDA republishes the download catalogue | US government data, published under openFDA's terms of service. Treated as public domain. Third-party text inside records is not cleared individually |
| **FAERS** (`openfda_faers`): `drug/event` zips (about 12,000 reports each) | Adverse event reports from 2004: reports, suspect and concomitant drugs with batch numbers, reaction terms; signal tables by quarter | `episteme.faers_*` (typed tables, by generation) | Quarterly in the zips; a refresh is a full reissue, done by hand (runbook section 5.12) | US government data. Reaction terms are MedDRA preferred terms published in FDA's public data. **Open (owner):** operator rights to expose the terms to subscribing tenants, before serving |
| **FDA substance data** (`other/substance` of openFDA, loaded by `load_substance`) | UNII, every synonym, RxCUI and ATC codes, active-moiety links: the anchor of the US/EU bridge | `episteme.substance`, `substance_xref` | When FDA republishes (one file per run) | US government data (FDA substance registration); treated as public domain |
| **AACT** (`aact`): the ClinicalTrials.gov snapshot, one pipe-delimited file per table in one zip | Studies, conditions, interventions, design, eligibility, outcomes, results, reported adverse events and references (609,804 studies in the 2026-11-03 release); `intervention_substance` ties each intervention to a substance (`substance_key`) and its active moiety (`moiety_key`) by exact normalised name | One schema per release, `aact_r<YYYYMMDD>`, with views in schema `aact` over the latest release; a row in `episteme.reg_release` (`dataset = 'aact'`). No `episteme.articles` rows | One full snapshot per release; the loader takes the newest zip. The monthly and daily dumps of CTTI are the source of the zips | ClinicalTrials.gov records are public US government data. The project treats them as public domain. Sponsor-supplied documents are not loaded. Lookup data: AACT writes no `articles` row, so it cannot reach the training corpus |
| **ICH guideline files** (`ich`): E2 guidelines, the E2B(R3) package page, and the efficacy and quality guideline pages | The official ICH texts, Q&As, implementation guide packages and information papers: E2 safety reporting (E2A to E2F, E2B(R3)) plus the efficacy and quality guidelines (E and Q series) | Files under `<raw>/ich/` with `_manifest.jsonl`; no database row | On demand (`run_pipeline.sh ich download`); with `--force`, a changed file is kept as a new dated copy | ICH public-licence notice: use, reproduce and adapt with ICH credit, mark every change, do not imply ICH endorsement. Class "use freely, with attribution" (confirmed by the owner 2026-11-09, section 2.7.1). The ICH logo and third-party content are excluded |
| **MedDRA Points to Consider** (`meddra_ptc`): the English term-selection and data-retrieval documents, the companion document and the best-practices document | The official coding and retrieval guidance for safety rules | Files under `<raw>/meddra_ptc/` with `_manifest.jsonl`; no database row | On demand; the documents follow the March and September MedDRA releases | The documents carry the ICH public-licence notice: use freely with attribution. **MedDRA terminology is licensed and out of scope** (section 2.4); the release notes and guides of each MedDRA version are not fetched |
| **EMA guidance documents** (`ema_guidance`): a fixed list of 55 documents under `/en/documents/` (GVP modules, PSUR and signal lists, EU ICSR implementation guide, RMP and QRD templates) | The EU text that rules for pharmacovigilance are built from | Files under `<raw>/ema_guidance/` with `_manifest.jsonl`; no database row | On demand, in chunks. EMA answers non-browser clients with HTTP 429, so the stage waits `EPISTEME_EMA_DOC_MIN_INTERVAL_S` (default 60) between requests and stops with exit 3 to resume | EMA legal notice: reuse allowed, commercial and non-commercial, with EMA credited in each copy; third-party content excluded. The SPOR API is out of scope (owner decision 2026-11-09); the owner exports SPOR lists from the web portal later |

Notes:

- `cdisc_bc` and `cdisc_usdm` rules are unchanged (section 2.6).
- **Not in the training corpus.** DailyMed labels, EMA EPAR text and the AACT trial tables are reference data: they are looked up, not trained on (owner's design, `docs/superpowers/specs/2026-10-28-end-consumer-programme-design.md`). `corpus materialize` leaves out the sources in the config list `corpus_excluded_sources` (`dailymed`, `ema_epar`), although their rows carry `subset = commercial`. `aact` has no `articles` rows, so the list does not need it; `article_schema.TRAINING_SOURCES` and `REFERENCE_SOURCES` classify every source of `articles`.
- Rights for EPAR text and DailyMed text, if a later decision puts them in the training corpus: Claude's reading is that the EMA notice allows reuse with credit and that DailyMed labels are public. The owner confirms before such a build.
- **Beta limits (owner, 2026-11-03).** This cut adds no new download. It has no trial version history (the agent says that amendments such as site additions, eligibility, procedures, dosing and endpoint changes are not covered), no EMA scientific guidelines, no FDA Complete Response Letters or historical documents, and no OpenAlex author, source or institution loaders. The OpenAlex raw download continues and is kept.
- Veterinary, device and food endpoints of openFDA, PMS, SPOR and CTIS are not wired; the model takes them as new `feeds.toml` entries.

### 2.3 Open or semi-open terminologies & units

| Source | What it contributes | License notes | Induction status |
|--------|---------------------|---------------|------------------|
| **UCUM** | Units of measure | UCUM license (typically free use with notice — confirm) | **List for Stream 2**; small artifact |
| **MeSH** | Subject headings | NLM terms | Align with PubMed extract |
| **RxNorm** | Drug names / normal forms | **UMLS license** required | Stream 2 if license held |
| **LOINC** | Lab/observation codes | LOINC license (free but terms) | Stream 2 |
| **HPO** | Phenotype terms | Open | Stream 2 / KG |
| **MONDO** | Disease ontology | Open | Stream 2 / KG |
| **ICD-10 / ICD-11** | Diagnosis coding | **WHO license terms** — not “public domain dump” everywhere | Stream 2 only with rights |
| **ATC** (WHO) | Anat. therapeutic chemical class | WHO terms | Stream 2 with rights |
| **ORDO / rare disease** | Rare disease ontology | Check | Candidate |
| **NCIt** | NCI thesaurus | Open-ish NCI terms | Candidate |

### 2.4 Licensed vocabularies (RBAC / customer-supplied only)

*Do not put in public Episteme open-induction FTP scripts. Support as **optional connectors** when the customer provides licensed data.*

| Source | Role | Notes |
|--------|------|-------|
| **MedDRA** | AE / safety coding | Paid MSSO license |
| **WHODrug** / WHO DD | Drug dictionary for PV | UMC/WHO licensing |
| **SNOMED CT** | Clinical terminology | Affiliate / national license |
| **MedDRA IME list** | Important Medical Events | **Depends on MedDRA** |
| **EMA DME / similar DME lists** | Designated Medical Events | Often MedDRA-coded; rights follow MedDRA + publisher |
| **Country-specific drug dictionaries** | Local PV / claims | Per country |

**DME / IME:** Treat as **Stream 2, license-bound** (usually MedDRA-linked), not Stream 1 tokens.

**Rule (2026-11-09).** MedDRA, WHODrug and SNOMED CT are paid subscriptions without sublicensing. They go to Stream 2 only, and only under a deployer's own licence. Episteme holds none of these licences. No training data, rule store or example set may contain their content. Checks may test the shape of a code and its version field only.

### 2.5 Knowledge-graph & graph-adjacent open sets

| Source | Role | Notes |
|--------|------|-------|
| **OpenAlex** | Works–authors–concepts graph | Also listed under Stream 1 metadata |
| **SemMedDB** (if still available / license OK) | Predications from PubMed | Historical; verify redistribution |
| **PrimeKG / open medical KGs** | Integrated disease–drug–gene graphs | Per project license on HF/Zenodo |
| **Hetionet** | Integrated network | Open | Candidate |
| **DrugBank open subsets** (if any remain open) | Drug targets | Much of DrugBank is licensed — audit |

### 2.6 Clinical data standards (CDISC)

*Each item below has its own licence. Verified 2026-09-25. Only the controlled terminology is cleared for Stream 1.*

| Source | What it contributes | License notes | Induction status |
|--------|---------------------|---------------|------------------|
| **CDISC Controlled Terminology** (via NCI EVS) | Codelists and terms for SDTM, SEND, ADaM, Define-XML and Protocol (CDASH terms ship inside the SDTM file); quarterly dated releases per package | NCI states CDISC Terminology is free to use without licensing restrictions; `public_domain` governance override (decision 2026-09-28), same pattern as MeSH/PubChem/ClinVar | **Wired as `cdisc_ct`** — download, serialize, load (per-package retire keeps only the newest release per package; see `docs/10-data-sources-runbook.md` §5.6 and `docs/12-source-inventory.md`) |
| **CDISC Biomedical Concepts** (COSMoS `export/`, incl. SDTM dataset specializations) | Granular clinical-observation concepts and their SDTM mappings | COSMoS README: code MIT, "documentation and minutes" CC-BY-4.0; **no licence stated for `export/` data** | **Wired as `cdisc_bc`, download-only**; no serializer and no corpus inclusion until CDISC confirms (see `12-source-inventory.md`) |
| **USDM** (Unified Study Definitions Model, `cdisc-org/DDF-RA`) | Study-definition model, API spec, implementation guide | Same README pattern as COSMoS: code and scripts MIT, "content files like documentation and minutes" CC-BY-4.0; **model files not explicitly covered** | **Wired as `cdisc_usdm`, download-only** (latest release's `Deliverables/`); no serializer and no corpus inclusion until CDISC confirms (see `12-source-inventory.md`) |
| **SDTM** model and Implementation Guide | Tabulation model and implementation rules | CDISC standard licence: use **within your organization only**, **no derivative works**, no copying/distribution; machine-readable metadata via CDISC Library (membership) | **Stream 2 only**, deployer-licensed, like MedDRA/WHODrug; never in the open training corpus |

### 2.7 Licence classes of sources added 2026-11-09

Every source, every teacher output and the base model carry one class. Design: `superpowers/specs/2026-11-09-sft-and-reference-lookup-design.md` section 8.

| Class | Meaning |
|-------|---------|
| Use freely | Commercial training and RAG allowed, with attribution |
| Use with caution | RAG with citation; out of SFT until the open point is closed |
| Download-only | No corpus and no training |

The source table below is carried from the spec and was not re-read in this task. Only the items in 2.7.1 and 2.8 were verified on official pages. It is a project ruling, not a legal determination. Confirm each source again before first use.

| Class | Sources |
|-------|---------|
| Use freely | FDA Purple Book (US government work; FDA's own download); PDB/RCSB (CC0); Reactome (CC0, except the Functional Interaction network files, which carry KEGG data); Open Targets Platform (CC0; upstream terms per source); SureChEMBL (CC BY 4.0); Gene Ontology (CC BY 4.0); PubMedQA (MIT; contexts under PubMed posture); BioASQ (CC BY 2.5 plus NLM terms; registration by the owner); PARHAF (CC BY; embargoed part excluded); 21 CFR |
| Use with caution | MedMCQA and MedQA (MIT repos; exam questions whose rights holders granted nothing); FDA AdComm transcripts (written by contract court reporters); IUPHAR/GtoPdb (ODbL plus CC BY-SA); BindingDB (conflicting statements); UCUM (custom licence; fine as a lookup); Semantic Scholar (datasets ODC-BY, API non-commercial, abstracts belong to publishers); CDC (third-party content excluded); FDA Recognized Consensus Standards (list only; standard texts are copyrighted); LibreTexts (per page; only CC BY pages) |
| Download-only or out | MMedC (CC BY-NC-SA 4.0); WHO publications (CC BY-NC-SA 3.0 IGO; ICD/ICF CC BY-ND); PARCOMED (research-only); MedDRA, WHODrug, SNOMED CT (paid subscription, no sublicensing; Stream 2 only under a deployer's licence); ISO and other standard texts |

#### 2.7.1 Guideline and ICSR standard packages (read 2026-11-09)

Rules from a document may enter the rule store only when its class is "use freely". Keep the ICH credit, mark every adaptation, and do not imply ICH endorsement. The ICH notice excludes third-party content and the ICH logo. **The ICH class is confirmed by the owner (2026-11-09: "ICH is license free as such").** The notice grants use, reproduction, adaptation and modification with credit and has no non-commercial term; it says "under a public license" and names no licence. The owner statement covers the ICH guidelines; the attribution conditions of the notice still apply. The E2C(R2) rule file holds the ICH section titles and clause numbers. The E2B(R3) rule file is generated from the ICH business-rules sheet and carries its data element names and rule wording as published. The `source_docs` of each rule file give the ICH credit.

| Item | Official page read | Key licence words (paraphrase) | Class |
|------|--------------------|--------------------------------|-------|
| ICH E2C(R2) PBRER | `database.ich.org/sites/default/files/E2C_R2_Guideline.pdf` (legal notice, front page) | May be used, reproduced, adapted, modified, translated or distributed under a public license; credit ICH; label changes; no sponsor impression | Use freely, with attribution (confirmed by the owner 2026-11-09) |
| ICH E2B(R3) IG v5.03 (Step 4, 18 July 2025) | `database.ich.org`, ICH E2B(R3) IG PDF (legal notice, page 3) | Same ICH notice. Third-party content inside the IG (for example ISO, MedDRA, UCUM, EDQM) is excluded | Use freely, with attribution (IG text only; confirmed by the owner 2026-11-09) |
| ICH E2B(R3) XML schema and message files | ICH E2B page: the fetch tool returned no content on 2026-11-09 | ICH material; the owner states ICH is licence-free (2026-11-09) | Use freely, with attribution (owner: ICH licence-free, 2026-11-09) |
| ICH E6(R3) GCP (Step 4, 16 June 2026) | `database.ich.org`, E6(R3) final consolidated PDF (legal notice, front matter) | Same ICH notice; the ICH logo is excluded | Use freely, with attribution (confirmed by the owner 2026-11-09) |
| ICH E3 clinical study reports (1995) | `database.ich.org/sites/default/files/E3_Guideline.pdf` | The PDF carries no legal notice. The ICH copyright page returned no content | Use freely, with attribution (changed 2026-11-10 from "use with caution: no notice found"; the owner statement of 2026-11-09 covers all ICH guidelines) |
| FDA regional E2B(R3) IG (final, Aug 2024, docket FDA-2016-D-1280) | FDA guidance page; `fda.gov/about-fda/about-website/website-policies` | The guidance page states no terms of its own. The class rests on FDA's website policy: web content is public domain unless noted. ICH text inside keeps the ICH notice | Use freely (FDA text; class rests on FDA's website policy); third-party text excluded |
| EU ICSR IG (EMA/51938/2013, Rev 2) | `ema.europa.eu/en/about-us/legal-notice` | EMA legal notice: reuse in whole or in part, commercial and non-commercial, with EMA credit in each copy; third-party content excluded. The guide itself was not read (HTTP 429) | Use with caution: guide not read |
| HL7 v3 ICSR artefacts (ISO/HL7 27953) | ISO and HL7 catalogue entries (search result only) | An ISO/HL7 standard; the catalogue says reuse needs a fee or licence. HL7's own terms were not read | Download-only (ISO standard text) |

#### 2.7.2 Lookup sources in Stage B examples

A Stage B `tool_draft` or `rule_check` example is built on lookup results, not on corpus rows. Its licence comes from the data: every source named in the provenance of its tool results (alternatives included) must have class `commercial` in the config constant `LOOKUP_SOURCE_SUBSETS` (`src/episteme/config.py`). The corpus exclusion (`corpus_excluded_sources`) does not apply here: it governs raw text in the corpus. A source with class `pending` gives the reject reason `licence_pending`. A source that is not in the table, or has another class, gives the reject reason `licence`. A row whose tool results hold no claim gives `licence`: no source can be verified. Keep this table and the constant in step: there is no licence registry for lookup sources in code yet.

| `Provenance.source` | Class in `LOOKUP_SOURCE_SUBSETS` | Effect on a `tool_draft` or `rule_check` example |
|---|---|---|
| `substance` | `commercial` | Allowed |
| `dailymed` | `pending` | Held (`licence_pending`). **Open (owner):** whether DailyMed facts may appear in tool examples (DailyMed text stays out of the corpus) |
| `ema_epar` | `pending` | Held (`licence_pending`). No lookup emits it yet. **Open (owner):** whether EPAR facts may appear in tool examples |
| `aact` | `commercial` | Allowed |
| `fda_drugsfda` | `commercial` | Allowed |
| `fda_enforcement` | `commercial` | Allowed |
| `ema_medicines` | `commercial` | Allowed |
| `ema_article57` | `commercial` | Allowed |
| `ema_outside_eu` | `commercial` | Allowed |
| `ema_referrals` | `commercial` | Allowed |
| `ema_dhpc` | `commercial` | Allowed |
| `ema_psusa` | `commercial` | Allowed |
| `pubmed` | `open_metadata` | Refused (`licence`). **Open (owner):** whether PubMed literature facts (citations from `literature`) may appear in tool examples |
| `openfda_faers` | `pending` | Held (`licence_pending`). **Open (owner):** rights to the MedDRA reaction terms in FAERS facts (section 2.2.1) |

**`pbrer_skeleton` label.** A `rule_check` example checks a PBRER draft against the rule store. Its input row carries `source = pbrer_skeleton` and `subset = commercial`; the batch refuses a `rule_check` row with any other label. `skeleton()` copies claim lines into section bodies (for example FAERS counts and literature citations), so the row also carries the `tool_results` its skeleton was built from. The batch checks every claim source of those results by the table above, as for a `tool_draft`. A `rule_check` row with no tool results is refused (`licence`).

### 2.8 Model inputs

| Input | Role | Licence evidence (read 2026-11-09) | Class |
|-------|------|------------------------------------|-------|
| **Qwen3 (1.7B)** | Base model for the SFT model | Hugging Face model card `huggingface.co/Qwen/Qwen3-1.7B` states `apache-2.0` | Use freely: Apache 2.0. Keep the licence and notices with the weights |
| **DeepSeek Open Platform API** | Teacher that writes Stage B examples (public data only) | Terms of service section 4.2 (`cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html`): Inputs and Outputs may be used for "training other models (such as model distillation)", subject to law and the Terms | Use freely for outputs under these terms. Each example keeps its teacher name. Open-weight DeepSeek-R1 is not used as teacher |

Only the 1.7B card was read. The 0.6B and 4B cards must be verified before use. Qwen3.5 and the other candidate base models are not verified here. Verify the model card before any of them is used.

---

## Red-list (prohibited / avoid for open Episteme)

| Item | Reason |
|------|--------|
| **Real patient notes** (MIMIC, eICU, etc.) without full legal pathway | PHI / credentialed access |
| **WHO ICTRP bulk scrape** against NC terms | NC / ToS |
| **EU CTR / CTRI** without reliable open bulk rights & pipeline | Rights + engineering |
| **NICE full commercial reuse outside allowed use** | Licensing |
| **BiMediX / ArSyra-type NC corpora** for commercial weights | NC / paid |
| **MedDRA / WHODrug / SNOMED dumps in public training corpus** | License |
| **CDISC standard documents (SDTM IG and similar) in public training corpus** | CDISC standard licence: organization-internal use, no derivative works |
| **Any source with unclear commercial ML rights** | Until counsel/docs clear |

---

## Pre-SSD induction backlog (sample-first)

Work that **does not need the full 8 TB** — build scripts + **small subset** extract now; full run after ~2026-09-08.

| Priority | Source | Sample idea | Pipeline shape |
|----------|--------|-------------|----------------|
| P0 | PubMed scale | Already have pattern; more files | Existing extractor + workers |
| P0 | PMC `oa_comm` | Already 20; keep tool hot | Existing |
| P0 | Apollo remaining shards | 1–2 files already done | Existing |
| P0 | ID mappings | Full file is small | Validate CSV → `id_map` |
| P1 | **Swiss-Prot** | 1k entries or one taxon | New table `proteins` |
| P1 | **AACT** | One Postgres dump subset / sample tables | `pg_restore` + document |
| P1 | **DailyMed** | One daily ZIP | SPL XML → text + metadata |
| P1 | **openFDA** drug labels | Paginated API sample | JSON → normalized table |
| P1 | **ChEMBL** | SQLite/sample tables or subset dump | Relational Stream 2 |
| P1 | **OpenAlex** biomed filter | S3 sample prefix | Metadata / graph edges |
| P1 | **Meditron guidelines** | HF subset | License audit + text extract |
| P1 | **PubChem** | Small compound batch | Serialize to NL sentences |
| P2 | **UCUM** | Full (tiny) | Spec → table |
| P2 | **GO / HPO / MONDO** | Full OBO/OWL | Ontology tables |
| P2 | **ClinVar** | One VCF/XML slice | Stream 2 |
| P2 | SFT packs (MedMCQA, PubMedQA, …) | Full (usually small) | JSONL → SFT store |
| P2 | Author manuscripts | One baseline tar | JATS like preprints |
| Done | **CDISC Controlled Terminology** | Wired as `cdisc_ct`: download, serialize, load (per-package retire) | Serialized codelists in `articles` (Stream 1) |
| Later | MedDRA/WHODrug/SNOMED | **Only with customer license packs** | RBAC RAG connectors |
| Blocked | **Biomedical Concepts / USDM** | Download-only (`cdisc_bc` and `cdisc_usdm` wired) | Serializer only after CDISC confirms data licence |
| Later | **SDTM model / IG** | **Only with a deployer CDISC licence** | Stream 2 RAG connector |

---

## Mapping to “what role in project success”

| Capability | Primary sources |
|------------|-----------------|
| Biomedical language & argumentation | PubMed, PMC commercial, preprints, guidelines, Apollo |
| Commercial full-text depth | PMC `oa_comm`, CC-clean guidelines/textbooks |
| Multilingual | Apollo, MMedC *(if license OK)*, PARHAF/PARCOMED |
| Chemistry language | PubChem serialized, ChEMBL (RAG/SQL), GtoPdb |
| Protein function | Swiss-Prot (± GO/Reactome) |
| Trials & protocols | AACT, CT.gov API |
| Labels & safety signals (public) | DailyMed, openFDA, FAERS |
| Precise coding (AE, drugs, clinical) | **Licensed** MedDRA / WHODrug / SNOMED via RAG — not open pretrain |
| Units | UCUM |
| Graph reasoning | OpenAlex, open KGs, internal joins via ID maps |

---

## Relationship to other docs

| Doc | Role |
|-----|------|
| **This file (`02-data-sources.md`)** | **Master catalog** — what exists and why |
| `10-data-sources-runbook.md` | How we download/operate sources already implemented |
| `09-extraction-contract.md` | Schema/ops for literature-like extracts |
| `12-source-inventory.md` | Live per-source inventory: which sources are wired, at which stages |
| `13-graphrag-design.md` | SFT model and deterministic reference lookup as built: routing by claim type, lookups, rule store, harness, PBRER v1 |
| `superpowers/specs/2026-09-02-phase0-data-roadmap.md` | Suggested sequencing; **subordinate** to this catalog |

When a new source is added, update **this file first**, then runbook + extractor.

---

## Document control

| Version | Date | Notes |
|---------|------|-------|
| Aug 2026 | Prior | Original Stream 1 / 2 architecture notes |
| 2026-09-02 | Enrichment | Full catalog; induction status; DME/IME/UCUM; pre-SSD backlog; licensed vs open clarified; Swiss-Prot not sole focus |
| 2026-09-25 | CDISC standards | Added §2.6 (Controlled Terminology, Biomedical Concepts, USDM, SDTM) with per-item licence status; backlog and red-list rows |
| 2026-09-29 | SP7 | CDISC Controlled Terminology wired as `cdisc_ct` (download, serialize, load, per-package retire); §2.6 and backlog rows updated to done |
| 2026-09-29 | SP8 | USDM wired as `cdisc_usdm` (download-only, latest release's `Deliverables/`); §2.6 and backlog rows updated |
| 2026-11-03 | Regulatory sources | Added §2.2.1: DailyMed, the EMA feeds and EPARs, openFDA `drug/*`, FAERS and the FDA substance data, with the licence posture and the EMA credit |
| 2026-11-09 | SFT and reference lookup | Added 2.7 (licence classes of new sources; guideline and ICSR packages with page and date read), 2.8 (model inputs), Purple Book and AdComm rows in 2.2, the licensed-vocabulary rule in 2.4; MedMCQA and MedQA marked use with caution |
| 2026-11-09 | Reference documents | Three rows in 2.2.1: `ich`, `meddra_ptc`, `ema_guidance` (download-only; files and a manifest, no database row); scope of the MedDRA documents (Points to Consider only) |
| 2026-11-10 | SFT and reference lookup, as built | ICH class confirmed by the owner (2026-11-09) in 2.2.1 and 2.7.1; ICH E3 moved from use with caution to use freely, with attribution; new 2.7.2 (`LOOKUP_SOURCE_SUBSETS`, the `pbrer_skeleton` label, open owner questions on lookup facts in tool examples); cross-reference to `13-graphrag-design.md` |
