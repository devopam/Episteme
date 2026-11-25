"""MedCPT encoder wrapper: pooling, truncation, pair input, cache errors."""

from __future__ import annotations

import numpy as np
import pytest
import torch

from episteme.reference import medcpt
from episteme.reference.medcpt import TERM_MODELS, MedCPTEncoder, TermModelSpec


class FakeTokenizer:
    def __init__(self) -> None:
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        n = len(texts)
        return {
            "input_ids": torch.zeros((n, 3), dtype=torch.long),
            "attention_mask": torch.tensor([[1, 1, 0]] * n),
        }


class FakeModel:
    """Returns hidden states: token 0 = row index + 1, token 1 = 3, token 2 = 100."""

    def __call__(self, **inputs):
        n = inputs["input_ids"].shape[0]
        hidden = torch.zeros((n, 3, 768))
        for i in range(n):
            hidden[i, 0, :] = float(i + 1)
            hidden[i, 1, :] = 3.0
            hidden[i, 2, :] = 100.0
        return type("Out", (), {"last_hidden_state": hidden})()


def _enc(max_length=64, **kw):
    tok = FakeTokenizer()
    enc = MedCPTEncoder("m", "r", max_length, tokenizer=tok, model=FakeModel(), **kw)
    return enc, tok


def test_cls_pooling_takes_token_zero():
    enc, _ = _enc()
    out = enc.encode_queries(["a", "b"])
    assert out.dtype == np.float32
    assert out.shape == (2, 768)
    assert out[0, 0] == 1.0 and out[1, 0] == 2.0


def test_query_truncates_at_64():
    assert TERM_MODELS["medcpt"].query_max_length == 64


def test_query_path_passes_max_length_and_texts():
    enc, tok = _enc(max_length=64)
    enc.encode_queries(["heart attack"])
    texts, kw = tok.calls[0]
    assert texts == ["heart attack"]
    assert kw["max_length"] == 64 and kw["truncation"] is True


def test_article_path_passes_pairs_at_512():
    enc, tok = _enc(max_length=512)
    out = enc.encode_articles([("Title", "Text"), ("T2", "")])
    texts, kw = tok.calls[0]
    assert [list(p) for p in texts] == [["Title", "Text"], ["T2", ""]]
    assert kw["max_length"] == 512 and kw["truncation"] is True
    assert out.shape == (2, 768) and out.dtype == np.float32


def test_empty_input_returns_empty_matrix():
    enc, _ = _enc()
    assert enc.encode_queries([]).shape == (0, 768)


def test_batches_by_configured_size(monkeypatch):
    monkeypatch.setattr(medcpt, "_batch_size", lambda: 2)
    enc, tok = _enc()
    out = enc.encode_queries(["a", "b", "c"])
    assert [len(c[0]) for c in tok.calls] == [2, 1]
    assert out.shape == (3, 768)


def test_missing_cache_raises_exact_message(monkeypatch):
    def boom(*a, **k):
        raise OSError("not cached")

    monkeypatch.setattr(medcpt.AutoTokenizer, "from_pretrained", boom)
    spec = TERM_MODELS["medcpt"]
    with pytest.raises(RuntimeError) as exc:
        MedCPTEncoder.query()
    assert str(exc.value) == (
        f"model {spec.query_model}@{spec.query_revision} is not in the Hugging Face cache; "
        "run `python -m episteme.reference.medcpt download --model medcpt` (runbook 5.20.2)"
    )


def test_missing_cache_message_names_the_bge_model(monkeypatch):
    def boom(*a, **k):
        raise OSError("not cached")

    monkeypatch.setattr(medcpt.AutoTokenizer, "from_pretrained", boom)
    spec = TERM_MODELS["bge_base"]
    with pytest.raises(RuntimeError) as exc:
        MedCPTEncoder.query(spec)
    assert f"model {spec.query_model}@{spec.query_revision} " in str(exc.value)
    assert "download --model bge_base" in str(exc.value)


def test_download_cli_takes_a_model_name(monkeypatch, capsys):
    got: list[str] = []
    monkeypatch.setattr(medcpt, "download_models", got.append)
    assert medcpt.main(["download"]) == 0
    assert medcpt.main(["download", "--model", "bge_base"]) == 0
    assert got == ["medcpt", "bge_base"]
    assert medcpt.main(["download", "--model", "nope"]) == 2
    assert "unknown model 'nope'" in capsys.readouterr().err
    assert medcpt.main(["fetch"]) == 2


