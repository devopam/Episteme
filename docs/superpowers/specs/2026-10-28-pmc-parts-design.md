# PMC in bounded parts

Status: draft for review, 2026-10-28.
Parent: `2026-10-19-pipeline-operating-standard-design.md` (the operating standard), in particular bounded units of work, resume, part caps and representative samples. Pattern: `2026-10-26-pubchem-parts-design.md`.

Notation: **Owner** marks a decision the project owner made. **Claude's call** marks a choice the owner has not yet confirmed.

## 1. Problem

- `extract_pmc` treats one metadata file (`PMC{id}.{ver}.json`, with its JATS XML) as one unit of work. Each unit writes one staging shard with one row, one success marker and one audit row. The loader then runs one transaction per shard.
- On disk now (counted 2026-10-28): 935,604 files in `01_raw/pmc/oa_comm/xml` and 935,573 in `metadata`. Listing the `xml` folder alone takes 32 s on the USB HDD.
- At that grain, a full run would make about 935k staging files and 935k marker files, in single folders, and about 935k load transactions. Measured on the europepmc_preprint extractor, which has the same per-file design: about 0.7 s per file with 3 workers. That projects to about 7–8 days to extract. The europepmc_preprint load of one-row shards measured about 0.4 s per shard (2026-10-28), which projects to about 4–5 days to load 935k shards.
- This breaks the bounded-unit rule from the other side to pubchem: the units are far too small, not too large.
- PMC has no gate rules and no sample slice, so it has never had a representative sample.
- `graph_builder.build` for `pmc` reads every PMC article row with one `fetchall()` before it groups them. At 935k rows that breaks the controlled-memory rule.

## 2. Goal

- Extract PMC in bounded parts. Each part is one staging shard, one success marker, one audit row and one load transaction.
- A restart redoes only the parts that are missing or whose input changed.
- PMC gets gate rules and a sample slice for `extract` and `load`, and the slice includes the largest part.
- The PMC graph phase reads its rows in bounded, per-part batches.

Not in scope: the per-article parsing (`row_from_meta_and_xml`, `parse_jats_fields`), the licence posture (unchanged), the downloader, the loader, and the text-duplication change (end-consumer programme, project 2).

## 3. Facts the design depends on

| Fact | Evidence |
|---|---|
| File names are `PMC{id}.{ver}.json` / `.xml`; the numeric id is stable, the version changes when the article is revised. The row id is `pmcid:PMC{id}`, without the version, so all versions of one article share one row id. | Files on disk, for example `PMC1043859.2.xml`; `extract_pmc.row_from_meta_and_xml`. |
| The metadata files are discovered recursively and sorted by `input_key`. | `extract_pmc.discover_meta_files`. |
| No PMC state exists in production: no `_ops/pmc` folder, no PMC staging. | Checked 2026-10-28. |
| The profile already resolves `part_rows` and `part_bytes`. pubchem uses them. | `src/episteme/ops/profile.py`; the pubchem design. |
| The loader replaces rows by `source_file` and removes id collisions across `source_file`s, so a part is a normal shard to it. | `postgres_loader.load_source_file`; the pubchem load. |
| The graph phase for `pmc` re-reads each article's raw JATS by `pmcid` to build citation and keyword edges. | `graph_builder.build`, `_find_xml`, `_parse_jats`. |

## 4. Design

### 4.1 Buckets by PMC id

- A **bucket** is a fixed range of numeric PMC ids. `bucket = id // BUCKET_IDS`. `BUCKET_IDS` defaults to 50,000 (Claude's call). PMC ids on disk run up to about 13.5 million (for example `PMC13525915`), which gives about 270 buckets (fewer non-empty). With 935k articles that averages about 3,500 articles per bucket.
- Bucket boundaries never move. A newly downloaded article always falls into the same bucket, so resume and change detection work per bucket, not per file position. (A part numbered by position, as pubchem does, would shift every later part when one file is added.)
- Within a bucket, files are processed in `input_key` order.
- **Versions:** when several versions of one article are in the same bucket, only the highest version is extracted. The others are counted in the part's stats as `superseded_versions`. All versions of one article share one id, so they always fall into one bucket.

### 4.2 Parts within a bucket

- A bucket's rows are written as one or more parts, each capped by `part_rows` and `part_bytes`. The byte rule is the same as pubchem's: a row that would take the part over `part_bytes` starts the next part. Only a single row larger than `part_bytes` can exceed it.
- The part's `source_file` and shard stem are `pmc_b{bucket:04d}__part{n:05d}`, for example `pmc_b0020__part00001` (Claude's call).
- Each part writes its staging shard (parquet), its success marker and one `extract_commit` audit row. The marker holds `n_rows`, `first_pmcid`, `last_pmcid`, `superseded_versions`, `extract_status_counts`, `part_rows`, `part_bytes`, the write info, and the bucket's input fingerprint (4.3).
- After a bucket's last part, the bucket gets its own success marker listing its parts and the total rows.
- Memory: one article parse plus one write batch. A part never holds the whole bucket's rows in memory: rows stream into the writer, as in #92.

