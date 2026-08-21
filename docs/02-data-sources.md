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

## Explicitly Excluded
- Current full MedDRA / UCUM / ICD / SNOMED releases
- Product labels / SmPCs
- Non-commercial-only PMC content
- Real patient-level clinical notes
- Proprietary or customer data
