# CI and automatic Dependabot security merges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every pull request runs lint and the non-database tests in CI; Dependabot security-update pull requests merge themselves once that CI run passes.

**Architecture:** First bring the repository to a clean `ruff` baseline (Task 1). Then add three GitHub files — `dependabot.yml` (uv + github-actions, security-only group `security-fixes`), `ci.yml` (job `tests`), `dependabot-auto-merge.yml` (auto-merge only for that group) — with config tests that read the YAML, plus docs (Task 2). Once this PR's own `tests` run is green, the controller adds a required `tests` status check to the `main` ruleset before merging (ruleset step, not a task).

**Tech Stack:** GitHub Actions, Dependabot, `uv` (0.11.x locally), ruff 0.6.9, pytest, PyYAML.

**Spec:** `docs/superpowers/specs/2026-09-30-ci-dependabot-automerge-design.md`

## Global Constraints

- Auto-merge applies only to Dependabot pull requests whose `dependency-group` is `security-fixes`; routine updates stay one pull request per package, weekly, never auto-merged.
- Dependabot ecosystems: exactly `uv` and `github-actions`, `directory: "/"`, `interval: "weekly"`, each with group `security-fixes` (`applies-to: security-updates`, `patterns: ["*"]`); no group with `applies-to: version-updates`.
- CI job id and name: `tests`, `ubuntu-latest`, Python 3.13, `uv sync --locked --extra dev`, `ruff format --check .`, `ruff check .`, `pytest -m "not pg" -q --ignore=tests/test_run_pipeline_dispatch.py`. CI never ignores `tests/model` and never deselects `slow`. Workflow `permissions: contents: read`.
- The auto-merge workflow uses `pull_request` (never `pull_request_target`), never checks out code, runs only when `github.event.pull_request.user.login == 'dependabot[bot]'`, and merges with `gh pr merge --auto --merge`.
- No behaviour change in Task 1: the full non-`pg` suite, including `tests/model/test_model_smoke.py`, passes before and after.
- No database work. Secrets: never read, print, cat, grep or copy `.env`; never dump the environment.
- Git: `git add` explicit paths only; never `--no-verify`; never stage `.gitignore` or `.env`. Commit messages end with:
  ```
  Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```
- Local commands on Windows: Python `.venv/Scripts/python.exe`, ruff `.venv/Scripts/ruff.exe` (0.6.9, same as CI), `uv` is on PATH.

## Review Focus

