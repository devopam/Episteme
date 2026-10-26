"""PR 89 review fix A: retire the rows of deleted pubchem parts.

The ``not pg`` tests cover ``retire.retire_plan`` (the keep-set and the prefix
per unit, and every skip case) and the CLI wiring with the database calls
patched. The ``pg`` test runs serialize -> load -> re-serialize to fewer parts
-> load -> retire against ``episteme_test``.
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


def _mark_loaded(out: Path, key: str, n: int, ext: str = ".parquet") -> None:
    p = load_success_marker_path(out, "pubchem", f"{mod.part_source_file(key, n)}{ext}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")


def _parts(key: str, ns) -> list[str]:
    return [mod.part_source_file(key, n) for n in ns]


def test_plan_keeps_the_current_parts_and_scopes_to_the_unit(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    plan, skipped = retire.retire_plan(out)
    assert plan == [{"key": KEY, "prefix": f"{KEY}__part", "keep": _parts(KEY, (1, 2, 3))}]
    assert skipped == []


def test_plan_after_a_rebuild_to_fewer_parts(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    _serialize_shrunk(raw, out, monkeypatch)
    # The rebuild dropped every load marker: not loaded yet.
    plan, skipped = retire.retire_plan(out)
    assert plan == [] and len(skipped) == 1 and "not loaded" in skipped[0]
    for n in (1, 2):
        _mark_loaded(out, KEY, n)
    plan, _ = retire.retire_plan(out)
    assert plan == [{"key": KEY, "prefix": f"{KEY}__part", "keep": _parts(KEY, (1, 2))}]


def _serialize_shrunk(raw: Path, out: Path, monkeypatch) -> None:
    p = raw / "CID-SMILES.tsv"
    st = p.stat()
    p.write_bytes("".join(f"{c}\tCCO\n" for c in range(1, 6)).encode())
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    _serialize(monkeypatch, raw, out)


def test_plan_skips_a_unit_with_a_part_not_loaded(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    _mark_loaded(out, KEY, 1)
    _mark_loaded(out, KEY, 3)
    plan, skipped = retire.retire_plan(out)
    assert plan == []
    assert skipped == [f"{KEY}: 1 of 3 parts not loaded (first: {KEY}__part00002) -- skipped"]


def test_plan_accepts_a_jsonl_load_marker(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n, ext=".jsonl")
    plan, _ = retire.retire_plan(out)
    assert [e["key"] for e in plan] == [KEY]


def test_plan_skips_a_unit_without_a_primary_marker(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    success_marker_path(out, "pubchem", KEY).unlink()  # a serialize in progress
    plan, skipped = retire.retire_plan(out)
    assert plan == [] and skipped == []  # part markers are not units


def test_plan_skips_a_primary_marker_without_the_parts_format(tmp_path):
    out = tmp_path / "out"
    m = success_marker_path(out, "pubchem", KEY)
    m.parent.mkdir(parents=True)
    m.write_text(json.dumps({"source_file": KEY, "stats": {"n_rows": 10}}), encoding="utf-8")
    plan, skipped = retire.retire_plan(out)
    assert plan == [] and skipped == [
        f"{KEY}: the primary success marker has no parts format -- skipped"
    ]


def test_plan_skips_a_unit_with_zero_parts(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "CID-SMILES.tsv").write_bytes(b"")
    out = tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    plan, skipped = retire.retire_plan(out)
    assert plan == [] and skipped == [f"{KEY}: the unit has 0 parts -- skipped (no keep-set)"]


def test_plan_with_no_markers_is_empty(tmp_path):
    assert retire.retire_plan(tmp_path / "out") == ([], [])


def test_plan_gives_each_unit_its_own_prefix(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    _make_raw(raw / "a", list(range(1, 11)))
    _make_raw(raw / "b", list(range(1, 11)))
    out = tmp_path / "out"
    monkeypatch.setattr(mod, "_part_caps", lambda: (4, 10**9))
    monkeypatch.setattr(mod, "_best_effort_audit", lambda name, n: None)
    assert mod.serialize_pubchem(raw, out)["ok"] == 2
    for key in ("a__" + KEY, "b__" + KEY):
        for n in (1, 2, 3):
            _mark_loaded(out, key, n)
    plan, _ = retire.retire_plan(out)
    assert [(e["key"], e["prefix"]) for e in plan] == [
        ("a__" + KEY, f"a__{KEY}__part"),
        ("b__" + KEY, f"b__{KEY}__part"),
    ]
    # Neither prefix covers the other unit's parts.
    assert not plan[1]["keep"][0].startswith(plan[0]["prefix"])
    assert not plan[0]["keep"][0].startswith(plan[1]["prefix"])


def test_retire_calls_retire_source_files_per_unit(tmp_path, monkeypatch):
    raw, out = _make_raw(tmp_path / "raw", list(range(1, 11))), tmp_path / "out"
    _serialize(monkeypatch, raw, out)
    for n in (1, 2, 3):
        _mark_loaded(out, KEY, n)
    calls: list[dict] = []
    guards: list[dict] = []

    class _Conn:
        committed = False

        def commit(self):
            _Conn.committed = True

    class _G:
        def exit_code(self, rc):
            return rc

    @contextlib.contextmanager
    def _guard(conn_ref, **kw):
        guards.append(kw)
        yield _G()

    @contextlib.contextmanager
    def _connection():
        yield _Conn()

    monkeypatch.setattr("episteme.ops.stop.stage_guard", _guard)
    monkeypatch.setattr("episteme.data.db.connection.connection", _connection)
    monkeypatch.setattr(
        retire.postgres_loader,
        "retire_source_files",
        lambda conn, **kw: calls.append(kw) or 7,
    )
    assert retire.main(["--processed-dir", str(out), "--force", "--reason", "r"]) == 0
    assert calls == [
        {
            "source": "pubchem",
            "keep_source_files": _parts(KEY, (1, 2, 3)),
            "run_id": calls[0]["run_id"],
            "scope_prefix": f"{KEY}__part",
        }
    ]
    assert _Conn.committed
    assert guards == [{"lock_stage": "load", "db_lock": True, "source": "pubchem"}]


def test_retire_with_nothing_to_do_takes_no_lock_and_no_connection(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("no lock and no connection when the plan is empty")

    monkeypatch.setattr("episteme.ops.stop.stage_guard", _boom)
    monkeypatch.setattr("episteme.data.db.connection.connection", _boom)
    monkeypatch.setattr(retire.postgres_loader, "retire_source_files", _boom)
    rc = retire.main(
        ["--processed-dir", str(tmp_path / "out"), "--skip-unit", "x.parquet", "--reason", "r"]
    )
    assert rc == 0


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

    # 10 CIDs in 3 parts (1-4, 5-8, 9-10), loaded.
    _serialize(monkeypatch, raw, processed)
    assert _load(monkeypatch, processed) == 0
    assert _retire(monkeypatch, processed) == 0  # nothing stale yet
    pg_conn.rollback()
    assert _ids(pg_conn, "articles") == set(range(1, 11))

    # The input shrinks to CIDs 1-5: a plain serialize rebuilds 2 parts (1-4, 5).
    _serialize_shrunk(raw, processed, monkeypatch)
    assert _load(monkeypatch, processed) == 0
    pg_conn.rollback()
    # The plain load replaced parts 1 and 2 only: part 3 (CIDs 9, 10) is still there.
    assert _ids(pg_conn, "articles") == {1, 2, 3, 4, 5, 9, 10}

    assert _retire(monkeypatch, processed) == 0
    pg_conn.rollback()
    assert _ids(pg_conn, "articles") == {1, 2, 3, 4, 5}
    assert _ids(pg_conn, "article_body") == {1, 2, 3, 4, 5}
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT source_file, count(*) FROM episteme.articles "
            "WHERE source = 'pubchem' GROUP BY 1 ORDER BY 1"
        )
        assert cur.fetchall() == [(f"{KEY}__part00001", 4), (f"{KEY}__part00002", 1)]
        cur.execute(
            "SELECT object, rows_affected FROM episteme._audit "
            "WHERE event_type = 'load_replace' AND object LIKE 'pubchem retire%' "
            "ORDER BY seq DESC LIMIT 1"
        )
        assert cur.fetchone() == (f"pubchem retire {KEY}__part", 2)
