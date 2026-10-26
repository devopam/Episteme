"""The sample gate: rules, evaluation, the stage code hash and the pass file
(operating standard spec 7).

A source is gated for a stage when ``gate_rules/<source>.json`` has a section for
that stage. A sample run measures its output (``gate_report``); ``evaluate``
checks the measures against the rules; a pass writes
``<processed root>/_ops/<source>/gate/<stage>.pass``. The pass holds a hash of
the stage's code and a hash of the rules file, so a change to either one makes
the pass invalid.

Measure keys that the rules read: ``rows``, ``rows_per_input``,
``text_bytes_per_input_byte``, ``duplicate_ids``, ``duplicate_ids_in_shard``,
``empty_text_pct``,
``extract_status_pct`` (dict), ``null_pct`` (dict), ``license_counts`` (dict),
``failed_inputs``, ``orphan_bodies``, ``rows_loaded_equals_staging``,
``peak_rss_mb``, and for ``parts_within_caps`` ``max_rows_per_source_file``,
``max_est_bytes_per_source_file`` (staging) with ``part_rows``, ``part_bytes``
(the profile's caps). A rule
whose measure is missing fails: a gate never passes on data it did not see.

Limits: the code hash covers the ``episteme`` modules that the stage imports and
the wrapper script. It does not cover third-party packages, data files or other
scripts that the wrapper calls. A module that the stage imports inside a function
is added to the closure by
an ``ast`` scan of ``from episteme...`` / ``import episteme...`` statements; a
dynamic import (``importlib``) is not found.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

RULES_DIR = Path(__file__).resolve().parent / "gate_rules"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src"
_SCRIPTS_DATA = _REPO_ROOT / "scripts" / "data"

_EXTRACT_MODULES = {
    "bookshelf": ("episteme.data.bookshelf.extract_bookshelf", "bookshelf/extract_bookshelf.sh"),
    "europepmc_manuscript": (
        "episteme.data.europepmc.manuscripts.extract_europepmc_manuscripts",
        "europepmc_manuscript/extract_europepmc_manuscript.sh",
    ),
    "apollo": ("episteme.data.apollo.extract_apollo", "apollo/extract_apollo.sh"),
}
LOAD_MODULE = "episteme.data.load_articles"


class _StageModules(dict):
    """(source, stage) -> (module, wrapper). Any source has a ``load`` entry."""

    def __missing__(self, key: tuple[str, str]) -> tuple[str, str]:
        source, stage = key
        if stage == "load":
            return (LOAD_MODULE, f"{source}/load_{source}.sh")
        raise KeyError(key)


STAGE_MODULES: dict[tuple[str, str], tuple[str, str]] = _StageModules(
    {
        **{(source, "extract"): value for source, value in _EXTRACT_MODULES.items()},
        ("pubchem", "serialize"): (
            "episteme.data.pubchem.serialize_pubchem",
            "pubchem/serialize_pubchem.sh",
        ),
    }
)


def stage_module(source: str, stage: str) -> tuple[str, str]:
    """(module, wrapper) of a stage; ``KeyError`` for an unknown pair. Use this,
    not ``in`` or ``.get`` on ``STAGE_MODULES``, which miss the load entries."""
    return STAGE_MODULES[(source, stage)]


_KNOWN_KEYS = {
    "min_rows",
    "min_rows_per_input",
    "min_text_bytes_per_input_byte",
    "max_duplicate_ids",
    "max_duplicate_ids_in_shard",
    "max_empty_text_pct",
    "max_status_pct",
    "max_null_pct",
    "allowed_licenses",
    "max_failed_inputs",
    "max_orphan_bodies",
    "rows_loaded_equals_staging",
    "max_peak_rss_mb",
    "parts_within_caps",
}


def _rules_path(source: str) -> Path:
    return RULES_DIR / f"{source}.json"


def _check_names(path: Path, source: str, rules: dict) -> None:
    """Refuse a name that would silently disable a rule: a stage the gate cannot
    hash (not in ``STAGE_MODULES`` for this source), a ``max_null_pct`` column
    that is not an article column, a ``max_status_pct`` status that the
    extractors never write, a stage section that is not an object."""
    from episteme.data import article_schema

    for stage, section in rules.items():
        try:
            stage_module(source, stage)
        except KeyError:
            raise ValueError(
                f"{path}: unknown stage '{stage}' for {source} (the gate has no code to hash)"
            ) from None
        if not isinstance(section, dict):
            raise ValueError(f"{path}: {stage}: the stage section must be an object")
        for rule, known in (
            ("max_null_pct", article_schema.ARTICLE_COLUMNS),
            ("max_status_pct", article_schema.EXTRACT_STATUSES),
        ):
            limits = section.get(rule)
            if not isinstance(limits, dict):
                continue
            bad = sorted(set(limits) - set(known))
            if bad:
                raise ValueError(f"{path}: {stage}.{rule}: unknown name(s): {', '.join(bad)}")


def load_rules(source: str) -> dict | None:
    """The parsed rules file, or None when the source has none. A file that does
    not parse, or that names an unknown stage, article column or extract status,
    raises ``ValueError``: a damaged rules file never means "no gate"."""
    path = _rules_path(source)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        rules = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(rules, dict):
        raise ValueError(f"{path}: the top level must be an object")
    _check_names(path, source, rules)
    return rules


def is_gated(source: str, stage: str) -> bool:
    rules = load_rules(source)
    return bool(rules) and isinstance(rules.get(stage), dict)


def _num(value: Any) -> str:
    # Exact (shortest round-trip repr): a failure line must show why the
    # value failed, e.g. 0.01999998 against 0.02, never two equal numbers.
    return str(value)


def _missing(rule: str) -> str:
    return f"{rule}: the measure is missing"


def _max_rule(measures: dict, rule: str, key: str, limit: Any) -> list[str]:
    value = measures.get(key)
    if value is None:
        return [_missing(rule)]
    if value > limit:
        return [f"{rule}: {key} is {_num(value)}, the limit is {_num(limit)}"]
    return []


def _min_rule(measures: dict, rule: str, key: str, limit: Any) -> list[str]:
    value = measures.get(key)
    if value is None:
        return [_missing(rule)]
    if value < limit:
        return [f"{rule}: {key} is {_num(value)}, the minimum is {_num(limit)}"]
    return []


def _per_key_max(measures: dict, rule: str, key: str, limits: dict) -> list[str]:
    seen = measures.get(key)
    if not isinstance(seen, dict):
        return [_missing(rule)]
    out = []
    for name, limit in limits.items():
        value = seen.get(name, 0.0)
        if value > limit:
            out.append(f"{rule}: {key}[{name}] is {_num(value)}, the limit is {_num(limit)}")
    return out


_SIMPLE = {
    "min_rows": (_min_rule, "rows"),
    "min_rows_per_input": (_min_rule, "rows_per_input"),
    "min_text_bytes_per_input_byte": (_min_rule, "text_bytes_per_input_byte"),
    "max_duplicate_ids": (_max_rule, "duplicate_ids"),
    "max_duplicate_ids_in_shard": (_max_rule, "duplicate_ids_in_shard"),
    "max_empty_text_pct": (_max_rule, "empty_text_pct"),
    "max_failed_inputs": (_max_rule, "failed_inputs"),
    "max_orphan_bodies": (_max_rule, "orphan_bodies"),
    "max_peak_rss_mb": (_max_rule, "peak_rss_mb"),
}
_PER_KEY = {
    "max_status_pct": "extract_status_pct",
    "max_null_pct": "null_pct",
}


def _parts_within_caps(measures: dict, rule: str) -> list[str]:
    """Every part is within the row and byte caps (measured on the staging
    shards by ``gate_report``). The byte measure covers parts with 2 or more
    rows only: a single row over the byte cap goes alone in its own part, the
    one case the serializer allows over ``part_bytes``."""
    fails = []
    for key, cap_key, what in (
        ("max_rows_per_source_file", "part_rows", "rows"),
        ("max_est_bytes_per_source_file", "part_bytes", "estimated bytes"),
    ):
        value, cap = measures.get(key), measures.get(cap_key)
        if value is None or cap is None:
            fails.append(_missing(rule))
        elif value > cap:
            fails.append(f"{rule}: a part has {_num(value)} {what}, over the cap of {_num(cap)}")
    return fails


def evaluate(measures: dict, stage_rules: dict) -> list[str]:
    """The failed rules as readable lines; an empty list is a pass. An unknown
    rule key raises ``ValueError`` so a typo never disables a rule."""
    unknown = sorted(set(stage_rules) - _KNOWN_KEYS)
    if unknown:
        raise ValueError(f"unknown gate rule key(s): {', '.join(unknown)}")
    fails: list[str] = []
    for rule, limit in stage_rules.items():
        if rule in _SIMPLE:
            fn, key = _SIMPLE[rule]
            fails += fn(measures, rule, key, limit)
        elif rule in _PER_KEY:
            fails += _per_key_max(measures, rule, _PER_KEY[rule], limit)
        elif rule == "allowed_licenses":
            seen = measures.get("license_counts")
            if not isinstance(seen, dict):
                fails.append(_missing(rule))
                continue
            bad = sorted(str(k) for k, n in seen.items() if n and k not in limit)
            if bad:
                fails.append(f"{rule}: licence(s) not allowed: {', '.join(bad)}")
        elif rule == "rows_loaded_equals_staging" and limit:
            value = measures.get("rows_loaded_equals_staging")
            if value is None:
                fails.append(_missing(rule))
            elif not value:
                fails.append(f"{rule}: the loaded rows differ from the staging rows")
        elif rule == "parts_within_caps" and limit:
            fails += _parts_within_caps(measures, rule)
    return fails


_HASH_SCRIPT = """
import json, os, sys
src_root, modules = sys.argv[1], sys.argv[2:]
sys.path.insert(0, src_root)
for module in modules:
    __import__(module)
