"""OpenAlex serialize discovers works shards only, and shows progress. No DB."""

from __future__ import annotations

from pathlib import Path

from episteme.data.checkpoint_markers import input_key
from episteme.data.openalex import serialize_openalex as so


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def _mixed_tree(raw: Path) -> list[Path]:
    works = [
        _touch(raw / "data" / "jsonl" / "works" / f"updated_date=2026-06-2{d}" / f"part_000{n}.gz")
        for d in (5, 6)
        for n in (0, 1)
    ]
    for entity in ("authors", "institutions", "sources", "concepts", "funders"):
        for n in (0, 1, 2):
            _touch(raw / "data" / "jsonl" / entity / "updated_date=2026-06-25" / f"part_000{n}.gz")
    _touch(raw / "data" / "jsonl" / "works" / "deleted_ids.csv.gz")
    return works


def test_discovery_in_a_mixed_entity_tree_returns_only_works_shards(tmp_path):
    works = _mixed_tree(tmp_path)
    found = so.discover_openalex_files(tmp_path)
    assert len(found) == 4
    assert sorted(found) == sorted(works)
    assert all(f.name != "deleted_ids.csv.gz" for f in found)
    # keys stay relative to the raw dir, so earlier markers remain valid
    assert input_key(found[0], tmp_path).startswith("data__jsonl__works__updated_date=")


def test_a_tree_without_a_works_dir_keeps_the_old_behaviour(tmp_path):
    a = _touch(tmp_path / "updated_date=2026-06-25" / "part_0000.gz")
    b = _touch(tmp_path / "fixture.jsonl")
    assert sorted(so.discover_openalex_files(tmp_path)) == sorted([a, b])


def test_progress_line_is_printed_every_n_files_without_verbose(capsys):
    show = so._progress_printer(total=5, every=2)
    for i in range(5):
        show({"source_file": f"f{i}", "ok": True, "n_rows": 1})
    out = capsys.readouterr().out
    assert "progress 2/5" in out and "progress 4/5" in out and "progress 5/5" in out
    assert "progress 1/5" not in out


def test_default_run_uses_processes(monkeypatch):
    import inspect

    from episteme.data.parallel import run_per_file

    assert inspect.signature(run_per_file).parameters["executor"].default == "process"
