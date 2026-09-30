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


def _gap_row(text: str, prefix: str) -> str:
    """Locate one row of the design-vs-implementation gap table by its
    Design-statement cell prefix (same exact-match-on-start pattern as
    ``_event_row`` below, just against that table's first column instead)."""
    rows = [ln for ln in text.splitlines() if ln.startswith(f"| {prefix}")]
    assert len(rows) == 1, prefix
    return rows[0]


def test_ensure_audit_partitions_is_documented_as_implemented():
    # ensure_audit_partitions.sh landed (SP6 Task 2): its own gap-table row
    # must now describe monthly partition auto-creation as implemented (via
    # a rotation script), not absent, and must name the actual default
    # months_ahead so the doc and the script cannot silently drift apart.
    # schema.sql's own comment near the _audit table (SP6 close-out item 6)
    # now points at ensure_audit_partitions.sh instead of saying a rotation
    # script "is not yet built" -- pin that it names the real script, so a
    # future re-introduction of the stale wording fails this test loudly.
    text = doc("11-gxp-data-integrity.md")
    sql = (REPO / "src/episteme/data/db/schema.sql").read_text(encoding="utf-8")
    m_comment = re.search(r"-- episteme\._audit --.*?\nCREATE TABLE episteme\._audit \(", sql, re.S)
    assert m_comment, "_audit table's header comment block not found in schema.sql"
    audit_comment = m_comment.group(0)
    assert "create_audit_partition" in audit_comment
    assert "ensure_audit_partitions.sh" in audit_comment
    assert "is not yet" not in audit_comment

    script = REPO / "scripts/data/db/ensure_audit_partitions.sh"
    assert script.is_file()
    script_src = script.read_text(encoding="utf-8")
    m = re.search(r'MONTHS_AHEAD="\$\{2:-(\d+)\}"', script_src)
    assert m, "could not find ensure_audit_partitions.sh's default months_ahead"
    default_months = m.group(1)

    row = _gap_row(text, "Monthly partitions created by a rotation script")
    assert "**Implemented" in row
    assert "ensure_audit_partitions.sh" in row
    assert f"(default `{default_months}`)" in row
    assert "REVOKE UPDATE, DELETE" in row
    assert "_audit_default" in row  # the fallback fact must not be dropped


def test_rotate_audit_logs_is_documented_as_implemented():
    # rotate_audit_logs.sh landed (SP6 Task 4): its own gap-table row must
    # describe it as implemented, not absent, and verify()'s mirror-parity
    # glob must cover the .jsonl.gz files it produces.
    text = doc("11-gxp-data-integrity.md")
    assert (REPO / "scripts/data/rotate_audit_logs.sh").is_file()
    row = _gap_row(
        text, "`scripts/data/rotate_audit_logs.sh` gzips mirror files older than 30 days"
    )
    assert "**Implemented.**" in row
    assert "rotate_audit_logs.sh" in row
    # find's -mtime +30 is a floor comparison: a file is eligible only once
    # it is at least 31 days old. The chattr -a-before-gzip fix (Task 4
    # follow-up, commit c93ea59) must be described, and a per-file gzip
    # failure must be documented as a WARN-and-continue, not a `die`.
    assert "31" in row
    assert "chattr -a" in row
    assert "WARNING" in row
    chattr_row = _gap_row(text, "JSONL mirror is append-only via `chattr +a`")
    assert "chattr +a" in chattr_row and "rotate_audit_logs.sh" in chattr_row
    src = (REPO / "src/episteme/audit_trail.py").read_text(encoding="utf-8")
    assert 'glob("audit-*.jsonl.gz")' in src
    rotate_src = (REPO / "scripts/data/rotate_audit_logs.sh").read_text(encoding="utf-8")
    assert "chattr -a" in rotate_src and "-mtime +30" in rotate_src


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
    assert uses_both("serialize_*.py"), (
        "no serialize module pairs record() with mirror_only fallback"
    )


def test_reason_enforcement_gap_row_reflects_record_level_check():
    # SP6 Task 3 moved manual_correction/schema_migration reason enforcement
    # into record() itself; the gap-table row must say so, and force_override
    # must still be described as CLI-only (record() itself must not raise
    # for it -- tests/data/test_audit_trail.py pins that at the code level).
    text = doc("11-gxp-data-integrity.md")
    row = _gap_row(text, "`reason` is required for `force_override`")
    assert "record()" in row
    assert "manual_correction" in row and "schema_migration" in row
    assert "force_override" in row
    assert "ValueError" in row

    src = (REPO / "src/episteme/audit_trail.py").read_text(encoding="utf-8")
    assert 'in ("manual_correction", "schema_migration") and not reason' in src


def test_silent_drop_gap_row_reflects_double_failure_warning():
    # SP6 Task 5: when record() AND the mirror_only() fallback both fail,
    # that double failure is now logged as a WARNING by the extract/serialize
    # _best_effort_audit helpers, not silently swallowed -- still non-fatal.
    text = doc("11-gxp-data-integrity.md")
    row = _gap_row(text, "No silent drops: every event produces an audit record")
    assert "_LOG.warning" in row
    assert "non-fatal" in row
    assert "mirror_only()" in row


def test_code_premise_double_audit_failure_is_logged_not_swallowed():
    # Every extract/serialize module whose _best_effort_audit falls back to
    # mirror_only() must also log a WARNING when that fallback itself raises
    # -- an invariant over every current and future source module, not a
    # count pinned to today's thirteen.
    base = REPO / "src/episteme/data"
    missing = []
    for pattern in ("extract_*.py", "serialize_*.py"):
        for f in base.rglob(pattern):
            src = f.read_text(encoding="utf-8")
            if "mirror_only(" not in src:
                continue
            if "_LOG.warning" not in src or "fallback also failed" not in src:
                missing.append(f.name)
    assert not missing, missing
