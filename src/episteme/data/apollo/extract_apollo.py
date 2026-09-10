#!/usr/bin/env python3
"""Extraction: apollo — parse one ``ApolloCorpus`` file (multilingual medical
text; JSON array / JSONL / ``dict`` with a ``data``/``records``/``items`` list)
into the unified ``episteme.articles`` row schema (contract v1.4).

Unit of work: one input file. Per file: one ``article_schema`` row **per
document**, then ``staging_writer.write_rows``, then
``checkpoint_markers.mark_success`` / ``mark_failed``, then a best-effort chained
``audit_trail`` row.

Spec §3.2 apollo row: ``text`` = the document body (never a title+body rebuild —
``row["text"]`` is set explicitly so ``finalize_row`` does not re-run
``build_text``); ``source_file`` = input file name; ``language`` from the
per-doc lang tag; ``title`` synthesised from the first line of the body, or
``None``; ``license`` per Apollo's HF card → ``subset`` via
``subset_from_license``. ``pmid`` / ``pmcid`` / ``doi`` / ``mesh`` /
``container_id`` / ``book_meta`` are ``None`` on every row.

Importable core: ``extract_apollo(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory (honouring ``--max-files``) and prints a
field-shape table without writing any shard, marker, manifest or audit row.

Roadmap CLI:
  python -m episteme.data.apollo.extract_apollo \\
    --raw-dir ./01_raw/apollo \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
    --workers 4 \\
    [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.audit_trail import record as _audit  # noqa: E402
from episteme.config import get_settings  # noqa: E402
from episteme.data.article_schema import (  # noqa: E402
    ARTICLE_COLUMNS,
    SCHEMA_VERSION,
    finalize_row,
    normalize_license,
    normalize_whitespace,
    subset_from_license,
    utc_now_iso,
)
from episteme.data.checkpoint_markers import (  # noqa: E402
    is_success,
    list_input_files,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.staging_writer import write_rows  # noqa: E402

SOURCE = "apollo"

# ApolloCorpus HF dataset card (FreedomIntelligence/ApolloCorpus) declares
# license: apache-2.0. Per the Task 6 field-shape sign-off (user ruling,
# 2026-09-10), article_schema.normalize_license gained a permissive-OSI arm:
# "apache-2.0" -> license="permissive" -> subset="commercial", with the raw
# string carried in license_raw as evidence. ApolloCorpus is therefore
# commercial-shard eligible.
APOLLO_LICENSE_RAW = "apache-2.0"

# --------------------------------------------------------------------------- #
# SP2 apollo drop-heuristic — tuned at field-shape sign-off (spec §7 #2).
# One obvious knob. The heuristic runs ONLY on a non-empty body; a genuinely
# empty body falls through to decide_extract_status -> "empty".
APOLLO_MIN_CHARS = 200  # below this -> dropped (boilerplate/stub)
APOLLO_MEDICAL_GATE = False  # when True, also drop docs with no medical-signal token
_MEDICAL_SIGNALS = (
    "patient",
    "clinical",
    "disease",
    "treatment",
    "diagnos",
    "therap",
    "symptom",
    "dose",
    "mg/",
    "syndrome",
)  # lowercased substring hits
# --------------------------------------------------------------------------- #

# Synthesised-title cap (spec §3.2: "first sentence / first ~120 chars").
APOLLO_TITLE_MAXLEN = 160

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). extract() itself stays parallel — only this block serializes.
# Mirrors extract_pubmed.py.
_AUDIT_LOCK = threading.Lock()

# Non-document files that land in an apollo raw dir: the sync stamps written by
# download_apollo.sh (last_sync_utc.txt / hf_repo_id.txt are *.txt, listed for
# clarity) and HF repo metadata (dataset_infos.json, per-config config.json,
# README / LICENSE). Matched as a lowercased substring of the file name.
_SKIP_NAME_SUBSTRINGS = (
    "readme",
    "license",
    "dataset_infos",
    "dataset_info",
    "gitattributes",
    "config.json",
    "hf_repo_id",
    "last_sync",
)

_TEXT_KEYS = ("text", "content", "body", "article", "output", "response", "message")
_LANG_KEYS = ("lang", "language", "locale", "lang_code")
_ID_KEYS = ("id", "uid", "doc_id", "uuid", "sample_id")
_LIST_KEYS = ("data", "records", "items", "texts", "conversations", "samples")


# --------------------------------------------------------------------------- #
# document-shape detection (lifted from the retired apollo/extract.py)
# --------------------------------------------------------------------------- #
def _load_records(path: Path) -> list[Any]:
    """One apollo file -> a list of raw records (str, dict or nested list).

    Handles: a JSON array (of strings, objects, or nested QA lists — the real
    ApolloCorpus pretrain/sft shape); a JSON ``dict`` carrying a
    ``data``/``records``/``items``/... list (else the dict itself is one
    record); otherwise line-delimited JSON (JSONL), with any non-JSON line kept
    as a raw string.
    """
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return []
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        obj = None
    if obj is not None:
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for k in _LIST_KEYS:
                if isinstance(obj.get(k), list):
                    return obj[k]
            return [obj]
        return [obj]
    out: list[Any] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append(line)
    return out


def _extract_text(rec: Any) -> str:
    """Best-effort document body out of a raw record.

    A plain string is the body. A list (nested QA pairs / dialogue turns) is
    flattened and newline-joined. A dict is probed for a text-bearing key; a
    nested dict recurses; a list value of turns is joined. Falls back to a
    ``conversations`` list if present.
    """
    if isinstance(rec, str):
        return rec
    if isinstance(rec, list):
        # ApolloCorpus pretrain/sft shards are nested lists of strings
        # ([[q, a], ...] or [[[prompt, answer]], ...]); flatten to text.
        parts = [_extract_text(x) for x in rec]
        return "\n".join(p for p in parts if p and p.strip())
    if not isinstance(rec, dict):
        return str(rec)
    for k in _TEXT_KEYS:
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            inner = _extract_text(v)
            if inner:
                return inner
        if isinstance(v, list):
            parts: list[str] = []
            for turn in v:
                if isinstance(turn, str):
                    parts.append(turn)
                elif isinstance(turn, dict):
                    parts.append(
                        turn.get("value")
                        or turn.get("content")
                        or turn.get("text")
                        or turn.get("utterance")
                        or ""
                    )
            joined = "\n".join(p for p in parts if p)
            if joined.strip():
                return joined
    if isinstance(rec.get("conversations"), list):
        return _extract_text({"text": rec["conversations"]})
    return ""


def _extract_lang(rec: Any, source_file: str) -> str | None:
    if isinstance(rec, dict):
        for k in _LANG_KEYS:
            if rec.get(k):
                return str(rec[k])
    name = source_file.lower()
    for code in ("en", "zh", "es", "fr", "de", "ja", "ko", "pt", "it", "ru", "ar", "hi"):
        if f"_{code}_" in name or name.startswith(f"{code}_") or f".{code}." in name:
            return code
    return None


def _extract_id(rec: Any, body: str, source_file: str, idx: int) -> str:
    if isinstance(rec, dict):
        for k in _ID_KEYS:
            if rec.get(k) is not None:
                return f"apollo:{rec[k]}"
    # non-security fingerprint: a stable synthetic doc id when the record has none
    h = hashlib.sha1(
        f"{source_file}:{idx}:{body[:200]}".encode(), usedforsecurity=False
    ).hexdigest()[:16]
    return f"apollo:{h}"


def _synth_title(body: str) -> str | None:
    """Spec §3.2: title synthesised from the first line of the body (first
    sentence, else first ~120 chars up to a newline), or ``None`` if the body
    has no usable first line."""
    line = (body or "").strip().split("\n", 1)[0].strip()
    if not line:
        return None
    m = re.match(r".{1,%d}?[.!?](?:\s|$)" % APOLLO_TITLE_MAXLEN, line)
    if m:
        return m.group(0).strip()
    if len(line) <= APOLLO_TITLE_MAXLEN:
        return line
    return line[:APOLLO_TITLE_MAXLEN].rsplit(" ", 1)[0].strip() or None


def _drop_reason(body_stripped: str) -> str | None:
    """Non-``None`` -> this doc is ``dropped``. Runs only on a non-empty body."""
    if len(body_stripped) < APOLLO_MIN_CHARS:
        return f"short_text(len={len(body_stripped)}<{APOLLO_MIN_CHARS})"
    if APOLLO_MEDICAL_GATE:
        low = body_stripped.lower()
        if not any(sig in low for sig in _MEDICAL_SIGNALS):
            return "no_medical_signal"
    return None


def record_to_row(rec: Any, source_file: str, idx: int) -> dict[str, Any]:
    """One raw ApolloCorpus record -> a finalized ``episteme.articles`` row."""
    body = normalize_whitespace(_extract_text(rec))
    lang = _extract_lang(rec, source_file)
    title = _synth_title(body)
    lic, lic_url, lic_raw = normalize_license(APOLLO_LICENSE_RAW)
    subset = subset_from_license(lic)

    row: dict[str, Any] = {
        "id": _extract_id(rec, body, source_file, idx),
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": str(idx),
        "pmid": None,
        "pmcid": None,
        "doi": None,
        "title": title,
        "abstract": None,
        "body_text": body or None,
        # text = the document body, verbatim (spec §3.2). Set explicitly so
        # finalize_row does not rebuild it as title + body.
        "text": body or None,
        "authors": None,
        "journal": None,
        "year": None,
        "mesh": None,
        "publication_types": None,
        "language": lang,
        "license": lic,
        "license_url": lic_url,
        "license_raw": lic_raw,
        "subset": subset,
        "is_retracted": False,
        "pmc_version": None,
        "is_manuscript": None,
        "is_historical_ocr": None,
        "pdf_url": None,
        "container_id": None,
        "book_meta": None,
    }

    stripped = body.strip()
    if stripped:
        reason = _drop_reason(stripped)
        if reason:
            row["extract_status"] = "dropped"
            row["extract_notes"] = f"drop_heuristic: {reason}"
    return finalize_row(row)


def iter_rows_from_file(path: Path) -> Iterator[dict[str, Any]]:
    """Stream one ``article_schema`` row per ApolloCorpus document in ``path``."""
    source_file = path.name
    for idx, rec in enumerate(_load_records(path)):
        yield record_to_row(rec, source_file, idx)


def discover_apollo_files(raw_dir: Path) -> list[Path]:
    files = list_input_files(raw_dir, ["*.jsonl", "*.json"])
    out = [f for f in files if not any(s in f.name.lower() for s in _SKIP_NAME_SUBSTRINGS)]
    out.sort(key=lambda p: (0 if p.name.endswith(".jsonl") else 1, p.name))
    return out


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror extract_pubmed.py: one chained ``extract_commit`` audit row on a
    fresh connection, degrading to the file-only mirror when no DB is reachable.
    Never raises — ``mark_success`` has already run for this file."""
    with _AUDIT_LOCK:
        try:
            from episteme.data.db.connection import connection as _pg_connection

            with _pg_connection() as _conn:
                _audit(
                    "extract_commit",
                    conn=_conn,
                    object=f"{SOURCE} {basename}",
                    rows_affected=n_rows,
                    reason=None,
                    run_id=get_settings().run_id,
                )
                _conn.commit()
        except Exception:  # noqa: BLE001 - best-effort: DB unavailable or audit failed
            try:
                from episteme import audit_trail as _audit_trail

                _audit_trail.mirror_only(
                    "extract_commit",
                    object=f"{SOURCE} {basename}",
                    rows_affected=n_rows,
                    note="db_unavailable_or_failed",
                    run_id=get_settings().run_id,
                )
            except Exception:  # noqa: BLE001 - last-resort fallback must never escape
                pass


