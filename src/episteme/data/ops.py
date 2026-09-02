"""Per-file success/failure markers and run manifests (contract §2)."""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from episteme.data.article_schema import SCHEMA_VERSION, utc_now_iso


def ops_root(processed_root: Path, source: str) -> Path:
    return Path(processed_root) / "_ops" / source


def success_marker_path(processed_root: Path, source: str, input_basename: str) -> Path:
    return ops_root(processed_root, source) / "success" / f"{input_basename}.ok"


def failed_marker_path(processed_root: Path, source: str, input_basename: str) -> Path:
    return ops_root(processed_root, source) / "failed" / f"{input_basename}.json"


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
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


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
            payload["traceback"] = "".join(
                traceback.format_exception(type(exc), exc, tb)
            )[-4000:]
        except Exception:
            payload["traceback"] = f"{type(exc).__name__}: {exc}"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


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
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def list_input_files(raw_glob_root: Path, patterns: list[str]) -> list[Path]:
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
