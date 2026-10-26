#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/_lib/common.sh"
. "$HERE/_lib/stage_run.sh"
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
    echo "usage: $0 <source> <all|download|extract|load|graph|materialize|enrich|serialize> [--dry-run] [--force] [--reason REASON] [--max-files N] [--bulk] [--no-wait] [--skip-unit NAME --reason REASON] [--sample] [--skip-gate --reason REASON]" >&2
    echo "       $0 corpus materialize   # alias: writes the same 03_corpus shard as 'pmc materialize'" >&2
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
BULK=0
SAMPLE=0
SKIP_GATE=0
NO_WAIT=0
REASON=""
MAX_FILES=""
SKIP_UNITS=()
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
        # extract, serialize or load stage only: skip the named unit in this run
        # (repeatable; needs --reason). Per run: no marker survives it.
        --skip-unit)
            [ $# -ge 2 ] && [ -n "$2" ] || die "--skip-unit requires a unit name" 2
            SKIP_UNITS+=("$2"); shift 2 ;;
        # download_pmc.py's flag is --limit, extract_pmc.py's is --max-files --
        # one orchestrator flag, translated per downstream script below.
        --max-files) MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        # env-only signal for the fetch engine / wrappers; never appended to
        # any *_args array. Exported so every downstream subprocess sees it.
        --dry-run) export EPISTEME_DRY_RUN=1; shift ;;
        # load or graph stage only: drop the secondary indexes of the stage's group
        # (load: articles; graph: pubmed_graph), run the stage, always rebuild them
        # (scripts/data/db/bulk_load_indexes.sh).
        --bulk) BULK=1; shift ;;
        # Do not wait for a busy stage lock: refuse with exit 75 instead. The
        # Python stages see EPISTEME_NO_WAIT and refuse a busy advisory lock too.
        --no-wait) NO_WAIT=1; export EPISTEME_NO_WAIT=1; shift ;;
        # The sample gate (operating standard spec 7). --sample: run one stage
        # on a small raw slice, write under <processed root>/_sample/<source>/
        # and to the secondary database, then measure it (a pass on success).
        --sample) SAMPLE=1; shift ;;
        # Run gated stages that have no valid pass (needs --reason): one
        # audited gate_override row per such stage.
        --skip-gate) SKIP_GATE=1; shift ;;
        *) passthrough+=("$1"); shift ;;
    esac
done

if [ "$FORCE" = "1" ] && [ -z "$REASON" ]; then
    die "--force requires --reason" 2
fi
if [ "${#SKIP_UNITS[@]}" -gt 0 ]; then
    [ -n "$REASON" ] || die "--skip-unit requires --reason" 2
    case "$STAGE" in
        extract|serialize|load) ;;
        all) die "--skip-unit needs one named stage, not 'all'" 2 ;;
        *) die "--skip-unit is valid with the extract, serialize or load stage only (not $STAGE)" 2 ;;
    esac
    # These load wrappers reach a Python module that has no --skip-unit.
    if [ "$STAGE" = "load" ]; then
        case "$SOURCE" in
            europepmc_id_mappings|cdisc_ct)
                die "$SOURCE load does not support --skip-unit" 2 ;;
        esac
    fi
fi
if [ "$BULK" = "1" ] && [ "$STAGE" != "load" ] && [ "$STAGE" != "graph" ]; then
    log ERROR "--bulk is only valid with the load or graph stage"
    exit 2
fi
if [ "$SKIP_GATE" = "1" ]; then
    [ "$SAMPLE" = "0" ] || die "--sample and --skip-gate cannot be used together" 2
    [ -n "$REASON" ] || die "--skip-gate requires --reason" 2
fi
if [ "$SAMPLE" = "1" ]; then
    case "$STAGE" in
        extract|serialize|load) ;;
        all) die "--sample needs one named stage, not 'all'" 2 ;;
        *) die "--sample is valid with the extract, serialize or load stage only (not $STAGE)" 2 ;;
    esac
    [ "$BULK" = "0" ] || die "--sample cannot be used with --bulk" 2
    [ "$FORCE" = "0" ] || die "--sample cannot be used with --force" 2
    [ "${EPISTEME_DRY_RUN:-0}" != "1" ] || die "--sample cannot be used with --dry-run" 2
    # The slice is the file selection of a sample run: these flags would change it.
    [ -z "$MAX_FILES" ] || die "--sample cannot be used with --max-files" 2
    [ "${#SKIP_UNITS[@]}" -eq 0 ] || die "--sample cannot be used with --skip-unit" 2
    # The sample writes to the secondary database (episteme_test).
    require_env PGDATABASE_SECONDARY
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
    [cdisc_bc]="cdisc_bc/download_cdisc_bc.sh"
    [cdisc_ct]="cdisc_ct/download_cdisc_ct.sh"
    [cdisc_usdm]="cdisc_usdm/download_cdisc_usdm.sh"
)

# SP2 literature sources: the six that get the full extract -> load -> graph
# chain (dispatch to $HERE/<source>/<stage>_<source>.sh). Every other source is
# download-only in SP2 (structured serialize = SP4). `_is_lit` gates the
# extract/load/graph arms below.
LIT_SOURCES="pubmed apollo europepmc_manuscript europepmc_preprint guidelines bookshelf"
_is_lit() { case " $LIT_SOURCES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

STRUCTURED_SOURCES="chembl uniprot pubchem clinvar reactome mesh ontologies openalex cdisc_ct"
_is_structured() { case " $STRUCTURED_SOURCES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

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
    all|download|extract|load|graph|materialize|enrich|serialize) ;;
    *) usage ;;
