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


def no_graph_sources() -> set[str]:
    """Literature sources that have extract and load but no graph stage."""
    return _words("NO_GRAPH_SOURCES")


def reg_sources() -> set[str]:
    """Reference feeds: a download and a load into ``reg_record``, nothing else."""
    return _words("REG_SOURCES")


# `openfda_faers` is not in WRAPPER: it has no download of its own (the zips come from
# `openfda download all_human`) and only a load stage. run_pipeline.sh treats it as a
# special case, and the docs test helpers must too.
FAERS_SOURCE = "openfda_faers"
FAERS_SCRIPT = "openfda/load_faers.sh"


def dispatcher_treats_faers_as_a_special_case() -> bool:
    return f'"$SOURCE" = "{FAERS_SOURCE}"' in _RUN and "wpath_faers" in _RUN


def doc(name: str) -> str:
    return (REPO / "docs" / name).read_text(encoding="utf-8")
