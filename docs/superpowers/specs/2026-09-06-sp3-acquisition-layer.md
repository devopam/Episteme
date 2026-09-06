# SP3 — Acquisition Layer (spec-delta)

**Parent:** `docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md` — this is the
SP3 spec-delta. §4 of the roadmap is the frozen contract; this document refines
the parts §4 and the SP3 charter left to "the owning sub-plan" and records the
decisions from the 2026-09-06 brainstorm.

**Depends on:** SP1-β (merged to `main`, PR #1). `scripts/data/_lib/common.sh`
already exists with real `log` / `die` / `require_env` / `load_dotenv` /
`write_sync_stamp` and **stub** `discover_manifest` / `size_match_skip` /
`aria2_fetch` / `aws_sync`. SP3 replaces the stubs with the real engine.

**Scope in one line:** every wired source gets a working
`scripts/data/<source>/download_<source>.sh` in the nested layout, `.env`-driven,
no hardcoded endpoints, verifiable by `--dry-run` without the data disk.
**Download only** — no `extract_` / `serialize_` / `load_` for non-pmc sources
(those are SP2 literature / SP4 structured).

---

## 1. Architecture

Two layers, one entry point per source (brainstorm "Approach A").

```
scripts/data/
  _lib/
    common.sh          # SP1-β helpers + SP3 fetch engine (below)
    hf_download.sh      # hf_fetch REPO_ID REPO_TYPE DEST_DIR [REVISION]
    sources.env         # COMMITTED: every <SOURCE>_BASE default, KEY=value, no code
    check_prereqs.sh    # reports aria2c / aws|s5cmd / curl / hf presence
  run_pipeline.sh       # table-driven; dispatches `<source> download` for all 17
  <source>/download_<source>.sh   # ~20-40 line wrapper — THE entry point
  db/  materialize_corpus.sh  run_sample_audit.sh  verify_audit_trail.sh   # (unchanged from SP1-β)
```

- **Wrapper owns *what*:** its `<SOURCE>_BASE`, its file list / glob patterns /
  S3 URI / HF repo, its mode args. Contains **no** transfer mechanics and **no**
  literal host.
- **`_lib` owns *how*:** directory-listing URL resolution, HTTP HEAD size-match
  skip, the tuned `aria2c` / `s5cmd` / `aws` / `hf` invocations, `--dry-run`,
  prereq checks, sync-stamp, `.ok` markers.

**Two wrapper shapes, one layout:**
- **Bulk sources** (`bookshelf`, `chembl`, `uniprot`, `pubchem`, `clinvar`,
  `reactome`, `mesh`, `ontologies`, `openalex`, `hf_corpus`, `dailymed`,
  `openfda`, `aact`): wrapper → `_lib` fetch helper.
- **Python-backed** (`pmc` ✓, `pubmed`, `apollo`,
  `europepmc/{preprints,manuscripts,id_mappings,lite_metadata,abstracts}`):
  wrapper is `source _lib/common.sh; load_dotenv; require_env EPISTEME_ACTOR;
  exec "$PY" -m episteme.data.<source>.download_<source> "$@"` (the `$PY`
  discovery block from SP1-β's `scripts/data/pmc/*.sh`).

---

## 2. `scripts/data/_lib/sources.env` — single source of truth for endpoints

Plain `KEY=value`, committed, whole-project. Every endpoint the acquisition
layer touches lives here and **only** here.

```
# Literature / EPMC
PUBMED_FTP_BASE=https://ftp.ncbi.nlm.nih.gov/pubmed
PMC_S3_BUCKET=pmc-oa-opendata
EUROPEPMC_BASE=https://www.ebi.ac.uk/europepmc/webservices/rest
BOOKSHELF_BASE=https://ftp.ncbi.nlm.nih.gov/pub/litarch
APOLLO_HF_REPO=FreedomIntelligence/ApolloCorpus

# Structured
CHEMBL_BASE=https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest
UNIPROT_BASE=https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/complete
UNIPROT_MIRRORS=https://ftp.uniprot.org/...|https://ftp.expasy.org/...|https://ftp.ebi.ac.uk/...
PUBCHEM_BASE=https://ftp.ncbi.nlm.nih.gov/pubchem
CLINVAR_BASE=https://ftp.ncbi.nlm.nih.gov/pub/clinvar
REACTOME_BASE=https://reactome.org/download/current
MESH_BASE=https://nlmpubs.nlm.nih.gov/projects/mesh
ONTOLOGIES_BASE=https://data.bioontology.org        # or OBO/BioPortal per download_open_ontologies.sh
OPENALEX_S3=s3://openalex

# Volatile (download-only)
DAILYMED_BASE=https://dailymed-data.nlm.nih.gov/public-release-files
OPENFDA_CATALOG=https://api.fda.gov/download.json
AACT_DOWNLOADS=https://aact.ctti-clinicaltrials.org/static/exported_files
```
(Exact URLs are transcribed from the 16 batch scripts in scratch during
implementation — the list above is indicative.)

**Load order, both sides — last wins:** `sources.env` → `.env` → real environment.
- Bash: `common.sh` does `set -a; . "<lib>/sources.env"; set +a` then `load_dotenv`
  (which already skips keys already set). Real env vars set before the script
  runs are never clobbered.
- Python: `config.py` parses `sources.env` with its existing dotenv logic
  **before** `.env`, then `os.environ` still wins via `_get`. `config.py`
  stays the only Python `os.environ` reader.

Non-endpoint config (`PGHOST`, `EPISTEME_ACTOR`, `EPISTEME_DOWNLOAD_THREADS`,
secrets, `OM_*`) stays out of `sources.env` — that is `.env` / real-env only.

`.env.example`: the `<SOURCE>_BASE` block collapses to one line —
*"endpoint defaults live in `scripts/data/_lib/sources.env`; override any of them
here or in the real environment."*

---

## 3. `_lib` fetch engine — helper contracts

Frozen names from roadmap §4.6. `aria2_fetch` / `aws_sync` from §4.6 are kept as
**aliases** to the names below for backward-compat with the roadmap text;
`http_fetch` / `s3_sync` are the primary names.

### `http_fetch DEST_DIR [URL...]`  (URLs also accepted on stdin, one per line)
- Fetches each URL to `DEST_DIR/<basename-of-url>`.
- `aria2c` present → `aria2c -c -x16 -s16 -j"${EPISTEME_DOWNLOAD_THREADS:-4}"
  --max-tries=15 --retry-wait=30 --auto-file-renaming=false
  --allow-overwrite=true --file-allocation=none --console-log-level=warn
  -d "$DEST_DIR" -i <(the url list)`.
- `aria2c` absent → `curl -fL -C - --retry 15 --retry-delay 30 -o <dest>` per URL,
  sequential, with a one-time `log WARN "aria2c not found — sequential curl
  fallback (degraded throughput)"`.
- `EPISTEME_DRY_RUN=1` (set by `--dry-run`) → for each URL: `log INFO "DRY: would
  fetch <url> -> <dest>"` and, if `curl` present, a HEAD to print the size; no
  transfer, returns 0 even if no fetch tool is installed.
- Non-dry-run with neither `aria2c` nor `curl` → `die` with install hint.

### `s3_sync S3_URI DEST_DIR [-- passthrough args]`
- `s5cmd` present → `s5cmd --no-sign-request sync "$S3_URI/*" "$DEST_DIR/"`.
- else `aws` present → `aws s3 sync "$S3_URI" "$DEST_DIR" --no-sign-request`.
- `EPISTEME_DRY_RUN=1` → append the tool's dry-run flag (`--dry-run` for both) so
  it lists keys without transferring; if neither tool present, `log INFO "DRY:
  would sync <uri> -> <dir>"` and return 0.
- Non-dry-run with neither tool → `die` ("install s5cmd or awscli").

### `hf_fetch REPO_ID REPO_TYPE DEST_DIR [REVISION]`  (in `_lib/hf_download.sh`)
- `HF_HUB_ENABLE_HF_TRANSFER=1`; prefer `hf download`, fall back to
  `huggingface-cli download`; `--repo-type "$REPO_TYPE" --local-dir "$DEST_DIR"`
  `[--revision "$REVISION"]`.
- `REPO_TYPE` retry: if the first attempt fails and `REPO_TYPE=dataset`, retry as
  `model` (batch script behaviour) — but log both attempts.
- `EPISTEME_DRY_RUN=1` → `hf download --dry-run` if that flag exists on the
  installed version, else list files via `huggingface_hub` API through
  `python -c`, no download.
- Neither CLI present → `die "install the Hugging Face CLI (pip install -U
  'huggingface_hub[cli,hf_transfer]')"`.

### `size_match_skip LOCAL_PATH URL`
- Exit 0 (caller skips this URL) iff `LOCAL_PATH` exists, is non-empty, and its
  byte size equals the remote `Content-Length` from `curl -sSIL --connect-timeout
  20 --max-time 90 "$URL"`. Exit 1 otherwise (fetch it). No `curl` → exit 1
  (can't verify → fetch).
- Wrappers use this to prune a URL list before calling `http_fetch`; `http_fetch`
  itself does not re-check (aria2c's `-c` handles partial resume).

### `discover_manifest BASE_URL REGEX...`
- `curl -sSL "$BASE_URL/"` → extract `href="..."` filenames → strip query/anchor →
  for each `REGEX` arg, print the first matching filename (one per line, in the
  order the regexes were given). Used by `chembl` (versioned filenames) and any
  source whose file list isn't fixed. Sources with fixed filenames skip this.

### `resolve_dest SOURCE [SUBPATH]`
- Echoes `${EPISTEME_RAW_ROOT:-${EPISTEME_DATA_ROOT:-.}/01_raw}/SOURCE[/SUBPATH]`.
  Every wrapper's output-dir default goes through this — the string `01_raw`
  appears only here.

### `check_prereqs.sh`  (standalone script, also a function)
- Prints a table: `aria2c`, `s5cmd`, `aws`, `curl`, `hf`/`huggingface-cli`,
  `shellcheck` — present (with path) or MISSING (with the one-line install hint).
- `run_pipeline.sh <source> download` calls it once and `log WARN`s on anything
  missing that the chosen source needs, but only hard-fails on a real
  (non-dry-run) transfer with no capable tool.

---

## 4. `--dry-run` / `--max-files` / idempotency

- Every `download_<source>.sh` accepts `--dry-run` (sets `EPISTEME_DRY_RUN=1`,
  passed through `run_pipeline.sh`). Dry-run = resolve endpoints + list what would
  be fetched + HEAD size-checks where cheap; **zero bytes transferred; exit 0
  even with no fetch tool installed.**
- `--max-files N` (already in `run_pipeline.sh` from SP1-β) caps a wrapper's URL
  list to the first N entries after resolution — for sources that are one big
  dump (uniprot `sprot.dat.gz`), `--max-files 1` picks the smallest declared
  ancillary file (`LICENSE`, `reldate.txt`) so a real end-to-end fetch is cheap.
- Idempotency (roadmap §4.11): unit of work = one remote file. Re-run =
  `size_match_skip` prunes already-complete files; `aria2c -c` resumes partials.
  `write_sync_stamp "$DEST_DIR"` on success (`last_sync_utc.txt`). Bulk-download
  wrappers do **not** write per-file `.ok` markers (that is an extract/serialize
  concept); their "done" signal is the sync stamp + size-match on re-run.
- `--force` on a bulk wrapper → delete local files before re-fetch (still needs
  `--reason`, enforced by `run_pipeline.sh` as in SP1-β).

---

## 5. The 17 sources

| # | source | wrapper shape | engine | `sources.env` var(s) | mode / args | stage scope this SP |
|---|---|---|---|---|---|---|
| 1 | pmc | python exec | (SP1-β) | `PMC_S3_BUCKET` | — | full chain already wired; just confirm var wiring |
| 2 | pubmed | python exec | Python (existing) | `PUBMED_FTP_BASE` | `--since`, `baseline\|updates` | download + keep `pubmed/verify_pubmed.sh` (checksum verify/repair) |
| 3 | apollo | python exec | Python (existing, HF) | `APOLLO_HF_REPO` | — | download |
| 4 | europepmc/preprints | python exec | Python (existing) | `EUROPEPMC_BASE` | — | download; rebuild wrapper + module from batch `download_europepmc.sh` |
| 5 | europepmc/manuscripts | python exec | Python (existing) | `EUROPEPMC_BASE` | — | download; from batch `download_author_manuscripts.sh` |
| 6 | europepmc/id_mappings | python exec | Python (existing) | `EUROPEPMC_BASE` | — | download; from batch `download_epmc_id_mappings.sh` |
| 7 | europepmc/lite_metadata | python exec | Python (existing) | `EUROPEPMC_BASE` | — | download; from batch `download_epmc_lite_metadata.sh` |
| 8 | europepmc/abstracts | python exec | Python (existing) | `EUROPEPMC_BASE` | — | download (module already present) |
| 9 | bookshelf | bash → `http_fetch` | aria2c | `BOOKSHELF_BASE` | — | flat `scripts/download_bookshelf_oa.sh` → nested |
| 10 | chembl | bash → `discover_manifest`+`http_fetch` | aria2c | `CHEMBL_BASE` | `default\|all` | — |
| 11 | uniprot | bash → fixed list + `http_fetch` | aria2c + mirror probe | `UNIPROT_BASE`, `UNIPROT_MIRRORS` | swissprot only | — |
| 12 | pubchem | bash → `http_fetch` | aria2c | `PUBCHEM_BASE` | per batch script | — |
| 13 | clinvar | bash → `http_fetch` | aria2c | `CLINVAR_BASE` | per batch script | — |
| 14 | reactome | bash → `http_fetch` | aria2c | `REACTOME_BASE` | per batch script | — |
| 15 | mesh | bash → `http_fetch` | aria2c | `MESH_BASE` | year arg | — (feeds `article_mesh` in SP4) |
| 16 | ontologies | bash → `http_fetch` | aria2c | `ONTOLOGIES_BASE` | ontology-list arg | — |
| 17 | openalex | bash → `s3_sync` | s5cmd/aws | `OPENALEX_S3` | `works_jsonl\|works_parquet\|jsonl\|parquet\|full` | **wrapper wired now, all modes exposed, no default auto-run**; full-scope decision stays open for SP4 (roadmap §6.1) |
| — | hf_corpus | bash → `hf_fetch` | hf CLI | — (takes `<repo_id>`) | generic; `guidelines` + SFT/eval sets route through it |
| — | dailymed | bash → `http_fetch` | aria2c | `DAILYMED_BASE` | — | **download only, forever** |
| — | openfda | bash → `s3_sync`/`http_fetch` | aws/aria2c | `OPENFDA_CATALOG` | endpoint arg | **download only, forever** |
| — | aact | bash → `http_fetch` | aria2c | `AACT_DOWNLOADS` | — | **download only, forever** |

The 16 batch scripts (`…/scratchpad/acq-scripts/scripts/data/*.sh`) are the
**logic reference**: their transfer mechanics move into `_lib`, their URL /
pattern / mirror / mode knowledge moves into the wrappers. Not copied verbatim.

The **4 EPMC rewrites** in batch2 (`download_europepmc.sh`,
`download_author_manuscripts.sh`, `download_epmc_id_mappings.sh`,
`download_epmc_lite_metadata.sh`, authored 2026-08-31) are the normalisation
base for sources 4–7's Python modules **and** wrappers; the repo's
`scripts/download_epmc_*.sh` + any stale bits of the existing
`europepmc/*/download_*.py` are reconciled against them.

---

## 6. `run_pipeline.sh` expansion

- Table-driven source list (all 17 + the 3 volatile + `hf_corpus`).
- `<source> download [--dry-run] [--max-files N] [--force --reason "…"]` →
  `exec scripts/data/<source>/download_<source>.sh "$@"`. EPMC sub-feeds are
  addressed by the flat `article_schema.SOURCES` token where one exists
  (`europepmc_preprint`, `europepmc_manuscript`, `europepmc_lite`) and
  `europepmc_id_mappings` / `europepmc_abstracts` otherwise; the wrapper for
  each lives at `scripts/data/europepmc/<feed>/download_<feed>.sh` and
  `run_pipeline.sh` maps token → path.
- `<source> <extract|serialize|load|graph|materialize|enrich>`:
  - `pmc` → its existing chain.
  - anything else → `die "<source> <stage> is not in SP3 — SP2 (literature
    extract) / SP4 (structured serialize)"` (exit 3, not a stack trace).
- `<source> all` for non-pmc → runs `download` then the same graceful `die` for
  the next stage, so `all` is usable as "download everything wired" today.
- `run_start` / `run_end` audit bracket + `EPISTEME_RUN_ID` export: unchanged
  from SP1-β; a `download`-only run still gets its bracket.

---

## 7. Migration & retirement

- `git mv` each flat Python-backed caller (`scripts/download_pubmed_*.sh`,
  `scripts/download_apollo_corpus.sh`, `scripts/download_europepmc.sh`,
  `scripts/download_epmc_*.sh`, `scripts/download_author_manuscripts.sh`,
  `scripts/download_bookshelf_oa.sh`, `scripts/verify_pubmed_checksums.sh`,
  `scripts/repair_pubmed_failed_checksum.sh`, `scripts/pubmed_baseline_downloader.sh`)
  → `scripts/data/<source>/<verb>_<source>.sh`, then rewrite the body to the thin
  shape. `git mv` first so `git log --follow` survives.
- `git rm` flat `extract_*` callers that SP2 will re-create nested
  (`scripts/extract_pmc_oa_comm.sh` etc.) — SP3 does not add nested `extract_`
  wrappers, so these just go; SP2 adds the nested ones.
- `scripts/download_pmc_oa_comm.sh` + `scripts/__pycache__/download_pmc_oa_comm*.pyc`
  → remove (SP1-β already has `scripts/data/pmc/download_pmc.sh`).
- `.env.example` `<SOURCE>_BASE` block → one-line pointer to `sources.env`.
- No `src/episteme/` changes except: `config.py` load-order tweak
  (`sources.env` before `.env`) + any new `<SOURCE>_BASE` `Settings` field the
  Python-backed download modules actually read.

---

## 8. Prerequisites & CI

- **Runtime tools** (documented in the runbook + `check_prereqs.sh` hints):
  `aria2c` (HTTP/FTP segmented download), `s5cmd` *or* `awscli` (S3),
  `huggingface_hub[cli,hf_transfer]` (HF). None are bundled; a fresh machine
  installs them before a real download run. `--dry-run` works without any of
  them.
- **`shellcheck` via CI:** add `.github/workflows/shellcheck.yml` (first CI
  workflow in this repo) — `shellcheck` on `scripts/data/**/*.sh` +
  `scripts/*.sh`, on push + PR. Local `shellcheck` is optional (not installed
  here); CI is the gate.

---

## 9. Non-goals

- Any `extract_` / `serialize_` / `load_` / `graph_` / corpus work for a non-pmc
  source (SP2 literature, SP4 structured).
- `articles` rows or any downstream processing for `dailymed` / `openfda` /
  `aact` — ever, in Phase 0.
- The `openalex` full-scope decision (subset vs park vs citation-only) — SP4,
  roadmap §6.1. SP3 only wires its `download` wrapper.
- The `uniprot` licence call (ND → `commercial` vs `text_mining`) — SP4,
  roadmap §6.2.
- The `guidelines` source-identity question — SP2, roadmap §6.4.
- Actually running the multi-GB downloads (needs the data disk).
- A production OpenMetadata / any Stream-2 / RAG work.

---

## 10. Exit criteria

1. `scripts/data/<source>/download_<source>.sh --dry-run` (via
   `run_pipeline.sh <source> download --dry-run`) exits 0 for **all 17 wired
   sources + hf_corpus + dailymed + openfda + aact**, printing the resolved
   endpoint(s) and the file list it would fetch, transferring nothing, with
   `aria2c` / `aws` / `s5cmd` absent.
2. One **real small fetch** proves the live `http_fetch` (curl fallback path,
   since `aria2c` isn't installed here) end-to-end: e.g.
   `run_pipeline.sh uniprot download --max-files 1` pulls `reldate.txt` +
   `LICENSE` into `01_raw/uniprot/swissprot/`, `write_sync_stamp` writes
   `last_sync_utc.txt`, and a re-run reports both as size-matched skips.
3. `.github/workflows/shellcheck.yml` added and **green** on the branch.
4. Grep gate: `grep -REn 'ftp\.|s3://|https?://' scripts/data/` returns hits
   **only** in `_lib/sources.env` (and comments).
5. `scripts/data/_lib/sources.env` is the sole definition of every endpoint;
   `config.py` reads it before `.env`; a `python -c "from episteme.config import
   get_settings; ..."` confirms a `sources.env` value is picked up and that a
   `.env` / real-env override wins.
6. `run_pipeline.sh <source> extract` (or any non-download stage) for a non-pmc
   source exits 3 with the "SP2/SP4" message, not a traceback.
7. Flat superseded `scripts/*.sh` removed; nested equivalents present;
   `git log --follow scripts/data/pubmed/download_pubmed.sh` shows pre-rename
   history.
8. `pytest -q` green (SP3 adds no Python tests beyond a possible `config.py`
   `sources.env` load-order test); `pip install -e ".[data]"` import check clean.

---

## 11. Open items carried forward

| # | Item | Owner |
|---|---|---|
| 1 | `openalex` full ingest scope (subset / park / citation-only) | SP4 |
| 2 | `uniprot` CC BY-ND → `commercial` vs `text_mining` subset | SP4 |
| 3 | `guidelines` as its own `source` vs a bucket | SP2 |
| 4 | Install `aria2c` + `s5cmd` on the real acquisition machine before Phase-0 downloads | ops, pre-download |
| 5 | `ontologies` — which ontology set / registry (`download_open_ontologies.sh` specifics) | resolve during SP3 implementation from the batch script |

---

## Document control

| Version | Date | Notes |
|---|---|---|
| v1 | 2026-09-06 | SP3 spec-delta from the 2026-09-06 acquisition-layer brainstorm. |