def process_one(
    path: Path,
    *,
    processed_dir: Path,
    force: bool,
) -> dict[str, Any]:
    basename = path.name
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    status_counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    try:
        for row in iter_rows_from_file(path):
            status_counts[str(row.get("extract_status"))] += 1
            rows.append(row)

        write_info = write_rows(
            rows,
            processed_dir,
            source=SOURCE,
            source_file=basename,
            prefer_parquet=True,
        )
        elapsed = round(time.time() - t0, 4)
        stats = {
            "n_rows": len(rows),
            "extract_status_counts": dict(status_counts),
            "elapsed_sec": elapsed,
            "write": write_info,
        }
        mark_success(processed_dir, SOURCE, basename, stats=stats)
        _best_effort_audit(basename, len(rows))
        return {"source_file": basename, "skipped": False, "ok": True, **stats}
    except Exception as e:  # noqa: BLE001
        cls = type(e).__name__
        mark_failed(
            processed_dir,
            SOURCE,
            basename,
            error_class="parse_error" if ("JSON" in cls or "Decode" in cls) else "unknown",
            message=str(e),
            stats={"n_rows_partial": len(rows)},
            exc=e,
        )
        return {"source_file": basename, "skipped": False, "ok": False, "error": str(e)}


def _print_verbose(result: dict[str, Any]) -> None:
    basename = result.get("source_file")
    if result.get("skipped"):
        print(f"skip {basename}", file=sys.stderr)
    elif result.get("ok"):
        print(
            f"ok {basename} rows={result.get('n_rows')} "
            f"status={result.get('extract_status_counts')}",
            file=sys.stderr,
        )
    else:
        print(f"FAIL {basename}: {result.get('error')}", file=sys.stderr)


