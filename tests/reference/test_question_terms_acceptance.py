"""scripts/ops/question_terms_acceptance.py with a fake reader and a fake encoder. No DB, no
model."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from episteme.reference.question_mcp import QuestionService

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ops" / "question_terms_acceptance.py"
DOC_REV = "doc-rev-1"


def _load():
    spec = importlib.util.spec_from_file_location("question_terms_acceptance", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


acc = _load()


def _term(ui: str, name: str, basis: str, score: float | None = None) -> dict:
    return {
        "kind": "term",
        "descriptor_ui": ui,
        "descriptor_name": name,
        "matched_text": None,
        "matched_term": name,
        "term_kind": "name",
        "concept_relation": None,
        "basis": basis,
        "score": score,
        "mesh_release": "2026",
        "note": None,
    }


def _note(text: str) -> dict:
    return {"kind": "note", "note": text}


class FakeDB:
    """match_terms by question; suggest_terms by the vector's first value."""

    def __init__(self) -> None:
        self.match = {
            "SJS": [_term("D013262", "Stevens-Johnson Syndrome", "exact")],
            "heart attack": [_note("unmatched: heart attack")],
            "fits": [_note("unmatched: fits")],
        }
        self.suggest = [
            _term("D009203", "Myocardial Infarction", "medcpt", 0.91),
            _term("D006331", "Heart Diseases", "medcpt", 0.85),
        ]
        self.suggest_calls: list[tuple[str, str]] = []

    def match_terms(self, question):
        return list(self.match[question])

    def suggest_terms(self, vector, model_revision):
        self.suggest_calls.append((vector, model_revision))
        return list(self.suggest)

    def search_terms(self, descriptor_uis, sources):  # pragma: no cover - not used
        raise AssertionError("acceptance must not search")

    def literature_by_mesh(self, *a):  # pragma: no cover - not used
        raise AssertionError("acceptance must not search")


class FakeEncoder:
    def __init__(self) -> None:
        self.calls = 0

    def encode_queries(self, texts):
        self.calls += len(texts)
        return [[0.5] * 768 for _ in texts]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 0.25
        return self.t


ENTRIES = [
    {
        "question": "SJS",
        "kind": "acronym",
        "expected": ["D013262"],
        "expected_names": ["Stevens-Johnson Syndrome"],
    },
    {
        "question": "heart attack",
        "kind": "lay",
        "expected": ["D009203"],
        "expected_names": ["Myocardial Infarction"],
    },
    {
        "question": "fits",
        "kind": "lay",
        "expected": ["D012640"],
        "expected_names": ["Seizures"],
    },
]


def _service(tmp_path, db, encoder):
    clock = Clock()
    timed = acc.TimedDB(db, clock)
    service = QuestionService(
        timed,
        tmp_path / "log",
        encoder_factory=lambda: encoder,
        model_revision=lambda: DOC_REV,
    )
    return service, timed, clock


def test_hits_per_question(tmp_path):
    db, enc = FakeDB(), FakeEncoder()
    service, timed, clock = _service(tmp_path, db, enc)
    report = acc.run(ENTRIES, service, timed, clock=clock)
    by_q = {r["question"]: r for r in report["questions"]}

    sjs = by_q["SJS"]
    assert sjs["exact_hit"] and sjs["top10_hit"] and sjs["any_hit"]
    assert sjs["suggest_ran"] is False
    assert sjs["exact_uis"] == ["D013262"] and sjs["suggested_uis"] == []

    ha = by_q["heart attack"]
    assert not ha["exact_hit"] and ha["top10_hit"] and ha["any_hit"]
    assert ha["suggest_ran"] is True
    assert ha["suggested_uis"] == ["D009203", "D006331"]
    assert ha["notes"] == ["unmatched: heart attack"]

    fits = by_q["fits"]
    assert not fits["exact_hit"] and not fits["top10_hit"] and not fits["any_hit"]