prefix = os.path.join(os.path.realpath(src_root), modules[0].split(".")[0]) + os.sep
files = set()
for mod in list(sys.modules.values()):
    f = getattr(mod, "__file__", None)
    if f:
        f = os.path.realpath(f)
        if f.startswith(prefix):
            files.add(f)
print(json.dumps(sorted(files)))
"""


def _import_files(modules: list[str], src_root: Path) -> list[Path]:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _HASH_SCRIPT, str(src_root), *modules],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"could not import {modules[0]} to hash it: {proc.stderr.strip()[-500:]}"
        )
    lines = proc.stdout.strip().splitlines()
    if not lines:
        raise RuntimeError(
            f"importing {modules[0]} printed no module list: {proc.stderr.strip()[-500:]}"
        )
    return [Path(p) for p in json.loads(lines[-1])]


def _lazy_modules(path: Path, src_root: Path, top: str) -> set[str]:
    """Modules of package ``top`` that code inside functions in ``path`` imports
    (``from top.x import y`` gives ``top.x`` and, when it is a module, ``top.x.y``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module)
                names.update(f"{node.module}.{a.name}" for a in node.names)
            elif isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
    out = set()
    for name in names:
        if name.split(".")[0] != top:
            continue
        base = src_root.joinpath(*name.split("."))
        if base.with_suffix(".py").is_file() or (base / "__init__.py").is_file():
            out.add(name)
    return out


