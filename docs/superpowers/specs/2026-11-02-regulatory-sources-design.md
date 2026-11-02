# Regulatory sources: DailyMed, openFDA and EMA in one pluggable model

Status: draft for review, 2026-11-02.
Parents: `2026-10-28-end-consumer-programme-design.md` (this is project 3, slices 1 and 2) and `2026-10-19-pipeline-operating-standard-design.md` (bounded units, resume, samples, one heavy job at a time).

Notation: **Owner** marks a decision the project owner made. **Claude's call** marks a choice the owner has not yet confirmed.

## 1. Intent

The owner's near-term goal is a fine-tuned GGUF plus RAG lookup, working end to end, to check its efficiency. The first cut must include DailyMed, openFDA and EMA (Owner), because the holistic view across them is what shows the value of integrated data.

The FDA feeds are mostly structured, so the work is a data model that makes loading and search efficient in one go: suitable partitions, and indexes on the key columns used to join datasets (Owner). EMA has many small datasets, so the design must be pluggable and take in all of them (Owner).

Example of the target question: "Drug X is suddenly reported with many other drugs. Is this linked to a lot, or to combinations already in the warnings?" (`docs/benchmark-questions.md`, Q1).

## 2. Scope

**In the first cut:**

| Group | Datasets |
|---|---|
| openFDA | `drug/event` (FAERS), `drug/enforcement`, `drug/ndc`, `drug/drugsfda`, `drug/orangebook`, `drug/shortages`, `other/substance` (the UNII/GSRS substance data; moieties and synonyms), `other/nsde` if the plan needs it for NDC history |
| DailyMed | human Rx and OTC SPL releases, plus updates; substance and pharmacologic-class indexing files |
| EMA feeds | medicines, post-authorisation, orphan designations, PIPs, PSUSAs, referrals, DHPCs, shortages, maximum residue limits, herbal medicines, opinions outside the EU; Article 57 (Excel) |
| EMA text | EPAR English PDFs of type product-information, assessment-report and rmp-summary (Claude's call on the types) |

**Plugged in later, with no model change:** PMS public API (beta; terms to read), SPOR RMS lists, veterinary UPD, CTIS, HMA-EMA catalogues, PRAC signal PDFs, DailyMed animal labels, openFDA device and veterinary endpoints, India (after the beta release, Owner).

**Not in scope:**
- Moving old partitions to another disk. No code or automation for this now (Owner); it is a future manual step.
- Removing the duplicated text (programme project 2). New label rows use the current `text` contract.
- EudraVigilance counts: the portal requires accepting a disclaimer per report and has no bulk route.
- The concept graph (programme project 4), which is built on this.
- **The retrieval layer and the GGUF training.** The Owner wants the retrieval layer designed and built for the whole dataset at once, not piecemeal on part of it. It gets its own design after this data layer, covering every source. This design must not preclude that, and it does not build a partial version.

## 3. Facts the design depends on

Checked on 2026-11-02.

| Fact | Evidence |
|---|---|
| `drug/event`: 20,692,690 reports, 104.9 GB zipped, 1,767 files, 2004 to now | openFDA catalogue |
| Pre-2015 is about 16% of FAERS by size | catalogue, per-year sum |
| One JSON shape across FAERS eras; field coverage differs | one small file each from 2004q2, 2012q2, 2026q2 |
| Batch number on 8% / 15% / 27% of drug entries; `activesubstance` 0% / ~0% / 99%; `openfda` annotation 52% / 57% / 90%; report version 0% / 1% / 100% | same samples, so indicative only |
| In old reports a seriousness flag is present only when true; in new reports all flags are present | same samples |
| A FAERS report lists several drugs and reactions without linking a drug to a reaction | openFDA documentation |
| `drug/enforcement` has `code_info` (free text holding lot numbers) and `recall_number` | file on disk |
| `drug/drugsfda` key `application_number`; `drug/orangebook` nests `products`; `drug/ndc` catalogue 138,216 records | catalogue, files on disk |
| DailyMed: zips of per-label zips; each inner zip holds one SPL XML and images. SPL has `setId`, `versionNumber`, LOINC-coded sections, UNII (OID `2.16.840.1.113883.4.9`) and NDC (OID `2.16.840.1.113883.6.69`) codes | `dm_spl_daily_update_10132026.zip` |
| EMA: 18 JSON feeds, twice a day, each with `meta.total_records`. Medicines 2,754; orphan 3,310; PIPs 3,400; PSUSAs 2,752; referrals 593; DHPCs 175; shortages 85; MRLs 529; post-authorisation 152; outside EU 19; herbal 251 rows (feed says 204) | files fetched |
| EMA Article 57: about 164,700 rows, header at row 7; product, active substance, route, country, holder, pharmacovigilance contact | file fetched |
| EPAR documents feed: 20,342 records with type, `ema_product_number`, `document_url` and translated URLs. Product information 2,222; assessment reports 1,799; RMP summaries 679 | file fetched |
| The non-EPAR documents feed failed twice mid-download | curl error 18 |
| EMA search and "what we publish" pages show a human-verification check under automated browsing | browse and Chrome, 2026-11-02 |
| `other/substance`: 167,385 records, 332 MB zipped, one 2.4 GB JSON file. Fields: `unii`, `names`, `codes`, `relationships`, `moieties`. 32,512 records carry an `ACTIVE MOIETY` relationship. Metformin hydrochloride links to metformin (`9100L32L2N`); acetaminophen and paracetamol sit on one record (`362O9ITL9D`, 82 names) | file fetched and read |
| EMA reuse is allowed, commercial and non-commercial, with EMA credited in each copy; third-party content excluded | EMA legal notice |

## 4. Rules

Decisions on versions (Owner):
- **Reference data: each release is a full snapshot in its own partition.** No row-level change tracking, because each release has updates, deletes and additions.
- **Default answers read the latest release only.** Older releases are read only on explicit request.
- **Only the current release stays on the main disk**; older partitions go to an archive disk and may be purged later, as a manual step.
- **Publications and FAERS are the exception: the latest copy replaces the old one.** FAERS is a full reissue with no incremental detection.

Labels and EPAR text count as publications: the latest copy replaces the old one (Claude's call).

From the programme design: every claim carries provenance and confidence; exact values are fetched, never generated; gaps are reported with coverage numbers. Licence-bound data follows `docs/02`.

Project rules that bind this work:
- Endpoint URLs live only in `scripts/data/_lib/sources.env`.
- `config.py` is the only module that reads `os.environ`.
- Marker keys and stored `source_file` values use `input_key` or a name built from it, with a collision test.
- Tunable values go in config, not source constants (Owner).

## 5. Data model

### 5.1 Pluggable feeds

One config file, `src/episteme/data/regulatory/feeds.toml`, holds one entry per dataset:

```toml
[feeds.ema_orphan]
url_key = "EMA_ORPHAN_JSON"         # a key in sources.env, never a URL here
format = "ema_json"                 # names a parser
jurisdiction = "EU"
record_key = "eu_designation_number"
substance_fields = ["active_substance"]
product_fields = ["related_ema_product_number"]
date_field = "date_of_designation_or_refusal"
cadence = "monthly"
```

- A new dataset of a known format needs a config entry, a `sources.env` key and a gate rule. No new code.
- A new format needs one parser: `parse(path) -> Iterator[dict]`. First-cut parsers: `ema_json`, `article57_xlsx`, `openfda_json`.
- The config file stays out of the stage code hash, so tuning a value does not void a pass; its values go into the run record (Owner's parameter rule).

### 5.2 `episteme.reg_record`: all reference feeds

| Column | Type | Note |
|---|---|---|
| `dataset` | text | partition key, level 1 |
| `release_id` | text | partition key, level 2 |
| `record_key` | text | built from the config's `record_key` field list; a dataset with no natural id (Article 57) uses a hash of the row, and exact duplicate rows are counted and dropped |
| `jurisdiction` | text | `US`, `EU`, later `IN` |
| `substance_names` | text[] | normalised names as published; the join to `substance_xref` happens at query time, so an xref improvement rewrites no rows |
| `product_keys` | text[] | normalised forms: NDC in one canonical 11-digit form, application number without prefix, EMA product number. The raw values stay in `payload` |
| `event_date` | date | the feed's main date |
| `payload` | jsonb | the whole record |
| `source_file` | text | the identity rule |

- Partitioning: `LIST (dataset)`, then `LIST (release_id)` per dataset. Loading a release creates its partition, loads, validates, and then switches the registry. Dropping a release drops a partition.
- Indexes per release partition: unique `(record_key)`; GIN on `substance_names`; GIN on `product_keys`; `(event_date)`. Built after the load. No GIN on `payload` in the first cut (Claude's call): add it per dataset only if `pg_stat_statements` shows the need.
- `episteme.reg_release(dataset, release_id, loaded_at, rows, status, is_latest)` is the registry. Exactly one `is_latest` per dataset, enforced by a partial unique index.
- **Pruning needs the release as a literal.** A join to `reg_release` does not prune at plan time. Tools first read the latest `release_id`, then query with `dataset` and `release_id` as values. The efficiency checks (section 8) run this exact form.
- The cadence is ours to choose: EMA's feeds change twice a day, but a release is cut per the `cadence` config, so partitions do not multiply.

### 5.3 `episteme.substance_xref`: the join at the centre

The holistic view rests on this table.

**Composition means the active moiety (Owner).** Default answers group by moiety, so metformin hydrochloride and metformin count together. The exact substance stays available. `episteme.substance (substance_key, unii, preferred_name, moiety_key)` holds one row per UNII; `moiety_key` points at the active moiety, or at itself. All signal tables are keyed on `moiety_key`.

**FDA's substance data (`other/substance`) is the anchor and the US/EU bridge.** Its names and synonyms link acetaminophen to paracetamol, and INN to USAN. Its 2.4 GB single JSON file is read as a stream, never loaded whole (bounded memory). Substances with no UNII (some EU-only names) still get a key, with `method = normalised_name` and no moiety, and are counted.

| Column | Note |
|---|---|
| `substance_key` | bigint, surrogate |
| `id_type` | `unii`, `inn`, `name`, `atc`, `rxcui`, `ema_product`, `ndc_product`, `application_number`, `spl_set_id` |
| `id_value` | normalised value |
| `method` | `exact_code`, `annotation`, `normalised_name`, `manual` |
| `confidence` | the basis for the claim's confidence (programme 4.3) |
| `source` | dataset that gave it |

- Primary key `(id_type, id_value, substance_key)`; index `(substance_key)`.
- **Keys are stable:** a `substance_key` is never reused or renumbered. An improved mapping adds or corrects xref rows only; no fact table stores a resolved key, so no large table is ever rewritten.
- **One name can map to several substances,** so a combination product resolves to all its ingredients.
- Built in a fixed order: `other/substance` (UNII, names, codes, moieties), then DailyMed SPL codes, then FDA annotations, then EMA and FAERS names by normalised match against the synonyms.
- Name normalisation is deterministic and tested (case, whitespace, and the `|` separator in Article 57).
- Anything unmatched stays unmatched and is counted. The coverage per dataset goes into the gate report and is what the agent reports. No fuzzy match in the first cut (Claude's call): a wrong link is worse than a reported gap.

### 5.4 FAERS typed tables

Too large for JSONB, and they need exact counts.

| Table | Key columns | Other |
|---|---|---|
| `faers_report` | `safetyreportid`, `receive_year` | received and latest receipt dates, version, serious flags, country, sex, age, weight, `source_file` |
| `faers_drug` | `safetyreportid`, `seq` | `role` (suspect, concomitant, interacting), `drug_entry_id`, `batch_number`, route, indication, `source_file` |
| `faers_reaction` | `safetyreportid`, `seq` | `term_id`, outcome, `source_file` |
| `faers_drug_entry` | `drug_entry_id` | a dictionary of distinct (product name, active substance, annotation) combinations, so repeated text is stored once |
| `faers_entry_substance` | `drug_entry_id`, `substance_key` | the bridge to `substance_xref`, with `method`. Improving a link updates this small table only |
| `faers_term` | `term_id` | reaction term as published |

- `faers_report`, `faers_drug` and `faers_reaction`: `RANGE (receive_year)`, one partition per year, so recent-window queries prune old years. 2004 to now is 23 partitions, plus a default for a report with no usable date. Both child tables carry `receive_year` too.
- `receive_year` and `receive_date` come from the first receipt: the earlier of `receivedate` and `receiptdate` (Owner, measured on 12,000 reports in each of five quarters: up to 2012 `receiptdate` is usually the earlier and lies in the file's own quarter; from 2018 `receivedate` is the earlier). Both published values stay. The signal quarter uses the same date.
- Indexes per partition, built after the load: `faers_report (safetyreportid)`; `faers_drug (drug_entry_id)`; `faers_drug (safetyreportid)`; `faers_reaction (term_id)`; `faers_reaction (safetyreportid)`; partial `faers_drug (drug_entry_id) WHERE batch_number IS NOT NULL`.
- **Era handling** is in one parser with explicit rules: a missing seriousness flag means "no" when the report has any flag or a non-serious marker (Claude's call, to be checked on the samples); a missing `active_substance` falls back to the annotation, then to the normalised product name.
- **Latest copy only (Owner), by generation.** A refresh does not edit live tables. It builds a new generation of the three tables with no indexes:
  1. `COPY` per unit, one transaction per zip, with markers to resume.
  2. Where one id appears in several zips, keep the highest version, then the latest `receiptdate`, then the latest file; delete the other copies by `(safetyreportid, source_file)`, and count them.
  3. Build indexes per partition, then `ANALYZE`.
  4. Rebuild the signal tables.
  5. Swap the generation in one transaction, then drop the old one.

  Readers never see a half-replaced state, a report absent from the new issue simply disappears, and no per-row deletes run against live indexes. A refresh is manual, about quarterly (Claude's call).
- **Reaction terms are stored as published text.** They are MedDRA preferred terms in FDA's public data. No MedDRA hierarchy is loaded (`docs/02`, Stream 2).

Signal tables, rebuilt per generation and swapped with it, never computed at question time:
- `faers_pair_quarter (substance_key_a, substance_key_b, quarter, reports_suspect, reports_any)`: co-reporting counts, stored in both directions, indexed on `(substance_key_a, quarter)`. Reports listing more drugs than a configured cap are left out of the pair counts, and how many were left out is recorded, because pairs grow with the square of the drug count.
- `faers_event_quarter (substance_key, term_id, quarter, reports_suspect, reports_any)`.
- `faers_quarter_totals`, for the statistic's denominators.
- `faers_batch_coverage (substance_key, quarter, drug_rows, with_batch)`: the coverage number behind "lot present in n of N".
- `faers_batch_quarter (substance_key, batch_norm, quarter, reports)`: reports per normalised batch, so one lot behind a rise shows directly.

**Lot matching (Owner: in the first cut).** `recall_lot (recall_number, release_id, substance_names, lot_raw, lot_norm, method)` holds lots extracted from the free-text `code_info` of `drug/enforcement`. `faers_drug` keeps `batch_number` raw, and adds `batch_norm`. The normaliser is deterministic and tested: case, spaces and punctuation; ranges are expanded only when both ends are explicit. A match needs the same moiety and the same normalised lot. Index `recall_lot (lot_norm)`, and the partial index on `faers_drug (batch_norm) WHERE batch_norm IS NOT NULL`. The agent reports how many matches rest on real lot numbers, how many recalls gave no extractable lot, and the batch coverage. Free text will miss lots, and that is reported as a gap.

### 5.5 Text: labels and EPAR documents

- DailyMed labels and EPAR PDFs become `episteme.articles` rows with new sources `dailymed` and `ema_epar`, and `article_body` text, so RAG and the corpus reach them as they do publications.
- One row per `setId` (the highest `versionNumber`), or per EPAR document id. This is the publications rule. A label or EPAR that a full release no longer contains is retired through the existing retire path, with an audit row (Claude's call).
- Labelled sections (warnings, interactions, adverse reactions) are recorded in a new `label_section (article_id, loinc_code, title, start_offset, end_offset)`, pointing into the body text, because Q1 must check a pair against the interactions section specifically. Offsets, not a copy: no third copy of the text (the Owner rejected even two). Index `(article_id, loinc_code)`.
- `label_substance (article_id, substance_key, unii, ndc_product)` links text to composition.
- openFDA `drug/label` is not loaded as text: it derives from the same SPLs, so it would duplicate DailyMed (Claude's call). It stays on disk as a cross-check.

## 6. Pipeline

- **Downloads:** openFDA's existing `all_human` mode is running. `other/substance` needs one small new mode in `download_openfda.sh`, because `all_human` covers `drug/` only. A new `ema` download stage fetches the feeds through `sources.env` keys, verifying `meta.total_records`, and then the EPAR PDFs listed by the documents feed, one at a time with a configured delay. If EMA answers with its verification page, the stage stops and reports; it does not retry around it.
- **Bounded units:**
  - FAERS: one openFDA zip is one unit (about 12,000 reports). The file stays zipped and is read as a stream.
  - DailyMed: fixed buckets by the first characters of `setId`, written in parts capped by rows and bytes, reusing the PMC pattern, because the labels are many small files inside large zips.
  - EMA feeds: one feed is one unit.
  - EPAR: buckets of documents, written in parts.
- **Load:** the existing `load_articles` for text sources. A new typed loader for FAERS, by generation (section 5.4). A new `reg_record` loader per release.
- **Resume:** markers and fingerprints as in the PMC design.
- **PDF text:** a free, open-source library chosen in the plan, by reliability, maintenance and licence. A document that gives no usable text is listed as failed and counted, not guessed.

## 7. Samples and gates

Every stage is gated, and each sample covers the worst unit (Owner's sampling rule):

| Source | Slice |
|---|---|
| FAERS | the oldest quarter, 2012 (the AERS-to-FAERS change), and the largest file of the newest quarter |
| DailyMed | the largest label in the Rx release, a label with many products, and one OTC label |
| EMA feeds | all feeds (they are small) |
| EPAR | the largest PDFs by size, one scanned-looking PDF if one exists |

Rules include: `max_peak_rss_mb`, `rows_loaded_equals_staging`, failed inputs, `meta.total_records` equal to rows read (EMA), and a minimum `substance_xref` coverage per dataset, set from the first sample.

## 8. Efficiency checks

Query plans for these, checked on the samples and again on full data with `pg_stat_statements` (now installed):
1. Latest release of one dataset by substance, with `dataset` and `release_id` as values: must prune to one partition.
2. FAERS pair counts for one substance across two windows: must read the signal table, not raw rows.
3. FAERS drug rows for a substance with a batch number in the last two years: must prune by year and use the partial index.
4. Label interactions section for a substance: `label_substance` to `label_section`.
5. The EU and US view of one moiety through `substance_xref`.
6. Recalled lots for a moiety matched to FAERS batches.

## 9. Folded into this work (no separate PRs)

- Move tunable constants into config: batch and part caps, `BUCKET_IDS`, slice caps (Owner).
- PMC prologue INFO lines do not reach the run log; fix.
- `scripts/mcpg-episteme.sh` edit: the admin server's read-only `episteme_test` secondary (uncommitted in the main checkout).

## 10. Open questions

| Question | Who |
|---|---|
| Operator rights to expose FAERS reaction terms (MedDRA text in public FDA data) to subscribing tenants | Owner, before serving; programme section 9 |
| Rights for EPAR PDFs and DailyMed text in the training corpus | Claude's reading: EMA notice allows reuse with credit; DailyMed labels are public. To confirm in `docs/02` |
| The herbal feed's row and `total_records` mismatch (251 vs 204) | builder, on the sample |

## 11. Left to the builder

Exact column types beyond those named; the statistic's implementation; part caps' values from measurement; the PDF library, from a two-or-three-way comparison; the delay between EPAR downloads.

## 12. Decisions

| Decision | By |
|---|---|
| DailyMed, openFDA and EMA in the first cut | Owner |
| Pluggable design that covers all EMA datasets | Owner |
| Full snapshot per release for reference data; latest by default; only current on the main disk | Owner |
| Publications and FAERS keep the latest copy only | Owner |
| All FAERS years, with old-era differences handled | Owner |
| No partition-migration code now | Owner |
| Composition means the active moiety by default; the exact substance stays available | Owner |
| The retrieval layer is designed and built for the whole dataset, not piecemeal; it has its own design after this | Owner |
| Match FAERS batches to recalled lots in the first cut | Owner |
| Parameters in config | Owner |
| `other/substance` as the anchor and the US/EU bridge | Claude's call, verified on the file |
| `reg_record` with JSONB payload; `substance_xref` with stable keys, resolved at query time; typed FAERS tables by year with dictionaries; generation swap for FAERS; signal tables in both directions; section offsets, not a text copy | Claude's call |
| No fuzzy name matching; no GIN on payload; openFDA labels not loaded as text; EPAR document types; labels and EPAR as publications; DailyMed buckets by `setId` prefix | Claude's call |
