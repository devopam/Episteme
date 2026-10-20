"""Per-file success/failure markers and run manifests (contract §2)."""

from __future__ import annotations

import json
import logging
import os
import traceback
from pathlib import Path
from typing import Any

from episteme.data.article_schema import SCHEMA_VERSION, utc_now_iso

_LOG = logging.getLogger(__name__)
_POSIX = os.name == "posix"  # directories can be fsynced (a seam for tests)


def ops_root(processed_root: Path, source: str) -> Path:
    return Path(processed_root) / "_ops" / source


def success_marker_path(processed_root: Path, source: str, input_basename: str) -> Path:
    return ops_root(processed_root, source) / "success" / f"{input_basename}.ok"


def failed_marker_path(processed_root: Path, source: str, input_basename: str) -> Path:
    return ops_root(processed_root, source) / "failed" / f"{input_basename}.json"


def load_success_marker_path(processed_root: Path, source: str, input_basename: str) -> Path:
    return ops_root(processed_root, source) / "load_success" / f"{input_basename}.ok"


def graph_success_marker_path(processed_root: Path, source: str, input_basename: str) -> Path:
    return ops_root(processed_root, source) / "graph_success" / f"{input_basename}.ok"


def write_marker(path: Path, payload: dict[str, Any]) -> Path:
    """Write a marker atomically: a temp file in the same directory, then
    ``os.replace``. A write that fails part way (a disk error) leaves no
    marker, so a marker that exists is always complete; the presence checks
    (``is_success``, the load and graph skips) rely on that. The temp file is
    fsynced before the rename (and, on POSIX, the directory after it), so a
    power loss leaves either no marker or a complete one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if _POSIX:
        # The marker is complete and visible now. A failed directory sync must
        # not raise: a retry would see the marker, skip the unit and miss its
        # audit row. Only the rename's durability is at risk; log it.
        try:
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as exc:
            _LOG.warning("marker %s written; directory fsync failed: %r", path, exc)
    return path


def is_success(processed_root: Path, source: str, input_basename: str) -> bool:
    return success_marker_path(processed_root, source, input_basename).is_file()


def mark_success(
    processed_root: Path,
    source: str,
    input_basename: str,
    stats: dict[str, Any] | None = None,
) -> Path:
    path = success_marker_path(processed_root, source, input_basename)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Remove prior failure marker if any
    fail = failed_marker_path(processed_root, source, input_basename)
    if fail.is_file():
        fail.unlink()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "source_file": input_basename,
        "completed_at": utc_now_iso(),
        "stats": stats or {},
    }
    return write_marker(path, payload)


def mark_failed(
    processed_root: Path,
    source: str,
    input_basename: str,
    *,
    error_class: str,
    message: str,
    stats: dict[str, Any] | None = None,
    exc: BaseException | None = None,
) -> Path:
    path = failed_marker_path(processed_root, source, input_basename)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "source_file": input_basename,
        "failed_at": utc_now_iso(),
        "error_class": error_class,
        "message": message,
        "stats": stats or {},
    }
    if exc is not None:
        try:
            tb = getattr(exc, "__traceback__", None)
            payload["traceback"] = "".join(traceback.format_exception(type(exc), exc, tb))[-4000:]
        except Exception:
            payload["traceback"] = f"{type(exc).__name__}: {exc}"
    return write_marker(path, payload)


def write_run_manifest(
    processed_root: Path,
    source: str,
    run_id: str,
    config: dict[str, Any],
    totals: dict[str, Any],
) -> Path:
    runs = ops_root(processed_root, source) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"run_{run_id}.json"
    payload = {
        "run_id": run_id,
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "started_or_recorded_at": utc_now_iso(),
        "config": config,
        "totals": totals,
    }
    return write_marker(path, payload)


def list_input_files(raw_glob_root: Path, patterns: list[str]) -> list[Path]:
    """Legacy: dedups by BASENAME — two files sharing a name in different
    directories collapse to one. New code uses discover_input_files + input_key."""
    files: list[Path] = []
    for pat in patterns:
        files.extend(sorted(raw_glob_root.glob(pat)))
        files.extend(sorted(raw_glob_root.rglob(pat)))
    # unique, prefer shorter path order
    seen: set[str] = set()
    out: list[Path] = []
    for f in sorted(set(files), key=lambda p: str(p)):
        if not f.is_file():
            continue
        key = f.name
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def input_key(path: Path, raw_root: Path) -> str:
    """Stable per-input identity: the path relative to ``raw_root``, joined with
    ``__``. A file directly under ``raw_root`` yields its bare basename, so
    flat layouts keep the keys (markers, shards, ``source_file``) they had
    before this helper existed."""
    p = Path(path).resolve()
    try:
        rel = p.relative_to(Path(raw_root).resolve())
    except ValueError:
        return p.name
    return "__".join(rel.parts)


def discover_input_files(raw_root: Path, patterns: list[str]) -> list[Path]:
    """glob + rglob per pattern; dedup by resolved FULL path (never basename).

    Files under a hidden directory of ``raw_root`` (a path part starting with ``.``)
    are skipped: they are tool state, not source data -- e.g. the
    ``.cache/huggingface/`` folder that ``hf download --local-dir`` keeps, whose
    ``trees/<sha>.json`` would otherwise be read as an apollo or guidelines file."""
    root = Path(raw_root)
    seen: set[Path] = set()
    out: list[Path] = []
    for pat in patterns:
        for f in [*root.glob(pat), *root.rglob(pat)]:
            if not f.is_file():
                continue
            if any(part.startswith(".") for part in f.relative_to(root).parts[:-1]):
                continue
            rp = f.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            out.append(f)
    return sorted(out, key=lambda p: str(p))


def find_input_by_key(raw_root: Path, key: str) -> Path | None:
    """Inverse of :func:`input_key`: the file under ``raw_root`` whose key is ``key``."""
    root = Path(raw_root)
    parts = key.split("__")
    for i in range(len(parts)):
        name = "__".join(parts[i:])
        for cand in sorted(root.rglob(name)):
            if cand.is_file() and input_key(cand, root) == key:
                return cand
    return None
