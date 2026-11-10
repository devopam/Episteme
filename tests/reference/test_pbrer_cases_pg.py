"""pg test of the case builder. It lives here to use the ``ref_db`` fixture
(tests/ has no package root, so tests/bench cannot import tests/reference)."""

from __future__ import annotations

from datetime import date

import pytest

from episteme.bench.pbrer_cases import build_cases

from ._pbrer_db import KW, add_release

pytestmark = pytest.mark.pg


def _psusa(key: str, names: list[str], event: date | None) -> dict:
    return {
        "record_key": key,
        "jurisdiction": "EU",
        "substance_names": names,
        "event_date": event,
        "payload": {},
    }


def test_build_cases_skips_ambiguous_substances(ref_db):
    # 'metformin' -> moiety 1 only; 'metformin hydrochloride' -> family of moiety 1
    # (still one); 'aspirin' -> one; 'nosuch' -> none; metformin+aspirin -> two moieties.
    add_release(
        ref_db,
        "ema_psusa",
        "r1",
        [
            _psusa("P/1", ["metformin"], date(2024, 3, 15)),
            _psusa("P/2", ["nosuch"], date(2024, 3, 14)),
            _psusa("P/3", ["metformin", "aspirin"], date(2024, 3, 13)),
            _psusa("P/4", ["aspirin"], date(2024, 2, 29)),
            _psusa("P/5", ["aspirin"], None),
            _psusa("P/6", ["metformin", "metformin hydrochloride"], date(2024, 1, 5)),
        ],
        latest=True,
    )
    cases = build_cases(ref_db, 10, **KW)
    assert [c.psusa_record_key for c in cases] == ["P/1", "P/4", "P/6"]
    first = cases[0]
    assert first.substance_key == 1
    assert (first.start, first.end) == (date(2023, 3, 15), date(2024, 3, 14))
    assert cases[1].start == date(2023, 2, 28)
    assert cases[2].substance_key == 1  # salt plus its moiety: one family
    assert first.reviewed_by_owner is False
    assert "2" in first.expected_gaps or first.expected_facts is not None


def test_build_cases_honours_limit(ref_db):
    add_release(
        ref_db,
        "ema_psusa",
        "r1",
        [_psusa(f"P/{i}", ["aspirin"], date(2024, 1, 10 + i)) for i in range(3)],
        latest=True,
    )
    assert len(build_cases(ref_db, 2, **KW)) == 2
