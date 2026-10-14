"""Graph relation files written next to a source's articles shards.

One raw input file -> one Parquet file per relation under
``<processed>/staging/<source>_graph/<relation>/<stem>.parquet``. Every relation
file is written (empty ones too), so "no edges" differs from "not extracted".
All files go to ``.tmp`` names first and are renamed only by ``close()``; the
caller writes the success marker after that, so a crash leaves no marker and
the file is redone in full.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from episteme.data.checkpoint_markers import success_marker_path

RELATION_COLUMNS: dict[str, tuple[str, ...]] = {
    "mesh": (
        "pmid",
        "descriptor_ui",
        "descriptor_name",
        "major_topic",
        "qualifier_uis",
        "qualifiers",
        "source_file",
    ),
    "cites": ("src_pmid", "dst_pmid", "dst_doi", "ref_text", "source_file"),
    "comments": ("pmid", "ref_type", "ref_pmid", "ref_text", "source_file"),
    "chemicals": ("pmid", "substance_ui", "name", "registry_number", "source_file"),
    "keywords": ("pmid", "keyword", "owner", "major_topic", "source_file"),
    "authors": (
        "pmid",
        "position",
        "last_name",
        "fore_name",
        "initials",
        "collective_name",
        "orcid",
        "affiliations",
        "source_file",
    ),
}
RELATIONS: tuple[str, ...] = tuple(RELATION_COLUMNS)
_BOOL = {"major_topic"}
_INT = {"position"}
_LIST = {"qualifier_uis", "qualifiers", "affiliations"}


def graph_schema(relation: str):
    import pyarrow as pa

    fields = []
    for c in RELATION_COLUMNS[relation]:
        if c in _BOOL:
            t = pa.bool_()
        elif c in _INT:
            t = pa.int32()
        elif c in _LIST:
            t = pa.list_(pa.string())
        else:
            t = pa.string()
        fields.append(pa.field(c, t))
    return pa.schema(fields)


def _stem(source_file: str) -> str:
    stem = Path(source_file).name.replace("/", "_")
    if stem.endswith(".gz"):
        stem = stem[:-3]
    if stem.endswith(".xml"):
        stem = stem[:-4]
    return stem


def graph_file_path(processed_dir: Path, source: str, relation: str, source_file: str) -> Path:
    if relation not in RELATION_COLUMNS:
        raise KeyError(relation)
    return (
        Path(processed_dir)
        / "staging"
        / f"{source}_graph"
        / relation
        / f"{_stem(source_file)}.parquet"
    )


def graph_sha256(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


class GraphFileWriter:
    def __init__(
        self, processed_dir: Path, source: str, source_file: str, batch_rows: int = 50_000
    ) -> None:
        self._dir = Path(processed_dir)
        self._source = source
        self._source_file = source_file
        self._batch_rows = batch_rows
        self._buf: dict[str, list[dict[str, Any]]] = {r: [] for r in RELATIONS}
        self._rows: dict[str, int] = {r: 0 for r in RELATIONS}
        self._writers: dict[str, Any] = {}
        self._closed = False

    def _final(self, relation: str) -> Path:
        return graph_file_path(self._dir, self._source, relation, self._source_file)

    def _tmp(self, relation: str) -> Path:
        p = self._final(relation)
        return p.with_name(p.name + ".tmp")

    def _flush(self, relation: str) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        rows = self._buf[relation]
        if relation not in self._writers:
            tmp = self._tmp(relation)
            tmp.parent.mkdir(parents=True, exist_ok=True)
            self._writers[relation] = pq.ParquetWriter(
                tmp, graph_schema(relation), compression="zstd"
            )
        if rows:
            cols = RELATION_COLUMNS[relation]
            table = pa.table(
                {c: [r.get(c) for r in rows] for c in cols}, schema=graph_schema(relation)
            )
            self._writers[relation].write_table(table)
            self._rows[relation] += len(rows)
            self._buf[relation] = []

    def add(self, relation: str, rows: list[dict[str, Any]]) -> None:
        if relation not in self._buf:
            raise KeyError(relation)
        self._buf[relation].extend(rows)
        if len(self._buf[relation]) >= self._batch_rows:
            self._flush(relation)

    def close(self) -> dict[str, Any]:
        try:
            for rel in RELATIONS:
                self._flush(rel)
                self._writers[rel].close()
            for rel in RELATIONS:
                os.replace(self._tmp(rel), self._final(rel))
        except BaseException:
            self.abort()
            raise
        self._closed = True
        finals = [self._final(r) for r in RELATIONS]
        return {
            "graph_relations": list(RELATIONS),
            "graph_rows": dict(self._rows),
            "graph_sha256": graph_sha256(finals),
        }

    def abort(self) -> None:
        for w in self._writers.values():
            try:
                w.close()
            except Exception:  # noqa: BLE001 - best effort while unwinding
                pass
        for rel in RELATIONS:
            self._tmp(rel).unlink(missing_ok=True)
        self._closed = True

    def __enter__(self) -> GraphFileWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None and not self._closed:
            self.abort()


def marker_graph_info(processed_dir: Path, source: str, source_file: str) -> dict | None:
    path = success_marker_path(processed_dir, source, source_file)
    if not path.is_file():
        return None
    stats = json.loads(path.read_text(encoding="utf-8")).get("stats") or {}
    if "graph_relations" not in stats:
        return None
    return {
        "graph_relations": stats["graph_relations"],
        "graph_sha256": stats.get("graph_sha256"),
        "graph_rows": stats.get("graph_rows") or {},
    }
