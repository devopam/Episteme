"""FAERS parser: one openFDA ``drug/event`` zip to typed rows. Pure: no database.

``iter_reports`` streams the reports of one zip with ``ijson``; a real zip holds
about 12,000 reports and its one JSON member is never loaded whole. ``report_rows``
turns one report into ``(report row, drug rows, reaction rows)``.

One JSON shape covers all eras, but the field coverage differs. The rules:

* **Seriousness.** ``'1'`` is yes and ``'2'`` is no. Old reports list a flag only when
  it is true, so a missing flag means no when the report has ``serious`` or any other
  seriousness field. A report with none of these fields is unknown (``None``).
* **Dates.** ``YYYYMMDD`` only. Any other value gives ``None``. ``receive_year`` comes
  from ``receivedate`` (``_receive_date`` is the one place that chooses it). A report
  with no usable date has ``receive_year`` ``None``, so it goes to the default
  partition, and ``count_report`` counts it.
* **Active substance.** The published ``activesubstance``; else the annotation's
  ``substance_name`` values, sorted and joined with ``"; "`` (``split_names`` splits
  on that); else ``norm_name`` of the product name.
* **Annotation.** The ``openfda`` block reduced to its ``unii`` and ``substance_name``
  lists: sorted, distinct, as JSON with sorted keys and no spaces, for example
  ``{"substance_name":["X"],"unii":["Y"]}``. ``None`` when both lists are empty.

The stored text of ``product_name``, ``active_substance`` and ``annotation`` is a
permanent contract. ``faers_drug_entry`` only grows and its ids stay stable, so a new
format here would make a second entry for each combination.

Codes stay as published: ``sex`` (``patientsex``), ``route``
(``drugadministrationroute``) and ``outcome`` (``reactionoutcome``). ``role`` is
decoded: ``1`` suspect, ``2`` concomitant, ``3`` interacting. Reaction terms stay as
published, with their case. ``batch_number`` keeps the published text, and
``batch_norm`` is ``norm_lot`` of it.

All text loses U+0000 and lone surrogates (PostgreSQL refuses them) and U+001F (the
separator of the ``faers_drug_entry`` md5 key). Empty text becomes ``None``, as the
md5 key treats NULL and empty alike.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections import Counter
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import ijson

from episteme.data.regulatory.normalize import norm_lot, norm_name

_ROLES = {"1": "suspect", "2": "concomitant", "3": "interacting"}
_FLAG_VALUES = {"1": True, "2": False}
# Row column -> source field. The source spells the congenital flag this way.
_FLAGS = {
    "serious_death": "seriousnessdeath",
    "serious_hospitalization": "seriousnesshospitalization",
    "serious_life_threatening": "seriousnesslifethreatening",
    "serious_disabling": "seriousnessdisabling",
    "serious_congenital": "seriousnesscongenitalanomali",
    "serious_other": "seriousnessother",
}
# patientonsetageunit -> years per unit.
_AGE_UNITS = {
    "800": 10.0,
    "801": 1.0,
    "802": 1.0 / 12.0,
    "803": 7.0 / 365.25,
    "804": 1.0 / 365.25,
    "805": 1.0 / (24.0 * 365.25),
}
_YYYYMMDD = re.compile(r"\d{8}")
_MAX_INT4 = 2**31 - 1
# A larger age or weight is an error in the source, and could overflow a real column.
_MAX_MEASURE = 1e6


def iter_reports(zip_path: Path) -> Iterator[dict]:
    """The reports of one zip, one at a time (``results.item`` of its one JSON member).
    A truncated member raises when the stream reaches the cut."""
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".json")]
        if len(members) != 1:
            raise ValueError(f"{zip_path}: expected one .json member, found {len(members)}")
        with zf.open(members[0]) as fh:
            yield from ijson.items(fh, "results.item", use_float=True)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    if not s.isascii():
        s = s.encode("utf-8", "ignore").decode("utf-8")
    s = s.replace("\x00", "").replace("\x1f", " ").strip()
    return s or None


def _date(value: Any) -> date | None:
    s = _text(value)
    if s is None or not _YYYYMMDD.fullmatch(s):
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def _receive_date(report: dict) -> date | None:
    """The date that sets ``receive_year`` and the signal quarter."""
    return _date(report.get("receivedate"))


def _int(value: Any) -> int | None:
    s = _text(value)
    if s is None or not s.isdecimal():
        return None
    n = int(s)
    return n if n <= _MAX_INT4 else None


def _measure(value: Any) -> float | None:
    s = _text(value)
    if s is None:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if 0 <= v < _MAX_MEASURE else None  # False for NaN


def _age_years(patient: dict) -> float | None:
    factor = _AGE_UNITS.get(_text(patient.get("patientonsetageunit")) or "")
    age = _measure(patient.get("patientonsetage"))
    return age * factor if age is not None and factor is not None else None


def _flag(value: Any, *, known: bool) -> bool | None:
    if value is None:
        return False if known else None
    return _FLAG_VALUES.get(_text(value) or "")


def _seriousness(report: dict) -> dict[str, bool | None]:
    known = report.get("serious") is not None or any(
        report.get(field) is not None for field in _FLAGS.values()
    )
    flags = {col: _flag(report.get(field), known=known) for col, field in _FLAGS.items()}
    if report.get("serious") is None and known:
        flags["serious"] = any(v is True for v in flags.values())
    else:
        flags["serious"] = _flag(report.get("serious"), known=known)
    return flags


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _names(value: Any) -> list[str]:
    """Sorted distinct non-empty texts of a value or a list of values."""
    return sorted({t for t in (_text(v) for v in _as_list(value)) if t})


def _annotation(drug: dict) -> tuple[str | None, list[str]]:
    """(canonical annotation text, its substance names)."""
    block = drug.get("openfda")
    if not isinstance(block, dict):
        return None, []
    names = _names(block.get("substance_name"))
    uniis = _names(block.get("unii"))
    kept = {k: v for k, v in (("substance_name", names), ("unii", uniis)) if v}
    if not kept:
        return None, []
    return json.dumps(kept, sort_keys=True, separators=(",", ":"), ensure_ascii=False), names


def _published_substance(drug: dict) -> str | None:
    names = [
        _text(item.get("activesubstancename"))
        for item in _as_list(drug.get("activesubstance"))
        if isinstance(item, dict)
    ]
    return "; ".join(sorted({n for n in names if n})) or None


def _drug_row(report_id: str | None, year: int | None, seq: int, drug: dict) -> dict:
    product = _text(drug.get("medicinalproduct"))
    annotation, annotated_names = _annotation(drug)
    batch = _text(drug.get("drugbatchnumb"))
    substance = (
        _published_substance(drug)
        or "; ".join(annotated_names)
        or (norm_name(product) if product else None)
    )
    return {
        "safetyreportid": report_id,
        "seq": seq,
        "receive_year": year,
        "role": _ROLES.get(_text(drug.get("drugcharacterization")) or ""),
        "product_name": product,
        "active_substance": substance,
        "annotation": annotation,
        "batch_number": batch,
        "batch_norm": norm_lot(batch) if batch else None,
        "route": _text(drug.get("drugadministrationroute")),
        "indication": _text(drug.get("drugindication")),
    }


def report_rows(report: dict) -> tuple[dict, list[dict], list[dict]]:
    """(report row, drug rows, reaction rows) of one report. ``seq`` counts from 1 in
    source order. The drug rows carry the dictionary text (``product_name``,
    ``active_substance``, ``annotation``) and the reaction rows carry ``term``; the
    loader turns these into ids. A report with no id has ``safetyreportid`` ``None``,
    and the loader skips and counts it."""
    report_id = _text(report.get("safetyreportid"))
    received = _receive_date(report)
    year = received.year if received else None
    patient = report.get("patient")
    if not isinstance(patient, dict):
        patient = {}
    source = report.get("primarysource")
    reporter = source.get("reportercountry") if isinstance(source, dict) else None
    row = {
        "safetyreportid": report_id,
        "receive_year": year,
        "receive_date": received,
        "receipt_date": _date(report.get("receiptdate")),
        "version": _int(report.get("safetyreportversion")),
        **_seriousness(report),
        "country": (
            _text(report.get("occurcountry"))
            or _text(report.get("primarysourcecountry"))
            or _text(reporter)
        ),
        "sex": _text(patient.get("patientsex")),
        "age_years": _age_years(patient),
        "weight_kg": _measure(patient.get("patientweight")),
    }
    drugs = [
        _drug_row(report_id, year, seq, drug)
        for seq, drug in enumerate(_as_list(patient.get("drug")), start=1)
        if isinstance(drug, dict)
    ]
    reactions = [
        {
            "safetyreportid": report_id,
            "seq": seq,
            "receive_year": year,
            "term": _text(item.get("reactionmeddrapt")),
            "outcome": _text(item.get("reactionoutcome")),
        }
        for seq, item in enumerate(_as_list(patient.get("reaction")), start=1)
        if isinstance(item, dict)
    ]
    return row, drugs, reactions


def count_report(counts: Counter, row: dict) -> None:
    """Add one report row to the parse counts (``no_date``)."""
    if row["receive_year"] is None:
        counts["no_date"] += 1
