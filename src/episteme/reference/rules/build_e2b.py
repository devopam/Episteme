"""Build the E2B(R3) ICSR rule file from the official ICH IG package.

Inputs (all from the ICH E2B(R3) IG package, v1.11):

- ``11_ICH E2B(R3) Core Data Elements and Business Rules ver.1.00.xlsx``, sheet ``ICSR``:
  one row per data element (conformance, length, data type, value allowed, business rule).
- ``7_E2B Bilingual Code Lists v2.11/E2B CL*.xml``: the ICH code lists (OASIS genericode),
  and the code list set file that names the current version of each list.
- ``5_Reference Instances/*ICSR_Reference_Instance*.xml`` (optional): the HL7 reference
  instance. Its comments name the data element of the XML element before them
  (``<!-- C.1.1: ... -->``); the XML paths of the rule file come from there.

The spreadsheet layout is the contract: a moved, renamed or extra column raises
``ValueError`` that names the column. Nothing in the output is hand-written; business
rules are recognised by fixed sentence patterns, and a conditional element whose
condition the converter does not recognise is listed under ``"manual"``.

CLI::

    python -m episteme.reference.rules.build_e2b --raw <ig_v1_11 dir> \\
        --out src/episteme/reference/rules/data/ich_e2b_r3.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import openpyxl
from defusedxml.ElementTree import DefusedXMLParser
from defusedxml.ElementTree import parse as safe_parse

SHEET = "ICSR"
XLSX_GLOB = "*Core Data Elements and Business Rules*.xlsx"
CODELIST_DIR_GLOB = "*Code Lists*"
CODELIST_GLOB = "E2B CL*.xml"
CODELIST_SET_GLOB = "*Codelist Set*.xml"
REFERENCE_GLOB = "*ICSR_Reference_Instance*.xml"
CL_OID_PREFIX = "2.16.840.1.113883.3.989.2.1.1."

# The sheet header: (row 1 label, row 2 label) per column, after strip + casefold.
# Columns after the last one must have empty headers.
HEADER: tuple[tuple[str | None, str | None], ...] = (
    ("field identification", "source"),
    (None, "header element"),
    (None, "data element number"),
    (None, "data element name"),
    ("business rules", "max length"),
    (None, "data type"),
    (None, "value allowed"),
    (None, "conformance"),
    (None, "ich business rule"),
    (None, "regional business rule"),
    ("q&a", None),
    ("nullflavor applicable", "ni"),
    (None, "msk"),
    (None, "unk"),
    (None, "na"),
    (None, "asku"),
    (None, "nask"),
    (None, "ninf"),
    (None, "pinf"),
    ("field oids", "code system oid"),
)
(
    C_SOURCE,
    C_HEADER,
    C_NUMBER,
    C_NAME,
    C_LENGTH,
    C_TYPE,
    C_ALLOWED,
    C_CONFORMANCE,
    C_RULE,
    C_REGIONAL,
    C_QA,
) = range(11)
C_OID = 19

CONFORMANCE = {
    "mandatory": "mandatory",
    "required": "mandatory",
    "conditional-mandatory": "conditional",
    "optional": "optional",
}
DATA_TYPE = {
    "an": "AN",
    "n": "N",
    "a": "A",
    "date/time": "DT",
    "boolean": "BL",
    "n/a": None,
    "-": None,
}

# Keys every element carries, also when ``None``; other keys appear only when set.
CORE_KEYS = frozenset(
    {"element", "name", "conformance", "condition", "length", "format", "codelist", "xpath"}
)

# An element id: "C.1.1", "N.2.r.1", "D.2.2a", "G.k.9.i.3.1b". Segments are numbers (with an
# optional letter suffix) or a repeat letter, so "C.1.1.Therefore" reads as "C.1.1".
_ELEMENT_ID = r"[A-HN](?:\.(?:\d+[a-z]?|[rik]))+(?![A-Za-z0-9])"
_COMMENT_ID = re.compile(rf"^\s*({_ELEMENT_ID})(?::|\s)")
_PLACEHOLDER = re.compile(rf"^{_ELEMENT_ID}$")
_R3 = re.compile(r"(\S+?)\*\s*E2B\(R3\)")
_ENUM = re.compile(r"(?<![\w.])(\d+)\s*=")

# Business-rule sentence patterns (ICH BUSINESS RULE column).
_REQUIRED_IF = re.compile(rf"required if ({_ELEMENT_ID})\s*=\s*(\w+)", re.IGNORECASE)
_SAME_AS = re.compile(rf"value (?:is|must be) the same as ({_ELEMENT_ID})", re.IGNORECASE)
_EXACTLY_ONCE = re.compile(r"set to '(\w+)' once, and only once", re.IGNORECASE)
_NOT_BOTH = re.compile(r"either (\w+) or (\w+), but NOT both")
_ONE_MEDDRA_VERSION = re.compile(r"Only 1 MedDRA version is allowed per ICSR", re.IGNORECASE)
_NOT_FUTURE = re.compile(r"cannot refer to a future date", re.IGNORECASE)
_INTEGER = re.compile(r"No fractions, decimals", re.IGNORECASE)
_PRECISION = (
    (re.compile(r"precision required is the year", re.IGNORECASE), 4),
    (re.compile(r"CCYY \(Minimum\)"), 4),
    (re.compile(r"precision required is the day", re.IGNORECASE), 8),
    (re.compile(r"(?:down )?to the second", re.IGNORECASE), 14),
)
_MEDDRA_VERSION_NAME = re.compile(r"^MedDRA Version for ", re.IGNORECASE)


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).casefold()
    return text or None


def _col(i: int) -> str:
    return openpyxl.utils.get_column_letter(i + 1)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- spreadsheet


def _check_header(row1: tuple, row2: tuple) -> None:
    width = max(len(row1), len(row2), len(HEADER))
    for i in range(width):
        got1 = _norm(row1[i]) if i < len(row1) else None
        got2 = _norm(row2[i]) if i < len(row2) else None
        if i >= len(HEADER):
            label = got2 or got1
            if label:
                raise ValueError(f"{SHEET} sheet: unknown column {_col(i)} {label!r}")
            continue
        exp1, exp2 = HEADER[i]
        if (got1, got2) != (exp1, exp2):
            raise ValueError(
                f"{SHEET} sheet: column {_col(i)} header is {got1!r}/{got2!r};"
                f" expected {exp1!r}/{exp2!r} (the sheet layout changed)"
            )


def _length(element: str, raw: Any) -> int | None:
    if raw is None or _text(raw) in ("", "-"):
        return None
    if isinstance(raw, int):
        return raw
    text = _text(raw)
    if text.isdigit():
        return int(text)
    m = _R3.search(text)
    if m and m.group(1).isdigit():
        return int(m.group(1))
    raise ValueError(f"{element}: cannot read MAX LENGTH {raw!r}")


def _data_type(element: str, raw: Any) -> str | None:
    if raw is None:
        return None
    key = _norm(raw)
    if key in DATA_TYPE:
        return DATA_TYPE[key]
    m = _R3.search(_text(raw))
    if m and _norm(m.group(1)) in DATA_TYPE:
        return DATA_TYPE[_norm(m.group(1))]
    raise ValueError(f"{element}: unknown DATA TYPE {raw!r}")


def _precision(*texts: str) -> int | None:
    for text in texts:
        for pattern, digits in _PRECISION:
            if pattern.search(text):
                return digits
    return None


def _read_sheet(xlsx: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Return the data-element rows and notes about stray cells."""
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    try:
        if SHEET not in wb.sheetnames:
            raise ValueError(f"{xlsx.name}: no sheet {SHEET!r} (found {wb.sheetnames})")
        rows = list(wb[SHEET].iter_rows(values_only=True))
    finally:
        wb.close()
    if len(rows) < 2:
        raise ValueError(f"{SHEET} sheet: header rows missing")
    _check_header(rows[0], rows[1])
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    seen: set[str] = set()
    for n, row in enumerate(rows[2:], start=3):
        cells = list(row) + [None] * (len(HEADER) - len(row))
        for i in range(len(HEADER), len(cells)):
            if _text(cells[i]):
                notes.append(
                    f"{SHEET} sheet row {n}: cell {_col(i)}{n} {_text(cells[i])!r} is in a"
                    " column without a header; ignored."
                )
        element = _text(cells[C_NUMBER])
        conformance = _norm(cells[C_CONFORMANCE])
        if not element or element == "-" or conformance in (None, "-"):
            continue
        if conformance not in CONFORMANCE:
            raise ValueError(f"{element}: unknown CONFORMANCE {cells[C_CONFORMANCE]!r}")
        if element in seen:
            raise ValueError(f"{SHEET} sheet: data element {element} appears twice")
        seen.add(element)
        out.append(
            {
                "row": n,
                "element": element,
                "block": _text(cells[C_HEADER]) or None,
                "name": " ".join(_text(cells[C_NAME]).split()),
                "conformance": CONFORMANCE[conformance],
                "conformance_raw": _text(cells[C_CONFORMANCE]),
                "length": _length(element, cells[C_LENGTH]),
                "format": _data_type(element, cells[C_TYPE]),
                "allowed": _text(cells[C_ALLOWED]),
                "rule": _text(cells[C_RULE]),
                "oids": re.findall(r"\d+(?:\.\d+)+", _text(cells[C_OID])),
            }
        )
    return out, notes


