#!/usr/bin/env bash
# Episteme acquisition wrapper: official reference documents. The first argument names the
# source (run_pipeline.sh passes it): ich_e2 (ICH guideline files), meddra_ptc (MedDRA Points
# to Consider) or ema_guidance (EMA guidance documents). Download-only: no load stage.
# Files go to <raw root>/<source>/ with a _manifest.jsonl (sha256, size, licence, input_key).
# A known file is skipped without a request; --force asks upstream again (a changed file is
# kept as a new file with a date suffix). EMA answers a client that is not a browser with
# HTTP 429: ema_guidance waits EPISTEME_EMA_DOC_MIN_INTERVAL_S (default 60) between requests,
# honours Retry-After, and stops with exit 3 so that a later run resumes. Never fetch through
# a browser to get round the limit; never touch /search/.
# ICH and MedDRA wait EPISTEME_REFERENCE_DOCS_MIN_INTERVAL_S (default 2) between requests.
# Flags: --dry-run (plans; the ICH and MedDRA plans read the API), --max-files N,
# --budget-s SECONDS (stop with exit 3 after that time), --force (with --reason).
# Licence: ICH and MedDRA PtC documents carry the ICH public-licence notice (use with ICH
# credit, mark changes). EMA: reuse with credit. MedDRA terminology is out of scope.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv
require_env EPISTEME_ACTOR

SOURCE="${1:-}"
case "$SOURCE" in
    ich_e2|meddra_ptc|ema_guidance) shift ;;
    *) die "reference_docs: first argument must be ich_e2, meddra_ptc or ema_guidance (got '${SOURCE}')" ;;
esac

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "reference_docs: python not found (set PYTHON=/path/to/python)"

args=()
# run_pipeline.sh passes --dry-run as EPISTEME_DRY_RUN=1.
[ "${EPISTEME_DRY_RUN:-0}" = "1" ] && args+=(--dry-run)
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   args+=(--dry-run); shift ;;
        --max-files) args+=(--max-files "${2:-}"); shift; [ $# -gt 0 ] && shift ;;
        --budget-s)  args+=(--budget-s "${2:-}"); shift; [ $# -gt 0 ] && shift ;;
        --force)     args+=(--refresh); shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        -*)          log WARN "download_reference_docs.sh: ignoring $1"; shift ;;
        *)           die "reference_docs: unknown argument '$1' (the source has no modes)" ;;
    esac
done

exec "$PY" -m episteme.data.reference_docs.acquire "$SOURCE" "${args[@]+"${args[@]}"}"
