# SP3 — Acquisition Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every wired data source gets a working, `.env`-driven
`scripts/data/<source>/download_<source>.sh` in the nested layout, backed by a
real fetch engine in `_lib/`, verifiable end-to-end by `--dry-run` without the
data disk or any download tool installed.

**Architecture:** Two layers — thin per-source wrappers (own *what* to fetch) over
a shared `_lib` fetch engine (owns *how*). Single committed `_lib/sources.env`
holds every endpoint. Two wrapper shapes: bash→`_lib` helper for bulk sources,
`exec python -m` for the already-Python sources. Download only; extract/serialize
is SP2/SP4.

**Tech Stack:** bash (`set -uo pipefail`), `aria2c` / `s5cmd` / `aws` / `hf` as
runtime prereqs (not bundled — `--dry-run` works without them), `curl` fallback,
GitHub Actions (`shellcheck`), Python 3.10+ (`config.py` load-order only).

**Spec:** `docs/superpowers/specs/2026-09-06-sp3-acquisition-layer.md` (SP3
spec-delta) — refines `docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md`
§4 (frozen) + the SP3 charter. Read both.

## Global Constraints

- **Branch:** `sp3-acquisition-layer` (already checked out; the spec landed on it as `96ed825`).
- **`config.py` is the ONLY module in `src/episteme/` that reads `os.environ` / `os.getenv`.** SP3's only Python change is `config.py`'s load order.
- **Every endpoint default lives in `scripts/data/_lib/sources.env` and NOWHERE else** — not duplicated in `config.py`, `common.sh`, or any wrapper. `grep -REn 'ftp\.|s3://|https?://' scripts/data/` must hit only `sources.env` (and code comments).
- **Load order, both sides, last wins:** `sources.env` → `.env` → real environment. A var already set in the real environment is never clobbered.
- **`--dry-run` (`EPISTEME_DRY_RUN=1`) must succeed with `aria2c`, `aws`, `s5cmd`, `hf` ALL absent** (they are, on this machine): resolve endpoints, list what would be fetched, `curl` HEAD where cheap, transfer nothing, exit 0.
- **Download only.** No `extract_` / `serialize_` / `load_` / `graph_` wrappers or modules for any non-`pmc` source. `run_pipeline.sh <non-pmc-source> <non-download-stage>` → `die "<source> <stage> is not in SP3 — SP2 (literature) / SP4 (structured)"` exit 3, never a traceback.
- **Volatile sources** (`dailymed`, `openfda`, `aact`): a `download_<source>.sh` wrapper and nothing else, ever.
- **Nested layout:** `scripts/data/<source>/download_<source>.sh`. EPMC sub-feeds: `scripts/data/europepmc/<feed>/download_<feed>.sh`.
- **Wrapper = thin** (~20–40 lines): `source "$HERE/../_lib/common.sh"` → `load_dotenv` → `require_env EPISTEME_ACTOR` → declare source-specific what-to-fetch → call an `_lib` helper OR `exec "$PY" -m episteme.data.<source>.download_<source> "$@"`. No transfer mechanics, no literal host.
- **`bash -n` clean on every `.sh` touched.** `shellcheck` runs in CI (Task 11); not installed locally.
- **History-preserving migration:** `git mv` a file before rewriting its body; `git rm` only genuinely superseded files.
- **`pytest -q -m "not pg"` stays green (40 passed).** With `.env` sourced + `TEST_PG_DSN`, `pytest -q` stays green (46 passed, 1 skipped). SP3 adds at most one non-pg test (Task 1's load-order check).
- **`pip install -e ".[data]"` import check clean** at the end.
- **Batch-script logic reference:** the 16 original scripts are staged at `.superpowers/sdd/2026-09-06-sp3-acquisition-layer/batch-scripts/` (gitignored, survives the plan's execution). They are the source of each wrapper's URL patterns / modes / mirror lists — **their transfer mechanics move into `_lib`; they are not copied verbatim.**
- **Python for anything Python:** `.venv/Scripts/python.exe`.
- **Commits end with:**
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```

---

## File-structure map

**Created — `_lib`:**
- `scripts/data/_lib/sources.env` — committed endpoint defaults (KEY=value, no code)
- `scripts/data/_lib/hf_download.sh` — `hf_fetch REPO_ID REPO_TYPE DEST_DIR [REVISION]`
- `scripts/data/_lib/check_prereqs.sh` — tool-presence reporter

**Modified — `_lib` / orchestration / config:**
- `scripts/data/_lib/common.sh` — source `sources.env`; replace the 4 stub helpers with real `http_fetch` / `s3_sync` / `size_match_skip` / `discover_manifest`; add `resolve_dest`; `EPISTEME_DRY_RUN` plumbing. Keep `aria2_fetch`/`aws_sync` as thin aliases.
- `scripts/data/run_pipeline.sh` — table-driven source→wrapper map; `download` for all sources; graceful `die` for non-download non-pmc stages.
- `src/episteme/config.py` — parse `sources.env` before `.env`.
- `.env.example` — collapse the `<SOURCE>_BASE` block to a one-line pointer.

**Created — wrappers (bulk, bash→`_lib`):**
`scripts/data/{chembl,uniprot,pubchem,clinvar,reactome,mesh,ontologies,openalex,bookshelf,hf_corpus,dailymed,openfda,aact}/download_<source>.sh`

**Created — wrappers (Python-exec):**
`scripts/data/pubmed/download_pubmed.sh`, `scripts/data/pubmed/verify_pubmed.sh`,
`scripts/data/apollo/download_apollo.sh`,
`scripts/data/europepmc/{preprints,manuscripts,id_mappings,lite_metadata,abstracts}/download_<feed>.sh`

**Modified — Python download modules (endpoint wiring + EPMC reconciliation):**
`src/episteme/data/europepmc/{preprints,manuscripts,id_mappings,lite_metadata,abstracts}/download_*.py`,
`src/episteme/data/pubmed/download_pubmed.py`, `src/episteme/data/apollo/download_apollo.py`
— read `<SOURCE>_BASE` from `get_settings()`; reconcile against the batch EPMC rewrites.

**Created — CI:**
- `.github/workflows/shellcheck.yml`

**Removed (git rm / git mv):** flat `scripts/download_*.sh`, `scripts/extract_*.sh`, `scripts/pubmed_baseline_downloader.sh`, `scripts/{verify,repair}_pubmed*.sh`, `scripts/__pycache__/` (see Task 10).

---

## Task 1: `sources.env` + load-order (bash + `config.py`)

**Files:**
- Create: `scripts/data/_lib/sources.env`
- Modify: `scripts/data/_lib/common.sh`, `src/episteme/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `scripts/data/_lib/sources.env` (every `<SOURCE>_BASE`); `common.sh` sources it before `load_dotenv`; `config.py` parses it before `.env`. Consumed by every later task's wrapper and by the Python download modules.

- [ ] **Step 1: Write `scripts/data/_lib/sources.env`**

Transcribe every base URL from `.superpowers/sdd/2026-09-06-sp3-acquisition-layer/batch-scripts/*.sh` (the `BASE=` / `BASE_URL=` / `BASE_HTTPS=` / `MIRRORS=` lines) plus the ones already in `config.py` (`ncbi_ftp_host`, `pmc_s3_bucket`, `ebi_ftp_host`, `europepmc_base_url`, `apollo_hf_repo`). Format — plain `KEY=value`, one per line, a comment header per group, no code, no trailing spaces:

```
# Episteme acquisition endpoints — single source of truth (SP3).
# Load order everywhere: this file -> .env -> real environment (last wins).
# Override any value in ./.env or the real environment; never edit a wrapper.

# --- Literature / Europe PMC ---
PUBMED_FTP_BASE=https://ftp.ncbi.nlm.nih.gov/pubmed
PMC_S3_BUCKET=pmc-oa-opendata
EUROPEPMC_BASE=https://www.ebi.ac.uk/europepmc/webservices/rest
BOOKSHELF_BASE=https://ftp.ncbi.nlm.nih.gov/pub/litarch
APOLLO_HF_REPO=FreedomIntelligence/ApolloCorpus

# --- Structured databases ---
CHEMBL_BASE=https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest
UNIPROT_BASE=https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/complete
UNIPROT_MIRRORS=https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/complete|https://ftp.expasy.org/databases/uniprot/current_release/knowledgebase/complete|https://ftp.ebi.ac.uk/pub/databases/uniprot/current_release/knowledgebase/complete
PUBCHEM_BASE=https://ftp.ncbi.nlm.nih.gov/pubchem
CLINVAR_BASE=https://ftp.ncbi.nlm.nih.gov/pub/clinvar
REACTOME_BASE=https://reactome.org/download/current
MESH_BASE=https://nlmpubs.nlm.nih.gov/projects/mesh
MESH_FTP_BASE=https://ftp.nlm.nih.gov/online/mesh
OBO_PURL_BASE=http://purl.obolibrary.org/obo
OPENALEX_S3=s3://openalex

# --- Volatile (download-only) ---
DAILYMED_BASE=https://dailymed-data.nlm.nih.gov/public-release-files
OPENFDA_CATALOG=https://api.fda.gov/download.json
AACT_DOWNLOADS=https://aact.ctti-clinicaltrials.org/static/exported_files
```
(Use the ACTUAL URLs from the batch scripts where they differ from the above — the batch scripts win. `download_open_ontologies.sh` uses per-ontology PURLs; capture only the shared `OBO_PURL_BASE` here, the ontology list stays in that wrapper as data.)

- [ ] **Step 2: `common.sh` sources `sources.env` before `load_dotenv`**

In `scripts/data/_lib/common.sh`, near the top (after the `set -o pipefail` line, before any function definition), add:

```bash
# Endpoint defaults — sourced before load_dotenv so ./.env and the real
# environment both override. `set -a` exports every assignment.
_COMMON_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$_COMMON_LIB_DIR/sources.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$_COMMON_LIB_DIR/sources.env"
    set +a
fi
```
This runs at *source* time. `load_dotenv` (called explicitly by each wrapper) already skips keys already set — but a `sources.env` value must be overridable by `.env`, so `load_dotenv`'s "real env wins" check would wrongly block it. Fix `load_dotenv`: it must overwrite a value that came from `sources.env` but NOT one that came from the real pre-script environment. Simplest reliable approach — record the pre-`sources.env` environment:

```bash
# capture BEFORE sourcing sources.env (add just above the sources.env block):
_COMMON_REAL_ENV_KEYS="$(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p')"
```
Then in `load_dotenv`, replace the `[ -n "${!key:-}" ] && continue` guard with:

```bash
# real pre-script env wins; a value from sources.env does NOT block the .env override
case " $_COMMON_REAL_ENV_KEYS " in *" $key "*) continue ;; esac
```

- [ ] **Step 3: `config.py` parses `sources.env` before `.env`**

In `src/episteme/config.py`, inside `get_settings()`, before the existing `load_dotenv(dotenv_path)` call. Add a helper and call it:

```python
def _load_sources_env() -> None:
    """Load scripts/data/_lib/sources.env (committed endpoint defaults) into the
    process env, BEFORE the project .env and without clobbering anything already
    set. config.py stays the only os.environ *reader*; this is a one-time
    populate so _get() sees the defaults."""
    here = Path(__file__).resolve()
    for directory in (here.parent, *here.parents):
        cand = directory / "scripts" / "data" / "_lib" / "sources.env"
        if cand.is_file():
            for raw in cand.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
            return
```
Call `_load_sources_env()` as the first line of `get_settings()`. `os.environ.setdefault` = "don't clobber real env"; the later `.env` `load_dotenv` (python-dotenv, `override=False` by default) then does NOT override either — **so `.env` would lose to `sources.env`**, which is backwards. Fix: call `load_dotenv(dotenv_path, override=True)` for the project `.env` specifically (it's trusted, it's the user's intended override), keeping `_load_sources_env()`'s `setdefault` for the committed defaults. Net precedence: real env (setdefault skips) > `.env` (override=True) > `sources.env` (setdefault). Verify this precedence in the test below.

- [ ] **Step 4: Write the failing test** — `tests/test_config.py`

```python
def test_sources_env_load_order(tmp_path, monkeypatch):
    # sources.env default is visible
    import importlib
    import episteme.config as cfg

    monkeypatch.delenv("CHEMBL_BASE", raising=False)
    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    s = cfg.get_settings()
    assert (
        getattr(s, "chembl_base", None)
        == "https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest"
    )

    # real environment overrides sources.env
    monkeypatch.setenv("CHEMBL_BASE", "https://mirror.example/chembl")
    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().chembl_base == "https://mirror.example/chembl"
```

- [ ] **Step 5: Add the `Settings` fields the test + the Python download modules need**

`config.py`: add to `Settings` (frozen dataclass) and populate in `get_settings()` via `_get`:
`chembl_base`, `uniprot_base`, `pubchem_base`, `clinvar_base`, `reactome_base`, `mesh_base`, `pubmed_ftp_base`, `europepmc_base` (rename/alias the existing `europepmc_base_url`), `bookshelf_base`, `apollo_hf_repo` (exists), `pmc_s3_bucket` (exists), `openalex_s3`, `dailymed_base`, `openfda_catalog`, `aact_downloads`. Each `_get("<VAR>", "<same default as sources.env>")` — the `sources.env` populate makes the `_get` default a belt-and-braces duplicate; that is acceptable ONLY in `config.py` (the spec's "nowhere else" is about `scripts/data/**`). Prefer `_get("<VAR>")` with NO default and rely on `_load_sources_env()`, to keep truly one copy — decide during implementation and note which you chose in the report.

- [ ] **Step 6: Run** — `bash -n scripts/data/_lib/common.sh`; `.venv/Scripts/python.exe -m pytest -q tests/test_config.py`; `.venv/Scripts/python.exe -m pytest -q -m "not pg"` (→ 40 or 41 passed, report which). `bash -c 'set -a; . scripts/data/_lib/sources.env; set +a; echo "$CHEMBL_BASE"'` prints the URL.

- [ ] **Step 7: Commit** — `feat(sp3): sources.env single-source endpoints + config.py/_lib load order`

---

## Task 2: `_lib` HTTP fetch engine

**Files:** Modify `scripts/data/_lib/common.sh`.

**Interfaces:**
- Produces: `http_fetch DEST_DIR [URL...]` (URLs also on stdin), `size_match_skip LOCAL_PATH URL`, `discover_manifest BASE_URL REGEX...`, `resolve_dest SOURCE [SUBPATH]`, and `EPISTEME_DRY_RUN` handling. `aria2_fetch` kept as an alias to `http_fetch`. Consumed by every bulk wrapper (Tasks 5–8).

- [ ] **Step 1: `resolve_dest`**

```bash
resolve_dest() { # resolve_dest SOURCE [SUBPATH] -> <raw root>/SOURCE[/SUBPATH]
    local src="$1" sub="${2:-}"
    local root="${EPISTEME_RAW_ROOT:-${EPISTEME_DATA_ROOT:-.}/01_raw}"
    printf '%s\n' "$root/$src${sub:+/$sub}"
}
```
The string `01_raw` appears ONLY here in `scripts/data/**`.

- [ ] **Step 2: `size_match_skip`**

```bash
size_match_skip() { # exit 0 => caller SKIPS this url (local matches remote size)
    local local_path="$1" url="$2"
    [ -s "$local_path" ] || return 1
    command -v curl >/dev/null 2>&1 || return 1   # can't verify -> fetch
    local remote_len local_len
    remote_len="$(curl -sSIL --connect-timeout 20 --max-time 90 "$url" 2>/dev/null \
        | tr -d '\r' | awk 'tolower($1)=="content-length:"{print $2}' | tail -n1)"
    local_len="$(wc -c < "$local_path" | tr -d '[:space:]')"
    [ -n "$remote_len" ] && [ "$remote_len" = "$local_len" ]
}
```

- [ ] **Step 3: `discover_manifest`**

```bash
discover_manifest() { # discover_manifest BASE_URL REGEX... -> first filename matching each regex, one per line
    local base="$1"; shift
    local listing
    listing="$(curl -sSL --fail --connect-timeout 30 --max-time 120 "$base/" 2>/dev/null)" || {
        log ERROR "discover_manifest: cannot list $base/"
        return 1
    }
    local names
    names="$(printf '%s\n' "$listing" \
        | grep -oE 'href="[^"]+"' | sed -E 's/href="//; s/"$//' \
        | sed 's/[?#].*$//' | grep -vE '^(\?|/|\.\./|#|https?:)' | sort -u)"
    local re
    for re in "$@"; do
        printf '%s\n' "$names" | grep -E "$re" | head -n1 || true
    done
}
```

- [ ] **Step 4: `http_fetch` (+ `EPISTEME_DRY_RUN`, + `aria2_fetch` alias)**

Accepts `DEST_DIR` then URLs as args or on stdin. Each line may be a bare URL (→ `DEST_DIR/<basename>`) or `URL<TAB>relpath` (→ `DEST_DIR/<relpath>`, `mkdir -p` its parent).

```bash
http_fetch() {
    local dest_dir="$1"; shift
    mkdir -p "$dest_dir"
    local -a urls=()
    if [ "$#" -gt 0 ]; then urls=("$@"); else mapfile -t urls; fi
    [ "${#urls[@]}" -gt 0 ] || { log INFO "http_fetch: nothing to fetch"; return 0; }

    if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
        local line url rel
        for line in "${urls[@]}"; do
            url="${line%%$'\t'*}"; rel="${line#*$'\t'}"; [ "$rel" = "$line" ] && rel="$(basename "$url")"
            log INFO "DRY: would fetch $url -> $dest_dir/$rel"
            command -v curl >/dev/null 2>&1 && size_match_skip "$dest_dir/$rel" "$url" \
                && log INFO "DRY:   (already size-matched, would skip)"
        done
        return 0
    fi

    if command -v aria2c >/dev/null 2>&1; then
        local aria_in; aria_in="$(mktemp)"
        local line url rel
        for line in "${urls[@]}"; do
            url="${line%%$'\t'*}"; rel="${line#*$'\t'}"; [ "$rel" = "$line" ] && rel="$(basename "$url")"
            printf '%s\n  dir=%s\n  out=%s\n' "$url" "$dest_dir/$(dirname "$rel")" "$(basename "$rel")" >> "$aria_in"
        done
        aria2c -c -x16 -s16 -j"${EPISTEME_DOWNLOAD_THREADS:-4}" \
            --max-tries=15 --retry-wait=30 --auto-file-renaming=false \
            --allow-overwrite=true --file-allocation=none --console-log-level=warn \
            -i "$aria_in"
        local rc=$?; rm -f "$aria_in"; return $rc
    fi

    command -v curl >/dev/null 2>&1 || die "no download tool: install aria2c (or curl for the slow path)"
    log WARN "aria2c not found — sequential curl fallback (degraded throughput)"
    local line url rel
    for line in "${urls[@]}"; do
        url="${line%%$'\t'*}"; rel="${line#*$'\t'}"; [ "$rel" = "$line" ] && rel="$(basename "$url")"
        mkdir -p "$dest_dir/$(dirname "$rel")"
        curl -fL -C - --retry 15 --retry-delay 30 -o "$dest_dir/$rel" "$url" || die "curl failed: $url"
    done
}
aria2_fetch() { http_fetch "$@"; }   # roadmap §4.6 name kept as an alias
```

- [ ] **Step 5: Verify**

- `bash -n scripts/data/_lib/common.sh`.
- `bash -c 'source scripts/data/_lib/common.sh; resolve_dest chembl'` → `./01_raw/chembl`.
- `bash -c 'EPISTEME_RAW_ROOT=/tmp/x source scripts/data/_lib/common.sh; resolve_dest chembl sub'` → `/tmp/x/chembl/sub`.
- `bash -c 'source scripts/data/_lib/common.sh; EPISTEME_DRY_RUN=1 http_fetch /tmp/dl https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/complete/reldate.txt'` → prints `DRY: would fetch ...`, creates no file.
- `bash -c 'source scripts/data/_lib/common.sh; size_match_skip /nonexistent https://example.com/ ; echo $?'` → `1`.
- `bash -c 'source scripts/data/_lib/common.sh; discover_manifest https://reactome.org/download/current "\.txt$"'` → prints at least one `.txt` filename (real network).

- [ ] **Step 6: Commit** — `feat(sp3): _lib http_fetch/size_match_skip/discover_manifest/resolve_dest engine`

---

## Task 3: `_lib` S3 + HF engines + `check_prereqs.sh`

**Files:** Modify `scripts/data/_lib/common.sh`; create `scripts/data/_lib/hf_download.sh`, `scripts/data/_lib/check_prereqs.sh`.

- [ ] **Step 1: `s3_sync` in `common.sh` (+ `aws_sync` alias)**

```bash
s3_sync() { # s3_sync S3_URI DEST_DIR [-- extra passthrough args]
    local uri="$1" dest="$2"; shift 2 || true
    local -a extra=(); [ "${1:-}" = "--" ] && { shift; extra=("$@"); }
    mkdir -p "$dest"
    if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
        if command -v s5cmd >/dev/null 2>&1; then
            s5cmd --no-sign-request cp --dry-run "$uri/*" "$dest/" || true
        elif command -v aws >/dev/null 2>&1; then
            aws s3 sync "$uri" "$dest" --no-sign-request --dryrun "${extra[@]}" || true
        else
            log INFO "DRY: would sync $uri -> $dest"
        fi
        return 0
    fi
    if command -v s5cmd >/dev/null 2>&1; then
        s5cmd --no-sign-request sync "$uri/*" "$dest/"
    elif command -v aws >/dev/null 2>&1; then
        aws s3 sync "$uri" "$dest" --no-sign-request "${extra[@]}"
    else
        die "no S3 tool: install s5cmd (fast) or awscli"
    fi
}
aws_sync() { s3_sync "$@"; }
```

- [ ] **Step 2: `scripts/data/_lib/hf_download.sh`**

```bash
#!/usr/bin/env bash
# hf_fetch REPO_ID REPO_TYPE DEST_DIR [REVISION]  -- Hugging Face download helper.
set -uo pipefail

hf_fetch() {
    local repo="$1" rtype="${2:-dataset}" dest="$3" rev="${4:-}"
    mkdir -p "$dest"
    local bin=""
    command -v hf >/dev/null 2>&1 && bin="hf"
    [ -z "$bin" ] && command -v huggingface-cli >/dev/null 2>&1 && bin="huggingface-cli"
    [ -z "$bin" ] && { echo "ERROR: install the HF CLI (pip install -U 'huggingface_hub[cli,hf_transfer]')" >&2; return 1; }

    if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
        echo "DRY: would $bin download $repo (type=$rtype${rev:+ rev=$rev}) -> $dest" >&2
        return 0
    fi
    export HF_HUB_ENABLE_HF_TRANSFER=1
    local -a args=(download "$repo" --local-dir "$dest")
    [ "$rtype" != "model" ] && args+=(--repo-type "$rtype")
    [ -n "$rev" ] && args+=(--revision "$rev")
    if "$bin" "${args[@]}"; then return 0; fi
    # retry as model-type (batch-script behaviour)
    echo "WARN: $bin download as $rtype failed; retrying as model" >&2
    args=(download "$repo" --local-dir "$dest"); [ -n "$rev" ] && args+=(--revision "$rev")
    "$bin" "${args[@]}"
}
```

- [ ] **Step 3: `scripts/data/_lib/check_prereqs.sh`**

```bash
#!/usr/bin/env bash
# Report presence of the acquisition-layer runtime tools. Never fails.
set -uo pipefail
_row() {
    local t="$1" hint="$2" p
    if p="$(command -v "$t" 2>/dev/null)"; then printf '  %-14s %s\n' "$t" "$p"
    else printf '  %-14s MISSING  (%s)\n' "$t" "$hint"; fi
}
echo "acquisition-layer prerequisites:"
_row aria2c        "http/ftp segmented download; apt/brew install aria2"
_row s5cmd         "fast S3; https://github.com/peak/s5cmd"
_row aws           "S3 fallback; pip install awscli"
_row curl          "always needed (HEAD checks, dry-run, slow fallback)"
_row hf            "HF; pip install -U 'huggingface_hub[cli,hf_transfer]'"
_row huggingface-cli "HF (older name)"
_row shellcheck    "script lint (CI-enforced; local optional)"
```

- [ ] **Step 4: Verify** — `bash -n` all three; `bash scripts/data/_lib/check_prereqs.sh` prints the table; `bash -c 'source scripts/data/_lib/common.sh; EPISTEME_DRY_RUN=1 s3_sync s3://openalex/data/jsonl/works /tmp/oa'` prints `DRY: would sync ...` (or the s5cmd/aws dry-run if installed); `bash -c 'source scripts/data/_lib/hf_download.sh; EPISTEME_DRY_RUN=1 hf_fetch epfl-llm/guidelines dataset /tmp/g'` prints `DRY: ...`.

- [ ] **Step 5: `chmod +x` + `git update-index --chmod=+x` the two new `.sh`; Commit** — `feat(sp3): _lib s3_sync + hf_download.sh + check_prereqs.sh`

---

## Task 4: `run_pipeline.sh` — all-source `download` dispatch

**Files:** Modify `scripts/data/run_pipeline.sh`.

**Interfaces:**
- Consumes: every `scripts/data/<source>/download_<source>.sh` (created in Tasks 5–9; this task tolerates them not existing yet — see Step 3).
- Produces: `run_pipeline.sh <source> download [--dry-run] [--max-files N] [--force --reason "…"]` for all sources; `run_pipeline.sh <source> <other-stage>` → graceful die for non-pmc.

- [ ] **Step 1: Source→wrapper-path table**

Replace the hardcoded `if [ "$SOURCE" != "pmc" ]; then die …` with a table. Keep pmc's existing full-chain `case`. Add:

```bash
# source token -> wrapper path (relative to $HERE). EPMC feeds use their
# article_schema.SOURCES token where one exists.
declare -A WRAPPER=(
  [pmc]="pmc/download_pmc.sh"
  [pubmed]="pubmed/download_pubmed.sh"
  [apollo]="apollo/download_apollo.sh"
  [europepmc_preprint]="europepmc/preprints/download_europepmc_preprint.sh"
  [europepmc_manuscript]="europepmc/manuscripts/download_europepmc_manuscript.sh"
  [europepmc_id_mappings]="europepmc/id_mappings/download_europepmc_id_mappings.sh"
  [europepmc_lite]="europepmc/lite_metadata/download_europepmc_lite.sh"
  [europepmc_abstracts]="europepmc/abstracts/download_europepmc_abstracts.sh"
  [bookshelf]="bookshelf/download_bookshelf.sh"
  [chembl]="chembl/download_chembl.sh"
  [uniprot]="uniprot/download_uniprot.sh"
  [pubchem]="pubchem/download_pubchem.sh"
  [clinvar]="clinvar/download_clinvar.sh"
  [reactome]="reactome/download_reactome.sh"
  [mesh]="mesh/download_mesh.sh"
  [ontologies]="ontologies/download_ontologies.sh"
  [openalex]="openalex/download_openalex.sh"
  [hf_corpus]="hf_corpus/download_hf_corpus.sh"
  [dailymed]="dailymed/download_dailymed.sh"
  [openfda]="openfda/download_openfda.sh"
  [aact]="aact/download_aact.sh"
)
```

- [ ] **Step 2: Arg handling**

The SP1-β `run_pipeline.sh` already `shift 2`s off `<source> <stage>` then consumes `--force`/`--reason`/`--max-files` into `FORCE`/`REASON`/`MAX_FILES` via a `while` loop (leaving `$@` empty). Add `--dry-run` to that same loop: `--dry-run) export EPISTEME_DRY_RUN=1; shift ;;`. Then **rebuild** the passthrough arg list for the wrapper from the parsed vars:

```bash
wrapper_args=()
[ -n "$MAX_FILES" ] && wrapper_args+=(--max-files "$MAX_FILES")
[ "$FORCE" = "1" ] && wrapper_args+=(--force --reason "$REASON")
# EPISTEME_DRY_RUN is exported, not passed as a flag.
```

- [ ] **Step 3: Stage dispatch**

```bash
if [ -z "${WRAPPER[$SOURCE]:-}" ]; then
    die "unknown source '$SOURCE' (see the roadmap source list / scripts/data/_lib/check_prereqs.sh)" 3
fi

case "$STAGE" in
  download)
    w="$HERE/${WRAPPER[$SOURCE]}"
    [ -f "$w" ] || die "wrapper not found: $w (not yet implemented?)" 3
    run_stage download bash "$w" "${wrapper_args[@]}" ;;
  extract|serialize|load|graph|materialize|enrich|all)
    if [ "$SOURCE" = "pmc" ]; then
        : # fall through to pmc's existing chain below
    else
        die "$SOURCE $STAGE is not in SP3 — SP2 (literature extract) / SP4 (structured serialize)" 3
    fi ;;
  *) usage ;;
esac
```
`EPISTEME_DRY_RUN` propagates to the wrapper because `run_pipeline.sh` `export`ed it; a wrapper invoked directly (not via `run_pipeline.sh`) still honours a bare `--dry-run` via its own local parse (Task 5 Step 3). The `[ -f "$w" ] || die "wrapper not found"` guard makes this task committable before Tasks 5–9 exist; `run_pipeline.sh pmc download --dry-run` must still work now (pmc's wrapper exists from SP1-β).

- [ ] **Step 4: Verify** — `bash -n scripts/data/run_pipeline.sh`; `bash scripts/data/run_pipeline.sh pmc download --dry-run` (pmc's Python `--help`/dry path — confirm it doesn't error on the flag; if `download_pmc.py` lacks `--dry-run`, add a passthrough note to Task 8's pmc confirm step, do NOT modify pmc here); `bash scripts/data/run_pipeline.sh chembl download` → `die "wrapper not found"` exit 3; `bash scripts/data/run_pipeline.sh chembl extract` → `die "... SP2/SP4"` exit 3; `bash scripts/data/run_pipeline.sh bogus download` → `die "unknown source"` exit 3.

- [ ] **Step 5: Commit** — `feat(sp3): run_pipeline.sh table-driven all-source download dispatch`

---

## Task 5: Wrappers — `chembl` + `uniprot`

**Files:** Create `scripts/data/chembl/download_chembl.sh`, `scripts/data/uniprot/download_uniprot.sh`.
**Reference:** `.superpowers/sdd/2026-09-06-sp3-acquisition-layer/batch-scripts/download_chembl.sh`, `download_uniprot_swissprot.sh`.

**Interfaces:** Consumes `_lib` `discover_manifest` / `size_match_skip` / `http_fetch` / `resolve_dest` / `write_sync_stamp`.

- [ ] **Step 1: `chembl` wrapper** — thin: parse `[--dry-run] [MODE]` (MODE `default|all`, default `default`); `BASE="$CHEMBL_BASE"`; `dest="$(resolve_dest chembl)"`; `discover_manifest "$BASE" <the regex set from the batch script>` for the default file set (LICENSE, README, checksums.txt, `chembl_[0-9]+_sqlite.tar.gz`, `chembl_[0-9]+.sdf.gz`, `chembl_[0-9]+_chemreps.txt.gz`, `chembl_uniprot_mapping.txt`, release notes, schema doc) plus the `all`-mode extras (`postgresql`/`mysql`/`.h5`/`.fps.gz`); build a URL list (`$BASE/$f` per resolved filename); `size_match_skip` per file to prune; `http_fetch "$dest"` the survivors; `write_sync_stamp "$dest"`. ~35 lines.

- [ ] **Step 2: `uniprot` wrapper** — thin: parse `[--dry-run]`; mirror probe over `$UNIPROT_MIRRORS` (split on `|`, first whose `reldate.txt` HEAD returns 200 wins → `BASE`); fixed `FILES` list from the batch script (`uniprot_sprot.{xml,fasta,dat}.gz`, `uniprot_sprot_varsplic.fasta.gz`, `LICENSE`, `README`, `reldate.txt`, `uniprot.xsd`); `dest="$(resolve_dest uniprot swissprot)"`; `size_match_skip` prune; `http_fetch`; `write_sync_stamp`. In `--dry-run`, the mirror probe still runs (cheap HEAD) so the resolved BASE is printed. ~35 lines.

- [ ] **Step 3: Both parse `--dry-run`** locally too (for direct invocation): `[ "${1:-}" = "--dry-run" ] && { export EPISTEME_DRY_RUN=1; shift; }` near the top, after sourcing `_lib`.

- [ ] **Step 4: Verify** — `bash -n` both; `chmod +x` + `git update-index --chmod=+x`; `bash scripts/data/chembl/download_chembl.sh --dry-run` prints the resolved versioned filenames + `DRY: would fetch` lines, transfers nothing (real network for the listing); same for `uniprot` (prints the chosen mirror + file list); `bash scripts/data/run_pipeline.sh chembl download --dry-run` works via the Task 4 table.

- [ ] **Step 5: Commit** — `feat(sp3): chembl + uniprot download wrappers`

---

## Task 6: Wrappers — `pubchem` + `clinvar` + `reactome` + `mesh` + `ontologies`

**Files:** Create `scripts/data/{pubchem,clinvar,reactome,mesh,ontologies}/download_<source>.sh`.
**Reference:** the matching `batch-scripts/download_*.sh` (`download_open_ontologies.sh` for `ontologies`).

**Interfaces:** Consumes the same `_lib` helpers as Task 5.

- [ ] **Step 1: `reactome`** — `discover_manifest "$REACTOME_BASE" <regexes>` for the file set in the batch script; `dest="$(resolve_dest reactome)"`; prune + `http_fetch` + `write_sync_stamp`. ~30 lines.
- [ ] **Step 2: `clinvar`** — batch script lists multiple subdirs (`tsv`, `xml`, `vcf` per `MODE`); `discover_manifest "$CLINVAR_BASE/$sub"` per subdir; build `URL<TAB>relpath` lines (`$CLINVAR_BASE/$sub/$f` → `<sub>/$f`); `http_fetch "$(resolve_dest clinvar)"`. Keep the batch's `MODE` (`tsv|xml|vcf|all`, default `tsv`). ~40 lines.
- [ ] **Step 3: `pubchem`** — batch `MODE` (`compound_extras|...`, default from batch); `discover_manifest "$PUBCHEM_BASE/$rel"`; `URL<TAB>relpath`; `http_fetch "$(resolve_dest pubchem)"`. ~40 lines.
- [ ] **Step 4: `mesh`** — batch tries `$MESH_BASE/xmlmesh$YEAR`, `$MESH_BASE/ascii$YEAR`, `$MESH_FTP_BASE/...` with fallback; take a `[YEAR]` arg (default = current year); `discover_manifest` each candidate dir, first non-empty wins; `URL<TAB>relpath`; `http_fetch "$(resolve_dest mesh)"`. Preserve the batch's "NLM layout may have changed" warning path. ~45 lines.
- [ ] **Step 5: `ontologies`** — NO discovery: a `data` array of `name|filename` pairs (from `download_open_ontologies.sh`), URLs built as `${OBO_PURL_BASE}/<filename>` (and the UCUM raw-github ones kept as-is in the array — those are the exception, add a `# noqa: host` style comment and let the Task 11 grep gate allow `_lib` + this array... actually: put the 2 UCUM URLs in `sources.env` as `UCUM_ESSENCE_URL` / `UCUM_README_URL` to keep the gate clean). `URL<TAB>name/filename`; `http_fetch "$(resolve_dest ontologies)"`. ~35 lines.

- [ ] **Step 6: All five** parse `--dry-run` locally (Task 5 Step 3 pattern); `bash -n`; `chmod +x` + tracked.

- [ ] **Step 7: Verify** — `bash scripts/data/<s>/download_<s>.sh --dry-run` for each prints resolved URLs + `DRY:` lines, no transfer; `run_pipeline.sh <s> download --dry-run` works for each.

- [ ] **Step 8: Commit** — `feat(sp3): pubchem/clinvar/reactome/mesh/ontologies download wrappers`

---

## Task 7: Wrappers — `openalex` + `openfda` + `bookshelf` + `hf_corpus`

**Files:** Create `scripts/data/{openalex,openfda,bookshelf,hf_corpus}/download_<source>.sh`.
**Reference:** the matching `batch-scripts/*` (`bookshelf` has no batch script — use the flat `scripts/download_bookshelf_oa.sh` currently on the branch as its reference).

- [ ] **Step 1: `openalex`** — `[--dry-run] [MODE]` (MODE `works_jsonl|works_parquet|jsonl|parquet|full`, default `works_jsonl`); map MODE → `$OPENALEX_S3/data/jsonl/works` etc. (the batch script's `case`); `s3_sync "$SRC" "$(resolve_dest openalex "$relpath")"`; also `s3_sync`/`http_fetch` the top-level `manifest.json` when the mode covers it; `write_sync_stamp`; write `sync_mode.txt`. **No default auto-run beyond the explicit MODE arg.** ~35 lines.
- [ ] **Step 2: `openfda`** — batch script fetches `$OPENFDA_CATALOG` (a JSON index) then pulls the per-endpoint zip URLs it lists; for the wrapper: `curl` the catalog, `python -c`/`grep`+`sed` the `.zip` URLs for the requested endpoint arg (default: a small one, e.g. `drug/label` or whatever the batch defaults to), `http_fetch "$(resolve_dest openfda)"`. In `--dry-run`, fetch the catalog (small) and print the zip URLs, transfer none. ~45 lines.
- [ ] **Step 3: `bookshelf`** — `discover_manifest "$BOOKSHELF_BASE"` (or the fixed path the flat script uses) for the OA package list; `http_fetch "$(resolve_dest bookshelf)"`. ~30 lines.
- [ ] **Step 4: `hf_corpus`** — `source "$HERE/../_lib/hf_download.sh"`; args `<repo_id> [output_subdir] [revision] [--repo-type dataset|model]`; `dest="$(resolve_dest hf_corpus "${2:-${1//\//_}}")"`; `hf_fetch "$1" "${repo_type:-dataset}" "$dest" "${3:-}"`; `write_sync_stamp`; write `hf_repo_id.txt`. `--dry-run` handled by `hf_fetch`. ~30 lines.

- [ ] **Step 5** — `--dry-run` local parse; `bash -n`; `chmod +x` + tracked.
- [ ] **Step 6: Verify** — each `--dry-run` prints its resolved target + planned transfer, moves nothing; `openfda` really fetches its small catalog in dry-run and lists zips; `run_pipeline.sh <s> download --dry-run` works for each.
- [ ] **Step 7: Commit** — `feat(sp3): openalex/openfda/bookshelf/hf_corpus download wrappers`

---

## Task 8: Wrappers — `dailymed` + `aact` + Python-exec (`pubmed`, `apollo`) + `pubmed/verify_pubmed.sh`

**Files:** Create `scripts/data/{dailymed,aact}/download_<source>.sh`; `scripts/data/pubmed/download_pubmed.sh` (git mv base — see Task 10, but create-new is fine here and Task 10 handles the `git rm` of the flat one), `scripts/data/pubmed/verify_pubmed.sh`, `scripts/data/apollo/download_apollo.sh`.

- [ ] **Step 1: `dailymed`** — `discover_manifest "$DAILYMED_BASE"` (or the batch's fixed subpaths) for the SPL release zips; `http_fetch "$(resolve_dest dailymed)"`; `write_sync_stamp`. Download-only. ~35 lines.
- [ ] **Step 2: `aact`** — batch fetches the latest `$AACT_DOWNLOADS/<date>_pipe-delimited-export.zip` (or `_export.zip`); resolve the newest via `discover_manifest` + a date-sort, or the batch's own logic; `http_fetch "$(resolve_dest aact)"`. Download-only. ~35 lines.
- [ ] **Step 3: `pubmed/download_pubmed.sh`** — thin Python-exec wrapper:
  ```bash
  #!/usr/bin/env bash
  set -uo pipefail
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  . "$HERE/../_lib/common.sh"
  load_dotenv
  require_env EPISTEME_ACTOR
  PY="${PYTHON:-}"; if [ -z "$PY" ]; then for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }; done; fi
  [ -n "$PY" ] || die "python not found (set PYTHON=)"
  exec "$PY" -m episteme.data.pubmed.download_pubmed "$@"
  ```
- [ ] **Step 4: `pubmed/verify_pubmed.sh`** — same shape, `exec "$PY" -m episteme.data.pubmed.<verify module>` (find the module: the flat `scripts/verify_pubmed_checksums.sh` names it). If checksum verify/repair is only a bash script today (no Python module), port its logic into `verify_pubmed.sh` directly using `_lib` helpers — keep it download-adjacent, do not build a Python module for it in SP3.
- [ ] **Step 5: `apollo/download_apollo.sh`** — thin Python-exec wrapper → `episteme.data.apollo.download_apollo`.
- [ ] **Step 6: `pmc` confirm** — no new wrapper; verify `scripts/data/pmc/download_pmc.sh` still works via `run_pipeline.sh pmc download --dry-run`. If `download_pmc.py` errors on an unknown `--dry-run`, add a `--dry-run` no-op flag to `download_pmc.py`'s argparse that prints its resolved S3 URI + planned prefix and exits 0 (minimal; note it in the report as a small pmc touch).
- [ ] **Step 7** — all new `.sh`: `--dry-run` local parse where bash; `bash -n`; `chmod +x` + tracked. `pytest -q -m "not pg"` still 40/41.
- [ ] **Step 8: Verify** — every wrapper `--dry-run` clean; `run_pipeline.sh {dailymed,aact,pubmed,apollo,pmc} download --dry-run` all exit 0.
- [ ] **Step 9: Commit** — `feat(sp3): dailymed/aact + pubmed/apollo python-exec wrappers + verify_pubmed`

---

## Task 9: Europe PMC — 5 wrappers + module reconciliation

**Files:**
- Create: `scripts/data/europepmc/{preprints,manuscripts,id_mappings,lite_metadata,abstracts}/download_<feed>.sh`
- Modify: `src/episteme/data/europepmc/{preprints,manuscripts,id_mappings,lite_metadata}/download_*.py` (+ `abstracts` if it drifts)
- Reference: `batch-scripts/{download_europepmc.sh, download_author_manuscripts.sh, download_epmc_id_mappings.sh, download_epmc_lite_metadata.sh}` (the 2026-08-31 EPMC rewrites — the normalisation base).

**Interfaces:** Each wrapper `exec "$PY" -m episteme.data.europepmc.<feed>.download_<feed> "$@"`. Each Python module reads its base URL from `get_settings().europepmc_base` (no `os.environ`).

- [ ] **Step 1:** Diff each existing `download_*.py` against its batch EPMC rewrite. For each: if the module already covers the batch script's behaviour (endpoint, page-size, gzip integrity check per `docs/10` §5, output layout), the change is only: swap any hardcoded base URL for `get_settings().europepmc_base`, add a `--dry-run` no-op flag (print resolved URL + planned request, exit 0), keep everything else. If the batch script has materially better logic (resume, integrity check, rate-limit handling), port that delta into the module — minimally, with a one-line note in the task report on what changed and why.
- [ ] **Step 2:** Five thin Python-exec wrappers (Task 8 Step 3 shape), one per feed, at `scripts/data/europepmc/<feed>/download_<feed>.sh`.
- [ ] **Step 3:** Add/confirm `europepmc_base` on `Settings` (Task 1 Step 5). Grep the 5 modules + the `europepmc/preprints/extract_europepmc_preprints.py` for `os.environ` / hardcoded `ebi.ac.uk` / `europepmc` URLs — none may remain outside `get_settings()`.
- [ ] **Step 4: Verify** — `pytest -q -m "not pg"` still green (existing europepmc tests, if any, pass); each wrapper `run_pipeline.sh europepmc_<feed> download --dry-run` exits 0 and prints the resolved `EUROPEPMC_BASE`; `.venv/Scripts/python.exe -c "import episteme.data.europepmc.preprints.download_europepmc_preprints"` (and the other 4) clean.
- [ ] **Step 5: Commit** — `feat(sp3): europepmc download wrappers + module endpoint wiring (batch rewrites reconciled)`

---

## Task 10: Migration — retire the flat scripts

**Files:** `git mv` / `git rm` under `scripts/`; modify `.env.example`.

- [ ] **Step 1: `git rm`** the genuinely superseded flat scripts (each has a nested replacement now):
  `scripts/download_pmc_oa_comm.sh`, `scripts/extract_pmc_oa_comm.sh`,
  `scripts/download_pubmed_baseline_md5.sh`, `scripts/download_pubmed_updates.sh`, `scripts/pubmed_baseline_downloader.sh`, `scripts/extract_pubmed.sh`,
  `scripts/verify_pubmed_checksums.sh`, `scripts/repair_pubmed_failed_checksum.sh`,
  `scripts/download_apollo_corpus.sh`, `scripts/extract_apollo_corpus.sh`,
  `scripts/download_bookshelf_oa.sh`,
  `scripts/download_europepmc.sh`, `scripts/download_epmc_id_mappings.sh`, `scripts/download_epmc_lite_metadata.sh`, `scripts/download_author_manuscripts.sh`.
  Also `git rm -r --cached scripts/__pycache__` and add `scripts/__pycache__/` to `.gitignore` if not covered.
  **Keep:** `scripts/mcpg-episteme.sh`.
- [ ] **Step 2: History note** — SP3 created the nested wrappers fresh (they're thin, not line-edits of the flat ones), so `git mv` isn't required for content lineage; the flat scripts' history stays reachable via `git log -- <old path>`. If any nested wrapper IS a near-copy of a flat script (e.g. `bookshelf`), do `git mv` for that one instead of create+rm, and rebase the wrapper edit on top.
- [ ] **Step 3: `.env.example`** — replace the `# --- Upstream endpoints ---` / `PMC_S3_BUCKET=...` block (and any other `<SOURCE>_BASE` lines) with:
  ```
  # --- Upstream endpoints ---
  # All <SOURCE>_BASE / _S3 / _FTP defaults live in scripts/data/_lib/sources.env
  # (committed, one source of truth). Override any of them here or in the real
  # environment; load order is sources.env -> .env -> real env, last wins.
  ```
- [ ] **Step 4: Verify** — `find scripts -maxdepth 1 -name '*.sh'` → only `mcpg-episteme.sh`; `git status` clean of stray deletions; `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v '_lib/sources.env'` → only comments (no bare endpoint literals in wrappers); `pytest -q -m "not pg"` green.
- [ ] **Step 5: Commit** — `refactor(sp3): retire flat scripts/*.sh; .env.example points at sources.env`

---

## Task 11: shellcheck CI + verification sweep

**Files:** Create `.github/workflows/shellcheck.yml`; touch `docs/project-incubation-baseline.md` (drift log).

- [ ] **Step 1: `.github/workflows/shellcheck.yml`**

```yaml
name: shellcheck
on:
  push:
    paths: ['scripts/**', '.github/workflows/shellcheck.yml']
  pull_request:
    paths: ['scripts/**', '.github/workflows/shellcheck.yml']
permissions:
  contents: read
jobs:
  shellcheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: shellcheck
        run: |
          sudo apt-get update && sudo apt-get install -y shellcheck
          find scripts -name '*.sh' -print0 | xargs -0 shellcheck --severity=warning --external-sources
```
(`--external-sources` so `source "$HERE/../_lib/common.sh"` resolves; add `# shellcheck disable=SCxxxx` only where genuinely justified, with a reason comment.)

- [ ] **Step 2: Full dry-run sweep** — for **every** token in the Task 4 `WRAPPER` table:
  `bash scripts/data/run_pipeline.sh <token> download --dry-run` → exit 0, prints resolved endpoint(s) + the file list it would fetch, transfers nothing, with `aria2c`/`aws`/`s5cmd`/`hf` absent. Capture the full transcript into the task report.
- [ ] **Step 3: One real small fetch** —
  `bash scripts/data/run_pipeline.sh uniprot download --max-files 2` → pulls `reldate.txt` + `LICENSE` into `01_raw/uniprot/swissprot/` via the `curl` fallback path; `01_raw/uniprot/swissprot/last_sync_utc.txt` exists; a second identical run reports both files as size-matched skips. Then `git clean -fdx 01_raw/uniprot` (gitignored — leave no artifact). Transcript into the report.
- [ ] **Step 4: Grep gate** — `grep -REn 'ftp\.|s3://|https?://' scripts/data/` returns matches ONLY in `_lib/sources.env` (and `#` comments). Any wrapper hit = fix it (move the literal to `sources.env`).
- [ ] **Step 5: Python gates** — `pytest -q -m "not pg"` → 40/41 passed; with `.env` + `TEST_PG_DSN`, `pytest -q` → 46 passed, 1 skipped; `pip install -e ".[data]"` in a scratch venv (or `.venv/Scripts/python.exe -c "import episteme.data.europepmc.preprints.download_europepmc_preprints, episteme.data.pubmed.download_pubmed, episteme.data.apollo.download_apollo"`) clean.
- [ ] **Step 6: `check_prereqs.sh`** in the report so the reviewer sees what's installed vs. what a real run needs.
- [ ] **Step 7: Drift log** — one entry in `docs/project-incubation-baseline.md` summarising SP3 (acquisition layer landed, `sources.env` single-source, `_lib` fetch engine, all 21 tokens dry-run-verified, `aria2c`/`s5cmd`/`aws` are documented install-before-real-download prereqs, shellcheck CI added).
- [ ] **Step 8: Commit** — `ci(sp3): shellcheck workflow + acquisition-layer verification sweep`

---

## Self-Review

**Spec coverage (`2026-09-06-sp3-acquisition-layer.md`):**

| Spec item | Task |
|---|---|
| `sources.env` single source of truth + load order (bash + `config.py`) | 1 |
| `_lib` `http_fetch`/`size_match_skip`/`discover_manifest`/`resolve_dest` + `--dry-run` | 2 |
| `_lib` `s3_sync` + `_lib/hf_download.sh` + `check_prereqs.sh` | 3 |
| `run_pipeline.sh` all-source `download` + graceful `die` for non-download non-pmc | 4 |
| 13 bulk wrappers (chembl…aact) | 5, 6, 7, 8 |
| Python-exec wrappers (pubmed, apollo, europepmc×5) + EPMC module reconciliation | 8, 9 |
| Volatile = download-only (dailymed, openfda, aact) | 8 (dailymed/aact), 7 (openfda) |
| openalex wrapper wired, all modes, no auto-run | 7 |
| `.env` var wiring on Python download modules | 1 (Settings), 9 (europepmc) |
| History-preserving migration; retire flat `scripts/*.sh` | 10 |
| `.env.example` collapse to a pointer | 10 |
| shellcheck via new CI | 11 |
| Exit criteria 1 (dry-run all sources) | 11 Step 2 |
| Exit criteria 2 (one real small fetch) | 11 Step 3 |
| Exit criteria 4 (grep gate) | 10 Step 4, 11 Step 4 |
| Exit criteria 5 (`sources.env` sole definition, override precedence) | 1 Step 4 test, 11 Step 5 |
| Exit criteria 6 (non-download stage → exit 3) | 4 Step 2, 4 Step 4 |
| Exit criteria 8 (`pytest` green, import check) | every task's verify step, 11 Step 5 |

**Placeholder scan:** Each wrapper task says "read `batch-scripts/download_<source>.sh`, extract its URL patterns/modes into the wrapper; mechanics are in `_lib`" — the batch scripts are concrete, staged, and are the exact source material, not a "figure it out". Line-count targets (~30–45) are guidance, not contracts. The `sources.env` URL list in Task 1 Step 1 is explicitly "use the batch scripts' actual URLs where they differ".

**Type consistency:** `resolve_dest SOURCE [SUBPATH]`, `http_fetch DEST_DIR [URL...]` (+ `URL<TAB>relpath` line form), `size_match_skip LOCAL_PATH URL`, `discover_manifest BASE_URL REGEX...`, `s3_sync S3_URI DEST_DIR`, `hf_fetch REPO_ID REPO_TYPE DEST_DIR [REV]` — used consistently across Tasks 5–9. `EPISTEME_DRY_RUN=1` is the single dry-run signal, set by `run_pipeline.sh` and by each wrapper's local `--dry-run` parse.

**Risks:** (a) The `common.sh` load-order change (`_COMMON_REAL_ENV_KEYS` capture + `load_dotenv` guard rewrite) is the subtlest part — Task 1 Step 4's test pins the precedence; if it's flaky, fall back to a simpler rule ("`.env` always overrides `sources.env`; real env always overrides `.env`" via explicit `override=True` on the `.env` load and `setdefault` on `sources.env`, no key-capture needed) and note it. (b) `discover_manifest`'s href-scrape is fragile to server listing-format changes (Apache vs nginx vs S3 XML) — every bulk wrapper's dry-run in Task 11 Step 2 is the canary; a source whose listing doesn't parse gets a `# TODO(SP3-followup): <source> listing format` and its dry-run asserts "resolved 0 files, listing format unrecognised" rather than failing the sweep. (c) `aria2c` absent means the real-fetch path (Task 11 Step 3) only exercises the `curl` fallback — the `aria2c` branch is `bash -n`- and shellcheck-verified but not run until the tool is installed; flagged in the drift log.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-06-sp3-acquisition-layer.md`.

**1. Subagent-Driven (per the standing preference — no mode question)** — dispatch a fresh implementer per task, task review after each, broad whole-branch review at the end.

`pg`-marked tests aren't central to SP3 (only Task 1's non-pg load-order test is added), but the suite must stay green: `TEST_PG_DSN` from `.env` should be available to any task that runs `pytest -q` (all tasks' verify steps). `.env` + the live PostgreSQL from SP1-β are already in place.
