"""Parse one DailyMed SPL label (HL7 v3 XML) into plain text, labelled sections
and substance codes.

``parse_spl(xml)`` is a pure function over bytes (``defusedxml``: no entity
expansion, no external entities, no DTD fetch). It returns:

- ``set_id`` (lower case UUID) and ``version`` (int): the identity of the label.
  Always read from the XML: neither the XML file name nor the name of the inner
  zip is the ``setId``.
- ``doc_type``: the LOINC code of the document (``34391-3`` human Rx label,
  ``34390-5`` human OTC label), ``title`` (markup removed, ``None`` if the label has no
  title) and ``effective_year``.
- ``body_text``: the text of every section of the label, in document order, joined
  with a blank line (``"\\n\\n"``).
- ``sections``: one dict per section that has a LOINC code and some text:
  ``loinc``, ``title``, ``text``, ``start`` and ``end``. ``start`` / ``end`` are
  character offsets into ``body_text`` and ``body_text[start:end] == text``. A
  subsection belongs to the text of its parent: a parent spans its subsections
  (a coded subsection has its own entry, inside the span of the parent). A
  section without a LOINC code has no entry, but its text is part of the body and
  of the span of the section that holds it. Unclassified sections keep their code
  (``42229-5``).
- ``uniis``: the UNII codes of the active moieties and active ingredients (no
  repeats). ``substances``: the same as ``{"unii", "role"}`` dicts, where ``role`` is
  ``active_moiety`` or ``active_ingredient``. The XML gives the active moiety directly
  (``ingredientSubstance/activeMoiety/activeMoiety/code``), so it is read, not derived.
- ``ndcs``: the NDC product codes (labeler-product, no package segment).

Tables are part of the text (a row is a line, cells are joined with `` | ``).
Images carry no text. No special rule applies to a caption: its text is read as any
other text of the section.
"""

from __future__ import annotations

import re
from typing import Any

from defusedxml import ElementTree as ET

NS = "{urn:hl7-org:v3}"
LOINC_OID = "2.16.840.1.113883.6.1"
UNII_OID = "2.16.840.1.113883.4.9"
NDC_OID = "2.16.840.1.113883.6.69"

SECTION_SEPARATOR = "\n\n"
_ACTIVE_CLASSES = frozenset({"ACTIB", "ACTIM", "ACTIR"})
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# A line break before and after the text of these elements.
_BLOCK_TAGS = frozenset(
    {
        "paragraph",
        "item",
        "list",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "caption",
        "title",
        "text",
        "footnote",
        "linkHtml",
    }
)
_CELL_TAGS = frozenset({"td", "th"})
_SPACE_RE = re.compile(r"\s+")


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _flatten(el: Any, out: list[str]) -> None:
    """Append the text of ``el`` and its descendants to ``out``; ``\\n`` ends a
    block and ``\\t`` ends a table cell. Comments and processing instructions
    (their tag is not a string) have no text."""
    tag = _local(el.tag)
    if not isinstance(el.tag, str):
        return
    block = tag in _BLOCK_TAGS
    if block or tag == "br":
        out.append("\n")
    if el.text:
        out.append(el.text)
    for child in el:
        _flatten(child, out)
        if child.tail:
            out.append(child.tail)
    if tag in _CELL_TAGS:
        out.append("\t")
    if block:
        out.append("\n")


def plain_text(el: Any) -> str:
    """The text of ``el`` without markup: one line per block, cells of a row joined
    with `` | ``, runs of white space (a no-break space too) made one space, empty
    lines dropped."""
    parts: list[str] = []
    _flatten(el, parts)
    lines: list[str] = []
    for raw in "".join(parts).split("\n"):
        cells = [_SPACE_RE.sub(" ", c).strip() for c in raw.split("\t")]
        line = " | ".join(c for c in cells if c)
        if line:
            lines.append(line)
    return "\n".join(lines)


def _one_line(el: Any) -> str | None:
    text = _SPACE_RE.sub(" ", plain_text(el)).strip()
    return text or None


