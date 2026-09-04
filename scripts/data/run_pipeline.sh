#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/_lib/common.sh"
load_dotenv
require_env EPISTEME_ACTOR

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../.venv/Scripts/python.exe" "$HERE/../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

usage() {
    echo "usage: $0 <source> <all|download|extract|load|graph|materialize|enrich> [--force] [--reason REASON]" >&2
    exit 2
}

SOURCE="${1:-}"
STAGE="${2:-}"
[ -n "$SOURCE" ] && [ -n "$STAGE" ] || usage
shift 2 || true

FORCE=0
REASON=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --reason) REASON="${2:-}"; shift 2 ;;
        *) log WARN "run_pipeline.sh: ignoring unknown arg $1"; shift ;;
    esac
done

if [ "$FORCE" = "1" ] && [ -z "$REASON" ]; then
    die "--force requires --reason" 2
fi

if [ "$SOURCE" != "pmc" ]; then
    die "source '$SOURCE' is not wired in SP1-beta (pmc only)" 3
fi

# Validate STAGE *before* anything that touches the DB (the run_start audit
# call below) — a bad stage name should be a plain usage error, not a
# dangling, DB-dependent run_start with no matching run_end.
case "$STAGE" in
    all|download|extract|load|graph|materialize|enrich) ;;
    *) usage ;;
esac

RUN_ID="${SOURCE}-$(date -u +%Y%m%dT%H%M%SZ)"

run_stage() {
    local name="$1"
    shift
    log INFO "stage: $name"
    if ! "$@"; then
        log ERROR "stage failed: $name"
        log ERROR "resume with: $0 $SOURCE $name"
        "$PY" -m episteme.audit_trail record run_end --object "$SOURCE" --run-id "$RUN_ID" --reason "failed at stage $name" >/dev/null 2>&1 || true
        exit 1
    fi
}

load_args=()
[ "$FORCE" = "1" ] && load_args=(--force --reason "$REASON")

# Best-effort: a down/unreachable DB must not silently skip the run_start
# bracket. extract's own audit already degrades to a file-only mirror when
# the DB is unreachable (see extract_pmc.py), so the pipeline itself stays
# usable without one -- but say so loudly rather than swallowing the failure.
"$PY" -m episteme.audit_trail record run_start --object "$SOURCE" --run-id "$RUN_ID" > /dev/null \
    || log WARN "run_start audit failed; proceeding unaudited (is the DB up?)"

case "$STAGE" in
    download)    run_stage download "$HERE/pmc/download_pmc.sh" ;;
    extract)     run_stage extract "$HERE/pmc/extract_pmc.sh" ;;
    load)        run_stage load "$HERE/pmc/load_pmc.sh" "${load_args[@]}" ;;
    graph)       run_stage graph "$HERE/pmc/graph_pmc.sh" ;;
    materialize) run_stage materialize "$HERE/materialize_corpus.sh" ;;
    enrich)      run_stage enrich "$HERE/pmc/enrich_pmc.sh" ;;
    all)
        run_stage download "$HERE/pmc/download_pmc.sh"
        run_stage extract "$HERE/pmc/extract_pmc.sh"
        run_stage load "$HERE/pmc/load_pmc.sh" "${load_args[@]}"
        run_stage graph "$HERE/pmc/graph_pmc.sh"
        run_stage materialize "$HERE/materialize_corpus.sh"
        run_stage enrich "$HERE/pmc/enrich_pmc.sh"
        ;;
esac

"$PY" -m episteme.audit_trail record run_end --object "$SOURCE" --run-id "$RUN_ID" > /dev/null \
    || log WARN "run_end audit failed (is the DB up?)"
log INFO "pipeline done: $SOURCE $STAGE"
