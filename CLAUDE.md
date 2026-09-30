# Episteme — notes for Claude

Medical-LLM data pipeline: acquire open biomedical sources, extract or serialize them into `episteme.articles` (PostgreSQL), build the graph, materialize the training corpus.

## Where things are

- `docs/02-data-sources.md` — source catalog and licence posture (update this first when adding a source)
- `docs/09-extraction-contract.md` — row schema, licence vocabulary, input identity (`input_key`)
- `docs/10-data-sources-runbook.md` — operator runbook; `docs/11-gxp-data-integrity.md` — audit trail; `docs/12-source-inventory.md` — wired sources
- `docs/project-incubation-baseline.md` — drift log: what each sub-project landed and what is still deferred
- `docs/superpowers/specs/`, `docs/superpowers/plans/` — designs and plans per sub-project

## How work is done

- New work: brainstorm → written spec → plan → execute with `superpowers:subagent-driven-development` (always; don't ask which mode) → per-task review → whole-branch review → PR. The user merges PRs.
- Record decisions and deferred findings in the drift log; the SDD ledgers under `.superpowers/` are gitignored and do not travel.

## Where work runs

- **Cloud sandbox:** code and docs work; run only `pytest -m "not pg"`. Do not install or emulate the database here.
- **Local machine only:** anything touching the database or real data — `pg`-marked tests, loads, graph builds, cold-read runs, `01_raw/` and `02_processed/`. Hand these back to a local session.
- Local DB commands: prefix `PGDATABASE=episteme_test` and first confirm `select current_setting('server_version')` is `19beta3`. `pg` tests need `.env` sourced into the shell (`set -a; . ./.env; set +a`) or they skip.

## Hard rules

- Secrets: never read, print, cat, grep or copy `.env`; never run `env | grep` or dump the environment. Docs and reports name env keys only, never values.
- `src/episteme/config.py` is the only module that reads `os.environ`.
- Endpoint URLs live only in `scripts/data/_lib/sources.env`; this must stay empty: `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'`
- Restartability and duplicate detection are a project-wide rule: every extractor/serializer keys its checkpoint marker and stored `source_file` on `input_key(path, raw_dir)`, with a collision test.
- `cdisc_bc` and `cdisc_usdm` stay download-only: no serializer, no corpus inclusion, until CDISC confirms a licence for their data files (`cdisc_bc`: `export/`; `cdisc_usdm`: the USDM model files).
- Git: `git add` explicit paths only; never `--no-verify`; run `ruff format` and `ruff check` on changed Python before committing; never commit `.env`.
