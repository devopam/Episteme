# Question to Standard MeSH Terms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A PV reviewer's question in their own words becomes a list of standard MeSH descriptors (exact entry-term matches first, MedCPT suggestions second); the person confirms the terms; the exact searches run only on confirmed terms and state the terms and query text they used.

**Architecture:** Migration 0018 adds `mesh_term` (names and entry terms) and `mesh_term_vec` (MedCPT vectors, pgvector `halfvec` HNSW), filled by the `mesh` graph phase and a new `mesh embed` stage, plus SQL functions `match_terms`, `suggest_terms` and `search_terms` in `episteme_api`. A new Episteme MCP server (`episteme.reference.question_mcp`, stdio) embeds the question, asks the person to confirm through MCP elicitation, runs the searches as `episteme_api_reader` and writes a JSON-lines log.

**Tech Stack:** PostgreSQL 19.0, pgvector 0.8.7, plpgsql; Python 3, psycopg 3, torch + transformers (existing), `mcp` Python SDK (new); bash pipeline wrappers.

**Spec:** `docs/superpowers/specs/2026-11-24-question-to-mesh-terms-design.md`

## Global Constraints

- Server: PostgreSQL 19.0 on `localhost:5433` (upgraded from 19beta3 on 2026-11-24). Version guards accept any `19.x` (and still `19beta3`), nothing else.
- Every SQL function: owned by `episteme_api_owner`, `SECURITY DEFINER` when it reads a table, `STABLE`, `SET search_path = pg_catalog, pg_temp`, schema-qualified bodies, no writes. Public wrappers in `episteme_api` cut at the cap of their family and add the `truncated:` note (0015 rule).
- `src/episteme/config.py` is the only `os.environ` reader. New API families go in `API_RESULT_LIMITS` and reach the database through `python -m episteme.reference.api_settings sync`.
- New families (exact): `"match_ngram_max": (8, 8)`, `"suggest": (10, 50)`, `"suggest_ef_search": (200, 200)`.
- Models (exact): query `ncbi/MedCPT-Query-Encoder` revision `d83a36cc6b8e3a5c5e9d9d6ba156808c1643dcbc`; article `ncbi/MedCPT-Article-Encoder` revision `d05a736da4bb84ee4057b7f7999485be6ed85465`. 768 dims, inner product, CLS pooling (`last_hidden_state[:, 0, :]`). Query max length 64 tokens, article max length 512.
- `mesh_release` = the 4-digit year in the descriptor file name (`desc2026.gz` -> `'2026'`).
- `term_norm(t)`: lower case; every character that is not a letter or a digit becomes a space; runs of spaces collapse to one; trimmed. SQL (`episteme_api_internal.term_norm`, `IMMUTABLE`) and Python (`episteme.reference.term_norm.term_norm`) give identical output.
- Note texts (exact): `unmatched: <words>`; `terms used: <UI> <name>; query: <tsquery text>`; `capped: first <n> matches by article_id of <source> for <UI>`; `not run: time budget of <n> ms spent before <UI> / <source> finished; search with fewer terms`; `model revision <r> does not match the stored vectors (<s>)`; `vocabulary vectors are from MeSH <a>, terms are from MeSH <b>; run mesh embed`.
- Git: explicit `git add` paths; `ruff format` and `ruff check` on changed Python; never `--no-verify`; never read or print `.env`.
- Implementers have no database: they write `pg`-marked tests but do not run them; the controller runs them on `episteme_test`.
- Endpoint URLs only in `scripts/data/_lib/sources.env` (the Hugging Face model ids are not URLs; the download step uses `huggingface_hub` by id).

## Review Focus

1. A question with no matchable words at all (only stop words, digits, punctuation): `match_terms` returns only the `unmatched:` note (or an empty-question note), never an error; the MCP tool then suggests. Test in Task 6.
2. The same term matching twice ("SJS ... Stevens-Johnson syndrome") or two descriptors sharing an entry term: one row per descriptor, words consumed once. Test in Task 6.
3. A vector from the wrong model revision or a stale vocabulary (terms from a newer release than the vectors): `suggest_terms` refuses with the exact note, no rows. Test in Task 6.
4. A common descriptor whose OR'ed entry terms match millions of rows: `search_terms` returns the capped rows in `article_id` order and the same rows on a second call, or the `not run:` note — never a partial row set. Test in Task 7.
5. The person declines, cancels, or the client lacks elicitation; or the agent calls `search_confirmed` with a stale `confirmation_id`: no search runs, the log records it. Test in Task 8.

