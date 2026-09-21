"""Single source of environment-varying configuration.

This is the ONLY module in the codebase that reads os.environ / os.getenv.
Everything that varies between machines or deployments — filesystem roots,
Postgres DSN parts, upstream endpoints, thread counts, the audit actor —
comes from here. Invariant constants (schema columns, MinHash params,
license rules) live next to the code that uses them, not here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when a required configuration value is missing."""


# Committed endpoint defaults (scripts/data/_lib/sources.env), populated once by
# _load_sources_env(). These rank BELOW the real environment and the project
# .env: _get() consults this dict only when neither supplies a value. Nothing is
# written to os.environ, so config.py stays the sole os.environ *reader*.
_SOURCES: dict[str, str] = {}

DB_MODES = ("read-only", "restricted", "unrestricted")


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val not in (None, ""):
        return val
    val = _SOURCES.get(name)
    return val if val not in (None, "") else default


def _load_sources_env() -> None:
    """Populate ``_SOURCES`` from the nearest ``scripts/data/_lib/sources.env``.

    Load order everywhere (last wins): sources.env -> .env -> real environment.
    This is the lowest tier; ``.env`` (loaded into ``os.environ`` by
    ``load_dotenv``) and the real environment both override it, and neither is
    ever clobbered because nothing here touches ``os.environ``.
    """
    _SOURCES.clear()
    here = Path(__file__).resolve()
    for directory in (here.parent, *here.parents):
        cand = directory / "scripts" / "data" / "_lib" / "sources.env"
        if cand.is_file():
            for raw in cand.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                _SOURCES.setdefault(key.strip(), val)
            return
    raise ConfigError(
        "scripts/data/_lib/sources.env not found relative to episteme.config "
        "(endpoint defaults unavailable — non-editable install?)"
    )


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _find_project_dotenv() -> str | None:
    """Path to the project-root .env, or None.

    The project root is the nearest ancestor of the current working
    directory that contains ``pyproject.toml``. The search is bounded:
    it never looks above the project root, so a ``.env`` planted in an
    unrelated ancestor directory cannot inject configuration into an
    Episteme run.
    """
    here = Path.cwd()
    for directory in (here, *here.parents):
        if (directory / "pyproject.toml").is_file():
            env_path = directory / ".env"
            return str(env_path) if env_path.is_file() else None
    return None