class _Body:
    """The body text as it is built: fragments joined with ``SECTION_SEPARATOR``,
    with the running character length, so every offset is known at once."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.length = 0

    def next_start(self) -> int:
        return self.length + (len(SECTION_SEPARATOR) if self.parts else 0)

    def add(self, fragment: str) -> None:
        self.length = self.next_start() + len(fragment)
        self.parts.append(fragment)


def _loinc_of(sec: Any) -> str | None:
    code = sec.find(f"{NS}code")
    if code is None or not code.get("code"):
        return None
    system = code.get("codeSystem")
    if system is not None and system != LOINC_OID:
        return None
    return str(code.get("code")).strip()


def _own_text(sec: Any) -> str:
    """The title and the ``text`` of a section, not its subsections. The
    ``excerpt`` (the highlights block) is not read: it repeats the section."""
    pieces: list[str] = []
    title = sec.find(f"{NS}title")
    if title is not None:
        t = _one_line(title)
        if t:
            pieces.append(t)
    for text_el in sec.findall(f"{NS}text"):
        t = plain_text(text_el)
        if t:
            pieces.append(t)
    return "\n".join(pieces)


def _walk(sec: Any, body: _Body, entries: list[dict[str, Any]]) -> None:
    loinc = _loinc_of(sec)
    start = body.next_start()
    n_before = len(body.parts)
    entry: dict[str, Any] | None = None
    if loinc is not None:
        title_el = sec.find(f"{NS}title")
        entry = {
            "loinc": loinc,
            "title": _one_line(title_el) if title_el is not None else None,
            "start": start,
            "end": start,
        }
        entries.append(entry)
    own = _own_text(sec)
    if own:
        body.add(own)
    for comp in sec.findall(f"{NS}component"):
        child = comp.find(f"{NS}section")
        if child is not None:
            _walk(child, body, entries)
    if entry is not None:
        entry["end"] = body.length if len(body.parts) > n_before else start


def _codes(el: Any, oid: str) -> list[str]:
    return [
        str(c.get("code")).strip()
        for c in el.iter(f"{NS}code")
        if c.get("codeSystem") == oid and c.get("code")
    ]


def _substances(root: Any) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(unii: str | None, role: str) -> None:
        if unii and (unii, role) not in seen:
            seen.add((unii, role))
            found.append({"unii": unii, "role": role})

    for ing in root.iter(f"{NS}ingredient"):
        if ing.get("classCode") not in _ACTIVE_CLASSES:
            continue
        sub = ing.find(f"{NS}ingredientSubstance")
        if sub is None:
            continue
        own = [
            c.get("code")
            for c in sub.findall(f"{NS}code")
            if c.get("codeSystem") == UNII_OID and c.get("code")
        ]
        if own:
            # An ACTIM ingredient is the moiety itself.
            add(
                own[0].strip(),
                "active_moiety" if ing.get("classCode") == "ACTIM" else "active_ingredient",
            )
        for wrapper in sub.findall(f"{NS}activeMoiety"):
            for inner in wrapper.findall(f"{NS}activeMoiety"):
                for code in _codes(inner, UNII_OID)[:1]:
                    add(code, "active_moiety")
    return found


def _ndc_products(root: Any) -> list[str]:
    out: list[str] = []
    for code in _codes(root, NDC_OID):
        product = "-".join(code.split("-")[:2])
        if product not in out:
            out.append(product)
    return out


def clean_set_id(raw: str | None) -> str:
    """The ``setId`` in lower case; ``ValueError`` when it is not a UUID."""
    set_id = (raw or "").strip().lower()
    if not _UUID_RE.match(set_id):
        raise ValueError(f"setId is missing or not a UUID: {set_id!r}")
    return set_id


def clean_version(raw: str | None, set_id: str = "") -> int:
    """``versionNumber`` as an int; ``ValueError`` when it is missing or not a number."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"versionNumber is missing or not a number (setId {set_id})") from None


def read_identity(stream: Any) -> tuple[str, int]:
    """``(set_id, version)`` of an SPL document read from the head of ``stream``.

    The reader stops at ``versionNumber`` (the header of the document, a few KB in),
    so the body of a large label is not read. Only the children of the root count:
    other ``setId`` elements (related documents) are deeper. ``ValueError`` when
    the head holds no identity."""
    depth = 0
    set_id: str | None = None
    for event, el in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            depth += 1
            if depth == 1 and el.tag != f"{NS}document":
                raise ValueError(f"not an SPL document: root element {el.tag!r}")
            continue
        if depth == 2 and el.tag == f"{NS}setId":
            set_id = clean_set_id(el.get("root"))
        elif depth == 2 and el.tag == f"{NS}versionNumber":
            if set_id is None:
                raise ValueError("versionNumber comes before setId")
            return set_id, clean_version(el.get("value"), set_id)
        depth -= 1
    raise ValueError("no setId and versionNumber in the document")


def parse_spl(xml: bytes) -> dict[str, Any]:
    """One SPL document -> the dict described in the module docstring.

    Raises ``ValueError`` for a document that is not an SPL document or that has
    no usable ``setId`` or ``versionNumber``; a malformed XML raises the parser's
    error. The caller records the label as failed."""
    root = ET.fromstring(xml)
    if root.tag != f"{NS}document":
        raise ValueError(f"not an SPL document: root element {root.tag!r}")
    set_el = root.find(f"{NS}setId")
    set_id = clean_set_id(set_el.get("root") if set_el is not None else None)
    ver_el = root.find(f"{NS}versionNumber")
    version = clean_version(ver_el.get("value") if ver_el is not None else None, set_id)

    code_el = root.find(f"{NS}code")
    doc_type = code_el.get("code") if code_el is not None else None
    title_el = root.find(f"{NS}title")
    eff = root.find(f"{NS}effectiveTime")
    year = None
    if eff is not None and re.match(r"^\d{4}", eff.get("value") or ""):
        year = int(str(eff.get("value"))[:4])

    body = _Body()
    entries: list[dict[str, Any]] = []
    for sb in root.iter(f"{NS}structuredBody"):
        for comp in sb.findall(f"{NS}component"):
            sec = comp.find(f"{NS}section")
            if sec is not None:
                _walk(sec, body, entries)
    body_text = SECTION_SEPARATOR.join(body.parts)
    sections = []
    for e in entries:
        if e["end"] > e["start"]:
            sections.append({**e, "text": body_text[e["start"] : e["end"]]})
    substances = _substances(root)
    uniis: list[str] = []
    for s in substances:
        if s["unii"] not in uniis:
            uniis.append(s["unii"])
    return {
        "set_id": set_id,
        "version": version,
        "doc_type": doc_type,
        "title": _one_line(title_el) if title_el is not None else None,
        "effective_year": year,
        "body_text": body_text,
        "sections": sections,
        "uniis": uniis,
        "substances": substances,
        "ndcs": _ndc_products(root),
    }
