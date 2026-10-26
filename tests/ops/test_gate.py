"""Gate rules, evaluation, code hash and pass file (ops/gate.py)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from episteme.ops import gate

RULES = {
    "extract": {"min_rows": 100, "max_duplicate_ids": 0},
    "load": {"max_orphan_bodies": 0},
}


@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    d = tmp_path / "rules"
    d.mkdir()
    monkeypatch.setattr(gate, "RULES_DIR", d)
    return d


def _write_rules(d: Path, source: str, rules: dict) -> None:
    (d / f"{source}.json").write_text(json.dumps(rules), encoding="utf-8")


# --- evaluate: each rule, met and broken -----------------------------------

GOOD = {
    "rows": 500,
    "rows_per_input": 5.0,
    "text_bytes_per_input_byte": 0.5,
    "duplicate_ids": 0,
    "duplicate_ids_in_shard": 0,
    "empty_text_pct": 0.0,
    "extract_status_pct": {"dropped": 0.5, "partial": 1.0},
    "null_pct": {"title": 0.0},
    "license_counts": {"CC BY": 10, "unknown": 2},
    "failed_inputs": 0,
    "orphan_bodies": 0,
    "rows_loaded_equals_staging": True,
}

CASES = [
    # rule, value in rules, measure patch that breaks it, words the line must hold
    ("min_rows", 100, {"rows": 40}, ["min_rows", "100", "40"]),
    ("min_rows_per_input", 3.0, {"rows_per_input": 1.8}, ["min_rows_per_input", "3", "1.8"]),
    (
        "min_text_bytes_per_input_byte",
        0.01,
        {"text_bytes_per_input_byte": 0.006},
        ["min_text_bytes_per_input_byte", "0.01", "0.006"],
    ),
    ("max_duplicate_ids", 0, {"duplicate_ids": 3}, ["max_duplicate_ids", "0", "3"]),
    (
        "max_duplicate_ids_in_shard",
        0,
        {"duplicate_ids_in_shard": 1},
        ["max_duplicate_ids_in_shard", "0", "1"],
    ),
    ("max_empty_text_pct", 0.0, {"empty_text_pct": 2.5}, ["max_empty_text_pct", "2.5"]),
    (
        "max_status_pct",
        {"dropped": 2.0, "partial": 10.0},
        {"extract_status_pct": {"dropped": 7.0}},
        ["max_status_pct", "dropped", "2", "7"],
    ),
    (
        "max_null_pct",
        {"title": 1.0},
        {"null_pct": {"title": 30.0}},
        ["max_null_pct", "title", "1", "30"],
    ),
    (
        "allowed_licenses",
        ["CC BY", "unknown"],
        {"license_counts": {"CC BY": 1, "proprietary": 4}},
        ["allowed_licenses", "proprietary"],
    ),
    ("max_failed_inputs", 0, {"failed_inputs": 2}, ["max_failed_inputs", "0", "2"]),
    ("max_orphan_bodies", 0, {"orphan_bodies": 5}, ["max_orphan_bodies", "0", "5"]),
    (
        "rows_loaded_equals_staging",
        True,
        {"rows_loaded_equals_staging": False},
        ["rows_loaded_equals_staging"],
    ),
]


@pytest.mark.parametrize(("rule", "limit", "patch", "words"), CASES, ids=[c[0] for c in CASES])
def test_rule_met_gives_no_line(rule, limit, patch, words):
    assert gate.evaluate(GOOD, {rule: limit}) == []


@pytest.mark.parametrize(("rule", "limit", "patch", "words"), CASES, ids=[c[0] for c in CASES])
def test_rule_broken_gives_one_line(rule, limit, patch, words):
    lines = gate.evaluate({**GOOD, **patch}, {rule: limit})
    assert len(lines) == 1
    for w in words:
        assert w in lines[0]


def test_a_rule_at_its_limit_passes():
    m = {**GOOD, "rows": 100, "duplicate_ids": 0}
    assert gate.evaluate(m, {"min_rows": 100, "max_duplicate_ids": 0}) == []


def test_a_missing_measure_fails():
    lines = gate.evaluate({}, {"min_rows": 1})
    assert len(lines) == 1
    assert "min_rows" in lines[0]


def test_a_null_column_absent_from_null_pct_counts_as_zero():
    assert gate.evaluate({"null_pct": {}}, {"max_null_pct": {"title": 1.0}}) == []


def test_a_status_absent_from_the_mix_counts_as_zero():
    assert gate.evaluate({"extract_status_pct": {}}, {"max_status_pct": {"dropped": 2.0}}) == []


def test_several_broken_rules_give_several_lines():
    lines = gate.evaluate({**GOOD, "rows": 1, "duplicate_ids": 9}, RULES["extract"])
    assert len(lines) == 2


def test_unknown_rule_key_raises():
    with pytest.raises(ValueError, match="min_row"):
        gate.evaluate(GOOD, {"min_row": 100})


# --- rules file ---------------------------------------------------------------


def test_is_gated_follows_the_sections(rules_dir):
    assert gate.load_rules("apollo") is None
    assert not gate.is_gated("apollo", "extract")
    _write_rules(rules_dir, "apollo", {"extract": {"min_rows": 1}})
    assert gate.load_rules("apollo") == {"extract": {"min_rows": 1}}
    assert gate.is_gated("apollo", "extract")
    assert not gate.is_gated("apollo", "load")


def test_damaged_rules_file_raises(rules_dir):
    (rules_dir / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        gate.load_rules("bad")


@pytest.mark.parametrize(
    ("rules", "words"),
    [
        ({"extarct": {"min_rows": 1}}, ["extarct"]),
        ({"graph": {"min_rows": 1}}, ["graph"]),
        ({"extract": {"max_null_pct": {"titel": 1.0}}}, ["max_null_pct", "titel"]),
        ({"extract": {"max_status_pct": {"droped": 1.0}}}, ["max_status_pct", "droped"]),
    ],
    ids=["typo-stage", "unhashed-stage", "null-column", "status"],
)
def test_load_rules_rejects_unknown_names(rules_dir, rules, words):
    """F8: a typo never disables a rule; a stage the gate cannot hash is refused."""
    _write_rules(rules_dir, "apollo", rules)
    with pytest.raises(ValueError) as err:
        gate.load_rules("apollo")
    for w in words:
        assert w in str(err.value)


@pytest.mark.parametrize("section", [[], "x", 1, None], ids=["list", "str", "int", "null"])
def test_load_rules_rejects_a_stage_section_that_is_not_an_object(rules_dir, section):
    """PR review: ``"extract": []`` must not leave the stage ungated."""
    _write_rules(rules_dir, "apollo", {"extract": section})
    with pytest.raises(ValueError) as err:
        gate.load_rules("apollo")
    assert "extract" in str(err.value)


def test_load_rules_accepts_known_names(rules_dir):
    rules = {
        "extract": {
            "max_null_pct": {"abstract": 1.0, "language": 2.0},
            "max_status_pct": {"ok": 100.0, "empty": 1.0, "partial": 1.0, "dropped": 1.0},
        },
        "load": {"max_orphan_bodies": 0},
    }
    _write_rules(rules_dir, "apollo", rules)
    assert gate.load_rules("apollo") == rules


def test_rules_dir_is_the_package_folder():
    assert gate.RULES_DIR.name == "gate_rules"
    assert (gate.RULES_DIR / "__init__.py").is_file()


# --- code hash ------------------------------------------------------------------


def _make_pkg(root: Path) -> Path:
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pkg / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    (pkg / "stage.py").write_text("from pkg import helper\n", encoding="utf-8")
    return pkg


def _hash(root: Path, wrapper: Path | None = None) -> str:
    return gate.code_hash("s", "extract", module="pkg.stage", src_root=root, wrapper=wrapper)


def test_code_hash_is_stable(tmp_path):
    _make_pkg(tmp_path / "src")
    assert _hash(tmp_path / "src") == _hash(tmp_path / "src")


def test_code_hash_changes_when_an_imported_module_changes(tmp_path):
    pkg = _make_pkg(tmp_path / "src")
    before = _hash(tmp_path / "src")
    (pkg / "helper.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert _hash(tmp_path / "src") != before


def test_code_hash_ignores_a_module_the_stage_does_not_import(tmp_path):
    pkg = _make_pkg(tmp_path / "src")
    before = _hash(tmp_path / "src")
    (pkg / "other.py").write_text("OTHER = 2\n", encoding="utf-8")
    assert _hash(tmp_path / "src") == before


def test_code_hash_ignores_line_endings(tmp_path):
    """Two checkouts of the same commit can differ only in line endings
    (core.autocrlf): a pass made in one must be valid in the other."""
    pkg = _make_pkg(tmp_path / "src")
    wrapper = tmp_path / "run.sh"
    wrapper.write_bytes(b"echo 1\n")
    before = _hash(tmp_path / "src", wrapper)
    (pkg / "helper.py").write_bytes(b"VALUE = 1\r\n")
    (pkg / "stage.py").write_bytes(b"from pkg import helper\r\n")
    wrapper.write_bytes(b"echo 1\r\n")
    assert _hash(tmp_path / "src", wrapper) == before


def test_code_hash_changes_when_the_wrapper_changes(tmp_path):
    _make_pkg(tmp_path / "src")
    wrapper = tmp_path / "run.sh"
    wrapper.write_text("echo 1\n", encoding="utf-8")
    before = _hash(tmp_path / "src", wrapper)
    wrapper.write_text("echo 2\n", encoding="utf-8")
    assert _hash(tmp_path / "src", wrapper) != before
    assert before != _hash(tmp_path / "src")


def test_code_hash_uses_a_fresh_interpreter(tmp_path):
    """A temp package named like the real one (already loaded in this process) is
    read from the temp tree: a change to its config.py changes the hash."""
    root = tmp_path / "src"
    pkg = root / "episteme"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "config.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "stage.py").write_text("from episteme import config\n", encoding="utf-8")
    import episteme.config  # noqa: F401 - loaded here from the real tree

    files = gate.stage_files("episteme.stage", root.resolve())
    assert {f.name for f in files} == {"__init__.py", "config.py", "stage.py"}
    assert all(f.is_relative_to(root.resolve()) for f in files)
    before = gate.code_hash("s", "extract", module="episteme.stage", src_root=root)
    (pkg / "config.py").write_text("X = 2\n", encoding="utf-8")
    assert gate.code_hash("s", "extract", module="episteme.stage", src_root=root) != before


def test_stage_files_with_no_output_raises_clearly(tmp_path):
    root = tmp_path / "src"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "quiet.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no module list"):
        gate.stage_files("pkg.quiet", root.resolve())


def test_stage_module_accessor():
    assert gate.stage_module("anything", "load") == (
        "episteme.data.load_articles",
        "anything/load_anything.sh",
    )
    assert gate.stage_module("apollo", "extract")[0].endswith("extract_apollo")
    with pytest.raises(KeyError):
        gate.stage_module("apollo", "graph")


GATED = [
    ("bookshelf", "extract"),
    ("europepmc_manuscript", "extract"),
    ("apollo", "extract"),
    ("bookshelf", "load"),
    ("pubchem", "load"),
]


def _lazy_episteme_imports(path: Path) -> set[str]:
    """Dotted module names that code inside functions imports from episteme."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] == "episteme":
                    found.add(node.module)
                    found.update(f"{node.module}.{a.name}" for a in node.names)
            elif isinstance(node, ast.Import):
                found.update(a.name for a in node.names if a.name.split(".")[0] == "episteme")
    return found


