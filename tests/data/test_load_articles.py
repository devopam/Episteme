"""SP7 fix-wave (I1, whole-branch review Ruling FW7-1): load_articles' --only
shard filter, and a pg-marked integration proof that the cdisc_ct load path
(load_articles driven by retire.current_shard_names) loads only each
package's newest shard, never an older one sitting in the same staging dir.

Only the last test is ``pg``-marked; the rest never open a DB connection.
"""

from __future__ import annotations

import importlib
from contextlib import nullcontext
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp7" / "cdisc_ct" / "SDTM_Terminology.txt"


def test_filter_only_none_or_empty_keeps_everything(tmp_path):
    from episteme.data.load_articles import _filter_only

    shards = [tmp_path / "a.parquet", tmp_path / "b.jsonl"]
    assert _filter_only(shards, None) == shards
    assert _filter_only(shards, []) == shards


def test_filter_only_keeps_named_shards_and_drops_the_rest(tmp_path):
    from episteme.data.load_articles import _filter_only

    a, b, c = tmp_path / "a.parquet", tmp_path / "b.parquet", tmp_path / "c.jsonl"
    shards = [a, b, c]
    assert _filter_only(shards, ["a.parquet", "c.jsonl"]) == [a, c]
    assert _filter_only(shards, ["nonexistent.parquet"]) == []


def test_only_filters_before_any_db_connection_is_opened(tmp_path, capsys):
    """When --only names shards that don't exist, main() must fail loudly
       (rc 1, an error naming the requested shard) without ever reaching
       `connection()` -- proven here by NOT patching a DB at all. Silently
       returning 0 hid a real Windows bug: a trailing
    on every name made
       load_cdisc_ct.sh load nothing while reporting "no shards"."""
    from episteme.data import load_articles

    staging = tmp_path / "staging" / "cdisc_ct"
    staging.mkdir(parents=True)
    (staging / "keep_me.parquet").write_bytes(b"")
    (staging / "drop_me.parquet").write_bytes(b"")

    rc = load_articles.main(
        [
            "--source",
            "cdisc_ct",
            "--processed-dir",
            str(tmp_path),
            "--only",
            "nonexistent.parquet",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "nonexistent.parquet" in err


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


def _write_sdtm_release(raw: Path, processed: Path, date: str) -> str:
    """Write one SDTM release under raw/SDTM/<date>/ and its staging shard
    under processed/staging/cdisc_ct/, mirroring what download+serialize do.
    Returns the release's bare input_key (no shard extension)."""
    from episteme.data import checkpoint_markers
    from episteme.data.cdisc_ct.ct_parse import build_rows, parse_ct_file
    from episteme.data.staging_writer import write_rows

    d = raw / "SDTM" / date
    d.mkdir(parents=True)
    target = d / "SDTM_Terminology.txt"
    target.write_bytes(FX.read_bytes())

    basename = checkpoint_markers.input_key(target, raw)
    codelists, _counts = parse_ct_file(target)
    rows = build_rows(codelists, package="SDTM", release_date=date, source_file=basename)
    write_rows(rows, processed, source="cdisc_ct", source_file=basename, prefer_parquet=True)
    return basename


@pytest.mark.pg
def test_only_loads_the_newer_sdtm_shard_and_never_the_older(
    pg_conn, monkeypatch, tmp_path, capsys
):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    raw = tmp_path / "raw" / "cdisc_ct"
    processed = tmp_path / "processed"

    older = _write_sdtm_release(raw, processed, "1999-01-01")
    newer = _write_sdtm_release(raw, processed, "1999-04-01")

    # Both releases' shards now sit side by side in the staging dir, both
    # unloaded -- exactly the I1 scenario. current_shard_names must resolve
    # to the newer one only (discover_cdisc_ct_files only ever returns the
    # newest release per package).
    from episteme.data.cdisc_ct import retire

    only = retire.current_shard_names(raw, processed)
    assert len(only) == 1
    assert only[0].startswith(newer)
    assert not any(name.startswith(older) for name in only)

    import episteme.data.db.connection as conn_mod
    from episteme.data import load_articles

    # load_articles binds `connection` at import time. If a non-pg test in the
    # same session imported it first, that binding is tests/conftest.py's
    # blocked stand-in; reload so this pg test gets the real one.
    importlib.reload(load_articles)
    conn_mod.get_settings.cache_clear()
    monkeypatch.setattr(conn_mod, "_POOL", None)
    try:
        rc = load_articles.main(
            ["--source", "cdisc_ct", "--processed-dir", str(processed), "--only", only[0]]
        )
    finally:
        if conn_mod._POOL is not None:
            conn_mod._POOL.close()
        conn_mod.get_settings.cache_clear()

    assert rc == 0
    out = capsys.readouterr().out
    assert older not in out  # never even mentioned: not loaded, not reported as failed
    assert newer in out

    pg_conn.rollback()  # see rows committed by load_articles' own connection
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source='cdisc_ct' AND source_file=%s",
            (newer,),
        )
        assert cur.fetchone()[0] > 0
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source='cdisc_ct' AND source_file=%s",
            (older,),
        )
        assert cur.fetchone()[0] == 0


