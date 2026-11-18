"""EU label substance links: ``label_substance`` rows for the loaded EPAR product information.

    python -m episteme.data.ema.epar_links [--processed-dir DIR] [--release-id ID]

``load_ema_epar.sh`` runs this step after every load and retire. A shard load replaces the
label rows of its articles (``labels.replace_labels``), the ``label_substance`` rows too, so
the links are written again after each load, also when every shard was skipped.

**Method ``ema_name_exact``** (spec section 6, owner decision of 2026-11-17). For each loaded
``ema_epar`` article (id ``ema_epar:<document id>``):

1. The document is looked up in the ``ema_epar_documents`` records (``reg_record``). Only a
   ``product-information`` document (an EU label, the SmPC) is linked; other document types
   are neither linked nor reported.
2. Its ``ema_product_number`` gives the ``ema_medicines`` record. Only a medicine with
   ``category = 'Human'`` is linked: a veterinary label is loaded as text but never linked,
   so ``current_label`` for a human substance never returns it.
3. The ``active_substance`` field is split on ``;`` (``split_active_substance``). Each part
   is normalised in SQL as the API does (``episteme_api_internal.norm_name``, inner white
   space collapsed, outer space removed; ``resolve_entity``) and matched against the
   ``substance_xref`` names with ``id_type`` in ``('ema_name', 'name')``.
4. A part is linked only when exactly one substance matches and that substance has a
   UNII. The row is ``(article_id, unii, ndc_product NULL)``. No fuzzy match.

``label_substance`` has no method column, and this step does not change the table: the
method ``ema_name_exact`` is recorded in the link report and in the audit row.

**Report.** Every label that is not linked, or not linked in full, is listed with a reason:
``no_document_record``, ``no_medicine_record``, ``veterinary`` (``not_human`` for another
category), ``no_active_substance``, ``no_match``, ``ambiguous``, ``no_unii``, ``mixed``
(no part linked, for more than one of those reasons) or ``partial`` (some parts linked).
``parts`` names each unlinked part with its reason. The report is written next to the run
markers: ``_ops/ema_epar/runs/label_links_<run_id>.json``, with both feed release ids
(lineage) and the counts.

**Releases.** ``release_id`` ``None`` (the default) reads the ``is_latest`` release of each
of the two feeds; a given id reads that release of both. A feed with no such release stops
the step before any row is deleted (``EparLinkError``).

**Writes.** One transaction: the rows are built first, then every ``label_substance`` row of
an ``ema_epar:`` article is deleted and the new rows are inserted, with one ``load_commit``
audit row. Running it again gives the same rows. No input file is read, so no checkpoint
marker or ``input_key`` is involved. No EMA request is made.

The pipeline role needs ``EXECUTE`` on ``episteme_api_internal.norm_name``
(``grants/0015_api_roles.sql``). Config comes from ``episteme.config``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from episteme import audit_trail
from episteme.config import get_settings
from episteme.data.checkpoint_markers import ops_root, write_marker

SOURCE = "ema_epar"
METHOD = "ema_name_exact"
ARTICLE_PREFIX = f"{SOURCE}:"
DOC_DATASET = "ema_epar_documents"
MED_DATASET = "ema_medicines"
# The document type that is an EU label (extract_epar.SECTION_DOC_TYPE).
LABEL_DOC_TYPE = "product-information"
HUMAN = "Human"
VETERINARY = "Veterinary"
_ID_TYPES = ("ema_name", "name")


class EparLinkError(RuntimeError):
    """The link step cannot run (a feed release is missing); nothing was changed."""


def split_active_substance(value: str | None) -> list[str]:
    """The parts of an EMA ``active_substance`` value: split on ``;`` only, stripped, empty
    parts dropped, each part once (first order kept)."""
    out: list[str] = []
    for part in str(value or "").split(";"):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


def decide(
    docs: list[dict[str, Any]], matches: dict[str, tuple[int, str | None]]
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """The link rows and the report, from the documents (``_documents``) and the matches of
    each part (``part -> (number of distinct substances, UNII when there is one)``).

    Returns sorted, distinct ``(article_id, unii)`` rows and one report entry per label
    that is not linked in full."""
    rows: set[tuple[str, str]] = set()
    report: list[dict[str, Any]] = []

    def _report(d: dict[str, Any], reason: str, parts: dict[str, str] | None = None) -> None:
        report.append(
            {
                "article_id": d["article_id"],
                "product_number": d.get("product_number"),
                "category": d.get("category"),
                "active_substance": d.get("active_substance"),
                "reason": reason,
                "parts": parts or {},
            }
        )

    for d in docs:
        if not d.get("has_document"):
            _report(d, "no_document_record")
            continue
        if d.get("doc_type") != LABEL_DOC_TYPE:
            continue
        if not d.get("has_medicine"):
            _report(d, "no_medicine_record")
            continue
        category = d.get("category")
        if category != HUMAN:
            _report(d, "veterinary" if category == VETERINARY else "not_human")
            continue
        parts = split_active_substance(d.get("active_substance"))
        if not parts:
            _report(d, "no_active_substance")
            continue
        linked = 0
        missed: dict[str, str] = {}
        for part in parts:
            n, unii = matches.get(part, (0, None))
            if n == 0:
                missed[part] = "no_match"
            elif n > 1:
                missed[part] = "ambiguous"
            elif not unii:
                missed[part] = "no_unii"
            else:
                rows.add((d["article_id"], unii))
                linked += 1
        if missed:
            reasons = set(missed.values())
            if linked:
                reason = "partial"
            else:
                reason = reasons.pop() if len(reasons) == 1 else "mixed"
            _report(d, reason, missed)
    report.sort(key=lambda r: r["article_id"])
    return sorted(rows), report


# ---------------------------------------------------------------------------
# Database reads
# ---------------------------------------------------------------------------
def releases(conn: psycopg.Connection, release_id: str | None = None) -> dict[str, str]:
    """``dataset -> release_id`` of the two feeds: the ``is_latest`` release of each, or
    ``release_id`` when given (it must exist for both). Raises ``EparLinkError`` when one
    is missing."""
    out: dict[str, str] = {}
    with conn.cursor() as cur:
        for dataset in (MED_DATASET, DOC_DATASET):
            if release_id is None:
                cur.execute(
                    "SELECT release_id FROM episteme.reg_release WHERE dataset = %s AND is_latest",
                    (dataset,),
                )
            else:
                cur.execute(
                    "SELECT release_id FROM episteme.reg_release "
                    "WHERE dataset = %s AND release_id = %s",
                    (dataset, release_id),
                )
            row = cur.fetchone()
            if row is None:
                which = "a latest release" if release_id is None else f"release {release_id}"
                raise EparLinkError(
                    f"{dataset} has no {which} in reg_release; no link was changed "
                    "(load the EMA feeds first)"
                )
            out[dataset] = row[0]
    return out


def _documents(conn: psycopg.Connection, rel: dict[str, str]) -> list[dict[str, Any]]:
    """One dict per loaded ``ema_epar:`` article, with its document and medicine fields.
    Dataset and release ids are literals, so the plan reads one leaf of each feed."""
    query = sql.SQL(
        """
        SELECT a.id,
               d.record_key IS NOT NULL,
               d.payload ->> 'type',
               d.payload ->> 'ema_product_number',
               m.record_key IS NOT NULL,
               m.payload ->> 'category',
               m.payload ->> 'active_substance'
          FROM episteme.articles a
          LEFT JOIN episteme.reg_record d
                 ON d.dataset = {doc_ds} AND d.release_id = {doc_rel}
                AND d.record_key = substr(a.id, {cut})
          LEFT JOIN episteme.reg_record m
                 ON m.dataset = {med_ds} AND m.release_id = {med_rel}
                AND m.record_key = d.payload ->> 'ema_product_number'
         WHERE a.source = {source} AND starts_with(a.id, {prefix})
         ORDER BY a.id
        """
    ).format(
        doc_ds=sql.Literal(DOC_DATASET),
        doc_rel=sql.Literal(rel[DOC_DATASET]),
        med_ds=sql.Literal(MED_DATASET),
        med_rel=sql.Literal(rel[MED_DATASET]),
        cut=sql.Literal(len(ARTICLE_PREFIX) + 1),
        source=sql.Literal(SOURCE),
        prefix=sql.Literal(ARTICLE_PREFIX),
    )
    keys = (
        "article_id",
        "has_document",
        "doc_type",
        "product_number",
        "has_medicine",
        "category",
        "active_substance",
    )
    with conn.cursor() as cur:
        cur.execute(query)
        return [dict(zip(keys, row, strict=True)) for row in cur.fetchall()]


def _matches(conn: psycopg.Connection, parts: list[str]) -> dict[str, tuple[int, str | None]]:
    """``part -> (distinct substances matched, their UNII when exactly one)``. The name key
    is ``norm_name`` with inner white space collapsed and outer space removed, as in
    ``resolve_entity``; ``ema_name`` rows repeat ``name`` rows, so substances are counted
    once."""
    if not parts:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.raw,
                   count(DISTINCT s.substance_key)::int,
                   CASE WHEN count(DISTINCT s.substance_key) = 1
                        THEN max(nullif(s.unii, '')) END
              FROM unnest(%s::text[]) AS p(raw)
              LEFT JOIN episteme.substance_xref x
                     ON x.id_type = ANY (%s::text[])
                    AND x.id_value = btrim(regexp_replace(
                            episteme_api_internal.norm_name(p.raw), '\\s+', ' ', 'g'))
              LEFT JOIN episteme.substance s ON s.substance_key = x.substance_key
             GROUP BY p.raw
            """,
            (parts, list(_ID_TYPES)),
        )
        return {raw: (n, unii) for raw, n, unii in cur.fetchall()}