1. **PyYAML turns the workflow key `on:` into the boolean `True`.** Config tests that look up `"on"` only would pass vacuously or crash. Pinned by the `_triggers()` helper in Task 2's test file, and a test that it finds `pull_request`.
2. **CI silently skipping the model libraries** (a later edit adds `-m "not slow"` or `--ignore=tests/model`): CVE bumps to `torch`/`accelerate` would then merge untested. Pinned by `test_ci_runs_model_smoke_tests`.
3. **The auto-merge workflow widening its reach** (running for non-Dependabot pull requests, checking out pull-request code, or switching to `pull_request_target`): pinned by `test_auto_merge_is_dependabot_only_and_never_checks_out`.
4. **Lock drift** (`pyproject.toml` changed without `uv.lock`): CI must fail. Pinned by `test_ci_installs_locked` (asserts `--locked`).
5. **Routine updates accidentally grouped into the security group** (a `version-updates` group, or the group's `applies-to` changed): they would then auto-merge. Pinned by `test_dependabot_groups_security_updates_only`.

---

## File Structure

- Modify (Task 1, lint only): `src/episteme/data/_legacy_download.py`, `src/episteme/data/curate/deduplicate_corpus.py`, `src/episteme/data/curate/serialize_structured_sources.py`, `src/episteme/model/evaluate_benchmarks.py`, `src/episteme/model/train_continual_pretraining.py`, `src/episteme/model/train_preference_optimization.py`, `src/episteme/model/train_supervised_finetuning.py`, `tests/model/test_model_smoke.py`.
- Modify (Task 2): `.github/dependabot.yml`, `pyproject.toml` (+ `uv.lock`), `docs/10-data-sources-runbook.md`, `CLAUDE.md`, `docs/project-incubation-baseline.md`.
- Create (Task 2): `.github/workflows/ci.yml`, `.github/workflows/dependabot-auto-merge.yml`, `tests/test_ci_config.py`.

---

### Task 1: Clean `ruff` baseline (no behaviour change)

**Files:**
- Modify: the eight files listed under File Structure (Task 1).
- Test: existing suite (`pytest -m "not pg"`), including `tests/model/test_model_smoke.py`.

**Interfaces:**
- Consumes: nothing.
- Produces: a tree on which `.venv/Scripts/ruff.exe format --check .` and `.venv/Scripts/ruff.exe check .` both exit 0. Task 2's CI relies on this.

- [ ] **Step 1: Record the baseline**

Run:
```bash
.venv/Scripts/ruff.exe format --check . ; .venv/Scripts/ruff.exe check . --statistics
.venv/Scripts/python.exe -m pytest -m "not pg" -q --ignore=tests/test_run_pipeline_dispatch.py
```
Expected: ruff reports 8 files to reformat and 84 errors (44 E501, 18 F401, 8 I001, 5 F841, 4 UP015, 3 F541, 1 B007, 1 B905); pytest ends `0 failed` (the tests in `tests/model/` need network for the tiny Hugging Face model). Save both summary lines for the report. If pytest already fails, stop and report `BLOCKED` with the failure.

- [ ] **Step 2: Apply the automatic fixes**

Run:
```bash
.venv/Scripts/ruff.exe format src/episteme/data/_legacy_download.py src/episteme/data/curate/deduplicate_corpus.py src/episteme/data/curate/serialize_structured_sources.py src/episteme/model/evaluate_benchmarks.py src/episteme/model/train_continual_pretraining.py src/episteme/model/train_preference_optimization.py src/episteme/model/train_supervised_finetuning.py tests/model/test_model_smoke.py
.venv/Scripts/ruff.exe check --fix src/episteme/data/_legacy_download.py src/episteme/data/curate/deduplicate_corpus.py src/episteme/data/curate/serialize_structured_sources.py src/episteme/model/evaluate_benchmarks.py src/episteme/model/train_continual_pretraining.py src/episteme/model/train_preference_optimization.py src/episteme/model/train_supervised_finetuning.py tests/model/test_model_smoke.py
.venv/Scripts/ruff.exe check . --output-format concise
```
Do not use `--unsafe-fixes`. Expected: the remaining errors are E501 (long lines), the three `deepspeed` F401s, and the F841/B007 ones ruff will not fix automatically.

- [ ] **Step 3: Fix the rest by hand, with these rules**

- **E501:** wrap to 100 characters (split strings with implicit concatenation, break argument lists). Do not change any string's runtime value.
- **`import deepspeed` inside `try:` (F401)** in the three `train_*.py` files: keep the import (it probes availability and sets `HAS_DEEPSPEED`), add `  # noqa: F401  (availability probe)`.
- **F841 unused local:** if the right-hand side is a call whose side effect matters (opens a connection, writes a file, mutates state), keep the call and drop only the name (`call(...)` instead of `x = call(...)`); if it is a pure expression, delete the line. Read the surrounding code before deciding and state each decision in your report.
- **B007 unused loop variable** (`dirnames` in `serialize_structured_sources.py`): rename to `_dirnames`.
- **B905 `zip()` without `strict=`** in `_legacy_download.py`: add `strict=False` (keeps today's behaviour).
- Do not change logic, messages, or public names anywhere.

- [ ] **Step 4: Verify ruff is clean and behaviour is unchanged**

Run:
```bash
.venv/Scripts/ruff.exe format --check . && .venv/Scripts/ruff.exe check .
.venv/Scripts/python.exe -m pytest -m "not pg" -q --ignore=tests/test_run_pipeline_dispatch.py
```
Expected: ruff prints `147 files already formatted` (or the current count) and `All checks passed!`; pytest ends with the same passed/skipped counts as Step 1 and `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add src/episteme/data/_legacy_download.py src/episteme/data/curate/deduplicate_corpus.py src/episteme/data/curate/serialize_structured_sources.py src/episteme/model/evaluate_benchmarks.py src/episteme/model/train_continual_pretraining.py src/episteme/model/train_preference_optimization.py src/episteme/model/train_supervised_finetuning.py tests/model/test_model_smoke.py
git commit -m "style: clean ruff baseline for CI (format + lint, no behaviour change)

Co-Authored-By: <model> <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z"
```
If a pre-commit hook (ruff, bandit) fails, fix the cause and commit again; never `--no-verify`.

---

### Task 2: Dependabot, CI and auto-merge workflows, config tests, docs

**Files:**
- Modify: `.github/dependabot.yml`, `pyproject.toml`, `uv.lock`, `docs/10-data-sources-runbook.md` (§8), `CLAUDE.md` ("Where work runs"), `docs/project-incubation-baseline.md` (append)
- Create: `.github/workflows/ci.yml`, `.github/workflows/dependabot-auto-merge.yml`
- Test: `tests/test_ci_config.py`

**Interfaces:**
- Consumes: Task 1's clean ruff baseline.
- Produces: a CI check run named `tests` (job id `tests` in workflow `ci`), which the pre-merge ruleset step requires.

- [ ] **Step 1: Add PyYAML explicitly to the dev dependencies**

In `pyproject.toml`, add `"pyyaml>=6.0",` to the `[project.optional-dependencies] dev` list (after `"jsonschema>=4.0.0",`) and to the `[dependency-groups] dev` list (after `"jsonschema>=4.0.0",`). Then run:
```bash
uv lock
uv lock --check
git diff --stat uv.lock
```
Expected: `uv lock --check` succeeds; the `uv.lock` diff only adds `pyyaml` to the `episteme` package's dev requirements (no other package versions change). If other versions change, run `git checkout uv.lock` and retry with `uv lock` only after reverting; report it if it persists.

- [ ] **Step 2: Write the failing config tests**

Create `tests/test_ci_config.py`:

```python
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


def test_dependabot_groups_security_updates_only():
    for u in _load(GH / "dependabot.yml")["updates"]:
        groups = u.get("groups", {})
        assert groups["security-fixes"]["applies-to"] == "security-updates"
        assert groups["security-fixes"]["patterns"] == ["*"]
        # routine updates must stay ungrouped, one pull request per package
        assert all(g.get("applies-to") != "version-updates" for g in groups.values())
        assert all(g.get("applies-to") == "security-updates" for g in groups.values())


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


def test_auto_merge_only_for_security_group():
    _, job = _am()
    meta = [s for s in job["steps"] if s.get("uses", "").startswith("dependabot/fetch-metadata@")]
    assert len(meta) == 1 and meta[0]["id"] == "meta"
    merges = [s for s in job["steps"] if "gh pr merge" in s.get("run", "")]
    assert len(merges) == 1
    step = merges[0]
    assert "steps.meta.outputs.dependency-group == 'security-fixes'" in step["if"]
    assert "--auto" in step["run"] and "--merge" in step["run"]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ci_config.py -q`
Expected: FAIL — `KeyError`/`FileNotFoundError` for the missing workflows, and the dependabot tests fail on the `pip` ecosystem.

- [ ] **Step 4: Replace `.github/dependabot.yml`**

```yaml
# Dependabot: weekly routine updates (one pull request per package, merged by a person)
# and security updates grouped as `security-fixes`, which the dependabot-auto-merge
# workflow merges automatically once the CI `tests` check passes.
version: 2
updates:
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      security-fixes:
        applies-to: security-updates
        patterns: ["*"]
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      security-fixes:
        applies-to: security-updates
        patterns: ["*"]
```

- [ ] **Step 5: Create `.github/workflows/ci.yml`**

```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
concurrency:
  group: ci-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true
jobs:
  tests:
    name: tests
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
          python-version: "3.13"
      - name: Install (locked)
        run: uv sync --locked --extra dev
      - name: ruff format
        run: uv run --no-sync ruff format --check .
      - name: ruff check
        run: uv run --no-sync ruff check .
      - name: pytest (no database, no network dispatch smoke test)
        run: uv run --no-sync pytest -m "not pg" -q --ignore=tests/test_run_pipeline_dispatch.py
```

- [ ] **Step 6: Create `.github/workflows/dependabot-auto-merge.yml`**

```yaml
# Enables auto-merge on Dependabot SECURITY pull requests (group `security-fixes`).
# The merge happens only after the required `tests` check passes (ruleset on main).
# Never checks out or runs pull-request code.
name: dependabot-auto-merge
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions:
  contents: read
jobs:
  auto-merge:
    if: github.event.pull_request.user.login == 'dependabot[bot]'
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
    steps:
      - name: Fetch Dependabot metadata
        id: meta
        uses: dependabot/fetch-metadata@v2
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
      - name: Enable auto-merge for security fixes
        if: steps.meta.outputs.dependency-group == 'security-fixes'
        run: gh pr merge --auto --merge "$PR_URL"
        env:
          PR_URL: ${{ github.event.pull_request.html_url }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

- [ ] **Step 7: Run the config tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ci_config.py -q`
Expected: `8 passed`.

- [ ] **Step 8: Docs**

(a) `docs/10-data-sources-runbook.md`: insert this subsection at the end of section 8 (after the ops-cadence table, before the `---` that precedes `## 9. Document control`). Use no fenced code block (the runbook's tests check every fenced command against the pipeline dispatcher):

```markdown
### 8.1 CI and Dependabot

- **CI** (`.github/workflows/ci.yml`, check `tests`) runs on every pull request and every push to `main`: `uv sync --locked --extra dev`, `ruff format --check .`, `ruff check .`, and `pytest -m "not pg"` without `tests/test_run_pipeline_dispatch.py`. It includes the model smoke tests in `tests/model/`, which download a tiny Hugging Face model. The `pg` tests and the network dispatch smoke test stay local-only; run them before merging changes that touch the database or `run_pipeline.sh`.
- **`main` requires the `tests` check.** Every change reaches `main` through a pull request; direct pushes (including GitHub web-editor commits) are refused.
- **Dependabot** checks weekly for `uv` and GitHub Actions updates. Routine updates arrive one pull request per package and wait for a person. Security updates arrive grouped as `security-fixes`; `.github/workflows/dependabot-auto-merge.yml` enables auto-merge on them, so they merge by themselves once `tests` passes.
- **A security pull request that did not merge** has a failed `tests` run: open its Checks tab, fix the break on the Dependabot branch (or wait for a newer Dependabot push), and it merges when `tests` goes green.
```

(b) `CLAUDE.md`, section "Where work runs": add this bullet after the "Local machine only" bullet:

```markdown
- **CI (GitHub Actions):** every pull request runs `ruff format --check`, `ruff check` and `pytest -m "not pg"` (without the network dispatch smoke test) as the required `tests` check; changes reach `main` only through pull requests. Dependabot security pull requests merge themselves once `tests` passes.
```

(c) `docs/project-incubation-baseline.md`: append at the end of the file:

```markdown
- 2026-09-30: CI and automatic Dependabot security merges (spec `docs/superpowers/specs/2026-09-30-ci-dependabot-automerge-design.md`).
  - **Decisions (user, 2026-09-30):** auto-merge CVE (security) updates only, any version jump, gated by CI; routine updates weekly, one pull request per package, merged by a person; CI installs the full dev set so model-library bumps are exercised by `tests/model/test_model_smoke.py`; security updates are recognised by the Dependabot group `security-fixes` (no personal token).
  - **Landed:** a clean `ruff` baseline (8 legacy/model files, no behaviour change); `dependabot.yml` switched from `pip` to `uv` (so `uv.lock` updates too) plus `github-actions`; `.github/workflows/ci.yml` (check `tests`); `.github/workflows/dependabot-auto-merge.yml`; `tests/test_ci_config.py`; `pyyaml` added to the dev dependencies; runbook §8.1; `CLAUDE.md`.
  - **Also done:** Dependabot pull requests #15–#19 (raised under `pip`, `pyproject.toml` only) closed with comments; CodeQL alerts #1–#6 dismissed with reasons (#1–#5 false positives, #6 test code); `SECURITY.md` added (PR #20).
  - **Deferred:** the `pg` tests and the network dispatch smoke test are not in CI; pinning GitHub Actions to commit SHAs.
```

- [ ] **Step 9: Full verification**

Run:
```bash
.venv/Scripts/ruff.exe format --check . && .venv/Scripts/ruff.exe check .
.venv/Scripts/python.exe -m pytest -m "not pg" -q --ignore=tests/test_run_pipeline_dispatch.py
uv lock --check
```
Expected: ruff clean; pytest `0 failed` (Task 1's count plus 8); `uv lock --check` succeeds.

- [ ] **Step 10: Commit**

```bash
.venv/Scripts/ruff.exe format tests/test_ci_config.py && .venv/Scripts/ruff.exe check tests/test_ci_config.py
git add .github/dependabot.yml .github/workflows/ci.yml .github/workflows/dependabot-auto-merge.yml tests/test_ci_config.py pyproject.toml uv.lock docs/10-data-sources-runbook.md CLAUDE.md docs/project-incubation-baseline.md
git commit -m "ci: pull-request CI and automatic Dependabot security merges

Co-Authored-By: <model> <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z"
```

---

## Ruleset step (controller, once this PR's CI `tests` run is green — before merging)

1. Confirm this PR's `tests` check is green: `gh pr checks <PR number>`.
2. Add the required status check to the ruleset, keeping the existing rules:

```bash
cat > "$TEMP/ruleset-v2.json" <<'EOF'
{
  "name": "default protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "copilot_code_review", "parameters": { "review_on_push": false, "review_draft_pull_requests": false } },
    { "type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [ { "context": "tests", "integration_id": 15368 } ] } }
  ]
}
EOF
gh api -X PUT repos/devopam/Episteme/rulesets/24230442 --input "$TEMP/ruleset-v2.json" --jq '[.rules[].type]'
gh api repos/devopam/Episteme/rules/branches/main --jq '.[].type'
```
Expected: the four rule types, including `required_status_checks`.

Why before merge, not after: until the ruleset requires `tests`, `gh pr merge --auto` merges a clean Dependabot pull request immediately, so the rule must exist before the auto-merge workflow's first run reaches `main`.
