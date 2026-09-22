"""SP6 Task 5 — the innermost ``except Exception: pass`` in each extractor's
``_best_effort_audit``-shaped fallback (the last-resort path *after*
``audit_trail.mirror_only`` itself has already failed) now logs a WARNING
instead of silently swallowing the exception.

Representative module: ``episteme.data.apollo.extract_apollo`` (small, has
its own test file). ``tests/conftest.py``'s autouse ``_block_real_db_audit``
fixture already forces ``episteme.data.db.connection.connection`` to raise
for every non-``pg`` test, which drives every extractor test through the
*outer* except -> ``mirror_only`` path already. This test additionally
monkeypatches ``episteme.audit_trail.mirror_only`` to raise, so the
*innermost* except -- the one this task changes -- fires too.

Confirms:
  * a WARNING-level record is emitted by the extractor's own module logger
    (not just "some warning somewhere");
  * the extraction's own success outcome is UNCHANGED -- the audit failure
    (at either level) must never fail the pipeline or flip ``mark_success``
    to ``mark_failed``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from episteme.data.apollo.extract_apollo import extract_apollo

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp2" / "apollo"


def test_innermost_audit_fallback_logs_warning_and_pipeline_still_succeeds(
    tmp_path, monkeypatch, caplog
):
    def _raise_mirror_only(*_args, **_kwargs):
        raise RuntimeError("mirror_only also unavailable (test)")

    # connection() is already forced to raise for every non-pg test (see
    # tests/conftest.py::_block_real_db_audit) -- that drives the OUTER
    # except into mirror_only. Here we also fail mirror_only itself, so the
    # INNERMOST except (this task's change) fires.
    monkeypatch.setattr("episteme.audit_trail.mirror_only", _raise_mirror_only)

    with caplog.at_level(logging.WARNING, logger="episteme.data.apollo.extract_apollo"):
        res = extract_apollo(FX, tmp_path)

    # The audit-mirror double-failure must never fail the extraction run.
    assert res["inputs"] >= 1
    assert res["failed"] == 0
    assert res["ok"] == res["inputs"]

    warnings = [
        r
        for r in caplog.records
        if r.name == "episteme.data.apollo.extract_apollo" and r.levelno == logging.WARNING
    ]
    assert warnings, "expected a WARNING from the innermost audit-fallback except"
    assert "mirror_only fallback also failed" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None  # exc_info=True was passed


def test_outer_except_alone_stays_unlogged_at_that_level(tmp_path):
    """Sanity check on the untouched path: when only connection() fails (the
    normal degraded case -- forced by the autouse fixture) and mirror_only
    itself succeeds, extraction still succeeds and no audit-fallback WARNING
    is emitted (the outer except's fallthrough to mirror_only is intentional
    and must stay silent at that level)."""
    handler_records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            handler_records.append(record)

    logger = logging.getLogger("episteme.data.apollo.extract_apollo")
    collector = _Collector(level=logging.WARNING)
    logger.addHandler(collector)
    try:
        res = extract_apollo(FX, tmp_path)
    finally:
        logger.removeHandler(collector)

    assert res["failed"] == 0
    assert res["ok"] == res["inputs"]
    assert not handler_records, "outer except's fallthrough to mirror_only must stay unlogged"
