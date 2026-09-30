#!/usr/bin/env python3
"""Extraction: NCBI Bookshelf — parse each book package (``*.tar.gz`` holding
one ``<book>`` NXML document) into the unified ``episteme.articles`` row schema
(contract v1.4): ONE book row + N book-part rows, linked by ``container_id``.

Unit of work: one input ``.tar.gz`` archive. Per archive: open the tar, find
the single ``.nxml``/``.xml`` member, parse it with ``defusedxml``, build the
book row (``container_id=None``, ``book_meta`` set) and one row per kept
``<book-part>`` (``container_id = <book row id>``, ``book_meta=None``), then
one ``staging_writer.write_rows`` call for the whole archive (book + parts in
one shard), then ``checkpoint_markers.mark_success``/``mark_failed``, then a
best-effort chained ``audit_trail`` row.

NXML is JATS-ish (Book DTD, not Article DTD) -- ``episteme.data.jats`` supplies
the shared local-name / itertext / child-text walking primitives plus
``iter_book_parts`` for the ``<book-part>`` walk; this module does not
reimplement JATS tree-walking.

CRITICAL: ``book_meta`` is stored as a JSON **string** (``json.dumps(...)``),
never a raw ``dict`` -- see the module-level note in the SP2 Task 10 report.
``staging_writer.write_parquet_shard``'s catch-all column branch does
``str(v)`` for anything not in its special-cased lists, which for a bare dict
would produce Python's single-quoted repr (invalid JSON); ``postgres_loader``'s
COPY path has no ``Jsonb(...)`` adapter for a bare dict either. A JSON string
round-trips correctly through both: the parquet writer passes a ``str``
through unchanged, and Postgres's ``COPY ... FROM STDIN`` (text format)
implicitly casts a text value being copied into a ``jsonb`` column, the same
way ``INSERT ... VALUES ('{"a":1}')`` does.

Importable core: ``extract_bookshelf(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory and prints a field-shape table without
writing any shard, marker, manifest or audit row.

Roadmap CLI (§4.7):
  python -m episteme.data.bookshelf.extract_bookshelf \\
    --raw-dir ./01_raw/bookshelf \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
    --workers 4 \\
    [--force]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import tarfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as ET

_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.audit_trail import record as _audit  # noqa: E402
from episteme.config import get_settings  # noqa: E402
from episteme.data.article_schema import (  # noqa: E402
    ARTICLE_COLUMNS,
    MIN_OK_TEXT_LEN,
    SCHEMA_VERSION,
    finalize_row,
    normalize_license,
    subset_from_license,
    utc_now_iso,
)
from episteme.data.checkpoint_markers import (  # noqa: E402
    discover_input_files,
    input_key,
    is_success,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.jats import child_text, iter_book_parts, itertext, local_name  # noqa: E402
from episteme.data.staging_writer import write_rows  # noqa: E402

_LOG = logging.getLogger(__name__)

SOURCE = "bookshelf"

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). extract() itself stays parallel -- only this block serializes.
# Mirrors extract_pubmed.py / extract_pmc.py.
_AUDIT_LOCK = threading.Lock()

# A part whose <book-part-meta><title> matches this (case-insensitive, full
# match) is dropped regardless of its body content -- these are book furniture,
# not content, and none of them are useful units of retrieval.
_SKIP_TITLE_RE = re.compile(r"^(copyright|index|table of contents)$", re.IGNORECASE)


def _find_child(parent: ET.Element | None, name: str) -> ET.Element | None:
    """First direct child of ``parent`` whose local tag name is ``name``."""
    if parent is None:
        return None
    for c in parent:
        if local_name(c.tag) == name:
            return c
    return None


def _license_text(el: ET.Element | None) -> str | None:
    """First ``<license>``/``<copyright-statement>``/``<license-p>`` text found
    anywhere under ``el``, or ``None``. Bookshelf licensing varies book-by-book
    (and is frequently absent on scoped elements) -- the caller feeds this
    through ``normalize_license``, which already falls back to
    ``unknown``/``open_metadata`` on ``None``, the conservative default."""
    if el is None:
        return None
    for node in el.iter():
        if local_name(node.tag) in ("license", "copyright-statement", "license-p"):
            txt = itertext(node)
            if txt:
                return txt
    return None


def _parse_editors(book_meta_el: ET.Element | None) -> list[str]:
    """``<contrib-group>`` -> list of ``"{given-names} {surname}"``.

    Prefers ``<contrib contrib-type="editor">``; if the book carries none with
    that exact type (e.g. type omitted), every ``<contrib>`` in the group is
    used instead -- NCBI Bookshelf book-meta contrib-type usage is not
    universally consistent, and an editor-less list would silently under-report.
    """
    cg = _find_child(book_meta_el, "contrib-group")
    if cg is None:
        return []
    contribs = [c for c in cg if local_name(c.tag) == "contrib"]
    editors = [c for c in contribs if (c.get("contrib-type") or "").strip().lower() == "editor"]
    chosen = editors or contribs
    out: list[str] = []
    for c in chosen:
        name_el = _find_child(c, "name")
        if name_el is None:
            continue
        surname = child_text(name_el, "surname")
        given = child_text(name_el, "given-names")
        if surname or given:
            out.append(f"{given} {surname}".strip() if given else surname)
    return out


def _title_from(parent: ET.Element | None, tag: str, group_tag: str) -> str | None:
    """``<tag>`` text, checked as a direct child of ``parent`` first and, if
    absent, as a direct child of ``parent``'s ``<group_tag>`` instead.

    Real NCBI Bookshelf BITS-book NXML nests titles one level deeper than the
    brief's flat guess (``<book-title-group><book-title>`` at book level,
    ``<title-group><title>`` at book-part level) -- the same shape of bug
    ``jats.py``'s ``parse_jats_fields`` docstring already flags for PMC
    ``<contrib><name>``. A bare ``child_text(parent, tag)`` would silently read
    empty on real data.
    """
    if parent is None:
        return None
    direct = child_text(parent, tag)
    if direct:
        return direct
    group = _find_child(parent, group_tag)
    if group is not None:
        nested = child_text(group, tag)
        if nested:
            return nested
    return None


def _book_year(book_meta_el: ET.Element | None) -> int | None:
    """``<book-meta><pub-date><year>`` -> int, preferring
    ``<pub-date date-type="pub">`` when more than one ``<pub-date>`` is
    present; falls back to any bare ``<year>`` found anywhere under
    ``book-meta`` if no ``<pub-date>`` yields one. Mirrors
    ``jats.parse_jats_fields``'s ``int(...[:4])``-with-fallback year parsing."""
    if book_meta_el is None:
        return None
    pub_dates = [c for c in book_meta_el if local_name(c.tag) == "pub-date"]
    ordered = sorted(pub_dates, key=lambda c: (c.get("date-type") or "") != "pub")
    for pd in ordered:
        y = child_text(pd, "year")
        if y:
            try:
                return int(y[:4])
            except ValueError:
                pass
    for node in book_meta_el.iter():
        if local_name(node.tag) == "year":
            y = itertext(node)
            if y:
                try:
                    return int(y[:4])
                except ValueError:
                    pass
    return None


