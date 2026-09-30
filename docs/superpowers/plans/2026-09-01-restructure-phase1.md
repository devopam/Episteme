# Data-Taxonomy Restructure — Phase 1 (moves, renames, config, green baseline) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganise `src/` and `scripts/` into the subject-area taxonomy, delete the legacy `data_pipeline/` and `model_pipeline/` trees, pull environment-varying configuration into `.env` + `config.py`, and get `pytest -q` green — with no behaviour changes.

**Architecture:** Pure restructure. Every code move is `git mv` + import-path fixes so history follows. New shared modules (`config.py`, `logging_setup.py`) and every not-yet-implemented source/stage module land as scaffolding (stubs that `raise NotImplementedError`). The Postgres storage layer, audit trail, and operator scripts are **out of scope** for this plan (Plans 2 and 3).

**Tech Stack:** Python 3.10+, setuptools/`pyproject.toml`, pytest, ruff, python-dotenv, pre-commit. No database, no new heavy dependency.

**Spec:** `docs/superpowers/specs/2026-09-01-data-taxonomy-and-postgres-restructure-design.md`

## Global Constraints

- **License:** MIT. The `LICENSE` file (already MIT) is authoritative; `pyproject.toml`, `README.md`, and `docs/project-incubation-baseline.md` must say MIT, not Apache-2.0.
- **`requires-python`** stays `>=3.10`.
- **Package layout:** src-layout, `[tool.setuptools.packages.find] where = ["src"]` unchanged.
- **Naming convention:** folder = subject area (`pubmed`, `pmc`, `europepmc`, `apollo`); filename = `<stage>_<subjectarea>[_<feed>].py` fully spelled out; stage verbs are the closed set `download`, `extract`, `load`, `graph`, `enrich` (+ `verify`, `train`, `evaluate`). No acronyms in filenames — `europepmc` not `epmc`, `continual_pretraining` not `cpt` — except `pubmed` and `pmc` which are kept (literal NCBI product names).
- **Config rule:** `src/episteme/config.py` is the ONLY module that reads `os.environ` / `os.getenv`. Everything environment-varying (paths, hosts, bucket names, HF repo ids, thread counts, sample limits, DSN parts) comes from it. Invariant constants (schema column lists, MinHash params, license rules) stay in code.
- **Do NOT rename** `src/episteme/data/schema.py`, `ops.py`, `writer.py` in this plan — they are imported by the four post-pull `data/*/extract.py` modules and get renamed in Plan 2 when those extractors are reworked.
- **Commits:** conventional-commit style. End every commit message body with the two trailer lines used elsewhere in this repo's history:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```
- **Branch:** `refactor/data-taxonomy-and-postgres` (already checked out).
- **Between every task, `pytest -q` must pass** (Task 1 establishes the green baseline).

---

## File-structure map

**Created:**
- `.editorconfig`, `.gitattributes`, `.pre-commit-config.yaml`, `.env.example`
- `src/episteme/config.py` — the single `os.environ` reader; typed `Settings`
- `src/episteme/logging_setup.py` — `configure_logging()`
- `src/episteme/audit_trail.py` — stub (filled in Plan 2)
- `src/episteme/data/__init__.py` and `__init__.py` in every `data/` subpackage
- `src/episteme/data/_legacy_download.py` — parked verbatim copy of the old `data_pipeline/download.py`, imported by nothing
- `src/episteme/data/pubmed/download_pubmed.py`, `src/episteme/data/apollo/download_apollo.py` — stubs
- `src/episteme/data/europepmc/{preprints,manuscripts,id_mappings,lite_metadata,abstracts}/__init__.py` + stub `download_europepmc_*.py`
- `src/episteme/model/__init__.py`
- `tests/data/test_curate.py`, `tests/data/test_download_pmc.py`, `tests/model/test_model_smoke.py`, `tests/test_config.py`, `tests/test_logging_setup.py`

**Moved (`git mv`, history preserved):**
- `src/episteme/model_pipeline/train_cpt.py` → `src/episteme/model/train_continual_pretraining.py`
- `src/episteme/model_pipeline/train_sft.py` → `src/episteme/model/train_supervised_finetuning.py`
- `src/episteme/model_pipeline/train_preference.py` → `src/episteme/model/train_preference_optimization.py`
- `src/episteme/model_pipeline/evaluate.py` → `src/episteme/model/evaluate_benchmarks.py`
- `src/episteme/data_pipeline/dedup.py` → `src/episteme/data/curate/deduplicate_corpus.py`
- `src/episteme/data_pipeline/decontaminate.py` → `src/episteme/data/curate/decontaminate_benchmarks.py`
- `src/episteme/data_pipeline/preprocess.py` → `src/episteme/data/curate/serialize_structured_sources.py`
- `scripts/download_pmc_oa_comm.py` → `src/episteme/data/pmc/download_pmc.py`
- `src/episteme/data/epmc/preprint_extract.py` → `src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py`

**Modified:**
- `pyproject.toml` — dependency groups, `[tool.ruff]`, `[tool.pytest.ini_options]`, MIT
- `.gitignore` — add `.env`, `graphify-out/`, `02_processed/`, `03_corpus/`, `.entire/`
- `README.md` — fix `python -m …train_cpt.py` invocations; MIT; point data section at `docs/10`
- `docs/project-incubation-baseline.md` — MIT; Drift Log entry
- `scripts/download_pmc_oa_comm.sh` — call `python -m episteme.data.pmc.download_pmc`

**Deleted:**
- `tests/test_pmc_download.py` (imports `query_esearch`/`get_metadata`/`download_pmc_commercial` — removed by the upstream rewrite; current cause of the red collection)
- `tests/test_data_pipeline.py`, `tests/test_model_pipeline.py` (moved into `tests/data/` and `tests/model/`)
- `src/episteme/data_pipeline/` (whole tree, after moves)
- `src/episteme/model_pipeline/` (whole tree, after moves)
- `src/episteme/data/epmc/` (whole dir, after the preprint move; superseded by `data/europepmc/`)