---

### Task 1: PostgreSQL 19.x version guards and the ride-along docs

**Files:**
- Modify: `src/episteme/data/db/text_override.py:14,33,51-60` (guard), `src/episteme/data/db/fts_index.py:25` (docstring), `scripts/data/db/text_override.sh:10`, `scripts/data/db/fts_index.sh:14` (comments)
- Modify: `CLAUDE.md:23`, `docs/10-data-sources-runbook.md` (lines 25, 40, 81, 163, 181, 227, 1822, 1853 and step 13.4 of section 5.19.1), `docs/project-incubation-baseline.md` (drift log)
- Test: `tests/data/test_text_override_tool.py`

**Interfaces:**
- Produces: `episteme.data.db.text_override.is_supported_server_version(version: str) -> bool`; `check_server_version(conn)` uses it.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize("v,ok", [("19beta3", True), ("19.0", True), ("19.2", True),
    ("19rc1", True), ("18.3", False), ("190.1", False), ("20.0", False), ("", False)])
def test_supported_server_version(v, ok):
    assert text_override.is_supported_server_version(v) is ok
```

- [ ] **Step 2: Run it: `pytest tests/data/test_text_override_tool.py -k supported_server_version -v`** — expected FAIL (no attribute).
- [ ] **Step 3: Implement** `is_supported_server_version` with `re.fullmatch(r"19(\.\d+|beta\d+|rc\d+)?", v)`; `check_server_version` raises with `"this tool runs only on PostgreSQL 19"`. Replace `REQUIRED_SERVER_VERSION`. Update the docstrings/comments ("refuses any server except PostgreSQL 19").
- [ ] **Step 4: Docs.** Runbook and CLAUDE.md: the expected check output becomes `episteme_test|19.0` (any `19.x`); add one line in section 2.2: "Upgraded from 19beta3 to 19.0 on 2026-11-24 (`pg_upgrade --copy`, data in `E:\pgdata19`)". Step 13.4 of section 5.19.1: the command becomes `EPISTEME_RAW_ROOT=D:/episteme/01_raw bash scripts/data/run_pipeline.sh mesh graph`, plus the sentence "Without `EPISTEME_RAW_ROOT` the phase looks in `./01_raw` and finds no file." Drift log: one entry "0017 rollout (2026-11-24)": 16 reader calls, no timeout; 13 under 10 s; Neoplasms on atorvastatin 16-17 s, rare qualifier on warfarin 14-20 s, `search_text('patients')` 17-19 s, accepted by the owner, 10 s tuning parked; `mesh_qualifier` 76 rows. One entry "PostgreSQL 19.0 (2026-11-24)": upgrade method, pgvector rebuilt with MSVC, ANALYZE of 200 never-analyzed tables.
- [ ] **Step 5: Run** `pytest tests/data/test_text_override_tool.py -v` — PASS. Run `grep -rn "19beta3" CLAUDE.md docs/10-data-sources-runbook.md src scripts` — only historical mentions remain (each line names it as the former version).
- [ ] **Step 6: Commit** `fix(db): version guards accept PostgreSQL 19.x; runbook for 19.0 and EPISTEME_RAW_ROOT`.

### Task 2: Migration 0018 tables, `term_norm`, config families

**Files:**
- Create: `src/episteme/data/db/migrations/0018_question_terms.sql` (this task writes the head of the file: families, tables, `term_norm`; Tasks 6 and 7 append functions)
- Create: `src/episteme/reference/term_norm.py`
- Modify: `src/episteme/data/db/schema.sql` (same objects, byte-identical bodies), `src/episteme/config.py` (`API_RESULT_LIMITS`), `src/episteme/data/db/grants/0015_api_tables.sql` (`c_read` += `mesh_term`, `mesh_term_vec`), `src/episteme/ops/gate_migrations.py` (`"0018_question_terms.sql": ["table:episteme.mesh_term", "table:episteme.mesh_term_vec"]`), `tests/reference/_pbrer_db.py` (`_MIGRATION_FILES` += 0018)
- Test: `tests/data/test_migration_0018.py`, `tests/reference/test_term_norm.py`

**Interfaces:**
- Produces: tables
  - `episteme.mesh_term (descriptor_ui text NOT NULL, term text NOT NULL, term_norm text NOT NULL, term_kind text NOT NULL CHECK (term_kind IN ('name','entry')), is_preferred boolean NOT NULL, concept_ui text, concept_relation text CHECK (concept_relation IN ('NRW','BRD','REL')), mesh_release text NOT NULL, source_file text NOT NULL)`, indexes on `(term_norm)` and `(descriptor_ui)`;
  - `episteme.mesh_term_vec (descriptor_ui text NOT NULL, text_kind text NOT NULL CHECK (text_kind IN ('descriptor','entry')), text text NOT NULL, embedding halfvec(768) NOT NULL, model text NOT NULL, model_revision text NOT NULL, mesh_release text NOT NULL)`, index `mesh_term_vec_hnsw_idx USING hnsw (embedding halfvec_ip_ops)` and `(descriptor_ui)`;
  - `episteme_api_internal.term_norm(text) RETURNS text IMMUTABLE`; Python `term_norm(text: str) -> str`.
- The migration starts with a DO block that raises `'0018 needs the vector extension (scripts/data/db/install_extensions.sh)'` when `vector` is not installed.

- [ ] **Step 1: Write the failing tests** — non-pg: seeds of the three families equal `config.API_RESULT_LIMITS`; the file is one transaction (`BEGIN;` … `COMMIT;`); schema.sql carries the same `term_norm` body; gate marker present; `c_read` lists both tables. Python: `term_norm("Stevens-Johnson  Syndrome!") == "stevens johnson syndrome"`, `term_norm("Hb A1c") == "hb a1c"`, `term_norm("Ångström") == "ångström"`, `term_norm("") == ""`. pg: `test_term_norm_sql_equals_python` over 20 strings (accents, Greek letters, digits, punctuation, tabs); `test_0018_idempotent` (apply twice after 0017).
- [ ] **Step 2: Run non-pg tests** — FAIL.
- [ ] **Step 3: Implement** the migration head, schema.sql, config, grants, gate marker, `term_norm` (SQL: `btrim(regexp_replace(regexp_replace(lower(t), '[^[:alnum:]]+', ' ', 'g'), ' +', ' ', 'g'))`; Python: the same with `re.sub(r"[^\w]|_", " ", s.lower())` then collapse — the pg parity test is the judge).
- [ ] **Step 4: Run non-pg tests** — PASS. Controller runs the pg tests.
- [ ] **Step 5: Commit** `feat(db): 0018 mesh_term and mesh_term_vec tables, term_norm`.

### Task 3: Fill `mesh_term` in the `mesh` graph phase; warn on a missing raw file

**Files:**
- Modify: `src/episteme/data/graph_builder.py` (new `_parse_mesh_terms`; mesh phase lines ~516-626; result key `mesh_term`; summary line ~1090)
- Test: `tests/data/test_graph_builder_mesh_terms.py` (new; fixture XML with two descriptors, one carrying a preferred concept plus an `NRW` concept, gzip variant, malformed file)

**Interfaces:**
- Produces: `_parse_mesh_terms(xml_path: Path) -> list[tuple[str, str, str, bool, str | None, str | None]]` = `(descriptor_ui, term, term_kind, is_preferred, concept_ui, concept_relation)`; the name row has `term_kind='name'`, `is_preferred=True`, `concept_ui` of the preferred concept, `concept_relation=None`. Each `Term/String` of each `Concept` gives an `'entry'` row (skip a string equal to the descriptor name); `concept_relation` is the `RelationName` of the `ConceptRelation` whose `Concept2UI` is that concept (None for the preferred concept). Same streaming and failure rules as `_parse_mesh_qualifiers` (a parse failure returns `[]`).
- `mesh_release_of(source_file: str) -> str | None`: the 4-digit year after `desc` in the file name.

- [ ] **Step 1: Write the failing tests:** parse result for the fixture (exact tuples); gzip equals plain; malformed gives `[]`; `mesh_release_of("mesh/desc2026.gz") == "2026"`, `mesh_release_of("x.xml") is None`; pg: build fills `mesh_term` with `term_norm` set and `mesh_release='2026'`; a second build with a `desc2027` file replaces every row (only `'2027'` left); with no table it warns `"apply migration 0018"` and still builds; **a `source_file` whose raw file is not found prints `WARNING: graph_builder mesh phase: raw file for <source_file> not found under <raw_dir>; set EPISTEME_RAW_ROOT` and the result counts it in `mesh_missing_raw`** (ride-along).
- [ ] **Step 2: Run non-pg tests** — FAIL.
- [ ] **Step 3: Implement.** `mesh_term` is written for the newest release only (the source_file with the highest `mesh_release_of`): `DELETE FROM episteme.mesh_term` then COPY, in the same transaction as the other mesh tables of that file; `term_norm` computed in Python with `episteme.reference.term_norm.term_norm`. Rows counted in the `graph_commit` audit row. The missing-file branch (`if xml_path is None: continue`) prints the warning and counts.
- [ ] **Step 4: Run non-pg tests** — PASS.
- [ ] **Step 5: Commit** `feat(graph): mesh_term from desc entry terms; warn when a raw MeSH file is missing`.

### Task 4: MedCPT encoder wrapper and model config

**Files:**
- Create: `src/episteme/reference/medcpt.py`
- Modify: `src/episteme/config.py` (`MEDCPT_QUERY_MODEL`, `MEDCPT_QUERY_REVISION`, `MEDCPT_ARTICLE_MODEL`, `MEDCPT_ARTICLE_REVISION` constants; `EPISTEME_MEDCPT_THREADS` (default 4), `EPISTEME_MEDCPT_BATCH` (default 32))
- Test: `tests/reference/test_medcpt.py`

**Interfaces:**
- Produces: `class MedCPTEncoder` with `MedCPTEncoder.query() -> MedCPTEncoder`, `MedCPTEncoder.article() -> MedCPTEncoder` (load with `local_files_only=True` and the pinned revision; a missing model raises `RuntimeError("MedCPT model <id>@<rev> is not in the Hugging Face cache; run the model download step (runbook 5.20.2)")`), `encode_queries(texts: list[str]) -> numpy.ndarray[float32] (n, 768)`, `encode_articles(pairs: list[tuple[str, str]]) -> numpy.ndarray[float32] (n, 768)`, attributes `model_id`, `revision`. `torch.no_grad()`, `torch.set_num_threads(config threads)`, CLS pooling, no normalisation.
- `download_models() -> None` (`huggingface_hub.snapshot_download` of both ids at the pinned revisions), exposed as `python -m episteme.reference.medcpt download`.

- [ ] **Step 1: Write the failing tests** with a fake tokenizer/model injected through a constructor parameter (`MedCPTEncoder(model_id, revision, max_length, *, tokenizer=None, model=None)`): pooling takes token 0; the query path truncates at 64, the article path at 512 and passes `(title, text)` pairs; output dtype float32 shape `(n, 768)`; missing cache raises the exact message. A test marked `slow` encodes "heart attack" and "myocardial infarction" with the real models when cached (skips otherwise) and asserts their inner product is higher than with "fracture".
- [ ] **Step 2: Run** `pytest tests/reference/test_medcpt.py -m "not slow" -v` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(reference): MedCPT encoder wrapper with pinned revisions`.

