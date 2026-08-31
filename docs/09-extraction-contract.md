# Episteme — Extraction Contract (v1.1)

**Status:** Active draft (post sample audit 2026-08-31)  
**Scope:** Phase 0 open-literature extraction into a single analytical / training table  
**Principles:** Restartability first · sample-before-scale · Iceberg + OpenMetadata from day one · license-safe subsets · transparency over silent drops

**Changelog (v1 → v1.1):** Incorporates findings from `analyze_source_samples.py` audit across PubMed, PMC `oa_comm`, EPMC preprints, author manuscripts, Apollo, lite metadata, and ID mappings. Adds PMC provenance columns, license normalisation rules, stricter `extract_status` guidance, and documents corrupt ID-mapping file as an operational blocker.

---

## 1. Goals

| Goal | Implication |
|------|-------------|
| One logical article row | Stable ID, source, license, text, key metadata in a single schema across sources |
| Stream; do not load all RAM | Streaming parsers (e.g. iterparse); chunked writers; bounded concurrency on laptop |
| Restartable | Per **input file** checkpoints; failed units re-runnable without redoing successes |
| Train + RAG ready | Flat `text` for pretrain; structured columns for filters, analytics, and KG; validate on a **sample subset** before full runs |
| Iceberg + OpenMetadata from the beginning | No intermediate “JSONL-only source of truth”; Parquet/Iceberg is the processed layer of record |
| License-safe | Explicit normalised `license` + `subset`; commercial training paths filter on these only |
| Failure visibility | Structured failure logs; row-level `extract_status`; retain sparse/empty source rows when policy requires transparency |

We optimise for **low rework later**, not for the fastest first metric.

---

## 2. Restart & failure model (mandatory)

### 2.1 Unit of work

- **Unit of work** = one **input file** (example: one `pubmed26n0123.xml.gz`, one PMC article object, one Apollo shard).
- A unit is **successful** only when:
  1. The file was fully read/parsed, and
  2. Resulting rows were **committed** to the Iceberg table, and
  3. A **success marker** was written.
- A unit is **failed** if any of the above does not complete. No success marker is written. Other units continue.

There is no “partial success” for a unit: either full commit + marker, or retry from scratch for that file (idempotent).

### 2.2 Ops layout

```text
02_processed/_ops/<source>/
  success/
    <input_file_basename>.ok          # marker; may contain JSON stats
  failed/
    <input_file_basename>.json        # error class, message, stats, run id
  row_issues/
    <input_file_basename>.jsonl       # optional per-record issues
  runs/
    run_<UTC_timestamp>.json          # run config, version, totals
```

### 2.3 Retry behaviour

1. Enumerate input files from `01_raw/...`.
2. Skip any input that has a valid success marker (unless `--force`).
3. Process only missing or previously failed inputs.
4. On retry of the same `source_file`, **replace** prior rows for that `source_file` (delete-by-`source_file` then append, or equivalent Iceberg strategy).

### 2.4 Failure classes

| Class | Meaning | Typical follow-up |
|-------|---------|-------------------|
| `parse_error` | Stream/XML/JSON structure broke | Parser fix or quarantine file |
| `io_error` | Read/write/network/disk | Retry |
| `schema_violation` | Hard schema policy failed | Mapper or policy change |
| `empty_record` | ID present but no usable text fields | **Retain** with `extract_status=empty` |
| `corrupt_source` | Input file is not valid data (e.g. error log shipped as `.csv.gz`) | Re-download; quarantine |
| `unknown` | Unclassified | Inspect `failed/*.json` |

### 2.5 Row-level status

| Field | Values | Role |
|-------|--------|------|
| `extract_status` | `ok` \| `empty` \| `partial` \| `dropped` | Quality / usability |
| `extract_notes` | optional short codes | Why partial/empty/dropped |

**Policy defaults (v1.1):**

| Status | When |
|--------|------|
| `ok` | Normalised `text` present **and** (`len(text) ≥ 200` **or** non-empty `abstract` **or** non-empty `body_text`) |
| `partial` | Some content (e.g. title only, or `text` shorter than threshold) but identifiable |
| `empty` | Identifier present; no title/abstract/body |
| `dropped` | Rare; explicit drop rule only; always counted in run stats |

**Rationale:** Sample audit showed historical PubMed records with title-only `text_len` ~60–120 marked `ok`; for training quality those should be `partial`.

Training / commercial materialisation:

```text
extract_status = 'ok'
AND subset = 'commercial'    -- when commercial-only corpora are required
```

---

## 3. Canonical schema (v1.1)

Logical table: **`episteme.articles`**

### 3.1 Core columns

