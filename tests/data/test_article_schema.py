from episteme.data.article_schema import (
    ARTICLE_COLUMNS,
    BOOK_META_KEYS,
    MIN_OK_TEXT_LEN,
    SCHEMA_VERSION,
    SOURCES,
    build_text,
    decide_extract_status,
    normalize_license,
    subset_from_license,
)


def test_schema_version_and_columns():
    assert SCHEMA_VERSION == "1.4"
    assert ARTICLE_COLUMNS[-2:] == ["container_id", "book_meta"]
    assert "isbn" in BOOK_META_KEYS


def test_decide_extract_status_book_row_never_empty():
    status, notes = decide_extract_status(
        text="", abstract=None, body_text=None, has_id=True, book_meta={"isbn": "x"}
    )
    assert status == "partial"
    assert notes == "book_row_short_text_len=0"
    status, notes = decide_extract_status(
        text="y" * MIN_OK_TEXT_LEN,
        abstract=None,
        body_text=None,
        has_id=False,
        book_meta={"isbn": "x"},
    )
    assert status == "ok"
    assert notes is None


def test_sources_covers_roadmap_phase0_set():
    for s in ("pubmed", "pmc", "bookshelf", "apollo", "chembl", "uniprot", "mesh", "openalex"):
        assert s in SOURCES


def test_build_text_joins_present_parts_only():
    assert build_text("T", None, "B") == "T\n\nB"
    assert build_text(None, "A", None) == "A"
    assert build_text("", "", "") == ""


def test_extract_status_ok_when_abstract_present_even_if_text_short():
    status, notes = decide_extract_status(
        text="short", abstract="a real abstract", body_text=None, has_id=True
    )
    assert status == "ok"


def test_extract_status_partial_for_title_only_short_text():
    status, notes = decide_extract_status(
        text="x" * (MIN_OK_TEXT_LEN - 1), abstract=None, body_text=None, has_id=True
    )
    assert status == "partial"


def test_extract_status_empty_when_id_but_no_content():
    status, notes = decide_extract_status(text="", abstract=None, body_text=None, has_id=True)
    assert status == "empty"


def test_normalize_license_cc_by_nc_is_not_commercial():
    code, url, raw = normalize_license("This article is CC BY-NC 4.0")
    assert code == "CC BY-NC"
    assert subset_from_license(code) == "text_mining"


def test_normalize_license_cc0_is_commercial():
    code, _, _ = normalize_license("CC0 1.0 Universal public domain dedication")
    assert code == "CC0"
    assert subset_from_license(code) == "commercial"
