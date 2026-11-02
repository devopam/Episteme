"""Feed config: one ``Feed`` per dataset, read from ``feeds.toml``.

``feeds.toml`` holds key names of ``scripts/data/_lib/sources.env``, never URLs.
The file is data, not stage code: it is kept out of the stage code hash, and its
values go into the run record.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomllib

FEEDS_PATH = Path(__file__).with_name("feeds.toml")

_REQUIRED = (
    "url_key",
    "format",
    "jurisdiction",
    "record_key",
    "substance_fields",
    "product_fields",
    "cadence",
)


@dataclass(frozen=True)
class Feed:
    name: str
    url_key: str  # a key in scripts/data/_lib/sources.env
    format: str  # names a parser in episteme.data.regulatory.parsers
    jurisdiction: str
    # Field names (dotted paths reach into nested records; a list on the way is
    # walked element by element). An empty record_key means the dataset has no
    # natural id and the loader hashes the whole row.
    record_key: tuple[str, ...]
    substance_fields: tuple[str, ...]
    product_fields: tuple[str, ...]
    date_field: str | None
    cadence: str


def load_feeds(path: Path | None = None) -> dict[str, Feed]:
    """Read every ``[feeds.<name>]`` table of the config file into a ``Feed``."""
    src = Path(path) if path is not None else FEEDS_PATH
    data = tomllib.loads(src.read_text(encoding="utf-8"))
    out: dict[str, Feed] = {}
    for name, entry in (data.get("feeds") or {}).items():
        missing = [k for k in _REQUIRED if k not in entry]
        if missing:
            raise ValueError(f"feed {name!r} in {src.name} lacks: {', '.join(missing)}")
        out[name] = Feed(
            name=name,
            url_key=str(entry["url_key"]),
            format=str(entry["format"]),
            jurisdiction=str(entry["jurisdiction"]),
            record_key=tuple(entry["record_key"]),
            substance_fields=tuple(entry["substance_fields"]),
            product_fields=tuple(entry["product_fields"]),
            date_field=entry.get("date_field") or None,
            cadence=str(entry["cadence"]),
        )
    return out
