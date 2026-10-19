# Ops retry and fail fast (PR 2 of the operating standard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every processing stage (extract, serialize, load, graph) stops at the first permanent failure of a unit, retries transient errors 3 times (30, 60, 120 s), names the failed unit in the audit `run_end`, and leaves no PostgreSQL statement running after its process stops or is killed.

**Architecture:** `ops/retry.py` classifies an error (SQLSTATE and errno allowlists) and retries a callable. A retry is always the whole unit transaction on a fresh connection, with the audit mirror deferred until commit. `ops/stop.py` turns SIGTERM/SIGBREAK into KeyboardInterrupt (psycopg then cancels the statement) and runs a parent-death watchdog. Connections carry `application_name = episteme:<run_id>:<pid>`, so `ops/orphans.py` can cancel backends whose client process is gone. `ops/failure.py` hands the failed unit from Python to `run_pipeline.sh`. The per-file runner (`parallel.run_per_file`) gains stop-on-failure, transient retry and a thread mode, and the serializers use it in thread mode (their thread model stays).

**Tech Stack:** Python 3.13, psycopg 3 (`psycopg.errors`, `Connection.cancel_safe`), psutil, pytest (`pg` marker for DB tests), bash.

**Spec:** `docs/superpowers/specs/2026-10-19-pipeline-operating-standard-design.md` (sections 6.2, 6.4, 8, 9 PR 2).

## Global Constraints

- Fail fast applies to extract, serialize, load and graph. A permanent error in a unit stops the stage; `run_pipeline.sh` exits non-zero; the audit `run_end` reason names the unit and the error (spec 6.2).
- Transient errors are retried 3 times, after 30 s, 60 s and 120 s (4 attempts in all), then treated as permanent (spec 6.2).
- Transient = a lost or refused connection, a timeout, a deadlock, a serialization failure, an OS-level I/O error. Classified by SQLSTATE and errno/winerror allowlists, never by message text or by exception class alone. `57014 query_canceled` is permanent. `FileNotFoundError`, `PermissionError`, `IsADirectoryError`, `NotADirectoryError` are permanent. `BaseException` subclasses that are not `Exception` (KeyboardInterrupt, SystemExit) are never caught by the retry.
- A retry is the whole unit transaction on a fresh connection. Audit records written in a failed attempt must not survive in the table or the JSONL mirror.
- A stage that is stopped cancels its running statement. A stage whose parent process dies cancels its statement and exits. A backend left by a killed client is cancelled by the next stage that starts.
- Downloads are out of scope for this PR (spec 6.3 keeps record-and-continue; PR 3).
- Units that already succeeded keep their success markers; a re-run resumes at the failed unit (spec 6.4).
- `config.py` is the only module that reads `os.environ`. Endpoint URLs only in `scripts/data/_lib/sources.env`. `git add` explicit paths only.
- Tests are `not pg` unless they need the database; `pg` tests use `PGDATABASE=episteme_test`.
- CI runs `ruff format --check .` with ruff 0.17 over the whole repo, including Python code blocks in Markdown; run it before each commit.

## Review Focus

1. **A permanently bad input blocks its source forever.** With fail fast plus resume, a corrupt file stops every re-run at the same unit. Expected: an audited escape, `--skip-unit NAME --reason TEXT`, that marks the unit skipped with a `unit_skip` audit row and lets the run go on. Test in Task 5 and Task 7. **Decision (user, 2026-10-19): in.**
2. **A retried load and the audit trail.** A load fails after `force_override` and `load_commit` were recorded, rolls back, and retries. Expected: the table and the JSONL mirror hold exactly the rows of the successful attempt; `verify()` passes. Test in Task 2 (`pg`).
3. **A hard kill of the runner.** On Windows, killing the bash parent does not kill `python.exe`. Expected: the watchdog sees the parent gone within 10 s, cancels the statement and exits; if the client itself was killed, the next stage cancels the orphan backend at start. Tests in Task 3 (`pg` for the cancel).
4. **Deliberate stop vs transient.** Ctrl-C or an orphan sweep cancels a statement (`57014`). Expected: no retry; the stage exits at once with 130 (INT) or 143 (TERM). Test in Task 1 and Task 3.
5. **A worker process dies (`BrokenProcessPool`).** Expected: permanent; pending units are cancelled, units in flight finish and write their markers, the stage exits non-zero naming the first failed unit. Test in Task 6.

