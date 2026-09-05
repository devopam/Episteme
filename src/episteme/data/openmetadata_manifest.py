"""Pure builder for the OpenMetadata catalog manifest.

``build_manifest`` describes the ``episteme`` Postgres service, the requested
tables (each carrying an ``episteme.license`` / ``episteme.subset`` column tag),
and a single ``01_raw/<source> -> extract -> load -> articles`` lineage edge.

The function does NO I/O and NO DB work -- it only reads
``episteme.config.get_settings()`` (config, never ``os.environ``) for the
service coordinates. Before returning it validates the manifest against the
bundled ``manifest.schema.json`` (JSON Schema draft 2020-12); a
``jsonschema.ValidationError`` propagates unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from episteme.config import get_settings
from episteme.data import article_schema

MANIFEST_VERSION = "1.0"

_SCHEMA = json.loads((Path(__file__).parent / "manifest.schema.json").read_text(encoding="utf-8"))

# The two columns every episteme table exposes to the catalog with a tag so a
# downstream consumer can filter the corpus by redistribution terms.
_TAGGED_COLUMNS: tuple[dict, ...] = (
    {"name": "license", "dataType": "TEXT", "tags": ["episteme.license"]},
    {"name": "subset", "dataType": "TEXT", "tags": ["episteme.subset"]},
)


def build_manifest(source: str, *, tables: list[str]) -> dict:
    """Return a schema-valid OpenMetadata manifest dict for ``source``.

    ``source`` must be one of :data:`article_schema.SOURCES` -- otherwise
    ``ValueError``. ``tables`` is the list of ``episteme`` table names to
    describe.
    """
    if source not in article_schema.SOURCES:
        raise ValueError(
            f"unknown source {source!r}; must be one of {sorted(article_schema.SOURCES)}"
        )

    s = get_settings()
    db = s.pg_database

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "generated_by": "episteme.data.openmetadata_manifest",
        "service": {
            "name": "episteme_pg",
            "type": "Postgres",
            "database": db,
            "schema": "episteme",
            "hostPort": f"{s.pg_host}:{s.pg_port}",
        },
        "source": source,
        "tables": [
            {
                "name": t,
                "fullyQualifiedName": f"episteme_pg.{db}.episteme.{t}",
                "columns": [dict(c) for c in _TAGGED_COLUMNS],
            }
            for t in tables
        ],
        "lineage": [
            {
                "fromEntity": f"file://01_raw/{source}",
                "pipeline": ["extract", "load"],
                "toEntity": f"episteme_pg.{db}.episteme.articles",
            }
        ],
    }

    jsonschema.validate(manifest, _SCHEMA)
    return manifest