| Column | Type | Required | Description |
|--------|------|----------|-------------|
| `id` | string | yes | Canonical ID: `pmid:<n>`, `pmcid:PMC<n>`, `doi:<…>`, `apollo:<…>`, etc. |
| `source` | string | yes | `pubmed` \| `pmc_oa_comm` \| `epmc_preprint` \| `epmc_manuscript` \| `apollo` \| … |
| `source_file` | string | yes | Input file basename (unit of work key) |
| `source_record_id` | string | no | Native id inside the file |
| `pmid` | string | no | PubMed ID |
| `pmcid` | string | no | PMC ID (`PMC…`) |
| `doi` | string | no | DOI |
| `title` | string | no | Article title |
| `abstract` | string | no | Abstract |
| `body_text` | string | no | Full body when available |
| `text` | string | no | Normalised train/RAG field (see §5) |
| `authors` | list\<string\> | no | Author display names |
| `journal` | string | no | Journal or venue |
| `year` | int | no | Publication year; partition sentinel when unknown (see §4) |
| `mesh` | list\<string\> | no | MeSH descriptors |
| `publication_types` | list\<string\> | no | Publication types |
| `language` | string | no | Language code when known (critical for Apollo / multilingual) |
| `license` | string | no | **Normalised** label: `CC0`, `CC BY`, `CC BY-SA`, `CC BY-ND`, `CC BY-NC`, `CC BY-NC-SA`, `CC BY-NC-ND`, `text_mining`, `unknown`, … |
| `license_url` | string | no | License URL when source provides one (common on preprints) |
| `license_raw` | string | no | Optional short raw license snippet for audit |
| `subset` | string | yes | `commercial` \| `text_mining` \| `open_metadata` \| `other` |
| `is_retracted` | boolean | yes | Default `false` when unknown |
| `extract_status` | string | yes | `ok` \| `empty` \| `partial` \| `dropped` |
| `extract_notes` | string | no | Short reason codes |
| `retrieved_at` | timestamp | yes | Extract/commit time (UTC) |
| `content_hash` | string | no | Hash of normalised `text` for dedup |

### 3.2 Columns added from sample audit (v1.1)

| Column | Type | Required | Description |
|--------|------|----------|-------------|
| `pmc_version` | string | no | PMC article version / `mid` (e.g. `1` or full `PMC….1`) |
| `is_manuscript` | boolean | no | From PMC metadata; default unknown/null |
| `is_historical_ocr` | boolean | no | OCR quality flag from PMC metadata |
| `pdf_url` | string | no | URL to PDF when available |

**Deferred (not first-class columns yet):** `media_urls`, `keywords`, `preprint_server`, free-form `source_meta` map — revisit if extractors surface them at scale.

### 3.3 Identifier rules

- Prefer explicit prefixes so IDs never collide across sources.
- Populate `pmid` / `pmcid` / `doi` whenever present; `id` is the row’s primary logical key **within source conventions**.
- Cross-source dedup is a **later** corpus job, not the extractors’ job.
- Sparse sources (e.g. Apollo text-only) remain valid rows; do not drop because bibliographic fields are null.

### 3.4 Sample-audit coverage summary

| Area | Observation |
|------|-------------|
| Core IDs + `text` | Present for PubMed, PMC OA, preprints, manuscripts, Apollo |
| `authors` | Reliable on PubMed samples; **under-parsed** on JATS (PMC/preprint/manuscript) — extractor gap, not schema gap |
| `abstract` | Often absent on historical PubMed and PMC JSON-only path; present on preprints/manuscripts |
| `mesh` / `publication_types` | PubMed-native; null elsewhere is expected |
| `language` | PubMed samples OK; must be filled for Apollo when corpus provides it |
| PMC extras | `version`, `is_manuscript`, `is_historical_ocr`, `pdf_url` justified as columns |
| License | Normalise codes; keep URL/raw separately for preprints |
| Lite metadata / ID map | **Enrichment only**; not primary `articles` text sources |

---

## 4. Iceberg & OpenMetadata

| Item | v1.1 choice |
|------|-------------|
| Table | `episteme.articles` |
| Format | Apache Iceberg, Parquet data files |
| Partitioning | `source` + `year` (`year` missing → `0`) |
| Write granularity | Commit per successful **input file** |
| Idempotent retry | On reprocess of `source_file`, remove existing rows for that `source_file` then append |

```text
02_processed/
  warehouse/
    episteme/articles/
  _ops/
    pubmed/
    pmc_oa_comm/
    ...
```

OpenMetadata: register table + columns on first successful sample load; tag license/subset columns; lineage from `01_raw/<source>` → extract job → table.

**Related tables (not `articles`):**

