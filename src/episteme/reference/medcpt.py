"""MedCPT encoders (NCBI) for question-to-MeSH-terms.

The article encoder embeds MeSH descriptors and entry terms as (title, text) pairs.
The query encoder embeds a reviewer's question. Vectors are 768 floats, compared by
inner product. Construction never downloads: models load from the local Hugging Face
cache at a pinned revision. Only ``download_models()`` downloads.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

from episteme import config

DIMENSIONS = 768
QUERY_MAX_LENGTH = 64
ARTICLE_MAX_LENGTH = 512


@dataclass(frozen=True)
class TermModelSpec:
    """One embedding model pair for MeSH terms. A later model switch is a new spec."""

    name: str
    query_model: str
    query_revision: str
    doc_model: str
    doc_revision: str
    pooling: str  # "cls" or "mean"
    normalize: bool
    query_prefix: str
    query_max_length: int
    doc_max_length: int


TERM_MODELS: dict[str, TermModelSpec] = {
    "medcpt": TermModelSpec(
        name="medcpt",
        query_model=config.MEDCPT_QUERY_MODEL,
        query_revision=config.MEDCPT_QUERY_REVISION,
        doc_model=config.MEDCPT_ARTICLE_MODEL,
        doc_revision=config.MEDCPT_ARTICLE_REVISION,
        pooling="cls",
        normalize=False,
        query_prefix="",
        query_max_length=QUERY_MAX_LENGTH,
        doc_max_length=ARTICLE_MAX_LENGTH,
    )
}


def _batch_size() -> int:
    return config.get_settings().medcpt_batch


def _threads() -> int:
    return config.get_settings().medcpt_threads


class MedCPTEncoder:
    def __init__(
        self,
        model_id: str,
        revision: str,
        max_length: int,
        *,
        tokenizer: Any = None,
        model: Any = None,
        pooling: str = "cls",
        normalize: bool = False,
        prefix: str = "",
    ) -> None:
        if pooling not in ("cls", "mean"):
            raise ValueError(f"pooling must be 'cls' or 'mean', got {pooling!r}")
        self.model_id = model_id
        self.revision = revision
        self.max_length = max_length
        self.pooling = pooling
        self.normalize = normalize
        self.prefix = prefix
        if tokenizer is None or model is None:
            tokenizer, model = self._load(model_id, revision)
        self._tokenizer = tokenizer
        self._model = model
        if hasattr(model, "eval"):
            model.eval()

    @staticmethod
    def _load(model_id: str, revision: str) -> tuple[Any, Any]:
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                model_id, revision=revision, local_files_only=True
            )
            model = AutoModel.from_pretrained(model_id, revision=revision, local_files_only=True)
        except OSError as exc:
            raise RuntimeError(
                f"MedCPT model {model_id}@{revision} is not in the Hugging Face cache; "
                "run the model download step (runbook 5.20.2)"
            ) from exc
        return tokenizer, model

    @classmethod
    def query(cls, spec: TermModelSpec | None = None) -> MedCPTEncoder:
        spec = spec or TERM_MODELS["medcpt"]
        return cls(
            spec.query_model,
            spec.query_revision,
            spec.query_max_length,
            pooling=spec.pooling,
            normalize=spec.normalize,
            prefix=spec.query_prefix,
        )

    @classmethod
    def article(cls, spec: TermModelSpec | None = None) -> MedCPTEncoder:
        spec = spec or TERM_MODELS["medcpt"]
        return cls(
            spec.doc_model,
            spec.doc_revision,
            spec.doc_max_length,
            pooling=spec.pooling,
            normalize=spec.normalize,
        )

    def _encode(self, inputs: list) -> np.ndarray:
        if not inputs:
            return np.zeros((0, DIMENSIONS), dtype=np.float32)
        torch.set_num_threads(_threads())
        step = _batch_size()
        chunks: list[np.ndarray] = []
        with torch.no_grad():
            for start in range(0, len(inputs), step):
                batch = inputs[start : start + step]
                enc = self._tokenizer(
                    batch,
                    truncation=True,
                    padding=True,
                    max_length=self.max_length,
                    return_tensors="pt",
                )
                hidden = self._model(**enc).last_hidden_state
                if self.pooling == "cls":
                    vec = hidden[:, 0, :]
                else:
                    mask = enc["attention_mask"].unsqueeze(-1).to(hidden.dtype)
                    vec = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
                if self.normalize:
                    vec = torch.nn.functional.normalize(vec, dim=-1)
                chunks.append(vec.to(torch.float32).cpu().numpy())
        return np.concatenate(chunks, axis=0).astype(np.float32, copy=False)

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self._encode([self.prefix + t for t in texts])

    def encode_articles(self, pairs: list[tuple[str, str]]) -> np.ndarray:
        return self._encode([[title, text] for title, text in pairs])


def download_models() -> None:
    """Fetch both encoders at the pinned revisions into the Hugging Face cache."""
    from huggingface_hub import snapshot_download

    spec = TERM_MODELS["medcpt"]
    for model_id, revision in (
        (spec.query_model, spec.query_revision),
        (spec.doc_model, spec.doc_revision),
    ):
        snapshot_download(repo_id=model_id, revision=revision)
        print(f"downloaded {model_id}@{revision}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["download"]:
        print("usage: python -m episteme.reference.medcpt download", file=sys.stderr)
        return 2
    download_models()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
