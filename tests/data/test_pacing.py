"""Persistent request spacing: the pacing file and the wait rule. No clock, no network."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from episteme.data import pacing
from episteme.data.pacing import pacing_path, read_last_request, record_request, wait_seconds

_REAL_DEFAULT_DIR = pacing.default_pacing_dir  # the conftest replaces the module attribute
T = datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone.utc)


def test_missing_file_no_wait(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        last = read_last_request(pacing_path("ema.test", tmp_path))
    assert last is None
    assert wait_seconds(last, T, 60) == 0
    assert not caplog.records


def test_recent_request_waits_remainder():
    assert wait_seconds(T - timedelta(seconds=20), T, 60) == 40


def test_old_request_no_wait():
    assert wait_seconds(T - timedelta(minutes=10), T, 60) == 0


def test_future_time_caps_wait():
    assert wait_seconds(T + timedelta(hours=3), T, 60) == 60


def test_larger_cap_lets_a_retry_after_through():
    assert wait_seconds(T + timedelta(seconds=60), T, 60, cap_s=600) == 120


def test_corrupt_file_warns_and_returns_none(tmp_path, caplog):
    path = pacing_path("ema.test", tmp_path)
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert read_last_request(path) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_empty_file_warns_and_returns_none(tmp_path, caplog):
    path = pacing_path("ema.test", tmp_path)
    path.write_text("", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert read_last_request(path) is None
    assert len(caplog.records) == 1


def test_record_then_read_round_trip(tmp_path):
    path = pacing_path("ema.test", tmp_path / "new" / "dir")
    record_request(path, T)
    assert read_last_request(path) == T
    assert read_last_request(path).tzinfo is not None
    assert not [p for p in path.parent.iterdir() if p != path]  # no temp file left


def test_failed_write_warns_and_does_not_raise(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the directory should be", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        record_request(pacing_path("ema.test", blocker), T)
    assert len(caplog.records) == 1


def test_bad_host_refused(tmp_path):
    for host in ("", "a/b", "../x", "ema.test:443", "a b"):
        with pytest.raises(ValueError):
            pacing_path(host, tmp_path)


def test_default_directory_is_under_the_raw_root(tmp_path, monkeypatch):
    class _S:
        raw_root = tmp_path

    monkeypatch.setattr(pacing, "get_settings", lambda: _S)
    assert _REAL_DEFAULT_DIR() == tmp_path / "_pacing"