---

## Task 1: Repo hygiene and green baseline

**Files:**
- Delete: `tests/test_pmc_download.py`
- Create: `.editorconfig`, `.gitattributes`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: a green `pytest -q` baseline (9 tests) for every later task to build on.

- [ ] **Step 1: Confirm the current red**

Run: `.venv/Scripts/python.exe -m pytest -q --co`
Expected: collection ERROR in `tests/test_pmc_download.py` — `cannot import name 'query_esearch' from 'download_pmc_oa_comm'`.

- [ ] **Step 2: Delete the broken test**

Run: `git rm tests/test_pmc_download.py`

The three functions it exercised (`query_esearch`, `get_metadata`, `download_pmc_commercial`) no longer exist in `scripts/download_pmc_oa_comm.py` after the upstream rewrite. A minimal replacement test against the new API is added in Task 6.

- [ ] **Step 3: Verify green baseline**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `9 passed` (5 in `tests/test_data_pipeline.py`, 4 in `tests/test_model_pipeline.py`).

- [ ] **Step 4: Add `.editorconfig`**

Create `.editorconfig`:

```ini
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 4

[*.{yml,yaml,json,toml}]
indent_size = 2

[*.md]
trim_trailing_whitespace = false

[*.{sh,bash}]
indent_size = 2
```

- [ ] **Step 5: Add `.gitattributes`**

Create `.gitattributes`:

```gitattributes
* text=auto eol=lf
*.sh text eol=lf
*.bash text eol=lf
*.ps1 text eol=crlf
*.bat text eol=crlf
*.cmd text eol=crlf
*.png binary
*.gz binary
*.parquet binary
```

- [ ] **Step 6: Extend `.gitignore`**

Append to `.gitignore` (after the existing `01_raw/` line):

```gitignore

# Environment
.env

# Local pipeline output
02_processed/
03_corpus/

# Tooling scratch
graphify-out/
.entire/
```

- [ ] **Step 7: Verify still green and commit**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `9 passed`.

```bash
git add .editorconfig .gitattributes .gitignore
git rm --cached --ignore-unmatch -r graphify-out .entire 2>/dev/null || true
git commit -m "$(cat <<'EOF'
chore: green test baseline + editorconfig/gitattributes/gitignore

Delete tests/test_pmc_download.py — it imports query_esearch/get_metadata/
download_pmc_commercial, all removed by the upstream download_pmc_oa_comm.py
rewrite, and it halts collection for the whole suite. Add .editorconfig and
.gitattributes (LF-forced *.sh — repo is edited on Windows), and ignore
.env / 02_processed / 03_corpus / graphify-out / .entire.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 2: `config.py`, `logging_setup.py`, `.env.example`

**Files:**
- Create: `src/episteme/config.py`, `src/episteme/logging_setup.py`, `.env.example`
- Create: `tests/test_config.py`, `tests/test_logging_setup.py`
- Modify: `pyproject.toml` (add `python-dotenv` to `dependencies`)

**Interfaces:**
- Produces:
  - `episteme.config.get_settings() -> Settings` (cached). `Settings` is a frozen dataclass with fields:
    `raw_root: Path`, `processed_root: Path`, `corpus_root: Path`,
    `pg_host: str`, `pg_port: int`, `pg_database: str`, `pg_user: str`, `pg_password: str`,
    `om_host: str | None`, `om_jwt: str | None`, `ncbi_api_key: str | None`,
    `download_threads: int`, `sample_limit: int`, `actor: str | None`,
    `ncbi_ftp_host: str`, `pmc_s3_bucket: str`, `ebi_ftp_host: str`, `europepmc_base_url: str`, `apollo_hf_repo: str`.
  - `episteme.config.require_actor() -> str` — returns `settings.actor`, raises `ConfigError` if `None`/empty. (Used by Plan 2's audit trail; defined here so the rule lives in one place.)
  - `episteme.config.ConfigError(RuntimeError)`.
  - `episteme.logging_setup.configure_logging(level: str = "INFO", json_format: bool = False) -> None`.

- [ ] **Step 1: Add `python-dotenv` dependency**

In `pyproject.toml`, add to the `dependencies` list (under "Data Processing & Parsing"):

```toml
    "python-dotenv>=1.0.0",
```

Run: `.venv/Scripts/python.exe -m pip install "python-dotenv>=1.0.0"`

- [ ] **Step 2: Write the failing test for `config.py`**

Create `tests/test_config.py`:

```python
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
```

- [ ] **Step 3: Run it and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_config.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'episteme.config'`.

- [ ] **Step 4: Write `src/episteme/config.py`**

```python
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


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    raw_root: Path
    processed_root: Path
    corpus_root: Path
    pg_host: str
    pg_port: int
    pg_database: str
    pg_user: str
    pg_password: str
    om_host: str | None
    om_jwt: str | None
    ncbi_api_key: str | None
    download_threads: int
    sample_limit: int
    actor: str | None
    ncbi_ftp_host: str
    pmc_s3_bucket: str
    ebi_ftp_host: str
    europepmc_base_url: str
    apollo_hf_repo: str

    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_database} "
            f"user={self.pg_user} password={self.pg_password}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv()  # reads ./.env if present; real env vars still win
    return Settings(
        raw_root=Path(_get("EPISTEME_RAW_ROOT", "./01_raw")),
        processed_root=Path(_get("EPISTEME_PROCESSED_ROOT", "./02_processed")),
        corpus_root=Path(_get("EPISTEME_CORPUS_ROOT", "./03_corpus")),
        pg_host=_get("PGHOST", "localhost"),
        pg_port=_get_int("PGPORT", 5432),
        pg_database=_get("PGDATABASE", "episteme"),
        pg_user=_get("PGUSER", "episteme"),
        pg_password=_get("PGPASSWORD", ""),
        om_host=_get("OM_HOST"),
        om_jwt=_get("OM_JWT"),
        ncbi_api_key=_get("NCBI_API_KEY"),
        download_threads=_get_int("EPISTEME_DOWNLOAD_THREADS", 4),
        sample_limit=_get_int("EPISTEME_SAMPLE_LIMIT", 0),
        actor=_get("EPISTEME_ACTOR"),
        ncbi_ftp_host=_get("NCBI_FTP_HOST", "ftp.ncbi.nlm.nih.gov"),
        pmc_s3_bucket=_get("PMC_S3_BUCKET", "pmc-oa-opendata"),
        ebi_ftp_host=_get("EBI_FTP_HOST", "ftp.ebi.ac.uk"),
        europepmc_base_url=_get(
            "EUROPEPMC_BASE_URL", "https://www.ebi.ac.uk/europepmc/webservices/rest"
        ),
        apollo_hf_repo=_get("APOLLO_HF_REPO", "FreedomIntelligence/ApolloCorpus"),
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
```

