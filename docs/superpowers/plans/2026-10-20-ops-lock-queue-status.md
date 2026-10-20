# Ops lock, queue and status (PR 3 of the operating standard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Only one heavy disk or database job runs at a time, and downloads pause while one runs. Every run reports its progress in one status file, and a stalled or crashed run is detected. A queue runner runs steps in order and stops at the first step that did not really finish.

**Architecture:** `ops/lock.py` holds two file locks under `<data root>/_ops/locks/` (`heavy.lock`, `download.lock`) plus a `heavy.request` file. A lock records its owner PID and that process's create time, so a reused PID never looks alive. A waiting heavy job posts a request; a download pauses at its next pause point and resumes when the heavy job ends. Python database stages also hold a PostgreSQL advisory lock on a dedicated connection. `ops/status.py` writes `<processed root>/_ops/runs/<run_id>/status.json`. A heartbeat thread writes `heartbeat`, and the work writes `last_progress`; stall detection uses `last_progress`. `run_pipeline.sh` takes the lock for each stage and writes the stage state. `run_queue.sh` runs `run_pipeline.sh` steps and checks each log for the exact `pipeline done: <source> <stage>` line.

**Tech Stack:** Python 3.13 (requires-python >= 3.10), psutil, psycopg 3, pytest (`pg` marker for DB tests), Git Bash.

**Spec:** `docs/superpowers/specs/2026-10-19-pipeline-operating-standard-design.md` (sections 4, 6.1, 6.3, 6.5, 6.6, 8, 9 PR 3, 11).

## Global Constraints

- One heavy job per machine: `load`, `graph`, `enrich`, `materialize`, every `extract` and `serialize`, `scripts/data/db/migrate_database.sh` and `scripts/data/db/bulk_load_indexes.sh` (spec 6.1).
- A second heavy job waits by default. With `--no-wait` it refuses with exit code **75** (spec 6.1).
- A lock whose owner process is gone is stale. The next holder takes it over and logs a WARNING (spec 6.1). The owner is a PID plus that process's create time (Windows reuses PIDs).
- A download runs only while no heavy job holds or requests the heavy lock. At a pause point it finishes the current file or batch, pauses, and resumes when the heavy lock is released (spec 6.1). Tools with no pause point (`s5cmd`, `aws`, the HF CLI) are not paused: the heavy job waits for them to finish and logs that (spec 11, open point 2).
- The PostgreSQL advisory lock key is the literal `4994572608998821189` (the bytes of `EPISTEME` as a big-endian int64). Advisory locks are per database (spec 11, open point 1).
- Status: `<run dir>/status.json` is written every 30 s and when a unit ends. `--stalled` exits 1 when `last_progress` is older than 10 min, or 60 min inside a marked long statement, or when a running run's runner process is gone (spec 6.6).
- A queue step is done only when it exits 0 **and** its log holds the exact line `pipeline done: <source> <stage>`. Otherwise the queue writes `STOPPED at <source> <stage>` and stops. The queue takes no lock, and never edits or sources a script that is running (spec 6.5).
- A download file that still fails after the fetch engine's retries goes into `<run dir>/failed_files.txt`; the download goes on, and the stage ends non-zero (spec 6.3).
- Tests never touch the real lock, runs or queue folders (an autouse conftest redirect, as `_redirect_failure_record` does for `last_failure.json`).
- `config.py` is the only module that reads `os.environ`. Endpoint URLs only in `scripts/data/_lib/sources.env`. `git add` explicit paths only. Never edit a script that is running.
- CI runs `ruff format --check .` (ruff 0.17, Python blocks in Markdown included), `ruff check .` and `pytest -m "not pg"`. Run them before each commit.

## Review Focus

1. **A run that hangs in one statement while its heartbeat thread is alive.** Expected: `--stalled` reports it after 10 min, or after 60 min inside a marked long statement, because it reads `last_progress`, not `heartbeat`. Test in Task 3.
2. **A test that touches the production lock.** Expected: no test can create, take over or delete a lock under the real data root. A conftest guard fails a test whose resolved lock folder is outside the pytest temp root. Test in Task 1.
3. **`--no-wait` refusal.** Expected: a single-stage run refuses with exit 75 before `run_start`, so no audit row is written. An `all` chain that is refused at a later stage writes `run_end` with reason `refused: heavy lock held by <run_id> (<stage>)` and exits 75. Test in Task 6.
4. **Two waiters and one stale lock.** Expected: exactly one takes it over; the other stays waiting. A lock whose PID was reused by a new process counts as stale. Test in Task 1.
5. **A queue step that exits 0 but did not finish.** Expected: exit 0 without the exact `pipeline done: <source> <stage>` line stops the queue; a step whose code changed under the queue (HEAD or `scripts/data`, `src` changed) stops the queue before that step. Test in Task 7.

---

## File Structure

