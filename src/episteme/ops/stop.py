"""Stop handling: turn SIGTERM/SIGBREAK (and the runner's stop-request file)
into KeyboardInterrupt, and cancel the running statement when the parent
process dies.

A hard kill of the runner on Windows once left a 3-hour orphan DELETE running
in PostgreSQL. A stage wraps its main in ``stage_guard`` so a stop or a dead
parent cancels the statement instead of leaving it behind.

The watched process is the runner (``EPISTEME_RUNNER_PID``, the Windows PID of
the outermost pipeline script), else the first ancestor that is not the venv
launcher.
"""

from __future__ import annotations

import _thread
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import Any

import psutil

from episteme.ops.lock import EXIT_BUSY, LockBusy

_LOG = logging.getLogger(__name__)

EXIT_INT = 130
EXIT_TERM = 143

# The stop-request file is polled this often (seconds); the parent check keeps
# its own ``interval``.
STOP_POLL_S = 1.0
# After a stop request, the watchdog cancels the statement if the stage still
# runs this many seconds later (its main thread is inside a long C call).
STOP_CANCEL_AFTER_S = 5.0

_TERM_SEEN = False
# True once a stop (SIGINT, SIGTERM, SIGBREAK, the stop file) reached this
# stage: the watchdog then delivers no second stop from the stop file, which
# could cut the cleanup of the first one short.
_STOPPING = False


def _on_term(signum: int, frame: Any) -> None:
    global _TERM_SEEN, _STOPPING
    _TERM_SEEN = True
    _STOPPING = True
    signal.default_int_handler(signum, frame)


def _on_int(signum: int, frame: Any) -> None:
    global _STOPPING
    _STOPPING = True
    signal.default_int_handler(signum, frame)


def install_stop_handlers() -> None:
    """SIGTERM (and SIGBREAK on Windows) raise ``KeyboardInterrupt`` in the
    main thread. SIGINT keeps raising ``KeyboardInterrupt`` and also marks the
    stop; a SIGINT that starts ignored (a POSIX background job) stays ignored.
    Safe to call more than once."""
    names = ("SIGTERM", "SIGBREAK")
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_term)
        except ValueError:  # not the main thread
            _LOG.warning("cannot install %s handler outside the main thread", name)
    if signal.getsignal(signal.SIGINT) in (signal.default_int_handler, _on_int):
        try:
            signal.signal(signal.SIGINT, _on_int)
        except ValueError:  # not the main thread
            _LOG.warning("cannot install SIGINT handler outside the main thread")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _launcher_paths() -> set[str]:
    """Paths of the venv launcher. On Windows a uv venv's ``python.exe`` is a
    launcher that starts the real interpreter as its child; inside the real
    interpreter ``sys.executable`` is the launcher's path."""
    paths = {
        sys.executable,
        os.path.join(sys.prefix, "Scripts", "python.exe"),
        os.path.join(sys.prefix, "Scripts", "pythonw.exe"),
    }
    return {_norm(p) for p in paths if p}


def _exe(proc: psutil.Process) -> str:
    try:
        return proc.exe() or ""
    except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return ""


def _ancestor_to_watch() -> psutil.Process | None:
    """The first ancestor that is not the venv launcher. On Linux there is no
    launcher, so this is the parent."""
    launchers = _launcher_paths()
    try:
        proc = psutil.Process(os.getpid()).parent()
        while proc is not None:
            exe = _exe(proc)
            if not exe or _norm(exe) not in launchers:
                return proc
            proc = proc.parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        _LOG.warning("cannot walk the parent processes: %s", exc)
    return None


def _open(pid: int) -> psutil.Process | None:
    """``psutil.Process(pid)``, or None when that process is not running."""
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError) as exc:
        _LOG.warning("process %s cannot be watched: %s", pid, exc)
        return None
    return proc if _is_running(proc) else None


def _is_running(proc: psutil.Process) -> bool:
    """``is_running`` also checks the create time, so a reused PID counts as gone."""
    try:
        return proc.is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        _LOG.warning("process %s treated as gone: %s", proc.pid, exc)
        return False


def _runner_pid() -> int | None:
    try:
        from episteme.config import get_settings

        return get_settings().runner_pid
    except Exception as exc:  # noqa: BLE001 - no settings: use the ancestor walk
        _LOG.warning("runner PID unavailable (%s); watching the parent process", exc)
        return None


