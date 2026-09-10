"""Session-wide pytest guards for the Episteme test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _block_real_db_audit(request, monkeypatch):
    """Stop non-``pg`` tests from ever reaching a live Postgres.

    The extractors' best-effort audit (``_best_effort_audit`` in
    ``extract_pmc`` / ``extract_pubmed`` / ``extract_apollo`` / …) opens
    ``episteme.data.db.connection.connection`` and writes an
    ``episteme._audit`` row, degrading to the append-only JSONL mirror *only*
    when that connection fails. In a dev checkout ``./.env`` points
    ``PGDATABASE`` at the real ``episteme`` database, so a reachable server
    means every non-pg extractor unit test silently appends junk
    ``extract_commit`` rows to the real hash-chained audit log.

    Force the mirror path for every test *without* the ``pg`` marker by making
    the connection factory raise. ``pg``-marked tests are left alone — they
    target ``episteme_test`` via ``TEST_PG_DSN`` on purpose.
    """
    if "pg" in request.keywords:
        return

    def _no_db(*_args, **_kwargs):
        raise RuntimeError(
            "non-pg test attempted a real DB connection "
            "(blocked by tests/conftest.py::_block_real_db_audit)"
        )

    monkeypatch.setattr("episteme.data.db.connection.connection", _no_db, raising=True)
