# PubMed graph, corrected extraction and MeSH facets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One corrected PubMed extract that writes six graph relations next to each articles
shard, loads them into indexed graph tables with COPY, derives MeSH facets per file, and exposes
search helpers.

**Architecture:** `extract_pubmed` parses each `<PubmedArticle>` once. A new `relations` module
turns the record into edge rows, and a new `graph_files` writer streams them to
`staging/pubmed_graph/<relation>/<stem>.parquet`; the success marker, written last, records the
relation set and a hash. Migration 0006 adds the tables. A new `graph_load` module loads one raw
file's relation files per transaction (delete-if-graphed, COPY, facets, audit, `_graph_lineage`).
`bulk_indexes` is generalised to index groups so `graph --bulk` works.

**Tech Stack:** Python 3.13+, pyarrow, psycopg 3 (COPY), defusedxml iterparse, DuckDB (audit),
PostgreSQL 19 (local), bash wrappers, pytest (`pg` marker for DB tests).

**Spec:** `docs/superpowers/specs/2026-10-14-pubmed-graph-design.md`

## Global Constraints

- `src/episteme/config.py` is the only module that reads `os.environ`.
- Endpoint URLs live only in `scripts/data/_lib/sources.env`.
- Every extractor keys its marker and stored `source_file` on `input_key(path, raw_dir)`, with a
  collision test (project rule; applies to graph files too).
- `SCHEMA_VERSION` becomes `"1.6"` with a changelog line.
- Every stored text element of a list column, and `ref_text`, `affiliations` elements and
  `keyword`, passes through the 2,000-byte cap (`MAX_LIST_ELEMENT_BYTES`).
- ORCID stored only as `0000-0000-0000-000X` (16 characters with hyphens; last may be `X`);
  anything else is dropped and counted.
- `is_retracted` is true only for publication type exactly `Retracted Publication` or a
  `CommentsCorrections RefType="RetractionIn"`.
- Migration 0006 is one `BEGIN; … COMMIT;` transaction. The pipeline writes as `episteme_app`, so
  the data tables and `_graph_lineage` keep `schema.sql`'s standard grants; only
  `mesh_facet_rule` is read-only for `episteme_app` (REVOKE INSERT, UPDATE, DELETE, TRUNCATE).
- Every edge table and `article_facets` keeps a permanent `(source_file)` index that bulk mode
  never drops.
- `pg` tests use only the `pg_conn` fixture (`tests/conftest.py`), which pins `episteme_test`.
- Run `ruff format` and `ruff check` before every commit; `git add` explicit paths only; never
  `--no-verify`; never stage `.gitignore` or `.env`.
- Commit trailer on every commit:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z`.
- Worktree setup once: `uv sync --all-extras` (without the extras, `psycopg` is missing and
  collection fails). Test modules in `tests/data/` may import shared helpers relatively
  (`from .test_pubmed_relations import RECORD`); this was verified to work.
- Not-pg suite: `uv run --no-sync pytest -q -m "not pg"`. pg tests: source `.env`
  (`set -a; . ../Episteme/.env; set +a` from the worktree), set `TEST_PG_DSN` to the
  `episteme_test` DSN, then `uv run --no-sync pytest -q -m pg <file>`.

## Review Focus

1. **A record with no PMID** (row id `pubmed:<file>:unknown`) must produce no edge rows and be
   counted as `edges_skipped_no_pmid`, not write edges keyed on `""`. Test in Task 2.
2. **A re-graph after the relation files changed** must replace, never duplicate: the old rows of
   that source file are deleted and the new ones loaded, and the counts match the files. Test in
   Task 8.
3. **A relation file missing from an otherwise extracted file** (manual deletion, partial copy)
   must fail that file with a clear error naming the relation, not load a partial set. Test in
   Task 8.
4. **An overlong keyword** must be capped: `lower(keyword)` has a btree index, whose entries max
   out near 2,700 bytes. Test in Task 2.
5. **A chemical whose `substance_ui` is a Supplementary Concept** (`C…` UI, not in `mesh_tree`)
   must not produce a `drug` facet; only tree-D descriptors do. Test in Task 8.

---

### Task 1: Graph file schemas and streaming writer

**Files:**
- Create: `src/episteme/data/graph_files.py`
- Test: `tests/data/test_graph_files.py`

**Interfaces:**
- Produces:
  - `RELATION_COLUMNS: dict[str, tuple[str, ...]]`, the column order per relation:
    - `"mesh": ("pmid", "descriptor_ui", "descriptor_name", "major_topic", "qualifier_uis", "qualifiers", "source_file")`
    - `"cites": ("src_pmid", "dst_pmid", "dst_doi", "ref_text", "source_file")`
    - `"comments": ("pmid", "ref_type", "ref_pmid", "ref_text", "source_file")`
    - `"chemicals": ("pmid", "substance_ui", "name", "registry_number", "source_file")`
    - `"keywords": ("pmid", "keyword", "owner", "major_topic", "source_file")`
    - `"authors": ("pmid", "position", "last_name", "fore_name", "initials", "collective_name", "orcid", "affiliations", "source_file")`
  - `RELATIONS: tuple[str, ...] = tuple(RELATION_COLUMNS)`.
  - `graph_schema(relation: str) -> pyarrow.Schema`. Booleans: `major_topic`. int32:
    `position`. `list<string>`: `qualifier_uis`, `qualifiers`, `affiliations`. Every other
    column: string.
  - `graph_file_path(processed_dir: Path, source: str, relation: str, source_file: str) -> Path`
    returns `<processed_dir>/staging/<source>_graph/<relation>/<stem>.parquet`, using the same
    stem rule as `staging_writer._out_path`: `Path(source_file).name` with `/` replaced by `_`,
    then `.gz` and then `.xml` stripped.
  - `class GraphFileWriter(processed_dir: Path, source: str, source_file: str, batch_rows: int = 50_000)`:
    - `add(relation: str, rows: list[dict])` buffers rows and flushes a row group at
      `batch_rows`;
    - `close() -> dict` writes every relation file (empty ones too) to `<path>.tmp`, then
      `os.replace`s all of them, and returns
      `{"graph_relations": list(RELATIONS), "graph_rows": {rel: n}, "graph_sha256": hex}`;
    - `abort()` deletes every `.tmp` file;
    - it is also a context manager: `abort()` on an exception, and `close()` is never implied.
  - `graph_sha256(paths: list[Path]) -> str`: sha256 over the bytes of the files in the given
    order.
  - `marker_graph_info(processed_dir: Path, source: str, source_file: str) -> dict | None` reads
    the success marker. It returns `stats["graph_relations"]`, `stats["graph_sha256"]` and
    `stats["graph_rows"]` as a dict, or `None` when the marker or `graph_relations` is absent.

- [ ] **Step 1: Write the failing tests**

```python
"""graph_files: fixed per-relation schemas, a streaming writer that publishes all
relation files atomically (empty ones too), and marker read-back. No DB."""

from __future__ import annotations

import json

import pyarrow.parquet as pq
import pytest

from episteme.data import graph_files as gf
from episteme.data.checkpoint_markers import mark_success


def _mesh(pmid: str) -> dict:
    return {
        "pmid": pmid,
        "descriptor_ui": "D006801",
        "descriptor_name": "Humans",
        "major_topic": False,
        "qualifier_uis": [],
        "qualifiers": [],
        "source_file": "baseline__pubmed26n0001.xml.gz",
    }


def test_relations_and_schemas_are_fixed():
    assert gf.RELATIONS == ("mesh", "cites", "comments", "chemicals", "keywords", "authors")
    s = gf.graph_schema("authors")
    assert s.names == list(gf.RELATION_COLUMNS["authors"])
    assert str(s.field("position").type) == "int32"
    assert str(s.field("affiliations").type) == "list<item: string>"
    assert str(gf.graph_schema("mesh").field("major_topic").type) == "bool"


def test_graph_file_path_uses_shard_stem(tmp_path):
    p = gf.graph_file_path(tmp_path, "pubmed", "mesh", "baseline__pubmed26n0001.xml.gz")
    assert p == tmp_path / "staging" / "pubmed_graph" / "mesh" / "baseline__pubmed26n0001.parquet"


def test_writer_publishes_every_relation_including_empty(tmp_path):
    sf = "baseline__pubmed26n0001.xml.gz"
    with gf.GraphFileWriter(tmp_path, "pubmed", sf, batch_rows=2) as w:
        w.add("mesh", [_mesh("1"), _mesh("2"), _mesh("3")])
        info = w.close()
    assert info["graph_relations"] == list(gf.RELATIONS)
    assert info["graph_rows"]["mesh"] == 3
    assert info["graph_rows"]["cites"] == 0
    for rel in gf.RELATIONS:
        path = gf.graph_file_path(tmp_path, "pubmed", rel, sf)
        assert path.is_file()
        assert pq.read_schema(path).names == list(gf.RELATION_COLUMNS[rel])
    assert pq.read_table(gf.graph_file_path(tmp_path, "pubmed", "mesh", sf)).num_rows == 3
    assert not list(tmp_path.rglob("*.tmp"))
    assert len(info["graph_sha256"]) == 64


def test_writer_aborts_on_exception_and_leaves_nothing(tmp_path):
    sf = "baseline__pubmed26n0002.xml.gz"
    with pytest.raises(RuntimeError):
        with gf.GraphFileWriter(tmp_path, "pubmed", sf, batch_rows=1) as w:
            w.add("mesh", [_mesh("1"), _mesh("2")])
            raise RuntimeError("boom")
    assert not list(tmp_path.rglob("*.parquet"))
    assert not list(tmp_path.rglob("*.tmp"))


def test_unknown_relation_is_rejected(tmp_path):
    with gf.GraphFileWriter(tmp_path, "pubmed", "x.xml.gz") as w:
        with pytest.raises(KeyError):
            w.add("grants", [{}])
        w.abort()


