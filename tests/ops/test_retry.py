# tests/ops/test_retry.py
import errno

import psycopg
import pytest
from psycopg import errors as pgerr

from episteme.ops import retry


def _pg(cls):
    return cls("x")


@pytest.mark.parametrize(
    "exc",
    [
        _pg(pgerr.DeadlockDetected),  # 40P01
        _pg(pgerr.SerializationFailure),  # 40001
        _pg(pgerr.LockNotAvailable),  # 55P03
        _pg(pgerr.AdminShutdown),  # 57P01
        _pg(pgerr.CannotConnectNow),  # 57P03
        _pg(pgerr.TooManyConnections),  # 53300
        psycopg.OperationalError("server closed the connection unexpectedly"),  # no sqlstate
        TimeoutError("t"),
        ConnectionResetError("r"),
        OSError(errno.EIO, "io"),
        OSError(errno.ETIMEDOUT, "t"),
        retry.TransientUnitError("u", "c"),
    ],
)
def test_transient(exc):
    assert retry.is_transient(exc)


@pytest.mark.parametrize(
    "exc",
    [
        _pg(pgerr.QueryCanceled),  # 57014: our own stop or an orphan sweep
        _pg(pgerr.UniqueViolation),
        _pg(pgerr.UndefinedTable),
        _pg(pgerr.InsufficientPrivilege),
        FileNotFoundError(errno.ENOENT, "f"),
        PermissionError(errno.EACCES, "p"),
        IsADirectoryError(errno.EISDIR, "d"),
        OSError(errno.ENOSPC, "full"),
        ValueError("bad"),
        KeyError("k"),
    ],
)
def test_permanent(exc):
    assert not retry.is_transient(exc)


def test_windows_winerror_allowlist():
    e = OSError(None, "device not ready")
    e.winerror = 21
    assert retry.is_transient(e)
    e2 = OSError(None, "other")
    e2.winerror = 5  # access denied
    assert not retry.is_transient(e2)


def test_with_retries_retries_transient_then_succeeds():
    calls, slept = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise pgerr.DeadlockDetected("d")
        return "ok"

    assert retry.with_retries(fn, sleep=slept.append) == "ok"
    assert len(calls) == 3 and slept == [30, 60]


def test_with_retries_gives_up_after_three_retries():
    calls, slept = [], []

    def fn():
        calls.append(1)
        raise TimeoutError("t")

    with pytest.raises(TimeoutError):
        retry.with_retries(fn, sleep=slept.append)
    assert len(calls) == 4 and slept == [30, 60, 120]


def test_with_retries_does_not_retry_permanent():
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("v")

    with pytest.raises(ValueError):
        retry.with_retries(fn, sleep=lambda s: None)
    assert len(calls) == 1


def test_with_retries_never_catches_keyboard_interrupt():
    def fn():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        retry.with_retries(fn, sleep=lambda s: None)


def test_on_retry_is_called_with_attempt_error_and_wait():
    seen = []

    def fn():
        if not seen:
            raise TimeoutError("t")
        return 1

    retry.with_retries(
        fn, sleep=lambda s: None, on_retry=lambda n, e, w: seen.append((n, type(e), w))
    )
    assert seen == [(1, TimeoutError, 30)]


def test_transient_unit_error_pickles():
    import pickle

    e = pickle.loads(pickle.dumps(retry.TransientUnitError("a.xml", "OSError(5)")))
    assert (e.unit, e.cause) == ("a.xml", "OSError(5)")
