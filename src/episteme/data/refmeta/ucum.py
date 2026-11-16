"""UCUM essence XML as a read-only projection.

The XML file stays verbatim on disk. This module does not rewrite it. The code
is ``<kind>:<Code attribute>`` (``prefix``, ``base-unit`` or ``unit``). UCUM keeps
``Code`` unique; ``printSymbol`` is not (``10*`` and ``10^`` both print ``10``), so it is
only a fallback, then ``name``.
"""

from __future__ import annotations

from pathlib import Path

from defusedxml.ElementTree import parse as safe_parse

from episteme.data.refmeta.model import CodeRelease, CodeRow

_KINDS = ("prefix", "base-unit", "unit")


def parse_ucum(path: Path, *, notice: str, source_file: str, release_id: str) -> CodeRelease:
    root = safe_parse(str(path), forbid_dtd=True).getroot()
    rows: list[CodeRow] = []
    for node in root.iter():
        kind = node.tag.rsplit("}", 1)[-1]
        if kind not in _KINDS:
            continue
        print_symbol = _child_text(node, "printSymbol")
        symbol = node.get("Code") or print_symbol or _child_text(node, "name")
        if not symbol:
            raise ValueError(f"{path}: a {kind} has no Code, printSymbol or name")
        rows.append(
            CodeRow(
                code=f"{kind}:{symbol}",
                display=_child_text(node, "name") or symbol,
                extra={
                    "kind": kind,
                    "code_attr": node.get("Code"),
                    "print_symbol": print_symbol,
                },
            )
        )
    return CodeRelease(
        system_key="ucum",
        release_id=release_id,
        licence_notice=notice,
        source_file=source_file,
        codes=tuple(rows),
    )


def _child_text(node, name: str) -> str | None:
    for child in node:
        if child.tag.rsplit("}", 1)[-1] == name and child.text and child.text.strip():
            return child.text.strip()
    return None
