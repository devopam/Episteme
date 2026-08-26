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
│   │   ├── oa_comm/                    # Commercial-use only
│   │   └── oa_noncomm/                 # Kept separate (do not train on)
│   ├── europepmc/
│   ├── multilingual/
│   │   ├── mmedc/
│   │   └── apollo/
│   ├── guidelines/
│   │   ├── meditron/
│   │   └── who/
│   └── keyvaluedatasets/                      # ChEMBL, UniProt, ClinVar, etc.
│       ├── chembl/
│       ├── pubchem/
│       ├── uniprot/
│       └── ...
│
├── 02_processed/                       # Cleaned / serialized / filtered
│   ├── pubmed/
│   ├── pmc_comm/
│   ├── multilingual/
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