# --- fail-fast, retry and --skip-unit (operating standard PR 2, Task 5) ------


class _FakeConn:
    info = type("Info", (), {"backend_pid": 4242})()

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def ff(monkeypatch, tmp_path):
    """load_articles.main with no DB: fake connection, fake load_source_file,
    audit rows collected, orphan sweep and stage_guard stubbed."""
    from contextlib import contextmanager, nullcontext
    from types import SimpleNamespace

    import episteme.ops.failure as failure
    import episteme.ops.skip as skip
    from episteme.data import load_articles as la

    # No module reload: patch the settings seam of the two modules that read it.
    settings = SimpleNamespace(processed_root=tmp_path, run_id=None)
    monkeypatch.setattr(la, "get_settings", lambda: settings)
    monkeypatch.setattr(failure, "get_settings", lambda: settings)

    staging = tmp_path / "staging" / "src"
    staging.mkdir(parents=True)
    for n in ("a.jsonl", "b.jsonl", "c.jsonl"):
        (staging / n).write_text("", encoding="utf-8")

    @contextmanager
    def _connection():
        yield _FakeConn()

    rows = []

    def _record(event, **kw):
        rows.append((event, kw.get("object"), kw.get("reason")))
        return "h"

    state = {"calls": [], "behaviour": {}}

    def _load(conn, *, source, staging_path, run_id):
        name = Path(staging_path).name
        state["calls"].append(name)
        b = state["behaviour"].get(name)
        if isinstance(b, list):
            if b:
                raise b.pop(0)
        elif b is not None:
            raise b
        return {
            "rows": 1,
            "deleted": 0,
            "body_deleted": 0,
            "event": "load_commit",
            "read_sec": 0,
            "copy_sec": 0,
            "rows_per_sec": 0,
        }

    for mod in (la, skip):
        monkeypatch.setattr(mod, "connection", _connection)
        monkeypatch.setattr(mod.audit_trail, "record", _record)
    monkeypatch.setattr(la.postgres_loader, "load_source_file", _load)
    monkeypatch.setattr(la.orphans, "sweep", lambda conn: [])
    monkeypatch.setattr(la, "stage_guard", lambda ref, **kw: nullcontext())
    sleeps = []
    monkeypatch.setattr(la, "_SLEEP", sleeps.append)

    def run(*extra):
        return la.main(["--source", "src", "--processed-dir", str(tmp_path), *extra])

    class NS:
        pass

    ns = NS()
    ns.run, ns.rows, ns.state, ns.sleeps, ns.root, ns.la = run, rows, state, sleeps, tmp_path, la
    return ns


def test_permanent_failure_stops_the_run_and_names_the_shard(ff, capsys):
    # FLIPPED: the loader used to count the failure and go on to later shards.
    ff.state["behaviour"]["b.jsonl"] = ValueError("bad row")
    assert ff.run() == 1
    assert ff.state["calls"] == ["a.jsonl", "b.jsonl"]  # c.jsonl is never touched
    out = capsys.readouterr().out
    assert "FAIL b.jsonl: bad row" in out
    assert "done shards=3 failed=1 stopped_at=b.jsonl" in out
    import json

    data = json.loads((ff.root / "_ops" / "src" / "last_failure.json").read_text())
    assert data["stage"] == "load" and data["unit"] == "b.jsonl"
    assert "bad row" in data["error"]
    ends = [r for r in ff.rows if r[0] == "run_end"]
    assert len(ends) == 1 and ends[0][2].startswith("failed at shard b.jsonl")
    assert ff.sleeps == []  # permanent: no retry
    from episteme.data import checkpoint_markers as cm

    assert cm.load_success_marker_path(ff.root, "src", "a.jsonl").is_file()
    assert not cm.load_success_marker_path(ff.root, "src", "b.jsonl").is_file()


