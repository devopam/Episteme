"""Stage B batch generator: input rows -> teacher -> checks -> JSONL and a manifest.

Training data rule (spec section 9.2), decided per row before any request:

- ``grounded_qa`` and ``abstain``: the row has ``subset == 'commercial'`` and a ``source``
  outside ``corpus_excluded_sources``.
- ``tool_draft``: the licence comes from the data, not the row label. Every claim in the
  tool results (alternatives included) names its source in its provenance; each source
  must have class ``commercial`` in ``config.LOOKUP_SOURCE_SUBSETS``. The corpus
  exclusion does not apply: it governs raw text in the corpus, and the spec builds Stage B
  examples on lookup results. A ``pending`` source (``openfda_faers``) gives the reason
  ``licence_pending``.
- ``rule_check``: the document is a skeleton draft; the row says
  ``source = 'pbrer_skeleton'`` and ``subset = 'commercial'``.

Restart: ``examples.jsonl`` is never truncated. A re-run reads the ids already in it,
skips those rows (no request) and appends; ``n`` is the target count of the whole file.
The manifest is written on every exit, also on an error (``stopped = 'error'``); the
manifest of an earlier run is kept as ``manifest.run-<k>.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from pathlib import Path

from episteme.config import LOOKUP_SOURCE_SUBSETS
from episteme.harness.loop import TOOLS
from episteme.sft.checks import run_checks
from episteme.sft.examples import (
    KINDS,
    PROMPTS,
    FormatError,
    InputError,
    build_messages,
    check_input,
    example_to_json,
    parse_example,
)
from episteme.sft.manifest import write_manifest
from episteme.sft.teacher import BudgetExceeded, Teacher

log = logging.getLogger(__name__)

# Order of the reasons in a batch manifest. ``input``: the row lacks the fields its kind
# needs. The last four come from ``episteme.sft.checks``.
REJECT_REASONS: tuple[str, ...] = (
    "licence",
    "licence_pending",
    "input",
    "format",
    "citation",
    "exact_values",
    "structure",
)
RULE_CHECK_SOURCE = "pbrer_skeleton"
# Hash of the harness tool schema the tool-call examples are written in.
TOOLS_SHA256 = hashlib.sha256(
    json.dumps(TOOLS, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def _claim_sources(claim: dict) -> set[str]:
    found = {(claim.get("provenance") or {}).get("source")}
    for alt in claim.get("alternatives") or ():
        found |= _claim_sources(alt)
    return found


def lookup_sources(row: dict) -> list[str]:
    """Sources named in the provenance of every claim of the row's tool results."""
    found: set = set()
    for result in row.get("tool_results") or ():
        for claim in result.get("claims") or ():
            found |= _claim_sources(claim)
    return sorted(found, key=str)


def licence_reason(
    kind: str, row: dict, excluded: tuple[str, ...], lookup_subsets: dict[str, str]
) -> tuple[str, str] | None:
    """``(reason, detail)`` when the row may not reach the teacher, else None."""
    if kind == "tool_draft":
        sources = lookup_sources(row)
        bad = [s for s in sources if lookup_subsets.get(s) not in ("commercial", "pending")]
        if bad:
            return "licence", f"lookup sources not commercial: {bad}"
        pending = [s for s in sources if lookup_subsets.get(s) == "pending"]
        if pending:
            return "licence_pending", f"lookup sources pending an owner decision: {pending}"
        return None
    source, subset = row.get("source"), row.get("subset")
    if kind == "rule_check":
        if source != RULE_CHECK_SOURCE or subset != "commercial":
            return "licence", f"rule_check rows need source {RULE_CHECK_SOURCE!r}, commercial"
        return None
    if subset != "commercial":
        return "licence", f"subset {subset!r}"
    if not isinstance(source, str) or not source.strip():
        return "licence", "no source"
    if source in excluded:
        return "licence", f"source {source!r} is excluded from the corpus"
    return None


def _source_row(kind: str, row: dict) -> dict:
    ident = {k: row.get(k) for k in ("id", "source", "subset", "content_hash")}
    if "_lineage" in row:
        ident["_lineage"] = row["_lineage"]
    if kind == "tool_draft":
        ident["lookup_sources"] = lookup_sources(row)
    return ident


def _done_ids(path: Path) -> set[str]:
    """Ids of the rows already in ``examples.jsonl``. A line that is not a whole record
    stops the run: the file is never truncated or rewritten here."""
    done: set[str] = set()
    if not path.exists():
        return done
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            done.add(str(json.loads(line)["source_row"]["id"]))
        except (ValueError, KeyError, TypeError):
            raise ValueError(
                f"{path}: line {i} is not a complete record; move or repair the file"
                " before a re-run"
            ) from None
    return done


def _keep_old_manifest(path: Path) -> None:
    if not path.exists():
        return
    k = 1
    while (old := path.with_name(f"manifest.run-{k:03d}.json")).exists():
        k += 1
    path.rename(old)


