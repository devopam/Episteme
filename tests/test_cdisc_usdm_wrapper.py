"""Network-free tests for scripts/data/cdisc_usdm/download_cdisc_usdm.sh (fake curl on PATH)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "data" / "cdisc_usdm" / "download_cdisc_usdm.sh"
TAG = "v4.0.0"
SHA = "aa303cb32f5d3ceecc68a16803e26720d2c1fc26"
RAW = "https://raw.githubusercontent.com/cdisc-org/DDF-RA/" + SHA

pytestmark = pytest.mark.skipif(
    shutil.which("aria2c") is not None, reason="http_fetch prefers aria2c, bypassing the curl shim"
)

DATA_FILES = [
    "Deliverables/API/USDM_API.json",
    "Deliverables/CT/USDM_CT.xlsx",
    "Deliverables/IG/USDM IG #2.pdf",
    "Deliverables/UML/UML_Views/Timeline.png",
]


def _entry(path: str, kind: str) -> dict:
    e = {
        "path": path,
        "mode": "100644" if kind == "blob" else "040000",
        "type": kind,
        "sha": "0" * 40,
    }
    if kind == "blob":
        e["size"] = 10
    e["url"] = "https://api.github.com/x"
    return e


def _entry_type_first(path: str, kind: str) -> dict:
    # Same fields as _entry but with "type" emitted before "path", to prove the awk tree
    # parser pairs path/type per entry rather than by "last path seen, next type wins".
    e = {
        "type": kind,
        "mode": "100644" if kind == "blob" else "040000",
        "path": path,
        "sha": "0" * 40,
    }
    if kind == "blob":
        e["size"] = 10
    e["url"] = "https://api.github.com/x"
    return e


def _tree(paths: list[tuple[str, str]], truncated: bool = False) -> str:
    body = {
        "sha": SHA,
        "url": "https://api.github.com/x",
        "tree": [_entry(p, k) for p, k in paths],
        "truncated": truncated,
    }
    return json.dumps(body, indent=2) + "\n"


def _tree_type_first(paths: list[tuple[str, str]], truncated: bool = False) -> str:
    body = {
        "sha": SHA,
        "url": "https://api.github.com/x",
        "tree": [_entry_type_first(p, k) for p, k in paths],
        "truncated": truncated,
    }
    return json.dumps(body, indent=2) + "\n"


DEFAULT_TREE = _tree(
    [
        ("Deliverables", "tree"),
        ("Deliverables/API", "tree"),
        *[(p, "blob") for p in DATA_FILES],
        ("Deliverables/../evil.txt", "blob"),
        ("Documents/Examples/example.json", "blob"),
        ("LICENSE", "blob"),
        ("README.md", "blob"),
    ]
)


def _release(tag: str) -> str:
    return (
        json.dumps({"url": "https://api.github.com/x", "id": 1, "tag_name": tag}, indent=2) + "\n"
    )


COMMIT = json.dumps({"sha": SHA, "node_id": "x", "commit": {"sha": "f" * 40}}, indent=2) + "\n"


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _msys_path(p: Path) -> str:
    q = p.as_posix()  # C:/x -> /c/x so bash accepts it inside PATH on Windows
    return f"/{q[0].lower()}{q[2:]}" if len(q) > 1 and q[1] == ":" else q


def _fake_curl(bindir: Path, log: Path, tree: str, release: str) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "release.json").write_text(release, encoding="utf-8")
    (bindir / "commit.json").write_text(COMMIT, encoding="utf-8")
    (bindir / "tree.json").write_text(tree, encoding="utf-8")
    script = bindir / "curl"
    body = (
        "#!/usr/bin/env bash\n"
        f'BIN="{bindir.as_posix()}"\n'
        "out=''; url=''; head=0; prev=''\n"
        'for a in "$@"; do\n'
        '  [ "$prev" = "-o" ] || [ "$prev" = "--output" ] && out="$a"\n'
        '  case "$a" in -I|-sSIL|-sIL|-fsSI) head=1 ;; http*) url="$a" ;; esac\n'
        '  prev="$a"\n'
        "done\n"
        f'echo "$url" >> "{log.as_posix()}"\n'
        'case "$url" in\n'
        '  */releases/latest) body="$(cat "$BIN/release.json")" ;;\n'
        '  */commits/*) body="$(cat "$BIN/commit.json")" ;;\n'
        '  */git/trees/*) body="$(cat "$BIN/tree.json")" ;;\n'
        '  */LICENSE) body="licence text" ;;\n'
        '  */README.md) body="readme text" ;;\n'
        '  *) body="data of $(basename "$url")" ;;\n'
        "esac\n"
        'if [ "$head" = 1 ]; then\n'
        '  printf "content-length: %s\\r\\n" "$(printf "%s\\n" "$body" | wc -c | tr -d " ")"\n'
        "  exit 0\n"
        "fi\n"
        'if [ -n "$out" ]; then mkdir -p "$(dirname "$out")"; printf "%s\n" "$body" > "$out"\n'
        'else printf "%s\n" "$body"; fi\n'
    )
    script.write_bytes(body.encode())  # LF endings: a CRLF shebang breaks bash on Windows
    script.chmod(0o755)


def _run(tmp_path, *args, tree: str = DEFAULT_TREE, release: str | None = None):
    log = tmp_path / "curl.log"
    _fake_curl(tmp_path / "bin", log, tree, _release(TAG) if release is None else release)
    env = {
        **os.environ,
        "SHIM_DIR": _msys_path(tmp_path / "bin"),
        "EPISTEME_ACTOR": "episteme_sys_admin",
        "EPISTEME_DATA_ROOT": str(tmp_path),
        "EPISTEME_RAW_ROOT": str(tmp_path / "01_raw"),
        "PGDATABASE": "episteme_test",
    }
    proc = subprocess.run(
        # Git-for-Windows bash prepends /mingw64/bin (real curl) to PATH at startup, so put the
        # shim first from *inside* a bash -c, then exec the wrapper.
        [_bash(), "-c", 'PATH="$SHIM_DIR:$PATH"; exec bash "$0" "$@"', str(WRAPPER), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def _root(tmp_path) -> Path:
    return tmp_path / "01_raw" / "cdisc_usdm"


def _rel(tmp_path) -> Path:
    return _root(tmp_path) / TAG


def _data_files(tmp_path) -> list[str]:
    base = _rel(tmp_path)
    return sorted(
        p.relative_to(base).as_posix() for p in (base / "Deliverables").rglob("*") if p.is_file()
    )


def test_fetches_deliverables_licence_readme_and_provenance(tmp_path):
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _data_files(tmp_path) == sorted(DATA_FILES)
    rel = _rel(tmp_path)
    assert (rel / "LICENSE").read_text(encoding="utf-8").startswith("licence text")
    assert (rel / "README.md").read_text(encoding="utf-8").startswith("readme text")
    assert not (rel / "Documents").exists()  # outside Deliverables/
    assert not list(tmp_path.rglob("evil.txt"))  # '..' path rejected
    assert "rejecting unsafe path" in proc.stderr
    assert (_root(tmp_path) / "last_sync_utc.txt").is_file()
    raw_calls = [c for c in calls if c.startswith("https://raw.githubusercontent.com/")]
    assert raw_calls and all(c.startswith(RAW + "/") for c in raw_calls)  # pinned to the SHA
    assert not any(f"/{TAG}/" in c for c in raw_calls)
    assert f"{RAW}/Deliverables/IG/USDM%20IG%20%232.pdf" in calls  # URL encoded, local name kept
    prov = (rel / "PROVENANCE.txt").read_text(encoding="utf-8")
    assert "source_repo: https://github.com/cdisc-org/DDF-RA" in prov
    assert f"release_tag: {TAG}" in prov
    assert f"commit_sha: {SHA}" in prov  # top-level sha, not the nested one
    assert "retrieved_at: 20" in prov
    assert "model files not covered" in prov
    assert "until CDISC confirms a licence for the model files" in prov
    assert "excluded from any training corpus" in prov


def test_max_files_caps_data_files_only(tmp_path):
    proc, _ = _run(tmp_path, "--max-files", "2")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert len(_data_files(tmp_path)) == 2
    assert (_rel(tmp_path) / "LICENSE").is_file() and (_rel(tmp_path) / "README.md").is_file()
    assert (_rel(tmp_path) / "PROVENANCE.txt").is_file()


def test_dry_run_writes_nothing(tmp_path):
    proc, calls = _run(tmp_path, "--dry-run")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stderr.count("DRY: would fetch") == len(DATA_FILES) + 2  # + LICENSE, README.md
    assert not _root(tmp_path).exists()
    api_calls = [c for c in calls if c.startswith("https://api.github.com/")]
    assert len(api_calls) == 3  # release, commit, tree


def test_second_run_is_up_to_date_and_force_refetches(tmp_path):
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    prov = _rel(tmp_path) / "PROVENANCE.txt"
    # An up-to-date sentinel must already record this run's SHA on its own line, or the
    # stale-commit_sha rewrite (grep -qx "commit_sha: $sha") would legitimately touch it.
    sentinel = f"sentinel\ncommit_sha: {SHA}\n"
    prov.write_text(sentinel, encoding="utf-8")
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "up to date" in proc.stderr
    assert prov.read_text(encoding="utf-8") == sentinel
    proc, _ = _run(tmp_path, "--force", "--reason", "test")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert f"commit_sha: {SHA}" in prov.read_text(encoding="utf-8")


def test_stale_commit_sha_is_rewritten_on_up_to_date_run(tmp_path):
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    prov = _rel(tmp_path) / "PROVENANCE.txt"
    text = prov.read_text(encoding="utf-8")
    assert f"commit_sha: {SHA}" in text
    stale_sha = "b" * 40
    prov.write_text(
        text.replace(f"commit_sha: {SHA}", f"commit_sha: {stale_sha}"), encoding="utf-8"
    )
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "up to date" in proc.stderr  # same-size files: nothing was re-fetched
    rewritten = prov.read_text(encoding="utf-8")
    assert f"commit_sha: {SHA}" in rewritten
    assert stale_sha not in rewritten


def test_url_glob_chars_are_percent_encoded(tmp_path):
    special_path = "Deliverables/RULES/Rules[1]{a}.xlsx"
    tree = _tree(
        [
            ("Deliverables", "tree"),
            ("Deliverables/RULES", "tree"),
            (special_path, "blob"),
            ("LICENSE", "blob"),
            ("README.md", "blob"),
        ]
    )
    proc, calls = _run(tmp_path, tree=tree)
    assert proc.returncode == 0, proc.stderr[-2000:]
    expected_url = RAW + "/Deliverables/RULES/Rules%5B1%5D%7Ba%7D.xlsx"
    assert expected_url in calls
    assert (_rel(tmp_path) / "Deliverables" / "RULES" / "Rules[1]{a}.xlsx").is_file()


def test_awk_tree_parser_pairs_path_and_type_per_entry(tmp_path):
    # "type" precedes "path" in every entry here (the reverse of GitHub's usual field
    # order); a parser that remembers "last path seen, next type wins" mis-pairs across
    # entries. It must still fetch exactly the Deliverables/ blobs and skip the tree entry.
    tree = _tree_type_first(
        [
            ("Deliverables", "tree"),
            ("Deliverables/API", "tree"),
            ("Deliverables/API/USDM_API.json", "blob"),
            ("Deliverables/CT/USDM_CT.xlsx", "blob"),
            ("LICENSE", "blob"),
            ("README.md", "blob"),
        ]
    )
    proc, _ = _run(tmp_path, tree=tree)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _data_files(tmp_path) == sorted(
        ["Deliverables/API/USDM_API.json", "Deliverables/CT/USDM_CT.xlsx"]
    )


def test_last_deliverables_entry_is_not_fetched_twice(tmp_path):
    # The outer tree object's own closing "}" line matches the same awk closing-brace
    # pattern used to emit a per-entry blob path. If the parser never clears path/type
    # after emitting, and the LAST tree entry is a Deliverables/ blob, the outer object's
    # close re-emits that same path a second time -> fetched twice.
    tree = _tree(
        [
            ("Deliverables", "tree"),
            ("Deliverables/API/USDM_API.json", "blob"),
            ("Deliverables/CT/USDM_CT.xlsx", "blob"),
        ]
    )
    proc, calls = _run(tmp_path, tree=tree)
    assert proc.returncode == 0, proc.stderr[-2000:]
    get_calls = [c for c in calls if c.startswith("https://raw.githubusercontent.com/")]
    assert get_calls.count(RAW + "/Deliverables/CT/USDM_CT.xlsx") == 1

    # Separate, untouched tmp dir: no local files means no size-match HEAD requests to
    # muddy the "DRY: would fetch" count.
    proc, _ = _run(tmp_path / "dry", "--dry-run", tree=tree)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stderr.count("DRY: would fetch") == 4  # 2 data files + LICENSE + README.md


def test_older_release_folder_is_untouched(tmp_path):
    old = _root(tmp_path) / "v3.13.0" / "Deliverables" / "keep.txt"
    old.parent.mkdir(parents=True)
    old.write_text("old release\n", encoding="utf-8")
    proc, _ = _run(tmp_path, "--force", "--reason", "test")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert old.read_text(encoding="utf-8") == "old release\n"
    assert (_rel(tmp_path) / "PROVENANCE.txt").is_file()


def test_truncated_tree_dies(tmp_path):
    tree = _tree([(p, "blob") for p in DATA_FILES], truncated=True)
    proc, _ = _run(tmp_path, tree=tree)
    assert proc.returncode != 0
    assert "truncated" in proc.stderr
    assert not _root(tmp_path).exists()


def test_empty_deliverables_dies(tmp_path):
    tree = _tree([("Documents/Examples/example.json", "blob"), ("README.md", "blob")])
    proc, _ = _run(tmp_path, tree=tree)
    assert proc.returncode != 0
    assert "no files resolved under Deliverables/" in proc.stderr
    assert not _root(tmp_path).exists()


def test_unexpected_tag_dies(tmp_path):
    proc, _ = _run(tmp_path, release=_release("../x"))
    assert proc.returncode != 0
    assert "refusing unexpected release tag" in proc.stderr
    assert not _root(tmp_path).exists()


def test_unresolvable_release_dies_and_writes_nothing(tmp_path):
    proc, _ = _run(tmp_path, release="")
    assert proc.returncode != 0
    assert "GitHub API rate limit?" in proc.stderr
    assert not _root(tmp_path).exists()


def test_positional_dies_unknown_mode(tmp_path):
    proc, _ = _run(tmp_path, "yaml")
    assert proc.returncode != 0
    assert "unknown mode 'yaml'" in proc.stderr
    assert not _root(tmp_path).exists()
