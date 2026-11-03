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

Old update zips that stay on disk keep a label that a new full release dropped: delete the
old updates when a full release is replaced.

**Guards** (refuse before anything is written; ``--force --reason`` overrides them, with a
``force_override`` audit row; a run under a ``_sample`` processed root skips them):
the release part numbers of each kind must run from 1 without a gap, and the share of the
previously known labels that this scan no longer holds must not exceed
``Settings.dailymed_max_withdrawn_share`` (``EPISTEME_DAILYMED_MAX_WITHDRAWN_SHARE``,
default 0.1; with no earlier state there is no limit).

A known label whose inner zip cannot be read in the scan, with no other readable copy
(or only an older version), is not withdrawn and its row is not dropped. The XML is
needed to write the row again, so its whole bucket is held: not rewritten, listed as
failed (``held_buckets``), and its earlier parts and manifest stay as they are.

Importable core: ``extract_dailymed(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False, skip_units=None, skip_reason=None,
release_kinds=None) -> dict``. ``main()`` is the thin CLI wrapper.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import re
import sys
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

from episteme.config import get_settings  # noqa: E402
from episteme.data import bucket_parts  # noqa: E402
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
from episteme.data.staging_writer import WRITE_BATCH_ROWS  # noqa: E402
from episteme.ops.profile import get_profile  # noqa: E402
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

_RELEASE_RE = re.compile(r"^dm_spl_release_human_(rx|otc)_part(\d+)\.zip$")
_UPDATE_RE = re.compile(r"^dm_spl_(monthly|weekly|daily)_update_.+\.zip$")
_RELEASE_KIND = {"rx": "prescription", "otc": "otc"}

_WRITE_BATCH_ROWS = WRITE_BATCH_ROWS
_FAILED_LABELS_LISTED = 100


class DailymedScanError(RuntimeError):
    """The scan cannot be trusted (an outer zip is unreadable); nothing was written."""


class DailymedGuardError(DailymedScanError):
    """A guard refused the run (a release part is missing, or too many labels would be
    withdrawn). ``--force --reason`` overrides it."""


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
    stored = bucket_parts.check_bucket_scheme(
        processed_dir, SOURCE, "prefix_chars", BUCKET_PREFIX_CHARS
    )
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


def _identity(inner: zipfile.ZipFile) -> tuple[str, int]:
    with inner.open(_xml_name(inner)) as f:
        return spl.read_identity(f)


@contextlib.contextmanager
def _inner_zip(outer: zipfile.ZipFile, info: zipfile.ZipInfo) -> Iterator[zipfile.ZipFile]:
    """The inner zip ``info`` of an outer zip.

    A STORED outer member is opened as a seekable stream: the inner central directory
    (at the end) and the XML are read, and the images are skipped by a seek, not read. A
    compressed outer member cannot be sought without decompressing it up to the target (and
    again after each rewind), so it is read whole once, as before."""
    if info.compress_type == zipfile.ZIP_STORED:
        with outer.open(info) as f, zipfile.ZipFile(f) as inner:
            yield inner
    else:
        with zipfile.ZipFile(io.BytesIO(outer.read(info))) as inner:
            yield inner


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
    failed_keys: set[str] = field(default_factory=set)  # every failed label key
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
                    with _inner_zip(zf, info) as inner:
                        set_id, version = _identity(inner)
                except Exception as e:  # noqa: BLE001 - one label; transient ones stop the run
                    if is_transient(e):
                        raise
                    key = label_key(path, raw_dir, info.filename)
                    scan.n_failed += 1
                    scan.failed_keys.add(key)
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


def release_part_gaps(outers: list[Path]) -> list[str]:
    """One message per release kind whose part numbers do not run from 1 without a gap
    (``dm_spl_release_human_rx_part1..N``). A kind with no release zip is not a gap
    (an update-only directory); a missing LAST part cannot be seen here, the withdrawn
    share guard is what catches that."""
    nums: dict[str, set[int]] = {}
    for p in outers:
        m = _RELEASE_RE.match(p.name)
        if m:
            nums.setdefault(_RELEASE_KIND[m.group(1)], set()).add(int(m.group(2)))
    gaps = []
    for kind in sorted(nums):
        missing = [n for n in range(1, max(nums[kind]) + 1) if n not in nums[kind]]
        if missing:
            gaps.append(f"{kind}: release part(s) {missing} missing")
    return gaps


def _is_sample(processed_dir: Path) -> bool:
    return bucket_parts.is_sample(processed_dir)


