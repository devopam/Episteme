"""pg fixtures that insert substance rows clear them at teardown (restart identity)."""

from __future__ import annotations

import pytest

from conftest import truncate_substance


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)

    def fetchone(self):
        return (self.conn.db if "current_database" in self.conn.sql[-1] else True,)


class _Conn:
    def __init__(self, db):
        self.db = db
        self.sql: list[str] = []
        self.commits = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1


def test_truncate_restarts_identity_inside_the_episteme_test_guard():
    conn = _Conn("episteme_test")
    truncate_substance(conn)
    assert any("RESTART IDENTITY" in s and "substance_xref" in s for s in conn.sql)
    assert conn.commits == 1


def test_truncate_refuses_any_other_database():
    conn = _Conn("episteme")
    with pytest.raises(RuntimeError, match="refusing"):
        truncate_substance(conn)
    assert not any("TRUNCATE" in s for s in conn.sql)


def test_every_pg_module_that_inserts_substance_rows_requests_the_cleanup():
    """Any test file with an INSERT into substance or substance_xref has a ``db`` fixture
    that asks for ``clean_substance`` (or deletes its own rows by source)."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent
    insert = re.compile(r"INSERT INTO episteme\.substance(_xref)?\b")
    missing = []
    for path in sorted(root.rglob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name == Path(__file__).name or not insert.search(text):
            continue
        if "clean_substance" in text or "DELETE FROM episteme.substance_xref WHERE source" in text:
            continue
        missing.append(path.name)
    assert missing == []
