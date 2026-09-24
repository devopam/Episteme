"""Network-free smoke tests for the pure helpers in episteme.data.pmc.download_pmc.

Replaces the deleted tests/test_pmc_download.py. Assertions are checked against
the module source: s3_to_http() strips the s3:// prefix and any ?query, then
prefixes S3_HTTP; is_commercial_meta() reads meta["license_code"], normalises
spacing/underscores, and accepts CC0 / CC BY / CC BY-SA / CC BY-ND (rejecting
anything containing "NC").
"""

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
