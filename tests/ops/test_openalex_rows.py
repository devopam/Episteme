"""episteme.ops.openalex_rows with a fake connection (no DB)."""

from __future__ import annotations

from contextlib import contextmanager

from episteme.ops import openalex_rows as mod


class _Cur:
    def __init__(self, table: bool, rows: bool):
        self.table, self.rows, self._last = table, rows, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = self.table if "to_regclass" in sql else self.rows

    def fetchone(self):
        return (self._last,)


class _Conn:
    def __init__(self, table=True, rows=True):
        self._c = _Cur(table, rows)

    def cursor(self):
        return self._c


def test_rows_no_rows_no_table():
    assert mod.openalex_rows_exist(_Conn(True, True)) is True
    assert mod.openalex_rows_exist(_Conn(True, False)) is False
    assert mod.openalex_rows_exist(_Conn(False, True)) is False


def _patch(monkeypatch, conn=None, error=None):
    @contextmanager
    def fake():
        if error:
            raise error
        yield conn

    monkeypatch.setattr("episteme.data.db.connection.connection", fake)


def test_main_exit_codes(monkeypatch):
    _patch(monkeypatch, _Conn(True, True))
    assert mod.main() == 1
    _patch(monkeypatch, _Conn(True, False))
    assert mod.main() == 0
    _patch(monkeypatch, error=RuntimeError("down"))
    assert mod.main() == 2
