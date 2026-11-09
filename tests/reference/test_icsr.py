"""E2B(R3) ICSR checks driven by the generated rule file ``ich_e2b_r3.json``."""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from episteme.reference.rules import Finding, load_rules
from episteme.reference.rules.icsr import check_icsr

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "icsr"
VALID = (FIXTURES / "valid_min.xml").read_bytes()
TODAY = date(2026, 11, 9)
IG_PACKAGE = Path(
    "ich", "page_e2br3-individual-case-safety-report-icsr", "IG_Complete_Package_v1_11_1"
)

C13 = b'<value xsi:type="CE" code="1" codeSystem="2.16.840.1.113883.3.989.2.1.1.2"/>'
REACTION = b'code="10022617" codeSystem="2.16.840.1.113883.6.163" codeSystemVersion="15.1"'


def _replace(old: bytes, new: bytes) -> bytes:
    assert VALID.count(old) == 1, old
    return VALID.replace(old, new)


def _kinds(findings: list[Finding]) -> set[tuple[str, str]]:
    return {(f.rule_id, f.clause) for f in findings}


def test_rule_file_loads_with_its_sources():
    rules = load_rules("ich_e2b_r3")
    assert rules.ig == "ICH E2B(R3)"
    xlsx = next(d for d in rules.source_docs if d["file"].endswith(".xlsx"))
    assert xlsx["sha256"].startswith("d1afe71eb57e4219")
    elements = {e["element"]: e for e in rules.data["elements"]}
    assert len(elements) == 223
    assert elements["C.1.3"]["codelist"] == "CL2"
    assert "1" in rules.data["codelists"]["CL2"]["codes"]


def test_minimal_valid_icsr_passes():
    assert check_icsr(VALID, today=TODAY) == []


def test_missing_mandatory_element_is_named():
    findings = check_icsr((FIXTURES / "missing_mandatory.xml").read_bytes(), today=TODAY)
    assert ("C.1.3", "mandatory") in _kinds(findings)
    assert any("C.1.3" in f.message and "Type of Report" in f.message for f in findings)


# Observation codes (CL19) of the mandatory per-reaction elements in valid_min.xml.
REACTION_CHILDREN = {
    "E.i.3.2a": b"34",
    "E.i.3.2b": b"21",
    "E.i.3.2c": b"33",
    "E.i.3.2d": b"35",
    "E.i.3.2e": b"12",
    "E.i.3.2f": b"26",
    "E.i.7": b"27",
}


@pytest.mark.parametrize("element", sorted(REACTION_CHILDREN))
def test_missing_mandatory_child_of_a_repeat_block_is_named(element):
    code = REACTION_CHILDREN[element]
    xml = _replace(b'<code code="' + code + b'" ', b'<code code="99" ')  # drops the element
    assert (element, "mandatory") in _kinds(check_icsr(xml, today=TODAY))


def test_conditional_rule_fires():
    # C.5.4 (study type) is required if C.1.3 = 2 (report from study).
    xml = _replace(C13, C13.replace(b'code="1"', b'code="2"'))
    findings = check_icsr(xml, today=TODAY)
    assert ("C.5.4", "conditional") in _kinds(findings)
    assert all(f.clause != "conditional" for f in check_icsr(VALID, today=TODAY))


def test_bad_date_precision_is_reported():
    # C.1.2 needs date and time to the second.
    xml = VALID.replace(
        b'<effectiveTime value="20120720100001+09"/>', b'<effectiveTime value="20120720"/>'
    )
    findings = check_icsr(xml, today=TODAY)
    assert ("C.1.2", "date") in _kinds(findings)
    assert any("C.1.2" in f.message and "second" in f.message for f in findings)


def test_future_date_is_reported():
    xml = _replace(b'<low value="20090622"/>', b'<low value="20301231"/>')
    assert ("C.1.4", "date") in _kinds(check_icsr(xml, today=TODAY))


def test_code_outside_codelist_is_reported():
    xml = _replace(C13, C13.replace(b'code="1"', b'code="7"'))
    findings = check_icsr(xml, today=TODAY)
    assert ("C.1.3", "codelist") in _kinds(findings)
    assert any("CL2" in f.message and "'7'" in f.message for f in findings)


