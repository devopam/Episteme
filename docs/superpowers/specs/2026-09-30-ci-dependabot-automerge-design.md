# CI and automatic Dependabot security merges: design

**Date:** 2026-09-30
**Status:** Design approved in chat by the user (2026-09-30); this written spec awaits review.
**Depends on:** `main` at or after the user's `dependabot.yml` commit (8d9a679) and the recreated "default protection" ruleset (id 24230442).

## 1. Goal

When Dependabot opens a pull request because a CVE was found, the fix merges into `main` with no human approval — but only after an automated test run passes. Routine (non-CVE) version updates keep arriving weekly, one pull request per package, for the user to merge.

**Success:** a security-update pull request that passes CI lands on `main` by itself; one that fails CI stays open and the user is notified; nothing reaches `main` without the CI `tests` check passing.

## 2. Decisions (user, 2026-09-30)

- **Auto-merge scope:** CVE (security) updates only, any size of version jump, gated by CI.
- **Routine updates:** weekly, one pull request per package, merged by the user.
- **Model libraries:** CI installs the full dev set (including `torch`, `transformers`, `accelerate`, `peft`, `trl`) and runs the model smoke tests (see §4.3).
- **Detecting a CVE pull request:** a Dependabot group that applies only to security updates; the auto-merge workflow acts only on that group. No personal access token.
- The five `pip`-ecosystem pull requests (#15–#19) were closed with comments and the six CodeQL alerts dismissed with reasons (done 2026-09-30, outside this spec).

## 3. Facts the design rests on (verified 2026-09-30)

- Repository settings: auto-merge is allowed (`allow_auto_merge: true`); branches are deleted on merge; Dependabot security updates, secret scanning with push protection, and CodeQL default setup (actions, python) are enabled.
- The only workflow is `.github/workflows/shellcheck.yml` (path-filtered to `scripts/**`). Nothing runs the Python tests on pull requests.
- The ruleset "default protection" targets `~DEFAULT_BRANCH` with rules `deletion`, `non_fast_forward`, `copilot_code_review`; no bypass actors.
- The project is managed with `uv` (`uv.lock`, a `[dependency-groups] dev` group with ruff `<0.7`, pytest, bandit, pre-commit). Extras: `data`, `model`, `dev` (= `data` + `model` + pytest + jsonschema).
- `tests/model/test_model_smoke.py` already imports the four `src/episteme/model/` scripts and runs each one's `main()` as a `--dry_run` against the tiny Hugging Face model `HuggingFaceM4/tiny-random-LlamaForCausalLM` (network needed). It is not `pg`- or `slow`-marked, so `pytest -m "not pg"` runs it. *(Correction 2026-09-30: the chat design said no test touched the model libraries; that missed this file, which imports them through `episteme.model`.)*
- `ruff format --check .` and `ruff check .` (ruff 0.6.9, the version `uv.lock` pins) fail on the current tree: 8 files would be reformatted and 84 lint errors, all in `src/episteme/data/_legacy_download.py`, `src/episteme/data/curate/{deduplicate_corpus,serialize_structured_sources}.py`, the four `src/episteme/model/` scripts and `tests/model/test_model_smoke.py`. `uv lock --check` passes.
- `pyyaml` is in `uv.lock` only as a transitive dependency.
- The `pip`-ecosystem `dependabot.yml` produced pull requests that changed `pyproject.toml` only, never `uv.lock`.
- `tests/test_run_pipeline_dispatch.py` needs the network and takes about 13 minutes (real `--dry-run` of every source).

## 4. Design

### 4.1 `.github/dependabot.yml`

Two entries, both `schedule: interval: weekly`:

- `package-ecosystem: "uv"`, `directory: "/"`.
- `package-ecosystem: "github-actions"`, `directory: "/"`.

Each carries the same group:

```yaml
groups:
  security-fixes:
    applies-to: security-updates
    patterns: ["*"]
```

No group applies to version updates, so routine updates stay one pull request per package. Security updates for each ecosystem arrive as one grouped pull request named after `security-fixes`.

### 4.1a Lint baseline

Before CI can gate anything, the eight files in §3 are brought to a clean `ruff format --check .` and `ruff check .`: `ruff format`, `ruff check --fix`, then hand fixes (long lines wrapped; unused imports removed, except optional-dependency probes such as `import deepspeed` inside `try`, which keep a `# noqa: F401` with a reason; unused locals removed while keeping any call whose side effect matters). No behaviour change; the full non-`pg` suite, including `tests/model/test_model_smoke.py`, must pass before and after.

### 4.2 CI — `.github/workflows/ci.yml`

- Triggers: `pull_request` (all branches) and `push` to `main`. `permissions: contents: read`. `concurrency` cancels superseded runs of the same pull request.
- One job, id and name `tests`, `runs-on: ubuntu-latest`, timeout 30 minutes.
- Steps: checkout; `astral-sh/setup-uv` with caching enabled and Python 3.13; `uv sync --locked --extra dev` (the locked versions, including the Linux `torch` build `uv.lock` pins; the uv cache keeps repeat runs fast); `uv run ruff format --check .`; `uv run ruff check .`; `uv run pytest -m "not pg" -q --ignore=tests/test_run_pipeline_dispatch.py`.
- `--locked` makes CI fail if `uv.lock` does not match `pyproject.toml` — the failure the old `pip` pull requests would have caused.
- The network dispatch smoke test stays a manual, local check (documented in docs/10).

### 4.3 Model smoke tests — existing `tests/model/test_model_smoke.py`

- No new test. The existing smoke tests import the four `episteme.model.*` scripts and dry-run each `main()` on a tiny model, which exercises the model libraries' loading paths — more than an import check would.
- CI must run them: the pytest command must not deselect `slow` or ignore `tests/model`.
- They catch broken releases, removed or renamed APIs and install failures; they do not prove full training behaviour.

### 4.4 Auto-merge — `.github/workflows/dependabot-auto-merge.yml`

- Trigger: `pull_request` (`opened`, `synchronize`, `reopened`). Job condition: `github.event.pull_request.user.login == 'dependabot[bot]'`.
- `permissions: contents: write, pull-requests: write` (job-level; the default token of a Dependabot-triggered run is read-only otherwise).
- Steps: `dependabot/fetch-metadata@v2` (id `meta`); then, only if `steps.meta.outputs.dependency-group == 'security-fixes'`, run `gh pr merge --auto --merge "$PR_URL"` with `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` and `PR_URL: ${{ github.event.pull_request.html_url }}`.
- It never checks out or runs pull-request code, and it uses `pull_request`, not `pull_request_target`.
- Merge method: a merge commit, as for every other merge in this repository.
- A merge made with the workflow token does not start new workflow runs on `main`; the pull request's own CI run is the gate.

### 4.5 Ruleset — required status check

- Add to "default protection" (id 24230442) a `required_status_checks` rule: context `tests` (the CI job), `strict_required_status_checks_policy: false` (a pull request need not be rebased onto the latest `main` before merging; Dependabot rebases its own pull requests when needed).
- Applied with `gh api -X PUT repos/devopam/Episteme/rulesets/24230442` carrying the full existing rule set plus the new rule, **after** the CI workflow is on `main` and has reported `tests` at least once (otherwise every pull request, including this one, would wait on a check that never runs).
- Effect: nothing merges into `main` unless `tests` passed, and commits can no longer be pushed straight to `main` (for example from GitHub's web editor); every change goes through a pull request.

### 4.6 Failure path

- CI fails → the pull request stays open with auto-merge pending; GitHub notifies the repository owner. Nothing reaches `main`. A newer Dependabot push re-runs CI; a human fix on the branch does too.
- The workflow cannot bypass the ruleset (no bypass actors).

### 4.7 Docs

- `docs/10-data-sources-runbook.md`: a short section "CI and Dependabot" — what CI runs, that CVE pull requests merge themselves after CI passes, that routine updates wait for the user, that the network dispatch test and the `pg` tests are local-only, and how to see why a security pull request did not merge.
- `CLAUDE.md` "Where work runs": one line saying CI runs `ruff` and `pytest -m "not pg"` (without the network dispatch module) on every pull request, and changes reach `main` only through pull requests.
- `docs/project-incubation-baseline.md`: drift-log entry.

## 5. Tests

- `tests/test_ci_config.py`, reading the YAML files with `yaml.safe_load` (`pyyaml` added explicitly to the `[dependency-groups] dev` group and to the `dev` extra, `uv.lock` updated):
  - `dependabot.yml`: exactly the `uv` and `github-actions` ecosystems, both weekly, both with group `security-fixes` whose `applies-to` is `security-updates`, and no group with `applies-to: version-updates`.
  - `ci.yml`: a job `tests`; its steps include `uv sync --locked`, `ruff format --check`, `ruff check`, and a pytest run with `-m "not pg"` and `--ignore=tests/test_run_pipeline_dispatch.py`, that neither ignores `tests/model` nor deselects `slow`; `permissions` is read-only.
  - PyYAML reads the workflow key `on:` as the boolean `True`; the tests must look up both.
  - `dependabot-auto-merge.yml`: triggered by `pull_request` (not `pull_request_target`); the job condition names `dependabot[bot]`; a step uses `dependabot/fetch-metadata`; the merge step's `if` checks `dependency-group` against `security-fixes`; its command contains `gh pr merge --auto`; no step uses `actions/checkout`.
- CI itself proves the rest: this change's own pull request must show a green `tests` run before the ruleset step (§4.5).

## 6. Out of scope

- Running the `pg` tests in CI (database work stays local; the server is PostgreSQL 19beta3).
- Auto-merging routine (non-security) updates.
- Pinning GitHub Actions to commit SHAs (Dependabot's `github-actions` entry keeps tags current; pinning can come later).
- Changing the Copilot review rule or adding required human approvals.

## 7. Risks

- **A security fix that breaks behaviour the tests do not cover** (the `pg` tests, real training runs) merges anyway. Accepted by the user; local runs and the drift log catch it later.
- **A malicious release of a dependency** published as a "security fix" would be auto-merged once tests pass. Mitigated only by GitHub's advisory database being the trigger (security updates come from reviewed advisories, not arbitrary releases).
- **Grouped security pull request:** if one of several fixes breaks CI, all of them wait together.
- **Large install:** the Linux `torch` build makes the first CI run slow (several minutes); the uv cache speeds up later runs.
- **Direct pushes to `main` are blocked** after §4.5; the user's web-editor edits must become pull requests.
