"""DailyMed extraction: zips of zips, newest version per setId, buckets, parts, sidecars."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from episteme.data import parts_retire
from episteme.data.checkpoint_markers import (
    failed_marker_path,
    load_success_marker_path,
    success_marker_path,
)
from episteme.data.dailymed import extract_dailymed as dm
from episteme.data.dailymed import labels, spl

from ._dailymed_fixtures import (
    ingredient,
    inner_zip,
    outer_zip,
    section,
    set_id,
    spl_xml,
)

BIG = 10**12


@pytest.fixture(autouse=True)
def _settings_cache():
    from episteme.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _share(monkeypatch, value: float) -> None:
    from episteme.config import get_settings

    monkeypatch.setenv("EPISTEME_DAILYMED_MAX_WITHDRAWN_SHARE", str(value))
    get_settings.cache_clear()


@pytest.fixture
def raw(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "out"


def _inner(sid: str, version: int = 1, **kw) -> bytes:
    return inner_zip(spl_xml(sid, version, **kw))


def _name(kind: str, sid: str, day: str = "20240101") -> str:
    return f"{kind}/{day}_{sid}.zip"


def _caps(monkeypatch, rows: int, nbytes: int = BIG) -> None:
    monkeypatch.setattr(dm, "_part_caps", lambda: (rows, nbytes))


def _shards(out: Path) -> list[str]:
    return sorted(p.name.split(".")[0] for p in (out / "staging" / "dailymed").glob("*.parquet"))


def _rows(out: Path, part: str) -> list[dict]:
    return pq.read_table(out / "staging" / "dailymed" / f"{part}.parquet").to_pylist()


def _side(out: Path, part: str) -> list[dict]:
    return pq.read_table(labels.sidecar_path(out, part)).to_pylist()


def _bucket_marker(out: Path, key: str) -> dict:
    p = success_marker_path(out, "dailymed", key)
    return json.loads(p.read_text(encoding="utf-8"))["stats"]


S_AB, S_AB2, S_0F = set_id("ab", 1), set_id("ab", 2), set_id("0f", 3)


def test_the_scan_reads_zips_of_zips(raw):
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {_name("prescription", S_AB): _inner(S_AB), _name("prescription", S_0F): _inner(S_0F)},
    )
    scan = dm.scan_zips(raw, ("prescription", "otc"))
    assert set(scan.winners) == {S_AB, S_0F}
    assert scan.outer_rels == ["dm_spl_release_human_rx_part1.zip"]
    assert scan.n_failed == 0 and not scan.unreadable_outers
    version, oi, member, _crc, _size = scan.winners[S_AB]
    assert (version, oi) == (1, 0) and member == _name("prescription", S_AB)


def test_newest_version_per_set_id_wins(raw, out):
    # Version 1 in the release, version 2 in an update; the name of the inner zip is
    # not the setId (it is a different id), so the XML decides.
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {
            "prescription/20230101_other-id-1.zip": _inner(S_AB, 1, title="OLD"),
            _name("prescription", S_0F): _inner(S_0F, 1),
        },
    )
    outer_zip(
        raw / "dm_spl_weekly_update_20240108.zip",
        {"prescription/20240108_other-id-2.zip": _inner(S_AB, 2, title="NEW")},
    )
    res = dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    assert (res["ok"], res["failed"], res["rows"], res["labels"]) == (2, 0, 2, 2)
    assert res["superseded_versions"] == 1 and res["duplicate_files"] == 0
    row = _rows(out, "dailymed_bab__part00001")[0]
    assert (row["id"], row["source_record_id"], row["title"]) == (
        f"dailymed:{S_AB}",
        f"{S_AB}.2",
        "NEW",
    )
    # An older version in a later zip does not replace a newer one.
    outer_zip(
        raw / "dm_spl_daily_update_20240201.zip",
        {"prescription/x.zip": _inner(S_AB, 1, title="STALE")},
    )
    dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    assert _rows(out, "dailymed_bab__part00001")[0]["source_record_id"] == f"{S_AB}.2"


def test_corrupt_inner_zip_fails_one_label_only(raw, out, monkeypatch):
    no_xml = __import__("io").BytesIO()
    with zipfile.ZipFile(no_xml, "w") as zf:
        zf.writestr("figure.jpg", b"img")
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {
            _name("prescription", S_AB): _inner(S_AB),
            _name("prescription", "corrupt-1"): b"this is not a zip file",
            _name("prescription", "noxml-1"): no_xml.getvalue(),
            _name("prescription", S_AB2): _inner(S_AB2),
        },
    )
    monkeypatch.setenv("EPISTEME_RUN_ID", "run-1")
    from episteme.config import get_settings

    get_settings.cache_clear()
    res = dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    # The bucket completes with the two good labels; the two bad ones are listed.
    assert (res["ok"], res["failed"], res["rows"]) == (1, 0, 2)
    assert res["failed_labels_count"] == 2
    assert len(_rows(out, "dailymed_bab__part00001")) == 2
    key = "dm_spl_release_human_rx_part1.zip__prescription__20240101_corrupt-1.zip"
    assert failed_marker_path(out, "dailymed", key).is_file()
    dm._write_failed_inputs(out, res)
    listed = json.loads((out / "_ops" / "dailymed" / "failed_inputs.json").read_text("utf-8"))
    assert (listed["run_id"], listed["failed_inputs"]) == ("run-1", 2)
    get_settings.cache_clear()


def test_a_label_that_fails_in_the_bucket_is_listed_and_the_bucket_completes(raw, out):
    # The identity reads fine, the body is broken later in the file.
    good = spl_xml(S_AB)
    broken = good.replace(b"</document>", b"<unclosed>")
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {
            _name("prescription", S_AB): inner_zip(broken),
            _name("prescription", S_AB2): _inner(S_AB2),
        },
    )
    res = dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    assert (res["ok"], res["failed"], res["rows"], res["failed_labels_count"]) == (1, 0, 1, 1)
    assert _bucket_marker(out, "dailymed_bab")["failed_labels_count"] == 1


def test_images_in_inner_zip_are_skipped(raw, out, monkeypatch):
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {_name("prescription", S_AB): _inner(S_AB)},
    )
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def spy(self, name, *a, **k):
        opened.append(name if isinstance(name, str) else name.filename)
        return real_open(self, name, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    res = dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    assert res["rows"] == 1
    assert any(n.endswith(".xml") for n in opened)
    assert not any(n.endswith(".jpg") for n in opened)  # no image member was opened
    assert "not a real image" not in _rows(out, "dailymed_bab__part00001")[0]["body_text"]


def test_identity_collision(raw, out):
    # Two inner paths with one setId and one version: one row, one duplicate.
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {
            "prescription/20240101_aaa.zip": _inner(S_AB, 1),
            "prescription/20240101_bbb.zip": _inner(S_AB, 1),
        },
    )
    res = dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    assert (res["rows"], res["duplicate_files"], res["superseded_versions"]) == (1, 1, 0)
    assert len(_rows(out, "dailymed_bab__part00001")) == 1
    assert _bucket_marker(out, "dailymed_bab")["duplicate_files"] == 1


def test_label_keys_differ_for_the_same_name_in_two_directories(raw):
    a, b = raw / "r1" / "x.zip", raw / "r2" / "x.zip"
    ka = dm.label_key(a, raw, "prescription/1.zip")
    kb = dm.label_key(b, raw, "prescription/1.zip")
    assert ka != kb
    assert ka == "r1__x.zip__prescription__1.zip"
    assert dm.label_key(raw / "x.zip", raw, "otc/2.zip") == "x.zip__otc__2.zip"


def test_buckets_are_the_first_two_hex_characters(raw, out):
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {_name("prescription", s): _inner(s) for s in (S_AB, S_AB2, S_0F)},
    )
    res = dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    assert (res["buckets"], res["rows"]) == (2, 3)
    assert _shards(out) == ["dailymed_b0f__part00001", "dailymed_bab__part00001"]
    st = _bucket_marker(out, "dailymed_bab")
    assert st["parts_list"] == ["dailymed_bab__part00001"] and st["n_rows"] == 2
    assert {r["source_file"] for r in _rows(out, "dailymed_bab__part00001")} == {
        "dailymed_bab__part00001"
    }
    assert success_marker_path(
        out, "dailymed", "dailymed_b0f__part00001"
    ).is_file()  # a marker per part
    assert (out / "_ops" / "dailymed" / "buckets" / "dailymed_bab.json").is_file()


def test_parts_stop_at_the_row_cap_and_each_has_a_sidecar(raw, out, monkeypatch):
    sids = [set_id("ab", n) for n in range(1, 6)]
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {_name("prescription", s): _inner(s) for s in sids},
    )
    _caps(monkeypatch, 2)
    res = dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    assert res["rows"] == 5
    parts = [f"dailymed_bab__part0000{i}" for i in (1, 2, 3)]
    assert _shards(out) == parts
    assert [len(_rows(out, p)) for p in parts] == [2, 2, 1]
    for p in parts:
        ids = {r["id"] for r in _rows(out, p)}
        assert {s["article_id"] for s in _side(out, p)} == ids  # a sidecar row set per part
    # The sidecar sits beside the shard directory, not in it: the shard globs stay clean.
    assert not list((out / "staging" / "dailymed").glob("*labels*"))


def test_sidecar_offsets_point_into_the_stored_body_text(raw, out):
    sections = [
        section("34067-9", "1 INDICATIONS", ["Used for testing."]),
        section(
            "34073-7",
            "7 DRUG INTERACTIONS",
            ["Avoid warfarin."],
            children=[section("42229-5", "7.1 Detail", ["Nested é ü."])],
        ),
    ]
    ings = [
        ingredient("ACTIB", "AAAAAAAAAA", "SALT", moiety="BBBBBBBBBB"),
        ingredient("IACT", "CCCCCCCCCC", "INERT"),
    ]
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {
            _name("prescription", S_AB): inner_zip(
                spl_xml(S_AB, sections=sections, ingredients=ings, ndcs=("11111-222-01",))
            )
        },
    )
    dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    part = "dailymed_bab__part00001"
    row = _rows(out, part)[0]
    side = _side(out, part)
    secs = [s for s in side if s["kind"] == "section"]
    assert {s["loinc_code"] for s in secs} == {"34067-9", "34073-7", "42229-5"}
    body = row["body_text"]  # as stored, after finalize_row
    parsed = spl.parse_spl(
        spl_xml(S_AB, sections=sections, ingredients=ings, ndcs=("11111-222-01",))
    )
    assert body == parsed["body_text"]
    expected = {s["loinc"]: s["text"] for s in parsed["sections"]}
    for s in secs:
        assert body[s["start_offset"] : s["end_offset"]] == expected[s["loinc_code"]]
        assert s["article_id"] == row["id"]
    subs = [s for s in side if s["kind"] == "substance"]
    assert {(s["unii"], s["unii_role"], s["method"]) for s in subs if s["unii"]} == {
        ("AAAAAAAAAA", "active_ingredient", "xml"),
        ("BBBBBBBBBB", "active_moiety", "xml"),
    }
    assert [s["ndc_product"] for s in subs if s["ndc_product"]] == ["111110222"]  # 9 digits


def test_the_row_follows_the_contract(raw, out):
    outer_zip(
        raw / "dm_spl_release_human_otc_part1.zip",
        {
            _name("otc", S_AB): _inner(
                S_AB,
                4,
                doc_code="34390-5",
                effective="20220301",
                sections=[section("34067-9", "1 USES", ["Relieves the symptoms. " * 12])],
            )
        },
    )
    dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    row = _rows(out, "dailymed_bab__part00001")[0]
    assert row["source"] == "dailymed" and row["id"] == f"dailymed:{S_AB}"
    assert row["source_record_id"] == f"{S_AB}.4"
    assert row["title"] == "Test Drug Tablets"
    assert (row["license"], row["subset"]) == ("public_domain", "commercial")
    assert row["license_raw"] and row["year"] == 2022
    assert json.loads(row["book_meta"])["doc_type"] == "34390-5"
    assert row["extract_status"] == "ok"
    assert row["abstract"] is None and row["pmid"] is None and row["is_retracted"] is False


def test_only_the_selected_kinds_and_known_files_are_read(raw, out):
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip", {_name("prescription", S_AB): _inner(S_AB)}
    )
    outer_zip(raw / "dm_spl_release_human_otc_part1.zip", {_name("otc", S_0F): _inner(S_0F)})
    # Not read in this cut: if one were opened it would fail as a corrupt zip.
    for junk in (
        "dm_spl_release_animal.zip",
        "dm_spl_release_homeopathic.zip",
        "dm_spl_release_remainder.zip",
        "dm_spl_release_human_rx_part1_indexing_spl_files.zip",
        "dm_spl_monthly_update_202401_indexing_spl_files.zip",
        "notes.zip",
    ):
        (raw / junk).write_bytes(b"not a zip")
    # An update holds other label kinds too; the inner prefix filters them.
    outer_zip(
        raw / "dm_spl_monthly_update_202401.zip",
        {
            _name("prescription", set_id("cd", 1)): _inner(set_id("cd", 1)),
            "animal/20240101_zzz.zip": b"not a zip",
            "other/20240101_zzz.zip": b"not a zip",
        },
    )
    res = dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    assert (res["rows"], res["failed_labels_count"]) == (3, 0)
    # Only the OTC kind: the Rx release and the Rx labels of the update are not read.
    out2 = out.parent / "out2"
    res2 = dm.extract_dailymed(raw, out2, release_kinds=("otc",))
    assert (res2["rows"], res2["failed_labels_count"]) == (1, 0)
    assert _shards(out2) == ["dailymed_b0f__part00001"]


def test_a_second_run_skips_done_buckets_and_a_changed_input_rewrites(raw, out, monkeypatch):
    zpath = raw / "dm_spl_release_human_rx_part1.zip"
    outer_zip(
        zpath,
        {_name("prescription", S_AB): _inner(S_AB), _name("prescription", S_0F): _inner(S_0F)},
    )
    kinds = ("prescription", "otc")
    assert dm.extract_dailymed(raw, out, release_kinds=kinds)["rows"] == 2
    again = dm.extract_dailymed(raw, out, release_kinds=kinds)
    assert (again["ok"], again["rows"]) == (0, 0)  # both buckets skipped
    assert dm.extract_dailymed(raw, out, force=True, release_kinds=kinds)["rows"] == 2
    # A new version of one label: only its bucket is written again.
    outer_zip(raw / "dm_spl_weekly_update_20240108.zip", {"prescription/n.zip": _inner(S_AB, 2)})
    res = dm.extract_dailymed(raw, out, release_kinds=kinds)
    assert (res["ok"], res["rows"]) == (1, 1)
    assert _rows(out, "dailymed_bab__part00001")[0]["source_record_id"].endswith(".2")


def test_a_withdrawn_label_is_retired_through_the_pending_file(raw, out, monkeypatch):
    _share(monkeypatch, 1.0)  # one of two labels leaves; the guard is tested below
    kinds = ("prescription", "otc")
    zpath = raw / "dm_spl_release_human_rx_part1.zip"
    outer_zip(
        zpath,
        {_name("prescription", S_AB): _inner(S_AB), _name("prescription", S_0F): _inner(S_0F)},
    )
    dm.extract_dailymed(raw, out, release_kinds=kinds)
    # Both parts were loaded.
    for part in ("dailymed_bab__part00001", "dailymed_b0f__part00001"):
        lp = load_success_marker_path(out, "dailymed", f"{part}.parquet")
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text("{}", encoding="utf-8")
    # The new full release no longer holds the label of bucket 0f.
    outer_zip(zpath, {_name("prescription", S_AB): _inner(S_AB)})
    res = dm.extract_dailymed(raw, out, release_kinds=kinds)
    assert res["withdrawn_labels"] == 1 and res["retired_buckets"] == 1
    assert "dailymed_b0f__part00001" in parts_retire.read_retire_pending(out, "dailymed")
    assert _shards(out) == ["dailymed_bab__part00001"]
    assert not labels.sidecar_path(out, "dailymed_b0f__part00001").exists()
    assert not (out / "_ops" / "dailymed" / "buckets" / "dailymed_b0f.json").exists()
    assert not success_marker_path(out, "dailymed", "dailymed_b0f").is_file()


def test_a_label_only_in_an_update_stays(raw, out):
    kinds = ("prescription", "otc")
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip", {_name("prescription", S_AB): _inner(S_AB)}
    )
    outer_zip(raw / "dm_spl_weekly_update_20240108.zip", {"prescription/n.zip": _inner(S_0F)})
    res = dm.extract_dailymed(raw, out, release_kinds=kinds)
    assert res["rows"] == 2 and res["withdrawn_labels"] == 0
    assert _shards(out) == ["dailymed_b0f__part00001", "dailymed_bab__part00001"]


def test_an_unreadable_outer_zip_stops_before_anything_is_retired(raw, out):
    kinds = ("prescription", "otc")
    zpath = raw / "dm_spl_release_human_rx_part1.zip"
    outer_zip(zpath, {_name("prescription", S_AB): _inner(S_AB)})
    dm.extract_dailymed(raw, out, release_kinds=kinds)
    zpath.write_bytes(b"half a download")
    with pytest.raises(dm.DailymedScanError, match="dm_spl_release_human_rx_part1.zip"):
        dm.extract_dailymed(raw, out, release_kinds=kinds)
    assert _shards(out) == ["dailymed_bab__part00001"]  # nothing was removed
    assert parts_retire.read_retire_pending(out, "dailymed") == []


def test_the_bucket_scheme_is_recorded_and_checked(raw, out, monkeypatch):
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip", {_name("prescription", S_AB): _inner(S_AB)}
    )
    dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    stored = json.loads((out / "_ops" / "dailymed" / "bucket_scheme.json").read_text("utf-8"))
    assert stored == {"prefix_chars": 2}
    monkeypatch.setattr(dm, "BUCKET_PREFIX_CHARS", 3)
    with pytest.raises(RuntimeError, match="prefix"):
        dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))


def test_the_scan_keeps_no_xml(raw):
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip", {_name("prescription", S_AB): _inner(S_AB)}
    )
    scan = dm.scan_zips(raw, ("prescription", "otc"))
    (entry,) = scan.winners.values()
    assert not any(isinstance(v, (bytes, bytearray)) for v in entry)
    assert entry[0] == 1  # version


def test_reads_inside_a_bucket_follow_the_order_of_the_outer_zip(raw, out, monkeypatch):
    sids = [set_id("ab", n) for n in (5, 1, 9, 3)]
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {_name("prescription", s, f"2024010{i}"): _inner(s) for i, s in enumerate(sids)},
    )
    order: list[str] = []
    real = zipfile.ZipFile.open

    def spy(self, name, *a, **k):
        n = name if isinstance(name, str) else name.filename
        if n.startswith("prescription/"):
            order.append(n)
        return real(self, name, *a, **k)

    dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    order.clear()
    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    dm.extract_dailymed(raw, out, force=True, release_kinds=("prescription", "otc"))
    # The scan and then the bucket each read in the order of the file (the dates in the
    # names run with the offsets), and each reads every inner zip once.
    assert len(order) == 8
    assert order[:4] == sorted(order[:4]) and order[4:] == sorted(order[4:])


def test_the_kinds_setting(monkeypatch):
    from episteme.config import ConfigError, get_settings

    get_settings.cache_clear()
    monkeypatch.delenv("EPISTEME_DAILYMED_KINDS", raising=False)
    assert get_settings().dailymed_kinds == ("prescription", "otc")
    monkeypatch.setenv("EPISTEME_DAILYMED_KINDS", " OTC ")
    get_settings.cache_clear()
    assert get_settings().dailymed_kinds == ("otc",)
    monkeypatch.setenv("EPISTEME_DAILYMED_KINDS", "otc,animal")
    get_settings.cache_clear()
    with pytest.raises(ConfigError, match="animal"):
        get_settings()
    monkeypatch.delenv("EPISTEME_DAILYMED_KINDS")
    get_settings.cache_clear()


def test_the_gate_knows_the_dailymed_stages():
    from episteme.ops import gate

    assert dm.FAILED_INPUTS_FILE == gate.FAILED_INPUTS_FILE
    assert gate.stage_module("dailymed", "extract") == (
        "episteme.data.dailymed.extract_dailymed",
        "dailymed/extract_dailymed.sh",
    )
    assert gate.stage_module("dailymed", "load") == (
        "episteme.data.load_articles",
        "dailymed/load_dailymed.sh",
    )
    assert gate.hash_modules("dailymed", "load") == [
        "episteme.data.load_articles",
        "episteme.data.dailymed.retire",
    ]


# ---------------------------------------------------------------------------
# fix round 1
# ---------------------------------------------------------------------------
def _read_bytes(monkeypatch) -> list[int]:
    """Count the bytes that the zip layer reads from the files (the shared file object)."""
    total = [0]
    real = zipfile._SharedFile.read

    def spy(self, n=-1):
        data = real(self, n)
        total[0] += len(data)
        return data

    monkeypatch.setattr(zipfile._SharedFile, "read", spy)
    return total


def test_images_are_not_read_from_a_stored_outer_zip(raw, monkeypatch):
    big = 2_000_000
    inner = inner_zip(spl_xml(S_AB), image_bytes=big)
    outer_zip(raw / "dm_spl_release_human_rx_part1.zip", {_name("prescription", S_AB): inner})
    total = _read_bytes(monkeypatch)
    scan = dm.scan_zips(raw, ("prescription", "otc"))
    assert S_AB in scan.winners
    assert 0 < total[0] < 200_000  # the XML head and the directories, not the 2 MB image


def test_a_compressed_outer_zip_is_read_whole(raw, monkeypatch):
    # A deflated outer member cannot be sought without decompressing it: it is read once.
    big = 300_000
    inner = inner_zip(spl_xml(S_AB), image_bytes=big)
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {_name("prescription", S_AB): inner},
        compression=zipfile.ZIP_DEFLATED,
    )
    total = _read_bytes(monkeypatch)
    scan = dm.scan_zips(raw, ("prescription", "otc"))
    assert S_AB in scan.winners and not scan.n_failed
    assert total[0] >= big


def test_bucket_reads_of_a_stored_outer_zip_skip_the_images(raw, out, monkeypatch):
    inner = inner_zip(spl_xml(S_AB), image_bytes=2_000_000)
    outer_zip(raw / "dm_spl_release_human_rx_part1.zip", {_name("prescription", S_AB): inner})
    total = _read_bytes(monkeypatch)
    res = dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    assert res["rows"] == 1
    assert total[0] < 600_000  # scan + bucket: two XML reads, never the image


def test_a_missing_release_part_refuses_before_anything_is_written(raw, out, monkeypatch):
    kinds = ("prescription", "otc")
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip", {_name("prescription", S_AB): _inner(S_AB)}
    )
    outer_zip(
        raw / "dm_spl_release_human_rx_part3.zip", {_name("prescription", S_0F): _inner(S_0F)}
    )
    with pytest.raises(dm.DailymedGuardError, match=r"prescription: release part\(s\) \[2\]"):
        dm.extract_dailymed(raw, out, release_kinds=kinds)
    assert not (out / "_ops").exists() and not (out / "staging").exists()
    # --force alone does not override; --force --reason does, with an audit row.
    with pytest.raises(dm.DailymedGuardError):
        dm.extract_dailymed(raw, out, force=True, release_kinds=kinds)
    audited: list[tuple[str, str]] = []
    monkeypatch.setattr(dm, "_audit_override", lambda reason, what: audited.append((reason, what)))
    res = dm.extract_dailymed(
        raw, out, force=True, force_reason="part 2 is lost", release_kinds=kinds
    )
    assert res["rows"] == 2
    assert audited and audited[0][0] == "part 2 is lost" and "[2]" in audited[0][1]


def test_contiguous_parts_and_update_only_dirs_pass(raw, out):
    kinds = ("prescription", "otc")
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip", {_name("prescription", S_AB): _inner(S_AB)}
    )
    outer_zip(
        raw / "dm_spl_release_human_rx_part2.zip", {_name("prescription", S_0F): _inner(S_0F)}
    )
    outer_zip(
        raw / "dm_spl_release_human_otc_part1.zip",
        {_name("otc", set_id("cd", 1)): _inner(set_id("cd", 1))},
    )
    assert dm.extract_dailymed(raw, out, release_kinds=kinds)["rows"] == 3
    assert dm.release_part_gaps([]) == []


def test_a_sample_root_skips_the_guards(raw, tmp_path):
    outer_zip(
        raw / "dm_spl_release_human_rx_part3.zip", {_name("prescription", S_AB): _inner(S_AB)}
    )
    sample_out = tmp_path / "_sample" / "dailymed" / "02_processed"
    res = dm.extract_dailymed(raw, sample_out, release_kinds=("prescription", "otc"))
    assert res["rows"] == 1


def _ten(raw: Path, n: int) -> None:
    sids = [set_id("ab", i) for i in range(1, n + 1)]
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {_name("prescription", s): _inner(s) for s in sids},
    )


def test_too_many_withdrawn_labels_refuse_and_the_override_works(raw, out, monkeypatch):
    kinds = ("prescription", "otc")
    _ten(raw, 10)
    # No earlier state: no limit.
    assert dm.extract_dailymed(raw, out, release_kinds=kinds)["rows"] == 10
    # One of ten is 10 percent: at the limit, allowed.
    _ten(raw, 9)
    assert dm.extract_dailymed(raw, out, release_kinds=kinds)["withdrawn_labels"] == 1
    # Two of nine are over it: refused, and nothing changes.
    _ten(raw, 7)
    manifest = out / "_ops" / "dailymed" / "buckets" / "dailymed_bab.json"
    before = manifest.read_text(encoding="utf-8")
    with pytest.raises(dm.DailymedGuardError, match="2 of 9 known labels"):
        dm.extract_dailymed(raw, out, release_kinds=kinds)
    assert manifest.read_text(encoding="utf-8") == before
    assert len(_rows(out, "dailymed_bab__part00001")) == 9
    # The override, audited.
    audited: list[tuple[str, str]] = []
    monkeypatch.setattr(dm, "_audit_override", lambda reason, what: audited.append((reason, what)))
    res = dm.extract_dailymed(
        raw, out, force=True, force_reason="two withdrawn", release_kinds=kinds
    )
    assert (res["withdrawn_labels"], res["rows"]) == (2, 7)
    assert audited and "2 of 9" in audited[0][1]


def test_the_share_limit_is_a_setting(monkeypatch):
    from episteme.config import get_settings

    assert get_settings().dailymed_max_withdrawn_share == 0.1
    _share(monkeypatch, 0.25)
    assert get_settings().dailymed_max_withdrawn_share == 0.25


def test_a_known_label_that_cannot_be_read_holds_its_bucket(raw, out):
    kinds = ("prescription", "otc")
    z = raw / "dm_spl_release_human_rx_part1.zip"
    good = {
        _name("prescription", S_AB): _inner(S_AB),
        _name("prescription", S_AB2): _inner(S_AB2),
        _name("prescription", S_0F): _inner(S_0F),
    }
    outer_zip(z, good)
    dm.extract_dailymed(raw, out, release_kinds=kinds)
    # The next release holds a corrupt copy of S_AB and no other copy.
    outer_zip(z, {**good, _name("prescription", S_AB): b"damaged in the download"})
    res = dm.extract_dailymed(raw, out, release_kinds=kinds)
    assert res["held_buckets"] == ["dailymed_bab"] and res["failed"] == 1
    assert res["failed_labels_count"] == 1 and res["withdrawn_labels"] == 0
    # The earlier parts and manifest stay: the row of S_AB is not dropped or retired.
    assert {r["id"] for r in _rows(out, "dailymed_bab__part00001")} == {
        f"dailymed:{S_AB}",
        f"dailymed:{S_AB2}",
    }
    manifest = out / "_ops" / "dailymed" / "buckets" / "dailymed_bab.json"
    assert len(json.loads(manifest.read_text("utf-8"))["labels"]) == 2
    assert parts_retire.read_retire_pending(out, "dailymed") == []
    assert failed_marker_path(out, "dailymed", "dailymed_bab").is_file()
    _write = dm._write_failed_inputs
    _write(out, res)
    assert (
        json.loads((out / "_ops" / "dailymed" / "failed_inputs.json").read_text("utf-8"))[
            "failed_inputs"
        ]
        == 2
    )
    # The other bucket was not held. When the file is good again, the hold is gone.
    outer_zip(z, good)
    res = dm.extract_dailymed(raw, out, release_kinds=kinds)
    assert res["held_buckets"] == [] and res["failed"] == 0
    assert not failed_marker_path(out, "dailymed", "dailymed_bab").is_file()


def test_a_corrupt_newest_copy_does_not_downgrade_the_row(raw, out):
    kinds = ("prescription", "otc")
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip", {_name("prescription", S_AB): _inner(S_AB, 1)}
    )
    upd = raw / "dm_spl_weekly_update_20240108.zip"
    outer_zip(upd, {"prescription/new.zip": _inner(S_AB, 2)})
    dm.extract_dailymed(raw, out, release_kinds=kinds)
    outer_zip(upd, {"prescription/new.zip": b"damaged"})
    res = dm.extract_dailymed(raw, out, release_kinds=kinds)
    assert res["held_buckets"] == ["dailymed_bab"]
    assert _rows(out, "dailymed_bab__part00001")[0]["source_record_id"].endswith(".2")


def test_a_label_with_no_text_is_empty_and_one_with_text_is_ok(raw, out):
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {
            _name("prescription", S_AB): _inner(S_AB, sections=[]),
            _name("prescription", S_AB2): _inner(S_AB2),
        },
    )
    res = dm.extract_dailymed(raw, out, release_kinds=("prescription", "otc"))
    by_id = {r["id"]: r for r in _rows(out, "dailymed_bab__part00001")}
    assert by_id[f"dailymed:{S_AB}"]["extract_status"] == "empty"
    assert by_id[f"dailymed:{S_AB2}"]["extract_status"] == "ok"
    st = _bucket_marker(out, "dailymed_bab")
    assert st["extract_status_counts"] == {"empty": 1, "ok": 1}
    assert res["rows"] == 2


def test_iter_labels_is_gone():
    assert not hasattr(dm, "iter_labels")
