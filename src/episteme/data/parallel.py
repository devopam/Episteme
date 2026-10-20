"""Per-file fan-out for the extractors and the serializers.

Parsing XML and JSON in Python is CPU-bound, so threads stay on one core
(the GIL). ``run_per_file`` runs one call per input file in worker
*processes* by default.

* ``workers <= 1``: in order, in this process (the old single-threaded path).
* ``workers > 1``, ``executor="process"``: a ``ProcessPoolExecutor`` with the
  ``spawn`` start method -- the only one on Windows, chosen explicitly so Linux
  CI behaves the same.
* ``workers > 1``, ``executor="thread"``: a ``ThreadPoolExecutor``, for work
  that releases the GIL (the DuckDB serializers).

``fn`` must be a module-level function and every argument picklable (``Path``,
``bool``, ``int``, ``str``). Children inherit the environment, so
``get_settings()`` (run id, roots, DB mode) matches the parent's.

Failure (operating standard section 6.2): the run stops at the first failed
unit. A failed unit is a result with ``ok`` false and ``skipped`` false -- a
``process_one`` that caught its own error, or an exception that escaped ``fn``
(a crash or a pickling error), which becomes a failed result here in every
mode. After the first failed result (completion order) no new unit starts:
pending futures are cancelled (``Future.cancel`` on each), the units already
in flight finish, and their results (and markers) are kept and returned.
``stop_on_failure=False`` keeps the old behaviour: every file runs.

Transient errors: ``fn`` classifies its own exception (where the real exception
exists) and raises ``TransientUnitError``. The unit is then called again after
``waits[0]``, ``waits[1]``, ... seconds; after ``len(waits)`` retries it is a
failed result. The wait runs in this (the parent) process: with a pool, the
workers keep working on the queued files during the wait, only the collection
of results waits. A transient error that arrives after the stop is not retried.

A worker that dies breaks the pool (``BrokenProcessPool``): that is permanent,
never retried; every file still in flight fails the same way, logged once.
"""

from __future__ import annotations