---

## File Structure

- Create `src/episteme/ops/retry.py`, `src/episteme/ops/stop.py`, `src/episteme/ops/orphans.py`, `src/episteme/ops/failure.py`.
- Modify `src/episteme/audit_trail.py` (deferred mirror), `src/episteme/data/db/connection.py` (application_name), `src/episteme/data/load_articles.py`, `src/episteme/data/graph_load.py`, `src/episteme/data/graph_builder.py`, `src/episteme/data/parallel.py`, every extractor's and serializer's `process_one`/run loop, `scripts/data/run_pipeline.sh`.
- Tests: `tests/ops/test_retry.py`, `tests/ops/test_stop.py`, `tests/ops/test_orphans.py`, `tests/ops/test_failure.py`, `tests/test_audit_deferred_mirror.py`, plus the existing per-stage tests that assert continue-on-failure (listed in each task).
- Docs: `docs/10-data-sources-runbook.md`, `docs/11-gxp-data-integrity.md` (deferred mirror), `docs/project-incubation-baseline.md`.

---

### Task 1: `ops/retry.py`

**Files:** Create `src/episteme/ops/retry.py`; Test `tests/ops/test_retry.py`.

**Interfaces — Produces:**
`is_transient(exc: BaseException) -> bool`; `class TransientUnitError(Exception)` (picklable; carries `unit: str`, `cause: str`); `RETRY_WAITS: tuple[int, ...] = (30, 60, 120)`; `with_retries(fn: Callable[[], T], *, waits: Sequence[float] = RETRY_WAITS, sleep: Callable[[float], None] = time.sleep, on_retry: Callable[[int, BaseException, float], None] | None = None) -> T`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/test_retry.py
import errno

import psycopg
import pytest
from psycopg import errors as pgerr

from episteme.ops import retry


def _pg(cls):
    return cls("x")


@pytest.mark.parametrize(
    "exc",
    [
        _pg(pgerr.DeadlockDetected),  # 40P01
        _pg(pgerr.SerializationFailure),  # 40001
        _pg(pgerr.LockNotAvailable),  # 55P03
        _pg(pgerr.AdminShutdown),  # 57P01
        _pg(pgerr.CannotConnectNow),  # 57P03
        _pg(pgerr.TooManyConnections),  # 53300
        psycopg.OperationalError("server closed the connection unexpectedly"),  # no sqlstate
        TimeoutError("t"),
        ConnectionResetError("r"),
        OSError(errno.EIO, "io"),
        OSError(errno.ETIMEDOUT, "t"),
        retry.TransientUnitError("u", "c"),
    ],
)
def test_transient(exc):
    assert retry.is_transient(exc)


@pytest.mark.parametrize(
    "exc",
    [
        _pg(pgerr.QueryCanceled),  # 57014: our own stop or an orphan sweep
        _pg(pgerr.UniqueViolation),
        _pg(pgerr.UndefinedTable),
        _pg(pgerr.InsufficientPrivilege),
        FileNotFoundError(errno.ENOENT, "f"),
        PermissionError(errno.EACCES, "p"),
        IsADirectoryError(errno.EISDIR, "d"),
        OSError(errno.ENOSPC, "full"),
        ValueError("bad"),
        KeyError("k"),
    ],
)
def test_permanent(exc):
    assert not retry.is_transient(exc)


def test_windows_winerror_allowlist():
    e = OSError(None, "device not ready")
    e.winerror = 21
    assert retry.is_transient(e)
    e2 = OSError(None, "other")
    e2.winerror = 5  # access denied
    assert not retry.is_transient(e2)