- Create `src/episteme/ops/lock.py`, `src/episteme/ops/status.py`, `scripts/data/run_queue.sh`.
- Modify `src/episteme/config.py` (`lock_dir`, `run_dir`), `src/episteme/ops/stop.py` (`stage_guard` takes the stage lock and the status), `src/episteme/data/parallel.py` (progress per unit), `src/episteme/data/load_articles.py`, `src/episteme/data/postgres_loader.py`, `src/episteme/data/graph_load.py`, `src/episteme/data/graph_builder.py`, `src/episteme/data/db/bulk_indexes.py`, `src/episteme/data/cdisc_ct/retire.py`, the 16 extract and serialize mains (one argument on their `stage_guard` call), `src/episteme/data/pmc/download_pmc.py`, `scripts/data/_lib/common.sh` (`http_fetch` batches, pause points, failed files), `scripts/data/run_pipeline.sh`, `scripts/data/db/migrate_database.sh`, `scripts/data/db/bulk_load_indexes.sh`, `tests/conftest.py`.
- Tests: `tests/ops/test_lock.py`, `tests/ops/test_status.py`, `tests/ops/test_stage_lock.py`, `tests/test_run_pipeline_lock_status.py`, `tests/test_run_queue.py`, `tests/test_http_fetch_failed_files.py`, plus `pg` tests named in each task.
- Docs: `docs/10-data-sources-runbook.md` (new section "Locks, status and the queue"), `docs/project-incubation-baseline.md` (drift-log entry).

---

### Task 1: `ops/lock.py` — the two file locks, stale takeover, CLI; test isolation

**Files:**
- Create: `src/episteme/ops/lock.py`
- Modify: `src/episteme/config.py` (add `lock_dir`), `tests/conftest.py` (autouse redirect + guard)
- Test: `tests/ops/test_lock.py`

**Interfaces:**
- Consumes: `get_settings().data_root`.
- Produces:
  - `config.Settings.lock_dir: Path` = `EPISTEME_LOCK_DIR` if set, else `data_root / "_ops" / "locks"`.
  - `lock.EXIT_BUSY = 75`, `lock.HEAVY_ADVISORY_KEY = 4994572608998821189`, `lock.POLL_SEC = 5.0`.
  - `lock.LockBusy(Exception)` with `.holder: dict`.
  - `lock.owner_alive(rec: dict) -> bool`; `lock.read(path: Path) -> dict | None` (None = no file; `{}` = unreadable).
  - `lock.try_acquire(kind, run_id, stage, *, owner_pid=None, d=None) -> tuple[str, dict]` with status `"acquired" | "reentrant" | "busy"`.
  - `lock.acquire(kind, run_id, stage, *, owner_pid=None, wait=True, d=None, sleep=time.sleep) -> str` (`"acquired"` or `"reentrant"`; raises `LockBusy` when `wait=False`).
  - `lock.release(kind, run_id, *, owner_pid=None, d=None) -> bool`.
  - `lock.held(kind, run_id, stage, *, owner_pid=None, wait=True)` context manager (releases only what it acquired).
  - `lock.pause_point(run_id, *, d=None, sleep=time.sleep) -> bool`.
  - CLI `python -m episteme.ops.lock acquire|release KIND --run-id R [--stage S] [--owner-pid P] [--no-wait]`, `status`, `pause-point --run-id R`.

- [ ] **Step 1: config key.** In `config.py`, add `lock_dir: Path` to `Settings` and set it from `_get("EPISTEME_LOCK_DIR")` or `data_root / "_ops" / "locks"`, like `processed_root`. Add `EPISTEME_LOCK_DIR` to the env table in docs/10 (Task 8 writes the prose).

