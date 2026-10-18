import importlib
import logging
import os
from pathlib import Path

import pytest


@pytest.fixture
def fresh_config(tmp_path, monkeypatch):
    """Reload episteme.config with a controlled environment and cwd."""
    env_file = tmp_path / ".env"
    monkeypatch.chdir(tmp_path)
    snapshot = dict(os.environ)  # load_dotenv writes into os.environ; restore on teardown
    for var in list(os.environ):
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

    yield _load

    os.environ.clear()
    os.environ.update(snapshot)
    try:
        import episteme.config as cfg

        cfg.get_settings.cache_clear()
    except Exception:  # pragma: no cover - best effort
        pass


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
    cfg = fresh_config("EPISTEME_DATA_ROOT=/mnt/ssd\nEPISTEME_RAW_ROOT=/other/raw\n")
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
    # M2: chdir to an isolated dir with a stub pyproject.toml and NO .env, so
    # _find_project_dotenv() finds nothing and load_dotenv never writes the real
    # ./.env into os.environ for the rest of the pytest session.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "episteme-test"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

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


_EUROPEPMC_PREPRINT_DEFAULT = "https://ftp.ebi.ac.uk/pub/databases/pmc/preprints"


def test_europepmc_preprint_base_from_sources_env(tmp_path, monkeypatch):
    # PF-3: europepmc_preprint_base has NO literal default in config.py — its
    # value comes from scripts/data/_lib/sources.env (EUROPEPMC_PREPRINT_BASE).
    # Same isolation ritual as test_sources_env_load_order (M2): stub
    # pyproject.toml + chdir + no .env so the real ./.env never leaks.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "episteme-test"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    import episteme.config as cfg

    monkeypatch.delenv("EUROPEPMC_PREPRINT_BASE", raising=False)
    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().europepmc_preprint_base == _EUROPEPMC_PREPRINT_DEFAULT

    # real environment overrides sources.env
    monkeypatch.setenv("EUROPEPMC_PREPRINT_BASE", "https://mirror.example/preprints")
    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().europepmc_preprint_base == "https://mirror.example/preprints"


def test_dotenv_overrides_sources_env(fresh_config, monkeypatch):
    # middle leg of the load order: ./.env beats sources.env, loses to real env
    monkeypatch.delenv("CHEMBL_BASE", raising=False)
    cfg = fresh_config("CHEMBL_BASE=https://dotenv.example/chembl\n")
    assert cfg.get_settings().chembl_base == "https://dotenv.example/chembl"

    # top leg: real env beats ./.env for the same endpoint var
    monkeypatch.setenv("CHEMBL_BASE", "https://realenv.example/chembl")
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().chembl_base == "https://realenv.example/chembl"


class TestFreshConfigNoLeak:
    """fresh_config must not leak load_dotenv-written vars into os.environ.

    test_a loads an env file with a marker value; test_b asserts no marker value is left in
    os.environ. Keyed on the marker, not on variable names, so a leak of the same names from
    some unrelated earlier test cannot fail this check.
    """

    _MARK = "/mnt/fresh-config-leak-probe"
    _ran = False

    def test_a_loads_env_file(self, fresh_config):
        fresh_config(f"EPISTEME_DATA_ROOT={self._MARK}\nEPISTEME_RAW_ROOT={self._MARK}/raw\n")
        type(self)._ran = True

    def test_b_nothing_leaked_afterwards(self):
        if not self._ran:
            pytest.skip("needs test_a to have run first (file order)")
        assert not [k for k, v in os.environ.items() if self._MARK in v]


def test_duckdb_memory_limit_default(fresh_config):
    assert fresh_config("").get_settings().duckdb_memory_limit == "4GB"


def test_duckdb_memory_limit_override(fresh_config):
    cfg = fresh_config("EPISTEME_DUCKDB_MEMORY_LIMIT=2GB\n")
    assert cfg.get_settings().duckdb_memory_limit == "2GB"


def test_extract_workers_default(fresh_config):
    assert fresh_config("").get_settings().extract_workers == 3


def test_extract_workers_override(fresh_config):
    assert fresh_config("EPISTEME_EXTRACT_WORKERS=5\n").get_settings().extract_workers == 5


@pytest.mark.parametrize("bad", ["abc", "0", "-2", "2.5"])
def test_extract_workers_invalid_falls_back_with_warning(fresh_config, caplog, bad):
    cfg = fresh_config(f"EPISTEME_EXTRACT_WORKERS={bad}\n")
    with caplog.at_level(logging.WARNING, logger="episteme.config"):
        assert cfg.get_settings().extract_workers == 3
    assert "EPISTEME_EXTRACT_WORKERS" in caplog.text


def test_sys_admin_password_read(fresh_config):
    s = fresh_config("EPISTEME_SYS_ADMIN_PASSWORD=s3cret\n").get_settings()
    assert s.sys_admin_password == "s3cret"


def test_sys_admin_password_default_empty(fresh_config):
    assert fresh_config("").get_settings().sys_admin_password == ""


def test_duckdb_temp_dir_default_is_none(fresh_config):
    assert fresh_config("").get_settings().duckdb_temp_dir is None


def test_duckdb_temp_dir_override(fresh_config, tmp_path):
    cfg = fresh_config(f"EPISTEME_DUCKDB_TEMP_DIR={tmp_path.as_posix()}\n")
    assert cfg.get_settings().duckdb_temp_dir == tmp_path
