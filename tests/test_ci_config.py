"""Pins the CI / Dependabot / auto-merge configuration (spec 2026-09-30).

Reads the YAML with PyYAML. Note: PyYAML parses the workflow key `on:` as the
boolean True, so `_triggers()` looks up both spellings.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
GH = REPO / ".github"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(wf: dict) -> dict:
    trig = wf.get("on", wf.get(True))
    assert trig is not None, "workflow has no 'on:' block"
    return (
        trig
        if isinstance(trig, dict)
        else {t: None for t in ([trig] if isinstance(trig, str) else trig)}
    )


def _run_lines(job: dict) -> list[str]:
    return [s["run"] for s in job["steps"] if "run" in s]


def _uses(job: dict) -> list[str]:
    return [s["uses"] for s in job["steps"] if "uses" in s]


# --- dependabot.yml --------------------------------------------------------


def test_dependabot_ecosystems_are_uv_and_actions_weekly():
    cfg = _load(GH / "dependabot.yml")
    assert cfg["version"] == 2
    ups = {u["package-ecosystem"]: u for u in cfg["updates"]}
    assert set(ups) == {"uv", "github-actions"}
    for u in ups.values():
        assert u["directory"] == "/"
        assert u["schedule"]["interval"] == "weekly"


def test_dependabot_groups_security_and_minor_patch_updates():
    """Security updates grouped as `security-fixes`; minor and patch version updates
    grouped per ecosystem; a major bump stays a pull request of its own."""
    ups = {u["package-ecosystem"]: u for u in _load(GH / "dependabot.yml")["updates"]}
    for eco, minor_group in (
        ("uv", "uv-minor-patch"),
        ("github-actions", "gh-actions-minor-patch"),
    ):
        groups = ups[eco]["groups"]
        assert set(groups) == {"security-fixes", minor_group}
        assert groups["security-fixes"]["applies-to"] == "security-updates"
        assert groups["security-fixes"]["patterns"] == ["*"]
        mp = groups[minor_group]
        assert mp["applies-to"] == "version-updates"
        assert mp["patterns"] == ["*"]
        assert sorted(mp["update-types"]) == ["minor", "patch"]


def test_dependabot_schedule_and_commit_prefixes_match_mcpg():
    ups = {u["package-ecosystem"]: u for u in _load(GH / "dependabot.yml")["updates"]}
    for eco, prefix in (("uv", "chore"), ("github-actions", "ci")):
        u = ups[eco]
        assert u["schedule"]["day"] == "monday"
        assert u["schedule"]["time"] == "06:00"
        assert u["schedule"]["timezone"] == "Etc/UTC"
        # `include: scope` adds "(deps)", so the prefix must not carry it too
        assert u["commit-message"] == {"prefix": prefix, "include": "scope"}
        assert u["labels"] == ["dependencies"]
        assert u["open-pull-requests-limit"] >= 1


# --- ci.yml -----------------------------------------------------------------


def _ci_job() -> tuple[dict, dict]:
    wf = _load(GH / "workflows" / "ci.yml")
    return wf, wf["jobs"]["tests"]


def test_ci_triggers_and_read_only_permissions():
    wf, job = _ci_job()
    trig = _triggers(wf)
    assert "pull_request" in trig
    assert trig["push"]["branches"] == ["main"]
    assert wf["permissions"] == {"contents": "read"}
    assert job.get("name", "tests") == "tests"
    assert job["runs-on"] == "ubuntu-latest"


def test_ci_installs_locked():
    _, job = _ci_job()
    assert any("uv sync --locked" in r and "--extra dev" in r for r in _run_lines(job))


def test_ci_runs_ruff_format_and_check():
    _, job = _ci_job()
    runs = _run_lines(job)
    assert any("ruff format --check" in r for r in runs)
    assert any("ruff check" in r and "--fix" not in r for r in runs)


def test_ci_runs_model_smoke_tests():
    _, job = _ci_job()
    pytest_runs = [r for r in _run_lines(job) if "pytest" in r]
    assert len(pytest_runs) == 1
    cmd = pytest_runs[0]
    assert '-m "not pg"' in cmd
    assert "--ignore=tests/test_run_pipeline_dispatch.py" in cmd
    assert "tests/model" not in cmd  # model smoke tests gate model-library bumps
    assert "slow" not in cmd


def test_ci_removes_aria2_before_pytest():
    _, job = _ci_job()
    runs = _run_lines(job)
    aria2_idx = next(i for i, r in enumerate(runs) if "apt-get remove -y aria2" in r)
    pytest_idx = next(i for i, r in enumerate(runs) if "pytest" in r)
    assert aria2_idx < pytest_idx


def test_actions_run_on_current_majors():
    wf, job = _ci_job()
    uses = _uses(job)
    assert "actions/checkout@v7" in uses
    setup_uv = [u for u in uses if u.startswith("astral-sh/setup-uv@v10.")]
    assert len(setup_uv) == 1
    setup_uv_step = next(
        s for s in job["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@v10.")
    )
    assert setup_uv_step["with"]["prune-cache"] is True


# --- dependabot-auto-merge.yml -----------------------------------------------


def _am() -> tuple[dict, dict]:
    wf = _load(GH / "workflows" / "dependabot-auto-merge.yml")
    (job,) = wf["jobs"].values()
    return wf, job


def test_auto_merge_is_dependabot_only_and_never_checks_out():
    wf, job = _am()
    trig = _triggers(wf)
    assert "pull_request" in trig
    assert "pull_request_target" not in trig
    assert "dependabot[bot]" in job["if"]
    assert not any(u.startswith("actions/checkout") for u in _uses(job))
    assert job["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert wf.get("permissions") == {"contents": "read"}


def _step(job: dict, name_part: str) -> dict:
    (step,) = [s for s in job["steps"] if name_part in s.get("name", "")]
    return step


def test_auto_merge_only_for_signed_security_fixes():
    """Auto-merge is enabled only for the `security-fixes` group and only when every
    commit is Dependabot's, signed by GitHub (user decision 2026-10-18)."""
    _, job = _am()
    meta = _step(job, "Fetch Dependabot metadata")
    assert meta["id"] == "meta"
    signed = _step(job, "every commit is Dependabot's")
    assert signed["id"] == "signed"
    assert "steps.meta.outputs.dependency-group == 'security-fixes'" in signed["if"]
    for term in ('"dependabot[bot]"', '"web-flow"', "verification.verified", "--paginate"):
        assert term in signed["run"], term
    enable = _step(job, "Enable auto-merge")
    assert "steps.meta.outputs.dependency-group == 'security-fixes'" in enable["if"]
    assert "steps.signed.outputs.ok == 'true'" in enable["if"]
    assert "--auto" in enable["run"] and "--merge" in enable["run"]
    merges = [s for s in job["steps"] if "gh pr merge --auto" in s.get("run", "")]
    assert merges == [enable]


def test_auto_merge_is_turned_off_after_a_foreign_commit():
    _, job = _am()
    off = _step(job, "Turn auto-merge off")
    assert "steps.signed.outputs.ok == 'false'" in off["if"]
    assert "--disable-auto" in off["run"]
    failure = [s for s in job["steps"] if s.get("if") == "failure()"]
    assert len(failure) == 1 and "gh pr comment" in failure[0]["run"]


def test_fetch_metadata_is_pinned_to_a_commit():
    _, job = _am()
    uses = _step(job, "Fetch Dependabot metadata")["uses"]
    name, ref = uses.split("@")
    assert name == "dependabot/fetch-metadata"
    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)
