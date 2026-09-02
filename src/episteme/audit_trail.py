"""ALCOA+ tamper-evident audit trail.

STUB — full implementation in Plan 2: writes episteme._audit (in the same
transaction as each data change) plus an append-only JSONL mirror, hash-
chained (prev_hash / record_hash), actor from config.require_actor().
"""

from __future__ import annotations

from typing import Any


def record(event_type: str, **fields: Any) -> None:
    raise NotImplementedError(
        "audit_trail.record is implemented in Plan 2 (Postgres storage layer)"
    )