def _plan_against_earlier_state(processed_dir: Path, scan: Scan) -> tuple[dict[str, str], int, int]:
    """Compare the scan with the manifests of the earlier run, one manifest at a time.

    Returns ``(held, withdrawn, known)``. ``held`` maps a bucket prefix to the reason it
    must not be rewritten: one of its known labels failed in this scan and has no readable
    copy of the same or a newer version. The rest are counted: ``known`` labels in the
    other buckets, ``withdrawn`` of them that the scan no longer holds. Nothing is written."""
    held: dict[str, str] = {}
    withdrawn = known = 0
    d = _manifest_dir(processed_dir)
    if not d.is_dir():
        return held, withdrawn, known
    for p in sorted(d.glob(f"{bucket_key('')}*.json")):
        prefix = p.stem[len(bucket_key("")) :]
        try:
            entries = json.loads(p.read_text(encoding="utf-8"))["labels"]
        except (OSError, ValueError, KeyError):
            continue
        unreadable = [
            e["key"]
            for e in entries
            if e["key"] in scan.failed_keys
            and (scan.winners.get(e["set_id"]) or (-1,))[0] < e["version"]
        ]
        if unreadable:
            held[prefix] = f"{unreadable[0]} cannot be read and no copy of its version is readable"
            continue
        known += len(entries)
        withdrawn += sum(1 for e in entries if e["set_id"] not in scan.winners)
    return held, withdrawn, known


def _write_manifests(
    processed_dir: Path, raw_dir: Path, scan: Scan, held: dict[str, str]
) -> dict[str, Path]:
    """One manifest per bucket that is not held. Returns ``prefix -> manifest path``."""
    by_prefix: dict[str, list[dict]] = {}
    for set_id in sorted(scan.winners):
        prefix = bucket_prefix(set_id)
        if prefix not in held:
            by_prefix.setdefault(prefix, []).append(
                _entry(scan, raw_dir, set_id, scan.winners[set_id])
            )
    paths: dict[str, Path] = {}
    for prefix in sorted(by_prefix):
        entries = by_prefix[prefix]
        path = _manifest_path(processed_dir, prefix)
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
    return paths


def _retire_vanished_buckets(processed_dir: Path, live: set[str]) -> int:
    """Remove the buckets that have a manifest but no label in this scan (and are not
    held): delete their parts (the loaded ones go to the retire pending file), markers
    and manifest. Returns the number of buckets removed."""
    d = _manifest_dir(processed_dir)
    if not d.is_dir():
        return 0
    removed = 0
    for p in sorted(d.glob(f"{bucket_key('')}*.json")):
        prefix = p.stem[len(bucket_key("")) :]
        if prefix in live:
            continue
        success_marker_path(processed_dir, SOURCE, bucket_key(prefix)).unlink(missing_ok=True)
        _delete_all_parts(processed_dir, bucket_key(prefix))
        p.unlink(missing_ok=True)
        removed += 1
    return removed


def _audit_override(reason: str, what: str) -> None:
    bucket_parts.audit_override(SOURCE, reason, what)


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
                with _inner_zip(zf, info) as inner:
                    xml = inner.read(_xml_name(inner))
            except Exception as e:  # noqa: BLE001
                if is_transient(e):
                    raise
                on_error(entry, e)
                continue
            yield entry, xml


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
        # book_meta is the only metadata column that fits; it makes decide_extract_status
        # treat the row as a book row (never empty), so the status is set here.
        "book_meta": json.dumps({"doc_type": parsed["doc_type"], "version": version}),
        "extract_status": "ok" if parsed["body_text"].strip() else "empty",
    }
    out = finalize_row(row)
    if out["body_text"] != row["body_text"]:
        raise DailymedBucketError(f"{set_id}: finalize_row changed body_text; offsets would drift")
    return out, labels.sidecar_rows(out["id"], parsed), parsed


def dailymed_row_bytes(row: dict[str, Any]) -> int:
    """Estimated bytes of one row (as ``pmc_row_bytes``)."""
    return bucket_parts.row_bytes(row)


# ---------------------------------------------------------------------------
# Parts, markers, audit
# ---------------------------------------------------------------------------
def _part_caps() -> tuple[int, int]:
    """``(part_rows, part_bytes)`` from the runtime profile (a test seam)."""
    profile = get_profile()
    return profile.part_rows, profile.part_bytes