esac

# SP2 generalises SP3's C2: --dry-run is meaningful only for `download` (the
# fetch wrappers have a real EPISTEME_DRY_RUN no-op). Every other stage writes
# the DB; DR-1 would strip its audit bracket -> a real unaudited write. Refuse.
# `all` is in the list too: it runs a write stage, so the review wants the
# combination refused outright, not run download-only and silently partial.
case "$STAGE" in
    extract|load|graph|enrich|materialize|serialize|all)
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
        load)
            if [ "$SOURCE" = "europepmc_id_mappings" ]; then
                idmapw="$HERE/europepmc/id_mappings/load_europepmc_id_mappings.sh"
                [ -f "$idmapw" ] || die "wrapper not found: $idmapw (not yet implemented?)" 3
            else
                _is_lit "$SOURCE" || _is_structured "$SOURCE" \
                    || die "$SOURCE $STAGE is not in SP2/SP4 (literature/structured)" 3
                litw="$HERE/$SOURCE/load_$SOURCE.sh"
                [ -f "$litw" ] || die "wrapper not found: $litw (not yet implemented?)" 3
            fi
            ;;
        extract)
            _is_lit "$SOURCE" || die "$SOURCE $STAGE is not in SP2 — SP4 (structured serialize)" 3
            litw="$HERE/$SOURCE/extract_$SOURCE.sh"
            [ -f "$litw" ] || die "wrapper not found: $litw (not yet implemented?)" 3
            ;;
        graph)
            _is_lit "$SOURCE" || [ "$SOURCE" = "mesh" ] \
                || die "$SOURCE graph is not wired" 3
            litw="$HERE/$SOURCE/graph_$SOURCE.sh"
            [ -f "$litw" ] || die "wrapper not found: $litw (not yet implemented?)" 3
            ;;
        serialize)
            _is_structured "$SOURCE" || die "$SOURCE $STAGE is not in SP4 — SP2 (literature) territory" 3
            structw="$HERE/$SOURCE/serialize_$SOURCE.sh"
            [ -f "$structw" ] || die "wrapper not found: $structw (not yet implemented?)" 3
            ;;
        enrich)
            if [ "$SOURCE" = "europepmc_lite" ]; then
                litw="$HERE/europepmc/lite_metadata/enrich_europepmc_lite.sh"
                [ -f "$litw" ] || die "wrapper not found: $litw (not yet implemented?)" 3
            else
                die "$SOURCE enrich is not in SP2 — SP4 (structured serialize)" 3
            fi
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

# pmc: reject any STAGE outside its own known chain here, before the audit
# bracket, same early-validation principle as `corpus` above -- `serialize` is
# a valid top-level STAGE (SP4's structured-source allow-list) but pmc has no
# dispatch arm for it; without this guard it would pass validation, write a
# run_start/run_end audit pair, and no-op with rc 0 (a real dispatch bug the
# whole-branch review flagged). The pmc case block below carries a
# belt-and-braces `die` for the same case.
if [ "$SOURCE" = "pmc" ]; then
    case "$STAGE" in
        download|extract|load|graph|materialize|enrich|all) ;;
        # Message distinguished from the belt-and-braces case-arm below on
        # purpose: a dispatch test asserting only "not wired" would still
        # pass if this early guard were ever deleted (the case-arm alone
        # would still reject with rc 3), silently losing the property this
        # guard exists for -- that pmc's run_start audit call never fires
        # for an unwired stage. The distinct substring lets a test pin
        # which guard actually fired.
        *) die "pmc: $STAGE is not wired (early validation)" 3 ;;
    esac
fi

