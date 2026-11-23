"""Generated EU medicine records: one ``ema_epar`` article per EU medicine.

    python -m episteme.data.ema.medicine_records [--processed-dir DIR]

The text of an EU medicine (its name, substances, indication, status and the list of its EPAR
documents) is written from two feeds that are already loaded, the ``ema_medicines`` records
and the ``ema_epar_documents`` records of ``reg_record`` (the latest release of each, found
with ``epar_links.releases``). No EMA request is made and no file is read.

**Rows.** The format is the one of ``extract_epar.row_from_document``: ``source``
``ema_epar``, licence ``extract_epar.LICENSE``. The article id is
``ema_medicine:<ema_product_number>``, so it never equals a PDF article (``ema_epar:<id>``).
The label link step (``epar_links``) reads only ``ema_epar:`` articles, so these rows get no
label links: they are not labels. Each part has an empty label sidecar, as the load needs.

**Text.** A fixed template (``medicine_text``). Fields, in this order: name, active
substance, INN, ATC code, therapeutic area, category, status, dates (marketing
authorisation, withdrawal or lapse, last update), therapeutic indication, then the EPAR
documents (title, type, first published, last updated), sorted by type, then
``first_published_date``, then id. An empty field is left out.

**Parts and checkpoint.** The records are one bucket, ``ema_epar_medicines``, written as
bounded parts ``ema_epar_medicines__partNNNNN`` with the same helpers as the EPAR buckets
(``bucket_parts``). The bucket marker's ``inputs`` are the two release ids and the template
version (``inputs_key``): a rerun with the same releases and template writes nothing (unless
``force``). Another pair of releases rewrites the bucket from part 1: the old parts are
deleted, and the loaded ones go to the retire pending file first. The marker's ``lineage``
names both feed releases. A run record is written to
``_ops/ema_epar/runs/medicine_records_<run_id>.json``.

The parts load through the existing path (``load_ema_epar.sh``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import psycopg

from episteme.config import get_settings
from episteme.data import bucket_parts
from episteme.data.article_schema import finalize_row, subset_from_license, utc_now_iso
from episteme.data.checkpoint_markers import (
    mark_success,
    ops_root,
    success_marker_path,
    write_marker,
)
from episteme.data.dailymed import labels
from episteme.data.ema import epar_links
from episteme.data.ema.extract_epar import LICENSE, LICENSE_RAW, SOURCE
from episteme.data.staging_writer import WRITE_BATCH_ROWS
from episteme.ops.profile import get_profile

ARTICLE_PREFIX = "ema_medicine:"
BUCKET_KEY = "ema_epar_medicines"
# Part of the checkpoint key: a new template writes the records again.
TEMPLATE_VERSION = "1"
MED_DATASET = epar_links.MED_DATASET
DOC_DATASET = epar_links.DOC_DATASET

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ]|$)")

# (payload field, label) in template order, before the dates and the indication.
_HEAD_FIELDS = (
    ("active_substance", "Active substance"),
    ("international_non_proprietary_name_common_name", "INN"),
    ("atc_code_human", "ATC code"),
    ("therapeutic_area_mesh", "Therapeutic area"),
    ("category", "Category"),
    ("medicine_status", "Status"),
)
_DATE_FIELDS = (
    ("marketing_authorisation_date", "Marketing authorisation date"),
    (
        "withdrawal_expiry_revocation_lapse_of_marketing_authorisation_date",
        "Withdrawal, expiry, revocation or lapse date",
    ),
    ("last_updated_date", "Last updated"),
)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
def _clean(value: Any) -> str:
    """A payload value as one trimmed string ('' for none); control characters dropped."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(v).strip() for v in value if str(v).strip())
    s = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_RE.sub("", s).strip()


def _date(value: Any) -> str:
    s = _clean(value)
    m = _DATE_RE.match(s)
    return m.group(1) if m else s


def document_sort_key(d: dict[str, Any]) -> tuple[str, str, str]:
    """Documents sort by type, then ``first_published_date``, then id."""
    return (_clean(d.get("type")), _clean(d.get("first_published_date")), _clean(d.get("id")))


