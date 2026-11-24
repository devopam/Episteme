"""question_mcp: translate a question to MeSH terms, the person confirms by MCP
elicitation, then search, and log (non-pg with a fake DB and a fake encoder; one pg smoke
test through a throwaway member of episteme_api_reader, episteme_test only). The
descriptor UIs D9xxxxx are fixture values."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import mcp_types
import numpy as np
import psycopg
import pytest
from mcp import Client
from mcp.server.elicitation import render_elicitation_schema
from mcp.server.mcpserver import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from psycopg import sql
from psycopg.conninfo import make_conninfo

from episteme import config
from episteme.reference import question_mcp as qm

_UNMATCHED = "unmatched: zzword"

_EXACT = [
    {
        "kind": "term",
        "descriptor_ui": "D9000001",
        "descriptor_name": "Stevens-Johnson Syndrome",
        "matched_text": "sjs",
        "matched_term": "SJS",
        "term_kind": "entry",
        "concept_relation": None,
        "basis": "exact",
        "score": None,
        "mesh_release": "2026",
        "note": None,
    },
    {
        "kind": "term",
        "descriptor_ui": "D9000002",
        "descriptor_name": "Amoxicillin",
        "matched_text": "amoxicillin",
        "matched_term": "Amoxicillin",
        "term_kind": "name",
        "concept_relation": None,
        "basis": "exact",
        "score": None,
        "mesh_release": "2026",
        "note": None,
    },
]

_SUGGESTED = [
    {
        "kind": "term",
        "descriptor_ui": "D9000007",
        "descriptor_name": "Anaphylaxis",
        "matched_text": None,
        "matched_term": "Anaphylactic Shock",
        "term_kind": "entry",
        "concept_relation": "NRW",
        "basis": "medcpt",
        "score": 0.875,
        "mesh_release": "2026",
        "note": None,
    },
    # The same descriptor as an exact term: not proposed twice.
    {**_EXACT[0], "basis": "medcpt", "score": 0.5, "matched_text": None},
]


def _note(text: str) -> dict:
    return {k: None for k in qm.TERM_FIELDS} | {"kind": "note", "note": text}


def _article(article_id: str, release: str) -> dict:
    return {
        "kind": "article",
        "article_id": article_id,
        "source": "pmc",
        "title": "t",
        "year": 2024,
        "score": 1.5,
        "relation": None,
        "release_id": release,
        "licence": "cc-by",
        "note": None,
        "source_file": "pmc/x.xml",
    }


def _article_note(text: str) -> dict:
    return {k: None for k in qm.ARTICLE_FIELDS} | {"kind": "note", "note": text}


class FakeDB:
    def __init__(self, match_rows, suggest_rows=()):
        self.match_rows = list(match_rows)
        self.suggest_rows = list(suggest_rows)
        self.calls: list[tuple] = []

    def match_terms(self, question):
        self.calls.append(("match_terms", question))
        return [dict(r) for r in self.match_rows]

    def suggest_terms(self, vector, model_revision):
        self.calls.append(("suggest_terms", vector, model_revision))
        return [dict(r) for r in self.suggest_rows]

    def search_terms(self, descriptor_uis, sources):
        self.calls.append(("search_terms", list(descriptor_uis), sources))
        return [
            _article("PMC1", "pmc-2026-01"),
            _article_note("terms used: D9000001 Stevens-Johnson Syndrome; query: 'sjs'"),
        ]

    def literature_by_mesh(self, substance_key, topic, from_year, to_year):
        self.calls.append(("literature_by_mesh", substance_key, topic, from_year, to_year))
        return [_article("PMC2", "lit-2026-02")]

    def names(self):
        return [c[0] for c in self.calls]


class FakeEncoder:
    made = 0

    def __init__(self):
        FakeEncoder.made += 1

    def encode_queries(self, texts):
        assert len(texts) == 1
        return np.full((1, 768), 0.5, dtype=np.float32)


_NOW = datetime(2026, 11, 24, 23, 30, tzinfo=timezone.utc)


def _service(tmp_path, db, made=None):
    def factory():
        if made is not None:
            made.append(1)
        return FakeEncoder()

    return qm.QuestionService(db, tmp_path / "log", encoder_factory=factory, now=lambda: _NOW)


class FakeCtx:
    """``elicit`` builds its answer from the schema it receives."""

    def __init__(self, action="accept", ticks=(), error=None):
        self.action, self.ticks, self.error = action, ticks, error
        self.calls: list[tuple] = []

    async def elicit(self, message, schema):
        self.calls.append((message, schema))
        if self.error is not None:
            raise self.error
        if self.action == "accept":
            return AcceptedElicitation(data=schema(**{f"accept_{n}": True for n in self.ticks}))
        if self.action == "decline":
            return DeclinedElicitation()
        return CancelledElicitation()


def _log_lines(tmp_path):
    path = tmp_path / "log" / "question-2026-11-24.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


_LOG_KEYS = {
    "event",
    "ts",
    "question",
    "confirmation_id",
    "terms",
    "elicitation_action",
    "confirmed_by",
    "accepted",
    "rejected",
    "queries",
    "release_ids",
}


def _translate_three(tmp_path):
    db = FakeDB(_EXACT + [_note(_UNMATCHED)], _SUGGESTED)
    svc = _service(tmp_path, db)
    out = svc.translate_question("SJS after amoxicillin zzword")
    return svc, db, out


# ---------------------------------------------------------------------------
# translate_question


def test_exact_only_question_does_not_load_the_encoder(tmp_path):
    db = FakeDB(_EXACT)
    made: list[int] = []
    out = _service(tmp_path, db, made).translate_question("SJS after amoxicillin")
    assert made == []
    assert db.names() == ["match_terms"]
    assert [t["n"] for t in out["terms"]] == [1, 2]
    assert set(out["terms"][0]) == set(qm.TERM_FIELDS) | {"n"}
    assert out["mesh_release"] == "2026"
    assert out["notes"] == []
    assert set(out) == {"confirmation_id", "mesh_release", "terms", "notes"}


def test_unmatched_note_triggers_one_suggestion_with_the_doc_revision(tmp_path):
    db = FakeDB(_EXACT + [_note(_UNMATCHED)], _SUGGESTED + [_note("capped: x")])
    made: list[int] = []
    svc = _service(tmp_path, db, made)
    out = svc.translate_question("SJS after amoxicillin zzword")
    assert db.names() == ["match_terms", "suggest_terms"]
    _, vector, revision = db.calls[1]
    # The DOCUMENT-side revision of the stored vectors, not the query encoder's own.
    assert revision == config.MEDCPT_ARTICLE_REVISION != config.MEDCPT_QUERY_REVISION
    assert vector.startswith("[0.5,") and vector.endswith("]")
    assert len(vector[1:-1].split(",")) == 768
    # Exact terms first, then new suggestions; a descriptor is proposed once.
    assert [(t["n"], t["descriptor_ui"], t["basis"]) for t in out["terms"]] == [
        (1, "D9000001", "exact"),
        (2, "D9000002", "exact"),
        (3, "D9000007", "medcpt"),
    ]
    assert out["notes"] == [_UNMATCHED, "capped: x"]
    svc.translate_question("another zzword")
    assert made == [1]  # loaded once per process


def test_confirmation_id_is_stable_for_equal_input(tmp_path):
    a = _translate_three(tmp_path)[2]["confirmation_id"]
    b = _translate_three(tmp_path)[2]["confirmation_id"]
    assert a == b and len(a) == 64
    expected = qm.confirmation_id(
        "SJS after amoxicillin zzword",
        "2026",
        [
            {"descriptor_ui": "D9000001", "basis": "exact", "score": None},
            {"descriptor_ui": "D9000002", "basis": "exact", "score": None},
            {"descriptor_ui": "D9000007", "basis": "medcpt", "score": 0.875},
        ],
    )
    assert a == expected
    other = _service(tmp_path, FakeDB(_EXACT)).translate_question("SJS after amoxicillin")
    assert other["confirmation_id"] != a


# ---------------------------------------------------------------------------
# search_confirmed


def _confirm(svc, cid, ctx, **kw):
    return asyncio.run(svc.search_confirmed(cid, ctx, **kw))


def test_unknown_confirmation_runs_no_search(tmp_path):
    svc, db, _ = _translate_three(tmp_path)
    ctx = FakeCtx(ticks=(1,))
    out = _confirm(svc, "0" * 64, ctx)
    assert out["status"] == "unknown_confirmation"
    assert out["rows"] == [] and ctx.calls == []
    assert "search_terms" not in db.names()


@pytest.mark.parametrize(
    ("ctx", "status", "action"),
    [
        (FakeCtx(action="decline"), "declined", "decline"),
        (FakeCtx(action="cancel"), "cancelled", "cancel"),
        (FakeCtx(error=RuntimeError("Method not found")), "no_elicitation", None),
        (FakeCtx(action="accept", ticks=()), "nothing_accepted", "accept"),
    ],
)
def test_no_search_without_an_accepted_term(tmp_path, ctx, status, action):
    svc, db, tr = _translate_three(tmp_path)
    out = _confirm(svc, tr["confirmation_id"], ctx, substance_key=7)
    assert out["status"] == status
    assert db.names() == ["match_terms", "suggest_terms"]
    assert out["accepted"] == [] and out["rows"] == []
    assert [t["n"] for t in out["rejected"]] == [1, 2, 3]
    line = _log_lines(tmp_path)[-1]
    assert line["event"] == "confirm" and line["elicitation_action"] == action
    assert line["confirmed_by"] == ("user via MCP elicitation" if action == "accept" else None)


def test_accept_terms_1_and_3_searches_only_those(tmp_path):
    svc, db, tr = _translate_three(tmp_path)
    ctx = FakeCtx(ticks=(1, 3))
    out = _confirm(
        svc,
        tr["confirmation_id"],
        ctx,
        sources=["pmc"],
        substance_key=7,
        from_year=2020,
        to_year=2025,
    )
    assert out["status"] == "searched"
    assert db.calls[2:] == [
        ("search_terms", ["D9000001", "D9000007"], ["pmc"]),
        ("literature_by_mesh", 7, "Stevens-Johnson Syndrome", 2020, 2025),
        ("literature_by_mesh", 7, "Anaphylaxis", 2020, 2025),
    ]
    assert [t["descriptor_ui"] for t in out["accepted"]] == ["D9000001", "D9000007"]
    assert [t["descriptor_ui"] for t in out["rejected"]] == ["D9000002"]
    assert [r["article_id"] for r in out["rows"] if r["kind"] == "article"] == [
        "PMC1",
        "PMC2",
        "PMC2",
    ]
    prov = out["provenance"]
    assert prov["question"] == "SJS after amoxicillin zzword"
    assert prov["queries"] == ["terms used: D9000001 Stevens-Johnson Syndrome; query: 'sjs'"]
    assert prov["release_ids"] == ["lit-2026-02", "pmc-2026-01"]
    assert prov["confirmed_by"] == "user via MCP elicitation"
    assert prov["model_revision"] == config.MEDCPT_ARTICLE_REVISION
    assert [t["n"] for t in prov["proposed"]] == [1, 2, 3]
    # The person saw every proposed term, titled with its basis.
    message, schema = ctx.calls[0]
    assert "SJS after amoxicillin zzword" in message
    titles = [f.title for f in schema.model_fields.values()]
    assert titles == [
        "Stevens-Johnson Syndrome (D9000001) — exact",
        "Amoxicillin (D9000002) — exact",
        "Anaphylaxis (D9000007) — medcpt, score 0.875, NRW",
    ]


class FailingDB(FakeDB):
    def search_terms(self, descriptor_uis, sources):
        self.calls.append(("search_terms", list(descriptor_uis), sources))
        raise RuntimeError("SELECT secret FROM somewhere -- params")


def test_failed_search_still_logs_the_confirmation(tmp_path):
    db = FailingDB(_EXACT + [_note(_UNMATCHED)], _SUGGESTED)
    svc = _service(tmp_path, db)
    tr = svc.translate_question("SJS after amoxicillin zzword")
    with pytest.raises(RuntimeError):
        _confirm(svc, tr["confirmation_id"], FakeCtx(ticks=(1, 3)))
    lines = _log_lines(tmp_path)
    assert [line["event"] for line in lines] == ["translate", "confirm"]
    confirm = lines[-1]
    assert set(confirm) == _LOG_KEYS | {"error"}
    assert confirm["error"] == "RuntimeError"
    assert "secret" not in json.dumps(confirm)
    assert confirm["confirmed_by"] == "user via MCP elicitation"
    assert [t["n"] for t in confirm["accepted"]] == [1, 3]
    assert confirm["queries"] == [] and confirm["release_ids"] == []


def test_provenance_carries_the_search_parameters(tmp_path):
    svc, _, tr = _translate_three(tmp_path)
    out = _confirm(
        svc,
        tr["confirmation_id"],
        FakeCtx(ticks=(1,)),
        sources=["pmc"],
        substance_key=7,
        from_year=2020,
        to_year=2025,
    )
    prov = out["provenance"]
    assert (prov["sources"], prov["substance_key"], prov["from_year"], prov["to_year"]) == (
        ["pmc"],
        7,
        2020,
        2025,
    )
    assert "error" not in _log_lines(tmp_path)[-1]


def test_no_literature_call_without_substance_key(tmp_path):
    svc, db, tr = _translate_three(tmp_path)
    out = _confirm(svc, tr["confirmation_id"], FakeCtx(ticks=(2,)))
    assert out["status"] == "searched"
    assert db.calls[2:] == [("search_terms", ["D9000002"], None)]


def test_log_lines_carry_the_exact_keys(tmp_path):
    svc, _, tr = _translate_three(tmp_path)
    _confirm(svc, tr["confirmation_id"], FakeCtx(ticks=(1,)))
    translate, confirm = _log_lines(tmp_path)
    assert set(translate) == _LOG_KEYS and set(confirm) == _LOG_KEYS
    assert translate["event"] == "translate" and translate["elicitation_action"] is None
    assert translate["ts"] == "2026-11-24T23:30:00+00:00"
    assert [t["descriptor_ui"] for t in translate["terms"]] == ["D9000001", "D9000002", "D9000007"]
    assert confirm["confirmation_id"] == tr["confirmation_id"]
    assert confirm["elicitation_action"] == "accept"
    assert confirm["confirmed_by"] == "user via MCP elicitation"
    assert [t["n"] for t in confirm["accepted"]] == [1]
    assert [t["n"] for t in confirm["rejected"]] == [2, 3]
    assert confirm["queries"] == ["terms used: D9000001 Stevens-Johnson Syndrome; query: 'sjs'"]
    assert confirm["release_ids"] == ["pmc-2026-01"]


def test_log_file_name_uses_the_utc_date(tmp_path):
    # 03:00 on the 25th in UTC+05:30 is 21:30 on the 24th in UTC.
    local = datetime(2026, 11, 25, 3, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    svc = qm.QuestionService(FakeDB(_EXACT), tmp_path, now=lambda: local)
    svc.translate_question("SJS")
    assert [p.name for p in tmp_path.iterdir()] == ["question-2026-11-24.jsonl"]
    line = json.loads((tmp_path / "question-2026-11-24.jsonl").read_text(encoding="utf-8"))
    assert line["ts"] == "2026-11-24T21:30:00+00:00"


def test_confirm_schema_is_spec_valid_flat_bools(tmp_path):
    _, _, tr = _translate_three(tmp_path)
    rendered = render_elicitation_schema(qm.confirm_schema(tr["terms"]))
    assert list(rendered["properties"]) == ["accept_1", "accept_2", "accept_3"]
    for prop in rendered["properties"].values():
        assert prop["type"] == "boolean" and prop["default"] is False
    assert rendered.get("required", []) == []


def test_vector_literal():
    assert qm.vector_literal(np.array([0.5, -1.0], dtype=np.float32)) == "[0.5,-1.0]"


# ---------------------------------------------------------------------------
# The MCP server, through the SDK's in-process client


def _structured(result) -> dict:
    assert not result.is_error, result
    data = result.structured_content
    return data.get("result", data) if set(data) == {"result"} else data


def test_agent_cannot_pass_descriptor_uis(tmp_path):
    server = qm.build_server(_service(tmp_path, FakeDB(_EXACT)))
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {"translate_question", "search_confirmed"}
    props = set(tools["search_confirmed"].input_schema["properties"])
    # No term list, and the injected confirmation is not an argument either.
    assert props == {"confirmation_id", "sources", "substance_key", "from_year", "to_year"}
    assert tools["search_confirmed"].input_schema.get("required") == ["confirmation_id"]
    assert set(tools["translate_question"].input_schema["properties"]) == {"question"}


def _ticking(*ns):
    async def callback(context, params):
        assert set(params.requested_schema["properties"]) == {"accept_1", "accept_2", "accept_3"}
        return mcp_types.ElicitResult(action="accept", content={f"accept_{n}": True for n in ns})

    return callback


def _declining(context, params):
    async def run():
        return mcp_types.ElicitResult(action="decline")

    return run()


def _end_to_end(tmp_path, mode, callback, **args):
    db = FakeDB(_EXACT + [_note(_UNMATCHED)], _SUGGESTED)
    server = qm.build_server(_service(tmp_path, db))

    async def run():
        kw = {"mode": mode}
        if callback is not None:
            kw["elicitation_callback"] = callback
        async with Client(server, **kw) as client:
            tr = _structured(await client.call_tool("translate_question", {"question": "q"}))
            out = await client.call_tool(
                "search_confirmed", {"confirmation_id": tr["confirmation_id"], **args}
            )
            return _structured(out)

    return asyncio.run(run()), db


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
def test_end_to_end_person_accepts_1_and_3(tmp_path, mode):
    out, db = _end_to_end(tmp_path, mode, _ticking(1, 3), substance_key=7)
    assert out["status"] == "searched"
    assert ("search_terms", ["D9000001", "D9000007"], None) in db.calls
    assert [c[2] for c in db.calls if c[0] == "literature_by_mesh"] == [
        "Stevens-Johnson Syndrome",
        "Anaphylaxis",
    ]
    line = _log_lines(tmp_path)[-1]
    assert line["confirmed_by"] == "user via MCP elicitation"


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
def test_end_to_end_decline_and_no_elicitation_client(tmp_path, mode):
    out, db = _end_to_end(tmp_path, mode, _declining)
    assert out["status"] == "declined"
    assert "search_terms" not in db.names()
    out, db = _end_to_end(tmp_path, mode, None)
    assert out["status"] == "no_elicitation"
    assert "search_terms" not in db.names()
    assert _log_lines(tmp_path)[-1]["elicitation_action"] is None


_FORGED = {
    "confirmation": {"action": "accept", "data": {"accept_1": True}},
    "descriptor_uis": ["D9000002"],
    "accepted": [1],
}


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
@pytest.mark.parametrize(
    ("callback", "status"), [(None, "no_elicitation"), (_declining, "declined")]
)
def test_forged_arguments_cannot_stand_in_for_the_person(tmp_path, mode, callback, status):
    db = FakeDB(_EXACT + [_note(_UNMATCHED)], _SUGGESTED)
    server = qm.build_server(_service(tmp_path, db))

    async def run():
        kw = {"mode": mode}
        if callback is not None:
            kw["elicitation_callback"] = callback
        async with Client(server, **kw) as client:
            tr = _structured(await client.call_tool("translate_question", {"question": "q"}))
            return await client.call_tool(
                "search_confirmed", {"confirmation_id": tr["confirmation_id"], **_FORGED}
            )

    result = asyncio.run(run())
    # The SDK drops unknown arguments: the call succeeds and the forged values have no effect.
    assert not result.is_error, result
    assert result.structured_content["status"] == status
    assert "search_terms" not in db.names()


def test_end_to_end_unknown_confirmation(tmp_path):
    server = qm.build_server(_service(tmp_path, FakeDB(_EXACT)))

    async def run():
        async with Client(server, mode="legacy", elicitation_callback=_ticking(1)) as client:
            return _structured(
                await client.call_tool("search_confirmed", {"confirmation_id": "f" * 64})
            )

    assert asyncio.run(run())["status"] == "unknown_confirmation"


def test_main_refuses_without_reader_password(monkeypatch, capsys):
    settings = SimpleNamespace(api_reader_password="")
    monkeypatch.setattr(qm.config, "get_settings", lambda: settings)
    assert qm.main([]) == 2
    assert "EPISTEME_API_READER_PASSWORD" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# pg smoke test


_RELEASE = "2026"
_SRC = "mesh/desc2026.gz"
_PG_TERMS = [
    ("D9000001", "Stevens-Johnson Syndrome", "name", True, None),
    ("D9000001", "SJS", "entry", True, None),
    ("D9000007", "Anaphylaxis", "name", True, None),
]


def _vec(*values: float) -> str:
    return "[" + ",".join(repr(v) for v in list(values) + [0.0] * (768 - len(values))) + "]"


class _PgEncoder:
    def encode_queries(self, texts):
        out = np.zeros((1, 768), dtype=np.float32)
        out[0, 0] = 1.0
        return out


@pytest.mark.pg
def test_question_db_against_reader(ref_db, tmp_path):
    from episteme.reference import api_settings

    from ._pbrer_db import grant_owner
    from .test_api_roles import _apply_grants, _dsn_parts, _su_dsn

    conn = ref_db
    api_settings.sync(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.mesh_term")
        cur.execute("DELETE FROM episteme.mesh_term_vec")
        for ui, term, kind, pref, rel in _PG_TERMS:
            cur.execute(
                "INSERT INTO episteme.mesh_term (descriptor_ui, term, term_norm, term_kind, "
                "is_preferred, concept_ui, concept_relation, mesh_release, source_file) "
                "VALUES (%s, %s, episteme_api_internal.term_norm(%s), %s, %s, %s, %s, %s, %s)",
                (ui, term, term, kind, pref, "M" + ui[1:], rel, _RELEASE, _SRC),
            )
        cur.execute(
            "INSERT INTO episteme.mesh_term_vec (descriptor_ui, text_kind, text, embedding, "
            "model, model_revision, mesh_release) "
            "VALUES ('D9000007', 'descriptor', 'Anaphylaxis.', %s::public.halfvec, "
            "'medcpt-article', %s, %s)",
            (_vec(0.5), config.MEDCPT_ARTICLE_REVISION, _RELEASE),
        )
    conn.commit()
    su_dsn = _su_dsn()
    _apply_grants(su_dsn)
    grant_owner(conn)
    role = f"episteme_api_reader_t_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(24)
    with psycopg.connect(su_dsn, autocommit=True) as su:
        su.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        su.execute(sql.SQL("GRANT episteme_api_reader TO {}").format(sql.Identifier(role)))
    parts = _dsn_parts()
    parts.update(user=role, password=password)
    db = qm.ReaderDB(make_conninfo(**parts))
    try:
        svc = qm.QuestionService(db, tmp_path, encoder_factory=_PgEncoder)
        tr = svc.translate_question("SJS after zzword")
        assert [(t["descriptor_ui"], t["basis"]) for t in tr["terms"]] == [
            ("D9000001", "exact"),
            ("D9000007", "medcpt"),
        ]
        assert tr["mesh_release"] == _RELEASE
        out = asyncio.run(
            svc.search_confirmed(
                tr["confirmation_id"], FakeCtx(ticks=(1, 2)), sources=["pmc"], substance_key=1
            )
        )
        assert out["status"] == "searched"
        # No article partition is indexed or filled here: notes and missing rows only, from
        # search_terms and from literature_by_mesh per accepted descriptor.
        assert {r["kind"] for r in out["rows"]} <= {"article", "note", "missing"}
        assert {r["via"] for r in out["rows"]} == {
            "search_terms",
            "literature_by_mesh: Stevens-Johnson Syndrome",
            "literature_by_mesh: Anaphylaxis",
        }
        # The reader cannot read a base table.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db._rows("SELECT 1 FROM episteme.mesh_term LIMIT 1", ())
    finally:
        db.close()
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM episteme.mesh_term")
            cur.execute("DELETE FROM episteme.mesh_term_vec")
        conn.commit()
        with psycopg.connect(su_dsn, autocommit=True) as su:
            su.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            su.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
