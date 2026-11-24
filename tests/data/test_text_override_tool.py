"""text_override tool: argument checks and SQL text (non-pg), null-out behaviour (pg)."""

from __future__ import annotations

from pathlib import Path

import pytest

import episteme.config as cfg
from episteme.data.article_schema import TEXT_JOIN_SQL
from episteme.data.db import text_override as tool

REPO_ROOT = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------------- non-pg


class _NoDbConn:
    """A connection that must not be asked anything when the name is refused early."""

    def cursor(self):  # pragma: no cover - reached only on a bug
        raise AssertionError("the name check must run before any SQL")

    def rollback(self):  # pragma: no cover
        pass


@pytest.mark.parametrize(
    "name",
    ["article_body; drop", "articles_pubmed", "article_body_", "Article_body_pmc", "", "a b"],
)
def test_partition_name_refused_before_any_sql(name):
    with pytest.raises(tool.TextOverrideError):
        tool.validate_partition(_NoDbConn(), name)


def test_nullout_refuses_bad_partition_before_any_sql():
    with pytest.raises(tool.TextOverrideError):
        tool.nullout(_NoDbConn(), "article_body; drop", batch_rows=10, log=lambda m: None)


def test_batch_size_comes_from_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EPISTEME_TEXT_NULLOUT_BATCH_ROWS", raising=False)
    cfg.get_settings.cache_clear()
    try:
        assert cfg.get_settings().text_nullout_batch_rows == 50_000
        monkeypatch.setenv("EPISTEME_TEXT_NULLOUT_BATCH_ROWS", "77")
        cfg.get_settings.cache_clear()
        assert cfg.get_settings().text_nullout_batch_rows == 77
    finally:
        cfg.get_settings.cache_clear()