def test_with_retries_retries_transient_then_succeeds():
    calls, slept = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise pgerr.DeadlockDetected("d")
        return "ok"

    assert retry.with_retries(fn, sleep=slept.append) == "ok"
    assert len(calls) == 3 and slept == [30, 60]


def test_with_retries_gives_up_after_three_retries():
    calls, slept = [], []

    def fn():
        calls.append(1)
        raise TimeoutError("t")

    with pytest.raises(TimeoutError):
        retry.with_retries(fn, sleep=slept.append)
    assert len(calls) == 4 and slept == [30, 60, 120]


def test_with_retries_does_not_retry_permanent():
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("v")

    with pytest.raises(ValueError):
        retry.with_retries(fn, sleep=lambda s: None)
    assert len(calls) == 1


def test_with_retries_never_catches_keyboard_interrupt():
    def fn():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        retry.with_retries(fn, sleep=lambda s: None)


def test_on_retry_is_called_with_attempt_error_and_wait():
    seen = []

    def fn():
        if not seen:
            raise TimeoutError("t")
        return 1

    retry.with_retries(
        fn, sleep=lambda s: None, on_retry=lambda n, e, w: seen.append((n, type(e), w))
    )
    assert seen == [(1, TimeoutError, 30)]


def test_transient_unit_error_pickles():
    import pickle

    e = pickle.loads(pickle.dumps(retry.TransientUnitError("a.xml", "OSError(5)")))
    assert (e.unit, e.cause) == ("a.xml", "OSError(5)")
```

- [ ] **Step 2: Run to see them fail.** `.venv/Scripts/python -m pytest -q tests/ops/test_retry.py` → FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# src/episteme/ops/retry.py
"""Error classification and retry for the processing stages (operating
standard section 6.2). A transient error is retried 3 times, after 30, 60 and
120 s; anything else, and a transient error after the last retry, is permanent.

Classification uses SQLSTATE codes and errno/winerror values, never message
text. ``57014 query_canceled`` is permanent: it is what a deliberate stop or an
orphan sweep produces. Only ``Exception`` subclasses are retried, so
KeyboardInterrupt and SystemExit always propagate.
"""

from __future__ import annotations

import errno
import logging
import time
from collections.abc import Callable, Sequence
from typing import TypeVar

import psycopg

_LOG = logging.getLogger(__name__)
T = TypeVar("T")

RETRY_WAITS: tuple[int, ...] = (30, 60, 120)

# Connection exceptions (08xxx), serialization/deadlock (40001, 40P01),
# lock not available (55P03), operator intervention except query_canceled
# (57P01-57P05), too many connections (53300).
_TRANSIENT_SQLSTATE_PREFIXES = ("08",)
_TRANSIENT_SQLSTATES = frozenset(
    {"40001", "40P01", "55P03", "57P01", "57P02", "57P03", "57P04", "57P05", "53300"}
)
_TRANSIENT_ERRNOS = frozenset(
    {
        errno.EIO,
        errno.ETIMEDOUT,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.EPIPE,
        errno.EAGAIN,
    }
)
# Windows: 21 device not ready, 64 network name no longer available,
# 121 semaphore timeout, 1117 I/O device error, 1231 network unreachable.
_TRANSIENT_WINERRORS = frozenset({21, 64, 121, 1117, 1231})
_PERMANENT_OSERRORS = (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)


class TransientUnitError(Exception):
    """A unit failed with a transient error inside a worker. Carries text only,
    so it pickles across a process pool without losing its meaning."""

    def __init__(self, unit: str, cause: str) -> None:
        super().__init__(unit, cause)
        self.unit = unit
        self.cause = cause

    def __str__(self) -> str:
        return f"transient error in {self.unit}: {self.cause}"


def is_transient(exc: BaseException) -> bool:
    if not isinstance(exc, Exception):
        return False
    if isinstance(exc, TransientUnitError):
        return True
    if isinstance(exc, psycopg.Error):
        state = getattr(exc, "sqlstate", None)
        if state is None:
            # No SQLSTATE: a client-side connection failure (lost or refused).
            return isinstance(exc, psycopg.OperationalError)
        return state in _TRANSIENT_SQLSTATES or state.startswith(_TRANSIENT_SQLSTATE_PREFIXES)
    if isinstance(exc, _PERMANENT_OSERRORS):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS:
            return True
        return exc.errno in _TRANSIENT_ERRNOS
    return False


def with_retries(
    fn: Callable[[], T],
    *,
    waits: Sequence[float] = RETRY_WAITS,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= len(waits) or not is_transient(exc):
                raise
            wait = waits[attempt]
            attempt += 1
            if on_retry is not None:
                on_retry(attempt, exc, wait)
            else:
                _LOG.warning(
                    "transient error (retry %d/%d in %ss): %r", attempt, len(waits), wait, exc
                )
            sleep(wait)
```

