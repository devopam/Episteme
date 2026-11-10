import json
from datetime import date

import pytest
import requests

from episteme.harness.pbrer import PbrerFacts, gate, skeleton
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance
from episteme.sft import batch as batch_mod
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
    # 5 tokens of the answered request plus the estimate of the failed one.
    ledger = _jsonl(tmp_path / "requests.jsonl")
    assert doc["budget_used"] == sum(e.get("tokens", 0) for e in ledger) > 5
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
        # The row label is ignored: the claims decide (fda_enforcement is commercial).
        _tool_row(1, _result("fda_enforcement"), source="dailymed", subset="commercial"),
        _tool_row(2, _result("openfda_faers")),
        _tool_row(3, _result("pubmed")),
        _tool_row(4, _result("no_such_source")),
        _tool_row(5, _result("fda_enforcement"), _result("openfda_faers")),
        # DailyMed and EPAR facts wait for the owner (ruling R19).
        _tool_row(6, _result("dailymed")),
        _tool_row(7, _result("ema_epar")),
    ]
    out = generate_batch(
        _teacher(session), rows, "tool_draft", 7, tmp_path, excluded=("dailymed", "ema_epar")
    )
    assert out["kept"] == 1
    assert out["rejected"]["licence_pending"] == 4
    assert out["rejected"]["licence"] == 2
    assert len(session.posts) == 1
    row = _jsonl(tmp_path / "examples.jsonl")[0]
    assert row["source_row"]["id"] == "T1"
    assert row["source_row"]["lookup_sources"] == ["fda_enforcement"]
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
        _tool_row(
            1, _result(), pbrer_region="ich", skeleton=draft, task=f"Draft the PBRER {INTERVAL}."
        ),
        _tool_row(
            2, _result(), pbrer_region="ich", skeleton=draft, task=f"Draft the PBRER {INTERVAL}."
        ),
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
    label = {"source": "pbrer_skeleton", "subset": "commercial", "document": doc}

    def results(*sources):
        return [_result(src).to_json() for src in sources]

    rows = [
        {
            "id": "R1",
            "source": "pmc",
            "subset": "commercial",
            "document": doc,
            "tool_results": results("fda_enforcement"),
        },
        {"id": "R2", **label, "tool_results": results("fda_enforcement", "ema_psusa")},
        # No tool results: the skeleton's sources cannot be verified.
        {"id": "R3", **label},
        {"id": "R4", **label, "tool_results": []},
        {"id": "R5", **label, "tool_results": results("fda_enforcement", "pubmed")},
        {"id": "R6", **label, "tool_results": results("openfda_faers")},
        {"id": "R7", **label, "tool_results": results("dailymed")},
    ]
    out = generate_batch(_teacher(session), rows, "rule_check", 7, tmp_path, excluded=())
    assert out["kept"] == 1
    assert out["rejected"]["licence"] == 4
    assert out["rejected"]["licence_pending"] == 2
    assert len(session.posts) == 1
    kept = _jsonl(tmp_path / "examples.jsonl")[0]["source_row"]
    assert kept["id"] == "R2"
    assert kept["lookup_sources"] == ["ema_psusa", "fda_enforcement"]


def test_rule_check_malformed_tool_results_are_format(fakes, tmp_path):
    row = {
        "id": "R1",
        "source": "pbrer_skeleton",
        "subset": "commercial",
        "document": _skeleton(),
        "tool_results": [{"claims": ["x"]}],
    }
    session = fakes.FakeSession([])
    out = generate_batch(_teacher(session), [row], "rule_check", 1, tmp_path, excluded=())
    assert out["rejected"]["format"] == 1
    assert session.posts == []


def test_dailymed_and_epar_lookup_facts_are_pending():
    from episteme.config import LOOKUP_SOURCE_SUBSETS

    assert LOOKUP_SOURCE_SUBSETS["dailymed"] == "pending"
    assert LOOKUP_SOURCE_SUBSETS["ema_epar"] == "pending"


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


# --- fix round 2: ledger, guarded manifest, shapes, kind ------------------------------


def _ledger(path) -> list[dict]:
    return _jsonl(path / "requests.jsonl")


