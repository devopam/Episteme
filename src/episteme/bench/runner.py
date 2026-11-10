"""Score PBRER drafts against reviewed benchmark cases.

``python -m episteme.bench.runner --cases benchmarks/pbrer --out <dir>`` runs the cases
with ``reviewed_by_owner: true`` and writes ``<dir>/scores.json``. The default producer
is the deterministic skeleton of the PBRER harness (no model call): it is the baseline a
model draft has to beat.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path

from episteme.bench.pbrer_cases import PbrerCase, load_case
from episteme.harness.check import check_answer
from episteme.harness.pbrer import gate
from episteme.reference.claims import LookupResult
from episteme.reference.rules import MAH_MARKER

log = logging.getLogger(__name__)

Produce = Callable[[PbrerCase], tuple[dict[str, str], list[LookupResult]]]


def _share(expected: dict[str, list[str]], text: str) -> float:
    items = [x for xs in expected.values() for x in xs]
    if not items:
        return 1.0
    return sum(1 for x in items if x in text) / len(items)


def score(case: PbrerCase, draft: dict[str, str], results: list[LookupResult]) -> dict:
    """Score one draft.

    ``sections_ok``: the gate has no section finding (missing, duplicate, order).
    ``mah_markers_ok``: the gate has no MAH-marker finding.
    ``invented_values``: count of values from ``check_answer`` over the whole draft text.
    ``facts_found`` / ``gaps_reported``: share of expected lines present in the draft
    (1.0 when none are expected)."""
    findings = gate(draft)
    marker = [f for f in findings if MAH_MARKER in f.message]
    text = "\n".join(draft.values())
    return {
        "facts_found": _share(case.expected_facts, text),
        "gaps_reported": _share(case.expected_gaps, text),
        "invented_values": len(check_answer(text, results)),
        "mah_markers_ok": not marker,
        "sections_ok": len(findings) == len(marker),
    }


def skeleton_producer(conn, *, tenant: str, caller: str) -> Produce:
    from episteme.harness.loop import make_call_tool
    from episteme.harness.pbrer import assemble_facts, skeleton
    from episteme.reference.resolve import resolve_entity

    call = make_call_tool(conn, tenant=tenant, caller=caller)

    def produce(case: PbrerCase):
        substance = resolve_entity(conn, case.substance_name, tenant=tenant, caller=caller)
        facts = assemble_facts(call, substance, case.start, case.end, tenant=tenant, caller=caller)
        results = [substance, *facts.sections.values()]
        return skeleton(facts), results

    return produce


def main(argv: Sequence[str] | None = None, *, produce: Produce | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    cases = [load_case(p) for p in sorted(args.cases.glob("*.json"))]
    reviewed = [c for c in cases if c.reviewed_by_owner]
    skipped = len(cases) - len(reviewed)
    scores: dict[str, dict] = {}
    if reviewed:
        if produce is not None:
            scores = {c.case_id: score(c, *produce(c)) for c in reviewed}
        else:
            import psycopg

            from episteme.config import get_settings

            with psycopg.connect(get_settings().pg_dsn()) as conn:
                run = skeleton_producer(conn, tenant="bench", caller="bench-runner")
                scores = {c.case_id: score(c, *run(c)) for c in reviewed}
    args.out.mkdir(parents=True, exist_ok=True)
    payload = {"cases": scores, "skipped_unreviewed": skipped}
    (args.out / "scores.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    log.info("scored %d cases; skipped %d unreviewed", len(scores), skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
