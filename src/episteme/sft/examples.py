"""Stage B instruction examples: the four kinds, teacher prompts and the batch generator.

Spec section 9.2 (Stage B) and 9.4 (no teacher output is trusted). The grounding of an
example (passage, tool calls, tool results, rule-check document, abstain reason) comes
from the input row; the teacher writes only the text around it. Every generated example
passes the deterministic checks of ``episteme.sft.checks`` before it is kept.

Training data rule: only rows with ``subset == 'commercial'`` from sources outside
``corpus_excluded_sources`` go to the teacher; other rows are refused (reason
``licence``) and cost no request.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from episteme.harness.loop import TOOLS
from episteme.reference.claims import LookupResult
from episteme.reference.rules import MAH_MARKER, Finding
from episteme.sft.manifest import write_manifest
from episteme.sft.teacher import BudgetExceeded, Teacher

log = logging.getLogger(__name__)

ABSTAIN_REASONS: tuple[str, ...] = (MAH_MARKER, "unverified")
KINDS: tuple[str, ...] = ("grounded_qa", "tool_draft", "rule_check", "abstain")
# Order of the reasons in a batch manifest. ``input``: the row lacks the fields its kind
# needs. The last four come from ``episteme.sft.checks``.
REJECT_REASONS: tuple[str, ...] = (
    "licence",
    "input",
    "format",
    "citation",
    "exact_values",
    "structure",
)
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
        " section heading to its body, with the headings of the given draft in the same order;"
        f" a section that needs company data holds exactly {MAH_MARKER!r}. "
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


def _check_input(kind: str, row: dict) -> None:
    if kind == "grounded_qa":
        _need(row, "id", "text")
    elif kind == "tool_draft":
        _need(row, "task", "tool_results")
        if len(row.get("tool_calls") or ()) != len(row["tool_results"]):
            raise InputError(f"row {row.get('id')!r}: tool_calls and tool_results differ")
    elif kind == "rule_check":
        _need(row, "document")
        if not isinstance(row["document"], dict):
            raise InputError(f"row {row.get('id')!r}: document is not an object")
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


# --- batch generator ----------------------------------------------------------------


def _licence_ok(row: dict, excluded: tuple[str, ...]) -> bool:
    source = row.get("source")
    return (
        row.get("subset") == "commercial"
        and isinstance(source, str)
        and bool(source.strip())
        and source not in excluded
    )


def _source_row(row: dict) -> dict:
    ident = {k: row.get(k) for k in ("id", "source", "subset", "content_hash")}
    if "_lineage" in row:
        ident["_lineage"] = row["_lineage"]
    return ident


def generate_batch(
    teacher: Teacher,
    passages: Iterable[dict],
    kind: str,
    n: int,
    out_dir: Path,
    *,
    excluded: tuple[str, ...] | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Generate up to ``n`` checked examples of ``kind`` from ``passages``.

    Writes ``out_dir/examples.jsonl`` (one kept example per line, with its source row and
    teacher) and ``out_dir/manifest.json`` (kind ``batch``). Stops when ``n`` examples are
    kept (``stopped`` None), when the rows run out (``exhausted``) or at the budget cap
    (``budget``); the examples kept so far stay written. ``excluded`` defaults to
    ``Settings.corpus_excluded_sources`` and ``max_tokens`` to ``Settings.teacher_max_tokens``.
    A row without a ``source`` or with a ``subset`` other than ``commercial`` is refused
    (reason ``licence``). HTTP and teacher errors propagate."""
    from episteme.sft.checks import run_checks

    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    if excluded is None or max_tokens is None:
        from episteme.config import get_settings

        settings = get_settings()
        if excluded is None:
            excluded = settings.corpus_excluded_sources
        if max_tokens is None:
            max_tokens = settings.teacher_max_tokens
    excluded = tuple(excluded)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    examples_path = out_dir / "examples.jsonl"
    manifest_path = out_dir / "manifest.json"

    rejected = dict.fromkeys(REJECT_REASONS, 0)
    inputs: list[dict] = []
    kept = 0
    stopped: str | None = "exhausted"
    used_at_start = teacher.used_tokens

    def reject(row: dict, reason: str, detail: str) -> None:
        rejected[reason] += 1
        inputs.append({**_source_row(row), "outcome": reason})
        log.info("batch %s: row %r rejected (%s): %s", kind, row.get("id"), reason, detail)

    with examples_path.open("w", encoding="utf-8") as out:
        for row in passages if n > 0 else ():
            if not _licence_ok(row, excluded):
                reject(
                    row, "licence", f"subset {row.get('subset')!r}, source {row.get('source')!r}"
                )
                continue
            try:
                _check_input(kind, row)
            except InputError as exc:
                reject(row, "input", str(exc))
                continue
            try:
                raw = teacher.complete(build_messages(kind, row), max_tokens=max_tokens)
            except BudgetExceeded as exc:
                log.info("batch %s: %s; stopping", kind, exc)
                stopped = "budget"
                break
            try:
                ex = parse_example(kind, row, raw)
            except FormatError as exc:
                reject(row, "format", str(exc))
                continue
            problems = run_checks(ex)
            if problems:
                reason, detail = problems[0]
                reject(row, reason, detail)
                continue
            record = {
                "kind": kind,
                "example": example_to_json(ex),
                "messages": ex.to_chat(),
                "source_row": _source_row(row),
                "teacher": teacher.model,
            }
            if kind == "tool_draft":
                record["tools"] = TOOLS
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            kept += 1
            inputs.append({**_source_row(row), "outcome": "kept"})
            if kept >= n:
                stopped = None
                break
    if n <= 0:
        stopped = None

    budget_used = teacher.used_tokens - used_at_start
    counts = {"kept": kept, **{f"rejected_{r}": c for r, c in rejected.items()}}
    config = {
        "kind": kind,
        "n": n,
        "max_tokens": max_tokens,
        "excluded": sorted(excluded),
        "teacher_model": teacher.model,
        "prompt": PROMPTS[kind],
    }
    digest = write_manifest(
        manifest_path,
        "batch",
        inputs=inputs,
        counts=counts,
        config=config,
        teacher=teacher.model,
        budget_used=budget_used,
        extra={"stopped": stopped},
    )
    log.info(
        "batch %s: kept %d, rejected %s, %d tokens, stopped %s",
        kind,
        kept,
        rejected,
        budget_used,
        stopped,
    )
    return {
        "kept": kept,
        "rejected": rejected,
        "budget_used": budget_used,
        "stopped": stopped,
        "config_hash": digest,
        "examples": str(examples_path),
        "manifest": str(manifest_path),
    }