def test_transient_error_is_retried_and_the_shard_loads_once(ff, capsys):
    import psycopg

    ff.state["behaviour"]["a.jsonl"] = [psycopg.OperationalError("connection lost")]
    assert ff.run() == 0
    assert ff.state["calls"] == ["a.jsonl", "a.jsonl", "b.jsonl", "c.jsonl"]
    assert ff.sleeps == [30]
    assert "done shards=3 failed=0" in capsys.readouterr().out


def test_transient_error_past_the_last_retry_is_permanent(ff, capsys):
    import psycopg

    ff.state["behaviour"]["a.jsonl"] = [psycopg.OperationalError("x")] * 4
    assert ff.run() == 1
    assert ff.state["calls"] == ["a.jsonl"] * 4
    assert ff.sleeps == [30, 60, 120]
    assert "stopped_at=a.jsonl" in capsys.readouterr().out


def test_skip_unit_without_reason_exits_2(ff):
    with pytest.raises(SystemExit) as e:
        ff.run("--skip-unit", "b.jsonl")
    assert e.value.code == 2
    assert ff.state["calls"] == []


def test_skip_unit_skips_the_shard_and_records_one_row(ff, capsys):
    assert ff.run("--skip-unit", "b.jsonl", "--reason", "corrupt upstream") == 0
    assert ff.state["calls"] == ["a.jsonl", "c.jsonl"]
    skips = [r for r in ff.rows if r[0] == "unit_skip"]
    assert skips == [("unit_skip", "src b.jsonl", "corrupt upstream")]
    assert "done shards=3 failed=0 skipped_by_operator=1" in capsys.readouterr().out


def test_skip_unit_unknown_name_exits_2_before_loading(ff):
    assert ff.run("--skip-unit", "nope.jsonl", "--reason", "r") == 2
    assert ff.state["calls"] == []
    assert ff.rows == []  # validated before any DB work: no run_start either


def test_stop_writes_one_run_end_and_exits_130(ff, monkeypatch):
    from episteme.ops import stop

    # real stage_guard, with its watchdog thread neutralised
    monkeypatch.setattr(stop, "start_parent_watchdog", lambda cancel, **kw: None)
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)
    monkeypatch.setattr(ff.la, "stage_guard", stop.stage_guard)
    from episteme.ops import lock

    monkeypatch.setattr(lock, "advisory_lock", lambda **kw: nullcontext())  # no real DB
    ff.state["behaviour"]["a.jsonl"] = KeyboardInterrupt()
    with pytest.raises(SystemExit) as e:
        ff.run()
    assert e.value.code == 130
    ends = [r for r in ff.rows if r[0] == "run_end"]
    assert len(ends) == 1 and "KeyboardInterrupt" in ends[0][2]
    assert ff.state["calls"] == ["a.jsonl"]


def test_skip_unit_with_no_shards_exits_2(ff):
    for f in (ff.root / "staging" / "src").iterdir():
        f.unlink()
    assert ff.run("--skip-unit", "a.jsonl", "--reason", "r") == 2
    assert ff.rows == []


def test_transient_sweep_error_is_retried(ff, monkeypatch, capsys):
    import psycopg

    errors = [psycopg.OperationalError("connection refused")]

    def flaky(conn):
        if errors:
            raise errors.pop(0)
        return []

    monkeypatch.setattr(ff.la.orphans, "sweep", flaky)
    assert ff.run() == 0
    assert ff.sleeps == [30]
    assert ff.state["calls"] == ["a.jsonl", "b.jsonl", "c.jsonl"]


def test_sweep_error_writes_one_run_end(ff, monkeypatch):
    def boom(conn):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(ff.la.orphans, "sweep", boom)
    with pytest.raises(RuntimeError):
        ff.run()
    ends = [r for r in ff.rows if r[0] == "run_end"]
    assert len(ends) == 1 and ends[0][2].startswith("failed: RuntimeError")