# The sample gate (operating standard spec 7). PASS_ROOT is the production
# processed root, read before any --sample redirect: the pass files are read
# and written there. A dry run is never gated.
_chain_stages() { # the stages this run executes, in order (keep in step with the dispatch below)
    if [ "$STAGE" != "all" ]; then
        echo "$STAGE"
    elif [ "$SOURCE" = "pmc" ]; then
        echo download extract load graph materialize enrich
    elif _is_structured "$SOURCE"; then
        echo download serialize load
        if [ "$SOURCE" = "mesh" ]; then echo graph; fi
    elif _is_lit "$SOURCE"; then
        echo download extract load graph
    else
        echo download
    fi
}
PASS_ROOT=""
GATE_OVERRIDES=()  # the gated stages that run without a valid pass (--skip-gate)
if [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then
    PASS_ROOT="$("$PY" -m episteme.ops.gate paths)" || die "gate: could not read the processed root" 1
    PASS_ROOT="${PASS_ROOT%$'\r'}"  # a Windows Python can write CRLF to a pipe
    PASS_ROOT="${PASS_ROOT//\\//}"
fi
if [ "${EPISTEME_DRY_RUN:-0}" != "1" ] && [ "$SAMPLE" = "0" ]; then
    # A full run: every gated stage of the run needs a valid pass. Exit 0 of
    # `gate check` is "ok" or "not gated"; any other exit code fails closed.
    gate_refused=()
    for gate_stage in $(_chain_stages); do
        gate_rc=0
        gate_out="$("$PY" -m episteme.ops.gate check --source "$SOURCE" --stage "$gate_stage" --pass-root "$PASS_ROOT")" || gate_rc=$?
        gate_out="${gate_out%$'\r'}"
        [ "$gate_rc" -ne 0 ] || continue
        [ -n "$gate_out" ] || gate_out="gate check failed, rc=$gate_rc"
        if [ "$SKIP_GATE" = "1" ]; then
            log WARN "gate: $SOURCE $gate_stage has no valid pass ($gate_out); --skip-gate overrides it: $REASON"
            GATE_OVERRIDES+=("$gate_stage")
        else
            gate_refused+=("$gate_stage ($gate_out)")
        fi
    done
    if [ "${#gate_refused[@]}" -gt 0 ]; then
        for gate_stage in "${gate_refused[@]}"; do
            log ERROR "gate: no valid pass for $SOURCE $gate_stage"
        done
        die "gate: refused; run '$0 $SOURCE <stage> --sample' until it passes, or use --skip-gate --reason REASON" 2
    fi
fi

# --sample: the stage runs on a raw slice under SAMPLE_ROOT. Only the raw,
# processed and corpus roots and the database target are redirected; the data
# root and the lock folder stay, so a sample run and a production heavy job
# never run at the same time. The run_start/run_end rows go to the secondary
# database too.
SAMPLE_ROOT=""
SAMPLE_RECORD=""
NATIVE_BASH="$BASH"
if [ "$SAMPLE" = "1" ]; then
    [ -n "$PASS_ROOT" ] || die "--sample: could not read the processed root" 1
    gate_rc=0
    gate_out="$("$PY" -m episteme.ops.gate check --source "$SOURCE" --stage "$STAGE" --pass-root "$PASS_ROOT")" || gate_rc=$?
    gate_out="${gate_out%$'\r'}"
    # Ruling R10: a known answer goes on; "not gated" and rc 2 (invalid rules)
    # are refusals (2); any other answer is a crash, an operational failure (1).
    case "$gate_rc:$gate_out" in
        "0:not gated") die "--sample: $SOURCE $STAGE has no gate rules (src/episteme/ops/gate_rules/$SOURCE.json)" 2 ;;
        "0:ok"|"1:no pass"|"1:code changed"|"1:rules changed") ;;
        2:*) die "--sample: gate check refused $SOURCE $STAGE (rc=2): $gate_out" 2 ;;
        *) die "--sample: gate check failed for $SOURCE $STAGE (rc=$gate_rc): $gate_out" 1 ;;
    esac
    SAMPLE_ROOT="$PASS_ROOT/_sample/$SOURCE"
    SAMPLE_RECORD="$SAMPLE_ROOT/run_record_$STAGE.json"
    # The production raw root (common.sh resolve_dest reads it the same way).
    PROD_RAW_ROOT="${EPISTEME_RAW_ROOT:-${EPISTEME_DATA_ROOT:-.}/01_raw}"
    if [ "$STAGE" = "load" ]; then
        sample_staging="$SAMPLE_ROOT/02_processed/staging/$SOURCE"
        [ -n "$(ls -A "$sample_staging" 2>/dev/null)" ] \
            || die "--sample load: no sample staging in $sample_staging; run the sample extract first" 2
    fi
    export EPISTEME_RAW_ROOT="$SAMPLE_ROOT/01_raw"
    export EPISTEME_PROCESSED_ROOT="$SAMPLE_ROOT/02_processed"
    export EPISTEME_CORPUS_ROOT="$SAMPLE_ROOT/03_corpus"
    export EPISTEME_DB_TARGET=secondary
    # The DB-mode guard then refuses the production database even when
    # PGDATABASE_SECONDARY names it by mistake (episteme_test stays allowed).
    export EPISTEME_DB_MODE=restricted
    if [ "$STAGE" = "load" ]; then
        # The secondary database must have every migration before a sample load.
        mig_rc=0
        mig_out="$("$PY" -m episteme.ops.gate migrations-check)" || mig_rc=$?
        mig_out="${mig_out//$'\r'/}"
        # Ruling R10: exit 1 with names is a refusal (2); any other failure
        # (an unreachable database, an error) is operational (1).
        if [ "$mig_rc" -eq 1 ] && [ -n "$mig_out" ]; then
            printf '%s\n' "$mig_out" >&2
            die "--sample load: the secondary database lacks the migrations above; apply them first with the per-file procedure of docs/10-data-sources-runbook.md section 2.3 (not migrate_database.sh: it stops at 0001)" 2
        elif [ "$mig_rc" -ne 0 ]; then
            die "--sample load: migrations-check failed (rc=$mig_rc)" 1
        fi
    fi
    # `gate run` starts the stage from Python: on Windows it needs the path of
    # this bash, or the system search finds the WSL bash in System32 first.
    if command -v cygpath >/dev/null 2>&1; then
        NATIVE_BASH="$(cygpath -m "$BASH")"
    fi
fi

RUN_ID="${SOURCE}-$(date -u +%Y%m%dT%H%M%SZ)"
# Exported so every stage's `python -m episteme.data.*` subprocess (via
# config.get_settings().run_id) shares this one run_id instead of each
# generating its own -- otherwise the audit trail fragments one pipeline
# invocation across N unrelated run_ids.
export EPISTEME_RUN_ID="$RUN_ID"

