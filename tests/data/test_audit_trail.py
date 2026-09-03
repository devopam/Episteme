import importlib

import pytest

pytestmark = pytest.mark.pg


def _setup_schema(conn):
    with (
        conn.cursor() as cur,
        open("src/episteme/data/db/extensions.sql") as ext,
        open("src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
    conn.commit()


def test_hash_chain_and_tamper_detection(pg_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test-actor")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    from episteme import audit_trail

    importlib.reload(audit_trail)

    _setup_schema(pg_conn)
    h1 = audit_trail.record("run_start", conn=pg_conn, object="pmc", run_id="r1")
    h2 = audit_trail.record(
        "load_commit", conn=pg_conn, object="pmc PMCFIX0001", rows_affected=1, run_id="r1"
    )
    pg_conn.commit()

    assert audit_trail.verify(pg_conn) == []
    # chain links
    with pg_conn.cursor() as cur:
        cur.execute("SELECT seq, prev_hash, record_hash FROM episteme._audit ORDER BY seq")
        rows = cur.fetchall()
    assert rows[0][1] == "0" * 64
    assert rows[1][1] == rows[0][2] == h1
    assert rows[1][2] == h2
    # JSONL mirror parity
    mirror = list((tmp_path / "_ops" / "_audit").glob("audit-*.jsonl"))
    assert mirror and len(mirror[0].read_text().splitlines()) == 2

    # tamper: flip a field, verify() must catch it
    with pg_conn.cursor() as cur:
        # superuser test conn can UPDATE the append-only table
        cur.execute("UPDATE episteme._audit SET object = 'TAMPERED' WHERE seq = 1")
    assert audit_trail.verify(pg_conn) != []


def test_bad_event_type_rejected(pg_conn, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    from episteme import audit_trail

    with pytest.raises(ValueError):
        audit_trail.record("not_a_real_event", conn=pg_conn)
