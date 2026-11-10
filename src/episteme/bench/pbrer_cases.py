"""PSUSA-derived PBRER benchmark cases.

A case pairs one EMA PSUSA record (the latest ``ema_psusa`` release) with the facts and
gaps the Task 4 lookups return for its substance in the 12 months before the record's
``event_date``. The owner reviews each case before it counts (``reviewed_by_owner``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

import psycopg

from episteme.harness.loop import make_call_tool
from episteme.harness.pbrer import GAP_PREFIX, NOTE_PREFIX, assemble_facts
from episteme.reference._scope import latest_release, relation_exists, substance_scope
from episteme.reference.claims import LookupResult
from episteme.reference.resolve import resolve_entity

log = logging.getLogger(__name__)

DATASET = "ema_psusa"
_RECORDS_SQL = """
SELECT record_key, substance_names, event_date
FROM episteme.reg_record
WHERE dataset = %(ds)s AND release_id = %(rel)s AND event_date IS NOT NULL
ORDER BY event_date DESC, record_key
"""


@dataclass(frozen=True)
class PbrerCase:
    case_id: str
    substance_key: int
    substance_name: str
    start: date
    end: date
    psusa_record_key: str
    expected_facts: dict[str, list[str]] = field(default_factory=dict)
    expected_gaps: dict[str, list[str]] = field(default_factory=dict)
    reviewed_by_owner: bool = False

    def to_json(self) -> dict:
        d = asdict(self)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict) -> PbrerCase:
        return cls(
            case_id=d["case_id"],
            substance_key=int(d["substance_key"]),
            substance_name=d["substance_name"],
            start=date.fromisoformat(d["start"]),
            end=date.fromisoformat(d["end"]),
            psusa_record_key=d["psusa_record_key"],
            expected_facts={k: list(v) for k, v in d.get("expected_facts", {}).items()},
            expected_gaps={k: list(v) for k, v in d.get("expected_gaps", {}).items()},
            reviewed_by_owner=bool(d.get("reviewed_by_owner", False)),
        )


def write_case(case: PbrerCase, directory: Path) -> Path:
    """Write ``<case_id>.json`` with sorted keys and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{case.case_id}.json"
    path.write_text(
        json.dumps(case.to_json(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def load_case(path: Path) -> PbrerCase:
    return PbrerCase.from_json(json.loads(Path(path).read_text("utf-8")))


def interval_for(event_date: date) -> tuple[date, date]:
    """The reporting interval of a PSUSA record: the 12 months before ``event_date``.

    ``start`` is ``event_date`` one year earlier (29 February maps to 28 February);
    ``end`` is ``event_date`` minus one day. Both ends are inclusive."""
    try:
        start = event_date.replace(year=event_date.year - 1)
    except ValueError:
        start = event_date.replace(year=event_date.year - 1, day=28)
    return start, event_date - timedelta(days=1)


def _slug(record_key: str) -> str:
    return "psusa-" + re.sub(r"[^A-Za-z0-9]+", "-", record_key).strip("-")


def _single_substance(conn, names, *, tenant: str, caller: str) -> LookupResult | None:
    """The ``resolve_entity`` result when every name resolves and all names lead to one
    moiety family; otherwise ``None`` (none, ambiguous or several substances)."""
    first: LookupResult | None = None
    moieties: set[int] = set()
    for name in names or ():
        res = resolve_entity(conn, name, tenant=tenant, caller=caller)
        if not res.claims:
            return None
        head = res.claims[0]
        for c in (head, *head.alternatives):
            scope = substance_scope(conn, int(c.value))
            moieties.add(scope.moiety_key if scope else int(c.value))
        first = first or res
    return first if first is not None and len(moieties) == 1 else None


def _lines(r: LookupResult) -> tuple[list[str], list[str]]:
    facts = [c.text for c in r.claims]
    gaps = [GAP_PREFIX + g for g in r.coverage.missing] + [
        NOTE_PREFIX + n for n in r.coverage.notes
    ]
    return facts, gaps


def build_cases(
    conn: psycopg.Connection, limit: int, *, tenant: str, caller: str
) -> list[PbrerCase]:
    """Cases from the newest records of the latest ``ema_psusa`` release.

    A record is used when it has an ``event_date`` and every substance name resolves to
    one moiety family (via ``resolve_entity`` and the Task 4 family scope). Others are
    skipped; the counts are logged. Expected facts and gaps come from the same
    lookups as the PBRER harness (``assemble_facts``), at build time."""
    release = latest_release(conn, DATASET)
    if release is None or not relation_exists(conn, "episteme.reg_record"):
        log.warning("no latest %s release; no cases", DATASET)
        return []
    with conn.cursor() as cur:
        cur.execute(_RECORDS_SQL, {"ds": DATASET, "rel": release})
        records = cur.fetchall()
    call = make_call_tool(conn, tenant=tenant, caller=caller)
    cases: list[PbrerCase] = []
    skipped = 0
    for record_key, names, event_date in records:
        if len(cases) >= limit:
            break
        substance = _single_substance(conn, names, tenant=tenant, caller=caller)
        if substance is None:
            skipped += 1
            continue
        start, end = interval_for(event_date)
        facts = assemble_facts(call, substance, start, end, tenant=tenant, caller=caller)
        expected_facts: dict[str, list[str]] = {}
        expected_gaps: dict[str, list[str]] = {}
        for sid, result in facts.sections.items():
            f, g = _lines(result)
            if f:
                expected_facts[sid] = f
            if g:
                expected_gaps[sid] = g
        head = substance.claims[0]
        cases.append(
            PbrerCase(
                case_id=_slug(record_key),
                substance_key=int(head.value),
                substance_name=head.text,
                start=start,
                end=end,
                psusa_record_key=record_key,
                expected_facts=expected_facts,
                expected_gaps=expected_gaps,
            )
        )
    log.info("built %d cases from %s %s; skipped %d", len(cases), DATASET, release, skipped)
    return cases
