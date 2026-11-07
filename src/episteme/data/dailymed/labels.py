"""The label side tables of a DailyMed part: sidecar file and database load.

``extract_dailymed`` writes, for each part, a sidecar next to the shard
directory: ``staging/dailymed_labels/<part>.labels.parquet``. It is a different
directory from the shard (``staging/dailymed/``) on purpose: tools that read every
``*.parquet`` of a source's staging directory (``load_articles``, the sample audit,
the gate report) would take the sidecar for a shard.

One row of the sidecar is either a section or a substance (``kind``):

- ``section``: ``article_id``, ``loinc_code``, ``title``, ``start_offset``,
  ``end_offset`` (character offsets into the stored ``body_text``). These go to
  ``episteme.label_section``.
- ``substance``: ``article_id`` and one of ``unii`` (with ``unii_role``
  ``active_moiety`` or ``active_ingredient``, and ``method`` ``xml``: the active
  moiety is read from the XML, not derived) or ``ndc_product``. These go to
  ``episteme.label_substance``; the table has no role column, so a UNII is stored
  once per article. No resolved substance key is stored (spec 5.3): a query joins
  ``label_substance.unii`` to ``substance.unii`` and its ``moiety_key``. ``ndc_product``
  is the 9-digit labeler-product key (``normalize.norm_ndc_product``), as in the
  ``product_keys`` of ``fda_ndc``; a value that is not an NDC product is kept as it is.

The module serves two sources (``LABEL_SOURCES``). ``dailymed`` writes sections and
substances. ``ema_epar`` writes sections only (SmPC sections, ``episteme.data.ema.smpc``),
each with ``ordinal`` and ``product`` (NULL for DailyMed). The sidecar directory is
``<source>_labels``. Every function takes the source as a parameter; the default is
``dailymed``.

``replace_labels`` runs inside the transaction of the shard's load: it deletes the
label rows of the part (by the ``source_file`` of its articles) and copies the
sidecar. ``delete_labels`` is also what the retire step calls, so no label row
outlives its article.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from episteme.data.regulatory.normalize import norm_ndc_product

SIDECAR_SUFFIX = ".labels.parquet"
SOURCE = "dailymed"
# Sources with label side tables. Only the sources of this module's sidecar format.
LABEL_SOURCES = ("dailymed", "ema_epar")
# The sections of these sources carry the order and the product (the COPY writes them).
_ORDERED_SOURCES = ("ema_epar",)


def sidecar_dir_name(source: str = SOURCE) -> str:
    if source not in LABEL_SOURCES:
        raise ValueError(f"{source!r} has no label tables")
    return f"{source}_labels"


SIDECAR_DIR = sidecar_dir_name(SOURCE)  # the DailyMed directory, kept for the earlier name

SIDECAR_COLUMNS = (
    "article_id",
    "kind",
    "loinc_code",
    "title",
    "start_offset",
    "end_offset",
    "ordinal",
    "product",
    "unii",
    "unii_role",
    "ndc_product",
    "method",
)
_INT_COLUMNS = ("start_offset", "end_offset", "ordinal")
_COPY_BATCH_ROWS = 20_000


def sidecar_path(processed_dir: Path, part: str, source: str = SOURCE) -> Path:
    return Path(processed_dir) / "staging" / sidecar_dir_name(source) / f"{part}{SIDECAR_SUFFIX}"


def shard_sidecar(shard: Path, source: str = SOURCE) -> Path:
    """The sidecar of a shard ``staging/<source>/<part>.parquet``."""
    shard = Path(shard)
    return shard.parent.parent / sidecar_dir_name(source) / f"{shard.stem}{SIDECAR_SUFFIX}"


def sidecar_rows(article_id: str, parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """The sidecar rows of one parsed label (``spl.parse_spl``)."""
    rows: list[dict[str, Any]] = []
    for s in parsed["sections"]:
        rows.append(
            {
                "article_id": article_id,
                "kind": "section",
                "loinc_code": s["loinc"],
                "title": s["title"],
                "start_offset": s["start"],
                "end_offset": s["end"],
            }
        )
    for sub in parsed["substances"]:
        rows.append(
            {
                "article_id": article_id,
                "kind": "substance",
                "unii": sub["unii"],
                "unii_role": sub["role"],
                "method": "xml",
            }
        )
    seen: set[str] = set()
    for ndc in parsed["ndcs"]:
        product = norm_ndc_product(ndc) or str(ndc).strip()
        if product and product not in seen:
            seen.add(product)
            rows.append({"article_id": article_id, "kind": "substance", "ndc_product": product})
    return rows


def write_sidecar(
    processed_dir: Path, part: str, rows: list[dict[str, Any]], source: str = SOURCE
) -> Path:
    """Write the sidecar of ``part`` (also with no rows: a missing sidecar is an
    error at load time, an empty one is not). Atomic: temp file, then rename."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    cols: dict[str, list[Any]] = {c: [r.get(c) for r in rows] for c in SIDECAR_COLUMNS}
    schema = pa.schema(
        [pa.field(c, pa.int32() if c in _INT_COLUMNS else pa.string()) for c in SIDECAR_COLUMNS]
    )
    table = pa.table(
        {c: pa.array(cols[c], type=schema.field(c).type) for c in SIDECAR_COLUMNS}, schema=schema
    )
    path = sidecar_path(processed_dir, part, source)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, path)
    return path


