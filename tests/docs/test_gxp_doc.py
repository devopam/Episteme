import re

from episteme.audit_trail import _HASHED_FIELDS, EVENT_TYPES

from ._repo import REPO, doc


def _schema_columns() -> list[str]:
    sql = (REPO / "src/episteme/data/db/schema.sql").read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE episteme\._audit \((.*?)\n\) PARTITION", sql, re.S)
    assert m, "_audit table definition not found in schema.sql"
    cols = []
    for ln in m.group(1).splitlines():
        s = ln.strip()
        if not s or s.startswith(("PRIMARY", "--")):
            continue
        cols.append(s.split()[0])
    assert len(cols) == 16, cols  # the doc's schema table claims 16 columns
    return cols


def test_every_event_type_is_documented():
    text = doc("11-gxp-data-integrity.md")
    assert len(EVENT_TYPES) == 13
    for ev in EVENT_TYPES:
        assert f"`{ev}`" in text, ev


def test_every_audit_column_is_documented():
    text = doc("11-gxp-data-integrity.md")
    for col in _schema_columns():
        assert f"`{col}`" in text, col
    for f in _HASHED_FIELDS:
        assert f"`{f}`" in text, f
    assert " ".join(f"`{f}`" for f in _HASHED_FIELDS[:3]).replace("` `", "`, `") in text


def test_boundaries_and_claims():
    text = doc("11-gxp-data-integrity.md")
    assert "GxP-ready" in text
    # every use of "GxP-compliant" must be a negation
    for m in re.finditer(r"GxP-compliant", text):
        assert text[max(0, m.start() - 4) : m.start()] == "not ", m.start()
    assert "EPISTEME_DB_MODE=restricted" in text  # go-live item
    assert "database name" in text  # guard limit
    assert "verify_audit_trail.sh" in text
    assert (REPO / "scripts/data/verify_audit_trail.sh").is_file()


def test_unimplemented_design_items_are_stated_as_absent():
    text = doc("11-gxp-data-integrity.md")
    # These must stay described as NOT implemented while the repo lacks them.
    assert not (REPO / "scripts/data/rotate_audit_logs.sh").exists()
    assert "chattr +a" in text and "rotate_audit_logs.sh" in text
    assert text.count("**Not implemented.**") >= 3
    sql = (REPO / "src/episteme/data/db/schema.sql").read_text(encoding="utf-8")
    assert "not yet" in sql and "create_audit_partition" in sql


def _event_row(text: str, ev: str) -> str:
    rows = [ln for ln in text.splitlines() if ln.startswith(f"| `{ev}` |")]
    assert len(rows) == 1, ev
    return rows[0]


def test_extract_and_serialize_commit_are_described_as_chained_with_fallback():
    text = doc("11-gxp-data-integrity.md")
    for ev in ("extract_commit", "serialize_commit"):
        row = _event_row(text, ev)
        assert "record()" in row and "chained DB row" in row, ev
        assert "fallback" in row, ev
        assert "JSONL only" not in row, ev


def test_code_premise_extract_and_serialize_use_record_with_mirror_fallback():
    base = REPO / "src/episteme/data"

    def uses_both(pattern: str) -> list[str]:
        hits = []
        for f in base.rglob(pattern):
            src = f.read_text(encoding="utf-8")
            if "from episteme.audit_trail import record as _audit" in src and "mirror_only(" in src:
                hits.append(f.name)
        return hits

    assert uses_both("extract_*.py"), "no extract module pairs record() with mirror_only fallback"
    assert uses_both(
        "serialize_*.py"
    ), "no serialize module pairs record() with mirror_only fallback"
