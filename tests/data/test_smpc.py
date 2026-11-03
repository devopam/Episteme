"""EPAR SmPC sections: the heading match, the sidecar and the label step for ``ema_epar``.

The unmarked tests build PDFs in the layout of the EMA SmPC template (no real EPAR is on
disk yet; the heading pattern is checked against real text at the sample gate). The
pg-marked test of a loaded part is in ``test_epar.py``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from episteme.data import load_articles, postgres_loader
from episteme.data.dailymed import labels
from episteme.data.ema import extract_epar as ex
from episteme.data.ema import smpc

from .test_dailymed_load import _Conn, _Cursor
from .test_epar import PREFIX, doc

ANNEX_I = ["ANNEX I", "SUMMARY OF PRODUCT CHARACTERISTICS"]


def smpc_block(name: str, *, with_annex_head: bool = True) -> list[str]:
    head = list(ANNEX_I) if with_annex_head else ["SUMMARY OF PRODUCT CHARACTERISTICS"]
    return [
        *head,
        "1. NAME OF THE MEDICINAL PRODUCT",
        f"{name} 10 mg tablets",
        "2. QUALITATIVE AND QUANTITATIVE COMPOSITION",
        "Each tablet contains 10 mg.",
        "4. CLINICAL PARTICULARS",
        "4.1 Therapeutic indications",
        f"{name} is indicated for hypertension.",
        "4.2 Posology and method of administration",
        "One tablet daily.",
        "4.5 Interaction with other medicinal products and other forms of interaction",
        "Avoid warfarin.",
        "4.7 Effects on ability to drive and use machines",
        "None known.",
        "5. PHARMACOLOGICAL PROPERTIES",
        "5.1 Pharmacodynamic properties",
        "A calcium channel blocker.",
        "7. MARKETING AUTHORISATION HOLDER",
        "Holder name, address.",
    ]


LEAFLET = [
    "ANNEX II",
    "A. MANUFACTURER(S) RESPONSIBLE FOR BATCH RELEASE",
    "ANNEX III",
    "LABELLING AND PACKAGE LEAFLET",
    "A. LABELLING",
    "B. PACKAGE LEAFLET",
    "Package leaflet: Information for the patient",
    "4. Possible side effects",
    "4.2 Not an SmPC heading in the leaflet",
    "Tell your doctor.",
]


def text_of(lines: list[str]) -> str:
    return "\n".join(lines)


def smpc_pdf(lines: list[str], per_page: int = 40) -> bytes:
    """A PDF with one text line per line of ``lines`` (Helvetica), pages of ``per_page``."""
    chunks = [lines[i : i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    objs: list[bytes] = []
    n = len(chunks)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, chunk in enumerate(chunks):
        body = ["BT /F1 10 Tf 14 TL 40 760 Td"]
        for ln in chunk:
            esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            body.append(f"({esc}) Tj T*")
        body.append("ET")
        stream = "\n".join(body).encode("latin-1")
        objs.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> " + f"/Contents {5 + 2 * i} 0 R >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, ob in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + ob + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    )
    return out.getvalue()


# ---------------------------------------------------------------------------
# the heading match
# ---------------------------------------------------------------------------
def test_sections_from_annex_one_only():
    body = text_of([*smpc_block("Foo"), *LEAFLET])
    rows = smpc.smpc_sections(body)
    assert [r["loinc_code"] for r in rows] == [
        "34067-9",
        "34068-7",
        "34073-7",
        "smpc:4.7",
        "43679-0",
    ]
    assert [r["ordinal"] for r in rows] == [1, 2, 3, 4, 5]
    assert set(rows[0]) == {
        "loinc_code",
        "title",
        "ordinal",
        "product",
        "start_offset",
        "end_offset",
    }
    assert rows[0]["title"] == "4.1 Therapeutic indications"
    assert rows[0]["product"] == "Foo 10 mg tablets"
    # the last section ends where the next top-level heading starts, not at Annex II
    last = body[rows[-1]["start_offset"] : rows[-1]["end_offset"]]
    assert last.endswith("A calcium channel blocker.")


def test_leaflet_headings_are_not_sections():
    assert smpc.smpc_sections(text_of(LEAFLET)) == []
    rows = smpc.smpc_sections(text_of([*smpc_block("Foo"), *LEAFLET]))
    assert not any("leaflet" in r["title"] or "side effects" in r["title"] for r in rows)
    assert [r["loinc_code"] for r in rows].count("34068-7") == 1  # the leaflet's 4.2 is not read


def test_two_smpcs_give_two_rows_per_section():
    body = text_of(
        [*smpc_block("Foo"), *smpc_block("Bar", with_annex_head=True), *LEAFLET],
    )
    rows = smpc.smpc_sections(body)
    assert len(rows) == 10
    foo = [r for r in rows if r["product"] == "Foo 10 mg tablets"]
    bar = [r for r in rows if r["product"] == "Bar 10 mg tablets"]
    assert len(foo) == len(bar) == 5
    assert [r["loinc_code"] for r in foo] == [r["loinc_code"] for r in bar]
    assert foo[0]["title"] == "4.1 Therapeutic indications - Foo 10 mg tablets"
    assert [r["ordinal"] for r in rows] == list(range(1, 11))
    # Foo's last section stops before Bar's Annex I
    last_foo = body[foo[-1]["start_offset"] : foo[-1]["end_offset"]]
    assert "Bar" not in last_foo and "ANNEX I" not in last_foo


def test_offsets_point_into_body_text():
    body = text_of([*smpc_block("Foo"), *LEAFLET])
    rows = smpc.smpc_sections(body)
    by = {r["loinc_code"]: r for r in rows}
    r = by["34073-7"]
    assert body[r["start_offset"] : r["end_offset"]] == (
        "4.5 Interaction with other medicinal products and other forms of interaction\n"
        "Avoid warfarin."
    )
    for r in rows:
        assert 0 <= r["start_offset"] < r["end_offset"] <= len(body)
        assert body[r["start_offset"] : r["end_offset"]].startswith(r["title"].split(" - ")[0])


def test_unmapped_heading_gets_smpc_code():
    rows = smpc.smpc_sections(text_of(smpc_block("Foo")))
    assert "smpc:4.7" in [r["loinc_code"] for r in rows]
    assert smpc.loinc_for("4.7") == "smpc:4.7" and smpc.loinc_for("4.5") == "34073-7"


def test_the_loinc_map_is_the_spec_table():
    assert smpc.SMPC_LOINC == {
        "4.1": "34067-9",
        "4.2": "34068-7",
        "4.3": "34070-3",
        "4.4": "43685-7",
        "4.5": "34073-7",
        "4.6": "42228-7",
        "4.8": "34084-4",
        "4.9": "34088-5",
        "5.1": "43679-0",
        "5.2": "43682-4",
    }


def test_no_headings_found_gives_no_rows_and_is_counted(tmp_path):
    assert smpc.smpc_sections("") == []
    assert smpc.smpc_sections("Just some text.\n4.1 Therapeutic indications\nx") == []
    raw, out = tmp_path / "raw", tmp_path / "out"
    (raw / "epar").mkdir(parents=True)
    docs = [doc(1), doc(2)]
    (raw / "ema_epar_documents.json").write_text(
        json.dumps({"meta": {"total_records": 2}, "data": docs}), encoding="utf-8"
    )
    (raw / "epar" / "1.pdf").write_bytes(smpc_pdf([*smpc_block("Foo"), *LEAFLET]))
    (raw / "epar" / "2.pdf").write_bytes(smpc_pdf(["Scientific text without any SmPC."]))
    res = ex.extract_epar(raw, out)
    assert res["documents_with_sections"] == 1
    assert res["documents_without_sections"] == 1
    assert PREFIX  # the shared feed prefix is the one the documents use


def test_a_wrong_order_or_repeat_is_not_a_second_section():
    lines = [
        *ANNEX_I,
        "4.2 Posology and method of administration",
        "x",
        "4.1 Therapeutic indications",
        "y",
        "4.2 Posology and method of administration",
        "z",
        "4.3 Contraindications",
        "w",
    ]
    rows = smpc.smpc_sections(text_of(lines))
    assert [r["loinc_code"] for r in rows] == ["34068-7", "34070-3"]


# ---------------------------------------------------------------------------
# extract: PDF text -> sidecar
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _small_buckets(monkeypatch):
    monkeypatch.setenv("EPISTEME_EPAR_BUCKET_IDS", "10")
    import episteme.config as cfg
    from episteme.data import bucket_parts

    for fn in {cfg.get_settings, ex.get_settings, bucket_parts.get_settings}:
        fn.cache_clear()
    yield
    for fn in {cfg.get_settings, ex.get_settings, bucket_parts.get_settings}:
        fn.cache_clear()


def _extract(tmp_path: Path, docs: list[dict], pdfs: dict[int, list[str]]):
    raw, out = tmp_path / "raw", tmp_path / "out"
    (raw / "epar").mkdir(parents=True, exist_ok=True)
    (raw / "ema_epar_documents.json").write_text(
        json.dumps({"meta": {"total_records": len(docs)}, "data": docs}), encoding="utf-8"
    )
    for i, lines in pdfs.items():
        (raw / "epar" / f"{i}.pdf").write_bytes(smpc_pdf(lines))
    return raw, out, ex.extract_epar(raw, out)


def test_the_extract_writes_a_sidecar_of_sections_that_match_the_stored_text(tmp_path):
    docs = [doc(1), doc(2, type_="assessment-report")]
    _, out, res = _extract(
        tmp_path,
        docs,
        {1: [*smpc_block("Foo"), *LEAFLET], 2: [*smpc_block("Baz")]},
    )
    assert res["ok"] == 1 and res["documents_with_sections"] == 1
    assert res["documents_without_sections"] == 0  # only product-information is counted
    part = "ema_epar_b0000__part00001"
    side = pq.read_table(labels.sidecar_path(out, part, source="ema_epar")).to_pylist()
    assert side and {r["article_id"] for r in side} == {"ema_epar:1"}  # no section for the EPAR
    assert {r["kind"] for r in side} == {"section"}
    body = {
        r["id"]: r["body_text"]
        for r in pq.read_table(out / "staging/ema_epar" / f"{part}.parquet").to_pylist()
    }
    for r in side:
        text = body[r["article_id"]][r["start_offset"] : r["end_offset"]]
        assert text.startswith(r["title"])
    assert any(
        r["loinc_code"] == "34073-7"
        and "Avoid warfarin." in body["ema_epar:1"][r["start_offset"] : r["end_offset"]]
        for r in side
    )
    assert labels.sidecar_path(out, part, source="ema_epar").parent.name == "ema_epar_labels"
    assert labels.sidecar_path(out, "x").parent.name == "dailymed_labels"  # DailyMed unchanged


def test_rule_version_in_fingerprint_of_product_information_only(monkeypatch):
    base = {
        "id": "1",
        "last_updated": "u",
        "key": "epar__1.pdf",
        "size": 1,
        "sha256": "s",
        "name": "n",
        "product_number": "p",
        "type": "product-information",
        "medicine_name": "m",
        "first_published": "f",
        "url": "u",
    }
    other = {**base, "id": "2", "type": "assessment-report"}
    pi_1, other_1 = ex._fingerprint_row(base), ex._fingerprint_row(other)
    assert smpc.SMPC_RULE_VERSION in pi_1 or any(
        isinstance(x, list) and smpc.SMPC_RULE_VERSION in x for x in pi_1
    )
    assert len(other_1) == 6  # the other types keep the earlier shape
    monkeypatch.setattr(ex.smpc, "SMPC_RULE_VERSION", smpc.SMPC_RULE_VERSION + 1)
    assert ex._fingerprint_row(base) != pi_1
    assert ex._fingerprint_row(other) == other_1


def test_a_rule_version_change_extracts_only_product_information_again(tmp_path, monkeypatch):
    docs = [doc(1), doc(11, type_="assessment-report")]
    pdfs = {1: [*smpc_block("Foo"), *LEAFLET], 11: ["Assessment text."]}
    raw, out, first = _extract(tmp_path, docs, pdfs)
    assert first["ok"] == 2
    again = ex.extract_epar(raw, out)
    assert again["rows"] == 0  # both buckets skipped: nothing was written again
    monkeypatch.setattr(ex.smpc, "SMPC_RULE_VERSION", smpc.SMPC_RULE_VERSION + 1)
    marker_b0 = ex.bucket_parts.read_bucket_marker(out, "ema_epar", "ema_epar_b0000")
    marker_b1 = ex.bucket_parts.read_bucket_marker(out, "ema_epar", "ema_epar_b0001")
    manifests = ex._read_manifests(out)
    assert ex._fingerprint_row(manifests[0][0]) != marker_b0["inputs"][0]
    assert ex._fingerprint_row(manifests[1][0]) == marker_b1["inputs"][0]
    third = ex.extract_epar(raw, out)
    assert third["rows"] == 1  # only the product-information bucket was written again


# ---------------------------------------------------------------------------
# the label step for ema_epar
# ---------------------------------------------------------------------------
def test_pre_load_runs_for_dailymed_and_ema_epar_only(monkeypatch):
    calls: list[tuple[str, Path]] = []

    def fake(conn, source, shard):
        calls.append((source, shard))
        return {"label_sections": 1}

    monkeypatch.setattr(labels, "replace_labels", fake)
    shard = Path("staging/ema_epar/ema_epar_b0000__part00001.parquet")
    assert load_articles._pre_load(object(), "ema_epar", shard) == {"label_sections": 1}
    for other in ("pmc", "pubmed", "pubchem", "bookshelf", "cdisc_ct", "fda_ndc"):
        assert load_articles._pre_load(object(), other, shard) == {}
    assert calls == [("ema_epar", shard)]


def test_retire_deletes_label_rows_for_ema_epar_too(monkeypatch):
    monkeypatch.setattr(postgres_loader.audit_trail, "record", lambda *a, **k: None)
    conn = _Conn()
    postgres_loader.delete_source_files(conn, source="ema_epar", source_files=["p1"])
    label_stmts = [i for i, s in enumerate(conn.log) if "label_" in s]
    articles_delete = next(
        i for i, s in enumerate(conn.log) if s.startswith("DELETE FROM episteme.articles")
    )
    assert len(label_stmts) == 2 and max(label_stmts) < articles_delete


def test_delete_labels_filters_on_the_source():
    seen: list[tuple[str, tuple]] = []

    class Cur(_Cursor):
        def execute(self, sql, params=None):
            seen.append((" ".join(str(sql).split()), params))

    labels.delete_labels(Cur([]), "ema_epar", ["p1", "p1"])
    assert all(p[0] == "ema_epar" for _, p in seen) and len(seen) == 2


def test_replace_labels_for_ema_epar_copies_sections_with_ordinal_and_product(tmp_path):
    shard = tmp_path / "staging" / "ema_epar" / "ema_epar_b0000__part00001.parquet"
    shard.parent.mkdir(parents=True)
    pq.write_table(pa.table({"id": ["ema_epar:1"]}), shard)
    labels.write_sidecar(
        tmp_path,
        shard.stem,
        [
            {
                "article_id": "ema_epar:1",
                "kind": "section",
                "loinc_code": "34073-7",
                "title": "4.5 Interactions",
                "ordinal": 3,
                "product": "Foo",
                "start_offset": 10,
                "end_offset": 20,
            }
        ],
        source="ema_epar",
    )
    copies: list[tuple[str, tuple]] = []

    class Copy:
        def __init__(self, sql):
            self.sql = " ".join(sql.split())

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write_row(self, row):
            copies.append((self.sql, tuple(row)))

    class Cur(_Cursor):
        def copy(self, sql):
            return Copy(sql)

    class Conn(_Conn):
        def cursor(self):
            return Cur(self.log)

    out = labels.replace_labels(Conn(), "ema_epar", shard)
    assert out["label_sections"] == 1 and out["label_substances"] == 0
    assert copies == [
        (
            "COPY episteme.label_section "
            "(article_id, loinc_code, title, start_offset, end_offset, ordinal, product) "
            "FROM STDIN",
            ("ema_epar:1", "34073-7", "4.5 Interactions", 10, 20, 3, "Foo"),
        )
    ]


def test_a_shard_without_an_epar_sidecar_is_refused(tmp_path):
    shard = tmp_path / "staging" / "ema_epar" / "ema_epar_b0000__part00001.parquet"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"x")
    with pytest.raises(FileNotFoundError, match="sidecar"):
        labels.replace_labels(_Conn(), "ema_epar", shard)
