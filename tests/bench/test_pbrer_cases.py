from __future__ import annotations

import json
from datetime import date

import pytest

from episteme.bench.pbrer_cases import PbrerCase, interval_for, load_case, write_case


def _case(**kw) -> PbrerCase:
    base = dict(
        case_id="psusa-1",
        substance_key=7,
        substance_name="Metformin",
        start=date(2023, 3, 15),
        end=date(2024, 3, 14),
        psusa_record_key="EMEA/H/C/PSUSA/1/202403",
        expected_facts={"2": ["a fact"]},
        expected_gaps={"9": ["Coverage gap: faers"]},
    )
    base.update(kw)
    return PbrerCase(**base)


def test_case_round_trips_through_json(tmp_path):
    case = _case(reviewed_by_owner=True)
    path = write_case(case, tmp_path)
    assert path.name == "psusa-1.json"
    assert load_case(path) == case
    raw = json.loads(path.read_text("utf-8"))
    assert raw["start"] == "2023-03-15"
    assert list(raw) == sorted(raw)


def test_unreviewed_by_default():
    assert _case().reviewed_by_owner is False


@pytest.mark.parametrize(
    ("event", "start", "end"),
    [
        (date(2024, 3, 15), date(2023, 3, 15), date(2024, 3, 14)),
        (date(2024, 2, 29), date(2023, 2, 28), date(2024, 2, 28)),
        (date(2024, 1, 1), date(2023, 1, 1), date(2023, 12, 31)),
    ],
)
def test_interval_is_twelve_months_before_event(event, start, end):
    assert interval_for(event) == (start, end)
