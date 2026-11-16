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


@pytest.mark.parametrize("key", loader.DEDUP_KEYS)
def test_sweep_probe_is_lateral_without_limit(key):
    """The reverse sweep returns every openalex row of a key (two works can share
    one DOI), so it is a LATERAL probe with no LIMIT, restricted to openalex."""
    sql = loader._sweep_probe_sql(key, "openalex")
    assert "CROSS JOIN LATERAL" in sql
    assert "LIMIT" not in sql
    assert "EXISTS" not in sql
    # The partition itself, no source predicate: through the parent the source
    # predicate let the planner pick the (source, source_file) index instead.
    assert "episteme.articles_openalex a " in sql
    assert "a.source =" not in sql
    assert "episteme.articles a " not in sql
    assert f"a.{key} IS NOT NULL" in sql
    assert "pg_temp._sweep_keys" in sql
    # The raw column, so the plain articles_openalex_<key>_idx serves the probe
    # (OpenAlex DOIs are lower case; the key table holds lower(doi)).
    assert f"a.{key} = k.k" in sql
    assert "lower(" not in sql


def test_sweep_probe_refuses_unknown_target_and_key():
    with pytest.raises(ValueError, match="unknown sweep target"):
        loader._sweep_probe_sql("pmid", "openalex' OR 'x")
    with pytest.raises(ValueError, match="unknown dedup key"):
        loader._sweep_probe_sql("title", "openalex")


def test_sweep_into_maps_pubmed_and_pmc_to_openalex():
    assert loader.SWEEP_INTO == {"pubmed": "openalex", "pmc": "openalex"}
