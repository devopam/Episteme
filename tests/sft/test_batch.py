import json
from datetime import date

import pytest
import requests

from episteme.harness.pbrer import PbrerFacts, gate, skeleton
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance
from episteme.sft.batch import TOOLS_SHA256, generate_batch
from episteme.sft.examples import GroundedQA
from episteme.sft.teacher import Teacher

PASSAGE = "In the trial, 412 patients received the drug. Follow-up ended on 2019-06-30."
INTERVAL = "from 2024-01-01 to 2024-12-31"


def _passage(i: int, **kw) -> dict:
    base = {
        "id": f"PMC{i}",
        "source": "pmc",
        "subset": "commercial",
        "text": PASSAGE,
        "content_hash": f"h{i}",
    }
    return {**base, **kw}


def _qa_json(ref: str, answer: str | None = None) -> str:
    answer = answer if answer is not None else f'"412 patients received the drug" [{ref}].'
    return json.dumps({"question": "How many patients received the drug?", "answer": answer})


def _teacher(session, budget: int = 1_000_000) -> Teacher:
    return Teacher("https://teacher.invalid/v1", "sk-x", "teach-1", budget, session=session)


def _result(source: str = "fda_enforcement", n: str = "1234") -> LookupResult:
    prov = Provenance(source, "r1", "f", "KEY9", "US")
    return LookupResult((Claim(f"{n} records", n, "fetched", prov, 1.0, "b"),), Coverage(1))


def _tool_row(i: int, *results: LookupResult, **kw) -> dict:
    row = {
        "id": f"T{i}",
        "task": f"Report the recalls for substance 7 {INTERVAL}.",
        "tool_calls": [
            {"name": "safety_actions", "arguments": {"substance_key": 7}} for _ in results
        ],
        "tool_results": [r.to_json() for r in results],
    }
    return {**row, **kw}


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _manifest(path) -> dict:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


def _skeleton() -> dict[str, str]:
    facts = PbrerFacts(
        LookupResult((), Coverage(0)), (date(2024, 1, 1), date(2024, 12, 31)), sections={}
    )
    return skeleton(facts)


# --- grounded_qa ------------------------------------------------------------------


def test_malformed_teacher_json_is_rejected_and_counted(fakes, tmp_path):
    session = fakes.FakeSession(
        [
            fakes.chat_reply("not json at all", {"total_tokens": 10}),
            fakes.chat_reply("```json\n" + _qa_json("PMC2") + "\n```", {"total_tokens": 10}),
            fakes.chat_reply(json.dumps({"question": "Q?"}), {"total_tokens": 10}),
            fakes.chat_reply(_qa_json("PMC4"), {"total_tokens": 10}),
        ]
    )
    passages = [_passage(i) for i in range(1, 5)]
    out = generate_batch(_teacher(session), passages, "grounded_qa", 5, tmp_path, excluded=())
    assert out["kept"] == 1
    assert out["rejected"]["format"] == 3
    assert out["stopped"] == "exhausted"
    assert len(_jsonl(tmp_path / "examples.jsonl")) == 1


