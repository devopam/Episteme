"""Run status files (operating standard spec 6.6).

One file per run: ``<processed root>/_ops/runs/<run_id>/status.json``. The
heartbeat thread writes ``heartbeat`` (the process is alive); the work writes
``last_progress`` (a unit, a batch or a phase ended). The stall check reads
``last_progress``, so a run stuck in one statement is found even while its
heartbeat thread runs.

Limits: the read-modify-write of ``update`` is serialized only inside one
process. A heartbeat that interleaves with a bash ``set``, ``long-begin`` or
``long-end`` can lose that change (a window of milliseconds every 30 s). A lost
``long-begin`` can give a false stall after 10 min. ``heartbeat`` re-reads right
before its write and changes only the ``heartbeat`` key, to keep the window
small. A status write that fails (Windows: a reader holds the file) is retried
a few times, then logged and dropped: a lost write never stops a stage.
Because of that, ``set``, ``progress``, ``long-begin`` and ``long-end`` exit 0
even when the write was dropped; the WARNING in the log (at most one per file
per 60 s) is the only signal. ``update`` never writes from a failed read: it
skips the write, so an existing record is not overwritten by a near-empty one.
A damaged file is repaired from ``{}`` with a WARNING.
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

from episteme.ops.lock import retry_io

_LOG = logging.getLogger(__name__)

FILE = "status.json"
HEARTBEAT_SEC = 30.0
STALL_SEC = 600.0
PROGRESS_MIN_INTERVAL_SEC = 2.0
LONG_STALL_SEC = 3600.0
STATES = ("running", "done", "failed", "stopped", "refused")
_TERMINAL = {"done", "failed", "stopped", "refused"}


_WARN_EVERY_SEC = 60.0
_LAST_WARN: dict[tuple[str, str], float] = {}


def _warn(kind: str, path: Path, exc: BaseException) -> None:
    """Log a status I/O failure, at most once per file and kind per 60 s."""
    key = (kind, str(path))
    now = time.monotonic()
    last = _LAST_WARN.get(key)
    if last is not None and now - last < _WARN_EVERY_SEC:
        return
    _LAST_WARN[key] = now
    _LOG.warning("could not %s %s: %r", kind, path, exc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


now_iso = _now  # public: the stage guard stamps ``started_at`` with it


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
        self._clock = time.monotonic  # seam: tests patch this
        self._last_write: float | None = None
        self._held: dict[str, Any] = {}

    def _load(self) -> tuple[dict[str, Any], bool]:
        """(record, ok). ok is False only when the file exists but cannot be read."""
        try:
            rec = json.loads(retry_io(lambda: self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return {}, True
        except ValueError as exc:
            _warn("repair", self.path, exc)  # damaged: the caller rewrites it
            return {}, True
        except OSError as exc:
            _warn("read", self.path, exc)
            return {}, False
        if not isinstance(rec, dict):
            _warn("repair", self.path, ValueError("not a JSON object"))
            return {}, True
        return rec, True

    def read(self) -> dict[str, Any]:
        return self._load()[0]

    def update(self, **fields: Any) -> dict[str, Any]:
        with self._mutex:
            rec, ok = self._load()
            rec.update(self._held)  # progress held back by the throttle
            self._held = {}
            rec.update(fields)
            rec["updated_at"] = _now()
            if not ok:  # never overwrite an existing record from a failed read
                return rec
            tmp = self.path.with_name(f".{FILE}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
                retry_io(os.replace, tmp, self.path)
            except OSError as exc:  # a lost status write must never stop a stage
                _warn("write", self.path, exc)
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return rec

    def heartbeat(self) -> dict[str, Any]:
        # update() re-reads right before the write and changes only this key.
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
        elapsed = self._clock() - self._t0
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
        # Throttle: at most one write per PROGRESS_MIN_INTERVAL_SEC. The latest
        # fields wait in memory for the next allowed call. Always write the first
        # call, the last unit (done == total) and a new unit after the interval.
        mono = self._clock()
        with self._mutex:
            self._held.update(fields)
            age = None if self._last_write is None else mono - self._last_write
            final = units_done is not None and units_done == units_total
            due = age is None or age >= PROGRESS_MIN_INTERVAL_SEC
            if not (final or due):
                return dict(self._held)
            pending, self._held = self._held, {}
            self._last_write = mono
        return self.update(**pending)

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

    def start_heartbeat(
        self, stop: threading.Event, interval: float = HEARTBEAT_SEC
    ) -> threading.Thread:
        t = threading.Thread(target=lambda: None, name="status-heartbeat-null", daemon=True)
        t.start()
        return t


_CURRENT: RunStatus | NullStatus = NullStatus()


def current() -> RunStatus | NullStatus:
    return _CURRENT


def set_current(st: RunStatus | NullStatus) -> None:
    global _CURRENT
    _CURRENT = st


def _age(ts: str, now: datetime) -> float:
    then = datetime.fromisoformat(ts)
    if then.tzinfo is None:  # a naive timestamp counts as UTC
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds()


def stalled(rec: dict[str, Any], now: datetime | None = None) -> str | None:
    if rec.get("state") in _TERMINAL:
        return None
    from episteme.ops.lock import owner_alive

    now = now or datetime.now(timezone.utc)
    pids = rec.get("pids") or {}
    runner = pids.get("runner")
    if isinstance(runner, dict):
        if not owner_alive(runner):
            return "crashed: the runner process is gone"
    else:  # a direct run (no run_pipeline.sh): the stage process is the owner
        stage = pids.get("stage")
        if isinstance(stage, dict) and not owner_alive(stage):
            return "crashed: the stage process is gone"
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


def _runner_record(pid: int) -> dict[str, Any]:
    from episteme.ops.lock import create_time

    return {"pid": pid, "create_time": create_time(pid)}


def _line(run_id: str, rec: dict[str, Any], reason: str | None) -> str:
    done, total = rec.get("units_done"), rec.get("units_total")
    parts = [
        run_id,
        str(rec.get("source", "-")),
        str(rec.get("stage", "-")),
        str(rec.get("state", "-")),
        str(rec.get("unit") or "-"),
        f"{done if done is not None else '-'}/{total if total is not None else '-'}",
        str(rec.get("last_progress") or "-"),
    ]
    if reason:
        parts.append(reason)
    return " ".join(parts)


def _cmd_set(args: argparse.Namespace) -> int:
    st = RunStatus(run_dir(args.run_id))
    old = st.read()
    fields: dict[str, Any] = {
        "run_id": args.run_id,
        "source": args.source,
        "stage": args.stage,
        "state": args.state,
        "unit": args.unit,
        "reason": args.reason,
    }
    if args.state == "running":
        now = _now()
        if old.get("stage") != args.stage or not old.get("started_at"):
            fields["started_at"] = now
        fields["last_progress"] = now
        pids = dict(old.get("pids") or {})
        pids["runner"] = _runner_record(args.runner_pid or os.getpid())
        fields["pids"] = pids
    st.update(**fields)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m episteme.ops.status",
        epilog="set, progress, long-begin and long-end exit 0 even when a status write "
        "was dropped (a status write never stops a stage); the WARNING in the log is "
        "the only signal.",
    )
    ap.add_argument("--stalled", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("set")
    p.add_argument("--run-id", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--stage", required=True)
    p.add_argument("--state", required=True, choices=STATES)
    p.add_argument("--unit", default=None)
    p.add_argument("--reason", default=None)
    p.add_argument("--runner-pid", type=int, default=None)
    p = sub.add_parser("progress")
    p.add_argument("--run-id", required=True)
    p.add_argument("--unit", default=None)
    p = sub.add_parser("long-begin")
    p.add_argument("--run-id", required=True)
    p.add_argument("--label", required=True)
    p = sub.add_parser("long-end")
    p.add_argument("--run-id", required=True)
    p = sub.add_parser("run-dir")
    p.add_argument("--run-id", required=True)
    p = sub.add_parser("queue-dir")
    p.add_argument("--queue-id", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "set":
        return _cmd_set(args)
    if args.cmd == "progress":
        st = RunStatus(run_dir(args.run_id))
        if not st.path.is_file():
            print(f"error: no status file for run {args.run_id}", file=sys.stderr)
            return 2
        st.progress(unit=args.unit)
        return 0
    if args.cmd == "long-begin":
        RunStatus(run_dir(args.run_id)).update(
            long_statement={"label": args.label, "since": _now()}
        )
        return 0
    if args.cmd == "long-end":
        RunStatus(run_dir(args.run_id)).update(long_statement=None, last_progress=_now())
        return 0
    if args.cmd == "run-dir":
        print(run_dir(args.run_id))
        return 0
    if args.cmd == "queue-dir":
        from episteme.config import get_settings

        print(get_settings().processed_root / "_ops" / "queue" / args.queue_id)
        return 0

    found = False
    for run_id, rec in list_runs():
        if not rec:  # a damaged or half-written file: skip it, never crash
            print(f"warning: skipped unreadable status file of run {run_id}", file=sys.stderr)
            continue
        try:
            reason = stalled(rec)
        except (ValueError, TypeError) as exc:  # a bad timestamp
            print(f"warning: skipped run {run_id}: {exc!r}", file=sys.stderr)
            continue
        if args.stalled:
            if reason:
                found = True
                print(_line(run_id, rec, reason))
        else:
            print(_line(run_id, rec, reason))
    return 1 if (args.stalled and found) else 0


if __name__ == "__main__":
    sys.exit(main())