def extract_apollo(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse ApolloCorpus ``*.json(l)`` under ``raw_dir`` into
    staging shards + ops markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_apollo_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]

    workers = max(1, int(workers))
    results: list[dict[str, Any]] = []
    t0 = time.time()

    if workers == 1:
        for fp in files:
            result = process_one(fp, processed_dir=processed_dir, force=force)
            results.append(result)
            if verbose:
                _print_verbose(result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(process_one, fp, processed_dir=processed_dir, force=force): fp
                for fp in files
            }
            for fut in as_completed(futs):
                result = fut.result()
                results.append(result)
                if verbose:
                    _print_verbose(result)

    ok = sum(1 for r in results if r.get("ok"))
    failed = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))
    rows = sum(int(r.get("n_rows") or 0) for r in results if r.get("ok"))
    summary = {"inputs": len(files), "ok": ok, "failed": failed, "rows": rows}

    run_id = get_settings().run_id or utc_now_iso().replace(":", "").replace("-", "")
    try:
        write_run_manifest(
            processed_dir,
            SOURCE,
            run_id,
            config={
                "raw_dir": str(raw_dir),
                "max_files": max_files,
                "workers": workers,
                "force": force,
            },
            totals={
                **summary,
                "skipped": sum(1 for r in results if r.get("skipped")),
                "elapsed_sec": round(time.time() - t0, 3),
            },
        )
    except Exception:  # noqa: BLE001 — manifest is best-effort, never fail the run on it
        pass

    return summary