def _part_id(bp: ET.Element, meta: ET.Element | None, fallback: str) -> str:
    """``<book-part>``'s stable identifier: the ``id`` attribute when present,
    else ``<book-part-meta><book-part-id>`` text (real Bookshelf BITS-book
    parts frequently carry no ``id`` attribute at all -- the identifier lives
    in ``book-part-id`` instead), else a positional fallback."""
    explicit = bp.get("id")
    if explicit:
        return explicit
    if meta is not None:
        bp_id = child_text(meta, "book-part-id")
        if bp_id:
            return bp_id
    return fallback


def _part_title(bp: ET.Element) -> str:
    meta = _find_child(bp, "book-part-meta")
    return _title_from(meta, "title", "title-group") or ""


def _should_skip_part(title: str, body_el: ET.Element | None) -> bool:
    """Decide whether a ``<book-part>`` is content worth keeping.

    Skip when: the part's ``<body>`` text is empty/whitespace-only; the title
    (case-insensitive, full match) is copyright/index/table-of-contents
    furniture; or the body's only child is an ``<index>`` element whose own
    ``<p>`` content never clears ``MIN_OK_TEXT_LEN`` (an index with no
    substantial prose is not a useful retrieval unit even if untitled)."""
    text = itertext(body_el)
    if not text.strip():
        return True
    if _SKIP_TITLE_RE.match(title.strip()):
        return True
    if body_el is not None:
        children = list(body_el)
        if len(children) == 1 and local_name(children[0].tag) == "index":
            idx_el = children[0]
            p_texts = [itertext(p) for p in idx_el.iter() if local_name(p.tag) == "p"]
            if not any(len(t) >= MIN_OK_TEXT_LEN for t in p_texts):
                return True
    return False