# ---------------------------------------------------------------- code lists


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(node: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in node if _local(c.tag) == name), None)


def _child_text(node: ET.Element | None, *path: str) -> str | None:
    for name in path:
        if node is None:
            return None
        node = _child(node, name)
    return None if node is None or node.text is None else node.text.strip()


def _cl_id(oid: str) -> str:
    if not oid.startswith(CL_OID_PREFIX) or not oid[len(CL_OID_PREFIX) :].isdigit():
        raise ValueError(f"code list OID {oid!r} is not an ICH E2B code list OID")
    return f"CL{oid[len(CL_OID_PREFIX) :]}"


def _read_codelist(path: Path) -> dict[str, Any]:
    root = safe_parse(str(path), forbid_dtd=True).getroot()
    ident = _child(root, "Identification")
    oid = _child_text(ident, "CanonicalUri")
    if not oid:
        raise ValueError(f"{path.name}: no Identification/CanonicalUri")
    codes: list[str] = []
    retired: list[str] = []
    rows = _child(root, "SimpleCodeList")
    for row in rows if rows is not None else ():
        if _local(row.tag) != "Row":
            continue
        values = {
            v.get("ColumnRef"): _child_text(v, "SimpleValue")
            for v in row
            if _local(v.tag) == "Value"
        }
        code = values.get("code")
        if not code:
            raise ValueError(f"{path.name}: a row has no code value")
        (retired if (values.get("cf") or "y").lower() == "n" else codes).append(code)
    return {
        "id": _cl_id(oid),
        "oid": oid,
        "name": _child_text(ident, "ShortName"),
        "version_uri": _child_text(ident, "CanonicalVersionUri"),
        "file": path.name,
        "codes": codes,
        "retired": retired,
    }


