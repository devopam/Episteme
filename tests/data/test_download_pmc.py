"""Network-free smoke tests for the pure helpers in episteme.data.pmc.download_pmc.

Replaces the deleted tests/test_pmc_download.py. Assertions are checked against
the module source: s3_to_http() strips the s3:// prefix and any ?query, then
prefixes S3_HTTP; is_commercial_meta() reads meta["license_code"], normalises
spacing/underscores, and accepts CC0 / CC BY / CC BY-SA / CC BY-ND (rejecting
anything containing "NC").
"""

import importlib

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
    monkeypatch.setenv("EPISTEME_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    import episteme.config as cfg

    cfg.get_settings.cache_clear()
    from episteme.data.pmc import download_pmc

    importlib.reload(download_pmc)  # re-evaluate the argparse default against the new settings
    parser = download_pmc.build_parser()
    args = parser.parse_args([])
    assert args.output_dir == tmp_path / "01_raw" / "pmc" / "oa_comm"