def test_marker_graph_info_round_trip(tmp_path):
    sf = "baseline__pubmed26n0003.xml.gz"
    assert gf.marker_graph_info(tmp_path, "pubmed", sf) is None
    mark_success(tmp_path, "pubmed", sf, stats={"n_rows": 1})
    assert gf.marker_graph_info(tmp_path, "pubmed", sf) is None  # no graph keys: old marker
    stats = {"graph_relations": list(gf.RELATIONS), "graph_sha256": "a" * 64, "graph_rows": {}}
    mark_success(tmp_path, "pubmed", sf, stats=stats)
    got = gf.marker_graph_info(tmp_path, "pubmed", sf)
    assert got["graph_sha256"] == "a" * 64
    assert json.loads(json.dumps(got)) == got


def test_sha256_changes_with_content(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    assert gf.graph_sha256([a, b]) != gf.graph_sha256([b, a])
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run --no-sync pytest -q tests/data/test_graph_files.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'episteme.data.graph_files'`.

- [ ] **Step 3: Implement `src/episteme/data/graph_files.py`**

```python
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
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run --no-sync pytest -q tests/data/test_graph_files.py`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
uv run --no-sync ruff format src/episteme/data/graph_files.py tests/data/test_graph_files.py
uv run --no-sync ruff check src tests
git add src/episteme/data/graph_files.py tests/data/test_graph_files.py
git commit -F <message file>   # "feat(graph): per-relation graph files with an atomic streaming writer" + trailer
```

---

### Task 2: PubMed relation parsers

**Files:**
- Create: `src/episteme/data/pubmed/relations.py`
- Modify: `src/episteme/data/article_schema.py` (add the public `cap_text`)
- Test: `tests/data/test_pubmed_relations.py`

**Interfaces:**
- Consumes: `graph_files.RELATIONS`; `article_schema.cap_list_elements`.
- Produces:
  - `article_schema.cap_text(s: str | None) -> str | None`: `None` passes through; otherwise
    `cap_list_elements([s])[0]`.
  - `own_article_ids(art) -> dict[str, str]`: lower-cased `IdType` to stripped value, read only
    from the direct `ArticleId` children of `PubmedData/ArticleIdList`.
  - `normalize_orcid(raw: str | None) -> str | None`.
  - `extract_relations(art, pmid: str, source_file: str) -> tuple[dict[str, list[dict]], dict[str, int]]`:
    returns edges keyed by every name in `RELATIONS`, plus counters with keys
    `orcid_dropped` and `edges_skipped_no_pmid`.
  - `retracted(pub_types: list[str], comments: list[dict]) -> bool`.

Element-walking rule: match on local tag names (`_local`), and walk **direct children by path**,
never `iter()`, except inside one `Reference` (a nested `ReferenceList` is handled explicitly).

- [ ] **Step 1: Write the failing tests**

```python
"""PubMed relation parsers: each relation reads only its own element path. No DB."""

from __future__ import annotations

from defusedxml.ElementTree import fromstring

from episteme.data.article_schema import MAX_LIST_ELEMENT_BYTES
from episteme.data.graph_files import RELATIONS
from episteme.data.pubmed import relations as rel

SF = "baseline__pubmed26n0001.xml.gz"

RECORD = """
<PubmedArticle>
  <MedlineCitation>
    <PMID Version="1">40571748</PMID>
    <Article>
      <AuthorList>
        <Author><LastName>Doe</LastName><ForeName>Jane</ForeName><Initials>J</Initials>
          <Identifier Source="ORCID">https://orcid.org/0000-0002-1825-009X</Identifier>
          <AffiliationInfo><Affiliation>Dept A, Univ X.</Affiliation></AffiliationInfo>
          <AffiliationInfo><Affiliation>Hospital Y.</Affiliation></AffiliationInfo>
        </Author>
        <Author><CollectiveName>COVID Study Group</CollectiveName></Author>
        <Author><LastName>Roe</LastName><Identifier Source="ORCID">12345</Identifier></Author>
      </AuthorList>
      <PublicationTypeList>
        <PublicationType UI="D016428">Journal Article</PublicationType>
        <PublicationType UI="D016441">Retracted Publication</PublicationType>
      </PublicationTypeList>
    </Article>
    <ChemicalList>
      <Chemical><RegistryNumber>0</RegistryNumber><NameOfSubstance UI="D007328">Insulin</NameOfSubstance></Chemical>
      <Chemical><RegistryNumber>EC 1.1.1.1</RegistryNumber><NameOfSubstance UI="C000123">Foo protein</NameOfSubstance></Chemical>
    </ChemicalList>
    <CommentsCorrectionsList>
      <CommentsCorrections RefType="RetractionIn"><RefSource>J X 2025</RefSource><PMID Version="1">41000001</PMID></CommentsCorrections>
      <CommentsCorrections RefType="CommentOn"><RefSource>J Y 2020</RefSource></CommentsCorrections>
    </CommentsCorrectionsList>
    <MeshHeadingList>
      <MeshHeading><DescriptorName UI="D006801" MajorTopicYN="N">Humans</DescriptorName></MeshHeading>
      <MeshHeading>
        <DescriptorName UI="D003920" MajorTopicYN="N">Diabetes Mellitus</DescriptorName>
        <QualifierName UI="Q000188" MajorTopicYN="Y">drug therapy</QualifierName>
        <QualifierName UI="Q000453" MajorTopicYN="N">epidemiology</QualifierName>
      </MeshHeading>
    </MeshHeadingList>
    <KeywordList Owner="NOTNLM">
      <Keyword MajorTopicYN="N">insulin resistance</Keyword>
    </KeywordList>
  </MedlineCitation>
  <PubmedData>
    <ArticleIdList>
      <ArticleId IdType="pubmed">40571748</ArticleId>
      <ArticleId IdType="doi">10.1007/s00059-025-05325-x</ArticleId>
      <ArticleId IdType="pmc">PMC123</ArticleId>
    </ArticleIdList>
    <ReferenceList>
      <Reference><Citation>Ref one.</Citation>
        <ArticleIdList><ArticleId IdType="pubmed">111</ArticleId><ArticleId IdType="doi">10.1/one</ArticleId></ArticleIdList>
      </Reference>
      <Reference><Citation>Ref two, no ids.</Citation></Reference>
      <ReferenceList><Reference><Citation>Nested.</Citation>
        <ArticleIdList><ArticleId IdType="doi">10.1016/j.lanhl.2024.100671</ArticleId><ArticleId IdType="pmc">PMC999</ArticleId></ArticleIdList>
      </Reference></ReferenceList>
    </ReferenceList>
  </PubmedData>
</PubmedArticle>
"""


def _art():
    return fromstring(RECORD)


def test_own_ids_ignore_reference_ids():
    ids = rel.own_article_ids(_art())
    assert ids["doi"] == "10.1007/s00059-025-05325-x"
    assert ids["pmc"] == "PMC123"


def test_every_relation_key_present():
    edges, counters = rel.extract_relations(_art(), "40571748", SF)
    assert tuple(edges) == RELATIONS
    assert counters["edges_skipped_no_pmid"] == 0


def test_mesh_rows():
    edges, _ = rel.extract_relations(_art(), "40571748", SF)
    m = {r["descriptor_ui"]: r for r in edges["mesh"]}
    assert m["D006801"]["major_topic"] is False
    assert m["D003920"]["major_topic"] is True  # a qualifier is major
    assert m["D003920"]["qualifier_uis"] == ["Q000188", "Q000453"]
    assert m["D003920"]["qualifiers"] == ["drug therapy", "epidemiology"]
    assert all(r["pmid"] == "40571748" and r["source_file"] == SF for r in edges["mesh"])


def test_cites_rows_include_unresolved_and_nested():
    edges, _ = rel.extract_relations(_art(), "40571748", SF)
    c = edges["cites"]
    assert [r["dst_pmid"] for r in c] == ["111", None, None]
    assert c[0]["dst_doi"] == "10.1/one"
    assert c[1]["ref_text"] == "Ref two, no ids."
    assert c[2]["dst_doi"] == "10.1016/j.lanhl.2024.100671"
    assert all(r["src_pmid"] == "40571748" for r in c)


def test_comments_chemicals_keywords():
    edges, _ = rel.extract_relations(_art(), "40571748", SF)
    assert [(r["ref_type"], r["ref_pmid"]) for r in edges["comments"]] == [
        ("RetractionIn", "41000001"),
        ("CommentOn", None),
    ]
    assert edges["comments"][1]["ref_text"] == "J Y 2020"
    chem = edges["chemicals"]
    assert (chem[0]["substance_ui"], chem[0]["name"], chem[0]["registry_number"]) == (
        "D007328",
        "Insulin",
        None,  # "0" means no registry number
    )
    assert chem[1]["registry_number"] == "EC 1.1.1.1"
    kw = edges["keywords"][0]
    assert (kw["keyword"], kw["owner"], kw["major_topic"]) == ("insulin resistance", "NOTNLM", False)


def test_authors_orcid_and_affiliations():
    edges, counters = rel.extract_relations(_art(), "40571748", SF)
    a = edges["authors"]
    assert [x["position"] for x in a] == [1, 2, 3]
    assert a[0]["orcid"] == "0000-0002-1825-009X"
    assert a[0]["affiliations"] == ["Dept A, Univ X.", "Hospital Y."]
    assert a[1]["collective_name"] == "COVID Study Group"
    assert a[2]["orcid"] is None
    assert counters["orcid_dropped"] == 1


def test_normalize_orcid():
    assert rel.normalize_orcid("0000-0002-1825-009x") == "0000-0002-1825-009X"
    assert rel.normalize_orcid("http://orcid.org/0000000218250097") == "0000-0002-1825-0097"
    assert rel.normalize_orcid("orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert rel.normalize_orcid("12345") is None
    assert rel.normalize_orcid(None) is None


def test_retracted_rule():
    assert rel.retracted(["Retracted Publication"], []) is True
    assert rel.retracted(["Retraction of Publication"], []) is False
    assert rel.retracted([], [{"ref_type": "RetractionIn"}]) is True
    assert rel.retracted(["Journal Article"], [{"ref_type": "RetractionOf"}]) is False


def test_no_pmid_means_no_edges():
    edges, counters = rel.extract_relations(_art(), "", SF)
    assert all(edges[r] == [] for r in RELATIONS)
    assert counters["edges_skipped_no_pmid"] == 1


def test_long_text_is_capped_including_keywords():
    long = "x" * 5000
    xml = RECORD.replace("insulin resistance", long).replace("Ref two, no ids.", long)
    edges, _ = rel.extract_relations(fromstring(xml), "40571748", SF)
    assert len(edges["keywords"][0]["keyword"].encode()) <= MAX_LIST_ELEMENT_BYTES
    assert len(edges["cites"][1]["ref_text"].encode()) <= MAX_LIST_ELEMENT_BYTES
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run --no-sync pytest -q tests/data/test_pubmed_relations.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'episteme.data.pubmed.relations'`.

- [ ] **Step 3: Add `cap_text` to `article_schema.py`**, directly after `cap_list_elements`:

```python
def cap_text(s: str | None) -> str | None:
    """One string under the same ``MAX_LIST_ELEMENT_BYTES`` cap as list elements."""
    if s is None:
        return None
    return cap_list_elements([s])[0]
```

- [ ] **Step 4: Implement `src/episteme/data/pubmed/relations.py`**

```python
"""Graph relations of one PubMed ``<PubmedArticle>`` (schema 1.6).

Each function reads only its own element path, by direct children -- never
``iter()`` over ``PubmedData``, which also walks every cited reference's
``ArticleIdList`` (the pre-1.6 DOI bug).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from episteme.data.article_schema import cap_list_elements, cap_text
from episteme.data.graph_files import RELATIONS

_ORCID = re.compile(r"(\d{4})-?(\d{4})-?(\d{4})-?(\d{3}[\dX])$")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _kids(el, name: str) -> list:
    return [] if el is None else [c for c in el if _local(c.tag) == name]


def _kid(el, name: str):
    k = _kids(el, name)
    return k[0] if k else None


def _path(el, *names: str):
    for n in names:
        el = _kid(el, n)
        if el is None:
            return None
    return el


def _text(el) -> str | None:
    if el is None:
        return None
    t = "".join(el.itertext()).strip()
    return t or None


def _ids(id_list) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in _kids(id_list, "ArticleId"):
        v = (a.text or "").strip()
        k = (a.get("IdType") or "").lower()
        if v and k and k not in out:
            out[k] = v
    return out


def own_article_ids(art) -> dict[str, str]:
    return _ids(_path(art, "PubmedData", "ArticleIdList"))


def normalize_orcid(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().upper().rstrip("/")
    s = s.rsplit("/", 1)[-1]
    m = _ORCID.search(s)
    if not m or len(s.replace("-", "")) != 16:
        return None
    return "-".join(m.groups())


def retracted(pub_types: list[str], comments: list[dict[str, Any]]) -> bool:
    if "Retracted Publication" in pub_types:
        return True
    return any(c.get("ref_type") == "RetractionIn" for c in comments)


def _mesh(medline, pmid, sf):
    rows = []
    for mh in _kids(_kid(medline, "MeshHeadingList"), "MeshHeading"):
        d = _kid(mh, "DescriptorName")
        if d is None:
            continue
        quals = _kids(mh, "QualifierName")
        major = d.get("MajorTopicYN") == "Y" or any(q.get("MajorTopicYN") == "Y" for q in quals)
        rows.append(
            {
                "pmid": pmid,
                "descriptor_ui": d.get("UI"),
                "descriptor_name": cap_text(_text(d)),
                "major_topic": major,
                "qualifier_uis": [q.get("UI") for q in quals if q.get("UI")],
                "qualifiers": cap_list_elements([_text(q) for q in quals if _text(q)]),
                "source_file": sf,
            }
        )
    return rows


def _references(ref_list):
    for c in ref_list if ref_list is not None else []:
        n = _local(c.tag)
        if n == "Reference":
            yield c
        elif n == "ReferenceList":
            yield from _references(c)


def _cites(art, pmid, sf):
    rows = []
    for ref in _references(_path(art, "PubmedData", "ReferenceList")):
        ids = _ids(_kid(ref, "ArticleIdList"))
        rows.append(
            {
                "src_pmid": pmid,
                "dst_pmid": ids.get("pubmed"),
                "dst_doi": ids.get("doi"),
                "ref_text": cap_text(_text(_kid(ref, "Citation"))),
                "source_file": sf,
            }
        )
    return rows


def _comments(medline, pmid, sf):
    rows = []
    for cc in _kids(_kid(medline, "CommentsCorrectionsList"), "CommentsCorrections"):
        rows.append(
            {
                "pmid": pmid,
                "ref_type": cc.get("RefType"),
                "ref_pmid": _text(_kid(cc, "PMID")),
                "ref_text": cap_text(_text(_kid(cc, "RefSource"))),
                "source_file": sf,
            }
        )
    return rows


def _chemicals(medline, pmid, sf):
    rows = []
    for ch in _kids(_kid(medline, "ChemicalList"), "Chemical"):
        name = _kid(ch, "NameOfSubstance")
        reg = _text(_kid(ch, "RegistryNumber"))
        rows.append(
            {
                "pmid": pmid,
                "substance_ui": None if name is None else name.get("UI"),
                "name": cap_text(_text(name)),
                "registry_number": None if reg in (None, "0") else reg,
                "source_file": sf,
            }
        )
    return rows


def _keywords(medline, pmid, sf):
    rows = []
    for kl in _kids(medline, "KeywordList"):
        owner = kl.get("Owner")
        for k in _kids(kl, "Keyword"):
            text = _text(k)
            if text:
                rows.append(
                    {
                        "pmid": pmid,
                        "keyword": cap_text(text),
                        "owner": owner,
                        "major_topic": k.get("MajorTopicYN") == "Y",
                        "source_file": sf,
                    }
                )
    return rows


def _authors(medline, pmid, sf, counters):
    rows = []
    authors = _kids(_path(medline, "Article", "AuthorList"), "Author")
    for pos, a in enumerate(authors, start=1):
        orcid = None
        for ident in _kids(a, "Identifier"):
            if (ident.get("Source") or "").upper() == "ORCID":
                orcid = normalize_orcid(ident.text)
                if orcid is None:
                    counters["orcid_dropped"] += 1
                break
        affs = [_text(_kid(ai, "Affiliation")) for ai in _kids(a, "AffiliationInfo")]
        rows.append(
            {
                "pmid": pmid,
                "position": pos,
                "last_name": cap_text(_text(_kid(a, "LastName"))),
                "fore_name": cap_text(_text(_kid(a, "ForeName"))),
                "initials": _text(_kid(a, "Initials")),
                "collective_name": cap_text(_text(_kid(a, "CollectiveName"))),
                "orcid": orcid,
                "affiliations": cap_list_elements([x for x in affs if x]) or None,
                "source_file": sf,
            }
        )
    return rows


def extract_relations(art, pmid: str, source_file: str):
    counters: Counter[str] = Counter({"orcid_dropped": 0, "edges_skipped_no_pmid": 0})
    edges: dict[str, list[dict[str, Any]]] = {r: [] for r in RELATIONS}
    if not pmid:
        counters["edges_skipped_no_pmid"] += 1
        return edges, dict(counters)
    medline = _kid(art, "MedlineCitation")
    edges["mesh"] = _mesh(medline, pmid, source_file)
    edges["cites"] = _cites(art, pmid, source_file)
    edges["comments"] = _comments(medline, pmid, source_file)
    edges["chemicals"] = _chemicals(medline, pmid, source_file)
    edges["keywords"] = _keywords(medline, pmid, source_file)
    edges["authors"] = _authors(medline, pmid, source_file, counters)
    return edges, dict(counters)
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run --no-sync pytest -q tests/data/test_pubmed_relations.py tests/data/test_article_schema.py`
Expected: all pass.

- [ ] **Step 6: Commit** (ruff format and check first): `feat(pubmed): relation parsers for mesh, cites, comments, chemicals, keywords, authors`.

---

### Task 3: Correct the PubMed article row (DOI, PMCID, retraction; schema 1.6)

**Files:**
- Modify: `src/episteme/data/pubmed/extract_pubmed.py` (`parse_pubmed_article`, about lines 85–250; `iter_rows_from_file`)
- Modify: `src/episteme/data/article_schema.py` (`SCHEMA_VERSION = "1.6"`, a changelog line)
- Modify: `tests/data/test_article_schema.py` (`assert SCHEMA_VERSION == "1.6"`)
- Test: `tests/data/test_extract_pubmed_record.py`

**Interfaces:**
- Consumes: `relations.own_article_ids`, `relations.extract_relations`, `relations.retracted`.
- Produces:
  - `parse_pubmed_record(art, source_file: str) -> tuple[dict, dict[str, list[dict]], dict[str, int]]`
    returns (article row, edges, counters);
  - `parse_pubmed_article(art, source_file)`, unchanged signature, returns
    `parse_pubmed_record(...)[0]`;
  - `iter_records_from_file(path: Path, *, source_file: str) -> Iterator[tuple[dict, dict, dict]]`
    streams like `iter_rows_from_file`, which is kept and yields `rec[0]`.

- [ ] **Step 1: Write the failing tests**

```python
"""Schema 1.6 article row: own ids only, retraction rule, edges returned. No DB."""

from __future__ import annotations

from defusedxml.ElementTree import fromstring

from episteme.data.article_schema import SCHEMA_VERSION
from episteme.data.pubmed.extract_pubmed import parse_pubmed_article, parse_pubmed_record

from .test_pubmed_relations import RECORD, SF


def test_schema_is_1_6():
    assert SCHEMA_VERSION == "1.6"


def test_doi_and_pmcid_come_from_the_records_own_id_list():
    row = parse_pubmed_article(fromstring(RECORD), SF)
    assert row["doi"] == "10.1007/s00059-025-05325-x"
    assert row["pmcid"] == "PMC123"


def test_retraction_notice_is_not_retracted():
    notice = RECORD.replace("Retracted Publication", "Retraction of Publication").replace(
        'RefType="RetractionIn"', 'RefType="RetractionOf"'
    )
    assert parse_pubmed_article(fromstring(notice), SF)["is_retracted"] is False
    assert parse_pubmed_article(fromstring(RECORD), SF)["is_retracted"] is True


def test_retraction_in_alone_marks_retracted():
    only_link = RECORD.replace(
        '<PublicationType UI="D016441">Retracted Publication</PublicationType>', ""
    )
    assert parse_pubmed_article(fromstring(only_link), SF)["is_retracted"] is True


def test_record_returns_row_edges_counters():
    row, edges, counters = parse_pubmed_record(fromstring(RECORD), SF)
    assert row["pmid"] == "40571748"
    assert len(edges["mesh"]) == 2
    assert counters["orcid_dropped"] == 1
    assert row["mesh"] == ["Humans", "Diabetes Mellitus"]  # article column unchanged
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run --no-sync pytest -q tests/data/test_extract_pubmed_record.py`
Expected: FAIL (`ImportError: cannot import name 'parse_pubmed_record'`).

- [ ] **Step 3: Implement.** In `extract_pubmed.py`:
  1. Rename the body of `parse_pubmed_article` to `parse_pubmed_record`.
  2. Delete the `# PubmedData ArticleIds` loop (the `for c in art: … c.iter()` block) and use
     instead:

```python
    ids = own_article_ids(art)
    doi = ids.get("doi", "")
    pmc = ids.get("pmc", "")
    pmcid = pmc if not pmc or pmc.upper().startswith("PMC") else f"PMC{pmc}"
    if not pmid:
        pmid = ids.get("pubmed", "")
```

  3. In the `PublicationTypeList` branch, drop the `if "retract" in val.lower(): is_retracted = True`
     lines and the `is_retracted = False` initialiser. After the ids block, add:

```python
    edges, counters = extract_relations(art, pmid, source_file)
    is_retracted = retracted(pub_types, edges["comments"])
```

  4. End with `return finalize_row(row), edges, counters`, and add:

```python
def parse_pubmed_article(art, source_file: str) -> dict[str, Any]:
    return parse_pubmed_record(art, source_file)[0]


def iter_records_from_file(path: Path, *, source_file: str):
    """Like ``iter_rows_from_file`` but yields ``(row, edges, counters)``."""
    opener = open_gz if path.name.endswith(".gz") else open
    with opener(path, "rb") as f:
        it = _safe_iterparse(f, events=("start", "end"))
        _, root = next(it)
        for event, elem in it:
            if event != "end" or _local(elem.tag) != "PubmedArticle":
                continue
            try:
                yield parse_pubmed_record(elem, source_file)
            finally:
                elem.clear()
                root.clear()
```

  5. Rewrite `iter_rows_from_file` as `for rec in iter_records_from_file(path, source_file=source_file or path.name): yield rec[0]`.
  6. Import `own_article_ids`, `extract_relations` and `retracted` from
     `episteme.data.pubmed.relations`.
  7. In `article_schema.py`, set `SCHEMA_VERSION = "1.6"` and add this changelog line:

```python
#   1.6  (2026-10-14): pubmed doi/pmcid read only from the record's own
#        PubmedData/ArticleIdList (1.5 and earlier could take a cited
#        reference's DOI); is_retracted only for "Retracted Publication" or a
#        RetractionIn link; pubmed writes graph relation files. Row shape unchanged.
```

- [ ] **Step 4: Run** `uv run --no-sync pytest -q tests/data/test_extract_pubmed_record.py tests/data/test_extract_pubmed.py tests/data/test_article_schema.py`.
  Expected: all pass. If an existing `test_extract_pubmed.py` assertion relied on the old
  "retract"-substring rule, update it to the spec's rule and note it in the commit message.

- [ ] **Step 5: Commit**: `fix(pubmed): own-id DOI/PMCID and the retraction rule (schema 1.6)`.

---

### Task 4: Wire graph files into the PubMed extract

**Files:**
- Modify: `src/episteme/data/pubmed/extract_pubmed.py` (`process_one`)
- Test: `tests/data/test_extract_pubmed_graph.py`

**Interfaces:**
- Consumes: `iter_records_from_file`; `GraphFileWriter`; `marker_graph_info`; `graph_file_path`.
- Produces: a success marker whose `stats` includes `graph_relations`, `graph_rows`,
  `graph_sha256`, `orcid_dropped` and `edges_skipped_no_pmid`. Graph files are written before
  the articles shard; the marker is written last.

- [ ] **Step 1: Write the failing tests** (reuse `_pubmed_xml_gz` from `tests/data/test_extract_pubmed.py` by import)

```python
"""extract_pubmed writes graph files with the articles shard; marker last. No DB."""

from __future__ import annotations

import pyarrow.parquet as pq
import pytest

from episteme.data import graph_files as gf
from episteme.data.checkpoint_markers import is_success
from episteme.data.pubmed import extract_pubmed as ep

from .test_extract_pubmed import _pubmed_xml_gz


def _raw(tmp_path):
    raw = tmp_path / "raw"
    (raw / "baseline").mkdir(parents=True)
    _pubmed_xml_gz(raw / "baseline" / "pubmed26n0001.xml.gz", pmid="1", title="T", token="a")
    return raw


def test_graph_files_and_marker(tmp_path):
    raw, out = _raw(tmp_path), tmp_path / "out"
    res = ep.extract_pubmed(raw, out, workers=1)
    assert res["ok"] == 1
    key = "baseline__pubmed26n0001.xml.gz"
    info = gf.marker_graph_info(out, "pubmed", key)
    assert info["graph_relations"] == list(gf.RELATIONS)
    for rel in gf.RELATIONS:
        assert gf.graph_file_path(out, "pubmed", rel, key).is_file()


def test_crash_before_marker_redoes_file(tmp_path, monkeypatch):
    raw, out = _raw(tmp_path), tmp_path / "out"
    key = "baseline__pubmed26n0001.xml.gz"

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ep, "write_rows", boom)
    ep.extract_pubmed(raw, out, workers=1)
    assert not is_success(out, "pubmed", key)
    assert not list((out / "staging").rglob("*.tmp"))
    monkeypatch.undo()
    assert ep.extract_pubmed(raw, out, workers=1)["ok"] == 1
    assert gf.marker_graph_info(out, "pubmed", key) is not None


def test_same_basename_in_two_dirs_never_collides(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    for sub, pmid in (("baseline", "1"), ("updates", "2")):
        (raw / sub).mkdir(parents=True)
        _pubmed_xml_gz(raw / sub / "pubmed26n0001.xml.gz", pmid=pmid, title="T", token=sub)
    ep.extract_pubmed(raw, out, workers=1)
    a = gf.graph_file_path(out, "pubmed", "authors", "baseline__pubmed26n0001.xml.gz")
    b = gf.graph_file_path(out, "pubmed", "authors", "updates__pubmed26n0001.xml.gz")
    assert a != b and a.is_file() and b.is_file()


@pytest.mark.parametrize("rel", gf.RELATIONS)
def test_one_and_three_workers_write_identical_graph_files(tmp_path, rel):
    raw = tmp_path / "raw"
    (raw / "baseline").mkdir(parents=True)
    for i in range(1, 5):
        _pubmed_xml_gz(
            raw / "baseline" / f"pubmed26n000{i}.xml.gz", pmid=str(i), title="T", token=str(i)
        )
    ep.extract_pubmed(raw, tmp_path / "w1", workers=1)
    ep.extract_pubmed(raw, tmp_path / "w3", workers=3)
    for i in range(1, 5):
        key = f"baseline__pubmed26n000{i}.xml.gz"
        t1 = pq.read_table(gf.graph_file_path(tmp_path / "w1", "pubmed", rel, key))
        t3 = pq.read_table(gf.graph_file_path(tmp_path / "w3", "pubmed", rel, key))
        assert t1.equals(t3)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run --no-sync pytest -q tests/data/test_extract_pubmed_graph.py`
Expected: FAIL (`marker_graph_info` returns `None`; there are no graph files).

- [ ] **Step 3: Implement.** Replace the body of `process_one`'s `try:` block with:

```python
    rows: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    try:
        with GraphFileWriter(processed_dir, SOURCE, basename) as gw:
            for row, edges, rec_counters in iter_records_from_file(path, source_file=basename):
                status_counts[str(row.get("extract_status"))] += 1
                rows.append(row)
                counters.update(rec_counters)
                for rel_name, rel_rows in edges.items():
                    if rel_rows:
                        gw.add(rel_name, rel_rows)
            graph_info = gw.close()

        write_info = write_rows(
            rows, processed_dir, source=SOURCE, source_file=basename, prefer_parquet=True
        )
        elapsed = round(time.time() - t0, 4)
        stats = {
            "n_rows": len(rows),
            "extract_status_counts": dict(status_counts),
            "elapsed_sec": elapsed,
            "write": write_info,
            **graph_info,
            "orcid_dropped": counters["orcid_dropped"],
            "edges_skipped_no_pmid": counters["edges_skipped_no_pmid"],
        }
        mark_success(processed_dir, SOURCE, basename, stats=stats)
```

Keep the existing audit, return and `except` blocks. Add the imports
`from episteme.data.graph_files import GraphFileWriter`, and `iter_records_from_file` (same
module). `write_rows` must stay a module-level name, so the crash test can monkeypatch it.

- [ ] **Step 4: Run** `uv run --no-sync pytest -q tests/data/test_extract_pubmed_graph.py tests/data/test_extract_pubmed.py`.
  Expected: all pass.

- [ ] **Step 5: Commit**: `feat(pubmed): write graph relation files with each shard; marker records them`.

---

### Task 5: Migration 0006 (graph tables, facet rules, lineage)

**Files:**
- Create: `src/episteme/data/db/migrations/0006_pubmed_graph.sql`
- Create: `tests/data/_graph_schema.py` (shared pg setup helper)
- Test: `tests/data/test_migration_0006.py`

**Interfaces:**
- Produces: tables `article_comments`, `article_chemicals`, `article_keywords`,
  `article_authors`, `article_facets` (each hash(pmid), 8 partitions `_h0`..`_h7`),
  `mesh_tree`, `mesh_facet_rule` and `_graph_lineage`, plus new columns on `article_mesh` and
  `article_cites`. Index names, used by Task 6:
  - `article_mesh_pmid_idx`, `article_mesh_descriptor_ui_idx`
  - `article_cites_src_pmid_idx`, `article_cites_dst_pmid_idx`
  - `article_comments_pmid_idx`, `article_comments_ref_pmid_idx`
  - `article_chemicals_pmid_idx`, `article_chemicals_substance_ui_idx`
  - `article_keywords_pmid_idx`, `article_keywords_keyword_idx`
  - `article_authors_pmid_idx`, `article_authors_orcid_idx`
  - `article_facets_pmid_idx`, `article_facets_facet_descriptor_idx`
  - the permanent `<table>_source_file_idx` on each of the 7 tables
  - `mesh_tree_tree_number_idx`, `mesh_tree_descriptor_ui_idx`
- `tests/data/_graph_schema.py`: `setup_graph_schema(conn)` drops and recreates the `episteme`
  schema, applying `extensions.sql`, `schema.sql`, then migrations 0002–0006 in order, and
  commits.

- [ ] **Step 1: Write the migration**

```sql
-- Migration 0006 -- PubMed graph relations, MeSH facets, graph lineage. Idempotent.
-- Apply as episteme_sys_admin (owner). One transaction (see 0005).
-- schema.sql's ALTER DEFAULT PRIVILEGES grants episteme_app DML on new tables,
-- which the pipeline needs. MCPg run_ddl refuses GRANT/REVOKE, so when applying
-- through MCPg the operator runs this file's REVOKE line once as episteme_sys_admin.

BEGIN;

ALTER TABLE episteme.article_mesh  ADD COLUMN IF NOT EXISTS qualifier_uis text[];
ALTER TABLE episteme.article_cites ADD COLUMN IF NOT EXISTS dst_doi  text;
ALTER TABLE episteme.article_cites ADD COLUMN IF NOT EXISTS ref_text text;
ALTER TABLE episteme.article_cites ALTER COLUMN dst_pmid DROP NOT NULL;

CREATE TABLE IF NOT EXISTS episteme.article_comments (
    pmid text NOT NULL, ref_type text, ref_pmid text, ref_text text, source_file text
) PARTITION BY HASH (pmid);
CREATE TABLE IF NOT EXISTS episteme.article_chemicals (
    pmid text NOT NULL, substance_ui text, name text, registry_number text, source_file text
) PARTITION BY HASH (pmid);
CREATE TABLE IF NOT EXISTS episteme.article_keywords (
    pmid text NOT NULL, keyword text, owner text, major_topic boolean, source_file text
) PARTITION BY HASH (pmid);
CREATE TABLE IF NOT EXISTS episteme.article_authors (
    pmid text NOT NULL, position int NOT NULL, last_name text, fore_name text, initials text,
    collective_name text, orcid text, affiliations text[], source_file text
) PARTITION BY HASH (pmid);
CREATE TABLE IF NOT EXISTS episteme.article_facets (
    pmid text NOT NULL, facet text NOT NULL, descriptor_ui text NOT NULL,
    rule_version int NOT NULL, source_file text
) PARTITION BY HASH (pmid);

DO $$
DECLARE t text; i int;
BEGIN
  FOREACH t IN ARRAY ARRAY['article_comments','article_chemicals','article_keywords',
                           'article_authors','article_facets'] LOOP
    FOR i IN 0..7 LOOP
      EXECUTE format(
        'CREATE TABLE IF NOT EXISTS episteme.%I PARTITION OF episteme.%I '
        'FOR VALUES WITH (MODULUS 8, REMAINDER %s)', t || '_h' || i, t, i);
    END LOOP;
  END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS episteme.mesh_tree (
    descriptor_ui text NOT NULL, tree_number text NOT NULL, source_file text
);
CREATE TABLE IF NOT EXISTS episteme.mesh_facet_rule (
    facet text NOT NULL, kind text NOT NULL CHECK (kind IN ('tree_prefix', 'descriptor')),
    value text NOT NULL, rule_version int NOT NULL,
    PRIMARY KEY (facet, kind, value, rule_version)
);
CREATE TABLE IF NOT EXISTS episteme._graph_lineage (
    source text NOT NULL, source_file text NOT NULL, graph_sha256 text NOT NULL,
    rows jsonb NOT NULL, run_id text, graphed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, source_file)
);

-- Secondary indexes (bulk group "pubmed_graph" drops/rebuilds these).
CREATE INDEX IF NOT EXISTS article_mesh_pmid_idx            ON episteme.article_mesh (pmid);
CREATE INDEX IF NOT EXISTS article_mesh_descriptor_ui_idx   ON episteme.article_mesh (descriptor_ui);
CREATE INDEX IF NOT EXISTS article_cites_src_pmid_idx       ON episteme.article_cites (src_pmid);
CREATE INDEX IF NOT EXISTS article_cites_dst_pmid_idx       ON episteme.article_cites (dst_pmid);
CREATE INDEX IF NOT EXISTS article_comments_pmid_idx        ON episteme.article_comments (pmid);
CREATE INDEX IF NOT EXISTS article_comments_ref_pmid_idx    ON episteme.article_comments (ref_pmid);
CREATE INDEX IF NOT EXISTS article_chemicals_pmid_idx       ON episteme.article_chemicals (pmid);
CREATE INDEX IF NOT EXISTS article_chemicals_substance_ui_idx ON episteme.article_chemicals (substance_ui);
CREATE INDEX IF NOT EXISTS article_keywords_pmid_idx        ON episteme.article_keywords (pmid);
CREATE INDEX IF NOT EXISTS article_keywords_keyword_idx     ON episteme.article_keywords (lower(keyword));
CREATE INDEX IF NOT EXISTS article_authors_pmid_idx         ON episteme.article_authors (pmid);
CREATE INDEX IF NOT EXISTS article_authors_orcid_idx        ON episteme.article_authors (orcid) WHERE orcid IS NOT NULL;
CREATE INDEX IF NOT EXISTS article_facets_pmid_idx          ON episteme.article_facets (pmid);
CREATE INDEX IF NOT EXISTS article_facets_facet_descriptor_idx ON episteme.article_facets (facet, descriptor_ui);
-- Permanent: per-file deletes; never dropped by bulk mode.
CREATE INDEX IF NOT EXISTS article_mesh_source_file_idx      ON episteme.article_mesh (source_file);
CREATE INDEX IF NOT EXISTS article_cites_source_file_idx     ON episteme.article_cites (source_file);
CREATE INDEX IF NOT EXISTS article_comments_source_file_idx  ON episteme.article_comments (source_file);
CREATE INDEX IF NOT EXISTS article_chemicals_source_file_idx ON episteme.article_chemicals (source_file);
CREATE INDEX IF NOT EXISTS article_keywords_source_file_idx  ON episteme.article_keywords (source_file);
CREATE INDEX IF NOT EXISTS article_authors_source_file_idx   ON episteme.article_authors (source_file);
CREATE INDEX IF NOT EXISTS article_facets_source_file_idx    ON episteme.article_facets (source_file);
CREATE INDEX IF NOT EXISTS mesh_tree_tree_number_idx  ON episteme.mesh_tree (tree_number text_pattern_ops);
CREATE INDEX IF NOT EXISTS mesh_tree_descriptor_ui_idx ON episteme.mesh_tree (descriptor_ui);

-- Facet rules, version 1 (UIs checked against MeSH desc2026). Male/Female have
-- no tree numbers (check tags), so they are 'descriptor' rules.
INSERT INTO episteme.mesh_facet_rule (facet, kind, value, rule_version) VALUES
  ('condition', 'tree_prefix', 'C', 1),
  ('condition', 'tree_prefix', 'F03', 1),
  ('test', 'tree_prefix', 'E01', 1),
  ('test', 'tree_prefix', 'E05', 1),
  ('drug', 'tree_prefix', 'D', 1),
  ('demographic', 'descriptor', 'D006801', 1),
  ('demographic', 'descriptor', 'D000818', 1),
  ('demographic', 'descriptor', 'D008297', 1),
  ('demographic', 'descriptor', 'D005260', 1),
  ('demographic', 'tree_prefix', 'M01.060', 1),
  ('demographic', 'tree_prefix', 'M01.390', 1),
  ('demographic', 'tree_prefix', 'M01.975', 1),
  ('special_population', 'descriptor', 'D011247', 1),
  ('special_population', 'descriptor', 'D037841', 1),
  ('special_population', 'descriptor', 'D007231', 1),
  ('special_population', 'descriptor', 'D007234', 1),
  ('special_population', 'descriptor', 'D000369', 1),
  ('special_population', 'tree_prefix', 'M01.066', 1),
  ('special_population', 'tree_prefix', 'M01.150', 1),
  ('special_population', 'tree_prefix', 'M01.169', 1),
  ('special_population', 'tree_prefix', 'M01.189', 1),
  ('special_population', 'tree_prefix', 'M01.270', 1),
  ('special_population', 'tree_prefix', 'M01.325', 1),
  ('special_population', 'tree_prefix', 'M01.385', 1),
  ('special_population', 'tree_prefix', 'M01.686', 1),
  ('special_population', 'tree_prefix', 'M01.729', 1),
  ('special_population', 'tree_prefix', 'M01.755', 1),
  ('special_population', 'tree_prefix', 'M01.873', 1),
  ('special_population', 'tree_prefix', 'M01.920', 1),
  ('special_population', 'tree_prefix', 'M01.925', 1),
  ('special_population', 'tree_prefix', 'M01.930', 1)
ON CONFLICT DO NOTHING;

-- The pipeline writes as episteme_app: data tables and _graph_lineage keep the
-- default grants (as articles and _lineage do). Only facet rules are read-only.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON episteme.mesh_facet_rule FROM episteme_app;

COMMIT;
```

Ruling (made while planning, also corrected in the spec): `connection()` connects as
`episteme_app`, and every pipeline write uses it, so no data table may lose `episteme_app` DML.

- [ ] **Step 2: Write the shared helper and the failing pg test**

`tests/data/_graph_schema.py`:

```python
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIG = REPO / "src/episteme/data/db/migrations"


def setup_graph_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute((REPO / "src/episteme/data/db/extensions.sql").read_text(encoding="utf-8"))
        cur.execute((REPO / "src/episteme/data/db/schema.sql").read_text(encoding="utf-8"))
        for name in sorted(p.name for p in MIG.glob("000[2-6]_*.sql")):
            cur.execute((MIG / name).read_text(encoding="utf-8"))
    conn.commit()
```

`tests/data/test_migration_0006.py`:

```python
import pytest

from ._graph_schema import MIG, setup_graph_schema

pytestmark = pytest.mark.pg


def test_0006_tables_indexes_rules_and_rights(pg_conn):
    setup_graph_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute((MIG / "0006_pubmed_graph.sql").read_text(encoding="utf-8"))  # twice
        cur.execute(
            "SELECT count(*) FROM pg_inherits WHERE inhparent = 'episteme.article_authors'::regclass"
        )
        assert cur.fetchone()[0] == 8
        cur.execute("SELECT count(*) FROM episteme.mesh_facet_rule WHERE rule_version = 1")
        assert cur.fetchone()[0] == 31
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='episteme' "
            "AND indexname LIKE '%%_source_file_idx' AND tablename IN ("
            "'article_mesh','article_cites','article_comments','article_chemicals',"
            "'article_keywords','article_authors','article_facets')"
        )
        assert len(cur.fetchall()) == 7
        cur.execute(
            "SELECT has_table_privilege('episteme_app','episteme.article_authors','INSERT'), "
            "has_table_privilege('episteme_app','episteme._graph_lineage','INSERT'), "
            "has_table_privilege('episteme_app','episteme.mesh_facet_rule','SELECT'), "
            "has_table_privilege('episteme_app','episteme.mesh_facet_rule','INSERT')"
        )
        assert cur.fetchone() == (True, True, True, False)
        cur.execute(
            "INSERT INTO episteme.article_cites (src_pmid, dst_pmid, ref_text) "
            "VALUES ('1', NULL, 'x')"
        )
    pg_conn.rollback()
```

- [ ] **Step 3: Run it** with `.env` sourced and `TEST_PG_DSN` set:
  `uv run --no-sync pytest -q -m pg tests/data/test_migration_0006.py`. Expected: PASS.
  Confirm it FAILS if you comment out the `REVOKE` line, then restore it.
- [ ] **Step 4: Commit**: `feat(db): migration 0006 for the pubmed graph tables, facet rules and graph lineage`.

---

### Task 6: Bulk index groups and `graph --bulk`

**Files:**
- Modify: `src/episteme/data/db/bulk_indexes.py`
- Modify: `scripts/data/db/bulk_load_indexes.sh` (pass `--group` through)
- Modify: `scripts/data/run_pipeline.sh` (allow `--bulk` with `graph`; group by stage)
- Test: `tests/data/test_bulk_indexes_unit.py` (extend), `tests/data/test_bulk_indexes.py` (extend, pg), `tests/test_run_pipeline_bulk.py` (extend)

**Interfaces:**
- Produces:
  - `INDEX_GROUPS: dict[str, tuple[tuple[str, str], ...]]` maps a group to (table, index)
    pairs:
    - `"articles"` holds the 6 existing `episteme.articles` indexes;
    - `"pubmed_graph"` holds the 14 Task 5 secondary indexes on their tables.
  - `COVERED_INDEXES` stays as an alias of the `articles` index names.
  - `drop(conn, *, run_id, group="articles")`.
  - `rebuild(conn, *, run_id)` rebuilds every pending row, whatever the group, then ANALYZEs
    each table it touched.
  - `rebuild_sql(definition, name)` accepts `ON ONLY episteme.<any_table>`.
  - CLI: `python -m episteme.data.db.bulk_indexes drop|rebuild|status [--group articles|pubmed_graph]`.
  - `bulk_load_indexes.sh <action> [--group G]`.
  - `run_pipeline.sh`: `--bulk` is valid with `load` (group `articles`) and `graph` (group
    `pubmed_graph`); any other stage still exits with the existing error text.
  - Audit objects become `"<table> <index>"` per index on rebuild, and
    `"<group> <names>"` on drop.

- [ ] **Step 1: Write the failing unit tests** (append to `tests/data/test_bulk_indexes_unit.py`):

```python
def test_rebuild_sql_accepts_any_episteme_table():
    from episteme.data.db.bulk_indexes import rebuild_sql

    d = "CREATE INDEX article_mesh_pmid_idx ON ONLY episteme.article_mesh USING btree (pmid)"
    assert rebuild_sql(d, "article_mesh_pmid_idx") == (
        "CREATE INDEX article_mesh_pmid_idx ON episteme.article_mesh USING btree (pmid)"
    )


def test_groups_cover_the_spec_indexes_and_never_source_file():
    from episteme.data.db.bulk_indexes import COVERED_INDEXES, INDEX_GROUPS

    assert tuple(n for _, n in INDEX_GROUPS["articles"]) == COVERED_INDEXES
    names = [n for _, n in INDEX_GROUPS["pubmed_graph"]]
    assert len(names) == 14
    assert not any(n.endswith("_source_file_idx") for n in names)
```

and to `tests/test_run_pipeline_bulk.py` a case that `run_pipeline.sh pubmed graph --bulk` gets
past argument validation (use that file's existing fake-wrapper pattern), while
`pubmed extract --bulk` still fails with `--bulk is only valid with the load or graph stage`.

- [ ] **Step 2: Run them and confirm they fail.**

- [ ] **Step 3: Implement.**
  - Generalise `_PARENT_DEF` to `r"^CREATE (?:UNIQUE )?INDEX (\S+) ON ONLY episteme\.(\w+) USING "`.
    A non-partitioned table's definition (`mesh_tree` is not in a group, so only partitioned
    parents occur) never reaches this function.
  - In `drop`, select definitions with
    `WHERE c.relname = ANY(%s) AND i.indrelid = ANY(%s::regclass[])` over the group's pairs.
  - In `rebuild`, collect the distinct tables from the saved definitions and run
    `ANALYZE episteme.<table>` for each, using `sql.Identifier`.
  - In `run_pipeline.sh`:
    - change the guard to `[ "$STAGE" != "load" ] && [ "$STAGE" != "graph" ]`, with the message
      `--bulk is only valid with the load or graph stage`;
    - set `BULK_GROUP=articles` for `load` and `pubmed_graph` for `graph`;
    - pass `--group "$BULK_GROUP"` to every `bulk_load_indexes.sh drop` call;
    - make the log lines name the group.
  - Update the `bulk_load_indexes.sh` usage text.

- [ ] **Step 4: Run** the unit tests, `tests/test_run_pipeline_bulk.py`, and (pg)
  `tests/data/test_bulk_indexes.py`, plus one new pg test. That test applies 0006 (via
  `setup_graph_schema`), runs `drop(group="pubmed_graph")`, asserts the 14 indexes are gone and
  the `_source_file_idx` ones remain, then runs `rebuild` and asserts all 14 are valid and
  `_bulk_index_state` is empty. Expected: pass.
- [ ] **Step 5: Commit**: `feat(db): bulk index groups; --bulk on graph uses the pubmed_graph group`.

---

### Task 7: `mesh graph` fills `mesh_tree`

**Files:**
- Modify: `src/episteme/data/graph_builder.py` (the `if source == "mesh":` phase, about lines 355–410)
- Test: `tests/data/test_graph_builder.py` (extend; pg)

**Interfaces:**
- Consumes: `_parse_mesh_descriptors(xml_path) -> dict[str, list[str]]` (UI to tree numbers),
  which already exists.
- Produces: `episteme.mesh_tree` rows `(descriptor_ui, tree_number, source_file)`. Each run
  deletes and re-inserts by `source_file` in the same transaction and audit row as
  `mesh_hierarchy`. The return dict gains `"mesh_tree": int`.

- [ ] **Step 1: Write the failing pg test.** Use the existing mesh fixture in
  `tests/data/test_graph_builder.py`, calling `setup_graph_schema` instead of that file's
  current schema setup for this one test. Run `build(conn, source="mesh", …)` twice, then
  assert:
  - `SELECT count(*) FROM episteme.mesh_tree` equals the fixture's total number of tree numbers,
    after both runs (idempotent);
  - `res["mesh_tree"]` equals that count.
- [ ] **Step 2: Run it and confirm it fails** (`KeyError: 'mesh_tree'`, or a zero count).
- [ ] **Step 3: Implement.** After the `mesh_hierarchy` delete and insert, add:

```python
            tree_rows = [(ui, t, src_file) for ui, trees in descriptors.items() for t in trees]
            with conn.cursor() as cur:
                cur.execute("DELETE FROM episteme.mesh_tree WHERE source_file = %s", (src_file,))
                with cur.copy(
                    "COPY episteme.mesh_tree (descriptor_ui, tree_number, source_file) FROM STDIN"
                ) as cp:
                    for r in tree_rows:
                        cp.write_row(r)
            total_mesh_tree += len(tree_rows)
```

  Initialise `total_mesh_tree = 0` beside `total_mesh_hierarchy`, change the audit
  `rows_affected` to `len(edges) + len(tree_rows)`, add `"mesh_tree": total_mesh_tree` to the
  return dict, and add `mesh_tree=` to `main`'s printed summary.
- [ ] **Step 4: Run** the graph_builder pg tests. Expected: pass.
- [ ] **Step 5: Commit**: `feat(graph): mesh graph fills mesh_tree from the descriptor tree numbers`.

---

### Task 8: PubMed graph load (per file: delete-if-graphed, COPY, facets, audit, lineage)

**Files:**
- Create: `src/episteme/data/graph_load.py`
- Modify: `src/episteme/data/graph_builder.py`:
  - `build` routes `source == "pubmed"` to `graph_load.build_pubmed_graph`; the old pubmed
    `article_parts` pass is removed for pubmed only;
  - `main` gains `--processed-dir`, `--force` and `--reason`.
- Modify: `scripts/data/pubmed/graph_pubmed.sh` (pass `--processed-dir "${EPISTEME_PROCESSED_ROOT:-./02_processed}"` and `"$@"`)
- Test: `tests/data/test_graph_load.py` (pg), `tests/data/test_graph_load_unit.py` (no DB)

**Interfaces:**
- Consumes:
  - `graph_files.RELATIONS`, `RELATION_COLUMNS`, `graph_file_path` and `marker_graph_info`;
  - `episteme.data.checkpoint_markers.ops_root`;
  - `audit_trail.record`;
  - the Task 5 tables.
- Produces:
  - `class GraphLoadError(RuntimeError)`.
  - `TABLE_FOR: dict[str, str]` maps each relation to its table:
    - `mesh` → `episteme.article_mesh`
    - `cites` → `episteme.article_cites`
    - `comments` → `episteme.article_comments`
    - `chemicals` → `episteme.article_chemicals`
    - `keywords` → `episteme.article_keywords`
    - `authors` → `episteme.article_authors`
  - `FACET_SQL: str`, the per-file facet insert (below). Task 9 reuses it.
  - `graphed_files(processed_dir, source) -> list[str]`: the `source_file`s whose success marker
    has graph info, sorted.
  - `load_one(conn, *, source, source_file, processed_dir, run_id, force=False, reason=None) -> dict`
    returns `{"source_file", "skipped": bool, "rows": {rel: n}, "facets": int}`. It **commits**
    on success and rolls back and re-raises on error.
  - `build_pubmed_graph(conn, *, processed_dir, run_id, force=False, reason=None) -> dict`
    returns `{"files": n, "loaded": n, "skipped": n, "failed": [source_file, ...], "rows": {rel: total}, "facets": total}`.
  - `graph_builder.main` exits 1 when `failed` is non-empty.

Facet SQL (newest rule version; descriptor rules match `descriptor_ui`; tree-prefix rules match
`mesh_tree`, where a one-letter value matches any tree number starting with it and a longer value
matches itself or `value.%`). Drug facets from chemicals take only substances whose UI has a
tree-D number:

```python
FACET_SQL = """
WITH rv AS (SELECT max(rule_version) v FROM episteme.mesh_facet_rule),
r AS (SELECT r.* FROM episteme.mesh_facet_rule r, rv WHERE r.rule_version = rv.v),
hits AS (
  SELECT m.pmid, r.facet, m.descriptor_ui, r.rule_version
    FROM episteme.article_mesh m JOIN r ON r.kind = 'descriptor' AND r.value = m.descriptor_ui
   WHERE m.source_file = %(sf)s
  UNION
  SELECT m.pmid, r.facet, m.descriptor_ui, r.rule_version
    FROM episteme.article_mesh m
    JOIN episteme.mesh_tree t ON t.descriptor_ui = m.descriptor_ui
    JOIN r ON r.kind = 'tree_prefix'
          AND (t.tree_number = r.value OR t.tree_number LIKE r.value || '.%%'
               OR (length(r.value) = 1 AND t.tree_number LIKE r.value || '%%'))
   WHERE m.source_file = %(sf)s
  UNION
  SELECT c.pmid, 'drug', c.substance_ui, rv.v
    FROM episteme.article_chemicals c, rv
   WHERE c.source_file = %(sf)s
     AND EXISTS (SELECT 1 FROM episteme.mesh_tree t
                  WHERE t.descriptor_ui = c.substance_ui AND t.tree_number LIKE 'D%%')
)
INSERT INTO episteme.article_facets (pmid, facet, descriptor_ui, rule_version, source_file)
SELECT pmid, facet, descriptor_ui, rule_version, %(sf)s FROM hits
"""
```

`load_one` algorithm:
1. Read `info = marker_graph_info(...)`. If it is `None`, raise
   `GraphLoadError(f"{source_file}: no graph files in its marker; re-extract with schema >= 1.6")`.
2. Check that every relation file exists; if one is missing, raise
   `GraphLoadError(f"{source_file}: graph file missing for relation {rel!r}")`.
3. `SELECT graph_sha256 FROM episteme._graph_lineage WHERE source=%s AND source_file=%s`. If it
   equals `info["graph_sha256"]` and `force` is false, return `skipped=True`.
4. If a lineage row exists, delete this `source_file`'s rows from all six tables and from
   `article_facets`.
5. COPY each relation: `pq.ParquetFile(path).iter_batches(batch_size=50_000)`, column-wise
   `to_pylist`, `cp.write_row(tuple)`, columns in `RELATION_COLUMNS` order. Assert the copied
   count equals `info["graph_rows"][rel]` when that key is present; raise `GraphLoadError` on a
   mismatch.
6. Run `FACET_SQL` with `{"sf": source_file}`.
7. Write the audit:
   - if `force` was used, `audit_trail.record("force_override", conn=conn, object=f"{source} graph {source_file}", reason=reason, run_id=run_id)`;
   - always `audit_trail.record("graph_commit", conn=conn, object=f"{source} {source_file}", input_content_hash=info["graph_sha256"], rows_affected=sum(rows.values()) + facets, new_value={"rows": rows, "facets": facets}, run_id=run_id)`.
8. Upsert `_graph_lineage` (`ON CONFLICT (source, source_file) DO UPDATE`), then `conn.commit()`.

`build_pubmed_graph`:
- Refuse with `GraphLoadError("mesh_tree is empty; run `run_pipeline.sh mesh graph` first")`
  when `SELECT 1 FROM episteme.mesh_tree LIMIT 1` returns nothing.
- `force` without `reason` raises `GraphLoadError("--force requires --reason")`.
- Loop over `graphed_files`, plus any success-marked file with no graph info, which is reported
  as failed with the re-extract message. A failure on one file is caught, rolled back, appended
  to `failed` and printed to stderr; the loop continues.

- [ ] **Step 1: Write the failing tests.**

  `tests/data/test_graph_load_unit.py` (no DB):
  - a success marker without graph info means `graphed_files` excludes the file;
  - `FACET_SQL` contains `max(rule_version)` and `'drug'`.

  `tests/data/test_graph_load.py` (pg). Fixture: `setup_graph_schema(pg_conn)`; insert
  `mesh_tree` rows:
  - `('D003920','C18.452.394.750')`
  - `('D003920','C19.246')`
  - `('D007328','D06.472.699.587.200.500.500')`
  - `('D007328','D12.644.548.586.200.500')`
  - `('D000369','M01.060.116.100.080')`

  Write graph files for one source file with `GraphFileWriter`, using rows like Task 2's
  (`D006801` Humans, `D003920` Diabetes, `D000369` Aged 80+, chemicals `D007328` and
  `C000123`), and `mark_success` with the writer's info. Tests:
  - `test_first_load_copies_every_relation_and_facets`:
    - the row counts equal the writer's `graph_rows`;
    - the facets are exactly {(pmid,'demographic','D006801'), (pmid,'condition','D003920'),
      (pmid,'demographic','D000369'), (pmid,'special_population','D000369'),
      (pmid,'drug','D007328')};
    - no facet row for `C000123` (Review Focus 5);
    - one `_graph_lineage` row, and one `graph_commit` audit row.
  - `test_unchanged_hash_is_skipped`: a second `load_one` returns `skipped=True`.
  - `test_regraph_replaces_not_duplicates` (Review Focus 2): rewrite the files with one fewer
    mesh row, re-mark, `load_one` again, and assert the counts equal the new files' counts.
  - `test_force_requires_reason_and_is_audited`.
  - `test_missing_relation_file_fails_that_file_only` (Review Focus 3): delete one relation file
    of file A; `build_pubmed_graph` loads file B, `failed == [A]`, and A has no rows.
  - `test_old_marker_is_refused_with_reextract_message`.
  - `test_empty_mesh_tree_refuses_to_start`.

- [ ] **Step 2: Run them and confirm they fail** (`ModuleNotFoundError: episteme.data.graph_load`).
- [ ] **Step 3: Implement `graph_load.py`** to the interfaces and algorithm above. Wire
  `graph_builder.build`: `if source == "pubmed": return graph_load.build_pubmed_graph(conn, processed_dir=processed_dir, run_id=run_id, force=force, reason=reason)`.
  Give `build` the keyword-only parameters `processed_dir=None, force=False, reason=None`.
  `main` passes `--processed-dir` (default `settings.processed_root`), prints the pubmed summary
  and returns 1 when `failed` is non-empty. Connection: the existing `connection()` (`episteme_app`), as for `pmc` and `mesh`.
- [ ] **Step 4: Run** both test files and the existing `tests/data/test_graph_builder.py`
  (pg). Expected: all pass.
- [ ] **Step 5: Commit**: `feat(graph): load pubmed graph files per source file with facets, audit and lineage`.

---

### Task 9: Facet rebuild and status

**Files:**
- Modify: `src/episteme/data/graph_load.py` (add `rebuild_facets`, `facet_status`)
- Modify: `src/episteme/data/graph_builder.py` (`main` subcommand `facets --rebuild|--status`)
- Test: `tests/data/test_graph_load.py` (extend, pg)

**Interfaces:**
- Produces:
  - `facet_status(conn) -> dict` returns
    `{"rule_version": int, "stale_rows": int, "stale_files": int, "files": int}`. A row is stale
    when its `rule_version` is below the newest version.
  - `rebuild_facets(conn, *, run_id, force=False, reason=None) -> dict` returns
    `{"rebuilt": n, "skipped": n}`.
    - It works per distinct `source_file` in `article_mesh` that has a `_graph_lineage` row.
    - A file is skipped when it has facet rows and none is stale, unless `force` is set
      (`force` requires `reason`, audited with `force_override`).
    - Otherwise it deletes that file's `article_facets`, runs `FACET_SQL`, writes
      `graph_commit` with object `facets <source_file>`, and commits per file.
  - CLI: `python -m episteme.data.graph_builder facets --rebuild [--force --reason R]` and
    `… facets --status`. The existing `--source` form stays.
    - Implement with `argparse` subparsers, or a first-positional check, so
      `--source pubmed` keeps working unchanged.
    - `--status` exits 0 when there are no stale rows, 1 when there are.

- [ ] **Step 1: Write the failing pg test.**
  1. After a first load, insert rule version 2, copying version 1 and adding
     `('test','tree_prefix','C19',2)`.
  2. `facet_status` reports `stale_files == 1`.
  3. `rebuild_facets` rebuilds 1 file; the facets now carry `rule_version == 2` and include
     `(pmid,'test','D003920')`.
  4. `facet_status` reports `stale_rows == 0`.
  5. A second `rebuild_facets` skips the file.
- [ ] **Step 2: Run it and confirm it fails.** **Step 3: Implement.** **Step 4: Run it and
  confirm it passes.**
- [ ] **Step 5: Commit**: `feat(graph): restartable facet rebuild and stale-rule status`.

---

### Task 10: Query helpers

**Files:**
- Modify: `src/episteme/data/graph_builder.py` (`neighbours`, `_neighbours_cte`, new helpers)
- Test: `tests/data/test_graph_queries.py` (pg)

**Interfaces:**
- Produces:
  - `neighbours(conn, pmid, hops=1, kind="mesh", ref_type=None)`. The allowed kinds add
    `"cited_by"`, `"comments"`, `"chemical"`, `"keyword"`, `"author"` and `"facet"`. Outputs
    are sorted distinct strings:
    - `cited_by`: `src_pmid` where `dst_pmid = pmid`;
    - `comments`: `f"{ref_type}:{ref_pmid or ''}"`, filtered by `ref_type` when it is given;
    - `chemical`: `substance_ui`;
    - `keyword`: `lower(keyword)`;
    - `author`: `f"{position}:{name}:{orcid or ''}"`, where name is
      `collective_name or trim(fore_name || ' ' || last_name)`;
    - `facet`: `f"{facet}:{descriptor_ui}"`.

    The error message lists every kind.
  - `articles_for_mesh(conn, descriptor_ui, include_descendants=True, major_only=False, limit=1000) -> list[str]`
    returns sorted distinct pmids. With descendants, the UI set is every `descriptor_ui` in
    `mesh_tree` whose tree number equals, or starts with `<tn>.`, for any tree number of the
    given UI; then it looks up `article_mesh` by `descriptor_ui = ANY(set)`.
  - `articles_for_facet(conn, facet, descriptor_ui=None, limit=1000) -> list[str]`.
  - `articles_by_orcid(conn, orcid, limit=1000) -> list[str]` normalises its input with
    `relations.normalize_orcid` first.

- [ ] **Step 1: Write the failing pg tests**:
  - **Fixture:** `setup_graph_schema`; `mesh_tree` holds Diabetes `C19.246` and Diabetes
    Type 2 `D003924` at `C19.246.300`; `article_mesh` has pmid 1 → D003920 and pmid 2 → D003924;
    cites 1 → 2; comments, chemicals, keywords, authors and facets get one row each for pmid 1.
  - **Asserts:**
    - `articles_for_mesh(conn,'D003920') == ['1','2']`, and `== ['1']` with
      `include_descendants=False`;
    - `neighbours(conn,'2',kind='cited_by') == ['1']`;
    - each other new kind returns its formatted value;
    - an unknown kind raises `ValueError` naming `cited_by`;
    - `articles_by_orcid(conn,'https://orcid.org/0000-0002-1825-009X') == ['1']`.
- [ ] **Step 2: Run them and confirm they fail.** **Step 3: Implement** in the existing style:
  `_neighbours_cte` gets one branch per new kind; the helpers are module-level functions. **Step
  4: Run them and confirm they pass**, along with the existing neighbours tests.
- [ ] **Step 5: Commit**: `feat(graph): cited_by/comments/chemical/keyword/author/facet neighbours and search helpers`.

---

### Task 11: PubMed data-quality audit module

**Files:**
- Create: `src/episteme/data/audit/__init__.py` (empty), `src/episteme/data/audit/pubmed_audit.py`
- Test: `tests/data/test_pubmed_audit.py` (no DB)

**Interfaces:**
- Produces:
  - `run_audit(processed_dir: Path, *, raw_dir: Path | None, out_dir: Path, sample_files: int = 5, duckdb_temp: Path | None = None) -> dict`
    writes `pubmed_audit.json` and `pubmed_audit.md` into `out_dir` (default
    `<processed>/_ops/pubmed/audit/`) and returns the report dict.
  - CLI `python -m episteme.data.audit.pubmed_audit [--processed-dir] [--raw-dir] [--out-dir] [--sample-files N]`.
    It reads only `config.get_settings()` for defaults (no `os.environ`).
  - Report keys:
    - everything in the scratchpad audit (`rows`, `shards`, `null_pct`, `extract_status`,
      `year_range`, `year_out_of_range`, `pmid_not_numeric`, `id_mismatch`, `pmcid_bad`,
      `doi_bad`, `dup_ids_across_shards`, `dup_doi_groups`, `text_without_hash`, `retraction`,
      `status_vs_content`);
    - `graph_rows` (rows per relation);
    - `mesh_ui_known_pct` (the share of `article_mesh.descriptor_ui` values present as `mesh:<UI>`
      ids in `staging/mesh/*.parquet`; `None` when there are no mesh shards);
    - `cites_resolved_pct` (the share of non-null `dst_pmid` present among the pubmed shard
      pmids);
    - `orcid_checksum_bad` (ORCIDs failing the ISO 7064 mod 11-2 check digit);
    - `retracted_without_link` and `retraction_in_without_flag`;
    - `doi_probe` = `{"files": n, "records": n, "mismatch": n}`, re-reading `sample_files`
      evenly spaced raw files with `relations.own_article_ids`. Expected `mismatch == 0`.

Base the module on the working scratchpad script (DuckDB, a narrow temp table, spill to
`duckdb_temp`); the controller will paste it into the brief. Add the graph checks as SQL over
`read_parquet('<processed>/staging/pubmed_graph/<rel>/*.parquet')`. ORCID check digit:

```python
def orcid_check_ok(orcid: str) -> bool:
    digits = orcid.replace("-", "")
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    return digits[-1] == ("X" if check == 10 else str(check))
```

- [ ] **Step 1: Write the failing test.**
  1. Build a tiny processed tree with `extract_pubmed` on two fixture files (Task 4's helper).
  2. Run `run_audit(out, raw_dir=raw, out_dir=tmp_path/'audit', sample_files=2)`.
  3. Assert:
     - both report files exist;
     - `report["rows"] == 2`;
     - `report["doi_probe"]["mismatch"] == 0`;
     - `"mesh" in report["graph_rows"]`;
     - `orcid_check_ok("0000-0002-1825-0097")` is true, and false for `0000-0002-1825-0098`.
- [ ] **Step 2: Run it and confirm it fails.** **Step 3: Implement.** **Step 4: Run it and
  confirm it passes.**
- [ ] **Step 5: Commit**: `feat(audit): pubmed data-quality audit over shards and graph files`.

---

### Task 12: Documentation and drift log

**Files:**
- Modify: `docs/09-extraction-contract.md` (schema 1.6, the graph-file layout, relation row
  shapes copied from `RELATION_COLUMNS`, the DOI and retraction rules)
- Modify: `docs/10-data-sources-runbook.md`:
  - §2.3: migration 0006, including the operator REVOKE/GRANT lines;
  - §4.3: `--bulk` on `load` and `graph`;
  - §4.6: index groups;
  - §5.1 pubmed: the `mesh graph` then `pubmed graph` order, `facets --rebuild` and
    `--status`, the audit command, and the rollout steps from the spec.
- Modify: `docs/12-source-inventory.md` (pubmed graph coverage)
- Modify: `docs/project-incubation-baseline.md` (a drift-log entry: the DOI defect with the
  n1300 figure, the retraction rule, the six relations, facets, deferred items: PubTator3/NLP
  as sub-project 2, the PGQ extension, PMC/Europe PMC graph files, grants)

- [ ] **Step 1: Write the docs.** Name env keys only, never values. Keep ruff happy: ruff 0.16
  formats Python code blocks inside Markdown, so run `uv run --no-sync ruff format --check .`.
- [ ] **Step 2: Run** the full not-pg suite and `ruff check .`. Expected: all pass.
- [ ] **Step 3: Commit**: `docs: pubmed graph, schema 1.6, bulk groups, facets, audit`.
