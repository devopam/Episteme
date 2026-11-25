"""Term-model bake-off: which embedding model finds the right MeSH descriptors.

Every acceptance question is embedded (the exact step ``match_terms`` is not part of
this comparison). Search is exact: a numpy inner product over the full vocabulary
cache, so the comparison measures the model and not an index. A descriptor scores as
the best of its vectors (descriptor row and entry rows), as ``suggest_terms`` groups
by descriptor.

Hit definitions, per top-k (k = 1, 5, 10):
- ``all``: every expected descriptor UI is in the top k.
- ``any``: at least one expected descriptor UI is in the top k.

Vocabulary vectors are cached at ``<out>/<model>/<revision>.npy`` with
``<revision>.ids.json`` (descriptor UI per row) and ``<revision>.meta.json``
(embedding time). A second run reuses the cache.

Licence posture: SapBERT (cambridgeltl/SapBERT-from-PubMedBERT-fulltext) is excluded.
Its model card states training on UMLS 2020AA, and the project rule forbids
UMLS, SNOMED and MedDRA bound material.

Usage::

    python -m episteme.bench.term_model_bakeoff --models medcpt,bge_base \\
        --acceptance tests/reference/data/question_terms_acceptance.json --out <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from episteme.reference.medcpt import TERM_MODELS, MedCPTEncoder, TermModelSpec

KS = (1, 5, 10)

Row = tuple[str, str, str, str]


def document_inputs(spec: TermModelSpec, rows: list[Row]) -> list[Any]:
    """Document side inputs. A symmetric model gets one string; MedCPT gets (title, text)."""
    if spec.query_model == spec.doc_model:
        return [f"{title}. {text}" if text else title for _ui, _kind, title, text in rows]
    return [(title, text) for _ui, _kind, title, text in rows]


def _encode_documents(spec: TermModelSpec, encoder: Any, rows: list[Row]) -> np.ndarray:
    inputs = document_inputs(spec, rows)
    if spec.query_model == spec.doc_model:
        return encoder.encode_texts(inputs)
    return encoder.encode_articles(inputs)


def _default_encoders(spec: TermModelSpec) -> tuple[Any, Any]:
    return MedCPTEncoder.query(spec), MedCPTEncoder.article(spec)


def _load_or_build_cache(
    spec: TermModelSpec,
    out: Path,
    vocabulary: Callable[[], list[Row]],
    doc_encoder_factory: Callable[[], Any],
) -> tuple[np.ndarray, list[str], float]:
    folder = out / spec.name
    npy = folder / f"{spec.doc_revision}.npy"
    ids_path = folder / f"{spec.doc_revision}.ids.json"
    meta = folder / f"{spec.doc_revision}.meta.json"
    if npy.exists() and ids_path.exists():
        vecs = np.load(npy)
        ids = json.loads(ids_path.read_text(encoding="utf-8"))
        if len(ids) == len(vecs):
            seconds = 0.0
            if meta.exists():
                seconds = float(json.loads(meta.read_text(encoding="utf-8"))["embed_seconds"])
            return vecs, ids, seconds
    rows = vocabulary()
    encoder = doc_encoder_factory()
    start = time.perf_counter()
    vecs = np.asarray(_encode_documents(spec, encoder, rows), dtype=np.float32)
    seconds = time.perf_counter() - start
    if len(vecs) != len(rows):
        raise RuntimeError(f"encoder returned {len(vecs)} vectors for {len(rows)} texts")
    ids = [r[0] for r in rows]
    folder.mkdir(parents=True, exist_ok=True)
    np.save(npy, vecs)
    ids_path.write_text(json.dumps(ids), encoding="utf-8")
    meta.write_text(json.dumps({"embed_seconds": seconds}), encoding="utf-8")
    return vecs, ids, seconds


def _rank_descriptors(scores: np.ndarray, inverse: np.ndarray, n_desc: int) -> np.ndarray:
    """Descriptor indices, best first; a descriptor scores as its best vector."""
    best = np.full(n_desc, -np.inf, dtype=np.float64)
    np.maximum.at(best, inverse, scores.astype(np.float64))
    return np.argsort(-best, kind="stable")


def _rates(flags: list[dict[str, bool]]) -> dict[str, float]:
    n = len(flags)
    return {
        "all": sum(f["all"] for f in flags) / n if n else 0.0,
        "any": sum(f["any"] for f in flags) / n if n else 0.0,
    }


def _summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(results)}
    for k in KS:
        out[f"top{k}"] = _rates([r["hits"][k] for r in results])
    return out


def _evaluate(
    acceptance: list[dict[str, Any]],
    vecs: np.ndarray,
    ids: list[str],
    query_encoder: Any,
) -> tuple[list[dict[str, Any]], list[float]]:
    uis, inverse = np.unique(np.asarray(ids), return_inverse=True)
    ui_list = [str(u) for u in uis]
    results: list[dict[str, Any]] = []
    times_ms: list[float] = []
    for entry in acceptance:
        start = time.perf_counter()
        q = np.asarray(query_encoder.encode_queries([entry["question"]]), dtype=np.float32)[0]
        order = _rank_descriptors(vecs @ q, inverse, len(ui_list))
        times_ms.append((time.perf_counter() - start) * 1000.0)
        ranked = [ui_list[i] for i in order]
        expected = set(entry["expected"])
        hits = {}
        for k in KS:
            top = set(ranked[:k])
            hits[k] = {"all": expected <= top, "any": bool(expected & top)}
        results.append({"kind": entry["kind"], "hits": hits})
    return results, times_ms


def run(
    models: list[str],
    acceptance: list[dict[str, Any]],
    out: Path,
    *,
    vocabulary: Callable[[], list[Row]],
    encoders: Callable[[TermModelSpec], tuple[Any, Any]] = _default_encoders,
) -> dict[str, Any]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"acceptance_n": len(acceptance), "models": {}}
    for name in models:
        spec = TERM_MODELS[name]
        pair: list[Any] = []

        def get_pair(spec: TermModelSpec = spec, pair: list[Any] = pair) -> tuple[Any, Any]:
            if not pair:
                pair.extend(encoders(spec))
            return pair[0], pair[1]

        vecs, ids, seconds = _load_or_build_cache(
            spec, out, vocabulary, lambda get_pair=get_pair: get_pair()[1]
        )
        results, times_ms = _evaluate(acceptance, vecs, ids, get_pair()[0])
        by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in results:
            by_kind[r["kind"]].append(r)
        report["models"][name] = {
            "query_model": f"{spec.query_model}@{spec.query_revision}",
            "doc_model": f"{spec.doc_model}@{spec.doc_revision}",
            "licence": spec.licence,
            "overall": _summarise(results),
            "by_kind": {k: _summarise(by_kind[k]) for k in sorted(by_kind)},
            "query_ms_mean": float(np.mean(times_ms)) if times_ms else 0.0,
            "query_ms_p95": float(np.percentile(times_ms, 95)) if times_ms else 0.0,
            "vocab_embed_seconds": seconds,
            "vocab_vectors": int(len(vecs)),
            "vocab_bytes": int(vecs.nbytes),
        }
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "REPORT.md").write_text(_markdown(report), encoding="utf-8")
    return report


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _cells(summary: dict[str, Any]) -> str:
    return " | ".join(
        f"{_pct(summary[f'top{k}']['all'])} / {_pct(summary[f'top{k}']['any'])}" for k in KS
    )


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Term-model bake-off",
        "",
        f"Acceptance questions: {report['acceptance_n']}. Search is exact.",
        "Cells are all / any. All: every expected UI is in the top k.",
        "Any: at least one expected UI is in the top k.",
        "",
        "SapBERT is not compared. Its model card states training on UMLS 2020AA.",
        "The project rule forbids UMLS, SNOMED and MedDRA bound material.",
        "",
        "| model | licence | top-1 | top-5 | top-10 | query ms mean | query ms p95 "
        "| vocab seconds | vocab vectors | vocab MB |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, m in report["models"].items():
        lines.append(
            f"| {name} | {m['licence']} | {_cells(m['overall'])} | {m['query_ms_mean']:.1f} | "
            f"{m['query_ms_p95']:.1f} | {m['vocab_embed_seconds']:.1f} | {m['vocab_vectors']} | "
            f"{m['vocab_bytes'] / 1e6:.1f} |"
        )
    for name, m in report["models"].items():
        lines += [
            "",
            f"## {name} by question kind",
            "",
            "| kind | n | top-1 | top-5 | top-10 |",
            "|---|---|---|---|---|",
        ]
        for kind, s in m["by_kind"].items():
            lines.append(f"| {kind} | {s['n']} | {_cells(s)} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m episteme.bench.term_model_bakeoff")
    parser.add_argument("--models", required=True, help="comma separated model names")
    parser.add_argument("--acceptance", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    names = [n.strip() for n in args.models.split(",") if n.strip()]
    unknown = [n for n in names if n not in TERM_MODELS]
    if unknown:
        print(
            f"unknown model(s): {', '.join(unknown)}; known: {', '.join(sorted(TERM_MODELS))}",
            file=sys.stderr,
        )
        return 2
    acceptance = json.loads(Path(args.acceptance).read_text(encoding="utf-8"))

    def vocabulary() -> list[Row]:
        from episteme.data.db.connection import connection
        from episteme.data.mesh.embed_mesh import vocabulary_texts

        with connection() as conn:
            return vocabulary_texts(conn)

    report = run(names, acceptance, Path(args.out), vocabulary=vocabulary)
    print(f"wrote {Path(args.out) / 'REPORT.md'} ({len(report['models'])} model(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
