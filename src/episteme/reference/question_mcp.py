"""Episteme MCP server ``question_mcp``: a question to MeSH terms, confirmed by the person.

Tools (stdio):

* ``translate_question(question)`` calls ``episteme_api.match_terms``. When a note reports
  unmatched words, it embeds the question with the MedCPT query encoder (loaded once, on
  the first suggestion) and calls ``episteme_api.suggest_terms``. It returns the exact terms
  first, then the suggestions, and a ``confirmation_id``.
* ``search_confirmed(confirmation_id, ...)`` asks the PERSON through MCP elicitation which
  proposed terms to accept (owner decision 1a, spec 5.4). The agent cannot pass a term
  list: the tool has no such parameter. Only accepted terms go to ``search_terms`` (and to
  ``literature_by_mesh`` when a substance key is given).

The server connects as ``episteme_api_reader``, so it can do nothing the reader cannot do.
It does not apply the production-database guard: the reader role is read-only by its
grants, and its target is the production database (as ``mcpg-episteme.sh --reader``).

Every ``translate_question`` and ``search_confirmed`` call appends one JSON line to
``<EPISTEME_QUESTION_LOG_DIR>/question-<YYYY-MM-DD>.jsonl`` (UTC date). The server never
reads the log back.

The elicitation runs through the SDK's ``Resolve`` mechanism, so it works on both protocol
generations: a mid-call ``elicitation/create`` request (protocol 2025-11-25 and older) and
``InputRequiredResult`` rounds (2026-07-28 and later).

No ``from __future__ import annotations`` here: the ``Resolve`` marker in a tool
annotation refers to a closure, and the SDK evaluates the annotations.
"""

import hashlib
import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Protocol

import anyio.to_thread
from mcp.server.mcpserver import Context, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.resolve import Elicit
from pydantic import BaseModel, Field, create_model

from episteme import config

_LOG = logging.getLogger(__name__)

READER_ROLE = "episteme_api_reader"
CONFIRMED_BY = "user via MCP elicitation"
UNMATCHED_PREFIX = "unmatched:"
QUERY_NOTE_PREFIX = "terms used:"

TERM_FIELDS = (
    "kind",
    "descriptor_ui",
    "descriptor_name",
    "matched_text",
    "matched_term",
    "term_kind",
    "concept_relation",
    "basis",
    "score",
    "mesh_release",
    "note",
)
ARTICLE_FIELDS = (
    "kind",
    "article_id",
    "source",
    "title",
    "year",
    "score",
    "relation",
    "release_id",
    "licence",
    "note",
    "source_file",
)

_TERM_COLS = ", ".join(TERM_FIELDS)
_ARTICLE_COLS = ", ".join(ARTICLE_FIELDS)


class QuestionDB(Protocol):
    """The four episteme_api functions this server calls. Each returns dict rows."""

    def match_terms(self, question: str) -> list[dict[str, Any]]: ...

    def suggest_terms(self, vector: str, model_revision: str) -> list[dict[str, Any]]: ...

    def search_terms(
        self, descriptor_uis: list[str], sources: list[str] | None
    ) -> list[dict[str, Any]]: ...

    def literature_by_mesh(
        self, substance_key: int, topic: str, from_year: int | None, to_year: int | None
    ) -> list[dict[str, Any]]: ...