@pytest.mark.parametrize(("source", "stage"), GATED)
def test_lazy_imports_of_the_closure_are_already_hashed(source, stage):
    """An import inside a function is invisible to the hash unless the module is
    also imported at top level somewhere in the closure. Fail on a lazy-only one."""
    files = set(gate.stage_files(gate.hash_modules(source, stage), gate._SRC_ROOT.resolve()))
    src = gate._SRC_ROOT.resolve()
    missing = []
    for f in sorted(files):
        for dotted in _lazy_episteme_imports(f):
            base = src.joinpath(*dotted.split("."))
            candidates = [base.with_suffix(".py"), base / "__init__.py"]
            for c in candidates:
                if c.is_file() and c.resolve() not in files:
                    missing.append(f"{f.name} imports {dotted} lazily; {c.name} is not hashed")
    assert not missing, missing


def test_code_hash_of_an_unimportable_module_raises(tmp_path):
    _make_pkg(tmp_path / "src")
    with pytest.raises(RuntimeError):
        gate.code_hash("s", "extract", module="pkg.nothing", src_root=tmp_path / "src")


def test_stage_modules_table():
    assert gate.STAGE_MODULES[("bookshelf", "extract")] == (
        "episteme.data.bookshelf.extract_bookshelf",
        "bookshelf/extract_bookshelf.sh",
    )
    assert gate.STAGE_MODULES[("apollo", "load")] == (
        "episteme.data.load_articles",
        "apollo/load_apollo.sh",
    )
    assert gate.STAGE_MODULES[("whatever", "load")][1] == "whatever/load_whatever.sh"
    for key in [
        ("bookshelf", "extract"),
        ("europepmc_manuscript", "extract"),
        ("apollo", "extract"),
    ]:
        module, wrapper = gate.STAGE_MODULES[key]
        assert (gate._SCRIPTS_DATA / wrapper).is_file(), wrapper


