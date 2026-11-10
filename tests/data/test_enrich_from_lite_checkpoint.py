"""Europe PMC lite enrichment: no-op rows write nothing, one commit per tar member, and a
re-run skips finished members. Non-pg: a fake connection records the statements."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from episteme.data.europepmc.lite_metadata import enrich_from_lite as mod


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append((sql, params))
        n = (len(params) // 4) if params else 0
        self.rowcount = n if self.conn.update_all else 0


class FakeConn:
    def __init__(self, update_all=False):
        self.statements: list = []
        self.commits = 0
        self.update_all = update_all

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


def _rec(pmcid: str, journal="J", year="2020") -> str:
    return (
        f"<PMC_ARTICLE><pmcid>{pmcid}</pmcid><JournalTitle>{journal}</JournalTitle>"
        f"<PubYear>{year}</PubYear></PMC_ARTICLE>"
    )


def _tgz(tmp_path: Path, members: dict[str, list[str]], name="lite.tgz") -> Path:
    path = tmp_path / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tf:
        for member, pmcids in members.items():
            xml = ("<PMCSet>" + "".join(_rec(p) for p in pmcids) + "</PMCSet>").encode()
            info = tarfile.TarInfo(name=member)
            info.size = len(xml)
            tf.addfile(info, io.BytesIO(xml))
    return path


def test_update_only_touches_rows_where_a_column_would_change():
    sql = mod._UPDATE_SQL
    assert "a.journal IS NULL AND v.journal IS NOT NULL" in sql
    assert "v.year IS NOT NULL AND v.year <> 0" in sql
    assert "v.mesh IS NOT NULL AND v.mesh <> '{}'" in sql


def test_matched_and_updated_are_reported_apart(tmp_path):
    tgz = _tgz(tmp_path, {"out/PMC.1.xml": ["PMC1", "PMC2", "PMC3"]})
    conn = FakeConn(update_all=False)  # the database changes nothing: rowcount 0
    res = mod.enrich_from_lite(tgz, conn)
    assert (res["records_read"], res["rows_matched"], res["rows_updated"]) == (3, 3, 0)
    conn = FakeConn(update_all=True)
    res = mod.enrich_from_lite(tgz, conn)
    assert res["rows_matched"] == 3 and res["rows_updated"] == 3


def test_commit_per_member_and_resume_skips_finished_members(tmp_path):
    tgz = _tgz(tmp_path, {"out/PMC.1.xml": ["PMC1"], "out/PMC.2.xml": ["PMC2"]})
    processed = tmp_path / "proc"
    conn = FakeConn()
    kw = {"processed_root": processed, "raw_dir": tmp_path / "raw"}
    res = mod.enrich_from_lite(tgz, conn, **kw)
    assert conn.commits == 2 and len(conn.statements) == 2
    assert res["members_done"] == 2 and res["members_skipped"] == 0
    # re-run: every member has a marker, nothing runs
    conn2 = FakeConn()
    res2 = mod.enrich_from_lite(tgz, conn2, **kw)
    assert conn2.statements == [] and conn2.commits == 0
    assert res2["members_skipped"] == 2 and res2["records_read"] == 0
    # --force redoes
    conn3 = FakeConn()
    res3 = mod.enrich_from_lite(tgz, conn3, force=True, **kw)
    assert len(conn3.statements) == 2 and res3["members_done"] == 2


def test_a_new_file_of_the_same_name_is_not_skipped(tmp_path):
    kw = {"processed_root": tmp_path / "proc", "raw_dir": tmp_path / "raw"}
    tgz = _tgz(tmp_path, {"out/PMC.1.xml": ["PMC1"]})
    mod.enrich_from_lite(tgz, FakeConn(), **kw)
    tgz = _tgz(tmp_path, {"out/PMC.1.xml": ["PMC1", "PMC9"]})  # replaced weekly file
    conn = FakeConn()
    mod.enrich_from_lite(tgz, conn, **kw)
    assert len(conn.statements) == 1


def test_commit_every_n_batches_inside_a_member(tmp_path, monkeypatch):
    tgz = _tgz(tmp_path, {"out/PMC.1.xml": [f"PMC{i}" for i in range(10)]})
    conn = FakeConn()
    mod.enrich_from_lite(
        tgz,
        conn,
        processed_root=tmp_path / "proc",
        raw_dir=tmp_path / "raw",
        batch_size=2,
        commit_batches=2,
    )
    # 5 batches: commits after batches 2 and 4, then one at the member end
    assert len(conn.statements) == 5 and conn.commits == 3


def test_marker_keys_do_not_collide(tmp_path):
    raw = tmp_path / "raw"
    a, b = raw / "x" / "lite.tgz", raw / "y" / "lite.tgz"
    keys = {
        mod.member_key(a, raw, "a/b.xml"),
        mod.member_key(a, raw, "a__b.xml"),
        mod.member_key(a, raw, "a%2Fb.xml"),
        mod.member_key(b, raw, "a/b.xml"),
    }
    assert len(keys) == 4