- [ ] **Step 4: Run to see them pass.** Same command → all pass. Confirm each `psycopg.errors` class name used in the test exists in the installed psycopg (adjust the name, not the SQLSTATE, if one differs; say so in the report).
- [ ] **Step 5: Commit** `git add src/episteme/ops/retry.py tests/ops/test_retry.py` — `feat(ops): retry.py classifies errors by SQLSTATE/errno and retries transient ones`.

---

### Task 2: Deferred audit mirror

**Files:** Modify `src/episteme/audit_trail.py`; Test `tests/test_audit_deferred_mirror.py`; Docs `docs/11-gxp-data-integrity.md` (one paragraph).

**Interfaces — Produces:** `audit_trail.deferred_mirror(conn)` (context manager); inside it `record()` inserts the row as today but queues the mirror line on the connection instead of appending it; `audit_trail.commit(conn)` commits and then appends the queued lines (under the existing mirror lock); `audit_trail.rollback(conn)` rolls back and drops them. Outside `deferred_mirror`, behaviour is unchanged.

Why: today `record()` appends the mirror line after the INSERT but before the caller commits, so any rolled-back transaction leaves a mirror line with no table row, and `verify()`'s line-count cross-check then fails. Retries (Tasks 4–5) would make this frequent.

- [ ] **Step 1: Failing tests.** Not-pg: monkeypatch the INSERT path (follow the existing audit tests' fake-connection pattern; read `tests/` for how `record()` is unit-tested) and assert: inside `deferred_mirror`, after `record()` the mirror file has no new line; after `audit_trail.commit(conn)` it has exactly one; after `audit_trail.rollback(conn)` it has none and the queue is empty; leaving the context with queued lines and no commit/rollback raises `RuntimeError("deferred audit mirror lines were neither committed nor rolled back")` and drops them; leaving the context by an exception drops the queued lines and lets that exception propagate (no RuntimeError). `pg` test: `with deferred_mirror(conn)`: record two events, rollback, record one, commit; `verify()` passes and the mirror has exactly one new line whose `record_hash` matches the table row.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement.** Store the queue in a `weakref.WeakKeyDictionary[connection, list[dict]]` (connections are not always attribute-assignable); `record()` checks whether `conn` is in the dictionary. Keep the mirror-after-INSERT ordering for the non-deferred path. Nesting `deferred_mirror` on the same connection is an error (`RuntimeError`).
- [ ] **Step 4:** pass; also run the existing audit tests.
- [ ] **Step 5:** docs/11: one paragraph — "a stage that retries a transaction defers the mirror append until commit, so the mirror never holds a line for a rolled-back row". Commit.

---

### Task 3: Stop handling, connection tags, orphan sweep

**Files:** Create `src/episteme/ops/stop.py`, `src/episteme/ops/orphans.py`; Modify `src/episteme/data/db/connection.py`; Tests `tests/ops/test_stop.py`, `tests/ops/test_orphans.py`.

**Interfaces — Produces:**
- `connection.py`: every connection it opens sets `application_name` to `episteme:<run_id>:<pid>` (run id from `get_settings().run_id`, or `adhoc`; truncated to 63 bytes, run id shortened first so `:<pid>` always survives). Use the psycopg `application_name` connect kwarg / pool `kwargs`.
- `ops/stop.install_stop_handlers() -> None`: SIGTERM, and SIGBREAK on Windows, raise `KeyboardInterrupt` in the main thread (`signal.default_int_handler`). Idempotent.
- `ops/stop.start_parent_watchdog(cancel: Callable[[], None], *, interval: float = 10.0, parent_pid: int | None = None) -> threading.Thread`: a daemon thread; when `psutil.pid_exists(parent_pid or os.getppid())` turns false it calls `cancel()` (swallowing its errors), logs, and calls `os._exit(143)`.
- `ops/stop.stage_guard(conn_ref: Callable[[], psycopg.Connection | None])`: context manager used by each stage main: installs the handlers, starts the watchdog with `cancel = lambda: (c := conn_ref()) and c.cancel_safe()`, and maps `KeyboardInterrupt` to exit code 130 (INT) or 143 (TERM) via `SystemExit`.
- `ops/orphans.sweep(conn) -> list[int]`: selects `pid, application_name` from `pg_stat_activity` where `application_name LIKE 'episteme:%'`, `usename = current_user`, `pid <> pg_backend_pid()` and `state <> 'idle'`; parses the client PID (last `:` field); for each whose client PID does not exist on this machine (`psutil.pid_exists`), runs `pg_cancel_backend(pid)`, records one `orphan_cancel` audit row (object = application_name, reason = query start time), and returns the cancelled PIDs. A row whose application_name does not parse is left alone and logged.

- [ ] **Step 1: Failing tests.** Not-pg: application_name format and truncation; `install_stop_handlers` maps SIGTERM → KeyboardInterrupt (call the installed handler directly); watchdog with a fake `pid_exists` that flips false calls `cancel` once and `os._exit(143)` (monkeypatch `os._exit` to raise a sentinel); `sweep` with a fake cursor returning rows (alive pid, dead pid, unparsable) cancels only the dead one. `pg`: (a) start `SELECT pg_sleep(30)` on one connection in a thread, raise KeyboardInterrupt in that thread's wait via `conn.cancel_safe()` from another thread — the query ends with `QueryCanceled` within 5 s (this pins psycopg's cancel behaviour on this server); (b) open a connection whose application_name carries a PID that does not exist, run `pg_sleep(60)` in a thread, call `sweep(conn2)` → the PID is returned and the sleep ends with `QueryCanceled`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** pass. **Step 5:** commit.

