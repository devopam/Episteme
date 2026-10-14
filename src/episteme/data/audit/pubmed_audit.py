"""Read-only data-quality audit of the PubMed staging shards and graph files.

DuckDB only; no database access. Used as a gate before reloading production
data. Writes ``pubmed_audit.json`` and ``pubmed_audit.md`` and returns the
report dict.

    python -m episteme.data.audit.pubmed_audit [--processed-dir] [--raw-dir]
        [--out-dir] [--sample-files N] [--duckdb-temp DIR]

The default DuckDB memory limit (2GB) is deliberately below the 4GB
EPISTEME_DUCKDB_MEMORY_LIMIT so the audit can share the machine with
PostgreSQL. The DOI probe uses the same ``own_article_ids`` as the extractor,
so it catches stale shards written by older extractor code, not bugs inside
``own_article_ids`` itself. ``doi_probe["status"]`` is "ok", "skipped_no_raw_dir",
"no_records" or (only for ``sample_files <= 0``) "skipped_by_request".
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from defusedxml.ElementTree import iterparse as _safe_iterparse

from episteme.config import get_settings
from episteme.data import duckdb_conn
from episteme.data.checkpoint_markers import input_key
from episteme.data.graph_files import RELATIONS
from episteme.data.gz import open_gz
from episteme.data.pubmed.extract_pubmed import discover_pubmed_files
from episteme.data.pubmed.relations import own_article_ids

SOURCE = "pubmed"
_PMCID_RE = "^PMC[0-9]+$"
_DOI_RE = "^10\\.[0-9]{4,9}/\\S+$"


def orcid_check_ok(orcid: str) -> bool:
    """ISO 7064 mod 11-2 check digit of an ORCID (``0000-0002-1825-0097``)."""
    digits = orcid.replace("-", "")
    if len(digits) != 16 or not digits[:-1].isdigit():
        return False
    if not (digits[-1].isdigit() or digits[-1] == "X"):
        return False
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    return digits[-1] == ("X" if check == 10 else str(check))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _norm_doi(doi: str | None) -> str | None:
    return doi.strip().lower() if doi and doi.strip() else None


def _doi_probe(con, raw_dir: Path | None, sample_files: int) -> dict[str, Any]:
    """Re-read evenly spaced raw files; compare each record's own DOI to the shard's."""
    probe: dict[str, Any] = {"files": 0, "records": 0, "mismatch": 0, "status": "ok"}
    if sample_files <= 0:
        probe["status"] = "skipped_by_request"
        return probe
    if raw_dir is None or not Path(raw_dir).is_dir():
        probe["status"] = "skipped_no_raw_dir"
        return probe
    files = sorted(discover_pubmed_files(raw_dir))
    if not files:
        probe["status"] = "no_records"
        return probe
    n = min(sample_files, len(files))
    picks = sorted({files[(i * len(files)) // n] for i in range(n)})
    for path in picks:
        key = input_key(path, raw_dir)
        stored = dict(
            con.execute("select pmid, doi from a where source_file = ?", [key]).fetchall()
        )
        probe["files"] += 1
        opener = open_gz if path.name.endswith(".gz") else open
        with opener(path, "rb") as f:
            it = _safe_iterparse(f, events=("start", "end"))
            _, root = next(it)
            for event, elem in it:
                if event != "end" or _local(elem.tag) != "PubmedArticle":
                    continue
                pmid = None
                for c in elem:
                    if _local(c.tag) == "MedlineCitation":
                        for g in c:
                            if _local(g.tag) == "PMID":
                                pmid = (g.text or "").strip()
                                break
                own = _norm_doi(own_article_ids(elem).get("doi"))
                elem.clear()
                root.clear()
                if pmid is None or pmid not in stored:
                    continue
                probe["records"] += 1
                if own != _norm_doi(stored[pmid]):
                    probe["mismatch"] += 1
    if probe["records"] == 0:
        probe["status"] = "no_records"
    return probe


def run_audit(
    processed_dir: Path,
    *,
    raw_dir: Path | None,
    out_dir: Path | None = None,
    sample_files: int = 5,
    duckdb_temp: Path | None = None,
    memory_limit: str = "2GB",
) -> dict[str, Any]:
    processed_dir = Path(processed_dir)
    out_dir = Path(out_dir) if out_dir else processed_dir / "_ops" / "pubmed" / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    temp = Path(duckdb_temp) if duckdb_temp else out_dir / "_duckdb_tmp"
    con = duckdb_conn.connect(memory_limit=memory_limit, temp_dir=temp)
    try:
        con.execute("set preserve_insertion_order=false")
        return _audit(con, processed_dir, raw_dir, out_dir, sample_files)
    finally:
        con.close()


def _audit(
    con, processed_dir: Path, raw_dir: Path | None, out_dir: Path, sample_files: int
) -> dict[str, Any]:

    glob = (processed_dir / "staging" / SOURCE / "*.parquet").as_posix().replace("'", "''")
    t0 = time.time()
    con.execute(f"""
    create temp table a as
    select id, pmid, pmcid, doi, journal, year, language, license, subset, extract_status,
           is_retracted, container_id, parse_filename(filename) shard, source_file,
           source_record_id, content_hash is not null has_hash,
           coalesce(strlen(title), 0) title_len,
           coalesce(starts_with(title, '['), false) title_br,
           coalesce(strlen(abstract), 0) abstract_len,
           coalesce(strlen(text), 0) text_len,
           len(authors) n_authors, len(mesh) n_mesh, publication_types
    from read_parquet('{glob}', filename=true)
    """)
    scan_sec = round(time.time() - t0, 1)

    def q(sql: str, params: list | None = None):
        return con.execute(sql, params or []).fetchall()

    def one(sql: str):
        return con.execute(sql).fetchone()[0]

    R: dict[str, Any] = {"scan_sec": scan_sec}
    total = one("select count(*) from a")
    R["rows"] = total
    R["shards"] = one("select count(distinct shard) from a")
    R["rows_per_shard"] = q(
        "select min(c), max(c), round(avg(c)) from (select count(*) c from a group by shard)"
    )[0]
    denom = total or 1

    # completeness
    R["null_pct"] = {}
    for c in ["pmid", "pmcid", "doi", "journal", "year", "language", "license", "content_hash"]:
        expr = "not has_hash" if c == "content_hash" else f"{c} is null"
        n = one(f"select count(*) filter (where {expr}) from a")
        R["null_pct"][c] = round(100.0 * n / denom, 3)
    for c, e in [
        ("title", "title_len = 0"),
        ("abstract", "abstract_len = 0"),
        ("text", "text_len = 0"),
        ("authors", "coalesce(n_authors,0) = 0"),
        ("mesh", "coalesce(n_mesh,0) = 0"),
        ("publication_types", "coalesce(len(publication_types),0) = 0"),
    ]:
        n = one(f"select count(*) filter (where {e}) from a")
        R["null_pct"][c] = round(100.0 * n / denom, 3)
    R["extract_status"] = q("select extract_status, count(*) from a group by 1 order by 2 desc")
    R["license"] = q(
        "select license, subset, count(*) from a group by 1,2 order by 3 desc limit 20"
    )
    R["language_top"] = q("select language, count(*) from a group by 1 order by 2 desc limit 15")
    R["language_distinct"] = one("select count(distinct language) from a")

    # validity
    R["year_range"] = q("select min(year), max(year) from a")[0]
    R["year_out_of_range"] = one("select count(*) from a where year < 1780 or year > 2027")
    R["year_hist_decade"] = q("select (year//10)*10 d, count(*) from a group by 1 order by 1")
    R["pmid_not_numeric"] = one(
        "select count(*) from a where pmid is not null and not regexp_matches(pmid, '^[0-9]+$')"
    )
    R["id_mismatch"] = one("select count(*) from a where id is distinct from 'pmid:' || pmid")
    bad_pmcid = f"pmcid is not null and not regexp_matches(pmcid, '{_PMCID_RE}')"
    R["pmcid_bad"] = one(f"select count(*) from a where {bad_pmcid}")
    R["pmcid_bad_samples"] = q(f"select pmcid from a where {bad_pmcid} limit 5")
    bad_doi = f"doi is not null and not regexp_matches(doi, '{_DOI_RE}')"
    R["doi_bad"] = one(f"select count(*) from a where {bad_doi}")
    R["doi_bad_samples"] = q(f"select doi from a where {bad_doi} limit 8")
    R["container_id_set"] = one("select count(*) from a where container_id is not null")
    R["source_record_id_mismatch"] = one(
        "select count(*) from a where source_record_id is distinct from pmid"
    )
    R["source_file_shard_mismatch"] = one(
        "select count(*) from a where replace(source_file, '.xml.gz', '.parquet') <> shard"
    )

    # uniqueness
    R["dup_ids_within_shard"] = one(
        "select count(*) from (select shard, id from a group by 1,2 having count(*) > 1)"
    )
    R["dup_ids_across_shards"] = one(
        "select count(*) from (select id from a group by 1 having count(distinct shard) > 1)"
    )
    R["dup_id_extra_rows"] = one(
        "select coalesce(sum(c-1),0) from "
        "(select id, count(*) c from a group by 1 having count(*) > 1)"
    )
    R["dup_id_samples"] = q(
        """select id, list(shard order by shard) from a where id in
        (select id from a group by 1 having count(*) > 1 limit 5) group by 1"""
    )
    R["distinct_ids"] = one("select count(distinct id) from a")
    R["dup_doi_groups"] = one(
        "select count(*) from (select lower(doi) from a where doi is not null "
        "group by 1 having count(distinct id) > 1)"
    )
    R["dup_doi_samples"] = q(
        """select lower(doi) d, count(distinct id) n from a where doi is not null
        group by 1 having count(distinct id) > 1 order by n desc limit 8"""
    )

    # consistency
    R["text_without_hash"] = one("select count(*) from a where text_len > 0 and not has_hash")
    R["retraction"] = q(
        """select is_retracted, list_contains(publication_types, 'Retracted Publication') rp,
        list_contains(publication_types, 'Retraction of Publication') rop, count(*)
        from a group by 1,2,3 order by 4 desc"""
    )
    R["text_len_pctl"] = q("select quantile_cont(text_len, [0.01,0.5,0.99]), max(text_len) from a")[
        0
    ]
    R["abstract_len_pctl"] = q(
        "select quantile_cont(abstract_len, [0.5,0.99]), max(abstract_len) "
        "from a where abstract_len > 0"
    )[0]
    R["title_bracketed_pct"] = round(
        100.0 * one("select count(*) from a where title_br") / denom, 3
    )
    R["authors_max"] = q("select max(n_authors), quantile_cont(n_authors, 0.99) from a")[0]
    R["status_vs_content"] = q(
        """select extract_status, abstract_len>0 has_abs, title_len>0 has_title, count(*)
        from a group by 1,2,3 order by 4 desc"""
    )
    R["pub_types_top"] = q(
        "select t, count(*) from (select unnest(publication_types) t from a) "
        "group by 1 order by 2 desc limit 15"
    )

    # graph files
    gdir = processed_dir / "staging" / f"{SOURCE}_graph"

    def rel_glob(rel: str) -> str | None:
        d = gdir / rel
        if d.is_dir() and any(d.glob("*.parquet")):
            return (d / "*.parquet").as_posix().replace("'", "''")
        return None

    R["graph_rows"] = {}
    for rel in RELATIONS:
        g = rel_glob(rel)
        R["graph_rows"][rel] = one(f"select count(*) from read_parquet('{g}')") if g else 0

    mesh_g = rel_glob("mesh")
    mesh_shards = processed_dir / "staging" / "mesh"
    R["mesh_ui_known_pct"] = None
    if mesh_g and mesh_shards.is_dir() and any(mesh_shards.glob("*.parquet")):
        mg = (mesh_shards / "*.parquet").as_posix().replace("'", "''")
        row = con.execute(
            f"""with u as (select distinct descriptor_ui from read_parquet('{mesh_g}')
                           where descriptor_ui is not null),
                 k as (select distinct id from read_parquet('{mg}'))
                select count(*), count(*) filter (where k.id is not null)
                from u left join k on k.id = 'mesh:' || u.descriptor_ui"""
        ).fetchone()
        R["mesh_ui_known_pct"] = round(100.0 * row[1] / row[0], 3) if row[0] else None

    R["cites_resolved_pct"] = None
    cites_g = rel_glob("cites")
    if cites_g:
        row = con.execute(
            f"""with d as (select distinct dst_pmid from read_parquet('{cites_g}')
                           where dst_pmid is not null)
                select count(*), count(*) filter (where a_.pmid is not null)
                from d left join (select distinct pmid from a) a_ on a_.pmid = d.dst_pmid"""
        ).fetchone()
        R["cites_resolved_pct"] = round(100.0 * row[1] / row[0], 3) if row[0] else None

    R["orcid_checksum_bad"] = 0
    R["orcid_checked"] = 0
    auth_g = rel_glob("authors")
    if auth_g:
        orcids = [
            r[0]
            for r in q(
                f"select distinct orcid from read_parquet('{auth_g}') where orcid is not null"
            )
        ]
        R["orcid_checked"] = len(orcids)
        R["orcid_checksum_bad"] = sum(1 for o in orcids if not orcid_check_ok(o))

    com_g = rel_glob("comments")
    if com_g:
        con.execute(
            f"""create temp table retr_in as select distinct pmid from read_parquet('{com_g}')
                where ref_type = 'RetractionIn' and pmid is not null"""
        )
    else:
        con.execute("create temp table retr_in(pmid varchar)")
    R["retracted_without_link"] = one(
        """select count(*) from a where (coalesce(is_retracted, false)
           or list_contains(publication_types, 'Retracted Publication'))
           and pmid not in (select pmid from retr_in)"""
    )
    R["retraction_in_without_flag"] = one(
        """select count(*) from a where pmid in (select pmid from retr_in)
           and not coalesce(is_retracted, false)"""
    )

    # DOI probe against the raw files
    R["doi_probe"] = _doi_probe(con, raw_dir, sample_files)

    R["total_sec"] = round(time.time() - t0, 1)

    (out_dir / "pubmed_audit.json").write_text(
        json.dumps(R, indent=1, default=str), encoding="utf-8"
    )
    lines = [
        "# PubMed staging audit\n",
        f"{R['rows']:,} rows in {R['shards']} shards; scan {scan_sec}s, total {R['total_sec']}s\n",
    ]
    lines += [f"- **{k}**: {v}" for k, v in R.items()]
    (out_dir / "pubmed_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return R


def main(argv: list[str] | None = None) -> int:
    s = get_settings()
    ap = argparse.ArgumentParser(description="Read-only PubMed data-quality audit")
    ap.add_argument("--processed-dir", type=Path, default=s.processed_root)
    ap.add_argument("--raw-dir", type=Path, default=s.raw_root / SOURCE)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--sample-files", type=int, default=5)
    ap.add_argument(
        "--memory-limit",
        default="2GB",
        help="DuckDB memory limit; deliberately below the 4GB EPISTEME_DUCKDB_MEMORY_LIMIT "
        "so the audit can share the machine with PostgreSQL",
    )
    ap.add_argument("--duckdb-temp", type=Path, default=None, help="spill directory (use the NVMe)")
    args = ap.parse_args(argv)
    out_dir = args.out_dir or args.processed_dir / "_ops" / "pubmed" / "audit"
    R = run_audit(
        args.processed_dir,
        raw_dir=args.raw_dir if args.raw_dir.is_dir() else None,
        out_dir=out_dir,
        sample_files=args.sample_files,
        memory_limit=args.memory_limit,
        duckdb_temp=args.duckdb_temp,
    )
    print(out_dir / "pubmed_audit.md")
    probe = R["doi_probe"]
    print(f"rows={R['rows']:,} doi_probe={probe}")
    if args.sample_files == 0:
        return 0
    if probe["status"] != "ok":
        print(f"WARNING: DOI probe did not run ({probe['status']}); the gate is NOT verified")
        return 1
    if probe["mismatch"] > 0:
        print(f"WARNING: DOI probe found {probe['mismatch']} mismatches")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
