"""``get_pool``: one pool per process, and exit hooks that close it once."""

import threading
import time

from episteme.data.db import connection as conn_mod

# Captured at import: the autouse conftest fixture replaces ``get_pool`` per test.
_REAL_GET_POOL = conn_mod.get_pool


class _FakePool:
    built = 0

    def __init__(self, *a, **kw):
        time.sleep(0.05)  # widen the race window
        type(self).built += 1
        self.close_calls = []

    def close(self, timeout=5.0):
        self.close_calls.append(timeout)


def _patch(monkeypatch):
    atexits, finalizers = [], []
    _FakePool.built = 0
    monkeypatch.setattr(conn_mod.atexit, "register", lambda fn, *a, **k: atexits.append(fn))
    monkeypatch.setattr(
        conn_mod.mp_util, "Finalize", lambda obj, fn, *a, **k: finalizers.append((fn, k))
    )
    monkeypatch.setattr(conn_mod, "ConnectionPool", _FakePool)
    monkeypatch.setattr(conn_mod, "dsn_from_settings", lambda: "dsn")
    monkeypatch.setattr(conn_mod, "connect_kwargs", lambda s: {})
    monkeypatch.setattr(conn_mod, "_POOL", None)
    monkeypatch.setattr(conn_mod, "_ATEXIT_PID", None)
    monkeypatch.setattr(conn_mod, "_CLOSED_POOL", None)
    return atexits, finalizers


def test_get_pool_registers_both_hooks_once(monkeypatch):
    atexits, finalizers = _patch(monkeypatch)
    pool = _REAL_GET_POOL()
    assert _REAL_GET_POOL() is pool
    assert len(atexits) == 1
    assert len(finalizers) == 1
    assert finalizers[0][1] == {"exitpriority": 10}
    # a rebuilt pool in the same process does not register again
    monkeypatch.setattr(conn_mod, "_POOL", None)
    _REAL_GET_POOL()
    assert len(atexits) == 1
    assert len(finalizers) == 1


def test_two_threads_build_exactly_one_pool(monkeypatch):
    atexits, finalizers = _patch(monkeypatch)
    pools = []
    barrier = threading.Barrier(2)

    def work():
        barrier.wait()
        pools.append(_REAL_GET_POOL())

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _FakePool.built == 1
    assert pools[0] is pools[1]
    assert len(atexits) == 1
    assert len(finalizers) == 1


def test_exit_hook_closes_once_with_short_timeout_and_swallows_errors(monkeypatch):
    atexits, finalizers = _patch(monkeypatch)
    pool = _REAL_GET_POOL()
    atexits[0]()
    finalizers[0][0]()  # the second hook for the same pool is harmless
    assert len(pool.close_calls) == 1
    assert pool.close_calls[0] <= 2

    monkeypatch.setattr(conn_mod, "_CLOSED_POOL", None)

    def boom(timeout=5.0):
        raise RuntimeError("x")

    pool.close = boom
    atexits[0]()  # must not raise