# Run status: <processed root>/_ops/runs/<run id>/status.json. Every stage
# sees the folder as EPISTEME_RUN_DIR.
RUN_DIR="$("$PY" -m episteme.ops.status run-dir --run-id "$RUN_ID" 2>/dev/null)" || RUN_DIR=""
RUN_DIR="${RUN_DIR%$'\r'}"  # a Windows Python can write CRLF to a pipe
[ -n "$RUN_DIR" ] || log WARN "status: could not resolve the run folder; EPISTEME_RUN_DIR is empty (no failed_files.txt in a run folder for this run)"
export EPISTEME_RUN_DIR="$RUN_DIR"
# The stop-request file: on INT/TERM the runner writes it, and the stage's
# parent watchdog stops the stage (bash cannot signal a native Windows Python).
# Without a run folder it goes to the temp folder, so a stop still reaches the
# stage. Python reads the path: on Git Bash give it the Windows form.
if [ -n "$RUN_DIR" ]; then
    EPISTEME_STOP_FILE="$RUN_DIR/stop.request"
else
    EPISTEME_STOP_FILE="${TMPDIR:-/tmp}/episteme-$RUN_ID.stop.request"
    if command -v cygpath >/dev/null 2>&1; then
        EPISTEME_STOP_FILE="$(cygpath -m "$EPISTEME_STOP_FILE")"
    fi
fi
export EPISTEME_STOP_FILE

# The stage lock (operating standard spec 6.1): download -> the download lock;
# every stage that writes the DB or 02_processed -> the heavy lock. A single
# stage holds its lock from before run_start to the exit; `all` takes and
# releases it per stage. The Python stage mains re-enter it (same run id, owner
# $EPISTEME_RUNNER_PID). A dry run takes no lock and writes no status.
_lock_kind() {
    case "$1" in
        download) echo download ;;
        extract|serialize|load|graph|enrich|materialize) echo heavy ;;
    esac
}
LOCK_KIND=""     # the lock this run holds now (acquired or re-entered)
LOCK_OWNED=0     # 1 when this run acquired it, and so must release it
LOCK_REFUSAL=""  # set by a busy --no-wait: "<kind> lock held by <run> (<stage>)"
WAIT_KIND=""     # set while this run waits for a lock (the EXIT handler reads it)
WAIT_STAGE=""
_lock_take() { # KIND STAGE -> 0; 75 when busy under --no-wait; another code on error
    local kind="$1" stage="$2" out="" err="" rc=0 errf
    local args=(-m episteme.ops.lock acquire "$kind" --run-id "$RUN_ID" --stage "$stage" --owner-pid "$EPISTEME_RUNNER_PID")
    # The status shows the wait, not the end state of the previous stage.
    WAIT_KIND="$kind"
    WAIT_STAGE="$stage"
    _status_set "$stage" running --unit "waiting for the $kind lock" --runner-pid "$EPISTEME_RUNNER_PID"
    if [ "$NO_WAIT" = "1" ]; then
        errf="$(mktemp)"
        out="$("$PY" "${args[@]}" --no-wait 2>"$errf")" || rc=$?
        err="$(cat "$errf" 2>/dev/null)"
        rm -f "$errf"
        [ -z "$err" ] || printf '%s\n' "$err" >&2
    else
        # Waits while another run holds the lock; its log lines reach stderr.
        out="$("$PY" "${args[@]}")" || rc=$?
    fi
    out="${out%$'\r'}"  # a Windows Python can write CRLF to a pipe
    if [ "$rc" -eq 75 ]; then
        WAIT_KIND=""
        WAIT_STAGE=""
        LOCK_REFUSAL="$kind lock busy"
        if [[ "$err" =~ held\ by\ run\ ([^ ]+)\ \(([^,]+), ]]; then
            LOCK_REFUSAL="$kind lock held by ${BASH_REMATCH[1]} (${BASH_REMATCH[2]})"
        fi
        return 75
    fi
    if [ "$rc" -ne 0 ]; then
        WAIT_KIND=""
        WAIT_STAGE=""
        log ERROR "lock: could not take the $kind lock (rc=$rc)"
        return "$rc"
    fi
    LOCK_KIND="$kind"
    LOCK_OWNED=0
    [ "$out" = "acquired" ] && LOCK_OWNED=1
    # Cleared only now: a signal that lands between the acquire and this line
    # still lets the EXIT handler release the lock.
    WAIT_KIND=""
    WAIT_STAGE=""
    return 0
}
_lock_drop() {
    [ -n "$LOCK_KIND" ] || return 0
    if [ "$LOCK_OWNED" = "1" ]; then
        "$PY" -m episteme.ops.lock release "$LOCK_KIND" --run-id "$RUN_ID" --owner-pid "$EPISTEME_RUNNER_PID" >/dev/null 2>&1 \
            || log WARN "lock: could not release the $LOCK_KIND lock (the next run takes over a stale lock)"
    fi
    LOCK_KIND=""
    LOCK_OWNED=0
}
_status_set() { # STAGE STATE [--unit U] [--reason R] [--runner-pid P]
    [ "${EPISTEME_DRY_RUN:-0}" = "1" ] && return 0
    local stage="$1" state="$2"
    shift 2
    "$PY" -m episteme.ops.status set --run-id "$RUN_ID" --source "$SOURCE" --stage "$stage" --state "$state" "$@" >/dev/null 2>&1 \
        || log WARN "status: could not write state $state for stage $stage"
}
STATUS_OPEN=""  # the stage that is "running", with no end state written yet
LAST_STAGE=""   # the stage run_stage began last (`all`: a signal between stages)
RUN_OPEN=0      # 1 from a successful run_start until a run_end is written
BULK_DROPPING=0 # 1 while the --bulk index drop runs
BULK_NOTE=""    # " (bulk index drop)" or " (bulk index rebuild)" while that step runs

# The one EXIT handler (a second `trap ... EXIT` would replace it). Order: the
# --bulk index rebuild (it needs the lock), the end state and run_end of a run
# that a signal or an error stopped, then the lock release. A stage failure
# keeps its exit code; else a failed rebuild is the exit code.
BULK_PENDING=0
_on_exit() {
    local rc=$?
    trap - EXIT
    local rrc=0
    # A signal during the drop: the drop may have finished. Rebuild what is
    # pending (the rebuild covers every pending row).
    if [ "$BULK_DROPPING" = "1" ]; then
        BULK_DROPPING=0
        BULK_PENDING=1
    fi
    if [ "$BULK_PENDING" = "1" ]; then
        _bulk_rebuild
        rrc=$?
        if [ "$rrc" -ne 0 ]; then
            log ERROR "bulk: index rebuild failed (rc=$rrc); the secondary indexes are still dropped — finish it with: bash scripts/data/db/bulk_load_indexes.sh rebuild"
        fi
    fi
    # A signal during a lock wait: the acquire may have written the lock before
    # _lock_take recorded it. Release it (release removes only this run's lock).
    if [ -n "$WAIT_KIND" ]; then
        LOCK_KIND="$WAIT_KIND"
        LOCK_OWNED=1
    fi
    # Every path that ends on its own writes its end state (and run_end) and
    # clears STATUS_OPEN, WAIT_STAGE and RUN_OPEN first. What is still set here
    # belongs to a run that a signal (or an unplanned error) ended: close it once.
    if [ "$rc" -ne 0 ]; then
        local where="" at=""
        if [ -n "$STATUS_OPEN" ]; then
            at="$STATUS_OPEN"
            where="at stage $at"
        elif [ -n "$WAIT_STAGE" ]; then
            at="$WAIT_STAGE"
            where="while waiting for the $WAIT_KIND lock (stage $at)"
        elif [ "$RUN_OPEN" = "1" ]; then
            at="${LAST_STAGE:-$STAGE}"
            where="at stage $at$BULK_NOTE"
        fi
        if [ -n "$where" ]; then
            local why state
            case "$rc" in
                130|143) why="stopped $where"; state=stopped ;;
                *) why="failed $where"; state=failed ;;
            esac
            log ERROR "$why"
            _status_set "$at" "$state" --reason "$why"
            STATUS_OPEN=""
            WAIT_STAGE=""
            [ "$RUN_OPEN" = "1" ] && _run_end "$why"
        fi
    fi
    _lock_drop
    [ "$rc" -ne 0 ] && exit "$rc"
    exit "$rrc"
}
trap _on_exit EXIT
# A signal ends the runner through the EXIT handler (exit 130 or 143). During a
# stage (stage_exec) the trap first writes $EPISTEME_STOP_FILE, which stops the
# stage, and waits for the stage to end: the end state, run_end and the lock
# release come after it. Outside a stage the trap exits after the foreground
# command (such as a lock acquire) ends. See _lib/stage_run.sh.
stage_install_stop_traps

