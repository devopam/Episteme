"""PR 89 review fix A (round 2): retire the rows of deleted pubchem parts.

Serialize records each deleted part that has a load_success marker in
``_ops/pubchem/retire_pending.json``. The retire step after a good load
deletes exactly those ``source_file`` values (``= ANY``, index-only) and
empties the file. The ``not pg`` tests cover the pending file on the
serialize side and the retire step with the database calls patched. The
``pg`` test runs serialize -> load -> re-serialize to fewer parts -> load ->
retire against ``episteme_test``.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
from pathlib import Path

import pytest

from episteme.data.checkpoint_markers import load_success_marker_path, success_marker_path
from episteme.data.pubchem import retire
from episteme.data.pubchem import serialize_pubchem as mod

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY = "CID-SMILES.tsv"


def _make_raw(raw: Path, cids: list[int]) -> Path:
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "CID-SMILES.tsv").write_bytes("".join(f"{c}\tCCO\n" for c in cids).encode())
    (raw / "CID-Title.tsv").write_bytes("".join(f"{c}\tEthanol\n" for c in cids).encode())
    return raw


def _serialize(monkeypatch, raw: Path, out: Path, rows: int = 4) -> None:
    monkeypatch.setattr(mod, "_part_caps", lambda: (rows, 10**9))
    monkeypatch.setattr(mod, "_best_effort_audit", lambda name, n: None)
    assert mod.serialize_pubchem(raw, out)["ok"] == 1


def _serialize_shrunk(raw: Path, out: Path, monkeypatch, cids=range(1, 6)) -> None:
    p = raw / "CID-SMILES.tsv"
    st = p.stat()
    p.write_bytes("".join(f"{c}\tCCO\n" for c in cids).encode())
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    _serialize(monkeypatch, raw, out)


def _mark_loaded(out: Path, key: str, n: int, ext: str = ".parquet") -> None:
    p = load_success_marker_path(out, "pubchem", f"{mod.part_source_file(key, n)}{ext}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")


def _parts(key: str, ns) -> list[str]:
    return [mod.part_source_file(key, n) for n in ns]


def _pending_file(out: Path) -> Path:
    return out / "_ops" / "pubchem" / "retire_pending.json"


# --------------------------------------------------------------------------- #
# serialize side: the pending file
# --------------------------------------------------------------------------- #


def test_a_deleted_loaded_part_is_recorded(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    _serialize_shrunk(raw, out, monkeypatch)  # a rebuild deletes all 3 parts
    assert json.loads(_pending_file(out).read_text(encoding="utf-8")) == _parts(KEY, (1, 2, 3))
    assert mod.read_retire_pending(out) == _parts(KEY, (1, 2, 3))


def test_a_deleted_part_without_a_load_marker_is_not_recorded(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    _mark_loaded(out, KEY, 3)  # parts 1 and 2 were never loaded
    _serialize_shrunk(raw, out, monkeypatch)
    assert mod.read_retire_pending(out) == _parts(KEY, (3,))


def test_a_jsonl_load_marker_counts_as_loaded(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    _mark_loaded(out, KEY, 3, ext=".jsonl")
    _serialize_shrunk(raw, out, monkeypatch)
    assert mod.read_retire_pending(out) == _parts(KEY, (3,))


def test_the_pending_file_is_deduplicated(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    _mark_loaded(out, KEY, 3)
    mod.write_retire_pending(out, _parts(KEY, (3,)))
    _serialize_shrunk(raw, out, monkeypatch)
    assert mod.read_retire_pending(out) == _parts(KEY, (3,))


def test_nothing_deleted_writes_no_pending_file(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    res = mod.serialize_pubchem(raw, out)  # same inputs: the unit is skipped
    assert (res["ok"], res["failed"]) == (0, 0)
    assert not _pending_file(out).exists()
    assert mod.read_retire_pending(out) == []


def test_six_digit_loaded_part_is_recorded(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    psf = mod.part_source_file(KEY, 100000)
    (out / "staging" / "pubchem" / f"{psf}.parquet").write_bytes(b"x")
    _mark_loaded(out, KEY, 100000)
    mod._delete_parts_above(out, KEY, 3)
    assert mod.read_retire_pending(out) == [psf]


# --------------------------------------------------------------------------- #
# retire side
# --------------------------------------------------------------------------- #


def _boom(*a, **k):
    raise AssertionError("not expected here")


class _Db:
    """Patched lock, advisory lock, connection and delete for the retire step."""

    def __init__(self, monkeypatch, *, db: bool):
        self.calls: list[dict] = []
        self.guards: list[dict] = []
        self.advisory = 0
        self.committed = 0
        self.on_lock = None  # a hook: runs when the heavy lock is taken
        self.conn_ref = None
        db_self = self

        class _Conn:
            def commit(self):
                db_self.committed += 1

        class _G:
            def exit_code(self, rc):
                return rc

        @contextlib.contextmanager
        def _guard(conn_ref, **kw):
            db_self.guards.append(kw)
            db_self.conn_ref = conn_ref
            if db_self.on_lock is not None:
                db_self.on_lock()
            yield _G()

        @contextlib.contextmanager
        def _advisory(**kw):
            db_self.advisory += 1
            yield

        @contextlib.contextmanager
        def _connection():
            yield _Conn()

        def _delete(conn, **kw):
            # The stop handler can reach the connection of the running delete.
            assert db_self.conn_ref() is conn
            db_self.calls.append(kw)
            return 2

        monkeypatch.setattr("episteme.ops.stop.stage_guard", _guard)
        if db:
            monkeypatch.setattr("episteme.ops.lock.advisory_lock", _advisory)
            monkeypatch.setattr("episteme.data.db.connection.connection", _connection)
            monkeypatch.setattr(retire.postgres_loader, "delete_source_files", _delete)
        else:
            monkeypatch.setattr("episteme.ops.lock.advisory_lock", _boom)
            monkeypatch.setattr("episteme.data.db.connection.connection", _boom)
            monkeypatch.setattr(retire.postgres_loader, "delete_source_files", _boom)


@pytest.fixture
def no_db(monkeypatch):
    """No lock and no database at all."""
    db = _Db(monkeypatch, db=False)
    monkeypatch.setattr("episteme.ops.stop.stage_guard", _boom)
    return db


@pytest.fixture
def lock_only(monkeypatch):
    """The heavy lock (for the file update) but no database."""
    return _Db(monkeypatch, db=False)


@pytest.fixture
def fake_db(monkeypatch):
    return _Db(monkeypatch, db=True)


def _mark_current(out: Path, name: str, *, loaded: bool) -> None:
    m = success_marker_path(out, "pubchem", name)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text("{}", encoding="utf-8")
    if loaded:
        lm = load_success_marker_path(out, "pubchem", f"{name}.parquet")
        lm.parent.mkdir(parents=True, exist_ok=True)
        lm.write_text("{}", encoding="utf-8")


def test_no_pending_file_makes_no_db_call(tmp_path, no_db):
    assert retire.main(["--processed-dir", str(tmp_path / "out")]) == 0


def test_an_empty_pending_file_makes_no_db_call(tmp_path, no_db):
    out = tmp_path / "out"
    mod.write_retire_pending(out, [])
    rc = retire.main(["--processed-dir", str(out), "--skip-unit", "x.parquet", "--reason", "r"])
    assert rc == 0


def test_pending_names_are_deleted_and_the_file_emptied(tmp_path, monkeypatch, fake_db):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    _serialize_shrunk(raw, out, monkeypatch)  # 2 parts now; part 3 is gone
    for n in (1, 2):  # the load of the 2 new parts
        _mark_loaded(out, KEY, n)
    assert retire._retire(out, "run-7") == 0
    # Parts 1 and 2 were written again and loaded: dropped, not deleted.
    assert fake_db.calls == [
        {"source": "pubchem", "source_files": _parts(KEY, (3,)), "run_id": "run-7"}
    ]
    assert fake_db.committed == 1 and fake_db.advisory == 1
    assert fake_db.guards == [{"lock_stage": "load", "source": "pubchem"}]
    assert mod.read_retire_pending(out) == []


def test_a_unit_that_shrinks_to_zero_rows_retires_all_its_parts(tmp_path, monkeypatch, fake_db):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    _serialize_shrunk(raw, out, monkeypatch, cids=[])  # 0 rows: no part is written
    assert retire.main(["--processed-dir", str(out)]) == 0
    assert [c["source_files"] for c in fake_db.calls] == [_parts(KEY, (1, 2, 3))]
    assert mod.read_retire_pending(out) == []


def test_a_name_written_again_and_loaded_is_dropped(tmp_path, monkeypatch, lock_only):
    # A part deleted (loaded), then written again with the same number and
    # loaded: its rows are current. No database call; the names are dropped.
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    _serialize_shrunk(raw, out, monkeypatch, cids=range(1, 12))  # 3 parts again
    assert mod.read_retire_pending(out) == _parts(KEY, (1, 2, 3))
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    assert retire.main(["--processed-dir", str(out)]) == 0
    assert mod.read_retire_pending(out) == []


def test_a_rewritten_part_not_loaded_stays_pending(tmp_path, monkeypatch, lock_only):
    # Part 3 loaded -> a rebuild records it and writes it again -> a load with
    # --skip-unit for part 3 exits 0. Part 3 has a success marker but no load
    # marker: its old rows may hold CIDs that the new part 3 does not. It is
    # neither deleted nor dropped.
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    _serialize_shrunk(raw, out, monkeypatch, cids=range(1, 12))  # 3 parts again
    for n in (1, 2):  # part 3 skipped by --skip-unit
        _mark_loaded(out, KEY, n)
    assert retire.main(["--processed-dir", str(out)]) == 0
    assert mod.read_retire_pending(out) == _parts(KEY, (3,))


def test_skip_unit_sequence_still_retires_the_old_rows_later(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    _serialize_shrunk(raw, out, monkeypatch, cids=range(1, 12))
    for n in (1, 2):  # part 3 skipped
        _mark_loaded(out, KEY, n)
    _Db(monkeypatch, db=False)
    assert retire.main(["--processed-dir", str(out)]) == 0
    # A later shrink deletes part 3 again. It has no load marker now, so this
    # delete records nothing new, but the pending name from before is kept.
    _serialize_shrunk(raw, out, monkeypatch)
    assert _parts(KEY, (3,))[0] in mod.read_retire_pending(out)
    db = _Db(monkeypatch, db=True)
    assert retire.main(["--processed-dir", str(out)]) == 0
    assert [c["source_files"] for c in db.calls] == [_parts(KEY, (3,))]


def test_the_pending_file_is_read_again_under_the_lock(tmp_path, fake_db):
    # A serialize that held the heavy lock while this run waited: it wrote part
    # P again (not loaded yet) and added Q. P must not be deleted, Q not lost.
    out = tmp_path / "out"
    p, q = f"{KEY}__part00002", "other.tsv__part00009"
    mod.write_retire_pending(out, [p])

    def _serialize_ran():
        _mark_current(out, p, loaded=False)
        mod.add_retire_pending(out, [q])

    fake_db.on_lock = _serialize_ran
    assert retire.main(["--processed-dir", str(out)]) == 0
    assert [c["source_files"] for c in fake_db.calls] == [[q]]
    assert mod.read_retire_pending(out) == [p]


def test_a_name_added_after_the_delete_is_kept(tmp_path, monkeypatch, fake_db):
    # The write-back removes only the names this run deleted or dropped.
    out = tmp_path / "out"
    a, late = f"{KEY}__part00004", f"{KEY}__part00005"
    mod.write_retire_pending(out, [a])
    real = retire.postgres_loader.delete_source_files

    def _delete_then_add(conn, **kw):
        n = real(conn, **kw)
        mod.add_retire_pending(out, [late])
        return n

    monkeypatch.setattr(retire.postgres_loader, "delete_source_files", _delete_then_add)
    assert retire.main(["--processed-dir", str(out)]) == 0
    assert mod.read_retire_pending(out) == [late]


def test_a_crash_between_delete_and_file_update_is_idempotent(tmp_path, monkeypatch, fake_db):
    out = tmp_path / "out"
    stale = _parts(KEY, (7,))
    mod.write_retire_pending(out, stale)
    real = retire.write_retire_pending

    def _crash(*a, **k):
        raise OSError("disk went away")

    monkeypatch.setattr(retire, "write_retire_pending", _crash)
    with pytest.raises(OSError):
        retire.main(["--processed-dir", str(out)])
    assert mod.read_retire_pending(out) == stale  # the file survived
    monkeypatch.setattr(retire, "write_retire_pending", real)
    assert retire.main(["--processed-dir", str(out)]) == 0
    assert [c["source_files"] for c in fake_db.calls] == [stale, stale]  # same delete, again
    assert mod.read_retire_pending(out) == []


def test_a_bad_pending_file_fails_without_a_db_call(tmp_path, no_db):
    out = tmp_path / "out"
    p = _pending_file(out)
    p.parent.mkdir(parents=True)
    p.write_text('{"not": "a list"}', encoding="utf-8")
    assert retire.main(["--processed-dir", str(out)]) == 1


def test_delete_source_files_audits_the_deleted_names(monkeypatch):
    from episteme.data import postgres_loader

    executed: list[tuple] = []

    class _Cur:
        rowcount = 3

        def execute(self, sql, params):
            executed.append((sql, params))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    audits: list[dict] = []
    monkeypatch.setattr(
        postgres_loader.audit_trail, "record", lambda ev, **kw: audits.append({"ev": ev, **kw})
    )
    names = ["k__part00003", "k__part00002", "k__part00003"]
    assert postgres_loader.delete_source_files(_Conn(), source="pubchem", source_files=names) == 3
    assert all("= ANY(%s)" in sql and "EXCEPT" not in sql for sql, _ in executed)
    assert [a["ev"] for a in audits] == ["load_replace"]
    assert audits[0]["old_value"] == {"source_files": ["k__part00002", "k__part00003"]}
    assert audits[0]["rows_affected"] == 3
    assert audits[0]["object"] == "pubchem retire 2 source_file(s)"
    with pytest.raises(ValueError):
        postgres_loader.delete_source_files(_Conn(), source="pubchem", source_files=[])


def test_load_wrapper_runs_retire_only_after_a_good_load():
    text = (REPO_ROOT / "scripts/data/pubchem/load_pubchem.sh").read_text(encoding="utf-8")
    code = [ln for ln in text.splitlines() if ln and not ln.lstrip().startswith("#")]
    load = code.index('"$PY" -m episteme.data.load_articles --source pubchem "$@" || exit $?')
    ret = code.index('exec "$PY" -m episteme.data.pubchem.retire "$@"')
    assert load < ret


# --------------------------------------------------------------------------- #
# pg: the rows of a deleted part leave the database
# --------------------------------------------------------------------------- #


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "processed"))
    monkeypatch.setenv("EPISTEME_RAW_ROOT", str(tmp_path / "raw"))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    from episteme import audit_trail

    importlib.reload(audit_trail)


def _setup_schema(conn):
    with (
        conn.cursor() as cur,
        open(REPO_ROOT / "src/episteme/data/db/extensions.sql") as ext,
        open(REPO_ROOT / "src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
    conn.commit()


def _with_pool(monkeypatch, fn):
    import episteme.data.db.connection as conn_mod

    conn_mod.get_settings.cache_clear()
    monkeypatch.setattr(conn_mod, "_POOL", None)
    try:
        return fn()
    finally:
        if conn_mod._POOL is not None:
            conn_mod._POOL.close()
        conn_mod.get_settings.cache_clear()


def _load(monkeypatch, processed: Path) -> int:
    from episteme.data import load_articles

    importlib.reload(load_articles)
    return _with_pool(
        monkeypatch,
        lambda: load_articles.main(["--source", "pubchem", "--processed-dir", str(processed)]),
    )


def _retire(monkeypatch, processed: Path) -> int:
    return _with_pool(monkeypatch, lambda: retire.main(["--processed-dir", str(processed)]))


def _ids(conn, table: str) -> set[int]:
    col = "id" if table == "articles" else "article_id"
    with conn.cursor() as cur:
        cur.execute(f"SELECT {col} FROM episteme.{table} WHERE source = 'pubchem'")
        return {int(r[0].split(":")[1]) for r in cur.fetchall()}


@pytest.mark.pg
def test_retire_deletes_the_rows_of_a_deleted_part(pg_conn, monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    raw = _make_raw(tmp_path / "raw" / "pubchem", list(range(1, 11)))
    processed = tmp_path / "processed"
    stale = f"{KEY}__part00003"
    # Another source's row with the same source_file name must stay.
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file) VALUES (%s, %s, %s)",
            ("mesh:D1", "mesh", stale),
        )
    pg_conn.commit()

    # 10 CIDs in 3 parts (1-4, 5-8, 9-10), loaded. No pending file: no retire.
    _serialize(monkeypatch, raw, processed)
    assert _load(monkeypatch, processed) == 0
    assert _retire(monkeypatch, processed) == 0
    pg_conn.rollback()
    assert _ids(pg_conn, "articles") == set(range(1, 11))

    # The input shrinks to CIDs 1-5: a plain serialize rebuilds 2 parts (1-4, 5)
    # and records the 3 deleted, loaded parts as pending.
    _serialize_shrunk(raw, processed, monkeypatch)
    assert mod.read_retire_pending(processed) == _parts(KEY, (1, 2, 3))
    assert _load(monkeypatch, processed) == 0
    pg_conn.rollback()
    # The plain load replaced parts 1 and 2 only: part 3 (CIDs 9, 10) is still there.
    assert _ids(pg_conn, "articles") == {1, 2, 3, 4, 5, 9, 10}

    assert _retire(monkeypatch, processed) == 0
    pg_conn.rollback()
    assert mod.read_retire_pending(processed) == []
    assert _ids(pg_conn, "articles") == {1, 2, 3, 4, 5}
    assert _ids(pg_conn, "article_body") == {1, 2, 3, 4, 5}
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT source_file, count(*) FROM episteme.articles "
            "WHERE source = 'pubchem' GROUP BY 1 ORDER BY 1"
        )
        assert cur.fetchall() == [(f"{KEY}__part00001", 4), (f"{KEY}__part00002", 1)]
        cur.execute("SELECT count(*) FROM episteme.articles WHERE id = 'mesh:D1'")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT object, rows_affected FROM episteme._audit "
            "WHERE event_type = 'load_replace' AND object LIKE 'pubchem retire%' "
            "ORDER BY seq DESC LIMIT 1"
        )
        assert cur.fetchone() == ("pubchem retire 1 source_file(s)", 2)
        cur.execute(
            "SELECT old_value FROM episteme._audit "
            "WHERE event_type = 'load_replace' AND object LIKE 'pubchem retire%' "
            "ORDER BY seq DESC LIMIT 1"
        )
        assert cur.fetchone()[0] == {"source_files": [stale]}

    # A second retire has nothing pending: no change.
    assert _retire(monkeypatch, processed) == 0
    pg_conn.rollback()
    assert _ids(pg_conn, "articles") == {1, 2, 3, 4, 5}