- [ ] **Step 5: Run the config test and watch it pass**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_config.py`
Expected: `4 passed`.

- [ ] **Step 6: Write the failing test for `logging_setup.py`**

Create `tests/test_logging_setup.py`:

```python
import json
import logging

from episteme.logging_setup import configure_logging


def test_configure_logging_plain(capsys):
    configure_logging(level="INFO", json_format=False)
    logging.getLogger("episteme.test").info("hello")
    err = capsys.readouterr().err
    assert "hello" in err
    assert "INFO" in err


def test_configure_logging_json(capsys):
    configure_logging(level="DEBUG", json_format=True)
    logging.getLogger("episteme.test").warning("structured", extra={"source": "pubmed"})
    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "structured"
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "episteme.test"
```

- [ ] **Step 7: Run it and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_logging_setup.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'episteme.logging_setup'`.

- [ ] **Step 8: Write `src/episteme/logging_setup.py`**

```python
"""Operational logging configuration.

This is NOT the audit trail (see episteme.audit_trail, Plan 2). These logs
are for debugging and progress; they are allowed to rotate and be lost.
Library modules should use ``logging.getLogger(__name__)`` and never call
``print``.
"""

from __future__ import annotations

import json
import logging
import sys


class _JsonFormatter(logging.Formatter):
    _RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_format: bool = False) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    if json_format:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())
```

- [ ] **Step 9: Run the logging test and watch it pass**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_logging_setup.py`
Expected: `2 passed`.

- [ ] **Step 10: Write `.env.example`**

Create `.env.example`:

```dotenv
# Copy to .env and adjust. .env is gitignored. Real environment variables
# override values set here. src/episteme/config.py is the only reader.

# --- Filesystem roots ---
EPISTEME_RAW_ROOT=./01_raw
EPISTEME_PROCESSED_ROOT=./02_processed
EPISTEME_CORPUS_ROOT=./03_corpus

# --- Postgres (Plan 2+) ---
PGHOST=localhost
PGPORT=5432
PGDATABASE=episteme
PGUSER=episteme
PGPASSWORD=

# --- OpenMetadata (Plan 2+; leave blank to skip the ingest step) ---
OM_HOST=
OM_JWT=

# --- Upstream endpoints ---
NCBI_FTP_HOST=ftp.ncbi.nlm.nih.gov
PMC_S3_BUCKET=pmc-oa-opendata
EBI_FTP_HOST=ftp.ebi.ac.uk
EUROPEPMC_BASE_URL=https://www.ebi.ac.uk/europepmc/webservices/rest
APOLLO_HF_REPO=FreedomIntelligence/ApolloCorpus

# --- Behaviour ---
NCBI_API_KEY=
EPISTEME_DOWNLOAD_THREADS=4
EPISTEME_SAMPLE_LIMIT=0

# --- Audit trail (Plan 2): REQUIRED for any stage that writes audit records.
#     No default — set to an operator identity or a named service account. ---
EPISTEME_ACTOR=
```

- [ ] **Step 11: Full suite green, commit**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `15 passed`.

```bash
git add src/episteme/config.py src/episteme/logging_setup.py .env.example \
        tests/test_config.py tests/test_logging_setup.py pyproject.toml
git commit -m "$(cat <<'EOF'
feat: config.py (sole env reader) + logging_setup + .env.example

config.get_settings() returns a frozen Settings dataclass built from .env
(via python-dotenv) with real env vars winning; config.require_actor()
centralises the "EPISTEME_ACTOR required, no placeholder" rule for Plan 2's
audit trail. logging_setup.configure_logging() gives plain or JSON stderr
logs so library code can drop bare print().

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 3: `pyproject.toml` dependency groups, tooling config, pre-commit, MIT

**Files:**
- Modify: `pyproject.toml`, `README.md`, `docs/project-incubation-baseline.md`
- Create: `.pre-commit-config.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces: `pip install -e ".[dev]"` and `pip install -e ".[data]"` targets; `ruff` + `pytest` config; `pg` / `slow` pytest markers.

- [ ] **Step 1: Rewrite the `[project]` dependency section of `pyproject.toml`**

Replace the current flat `dependencies = [ ... ]` block with a minimal base plus optional groups:

```toml
dependencies = [
    "python-dotenv>=1.0.0",
    "tqdm>=4.66.0",
]

