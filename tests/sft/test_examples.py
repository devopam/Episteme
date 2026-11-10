import json
from datetime import date

import pytest

from episteme.harness import loop
from episteme.harness.pbrer import PbrerFacts, skeleton
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance
from episteme.reference.rules import Finding
from episteme.sft.examples import (
    ABSTAIN_REASONS,
    PROMPTS,
    Abstain,
    GroundedQA,
    RuleCheck,
    ToolDraft,
    build_messages,
    parse_example,
)

PASSAGE = "In the trial, 412 patients received the drug. Follow-up ended on 2019-06-30."


def _result(n: str = "1234") -> LookupResult:
    prov = Provenance("faers", "2024Q4", "f.parquet", "KEY9", "US")
    return LookupResult((Claim(f"{n} reports", n, "computed", prov, 1.0, "count"),), Coverage(1))


def _tool_draft() -> ToolDraft:
    return ToolDraft(
        task="Report FAERS counts for substance 7 from 2024-01-01 to 2024-12-31.",
        tool_calls=(
            {"name": "resolve_entity", "arguments": {"text": "drugA", "tenant": "t"}},
            {
                "name": "faers_counts",
                "arguments": {"substance_key": 7, "start": "2024-01-01", "end": "2024-12-31"},
            },
        ),
        tool_results=(_result("7"), _result()),
        answer="There were 1234 reports from 2024-01-01 to 2024-12-31.",
    )


def _passage(i: int, **kw) -> dict:
    base = {
        "id": f"PMC{i}",
        "source": "pmc",
        "subset": "commercial",
        "text": PASSAGE,
        "content_hash": f"h{i}",
    }
    return {**base, **kw}


# --- example kinds ------------------------------------------------------------