def _document_line(d: dict[str, Any]) -> str:
    parts = [
        _clean(d.get("name")),
        f"type: {t}" if (t := _clean(d.get("type"))) else "",
        f"first published: {p}" if (p := _date(d.get("first_published_date"))) else "",
        f"last updated: {u}" if (u := _date(d.get("last_updated_date"))) else "",
    ]
    return "- " + "; ".join(p for p in parts if p)


def medicine_text(med: dict[str, Any], docs: list[dict[str, Any]]) -> str:
    """The text of one medicine: the fixed template, empty fields left out."""
    lines: list[str] = []
    if name := _clean(med.get("name_of_medicine")):
        lines.append(f"Medicine: {name}")
    for field, label in _HEAD_FIELDS:
        if v := _clean(med.get(field)):
            lines.append(f"{label}: {v}")
    for field, label in _DATE_FIELDS:
        if v := _date(med.get(field)):
            lines.append(f"{label}: {v}")
    if ind := _clean(med.get("therapeutic_indication")):
        lines.append(f"Therapeutic indication: {ind}")
    if docs:
        lines.append("EPAR documents:")
        lines.extend(_document_line(d) for d in sorted(docs, key=document_sort_key))
    return "\n".join(lines)


def article_id(product_number: str) -> str:
    return f"{ARTICLE_PREFIX}{product_number}"


def _year(value: Any) -> int | None:
    m = re.match(r"^(\d{4})", _clean(value))
    return int(m.group(1)) if m else None


def row_from_medicine(
    product_number: str, med: dict[str, Any], docs: list[dict[str, Any]]
) -> dict[str, Any]:
    """One medicine -> a finalized articles row (the ``extract_epar`` row format)."""
    body = medicine_text(med, docs)
    name = _clean(med.get("name_of_medicine")) or f"EU medicine {product_number}"
    row = {
        "id": article_id(product_number),
        "source": SOURCE,
        "source_file": None,  # the part name, set by write_parts
        "source_record_id": product_number,
        "title": name,
        "body_text": body or None,
        "year": _year(med.get("marketing_authorisation_date")),
        "language": "en",
        "license": LICENSE,
        "license_raw": LICENSE_RAW,
        "subset": subset_from_license(LICENSE),
        "is_retracted": False,
        "book_meta": json.dumps(
            {
                "doc_type": "ema_medicine",
                "ema_product_number": product_number,
                "category": _clean(med.get("category")) or None,
                "documents": len(docs),
            }
        ),
        "extract_status": "ok" if body else "empty",
    }
    out = finalize_row(row)
    if out["body_text"] != row["body_text"]:
        raise ValueError(f"{product_number}: finalize_row changed body_text")
    return out


# ---------------------------------------------------------------------------
# Checkpoint key
# ---------------------------------------------------------------------------
def inputs_key(rel: dict[str, str]) -> list[str]:
    """The input identity of the bucket: both release ids (each named by its feed) and the
    template version. Two different pairs never give the same list."""
    return [
        f"{MED_DATASET}={rel[MED_DATASET]}",
        f"{DOC_DATASET}={rel[DOC_DATASET]}",
        f"template={TEMPLATE_VERSION}",
    ]


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def read_feeds(
    conn: psycopg.Connection, release_id: str | None = None
) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """``(releases, medicines, documents)``: the releases (``epar_links.releases``), the
    medicine payloads by ``ema_product_number``, and the document payloads (with their
    ``id``) grouped by ``ema_product_number``. Reads only."""
    rel = epar_links.releases(conn, release_id)
    meds: dict[str, dict[str, Any]] = {}
    docs: dict[str, list[dict[str, Any]]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT record_key, payload FROM episteme.reg_record "
            "WHERE dataset = %s AND release_id = %s ORDER BY record_key",
            (MED_DATASET, rel[MED_DATASET]),
        )
        for key, payload in cur.fetchall():
            meds[str(key)] = dict(payload or {})
        cur.execute(
            "SELECT record_key, payload FROM episteme.reg_record "
            "WHERE dataset = %s AND release_id = %s ORDER BY record_key",
            (DOC_DATASET, rel[DOC_DATASET]),
        )
        for key, payload in cur.fetchall():
            p = dict(payload or {})
            p.setdefault("id", str(key))
            num = _clean(p.get("ema_product_number"))
            if num:
                docs.setdefault(num, []).append(p)
    return rel, meds, docs


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
def part_name(i: int) -> str:
    return f"{BUCKET_KEY}__part{i:05d}"


