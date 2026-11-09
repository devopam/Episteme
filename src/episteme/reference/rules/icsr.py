"""E2B(R3) ICSR checks (spec section 7), driven by the generated rule file
``data/ich_e2b_r3.json`` (see ``build_e2b``).

Scope: mandatory elements; the conditional rules the ICH sheet states in a checkable
form (the others are listed under ``"manual"`` in the rule file and not checked); code
lists; lengths and formats; date precision; batch and message header consistency;
MedDRA code shape (8 digits) and a present MedDRA version. No MedDRA term lookup and no
XSD step.

Each ``Finding`` has ``rule_id`` = the data element (``"C.1.3"``) or ``"ICSR"`` for the
whole document, and ``clause`` = the kind of check: ``parse``, ``structure``, ``region``,
``mandatory``, ``conditional``, ``codelist``, ``value``, ``length``, ``format``,
``date``, ``consistency`` or ``meddra``.
"""

from __future__ import annotations

import calendar
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Literal

import defusedxml.ElementTree as SafeET
from defusedxml import DefusedXmlException

from . import Finding, load_rules

RULE_SET = "ich_e2b_r3"
REGIONS_NOT_LOADED = {
    "eu": "EU regional rules not loaded",
    "fda": "FDA regional rules not loaded",
}
_STEP = re.compile(r"[^/\[]+(?:\[[^\]]*\])*")
_PRED_ROOT = re.compile(r"\[@root='([^']*)'\]")
_PRED_CODE = re.compile(r"\[code/@code='([^']*)'\]")
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")
_INTEGER = re.compile(r"[+-]?\d+")
_ALPHA = re.compile(r"[A-Za-z]+")
_DATE = re.compile(r"(\d{4,14})(\.\d+)?(?:[+-]\d{2}(?:\d{2})?)?")
_MEDDRA_CODE = re.compile(r"\d{8}")
# The sheet says "N.N"; the ICH example instances send "15". Both are accepted.
_MEDDRA_VERSION = re.compile(r"\d+(?:\.\d+)?")
_PRECISION_NAME = {4: "the year", 6: "the month", 8: "the day", 14: "the second"}


@dataclass(frozen=True)
class _Step:
    tag: str
    root: str | None
    code: str | None


@dataclass(frozen=True)
class _Path:
    text: str
    steps: tuple[_Step, ...]
    value: str | None  # "@attr", "text()" or None (the element itself)


def _parse_path(text: str, ns: str) -> _Path:
    parts = _STEP.findall(text)
    value = None
    if parts and (parts[-1].startswith("@") or parts[-1] == "text()"):
        value = parts.pop()
    steps = []
    for part in parts:
        root, code = _PRED_ROOT.search(part), _PRED_CODE.search(part)
        steps.append(
            _Step(
                f"{{{ns}}}{part.split('[', 1)[0]}",
                root.group(1) if root else None,
                code.group(1) if code else None,
            )
        )
    return _Path(text, tuple(steps), value)


@dataclass(frozen=True)
class _Block:
    steps: tuple[_Step, ...]
    members: tuple[str, ...]  # elements inside one occurrence
    outside: tuple[str, ...]  # elements of the block that sit elsewhere (G.k.1)


@dataclass(frozen=True)
class _Rules:
    ns: str
    root_tag: str
    message_tag: str
    elements: Mapping[str, Mapping[str, Any]]
    paths: Mapping[str, tuple[_Path, ...]]
    shared: frozenset[tuple[str, str]]  # (scope, path) owned by more than one element
    blocks: Mapping[str, _Block]
    codelists: Mapping[str, Mapping[str, Any]]
    by_oid: Mapping[str, str]
    rules: tuple[Mapping[str, Any], ...]
    meddra: frozenset[str]


