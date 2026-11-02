"""Feed config and parsers for the regulatory sources (no network, no database)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from dataclasses import FrozenInstanceError
from pathlib import Path

import openpyxl
import pytest

from episteme.data.regulatory import parsers
from episteme.data.regulatory.feeds import Feed, load_feeds
from episteme.data.regulatory.parsers import ema_total, parse

REPO = Path(__file__).resolve().parents[2]
SOURCES_ENV = REPO / "scripts" / "data" / "_lib" / "sources.env"
FEEDS_TOML = REPO / "src" / "episteme" / "data" / "regulatory" / "feeds.toml"
OPENFDA_SH = REPO / "scripts" / "data" / "openfda" / "download_openfda.sh"

EXPECTED = {
    "ema_medicines",
    "ema_post_authorisation",
    "ema_orphan",
    "ema_pip",
    "ema_psusa",
    "ema_referrals",
    "ema_dhpc",
    "ema_shortages",
    "ema_mrl",
    "ema_herbal",
    "ema_outside_eu",
    "ema_article57",
    "ema_epar_documents",
    "fda_enforcement",
    "fda_ndc",
    "fda_drugsfda",
    "fda_orangebook",
    "fda_shortages",
}


def _env_keys() -> set[str]:
    keys = set()
    for raw in SOURCES_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.partition("=")[0].strip())
    return keys


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def test_every_expected_feed_is_configured():
    assert set(load_feeds()) == EXPECTED


def test_every_feed_has_a_url_key_in_sources_env():
    keys = _env_keys()
    for name, feed in load_feeds().items():
        assert feed.name == name
        assert feed.url_key in keys, f"{name}: {feed.url_key} missing from sources.env"


def test_feeds_toml_holds_no_urls():
    import tomllib

    data = tomllib.loads(FEEDS_TOML.read_text(encoding="utf-8"))
    for value in _strings(data):
        assert "://" not in value


def test_feed_is_frozen_and_fields_are_tuples():
    feed = load_feeds()["ema_orphan"]
    assert isinstance(feed.record_key, tuple)
    assert isinstance(feed.substance_fields, tuple)
    assert isinstance(feed.product_fields, tuple)
    with pytest.raises(FrozenInstanceError):
        feed.name = "x"  # type: ignore[misc]


def test_every_feed_format_has_a_parser():
    for feed in load_feeds().values():
        assert feed.format in parsers.PARSERS, feed.name


def test_load_feeds_reads_a_given_path_and_rejects_a_missing_field(tmp_path):
    good = tmp_path / "f.toml"
    good.write_text(
        '[feeds.x]\nurl_key = "K"\nformat = "ema_json"\njurisdiction = "EU"\n'
        'record_key = ["a"]\nsubstance_fields = []\nproduct_fields = []\ncadence = "monthly"\n',
        encoding="utf-8",
    )
    feed = load_feeds(good)["x"]
    assert feed == Feed(
        name="x",
        url_key="K",
        format="ema_json",
        jurisdiction="EU",
        record_key=("a",),
        substance_fields=(),
        product_fields=(),
        date_field=None,
        cadence="monthly",
    )
    bad = tmp_path / "bad.toml"
    bad.write_text('[feeds.x]\nurl_key = "K"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="x"):
        load_feeds(bad)


def _feed(fmt: str) -> Feed:
    return Feed("t", "K", fmt, "EU", (), (), (), None, "monthly")


def test_ema_json_parser_reads_data_rows_and_checks_total(tmp_path):
    path = tmp_path / "f.json"
    rows = [{"a": "1"}, {"a": "2", "b": 2.5}, {"a": "3"}]
    path.write_text(json.dumps({"meta": {"total_records": 3, "timestamp": "t"}, "data": rows}))
    got = list(parse(_feed("ema_json"), path))
    assert got == rows
    assert ema_total(path) == 3
    assert ema_total(path) == len(got)


def test_ema_json_parser_raises_on_a_truncated_file(tmp_path):
    path = tmp_path / "f.json"
    path.write_text('{"meta": {"total_records": 9}, "data": [{"a": "1"}, {"a": "2')
    with pytest.raises(Exception) as err:
        list(parse(_feed("ema_json"), path))
    assert "ncomplete" in type(err.value).__name__ or "ncomplete" in str(err.value)


def _article57_workbook(path: Path, header_row: int) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.cell(row=2, column=1, value="Data as present in the Article 57 database on 09/10/2026")
    ws.cell(row=3, column=1, value="EMA/518502/2018, Rev.90")
    headers = [
        "Product name\n\nProduct short name: brand name or the combination",
        'Active substance\n\n\nThe symbol "|" is used to separate',
        "Route of administration",
    ]
    for col, text in enumerate(headers, start=1):
        ws.cell(row=header_row, column=col, value=text)
    for col, text in enumerate(["A", "x | y", "Oral use"], start=1):
        ws.cell(row=header_row + 1, column=col, value=text)
    for col, text in enumerate(["B", "z", None], start=1):
        ws.cell(row=header_row + 3, column=col, value=text)  # a blank row sits between
    wb.save(path)


def test_article57_parser_finds_the_header_row_and_streams(tmp_path, monkeypatch):
    path = tmp_path / "art57.xlsx"
    _article57_workbook(path, header_row=7)
    seen = {}
    real = openpyxl.load_workbook

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(parsers.openpyxl, "load_workbook", spy)
    got = list(parse(_feed("article57_xlsx"), path))
    assert seen.get("read_only") is True
    assert got == [
        {"product_name": "A", "active_substance": "x | y", "route_of_administration": "Oral use"},
        {"product_name": "B", "active_substance": "z", "route_of_administration": None},
    ]


def test_article57_parser_follows_a_header_that_moves(tmp_path):
    path = tmp_path / "art57.xlsx"
    _article57_workbook(path, header_row=20)
    got = list(parse(_feed("article57_xlsx"), path))
    assert [r["product_name"] for r in got] == ["A", "B"]


def test_article57_parser_without_a_header_fails(tmp_path):
    path = tmp_path / "art57.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["only a title"])
    wb.save(path)
    with pytest.raises(ValueError, match="header"):
        list(parse(_feed("article57_xlsx"), path))


def test_openfda_json_parser_reads_results_from_the_zip(tmp_path):
    path = tmp_path / "drug-enforcement-0001-of-0001.json.zip"
    payload = {
        "meta": {"results": {"total": 2}},
        "results": [{"recall_number": "D-1", "n": 1.5}, {"recall_number": "D-2", "n": 2}],
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("drug-enforcement-0001-of-0001.json", json.dumps(payload))
    got = list(parse(_feed("openfda_json"), path))
    assert got == payload["results"]
    assert isinstance(got[0]["n"], float)


def test_parse_rejects_an_unknown_format(tmp_path):
    with pytest.raises(ValueError, match="nope"):
        list(parse(_feed("nope"), tmp_path / "x"))


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    found = shutil.which("bash")
    if found:
        return found
    pytest.skip("no POSIX bash")


def _dry_run_urls(tmp_path: Path, mode: str) -> list[str]:
    catalog = {
        "results": {
            "drug": {
                "label": {"partitions": [{"file": "https://x.test/drug/label/a.json.zip"}]},
                "ndc": {"partitions": [{"file": "https://x.test/drug/ndc/b.json.zip"}]},
            },
            "other": {
                "substance": {
                    "partitions": [{"file": "https://x.test/other/substance/s.json.zip"}]
                },
                "unii": {"partitions": [{"file": "https://x.test/other/unii/u.json.zip"}]},
                "nsde": {"partitions": [{"file": "https://x.test/other/nsde/n.json.zip"}]},
            },
            "device": {"event": {"partitions": [{"file": "https://x.test/device/e.json.zip"}]}},
        }
    }
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps(catalog), encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "EPISTEME_ACTOR": "test",
        "EPISTEME_RAW_ROOT": str(tmp_path / "raw"),
        "OPENFDA_CATALOG": cat.as_uri(),
    }
    proc = subprocess.run(
        [_bash(), str(OPENFDA_SH), "--dry-run", mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return [
        ln.split("would fetch ", 1)[1].split(" -> ", 1)[0]
        for ln in proc.stderr.splitlines()
        if "would fetch " in ln
    ]


def test_substance_mode_selects_only_other_substance(tmp_path):
    assert _dry_run_urls(tmp_path, "substance") == ["https://x.test/other/substance/s.json.zip"]


def test_all_human_mode_is_unchanged_and_excludes_substance(tmp_path):
    urls = _dry_run_urls(tmp_path, "all_human")
    assert sorted(urls) == [
        "https://x.test/drug/label/a.json.zip",
        "https://x.test/drug/ndc/b.json.zip",
    ]
