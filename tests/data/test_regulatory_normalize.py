"""Normalisers and the pure row-preparation step of the reg_record loader."""

import os
from pathlib import Path

import pytest

from episteme.data.regulatory.feeds import Feed, load_feeds
from episteme.data.regulatory.load_reg import (
    parse_date,
    partition_names,
    prepare_rows,
    release_id_for,
)
from episteme.data.regulatory.normalize import (
    norm_application_number,
    norm_lot,
    norm_name,
    norm_ndc,
    norm_ndc_product,
    norm_product_key,
    split_names,
)


def _feed(**kw) -> Feed:
    base = dict(
        name="ds",
        url_key="K",
        format="ema_json",
        jurisdiction="EU",
        record_key=("url",),
        substance_fields=("sub",),
        product_fields=("prod",),
        date_field="date",
        cadence="monthly",
    )
    base.update(kw)
    return Feed(**base)


def test_norm_ndc():
    assert norm_ndc("0093-7214-01") == "00093721401"
    assert norm_ndc("93-7214-01") == "00093721401"
    assert norm_ndc("00093721401") == "00093721401"
    assert norm_ndc("12345-678-90") == "12345067890"
    assert norm_ndc("12345-6789-1") == "12345678901"
    assert norm_ndc("abc") is None
    assert norm_ndc("1234567890") is None  # 10 digits, no hyphens: split unknown


def test_norm_application_number():
    assert norm_application_number("NDA021436") == "021436"
    assert norm_application_number("ANDA 076543") == "076543"
    assert norm_application_number("bla125514") == "125514"
    assert norm_application_number("021436") == "021436"
    assert norm_application_number("BN123") is None
    assert norm_application_number("") is None


def test_norm_lot():
    assert norm_lot(" ab-12 3x ") == "AB123X"
    assert norm_lot("000123") == "000123"
    assert norm_lot("--") is None


def test_norm_name_keeps_salts_distinct():
    assert norm_name("Metformin HCl ") != norm_name("Metformin")
    assert norm_name("  Metformin   HCl ") == "metformin hcl"


def test_split_names_on_article57_separator():
    assert split_names("A | B;C ||  ") == ["A", "B", "C"]
    assert split_names("single") == ["single"]
    assert split_names("") == []


def test_norm_ndc_product_is_9_digits_from_any_ndc_form():
    assert norm_ndc_product("12345-678") == "123450678"
    assert norm_ndc_product("0093-7214") == "000937214"
    assert norm_ndc_product("0093-7214-01") == "000937214"  # the package segment goes
    assert norm_ndc_product("00093721401") == "000937214"
    assert norm_ndc_product("000937214") == "000937214"
    assert norm_ndc_product("abc") is None
    assert norm_ndc_product("1234567890") is None


def test_label_ndc_and_fda_ndc_product_keys_join():
    # A label's NDC product (DailyMed sidecar) and fda_ndc's product_ndc give one key.
    assert norm_ndc_product("0093-7214") == norm_product_key("0093-7214")


def test_norm_product_key():
    assert norm_product_key("0093-7214-01") == "00093721401"
    assert norm_product_key("0093-7214") == "000937214"
    assert norm_product_key("NDA021436") == "021436"
    assert norm_product_key(" EMEA/H/C/000123 ") == "EMEA/H/C/000123"
    assert norm_product_key("  ") is None


def test_record_key_hash_for_keyless_dataset_is_stable_and_drops_duplicates():
    feed = _feed(record_key=())
    rows = [{"a": 1, "b": "x"}, {"b": "x", "a": 1}, {"a": 2, "b": "x"}]
    out1, stats1 = prepare_rows(feed, rows, "f.xlsx")
    out2, _ = prepare_rows(feed, list(rows), "f.xlsx")
    keys1 = [r["record_key"] for r in out1]
    assert len(keys1) == 2 and len(set(keys1)) == 2
    assert stats1["duplicates"] == 1  # key order inside a row does not matter
    assert keys1 == [r["record_key"] for r in out2]  # stable across runs


def test_keyed_exact_duplicates_are_dropped_and_counted():
    feed = _feed()
    rows = [{"url": "u1", "x": 1}, {"url": "u1", "x": 1}, {"url": "u2", "x": 1}]
    out, stats = prepare_rows(feed, rows, "f.json")
    assert [r["record_key"] for r in out] == ["u1", "u2"]
    assert stats["duplicates"] == 1 and stats["conflicts"] == 0


def test_keyed_non_exact_repeat_is_counted_and_both_rows_flow():
    feed = _feed()
    rows = [{"url": "u1", "x": 1}, {"url": "u1", "x": 2}]
    out, stats = prepare_rows(feed, rows, "f.json")
    assert [r["payload"]["x"] for r in out] == [1, 2]  # the loader keeps the last
    assert stats["conflicts"] == 1 and stats["duplicates"] == 0


