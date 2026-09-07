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
    echo "usage: $0 <source> <all|download|extract|load|graph|materialize|enrich> [--force] [--reason REASON] [--max-files N]" >&2
    exit 2
}

SOURCE="${1:-}"
STAGE="${2:-}"
[ -n "$SOURCE" ] && [ -n "$STAGE" ] || usage
shift 2 || true

FORCE=0
REASON=""
MAX_FILES=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --reason) REASON="${2:-}"; shift 2 ;;
        # download_pmc.py's flag is --limit, extract_pmc.py's is --max-files --
        # one orchestrator flag, translated per downstream script below.
        --max-files) MAX_FILES="${2:-}"; shift 2 ;;
        # env-only signal for the fetch engine / wrappers; never appended to
        # any *_args array. Exported so every downstream subprocess sees it.
        --dry-run) export EPISTEME_DRY_RUN=1; shift ;;
        *) log WARN "run_pipeline.sh: ignoring unknown arg $1"; shift ;;
    esac
done

if [ "$FORCE" = "1" ] && [ -z "$REASON" ]; then
    die "--force requires --reason" 2
fi

# source token -> download wrapper path (relative to $HERE). SP3 wires the
# `download` stage for every source through these; pmc additionally keeps its
# full extract/load/graph/materialize/enrich chain (dispatch block below).
# The wrapper files themselves land in Tasks 5-9 — a missing one is caught by
# the `[ -f "$wpath" ]` guard, so this script is committable before they exist.
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

# Early validation — *before* anything that touches the DB (the run_start audit
# call below). A bad source/stage, or a source+stage combination SP3 does not
# cover, must be a plain error with a clean exit — never a dangling,
# DB-dependent run_start with no matching run_end.
if [ -z "${WRAPPER[$SOURCE]:-}" ]; then
    die "unknown source '$SOURCE' (see scripts/data/_lib/check_prereqs.sh / the roadmap source list)" 3
fi

case "$STAGE" in
    all|download|extract|load|graph|materialize|enrich) ;;
    *) usage ;;
esac

# Non-pmc sources: SP3 wires only `download` (and `all`, which runs download
# then stops). Every other stage is SP2 (literature extract) / SP4 (structured
# serialize). Validate the wrapper path here too, so a not-yet-implemented
# wrapper fails before the audit bracket rather than after it.
if [ "$SOURCE" != "pmc" ]; then
    case "$STAGE" in
        download|all)
            wpath="$HERE/${WRAPPER[$SOURCE]}"
            [ -f "$wpath" ] || die "wrapper not found: $wpath (not yet implemented?)" 3
            ;;
        *)
            die "$SOURCE $STAGE is not in SP3 — SP2 (literature extract) / SP4 (structured serialize)" 3
            ;;
    esac
fi

RUN_ID="${SOURCE}-$(date -u +%Y%m%dT%H%M%SZ)"
# Exported so every stage's `python -m episteme.data.*` subprocess (via
# config.get_settings().run_id) shares this one run_id instead of each
# generating its own -- otherwise the audit trail fragments one pipeline
# invocation across N unrelated run_ids.
export EPISTEME_RUN_ID="$RUN_ID"

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

download_args=()
extract_args=()
if [ -n "$MAX_FILES" ]; then
    download_args=(--limit "$MAX_FILES")
    extract_args=(--max-files "$MAX_FILES")
fi
[ "$FORCE" = "1" ] && extract_args+=(--force)

# Passthrough args for the table-driven bulk wrappers. Unlike download_args
# (pmc's download_pmc.py takes --limit N), the SP3 wrappers take --max-files N
# and --force --reason R. --dry-run is NOT here — it rides EPISTEME_DRY_RUN.
wrapper_args=()
[ -n "$MAX_FILES" ] && wrapper_args+=(--max-files "$MAX_FILES")
[ "$FORCE" = "1" ] && wrapper_args+=(--force --reason "$REASON")

# Best-effort: a down/unreachable DB must not silently skip the run_start
# bracket. extract's own audit already degrades to a file-only mirror when
# the DB is unreachable (see extract_pmc.py), so the pipeline itself stays
# usable without one -- but say so loudly rather than swallowing the failure.
# DR-1: a --dry-run must be side-effect-free and DB-independent, so the whole
# audit bracket (run_start here, run_end below) is skipped on a dry run.
if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
    log INFO "dry-run: skipping run_start/run_end audit bracket"
else
    "$PY" -m episteme.audit_trail record run_start --object "$SOURCE" --run-id "$RUN_ID" > /dev/null \
        || log WARN "run_start audit failed; proceeding unaudited (is the DB up?)"
fi

if [ "$SOURCE" = "pmc" ]; then
    # pmc keeps its full stage chain.
    case "$STAGE" in
        download)    run_stage download "$HERE/pmc/download_pmc.sh" "${download_args[@]}" ;;
        extract)     run_stage extract "$HERE/pmc/extract_pmc.sh" "${extract_args[@]}" ;;
        load)        run_stage load "$HERE/pmc/load_pmc.sh" "${load_args[@]}" ;;
        graph)       run_stage graph "$HERE/pmc/graph_pmc.sh" ;;
        materialize) run_stage materialize "$HERE/materialize_corpus.sh" ;;
        enrich)      run_stage enrich "$HERE/pmc/enrich_pmc.sh" ;;
        all)
            run_stage download "$HERE/pmc/download_pmc.sh" "${download_args[@]}"
            run_stage extract "$HERE/pmc/extract_pmc.sh" "${extract_args[@]}"
            run_stage load "$HERE/pmc/load_pmc.sh" "${load_args[@]}"
            run_stage graph "$HERE/pmc/graph_pmc.sh"
            run_stage materialize "$HERE/materialize_corpus.sh"
            run_stage enrich "$HERE/pmc/enrich_pmc.sh"
            ;;
    esac
else
    # Non-pmc: SOURCE, STAGE and $wpath were all validated before the audit
    # bracket. Only `download` / `all` reach here; both run the one wrapper.
    bash "$HERE/_lib/check_prereqs.sh" >&2 || true
    run_stage download bash "$wpath" "${wrapper_args[@]}"
    if [ "$STAGE" = "all" ]; then
        log INFO "$SOURCE: only 'download' is wired in SP3 (extract/serialize = SP2/SP4)"
    fi
fi

if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
    log INFO "dry-run: audit bracket skipped (no run_end)"
else
    "$PY" -m episteme.audit_trail record run_end --object "$SOURCE" --run-id "$RUN_ID" > /dev/null \
        || log WARN "run_end audit failed (is the DB up?)"
fi
log INFO "pipeline done: $SOURCE $STAGE"
