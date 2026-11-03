"""The AACT and EPAR-section docs (beta data completion, Task 4) against the code. Read-only;
no DB, no network."""

from __future__ import annotations

import re

from episteme.data import article_schema
from episteme.ops import gate

from ._repo import REPO, doc

RUNBOOK = "10-data-sources-runbook.md"
BETA_LIMITS = (
    "no trial version history",
    "EMA scientific guidelines",
    "FDA Complete Response Letters",
    "OpenAlex",
)


def _row(name: str, source: str) -> str:
    """The table row of ``doc(name)`` that starts with the source in backticks or bold."""
    for line in doc(name).splitlines():
        if line.startswith("|") and (f"(`{source}`)" in line or line.startswith(f"| `{source}` |")):
            return line
    raise AssertionError(f"{name}: no row for {source}")


def _section(text: str, start: str) -> str:
    rest = text.split(start, 1)[1]
    return re.split(r"\n#{2,3} ", rest, maxsplit=1)[0]


def test_docs_02_records_the_aact_licence_posture():
    row = _row("02-data-sources.md", "aact")
    assert "public US government data" in row
    assert "sponsor-supplied documents are not loaded" in row.lower()
    assert "episteme.reg_release" in row and "aact_r<YYYYMMDD>" in row
    notes = doc("02-data-sources.md")
    assert (
        "not trained on" in notes
        and "`aact`" in notes.split("**Not in the training corpus.**")[1][:900]
    )


def test_docs_02_names_the_beta_limits():
    text = doc("02-data-sources.md")
    for phrase in BETA_LIMITS:
        assert phrase in text, phrase


def test_docs_12_aact_row_is_a_load_with_a_licence():
    row = _row("12-source-inventory.md", "aact")
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert "download, load" in cells[2] and "reg_release" in cells[2]
    assert "public US government data" in cells[3] and "not trained on" in cells[3]
    assert "UNVERIFIED" not in row


def test_docs_08_describes_the_aact_storage_and_the_label_columns():
    text = doc("08-data-storage-principles.md")
    assert "aact_r<YYYYMMDD>" in text and "intervention_substance" in text
    assert "migration 0011" in text
    section = text.split("### Postgres tables")[1]
    aact = [ln for ln in section.splitlines() if "aact_r" in ln]
    assert aact and "views" in aact[0] and "`aact`" in aact[0]
    label = [ln for ln in section.splitlines() if "episteme.label_section" in ln][0]
    assert "`ordinal`" in label and "`product`" in label and "ema_epar" in label


def test_docs_09_keeps_trials_out_of_articles_and_names_the_source_classes():
    text = doc("09-extraction-contract.md")
    assert "`aact` writes no `episteme.articles` rows" in text
    assert "TRAINING_SOURCES" in text and "REFERENCE_SOURCES" in text
    assert set(article_schema.REFERENCE_SOURCES) == {"dailymed", "ema_epar"}


def test_runbook_has_an_aact_section_with_the_operator_steps():
    text = doc(RUNBOOK)
    sec = _section(text, "### 5.16 AACT trials (`aact`)")
    for needle in (
        "run_pipeline.sh aact load",
        "run_pipeline.sh aact load --zip",
        "EPISTEME_AACT_MAX_ORPHAN_SHARE",
        "aact_r<YYYYMMDD>",
        "DROP SCHEMA",
        "_prev_",
        "--force --reason",
        "intervention_substance",
        "documents_with_sections",
        "documents_without_sections",
    ):
        assert needle in sec, needle
    for phrase in BETA_LIMITS:
        assert phrase in sec, phrase
    # The dispatcher carries --zip, so the gate pass applies: no direct wrapper call.
    assert "load_aact.sh --zip" not in sec
    assert "mesh_headings" in doc(RUNBOOK).split("`tables_with_rows` at least 45")[1][:400]
    # The steps: a load, the release switch, dropping an old schema, the EPAR counts.
    for title in (
        "Load.",
        "Switch to a release.",
        "Drop an old release schema.",
        "EPAR section counts.",
    ):
        assert f"**{title}**" in sec, title


def test_runbook_settings_table_lists_the_aact_orphan_key_and_it_exists_in_config():
    sec = _section(doc(RUNBOOK), "### 5.14 Settings of the regulatory sources")
    assert "`EPISTEME_AACT_MAX_ORPHAN_SHARE`" in sec
    config = (REPO / "src" / "episteme" / "config.py").read_text(encoding="utf-8")
    assert "EPISTEME_AACT_MAX_ORPHAN_SHARE" in config


def test_runbook_gate_table_lists_aact_with_the_shipped_rules():
    text = doc(RUNBOOK)
    gate_sec = _section(text, "### 4.9 The sample gate")
    row = [ln for ln in gate_sec.splitlines() if ln.startswith("| `aact` |")][0]
    assert "`load`" in row and "4 GB" in row
    rules = gate.load_rules("aact")["load"]
    assert rules["max_peak_rss_mb"] == 2048 and rules["max_orphan_share_pct"] == 5.0
    for key in ("max_empty_tables", "studies_equal_distinct_nct_id", "max_orphan_share_pct"):
        assert f"`{key}`" in gate_sec, key
    assert "sections_share_pct" in gate_sec  # EPAR share: reported, no limit


def test_runbook_stage_table_shows_aact_as_download_and_load():
    text = doc(RUNBOOK)
    rows = [ln for ln in text.splitlines() if ln.startswith("| ") and "`aact`" in ln]
    stage_rows = [ln for ln in rows if "| yes |" in ln and "download" in ln]
    assert stage_rows, "no stage-table row for aact"
    assert not any("`aact`, `cdisc_bc`" in ln for ln in stage_rows)


def test_drift_log_records_the_beta_data_completion():
    text = doc("project-incubation-baseline.md")
    entry = text.split("Beta data completion: AACT trials", 1)[1]
    for phrase in BETA_LIMITS:
        assert phrase in entry, phrase
    for needle in (
        "migration 0011",
        "aact_r<YYYYMMDD>",
        "**Rulings:**",
        "**Deferred (compact):**",
        "**pg tests written, not run:**",
        "**Rollout:**",
        "REFERENCE_SOURCES",
        "_prev_",
        "left(col, 500)",
        "wrapped cross-reference",
    ):
        assert needle in entry, needle