---

### Task 4: Failure handoff (`ops/failure.py`) and `run_pipeline.sh`

**Files:** Create `src/episteme/ops/failure.py`; Modify `scripts/data/run_pipeline.sh`; Tests `tests/ops/test_failure.py`, the existing `tests/test_run_pipeline_dispatch.py` harness.

**Interfaces — Produces:**
- `failure.record_failure(source: str, stage: str, unit: str, error: str, *, run_id: str | None = None) -> Path` writes `<processed root>/_ops/<source>/last_failure.json` (`run_id`, `stage`, `unit`, `error` truncated to 500 chars, `at` UTC).
- `failure.clear(source)`; `python -m episteme.ops.failure show --source S --run-id R` prints `<unit>: <error>` when the file's run id matches, else nothing, exit 0.
- `run_pipeline.sh`: `run_stage` clears the record before a stage; on failure it reads `failure show` and writes `run_end --reason "failed at stage <name>: <unit>: <error>"` (or the old text when nothing was recorded); maps exit codes 130/143 to "stopped at stage <name>". It exits with the stage's code.

- [ ] Steps: failing tests (record/show round trip, run-id mismatch prints nothing; bash: a fake stage that writes a failure record and exits 1 makes the audit `run_end` reason contain the unit — use the existing fake-python/fake-audit pattern of the dispatch tests), implement, pass, commit. Do not touch any run-pipeline copy outside this worktree.

---

### Task 5: Load fails fast

**Files:** Modify `src/episteme/data/load_articles.py`; Tests `tests/data/test_load_articles*.py` (find the files that assert a failed shard is reported and the loop continues; flip them).

