# SP8 — CDISC USDM download-only source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `cdisc_usdm`, a download-only source that fetches the `Deliverables/` folder of the latest USDM release from `cdisc-org/DDF-RA` into a release-named folder with provenance.

**Architecture:** One Bash wrapper over `scripts/data/_lib/common.sh`, modelled on `scripts/data/cdisc_bc/download_cdisc_bc.sh`: three GitHub API calls (latest release → commit SHA → recursive tree), then `size_match_skip` + `http_fetch` of every `Deliverables/` blob at the pinned SHA. Registered in `run_pipeline.sh`'s `WRAPPER` table only, so every stage except `download` is refused. Docs and doc tests follow.

**Tech Stack:** Bash (Git for Windows bash locally, GNU bash in the cloud sandbox), awk, curl; pytest with a fake `curl` on `PATH`.

**Spec:** `docs/superpowers/specs/2026-09-29-sp8-cdisc-usdm-download-design.md`

## Global Constraints

- Download-only: no serializer, no loader, no `articles` rows, no corpus inclusion for `cdisc_usdm`.
- Endpoint URLs live only in `scripts/data/_lib/sources.env`. This must print nothing: `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'`
- New `sources.env` keys, exactly: `USDM_API_BASE=https://api.github.com/repos/cdisc-org/DDF-RA`, `USDM_RAW_BASE=https://raw.githubusercontent.com/cdisc-org/DDF-RA`, `USDM_REPO_URL=https://github.com/cdisc-org/DDF-RA`.
- Raw layout: `<raw>/cdisc_usdm/<release-tag>/` holding `Deliverables/...`, `LICENSE`, `README.md`, `PROVENANCE.txt`; the sync stamp goes in `<raw>/cdisc_usdm/last_sync_utc.txt`. Older release folders are never touched.
- Release tag must match `^v?[0-9]+(\.[0-9]+)*$`; every raw fetch uses the 40-hex commit SHA, never the tag.
- Secrets: never read, print, cat, grep or copy `.env`; never dump the environment. Docs name env keys only.
- No database work: run only `pytest -m "not pg"`. No command in this plan touches Postgres.
- Git: `git add` explicit paths only; never `--no-verify`; never stage `.gitignore` or `.env`. Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```
- Test commands: `.venv/Scripts/python.exe -m pytest ...` locally on Windows (use `.venv/bin/python` if that is what exists).

## Review Focus

1. **A `Deliverables/` path with a space, `#` or `%`** (none today, CDISC could add one): the raw URL must be percent-encoded while the local file keeps its real name. Pinned by the fixture file `Deliverables/IG/USDM IG #2.pdf` in Task 1.
2. **A newer release arriving while an older release folder exists:** the new tag gets its own folder and the old folder is left untouched. Pinned by `test_older_release_folder_is_untouched`.
3. **GitHub API failure or rate limit on the first call:** die non-zero with a message naming the rate limit, write nothing. Pinned by `test_unresolvable_release_dies_and_writes_nothing`.
4. **A tree entry with a `..` component under `Deliverables/`:** skipped with a warning, never written outside the release folder. Pinned in the main fetch test.
5. **Blobs outside `Deliverables/` and the repository's own top-level `README.md`:** only `Deliverables/` blobs count as data (and against `--max-files`); `README.md` and `LICENSE` ride along separately. Pinned by the main fetch test and the `--max-files` test.

---

## File Structure

- Create `scripts/data/cdisc_usdm/download_cdisc_usdm.sh` — the wrapper (Task 1).
- Modify `scripts/data/_lib/sources.env` — three `USDM_*` keys (Task 1).
- Create `tests/test_cdisc_usdm_wrapper.py` — offline wrapper tests (Task 1).
- Modify `scripts/data/run_pipeline.sh` — `WRAPPER` entry (Task 2).
- Modify `tests/test_run_pipeline_dispatch.py` — token list + download-only test (Task 2).
- Modify `docs/02-data-sources.md`, `docs/09-extraction-contract.md`, `docs/10-data-sources-runbook.md`, `docs/12-source-inventory.md`, `CLAUDE.md`, `docs/project-incubation-baseline.md` (Task 2).
- Modify `tests/docs/test_extraction_contract_doc.py`, `tests/docs/test_runbook_doc.py`, `tests/docs/test_source_inventory_doc.py` (Task 2).

Registration and docs are one task because `tests/docs/test_source_inventory_doc.py::test_inventory_covers_exactly_the_wired_sources` fails the moment `WRAPPER` gains `cdisc_usdm` without a `docs/12` row.

---

### Task 1: The `cdisc_usdm` download wrapper

**Files:**
- Create: `scripts/data/cdisc_usdm/download_cdisc_usdm.sh`
- Modify: `scripts/data/_lib/sources.env` (after the COSMoS block, before the CDISC CT block)
- Test: `tests/test_cdisc_usdm_wrapper.py`