_NBK_RE = re.compile(r"NBK\d+")


def _accession_from_filename(path: Path) -> str:
    """The archive's Bookshelf accession id.

    The real NCBI LitArch download layout (``download_bookshelf.sh``) names
    packages ``<report-id>_<NBK-accession>.tar.gz`` (e.g.
    ``tr826967112990310_NBK599773.tar.gz``) -- the ``NBK\\d+`` accession does
    not appear anywhere inside the NXML itself (checked against a real
    package), so it must come from the filename. ``NBK\\d+`` is pulled out of
    the ``.tar.gz``-stripped stem when present; otherwise (e.g. this task's
    ``NBK1.tar.gz`` fixture, or a filename with no NBK-shaped token) the whole
    stem is used as-is.
    """
    name = path.name
    stem = name[: -len(".tar.gz")] if name.endswith(".tar.gz") else Path(name).stem
    m = _NBK_RE.search(stem)
    return m.group(0) if m else stem


def _id_key_from_filename(path: Path, raw_dir: Path) -> str:
    """The token used to build this archive's row ``id`` (and its parts'
    ``container_id``): the ``NBK\\d+`` accession when the filename carries
    one -- same value as ``_accession_from_filename`` for that case, id
    unchanged -- otherwise ``checkpoint_markers.input_key(path, raw_dir)``
    with a trailing ``.tar.gz`` stripped.

    ``_accession_from_filename`` falls back to the bare filename stem when no
    NBK token is present, which two unrelated archives sharing a basename in
    different subfolders (there is no such guarantee under the real hashed
    LitArch ``packages/<rel>`` tree) collapse onto identically -- and
    ``postgres_loader``'s (d0) id-collision guard then keeps only the
    last-loaded one at load time. ``input_key`` is already unique per
    subfolder (it is the path relative to ``raw_dir``, joined with ``__``),
    so using it here instead of the bare stem keeps NBK-less archives from
    colliding on ``id`` without touching the NBK branch at all.
    """
    name = path.name
    stem = name[: -len(".tar.gz")] if name.endswith(".tar.gz") else Path(name).stem
    m = _NBK_RE.search(stem)
    if m:
        return m.group(0)
    key = input_key(path, raw_dir)
    if key.endswith(".tar.gz"):
        key = key[: -len(".tar.gz")]
    return key


def _find_nxml_member(tar: tarfile.TarFile, nbk: str) -> tarfile.TarInfo:
    members = [
        m for m in tar.getmembers() if m.isfile() and m.name.lower().endswith((".nxml", ".xml"))
    ]
    if not members:
        raise ValueError("no .nxml/.xml member in archive")
    # Prefer the member whose stem matches the accession id (the real Bookshelf
    # layout nests it as <NBK>/<NBK>.nxml); otherwise take the first found.
    for m in members:
        if Path(m.name).stem == nbk:
            return m
    return members[0]


