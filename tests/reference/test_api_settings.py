"""API result-limit settings (no database)."""

from __future__ import annotations

import pytest

import episteme.config as cfg
from episteme.reference import api_settings

_KEYS = [f"EPISTEME_API_LIMIT_{f.upper()}" for f in cfg.API_RESULT_LIMITS]


@pytest.fixture
def clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_api_limits_defaults(clean):
    assert cfg.api_result_limits() == {
        "regulatory": (200, 1000),
        "label": (200, 2000),
        "trials": (200, 1000),
        "faers": (500, 5000),
        "literature": (50, 500),
        "graph": (50, 500),
        "search": (20, 200),
        "code": (1, 1),
        "resolve": (10, 50),
    }


def test_api_limit_env_override(clean):
    clean.setenv("EPISTEME_API_LIMIT_SEARCH", "5,50")
    out = cfg.api_result_limits()
    assert out["search"] == (5, 50)
    assert out["label"] == (200, 2000)


@pytest.mark.parametrize("bad", ["0,5", "10,5", "x", "5", "1,2,3", "-1,5"])
def test_api_limit_env_rejects_bad(clean, bad):
    clean.setenv("EPISTEME_API_LIMIT_SEARCH", bad)
    with pytest.raises(ValueError):
        cfg.api_result_limits()


def test_sync_is_one_transaction_with_counts(clean):
    calls = []

    class Cur:
        def execute(self, sql, params=None):
            calls.append(("x", sql.split()[0]))

        def executemany(self, sql, rows):
            calls.append(("m", len(list(rows))))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Tx:
        def __enter__(self):
            calls.append(("begin", 0))

        def __exit__(self, *a):
            calls.append(("end", 0))
            return False

    class Conn:
        def transaction(self):
            return Tx()

        def cursor(self):
            return Cur()

    counts = api_settings.sync(Conn())
    assert counts == {
        "source_subset": len(cfg.LOOKUP_SOURCE_SUBSETS),
        "result_limit": len(cfg.API_RESULT_LIMITS),
    }
    assert calls[0] == ("begin", 0) and calls[-1] == ("end", 0)
    assert [c for c in calls if c[0] == "x"] == [("x", "DELETE"), ("x", "DELETE")]