def test_mirror_failure_after_commit_is_permanent_not_retried(ff, monkeypatch, capsys):
    """The commit succeeds, then the mirror append raises OSError(EIO). EIO alone
    is transient, but a retry would commit the shard twice: AuditMirrorError is
    permanent, so there is no retry and no sleep, and one load_commit only."""
    import errno
    import json

    from episteme import audit_trail

    def _record(event, **kw):
        ff.rows.append((event, kw.get("object"), kw.get("reason")))
        queue = audit_trail._DEFERRED.get(kw.get("conn"))
        if queue is not None:  # what the real record() does inside deferred_mirror
            queue.append({"event_type": event, "record_hash": "h"})
        return "h"

    monkeypatch.setattr(audit_trail, "record", _record)

    def _load(conn, *, source, staging_path, run_id):
        ff.state["calls"].append(Path(staging_path).name)
        audit_trail.record("load_commit", conn=conn, object=source, run_id=run_id)
        return {
            "rows": 1,
            "deleted": 0,
            "body_deleted": 0,
            "event": "load_commit",
            "read_sec": 0,
            "copy_sec": 0,
            "rows_per_sec": 0,
        }

    monkeypatch.setattr(ff.la.postgres_loader, "load_source_file", _load)

    def _broken_mirror(lines):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(audit_trail, "_append_mirror_lines", _broken_mirror)

    assert ff.run() == 1
    assert ff.sleeps == []  # no retry, no sleep
    assert ff.state["calls"] == ["a.jsonl"]
    assert [r for r in ff.rows if r[0] == "load_commit"] == [("load_commit", "src", None)]
    out = capsys.readouterr().out
    assert "FAIL a.jsonl" in out and "stopped_at=a.jsonl" in out
    data = json.loads((ff.root / "_ops" / "src" / "last_failure.json").read_text())
    assert data["unit"] == "a.jsonl" and "AuditMirrorError" in data["error"]
    ends = [r for r in ff.rows if r[0] == "run_end"]
    assert len(ends) == 1 and "AuditMirrorError" in ends[0][2]


def test_skip_unit_is_matched_after_only_filtering(ff):
    assert ff.run("--only", "a.jsonl", "--skip-unit", "b.jsonl", "--reason", "r") == 2


@pytest.mark.pg
def test_transient_error_then_success_writes_one_load_commit(pg_conn, monkeypatch, tmp_path):
    """Real DB: the first attempt dies with a transient error after writing,
    the retry runs on a fresh connection; exactly one load_commit row remains
    and the shard is loaded once."""
    import psycopg

    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    raw, processed = tmp_path / "raw" / "cdisc_ct", tmp_path / "processed"
    key = _write_sdtm_release(raw, processed, "1999-01-01")

    import episteme.data.db.connection as conn_mod
    from episteme.data import load_articles

    importlib.reload(load_articles)
    conn_mod.get_settings.cache_clear()
    monkeypatch.setattr(conn_mod, "_POOL", None)
    monkeypatch.setattr(load_articles, "_SLEEP", lambda s: None)
    real = load_articles.postgres_loader.load_source_file
    calls = []

    def flaky(conn, **kw):
        calls.append(1)
        if len(calls) == 1:
            real(conn, **kw)  # write, then die before the commit
            raise psycopg.OperationalError("connection lost")
        return real(conn, **kw)

    monkeypatch.setattr(load_articles.postgres_loader, "load_source_file", flaky)
    try:
        rc = load_articles.main(["--source", "cdisc_ct", "--processed-dir", str(processed)])
    finally:
        if conn_mod._POOL is not None:
            conn_mod._POOL.close()
        conn_mod.get_settings.cache_clear()

    assert rc == 0 and len(calls) == 2
    # the failed attempt left no audit mirror line: lines == committed DB rows
    from episteme import audit_trail

    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme._audit")
        db_rows = cur.fetchone()[0]
    mirror_lines = sum(
        1
        for f in audit_trail._mirror_dir().glob("audit-*.jsonl")
        for ln in f.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    )
    assert mirror_lines == db_rows
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme._audit "
            "WHERE event_type IN ('load_commit','load_replace')"
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source='cdisc_ct' AND source_file=%s",
            (key,),
        )
        assert cur.fetchone()[0] > 0
