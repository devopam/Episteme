# Episteme — Extraction Contract (v1.2)

**Status:** Active (v1.2 reconciled with the implemented Postgres pipeline, SP5, 2026-09-21)
**Scope:** Phase 0 open-literature extraction into a single analytical / training table
**Principles:** Restartability first · sample-before-scale · Postgres as the store of record (Iceberg superseded, see §4) · license-safe subsets · transparency over silent drops

**Changelog (v1.1 → v1.2, SP5):** Reconciled with the code. Storage is Postgres (`episteme.articles` + `episteme.article_body`), not Iceberg (ADR-0001, ADR-0002). Licence vocabulary gains `permissive` and `public_domain` (a governance override that `normalize_license` never returns). Adds the structured-source serialisation contract, the shared input-identity rule (`input_key`), and real entrypoint paths. Where this document and the code disagree, the code wins and the gap is named in §4.4 and §12. Audit obligations are in `docs/11-gxp-data-integrity.md`.

**Changelog (v1 → v1.1):** Incorporates findings from `analyze_source_samples.py` audit across PubMed, PMC `oa_comm`, EPMC preprints, author manuscripts, Apollo, lite metadata, and ID mappings. Adds PMC provenance columns, license normalisation rules, stricter `extract_status` guidance, and documents corrupt ID-mapping file as an operational blocker.

---

## 1. Goals

| Goal | Implication |
|------|-------------|
| One logical article row | Stable ID, source, license, text, key metadata in a single schema across sources |
| Stream; do not load all RAM | Streaming parsers (e.g. iterparse); chunked writers; bounded concurrency on laptop |
| Restartable | Per **input file** checkpoints; failed units re-runnable without redoing successes |
| Train + RAG ready | Flat `text` for pretrain; structured columns for filters, analytics, and KG; validate on a **sample subset** before full runs |
| Postgres store of record, OpenMetadata catalog | Staging Parquet shards are an intermediate hand-off, not the source of truth; `episteme.articles` / `episteme.article_body` in Postgres are the processed layer of record (ADR-0001; originally specified as Iceberg, superseded) |
| License-safe | Explicit normalised `license` + `subset`; commercial training paths filter on these only |
| Failure visibility | Structured failure logs; row-level `extract_status`; retain sparse/empty source rows when policy requires transparency |

We optimise for **low rework later**, not for the fastest first metric.

---

## 2. Restart & failure model (mandatory)

### 2.1 Unit of work

- **Unit of work** = one **input file** (example: one `pubmed26n0123.xml.gz`, one PMC article object, one Apollo shard).
- A unit is **successful** only when:
  1. The file was fully read/parsed, and
  2. Resulting rows were **committed** (extract: written to a staging Parquet/JSONL shard; load: loaded into Postgres, see §4), and
  3. A **success marker** was written.
- A unit is **failed** if any of the above does not complete. No success marker is written. Other units continue.

There is no “partial success” for a unit: either full commit + marker, or retry from scratch for that file (idempotent).

### 2.2 Ops layout

```text
02_processed/_ops/<source>/
  success/
    <input_key>.ok                    # extract/serialize marker; JSON with schema_version, source, source_file, completed_at, stats
  failed/
    <input_key>.json                  # error_class, message, stats, traceback tail
  load_success/<input_key>.ok         # written by the load stage
  graph_success/<input_key>.ok        # written by the graph stage
  runs/
    run_<run_id>.json                 # run config, schema version, totals
```

`<input_key>` is the input identity defined in §4.3. `row_issues/<…>.jsonl` was specified in v1.1 as optional; no code in `src/` writes it, so treat it as not implemented.

### 2.3 Retry behaviour

