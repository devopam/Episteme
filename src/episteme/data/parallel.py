"""Per-file fan-out for the extractors and the openalex serializer.

Parsing XML and JSON in Python is CPU-bound, so threads stay on one core
(the GIL). ``run_per_file`` runs one call per input file in worker
*processes* instead.

* ``workers <= 1``: in order, in this process (the old single-threaded path).
* ``workers > 1``: a ``ProcessPoolExecutor`` with the ``spawn`` start method --
  the only one on Windows, chosen explicitly so Linux CI behaves the same.

``fn`` must be a module-level function and every argument picklable (``Path``,
``bool``, ``int``, ``str``). Children inherit the environment, so
``get_settings()`` (run id, roots, DB mode) matches the parent's.

An exception that escapes ``fn`` (every ``process_one`` catches its own, so
this means a crash or a pickling error) becomes a failed result and the run
continues, as one failing file does today. A worker that dies breaks the
pool: every file still pending fails the same way, logged once.
"""

from __future__ import annotations

import logging
import multiprocessing
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

from episteme.data.checkpoint_markers import input_key

_LOG = logging.getLogger(__name__)


def _failed_result(path: Path, raw_dir: Path | None, exc: BaseException) -> dict[str, Any]:
    key = input_key(path, raw_dir) if raw_dir is not None else Path(path).name
    return {"source_file": key, "skipped": False, "ok": False, "error": f"worker: {exc!r}"}


def run_per_file(
    fn: Callable[..., dict[str, Any]],
    files: Sequence[Path],
    *,
    workers: int,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Call ``fn(path, **kwargs)`` for every path; return the results.

    With one worker the results come back in ``files`` order; with more, in
    completion order. ``on_result`` always runs in this (the parent) process.
    """
    files = list(files)
    workers = max(1, int(workers))
    results: list[dict[str, Any]] = []

    def _emit(result: dict[str, Any]) -> None:
        results.append(result)
        if on_result is not None:
            on_result(result)

    if not files:
        return results
    if workers == 1:
        for path in files:
            _emit(fn(path, **kwargs))
        return results

    raw_dir = kwargs.get("raw_dir")
    broken_logged = False
    with ProcessPoolExecutor(
        max_workers=min(workers, len(files)),
        mp_context=multiprocessing.get_context("spawn"),
    ) as ex:
        futures = {ex.submit(fn, path, **kwargs): path for path in files}
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                result = fut.result()
            except BrokenProcessPool as exc:
                if not broken_logged:
                    _LOG.error("a worker process died (%r); failing every file still pending", exc)
                    broken_logged = True
                result = _failed_result(path, raw_dir, exc)
            except Exception as exc:  # noqa: BLE001 - one file's failure must not stop the run
                _LOG.error("worker failed on %s: %r", path, exc)
                result = _failed_result(path, raw_dir, exc)
            _emit(result)
    return results