**Interfaces:**
- Consumes (from `scripts/data/_lib/common.sh`, unchanged): `log`, `die`, `load_dotenv`, `require_env`, `resolve_dest SOURCE [SUBPATH]`, `_safe_rel REL`, `size_match_skip LOCAL URL`, `http_fetch DEST` (stdin lines `URL<TAB>relpath`; under `EPISTEME_DRY_RUN=1` prints `DRY: would fetch ...` per line to stderr and writes nothing), `cap_urls N`, `write_sync_stamp DIR` (no-op under dry run).
- Produces: `scripts/data/cdisc_usdm/download_cdisc_usdm.sh` accepting `--dry-run`, `--max-files N`, `--force`, `--reason R`; exit 0 on success, non-zero via `die` otherwise. Task 2 registers this exact path as `cdisc_usdm/download_cdisc_usdm.sh`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cdisc_usdm_wrapper.py`:

```python
"""Network-free tests for scripts/data/cdisc_usdm/download_cdisc_usdm.sh (fake curl on PATH)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "data" / "cdisc_usdm" / "download_cdisc_usdm.sh"
TAG = "v4.0.0"
SHA = "aa303cb32f5d3ceecc68a16803e26720d2c1fc26"
RAW = "https://raw.githubusercontent.com/cdisc-org/DDF-RA/" + SHA

pytestmark = pytest.mark.skipif(
    shutil.which("aria2c") is not None, reason="http_fetch prefers aria2c, bypassing the curl shim"
)

DATA_FILES = [
    "Deliverables/API/USDM_API.json",
    "Deliverables/CT/USDM_CT.xlsx",
    "Deliverables/IG/USDM IG #2.pdf",
    "Deliverables/UML/UML_Views/Timeline.png",
]


def _entry(path: str, kind: str) -> dict:
    e = {
        "path": path,
        "mode": "100644" if kind == "blob" else "040000",
        "type": kind,
        "sha": "0" * 40,
    }
    if kind == "blob":
        e["size"] = 10
    e["url"] = "https://api.github.com/x"
    return e


def _tree(paths: list[tuple[str, str]], truncated: bool = False) -> str:
    body = {
        "sha": SHA,
        "url": "https://api.github.com/x",
        "tree": [_entry(p, k) for p, k in paths],
        "truncated": truncated,
    }
    return json.dumps(body, indent=2) + "\n"


DEFAULT_TREE = _tree(
    [
        ("Deliverables", "tree"),
        ("Deliverables/API", "tree"),
        *[(p, "blob") for p in DATA_FILES],
        ("Deliverables/../evil.txt", "blob"),
        ("Documents/Examples/example.json", "blob"),
        ("LICENSE", "blob"),
        ("README.md", "blob"),
    ]
)


def _release(tag: str) -> str:
    return (
        json.dumps({"url": "https://api.github.com/x", "id": 1, "tag_name": tag}, indent=2) + "\n"
    )


COMMIT = json.dumps({"sha": SHA, "node_id": "x", "commit": {"sha": "f" * 40}}, indent=2) + "\n"


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _msys_path(p: Path) -> str:
    q = p.as_posix()  # C:/x -> /c/x so bash accepts it inside PATH on Windows
    return f"/{q[0].lower()}{q[2:]}" if len(q) > 1 and q[1] == ":" else q


def _fake_curl(bindir: Path, log: Path, tree: str, release: str) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "release.json").write_text(release, encoding="utf-8")
    (bindir / "commit.json").write_text(COMMIT, encoding="utf-8")
    (bindir / "tree.json").write_text(tree, encoding="utf-8")
    script = bindir / "curl"
    body = (
        "#!/usr/bin/env bash\n"
        f'BIN="{bindir.as_posix()}"\n'
        "out=''; url=''; head=0; prev=''\n"
        'for a in "$@"; do\n'
        '  [ "$prev" = "-o" ] || [ "$prev" = "--output" ] && out="$a"\n'
        '  case "$a" in -I|-sSIL|-sIL|-fsSI) head=1 ;; http*) url="$a" ;; esac\n'
        '  prev="$a"\n'
        "done\n"
        f'echo "$url" >> "{log.as_posix()}"\n'
        'case "$url" in\n'
        '  */releases/latest) body="$(cat "$BIN/release.json")" ;;\n'
        '  */commits/*) body="$(cat "$BIN/commit.json")" ;;\n'
        '  */git/trees/*) body="$(cat "$BIN/tree.json")" ;;\n'
        '  */LICENSE) body="licence text" ;;\n'
        '  */README.md) body="readme text" ;;\n'
        '  *) body="data of $(basename "$url")" ;;\n'
        "esac\n"
        'if [ "$head" = 1 ]; then\n'
        '  printf "content-length: %s\\r\\n" "$(printf "%s\\n" "$body" | wc -c | tr -d " ")"\n'
        "  exit 0\n"
        "fi\n"
        'if [ -n "$out" ]; then mkdir -p "$(dirname "$out")"; printf "%s\n" "$body" > "$out"\n'
        'else printf "%s\n" "$body"; fi\n'
    )
    script.write_bytes(body.encode())  # LF endings: a CRLF shebang breaks bash on Windows
    script.chmod(0o755)


def _run(tmp_path, *args, tree: str = DEFAULT_TREE, release: str | None = None):
    log = tmp_path / "curl.log"
    _fake_curl(tmp_path / "bin", log, tree, _release(TAG) if release is None else release)
    env = {
        **os.environ,
        "SHIM_DIR": _msys_path(tmp_path / "bin"),
        "EPISTEME_ACTOR": "episteme_sys_admin",
        "EPISTEME_DATA_ROOT": str(tmp_path),
        "EPISTEME_RAW_ROOT": str(tmp_path / "01_raw"),
        "PGDATABASE": "episteme_test",
    }
    proc = subprocess.run(
        # Git-for-Windows bash prepends /mingw64/bin (real curl) to PATH at startup, so put the
        # shim first from *inside* a bash -c, then exec the wrapper.
        [_bash(), "-c", 'PATH="$SHIM_DIR:$PATH"; exec bash "$0" "$@"', str(WRAPPER), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def _root(tmp_path) -> Path:
    return tmp_path / "01_raw" / "cdisc_usdm"


def _rel(tmp_path) -> Path:
    return _root(tmp_path) / TAG


def _data_files(tmp_path) -> list[str]:
    base = _rel(tmp_path)
    return sorted(
        p.relative_to(base).as_posix() for p in (base / "Deliverables").rglob("*") if p.is_file()
    )


def test_fetches_deliverables_licence_readme_and_provenance(tmp_path):
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _data_files(tmp_path) == sorted(DATA_FILES)
    rel = _rel(tmp_path)
    assert (rel / "LICENSE").read_text(encoding="utf-8").startswith("licence text")
    assert (rel / "README.md").read_text(encoding="utf-8").startswith("readme text")
    assert not (rel / "Documents").exists()  # outside Deliverables/
    assert not list(tmp_path.rglob("evil.txt"))  # '..' path rejected
    assert "rejecting unsafe path" in proc.stderr
    assert (_root(tmp_path) / "last_sync_utc.txt").is_file()
    raw_calls = [c for c in calls if c.startswith("https://raw.githubusercontent.com/")]
    assert raw_calls and all(c.startswith(RAW + "/") for c in raw_calls)  # pinned to the SHA
    assert not any(f"/{TAG}/" in c for c in raw_calls)
    assert f"{RAW}/Deliverables/IG/USDM%20IG%20%232.pdf" in calls  # URL encoded, local name kept
    prov = (rel / "PROVENANCE.txt").read_text(encoding="utf-8")
    assert "source_repo: https://github.com/cdisc-org/DDF-RA" in prov
    assert f"release_tag: {TAG}" in prov
    assert f"commit_sha: {SHA}" in prov  # top-level sha, not the nested one
    assert "retrieved_at: 20" in prov
    assert "model files not covered" in prov
    assert "until CDISC confirms a licence for the model files" in prov
    assert "excluded from any training corpus" in prov


def test_max_files_caps_data_files_only(tmp_path):
    proc, _ = _run(tmp_path, "--max-files", "2")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert len(_data_files(tmp_path)) == 2
    assert (_rel(tmp_path) / "LICENSE").is_file() and (_rel(tmp_path) / "README.md").is_file()
    assert (_rel(tmp_path) / "PROVENANCE.txt").is_file()


def test_dry_run_writes_nothing(tmp_path):
    proc, calls = _run(tmp_path, "--dry-run")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stderr.count("DRY: would fetch") == len(DATA_FILES) + 2  # + LICENSE, README.md
    assert not _root(tmp_path).exists()
    api_calls = [c for c in calls if c.startswith("https://api.github.com/")]
    assert len(api_calls) == 3  # release, commit, tree


def test_second_run_is_up_to_date_and_force_refetches(tmp_path):
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    prov = _rel(tmp_path) / "PROVENANCE.txt"
    prov.write_text("sentinel\n", encoding="utf-8")
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "up to date" in proc.stderr
    assert prov.read_text(encoding="utf-8") == "sentinel\n"
    proc, _ = _run(tmp_path, "--force", "--reason", "test")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert f"commit_sha: {SHA}" in prov.read_text(encoding="utf-8")


def test_older_release_folder_is_untouched(tmp_path):
    old = _root(tmp_path) / "v3.13.0" / "Deliverables" / "keep.txt"
    old.parent.mkdir(parents=True)
    old.write_text("old release\n", encoding="utf-8")
    proc, _ = _run(tmp_path, "--force", "--reason", "test")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert old.read_text(encoding="utf-8") == "old release\n"
    assert (_rel(tmp_path) / "PROVENANCE.txt").is_file()


def test_truncated_tree_dies(tmp_path):
    tree = _tree([(p, "blob") for p in DATA_FILES], truncated=True)
    proc, _ = _run(tmp_path, tree=tree)
    assert proc.returncode != 0
    assert "truncated" in proc.stderr
    assert not _root(tmp_path).exists()


def test_empty_deliverables_dies(tmp_path):
    tree = _tree([("Documents/Examples/example.json", "blob"), ("README.md", "blob")])
    proc, _ = _run(tmp_path, tree=tree)
    assert proc.returncode != 0
    assert "no files resolved under Deliverables/" in proc.stderr
    assert not _root(tmp_path).exists()


def test_unexpected_tag_dies(tmp_path):
    proc, _ = _run(tmp_path, release=_release("../x"))
    assert proc.returncode != 0
    assert "refusing unexpected release tag" in proc.stderr
    assert not _root(tmp_path).exists()


def test_unresolvable_release_dies_and_writes_nothing(tmp_path):
    proc, _ = _run(tmp_path, release="")
    assert proc.returncode != 0
    assert "GitHub API rate limit?" in proc.stderr
    assert not _root(tmp_path).exists()


def test_positional_dies_unknown_mode(tmp_path):
    proc, _ = _run(tmp_path, "yaml")
    assert proc.returncode != 0
    assert "unknown mode 'yaml'" in proc.stderr
    assert not _root(tmp_path).exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cdisc_usdm_wrapper.py -q`
Expected: all 10 tests FAIL (the wrapper does not exist, so bash exits non-zero and the files are missing). If they all report SKIPPED, `aria2c` is on `PATH`: say so in your report and verify in an environment without it (the cloud sandbox has none).

- [ ] **Step 3: Add the `sources.env` keys**

In `scripts/data/_lib/sources.env`, insert after the COSMoS block (after the `COSMOS_REPO_URL=...` line and its following blank line) and before `# --- CDISC Controlled Terminology`:

```bash
# --- CDISC USDM (DDF-RA GitHub repo; latest release's Deliverables/; download-only) ---
USDM_API_BASE=https://api.github.com/repos/cdisc-org/DDF-RA
USDM_RAW_BASE=https://raw.githubusercontent.com/cdisc-org/DDF-RA
USDM_REPO_URL=https://github.com/cdisc-org/DDF-RA

```

- [ ] **Step 4: Write the wrapper**

Create `scripts/data/cdisc_usdm/download_cdisc_usdm.sh`:

```bash
#!/usr/bin/env bash
# Episteme acquisition wrapper: CDISC USDM (Unified Study Definitions Model), as published
# in the public GitHub repo cdisc-org/DDF-RA. Fetches the Deliverables/ folder of the LATEST
# RELEASE (API spec, USDM controlled terminology, implementation guide, CORE rules, UML
# model). Download-only: no serializer.
# Thin shell over scripts/data/_lib/common.sh — GitHub release/commit/tree + size-skip + fetch.
# No MODE positional: Deliverables/ is the only thing fetched.
# Layout: <raw>/cdisc_usdm/<release-tag>/{Deliverables/...,LICENSE,README.md,PROVENANCE.txt};
# folders of older releases are never touched.
# Licence: the repo LICENSE is MIT for code and scripts; the README grants CC-BY-4.0 to
# "content files like documentation and minutes"; the model files are not named in either -
# verify before redistribution. No serializer exists or is planned for cdisc_usdm until CDISC
# confirms a licence for the model files; this source is excluded from any training corpus.
# Pinning: the release tag is resolved to its commit SHA and every raw fetch uses the SHA, so
# one run is internally consistent even if the tag moves.
# Re-fetch: size-skip cannot detect a same-size change under a moved tag; use --force (with
# --reason) to re-fetch.
# Every run, --dry-run included, makes three GitHub API calls (release, commit, tree); they
# count against the unauthenticated 60 requests/hour limit.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   export EPISTEME_DRY_RUN=1; shift ;;
        --max-files) MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)     FORCE=1; shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        -*)          log WARN "download_cdisc_usdm.sh: ignoring $1"; shift ;;
        *)           die "cdisc_usdm: unknown mode '$1' (this source has no modes)" ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR USDM_API_BASE USDM_RAW_BASE USDM_REPO_URL

_api() { # _api PATH -> response body with CR stripped; empty on failure
    curl -fsS --connect-timeout 30 --max-time 120 "$USDM_API_BASE/$1" 2>/dev/null | tr -d '\r'
}

_raw_url() { # _raw_url REL -> raw URL at the pinned SHA, with % space # ? percent-encoded
    local u="${1//\%/%25}"
    u="${u// /%20}"; u="${u//\#/%23}"; u="${u//\?/%3F}"
    printf '%s/%s/%s' "$USDM_RAW_BASE" "$sha" "$u"
}

tag="$(_api releases/latest | grep -m1 -oE '"tag_name": *"[^"]*"' | sed -E 's/^"tag_name": *"(.*)"$/\1/')"
[ -n "$tag" ] || die "cdisc_usdm: cannot resolve the latest release of $USDM_API_BASE (GitHub API rate limit?)"
[[ "$tag" =~ ^v?[0-9]+(\.[0-9]+)*$ ]] || die "cdisc_usdm: refusing unexpected release tag '$tag'"

# The pretty-printed commit JSON carries the commit's own "sha" first (parents/tree come later).
sha="$(_api "commits/$tag" | grep -m1 -oE '"sha": *"[0-9a-f]{40}"' | grep -oE '[0-9a-f]{40}')"
[ -n "$sha" ] || die "cdisc_usdm: cannot resolve the commit of release $tag (GitHub API rate limit?)"

tree="$(_api "git/trees/$sha?recursive=1")"
[ -n "$tree" ] || die "cdisc_usdm: cannot list the tree of release $tag (GitHub API rate limit?)"
if printf '%s\n' "$tree" | grep -qE '"truncated": *true'; then
    die "cdisc_usdm: GitHub truncated the tree listing of release $tag; refusing a partial download"
fi

dest="$(resolve_dest cdisc_usdm "$tag")"

# One JSON object per tree entry, one field per line ("path" precedes "type"); emit the path
# of every blob under Deliverables/.
planned=()
while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    _safe_rel "$rel" || { log WARN "cdisc_usdm: rejecting unsafe path '$rel'"; continue; }
    planned+=("$(_raw_url "$rel")"$'\t'"$rel")
done < <(printf '%s\n' "$tree" | awk '
    /^ *"path":/ { p=$0; sub(/^ *"path": *"/, "", p); sub(/",? *$/, "", p) }
    /^ *"type":/ { if ($0 ~ /"blob"/ && p ~ /^Deliverables\//) print p; p="" }')
[ "${#planned[@]}" -gt 0 ] || die "cdisc_usdm: no files resolved under Deliverables/ in release $tag"

# --max-files caps the RESOLVED data-file set here, before the --force prune loop.
if [ -n "$MAX_FILES" ]; then
    mapfile -t planned < <(printf '%s\n' "${planned[@]}" | cap_urls "$MAX_FILES")
fi
# The repo LICENSE and README (licence wording) always ride along (not counted against --max-files).
planned+=("$(_raw_url LICENSE)"$'\t'"LICENSE")
planned+=("$(_raw_url README.md)"$'\t'"README.md")

lines=()
for entry in "${planned[@]}"; do
    url="${entry%%$'\t'*}"
    rel="${entry#*$'\t'}"
    # never delete real files during a --dry-run preview
    if [ "$FORCE" = "1" ] && [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then rm -f "$dest/$rel"; fi
    if size_match_skip "$dest/$rel" "$url"; then
        log INFO "skip (size-matched): $rel"
    else
        lines+=("$entry")
    fi
done

if [ "${#lines[@]}" -gt 0 ]; then
    printf '%s\n' "${lines[@]}" | http_fetch "$dest" || die "cdisc_usdm: fetch failed"
else
    log INFO "cdisc_usdm: up to date ($tag)"
fi

if [ "${EPISTEME_DRY_RUN:-0}" != "1" ] && { [ "${#lines[@]}" -gt 0 ] || [ ! -f "$dest/PROVENANCE.txt" ]; }; then
    mkdir -p "$dest"
    {
        printf 'source_repo: %s\n' "$USDM_REPO_URL"
        printf 'release_tag: %s\n' "$tag"
        printf 'commit_sha: %s\n' "$sha"
        printf 'retrieved_at: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'content_licence: LICENSE is MIT for code and scripts; README grants CC-BY-4.0 to content files like documentation and minutes; model files not covered - verify before redistribution\n'
        printf 'corpus_status: excluded - download-only; no serializer exists or is planned for cdisc_usdm until CDISC confirms a licence for the model files; excluded from any training corpus\n'
    } > "$dest/PROVENANCE.txt"
fi
write_sync_stamp "$(resolve_dest cdisc_usdm)"
```

Then make it executable in git: `git update-index --chmod=+x` is applied at commit time in Step 7.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cdisc_usdm_wrapper.py -q`
Expected: `10 passed`. Read the summary line itself; do not grep only for "passed".

- [ ] **Step 6: Run the URL gate and the non-pg suite**

Run: `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'`
Expected: no output.

Run: `.venv/Scripts/python.exe -m pytest -m "not pg" -q`
Expected: everything passes except the pre-existing skips; the summary line shows `0 failed`.

- [ ] **Step 7: Commit**

```bash
ruff format tests/test_cdisc_usdm_wrapper.py && ruff check tests/test_cdisc_usdm_wrapper.py
git add scripts/data/cdisc_usdm/download_cdisc_usdm.sh scripts/data/_lib/sources.env tests/test_cdisc_usdm_wrapper.py
git update-index --chmod=+x scripts/data/cdisc_usdm/download_cdisc_usdm.sh
git commit -m "feat(sp8): cdisc_usdm download-only wrapper for the latest USDM release

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z"
```

---

### Task 2: Register `cdisc_usdm`, document it, pin the docs

**Files:**
- Modify: `scripts/data/run_pipeline.sh` (the `WRAPPER` table, after `[cdisc_ct]=...`)
- Modify: `tests/test_run_pipeline_dispatch.py` (`FAST_TOKENS`; new test after `test_cdisc_bc_is_download_only`)
- Modify: `docs/02-data-sources.md` (§2.6 USDM row; backlog "Blocked" row; document-control table)
- Modify: `docs/09-extraction-contract.md` (§6.3 table, after the `cdisc_bc` row)
- Modify: `docs/10-data-sources-runbook.md` (stage table download-only row ~line 237; `--dry-run` bullet ~line 244; new §5.7 after §5.6's last table and before the `---` that precedes `## 6.`; "On upstream release" row ~line 692)
- Modify: `docs/12-source-inventory.md` (row after `cdisc_ct`)
- Modify: `CLAUDE.md` (hard rule)
- Modify: `docs/project-incubation-baseline.md` (append drift-log entry at end of file)
- Test: `tests/docs/test_extraction_contract_doc.py`, `tests/docs/test_runbook_doc.py`, `tests/docs/test_source_inventory_doc.py`

**Interfaces:**
- Consumes: `scripts/data/cdisc_usdm/download_cdisc_usdm.sh` from Task 1 (flags `--dry-run`, `--max-files N`, `--force`, `--reason R`; layout `<raw>/cdisc_usdm/<tag>/`; `PROVENANCE.txt` fields `source_repo`, `release_tag`, `commit_sha`, `retrieved_at`, `content_licence`, `corpus_status`).
- Produces: `cdisc_usdm` as a wired source name in `run_pipeline.sh` (download only).

- [ ] **Step 1: Write the failing doc and dispatch tests**

In `tests/docs/test_source_inventory_doc.py`, inside `test_licence_caveats_are_recorded_per_row`, after the three `cdisc_bc` asserts add:

```python
    usdm = " ".join(rows["cdisc_usdm"])
    assert "UNVERIFIED" in usdm  # model-file licence
    assert "until CDISC confirms" in usdm
    assert "training corpus" in usdm
```

In `tests/docs/test_extraction_contract_doc.py`, add after `test_uniprot_override_and_cdisc_note`:

```python
def test_cdisc_usdm_row_is_download_only():
    sec = _section(doc(DOC), "### 6.3", "\n---")
    row = next(line for line in sec.splitlines() if line.startswith("| `cdisc_usdm`"))
    assert "download-only" in row
    assert "until CDISC confirms" in row
    assert "training corpus" in row
```

In `tests/docs/test_runbook_doc.py`, add after `test_cdisc_bc_corpus_lockdown_is_documented`:

```python
def test_cdisc_usdm_section_is_documented():
    sec = _section(_text(), "### 5.7", "\n---")
    assert "`cdisc_usdm`" in sec
    assert "Download-only" in sec
    assert "Deliverables/" in sec
    assert "release_tag" in sec and "commit_sha" in sec
    assert "until CDISC confirms a licence for the model files" in sec
    assert "excluded from any training corpus" in sec
    assert "three GitHub API calls" in sec
    row = next(line for line in _text().splitlines() if line.startswith("| `europepmc_abstracts`"))
    assert "`cdisc_usdm`" in row  # download-only row of the stage table
```

In `tests/test_run_pipeline_dispatch.py`, add `"cdisc_usdm",` to `FAST_TOKENS` directly after `"cdisc_ct",`, and add after `test_cdisc_bc_is_download_only`:

```python
@pytest.mark.parametrize("stage", ["serialize", "extract", "load"])
def test_cdisc_usdm_is_download_only(tmp_path, stage):
    # cdisc_usdm is in WRAPPER only (not LIT_SOURCES / STRUCTURED_SOURCES): early validation dies 3.
    proc = _run(["cdisc_usdm", stage], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "unknown source" not in proc.stderr  # registered in WRAPPER, refused for the stage
    assert f"cdisc_usdm {stage} is not in SP" in proc.stderr
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/docs tests/test_run_pipeline_dispatch.py -q -k "usdm or licence_caveats"`
Expected: the new doc tests FAIL (`KeyError: 'cdisc_usdm'`, `StopIteration`, or `IndexError` from `_section`); the dispatch tests FAIL with "unknown source" (or the whole dispatch module SKIPS if there is no network — then note it in your report).

- [ ] **Step 3: Register the source**

In `scripts/data/run_pipeline.sh`, in the `WRAPPER` associative array, add after `[cdisc_ct]="cdisc_ct/download_cdisc_ct.sh"`:

```bash
    [cdisc_usdm]="cdisc_usdm/download_cdisc_usdm.sh"
```

Do not add it to `LIT_SOURCES` or `STRUCTURED_SOURCES`.

- [ ] **Step 4: Update `docs/12-source-inventory.md`**

Add this row directly after the `cdisc_ct` row:

```markdown
| `cdisc_usdm` | volatile (Stream 2) | download (no serializer) | **UNVERIFIED**: the repo README says code and scripts are MIT and content files like documentation and minutes are CC-BY-4.0; the model files are named in neither (`download_cdisc_usdm.sh` header); until CDISC confirms, this source stays out of any training corpus | per USDM release (latest release tag, pinned per run to its commit) | `cdisc_usdm/download_cdisc_usdm.sh` |
```

- [ ] **Step 5: Update `docs/09-extraction-contract.md` §6.3**

Add this row directly after the `cdisc_bc` row:

```markdown
| `cdisc_usdm` | none | download-only, no serializer, no `articles` rows; licence **unverified** (the repo README grants MIT to code and scripts and CC-BY-4.0 to documentation and minutes; the USDM model files are named in neither); no `subset` is ever assigned, and until CDISC confirms a licence for the model files no row from this source may enter a training corpus |
```

- [ ] **Step 6: Update `docs/10-data-sources-runbook.md`**

(a) Stage table, the download-only row that starts `| \`europepmc_abstracts\``: change `` `aact`, `cdisc_bc` `` to `` `aact`, `cdisc_bc`, `cdisc_usdm` ``.

(b) `--dry-run` bullet (~line 244): after `` `cdisc_bc` makes two GitHub API calls that count against the unauthenticated 60 requests per hour limit`` insert `` and `cdisc_usdm` makes three``, so it reads: `` (`curl` listings, mirror and release probes; `cdisc_bc` makes two GitHub API calls and `cdisc_usdm` makes three, all counting against the unauthenticated 60 requests per hour limit) ``.

(c) Insert this new section after the last table of §5.6 and before the `---` line that precedes `## 6. Materialize`:

````markdown
### 5.7 CDISC USDM (`cdisc_usdm`)

CDISC's Unified Study Definitions Model from the public GitHub repository `cdisc-org/DDF-RA`: the `Deliverables/` folder of the latest release (OpenAPI spec, USDM controlled terminology, implementation guide PDF, CORE rules, UML model and data dictionary; about 15 MB for `v4.0.0`). **Download-only; there is no serializer.** No modes; a stray positional token makes the wrapper die (rc 1 through the dispatcher).

- **Pinned to one release.** Each run resolves the latest release tag, then that tag's commit SHA, then lists the whole tree at that SHA in one call; every file is fetched at the SHA. Files land in `<raw>/cdisc_usdm/<release-tag>/`, for example `<raw>/cdisc_usdm/v4.0.0/Deliverables/API/USDM_API.json`. A new release gets its own folder; older release folders are never touched.
- **`PROVENANCE.txt`** is written in the release folder: `source_repo`, `release_tag`, `commit_sha`, `retrieved_at`, and the licence and corpus-status notes. The repository `LICENSE` and `README.md` (which carries the licence wording) are stored beside it and do not count against `--max-files`.
- **Licence: UNVERIFIED.** The README grants MIT to code and scripts and CC-BY-4.0 to content files like documentation and minutes; the USDM model files are named in neither. No serializer exists or is planned until CDISC confirms a licence for the model files; this source is excluded from any training corpus. Verify before any redistribution (docs/12).
- **Re-fetch:** size-matched files are skipped, so a same-size change under a moved tag is not detected; run with `--force --reason "..."` in that case.
- Every run, `--dry-run` included, makes three GitHub API calls (release, commit, tree). Unauthenticated GitHub allows 60 requests per hour; hitting the limit fails the stage (rc 1 through the dispatcher) with `cannot resolve ... (GitHub API rate limit?)`.

```bash
# bounded first run: two data files plus LICENSE, README.md and PROVENANCE.txt
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh cdisc_usdm download --max-files 2

# preview (network: GitHub API only; writes nothing)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh cdisc_usdm download --dry-run

# force a refetch of the current release
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh cdisc_usdm download --force --reason "refetch USDM release"
```

Sample success (bounded first run): exit code 0. Without `aria2c` you see a WARN that it was not found and downloads fall back to sequential `curl`; that is expected. The run ends with `write_sync_stamp: ./01_raw/cdisc_usdm/last_sync_utc.txt` and `pipeline done: cdisc_usdm download`, and `<raw>/cdisc_usdm/<release-tag>/` then holds `Deliverables/`, `LICENSE`, `README.md` and `PROVENANCE.txt`.
````

(d) Ops cadence row `| On upstream release |` (~line 692): after `` `cdisc_bc` with `--force --reason` `` append `` ; `cdisc_usdm` on a new USDM release (no `--force` needed: a new tag gets a new folder) ``.

- [ ] **Step 7: Update `docs/02-data-sources.md`**

(a) §2.6, the row starting `| **USDM**`: replace its last cell (`**Unverified** — treat like ...; not started`) with `**Wired as \`cdisc_usdm\`, download-only** (latest release's \`Deliverables/\`); no serializer and no corpus inclusion until CDISC confirms (see \`12-source-inventory.md\`)`. Keep the licence cell's facts; if it does not already say so, make it say that the README grants MIT to code and scripts and CC-BY-4.0 to "content files like documentation and minutes", and the model files are not covered.

(b) Backlog row `| Blocked | **Biomedical Concepts / USDM** | ...`: change the middle cell to `Download-only (\`cdisc_bc\` and \`cdisc_usdm\` wired)`.

(c) Document-control table: append

```markdown
| 2026-09-29 | SP8 | USDM wired as `cdisc_usdm` (download-only, latest release's `Deliverables/`); §2.6 and backlog rows updated |
```

- [ ] **Step 8: Update `CLAUDE.md`**

Replace the line

```markdown
- `cdisc_bc` stays download-only: no serializer, no corpus inclusion, until CDISC confirms a licence for its `export/` data.
```

with

```markdown
- `cdisc_bc` and `cdisc_usdm` stay download-only: no serializer, no corpus inclusion, until CDISC confirms a licence for their data files (`cdisc_bc`: `export/`; `cdisc_usdm`: the USDM model files).
```

- [ ] **Step 9: Append the drift-log entry**

At the end of `docs/project-incubation-baseline.md` (the drift log is a chronological bullet list; the last entry is the 2026-09-29 SP6/SP7 close-out), append:

```markdown
- 2026-09-29: SP8 — CDISC USDM wired as `cdisc_usdm`, download-only (spec `docs/superpowers/specs/2026-09-29-sp8-cdisc-usdm-download-design.md`).
  - **Decision (user, 2026-09-29):** the internal-use licence exception for Biomedical Concepts and USDM was offered and declined; both stay download-only until CDISC confirms a licence for the data files. "Not reselling" is not the test — redistribution of the data or of a model trained on it is.
  - **Landed:** `scripts/data/cdisc_usdm/download_cdisc_usdm.sh` (latest release tag → commit SHA → one recursive tree call; `Deliverables/` only; `<raw>/cdisc_usdm/<tag>/` with `LICENSE`, `README.md`, `PROVENANCE.txt`); `USDM_*` keys in `sources.env`; `WRAPPER` registration (download only); docs 02, 09, 10 §5.7, 12; `CLAUDE.md` hard rule extended.
  - **Deferred:** a USDM reader (needs CDISC's licence confirmation); an authenticated GitHub token for the 60/hour API limit; the wrapper tests skip where `aria2c` is installed (same known limitation as `cdisc_bc` / `cdisc_ct`).
```

- [ ] **Step 10: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/docs tests/test_run_pipeline_dispatch.py -q -k "usdm or licence_caveats or inventory or runbook"`
Expected: all selected tests pass (the dispatch tests may SKIP only if there is no network; say so if they do).

Run: `.venv/Scripts/python.exe -m pytest -m "not pg" -q`
Expected: summary line shows `0 failed`.

Run the URL gate: `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'`
Expected: no output.

- [ ] **Step 11: Commit**

```bash
ruff format tests/docs/test_extraction_contract_doc.py tests/docs/test_runbook_doc.py tests/docs/test_source_inventory_doc.py tests/test_run_pipeline_dispatch.py
ruff check tests/docs/test_extraction_contract_doc.py tests/docs/test_runbook_doc.py tests/docs/test_source_inventory_doc.py tests/test_run_pipeline_dispatch.py
git add scripts/data/run_pipeline.sh tests/test_run_pipeline_dispatch.py tests/docs/test_extraction_contract_doc.py tests/docs/test_runbook_doc.py tests/docs/test_source_inventory_doc.py docs/02-data-sources.md docs/09-extraction-contract.md docs/10-data-sources-runbook.md docs/12-source-inventory.md CLAUDE.md docs/project-incubation-baseline.md
git commit -m "feat(sp8): register cdisc_usdm (download only) and document it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z"
```
