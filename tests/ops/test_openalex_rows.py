"""episteme.ops.openalex_rows with a fake connection (no DB)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from episteme.ops import openalex_rows as mod


class _Cur:
    def __init__(self, present: dict, rows: bool):
        self.present, self.rows, self._last = present, rows, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "to_regclass('episteme.articles_openalex')" in sql:
            self._last = self.present["articles"]
        elif "to_regclass('episteme.article_body_openalex')" in sql:
            self._last = self.present["body"]
        elif "information_schema" in sql:
            self._last = self.present["column"]
        else:
            self._last = self.rows

    def fetchone(self):
        return (self._last,)


class _Conn:
    def __init__(self, rows=True, **missing):
        present = {"articles": True, "body": True, "column": True}
        present.update({k: False for k in missing})
        self._c = _Cur(present, rows)

    def cursor(self):
        return self._c


def test_rows_and_no_rows():
    assert mod.openalex_rows_exist(_Conn(True)) is True
    assert mod.openalex_rows_exist(_Conn(False)) is False


@pytest.mark.parametrize("gone", ["articles", "body", "column"])
def test_missing_migration_0012_raises(gone):
    with pytest.raises(mod.MigrationMissing, match="migration 0012"):
        mod.openalex_rows_exist(_Conn(False, **{gone: True}))


def _patch(monkeypatch, conn=None, error=None):
    @contextmanager
    def fake():
        if error:
            raise error
        yield conn

    monkeypatch.setattr("episteme.data.db.connection.connection", fake)


def test_main_exit_codes(monkeypatch):
    _patch(monkeypatch, _Conn(True))
    assert mod.main() == 1
    _patch(monkeypatch, _Conn(False))
    assert mod.main() == 0
    _patch(monkeypatch, error=RuntimeError("down"))
    assert mod.main() == 2


def test_main_exits_2_without_migration_0012(monkeypatch):
    _patch(monkeypatch, _Conn(False, articles=True))
    assert mod.main() == 2