### Task 5: `mesh embed` stage

**Files:**
- Create: `src/episteme/data/mesh/embed_mesh.py`, `scripts/data/mesh/embed_mesh.sh`
- Modify: `scripts/data/run_pipeline.sh` (stage allow-list += `embed`; validation arm: `embed` only for `mesh`; dispatch arm `run_stage embed "$HERE/mesh/embed_mesh.sh"`; not part of `all`; the DB-write `--dry-run` refusal includes `embed`)
- Test: `tests/data/test_embed_mesh.py`, `tests/ops/test_run_pipeline_dispatch*.py` (one case: `mesh embed` dispatches; `pubmed embed` dies with "embed is wired for mesh only")

**Interfaces:**
- Consumes: Task 2 tables, Task 3 `mesh_term`, Task 4 `MedCPTEncoder.article()`.
- Produces: `python -m episteme.data.mesh.embed_mesh [--limit N] [--batch N]`; `embed(conn, encoder, *, limit=None, batch=32) -> dict` with keys `release`, `deleted_stale`, `embedded`, `skipped_done`.
- Texts: per descriptor one `('descriptor', <name>, <scope note>)` row — the scope note is `episteme.articles.text` of `id = 'mesh:' || descriptor_ui` minus the leading name (empty when absent) — encoded as the pair `(name, scope note)`; per entry term one `('entry', <term>, '')` row encoded as `(term, '')`.

