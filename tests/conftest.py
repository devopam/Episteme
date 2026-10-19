"""Session-wide pytest guards for the Episteme test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_os_environ():
    """Snapshot/restore ``os.environ`` around every test, unconditionally.

    ``episteme.config.get_settings()`` is ``@lru_cache(maxsize=1)``-memoized:
    on its first invocation in a pytest session it calls ``dotenv.load_dotenv()``,
    which — by design, for production use — writes the project ``.env``'s real
    values directly into ``os.environ``. Nothing restores those values
    afterward, so whichever test happens to be the first (in whatever
    collection order) to reach ``get_settings()``, directly or via an imported
    module, leaks real ``.env`` values into ``os.environ`` for the rest of the
    session. This is a structural property of ``get_settings()``'s first call,
    not a bug in any one test.

    ``monkeypatch`` cannot fix this: it only restores what *it* was used to
    set, and ``load_dotenv`` writes to ``os.environ`` directly. So this
    fixture uses the same snapshot/restore idiom as ``tests/test_config.py``'s
    ``fresh_config`` (``os.environ.copy()`` before, ``os.environ.clear()`` +
    ``os.environ.update(...)`` after), but unconditionally, for every test —
    generalizing ``fresh_config``'s protection (opt-in, one fixture) to the
    whole suite (autouse, every test, every directory).
    """
    snapshot = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(snapshot)


# Belt-and-suspenders companion to _isolate_os_environ, keyed by item nodeid
# (tests run strictly serially in this suite, but a dict is defensive against
# any future parallelization).
_env_snapshot_by_item: dict[str, dict[str, str]] = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    """Snapshot ``os.environ`` before ANY fixture for this test is set up.

    A hookwrapper's pre-``yield`` code runs before the wrapped (default)
    implementation of the same hook, and fixture *setup* — including
    ``_isolate_os_environ`` below — happens inside that wrapped call. So this
    snapshot is taken strictly earlier than the fixture's own, at the true
    "nothing has touched os.environ for this test yet" point. Paired with
    the ``pytest_runtest_teardown`` hookwrapper below, which restores this
    exact snapshot after every finalizer (including ``monkeypatch``'s) has
    run.
    """
    _env_snapshot_by_item[item.nodeid] = os.environ.copy()
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Pin the pre-test ``os.environ`` snapshot after all finalizers have run.

    ``_isolate_os_environ`` restores the pre-test snapshot from *its own*
    finalizer — but it is an ordinary function-scoped fixture, and pytest
    does not guarantee a no-dependency autouse fixture finalizes after every
    other same-scope fixture. In particular, ``monkeypatch``'s own finalizer
    (``undo()``) can run *after* ``_isolate_os_environ``'s restore: if a test
    calls ``monkeypatch.setenv(KEY, ...)`` on a key that
    ``dotenv.load_dotenv()`` had just written directly into ``os.environ``
    (e.g. ``tests/test_config.py::test_dotenv_overrides_sources_env`` calling
    ``monkeypatch.setenv("CHEMBL_BASE", ...)`` right after ``fresh_config``
    loads a value for it via ``load_dotenv``), ``monkeypatch`` remembers that
    pre-existing value and restores *it* on its own teardown — re-introducing
    the very value ``_isolate_os_environ`` just cleaned up, with nothing left
    to undo that. Confirmed empirically: with only the fixture in place,
    that key was still present in ``os.environ`` at end-of-session.

    A hookwrapper's post-``yield`` code, by contrast, is guaranteed to run
    after *every* finalizer for the item (fixture teardown happens inside
    the wrapped/default implementation of this same hook) — so restoring the
    snapshot taken in ``pytest_runtest_setup`` above, here, after ``yield``,
    closes that gap regardless of fixture teardown order.
    """
    yield
    snapshot = _env_snapshot_by_item.pop(item.nodeid, None)
    if snapshot is not None:
        os.environ.clear()
        os.environ.update(snapshot)


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
    patching all four chokepoints in ``episteme.data.db.connection``:
    ``connection`` (the lazy-import seam most extractors use),
    ``get_pool`` (the pool factory ``connection()`` itself calls internally
    -- patching this also closes a module-top-import caller like
    ``load_articles.py``'s ``from ... import connection``, whose bound name
    is stale but whose function BODY still resolves ``get_pool`` dynamically,
    at call time, against ``connection.py``'s own module namespace -- so the
    patch here is still seen), and ``dsn_from_settings`` (the DSN builder a
    direct caller like ``corpus_materializer.py`` uses to hand a raw DSN to
    DuckDB's ``ATTACH``, bypassing ``connection()``/``get_pool()`` entirely).
    ``admin_dsn_from_settings`` is the bulk-index entry point's DSN builder (table-owner role).
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
    monkeypatch.setattr("episteme.data.db.connection.admin_dsn_from_settings", _no_db, raising=True)


@pytest.fixture(autouse=True)
def _redirect_audit_mirror(request, monkeypatch, tmp_path_factory):
    """Stop non-``pg`` tests from appending ``mirror_only`` lines to the
    real, developer-machine audit mirror (``02_processed/_ops/_audit/``).

    ``episteme.audit_trail._mirror_dir()`` (the sole chokepoint every
    mirror read/write in that module goes through -- ``record()``,
    ``verify()``, ``mirror_only()``) is ``get_settings().processed_root /
    "_ops" / "_audit"``. Many extractor tests pass their own ``tmp_path`` as
    an explicit ``processed_dir`` *argument* to the function under test, but
    that argument is never threaded into ``get_settings()`` -- so
    ``audit_trail``'s mirror keeps resolving against the session-wide
    cached ``Settings.processed_root`` (usually the repo's real
    ``./02_processed``, via ``.env`` or its default), independent of
    whatever ``processed_dir`` the test itself used. Every non-``pg``
    extractor test that reaches the ``mirror_only`` fallback (which
    ``_block_real_db_audit`` above forces for all of them) therefore
    silently appends a junk line to that real, shared file.

    Patches ``_mirror_dir`` with a lazy function -- it reads
    ``get_settings()`` only when actually CALLED (never at fixture setup,
    so it never forces a premature ``get_settings()`` cache fill for tests
    that don't need one): if the currently-configured ``processed_root`` is
    already inside this pytest session's own tmp tree (i.e. a test pointed
    it there itself, e.g. via ``monkeypatch.setenv("EPISTEME_PROCESSED_ROOT",
    str(tmp_path))`` + ``cfg.get_settings.cache_clear()``), the normal
    ``processed_root / "_ops" / "_audit"`` path is kept unchanged -- those
    tests must keep working exactly as before. Otherwise (the common case:
    no test-local override, so ``processed_root`` is whatever the real
    environment resolved to) the mirror is redirected to a throwaway
    directory under this fixture's own ``tmp_path_factory`` mount instead.

    ``pg``-marked tests are left alone, same as ``_block_real_db_audit``.
    """
    if "pg" in request.keywords:
        return

    import episteme.audit_trail as _audit_trail

    redirect_dir = tmp_path_factory.mktemp("audit_mirror")
    base = tmp_path_factory.getbasetemp().resolve()
    _real_mirror_dir = _audit_trail._mirror_dir

    def _mirror_dir():
        root = Path(_audit_trail.get_settings().processed_root).resolve()
        if root == base or base in root.parents:
            return _real_mirror_dir()
        return redirect_dir

    monkeypatch.setattr(_audit_trail, "_mirror_dir", _mirror_dir, raising=True)


@pytest.fixture(autouse=True)
def _redirect_failure_record(request, monkeypatch, tmp_path_factory):
    """Keep ``ops.failure.record_failure`` (``last_failure.json``) off the real
    ``02_processed/_ops/``, by the same rule as ``_redirect_audit_mirror``: an
    extractor test passes its own ``processed_dir`` argument, but the failure
    record resolves against ``get_settings().processed_root``. A test that
    points ``processed_root`` into the pytest tmp tree keeps the real path."""
    if "pg" in request.keywords:
        return

    from episteme.ops import failure as _failure

    redirect_dir = tmp_path_factory.mktemp("failure_record")
    base = tmp_path_factory.getbasetemp().resolve()
    _real_path = _failure._path

    def _path(source: str) -> Path:
        root = Path(_failure.get_settings().processed_root).resolve()
        if root == base or base in root.parents:
            return _real_path(source)
        return redirect_dir / "_ops" / source / "last_failure.json"

    monkeypatch.setattr(_failure, "_path", _path, raising=True)


@pytest.fixture
def spawn_child_env(monkeypatch, tmp_path):
    """Environment for a test that starts worker processes or subprocesses.

    A spawned child imports ``episteme`` afresh: none of this file's
    monkeypatches (``_block_real_db_audit``, ``_redirect_audit_mirror``) reach
    it, and its ``get_settings()`` would load the developer's ``.env`` (which
    may point at the production database). Spawn copies the parent's
    ``os.environ``, and real environment values beat ``.env``, so this pins
    the child to:

    * a database the DB-mode guard refuses before any connection attempt
      (``restricted`` mode, ``PGDATABASE`` == ``EPISTEME_PRODUCTION_DATABASE``),
      on a dead host/port as a second wall -- the extractors' best-effort
      audit then falls back to ``mirror_only``;
    * a ``processed_root`` (and so an audit mirror) under ``tmp_path``.

    Returns that ``processed_root``.
    """
    processed = tmp_path / "spawn_processed"
    monkeypatch.setenv("EPISTEME_DB_MODE", "restricted")
    monkeypatch.setenv("EPISTEME_DB_TARGET", "primary")
    monkeypatch.delenv("PGDATABASE_SECONDARY", raising=False)
    monkeypatch.setenv("PGDATABASE", "episteme_spawn_blocked")
    monkeypatch.setenv("EPISTEME_PRODUCTION_DATABASE", "episteme_spawn_blocked")
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    monkeypatch.setenv("PGPORT", "9")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(processed))
    monkeypatch.setenv("EPISTEME_ACTOR", "pytest-spawn")
    monkeypatch.delenv("EPISTEME_RUN_ID", raising=False)
    return processed


def _reset_settings_and_pool(monkeypatch) -> None:
    """Drop cached Settings everywhere they may be bound, and the pool.

    Other pg tests ``importlib.reload(episteme.config)``, which rebinds
    ``episteme.config.get_settings`` while ``connection.py`` keeps the
    object it imported, so clear both. Drop the pool so it is rebuilt
    from the pinned settings."""
    import episteme.config as cfg
    import episteme.data.db.connection as conn_mod

    cfg.get_settings.cache_clear()
    conn_mod.get_settings.cache_clear()
    monkeypatch.setattr(conn_mod, "_POOL", None)


@pytest.fixture
def pg_conn(monkeypatch):
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    # Guard against a misconfigured TEST_PG_DSN pointing at the real
    # `episteme` database: 7 `pg`-marked test files run
    # `DROP SCHEMA IF EXISTS episteme CASCADE` as their first act using this
    # fixture's connection. Refuse anything whose dbname isn't
    # episteme_test, rather than risk dropping the real schema.
    parts = conninfo_to_dict(dsn)
    dbname = parts.get("dbname")
    if dbname != "episteme_test":
        pytest.fail(
            f"TEST_PG_DSN does not target episteme_test (dbname={dbname!r}); "
            "refusing to run a pg test that DROPs SCHEMA against it"
        )

    # Pin every settings-based connection (pool, connection(), CLIs such as
    # load_articles.main / retire.main) to the same database as this fixture.
    # Without this they follow .env -- which on a developer machine may name
    # the production database (incident 2026-10-13: a pg test loaded fixture
    # rows into production). restricted mode + the production name is the
    # second wall: the guard refuses `episteme` before any connection.
    monkeypatch.setenv("PGDATABASE", dbname)
    for key, env in (
        ("host", "PGHOST"),
        ("port", "PGPORT"),
        ("user", "PGUSER"),
        ("password", "PGPASSWORD"),
    ):
        if parts.get(key):
            monkeypatch.setenv(env, str(parts[key]))
    monkeypatch.setenv("EPISTEME_DB_MODE", "restricted")
    monkeypatch.setenv("EPISTEME_PRODUCTION_DATABASE", "episteme")
    monkeypatch.setenv("EPISTEME_DB_TARGET", "primary")
    monkeypatch.delenv("PGDATABASE_SECONDARY", raising=False)
    _reset_settings_and_pool(monkeypatch)

    conn = psycopg.connect(dsn, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
