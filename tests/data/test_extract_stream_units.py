"""An extractor streams a unit's rows into the staging writer.

``process_one`` must hand ``write_rows`` an iterator that yields rows on
demand, never a list of every row of the unit. Memory is then bounded by one
write batch, not by the unit (incident 2026-10-27: the whole-unit list grew to
~3 GB per worker on a baseline archive of ~116k rows).

All ``not pg`` -- no DB.
"""

from __future__ import annotations

import gzip
import importlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from episteme.data.checkpoint_markers import failed_marker_path, success_marker_path

N = 5
_BODY = "Fixture body text that is long enough to clear the ok-text gate. " * 8


def _apollo(raw: Path) -> Path:
    p = raw / "units.jsonl"
    p.write_text(
        "".join(
            json.dumps({"id": f"a{i}", "text": f"{_BODY} {i}", "lang": "en"}) + "\n"
            for i in range(N)
        ),
        encoding="utf-8",
    )
    return p


def _guidelines(raw: Path) -> Path:
    p = raw / "units.jsonl"
    p.write_text(
        "".join(
            json.dumps(
                {
                    "id": f"g{i}",
                    "source": "fixture",
                    "title": f"T{i}",
                    "clean_text": f"{_BODY} {i}",
                    "raw_text": f"{_BODY} {i}",
                    "url": f"https://example.invalid/{i}",
                    "overview": "o",
                }
            )
            + "\n"
            for i in range(N)
        ),
        encoding="utf-8",
    )
    return p


def _manuscript(raw: Path) -> Path:
    p = raw / "units.tar.gz"
    with tarfile.open(p, "w:gz") as tar:
        for i in range(N - 1):
            body = f"{_BODY} {i}".encode()
            info = tarfile.TarInfo(name=f"PMC{1000 + i}.txt")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        # One .xml member: runs the NamedTemporaryFile branch of the reader.
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?><article><front><article-meta>'
            '<article-id pub-id-type="pmcid">PMC2000</article-id>'
            "<title-group><article-title>T</article-title></title-group>"
            f"</article-meta></front><body><p>{_BODY}</p></body></article>"
        ).encode()
        info = tarfile.TarInfo(name="PMC2000.xml")
        info.size = len(xml)
        tar.addfile(info, io.BytesIO(xml))
    return p


def _preprint(raw: Path) -> Path:
    p = raw / "PPR1.xml"
    arts = "".join(
        "<article><front><article-meta>"
        f'<article-id pub-id-type="doi">10.1101/x{i}</article-id>'
        "<title-group><article-title>T</article-title></title-group>"
        f"</article-meta></front><body><p>{_BODY} {i}</p></body></article>"
        for i in range(N)
    )
    p.write_text(f'<?xml version="1.0" encoding="UTF-8"?><set>{arts}</set>', encoding="utf-8")
    return p


def _pubmed(raw: Path) -> Path:
    p = raw / "pubmed_units.xml.gz"
    arts = "".join(
        f"<PubmedArticle><MedlineCitation><PMID>{100 + i}</PMID><Article>"
        f"<ArticleTitle>T{i}</ArticleTitle><Abstract><AbstractText>{_BODY}</AbstractText></Abstract>"
        "<Journal><Title>J</Title><JournalIssue><PubDate><Year>2023</Year></PubDate>"
        "</JournalIssue></Journal></Article></MedlineCitation></PubmedArticle>"
        for i in range(N)
    )
    with gzip.open(p, "wb") as f:
        f.write(f'<?xml version="1.0"?><PubmedArticleSet>{arts}</PubmedArticleSet>'.encode())
    return p


# (id, module, source, row iterator name, fixture builder)
CASES = [
    (
        "manuscript",
        "episteme.data.europepmc.manuscripts.extract_europepmc_manuscripts",
        "europepmc_manuscript",
        "iter_rows_from_archive",
        _manuscript,
    ),
    (
        "preprint",
        "episteme.data.europepmc.preprints.extract_europepmc_preprints",
        "europepmc_preprint",
        "iter_articles",
        _preprint,
    ),
    (
        "guidelines",
        "episteme.data.guidelines.extract_guidelines",
        "guidelines",
        "iter_rows_from_file",
        _guidelines,
    ),
    ("apollo", "episteme.data.apollo.extract_apollo", "apollo", "iter_rows_from_file", _apollo),
    ("pubmed", "episteme.data.pubmed.extract_pubmed", "pubmed", "iter_records_from_file", _pubmed),
]
IDS = [c[0] for c in CASES]


class _Counted:
    """Wraps a module's row iterator; ``produced`` counts items handed out."""

    def __init__(self, real, fail_after: int | None = None):
        self.real = real
        self.produced = 0
        self.fail_after = fail_after
        self.closed = False

    def __call__(self, *a, **kw):
        try:
            for item in self.real(*a, **kw):
                if self.fail_after is not None and self.produced >= self.fail_after:
                    raise RuntimeError("boom mid-stream")
                self.produced += 1
                yield item
        finally:
            self.closed = True


