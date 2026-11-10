"""Stage B instruction examples: the four kinds, the teacher prompts and the parsing.

Spec section 9.2 (Stage B) and 9.4 (no teacher output is trusted). The grounding of an
example (passage, tool calls, tool results, rule-check document, abstain reason) comes
from the input row; the teacher writes only the text around it. The batch generator is
``episteme.sft.batch``; the checks are ``episteme.sft.checks``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from episteme.harness.pbrer import section_headings
from episteme.reference.claims import LookupResult
from episteme.reference.rules import MAH_MARKER, Finding

ABSTAIN_REASONS: tuple[str, ...] = (MAH_MARKER, "unverified")
KINDS: tuple[str, ...] = ("grounded_qa", "tool_draft", "rule_check", "abstain")
_RESERVED_ARGS = ("tenant", "caller")

_JSON_ONLY = "Reply with one JSON object and nothing else: no prose, no code fence, no comment."
PROMPTS: dict[str, str] = {
    "grounded_qa": (
        "You write question-answer pairs for a medical language model. You get one source"
        " passage with its reference. Write one question that the passage answers, and the"
        " answer. The answer quotes the passage word for word in double quotes and cites the"
        " reference in square brackets. Use only facts stated in the passage; do not add a"
        " number, date or identifier that the passage does not state. " + _JSON_ONLY + " Keys:"
        ' "question" (string), "answer" (string).'
    ),
    "tool_draft": (
        "You write the final answer of a tool-using medical assistant. You get the task, the"
        " tool calls the assistant made and the tool results. Write the answer from the tool"
        " results only. Every date, count and identifier in the answer must appear in the"
        " task or the tool results. State coverage gaps as the results give them. When"
        ' "pbrer_region" is given, the answer is a PBRER draft: a JSON object that maps each'
        ' heading of "headings", in that order and with no other key, to its body. Start from'
        ' "skeleton" when it is given: keep its facts, coverage gaps and markers, and write'
        " the sections it leaves to be drafted from those facts;"
        f" a section that needs company data holds exactly {MAH_MARKER!r} and nothing else. "
        + _JSON_ONLY
        + ' Keys: "answer" (string, or for a PBRER draft an object of heading to body).'
    ),
    "rule_check": (
        "You check a PBRER draft against ICH E2C(R2). You get the draft (heading to body) and"
        " the region. List every finding: a missing or misordered section, or a company-data"
        f" section that does not hold exactly {MAH_MARKER!r}. Use the rule id and clause of the"
        " rule store; write a short message for each finding. " + _JSON_ONLY + " Keys:"
        ' "findings" (list of objects with "rule_id", "clause", "message").'
    ),
    "abstain": (
        "You write questions that a medical assistant must decline. You get a passage and"
        f" the abstain reason: {MAH_MARKER!r} (the answer needs company data, which is not"
        ' available) or "unverified" (no source can confirm the answer). Write one question'
        " about the passage's subject that the assistant must decline for that reason."
        " " + _JSON_ONLY + ' Keys: "question" (string).'
    ),
}


class FormatError(ValueError):
    """The teacher reply is not the JSON object its kind needs."""


class InputError(ValueError):
    """The input row lacks the fields its kind needs."""


# --- example kinds ----------------------------------------------------------------


@dataclass(frozen=True)
class GroundedQA:
    question: str
    passage: str
    passage_ref: str
    answer: str

    def to_chat(self) -> list[dict]:
        user = f"Passage [{self.passage_ref}]:\n{self.passage}\n\nQuestion: {self.question}"
        return [{"role": "user", "content": user}, {"role": "assistant", "content": self.answer}]


@dataclass(frozen=True)
class ToolDraft:
    """A task, the tool calls of the harness and their recorded results, and the answer.

    ``tool_calls`` items are ``{"name": ..., "arguments": {...}}``. With ``pbrer_region``
    set, ``answer`` is the JSON text of a PBRER draft (heading -> body)."""

    task: str
    tool_calls: tuple[dict, ...]
    tool_results: tuple[LookupResult, ...]
    answer: str
    pbrer_region: str | None = None

    def to_chat(self) -> list[dict]:
        """The harness loop format (``episteme.harness.loop``): one assistant turn with all
        tool calls, one tool message per result, then the answer. ``tenant`` and ``caller``
        are left out of the arguments, because the loop adds them."""
        calls = [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": json.dumps(
                        {
                            k: v
                            for k, v in (c.get("arguments") or {}).items()
                            if k not in _RESERVED_ARGS
                        }
                    ),
                },
            }
            for i, c in enumerate(self.tool_calls)
        ]
        msgs: list[dict] = [{"role": "user", "content": self.task}]
        if calls:
            msgs.append({"role": "assistant", "content": None, "tool_calls": calls})
            msgs.extend(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result.to_json()),
                }
                for call, result in zip(calls, self.tool_results, strict=False)
            )
        msgs.append({"role": "assistant", "content": self.answer})
        return msgs


@dataclass(frozen=True)
class RuleCheck:
    document: dict[str, str]
    findings: tuple[Finding, ...]
    region: str = "ich"

    def to_chat(self) -> list[dict]:
        user = (
            f"Check this PBRER draft against ICH E2C(R2) (region {self.region}). "
            "List the findings as JSON.\n" + json.dumps(self.document, ensure_ascii=False)
        )
        findings = [asdict(f) for f in self.findings]
        return [
            {"role": "user", "content": user},
            {"role": "assistant", "content": json.dumps(findings, ensure_ascii=False)},
        ]


@dataclass(frozen=True)
class Abstain:
    question: str
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in ABSTAIN_REASONS:
            raise ValueError(f"reason must be one of {ABSTAIN_REASONS}, got {self.reason!r}")

    def to_chat(self) -> list[dict]:
        return [
            {"role": "user", "content": self.question},
            {"role": "assistant", "content": self.reason},
        ]


Example = GroundedQA | ToolDraft | RuleCheck | Abstain


def example_to_json(ex: Example) -> dict[str, Any]:
    if isinstance(ex, ToolDraft):
        return {
            "task": ex.task,
            "tool_calls": list(ex.tool_calls),
            "tool_results": [r.to_json() for r in ex.tool_results],
            "answer": ex.answer,
            "pbrer_region": ex.pbrer_region,
        }
    return asdict(ex) if is_dataclass(ex) else dict(ex)


# --- prompts and parsing ----------------------------------------------------------


def _need(row: dict, *keys: str) -> None:
    missing = [k for k in keys if not row.get(k)]
    if missing:
        raise InputError(f"row {row.get('id')!r} lacks {missing}")


def _check_lookup_results(row: dict, results: list) -> None:
    for r in results:
        try:
            LookupResult.from_json(r)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise FormatError(
                f"row {row.get('id')!r}: a tool result is not a lookup result"
                f" ({type(exc).__name__})"
            ) from None


def check_input(kind: str, row: dict) -> None:
    """Raise ``InputError`` when ``row`` lacks what ``kind`` needs, and ``FormatError``
    when a tool result is not a well-formed lookup result (claims and provenance). Every
    kind needs an ``id``, so each example records the row it came from."""
    _need(row, "id")
    if kind == "grounded_qa":
        _need(row, "text")
    elif kind == "tool_draft":
        _need(row, "task")
        results = row.get("tool_results")
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            raise InputError(f"row {row.get('id')!r}: tool_results is not a list of objects")
        if len(row.get("tool_calls") or ()) != len(results):
            raise InputError(f"row {row.get('id')!r}: tool_calls and tool_results differ")
        _check_lookup_results(row, results)
        region = row.get("pbrer_region")
        if region is not None and region not in ("ich", "eu"):
            raise InputError(f"row {row.get('id')!r}: pbrer_region {region!r}")
        if row.get("skeleton") is not None and not isinstance(row["skeleton"], dict):
            raise InputError(f"row {row.get('id')!r}: skeleton is not an object")
    elif kind == "rule_check":
        _need(row, "document")
        if not isinstance(row["document"], dict):
            raise InputError(f"row {row.get('id')!r}: document is not an object")
        # The tool results the skeleton was built from; missing ones fail the licence check.
        results = row.get("tool_results")
        if results is not None:
            if not isinstance(results, list):
                raise FormatError(f"row {row.get('id')!r}: tool_results is not a list")
            _check_lookup_results(row, results)
    elif kind == "abstain":
        _need(row, "text", "reason")
        if row["reason"] not in ABSTAIN_REASONS:
            raise InputError(f"row {row.get('id')!r}: reason {row['reason']!r}")


def build_messages(kind: str, row: dict) -> list[dict]:
    """The teacher request for ``row``: the kind prompt, then the input as JSON."""
    if kind == "grounded_qa":
        user = {"passage_ref": row["id"], "passage": row["text"]}
    elif kind == "tool_draft":
        user = {
            "task": row["task"],
            "tool_calls": row.get("tool_calls") or [],
            "tool_results": row["tool_results"],
        }
        if row.get("pbrer_region"):
            user["pbrer_region"] = row["pbrer_region"]
            user["headings"] = section_headings(row["pbrer_region"])
            if row.get("skeleton"):
                user["skeleton"] = row["skeleton"]
    elif kind == "rule_check":
        user = {"draft": row["document"], "region": row.get("region") or "ich"}
    elif kind == "abstain":
        user = {"passage": row["text"], "reason": row["reason"]}
    else:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    return [
        {"role": "system", "content": PROMPTS[kind]},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _str_field(obj: dict, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FormatError(f"field {key!r} is missing or not a non-empty string")
    return value


def parse_example(kind: str, row: dict, raw: str) -> Example:
    """Build the example from the teacher reply ``raw`` (strict JSON) and the input row.
    Raises ``FormatError`` when the reply is not the JSON object the kind needs."""
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise FormatError(f"reply is not JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise FormatError("reply is not a JSON object")
    try:
        if kind == "grounded_qa":
            return GroundedQA(
                _str_field(obj, "question"), row["text"], str(row["id"]), _str_field(obj, "answer")
            )
        if kind == "tool_draft":
            region = row.get("pbrer_region")
            answer = obj.get("answer")
            if region and isinstance(answer, dict):
                answer = json.dumps(answer, ensure_ascii=False)
            if not isinstance(answer, str) or not answer.strip():
                raise FormatError("field 'answer' is missing or empty")
            return ToolDraft(
                row["task"],
                tuple(row.get("tool_calls") or ()),
                tuple(LookupResult.from_json(r) for r in row["tool_results"]),
                answer,
                region,
            )
        if kind == "rule_check":
            items = obj.get("findings")
            if not isinstance(items, list):
                raise FormatError("field 'findings' is missing or not a list")
            findings = []
            for item in items:
                if not isinstance(item, dict):
                    raise FormatError("a finding is not an object")
                findings.append(
                    Finding(
                        _str_field(item, "rule_id"),
                        _str_field(item, "clause"),
                        _str_field(item, "message"),
                    )
                )
            return RuleCheck(dict(row["document"]), tuple(findings), row.get("region") or "ich")
        if kind == "abstain":
            return Abstain(_str_field(obj, "question"), row["reason"])
    except FormatError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise FormatError(f"reply does not fit {kind}: {type(exc).__name__}") from None
    raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
