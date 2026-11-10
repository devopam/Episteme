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
  ``source = 'pbrer_skeleton'`` and ``subset = 'commercial'``, and carries the tool
  results the skeleton was built from. Their claim sources pass the same check as a
  ``tool_draft``.

A ``tool_draft`` or ``rule_check`` row with no claim in its tool results has nothing to
verify and is refused.

Ledger and restart: ``requests.jsonl`` is append-only. Right after each answered request
(before any check) a ``paid`` line with the row id and its tokens is written and forced
to disk; the outcome (``kept`` or ``rejected`` with its reason) follows as an ``outcome``
line. A request that raises gets an ``error`` line with its estimated tokens. A hard kill
loses at most the request in flight.

``examples.jsonl`` and the ledger are never truncated. A re-run skips every row that was
kept or paid for (no new request), retries rows whose request failed, and appends; ``n``
is the target count of the whole file. The tokens in the ledger count against the budget
of the re-run. An out_dir that holds another kind is refused. The manifest is written on
every exit, also on an error (``stopped = 'error'``); the manifest of an earlier run is
kept as ``manifest.run-<k>.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
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
LEDGER_NAME = "requests.jsonl"
# Hash of the harness tool schema the tool-call examples are written in.
TOOLS_SHA256 = hashlib.sha256(
    json.dumps(TOOLS, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def _items(value: object) -> list:
    return value if isinstance(value, list) else []


def _claim_sources(claim: object) -> set:
    if not isinstance(claim, dict):
        return set()
    prov = claim.get("provenance")
    found = {prov.get("source")} if isinstance(prov, dict) else set()
    for alt in _items(claim.get("alternatives")):
        found |= _claim_sources(alt)
    return found


def lookup_sources(row: dict) -> list[str]:
    """Sources named in the provenance of every claim of the row's tool results.
    Malformed parts give no source (``check_input`` rejects such rows)."""
    found: set = set()
    for result in _items(row.get("tool_results")):
        if isinstance(result, dict):
            for claim in _items(result.get("claims")):
                found |= _claim_sources(claim)
    return sorted(found, key=str)


def _lookup_reason(row: dict, lookup_subsets: dict[str, str]) -> tuple[str, str] | None:
    """The licence of lookup facts: every claim source of the row's tool results must be
    ``commercial``; a ``pending`` source holds the row."""
    sources = lookup_sources(row)
    if not sources:
        return "licence", "the tool results hold no claim, so no source to verify"
    bad = [s for s in sources if lookup_subsets.get(s) not in ("commercial", "pending")]
    if bad:
        return "licence", f"lookup sources not commercial: {bad}"
    pending = [s for s in sources if lookup_subsets.get(s) == "pending"]
    if pending:
        return "licence_pending", f"lookup sources pending an owner decision: {pending}"
    return None


def licence_reason(
    kind: str, row: dict, excluded: tuple[str, ...], lookup_subsets: dict[str, str]
) -> tuple[str, str] | None:
    """``(reason, detail)`` when the row may not reach the teacher, else None."""
    if kind == "tool_draft":
        return _lookup_reason(row, lookup_subsets)
    source, subset = row.get("source"), row.get("subset")
    if kind == "rule_check":
        if source != RULE_CHECK_SOURCE or subset != "commercial":
            return "licence", f"rule_check rows need source {RULE_CHECK_SOURCE!r}, commercial"
        return _lookup_reason(row, lookup_subsets)
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
    if kind in ("tool_draft", "rule_check"):
        ident["lookup_sources"] = lookup_sources(row)
    return ident


def _jsonl_records(path: Path) -> list[dict]:
    """The records of an append-only JSONL file. A line that is not a whole JSON object
    stops the run: the file is never truncated or rewritten here."""
    if not path.exists():
        return []
    records: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        if not isinstance(rec, dict):
            raise ValueError(
                f"{path}: line {i} is not a complete record; move or repair the file"
                " before a re-run"
            )
        records.append(rec)
    return records


def _check_kind(path: Path, records: list[dict], kind: str) -> None:
    other = sorted({str(r.get("kind")) for r in records if r.get("kind") != kind})
    if other:
        raise ValueError(
            f"{path} holds {', '.join(other)} records, not {kind!r}; use another out_dir"
        )


def _prior_state(examples_path: Path, ledger_path: Path, kind: str) -> tuple[set, set, int]:
    """Kept ids, paid ids (a request answered; kept, rejected or cut off before its
    outcome) and the tokens of all earlier requests, failed ones included."""
    examples = _jsonl_records(examples_path)
    ledger = _jsonl_records(ledger_path)
    _check_kind(examples_path, examples, kind)
    _check_kind(ledger_path, ledger, kind)
    try:
        kept = {str(r["source_row"]["id"]) for r in examples}
    except (KeyError, TypeError):
        raise ValueError(f"{examples_path}: a record has no source_row id") from None
    paid = {str(r.get("id")) for r in ledger if r.get("event") == "paid"}
    tokens = sum(int(r.get("tokens") or 0) for r in ledger if r.get("event") in ("paid", "error"))
    return kept, paid, tokens


def _append(f, record: dict) -> None:
    """Append one line and force it to disk before going on."""
    f.write(json.dumps(record, ensure_ascii=False) + "\n")
    f.flush()
    os.fsync(f.fileno())


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
    ``out_dir/examples.jsonl``, every paid request to ``out_dir/requests.jsonl`` and
    writes ``out_dir/manifest.json`` (kind ``batch``). ``stopped`` is None when ``n`` is
    reached, ``exhausted`` when the rows run out, ``budget`` at the budget cap, ``error``
    when an exception ends the run (the manifest is written if it can be, then the
    original exception propagates). The tokens of earlier runs in ``out_dir`` count
    against the teacher's budget. ``excluded`` defaults to
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
    ledger_path = out_dir / LEDGER_NAME
    manifest_path = out_dir / "manifest.json"

    kept_ids, paid_ids, tokens_before = _prior_state(examples_path, ledger_path, kind)
    skip = kept_ids | paid_ids
    kept_before = len(kept_ids)
    # Earlier runs in this out_dir spent these tokens: they count against the budget.
    teacher.used_tokens += tokens_before
    used_at_start = teacher.used_tokens
    rejected = dict.fromkeys(REJECT_REASONS, 0)
    inputs: list[dict] = []
    state: dict = {"kept": 0, "skipped_done": 0, "stopped": "exhausted", "error": None}

    def reject(row: dict, reason: str, detail: str, ledger=None) -> None:
        rejected[reason] += 1
        inputs.append({**_source_row(kind, row), "outcome": reason})
        if ledger is not None:
            _append(
                ledger,
                {
                    "event": "outcome",
                    "id": str(row["id"]),
                    "kind": kind,
                    "outcome": "rejected",
                    "reason": reason,
                },
            )
        log.info("batch %s: row %r rejected (%s): %s", kind, row.get("id"), reason, detail)

    def run(out, ledger) -> None:
        if kept_before >= n:
            state["stopped"] = None
            return
        for row in passages:
            if row.get("id") is not None and str(row["id"]) in skip:
                state["skipped_done"] += 1
                continue
            try:
                check_input(kind, row)
            except InputError as exc:
                reject(row, "input", str(exc))
                continue
            except FormatError as exc:
                reject(row, "format", str(exc))
                continue
            if refusal := licence_reason(kind, row, excluded, lookup_subsets):
                reject(row, *refusal)
                continue
            row_id = str(row["id"])
            before = teacher.used_tokens
            try:
                raw = teacher.complete(build_messages(kind, row), max_tokens=max_tokens)
            except BudgetExceeded as exc:
                log.info("batch %s: %s; stopping", kind, exc)
                state["stopped"] = "budget"
                return
            except BaseException as exc:
                try:
                    _append(
                        ledger,
                        {
                            "event": "error",
                            "id": row_id,
                            "kind": kind,
                            "tokens": teacher.used_tokens - before,
                            "error": type(exc).__name__,
                        },
                    )
                except Exception:
                    log.exception("batch %s: ledger line for row %r not written", kind, row_id)
                raise
            # Paid: on disk before any check runs.
            _append(
                ledger,
                {
                    "event": "paid",
                    "id": row_id,
                    "kind": kind,
                    "tokens": teacher.used_tokens - before,
                },
            )
            try:
                ex = parse_example(kind, row, raw)
            except FormatError as exc:
                reject(row, "format", str(exc), ledger)
                continue
            problems = run_checks(ex)
            if problems:
                reject(row, *problems[0], ledger)
                continue
            record = {
                "kind": kind,
                "example": example_to_json(ex),
                "messages": ex.to_chat(),
                "source_row": _source_row(kind, row),
                "teacher": teacher.model,
                "tools_sha256": TOOLS_SHA256,
            }
            _append(out, record)
            _append(
                ledger,
                {"event": "outcome", "id": row_id, "kind": kind, "outcome": "kept"},
            )
            skip.add(row_id)
            state["kept"] += 1
            inputs.append({**_source_row(kind, row), "outcome": "kept"})
            if kept_before + state["kept"] >= n:
                state["stopped"] = None
                return

    def finish() -> str:
        counts = {
            "kept": state["kept"],
            "kept_before": kept_before,
            "skipped_done": state["skipped_done"],
            "tokens_before": tokens_before,
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
        return write_manifest(
            manifest_path,
            "batch",
            inputs=inputs,
            counts=counts,
            config=config,
            teacher=teacher.model,
            budget_used=teacher.used_tokens - used_at_start,
            extra={"stopped": state["stopped"], "error": state["error"]},
        )

    try:
        with (
            examples_path.open("a", encoding="utf-8") as out,
            ledger_path.open("a", encoding="utf-8") as ledger,
        ):
            run(out, ledger)
    except BaseException as exc:
        state["stopped"] = "error"
        state["error"] = type(exc).__name__
        log.error("batch %s: stopped by %s", kind, state["error"])
        try:
            finish()
        except Exception:
            # The original exception matters more; the ledger still holds the spend.
            log.exception("batch %s: manifest not written after %s", kind, state["error"])
        raise
    digest = finish()
    budget_used = teacher.used_tokens - used_at_start
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
        "tokens_before": tokens_before,
        "rejected": rejected,
        "budget_used": budget_used,
        "stopped": state["stopped"],
        "config_hash": digest,
        "examples": str(examples_path),
        "manifest": str(manifest_path),
    }