def stage_files(module: str, src_root: Path) -> list[Path]:
    """The source files of every package module that importing ``module`` loads,
    found in a fresh isolated interpreter (so this process's modules do not leak
    in). Importing a stage module only imports; it does not connect to a database.
    A module that code imports inside a function is added too (found with ``ast``),
    and the closure is rebuilt until nothing new appears. Each round starts an
    interpreter (about 1 s): never call this in a loop."""
    top = module.split(".")[0]
    modules = [module]
    while True:
        files = _import_files(modules, src_root)
        known = set(modules)
        new = set()
        for f in files:
            new |= _lazy_modules(f, src_root, top) - known
        if not new:
            return files
        modules += sorted(new)


def code_hash(
    source: str,
    stage: str,
    *,
    module: str | None = None,
    src_root: Path | None = None,
    wrapper: Path | None = None,
) -> str:
    """SHA-256 over the stage's import closure (each file's bytes with its path
    relative to ``src_root``) plus the wrapper script. ``module``, ``src_root``
    and ``wrapper`` default to the stage's entry in ``STAGE_MODULES``; tests pass
    a temp package."""
    src_root = (src_root or _SRC_ROOT).resolve()
    if module is None:
        module, wrapper_rel = stage_module(source, stage)
        wrapper = wrapper or _SCRIPTS_DATA / wrapper_rel
    h = hashlib.sha256()
    for path in sorted(stage_files(module, src_root)):
        h.update(path.relative_to(src_root).as_posix().encode("utf-8") + b"\0")
        h.update(_lf(path.read_bytes()) + b"\0")
    if wrapper is not None:
        h.update(b"wrapper\0" + _lf(wrapper.read_bytes()))
    return h.hexdigest()


