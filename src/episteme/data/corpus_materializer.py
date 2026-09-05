"""Materialise the pretraining corpus: ``episteme.articles`` JOIN ``article_body``
-> near-deduplicated, benchmark-decontaminated, Hive-partitioned Parquet shards.

``materialize`` streams the commercial-subset, successfully-extracted rows out of
Postgres through a DuckDB ``postgres`` attachment. That attachment is a
*separate*, read-only connection to the live database -- the psycopg ``conn``
argument is used ONLY for the ``audit_trail.record`` call, which lands in the
caller's transaction and is NOT committed here (matching ``postgres_loader`` /
``graph_builder``). The read is streamed with ``fetchmany`` so the corpus is
never held in RAM as one frame.

Two filters run over the row iterator, reusing the ``data.curate`` primitives
verbatim (this module wires them together, it does not reimplement them):

  * near-duplicate removal -- one in-process
    ``datasketch.MinHashLSH(threshold=dedup_threshold, num_perm=128)``; the
    signature is ``build_minhash(get_shingles(text))``. Rows arrive in
    ``content_hash`` order so the first occurrence of a cluster is kept and
    later near-dups are dropped (``dropped_dup``).
  * benchmark decontamination -- 13-gram overlap against
    ``build_test_ngrams(13, sample_only=...)``; a row that shares any 13-gram
    with the eval set is dropped (``dropped_contam``). SP1-beta runs
    ``sample_only=True`` (the HF eval sets are not downloaded here).

Survivors are grouped by ``(source, year)`` and written to
``out_root/pretrain/source=<source>/year=<year>/part-000.parquet`` (zstd, rows
in ``content_hash`` order), then one ``corpus_materialize`` audit row is
recorded. Returns
``{rows_in, rows_out, dropped_dup, dropped_contam, shards}``.

This module never reads ``os.environ`` -- the Postgres DSN comes from the
``pg_dsn`` argument or ``episteme.data.db.connection.dsn_from_settings()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from episteme import audit_trail
from episteme.data.curate.decontaminate_benchmarks import (
    build_test_ngrams,
    get_ngrams,
    normalize_text,
)
from episteme.data.curate.deduplicate_corpus import build_minhash, get_shingles

# The join is ON b.article_id = a.id -- episteme.articles is keyed on `id`,
# episteme.article_body on `article_id` (= articles.id). ORDER BY content_hash
# makes the stream deterministic and gives each (source, year) shard its rows in
# content_hash order for free.
_SELECT = (
    "SELECT a.id, a.source, a.year, a.pmid, a.doi, a.license, a.content_hash, b.text "
    "FROM pg.episteme.articles a "
    "JOIN pg.episteme.article_body b ON b.article_id = a.id "
    "WHERE a.subset = 'commercial' AND a.extract_status = 'ok' AND b.text IS NOT NULL "
    "ORDER BY a.content_hash"
)

# Column order per ADR-0002's Parquet section: hot->cold (frequently scanned
# columns first -- text, source, year -- then the remaining metadata columns).
_COLUMNS = ("text", "source", "year", "id", "pmid", "doi", "license", "content_hash")

_NGRAM_SIZE = 13
_NUM_PERM = 128
_BATCH = 1000


def materialize(
    conn,
    *,
    out_root: Path | str,
    run_id: str,
    pg_dsn: str | None = None,
    dedup_threshold: float = 0.8,
    sample_only: bool = True,
) -> dict[str, Any]:
    """Stream + curate + shard the pretraining corpus. See module docstring."""
    import duckdb
    from datasketch import MinHashLSH

    if pg_dsn is None:
        from episteme.data.db.connection import dsn_from_settings

        pg_dsn = dsn_from_settings()

    out_root = Path(out_root)
    test_ngrams = build_test_ngrams(_NGRAM_SIZE, sample_only=sample_only)
    lsh = MinHashLSH(threshold=dedup_threshold, num_perm=_NUM_PERM)

    rows_in = 0
    dropped_dup = 0
    dropped_contam = 0
    # (source, year) -> list of survivor rows, kept in arrival (content_hash) order.
    groups: dict[tuple[Any, Any], list[tuple]] = {}

    d = duckdb.connect()
    try:
        d.execute("INSTALL postgres")
        d.execute("LOAD postgres")
        # pg_dsn is operator config, not user input -- safe to interpolate here.
        try:
            d.execute(f"ATTACH '{pg_dsn}' AS pg (TYPE postgres, READ_ONLY)")  # nosec B608
        except Exception:
            # DuckDB's own error message can echo the failing statement (DSN,
            # password included) -- raise a clean error and suppress exception
            # chaining (`from None`) so the original never reaches a traceback.
            raise RuntimeError(
                "DuckDB ATTACH to Postgres failed (connection string redacted from "
                "this exception; check the target server, credentials, and that the "
                "duckdb postgres extension can install)"
            ) from None
        cur = d.execute(_SELECT)
        while batch := cur.fetchmany(_BATCH):
            for row in batch:
                id_, source, year, pmid, doi, license_, content_hash, text = row
                rows_in += 1

                sig = build_minhash(get_shingles(text), num_perm=_NUM_PERM)
                if lsh.query(sig):
                    dropped_dup += 1
                    continue
                lsh.insert(id_, sig)

                doc_ngrams = get_ngrams(normalize_text(text), _NGRAM_SIZE)
                if doc_ngrams & test_ngrams:
                    dropped_contam += 1
                    continue

                groups.setdefault((source, year), []).append(
                    (text, source, year, id_, pmid, doi, license_, content_hash)
                )
    finally:
        d.close()

    shards: list[str] = []
    rows_out = 0
    for (source, year), survivors in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
        part_dir = out_root / "pretrain" / f"source={source}" / f"year={year}"
        part_dir.mkdir(parents=True, exist_ok=True)
        path = part_dir / "part-000.parquet"
        _write_shard(path, survivors)
        shards.append(str(path))
        rows_out += len(survivors)

    audit_trail.record(
        "corpus_materialize",
        conn=conn,
        object=f"pretrain {run_id}",
        rows_affected=rows_out,
        run_id=run_id,
    )

    return {
        "rows_in": rows_in,
        "rows_out": rows_out,
        "dropped_dup": dropped_dup,
        "dropped_contam": dropped_contam,
        "shards": shards,
    }


def _write_shard(path: Path, survivors: list[tuple]) -> None:
    """Write one ``(source, year)`` group to a zstd Parquet file, no pandas."""
    # TODO(later SP): split into multiple part-NNN.parquet files at ADR-0002's
    # 256-512MB target; single-file at SP1-beta's proving-slice scale.
    import pyarrow as pa
    import pyarrow.parquet as pq

    columns = {name: [row[i] for row in survivors] for i, name in enumerate(_COLUMNS)}
    pq.write_table(
        pa.table(columns),
        path,
        compression="zstd",
        compression_level=3,
        # ADR-0002 targets ~128MB row groups by bytes, not row count -- 131072
        # is a placeholder that overshoots badly once `text` carries full
        # article bodies (tens of KB/row); revisit with a byte-aware group
        # size (or rely on part-file rolling, see the TODO above) before a
        # real Phase-0-scale materialize run.
        row_group_size=131072,
        use_dictionary=["source", "license"],  # low-cardinality cols present in _COLUMNS
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    from datetime import datetime, timezone

    from episteme.config import get_settings
    from episteme.data.db.connection import connection

    settings = get_settings()
    p = argparse.ArgumentParser(description="Materialize the pretraining corpus from Postgres")
    p.add_argument("--out-root", type=Path, default=settings.corpus_root)
    p.add_argument("--pg-dsn", default=None, help="defaults to config.dsn_from_settings()")
    p.add_argument(
        "--no-sample-only",
        dest="sample_only",
        action="store_false",
        default=True,
        help="use the real HF eval sets for decontamination instead of the mock question list "
        "(requires the datasets to be downloaded)",
    )
    args = p.parse_args(argv)

    run_id = (
        settings.run_id or f"materialize-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    with connection() as conn:
        res = materialize(
            conn,
            out_root=args.out_root,
            run_id=run_id,
            pg_dsn=args.pg_dsn,
            sample_only=args.sample_only,
        )
        conn.commit()
    print(
        f"done rows_in={res['rows_in']} rows_out={res['rows_out']} "
        f"dropped_dup={res['dropped_dup']} dropped_contam={res['dropped_contam']} "
        f"shards={len(res['shards'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
