#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/_lib/common.sh"
load_dotenv
# require_env EPISTEME_ACTOR is deferred to *after* argument validation (M5) so a
# no-arg invocation prints usage() rather than a "missing env var" error.

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../.venv/Scripts/python.exe" "$HERE/../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

usage() {
    echo "usage: $0 <source> <all|download|extract|load|graph|materialize|enrich> [--dry-run] [--force] [--reason REASON] [--max-files N]" >&2
    exit 2
}

SOURCE="${1:-}"
STAGE="${2:-}"
[ -n "$SOURCE" ] && [ -n "$STAGE" ] || usage
shift 2 || true

# M5: validate args first, THEN require the audit actor — so `run_pipeline.sh`
# with no/bad args gives usage(), not "missing required environment variable".
require_env EPISTEME_ACTOR

FORCE=0
REASON=""
MAX_FILES=""
# I1: unrecognised tokens are wrapper MODE positionals (openalex `parquet`,
# hf_corpus `<repo_id>`, chembl `all`, …). Collect and forward them to the
# table-driven wrapper rather than WARN-and-drop — the wrapper is the layer that
# can judge a real typo (e.g. openalex's F-2 `die`). pmc's stage chain below is
# deliberately NOT changed: its downstream flags are all `--`-prefixed.
passthrough=()
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        # `shift; [ $# -gt 0 ] && shift` not `shift 2`: a trailing valueless
        # --reason/--max-files would make `shift 2` fail (count out of range),
        # leave $1 unchanged, and spin the while-loop forever.
        --reason) REASON="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        # download_pmc.py's flag is --limit, extract_pmc.py's is --max-files --
        # one orchestrator flag, translated per downstream script below.
        --max-files) MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        # env-only signal for the fetch engine / wrappers; never appended to
        # any *_args array. Exported so every downstream subprocess sees it.
        --dry-run) export EPISTEME_DRY_RUN=1; shift ;;
        *) passthrough+=("$1"); shift ;;
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
    [guidelines]="guidelines/download_guidelines.sh"
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

# SP2 literature sources: the six that get the full extract -> load -> graph
# chain (dispatch to $HERE/<source>/<stage>_<source>.sh). Every other source is
# download-only in SP2 (structured serialize = SP4). `_is_lit` gates the
# extract/load/graph arms below.
LIT_SOURCES="pubmed apollo europepmc_manuscript europepmc_preprint guidelines bookshelf"
_is_lit() { case " $LIT_SOURCES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# Early validation — *before* anything that touches the DB (the run_start audit
# call below). A bad source/stage, or a source+stage combination SP3 does not
# cover, must be a plain error with a clean exit — never a dangling,
# DB-dependent run_start with no matching run_end.
# `corpus` is not a source token — it's the materialize-only alias handled in the
# dispatch chain below (SOURCE == "corpus" arm). Let it past the WRAPPER lookup.
if [ "$SOURCE" != "corpus" ] && [ -z "${WRAPPER[$SOURCE]:-}" ]; then
    die "unknown source '$SOURCE' (see scripts/data/_lib/check_prereqs.sh / the roadmap source list)" 3
fi

case "$STAGE" in
    all|download|extract|load|graph|materialize|enrich) ;;
    *) usage ;;
esac

# SP2 generalises SP3's C2: --dry-run is meaningful only for `download` (the
# fetch wrappers have a real EPISTEME_DRY_RUN no-op). Every other stage writes
# the DB; DR-1 would strip its audit bracket -> a real unaudited write. Refuse.
# `all` is in the list too: it runs a write stage, so the review wants the
# combination refused outright, not run download-only and silently partial.
case "$STAGE" in
    extract|load|graph|enrich|materialize|all)
        [ "${EPISTEME_DRY_RUN:-0}" != "1" ] \
            || die "$SOURCE $STAGE writes the DB — --dry-run is supported on 'download' only" 3 ;;
esac

# Non-pmc sources: SP3 wires `download` (and `all`) for every source; SP2 adds
# the `extract`/`load`/`graph` chain for the six literature sources only.
# Validate the wrapper path here too, so a not-yet-implemented wrapper fails
# before the audit bracket rather than after it. `corpus` (materialize alias) is
# excluded — it has its own dispatch arm and its own pre-bracket reject below.
if [ "$SOURCE" != "pmc" ] && [ "$SOURCE" != "corpus" ]; then
    case "$STAGE" in
        download|all)
            wpath="$HERE/${WRAPPER[$SOURCE]}"
            [ -f "$wpath" ] || die "wrapper not found: $wpath (not yet implemented?)" 3
            ;;
        extract|load|graph)
            _is_lit "$SOURCE" || die "$SOURCE $STAGE is not in SP2 — SP4 (structured serialize)" 3
            litw="$HERE/$SOURCE/${STAGE}_${SOURCE}.sh"
            [ -f "$litw" ] || die "wrapper not found: $litw (not yet implemented?)" 3
            ;;
        *)
            die "$SOURCE $STAGE is not in SP3 — SP2 (literature extract) / SP4 (structured serialize)" 3
            ;;
    esac
