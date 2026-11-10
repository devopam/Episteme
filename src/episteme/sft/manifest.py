"""Manifests for Stage A slices, Stage B batches and training rounds."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


def config_hash(config: dict) -> str:
    """Return the sha256 of the canonical JSON of ``config``."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_manifest(
    path: Path,
    kind: Literal["slice", "batch", "round"],
    *,
    inputs: list[dict],
    counts: dict[str, int],
    config: dict,
    teacher: str | None = None,
    budget_used: int | None = None,
    extra: dict | None = None,
) -> str:
    """Write a manifest JSON at ``path`` and return its ``config_hash``.

    ``created_at`` is recorded but is not part of the hash. ``extra`` adds outcome
    fields (for example ``stopped``); they are not part of the hash and may not
    replace a standard field.
    """
    digest = config_hash(config)
    doc = {
        "kind": kind,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config_hash": digest,
        "config": config,
        "inputs": inputs,
        "counts": counts,
        "teacher": teacher,
        "budget_used": budget_used,
    }
    if extra:
        if clash := sorted(set(extra) & set(doc)):
            raise ValueError(f"extra fields replace standard fields: {clash}")
        doc.update(extra)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return digest
