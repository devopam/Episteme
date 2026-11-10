import json
from datetime import date

import pytest

from episteme.harness import loop
from episteme.harness.pbrer import PbrerFacts, gate, skeleton
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
    generate_batch,
    parse_example,
)
from episteme.sft.teacher import Teacher

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


def _qa_json(answer: str = '"412 patients received the drug" [PMC1].') -> str:
    return json.dumps({"question": "How many patients received the drug?", "answer": answer})


def _teacher(session, budget: int = 1_000_000) -> Teacher:
    return Teacher("https://teacher.invalid/v1", "sk-x", "teach-1", budget, session=session)


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


# --- generate_batch -------------------------------------------------------------


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_malformed_teacher_json_is_rejected_and_counted(fakes, tmp_path):
    session = fakes.FakeSession(
        [
            fakes.chat_reply("not json at all", {"total_tokens": 10}),
            fakes.chat_reply("```json\n" + _qa_json() + "\n```", {"total_tokens": 10}),
            fakes.chat_reply(json.dumps({"question": "Q?"}), {"total_tokens": 10}),
            fakes.chat_reply(_qa_json(), {"total_tokens": 10}),
        ]
    )
    passages = [_passage(i) for i in range(1, 5)]
    out = generate_batch(_teacher(session), passages, "grounded_qa", 5, tmp_path, excluded=())
    assert out["kept"] == 1
    assert out["rejected"]["format"] == 3
    assert out["stopped"] == "exhausted"
    rows = _jsonl(tmp_path / "examples.jsonl")
    assert len(rows) == 1


def test_batch_manifest_records_teacher_and_rejections(fakes, tmp_path):
    session = fakes.FakeSession(
        [
            fakes.chat_reply(_qa_json(), {"total_tokens": 11}),
            fakes.chat_reply(_qa_json('"512 patients received"'), {"total_tokens": 12}),
            fakes.chat_reply(_qa_json('"412 patients" ended 2020-02-02'), {"total_tokens": 13}),
            fakes.chat_reply(_qa_json(), {"total_tokens": 14}),
        ]
    )
    passages = [
        _passage(1),
        _passage(2, subset="noncommercial"),
        _passage(3, source="dailymed"),
        _passage(4, subset=None),
        _passage(10, source=None),
        {k: v for k, v in _passage(11).items() if k != "source"},
        _passage(5),
        _passage(6),
        _passage(7),
        _passage(8),
        _passage(9),
    ]
    out = generate_batch(
        _teacher(session), passages, "grounded_qa", 2, tmp_path, excluded=("dailymed",)
    )
    # Five refused rows cost no request; the run stops at the second kept example.
    assert len(session.posts) == 4
    assert out["kept"] == 2
    assert out["rejected"]["licence"] == 5
    assert out["rejected"]["citation"] == 1
    assert out["rejected"]["exact_values"] == 1
    assert out["stopped"] is None
    assert out["budget_used"] == 50
    doc = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert doc["kind"] == "batch"
    assert doc["teacher"] == "teach-1"
    assert doc["budget_used"] == 50
    assert doc["counts"]["kept"] == 2
    assert doc["counts"]["rejected_licence"] == 5
    assert doc["counts"]["rejected_citation"] == 1
    assert doc["counts"]["rejected_exact_values"] == 1
    assert doc["counts"]["rejected_format"] == 0
    assert doc["config"]["kind"] == "grounded_qa"
    assert doc["config"]["n"] == 2
    assert doc["stopped"] is None
    outcomes = {i["id"]: i["outcome"] for i in doc["inputs"]}
    assert outcomes["PMC1"] == "kept" and outcomes["PMC2"] == "licence"
    assert outcomes["PMC10"] == outcomes["PMC11"] == "licence"
    assert "PMC8" not in outcomes
    rows = _jsonl(tmp_path / "examples.jsonl")
    assert [r["source_row"]["id"] for r in rows] == ["PMC1", "PMC7"]
    assert all(r["teacher"] == "teach-1" for r in rows)
    assert rows[0]["messages"] == GroundedQA(**rows[0]["example"]).to_chat()