class ReaderDB:
    """``QuestionDB`` over psycopg, as ``episteme_api_reader``. One autocommit connection,
    opened on the first call and opened again after it breaks; no call leaves a
    transaction open."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo
        self._conn: Any = None

    def _rows(self, query: str, params: tuple) -> list[dict[str, Any]]:
        import psycopg
        from psycopg.rows import dict_row

        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._conninfo, autocommit=True, row_factory=dict_row)
        try:
            with self._conn.cursor() as cur:
                cur.execute(query, params)
                return list(cur.fetchall())
        except psycopg.OperationalError:
            self.close()
            raise

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def match_terms(self, question: str) -> list[dict[str, Any]]:
        return self._rows(
            f"SELECT {_TERM_COLS} FROM episteme_api.match_terms(%s)",
            (question,),
        )

    def suggest_terms(self, vector: str, model_revision: str) -> list[dict[str, Any]]:
        return self._rows(
            f"SELECT {_TERM_COLS} FROM episteme_api.suggest_terms(%s::public.halfvec, %s)",
            (vector, model_revision),
        )

    def search_terms(
        self, descriptor_uis: list[str], sources: list[str] | None
    ) -> list[dict[str, Any]]:
        return self._rows(
            f"SELECT {_ARTICLE_COLS} FROM episteme_api.search_terms(%s::text[], %s::text[])",
            (list(descriptor_uis), None if sources is None else list(sources)),
        )

    def literature_by_mesh(
        self, substance_key: int, topic: str, from_year: int | None, to_year: int | None
    ) -> list[dict[str, Any]]:
        return self._rows(
            f"SELECT {_ARTICLE_COLS} FROM episteme_api.literature_by_mesh("
            "%s::bigint, %s, %s::int, %s::int)",
            (substance_key, topic, from_year, to_year),
        )


def reader_conninfo(settings: Any) -> str:
    """Connection string of ``episteme_api_reader`` (host, port, database from config)."""
    from psycopg.conninfo import make_conninfo

    from episteme.data.db.connection import app_name

    if not settings.api_reader_password:
        raise config.ConfigError(
            "EPISTEME_API_READER_PASSWORD is not set (needed to connect as episteme_api_reader)"
        )
    return make_conninfo(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_database,
        user=READER_ROLE,
        password=settings.api_reader_password,
        application_name=app_name(settings, ":question"),
    )


def vector_literal(vector: Any) -> str:
    """``[v1,...]``, the text form ``public.halfvec`` reads."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def _default_encoder() -> Any:
    from episteme.reference.medcpt import TERM_MODELS, MedCPTEncoder

    return MedCPTEncoder.query(TERM_MODELS["medcpt"])


def _default_doc_revision() -> str:
    # suggest_terms compares this with the stored vectors, which the DOCUMENT-side encoder
    # made: the doc revision of the active spec, not the query encoder's own revision.
    from episteme.reference.medcpt import TERM_MODELS

    return TERM_MODELS["medcpt"].doc_revision


