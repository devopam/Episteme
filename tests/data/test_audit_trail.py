import gzip
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


def test_verify_counts_gzipped_mirror_lines(pg_conn, monkeypatch, tmp_path):
    # scripts/data/rotate_audit_logs.sh gzips mirror files older than 30 days
    # in place. verify()'s mirror-parity glob must count *.jsonl.gz lines
    # too, or every rotated day silently stops counting toward mirror_lines
    # and verify() starts reporting a false "mirror_short".
    monkeypatch.setenv("EPISTEME_ACTOR", "test-actor")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    from episteme import audit_trail

    importlib.reload(audit_trail)

    _setup_schema(pg_conn)
    audit_trail.record("run_start", conn=pg_conn, object="pmc", run_id="r1")
    audit_trail.record(
        "load_commit", conn=pg_conn, object="pmc PMCFIX0001", rows_affected=1, run_id="r1"
    )
    pg_conn.commit()

    mdir = tmp_path / "_ops" / "_audit"
    mirror = next(mdir.glob("audit-*.jsonl"))
    lines = mirror.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(lines) == 2

    # Simulate rotation: keep line 1 in the plain (still-open) mirror file,
    # move line 2 into a gzip'd file as rotate_audit_logs.sh would produce.
    mirror.write_text(lines[0], encoding="utf-8")
    gz_path = mdir / "audit-20200101.jsonl.gz"
    with gzip.open(gz_path, "wt", encoding="utf-8") as fh:
        fh.write(lines[1])

    problems = audit_trail.verify(pg_conn)
    mirror_short = [p for p in problems if p["reason"] == "mirror_short"]
    assert mirror_short == [], f"gzip'd mirror lines not counted: {mirror_short}"


def test_bad_event_type_rejected(pg_conn, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    from episteme import audit_trail

    with pytest.raises(ValueError):
        audit_trail.record("not_a_real_event", conn=pg_conn)


def test_manual_correction_requires_reason(pg_conn, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    import episteme.config as cfg

    cfg.get_settings.cache_clear()
    from episteme import audit_trail

    with pytest.raises(ValueError, match="reason"):
        audit_trail.record("manual_correction", conn=pg_conn, reason=None)


def test_schema_migration_requires_reason(pg_conn, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    import episteme.config as cfg

    cfg.get_settings.cache_clear()
    from episteme import audit_trail

    with pytest.raises(ValueError, match="reason"):
        audit_trail.record("schema_migration", conn=pg_conn, reason="")


def test_force_override_still_requires_no_new_enforcement_at_record_level(pg_conn, monkeypatch):
    # record() itself does not enforce reason for force_override (that stays a
    # CLI-level rule in load_articles.py); this pins that record() does not
    # newly break the existing force_override call site by demanding a reason
    # it doesn't otherwise validate at this layer.
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    import episteme.config as cfg

    cfg.get_settings.cache_clear()
    from episteme import audit_trail

    audit_trail.record("force_override", conn=pg_conn, reason=None)  # must not raise
    pg_conn.rollback()