def _setup(tmp_path, monkeypatch, case, *, fail_after=None):
    _id, modname, source, iter_name, build = case
    mod = importlib.import_module(modname)
    raw = tmp_path / "raw"
    raw.mkdir()
    path = build(raw)
    counted = _Counted(getattr(mod, iter_name), fail_after)
    monkeypatch.setattr(mod, iter_name, counted)
    monkeypatch.setattr(mod, "_best_effort_audit", lambda *a, **k: None)
    return mod, source, raw, path, counted


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_process_one_streams_rows_into_write_rows(tmp_path, monkeypatch, case):
    mod, source, raw, path, counted = _setup(tmp_path, monkeypatch, case)
    real_write = mod.write_rows
    seen: dict = {}

    def fake_write_rows(rows, *args, **kwargs):
        assert not isinstance(rows, list), "write_rows got a whole-unit list"
        consumed = 0
        collected = []
        for row in rows:
            consumed += 1
            collected.append(row)
            # Lazy: no row is produced ahead of consumption.
            assert counted.produced == consumed
        seen["n"] = consumed
        # Delegate the real write with the rows already seen (shard checks below).
        return real_write(collected, *args, **kwargs)

    monkeypatch.setattr(mod, "write_rows", fake_write_rows)
    processed = tmp_path / "processed"
    res = mod.process_one(path, raw_dir=raw, processed_dir=processed, force=False)

    assert res["ok"] is True
    assert seen["n"] == N
    assert res["n_rows"] == N
    assert sum(res["extract_status_counts"].values()) == N
    assert res["write"]["n_rows"] == N
    assert success_marker_path(processed, source, res["source_file"]).is_file()


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_midstream_failure_records_rows_produced_before_the_error(tmp_path, monkeypatch, case):
    mod, source, raw, path, counted = _setup(tmp_path, monkeypatch, case, fail_after=2)
    processed = tmp_path / "processed"
    res = mod.process_one(path, raw_dir=raw, processed_dir=processed, force=False)

    assert res["ok"] is False
    assert "boom mid-stream" in res["error"]
    marker = json.loads(failed_marker_path(processed, source, res["source_file"]).read_text())
    assert marker["stats"] == {"n_rows_partial": 2}
    assert not success_marker_path(processed, source, res["source_file"]).exists()


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_writer_failure_closes_the_reader_and_leaves_no_tmp(tmp_path, monkeypatch, case):
    mod, source, raw, path, counted = _setup(tmp_path, monkeypatch, case)

    def failing_write_rows(rows, *args, **kwargs):
        next(rows)
        next(rows)
        raise OSError("disk full")

    monkeypatch.setattr(mod, "write_rows", failing_write_rows)
    processed = tmp_path / "processed"
    res = mod.process_one(path, raw_dir=raw, processed_dir=processed, force=False)

    assert res["ok"] is False
    assert "disk full" in res["error"]
    assert counted.closed is True
    assert list(processed.rglob("*.tmp")) == []
    marker = json.loads(failed_marker_path(processed, source, res["source_file"]).read_text())
    assert marker["stats"] == {"n_rows_partial": 2}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_real_writer_failure_mid_batch_leaves_no_tmp_and_no_shard(tmp_path, monkeypatch, case):
    # Review (Greptile): use the REAL writer, so a staging .tmp file exists when
    # the write fails; it must be removed and no shard must appear.
    import pyarrow.parquet as pq

    from episteme.data import staging_writer

    mod, source, raw, path, counted = _setup(tmp_path, monkeypatch, case)
    monkeypatch.setattr(staging_writer, "WRITE_BATCH_BYTES", 1)  # one row per batch
    calls = {"n": 0}
    real_write_table = pq.ParquetWriter.write_table

    def failing_write_table(self, table, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full mid-batch")
        return real_write_table(self, table, *a, **kw)

    monkeypatch.setattr(pq.ParquetWriter, "write_table", failing_write_table)
    processed = tmp_path / "processed"
    res = mod.process_one(path, raw_dir=raw, processed_dir=processed, force=False)

    assert res["ok"] is False and "disk full mid-batch" in res["error"]
    assert calls["n"] == 2  # the first batch was written to the .tmp file
    assert counted.closed is True
    assert list(processed.rglob("*.tmp")) == []
    staging = processed / "staging" / source
    assert not staging.exists() or list(staging.glob("*.parquet")) == []
    assert not success_marker_path(processed, source, res["source_file"]).exists()


def test_pubmed_graph_files_are_final_before_the_shard(tmp_path, monkeypatch):
    # Review (Greptile): a hard kill between the two must never leave a final
    # shard without its graph files, so the graph files are published first.
    from episteme.data import graph_files

    case = next(c for c in CASES if c[0] == "pubmed")
    mod, source, raw, path, _counted = _setup(tmp_path, monkeypatch, case)
    processed = tmp_path / "processed"
    staging = processed / "staging" / source
    seen = {}
    real_close = graph_files.GraphFileWriter.close

    def spying_close(self):
        seen["shards_at_graph_close"] = sorted(p.name for p in staging.glob("*.parquet"))
        return real_close(self)

    monkeypatch.setattr(graph_files.GraphFileWriter, "close", spying_close)
    res = mod.process_one(path, raw_dir=raw, processed_dir=processed, force=False)

    assert res["ok"] is True
    assert seen["shards_at_graph_close"] == []  # no final shard yet when the graph files close
    assert list(staging.glob("*.parquet"))  # the shard is final afterwards


def test_pubmed_graph_close_failure_removes_the_final_shard(tmp_path, monkeypatch):
    from episteme.data import graph_files

    case = next(c for c in CASES if c[0] == "pubmed")
    mod, source, raw, path, _counted = _setup(tmp_path, monkeypatch, case)

    def failing_close(self):
        self.abort()
        raise OSError("graph close failed")

    monkeypatch.setattr(graph_files.GraphFileWriter, "close", failing_close)
    processed = tmp_path / "processed"
    res = mod.process_one(path, raw_dir=raw, processed_dir=processed, force=False)

    assert res["ok"] is False
    staging = processed / "staging" / source
    assert not staging.exists() or list(staging.iterdir()) == []
    assert not success_marker_path(processed, source, res["source_file"]).exists()
