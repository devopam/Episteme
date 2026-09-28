# SP7 — CDISC Controlled Terminology source: design

**Date:** 2026-09-28
**Status:** Design approved in chat by the user (2026-09-28); this written spec awaits review.
**Depends on:** SP1–SP6 and the CDISC catalog entry (`docs/02-data-sources.md` §2.6) merged to `main`.

## 1. Goal

Add CDISC Controlled Terminology (CT), published by NCI Enterprise Vocabulary Services (EVS), as a structured Stream 1 source: download each quarterly release, serialize it into prose rows in `episteme.articles`, and load it so the corpus always holds the **current release only**. It is the one CDISC item cleared for training data (`docs/02` §2.6).

**Success:** a release downloads, serializes into readable per-codelist rows carrying the `public_domain` licence, loads without duplicates on re-runs, and a later quarter fully replaces the earlier one (including codelists CDISC retired).

## 2. Decisions (user, 2026-09-28)

- **Packages:** all five NCI publishes in the same format: SDTM, SEND, ADaM, Define-XML, Protocol. (CDASH terms ship inside the SDTM file.)
- **Row granularity:** one row per codelist per package; very large codelists split into numbered parts.
- **Versioning:** latest release only. Older release files stay on disk for provenance; only the newest is serialized; the load step retires the superseded release's rows.
- **Licence:** `public_domain` — a source-anchored governance override, like MeSH/PubChem/ClinVar (2026-09-19). NCI states CDISC Terminology is "free to use without licensing restrictions"; `license_raw` keeps that statement. Subset resolves to `commercial`.

## 3. Facts the design rests on (verified 2026-09-28)

