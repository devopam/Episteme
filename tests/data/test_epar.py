"""EPAR documents: select, download (fake responses only), PDF text, extract in parts.

No test calls EMA: the sessions are fakes and the PDFs are built here, with ``pypdf`` for
a blank one and by hand for the ones that hold text. The pg-marked tests at the end run
against ``episteme_test`` only; their schema is built once per module.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from episteme.data import parts_retire
from episteme.data.checkpoint_markers import (
    failed_marker_path,
    load_success_marker_path,
    success_marker_path,
)
from episteme.data.ema import download_epar as dl
from episteme.data.ema import extract_epar as ex
from episteme.data.regulatory.download_ema import FeedVerificationError

from .test_download_ema import FakeResp, FakeSession

PREFIX = "https://www.ema.europa.eu/"
URL = PREFIX + "en/documents/product-information/{slug}_en.pdf"
TYPES = ("product-information", "assessment-report", "rmp-summary")
BIG = 10**12


# ---------------------------------------------------------------------------
# fixtures and builders
# ---------------------------------------------------------------------------
def _clear_settings() -> None:
    """Drop the cached Settings everywhere they are bound. Other tests reload
    ``episteme.config``, which rebinds ``get_settings`` while the modules under test keep the
    function they imported."""
    import episteme.config as cfg
    from episteme.data import bucket_parts

    for fn in {cfg.get_settings, ex.get_settings, dl.get_settings, bucket_parts.get_settings}:
        fn.cache_clear()


@pytest.fixture(autouse=True)
def _settings_cache():
    _clear_settings()
    yield
    _clear_settings()


def _env(monkeypatch, name: str, value) -> None:
    monkeypatch.setenv(name, str(value))
    _clear_settings()


def make_pdf(pages: list[str]) -> bytes:
    """A small PDF with one line of text per page (Helvetica), built by hand."""
    objs: list[bytes] = []
    n_pages = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({esc}) Tj ET".encode("latin-1")
        objs.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> " + f"/Contents {5 + 2 * i} 0 R >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    )
    return out.getvalue()


def blank_pdf(pages: int = 2) -> bytes:
    """A PDF with pages and no text at all: what an image-only scan looks like to a text reader."""
    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def doc(i: int, *, type_: str = "product-information", updated: str = "2026-01-01T00:00:00Z", **kw):
    return {
        "id": str(i),
        "name": f"Medicine{i} : EPAR - Product information",
        "type": type_,
        "medicine_name": f"Medicine{i}",
        "ema_product_number": f"EMEA/H/C/{i:06d}",
        "status": "unknown",
        "first_published_date": "2020-05-17T10:00:00Z",
        "last_updated_date": updated,
        "reference_number": "",
        "document_url": URL.format(slug=f"medicine{i}"),
        "translations": {"de": PREFIX + f"de/documents/x/medicine{i}_de.pdf"},
        **kw,
    }


# ---------------------------------------------------------------------------
# select
# ---------------------------------------------------------------------------
def test_select_documents_keeps_only_listed_types_and_english_urls():
    rows = [
        doc(1),
        doc(2, type_="assessment-report"),
        doc(3, type_="rmp-summary"),
        doc(4, type_="overview"),  # a type that is not in the list
        doc(5, type_="variation-report"),
        doc(6, document_url=URL.format(slug="x").replace("_en.pdf", "_de.pdf")),  # not English
        doc(7, document_url=URL.format(slug="x").replace("_en.pdf", "_en.docx")),
        doc(8, document_url=URL.format(slug="x") + "-0"),
        doc(9, document_url="https://evil.example/en/documents/a/b_en.pdf"),
        doc(10, document_url="http://www.ema.europa.eu/en/documents/a/b_en.pdf"),  # not https
        doc(11, document_url=PREFIX.rstrip("/") + ".evil.example/a_en.pdf"),
        doc(12, document_url=None),
        doc(13, id="../../etc/passwd"),
        doc(14, id="007"),
        doc(15, id=""),
    ]
    stats: dict = {}
    got = list(dl.select_documents(rows, TYPES, stats=stats))
    assert [d["id"] for d in got] == ["1", "2", "3"]
    assert stats["rows"] == 15
    assert stats["other_type"] == 2
    assert stats["refused_url"] == 7  # six urls and a missing one
    assert stats["refused_id"] == 3


def test_select_documents_uses_the_allowed_prefix_argument():
    rows = [doc(1), doc(2, document_url="https://example.org/a_en.pdf")]
    got = list(dl.select_documents(rows, TYPES, prefix="https://example.org/"))
    assert [d["id"] for d in got] == ["2"]


def test_the_types_come_from_feeds_toml():
    from episteme.data.regulatory.feeds import load_feeds

    feed = load_feeds()["ema_epar_documents"]
    assert feed.document_types == TYPES


def test_documents_are_deduplicated_by_id_keeping_the_latest_and_sorted_by_number():
    rows = [
        doc(10, updated="2026-01-01T00:00:00Z"),
        doc(9),
        doc(10, updated="2026-03-01T00:00:00Z"),
        doc(10, updated="2025-01-01T00:00:00Z"),
    ]
    stats: dict = {}
    got = dl.latest_documents(rows, stats=stats)
    assert [d["id"] for d in got] == ["9", "10"]
    assert got[1]["last_updated_date"] == "2026-03-01T00:00:00Z"
    assert stats["duplicate_ids"] == 2


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------
@pytest.fixture
def dlenv(tmp_path):
    sleeps: list[float] = []
    return {
        "raw": tmp_path / "raw",
        "proc": tmp_path / "proc",
        "sleeps": sleeps,
        "kw": {"processed_root": tmp_path / "proc", "delay_s": 2.0, "sleep": sleeps.append},
    }


def _pdf_resp(text: str = "hello") -> FakeResp:
    return FakeResp(make_pdf([text]), 200, "application/pdf")


def _session(*docs_and_resps) -> FakeSession:
    return FakeSession({d["document_url"]: list(r) for d, r in docs_and_resps})


def test_documents_download_one_at_a_time_named_by_id_with_a_delay_and_a_marker(dlenv):
    a, b = doc(5), doc(6)
    s = _session((a, [_pdf_resp("a")]), (b, [_pdf_resp("b")]))
    out = dl.fetch_documents(dlenv["raw"], [a, b], session=s, **dlenv["kw"])
    assert out["fetched"] == ["5", "6"] and not out["failed"] and out["skipped"] == []
    assert (dlenv["raw"] / "epar" / "5.pdf").read_bytes().startswith(b"%PDF")
    assert dlenv["sleeps"] == [2.0]  # between the two requests, not before the first
    for key in ("epar__5.pdf", "epar__6.pdf"):
        assert success_marker_path(dlenv["proc"], dl.MARKER_SOURCE, key).is_file()
    assert not list((dlenv["raw"] / "epar").glob(".*"))  # no temporary file left


def test_the_stored_name_comes_from_the_id_not_the_url_path(dlenv):
    # Two documents with the same file name in their URLs land in two files.
    a = doc(1, document_url=PREFIX + "en/documents/a/same_en.pdf")
    b = doc(2, document_url=PREFIX + "en/documents/b/same_en.pdf")
    s = _session((a, [_pdf_resp("a")]), (b, [_pdf_resp("b")]))
    dl.fetch_documents(dlenv["raw"], [a, b], session=s, **dlenv["kw"])
    assert sorted(p.name for p in (dlenv["raw"] / "epar").iterdir()) == ["1.pdf", "2.pdf"]


def test_two_documents_cannot_share_a_stored_file_and_marker(dlenv):
    a = doc(5)
    with pytest.raises(ValueError, match="share a stored file name"):
        dl.fetch_documents(dlenv["raw"], [a, dict(a)], session=FakeSession({}), **dlenv["kw"])


def test_verification_page_stops_the_download(dlenv):
    a, b, c = doc(1), doc(2), doc(3)
    html = b"<!DOCTYPE html><html><body>Please verify you are human</body></html>"
    s = _session(
        (a, [_pdf_resp("a")]),
        (b, [FakeResp(html, 200, "text/html; charset=utf-8")]),
        (c, [_pdf_resp("c")]),
    )
    with pytest.raises(FeedVerificationError):
        dl.fetch_documents(dlenv["raw"], [a, b, c], session=s, **dlenv["kw"])
    assert s.calls == [a["document_url"], b["document_url"]]  # no retry, no third request
    assert (dlenv["raw"] / "epar" / "1.pdf").is_file()  # what was fetched stays
    assert not (dlenv["raw"] / "epar" / "2.pdf").exists()
    assert not list((dlenv["raw"] / "epar").glob(".*"))
    assert success_marker_path(dlenv["proc"], dl.MARKER_SOURCE, "epar__1.pdf").is_file()
    marker = failed_marker_path(dlenv["proc"], dl.MARKER_SOURCE, "epar__2.pdf")
    assert "FeedVerificationError" in marker.read_text(encoding="utf-8")
    # A re-run resumes: the first document is skipped, the second is asked for again.
    s2 = _session((b, [_pdf_resp("b")]), (c, [_pdf_resp("c")]))
    out = dl.fetch_documents(dlenv["raw"], [a, b, c], session=s2, **dlenv["kw"])
    assert out["skipped"] == ["1"] and out["fetched"] == ["2", "3"]
    assert s2.calls == [b["document_url"], c["document_url"]]
    assert not failed_marker_path(dlenv["proc"], dl.MARKER_SOURCE, "epar__2.pdf").exists()


def test_a_200_body_that_is_not_a_pdf_is_the_same_stop(dlenv):
    a, b = doc(1), doc(2)
    # Not HTML by content type or first byte, and still not a PDF.
    s = _session((a, [FakeResp(b"PK\x03\x04 a zip", 200, "application/octet-stream")]))
    with pytest.raises(FeedVerificationError, match="not a PDF"):
        dl.fetch_documents(dlenv["raw"], [a, b], session=s, **dlenv["kw"])
    assert s.calls == [a["document_url"]] and not (dlenv["raw"] / "epar" / "1.pdf").exists()


def test_an_html_error_status_is_an_ordinary_failure_not_the_stop(dlenv):
    a, b = doc(1), doc(2)
    s = _session(
        (a, [FakeResp(b"<html>not found</html>", 404, "text/html")]), (b, [_pdf_resp("b")])
    )
    out = dl.fetch_documents(dlenv["raw"], [a, b], session=s, **dlenv["kw"])
    assert out["fetched"] == ["2"] and "HTTP 404" in out["failed"]["1"]
    assert s.calls.count(a["document_url"]) == dl.ATTEMPTS  # tried again once, then failed
    marker = failed_marker_path(dlenv["proc"], dl.MARKER_SOURCE, "epar__1.pdf")
    assert marker.is_file()


def test_a_cut_short_pdf_is_tried_again_and_then_failed(dlenv):
    a = doc(1)
    whole = make_pdf(["whole"])
    s = _session((a, [FakeResp(whole[:-40], 200, "application/pdf", cl=len(whole[:-40]))]))
    out = dl.fetch_documents(dlenv["raw"], [a], session=s, **dlenv["kw"])
    assert "1" in out["failed"] and "truncated" in out["failed"]["1"]
    assert not (dlenv["raw"] / "epar" / "1.pdf").exists()


def test_download_resumes_past_existing_files(dlenv):
    a, b = doc(1), doc(2)
    s = _session((a, [_pdf_resp("a")]), (b, [_pdf_resp("b")]))
    dl.fetch_documents(dlenv["raw"], [a], session=s, **dlenv["kw"])
    out = dl.fetch_documents(dlenv["raw"], [a, b], session=s, **dlenv["kw"])
    assert out["skipped"] == ["1"] and out["fetched"] == ["2"]
    assert s.calls == [a["document_url"], b["document_url"]]
    # A file without its success marker is not trusted.
    success_marker_path(dlenv["proc"], dl.MARKER_SOURCE, "epar__1.pdf").unlink()
    out = dl.fetch_documents(dlenv["raw"], [a], session=s, **dlenv["kw"])
    assert out["fetched"] == ["1"]


def test_a_changed_last_updated_date_downloads_again_and_replaces_the_file(dlenv):
    a = doc(1, updated="2026-01-01T00:00:00Z")
    s = _session((a, [_pdf_resp("old")]))
    dl.fetch_documents(dlenv["raw"], [a], session=s, **dlenv["kw"])
    old = (dlenv["raw"] / "epar" / "1.pdf").read_bytes()
    a2 = doc(1, updated="2026-06-01T00:00:00Z")
    s2 = _session((a2, [_pdf_resp("new text")]))
    out = dl.fetch_documents(dlenv["raw"], [a2], session=s2, **dlenv["kw"])
    assert out["fetched"] == ["1"] and s2.calls == [a2["document_url"]]
    assert (dlenv["raw"] / "epar" / "1.pdf").read_bytes() != old
    # The same date again is a skip.
    out = dl.fetch_documents(dlenv["raw"], [a2], session=s2, **dlenv["kw"])
    assert out["skipped"] == ["1"]


def test_fetch_refuses_a_url_off_the_allowed_prefix_even_when_called_directly(dlenv):
    bad = doc(1, document_url="https://evil.example/en/documents/a/b_en.pdf")
    s = FakeSession({})
    out = dl.fetch_documents(dlenv["raw"], [bad], session=s, **dlenv["kw"])
    assert s.calls == [] and out["refused"] == 1 and out["fetched"] == []


def test_max_files_limits_the_fetches(dlenv):
    docs = [doc(i) for i in (1, 2, 3)]
    s = _session(*[(d, [_pdf_resp(d["id"])]) for d in docs])
    out = dl.fetch_documents(dlenv["raw"], docs, session=s, max_files=2, **dlenv["kw"])
    assert out["fetched"] == ["1", "2"]


def test_a_document_over_the_byte_cap_is_stopped_failed_and_not_retried(dlenv, monkeypatch):
    big = make_pdf(["x" * 400])
    _env(monkeypatch, "EPISTEME_EPAR_MAX_BYTES", len(big) - 1)
    a, b, c = doc(1), doc(2), doc(3)
    s = _session(
        (a, [FakeResp(big, 200, "application/pdf")]),  # Content-Length says it is too big
        (b, [FakeResp(big, 200, "application/pdf", cl=False)]),  # only the stream says so
        (c, [_pdf_resp("small")]),
    )
    out = dl.fetch_documents(dlenv["raw"], [a, b, c], session=s, **dlenv["kw"])
    assert out["fetched"] == ["3"] and set(out["failed"]) == {"1", "2"}
    assert "cap" in out["failed"]["1"] and "cap" in out["failed"]["2"]
    assert s.calls.count(a["document_url"]) == 1  # not retried: it can never fit
    assert s.calls.count(b["document_url"]) == 1
    for i in ("1", "2"):
        assert not (dlenv["raw"] / "epar" / f"{i}.pdf").exists()
        marker = failed_marker_path(dlenv["proc"], dl.MARKER_SOURCE, f"epar__{i}.pdf")
        assert json.loads(marker.read_text("utf-8"))["error_class"] == "over_limit"
    assert not list((dlenv["raw"] / "epar").glob(".*"))  # no partial file left


def test_the_ema_feeds_have_no_byte_cap():
    import inspect

    from episteme.data.regulatory import download_ema

    assert inspect.signature(download_ema.download_to).parameters["max_bytes"].default is None
    assert "max_bytes" not in inspect.getsource(download_ema._download_once)


def test_the_delay_is_a_setting(dlenv, monkeypatch):
    _env(monkeypatch, "EPISTEME_EMA_DELAY_SECONDS", 7)
    a, b = doc(1), doc(2)
    s = _session((a, [_pdf_resp("a")]), (b, [_pdf_resp("b")]))
    kw = {k: v for k, v in dlenv["kw"].items() if k != "delay_s"}
    dl.fetch_documents(dlenv["raw"], [a, b], session=s, **kw)
    assert dlenv["sleeps"] == [7.0]


# ---------------------------------------------------------------------------
# pdf_text
# ---------------------------------------------------------------------------
def test_pdf_text_from_fixture(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(make_pdf(["Warfarin dose is 5 mg (see section 4.2)", "Second page text"]))
    text = ex.pdf_text(p)
    assert "Warfarin dose is 5 mg (see section 4.2)" in text
    assert text.index("Warfarin") < text.index("Second page text")


def test_a_blank_pdf_gives_no_text_and_is_told_apart_from_a_broken_one(tmp_path):
    p = tmp_path / "blank.pdf"
    p.write_bytes(blank_pdf(3))
    assert ex.pdf_text(p).strip() == ""
    q = tmp_path / "broken.pdf"
    q.write_bytes(b"%PDF-1.4\nthis is not a pdf at all")
    with pytest.raises(ex.PdfParseError):
        ex.pdf_text(q)


def test_pdf_text_caps_pages_and_bytes(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(make_pdf(["one", "two", "three"]))
    with pytest.raises(ex.PdfLimitError, match="3 pages"):
        ex.pdf_text(p, max_pages=2)
    with pytest.raises(ex.PdfLimitError, match="bytes"):
        ex.pdf_text(p, max_bytes=100)
    assert "three" in ex.pdf_text(p, max_pages=3, max_bytes=10**7)


def test_the_caps_are_settings(monkeypatch):
    from episteme.config import get_settings

    monkeypatch.delenv("EPISTEME_EPAR_BUCKET_IDS")  # the module fixture narrows it
    _clear_settings()
    s = get_settings()
    assert (s.epar_max_pages, s.epar_max_bytes) == (2000, 200 * 1024 * 1024)
    assert (s.epar_max_withdrawn_share, s.epar_bucket_ids) == (0.1, 8000)
    _env(monkeypatch, "EPISTEME_EPAR_MAX_PAGES", 5)
    _env(monkeypatch, "EPISTEME_EPAR_MAX_BYTES", 1234)
    s = get_settings()
    assert (s.epar_max_pages, s.epar_max_bytes) == (5, 1234)


def test_control_characters_are_removed_from_the_text(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(make_pdf(["abc\x01def"]))
    text = ex.pdf_text(p)
    assert "\x00" not in text and "\x01" not in text and "abc" in text and "def" in text


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------
@pytest.fixture
def raw(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    (r / "epar").mkdir(parents=True)
    return r


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "out"


@pytest.fixture(autouse=True)
def _small_buckets(monkeypatch):
    _env(monkeypatch, "EPISTEME_EPAR_BUCKET_IDS", 10)


def _feed(raw: Path, docs: list[dict], total: int | None = None) -> None:
    body = {"meta": {"total_records": len(docs) if total is None else total}, "data": docs}
    (raw / "ema_epar_documents.json").write_text(json.dumps(body), encoding="utf-8")


def _pdf(raw: Path, i: int, text: str | None = None, data: bytes | None = None) -> None:
    (raw / "epar" / f"{i}.pdf").write_bytes(data or make_pdf([text or f"Text of document {i}."]))


def _setup(raw: Path, ids, **kw) -> list[dict]:
    docs = [doc(i, **kw) for i in ids]
    _feed(raw, docs)
    for i in ids:
        _pdf(raw, i)
    return docs


def _caps(monkeypatch, rows: int, nbytes: int = BIG) -> None:
    monkeypatch.setattr(ex, "_part_caps", lambda: (rows, nbytes))


def _shards(out: Path) -> list[str]:
    return sorted(p.name.split(".")[0] for p in (out / "staging" / "ema_epar").glob("*.parquet"))


def _rows(out: Path, part: str) -> list[dict]:
    return pq.read_table(out / "staging" / "ema_epar" / f"{part}.parquet").to_pylist()


def test_a_document_becomes_a_row(raw, out):
    _setup(raw, [3])
    res = ex.extract_epar(raw, out)
    assert (res["ok"], res["failed"], res["rows"], res["documents"]) == (1, 0, 1, 1)
    (row,) = _rows(out, "ema_epar_b0000__part00001")
    assert row["id"] == "ema_epar:3" and row["source"] == "ema_epar"
    assert row["source_file"] == "ema_epar_b0000__part00001"
    assert row["source_record_id"] == "EMEA/H/C/000003"
    assert row["title"] == "Medicine3 : EPAR - Product information"
    assert "Text of document 3." in row["body_text"]
    assert row["extract_status"] == "ok"
    assert row["license"] == "permissive" and row["subset"] == "commercial"
    assert "EMA" in row["license_raw"] and "acknowledg" in row["license_raw"]
    assert row["pdf_url"] == URL.format(slug="medicine3")
    assert row["year"] == 2020 and row["language"] == "en" and row["is_retracted"] is False
    meta = json.loads(row["book_meta"])
    assert meta["doc_type"] == "product-information" and meta["document_id"] == "3"
    assert meta["last_updated_date"] == "2026-01-01T00:00:00Z"
    assert success_marker_path(out, "ema_epar", "ema_epar_b0000").is_file()
    assert success_marker_path(out, "ema_epar", "ema_epar_b0000__part00001").is_file()


def test_documents_go_to_buckets_by_id_range_and_parts_by_the_caps(raw, out, monkeypatch):
    _caps(monkeypatch, 2)
    _setup(raw, [1, 2, 3, 12, 25])
    res = ex.extract_epar(raw, out)
    assert (res["buckets"], res["rows"]) == (3, 5)
    assert _shards(out) == [
        "ema_epar_b0000__part00001",
        "ema_epar_b0000__part00002",
        "ema_epar_b0001__part00001",
        "ema_epar_b0002__part00001",
    ]
    assert [r["id"] for r in _rows(out, "ema_epar_b0000__part00002")] == ["ema_epar:3"]


def test_a_part_stops_at_the_byte_cap_and_one_row_over_the_cap_is_alone(raw, out, monkeypatch):
    _caps(monkeypatch, 100, 1)
    _setup(raw, [1, 2, 3])
    res = ex.extract_epar(raw, out)
    assert res["rows"] == 3
    assert len(_shards(out)) == 3


def test_the_bucket_of_a_document_does_not_depend_on_the_other_documents():
    assert ex.bucket_number("12", 10) == ex.bucket_number("19", 10) == 1
    assert ex.bucket_number("9", 10) == 0 and ex.bucket_number("20", 10) == 2
    assert (
        ex.bucket_key(7) == "ema_epar_b0007" and ex.part_name(7, 3) == "ema_epar_b0007__part00003"
    )


def test_the_staged_files_and_markers_follow_input_key(raw, out):
    from episteme.data.checkpoint_markers import input_key

    _setup(raw, [3])
    ex.extract_epar(raw, out)
    assert ex.document_key(raw / "epar" / "3.pdf", raw) == input_key(raw / "epar" / "3.pdf", raw)
    assert ex.document_key(raw / "epar" / "3.pdf", raw) == "epar__3.pdf"
    manifest = json.loads(
        (out / "_ops" / "ema_epar" / "buckets" / "ema_epar_b0000.json").read_text("utf-8")
    )
    assert [e["key"] for e in manifest["documents"]] == ["epar__3.pdf"]
    # A failed document is marked under its key, and a same-named file elsewhere is another key.
    _pdf(raw, 4, data=b"%PDF-1.4\nbroken")
    _feed(raw, [doc(3), doc(4)])
    ex.extract_epar(raw, out)
    assert failed_marker_path(out, "ema_epar", "epar__4.pdf").is_file()
    other = raw / "other" / "4.pdf"
    assert ex.document_key(other, raw) == "other__4.pdf" != "epar__4.pdf"


def test_two_documents_that_share_a_key_stop_the_run_before_anything_is_written(raw, out):
    assert len(dl.latest_documents([doc(3), doc(3, updated="2027-01-01T00:00:00Z")])) == 1
    with pytest.raises(ex.EparBucketError, match="share a key"):
        ex.check_keys([{"id": "3", "key": "epar__3.pdf"}, {"id": "03", "key": "epar__3.pdf"}])


def test_a_pdf_with_no_text_is_failed_and_counted(raw, out):
    _setup(raw, [1, 2])
    _pdf(raw, 2, data=blank_pdf(2))
    res = ex.extract_epar(raw, out)
    assert res["ok"] == 1 and res["failed"] == 0  # the bucket is whole; the document is not
    assert res["failed_docs_count"] == 1 and res["failed_by_reason"] == {"no_text": 1}
    assert res["rows"] == 2
    by_id = {r["id"]: r for r in _rows(out, "ema_epar_b0000__part00001")}
    # Not silently passed: the row is `empty`, so the gate's empty-text rule sees it.
    assert by_id["ema_epar:2"]["extract_status"] == "empty"
    assert by_id["ema_epar:2"]["body_text"] is None
    assert by_id["ema_epar:1"]["extract_status"] == "ok"
    marker = json.loads(failed_marker_path(out, "ema_epar", "epar__2.pdf").read_text("utf-8"))
    assert marker["error_class"] == "no_text"
    bucket = json.loads(success_marker_path(out, "ema_epar", "ema_epar_b0000").read_text("utf-8"))
    assert bucket["stats"]["failed_docs"][0]["file"] == "epar__2.pdf"
    assert bucket["stats"]["failed_docs"][0]["reason"] == "no_text"
    # The count reaches the sample gate.
    ex._write_failed_inputs(out, res)
    listed = json.loads((out / "_ops" / "ema_epar" / "failed_inputs.json").read_text("utf-8"))
    # A document with no text is told apart: the empty-text rule sees its row.
    assert listed["failed_inputs"] == 0 and listed["no_text"] == 1
    # The section counts reach the gate report too (report only).
    assert listed["documents_with_sections"] == res["documents_with_sections"]
    assert listed["documents_without_sections"] == res["documents_without_sections"]
    # A scan (no text) is out of the section share: counted apart as documents_no_text.
    assert res["documents_no_text"] == 1
    assert res["documents_with_sections"] + res["documents_without_sections"] == 1
    assert listed["documents_no_text"] == 1
    assert bucket["stats"]["documents_no_text"] == 1
    # A skipped bucket keeps the count from its marker.
    again = ex.extract_epar(raw, out)
    assert again["documents_no_text"] == 1


def test_a_broken_or_oversize_pdf_is_failed_and_does_not_stop_the_bucket(raw, out, monkeypatch):
    _setup(raw, [1, 2, 3, 4])
    _pdf(raw, 2, data=b"%PDF-1.4\nbroken")
    _pdf(raw, 3, data=make_pdf(["p1", "p2", "p3", "p4"]))
    _env(monkeypatch, "EPISTEME_EPAR_MAX_PAGES", 3)
    res = ex.extract_epar(raw, out)
    assert res["failed_by_reason"] == {"pdf_parse_error": 1, "over_limit": 1}
    assert res["failed_docs_count"] == 2
    ids = {r["id"] for r in _rows(out, "ema_epar_b0000__part00001")}
    assert ids == {"ema_epar:1", "ema_epar:4"}  # the failed ones have no row
    assert failed_marker_path(out, "ema_epar", "epar__3.pdf").is_file()


def test_a_later_good_pdf_clears_the_failed_marker(raw, out):
    _setup(raw, [1])
    _pdf(raw, 1, data=b"%PDF-1.4\nbroken")
    ex.extract_epar(raw, out)
    assert failed_marker_path(out, "ema_epar", "epar__1.pdf").is_file()
    _pdf(raw, 1, "Now it reads.")
    res = ex.extract_epar(raw, out)
    assert res["failed_docs_count"] == 0 and res["rows"] == 1
    assert not failed_marker_path(out, "ema_epar", "epar__1.pdf").is_file()


def test_a_complete_bucket_is_skipped_and_a_changed_document_rewrites_only_its_bucket(raw, out):
    _setup(raw, [1, 12])
    ex.extract_epar(raw, out)
    again = ex.extract_epar(raw, out)
    assert again["rows"] == 0 and again["failed"] == 0  # both buckets skipped
    mtimes = {
        n: (out / "staging" / "ema_epar" / f"{n}.parquet").stat().st_mtime_ns for n in _shards(out)
    }
    # A new last_updated_date (and a new file) for document 12 only.
    _feed(raw, [doc(1), doc(12, updated="2026-09-09T00:00:00Z")])
    _pdf(raw, 12, "Revised text.")
    res = ex.extract_epar(raw, out)
    assert res["rows"] == 1
    assert "Revised text." in _rows(out, "ema_epar_b0001__part00001")[0]["body_text"]
    assert (
        out / "staging" / "ema_epar" / "ema_epar_b0000__part00001.parquet"
    ).stat().st_mtime_ns == mtimes["ema_epar_b0000__part00001"]
    # A changed feed field that the row carries (the title) also rewrites.
    _feed(raw, [doc(1, name="Renamed"), doc(12, updated="2026-09-09T00:00:00Z")])
    assert ex.extract_epar(raw, out)["rows"] == 1
    assert _rows(out, "ema_epar_b0000__part00001")[0]["title"] == "Renamed"


def test_a_known_document_that_changed_and_fails_holds_its_bucket(raw, out):
    _setup(raw, [1, 2])
    ex.extract_epar(raw, out)
    part = out / "staging" / "ema_epar" / "ema_epar_b0000__part00001.parquet"
    before = part.read_bytes()
    # Document 2 changes, and its new file cannot be read.
    _feed(raw, [doc(1), doc(2, updated="2026-09-09T00:00:00Z")])
    _pdf(raw, 2, data=b"%PDF-1.4\nbroken now, and longer than before")
    res = ex.extract_epar(raw, out)
    assert res["held_buckets"] == ["ema_epar_b0000"] and res["failed"] == 1
    assert part.read_bytes() == before  # the old part, and so the old row, stay
    assert {r["id"] for r in _rows(out, "ema_epar_b0000__part00001")} == {
        "ema_epar:1",
        "ema_epar:2",
    }
    assert success_marker_path(out, "ema_epar", "ema_epar_b0000").is_file()
    held = json.loads(failed_marker_path(out, "ema_epar", "ema_epar_b0000").read_text("utf-8"))
    assert held["error_class"] == "held_bucket" and "epar__2.pdf" in held["message"]
    # The fixed file: the bucket is written again and the hold is gone.
    _pdf(raw, 2, "Revised text of document 2.")
    res = ex.extract_epar(raw, out)
    assert res["held_buckets"] == [] and res["failed"] == 0 and res["rows"] == 2
    assert not failed_marker_path(out, "ema_epar", "ema_epar_b0000").is_file()
    texts = {r["id"]: r["body_text"] for r in _rows(out, "ema_epar_b0000__part00001")}
    assert "Revised text" in texts["ema_epar:2"]


def test_a_document_that_had_no_row_does_not_hold_its_bucket(raw, out):
    _setup(raw, [1, 2])
    _pdf(raw, 2, data=b"%PDF-1.4\nbroken")
    ex.extract_epar(raw, out)
    _feed(raw, [doc(1), doc(2, updated="2026-09-09T00:00:00Z")])
    _pdf(raw, 2, data=b"%PDF-1.4\nstill broken, other bytes")
    res = ex.extract_epar(raw, out)
    assert res["held_buckets"] == [] and res["failed_by_reason"] == {"pdf_parse_error": 1}


def test_the_run_manifest_records_the_feed_config(raw, out):
    _setup(raw, [3])
    ex.extract_epar(raw, out)
    (manifest,) = (out / "_ops" / "ema_epar" / "runs").glob("run_*.json")
    feeds = json.loads(manifest.read_text("utf-8"))["config"]["feeds"]
    assert feeds["file"] == "feeds.toml" and len(feeds["sha256"]) == 64
    assert list(feeds["entries"]) == ["ema_epar_documents"]
    assert feeds["entries"]["ema_epar_documents"]["document_types"] == list(TYPES)


def test_force_writes_again(raw, out):
    _setup(raw, [1])
    ex.extract_epar(raw, out)
    assert ex.extract_epar(raw, out, force=True)["rows"] == 1


def test_the_bucket_width_is_recorded_and_a_run_at_another_width_is_refused(raw, out, monkeypatch):
    _setup(raw, [1])
    ex.extract_epar(raw, out)
    stored = json.loads((out / "_ops" / "ema_epar" / "bucket_scheme.json").read_text("utf-8"))
    assert stored == {"bucket_ids": 10}
    _env(monkeypatch, "EPISTEME_EPAR_BUCKET_IDS", 20)
    with pytest.raises(RuntimeError, match="bucket width is 20"):
        ex.extract_epar(raw, out)


def test_a_document_removed_from_feed_is_retired(raw, out, monkeypatch):
    _env(monkeypatch, "EPISTEME_EPAR_MAX_WITHDRAWN_SHARE", 1.0)  # the guard is tested below
    _setup(raw, [1, 12])
    ex.extract_epar(raw, out)
    for part in ("ema_epar_b0000__part00001", "ema_epar_b0001__part00001"):  # both were loaded
        lp = load_success_marker_path(out, "ema_epar", f"{part}.parquet")
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text("{}", encoding="utf-8")
    # The feed no longer lists document 12 (its file stays on disk: the feed decides).
    _feed(raw, [doc(1)])
    res = ex.extract_epar(raw, out)
    assert res["withdrawn_docs"] == 1 and res["retired_buckets"] == 1
    assert parts_retire.read_retire_pending(out, "ema_epar") == ["ema_epar_b0001__part00001"]
    assert _shards(out) == ["ema_epar_b0000__part00001"]
    assert not (out / "_ops" / "ema_epar" / "buckets" / "ema_epar_b0001.json").exists()
    assert not success_marker_path(out, "ema_epar", "ema_epar_b0001").is_file()


def test_a_document_that_leaves_a_shared_bucket_is_retired_by_a_rewrite(raw, out, monkeypatch):
    _env(monkeypatch, "EPISTEME_EPAR_MAX_WITHDRAWN_SHARE", 1.0)
    _setup(raw, [1, 2])
    ex.extract_epar(raw, out)
    lp = load_success_marker_path(out, "ema_epar", "ema_epar_b0000__part00001.parquet")
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("{}", encoding="utf-8")
    _feed(raw, [doc(1)])
    res = ex.extract_epar(raw, out)
    assert res["withdrawn_docs"] == 1 and res["rows"] == 1
    # The part is written again under the same name; the pending name is dropped by the retire
    # step once that name is loaded again (parts_retire classify), and rows of 2 are replaced.
    assert [r["id"] for r in _rows(out, "ema_epar_b0000__part00001")] == ["ema_epar:1"]
    assert "ema_epar_b0000__part00001" in parts_retire.read_retire_pending(out, "ema_epar")


def test_the_withdrawal_decision_reads_the_feed_not_the_files_on_disk(raw, out, monkeypatch):
    _env(monkeypatch, "EPISTEME_EPAR_MAX_WITHDRAWN_SHARE", 1.0)
    _setup(raw, [1, 12])
    ex.extract_epar(raw, out)
    (raw / "epar" / "12.pdf").unlink()  # the file is gone, the feed still lists the document
    res = ex.extract_epar(raw, out)
    assert res["withdrawn_docs"] == 0 and res["held_buckets"] == ["ema_epar_b0001"]
    assert parts_retire.read_retire_pending(out, "ema_epar") == []
    assert "ema_epar_b0001__part00001" in _shards(out)  # its earlier part stays
    assert failed_marker_path(out, "ema_epar", "ema_epar_b0001").is_file()
    assert res["failed"] == 1
    # When the file is back the hold is gone.
    _pdf(raw, 12)
    res = ex.extract_epar(raw, out)
    assert res["held_buckets"] == [] and res["failed"] == 0
    assert not failed_marker_path(out, "ema_epar", "ema_epar_b0001").is_file()


def test_a_listed_document_never_downloaded_is_counted_not_held(raw, out):
    _feed(raw, [doc(1), doc(2)])
    _pdf(raw, 1)
    res = ex.extract_epar(raw, out)
    assert res["rows"] == 1 and res["not_downloaded"] == 1
    assert res["failed_docs_count"] == 1 and res["failed_by_reason"] == {"not_downloaded": 1}
    assert res["held_buckets"] == []


def test_too_many_withdrawn_documents_refuse_and_the_override_works(raw, out, monkeypatch):
    _setup(raw, range(1, 11))
    # No earlier state: no limit.
    assert ex.extract_epar(raw, out)["rows"] == 10
    # One of ten is 10 percent: at the limit, allowed.
    _feed(raw, [doc(i) for i in range(1, 10)])
    assert ex.extract_epar(raw, out)["withdrawn_docs"] == 1
    # Two of nine are over it: refused, and nothing changes.
    manifest = out / "_ops" / "ema_epar" / "buckets" / "ema_epar_b0000.json"
    before = manifest.read_text(encoding="utf-8")
    _feed(raw, [doc(i) for i in range(1, 8)])
    with pytest.raises(ex.EparGuardError, match="2 of 9 known documents"):
        ex.extract_epar(raw, out)
    assert manifest.read_text(encoding="utf-8") == before
    assert len(_rows(out, "ema_epar_b0000__part00001")) == 9
    with pytest.raises(ex.EparGuardError):  # --force alone does not override
        ex.extract_epar(raw, out, force=True)
    audited: list[tuple[str, str]] = []
    monkeypatch.setattr(ex, "_audit_override", lambda reason, what: audited.append((reason, what)))
    res = ex.extract_epar(raw, out, force=True, force_reason="two withdrawn")
    assert (res["withdrawn_docs"], res["rows"]) == (2, 7)
    assert audited and audited[0][0] == "two withdrawn" and "2 of 9" in audited[0][1]


def test_a_sample_root_skips_the_guards_and_missing_files_are_not_failures(raw, tmp_path):
    sample_out = tmp_path / "_sample" / "ema_epar" / "02_processed"
    _feed(raw, [doc(i) for i in range(1, 6)])
    _pdf(raw, 1)
    _pdf(raw, 2)
    res = ex.extract_epar(raw, sample_out)
    assert res["rows"] == 2 and res["failed_docs_count"] == 0 and res["not_downloaded"] == 3
    _feed(raw, [doc(1)])
    assert ex.extract_epar(raw, sample_out)["withdrawn_docs"] == 1  # no guard


def test_an_empty_feed_is_an_error_and_a_short_feed_is_refused(raw, out):
    _feed(raw, [])
    with pytest.raises(ex.EparScanError, match="no EPAR document"):
        ex.extract_epar(raw, out)
    _feed(raw, [doc(1)], total=5)
    _pdf(raw, 1)
    with pytest.raises(ex.EparGuardError, match="1 rows but meta.total_records = 5"):
        ex.extract_epar(raw, out)
    assert not (out / "staging").exists()
    res = ex.extract_epar(raw, out, force=True, force_reason="feed total is wrong")
    assert res["rows"] == 1


def test_a_missing_feed_file_is_an_error(raw, out):
    with pytest.raises(ex.EparScanError, match="ema_epar_documents.json"):
        ex.extract_epar(raw, out)


def test_the_manifest_does_not_hold_a_url_off_the_prefix(raw, out):
    _feed(raw, [doc(1), doc(2, document_url="https://evil.example/a_en.pdf")])
    _pdf(raw, 1)
    _pdf(raw, 2)
    res = ex.extract_epar(raw, out)
    assert res["documents"] == 1 and res["refused_rows"] == 1


def test_skip_unit_leaves_a_bucket_out(raw, out, monkeypatch):
    from episteme.ops import skip

    monkeypatch.setattr(
        skip,
        "apply_skips",
        lambda src, stage, units, skipped, reason, **kw: [u for u in units if u not in skipped],
    )
    _setup(raw, [1, 12])
    res = ex.extract_epar(raw, out, skip_units=["ema_epar_b0001"], skip_reason="test")
    assert res["rows"] == 1 and _shards(out) == ["ema_epar_b0000__part00001"]


def test_main_exit_codes_and_failed_inputs(raw, out, capsys):
    _setup(raw, [1])
    rc = ex._main(["--raw-dir", str(raw), "--processed-dir", str(out)])
    assert rc == 0
    listed = json.loads((out / "_ops" / "ema_epar" / "failed_inputs.json").read_text("utf-8"))
    assert listed["failed_inputs"] == 0
    rc = ex._main(["--raw-dir", str(raw / "nope"), "--processed-dir", str(out)])
    assert rc == 1


def test_the_failed_inputs_file_name_matches_the_gate():
    from episteme.ops import gate

    assert gate.FAILED_INPUTS_FILE == "failed_inputs.json"


def test_the_gate_knows_the_stage_modules():
    from episteme.ops import gate

    assert gate.stage_module("ema_epar", "extract") == (
        "episteme.data.ema.extract_epar",
        "ema/extract_ema_epar.sh",
    )
    assert gate.stage_module("ema_epar", "load") == (
        "episteme.data.load_articles",
        "ema/load_ema_epar.sh",
    )
    assert "episteme.data.ema.retire" in gate.hash_modules("ema_epar", "load")


def test_the_wrappers_exist_and_the_pipeline_knows_the_source():
    root = Path(__file__).resolve().parents[2] / "scripts" / "data"
    for name in ("extract_ema_epar.sh", "load_ema_epar.sh", "download_ema_epar.sh"):
        assert (root / "ema" / name).is_file()
    pipeline = (root / "run_pipeline.sh").read_text(encoding="utf-8")
    assert "[ema_epar]=" in pipeline and "ema_epar" in pipeline.split("LIT_SOURCES=")[1][:200]


# ---------------------------------------------------------------------------
# pg (run by the controller, not in the cloud sandbox)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "src/episteme/data/db/migrations"


@pytest.fixture(scope="module")
def _schema():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    if conninfo_to_dict(dsn).get("dbname") != "episteme_test":
        pytest.fail("TEST_PG_DSN does not target episteme_test; refusing to DROP SCHEMA")
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
            cur.execute((REPO_ROOT / "src/episteme/data/db/extensions.sql").read_text("utf-8"))
            cur.execute((REPO_ROOT / "src/episteme/data/db/schema.sql").read_text("utf-8"))
            for name in (
                "0002_container_and_book_parts.sql",
                "0010_regulatory_sources.sql",
                "0011_label_section_ordinal.sql",
            ):
                cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db(_schema, pg_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "audit_root"))
    _clear_settings()
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE episteme.label_section, episteme.label_substance")
        cur.execute("DELETE FROM episteme.article_body WHERE source = 'ema_epar'")
        cur.execute("DELETE FROM episteme.articles WHERE source = 'ema_epar'")
    pg_conn.commit()
    yield pg_conn


def _count(conn, source: str = "ema_epar") -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source = %s", (source,))
        return cur.fetchone()[0]


@pytest.mark.pg
def test_a_part_loads_and_its_text_is_stored(db, raw, out):
    from episteme.data import postgres_loader

    _setup(raw, [1, 2])
    ex.extract_epar(raw, out)
    shard = out / "staging" / "ema_epar" / "ema_epar_b0000__part00001.parquet"
    postgres_loader.load_source_file(db, source="ema_epar", staging_path=shard, run_id="t")
    db.commit()
    assert _count(db) == 2
    with db.cursor() as cur:
        cur.execute(
            "SELECT a.source_record_id, a.license, b.body_text FROM episteme.articles a "
            "JOIN episteme.article_body b ON b.article_id = a.id AND b.source = a.source "
            "WHERE a.id = 'ema_epar:1'"
        )
        pn, lic, body = cur.fetchone()
    assert pn == "EMEA/H/C/000001" and lic == "permissive" and "Text of document 1." in body


@pytest.mark.pg
def test_retire_removes_the_rows_of_a_part_a_withdrawn_document_left(db, raw, out, monkeypatch):
    from episteme.data import postgres_loader

    _env(monkeypatch, "EPISTEME_EPAR_MAX_WITHDRAWN_SHARE", 1.0)
    _setup(raw, [1, 12])
    ex.extract_epar(raw, out)
    for part in ("ema_epar_b0000__part00001", "ema_epar_b0001__part00001"):
        shard = out / "staging" / "ema_epar" / f"{part}.parquet"
        postgres_loader.load_source_file(db, source="ema_epar", staging_path=shard, run_id="t")
        lp = load_success_marker_path(out, "ema_epar", f"{part}.parquet")
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text("{}", encoding="utf-8")
    db.commit()
    assert _count(db) == 2
    _feed(raw, [doc(1)])
    ex.extract_epar(raw, out)
    pending = parts_retire.read_retire_pending(out, "ema_epar")
    assert pending == ["ema_epar_b0001__part00001"]
    # Retiring the bucket removed the part's load marker with its shard (delete_all_parts).
    lp = load_success_marker_path(out, "ema_epar", "ema_epar_b0001__part00001.parquet")
    assert not lp.exists()
    postgres_loader.delete_source_files(db, source="ema_epar", source_files=pending, run_id="t")
    db.commit()
    assert _count(db) == 1


@pytest.mark.pg
def test_epar_part_load_writes_its_sections(db, raw, out):
    from episteme.data import load_articles, postgres_loader

    from .test_smpc import LEAFLET, smpc_block, smpc_pdf

    docs = [doc(1), doc(2, type_="assessment-report")]
    _feed(raw, docs)
    (raw / "epar" / "1.pdf").write_bytes(smpc_pdf([*smpc_block("Foo"), *LEAFLET]))
    _pdf(raw, 2)
    ex.extract_epar(raw, out)
    shard = out / "staging" / "ema_epar" / "ema_epar_b0000__part00001.parquet"
    extra = load_articles._pre_load(db, "ema_epar", shard)
    postgres_loader.load_source_file(db, source="ema_epar", staging_path=shard, run_id="t")
    db.commit()
    assert extra["label_sections"] == 5 and extra["label_substances"] == 0
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT article_id FROM episteme.label_section")
        assert [r[0] for r in cur.fetchall()] == ["ema_epar:1"]
        cur.execute(
            """
            SELECT s.ordinal, s.product,
                   substr(b.body_text, s.start_offset + 1, s.end_offset - s.start_offset)
              FROM episteme.label_section s
              JOIN episteme.article_body b ON b.article_id = s.article_id
             WHERE s.loinc_code = '34073-7'
            """
        )
        ordinal, product, text = cur.fetchone()
    assert (ordinal, product) == (3, "Foo 10 mg tablets")
    assert text.endswith("Avoid warfarin.") and text.startswith("4.5 Interaction")
    # The same part again: replaced, not added to.
    load_articles._pre_load(db, "ema_epar", shard)
    postgres_loader.load_source_file(db, source="ema_epar", staging_path=shard, run_id="t")
    db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.label_section")
        assert cur.fetchone()[0] == 5
    # Retire removes the label rows with the articles.
    postgres_loader.delete_source_files(
        db, source="ema_epar", source_files=["ema_epar_b0000__part00001"], run_id="t"
    )
    db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.label_section")
        assert cur.fetchone()[0] == 0
