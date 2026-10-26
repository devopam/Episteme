"""The representative raw slice of each gated source, and ``prepare`` (spec 7).

A sample run reads a small slice of the raw files. ``select`` picks the files
from the stage's own discovery function, so the slice holds only files that the
real run would process. ``prepare`` builds the slice folder under
``<sample root>/01_raw/<source>/`` and keeps each file's path relative to the
raw source folder. The ``input_key`` and the row ids then match production.
"""

from __future__ import annotations

import gzip
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


def _select_pubchem(raw: Path) -> list[Path]:
    """The primary files. The slice of pubchem is cut by ``_build_pubchem``."""
    from episteme.data.pubchem.serialize_pubchem import discover_pubchem_files

    return discover_pubchem_files(raw)


def _pubchem_part_rows() -> int:
    from episteme.ops.profile import get_profile

    return get_profile().part_rows


def _gz_or_plain_writer(dest: Path):
    """A binary writer: the cut writes each kept line's bytes unchanged."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.suffix == ".gz":
        return gzip.open(dest, "wb")
    return open(dest, "wb")  # noqa: SIM115


def _cut_file(src: Path, dest: Path, keep: Callable[[int, int], bool | None]) -> int:
    """Write the lines of ``src`` (bytes as read) for which ``keep(index, cid)`` is true.

    ``keep`` returns ``None`` to stop the read. The CID order check of
    ``_iter_cid_raw`` runs on every line read.
    """
    from episteme.data.pubchem.serialize_pubchem import _iter_cid_raw

    n = 0
    with _gz_or_plain_writer(dest) as out:
        for i, (cid, line, _parts) in enumerate(_iter_cid_raw(src)):
            verdict = keep(i, cid)
            if verdict is None:
                break
            if verdict:
                out.write(line)
                n += 1
    return n


def _build_pubchem(raw: Path, dest: Path, part_rows: int | None = None) -> dict:
    """Cut the four pubchem files to the same CID ranges (spec 4.4).

    R1 is the first ``2 * part_rows`` CIDs of CID-SMILES and R2 is ``part_rows``
    CIDs from line floor(N/2). Each sibling file keeps the CIDs of R1 and R2.
    """
    from episteme.data.pubchem.serialize_pubchem import (
        PubchemInputError,
        _find_siblings,
        _iter_cid_raw,
        discover_pubchem_files,
    )

    if part_rows is None:
        part_rows = _pubchem_part_rows()
    primaries = discover_pubchem_files(raw)
    if not primaries:
        raise SliceError(f"pubchem: no CID-SMILES file under {raw}")
    primary = primaries[0]
    if part_rows < 1:
        raise SliceError(f"pubchem: part_rows must be at least 1 (got {part_rows})")
    try:
        n_lines = sum(1 for _ in _iter_cid_raw(primary))
        if n_lines < 1:
            raise SliceError("pubchem: the primary file has no CID lines")
        r1_end = min(2 * part_rows, n_lines)
        r2_start = max(n_lines // 2, r1_end)
        r2_end = min(r2_start + part_rows, n_lines)
        has_r2 = r2_start < r2_end
        last = r2_end if has_r2 else r1_end
        bounds: dict[str, int] = {}

        def keep_primary(i: int, cid: int) -> bool | None:
            if i >= last:
                return None
            if i == 0:
                bounds["lo1"] = cid
            if i == r1_end - 1:
                bounds["hi1"] = cid
            if has_r2 and i == r2_start:
                bounds["lo2"] = cid
            if has_r2 and i == r2_end - 1:
                bounds["hi2"] = cid
            return i < r1_end or r2_start <= i < r2_end

        written: list[Path] = []
        rels: list[str] = []
        pri_dest = dest / primary.relative_to(raw)
        _cut_file(primary, pri_dest, keep_primary)
        written.append(pri_dest)
        rels.append(primary.relative_to(raw).as_posix())
        ranges = [[bounds["lo1"], bounds["hi1"]]]
        if has_r2:
            ranges.append([bounds["lo2"], bounds["hi2"]])
        stop = ranges[-1][1]

        def keep_sibling(_i: int, cid: int) -> bool | None:
            if cid > stop:
                return None
            return any(lo <= cid <= hi for lo, hi in ranges)

        for sib in _find_siblings(primary).values():
            sib_dest = dest / sib.relative_to(raw)
            _cut_file(sib, sib_dest, keep_sibling)
            written.append(sib_dest)
            rels.append(sib.relative_to(raw).as_posix())
    except PubchemInputError as exc:
        raise SliceError(f"pubchem: {exc}") from exc
    return {
        "files": rels,
        "inputs": [primary.relative_to(raw).as_posix()],
        "bytes": sum(_size(p) for p in written),
        "n_lines": n_lines,
        "part_rows": part_rows,
        "ranges": ranges,
    }


def _every_file(_p: Path) -> bool:
    return True


def _is_archive(p: Path) -> bool:
    """A europepmc unit of work: ``discover`` globs ``*.tar.gz`` only."""
    return p.name.endswith(".tar.gz")


@dataclass(frozen=True)
class SliceSpec:
    name: str
    max_bytes: int
    select: Callable[[Path], list[Path]]
    raw_subpath: Path
    # A file the stage's discovery returns (a unit of work). A sidecar file
    # (europepmc .filelist.csv/.txt) is in the slice but is no unit: the yield
    # (rows per input) counts the units only.
    is_unit: Callable[[Path], bool] = _every_file
    # A source whose files are too large to link is cut instead:
    # ``build(raw_dir, dest_dir)`` writes the slice files and returns
    # ``{"files", "inputs", "bytes", **extra}``. ``select`` is then not used by
    # ``prepare``.
    build: Callable[[Path, Path], dict] | None = None


SLICES: dict[str, SliceSpec] = {
    "bookshelf": SliceSpec("bookshelf", 2 * GB, _select_bookshelf, Path("bookshelf")),
    "europepmc_manuscript": SliceSpec(
        "europepmc_manuscript",
        1 * GB,
        _select_europepmc_manuscript,
        Path("europepmc/manuscripts"),
        _is_archive,
    ),
    "apollo": SliceSpec("apollo", 4 * GB, _select_apollo, Path("apollo")),
    "pubchem": SliceSpec("pubchem", 1 * GB, _select_pubchem, Path("pubchem"), build=_build_pubchem),
}


def _spec(source: str) -> SliceSpec:
    try:
        return SLICES[source]
    except KeyError:
        raise SliceError(f"no slice defined for source '{source}'") from None


def select(source: str, raw_root: Path) -> list[Path]:
    """The raw files of the slice, in a deterministic order."""
    spec = _spec(source)
    return spec.select(Path(raw_root).resolve() / spec.raw_subpath)


def _sample_base(processed_root: Path | None) -> Path:
    if processed_root is None:
        from episteme.config import get_settings

        processed_root = get_settings().processed_root
    return Path(processed_root).resolve() / "_sample"


def _reject_links(root: Path, dest_root: Path) -> None:
    """Refuse a symlink or junction at or under the sample root, up to dest_root."""
    p = dest_root
    while True:
        is_junction = getattr(p, "is_junction", None)
        if os.path.islink(p) or (is_junction is not None and is_junction()):
            raise SliceError(f"refusing: {p} is a symlink or junction")
        if p == root or p.parent == p:
            return
        p = p.parent


def prepare(
    source: str,
    raw_root: Path,
    sample_root: Path,
    *,
    processed_root: Path | None = None,
) -> dict:
    """Build ``sample_root/01_raw/<raw subpath>/`` from the slice and write ``slice.json``.

    ``slice.json`` lists every slice file under ``files`` and the units of work
    (the files the stage's discovery returns) under ``inputs``.

    Nothing is deleted unless ``sample_root`` resolves inside
    ``<processed root>/_sample/``.
    """
    spec = _spec(source)
    base = _sample_base(processed_root)
    target_root = Path(sample_root).resolve()
    if target_root == base or base not in target_root.parents:
        raise SliceError(f"refusing: sample root {target_root} is not inside {base}")
    raw = Path(raw_root).resolve() / spec.raw_subpath
    dest_root = Path(sample_root).absolute() / "01_raw" / spec.raw_subpath
    _reject_links(Path(sample_root).absolute(), dest_root)
    meta_path = target_root / "slice.json"
    if spec.build is not None:
        meta_path.unlink(missing_ok=True)
        if dest_root.exists():
            shutil.rmtree(dest_root)
        dest_root.mkdir(parents=True)
        built = spec.build(raw, dest_root)
        if built["bytes"] > spec.max_bytes:
            raise SliceError(
                f"{source}: slice is {built['bytes']} bytes, over the cap of {spec.max_bytes}"
            )
        result = {**built, "linked": 0, "copied": len(built["files"])}
        meta_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    files = select(source, raw_root)
    missing = [p for p in files if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"selected file is missing: {missing[0]}")
    total = sum(_size(p) for p in files)
    if total > spec.max_bytes:
        raise SliceError(f"{source}: slice is {total} bytes, over the cap of {spec.max_bytes}")

    meta_path.unlink(missing_ok=True)
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True)
    linked = copied = 0
    rels: list[str] = []
    units: list[str] = []
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
        if spec.is_unit(src):
            units.append(rel.as_posix())
    result = {"files": rels, "inputs": units, "bytes": total, "linked": linked, "copied": copied}
    meta_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
