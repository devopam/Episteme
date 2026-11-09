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
import zipfile
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

# FAERS: the quarter folder of the openFDA zips, and the quarter that the change from
# AERS to FAERS (2012) falls in.
_FAERS_QUARTER = re.compile(r"(\d{4})q([1-4])")
FAERS_CHANGE_QUARTER = (2012, 3)
# DailyMed: the number of largest inner zips whose XML size is read, and the number of
# largest labels whose products are counted.
DAILYMED_SIZE_CANDIDATES = 200
DAILYMED_PRODUCT_CANDIDATES = 20
# EPAR: the number of largest PDFs in the slice; the number of next-largest PDFs searched
# for a scan, and the bytes per page from which a PDF looks like a scan.
EPAR_LARGEST = 6
EPAR_SCAN_CANDIDATES = 50
EPAR_SCANNED_MIN_BYTES_PER_PAGE = 100_000


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


def _select_europepmc_preprint(raw: Path) -> list[Path]:
    """The largest bulk file: the sample covers the largest real unit (about 10,000 to 25,000
    records, 150 to 280 MB), as the rule for samples asks."""
    from episteme.data.europepmc.preprints.extract_europepmc_preprints import discover

    files = discover(raw)
    if not files:
        raise SliceError(f"europepmc_preprint: no PPR<a>_PPR<b>.xml.gz bulk file under {raw}")
    return [max(files, key=lambda p: (_size(p), p.name))]


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


