from contextlib import contextmanager

from episteme.data import graph_builder as gb


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        pass

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    @contextmanager
    def transaction(self):
        yield

    def cursor(self):
        return _Cur(self._rows)


def test_pgq_drops_null_and_dedups():
    conn = _Conn([("3",), (None,), ("2",), ("3",), (None,)])
    assert gb._neighbours_pgq(conn, "1", 2) == ["2", "3"]