def parse_book_archive(path: Path, raw_dir: Path) -> list[dict[str, Any]]:
    """One ``.tar.gz`` book package -> ``[book_row, *kept_part_rows]``, each a
    finalized ``episteme.articles`` row."""
    nbk = _accession_from_filename(path)
    # id_key drives the row `id` / part `container_id`, kept distinct from
    # `nbk` (the accession used for member lookup and source_record_id
    # below): see _id_key_from_filename for why the two diverge for
    # NBK-less filenames.
    id_key = _id_key_from_filename(path, raw_dir)
    # input_key, not path.name: two packages sharing a basename in different
    # hash-bucket subdirectories under raw_dir (the real LitArch
    # packages/<rel> tree -- see discover_bookshelf_files/process_one below)
    # must not collapse to the same source_file value.
    source_file = input_key(path, raw_dir)

    with tarfile.open(path, "r:gz") as tar:
        member = _find_nxml_member(tar, nbk)
        fh = tar.extractfile(member)
        if fh is None:
            raise ValueError(f"cannot extract {member.name} from {source_file}")
        root = ET.parse(fh).getroot()

    book_meta_el = _find_child(root, "book-meta")
    title = _title_from(book_meta_el, "book-title", "book-title-group")

    publisher = None
    if book_meta_el is not None:
        pub_el = _find_child(book_meta_el, "publisher")
        if pub_el is not None:
            publisher = child_text(pub_el, "publisher-name") or None

    book_meta_dict: dict[str, Any] = {
        "isbn": (child_text(book_meta_el, "isbn") or None) if book_meta_el is not None else None,
        "editors": _parse_editors(book_meta_el) or None,
        "publisher": publisher,
        "edition": (child_text(book_meta_el, "edition") or None)
        if book_meta_el is not None
        else None,
        "n_parts": None,  # filled below, after the part walk
    }

    toc_el = _find_child(root, "toc")
    front_el = _find_child(root, "front")

    book_text_parts = [t for t in (itertext(toc_el), itertext(front_el)) if t]
    if not book_text_parts:
        # Real single-section Bookshelf packages (e.g. CADTH HTA reports; see
        # the Task 10 report) carry no book-level <toc>/<front> at all -- the
        # only book-level prose is <book-meta><abstract> (a plain-language
        # summary, distinct from the one <book-part>'s technical body text).
        # Falling back to it keeps the book row from going text-less on real
        # data while still never pulling in chapter/part body content.
        abstract_el = _find_child(book_meta_el, "abstract") if book_meta_el is not None else None
        if abstract_el is not None:
            abs_text = itertext(abstract_el)
            if abs_text:
                book_text_parts = [abs_text]
    book_text = "\n\n".join(book_text_parts) or None

    book_license_text = _license_text(book_meta_el) or _license_text(root)
    book_lic, book_lic_url, book_lic_raw = normalize_license(book_license_text)
    book_subset = subset_from_license(book_lic)
    book_year = _book_year(book_meta_el)

    part_rows: list[dict[str, Any]] = []
    # NOTE: no ``<body> is not None`` guard here -- real BITS-book packages
    # root at <book-part-wrapper>, which has no book-level <body> of its own
    # (the single <book-part> sits directly under the wrapper); iter_book_parts
    # walks the whole tree regardless, so gating on a book-level <body> here
    # silently dropped every part row on real data (found live, see report).
    for bp in iter_book_parts(root):
        meta = _find_child(bp, "book-part-meta")
        bp_id = _part_id(bp, meta, f"part{len(part_rows) + 1}")
        part_title = _part_title(bp) or None
        part_body_el = _find_child(bp, "body")
        if _should_skip_part(part_title or "", part_body_el):
            continue
        part_text = itertext(part_body_el) or None
        # Inherit the book's license when the part carries none of its own
        # (the book-level <permissions> almost always covers the whole work;
        # per-part license text is rare) -- one of the two brief-sanctioned
        # options ("inherit the book's license").
        p_lic, p_lic_url, p_lic_raw = normalize_license(_license_text(bp) or book_license_text)
        p_subset = subset_from_license(p_lic)
        part_rows.append(
            finalize_row(
                {
                    "id": f"bookshelf:{id_key}:{bp_id}",
                    "source": SOURCE,
                    "source_file": source_file,
                    "source_record_id": bp_id,
                    "pmid": None,
                    "pmcid": None,
                    "doi": None,
                    "title": part_title,
                    "text": part_text,
                    "authors": None,
                    "journal": None,
                    # Parts inherit the book's publication year (no per-part
                    # pub-date exists in practice) -- keeps them out of the
                    # articles_bookshelf_y0 sentinel partition alongside the
                    # book row, and year is a bibliographic fact of the book,
                    # not of the chapter.
                    "year": book_year,
                    "mesh": None,
                    "language": None,
                    "license": p_lic,
                    "license_url": p_lic_url,
                    "license_raw": p_lic_raw,
                    "subset": p_subset,
                    "container_id": f"bookshelf:{id_key}",
                    "book_meta": None,
                }
            )
        )

    book_meta_dict["n_parts"] = len(part_rows)

    book_row = finalize_row(
        {
            "id": f"bookshelf:{id_key}",
            "source": SOURCE,
            "source_file": source_file,
            "source_record_id": nbk,
            "pmid": None,
            "pmcid": None,
            "doi": None,
            "title": title,
            "text": book_text,
            "authors": None,
            "journal": None,
            "year": book_year,
            "mesh": None,
            "language": None,
            "license": book_lic,
            "license_url": book_lic_url,
            "license_raw": book_lic_raw,
            "subset": book_subset,
            "container_id": None,
            "book_meta": json.dumps(book_meta_dict, ensure_ascii=False),
        }
    )

    return [book_row, *part_rows]