def _read_codelist_set(path: Path) -> dict[str, str]:
    root = safe_parse(str(path), forbid_dtd=True).getroot()
    out: dict[str, str] = {}
    for ref in root:
        if _local(ref.tag) != "CodeListRef":
            continue
        oid = _child_text(ref, "CanonicalUri")
        if oid:
            out[oid] = _child_text(ref, "CanonicalVersionUri") or ""
    return out


def _read_codelists(codelist_dir: Path) -> tuple[dict[str, dict], list[Path], list[str]]:
    """Return ``{CLn: list}``, the files used, and notes. Duplicate lists must agree."""
    files = sorted(p for p in codelist_dir.glob(CODELIST_GLOB) if p.is_file())
    if not files:
        raise ValueError(f"{codelist_dir}: no code list files ({CODELIST_GLOB})")
    notes: list[str] = []
    by_id: dict[str, dict] = {}
    # Prefer the standard ".gc.xml" name, then the file name, so the choice is stable.
    for path in sorted(files, key=lambda p: (not p.name.endswith(".gc.xml"), p.name)):
        cl = _read_codelist(path)
        kept = by_id.get(cl["id"])
        if kept is None:
            by_id[cl["id"]] = cl
            continue
        if (sorted(kept["codes"]), sorted(kept["retired"])) != (
            sorted(cl["codes"]),
            sorted(cl["retired"]),
        ):
            raise ValueError(f"{cl['id']}: {kept['file']} and {path.name} list different codes")
        notes.append(
            f"{cl['id']}: {path.name} lists the same codes as {kept['file']};"
            f" {kept['file']} is used."
        )
    used = [codelist_dir / cl["file"] for cl in by_id.values()]
    sets = sorted(p for p in codelist_dir.glob(CODELIST_SET_GLOB) if p.is_file())
    for set_path in sets:
        used.append(set_path)
        versions = _read_codelist_set(set_path)
        for oid, version_uri in versions.items():
            cl_id = _cl_id(oid)
            if cl_id not in by_id:
                raise ValueError(f"{set_path.name} names {cl_id} ({oid}); no code list file has it")
            if by_id[cl_id]["version_uri"] != version_uri:
                notes.append(
                    f"{cl_id}: {set_path.name} gives version {version_uri};"
                    f" {by_id[cl_id]['file']} gives {by_id[cl_id]['version_uri']}."
                    " The file is used."
                )
    for cl in by_id.values():
        if cl["retired"]:
            notes.append(
                f"{cl['id']}: codes {cl['retired']} have current flag 'n'; the check reports them"
                " as retired."
            )
    ordered = dict(sorted(by_id.items(), key=lambda kv: int(kv[0][2:])))
    return ordered, used, notes