@lru_cache(maxsize=1)
def _rules() -> _Rules:
    data = load_rules(RULE_SET).data
    ns = data["namespace"]
    elements = {e["element"]: e for e in data["elements"]}
    paths = {
        eid: tuple(_parse_path(p, ns) for p in e["xpath"] or ()) for eid, e in elements.items()
    }
    owners: dict[tuple[str, str], int] = {}
    for eid, e in elements.items():
        for p in paths[eid]:
            owners[(e["scope"], p.text)] = owners.get((e["scope"], p.text), 0) + 1
    blocks = {
        name: _Block(
            _parse_path(b["path"], ns).steps, tuple(b["members"]), tuple(b.get("outside", ()))
        )
        for name, b in data["blocks"].items()
    }
    meddra = frozenset(
        e for r in data["rules"] if r["kind"] == "meddra" for e in (r["code"], r["version"])
    )
    return _Rules(
        ns=ns,
        root_tag=f"{{{ns}}}{data['root_tag']}",
        message_tag=f"{{{ns}}}{data['message_tag']}",
        elements=elements,
        paths=paths,
        shared=frozenset(k for k, n in owners.items() if n > 1),
        blocks=blocks,
        codelists=data["codelists"],
        by_oid={cl["oid"]: cl_id for cl_id, cl in data["codelists"].items()},
        rules=tuple(data["rules"]),
        meddra=meddra,
    )


def _has_code(node: ET.Element, code: str, ns: str) -> bool:
    return any(c.tag == f"{{{ns}}}code" and c.get("code") == code for c in node)


def _match(start: ET.Element, steps: Iterable[_Step], ns: str) -> list[ET.Element]:
    nodes = [start]
    for st in steps:
        nodes = [
            c
            for n in nodes
            for c in n
            if c.tag == st.tag
            and (st.root is None or c.get("root") == st.root)
            and (st.code is None or _has_code(c, st.code, ns))
        ]
    return nodes


def _value(node: ET.Element, spec: str | None) -> str | None:
    if spec is None:
        return None
    raw = node.text if spec == "text()" else node.get(spec[1:])
    raw = (raw or "").strip()
    return raw or None


@dataclass
class _Hit:
    node: ET.Element
    path: _Path
    shared: bool

    @property
    def null(self) -> bool:
        return self.node.get("nullFlavor") is not None

    @property
    def value(self) -> str | None:
        return _value(self.node, self.path.value)

    @property
    def present(self) -> bool:
        return self.null or self.path.value is None or self.value is not None


