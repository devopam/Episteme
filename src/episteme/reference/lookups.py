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
from typing import Any

import psycopg
from psycopg import sql

from episteme.reference._scope import (
    SCHEMA,
    SubstanceScope,
    latest_release,
    relation_exists,
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
    "latest_release",
    "relation_exists",
    "safety_actions",
    "substance_scope",
    "trials",
]
# The schema of the AACT views of the latest release (load_aact.VIEW_SCHEMA). A module
# constant so that a test can point it at its own schema.
AACT_SCHEMA = "aact"

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

LABEL_HISTORY_GAP = "change history not determinable"
FAERS_LABEL = "(US public spontaneous reports (FAERS))"
FAERS_QUARTER_RULE = "quarters that overlap the interval are counted whole"

# Payload fields shown in a claim, read with .get(): a missing key is skipped. The first
# field of each "status" tuple is the claim value. Article 57 names are checked in
# parsers.ARTICLE57_COLUMNS; the others are NOT checked against a downloaded file.
_STATUS_FIELDS: dict[str, tuple[str, ...]] = {
    "fda_drugsfda": ("products.marketing_status",),
    "ema_medicines": ("medicine_status",),
    "ema_outside_eu": ("opinion_status", "medicine_status"),
    "fda_enforcement": ("classification", "status"),
    "ema_referrals": ("current_status", "status"),
    "ema_psusa": ("regulatory_outcome", "outcome"),
}
_DETAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "fda_drugsfda": ("sponsor_name", "products.brand_name"),
    "ema_medicines": ("name_of_medicine", "marketing_authorisation_developer_applicant_holder"),
    "ema_article57": (
        "product_name",
        "product_authorisation_country",
        "marketing_authorisation_holder",
    ),
    "ema_outside_eu": ("name_of_medicine",),
    "fda_enforcement": ("recall_number", "reason_for_recall"),
    "ema_referrals": ("referral_name", "reference_number"),
    "ema_dhpc": ("name_of_medicine", "dhpc_type"),
    "ema_psusa": ("procedure_number",),
}

_REG_SQL = """
SELECT record_key, jurisdiction, event_date, payload, source_file
FROM episteme.reg_record
WHERE dataset = %(ds)s AND release_id = %(rel)s AND substance_names && %(names)s::text[]
  {interval}
ORDER BY event_date DESC NULLS LAST, record_key
"""
_INTERVAL = "AND event_date BETWEEN %(start)s AND %(end)s"
_UNDATED_SQL = """
SELECT count(*) FROM episteme.reg_record
WHERE dataset = %(ds)s AND release_id = %(rel)s AND substance_names && %(names)s::text[]
  AND event_date IS NULL
"""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _no_substance(function: str, substance_key: int) -> LookupResult:
    note = f"substance {substance_key} not found"
    return LookupResult(
        claims=(),
        coverage=Coverage(matched=0, missing=PBRER_DATASETS[function], notes=(note,)),
    )


def _get(payload: Any, path: str) -> list[str]:
    """Scalar values at a dotted ``path``; lists are walked."""
    values = [payload]
    for part in path.split("."):
        nxt: list[Any] = []
        for v in values:
            items = v if isinstance(v, list) else [v]
            nxt.extend(i.get(part) for i in items if isinstance(i, dict))
        values = [v for v in nxt if v is not None]
    flat: list[str] = []
    for v in values:
        for item in v if isinstance(v, list) else [v]:
            s = str(item).strip()
            if s and s not in flat:
                flat.append(s)
    return flat


def _reg_claim(dataset: str, release: str, row: tuple) -> Claim:
    record_key, jurisdiction, event_date, payload, source_file = row
    status = next((vals for f in _STATUS_FIELDS.get(dataset, ()) if (vals := _get(payload, f))), [])
    parts = [f"{dataset} {record_key}"]
    for field in _DETAIL_FIELDS.get(dataset, ()):
        if vals := _get(payload, field):
            parts.append(f"{field.split('.')[-1]}: {'; '.join(vals)}")
    if status:
        parts.append(f"status: {'; '.join(status)}")
    if event_date is not None:
        parts.append(f"date: {event_date.isoformat()}")
    if jurisdiction:
        parts.append(f"jurisdiction: {jurisdiction}")
    return Claim(
        text=", ".join(parts),
        value="; ".join(status) if status else None,
        method="fetched",
        provenance=Provenance(
            source=dataset,
            release=release,
            source_file=source_file,
            record_key=record_key,
            jurisdiction=jurisdiction,
        ),
        confidence=1.0,
        confidence_basis="exact normalised substance name in the latest release",
    )


def _reg_lookup(
    conn: psycopg.Connection,
    function: str,
    substance_key: int,
    interval: tuple[date, date] | None = None,
) -> LookupResult:
    scope = substance_scope(conn, substance_key)
    if scope is None:
        return _no_substance(function, substance_key)
    has_table = relation_exists(conn, f"{SCHEMA}.reg_record")
    claims: list[Claim] = []
    missing: list[str] = []
    notes: list[str] = []
    for ds in PBRER_DATASETS[function]:
        release = latest_release(conn, ds) if has_table else None
        if release is None:
            missing.append(ds)
            continue
        if not scope.names:
            continue
        params: dict[str, Any] = {"ds": ds, "rel": release, "names": list(scope.names)}
        query = _REG_SQL.format(interval=_INTERVAL if interval else "")
        with conn.cursor() as cur:
            if interval:
                params["start"], params["end"] = interval
                cur.execute(_UNDATED_SQL, params)
                undated = cur.fetchone()[0]
                if undated:
                    notes.append(f"{ds}: {undated} records with no date not checked")
            cur.execute(query, params)
            claims.extend(_reg_claim(ds, release, r) for r in cur.fetchall())
    if not scope.names:
        notes.append(f"substance {substance_key} has no name to match")
    return LookupResult(
        claims=tuple(claims),
        coverage=Coverage(matched=len(claims), missing=tuple(missing), notes=tuple(notes)),
    )


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def approval_status(
    conn: psycopg.Connection, substance_key: int, *, tenant: str, caller: str
) -> LookupResult:
    """Marketing records per jurisdiction: Drugs@FDA, EMA medicines, Article 57 and
    EMA opinions outside the EU, from the latest release of each."""
    return _reg_lookup(conn, "approval_status", substance_key)


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
    ``[start, end]``. Records with no date are counted in a note."""
    return _reg_lookup(conn, "safety_actions", substance_key, (start, end))


_LABEL_SQL = """
SELECT s.article_id, s.loinc_code, s.title, a.book_meta->>'version', NULLIF(a.year, 0),
       a.source_file
