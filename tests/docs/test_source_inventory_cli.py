import os
import subprocess
import sys

from episteme.data.source_inventory import collect

from ._repo import REPO


def test_collect_reads_sync_stamp_and_counts(tmp_path):
    d = tmp_path / "chembl"
    d.mkdir()
    (d / "last_sync_utc.txt").write_text("2026-09-01T00:00:00Z\n", encoding="utf-8")
    (tmp_path / "mesh").mkdir()
    out = {r["source"]: r for r in collect(tmp_path, ["chembl", "mesh", "uniprot"], {"chembl": 7})}
    assert out["chembl"] == {"source": "chembl", "last_sync": "2026-09-01T00:00:00Z", "rows": 7}
    assert out["mesh"]["last_sync"] == "" and out["mesh"]["rows"] is None
    assert out["uniprot"]["last_sync"] == ""  # directory absent


def test_collect_finds_nested_stamp(tmp_path):
    d = tmp_path / "openalex" / "data" / "jsonl" / "works"
    d.mkdir(parents=True)
    (d / "last_sync_utc.txt").write_text("2026-09-21T10:00:00Z\n", encoding="utf-8")
    out = collect(tmp_path, ["openalex"], None)
    assert out[0]["last_sync"] == "2026-09-21T10:00:00Z"


def test_collect_newest_stamp_wins(tmp_path):
    top = tmp_path / "src"
    sub = top / "a" / "b"
    sub.mkdir(parents=True)
    (top / "last_sync_utc.txt").write_text("2026-09-01T00:00:00Z", encoding="utf-8")
    (sub / "last_sync_utc.txt").write_text("2026-09-20T00:00:00Z", encoding="utf-8")
    assert collect(tmp_path, ["src"], None)[0]["last_sync"] == "2026-09-20T00:00:00Z"


def test_cli_degrades_when_db_unreachable(tmp_path):
    env = {
        **os.environ,
        "PGDATABASE": "episteme_test",
        "PGPORT": "1",
        "EPISTEME_RAW_ROOT": str(tmp_path),
    }
    p = subprocess.run(
        [sys.executable, "-m", "episteme.data.source_inventory"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert p.returncode == 0, p.stderr[-500:]
    assert "chembl" in p.stdout and "n/a" in p.stdout