def test_real_stage_hash_is_stable_and_differs_by_stage():
    a = gate.code_hash("bookshelf", "extract")
    assert a == gate.code_hash("bookshelf", "extract")
    assert a != gate.code_hash("bookshelf", "load")


# --- pass file ----------------------------------------------------------------


@pytest.fixture
def fake_hash(monkeypatch):
    state = {"h": "hash-1"}
    monkeypatch.setattr(gate, "code_hash", lambda source, stage, **kw: state["h"])
    return state


def test_no_pass(tmp_path, rules_dir, fake_hash):
    assert gate.pass_valid("apollo", "extract", tmp_path) == (False, "no pass")


def test_write_pass_then_valid(tmp_path, rules_dir, fake_hash):
    _write_rules(rules_dir, "apollo", RULES)
    path = gate.write_pass("apollo", "extract", tmp_path, tmp_path / "report.json")
    assert path == tmp_path / "_ops" / "apollo" / "gate" / "extract.pass"
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["source"] == "apollo"
    assert rec["stage"] == "extract"
    assert rec["code_hash"] == "hash-1"
    assert rec["rules_hash"] == gate.rules_hash("apollo")
    assert rec["report"] == str(tmp_path / "report.json")
    assert rec["passed_at"].endswith("+00:00")
    assert "package_version" in rec
    assert "git_head" in rec
    assert gate.pass_valid("apollo", "extract", tmp_path) == (True, "ok")