class _Checker:
    def __init__(self, rules: _Rules, today: date, prefix: str) -> None:
        self.r = rules
        self.today = today.strftime("%Y%m%d")
        self.prefix = prefix
        self.findings: list[Finding] = []

    def add(self, rule_id: str, kind: str, message: str) -> None:
        self.findings.append(Finding(rule_id, kind, self.prefix + message))

    def label(self, eid: str) -> str:
        return f"{eid} ({self.r.elements[eid]['name']})"

    def hits(self, eid: str, start: ET.Element, below: tuple[_Step, ...] = ()) -> list[_Hit]:
        """Hits of ``eid`` under ``start``; with ``below`` (a block path), ``start`` is a
        block occurrence and only the element's paths inside the block are used."""
        e = self.r.elements[eid]
        out = []
        for p in self.r.paths[eid]:
            if p.steps[: len(below)] != below:
                continue
            for node in _match(start, p.steps[len(below) :], self.r.ns):
                out.append(_Hit(node, p, (e["scope"], p.text) in self.r.shared))
        return out

    # -- per scope (the batch root, or one message) --------------------------------

    def run(self, scope: str, node: ET.Element) -> None:
        found = {
            eid: self.hits(eid, node)
            for eid, e in self.r.elements.items()
            if e["scope"] == scope and self.r.paths[eid]
        }
        self.mandatory(scope, node, found)
        checked: set[int] = set()
        for eid, hits in found.items():
            self.element_values(eid, hits, checked)
        self.codelist_scan(node, found, checked, scope)
        for rule in self.r.rules:
            self.rule(rule, found)

    def mandatory(self, scope: str, node: ET.Element, found: Mapping[str, list[_Hit]]) -> None:
        for eid, hits in found.items():
            e = self.r.elements[eid]
            if e["conformance"] != "mandatory":
                continue
            block = (e.get("block") or "").strip()
            if block in self.r.blocks:
                self.mandatory_in_block(eid, block, node)
            elif not any(h.present for h in hits):
                self.add(eid, "mandatory", self.missing(eid, hits))

    def missing(self, eid: str, hits: list[_Hit], where: str = "") -> str:
        if hits:
            return f"mandatory element {self.label(eid)}{where} has no value and no nullFlavor"
        return f"missing mandatory element {self.label(eid)}{where}"

    def mandatory_in_block(self, eid: str, block: str, node: ET.Element) -> None:
        b = self.r.blocks[block]
        occurrences = _match(node, b.steps, self.r.ns)  # one node per repeat
        if not occurrences:
            self.add(eid, "mandatory", self.missing(eid, [], f": no {block} block"))
            return
        if eid in b.outside:
            # Not inside the repeat: it must occur at least once per occurrence.
            hits = self.hits(eid, node)
            count = sum(h.present for h in hits)
            if count < len(occurrences):
                self.add(
                    eid,
                    "mandatory",
                    f"mandatory element {self.label(eid)} occurs {count} times for"
                    f" {len(occurrences)} {block} blocks",
                )
            return
        for n, occ in enumerate(occurrences, start=1):
            hits = self.hits(eid, occ, b.steps)
            if not any(h.present for h in hits):
                self.add(eid, "mandatory", self.missing(eid, hits, f" in {block} #{n}"))

    def element_values(self, eid: str, hits: list[_Hit], checked: set[int]) -> None:
        e = self.r.elements[eid]
        for h in hits:
            if h.shared or h.null or h.path.value is None:
                continue
            v = h.value
            if v is None:
                continue
            if h.path.value == "@code":
                checked.add(id(h.node))
            if e.get("codelist"):
                self.code_in_list(eid, e["codelist"], v)
            elif e.get("values") and v not in e["values"]:
                self.add(eid, "value", f"{self.label(eid)}: {v!r} is not one of {e['values']}")
            if eid in self.r.meddra:
                continue  # shape checked by the MedDRA rule
            if e.get("length") and len(v) > e["length"]:
                self.add(
                    eid,
                    "length",
                    f"{self.label(eid)}: {len(v)} characters; at most {e['length']} allowed",
                )
            self.format(eid, e, v)

    def code_in_list(self, rule_id: str, cl_id: str, code: str) -> None:
        cl = self.r.codelists[cl_id]
        if code in cl["codes"]:
            return
        what = self.label(rule_id) if rule_id in self.r.elements else rule_id
        state = "a retired code of" if code in cl["retired"] else "not in"
        self.add(rule_id, "codelist", f"{what}: code {code!r} is {state} {cl_id} ({cl['name']})")

    def codelist_scan(
        self, node: ET.Element, found: Mapping[str, list[_Hit]], checked: set[int], scope: str
    ) -> None:
        """Every coded node whose ``codeSystem`` is an ICH code list must use a code of it."""
        owner = {id(h.node): eid for eid, hits in found.items() for h in hits}
        for n in self._scope_nodes(node, scope):
            cl_id = self.r.by_oid.get(n.get("codeSystem") or "")
            code = n.get("code")
            if cl_id is None or code is None or id(n) in checked:
                continue
            local = n.tag.rsplit("}", 1)[-1]
            self.code_in_list(owner.get(id(n), f"{cl_id} on <{local}>"), cl_id, code)

    def _scope_nodes(self, node: ET.Element, scope: str) -> Iterable[ET.Element]:
        if scope == "message":
            yield from node.iter()
            return
        for child in node:  # the batch: everything outside the messages
            if child.tag != self.r.message_tag:
                yield from child.iter()

    def format(self, eid: str, e: Mapping[str, Any], v: str) -> None:
        fmt = e.get("format")
        if fmt == "N":
            pattern = _INTEGER if e.get("integer") else _NUMBER
            if not pattern.fullmatch(v):
                kind = "a whole number" if e.get("integer") else "a number"
                self.add(eid, "format", f"{self.label(eid)}: {v!r} is not {kind}")
        elif fmt == "A" and not _ALPHA.fullmatch(v):
            self.add(eid, "format", f"{self.label(eid)}: {v!r} is not alphabetic")
        elif fmt == "BL" and e.get("booleans") and v not in e["booleans"]:
            self.add(eid, "value", f"{self.label(eid)}: {v!r} is not one of {e['booleans']}")
        elif fmt == "DT":
            self.date(eid, e, v)

    def date(self, eid: str, e: Mapping[str, Any], v: str) -> None:
        m = _DATE.fullmatch(v)
        if (
            not m
            or len(m.group(1)) not in (4, 6, 8, 10, 12, 14)
            or (m.group(2) and len(m.group(1)) != 14)
            or not _calendar_ok(m.group(1))
        ):
            self.add(eid, "format", f"{self.label(eid)}: {v!r} is not an HL7 date/time")
            return
        digits = m.group(1)
        need = e.get("date_precision")
        if need and len(digits) < need:
            self.add(
                eid,
                "date",
                f"{self.label(eid)}: {v!r} is too coarse; minimum precision is"
                f" {_PRECISION_NAME.get(need, f'{need} digits')}",
            )
        day = digits[:8]
        if e.get("not_future") and day > self.today[: len(day)]:
            self.add(eid, "date", f"{self.label(eid)}: {v!r} is a future date")

    # -- cross-element rules ------------------------------------------------------

    def values(self, found: Mapping[str, list[_Hit]], eid: str) -> list[str]:
        return [v for h in found.get(eid, ()) if not h.shared and (v := h.value) is not None]

    def rule(self, rule: Mapping[str, Any], found: Mapping[str, list[_Hit]]) -> None:
        kind = rule["kind"]
        if kind == "required_if":
            eid, other = rule["element"], rule["if_element"]
            if eid not in found or other not in found:
                return
            if rule["equals"] in self.values(found, other) and not any(
                h.present for h in found[eid]
            ):
                self.add(
                    eid,
                    "conditional",
                    f"{self.label(eid)} is required when {other} = {rule['equals']}",
                )
        elif kind == "same_root":
            eid, other = rule["element"], rule["other"]
            if eid not in found or other not in found:
                return
            mine = {h.node.get("root") for h in found[eid]}
            theirs = {h.node.get("root") for h in found[other]}
            if mine and theirs and mine != theirs:
                self.add(
                    eid,
                    "consistency",
                    f"{self.label(eid)} root {sorted(mine)} differs from {other} {sorted(theirs)}",
                )
        elif kind == "same_as":
            eid, other = rule["element"], rule["other"]
            if eid not in found or other not in found:
                return
            mine, theirs = self.values(found, eid), self.values(found, other)
            if mine and theirs and set(mine) != set(theirs):
                self.add(
                    eid,
                    "consistency",
                    f"{self.label(eid)} is {mine[0]!r}; it must be the same as {other}"
                    f" ({theirs[0]!r})",
                )
        elif kind == "exactly_once":
            eid = rule["element"]
            if eid not in found:
                return
            count = self.values(found, eid).count(rule["value"])
            if count != 1:
                self.add(
                    eid,
                    "conditional",
                    f"{self.label(eid)} must be {rule['value']!r} exactly once; found {count}",
                )
        elif kind == "not_both":
            a, b = rule["elements"]
            if a in found and b in found and self.values(found, a) and self.values(found, b):
                self.add(a, "conditional", f"{self.label(a)} and {b} must not both be given")
        elif kind == "single_value":
            seen = {v for eid in rule["elements"] if eid in found for v in self.values(found, eid)}
            if len({_number(v) for v in seen}) > 1:  # '15' and '15.0' are one version
                self.add(
                    rule["elements"][0],
                    "consistency",
                    f"{rule['text']} Found versions {sorted(seen)}",
                )
        elif kind == "meddra":
            self.meddra(rule["code"], rule["version"], found)

    def meddra(self, code_id: str, version_id: str, found: Mapping[str, list[_Hit]]) -> None:
        if code_id not in found:
            return
        version_paths = {p.steps: p for p in self.r.paths.get(version_id, ())}
        for h in found[code_id]:
            code = h.value
            if h.shared or code is None:
                continue
            if not _MEDDRA_CODE.fullmatch(code):
                self.add(
                    code_id, "meddra", f"{self.label(code_id)}: {code!r} is not an 8-digit code"
                )
            vp = version_paths.get(h.path.steps)
            version = (
                _value(h.node, vp.value) if vp else next(iter(self.values(found, version_id)), None)
            )
            if version is None:
                self.add(
                    version_id,
                    "meddra",
                    f"{self.label(version_id)} is missing for MedDRA code {code!r}",
                )
            elif not _MEDDRA_VERSION.fullmatch(version):
                self.add(
                    version_id,
                    "meddra",
                    f"{self.label(version_id)}: {version!r} is not a MedDRA version",
                )


