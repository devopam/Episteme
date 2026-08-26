```markdown
# Multilingual Medical Corpora – Acquisition Principles

**Last updated:** August 2026  
**Status:** Principles defined – download & extraction scripts pending

## 1. Objective

Provide the model with strong medical language capability beyond English, covering major languages present in high-quality open medical corpora (especially Chinese, Spanish, French, and others available in the selected sources).

## 2. Target Sources (Phase 0)

| Source | Description | Primary Access |
|--------|-------------|----------------|
| **MMedC** | Large multilingual medical corpus | Hugging Face |
| **ApolloCorpora** | Multilingual medical books, papers, and QA-style data | Hugging Face |

## 3. Scope Decisions

| Item | Decision |
|------|----------|
| Goal | Multilingual medical language modeling & reasoning support |
| License stance | Use only subsets that clearly permit research / commercial use. Record license per source. |
| Language policy | Preserve original language. Attach language tags. Do not translate everything into English. |
| Format target | Clean text normalized into consistent JSONL |
| Quality | Basic cleaning + language ID + later deduplication |
| Relation to English data | Complementary to PubMed / PMC |

## 4. Folder Mapping

Raw:
```bash
01_raw/multilingual/
├── mmedc/
│   ├── raw/
│   └── metadata/
└── apollo/
    ├── raw/
    └── metadata/
