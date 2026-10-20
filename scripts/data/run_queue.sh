#!/usr/bin/env bash
# Run run_pipeline.sh steps in order; stop at the first step that did not finish.
#   run_queue.sh "<source>:<stage>[,<stage>...]" ...
# Exit: 0 all steps done, 1 queue stopped, 2 usage error.
# The queue takes no lock and writes no status file: each run_pipeline.sh does.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

usage() {
    echo "usage: $0 \"<source>:<stage>[,<stage>...]\" ..." >&2
    echo "  example: $0 pubmed:extract,load,graph pmc:extract" >&2
    exit 2
}

[ "$#" -gt 0 ] || usage

# Parse every argument first; nothing runs when one is bad.
STEPS=()
for arg in "$@"; do
    case "$arg" in
        *:*) ;;
        *) echo "bad argument (no ':'): $arg" >&2; usage ;;
    esac
    src="${arg%%:*}"
    stages="${arg#*:}"
    case ",$stages," in
        *,,*) echo "bad argument (empty stage): $arg" >&2; usage ;;
    esac
    if [ -z "$src" ] || [ -z "$stages" ]; then
        echo "bad argument (empty source or stage): $arg" >&2
        usage
    fi
    IFS=',' read -r -a parts <<< "$stages"
    for st in "${parts[@]}"; do
        if [ -z "$st" ]; then
            echo "bad argument (empty stage): $arg" >&2
            usage
        fi
        STEPS+=("$src $st")
    done
done

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../.venv/Scripts/python.exe" "$HERE/../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || { echo "python not found (set PYTHON=/path/to/python)" >&2; exit 2; }

QUEUE_ID="queue-$(date -u +%Y%m%dT%H%M%SZ)"
QDIR="$("$PY" -m episteme.ops.status queue-dir --queue-id "$QUEUE_ID" | tr -d '\r')"
[ -n "$QDIR" ] || { echo "cannot resolve the queue folder" >&2; exit 2; }
mkdir -p "$QDIR" || { echo "cannot create $QDIR" >&2; exit 2; }
QLOG="$QDIR/queue.log"

qlog() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$QLOG"; }

# Code-changed guard: HEAD, plus a hash of the content of every change under
# scripts/data and src (the diff against HEAD, and the names and contents of
# the untracked files). An edit to a file that was already changed at the
# queue start counts too. Without git, or outside a git repo: warn once, run
# without the guard.
GUARD=1
code_state() {
    local head diff names untracked
    head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" || return 1
    diff="$(git -C "$REPO" diff HEAD --binary -- scripts/data src 2>/dev/null | cksum)" || return 1
    # NUL-separated names (safe for any name), one per line for hash-object.
    names="$(git -C "$REPO" ls-files -o --exclude-standard -z -- scripts/data src 2>/dev/null | tr '\0' '\n')" \
        || return 1
    untracked="$(printf '%s\n' "$names" | cksum)"
    if [ -n "$names" ]; then
        untracked="$untracked $(printf '%s\n' "$names" | git -C "$REPO" hash-object --stdin-paths 2>/dev/null | cksum)" \
            || return 1
    fi
    printf '%s %s %s' "$head" "$diff" "$untracked"
}
START_STATE=""
if ! START_STATE="$(code_state)"; then
    GUARD=0
    qlog "WARNING: no git state available; the code-changed guard is off"
fi

esc() { printf '%s' "$1" | sed 's/[][\.*^$+?(){}|]/\\&/g'; }

qlog "queue $QUEUE_ID start: ${#STEPS[@]} step(s), folder $QDIR"

n=0
for step in "${STEPS[@]}"; do
    n=$((n + 1))
    src="${step% *}"
    st="${step#* }"
    if [ "$GUARD" -eq 1 ] && [ "$(code_state)" != "$START_STATE" ]; then
        qlog "STOPPED at $src $st: code changed since the queue started"
        exit 1
    fi
    slog="$QDIR/$(printf '%02d' "$n")-$src-$st.log"
    qlog "step $n: $src $st (log $slog)"
    bash "$HERE/run_pipeline.sh" "$src" "$st" > "$slog" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ] && grep -qE "^[^ ]+ \[INFO\] pipeline done: $(esc "$src") $(esc "$st")\$" "$slog"; then
        qlog "done: $src $st"
    else
        qlog "STOPPED at $src $st (rc=$rc)"
        exit 1
    fi
done
qlog "queue $QUEUE_ID finished: all ${#STEPS[@]} step(s) done"
exit 0
