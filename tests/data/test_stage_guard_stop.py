"""A stop (SIGTERM) during ``run_per_file`` exits 143 through ``stage_guard``.

One extractor (pmc) and one serializer (pubchem). The in-flight unit leaves
no success marker and no failed marker, so a re-run does it again; the unit
that finished before the stop keeps its success marker."""

from __future__ import annotations

import importlib
import shutil
import signal
from pathlib import Path

import pytest

from episteme.config import get_settings
from episteme.data.checkpoint_markers import failed_marker_path, is_success
from episteme.ops import stop

TESTS = Path(__file__).resolve().parents[1]
PMC_FIX = TESTS / "data" / "fixtures" / "pmc"
PUBCHEM_FIX = TESTS / "fixtures" / "sp4" / "pubchem"


def _pmc_raw(tmp_path: Path) -> Path:
    """Three articles in three id buckets (pmc_b0000..2): one part each, so
    the stop on the second ``write_rows`` call falls between units."""
    from episteme.data.pmc.buckets import BUCKET_IDS

    raw = tmp_path / "raw"
    raw.mkdir()
    meta = (PMC_FIX / "PMC9000001.1.json").read_text("utf-8")
    xml = (PMC_FIX / "PMC9000001.1.xml").read_text("utf-8")
    for i in (0, 1, 2):
        pmcid = f"PMC{i * BUCKET_IDS + 1}"
        (raw / f"{pmcid}.1.json").write_text(meta.replace("PMCFIX0001", pmcid), "utf-8")
        (raw / f"{pmcid}.1.xml").write_text(xml.replace("PMCFIX0001", pmcid), "utf-8")
    return raw


def _pubchem_raw(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    shutil.copytree(PUBCHEM_FIX, raw)
    return raw


# (module, raw-dir builder, which write_rows call gets the SIGTERM). The pubchem
# fixture is a single unit, so the stop hits its first (only) unit.
CASES = [
    ("episteme.data.pmc.extract_pmc", _pmc_raw, 2),
    ("episteme.data.pubchem.serialize_pubchem", _pubchem_raw, 1),
]


@pytest.fixture
def restore_signals(monkeypatch, tmp_path):
    """stage_guard installs process-wide handlers: put the old ones back."""
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("EPISTEME_RUN_ID", "stop-1")
    get_settings.cache_clear()
    saved = {
        name: signal.getsignal(getattr(signal, name))
        for name in ("SIGTERM", "SIGBREAK", "SIGINT")
        if hasattr(signal, name)
    }
    yield
    for name, handler in saved.items():
        signal.signal(getattr(signal, name), handler)
    stop._TERM_SEEN = False
    get_settings.cache_clear()


@pytest.mark.parametrize(("module", "make_raw", "stop_at"), CASES, ids=["pmc", "pubchem"])
def test_sigterm_during_run_per_file_exits_143(
    tmp_path, restore_signals, monkeypatch, module, make_raw, stop_at
):
    mod = importlib.import_module(module)
    raw = make_raw(tmp_path)
    out = tmp_path / "out"
    real_write_rows = mod.write_rows
    calls: list[str] = []

    def _write_rows(*args, **kwargs):
        calls.append(kwargs["source_file"])
        if len(calls) == stop_at:  # this unit is in flight when SIGTERM arrives
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)  # what the OS would do; raises KeyboardInterrupt
        return real_write_rows(*args, **kwargs)

    monkeypatch.setattr(mod, "write_rows", _write_rows)
    argv = ["--raw-dir", str(raw), "--processed-dir", str(out), "--workers", "1"]
    with pytest.raises(SystemExit) as exc:
        mod.main(argv)
    assert exc.value.code == 143
    assert len(calls) == stop_at  # no later unit started

    *done, inflight = calls
    for name in done:
        assert is_success(out, mod.SOURCE, name)  # finished before the stop: kept
    assert not is_success(out, mod.SOURCE, inflight)
    assert not failed_marker_path(out, mod.SOURCE, inflight).exists()