Behaviour:
- `main` runs inside `stage_guard`; at start, after the run bracket, it calls `orphans.sweep(conn)`.
- Each shard is one unit: `with_retries(lambda: _load_one(shard))`, where `_load_one` takes a **fresh connection** from `connection()`, enters `deferred_mirror(conn)`, writes `force_override` (when forced), runs `load_source_file`, and ends with `audit_trail.commit(conn)`; any exception → `audit_trail.rollback(conn)` and re-raise.
- On a permanent failure: print `FAIL <name>: <error>`, `record_failure(source, "load", name, repr(exc))`, write the run bracket's `run_end` with the unit in the reason when it owns the bracket, print `done shards=<n> failed=1 stopped_at=<name>`, return 1. Shards after it are not touched.
- `--skip-unit NAME` (repeatable) with `--reason TEXT` (required with it): the named shard gets one `unit_skip` audit row and is not loaded; the summary counts it as `skipped_by_operator`. (Review Focus 1 — in, user decision 2026-10-19.)
- Create `src/episteme/ops/skip.py`, shared by Tasks 5–7: `add_skip_args(ap)` adds the repeatable `--skip-unit NAME` (and `--reason` only when the parser has none; `load_articles` already has `--reason`, so one reason covers `force_override` and `unit_skip`); `check_skip_args(ap, args)` calls `ap.error(...)` (exit 2) when `--skip-unit` comes without `--reason`; `apply_skips(source, stage, unit_names, skip_names, reason, *, run_id) -> list[str]` raises `SystemExit(2)` with a message when a skip name matches no unit, else writes one `unit_skip` audit row per skipped unit (object `f"{source} {name}"`, reason, run_id) on its own fresh connection, commits, and returns the remaining unit names. The unit name is the name `record_failure` writes (load: the shard file name). Test it in `tests/ops/test_skip.py`.

Tests to flip: every test that loads several shards with one failing and asserts later shards loaded. New expectations: later shards are not loaded; exit 1; `last_failure.json` names the shard; a transient error on the first attempt then success loads the shard once and writes one `load_commit` row (fake `load_source_file`, `sleep` patched to no-op); `--skip-unit` without `--reason` exits 2.

- [ ] Steps: failing tests, implement, pass (`tests/data` load tests + `pg` load tests if available locally), commit.

---

### Task 6: Per-file runner fails fast; extractors use it

**Files:** Modify `src/episteme/data/parallel.py`; every extractor's `process_one` except block (`apollo`, `bookshelf`, `europepmc` manuscripts and preprints, `guidelines`, `pmc`, `pubmed`, `openalex` serializer); Tests `tests/data/test_parallel*.py` and the extractor tests that assert continue-on-failure.

Behaviour:
- `run_per_file(fn, files, *, workers, executor="process", stop_on_failure=True, waits=RETRY_WAITS, sleep=time.sleep, on_result=None, **kwargs)`.
  - `executor="thread"` uses a `ThreadPoolExecutor` (for the serializers, Task 7).
  - A call that raises `TransientUnitError` is resubmitted after `waits[i]`, up to `len(waits)` times; then it becomes a failed result.
  - With `stop_on_failure`, the first failed result (not `skipped`) stops submission: pending futures are cancelled (`shutdown(cancel_futures=True)`), units in flight finish and their results are still emitted, and the returned list ends with them. `BrokenProcessPool` is permanent.
  - The result list gains no new keys; callers find the first failure with `next(r for r in results if not r.get("ok") and not r.get("skipped"))`.
- Each `process_one` keeps its current `except Exception as e:` block, and first does `if is_transient(e): raise TransientUnitError(<basename>, repr(e)) from None` — classification happens in the worker, where the real exception exists.
- Each extractor `run()`/`main()`: after `run_per_file`, when a unit failed, `record_failure(SOURCE, "extract", unit, error)` and return 1 (they already return 1 on any failure); `--skip-unit NAME --reason TEXT` as in Task 5 (filter the file list with `ops.skip.apply_skips` from Task 5 before `run_per_file`; the unit name is the file basename; `run_per_file` gets no skip parameter).
- Update the `parallel.py` module docstring: it now stops at the first failure.