FROM (SELECT DISTINCT article_id FROM episteme.label_substance WHERE unii = ANY(%(uniis)s)) l
JOIN episteme.label_section s ON s.article_id = l.article_id
LEFT JOIN episteme.articles a ON a.source = 'dailymed' AND a.id = l.article_id
ORDER BY s.article_id, s.start_offset, s.loinc_code
"""


def current_label(
    conn: psycopg.Connection, substance_key: int, *, tenant: str, caller: str
) -> LookupResult:
    """The sections of the current DailyMed labels whose ``label_substance.unii`` is the
    UNII of a member of the substance's moiety family. The claim value is the section
    title; the release is the SPL version (``articles.book_meta``). The extract keeps
    only the newest SPL version per set id, so the change history is always a gap."""
    scope = substance_scope(conn, substance_key)
    if scope is None:
        return _no_substance("current_label", substance_key)
    notes = (LABEL_HISTORY_GAP,)
    tables = ("label_section", "label_substance", "articles")
    if not all(relation_exists(conn, f"{SCHEMA}.{t}") for t in tables):
        return LookupResult((), Coverage(matched=0, missing=("dailymed",), notes=notes))
    rows: list[tuple] = []
    if scope.uniis:
        with conn.cursor() as cur:
            cur.execute(_LABEL_SQL, {"uniis": list(scope.uniis)})
            rows = cur.fetchall()
    claims = tuple(
        Claim(
            text=(
                f"{article_id} section {loinc} {title or '(no title)'}, SPL version "
                f"{version or 'unknown'}, effective year {year or 'unknown'}"
            ),
            value=title,
            method="fetched",
            provenance=Provenance(
                source="dailymed",
                release=version,
                source_file=source_file,
                record_key=f"{article_id}#{loinc}",
                jurisdiction="US",
            ),
            confidence=1.0,
            confidence_basis="exact UNII match in the label's active ingredients",
        )
        for article_id, loinc, title, version, year, source_file in rows
    )
    return LookupResult(claims, Coverage(matched=len(claims), notes=notes))


_TRIALS_SQL = """
SELECT st.nct_id, st.brief_title, st.overall_status, st.phase,
       st.results_first_posted_date IS NOT NULL
FROM (SELECT DISTINCT nct_id FROM {s}.intervention_substance
      WHERE substance_key = ANY(%(keys)s)) i
JOIN {s}.studies st ON st.nct_id = i.nct_id
ORDER BY st.nct_id
"""


def trials(
    conn: psycopg.Connection, substance_key: int, *, tenant: str, caller: str
) -> LookupResult:
    """AACT studies with an intervention matched to a member of the substance's moiety
    family (``intervention_substance.substance_key``), with status, phase and whether results
    are posted (``studies.results_first_posted_date``)."""
    scope = substance_scope(conn, substance_key)
    if scope is None:
        return _no_substance("trials", substance_key)
    release = latest_release(conn, "aact")
    views = ("intervention_substance", "studies")
    if release is None or not all(relation_exists(conn, f"{AACT_SCHEMA}.{v}") for v in views):
        return LookupResult((), Coverage(matched=0, missing=("aact",)))
    query = sql.SQL(_TRIALS_SQL).format(s=sql.Identifier(AACT_SCHEMA))
    with conn.cursor() as cur:
        cur.execute(query, {"keys": list(scope.keys)})
        rows = cur.fetchall()
    claims = tuple(
        Claim(
            text=(
                f"{nct} {title or '(no title)'}: status {status or 'unknown'}, phase "
                f"{phase or 'unknown'}, results posted: {'yes' if results else 'no'}"
            ),
            value=status,
            method="fetched",
            provenance=Provenance("aact", release, None, nct, None),
            confidence=1.0,
            confidence_basis="intervention name matched to the substance by exact normalised name",
        )
        for nct, title, status, phase, results in rows
    )
    return LookupResult(claims, Coverage(matched=len(claims)))


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
    if not all(relation_exists(conn, f"{SCHEMA}.{t}") for t in tables):
        return LookupResult((), Coverage(matched=0, missing=("faers",), notes=notes))
    with conn.cursor() as cur:
        cur.execute(_FAERS_SQL, {"moiety": scope.moiety_key, "start": start, "end": end})
        rows = cur.fetchall()
    claims = []
    for quarter, suspect, any_role, total in rows:
        label = f"{quarter.year}Q{(quarter.month - 1) // 3 + 1}"
        of_total = f"of {total} reports received" if total is not None else "total not available"
        claims.append(
            Claim(
                text=(
                    f"{label}: {suspect} reports name the substance as suspect, {any_role} "
                    f"in any role, {of_total} {FAERS_LABEL}"
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
