"""``get_pool`` registers an ``atexit`` close hook, once per process."""

from episteme.data.db import connection as conn_mod

# Captured at import: the autouse conftest fixture replaces ``get_pool`` per test.
_REAL_GET_POOL = conn_mod.get_pool


class _FakePool:
    def __init__(self, *a, **kw):
        self.closed_with = "not closed"

    def close(self, timeout=5.0):
        self.closed_with = timeout


def _patch(monkeypatch):
    registered = []
    monkeypatch.setattr(conn_mod.atexit, "register", lambda fn, *a, **k: registered.append(fn))
    monkeypatch.setattr(conn_mod, "ConnectionPool", _FakePool)
    monkeypatch.setattr(conn_mod, "dsn_from_settings", lambda: "dsn")
    monkeypatch.setattr(conn_mod, "connect_kwargs", lambda s: {})
    monkeypatch.setattr(conn_mod, "_POOL", None)
    monkeypatch.setattr(conn_mod, "_ATEXIT_PID", None)
    return registered


def test_get_pool_registers_atexit_once(monkeypatch):
    registered = _patch(monkeypatch)
    pool = _REAL_GET_POOL()
    assert _REAL_GET_POOL() is pool
    assert len(registered) == 1
    # a rebuilt pool in the same process does not register a second hook
    monkeypatch.setattr(conn_mod, "_POOL", None)
    _REAL_GET_POOL()
    assert len(registered) == 1


def test_atexit_hook_closes_pool_with_short_timeout_and_swallows_errors(monkeypatch):
    registered = _patch(monkeypatch)
    pool = _REAL_GET_POOL()
    registered[0]()
    assert pool.closed_with <= 2

    def boom(timeout=5.0):
        raise RuntimeError("x")

    pool.close = boom
    registered[0]()  # must not raise
