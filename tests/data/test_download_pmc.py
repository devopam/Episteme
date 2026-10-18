"""Network-free smoke tests for the pure helpers in episteme.data.pmc.download_pmc.

Replaces the deleted tests/test_pmc_download.py. Assertions are checked against
the module source: s3_to_http() strips the s3:// prefix and any ?query, then
prefixes S3_HTTP; is_commercial_meta() reads meta["license_code"], normalises
spacing/underscores, and accepts CC0 / CC BY / CC BY-SA / CC BY-ND (rejecting
anything containing "NC").
"""

import csv
import gzip
import io
import sys

import pytest

from episteme.data.pmc import download_pmc as dp
from episteme.data.pmc.download_pmc import (
    COMMERCIAL_LICENSE_CODES,
    is_commercial_meta,
    s3_to_http,
)


def test_s3_to_http_strips_scheme_and_query():
    url = "s3://pmc-oa-opendata/PMC12345.1/PMC12345.1.xml?md5=abc"
    assert s3_to_http(url) == "https://pmc-oa-opendata.s3.amazonaws.com/PMC12345.1/PMC12345.1.xml"


def test_is_commercial_meta_accepts_cc_by():
    assert "CC BY" in COMMERCIAL_LICENSE_CODES
    assert is_commercial_meta({"license_code": "CC BY"}) is True
    assert is_commercial_meta({"license_code": "CC BY 4.0"}) is True


def test_is_commercial_meta_rejects_noncommercial():
    assert is_commercial_meta({"license_code": "CC BY-NC"}) is False


def test_is_commercial_meta_rejects_missing_license():
    assert is_commercial_meta({}) is False


