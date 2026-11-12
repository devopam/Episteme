# Load follow-ups: OpenAlex reverse sweep, persistent EMA pacing, tuning values in config

Date: 2026-11-12. Owner decisions in this document: the reverse sweep runs automatically per shard (owner, 2026-11-12); one bundled PR for the three items (owner rule: no piecemeal PRs). Everything marked "Claude's call" may be changed by the owner at review.

## 1. Why

- **Reverse sweep.** The OpenAlex load skips a work that `pubmed` or `pmc` already holds (PR 109, PR 110). The check runs only when the OpenAlex shard loads. A `pubmed` or `pmc` shard loaded later can add a work that `articles_openalex` already holds, so the database holds that work twice. (Drift log, 2026-11-11 OpenAlex entry, deferred item (a).)
- **EMA pacing.** `reference_docs/acquire.py` (`fetch`, `state["last"]`) and `regulatory/download_ema.py` (`requests_made` and `delay`) keep the time of the last request in memory only. A new run (resume after stop, the next chunk of a queue) sends its first request at once. EMA answered HTTP 429 when a new chunk started 14 s after the last request of the previous chunk (2026-11-11).
- **Tuning values in code.** Owner rule (memory "params in config"): batch sizes, part sizes, slice caps, timeouts and memory limits belong in `episteme.config`, folded into the next real change. Most already are; section 4 lists the rest.

## 2. Reverse sweep (owner decision: automatic per shard)

### Behaviour

- Sources that sweep: `pubmed` and `pmc` (a module constant `SWEEP_INTO: dict[str, str] = {"pubmed": "openalex", "pmc": "openalex"}` in `postgres_loader.py`, the mirror of `DEDUP_AGAINST`).
- In `load_source_file`, for a sweeping source, after the shard's rows are inserted and in the same transaction: delete every `openalex` row (from `article_body` first, then `articles`, both restricted to `source = 'openalex'`) whose
  - `pmid` equals a `pmid` of the shard's inserted rows, or
  - `pmcid` equals a `pmcid` of the shard's inserted rows, or
  - `doi` equals `lower(doi)` of the shard's inserted rows.
