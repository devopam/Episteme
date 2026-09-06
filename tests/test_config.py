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
        toml_content = '[project]\nname = "episteme-test"\n'
        (tmp_path / "pyproject.toml").write_text(toml_content, encoding="utf-8")
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


def test_data_root_derives_the_three_roots(fresh_config):
    cfg = fresh_config("EPISTEME_DATA_ROOT=/mnt/ssd\n")
    s = cfg.get_settings()
    assert s.data_root == Path("/mnt/ssd")
    assert s.raw_root == Path("/mnt/ssd/01_raw")
    assert s.processed_root == Path("/mnt/ssd/02_processed")
    assert s.corpus_root == Path("/mnt/ssd/03_corpus")


def test_explicit_root_overrides_data_root(fresh_config):
    cfg = fresh_config("EPISTEME_DATA_ROOT=/mnt/ssd\n" "EPISTEME_RAW_ROOT=/other/raw\n")
    s = cfg.get_settings()
    assert s.raw_root == Path("/other/raw")  # explicit wins
    assert s.processed_root == Path("/mnt/ssd/02_processed")  # derived


def test_data_root_defaults_to_dot(fresh_config):
    cfg = fresh_config("")
    s = cfg.get_settings()
    assert s.data_root == Path(".")
    assert s.raw_root == Path("01_raw")  # Path(".") / "01_raw"


def test_db_password_from_env(fresh_config):
    cfg = fresh_config("EPISTEME_DB_PASSWORD=s3cr3t\n")
    assert cfg.get_settings().db_password == "s3cr3t"


_CHEMBL_DEFAULT = "https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest"


def test_sources_env_load_order(tmp_path, monkeypatch):
    # sources.env default is visible
    import episteme.config as cfg

    monkeypatch.delenv("CHEMBL_BASE", raising=False)
    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    s = cfg.get_settings()
    assert getattr(s, "chembl_base", None) == _CHEMBL_DEFAULT

    # real environment overrides sources.env
    monkeypatch.setenv("CHEMBL_BASE", "https://mirror.example/chembl")
    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().chembl_base == "https://mirror.example/chembl"


def test_dotenv_overrides_sources_env(fresh_config, monkeypatch):
    # middle leg of the load order: ./.env beats sources.env, loses to real env
    monkeypatch.delenv("CHEMBL_BASE", raising=False)
    cfg = fresh_config("CHEMBL_BASE=https://dotenv.example/chembl\n")
    assert cfg.get_settings().chembl_base == "https://dotenv.example/chembl"

    # top leg: real env beats ./.env for the same endpoint var
    monkeypatch.setenv("CHEMBL_BASE", "https://realenv.example/chembl")
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().chembl_base == "https://realenv.example/chembl"