def test_output_dir_defaults_to_configured_raw_root(tmp_path, monkeypatch):
    # EPISTEME_RAW_ROOT overrides EPISTEME_DATA_ROOT in Settings' own resolution
    # order (config.py: raw_root = EPISTEME_RAW_ROOT or data_root / "01_raw") --
    # an operator .env that happens to set it would silently make this assertion
    # depend on the machine it runs on, not just tmp_path. delenv alone is not
    # enough: get_settings() calls load_dotenv() on cache-miss with the default
    # override=False, which *fills in* any var missing from os.environ straight
    # back out of that same .env -- undoing the delenv. Block dotenv from being
    # read at all instead, same pattern as tests/test_db_guard.py's
    # clean_settings fixture.
    monkeypatch.setattr("episteme.config._find_project_dotenv", lambda: None)
    monkeypatch.delenv("EPISTEME_RAW_ROOT", raising=False)
    monkeypatch.setenv("EPISTEME_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    import episteme.config as cfg

    cfg.get_settings.cache_clear()
    try:
        # No importlib.reload needed: build_parser() calls get_settings() at
        # call time (download_pmc.py line ~339), not as a module-level
        # argparse default frozen at import time, so a plain cache_clear()
        # already makes it see the monkeypatched settings.
        from episteme.data.pmc import download_pmc

        parser = download_pmc.build_parser()
        args = parser.parse_args([])
        assert args.output_dir == tmp_path / "01_raw" / "pmc" / "oa_comm"
    finally:
        # Otherwise the next test to call get_settings() (without its own
        # monkeypatch) inherits this test's tmp_path-scoped Settings from cache.
        cfg.get_settings.cache_clear()


# --- bucket inventory (post 2026-08-24 layout) --------------------------------

_LISTING = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult><Prefix>inventory-reports/pmc-oa-opendata/metadata/</Prefix>
<CommonPrefixes><Prefix>inventory-reports/pmc-oa-opendata/metadata/2026-10-16T01-00Z/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>inventory-reports/pmc-oa-opendata/metadata/2026-10-17T01-00Z/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>inventory-reports/pmc-oa-opendata/metadata/data/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>inventory-reports/pmc-oa-opendata/metadata/hive/</Prefix></CommonPrefixes>
</ListBucketResult>"""

_ROWS = [
    ["pmc-oa-opendata", "metadata/PMC100.1.json", "2026-01-08T11:47:59.000Z", "e1"],
    ["pmc-oa-opendata", "metadata/PMC100.2.json", "2026-01-08T11:47:59.000Z", "e2"],
    ["pmc-oa-opendata", "metadata/PMC20.1.json", "2026-01-08T11:47:59.000Z", "e3"],
    ["pmc-oa-opendata", "metadata/README.txt", "2026-01-08T11:47:59.000Z", "e4"],
    ["pmc-oa-opendata"],
]


class _Resp:
    def __init__(self, text="", payload=None):
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload


def _gz_csv(rows) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, quoting=csv.QUOTE_ALL).writerows(rows)
    return gzip.compress(buf.getvalue().encode("utf-8"))


def test_latest_inventory_date_picks_newest_dated_folder():
    assert dp.latest_inventory_date(_LISTING) == "2026-10-17T01-00Z"
    assert dp.latest_inventory_date("<ListBucketResult></ListBucketResult>") is None


def test_inventory_versions_maps_pmcid_to_versions_highest_first():
    assert dp.inventory_versions(_ROWS) == {"PMC100": [2, 1], "PMC20": [1]}


def test_fetch_inventory_reads_the_newest_manifest(tmp_path, monkeypatch):
    data_key = "inventory-reports/pmc-oa-opendata/metadata/data/abc.csv.gz"
    gets: list[str] = []

    def fake_get(url, **kw):
        gets.append(url)
        if "list-type=2" in url:
            return _Resp(text=_LISTING)
        if url.endswith("2026-10-17T01-00Z/manifest.json"):
            return _Resp(payload={"files": [{"key": data_key}]})
        return None

    def fake_download(url, out_path, dry_run=False, retries=3):
        assert url.endswith(data_key)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_gz_csv(_ROWS))
        return True

    monkeypatch.setattr(dp, "http_get", fake_get)
    monkeypatch.setattr(dp, "download_to_file", fake_download)
    date, versions = dp.fetch_inventory(tmp_path)
    assert date == "2026-10-17T01-00Z"
    assert versions == {"PMC100": [2, 1], "PMC20": [1]}
    assert (tmp_path / date / "abc.csv.gz").is_file()


def test_fetch_inventory_returns_none_without_a_dated_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "http_get", lambda url, **kw: _Resp(text="<ListBucketResult/>"))
    assert dp.fetch_inventory(tmp_path) is None


def _meta(vid, code):
    return {
        "license_code": code,
        "xml_url": f"s3://pmc-oa-opendata/{vid}/{vid}.xml?md5=x",
    }


def _fake_store(monkeypatch, codes):
    """Patch metadata reads to ``codes`` ({version_id: license_code}) and file
    downloads to a stub that writes the file; return the list of metadata reads."""
    reads: list[str] = []

    def fake_load(vid):
        reads.append(vid)
        return _meta(vid, codes[vid]) if vid in codes else None

    def fake_download(url, out_path, dry_run=False, retries=3):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("<article/>", encoding="utf-8")
        return True

    monkeypatch.setattr(dp, "load_metadata", fake_load)
    monkeypatch.setattr(dp, "download_to_file", fake_download)
    return reads


def test_process_one_keeps_the_highest_commercial_version(tmp_path, monkeypatch):
    _fake_store(monkeypatch, {"PMC100.2": "CC BY-NC", "PMC100.1": "CC BY"})
    assert dp.process_one("PMC100", tmp_path, ["xml"], False, [2, 1]) == ("PMC100", True, None)
    assert (tmp_path / "metadata" / "PMC100.1.json").is_file()
    assert (tmp_path / "xml" / "PMC100.1.xml").is_file()
    assert not (tmp_path / "metadata" / "PMC100.2.json").exists()


def test_process_one_reports_non_commercial_when_no_version_is(tmp_path, monkeypatch):
    _fake_store(monkeypatch, {"PMC100.2": "CC BY-NC", "PMC100.1": "CC BY-NC-ND"})
    pmcid, ok, err = dp.process_one("PMC100", tmp_path, ["xml"], False, [2, 1])
    assert (pmcid, ok) == ("PMC100", False)
    assert err.startswith(dp.NONCOMMERCIAL_REASON)
    assert not (tmp_path / "metadata").exists()


def test_process_one_treats_a_null_licence_as_non_commercial(tmp_path, monkeypatch):
    """Real metadata (PMC13915.1) has license_code null; that is a version without a
    commercial licence, not a missing version."""
    _fake_store(monkeypatch, {"PMC13915.1": None})
    pmcid, ok, err = dp.process_one("PMC13915", tmp_path, ["xml"], False, [1])
    assert ok is False
    assert err == f"{dp.NONCOMMERCIAL_REASON} license_code=None"


def test_process_one_skips_a_downloaded_article_without_requests(tmp_path, monkeypatch):
    (tmp_path / "metadata").mkdir()
    (tmp_path / "xml").mkdir()
    (tmp_path / "metadata" / "PMC100.1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "xml" / "PMC100.1.xml").write_text("<article/>", encoding="utf-8")
    reads = _fake_store(monkeypatch, {})
    assert dp.process_one("PMC100", tmp_path, ["xml"], False, [2, 1]) == ("PMC100", True, None)
    assert reads == []


def test_esearch_refuses_more_than_it_can_return(monkeypatch):
    payload = {"esearchresult": {"count": "5501159", "idlist": [], "webenv": "w", "querykey": "1"}}
    monkeypatch.setattr(dp, "http_get", lambda url, **kw: _Resp(payload=payload))
    with pytest.raises(SystemExit) as exc:
        dp.esearch_commercial_ids(0)
    assert "9,999" in str(exc.value)


def test_main_records_non_commercial_and_skips_them_on_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("episteme.config._find_project_dotenv", lambda: None)
    monkeypatch.setenv("EPISTEME_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    import episteme.config as cfg

    cfg.get_settings.cache_clear()
    out = tmp_path / "oa_comm"
    monkeypatch.setattr(
        dp,
        "fetch_inventory",
        lambda dest: ("2026-10-17T01-00Z", {"PMC100": [2, 1], "PMC20": [1], "PMC3": [1]}),
    )
    reads = _fake_store(monkeypatch, {"PMC100.2": "CC BY", "PMC20.1": "CC BY-NC", "PMC3.1": "CC0"})
    monkeypatch.setattr(sys, "argv", ["download_pmc", "--output_dir", str(out), "--limit", "0"])
    try:
        dp.main()
        assert sorted(reads) == ["PMC100.2", "PMC20.1", "PMC3.1"]
        assert (out / "filelists" / dp.NONCOMMERCIAL_LEDGER).read_text().split() == ["PMC20"]
        assert sorted(p.name for p in (out / "xml").iterdir()) == ["PMC100.2.xml", "PMC3.1.xml"]
        meta = (out / "sync_meta.txt").read_text()
        assert "id_source=inventory:2026-10-17T01-00Z" in meta
        assert "succeeded=2" in meta and "noncommercial=1" in meta

        reads.clear()
        dp.main()
        assert reads == []
        assert "succeeded=2" in (out / "sync_meta.txt").read_text()
    finally:
        cfg.get_settings.cache_clear()
