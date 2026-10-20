"""The heavy-job lock and the download lock (operating standard spec 6.1).

Files under ``lock_dir()``: ``heavy.lock`` and ``download.lock`` (JSON: pid,
create_time, run_id, stage, started_at, state) and ``heavy.request`` (a heavy
job that waits). An owner is a PID plus that process's create time, so a PID
that Windows reused counts as gone. Limits: a lock owned by a live but unrelated
process (a wrong inherited EPISTEME_RUNNER_PID) is not detected. A download and
a heavy job can both acquire at the same instant (each checks the other, then
creates its own file); the download then pauses at its first pause point, by
design (spec 6.1). A file that stays locked by another process (Windows) reads as
``_LOCKED`` and counts as live: the lock fails closed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psutil

_LOG = logging.getLogger(__name__)

EXIT_BUSY = 75
HEAVY_ADVISORY_KEY = 4994572608998821189  # b"EPISTEME" as a big-endian int64
POLL_SEC = 5.0
WAIT_LOG_SEC = 300.0
TAKEOVER_EXPIRE_SEC = 30.0
UNREADABLE_GRACE_SEC = 60.0
_TRY_ROUNDS = 40  # with the pause below, about 1 s of contention before a caller sees busy
_TRY_PAUSE_SEC = 0.02
_FS_TRIES = 5
_LOCKED = {"_unreadable": "locked"}  # read() sentinel: could not open the file, not stale
KINDS = ("heavy", "download")


class LockBusy(Exception):
    def __init__(self, holder: dict) -> None:
        self.holder = holder
        super().__init__(describe(holder))


def lock_dir() -> Path:
    from episteme.config import get_settings

    return get_settings().lock_dir


def describe(rec: dict | None) -> str:
    if rec is _LOCKED:
        return "a lock file that another process holds open"
    if not rec:
        return "an unreadable lock file"
    return (
        f"run {rec.get('run_id')} ({rec.get('stage')}, pid {rec.get('pid')}, "
        f"since {rec.get('started_at')})"
    )


def _path(kind: str, d: Path) -> Path:
    if kind not in KINDS:
        raise ValueError(f"unknown lock kind {kind!r}")
    return d / f"{kind}.lock"


def _request_path(d: Path) -> Path:
    return d / "heavy.request"


def _create_time(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return None


def owner_alive(rec: dict) -> bool:
    pid, ct = rec.get("pid"), rec.get("create_time")
    if not isinstance(pid, int):
        return False
    now_ct = _create_time(pid)
    if now_ct is None:
        return False
    return ct is None or abs(now_ct - float(ct)) < 1.0


def _retry(fn: Callable, *args):
    """Call ``fn``; on PermissionError (Windows: a reader holds the file) try again
    a few times, then raise the last error."""
    for attempt in range(_FS_TRIES):
        try:
            return fn(*args)
        except PermissionError:
            if attempt == _FS_TRIES - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


def _unlink(path: Path) -> None:
    try:
        _retry(os.unlink, path)
    except FileNotFoundError:
        pass


def read(path: Path) -> dict | None:
    """The record; None when there is no file; {} when it cannot be parsed;
    ``_LOCKED`` when the file stays unopenable (treated as live)."""
    try:
        text = _retry(lambda: path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except PermissionError:
        return _LOCKED
    try:
        rec = json.loads(text)
    except ValueError:
        return {}
    return rec if isinstance(rec, dict) else {}


def _is_stale(path: Path, rec: dict) -> bool:
    if rec is _LOCKED:  # fail closed: never stale, no grace time
        return False
    if rec == {}:  # being written, or damaged: stale only after a grace time
        try:
            return time.time() - path.stat().st_mtime > UNREADABLE_GRACE_SEC
        except FileNotFoundError:
            return False
    return not owner_alive(rec)


def _live(path: Path) -> dict | None:
    """The record of a lock or request file whose owner is alive, else None."""
    rec = read(path)
    if rec is None or _is_stale(path, rec):
        return None
    return rec


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(kind: str, run_id: str, stage: str, pid: int) -> dict:
    return {
        "kind": kind,
        "pid": pid,
        "create_time": _create_time(pid),
        "run_id": run_id,
        "stage": stage,
        "started_at": _now(),
        "state": "running",
    }


def _write_new(path: Path, rec: dict) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except (FileExistsError, PermissionError):  # PermissionError: Windows, delete pending
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec))
    return True


def _rewrite(path: Path, rec: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    _retry(os.replace, tmp, path)


def _take_over_stale(path: Path, rec: dict) -> None:
    """Delete a stale lock file, one taker at a time (a short-lived guard file)."""
    guard = path.with_name(path.name + ".takeover")
    try:
        fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except (FileExistsError, PermissionError):
        try:
            if time.time() - guard.stat().st_mtime > TAKEOVER_EXPIRE_SEC:
                _unlink(guard)  # a taker died holding the guard
        except (FileNotFoundError, PermissionError):
            pass
        return
    os.close(fd)
    try:
        now = read(path)
        if now is not _LOCKED and now == rec:  # still the same stale record
            try:
                _unlink(path)
            except PermissionError:  # a reader still holds it; the caller tries again
                return
            _LOG.warning("took over a stale %s from %s", path.name, describe(rec))
    finally:
        try:
            _unlink(guard)
        except PermissionError:
            _LOG.error("could not remove %s; it expires in %s s", guard.name, TAKEOVER_EXPIRE_SEC)


def _blocker(kind: str, d: Path) -> dict | None:
    """A live lock of the other kind that blocks ``kind``."""
    if kind == "heavy":
        dl = _live(_path("download", d))
        return dl if dl and dl.get("state") != "paused" else None
    return _live(_path("heavy", d)) or _live(_request_path(d))


def try_acquire(
    kind: str,
    run_id: str,
    stage: str,
    *,
    owner_pid: int | None = None,
    d: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, dict]:
    d = d or lock_dir()
    d.mkdir(parents=True, exist_ok=True)
    pid = owner_pid or os.getpid()
    path = _path(kind, d)
    for _ in range(_TRY_ROUNDS):
        rec = read(path)
        if rec is None:
            new = _record(kind, run_id, stage, pid)
            if _write_new(path, new):
                return "acquired", new
            sleep(_TRY_PAUSE_SEC)
            continue  # another taker won the create: read it again
        if rec.get("run_id") == run_id and rec.get("pid") == pid and owner_alive(rec):
            return "reentrant", rec
        if _is_stale(path, rec):
            _take_over_stale(path, rec)
            sleep(_TRY_PAUSE_SEC)  # another taker may hold the guard
            continue
        return "busy", rec
    return "busy", read(path) or {}


def _post_request(run_id: str, stage: str, pid: int, d: Path) -> None:
    path = _request_path(d)
    if _live(path) is None:
        _rewrite(path, _record("heavy", run_id, stage, pid))


def _clear_request(run_id: str, d: Path) -> None:
    path = _request_path(d)
    rec = read(path)
    if rec is not None and rec.get("run_id") == run_id:
        try:
            _unlink(path)
        except PermissionError:  # never raise from acquire's finally
            _LOG.error("could not remove %s", path.name)


def acquire(
    kind: str,
    run_id: str,
    stage: str,
    *,
    owner_pid: int | None = None,
    wait: bool = True,
    d: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    d = d or lock_dir()
    d.mkdir(parents=True, exist_ok=True)
    pid = owner_pid or os.getpid()
    requested = False
    waited = 0.0
    try:
        while True:
            blocker = _blocker(kind, d)
            if blocker is None:
                status, rec = try_acquire(kind, run_id, stage, owner_pid=pid, d=d, sleep=sleep)
                if status != "busy":
                    return status
                blocker = rec
            if not wait:
                raise LockBusy(blocker)
            if kind == "heavy":
                _post_request(run_id, stage, pid, d)  # re-posted if another waiter cleared it
                requested = True
            if waited % WAIT_LOG_SEC == 0:
                _LOG.info("waiting for the %s lock: held by %s", kind, describe(blocker))
            sleep(POLL_SEC)
            waited += POLL_SEC
    finally:
        if requested:
            _clear_request(run_id, d)


def release(kind: str, run_id: str, *, owner_pid: int | None = None, d: Path | None = None) -> bool:
    d = d or lock_dir()
    path = _path(kind, d)
    rec = read(path)
    if rec and rec.get("run_id") == run_id and rec.get("pid") == (owner_pid or os.getpid()):
        try:
            _unlink(path)
        except PermissionError:  # never raise from a finally: the lock stays
            _LOG.error("could not release %s: another process holds it open", path.name)
            return False
        return True
    return False


@contextmanager
def held(
    kind: str, run_id: str, stage: str, *, owner_pid: int | None = None, wait: bool = True
) -> Iterator[str]:
    # resolve time.sleep at call time, so a test can replace it
    status = acquire(
        kind, run_id, stage, owner_pid=owner_pid, wait=wait, sleep=lambda sec: time.sleep(sec)
    )
    try:
        yield status
    finally:
        if status == "acquired":
            release(kind, run_id, owner_pid=owner_pid)


@contextmanager
def advisory_lock(
    *, wait: bool = True, sleep: Callable[[float], None] = time.sleep
) -> Iterator[None]:
    """Hold the PostgreSQL advisory lock ``HEAVY_ADVISORY_KEY`` for the block.

    It uses its own autocommit connection, tagged ``episteme:<run>:<pid>:lock``.
    That connection is idle between polls, so the orphan sweep (active
    backends only) never meets it; the server frees the lock when the process
    dies. The lock is per database."""
    import psycopg

    from episteme.config import get_settings
    from episteme.data.db.connection import app_name, dsn_from_settings

    conn = psycopg.connect(
        dsn_from_settings(),
        autocommit=True,
        application_name=app_name(get_settings(), suffix=":lock"),
    )
    got = False
    try:
        waited = 0.0
        while True:
            row = conn.execute("select pg_try_advisory_lock(%s)", (HEAVY_ADVISORY_KEY,)).fetchone()
            if row and row[0]:
                got = True
                break
            if not wait:
                raise LockBusy({"stage": "advisory lock", "run_id": "another session"})
            if waited % WAIT_LOG_SEC == 0:
                _LOG.info("waiting for the database advisory lock")
            sleep(POLL_SEC)
            waited += POLL_SEC
        yield
    finally:
        try:
            if got:
                conn.execute("select pg_advisory_unlock(%s)", (HEAVY_ADVISORY_KEY,))
        except Exception:  # noqa: BLE001 - closing the connection frees the lock too
            _LOG.exception("could not release the advisory lock")
        finally:
            conn.close()


@contextmanager
def stage_lock(stage: str, *, db: bool = False) -> Iterator[str]:
    """The heavy lock for one stage; with ``db`` also the advisory lock. Yields
    the run id. A stage run by a wrapper script re-enters the wrapper's lock:
    the wrapper takes it as ``adhoc-<runner pid>`` owned by the runner PID."""
    from episteme.config import get_settings

    settings = get_settings()
    owner = settings.runner_pid or os.getpid()
    run_id = settings.run_id or f"adhoc-{owner}"
    with held("heavy", run_id, stage, owner_pid=owner):
        if db:
            with advisory_lock():
                yield run_id
        else:
            yield run_id


def _heavy_wanted(d: Path) -> bool:
    return _live(_path("heavy", d)) is not None or _live(_request_path(d)) is not None


def pause_point(
    run_id: str, *, d: Path | None = None, sleep: Callable[[float], None] = time.sleep
) -> bool:
    """Call between files of a download. Pauses while a heavy job holds or wants
    the heavy lock; returns True when it paused."""
    d = d or lock_dir()
    if not _heavy_wanted(d):
        return False
    path = _path("download", d)

    def _set(state: str) -> None:
        rec = read(path)
        if rec and rec.get("run_id") == run_id:
            rec["state"] = state
            try:
                _rewrite(path, rec)
            except PermissionError:  # never raise from the finally below
                _LOG.error("could not set the download lock state to %s", state)

    _set("paused")
    _LOG.info("download paused: a heavy job holds or wants the heavy lock")
    try:
        while _heavy_wanted(d):
            sleep(POLL_SEC)
    finally:
        _set("running")
    _LOG.info("download resumed")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m episteme.ops.lock")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("acquire", "release"):
        p = sub.add_parser(name)
        p.add_argument("kind", choices=KINDS)
        p.add_argument("--run-id", required=True)
        p.add_argument("--stage", default="")
        p.add_argument("--owner-pid", type=int, default=None)
        if name == "acquire":
            p.add_argument("--no-wait", action="store_true")
    sub.add_parser("status")
    pp = sub.add_parser("pause-point")
    pp.add_argument("--run-id", required=True)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.cmd == "acquire":
        try:
            print(
                acquire(
                    args.kind,
                    args.run_id,
                    args.stage,
                    owner_pid=args.owner_pid,
                    wait=not args.no_wait,
                )
            )
        except LockBusy as exc:
            print(f"busy: the {args.kind} lock is held by {exc}", file=sys.stderr)
            return EXIT_BUSY
        return 0
    if args.cmd == "release":
        release(args.kind, args.run_id, owner_pid=args.owner_pid)
        return 0
    if args.cmd == "pause-point":
        pause_point(args.run_id)
        return 0
    d = lock_dir()
    for name in ("heavy.lock", "download.lock", "heavy.request"):
        rec = read(d / name)
        state = (
            "free"
            if rec is None
            else ("stale " if _is_stale(d / name, rec) else "") + describe(rec)
        )
        print(f"{name:14} {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
