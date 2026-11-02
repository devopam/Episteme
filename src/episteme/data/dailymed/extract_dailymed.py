#!/usr/bin/env python3
"""Extraction: DailyMed SPL labels into ``episteme.articles`` rows, in bounded parts.

Input: the DailyMed zips in ``raw_dir`` (zips of zips). A full release
(``dm_spl_release_human_rx_partN.zip`` for the ``prescription`` kind,
``dm_spl_release_human_otc_partN.zip`` for ``otc``) and an update
(``dm_spl_{monthly,weekly,daily}_update_*.zip``) hold inner zips named
``prescription/<date>_<id>.zip`` or ``otc/...``; an inner zip holds one XML (its name is
NOT the ``setId``) and images. Which kinds are read is ``Settings.dailymed_kinds``
(``EPISTEME_DAILYMED_KINDS``). Animal, homeopathic and remainder releases and the
indexing files are not read in this cut.

Two passes, so that a label is parsed once and no XML waits in memory:

1. **Scan** (``scan_zips``). Each wanted inner zip is read in file order, its XML
   head is parsed for ``setId`` and ``versionNumber`` only, and the label is noted in
   a dict ``setId -> (version, outer zip index, inner path, crc, size)``. The highest
   version wins, across releases and updates. This dict is the only state of the
   scan: about 150,000 labels at roughly 350 bytes (the key and the inner path are
   strings) is 50 to 80 MB. The scan reads every inner zip once, in sequence, because
   the central directory of an inner zip is at its end.
2. **Buckets**. The winners are grouped by the first two hex characters of the
   ``setId`` (256 buckets; the width is fixed and checked, ``check_bucket_scheme``).
   A bucket manifest lists its labels. ``process_bucket`` reads them back grouped by
   outer zip, in file order (one open per outer zip per bucket), parses each label
   (``spl.parse_spl``) and writes the rows in parts ``dailymed_bXX__partNNNNN``, capped
   by the profile's ``part_rows`` / ``part_bytes`` (the PMC rules: a row that would take
   a part over the bytes starts the next part; one row over the cap is alone). Peak
   memory with one worker is the dict of the scan (freed before the buckets run) or,
   in a bucket, one inner zip plus the rows of one part.

A part gets a shard, a sidecar (``labels.py``: sections as offsets into ``body_text``,
and substances), an ``extract_commit`` audit row, then a success marker. After the last
part the bucket gets its marker (``parts``, ``parts_list``, ``inputs`` = the manifest
fingerprint). A bucket whose ``inputs`` equal the fingerprint is skipped (unless
``--force``); any other is written again from part 1 after its old parts are deleted
(the names of loaded parts go to ``retire_pending.d/<bucket key>.json`` first).

A label whose inner zip is corrupt or has no XML, or whose XML does not parse, is left
out, listed in the bucket's ``failed_labels`` (or, when the scan failed it, in the run's
count), given a failed marker keyed on ``label_key`` and counted in
``_ops/dailymed/failed_inputs.json``. The run goes on. An OUTER zip that cannot be
opened is different: the labels in it are unknown, and a scan without them would
retire them. The run stops (``DailymedScanError``) before it writes or removes anything.

**Retire.** A bucket is written again whenever its label set changes, so a label that
is no longer in any zip of the scan leaves its part (the part is loaded again by name,
or deleted and retired). A bucket with no label left is removed with its parts. The
parts that had been loaded go to the retire pending file, and ``parts_retire`` deletes
their rows (and, through ``postgres_loader.delete_source_files``, their label rows)
with an audit row. Absence can only be judged on what the zips hold: a label withdrawn
only by being missing from the updates cannot be seen, so a ``setId`` that an update
holds and no full release holds stays. The withdrawals that a full release shows are
the ones that the replacement of the release zips brings.

Importable core: ``extract_dailymed(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False, skip_units=None, skip_reason=None,
release_kinds=None) -> dict``. ``main()`` is the thin CLI wrapper.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import io
import json
import logging
import re
import sys
import threading
import time
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.audit_trail import record as _audit  # noqa: E402
from episteme.config import get_settings  # noqa: E402
from episteme.data import parts_retire, staging_writer  # noqa: E402
from episteme.data.article_schema import (  # noqa: E402
    LICENSE_PUBLIC_DOMAIN,
    SCHEMA_VERSION,
    finalize_row,
    subset_from_license,
    utc_now_iso,
)
from episteme.data.checkpoint_markers import (  # noqa: E402
    discover_input_files,
    failed_marker_path,
    input_key,
    is_success,
    load_success_marker_path,
    mark_failed,
    mark_success,
    ops_root,
    success_marker_path,
    write_marker,
    write_run_manifest,
)
from episteme.data.dailymed import labels, spl  # noqa: E402
from episteme.data.parallel import (  # noqa: E402
    default_workers,
    record_first_failure,
    run_per_file,
)
from episteme.data.pmc.buckets import key_and_rel  # noqa: E402
from episteme.data.staging_writer import write_rows  # noqa: E402
from episteme.ops.profile import PART_ROW_OVERHEAD_BYTES, get_profile  # noqa: E402
from episteme.ops.retry import TransientUnitError, is_transient  # noqa: E402
from episteme.ops.skip import add_skip_args, check_skip_args  # noqa: E402
from episteme.ops.stop import stage_guard  # noqa: E402

_LOG = logging.getLogger(__name__)

SOURCE = "dailymed"
# File name the sample gate reads (``gate.FAILED_INPUTS_FILE``; a test keeps them equal).
FAILED_INPUTS_FILE = "failed_inputs.json"
# A bucket is the first BUCKET_PREFIX_CHARS hex characters of the setId (16**2 = 256).
BUCKET_PREFIX_CHARS = 2
LICENSE_RAW = "U.S. government work: DailyMed (NLM) / FDA structured product labeling"

_RELEASE_RE = re.compile(r"^dm_spl_release_human_(rx|otc)_part\d+\.zip$")
_UPDATE_RE = re.compile(r"^dm_spl_(monthly|weekly|daily)_update_.+\.zip$")
_RELEASE_KIND = {"rx": "prescription", "otc": "otc"}

_AUDIT_LOCK = threading.Lock()
_WRITE_BATCH_ROWS = staging_writer.WRITE_BATCH_ROWS
_FAILED_LABELS_LISTED = 100


class DailymedScanError(RuntimeError):
    """The scan cannot be trusted (an outer zip is unreadable); nothing was written."""


class DailymedBucketError(ValueError):
    """A permanent error in the bucket machinery; it fails the bucket."""


# ---------------------------------------------------------------------------
# Keys and buckets
# ---------------------------------------------------------------------------
def label_key(outer: Path, raw_dir: Path, member: str) -> str:
    """The input key of one label: the ``input_key`` of its outer zip, then the inner
    path (``/`` as ``__``). Two outer zips of one name in two directories, or one
    inner name in two outer zips, give different keys."""
    return f"{input_key(outer, raw_dir)}__{member.replace('/', '__')}"


def bucket_prefix(set_id: str) -> str:
    return set_id[:BUCKET_PREFIX_CHARS]


def bucket_key(prefix: str) -> str:
    return f"dailymed_b{prefix}"


def part_name(prefix: str, n: int) -> str:
    return f"{bucket_key(prefix)}__part{n:05d}"


def check_bucket_scheme(processed_dir: Path) -> int:
    """Record the bucket prefix width of this processed dir at the first run and refuse
    a run at another width, before anything is written (a different width gives
    different bucket keys: the old parts and rows would stay, and the same labels
    would load again under the new keys). Returns the width."""
    path = ops_root(processed_dir, SOURCE) / "bucket_scheme.json"
    if not path.is_file():
        write_marker(path, {"prefix_chars": BUCKET_PREFIX_CHARS})
        return BUCKET_PREFIX_CHARS
    stored = int(json.loads(path.read_text(encoding="utf-8"))["prefix_chars"])
    if stored != BUCKET_PREFIX_CHARS:
        raise RuntimeError(
            f"DailyMed bucket prefix is {BUCKET_PREFIX_CHARS} characters but {processed_dir} "
            f"was built at {stored}. A different prefix gives different bucket keys and would "
            f"load the same labels twice. Retire the old dailymed parts first."
        )
    return stored


def _manifest_dir(processed_dir: Path) -> Path:
    return ops_root(processed_dir, SOURCE) / "buckets"


def _manifest_path(processed_dir: Path, prefix: str) -> Path:
    return _manifest_dir(processed_dir) / f"{bucket_key(prefix)}.json"


# ---------------------------------------------------------------------------
# Pass 1: the scan
# ---------------------------------------------------------------------------
def discover_outer_zips(raw_dir: Path, release_kinds: tuple[str, ...]) -> list[Path]:
    """The outer zips to read, sorted by ``input_key``: the full releases of the
    selected kinds, and every update. Indexing files and the animal, homeopathic and
    remainder releases are not wanted."""
    raw_resolved = Path(raw_dir).resolve()
    found: list[Path] = []
    for p in discover_input_files(raw_dir, ["dm_spl_*.zip"]):
        name = p.name
        if "indexing" in name:
            continue
        m = _RELEASE_RE.match(name)
        if m:
            if _RELEASE_KIND[m.group(1)] in release_kinds:
                found.append(p)
        elif _UPDATE_RE.match(name):
            found.append(p)
    found.sort(key=lambda p: key_and_rel(p, raw_resolved)[0])
    return found


def _wanted_member(info: zipfile.ZipInfo, release_kinds: tuple[str, ...]) -> bool:
    name = info.filename
    return (
        not info.is_dir()
        and name.lower().endswith(".zip")
        and any(name.startswith(f"{k}/") for k in release_kinds)
    )


def _xml_name(zf: zipfile.ZipFile) -> str:
    names = [
        i.filename for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".xml")
    ]
    if len(names) != 1:
        raise ValueError(f"expected one XML file in the inner zip, found {len(names)}")
    return names[0]


def _identity(inner_bytes: bytes) -> tuple[str, int]:
    with zipfile.ZipFile(io.BytesIO(inner_bytes)) as zf:
        with zf.open(_xml_name(zf)) as f:
            return spl.read_identity(f)


def _xml_bytes(inner_bytes: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(inner_bytes)) as zf:
        return zf.read(_xml_name(zf))


@dataclass
class Scan:
    """The result of pass 1. ``winners`` maps a ``setId`` to ``(version, outer index,
    inner path, crc, size)``: numbers and short strings only, never XML."""

    outers: list[Path]
    outer_keys: list[str]
    outer_rels: list[str]
    winners: dict[str, tuple[int, int, str, int, int]] = field(default_factory=dict)
    superseded: Counter = field(default_factory=Counter)  # per bucket prefix
    duplicates: Counter = field(default_factory=Counter)
    failed: list[dict[str, str]] = field(default_factory=list)  # at most _FAILED_LABELS_LISTED
    n_failed: int = 0
    unreadable_outers: list[str] = field(default_factory=list)


def scan_zips(
    raw_dir: Path,
    release_kinds: tuple[str, ...],
    on_failed: Callable[[str, BaseException], None] | None = None,
) -> Scan:
    """Pass 1 (see the module docstring). ``on_failed(label_key, exc)`` is called for
    each label that cannot be read; an outer zip that cannot be opened is listed in
    ``unreadable_outers`` and the scan goes on to the next."""
    raw_dir = Path(raw_dir)
    raw_resolved = raw_dir.resolve()
    outers = discover_outer_zips(raw_dir, release_kinds)
    pairs = [key_and_rel(p, raw_resolved) for p in outers]
    scan = Scan(outers, [k for k, _ in pairs], [r for _, r in pairs])
    for oi, path in enumerate(outers):
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as e:
            scan.unreadable_outers.append(f"{scan.outer_keys[oi]}: {e}")
            continue
        with zf:
            infos = [i for i in zf.infolist() if _wanted_member(i, release_kinds)]
            infos.sort(key=lambda i: i.header_offset)
            for info in infos:
                try:
                    set_id, version = _identity(zf.read(info))
                except Exception as e:  # noqa: BLE001 - one label; transient ones stop the run
                    if is_transient(e):
                        raise
                    key = label_key(path, raw_dir, info.filename)
                    scan.n_failed += 1
                    if len(scan.failed) < _FAILED_LABELS_LISTED:
                        scan.failed.append({"file": key, "error": str(e)})
                    if on_failed is not None:
                        on_failed(key, e)
                    continue
                prefix = bucket_prefix(set_id)
                cand = (version, oi, info.filename, info.CRC, info.file_size)
                cur = scan.winners.get(set_id)
                if cur is None:
                    scan.winners[set_id] = cand
                elif version > cur[0]:
                    scan.superseded[prefix] += 1
                    scan.winners[set_id] = cand
                elif version < cur[0]:
                    scan.superseded[prefix] += 1
                else:
                    # The same setId and version in two inner zips: keep the first by
                    # (outer key, inner path), so the choice does not depend on the order.
                    scan.duplicates[prefix] += 1
                    if (scan.outer_keys[oi], info.filename) < (scan.outer_keys[cur[1]], cur[2]):
                        scan.winners[set_id] = cand
    return scan


def _entry(scan: Scan, raw_dir: Path, set_id: str, w: tuple[int, int, str, int, int]) -> dict:
    version, oi, member, crc, size = w
    return {
        "set_id": set_id,
        "version": version,
        "outer": scan.outer_rels[oi],
        "member": member,
        "key": label_key(scan.outers[oi], raw_dir, member),
        "crc": crc,
        "size": size,
    }


def _write_manifests(
    processed_dir: Path, raw_dir: Path, scan: Scan
) -> tuple[dict[str, Path], dict[str, int], int]:
    """One manifest per bucket. Returns ``(prefix -> manifest path, prefix -> count of
    labels in it, count of labels that left their bucket since the last manifest)``."""
    by_prefix: dict[str, list[dict]] = {}
    for set_id in sorted(scan.winners):
        by_prefix.setdefault(bucket_prefix(set_id), []).append(
            _entry(scan, raw_dir, set_id, scan.winners[set_id])
        )
    paths: dict[str, Path] = {}
    counts: dict[str, int] = {}
    withdrawn = 0
    for prefix in sorted(by_prefix):
        entries = by_prefix[prefix]
        path = _manifest_path(processed_dir, prefix)
        with contextlib.suppress(OSError, ValueError, KeyError):
            old = json.loads(path.read_text(encoding="utf-8"))
            withdrawn += len({e["set_id"] for e in old["labels"]} - {e["set_id"] for e in entries})
        payload = {
            "bucket": prefix,
            "key": bucket_key(prefix),
            "labels": entries,
            "superseded_versions": scan.superseded.get(prefix, 0),
            "duplicate_files": scan.duplicates.get(prefix, 0),
            # Content identity of the inputs: the label, its version, its place, and the
            # crc and size of the inner zip as the outer zip's directory records them.
            "fingerprint": [
                [e["set_id"], e["version"], e["key"], e["crc"], e["size"]] for e in entries
            ],
        }
        write_marker(path, payload)
        paths[prefix] = path
        counts[prefix] = len(entries)
    return paths, counts, withdrawn


def _retire_vanished_buckets(processed_dir: Path, live: set[str]) -> tuple[int, int]:
    """Remove the buckets that have a manifest but no label in this scan: delete their
    parts (the loaded ones go to the retire pending file), markers and manifest.
    Returns ``(buckets removed, labels withdrawn)``."""
    d = _manifest_dir(processed_dir)
    if not d.is_dir():
        return 0, 0
    removed = withdrawn = 0
    for p in sorted(d.glob(f"{bucket_key('')}*.json")):
        prefix = p.stem[len(bucket_key("")) :]
        if prefix in live:
            continue
        with contextlib.suppress(OSError, ValueError, KeyError):
            withdrawn += len(json.loads(p.read_text(encoding="utf-8"))["labels"])
        success_marker_path(processed_dir, SOURCE, bucket_key(prefix)).unlink(missing_ok=True)
        _delete_all_parts(processed_dir, bucket_key(prefix))
        p.unlink(missing_ok=True)
        removed += 1
    return removed, withdrawn


# ---------------------------------------------------------------------------
# Reading the labels of one outer zip, in file order
# ---------------------------------------------------------------------------
def _iter_outer(
    path: Path,
    entries: list[dict],
    on_error: Callable[[dict, BaseException], None],
) -> Iterator[tuple[dict, bytes]]:
    """``(entry, XML bytes)`` for each entry of one outer zip, read in file order
    with one open of the outer zip. An entry that cannot be read (member missing or
    changed since the scan, corrupt inner zip) goes to ``on_error`` and is left out."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        if is_transient(e):
            raise
        for entry in entries:
            on_error(entry, e)
        return
    with zf:
        ordered: list[tuple[int, dict, zipfile.ZipInfo]] = []
        for entry in entries:
            try:
                info = zf.getinfo(entry["member"])
            except KeyError as e:
                on_error(entry, e)
                continue
            if info.CRC != entry["crc"] or info.file_size != entry["size"]:
                on_error(entry, ValueError("the inner zip changed since the scan"))
                continue
            ordered.append((info.header_offset, entry, info))
        ordered.sort(key=lambda t: t[0])
        for _off, entry, info in ordered:
            try:
                xml = _xml_bytes(zf.read(info))
            except Exception as e:  # noqa: BLE001
                if is_transient(e):
                    raise
                on_error(entry, e)
                continue
            yield entry, xml