# ---------------------------------------------------------------- reference instance


def _parse_with_comments(path: Path) -> ET.Element:
    parser = DefusedXMLParser(target=ET.TreeBuilder(insert_comments=True), forbid_dtd=True)
    parser.feed(path.read_bytes())
    return parser.close()


def _step(node: ET.Element) -> str:
    name = _local(node.tag)
    preds = []
    if name == "id" and node.get("root"):
        preds.append(f"[@root='{node.get('root')}']")
    code = _child(node, "code")
    if code is not None and code.get("code") and not _PLACEHOLDER.match(code.get("code", "")):
        preds.append(f"[code/@code='{code.get('code')}']")
    return name + "".join(preds)


def _value_step(node: ET.Element, element: str) -> str | None:
    """Where the element's value sits: the attribute or the text that holds the
    placeholder (the element id), else ``@value``, else the text."""
    attrs = {k: v for k, v in node.attrib.items() if not k.startswith("{")}
    for key, value in attrs.items():
        if value == element:
            return f"@{key}"
    for key, value in attrs.items():
        if element in value:
            return f"@{key}"
    text = (node.text or "").strip()
    if text == element:
        return "text()"
    if "value" in attrs:
        return "@value"
    if text:
        return "text()"
    return None


def _harvest(instance: Path, wanted: set[str]) -> tuple[dict[str, dict], str, str]:
    """Map each element id named in a comment to its paths and code system.

    Returns ``(found, root_tag, message_tag)``; paths start below the root.
    """
    root = _parse_with_comments(instance)
    parent = {c: p for p in root.iter() for c in p}
    found: dict[str, dict] = {}
    for holder in root.iter():
        previous = None
        for child in list(holder):
            if child.tag is ET.Comment:
                m = _COMMENT_ID.match(child.text or "")
                if m and m.group(1) in wanted:
                    target = previous if previous is not None else holder
                    steps = []
                    node: ET.Element | None = target
                    while node is not None and node is not root:
                        steps.append(_step(node))
                        node = parent.get(node)
                    steps.reverse()
                    entry = found.setdefault(m.group(1), {"paths": [], "code_system": None})
                    value = _value_step(target, m.group(1))
                    path = "/".join(steps + ([value] if value else []))
                    if path not in entry["paths"]:
                        entry["paths"].append(path)
                    if entry["code_system"] is None:
                        entry["code_system"] = target.get("codeSystem")
            elif isinstance(child.tag, str):
                previous = child
    tops = [p["paths"][0].split("/", 1)[0] for p in found.values()]
    message_tag = max(set(tops), key=tops.count) if tops else ""
    return found, _local(root.tag), message_tag