_run_end() { # REASON (empty: a clean end). DR-1: a dry run writes no run_end.
    [ "${EPISTEME_DRY_RUN:-0}" = "1" ] && return 0
    local reason=()
    [ -z "$1" ] || reason=(--reason "$1")
    RUN_OPEN=0
    "$PY" -m episteme.audit_trail record run_end --object "$SOURCE" --run-id "$RUN_ID" "${reason[@]+"${reason[@]}"}" >/dev/null 2>&1 || true
}

run_stage() {
    local name="$1"
    shift
    log INFO "stage: $name"
    LAST_STAGE="$name"
    local kind
    kind="$(_lock_kind "$name")"
    if [ "${EPISTEME_DRY_RUN:-0}" != "1" ] && [ -n "$kind" ] && [ "$LOCK_KIND" != "$kind" ]; then
        _lock_drop
        local lrc=0
        _lock_take "$kind" "$name" || lrc=$?
        if [ "$lrc" -ne 0 ]; then
            local why="failed at stage $name (lock, rc=$lrc)" state=failed
            if [ "$lrc" -eq 75 ]; then
                why="refused: $LOCK_REFUSAL"
                state=refused
            fi
            log ERROR "$why"
            _run_end "$why"
            _status_set "$name" "$state" --reason "$why"
            [ "$lrc" -eq 75 ] && exit 75
            exit 1
        fi
    fi
    # Drop any stale failure record so a later read only sees this stage's.
    "$PY" -m episteme.ops.failure clear --source "$SOURCE" >/dev/null 2>&1 || true
    _status_set "$name" running --runner-pid "$EPISTEME_RUNNER_PID"
    STATUS_OPEN="$name"
    # `if ! cmd` would lose the exit code; capture it so it can be reused.
    local rc=0
    if [ "$SAMPLE" = "1" ]; then
        # The sample measures the stage: time, exit code and peak RSS. It also
        # hashes the stage code and the rules first: the pass carries these
        # hashes, so a code change during the run cannot be certified.
        [ "$1" != "bash" ] || shift
        stage_exec "$PY" -m episteme.ops.gate run --record "$SAMPLE_RECORD" --source "$SOURCE" --stage "$name" \
            -- "$NATIVE_BASH" "$@" || rc=$?
    else
        # In the background (stage_run.sh): a stop reaches the stage at once.
        stage_exec "$@" || rc=$?
    fi
    if [ "$rc" -ne 0 ]; then
        local reason state unit_args=()
        case "$rc" in
            130|143) reason="stopped at stage $name"; state=stopped ;;
            75) reason="refused at stage $name"; state=refused ;;
            *)
                reason="failed at stage $name"
                state=failed
                local detail unit
                detail="$("$PY" -m episteme.ops.failure show --source "$SOURCE" --run-id "$RUN_ID" 2>/dev/null)" || detail=""
                detail="${detail//$'\r'/}"
                [ -z "$detail" ] || reason="$reason: $detail"
                # The unit alone (a name may hold ": "); an empty unit passes no --unit.
                unit="$("$PY" -m episteme.ops.failure show --source "$SOURCE" --run-id "$RUN_ID" --field unit 2>/dev/null)" || unit=""
                unit="${unit%$'\r'}"
                [ -z "$unit" ] || unit_args=(--unit "$unit")
                ;;
        esac
        log ERROR "stage failed: $name"
        log ERROR "resume with: $0 $SOURCE $name"
        _status_set "$name" "$state" "${unit_args[@]+"${unit_args[@]}"}" --reason "$reason"
        STATUS_OPEN=""
        _run_end "$reason"
        # A stop and a lock refusal pass through; every other stage failure is
        # exit 1 (exit 2 stays for the dispatcher's own rejections before run_start).
        case "$rc" in
            130|143|75) exit "$rc" ;;
            *) exit 1 ;;
        esac
    fi
    _status_set "$name" "done"
    STATUS_OPEN=""
    # `all`: release the lock after each stage (the next stage takes its own).
    [ "$STAGE" != "all" ] || _lock_drop
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

