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
        for i in range(N):
            body = f"{_BODY} {i}".encode()
            info = tarfile.TarInfo(name=f"PMC{1000 + i}.txt")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
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

    def __call__(self, *a, **kw):
        for item in self.real(*a, **kw):
            if self.fail_after is not None and self.produced >= self.fail_after:
                raise RuntimeError("boom mid-stream")
            self.produced += 1
            yield item


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
