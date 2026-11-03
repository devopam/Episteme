"""Mechanics shared by the extractors that write buckets as bounded parts (DailyMed, EPAR).

A bucket is a set of inputs that gets one manifest, one success marker and a run of
parts ``<bucket key>__partNNNNN``. A part is one shard under ``staging/<source>/``, an
``extract_commit`` audit row and a success marker, in that order. These helpers do not
know what an input is: the callers (``dailymed.extract_dailymed``, ``ema.extract_epar``)
keep the scan, the manifests and the row building.

``write_parts`` is the PMC rule: a row that would take a part over ``part_bytes`` starts
the next part, one row over the cap is alone in its part, and a part stops at
``part_rows``. ``delete_all_parts`` is the rewrite rule: the names of parts that were
loaded go to the retire pending file before any file is deleted.
"""

from __future__ import annotations

import contextlib
import glob
import json
import logging
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from episteme.audit_trail import record as _audit
from episteme.config import get_settings
from episteme.data import parts_retire
from episteme.data.checkpoint_markers import (
    failed_marker_path,
    load_success_marker_path,
    mark_success,
    ops_root,
    success_marker_path,
    write_marker,
)
from episteme.data.staging_writer import write_rows
from episteme.ops.profile import PART_ROW_OVERHEAD_BYTES

_LOG = logging.getLogger(__name__)

AUDIT_LOCK = threading.Lock()


def is_sample(processed_dir: Path) -> bool:
    """A sample run (``run_pipeline.sh --sample``) has a processed root under ``_sample``."""
    return "_sample" in Path(processed_dir).resolve().parts


def check_bucket_scheme(processed_dir: Path, source: str, field: str, value: int) -> int:
    """Record the bucket width of this processed dir at the first run and return the
    width on file. The caller refuses a run at another width, before anything is written:
    a different width gives different bucket keys, so the old parts and rows would stay
    and the same inputs would load again under the new keys."""
    path = ops_root(processed_dir, source) / "bucket_scheme.json"
    if not path.is_file():
        write_marker(path, {field: value})
        return value
    return int(json.loads(path.read_text(encoding="utf-8"))[field])


def audit_override(source: str, reason: str, what: str) -> None:
    """One ``force_override`` audit row for a guard that ``--force --reason`` overrode,
    degrading to the file-only mirror when no DB is reachable. Never raises."""
    obj = f"{source} extract guard: {what}"[:500]
    with AUDIT_LOCK:
        try:
            from episteme.data.db.connection import connection as _pg_connection

            with _pg_connection() as _conn:
                _audit(
                    "force_override",
                    conn=_conn,
                    object=obj,
                    reason=reason,
                    run_id=get_settings().run_id,
                )
                _conn.commit()
        except Exception:  # noqa: BLE001 - best-effort
            try:
                from episteme import audit_trail as _audit_trail

                _audit_trail.mirror_only(
                    "force_override",
                    object=obj,
                    reason=reason,
                    note="db_unavailable_or_failed",
                    run_id=get_settings().run_id,
                )
            except Exception:  # noqa: BLE001
                _LOG.warning("audit mirror_only fallback also failed", exc_info=True)


def best_effort_audit(source: str, name: str, n_rows: int) -> None:
    """One chained ``extract_commit`` audit row, degrading to the file-only mirror when
    no DB is reachable. Never raises. Called BEFORE a part's ``mark_success``."""
    with AUDIT_LOCK:
        try:
            from episteme.data.db.connection import connection as _pg_connection

            with _pg_connection() as _conn:
                _audit(
                    "extract_commit",
                    conn=_conn,
                    object=f"{source} {name}",
                    rows_affected=n_rows,
                    reason=None,
                    run_id=get_settings().run_id,
                )
                _conn.commit()
        except Exception:  # noqa: BLE001 - best-effort
            try:
                from episteme import audit_trail as _audit_trail

                _audit_trail.mirror_only(
                    "extract_commit",
                    object=f"{source} {name}",
                    rows_affected=n_rows,
                    note="db_unavailable_or_failed",
                    run_id=get_settings().run_id,
                )
            except Exception:  # noqa: BLE001
                _LOG.warning("audit mirror_only fallback also failed for %s", name, exc_info=True)


def delete_all_parts(
    processed_dir: Path, source: str, key: str, extra_dirs: tuple[Path, ...] = ()
) -> None:
    """Delete every part of bucket ``key``: success, failed and load-success markers
    first, then the shards (and the files in ``extra_dirs``, e.g. sidecars). A part with
    a load-success marker was loaded: its name goes to the retire pending file of the
    bucket BEFORE any delete."""
    pattern = re.compile(re.escape(key) + r"__part(\d{5,})\.")
    prefix = glob.escape(f"{key}__part") + "*"
    load_dir = load_success_marker_path(processed_dir, source, "x").parent
    marker_dirs = [
        success_marker_path(processed_dir, source, "x").parent,
        failed_marker_path(processed_dir, source, "x").parent,
        load_dir,
    ]
    loaded: list[str] = []
    if load_dir.is_dir():
        for p in load_dir.glob(prefix):
            m = pattern.match(p.name)
            if m:
                loaded.append(f"{key}__part{m.group(1)}")
    if loaded:
        parts_retire.add_retire_pending(processed_dir, source, loaded, unit=key)
    staging = Path(processed_dir) / "staging" / source
    for d in [*marker_dirs, staging, *extra_dirs]:
        if not d.is_dir():
            continue
        for p in d.glob(prefix):
            if pattern.match(p.name):
                p.unlink(missing_ok=True)


