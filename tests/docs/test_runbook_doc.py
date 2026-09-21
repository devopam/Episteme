"""docs/10 (operator runbook) vs the dispatcher and wrappers. Read-only; no DB, no network."""

from __future__ import annotations

import re
import shlex

from ._repo import REPO, doc, lit_sources, structured_sources, wrapper_table

RUNBOOK = "10-data-sources-runbook.md"
STAGES = {"all", "download", "extract", "load", "graph", "materialize", "enrich", "serialize"}
DISPATCHER_FLAGS = {"--dry-run", "--force", "--reason", "--max-files"}
# flags that consume the next token as their value
VALUE_FLAGS = {"--reason", "--max-files", "--since", "--raw-dir", "--repo-type"}
# stages that honour --max-files (extract/serialize/download) or forward it (all)
MAX_FILES_STAGES = {"download", "extract", "serialize", "all"}
# downloads that accept --max-files but ignore it (hf download resumes by itself)
MAX_FILES_IGNORED = {"apollo", "guidelines", "hf_corpus"}
_RUN = (REPO / "scripts" / "data" / "run_pipeline.sh").read_text(encoding="utf-8")


def _text() -> str:
    return doc(RUNBOOK)


def _fenced_lines() -> list[str]:
    """Logical command lines inside ``` fences (backslash continuations joined)."""
    out: list[str] = []
    for block in re.findall(r"```[^\n]*\n(.*?)```", _text(), re.S):
        block = block.replace("\\\n", " ")
        out.extend(ln.strip() for ln in block.splitlines() if ln.strip())
    return out


def _inline_spans() -> list[str]:
    stripped = re.sub(r"```.*?```", "", _text(), flags=re.S)
    return re.findall(r"`([^`\n]+)`", stripped)


def _invocations(lines: list[str]) -> list[list[str]]:
    """Tokenised run_pipeline.sh invocations: [source, stage, *rest]."""
    found: list[list[str]] = []
    for ln in lines:
        if "run_pipeline.sh" not in ln or ln.lstrip().startswith("#"):
            continue
        toks = shlex.split(ln, comments=True)
        idx = next((i for i, t in enumerate(toks) if t.endswith("run_pipeline.sh")), None)
        if idx is None:
            continue
        rest = toks[idx + 1 :]
        # skip prose-like spans such as `run_pipeline.sh <source> <stage>` (placeholders)
        if len(rest) < 2 or any(t.startswith("<") for t in rest[:2]):
            continue
        found.append(rest)
    return found


def _fenced() -> list[list[str]]:
    return _invocations(_fenced_lines())


def _all_invocations() -> list[list[str]]:
    return _fenced() + _invocations(_inline_spans())


def _allowed(src: str, stage: str) -> bool:
    if src == "corpus":
        return stage == "materialize"
    if src not in wrapper_table():
        return False
    if src == "pmc":
        return stage in {"download", "extract", "load", "graph", "materialize", "enrich", "all"}
    if stage in ("download", "all"):
        return True
    if stage == "extract":
        return src in lit_sources()
    if stage == "graph":
        return src in lit_sources() or src == "mesh"
    if stage == "serialize":
        return src in structured_sources()
    if stage == "load":
        return src in lit_sources() or src in structured_sources() or src == "europepmc_id_mappings"
    if stage == "enrich":
        return src == "europepmc_lite"
    return False


def _wrapper_text(src: str) -> str:
    rel = wrapper_table()[src]
    return (REPO / "scripts" / "data" / rel).read_text(encoding="utf-8")


def test_the_runbook_shows_commands():
    assert len(_fenced()) >= 20, "runbook shows too few run_pipeline.sh commands"


def test_documented_commands_are_accepted_by_the_dispatcher_rules():
    for rest in _all_invocations():
        src, stage = rest[0], rest[1]
        assert stage in STAGES, rest
        assert _allowed(src, stage), f"runbook documents an unwired command: {src} {stage}"


def test_every_dispatcher_stage_and_source_is_reachable_from_the_doc():
    seen = {(r[0], r[1]) for r in _all_invocations()}
    for src in wrapper_table():
        assert (src, "download") in seen or (src, "all") in seen, f"no command for {src}"
    for st in STAGES:
        assert any(s == st for _, s in seen), f"stage {st} never shown"


