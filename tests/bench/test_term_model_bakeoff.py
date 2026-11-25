from __future__ import annotations

import json

import numpy as np
import pytest

from episteme.bench import term_model_bakeoff as bake
from episteme.reference.medcpt import TERM_MODELS

# 6 descriptors, one axis each. D6 has an extra entry row near D1.
ROWS = [
    ("D1", "descriptor", "alpha", "scope one"),
    ("D2", "descriptor", "beta", ""),
    ("D3", "descriptor", "gamma", ""),
    ("D4", "descriptor", "delta", ""),
    ("D5", "descriptor", "eps", ""),
    ("D6", "descriptor", "zeta", ""),
    ("D6", "entry", "zeta synonym", ""),
]
ACCEPT = [
    {"question": "q1", "kind": "acronym", "expected": ["D1"]},
    {"question": "q2", "kind": "lay", "expected": ["D1", "D2"]},
    {"question": "q3", "kind": "description", "expected": ["D3"]},
]
# Scripted ranking per question (best first, by descriptor axis index 0..5).
SCRIPT = {
    "q1": [0, 1, 2, 3, 4, 5],  # D1 first
    "q2": [0, 2, 3, 4, 5, 1],  # D1 rank 1, D2 rank 6
    "q3": [1, 3, 2, 0, 4, 5],  # D3 rank 3
}


def _unit(i: int, w: float = 1.0) -> np.ndarray:
    v = np.zeros(6, dtype=np.float32)
    v[i] = w
    return v


class FakeEncoder:
    def __init__(self) -> None:
        self.doc_calls = 0
        self.query_calls = 0

    def _doc_vecs(self, titles: list[str]) -> np.ndarray:
        out = []
        for t in titles:
            idx = {"alpha": 0, "beta": 1, "gamma": 2, "delta": 3, "eps": 4, "zeta": 5}
            key = t.split(".")[0].split()[0]
            out.append(_unit(idx[key]))
        return np.stack(out)

    def encode_articles(self, pairs):
        self.doc_calls += 1
        return self._doc_vecs([t for t, _ in pairs])

    def encode_texts(self, texts):
        self.doc_calls += 1
        return self._doc_vecs(texts)

    def encode_queries(self, texts):
        self.query_calls += 1
        (q,) = texts
        order = SCRIPT[q]
        v = np.zeros(6, dtype=np.float32)
        for rank, axis in enumerate(order):
            v[axis] = 10.0 - rank
        return v[None, :]


def _factory(enc):
    return lambda spec: (enc, enc)


def _run(tmp_path, enc, models=("medcpt",)):
    return bake.run(
        list(models),
        ACCEPT,
        tmp_path,
        vocabulary=lambda: ROWS,
        encoders=_factory(enc),
    )


def test_hit_rates_exact_and_split(tmp_path):
    report = _run(tmp_path, FakeEncoder())
    m = report["models"]["medcpt"]
    o = m["overall"]
    assert o["n"] == 3
    # top1: q1 hit, q2 no, q3 no
    assert o["top1"]["all"] == pytest.approx(1 / 3)
    assert o["top1"]["any"] == pytest.approx(2 / 3)
    # top5: q1 hit, q2 any only (D2 rank 6), q3 hit
    assert o["top5"]["all"] == pytest.approx(2 / 3)
    assert o["top5"]["any"] == pytest.approx(1.0)
    # top10: every descriptor within 6
    assert o["top10"]["all"] == pytest.approx(1.0)
    assert m["by_kind"]["acronym"]["top1"]["all"] == 1.0
    assert m["by_kind"]["lay"]["top5"]["all"] == 0.0
    assert m["by_kind"]["lay"]["top5"]["any"] == 1.0
    assert m["by_kind"]["description"]["top5"]["all"] == 1.0
    assert m["by_kind"]["description"]["top1"]["any"] == 0.0


def test_report_lists_metrics_and_files(tmp_path):
    _run(tmp_path, FakeEncoder(), models=("medcpt", "bge_base"))
    rep = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    for name in ("medcpt", "bge_base"):
        m = rep["models"][name]
        assert m["query_ms_mean"] >= 0
        assert m["query_ms_p95"] >= 0
        assert m["vocab_embed_seconds"] >= 0
        assert m["vocab_vectors"] == 7
        assert m["vocab_bytes"] == 7 * 6 * 4
        assert m["licence"] == TERM_MODELS[name].licence
    assert rep["models"]["bge_base"]["licence"] == "MIT"
    md = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    assert "medcpt" in md and "bge_base" in md and "MIT" in md
    assert "top-10" in md and "SapBERT" in md and "UMLS" in md
    rev = TERM_MODELS["medcpt"].doc_revision
    assert (tmp_path / "medcpt" / f"{rev}.npy").exists()
    assert (tmp_path / "medcpt" / f"{rev}.ids.json").exists()


def test_second_run_reuses_cache(tmp_path):
    first = FakeEncoder()
    _run(tmp_path, first)
    assert first.doc_calls == 1
    second = FakeEncoder()

    def no_vocab():
        raise AssertionError("vocabulary must not be read when the cache exists")

    bake.run(["medcpt"], ACCEPT, tmp_path, vocabulary=no_vocab, encoders=_factory(second))
    assert second.doc_calls == 0
    assert second.query_calls == 3


def test_document_text_symmetric_vs_pair():
    row = ("D1", "descriptor", "alpha", "scope")
    assert bake.document_inputs(TERM_MODELS["bge_base"], [row]) == ["alpha. scope"]
    assert bake.document_inputs(TERM_MODELS["bge_base"], [("D1", "entry", "alpha", "")]) == [
        "alpha"
    ]
    assert bake.document_inputs(TERM_MODELS["medcpt"], [row]) == [("alpha", "scope")]


def test_sapbert_is_not_a_model():
    assert "sapbert" not in TERM_MODELS


def test_unknown_model_exits_2(tmp_path, capsys):
    rc = bake.main(["--models", "medcpt,nope", "--acceptance", "x.json", "--out", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "nope" in err and "medcpt" in err and "bge_base" in err
