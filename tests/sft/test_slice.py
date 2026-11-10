import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from episteme.sft.slice import build_slice


def _shard(root: Path, source: str, rows: list[dict], year: int = 2024) -> Path:
    d = root / "pretrain" / f"source={source}" / f"year={year}"
    d.mkdir(parents=True, exist_ok=True)
    cols = {
        "text": [r["text"] for r in rows],
        "source": [source] * len(rows),
        "year": [year] * len(rows),
        "id": [r["id"] for r in rows],
        "content_hash": [r["id"] for r in rows],
    }
    if any("subset" in r for r in rows):
        cols["subset"] = [r.get("subset", "commercial") for r in rows]
    path = d / "part-000.parquet"
    pq.write_table(pa.table(cols), path)
    return path


def _rows(prefix: str, n: int, words: int = 10, **extra) -> list[dict]:
    return [
        {"id": f"{prefix}{i}", "text": " ".join([f"{prefix}w{i}"] * words), **extra}
        for i in range(n)
    ]


def _read(out: Path) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((out / "stage_a").glob("part-*.parquet")):
        rows += pq.read_table(f).to_pylist()
    return rows


def _manifest(out: Path) -> dict:
    return json.loads((out / "stage_a" / "manifest.json").read_text(encoding="utf-8"))


def test_excluded_source_shard_is_refused(tmp_path):
    root, out = tmp_path / "c", tmp_path / "o"
    _shard(root, "pmc", _rows("p", 5))
    _shard(root, "dailymed", _rows("d", 5))
    counts = build_slice(root, out, 10_000, weights={}, excluded=("dailymed",), seed=1)
    assert counts["shards_refused_excluded"] == 1
    assert {r["source"] for r in _read(out)} == {"pmc"}
    statuses = {i["source"]: i["status"] for i in _manifest(out)["inputs"]}
    assert statuses["dailymed"] == "refused_excluded"


def test_non_commercial_row_is_dropped_and_counted(tmp_path):
    root, out = tmp_path / "c", tmp_path / "o"
    rows = _rows("p", 4)
    rows[1]["subset"] = "open_metadata"
    rows[3]["subset"] = "noncommercial"
    rows[0]["subset"] = "commercial"
    rows[2]["subset"] = "commercial"
    _shard(root, "pmc", rows)
    counts = build_slice(root, out, 10_000, weights={}, excluded=(), seed=1)
    assert counts["rows_dropped_non_commercial"] == 2
    assert counts["rows_kept"] == 2
    assert sorted(r["id"] for r in _read(out)) == ["p0", "p2"]


def test_weights_shape_the_mix(tmp_path):
    root, out = tmp_path / "c", tmp_path / "o"
    _shard(root, "a", _rows("a", 400))
    _shard(root, "b", _rows("b", 400))
    _shard(root, "c", _rows("c", 50))
    # 4000 tokens each for a and b; ask for 2000 tokens total, 3:1.
    counts = build_slice(root, out, 2000, weights={"a": 3.0, "b": 1.0}, excluded=(), seed=7)
    by = {}
    for r in _read(out):
        by[r["source"]] = by.get(r["source"], 0) + 1
    assert "c" not in by
    assert counts["shards_skipped_no_weight"] == 1
    assert by["a"] > 2 * by["b"]
    assert 1500 <= counts["tokens_kept"] <= 2500


def test_same_seed_same_slice(tmp_path):
    root = tmp_path / "c"
    _shard(root, "a", _rows("a", 300))
    _shard(root, "b", _rows("b", 300))
    c1 = build_slice(root, tmp_path / "o1", 1500, weights={}, excluded=(), seed=42)
    c2 = build_slice(root, tmp_path / "o2", 1500, weights={}, excluded=(), seed=42)
    c3 = build_slice(root, tmp_path / "o3", 1500, weights={}, excluded=(), seed=43)
    assert c1 == c2
    assert _read(tmp_path / "o1") == _read(tmp_path / "o2")
    assert [r["id"] for r in _read(tmp_path / "o1")] != [r["id"] for r in _read(tmp_path / "o3")]
    assert c3["rows_kept"] > 0
    m1, m2 = _manifest(tmp_path / "o1"), _manifest(tmp_path / "o2")
    assert m1["config_hash"] == m2["config_hash"]
    assert m1["counts"] == m2["counts"]


def test_duplicate_rows_are_dropped(tmp_path):
    root, out = tmp_path / "c", tmp_path / "o"
    rows = _rows("p", 3)
    _shard(root, "a", rows)
    _shard(root, "b", rows)
    counts = build_slice(root, out, 100_000, weights={}, excluded=(), seed=1)
    assert counts["rows_kept"] == 3
    assert counts["rows_dropped_duplicate"] == 3


def test_slice_rows_carry_subset_and_source(tmp_path):
    root, out = tmp_path / "c", tmp_path / "o"
    d = root / "pretrain" / "source=pmc" / "year=2024"
    d.mkdir(parents=True)
    # A shard without a source column: the source comes from the path.
    pq.write_table(
        pa.table({"text": ["a b c", "d e f"], "id": ["x1", "x2"], "content_hash": ["1", "2"]}),
        d / "part-000.parquet",
    )
    _shard(root, "pmcx", _rows("q", 2))
    build_slice(root, out, 10_000, weights={}, excluded=(), seed=1)
    rows = _read(out)
    assert {r["subset"] for r in rows} == {"commercial"}
    by_id = {r["id"]: r["source"] for r in rows}
    assert by_id == {"x1": "pmc", "x2": "pmc", "q0": "pmcx", "q1": "pmcx"}
