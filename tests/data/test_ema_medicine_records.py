"""Generated EU medicine records (``episteme.data.ema.medicine_records``).

All tests are non-pg: the feeds are passed in as dicts, no database or EMA request is used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from episteme.data import bucket_parts
from episteme.data.ema import medicine_records as mr

from .test_epar import _clear_settings

REL = {"ema_medicines": "m1", "ema_epar_documents": "d1"}


@pytest.fixture(autouse=True)
def _settings_cache():
    _clear_settings()
    yield
    _clear_settings()


def _med(**kw):
    base = {
        "name_of_medicine": "Aspirinex",
        "active_substance": "acetylsalicylic acid",
        "international_non_proprietary_name_common_name": "aspirin",
        "atc_code_human": "B01AC06",
        "therapeutic_area_mesh": "Thrombosis",
        "category": "Human",
        "medicine_status": "Authorised",
        "marketing_authorisation_date": "2015-03-02T00:00:00Z",
        "withdrawal_expiry_revocation_lapse_of_marketing_authorisation_date": "2020-01-31",
        "last_updated_date": "2024-05-06T08:00:00Z",
        "therapeutic_indication": "Prevention of clot formation.",
    }
    base.update(kw)
    return base


def _doc(i, type_, first, name=None, updated="2024-01-01T00:00:00Z"):
    return {
        "id": str(i),
        "name": name or f"Doc {i}",
        "type": type_,
        "ema_product_number": "EMEA/H/C/000001",
        "first_published_date": first,
        "last_updated_date": updated,
    }


def test_template_order_and_omits_empty():
    text = mr.medicine_text(_med(), [_doc(1, "assessment-report", "2015-01-01T00:00:00Z")])
    labels = [ln.split(":")[0] for ln in text.split("\n")]
    assert labels == [
        "Medicine",
        "Active substance",
        "INN",
        "ATC code",
        "Therapeutic area",
        "Category",
        "Status",
        "Marketing authorisation date",
        "Withdrawal, expiry, revocation or lapse date",
        "Last updated",
        "Therapeutic indication",
        "EPAR documents",
        "- Doc 1; type",
    ]
    assert "Marketing authorisation date: 2015-03-02" in text

    sparse = mr.medicine_text(
        {"name_of_medicine": "Only", "atc_code_human": "", "medicine_status": None}, []
    )
    assert sparse == "Medicine: Only"  # no empty field, no documents heading


def test_documents_sorted():
    docs = [
        _doc(3, "rmp-summary", "2019-01-01T00:00:00Z", "R"),
        _doc(2, "assessment-report", "2016-01-01T00:00:00Z", "A2"),
        _doc(1, "assessment-report", "2015-01-01T00:00:00Z", "A1"),
        _doc(4, "product-information", "2014-01-01T00:00:00Z", "P"),
    ]
    text = mr.medicine_text(_med(), docs)
    lines = [ln for ln in text.split("\n") if ln.startswith("- ")]
    assert [ln.split(";")[0] for ln in lines] == ["- A1", "- A2", "- P", "- R"]
    assert (
        "type: assessment-report; first published: 2015-01-01; last updated: 2024-01-01"
        in (lines[0])
    )


def test_article_id_prefix():
    row = mr.row_from_medicine("EMEA/H/C/000001", _med(), [])
    assert row["id"] == "ema_medicine:EMEA/H/C/000001"
    assert not row["id"].startswith("ema_epar:")
    assert row["source"] == "ema_epar" and row["source_record_id"] == "EMEA/H/C/000001"
    assert row["license"] == "permissive" and row["extract_status"] == "ok"
    assert row["year"] == 2015 and row["title"] == "Aspirinex"


def _run(tmp_path: Path, rel=REL, meds=None, **kw):
    meds = meds if meds is not None else {"EMEA/H/C/000001": _med(), "EMEA/H/C/000002": _med()}
    docs = {"EMEA/H/C/000001": [_doc(1, "assessment-report", "2015-01-01T00:00:00Z")]}
    return mr.write_records(tmp_path, rel, meds, docs, run_id="r1", **kw)


def test_parts_sidecar_and_lineage(tmp_path):
    res = _run(tmp_path)
    assert res["skipped"] is False and res["n_rows"] == 2 and res["releases"] == REL
    shard = tmp_path / "staging" / "ema_epar" / "ema_epar_medicines__part00001.parquet"
    assert sorted(pq.read_table(shard, columns=["id"]).column("id").to_pylist()) == [
        "ema_medicine:EMEA/H/C/000001",
        "ema_medicine:EMEA/H/C/000002",
    ]
    assert (
        tmp_path / "staging" / "ema_epar_labels" / "ema_epar_medicines__part00001.labels.parquet"
    ).is_file()
    marker = bucket_parts.read_bucket_marker(tmp_path, "ema_epar", mr.BUCKET_KEY)
    assert marker["lineage"]["releases"] == REL
    run = json.loads((tmp_path / "_ops/ema_epar/runs/medicine_records_r1.json").read_text("utf-8"))
    assert run["releases"] == REL


def test_rerun_same_releases_noop(tmp_path):
    _run(tmp_path)
    shard = tmp_path / "staging" / "ema_epar" / "ema_epar_medicines__part00001.parquet"
    before = shard.stat().st_mtime_ns
    again = _run(tmp_path)
    assert again["skipped"] is True and again["n_rows"] == 2
    assert shard.stat().st_mtime_ns == before
    # another release of one feed writes again
    changed = _run(tmp_path, rel={**REL, "ema_epar_documents": "d2"})
    assert changed["skipped"] is False
    assert _run(tmp_path, rel={**REL, "ema_epar_documents": "d2"}, force=True)["skipped"] is False


def test_checkpoint_key_collision():
    pairs = [
        ("m1", "d1"),
        ("m1", "d2"),
        ("m2", "d1"),
        ("d1", "m1"),  # the same ids, swapped between the feeds
        ("m1d", "1"),
        ("m", "1d1"),  # the same joined text, split elsewhere
    ]
    keys = [tuple(mr.inputs_key({"ema_medicines": m, "ema_epar_documents": d})) for m, d in pairs]
    assert len(set(keys)) == len(pairs)


def test_build_records_reads_feeds(tmp_path, monkeypatch):
    meds = {"EMEA/H/C/000001": _med()}
    monkeypatch.setattr(mr, "read_feeds", lambda conn, release_id=None: (REL, meds, {}))
    res = mr.build_records(object(), tmp_path)
    assert res["n_rows"] == 1 and res["skipped"] is False
