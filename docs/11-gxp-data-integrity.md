# 11 - GxP data-integrity posture

Scope: the tamper-evident audit trail (`src/episteme/audit_trail.py`,
`episteme._audit`), the operator-identity rules around it, and the database-mode
guard. Every statement below is checked against the implemented code. Where the
2026-09-01 design (section 3.8) promised more than was built, this document says so;
where a gap has since been closed, the row says that instead (see "Design versus
implementation gaps" in section 5). Run-book usage lives in
`docs/10-data-sources-runbook.md`.

## 1. Posture

Episteme is **GxP-ready**, not GxP-compliant. "Ready" means the data layer records
who did what, when, to which object, from which input, in an append-only,
hash-chained, ALCOA+-aligned record that can be independently verified. It does
not mean the system has been validated or that any regulatory claim is made.

Claimed (all verifiable in code):

- Audited data events are `episteme._audit` rows, SHA-256 hash-chained to the previous
  row. Loads, graph builds, corpus materialization and forced reloads write the row in
  the caller's transaction; the per-file extract and serialize events and
  `config_change` write it on their own short connection and commit independently
  (best-effort, see sections 2 and 4).
- The application role has `REVOKE UPDATE, DELETE` on `_audit`, the partitions
  `schema.sql` creates statically, and every partition `ensure_audit_partitions.sh`
  touches (section 5); a partition created by any other route needs the `REVOKE`
  applied by hand.
- A verifier (`episteme.audit_trail.verify`, wrapped by
  `scripts/data/verify_audit_trail.sh`) recomputes hashes and chain links.
- An operator identity (`EPISTEME_ACTOR`) is mandatory for any audit write.

Not claimed: regulatory compliance of any kind; validated status; protection against
a database superuser or the table owner; protection of the JSONL mirror against
deletion or edit (see section 5). Procedural items are listed in section 9.

## 2. Two tiers

| Tier | Component | Mutability | Role |
|---|---|---|---|
| Operational logs | stderr / log output (`log INFO ...` in scripts; Python logging configured in `src/episteme/logging_setup.py`) | mutable | debugging and progress; not the record |
| Audit trail | `episteme._audit` table plus a JSONL mirror | append-only by grant (table); by convention only (mirror) | the electronic record of data events |

The JSONL mirror is written to `<processed_root>/_ops/_audit/audit-YYYYMMDD.jsonl`
(UTC date of the write; `processed_root` defaults to `<data_root>/02_processed`).

- `record()` inserts the DB row first, then appends the same record (plus `record_hash`
  and a `ts` field) to the mirror. The DB insert is in the caller's open transaction
  and `record()` never commits; the caller owns commit and rollback.
- A mirror write failure (`OSError`) is logged and **re-raised** by `record()`. The DB
  row is not rolled back by `record()` itself. That loudness only reaches callers that
  let the exception propagate (loads, graph, corpus, `load_articles`). The extract and
  serialize stages and `config_change` wrap the call in `except Exception`, so for them
  an audit failure (database down, connection refused by the DB-mode guard, unwritable
  mirror) does not stop or fail the stage; see the silent-degradation row in section 5.
- `mirror_only()` is the last-resort fallback, used by the extract and serialize
  stages only when their `record()` attempt fails. It writes a JSONL line only: no DB
  row, no hash chain, a line marked `"chained": false` and `note`
  `db_unavailable_or_failed`. A write failure inside `mirror_only()` itself is logged
  (`_LOG.exception`) and swallowed there -- it is the last-resort fallback, so there is
  nowhere further to escalate to. If the fallback call itself raises (the `record()`
  attempt failed *and* the mirror-only write also failed), the caller's
  `_best_effort_audit` helper now logs that double failure as a
  `_LOG.warning(..., exc_info=True)` instead of silently swallowing it -- it still does
  not raise, so the extract/serialize stage still succeeds (an observability change,
  not a pipeline-behaviour change; see the "No silent drops" row in section 5). Such
  lines are not covered by the tamper-evidence in section 5.

## 3. Audit record schema

`episteme._audit` has 16 columns. Partitioned by `RANGE (recorded_at)`, primary key
`(seq, recorded_at)`.

| Column | Type | ALCOA+ | Notes |
|---|---|---|---|
| `seq` | bigserial | Consistent | DB-assigned; not in the hash |
| `recorded_at` | timestamptz, default `now()` | Contemporaneous | DB-assigned; not in the hash |
| `actor` | text NOT NULL | Attributable | from `EPISTEME_ACTOR`; required, no fallback |
| `host` | text | Attributable | `socket.gethostname()` |
| `pid` | int | Attributable | process id |
| `run_id` | text | Attributable | correlates events of one pipeline run |
| `code_version` | text | Original / Accurate | `git rev-parse --short HEAD`, or `unknown` |
| `event_type` | text NOT NULL | (context) | closed set of 13, section 4 |
| `object` | text | (context) | what was acted on, e.g. `<source> <file>` |
| `input_content_hash` | text | Original | hash of the input, when the caller supplies it |
| `rows_affected` | int | Accurate | counts supplied by the caller |
| `old_value` | jsonb | Original / Accurate | prior value for modifications |
| `new_value` | jsonb | Original / Accurate | new value for modifications |
| `reason` | text | Attributable | free text; see section 6 |
| `prev_hash` | text NOT NULL | (integrity) | previous row's `record_hash` |
| `record_hash` | text NOT NULL | (integrity) | SHA-256 over the hashed fields |

Hash construction:

- Hashed fields, in this order (`_HASHED_FIELDS`): `actor`, `host`, `pid`, `run_id`,
  `code_version`, `event_type`, `object`, `input_content_hash`, `rows_affected`,
  `old_value`, `new_value`, `reason`, `prev_hash`.
- `seq` and `recorded_at` are assigned by the database at insert and are not known
  beforehand, so they are excluded; `record_hash` is the output.
- The digest is SHA-256 of canonical JSON (sorted keys, no whitespace, non-JSON types
  via `str()`).
- The first row uses the genesis `prev_hash` of 64 zeros.
- `record()` takes a transaction-scoped advisory lock (`pg_advisory_xact_lock`) before
  reading the previous hash, so concurrent writers cannot fork the chain.
- Because `seq` and `recorded_at` are outside the hash, they are not tamper-evident
  through the chain alone. Row order is evidenced by the `prev_hash` links.
- Known limitation (in the code comment): hash equality across the jsonb round trip is
  assured for str/int/bool/None payloads; float or Decimal payloads in
  `old_value`/`new_value` can be normalised by Postgres and break verification.

## 4. Event types

`EVENT_TYPES` is a closed set; `record()` and `mirror_only()` raise `ValueError` for
anything else before touching config or the database. "Emitted" means a call site
exists in `src/`.

| Event | Meaning | Emitted by |
|---|---|---|
| `run_start` | a pipeline run began | `run_pipeline.sh` (via `python -m episteme.audit_trail record`); `load_articles` when it has no external run id |
| `run_end` | a pipeline run ended (also on stage failure, reason `failed at stage ...`) | same as above |
| `extract_commit` | an extract output file was committed | the extractors' `_best_effort_audit`: `record()` on a fresh connection, committed immediately (chained DB row); `mirror_only` only as a fallback if that fails |
| `serialize_commit` | a structured-source serializer output was committed; added after the original design, which had no such type | the serializers' `_best_effort_audit`: `record()` on a fresh connection, committed immediately (chained DB row); `mirror_only` only as a fallback if that fails |
| `load_commit` | a shard was loaded with nothing replaced | `postgres_loader` (DB row) |
| `load_replace` | a shard load replaced existing rows (delete count recorded) | `postgres_loader` (DB row) |
| `graph_commit` | graph build output for a source file | `graph_builder` (DB row) |
| `corpus_materialize` | corpus materialization | `corpus_materializer` (DB row) |
| `schema_migration` | a schema migration was applied | **no call site**; reserved (only reachable via `python -m episteme.audit_trail record schema_migration --reason ...`; `--reason` is required, section 6) |
| `force_override` | an operator ran `load_articles --force --reason` | `load_articles` (DB row, with `reason`); emitted for every shard iterated, including shards that have no success marker, so rows are not 1:1 with bypassed markers |
| `integrity_check` | an integrity check ran | **no call site**; reserved |
| `manual_correction` | a manual data correction | **no call site**; reserved (only reachable via `python -m episteme.audit_trail record manual_correction --reason ...`; `--reason` is required, section 6) |
| `config_change` | a configuration change | `enrich_openmetadata` (DB row on its own connection; best-effort, a failure only prints a stderr warning) |

Consequence: in a normal run with a reachable database, `extract_commit` and
`serialize_commit` are chained DB rows (plus mirror lines). They are best-effort: when
the audit attempt fails, the event survives only as an unchained JSONL line (or, if
that also fails, not at all), and the stage still succeeds. Whether a given run's
extract/serialize events are chained rows can be seen from the mirror: fallback lines
carry `"chained": false`.

## 5. Integrity

Hash chain and verification:

- `episteme.audit_trail.verify(conn)` walks the table by `seq`. For each row it
  recomputes `record_hash` (problem `hash_mismatch`) and checks `prev_hash` equals
  the previous stored `record_hash` (problem `chain_break`).
- It then compares the count of non-blank lines in all `audit-*.jsonl` files, plus
  every `audit-*.jsonl.gz` file (opened with `gzip.open(..., "rt")`, so a file rotated
  by `scripts/data/rotate_audit_logs.sh` still counts), against the table row count;
  fewer mirror lines than rows gives a `mirror_short` problem (`seq` is `None`). More
  mirror lines than rows is treated as legitimate (for example after a schema
  recreate).
- The check is a line count, not a per-line content comparison of the mirror against
  the table. Lines from `mirror_only()` count towards the total, so they can mask
  missing chained lines.
- `scripts/data/verify_audit_trail.sh` requires `EPISTEME_ACTOR`, finds the venv
  Python, calls `verify`, and prints `audit chain OK` or `CHAIN BROKEN at seq [...]`
  (a `mirror_short` problem prints `[None]`). Exit codes: `0` chain intact; `1` any
  problem, or an unhandled error such as an unreachable database; `2` a missing
  required environment variable (from `require_env`).

Append-only enforcement:

- `schema.sql` grants `episteme_app` full DML on all tables, then runs
  `REVOKE UPDATE, DELETE` on `episteme._audit`, `_audit_202609`, `_audit_202610`
  and `_audit_default`. The application role can therefore only INSERT and SELECT.
- The revoke in `schema.sql` is per partition and static: a partition created later
  does not appear in that file, and its privileges follow the default-privileges rule
  there (full DML to `episteme_app`), so the revoke must be repeated for any new
  partition. `scripts/data/db/ensure_audit_partitions.sh` does this automatically for
  every partition it creates or otherwise touches (section 5); a partition created by
  any other route (a manual `CREATE TABLE ... PARTITION OF`) still needs the `REVOKE`
  applied by hand.
- The table owner (`episteme_sys_admin`) and superusers are not restricted by this.

Design versus implementation gaps (the design text is section 3.8 of
`docs/superpowers/specs/2026-09-01-data-taxonomy-and-postgres-restructure-design.md`;
the code wins):

| Design statement | Actual state |
|---|---|
| JSONL mirror is append-only via `chattr +a` | **Partly.** `scripts/data/rotate_audit_logs.sh` best-effort `chattr +a`'s the current day's still-open mirror file (`2>/dev/null \|\| true`, so a failure is silent) when `chattr` exists -- Linux only, and only on filesystems that support the attribute. It is a no-op on Windows/macOS/non-ext filesystems, and there is no per-write enforcement: an operator (or a scheduler) must invoke the script. |
| `scripts/data/rotate_audit_logs.sh` gzips mirror files older than 30 days | **Implemented.** The script gzips `audit-*.jsonl` files matched by `find ... -mtime +30` in place; `-mtime`'s floor comparison means a file becomes eligible only once it is at least 31 days old, not exactly 30 (errs safe). It is idempotent (an already-`.jsonl.gz` file is left alone). Before gzipping each file it best-effort `chattr -a`'s it first, undoing the append-only bit a previous day's `chattr +a` (below) would otherwise have set -- without that clear, gzip's unlink-and-replace would fail forever on a 30-day-old append-only file. A per-file gzip failure is logged as a WARNING and the loop continues to the next file rather than aborting the run. `verify()`'s mirror-parity glob matches both `audit-*.jsonl` and `audit-*.jsonl.gz` (opening the latter with `gzip.open(..., "rt")`), so a rotated file still counts toward `mirror_lines`. Not scheduled automatically -- an operator or cron/Task Scheduler must run it. |
| Monthly partitions created by a rotation script / `create_audit_partition()` | **Implemented,** as a rotation script (not a `create_audit_partition()` function -- the design text's name for it). `scripts/data/db/ensure_audit_partitions.sh [target_db] [months_ahead]` creates `episteme._audit_YYYYMM` partitions for a rolling window of `months_ahead` calendar months starting this month (default `6`). It is idempotent (`CREATE TABLE IF NOT EXISTS`) and re-applies `REVOKE UPDATE, DELETE ... FROM episteme_app` on every partition it touches that run, not only newly created ones; both behaviours are covered by `tests/data/test_ensure_audit_partitions.py` (pg-marked). It is not invoked automatically by any pipeline stage or scheduler in this repo -- an operator or external scheduler must run it periodically (go-live checklist, section 8). Any month whose partition was never created this way (for example because the script was never run within its rolling window) still lands in `_audit_default`, same as before Task 2. `schema.sql`'s own comment near the `_audit` table (SP6 close-out item 6) now names `ensure_audit_partitions.sh` directly instead of saying a rotation script "is not yet built". |
| Verifier walks the chain across both table and mirror and reports the first divergence | **Partly.** The chain is verified on the table only; the mirror is checked by line count only (both `.jsonl` and `.jsonl.gz`, above). It reports all problems, not just the first. |
| `reason` is required for `force_override`, `manual_correction`, `schema_migration` | **Partly.** See section 6. `record()` now enforces a non-empty `reason` for `manual_correction` and `schema_migration` -- it raises `ValueError` when `reason` is `None` or empty, checked right after the closed-set `event_type` check and before `require_actor()` or any database work. That covers every caller of `record()`, including the `python -m episteme.audit_trail record` CLI. `force_override`'s reason is still enforced only at the CLI layer (`load_articles --force`); `record()` itself accepts `force_override` with `reason=None` without raising. |
| Event set of 12 types | The code has 13 (adds `serialize_commit`). Three types have no emitter (section 4). |
| No silent drops: every event produces an audit record | **Not guaranteed,** but a double failure is no longer silent. The extract and serialize stages and `enrich_openmetadata` treat auditing as best-effort: an audit failure does not stop or fail the stage. When `record()` fails, the extract/serialize `_best_effort_audit` helpers fall back to `mirror_only()`, writing an unchained JSONL line with `note: db_unavailable_or_failed`; when that fallback call *also* raises, `_best_effort_audit` now logs the double failure with `_LOG.warning(..., exc_info=True)` instead of swallowing it silently -- still non-fatal to the pipeline (the stage still succeeds; this changes observability only, not ingestion behaviour). `run_pipeline.sh` likewise continues past a failed `run_start`/`run_end` (section 6). |

## 6. Actor and reason rules

Actor:

- `record()` calls `require_actor()` (`episteme.config`), which raises `ConfigError`
  when `EPISTEME_ACTOR` is unset or empty. There is no fallback value. The check
  happens after the event-type check and before any database work.
- Shell entry points (`run_pipeline.sh`, `verify_audit_trail.sh`, every stage wrapper under `scripts/data/<source>/`; the `db/*.sh` and `_lib` scripts are not covered)
  call `require_env EPISTEME_ACTOR` first and exit with code 2 when it is missing.
- The value is whatever the operator sets. The code does not verify it against any
  identity system (see section 9).

Reason:

- `run_pipeline.sh --force` without a non-empty `--reason` exits 2 with
  `--force requires --reason`, before any DB work.
- `load_articles --force` without `--reason` exits 2 (`error: --force requires
  --reason`), and with both it records a `force_override` audit row, carrying the
  reason, for every shard iterated in that run. The emit condition is `--force` plus
  `--reason`, not an actual success-marker bypass, so shards with no marker also get a
  row.
- For `manual_correction` and `schema_migration`, `record()` itself now requires a
  non-empty `reason`: it raises `ValueError` when `reason` is `None` or empty, checked
  right after the closed-set `event_type` check and before `require_actor()`, config
  resolution, or any database work. This applies to every caller of `record()`,
  including the `python -m episteme.audit_trail record` CLI (`--reason` is still an
  optional argument there, but omitting it for these two event types now raises).
  Neither event has a production emitter in `src/` (section 4); the CLI is the only
  way to write one today. `force_override`'s reason is still enforced only in
  `load_articles` (the CLI argument parser, above), not inside `record()` itself --
  `record()` accepts `force_override` with `reason=None` without raising.
- `run_pipeline.sh` also passes `--reason` on the failure `run_end` record.

Degraded operation to be aware of: `run_pipeline.sh` treats a failed `run_start` or
`run_end` audit call as a warning ("proceeding unaudited"), for example when the
database is unreachable or refused by the guard. A run can therefore complete with
its bracket missing from `_audit`. Stages that write DB audit rows in their own
transaction (loads, graph, materialize) fail on their own if the DB is unavailable.

## 7. Database-mode guard

`episteme.data.db.guard.check_database_allowed` runs when a DSN is built. It reads
two settings:

- `EPISTEME_DB_MODE`: `read-only`, `restricted` or `unrestricted`. The code default
  when the variable is unset is `restricted`. An invalid value raises `ConfigError`.
  - `read-only`: connections are opened with `default_transaction_read_only=on`.
  - `restricted`: refuses to connect when the target database name equals the
    production database name (`EPISTEME_PRODUCTION_DATABASE`, default `episteme`).
  - `unrestricted`: no refusal.
- `EPISTEME_DB_TARGET`: `primary` (uses `PGDATABASE`, default `episteme`) or
  `secondary` (uses `PGDATABASE_SECONDARY`, which must then be set).

What it does and does not check:

- It compares the **database name** only. It does not check the server host or
  identity: a database that happens to be named differently on the production server,
  or a production-named database on another server, is judged by name alone.
- It applies to Python connections built through the settings DSN. It does not cover
  raw `psql` DDL scripts (`scripts/data/db/*.sh`) or any other tool that connects
  directly.
- It is a safety rail against accidental operation, not an access control. Anyone who
  can set `EPISTEME_DB_MODE=unrestricted` (or edit `.env`) removes it.
- Process control (not enforced by code): confirm the target server and version
  before any production operation.

## 8. Go-live checklist

1. Set `EPISTEME_DB_MODE=restricted` in the production environment (the `.env`
   template already ships it; the code default is also `restricted`). Development
   convenience settings such as `unrestricted` must not be carried over.
2. Review the `.env` file on the host: `EPISTEME_ACTOR` convention, `PGDATABASE`,
   `EPISTEME_PRODUCTION_DATABASE`, `EPISTEME_DB_TARGET`. Do not copy it into tickets
   or logs; it holds credentials.
3. Run `scripts/data/verify_audit_trail.sh` and confirm `audit chain OK` (exit 0).
4. Run `scripts/data/db/ensure_audit_partitions.sh [target_db] [months_ahead]`
   (default `months_ahead` is `6`) to ensure monthly `_audit` partitions exist for the
   current and coming months, with the matching `REVOKE UPDATE, DELETE ... FROM
   episteme_app` re-applied on every partition it touches. It connects as
   `episteme_sys_admin`, not the application role, and needs `PGHOST`, `PGPORT` and
   `EPISTEME_SYS_ADMIN_PASSWORD` set (`target_db` defaults to
   `${PGDATABASE:-episteme}`). It is idempotent and safe to run repeatedly, but this
   repo does not schedule it automatically -- an operator or external scheduler
   (cron/Task Scheduler) must invoke it periodically.
5. Confirm the mirror directory (`<processed_root>/_ops/_audit/`) is on retained
   storage, backed up, and permission-restricted. Schedule
   `scripts/data/rotate_audit_logs.sh [ops_dir]` periodically (for example daily from
   cron): it gzips mirror files older than ~30 days in place and best-effort
   `chattr +a`'s the current day's open mirror file. That `chattr +a` only takes effect
   on Linux with a filesystem that supports the attribute, fails silently by design
   when it can't be applied, and is not a per-write enforcement (it is a no-op on
   Windows/macOS); decide whether to apply any further host-level append-only
   protection beyond that best-effort mechanism.
6. Confirm the server identity and version for the target database by hand.

## 9. Out of scope and procedural

The following are not provided by this codebase and must be handled by the
organisation before any regulated use:

- Computer system validation (CSV): risk assessment, IQ/OQ/PQ, traceability,
  validation reports.
- Role-based access control and account management beyond the two database roles
  above; verification of the `EPISTEME_ACTOR` value.
- Electronic signatures and their binding to records.
- Periodic review of the audit trail, and handling of a failed verification.
- SOPs: change control, backup and restore, incident handling, data retention.
- Protection of the JSONL mirror and of database owner/superuser activity.
