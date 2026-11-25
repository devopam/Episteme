"""Term-model bake-off: which embedding model finds the right MeSH descriptors.

Every acceptance question is embedded (the exact step ``match_terms`` is not part of
this comparison). Search is exact: a numpy inner product over the full vocabulary
cache, so the comparison measures the model and not an index. A descriptor scores as
the best of its vectors (descriptor row and entry rows), as ``suggest_terms`` groups
by descriptor.

Hit definitions, per top-k (k = 1, 5, 10):
- ``all``: every expected descriptor UI is in the top k.
- ``any``: at least one expected descriptor UI is in the top k.

Vocabulary vectors come from one of two places:

- ``mesh_term_vec``: when the stored model, revision and release equal the model under test
  and the stored rows cover every descriptor of ``mesh_term``, the stored vectors are read in
  chunks and nothing is embedded (``vocab_source`` = ``mesh_term_vec``).
- Embedding, in parts of ``--part-size`` rows. A part is saved at
  ``<out>/<model>/<mesh_release>/<revision>.part-<NNNN>.npy`` with ``.ids.json``. A restart
  skips the parts on disk. After the last part the parts are joined into
  ``<revision>.npy``, ``<revision>.ids.json`` and ``<revision>.meta.json`` (embedding time)
  and the part files are deleted. A second run reuses the joined cache. The release is in the
  path, so a new MeSH release never reuses old vectors.

Licence posture: SapBERT (cambridgeltl/SapBERT-from-PubMedBERT-fulltext) is excluded.
Its model card states training on UMLS 2020AA, and the project rule forbids
UMLS, SNOMED and MedDRA bound material.

Usage::

    python -m episteme.bench.term_model_bakeoff --models medcpt,bge_base \\
        --acceptance tests/reference/data/question_terms_acceptance.json --out <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from episteme.reference.medcpt import TERM_MODELS, MedCPTEncoder, TermModelSpec

KS = (1, 5, 10)
DEFAULT_PART_SIZE = 20000
CHUNK_ROWS = 4000

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


class StoredVectors(Protocol):
    """Source of the vectors already stored in ``episteme.mesh_term_vec``."""

    def check(self, spec: TermModelSpec, release: str) -> tuple[int, str | None]:
        """Return ``(row count, None)`` when the stored rows can be reused, else ``(0, reason)``."""
        ...

    def chunks(self, spec: TermModelSpec, release: str) -> Iterator[tuple[list[str], np.ndarray]]:
        """Yield ``(descriptor_ui list, float32 matrix)`` in a stable order."""
        ...


class DbStoredVectors:
    """``StoredVectors`` on the database; the connection comes from ``connection()``."""

    def __init__(self, chunk_rows: int = CHUNK_ROWS) -> None:
        self.chunk_rows = chunk_rows

    def check(self, spec: TermModelSpec, release: str) -> tuple[int, str | None]:
        from episteme.data.db.connection import connection

        # Row definition of vocabulary_texts: one row per descriptor with a name row, plus
        # one row per distinct (descriptor, entry term) of those descriptors.
        names = (
            "SELECT DISTINCT descriptor_ui FROM episteme.mesh_term "
            "WHERE term_kind = 'name' AND mesh_release = %s"
        )
        vec_where = "mesh_release = %s AND model = %s AND model_revision = %s"
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"WITH n AS ({names}) SELECT "
                "(SELECT count(*) FROM n) + (SELECT count(*) FROM (SELECT DISTINCT "
                "descriptor_ui, term FROM episteme.mesh_term WHERE term_kind = 'entry' "
                "AND mesh_release = %s AND descriptor_ui IN (SELECT descriptor_ui FROM n)) e), "
                "(SELECT count(*) FROM n)",
                (release, release),
            )
            want_rows, want_desc = cur.fetchone()
            cur.execute(
                "SELECT model, model_revision, count(*), count(DISTINCT descriptor_ui) "
                "FROM episteme.mesh_term_vec WHERE mesh_release = %s GROUP BY 1, 2",
                (release,),
            )
            groups = cur.fetchall()
            params = (release, spec.doc_model, spec.doc_revision)
            cur.execute(
                f"WITH n AS ({names}) SELECT count(*) FROM n WHERE descriptor_ui NOT IN "
                f"(SELECT descriptor_ui FROM episteme.mesh_term_vec WHERE {vec_where})",
                (release, *params),
            )
            missing = cur.fetchone()[0]
            cur.execute(
                "SELECT count(DISTINCT descriptor_ui) FROM episteme.mesh_term_vec "
                f"WHERE {vec_where} AND descriptor_ui NOT IN ({names})",
                (*params, release),
            )
            extra = cur.fetchone()[0]
        return _check_stored(spec, want_rows, want_desc, groups, missing=missing, extra=extra)

    def chunks(self, spec: TermModelSpec, release: str) -> Iterator[tuple[list[str], np.ndarray]]:
        from episteme.data.db.connection import connection

        with connection() as conn, conn.transaction(), conn.cursor(name="bakeoff_vec") as cur:
            cur.execute(
                "SELECT descriptor_ui, embedding::real[] FROM episteme.mesh_term_vec "
                "WHERE mesh_release = %s AND model = %s AND model_revision = %s "
                "ORDER BY descriptor_ui, text_kind, text",
                (release, spec.doc_model, spec.doc_revision),
            )
            while True:
                batch = cur.fetchmany(self.chunk_rows)
                if not batch:
                    return
                yield (
                    [r[0] for r in batch],
                    np.asarray([r[1] for r in batch], dtype=np.float32),
                )


def _check_stored(
    spec: TermModelSpec,
    want_rows: int,
    want_desc: int,
    groups: list[tuple[Any, ...]],
    *,
    missing: int = 0,
    extra: int = 0,
) -> tuple[int, str | None]:
    """Decide from the stored ``(model, revision, rows, descriptors)`` groups.

    ``missing``: descriptors of ``mesh_term`` without a stored vector. ``extra``: stored
    descriptors that ``mesh_term`` does not list.
    """
    for model, revision, rows, _descriptors in groups:
        if model == spec.doc_model and revision == spec.doc_revision:
            if missing or extra:
                return 0, (
                    f"descriptor sets differ: {missing} of {want_desc} descriptors "
                    f"have no stored vector, {extra} stored descriptors are not in mesh_term"
                )
            if rows != want_rows:
                return 0, f"stored rows are {rows}; the vocabulary has {want_rows} rows"
            return int(rows), None
    stored = ", ".join(f"{m}@{r}" for m, r, _n, _d in groups) or "no rows"
    return 0, f"stored vectors are {stored}; need {spec.doc_model}@{spec.doc_revision}"


def _load_stored(
    stored: StoredVectors, spec: TermModelSpec, release: str, rows: int
) -> tuple[np.ndarray, list[str]]:
    """Fill one float32 matrix from the stored chunks."""
    matrix: np.ndarray | None = None
    ids: list[str] = []
    for chunk_ids, chunk in stored.chunks(spec, release):
        if matrix is None:
            matrix = np.empty((rows, chunk.shape[1]), dtype=np.float32)
        end = len(ids) + len(chunk_ids)
        if end > rows:
            raise RuntimeError(f"stored rows exceed the {rows} counted rows")
        matrix[len(ids) : end] = chunk
        ids.extend(chunk_ids)
    if matrix is None or len(ids) != rows:
        raise RuntimeError(f"read {len(ids)} stored vectors, expected {rows}")
    return matrix, ids


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _save_npy(path: Path, array: np.ndarray) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        np.save(handle, array)
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _part_paths(folder: Path, revision: str, index: int) -> tuple[Path, Path]:
    return (
        folder / f"{revision}.part-{index:04d}.npy",
        folder / f"{revision}.part-{index:04d}.ids.json",
    )


def _clear_parts(folder: Path, revision: str) -> None:
    for path in folder.glob(f"{revision}.part-*"):
        path.unlink()
    (folder / f"{revision}.plan.json").unlink(missing_ok=True)
    (folder / f"{revision}.plan.json.tmp").unlink(missing_ok=True)


def _vocab_hash(rows: list[Row]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update("|".join(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _part_dim(npy_path: Path, want_rows: int) -> int | None:
    """Column count of a whole float32 part with ``want_rows`` rows, else None."""
    try:
        part = np.load(npy_path, mmap_mode="r")
        ok = part.ndim == 2 and part.shape[0] == want_rows and part.dtype == np.float32
        dim = int(part.shape[1]) if ok else None
        del part
    except (OSError, ValueError, EOFError):
        return None
    return dim


def _embed_in_parts(
    spec: TermModelSpec,
    folder: Path,
    rows: list[Row],
    part_size: int,
    doc_encoder_factory: Callable[[], Any],
) -> tuple[np.ndarray, list[str], float]:
    """Encode in parts, skip parts on disk, join them, delete the parts."""
    if part_size < 1:
        raise ValueError("part_size must be at least 1")
    revision = spec.doc_revision
    folder.mkdir(parents=True, exist_ok=True)
    plan_path = folder / f"{revision}.plan.json"
    plan = _read_json(plan_path)
    vocab_hash = _vocab_hash(rows)
    if (
        not plan
        or plan.get("rows") != len(rows)
        or plan.get("part_size") != part_size
        or plan.get("vocab_hash") != vocab_hash
    ):
        _clear_parts(folder, revision)
        plan = {
            "rows": len(rows),
            "part_size": part_size,
            "vocab_hash": vocab_hash,
            "seconds": 0.0,
        }
        _write_json(plan_path, plan)
    seconds = float(plan.get("seconds", 0.0))
    n_parts = math.ceil(len(rows) / part_size)
    encoder: Any = None
    dim: int | None = None
    for index in range(n_parts):
        chunk = rows[index * part_size : (index + 1) * part_size]
        want_ids = [r[0] for r in chunk]
        npy_path, ids_path = _part_paths(folder, revision, index)
        if _read_json(ids_path) == want_ids:
            found = _part_dim(npy_path, len(want_ids))
            if found is not None and (dim is None or found == dim):
                dim = found
                continue
        if encoder is None:
            encoder = doc_encoder_factory()
        start = time.perf_counter()
        vecs = np.asarray(_encode_documents(spec, encoder, chunk), dtype=np.float32)
        seconds += time.perf_counter() - start
        if len(vecs) != len(chunk):
            raise RuntimeError(f"encoder returned {len(vecs)} vectors for {len(chunk)} texts")
        dim = int(vecs.shape[1])
        plan["seconds"] = seconds
        _write_json(plan_path, plan)
        _save_npy(npy_path, vecs)
        _write_json(ids_path, want_ids)
        del vecs
    matrix: np.ndarray | None = None
    offset = 0
    for index in range(n_parts):
        npy_path, _ids_path = _part_paths(folder, revision, index)
        part = np.load(npy_path)
        if matrix is None:
            matrix = np.empty((len(rows), part.shape[1]), dtype=np.float32)
        matrix[offset : offset + len(part)] = part
        offset += len(part)
        del part
    if matrix is None:
        matrix = np.empty((0, 0), dtype=np.float32)
    ids = [r[0] for r in rows]
    _save_npy(folder / f"{revision}.npy", matrix)
    _write_json(folder / f"{revision}.ids.json", ids)
    _write_json(folder / f"{revision}.meta.json", {"embed_seconds": seconds})
    _clear_parts(folder, revision)
    return matrix, ids, seconds


def _load_or_build_cache(
    spec: TermModelSpec,
    out: Path,
    mesh_release: str,
    vocabulary: Callable[[], list[Row]],
    doc_encoder_factory: Callable[[], Any],
    part_size: int = DEFAULT_PART_SIZE,
) -> tuple[np.ndarray, list[str], float | None]:
    folder = out / spec.name / mesh_release
    npy = folder / f"{spec.doc_revision}.npy"
    ids_path = folder / f"{spec.doc_revision}.ids.json"
    meta = folder / f"{spec.doc_revision}.meta.json"
    if npy.exists() and ids_path.exists():
        vecs = np.load(npy)
        ids = json.loads(ids_path.read_text(encoding="utf-8"))
        if len(ids) == len(vecs):
            seconds: float | None = None
            data = _read_json(meta) if meta.exists() else None
            if isinstance(data, dict) and "embed_seconds" in data:
                seconds = float(data["embed_seconds"])
            return vecs, ids, seconds
    return _embed_in_parts(spec, folder, vocabulary(), part_size, doc_encoder_factory)


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
    if acceptance:
        # One encode that is not timed: the first call pays one-time start-up cost.
        query_encoder.encode_queries([acceptance[0]["question"]])
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
    mesh_release: str,
    encoders: Callable[[TermModelSpec], tuple[Any, Any]] = _default_encoders,
    stored: StoredVectors | None = None,
    part_size: int = DEFAULT_PART_SIZE,
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

        vecs: np.ndarray | None = None
        ids: list[str] = []
        seconds: float | None = None
        reason: str | None = None
        source = "embedded"
        if stored is not None:
            rows, reason = stored.check(spec, mesh_release)
            if reason is None:
                vecs, ids = _load_stored(stored, spec, mesh_release, rows)
                source = "mesh_term_vec"
        if vecs is None:
            vecs, ids, seconds = _load_or_build_cache(
                spec,
                out,
                mesh_release,
                vocabulary,
                lambda get_pair=get_pair: get_pair()[1],
                part_size,
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
            "vocab_source": source,
            "reuse_skipped_reason": reason,
            "vocab_embed_seconds": seconds,
            "vocab_vectors": int(len(vecs)),
            "vocab_bytes": int(vecs.nbytes),
        }
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "REPORT.md").write_text(_markdown(report), encoding="utf-8")
    return report


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


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
        "Vectors read from mesh_term_vec are fp16 (halfvec); embedded vectors are fp32.",
        "Check the vocab_source of each model before you compare small differences.",
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
            f"{m['query_ms_p95']:.1f} | {_seconds(m['vocab_embed_seconds'])} | "
            f"{m['vocab_vectors']} | "
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
    parser.add_argument(
        "--part-size",
        type=int,
        default=DEFAULT_PART_SIZE,
        help="rows encoded and saved per part (default %(default)s)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    names = [n.strip() for n in args.models.split(",") if n.strip()]
    unknown = [n for n in names if n not in TERM_MODELS]
    if unknown:
        print(
            f"unknown model(s): {', '.join(unknown)}; known: {', '.join(sorted(TERM_MODELS))}",
            file=sys.stderr,
        )
        return 2
    if args.part_size < 1:
        print("--part-size must be at least 1", file=sys.stderr)
        return 2
    acceptance = json.loads(Path(args.acceptance).read_text(encoding="utf-8"))

    def vocabulary() -> list[Row]:
        from episteme.data.db.connection import connection
        from episteme.data.mesh.embed_mesh import vocabulary_texts

        with connection() as conn:
            return vocabulary_texts(conn)

    from episteme.data.db.connection import connection

    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT mesh_release FROM episteme.mesh_term")
        releases = [r[0] for r in cur.fetchall()]
    if len(releases) != 1:
        print(f"mesh_term must hold one release, found {releases}", file=sys.stderr)
        return 2
    report = run(
        names,
        acceptance,
        Path(args.out),
        vocabulary=vocabulary,
        mesh_release=releases[0],
        stored=DbStoredVectors(),
        part_size=args.part_size,
    )
    print(f"wrote {Path(args.out) / 'REPORT.md'} ({len(report['models'])} model(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
