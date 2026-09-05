import json

import pytest

from episteme.data.article_schema import empty_article_row, finalize_row

# build_report / write_rows both take explicit paths and never touch
# episteme.config, so no env var / config reload is needed here.


def _mk(**kw):
    return finalize_row({**empty_article_row(), "source": "pmc", **kw})


def test_sample_audit_build_report(tmp_path):
    from episteme.data import sample_audit
    from episteme.data.staging_writer import write_rows

    rows = [
        _mk(
            id="pmc:1",
            title="T1",
            abstract="a" * 300,
            authors=["X"],
            license="CC BY",
            extract_status="ok",
            year=None,
        ),
        _mk(
            id="pmc:2",
            title="T2",
            abstract="b" * 300,
            authors=None,
            license="CC BY",
            extract_status="partial",
            year=None,
        ),
        _mk(
            id="pmc:3",
            title="T3",
            abstract="c" * 300,
            authors=None,
            license="CC0",
            extract_status="ok",
            year=None,
        ),
    ]
    write_rows(rows, tmp_path / "02_processed", source="pmc", source_file="B01.json")

    staging = tmp_path / "02_processed" / "staging" / "pmc"
    md = sample_audit.build_report(staging, "pmc")

    assert md.is_file()
    assert md.suffix == ".md"
    js = md.with_suffix(".json")
    assert js.is_file()

    data = json.loads(js.read_text())
    assert {"columns", "license_values", "extract_status_histogram", "flags"} <= set(data)
    assert abs(data["columns"]["authors"]["non_null_pct"] - 33.3) < 0.2
    assert data["license_values"]["CC BY"] == 2
    assert data["license_values"]["CC0"] == 1
    assert data["extract_status_histogram"]["ok"] == 2
    assert "year is" in " ".join(data["flags"])

    # empty staging dir -> FileNotFoundError
    empty = tmp_path / "02_processed" / "staging" / "empty"
    empty.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        sample_audit.build_report(empty, "pmc")
