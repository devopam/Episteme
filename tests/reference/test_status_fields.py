"""Argument conversion of the harness. The payload status fields of the regulatory
claims (EMA keys as found in the loaded 2026-11 releases, checked 2026-11-10) are now
in the SQL function ``episteme_api_internal.reg_fields`` (migration 0015); their tests
are in ``test_api_regulatory.py`` (pg)."""

from datetime import date

from episteme.harness.loop import _convert


def test_convert_accepts_iso_strings_and_dates():
    as_str = _convert(
        "faers_counts", {"substance_key": 1, "start": "2025-01-01", "end": "2025-06-30"}
    )
    as_date = _convert(
        "faers_counts", {"substance_key": 1, "start": date(2025, 1, 1), "end": date(2025, 6, 30)}
    )
    assert as_str == as_date