def _delete_all_parts(processed_dir: Path, key: str) -> None:
    """Delete every part of bucket ``key``, the sidecars too (see ``bucket_parts``)."""
    side_dir = labels.sidecar_path(processed_dir, "x").parent
    bucket_parts.delete_all_parts(processed_dir, SOURCE, key, (side_dir,))


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
            done = bucket_parts.read_bucket_marker(processed_dir, SOURCE, key)
            if done is not None and done["inputs"] == fingerprint:
                failed_marker_path(processed_dir, SOURCE, key).unlink(missing_ok=True)
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

        def _sidecar(psf: str, side: list[dict[str, Any]]) -> dict[str, Any]:
            # The sidecar follows the shard it belongs to; the load refuses a shard
            # without one, and the bucket marker (after the last part) is what says
            # that the extract is whole.
            labels.write_sidecar(processed_dir, psf, side)
            return {"label_rows": len(side)}

        progress: dict[str, Any] = {"part": None, "rows": 0}
        try:
            n, n_rows_new, status_counts = bucket_parts.write_parts(
                _bucket_rows(),
                processed_dir=processed_dir,
                source=SOURCE,
                part_name=lambda i: part_name(prefix, i),
                cap_rows=cap_rows,
                cap_bytes=cap_bytes,
                batch_rows=_WRITE_BATCH_ROWS,
                after_shard=_sidecar,
                progress=progress,
            )
        finally:
            current_part = progress["part"]
            n_rows_new = progress["rows"]

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
        failed_marker_path(processed_dir, SOURCE, key).unlink(missing_ok=True)  # a held mark
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
    return bucket_parts.select_buckets(SOURCE, keys, skip_units, skip_reason)


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
    force_reason: str | None = None,
) -> dict:
    """Importable core: scan the DailyMed zips under ``raw_dir`` and extract them by
    bucket into staging parts + ops markers under ``processed_dir``.

    ``max_files`` limits the number of buckets of the run; ``skip_units`` take bucket
    keys. Returns ``{"inputs", "ok", "failed", "rows", "buckets", "labels",
    "superseded_versions", "duplicate_files", "failed_labels_count", "withdrawn_labels",
    "retired_buckets", "held_buckets"}``. ``force_reason`` with ``force`` overrides the
    guards (see the module docstring). Raises ``DailymedScanError`` when an outer zip is
    unreadable, ``DailymedGuardError`` when a guard refuses."""
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    kinds = tuple(release_kinds) if release_kinds is not None else settings.dailymed_kinds
    override = bool(force and force_reason)
    sample = _is_sample(processed_dir)
    overridden: list[str] = []

    def _guard(message: str) -> None:
        """Refuse with ``message``, unless a sample run or ``--force --reason``."""
        if sample:
            return
        if not override:
            raise DailymedGuardError(
                f"{message}; nothing was written. Fix the input, or run again with "
                "--force --reason to override (audited)"
            )
        overridden.append(message)

    gaps = release_part_gaps(discover_outer_zips(raw_dir, kinds))
    if gaps:
        _guard("; ".join(gaps))
    check_bucket_scheme(processed_dir)

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
    held: dict[str, str] = {}
    if scan.outers:
        # With no zip at all nothing is judged: an empty raw dir retires nothing.
        held, withdrawn, known = _plan_against_earlier_state(processed_dir, scan)
        limit = settings.dailymed_max_withdrawn_share
        if known and withdrawn / known > limit:
            _guard(
                f"{withdrawn} of {known} known labels ({withdrawn / known:.1%}) are not in "
                f"the zips; the limit is {limit:.1%} (EPISTEME_DAILYMED_MAX_WITHDRAWN_SHARE)"
            )
        if overridden:
            _audit_override(str(force_reason), "; ".join(overridden))
        paths = _write_manifests(processed_dir, raw_dir, scan, held)
        retired_buckets = _retire_vanished_buckets(processed_dir, set(paths) | set(held))
        for prefix, why in held.items():
            mark_failed(
                processed_dir,
                SOURCE,
                bucket_key(prefix),
                error_class="held_bucket",
                message=f"bucket not rewritten: {why}",
            )
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
        "failed": sum(1 for r in results if not r.get("ok") and not r.get("skipped")) + len(held),
        "held_buckets": [bucket_key(p) for p in sorted(held)],
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
    """Tell the sample gate how many inputs failed: failed buckets plus failed labels."""
    failed = int(res.get("failed") or 0) + int(res.get("failed_labels_count") or 0)
    bucket_parts.write_failed_inputs(processed_dir, SOURCE, failed)


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
            force_reason=args.reason,
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