- [ ] **Step 1: Write the failing tests** (pg, with a fake encoder returning deterministic vectors): first run embeds every row of the 2-descriptor fixture; second run embeds 0 and reports `skipped_done` (restart = skip descriptors whose rows already exist for the same `mesh_release` and `model_revision`); rows of another release or revision are deleted first and counted in `deleted_stale`; `--limit 1` embeds one descriptor (all its rows); one `_audit` row per run; each batch commits on its own (kill between batches leaves whole descriptors only). Non-pg: the wrapper script sources `common.sh`, `load_dotenv`, requires `EPISTEME_ACTOR`; dispatch tests.
- [ ] **Step 2: Run non-pg tests** — FAIL.
- [ ] **Step 3: Implement.** Descriptors in `descriptor_ui` order; per batch of descriptors one transaction with COPY into `mesh_term_vec` (halfvec text format `'[v1,...]'`). Prints `done: release=<r> embedded=<n> skipped_done=<n> deleted_stale=<n>`.
- [ ] **Step 4: Run non-pg tests** — PASS.
- [ ] **Step 5: Commit** `feat(mesh): embed stage writes MedCPT vectors of the MeSH vocabulary`.

### Task 6: `match_terms` and `suggest_terms`

**Files:**
- Modify: `src/episteme/data/db/migrations/0018_question_terms.sql` (append), `src/episteme/data/db/schema.sql`
- Test: `tests/reference/test_api_question_terms.py` (new; pg fixture inserts `mesh_term`/`mesh_term_vec` rows directly)