[project.optional-dependencies]
data = [
    "pandas>=2.0.0",
    "polars>=0.19.0",
    "pyarrow>=12.0.0",
    "requests>=2.31.0",
    "nltk>=3.8.0",
    "datasketch>=1.5.0",
    "pyeuropepmc>=0.1.0",
    "datasets>=2.14.0",
    "lxml>=5.0.0",
]
model = [
    "torch>=2.0.0",
    "transformers>=4.34.0",
    "accelerate>=0.24.0",
    "peft>=0.6.0",
    "trl>=0.7.2",
    "evaluate>=0.4.0",
    "scikit-learn>=1.3.0",
]
dev = [
    "pytest>=7.0.0",
    "jsonschema>=4.0.0",
    "ruff>=0.6.0",
]
```

> Note: `psycopg`, `pgvector`, and `openmetadata-ingestion` are added to a
> `data` / `catalog` group in Plan 2, not here — Plan 1 touches no database.

- [ ] **Step 2: Add tooling config to `pyproject.toml`**

Append these tables:

```toml
[tool.ruff]
line-length = 100
target-version = "py310"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "pg: test requires a PostgreSQL database (skipped unless TEST_PG_DSN is set)",
    "slow: test is slow (model downloads, large fixtures)",
]
```

- [ ] **Step 3: Set the license to MIT everywhere**

- `pyproject.toml`: `license = "MIT"` (currently `"Apache-2.0"`).
- `README.md`: replace the "## License" body with:
  `This project is licensed under the MIT License. See [LICENSE](LICENSE).`
- `docs/project-incubation-baseline.md`: in the "## License" section change
  `**Chosen license:** Apache-2.0` → `**Chosen license:** MIT` and set the
  reasoning line to `MIT chosen for maximum permissiveness and simplicity; matches the existing LICENSE file.`
  Leave the 2026-09-01 audit note about the MIT/Apache contradiction — append `Resolved 2026-09-01: standardised on MIT.`

- [ ] **Step 4: Create `.pre-commit-config.yaml`**

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: end-of-file-fixer
      - id: trailing-whitespace
      - id: check-added-large-files
        args: [--maxkb=2048]
      - id: detect-private-key
      - id: check-merge-conflict
      - id: check-yaml
      - id: check-toml
```

- [ ] **Step 5: Verify installs and tooling**

Run:
```bash
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m pytest -q
```
Expected: install clean; `ruff` reports only pre-existing issues in un-migrated files (do not fix them here — later tasks move those files); `15 passed`.

> The existing `.venv` already has `pandas`, `torch`, `transformers`, etc. installed, so moving
> them into `[data]`/`[model]` groups does not uninstall them — `pytest` stays green here. A
> *fresh* venv for the full suite would need `pip install -e ".[data,model,dev]"`; that is only
> relevant to CI setup (Plan 3), not this task.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml README.md docs/project-incubation-baseline.md .pre-commit-config.yaml
git commit -m "$(cat <<'EOF'
build: dependency groups, ruff/pytest config, pre-commit, MIT license

Split the flat dependency list into base + [data]/[model]/[dev] optional
groups so the pipeline installs without torch. Add [tool.ruff] and
[tool.pytest.ini_options] (pg/slow markers). Standardise the license on
MIT across pyproject, README, and the incubation baseline to match the
LICENSE file. Add .pre-commit-config.yaml (ruff + large-file + private-key
guards — the class of check that would have caught the committed .pyc).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 4: `model_pipeline/` → `model/`

**Files:**
- Move: the four `model_pipeline/*.py` modules (see file-structure map)
- Create: `src/episteme/model/__init__.py`, `tests/model/test_model_smoke.py`
- Delete: `tests/test_model_pipeline.py`, `src/episteme/model_pipeline/` (incl. `__pycache__`)
- Modify: `README.md`

**Interfaces:**
- Produces: `episteme.model.train_continual_pretraining`, `episteme.model.train_supervised_finetuning`, `episteme.model.train_preference_optimization`, `episteme.model.evaluate_benchmarks` — each exposing `main()` exactly as the old modules did (no signature changes).

- [ ] **Step 1: Move the package with history**

```bash
git mv src/episteme/model_pipeline src/episteme/model
git mv src/episteme/model/train_cpt.py         src/episteme/model/train_continual_pretraining.py
git mv src/episteme/model/train_sft.py         src/episteme/model/train_supervised_finetuning.py
git mv src/episteme/model/train_preference.py  src/episteme/model/train_preference_optimization.py
git mv src/episteme/model/evaluate.py          src/episteme/model/evaluate_benchmarks.py
git rm -r --ignore-unmatch src/episteme/model/__pycache__
```

- [ ] **Step 2: Add the package marker**

Create `src/episteme/model/__init__.py`:

```python
"""Model fine-tuning and evaluation pipeline (Phase 0)."""
```

- [ ] **Step 3: Check for stale internal references**

Run: `grep -rn "model_pipeline\|train_cpt\|train_sft\|train_preference" src/episteme/model/`
Expected: no matches (the four modules are self-contained; each `main()` uses `argparse` and does not import its siblings). If any match appears, update it to the new module name in the same step.

- [ ] **Step 4: Move and rewrite the test**

```bash
mkdir -p tests/model
git mv tests/test_model_pipeline.py tests/model/test_model_smoke.py
```

In `tests/model/test_model_smoke.py`:
- change the import line
  `from episteme.model_pipeline import train_cpt, train_sft, train_preference, evaluate`
  to
  `from episteme.model import (`
  `    train_continual_pretraining as train_cpt,`
  `    train_supervised_finetuning as train_sft,`
  `    train_preference_optimization as train_preference,`
  `    evaluate_benchmarks as evaluate,`
  `)`
- in each test, the `test_args` list's first element (`"train_cpt.py"`, `"train_sft.py"`,
  `"train_preference.py"`, `"evaluate.py"`) is only `sys.argv[0]` and is not parsed — leave
  as-is or rename to the new filenames; either works. No other change needed; the aliases keep
  the bodies untouched.

- [ ] **Step 5: Run the model tests**

Run: `.venv/Scripts/python.exe -m pytest -q tests/model`
Expected: `4 passed` (these download `HuggingFaceM4/tiny-random-LlamaForCausalLM`; allow ~90s).

- [ ] **Step 6: Fix the README invocations**

In `README.md`, in the "### 2. Model Pipeline" block, replace the four commands' module paths:
- `python -m episteme.model_pipeline.train_cpt.py …` → `python -m episteme.model.train_continual_pretraining …`
- `python -m episteme.model_pipeline.train_sft.py …` → `python -m episteme.model.train_supervised_finetuning …`
- `python -m episteme.model_pipeline.train_preference.py …` → `python -m episteme.model.train_preference_optimization …`
- `python -m episteme.model_pipeline.evaluate …` → `python -m episteme.model.evaluate_benchmarks …`

