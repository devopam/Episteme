"""parse_spl: identity, sections with offsets into the body text, substances."""

from __future__ import annotations

import pytest

from episteme.data.dailymed import spl

from ._dailymed_fixtures import ingredient, section, set_id, spl_xml

SID = set_id("ab", 7)


def test_parse_spl_reads_set_id_version_codes():
    xml = spl_xml(
        SID.upper(),
        version=3,
        doc_code="34390-5",
        ingredients=[
            ingredient("ACTIB", "AAAAAAAAAA", "SALT X", moiety="BBBBBBBBBB"),
            ingredient("IACT", "CCCCCCCCCC", "INACTIVE"),
        ],
        ndcs=("12345-678-01", "12345-678-02", "99999-111-05"),
        effective="20231201",
    )
    out = spl.parse_spl(xml)
    assert out["set_id"] == SID  # lower case
    assert out["version"] == 3
    assert out["doc_type"] == "34390-5"
    assert out["title"] == "Test Drug Tablets"  # markup removed
    assert out["effective_year"] == 2023
    # The active moiety is given in the XML; the inactive ingredient is left out.
    assert out["uniis"] == ["AAAAAAAAAA", "BBBBBBBBBB"]
    assert out["substances"] == [
        {"unii": "AAAAAAAAAA", "role": "active_ingredient"},
        {"unii": "BBBBBBBBBB", "role": "active_moiety"},
    ]
    assert out["ndcs"] == ["12345-678", "99999-111"]


def test_actim_ingredient_is_the_moiety_itself():
    out = spl.parse_spl(spl_xml(SID, ingredients=[ingredient("ACTIM", "DDDDDDDDDD", "MOIETY")]))
    assert out["substances"] == [{"unii": "DDDDDDDDDD", "role": "active_moiety"}]


def test_a_label_without_a_title_has_none():
    assert spl.parse_spl(spl_xml(SID, title=None))["title"] is None


def test_sections_keep_loinc_and_text():
    sections = [
        section("34067-9", "1 INDICATIONS", ["Line one.", "Line <sub>two</sub>."]),
        section("42229-5", "Unclassified", ["Kept with its code."]),
        section(None, "No code", ["Part of the body, not a section."]),
        section("34084-4", "6 ADVERSE", table=[["Event", "Rate"], ["Nausea", "5%"]]),
    ]
    out = spl.parse_spl(spl_xml(SID, sections=sections))
    got = {s["loinc"]: s for s in out["sections"]}
    assert list(got) == ["34067-9", "42229-5", "34084-4"]  # 48780-1 has no text: no entry
    assert got["34067-9"]["title"] == "1 INDICATIONS"
    assert got["34067-9"]["text"] == "1 INDICATIONS\nLine one.\nLine two."
    assert got["34084-4"]["text"] == "6 ADVERSE\nEvent | Rate\nNausea | 5%"
    assert "Part of the body, not a section." in out["body_text"]


def test_section_offsets_point_into_body_text():
    sections = [
        section("34067-9", "1 INDICATIONS", ["First."]),
        section(
            "34073-7",
            "7 DRUG INTERACTIONS",
            ["Parent text."],
            children=[
                section("42229-5", "7.1 Warfarin", ["Child coded."]),
                section(None, "7.2 Uncoded", ["Child uncoded."]),
            ],
        ),
        section("34071-1", "5 WARNINGS", ["Last é ü 日本."]),
    ]
    out = spl.parse_spl(spl_xml(SID, sections=sections))
    body = out["body_text"]
    for s in out["sections"]:
        assert body[s["start"] : s["end"]] == s["text"]
    by_code = {s["loinc"]: s for s in out["sections"]}
    parent, child = by_code["34073-7"], by_code["42229-5"]
    # A parent spans its subsections, coded or not; a coded child lies inside it.
    assert "Child coded." in parent["text"] and "Child uncoded." in parent["text"]
    assert parent["start"] <= child["start"] and child["end"] <= parent["end"]
    # Sections of the top level are joined by a blank line and do not overlap.
    first, last = by_code["34067-9"], by_code["34071-1"]
    assert body[first["end"] : first["end"] + 2] == "\n\n"
    assert parent["end"] + 2 == last["start"]
    assert out["body_text"].count("Child coded.") == 1  # no copy of the child text


def test_the_highlights_excerpt_is_not_read_twice():
    xml = spl_xml(SID, sections=[]).decode()
    xml = xml.replace(
        "</structuredBody>",
        '<component><section><code code="34067-9" codeSystem="2.16.840.1.113883.6.1"/>'
        "<excerpt><highlight><text><paragraph>HIGHLIGHT COPY</paragraph></text>"
        "</highlight></excerpt>"
        "<text><paragraph>Full text.</paragraph></text></section></component></structuredBody>",
    )
    out = spl.parse_spl(xml.encode())
    assert "HIGHLIGHT COPY" not in out["body_text"]
    assert "Full text." in out["body_text"]


@pytest.mark.parametrize(
    "bad",
    [
        b"<document xmlns='urn:hl7-org:v3'><versionNumber value='1'/></document>",
        b"<other xmlns='urn:hl7-org:v3'/>",
        b"not xml at all",
    ],
)
def test_a_document_without_identity_is_refused(bad):
    with pytest.raises((ValueError, SyntaxError)):
        spl.parse_spl(bad)


def test_a_missing_version_is_refused():
    xml = spl_xml(SID).replace(b'<versionNumber value="1"/>', b"")
    with pytest.raises(ValueError, match="versionNumber"):
        spl.parse_spl(xml)


def test_entity_expansion_is_refused():
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]>'
        b'<document xmlns="urn:hl7-org:v3"><title>&a;</title></document>'
    )
    with pytest.raises(Exception, match="(?i)entit|forbid"):
        spl.parse_spl(bomb)
