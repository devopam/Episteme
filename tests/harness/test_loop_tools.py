import datetime
import inspect
import json

import psycopg
import pytest

from episteme.harness import loop
from episteme.reference import literature, lookups, resolve
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance


def _result() -> LookupResult:
    prov = Provenance("src", "r1", "f", "KEY1", "US")
    return LookupResult((Claim("a fact", "42", "fetched", prov, 1.0, "d"),), Coverage(1))


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


def _run_two_steps(first, call_tool, budget=5):
    replies = iter([first, _final("ok")])
    payloads = []

    def post(p):
        payloads.append(json.loads(json.dumps(p)))
        return next(replies)

    out = loop.run(
        [{"role": "user", "content": "q"}],
        call_tool=call_tool,
        post=post,
        max_tool_calls=budget,
        tenant="t",
        caller="c",
    )
    tool_msgs = [m["content"] for m in payloads[1]["messages"] if m.get("role") == "tool"]
    return out, tool_msgs


def test_tenant_and_caller_are_required():
    with pytest.raises(TypeError):
        loop.run([], call_tool=lambda n, a: _result(), post=lambda p: _final("x"), max_tool_calls=1)


def test_bad_json_arguments_return_generic_error():
    first = _tool_msg([("trials", {})])
    first["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
    called = []
    _, msgs = _run_two_steps(first, lambda n, a: called.append(n) or _result())
    assert called == [] and "trials" in msgs[0] and "not valid JSON" in msgs[0]


def test_psycopg_error_text_does_not_reach_the_model():
    def boom(name, args):
        raise psycopg.OperationalError("host=db.internal user=secret")

    (answer, results), msgs = _run_two_steps(_tool_msg([("trials", {"substance_key": 1})]), boom)
    assert answer == "ok" and results == []
    assert "trials" in msgs[0] and "secret" not in msgs[0] and "db.internal" not in msgs[0]


def test_argument_error_gets_generic_message_without_exception_text():
    def boom(name, args):
        raise loop.ToolArgumentError("secret detail")

    _, msgs = _run_two_steps(_tool_msg([("trials", {"substance_key": 1})]), boom)
    assert "secret detail" not in msgs[0] and "trials" in msgs[0]


def test_other_exceptions_propagate():
    def boom(name, args):
        raise RuntimeError("wiring bug")

    with pytest.raises(RuntimeError):
        _run_two_steps(_tool_msg([("trials", {"substance_key": 1})]), boom)


def test_budget_overrun_inside_one_message():
    first = _tool_msg([("trials", {"substance_key": 1})] * 3)
    executed = []
    _, msgs = _run_two_steps(first, lambda n, a: executed.append(n) or _result(), budget=2)
    assert len(executed) == 2
    assert len(msgs) == 3 and "budget" in msgs[2]


def test_make_call_tool_converts_dates_and_injects(monkeypatch):
    seen = {}

    def fake(conn, key, start, end, *, tenant, caller):
        seen.update(conn=conn, key=key, start=start, end=end, tenant=tenant, caller=caller)
        return _result()

    monkeypatch.setattr(lookups, "faers_counts", fake)
    call = loop.make_call_tool("CONN", tenant="t1", caller="c1")
    call(
        "faers_counts",
        {"substance_key": "5", "start": "2020-01-01", "end": "2020-03-31", "tenant": "x"},
    )
    assert seen == {
        "conn": "CONN",
        "key": 5,
        "start": datetime.date(2020, 1, 1),
        "end": datetime.date(2020, 3, 31),
        "tenant": "t1",
        "caller": "c1",
    }


def test_make_call_tool_rejects_bad_arguments():
    call = loop.make_call_tool(None, tenant="t", caller="c")
    with pytest.raises(loop.ToolArgumentError):
        call("faers_counts", {"substance_key": 1})
    with pytest.raises(loop.ToolArgumentError):
        call("faers_counts", {"substance_key": 1, "start": "bad", "end": "2020-01-01"})
    with pytest.raises(loop.ToolArgumentError):
        call("trials", {"substance_key": 1, "extra": 1})
    with pytest.raises(loop.ToolArgumentError):
        call("literature", {"substance_key": 1, "topic": 5})


def test_lookup_body_errors_propagate_out_of_run(monkeypatch):
    def broken(conn, key, *, tenant, caller):
        raise TypeError("bug in the lookup")

    monkeypatch.setattr(lookups, "trials", broken)
    call = loop.make_call_tool(None, tenant="t", caller="c")
    with pytest.raises(TypeError):
        _run_two_steps(_tool_msg([("trials", {"substance_key": 1})]), call)


def test_psycopg_error_logs_class_at_warning_and_traceback_at_debug(caplog):
    def boom(name, args):
        raise psycopg.OperationalError("host=db.internal")

    with caplog.at_level("DEBUG", logger="episteme.harness.loop"):
        _run_two_steps(_tool_msg([("trials", {"substance_key": 1})]), boom)
    warn = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warn and "OperationalError" in warn[0].getMessage()
    assert "db.internal" not in warn[0].getMessage()
    assert any(r.levelname == "DEBUG" and r.exc_info for r in caplog.records)


def test_literature_topic_is_required_and_nullable(monkeypatch):
    fn = next(t for t in loop.TOOLS if t["function"]["name"] == "literature")["function"]
    assert "topic" in fn["parameters"]["required"]
    seen = {}

    def fake(conn, key, topic, *, tenant, caller):
        seen["topic"] = topic
        return _result()

    monkeypatch.setattr(literature, "literature", fake)
    call = loop.make_call_tool(None, tenant="t", caller="c")
    call("literature", {"substance_key": 1, "topic": None})
    assert seen["topic"] is None


def test_tool_schemas_match_real_signatures():
    mods = {"resolve_entity": resolve, "literature": literature}
    for t in loop.TOOLS:
        name = t["function"]["name"]
        fn = getattr(mods.get(name, lookups), name)
        sig = [
            p
            for p in inspect.signature(fn).parameters.values()
            if p.name not in ("conn", "tenant", "caller", "limit")
        ]
        schema = t["function"]["parameters"]
        assert [p.name for p in sig] == list(schema["properties"]), name
        required = {p.name for p in sig if p.default is inspect.Parameter.empty}
        assert required == set(schema["required"]), name


# --- final review I4: a database error on a transaction connection rolls back -------


class _Conn:
    def __init__(self, autocommit):
        self.autocommit = autocommit
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


def _failing(*a, **k):
    raise psycopg.errors.QueryCanceled("x")


@pytest.mark.parametrize(("autocommit", "rollbacks"), [(False, 1), (True, 0)])
def test_make_call_tool_rolls_back_after_database_error(monkeypatch, autocommit, rollbacks):
    monkeypatch.setattr(lookups, "trials", _failing)
    conn = _Conn(autocommit)
    call = loop.make_call_tool(conn, tenant="t", caller="c")
    with pytest.raises(psycopg.Error):
        call("trials", {"substance_key": 1})
    assert conn.rollbacks == rollbacks


def test_make_call_tool_docstring_states_autocommit():
    assert "autocommit" in (loop.make_call_tool.__doc__ or "")


# --- final review M2: max_tool_calls default from settings ---------------------------


def test_run_default_max_tool_calls_from_settings(monkeypatch):
    import dataclasses

    from episteme import config

    s = dataclasses.replace(config.get_settings(), harness_max_tool_calls=1)
    monkeypatch.setattr(loop, "get_settings", lambda: s)
    replies = iter([_tool_msg([("trials", {"substance_key": 1})] * 3), _final("ok")])
    executed = []
    loop.run(
        [{"role": "user", "content": "q"}],
        call_tool=lambda n, a: executed.append(n) or _result(),
        post=lambda p: next(replies),
        tenant="t",
        caller="c",
    )
    assert executed == ["trials"]
