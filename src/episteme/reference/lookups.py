"""PBRER v1 lookups: exact facts with provenance and coverage. Read-only.

Only SELECT statements run here (``to_regclass`` and ``reg_release`` reads included).
Each function answers from the latest release of each source it reads. A source whose
relation is missing, or that has no ``is_latest`` release, is named in
``coverage.missing``; the other sources still answer. No function falls back to an
older release, and no function raises for a missing or empty schema.

Substance scope: the input's whole moiety family (``_scope.substance_scope``): the moiety
and every salt or ester whose moiety it is. Approvals and actions match the family's
names, labels its UNIIs, trials its keys; the FAERS signal tables count the family as
its moiety key.

``tenant`` and ``caller`` are accepted for a stable signature; v1 does not use them.
"""

from __future__ import annotations

from datetime import date

import psycopg

from episteme.reference import _api
from episteme.reference._scope import (
    SCHEMA,
    SubstanceScope,
    latest_release,
    relation_exists,
    source_ready,
    substance_scope,
)
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance

__all__ = [
    "PBRER_DATASETS",
    "SCHEMA",
    "SubstanceScope",
    "approval_status",
    "current_label",
    "faers_counts",
    "label_section_text",
    "latest_release",
    "relation_exists",
    "safety_actions",
    "source_ready",
    "substance_scope",
    "trials",
]
# approval_status, safety_actions, current_label, label_section_text and trials run in
# SQL (migration 0015). The regulatory pair uses reg_lookup, its payload fields in
# reg_fields; these two tuples name the same datasets in the same order.
APPROVAL_DATASETS = ("fda_drugsfda", "ema_medicines", "ema_article57", "ema_outside_eu")
SAFETY_DATASETS = ("fda_enforcement", "ema_referrals", "ema_dhpc", "ema_psusa")
# The names that coverage.missing uses, per function.
PBRER_DATASETS: dict[str, tuple[str, ...]] = {
    "approval_status": APPROVAL_DATASETS,
    "safety_actions": SAFETY_DATASETS,
    "current_label": ("dailymed",),
    "trials": ("aact",),
    "faers_counts": ("faers",),
    "literature": ("article_mesh",),
}

# The note text of current_label (the SQL function returns the same text).
LABEL_HISTORY_GAP = "change history not determinable"
FAERS_LABEL = "US public spontaneous reports (FAERS)"
FAERS_QUARTER_RULE = "quarters that overlap the interval are counted whole"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _no_substance(function: str, substance_key: int) -> LookupResult:
    note = f"substance {substance_key} not found"
    return LookupResult(
        claims=(),
        coverage=Coverage(matched=0, missing=PBRER_DATASETS[function], notes=(note,)),
    )


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def approval_status(
    conn: psycopg.Connection, substance_key: int, *, tenant: str, caller: str
) -> LookupResult:
    """Marketing records per jurisdiction: Drugs@FDA, EMA medicines, Article 57 and
    EMA opinions outside the EU, from the latest release of each. Runs the SQL function
    ``episteme_api_internal.approval_status`` (migration 0015)."""
    return _api.rows_to_result(_api.call(conn, "approval_status", int(substance_key)))


def safety_actions(
    conn: psycopg.Connection,
    substance_key: int,
    start: date,
    end: date,
    *,
    tenant: str,
    caller: str,
) -> LookupResult:
    """FDA recalls, EMA referrals, DHPCs and PSUSA outcomes whose ``event_date`` is in
    ``[start, end]``. Records with no date are counted in a note. Runs the SQL function
    ``episteme_api_internal.safety_actions`` (migration 0015)."""
    return _api.rows_to_result(_api.call(conn, "safety_actions", int(substance_key), start, end))


def current_label(
    conn: psycopg.Connection, substance_key: int, *, tenant: str, caller: str
) -> LookupResult:
    """The sections of the current labels (DailyMed and the EU labels of ``ema_epar``)
    whose ``label_substance.unii`` is the UNII of a member of the substance's moiety
    family. The claim value is the section title; the release is the SPL version
    (``articles.book_meta``). The extract keeps only the newest version per label, so
    the change history is always a gap. Runs the SQL function
    ``episteme_api_internal.current_label`` (migration 0015)."""
    return _api.rows_to_result(_api.call(conn, "current_label", int(substance_key)))