def discover_bookshelf_files(raw_dir: Path) -> list[Path]:
    """``*.tar.gz`` book packages under ``raw_dir``, deduped by resolved full
    path (never by basename) via ``checkpoint_markers.discover_input_files``.

    The real download layout (``download_bookshelf.sh``) nests packages under
    ``packages/<rel-path>`` from NCBI's hashed LitArch tree, not directly
    under ``raw_dir`` -- and, because that tree is bucketed by a hash with no
    usable directory listing, nothing rules out two packages sharing an
    identical basename under different buckets (e.g. a re-released package
    landing in a new bucket while the old one is still on disk). Sorting by
    ``input_key`` (not raw path string) keeps discovery order deterministic
    the same way it keys checkpoint markers/``source_file`` in ``process_one``
    below -- mirrors ``extract_pmc.discover_meta_files`` (Task 9).
    """
    files = discover_input_files(raw_dir, ["*.tar.gz"])
    files.sort(key=lambda p: input_key(p, raw_dir))
    return files


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror extract_pubmed.py / extract_pmc.py: one chained ``extract_commit``
    audit row on a fresh connection, degrading to the file-only mirror when no
    DB is reachable. Never raises -- ``mark_success`` has already run."""
    with _AUDIT_LOCK:
        try:
            from episteme.data.db.connection import connection as _pg_connection

            with _pg_connection() as _conn:
                _audit(
                    "extract_commit",
                    conn=_conn,
                    object=f"{SOURCE} {basename}",
                    rows_affected=n_rows,
                    reason=None,
                    run_id=get_settings().run_id,
                )
                _conn.commit()
        except Exception:  # noqa: BLE001 - best-effort: DB unavailable or audit failed
            try:
                from episteme import audit_trail as _audit_trail

                _audit_trail.mirror_only(
                    "extract_commit",
                    object=f"{SOURCE} {basename}",
                    rows_affected=n_rows,
                    note="db_unavailable_or_failed",
                    run_id=get_settings().run_id,
                )
            except Exception:  # noqa: BLE001 - last-resort fallback must never escape
                _LOG.warning(
                    "audit mirror_only fallback also failed for %s", basename, exc_info=True
                )


def process_one(
    path: Path,
    *,
    raw_dir: Path,
    processed_dir: Path,
    force: bool,
) -> dict[str, Any]:
    basename = input_key(path, raw_dir)
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    try:
        rows = parse_book_archive(path, raw_dir)
        status_counts: Counter[str] = Counter(str(r.get("extract_status")) for r in rows)

        write_info = write_rows(
            rows,
            processed_dir,
            source=SOURCE,
            source_file=basename,
            prefer_parquet=True,
        )
        elapsed = round(time.time() - t0, 4)
        stats = {
            "n_rows": len(rows),
            "extract_status_counts": dict(status_counts),
            "elapsed_sec": elapsed,
            "write": write_info,
        }
        mark_success(processed_dir, SOURCE, basename, stats=stats)
        _best_effort_audit(basename, len(rows))
        return {"source_file": basename, "skipped": False, "ok": True, **stats}
    except Exception as e:  # noqa: BLE001
        mark_failed(
            processed_dir,
            SOURCE,
            basename,
            error_class="parse_error" if "Parse" in type(e).__name__ else "unknown",
            message=str(e),
            stats={},
            exc=e,
        )
        return {"source_file": basename, "skipped": False, "ok": False, "error": str(e)}


def _print_verbose(result: dict[str, Any]) -> None:
    basename = result.get("source_file")
    if result.get("skipped"):
        print(f"skip {basename}", file=sys.stderr)
    elif result.get("ok"):
        print(
            f"ok {basename} rows={result.get('n_rows')} "
            f"status={result.get('extract_status_counts')}",
            file=sys.stderr,
        )
    else:
        print(f"FAIL {basename}: {result.get('error')}", file=sys.stderr)


def extract_bookshelf(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse ``*.tar.gz`` book packages under ``raw_dir`` into
    staging shards + ops markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_bookshelf_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]

    workers = max(1, int(workers))
    results: list[dict[str, Any]] = []
    t0 = time.time()

    if workers == 1:
        for fp in files:
            result = process_one(fp, raw_dir=raw_dir, processed_dir=processed_dir, force=force)
            results.append(result)
            if verbose:
                _print_verbose(result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(
                    process_one, fp, raw_dir=raw_dir, processed_dir=processed_dir, force=force
                ): fp
                for fp in files
            }
            for fut in as_completed(futs):
                result = fut.result()
                results.append(result)
                if verbose:
                    _print_verbose(result)

    ok = sum(1 for r in results if r.get("ok"))
    failed = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))
    rows = sum(int(r.get("n_rows") or 0) for r in results if r.get("ok"))
    summary = {"inputs": len(files), "ok": ok, "failed": failed, "rows": rows}

    run_id = get_settings().run_id or utc_now_iso().replace(":", "").replace("-", "")
    try:
        write_run_manifest(
            processed_dir,
            SOURCE,
            run_id,
            config={
                "raw_dir": str(raw_dir),
                "max_files": max_files,
                "workers": workers,
                "force": force,
            },
            totals={
                **summary,
                "skipped": sum(1 for r in results if r.get("skipped")),
                "elapsed_sec": round(time.time() - t0, 3),
            },
        )
    except Exception:  # noqa: BLE001 -- manifest is best-effort, never fail the run on it
        pass

    return summary


# --------------------------------------------------------------------------- #
# --report: parse-only field-shape table (no shard / marker / manifest / audit)
# --------------------------------------------------------------------------- #

_SAMPLE_MAX = 4
_SAMPLE_TRUNC = 60


def _stringify(value: object) -> str:
    text = str(value)
    if len(text) > _SAMPLE_TRUNC:
        text = text[: _SAMPLE_TRUNC - 3] + "..."
    return text.replace("\n", " ")


def _render_field_shape(rows: list[dict[str, Any]], n_files: int) -> str:
    n = len(rows)
    lines = [
        f"field-shape report - source={SOURCE} schema={SCHEMA_VERSION}",
        f"files={n_files}  rows={n}",
        "",
    ]
    col_w = max((len(c) for c in ARTICLE_COLUMNS), default=6)
    header = f"{'column':<{col_w}}  {'non-null%':>9}  {'distinct':>8}  samples"
    lines.append(header)
    lines.append("-" * len(header))
    for col in ARTICLE_COLUMNS:
        vals = [r.get(col) for r in rows]
        non_null = [v for v in vals if v is not None]
        pct = round(100.0 * len(non_null) / n, 1) if n else 0.0
        distinct = len({repr(v) for v in non_null})
        samples = "; ".join(_stringify(v) for v in non_null[:_SAMPLE_MAX])
        lines.append(f"{col:<{col_w}}  {pct:>9}  {distinct:>8}  {samples}")

    lines += ["", "extract_status histogram:"]
    hist = Counter(str(r.get("extract_status")) for r in rows)
    for status, cnt in sorted(hist.items()):
        lines.append(f"  {status:<10} {cnt}")

    lines += ["", "subset / license breakdown:"]
    sub = Counter(str(r.get("subset")) for r in rows)
    lic = Counter(str(r.get("license")) for r in rows)
    for k, c in sorted(sub.items()):
        lines.append(f"  subset={k:<16} {c}")
    for k, c in sorted(lic.items()):
        lines.append(f"  license={k:<16} {c}")

    lines += ["", "book rows (container_id IS NULL) vs part rows:"]
    n_book = sum(1 for r in rows if r.get("container_id") is None)
    lines.append(f"  book={n_book} part={n - n_book}")

    return "\n".join(lines) + "\n"


def _run_report(raw_dir: Path, max_files: int) -> int:
    files = discover_bookshelf_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no *.tar.gz under {raw_dir}", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    for fp in files:
        rows.extend(parse_book_archive(fp, raw_dir))
    print(_render_field_shape(rows, len(files)), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(
        prog="python -m episteme.data.bookshelf.extract_bookshelf",
        description="NCBI Bookshelf .tar.gz packages to episteme.articles staging shards",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "bookshelf")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered *.tar.gz (env: EPISTEME_SAMPLE_LIMIT)",
    )
    p.add_argument("--workers", type=int, default=1, help="Parallel archive workers")
    p.add_argument("--force", action="store_true")
    p.add_argument("--verbose", action="store_true", help="print ok/skip/FAIL per file to stderr")
    p.add_argument(
        "--report",
        action="store_true",
        help="parse only; print a field-shape table; write NO shard/marker/manifest/audit",
    )
    args = p.parse_args(argv)

    raw_dir: Path = args.raw_dir
    processed_dir: Path = args.processed_dir

    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    if args.report:
        return _run_report(raw_dir, args.max_files)

    print(f"schema={SCHEMA_VERSION} source={SOURCE}")
    print(f"raw_dir={raw_dir.resolve()} workers={max(1, args.workers)}")

    res = extract_bookshelf(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no *.tar.gz under {raw_dir}", file=sys.stderr)
        return 1

    print(f"done inputs={res['inputs']} ok={res['ok']} failed={res['failed']} rows={res['rows']}")
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
