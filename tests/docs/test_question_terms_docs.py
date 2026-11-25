"""Question to MeSH terms (migration 0018): the runbook section 5.20, the design and source
docs, and the acceptance list. Read-only; no DB, no network."""

from __future__ import annotations

import json
import re
from collections import Counter

import pytest

from episteme import config

from ._repo import REPO, doc

RUNBOOK = "10-data-sources-runbook.md"
ACCEPTANCE = REPO / "tests" / "reference" / "data" / "question_terms_acceptance.json"
# A local workspace file (UI<TAB>name of every descriptor of the loaded desc2026). Absent in
# CI: the test that needs it skips.
MESH_TSV = (
    REPO
    / ".superpowers"
    / "sdd"
    / "2026-11-24-question-to-mesh-terms"
    / "mesh_descriptors_2026.tsv"
)
KINDS = ("acronym", "lay", "description")


def _section(text: str, start: str) -> str:
    assert start in text, f"missing heading: {start}"
    rest = text.split(start, 1)[1]
    return re.split(r"\n#{2,3} ", rest, maxsplit=1)[0]


def _entries() -> list[dict]:
    return json.loads(ACCEPTANCE.read_text(encoding="utf-8"))


def test_runbook_section_5_20_has_the_rollout_steps():
    sec = _section(doc(RUNBOOK), "### 5.20 Question to standard terms")
    for sub in ("#### 5.20.1", "#### 5.20.2", "#### 5.20.3", "#### 5.20.4", "#### 5.20.5"):
        assert sub in sec, sub
    assert "0018_question_terms" in sec
    assert "grants/0015_api_roles.sql" in sec
    assert "episteme.reference.api_settings sync" in sec
    assert f"{len(config.API_RESULT_LIMITS)} limits" in sec
    assert "-m episteme.reference.medcpt download" in sec
    assert "EPISTEME_RAW_ROOT=D:/episteme/01_raw" in sec
    assert "run_pipeline.sh mesh graph" in sec
    assert "run_pipeline.sh mesh embed --limit 2000" in sec
    assert "ANALYZE episteme.mesh_term, episteme.mesh_term_vec" in sec
    assert "scripts/question-mcp.sh" in sec
    assert "no_elicitation" in sec
    assert "scripts/ops/question_terms_acceptance.py" in sec
    assert "mesh_term_vec_hnsw_idx" in sec


def test_runbook_migration_table_and_loop_reach_0018():
    text = doc(RUNBOOK)
    assert "### 2.3 Migrations: 0001 blocked, 0002-0018 applied by hand" in text
    row = [ln for ln in text.splitlines() if ln.startswith("| `0018_question_terms.sql`")]
    assert row, "no 0018 row in the migration table"
    assert "5.20" in row[0]
    loop = [ln for ln in text.splitlines() if "for f in 0002_container_and_book_parts" in ln][0]
    assert loop.split("; do")[0].split()[-1] == "0018_question_terms"


def test_runbook_names_the_new_keys():
    keys = _section(doc(RUNBOOK), "### 1.2 Keys (names only)")
    for key in ("EPISTEME_QUESTION_LOG_DIR", "EPISTEME_MEDCPT_THREADS", "EPISTEME_MEDCPT_BATCH"):
        assert f"`{key}`" in keys, key


def test_design_6_2_is_the_as_built_question_to_terms_design():
    text = doc("13-graphrag-design.md")
    assert "### 6.2 Semantic search: the next spec" not in text
    sec = _section(text, "### 6.2 Question to standard terms (as built)")
    assert "2026-11-24-question-to-mesh-terms-design.md" in sec
    for name in ("match_terms", "suggest_terms", "search_terms", "translate_question"):
        assert name in sec, name
    table = _section(text, "### 3.2 SQL lookup API")
    for name in ("match_terms(", "suggest_terms(", "search_terms("):
        assert name in table, name


def test_data_sources_lists_the_medcpt_models():
    text = doc("02-data-sources.md")
    row = [ln for ln in text.splitlines() if ln.startswith("| **MedCPT")]
    assert row, "no MedCPT row in 02-data-sources.md"
    assert "public domain" in row[0].lower()
    assert "US government work" in row[0]
    assert config.MEDCPT_QUERY_REVISION in row[0]
    assert config.MEDCPT_ARTICLE_REVISION in row[0]


def test_drift_log_has_the_question_terms_entry():
    text = doc("project-incubation-baseline.md")
    assert "question to mesh terms" in text.lower()
    assert "2026-11-24-question-to-mesh-terms-design.md" in text


def test_acceptance_list_shape():
    entries = _entries()
    assert len(entries) >= 30
    counts = Counter(e["kind"] for e in entries)
    assert set(counts) <= set(KINDS), counts
    for kind in KINDS:
        assert counts[kind] >= 8, (kind, counts)
    questions = [e["question"] for e in entries]
    assert len(set(questions)) == len(questions), "duplicate question"
    for e in entries:
        assert set(e) == {"question", "kind", "expected", "expected_names"}, e
        assert e["question"].strip() == e["question"] and e["question"], e
        assert e["expected"], e
        assert len(e["expected"]) == len(set(e["expected"])), e
        assert len(e["expected_names"]) == len(e["expected"]), e
        for ui in e["expected"]:
            assert re.fullmatch(r"D\d{6,9}", ui), ui


def test_acceptance_uis_are_desc2026_descriptors_with_the_intended_names():
    if not MESH_TSV.is_file():
        pytest.skip("local MeSH descriptor list not present")
    names = {}
    for line in MESH_TSV.read_text(encoding="utf-8").splitlines():
        ui, name = line.split("\t")
        names[ui] = name
    for e in _entries():
        for ui, want in zip(e["expected"], e["expected_names"], strict=True):
            assert ui in names, (e["question"], ui)
            assert names[ui] == want, (e["question"], ui, names[ui], want)