def test_message_header_must_match_case_identifier():
    n2r1 = b'<id extension="JP-DSJP-DSJ-2009-98765-1" root="2.16.840.1.113883.3.989.2.1.3.1"/>'
    n2r1 += b"\n    <!-- N.2.r.1"
    xml = _replace(n2r1, n2r1.replace(b"JP-DSJP-DSJ-2009-98765-1", b"OTHER"))
    assert ("N.2.r.1", "consistency") in _kinds(check_icsr(xml, today=TODAY))


def test_meddra_code_shape_only():
    # Any 8-digit code passes: there is no MedDRA term lookup.
    unknown = _replace(REACTION, REACTION.replace(b"10022617", b"99999999"))
    assert check_icsr(unknown, today=TODAY) == []
    bad = _replace(REACTION, REACTION.replace(b"10022617", b"abc"))
    findings = check_icsr(bad, today=TODAY)
    assert ("E.i.2.1b", "meddra") in _kinds(findings)
    no_version = _replace(REACTION, REACTION.replace(b' codeSystemVersion="15.1"', b""))
    assert ("E.i.2.1a", "meddra") in _kinds(check_icsr(no_version, today=TODAY))


def test_malformed_xml_returns_one_finding_not_an_exception():
    findings = check_icsr(b"<MCCI_IN200100UV01><id></MCCI_IN200100UV01>")
    assert len(findings) == 1
    assert findings[0].clause == "parse"


def test_xml_with_external_entity_is_refused():
    xml = (
        b'<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>\n'
        b'<MCCI_IN200100UV01 xmlns="urn:hl7-org:v3">&x;</MCCI_IN200100UV01>'
    )
    findings = check_icsr(xml)
    assert len(findings) == 1
    assert findings[0].clause == "parse"
    assert "refused" in findings[0].message


def test_other_root_element_is_one_structure_finding():
    findings = check_icsr(b'<MCCI_IN200101UV01 xmlns="urn:hl7-org:v3"/>')
    assert [f.clause for f in findings] == ["structure"]


def test_eu_region_without_eu_rules_reports_not_loaded():
    assert check_icsr(VALID, region="eu") == [
        Finding("ICSR", "region", "EU regional rules not loaded")
    ]
    assert check_icsr(VALID, region="fda") == [
        Finding("ICSR", "region", "FDA regional rules not loaded")
    ]
    with pytest.raises(ValueError, match="region"):
        check_icsr(VALID, region="pmda")  # type: ignore[arg-type]


def _ig_package() -> Path:
    from episteme.config import ConfigError, get_settings

    get_settings.cache_clear()
    try:
        package = get_settings().raw_root / IG_PACKAGE
    except ConfigError as exc:
        pytest.skip(f"settings not usable: {exc}")
    finally:
        get_settings.cache_clear()
    if not package.is_dir():
        pytest.skip(f"IG package not in the raw root ({IG_PACKAGE})")
    return package


# ICH example 3 leaves out seriousness criteria that the ICH sheet marks Mandatory
# (E.i.3.2a-f, "true" or nullFlavor NI): it sends only E.i.3.2a, and E.i.3.2c in reaction 3.
# The check reports them; the expectation names them exactly, so any other mandatory or
# code-list finding fails the test.
EXPECTED_EXAMPLE_FINDINGS = {
    "3_ExampleCase_Clinical_trial_v1_0.xml": {
        "E.i.3.2b": 3,
        "E.i.3.2c": 2,
        "E.i.3.2d": 3,
        "E.i.3.2e": 3,
        "E.i.3.2f": 3,
    },
}


def test_ich_example_instances_pass():
    package = _ig_package()
    examples = sorted(
        p for d in package.rglob("6_Example Instances") for p in d.glob("*.xml") if p.is_file()
    )
    assert len(examples) == 5, [p.name for p in examples]
    for path in examples:
        findings = check_icsr(path.read_bytes())
        assert [f for f in findings if f.clause in ("parse", "structure")] == [], path.name
        counts = Counter(f.rule_id for f in findings if f.clause in ("mandatory", "codelist"))
        assert dict(counts) == EXPECTED_EXAMPLE_FINDINGS.get(path.name, {}), path.name