def _watch_target(parent_pid: int | None) -> tuple[psutil.Process | None, int | None]:
    """The process to watch, in this order: an explicit ``parent_pid``; the
    runner (``EPISTEME_RUNNER_PID``, exported by scripts/data/_lib/common.sh) if
    it is running now; the first ancestor that is not the venv launcher.
    Returns ``(process, pid)``; a None process means it is already gone."""
    if parent_pid is not None:
        return _open(parent_pid), parent_pid
    runner = _runner_pid()
    if runner is not None:
        proc = _open(runner)
        if proc is not None:
            return proc, runner
        _LOG.warning(
            "EPISTEME_RUNNER_PID=%s is not running; watching the parent process instead",
            runner,
        )
    proc = _ancestor_to_watch()
    return proc, (proc.pid if proc is not None else None)


def _stop_file() -> Path | None:
    """The stop-request file of this run (``EPISTEME_STOP_FILE``), or None."""
    try:
        from episteme.config import get_settings

        return get_settings().stop_file
    except Exception as exc:  # noqa: BLE001 - no settings: no stop file
        _LOG.warning("stop file unavailable (%s); only a signal stops this stage", exc)
        return None


def _self_process() -> psutil.Process:
    return psutil.Process(os.getpid())


def _end_children(timeout: float = 5.0) -> None:
    """Terminate this process's descendants (the pool workers), then kill the
    ones left after ``timeout``. Best effort: the process exits next."""
    try:
        kids = _self_process().children(recursive=True)
        if not kids:
            return
        for p in kids:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                p.terminate()
        _gone, alive = psutil.wait_procs(kids, timeout=timeout)
        for p in alive:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                p.kill()
        if alive:
            psutil.wait_procs(alive, timeout=timeout)
        _LOG.error(
            "terminated %d child process(es) before exiting (%d killed)", len(kids), len(alive)
        )
    except Exception:  # noqa: BLE001 - the process exits next
        _LOG.exception("could not end the child processes")


def _bounded(fn: Callable[[], None], timeout: float, what: str) -> None:
    """Run ``fn`` in a helper thread and wait at most ``timeout`` seconds (a
    cancel can hang when the DB is unreachable)."""

    def _run() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - best effort
            _LOG.exception("%s failed", what)

    helper = threading.Thread(target=_run, name="watchdog-cancel", daemon=True)
    helper.start()
    helper.join(timeout)
    if helper.is_alive():
        _LOG.error("%s did not finish within %ss; going on", what, timeout)


_UNSET: Any = object()