def test_changed_code_hash_invalidates(tmp_path, rules_dir, fake_hash):
    _write_rules(rules_dir, "apollo", RULES)
    gate.write_pass("apollo", "extract", tmp_path, tmp_path / "r.json")
    fake_hash["h"] = "hash-2"
    assert gate.pass_valid("apollo", "extract", tmp_path) == (False, "code changed")


def test_changed_rules_file_invalidates(tmp_path, rules_dir, fake_hash):
    _write_rules(rules_dir, "apollo", RULES)
    gate.write_pass("apollo", "extract", tmp_path, tmp_path / "r.json")
    _write_rules(rules_dir, "apollo", {"extract": {"min_rows": 5}})
    assert gate.pass_valid("apollo", "extract", tmp_path) == (False, "rules changed")


def test_a_pass_is_per_stage(tmp_path, rules_dir, fake_hash):
    _write_rules(rules_dir, "apollo", RULES)
    gate.write_pass("apollo", "extract", tmp_path, tmp_path / "r.json")
    assert gate.pass_valid("apollo", "load", tmp_path) == (False, "no pass")


def test_a_damaged_pass_file_reads_as_no_pass(tmp_path, rules_dir, fake_hash):
    p = gate.pass_path("apollo", "extract", tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{oops", encoding="utf-8")
    assert gate.pass_valid("apollo", "extract", tmp_path) == (False, "no pass")


# --- parts (pubchem serialize) ------------------------------------------------


def test_parts_within_caps_pass_and_fail():
    m = {
        "max_rows_per_source_file": 5,
        "max_est_bytes_per_source_file": 10,
        "part_rows": 4,
        "part_bytes": 100,
    }
    fails = gate.evaluate(m, {"parts_within_caps": True})
    assert len(fails) == 1
    assert "5" in fails[0] and "4" in fails[0]
    assert gate.evaluate({**m, "part_rows": 5}, {"parts_within_caps": True}) == []
    bytes_fail = gate.evaluate({**m, "part_rows": 5, "part_bytes": 9}, {"parts_within_caps": True})
    assert len(bytes_fail) == 1 and "10" in bytes_fail[0] and "9" in bytes_fail[0]


def test_parts_within_caps_missing_measure_fails():
    fails = gate.evaluate({"part_rows": 4, "part_bytes": 100}, {"parts_within_caps": True})
    assert fails and "missing" in fails[0]
    assert gate.evaluate({}, {"parts_within_caps": False}) == []


def test_max_peak_rss_mb_rule():
    assert len(gate.evaluate({"peak_rss_mb": 1100}, {"max_peak_rss_mb": 1024})) == 1
    assert gate.evaluate({"peak_rss_mb": 1000}, {"max_peak_rss_mb": 1024}) == []
    assert "missing" in gate.evaluate({}, {"max_peak_rss_mb": 1024})[0]


def test_pubchem_serialize_stage_is_known():
    assert gate.stage_module("pubchem", "serialize") == (
        "episteme.data.pubchem.serialize_pubchem",
        "pubchem/serialize_pubchem.sh",
    )
    assert (gate._SCRIPTS_DATA / "pubchem/serialize_pubchem.sh").is_file()
    module, _ = gate.stage_module("pubchem", "serialize")
    files = {p.name for p in gate.stage_files(module, gate._SRC_ROOT.resolve())}
    assert "serialize_pubchem.py" in files


def test_pubchem_load_hash_covers_the_retire_step():
    """load_pubchem.sh runs the retire step after the load: its code is part of
    the pubchem load stage, so a change to it voids a pubchem load pass."""
    assert gate.hash_modules("pubchem", "load") == [
        "episteme.data.load_articles",
        "episteme.data.pubchem.retire",
    ]
    assert gate.hash_modules("apollo", "load") == ["episteme.data.load_articles"]
    src = gate._SRC_ROOT.resolve()
    files = {
        p.relative_to(src).as_posix()
        for p in gate.stage_files(gate.hash_modules("pubchem", "load"), src)
    }
    assert "episteme/data/pubchem/retire.py" in files
    assert "episteme/data/load_articles.py" in files


def test_code_hash_covers_every_hash_module(tmp_path):
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "a.py").write_text("A = 1", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("B = 1", encoding="utf-8")
    before = gate.code_hash("s", "load", module=["pkg.a", "pkg.b"], src_root=root)
    (root / "pkg" / "b.py").write_text("B = 2", encoding="utf-8")
    assert gate.code_hash("s", "load", module=["pkg.a", "pkg.b"], src_root=root) != before
