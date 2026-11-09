import json

from episteme.harness import loop
from episteme.harness.pbrer import SECTION_SOURCES
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance


def _result() -> LookupResult:
    prov = Provenance("src", "r1", "f", "KEY1", "US")
    claim = Claim("a fact", "42", "fetched", prov, 1.0, "direct")
    return LookupResult((claim,), Coverage(1))


def _tool_msg(calls):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"c{i}",
                            "type": "function",
                            "function": {"name": n, "arguments": json.dumps(a)},
                        }
                        for i, (n, a) in enumerate(calls)
                    ],
                }
            }
        ]
    }


def _final(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def test_tools_cover_resolve_and_section_lookups():
    names = {t["function"]["name"] for t in loop.TOOLS}
    wanted = {n for v in SECTION_SOURCES.values() for n in v} | {"resolve_entity"}
    assert names == wanted
    for t in loop.TOOLS:
        props = t["function"]["parameters"]["properties"]
        assert "tenant" not in props and "caller" not in props


def test_loop_runs_tool_calls_and_returns_answer():
    replies = iter([_tool_msg([("trials", {"substance_key": 1})]), _final("done")])
    seen = []

    def call_tool(name, args):
        seen.append((name, args))
        return _result()

    answer, results = loop.run(
        [{"role": "user", "content": "q"}],
        call_tool=call_tool,
        post=lambda payload: next(replies),
        max_tool_calls=5,
    )
    assert answer == "done"
    assert len(results) == 1 and seen[0][0] == "trials"


def test_loop_stops_at_max_tool_calls():
    n = {"calls": 0}

    def post(payload):
        n["calls"] += 1
        if n["calls"] > 20:
            raise AssertionError("loop did not stop")
        if not payload.get("tools"):
            return _final("budget answer")
        return _tool_msg([("trials", {"substance_key": 1})])

    executed = []
    answer, results = loop.run(
        [{"role": "user", "content": "q"}],
        call_tool=lambda n_, a: executed.append(n_) or _result(),
        post=post,
        max_tool_calls=3,
    )
    assert len(executed) == 3
    assert answer == "budget answer"


def test_tenant_and_caller_are_injected_not_model_supplied():
    replies = iter(
        [
            _tool_msg([("trials", {"substance_key": 1, "tenant": "evil", "caller": "evil"})]),
            _final("ok"),
        ]
    )
    seen = []
    loop.run(
        [{"role": "user", "content": "q"}],
        call_tool=lambda n, a: seen.append(a) or _result(),
        post=lambda p: next(replies),
        max_tool_calls=5,
        tenant="t1",
        caller="c1",
    )
    assert seen[0]["tenant"] == "t1" and seen[0]["caller"] == "c1"
    assert seen[0]["substance_key"] == 1


def test_unknown_tool_returns_error_and_counts():
    replies = iter([_tool_msg([("drop_tables", {})]), _final("ok")])
    payloads = []

    def post(p):
        payloads.append(json.loads(json.dumps(p)))
        return next(replies)

    called = []
    answer, results = loop.run(
        [{"role": "user", "content": "q"}],
        call_tool=lambda n, a: called.append(n) or _result(),
        post=post,
        max_tool_calls=1,
    )
    assert answer == "ok" and results == [] and called == []
    assert "tools" not in payloads[1]  # the unknown call used the whole budget
    tool_msgs = [m for m in payloads[1]["messages"] if m.get("role") == "tool"]
    assert "unknown tool" in tool_msgs[0]["content"]