- Keys come from the rows the shard just inserted (the shard's `source_file` values), not from the whole `pubmed`/`pmc` partition.
- Same probe pattern as PR 110: sorted, distinct key tables in `pg_temp`, one `CROSS JOIN LATERAL (... LIMIT ...)` probe per key into the `articles_openalex` indexes. Collect the matched openalex `id`s into a temp table, then delete by `id`. A key can match more than one openalex row (two works with one DOI): the probe must return every match, so it is `LATERAL` without `LIMIT 1` (Claude's call: a `LIMIT 1` would leave the second duplicate).
- Index use: `articles_openalex_pmid_idx`, `articles_openalex_pmcid_idx` and `articles_openalex_doi_idx` (all exist since migration 0012, inherited from the parent indexes). No migration (Claude's call; reason below).
- Counts: `swept_openalex` (rows removed from `articles`). It goes into the result dict, the `ok` line, the load marker stats, the audit `load_commit` `new_value`, and `_lineage.dedup_counts` (jsonb, migration 0012) of the `pubmed`/`pmc` shard. When the count is 0, the key is still written (0).
- When no `openalex` row exists at all (a fresh database), the sweep still runs and costs about nothing; it is not skipped by a flag.
- Readiness: a sweeping load refuses the shard (RuntimeError, before any work, like `_check_dedup_ready`) when `episteme.articles_openalex` or one of the three indexes is missing (a `--bulk` drop or no 0012). `run_pipeline.sh pubmed load --bulk` and `pmc load --bulk` keep working only if the bulk group does not drop those three partition indexes; the implementer checks `scripts/data/db/bulk_load_indexes.sh` and documents the outcome (refuse with a clear message is acceptable).
- The openalex shards' own `_lineage.rows_inserted` stays the load-time count (documented, not rewritten).

### Why no `lower(doi)` index on `articles_openalex`

OpenAlex stores DOIs in lower case: on 2026-11-12, 0 of 2,274,251 loaded `articles_openalex` DOIs differ from `lower(doi)`. The sweep compares `openalex.doi = lower(<pubmed or pmc doi>)` through the existing plain `doi` index. To keep this true for later snapshots, `serialize_openalex._bare_doi` returns the DOI in lower case. Shards already staged are lower case and are not re-serialized. The runbook's post-load checks get one query: `SELECT count(*) FROM episteme.articles_openalex WHERE doi <> lower(doi)` must be 0.

### Tests

- pg: a pubmed shard whose pmid, pmcid or DOI (mixed case) matches loaded openalex rows removes exactly those rows from `articles` and `article_body`, leaves other openalex rows, and reports `swept_openalex`; a pubmed shard with no match reports 0; two openalex rows with one DOI are both removed; a `pmc` shard sweeps too; a non-sweeping source (e.g. `dailymed`) writes no `swept_openalex`.
- pg plan test: the sweep probes use the three openalex indexes, no Seq Scan / Bitmap / Sort / Unique / Hash on `articles_openalex`.
- non-pg: SQL shape of the sweep probe (LATERAL, no `LIMIT 1`, source literals validated as in `_dedup_probe_sql`); `_bare_doi` lower-cases.

## 3. Persistent EMA pacing

- Both downloaders keep the wall-clock time (UTC, ISO 8601) of their last request to a host in a small state file, `<raw_dir>/_pacing/<host>.json` (`{"host": ..., "last_request_at": ...}`), written after every request (success or failure, including 429). Claude's call: one file per host, so `acquire.py` and `download_ema.py` share the EMA spacing.
- At the start of a run, and before each request, the wait is `max(0, min_interval - (now - last_request_at))` with the larger of the in-memory and the on-disk time. A `Retry-After` wait also updates the file to `now + Retry-After - min_interval` so the next run honours it (Claude's call).
- Wall clock for the file, monotonic clock in memory as now; both injectable for tests. A future time in the file (clock change) is capped at `min_interval` of wait.
- A missing, empty or unreadable state file means "no wait" and a warning; it never stops the run.
- `_pacing/` starts with `_`, so discovery skips it (PR 108 rule). It is not in any manifest.
- Tests (non-pg): a second `fetch` run right after a first one waits the remainder of the interval; a state file 10 min old means no wait; a corrupt file warns and does not wait; `download_ema` does the same with `ema_delay_seconds`.

## 4. Tuning values into config

Move to `config.py` Settings (env key `EPISTEME_<NAME>`, default = today's value, the same pattern as `DEFAULT_FAERS_BRIDGE_BATCH_ROWS`), read through `get_settings()`:

| Module | Constant | Default |
|---|---|---|
| `data/dailymed/labels.py` | `_COPY_BATCH_ROWS` | 20,000 |
| `data/pmc/download_pmc.py` | `SUBMIT_BATCH` | 2,000 |
| `sft/slice.py` | `_BATCH_ROWS`, `_ROWS_PER_PART` | 2,048, 50,000 |
| `data/reference_docs/acquire.py` | `_CONNECT_TIMEOUT_S`, `_READ_TIMEOUT_S`, `MAX_UNPACKED_BYTES`, `DEFAULT_MAX_WAIT_S`, `_FALLBACK_THROTTLE_WAIT_S` | 30, 120, 2 GiB, 900, 30 |
| `data/regulatory/download_ema.py` | `_CONNECT_TIMEOUT_S` | 30 |
| `ops/gate_slice.py` | `BOOKSHELF_MAX_PACKAGE_BYTES`, `APOLLO_MIN_BYTES` | 1 GiB, 10 MiB |

Out of scope (format or protocol limits, not tuning): `MAX_LIST_ELEMENT_BYTES`, `BTREE_LIMIT_BYTES`, `MAX_INDEX_TEXT_BYTES`, `_APP_NAME_MAX`, `_SAMPLE_MAX`, poll and sleep-slice intervals, `_HASH_CHUNK_BYTES`.

- `config.py` stays the only module that reads `os.environ`.
- Hash rule (memory "params in config"): a tuning value must not enter a stage's code hash; if a stage writes a run record, the value goes there. The implementer checks `gate_slice.py` values against the gate hash closure and states the result.
- Tests: each new setting has a default test and an env-override test (existing config test pattern).

## 5. Docs

- `docs/10` section 5.2: the post-load DOI case check; a short note that `pubmed`/`pmc` loads sweep `openalex` and print `swept_openalex`.
- `docs/10` reference-docs and EMA sections: `_pacing/` state files.
- `docs/09` if it lists result/marker keys: `swept_openalex`.
- `.env.example` (if present): the new keys, names only.
- Drift log entry with the decisions above.

## 6. Global constraints

- Run only `pytest -m "not pg"` in implementer subagents; the controller runs pg tests on `episteme_test`. No real data on D: by implementers. A full OpenAlex load is running on `episteme` from the main checkout: implementers never touch `episteme`.
- `ruff format`, `ruff check`; the endpoint-URL guard prints nothing.
- No new dependency.

## 7. Left to the builder

Names of private helpers, test file names, the exact warning text, and where the sweep sits in `load_source_file` (after both inserts, before the lineage write).