def iter_labels(
    raw_dir: Path,
    release_kinds: tuple[str, ...],
    *,
    failed: list[dict[str, str]] | None = None,
) -> Iterator[tuple[str, bytes]]:
    """``(label key, XML bytes)`` of the newest version of each label, streamed from
    the zips of zips: a scan for the winners, then each outer zip read once in file
    order. Images are never read. Labels that cannot be read are appended to ``failed``
    (``{"file", "error"}``) when it is given, else they raise."""
    raw_dir = Path(raw_dir)
    scan = scan_zips(raw_dir, release_kinds, on_failed=None)
    if scan.unreadable_outers:
        raise DailymedScanError("; ".join(scan.unreadable_outers))
    for item in scan.failed:
        if failed is not None:
            failed.append(item)
    by_outer: dict[int, list[dict]] = {}
    for set_id in sorted(scan.winners):
        w = scan.winners[set_id]
        by_outer.setdefault(w[1], []).append(_entry(scan, raw_dir, set_id, w))

    def _on_error(entry: dict, exc: BaseException) -> None:
        if failed is None:
            raise exc
        failed.append({"file": entry["key"], "error": str(exc)})

    for oi in sorted(by_outer):
        for entry, xml in _iter_outer(scan.outers[oi], by_outer[oi], _on_error):
            yield entry["key"], xml


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------
def row_from_xml(xml: bytes) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """One label's XML -> ``(finalized row, sidecar rows, parsed)``.

    ``body_text`` is the sections joined with a blank line; the offsets in the sidecar
    are into that text. ``finalize_row`` does not change ``body_text``; this is checked,
    because the offsets would no longer point into the stored text if it did."""
    parsed = spl.parse_spl(xml)
    set_id, version = parsed["set_id"], parsed["version"]
    license_code = LICENSE_PUBLIC_DOMAIN
    row = {
        "id": f"{SOURCE}:{set_id}",
        "source": SOURCE,
        "source_file": None,  # the part name, set by process_bucket
        "source_record_id": f"{set_id}.{version}",
        "title": parsed["title"],
        "body_text": parsed["body_text"] or None,
        "year": parsed["effective_year"],
        "license": license_code,
        "license_raw": LICENSE_RAW,
        "subset": subset_from_license(license_code),
        "is_retracted": False,
        # Metadata of the label: the LOINC code of the document (human Rx / OTC label).
        "book_meta": json.dumps({"doc_type": parsed["doc_type"], "version": version}),
    }
    out = finalize_row(row)
    if out["body_text"] != row["body_text"]:
        raise DailymedBucketError(f"{set_id}: finalize_row changed body_text; offsets would drift")
    return out, labels.sidecar_rows(out["id"], parsed), parsed


