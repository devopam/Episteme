# SP4.1 — Hardening & acquisition follow-ups (design)

**Date:** 2026-09-19 · **Status:** design approved section-by-section in conversation; awaiting written-spec review
**Follows:** SP4 (`2026-09-15-sp4-structured-serializers-design.md`, merged as PR #4)
**Precedes:** SP5 (docs & governance) — deliberately last, so it documents the final behavior once.

---

## 1. Goal and context

SP4 shipped eight structured serializers. Its whole-branch review and drift log deferred a set of
follow-ups, and two additions came from the user on 2026-09-19. This sub-project closes them:

1. A **fail-closed database-mode guard**. During SP4, four separate people/agents — all warned — ran
   commands without `PGDATABASE=episteme_test` and wrote 18 audit rows to the real `episteme` DB
   (`run_pipeline.sh`'s `run_start`/`run_end` bracket and every serializer's `_best_effort_audit` resolve
   the database from the environment default). Discipline alone demonstrably failed.
2. **Identity and idempotency**: `checkpoint_markers` keys by basename, and SP4 shipped two conventions
   for the same problem (openalex fixed it locally; the other seven serializers did not).
3. **Acquisition-wrapper gaps** in three SP3 wrappers, plus one **new source** (CDISC Biomedical
   Concepts) requested 2026-09-19.
4. **Licence mapping**: GO/MONDO's bare CC-BY URLs are not recognised; three government-hosted sources
   are conservatively `open_metadata`.
5. Small correctness items and a Dependabot triage.

## 2. Scope and task layout (13 tasks, 4 phases + close-out)

| Phase | # | Task |
|---|---|---|
| 1 Safety | 1 | DB-mode guard core: `config.py` settings, `dsn_from_settings()` check, `read-only` connection option, `.env`/`.env.example` keys |
| | 2 | Shell-layer integration: `run_pipeline.sh` message, dispatch tests under `restricted` mode |
| 2 Identity | 3 | `checkpoint_markers` keyed by raw-dir-relative path; all eight serializers migrated; openalex's local convention deleted |
| | 4 | `source:file:unknown` id fallback → counted skip (chembl, clinvar, pubchem, reactome, openalex) |
| | 5 | tarfile `data_filter` compatibility (chembl); missing mesh `--report --max-files` test |
| 3 Acquisition | 6 | `download_uniprot.sh` file order |
| | 7 | `download_mesh.sh` real NLM endpoint |
| | 8 | `download_openalex.sh` real bounded `--max-files` |
| | 9 | **new source** `cdisc_bc`: CDISC Biomedical Concepts download wrapper |
| 4 Licences | 10 | `normalize_license` recognises `creativecommons.org` licence URLs |
| | 11 | `public_domain` class for MeSH, PubChem, ClinVar |
| Close-out | 12 | Full sweep + drift-log entry (incl. go-live checklist) |
| | 13 | Dependabot triage (report first; patch-level bumps only if the full suite stays green) |

Execution: subagent-driven-development, per-task review, opus whole-branch review, one fix wave.

## 3. Design

### 3.1 Database-mode guard (tasks 1–2)

Vocabulary deliberately mirrors the existing `MCPG_ACCESS_MODE` (`read-only | restricted | unrestricted`).

**Settings** (`config.py`, which remains the only `os.environ` reader): `db_mode` from
`EPISTEME_DB_MODE`; `production_database` from `EPISTEME_PRODUCTION_DATABASE` (default `episteme`);
`pg_database_secondary` from `PGDATABASE_SECONDARY`; `db_target` from `EPISTEME_DB_TARGET`
(`primary` default | `secondary`). When `db_target=secondary`, `pg_database` resolves to the secondary
name (error if unset). **Code default for `db_mode` when unset is `restricted`** (fail-closed), so CI,
fresh clones and any environment without the user's `.env` are safe.

**Modes:**
- `unrestricted` — no refusal; primary database writable exactly as today.
- `restricted` — any database except `production_database` is allowed; the production database is
  refused. The per-command override is a real-environment prefix (`EPISTEME_DB_MODE=unrestricted …`),
  which beats `.env` under the existing load order (sources.env → .env → real environment).
- `read-only` — any database allowed, but pool connections carry
  `options="-c default_transaction_read_only=on"`, so any write fails at the database.

**Check location:** a pure function `check_database_allowed(settings)` in a new
`episteme/data/db/guard.py`, called first by `dsn_from_settings()`. `get_pool()`, `connection()` and
`corpus_materializer`'s direct DSN use all go through `dsn_from_settings()`, so audit, load, graph and
materialize are covered by one change. On refusal it raises `ProductionDatabaseGuardError` (a
`RuntimeError`) naming the database, stating that no connection was attempted, and giving both exits
(`EPISTEME_DB_MODE=unrestricted` prefix, or `EPISTEME_DB_TARGET=secondary`). One WARNING per process is logged.
The pure function is what unit tests exercise, because `tests/conftest.py` patches
`dsn_from_settings` for every non-`pg` test.

**Caller behavior:** best-effort audit callers already catch connection errors and fall back to the
append-only JSONL mirror (`verify()` only flags a mirror *shorter* than the table, so this creates no new
failure mode). `load`/`graph`/`materialize` fail loudly. `run_pipeline.sh`'s `run_start` warning
("is the DB up?") is reworded to mention the guard.

**`.env` (user-authorized 2026-09-19):** task 1 adds three non-secret lines to the local, gitignored
`.env` — `PGDATABASE_SECONDARY=episteme_test`, `EPISTEME_DB_MODE=unrestricted`, and a commented
`# EPISTEME_DB_TARGET=primary` — leaving every existing value (including all secrets) untouched.
`PGDATABASE=episteme` stays primary. `.env.example` gets the same keys with placeholders. `.env` is never
staged; the executor never prints its values.

**Honest consequence (accepted by the user):** with `unrestricted` in this checkout's `.env`, the guard does
not stop an un-prefixed command from writing to `episteme`. It is a development-phase setting: "we are not
throwing caution to the wind till the development phase is over." What still protects this checkout is
the `tests/conftest.py` guard for non-`pg` tests and the dispatch discipline that agent-run DB commands
use `EPISTEME_DB_TARGET=secondary` (or `PGDATABASE=episteme_test` before task 1 lands).
**Go-live checklist item** (recorded in the drift log, picked up by SP5's runbook): set
`EPISTEME_DB_MODE=restricted` in `.env` before any production operation.

**Not covered, by design:** the `psql`-based DDL scripts (`init_database.sh`, `migrate_database.sh`,
which take explicit `-d` targets) and the `pg`-marked test fixtures (explicit `TEST_PG_DSN`).

### 3.2 Identity and correctness (tasks 3–5)

**Task 3 — `checkpoint_markers`.** The marker/shard/`source_file` key becomes the file's path relative to
the raw dir, POSIX-normalised with `/` → `__`. `list_input_files` dedups by full resolved path and returns
what callers need to derive the same key. Flat layouts produce exactly today's keys (relative path ==
basename), so existing markers, shards and stored `source_file` values stay valid. Nested layouts re-key
once (ClinVar `tab_delimited/…`, ontologies `go/go.obo`, bookshelf's hashed tree, openalex partitions); the
first re-run reprocesses them, and SP4 Task 1's id-collision guard replaces the old rows under the old
names, so no duplicates result. `discover_openalex_files`/`_qualified_source_file` are deleted in favor of
the shared helper (the key format is identical for openalex's real layout), leaving one convention.

**Task 4 — id fallback.** The five serializers that synthesize `f"{SOURCE}:{source_file}:unknown"` when a
record has no native id instead skip the record and count it (per-file stats and `--verbose`). The frozen
4-key return dict `{"inputs","ok","failed","rows"}` is unchanged.

**Task 5 — small items.** `serialize_chembl` uses `tarfile`'s `data_filter` when the interpreter provides it
and otherwise an equivalent member check (reject absolute paths, `..` components, links), so
`requires-python` stays `>=3.10`. Add the missing `test_mesh_report_respects_max_files` (the only serializer
without one).

### 3.3 Acquisition (tasks 6–9)

**Task 6 — uniprot.** Reorder `download_uniprot.sh`'s list so the 94 MB FASTA follows the ancillaries and
precedes the 941 MB XML, making a small `--max-files` useful.

**Task 7 — mesh.** Discover the current real NLM location of the descriptor release (the one SP4 Task 10
fetched by hand). The URL goes into `sources.env` (the grep gate holds); the wrapper resolves the newest
`desc<year>.gz`. Requires real-network discovery at implementation time.

**Task 8 — openalex.** A genuine bounded `--max-files N`: list the prefix with
`aws s3 ls --no-sign-request`, copy the first N objects in a fixed order, and leave the unbounded behavior
unchanged when the flag is absent. `--dry-run` prints the planned N.

**Task 9 — `cdisc_bc` (new source, download-only).** Interpretation of the user's "BioMedical Concepts
acquisition" request: **CDISC Biomedical Concepts and SDTM Dataset Specializations**, published in the
public `cdisc-org/COSMoS` repository (`export/`: CSV and Excel of the latest versions; no API key; content
CC-BY-4.0, code MIT). Roadmap conventions apply: source token `cdisc_bc`, wrapper
`scripts/data/cdisc_bc/download_cdisc_bc.sh`, `WRAPPER` table entry, endpoint URLs only in
`sources.env`, `--dry-run` supported, dispatch tests. Default mode fetches `export/` (listing via GitHub's
contents API); an optional `yaml` mode adds the `yaml/` tree. **Provenance:** the wrapper records the
resolved repository commit SHA, retrieval time and a copy of the licence file alongside the data (SP3's
provenance-only handling, in the same spirit as the volatile group). No serializer in this plan.
*If the user meant a different source (e.g. UMLS concepts — licence-gated, Stream 2 only), only this task
changes.*

### 3.4 Licences (tasks 10–11)

**Task 10 — CC URLs.** `normalize_license` recognises `creativecommons.org/licenses/<code>/<ver>` and
`/publicdomain/zero/` URLs by extracting the code segment and reusing the existing arms (NC-before-BY
ordering preserved). GO and MONDO then resolve to `CC BY` → `commercial`. Existing rows in `episteme_test`
need re-serialization; the real database holds no SP4 rows.

**Task 11 — `public_domain`.** New licence code; `subset_from_license("public_domain") == "commercial"`.
Only the MeSH, PubChem and ClinVar serializers assign it, explicitly and per source (the same shape as
UniProt's `text_mining` override), with a comment citing the governance record below; `license_raw` keeps
the real disclaimer text. `normalize_license` itself never returns `public_domain`, so free text saying
"public domain" stays `unknown`, and a test pins that.

**Governance record (2026-09-19).** The user chose "all three government-hosted sources" after being told
that PubChem and ClinVar carry contributor-submitted content with per-record terms and that this is the
highest-compliance-risk option (the conservative alternatives were CC-URL-fix-only, or CC-URL plus MeSH).
The mechanism is kept source-anchored to bound that risk.

### 3.5 Close-out (tasks 12–13)

Task 12: the full sweep (both suites, dispatch matrix, `bash -n`, grep gate) and a dated drift-log entry
including the go-live checklist item. Task 13: list Dependabot alerts (`gh api`), classify, and apply only
patch-level bumps that keep the full suite green; anything else is reported, not changed.

## 4. Testing and verification

TDD per task. Guard: unit tests on `check_database_allowed` (production name with each mode; secondary
target; custom production name; unset secondary), a `read-only` test that a write fails, and a
`restricted`-mode dispatch test proving `run_pipeline.sh` refuses the production database — safe by
construction, because the guard is what prevents the connection. Identity: a two-directory
same-basename regression for the shared helper, plus a flat-layout key-stability test. Licences:
adversarial `normalize_license` cases (URL embedded in prose, prose "public domain"). Network tests are
dry-run only; no test downloads real data. Each new source/wrapper gets a real bounded end-to-end proof
against `episteme_test` (or download-only proof for `cdisc_bc`) recorded in its task report.

## 5. Non-goals

Streaming shard writes across the serializer family (its own plan); the UCUM parser; full-scale chembl /
pubchem / openalex runs (ops); the `psql` DDL scripts' targeting; a `cdisc_bc` serializer; Stream 2 / RAG.

## 6. Executor constraints (carry into the plan's Global Constraints)

- `.env` holds live secrets: only task 1 edits it, only to add the three lines in §3.1; never print,
  echo or stage it; if it ever appears staged, stop.
- Agent-run DB commands target `episteme_test` (`PGDATABASE=episteme_test`, or
  `EPISTEME_DB_TARGET=secondary` once task 1 lands) — no exceptions, including `download` and `--report`
  invocations, which still open a best-effort audit connection.
- Commit trailer: `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z`.
- `config.py` stays the only env reader; `article_schema.ARTICLE_COLUMNS` is untouched; endpoint URLs
  live only in `scripts/data/_lib/sources.env`.

## 7. Open items (resolved at the owning task)

| # | Item | Resolved in |
|---|---|---|
| 1 | Real current NLM descriptor-release URL and naming | Task 7 |
| 2 | COSMoS `export/` file names, GitHub contents-API rate limits, commit-SHA capture | Task 9 |
| 3 | Deterministic object ordering for openalex bounded fetch (lexicographic vs newest partition first) | Task 8 |
| 4 | Whether the `read-only` connection option interacts with `psycopg_pool` reconnects | Task 1 |
