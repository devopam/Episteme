import json
from pathlib import Path

import jsonschema
import pytest

from episteme.data.openmetadata_manifest import build_manifest

_SCHEMA_PATH = Path("src/episteme/data/manifest.schema.json")


def test_openmetadata_manifest_build_and_validate():
    m = build_manifest("pmc", tables=["articles", "article_body"])

    # shape
    assert m["manifest_version"] == "1.0"
    assert m["service"]["schema"] == "episteme"
    assert m["service"]["type"] == "Postgres"
    assert m["source"] == "pmc"
    assert len(m["tables"]) == 2
    assert m["tables"][0]["name"] == "articles"
    tag_cols = {c["name"] for c in m["tables"][0]["columns"]}
    assert {"license", "subset"} <= tag_cols
    assert m["lineage"][0]["pipeline"] == ["extract", "load"]

    # bundled schema accepts a real manifest
    schema = json.loads(_SCHEMA_PATH.read_text())
    jsonschema.validate(m, schema)

    # ... and rejects an incomplete one
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"manifest_version": "1.0"}, schema)

    # ... and rejects a non-array `tables`
    bad = dict(m)
    bad["tables"] = "not-an-array"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)

    # unknown source is a ValueError
    with pytest.raises(ValueError):
        build_manifest("not_a_source", tables=["articles"])
