"""Rule store: the E2C(R2) PBRER section list and the two section checks."""

from __future__ import annotations

import hashlib
import json
import re
from importlib import resources

import pytest

from episteme.reference.rules import (
    MAH_MARKER,
    Finding,
    check_mah_markers,
    check_sections,
    headings,
    load_rules,
    parse_rules,
)

GUIDELINE_PDF = "E2C_R2_Guideline.pdf"


@pytest.fixture(scope="module")
def rules():
    return load_rules("ich_e2c_r2")


def _ids(findings: list[Finding]) -> list[str]:
    return [f.rule_id for f in findings]


def test_complete_ordered_draft_has_no_findings(rules):
    assert check_sections(rules, headings(rules, "ich")) == []
    assert check_sections(rules, headings(rules, "eu"), region="eu") == []


def test_missing_section_is_named_with_clause(rules):
    draft = [
        h for h in headings(rules) if h != "5.1. Cumulative Subject Exposure in Clinical Trials"
    ]
    findings = check_sections(rules, draft)
    assert findings == [
        Finding(
            "5.1",
            "3.5.1",
            "missing required section '5.1. Cumulative Subject Exposure in Clinical Trials'"
            " (ICH E2C(R2) 3.5.1)",
        )
    ]


def test_optional_appendix_may_be_left_out(rules):
    draft = [h for h in headings(rules) if not h.startswith(("Appendix 3", "Appendix 5"))]
    assert check_sections(rules, draft) == []


def test_wrong_order_is_reported(rules):
    draft = headings(rules)
    i, j = draft.index("10. Non-Clinical Data"), draft.index("11. Literature")
    draft[i], draft[j] = draft[j], draft[i]
    findings = check_sections(rules, draft)
    assert _ids(findings) == ["10"]
    assert "wrong order" in findings[0].message
    assert findings[0].clause == "3.10"


def test_duplicate_section_is_reported(rules):
    draft = headings(rules)
    draft.insert(draft.index("12. Other Periodic Reports"), "11. Literature")
    findings = check_sections(rules, draft)
    assert _ids(findings) == ["11"]
    assert findings[0].clause == "3.11"
    assert "duplicate" in findings[0].message


def test_one_moved_section_yields_one_order_finding(rules):
    draft = headings(rules)
    draft.remove("19. Conclusions and Actions")
    draft.insert(draft.index("1. Introduction") + 1, "19. Conclusions and Actions")
    findings = check_sections(rules, draft)
    assert _ids(findings) == ["19"]
    assert findings[0].clause == "3.19"
    assert "wrong order" in findings[0].message


@pytest.mark.parametrize("region", ["ich", "eu"])
def test_165_heading_without_if_applicable(rules, region):
    full = "16.5. Effectiveness of Risk Minimisation (if applicable)"
    draft = headings(rules, region)
    draft[draft.index(full)] = "16.5. Effectiveness of Risk Minimisation"
    assert check_sections(rules, draft, region=region) == []
    unnumbered = [
        "Effectiveness of Risk Minimisation" if h == full else h for h in headings(rules, region)
    ]
    assert check_sections(rules, unnumbered, region=region) == []


def test_number_that_matches_no_candidate_is_unresolved(rules):
    # Section 2's title with number 3: no section has both, so the heading is unresolved.
    draft = headings(rules, "eu")
    draft[draft.index("2. Worldwide Marketing Approval Status")] = (
        "3. Worldwide Marketing Approval Status"
    )
    findings = check_sections(rules, draft, region="eu")
    assert _ids(findings) == ["2"]
    assert "missing" in findings[0].message


def test_heading_match_ignores_case_and_number(rules):
    draft = [s.title.upper() for s in rules.sections_for("ich")]
    assert check_sections(rules, draft) == []


