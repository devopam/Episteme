"""Mirror appends from several worker processes stay whole lines."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from episteme.data.parallel import run_per_file


def _hammer(path: Path, *, raw_dir: Path, n: int) -> dict:
    from episteme import audit_trail

    for i in range(n):
        audit_trail.mirror_only("extract_commit", object=f"{path.name} {i}", note="x" * 2000)
    return {"source_file": path.name, "skipped": False, "ok": True}


def test_mirror_only_takes_the_append_lock(monkeypatch):
    from episteme import audit_trail

    calls: list[Path] = []
    real = audit_trail._mirror_append_lock

    @contextmanager
    def _spy(mirror_dir):
        calls.append(mirror_dir)
        with real(mirror_dir):
            yield

    monkeypatch.setattr(audit_trail, "_mirror_append_lock", _spy)
    audit_trail.mirror_only("extract_commit", object="x")
    assert len(calls) == 1


def test_concurrent_mirror_only_appends_stay_whole_lines(tmp_path, spawn_child_env):
    raw = tmp_path / "raw"
    raw.mkdir()
    files = []
    for k in range(4):
        p = raw / f"w{k}"
        p.write_text("x", encoding="utf-8")
        files.append(p)
    results = run_per_file(_hammer, files, workers=4, raw_dir=raw, n=200)
    assert all(r["ok"] for r in results)

    lines: list[str] = []
    for f in sorted((spawn_child_env / "_ops" / "_audit").glob("audit-*.jsonl")):
        lines += f.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 800
    objects = sorted(json.loads(line)["object"] for line in lines)
    assert objects == sorted(f"w{k} {i}" for k in range(4) for i in range(200))
