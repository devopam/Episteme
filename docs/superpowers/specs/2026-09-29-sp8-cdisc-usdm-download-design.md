# SP8 — CDISC USDM source (download-only): design

**Date:** 2026-09-29
**Status:** Design approved in chat by the user (2026-09-29); this written spec awaits review.
**Depends on:** SP7 (`cdisc_ct`) and the SP6/SP7 drift close-out merged to `main`.

## 1. Goal

Add CDISC's Unified Study Definitions Model (USDM, repository `cdisc-org/DDF-RA`) as a **download-only** source, `cdisc_usdm`, following the `cdisc_bc` pattern. It closes the CDISC line of work that needs no licence decision: the files are fetched and kept with their provenance; nothing is serialized, loaded or included in any corpus.

**Success:** `run_pipeline.sh cdisc_usdm download` fetches the latest USDM release's `Deliverables/` folder into a release-named folder with `LICENSE`, `README.md` and `PROVENANCE.txt`; re-runs skip unchanged files; every other stage is refused.

## 2. Decisions (user, 2026-09-29)

- **Download-only.** No serializer and no loader. The internal-use licence exception for Biomedical Concepts and USDM was offered and declined; both stay download-only until CDISC confirms a licence for the data files. A later reader, if any, is a separate sub-project.
- **Scope:** the whole `Deliverables/` folder of the release (about 15 MB). Not the whole repository (about 183 MB, mostly diagrams and examples), not a hand-picked subset.
- **Versioning:** follow USDM's published releases, not the latest commit on `main`.

## 3. Facts the design rests on (verified 2026-09-29)

- Latest release: `v4.0.0`, published 2025-06-03 (`GET /repos/cdisc-org/DDF-RA/releases/latest` → `tag_name`). The repository has not been pushed since 2025-06-19.
- `GET /repos/cdisc-org/DDF-RA/git/trees/v4.0.0?recursive=1` returns the whole tree in one response: 158 entries, `"truncated": false`. Each blob entry has `path`, `type: "blob"` and `size`.
- `Deliverables/` at `v4.0.0`: 75 files, 15,250,261 bytes, in `API/` (OpenAPI JSON/YAML, version-diff files), `CT/` (`USDM_CT.xlsx`, diffs), `IG/` (`USDM-IG.pdf`, 5.9 MB), `RULES/` (CORE rules `.xlsx`, including a `USDM_V3.0/` subfolder) and `UML/` (`dataStructure.yml`, `dataDictionary.MD`, `USDM_UML.xmi`/`.qea`, version diffs, diagram PNGs under `UML_Views/`). No path under `Deliverables/` contains a space.
- Raw files are served at `https://raw.githubusercontent.com/cdisc-org/DDF-RA/<ref>/<path>`.
- Licence: `LICENSE` is MIT. The README says MIT covers "code and scripts" and CC-BY-4.0 covers "content files like documentation and minutes"; the model files are not named in either. Status: **unverified**, as for `cdisc_bc`.
- Unauthenticated GitHub API limit: 60 requests per hour.

## 4. Design

### 4.1 Download — `scripts/data/cdisc_usdm/download_cdisc_usdm.sh`

- `sources.env` gains `USDM_API_BASE=https://api.github.com/repos/cdisc-org/DDF-RA`, `USDM_RAW_BASE=https://raw.githubusercontent.com/cdisc-org/DDF-RA` and `USDM_REPO_URL=https://github.com/cdisc-org/DDF-RA`. No URL appears anywhere else under `scripts/data/` (grep gate stays empty).
- Flags exactly as `download_cdisc_bc.sh`: `--dry-run`, `--max-files N`, `--force`, `--reason R` (accepted and ignored; `run_pipeline.sh` enforces it), unknown `-x` flags warn, any positional argument dies ("this source has no modes").
- Steps, three GitHub API calls in total:
  1. `releases/latest` → `tag_name`. The tag must match `^v?[0-9]+(\.[0-9]+)*$`, otherwise die; it becomes a folder name.
  2. `commits/<tag>` → the 40-hex commit SHA, recorded in provenance. Every raw fetch uses this SHA, so the whole run is pinned to one commit even if the tag moves.
  3. `git/trees/<sha>?recursive=1`. Die if the response says `"truncated": true`. Keep entries with `type` `blob` whose `path` starts with `Deliverables/`; each path passes `_safe_rel` (unsafe paths are logged and skipped). Die if nothing is left.
