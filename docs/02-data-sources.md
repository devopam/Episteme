# Data Sources for Phase 0 – Generic Open Medical LLM

## Priority Sources

| Priority | Source                              | Languages / Regions          | Content Type              | Bulk Access          | Notes |
|----------|-------------------------------------|------------------------------|---------------------------|----------------------|-------|
| Core     | PubMed Abstracts                    | Global (English-heavy)       | Abstracts + metadata      | Excellent            | Must-have |
| Core     | PMC Open Access – Commercial Use    | Global (English-heavy)       | Full text                 | Excellent (FTP/AWS)  | Commercial-use only |
| Core     | Europe PMC Open Access              | Stronger European coverage   | Full text + preprints     | Excellent            | Good complement |
| High     | MMedC                               | EN, ZH, JA, FR, RU, ES       | Multilingual medical text | Available            | ~25.5B tokens |
| High     | ApolloCorpora                       | EN, ZH, FR, ES, AR, HI       | Books, papers, dialogues  | Public               | Designed for global reach |
| High     | WHO Guidelines + OpenWHO            | Multilingual                 | Guidelines + education    | Good                 | Strong LMIC / global value |
| High     | Meditron / EPFL Guidelines (public) | Multi-country                | Clinical guidelines       | Hugging Face         | Clean and ready |
| Medium   | SciELO / LILACS                     | Portuguese, Spanish, LatAm   | Full text                 | Partial              | Important for Latin America |
| Medium   | Language-specific open collections  | Chinese, Japanese, Arabic... | Papers, textbooks, exams  | Varies               | Prefer packaged corpora first |


## Overview
This document details the exact endpoints, bulk retrieval methods, and data formats for the foundational, generic open medical datasets required for Phase 0 of the Episteme LLM training process. These sources explicitly exclude proprietary company data, real patient-level clinical notes, and non-commercial literature.

---

## 1. Foundational Biomedical Literature
These sources form the bedrock of the model's domain language and scientific reasoning.

### PubMed Baseline (Abstracts & Metadata)
*   **Priority:** Core
*   **Content:** The 2026 production year PubMed Baseline consists of citation metadata and abstract text. 
*   **License:** Public Domain / Open (US Government Data).
*   **Access Protocol:** FTP (Anonymous)
*   **Endpoint:** `ftp://ftp.ncbi.nlm.nih.gov/pubmed/baseline/`
*   **Data Format:** Gzipped XML files (`.xml.gz`), conforming to the `pubmed_250101.dtd` document type definition.
*   **Retrieval Strategy:** Mirror the directory using automated tools. Note that the baseline must be downloaded and processed prior to loading any subsequent daily update files. Stream the XML parsing locally to avoid memory exhaustion.

### PMC Open Access Subset (Commercial Use Allowed)
*   **Priority:** Core
*   **Content:** Full-text biomedical journal articles with machine-readable Creative Commons licenses.
*   **License Target:** CC0, CC BY, CC BY-SA, and CC BY-ND (Explicitly Commercial Use Allowed).
*   **Access Protocol:** AWS RODA, PMC OAI service, PMC FTP service, or BioC API. Systemic retrieval through any other automated process is strictly prohibited.
*   **Endpoint (AWS Cloud):** Available as PMC Article Datasets hosted in the Registry of Open Data on Amazon Web Services (AWS) via HTTPS or S3 URL.
*   **Retrieval Strategy:** Utilize AWS CLI for bulk retrieval of the commercially licensed subsets to your external SSD.

### OpenAlex Biomedical Knowledge Graph
*   **Priority:** Core (New Addition)
*   **Content:** Massive open knowledge graph of scholarly metadata and open-access links, which can be filtered specifically for biomedical entities.
*   **License:** CC0.
*   **Access Protocol:** Amazon S3 (AWS Open Data Program covers data-transfer fees).
*   **Endpoint:** `s3://openalex`
*   **Data Format:** JSON Lines (under `data/jsonl/`) and Parquet (under `data/parquet/`).
*   **Retrieval Strategy:** You can use the AWS CLI with the `--no-sign-request` flag for anonymous access to sync the snapshot locally without an AWS account. 