def test_manifest_write_failure_keeps_the_original_exception(fakes, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(batch_mod, "write_manifest", broken)
    session = fakes.FakeSession([fakes.FakeResponse({}, status=503)])
    with pytest.raises(requests.HTTPError):
        generate_batch(_teacher(session), [_passage(1)], "grounded_qa", 1, tmp_path, excluded=())
    # With no exception in flight, a manifest failure is the error the caller sees.
    session = fakes.FakeSession([fakes.chat_reply(_qa_json("PMC2"), {"total_tokens": 1})])
    with pytest.raises(OSError):
        generate_batch(_teacher(session), [_passage(2)], "grounded_qa", 1, tmp_path, excluded=())


def test_ledger_records_every_paid_request(fakes, tmp_path):
    session = fakes.FakeSession(
        [
            fakes.chat_reply(_qa_json("PMC1"), {"total_tokens": 5}),
            fakes.chat_reply(_qa_json("PMC2", '"999 patients" [PMC2]'), {"total_tokens": 6}),
            fakes.chat_reply("not json", {"total_tokens": 7}),
            fakes.FakeResponse({}, status=503),
        ]
    )
    t = _teacher(session)
    rows = [_passage(i) for i in range(1, 5)]
    with pytest.raises(requests.HTTPError):
        generate_batch(t, rows, "grounded_qa", 9, tmp_path, excluded=())
    ledger = _ledger(tmp_path)
    paid = [e for e in ledger if e["event"] in ("paid", "error")]
    assert [(e["id"], e["event"]) for e in paid] == [
        ("PMC1", "paid"),
        ("PMC2", "paid"),
        ("PMC3", "paid"),
        ("PMC4", "error"),
    ]
    assert [e["tokens"] for e in paid[:3]] == [5, 6, 7]
    assert paid[3]["tokens"] > 0 and paid[3]["error"] == "HTTPError"
    outcomes = {e["id"]: (e["outcome"], e.get("reason")) for e in ledger if e["event"] == "outcome"}
    assert outcomes == {
        "PMC1": ("kept", None),
        "PMC2": ("rejected", "citation"),
        "PMC3": ("rejected", "format"),
    }
    assert all(e["kind"] == "grounded_qa" for e in ledger)
    assert sum(e["tokens"] for e in paid) == t.used_tokens


def test_resume_skips_rejected_ids_and_retries_errors(fakes, tmp_path):
    first = fakes.FakeSession(
        [
            fakes.chat_reply(_qa_json("PMC1"), {"total_tokens": 5}),
            fakes.chat_reply("not json", {"total_tokens": 6}),
            fakes.FakeResponse({}, status=503),
        ]
    )
    rows = [_passage(i) for i in range(1, 5)]
    with pytest.raises(requests.HTTPError):
        generate_batch(_teacher(first), rows, "grounded_qa", 3, tmp_path, excluded=())
    second = fakes.FakeSession(
        [
            fakes.chat_reply(_qa_json("PMC3"), {"total_tokens": 1}),
            fakes.chat_reply(_qa_json("PMC4"), {"total_tokens": 1}),
        ]
    )
    out = generate_batch(_teacher(second), rows, "grounded_qa", 3, tmp_path, excluded=())
    # PMC1 (kept) and PMC2 (rejected) were paid for: no new request. PMC3 failed: retried.
    asked = [json.loads(p["json"]["messages"][1]["content"])["passage_ref"] for p in second.posts]
    assert asked == ["PMC3", "PMC4"]
    assert out["skipped_done"] == 2
    assert out["kept"] == 2 and out["kept_before"] == 1


def test_hard_kill_loses_at_most_the_in_flight_request(fakes, tmp_path, monkeypatch):
    def killed(example):
        raise KeyboardInterrupt

    session = fakes.FakeSession([fakes.chat_reply(_qa_json("PMC1"), {"total_tokens": 9})])
    monkeypatch.setattr(batch_mod, "run_checks", killed)
    with pytest.raises(KeyboardInterrupt):
        generate_batch(_teacher(session), [_passage(1)], "grounded_qa", 1, tmp_path, excluded=())
    assert [(e["event"], e["id"], e["tokens"]) for e in _ledger(tmp_path)] == [("paid", "PMC1", 9)]
    monkeypatch.undo()
    again = fakes.FakeSession([])
    out = generate_batch(_teacher(again), [_passage(1)], "grounded_qa", 1, tmp_path, excluded=())
    assert again.posts == []
    assert out["skipped_done"] == 1


def test_budget_across_runs_reads_the_ledger(fakes, tmp_path):
    first = fakes.FakeSession([fakes.chat_reply(_qa_json("PMC1"), {"total_tokens": 900})])
    rows = [_passage(i) for i in range(1, 3)]
    generate_batch(
        _teacher(first, budget=1000),
        rows[:1],
        "grounded_qa",
        2,
        tmp_path,
        excluded=(),
        max_tokens=100,
    )
    assert len(first.posts) == 1
    second = fakes.FakeSession([fakes.chat_reply(_qa_json("PMC2"), {"total_tokens": 1})])
    out = generate_batch(
        _teacher(second, budget=1000), rows, "grounded_qa", 2, tmp_path, excluded=(), max_tokens=100
    )
    assert second.posts == []
    assert out["stopped"] == "budget"
    assert _manifest(tmp_path)["counts"]["tokens_before"] == 900


def test_malformed_claims_are_format_and_empty_claims_are_licence(fakes, tmp_path):
    good = _result().to_json()
    bad_claim = {"claims": ["not a claim"], "coverage": good["coverage"]}
    bad_prov = {"claims": [{**good["claims"][0], "provenance": "x"}], "coverage": good["coverage"]}
    empty = {"claims": [], "coverage": {"matched": 0, "missing": ["faers"], "notes": []}}

    def row(i, result):
        return {**_tool_row(i, _result()), "tool_results": [result]}

    session = fakes.FakeSession([])
    out = generate_batch(
        _teacher(session),
        [row(1, bad_claim), row(2, bad_prov), row(3, empty)],
        "tool_draft",
        3,
        tmp_path,
        excluded=(),
    )
    assert out["rejected"]["format"] == 2
    assert out["rejected"]["licence"] == 1
    assert session.posts == []


def test_resume_refuses_another_kind(fakes, tmp_path):
    session = fakes.FakeSession([fakes.chat_reply(_qa_json("PMC1"), {"total_tokens": 1})])
    generate_batch(_teacher(session), [_passage(1)], "grounded_qa", 1, tmp_path, excluded=())
    with pytest.raises(ValueError, match="grounded_qa"):
        generate_batch(
            _teacher(fakes.FakeSession([])),
            [_passage(2, reason="unverified")],
            "abstain",
            1,
            tmp_path,
            excluded=(),
        )