# --skip-unit NAME ... --reason R: forwarded to the one named stage only
# (extract, serialize or load; the validation above rejects every other use).
skip_args=()
for _u in "${SKIP_UNITS[@]+"${SKIP_UNITS[@]}"}"; do skip_args+=(--skip-unit "$_u"); done
# --reason rides along once: load_args already holds it when --force is set.
skip_reason=()
[ "${#skip_args[@]}" -eq 0 ] || skip_reason=(--reason "$REASON")
extract_args+=("${skip_args[@]+"${skip_args[@]}"}" "${skip_reason[@]+"${skip_reason[@]}"}")
load_args+=("${skip_args[@]+"${skip_args[@]}"}")
[ "$FORCE" = "1" ] || load_args+=("${skip_reason[@]+"${skip_reason[@]}"}")

# SP2 lit `extract` wrappers (bookshelf/extract_bookshelf.sh, ...) accept only
# --max-files N and --force (plus --skip-unit NAME --reason R when skipping);
# NOT the SP3 wrapper_args superset.
# `load` reuses load_args above; `graph` gets --force --reason R only when forced
# (graph_builder.main takes --source/--raw-dir/--processed-dir/--force/--reason).
graph_args=()
[ "$FORCE" = "1" ] && graph_args=(--force --reason "$REASON")
lit_extract_args=()
[ -n "$MAX_FILES" ] && lit_extract_args+=(--max-files "$MAX_FILES")
[ "$FORCE" = "1" ] && lit_extract_args+=(--force)
lit_extract_args+=("${skip_args[@]+"${skip_args[@]}"}" "${skip_reason[@]+"${skip_reason[@]}"}")

# SP4 structured `serialize` wrappers accept --max-files N and --force (plus
# --skip-unit NAME --reason R when skipping), mirroring lit_extract_args' shape. `load` reuses load_args;
# `graph` (mesh only) gets no extra args, same as the lit graph arm.
struct_serialize_args=()
[ -n "$MAX_FILES" ] && struct_serialize_args+=(--max-files "$MAX_FILES")
[ "$FORCE" = "1" ] && struct_serialize_args+=(--force)
struct_serialize_args+=("${skip_args[@]+"${skip_args[@]}"}" "${skip_reason[@]+"${skip_reason[@]}"}")

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
idmapw="${idmapw:-}"
litw="${litw:-}"
structw="${structw:-}"

