"""`ema download --dry-run` and `ema_epar download --dry-run` through run_pipeline.sh.

run_pipeline.sh passes --dry-run to a download wrapper as EPISTEME_DRY_RUN=1. The two EMA
wrappers must turn it into the flag of their Python module, or a dry run downloads for real.

No network: every EMA endpoint key (sources.env) is set to a closed local port in the child
environment, so a request would fail the run. The dry runs must exit 0 and list what they
would fetch. Unlike tests/test_run_pipeline_dispatch.py this module needs no network, so it
runs in CI.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from test_run_pipeline_bulk import BASH, REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "data" / "run_pipeline.sh"
SOURCES_ENV = REPO_ROOT / "scripts" / "data" / "_lib" / "sources.env"
DEAD = "http://127.0.0.1:9/"  # the discard port: nothing answers


def _ema_keys() -> list[str]:
    keys = re.findall(r"^(EMA_[A-Z0-9_]+)=", SOURCES_ENV.read_text(encoding="utf-8"), re.M)
    assert "EMA_EPAR_DOCUMENT_PREFIX" in keys
    return keys


def _run(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        **{k: DEAD + k.lower() for k in _ema_keys()},
        "EMA_EPAR_DOCUMENT_PREFIX": DEAD,
        "EPISTEME_EMA_DELAY_SECONDS": "0",
        "EPISTEME_ACTOR": "episteme_sys_admin",
        "PGDATABASE": "episteme_test",
        "EPISTEME_DATA_ROOT": str(tmp_path),
        "EPISTEME_RAW_ROOT": str(tmp_path / "01_raw"),
        "EPISTEME_PROCESSED_ROOT": str(tmp_path / "02_processed"),
        "EPISTEME_LOCK_DIR": str(tmp_path / "_ops" / "locks"),
    }
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_ema_download_dry_run_fetches_nothing(tmp_path: Path) -> None:
    proc = _run(["ema", "download", "--dry-run"], tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "dry-run: would fetch EMA_MEDICINES_JSON" in proc.stdout
    raw = tmp_path / "01_raw" / "ema"
    assert not raw.exists() or not any(raw.rglob("*.json"))


def test_ema_epar_download_dry_run_fetches_nothing(tmp_path: Path) -> None:
    raw = tmp_path / "01_raw" / "ema"
    raw.mkdir(parents=True)
    row = {
        "id": "7",
        "type": "product-information",
        "name": "Medicine7 : EPAR - Product information",
        "document_url": DEAD + "en/documents/product-information/medicine7_en.pdf",
        "last_updated_date": "2026-01-01T00:00:00Z",
    }
    feed = {"meta": {"total_records": 1}, "data": [row]}
    (raw / "ema_epar_documents.json").write_text(json.dumps(feed), encoding="utf-8")
    proc = _run(["ema_epar", "download", "--dry-run"], tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "dry-run: would fetch document 7" in proc.stdout
    assert not (raw / "epar" / "7.pdf").exists()


@pytest.mark.parametrize("wrapper", ["download_ema.sh", "download_ema_epar.sh"])
def test_the_wrapper_turns_the_dry_run_variable_into_the_flag(wrapper: str) -> None:
    text = (REPO_ROOT / "scripts" / "data" / "ema" / wrapper).read_text(encoding="utf-8")
    line = '[ "${EPISTEME_DRY_RUN:-0}" = "1" ] && set -- --dry-run "$@"'
    assert line in text and text.index(line) < text.index('exec "$PY"')
