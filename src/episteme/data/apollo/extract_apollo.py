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
import logging
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
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
    discover_input_files,
    input_key,
    is_success,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.parallel import (  # noqa: E402
    default_workers,
    record_first_failure,
    run_per_file,
    skip_files,
)
from episteme.data.staging_writer import write_rows  # noqa: E402
from episteme.ops.retry import TransientUnitError, is_transient  # noqa: E402
from episteme.ops.skip import add_skip_args, check_skip_args  # noqa: E402

_LOG = logging.getLogger(__name__)

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

# Guards the in-process (workers=1) path only. Across worker processes, the mirror
# append lock in audit_trail and the DB advisory lock serialise audit writes.
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

# Benchmark and decontamination files of FreedomIntelligence/ApolloCorpus (its README
# config uses only train/sft/medicalExam_<lang>_clean.json for the exam split):
#   check_exam/                    the Apollo benchmark questions and scorers
#   medicalExam_<lang>.json        the exam set before decontamination (a superset
#                                  of the _clean file)
#   medicalExam_<lang>_dup*.json   the items removed for overlapping the benchmark
# None of them may reach the corpus: they would put test questions into training.
_BENCHMARK_DIRS = ("check_exam",)
_BENCHMARK_NAME = re.compile(r"^medicalExam_[a-z]{2}(_dup|_dup_question)?\.json$")

_TEXT_KEYS = ("text", "content", "body", "article", "output", "response", "message")
_LANG_KEYS = ("lang", "language", "locale", "lang_code")
_ID_KEYS = ("id", "uid", "doc_id", "uuid", "sample_id")
_LIST_KEYS = ("data", "records", "items", "texts", "conversations", "samples")


# --------------------------------------------------------------------------- #
# document-shape detection (lifted from the retired apollo/extract.py)
# --------------------------------------------------------------------------- #
_STREAM_CHUNK = 1 << 22  # 4 MiB of text per read


_WS = " \t\r\n"


def _iter_json_array(fh: Any, chunk: int = _STREAM_CHUNK) -> Iterator[Any]:
    """Yield the elements of the JSON array that ``fh`` (a text file positioned
    just after its opening ``[``) holds, one at a time. Memory stays at about one
    element plus one chunk, whatever the file size: the 2 GiB ApolloCorpus files
    took 5 GiB and more per worker when read whole (2026-10-18).

    Strict, like ``json.loads``: elements are separated by one ``,``, the array
    ends with ``]``, and only whitespace may follow it. A missing separator, a
    trailing comma, a missing ``]`` or trailing content raises
    ``json.JSONDecodeError``, so a damaged file fails instead of yielding part of
    its records."""
    dec = json.JSONDecoder()
    buf = ""
    pos = 0
    eof = False

    def more() -> bool:
        nonlocal buf, pos, eof
        if eof:
            return False
        data = fh.read(chunk)
        if not data:
            eof = True
            return False
        buf = buf[pos:] + data
        pos = 0
        return True

    def next_char() -> str:
        """Skip whitespace; the next character ('' at the end of the file)."""
        nonlocal pos
        while True:
            while pos < len(buf) and buf[pos] in _WS:
                pos += 1
            if pos < len(buf):
                return buf[pos]
            if not more():
                return ""

    def fail(msg: str) -> json.JSONDecodeError:
        return json.JSONDecodeError(msg, buf, pos)

    first = True
    while True:
        c = next_char()
        if c == "]" and first:
            pos += 1
            break
        if c == "":
            raise fail("unterminated array")
        while True:
            try:
                obj, end = dec.raw_decode(buf, pos)
            except json.JSONDecodeError:
                if more():
                    continue
                raise
            # A number cut by the chunk ("-0.5e" of "-0.5e-3") decodes without
            # error; an element must be followed by whitespace, "," or "]".
            if (end >= len(buf) or buf[end] not in _WS + ",]") and more():
                continue
            break
        yield obj
        pos = end
        first = False
        c = next_char()
        if c == ",":
            pos += 1
            if next_char() == "]":
                raise fail("trailing comma in array")
            continue
        if c == "]":
            pos += 1
            break
        raise fail("expected ',' or ']' after an array element")
    if next_char() != "":
        raise fail("content after the end of the array")


def _iter_jsonl(path: Path) -> Iterator[Any]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield line


def _iter_records(path: Path) -> Iterator[Any]:
    """One apollo file -> its raw records (str, dict or nested list), one at a time.

    Handles: a ``.jsonl`` file, read line by line (a line may itself be an
    array), with any non-JSON line kept as a raw string; a ``.json`` file whose
    top level is an array (the real ApolloCorpus pretrain/sft shape), streamed
    element by element; any other ``.json`` file, read whole (a ``dict`` carrying
    a ``data``/``records``/``items``/... list, else the dict itself is one
    record), falling back to line-by-line when it is not one JSON value.
    """
    if path.suffix.lower() == ".jsonl":
        yield from _iter_jsonl(path)
        return
    with path.open(encoding="utf-8", errors="replace") as fh:
        head = fh.read(1)
        while head and head.isspace():
            head = fh.read(1)
        if not head:
            return
        if head == "[":
            yield from _iter_json_array(fh)
            return
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        obj = None
    del raw
    if obj is None:
        yield from _iter_jsonl(path)
        return
    if isinstance(obj, dict):
        for k in _LIST_KEYS:
            if isinstance(obj.get(k), list):
                yield from obj[k]
                return
    yield obj


def _load_records(path: Path) -> list[Any]:
    """All records of one apollo file as a list (see _iter_records)."""
    return list(_iter_records(path))


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
    m = re.match(rf".{{1,{APOLLO_TITLE_MAXLEN}}}?[.!?](?:\s|$)", line)
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