### 4.3 Resume and change detection

- **Input fingerprint:** a hash over the bucket's sorted list of `(input_key, size, mtime)` for the metadata and XML files the bucket uses (Claude's call). It is computed from directory entries, without reading file contents.
- **Skip:** a bucket whose success marker exists and whose fingerprint matches is skipped, unless `--force` is given.
- **Redo:** a bucket whose fingerprint changed, for example after a new download, is extracted again as a whole. Its old parts are retired through the existing retire path (the pending list, delete by `source_file = ANY`, an audit row with `old_value`), as for pubchem.
- **Partial bucket:** a bucket with parts but no bucket marker (a stopped run) is extracted again from its first part. Buckets are small, so there is no resume inside a bucket (Claude's call).
- **Identity:** the marker key and stored `source_file` follow the project rule (`input_key`-based identity, with a collision test). Two buckets can never share a name; the test checks that two inputs mapping to the same bucket name under different raw roots do not collide.

### 4.4 Parallelism

- Units of work are buckets. Buckets run in the existing process pool (`run_per_file`), sized by the profile. The stop file reaches the workers, as in #90.
- Discovery walks the raw folder once, groups files by bucket in memory as `(bucket → list of paths)`, and hands each worker one bucket. Only paths are held, not contents. About 935k paths fit in a few hundred MB; project 2's measurement confirms it.

### 4.5 Graph phase

- `graph_builder.build` for `pmc` replaces the single `fetchall()` with a server-side (named) cursor ordered by `source_file`, read in fixed-size batches. It builds edges one `source_file` (one part) at a time. The build runs in ONE transaction (the named cursor needs it), so it does not commit per part. A long single graph transaction at 935k parses is a deferred item (drift log).
- Raw JATS is still re-read per article through `_find_xml(raw_dir, pmcid)`. Its directory index is built once per run, not per article.

### 4.6 Sample slice and gate rules

- **Slice:** `gate_slice` gains a `pmc` selector. It picks the largest bucket by total bytes of its XML files, one bucket near the median, and the bucket with the most superseded versions. The slice is hard-linked as for the other sources, under a cap of 4 GB (Claude's call; the same as apollo).
- **Rules** (`gate_rules/pmc.json`), Claude's call, to be checked on the first sample:
  - `extract`: `max_failed_inputs` 0, `max_duplicate_ids_in_shard` 0, `max_empty_text_pct` from the sample, `allowed_licenses` as today, `max_peak_rss_mb` 2048, `parts_within_caps`.
  - `load`: `max_orphan_bodies` 0, `rows_loaded_equals_staging`, `max_peak_rss_mb` 4096.

## 5. Error handling

- An unparseable article fails that article, not the bucket. It is recorded in the part's stats (`failed_articles`, with ids) and in `mark_failed` for the article key, as today. The bucket still completes, and the gate's `max_failed_inputs` counts failed articles.
- A transient read error on the USB HDD (`TransientUnitError`) retries the bucket under the existing retry policy.
- A permanent error in the bucket machinery (for example two different articles mapping to one row id within a bucket) fails the bucket fast and names it.

## 6. Testing

- Bucket assignment: ids on both sides of a boundary; the highest version wins; superseded versions are counted.
- Parts: the byte cap ends a part; one oversized row is its own part; parts never hold a whole bucket in memory (a streaming test, as in #92).
- Resume: a matching fingerprint skips the bucket; a changed fingerprint redoes it and retires the old parts; a partial bucket redoes from part 1.
- Identity: the collision test for bucket names.
- Graph: the PMC phase reads through a named cursor in batches; the per-part edge counts match the per-article result on a fixture.
- A sample run on the real slice before any full run; its peak memory and time per bucket go into the PR.

## 7. Rollout

- No PMC state exists, so there is nothing to migrate.
- The `extract` hash for `pmc` changes, and `pmc` becomes a gated source. Run `pmc extract --sample`, then `pmc load --sample`, then the full extract and load, one heavy job at a time.
- The graph hash changes, because `graph_builder` changes.

## 8. Decisions

| Decision | By |
|---|---|
| PMC moves to bounded parts before it runs | Owner (2026-10-28) |
| Fixed id buckets (`BUCKET_IDS` 50,000), not positional parts | Claude's call |
| Highest version wins within a bucket | Claude's call |
| Fingerprint from directory entries; redo a changed bucket as a whole | Claude's call |
| No resume inside a bucket | Claude's call |
| Slice: largest bucket, a median bucket, the most-versions bucket; 4 GB cap | Claude's call |
| Rule thresholds, to be checked on the first sample | Claude's call |