def test_row_fields_are_normalised():
    feed = _feed(record_key=("id",), substance_fields=("subs.name", "alt"), product_fields=("p",))
    row = {
        "id": 7,
        "subs": [{"name": "Metformin HCl"}, {"name": "Aspirin"}],
        "alt": "Aspirin | Caffeine",
        "p": ["0093-7214-01", "NDA021436"],
        "date": "2025-03-04T00:00:00",
    }
    (out,), stats = prepare_rows(feed, [row], "f.json")
    assert out["record_key"] == "7"
    assert out["substance_names"] == ["metformin hcl", "aspirin", "caffeine"]
    assert out["product_keys"] == ["00093721401", "021436"]
    assert str(out["event_date"]) == "2025-03-04"
    assert out["source_file"] == "f.json"
    assert stats["missing_key"] == 0


def test_missing_key_falls_back_to_hash_and_is_counted():
    (out,), stats = prepare_rows(_feed(), [{"x": 1}], "f.json")
    assert out["record_key"].startswith("h:")
    assert stats["missing_key"] == 1


@pytest.mark.parametrize(
    ("raw", "iso"),
    [("20250304", "2025-03-04"), ("04/03/2025", "2025-03-04"), ("2025-03", None), ("n/a", None)],
)
def test_dates(raw, iso):
    (out,), _ = prepare_rows(_feed(), [{"url": "u", "date": raw}], "f.json")
    assert (str(out["event_date"]) if out["event_date"] else None) == iso


@pytest.mark.parametrize(
    ("raw", "order", "iso"),
    [
        ("09/20/2023", "mdy", "2023-09-20"),
        ("03/10/2026", "mdy", "2026-03-10"),
        ("03/10/2026", "dmy", "2026-10-03"),
        ("09/20/2023", "dmy", None),
        ("20230920", "mdy", "2023-09-20"),
    ],
)
def test_slash_date_order_is_per_feed(raw, order, iso):
    got = parse_date(raw, order)
    assert (str(got) if got else None) == iso
    (out,), stats = prepare_rows(_feed(date_order=order), [{"url": "u", "date": raw}], "f.json")
    assert (str(out["event_date"]) if out["event_date"] else None) == iso
    assert stats["bad_dates"] == (0 if iso else 1)


def test_default_order_is_dmy_and_unknown_order_is_refused():
    assert _feed().date_order == "dmy"
    assert str(parse_date("03/10/2026")) == "2026-10-03"
    with pytest.raises(ValueError):
        parse_date("03/10/2026", "ymd")


def test_fda_shortages_reads_mdy_and_ema_feeds_read_dmy(tmp_path):
    feeds = load_feeds()
    assert feeds["fda_shortages"].date_order == "mdy"
    ema = [f for f in feeds.values() if f.jurisdiction == "EU"]
    assert ema and all(f.date_order == "dmy" for f in ema)
    assert {f.name for f in feeds.values() if f.date_order != "dmy"} == {"fda_shortages"}
    bad = tmp_path / "f.toml"
    bad.write_text(
        '[feeds.x]\nurl_key = "K"\nformat = "ema_json"\njurisdiction = "EU"\n'
        "record_key = []\nsubstance_fields = []\nproduct_fields = []\n"
        'cadence = "monthly"\ndate_order = "ymd"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="date_order"):
        load_feeds(bad)


def test_partition_names_are_safe_unique_and_short():
    ds, leaf = partition_names("ema_medicines", "2026-11")
    assert leaf.startswith(ds) and len(leaf) <= 56 and len(ds) <= 40
    assert all(c.isalnum() or c == "_" for c in leaf)
    evil = 'x"; DROP TABLE episteme.articles; --'
    _, leaf2 = partition_names("ema_medicines", evil)
    assert all(c.isalnum() or c == "_" for c in leaf2) and len(leaf2) <= 56
    # release ids that sanitise alike still get distinct names
    assert partition_names("d", "a-b")[1] != partition_names("d", "a_b")[1]
    assert partition_names("d", "x" * 200)[1] != partition_names("d", "x" * 199)[1]


def test_release_id_follows_cadence(tmp_path: Path):
    f = tmp_path / "a.json"
    f.write_text("{}")
    ts = 1_793_000_000  # 2026-10-26 UTC
    os.utime(f, (ts, ts))
    assert release_id_for(_feed(cadence="monthly"), [f]) == "2026-10"
    assert release_id_for(_feed(cadence="daily"), [f]) == "2026-10-26"
    with pytest.raises(ValueError):
        release_id_for(_feed(cadence="sometimes"), [f])


def test_source_file_follows_input_key_and_a_collision_stops_the_load(tmp_path: Path):
    from episteme.data.regulatory.load_reg import input_keys

    raw = tmp_path / "raw"
    (raw / "a").mkdir(parents=True)
    (raw / "b").mkdir()
    pa, pb = raw / "a" / "x.zip", raw / "b" / "x.zip"
    assert input_keys([pa, pb], raw) == ["a__x.zip", "b__x.zip"]
    # outside the raw dir a key falls back to the bare name: two such files clash
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="share an input_key"):
        input_keys([tmp_path / "x.zip", other / "x.zip"], raw)
