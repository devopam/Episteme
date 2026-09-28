# SP5 — Docs & Governance: design

**Date:** 2026-09-21
**Status:** Draft for user review
**Charter:** `docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md` §SP5 (this spec refines it against the tree as shipped by SP1–SP4.1, `main`@`a899074`).
**Depends on:** SP1–SP4.1 merged. ADR-0001/0002 already landed.

## 1. Goal

Make the documentation match the shipped tree, add the governance document (GxP posture) and a living source inventory, and prove the runbook works from a cold read. No pipeline behaviour changes; the only code added is a read-only generator script and consistency tests.

**Exit criteria (from the charter, made checkable):**
1. Docs 07/08/09/10 describe the shipped tree (Postgres hybrid, serializers, DB-mode guard, all wired sources).
2. `docs/11-gxp-data-integrity.md` exists and every technical claim in it is backed by code or a test (no compliance claimed: "GxP-ready", never "GxP-compliant").
3. The source inventory lists every source in `run_pipeline.sh`'s `WRAPPER` table, pinned by a test.
4. A fresh agent following `docs/10` alone completes one small source (download → serialize → load) against `episteme_test`.
5. No dangling doc links (`docs/02` → `11-data-roadmap.md` resolved).

## 2. Decisions already made (do not re-ask)

- One phased plan (not separate plans per doc).
- Inventory = a static table committed in docs, pinned to the pipeline's source list by a test, plus a read-only generator script for machine-local columns (last sync, row counts).
- Verification = docs-vs-code consistency tests plus one cold-read run (a fresh agent follows `docs/10` alone for one small source against `episteme_test`).
- Agent-run DB commands target `episteme_test` regardless of the user's `.env` mode.
- `docs/11` is reserved for the GxP document; `docs/02`'s link to a nonexistent `11-data-roadmap.md` is reconciled.

## 3. Deliverables

### 3.1 Source inventory — `docs/12-source-inventory.md` + `scripts/data/source_inventory.sh`
- Static table, one row per `WRAPPER` key (23 today, incl. `cdisc_bc`): source, class (literature / EPMC support / structured / volatile / acquisition), stages wired (download / extract / serialize / load / graph), licence class and provenance of that claim (**including cdisc_bc: UNVERIFIED**, and PubChem/ClinVar `public_domain` as a governance override), cadence, script path.
- `source_inventory.sh` (read-only; never writes, never touches `.env` values): prints the machine-local columns (last sync stamp from `01_raw/<source>`, row count per `source` from `episteme.articles` via `PGDATABASE` as given). Doc says to run it against `episteme_test` when scripted by an agent.
- Test: the set of sources in the table equals the keys of `WRAPPER` parsed from `run_pipeline.sh`; every script path in the table exists; every source's `sources.env` endpoint keys are referenced.

### 3.2 `docs/11-gxp-data-integrity.md` (new)
ALCOA+ posture, audit-record schema, hash-chain scheme, retention, and the automated-vs-procedural boundary, written from the **implemented** `audit_trail` / `episteme._audit` / `verify_audit_trail.sh` (not from the 2026-09-01 design), so drift between design and code is resolved in the doc's favour of the code. Explicitly covers: the DB-mode guard and its limit (checks the database *name* only, not the server), `EPISTEME_ACTOR` required, fail-closed audit, e-signatures out of scope, and the go-live checklist (`EPISTEME_DB_MODE=restricted`).
Test: event types and audit columns named in the doc exist in code/schema.

### 3.3 Rewrites of `docs/07`, `docs/08`, `docs/09`
- `docs/07`: graph → Postgres property tables + committed SQL/PGQ; keep the "structured published metadata over LLM-extracted triples" thesis.
- `docs/08`: Postgres hybrid storage; the Iceberg/OpenMetadata clauses are marked superseded by ADR-0001 (as the ADR already states).
- `docs/09`: §4 storage → Postgres tables + idempotent delete-by-`source_file`; the structured-source serialisation addendum; the finalised `article_schema` table; **the licence vocabulary (~line 124) and §6 licence→subset table (~211–218) gain `public_domain` (source-anchored governance override, never returned by `normalize_license`) and GO/MONDO resolving to `CC BY` → `commercial`**; the shared input-identity rule (`input_key`).
- Test: the licence vocabulary in `docs/09` equals the codes in `article_schema` (constants + `subset_from_license` tuples).

### 3.4 `docs/10` runbook rewrite
Single operator runbook: DB setup (`init_database.sh`, `PG*`), DB-mode guard (modes, targets, `.env` keys, warning that agents/CI use `episteme_test`), then per source a first-time block and an incremental block using real paths and `run_pipeline.sh`, then materialize and ingest. Keep the known-failure tables and ops cadence. Includes `cdisc_bc` (download-only) and the openalex `--max-files`. The status board is replaced by a pointer to `docs/12`.
Test: every `run_pipeline.sh <source> <stage>` line in the doc is accepted by the dispatcher in `--dry-run`/rc-contract terms (download → rc 0; unwired stage → rc 3), and every path mentioned exists.

### 3.5 Reconciliations
- `docs/02`: replace the `11-data-roadmap.md` references (lines ~5, ~223) with pointers to `docs/12` and the roadmap spec; sweep other docs for dangling links (test: all relative `docs/*.md` links resolve).
- `README.md`: fix stale `train_cpt` invocations if still present; point the data section at `docs/10` and `docs/12`.
- `docs/project-incubation-baseline.md`: one dated SP5 drift entry.

### 3.6 Verification
- Consistency tests as named above, in `tests/docs/` (non-pg, fast).
- Cold-read run: a fresh subagent receives only `docs/10` (and the repo) and runs one small source end to end against `episteme_test` (candidate: `cdisc_bc download --max-files 2`, then a serialize-capable small source such as `mesh` bounded). Failures become doc fixes; the transcript is stored in the SDD workspace.

## 4. Out of scope
Code behaviour changes; new sources; SP2-extractor identity migration; `uv.lock` refresh; Dependabot (pyeuropepmc upgrade); pg-only features; Stream 2/RAG design; e-signatures.

## 5. Risks
- **Doc–code drift while writing:** mitigated by tests that parse code (WRAPPER table, licence codes, audit columns).
- **Over-claiming compliance/licences:** the doc states GxP-*ready*, cdisc_bc licence UNVERIFIED, `public_domain` as governance decision 2026-09-19 with the contributor-content caveat.
- **Cold-read run touching the wrong DB:** `PGDATABASE=episteme_test`, server identity check (`19beta3`) first.

## 6. Phasing (for the plan)
A. Inventory + generator + pinning test. B. `docs/11` + tests. C. `docs/09`, `docs/08`, `docs/07`. D. `docs/10`. E. Reconciliations + README + drift entry. F. Consistency sweep, cold-read run, fixes, close-out.