- Any API failure dies with a message naming the likely cause (rate limit), as in `cdisc_bc`.
- Destination: `$(resolve_dest cdisc_usdm <tag>)`, for example `01_raw/cdisc_usdm/v4.0.0/Deliverables/API/USDM_API.json`. Folders of older releases are never touched.
- `--max-files` caps the resolved `Deliverables/` file list (in tree order) before any other step. `LICENSE` and `README.md` from the same commit always ride along and do not count against it.
- Re-fetch: `--force` removes each planned local file before fetching (never under `--dry-run`); otherwise `size_match_skip` skips files whose size matches. Fetching goes through `http_fetch` with `URL<TAB>relpath` lines.
- `PROVENANCE.txt` in the release folder, written when anything was fetched or it is missing, never under `--dry-run`: `source_repo`, `release_tag`, `commit_sha`, `retrieved_at`, `content_licence` (the MIT / CC-BY-4.0 wording above, "model files not covered; verify before redistribution") and `corpus_status` ("excluded — download-only; no serializer until CDISC confirms a licence for the model files").
- `write_sync_stamp` on the source root `01_raw/cdisc_usdm/`.
- Header comment in the style of `download_cdisc_bc.sh`: what is fetched, licence status, the unauthenticated rate limit (three calls per run, `--dry-run` included), and that a moved tag with same-size files needs `--force`.

### 4.2 Pipeline registration — `scripts/data/run_pipeline.sh`

- `[cdisc_usdm]="cdisc_usdm/download_cdisc_usdm.sh"` in the `WRAPPER` table only — not in `LIT_SOURCES` or `STRUCTURED_SOURCES` — so any stage other than `download` exits 3 with the existing "is not in SP" message, as for `cdisc_bc`.

### 4.3 Docs

- `docs/02-data-sources.md` §2.6: the USDM row becomes "Wired as `cdisc_usdm`, download-only", licence facts as in §3; backlog row updated; changelog row.
- `docs/09-extraction-contract.md`: a `cdisc_usdm` row beside `cdisc_bc` (no serializer, no rows, licence unverified, excluded from any corpus).
- `docs/10-data-sources-runbook.md`: new §5.7 "CDISC USDM (`cdisc_usdm`)" modelled on §5.5 (what is fetched, folder layout, provenance, bounded run, dry run, forced re-fetch, expected success output); `cdisc_usdm` added to the download-only row of the stage table and to the `--dry-run` network note; the "On upstream release" row mentions it.
- `docs/12-source-inventory.md`: a `cdisc_usdm` row.
- `CLAUDE.md`: the `cdisc_bc` download-only hard rule extends to `cdisc_usdm`.
- `docs/project-incubation-baseline.md`: SP8 entry.

## 5. Tests (all offline; run in the cloud sandbox too)

- `tests/test_cdisc_usdm_wrapper.py`, modelled on `tests/test_cdisc_bc_wrapper.py`: a fake `curl` on `PATH` serves canned `releases/latest`, `commits/<tag>` and tree JSON and raw files; `EPISTEME_RAW_ROOT` points at a temp folder. Cases:
  - a normal run writes only `Deliverables/` blobs (not tree entries, not files outside `Deliverables/`) plus `LICENSE`, `README.md`, `PROVENANCE.txt` under `<raw>/cdisc_usdm/<tag>/`, and every raw URL uses the commit SHA, not the tag;
  - `PROVENANCE.txt` carries tag, SHA and the licence and corpus-status lines;
  - `--max-files 2` fetches two data files plus `LICENSE` and `README.md`;
  - `--dry-run` writes nothing;
  - a second run fetches nothing (size skip); `--force` fetches again;
  - a tree with `"truncated": true` dies; an empty `Deliverables/` dies; a tag that fails the pattern (for example `../x`) dies; a `..` path is skipped with a warning;
  - a positional argument dies.
- `tests/test_run_pipeline_dispatch.py`: `cdisc_usdm` in the known-sources list; every non-download stage exits 3 with "is not in SP" (parametrised like `test_cdisc_bc_is_download_only`).
- Doc tests (`tests/docs/`): extend the existing source-inventory, runbook and extraction-contract checks so `cdisc_usdm` must appear, as `cdisc_bc` does.
- The `sources.env` grep gate stays empty.

## 6. Out of scope

- Any serializer, loader or corpus inclusion for USDM (needs CDISC's licence confirmation).
- `Documents/` (examples, mappings, diagrams) and the rest of the repository.
- Pruning older release folders.
- An authenticated GitHub token to lift the rate limit.
- The SDTM model and IG (Stream 2, needs a deployer CDISC licence).

## 7. Risks

- **Rate limit:** three unauthenticated calls per run; a shared IP may already be near the limit. The error message says so; retry after the hour.
- **Tag moved or re-published** with same-size files: the size skip misses it. Documented; use `--force --reason`.
- **Wrapper tests skip where `aria2c` is installed** (`http_fetch` prefers it and bypasses the fake `curl`), the same known limitation as the `cdisc_bc` and `cdisc_ct` wrapper tests (SP7 deferred list). The cloud sandbox has no `aria2c`, so the tests run there.
- **Tree response format:** the parser depends on GitHub's pretty-printed JSON field order, as `cdisc_bc` already does; the tests pin the parsed shape so a change fails loudly rather than fetching the wrong set.