def iter_rows_from_file(path: Path, *, source_file: str | None = None) -> Iterator[dict[str, Any]]:
    """Stream one ``article_schema`` row per ApolloCorpus document in ``path``.

    ``source_file`` defaults to ``path.name`` when omitted (existing
    positional callers keep working), but callers with a ``raw_dir`` in scope
    must pass ``input_key(path, raw_dir)`` explicitly -- two input files
    sharing a basename in different subdirectories under ``raw_dir`` must not
    collapse to the same ``source_file`` value on the rows themselves.
    """
    source_file = source_file or path.name
    for idx, rec in enumerate(_iter_records(path)):
        yield record_to_row(rec, source_file, idx)


def _is_benchmark_file(path: Path, raw_dir: Path) -> bool:
    """True for a benchmark or decontamination file (see _BENCHMARK_NAME)."""
    try:
        parts = path.relative_to(raw_dir).parts
    except ValueError:
        parts = path.parts
    return any(d in parts for d in _BENCHMARK_DIRS) or bool(_BENCHMARK_NAME.match(path.name))


def discover_apollo_files(raw_dir: Path) -> list[Path]:
    """``*.jsonl``/``*.json`` ApolloCorpus files under ``raw_dir``, deduped by
    resolved full path (never by basename) via
    ``checkpoint_markers.discover_input_files``.

    ``download_apollo.sh`` builds no subpath of its own -- unlike
    pmc/bookshelf/pubmed's downloaders, it delegates entirely to
    ``hf download --local-dir <raw_dir>``, which mirrors the
    ``FreedomIntelligence/ApolloCorpus`` HF repo's own directory tree
    byte-for-byte. Whether that upstream tree is flat or nested by
    split/config/language is therefore outside this script's control, so
    ``list_input_files``'s basename-only dedup (the pre-migration behaviour)
    was never actually safe here either -- two same-basename files under
    different upstream subdirectories would have silently collapsed to one
    discovered file. Mirrors ``extract_pmc.discover_meta_files`` /
    ``extract_bookshelf.discover_bookshelf_files`` / ``extract_pubmed.
    discover_pubmed_files`` (Tasks 9/10/11).
    """
    files = discover_input_files(raw_dir, ["*.jsonl", "*.json"])
    out = [
        f
        for f in files
        if not any(s in f.name.lower() for s in _SKIP_NAME_SUBSTRINGS)
        and not _is_benchmark_file(f, raw_dir)
    ]
    out.sort(key=lambda p: (0 if p.name.endswith(".jsonl") else 1, input_key(p, raw_dir)))
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
                _LOG.warning(
                    "audit mirror_only fallback also failed for %s", basename, exc_info=True
                )


def process_one(
    path: Path,
    *,
    raw_dir: Path,
    processed_dir: Path,
    force: bool,
) -> dict[str, Any]:
    # input_key, not path.name: two apollo *.json(l) files sharing a basename
    # in different subdirectories under raw_dir (download_apollo.sh does not
    # control the layout -- it mirrors whatever tree the upstream HF repo
    # has, see discover_apollo_files) must not collapse to the same
    # marker/source_file identity.
    basename = input_key(path, raw_dir)
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    status_counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    try:
        for row in iter_rows_from_file(path, source_file=basename):
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
        if is_transient(e):
            raise TransientUnitError(basename, repr(e)) from None
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
    skip_units: list[str] | None = None,
    skip_reason: str | None = None,
) -> dict:
    """Importable core: parse ApolloCorpus ``*.json(l)`` under ``raw_dir`` into
    staging shards + ops markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_apollo_files(raw_dir)
    files = skip_files(SOURCE, "extract", files, raw_dir, skip_units, skip_reason)
    if max_files and max_files > 0:
        files = files[:max_files]

    workers = max(1, int(workers))
    t0 = time.time()

    results = run_per_file(
        process_one,
        files,
        workers=workers,
        on_result=_print_verbose if verbose else None,
        raw_dir=raw_dir,
        processed_dir=processed_dir,
        force=force,
    )
    record_first_failure(SOURCE, "extract", results)

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
        rows.extend(iter_rows_from_file(fp, source_file=input_key(fp, raw_dir)))
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
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="worker processes (default: the runtime profile; EPISTEME_EXTRACT_WORKERS overrides)",
    )
    p.add_argument("--force", action="store_true")
    p.add_argument("--verbose", action="store_true", help="print ok/skip/FAIL per file to stderr")
    p.add_argument(
        "--report",
        action="store_true",
        help="parse only; print a field-shape table; write NO shard/marker/manifest/audit",
    )
    add_skip_args(p)
    args = p.parse_args(argv)
    check_skip_args(p, args)
    if args.report and args.skip_unit:
        p.error("--skip-unit cannot be used with --report")
    workers = default_workers() if args.workers is None else max(1, args.workers)

    raw_dir: Path = args.raw_dir
    processed_dir: Path = args.processed_dir

    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    if args.report:
        return _run_report(raw_dir, args.max_files)

    print(f"schema={SCHEMA_VERSION} source={SOURCE}")
    print(f"raw_dir={raw_dir.resolve()} workers={workers}")

    res = extract_apollo(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=workers,
        verbose=args.verbose,
        skip_units=args.skip_unit,
        skip_reason=args.reason,
    )

    # inputs == 0 after --skip-unit dropped every file is a clean run (R8).
    if res["inputs"] == 0 and not args.skip_unit:
        print(f"ERROR: no apollo *.json(l) under {raw_dir}", file=sys.stderr)
        return 1

    print(f"done inputs={res['inputs']} ok={res['ok']} failed={res['failed']} rows={res['rows']}")
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
