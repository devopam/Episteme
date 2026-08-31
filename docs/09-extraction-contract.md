# Episteme — Extraction Contract (v1)

**Status:** Draft for review  
**Scope:** Phase 0 open-literature extraction into a single analytical / training table  
**Principles:** Restartability first · sample-before-scale · Iceberg + OpenMetadata from day one · license-safe subsets · transparency over silent drops

---

## 1. Goals

| Goal | Implication |
|------|-------------|
| One logical article row | Stable ID, source, license, text, key metadata in a single schema across sources |
| Stream; do not load all RAM | Streaming parsers (e.g. iterparse); chunked writers; bounded concurrency on laptop |
| Restartable | Per **input file** checkpoints; failed units re-runnable without redoing successes |
| Train + RAG ready | Flat `text` for pretrain; structured columns for filters, analytics, and KG; validate on a **sample subset** before full runs |
| Iceberg + OpenMetadata from the beginning | No intermediate “JSONL-only source of truth”; Parquet/Iceberg is the processed layer of record |
| License-safe | Explicit `license` and `subset` columns; commercial training paths filter on these only |
| Failure visibility | Structured failure logs; row-level `extract_status`; retain sparse/empty source rows when policy requires transparency |

We optimise for **low rework later**, not for the fastest first dashboard metric.

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

Under the processed ops area (example):

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
2. Skip any input that has a valid success marker (unless `--force` is set for that file/pattern).
3. Process only missing or previously failed inputs.
4. On retry of the same `source_file`, **replace** prior rows for that `source_file` in the table (delete-by-`source_file` then append, or equivalent Iceberg strategy) so commits stay idempotent.

### 2.4 Failure classes

| Class | Meaning | Typical follow-up |
|-------|---------|-------------------|
| `parse_error` | Stream/XML/JSON structure broke | Parser fix or quarantine file |
| `io_error` | Read/write/network/disk | Retry |
| `schema_violation` | Hard schema policy failed | Mapper or policy change |
| `empty_record` | Source provided ID (or shell) but no usable text fields | **Retain** row with `extract_status=empty` when ID exists |
| `unknown` | Unclassified | Inspect `failed/*.json` |

Pipeline refinement (code/policy) and **known gaps** (unfixable source emptiness) are both first-class outcomes of failure analysis. Known gaps are documented, counted, and retained where transparency requires it—not silently deleted.

### 2.5 Row-level status

Every output row carries:

| Field | Values | Role |
|-------|--------|------|
| `extract_status` | `ok` \| `empty` \| `partial` \| `dropped` | Quality / usability |
| `extract_notes` | optional short codes | Why partial/empty/dropped |

**Policy defaults:**

- **`ok`** — usable primary `text` (after normalisation rules).
- **`empty`** — identifier present; title/abstract/body all missing or whitespace; **retain** for transparency.
- **`partial`** — some content (e.g. title only) but below full `ok` bar.
- **`dropped`** — rare; only when an explicit drop rule applies; always counted in run stats.

Training and commercial RAG materialisation should filter:

```text
extract_status = 'ok'
AND subset = 'commercial'   -- when commercial-only corpora are required
```

Analytics and data-quality reports may use the full table including `empty` / `partial`.

---

## 3. Canonical schema (v1)

Logical table: **`episteme.articles`** (final physical name may follow warehouse conventions).

### 3.1 Columns

| Column | Type | Required | Description |
|--------|------|----------|-------------|
| `id` | string | yes | Canonical ID: `pmid:<n>`, `pmcid:PMC<n>`, `ppr:<n>`, `apollo:<…>`, etc. |
| `source` | string | yes | Provenance system: `pubmed`, `pmc_oa_comm`, `epmc_preprint`, `epmc_manuscript`, `apollo`, … |
| `source_file` | string | yes | Input file path or stable basename used as unit of work |
| `source_record_id` | string | no | Native record id inside the file, if any |
| `pmid` | string | no | PubMed ID when known |
| `pmcid` | string | no | PMC ID when known |
| `doi` | string | no | DOI when known |
| `title` | string | no | Article title |
| `abstract` | string | no | Abstract text |
| `body_text` | string | no | Full body when available |
| `text` | string | no | Normalised field for train/RAG (see §5) |
| `authors` | list\<string\> | no | Author names (display form) |
| `journal` | string | no | Journal or venue |
| `year` | int | no | Publication year; use partition sentinel when unknown (see §4) |
| `mesh` | list\<string\> | no | MeSH descriptors when present |
| `publication_types` | list\<string\> | no | Publication types when present |
| `language` | string | no | ISO-ish language code when known |
| `license` | string | no | Machine-oriented license label (`CC0`, `CC BY`, `unknown`, …) |
| `subset` | string | yes | Rights cohort: `commercial` \| `text_mining` \| `open_metadata` \| `other` |
| `is_retracted` | boolean | yes | Default `false` when unknown |
| `extract_status` | string | yes | `ok` \| `empty` \| `partial` \| `dropped` |
| `extract_notes` | string | no | Short reason codes |
| `retrieved_at` | timestamp | yes | Extract/commit time (UTC) |
| `content_hash` | string | no | Hash of normalised `text` (or title+abstract) for dedup |

### 3.2 Identifier rules

