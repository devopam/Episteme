# shellcheck shell=bash
# Run one pipeline stage so that a stop reaches it at once.
#
# Source it after common.sh (it uses `log`):
#   . "$HERE/_lib/stage_run.sh"
#   stage_install_stop_traps
#   stage_exec CMD [ARG...] || rc=$?
#
# Why (2026-10-26): bash defers a trapped signal while a foreground child
# runs, so `kill -TERM` to the runner did nothing until the stage ended; and
# on Windows bash cannot signal a native Python process at all.
#
# * stage_exec starts the stage in the background and waits for it; bash's
#   `wait` returns at once on a trapped signal.
# * On INT or TERM the trap stops the stage, waits for it to end, and exits
#   130 (INT) or 143 (TERM), so the caller's EXIT handler runs only after the
#   stage has ended. Two ways reach the stage:
#   - Not on MSYS/Cygwin: one SIGTERM to the stage and every process under it.
#     A background job of a non-interactive bash starts with SIGINT ignored,
#     so SIGINT would not stop it; SIGTERM stops a Python stage (stage_guard
#     maps it to exit 143), a shell wrapper and its tools alike.
#   - Everywhere: the signal name in $EPISTEME_STOP_FILE. The parent watchdog
#     of a Python stage under stage_guard (episteme.ops.stop) polls the file
#     every second and stops the stage as a SIGTERM would. On MSYS/Cygwin this
#     is the only way: there `kill` on a native Windows process is a hard
#     TerminateProcess, so the trap never signals the stage. A stage that is
#     not under stage_guard then runs to its end (a console Ctrl-C still
#     reaches a native child directly).
#
# EPISTEME_STOP_FILE is the caller's: export it before the first stage. Empty:
# no stop file.

STAGE_PID=""       # the stage that stage_exec waits for; empty between stages
STAGE_STARTING=0   # 1 from just before the fork until STAGE_PID is set
STAGE_FORWARDED=0  # 1 once the trap has signalled the stage (only once)
STAGE_TREE=""      # the PIDs the first stop signalled; a repeated stop waits for them too
# Forward the stop as a SIGTERM to the stage tree? Yes on Linux and macOS (a
# background job starts with SIGINT ignored, so only a signal or the stop file
# reaches it). No on MSYS/Cygwin: there `kill` on a native Windows process is a
# hard TerminateProcess, so the stop file is the only path. _EPISTEME_STOP_FORWARD
# is a test seam only (0 or 1).
case "${OSTYPE:-}" in
    msys*|cygwin*) _STAGE_FORWARD="${_EPISTEME_STOP_FORWARD:-0}" ;;
    *) _STAGE_FORWARD="${_EPISTEME_STOP_FORWARD:-1}" ;;
esac

# stage_exec CMD [ARG...]: run CMD in the background, wait, return its exit code.
# stdin, stdout and stderr stay the caller's.
stage_exec() {
    # A stop request is for the stage that runs now, never for the next one.
    [ -z "${EPISTEME_STOP_FILE:-}" ] || rm -f "$EPISTEME_STOP_FILE"
    STAGE_FORWARDED=0
    STAGE_TREE=""
    STAGE_STARTING=1
    "$@" <&0 &
    STAGE_PID=$!
    STAGE_STARTING=0
    local rc=0
    wait "$STAGE_PID" || rc=$?
    STAGE_PID=""
    return "$rc"
}

# "PID PPID" of every process: ps where it takes -o (Linux, macOS), else /proc
# (Cygwin/MSYS, where the PIDs are the ones bash uses).
_stage_pairs() {
    ps -A -o pid= -o ppid= 2>/dev/null && return 0
    local f stat
    for f in /proc/[0-9]*/stat; do
        stat="$(cat "$f" 2>/dev/null)" || continue
        # After the command name, which can hold spaces: "STATE PPID ...".
        # shellcheck disable=SC2086 # split into fields on purpose
        set -- ${stat##*) }
        [ -n "${2:-}" ] && printf '%s %s\n' "${f//[!0-9]/}" "$2"
    done
}

# _stage_zombie PID: true when PID is a zombie (ended, not yet reaped).
_stage_zombie() {
    local stat state
    if stat="$(cat "/proc/$1/stat" 2>/dev/null)"; then
        state="${stat##*) }"
        state="${state%% *}"
    else
        state="$(ps -o stat= -p "$1" 2>/dev/null)"
        state="${state#"${state%%[![:space:]]*}"}"
    fi
    case "$state" in Z*) return 0 ;; *) return 1 ;; esac
}