# Best-effort: a down/unreachable DB must not silently skip the run_start
# bracket. extract's own audit already degrades to a file-only mirror when
# the DB is unreachable (see extract_pmc.py), so the pipeline itself stays
# usable without one -- but say so loudly rather than swallowing the failure.
# DR-1: a --dry-run must be side-effect-free and DB-independent, so the whole
# audit bracket (run_start here, run_end below) is skipped on a dry run.
# A single stage takes its lock before run_start: a refusal writes no audit row.
if [ "$STAGE" != "all" ] && [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then
    first_kind="$(_lock_kind "$STAGE")"
    if [ -n "$first_kind" ]; then
        first_rc=0
        _lock_take "$first_kind" "$STAGE" || first_rc=$?
        if [ "$first_rc" -eq 75 ]; then
            log ERROR "refused: $LOCK_REFUSAL"
            _status_set "$STAGE" refused --reason "refused: $LOCK_REFUSAL"
            exit 75
        fi
        if [ "$first_rc" -ne 0 ]; then
            _status_set "$STAGE" failed --reason "failed at stage $STAGE (lock, rc=$first_rc)"
            die "could not take the $first_kind lock for stage $STAGE (rc=$first_rc)" 1
        fi
        # The lock is held: the status no longer shows the wait.
        _status_set "$STAGE" running --runner-pid "$EPISTEME_RUNNER_PID"
    fi
fi

# --sample, under the lock: an extract or serialize starts from the new slice
# and an empty sample output (old staging or markers would skip the slice); a
# load re-loads every sample shard.
if [ "$SAMPLE" = "1" ]; then
    # Never delete outside <production processed root>/_sample/<source>.
    case "$SAMPLE_ROOT" in
        "$PASS_ROOT"/_sample/?*) ;;
        *) die "sample: the sample root $SAMPLE_ROOT is not inside $PASS_ROOT/_sample" 1 ;;
    esac
    if [ "$STAGE" = "load" ]; then
        rm -rf "$SAMPLE_ROOT/02_processed/_ops/$SOURCE/load_success"
    else
        log INFO "sample: building the raw slice in $SAMPLE_ROOT"
        prep_rc=0
        # A signal during the copy: the EXIT handler writes the end state.
        STATUS_OPEN="$STAGE"
        "$PY" -m episteme.ops.gate prepare --source "$SOURCE" --raw-root "$PROD_RAW_ROOT" \
            --sample-root "$SAMPLE_ROOT" --processed-root "$PASS_ROOT" >&2 || prep_rc=$?
        if [ "$prep_rc" -ne 0 ]; then
            # Ruling R10: prepare exits 2 for a slice refusal (a missing group
            # or file, the cap) and 1 for an I/O error or any other failure.
            prep_exit=1
            [ "$prep_rc" -ne 2 ] || prep_exit=2
            _status_set "$STAGE" failed --reason "sample: could not build the raw slice (rc=$prep_rc)"
            STATUS_OPEN=""
            die "sample: could not build the raw slice for $SOURCE (rc=$prep_rc)" "$prep_exit"
        fi
        STATUS_OPEN=""
        rm -rf "$SAMPLE_ROOT/02_processed/staging" "$SAMPLE_ROOT/02_processed/_ops/$SOURCE" \
            "$SAMPLE_ROOT/03_corpus"
    fi
fi

# --skip-gate: one gate_override row per gated stage without a valid pass,
# before run_start. A full run is never redirected, so the rows go to
# production. No row, no run: an override is never unaudited.
for gate_stage in "${GATE_OVERRIDES[@]+"${GATE_OVERRIDES[@]}"}"; do
    if ! "$PY" -m episteme.audit_trail record gate_override --object "$SOURCE $gate_stage" \
        --run-id "$RUN_ID" --reason "$REASON" >/dev/null; then
        _status_set "$STAGE" failed --reason "gate: could not write the gate_override audit row for $SOURCE $gate_stage"
        die "gate: could not write the gate_override audit row for $SOURCE $gate_stage; the run does not go on" 1
    fi
done

if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
    log INFO "dry-run: skipping run_start/run_end audit bracket"
else
    # RUN_OPEN: from here the EXIT handler closes the bracket on a signal.
    if "$PY" -m episteme.audit_trail record run_start --object "$SOURCE" --run-id "$RUN_ID" > /dev/null; then
        RUN_OPEN=1
    else
        log WARN "run_start audit failed; proceeding unaudited (DB unreachable, or refused by the DB-mode guard — see EPISTEME_DB_MODE)"
    fi
fi

# M4: tool report for EVERY source (was non-pmc only), once, before dispatch.
bash "$HERE/_lib/check_prereqs.sh" >&2 || true

# --bulk: drop the secondary-index group of the stage (load: articles; graph:
# pubmed_graph) before it and always rebuild them -- explicitly after a
# successful stage (so the rebuild's audit row lands before run_end), and from
# the EXIT handler (_on_exit) after a failed or interrupted one. A hard kill skips the handler; the saved
# state then blocks the next --bulk, and `bulk_load_indexes.sh rebuild` finishes the job.
BULK_GROUP=articles
[ "$STAGE" = "graph" ] && BULK_GROUP=pubmed_graph
_bulk_rebuild() {
    [ "$BULK_PENDING" = "1" ] || return 0
    BULK_PENDING=0
    log INFO "bulk: rebuilding the secondary indexes (group $BULK_GROUP)"
    # The rebuild always waits, also under --no-wait: a refused rebuild would
    # leave the secondary indexes dropped.
    EPISTEME_NO_WAIT='' bash "$HERE/db/bulk_load_indexes.sh" rebuild
}
if [ "$BULK" = "1" ]; then
    log INFO "bulk: dropping the secondary indexes (group $BULK_GROUP)"
    BULK_DROPPING=1
    BULK_NOTE=" (bulk index drop)"
    bash "$HERE/db/bulk_load_indexes.sh" drop --group "$BULK_GROUP"
    drop_rc=$?
    BULK_DROPPING=0
    BULK_NOTE=""
    if [ "$drop_rc" -ne 0 ]; then
        # 75: --no-wait and the advisory lock is busy; nothing was dropped.
        drop_why="failed at stage $STAGE (bulk index drop)"
        drop_state=failed
        if [ "$drop_rc" -eq 75 ]; then
            drop_why="refused at stage $STAGE (bulk index drop: the advisory lock is busy)"
            drop_state=refused
        fi
        log ERROR "bulk: index drop failed (rc=$drop_rc); the $STAGE was not started (check: bash scripts/data/db/bulk_load_indexes.sh status)"
        _run_end "$drop_why"
        _status_set "$STAGE" "$drop_state" --reason "$drop_why"
        exit "$drop_rc"
    fi
    BULK_PENDING=1