(Note: the `.py` suffix on a `-m` module path was already broken; dropping it is part of the fix.)

- [ ] **Step 7: Full suite green, commit**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `15 passed`.

```bash
git add -A src/episteme/model tests/model README.md
git commit -m "$(cat <<'EOF'
refactor: model_pipeline/ -> model/ with spelled-out module names

git mv preserves history. train_cpt -> train_continual_pretraining,
train_sft -> train_supervised_finetuning, train_preference ->
train_preference_optimization, evaluate -> evaluate_benchmarks. Test moved
to tests/model/test_model_smoke.py with import aliases so bodies are
untouched. README -m invocations fixed (also drops the broken .py suffix).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 5: `data_pipeline/{dedup,decontaminate,preprocess}` → `data/curate/`

**Files:**
- Create: `src/episteme/data/__init__.py`, `src/episteme/data/curate/__init__.py`, `tests/data/test_curate.py`
- Move: the three curate modules (see map)
- Delete: `tests/test_data_pipeline.py`

**Interfaces:**
- Produces:
  - `episteme.data.curate.serialize_structured_sources` — `serialize_uniprot`, `process_chembl_csv`, `filter_and_process_openmedtext`, `main` (unchanged signatures)
  - `episteme.data.curate.deduplicate_corpus` — `get_shingles`, `build_minhash`, `deduplicate_corpus`, `main`
  - `episteme.data.curate.decontaminate_benchmarks` — `normalize_text`, `get_ngrams`, `build_test_ngrams`, `decontaminate_corpus`, `main`

- [ ] **Step 1: Create the package markers**

`src/episteme/data/__init__.py`:

```python
"""Episteme data pipeline: acquisition, extraction, loading, curation.

Source-folder <-> wire-name map (article_schema.SOURCES):
    pubmed/                  -> pubmed
    pmc/                     -> pmc_oa_comm
    europepmc/preprints/     -> europepmc_preprint   (Plan 2 aligns the wire values)
    europepmc/manuscripts/   -> europepmc_manuscript
    europepmc/lite_metadata/ -> europepmc_lite_metadata
    apollo/                  -> apollo
"""
```

`src/episteme/data/curate/__init__.py`:

```python
"""Corpus-level curation: serialisation, near-dedup, benchmark decontamination."""
```

- [ ] **Step 2: Move the three modules with history**

```bash
git mv src/episteme/data_pipeline/dedup.py          src/episteme/data/curate/deduplicate_corpus.py
git mv src/episteme/data_pipeline/decontaminate.py  src/episteme/data/curate/decontaminate_benchmarks.py
git mv src/episteme/data_pipeline/preprocess.py     src/episteme/data/curate/serialize_structured_sources.py
```

- [ ] **Step 3: Check for stale internal references**

Run: `grep -rn "data_pipeline" src/episteme/data/curate/`
Expected: no matches (these three modules import only stdlib + `datasketch` + `pandas` + `datasets`; they do not import each other or `data_pipeline`). If a match appears, fix it here.

- [ ] **Step 4: Move and fix the test**

```bash
mkdir -p tests/data
git mv tests/test_data_pipeline.py tests/data/test_curate.py
```

In `tests/data/test_curate.py`, replace the three import lines:

```python
from episteme.data.curate.serialize_structured_sources import (
    serialize_uniprot,
    process_chembl_csv,
    filter_and_process_openmedtext,
)
from episteme.data.curate.deduplicate_corpus import get_shingles
from episteme.data.curate.decontaminate_benchmarks import normalize_text, get_ngrams
```

No test-body changes.

- [ ] **Step 5: Run the curate tests**

Run: `.venv/Scripts/python.exe -m pytest -q tests/data/test_curate.py`
Expected: `5 passed`.

- [ ] **Step 6: Full suite green, commit**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `15 passed`.

```bash
git add -A src/episteme/data tests/data
git commit -m "$(cat <<'EOF'
refactor: data_pipeline/{dedup,decontaminate,preprocess} -> data/curate/

dedup -> deduplicate_corpus, decontaminate -> decontaminate_benchmarks,
preprocess -> serialize_structured_sources. Adds data/__init__.py (with the
source-folder <-> wire-name map) and data/curate/__init__.py. Test moved to
tests/data/test_curate.py, imports updated, bodies unchanged.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 6: PMC downloader into the package

**Files:**
- Move: `scripts/download_pmc_oa_comm.py` → `src/episteme/data/pmc/download_pmc.py`
- Create: `src/episteme/data/pmc/__init__.py`, `tests/data/test_download_pmc.py`
- Modify: `scripts/download_pmc_oa_comm.sh`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `episteme.data.pmc.download_pmc` — module keeps its current post-pull public API verbatim: `http_get`, `try_download_filelist`, `parse_filelist`, `accession_from_filelist_row`, `esearch_commercial_ids`, `load_metadata`, `resolve_version_id`, `is_commercial_meta`, `s3_to_http`, `download_to_file`, `process_one`, `collect_ids_from_filelist`, `main`, and constants `FILELIST_CANDIDATES`, `COMMERCIAL_ESEARCH`, `COMMERCIAL_LICENSE_CODES`.

- [ ] **Step 1: Move with history**

```bash
git mv scripts/download_pmc_oa_comm.py src/episteme/data/pmc/download_pmc.py
```

- [ ] **Step 2: Add the package marker**

Create `src/episteme/data/pmc/__init__.py`:

```python
"""PubMed Central Open Access commercial subset (pmc_oa_comm)."""
```

- [ ] **Step 3: Confirm the module imports cleanly from its new home**

