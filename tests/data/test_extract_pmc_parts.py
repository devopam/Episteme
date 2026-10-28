"""PMC extraction by fixed id bucket, written in bounded parts (spec 4.2-4.4, 5)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from episteme.data import parts_retire
from episteme.data.checkpoint_markers import (
    failed_marker_path,
    is_success,
    load_success_marker_path,
    success_marker_path,
)
from episteme.data.pmc import buckets
from episteme.data.pmc import extract_pmc as pmc

BIG = 10**12


def _meta(n: int, title: str | None = None) -> str:
    return json.dumps(
        {
            "pmcid": f"PMC{n}",
            "version": 1,
            "pmid": 40_000_000 + n,
            "doi": f"10.0/{n}",
            "title": title or f"Article PMC{n}",
            "citation": "Test J. 2024 Jan 1;1(1):1-2.",
            "is_manuscript": False,
            "is_historical_ocr": False,
            "is_retracted": False,
            "license_code": "CC BY",
        }
    )


def _xml(n: int, body: str = "") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<article>
  <front><article-meta>
    <article-id pub-id-type="pmc">PMC{n}</article-id>
    <title-group><article-title>Article PMC{n}</article-title></title-group>
    <abstract><p>Abstract text for PMC{n}, long enough to pass the minimum ok text
    length threshold so that the extract status of this row lands ok.</p></abstract>
  </article-meta></front>
  <body><sec><p>Body text for PMC{n}. {body}</p></sec></body>
</article>"""


def _article(raw: Path, n: int, *, xml: bool = True, body: str = "") -> Path:
    meta = raw / "metadata" / f"PMC{n}.1.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(_meta(n), encoding="utf-8")
    if xml:
        x = raw / "xml" / f"PMC{n}.1.xml"
        x.parent.mkdir(parents=True, exist_ok=True)
        x.write_text(_xml(n, body), encoding="utf-8")
    return meta


def _caps(monkeypatch, rows: int, nbytes: int) -> None:
    monkeypatch.setattr(pmc, "_part_caps", lambda: (rows, nbytes))


def _staging(out: Path) -> Path:
    return out / "staging" / "pmc"


def _part_rows(out: Path, name: str) -> list[dict]:
    return pq.read_table(_staging(out) / f"{name}.parquet").to_pylist()


def _parts(out: Path) -> list[str]:
    return sorted(p.name.split(".")[0] for p in _staging(out).glob("*.parquet"))


def _bucket_marker(out: Path, key: str = "pmc_b0000") -> dict:
    p = success_marker_path(out, "pmc", key)
    return json.loads(p.read_text(encoding="utf-8"))["stats"]