def dailymed_row_bytes(row: dict[str, Any]) -> int:
    """Estimated bytes of one row (as ``pmc_row_bytes``)."""
    total = PART_ROW_OVERHEAD_BYTES
    for value in row.values():
        if isinstance(value, str):
            total += len(value.encode("utf-8"))
    return total


# ---------------------------------------------------------------------------
# Parts, markers, audit
# ---------------------------------------------------------------------------
def _part_caps() -> tuple[int, int]:
    """``(part_rows, part_bytes)`` from the runtime profile (a test seam)."""
    profile = get_profile()
    return profile.part_rows, profile.part_bytes


def _staging_dir(processed_dir: Path) -> Path:
    return Path(processed_dir) / "staging" / SOURCE


def _best_effort_audit(name: str, n_rows: int) -> None:
    """One chained ``extract_commit`` audit row, degrading to the file-only mirror when
    no DB is reachable. Never raises. Called BEFORE a part's ``mark_success``."""
    with _AUDIT_LOCK:
        try:
            from episteme.data.db.connection import connection as _pg_connection

            with _pg_connection() as _conn:
                _audit(
                    "extract_commit",
                    conn=_conn,
                    object=f"{SOURCE} {name}",
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
                    object=f"{SOURCE} {name}",
                    rows_affected=n_rows,
                    note="db_unavailable_or_failed",
                    run_id=get_settings().run_id,
                )
            except Exception:  # noqa: BLE001
                _LOG.warning("audit mirror_only fallback also failed for %s", name, exc_info=True)