def _delete_all_parts(processed_dir: Path) -> None:
    side_dir = labels.sidecar_path(processed_dir, "x", SOURCE).parent
    bucket_parts.delete_all_parts(processed_dir, SOURCE, BUCKET_KEY, (side_dir,))


def _part_caps() -> tuple[int, int]:
    profile = get_profile()
    return profile.part_rows, profile.part_bytes


def write_records(
    processed_dir: Path,
    rel: dict[str, str],
    meds: dict[str, dict[str, Any]],
    docs: dict[str, list[dict[str, Any]]],
    *,
    force: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Write the records as parts, unless the bucket marker has the same ``inputs_key``
    (then nothing is written). Returns the counts, the releases and ``skipped``."""
    processed_dir = Path(processed_dir)
    inputs = inputs_key(rel)
    lineage = {"releases": dict(rel), "template_version": TEMPLATE_VERSION}
    done = bucket_parts.read_bucket_marker(processed_dir, SOURCE, BUCKET_KEY)
    if not force and done is not None and done["inputs"] == inputs:
        return {"skipped": True, "n_rows": int(done.get("n_rows") or 0), **lineage}

    success_marker_path(processed_dir, SOURCE, BUCKET_KEY).unlink(missing_ok=True)
    _delete_all_parts(processed_dir)

    def _rows():
        for num in sorted(meds):
            yield row_from_medicine(num, meds[num], docs.get(num, [])), []

    def _sidecar(psf: str, side: list[Any]) -> dict[str, Any]:
        labels.write_sidecar(processed_dir, psf, [], SOURCE)  # no labels, but the load needs it
        return {"label_rows": 0}

    cap_rows, cap_bytes = _part_caps()
    n, n_rows, status_counts = bucket_parts.write_parts(
        _rows(),
        processed_dir=processed_dir,
        source=SOURCE,
        part_name=part_name,
        cap_rows=cap_rows,
        cap_bytes=cap_bytes,
        batch_rows=WRITE_BATCH_ROWS,
        after_shard=_sidecar,
    )
    stats = {
        "n_rows": n_rows,
        "parts": n,
        "parts_list": [part_name(i) for i in range(1, n + 1)],
        "inputs": inputs,
        "lineage": lineage,
        "documents": sum(len(v) for v in docs.values()),
        "part_rows": cap_rows,
        "part_bytes": cap_bytes,
        "extract_status_counts": dict(status_counts),
    }
    mark_success(processed_dir, SOURCE, BUCKET_KEY, stats=stats)
    result = {"skipped": False, "n_rows": n_rows, "parts": n, **lineage}
    run_id = run_id or get_settings().run_id or utc_now_iso().replace(":", "").replace("-", "")
    path = ops_root(processed_dir, SOURCE) / "runs" / f"medicine_records_{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_marker(path, {"run_id": run_id, "source": SOURCE, **result})
    return result


def build_records(
    conn: psycopg.Connection,
    processed_dir: Path,
    *,
    release_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Read the two feeds' releases from ``reg_record`` and write the medicine records as
    ``ema_epar`` staging parts under ``processed_dir``. Returns ``write_records``' dict."""
    rel, meds, docs = read_feeds(conn, release_id)
    return write_records(processed_dir, rel, meds, docs, force=force)


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    p = argparse.ArgumentParser(
        prog="python -m episteme.data.ema.medicine_records",
        description="Write one ema_epar article per EU medicine from the loaded EMA feeds.",
    )
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument("--release-id", default=None, help="a release of both feeds (default: latest)")
    p.add_argument("--force", action="store_true", help="write again although nothing changed")
    args = p.parse_args(argv)

    from episteme.data.db.connection import connection

    try:
        with connection() as conn:
            res = build_records(
                conn, args.processed_dir, release_id=args.release_id, force=args.force
            )
    except epar_links.EparLinkError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    state = "unchanged" if res["skipped"] else f"parts={res['parts']}"
    print(f"ok {SOURCE} medicine records rows={res['n_rows']} {state} releases={res['releases']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
