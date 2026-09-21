"""Parsers shared by the docs-vs-code consistency tests. Read-only; no DB, no network."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_RUN = (REPO / "scripts" / "data" / "run_pipeline.sh").read_text(encoding="utf-8")


def wrapper_table() -> dict[str, str]:
    block = re.search(r"declare -A WRAPPER=\((.*?)\n\)", _RUN, re.S)
    assert block, "WRAPPER table not found in run_pipeline.sh"
    return dict(re.findall(r'\[(\w+)\]="([^"]+)"', block.group(1)))


def _words(var: str) -> set[str]:
    m = re.search(rf'^{var}="([^"]*)"', _RUN, re.M)
    assert m, f"{var} not found in run_pipeline.sh"
    return set(m.group(1).split())


def lit_sources() -> set[str]:
    return _words("LIT_SOURCES")


def structured_sources() -> set[str]:
    return _words("STRUCTURED_SOURCES")


def doc(name: str) -> str:
    return (REPO / "docs" / name).read_text(encoding="utf-8")
