"""The representative raw slice of each gated source, and ``prepare`` (spec 7).

A sample run reads a small slice of the raw files. ``select`` picks the files
from the stage's own discovery function, so the slice holds only files that the
real run would process. ``prepare`` builds the slice folder under
``<sample root>/01_raw/<source>/`` and keeps each file's path relative to the
raw source folder. The ``input_key`` and the row ids then match production.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

GB = 1024**3
MB = 1024**2

BOOKSHELF_MAX_PACKAGE_BYTES = 1 * GB
BOOKSHELF_SPREAD = 20
BOOKSHELF_PERCENTILE = 0.99
APOLLO_MIN_BYTES = 10 * MB

_EUROPEPMC_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})\.tar\.gz$")


class SliceError(RuntimeError):
    """The slice cannot be selected or built."""


def _size(path: Path) -> int:
    return path.stat().st_size


def _select_bookshelf(raw: Path) -> list[Path]:
    from episteme.data.bookshelf.extract_bookshelf import discover_bookshelf_files

    files = [p for p in discover_bookshelf_files(raw) if _size(p) <= BOOKSHELF_MAX_PACKAGE_BYTES]
    if not files:
        raise SliceError(f"bookshelf: no package of at most 1 GB under {raw}")
    n = len(files)
    if n <= BOOKSHELF_SPREAD:
        picked = set(range(n))
    else:
        step = BOOKSHELF_SPREAD - 1
        picked = {round(i * (n - 1) / step) for i in range(BOOKSHELF_SPREAD)}
    by_size = sorted(range(n), key=lambda i: (_size(files[i]), i))
    picked.add(by_size[max(0, math.ceil(BOOKSHELF_PERCENTILE * n) - 1)])
    return [files[i] for i in sorted(picked)]


def _europepmc_date(p: Path) -> str:
    m = _EUROPEPMC_DATE.search(p.name)
    return m.group(1) if m else ""


def _siblings(pkg: Path) -> list[Path]:
    base = pkg.name[: -len(".tar.gz")] if pkg.name.endswith(".tar.gz") else pkg.name
    out: list[Path] = []
    for ext in (".filelist.csv", ".filelist.txt"):
        for name in (base + ext, pkg.name + ext):
            cand = pkg.with_name(name)
            if cand.is_file() and cand not in out:
                out.append(cand)
    return out


def _select_europepmc_manuscript(raw: Path) -> list[Path]:
    from episteme.data.europepmc.manuscripts.extract_europepmc_manuscripts import discover

    pkgs = discover(raw)
    baselines = [p for p in pkgs if "baseline" in p.name]
    incrementals = [p for p in pkgs if "incr" in p.name]
    if not baselines:
        raise SliceError(f"europepmc_manuscript: no baseline package under {raw}")
    if not incrementals:
        raise SliceError(f"europepmc_manuscript: no incremental package under {raw}")
    chosen = [
        min(baselines, key=lambda p: p.name),
        max(baselines, key=lambda p: (_europepmc_date(p), p.name)),
        max(incrementals, key=lambda p: (_europepmc_date(p), p.name)),
    ]
    out: list[Path] = []
    for pkg in chosen:
        for p in [pkg, *_siblings(pkg)]:
            if p not in out:
                out.append(p)
    return out


_APOLLO_GROUPS: tuple[tuple[str, Callable[[tuple[str, ...], str], bool]], ...] = (
    (
        "train/pretrain *_qa.json",
        lambda parts, name: parts == ("train", "pretrain") and name.endswith("_qa.json"),
    ),
    (
        "train/pretrain *_text.json",
        lambda parts, name: parts == ("train", "pretrain") and name.endswith("_text.json"),
    ),
    (
        "train/sft medical*",
        lambda parts, name: parts == ("train", "sft") and name.startswith("medical"),
    ),
)


def _select_apollo(raw: Path) -> list[Path]:
    from episteme.data.apollo.extract_apollo import discover_apollo_files

    files = discover_apollo_files(raw)
    out: list[Path] = []
    for label, match in _APOLLO_GROUPS:
        cands = [
            p
            for p in files
            if match(p.relative_to(raw).parts[:-1], p.name) and _size(p) >= APOLLO_MIN_BYTES
        ]
        if not cands:
            raise SliceError(
                f"apollo: no file of at least {APOLLO_MIN_BYTES} bytes in group "
                f"'{label}' under {raw}"
            )
        out.append(min(cands, key=lambda p: (_size(p), p.name)))
    return out


@dataclass(frozen=True)
class SliceSpec:
    name: str
    max_bytes: int
    select: Callable[[Path], list[Path]]


SLICES: dict[str, SliceSpec] = {
    "bookshelf": SliceSpec("bookshelf", 2 * GB, _select_bookshelf),
    "europepmc_manuscript": SliceSpec("europepmc_manuscript", 1 * GB, _select_europepmc_manuscript),
    "apollo": SliceSpec("apollo", 4 * GB, _select_apollo),
}


def _spec(source: str) -> SliceSpec:
    try:
        return SLICES[source]
    except KeyError:
        raise SliceError(f"no slice defined for source '{source}'") from None


def select(source: str, raw_source_dir: Path) -> list[Path]:
    """The raw files of the slice, in a deterministic order."""
    return _spec(source).select(Path(raw_source_dir))


def _sample_base(processed_root: Path | None) -> Path:
    if processed_root is None:
        from episteme.config import get_settings

        processed_root = get_settings().processed_root
    return Path(processed_root).resolve() / "_sample"


def prepare(
    source: str,
    raw_source_dir: Path,
    sample_root: Path,
    processed_root: Path | None = None,
) -> dict:
    """Build ``sample_root/01_raw/<source>/`` from the slice and write ``slice.json``.

    Nothing is deleted unless ``sample_root`` resolves inside
    ``<processed root>/_sample/``.
    """
    spec = _spec(source)
    base = _sample_base(processed_root)
    target_root = Path(sample_root).resolve()
    if target_root == base or base not in target_root.parents:
        raise SliceError(f"refusing: sample root {target_root} is not inside {base}")
    raw = Path(raw_source_dir)
    files = select(source, raw)
    missing = [p for p in files if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"selected file is missing: {missing[0]}")
    total = sum(_size(p) for p in files)
    if total > spec.max_bytes:
        raise SliceError(f"{source}: slice is {total} bytes, over the cap of {spec.max_bytes}")

    dest_root = target_root / "01_raw" / raw.name
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True)
    linked = copied = 0
    rels: list[str] = []
    for src in files:
        rel = src.relative_to(raw)
        dst = dest_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)
            linked += 1
        except OSError:
            shutil.copy2(src, dst)
            copied += 1
        rels.append(rel.as_posix())
    result = {"files": rels, "bytes": total, "linked": linked, "copied": copied}
    (target_root / "slice.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
