"""Builders for small SPL documents and DailyMed zips (the real layout, trimmed).

The layout follows the real files: an outer zip holds inner zips named
``prescription/YYYYMMDD_<id>.zip`` or ``otc/...``; an inner zip holds one XML (its name
is NOT the setId) and images. The XML has ``setId``, ``versionNumber``, a LOINC
document code, a product data section (code 48780-1) with the ingredients, and the
text sections.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

LOINC = "2.16.840.1.113883.6.1"
UNII = "2.16.840.1.113883.4.9"
NDC = "2.16.840.1.113883.6.69"


def set_id(prefix: str, n: int = 0) -> str:
    """A UUID that starts with the two hex characters ``prefix``."""
    return f"{prefix}{n:06x}-aaaa-4bbb-8ccc-{n:012x}"


def section(code: str | None, title: str | None, paragraphs=(), children=(), table=None) -> str:
    code_xml = f'<code code="{code}" codeSystem="{LOINC}" displayName="X"/>' if code else ""
    title_xml = f"<title>{escape(title)}</title>" if title else ""
    text = "".join(f"<paragraph>{p}</paragraph>" for p in paragraphs)
    if table:
        rows = "".join(
            "<tr>" + "".join(f"<td>{escape(c)}</td>" for c in row) + "</tr>" for row in table
        )
        text += f"<table><tbody>{rows}</tbody></table>"
    text_xml = f"<text>{text}</text>" if text else ""
    kids = "".join(f"<component>{c}</component>" for c in children)
    return f"<section>{code_xml}{title_xml}{text_xml}{kids}</section>"


def ingredient(cls: str, unii: str, name: str, moiety: str | None = None) -> str:
    moiety_xml = ""
    if moiety:
        moiety_xml = (
            f'<activeMoiety><activeMoiety><code code="{moiety}" codeSystem="{UNII}"/>'
            f"<name>{name}</name></activeMoiety></activeMoiety>"
        )
    return (
        f'<ingredient classCode="{cls}"><ingredientSubstance>'
        f'<code code="{unii}" codeSystem="{UNII}"/><name>{name}</name>{moiety_xml}'
        f"</ingredientSubstance></ingredient>"
    )


def spl_xml(
    sid: str,
    version: int = 1,
    *,
    doc_code: str = "34391-3",
    title: str | None = "Test <b>Drug</b> Tablets",
    sections: list[str] | None = None,
    ingredients: list[str] | None = None,
    ndcs=("12345-678-01",),
    effective: str = "20240115",
) -> bytes:
    if sections is None:
        sections = [
            section("34067-9", "1 INDICATIONS", ["Used for <content>testing</content>."]),
            section("34073-7", "7 DRUG INTERACTIONS", ["Avoid warfarin with this drug."]),
        ]
    if ingredients is None:
        ingredients = [ingredient("ACTIB", "AAAAAAAAAA", "SALT X", moiety="BBBBBBBBBB")]
    ndc_xml = "".join(
        f'<containerPackagedProduct><code code="{n}" codeSystem="{NDC}"/>'
        "</containerPackagedProduct>"
        for n in ndcs
    )
    product = (
        f'<product><manufacturedProduct><code code="{ndcs[0].rsplit("-", 1)[0]}" '
        f'codeSystem="{NDC}"/><manufacturedMaterialKind>{"".join(ingredients)}'
        f"{ndc_xml}</manufacturedMaterialKind></manufacturedProduct></product>"
    )
    product_section = (
        f'<section><code code="48780-1" codeSystem="{LOINC}"/>'
        f"<subject>{product}</subject></section>"
    )
    title_xml = f"<title>{title}</title>" if title else ""
    body = "".join(f"<component>{s}</component>" for s in [product_section, *sections])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<document xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<id root="11111111-2222-4333-8444-555555555555"/>'
        f'<code code="{doc_code}" codeSystem="{LOINC}" displayName="LABEL"/>'
        f"{title_xml}"
        f'<effectiveTime value="{effective}"/>'
        f'<setId root="{sid}"/><versionNumber value="{version}"/>'
        f"<component><structuredBody>{body}</structuredBody></component></document>"
    ).encode()


def inner_zip(
    xml: bytes, *, xml_name: str = "label.xml", images: bool = True, image_bytes: int = 0
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(xml_name, xml)
        if images:
            zf.writestr("figure-1.jpg", b"\xff\xd8not a real image\xff\xd9")
        if image_bytes:
            zf.writestr("big.jpg", os.urandom(image_bytes), zipfile.ZIP_STORED)
    return buf.getvalue()


def outer_zip(path: Path, members: dict[str, bytes], compression: int = zipfile.ZIP_STORED) -> Path:
    """An outer zip of ``{member name: bytes}`` (the inner zips; stored by default)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path
