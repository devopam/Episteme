"""Minimal tool loop over an OpenAI-compatible chat endpoint.

``post`` and ``call_tool`` are injected. The loop adds ``tenant`` and ``caller`` to every
tool call and drops any value the model sends for them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from episteme.harness.pbrer import SECTION_SOURCES
from episteme.reference.claims import LookupResult

_INT = {"type": "integer", "description": "substance_key from resolve_entity"}
_DATE = {"type": "string", "description": "ISO date YYYY-MM-DD"}
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
        "topic": {"type": "string", "description": "optional topic"},
    },
}
_OPTIONAL = {"id_type", "topic"}
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
assert set(_NAMES) == set(_PARAMS), "TOOLS and SECTION_SOURCES have drifted"

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


def _default_post(payload: dict) -> dict:
    import requests

    from episteme.config import get_settings

    url = get_settings().harness_base_url.rstrip("/") + "/chat/completions"
    resp = requests.post(url, json=payload, timeout=300)
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
    tenant: str = "harness",
    caller: str = "harness-loop",
) -> tuple[str, list[LookupResult]]:
    """Chat until the model answers. Returns the answer and every lookup result.

    Each tool call counts toward ``max_tool_calls``, an unknown tool included. When the
    budget is spent the loop asks once more with no tools and returns that answer."""
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
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
            except ValueError as exc:
                msgs.append(_tool_message(call["id"], f"error: bad arguments: {exc}"))
                continue
            args = {k: v for k, v in args.items() if k not in _RESERVED}
            args.update(tenant=tenant, caller=caller)
            try:
                result = call_tool(name, args)
            except Exception as exc:  # tool failure goes back to the model
                msgs.append(_tool_message(call["id"], f"error: {type(exc).__name__}: {exc}"))
                continue
            results.append(result)
            msgs.append(_tool_message(call["id"], json.dumps(result.to_json())))