def start_parent_watchdog(
    cancel: Callable[[], None],
    *,
    interval: float = 10.0,
    parent_pid: int | None = None,
    cancel_timeout: float = 10.0,
    stop: threading.Event | None = None,
    stop_file: Path | None = _UNSET,
    stop_poll: float = STOP_POLL_S,
    cancel_after: float = STOP_CANCEL_AFTER_S,
) -> threading.Thread:
    """Start a daemon thread that watches two things.

    * The watched process (see ``_watch_target``), every ``interval`` seconds.
      When it is gone: cancel the running statement, end this process's child
      processes (the pool workers), and exit the process (code 143).
    * The stop-request file (``stop_file``; default ``EPISTEME_STOP_FILE``),
      every ``stop_poll`` seconds. ``run_pipeline.sh`` writes it on INT/TERM:
      on Windows bash cannot signal a native Python. When the file appears, the
      SIGTERM handler runs in the main thread (``_thread.interrupt_main``),
      once, as a real SIGTERM would; ``stage_guard`` then cancels the statement
      and exits 143. No ``os._exit`` on this path. The main thread sees the
      stop at its next Python instruction: if the process still runs
      ``cancel_after`` seconds later (a long statement), the watchdog cancels
      the statement too.

    Setting ``stop`` ends the thread without an exit (the guarded stage
    finished)."""
    # Limits: a zombie parent on POSIX counts as running.
    proc, ppid = _watch_target(parent_pid)
    request = _stop_file() if stop_file is _UNSET else stop_file
    watch_parent = proc is not None or parent_pid is not None

    if not watch_parent:
        # Nothing to watch (no runner, no usable ancestor): a stage must never
        # stop itself for that. Run without a parent watchdog.
        _LOG.warning("no process to watch; running without a parent watchdog")
        if request is None:

            def _idle() -> None:
                if stop is not None:
                    stop.wait()

            t = threading.Thread(target=_idle, name="parent-watchdog", daemon=True)
            t.start()
            return t

    asked_at: float | None = None
    cancelled = False

    def _check_request() -> None:
        nonlocal asked_at, cancelled
        if request is None:
            return
        if asked_at is None:
            try:
                if not request.exists():
                    return
                why = request.read_text(encoding="utf-8").strip()
            except OSError:
                return
            asked_at = time.monotonic()
            if _STOPPING:
                # A stop already arrived: a forwarded SIGTERM (POSIX) or a
                # console Ctrl-C (Windows). A second interrupt could cut the
                # cleanup of the first short.
                _LOG.warning("stop requested (%s, %s): a stop already arrived", why, request)
                return
            _LOG.warning("stop requested (%s, %s): stopping the stage", why or "?", request)
            _thread.interrupt_main(signal.SIGTERM)
        elif not cancelled and time.monotonic() - asked_at >= cancel_after:
            cancelled = True
            _LOG.warning(
                "the stage still runs %ss after the stop; cancelling the statement", cancel_after
            )
            _bounded(cancel, cancel_timeout, "cancel after the stop request")

    def _rest(secs: float) -> bool:
        """Sleep; True when ``stop`` is set (the stage finished)."""
        if stop is not None:
            return stop.wait(secs)
        time.sleep(secs)
        return False

    tick = interval if request is None else min(stop_poll, interval)

    def _watch() -> None:
        due = time.monotonic()
        while True:
            if watch_parent and time.monotonic() >= due:
                if proc is None or not _is_running(proc):
                    break
                due = time.monotonic() + interval
            _check_request()
            if _rest(tick):
                return

        # A clean finish at the same instant as the parent's death must not exit 143.
        if stop is not None and stop.is_set():
            return
        _bounded(cancel, cancel_timeout, f"cancel after parent {ppid} exited")
        _LOG.error("parent process %s is gone; exiting", ppid)
        # The pool workers would outlive an os._exit of this process.
        _end_children()
        os._exit(EXIT_TERM)

    t = threading.Thread(target=_watch, name="parent-watchdog", daemon=True)
    t.start()
    return t


class StageHandle:
    """What ``stage_guard`` yields. A main reports its return code with
    ``exit_code(rc)``; the end state follows from it."""

    def __init__(self) -> None:
        self.rc: int | None = None

    def exit_code(self, rc: int) -> int:
        self.rc = rc
        return rc


def _state_for_rc(rc: int | None) -> str:
    if rc is None or rc == 0:
        return "done"
    if rc in (EXIT_INT, EXIT_TERM):
        return "stopped"
    return "failed"


