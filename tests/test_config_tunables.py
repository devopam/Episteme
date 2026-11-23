"""Tunables that were source constants now come from ``config.Settings``.

Each one has today's value as its default and an env key to change it. The old
module names stay importable; the code reads the setting at the point of use.
"""

from __future__ import annotations

import os

import pytest

import episteme.config as cfg

MIB = 1024 * 1024
GB = 1024**3


@pytest.fixture
def clean_settings(tmp_path, monkeypatch):
    """No project .env (cwd has no pyproject.toml) and none of the tunables set."""
    monkeypatch.chdir(tmp_path)
    for key in [k for k in os.environ if k.startswith("EPISTEME_SLICE_MAX_BYTES_")]:
        monkeypatch.delenv(key)
    for key in (
        "EPISTEME_LOAD_BATCH_BYTES",
        "EPISTEME_WRITE_BATCH_BYTES",
        "EPISTEME_PMC_BUCKET_IDS",
        "EPISTEME_FAERS_LOCK_TIMEOUT_S",
        "EPISTEME_FAERS_MIN_LIVE_SHARE",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg.get_settings.cache_clear()
    yield monkeypatch
    cfg.get_settings.cache_clear()


def test_faers_lock_timeout_default_and_override(clean_settings):
    assert cfg.get_settings().faers_lock_timeout_s == 60
    clean_settings.setenv("EPISTEME_FAERS_LOCK_TIMEOUT_S", "7")
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().faers_lock_timeout_s == 7


def test_faers_min_live_share_default_and_override(clean_settings):
    assert cfg.get_settings().faers_min_live_share == 0.9
    clean_settings.setenv("EPISTEME_FAERS_MIN_LIVE_SHARE", "0.5")
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().faers_min_live_share == 0.5


def test_tunables_default_to_todays_values(clean_settings):
    s = cfg.get_settings()
    assert s.load_batch_bytes == 64 * MIB
    assert s.write_batch_bytes == 64 * MIB
    assert s.pmc_bucket_ids == 50000
    assert s.slice_max_bytes == {}


def test_tunables_read_from_env(clean_settings):
    clean_settings.setenv("EPISTEME_LOAD_BATCH_BYTES", str(8 * MIB))
    clean_settings.setenv("EPISTEME_WRITE_BATCH_BYTES", str(4 * MIB))
    clean_settings.setenv("EPISTEME_PMC_BUCKET_IDS", "1000")
    clean_settings.setenv("EPISTEME_SLICE_MAX_BYTES_PMC", str(2 * GB))
    clean_settings.setenv("EPISTEME_SLICE_MAX_BYTES_EUROPEPMC_MANUSCRIPT", "12345")
    s = cfg.get_settings()
    assert s.load_batch_bytes == 8 * MIB
    assert s.write_batch_bytes == 4 * MIB
    assert s.pmc_bucket_ids == 1000
    assert s.slice_max_bytes == {"pmc": 2 * GB, "europepmc_manuscript": 12345}


def test_bad_tunable_falls_back_to_default(clean_settings):
    clean_settings.setenv("EPISTEME_LOAD_BATCH_BYTES", "0")
    clean_settings.setenv("EPISTEME_PMC_BUCKET_IDS", "many")
    clean_settings.setenv("EPISTEME_SLICE_MAX_BYTES_PMC", "-5")
    s = cfg.get_settings()
    assert s.load_batch_bytes == 64 * MIB
    assert s.pmc_bucket_ids == 50000
    assert s.slice_max_bytes == {}


def test_old_names_stay_importable_with_todays_values():
    from episteme.data import postgres_loader, staging_writer
    from episteme.data.pmc import buckets

    assert postgres_loader.LOAD_BATCH_BYTES == 64 * MIB
    assert staging_writer.WRITE_BATCH_BYTES == 64 * MIB
    assert buckets.BUCKET_IDS == 50000


def test_bucket_of_follows_the_setting(clean_settings):
    from episteme.data.pmc import buckets

    clean_settings.setenv("EPISTEME_PMC_BUCKET_IDS", "1000")
    cfg.get_settings.cache_clear()
    assert buckets.bucket_of(2500) == 2
    clean_settings.delenv("EPISTEME_PMC_BUCKET_IDS")
    cfg.get_settings.cache_clear()
    assert buckets.bucket_of(2500) == 0


def test_batch_caps_follow_the_settings(clean_settings):
    from episteme.data import postgres_loader, staging_writer

    clean_settings.setenv("EPISTEME_WRITE_BATCH_BYTES", "10")
    clean_settings.setenv("EPISTEME_LOAD_BATCH_BYTES", "7")
    cfg.get_settings.cache_clear()
    rows = [{"title": "x" * 6}, {"title": "y" * 6}]
    assert [len(b) for b in staging_writer._batches(rows, 100)] == [1, 1]
    assert postgres_loader._load_batch_bytes() == 7


def test_slice_cap_follows_the_setting(clean_settings):
    from episteme.ops import gate_slice

    spec = gate_slice.SLICES["pmc"]
    assert gate_slice.max_bytes_of("pmc") == spec.max_bytes == 4 * GB
    clean_settings.setenv("EPISTEME_SLICE_MAX_BYTES_PMC", "999")
    cfg.get_settings.cache_clear()
    assert gate_slice.max_bytes_of("pmc") == 999
    assert gate_slice.max_bytes_of("apollo") == gate_slice.SLICES["apollo"].max_bytes


def test_teacher_settings_defaults_and_api_key_not_in_repr(clean_settings):
    for key in (
        "EPISTEME_TEACHER_API_KEY",
        "EPISTEME_TEACHER_MODEL",
        "EPISTEME_TEACHER_BUDGET_TOKENS",
        "EPISTEME_HARNESS_BASE_URL",
        "EPISTEME_HARNESS_MAX_TOOL_CALLS",
        "TEACHER_BASE_URL",
    ):
        clean_settings.delenv(key, raising=False)
    s = cfg.get_settings()
    assert s.teacher_api_key is None
    assert s.teacher_base_url == "https://api.deepseek.com/v1"
    assert s.teacher_model == "deepseek-chat"
    assert s.teacher_budget_tokens == 2000000
    assert s.harness_base_url == "http://127.0.0.1:8080/v1"
    assert s.harness_max_tool_calls == 24
    cfg.get_settings.cache_clear()
    clean_settings.setenv("EPISTEME_TEACHER_API_KEY", "secret-x")
    s = cfg.get_settings()
    assert s.teacher_api_key == "secret-x"
    assert "secret-x" not in repr(s)


def test_sft_mix_weights_parse(clean_settings):
    clean_settings.delenv("EPISTEME_SFT_MIX_WEIGHTS", raising=False)
    assert cfg.get_settings().sft_mix_weights == {}
    cfg.get_settings.cache_clear()
    clean_settings.setenv("EPISTEME_SFT_MIX_WEIGHTS", "pmc=0.7,guidelines=0.3")
    assert cfg.get_settings().sft_mix_weights == {"pmc": 0.7, "guidelines": 0.3}
    cfg.get_settings.cache_clear()
    clean_settings.setenv("EPISTEME_SFT_MIX_WEIGHTS", "pmc=-0.1")
    with pytest.raises(cfg.ConfigError):
        cfg.get_settings()
    cfg.get_settings.cache_clear()
    clean_settings.setenv("EPISTEME_SFT_MIX_WEIGHTS", "pmc")
    with pytest.raises(cfg.ConfigError):
        cfg.get_settings()


_NEW_TUNABLES = [
    # (env key, Settings field, default, distinct valid value)
    ("EPISTEME_DAILYMED_COPY_BATCH_ROWS", "dailymed_copy_batch_rows", 20_000, 111),
    ("EPISTEME_PMC_SUBMIT_BATCH", "pmc_submit_batch", 2_000, 112),
    ("EPISTEME_SFT_SLICE_BATCH_ROWS", "sft_slice_batch_rows", 2_048, 113),
    ("EPISTEME_SFT_SLICE_ROWS_PER_PART", "sft_slice_rows_per_part", 50_000, 114),
    ("EPISTEME_REFERENCE_DOCS_CONNECT_TIMEOUT_S", "reference_docs_connect_timeout_s", 30, 7.5),
    ("EPISTEME_REFERENCE_DOCS_READ_TIMEOUT_S", "reference_docs_read_timeout_s", 120, 8.5),
    (
        "EPISTEME_REFERENCE_DOCS_MAX_UNPACKED_BYTES",
        "reference_docs_max_unpacked_bytes",
        2 * GB,
        115,
    ),
    ("EPISTEME_REFERENCE_DOCS_MAX_WAIT_S", "reference_docs_max_wait_s", 900, 9.5),
    (
        "EPISTEME_REFERENCE_DOCS_FALLBACK_THROTTLE_WAIT_S",
        "reference_docs_fallback_throttle_wait_s",
        30,
        10.5,
    ),
    ("EPISTEME_EMA_CONNECT_TIMEOUT_S", "ema_connect_timeout_s", 30, 11.5),
    ("EPISTEME_BOOKSHELF_MAX_PACKAGE_BYTES", "bookshelf_max_package_bytes", GB, 116),
    ("EPISTEME_APOLLO_MIN_BYTES", "apollo_min_bytes", 10 * MIB, 117),
]


@pytest.fixture
def new_tunables_clean(clean_settings):
    for key, *_ in _NEW_TUNABLES:
        clean_settings.delenv(key, raising=False)
    cfg.get_settings.cache_clear()
    return clean_settings


def test_new_tunables_default_to_todays_values(new_tunables_clean):
    s = cfg.get_settings()
    for key, field, default, _ in _NEW_TUNABLES:
        assert getattr(s, field) == default, key


def test_new_tunables_read_from_env(new_tunables_clean):
    for key, field, _, value in _NEW_TUNABLES:
        new_tunables_clean.setenv(key, str(value))
        cfg.get_settings.cache_clear()
        assert getattr(cfg.get_settings(), field) == value, key
        new_tunables_clean.delenv(key)


@pytest.mark.parametrize("bad", ["0", "abc"])
def test_new_tunables_bad_values_fall_back(new_tunables_clean, bad):
    for key, field, default, _ in _NEW_TUNABLES:
        new_tunables_clean.setenv(key, bad)
        cfg.get_settings.cache_clear()
        assert getattr(cfg.get_settings(), field) == default, key
        new_tunables_clean.delenv(key)


def test_text_nullout_batch_rows_default_and_override(clean_settings):
    clean_settings.delenv("EPISTEME_TEXT_NULLOUT_BATCH_ROWS", raising=False)
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().text_nullout_batch_rows == 50_000
    clean_settings.setenv("EPISTEME_TEXT_NULLOUT_BATCH_ROWS", "123")
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().text_nullout_batch_rows == 123


def test_text_nullout_batch_blocks_default_and_override(clean_settings):
    clean_settings.delenv("EPISTEME_TEXT_NULLOUT_BATCH_BLOCKS", raising=False)
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().text_nullout_batch_blocks == 4096
    clean_settings.setenv("EPISTEME_TEXT_NULLOUT_BATCH_BLOCKS", "9")
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().text_nullout_batch_blocks == 9


def test_api_result_limits_default_and_override(clean_settings):
    assert cfg.api_result_limits()["search"] == (20, 200)
    clean_settings.setenv("EPISTEME_API_LIMIT_SEARCH", "5,50")
    assert cfg.api_result_limits()["search"] == (5, 50)
    clean_settings.setenv("EPISTEME_API_LIMIT_SEARCH", "10,5")
    with pytest.raises(ValueError):
        cfg.api_result_limits()


def test_fts_lock_timeout_default_none_and_override(clean_settings):
    """0 = no lock_timeout (a CONCURRENTLY build must not die in its last wait)."""
    clean_settings.delenv("EPISTEME_FTS_LOCK_TIMEOUT_S", raising=False)
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().fts_lock_timeout_s == 0
    clean_settings.setenv("EPISTEME_FTS_LOCK_TIMEOUT_S", "30")
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().fts_lock_timeout_s == 30
    for bad in ("-1", "x"):
        clean_settings.setenv("EPISTEME_FTS_LOCK_TIMEOUT_S", bad)
        cfg.get_settings.cache_clear()
        assert cfg.get_settings().fts_lock_timeout_s == 0


def test_search_scan_is_a_result_limit_family(clean_settings):
    clean_settings.delenv("EPISTEME_API_LIMIT_SEARCH_SCAN", raising=False)
    assert cfg.api_result_limits()["search_scan"] == (5000, 5000)
    clean_settings.setenv("EPISTEME_API_LIMIT_SEARCH_SCAN", "100,100")
    assert cfg.api_result_limits()["search_scan"] == (100, 100)