**Interfaces:**
- Produces type `episteme_api_internal.term_row (kind text, descriptor_ui text, descriptor_name text, matched_text text, matched_term text, term_kind text, concept_relation text, basis text, score real, mesh_release text, note text)`.
- `episteme_api_internal.match_terms(p_question text) RETURNS SETOF term_row` and `episteme_api.match_terms(p_question text)` (cap family `resolve`).
- `episteme_api_internal.suggest_terms(p_vector halfvec(768), p_model_revision text, p_limit int DEFAULT NULL) RETURNS SETOF term_row`; `episteme_api.suggest_terms(p_vector halfvec(768), p_model_revision text)` (cap family `suggest`).
- Exact match algorithm: normalise the question (`term_norm`, cut at 1000 characters); for n from `limit_for('match_ngram_max')` down to 1, every run of n unused words whose text equals a `mesh_term.term_norm` is a match; mark its words used; one row per descriptor (first match wins, other matched phrases of the same descriptor appended to `matched_text` with `; `). Rows ordered by first word position. Then one `note` row `unmatched: <words>` when unused words remain (English stop words of `to_tsvector('english', …)` do not count as unmatched; if every word is a stop word or the question is empty: note `question has no searchable words`).
- Suggest: refuse notes as in Global Constraints; otherwise `SET LOCAL hnsw.ef_search` to `limit_for('suggest_ef_search')` (via `set_config(..., true)`), take the nearest `4 * limit` vectors by `<#>`, best score per descriptor (`score = -(embedding <#> p_vector)`), order by score desc then descriptor_ui, cut at `p_limit`.

- [ ] **Step 1: Write the failing tests:** "SJS after amoxicillin" maps `sjs` to its descriptor and reports `unmatched: after amoxicillin` … (adapt to fixture words); longest match wins ("chemical and drug induced liver injury" over "liver injury"); the same descriptor twice gives one row; two descriptors sharing `term_norm` both appear; a narrower-concept term returns `concept_relation='NRW'`; empty, punctuation-only and stop-word-only questions give the note; a 5,000-character question is cut and still answers; suggest: right revision gives descriptors best-first with one row per descriptor; wrong revision and stale release give the exact refusal notes and no term rows; public caps add the `truncated:` note; reader can EXECUTE the public functions and not the internal ones (extend `tests/reference/test_api_roles.py`).
- [ ] **Step 2: Controller runs the pg tests** — FAIL.
- [ ] **Step 3: Implement** (plpgsql; `match_terms` reads `mesh_term` by `term_norm = ANY(<all n-grams>)` in one query, then assigns words in plpgsql).
- [ ] **Step 4: Controller runs the pg tests** — PASS.
- [ ] **Step 5: Commit** `feat(api): match_terms and suggest_terms`.

### Task 7: `search_terms` — repeatable, never partial

**Files:**
- Modify: `src/episteme/data/db/migrations/0018_question_terms.sql` (append), `src/episteme/data/db/schema.sql`
- Test: `tests/reference/test_api_search_terms.py`

