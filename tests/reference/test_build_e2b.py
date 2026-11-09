"""The E2B(R3) rule-file converter: ICH business-rules sheet + code lists -> JSON."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import openpyxl
import pytest

from episteme.reference.rules import parse_rules
from episteme.reference.rules.build_e2b import build_rules

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "icsr"

# The two header rows of the real ICSR sheet (IG package v1.11), copied as they are.
ROW1 = ["Field Identification", None, None, None, "Business rules", None, None, None, None, None]
ROW1 += ["Q&A ", "NullFlavor Applicable", None, None, None, None, None, None, None, "Field OIDs"]
ROW2 = ["SOURCE", "HEADER ELEMENT", "DATA ELEMENT NUMBER", "DATA ELEMENT NAME", "MAX LENGTH"]
ROW2 += ["DATA TYPE ", "VALUE ALLOWED", "CONFORMANCE", "ICH BUSINESS RULE"]
ROW2 += ["REGIONAL BUSINESS RULE", None, "NI", "MSK", "UNK", "NA", "ASKU", "NASK", "NINF"]
ROW2 += ["PINF", "Code system OID"]
NO = ["No"] * 8


DATA = [
    ("N.1 ", "N.1.1", "Types of Message in batch", 2, "N", "1=ichicsr", "Mandatory ",
     "Note the 'Value Allowed' for this data element is case sensitive.",
     "ICH - 2.16.840.1.113883.3.989.2.1.1.1"),
    ("C.1 ", "C.1.3 ", "Type of Report", 1, "N", "1=Spontaneous Report  2=Report from study",
     "Mandatory", None, "2.16.840.1.113883.3.989.2.1.1.2"),
    ("C.5 ", "C.5.4", "Study Type Where Reaction(s) / Event(s) Were Observed", 1, "N",
     "1=Clinical trials", "Conditional-Mandatory",
     "Optional, but required if C.1.3=2(Report from study)", "-"),
    ("D. ", "D.2.1", "Date of Birth", "-", "Date/Time", "See Appendix II", "Optional",
     "Minimum precision required is the day (i.e. 'CCYYMMDD').\n"
     "The date specified cannot refer to a future date.", "-"),
    ("D. ", "D.2.2a ", "Age at Time of Onset of Reaction / Event (number)", 5, "N", "Numeric",
     "Conditional-Mandatory", None, "-"),
]  # fmt: skip


def write_xlsx(path: Path, *, row1=ROW1, row2=ROW2, extra=None) -> Path:
    wb = openpyxl.Workbook()
    wb.active.title = "Read me"
    ws = wb.create_sheet("ICSR")
    ws.append(row1)
    ws.append(row2)
    for block, element, name, length, dtype, allowed, conf, rule, oid in DATA:
        row = ["ICH", block, element, name, length, dtype, allowed, conf, rule, None, None]
        ws.append(row + NO + [oid] + (extra or []))
    wb.save(path)
    return path


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path]:
    xlsx = write_xlsx(tmp_path / "business_rules_mini.xlsx")
    cl_dir = tmp_path / "codelists"
    cl_dir.mkdir()
    shutil.copy(FIXTURES / "cl_mini.gc.xml", cl_dir / "E2B CL1 ich-type-of-message-in-batch.gc.xml")
    return xlsx, cl_dir


def _by_element(data: dict) -> dict[str, dict]:
    return {e["element"]: e for e in data["elements"]}


def test_build_reads_mandatory_and_length(inputs, tmp_path):
    xlsx, cl_dir = inputs
    data = build_rules(xlsx, cl_dir, tmp_path / "out.json", source_sha256={})
    els = _by_element(data)
    assert list(els) == ["N.1.1", "C.1.3", "C.5.4", "D.2.1", "D.2.2a"]
    assert els["N.1.1"]["conformance"] == "mandatory"
    assert els["N.1.1"]["length"] == 2
    assert els["N.1.1"]["format"] == "N"
    assert els["C.5.4"]["conformance"] == "conditional"
    assert els["C.5.4"]["condition"] == "Optional, but required if C.1.3=2(Report from study)"
    assert els["D.2.1"]["format"] == "DT"
    assert els["D.2.1"]["length"] is None
    assert els["D.2.1"]["date_precision"] == 8
    assert els["D.2.1"]["not_future"] is True
    assert els["N.1.1"]["xpath"] is None  # no reference instance given
    # The recognised condition is a rule; the unrecognised one is listed as manual.
    required_if = [r for r in data["rules"] if r["kind"] == "required_if"]
    assert [(r["element"], r["if_element"], r["equals"]) for r in required_if] == [
        ("C.5.4", "C.1.3", "2")
    ]
    assert [m["element"] for m in data["manual"]] == ["D.2.2a"]
    # The output file is the returned data and loads as a rule set.
    on_disk = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert on_disk == data
    assert parse_rules("ich_e2b_r3", on_disk).ig == "ICH E2B(R3)"


def test_build_attaches_codelist_codes(inputs, tmp_path):
    xlsx, cl_dir = inputs
    data = build_rules(xlsx, cl_dir, tmp_path / "out.json", source_sha256={})
    els = _by_element(data)
    assert els["N.1.1"]["codelist"] == "CL1"
    assert els["C.1.3"]["codelist"] is None  # CL2 is not in the mini package
    assert els["C.1.3"]["values"] == ["1", "2"]  # from VALUE ALLOWED
    cl1 = data["codelists"]["CL1"]
    assert cl1["oid"] == "2.16.840.1.113883.3.989.2.1.1.1"
    assert cl1["codes"] == ["1"]
    assert cl1["retired"] == ["9"]


def test_build_rejects_changed_header(inputs, tmp_path):
    xlsx, cl_dir = inputs
    renamed = list(ROW2)
    renamed[4] = "LENGTH"
    bad = write_xlsx(tmp_path / "renamed.xlsx", row2=renamed)
    with pytest.raises(ValueError, match=r"column E .*'length'"):
        build_rules(bad, cl_dir, tmp_path / "out.json", source_sha256={})
    moved = list(ROW2)
    moved[5], moved[6] = moved[6], moved[5]
    bad = write_xlsx(tmp_path / "moved.xlsx", row2=moved)
    with pytest.raises(ValueError, match=r"column F"):
        build_rules(bad, cl_dir, tmp_path / "out.json", source_sha256={})
    extra = write_xlsx(tmp_path / "extra.xlsx", row2=ROW2 + ["EU RULE"])
    with pytest.raises(ValueError, match=r"unknown column U 'eu rule'"):
        build_rules(extra, cl_dir, tmp_path / "out.json", source_sha256={})


def test_build_ignores_stray_cell_without_header(inputs, tmp_path):
    _, cl_dir = inputs
    xlsx = write_xlsx(tmp_path / "stray.xlsx", extra=[None, None, "X-Error in E2B IG"])
    data = build_rules(xlsx, cl_dir, tmp_path / "out.json", source_sha256={})
    assert any("X-Error in E2B IG" in n for n in data["notes"])


def test_build_records_source_sha256(inputs, tmp_path):
    xlsx, cl_dir = inputs
    digest = hashlib.sha256(xlsx.read_bytes()).hexdigest()
    data = build_rules(xlsx, cl_dir, tmp_path / "out.json", source_sha256={xlsx.name: digest})
    docs = {d["file"]: d for d in data["source_docs"]}
    assert docs[xlsx.name]["sha256"] == digest
    cl_file = "E2B CL1 ich-type-of-message-in-batch.gc.xml"
    assert docs[cl_file]["sha256"] == hashlib.sha256((cl_dir / cl_file).read_bytes()).hexdigest()
    assert all(not Path(d["file"]).is_absolute() for d in data["source_docs"])
    with pytest.raises(ValueError, match="does not match"):
        build_rules(xlsx, cl_dir, tmp_path / "out.json", source_sha256={xlsx.name: "0" * 64})


REFERENCE = """<?xml version="1.0" encoding="UTF-8"?>
<MCCI_IN200100UV01 xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <name code="N.1.1" codeSystem="2.16.840.1.113883.3.989.2.1.1.1"/>
  <!-- N.1.1: Type of Messages in Batch -->
  <PORR_IN049016UV>
    <controlActProcess>
      <subject>
        <investigationEvent>
          <subjectOf2>
            <investigationCharacteristic>
              <code code="1" codeSystem="2.16.840.1.113883.3.989.2.1.1.23"/>
              <value xsi:type="CE" code="C.1.3" codeSystem="2.16.840.1.113883.3.989.2.1.1.2"/>
              <!-- C.1.3: Type of Report -->
            </investigationCharacteristic>
          </subjectOf2>
          <subjectOf2>
            <observation>
              <code code="3" codeSystem="2.16.840.1.113883.3.989.2.1.1.19"/>
              <value xsi:type="PQ" value="10" unit="a"/>
              <!-- D.2.2a: Age at Time of Onset (number) -->
            </observation>
          </subjectOf2>
        </investigationEvent>
      </subject>
    </controlActProcess>
  </PORR_IN049016UV>
</MCCI_IN200100UV01>
"""


def test_build_paths_from_reference_instance(inputs, tmp_path):
    xlsx, cl_dir = inputs
    ref = tmp_path / "ref.xml"
    ref.write_text(REFERENCE, encoding="utf-8")
    data = build_rules(
        xlsx, cl_dir, tmp_path / "out.json", source_sha256={}, reference_instance=ref
    )
    els = _by_element(data)
    assert data["root_tag"] == "MCCI_IN200100UV01"
    assert data["message_tag"] == "PORR_IN049016UV"
    assert els["N.1.1"]["scope"] == "batch"
    assert els["N.1.1"]["xpath"] == ["name/@code"]
    assert els["C.1.3"]["xpath"] == [
        "controlActProcess/subject/investigationEvent/subjectOf2"
        "/investigationCharacteristic[code/@code='1']/value/@code"
    ]
    assert els["D.2.2a"]["xpath"] == [
        "controlActProcess/subject/investigationEvent/subjectOf2/observation[code/@code='3']"
        "/value/@value"
    ]
    assert els["C.5.4"]["xpath"] is None
    assert "ref.xml" in {d["file"] for d in data["source_docs"]}