fi

# `corpus` is the materialize-only alias: reject every other stage here, before
# the audit bracket, so a bad `corpus <stage>` exits clean with no dangling
# run_start (matches the early-validation principle above). The dispatch arm
# below carries a belt-and-braces `die` for the same case.
if [ "$SOURCE" = "corpus" ] && [ "$STAGE" != "materialize" ]; then
    die "corpus: only 'materialize' is wired" 3
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
        # DR-1: a dry run touches no DB, so it also emits no failure-path run_end
        # (that would be a lone run_end with no matching run_start).
        [ "${EPISTEME_DRY_RUN:-0}" = "1" ] \
            || "$PY" -m episteme.audit_trail record run_end --object "$SOURCE" --run-id "$RUN_ID" --reason "failed at stage $name" >/dev/null 2>&1 || true
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
# pmc's download_pmc.py takes --dry-run directly (a true pre-network no-op); the
# table-driven wrappers instead read EPISTEME_DRY_RUN from the environment.
[ "${EPISTEME_DRY_RUN:-0}" = "1" ] && download_args+=(--dry-run)
[ "$FORCE" = "1" ] && extract_args+=(--force)

# SP2 lit `extract` wrappers (bookshelf/extract_bookshelf.sh, ...) accept only
# --max-files N and --force -- NOT --reason, NOT the SP3 wrapper_args superset.
# `load` reuses load_args above; `graph` gets no extra args (graph_builder.main
# takes only --source/--raw-dir and errors on anything else, PF-8).
lit_extract_args=()
[ -n "$MAX_FILES" ] && lit_extract_args+=(--max-files "$MAX_FILES")
[ "$FORCE" = "1" ] && lit_extract_args+=(--force)

# Passthrough args for the table-driven bulk wrappers. Unlike download_args
# (pmc's download_pmc.py takes --limit N), the SP3 wrappers take --max-files N
# and --force --reason R. --dry-run is NOT here — it rides EPISTEME_DRY_RUN.
wrapper_args=()
[ -n "$MAX_FILES" ] && wrapper_args+=(--max-files "$MAX_FILES")
[ "$FORCE" = "1" ] && wrapper_args+=(--force --reason "$REASON")
# I1: forward the collected MODE positionals last (guard the expansion for set -u).
[ "${#passthrough[@]}" -eq 0 ] || wrapper_args+=("${passthrough[@]}")

# Set in the non-pmc early-validation block above; declared here too so the
# cross-block use in the dispatch `else` is explicit under `set -u`.
wpath="${wpath:-}"

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

# M4: tool report for EVERY source (was non-pmc only), once, before dispatch.
bash "$HERE/_lib/check_prereqs.sh" >&2 || true

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
elif [ "$SOURCE" = "corpus" ]; then
    # `corpus materialize` alias -> the same shard writer pmc's `materialize`
    # stage runs (materialize_corpus.sh). --dry-run already died 3 above. Any
    # non-materialize stage already died 3 in early validation; the arm below is
    # belt-and-braces.
    case "$STAGE" in
        materialize) run_stage materialize "$HERE/materialize_corpus.sh" ;;
        *)           die "corpus: only 'materialize' is wired" 3 ;;
    esac
else
    # Non-pmc: SOURCE, STAGE and $wpath were all validated before the audit
    # bracket. `download` runs the one bulk wrapper; the six literature sources
    # also get `extract`/`load`/`graph` (dispatch to $HERE/<source>/<stage>_<source>.sh).
    case "$STAGE" in
        download) run_stage download bash "$wpath" "${wrapper_args[@]}" ;;
        extract)  run_stage extract bash "$HERE/$SOURCE/extract_$SOURCE.sh" "${lit_extract_args[@]}" ;;
        load)     run_stage load bash "$HERE/$SOURCE/load_$SOURCE.sh" "${load_args[@]}" ;;
        graph)    run_stage graph bash "$HERE/$SOURCE/graph_$SOURCE.sh" ;;
        all)
            run_stage download bash "$wpath" "${wrapper_args[@]}"
            if _is_lit "$SOURCE"; then
                run_stage extract bash "$HERE/$SOURCE/extract_$SOURCE.sh" "${lit_extract_args[@]}"
                run_stage load bash "$HERE/$SOURCE/load_$SOURCE.sh" "${load_args[@]}"
                run_stage graph bash "$HERE/$SOURCE/graph_$SOURCE.sh"
            else
                log INFO "$SOURCE: only 'download' is wired (extract/serialize = SP4)"
            fi
            ;;
    esac
fi

if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
    log INFO "dry-run: audit bracket skipped (no run_end)"
else
    "$PY" -m episteme.audit_trail record run_end --object "$SOURCE" --run-id "$RUN_ID" > /dev/null \
        || log WARN "run_end audit failed (is the DB up?)"
fi
log INFO "pipeline done: $SOURCE $STAGE"
