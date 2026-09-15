"""Shared JATS / NXML parsing primitives.

Lifted verbatim (behaviour-preserving) from ``episteme.data.pmc.extract_pmc``
so every SP2 literature extractor can reuse the same JATS parsing. The only
changes on the move are the public names (``_local`` -> ``local_name``,
``_child_text`` -> ``child_text``, ``_itertext`` -> ``itertext``);
``parse_jats_fields`` keeps its name. ``iter_book_parts`` is new — it yields
``<book-part>`` elements (recursing) for bookshelf / manuscripts NXML.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as ET


def local_name(tag: str) -> str:
    return tag.split("}")[-1] if tag else tag


def child_text(parent: ET.Element, name: str) -> str:
    for c in parent:
        if local_name(c.tag) == name:
            return "".join(c.itertext()).strip()
    return ""


def itertext(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def parse_jats_fields(xml_path: Path) -> dict[str, Any]:
    """Extract bibliographic + body fields from JATS XML."""
    out: dict[str, Any] = {
        "title": None,
        "abstract": None,
        "body_text": None,
        "authors": None,
        "journal": None,
        "year": None,
        "language": None,
        "pmid": None,
        "pmcid": None,
        "doi": None,
    }
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return out

    lang = root.get("{http://www.w3.org/XML/1998/namespace}lang") or root.get("lang")
    if lang:
        out["language"] = lang

    authors: list[str] = []
    abstract_parts: list[str] = []
    body_parts: list[str] = []

    for el in root.iter():
        t = local_name(el.tag)
        if t == "article-title" and not out["title"]:
            out["title"] = itertext(el)
        elif t == "abstract":
            # take the full text of each abstract node once
            txt = itertext(el)
            if txt and txt not in abstract_parts:
                abstract_parts.append(txt)
        elif t == "body":
            txt = itertext(el)
            if txt:
                body_parts.append(txt)
        elif t == "journal-title" and not out["journal"]:
            out["journal"] = itertext(el)
        elif t == "year" and out["year"] is None:
            try:
                out["year"] = int((itertext(el) or "")[:4])
            except ValueError:
                pass
        elif t == "article-id":
            idt = (el.get("pub-id-type") or "").lower()
            val = (el.text or "").strip()
            if idt in ("pmid", "pubmed") and not out["pmid"]:
                out["pmid"] = val
            elif idt == "pmcid" and not out["pmcid"]:
                out["pmcid"] = val if val.upper().startswith("PMC") else f"PMC{val}"
            elif idt == "doi" and not out["doi"]:
                out["doi"] = val
        elif t == "contrib" and (el.get("contrib-type") in (None, "author")):
            # JATS nests the name one level down: <contrib><name><surname/>
            # <given-names/></name></contrib> -- surname/given-names are
            # grandchildren of contrib, not direct children, so child_text
            # must be called on the <name> element, not on <contrib> itself
            # (field-shape report, Task 11: this bug made `authors` 100% null
            # on real PMC JATS despite well-formed <contrib-group> data).
            name_el = None
            for c in el:
                if local_name(c.tag) == "name":
                    name_el = c
                    break
            if name_el is not None:
                last = child_text(name_el, "surname")
                fore = child_text(name_el, "given-names")
                if last or fore:
                    authors.append(f"{fore} {last}".strip() if fore else last)

    if abstract_parts:
        # Prefer shortest unique abstract block (avoid body-sized duplicates)
        abstract_parts = sorted(set(abstract_parts), key=len)
        out["abstract"] = abstract_parts[0][:50000]
    if body_parts:
        out["body_text"] = body_parts[0][:500000]
    if authors:
        out["authors"] = authors
    return out


def iter_book_parts(root) -> Iterator:
    """Yield every <book-part> element under root, depth-first, including nested."""
    for el in root.iter():
        if local_name(el.tag) == "book-part":
            yield el
