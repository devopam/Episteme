"""term_norm: lower case, non-alphanumerics to one space, trimmed (Python side)."""

from __future__ import annotations

import pytest

from episteme.reference.term_norm import term_norm


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Stevens-Johnson  Syndrome!", "stevens johnson syndrome"),
        ("Hb A1c", "hb a1c"),
        ("Ångström", "ångström"),
        ("", ""),
        ("  --  ", ""),
        ("a_b", "a b"),
        ("\tTab\tseparated\n", "tab separated"),
        ("TNF-α", "tnf α"),
    ],
)
def test_term_norm(text, expected):
    assert term_norm(text) == expected
