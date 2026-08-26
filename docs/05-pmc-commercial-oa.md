# PMC Commercial Open Access Subset – Acquisition Principles

**Last updated:** August 2026  
**Status:** Principles defined – download scripts pending

## 1. Objective

Complement the PubMed abstracts stream with high-quality, full-text articles that are clearly licensed for commercial reuse. This forms a core part of the Phase 0 training corpus for Episteme and can later also support RAG.

## 2. Scope Decision

| Item | Decision | Reason |
|------|----------|--------|
| **Subset** | Only `oa_comm` (Commercial Use Allowed) | Contains CC0, CC BY, CC BY-SA, CC BY-ND licenses – suitable for commercial model training |
| **Excluded** | `oa_noncomm`, most of `oa_other` | Non-commercial licenses are not acceptable for our use case |
| **Preferred formats** | XML (first choice), plain TXT (second) | Cleanest signal for language model training |
| **PDFs** | Optional / lower priority | Higher storage cost and noisier text extraction |
| **Coverage** | Full `oa_comm` subset (not limited by year) | We want broad foundational full-text knowledge |

## 3. Key Principles

- Stay strictly inside the Commercial Use Allowed boundary.
- Prefer structured full text (JATS XML or clean plain text) over PDFs.
- Keep raw downloads clearly separated from processed/training-ready data.
- Record provenance and license information.
- Design for both continued pre-training and future use as a high-quality retrieval corpus.
- Prefer the official AWS Open Data distribution (`s3://pmc-oa-opendata`) as the primary source in 2026, with FTP as fallback while still available.

## 4. Recommended Folder Structure

```bash
/EpistemeData/
└── 01_raw/
    └── pmc/
        ├── oa_comm/                          # Commercial Use Allowed only
        │   ├── xml/                          # Preferred full-text format
        │   ├── txt/                          # Plain text version
        │   ├── pdf/                          # Optional
        │   ├── metadata/                     # File lists, inventories, license notes
        │   └── README_pmc_oa_comm.txt
        │
        └── oa_noncomm/                       # Do not populate for training
