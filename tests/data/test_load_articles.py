"""SP7 fix-wave (I1, whole-branch review Ruling FW7-1): load_articles' --only
shard filter, and a pg-marked integration proof that the cdisc_ct load path
(load_articles driven by retire.current_shard_names) loads only each
package's newest shard, never an older one sitting in the same staging dir.

Only the last test is ``pg``-marked; the rest never open a DB connection.
"""

from __future__ import annotations

import importlib
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