def test_shared_title_resolves_by_number_or_order(rules):
    # "Reference Information" is both 6.1 and Appendix 1.
    draft = headings(rules)
    shared = {"6.1. Reference Information", "Appendix 1. Reference Information"}
    unnumbered = ["Reference Information" if h in shared else h for h in draft]
    assert check_sections(rules, unnumbered) == []
    without_61 = [h for h in draft if h != "6.1. Reference Information"]
    # The numbered heading resolves to Appendix 1, so only 6.1 is missing.
    assert _ids(check_sections(rules, without_61)) == ["6.1"]


def test_unnumbered_shared_title_does_not_take_another_slot(rules):
    # 6.1 is missing; an unnumbered "Reference Information" at the appendix position is A1.
    draft = [h for h in headings(rules) if h != "6.1. Reference Information"]
    draft[draft.index("Appendix 1. Reference Information")] = "Reference Information"
    findings = check_sections(rules, draft)
    assert _ids(findings) == ["6.1"]
    assert "missing" in findings[0].message


def test_region_ich_ignores_eu_entries(rules):
    eu_only = [s.heading for s in rules.sections if s.region == "eu"]
    assert eu_only, "GVP Module VII entries are expected"
    assert check_sections(rules, headings(rules) + eu_only) == []
    findings = check_sections(rules, headings(rules), region="eu")
    assert set(_ids(findings)) == {s.id for s in rules.sections if s.region == "eu"}


def test_eu_draft_may_use_gvp_wording(rules):
    gvp = {s.id: (s.aliases[-1] if s.aliases else s.title) for s in rules.sections_for("eu")}
    assert check_sections(rules, list(gvp.values()), region="eu") == []


def test_mah_section_without_marker_fails(rules):
    draft = {s.heading: (MAH_MARKER if s.mah_data else "text") for s in rules.sections_for("eu")}
    assert check_mah_markers(rules, draft) == []
    draft["5.2. Cumulative and Interval Patient Exposure from Marketing Experience"] = (
        "About 1.2 million patient-years."
    )
    findings = check_mah_markers(rules, draft)
    assert findings == [Finding("5.2", "3.5.2", findings[0].message)]
    assert MAH_MARKER in findings[0].message
    assert check_mah_markers(rules, draft, supplied={"5.2"}) == []


def test_mah_data_covers_exposure_tabulations_and_ccsi(rules):
    mah = {s.id for s in rules.sections if s.mah_data}
    assert {"5", "5.1", "5.2", "6", "6.1", "6.2", "6.3", "A1", "A2"} <= mah
    # Spec section 6 routes these to lookups or model prose, not to the marker.
    assert not mah & {"2", "3", "4", "7", "11", "15", "16", "17", "18", "19"}


def test_rule_file_ids_are_unique(rules):
    ids = [s.id for s in rules.sections]
    assert len(ids) == len(set(ids))
    assert {s.region for s in rules.sections} == {"ich", "eu"}
    assert all(s.clause for s in rules.sections)
    numbered = [s.id for s in rules.sections if s.region == "ich" and s.id[0].isdigit()]
    assert [n for n in numbered if "." not in n] == [str(n) for n in range(1, 21)]


# E2C(R2) 2.9.2 table of contents, with 9.1/9.2 (3.9.1/3.9.2), then the 3.20 appendices,
# then the GVP VII.B.5.20 item 6 regional appendix and VII.C.5.1-5.5.
SOURCE_ORDER = [
    "title_page", "exec_summary", "toc",
    "1", "2", "3", "4", "5", "5.1", "5.2", "6", "6.1", "6.2", "6.3",
    "7", "7.1", "7.2", "7.3", "7.4", "7.5", "8", "9", "9.1", "9.2", "10", "11", "12", "13",
    "14", "15", "16", "16.1", "16.2", "16.3", "16.4", "16.5", "17", "17.1", "17.2", "17.3",
    "18", "18.1", "18.2", "19", "20",
    "A1", "A2", "A3", "A4", "A5",
    "A6", "A6.1", "A6.2", "A6.3", "A6.4", "A6.5",
]  # fmt: skip


