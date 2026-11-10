"""EMA status keys as found in the loaded 2026-11 releases (checked 2026-11-10)."""

from datetime import date

from episteme.harness.loop import _convert
from episteme.reference.lookups import _NO_STATUS_FIELD, _reg_claim


def _row(payload):
    return ("K1", "EU", date(2026, 1, 2), payload, "f.json")


def test_outside_eu_reads_ema_opinion_status():
    claim = _reg_claim("ema_outside_eu", "2026-11", _row({"ema_opinion_status": "Positive"}))
    assert claim.value == "Positive"


def test_dhpc_status_is_the_dhpc_type():
    payload = {"dhpc_type": "Safety signal", "regulatory_outcome": "Variation"}
    assert _reg_claim("ema_dhpc", "2026-11", _row(payload)).value == "Safety signal"


def test_article57_has_no_status_field_by_design():
    assert "ema_article57" in _NO_STATUS_FIELD
    claim = _reg_claim("ema_article57", "2026-11", _row({"product_name": "X"}))
    assert claim.value is None


def test_convert_accepts_iso_strings_and_dates():
    as_str = _convert(
        "faers_counts", {"substance_key": 1, "start": "2025-01-01", "end": "2025-06-30"}
    )
    as_date = _convert(
        "faers_counts", {"substance_key": 1, "start": date(2025, 1, 1), "end": date(2025, 6, 30)}
    )
    assert as_str == as_date