# --------------------------------------------------------------------------- #
# --report: parse-only field-shape table (no shard / marker / manifest / audit)
# --------------------------------------------------------------------------- #

_SAMPLE_MAX = 4
_SAMPLE_TRUNC = 60


def _stringify(value: object) -> str:
    text = str(value)
    if len(text) > _SAMPLE_TRUNC:
        text = text[: _SAMPLE_TRUNC - 3] + "..."
    return text.replace("\n", " ")


def _render_field_shape(rows: list[dict[str, Any]], n_files: int) -> str:
    n = len(rows)
    lines = [
        f"field-shape report - source={SOURCE} schema={SCHEMA_VERSION}",
        f"files={n_files}  rows={n}",
        "",
    ]
    col_w = max((len(c) for c in ARTICLE_COLUMNS), default=6)
    header = f"{'column':<{col_w}}  {'non-null%':>9}  {'distinct':>8}  samples"
    lines.append(header)
    lines.append("-" * len(header))
    for col in ARTICLE_COLUMNS:
        vals = [r.get(col) for r in rows]
        non_null = [v for v in vals if v is not None]
        pct = round(100.0 * len(non_null) / n, 1) if n else 0.0
        distinct = len({repr(v) for v in non_null})
        samples = "; ".join(_stringify(v) for v in non_null[:_SAMPLE_MAX])
        lines.append(f"{col:<{col_w}}  {pct:>9}  {distinct:>8}  {samples}")

    lines += ["", "extract_status histogram:"]
    hist = Counter(str(r.get("extract_status")) for r in rows)
    for status, cnt in sorted(hist.items()):
        lines.append(f"  {status:<10} {cnt}")

    lines += ["", "subset / license breakdown:"]
    sub = Counter(str(r.get("subset")) for r in rows)
    lic = Counter(str(r.get("license")) for r in rows)
    for k, c in sorted(sub.items()):
        lines.append(f"  subset={k:<16} {c}")
    for k, c in sorted(lic.items()):
        lines.append(f"  license={k:<16} {c}")
    raw = Counter(str(r.get("license_raw")) for r in rows)
    for k, c in sorted(raw.items()):
        lines.append(f"  license_raw={k:<16} {c}")

    return "\n".join(lines) + "\n"


def _run_report(raw_dir: Path, max_files: int = 0) -> int:
    files = discover_apollo_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no apollo *.json(l) under {raw_dir}", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    for fp in files:
        rows.extend(iter_rows_from_file(fp))
    print(_render_field_shape(rows, len(files)), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(
        description="ApolloCorpus multilingual medical text to episteme.articles staging shards",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "apollo")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered apollo *.json(l) (env: EPISTEME_SAMPLE_LIMIT)",
    )
    p.add_argument("--workers", type=int, default=1, help="Parallel file workers")
    p.add_argument("--force", action="store_true")
    p.add_argument("--verbose", action="store_true", help="print ok/skip/FAIL per file to stderr")
    p.add_argument(
        "--report",
        action="store_true",
        help="parse only; print a field-shape table; write NO shard/marker/manifest/audit",
    )
    args = p.parse_args(argv)

    raw_dir: Path = args.raw_dir
    processed_dir: Path = args.processed_dir

    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    if args.report:
        return _run_report(raw_dir, args.max_files)

    print(f"schema={SCHEMA_VERSION} source={SOURCE}")
    print(f"raw_dir={raw_dir.resolve()} workers={max(1, args.workers)}")

    res = extract_apollo(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no apollo *.json(l) under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} " f"failed={res['failed']} rows={res['rows']}"
    )
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
