from pathlib import Path

import defusedxml.ElementTree as ET

from episteme.data.jats import (
    child_text,
    iter_book_parts,
    itertext,
    local_name,
    parse_jats_fields,
)

# SP1-β pmc fixture lives under tests/data/fixtures/pmc (see
# tests/data/test_extract_pmc.py, which uses the same fixture).
FIX = Path(__file__).parent / "data" / "fixtures" / "pmc"


def test_local_name_strips_namespace():
    assert local_name("{http://x}article") == "article"
    assert local_name("plain") == "plain"


def test_child_text_reads_nested_name(tmp_path):
    p = tmp_path / "a.xml"
    p.write_text(
        "<contrib><name><surname>Doe</surname><given-names>J</given-names></name></contrib>"
    )
    root = ET.parse(str(p)).getroot()
    name_el = root.find("name")
    assert child_text(name_el, "surname") == "Doe"
    assert child_text(name_el, "given-names") == "J"
    # child_text is direct-children-only by design: see extract_pmc.py
    # contrib handling, which descends to <name> for exactly this reason.
    assert child_text(root, "surname") == ""


def test_itertext_flattens_markup_and_handles_none():
    assert itertext(None) == ""
    p = "<p>alpha <bold>beta</bold> gamma</p>"
    el = ET.fromstring(p)
    assert itertext(el) == "alpha beta gamma"


def test_parse_jats_fields_pmc_fixture():
    # reuse the SP1-β pmc fixture — parse must yield the same title/abstract
    # it did before jats.py
    xmls = sorted(FIX.rglob("*.xml"))
    assert xmls, "pmc fixture xml present"
    fields = parse_jats_fields(xmls[0])
    assert fields.get("title")
    assert "abstract" in fields


def test_iter_book_parts_yields_each_part(tmp_path):
    p = tmp_path / "book.xml"
    p.write_text(
        '<book><book-part id="p1"><book-part-meta><title>Ch1</title></book-part-meta>'
        "<body><p>one</p></body></book-part>"
        '<book-part id="p2"><body><p>two</p></body></book-part></book>'
    )
    root = ET.parse(str(p)).getroot()
    ids = [bp.get("id") for bp in iter_book_parts(root)]
    assert ids == ["p1", "p2"]
