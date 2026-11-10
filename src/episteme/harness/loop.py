"""Minimal tool loop over an OpenAI-compatible chat endpoint.

``post`` and ``call_tool`` are injected. The loop adds ``tenant`` and ``caller`` to every
tool call and drops any value the model sends for them. ``make_call_tool`` owns the
conversion that the tool schemas need (ISO date strings to ``date``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date
from typing import Any

import psycopg

from episteme.harness.pbrer import SECTION_SOURCES
from episteme.reference import literature as _literature
from episteme.reference import lookups as _lookups
from episteme.reference import resolve as _resolve
from episteme.reference.claims import LookupResult

log = logging.getLogger(__name__)


class ToolArgumentError(Exception):
    """The model sent arguments that do not fit the tool schema."""


_INT = {"type": "integer", "description": "substance_key from resolve_entity"}
_DATE = {"type": "string", "description": "ISO date YYYY-MM-DD"}
# Parameter order equals the positional order of the real function, after ``conn``.
_PARAMS: dict[str, dict[str, Any]] = {
    "resolve_entity": {
        "text": {"type": "string", "description": "drug name or UNII"},
        "id_type": {"type": "string", "description": "optional identifier type"},
    },
    "approval_status": {"substance_key": _INT},
    "safety_actions": {"substance_key": _INT, "start": _DATE, "end": _DATE},
    "current_label": {"substance_key": _INT},
    "trials": {"substance_key": _INT},
    "faers_counts": {"substance_key": _INT, "start": _DATE, "end": _DATE},
    "literature": {
        "substance_key": _INT,
        "topic": {"type": ["string", "null"], "description": "topic, or null for none"},
    },
}
_OPTIONAL = {"id_type"}
_DOC = {
    "resolve_entity": "Resolve a drug name or UNII to a substance.",
    "approval_status": "Marketing authorisation status from FDA and EMA records.",
    "safety_actions": "Recalls, referrals, DHPCs and PSUSA outcomes in a date range.",
    "current_label": "Current US label sections.",
    "trials": "Registered trials of the substance.",
    "faers_counts": "FAERS report counts in a date range.",
    "literature": "Literature records for the substance.",
}

# The tool set must match the lookups the PBRER section map uses.
_NAMES = ["resolve_entity", *sorted({n for v in SECTION_SOURCES.values() for n in v})]
if set(_NAMES) != set(_PARAMS):
    raise RuntimeError("TOOLS and SECTION_SOURCES have drifted")

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": n,
            "description": _DOC[n],
            "parameters": {
                "type": "object",
                "properties": _PARAMS[n],
                "required": [p for p in _PARAMS[n] if p not in _OPTIONAL],
            },
        },
    }
    for n in _NAMES
]
_TOOL_NAMES = frozenset(_NAMES)
_RESERVED = ("tenant", "caller")


def _convert(name: str, args: dict) -> list[Any]:
    """Check ``args`` against the schema of ``name``; return values in call order."""
    spec = _PARAMS[name]
    args = {k: v for k, v in args.items() if k not in _RESERVED}
    if extra := set(args) - set(spec):
        raise TypeError(f"unexpected arguments: {sorted(extra)}")
    values: list[Any] = []
    for pname, schema in spec.items():
        if pname not in args:
            if pname in _OPTIONAL:
                values.append(None)
                continue
            raise KeyError(pname)
        v = args[pname]
        if schema is _INT:
            v = int(v)
        elif schema is _DATE:
            v = date.fromisoformat(v)
        elif pname == "topic" and v is not None and not isinstance(v, str):
            raise TypeError("topic must be a string or null")
        values.append(v)
    return values


def make_call_tool(
    conn: psycopg.Connection, *, tenant: str, caller: str
) -> Callable[[str, dict], LookupResult]:
    """Build the ``call_tool`` for ``run``: check and convert the arguments, add the
    audit identity and call the real function. Bad arguments raise KeyError (missing),
    TypeError (unknown) or ValueError (bad value)."""

    def call(name: str, args: dict) -> LookupResult:
        try:
            values = _convert(name, args)
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolArgumentError(type(exc).__name__) from exc
        # Outside the try: an error in the lookup itself is a bug and must propagate.
        if name == "resolve_entity":
            return _resolve.resolve_entity(
                conn, values[0], id_type=values[1], tenant=tenant, caller=caller
            )
        module = _literature if name == "literature" else _lookups
        return getattr(module, name)(conn, *values, tenant=tenant, caller=caller)

    return call


def _default_post(payload: dict) -> dict:
    import requests

    from episteme.config import get_settings

    settings = get_settings()
    url = settings.harness_base_url.rstrip("/") + "/chat/completions"
    resp = requests.post(url, json=payload, timeout=settings.harness_request_timeout_s)
    resp.raise_for_status()
    return resp.json()


def _tool_message(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def run(
    messages: list[dict],
    *,
    call_tool: Callable[[str, dict], LookupResult],
    post: Callable[[dict], dict] = _default_post,
    max_tool_calls: int,
    tenant: str,
    caller: str,
) -> tuple[str, list[LookupResult]]:
    """Chat until the model answers. Returns the answer and every lookup result.

    Each tool call counts toward ``max_tool_calls``, an unknown tool included. When the
    budget is spent the loop asks once more with no tools and returns that answer.
    Only ToolArgumentError and psycopg.Error go back to the model, as a fixed text that
    names the tool; any other exception propagates."""
    msgs = list(messages)
    results: list[LookupResult] = []
    used = 0
    while True:
        exhausted = used >= max_tool_calls
        payload: dict[str, Any] = {"messages": msgs}
        if not exhausted:
            payload["tools"] = TOOLS
        message = post(payload)["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls or exhausted:
            return message.get("content") or "", results
        msgs.append(message)
        for call in calls:
            used += 1
            fn = call["function"]
            name = fn["name"]
            if used > max_tool_calls:
                msgs.append(_tool_message(call["id"], "error: tool-call budget spent"))
                continue
            if name not in _TOOL_NAMES:
                msgs.append(_tool_message(call["id"], f"error: unknown tool {name!r}"))
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = None
            if not isinstance(args, dict):
                msgs.append(
                    _tool_message(call["id"], f"error: arguments of {name} are not valid JSON")
                )
                continue
            args = {k: v for k, v in args.items() if k not in _RESERVED}
            args.update(tenant=tenant, caller=caller)
            try:
                result = call_tool(name, args)
            except ToolArgumentError as exc:
                log.warning("tool %s: argument error %s", name, exc)
                msgs.append(_tool_message(call["id"], f"error: arguments of {name} are invalid"))
                continue
            except psycopg.Error as exc:
                log.warning("tool %s: database error %s", name, type(exc).__name__)
                log.debug("tool %s: database error detail", name, exc_info=True)
                msgs.append(_tool_message(call["id"], f"error: {name} failed (database error)"))
                continue
            results.append(result)
            msgs.append(_tool_message(call["id"], json.dumps(result.to_json())))