**Interfaces:**
- `episteme_api_internal.search_terms(p_descriptor_uis text[], p_sources text[] DEFAULT NULL, p_limit int DEFAULT NULL) RETURNS SETOF article_row`; `episteme_api.search_terms(p_descriptor_uis text[], p_sources text[] DEFAULT NULL)` (cap family `search`).
- Per descriptor (input order, duplicates dropped, unknown UI -> note `unknown descriptor: <UI>`): tsquery = OR of `websearch_to_tsquery('english', term)` over its name and entry terms (all concepts), terms in `term_norm` order; the `terms used:` note carries `tsquery::text`.
- Per descriptor and covered source with a valid index (same source/index/missing rules and notes as `search_text`): candidates `WHERE <the fts_doc index expression> @@ q ORDER BY b.article_id COLLATE "C" LIMIT search_scan`; a full candidate set adds the `capped:` note. Rank with `ts_rank_cd` exactly as `search_text` (label rank-chars rule). An article found by several descriptors keeps its first descriptor in `relation`; the others are named in its `note`.
- Deadline as 0017 (`statement_timestamp() + time_budget_ms`), checked before each descriptor/source step; when spent: return only the source/index notes, the `terms used:` notes and the `not run:` note — **no article rows**. `work_mem` raised as `search_text` and set back before every return.
- Rows: ordered by score desc, article_id `COLLATE "C"`, cut at `p_limit`.

- [ ] **Step 1: Write the failing tests:** one descriptor found through an entry term only ("heart attack" in an abstract) is returned with `relation` = its UI; the note shows the exact tsquery; with `search_scan` set to 2 through `result_limit`, three matching articles give the two lowest `article_id`s plus the `capped:` note, and a second call gives identical rows; with `time_budget_ms` set to 1 the call returns no `article` rows and the `not run:` note; unknown UI note; overlapping descriptors give one row per article with the other UI in `note`; public cap note.
- [ ] **Step 2: Controller runs the pg tests** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Controller runs the pg tests and** `EXPLAIN` **of one candidate query on `episteme_test` shows a Bitmap Index Scan on the partition's `_fts_idx`** — PASS.
- [ ] **Step 5: Commit** `feat(api): search_terms on confirmed descriptors, repeatable and never partial`.

### Task 8: Episteme MCP server `question_mcp`

**Files:**
- Create: `src/episteme/reference/question_mcp.py`, `scripts/question-mcp.sh` (starts `python -m episteme.reference.question_mcp` with the venv, like `scripts/mcpg-episteme.sh`)
- Modify: `pyproject.toml` + `uv.lock` (`mcp` dependency, `uv add mcp`), `src/episteme/config.py` (`EPISTEME_QUESTION_LOG_DIR`, default `<data root>/_question_log`)
- Test: `tests/reference/test_question_mcp.py`

**Interfaces:**
- Consumes: `episteme_api.match_terms`, `episteme_api.suggest_terms`, `episteme_api.search_terms`, `episteme_api.literature_by_mesh` over a psycopg connection as `episteme_api_reader` (`config.api_reader_password`); `MedCPTEncoder.query()` (loaded lazily on the first suggestion).
- Produces MCP tools (FastMCP):
  - `translate_question(question: str) -> dict` with keys `confirmation_id`, `mesh_release`, `terms` (list of dicts with every `term_row` field plus `n`, a 1-based index), `notes`.
  - `search_confirmed(confirmation_id: str, ctx: Context, sources: list[str] | None = None, substance_key: int | None = None, from_year: int | None = None, to_year: int | None = None) -> dict` with keys `status` (`searched`, `declined`, `cancelled`, `no_elicitation`, `unknown_confirmation`, `nothing_accepted`), `accepted`, `rejected`, `rows`, `provenance`.
- `confirmation_id` = sha256 hex of `json.dumps({"question", "mesh_release", "terms": [descriptor_ui, basis, score…]}, sort_keys=True)`; proposals kept in a dict in the server process.
- Elicitation: `ctx.elicit(message, schema)` where the schema is a pydantic model built at run time with one `bool` field `accept_<n>` (default `False`) per proposed term, titled `"<name> (<UI>) — <basis>[, score <s>][, <relation>]"`. `accept` with at least one `True` -> search; `accept` with none -> `nothing_accepted`; `decline` / `cancel` -> no search; an exception from a client without elicitation support -> `no_elicitation`.
- Log: append one JSON object per line to `<log dir>/question-<YYYY-MM-DD>.jsonl` (`event`: `translate` or `confirm`; `ts` UTC; `question`; `confirmation_id`; `terms`; `elicitation_action`; `confirmed_by: "user via MCP elicitation"` on accept; `accepted`; `rejected`; `queries` (the `terms used:` notes); `release_ids`). Never read back.