def test_suggest_gets_the_document_side_revision_and_only_when_words_stay_unmatched(tmp_path):
    db, enc = FakeDB(), FakeEncoder()
    service, timed, clock = _service(tmp_path, db, enc)
    acc.run(ENTRIES, service, timed, clock=clock)
    # warm-up (SJS: no suggestion) + 2 unmatched questions
    assert [rev for _, rev in db.suggest_calls] == [DOC_REV, DOC_REV]
    assert enc.calls == 2


def test_top_k_cuts_the_suggestions(tmp_path):
    db, enc = FakeDB(), FakeEncoder()
    db.suggest = [_term("D006331", "Heart Diseases", "medcpt", 0.9)] + db.suggest[:1]
    service, timed, clock = _service(tmp_path, db, enc)
    report = acc.run(ENTRIES[1:2], service, timed, clock=clock, top_k=1)
    q = report["questions"][0]
    assert q["suggested_uis"] == ["D006331"]
    assert not q["top10_hit"] and not q["any_hit"]


def test_summary_and_times(tmp_path):
    db, enc = FakeDB(), FakeEncoder()
    service, timed, clock = _service(tmp_path, db, enc)
    report = acc.run(ENTRIES, service, timed, clock=clock)
    s = report["summary"]
    assert s["all"] == {
        "n": 3,
        "exact_hit": 1,
        "top10_hit": 2,
        "any_hit": 2,
        "exact_share": pytest.approx(1 / 3),
        "top10_share": pytest.approx(2 / 3),
    }
    assert s["by_kind"]["lay"]["n"] == 2 and s["by_kind"]["lay"]["top10_hit"] == 1
    assert s["by_kind"]["acronym"]["exact_hit"] == 1
    assert "description" not in s["by_kind"]  # no entry of that kind here
    t = report["times"]
    assert t["cold_call_s"] > 0 and t["target_warm_s"] == 5.0
    assert set(t) >= {"mean_s", "p95_s", "max_s", "within_target"}
    for q in report["questions"]:
        assert q["total_s"] > 0 and q["match_s"] > 0
        assert (q["suggest_s"] > 0) == q["suggest_ran"]
    assert "exact_hit" in report["definitions"] and "top10_hit" in report["definitions"]


def test_main_writes_the_report_and_exits_0(tmp_path, monkeypatch, capsys):
    db, enc = FakeDB(), FakeEncoder()
    acc_file = tmp_path / "acc.json"
    acc_file.write_text(json.dumps(ENTRIES), encoding="utf-8")
    out = tmp_path / "out" / "report.json"

    def fake_build(log_dir, clock):
        service, timed, _ = _service(tmp_path, db, enc)
        assert log_dir == out.parent / "question_log"
        return service, timed, {"database": "episteme_test", "model_load_s": 0.0}

    monkeypatch.setattr(acc, "build", fake_build)
    rc = acc.main(["--acceptance", str(acc_file), "--out", str(out)])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["meta"]["database"] == "episteme_test"
    assert report["meta"]["doc_revision"]
    assert len(report["questions"]) == 3
    printed = capsys.readouterr().out
    assert "exact=Y top10=Y" in printed and "SJS" in printed
    assert "exact=N top10=N" in printed and "fits" in printed
    assert "top-10 hit: 2/3" in printed


def test_main_config_error_exits_2(tmp_path, monkeypatch, capsys):
    from episteme import config

    def boom(log_dir, clock):
        raise config.ConfigError("EPISTEME_API_READER_PASSWORD is not set")

    monkeypatch.setattr(acc, "build", boom)
    rc = acc.main(["--out", str(tmp_path / "r.json")])
    assert rc == 2
    assert "EPISTEME_API_READER_PASSWORD" in capsys.readouterr().err


def test_main_missing_package_exits_2(tmp_path, monkeypatch, capsys):
    def no_torch(log_dir, clock):
        raise ImportError("No module named 'torch'")

    monkeypatch.setattr(acc, "build", no_torch)
    rc = acc.main(["--out", str(tmp_path / "r.json")])
    assert rc == 2
    assert "torch" in capsys.readouterr().err
