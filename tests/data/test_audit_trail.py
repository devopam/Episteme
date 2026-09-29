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


def test_verify_does_not_double_count_same_day_plain_and_gz(pg_conn, monkeypatch, tmp_path):
    # rotate_audit_logs.sh runs `gzip "$f"`, which writes the .gz then
    # unlinks the source in one step; if that unlink fails (e.g. `chattr -a`
    # couldn't clear the append-only bit without root/CAP_LINUX_IMMUTABLE),
    # both forms are left on disk, and the NEXT run's `gzip "$f"` then finds
    # that same-named .gz already present and refuses to overwrite it
    # non-interactively -- so both the plain audit-X.jsonl and
    # audit-X.jsonl.gz persist for that day. verify() must count that day
    # once, not once per file, or a coexisting stale .gz can silently
    # double-count a day and hide a real mirror_short shortfall.
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
    audit_trail.record(
        "load_commit", conn=pg_conn, object="pmc PMCFIX0002", rows_affected=1, run_id="r1"
    )
    pg_conn.commit()

    mdir = tmp_path / "_ops" / "_audit"
    mirror = next(mdir.glob("audit-*.jsonl"))
    lines = mirror.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(lines) == 3

    # Real shortfall: only the first 2 of 3 mirrored lines actually survive
    # (the 3rd was lost some other way, unrelated to this bug). Simulate a
    # stale same-day .gz coexisting with the plain file by writing the exact
    # same 2 surviving lines into audit-X.jsonl.gz too (the recurring
    # real-world case: a previous run's gzip succeeded, a later run's
    # `gzip "$f"` found that .gz already present and refused to overwrite,
    # so the plain file -- unchanged -- and its .gz both hold the same 2
    # lines).
    mirror.write_text("".join(lines[:2]), encoding="utf-8")
    gz_path = mdir / (mirror.name + ".gz")
    with gzip.open(gz_path, "wt", encoding="utf-8") as fh:
        fh.writelines(lines[:2])

    problems = audit_trail.verify(pg_conn)
    mirror_short = [p for p in problems if p["reason"] == "mirror_short"]
    assert len(mirror_short) == 1, (
        f"expected a real mirror_short (2 lines survive vs 3 table rows), " f"got: {problems}"
    )
    assert mirror_short[0]["mirror"] == 2, (
        "same-day plain+gz coexistence must count that day once (2 lines), "
        f"not double-count it: {mirror_short[0]}"
    )
    assert mirror_short[0]["table"] == 3


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
