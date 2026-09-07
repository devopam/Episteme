#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: openFDA bulk JSON zips. Thin shell over
# scripts/data/_lib/common.sh — catalog fetch + zip-URL parse + size-skip +
# fetch only (no extract/parse). Download-only, forever. The catalog URL comes
# from $OPENFDA_CATALOG (scripts/data/_lib/sources.env); the zip URLs are read
# out of that catalog's JSON at run time (not literals in this file).
# MODE positional: label (default) | drug | all_human | all.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

MODE="label"
MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   export EPISTEME_DRY_RUN=1; shift ;;
        --max-files) MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)     FORCE=1; shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        label|drug|all_human|all) MODE="$1"; shift ;;
        *)           log WARN "download_openfda.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR OPENFDA_CATALOG

# Prefer the project venv's interpreter; PYTHON overrides; bare `python` last.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "openfda: python not found (set PYTHON=)"

dest="$(resolve_dest openfda)"

# Ported verbatim from the batch script: walk() the catalog JSON, want() filters
# by MODE, order-preserving dedup. Reads argv[1]=MODE and stdin=catalog JSON,
# prints one .zip URL per line. Keep the heredoc single-quoted (no shell expand).
PYSRC="$(cat <<'PY'
import json, sys

mode = sys.argv[1]
data = json.load(sys.stdin)

# download.json shape: {"results": {"drug": {"label": {"export_date":..., "partitions":[{"file":url,...}]}}}}
results = data.get("results") or data
urls = []

def walk(obj, path=""):
    if isinstance(obj, dict):
        if "partitions" in obj and isinstance(obj["partitions"], list):
            for p in obj["partitions"]:
                u = p.get("file") or p.get("url")
                if u:
                    urls.append((path, u))
        for k, v in obj.items():
            walk(v, f"{path}/{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk(v, path)

walk(results)

def want(path: str) -> bool:
    p = path.lower()
    if mode == "all":
        return True
    if mode == "label":
        return "drug/label" in p or p.endswith("label") or "/label" in p
    if mode == "drug":
        keys = ("drug/label", "drug/event", "drug/ndc", "drug/drugsfda", "/label", "/event", "/ndc", "/drugsfda")
        return any(k in p for k in keys)
    if mode == "all_human":
        return p.startswith("drug") or "/drug/" in p
    return False

seen = set()
selected = 0
for path, u in urls:
    if not want(path):
        continue
    if u in seen:
        continue
    seen.add(u)
    selected += 1
    print(u)
print(f"selected_urls={selected}", file=sys.stderr)
PY
)"

mapfile -t urls < <(curl -sSL --fail --connect-timeout 30 --max-time 120 "$OPENFDA_CATALOG" | "$PY" -c "$PYSRC" "$MODE")
[ "${#urls[@]}" -gt 0 ] || die "openfda: no zip URLs for mode=$MODE"

lines=()
for url in "${urls[@]}"; do
    [ -n "$url" ] || continue
    base="${url##*/}"; base="${base%%\?*}"    # flat dest, namespaced by basename (no per-iter fork)
    # force a re-fetch — but never delete real files during a --dry-run preview
    if [ "$FORCE" = "1" ] && [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then rm -f "$dest/$base"; fi
    if size_match_skip "$dest/$base" "$url"; then
        log INFO "skip (size-matched): $base"
    else
        lines+=("$url")
    fi
done

if [ "${#lines[@]}" -eq 0 ]; then
    log INFO "openfda: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | cap_urls "$MAX_FILES" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "openfda: fetch failed"
fi