- [ ] **Step 1: Write the failing tests** (non-pg; a fake DB layer `QuestionDB` protocol with `match_terms`, `suggest_terms`, `search_terms`, `literature_by_mesh`; a fake encoder; a fake `ctx` whose `elicit` returns scripted results): exact-only question does not load the encoder; an `unmatched:` note triggers one suggestion call; `confirmation_id` stable for equal input; unknown id -> `unknown_confirmation`, no DB search call; decline / cancel / no-elicitation / nothing-accepted -> no DB search call and a log line; accept of terms 1 and 3 calls `search_terms` with exactly those UIs in proposal order and `literature_by_mesh` once per accepted descriptor name when `substance_key` is given; the agent cannot pass UIs (no such parameter); log lines carry the exact keys above.
- [ ] **Step 2: Run** `pytest tests/reference/test_question_mcp.py -v` — FAIL.
- [ ] **Step 3: Implement** (`QuestionDB` real implementation over psycopg in the same module; `main()` runs FastMCP over stdio).
- [ ] **Step 4: Run** — PASS. Controller: one pg smoke test `test_question_db_against_reader` (marked `pg`) calls the three functions through the reader role.
- [ ] **Step 5: Commit** `feat(reference): question_mcp — translate, confirm by elicitation, search, log`.

### Task 9: Acceptance list, runbook rollout, docs

**Files:**
- Create: `tests/reference/data/question_terms_acceptance.json` (at least 30 entries `{"question": ..., "expected": ["D…", …], "kind": "acronym"|"lay"|"description"}`, at least 8 of each kind, PV-flavoured: SJS, TEN, DILI, MI, QT prolongation, anaphylaxis, "kidney failure", "low platelets", "skin peeling after an antibiotic", …; expected UIs checked against desc2026 by the controller), `scripts/ops/question_terms_acceptance.py` (runs `match_terms`/`suggest_terms` through the reader and the query encoder; prints per question exact-hit, top-10-hit, times; writes a JSON report to a given path)
- Modify: `docs/10-data-sources-runbook.md` (new section 5.20 "Question to standard terms": 5.20.1 apply 0018 + roles file + `api_settings sync`; 5.20.2 model download `python -m episteme.reference.medcpt download` (about 0.9 GB, once); 5.20.3 `EPISTEME_RAW_ROOT=D:/episteme/01_raw bash scripts/data/run_pipeline.sh mesh graph` then `run_pipeline.sh mesh embed` (first `--limit 2000` on `episteme_test`, time it), `ANALYZE episteme.mesh_term, episteme.mesh_term_vec`; 5.20.4 MCP client config for `scripts/question-mcp.sh`; 5.20.5 acceptance; migration table row for 0018), `docs/13-graphrag-design.md` section 6.2 (replace with the as-built design and a pointer to the spec), `docs/02-data-sources.md` (MedCPT models: public domain, US government work; used for the vocabulary only), `docs/project-incubation-baseline.md` (drift entry for this feature)
- Test: `tests/docs/test_question_terms_docs.py` (runbook names 0018, `mesh embed`, `EPISTEME_RAW_ROOT`, `question-mcp.sh`; the acceptance file has >= 30 entries, >= 8 per kind, UIs match `^D\d{6,9}$`)

- [ ] **Step 1: Write the failing docs test** — FAIL.
- [ ] **Step 2: Write the acceptance list, script and docs.**
- [ ] **Step 3: Run** `pytest tests/docs/test_question_terms_docs.py -v` — PASS.
- [ ] **Step 4: Commit** `docs: question-to-terms runbook, acceptance list, design 6.2 as built`.

## Rollout (controller, after merge; not a task)

Per runbook 5.20, `episteme_test` first, then `episteme`, one heavy job at a time: 0018 -> roles file -> `api_settings sync` -> model download -> `mesh graph` (expect `mesh_term` rows > 200,000) -> `mesh embed` (sample timing, then full) -> ANALYZE -> acceptance through `scripts/question-mcp.sh` and the reader; report exact-hit and top-10-hit shares and times.
