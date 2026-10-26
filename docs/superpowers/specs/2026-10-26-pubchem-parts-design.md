# Sub-project 2: pubchem in bounded parts, with a streaming merge join

Status: draft for review, 2026-10-26.
Parent: `2026-10-19-pipeline-operating-standard-design.md` (the operating standard). That spec is binding here, in particular §5.3 (part caps), §6.4 (resume and bounded parts) and the sample-gate slice line for `pubchem`.

## 1. Problem

- `pubchem serialize` reads `CID-SMILES.gz` (the primary file, about 120 million CIDs) and joins three sibling files to it: `CID-IUPAC.gz`, `CID-Title.gz` and `CID-Mass.gz`.
- Today the join is a DuckDB `LEFT JOIN ... ORDER BY cid`. On the 16 GB laptop with the USB HDD, it spilled 31–60 GB to the HDD and ran 7.5–12 h without finishing.
- The output is one shard of about 120 million rows. That is one load transaction of about 40 h and one unit of restart.
- `pubchem` therefore has no gate rules and no sample slice, and it is not loaded.

## 2. Goal

- Serialize `pubchem` in one sequential pass with bounded memory and no spill to disk.
- Write the output as parts. Each part is one staging shard, one success marker, one audit row and one load transaction.
- A restart redoes only the parts that are missing.
- `pubchem` gets a sample slice and gate rules for `serialize` and `load`, the same as the other gated sources.

Not in scope: new pubchem properties (synonyms, MeSH, PMID, patents), the licence posture (unchanged: `public_domain`, governance override of 2026-09-19), and loader changes.

## 3. Facts the design depends on

| Fact | Evidence |
|---|---|
| The four files are headerless TSV, keyed by an integer CID in column 1. | `serialize_pubchem.py` module docstring; README-Extras. |
| Each file is sorted by CID, with one row per CID. | Checked on 5M-line samples of each file, 2026-10-19. Not yet checked on the whole files. |
| Sizes (gz): CID-SMILES 1.49 GB, CID-IUPAC 1.85 GB, CID-Mass 1.39 GB, CID-Title 1.89 GB. | `D:/episteme/01_raw/pubchem/Compound/Extras`, downloaded 2026-10-18. |
| Production holds no pubchem state: 0 rows in `articles_pubchem`, 0 `_lineage` rows, no markers, empty staging. | Checked 2026-10-26. |
| `isal` is installed, and `episteme.data.gz.open_gz` uses it. | `src/episteme/data/gz.py`. |
| The profile already resolves `part_rows` and `part_bytes` (`EPISTEME_PART_ROWS`, `EPISTEME_PART_BYTES`). No stage uses them yet. | `src/episteme/ops/profile.py`. |

## 4. Design

### 4.1 Streaming merge join

- Read each file line by line through `open_gz`, in text mode.
- Walk the primary file. For each CID, move each sibling cursor forward while its CID is lower. Take the sibling value when the CIDs are equal; else the value is None.
- Memory is one line per file plus one write batch.
- Fail fast. The reader raises `PubchemInputError` (a permanent error, §6 of the standard) when a CID is not an integer, or when a CID is not strictly above the one before it. The error names the file and the line number. A line with an empty CID is counted as `skipped_no_id` and skipped, as today.
- The DuckDB join, `_build_query` and `_columns_literal` are removed.
- The row builder (`pubchem_row`, `_build_text`) does not change. A row for the same CID and the same input is byte-identical to today's row.

### 4.2 Parts

- One primary file is one unit of work. Its output is a sequence of parts, numbered from 1.
- A part ends when it reaches `part_rows` rows or `part_bytes` of staging data, whichever comes first. The byte count is the sum of the UTF-8 length of each row's `text` plus a fixed per-row overhead; it is an estimate of the staging size, and it is deterministic for one input and one pair of caps.
- The part's `source_file` and shard stem are `<input_key>__part00001`, `<input_key>__part00002`, and so on (5 digits; 120 million rows at 1 million rows per part need 3 digits).
- Each part writes:
  - its staging shard (`write_rows`, parquet);
  - its success marker, with `n_rows`, `extract_status_counts`, `first_cid`, `last_cid`, `part_rows`, `part_bytes` and the write info;
  - one `serialize_commit` audit row.
- After the last part, the primary file gets its own success marker. Its stats list the number of parts and the total rows.

### 4.3 Resume

The risk: `part_rows` comes from the hardware profile or from the environment. If a restart counts rows to skip kept parts, and the caps differ from the first run, a kept marker no longer matches the rows that are skipped, and rows are lost without an error. So the restart does not count rows. It uses the CID range in each marker.

- When the primary file has a success marker, the unit is skipped (unless `--force`).
- Else the run finds the kept parts: parts 1..j whose markers exist and whose ranges follow each other (`first_cid` of part i+1 above `last_cid` of part i).
- The run deletes every part above j: its shard and its marker. A part without a marker can hold a half-written shard.
- The run reads the input, passes over every record up to and including `last_cid` of part j without building rows, and writes part j+1 onward.
- When the current caps differ from the caps in the kept markers, the run logs one warning and continues. The kept parts keep their ranges; the new parts use the new caps. No row is lost or written twice, because the boundary is a CID, not a count.
- `--force` deletes all parts and the primary marker of that unit first.
- Loading: each part loads in its own transaction (the loader replaces by `source_file`). A part that was rewritten with a different range is loaded again; the loader's id-collision guard moves a row that changed part (standard §6.4).

