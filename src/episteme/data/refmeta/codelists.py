"""ICH E2B bilingual code lists (OASIS genericode) as one ``e2b_cl`` release.

The code stored is ``CLn:<code>``, so a value of ``1`` in two lists does not collide.
English is the display name. Japanese, when the file has it, stays in ``extra``.
A revised duplicate of a list is kept only when its codes match the ``.gc.xml`` file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from defusedxml.ElementTree import parse as safe_parse

from episteme.data.refmeta.model import CodeRelease, CodeRow
from episteme.reference.rules.build_e2b import CL_OID_PREFIX

CODELIST_NOTICE = (
    "ICH E2B(R3) bilingual code lists v2.11, IG package v1.11. "
    "Use, reproduce and adapt with ICH credit; mark changes; do not imply "
    "ICH endorsement. English is the display name; Japanese is stored beside it. "
    "Third-party term content is not stored."
)

CODELIST_GLOB = "E2B CL*.xml"


def parse_codelist_dir(directory: Path, *, source_file: str) -> CodeRelease:
    files = sorted(p for p in directory.glob(CODELIST_GLOB) if p.is_file())
    if not files:
        raise ValueError(f"{directory}: no code list files ({CODELIST_GLOB})")
    chosen: dict[str, tuple[Path, list[CodeRow]]] = {}
    for path in sorted(files, key=lambda p: (not p.name.endswith(".gc.xml"), p.name)):
        cl_id, rows = _read(path)
        kept = chosen.get(cl_id)
        if kept is None:
            chosen[cl_id] = (path, rows)
            continue
        if sorted((r.code, r.status) for r in kept[1]) != sorted((r.code, r.status) for r in rows):
            raise ValueError(f"{cl_id}: {kept[0].name} and {path.name} list different codes")
    codes = tuple(row for _, rows in chosen.values() for row in rows)
    digest = hashlib.sha256()
    for path, _ in chosen.values():
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return CodeRelease(
        system_key="e2b_cl",
        release_id=f"v2.11-{digest.hexdigest()[:12]}",
        version="2.11",
        licence_notice=CODELIST_NOTICE,
        source_file=source_file,
        codes=codes,
    )


def _read(path: Path) -> tuple[str, list[CodeRow]]:
    root = safe_parse(str(path), forbid_dtd=True).getroot()
    ident = _child(root, "Identification")
    oid = _text(_child(ident, "CanonicalUri")) if ident is not None else ""
    if not oid.startswith(CL_OID_PREFIX) or not oid[len(CL_OID_PREFIX) :].isdigit():
        raise ValueError(f"{path.name}: code list OID {oid!r} is not an ICH E2B code list")
    cl_id = f"CL{oid[len(CL_OID_PREFIX) :]}"
    rows: list[CodeRow] = []
    body = _child(root, "SimpleCodeList")
    for node in body if body is not None else ():
        if _local(node.tag) != "Row":
            continue
        values = {
            item.get("ColumnRef"): _text(_child(item, "SimpleValue"))
            for item in node
            if _local(item.tag) == "Value"
        }
        code = values.get("code") or ""
        if not code:
            raise ValueError(f"{path.name}: a row has no code value")
        retired = (values.get("cf") or "y").lower() == "n"
        extra = {"list": cl_id, "oid": oid}
        if values.get("ja"):
            extra["ja"] = values["ja"]
        rows.append(
            CodeRow(
                code=f"{cl_id}:{code}",
                display=values.get("en") or None,
                status="retired" if retired else "current",
                extra=extra,
            )
        )
    if not rows:
        raise ValueError(f"{path.name}: no rows")
    return cl_id, rows


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node, name: str):
    if node is None:
        return None
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _text(node) -> str:
    return (node.text or "").strip() if node is not None else ""