def _split(path: str) -> list[str]:
    return re.findall(r"[^/\[]+(?:\[[^\]]*\])*", path)


def _element_steps(path: str) -> list[str]:
    steps = _split(path)
    if steps and (steps[-1].startswith("@") or steps[-1] == "text()"):
        steps = steps[:-1]
    return steps


def _common_prefix(paths: Iterable[str]) -> str:
    split = [_element_steps(p) for p in paths]
    if not split:
        return ""
    prefix = []
    for column in zip(*split, strict=False):
        if len(set(column)) != 1:
            break
        prefix.append(column[0])
    return "/".join(prefix)


# ---------------------------------------------------------------- build


def _codelist_for(row: dict, located: dict | None, codelists: Mapping[str, dict]) -> str | None:
    by_oid = {cl["oid"]: cl_id for cl_id, cl in codelists.items()}
    if located is not None:
        return by_oid.get(located["code_system"] or "")
    candidates = [by_oid[o] for o in row["oids"] if o in by_oid]
    return candidates[0] if len(candidates) == 1 else None


def _scope(row: dict) -> str:
    return "batch" if row["element"].startswith("N.1.") else "message"


def _strip_scope(path: str, scope: str, message_tag: str) -> str:
    """Batch paths stay as they are; message paths drop the message step."""
    if scope == "message":
        head, _, rest = path.partition("/")
        if head != message_tag:
            raise ValueError(f"path {path!r} is not below the message element {message_tag!r}")
        return rest
    return path