def test_download_models_fetches_a_shared_model_once(monkeypatch):
    import huggingface_hub

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        huggingface_hub,
        "snapshot_download",
        lambda repo_id, revision: seen.append((repo_id, revision)),
    )
    medcpt.download_models("bge_base")
    spec = TERM_MODELS["bge_base"]
    assert seen == [(spec.query_model, spec.query_revision)]
    seen.clear()
    medcpt.download_models("medcpt")
    assert len(seen) == 2


def test_loads_with_local_files_only_and_pinned_revision(monkeypatch):
    seen: list[dict] = []

    def tok(model_id, **kw):
        seen.append({"id": model_id, **kw})
        return FakeTokenizer()

    def mdl(model_id, **kw):
        seen.append({"id": model_id, **kw})
        return FakeModel()

    monkeypatch.setattr(medcpt.AutoTokenizer, "from_pretrained", tok)
    monkeypatch.setattr(medcpt.AutoModel, "from_pretrained", mdl)
    spec = TERM_MODELS["medcpt"]
    enc = MedCPTEncoder.article()
    assert enc.model_id == spec.doc_model and enc.revision == spec.doc_revision
    assert all(s["local_files_only"] is True and s["revision"] == spec.doc_revision for s in seen)
    assert len(seen) == 2


def test_medcpt_spec_values():
    spec = TERM_MODELS["medcpt"]
    assert spec.query_model == "ncbi/MedCPT-Query-Encoder"
    assert spec.query_revision == "d83a36cc6b8e3a5c5e9d9d6ba156808c1643dcbc"
    assert spec.doc_model == "ncbi/MedCPT-Article-Encoder"
    assert spec.doc_revision == "d05a736da4bb84ee4057b7f7999485be6ed85465"
    assert (spec.pooling, spec.normalize, spec.query_prefix) == ("cls", False, "")
    assert (spec.query_max_length, spec.doc_max_length) == (64, 512)


def test_mean_pooling_averages_over_attention_mask():
    enc, _ = _enc(pooling="mean")
    out = enc.encode_queries(["a"])
    # mask [1,1,0]: mean of token0 (1.0) and token1 (3.0) = 2.0; token 2 ignored
    assert out[0, 0] == pytest.approx(2.0)


def test_normalize_gives_unit_vectors():
    enc, _ = _enc(normalize=True)
    out = enc.encode_queries(["a", "b"])
    assert np.linalg.norm(out, axis=1) == pytest.approx(np.ones(2), abs=1e-5)


def test_query_prefix_prepended_to_queries_only():
    enc, tok = _enc(prefix="query: ")
    enc.encode_queries(["heart attack"])
    enc.encode_articles([("Title", "Text")])
    assert tok.calls[0][0] == ["query: heart attack"]
    assert [list(p) for p in tok.calls[1][0]] == [["Title", "Text"]]


def test_spec_builds_encoders_from_spec(monkeypatch):
    seen: list[dict] = []

    def tok(model_id, **kw):
        seen.append({"id": model_id, **kw})
        return FakeTokenizer()

    monkeypatch.setattr(medcpt.AutoTokenizer, "from_pretrained", tok)
    monkeypatch.setattr(medcpt.AutoModel, "from_pretrained", lambda *a, **k: FakeModel())
    spec = TermModelSpec(
        name="x",
        query_model="q/m",
        query_revision="qr",
        doc_model="d/m",
        doc_revision="dr",
        pooling="mean",
        normalize=True,
        query_prefix="Q: ",
        query_max_length=32,
        doc_max_length=128,
    )
    q = MedCPTEncoder.query(spec)
    assert (q.model_id, q.revision, q.max_length) == ("q/m", "qr", 32)
    a = MedCPTEncoder.article(spec)
    assert (a.model_id, a.revision, a.max_length) == ("d/m", "dr", 128)
    assert seen[0]["id"] == "q/m"


@pytest.mark.slow
def test_real_models_rank_synonym_above_unrelated():
    try:
        enc_q = MedCPTEncoder.query()
        enc_a = MedCPTEncoder.article()
    except RuntimeError:
        pytest.skip("MedCPT models are not in the Hugging Face cache")
    q = enc_q.encode_queries(["heart attack"])[0]
    a = enc_a.encode_articles([("myocardial infarction", ""), ("fracture", "")])
    assert float(q @ a[0]) > float(q @ a[1])