- Files: `https://evs.nci.nih.gov/ftp1/CDISC/<Package>/<Package>%20Terminology.txt` (also `.odm.xml`, `.xls`, `.html` for SDTM). The download site is a JavaScript app: a missing path returns a 2,873-byte HTML fallback page with HTTP 200, not a 404.
- Format: tab-separated, UTF-8, 8 columns — `Code`, `Codelist Code`, `Codelist Extensible (Yes/No)`, `Codelist Name`, `CDISC Submission Value`, `CDISC Synonym(s)`, `CDISC Definition`, `NCI Preferred Term`. A **codelist header row** has an empty `Codelist Code`; a **term row** carries its codelist's code.
- Sizes (release of 2026-09-25): SDTM 13.8 MB — 1,222 codelists, 46,020 term rows, 26,965 unique concepts, 17,673 concepts in more than one codelist, largest codelists 2,513 terms (~548k characters), median ~3,300 characters. SEND 4.2 MB (184 codelists, shares SDTM's largest lists). ADaM, Define-XML, Protocol are each under 150 KB.
- **Release dates differ per package**: `Last-Modified` is 2026-09-25 for SDTM/SEND/ADaM/Define-XML but 2026-07-11 for Protocol. Release handling must therefore be per package, never one shared release folder.
- The file carries no release date in its content; `Last-Modified` is the only date available without the ODM/XLS variants.

## 4. Design

### 4.1 Download — `scripts/data/cdisc_ct/download_cdisc_ct.sh`

- `CDISC_CT_BASE=https://evs.nci.nih.gov/ftp1/CDISC` added to `scripts/data/_lib/sources.env` only (grep gate stays empty).
- For each package: `HEAD` the `.txt` URL and require `Content-Type: text/plain`. The fallback page answers HTTP 200 with `Content-Type: text/html` (verified 2026-09-28), so a missing file is detected by content type, never by status code; a package whose file is missing or HTML is logged as a warning and skipped. Derive the release date (`YYYY-MM-DD`) from `Last-Modified`; after download, also check the file's first line is the expected 8-column header.
- Destination: `01_raw/cdisc_ct/<Package>/<release-date>/<Package>_Terminology.txt` (no spaces in local names). Fetch only if that file is absent or its size differs (common.sh size-skip).
- `PROVENANCE.txt` per release folder: source URL, `Last-Modified`, retrieved-at, and NCI's licence statement.
- Flags as in the other wrappers: `--dry-run` (no writes), `--max-files N` (packages, in fixed order SDTM, SEND, ADaM, Define-XML, Protocol), `--force`. Registered in `run_pipeline.sh`'s `WRAPPER` table.
- Older release folders are never deleted.

### 4.2 Serializer — `src/episteme/data/cdisc_ct/serialize_cdisc_ct.py`

- Frozen contract: `serialize_cdisc_ct(raw_dir, processed_dir, *, max_files=0, force=False, workers=1, verbose=False) -> {"inputs", "ok", "failed", "rows"}`; audit event `serialize_commit`; `_best_effort_audit` with the SP6 logged double-failure fallback.
- **Discovery:** for each package directory under `raw_dir`, pick the newest `YYYY-MM-DD` subfolder and its `<Package>_Terminology.txt`. Older release folders are ignored.
- **Identity:** `source_file = input_key(path, raw_dir)` (e.g. `SDTM__2026-09-25__SDTM_Terminology.txt`), used for the checkpoint marker and the stored row. A new release therefore gets a new marker and a new shard automatically.
- **Parsing:** header row validated against the 8 expected columns (fail the file with `mark_failed` otherwise). Codelist header rows open a codelist; term rows attach to their codelist by code. Term rows whose codelist header is missing are counted and skipped. Rows missing a `Code` are skipped and counted (`skipped_no_id`).
- **Row shape:** one row per codelist per package (split when needed):
  - `id = f"cdisc_ct:{package}:{codelist_code}:p{n}"` (always carries the part number, so ids are stable whether or not a list is split); `source_record_id = f"{package}:{codelist_code}:p{n}"`.
  - `title`: codelist name (with "part n of m" when split).
  - `text`: package and release date; codelist name, submission value, NCI code, extensible yes/no, definition, NCI preferred term; then one line per term: submission value, NCI code, preferred term, synonyms, definition.
  - Split codelists into parts of at most 200 terms; each part repeats the codelist header so it stands alone.
  - `license = public_domain` (override after `normalize_license`, per §2), `license_raw` = NCI statement, subset `commercial`.
- `cdisc_ct` added to `article_schema.SOURCES`.

### 4.3 Load — `scripts/data/cdisc_ct/load_cdisc_ct.sh` and a retire step

- `cdisc_ct` joins `STRUCTURED_SOURCES` in `run_pipeline.sh` (download → serialize → load; no graph stage).
- `load_cdisc_ct.sh` runs `episteme.data.load_articles --source cdisc_ct`, then a retire step.
- **Retire step:** new reusable `postgres_loader.retire_source_files(conn, *, source, keep_source_files) -> int` deletes `episteme.articles` and matching `article_body` rows for `source` whose `source_file` is not in `keep_source_files`, in the caller's transaction, and records one `load_replace` audit event with the deleted count. For `cdisc_ct`, `keep_source_files` is the newest `source_file` per package that has a successful load marker; if any package's newest shard is not yet loaded, the retire step does nothing and says so (never retire a package's old rows before its new rows are in).
- This is the only database-touching part; it runs and is tested locally.

### 4.4 Docs

`docs/02` §2.6 status (wired), a `docs/12` inventory row, a `docs/10` runbook section (first-time and quarterly blocks), and `docs/09` licence vocabulary note.

## 5. Tests

- **Serializer (non-DB), from a small fixture file** built from real rows: header validation; codelist/term assembly; part splitting at 200 terms with repeated headers; id stability; the same codelist in SDTM and SEND gets distinct ids and both rows are emitted; newest-release-per-package discovery (including a package whose newest release date differs from the others); restartability (a re-run skips via marker; a new release folder is processed); licence fields.
- **Download wrapper (non-DB)**, with a fake `curl` shim like `tests/test_cdisc_bc_wrapper.py`: URL construction, release-date derivation from `Last-Modified`, HTML-fallback rejection, per-package folders, size-skip, `--dry-run` writes nothing, `--max-files`.
- **Retire step (pg, local):** rows from an older release are deleted, rows in `keep_source_files` survive, `article_body` follows, the audit event is recorded, and it is a no-op when a package's new shard is not loaded.
- `tests/docs` updated where the inventory/catalog tests pin sources.

## 6. Out of scope

The ODM-XML/XLS variants; release history in the corpus; any graph edges; CDISC Library API; Biomedical Concepts, USDM and SDTM IG (see `docs/02` §2.6).

## 7. Risks

- **HTML fallback returning HTTP 200** could otherwise be saved as a "release": the wrapper rejects it explicitly and a test pins that.
- **`Last-Modified` changing without new content** creates a new release folder; harmless (rows are replaced with identical text and the retire step removes the old ones), at the cost of one re-serialize.
- **Very large codelists:** split into parts; part count can change between releases, which the retire step handles.
- **Duplicate text** for codelists shared by SDTM and SEND: accepted; the corpus dedup step can catch it.