def _delete_all_parts(processed_dir: Path, key: str) -> None:
    """Delete every part of bucket ``key``: success, failed and load-success markers
    first, then the shards and sidecars. A part with a load-success marker was loaded:
    its name goes to the retire pending file of the bucket BEFORE any delete."""
    pattern = re.compile(re.escape(key) + r"__part(\d{5,})\.")
    prefix = glob.escape(f"{key}__part") + "*"
    load_dir = load_success_marker_path(processed_dir, SOURCE, "x").parent
    marker_dirs = [
        success_marker_path(processed_dir, SOURCE, "x").parent,
        failed_marker_path(processed_dir, SOURCE, "x").parent,
        load_dir,
    ]
    loaded: list[str] = []
    if load_dir.is_dir():
        for p in load_dir.glob(prefix):
            m = pattern.match(p.name)
            if m:
                loaded.append(f"{key}__part{m.group(1)}")
    if loaded:
        parts_retire.add_retire_pending(processed_dir, SOURCE, loaded, unit=key)
    side_dir = labels.sidecar_path(processed_dir, "x").parent
    for d in [*marker_dirs, _staging_dir(processed_dir), side_dir]:
        if not d.is_dir():
            continue
        for p in d.glob(prefix):
            if pattern.match(p.name):
                p.unlink(missing_ok=True)