@dataclass(frozen=True)
class Settings:
    data_root: Path
    raw_root: Path
    processed_root: Path
    corpus_root: Path
    pg_host: str
    pg_port: int
    pg_database: str
    pg_user: str
    pg_password: str
    db_password: str
    om_host: str | None
    om_jwt: str | None
    ncbi_api_key: str | None
    download_threads: int
    sample_limit: int
    actor: str | None
    run_id: str | None
    ncbi_ftp_host: str
    pmc_s3_bucket: str
    ebi_ftp_host: str
    europepmc_base_url: str
    apollo_hf_repo: str
    # SP3 acquisition endpoints — defaults live in scripts/data/_lib/sources.env
    chembl_base: str
    uniprot_base: str
    pubchem_base: str
    clinvar_base: str
    reactome_base: str
    mesh_base: str
    pubmed_ftp_base: str
    europepmc_base: str
    europepmc_preprint_base: str
    bookshelf_base: str
    openalex_s3: str
    dailymed_base: str
    openfda_catalog: str
    aact_downloads: str
    db_mode: str = "restricted"
    db_target: str = "primary"
    production_database: str = "episteme"
    pg_database_secondary: str | None = None

    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_database} "
            f"user={self.pg_user} password={self.pg_password}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_sources_env()  # committed endpoint defaults; ranked below .env and real env
    dotenv_path = _find_project_dotenv()
    if dotenv_path:
        load_dotenv(dotenv_path)  # loads the project-root .env if present; real env vars still win
    data_root = Path(_get("EPISTEME_DATA_ROOT", "."))
    raw_root = Path(_get("EPISTEME_RAW_ROOT") or (data_root / "01_raw"))
    processed_root = Path(_get("EPISTEME_PROCESSED_ROOT") or (data_root / "02_processed"))
    corpus_root = Path(_get("EPISTEME_CORPUS_ROOT") or (data_root / "03_corpus"))
    db_mode = (_get("EPISTEME_DB_MODE") or "restricted").strip().lower()
    if db_mode not in DB_MODES:
        raise ConfigError(f"EPISTEME_DB_MODE must be one of {DB_MODES}, got {db_mode!r}")
    db_target = (_get("EPISTEME_DB_TARGET") or "primary").strip().lower()
    if db_target not in ("primary", "secondary"):
        raise ConfigError(f"EPISTEME_DB_TARGET must be primary|secondary, got {db_target!r}")
    secondary = _get("PGDATABASE_SECONDARY") or None
    if db_target == "secondary" and not secondary:
        raise ConfigError("EPISTEME_DB_TARGET=secondary but PGDATABASE_SECONDARY is not set")
    pg_database = secondary if db_target == "secondary" else _get("PGDATABASE", "episteme")
    return Settings(
        data_root=data_root,
        raw_root=raw_root,
        processed_root=processed_root,
        corpus_root=corpus_root,
        pg_host=_get("PGHOST", "localhost"),
        pg_port=_get_int("PGPORT", 5432),
        pg_database=pg_database,
        pg_user=_get("PGUSER", "episteme"),
        pg_password=_get("PGPASSWORD", ""),
        db_password=_get("EPISTEME_DB_PASSWORD", "") or "",
        om_host=_get("OM_HOST"),
        om_jwt=_get("OM_JWT"),
        ncbi_api_key=_get("NCBI_API_KEY"),
        download_threads=_get_int("EPISTEME_DOWNLOAD_THREADS", 4),
        sample_limit=_get_int("EPISTEME_SAMPLE_LIMIT", 0),
        actor=_get("EPISTEME_ACTOR"),
        # Set by scripts/data/run_pipeline.sh (export EPISTEME_RUN_ID) so every
        # stage it execs shares one audit run_id; None for a standalone CLI
        # invocation, where each module falls back to generating its own.
        run_id=_get("EPISTEME_RUN_ID"),
        ncbi_ftp_host=_get("NCBI_FTP_HOST", "ftp.ncbi.nlm.nih.gov"),
        pmc_s3_bucket=_get("PMC_S3_BUCKET", "pmc-oa-opendata"),
        ebi_ftp_host=_get("EBI_FTP_HOST", "ftp.ebi.ac.uk"),
        europepmc_base_url=_get(
            "EUROPEPMC_BASE_URL", "https://www.ebi.ac.uk/europepmc/webservices/rest"
        ),
        apollo_hf_repo=_get("APOLLO_HF_REPO", "FreedomIntelligence/ApolloCorpus"),
        chembl_base=_get("CHEMBL_BASE"),
        uniprot_base=_get("UNIPROT_BASE"),
        pubchem_base=_get("PUBCHEM_BASE"),
        clinvar_base=_get("CLINVAR_BASE"),
        reactome_base=_get("REACTOME_BASE"),
        mesh_base=_get("MESH_BASE"),
        pubmed_ftp_base=_get("PUBMED_FTP_BASE"),
        # C-1: EUROPEPMC_BASE, else the legacy EUROPEPMC_BASE_URL, else the literal.
        europepmc_base=_get("EUROPEPMC_BASE")
        or _get("EUROPEPMC_BASE_URL", "https://www.ebi.ac.uk/europepmc/webservices/rest"),
        # No literal default: _load_sources_env() supplies EUROPEPMC_PREPRINT_BASE
        # from scripts/data/_lib/sources.env (and raises ConfigError if that file
        # is missing). Mirrors chembl_base / uniprot_base / pubmed_ftp_base.
        europepmc_preprint_base=_get("EUROPEPMC_PREPRINT_BASE"),
        bookshelf_base=_get("BOOKSHELF_BASE"),
        openalex_s3=_get("OPENALEX_S3"),
        dailymed_base=_get("DAILYMED_BASE"),
        openfda_catalog=_get("OPENFDA_CATALOG"),
        aact_downloads=_get("AACT_DOWNLOADS"),
        db_mode=db_mode,
        db_target=db_target,
        production_database=_get("EPISTEME_PRODUCTION_DATABASE", "episteme"),
        pg_database_secondary=secondary,
    )


def require_actor() -> str:
    """Return the configured audit actor or fail loudly.

    Audit records must be attributable to a real operator or a named
    service account — never a placeholder. Any pipeline stage that writes
    an audit record calls this first.
    """
    actor = get_settings().actor
    if not actor:
        raise ConfigError(
            "EPISTEME_ACTOR is not set. Set it to an operator identity or a "
            "named CI/service account before running a stage that writes audit records."
        )
    return actor
