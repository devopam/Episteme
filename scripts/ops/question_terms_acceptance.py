"""Acceptance run of question to MeSH terms (spec 2026-11-24, section 6; runbook 5.20.5).

For every question of the acceptance list, the script runs ``translate_question`` exactly as
the ``question_mcp`` server does: ``episteme_api.match_terms`` as ``episteme_api_reader``,
then, only when words stay unmatched, the MedCPT query encoder and
``episteme_api.suggest_terms`` with the document-side model revision. It runs no search.

Per question it prints the exact hit, the top-10 hit and the time. It writes a JSON report
to ``--out``. Exit 0 when the report is written; 2 on a usage or configuration error.

Usage (repository root; ``config`` reads ``.env``; ``PGDATABASE`` picks the database)::

    PGDATABASE=episteme .venv/Scripts/python.exe scripts/ops/question_terms_acceptance.py \
        --out D:/episteme/_runlogs/question_terms_acceptance.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from episteme import config

REPO = Path(__file__).resolve().parents[2]
DEFAULT_ACCEPTANCE = REPO / "tests" / "reference" / "data" / "question_terms_acceptance.json"
KINDS = ("acronym", "lay", "description")
TARGET_WARM_S = 5.0
TOP_K = 10

DEFINITIONS = {
    "exact_hit": "every expected UI is among the terms with basis 'exact' (match_terms)",
    "top10_hit": (
        "every expected UI is among the exact terms plus the first 10 MedCPT suggestions"
    ),
    "any_hit": "at least one expected UI is among the exact terms plus the first 10 suggestions",
    "times": (
        "seconds per translate_question call after one warm-up call (cold_call_s is the "
        "warm-up call); the encoder load is model_load_s"
    ),
}


class TimedDB:
    """A ``QuestionDB`` that times each call of the wrapped reader. ``take()`` returns the
    calls since the last ``take()`` as ``(name, seconds)``."""

    def __init__(self, inner: Any, clock: Callable[[], float] = time.perf_counter) -> None:
        self._inner = inner
        self._clock = clock
        self._calls: list[tuple[str, float]] = []

    def _timed(self, name: str, *args: Any) -> Any:
        t0 = self._clock()
        try:
            return getattr(self._inner, name)(*args)
        finally:
            self._calls.append((name, self._clock() - t0))

    def take(self) -> list[tuple[str, float]]:
        calls, self._calls = self._calls, []
        return calls

    def match_terms(self, question: str) -> list[dict[str, Any]]:
        return self._timed("match_terms", question)

    def suggest_terms(self, vector: str, model_revision: str) -> list[dict[str, Any]]:
        return self._timed("suggest_terms", vector, model_revision)

    def search_terms(self, descriptor_uis: list[str], sources: list[str] | None) -> Any:
        return self._timed("search_terms", descriptor_uis, sources)

    def literature_by_mesh(self, *args: Any) -> Any:
        return self._timed("literature_by_mesh", *args)

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    exact = sum(r["exact_hit"] for r in rows)
    top10 = sum(r["top10_hit"] for r in rows)
    return {
        "n": n,
        "exact_hit": exact,
        "top10_hit": top10,
        "any_hit": sum(r["any_hit"] for r in rows),
        "exact_share": exact / n if n else 0.0,
        "top10_share": top10 / n if n else 0.0,
    }


def evaluate(entry: dict[str, Any], result: dict[str, Any], top_k: int = TOP_K) -> dict[str, Any]:
    """Hits of one ``translate_question`` result against the expected UIs."""
    terms = result.get("terms", [])
    exact = [t["descriptor_ui"] for t in terms if t.get("basis") == "exact"]
    suggested = [t["descriptor_ui"] for t in terms if t.get("basis") != "exact"][:top_k]
    expected = set(entry["expected"])
    shown = set(exact) | set(suggested)
    return {
        "question": entry["question"],
        "kind": entry["kind"],
        "expected": list(entry["expected"]),
        "exact_uis": exact,
        "suggested_uis": suggested,
        "exact_hit": expected <= set(exact),
        "top10_hit": expected <= shown,
        "any_hit": bool(expected & shown),
        "notes": list(result.get("notes", [])),
        "mesh_release": result.get("mesh_release"),
    }


def run(
    entries: list[dict[str, Any]],
    service: Any,
    timed: TimedDB,
    *,
    clock: Callable[[], float] = time.perf_counter,
    top_k: int = TOP_K,
) -> dict[str, Any]:
    """Translate every question once after one warm-up call; return the report body."""
    cold_s = 0.0
    if entries:
        t0 = clock()
        service.translate_question(entries[0]["question"])
        cold_s = clock() - t0
        timed.take()

    questions = []
    for entry in entries:
        t0 = clock()
        result = service.translate_question(entry["question"])
        total = clock() - t0
        calls = timed.take()
        row = evaluate(entry, result, top_k)
        row["suggest_ran"] = any(name == "suggest_terms" for name, _ in calls)
        row["match_s"] = sum(s for name, s in calls if name == "match_terms")
        row["suggest_s"] = sum(s for name, s in calls if name == "suggest_terms")
        row["total_s"] = total
        questions.append(row)

    totals = [q["total_s"] for q in questions]
    by_kind = {
        kind: _counts([q for q in questions if q["kind"] == kind])
        for kind in KINDS
        if any(q["kind"] == kind for q in questions)
    }
    other_notes = sorted(
        {n for q in questions for n in q["notes"] if not n.startswith("unmatched:")}
    )
    return {
        "definitions": DEFINITIONS,
        "summary": {"all": _counts(questions), "by_kind": by_kind, "other_notes": other_notes},
        "times": {
            "cold_call_s": cold_s,
            "mean_s": sum(totals) / len(totals) if totals else 0.0,
            "p95_s": _p95(totals),
            "max_s": max(totals, default=0.0),
            "target_warm_s": TARGET_WARM_S,
            "within_target": sum(t < TARGET_WARM_S for t in totals),
        },
        "questions": questions,
    }


def build(log_dir: Path, clock: Callable[[], float]) -> tuple[Any, TimedDB, dict[str, Any]]:
    """The ``question_mcp`` service over the reader, with the query encoder loaded up front
    (its load time is reported apart from the call times)."""
    from episteme.reference.medcpt import TERM_MODELS, MedCPTEncoder
    from episteme.reference.question_mcp import QuestionService, ReaderDB, reader_conninfo

    settings = config.get_settings()
    conninfo = reader_conninfo(settings)  # ConfigError when the reader password is unset
    spec = TERM_MODELS["medcpt"]
    t0 = clock()
    encoder = MedCPTEncoder.query(spec)
    model_load_s = clock() - t0
    timed = TimedDB(ReaderDB(conninfo), clock)
    service = QuestionService(
        timed,
        log_dir,
        encoder_factory=lambda: encoder,
        model_revision=lambda: spec.doc_revision,
    )
    return service, timed, {"database": settings.pg_database, "model_load_s": model_load_s}


def _print(report: dict[str, Any]) -> None:
    for q in report["questions"]:
        print(
            f"{q['kind']:<11} exact={'Y' if q['exact_hit'] else 'N'} "
            f"top10={'Y' if q['top10_hit'] else 'N'} {q['total_s']:6.2f}s  {q['question']}"
        )
    s, t = report["summary"], report["times"]
    for label, c in [("all", s["all"]), *s["by_kind"].items()]:
        print(
            f"{label}: exact hit: {c['exact_hit']}/{c['n']}, "
            f"top-10 hit: {c['top10_hit']}/{c['n']}, any hit: {c['any_hit']}/{c['n']}"
        )
    print(
        f"times (warm): mean {t['mean_s']:.2f}s, p95 {t['p95_s']:.2f}s, max {t['max_s']:.2f}s, "
        f"{t['within_target']}/{s['all']['n']} under {t['target_warm_s']:.0f}s; "
        f"cold call {t['cold_call_s']:.2f}s"
    )
    for note in s["other_notes"]:
        print(f"note: {note}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="question_terms_acceptance", description=__doc__.splitlines()[0]
    )
    p.add_argument("--acceptance", type=Path, default=DEFAULT_ACCEPTANCE)
    p.add_argument("--out", type=Path, required=True, help="path of the JSON report")
    p.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="search log of this run (default: <out dir>/question_log; not the server's log)",
    )
    args = p.parse_args(argv)
    log_dir = args.log_dir or args.out.parent / "question_log"
    entries = json.loads(args.acceptance.read_text(encoding="utf-8"))

    clock = time.perf_counter
    try:
        # torch and transformers load here: a missing package is a setup error (exit 2).
        from episteme.reference.medcpt import TERM_MODELS

        service, timed, meta = build(log_dir, clock)
    except (config.ConfigError, ImportError) as exc:
        print(f"question_terms_acceptance: {exc}", file=sys.stderr)
        return 2
    try:
        report = run(entries, service, timed, clock=clock)
    finally:
        timed.close()
    spec = TERM_MODELS["medcpt"]
    report["meta"] = {
        **meta,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "acceptance": str(args.acceptance),
        "query_model": spec.query_model,
        "query_revision": spec.query_revision,
        "doc_revision": spec.doc_revision,
        "mesh_release": next(
            (q["mesh_release"] for q in report["questions"] if q["mesh_release"]), None
        ),
    }
    _print(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