### 4.4 Sample slice

The standard says: the first 2 parts and 1 part from the middle of the file. A file-level slice (hard links) does not work: each file is one unit of more than 1 GB. So the `pubchem` slice is cut.

- `gate prepare` for `pubchem` writes four new gz files under `<processed root>/_sample/pubchem/01_raw/pubchem/Compound/Extras/`.
- The CID ranges come from CID-SMILES:
  - R1: the first `2 × part_rows` CIDs;
  - R2: `part_rows` CIDs from line ⌊N/2⌋, where N is the line count of CID-SMILES.
- Each sibling file is cut to the CIDs in R1 ∪ R2, so the join in the sample sees the same matches as the full run.
- Cost: one pass to count N, then one pass per file that stops after R2. The output is about 3 million rows per file.
- The cut also checks the sort order of every line it reads. The full file's sort order is then checked by the full run itself (§4.1).
- `slice.json` records the ranges and N. The units of work (`inputs`) are the primary file only.

### 4.5 Gate rules

`src/episteme/ops/gate_rules/pubchem.json`, with a `serialize` and a `load` section. `gate.STAGE_MODULES` gets the `("pubchem", "serialize")` entry. The rules use the existing measures:

| Section | Rule | First value |
|---|---|---|
| serialize | `failed_inputs` | 0 |
| serialize | `duplicate_ids` | 0 |
| serialize | `empty_text_pct` | max 0 |
| serialize | `license_counts` | `public_domain` only |
| serialize | `extract_status_pct` | `partial` at most the value of the first real sample plus a margin; the 2026-09-16 2.4M-row run had 15.8% partial |
| serialize | `peak_rss_mb` | max 1,024 |
| load | `rows_loaded_equals_staging` | true |

New measure, only for pubchem: `max_part_rows` and `max_part_bytes`, read from the part markers, with the rules "at most `part_rows`" and "at most `part_bytes`". This shows that the caps are applied.

No speed rule: `rows_per_s` and `elapsed_s` are in the report only. The run time depends on the compute capacity available (decision, user, 2026-10-26).

The first values are confirmed on the first real sample, as was done for apollo (drift log, deferred item j).

## 5. Error handling

| Case | Behaviour |
|---|---|
| A CID is not an integer, or out of order, in any of the four files | `PubchemInputError`: the unit's failed marker names the file and the line; the stage exits non-zero; the kept parts stay. |
| A sibling file is missing | As today: the join runs without it, and the rows are `partial`. The sample gate catches a missing sibling through the `partial` rule. |
| The run stops (crash, reboot, Ctrl-C) | The next run resumes after the last part with a marker (§4.3). |
| A transient I/O error | As today: `TransientUnitError`, retried by the stage (standard §6). |

## 6. Testing

- Unit tests (not pg): the merge join on small fixtures (a sibling with gaps, a sibling with extra CIDs, a missing sibling, an empty CID); out-of-order and non-integer CIDs in the primary and in a sibling; parts by row cap and by byte cap; part names; resume after a crash in the middle of a part; resume with different caps (no lost and no duplicate CIDs); `--force`; the slice cut (ranges, siblings cut to the same CIDs, an unsorted line fails); the gate rules file loads; rows are identical to the DuckDB version on one fixture.
- pg tests on `episteme_test`: load of a multi-part staging directory; a reload of a rewritten part.
- Real data, after stage3-r1 ends (one heavy job at a time): `pubchem serialize --sample`, then `pubchem load --sample`. Then the full run, after the user's go-ahead.

## 7. Rollout

1. PR with the code, the rules and the docs (docs/10 pubchem section and §4.9 gate table, the drift log).
2. The user merges.
3. The sample serialize and load; set the first rule values from the report; commit the rules if they change.
4. The full `pubchem serialize`, then a plain `pubchem load` (§8 decision 2), with the user's go-ahead.

## 8. Decisions

1. No speed limit in the gate rules: the run time depends on the compute capacity available (user, 2026-10-26).
2. The full load is a plain `load`, not `load --bulk` (user, 2026-10-26; this replaces an earlier "use `--bulk`"). Reason: no index that `--bulk` drops is impacted by pubchem rows. `articles_pmid_idx`, `articles_pmcid_idx` and `articles_doi_idx` are partial (`WHERE … IS NOT NULL`), and pubchem sets those columns to NULL, so its rows get no entry. `articles_mesh_gin`, `articles_authors_gin` and `articles_publication_types_gin` get only a NULL entry. `--bulk` drops parent indexes, so it would force a rebuild over every source for no gain. The sample load (all indexes present) shows the load rate before the full run.
3. Backlog (drift log): `--bulk` could drop only the covered indexes on columns the loaded source fills (a per-source subset of `bulk_indexes.COVERED_INDEXES`). Dropping a whole parent index is legal, so no DDL change is needed. Not in scope here.