- [ ] **Step 2: conftest isolation.** Read `tests/conftest.py` `_redirect_failure_record` and follow its rule. Add an autouse fixture `_redirect_ops_dirs` that monkeypatches `episteme.ops.lock.lock_dir` to return `<tmp>/_ops/locks` (a per-test temp folder), and `episteme.ops.status.runs_root` to `<tmp>/_ops/runs` (Task 3 creates `status.py`; patch it only when `importlib.util.find_spec("episteme.ops.status")` finds it, so this task's tests run before Task 3 exists). Add `test_lock_dir_is_redirected_in_tests` in `tests/ops/test_lock.py`: `lock.lock_dir()` resolves inside `tmp_path_factory.getbasetemp()`.

- [ ] **Step 3: failing tests** in `tests/ops/test_lock.py` (not pg). Use `d=tmp_path` and fake owners:
  - `acquire` on an empty folder returns `"acquired"` and writes `heavy.lock` with `pid`, `create_time`, `run_id`, `stage`, `started_at`, `state == "running"`.
  - A second `try_acquire` by another run is `"busy"`; by the same `run_id` and owner PID it is `"reentrant"`.
  - `release` by the wrong run returns False and keeps the file; by the owner it deletes it.
  - A record whose PID does not exist is stale: `try_acquire` takes it over, returns `"acquired"`, and logs `took over a stale heavy.lock`.
  - PID reuse: a record with the current process's PID but a `create_time` 100 s off counts as stale.
  - An unreadable file (bad JSON) younger than 60 s is busy; older than 60 s (set mtime with `os.utime`) is stale.
  - `acquire(wait=False)` on a busy lock raises `LockBusy` with the holder record.
  - `acquire(wait=True)` with a fake `sleep` that releases the holder on its second call returns `"acquired"` after 2 polls.
  - Two-process race: start 2 `multiprocessing` processes (spawn; module-level target) that both see the same stale record and call `try_acquire` at once, 20 rounds. Each round exactly one gets `"acquired"`.
  - Cross-kind: `download` acquire is busy while a live `heavy.lock` exists, and busy while a live `heavy.request` exists. `heavy` acquire is busy while a live `download.lock` has `state == "running"`, and succeeds when it has `state == "paused"`. A waiting heavy `acquire` writes `heavy.request` and removes it when it returns or raises.
  - `pause_point`: with no heavy lock and no request it returns False at once. With a live request, it sets `download.lock` `state` to `"paused"`, waits (fake `sleep` removes the request on the 2nd call), sets `state` back to `"running"` and returns True.
  - CLI: `acquire` prints `acquired`; a busy `--no-wait` acquire prints the holder to stderr and exits 75; `status` prints both locks and the request.

- [ ] **Step 4: implement** `src/episteme/ops/lock.py`:

```python
"""The heavy-job lock and the download lock (operating standard spec 6.1).

Files under ``lock_dir()``: ``heavy.lock`` and ``download.lock`` (JSON: pid,
create_time, run_id, stage, started_at, state) and ``heavy.request`` (a heavy
job that waits). An owner is a PID plus that process's create time, so a PID
that Windows reused counts as gone. Limits: a lock owned by a live but unrelated
process (a wrong inherited EPISTEME_RUNNER_PID) is not detected.
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
KINDS = ("heavy", "download")


class LockBusy(Exception):
    def __init__(self, holder: dict) -> None:
        self.holder = holder
        super().__init__(describe(holder))


def lock_dir() -> Path:
    from episteme.config import get_settings

    return get_settings().lock_dir


def describe(rec: dict | None) -> str:
    if not rec:
        return "an unreadable lock file"
    return f"run {rec.get('run_id')} ({rec.get('stage')}, pid {rec.get('pid')}, since {rec.get('started_at')})"


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


def read(path: Path) -> dict | None:
    """The record; None when there is no file; {} when it cannot be parsed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        rec = json.loads(text)
    except ValueError:
        return {}
    return rec if isinstance(rec, dict) else {}


def _is_stale(path: Path, rec: dict) -> bool:
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
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec))
    return True


def _rewrite(path: Path, rec: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, path)


def _take_over_stale(path: Path, rec: dict) -> None:
    """Delete a stale lock file, one taker at a time (a short-lived guard file)."""
    guard = path.with_name(path.name + ".takeover")
    try:
        fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - guard.stat().st_mtime > TAKEOVER_EXPIRE_SEC:
                guard.unlink(missing_ok=True)  # a taker died holding the guard
        except FileNotFoundError:
            pass
        return
    os.close(fd)
    try:
        if read(path) == rec:  # still the same stale record: no one replaced it
            path.unlink(missing_ok=True)
            _LOG.warning("took over a stale %s from %s", path.name, describe(rec))
    finally:
        guard.unlink(missing_ok=True)


def _blocker(kind: str, d: Path) -> dict | None:
    """A live lock of the other kind that blocks ``kind``."""
    if kind == "heavy":
        dl = _live(_path("download", d))
        return dl if dl and dl.get("state") != "paused" else None
    return _live(_path("heavy", d)) or _live(_request_path(d))


def try_acquire(
    kind: str, run_id: str, stage: str, *, owner_pid: int | None = None, d: Path | None = None
) -> tuple[str, dict]:
    d = d or lock_dir()
    d.mkdir(parents=True, exist_ok=True)
    pid = owner_pid or os.getpid()
    path = _path(kind, d)
    for _ in range(3):
        rec = read(path)
        if rec is None:
            new = _record(kind, run_id, stage, pid)
            if _write_new(path, new):
                return "acquired", new
            continue  # another taker won the create: read it again
        if rec.get("run_id") == run_id and rec.get("pid") == pid and owner_alive(rec):
            return "reentrant", rec
        if _is_stale(path, rec):
            _take_over_stale(path, rec)
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
        path.unlink(missing_ok=True)


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
                status, rec = try_acquire(kind, run_id, stage, owner_pid=pid, d=d)
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
        path.unlink(missing_ok=True)
        return True
    return False


@contextmanager
def held(
    kind: str, run_id: str, stage: str, *, owner_pid: int | None = None, wait: bool = True
) -> Iterator[str]:
    status = acquire(kind, run_id, stage, owner_pid=owner_pid, wait=wait)
    try:
        yield status
    finally:
        if status == "acquired":
            release(kind, run_id, owner_pid=owner_pid)


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
            _rewrite(path, rec)

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
```

- [ ] **Step 5:** run `tests/ops/test_lock.py` → pass; run `tests/ops` and `tests/test_config*.py` (if present). ruff format and check. Commit: `feat(ops): lock.py, the heavy and download locks with stale takeover`.

---

### Task 2: Advisory lock and the stage lock in `stage_guard`

**Files:**
- Modify: `src/episteme/ops/lock.py` (add `advisory_lock`, `stage_lock`), `src/episteme/ops/stop.py` (`stage_guard` gains `lock_stage`, `db_lock`), `src/episteme/data/load_articles.py`, `src/episteme/data/graph_builder.py`, `src/episteme/data/db/bulk_indexes.py`, `src/episteme/data/cdisc_ct/retire.py`, the 16 extract and serialize mains.
- Test: `tests/ops/test_stage_lock.py` (not pg), plus `pg` tests in the same file.

**Interfaces:**
- Consumes: Task 1 (`acquire`, `release`, `held`, `HEAVY_ADVISORY_KEY`, `POLL_SEC`, `LockBusy`); `get_settings().run_id`, `get_settings().runner_pid`; `episteme.data.db.connection` (read it: `dsn_from_settings()` and the application_name kwargs helper that PR 2 added).
- Produces:
  - `lock.advisory_lock(*, wait=True, sleep=time.sleep)` context manager: opens a **dedicated** `psycopg.connect(...)` (autocommit, tagged `application_name` like the pool, with the suffix `:lock`), polls `select pg_try_advisory_lock(%s)` with `HEAVY_ADVISORY_KEY` every `POLL_SEC` until true (or raises `LockBusy({"stage": "advisory lock", ...})` when `wait=False`), yields, then `pg_advisory_unlock` and closes in `finally`.
  - `lock.stage_lock(stage, *, db=False)` context manager: `run_id = settings.run_id or f"adhoc-{os.getpid()}"`; owner = `settings.runner_pid` when `settings.run_id` and `settings.runner_pid` are set, else `os.getpid()`; enters `held("heavy", run_id, stage, owner_pid=owner)` and, when `db`, `advisory_lock()` inside it. Yields `run_id`.
  - `stop.stage_guard(conn_ref, *, lock_stage: str | None = None, db_lock: bool = False)`: after the handlers and the watchdog start, enters `stage_lock(lock_stage, db=db_lock)` when `lock_stage` is set. Ctrl-C while waiting maps to 130 as usual. The existing callers without the new arguments keep their behaviour.

Wiring (one argument each):
- `load_articles.main`: `stage_guard(..., lock_stage="load", db_lock=True)`.
- `graph_builder.main`: `lock_stage="graph", db_lock=True`.
- `bulk_indexes` main and `cdisc_ct.retire` main: wrap in `stage_guard(lambda: None, lock_stage="load", db_lock=True)` if not already guarded.
- The 16 extract and serialize mains: `lock_stage="extract"` or `"serialize"` (no `db_lock`).

- [ ] **Step 1: failing tests (not pg).** With Task 1's redirect: `stage_lock("load")` with no run id acquires and releases `heavy.lock` (owner = this PID). With a fake settings `run_id="r1", runner_pid=<this pid>` and a `heavy.lock` already held by run `r1`, owner this PID: re-entrant, and the file is still there afterwards. With the lock held by another live run: `stage_guard(..., lock_stage="load")` waits (fake `sleep` releases it on the 2nd call). `db=True` calls `advisory_lock` (monkeypatched to record entry and exit). One extractor main and one serializer main pass `lock_stage` (assert via a patched `stage_lock`).
- [ ] **Step 2: pg tests.** Mark `pg`; follow the env rules of the existing pg tests (`PGDATABASE=episteme_test`, confirm `19beta3`). (a) `advisory_lock()` held in this process; a second connection's `pg_try_advisory_lock(4994572608998821189)` returns false; after exit it returns true (unlock it again). (b) `advisory_lock(wait=False)` while another connection holds the key raises `LockBusy`. (c) The dedicated connection's `application_name` starts with `episteme:` and ends with `:lock`.
- [ ] **Step 3:** implement. **Step 4:** run `tests/ops` and the stage tests (`tests/data/test_load_articles.py`, `tests/data/test_graph_builder_main.py`, `tests/data/test_extract_failfast.py`, `tests/data/test_serialize_failfast.py`, `tests/data/test_stage_guard_stop.py`). The existing tests that monkeypatch `stage_guard` with a lambda of one argument must accept the new keyword arguments (`lambda ref, **kw: nullcontext()`). **Step 5:** commit `feat(ops): stage lock and advisory lock in stage_guard`.

---

### Task 3: `ops/status.py` — the status file, heartbeat, progress, stall check, CLI

**Files:**
- Create: `src/episteme/ops/status.py`
- Modify: `src/episteme/config.py` (`run_dir: Path | None` from `EPISTEME_RUN_DIR`)
- Test: `tests/ops/test_status.py`

**Interfaces:**
- Consumes: `get_settings().processed_root`; `lock.owner_alive` (Task 1).
- Produces:
  - `status.runs_root() -> Path` = `processed_root / "_ops" / "runs"`; `status.run_dir(run_id) -> Path`.
  - `status.RunStatus(run_dir)` with `.read()`, `.update(**fields)`, `.heartbeat()`, `.progress(*, unit=None, units_done=None, units_total=None, rows_done=None)`, `.long_statement(label)` context manager, `.start_heartbeat(stop: threading.Event, interval=30.0) -> threading.Thread`.
  - `status.NullStatus` (same methods, no-ops); `status.current() -> RunStatus | NullStatus`; `status.set_current(st)`.
  - `status.stalled(rec: dict, now: datetime | None = None) -> str | None`.
  - CLI: `python -m episteme.ops.status [--stalled]` (list), `set --run-id R --source S --stage ST --state running|done|failed|stopped|refused [--unit U] [--reason TEXT] [--runner-pid P]`, `progress --run-id R [--unit U]`, `long-begin --run-id R --label L`, `long-end --run-id R`, `run-dir --run-id R` (prints the path), `queue-dir --queue-id Q` (prints `processed_root/_ops/queue/<Q>`).

- [ ] **Step 1: failing tests** (not pg, `runs_root` redirected by the Task 1 fixture):
  - `update` merges fields and writes atomically (no `.tmp` file left); `read` of a missing file is `{}`.
  - `progress(rows_done=1000)` sets `last_progress`, `heartbeat`, `rows_done` and a `rate_rows_per_s`; with `units_done=2, units_total=4` it sets an `eta_s`.
  - `long_statement("DELETE ...")` sets `long_statement.label` and `.since`, and clears it on exit (also when the body raises).
  - `start_heartbeat(stop, interval=0.01)` updates `heartbeat` but not `last_progress`; setting `stop` ends the thread.
  - `stalled`: a running record whose `last_progress` is 11 min old → a `stalled:` reason; 11 min old with a `long_statement` → None; 61 min with a `long_statement` → reason; `state == "done"` → None; a running record whose `pids.runner` names a dead PID → a `crashed:` reason; a heartbeat 1 s old but `last_progress` 11 min old → still stalled (Review Focus 1).
  - CLI: `set ... --state running` then `progress` then `--stalled` exit 0; with an old `last_progress` written by `update`, `--stalled` exits 1 and prints the run id and reason; `run-dir` prints `runs_root/<R>`.
- [ ] **Step 2: implement:**

```python
"""Run status files (operating standard spec 6.6).

One file per run: ``<processed root>/_ops/runs/<run_id>/status.json``. The
heartbeat thread writes ``heartbeat`` (the process is alive); the work writes
``last_progress`` (a unit, a batch or a phase ended). The stall check reads
``last_progress``, so a run stuck in one statement is found even while its
heartbeat thread runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

FILE = "status.json"
HEARTBEAT_SEC = 30.0
STALL_SEC = 600.0
LONG_STALL_SEC = 3600.0
STATES = ("running", "done", "failed", "stopped", "refused")
_TERMINAL = {"done", "failed", "stopped", "refused"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def runs_root() -> Path:
    from episteme.config import get_settings

    return get_settings().processed_root / "_ops" / "runs"


def run_dir(run_id: str) -> Path:
    return runs_root() / run_id


class RunStatus:
    def __init__(self, run_dir: Path) -> None:
        self.path = Path(run_dir) / FILE
        self._mutex = threading.Lock()
        self._t0 = time.monotonic()

    def read(self) -> dict[str, Any]:
        try:
            rec = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return rec if isinstance(rec, dict) else {}

    def update(self, **fields: Any) -> dict[str, Any]:
        with self._mutex:
            rec = self.read()
            rec.update(fields)
            rec["updated_at"] = _now()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{FILE}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
            return rec

    def heartbeat(self) -> dict[str, Any]:
        return self.update(heartbeat=_now())

    def progress(
        self,
        *,
        unit: str | None = None,
        units_done: int | None = None,
        units_total: int | None = None,
        rows_done: int | None = None,
    ) -> dict[str, Any]:
        now = _now()
        fields: dict[str, Any] = {"last_progress": now, "heartbeat": now}
        elapsed = time.monotonic() - self._t0
        if unit is not None:
            fields["unit"] = unit
        if units_done is not None:
            fields["units_done"] = units_done
        if units_total is not None:
            fields["units_total"] = units_total
        if units_done and units_total and elapsed > 0:
            fields["eta_s"] = int(elapsed / units_done * max(0, units_total - units_done))
        if rows_done is not None:
            fields["rows_done"] = rows_done
            fields["rate_rows_per_s"] = round(rows_done / elapsed, 1) if elapsed > 0 else None
        return self.update(**fields)

    @contextmanager
    def long_statement(self, label: str) -> Iterator[None]:
        self.update(long_statement={"label": label, "since": _now()})
        try:
            yield
        finally:
            self.update(long_statement=None, last_progress=_now())

    def start_heartbeat(
        self, stop: threading.Event, interval: float = HEARTBEAT_SEC
    ) -> threading.Thread:
        def _beat() -> None:
            while not stop.wait(interval):
                try:
                    self.heartbeat()
                except OSError as exc:  # a full or lost disk must not end the stage
                    _LOG.warning("status heartbeat failed: %r", exc)

        t = threading.Thread(target=_beat, name="status-heartbeat", daemon=True)
        t.start()
        return t


class NullStatus:
    """The status of a process that writes none (tests, tools)."""

    def read(self) -> dict[str, Any]:
        return {}

    def update(self, **fields: Any) -> dict[str, Any]:
        return {}

    def heartbeat(self) -> dict[str, Any]:
        return {}

    def progress(self, **fields: Any) -> dict[str, Any]:
        return {}

    @contextmanager
    def long_statement(self, label: str) -> Iterator[None]:
        yield

    def start_heartbeat(self, stop: threading.Event, interval: float = HEARTBEAT_SEC) -> None:
        return None


_CURRENT: RunStatus | NullStatus = NullStatus()


def current() -> RunStatus | NullStatus:
    return _CURRENT


def set_current(st: RunStatus | NullStatus) -> None:
    global _CURRENT
    _CURRENT = st


def _age(ts: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(ts)).total_seconds()


def stalled(rec: dict[str, Any], now: datetime | None = None) -> str | None:
    if rec.get("state") in _TERMINAL:
        return None
    from episteme.ops.lock import owner_alive

    now = now or datetime.now(timezone.utc)
    runner = (rec.get("pids") or {}).get("runner")
    if isinstance(runner, dict) and not owner_alive(runner):
        return "crashed: the runner process is gone"
    last = rec.get("last_progress") or rec.get("started_at")
    if not last:
        return None
    limit = LONG_STALL_SEC if rec.get("long_statement") else STALL_SEC
    age = _age(last, now)
    if age > limit:
        return f"stalled: no progress for {int(age)} s (limit {int(limit)} s)"
    return None


def list_runs(root: Path | None = None) -> list[tuple[str, dict[str, Any]]]:
    root = root or runs_root()
    if not root.is_dir():
        return []
    return [(p.name, RunStatus(p).read()) for p in sorted(root.iterdir()) if (p / FILE).is_file()]
```

  Write `main()` for the CLI as listed in **Produces**. `set` writes `source`, `stage`, `state`, `unit`, `reason`; with `--state running` it also writes `started_at` (when the stage changed), `last_progress` and `pids.runner = {"pid": P, "create_time": <psutil create time>}`. The list prints one line per run: `run_id source stage state unit units_done/units_total last_progress` plus the stall reason. `--stalled` prints only the stalled or crashed runs and exits 1 when there is one.
- [ ] **Step 3:** pass; ruff. **Step 4:** commit `feat(ops): status.py, run status files with a progress-based stall check`.

---

### Task 4: Status in the Python stages

**Files:**
- Modify: `src/episteme/ops/stop.py` (`stage_guard` creates the run status and its heartbeat), `src/episteme/data/parallel.py`, `src/episteme/data/load_articles.py`, `src/episteme/data/postgres_loader.py`, `src/episteme/data/graph_load.py`, `src/episteme/data/db/bulk_indexes.py`
- Test: `tests/ops/test_stage_status.py` and additions to the existing stage tests.

**Interfaces:**
- Consumes: Task 3 (`RunStatus`, `run_dir`, `set_current`, `current`); Task 2 (`stage_guard(..., lock_stage=...)`).
- Produces: inside every guarded stage, `status.current()` is the run's `RunStatus`.

Behaviour:
- `stage_guard`, when `lock_stage` is set: `run_id` = the one `stage_lock` yields; `st = RunStatus(run_dir(run_id))`; `st.update(stage=lock_stage, state="running", pids={"stage": {"pid": os.getpid(), "create_time": ...}})` (keep the runner entry if present); `set_current(st)`; start the heartbeat with a stop Event set in `finally` (PR 2's watchdog pattern); in `finally`, `set_current(NullStatus())`. The stage's own end state (`done`/`failed`/`stopped`) is written by `run_pipeline.sh` (Task 6) or, for a direct run, by `stage_guard`: `done` on a clean exit, `stopped` on 130/143, `failed` otherwise.
- `parallel.run_per_file`: after each result in the parent loop, `current().progress(unit=<source_file>, units_done=<n results>, units_total=<len(files)>)`.
- `load_articles`: per attempt `current().update(unit=name, backend_pid=conn.info.backend_pid)`; after each shard `progress(units_done=..., units_total=...)`.
- `postgres_loader.load_source_file`: in its batch loop, `current().progress(rows_done=<rows so far in this process>)` once per batch; wrap the id-replace `DELETE` phase in `current().long_statement("DELETE previous rows of <source_file>")`.
- `graph_load.build_pubmed_graph`: `progress` per file; wrap its per-file delete in `long_statement`.
- `bulk_indexes`: wrap each `DROP INDEX` / `CREATE INDEX` in `long_statement("CREATE INDEX <name>")`.

- [ ] **Step 1: failing tests:** a guarded fake stage writes `status.json` with `stage`, `state == "running"` and `pids.stage`; `run_per_file` over 3 files leaves `units_done == 3`, `units_total == 3`, `unit` = the last file; a fake `load_source_file` batch loop updates `rows_done`; a long statement shows in the file while its body runs (read it from inside the body); a direct run ends with `state == "done"`, a KeyboardInterrupt with `state == "stopped"`, an exception with `state == "failed"`; the heartbeat thread is gone after the guard exits.
- [ ] **Step 2:** implement. **Step 3:** run the stage tests listed in Task 2 Step 4 plus `tests/data/test_parallel*.py` and `tests/data/test_postgres_loader*.py` (not pg). **Step 4:** commit `feat(ops): the Python stages write run status and progress`.

---

### Task 5: Downloads — pause points and `failed_files.txt`

**Files:**
- Modify: `scripts/data/_lib/common.sh` (`http_fetch`, `_curl_fetch_all`), `src/episteme/data/pmc/download_pmc.py`, `src/episteme/config.py` (read `EPISTEME_RUN_DIR` into `run_dir`, if Task 3 did not)
- Test: `tests/test_http_fetch_failed_files.py`, `tests/data/test_download_pmc_pause.py`

**Interfaces:**
- Consumes: Task 1 CLI `python -m episteme.ops.lock pause-point --run-id R`; Task 3 CLI `status progress` and `long-begin`/`long-end`; env `EPISTEME_RUN_ID`, `EPISTEME_RUN_DIR` (exported by Task 6).
- Produces: `<run dir>/failed_files.txt` lines `<url><TAB><relpath><TAB><reason>`; `http_fetch` returns non-zero when any file failed.

Behaviour:
- `http_fetch` splits its URL list into batches of `EPISTEME_FETCH_BATCH` (default 64; a non-number logs a WARNING and uses 64). Before each batch, when `EPISTEME_RUN_ID` is set and this is not a dry run, it calls `"$PY" -m episteme.ops.lock pause-point --run-id "$EPISTEME_RUN_ID"` and `status progress --unit "batch <i>/<n>"`. Each `aria2c` call runs inside `status long-begin --label "aria2c batch <i>/<n>"` … `long-end`. `PY` is resolved the same way the wrappers resolve it; when no Python is found, the pause point and status calls are skipped with one WARNING.
- `_curl_fetch_all`: a file that still fails after `_curl_resume` goes into the failed list (not `die`), and the loop goes on. It returns 1 when any file failed.
- The failed list goes to `$EPISTEME_RUN_DIR/failed_files.txt` when that is set, else `$dest_dir/.failed_files.txt`. `http_fetch` logs `ERROR http_fetch: <n> file(s) failed; see <path>` and returns 1. A re-run fetches only the failed and missing files (the existing size-skip does this).
- `download_pmc.py`: before each submit batch (`SUBMIT_BATCH` loop), `lock.pause_point(settings.run_id)` when `settings.run_id` is set; each failed PMCID (not non-commercial) is appended to `settings.run_dir / "failed_files.txt"` as `<pmcid><TAB><reason>` when `run_dir` is set. The exit code is unchanged when nothing failed.
- `s5cmd`, `aws s3 sync` and `hf download` get no pause point (they cannot stop between files). The runbook says the heavy job waits for them (Task 8).

- [ ] **Step 1: failing tests:** bash harness (follow `tests/test_run_pipeline_bulk.py`): a fake `curl` that fails for one URL of three → 2 files fetched, `failed_files.txt` has the one line, `http_fetch` returns 1; with `EPISTEME_FETCH_BATCH=1` and a fake `PYTHON` that logs its arguments, `pause-point` is called once per batch; a dry run calls no pause point. Python: `download_pmc`'s batch loop with a patched `pause_point` is called once per submit batch; a failed PMCID lands in `failed_files.txt`.
- [ ] **Step 2:** implement. **Step 3:** run the new tests, `tests/test_common*.py` (find the http_fetch tests) and the PMC download tests. `bash -n` and shellcheck on `common.sh`. **Step 4:** commit `feat(download): pause points between batches; failed files are recorded and the download goes on`.

---

### Task 6: `run_pipeline.sh` — locks, run status, exit 75

**Files:**
- Modify: `scripts/data/run_pipeline.sh`, `scripts/data/db/migrate_database.sh`, `scripts/data/db/bulk_load_indexes.sh`
- Test: `tests/test_run_pipeline_lock_status.py` (fake stages, as in `tests/test_run_pipeline_skip_unit.py`)

**Interfaces:**
- Consumes: Task 1 CLI (`acquire`/`release`, exit 75), Task 3 CLI (`set`, `run-dir`).
- Produces: env `EPISTEME_RUN_DIR` for every stage; `--no-wait` option; exit 75.

Behaviour:
- After `RUN_ID` is set: `RUN_DIR=$("$PY" -m episteme.ops.status run-dir --run-id "$RUN_ID")`; `export EPISTEME_RUN_DIR="$RUN_DIR"`.
- `_lock_kind STAGE`: `download` → `download`; `extract|serialize|load|graph|enrich|materialize` → `heavy`; anything else → none.
- New option `--no-wait` (passed to `lock acquire`).
- A single-stage run (not `all`, not a dry run): take the lock **before** `run_start`. A refusal (exit 75) prints `refused: ...`, writes `status set --state refused`, and exits 75 with no audit row.
- `run_stage NAME ...`: when the lock for `NAME` is not already held by this run, acquire it; a refusal writes `run_end --reason "refused: heavy lock held by <holder run> (<holder stage>)"` (from the CLI's stderr), `status set --state refused`, and exits 75. Then `status set --state running --stage NAME --runner-pid "$EPISTEME_RUNNER_PID"`; run the stage; write `done`, `failed` or `stopped` (130/143) with the unit from `failure show`; release the lock (for `all`: after each stage). `run_stage` passes 75 through like 130/143, and maps every other non-zero code to 1 (unchanged).
- One EXIT handler: merge lock release and the `--bulk` rebuild into one `_on_exit` function (a second `trap ... EXIT` would replace the first). Order: bulk rebuild first (it needs the lock), then release the lock, then keep the exit code rules that exist today.
- A dry run takes no lock and writes no status.
- `bulk_load_indexes.sh` and `migrate_database.sh`: at start, `out=$("$PY" -m episteme.ops.lock acquire heavy --run-id "${EPISTEME_RUN_ID:-adhoc-$EPISTEME_RUNNER_PID}" --stage <script> --owner-pid "$EPISTEME_RUNNER_PID")` (exit 75 passes through); release in an EXIT trap only when `$out` is `acquired`. Under `run_pipeline.sh` the call is re-entrant (same run id and runner PID), so the runner's lock is not released early.
- Spec 8 test that PR 2 left open (drift-log backlog (j)): a fake stage that fails on its 2nd unit, then a re-run, resumes at that unit.

- [ ] **Step 1: failing tests** (fake stages log their arguments and env): a `load` run holds `heavy.lock` (the fake stage reads it) and the file is gone afterwards; a `download` run holds `download.lock`; with a live `heavy.lock` of another run, `--no-wait load` exits 75, writes no `run_start` (the fake audit log is empty), and status `refused`; `all` refused at its 2nd stage writes one `run_end` with `refused:` and exits 75; status.json shows `running` during the stage and `done` after; a fake stage that exits 143 leaves `stopped`; `--bulk` with a failing stage still rebuilds and then releases (both in the fake log, in that order); `bulk_load_indexes.sh` under the runner reports `reentrant` and does not delete the runner's lock; the resume test above.
- [ ] **Step 2:** implement. **Step 3:** run the new tests, `tests/test_run_pipeline_skip_unit.py`, `tests/test_run_pipeline_bulk.py`, and `tests/test_run_pipeline_dispatch.py` (slow: about 15 min; use `timeout 1500`). `bash -n`, shellcheck. **Step 4:** commit `feat(pipeline): run_pipeline.sh takes the stage lock and writes run status; --no-wait refuses with 75`.

---

### Task 7: `scripts/data/run_queue.sh`

**Files:**
- Create: `scripts/data/run_queue.sh`
- Test: `tests/test_run_queue.py`

**Interfaces:**
- Consumes: `run_pipeline.sh`; Task 3 CLI `queue-dir`.
- Produces: `run_queue.sh "<source>:<stage>[,<stage>...]" ...`; exit 0 when all steps are done, 1 when the queue stopped, 2 on a usage error.

Behaviour:
- Parse every argument first; a bad one (no `:`, empty source or stage) → usage, exit 2, nothing runs.
- Queue id `queue-<UTC timestamp>`; folder from `status queue-dir`; `queue.log` there, plus one log per step `NN-<source>-<stage>.log` (stdout and stderr both: `2>&1`).
- At start, record `git -C <repo> rev-parse HEAD` and a hash of `git -C <repo> status --porcelain -- scripts/data src`. Before each step, compare both; a change → `STOPPED at <source> <stage>: code changed since the queue started` and exit 1.
- Run each step as `bash "$HERE/run_pipeline.sh" <source> <stage>` (never `source` it). A step is done only when its exit code is 0 **and** its log has a line that matches `grep -qE '^[^ ]+ \[INFO\] pipeline done: <source> <stage>$'` (source and stage escaped for the regex). Otherwise write `STOPPED at <source> <stage> (rc=<rc>)` to stdout and `queue.log`, and exit 1.
- The queue takes no lock and writes no status file of its own; each step's `run_pipeline.sh` does.

- [ ] **Step 1: failing tests** (a fake `run_pipeline.sh` in a copied tree, as the dispatch tests do): 2 steps that both print the done line → exit 0 and 2 step logs; a step that exits 0 without the line → `STOPPED at` that step, the next step never runs; a step that prints `pipeline done: pubmed load` when the step is `pubmed graph` → stopped (exact match); a step that exits 1 → stopped with `rc=1`; a code change between steps (the fake step 1 touches a file under the copied `scripts/data` in a temp git repo) → `code changed`; a bad argument → exit 2 and no step runs.
- [ ] **Step 2:** implement. **Step 3:** pass; `bash -n`, shellcheck; the URL guard. **Step 4:** commit `feat(pipeline): run_queue.sh runs steps in order and stops at the first unfinished step`.

---

### Task 8: Docs

**Files:** `docs/10-data-sources-runbook.md`, `docs/project-incubation-baseline.md`.

- [ ] **Step 1: runbook**, a new section "Locks, status and the queue", in plain direct English (short sentences, one action per step):
  - which stages and scripts take the heavy lock; the download lock; waiting; `--no-wait` and exit 75 (add 75 to the exit-code table and to `run_stage`'s pass-through list);
  - the lock files under `<data root>/_ops/locks/` (`EPISTEME_LOCK_DIR` overrides), the stale rule (PID plus create time), the takeover WARNING, and `python -m episteme.ops.lock status`;
  - how to clear a lock by hand: only after `lock status` shows it stale, or the owner process is confirmed gone;
  - the advisory lock key `4994572608998821189`, per database;
  - download pauses: where they happen (`http_fetch` batches, `download_pmc.py` submit batches) and where they do not (`s5cmd`, `aws`, `hf download`: the heavy job waits);
  - `failed_files.txt` and the re-run;
  - status files: path, fields, `python -m episteme.ops.status`, `--stalled` rules (10 min, 60 min in a long statement, crashed);
  - `run_queue.sh`: usage, the done rule (exact line), `STOPPED at`, the code-changed stop, where the logs are.
- [ ] **Step 2: drift log**, an entry for operating standard PR 3: what landed; the decisions (lock folder at `<data root>/_ops/locks`; advisory key; lock per stage; the scripts that take the lock; no pause point for `s5cmd`/`aws`/HF; queue code-changed guard); backlog: a live but wrong inherited `EPISTEME_RUNNER_PID` is not detected (PR 2 backlog (s) applies to locks too); `enrich` stages take the lock but write no unit progress yet; spec 6.4 bounded parts stay sub-project 2. Close PR 2 backlog (a) (downloads: done here) and (j) (the resume test: Task 6).
- [ ] **Step 3:** `ruff format --check .` (Markdown Python blocks), the full `timeout 1500 .venv/Scripts/python -m pytest -q -m "not pg"`, `ruff check .`. Commit `docs: locks, status and the queue (operating standard PR 3)`.

---

## Rollout note

Do not pull this branch into the main checkout while a pipeline run is in progress there. Merge between runs. After the merge, the first run creates `<data root>/_ops/locks/` and `<processed root>/_ops/runs/`.

## Open decisions for the user (plan review)

1. Lock folder: the spec's `<data root>/_ops/locks/`, with `EPISTEME_LOCK_DIR` as an override (default), or `<processed root>/_ops/locks/`.
2. Spec 6.3 (`failed_files.txt`, record and continue) is in this PR (Task 5), because PR 2 deferred downloads here (default), or it moves to a later PR.
3. Lock scope for `all`: per stage, so a waiting job may run between two stages of an `all` chain (default), or held across the whole chain.
4. The scripts that take the heavy lock outside `run_pipeline.sh`: `migrate_database.sh` and `bulk_load_indexes.sh` (default). There are no separate purge scripts today.