def _select_pmc(raw: Path) -> list[Path]:
    """The files of three buckets: the largest by XML bytes, the median by XML
    bytes and the one with the most superseded versions (spec 4.6).

    One walk of the metadata folder and up to four ``is_file`` checks per kept
    article (``find_xml_for_meta`` with ``search=False``); no search of
    ``raw/xml`` (it holds about 935k files). A chosen bucket
    brings all of its metadata files, also the lower versions, so the sample
    runs the highest-version choice. Ties go to the lowest bucket number.
    """
    from episteme.data.pmc.buckets import bucket_of, group_by_bucket, parse_meta_name
    from episteme.data.pmc.extract_pmc import discover_meta_files, find_xml_for_meta

    meta_files = discover_meta_files(raw)
    buckets, _unrecognised = group_by_bucket(meta_files, raw)
    if not buckets:
        raise SliceError(f"pmc: no PMC*.json metadata file under {raw}")

    xml_of: dict[Path, Path] = {}

    def xml_bytes(bucket) -> int:
        total = 0
        for meta in bucket.files:
            xml = find_xml_for_meta(meta, raw, meta.stem, search=False)
            if xml is not None:
                xml_of[meta] = xml
                total += _size(xml)
        return total

    sizes = {b: xml_bytes(bk) for b, bk in buckets.items()}
    by_size = sorted(sizes, key=lambda b: (sizes[b], b))
    largest = max(sizes, key=lambda b: (sizes[b], -b))
    median = by_size[(len(by_size) - 1) // 2]
    most_versions = max(buckets, key=lambda b: (buckets[b].superseded, -b))
    chosen = sorted({largest, median, most_versions})

    out: list[Path] = []
    seen: set[Path] = set()
    for meta in sorted(meta_files, key=lambda p: p.as_posix()):
        parsed = parse_meta_name(meta.name)
        if parsed is None or bucket_of(parsed[0]) not in chosen:
            continue
        out.append(meta)
        xml = xml_of.get(meta)
        if xml is None:
            xml = find_xml_for_meta(meta, raw, meta.stem, search=False)
        if xml is not None and xml not in seen:
            seen.add(xml)
            out.append(xml)
    return out


# --- regulatory sources ------------------------------------------------------


def _quarter_index(folder: str) -> int | None:
    m = _FAERS_QUARTER.fullmatch(folder)
    return int(m.group(1)) * 4 + int(m.group(2)) - 1 if m else None


def _faers_by_quarter(raw: Path) -> dict[int, list[Path]]:
    """The openFDA event zips by quarter index. A folder not named like ``2020q1``
    (``all_other``) is left out."""
    from episteme.data.regulatory.faers.generation import faers_zips

    out: dict[int, list[Path]] = {}
    for p in faers_zips(raw):
        n = _quarter_index(p.parent.name)
        if n is not None:
            out.setdefault(n, []).append(p)
    return out


def faers_largest_year(raw: Path) -> int | None:
    """The year whose zips hold the most bytes (a tie goes to the later year), or None."""
    by_year: dict[int, int] = {}
    for n, files in _faers_by_quarter(Path(raw)).items():
        by_year[n // 4] = by_year.get(n // 4, 0) + sum(_size(p) for p in files)
    return max(by_year, key=lambda y: (by_year[y], y)) if by_year else None


def _select_faers(raw: Path) -> list[Path]:
    """The oldest quarter (all its zips), the quarter nearest to 2012q3 (all its zips; the
    earlier one on a tie), the largest zip of the newest quarter, and all zips of the
    largest year, so that the signal build runs on a year-sized partition."""
    quarters = _faers_by_quarter(raw)
    if not quarters:
        raise SliceError(f"openfda_faers: no event zip in a <year>q<n> folder under {raw}")
    target = FAERS_CHANGE_QUARTER[0] * 4 + FAERS_CHANGE_QUARTER[1] - 1
    chosen: set[Path] = set(quarters[min(quarters)])
    chosen.update(quarters[min(quarters, key=lambda n: (abs(n - target), n))])
    chosen.add(max(quarters[max(quarters)], key=lambda p: (_size(p), p.name)))
    year = faers_largest_year(raw)
    for n, files in quarters.items():
        if n // 4 == year:
            chosen.update(files)
    return sorted(chosen, key=lambda p: p.as_posix())


def _select_openfda(raw: Path) -> list[Path]:
    """The zips of the openFDA reference feeds (``drug/<tag>``). Not ``drug/event``
    (FAERS), ``drug/label`` or ``other/substance``."""
    from episteme.data.regulatory.load_reg import feed_files, select_feeds

    out: list[Path] = []
    for feed in select_feeds("openfda", None):
        out.extend(feed_files("openfda", feed, raw))
    if not out:
        raise SliceError(f"openfda: no feed zip under {raw}")
    return sorted(set(out), key=lambda p: p.as_posix())


def _select_ema(raw: Path) -> list[Path]:
    """Every EMA feed file directly under the raw folder (they are small). Not the PDFs."""
    from episteme.data.regulatory.load_reg import feed_files, select_feeds

    out: list[Path] = []
    for feed in select_feeds("ema", None):
        out.extend(feed_files("ema", feed, raw))
    if not out:
        raise SliceError(f"ema: no feed file under {raw}")
    return sorted(set(out), key=lambda p: p.as_posix())


def _select_aact(raw: Path) -> list[Path]:
    """The whole AACT release file: the newest release zip under the raw folder. The slice
    is one unit of 2.4 GiB, so the sample loads it all (spec 7)."""
    from episteme.data.aact.load_aact import find_release_zip, release_zips

    zips = release_zips(raw)
    if not zips:
        raise SliceError(f"aact: no release zip under {raw}")
    try:
        return [find_release_zip(raw, zips)]
    except ValueError as exc:
        raise SliceError(f"aact: {exc}") from exc


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _cut_only(source: str) -> Callable[[Path], list[Path]]:
    def select(_raw: Path) -> list[Path]:
        raise SliceError(f"{source}: the slice is cut by its builder, not selected")

    return select


def _build_dailymed(raw: Path, dest: Path) -> dict:
    """Cut new outer zips with the chosen labels: the largest label (by XML bytes, among
    the ``DAILYMED_SIZE_CANDIDATES`` largest inner zips), the label with the most products
    (among the ``DAILYMED_PRODUCT_CANDIDATES`` largest XMLs) and one OTC label (the median
    by inner zip size). The inner zips and their member names are copied unchanged, so a
    label keeps its ``label_key`` and its id. The outer zips are named ``..._part1``.

    Each outer zip is opened once and kept open for the whole build. A candidate whose inner
    zip or XML cannot be read is skipped and counted (``skipped_candidates``); the build
    stops with ``SliceError`` only when no readable prescription label remains."""
    import contextlib

    from defusedxml import ElementTree as ET  # noqa: N817

    from episteme.config import get_settings
    from episteme.data.dailymed import extract_dailymed as dm
    from episteme.data.dailymed import spl

    kinds = get_settings().dailymed_kinds
    outers = [p for p in dm.discover_outer_zips(raw, kinds) if dm.RELEASE_RE.match(p.name)]
    skipped = 0
    with contextlib.ExitStack() as stack:
        opened: dict[Path, zipfile.ZipFile] = {
            p: stack.enter_context(zipfile.ZipFile(p)) for p in outers
        }
        # (inner zip bytes, outer zip, member name), by kind.
        entries: dict[str, list[tuple[int, Path, str]]] = {k: [] for k in kinds}
        for outer, zf in opened.items():
            kind = dm.RELEASE_KIND[dm.RELEASE_RE.match(outer.name).group(1)]
            for info in zf.infolist():
                if dm.wanted_member(info, (kind,)):
                    entries[kind].append((info.file_size, outer, info.filename))
        empty = [k for k in kinds if not entries[k]]
        if empty:
            raise SliceError(f"dailymed: no {', '.join(empty)} label in a full release under {raw}")

        def read_xml(outer_path: Path, member: str) -> bytes:
            zf = opened[outer_path]
            with dm.inner_zip(zf, zf.getinfo(member)) as inner:
                return inner.read(dm.xml_name(inner))

        def xml_bytes_of(outer_path: Path, member: str) -> int:
            zf = opened[outer_path]
            with dm.inner_zip(zf, zf.getinfo(member)) as inner:
                return inner.getinfo(dm.xml_name(inner)).file_size

        chosen: dict[str, list[tuple[Path, str]]] = {k: [] for k in kinds}
        if "prescription" in kinds:
            rank = sorted(entries["prescription"], key=lambda e: (-e[0], e[1].name, e[2]))
            sized: list[tuple[int, Path, str]] = []
            for _bytes, outer_path, member in rank[:DAILYMED_SIZE_CANDIDATES]:
                try:
                    sized.append((xml_bytes_of(outer_path, member), outer_path, member))
                except (ValueError, zipfile.BadZipFile, KeyError, OSError):
                    skipped += 1  # an unreadable label is not a candidate
            sized.sort(key=lambda e: (-e[0], e[1].name, e[2]))
            best: tuple[int, Path, str] | None = None
            best_products = -1
            readable: list[tuple[int, Path, str]] = []
            for entry in sized[:DAILYMED_PRODUCT_CANDIDATES]:
                try:
                    root = ET.fromstring(read_xml(entry[1], entry[2]))
                except (ET.ParseError, ValueError, zipfile.BadZipFile, KeyError, OSError):
                    skipped += 1  # a corrupt XML among the candidates
                    continue
                readable.append(entry)
                n = len(spl.ndc_products(root))
                if n > best_products:
                    best, best_products = entry, n
            if not readable:
                raise SliceError(
                    f"dailymed: no readable prescription label under {raw} "
                    f"({skipped} candidate(s) skipped)"
                )
            picks = [readable[0]]
            if best is not None and best not in picks:
                picks.append(best)
            chosen["prescription"] = [(e[1], e[2]) for e in picks]
        if "otc" in kinds:
            ranked = sorted(entries["otc"], key=lambda e: (e[0], e[1].name, e[2]))
            mid = ranked[(len(ranked) - 1) // 2]
            chosen["otc"] = [(mid[1], mid[2])]

        names = {"prescription": "rx", "otc": "otc"}
        files: list[str] = []
        for kind in kinds:
            out = dest / f"dm_spl_release_human_{names[kind]}_part1.zip"
            out.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as dst:
                for outer_path, member in sorted(chosen[kind], key=lambda t: (t[0].name, t[1])):
                    dst.writestr(
                        member, opened[outer_path].read(member), compress_type=zipfile.ZIP_STORED
                    )
            files.append(out.name)
    return {
        "files": files,
        "inputs": list(files),
        "bytes": sum((dest / f).stat().st_size for f in files),
        "labels": {k: [m for _, m in chosen[k]] for k in kinds},
        "skipped_candidates": skipped,
    }


def _pdf_pages(path: Path) -> int | None:
    """The page count of a PDF, or None when it cannot be read."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path), strict=False).pages)
    except Exception:  # noqa: BLE001 - an unreadable PDF is simply not a scan candidate
        return None


def _build_epar(raw: Path, dest: Path) -> dict:
    """Cut the document feed to the chosen documents and link their PDFs: the
    ``EPAR_LARGEST`` largest by size, and the one among the next ``EPAR_SCAN_CANDIDATES``
    with the most bytes per page when that is at least ``EPAR_SCANNED_MIN_BYTES_PER_PAGE``
    (a scan). The feed lists only the chosen documents, so none counts as not downloaded."""
    from episteme.data.ema import download_epar as dl

    feed_path = raw / dl.FEED_FILE
    if not feed_path.is_file():
        raise SliceError(f"ema_epar: the feed file {feed_path} is missing")
    docs = dl.load_documents(feed_path)
    have = [(d, dl.document_path(raw, d["id"])) for d in docs]
    have = [(d, p) for d, p in have if p.is_file()]
    if not have:
        raise SliceError(f"ema_epar: no downloaded PDF of a listed document under {raw}")
    by_size = sorted(have, key=lambda t: (-_size(t[1]), int(t[0]["id"])))
    picked = by_size[:EPAR_LARGEST]
    scan: tuple[float, dict, Path] | None = None
    for d, p in by_size[EPAR_LARGEST : EPAR_LARGEST + EPAR_SCAN_CANDIDATES]:
        pages = _pdf_pages(p)
        if not pages:
            continue
        per_page = _size(p) / pages
        if per_page >= EPAR_SCANNED_MIN_BYTES_PER_PAGE and (scan is None or per_page > scan[0]):
            scan = (per_page, d, p)
    if scan is not None:
        picked.append((scan[1], scan[2]))
    picked.sort(key=lambda t: int(t[0]["id"]))

    feed_dest = dest / dl.FEED_FILE
    feed_dest.parent.mkdir(parents=True, exist_ok=True)
    feed_dest.write_text(
        json.dumps({"meta": {"total_records": len(picked)}, "data": [d for d, _ in picked]}),
        encoding="utf-8",
    )
    files = [dl.FEED_FILE]
    inputs: list[str] = []
    total = feed_dest.stat().st_size
    for d, p in picked:
        target = dl.document_path(dest, d["id"])
        _link_or_copy(p, target)
        rel = target.relative_to(dest).as_posix()
        files.append(rel)
        inputs.append(rel)
        total += _size(target)
    return {"files": files, "inputs": inputs, "bytes": total, "scanned_looking": scan is not None}


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
    "europepmc_preprint": SliceSpec(
        "europepmc_preprint",
        1 * GB,
        _select_europepmc_preprint,
        Path("europepmc/preprints_bulk"),
    ),
    "pubchem": SliceSpec("pubchem", 1 * GB, _select_pubchem, Path("pubchem"), build=_build_pubchem),
    "pmc": SliceSpec(
        "pmc",
        4 * GB,
        _select_pmc,
        Path("pmc/oa_comm"),
        is_unit=lambda p: p.suffix == ".json",
    ),
    "dailymed": SliceSpec(
        "dailymed", 1 * GB, _cut_only("dailymed"), Path("dailymed"), build=_build_dailymed
    ),
    "ema_epar": SliceSpec(
        "ema_epar", 2 * GB, _cut_only("ema_epar"), Path("ema"), build=_build_epar
    ),
    "ema": SliceSpec("ema", 1 * GB, _select_ema, Path("ema")),
    "openfda": SliceSpec("openfda", 2 * GB, _select_openfda, Path("openfda")),
    # The whole raw set is about 105 GB over 90 quarters: the largest year plus the other
    # picks is far above 8 GB. Override with EPISTEME_SLICE_MAX_BYTES_OPENFDA_FAERS.
    "openfda_faers": SliceSpec("openfda_faers", 32 * GB, _select_faers, Path("openfda")),
    # The whole file is the sample: one release zip of about 2.4 GiB (spec 7).
    "aact": SliceSpec("aact", 4 * GB, _select_aact, Path("aact")),
}


def _cap_key(source: str) -> str:
    """The environment key that sets the slice cap of ``source``."""
    return f"EPISTEME_SLICE_MAX_BYTES_{source.upper()}"


def max_bytes_of(source: str) -> int:
    """The size cap of a source's slice: ``EPISTEME_SLICE_MAX_BYTES_<SOURCE>`` when
    set, else the spec's own cap."""
    from episteme.config import get_settings

    spec = _spec(source)
    return get_settings().slice_max_bytes.get(source, spec.max_bytes)


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
        cap = max_bytes_of(source)
        if built["bytes"] > cap:
            raise SliceError(
                f"{source}: slice is {built['bytes']} bytes, over the cap of {cap}; raise the "
                f"cap with {_cap_key(source)}"
            )
        result = {**built, "linked": 0, "copied": len(built["files"])}
        meta_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    files = select(source, raw_root)
    missing = [p for p in files if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"selected file is missing: {missing[0]}")
    total = sum(_size(p) for p in files)
    cap = max_bytes_of(source)
    if total > cap:
        raise SliceError(
            f"{source}: slice is {total} bytes, over the cap of {cap}; raise the cap with "
            f"{_cap_key(source)}"
        )

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