def read_bucket_marker(processed_dir: Path, source: str, key: str) -> dict[str, Any] | None:
    """The stats of the bucket success marker when it has the parts format, else ``None``."""
    p = success_marker_path(processed_dir, source, key)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    st = payload.get("stats") if isinstance(payload, dict) else None
    if not isinstance(st, dict):
        return None
    parts = st.get("parts")
    if not isinstance(parts, int) or isinstance(parts, bool) or parts < 0:
        return None
    if st.get("parts_list") != [f"{key}__part{n:05d}" for n in range(1, parts + 1)]:
        return None
    if not isinstance(st.get("inputs"), list):
        return None
    return st


def select_buckets(
    source: str, keys: list[str], skip_units: list[str] | None, skip_reason: str | None
) -> list[str]:
    """``keys`` without the units named by ``--skip-unit`` (audited by ``ops.skip``)."""
    if not skip_units:
        return keys
    import sys

    from episteme.ops import skip

    keep = set(
        skip.apply_skips(
            source, "extract", keys, skip_units, skip_reason, run_id=get_settings().run_id
        )
    )
    for k in keys:
        if k not in keep:
            print(f"skip-unit {k}", file=sys.stderr)
    return [k for k in keys if k in keep]


def write_failed_inputs(processed_dir: Path, source: str, failed: int, **extra: int) -> None:
    """Tell the sample gate how many inputs failed. ``gate run`` reads the file when its
    ``run_id`` is the run's own."""
    from episteme.data.article_schema import utc_now_iso

    run_id = get_settings().run_id or utc_now_iso().replace(":", "").replace("-", "")
    write_marker(
        ops_root(processed_dir, source) / "failed_inputs.json",
        {"run_id": run_id, "failed_inputs": failed, **extra},
    )


def row_bytes(row: dict[str, Any]) -> int:
    """Estimated bytes of one row: the string columns plus the profile's row overhead."""
    total = PART_ROW_OVERHEAD_BYTES
    for value in row.values():
        if isinstance(value, str):
            total += len(value.encode("utf-8"))
    return total


def write_parts(
    rows_src: Iterator[tuple[dict[str, Any], list[Any]]],
    *,
    processed_dir: Path,
    source: str,
    part_name: Callable[[int], str],
    cap_rows: int,
    cap_bytes: int,
    batch_rows: int,
    after_shard: Callable[[str, list[Any]], dict[str, Any]] | None = None,
    progress: dict[str, Any] | None = None,
) -> tuple[int, int, Counter]:
    """Write ``rows_src`` (pairs of ``(row, side rows)``) as parts; return
    ``(parts, rows, extract_status counts)``.

    ``after_shard(part name, side rows)`` runs after the shard is written and before the
    audit row; its dict joins the part marker's stats. ``progress["part"]`` holds the
    number of the part being written and ``progress["rows"]`` the rows of the finished
    parts (for a failure marker)."""
    progress = progress if progress is not None else {}
    status_counts: Counter = Counter()
    n = n_rows = 0
    with contextlib.closing(rows_src):
        pending = next(rows_src, None)
        while pending is not None:
            n += 1
            progress["part"] = n
            psf = part_name(n)
            part: dict[str, Any] = {
                "rows": 0,
                "est_bytes": 0,
                "first": None,
                "last": None,
                "counts": Counter(),
                "side": [],
            }

            def _part_rows(psf: str = psf, part: dict[str, Any] = part):
                nonlocal pending
                while pending is not None:
                    row, side = pending
                    row_est = row_bytes(row)
                    # A row that does not fit ends the part and stays pending for the
                    # next one; a single row over the cap goes alone in its own part.
                    if part["rows"] and part["est_bytes"] + row_est > cap_bytes:
                        return
                    pending = None
                    row["source_file"] = psf
                    part["rows"] += 1
                    part["est_bytes"] += row_est
                    part["side"].extend(side)
                    if part["first"] is None:
                        part["first"] = row["id"]
                    part["last"] = row["id"]
                    part["counts"][str(row.get("extract_status"))] += 1
                    yield row
                    if part["rows"] >= cap_rows:
                        return
                    pending = next(rows_src, None)

            gen = _part_rows()
            try:
                write_info = write_rows(
                    gen,
                    processed_dir,
                    source=source,
                    source_file=psf,
                    prefer_parquet=True,
                    batch_size=batch_rows,
                )
            finally:
                gen.close()
            # A sidecar follows the shard it belongs to; the bucket marker (after the last
            # part) is what says that the extract is whole.
            extra = after_shard(psf, part["side"]) if after_shard is not None else {}
            part_stats = {
                "n_rows": part["rows"],
                "first_id": part["first"],
                "last_id": part["last"],
                "extract_status_counts": dict(part["counts"]),
                **extra,
                "part_rows": cap_rows,
                "part_bytes": cap_bytes,
                "est_bytes": part["est_bytes"],
                "write": write_info,
            }
            part["side"] = []
            best_effort_audit(source, psf, part["rows"])
            mark_success(processed_dir, source, psf, stats=part_stats)
            n_rows += part["rows"]
            progress["rows"] = n_rows
            status_counts.update(part["counts"])
            progress["part"] = n + 1
            if pending is None:
                pending = next(rows_src, None)
    return n, n_rows, status_counts
