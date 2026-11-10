"""Deterministic checks of a Stage B example (spec section 9.4). No model call.

Each check takes an example and returns a list of problems; empty means it passes.
``run_checks`` runs them in a fixed order and tags each problem with its reason, the
name the batch manifest counts it under.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from episteme.harness.check import check_answer
from episteme.harness.loop import TOOLS
from episteme.harness.pbrer import gate, section_headings
from episteme.reference.claims import LookupResult
from episteme.reference.rules import REGIONS, Finding
from episteme.sft.examples import (
    ABSTAIN_REASONS,
    Abstain,
    Example,
    GroundedQA,
    RuleCheck,
    ToolDraft,
    loads_strict,
)

_TOOL_NAMES = frozenset(t["function"]["name"] for t in TOOLS)
_QUOTED = re.compile(r'"([^"]*)"|“([^”]*)”')


def _ws(text: str) -> str:
    return " ".join(text.split())


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _pbrer_draft(answer: str) -> dict[str, str] | None:
    """The draft in a PBRER answer, or None when it is not a JSON object of strings or
    holds a heading twice."""
    try:
        draft = loads_strict(answer)
    except ValueError:
        return None
    if not isinstance(draft, dict) or not draft:
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in draft.items()):
        return None
    return draft


def check_format(example: Example) -> list[str]:
    """Required fields are present and well typed; tool names are harness tools; a PBRER
    answer is a JSON object of heading to body."""
    problems: list[str] = []
    if isinstance(example, GroundedQA):
        for name in ("question", "passage", "passage_ref", "answer"):
            if not _nonempty(getattr(example, name)):
                problems.append(f"{name} is empty")
    elif isinstance(example, ToolDraft):
        for name in ("task", "answer"):
            if not _nonempty(getattr(example, name)):
                problems.append(f"{name} is empty")
        if len(example.tool_calls) != len(example.tool_results):
            problems.append(
                f"{len(example.tool_calls)} tool calls but {len(example.tool_results)} results"
            )
        for call in example.tool_calls:
            if not isinstance(call, dict) or call.get("name") not in _TOOL_NAMES:
                name = call.get("name") if isinstance(call, dict) else call
                problems.append(f"unknown tool {name!r}")
            elif not isinstance(call.get("arguments", {}), dict):
                problems.append(f"arguments of {call['name']} are not an object")
        if not all(isinstance(r, LookupResult) for r in example.tool_results):
            problems.append("a tool result is not a lookup result")
        if example.pbrer_region is not None:
            if example.pbrer_region not in REGIONS:
                problems.append(f"unknown region {example.pbrer_region!r}")
            if _nonempty(example.answer) and _pbrer_draft(example.answer) is None:
                problems.append("PBRER answer is not a JSON object of heading to body")
    elif isinstance(example, RuleCheck):
        doc = example.document
        if not isinstance(doc, dict) or not doc:
            problems.append("document is empty")
        elif not all(isinstance(k, str) and isinstance(v, str) for k, v in doc.items()):
            problems.append("document is not heading to body text")
        if example.region not in REGIONS:
            problems.append(f"unknown region {example.region!r}")
        for f in example.findings:
            if not isinstance(f, Finding) or not all(
                _nonempty(x) for x in (f.rule_id, f.clause, f.message)
            ):
                problems.append(f"malformed finding {f!r}")
    elif isinstance(example, Abstain):
        if not _nonempty(example.question):
            problems.append("question is empty")
        if example.reason not in ABSTAIN_REASONS:
            problems.append(f"reason {example.reason!r} not in {ABSTAIN_REASONS}")
    else:
        problems.append(f"unknown example type {type(example).__name__}")
    return problems


def check_citation_quotes_passage(example: Example) -> list[str]:
    """A grounded answer quotes and cites the passage: every quoted span (straight or curly
    double quotes) is a substring of the passage after whitespace normalisation, and the
    answer cites ``[passage_ref]``. An answer with no quoted span fails. Other kinds have
    no passage and pass."""
    if not isinstance(example, GroundedQA):
        return []
    spans = [
        _ws(m.group(1) if m.group(1) is not None else m.group(2))
        for m in _QUOTED.finditer(example.answer)
    ]
    if not any(spans):
        return ["answer has no quoted span"]
    passage = _ws(example.passage)
    problems = [f"quote not in passage: {s!r}" for s in spans if not s or s not in passage]
    if f"[{example.passage_ref}]" not in example.answer:
        problems.append(f"answer does not cite [{example.passage_ref}]")
    return problems


def check_exact_values(example: Example) -> list[str]:
    """Dates, ids and counts in the answer that the grounding does not state
    (``episteme.harness.check.check_answer``). A grounded answer checks against its passage
    and reference; a tool answer against its tool results, its task and its call
    arguments. A PBRER draft is checked on its bodies, not its headings; the section
    headings of its region count as stated, so "section 16" is not an invented value."""
    if isinstance(example, GroundedQA):
        return check_answer(example.answer, [], known=(example.passage, example.passage_ref))
    if isinstance(example, ToolDraft):
        text = example.answer
        if example.pbrer_region is not None:
            draft = _pbrer_draft(example.answer)
            if draft is not None:
                text = "\n".join(draft.values())
        known = [example.task] + [
            json.dumps(c.get("arguments") or {}) for c in example.tool_calls if isinstance(c, dict)
        ]
        if example.pbrer_region in ("ich", "eu"):
            known += section_headings(example.pbrer_region)
        return check_answer(text, list(example.tool_results), known=known)
    return []


def _finding_text(f: Finding) -> str:
    return f"{f.rule_id} ({f.clause}): {f.message}"


def check_structure(example: Example) -> list[str]:
    """Rule-store checks. A PBRER draft must pass the gate (``episteme.harness.pbrer``);
    the findings of a rule check must be exactly the gate's findings (rule id and clause).
    Other examples pass."""
    if isinstance(example, ToolDraft) and example.pbrer_region is not None:
        draft = _pbrer_draft(example.answer)
        if draft is None:
            return ["PBRER answer is not a JSON object of heading to body"]
        return [_finding_text(f) for f in gate(draft, region=example.pbrer_region)]
    if isinstance(example, RuleCheck):
        expected = Counter(
            (f.rule_id, f.clause) for f in gate(example.document, region=example.region)
        )
        got = Counter((f.rule_id, f.clause) for f in example.findings)
        problems = [f"missed finding {k[0]} ({k[1]})" for k in (expected - got).elements()]
        problems += [
            f"finding not raised by the gate: {k[0]} ({k[1]})" for k in (got - expected).elements()
        ]
        return problems
    return []


_ORDER = (
    ("citation", check_citation_quotes_passage),
    ("exact_values", check_exact_values),
    ("structure", check_structure),
)


def run_checks(example: Example) -> list[tuple[str, str]]:
    """All problems as (reason, detail). A format problem stops the other checks."""
    problems = [("format", p) for p in check_format(example)]
    if problems:
        return problems
    for reason, check in _ORDER:
        problems.extend((reason, p) for p in check(example))
    return problems