def _number(v: str) -> Decimal | str:
    try:
        return Decimal(v).normalize()
    except InvalidOperation:
        return v


def _calendar_ok(digits: str) -> bool:
    """Month, day, hour, minute and second in range (as far as the digits go)."""
    parts = [int(digits[:4])] + [int(digits[i : i + 2]) for i in range(4, len(digits), 2)]
    limits = [(1, 12), (1, 31), (0, 23), (0, 59), (0, 59)]
    for value, (low, high) in zip(parts[1:], limits, strict=False):
        if not low <= value <= high:
            return False
    if len(parts) >= 3:
        return parts[2] <= calendar.monthrange(parts[0], parts[1])[1]
    return True


def check_icsr(
    xml_bytes: bytes,
    *,
    region: Literal["ich", "fda", "eu"] = "ich",
    today: date | None = None,
) -> list[Finding]:
    """Check one ICSR batch (``MCCI_IN200100UV01``) against the ICH E2B(R3) rules.

    Never raises on bad input: malformed XML, a DTD or an entity gives one ``parse``
    finding. ``region="eu"`` / ``"fda"`` gives one ``region`` finding until regional
    rules exist. ``today`` sets the reference for the no-future-date rule.
    """
    if region in REGIONS_NOT_LOADED:
        return [Finding("ICSR", "region", REGIONS_NOT_LOADED[region])]
    if region != "ich":
        raise ValueError(f"unknown region {region!r}; expected ich, fda or eu")
    try:
        root = SafeET.fromstring(xml_bytes, forbid_dtd=True)
    except DefusedXmlException as exc:
        return [Finding("ICSR", "parse", f"XML refused: {type(exc).__name__}: {exc}")]
    except (ET.ParseError, LookupError, ValueError) as exc:
        # ValueError and LookupError: a bad or unknown encoding declaration.
        return [Finding("ICSR", "parse", f"XML is not well-formed: {exc}")]
    rules = _rules()
    if root.tag != rules.root_tag:
        return [
            Finding(
                "ICSR",
                "structure",
                f"root element is {root.tag!r}; an ICSR batch is {rules.root_tag!r}",
            )
        ]
    today = today or date.today()
    batch = _Checker(rules, today, "")
    batch.run("batch", root)
    findings = batch.findings
    messages = [m for m in root if m.tag == rules.message_tag]
    if not messages:
        findings.append(Finding("ICSR", "structure", "the batch holds no ICSR message"))
    for n, message in enumerate(messages, start=1):
        checker = _Checker(rules, today, f"message {n}: " if len(messages) > 1 else "")
        checker.run("message", message)
        findings += checker.findings
    return findings
