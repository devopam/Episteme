"""Fixed PMC id buckets, highest-version selection and bucket manifests.

A bucket is a fixed range of ``BUCKET_IDS`` PMC ids. The bucket of an article
never changes, so a bucket's manifest and parts stay valid as new articles
arrive. Pure module: it reads file names and stats, never file content.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from episteme.data import checkpoint_markers

BUCKET_IDS: int = 50_000

_META_RE = re.compile(r"^PMC(\d+)\.(\d+)\.json$")


def parse_meta_name(name: str) -> tuple[int, int] | None:
    """``PMC1043859.2.json`` -> ``(1043859, 2)``; any other name -> ``None``."""
    m = _META_RE.match(name)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2))


def bucket_of(pmc_id: int) -> int:
    return pmc_id // BUCKET_IDS


def bucket_key(bucket: int) -> str:
    return f"pmc_b{bucket:04d}"


def part_name(bucket: int, n: int) -> str:
    return f"{bucket_key(bucket)}__part{n:05d}"


@dataclass(frozen=True)
class Bucket:
    bucket: int
    files: tuple[Path, ...]  # highest-version metadata path per article, sorted by input_key
    superseded: int  # lower-version files left out
    duplicate_files: int  # extra copies of the same id and version left out


def group_by_bucket(meta_files: Iterable[Path], raw_dir: Path) -> tuple[dict[int, Bucket], int]:
    """Group metadata files into buckets; keep the highest version per article.

    Returns the buckets and the count of unrecognised file names. Two paths
    with the same basename are both considered. For the same id and version
    the first path by ``input_key`` is kept.
    """
    unrecognised = 0
    # bucket -> pmc_id -> version -> [(input_key, path)]
    seen: dict[int, dict[int, dict[int, list[tuple[str, Path]]]]] = {}
    for path in meta_files:
        parsed = parse_meta_name(path.name)
        if parsed is None:
            unrecognised += 1
            continue
        pmc_id, version = parsed
        key = checkpoint_markers.input_key(path, raw_dir)
        seen.setdefault(bucket_of(pmc_id), {}).setdefault(pmc_id, {}).setdefault(
            version, []
        ).append((key, path))

    out: dict[int, Bucket] = {}
    for bucket, articles in seen.items():
        kept: list[tuple[str, Path]] = []
        superseded = 0
        duplicates = 0
        for versions in articles.values():
            top = max(versions)
            superseded += len(versions) - 1
            for version, copies in versions.items():
                copies.sort(key=lambda kp: kp[0])
                duplicates += len(copies) - 1
                if version == top:
                    kept.append(copies[0])
        kept.sort(key=lambda kp: kp[0])
        out[bucket] = Bucket(
            bucket=bucket,
            files=tuple(p for _, p in kept),
            superseded=superseded,
            duplicate_files=duplicates,
        )
    return out, unrecognised


def bucket_fingerprint(bucket: Bucket, raw_dir: Path) -> list[list]:
    """``[input_key, size, mtime_ns]`` of each kept metadata file and its XML."""
    # Imported here: extract_pmc will import this module, so a top-level
    # import here would be a cycle.
    from episteme.data.pmc.extract_pmc import find_xml_for_meta

    entries: list[list] = []
    for meta in bucket.files:
        paths = [meta]
        xml = find_xml_for_meta(meta, raw_dir, meta.stem)
        if xml is not None:
            paths.append(xml)
        for p in paths:
            st = p.stat()
            entries.append([checkpoint_markers.input_key(p, raw_dir), st.st_size, st.st_mtime_ns])
    entries.sort(key=lambda e: e[0])
    return entries


def _manifest_path(processed_dir: Path, bucket: int) -> Path:
    return (
        checkpoint_markers.ops_root(processed_dir, "pmc") / "buckets" / f"{bucket_key(bucket)}.json"
    )


def write_manifest(processed_dir: Path, bucket: Bucket, raw_dir: Path) -> Path:
    payload: dict[str, Any] = {
        "bucket": bucket.bucket,
        "key": bucket_key(bucket.bucket),
        "files": [checkpoint_markers.input_key(p, raw_dir) for p in bucket.files],
        "superseded_versions": bucket.superseded,
        "duplicate_files": bucket.duplicate_files,
        "fingerprint": bucket_fingerprint(bucket, raw_dir),
    }
    return checkpoint_markers.write_marker(_manifest_path(processed_dir, bucket.bucket), payload)


def read_manifest(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
