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