def test_batch_manifest_records_teacher_and_rejections(fakes, tmp_path):
    session = fakes.FakeSession(
        [
            fakes.chat_reply(_qa_json("PMC1"), {"total_tokens": 11}),
            fakes.chat_reply(
                _qa_json("PMC5", '"512 patients received" [PMC5]'), {"total_tokens": 12}
            ),
            fakes.chat_reply(
                _qa_json("PMC6", '"412 patients" [PMC6] ended 2020-02-02'), {"total_tokens": 13}
            ),
            fakes.chat_reply(_qa_json("PMC7"), {"total_tokens": 14}),
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
    doc = _manifest(tmp_path)
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
    assert doc["config"]["tools_sha256"] == TOOLS_SHA256
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
            fakes.chat_reply(_qa_json("PMC1"), {"total_tokens": 900}),
            fakes.chat_reply(_qa_json("PMC2"), {"total_tokens": 900}),
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
    doc = _manifest(tmp_path)
    assert doc["stopped"] == "budget"
    assert doc["budget_used"] == 900


# --- failure and resume -------------------------------------------------------------


def test_request_error_writes_manifest_then_reraises(fakes, tmp_path):
    session = fakes.FakeSession(
        [
            fakes.chat_reply(_qa_json("PMC1"), {"total_tokens": 5}),
            fakes.FakeResponse({}, status=503),
        ]
    )
    passages = [_passage(i) for i in range(1, 4)]
    with pytest.raises(requests.HTTPError):
        generate_batch(_teacher(session), passages, "grounded_qa", 3, tmp_path, excluded=())
    doc = _manifest(tmp_path)
    assert doc["stopped"] == "error"
    assert doc["error"] == "HTTPError"
    assert doc["counts"]["kept"] == 1
    assert doc["budget_used"] == 5
    assert [r["source_row"]["id"] for r in _jsonl(tmp_path / "examples.jsonl")] == ["PMC1"]


def test_rerun_resumes_by_id_and_keeps_paid_examples(fakes, tmp_path):
    first = fakes.FakeSession(
        [
            fakes.chat_reply(_qa_json("PMC1"), {"total_tokens": 5}),
            fakes.FakeResponse({}, status=503),
        ]
    )
    passages = [_passage(i) for i in range(1, 4)]
    with pytest.raises(requests.HTTPError):
        generate_batch(_teacher(first), passages, "grounded_qa", 3, tmp_path, excluded=())
    second = fakes.FakeSession(
        [
            fakes.chat_reply(_qa_json("PMC2"), {"total_tokens": 6}),
            fakes.chat_reply(_qa_json("PMC3"), {"total_tokens": 7}),
        ]
    )
    out = generate_batch(_teacher(second), passages, "grounded_qa", 3, tmp_path, excluded=())
    # PMC1 is already in examples.jsonl: no request, not rewritten.
    assert len(second.posts) == 2
    assert "PMC1" not in second.posts[0]["json"]["messages"][1]["content"]
    assert out["kept"] == 2
    assert out["kept_before"] == 1
    assert out["stopped"] is None
    ids = [r["source_row"]["id"] for r in _jsonl(tmp_path / "examples.jsonl")]
    assert ids == ["PMC1", "PMC2", "PMC3"]
    doc = _manifest(tmp_path)
    assert doc["counts"]["kept_before"] == 1
    assert doc["counts"]["skipped_done"] == 1
    # The manifest of the failed run is kept, not overwritten.
    old = list(tmp_path.glob("manifest.run-*.json"))
    assert len(old) == 1
    assert json.loads(old[0].read_text(encoding="utf-8"))["stopped"] == "error"


def test_resume_refuses_a_corrupt_examples_file(fakes, tmp_path):
    (tmp_path / "examples.jsonl").write_text('{"source_row": {"id": "PMC1"}}\n{"trunc', "utf-8")
    with pytest.raises(ValueError, match="examples.jsonl"):
        generate_batch(
            _teacher(fakes.FakeSession([])), [_passage(1)], "grounded_qa", 1, tmp_path, excluded=()
        )


# --- tool_draft, rule_check, abstain ------------------------------------------------


def test_tool_draft_licence_comes_from_claim_provenance(fakes, tmp_path):
    session = fakes.FakeSession(
        [
            fakes.chat_reply(
                json.dumps({"answer": f"1234 records {INTERVAL}."}), {"total_tokens": 1}
            ),
        ]
    )
    rows = [
        # dailymed is excluded from the corpus text but its looked-up facts are commercial.
        _tool_row(1, _result("dailymed"), source="dailymed", subset="commercial"),
        _tool_row(2, _result("openfda_faers")),
        _tool_row(3, _result("pubmed")),
        _tool_row(4, _result("no_such_source")),
        _tool_row(5, _result("fda_enforcement"), _result("openfda_faers")),
    ]
    out = generate_batch(
        _teacher(session), rows, "tool_draft", 5, tmp_path, excluded=("dailymed", "ema_epar")
    )
    assert out["kept"] == 1
    assert out["rejected"]["licence_pending"] == 2
    assert out["rejected"]["licence"] == 2
    assert len(session.posts) == 1
    row = _jsonl(tmp_path / "examples.jsonl")[0]
    assert row["source_row"]["id"] == "T1"
    assert row["source_row"]["lookup_sources"] == ["dailymed"]
    assert row["tools_sha256"] == TOOLS_SHA256
    assert "tools" not in row
    assert row["messages"][1]["tool_calls"][0]["function"]["name"] == "safety_actions"


def test_pbrer_tool_draft_batch_sends_headings_and_keeps_a_passing_draft(fakes, tmp_path):
    draft = _skeleton()
    bad = dict(draft)
    del bad[list(bad)[3]]
    session = fakes.FakeSession(
        [
            fakes.chat_reply(json.dumps({"answer": draft}), {"total_tokens": 1}),
            fakes.chat_reply(json.dumps({"answer": bad}), {"total_tokens": 1}),
        ]
    )
    rows = [
        _tool_row(1, pbrer_region="ich", skeleton=draft, task=f"Draft the PBRER {INTERVAL}."),
        _tool_row(2, pbrer_region="ich", skeleton=draft, task=f"Draft the PBRER {INTERVAL}."),
    ]
    out = generate_batch(_teacher(session), rows, "tool_draft", 2, tmp_path, excluded=())
    assert out["kept"] == 1
    assert out["rejected"]["structure"] == 1
    user = json.loads(session.posts[0]["json"]["messages"][1]["content"])
    assert user["headings"] == list(draft)
    assert user["skeleton"] == draft
    kept = _jsonl(tmp_path / "examples.jsonl")[0]
    assert json.loads(kept["example"]["answer"]) == draft


def test_rule_check_rows_use_the_skeleton_label(fakes, tmp_path):
    doc = _skeleton()
    del doc[list(doc)[3]]
    expected = [{"rule_id": f.rule_id, "clause": f.clause, "message": "m"} for f in gate(doc)]
    session = fakes.FakeSession(
        [fakes.chat_reply(json.dumps({"findings": expected}), {"total_tokens": 1})]
    )
    rows = [
        {"id": "R1", "source": "pmc", "subset": "commercial", "document": doc},
        {"id": "R2", "source": "pbrer_skeleton", "subset": "commercial", "document": doc},
    ]
    out = generate_batch(_teacher(session), rows, "rule_check", 2, tmp_path, excluded=())
    assert out["kept"] == 1
    assert out["rejected"]["licence"] == 1
    assert _jsonl(tmp_path / "examples.jsonl")[0]["source_row"]["id"] == "R2"


def test_abstain_batch(fakes, tmp_path):
    session = fakes.FakeSession(
        [fakes.chat_reply(json.dumps({"question": "Exposure?"}), {"total_tokens": 1})]
    )
    rows = [_passage(3, reason="MAH data required")]
    assert generate_batch(_teacher(session), rows, "abstain", 1, tmp_path, excluded=())["kept"] == 1


# --- input rules ---------------------------------------------------------------------


def test_input_missing_fields_is_rejected_without_a_request(fakes, tmp_path):
    session = fakes.FakeSession([])
    out = generate_batch(
        _teacher(session), [_passage(1, text="")], "grounded_qa", 1, tmp_path, excluded=()
    )
    assert out["rejected"]["input"] == 1
    assert session.posts == []


def test_every_kind_requires_an_id(fakes, tmp_path):
    session = fakes.FakeSession([])
    no_id = {k: v for k, v in _passage(1, reason="unverified").items() if k != "id"}
    tool = {k: v for k, v in _tool_row(1, _result()).items() if k != "id"}
    doc = {"source": "pbrer_skeleton", "subset": "commercial", "document": _skeleton()}
    t = _teacher(session)
    for kind, row in (("abstain", no_id), ("tool_draft", tool), ("rule_check", doc)):
        out = generate_batch(t, [row], kind, 1, tmp_path / kind, excluded=())
        assert out["rejected"]["input"] == 1, kind
    assert session.posts == []


def test_unknown_kind_is_refused(fakes, tmp_path):
    with pytest.raises(ValueError):
        generate_batch(_teacher(fakes.FakeSession([])), [], "essay", 1, tmp_path, excluded=())


def test_max_tokens_defaults_to_the_setting(fakes, tmp_path, monkeypatch):
    from episteme import config as cfg

    cfg.get_settings.cache_clear()
    monkeypatch.setenv("EPISTEME_TEACHER_MAX_TOKENS", "77")
    try:
        session = fakes.FakeSession([fakes.chat_reply(_qa_json("PMC1"), {"total_tokens": 1})])
        generate_batch(_teacher(session), [_passage(1)], "grounded_qa", 1, tmp_path, excluded=())
        assert session.posts[0]["json"]["max_tokens"] == 77
        assert _manifest(tmp_path)["config"]["max_tokens"] == 77
    finally:
        cfg.get_settings.cache_clear()