import logging
import multiprocessing
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import (
    FIRST_COMPLETED,
    BrokenExecutor,
    Executor,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path
from typing import Any

from episteme.data.checkpoint_markers import input_key
from episteme.ops.retry import RETRY_WAITS, TransientUnitError

_LOG = logging.getLogger(__name__)

_EXECUTORS = ("process", "thread")
_SLEEP = time.sleep  # seam: tests patch this so retry waits cost nothing


def default_workers() -> int:
    """Worker count when ``--workers`` is not given: the runtime profile's ``cpu_workers``."""
    from episteme.ops.profile import get_profile

    return get_profile().cpu_workers


def is_failure(result: dict[str, Any]) -> bool:
    """A failed unit: not ok and not skipped."""
    return not result.get("ok") and not result.get("skipped")


def first_failure(results: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The first failed result in ``results``, or ``None``."""
    return next((r for r in results if is_failure(r)), None)


def _failed_result(path: Path, raw_dir: Path | None, exc: BaseException) -> dict[str, Any]:
    key = input_key(path, raw_dir) if raw_dir is not None else Path(path).name
    return {"source_file": key, "skipped": False, "ok": False, "error": f"worker: {exc!r}"}


def _log_retry(path: Path, attempt: int, total: int, wait_s: float, exc: BaseException) -> None:
    _LOG.warning(
        "transient error on %s (retry %d/%d in %ss): %s", path, attempt, total, wait_s, exc
    )


def run_per_file(
    fn: Callable[..., dict[str, Any]],
    files: Sequence[Path],
    *,
    workers: int,
    executor: str = "process",
    stop_on_failure: bool = True,
    waits: Sequence[float] = RETRY_WAITS,
    sleep: Callable[[float], None] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Call ``fn(path, **kwargs)`` for every path; return the results.

    With one worker the results come back in ``files`` order; with more, in
    completion order. ``on_result`` always runs in this (the parent) process.
    With ``stop_on_failure`` the list ends at (one worker) or shortly after
    (a pool: the units in flight) the first failed result; find it with
    ``first_failure(results)``.
    """
    if executor not in _EXECUTORS:
        raise ValueError(f"executor must be one of {_EXECUTORS}, not {executor!r}")
    if sleep is None:
        # Late lookup, so a test that patches ``_SLEEP`` reaches every caller.
        def sleep(secs: float) -> None:
            _SLEEP(secs)

    files = list(files)
    workers = max(1, int(workers))
    raw_dir = kwargs.get("raw_dir")
    results: list[dict[str, Any]] = []

    def _emit(result: dict[str, Any]) -> None:
        results.append(result)
        if on_result is not None:
            on_result(result)

    if not files:
        return results
    if workers == 1:
        for path in files:
            result = _call_inline(fn, path, raw_dir, waits, sleep, kwargs)
            _emit(result)
            if stop_on_failure and is_failure(result):
                break
        return results

    max_workers = min(workers, len(files))
    pool: Executor
    if executor == "thread":
        pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="run_per_file")
    else:
        pool = ProcessPoolExecutor(
            max_workers=max_workers, mp_context=multiprocessing.get_context("spawn")
        )
    with pool:
        _run_pool(pool, fn, files, raw_dir, stop_on_failure, waits, sleep, kwargs, _emit)
    return results


def _call_inline(
    fn: Callable[..., dict[str, Any]],
    path: Path,
    raw_dir: Path | None,
    waits: Sequence[float],
    sleep: Callable[[float], None],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    tries = 0
    while True:
        try:
            return fn(path, **kwargs)
        except TransientUnitError as exc:
            if tries >= len(waits):
                _LOG.error("transient error on %s after %d retries: %s", path, tries, exc)
                return _failed_result(path, raw_dir, exc)
            wait_s = waits[tries]
            tries += 1
            _log_retry(path, tries, len(waits), wait_s, exc)
            sleep(wait_s)
        except Exception as exc:  # noqa: BLE001 - becomes a failed result, as in a pool
            _LOG.exception("failed on %s: %r", path, exc)
            return _failed_result(path, raw_dir, exc)


def _run_pool(
    pool: Executor,
    fn: Callable[..., dict[str, Any]],
    files: list[Path],
    raw_dir: Path | None,
    stop_on_failure: bool,
    waits: Sequence[float],
    sleep: Callable[[float], None],
    kwargs: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
) -> None:
    # future -> (path, retries so far). A plain dict: resubmitted futures join it,
    # which ``as_completed`` (a snapshot of its argument) would never yield.
    pending: dict[Future, tuple[Path, int]] = {
        pool.submit(fn, path, **kwargs): (path, 0) for path in files
    }
    stopping = False
    broken_logged = False
    try:
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                path, tries = pending.pop(fut)
                if fut.cancelled():
                    continue
                try:
                    result = fut.result()
                except TransientUnitError as exc:
                    if not stopping and tries < len(waits):
                        wait_s = waits[tries]
                        _log_retry(path, tries + 1, len(waits), wait_s, exc)
                        sleep(wait_s)
                        try:
                            pending[pool.submit(fn, path, **kwargs)] = (path, tries + 1)
                            continue
                        except RuntimeError as sub_exc:  # broken or shut-down pool
                            result = _failed_result(path, raw_dir, sub_exc)
                    elif stopping:
                        _LOG.error(
                            "transient error on %s after the stop, not retried: %s", path, exc
                        )
                        result = _failed_result(path, raw_dir, exc)
                    else:
                        _LOG.error("transient error on %s after %d retries: %s", path, tries, exc)
                        result = _failed_result(path, raw_dir, exc)
                except BrokenExecutor as exc:
                    if not broken_logged:
                        _LOG.error("a worker died (%r); failing every file still in flight", exc)
                        broken_logged = True
                    result = _failed_result(path, raw_dir, exc)
                except Exception as exc:  # noqa: BLE001 - one file's crash becomes its failed result
                    _LOG.exception("worker failed on %s: %r", path, exc)
                    result = _failed_result(path, raw_dir, exc)
                emit(result)
                if stop_on_failure and not stopping and is_failure(result):
                    stopping = True
                    _LOG.error("stopping at the first failed unit: %s", result.get("source_file"))
                    # Cancel each pending future; the ones already running (or, in a
                    # process pool, already handed to the call queue) cannot be
                    # cancelled and finish. Not shutdown(wait=False,
                    # cancel_futures=True) mid-loop: on CPython 3.14 / Windows the
                    # process pool then never completes the in-flight futures, and
                    # wait() below hangs.
                    for f in pending:
                        f.cancel()
                    pending = {f: v for f, v in pending.items() if not f.cancelled()}
    except BaseException:
        # A stop (KeyboardInterrupt, SystemExit) or a bug: drop the queued
        # units so the pool's shutdown waits only for the ones in flight.
        for f in pending:
            f.cancel()
        raise


def _resolve_skip_names(
    source: str, stage: str, keys: Sequence[str], skip_names: Sequence[str]
) -> list[str]:
    """Map each ``--skip-unit`` name to one unit key.

    An exact key is kept. A bare basename that matches exactly one file maps to
    that file's key. A basename that matches two or more files exits 2 and
    lists the keys (one skip must never drop several units behind one audit
    row). A name that matches nothing is passed on; ``apply_skips`` exits 2.
    """
    keyset = set(keys)
    by_base: dict[str, list[str]] = {}
    for key in keys:
        by_base.setdefault(key.rsplit("__", 1)[-1], []).append(key)
    resolved: list[str] = []
    for name in skip_names:
        if name in keyset:
            resolved.append(name)
            continue
        hits = by_base.get(name, [])
        if len(hits) > 1:
            print(
                f"error: --skip-unit {name!r} matches {len(hits)} {stage} units of {source}; "
                f"name one of: {', '.join(sorted(hits))}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        resolved.append(hits[0] if hits else name)
    return resolved


def skip_files(
    source: str,
    stage: str,
    files: Sequence[Path],
    raw_dir: Path,
    skip_names: Sequence[str] | None,
    reason: str | None,
) -> list[Path]:
    """Drop the files named by ``--skip-unit``.

    The unit name is ``input_key(path, raw_dir)`` -- the ``source_file`` that
    ``record_first_failure`` writes. A bare basename is accepted when it
    matches exactly one file. Without skip names this returns ``files`` and
    touches no database. With them, a name that matches no file, or a basename
    that matches several, exits 2 before any audit row; otherwise
    ``ops.skip.apply_skips`` writes one ``unit_skip`` row per skipped unit.
    """
    files = list(files)
    if not skip_names:
        return files
    from episteme.config import get_settings
    from episteme.ops.skip import apply_skips

    keys = [input_key(p, raw_dir) for p in files]
    resolved = _resolve_skip_names(source, stage, keys, skip_names)
    keep = set(apply_skips(source, stage, keys, resolved, reason, run_id=get_settings().run_id))
    out: list[Path] = []
    for path, key in zip(files, keys, strict=True):
        if key in keep:
            out.append(path)
        else:
            print(f"skip-unit {key}", file=sys.stderr)
    return out


def record_first_failure(
    source: str,
    stage: str,
    results: Iterable[dict[str, Any]],
) -> str | None:
    """Write ``last_failure.json`` for the first failed result; return its unit.

    The unit is the result's ``source_file`` (``input_key(path, raw_dir)``),
    the name ``--skip-unit`` takes.
    """
    fail = first_failure(results)
    if fail is None:
        return None
    from episteme.ops.failure import record_failure

    unit = str(fail.get("source_file") or "")
    error = str(fail.get("error") or "")
    record_failure(source, stage, unit, error)
    print(f"stopped at {unit}: {error}", file=sys.stderr)
    return unit
