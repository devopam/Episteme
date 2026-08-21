# Data Sources for Phase 0 – Episteme LLM Architecture

**Last updated:** August 2026

## Overview
A generic, open medical Large Language Model demands a bimodal data strategy that strictly segregates stable foundational knowledge from dynamic, proprietary data[cite: 8]. This document outlines the distinct data pipelines for the LLM's parametric memory (Stream 1) and the relational database Retrieval-Augmented Generation layer (Stream 2). Datasets bearing Non-Commercial (NC) restrictions, requiring proprietary enterprise licensing, or containing real patient clinical records are systematically excluded or tightly access-controlled[cite: 8].

---

## Stream 1: LLM Pre-training & SFT (Parametric Memory)
This stream is dedicated exclusively to open, commercially viable, and relatively stable foundational knowledge. The objective is to teach the model biomedical syntax, diagnostic reasoning pathways, fundamental chemistry concepts, and domain terminology without memorizing volatile specifics.

### 1. Foundational Biomedical Literature
*   **PubMed Baseline (Abstracts & Metadata):** Provides the core scientific language foundation[cite: 8]. Available via FTP (`ftp://ftp.ncbi.nlm.nih.gov/pubmed/baseline/`)[cite: 8].
*   **PMC Open Access Subset (Commercial Use):** Full-text articles restricted strictly to CC0, CC BY, CC BY-SA, and CC BY-ND licenses[cite: 8]. Bulk retrieved via AWS RODA or PMC FTP (`https://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_bulk/`)[cite: 8].
*   **OpenAlex Biomedical Knowledge Graph:** Massive open knowledge graph of scholarly metadata and open-access links (CC0 license)[cite: 8]. Retrieve via Amazon S3 (`s3://openalex`)[cite: 8].
*   **PubChem (Text Serialization):** Public domain chemical taxonomy[cite: 8]. Rather than injecting the raw databases, serialize PubChem's structured JSON/XML records into declarative natural language sentences to teach fundamental chemical constraints safely.

### 2. Clinical Guidelines & Educational Pedagogy
*   **EPFL Meditron Guidelines Corpus:** Over 35,000 cleaned clinical practice guidelines from major health organizations (WHO, CDC, NICE)[cite: 8]. Download via Hugging Face (`epfl-llm/guidelines`)[cite: 8].
*   **OpenMedText:** 121,489 MDPI journal articles (CC BY 4.0) and 29 open-source textbooks[cite: 8]. *Note: Subdirectories with NC licenses must be programmatically excluded.*

### 3. Multilingual & Synthetic Clinical Corpora
*   **MMedC (Multilingual Medical Corpus):** ~25.5 billion tokens across English, French, Chinese, and Spanish[cite: 8]. Hosted on Hugging Face (`Henrychur/MMedC`)[cite: 8].
*   **ApolloCorpora:** Multilingual books, papers, and QA datasets[cite: 8]. Hosted on Hugging Face (`FreedomIntelligence/ApolloCorpus`)[cite: 8].
*   **PARHAF / PARCOMED:** Thousands of French clinical reports describing strictly fictitious, synthetic patients to bypass privacy regulations (CC BY 4.0 / Etalab 2.0).

### 4. Supervised Fine-Tuning (SFT) Datasets
*   **MedMCQA:** 194,000 multiple-choice questions from medical entrance exams detailing the rationale of diagnostic deduction[cite: 8].
*   **Medprompt (CoT and ToT):** Chain-of-Thought and Tree-of-Thoughts reasoning architectures designed to teach step-by-step logical evaluation[cite: 8].
*   **PubMedQA:** Trains probabilistic reasoning by forcing binary or "maybe" responses to express clinical uncertainty[cite: 8].
*   **mmlu-medical-MedGENIE:** Medical open-domain QA containing generated factual contexts[cite: 8].

---

## Stream 2: RAG & Knowledge Layer (Incremental Ingestion Pipelines)
This stream is strictly reserved for dynamic, highly specific, version-dependent, and heavily structured data. These sources are natively ingested into local relational databases (e.g., PostgreSQL / `MCPg`) and kept up-to-date via automated incremental pipelines.

### 1. Pharmacological & Biochemical Databases
These massive relational webs belong in the database, allowing the model to execute exact, deterministic SQL queries for binding affinities, targets, and molecular weights.
*   **ChEMBL:** 2.9 million bioactive compounds and 24.5 million bioactivity measurements.
    *   *Pipeline Strategy:* Periodic bulk PostgreSQL database dumps downloaded via the ChEMBL FTP site.
*   **SureChEMBL:** Chemical entities extracted from patent literature with mechanism mappings.
    *   *Pipeline Strategy:* Biweekly incremental Apache Parquet file updates.
*   **UniProt:** Over 245 million protein sequences (Swiss-Prot / TrEMBL).
    *   *Pipeline Strategy:* Full release updates occur roughly every eight weeks; fetch FASTA and XML subsets via `ftp.uniprot.org`.
*   **ClinVar:** Millions of human genetic variants and phenotypes.
    *   *Pipeline Strategy:* Regular XML and VCF updates via NCBI FTP.

### 2. Structured Clinical Trials & Regulatory Metadata
Regulatory frameworks and trial protocols are highly volatile and must be dynamically retrieved to prevent hallucinating outdated dosages or side effects.
*   **AACT (ClinicalTrials.gov Data):** A complete relational database of global clinical trials[cite: 8]. 
    *   *Pipeline Strategy:* Utilize `pg_restore` on the monthly/daily PostgreSQL dump updates provided directly by AACT (`https://aact.ctti-clinicaltrials.org/downloads`)[cite: 8].
*   **DailyMed (Structured Product Labeling - SPL):** The FDA's most recent labeling for prescription and non-prescription drugs.
    *   *Pipeline Strategy:* Implement an automated pipeline to ingest the Daily, Weekly, or Monthly ZIP file updates containing XML indexing and SPL files from the NLM's Download Data endpoint.
*   **openFDA:** Structured APIs for drug labeling, adverse events, product recalls, and the NDC Directory.
    *   *Pipeline Strategy:* Query JSON incremental endpoints directly to maintain a real-time cache of adverse event reports and FDA enforcement actions.

### 3. Proprietary Vocabularies & Ontologies (RBAC Protected)
These terminologies carry severe legal restrictions, requiring multi-tenant Role-Based Access Control (RBAC) mechanisms within the enterprise RAG layer to ensure compliance.
*   **SNOMED CT:** Requires specific affiliate licenses based on end-users.
*   **MedDRA:** Highly proprietary, requiring paid subscriptions for commercial pharmaceutical application.
*   **ICD / UCUM / RxNorm:** Managed dynamically for versioned clinical mapping.

---

## 3. Explicit Exclusions (The Red-List)
The following sources are **PROHIBITED** from both open streams due to insurmountable legal, privacy, or technical barriers[cite: 8]:
*   **Real Patient Clinical Notes (e.g., MIMIC-IV, eICU):** Require credentialed access and risk severe HIPAA/privacy violations[cite: 8].
*   **WHO ICTRP:** Prohibited due to explicit Non-Commercial clauses regarding data extraction[cite: 8].
*   **EU CTR & CTRI:** Excluded due to the lack of reliable, automated bulk download infrastructure[cite: 8].
*   **NICE Guidelines (Non-UK Use):** International commercial usage requires explicit licensing agreements and fees[cite: 8].
*   **BiMediX & ArSyra:** Disqualified due to CC BY-NC-SA 4.0 restrictions and enterprise commercial license fees.