- Prefer explicit prefixes so IDs never collide across sources.
- When multiple IDs exist, still populate `pmid` / `pmcid` / `doi` columns; `id` remains the primary key for the row **within a source convention** (dedup across sources is a later corpus job).
- Empty or missing source IDs: if the record cannot be identified at all, log a row issue; drop only under explicit policy.

---

## 4. Iceberg & OpenMetadata

### 4.1 Table contract

| Item | v1 choice |
|------|-----------|
| Table | `episteme.articles` |
| Format | Apache Iceberg, Parquet data files |
| Partitioning | `source` + `year` (`year` missing → `0` or agreed sentinel) |
| Write granularity | Commit per successful **input file** |
| Idempotent retry | On reprocess of `source_file`, remove existing rows for that `source_file` then append |

### 4.2 Warehouse layout (illustrative)

```text
02_processed/
  warehouse/                          # Iceberg warehouse root
    episteme/
      articles/                       # table
  _ops/
    pubmed/
    pmc_oa_comm/
    epmc_preprint/
    ...
```

Exact warehouse path is fixed in implementation config; this doc only requires **Iceberg as the processed source of truth**.

### 4.3 OpenMetadata

On first successful sample load, register at least:

- Table `episteme.articles`
- Column descriptions (from this contract)
- Tags/classification: license-related columns, `subset`, “no personal data intent” on bulk literature text
- Lineage: `01_raw/<source>` → extract job → `episteme.articles`

---

## 5. Text normalisation (v1)

**`text` construction (default):**

1. Prefer `title` + `\n\n` + `abstract` when body is absent (typical PubMed).
2. When `body_text` exists, use `title` + `\n\n` + `abstract` + `\n\n` + `body_text` (trim empty parts).
3. Collapse excessive whitespace; strip null controls; UTF-8.
4. If the result is empty → `extract_status=empty` (when an ID exists) or `dropped` only under explicit rule.

Refinements (language filtering, min length for `ok`) may be tightened after sample diligence without changing column names.

---

## 6. License & subset assignment (extract-time)

Assignment happens **during extraction**, not in an ad-hoc post-pass.

| Source | Default `subset` | `license` guidance |
|--------|------------------|--------------------|
| PubMed (MEDLINE/citation XML) | `open_metadata` | `unknown` or documented NLM redistribution terms as applicable |
| PMC Commercial OA (`oa_comm`) | `commercial` | From article metadata (`CC0`, `CC BY`, `CC BY-SA`, `CC BY-ND`, …) |
| Europe PMC OA preprints | Prefer per-record; else conservative | From XML/metadata when present; else `unknown` |
| Author manuscripts | `text_mining` | Text-mining / applicable copyright; not mixed into commercial by default |
| ApolloCorpus | Per project license decision | Documented corpus license (e.g. Apache-2.0 for corpus packaging—confirm per file/terms) |

**Commercial training / commercial product path:**

```text
subset = 'commercial' AND extract_status = 'ok'
```

**Text-mining / research path** may include `text_mining` under separate policy review.

---

## 7. Source priority for implementation

| Order | Source | Notes |
|-------|--------|-------|
| 1 | PubMed XML | Complete raw data on disk; establishes parser + ops patterns |
| 2 | ApolloCorpus | Multilingual; structured inputs |
| 3 | PMC `oa_comm` | Full text; commercial; scale on SSD |
| 4 | EPMC preprints | Full-text ranges |
| 5 | Author manuscripts | After raw download completes; `text_mining` |
| 6 | EPMC lite metadata | Enrichment / joins—not primary `text` |

---

## 8. Sample-before-scale gate

Before a full source sweep:

1. Run extraction on a **small sample** (e.g. one PubMed baseline file; existing PMC sample articles).
2. Review:
   - Counts by `extract_status`
   - Null rates on key columns
   - Spot-check `text` quality
   - `license` / `subset` distribution
   - Failure logs (should be empty or understood)
3. Register/update OpenMetadata.
4. Only then scale to all input files (still per-file restartable).

---

## 9. Pipeline entrypoints (implementation later)

```text
scripts/data/extract_<source>.sh          # thin CLI, logging, paths
src/episteme/data/schema.py               # shared constants / validation helpers
src/episteme/data/<source>/extract.py     # streaming extract + Iceberg commit
src/episteme/data/ops.py                  # success/failed markers, run manifests
```

Shell entrypoints call Python modules; no heavy logic in shell beyond args and environment.

---

## 10. Non-goals (v1)

- Cross-source deduplication into a single “best” article (later corpus job).
- Full knowledge-graph materialisation (separate track; may consume this table).
- Perfect license interpretation for every edge-case jurisdiction (document unknowns; stay conservative for `commercial`).
- Loading entire sources into memory for “simpler” code.

---

## 11. Open questions (resolve during review)

1. Exact string for PubMed `subset` (`open_metadata` vs `open_abstracts`).
2. Partition sentinel for unknown `year` (`0` vs `null` + Iceberg partitioning behaviour).
3. Whether `source_file` is stored as basename only or path relative to `01_raw`.
4. Minimum character length for `extract_status=ok` after sample inspection.
5. Iceberg catalog choice (Hadoop / Nessie / REST / Glue-compatible) for laptop vs cloud.

---

## 12. Document control

| Version | Date | Notes |
|---------|------|-------|
| v1 | 2026-08-31 | Initial contract for review before coding |

Edits to this document should precede schema-breaking code changes.
