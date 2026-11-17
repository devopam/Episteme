"""Test-only, verbatim copy of the Python query paths of the reference lookups as they
were before the SQL lookup API (migration 0015): ``_scope``, ``resolve``, ``lookups``,
``literature`` and ``codes`` (ledger ruling R1).

The parity tests of the API layer compare these functions with the SQL-backed ones on the
same fixtures. Do not import this module from ``src``. Do not change the logic: a change
here voids the parity tests. Only these names were changed, because the modules shared
them: resolve ``_SQL``/``_claim``/``_SOURCE`` -> ``_RESOLVE_SQL``/``_resolve_claim``/
``_RESOLVE_SOURCE``; literature ``_SQL``/``_TOPIC``/``DATASETS`` -> ``_LITERATURE_SQL``/
``_LITERATURE_TOPIC``/``LITERATURE_DATASETS``; codes ``_SQL`` -> ``_CODE_SQL``.
``lookups.__all__`` is left out. ``AACT_SCHEMA`` is a module constant here too: a test
that points ``lookups.AACT_SCHEMA`` at its own schema must patch this module as well.

Delete this module when every parity test passes (spec section 4.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg
from psycopg import sql

from episteme.data.regulatory.load_substance import resolve_key
from episteme.data.regulatory.normalize import norm_name
from episteme.reference.claims import Claim, Coverage, LookupResult, Method, Provenance

# =============================================================================
# _scope.py
# =============================================================================
SCHEMA = "episteme"
# xref id types that hold a normalised name. ema_name / aact_name rows repeat values of
# name rows (resolve_names adds them only on an exact match), so name is enough.
NAME_ID_TYPES = ("name",)

# COALESCE(moiety_key, substance_key) = M, written so that substance_moiety_key_idx and
# the primary key can serve it.
_FAMILY_SQL = """
SELECT substance_key, unii, preferred_name FROM episteme.substance
WHERE moiety_key = %(m)s OR (moiety_key IS NULL AND substance_key = %(m)s)
ORDER BY substance_key
"""


@dataclass(frozen=True)
class SubstanceScope:
    """The keys, UNIIs and normalised names of a substance's moiety family."""

    substance_key: int
    moiety_key: int
    keys: tuple[int, ...]
    uniis: tuple[str, ...]
    names: tuple[str, ...]


def relation_exists(conn: psycopg.Connection, name: str) -> bool:
    """Whether ``name`` (``schema.relation``) exists, by ``to_regclass``."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
        return bool(cur.fetchone()[0])


def source_ready(conn: psycopg.Connection, tables: tuple[str, ...], probe: str) -> bool:
    """Whether every ``episteme`` table in ``tables`` exists and ``probe`` holds a row.
    The migrations create these tables empty, so an unloaded source is a gap, not "no
    data"."""
    if not all(relation_exists(conn, f"{SCHEMA}.{t}") for t in tables):
        return False
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(sql.Identifier(SCHEMA, probe))
        )
        return bool(cur.fetchone()[0])