1. Enumerate input files from `01_raw/...`.
2. Skip any input that has a valid success marker (unless `--force`).
3. Process only missing or previously failed inputs.
4. On retry of the same `source_file`, **replace** prior rows for that `source_file`: the Postgres loader deletes by `source_file` (within the `source`) then bulk-loads, in one transaction (§4.2).

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
| `source` | string | yes | One of `article_schema.SOURCES`: `pubmed`, `pmc`, `bookshelf`, `europepmc_preprint`, `europepmc_manuscript`, `europepmc_lite`, `apollo`, `guidelines`, `chembl`, `uniprot`, `pubchem`, `clinvar`, `reactome`, `mesh`, `ontologies`, `openalex` (v1.1 used `pmc_oa_comm` / `epmc_*`; the code names differ) |
| `source_file` | string | yes | Input identity, the unit-of-work key (§4.3): the raw-dir-relative path joined with `__` (structured serializers), or the input basename (literature extractors) |
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
| `year` | int | no | Publication year; the loader coerces unknown/NULL to `0` before load (see §4.2) |
| `mesh` | list\<string\> | no | MeSH descriptors |
| `publication_types` | list\<string\> | no | Publication types |
| `language` | string | no | Language code when known (critical for Apollo / multilingual) |
| `license` | string | no | Licence code. Vocabulary: `CC0`, `CC BY`, `CC BY-SA`, `CC BY-ND`, `CC BY-NC`, `CC BY-NC-SA`, `CC BY-NC-ND`, `permissive`, `text_mining`, `unknown` (all from `normalize_license`), plus `public_domain` (a source-anchored governance override set only by serializers; **never** returned by `normalize_license`; see §6.3) |
| `license_url` | string | no | License URL when source provides one (common on preprints) |
| `license_raw` | string | no | Raw licence text for audit (`normalize_license` truncates to 300 chars); kept even when a governance override sets `license` |
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

## 4. Storage: Postgres, identity and idempotency

> **Superseded:** v1.1 specified Apache Iceberg + Parquet under `02_processed/warehouse/` with `source` + `year` partitioning. That design was replaced by ADR-0001 (hybrid storage) and ADR-0002 (data model). No code in `src/` writes an Iceberg table or a `warehouse/` directory. The Iceberg layout is not the contract; this section is.

### 4.1 Tables

