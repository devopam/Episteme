"""Feed config: one ``Feed`` per dataset, read from ``feeds.toml``.

``feeds.toml`` holds key names of ``scripts/data/_lib/sources.env``, never URLs.
The file is data, not stage code: it is kept out of the stage code hash, and its
values go into the run record.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import tomllib

FEEDS_PATH = Path(__file__).with_name("feeds.toml")
# The feed formats that each reference-feed stage reads (``load_reg --source``). The
# ``ema_epar`` stages read the feed that has ``document_types``.
SOURCE_FORMATS: dict[str, tuple[str, ...]] = {
    "ema": ("ema_json", "article57_xlsx"),
    "openfda": ("openfda_json",),
}
# The stages whose run record carries the feed config (``feeds_record``).
FEED_SOURCES = ("ema", "openfda", "ema_epar")

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
    # Document types whose files are fetched and read as text (ema_epar_documents only).
    document_types: tuple[str, ...] = ()


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
            document_types=tuple(entry.get("document_types") or ()),
        )
    return out


def source_feeds(source: str, feeds: dict[str, Feed] | None = None) -> list[Feed]:
    """The feeds that the stages of ``source`` read: by format for ``ema`` and ``openfda``,
    the feed with ``document_types`` for ``ema_epar``; none for any other source."""
    feeds = load_feeds() if feeds is None else feeds
    if source == "ema_epar":
        return [f for f in feeds.values() if f.document_types]
    formats = SOURCE_FORMATS.get(source, ())
    return [f for f in feeds.values() if f.format in formats]


def feeds_record(source: str, path: Path | None = None) -> dict[str, Any]:
    """What a run record holds of the feed config: the file name, the sha256 of its bytes
    (CRLF read as LF, so two checkouts of one commit agree) and the parsed entries of the
    feeds of ``source``. The file is data, outside the stage code hash: this is how a run
    records the values it ran with."""
    src = Path(path) if path is not None else FEEDS_PATH
    data = src.read_bytes().replace(b"\r\n", b"\n")
    entries = {f.name: asdict(f) for f in source_feeds(source, load_feeds(src))}
    return {"file": src.name, "sha256": hashlib.sha256(data).hexdigest(), "entries": entries}
