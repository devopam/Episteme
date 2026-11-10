"""Stage A slice: a licence-clean, weighted sample of the materialised corpus.

Shards are read as streams (``ParquetFile.iter_batches``); no whole table is
held in memory. Tokens are whitespace tokens; the real tokenizer runs on the
GPU host. Sampling is deterministic for a given seed and input.

Materialised shards hold commercial rows only and have no ``subset`` column.
If a shard does carry a ``subset`` column, every row must say ``commercial``.
Every output row carries ``subset = 'commercial'`` and its ``source`` (from the shard
path when the shard has no ``source`` column), so Stage B can verify both.
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from episteme.sft.manifest import write_manifest

_BATCH_ROWS = 2048
_ROWS_PER_PART = 50_000
_OUT_COLUMNS = ("text", "source", "year", "id", "pmid", "doi", "license", "content_hash")


def _tokens(text: str | None) -> int:
    return len(text.split()) if text else 0


def _source_of(shard: Path) -> str:
    for part in shard.parts:
        if part.startswith("source="):
            return part[len("source=") :]
    return ""


def _rel(shard: Path, root: Path) -> str:
    return str(shard.relative_to(root)).replace("\\", "/")


def _read_columns(shard: Path) -> tuple[list[str], bool]:
    names = pq.ParquetFile(shard).schema_arrow.names
    cols = [c for c in _OUT_COLUMNS if c in names]
    has_subset = "subset" in names
    if has_subset:
        cols.append("subset")
    return cols, has_subset


def _iter_rows(shard: Path, cols: list[str], has_subset: bool):
    """Yield ``(row_dict, is_commercial)`` for each row, streamed by batch."""
    for batch in pq.ParquetFile(shard).iter_batches(batch_size=_BATCH_ROWS, columns=cols):
        data = {c: batch.column(c).to_pylist() for c in cols}
        for i in range(batch.num_rows):
            row = {c: data[c][i] for c in cols if c != "subset"}
            yield row, (not has_subset) or data["subset"][i] == "commercial"


def build_slice(
    corpus_root: Path,
    out_dir: Path,
    target_tokens: int,
    *,
    weights: dict[str, float],
    excluded: tuple[str, ...],
    seed: int,
) -> dict:
    """Sample a Stage A slice and write ``out_dir/stage_a/part-*.parquet`` plus a manifest.

    Returns the manifest counts. ``weights`` empty means proportional to the
    tokens each source holds; otherwise sources not in ``weights`` are skipped.
    """
    corpus_root = Path(corpus_root)
    out_dir = Path(out_dir)
    shards = sorted((corpus_root / "pretrain").glob("source=*/year=*/part-*.parquet"))

    refused: list[Path] = []
    skipped: list[Path] = []
    usable: list[Path] = []
    for shard in shards:
        src = _source_of(shard)
        if src in excluded:
            refused.append(shard)
        elif weights and weights.get(src, 0.0) <= 0:
            skipped.append(shard)
        else:
            usable.append(shard)

    # Pass 1: available commercial tokens per source (streamed).
    available: dict[str, int] = {}
    for shard in usable:
        cols, has_subset = _read_columns(shard)
        src = _source_of(shard)
        for row, commercial in _iter_rows(shard, cols, has_subset):
            if commercial:
                available[src] = available.get(src, 0) + _tokens(row["text"])

    # Keep probability per source from its token quota.
    w = {s: (weights[s] if weights else float(n)) for s, n in available.items()}
    total_w = sum(w.values())
    keep_p: dict[str, float] = {}
    for s, n in available.items():
        if total_w <= 0 or n <= 0:
            keep_p[s] = 0.0
        else:
            keep_p[s] = min(1.0, (target_tokens * w[s] / total_w) / n)

    # Pass 2: sample, dedupe, write.
    rng = random.Random(seed)
    stage_dir = out_dir / "stage_a"
    stage_dir.mkdir(parents=True, exist_ok=True)
    for old in stage_dir.glob("part-*.parquet"):
        old.unlink()

    seen: set[str] = set()
    state: dict = {"writer": None, "schema": None, "part": 0, "part_rows": 0}
    buf: dict[str, list] = {}
    totals = {
        "rows_kept": 0,
        "tokens_kept": 0,
        "rows_dropped_non_commercial": 0,
        "rows_dropped_duplicate": 0,
    }
    inputs: list[dict] = []

    def flush() -> None:
        if not buf.get("text"):
            return
        table = pa.table(dict(buf))
        buf.clear()
        if state["schema"] is None:
            state["schema"] = table.schema
        else:
            table = table.select(state["schema"].names).cast(state["schema"])
        if state["writer"] is None:
            path = stage_dir / f"part-{state['part']:05d}.parquet"
            state["writer"] = pq.ParquetWriter(path, state["schema"], compression="zstd")
        state["writer"].write_table(table)
        state["part_rows"] += table.num_rows
        if state["part_rows"] >= _ROWS_PER_PART:
            state["writer"].close()
            state["writer"] = None
            state["part"] += 1
            state["part_rows"] = 0

    for shard in usable:
        cols, has_subset = _read_columns(shard)
        src = _source_of(shard)
        p = keep_p.get(src, 0.0)
        rows_seen = rows_kept = tok_kept = non_comm = dupes = 0
        for row, commercial in _iter_rows(shard, cols, has_subset):
            rows_seen += 1
            if not commercial:
                non_comm += 1
                continue
            if p < 1.0 and rng.random() >= p:
                continue
            text = row["text"]
            key = (
                row.get("content_hash") or hashlib.sha256((text or "").encode("utf-8")).hexdigest()
            )
            if key in seen:
                dupes += 1
                continue
            seen.add(key)
            # Stage B verifies these two fields: only commercial rows reach this point,
            # and a shard without a source column takes it from its path.
            row["source"] = row.get("source") or src
            row["subset"] = "commercial"
            for c, v in row.items():
                buf.setdefault(c, []).append(v)
            rows_kept += 1
            tok_kept += _tokens(text)
            if len(buf["text"]) >= _BATCH_ROWS:
                flush()
        flush()
        totals["rows_kept"] += rows_kept
        totals["tokens_kept"] += tok_kept
        totals["rows_dropped_non_commercial"] += non_comm
        totals["rows_dropped_duplicate"] += dupes
        inputs.append(
            {
                "path": _rel(shard, corpus_root),
                "source": src,
                "status": "read",
                "rows_seen": rows_seen,
                "rows_kept": rows_kept,
                "tokens_kept": tok_kept,
                "rows_dropped_non_commercial": non_comm,
                "rows_dropped_duplicate": dupes,
            }
        )
    if state["writer"] is not None:
        state["writer"].close()

    for shard, status in [(s, "refused_excluded") for s in refused] + [
        (s, "skipped_no_weight") for s in skipped
    ]:
        inputs.append(
            {
                "path": _rel(shard, corpus_root),
                "source": _source_of(shard),
                "status": status,
                "rows_seen": 0,
                "rows_kept": 0,
                "tokens_kept": 0,
            }
        )

    counts = {
        **totals,
        "shards_read": len(usable),
        "shards_refused_excluded": len(refused),
        "shards_skipped_no_weight": len(skipped),
    }
    config = {
        "target_tokens": target_tokens,
        "weights": dict(sorted(weights.items())),
        "excluded": sorted(excluded),
        "seed": seed,
    }
    write_manifest(
        stage_dir / "manifest.json", "slice", inputs=inputs, counts=counts, config=config
    )
    return counts
