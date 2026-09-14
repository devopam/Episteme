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

    Force the mirror path for every test *without* the ``pg`` marker by
    patching all three chokepoints in ``episteme.data.db.connection``:
    ``connection`` (the lazy-import seam most extractors use),
    ``get_pool`` (the pool factory ``connection()`` itself calls internally
    -- patching this also closes a module-top-import caller like
    ``load_articles.py``'s ``from ... import connection``, whose bound name
    is stale but whose function BODY still resolves ``get_pool`` dynamically,
    at call time, against ``connection.py``'s own module namespace -- so the
    patch here is still seen), and ``dsn_from_settings`` (the DSN builder a
    direct caller like ``corpus_materializer.py`` uses to hand a raw DSN to
    DuckDB's ``ATTACH``, bypassing ``connection()``/``get_pool()`` entirely).
    ``pg``-marked tests are left alone — they target ``episteme_test`` via
    ``TEST_PG_DSN`` on purpose.
    """
    if "pg" in request.keywords:
        return

    def _no_db(*_args, **_kwargs):
        raise RuntimeError(
            "non-pg test attempted a real DB connection "
            "(blocked by tests/conftest.py::_block_real_db_audit)"
        )

    monkeypatch.setattr("episteme.data.db.connection.connection", _no_db, raising=True)
    monkeypatch.setattr("episteme.data.db.connection.get_pool", _no_db, raising=True)
    monkeypatch.setattr("episteme.data.db.connection.dsn_from_settings", _no_db, raising=True)