Run: `.venv/Scripts/python.exe -c "import episteme.data.pmc.download_pmc as m; print(sorted(n for n in dir(m) if not n.startswith('_')))"`
Expected: prints the public names listed in Interfaces above, no ImportError.
If the module does `from __future__ import annotations` and only stdlib/`requests`/`tqdm` imports, it needs no edits. If `grep -n "^import\|^from" src/episteme/data/pmc/download_pmc.py` shows a relative/`scripts`-path import, fix it to an absolute `episteme.*` import here.

- [ ] **Step 4: Point the shell wrapper at the module**

In `scripts/download_pmc_oa_comm.sh`, replace the line that runs the Python file
(currently invokes `download_pmc_oa_comm.py` by path) with:

```bash
exec "$PYTHON_EXE" -m episteme.data.pmc.download_pmc "$@"
```

and set `export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"` above it if not already present.

- [ ] **Step 5: Write a minimal test against the pure functions**

Create `tests/data/test_download_pmc.py`:

```python
from episteme.data.pmc.download_pmc import s3_to_http, is_commercial_meta, COMMERCIAL_LICENSE_CODES


def test_s3_to_http_strips_scheme_and_query():
    url = "s3://pmc-oa-opendata/PMC12345.1/PMC12345.1.xml?md5=abc"
    assert s3_to_http(url) == "https://pmc-oa-opendata.s3.amazonaws.com/PMC12345.1/PMC12345.1.xml"


def test_is_commercial_meta_accepts_cc_by():
    assert "CC BY" in COMMERCIAL_LICENSE_CODES
    assert is_commercial_meta({"license_code": "CC BY"}) is True


def test_is_commercial_meta_rejects_noncommercial():
    assert is_commercial_meta({"license_code": "CC BY-NC"}) is False


def test_is_commercial_meta_rejects_missing_license():
    assert is_commercial_meta({}) is False
```

> If the post-pull signatures differ (e.g. `s3_to_http` returns a different host form, or
> `is_commercial_meta` takes extra args), adjust the asserts to the module's actual behaviour —
> read `src/episteme/data/pmc/download_pmc.py` lines 234–258 first. The intent is a fast,
> network-free smoke test of the two pure helpers, replacing the deleted `test_pmc_download.py`.

- [ ] **Step 6: Run the new test**

Run: `.venv/Scripts/python.exe -m pytest -q tests/data/test_download_pmc.py`
Expected: `4 passed`.

- [ ] **Step 7: Full suite green, commit**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `19 passed`.

