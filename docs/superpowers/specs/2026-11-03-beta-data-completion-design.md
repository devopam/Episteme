# Beta data completion: AACT trials, EU label sections, parked fixes

Status: draft for review, 2026-11-03.

## 1. Intent

Close the data layer for the beta release. The agent must answer trial questions (study planning, trial results) and compare US and EU labels section by section. Three parked defects from PR #99 are fixed in the same change.

The owner set the limit: "Else, there will be no end to this data gathering exercise itself." So this design adds no new download.

## 2. Scope

**In:**
1. AACT: load the ClinicalTrials.gov snapshot already on disk (`D:/episteme/01_raw/aact/20261103_export_ctgov.zip`).
2. EPAR: section offsets for the SmPC (Annex I) of each `product-information` document.
3. Three parked items from PR #99 (section 6).

**Out, after the beta (Owner):**
- ClinicalTrials.gov version history, needed to explain amendments: site additions, eligibility changes, procedures and schedule of assessments, dosing changes, endpoint changes. The agent says this is not covered.
- EMA scientific guidelines, openFDA Complete Response Letters and historical documents.
- OpenAlex authors, sources and institutions loaders. The raw download continues and is kept.

## 3. Facts (measured on the real file)

| Fact | Value |
|---|---|
| File | `20261103_export_ctgov.zip`, 2.39 GiB zipped, 15.72 GiB unzipped, zip test ok |
| Members | 49 pipe-delimited `.txt` files, one per table, header row first |
| Format | standard CSV with `|` as delimiter and `"` as quote; fields may hold line breaks inside quotes; a CSV reader parses `brief_summaries` with no row of the wrong width |
| Studies | 609,804 rows in `studies.txt` (AACT's own "530K+" figure is out of date) |
| Largest tables | `reported_events` 5.0 GiB, `outcome_measurements` 3.0 GiB, `design_outcomes` 1.1 GiB, `eligibilities` 0.9 GiB, `detailed_descriptions` 0.8 GiB |
| Dictionary | `aact_data_dictionary_2026-11-03.csv` (owner download): 52 tables, 472 columns, with types, PK, FK, nullable, deprecated flags, descriptions |
| Not in the zip | `mesh_headings`, `mesh_terms`, `study_searches` (AACT's own lookup tables; our NLM MeSH covers the first two) |
| Key | every table has `nct_id`; most have an `id` |

## 4. AACT

### 4.1 Rules

- **Reference data, one full snapshot per release (Owner rule 4 of the regulatory design).** Latest by default. Only the current release stays on the main disk.
- **Lookup, not training (Owner design: regulatory and registry data is looked up).** AACT writes no `articles` rows in this cut, so it cannot reach the training corpus. The source classification test in section 6 makes the rule explicit for every `articles` source.
- **TEXT in staging (Owner principle).** Columns load as `text`. Typed columns come only where a join or a filter needs them.

### 4.2 Data model

- **Schema per release.** Each release loads into its own schema `aact_r<YYYYMMDD>`, one table per file, with the columns of the file header. The current release is exposed through the schema `aact`, which holds one view per table over the latest release schema. Switching release recreates the views in one transaction (Claude's call: the same "readers never see a half state" rule as the FAERS generation swap, without renaming 49 tables).
- **Registry:** `episteme.reg_release` gets a row with `dataset='aact'`, so `latest_release` works as for the other reference data.
- **Columns from the file header, checked against the dictionary.** A column in the file but not in the dictionary, or the reverse, is reported. A table in the dictionary but not in the zip is reported, not fatal (the three in section 3).
- **Typed columns:** `studies` dates (`study_first_submitted_date`, `start_date`, `completion_date`, `results_first_posted_date`, and others the dictionary types as `date`) get a typed copy, so planning questions can filter by date. The builder takes types from the dictionary.
- **Indexes,** built after the load, per release schema: `nct_id` on every table; `reported_events (adverse_event_term)`; `interventions (name)`; `conditions (downcase_name)`; `browse_conditions (mesh_term)`; `browse_interventions (mesh_term)`.

### 4.3 Joins to the rest of the data

- **Interventions to moieties:** `interventions.name` and `intervention_other_names.name` go through `resolve_names(conn, names, source='aact')`, the exact normalised match already used for EMA. Unmatched names are counted. No fuzzy match.
- **Conditions and interventions to MeSH:** through `browse_conditions` and `browse_interventions`, which hold MeSH terms, joined to our NLM MeSH by name.
- **Trial adverse events next to FAERS:** `reported_events` holds an event term, its organ system, and the number of participants affected and at risk per arm. The term joins to the FAERS reaction terms by case-folded text (the FAERS `term_norm` rule). The agent can state a trial rate with its denominator next to a FAERS report count, and must label the two as different kinds of evidence.
- **Trials to publications:** `study_references.pmid` joins to `articles.pmid`.

### 4.4 Load

- Stream each member from the zip, never unzip to disk. `COPY` per table in batches capped by `load_batch_bytes`.
- One transaction per table, with a marker per table, keyed on `input_key` plus the database name (the PR #99 rule). A re-run resumes at the first table not done.
- After all tables: indexes, `ANALYZE`, row checks, then the view switch and the `reg_release` row in one transaction.
- **Row checks:** `studies` rows must equal the number of distinct `nct_id`; every table's `nct_id` must exist in `studies` (counted, a share limit in config, not zero, because AACT has known orphans; Claude's call, set at the first sample); a table with zero rows fails, except the three absent ones.
- A failed or partial load leaves the previous release serving.
- Old release schemas are not dropped by code (Owner: no partition-migration code); the runbook says how to drop one.

### 4.5 Text for the agent

The agent reads trial text from the AACT tables through the tool layer later. No `articles` rows are made for trials in this cut (Claude's call: the text is already structured by table, and the retrieval design for the whole dataset comes next and will decide how trials are indexed).

## 5. EPAR SmPC sections

- **Which documents:** `product-information` only. The PDF holds Annex I (the SmPC, one or more products), Annex II, labelling, and the package leaflet. Sections are read from Annex I only; the leaflet's own "4. Possible side effects" must not be taken.
- **How:** a deterministic heading match on the SmPC numbering (`4.1 Therapeutic indications` to `6.6 ...`) in the stored `body_text`, with the start of Annex II as the end of Annex I. No model, no fuzzy text match.
- **Where:** `label_section`, so one query finds US and EU sections alike. `loinc_code` holds the DailyMed LOINC code for the same subject where one exists (Claude's call; the builder confirms each code against the DailyMed rows on disk):

| SmPC section | Subject | Code |
|---|---|---|
| 4.1 | Indications | 34067-9 |
| 4.2 | Dosage | 34068-7 |
| 4.3 | Contraindications | 34070-3 |
| 4.4 | Warnings and precautions | 43685-7 |
| 4.5 | Interactions | 34073-7 |
| 4.6 | Pregnancy and lactation | 42228-7 |
| 4.8 | Undesirable effects | 34084-4 |
| 4.9 | Overdose | 34088-5 |
| 5.1 | Pharmacodynamics | 43679-0 |
| 5.2 | Pharmacokinetics | 43682-4 |

  Other sections get `smpc:<number>`. `title` holds the heading as printed, including the product name when one PDF holds several SmPCs.
- **Where it runs:** in the EPAR extract, written to the per-part label sidecar as DailyMed does, and loaded by the same `replace_labels` step. Offsets point into the stored `body_text`, and `body_text[start:end]` must equal the section text (the DailyMed test).
- **Gaps:** a document where the headings are not found gets no section rows, and is counted per document. The gate reports the share with sections; a provisional floor is set at the sample.
- **Re-extract:** the EPAR fingerprint gains the section-rule version, so a change re-extracts.

## 6. Parked fixes from PR #99

1. The FAERS signal refusal message and docs/10 5.12 name `--force --reason` as the recovery step.
2. Spec 2026-11-02 section 5.5 drops `substance_key` from `label_substance`, and says the join goes through `unii`.
3. A test ties `corpus_excluded_sources` to every source of the regulatory and registry kind (`dailymed`, `ema_epar`, `aact`), so a new one cannot enter the training corpus by default.

## 7. Samples and gates

- **AACT:** the whole file is the sample (it is one unit, 2.4 GiB). The sample loads into `episteme_test`. Rules: all tables loaded with rows, studies equal distinct `nct_id`, orphan share under the limit, intervention coverage through `resolve_names` recorded (provisional floor), peak memory under 2 GB.
- **EPAR sections:** the existing EPAR slice (largest PDFs plus a scan-like one); rule: share of `product-information` documents with section 4.5 found, provisional.

## 8. Constraints

- `config.py` is the only module that reads `os.environ`. Every cap and share is a `Settings` value.
- No new download. No change to the running OpenAlex download.
- Database tests and samples wait for a quiet disk.
- One PR, with the drift log updated.

## 9. Builder questions settled (Claude's calls)

1. **Typed dates:** extra typed columns on the release's `studies` table, next to the text columns, which stay. The `aact` views expose every column. An empty or bad date loads as NULL and is counted per column.
2. **Release id:** the `YYYYMMDD` at the start of the zip name, checked as a date; the dictionary date is recorded in the run record. A finished release with the same input fingerprint is skipped. The same id with a different fingerprint is refused unless `--force --reason` is given (audited).
3. **Interventions to moieties:** `resolve_names(source='aact')` runs inside the load, after the tables and before the view switch, over the distinct names of `interventions` and `intervention_other_names` (the count is reported). A per-release table `intervention_substance (nct_id, intervention_id, substance_key, method)` is materialised, exposed by a view in `aact`; the agent reads it, never re-normalising at query time. Coverage is reported, not a failure, until the sample sets a floor.
4. **EPAR sections:** the label module takes the source as a parameter (no hard-coded `dailymed`); the EPAR sidecar holds sections only. The load and the retire step call it for `ema_epar` as they do for `dailymed`. DailyMed tests must pass unchanged.
5. **Re-extract:** the section-rule version enters the fingerprint of `product-information` documents only (about 2,200), so other EPAR types are not re-extracted. Article and label rows are replaced together in one transaction, as today, so offsets cannot lag the text.
6. **Order and repeats:** migration 0011 adds `ordinal integer` and `product text` (both nullable; NULL for DailyMed) to `label_section`. A PDF with several SmPCs gives one row per section per product. Every numbered heading found gets a row: the mapped LOINC code where section 5 lists one, else `smpc:<number>`.
7. **Source kind:** `article_schema` gets two explicit sets, training sources and reference sources. A test requires every source in `SOURCES` to be in exactly one, and `corpus_excluded_sources` to cover every reference source. A new source therefore fails the test until it is classified.
8. **Docs:** `docs/02` and `docs/12` get the AACT load row. Licence posture: ClinicalTrials.gov records are public US government data, recorded as such in `docs/02` with a note that sponsor-supplied documents are not loaded.
9. **Orphans and partial loads:** one orphan-share limit per table, from `Settings` with one default (`EPISTEME_AACT_MAX_ORPHAN_SHARE`). It is checked after the tables and before the switch; a breach leaves the previous release serving and fails the run. A re-run reuses the partial release schema and resumes. The runbook says how to drop a failed release schema by hand.
10. **Dictionary mismatches:** the file header drives the table columns. A column only in the file is loaded and reported; a column only in the dictionary is reported; deprecated columns are loaded. None fails the load.
11. **Memory:** the existing gate `peak_rss_mb` (the loader's process tree, server excluded), a hard gate at 2048 MB, as for other sources.
12. **EPAR section floor:** report only in this change; the sample sets the floor.

## 10. Left to the builder

- Exact typed columns of `studies`, from the dictionary.
- Batch sizes within the byte cap; how markers and the view switch are coded, following FAERS and `load_reg`.
- The SmPC heading pattern, built from real EPAR text at the sample (tests use real wording).

## 11. Decisions

| Decision | By |
|---|---|
| Version history out until after the beta | Owner |
| Guidelines, FDA letters, OpenAlex entity loaders out until after the beta | Owner (agreed to Claude's proposal) |
| No new download in this change | Owner |
| AACT is lookup, not training | Claude's call |
| Schema per release, views in `aact` switched in one transaction | Claude's call |
| No `articles` rows for trials in this cut | Claude's call |
| Release schema `aact_r<YYYYMMDD>` from the zip name; repeat with other content refused without `--force --reason` | Claude's call |
| Materialised `intervention_substance` per release | Claude's call |
| `label_section` gains `ordinal` and `product` (migration 0011) | Claude's call |
| Training and reference source sets, with a classification test | Claude's call |
| EU sections stored in `label_section` with the DailyMed LOINC code | Claude's call |
| Orphan `nct_id` share limit set at the first sample | Claude's call |
