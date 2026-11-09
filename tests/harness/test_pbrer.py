from __future__ import annotations

from datetime import date

import pytest

from episteme.harness import pbrer
from episteme.harness.pbrer import SECTION_SOURCES, assemble_facts, gate, skeleton
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance
from episteme.reference.rules import MAH_MARKER, headings, load_rules

START, END = date(2025, 1, 1), date(2025, 12, 31)
TRIALS_HEADING = (
    "7. Summaries of Significant Findings from Clinical Trials during the Reporting Period"
)


def _claim(text: str, source: str = "src") -> Claim:
    return Claim(
        text=text,
        value=None,
        method="fetched",
        provenance=Provenance(source, "r1", None, "k1", None),
        confidence=1.0,
        confidence_basis="exact",
    )


def make_lookup(empty=(), calls=None):
    def lookup(name: str, kwargs: dict) -> LookupResult:
        if calls is not None:
            calls.append((name, kwargs))
        if name in empty:
            return LookupResult((), Coverage(0, missing=(f"{name}_src",), notes=(f"{name} note",)))
        text = f"{name} fact"
        if name == "faers_counts":
            text += " (US public spontaneous reports (FAERS))"
        return LookupResult((_claim(text, name),), Coverage(1))

    return lookup


def facts(**kw):
    return assemble_facts(make_lookup(**kw), 7, START, END, tenant="t", caller="c")


def test_section_sources_match_spec_map():
    assert SECTION_SOURCES == {
        "2": ("approval_status",),
        "3": ("safety_actions",),
        "4": ("current_label",),
        "7": ("trials",),
        "9": ("faers_counts",),
        "11": ("literature",),
    }


def test_assemble_passes_kwargs():
    calls: list = []
    assemble_facts(make_lookup(calls=calls), 7, START, END, tenant="t", caller="c")
    by = dict(calls)
    assert by["safety_actions"]["start"] == START and by["safety_actions"]["end"] == END
    assert "start" not in by["trials"]
    assert by["trials"]["substance_key"] == 7 and by["trials"]["tenant"] == "t"


@pytest.mark.parametrize("region", ["ich", "eu"])
def test_skeleton_has_every_section_in_order(region):
    rules = load_rules("ich_e2c_r2")
    assert list(skeleton(facts(), region=region)) == headings(rules, region)
    assert ("A6.3" in [s.id for s in rules.sections_for(region)]) == (region == "eu")


@pytest.mark.parametrize("region", ["ich", "eu"])
def test_mah_sections_carry_the_marker(region):
    rules = load_rules("ich_e2c_r2")
    draft = skeleton(facts(), region=region)
    mah = [s for s in rules.sections_for(region) if s.mah_data]
    assert mah
    for s in mah:
        assert MAH_MARKER in draft[s.heading]
    assert draft["6.1. Reference Information"].startswith(MAH_MARKER)


def test_empty_lookup_section_states_the_gap():
    draft = skeleton(facts(empty=("trials", "approval_status")))
    assert draft[TRIALS_HEADING] == "Coverage gap: trials_src\nCoverage gap: trials note"
    assert "Coverage gap: approval_status_src" in draft["2. Worldwide Marketing Approval Status"]
    assert all(b.strip() for b in draft.values())


def test_claims_carry_provenance():
    body = skeleton(facts())["2. Worldwide Marketing Approval Status"]
    assert "approval_status fact [approval_status, release r1, k1]" in body


def test_faers_section_keeps_the_public_label():
    for f in (facts(), facts(empty=("faers_counts",))):
        body = skeleton(f)["9. Information from Other Clinical Trials and Sources"]
        assert "US public spontaneous reports (FAERS)" in body


def test_gate_accepts_the_skeleton():
    assert gate(skeleton(facts())) == []
    all_names = tuple(n for v in SECTION_SOURCES.values() for n in v)
    assert gate(skeleton(facts(empty=all_names))) == []
    assert gate(skeleton(facts(), region="eu"), region="eu") == []


def test_eu_region_requires_regional_sections():
    ids = {f.rule_id for f in gate(skeleton(facts()), region="eu")}
    assert "A6.3" in ids


def test_gate_rejects_a_draft_missing_a_section():
    draft = skeleton(facts())
    del draft["4. Changes to Reference Safety Information"]
    assert [f.rule_id for f in gate(draft)] == ["4"]


def test_gate_rejects_missing_marker():
    draft = skeleton(facts())
    draft["6.1. Reference Information"] = "Some text"
    assert [f.rule_id for f in gate(draft)] == ["6.1"]


def test_gate_rejects_marker_with_other_content():
    draft = skeleton(facts())
    draft["6.1. Reference Information"] = f"{MAH_MARKER}\nThe CCSI says X."
    findings = gate(draft)
    assert [f.rule_id for f in findings] == ["6.1"]
    assert "plus other content" in findings[0].message


def test_gate_allows_marker_with_skeleton_gap_text():
    draft = skeleton(facts())
    draft["6.1. Reference Information"] = f"{MAH_MARKER}\n{pbrer.GAP_PREFIX}label history"
    assert gate(draft) == []