```bash
git add -A src/episteme/data/pmc tests/data/test_download_pmc.py scripts/download_pmc_oa_comm.sh
git commit -m "$(cat <<'EOF'
refactor: move scripts/download_pmc_oa_comm.py into episteme.data.pmc.download_pmc

Kills the last Python module living under scripts/ and the sys.path hack
that imported it. The .sh wrapper now runs `python -m
episteme.data.pmc.download_pmc`. New network-free smoke test covers
s3_to_http() and is_commercial_meta(), replacing the deleted
tests/test_pmc_download.py.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 7: `data/epmc/` → `data/europepmc/` with feed subfolders; all `data/**` `__init__.py`

**Files:**
- Move: `src/episteme/data/epmc/preprint_extract.py` → `src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py`
- Create: `__init__.py` in `data/apollo`, `data/pubmed`, `data/europepmc`, and `data/europepmc/{preprints,manuscripts,id_mappings,lite_metadata,abstracts}`; stub `download_europepmc_*.py` in each feed folder
- Delete: `src/episteme/data/epmc/`

**Interfaces:**
- Consumes: nothing.
- Produces: `episteme.data.europepmc.preprints.extract_europepmc_preprints` (same public names the old `preprint_extract.py` had); five feed subpackages each with a stub `download_europepmc_<feed>.py` exposing `def main() -> None`.

- [ ] **Step 1: Add markers to the existing extract subpackages**

```bash
printf '"""PubMed baseline + daily update files (NLM)."""\n' > src/episteme/data/pubmed/__init__.py
printf '"""ApolloCorpus multilingual medical text (FreedomIntelligence)."""\n' > src/episteme/data/apollo/__init__.py
```

- [ ] **Step 2: Create the `europepmc/` tree**

```bash
mkdir -p src/episteme/data/europepmc/preprints \
         src/episteme/data/europepmc/manuscripts \
         src/episteme/data/europepmc/id_mappings \
         src/episteme/data/europepmc/lite_metadata \
         src/episteme/data/europepmc/abstracts
printf '"""Europe PMC feeds (EBI): preprints, manuscripts, id_mappings, lite_metadata, abstracts."""\n' \
  > src/episteme/data/europepmc/__init__.py
for feed in preprints manuscripts id_mappings lite_metadata abstracts; do
  printf '"""Europe PMC %s feed."""\n' "$feed" > "src/episteme/data/europepmc/$feed/__init__.py"
done
```

- [ ] **Step 3: Move the preprint extractor with history**

```bash
git mv src/episteme/data/epmc/preprint_extract.py \
       src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py
git rm -r --ignore-unmatch src/episteme/data/epmc
```

- [ ] **Step 4: Verify the moved extractor still imports**

Run: `.venv/Scripts/python.exe -c "import episteme.data.europepmc.preprints.extract_europepmc_preprints"`
Expected: no error. It imports `episteme.data.ops`, `episteme.data.schema`, `episteme.data.writer` — all unchanged by this plan (their rename is Plan 2), so no edits needed.

- [ ] **Step 5: Create the five download stubs**

For each `<feed>` in `preprints manuscripts id_mappings lite_metadata abstracts`, create
`src/episteme/data/europepmc/<feed>/download_europepmc_<feed>.py`:

```python
"""Download the Europe PMC <FEED> feed into 01_raw/europepmc/<feed>/.

STUB — real acquisition logic is ported from data/_legacy_download.py and the
existing scripts/ shell downloaders in Plan 3. See
docs/10-data-sources-runbook.md section for this feed.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "europepmc <feed> download is not implemented yet (Plan 3). "
        "Use the shell script under scripts/ until then."
    )


if __name__ == "__main__":
    main()
```

Substitute the real feed name for `<feed>` / `<FEED>` in each file.

- [ ] **Step 6: Import sweep**

Run:
```bash
.venv/Scripts/python.exe -c "import importlib, pkgutil, episteme; [importlib.import_module(m.name) for m in pkgutil.walk_packages(episteme.__path__, 'episteme.')]"
```
Expected: no error (every subpackage imports; stubs define `main` but don't call it).

- [ ] **Step 7: Full suite green, commit**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `19 passed`.

```bash
git add -A src/episteme/data
git commit -m "$(cat <<'EOF'
refactor: data/epmc/ -> data/europepmc/ with per-feed subpackages

Five feed subpackages (preprints, manuscripts, id_mappings, lite_metadata,
abstracts), each with a download_europepmc_<feed>.py stub. preprint_extract.py
-> europepmc/preprints/extract_europepmc_preprints.py (git mv). __init__.py
added across every data/ subpackage.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 8: Park `_legacy_download.py`, delete `data_pipeline/`, scaffold remaining stubs

**Files:**
- Move: `src/episteme/data_pipeline/download.py` → `src/episteme/data/_legacy_download.py`
- Create: `src/episteme/data/pubmed/download_pubmed.py`, `src/episteme/data/pubmed/extract_pubmed.py`,
  `src/episteme/data/pmc/extract_pmc.py`, `src/episteme/data/apollo/download_apollo.py`,
  `src/episteme/data/apollo/extract_apollo.py`, `src/episteme/audit_trail.py` (stub)
- Delete: `src/episteme/data_pipeline/` (whole remaining tree)

**Interfaces:**
- Consumes: nothing.
- Produces: `episteme.audit_trail` stub (`record(event_type, **fields) -> None` raising `NotImplementedError`), and `download_*`/`extract_*` stubs each with `def main() -> None`.

> **Why park, not port:** `data_pipeline/download.py` mixes literature-source
> downloads (pubmed, pmc, europepmc, apollo) with Stream-2 structured-DB
> downloads (ChEMBL, UniProt) and Hugging Face eval-dataset downloads.
> Splitting it cleanly is design work, not a move — it happens in Plan 3
> (literature feeds) and a future Stream-2 spec (ChEMBL/UniProt). Keeping a
> parked, unimported copy preserves the logic and its git history.

- [ ] **Step 1: Park the old multi-source downloader**

```bash
git mv src/episteme/data_pipeline/download.py src/episteme/data/_legacy_download.py
```

Prepend to `src/episteme/data/_legacy_download.py` (above the existing module docstring / first import):

```python
"""PARKED — pre-restructure multi-source downloader.

Not imported anywhere. Its per-source functions are ported in Plan 3:
    download_pubmed        -> data/pubmed/download_pubmed.py
    download_pmc_oa        -> already superseded by data/pmc/download_pmc.py
    download_europe_pmc    -> data/europepmc/preprints/download_europepmc_preprints.py
    download_hf_datasets   -> data/apollo/download_apollo.py + eval-set fetch (TBD)
    download_chembl,
    download_uniprot       -> a future Stream 2 (RAG) ingestion module, not this tree
Kept for reference + git history until each function has a real home.
"""
```

- [ ] **Step 2: Delete the rest of `data_pipeline/`**

```bash
git rm -r src/episteme/data_pipeline
```

(`data_pipeline/apollo/extract.py` is the pre-pull Apollo extractor, superseded by the
post-pull `data/apollo/extract.py`; `__pycache__` goes with it.)

- [ ] **Step 3: Create the acquisition/extraction stubs**

Create each of these with the stub body shown (substitute the real source/stage in the
docstring and the `NotImplementedError` message):

- `src/episteme/data/pubmed/download_pubmed.py`
- `src/episteme/data/pubmed/extract_pubmed.py`
- `src/episteme/data/pmc/extract_pmc.py`
- `src/episteme/data/apollo/download_apollo.py`
- `src/episteme/data/apollo/extract_apollo.py`

```python
"""<STAGE> <SOURCE> — <one line>.

STUB — implemented in Plan 2 (extract) / Plan 3 (download). Until then use
the shell script under scripts/ (download) or the post-pull data/<source>/
extract.py entrypoint if present.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError("<stage> <source> is not implemented yet")


if __name__ == "__main__":
    main()
```

> Note: `data/pmc/extract.py` and `data/pubmed/extract.py` already exist from the
> upstream pull. The `extract_pmc.py` / `extract_pubmed.py` stubs here are the
> *renamed* entrypoints the convention calls for; Plan 2 merges the real
> upstream `extract.py` logic into them and deletes the old-named files. For
> this task they are thin stubs sitting alongside the upstream files.

- [ ] **Step 4: Create the audit-trail stub**

`src/episteme/audit_trail.py`:

```python
"""ALCOA+ tamper-evident audit trail.

STUB — full implementation in Plan 2: writes episteme._audit (in the same
transaction as each data change) plus an append-only JSONL mirror, hash-
chained (prev_hash / record_hash), actor from config.require_actor().
"""

from __future__ import annotations

from typing import Any


def record(event_type: str, **fields: Any) -> None:
    raise NotImplementedError(
        "audit_trail.record is implemented in Plan 2 (Postgres storage layer)"
    )
```

- [ ] **Step 5: Import sweep + full suite**

Run:
```bash
.venv/Scripts/python.exe -c "import importlib, pkgutil, episteme; [importlib.import_module(m.name) for m in pkgutil.walk_packages(episteme.__path__, 'episteme.')]"
.venv/Scripts/python.exe -m pytest -q
```
Expected: import sweep clean; `19 passed`.

- [ ] **Step 6: Confirm the legacy trees are gone**

Run: `test ! -d src/episteme/data_pipeline && test ! -d src/episteme/model_pipeline && echo OK`
Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add -A src/episteme
git commit -m "$(cat <<'EOF'
refactor: remove data_pipeline/; park _legacy_download.py; scaffold stubs

data_pipeline/download.py -> data/_legacy_download.py (parked, unimported;
mixes literature + ChEMBL/UniProt + HF downloads that split in Plan 3 / a
Stream 2 spec). Rest of data_pipeline/ deleted (superseded by data/). Adds
stub download_/extract_ modules per the naming convention and an
audit_trail.record() stub for Plan 2.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 9: README + baseline drift-log + refresh the graph

**Files:**
- Modify: `README.md`, `docs/project-incubation-baseline.md`

**Interfaces:**
- Consumes: the finished layout from Tasks 1–8.
- Produces: docs that match the new tree; a final green gate.

- [ ] **Step 1: Update the README data-pipeline section**

In `README.md` "### 1. Data Pipeline", replace the `python -m episteme.data_pipeline.*`
command block with a short pointer:

```markdown
The data pipeline is driven by per-source scripts under `scripts/data/` and
documented end-to-end (first-time and incremental) in
[`docs/10-data-sources-runbook.md`](docs/10-data-sources-runbook.md).
Corpus-level curation lives in `episteme.data.curate`
(`serialize_structured_sources`, `deduplicate_corpus`, `decontaminate_benchmarks`).
```

Also update the "## Install" block to mention the extras:
`pip install -e ".[data]"` for the data pipeline, `pip install -e ".[model]"` for training,
`pip install -e ".[dev]"` for tests.

- [ ] **Step 2: Add the baseline Drift Log entry**

In `docs/project-incubation-baseline.md`, append under "## Drift log":

```markdown
- 2026-09-01: Phase-1 restructure (spec `docs/superpowers/specs/2026-09-01-data-taxonomy-and-postgres-restructure-design.md`).
  `src/episteme/{data_pipeline,model_pipeline}/` removed; subject-area taxonomy under
  `data/<source>/` and `model/` with spelled-out `<stage>_<subjectarea>` module names.
  Environment config centralised in `.env` + `episteme.config`. License standardised on MIT.
  Storage decision reversed (Iceberg/OpenMetadata-filesystem -> Postgres hybrid + Parquet
  corpus) — implemented in Plan 2; ADR-0001/0002 to be authored there.
```

- [ ] **Step 3: Final full-suite gate**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `19 passed`.

Run: `.venv/Scripts/python.exe -m ruff check src/episteme/config.py src/episteme/logging_setup.py src/episteme/audit_trail.py tests/test_config.py tests/test_logging_setup.py tests/data/test_download_pmc.py`
Expected: clean (new files only; pre-existing lint debt in moved files is Plan 2/3's to clear as those files are reworked).

- [ ] **Step 4: Commit**

```bash
git add README.md docs/project-incubation-baseline.md
git commit -m "$(cat <<'EOF'
docs: README + incubation-baseline drift log for the Phase-1 restructure

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

- [ ] **Step 5: Refresh the knowledge graph (optional, not committed)**

Run: `/graphify . --update`
This re-extracts only the changed files so later graph queries reflect the new layout.
`graphify-out/` is gitignored (Task 1) — do not commit it.

---

## Self-Review

**Spec coverage (Plan 1's slice — §3.2, §3.5, §3.6, §4 steps 1–6 + 9):**

| Spec item | Task |
|---|---|
| §3.2 `data/` + `model/` taxonomy, `<stage>_<subjectarea>` names, no acronyms | 4, 5, 6, 7, 8 |
| §3.2 `europepmc` not `epmc` | 7 |
| §3.2 `data_pipeline/` + `model_pipeline/` cease to exist | 4, 8 |
| §3.5 `.env` + `.env.example`; `config.py` sole `os.environ` reader | 2 |
| §3.5 `config.require_actor()` no-placeholder rule | 2 |
| §3.5 `logging_setup.py`, no bare `print()` in new code | 2 |
| §3.5 pyproject dependency groups + `[tool.ruff]` + `[tool.pytest.ini_options]` | 3 |
| §3.5 `.pre-commit-config.yaml`, `.editorconfig`, `.gitattributes` | 1, 3 |
| §3.5 `.gitignore` additions | 1 |
| §3.5 LICENSE → MIT (decision resolved) across 4 files | 3 |
| §3.6 tests reorg: `tests/data/`, `tests/model/`; delete `test_pmc_download.py` | 1, 4, 5, 6 |
| §3.6 `pytest -q` green again | 1 (baseline), held green through 9 |
| §4 step 1 scaffold (`__init__.py`, stubs, `audit_trail.py` stub) | 2, 7, 8 |
| §4 step 4 kill the `sys.path` hack | 6, 8 |

**Deferred to Plan 2/3 (not gaps — explicitly out of this plan's slice):** rename
`schema.py`/`ops.py`/`writer.py`; port `_legacy_download.py` per-source; `psycopg`/`pgvector`/
`openmetadata-ingestion` deps; `db/`, `postgres_loader`, `graph_builder`, `corpus_materializer`,
`openmetadata_manifest`, `enrich_openmetadata`, `sample_audit`; `scripts/data/` tree +
`run_pipeline.sh`; ADR-0001/0002; docs 07/08/09/10 rewrite.

**Placeholder scan:** the `NotImplementedError` stubs are intentional scaffolding per spec §3.4,
each with a concrete "implemented in Plan N" message — not plan placeholders. All test code and
all shell/edit steps are spelled out. Task 6 Step 5 carries an explicit "read lines 234–258
first and adjust asserts" instruction because the post-pull `download_pmc.py` API was rewritten
upstream and its exact helper semantics must be confirmed against the file — this is a
verification instruction, not a TODO.

**Type consistency:** `get_settings()` / `Settings` fields / `require_actor()` /
`ConfigError` are defined in Task 2 and referenced only there in Plan 1. `audit_trail.record`
signature defined in Task 8 matches its Plan 2 contract in the spec (§3.8). Module names used
in Task 4's test aliases match the `git mv` targets in Task 4 Step 1. `europepmc` feed folder
names are identical across Tasks 7 and 8.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-01-restructure-phase1.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