| Item | Current behaviour (verified against `src/episteme/data/db/schema.sql`, migration 0002, `postgres_loader.py`) |
|------|------|
| Row of record | `episteme.articles`: narrow "hot" table, the `ARTICLE_COLUMNS` minus `title`, `abstract`, `body_text`, `text`. No primary key and no unique index on `id`. |
| Text | `episteme.article_body` holds `article_id` (the row's `id`), `source`, `year` and the four text columns. |
| Partitioning | `articles`: `LIST (source)`. `pmc` (in `schema.sql`) and `bookshelf` (added by migration 0002, `src/episteme/data/db/migrations/0002_container_and_book_parts.sql`) are each sub-partitioned by `RANGE (year)`; every other source lands in the default partition. `article_body` is `LIST (source)` only, with no year sub-partitions: `article_body_pmc` and `article_body_default` in `schema.sql`, plus `article_body_bookshelf` from migration 0002. |
| Unknown year | The loader coerces `year` NULL or unparseable to `0` (partition `articles_pmc_y0` for `pmc`). |
| Staging hand-off | Extract and serialize write one Parquet shard (JSONL if pyarrow is missing) per input under `staging/<source>/`; `load_articles` reads it and loads Postgres. |
| Lineage | One `episteme._lineage` row per loaded `source_file` (`rows_inserted`, `rows_deleted`, `input_content_hash`, `run_id`). |
| Related table | `episteme.id_map` (PMID, PMCID, DOI; from the Europe PMC mappings file, **only after file integrity check**). It is not an `articles` table. |

Directory layout of the processed root:

```text
02_processed/
  staging/<source>/<shard>.parquet     # hand-off to the loader
  _ops/<source>/                       # markers and run manifests (2.2)
  _ops/_audit/audit-YYYYMMDD.jsonl     # audit mirror (docs/11)
```

### 4.2 Idempotent load (delete-by-`source_file`)

`postgres_loader.load_source_file` runs one transaction per shard (the caller commits). In order:

1. Delete the `episteme.article_body` rows belonging to the replaced `source_file`(s) (via the `articles.id` linkage) or whose id is in the incoming shard, scoped by `source`.
2. **Guard (d0):** delete any `episteme.articles` row of the same `source` whose `id` appears in the shard but whose `source_file` is **not** one of the shard's files. A periodic full re-release may reuse a native id under a different `source_file`; the old row is replaced rather than duplicated.
3. **delete-by-`source_file`:** delete the `episteme.articles` rows with `source_file` equal to each shard file, scoped by `source`. Basenames are not unique across sources, hence the scoping.
4. `COPY` the hot columns into `episteme.articles` and the text columns into `episteme.article_body`.
5. Insert the `_lineage` row(s) and write the audit event: `load_replace` if anything was deleted, otherwise `load_commit` (see `docs/11-gxp-data-integrity.md`).

Idempotency is delete plus `COPY`, never `ON CONFLICT`, because the table has no key.

### 4.3 Input identity (`input_key`)

`checkpoint_markers.input_key(path, raw_root)` is the shared unit-of-work identity: the path relative to the raw root with its parts joined by `__`. A file directly under the raw root keeps its bare basename, so flat layouts keep the marker, shard and `source_file` names they always had. A path outside the raw root falls back to its basename. `discover_input_files` dedups by resolved full path, not basename.

- The structured serializers (4.4) use `input_key` for the marker name, the shard name, the audit object and `source_file`.
- The SP2 literature extractors (`pubmed`, `pmc`, `apollo`, `bookshelf`, `guidelines`, `europepmc_preprint`, `europepmc_manuscript`) still use the input **basename** as `source_file`. Moving them to `input_key` is deferred. `pubmed` and `apollo` still discover inputs through `checkpoint_markers.list_input_files`, which its docstring marks legacy: it dedups by basename, so same-named files in different directories collapse to one.

### 4.4 Structured-source serialisation contract

Applies to `serialize_<src>` for `chembl`, `uniprot`, `pubchem`, `clinvar`, `reactome`, `mesh`, `ontologies`, `openalex`. Their importable entry point takes `(raw_dir, processed_dir, ...)`.

| Element | Contract (verified in the serializers) |
|---------|----------|
| Return value | A dict with the keys `inputs`, `ok`, `failed`, `rows` (file-level counters). `openalex` adds `n_records_seen`, `n_accepted_biomedical`, `n_rejected_non_biomedical`. |
| Row id | `id = f"{source}:{native_id}"`; `source_record_id` is the native id. For `ontologies` the native id is the term CURIE, used verbatim after the `ontologies:` prefix. |
| Audit event | `serialize_commit` (not `extract_commit`), best-effort: a failure to audit does not fail the file (`docs/11`). |
| Identity | `source_file`, the marker name and the shard name are `input_key(path, raw_dir)`. |
| Records with no native id | `chembl`, `clinvar`, `pubchem`, `reactome` and `openalex` skip the record and count it as `skipped_no_id` in the per-file result (surfaced with `--verbose`, and in the per-file marker stats), **not** in the four-key summary. `mesh` silently drops a descriptor with an empty UI (no counter). `uniprot` does not skip: a malformed header or empty accession raises `ValueError` and fails the file. `ontologies` has no id-less case (`term.id` is always present). |

**Doc-versus-code gaps found while reconciling (the code wins):**

| Gap | Where |
|-----|-------|
| v1.1 said Iceberg with `source` + `year` partitions; the code is Postgres, `LIST (source)` with year sub-partitions for `pmc` (`src/episteme/data/db/schema.sql`) and `bookshelf` (migration 0002) only. ADR-0002 describes year sub-partitions for each source, and says `article_body` mirrors `articles` (`LIST (source)` then `RANGE (year)`); the code has none for other sources and `article_body` is `LIST (source)` only. | 4.1 |
| `skipped_no_id` is not uniform across the structured serializers (table above). | 4.4 |
| `row_issues/` has no writer. Of the failure classes in 2.4, the code sets `error_class` to `parse_error`, `unknown`, or (serializers) the exception type name; `io_error`, `schema_violation` and `corrupt_source` have no emitter in `src/episteme/data`. | 2.2, 2.4 |
| Apollo `subset` was `other` here; the code assigns `permissive` -> `commercial` from the HF card (`apache-2.0`). | 6.2 |
| v1.1 named `scripts/data/analyze_source_samples.py`, `src/episteme/data/schema.py`, `src/episteme/data/ops.py`, `scripts/data/extract_<source>.sh`; none exist. | 8, 10 |

OpenMetadata: register table and columns on first successful sample load; tag license/subset columns; lineage from `01_raw/<source>` to the extract or serialize job to the table (see `enrich_openmetadata.py`).

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
| CC0, or a `creativecommons.org/publicdomain/zero/` URL | `CC0` | `commercial` |
| CC BY (no NC), or a `creativecommons.org/licenses/by/` URL | `CC BY` | `commercial` |
| CC BY-SA | `CC BY-SA` | `commercial` |
| CC BY-ND | `CC BY-ND` | `commercial` |
| CC BY-NC* | `CC BY-NC` / variants | `text_mining` |
| Explicit text-mining / fair-use manuscript notices | `text_mining` | `text_mining` |
| Exact permissive-OSI identifier (Apache-2.0, MIT, BSD, ISC; `_PERMISSIVE_LICENSE_IDS`) | `permissive` | `commercial` |
| Unknown / NLM citation redistribution | `unknown` | `open_metadata` (PubMed) |
| Source-anchored governance override (6.3), set by a serializer, **not** by `normalize_license` | `public_domain` | `commercial` |

Store URL in `license_url` when present. `license_raw` carries the raw text (truncated to 300 chars by `normalize_license`).

Matching order in `normalize_license` matters: CC0, then the NC/SA/ND variants, then plain BY, then the bare `creativecommons.org` URL forms, then `text_mining` (text mining, text-mining, fair use), then the permissive-identifier set, else `unknown`. Matching is substring-based and does not understand negation.

### 6.2 Source defaults

| Source | Default `subset` | Notes |
|--------|------------------|-------|
| PubMed | `open_metadata` | Not commercial full text |
| PMC `oa_comm` | `commercial` | Verify per-article `license_code` |
| EPMC preprints | Derived from license | NC → `text_mining`; BY/CC0 → `commercial` |
| Author manuscripts | `text_mining` | Do not mix into commercial training by default |
| Apollo | `permissive` -> `commercial` (HF card `apache-2.0`) | The code already assigns this; `docs/02` still lists commercial-use diligence as open |
| Structured sources | Per source, see 6.3 and `docs/12-source-inventory.md` | Licence rulings live in the serializers |

Commercial path:

```text
subset = 'commercial' AND extract_status = 'ok'
```

### 6.3 Structured-source licences and the `public_domain` override

`public_domain` is a **source-anchored governance override** (decision 2026-09-19). `normalize_license` never returns it: it is set explicitly by `serialize_mesh`, `serialize_pubchem` and `serialize_clinvar`, and `subset_from_license("public_domain")` is `commercial`. `normalize_license` itself returns `unknown` for the real upstream text of those three sources; `license_raw` keeps that real text, so the override is auditable.

Caveat for PubChem and ClinVar: they carry contributor-submitted content whose submitters may assert their own terms, so `public_domain` there is a governance ruling for the source, not a per-record legal determination.

| Source | `license` -> `subset` | Basis |
|--------|----------------|-------|
| `mesh`, `pubchem`, `clinvar` | `public_domain` -> `commercial` | governance override, above |
| `chembl` | `CC BY-SA` -> `commercial` | ChEMBL's stated release licence, via `normalize_license` |
| `reactome`, `openalex` | `CC0` -> `commercial` | via `normalize_license` (openalex: the dataset-level metadata licence, not per-work OA licences) |
| `ontologies` | GO and MONDO `CC BY` -> `commercial`; HPO `unknown` -> `open_metadata` | GO and MONDO declare a `creativecommons.org/licenses/by/` URL, which `normalize_license` now recognises; HPO declares only a licence page URL |
| `uniprot` | `subset` = `text_mining` (hardcoded) | the real licence is CC BY 4.0, but the serializer hardcodes `text_mining` as a governance override instead of calling `subset_from_license` |
| `cdisc_bc` | none | download-only, no serializer, no `articles` rows; licence **unverified** (repo code is MIT; the README grants CC-BY-4.0 to documentation only and states nothing for `export/` data) |

Per-source cadence and script paths: `docs/12-source-inventory.md`.

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

Audit tooling: `src/episteme/data/sample_audit.py` (field-shape report over staging shards).

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

Everything is driven from `scripts/data/run_pipeline.sh` (source and stage arguments). Per-source wrappers live under `scripts/data/<source>/`:

```text
scripts/data/run_pipeline.sh                      # orchestrator (audits run_start / run_end)
scripts/data/<source>/download_<source>.sh        # every wired source (Europe PMC feeds sit under scripts/data/europepmc/<feed>/)
scripts/data/<source>/extract_<source>.sh         # literature sources
scripts/data/<source>/serialize_<source>.sh       # structured sources (4.4)
scripts/data/<source>/load_<source>.sh            # -> python -m episteme.data.load_articles --source <source>
scripts/data/<source>/graph_<source>.sh           # literature sources, and mesh
scripts/data/materialize_corpus.sh                # 03_corpus materialisation
scripts/data/source_inventory.sh                  # machine-local inventory (docs/12)
scripts/data/verify_audit_trail.sh                # audit-chain verification (docs/11)
scripts/data/db/{init,migrate}_database.sh        # database setup
src/episteme/data/article_schema.py               # schema constants, licence and status rules
src/episteme/data/checkpoint_markers.py           # markers, run manifests, input_key
src/episteme/data/staging_writer.py               # staging shards
src/episteme/data/postgres_loader.py              # idempotent load (4.2)
src/episteme/data/sample_audit.py                 # field-shape report
src/episteme/data/<source>/extract_<source>.py    # literature extractors
src/episteme/data/<source>/serialize_<source>.py  # structured serializers
```

Exceptions: the Europe PMC extractors sit in `src/episteme/data/europepmc/{preprints,manuscripts}/` with plural file names, and their wrappers in `scripts/data/europepmc_{preprint,manuscript}/`. `scripts/data/run_pipeline.sh` is the authority on which wrapper handles which source. The v1.1 names `scripts/data/extract_<source>.sh`, `scripts/data/analyze_source_samples.py`, `src/episteme/data/schema.py` and `src/episteme/data/ops.py` do not exist.

Audit obligations (who records what, hash chaining, verification, the operator identity and the known gaps) are specified in `docs/11-gxp-data-integrity.md`.

---

## 11. Non-goals (v1.1)

- Cross-source deduplication into a single “best” article
- Full knowledge-graph materialisation
- Perfect multi-jurisdiction license interpretation (stay conservative for `commercial`)
- Loading entire sources into memory

---

## 12. Open questions

1. Exact Apollo `subset` after commercial-use diligence on corpus terms.
2. Resolved: Postgres is the store of record (ADR-0001); no Iceberg catalog is needed unless a lakehouse tier is added.
3. `source_file` identity: resolved for the structured serializers (`input_key`, 4.3); the SP2 literature extractors still use the basename (deferred).
4. Final min-length threshold for `ok` after a larger PubMed sample (default 200 chars).

---

## 13. Document control

| Version | Date | Notes |
|---------|------|-------|
| v1 | 2026-08-31 | Initial contract |
| v1.1 | 2026-08-31 | Sample audit incorporation; PMC columns; status/license rules; known corrupt ID map |
| v1.2 | 2026-09-21 | SP5: Postgres storage (Iceberg superseded), `permissive` / `public_domain` licence codes, structured-source contract, `input_key` identity, real entrypoints, docs/11 cross-reference |