def delete_labels(cur, source: str, source_files: list[str]) -> int:
    """Delete the label rows of the articles of ``source`` whose ``source_file`` is in
    ``source_files``. Run it BEFORE the articles are deleted: it finds the
    articles by ``source_file``. Returns the count of deleted ``label_section`` rows."""
    names = sorted(set(source_files))
    cur.execute(
        """
        DELETE FROM episteme.label_section s
         USING episteme.articles a
         WHERE a.source = %s AND a.source_file = ANY(%s) AND s.article_id = a.id
        """,
        (source, names),
    )
    n = cur.rowcount
    cur.execute(
        """
        DELETE FROM episteme.label_substance s
         USING episteme.articles a
         WHERE a.source = %s AND a.source_file = ANY(%s) AND s.article_id = a.id
        """,
        (source, names),
    )
    return n


def replace_labels(conn, source: str, shard: Path) -> dict[str, int]:
    """Replace the label rows of the part ``shard`` (``staging/<source>/<part>.parquet``)
    with its sidecar, in the open transaction of the caller (it never commits).

    Call it before the shard's articles are loaded: the delete finds the old rows
    through the old articles of the same ``source_file`` (the part name, the stem of
    the shard). A shard with no sidecar raises ``FileNotFoundError``: loading the
    articles without their labels would be silent and the load marker would hide it."""
    import pyarrow.parquet as pq

    shard = Path(shard)
    side = shard_sidecar(shard, source)
    if not side.is_file():
        raise FileNotFoundError(f"{shard.name}: no label sidecar at {side}")
    n_sections = n_substances = 0
    seen: set[tuple[str, str | None, str | None]] = set()
    # The ids of the incoming articles: a label that moved from another part of its bucket
    # is deleted from that part's articles by the load, but its label rows stay unless they
    # are deleted by id here.
    ids = sorted(set(pq.read_table(shard, columns=["id"]).column("id").to_pylist()))
    with conn.cursor() as cur:
        delete_labels(cur, source, [shard.stem])
        for table in ("label_section", "label_substance"):
            cur.execute(f"DELETE FROM episteme.{table} WHERE article_id = ANY(%s)", (ids,))
        # One COPY at a time per connection: the sidecar is read twice, once per table.
        ordered = source in _ORDERED_SOURCES
        columns = "article_id, loinc_code, title, start_offset, end_offset"
        if ordered:
            columns += ", ordinal, product"
        with cur.copy(f"COPY episteme.label_section ({columns}) FROM STDIN") as sec_copy:
            for r in _sidecar_rows(side, "section"):
                values = (
                    r["article_id"],
                    r["loinc_code"],
                    r["title"],
                    r["start_offset"],
                    r["end_offset"],
                )
                if ordered:
                    values += (r.get("ordinal"), r.get("product"))
                sec_copy.write_row(values)
                n_sections += 1
        with cur.copy(
            "COPY episteme.label_substance (article_id, unii, ndc_product) FROM STDIN"
        ) as sub_copy:
            for r in _sidecar_rows(side, "substance"):
                key = (r["article_id"], r["unii"], r["ndc_product"])
                if key in seen:  # a UNII is one row, whatever its role
                    continue
                seen.add(key)
                sub_copy.write_row(key)
                n_substances += 1
    return {"label_sections": n_sections, "label_substances": n_substances}


def _sidecar_rows(side: Path, kind: str):
    """The rows of one ``kind`` of a sidecar, in batches of ``_COPY_BATCH_ROWS``."""
    import pyarrow.parquet as pq

    with pq.ParquetFile(side, pre_buffer=False) as pf:
        for rb in pf.iter_batches(batch_size=_COPY_BATCH_ROWS):
            for r in rb.to_pylist():
                if (r["kind"] == "section") == (kind == "section"):
                    yield r
