"""Field-wise comparison of a legacy result (``_legacy``) with the new one.

``_legacy`` defines its own dataclasses (for example ``SubstanceScope``), so ``==``
between a legacy and a new object is False even when every field is equal. Compare
with ``assert_same_fields`` in every parity test.
"""

from __future__ import annotations

import dataclasses
from typing import Any


def as_fields(value: Any) -> Any:
    """``value`` with every dataclass (nested ones too) turned into a dict; ``None``
    and other values unchanged."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


def assert_same_fields(new: Any, old: Any) -> None:
    """Assert that ``new`` and ``old`` are both ``None`` or have equal fields."""
    if new is None or old is None:
        assert new is None and old is None, f"new={new!r} old={old!r}"
        return
    assert as_fields(new) == as_fields(old)