def label_section_text(
    conn: psycopg.Connection, article_id: str, section: str, *, tenant: str, caller: str
) -> LookupResult:
    """The text of one section of one label: ``section`` is the LOINC code or the
    section title (case-insensitive). One claim per matching section; the value is the
    slice of the stored body text. Runs the SQL function
    ``episteme_api_internal.label_section_text`` (migration 0015)."""
    return _api.rows_to_result(_api.call(conn, "label_section_text", article_id, section))


def trials(
    conn: psycopg.Connection, substance_key: int, *, tenant: str, caller: str
) -> LookupResult:
    """AACT studies with an intervention matched to a member of the substance's moiety
    family (``intervention_substance.substance_key``), with status, phase and whether results
    are posted (``studies.results_first_posted_date``). The tables are read from the
    schema of the latest ``aact`` release (``aact_r<release_id>``). Runs the SQL function
    ``episteme_api_internal.trials`` (migration 0015)."""
    return _api.rows_to_result(_api.call(conn, "trials", int(substance_key)))


# Quarters that overlap the interval and have no faers_quarter_totals row (not loaded).
_FAERS_GAPS_SQL = """
SELECT q::date FROM generate_series(
  date_trunc('quarter', %(start)s::date::timestamp), %(end)s::date::timestamp,
  interval '3 months') AS q
WHERE NOT EXISTS (SELECT 1 FROM episteme.faers_quarter_totals t WHERE t.quarter = q::date)
ORDER BY 1
"""


def _quarter_label(quarter: date) -> str:
    return f"{quarter.year}Q{(quarter.month - 1) // 3 + 1}"


_FAERS_SQL = """
SELECT m.quarter, m.reports_suspect, m.reports_any, t.reports
FROM episteme.faers_moiety_quarter m
LEFT JOIN episteme.faers_quarter_totals t ON t.quarter = m.quarter
WHERE m.substance_key = %(moiety)s
  AND m.quarter >= date_trunc('quarter', %(start)s::date)::date AND m.quarter <= %(end)s
ORDER BY m.quarter
"""


def faers_counts(
    conn: psycopg.Connection,
    substance_key: int,
    start: date,
    end: date,
    *,
    tenant: str,
    caller: str,
) -> LookupResult:
    """FAERS reports per quarter for the substance's moiety: as suspect, in any role, and
    all reports received in the quarter. Quarters that overlap ``[start, end]`` count
    whole. Public US data, never the MAH's own case counts."""
    scope = substance_scope(conn, substance_key)
    if scope is None:
        return _no_substance("faers_counts", substance_key)
    notes = (FAERS_QUARTER_RULE,)
    tables = ("faers_moiety_quarter", "faers_quarter_totals")
    if not source_ready(conn, tables, "faers_quarter_totals"):
        return LookupResult((), Coverage(matched=0, missing=("faers",), notes=notes))
    with conn.cursor() as cur:
        params = {"moiety": scope.moiety_key, "start": start, "end": end}
        cur.execute(_FAERS_SQL, params)
        rows = cur.fetchall()
        cur.execute(_FAERS_GAPS_SQL, params)
        gaps = [_quarter_label(r[0]) for r in cur.fetchall()]
    if gaps:
        notes += (f"no FAERS data for quarters: {', '.join(gaps)}",)
    claims = []
    for quarter, suspect, any_role, total in rows:
        label = _quarter_label(quarter)
        of_total = f"of {total} reports received" if total is not None else "total not available"
        claims.append(
            Claim(
                text=(
                    f"{label}: {suspect} reports name the substance as suspect, {any_role} "
                    f"in any role, {of_total} ({FAERS_LABEL})"
                ),
                value=str(suspect),
                method="computed",
                provenance=Provenance(
                    source="openfda_faers",
                    release=None,
                    source_file=None,
                    record_key=f"faers_moiety_quarter:{scope.moiety_key}:{quarter.isoformat()}",
                    jurisdiction="US",
                ),
                confidence=1.0,
                confidence_basis="exact count of stored FAERS reports per moiety and quarter",
            )
        )
    return LookupResult(tuple(claims), Coverage(matched=len(claims), notes=notes))
