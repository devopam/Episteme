"""make_call_tool against the real lookup signatures (pg). Run locally by the controller."""

from __future__ import annotations

import pytest

from episteme.harness.loop import make_call_tool
from episteme.reference.claims import LookupResult

pytestmark = pytest.mark.pg


def test_make_call_tool_resolve_and_date_range_tool(ref_db):
    call = make_call_tool(ref_db, tenant="t", caller="test")
    resolved = call("resolve_entity", {"text": "metformin"})
    assert isinstance(resolved, LookupResult)
    assert resolved.claims
    key = int(resolved.claims[0].value)
    for name in ("faers_counts", "safety_actions"):
        got = call(name, {"substance_key": key, "start": "2025-01-01", "end": "2025-12-31"})
        assert isinstance(got, LookupResult)
    lit = call("literature", {"substance_key": key, "topic": None})
    assert isinstance(lit, LookupResult)