fi

if [ "$SOURCE" = "pmc" ]; then
    # pmc keeps its full stage chain.
    case "$STAGE" in
        download)    run_stage download "$HERE/pmc/download_pmc.sh" "${download_args[@]}" ;;
        extract)     run_stage extract "$HERE/pmc/extract_pmc.sh" "${extract_args[@]}" ;;
        load)        run_stage load "$HERE/pmc/load_pmc.sh" "${load_args[@]}" ;;
        graph)       run_stage graph "$HERE/pmc/graph_pmc.sh" "${graph_args[@]}" ;;
        materialize) run_stage materialize "$HERE/materialize_corpus.sh" ;;
        enrich)      run_stage enrich "$HERE/pmc/enrich_pmc.sh" ;;
        all)
            run_stage download "$HERE/pmc/download_pmc.sh" "${download_args[@]}"
            run_stage extract "$HERE/pmc/extract_pmc.sh" "${extract_args[@]}"
            run_stage load "$HERE/pmc/load_pmc.sh" "${load_args[@]}"
            run_stage graph "$HERE/pmc/graph_pmc.sh" "${graph_args[@]}"
            run_stage materialize "$HERE/materialize_corpus.sh"
            run_stage enrich "$HERE/pmc/enrich_pmc.sh"
            ;;
        *) die "pmc: $STAGE is not wired" 3 ;;
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
        load)
            if [ "$SOURCE" = "europepmc_id_mappings" ]; then
                run_stage load bash "$HERE/europepmc/id_mappings/load_europepmc_id_mappings.sh" "${load_args[@]}"
            else
                run_stage load bash "$HERE/$SOURCE/load_$SOURCE.sh" "${load_args[@]}"
            fi
            ;;
        graph)    run_stage graph bash "$HERE/$SOURCE/graph_$SOURCE.sh" "${graph_args[@]}" ;;
        serialize) run_stage serialize bash "$HERE/$SOURCE/serialize_$SOURCE.sh" "${struct_serialize_args[@]}" ;;
        enrich)
            if [ "$SOURCE" = "europepmc_lite" ]; then
                run_stage enrich bash "$HERE/europepmc/lite_metadata/enrich_europepmc_lite.sh"
            else
                die "$SOURCE enrich is not wired" 3
            fi
            ;;
        all)
            run_stage download bash "$wpath" "${wrapper_args[@]}"
            if _is_structured "$SOURCE"; then
                run_stage serialize bash "$HERE/$SOURCE/serialize_$SOURCE.sh" "${struct_serialize_args[@]}"
                run_stage load bash "$HERE/$SOURCE/load_$SOURCE.sh" "${load_args[@]}"
                if [ "$SOURCE" = "mesh" ]; then
                    run_stage graph bash "$HERE/$SOURCE/graph_$SOURCE.sh" "${graph_args[@]}"
                fi
            elif _is_lit "$SOURCE"; then
                run_stage extract bash "$HERE/$SOURCE/extract_$SOURCE.sh" "${lit_extract_args[@]}"
                run_stage load bash "$HERE/$SOURCE/load_$SOURCE.sh" "${load_args[@]}"
                run_stage graph bash "$HERE/$SOURCE/graph_$SOURCE.sh" "${graph_args[@]}"
            else
                log INFO "$SOURCE: only 'download' is wired (extract/serialize = SP2/SP4)"
            fi
            ;;
    esac
fi

if [ "$BULK" = "1" ]; then
    BULK_NOTE=" (bulk index rebuild)"
    _bulk_rebuild
    rebuild_rc=$?
    BULK_NOTE=""
    if [ "$rebuild_rc" -ne 0 ]; then
        log ERROR "bulk: index rebuild failed (rc=$rebuild_rc); finish it with: bash scripts/data/db/bulk_load_indexes.sh rebuild"
        _run_end "failed at stage $STAGE (bulk index rebuild)"
        _status_set "$STAGE" failed --reason "failed at stage $STAGE (bulk index rebuild)"
        exit "$rebuild_rc"
    fi
fi

if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
    log INFO "dry-run: audit bracket skipped (no run_end)"
else
    RUN_OPEN=0
    "$PY" -m episteme.audit_trail record run_end --object "$SOURCE" --run-id "$RUN_ID" > /dev/null \
        || log WARN "run_end audit failed (DB unreachable, or refused by the DB-mode guard — see EPISTEME_DB_MODE)"
fi
# --sample: measure the output and evaluate the rules; a pass is written under
# the production processed root. The failed rules are on stdout.
if [ "$SAMPLE" = "1" ]; then
    report_rc=0
    "$PY" -m episteme.ops.gate report --source "$SOURCE" --stage "$STAGE" --sample-root "$SAMPLE_ROOT" \
        --pass-root "$PASS_ROOT" --record "$SAMPLE_RECORD" || report_rc=$?
    if [ "$report_rc" -ne 0 ]; then
        _status_set "$STAGE" failed --reason "sample gate: no pass for $SOURCE $STAGE (rc=$report_rc)"
        die "sample gate: no pass for $SOURCE $STAGE (rc=$report_rc); see the failed rules above" 1
    fi
    log INFO "sample gate: pass for $SOURCE $STAGE"
fi
log INFO "pipeline done: $SOURCE $STAGE"