def _lf(data: bytes) -> bytes:
    """CRLF to LF: two checkouts of one commit can differ only in line endings
    (core.autocrlf), and a pass made in one must be valid in the other."""
    return data.replace(b"\r\n", b"\n")


def rules_hash(source: str) -> str:
    """SHA-256 of the rules file bytes (of no bytes when there is no file)."""
    try:
        data = _rules_path(source).read_bytes()
    except FileNotFoundError:
        data = b""
    return hashlib.sha256(data).hexdigest()


def pass_path(source: str, stage: str, processed_root: Path) -> Path:
    return Path(processed_root) / "_ops" / source / "gate" / f"{stage}.pass"


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("episteme")
    except Exception:  # noqa: BLE001 - the version is for the record only
        return "unknown"


def _git_head() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def write_pass(
    source: str,
    stage: str,
    processed_root: Path,
    report_path: Path,
    *,
    code_hash_value: str | None = None,
    rules_hash_value: str | None = None,
) -> Path:
    """Write the pass. ``code_hash_value`` and ``rules_hash_value`` are the hashes
    taken at the start of the sample run (``gate run``); they default to the
    hashes of now."""
    path = pass_path(source, stage, processed_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "source": source,
        "stage": stage,
        "package_version": _package_version(),
        "code_hash": code_hash_value or code_hash(source, stage),
        "rules_hash": rules_hash_value or rules_hash(source),
        "report": str(report_path),
        "passed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_head": _git_head(),
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def pass_valid(source: str, stage: str, processed_root: Path) -> tuple[bool, str]:
    """(valid, reason): "no pass", "code changed", "rules changed" or "ok"."""
    path = pass_path(source, stage, processed_root)
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return False, "no pass"
    if not isinstance(rec, dict):
        return False, "no pass"
    if rec.get("code_hash") != code_hash(source, stage):
        return False, "code changed"
    if rec.get("rules_hash") != rules_hash(source):
        return False, "rules changed"
    return True, "ok"


# --- command line -------------------------------------------------------------
#
# ``python -m episteme.ops.gate <command>``. A command that prints a value for the
# bash runner to capture (``prepare``, ``paths``) prints only that value on
# stdout; logs and diagnostics go to stderr. ``check`` prints its reason
# ("ok", "not gated", "no pass", "code changed", "rules changed") on stdout.

POLL_S = 0.5
_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "data" / "db" / "migrations"


def _err(message: str) -> None:
    print(message, file=sys.stderr)


@contextlib.contextmanager
def _connect():
    """The configured database (the runner sets ``EPISTEME_DB_TARGET=secondary``)."""
    from episteme.data.db.connection import connection

    with connection() as conn:
        yield conn


def _tree_rss(proc: Any) -> int:
    """Summed RSS (bytes) of ``proc`` and all its descendants, now."""
    import psutil

    total = 0
    try:
        procs = [proc, *proc.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0
    for p in procs:
        try:
            total += p.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total


def _stop_tree(proc: Any, child: subprocess.Popen) -> None:
    """Terminate the child and the descendants seen now (only what this run started)."""
    import psutil

    try:
        victims = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        victims = []
    victims.append(proc)
    for p in victims:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            p.terminate()
    _gone, alive = psutil.wait_procs(victims, timeout=5)
    for p in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            p.kill()
    with contextlib.suppress(Exception):
        child.wait(timeout=5)


def _cmd_run(args: argparse.Namespace) -> int:
    import psutil

    cmd = args.command
    if not cmd:
        _err("gate run: no command given (use: run --record F -- <command...>)")
        return 2
    if bool(args.source) != bool(args.stage):
        _err("gate run: give --source and --stage together")
        return 2
    hashes: dict[str, str] = {}
    if args.source:
        # Hash once, at the start: the pass certifies the code that ran, so a
        # change during a long sample run cannot be certified (gate report).
        try:
            hashes = {
                "code_hash": code_hash(args.source, args.stage),
                "rules_hash": rules_hash(args.source),
            }
        except (KeyError, RuntimeError, OSError) as exc:
            _err(f"gate run: could not hash {args.source} {args.stage}: {exc}")
            return 1
    t0 = time.monotonic()
    try:
        child = subprocess.Popen(cmd)
    except OSError as exc:
        _err(f"gate run: cannot start {cmd[0]}: {exc}")
        return 127
    proc = psutil.Process(child.pid)
    peak = 0
    interrupted = False
    try:
        while True:
            peak = max(peak, _tree_rss(proc))
            try:
                rc = child.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                continue
    except KeyboardInterrupt:
        interrupted = True
        rc = 130
    finally:
        if child.poll() is None:
            _stop_tree(proc, child)
    record = {
        "elapsed_s": round(time.monotonic() - t0, 3),
        "rc": rc,
        "peak_rss_mb": round(peak / (1024 * 1024), 1),
        **hashes,
    }
    if interrupted:
        record["interrupted"] = True
    out = Path(args.record)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return rc


def _cmd_prepare(args: argparse.Namespace) -> int:
    from episteme.ops import gate_slice

    processed = Path(args.processed_root) if args.processed_root else None
    try:
        gate_slice.prepare(
            args.source, Path(args.raw_root), Path(args.sample_root), processed_root=processed
        )
    except (gate_slice.SliceError, FileNotFoundError) as exc:
        # A refusal: no slice can be built (a group or file is missing, the cap).
        _err(f"gate prepare: {exc}")
        return 2
    except OSError as exc:
        # An operational failure (a copy or a link failed).
        _err(f"gate prepare: I/O error: {exc}")
        return 1
    print(Path(args.sample_root))
    return 0


def _cmd_paths(_args: argparse.Namespace) -> int:
    from episteme.config import get_settings

    print(get_settings().processed_root)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    if not is_gated(args.source, args.stage):
        print("not gated")
        return 0
    ok, reason = pass_valid(args.source, args.stage, Path(args.pass_root))
    print(reason)
    return 0 if ok else 1


def _hash_failures(source: str, stage: str, record: dict) -> list[str]:
    """The run record holds the hashes of the run start (``gate run --source
    --stage``). A record without them, or a hash that differs now, fails: the
    pass certifies only the code and the rules that ran."""
    out = []
    code_start, rules_start = record.get("code_hash"), record.get("rules_hash")
    if not code_start:
        out.append("run record: no code hash (start the sample with run_pipeline.sh --sample)")
    elif code_start != code_hash(source, stage):
        out.append("code_hash: the stage code changed during the sample run")
    if not rules_start:
        out.append("run record: no rules hash (start the sample with run_pipeline.sh --sample)")
    elif rules_start != rules_hash(source):
        out.append("rules_hash: the rules changed during the sample run")
    return out


def _cmd_report(args: argparse.Namespace) -> int:
    from episteme.data.checkpoint_markers import input_key
    from episteme.ops import gate_report, gate_slice, profile

    source, stage = args.source, args.stage
    rules = load_rules(source)
    stage_rules = rules.get(stage) if rules else None
    if not isinstance(stage_rules, dict):
        _err(f"gate report: ({source}, {stage}) has no gate rules")
        return 2
    sample_root = Path(args.sample_root)
    try:
        slice_meta = json.loads((sample_root / "slice.json").read_text(encoding="utf-8"))
        record = json.loads(Path(args.record).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _err(f"gate report: {exc}")
        return 2
    raw_dir = sample_root / "01_raw" / gate_slice.SLICES[source].raw_subpath
    # The units of work only (a sidecar file is no input of the stage); an old
    # slice.json without "inputs" falls back to every file.
    inputs = [raw_dir / rel for rel in slice_meta.get("inputs", slice_meta["files"])]
    staging_dir = (
        Path(args.staging_dir)
        if args.staging_dir
        else sample_root / "02_processed" / "staging" / source
    )
    measures = gate_report.measure_staging(staging_dir, inputs=inputs, record=record)
    prof = profile.get_profile()
    measures["part_rows"] = prof.part_rows
    measures["part_bytes"] = prof.part_bytes
    if stage == "load":
        # Part shards carry their own source_file (<input_key>__partNNNNN).
        source_files = measures.get("source_files") or [input_key(p, raw_dir) for p in inputs]
        with _connect() as conn:
            # The loader keeps one row per id (a re-issued id under another
            # source_file replaces the older row), so compare distinct ids.
            measures.update(
                gate_report.measure_load(
                    conn, source, source_files, staging_rows=measures.get("distinct_ids")
                )
            )
    failures = _hash_failures(source, stage, record) + evaluate(measures, stage_rules)
    if record.get("rc") not in (0, None):
        failures.insert(0, f"run record: the command exited with rc {record['rc']}")
    report_path = gate_report.write_report(measures, failures, sample_root)
    if failures:
        # An older pass with the same hashes must not let a full run through.
        old = pass_path(source, stage, Path(args.pass_root))
        old.unlink(missing_ok=True)
        print(f"gate FAIL for {source} {stage}:")
        for line in failures:
            print(f"  {line}")
        print(f"report: {report_path}")
        return 1
    pass_file = write_pass(
        source,
        stage,
        Path(args.pass_root),
        report_path,
        code_hash_value=record["code_hash"],
        rules_hash_value=record["rules_hash"],
    )
    print(f"gate PASS for {source} {stage}: {pass_file}")
    return 0


def _cmd_migrations_check(_args: argparse.Namespace) -> int:
    """List migrations whose objects are missing on the configured database.
    It checks the objects each migration creates (``gate_migrations``) and does
    not use ``episteme._migrations``, which is not kept up to date."""
    from episteme.ops import gate_migrations

    files = sorted(p.name for p in _MIGRATIONS_DIR.glob("*.sql"))
    for name in files:
        reason = gate_migrations.DEFERRED_MIGRATIONS.get(name)
        if reason is not None:
            _err(f"{name}: deferred ({reason})")
    files = [n for n in files if n not in gate_migrations.DEFERRED_MIGRATIONS]
    with _connect() as conn:
        missing = gate_migrations.missing_markers(conn, files)
    for name, objects in missing.items():
        print(f"{name}: missing {', '.join(objects)}")
    return 1 if missing else 0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m episteme.ops.gate", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="build the raw slice of a sample run")
    p.add_argument("--source", required=True)
    p.add_argument("--raw-root", required=True)
    p.add_argument("--sample-root", required=True)
    p.add_argument("--processed-root")
    p.set_defaults(fn=_cmd_prepare)

    p = sub.add_parser("run", help="run a command; record time, exit code, peak RSS")
    p.add_argument("--record", required=True)
    p.add_argument("--source", help="with --stage: record the code and rules hashes first")
    p.add_argument("--stage")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(fn=_cmd_run)

    p = sub.add_parser("report", help="measure a sample run, evaluate, write the pass")
    p.add_argument("--source", required=True)
    p.add_argument("--stage", required=True)
    p.add_argument("--sample-root", required=True)
    p.add_argument("--pass-root", required=True)
    p.add_argument("--record", required=True)
    p.add_argument("--staging-dir")
    p.set_defaults(fn=_cmd_report)

    p = sub.add_parser("check", help="exit 0 when the stage is not gated or has a valid pass")
    p.add_argument("--source", required=True)
    p.add_argument("--stage", required=True)
    p.add_argument("--pass-root", required=True)
    p.set_defaults(fn=_cmd_check)

    p = sub.add_parser("paths", help="print the processed root")
    p.set_defaults(fn=_cmd_paths)

    p = sub.add_parser("migrations-check", help="list migrations not applied to the database")
    p.set_defaults(fn=_cmd_migrations_check)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "run" and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    try:
        return int(args.fn(args))
    except ValueError as exc:
        _err(f"gate {args.cmd}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
