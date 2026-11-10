import json
from datetime import date

from episteme.harness.pbrer import PbrerFacts, gate, skeleton
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance
from episteme.reference.rules import MAH_MARKER, Finding
from episteme.sft.checks import (
    check_citation_quotes_passage,
    check_exact_values,
    check_format,
    check_structure,
    run_checks,
)
from episteme.sft.examples import Abstain, GroundedQA, RuleCheck, ToolDraft

PASSAGE = (
    "In the trial, 412 patients   received the drug.\n"
    "The median follow-up was 18 months, ending on 2019-06-30."
)


def _qa(answer: str) -> GroundedQA:
    return GroundedQA("How many patients?", PASSAGE, "PMC123", answer)


def _result() -> LookupResult:
    prov = Provenance("faers", "2024Q4", "f.parquet", "KEY9", "US")
    claim = Claim("1234 reports in the interval", "1234", "computed", prov, 1.0, "count")
    return LookupResult((claim,), Coverage(1))


def _draft_example(answer: str, **kw) -> ToolDraft:
    return ToolDraft(
        task="Report FAERS counts for substance 7 from 2024-01-01 to 2024-12-31.",
        tool_calls=(
            {
                "name": "faers_counts",
                "arguments": {"substance_key": 7, "start": "2024-01-01", "end": "2024-12-31"},
            },
        ),
        tool_results=(_result(),),
        answer=answer,
        **kw,
    )


def _pbrer_draft() -> dict[str, str]:
    facts = PbrerFacts(
        LookupResult((), Coverage(0)), (date(2024, 1, 1), date(2024, 12, 31)), sections={}
    )
    return skeleton(facts)


# --- citation ---------------------------------------------------------------


def test_citation_in_passage_passes_after_whitespace_normalisation():
    ex = _qa('The passage says "412 patients received the drug" [PMC123].')
    assert check_citation_quotes_passage(ex) == []


def test_curly_quotes_are_spans_too():
    ex = _qa("It says “412 patients received\n the drug”.")
    assert check_citation_quotes_passage(ex) == []


def test_citation_not_in_passage_is_rejected():
    ex = _qa('The passage says "512 patients received the drug".')
    assert check_citation_quotes_passage(ex)


def test_answer_without_quoted_span_fails():
    assert check_citation_quotes_passage(_qa("412 patients received the drug."))
    assert check_citation_quotes_passage(_qa('Empty quote "" only.'))


def test_citation_check_ignores_other_kinds():
    assert check_citation_quotes_passage(Abstain("Q?", "unverified")) == []


# --- exact values -----------------------------------------------------------


def test_invented_value_is_rejected():
    ok = _qa('"412 patients received the drug" (PMC123), follow-up to 2019-06-30.')
    assert check_exact_values(ok) == []
    bad = _qa('"412 patients received the drug"; follow-up ended on 2020-01-15.')
    assert "2020-01-15" in check_exact_values(bad)
    ok_tool = _draft_example("There were 1234 reports from 2024-01-01 to 2024-12-31.")
    assert check_exact_values(ok_tool) == []
    bad_tool = _draft_example("There were 1243 reports from 2024-01-01 to 2024-12-31.")
    assert check_exact_values(bad_tool) == ["1243"]


def test_exact_values_scan_pbrer_bodies_not_headings():
    draft = _pbrer_draft()
    ex = _draft_example(json.dumps(draft), pbrer_region="ich")
    assert check_exact_values(ex) == []
    heading = next(iter(draft))
    draft[heading] = draft[heading] + "\n9999 reports"
    assert check_exact_values(_draft_example(json.dumps(draft), pbrer_region="ich")) == ["9999"]


# --- structure --------------------------------------------------------------


def test_pbrer_draft_passing_gate_is_kept():
    draft = _pbrer_draft()
    assert gate(draft) == []
    assert check_structure(_draft_example(json.dumps(draft), pbrer_region="ich")) == []


def test_pbrer_draft_failing_gate_is_rejected():
    draft = _pbrer_draft()
    mah_heading = next(h for h, b in draft.items() if b == MAH_MARKER)
    draft[mah_heading] = "We estimate exposure at 10 patient-years."
    problems = check_structure(_draft_example(json.dumps(draft), pbrer_region="ich"))
    assert problems
    missing = _pbrer_draft()
    del missing[list(missing)[3]]
    assert check_structure(_draft_example(json.dumps(missing), pbrer_region="ich"))


def test_rule_check_findings_must_match_the_gate():
    doc = _pbrer_draft()
    del doc[list(doc)[3]]
    expected = gate(doc)
    assert expected
    good = RuleCheck(doc, tuple(Finding(f.rule_id, f.clause, "teacher words") for f in expected))
    assert check_structure(good) == []
    assert check_structure(RuleCheck(doc, ()))
    extra = (*good.findings, Finding("made_up", "1.2", "invented"))
    assert check_structure(RuleCheck(doc, extra))


def test_plain_tool_answer_has_no_structure_check():
    assert check_structure(_draft_example("1234 reports.")) == []


# --- format -----------------------------------------------------------------


def test_format_requires_fields_and_known_tools():
    assert check_format(_qa('"412 patients"')) == []
    assert check_format(GroundedQA("", PASSAGE, "PMC123", "x"))
    bad_tool = ToolDraft(
        "task", ({"name": "no_such_tool", "arguments": {}},), (_result(),), "1234 reports"
    )
    assert check_format(bad_tool)
    unpaired = ToolDraft("task", (), (_result(),), "x")
    assert check_format(unpaired)
    assert check_format(_draft_example("not json", pbrer_region="ich"))
    assert check_format(_draft_example(json.dumps(["list"]), pbrer_region="ich"))


def test_run_checks_reports_first_failing_reason():
    assert run_checks(_qa('"412 patients received the drug"')) == []
    reasons = [r for r, _ in run_checks(_qa('"999 patients" were treated'))]
    assert reasons[0] == "citation"