def _read_bucket_marker(processed_dir: Path, key: str) -> dict[str, Any] | None:
    """The stats of the bucket success marker when it has the parts format, else ``None``."""
    p = success_marker_path(processed_dir, SOURCE, key)
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


def process_bucket(
    manifest_path: Path,
    *,
    raw_dir: Path,
    processed_dir: Path,
    force: bool,
) -> dict[str, Any]:
    """Extract one bucket (its manifest is ``manifest_path``) as bounded parts.

    Skipped when its marker has the parts format and its ``inputs`` equal the manifest
    fingerprint (unless ``force``). Else every part is deleted and the bucket is written
    again from part 1. A label that fails permanently is left out and recorded."""
    manifest_path = Path(manifest_path)
    key = manifest_path.stem
    t0 = time.time()
    n_rows_new = 0
    current_part: int | None = None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        key = str(manifest["key"])
        prefix = str(manifest["bucket"])
        fingerprint = manifest["fingerprint"]
        if not force:
            done = _read_bucket_marker(processed_dir, key)
            if done is not None and done["inputs"] == fingerprint:
                return {
                    "source_file": key,
                    "skipped": True,
                    "reason": "success_marker",
                    "failed_labels_count": int(done.get("failed_labels_count") or 0),
                }
            if is_success(processed_dir, SOURCE, key):
                _LOG.warning("%s: an input changed since the bucket completed; written again", key)
        success_marker_path(processed_dir, SOURCE, key).unlink(missing_ok=True)
        _delete_all_parts(processed_dir, key)

        cap_rows, cap_bytes = _part_caps()
        raw_dir = Path(raw_dir)
        entries: list[dict] = manifest["labels"]
        by_outer: dict[str, list[dict]] = {}
        for e in entries:
            by_outer.setdefault(e["outer"], []).append(e)
        failed: list[dict[str, str]] = []
        n_failed = 0
        seen_ids: dict[str, str] = {}
        status_counts: Counter[str] = Counter()

        def _label_failed(entry: dict, exc: BaseException) -> None:
            nonlocal n_failed
            n_failed += 1
            if len(failed) < _FAILED_LABELS_LISTED:
                failed.append({"file": entry["key"], "error": str(exc)})
            mark_failed(
                processed_dir,
                SOURCE,
                entry["key"],
                error_class="parse_error",
                message=str(exc),
                exc=exc,
            )

        def _bucket_rows() -> Iterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
            for outer_rel in sorted(by_outer):
                for entry, xml in _iter_outer(
                    raw_dir / outer_rel, by_outer[outer_rel], _label_failed
                ):
                    try:
                        row, side, parsed = row_from_xml(xml)
                        if (parsed["set_id"], parsed["version"]) != (
                            entry["set_id"],
                            entry["version"],
                        ):
                            raise DailymedBucketError(
                                "the XML identity differs from the scan "
                                f"({parsed['set_id']} v{parsed['version']})"
                            )
                    except Exception as e:  # noqa: BLE001
                        if is_transient(e):
                            raise
                        _label_failed(entry, e)
                        continue
                    other = seen_ids.get(row["id"])
                    if other is not None:
                        raise DailymedBucketError(
                            f"bucket {key}: {other} and {entry['key']} give one row id"
                        )
                    seen_ids[row["id"]] = entry["key"]
                    # A failed marker from an earlier run of this label is stale.
                    failed_marker_path(processed_dir, SOURCE, entry["key"]).unlink(missing_ok=True)
                    yield row, side

        rows_src = _bucket_rows()
        n = 0
        with contextlib.closing(rows_src):
            pending = next(rows_src, None)
            while pending is not None:
                n += 1
                current_part = n
                psf = part_name(prefix, n)
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
                        row_est = dailymed_row_bytes(row)
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
                        source=SOURCE,
                        source_file=psf,
                        prefer_parquet=True,
                        batch_size=_WRITE_BATCH_ROWS,
                    )
                finally:
                    gen.close()
                # The sidecar follows the shard it belongs to; the load refuses a shard
                # without one, and the bucket marker (after the last part) is what says
                # that the extract is whole.
                labels.write_sidecar(processed_dir, psf, part["side"])
                part_stats = {
                    "n_rows": part["rows"],
                    "first_id": part["first"],
                    "last_id": part["last"],
                    "extract_status_counts": dict(part["counts"]),
                    "label_rows": len(part["side"]),
                    "part_rows": cap_rows,
                    "part_bytes": cap_bytes,
                    "est_bytes": part["est_bytes"],
                    "write": write_info,
                }
                part["side"] = []
                _best_effort_audit(psf, part["rows"])
                mark_success(processed_dir, SOURCE, psf, stats=part_stats)
                n_rows_new += part["rows"]
                status_counts.update(part["counts"])
                current_part = n + 1
                if pending is None:
                    pending = next(rows_src, None)

        stats: dict[str, Any] = {
            "n_rows": n_rows_new,
            "parts": n,
            "parts_list": [part_name(prefix, i) for i in range(1, n + 1)],
            "inputs": fingerprint,
            "labels": len(entries),
            "superseded_versions": int(manifest.get("superseded_versions") or 0),
            "duplicate_files": int(manifest.get("duplicate_files") or 0),
            "failed_labels": failed,
            "failed_labels_count": n_failed,
            "part_rows": cap_rows,
            "part_bytes": cap_bytes,
            "extract_status_counts": dict(status_counts),
            "elapsed_sec": round(time.time() - t0, 4),
        }
        mark_success(processed_dir, SOURCE, key, stats=stats)
        result = {k: v for k, v in stats.items() if k != "inputs"}
        return {"source_file": key, "skipped": False, "ok": True, **result}
    except Exception as e:  # noqa: BLE001
        if is_transient(e):
            raise TransientUnitError(key, repr(e)) from None
        mark_failed(
            processed_dir,
            SOURCE,
            key,
            error_class=type(e).__name__,
            message=str(e),
            stats={"n_rows_partial": n_rows_new, "failed_part": current_part},
            exc=e,
        )
        return {"source_file": key, "skipped": False, "ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def _unit_name(result: dict[str, Any]) -> None:
    """A worker crash names the unit by the manifest basename (``dailymed_bXX.json``)."""
    sf = str(result.get("source_file") or "")
    if sf.startswith("dailymed_b") and sf.endswith(".json"):
        result["source_file"] = sf[: -len(".json")]


def _print_verbose(result: dict[str, Any]) -> None:
    key = result.get("source_file")
    if result.get("skipped"):
        print(f"skip {key}", file=sys.stderr)
    elif result.get("ok"):
        print(
            f"ok {key} parts={result.get('parts')} rows={result.get('n_rows')} "
            f"failed_labels={result.get('failed_labels_count')}",
            file=sys.stderr,
        )
    else:
        print(f"FAIL {key}: {result.get('error')}", file=sys.stderr)


def _select_buckets(
    keys: list[str], skip_units: list[str] | None, skip_reason: str | None
) -> list[str]:
    if not skip_units:
        return keys
    from episteme.ops import skip

    keep = set(
        skip.apply_skips(
            SOURCE, "extract", keys, skip_units, skip_reason, run_id=get_settings().run_id
        )
    )
    for k in keys:
        if k not in keep:
            print(f"skip-unit {k}", file=sys.stderr)
    return [k for k in keys if k in keep]


def extract_dailymed(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
    skip_units: list[str] | None = None,
    skip_reason: str | None = None,
    release_kinds: tuple[str, ...] | None = None,
) -> dict:
    """Importable core: scan the DailyMed zips under ``raw_dir`` and extract them by
    bucket into staging parts + ops markers under ``processed_dir``.

    ``max_files`` limits the number of buckets of the run; ``skip_units`` take bucket
    keys. Returns ``{"inputs", "ok", "failed", "rows", "buckets", "labels",
    "superseded_versions", "duplicate_files", "failed_labels_count", "withdrawn_labels",
    "retired_buckets"}``. Raises ``DailymedScanError`` when an outer zip is unreadable."""
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)
    check_bucket_scheme(processed_dir)
    kinds = tuple(release_kinds) if release_kinds is not None else get_settings().dailymed_kinds

    def _scan_failed(key: str, exc: BaseException) -> None:
        mark_failed(processed_dir, SOURCE, key, error_class="scan_error", message=str(exc), exc=exc)

    t_phase = time.monotonic()
    scan = scan_zips(raw_dir, kinds, on_failed=_scan_failed)
    _LOG.info(
        "scan: %d outer zips, %d labels, %d failed in %.1f s",
        len(scan.outers),
        len(scan.winners),
        scan.n_failed,
        time.monotonic() - t_phase,
    )
    if scan.unreadable_outers:
        raise DailymedScanError(
            "outer zip(s) cannot be opened, nothing was written or retired: "
            + "; ".join(scan.unreadable_outers)
        )
    n_labels = len(scan.winners)
    superseded_total = sum(scan.superseded.values())
    duplicates_total = sum(scan.duplicates.values())
    n_scan_failed = scan.n_failed
    retired_buckets = withdrawn = 0
    if scan.outers:
        # With no zip at all nothing is judged: an empty raw dir retires nothing.
        paths, counts, withdrawn = _write_manifests(processed_dir, raw_dir, scan)
        retired_buckets, gone = _retire_vanished_buckets(processed_dir, set(paths))
        withdrawn += gone
    else:
        paths = {}
    del scan  # the dict of the scan is not needed by the buckets (or the workers)

    keys = _select_buckets([bucket_key(p) for p in sorted(paths)], skip_units, skip_reason)
    if max_files and max_files > 0:
        keys = keys[:max_files]
    by_key = {bucket_key(p): paths[p] for p in paths}
    manifests = [by_key[k] for k in keys]

    workers = max(1, int(workers))
    t0 = time.time()

    def _on_result(result: dict[str, Any]) -> None:
        _unit_name(result)
        if verbose:
            _print_verbose(result)

    results = run_per_file(
        process_bucket,
        manifests,
        workers=workers,
        on_result=_on_result,
        raw_dir=raw_dir,
        processed_dir=processed_dir,
        force=force,
    )
    record_first_failure(SOURCE, "extract", results)

    summary = {
        "inputs": len(keys),
        "ok": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok") and not r.get("skipped")),
        "rows": sum(int(r.get("n_rows") or 0) for r in results if r.get("ok")),
        "buckets": len(keys),
        "labels": n_labels,
        "superseded_versions": superseded_total,
        "duplicate_files": duplicates_total,
        "failed_labels_count": n_scan_failed
        + sum(int(r.get("failed_labels_count") or 0) for r in results),
        "withdrawn_labels": withdrawn,
        "retired_buckets": retired_buckets,
    }

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
                "kinds": list(kinds),
            },
            totals={
                **summary,
                "skipped": sum(1 for r in results if r.get("skipped")),
                "elapsed_sec": round(time.time() - t0, 3),
            },
        )
    except Exception:  # noqa: BLE001 - the manifest is best-effort
        pass
    return summary