def test_batch_stops_cleanly_at_budget(fakes, tmp_path):
    session = fakes.FakeSession(
        [
            fakes.chat_reply(_qa_json(), {"total_tokens": 900}),
            fakes.chat_reply(_qa_json(), {"total_tokens": 900}),
        ]
    )
    passages = [_passage(i) for i in range(1, 4)]
    out = generate_batch(
        _teacher(session, budget=1000),
        passages,
        "grounded_qa",
        3,
        tmp_path,
        excluded=(),
        max_tokens=100,
    )
    assert out["stopped"] == "budget"
    assert out["kept"] == 1
    assert len(session.posts) == 1
    assert len(_jsonl(tmp_path / "examples.jsonl")) == 1
    doc = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert doc["stopped"] == "budget"
    assert doc["budget_used"] == 900


def test_batch_tool_draft_and_rule_check_and_abstain(fakes, tmp_path):
    facts = PbrerFacts(
        LookupResult((), Coverage(0)), (date(2024, 1, 1), date(2024, 12, 31)), sections={}
    )
    doc = skeleton(facts)
    del doc[list(doc)[3]]
    expected = [{"rule_id": f.rule_id, "clause": f.clause, "message": "m"} for f in gate(doc)]
    task = {
        "task": "Report FAERS counts for substance 7 from 2024-01-01 to 2024-12-31.",
        "tool_calls": [
            {
                "name": "faers_counts",
                "arguments": {"substance_key": 7, "start": "2024-01-01", "end": "2024-12-31"},
            }
        ],
        "tool_results": [_result().to_json()],
    }
    session = fakes.FakeSession(
        [
            fakes.chat_reply(json.dumps({"answer": "1234 reports."}), {"total_tokens": 1}),
            fakes.chat_reply(json.dumps({"findings": expected}), {"total_tokens": 1}),
            fakes.chat_reply(json.dumps({"question": "Exposure?"}), {"total_tokens": 1}),
        ]
    )
    t = _teacher(session)
    a = generate_batch(t, [_passage(1, **task)], "tool_draft", 1, tmp_path / "a", excluded=())
    b = generate_batch(t, [_passage(2, document=doc)], "rule_check", 1, tmp_path / "b", excluded=())
    c = generate_batch(
        t, [_passage(3, reason="MAH data required")], "abstain", 1, tmp_path / "c", excluded=()
    )
    assert (a["kept"], b["kept"], c["kept"]) == (1, 1, 1)
    row = _jsonl(tmp_path / "a" / "examples.jsonl")[0]
    assert row["tools"] == loop.TOOLS
    assert row["messages"][1]["tool_calls"][0]["function"]["name"] == "faers_counts"


def test_input_missing_fields_is_rejected_without_a_request(fakes, tmp_path):
    session = fakes.FakeSession([])
    out = generate_batch(
        _teacher(session), [_passage(1, text="")], "grounded_qa", 1, tmp_path, excluded=()
    )
    assert out["rejected"]["input"] == 1
    assert session.posts == []


def test_unknown_kind_is_refused(fakes, tmp_path):
    with pytest.raises(ValueError):
        generate_batch(_teacher(fakes.FakeSession([])), [], "essay", 1, tmp_path, excluded=())


def test_max_tokens_defaults_to_the_setting(fakes, tmp_path, monkeypatch):
    from episteme import config as cfg

    cfg.get_settings.cache_clear()
    monkeypatch.setenv("EPISTEME_TEACHER_MAX_TOKENS", "77")
    try:
        session = fakes.FakeSession([fakes.chat_reply(_qa_json(), {"total_tokens": 1})])
        generate_batch(_teacher(session), [_passage(1)], "grounded_qa", 1, tmp_path, excluded=())
        assert session.posts[0]["json"]["max_tokens"] == 77
        doc = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert doc["config"]["max_tokens"] == 77
    finally:
        cfg.get_settings.cache_clear()
