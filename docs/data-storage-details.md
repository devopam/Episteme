# Episteme Data Storage Layout

**Root:** `/EpistemeData/`  
**Last updated:** August 2026

This document defines the canonical folder structure for all Phase 0 data assets.

```bash
/EpistemeData/                          # Root of the file storage
│
├── 00_meta/                            # Tracking, licenses, inventories
│   ├── licenses/
│   ├── inventories/                    # CSV/JSON of what was downloaded
│   ├── checksums/
│   └── notes/
│
├── 01_raw/                             # Untouched original downloads
│   ├── pubmed/
│   │   ├── baseline/
│   │   └── updates/
│   ├── pmc/
│   │   ├── oa_comm/                    # Commercial-use only (train on this)
│   │   └── oa_noncomm/                 # Kept separate (do not train on)
│   ├── europepmc/
│   ├── multilingual/
│   │   ├── mmedc/
│   │   │   ├── raw/
│   │   │   └── metadata/
│   │   └── apollo/
│   │       ├── raw/
│   │       └── metadata/
│   ├── guidelines/
│   │   ├── meditron/
│   │   └── who/
│   └── keyvaluedatasets/               # Structured DBs (ChEMBL, UniProt, etc.)
│       ├── chembl/
│       ├── pubchem/
│       ├── uniprot/
│       └── ...
│
├── 02_processed/                       # Cleaned / serialized / filtered
│   ├── pubmed/
│   ├── pmc_comm/
│   ├── multilingual/
│   │   ├── mmedc/
│   │   ├── apollo/
│   │   └── combined/                   # Optional merged view
│   ├── guidelines/
│   └── secondary_serialized/           # Text versions of structured DBs
│
├── 03_corpus/                          # Final training-ready corpora
│   ├── pretrain/
│   │   ├── pretrain_corpus.jsonl
│   │   ├── pretrain_corpus_dedup.jsonl
│   │   └── pretrain_corpus_clean.jsonl
│   ├── sft/
│   └── preference/
│
├── 04_evals/                           # Benchmarks & decontamination sets
│   ├── medqa/
│   ├── medmcqa/
│   ├── pubmedqa/
│   └── custom/
│
├── 05_models/                          # Checkpoints (or symlinks to cloud)
│   ├── tiny/
│   ├── 8b/
│   └── experiments/
│
└── 99_tmp/                             # Temporary / scratch space
