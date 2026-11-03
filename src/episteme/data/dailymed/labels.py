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
  once per article, and ``substance_key`` stays NULL until the substance link is built.

``replace_labels`` runs inside the transaction of the shard's load: it deletes the
label rows of the part (by the ``source_file`` of its articles) and copies the
sidecar. ``delete_labels`` is also what the retire step calls, so no label row
outlives its article.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

SIDECAR_DIR = "dailymed_labels"
SIDECAR_SUFFIX = ".labels.parquet"
SOURCE = "dailymed"

SIDECAR_COLUMNS = (
    "article_id",
    "kind",
    "loinc_code",
    "title",
    "start_offset",
    "end_offset",
    "unii",
    "unii_role",
    "ndc_product",
    "method",
)
_INT_COLUMNS = ("start_offset", "end_offset")
_COPY_BATCH_ROWS = 20_000


def sidecar_path(processed_dir: Path, part: str) -> Path:
    return Path(processed_dir) / "staging" / SIDECAR_DIR / f"{part}{SIDECAR_SUFFIX}"


def shard_sidecar(shard: Path) -> Path:
    """The sidecar of a shard ``staging/dailymed/<part>.parquet``."""
    shard = Path(shard)
    return shard.parent.parent / SIDECAR_DIR / f"{shard.stem}{SIDECAR_SUFFIX}"


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
    for ndc in parsed["ndcs"]:
        rows.append({"article_id": article_id, "kind": "substance", "ndc_product": ndc})
    return rows


def write_sidecar(processed_dir: Path, part: str, rows: list[dict[str, Any]]) -> Path:
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
    path = sidecar_path(processed_dir, part)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, path)
    return path


def delete_labels(cur, source_files: list[str]) -> int:
    """Delete the label rows of the articles whose ``source_file`` is in
    ``source_files``. Run it BEFORE the articles are deleted: it finds the
    articles by ``source_file``. Returns the count of deleted ``label_section`` rows."""
    names = sorted(set(source_files))
    cur.execute(
        """
        DELETE FROM episteme.label_section s
         USING episteme.articles a
         WHERE a.source = %s AND a.source_file = ANY(%s) AND s.article_id = a.id
        """,
        (SOURCE, names),
    )
    n = cur.rowcount
    cur.execute(
        """
        DELETE FROM episteme.label_substance s
         USING episteme.articles a
         WHERE a.source = %s AND a.source_file = ANY(%s) AND s.article_id = a.id
        """,
        (SOURCE, names),
    )
    return n


def replace_labels(conn, shard: Path) -> dict[str, int]:
    """Replace the label rows of the part ``shard`` (``staging/dailymed/<part>.parquet``)
    with its sidecar, in the open transaction of the caller (it never commits).

    Call it before the shard's articles are loaded: the delete finds the old rows
    through the old articles of the same ``source_file`` (the part name, the stem of
    the shard). A shard with no sidecar raises ``FileNotFoundError``: loading the
    articles without their labels would be silent and the load marker would hide it."""
    import pyarrow.parquet as pq

    shard = Path(shard)
    side = shard_sidecar(shard)
    if not side.is_file():
        raise FileNotFoundError(f"{shard.name}: no label sidecar at {side}")
    n_sections = n_substances = 0
    seen: set[tuple[str, str | None, str | None]] = set()
    # The ids of the incoming articles: a label that moved from another part of its bucket
    # is deleted from that part's articles by the load, but its label rows stay unless they
    # are deleted by id here.
    ids = sorted(set(pq.read_table(shard, columns=["id"]).column("id").to_pylist()))
    with conn.cursor() as cur:
        delete_labels(cur, [shard.stem])
        for table in ("label_section", "label_substance"):
            cur.execute(f"DELETE FROM episteme.{table} WHERE article_id = ANY(%s)", (ids,))
        with (
            cur.copy(
                "COPY episteme.label_section "
                "(article_id, loinc_code, title, start_offset, end_offset) FROM STDIN"
            ) as sec_copy,
            cur.copy(
                "COPY episteme.label_substance "
                "(article_id, substance_key, unii, ndc_product) FROM STDIN"
            ) as sub_copy,
            pq.ParquetFile(side, pre_buffer=False) as pf,
        ):
            for rb in pf.iter_batches(batch_size=_COPY_BATCH_ROWS):
                for r in rb.to_pylist():
                    if r["kind"] == "section":
                        sec_copy.write_row(
                            (
                                r["article_id"],
                                r["loinc_code"],
                                r["title"],
                                r["start_offset"],
                                r["end_offset"],
                            )
                        )
                        n_sections += 1
                    else:
                        key = (r["article_id"], r["unii"], r["ndc_product"])
                        if key in seen:  # a UNII is one row, whatever its role
                            continue
                        seen.add(key)
                        sub_copy.write_row((r["article_id"], None, r["unii"], r["ndc_product"]))
                        n_substances += 1
    return {"label_sections": n_sections, "label_substances": n_substances}