def latest_release(conn: psycopg.Connection, dataset: str) -> str | None:
    """The ``is_latest`` ``release_id`` of ``dataset``, or ``None`` (also when
    ``reg_release`` does not exist)."""
    if not relation_exists(conn, f"{SCHEMA}.reg_release"):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT release_id FROM episteme.reg_release WHERE dataset = %s AND is_latest",
            (dataset,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def substance_scope(conn: psycopg.Connection, substance_key: int) -> SubstanceScope | None:
    """The moiety family of ``substance_key``, or ``None`` when the substance tables or
    the substance do not exist. Names: the preferred names (``norm_name`` form, as
    ``reg_record.substance_names``) and the ``name`` xref rows of every family member."""
    if not (
        relation_exists(conn, f"{SCHEMA}.substance")
        and relation_exists(conn, f"{SCHEMA}.substance_xref")
    ):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(moiety_key, substance_key) FROM episteme.substance "
            "WHERE substance_key = %s",
            (substance_key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        moiety = int(row[0])
        cur.execute(_FAMILY_SQL, {"m": moiety})
        family = cur.fetchall()
        keys = tuple(dict.fromkeys([int(substance_key)] + [int(k) for k, _, _ in family]))
        cur.execute(
            "SELECT DISTINCT id_value FROM episteme.substance_xref "
            "WHERE substance_key = ANY(%s) AND id_type = ANY(%s) ORDER BY id_value",
            (list(keys), list(NAME_ID_TYPES)),
        )
        xref_names = [r[0] for r in cur.fetchall()]
    names = [norm_name(p) for _, _, p in family if p] + xref_names
    return SubstanceScope(
        substance_key=int(substance_key),
        moiety_key=moiety,
        keys=keys,
        uniis=tuple(u for _, u, _ in family if u),
        names=tuple(n for n in dict.fromkeys(names) if n),
    )


# =============================================================================
# resolve.py
# =============================================================================
_RESOLVE_SOURCE = "substance"

# A substance matches when its own UNII equals the upper-cased input, or when a
# substance_xref row matches: id_type 'unii' on the upper-cased input, id_type 'name'
# on resolve_key(input), or (when the caller names an id_type) that type on either form.
_RESOLVE_SQL = """
SELECT s.substance_key,
       s.unii,
       s.preferred_name,
       (s.moiety_key IS NULL OR s.moiety_key = s.substance_key) AS is_moiety
FROM episteme.substance s
WHERE s.unii = %(unii)s
   OR s.substance_key IN (
        SELECT x.substance_key
        FROM episteme.substance_xref x
        WHERE (%(id_type)s::text IS NULL
               AND ((x.id_type = 'unii' AND x.id_value = %(unii)s)
                    OR (x.id_type = 'name' AND x.id_value = %(key)s)))
           OR (x.id_type = %(id_type)s::text
               AND x.id_value IN (%(unii)s, %(key)s))
   )
ORDER BY is_moiety DESC, s.substance_key ASC
"""


def _resolve_claim(row: tuple, method: Method, confidence: float, basis: str) -> Claim:
    key, unii, name, _is_moiety = row
    return Claim(
        text=name or (unii or str(key)),
        value=str(key),
        method=method,
        provenance=Provenance(
            source=_RESOLVE_SOURCE,
            release=None,
            source_file=None,
            record_key=f"unii:{unii}" if unii else None,
            jurisdiction=None,
        ),
        confidence=confidence,
        confidence_basis=basis,
    )


def resolve_entity(
    conn: psycopg.Connection,
    text: str,
    *,
    tenant: str,
    caller: str,
    id_type: str | None = None,
) -> LookupResult:
    """Resolve a drug name or UNII to a substance.

    Ordering rule when several substances match: a substance that is itself a moiety
    (``moiety_key`` NULL or equal to its own ``substance_key``) comes first; a salt or
    ester whose moiety is another row comes after it. Ties go to the lowest
    ``substance_key``. The first row is the claim; the others are its alternatives.

    One match: method ``fetched``, confidence 1.0. n > 1 matches: method ``mapped``,
    confidence 1/n, basis "<n> substances share this name". ``tenant`` and ``caller``
    are accepted for a stable signature; v1 does not use them.
    """
    raw = (text or "").strip()
    key = resolve_key(raw)
    empty = LookupResult(claims=(), coverage=Coverage(matched=0, missing=(_RESOLVE_SOURCE,)))
    if not raw or not key:
        return empty
    params = {"unii": raw.upper(), "key": key, "id_type": id_type}
    with conn.cursor() as cur:
        cur.execute(_RESOLVE_SQL, params)
        rows = cur.fetchall()
    if not rows:
        return empty
    n = len(rows)
    if n == 1:
        claim = _resolve_claim(rows[0], "fetched", 1.0, "exact UNII or normalised name match")
    else:
        basis = f"{n} substances share this name"
        first = _resolve_claim(rows[0], "mapped", 1.0 / n, basis)
        claim = Claim(
            text=first.text,
            value=first.value,
            method="mapped",
            provenance=first.provenance,
            confidence=first.confidence,
            confidence_basis=basis,
            alternatives=tuple(_resolve_claim(r, "mapped", 1.0 / n, basis) for r in rows[1:]),
        )
    return LookupResult(claims=(claim,), coverage=Coverage(matched=n))


# =============================================================================
# lookups.py
# =============================================================================
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
FAERS_LABEL = "US public spontaneous reports (FAERS)"
FAERS_QUARTER_RULE = "quarters that overlap the interval are counted whole"

# Payload fields shown in a claim, read with .get(): a missing key is skipped. The first
# field of each "status" tuple is the claim value. Article 57 names are checked in
# parsers.ARTICLE57_COLUMNS. The EMA keys were checked against the loaded 2026-11
# releases (2026-11-10); the FDA keys are not checked against a downloaded file.
_STATUS_FIELDS: dict[str, tuple[str, ...]] = {
    "fda_drugsfda": ("products.marketing_status",),
    "ema_medicines": ("medicine_status",),
    "ema_outside_eu": ("ema_opinion_status",),
    "fda_enforcement": ("classification", "status"),
    "ema_referrals": ("current_status",),
    "ema_psusa": ("regulatory_outcome",),
    "ema_dhpc": ("dhpc_type", "regulatory_outcome"),
}
# Datasets with no status field: a record is itself the fact (an Article 57 entry is an
# authorised product). Their claims have no value and raise no "status field not found".
_NO_STATUS_FIELD = frozenset({"ema_article57"})
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
            found = [_reg_claim(ds, release, r) for r in cur.fetchall()]
        if ds not in _NO_STATUS_FIELD and any(c.value is None for c in found):
            notes.append(f"{ds}: status field not found")
        claims.extend(found)
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
    if not source_ready(conn, tables, "label_section"):
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


# =============================================================================
# literature.py
# =============================================================================
LITERATURE_DATASETS = ("article_mesh",)

_LITERATURE_TOPIC = """
  AND EXISTS (
    SELECT 1 FROM episteme.article_mesh t
    WHERE t.pmid = m.pmid
      AND (lower(t.descriptor_name) = %(topic)s
           OR EXISTS (SELECT 1 FROM unnest(t.qualifiers) q WHERE lower(q) = %(topic)s)))
"""

_LITERATURE_SQL = """
WITH hits AS (
  SELECT DISTINCT m.pmid FROM episteme.article_mesh m
  WHERE lower(m.descriptor_name) = ANY(%(names)s)
  {topic}
)
SELECT h.pmid, y.year, d.descriptors, d.source_file
FROM hits h
LEFT JOIN LATERAL (
  SELECT NULLIF(max(a.year), 0) AS year FROM episteme.articles a WHERE a.pmid = h.pmid
) y ON true
CROSS JOIN LATERAL (
  SELECT array_agg(DISTINCT m.descriptor_name ORDER BY m.descriptor_name) AS descriptors,
         min(m.source_file) AS source_file
  FROM episteme.article_mesh m
  WHERE m.pmid = h.pmid AND lower(m.descriptor_name) = ANY(%(names)s)
) d
ORDER BY y.year DESC NULLS LAST, length(h.pmid) DESC, h.pmid DESC
LIMIT %(limit)s
"""


def literature(
    conn: psycopg.Connection,
    substance_key: int,
    topic: str | None,
    *,
    tenant: str,
    caller: str,
    limit: int = 20,
) -> LookupResult:
    """Up to ``limit`` PubMed records indexed with the substance as a MeSH descriptor,
    newest first. Each claim cites ``pmid:<pmid>``. ``tenant`` and ``caller`` are
    accepted for a stable signature; v1 does not use them."""
    scope = substance_scope(conn, substance_key)
    if scope is None:
        note = f"substance {substance_key} not found"
        return LookupResult((), Coverage(matched=0, missing=LITERATURE_DATASETS, notes=(note,)))
    if not source_ready(conn, ("article_mesh", "articles"), "article_mesh"):
        return LookupResult((), Coverage(matched=0, missing=LITERATURE_DATASETS))
    if not scope.names or limit <= 0:
        return LookupResult((), Coverage(matched=0))
    topic_key = (topic or "").strip().lower()
    params = {"names": list(scope.names), "topic": topic_key, "limit": limit}
    with conn.cursor() as cur:
        cur.execute(_LITERATURE_SQL.format(topic=_LITERATURE_TOPIC if topic_key else ""), params)
        rows = cur.fetchall()
    claims = tuple(
        Claim(
            text=(
                f"PMID {pmid} ({year or 'year unknown'}): MeSH {', '.join(descriptors or [])}"
                + (f"; topic {topic.strip()}" if topic_key else "")
            ),
            value=pmid,
            method="fetched",
            provenance=Provenance(
                source="pubmed",
                release=None,
                source_file=source_file,
                record_key=f"pmid:{pmid}",
                jurisdiction=None,
            ),
            confidence=1.0,
            confidence_basis="exact MeSH descriptor name match",
        )
        for pmid, year, descriptors, source_file in rows
    )
    notes = (f"limited to the {limit} newest records",) if len(claims) == limit else ()
    return LookupResult(claims, Coverage(matched=len(claims), notes=notes))


# =============================================================================
# codes.py
# =============================================================================
_CODE_SQL = """
SELECT release_id, version, licence_notice, display, status, source_file
FROM episteme.code_current
WHERE system_key = %s AND code = %s
"""


def lookup_code(
    conn: psycopg.Connection,
    system: str,
    code: str,
    *,
    tenant: str,
    caller: str,
) -> LookupResult:
    if not tenant.strip() or not caller.strip():
        raise ValueError("tenant and caller are required")
    with conn.cursor() as cur:
        cur.execute(_CODE_SQL, (system, code))
        row = cur.fetchone()
    if row is None:
        return LookupResult(
            claims=(),
            coverage=Coverage(matched=0, notes=(f"no current code {code!r} in {system}",)),
        )
    release_id, version, notice, display, status, source_file = row
    shown = display or code
    text = f"{system} {code} ({version or release_id}): {shown}. {notice}"
    claim = Claim(
        text=text,
        value=shown,
        method="fetched",
        provenance=Provenance(
            source=system,
            release=release_id,
            source_file=source_file,
            record_key=code,
            jurisdiction=None,
        ),
        confidence=1.0,
        confidence_basis=f"episteme.code_current; status={status or 'unset'}",
    )
    return LookupResult(claims=(claim,), coverage=Coverage(matched=1))
