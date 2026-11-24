"""Migration 0016 (API performance): idempotent after 0015, the year windows
(pg: episteme_test only), and schema.sql carrying the 0016 bodies (non-pg)."""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

import pytest

_DB_DIR = Path(__file__).resolve().parents[2] / "src/episteme/data/db"
_MIGRATION_0015 = _DB_DIR / "migrations/0015_api_layer.sql"
_MIGRATION = _DB_DIR / "migrations/0016_api_perf.sql"
_FUNCTION = re.compile(
    r"CREATE OR REPLACE FUNCTION (episteme_api(?:_internal)?\.\w+)\((.*?)\$\$(.*?)\$\$;", re.S
)


def _apply(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_MIGRATION_0015.read_text(encoding="utf-8"))
        cur.execute(_MIGRATION.read_text(encoding="utf-8"))
        cur.execute(_MIGRATION.read_text(encoding="utf-8"))
    conn.commit()


def _windows(conn, *args) -> list[tuple[int, int, int]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM episteme_api_internal.year_windows(%s, %s, %s, %s)", args)
        return cur.fetchall()


def _functions(path: Path) -> dict[str, str]:
    return {m.group(1): m.group(0) for m in _FUNCTION.finditer(path.read_text(encoding="utf-8"))}


# ---------------------------------------------------------------------------
# non-pg
# ---------------------------------------------------------------------------
def test_schema_sql_carries_the_0016_bodies():
    new = _functions(_MIGRATION)
    assert set(new) >= {
        "episteme_api_internal.year_windows",
        "episteme_api_internal.literature_by_mesh",
    }
    schema = _functions(_DB_DIR / "schema.sql")
    for name, text in new.items():
        assert schema.get(name) == text, name


def test_0016_functions_set_search_path_and_definer():
    for name, text in _functions(_MIGRATION).items():
        header = text.split("$$", 1)[0]
        assert "SET search_path = pg_catalog, pg_temp" in header, name
        if re.search(r"\bepisteme\.[a-z]", text):
            assert "SECURITY DEFINER" in header, name


# ---------------------------------------------------------------------------
# pg
# ---------------------------------------------------------------------------
@pytest.mark.pg
def test_0016_is_idempotent_and_creates_year_windows(pg_conn):
    _apply(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT to_regprocedure('episteme_api_internal.year_windows(int, int, int, int)')"
        )
        assert cur.fetchone()[0] is not None


@pytest.mark.pg
def test_year_windows_without_bounds(pg_conn):
    _apply(pg_conn)
    rows = _windows(pg_conn, None, None, 5, 2026)
    assert rows[0] == (2023, 9999, 1)
    assert rows[1] == (2013, 2022, 2)
    assert [r[2] for r in rows] == list(range(1, len(rows) + 1))
    for prev, cur in pairwise(rows):
        assert cur[1] == prev[0] - 1  # no gap, no overlap
    widths = [hi - lo + 1 for lo, hi, _ in rows[1:]]
    for prev, cur in pairwise(widths[:-1]):
        assert cur == 2 * prev  # each window twice the width of the one before
    assert widths[-1] <= 2 * widths[-2]  # the last one stops at 1
    assert rows[-1][0] == 1


@pytest.mark.pg
def test_year_windows_with_bounds(pg_conn):
    _apply(pg_conn)
    assert _windows(pg_conn, 2020, 2024, 5, 2026) == [(2020, 2024, 1)]
    assert _windows(pg_conn, 2024, 2020, 5, 2026) == []
    assert _windows(pg_conn, 2000, 2024, 5, 2026) == [
        (2020, 2024, 1),
        (2010, 2019, 2),
        (2000, 2009, 3),
    ]
    # A width below 1 counts as 1 (no endless recursion).
    assert _windows(pg_conn, 2024, 2025, 0, 2026) == [(2025, 2025, 1), (2024, 2024, 2)]