def build_rules(
    xlsx: Path,
    codelist_dir: Path,
    out: Path,
    *,
    source_sha256: Mapping[str, str],
    reference_instance: Path | None = None,
) -> dict:
    """Convert the ICH business-rules sheet and code lists into the rule file at ``out``.

    ``source_sha256`` maps a source file name to its expected sha256 (from the download
    manifest). A file whose sha256 differs raises ``ValueError``. Every source file is
    recorded with its sha256. ``reference_instance`` gives the XML paths; without it,
    ``xpath`` is ``None``.
    """
    rows, notes = _read_sheet(xlsx)
    codelists, cl_files, cl_notes = _read_codelists(codelist_dir)
    notes = [
        "Generated by episteme.reference.rules.build_e2b from the ICH E2B(R3) IG package."
        " Do not edit by hand.",
        "Conformance 'Required' (D.1, H.1) is read as mandatory; 'Conditional-Mandatory' as"
        " conditional.",
        "Paths come from the comments of the ICH reference instance: each comment names the"
        " data element of the XML element before it. Paths of message elements start below"
        " the message element; batch paths start below the root. The last step is the"
        " attribute or text that holds the value.",
        "No XSD validation step. The example and reference instances of the IG package are"
        " the acceptance set.",
        "MedDRA codes are checked for shape (8 digits) and a present version only; no term lookup.",
        *notes,
        *cl_notes,
    ]
    sources = [("Core data elements and business rules", xlsx)]
    sources += [(f"Code list {p.name}", p) for p in cl_files]
    located_all: dict[str, dict] = {}
    root_tag = message_tag = None
    if reference_instance is not None:
        located_all, root_tag, message_tag = _harvest(
            reference_instance, {r["element"] for r in rows}
        )
        sources.append(("Reference instance", reference_instance))

    source_docs = []
    for title, path in sources:
        actual = sha256_file(path)
        expected = source_sha256.get(path.name)
        if expected is not None and expected.lower() != actual:
            raise ValueError(f"{path.name}: sha256 {actual} does not match expected {expected}")
        source_docs.append({"source": "ich", "file": path.name, "sha256": actual, "title": title})

    elements: list[dict[str, Any]] = []
    for row in rows:
        located = located_all.get(row["element"])
        scope = _scope(row)
        xpath = None
        if located is not None:
            xpath = [_strip_scope(p, scope, message_tag or "") for p in located["paths"]]
        codelist = _codelist_for(row, located, codelists)
        allowed = row["allowed"]
        values = None
        if codelist is None:
            values = sorted(set(_ENUM.findall(allowed)), key=int) or None
        booleans = None
        if row["format"] == "BL":
            booleans = [b for b in ("false", "true") if b in allowed.lower()] or None
        elements.append(
            {
                "element": row["element"],
                "name": row["name"],
                "block": row["block"],
                "scope": scope,
                "conformance": row["conformance"],
                "conformance_raw": row["conformance_raw"],
                "condition": row["rule"] or None,
                "length": row["length"],
                "format": row["format"],
                "codelist": codelist,
                "values": values,
                "booleans": booleans,
                "integer": bool(_INTEGER.search(row["rule"])) or None,
                "date_precision": _precision(row["rule"], allowed)
                if row["format"] == "DT"
                else None,
                "not_future": bool(_NOT_FUTURE.search(row["rule"])) or None,
                "xpath": xpath,
            }
        )
        if located is None and reference_instance is not None:
            notes.append(f"{row['element']}: not named in the reference instance; no path.")

    by_id = {e["element"]: e for e in elements}
    # Two elements on the same path (same scope, same value step) are not separable there.
    owners: dict[tuple[str, str], list[str]] = {}
    for e in elements:
        for p in e["xpath"] or ():
            owners.setdefault((e["scope"], p), []).append(e["element"])
    for e in elements:
        shared = sorted(
            {o for p in e["xpath"] or () for o in owners[(e["scope"], p)] if o != e["element"]}
        )
        e["ambiguous_with"] = shared or None

    blocks = _blocks(elements)
    rules, checked, rule_notes = _rules(rows, by_id)
    notes += rule_notes
    manual = [
        {"element": r["element"], "name": r["name"], "text": r["rule"]}
        for r in rows
        if CONFORMANCE[_norm(r["conformance_raw"]) or ""] == "conditional"
        and r["element"] not in checked
    ]
    data = {
        "ig": "ICH E2B(R3)",
        "version": "IG package v1.11; core data elements and business rules v1.00;"
        " code lists v2.11",
        "source_docs": source_docs,
        "notes": notes,
        "namespace": "urn:hl7-org:v3",
        "root_tag": root_tag,
        "message_tag": message_tag,
        "elements": [
            {k: v for k, v in e.items() if v is not None or k in CORE_KEYS} for e in elements
        ],
        "blocks": blocks,
        "rules": rules,
        "manual": manual,
        "codelists": {
            cl_id: {
                "oid": cl["oid"],
                "name": cl["name"],
                "version_uri": cl["version_uri"],
                "file": cl["file"],
                "codes": cl["codes"],
                "retired": cl["retired"],
            }
            for cl_id, cl in codelists.items()
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def _repeating(block: str) -> bool:
    return bool(re.search(r"\.(?:r|i|k)(?:\.|$)", block))


def _blocks(elements: list[dict]) -> dict[str, dict]:
    """One entry per repeating block that has a path: the common path of its members."""
    members: dict[str, list[dict]] = {}
    for e in elements:
        if e["block"] and _repeating(e["block"]) and e["xpath"]:
            members.setdefault(e["block"], []).append(e)
    out = {}
    for block, items in members.items():
        out[block] = {
            "path": _common_prefix(p for e in items for p in e["xpath"]),
            "members": [e["element"] for e in items],
        }
    return out


def _rules(rows: list[dict], by_id: Mapping[str, dict]) -> tuple[list[dict], set[str], list[str]]:
    """Recognise business-rule sentences. Return rules, the conditional elements whose
    condition is checked, and notes about rules that cannot be checked."""
    rules: list[dict] = []
    checked: set[str] = set()
    notes: list[str] = []
    meddra_versions: list[str] = []
    for row in rows:
        element, text = row["element"], row["rule"]
        if m := _REQUIRED_IF.search(text):
            rules.append(
                {
                    "kind": "required_if",
                    "element": element,
                    "if_element": m.group(1),
                    "equals": m.group(2),
                    "text": text,
                }
            )
            checked.add(element)
        if m := _SAME_AS.search(text):
            rules.append({"kind": "same_as", "element": element, "other": m.group(1), "text": text})
        if m := _EXACTLY_ONCE.search(text):
            rules.append(
                {"kind": "exactly_once", "element": element, "value": m.group(1), "text": text}
            )
            checked.add(element)
        if m := _NOT_BOTH.search(text):
            pair = [_by_name_in_block(rows, row["block"], m.group(i)) for i in (1, 2)]
            if None in pair or any(by_id[p]["ambiguous_with"] for p in pair if p):
                notes.append(
                    f"{element}: rule {m.group(0)!r} is not checked; the two identifiers share one"
                    " XML element or have no path."
                )
            else:
                rule = {"kind": "not_both", "elements": pair, "text": m.group(0)}
                if rule not in rules:
                    rules.append(rule)
        if _ONE_MEDDRA_VERSION.search(text):
            meddra_versions.append(element)
    if meddra_versions:
        rules.append(
            {
                "kind": "single_value",
                "elements": meddra_versions,
                "text": "Only 1 MedDRA version is allowed per ICSR.",
            }
        )
    for row in rows:
        if _MEDDRA_VERSION_NAME.match(row["name"]) and row["element"].endswith("a"):
            code = row["element"][:-1] + "b"
            if code in by_id:
                rules.append({"kind": "meddra", "code": code, "version": row["element"]})
    return rules, checked, notes


def _by_name_in_block(rows: list[dict], block: str | None, short: str) -> str | None:
    hits = [r["element"] for r in rows if r["block"] == block and f"({short})" in r["name"]]
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------- CLI


def _one(base: Path, pattern: str, what: str, *, directory: bool = False) -> Path:
    hits = sorted(p for p in base.rglob(pattern) if (p.is_dir() if directory else p.is_file()))
    if len(hits) != 1:
        raise SystemExit(f"expected one {what} ({pattern}) under {base}, found {len(hits)}")
    return hits[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--raw", type=Path, required=True, help="the unpacked IG package directory")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    xlsx = _one(args.raw, XLSX_GLOB, "business-rules spreadsheet")
    codelist_dir = _one(args.raw, CODELIST_DIR_GLOB, "code list directory", directory=True)
    instance = _one(args.raw, REFERENCE_GLOB, "ICSR reference instance")
    data = build_rules(xlsx, codelist_dir, args.out, source_sha256={}, reference_instance=instance)
    print(
        f"{args.out}: {len(data['elements'])} elements, {len(data['rules'])} rules,"
        f" {len(data['manual'])} manual, {len(data['codelists'])} code lists"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
