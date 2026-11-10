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
from typing import Literal

import psycopg

from episteme.bench.pbrer_cases import PbrerCase, load_case
from episteme.data.db.connection import connect_kwargs, dsn_from_settings
from episteme.harness.check import check_answer
from episteme.harness.loop import make_call_tool
from episteme.harness.pbrer import assemble_facts, gate_parts, section_headings, skeleton
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance

log = logging.getLogger(__name__)

Produce = Callable[[PbrerCase], tuple[dict[str, str], list[LookupResult]]]


def _share(expected: dict[str, list[str]], text: str) -> float:
    items = [x for xs in expected.values() for x in xs]
    if not items:
        return 1.0
    return sum(1 for x in items if x in text) / len(items)


def score(
    case: PbrerCase,
    draft: dict[str, str],
    results: list[LookupResult],
    *,
    region: Literal["ich", "eu"] = "ich",
) -> dict:
    """Score one draft.

    ``sections_ok``: no section finding (missing, duplicate, order).
    ``mah_markers_ok``: no MAH-marker finding. The two lists are the two parts of
    ``gate``, kept apart by ``gate_parts``.
    ``invented_values``: count of values from ``check_answer`` over the whole draft text.
    The case's own interval dates, substance name and the section headings of ``region``
    count as stated values, so a cross-reference such as "section 16" is not invented.
    ``facts_found`` / ``gaps_reported``: share of expected lines present in the draft
    (1.0 when none are expected)."""
    text = "\n".join(draft.values())
    sections, markers = gate_parts(draft, region=region)
    known = [case.start.isoformat(), case.end.isoformat(), case.substance_name]
    known += section_headings(region)
    return {
        "facts_found": _share(case.expected_facts, text),
        "gaps_reported": _share(case.expected_gaps, text),
        "invented_values": len(check_answer(text, results, known=known)),
        "mah_markers_ok": not markers,
        "sections_ok": not sections,
    }


def skeleton_producer(conn, *, tenant: str, caller: str) -> Produce:
    """Deterministic baseline. The substance comes from ``case.substance_key``, not from
    a new name lookup. A key that no longer exists gives a resolution gap."""
    call = make_call_tool(conn, tenant=tenant, caller=caller)

    def substance_for(case: PbrerCase) -> LookupResult:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT unii, preferred_name FROM episteme.substance WHERE substance_key = %s",
                (case.substance_key,),
            )
            row = cur.fetchone()
        if row is None:
            return LookupResult(
                (), Coverage(0, missing=("substance",), notes=("substance not resolved",))
            )
        unii, name = row
        claim = Claim(
            text=name or case.substance_name,
            value=str(case.substance_key),
            method="fetched",
            provenance=Provenance("substance", None, None, f"unii:{unii}" if unii else None, None),
            confidence=1.0,
            confidence_basis="substance_key of the benchmark case",
        )
        return LookupResult((claim,), Coverage(1))

    def produce(case: PbrerCase):
        substance = substance_for(case)
        facts = assemble_facts(call, substance, case.start, case.end, tenant=tenant, caller=caller)
        return skeleton(facts), [substance, *facts.sections.values()]

    return produce


def connect(settings) -> psycopg.Connection:
    """A read-only autocommit connection through the project helpers.

    ``dsn_from_settings`` runs the DB-mode guard before any connection attempt, and
    ``connect_kwargs`` adds the ``application_name`` tag the orphan sweep reads.
    Autocommit keeps each lookup in its own short statement, so the run holds no
    transaction that blocks loader partition DDL. ``default_transaction_read_only`` makes
    every statement read-only, whatever the DB mode."""
    kwargs = connect_kwargs(settings)
    kwargs["options"] = "-c default_transaction_read_only=on"
    return psycopg.connect(dsn_from_settings(), autocommit=True, **kwargs)


def main(argv: Sequence[str] | None = None, *, produce: Produce | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tenant", default=None, help="audit tenant (default: config)")
    ap.add_argument("--caller", default=None, help="audit caller (default: config)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cases = [load_case(p) for p in sorted(args.cases.glob("*.json"))]
    reviewed = [c for c in cases if c.reviewed_by_owner]
    skipped = len(cases) - len(reviewed)
    scores: dict[str, dict] = {}
    if reviewed:
        if produce is not None:
            scores = {c.case_id: score(c, *produce(c)) for c in reviewed}
        else:
            from episteme.config import get_settings

            settings = get_settings()
            with connect(settings) as conn:
                run = skeleton_producer(
                    conn,
                    tenant=args.tenant or settings.bench_tenant,
                    caller=args.caller or settings.bench_caller,
                )
                scores = {c.case_id: score(c, *run(c)) for c in reviewed}
    args.out.mkdir(parents=True, exist_ok=True)
    payload = {"cases": scores, "skipped_unreviewed": skipped}
    (args.out / "scores.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{len(scores)} run, {skipped} skipped (unreviewed); wrote {args.out / 'scores.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