Tests to flip: `parallel` tests that assert all files run after one fails; extractor tests asserting `failed == 1` together with later `ok` results. New: with 1 worker and a failing second file, the third file is never called; with 2 workers, pending files are cancelled; a `TransientUnitError` twice then success → one ok result, `sleep` called with 30, 60; BrokenProcessPool → failed, no retry.

- [ ] Steps: failing tests, implement, pass, commit (one commit for `parallel.py`, one for the extractors).

---

### Task 7: Serializers fail fast (thread model kept)

**Files:** the serializers' run loops: `cdisc_ct`, `chembl`, `clinvar`, `mesh`, `ontologies`, `pubchem`, `reactome`, `uniprot` (find each `ThreadPoolExecutor`/sequential loop over `process_one`); Tests: each `tests/data/test_serialize_*.py` that asserts continue-on-failure.

Behaviour: replace each module's own loop with `run_per_file(process_one, files, workers=workers, executor="thread", on_result=..., processed_dir=..., raw_dir=..., force=...)` — the same threads as today, no process pool (a process per worker would multiply the DuckDB cap). Add the `TransientUnitError` re-raise in each `process_one`, `record_failure(SOURCE, "serialize", ...)`, and `--skip-unit`/`--reason` via `ops.skip` (Task 5; unit name = file basename, filtered before `run_per_file`). Do not change what a serializer writes.

- [ ] Steps: failing tests (flip), implement as one batch, pass, commit.

---

### Task 8: Graph fails fast

**Files:** Modify `src/episteme/data/graph_load.py` (`build_pubmed_graph`), `src/episteme/data/graph_builder.py` (`main`); Tests `tests/data/test_graph_load.py`, graph builder tests.

Behaviour:
- `build_pubmed_graph`: each source file is one unit with `with_retries` on a fresh connection inside `deferred_mirror` (same shape as Task 5); the first permanent failure stops the loop, `out["failed"]` holds that one file, `out["stopped_at"]` names it; `old`/`corrupt` pre-checks that today append to `failed` and continue now stop at the first one. `graph_builder.main` records the failure and exits 1 (it already exits 1 on `failed`).
- `graph_builder.build` for the other sources is one transaction: wrap the whole build in `with_retries` with a fresh connection per attempt and `deferred_mirror`; a permanent failure records the failure (unit = the source) and exits 1.
- Both mains run inside `stage_guard` and call `orphans.sweep` at start.
- Not changed: the per-shard tolerance for a malformed JATS/MeSH file inside `graph_builder` (`# a malformed shard must not abort the build`) — that is data tolerance, counted in the result, not a unit failure. Say so in the docstring.

Tests to flip: `test_graph_load.py` cases asserting a failed file is listed and later files load. New: stop at the first failed file; transient then success commits once.

- [ ] Steps: failing tests, implement, pass, commit.

---

### Task 9: Extract and serialize mains under `stage_guard`; docs

**Files:** every extractor and serializer `main`; `docs/10-data-sources-runbook.md` (section "Failure rules": fail fast, retries, `--skip-unit`, exit codes 1/130/143, `last_failure.json`, orphan sweep, application_name), `docs/project-incubation-baseline.md` (drift-log entry; backlog: downloads' record-and-continue and pause are PR 3).

- [ ] Steps: wrap each main in `stage_guard(conn_ref)` (extractors hold no long DB statement; `conn_ref` returns None and only the handlers and the watchdog apply); a test per kind (one extractor, one serializer) that SIGTERM during `run_per_file` exits 143 and leaves the in-flight unit's marker; docs; full suite `timeout 1500 .venv/Scripts/python -m pytest -q -m "not pg"`; `ruff format --check .`; commit.

---

## Rollout note

Do not pull this branch into the main checkout while a pipeline run is in progress there (`run_pipeline.sh` changes, and a later stage of a running queue would start new Python processes on the new code). Merge between runs.