| Table | Role |
|-------|------|
| `episteme.id_map` | PMID ↔ PMCID ↔ DOI (from Europe PMC mappings file — **only after file integrity check**) |

---

## 5. Text normalisation (v1.1)

1. Prefer `title` + `\n\n` + `abstract` when body is absent (typical PubMed).
2. When `body_text` exists: `title` + `\n\n` + `abstract` + `\n\n` + `body_text` (omit empty parts).
3. Collapse excessive whitespace; strip null controls; UTF-8.
4. Apply `extract_status` rules in §2.5.
5. `content_hash` = hash of normalised `text` (algorithm documented in implementation, e.g. SHA-256 hex).

---

## 6. License & subset assignment (extract-time)

### 6.1 Normalisation

| Pattern | `license` | Typical `subset` |
|---------|-----------|------------------|
| CC0 | `CC0` | `commercial` |
| CC BY (no NC) | `CC BY` | `commercial` |
| CC BY-SA | `CC BY-SA` | `commercial` |
| CC BY-ND | `CC BY-ND` | `commercial` |
| CC BY-NC* | `CC BY-NC` / variants | `text_mining` |
| Explicit text-mining / fair-use manuscript notices | `text_mining` | `text_mining` |
| Unknown / NLM citation redistribution | `unknown` | `open_metadata` (PubMed) |

Store URL in `license_url` when present. Optionally store truncated prose in `license_raw`.

### 6.2 Source defaults

| Source | Default `subset` | Notes |
|--------|------------------|-------|
| PubMed | `open_metadata` | Not commercial full text |
| PMC `oa_comm` | `commercial` | Verify per-article `license_code` |
| EPMC preprints | Derived from license | NC → `text_mining`; BY/CC0 → `commercial` |
| Author manuscripts | `text_mining` | Do not mix into commercial training by default |
| Apollo | `other` until corpus license diligence completes | Then tighten |

Commercial path:

```text
subset = 'commercial' AND extract_status = 'ok'
```

---

## 7. Source priority for implementation

| Order | Source | Notes |
|-------|--------|-------|
| 1 | PubMed XML | Complete on disk; establishes ops patterns |
| 2 | ApolloCorpus | Multilingual; sparse metadata expected |
| 3 | PMC `oa_comm` | Full text; commercial; scale on SSD |
| 4 | EPMC preprints | Full-text ranges |
| 5 | Author manuscripts | `text_mining` |
| 6 | EPMC lite metadata | Enrichment only — after clean download |
| — | ID mappings | Separate `id_map` table after **re-download** |

---

## 8. Sample-before-scale gate

1. Run extraction on a small sample (one PubMed baseline file; existing PMC articles).
2. Prefer **recent** PubMed files for abstract coverage checks (early PMIDs are often title-only).
3. Review counts by `extract_status`, null rates, license/subset distribution, failure logs.
4. Register/update OpenMetadata.
5. Scale out (still per-file restartable).

Audit tooling: `scripts/data/analyze_source_samples.py`.

---

## 9. Known issues from 2026-08-31 audit (must not forget)

| Issue | Severity | Action |
|-------|----------|--------|
| `PMID_PMCID_DOI.csv.gz` contains Oracle error text, not mappings | **High** | Re-download; validate header before any join use |
| EPMC lite metadata `tgz` incomplete / unparsed | Medium | Resume download; re-run sample audit |
| JATS authors / journal under-extracted | Medium | Improve XML mapping in extractors (schema already allows fields) |
| Apollo language/title not surfaced in samples | Medium | Inspect raw `*_text.json` structure; fix parser |
| Historical PubMed title-only marked `ok` in audit script | Low | Production extractor applies §2.5 thresholds |

---

## 10. Pipeline entrypoints

```text
scripts/data/extract_<source>.sh
scripts/data/analyze_source_samples.py
src/episteme/data/schema.py
src/episteme/data/<source>/extract.py
src/episteme/data/ops.py
```

---

## 11. Non-goals (v1.1)

- Cross-source deduplication into a single “best” article
- Full knowledge-graph materialisation
- Perfect multi-jurisdiction license interpretation (stay conservative for `commercial`)
- Loading entire sources into memory

---

## 12. Open questions

1. Exact Apollo `subset` after commercial-use diligence on corpus terms.
2. Iceberg catalog choice (laptop vs cloud).
3. Whether `source_file` is basename-only (preferred) or relative path under `01_raw`.
4. Final min-length threshold for `ok` after a larger PubMed sample (default 200 chars).

---

## 13. Document control

| Version | Date | Notes |
|---------|------|-------|
| v1 | 2026-08-31 | Initial contract |
| v1.1 | 2026-08-31 | Sample audit incorporation; PMC columns; status/license rules; known corrupt ID map |