def link_rows(
    conn: psycopg.Connection, release_id: str | None = None
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """``(article_id, unii)`` link rows and the report of unlinked labels (``decide``), for
    the loaded ``ema_epar`` articles. ``release_id`` ``None``: the latest release of each
    feed. Reads only."""
    _, rows, report = _build(conn, releases(conn, release_id))
    return rows, report


def _build(
    conn: psycopg.Connection, rel: dict[str, str]
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], list[dict[str, Any]]]:
    """The documents, the link rows and the report, for the releases ``rel``."""
    docs = _documents(conn, rel)
    parts: list[str] = []
    for d in docs:
        if d["doc_type"] == LABEL_DOC_TYPE and d["category"] == HUMAN:
            for p in split_active_substance(d["active_substance"]):
                if p not in parts:
                    parts.append(p)
    rows, report = decide(docs, _matches(conn, parts))
    return docs, rows, report


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
def write_report(processed_dir: Path, run_id: str, result: dict[str, Any]) -> Path:
    """Write the link report next to the run markers (``_ops/ema_epar/runs``)."""
    path = ops_root(Path(processed_dir), SOURCE) / "runs" / f"label_links_{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_marker(path, {"run_id": run_id, "source": SOURCE, "method": METHOD, **result})


def write_links(
    conn: psycopg.Connection,
    *,
    processed_dir: Path,
    release_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Replace the ``label_substance`` rows of the ``ema_epar:`` articles with the links of
    ``link_rows``, in one transaction with a ``load_commit`` audit row, then write the
    report. Returns the counts, the release ids and ``report_path``."""
    run_id = (
        run_id
        or get_settings().run_id
        or (f"{SOURCE}-links-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    )
    rel = releases(conn, release_id)  # raises before anything is deleted
    docs, rows, report = _build(conn, rel)
    label_docs = sum(1 for d in docs if d["doc_type"] == LABEL_DOC_TYPE)
    linked_docs = len({a for a, _ in rows})
    result: dict[str, Any] = {
        "releases": rel,
        "label_documents": label_docs,
        "linked_documents": linked_docs,
        "linked_rows": len(rows),
        "unlinked": report,
        "unlinked_by_reason": _by_reason(report),
    }
    try:
        with audit_trail.deferred_mirror(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM episteme.label_substance WHERE starts_with(article_id, %s)",
                    (ARTICLE_PREFIX,),
                )
                deleted = cur.rowcount
                with cur.copy(
                    "COPY episteme.label_substance (article_id, unii, ndc_product) FROM STDIN"
                ) as cp:
                    for article_id, unii in rows:
                        cp.write_row((article_id, unii, None))
            audit_trail.record(
                "load_commit",
                conn=conn,
                object=f"label_substance {SOURCE} {METHOD}",
                rows_affected=len(rows),
                old_value={"deleted": deleted},
                new_value={k: v for k, v in result.items() if k != "unlinked"},
                run_id=run_id,
            )
            audit_trail.commit(conn)
    except BaseException:
        audit_trail.rollback(conn)
        raise
    result["deleted_rows"] = deleted
    result["report_path"] = str(write_report(processed_dir, run_id, result))
    return result


def _by_reason(report: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in report:
        out[r["reason"]] = out.get(r["reason"], 0) + 1
    return dict(sorted(out.items()))


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    p = argparse.ArgumentParser(
        prog="python -m episteme.data.ema.epar_links",
        description="Link the loaded EU labels (ema_epar) to substances (ema_name_exact).",
    )
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--release-id",
        default=None,
        help="the release of both EMA feeds to read (default: the latest of each)",
    )
    # load_ema_epar.sh passes its own arguments to every step; these are accepted, unused.
    p.add_argument("--force", action="store_true", help="accepted, unused")
    p.add_argument("--reason", default=None, help="accepted, unused")
    p.add_argument("--skip-unit", action="append", default=None, help="accepted, unused")
    p.add_argument("--only", action="append", default=None, help="accepted, unused")
    p.add_argument("--workers", type=int, default=None, help="accepted, unused")
    args = p.parse_args(argv)

    from episteme.data.db.connection import connection

    try:
        with connection() as conn:
            res = write_links(conn, processed_dir=args.processed_dir, release_id=args.release_id)
    except EparLinkError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(
        f"ok {SOURCE} links method={METHOD} label_documents={res['label_documents']} "
        f"linked_documents={res['linked_documents']} rows={res['linked_rows']} "
        f"unlinked={res['unlinked_by_reason']} report={res['report_path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
