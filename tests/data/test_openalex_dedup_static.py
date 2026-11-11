"""OpenAlex dedup: the checks that need no database (fix round 1).

- migration 0012 commits the partition DDL before it builds the lower(doi) indexes
- the gate markers of 0012
- the loader refuses an openalex shard staged before the serializer kept the ids
- the runbook's id-shape check reads the pmid share only
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from episteme.data import article_schema, postgres_loader
from episteme.data.article_schema import empty_article_row, finalize_row
from episteme.data.staging_writer import write_rows
from episteme.ops import gate_migrations

REPO = Path(__file__).resolve().parents[2]
MIG_0012 = REPO / "src/episteme/data/db/migrations/0012_openalex_partitions.sql"
RUNBOOK = REPO / "docs/10-data-sources-runbook.md"


def _statements(sql: str) -> list[str]:
    """Statements of the file, comments removed, upper-cased, whitespace collapsed."""
    no_comments = re.sub(r"--[^\n]*", "", sql)
    return [" ".join(s.split()).upper() for s in no_comments.split(";") if s.strip()]


def _transactions(sql: str) -> list[list[str]]:
    """The statements of each BEGIN ... COMMIT block, in order."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for st in _statements(sql):
        if st == "BEGIN":
            assert current is None, "nested BEGIN"
            current = []
        elif st == "COMMIT":
            assert current is not None, "COMMIT without BEGIN"
            blocks.append(current)
            current = None
        else:
            assert current is not None, f"statement outside a transaction: {st[:60]}"
            current.append(st)
    assert current is None, "missing COMMIT"
    return blocks


def test_0012_commits_the_partitions_before_it_builds_the_doi_indexes():
    blocks = _transactions(MIG_0012.read_text(encoding="utf-8"))
    assert len(blocks) == 2
    first, second = blocks
    assert sum("PARTITION OF" in s for s in first) == 2
    assert not any("CREATE INDEX" in s for s in first)
    assert "SET LOCAL LOCK_TIMEOUT = '60S'" in first
    assert not any("PARTITION OF" in s for s in second)
    assert sum(s.startswith("CREATE INDEX IF NOT EXISTS") for s in second) == 2
    assert not any("CONCURRENTLY" in s for s in second)
    assert "SET LOCAL LOCK_TIMEOUT = '60S'" in second
    assert any(s.startswith("SET LOCAL MAINTENANCE_WORK_MEM = ") for s in second)


def test_0012_header_no_longer_claims_reads_go_on_during_the_partition_ddl():
    text = MIG_0012.read_text(encoding="utf-8")
    assert "not their reads" not in text
    assert "5,400 rpm" not in text
    assert "article_body_default" in text


def test_0012_gate_markers_cover_body_partition_and_both_doi_indexes():
    assert gate_migrations.MIGRATION_MARKERS["0012_openalex_partitions.sql"] == [
        "table:episteme.article_body_openalex",
        "index:episteme.articles_pubmed_doi_lower_idx",
        "index:episteme.articles_pmc_doi_lower_idx",
    ]


def _oa_rows() -> list[dict]:
    return [
        finalize_row(
            {
                **empty_article_row(),
                "id": "openalex:W1",
                "source": "openalex",
                "source_file": "old_part",
                "pmid": None,
                "doi": "10.1/x",
                "title": "t",
                "abstract": "an abstract " * 30,
                "license": "CC0",
                "subset": "commercial",
            }
        )
    ]


def test_loader_refuses_an_openalex_shard_staged_before_the_ids_were_kept(tmp_path):
    info = write_rows(_oa_rows(), tmp_path, source="openalex", source_file="old_part")
    shard = Path(info["paths"][0])
    with pytest.raises(RuntimeError, match="staged before"):
        postgres_loader.check_dedup_shard(shard)
    # load_source_file refuses before it touches the connection
    with pytest.raises(RuntimeError, match="serialize --force"):
        postgres_loader.load_source_file(None, source="openalex", staging_path=shard, run_id="r")


def test_loader_accepts_an_openalex_shard_with_the_ids_marker(tmp_path):
    info = write_rows(
        _oa_rows(),
        tmp_path,
        source="openalex",
        source_file="new_part",
        metadata={article_schema.DEDUP_IDS_META_KEY: article_schema.DEDUP_IDS_VERSION},
    )
    postgres_loader.check_dedup_shard(Path(info["paths"][0]))  # no error


def test_loader_refuses_an_openalex_jsonl_shard(tmp_path):
    info = write_rows(
        _oa_rows(), tmp_path, source="openalex", source_file="j_part", prefer_parquet=False
    )
    with pytest.raises(RuntimeError, match="staged before"):
        postgres_loader.check_dedup_shard(Path(info["paths"][0]))


def test_runbook_id_check_reads_the_pmid_share_only():
    text = RUNBOOK.read_text(encoding="utf-8")
    sec = text[text.index("#### OpenAlex load: only works") :]
    sec = sec[: sec.index("\n### ")] if "\n### " in sec else sec
    assert "Stop if either is 0.0" not in sec
    assert "`pmcid` is 0.0 on current snapshots" in sec
    assert "`skipped_pmid` must be greater than 0" in sec
    assert "VACUUM (ANALYZE) episteme.articles_pubmed, episteme.articles_pmc" in sec