@dataclass(frozen=True)
class Proposal:
    question: str
    mesh_release: str | None
    model_revision: str | None
    terms: tuple[dict[str, Any], ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class _Skip:
    """A resolver outcome that is not an elicitation: no prompt was sent."""

    status: str


def confirmation_id(question: str, mesh_release: str | None, terms: list[dict[str, Any]]) -> str:
    payload = {
        "question": question,
        "mesh_release": mesh_release,
        "terms": [[t["descriptor_ui"], t["basis"], t["score"]] for t in terms],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def term_title(term: dict[str, Any]) -> str:
    """``<name> (<UI>) — <basis>[, score <s>][, <relation>]``."""
    title = f"{term['descriptor_name']} ({term['descriptor_ui']}) — {term['basis']}"
    if term.get("score") is not None:
        title += f", score {float(term['score']):.3f}"
    if term.get("concept_relation"):
        title += f", {term['concept_relation']}"
    return title


def confirm_schema(terms: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> type[BaseModel]:
    """A flat model: one ``bool`` field ``accept_<n>`` (default False) per proposed term."""
    fields: dict[str, Any] = {
        f"accept_{t['n']}": (bool, Field(default=False, title=term_title(t))) for t in terms
    }
    return create_model("ConfirmMeshTerms", **fields)


def _confirm_message(proposal: Proposal) -> str:
    return (
        f"Question: {proposal.question}\n"
        f"Tick each MeSH term (MeSH {proposal.mesh_release or 'unknown'}) to search with. "
        "Terms you do not tick are not used."
    )


def _short(term: dict[str, Any]) -> dict[str, Any]:
    return {
        "n": term["n"],
        "descriptor_ui": term["descriptor_ui"],
        "descriptor_name": term["descriptor_name"],
        "basis": term["basis"],
        "score": term["score"],
        "concept_relation": term["concept_relation"],
    }


def _plain(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in fields:
        value = row.get(name)
        if name == "score" and value is not None:
            value = float(value)
        out[name] = value
    return out


class QuestionService:
    """The tool logic, free of the MCP transport (tests drive it with fakes)."""

    def __init__(
        self,
        db: QuestionDB,
        log_dir: Path,
        *,
        encoder_factory: Callable[[], Any] = _default_encoder,
        model_revision: Callable[[], str] = _default_doc_revision,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._db = db
        self._log_dir = Path(log_dir)
        self._encoder_factory = encoder_factory
        self._model_revision = model_revision
        self._encoder: Any = None
        self._now = now
        self._proposals: dict[str, Proposal] = {}

    # -- translate -------------------------------------------------------------------

    def _encode(self, question: str) -> str:
        if self._encoder is None:
            self._encoder = self._encoder_factory()
        vectors = self._encoder.encode_queries([question])
        return vector_literal(vectors[0])

    def translate_question(self, question: str) -> dict[str, Any]:
        rows = self._db.match_terms(question)
        terms = [_plain(r, TERM_FIELDS) for r in rows if r.get("kind") == "term"]
        notes = [r["note"] for r in rows if r.get("kind") == "note" and r.get("note")]
        revision: str | None = None
        if any(n.startswith(UNMATCHED_PREFIX) for n in notes):
            revision = self._model_revision()
            srows = self._db.suggest_terms(self._encode(question), revision)
            exact = {t["descriptor_ui"] for t in terms}
            for r in srows:
                if r.get("kind") == "term":
                    if r["descriptor_ui"] not in exact:
                        exact.add(r["descriptor_ui"])
                        terms.append(_plain(r, TERM_FIELDS))
                elif r.get("note"):
                    notes.append(r["note"])
        for i, t in enumerate(terms, start=1):
            t["n"] = i
        mesh_release = next((t["mesh_release"] for t in terms if t.get("mesh_release")), None)
        cid = confirmation_id(question, mesh_release, terms)
        self._proposals[cid] = Proposal(
            question, mesh_release, revision, tuple(terms), tuple(notes)
        )
        self._log(
            event="translate",
            question=question,
            confirmation_id=cid,
            terms=[_short(t) for t in terms],
        )
        return {
            "confirmation_id": cid,
            "mesh_release": mesh_release,
            "terms": [dict(t) for t in terms],
            "notes": list(notes),
        }

    # -- confirm ---------------------------------------------------------------------

    def elicit_request(self, cid: str, capabilities: Any = None, *, check: bool = True) -> Any:
        """The resolver body: an ``Elicit`` marker, or a ``_Skip`` when no prompt can be sent
        (unknown id, a proposal without terms, a client without form elicitation)."""
        proposal = self._proposals.get(cid)
        if proposal is None:
            return _Skip("unknown_confirmation")
        if not proposal.terms:
            return _Skip("nothing_accepted")
        if check:
            elicitation = getattr(capabilities, "elicitation", None)
            if elicitation is None or (elicitation.form is None and elicitation.url is not None):
                return _Skip("no_elicitation")
        return Elicit(_confirm_message(proposal), confirm_schema(proposal.terms))

    async def search_confirmed(
        self,
        confirmation_id: str,
        ctx: Any,
        sources: list[str] | None = None,
        substance_key: int | None = None,
        from_year: int | None = None,
        to_year: int | None = None,
    ) -> dict[str, Any]:
        """Ask through ``ctx.elicit`` directly, then ``complete``. An exception from the
        elicitation (a client without elicitation support) gives ``no_elicitation``."""
        request = self.elicit_request(confirmation_id, check=False)
        if isinstance(request, Elicit):
            try:
                outcome: Any = await ctx.elicit(request.message, request.schema)
            except Exception as exc:  # any failure means: nobody confirmed
                _LOG.warning("elicitation failed: %s", type(exc).__name__)
                outcome = _Skip("no_elicitation")
        else:
            outcome = request
        return self.complete(confirmation_id, outcome, sources, substance_key, from_year, to_year)

    def complete(
        self,
        cid: str,
        outcome: Any,
        sources: list[str] | None = None,
        substance_key: int | None = None,
        from_year: int | None = None,
        to_year: int | None = None,
    ) -> dict[str, Any]:
        """Act on the elicitation outcome. Only an ``accept`` with at least one ticked term
        runs a search."""
        proposal = self._proposals.get(cid)
        if proposal is None:
            return {
                "status": "unknown_confirmation",
                "accepted": [],
                "rejected": [],
                "rows": [],
                "provenance": {
                    "confirmation_id": cid,
                    "note": "unknown confirmation_id; call translate_question first",
                },
            }
        data = getattr(outcome, "data", None)
        if isinstance(data, _Skip):  # a resolver's plain outcome arrives wrapped
            outcome = data
        action: str | None = None
        accepted: list[dict[str, Any]] = []
        if isinstance(outcome, _Skip):
            status = outcome.status
        else:
            action = getattr(outcome, "action", None)
            if action == "accept":
                accepted = [
                    t for t in proposal.terms if getattr(data, f"accept_{t['n']}", False) is True
                ]
                status = "searched" if accepted else "nothing_accepted"
            elif action == "decline":
                status = "declined"
            elif action == "cancel":
                status = "cancelled"
            else:
                status = "no_elicitation"
        accepted_n = {t["n"] for t in accepted}
        rejected = [t for t in proposal.terms if t["n"] not in accepted_n]

        rows: list[dict[str, Any]] = []
        if status == "searched":
            uis = [t["descriptor_ui"] for t in accepted]
            for r in self._db.search_terms(uis, sources):
                rows.append({**_plain(r, ARTICLE_FIELDS), "via": "search_terms"})
            if substance_key is not None:
                for t in accepted:
                    for r in self._db.literature_by_mesh(
                        substance_key, t["descriptor_name"], from_year, to_year
                    ):
                        rows.append(
                            {
                                **_plain(r, ARTICLE_FIELDS),
                                "via": f"literature_by_mesh: {t['descriptor_name']}",
                            }
                        )
        queries = [
            r["note"]
            for r in rows
            if r.get("kind") == "note" and (r.get("note") or "").startswith(QUERY_NOTE_PREFIX)
        ]
        release_ids = sorted({r["release_id"] for r in rows if r.get("release_id")})
        confirmed_by = CONFIRMED_BY if action == "accept" else None
        accepted_short = [_short(t) for t in accepted]
        rejected_short = [_short(t) for t in rejected]
        self._log(
            event="confirm",
            question=proposal.question,
            confirmation_id=cid,
            terms=[_short(t) for t in proposal.terms],
            elicitation_action=action,
            confirmed_by=confirmed_by,
            accepted=accepted_short,
            rejected=rejected_short,
            queries=queries,
            release_ids=release_ids,
        )
        return {
            "status": status,
            "accepted": accepted_short,
            "rejected": rejected_short,
            "rows": rows,
            "provenance": {
                "question": proposal.question,
                "confirmation_id": cid,
                "mesh_release": proposal.mesh_release,
                "model_revision": proposal.model_revision,
                "proposed": [_short(t) for t in proposal.terms],
                "accepted": accepted_short,
                "rejected": rejected_short,
                "elicitation_action": action,
                "confirmed_by": confirmed_by,
                "queries": queries,
                "release_ids": release_ids,
                "notes": list(proposal.notes),
            },
        }

    # -- log -------------------------------------------------------------------------

    def _log(
        self,
        *,
        event: str,
        question: str,
        confirmation_id: str,
        terms: list[dict[str, Any]],
        elicitation_action: str | None = None,
        confirmed_by: str | None = None,
        accepted: list[dict[str, Any]] | None = None,
        rejected: list[dict[str, Any]] | None = None,
        queries: list[str] | None = None,
        release_ids: list[str] | None = None,
    ) -> None:
        ts = self._now().astimezone(timezone.utc)
        record = {
            "event": event,
            "ts": ts.isoformat(),
            "question": question,
            "confirmation_id": confirmation_id,
            "terms": terms,
            "elicitation_action": elicitation_action,
            "confirmed_by": confirmed_by,
            "accepted": accepted or [],
            "rejected": rejected or [],
            "queries": queries or [],
            "release_ids": release_ids or [],
        }
        self._log_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_dir / f"question-{ts:%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def build_server(service: QuestionService) -> MCPServer:
    server = MCPServer(
        "episteme-question",
        instructions=(
            "Translate a reviewer's question to MeSH terms with translate_question, then call "
            "search_confirmed with the confirmation_id. The person, not the agent, accepts "
            "the terms in a form; only accepted terms are searched."
        ),
    )

    def ask_person(confirmation_id: str, ctx: Context) -> Any:
        return service.elicit_request(confirmation_id, ctx.client_capabilities)

    @server.tool()
    async def translate_question(question: str) -> dict[str, Any]:
        """Propose MeSH descriptors for a question: exact matches first, then MedCPT
        suggestions (only when words stay unmatched). Returns a confirmation_id."""
        return await anyio.to_thread.run_sync(service.translate_question, question)

    @server.tool()
    async def search_confirmed(
        confirmation_id: str,
        confirmation: Annotated[ElicitationResult[BaseModel], Resolve(ask_person)],
        sources: list[str] | None = None,
        substance_key: int | None = None,
        from_year: int | None = None,
        to_year: int | None = None,
    ) -> dict[str, Any]:
        """Ask the person to accept proposed terms, then search with the accepted terms
        only. status: searched, declined, cancelled, no_elicitation, unknown_confirmation
        or nothing_accepted."""
        return await anyio.to_thread.run_sync(
            lambda: service.complete(
                confirmation_id, confirmation, sources, substance_key, from_year, to_year
            )
        )

    return server


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print("usage: python -m episteme.reference.question_mcp", file=sys.stderr)
        return 2
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    settings = config.get_settings()
    try:
        conninfo = reader_conninfo(settings)
    except config.ConfigError as exc:
        print(f"question_mcp: {exc}", file=sys.stderr)
        return 2
    db = ReaderDB(conninfo)
    service = QuestionService(db, settings.question_log_dir or settings.data_root / "_question_log")
    try:
        build_server(service).run("stdio")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
