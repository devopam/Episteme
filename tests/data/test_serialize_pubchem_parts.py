"""PubChem sub-project 2, Task 2: bounded parts that resume by CID range.

Fixture: a 10-CID primary ``CID-SMILES.tsv`` with its three siblings, written
to ``tmp_path``. Every CID has the same property values, so the row texts
differ only in the CID digits. Caps are set by monkeypatching
``serialize_pubchem._part_caps``. All ``not pg`` -- no DB.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import polars as pl
import pytest

from episteme.data.checkpoint_markers import load_success_marker_path, success_marker_path
from episteme.data.pubchem import serialize_pubchem as mod
from episteme.ops import gate, gate_report
from episteme.ops.profile import PART_ROW_OVERHEAD_BYTES

KEY = "CID-SMILES.tsv"
CIDS = list(range(1, 11))
BIG = 10**9


def _make_raw(raw: Path) -> Path:
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "CID-SMILES.tsv").write_bytes("".join(f"{c}\tCCO\n" for c in CIDS).encode())
    (raw / "CID-IUPAC.tsv").write_bytes("".join(f"{c}\tethanol\n" for c in CIDS).encode())
    (raw / "CID-Title.tsv").write_bytes("".join(f"{c}\tEthanol\n" for c in CIDS).encode())
    (raw / "CID-Mass.tsv").write_bytes(
        "".join(f"{c}\tC2H6O\t46.0419\t46.0419\n" for c in CIDS).encode()
    )
    return raw


@pytest.fixture
def raw(tmp_path):
    return _make_raw(tmp_path / "raw")


@pytest.fixture
def out(tmp_path):
    return tmp_path / "processed"


@pytest.fixture
def audits(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(mod, "_best_effort_audit", lambda name, n: calls.append((name, n)))
    return calls


def _caps(monkeypatch, rows, nbytes=BIG):
    monkeypatch.setattr(mod, "_part_caps", lambda: (rows, nbytes))


def _staging(out: Path) -> Path:
    return out / "staging" / "pubchem"


def _shard(out: Path, n: int, key: str = KEY) -> Path:
    return _staging(out) / f"{mod.part_source_file(key, n)}.parquet"


def _marker(out: Path, name: str) -> dict:
    return json.loads(success_marker_path(out, "pubchem", name).read_text(encoding="utf-8"))


def _part_marker(out: Path, n: int, key: str = KEY) -> dict:
    return _marker(out, mod.part_source_file(key, n))["stats"]


def _shard_names(out: Path) -> list[str]:
    return sorted(p.name for p in _staging(out).glob("*") if p.is_file())


def _all_cids(out: Path) -> list[int]:
    cids: list[int] = []
    for p in sorted(_staging(out).glob("*.parquet")):
        cids.extend(int(i) for i in pl.read_parquet(p)["source_record_id"].to_list())
    return cids


def test_part_source_file_name():
    assert mod.part_source_file("CID-SMILES.gz", 1) == "CID-SMILES.gz__part00001"
    assert mod.part_source_file("a__CID-SMILES.gz", 123) == "a__CID-SMILES.gz__part00123"


def test_parts_by_row_cap(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    res = mod.serialize_pubchem(raw, out)
    assert res == {"inputs": 1, "ok": 1, "failed": 0, "rows": 10}
    assert _shard_names(out) == [f"{KEY}__part0000{i}.parquet" for i in (1, 2, 3)]
    for n, (rows, first, last) in enumerate([(4, 1, 4), (4, 5, 8), (2, 9, 10)], start=1):
        st = _part_marker(out, n)
        assert (st["n_rows"], st["first_cid"], st["last_cid"]) == (rows, first, last)
        assert (st["part_rows"], st["part_bytes"]) == (4, BIG)
        assert st["est_bytes"] > 0 and st["write"]["n_rows"] == rows
        assert sum(st["extract_status_counts"].values()) == rows
        df = pl.read_parquet(_shard(out, n))
        assert set(df["source_file"].to_list()) == {mod.part_source_file(KEY, n)}
    primary = _marker(out, KEY)["stats"]
    assert primary["parts"] == 3
    assert primary["parts_written"] == 3 and primary["parts_kept"] == 0
    assert primary["n_rows"] == 10
    assert (primary["part_rows"], primary["part_bytes"]) == (4, BIG)
    assert sum(primary["extract_status_counts"].values()) == 10
    assert "elapsed_sec" in primary
    assert audits == [(mod.part_source_file(KEY, n), r) for n, r in ((1, 4), (2, 4), (3, 2))]
    assert _all_cids(out) == CIDS


def test_parts_by_byte_cap(raw, out, monkeypatch, audits):
    # Two of the longest rows fit exactly; a third row never fits.
    _caps(monkeypatch, 10**6, 2 * _row_est(raw, max))
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    assert _marker(out, KEY)["stats"]["parts"] == 5
    for n in range(1, 6):
        st = _part_marker(out, n)
        assert st["n_rows"] == 2
        assert st["est_bytes"] <= st["part_bytes"]
    assert _all_cids(out) == CIDS


def _row_est(raw: Path, pick=min) -> int:
    """The estimated staging bytes of the shortest (or longest) fixture row."""
    rows = list(mod.iter_rows_from_file(raw / KEY))
    return pick(len(r["text"].encode("utf-8")) for r in rows) + PART_ROW_OVERHEAD_BYTES


def _gate_parts(out: Path, raw: Path, caps: tuple[int, int]) -> tuple[dict, list[str]]:
    m = gate_report.measure_staging(_staging(out), inputs=[raw / KEY])
    m["part_rows"], m["part_bytes"] = caps
    return m, gate.evaluate(m, {"parts_within_caps": True})


@pytest.mark.parametrize("binding", ["bytes", "rows"])
def test_serializer_output_passes_parts_within_caps(raw, out, monkeypatch, audits, binding):
    # A byte cap of 2.5 rows: the old rule took a third row and ended over the cap.
    caps = (10**6, int(2.5 * _row_est(raw))) if binding == "bytes" else (3, BIG)
    _caps(monkeypatch, *caps)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    m, fails = _gate_parts(out, raw, caps)
    assert fails == []
    assert m["max_rows_per_source_file"] == (2 if binding == "bytes" else 3)
    assert m["max_est_bytes_per_source_file"] <= caps[1]
    assert _all_cids(out) == CIDS


def test_a_row_over_the_byte_cap_goes_alone_in_its_part(raw, out, monkeypatch, audits):
    caps = (10**6, _row_est(raw) - 1)
    _caps(monkeypatch, *caps)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    assert _marker(out, KEY)["stats"]["parts"] == 10
    for n in range(1, 11):
        st = _part_marker(out, n)
        assert st["n_rows"] == 1 and st["est_bytes"] > st["part_bytes"]
    _m, fails = _gate_parts(out, raw, caps)
    assert fails == []
    assert _all_cids(out) == CIDS


def test_resume_after_crash_mid_part(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    real = mod.write_rows

    def _crash_on_part2(rows, staging_root, *, source_file, **kw):
        if source_file.endswith("__part00002"):
            # A half-written shard of part 2, then the crash.
            stray = _staging(out) / f"{source_file}.parquet"
            stray.write_bytes(b"half a parquet file")
            raise RuntimeError("disk went away")
        return real(rows, staging_root, source_file=source_file, **kw)

    monkeypatch.setattr(mod, "write_rows", _crash_on_part2)
    res = mod.serialize_pubchem(raw, out)
    assert res["failed"] == 1
    failed = json.loads(
        (out / "_ops" / "pubchem" / "failed" / f"{KEY}.json").read_text(encoding="utf-8")
    )
    assert failed["stats"]["failed_part"] == 2
    assert not success_marker_path(out, "pubchem", KEY).exists()
    part1_mtime = _shard(out, 1).stat().st_mtime_ns

    monkeypatch.setattr(mod, "write_rows", real)
    res = mod.serialize_pubchem(raw, out)
    assert res == {"inputs": 1, "ok": 1, "failed": 0, "rows": 10}
    assert _shard(out, 1).stat().st_mtime_ns == part1_mtime
    assert pl.read_parquet(_shard(out, 2)).height == 4  # the stray shard was replaced
    primary = _marker(out, KEY)["stats"]
    assert (primary["parts_kept"], primary["parts_written"], primary["parts"]) == (1, 2, 3)
    assert primary["n_rows"] == 10
    assert _all_cids(out) == CIDS
    assert not (out / "_ops" / "pubchem" / "failed" / f"{KEY}.json").exists()


def test_resume_with_different_caps_loses_no_cid(raw, out, monkeypatch, audits, caplog):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    # Simulate a kill after part 1: no primary marker, no parts >= 2.
    success_marker_path(out, "pubchem", KEY).unlink()
    for n in (2, 3):
        success_marker_path(out, "pubchem", mod.part_source_file(KEY, n)).unlink()
        _shard(out, n).unlink()

    _caps(monkeypatch, 3)
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        assert mod.serialize_pubchem(raw, out)["ok"] == 1
    assert any("caps" in r.getMessage() for r in caplog.records)
    assert (_part_marker(out, 1)["first_cid"], _part_marker(out, 1)["last_cid"]) == (1, 4)
    assert [
        (_part_marker(out, n)["first_cid"], _part_marker(out, n)["last_cid"]) for n in (2, 3)
    ] == [(5, 7), (8, 10)]
    assert sorted(_all_cids(out)) == CIDS
    assert len(_all_cids(out)) == len(set(_all_cids(out)))
    assert _marker(out, KEY)["stats"]["n_rows"] == 10


def test_force_rewrites_all_parts(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    # A stale part from a longer earlier run.
    stale = mod.part_source_file(KEY, 9)
    _shard(out, 9).write_bytes(b"stale")
    success_marker_path(out, "pubchem", stale).write_text("{}", encoding="utf-8")

    _caps(monkeypatch, 5)
    assert mod.serialize_pubchem(raw, out, force=True)["ok"] == 1
    assert _shard_names(out) == [f"{KEY}__part0000{i}.parquet" for i in (1, 2)]
    assert not success_marker_path(out, "pubchem", stale).exists()
    assert not success_marker_path(out, "pubchem", mod.part_source_file(KEY, 3)).exists()
    assert _part_marker(out, 1)["n_rows"] == 5  # part 1 was rewritten with the new caps
    primary = _marker(out, KEY)["stats"]
    assert (primary["parts"], primary["parts_kept"]) == (2, 0)
    assert _all_cids(out) == CIDS


def test_parts_above_a_gap_are_dropped(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    success_marker_path(out, "pubchem", KEY).unlink()
    success_marker_path(out, "pubchem", mod.part_source_file(KEY, 2)).unlink()
    part1_mtime = _shard(out, 1).stat().st_mtime_ns

    audits.clear()
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    assert _shard(out, 1).stat().st_mtime_ns == part1_mtime
    assert [a[0] for a in audits] == [mod.part_source_file(KEY, n) for n in (2, 3)]
    primary = _marker(out, KEY)["stats"]
    assert (primary["parts_kept"], primary["parts_written"]) == (1, 2)
    assert _all_cids(out) == CIDS


def test_kept_marker_without_shard_is_not_kept(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    success_marker_path(out, "pubchem", KEY).unlink()
    _shard(out, 2).unlink()
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    assert _marker(out, KEY)["stats"]["parts_kept"] == 1
    assert _all_cids(out) == CIDS


def test_crash_after_last_part_before_primary_marker(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    success_marker_path(out, "pubchem", KEY).unlink()
    mtimes = [_shard(out, n).stat().st_mtime_ns for n in (1, 2, 3)]
    audits.clear()
    res = mod.serialize_pubchem(raw, out)
    assert res == {"inputs": 1, "ok": 1, "failed": 0, "rows": 10}
    assert [_shard(out, n).stat().st_mtime_ns for n in (1, 2, 3)] == mtimes
    assert _shard_names(out) == [f"{KEY}__part0000{i}.parquet" for i in (1, 2, 3)]
    primary = _marker(out, KEY)["stats"]
    assert (primary["parts"], primary["parts_kept"], primary["parts_written"]) == (3, 3, 0)
    assert audits == []


def test_primary_marker_skips_unit(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    r = mod.process_one(raw / KEY, processed_dir=out, raw_dir=raw, force=False)
    assert r["skipped"] is True


def test_input_key_collision_parts(tmp_path, monkeypatch, audits):
    raw = tmp_path / "raw"
    _make_raw(raw / "a")
    _make_raw(raw / "b")
    out = tmp_path / "processed"
    _caps(monkeypatch, 4)
    res = mod.serialize_pubchem(raw, out)
    assert res == {"inputs": 2, "ok": 2, "failed": 0, "rows": 20}
    names = _shard_names(out)
    assert len(names) == 6 and len(set(names)) == 6
    assert _shard(out, 1, "a__" + KEY).is_file()
    assert _shard(out, 1, "b__" + KEY).is_file()
    assert mod.part_source_file("a__" + KEY, 1) != mod.part_source_file("b__" + KEY, 1)


def test_part_caps_reads_profile(monkeypatch):
    class _P:
        part_rows = 7
        part_bytes = 99

    monkeypatch.setattr(mod, "get_profile", lambda: _P())
    assert mod._part_caps() == (7, 99)


def _load_marker(out: Path, n: int) -> Path:
    # The loader names a load marker by shard file name (load_articles.py).
    return load_success_marker_path(out, "pubchem", _shard(out, n).name)


def test_deleted_part_also_loses_its_load_marker(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    for n in (1, 2, 3):
        p = _load_marker(out, n)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
    # A resume that keeps part 1 only: parts 2 and 3 are deleted and rewritten.
    success_marker_path(out, "pubchem", KEY).unlink()
    success_marker_path(out, "pubchem", mod.part_source_file(KEY, 2)).unlink()
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    assert _load_marker(out, 1).is_file()
    assert not _load_marker(out, 2).exists()
    assert not _load_marker(out, 3).exists()


def test_force_also_drops_load_markers(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    p = _load_marker(out, 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    assert mod.serialize_pubchem(raw, out, force=True)["ok"] == 1
    assert not p.exists()


@pytest.mark.parametrize(
    "bad_first",
    [
        pytest.param(4, id="overlap"),
        pytest.param(2, id="overlap-deep"),
        pytest.param("5", id="str"),
    ],
)
def test_marker_range_that_does_not_chain_ends_the_kept_prefix(
    raw, out, monkeypatch, audits, bad_first
):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    success_marker_path(out, "pubchem", KEY).unlink()
    m2 = success_marker_path(out, "pubchem", mod.part_source_file(KEY, 2))
    payload = json.loads(m2.read_text(encoding="utf-8"))
    payload["stats"]["first_cid"] = bad_first  # part 1 ends at CID 4
    m2.write_text(json.dumps(payload), encoding="utf-8")
    part1_mtime = _shard(out, 1).stat().st_mtime_ns

    audits.clear()
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    assert _shard(out, 1).stat().st_mtime_ns == part1_mtime
    assert [a[0] for a in audits] == [mod.part_source_file(KEY, n) for n in (2, 3)]
    assert _part_marker(out, 2)["first_cid"] == 5
    primary = _marker(out, KEY)["stats"]
    assert (primary["parts_kept"], primary["parts_written"]) == (1, 2)
    cids = _all_cids(out)
    assert sorted(cids) == CIDS and len(cids) == len(set(cids))


def test_resume_drops_parts_when_an_input_changed(raw, out, monkeypatch, audits, caplog):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    fp = _part_marker(out, 1)["inputs"]
    assert [f[0] for f in fp] == [
        "CID-SMILES.tsv",
        "CID-IUPAC.tsv",
        "CID-Mass.tsv",
        "CID-Title.tsv",
    ]
    # A stop after part 1, then a new download of one sibling.
    success_marker_path(out, "pubchem", KEY).unlink()
    for n in (2, 3):
        success_marker_path(out, "pubchem", mod.part_source_file(KEY, n)).unlink()
    lm = _load_marker(out, 1)
    lm.parent.mkdir(parents=True, exist_ok=True)
    lm.write_text("{}", encoding="utf-8")
    title = raw / "CID-Title.tsv"
    title.write_bytes("".join(f"{c}\tEthanol (new)\n" for c in CIDS).encode())
    st = title.stat()
    os.utime(title, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))

    audits.clear()
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        assert mod.serialize_pubchem(raw, out)["ok"] == 1
    assert any("input" in r.getMessage() and "changed" in r.getMessage() for r in caplog.records)
    assert not lm.exists()
    primary = _marker(out, KEY)["stats"]
    assert (primary["parts_kept"], primary["parts_written"]) == (0, 3)
    assert [a[0] for a in audits] == [mod.part_source_file(KEY, n) for n in (1, 2, 3)]
    assert all("(new)" in t for t in pl.read_parquet(_shard(out, 1))["text"].to_list())
    assert _all_cids(out) == CIDS


def test_resume_keeps_parts_when_the_inputs_are_the_same(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    success_marker_path(out, "pubchem", KEY).unlink()
    success_marker_path(out, "pubchem", mod.part_source_file(KEY, 3)).unlink()
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    assert _marker(out, KEY)["stats"]["parts_kept"] == 2


def test_a_kept_part_without_a_fingerprint_is_dropped(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    success_marker_path(out, "pubchem", KEY).unlink()
    m1 = success_marker_path(out, "pubchem", mod.part_source_file(KEY, 1))
    payload = json.loads(m1.read_text(encoding="utf-8"))
    del payload["stats"]["inputs"]
    m1.write_text(json.dumps(payload), encoding="utf-8")
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    assert _marker(out, KEY)["stats"]["parts_kept"] == 0
    assert _all_cids(out) == CIDS


def test_delete_parts_above_handles_six_digit_part_numbers(raw, out, monkeypatch, audits):
    # part_source_file writes 6+ digits from part 100000 on; the delete must see them.
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    psf = mod.part_source_file(KEY, 100000)
    assert psf.endswith("__part100000")
    shard = _staging(out) / f"{psf}.parquet"
    shard.write_bytes(b"x")
    success_marker_path(out, "pubchem", psf).write_text("{}", encoding="utf-8")
    lm = load_success_marker_path(out, "pubchem", shard.name)
    lm.parent.mkdir(parents=True, exist_ok=True)
    lm.write_text("{}", encoding="utf-8")
    mod._delete_parts_above(out, KEY, 3)
    assert not shard.exists()
    assert not success_marker_path(out, "pubchem", psf).exists()
    assert not lm.exists()
    assert _shard(out, 3).is_file()  # parts 1..3 stay


def test_primary_marker_records_parts_list_and_inputs(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    primary = _marker(out, KEY)["stats"]
    assert primary["parts_list"] == [mod.part_source_file(KEY, n) for n in (1, 2, 3)]
    assert primary["inputs"] == _part_marker(out, 1)["inputs"]


def _write_title(raw: Path, label: str) -> None:
    title = raw / "CID-Title.tsv"
    st = title.stat()
    title.write_bytes("".join(f"{c}\t{label}\n" for c in CIDS).encode())
    os.utime(title, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


def test_changed_input_after_the_unit_completed_rebuilds_without_force(
    raw, out, monkeypatch, audits, caplog
):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    lms = [_load_marker(out, n) for n in (1, 2, 3)]
    for p in lms:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
    _write_title(raw, "Ethanol (new)")

    audits.clear()
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        res = mod.serialize_pubchem(raw, out)
    assert res == {"inputs": 1, "ok": 1, "failed": 0, "rows": 10}
    assert any("input" in r.getMessage() and "changed" in r.getMessage() for r in caplog.records)
    assert not any(p.exists() for p in lms)
    primary = _marker(out, KEY)["stats"]
    assert (primary["parts_kept"], primary["parts_written"]) == (0, 3)
    assert [a[0] for a in audits] == [mod.part_source_file(KEY, n) for n in (1, 2, 3)]
    for n in (1, 2, 3):
        assert all("(new)" in t for t in pl.read_parquet(_shard(out, n))["text"].to_list())
    # The same inputs again: the unit is skipped.
    r = mod.process_one(raw / KEY, processed_dir=out, raw_dir=raw, force=False)
    assert r["skipped"] is True


def test_changed_input_drops_parts_above_the_new_count(raw, out, monkeypatch, audits):
    _caps(monkeypatch, 4)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    # The primary shrinks to CIDs 1..5: 2 parts, and part 3 must go.
    (raw / "CID-SMILES.tsv").write_bytes("".join(f"{c}\tCCO\n" for c in range(1, 6)).encode())
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    assert _shard_names(out) == [f"{KEY}__part0000{i}.parquet" for i in (1, 2)]
    assert not success_marker_path(out, "pubchem", mod.part_source_file(KEY, 3)).exists()
    assert _marker(out, KEY)["stats"]["parts_list"] == [
        mod.part_source_file(KEY, n) for n in (1, 2)
    ]
    assert _all_cids(out) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "stats",
    [
        pytest.param({"n_rows": 10}, id="pre-parts"),
        pytest.param({"n_rows": 10, "parts": 3}, id="no-parts-list"),
        pytest.param({"n_rows": 10, "parts": 3, "parts_list": ["a", "b", "c"]}, id="no-inputs"),
    ],
)
def test_a_primary_marker_without_the_parts_format_does_not_skip(
    raw, out, monkeypatch, audits, stats
):
    _caps(monkeypatch, 4)
    m = success_marker_path(out, "pubchem", KEY)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(json.dumps({"source_file": KEY, "stats": stats}), encoding="utf-8")
    r = mod.process_one(raw / KEY, processed_dir=out, raw_dir=raw, force=False)
    assert r["skipped"] is False and r["ok"] is True
    primary = _marker(out, KEY)["stats"]
    assert primary["parts_list"] == [mod.part_source_file(KEY, n) for n in (1, 2, 3)]
    assert "inputs" in primary
    assert _all_cids(out) == CIDS


def test_part_audit_row_comes_before_its_success_marker(raw, out, monkeypatch):
    # A stop between the two leaves an audited part without a marker (rewritten
    # and audited again), never a marked part without an audit row.
    _caps(monkeypatch, 4)
    calls: list[tuple[str, str]] = []
    real_mark = mod.mark_success

    def _mark(processed_dir, source, name, stats=None):
        calls.append(("mark", name))
        return real_mark(processed_dir, source, name, stats=stats)

    monkeypatch.setattr(mod, "mark_success", _mark)
    monkeypatch.setattr(mod, "_best_effort_audit", lambda name, n: calls.append(("audit", name)))
    assert mod.serialize_pubchem(raw, out)["ok"] == 1
    expected: list[tuple[str, str]] = []
    for n in (1, 2, 3):
        psf = mod.part_source_file(KEY, n)
        expected += [("audit", psf), ("mark", psf)]
    expected.append(("mark", KEY))
    assert calls == expected