def generate_batch(
    teacher: Teacher,
    passages: Iterable[dict],
    kind: str,
    n: int,
    out_dir: Path,
    *,
    excluded: tuple[str, ...] | None = None,
    max_tokens: int | None = None,
    lookup_subsets: dict[str, str] | None = None,
) -> dict:
    """Generate checked examples of ``kind`` until ``out_dir/examples.jsonl`` holds ``n``.

    Appends kept examples (with their source row, teacher and tool-schema hash) to
    ``out_dir/examples.jsonl`` and writes ``out_dir/manifest.json`` (kind ``batch``).
    ``stopped`` is None when ``n`` is reached, ``exhausted`` when the rows run out,
    ``budget`` at the budget cap, ``error`` when an exception ends the run (the manifest
    is written, then the exception propagates). ``excluded`` defaults to
    ``Settings.corpus_excluded_sources``, ``max_tokens`` to ``Settings.teacher_max_tokens``,
    ``lookup_subsets`` to ``config.LOOKUP_SOURCE_SUBSETS``."""
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
    lookup_subsets = dict(LOOKUP_SOURCE_SUBSETS if lookup_subsets is None else lookup_subsets)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    examples_path = out_dir / "examples.jsonl"
    manifest_path = out_dir / "manifest.json"

    done = _done_ids(examples_path)
    kept_before = len(done)
    rejected = dict.fromkeys(REJECT_REASONS, 0)
    inputs: list[dict] = []
    state: dict = {"kept": 0, "skipped_done": 0, "stopped": "exhausted", "error": None}
    used_at_start = teacher.used_tokens

    def reject(row: dict, reason: str, detail: str) -> None:
        rejected[reason] += 1
        inputs.append({**_source_row(kind, row), "outcome": reason})
        log.info("batch %s: row %r rejected (%s): %s", kind, row.get("id"), reason, detail)

    def run(out) -> None:
        if kept_before >= n:
            state["stopped"] = None
            return
        for row in passages:
            if row.get("id") is not None and str(row["id"]) in done:
                state["skipped_done"] += 1
                continue
            try:
                check_input(kind, row)
            except InputError as exc:
                reject(row, "input", str(exc))
                continue
            if refusal := licence_reason(kind, row, excluded, lookup_subsets):
                reject(row, *refusal)
                continue
            try:
                raw = teacher.complete(build_messages(kind, row), max_tokens=max_tokens)
            except BudgetExceeded as exc:
                log.info("batch %s: %s; stopping", kind, exc)
                state["stopped"] = "budget"
                return
            try:
                ex = parse_example(kind, row, raw)
            except FormatError as exc:
                reject(row, "format", str(exc))
                continue
            problems = run_checks(ex)
            if problems:
                reject(row, *problems[0])
                continue
            record = {
                "kind": kind,
                "example": example_to_json(ex),
                "messages": ex.to_chat(),
                "source_row": _source_row(kind, row),
                "teacher": teacher.model,
                "tools_sha256": TOOLS_SHA256,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            done.add(str(row["id"]))
            state["kept"] += 1
            inputs.append({**_source_row(kind, row), "outcome": "kept"})
            if kept_before + state["kept"] >= n:
                state["stopped"] = None
                return

    try:
        with examples_path.open("a", encoding="utf-8") as out:
            run(out)
    except BaseException as exc:
        state["stopped"] = "error"
        state["error"] = type(exc).__name__
        log.error("batch %s: stopped by %s", kind, state["error"])
        raise
    finally:
        budget_used = teacher.used_tokens - used_at_start
        counts = {
            "kept": state["kept"],
            "kept_before": kept_before,
            "skipped_done": state["skipped_done"],
            **{f"rejected_{r}": c for r, c in rejected.items()},
        }
        config = {
            "kind": kind,
            "n": n,
            "max_tokens": max_tokens,
            "excluded": sorted(excluded),
            "lookup_subsets": dict(sorted(lookup_subsets.items())),
            "teacher_model": teacher.model,
            "prompt": PROMPTS[kind],
            "tools_sha256": TOOLS_SHA256,
        }
        _keep_old_manifest(manifest_path)
        digest = write_manifest(
            manifest_path,
            "batch",
            inputs=inputs,
            counts=counts,
            config=config,
            teacher=teacher.model,
            budget_used=budget_used,
            extra={"stopped": state["stopped"], "error": state["error"]},
        )
    log.info(
        "batch %s: kept %d (%d before), rejected %s, %d tokens, stopped %s",
        kind,
        state["kept"],
        kept_before,
        rejected,
        budget_used,
        state["stopped"],
    )
    return {
        "kept": state["kept"],
        "kept_before": kept_before,
        "skipped_done": state["skipped_done"],
        "rejected": rejected,
        "budget_used": budget_used,
        "stopped": state["stopped"],
        "config_hash": digest,
        "examples": str(examples_path),
        "manifest": str(manifest_path),
    }
