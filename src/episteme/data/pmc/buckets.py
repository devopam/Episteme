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


def key_and_rel(path: Path, raw_resolved: Path) -> tuple[str, str]:
    """``(input_key, POSIX path relative to raw_dir)`` of ``path`` from ONE
    ``resolve()``. ``raw_resolved`` is ``raw_dir`` already resolved (do it once
    per run). The key equals ``checkpoint_markers.input_key(path, raw_dir)``;
    outside ``raw_dir`` the key is the file name and the path is absolute."""
    p = Path(path).resolve()
    try:
        rel = p.relative_to(raw_resolved)
    except ValueError:
        return p.name, p.as_posix()
    return "__".join(rel.parts), rel.as_posix()


@dataclass(frozen=True)
class Bucket:
    bucket: int
    files: tuple[Path, ...]  # highest-version metadata path per article, sorted by input_key
    superseded: int  # lower-version files left out
    duplicate_files: int  # extra copies of the same id and version left out
    # input_key and raw_dir-relative path of each file in ``files`` (same order);
    # empty when a caller built the bucket by hand (the writers then compute them).
    keys: tuple[str, ...] = ()
    rels: tuple[str, ...] = ()


def group_by_bucket(meta_files: Iterable[Path], raw_dir: Path) -> tuple[dict[int, Bucket], int]:
    """Group metadata files into buckets; keep the highest version per article.

    Returns the buckets and the count of unrecognised file names. Two paths
    with the same basename are both considered. For the same id and version
    the first path by ``input_key`` is kept.
    """
    unrecognised = 0
    raw_resolved = Path(raw_dir).resolve()  # once, not once per article
    # bucket -> pmc_id -> version -> [(input_key, path, rel)]
    seen: dict[int, dict[int, dict[int, list[tuple[str, Path, str]]]]] = {}
    for path in meta_files:
        parsed = parse_meta_name(path.name)
        if parsed is None:
            unrecognised += 1
            continue
        pmc_id, version = parsed
        key, rel = key_and_rel(path, raw_resolved)
        seen.setdefault(bucket_of(pmc_id), {}).setdefault(pmc_id, {}).setdefault(
            version, []
        ).append((key, path, rel))

    out: dict[int, Bucket] = {}
    for bucket, articles in seen.items():
        kept: list[tuple[str, Path, str]] = []
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
            files=tuple(p for _, p, _ in kept),
            superseded=superseded,
            duplicate_files=duplicates,
            keys=tuple(k for k, _, _ in kept),
            rels=tuple(r for _, _, r in kept),
        )
    return out, unrecognised


def bucket_fingerprint(bucket: Bucket, raw_dir: Path) -> list[list]:
    """``[input_key, size, mtime_ns]`` of each kept metadata file and its XML."""
    # Imported here: extract_pmc will import this module, so a top-level
    # import here would be a cycle.
    from episteme.data.pmc.extract_pmc import find_xml_for_meta

    raw_resolved = Path(raw_dir).resolve()
    meta_keys = bucket.keys if len(bucket.keys) == len(bucket.files) else None
    entries: list[list] = []
    for i, meta in enumerate(bucket.files):
        meta_key = meta_keys[i] if meta_keys else key_and_rel(meta, raw_resolved)[0]
        st = meta.stat()
        entries.append([meta_key, st.st_size, st.st_mtime_ns])
        # search=False: no rglob over raw_dir/xml (935k files on real data).
        xml = find_xml_for_meta(meta, raw_dir, meta.stem, search=False)
        if xml is not None:
            st = xml.stat()
            entries.append([key_and_rel(xml, raw_resolved)[0], st.st_size, st.st_mtime_ns])
    entries.sort(key=lambda e: e[0])
    return entries


def _manifest_path(processed_dir: Path, bucket: int) -> Path:
    return (
        checkpoint_markers.ops_root(processed_dir, "pmc") / "buckets" / f"{bucket_key(bucket)}.json"
    )


def write_manifest(processed_dir: Path, bucket: Bucket, raw_dir: Path) -> Path:
    """``files`` holds the input keys; ``paths`` the same files relative to
    ``raw_dir`` (an input key cannot be turned back into a path)."""
    if len(bucket.keys) == len(bucket.files) and len(bucket.rels) == len(bucket.files):
        keys, rels = list(bucket.keys), list(bucket.rels)
    else:
        raw_resolved = Path(raw_dir).resolve()
        pairs = [key_and_rel(p, raw_resolved) for p in bucket.files]
        keys, rels = [k for k, _ in pairs], [r for _, r in pairs]
    payload: dict[str, Any] = {
        "bucket": bucket.bucket,
        "key": bucket_key(bucket.bucket),
        "files": keys,
        "paths": rels,
        "superseded_versions": bucket.superseded,
        "duplicate_files": bucket.duplicate_files,
        "fingerprint": bucket_fingerprint(bucket, raw_dir),
    }
    return checkpoint_markers.write_marker(_manifest_path(processed_dir, bucket.bucket), payload)


def read_manifest(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def manifest_files(manifest: dict, raw_dir: Path) -> list[Path]:
    """The metadata paths of a manifest (``raw_dir`` joined with ``paths``)."""
    return [Path(raw_dir) / p for p in manifest["paths"]]
