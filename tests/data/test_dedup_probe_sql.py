"""The OpenAlex dedup probe SQL shape (no database): one LATERAL LIMIT 1 probe per
key, never a flattenable EXISTS semi-join (the planner turned that around on the
real pubmed partitions and read every pubmed DOI per shard)."""

import pytest

from episteme.data import postgres_loader as loader


@pytest.mark.parametrize("key", loader.DEDUP_KEYS)
def test_probe_is_one_lateral_limit_1_per_key(key):
    sql = loader._dedup_probe_sql(key, ("pubmed", "pmc"))
    assert "CROSS JOIN LATERAL" in sql
    assert sql.rstrip().endswith("LIMIT 1) hit")
    assert "EXISTS" not in sql
    assert "a.source IN ('pubmed', 'pmc')" in sql
    assert f"a.{key} IS NOT NULL" in sql


def test_probe_compares_the_doi_lowercased():
    sql = loader._dedup_probe_sql("doi", ("pubmed", "pmc"))
    assert "lower(a.doi) = k.k" in sql


def test_probe_refuses_unknown_sources_and_keys():
    with pytest.raises(ValueError, match="unknown dedup source"):
        loader._dedup_probe_sql("pmid", ("pubmed', 'x",))
    with pytest.raises(ValueError, match="unknown dedup key"):
        loader._dedup_probe_sql("title", ("pubmed",))