def test_examples_render_in_harness_tool_format():
    ex = _tool_draft()
    chat = ex.to_chat()
    assert chat[0] == {"role": "user", "content": ex.task}
    call_msg = chat[1]
    assert call_msg["role"] == "assistant"
    calls = call_msg["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["resolve_entity", "faers_counts"]
    tool_names = {t["function"]["name"] for t in loop.TOOLS}
    for c in calls:
        assert c["type"] == "function"
        assert c["function"]["name"] in tool_names
        args = json.loads(c["function"]["arguments"])
        assert "tenant" not in args and "caller" not in args
    tool_msgs = chat[2:-1]
    assert [m["tool_call_id"] for m in tool_msgs] == [c["id"] for c in calls]
    assert tool_msgs[1]["content"] == json.dumps(_result().to_json())
    assert chat[-1] == {"role": "assistant", "content": ex.answer}


def test_tool_draft_chat_replays_through_the_harness_loop():
    ex = _tool_draft()
    chat = ex.to_chat()
    payloads: list[dict] = []
    replies = [{"choices": [{"message": chat[1]}]}, {"choices": [{"message": chat[-1]}]}]

    def post(payload):
        payloads.append(json.loads(json.dumps(payload)))
        return replies.pop(0)

    recorded = list(ex.tool_results)
    seen: list[tuple[str, dict]] = []

    def call_tool(name, args):
        seen.append((name, args))
        return recorded.pop(0)

    answer, results = loop.run(
        chat[:1], call_tool=call_tool, post=post, max_tool_calls=5, tenant="t", caller="c"
    )
    assert answer == ex.answer
    assert results == list(ex.tool_results)
    assert payloads[1]["messages"] == json.loads(json.dumps(chat[:-1]))
    assert [n for n, _ in seen] == ["resolve_entity", "faers_counts"]


def test_other_kinds_render_as_user_then_assistant():
    qa = GroundedQA("Q?", PASSAGE, "PMC1", '"412 patients"')
    chat = qa.to_chat()
    assert [m["role"] for m in chat] == ["user", "assistant"]
    assert PASSAGE in chat[0]["content"] and "PMC1" in chat[0]["content"]
    assert chat[1]["content"] == qa.answer
    ab = Abstain("What is the exposure?", "MAH data required")
    assert ab.to_chat() == [
        {"role": "user", "content": "What is the exposure?"},
        {"role": "assistant", "content": "MAH data required"},
    ]
    rc = RuleCheck({"A": "b"}, (Finding("r1", "1.1", "msg"),))
    rc_chat = rc.to_chat()
    assert [m["role"] for m in rc_chat] == ["user", "assistant"]
    assert json.loads(rc_chat[1]["content"]) == [
        {"rule_id": "r1", "clause": "1.1", "message": "msg"}
    ]


def test_abstain_reason_is_one_of_two_exact_strings():
    assert ABSTAIN_REASONS == ("MAH data required", "unverified")
    with pytest.raises(ValueError):
        Abstain("Q?", "mah data required")
    with pytest.raises(ValueError):
        Abstain("Q?", "unknown")


def test_prompts_ask_for_json_only():
    assert set(PROMPTS) == {"grounded_qa", "tool_draft", "rule_check", "abstain"}
    for prompt in PROMPTS.values():
        assert "JSON" in prompt


def test_parse_takes_grounding_from_the_input_not_the_teacher():
    raw = json.dumps({"question": "Q?", "answer": '"x"', "passage": "forged", "passage_ref": "z"})
    ex = parse_example("grounded_qa", _passage(1), raw)
    assert ex.passage == PASSAGE and ex.passage_ref == "PMC1"
    task = {
        "task": "t",
        "tool_calls": [{"name": "trials", "arguments": {"substance_key": 7}}],
        "tool_results": [_result().to_json()],
    }
    raw = json.dumps({"answer": "1234 reports", "tool_results": []})
    ex = parse_example("tool_draft", _passage(2, **task), raw)
    assert ex.tool_results == (_result(),)
    ab = parse_example("abstain", _passage(3, reason="unverified"), json.dumps({"question": "Q?"}))
    assert ab.reason == "unverified"


def test_build_messages_carries_the_kind_prompt():
    msgs = build_messages("grounded_qa", _passage(1))
    assert msgs[0] == {"role": "system", "content": PROMPTS["grounded_qa"]}
    assert PASSAGE in msgs[1]["content"]


def test_build_messages_sends_pbrer_headings_and_skeleton():
    facts = PbrerFacts(
        LookupResult((), Coverage(0)), (date(2024, 1, 1), date(2024, 12, 31)), sections={}
    )
    draft = skeleton(facts)
    row = _passage(
        1,
        task="Draft the PBRER.",
        tool_calls=[],
        tool_results=[],
        pbrer_region="ich",
        skeleton=draft,
    )
    user = json.loads(build_messages("tool_draft", row)[1]["content"])
    assert user["headings"] == list(draft)
    assert user["skeleton"] == draft
    assert "headings" in PROMPTS["tool_draft"]
    plain = json.loads(
        build_messages("tool_draft", _passage(2, task="t", tool_results=[]))[1]["content"]
    )
    assert "headings" not in plain


# --- final review I2: duplicate JSON keys are a format error -------------------------


def _pbrer_row() -> dict:
    return _passage(
        9,
        task="Draft the PBRER.",
        tool_calls=[],
        tool_results=[],
        pbrer_region="ich",
    )


def test_duplicate_heading_in_object_answer_is_rejected():
    from episteme.sft.examples import FormatError

    raw = '{"answer": {"1. Introduction": "a", "1. Introduction": "b"}}'
    with pytest.raises(FormatError, match="duplicate"):
        parse_example("tool_draft", _pbrer_row(), raw)


def test_duplicate_top_level_key_is_rejected():
    from episteme.sft.examples import FormatError

    with pytest.raises(FormatError, match="duplicate"):
        parse_example(
            "abstain", _passage(3, reason="unverified"), '{"question": "a", "question": "b"}'
        )