def test_repack_without_yes_flag_exits_2(capsys):
    assert tool.main(["repack", "--partition", "article_body_pmc"]) == 2
    assert "--yes-repack" in capsys.readouterr().err


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_main(monkeypatch):
    class _S:
        text_nullout_batch_rows = 10

    monkeypatch.setattr(tool, "get_settings", lambda: _S())
    monkeypatch.setattr(tool.db_connection, "admin_dsn_from_settings", lambda: "dsn")
    monkeypatch.setattr(tool, "pool_kwargs", lambda s: {})
    monkeypatch.setattr(tool.psycopg, "connect", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(tool, "check_server_version", lambda c: None)


def test_main_precheck_not_ok_exits_1(monkeypatch, capsys):
    _patch_main(monkeypatch)
    monkeypatch.setattr(
        tool,
        "precheck",
        lambda conn, part, **k: {
            "phase": "pre-0014",
            "column": "text",
            "rows": 3,
            "null_with_parts": 1,
            "empty_string": 0,
            "ok": False,
        },
    )
    assert tool.main(["precheck", "--partition", "article_body_pmc"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_precheck_ok_exits_0(monkeypatch, capsys):
    _patch_main(monkeypatch)
    monkeypatch.setattr(
        tool,
        "precheck",
        lambda conn, part, **k: {
            "phase": "post-0014",
            "column": "text_override",
            "rows": 3,
            "sentinel_all_blank": 0,
            "sentinel_with_parts": 0,
            "ok": True,
        },
    )
    assert tool.main(["precheck", "--partition", "article_body_pmc"]) == 0


def test_repack_parser_accepts_flag():
    args = tool.build_parser().parse_args(["repack", "--partition", "x", "--yes-repack"])
    assert args.yes_repack is True


def test_precheck_needs_partition_or_all():
    with pytest.raises(SystemExit):
        tool.build_parser().parse_args(["precheck"])


class _StubCur:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        text = query if isinstance(query, str) else query.as_string(None)
        self.conn.executed.append(text)
        self.conn.params.append(params)
        self.conn.last = text

    def fetchone(self):
        if "pg_inherits" in self.conn.last:
            return (1,)
        if "pg_relation_size" in self.conn.last:
            return (self.conn.end_block, 1234)
        if "array_agg" in self.conn.last:
            return self.conn.batches.pop(0)
        return None


class _StubConn:
    def __init__(self, batches):
        self.executed = []
        self.params = []
        self.end_block = 5
        self.last = ""
        self.batches = list(batches)

    def cursor(self):
        return _StubCur(self)

    def rollback(self):
        pass

    def commit(self):
        pass


def test_any_part_nonblank_is_the_negation_of_the_schema_blank_rule():
    from episteme.data import article_schema as sch

    for blank in (sch._BLANK_T, sch._BLANK_A, sch._BLANK_B):
        assert f"NOT ({blank})" in tool._ANY_PART_NONBLANK
    assert tool._ANY_PART_NONBLANK.count("NOT (") == 3


def test_nullout_composed_sql_compares_to_the_join_verbatim():
    conn = _StubConn([(1, "a1", "pmc"), (0, None, None)])
    logs = []
    res = tool.nullout(conn, "article_body_pmc", batch_rows=5, log=logs.append)
    updates = [q for q in conn.executed if q.startswith("UPDATE")]
    assert len(updates) == 1
    q = updates[0]
    assert q.endswith("text_override = " + TEXT_JOIN_SQL)
    assert "text_override IS NOT NULL AND text_override <> ''" in q
    assert 'UPDATE "episteme"."article_body_pmc" SET text_override = NULL' in q
    assert res == {"rows": 1, "nulled": 1}
    assert "nulled=1" in logs[0] and "scanned=1" in logs[0]


def test_log_last_key_is_the_start_after_format():
    conn = _StubConn([(2, "PMC123", "pmc"), (0, None, None)])
    logs = []
    tool.nullout(conn, "article_body_pmc", batch_rows=5, log=logs.append)
    key = logs[0].rsplit("last_key=", 1)[1]
    assert key == "PMC123,pmc"
    assert tool.parse_start_after(key) == ("PMC123", "pmc")


def test_parse_start_after_splits_on_the_last_comma():
    assert tool.parse_start_after("a,b,pmc") == ("a,b", "pmc")


@pytest.mark.parametrize("bad", ["nocomma", ",pmc", "id,", ","])
def test_parse_start_after_refuses_bad_values(bad):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        tool.parse_start_after(bad)


def test_start_after_without_comma_exits_2():
    with pytest.raises(SystemExit) as e:
        tool.main(["nullout", "--partition", "article_body_pmc", "--start-after", "nocomma"])
    assert e.value.code == 2


@pytest.mark.parametrize("bad", ["0", "-5", "x"])
def test_batch_rows_must_be_positive(bad):
    for action in ("precheck", "nullout"):
        argv = [action, "--partition", "article_body_pmc", "--batch-rows", bad]
        with pytest.raises(SystemExit) as e:
            tool.main(argv)
        assert e.value.code == 2


def test_repack_lock_timeout_default_and_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EPISTEME_REPACK_LOCK_TIMEOUT_S", raising=False)
    cfg.get_settings.cache_clear()
    try:
        assert cfg.get_settings().repack_lock_timeout_s == 60
        monkeypatch.setenv("EPISTEME_REPACK_LOCK_TIMEOUT_S", "9")
        cfg.get_settings.cache_clear()
        assert cfg.get_settings().repack_lock_timeout_s == 9
    finally:
        cfg.get_settings.cache_clear()


def test_repack_sets_lock_timeout_before_repack():
    class Conn(_StubConn):
        autocommit = False

    conn = Conn([])
    tool.repack(conn, "article_body_pmc", lock_timeout_s=7)
    sets = [i for i, q in enumerate(conn.executed) if q.startswith("SET lock_timeout")]
    packs = [i for i, q in enumerate(conn.executed) if q.startswith("REPACK")]
    assert sets and packs and sets[0] < packs[0]
    assert "'7s'" in conn.executed[sets[0]]


def test_physical_composed_sql_has_tid_range_and_the_shared_predicate():
    conn = _StubConn([])
    conn.end_block = 5
    logs = []
    res = tool.nullout_physical(conn, "article_body_pmc", batch_blocks=2, log=logs.append)
    idx = [i for i, q in enumerate(conn.executed) if q.startswith("UPDATE")]
    assert len(idx) == 3  # blocks 0-2, 2-4, 4-6
    q = conn.executed[idx[0]]
    assert "WHERE ctid >= %s::tid AND ctid < %s::tid AND " in q
    assert q.endswith(tool._NULLOUT_PREDICATE)
    assert q.endswith("text_override = " + TEXT_JOIN_SQL)
    assert [conn.params[i] for i in idx] == [
        ("(0,0)", "(2,0)"),
        ("(2,0)", "(4,0)"),
        ("(4,0)", "(6,0)"),
    ]
    assert logs[0] == "article_body_pmc: blocks=2/5 nulled=1 last_block=2"
    assert "blocks=5/5" in logs[2] and logs[2].endswith("last_block=6")
    assert res == {"rows": 1234, "nulled": 3, "end_block": 5}


def test_physical_start_block_skips_earlier_blocks():
    conn = _StubConn([])
    conn.end_block = 5
    tool.nullout_physical(
        conn, "article_body_pmc", batch_blocks=2, start_block=4, log=lambda m: None
    )
    ups = [i for i, q in enumerate(conn.executed) if q.startswith("UPDATE")]
    assert [conn.params[i] for i in ups] == [("(4,0)", "(6,0)")]


def test_key_mode_and_physical_mode_share_one_predicate():
    conn = _StubConn([(1, "a1", "pmc"), (0, None, None)])
    tool.nullout(conn, "article_body_pmc", batch_rows=5, log=lambda m: None)
    key_q = next(q for q in conn.executed if q.startswith("UPDATE"))
    assert key_q.endswith(tool._NULLOUT_PREDICATE)


@pytest.mark.parametrize("kw", [{"batch_blocks": 0}, {"batch_blocks": 1, "start_block": -1}])
def test_physical_refuses_bad_numbers_before_any_update(kw):
    conn = _StubConn([])
    with pytest.raises(tool.TextOverrideError):
        tool.nullout_physical(conn, "article_body_pmc", log=lambda m: None, **kw)
    assert not any(q.startswith("UPDATE") for q in conn.executed)


def test_physical_refuses_bad_partition_before_any_sql():
    with pytest.raises(tool.TextOverrideError):
        tool.nullout_physical(_NoDbConn(), "x; drop", batch_blocks=1, log=lambda m: None)


@pytest.mark.parametrize(
    "argv",
    [
        ["--order", "physical", "--start-after", "a,pmc"],
        ["--start-block", "3"],
        ["--batch-blocks", "3"],
        ["--order", "key", "--start-block", "3"],
    ],
)
def test_exclusive_order_options_exit_2(argv, capsys):
    assert tool.main(["nullout", "--partition", "article_body_pmc", *argv]) == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--batch-blocks", "0"],
        ["--batch-blocks", "-1"],
        ["--batch-blocks", "x"],
        ["--start-block", "-1"],
        ["--start-block", "x"],
        ["--order", "random"],
    ],
)
def test_physical_numeric_options_are_validated(argv):
    with pytest.raises(SystemExit) as e:
        tool.main(["nullout", "--partition", "article_body_pmc", "--order", "physical", *argv])
    assert e.value.code == 2


def test_physical_main_uses_settings_default_and_prints_final_line(monkeypatch, capsys):
    _patch_main(monkeypatch)
    seen = {}

    def fake(conn, part, **kw):
        seen.update(kw, part=part)
        return {"rows": 10, "nulled": 4, "end_block": 3}

    monkeypatch.setattr(tool, "nullout_physical", fake)
    monkeypatch.setattr(
        tool,
        "get_settings",
        lambda: type("S", (), {"text_nullout_batch_blocks": 77, "text_nullout_batch_rows": 10})(),
    )
    argv = ["nullout", "--partition", "article_body_pmc", "--order", "physical"]
    assert tool.main(argv) == 0
    assert capsys.readouterr().out.strip() == "nullout article_body_pmc: rows=10 nulled=4"
    assert seen["batch_blocks"] == 77 and seen["start_block"] == 0
    assert tool.main([*argv, "--batch-blocks", "5", "--start-block", "9"]) == 0
    assert seen["batch_blocks"] == 5 and seen["start_block"] == 9


# --------------------------------------------------------------------------------- pg


@pytest.fixture
def body(pg_conn):
    """episteme_test with the final schema; rows land in article_body_pmc."""
    with (
        pg_conn.cursor() as cur,
        open(REPO_ROOT / "src/episteme/data/db/extensions.sql") as ext,
        open(REPO_ROOT / "src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
    pg_conn.commit()
    return pg_conn


def _insert(conn, rows):
    with conn.cursor() as cur:
        for aid, title, abstract, body_text, override in rows:
            cur.execute(
                "INSERT INTO episteme.article_body "
                "(article_id, source, title, abstract, body_text, text_override) "
                "VALUES (%s, 'pmc', %s, %s, %s, %s)",
                (aid, title, abstract, body_text, override),
            )
    conn.commit()


def _fixture_rows():
    return [
        ("a1", "T", "A", "B", "T\n\nA\n\nB"),  # equals the join
        ("a2", "T", None, "B", "T\n\nB"),  # equals the join
        ("a3", "T", "A", "B", "different text"),  # differs
        ("a4", "T", "A", "B", ""),  # NULL sentinel
        ("a5", "T", "A", "B", None),  # already NULL
        ("a6", "  ", None, "B", "B"),  # blank title: join is just B
    ]


def _overrides(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT article_id, text_override FROM episteme.article_body ORDER BY 1")
        out = dict(cur.fetchall())
    conn.commit()
    return out


def _texts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT article_id, text FROM episteme.article_body ORDER BY 1")
        out = dict(cur.fetchall())
    conn.commit()
    return out


@pytest.mark.pg
def test_nullout_nulls_only_equal_rows(body):
    _insert(body, _fixture_rows())
    res = tool.nullout(body, "article_body_pmc", batch_rows=2, log=lambda m: None)
    ov = _overrides(body)
    assert res["rows"] == 6
    assert res["nulled"] == 3
    assert ov["a1"] is None and ov["a2"] is None and ov["a6"] is None


@pytest.mark.pg
def test_nullout_keeps_rows_that_differ(body):
    _insert(body, _fixture_rows())
    tool.nullout(body, "article_body_pmc", batch_rows=3, log=lambda m: None)
    assert _overrides(body)["a3"] == "different text"


@pytest.mark.pg
def test_nullout_keeps_the_null_sentinel(body):
    _insert(body, _fixture_rows())
    tool.nullout(body, "article_body_pmc", batch_rows=50, log=lambda m: None)
    assert _overrides(body)["a4"] == ""
    assert _texts(body)["a4"] is None


@pytest.mark.pg
def test_nullout_is_restartable(body):
    _insert(body, _fixture_rows())
    first = tool.nullout(body, "article_body_pmc", batch_rows=2, log=lambda m: None)
    second = tool.nullout(body, "article_body_pmc", batch_rows=2, log=lambda m: None)
    assert first["nulled"] == 3
    assert second["nulled"] == 0 and second["rows"] == 6


@pytest.mark.pg
def test_resume_from_logged_key_matches_one_full_run(body):
    rows = [(f"id,{n:02d}", "T", "A", "B", "T\n\nA\n\nB") for n in range(7)]
    rows.append(("id,07", "T", "A", "B", "different"))
    _insert(body, rows)
    tool.nullout(body, "article_body_pmc", batch_rows=3, log=lambda m: None)
    full = _overrides(body)
    with body.cursor() as cur:
        cur.execute("DELETE FROM episteme.article_body")
    body.commit()
    _insert(body, rows)

    class Stop(Exception):
        pass

    logged = []

    def log_then_stop(msg):
        logged.append(msg)
        raise Stop

    with pytest.raises(Stop):
        tool.nullout(body, "article_body_pmc", batch_rows=3, log=log_then_stop)
    # the first batch committed; later rows are untouched
    partial = _overrides(body)
    assert partial["id,00"] is None and partial["id,05"] is not None
    key = logged[0].rsplit("last_key=", 1)[1]
    start = tool.parse_start_after(key)
    assert start == ("id,02", "pmc")
    tool.nullout(body, "article_body_pmc", batch_rows=3, log=lambda m: None, start_after=start)
    assert _overrides(body) == full


@pytest.mark.pg
def test_text_unchanged_after_nullout(body):
    _insert(body, _fixture_rows())
    before = _texts(body)
    tool.nullout(body, "article_body_pmc", batch_rows=2, log=lambda m: None)
    assert _texts(body) == before


@pytest.mark.pg
def test_precheck_post_0014_reports_phase_and_is_ok(body):
    _insert(body, _fixture_rows() + [("a7", None, None, None, "")])
    res = tool.precheck(body, "article_body_pmc", batch_rows=2, log=lambda m: None)
    assert res["phase"] == "post-0014" and res["column"] == "text_override"
    assert res["rows"] == 7
    # a4: sentinel with parts; a7: sentinel with all parts blank. A NULL override is
    # normal after 0014 and is not counted.
    assert res["sentinel_with_parts"] == 1
    assert res["sentinel_all_blank"] == 1
    assert res["ok"] is True


# ---- pre-0014 shape: scratch schema t3pre, stored column ``text``

PRE = "t3pre"
PRE_TABLE = f"{PRE}.article_body"
_PRE_SHAPE = f"""
DROP SCHEMA IF EXISTS {PRE} CASCADE;
CREATE SCHEMA {PRE};
CREATE TABLE {PRE}.article_body (
    article_id  text NOT NULL,
    source      text NOT NULL,
    year        int,
    title       text,
    abstract    text,
    body_text   text,
    text        text,
    PRIMARY KEY (article_id, source)
) PARTITION BY LIST (source);
CREATE TABLE {PRE}.article_body_pmc
    PARTITION OF {PRE}.article_body FOR VALUES IN ('pmc');
"""


@pytest.fixture
def pre(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(_PRE_SHAPE)
    pg_conn.commit()
    try:
        yield pg_conn
    finally:
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {PRE} CASCADE")
        pg_conn.commit()


def _pre_insert(conn, rows):
    with conn.cursor() as cur:
        for aid, title, abstract, body_text, text in rows:
            cur.execute(
                f"INSERT INTO {PRE}.article_body "
                "(article_id, source, title, abstract, body_text, text) "
                "VALUES (%s, 'pmc', %s, %s, %s, %s)",
                (aid, title, abstract, body_text, text),
            )
    conn.commit()


def _pre_check(conn):
    return tool.precheck(
        conn, "article_body_pmc", batch_rows=2, log=lambda m: None, table=PRE_TABLE
    )


@pytest.mark.pg
def test_precheck_pre_0014_counts_null_text_with_parts(pre):
    _pre_insert(
        pre,
        [
            ("p1", "T", "A", "B", None),  # NULL text, parts: counted
            ("p2", None, "A", None, None),  # NULL text, parts: counted
            ("p3", " \t", None, None, None),  # NULL text, all blank: not counted
            ("p4", "T", None, None, "T"),
        ],
    )
    res = _pre_check(pre)
    assert res["phase"] == "pre-0014" and res["column"] == "text"
    assert res["null_with_parts"] == 2
    assert res["empty_string"] == 0
    assert res["ok"] is False


@pytest.mark.pg
def test_precheck_pre_0014_counts_empty_text(pre):
    _pre_insert(
        pre,
        [("p1", "T", "A", None, ""), ("p2", None, None, None, ""), ("p3", "T", None, None, "T")],
    )
    res = _pre_check(pre)
    assert res["empty_string"] == 2
    assert res["null_with_parts"] == 0
    assert res["ok"] is False


@pytest.mark.pg
def test_precheck_pre_0014_clean_is_ok(pre):
    _pre_insert(
        pre,
        [
            ("p1", "T", "A", "B", "T\n\nA\n\nB"),
            ("p2", None, None, None, None),  # NULL text, no parts: fine
            ("p3", "T", None, None, "different"),
            ("p4", "T", None, None, "T"),
            ("p5", "T", None, None, "T"),
        ],
    )
    res = _pre_check(pre)
    assert res["rows"] == 5
    assert res["null_with_parts"] == 0 and res["empty_string"] == 0
    assert res["ok"] is True


@pytest.mark.parametrize(
    "table", ["episteme.article_body; drop", "episteme.articles", "Episteme.article_body", ""]
)
def test_table_name_refused_before_any_sql(table):
    with pytest.raises(tool.TextOverrideError):
        tool.validate_partition(_NoDbConn(), "article_body_pmc", table)


@pytest.mark.pg
def test_nullout_refuses_a_non_partition_name(body):
    with pytest.raises(tool.TextOverrideError):
        tool.nullout(body, "article_body_nope", batch_rows=10, log=lambda m: None)


# ------------------------------------------------------------ physical order (pg)


def _phys(conn, **kw):
    kw.setdefault("batch_blocks", 1)
    return tool.nullout_physical(conn, "article_body_pmc", log=kw.pop("log", lambda m: None), **kw)


def _spread_rows():
    """Enough wide rows to fill several heap blocks."""
    import hashlib

    def pad(n):
        return "".join(hashlib.md5(f"{n}-{i}".encode()).hexdigest() for i in range(60))

    rows = [(f"p{n:03d}", "T", "A", pad(n), f"T\n\nA\n\n{pad(n)}") for n in range(60)]
    rows += [(f"q{n:03d}", "T", "A", pad(n), "different") for n in range(10)]
    return rows


@pytest.mark.pg
def test_physical_nullout_nulls_only_equal_rows(body):
    _insert(body, _fixture_rows())
    res = _phys(body)
    ov = _overrides(body)
    assert res["nulled"] == 3
    assert ov["a1"] is None and ov["a2"] is None and ov["a6"] is None
    assert ov["a3"] == "different text" and ov["a4"] == "" and ov["a5"] is None


@pytest.mark.pg
def test_physical_nullout_is_restartable_from_logged_block(body):
    rows = _spread_rows()
    _insert(body, rows)
    _phys(body)
    full = _overrides(body)
    with body.cursor() as cur:
        cur.execute("DELETE FROM episteme.article_body")
    body.commit()
    _insert(body, rows)

    class Stop(Exception):
        pass

    logged = []

    # Observe the real layout: the DELETE left dead tuples, so block 0 may hold no live row.
    with body.cursor() as cur:
        cur.execute(
            "SELECT (ctid::text::point)[0]::bigint FROM episteme.article_body "
            "WHERE article_id LIKE 'p%' ORDER BY 1"
        )
        eq_blocks = [r[0] for r in cur.fetchall()]
    body.commit()
    first_eq = eq_blocks[0]
    assert eq_blocks[-1] > first_eq, eq_blocks  # equal rows remain after the stop point

    def stop_after_first_equal_block(msg):
        logged.append(msg)
        if int(msg.rsplit("last_block=", 1)[1]) > first_eq:
            raise Stop

    with pytest.raises(Stop):
        _phys(body, log=stop_after_first_equal_block)
    last_block = int(logged[-1].rsplit("last_block=", 1)[1])
    assert last_block == first_eq + 1
    partial = _overrides(body)
    assert any(v is None for v in partial.values()) and any(v is not None for v in partial.values())
    _phys(body, start_block=last_block)
    assert _overrides(body) == full


@pytest.mark.pg
def test_physical_nullout_text_unchanged(body):
    _insert(body, _fixture_rows() + _spread_rows())
    before = _texts(body)
    _phys(body)
    assert _texts(body) == before


@pytest.mark.pg
def test_physical_update_uses_tid_range_scan(body):
    _insert(body, _fixture_rows())
    stmt = tool.sql.SQL(
        "EXPLAIN UPDATE {rel} SET text_override = NULL"
        " WHERE ctid >= %s::tid AND ctid < %s::tid AND " + tool._NULLOUT_PREDICATE
    ).format(rel=tool._rel("article_body_pmc"))
    with body.cursor() as cur:
        cur.execute("SET LOCAL enable_seqscan = off")  # as nullout_physical does per step
        cur.execute(stmt, ("(0,0)", "(4096,0)"))
        plan = "\n".join(r[0] for r in cur.fetchall())
    body.rollback()
    assert "Tid Range Scan" in plan, plan


def test_physical_step_forbids_seq_scan_before_each_update():
    conn = _StubConn([])
    conn.end_block = 3
    tool.nullout_physical(conn, "article_body_pmc", batch_blocks=1, log=lambda m: None)
    steps = [q for q in conn.executed if q.startswith(("SET LOCAL", "UPDATE"))]
    assert steps == ["SET LOCAL enable_seqscan = off", steps[1]] * 3
    assert steps[1].startswith("UPDATE")


def test_physical_start_block_past_end_logs_nothing_to_do():
    conn = _StubConn([])
    conn.end_block = 3
    logs = []
    res = tool.nullout_physical(
        conn, "article_body_pmc", batch_blocks=1, start_block=3, log=logs.append
    )
    assert any("start_block 3 >= end_block 3: nothing to do" in m for m in logs)
    assert not any(q.startswith("UPDATE") for q in conn.executed)
    assert res["nulled"] == 0


@pytest.mark.parametrize(
    "v,ok",
    [
        ("19beta3", True),
        ("19.0", True),
        ("19.2", True),
        ("19rc1", True),
        ("18.3", False),
        ("190.1", False),
        ("20.0", False),
        ("", False),
    ],
)
def test_supported_server_version(v, ok):
    assert tool.is_supported_server_version(v) is ok
