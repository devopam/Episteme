from __future__ import annotations

import json

import numpy as np
import pytest

from episteme.bench import term_model_bakeoff as bake
from episteme.reference.medcpt import TERM_MODELS

REL = "2026"

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


def _run(tmp_path, enc, models=("medcpt",), **kw):
    return bake.run(
        list(models),
        ACCEPT,
        tmp_path,
        vocabulary=lambda: ROWS,
        mesh_release=REL,
        encoders=_factory(enc),
        **kw,
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
    assert (tmp_path / "medcpt" / REL / f"{rev}.npy").exists()
    assert (tmp_path / "medcpt" / REL / f"{rev}.ids.json").exists()


def test_second_run_reuses_cache(tmp_path):
    first = FakeEncoder()
    _run(tmp_path, first)
    assert first.doc_calls == 1
    second = FakeEncoder()

    def no_vocab():
        raise AssertionError("vocabulary must not be read when the cache exists")

    bake.run(
        ["medcpt"],
        ACCEPT,
        tmp_path,
        vocabulary=no_vocab,
        mesh_release=REL,
        encoders=_factory(second),
    )
    assert second.doc_calls == 0
    assert second.query_calls == 4  # three questions + warm-up


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


class FakeStored:
    """Stand-in for the mesh_term_vec reader."""

    def __init__(self, reason=None, rows=ROWS):
        self.reason = reason
        self.rows = rows

    def check(self, spec, release):
        assert release == REL
        return (0, self.reason) if self.reason else (len(self.rows), None)

    def chunks(self, spec, release):
        enc = FakeEncoder()
        for lo, hi in ((0, 4), (4, len(self.rows))):  # two chunks
            part = self.rows[lo:hi]
            yield [r[0] for r in part], enc._doc_vecs([r[2] for r in part])


def _no_embed_encoders(spec):
    enc = FakeEncoder()

    def boom(*a, **k):
        raise AssertionError("must not embed")

    enc.encode_articles = boom
    enc.encode_texts = boom
    return enc, enc


def test_reuse_stored_vectors_skips_embedding(tmp_path):
    report = bake.run(
        ["medcpt"],
        ACCEPT,
        tmp_path,
        vocabulary=lambda: pytest.fail("vocabulary must not be read"),
        mesh_release=REL,
        encoders=_no_embed_encoders,
        stored=FakeStored(),
    )
    m = report["models"]["medcpt"]
    assert m["vocab_source"] == "mesh_term_vec"
    assert m["vocab_embed_seconds"] is None
    assert m["reuse_skipped_reason"] is None
    assert m["vocab_vectors"] == 7
    assert m["overall"]["top1"]["all"] == pytest.approx(1 / 3)
    assert "n/a" in (tmp_path / "REPORT.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("reason", ["stored vectors are x@y; need a@b", "covers 3 of 6"])
def test_fallback_to_embedding_with_reason(tmp_path, reason):
    enc = FakeEncoder()
    report = _run(tmp_path, enc, stored=FakeStored(reason=reason))
    m = report["models"]["medcpt"]
    assert m["vocab_source"] == "embedded"
    assert m["reuse_skipped_reason"] == reason
    assert enc.doc_calls == 1
    assert m["vocab_embed_seconds"] >= 0


def test_check_stored_matches_model_revision_rows_and_descriptors():
    spec = TERM_MODELS["medcpt"]
    ok = (spec.doc_model, spec.doc_revision, 10, 6)
    assert bake._check_stored(spec, 10, 6, [ok]) == (10, None)
    rows, reason = bake._check_stored(spec, 11, 6, [ok])
    assert rows == 0 and "10" in reason and "11" in reason
    rows, reason = bake._check_stored(spec, 10, 6, [ok], missing=2)
    assert rows == 0 and "2" in reason
    rows, reason = bake._check_stored(spec, 10, 6, [ok], extra=1)
    assert rows == 0 and "1" in reason
    rows, reason = bake._check_stored(spec, 10, 6, [(spec.doc_model, "other", 10, 6)])
    assert rows == 0 and "other" in reason and spec.doc_revision in reason
    assert bake._check_stored(spec, 10, 6, [])[1] is not None


def _part_files(tmp_path):
    return sorted(p.name for p in (tmp_path / "medcpt" / REL).glob("*.part-*"))


def test_parts_written_joined_and_cleaned(tmp_path):
    enc = FakeEncoder()
    report = _run(tmp_path, enc, part_size=3)
    assert enc.doc_calls == 3  # ceil(7 / 3)
    folder = tmp_path / "medcpt" / REL
    rev = TERM_MODELS["medcpt"].doc_revision
    assert _part_files(tmp_path) == []
    assert not (folder / f"{rev}.plan.json").exists()
    vecs = np.load(folder / f"{rev}.npy")
    assert vecs.shape == (7, 6) and vecs.dtype == np.float32
    assert json.loads((folder / f"{rev}.ids.json").read_text()) == [r[0] for r in ROWS]
    assert "embed_seconds" in json.loads((folder / f"{rev}.meta.json").read_text())
    assert report["models"]["medcpt"]["vocab_vectors"] == 7


class _DiesOn(FakeEncoder):
    def __init__(self, call: int) -> None:
        super().__init__()
        self.die_on = call

    def encode_articles(self, pairs):
        if self.doc_calls + 1 == self.die_on:
            raise KeyboardInterrupt
        return super().encode_articles(pairs)


def test_resume_encodes_only_missing_parts(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, _DiesOn(3), part_size=3)
    names = _part_files(tmp_path)
    assert any("part-0000.npy" in n for n in names) and any("part-0001.npy" in n for n in names)
    assert not any("part-0002" in n for n in names)
    enc = FakeEncoder()
    _run(tmp_path, enc, part_size=3)
    assert enc.doc_calls == 1  # only part 2 is missing
    assert _part_files(tmp_path) == []


def test_plan_mismatch_rebuilds(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, _DiesOn(2), part_size=3)
    assert _part_files(tmp_path)
    enc = FakeEncoder()
    bake.run(
        ["medcpt"],
        ACCEPT,
        tmp_path,
        vocabulary=lambda: ROWS[:6],  # row count changed
        mesh_release=REL,
        encoders=_factory(enc),
        part_size=3,
    )
    assert enc.doc_calls == 2  # all parts again: ceil(6 / 3)


def test_cache_folder_includes_release(tmp_path):
    _run(tmp_path, FakeEncoder())
    enc = FakeEncoder()
    bake.run(
        ["medcpt"],
        ACCEPT,
        tmp_path,
        vocabulary=lambda: ROWS,
        mesh_release="2027",
        encoders=_factory(enc),
    )
    assert enc.doc_calls == 1  # a new release does not reuse the old cache


def test_missing_meta_gives_none_seconds(tmp_path):
    _run(tmp_path, FakeEncoder())
    rev = TERM_MODELS["medcpt"].doc_revision
    (tmp_path / "medcpt" / REL / f"{rev}.meta.json").unlink()
    report = _run(tmp_path, FakeEncoder())
    assert report["models"]["medcpt"]["vocab_embed_seconds"] is None


def test_warmup_query_is_not_timed(tmp_path):
    import time

    class SlowFirst(FakeEncoder):
        def encode_queries(self, texts):
            if self.query_calls == 0:
                time.sleep(0.3)
            return super().encode_queries(texts)

    enc = SlowFirst()
    report = _run(tmp_path, enc)
    assert enc.query_calls == 4
    assert report["models"]["medcpt"]["query_ms_p95"] < 100


def test_part_size_option_is_validated(tmp_path, capsys):
    args = ["--models", "medcpt", "--acceptance", "x.json", "--out", str(tmp_path)]
    assert bake.main([*args, "--part-size", "0"]) == 2
    assert "--part-size" in capsys.readouterr().err


def test_truncated_part_is_encoded_again(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, _DiesOn(3), part_size=3)
    rev = TERM_MODELS["medcpt"].doc_revision
    part = tmp_path / "medcpt" / REL / f"{rev}.part-0000.npy"
    part.write_bytes(part.read_bytes()[:40])
    enc = FakeEncoder()
    _run(tmp_path, enc, part_size=3)
    assert enc.doc_calls == 2  # the broken part 0 and the missing part 2


def test_changed_vocabulary_text_rebuilds(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, _DiesOn(2), part_size=3)
    changed = [(ui, kind, title, text + "x") for ui, kind, title, text in ROWS]
    enc = FakeEncoder()
    bake.run(
        ["medcpt"],
        ACCEPT,
        tmp_path,
        vocabulary=lambda: changed,
        mesh_release=REL,
        encoders=_factory(enc),
        part_size=3,
    )
    assert enc.doc_calls == 3  # same row count, new text: all parts again


def test_changed_part_size_rebuilds(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, _DiesOn(2), part_size=3)
    enc = FakeEncoder()
    _run(tmp_path, enc, part_size=4)
    assert enc.doc_calls == 2  # ceil(7 / 4)


def test_load_stored_row_count_mismatch():
    class Stored:
        def __init__(self, n):
            self.n = n

        def chunks(self, spec, release):
            yield ["D"] * self.n, np.zeros((self.n, 2), dtype=np.float32)

    spec = TERM_MODELS["medcpt"]
    vecs, ids = bake._load_stored(Stored(3), spec, REL, 3)
    assert vecs.shape == (3, 2) and ids == ["D"] * 3
    with pytest.raises(RuntimeError):
        bake._load_stored(Stored(4), spec, REL, 3)
    with pytest.raises(RuntimeError):
        bake._load_stored(Stored(2), spec, REL, 3)


def test_report_says_stored_vectors_are_fp16(tmp_path):
    _run(tmp_path, FakeEncoder())
    md = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    assert "fp16" in md and "mesh_term_vec" in md