def test_rule_file_order_matches_source_numbering(rules):
    assert [s.id for s in rules.sections] == SOURCE_ORDER
    for s in rules.sections:
        if s.region == "ich" and s.id[0].isdigit():
            assert s.number == s.id
            assert s.clause == f"3.{s.id}"
        elif s.id in {"A1", "A2", "A3", "A4", "A5", "A6"}:
            assert s.number == f"Appendix {s.id[1:]}"


def test_loader_rejects_unknown_region(rules):
    data = json.loads(json.dumps(dict(rules.data), default=_plain))
    data["sections"][5]["region"] = "EU"
    with pytest.raises(ValueError, match=data["sections"][5]["id"]):
        parse_rules("ich_e2c_r2", data)


def test_rule_data_is_read_only(rules):
    with pytest.raises(TypeError):
        rules.data["ig"] = "x"
    with pytest.raises(TypeError):
        rules.source_docs[0]["sha256"] = "0" * 64


def test_eu_message_names_gvp_once(rules):
    draft = [h for h in headings(rules, "eu") if h != "Proposed product information"]
    (finding,) = check_sections(rules, draft, region="eu")
    assert finding.message.endswith("(EU GVP VII.C.5.1)")


def _plain(obj):
    return dict(obj) if hasattr(obj, "keys") else list(obj)


def test_rule_file_names_its_source_sha256(rules):
    assert rules.ig == "ICH E2C(R2)"
    assert rules.version == "Step 4, 2012-12-17"
    files = {d["file"]: d for d in rules.source_docs}
    assert GUIDELINE_PDF in files
    assert files[GUIDELINE_PDF]["source"] == "ich"
    for doc in rules.source_docs:
        assert doc["source"] in {"ich", "ema_guidance"}
        assert re.fullmatch(r"[0-9a-f]{64}", doc["sha256"])
    assert any(d["source"] == "ema_guidance" for d in rules.source_docs)
    assert "pending" not in rules.data


def test_rule_file_is_packaged_json():
    path = resources.files("episteme.reference.rules") / "data" / "ich_e2c_r2.json"
    assert json.loads(path.read_text(encoding="utf-8"))["ig"] == "ICH E2C(R2)"


def test_unknown_rule_set_is_named():
    with pytest.raises(FileNotFoundError, match="no_such_ig"):
        load_rules("no_such_ig")
    with pytest.raises(ValueError):
        load_rules("../ich_e2c_r2")


def _squash(text: str) -> str:
    """Case and whitespace normalisation; pypdf splits some words ("N on-Interventional")."""
    return re.sub(r"\s+", "", text).casefold()


def test_titles_match_source_pdf(rules):
    from episteme.config import get_settings

    raw_root = get_settings().raw_root / "ich"
    expected = next(d["sha256"] for d in rules.source_docs if d["file"] == GUIDELINE_PDF)
    pdfs = sorted(raw_root.rglob(GUIDELINE_PDF)) if raw_root.is_dir() else []
    pdf = next((p for p in pdfs if hashlib.sha256(p.read_bytes()).hexdigest() == expected), None)
    if pdf is None:
        pytest.skip(f"{GUIDELINE_PDF} with the rule file's sha256 is not under the raw root")
    from pypdf import PdfReader

    text = _squash("\n".join(page.extract_text() or "" for page in PdfReader(pdf).pages))
    missing = [
        s.title for s in rules.sections if s.region == "ich" and _squash(s.title) not in text
    ]
    assert missing == []


def test_check_mah_markers_strict_wants_the_marker_alone():
    rules = load_rules("ich_e2c_r2")
    head = "5.2. Cumulative and Interval Patient Exposure from Marketing Experience"
    assert check_mah_markers(rules, {head: "MAH data required"}, strict=True) == []
    bad = {head: "MAH data required\nmore"}
    assert check_mah_markers(rules, bad) == []
    assert [f.rule_id for f in check_mah_markers(rules, bad, strict=True)] == ["5.2"]