---

## 2. High-Quality Clinical Guidelines
Guidelines instill authoritative, safe, and structured clinical reasoning.

### EPFL Meditron Clinical Guidelines Corpus
*   **Priority:** High
*   **Content:** Cleaned clinical practice guidelines from major global health organizations.
*   **License:** Redistributable / Open.
*   **Access Protocol:** Hugging Face Datasets
*   **Endpoint:** `epfl-llm/guidelines`
*   **Retrieval Strategy:** Download directly via the Hugging Face CLI: `huggingface-cli download --repo-type dataset epfl-llm/guidelines`

---

## 3. Structured Clinical Trial Data (PostgreSQL Ready)
*This section perfectly aligns with your local PostgreSQL `MCPg` RAG architecture.*

### AACT (Database for Aggregate Analysis of ClinicalTrials.gov)
*   **Priority:** High (New Addition)
*   **Content:** A complete, publicly available relational database containing all protocol and result data elements registered in ClinicalTrials.gov.
*   **License:** Open / Public Domain.
*   **Access Protocol:** Direct Download (Updated daily/monthly).
*   **Endpoint:** `https://aact.ctti-clinicaltrials.org/downloads`
*   **Data Format:** Available as a complete PostgreSQL database dump (e.g., `20260817_clinical_trials_ctgov.zip`, ~2.34 GB) or as pipe-delimited flat text files.
*   **Retrieval & Integration Strategy:** Download the PostgreSQL Database Dump and use `pg_restore` to create a complete local copy of the AACT database on your own PostgreSQL server. This gives your LLM instant, structured access to global clinical trials via your MCP setup.

---

## 4. Multilingual & Medical QA Corpora
These datasets expand the model's language footprint.

### MMedC (Multilingual Medical Corpus)
*   **Priority:** High
*   **Content:** Approximately 25.5 billion tokens across languages like English, French, and Chinese.
*   **License:** Open (Varies by subset).
*   **Access Protocol:** Hugging Face Datasets (`Henrychur/MMedC`).
*   **Retrieval Strategy:** Download the zip archive and stream the raw `.txt` files sequentially.

### ApolloCorpora
*   **Priority:** High
*   **Content:** Multilingual medical books, papers, dialogues, and QA datasets.
*   **Access Protocol:** Hugging Face Datasets (`FreedomIntelligence/ApolloCorpus`).

---

## 5. Supervised Fine-Tuning (SFT) Datasets
For Phase 0 Stage 2 (Instruct Tuning) and Stage 3 (Preference Optimization).

### mmlu-medical-MedGENIE
*   **Priority:** Medium (New Addition)
*   **Content:** Medical open-domain QA containing generated factual contexts.
*   **License:** Open.
*   **Access Protocol:** Hugging Face Datasets (`disi-unibo-nlp/mmlu-medical-MedGENIE`).
*   **Data Format:** Stored in parquet format.
*   **Retrieval Strategy:** Suitable for training the LLM to utilize generated contexts, which directly supports the behavior needed for your standard RAG pipeline.

---

## 6. Explicit Exclusions for Phase 0 (Red-List)
To ensure compliance and structural separation from the RAG layer, the following data types are strictly **PROHIBITED** from the foundational training pipeline:
*   **Real Patient Clinical Notes:** Under no circumstances should datasets like **MIMIC-IV** or **eICU** be used, as they contain raw patient records and require credentialed source access. 
*   **Proprietary Vocabularies:** Current full MedDRA, UCUM, ICD, and SNOMED releases (these belong in the multi-tenant RAG layer).
*   **Product labels and SmPCs.**
* Current full MedDRA / UCUM / ICD / SNOMED releases
* Product labels / SmPCs
* Non-commercial-only PMC content
* Real patient-level clinical notes
* Proprietary or customer datacontent.