@pytest.fixture
def raw(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


def test_one_bucket_writes_parts_within_caps(tmp_path, raw, monkeypatch):
    for n in range(1, 8):
        _article(raw, n)
    _caps(monkeypatch, 3, BIG)
    out = tmp_path / "out"
    res = pmc.extract_pmc(raw, out)
    assert (res["ok"], res["failed"], res["rows"], res["buckets"]) == (1, 0, 7, 1)
    names = [f"pmc_b0000__part0000{i}" for i in (1, 2, 3)]
    assert _parts(out) == names
    assert [len(_part_rows(out, n)) for n in names] == [3, 3, 1]
    rows = _part_rows(out, names[0])
    assert {r["source_file"] for r in rows} == {names[0]}
    assert [r["pmcid"] for r in rows] == ["PMC1", "PMC2", "PMC3"]
    st = _bucket_marker(out)
    assert st["parts"] == 3
    assert st["parts_list"] == names
    assert st["n_rows"] == 7
    part = json.loads(success_marker_path(out, "pmc", names[1]).read_text("utf-8"))["stats"]
    assert (part["n_rows"], part["first_pmcid"], part["last_pmcid"]) == (3, "PMC4", "PMC6")
    assert (part["part_rows"], part["part_bytes"]) == (3, BIG)
    assert part["extract_status_counts"] == {"ok": 3}
    assert part["est_bytes"] > 0 and part["write"]["n_rows"] == 3


def test_byte_cap_ends_a_part_and_an_oversized_row_is_alone(tmp_path, raw, monkeypatch):
    _article(raw, 1)
    _article(raw, 2, body="x" * 20_000)
    _article(raw, 3)
    _article(raw, 4)
    _caps(monkeypatch, 100, 5_000)
    out = tmp_path / "out"
    pmc.extract_pmc(raw, out)
    names = _parts(out)
    assert [[r["pmcid"] for r in _part_rows(out, n)] for n in names] == [
        ["PMC1"],
        ["PMC2"],
        ["PMC3", "PMC4"],
    ]
    big = json.loads(success_marker_path(out, "pmc", names[1]).read_text("utf-8"))["stats"]
    assert big["est_bytes"] > 5_000


def test_pmc_row_bytes_counts_strings_and_string_lists():
    row = {"a": "héllo", "b": ["ab", "c", 3], "c": 7, "d": None}
    assert pmc.pmc_row_bytes(row) == 6 + 3 + pmc.PART_ROW_OVERHEAD_BYTES


def test_rows_stream_into_the_writer(tmp_path, raw, monkeypatch):
    for n in range(1, 6):
        _article(raw, n)
    _caps(monkeypatch, 3, BIG)
    made = {"n": 0}
    consumed = {"n": 0, "max_ahead": 0}
    real_row = pmc.row_from_meta_and_xml

    def _row(*a, **k):
        made["n"] += 1
        consumed["max_ahead"] = max(consumed["max_ahead"], made["n"] - consumed["n"])
        return real_row(*a, **k)

    def _write_rows(rows, *a, **k):
        n = 0
        for _ in rows:
            n += 1
            consumed["n"] += 1
        return {"format": "parquet", "paths": [], "n_rows": n}

    monkeypatch.setattr(pmc, "row_from_meta_and_xml", _row)
    monkeypatch.setattr(pmc, "write_rows", _write_rows)
    res = pmc.extract_pmc(raw, tmp_path / "out")
    assert res["rows"] == 5
    assert consumed["max_ahead"] <= 1


def test_matching_fingerprint_skips_the_bucket(tmp_path, raw, monkeypatch):
    for n in (1, 2):
        _article(raw, n)
    out = tmp_path / "out"
    pmc.extract_pmc(raw, out)
    calls: list = []
    monkeypatch.setattr(pmc, "write_rows", lambda *a, **k: calls.append(1))
    res = pmc.extract_pmc(raw, out)
    assert calls == []
    assert res["ok"] == 0 and res["failed"] == 0


def test_changed_input_rebuilds_the_bucket(tmp_path, raw, monkeypatch):
    for n in (1, 2):
        _article(raw, n)
    out = tmp_path / "out"
    pmc.extract_pmc(raw, out)
    meta = raw / "metadata" / "PMC2.1.json"
    meta.write_text(_meta(2, title="A new title for the second article"), encoding="utf-8")
    st = meta.stat()
    os.utime(meta, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    res = pmc.extract_pmc(raw, out)
    assert res["ok"] == 1
    rows = _part_rows(out, "pmc_b0000__part00001")
    titles = {r["pmcid"]: r["title"] for r in rows}
    assert titles["PMC2"] == "A new title for the second article"


def test_rebuild_with_fewer_parts_retires_the_extra_loaded_parts(tmp_path, raw, monkeypatch):
    for n in range(1, 8):
        _article(raw, n)
    _caps(monkeypatch, 3, BIG)
    out = tmp_path / "out"
    pmc.extract_pmc(raw, out)
    names = [f"pmc_b0000__part0000{i}" for i in (1, 2, 3)]
    for name in names:
        p = load_success_marker_path(out, "pmc", f"{name}.parquet")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
    for n in range(4, 8):
        (raw / "metadata" / f"PMC{n}.1.json").unlink()
    res = pmc.extract_pmc(raw, out)
    assert res["ok"] == 1
    assert _parts(out) == [names[0]]
    for name in names[1:]:
        assert not is_success(out, "pmc", name)
    assert not any(load_success_marker_path(out, "pmc", "x").parent.glob("*"))
    assert parts_retire.read_retire_pending(out, "pmc") == names
    assert _bucket_marker(out)["parts"] == 1


def test_partial_bucket_is_written_again_from_part_one(tmp_path, raw, monkeypatch):
    for n in range(1, 8):
        _article(raw, n)
    _caps(monkeypatch, 3, BIG)
    out = tmp_path / "out"
    pmc.extract_pmc(raw, out)
    success_marker_path(out, "pmc", "pmc_b0000").unlink()
    success_marker_path(out, "pmc", "pmc_b0000__part00003").unlink()
    _caps(monkeypatch, 4, BIG)
    res = pmc.extract_pmc(raw, out)
    assert res["ok"] == 1
    names = ["pmc_b0000__part00001", "pmc_b0000__part00002"]
    assert _parts(out) == names
    assert [len(_part_rows(out, n)) for n in names] == [4, 3]
    assert not success_marker_path(out, "pmc", "pmc_b0000__part00003").exists()
    assert _bucket_marker(out)["parts_list"] == names


def test_metadata_without_xml_still_writes_a_row(tmp_path, raw):
    _article(raw, 1, xml=False)
    out = tmp_path / "out"
    res = pmc.extract_pmc(raw, out)
    assert res["rows"] == 1
    (row,) = _part_rows(out, "pmc_b0000__part00001")
    assert row["pmcid"] == "PMC1" and row["body_text"] is None


def test_failed_article_does_not_fail_the_bucket(tmp_path, raw):
    _article(raw, 1)
    bad = _article(raw, 2)
    bad.write_text("{not json", encoding="utf-8")
    _article(raw, 3)
    out = tmp_path / "out"
    res = pmc.extract_pmc(raw, out)
    assert (res["ok"], res["failed"], res["rows"], res["failed_articles_count"]) == (1, 0, 2, 1)
    st = _bucket_marker(out)
    assert st["failed_articles_count"] == 1
    assert [f["file"] for f in st["failed_articles"]] == ["metadata__PMC2.1.json"]
    assert failed_marker_path(out, "pmc", "metadata__PMC2.1.json").is_file()


def test_transient_article_error_retries_the_bucket(tmp_path, raw, monkeypatch):
    from episteme.ops.retry import TransientUnitError

    _article(raw, 1)

    def _timeout(*_a, **_k):
        raise TimeoutError("disk timed out")

    monkeypatch.setattr(pmc, "row_from_meta_and_xml", _timeout)
    out = tmp_path / "out"
    grouped, _ = buckets.group_by_bucket(pmc.discover_meta_files(raw), raw)
    manifest = buckets.write_manifest(out, grouped[0], raw)
    with pytest.raises(TransientUnitError):
        pmc.process_bucket(manifest, raw_dir=raw, processed_dir=out, force=False)
    assert not failed_marker_path(out, "pmc", "metadata__PMC1.1.json").exists()


def test_two_buckets_run_in_the_pool(tmp_path, raw, spawn_child_env):
    _article(raw, 1)
    _article(raw, 100_001)
    out = tmp_path / "out"
    res = pmc.extract_pmc(raw, out, workers=2)
    assert (res["buckets"], res["ok"], res["rows"]) == (2, 2, 2)
    assert is_success(out, "pmc", "pmc_b0000")
    assert is_success(out, "pmc", "pmc_b0002")


def test_summary_counts_versions_duplicates_and_unrecognised(tmp_path, raw):
    _article(raw, 1)
    (raw / "metadata" / "PMC1.0.json").write_text(_meta(1), encoding="utf-8")
    (raw / "other").mkdir()
    (raw / "other" / "PMC1.1.json").write_text(_meta(1), encoding="utf-8")
    (raw / "metadata" / "PMCnotes.json").write_text("{}", encoding="utf-8")
    res = pmc.extract_pmc(raw, tmp_path / "out")
    assert res["rows"] == 1
    assert (res["superseded_versions"], res["duplicate_files"], res["unrecognised_files"]) == (
        1,
        1,
        1,
    )


def test_duplicate_row_id_in_a_bucket_fails_the_bucket(tmp_path, raw):
    _article(raw, 1)
    m2 = _article(raw, 2)
    m2.write_text(_meta(1), encoding="utf-8")  # metadata of PMC2 claims PMC1
    (raw / "xml" / "PMC2.1.xml").write_text(_xml(1), encoding="utf-8")
    res = pmc.extract_pmc(raw, tmp_path / "out")
    assert res["failed"] == 1
    assert not is_success(tmp_path / "out", "pmc", "pmc_b0000")


def test_skip_unit_takes_a_bucket_key(tmp_path, raw, monkeypatch):
    from episteme.ops import skip

    _article(raw, 1)
    _article(raw, 50_001)
    recorded: list = []
    monkeypatch.setattr(
        skip,
        "apply_skips",
        lambda src, st, keys, names, reason, run_id: (
            recorded.append(names) or [k for k in keys if k not in names]
        ),
    )
    out = tmp_path / "out"
    res = pmc.extract_pmc(raw, out, skip_units=["pmc_b0000"], skip_reason="r")
    assert recorded == [["pmc_b0000"]]
    assert res["buckets"] == 1
    assert not is_success(out, "pmc", "pmc_b0000")
    assert is_success(out, "pmc", "pmc_b0001")


def test_max_files_limits_buckets(tmp_path, raw):
    for n in (1, 2, 50_001, 100_001):
        _article(raw, n)
    res = pmc.extract_pmc(raw, tmp_path / "out", max_files=2)
    assert (res["buckets"], res["rows"]) == (2, 3)


def test_concurrent_rebuilds_keep_every_loaded_part_name(tmp_path, raw, spawn_child_env):
    """Two buckets rebuild at once in the pool; each records its loaded part
    names in its own retire_pending.d file, so no name is lost."""
    for n in (1, 2, 50_001, 50_002):
        _article(raw, n)
    out = tmp_path / "out"
    pmc.extract_pmc(raw, out, workers=2)
    names = ["pmc_b0000__part00001", "pmc_b0001__part00001"]
    for name in names:
        p = load_success_marker_path(out, "pmc", f"{name}.parquet")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
    res = pmc.extract_pmc(raw, out, workers=2, force=True)
    assert res["ok"] == 2
    assert parts_retire.read_retire_pending(out, "pmc") == names
    unit_dir = out / "_ops" / "pmc" / "retire_pending.d"
    assert sorted(p.name for p in unit_dir.glob("*.json")) == ["pmc_b0000.json", "pmc_b0001.json"]
    assert not parts_retire.retire_pending_path(out, "pmc").exists()