@contextmanager
def stage_guard(
    conn_ref: Callable[[], Any],
    *,
    lock_stage: str | None = None,
    db_lock: bool = False,
    keep_stage: bool = False,
    argv: Sequence[str] | None = None,
    source: str | None = None,
    dsn: str | None = None,
) -> Iterator[StageHandle]:
    """Stage-main wrapper: install the handlers, start the watchdog, and map a
    stop to ``SystemExit`` 130 (INT) or 143 (TERM). With ``lock_stage`` it also
    holds the heavy lock (and, with ``db_lock``, the advisory lock) for the
    block, and writes the run status: ``running`` once the heavy lock is held,
    and the end state (from ``exit_code``) at exit; ``run_pipeline.sh`` writes
    after it, so its value wins. The advisory-lock wait is marked as a long
    statement, so a long wait is not reported as a stall. A wait for a lock can
    be stopped like any other step. Under ``run_pipeline.sh --no-wait`` a busy
    lock maps to ``SystemExit(75)`` (state ``refused``). With ``keep_stage`` (a
    helper that runs inside another stage of the same run, such as
    ``bulk_indexes`` under ``--bulk graph``) an existing stage label is kept,
    and a ``done`` end does not replace the end state that stage wrote. The
    watchdog's ``os._exit(143)`` leaves ``heavy.lock`` behind; the next run
    takes it over as stale. ``dsn`` is the login of the advisory lock (default: the normal login).
    ``source`` is written with the status. A help
    request (``-h``/``--help`` in ``argv``, else in ``sys.argv[1:]``) takes no
    lock and writes no status: only the handlers apply."""

    def _cancel() -> None:
        c = conn_ref()
        if c is not None:
            c.cancel_safe()

    global _TERM_SEEN, _STOPPING
    _TERM_SEEN = False
    _STOPPING = False
    install_stop_handlers()
    done = threading.Event()
    start_parent_watchdog(_cancel, stop=done)
    handle = StageHandle()
    st: Any = None
    beat: threading.Thread | None = None
    kept: str | None = None
    end_state = "failed"
    check = sys.argv[1:] if argv is None else argv
    help_only = any(a in ("-h", "--help") for a in check)
    try:
        if lock_stage is None or help_only:
            yield handle
        else:
            from episteme.ops import lock
            from episteme.ops import status as ops_status

            with ExitStack() as stack:
                run_id = stack.enter_context(lock.heavy_stage_lock(lock_stage))
                st, beat, kept = _begin_status(
                    run_id, lock_stage, done, keep_stage=keep_stage, source=source
                )
                if db_lock:
                    with ops_status.current().long_statement("waiting for the advisory lock"):
                        stack.enter_context(lock.advisory_lock(wait=not lock.no_wait(), dsn=dsn))
                yield handle
        end_state = _state_for_rc(handle.rc)
    except LockBusy as exc:
        end_state = "refused"
        print(f"refused: the lock is busy: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BUSY) from None
    except KeyboardInterrupt:
        _STOPPING = True  # no second stop from the stop file during the cleanup
        end_state = "stopped"
        try:
            _cancel()
        except Exception:  # noqa: BLE001 - best effort; the exit follows
            _LOG.exception("cancel failed on stop")
        raise SystemExit(EXIT_TERM if _TERM_SEEN else EXIT_INT) from None
    except SystemExit as exc:
        if exc.code in (EXIT_INT, EXIT_TERM):
            end_state = "stopped"
        elif exc.code in (0, None):
            end_state = "done"
        raise
    finally:
        done.set()  # a finished stage must not leave a watchdog behind
        if beat is not None:
            beat.join(5.0)  # no heartbeat write after the end state
        _end_status(st, kept if (kept and end_state == "done") else end_state)


_ENDED = ("done", "failed", "stopped", "refused")


def _begin_status(
    run_id: str,
    stage: str,
    stop_beat: threading.Event,
    *,
    keep_stage: bool = False,
    source: str | None = None,
) -> tuple[Any, threading.Thread | None, str | None]:
    """Create this run's status, record the stage process, start the heartbeat.
    Returns (status, heartbeat thread, kept end state). With ``keep_stage`` and
    a stage label already in the file, the label and ``started_at`` stay, and
    an end state already written is returned, so a ``done`` end keeps it.
    A status problem never fails the stage: it logs a WARNING and returns no status."""
    try:
        from episteme.ops import status
        from episteme.ops.lock import create_time

        st = status.RunStatus(status.run_dir(run_id))
        old = st.read()
        pids = dict(old.get("pids") or {})  # keep the runner entry
        pids["stage"] = {"pid": os.getpid(), "create_time": create_time(os.getpid())}
        now = status.now_iso()
        fields: dict[str, Any] = {
            "run_id": run_id,
            "state": "running",
            "pids": pids,
            "last_progress": now,
        }
        if source is not None:
            fields["source"] = source
        kept: str | None = None
        if keep_stage and old.get("stage"):
            kept = old.get("state") if old.get("state") in _ENDED else None
        else:
            fields["stage"] = stage
            if old.get("stage") != stage:
                fields.update(status.STAGE_PROGRESS_RESET)
            if old.get("stage") != stage or not old.get("started_at"):
                fields["started_at"] = now
        st.update(**fields)
        status.set_current(st)
        # reset the loader's row counter, only when that module is loaded
        reset = getattr(
            sys.modules.get("episteme.data.postgres_loader"), "reset_process_rows", None
        )
        if reset is not None:
            reset()
        return st, st.start_heartbeat(stop_beat), kept
    except Exception as exc:  # noqa: BLE001 - a status problem must not fail the stage
        _LOG.warning("run status unavailable (%r); the stage runs without it", exc)
        try:
            from episteme.ops import status

            status.set_current(status.NullStatus())
        except Exception:  # noqa: BLE001
            pass
        return None, None, None


def _end_status(st: Any, end_state: str) -> None:
    """Write the end state and drop the process-wide status."""
    if st is None:
        return
    from episteme.ops import status

    st.update(state=end_state)
    status.set_current(status.NullStatus())