def test_flags_are_real_and_used_sensibly():
    usage = re.search(r'echo "usage: \$0 (.*?)"', _RUN).group(1)
    for f in DISPATCHER_FLAGS:
        assert f in usage, f"{f} missing from run_pipeline.sh usage"
    for rest in _all_invocations():
        src, stage, args = rest[0], rest[1], rest[2:]
        flags: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a.startswith("--"):
                flags.append(a)
                if a in VALUE_FLAGS:
                    i += 1
                    assert i < len(args), f"{a} without a value: {rest}"
                    if a == "--max-files":
                        assert args[i].isdigit(), f"--max-files needs an integer: {rest}"
                    if a == "--reason":
                        assert args[i].strip(), rest
            i += 1
        for f in flags:
            if f in DISPATCHER_FLAGS:
                continue
            # a passthrough flag must be understood by the source's own download wrapper
            assert stage in ("download", "all"), f"{f} passes through on download only: {rest}"
            assert f in _wrapper_text(src), f"{f} is not a flag of {src}'s wrapper: {rest}"
        if "--force" in flags:
            assert "--reason" in flags, f"--force needs --reason: {rest}"
        if "--dry-run" in flags:
            assert stage == "download", f"--dry-run is download-only: {rest}"
        if "--max-files" in flags:
            assert stage in MAX_FILES_STAGES, f"--max-files is ignored by {stage}: {rest}"
            if stage == "download":
                assert src not in MAX_FILES_IGNORED, f"{src} download ignores --max-files: {rest}"
            assert src != "corpus", rest


def test_positional_modes_exist_in_the_wrapper():
    for rest in _all_invocations():
        src, stage, args = rest[0], rest[1], rest[2:]
        if src == "corpus" or stage not in ("download", "all"):
            continue
        skip = False
        for a in args:
            if skip:
                skip = False
                continue
            if a.startswith("--"):
                skip = a in VALUE_FLAGS
                continue
            if src == "hf_corpus":
                continue  # <repo_id> argument, not a mode
            assert re.search(rf"\b{re.escape(a)}\b", _wrapper_text(src)), (src, a)


def test_every_wired_source_has_a_runbook_section_or_inventory_pointer():
    t = _text()
    for src in wrapper_table():
        assert src in t, src
    assert "12-source-inventory" in t


def test_guard_and_go_live_are_documented():
    t = _text()
    for term in (
        "EPISTEME_DB_MODE",
        "EPISTEME_DB_TARGET",
        "PGDATABASE_SECONDARY",
        "EPISTEME_PRODUCTION_DATABASE",
        "episteme_test",
        "restricted",
        "11-gxp-data-integrity",
        "server_version",
        "PGDATABASE=episteme_test",
    ):
        assert term in t, term
    assert "EPISTEME_DB_MODE=restricted" in t


def test_known_hazards_are_documented():
    t = _text()
    for term in (
        "PYTHONIOENCODING=utf-8",  # --report cp1252 crash on Windows
        "--max-files",
        "cdisc_bc",
        "PROVENANCE.txt",
        "UNVERIFIED",
        "0002_container_and_book_parts.sql",
        "0003_mesh_hierarchy.sql",
        "migrate_database.sh",
        "verify_audit_trail.sh",
        "source_inventory.sh",
        "run_start",
        "run_end",
    ):
        assert term in t, term
    assert "Quick status board" not in t, "the dated status board was replaced by docs/12"


def test_paths_mentioned_exist():
    t = _text()
    for p in set(re.findall(r"scripts/data/[\w./-]+\.sh", t)):
        assert (REPO / p).is_file(), p
    for p in set(re.findall(r"src/episteme/[\w./-]+\.(?:sql|py)", t)):
        assert (REPO / p).is_file(), p
    for p in set(re.findall(r"docs/[\w./-]+\.md", t)):
        assert (REPO / p).is_file(), p


def test_env_key_names_only_no_values_or_inline_comments():
    for ln in _fenced_lines():
        m = re.match(r"^(?:export\s+)?([A-Z][A-Z0-9_]*)=(\S+)", ln)
        if not m:
            continue
        key, val = m.groups()
        if key.endswith(("PASSWORD", "API_KEY", "JWT")):
            assert val.startswith(("<", "$", '"$')), f"secret-looking value shown for {key}"
