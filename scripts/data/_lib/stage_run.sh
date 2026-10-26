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
# * On INT or TERM the trap writes the signal name to $EPISTEME_STOP_FILE.
#   The stage's parent watchdog (episteme.ops.stop) polls that file every
#   second and stops the stage as a SIGTERM would (exit 143). The trap then
#   waits for the stage to end and exits 130 (INT) or 143 (TERM), so the
#   caller's EXIT handler runs only after the stage has ended.
# * A background command of a non-interactive bash ignores SIGINT (POSIX), so
#   Ctrl-C reaches the stage through the stop file too.
#
# EPISTEME_STOP_FILE is the caller's: export it before the first stage. Empty:
# no stop file; the trap then only waits for the stage.

STAGE_PID=""  # the stage that stage_exec waits for; empty between stages

# stage_exec CMD [ARG...]: run CMD in the background, wait, return its exit code.
# stdin, stdout and stderr stay the caller's.
stage_exec() {
    # A stop request is for the stage that runs now, never for the next one.
    [ -z "${EPISTEME_STOP_FILE:-}" ] || rm -f "$EPISTEME_STOP_FILE"
    "$@" <&0 &
    STAGE_PID=$!
    local rc=0
    wait "$STAGE_PID" || rc=$?
    STAGE_PID=""
    return "$rc"
}

# _stage_stop SIGNAL CODE: the INT/TERM trap.
_stage_stop() {
    if [ -n "$STAGE_PID" ]; then
        if [ -n "${EPISTEME_STOP_FILE:-}" ]; then
            # A dry run writes no status, so the run folder may not exist yet.
            mkdir -p "$(dirname "$EPISTEME_STOP_FILE")" 2>/dev/null
            if printf '%s\n' "$1" >"$EPISTEME_STOP_FILE"; then
                log WARN "stop ($1): asked the stage to stop ($EPISTEME_STOP_FILE); waiting for it to end"
            else
                log ERROR "stop ($1): could not write $EPISTEME_STOP_FILE; waiting for the stage to end on its own"
            fi
        else
            log WARN "stop ($1): no stop file; waiting for the stage to end"
        fi
        # A second signal makes `wait` return early: wait until the stage is gone.
        while kill -0 "$STAGE_PID" 2>/dev/null; do
            wait "$STAGE_PID"
        done
        STAGE_PID=""
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