def _write_failed_inputs(processed_dir: Path, res: dict[str, Any]) -> None:
    """Tell the sample gate how many inputs failed: failed buckets plus failed labels.
    ``gate run`` reads the file when its ``run_id`` is the run's own."""
    run_id = get_settings().run_id or utc_now_iso().replace(":", "").replace("-", "")
    failed = int(res.get("failed") or 0) + int(res.get("failed_labels_count") or 0)
    write_marker(
        ops_root(processed_dir, SOURCE) / FAILED_INPUTS_FILE,
        {"run_id": run_id, "failed_inputs": failed},
    )


def _main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    p = argparse.ArgumentParser(
        description="DailyMed SPL labels to episteme.articles staging shards"
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "dailymed")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all buckets; N = the first N buckets (env: EPISTEME_SAMPLE_LIMIT)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="worker processes (default: the runtime profile; EPISTEME_EXTRACT_WORKERS overrides)",
    )
    p.add_argument("--force", action="store_true")
    p.add_argument("--verbose", action="store_true", help="print ok/skip/FAIL per bucket to stderr")
    add_skip_args(p)
    args = p.parse_args(argv)
    check_skip_args(p, args)
    workers = default_workers() if args.workers is None else max(1, args.workers)

    raw_dir: Path = args.raw_dir
    processed_dir: Path = args.processed_dir
    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    print(f"schema={SCHEMA_VERSION} source={SOURCE}")
    print(
        f"raw_dir={raw_dir.resolve()} workers={workers} kinds={','.join(settings.dailymed_kinds)}"
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    old_level = _LOG.level
    _LOG.addHandler(handler)
    _LOG.setLevel(logging.INFO)
    try:
        res = extract_dailymed(
            raw_dir,
            processed_dir,
            max_files=args.max_files,
            force=args.force,
            workers=workers,
            verbose=args.verbose,
            skip_units=args.skip_unit,
            skip_reason=args.reason,
        )
    except DailymedScanError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        _LOG.removeHandler(handler)
        _LOG.setLevel(old_level)

    if res["inputs"] == 0 and not args.skip_unit:
        print(f"ERROR: no DailyMed labels under {raw_dir}", file=sys.stderr)
        return 1

    print(f"done inputs={res['inputs']} ok={res['ok']} failed={res['failed']} rows={res['rows']}")
    _write_failed_inputs(processed_dir, res)
    return 1 if res["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    with stage_guard(lambda: None, lock_stage="extract", argv=argv, source=SOURCE) as g:
        rc = _main(argv)
        g.exit_code(rc)
        return rc


if __name__ == "__main__":
    raise SystemExit(main())