# _stage_tree PID: PID and all its descendants, space-separated.
_stage_tree() {
    _stage_pairs | awk -v root="$1" '
        { kids[$2] = kids[$2] " " $1 }
        END {
            n = 1; q[1] = root; out = root
            for (i = 1; i <= n; i++) {
                m = split(kids[q[i]], k, " ")
                for (j = 1; j <= m; j++) { q[++n] = k[j]; out = out " " k[j] }
            }
            print out
        }'
}

# _stage_stop SIGNAL CODE: the INT/TERM trap.
_stage_stop() {
    # A signal between the fork and `STAGE_PID=$!`: the job table has the
    # stage already ($! could still name the previous stage).
    if [ -z "$STAGE_PID" ] && [ "$STAGE_STARTING" = "1" ]; then
        STAGE_PID="$(jobs -p | tail -n 1)"
    fi
    if [ -n "$STAGE_PID" ]; then
        # A repeated stop must wait for the same processes as the first one,
        # even after the wrapper has exited (else the lock goes early).
        local tree="$STAGE_TREE" p
        local -a pids=()
        if [ "$_STAGE_FORWARD" = "1" ] && [ "$STAGE_FORWARDED" = "0" ]; then
            # Find the tree BEFORE the flag is set: a nested stop during the
            # lookup then does the whole forward itself (it ends in `exit`),
            # instead of seeing "forwarded" with no tree stored yet.
            tree="$(_stage_tree "$STAGE_PID")"
            # Only plain decimal PIDs reach kill; the stage itself at least.
            for p in $tree; do
                case "$p" in '' | *[!0-9]*) ;; *) pids+=("$p") ;; esac
            done
            [ "${#pids[@]}" -gt 0 ] || pids=("$STAGE_PID")
            tree="${pids[*]}"
            STAGE_TREE="$tree"
            # Once: a second SIGTERM could break the stage's own cleanup.
            STAGE_FORWARDED=1
            kill -TERM "${pids[@]}" 2>/dev/null
            log WARN "stop ($1): sent SIGTERM to the stage (PIDs $tree)"
        fi
        if [ -n "${EPISTEME_STOP_FILE:-}" ]; then
            # A dry run writes no status, so the run folder may not exist yet.
            mkdir -p "$(dirname "$EPISTEME_STOP_FILE")" 2>/dev/null
            if printf '%s\n' "$1" >"$EPISTEME_STOP_FILE"; then
                log WARN "stop ($1): asked the stage to stop ($EPISTEME_STOP_FILE); waiting for it to end"
            else
                log ERROR "stop ($1): could not write $EPISTEME_STOP_FILE; waiting for the stage to end"
            fi
        else
            log WARN "stop ($1): no stop file; waiting for the stage to end"
        fi
        # A second signal makes `wait` return early: wait until the stage is gone.
        while kill -0 "$STAGE_PID" 2>/dev/null; do
            wait "$STAGE_PID"
        done
        # A wrapper can end before the processes under it: wait for them too.
        # A zombie counts as ended: under a PID 1 that does not reap orphans
        # it would pass `kill -0` forever and the lock would never be released.
        for p in $tree; do
            while kill -0 "$p" 2>/dev/null && ! _stage_zombie "$p"; do
                sleep 0.2
            done
        done
        STAGE_PID=""
        STAGE_TREE=""
        # The request was for that stage only: the EXIT handler's own Python
        # (the --bulk index rebuild) must not see it. The file stays as a record.
        EPISTEME_STOP_FILE=""
        export EPISTEME_STOP_FILE
    fi
    exit "$2"
}

stage_install_stop_traps() {
    trap '_stage_stop INT 130' INT
    trap '_stage_stop TERM 143' TERM
}

# stage_hold_stops: for an EXIT handler. A later INT/TERM only logs, so the
# handler finishes (end state, run_end, lock release). A foreground command of
# the handler can still get the signal itself (a console Ctrl-C).
stage_hold_stops() {
    trap 'log WARN "stop ignored during exit handling (INT)"' INT
    trap 'log WARN "stop ignored during exit handling (TERM)"' TERM
}
