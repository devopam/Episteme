import importlib
from pathlib import Path

import pytest


@pytest.fixture
def fresh_config(tmp_path, monkeypatch):
    """Reload episteme.config with a controlled environment and cwd."""
    env_file = tmp_path / ".env"
    monkeypatch.chdir(tmp_path)
    for var in list(__import__("os").environ):
        if var.startswith(("EPISTEME_", "PG", "OM_", "NCBI_")):
            monkeypatch.delenv(var, raising=False)

    def _load(text: str):
        env_file.write_text(text, encoding="utf-8")
        import episteme.config as cfg
        importlib.reload(cfg)
        cfg.get_settings.cache_clear()
        return cfg

    return _load


def test_defaults_when_env_absent(fresh_config):
    cfg = fresh_config("")
    s = cfg.get_settings()
    assert s.raw_root == Path("./01_raw")
    assert s.pg_host == "localhost"
    assert s.pg_port == 5432
    assert s.download_threads == 4
    assert s.actor is None
    assert s.pmc_s3_bucket == "pmc-oa-opendata"


def test_env_file_overrides(fresh_config):
    cfg = fresh_config(
        "EPISTEME_RAW_ROOT=/data/raw\n"
        "PGHOST=db.internal\n"
        "PGPORT=6543\n"
        "EPISTEME_DOWNLOAD_THREADS=8\n"
        "EPISTEME_ACTOR=alice\n"
    )
    s = cfg.get_settings()
    assert s.raw_root == Path("/data/raw")
    assert s.pg_host == "db.internal"
    assert s.pg_port == 6543
    assert s.download_threads == 8
    assert s.actor == "alice"


def test_require_actor_raises_when_unset(fresh_config):
    cfg = fresh_config("")
    with pytest.raises(cfg.ConfigError):
        cfg.require_actor()


def test_require_actor_returns_value(fresh_config):
    cfg = fresh_config("EPISTEME_ACTOR=ci-bot\n")
    assert cfg.require_actor() == "ci-bot"
