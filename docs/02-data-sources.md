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
| **Europe PMC preprints** | Full-text preprints | Per-article CC; filter NC | EBI FTP | **Done** (5 archives) |
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
| **MedMCQA** | Exam-style MCQ + rationales | Dataset license (check HF) | HF | **Not started** |
| **PubMedQA** | Yes/no/maybe + context | Open research norms / license on release | HF / official | **Not started** |
| **MedQA / USMLE-style open sets** | Clinical exam reasoning | Per release | HF | Candidate |
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

### 2.5 Knowledge-graph & graph-adjacent open sets

| Source | Role | Notes |
|--------|------|-------|
| **OpenAlex** | Works–authors–concepts graph | Also listed under Stream 1 metadata |
| **SemMedDB** (if still available / license OK) | Predications from PubMed | Historical; verify redistribution |
| **PrimeKG / open medical KGs** | Integrated disease–drug–gene graphs | Per project license on HF/Zenodo |
| **Hetionet** | Integrated network | Open | Candidate |
| **DrugBank open subsets** (if any remain open) | Drug targets | Much of DrugBank is licensed — audit |

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
| Later | MedDRA/WHODrug/SNOMED | **Only with customer license packs** | RBAC RAG connectors |

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
| `superpowers/specs/2026-09-02-phase0-data-roadmap.md` | Suggested sequencing; **subordinate** to this catalog |

When a new source is added, update **this file first**, then runbook + extractor.

---

## Document control

| Version | Date | Notes |
|---------|------|-------|
| Aug 2026 | Prior | Original Stream 1 / 2 architecture notes |
| 2026-09-02 | Enrichment | Full catalog; induction status; DME/IME/UCUM; pre-SSD backlog; licensed vs open clarified; Swiss-Prot not sole focus |
