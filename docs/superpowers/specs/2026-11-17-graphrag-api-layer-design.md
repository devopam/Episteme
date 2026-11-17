# GraphRAG API layer: SQL lookup functions for a read-only MCPg instance

Status: draft for owner review, 2026-11-17.
Owner decisions are marked **(owner)**. Calls made by the designer are marked **(designer)**; the owner may correct them.

## 1. Intent

The model must not invent regulatory or literature facts. It reads them from the Episteme database through lookup tools (docs/13 section 1).
Today these lookups exist only as Python functions in `src/episteme/reference/` (resolve, lookups, literature, codes). Only the harness in `src/episteme/harness/` can call them.
MCPg can only send SQL, so it cannot use them. An MCPg user has to rewrite the substance-family expansion, the latest-release choice and the licence tags by hand.

This work moves the lookup logic into the database as SQL functions. A **separate, read-only MCPg instance** **(owner)** then exposes them. The Python lookups become thin wrappers over the same functions, so MCPg and the later SFT harness give the same answers from one source.

The order is fixed **(owner)**: finish the data layer first, then SFT.

## 2. Goals

1. A schema `episteme_api` with one SQL function per lookup (section 4). Each function returns rows that carry their provenance.
2. A read-only role for the new MCPg instance. It has `USAGE` on `episteme_api` and `EXECUTE` on its functions, and nothing else.
3. Full-text search over title and abstract for the literature sources, and over the full text of labels **(owner)** (section 5).
4. A precomputed FAERS table for the seriousness and outcome splits **(owner)** (section 4.3).
5. The `ema_epar` source built from the offline data that is already on disk **(owner)** (section 6).
6. The Python lookups in `src/episteme/reference/` call the SQL functions. Their public signatures and their `LookupResult`/`Claim` shapes do not change.
7. docs/13 is updated: the "as built" sections, MCPg exposure, and the correction that pgvector 0.8.7 is installed.

## 3. Non-goals and anti-goals

- **Non-goals:**
  - No SFT builders, teacher runs or training. They come after this work **(owner)**.
  - No semantic or vector search. That is the next spec (section 9).
  - No new downloads from EMA or any other source.
  - No MedDRA, WHODrug Global or CDISC content, coding or hierarchy logic. These are licensed and out **(owner)**.
- **Anti-goals:** the work fails if any of these happen:
  - a function writes to the database;
  - the read-only role can read a base table directly;
  - a function answer differs from the current Python lookup for the same input, except where a section below says so;
  - a function returns a fact without its source, release and licence tag;
  - a full-text index build runs in parallel with another heavy job.

## 4. Lookup functions (`episteme_api`)

### 4.1 Common rules

- **Definition:** each function is `LANGUAGE sql` or `plpgsql`, `STABLE`, `SECURITY DEFINER`, owned by a dedicated `NOLOGIN` owner role, with `SET search_path = pg_catalog, episteme, episteme_api`. It reads only. **(designer)**: SECURITY DEFINER lets the read-only role use the functions without any grant on the base tables.
- **Provenance:** every result row carries `source` (dataset or article source), `release_id`, `released_on` where known, `licence` and `source_ref` (record id, article id, set id).
  - **(designer)**: `licence` keeps today's lookup vocabulary (`commercial`, `pending`, `open_metadata`, from `LOOKUP_SOURCE_SUBSETS`), because the SFT gate in `sft/batch.py` reads it.
  - Rows tagged `pending` are **returned with their tag, not hidden** **(designer)**. Filtering happens when SFT data is built (docs/13 section 9).
- **Settings in the database (designer):** SQL cannot read `config.py`, and the reader can change a session setting with `SET`.
  - The licence mapping and the result limits live in two small tables, `episteme_api_internal.source_subset` and `episteme_api_internal.result_limit`. `episteme_api_owner` owns them, and the reader has no access.
  - The command `python -m episteme.reference.api_settings sync` writes them from `config.py`. It runs at rollout and after any config change, so `config.py` stays the only source.
- **Release choice:** a function reads the `is_latest` release of each dataset (`reg_release`). An optional `p_release` argument pins a release where today's Python allows it.
- **Substance family:** functions that take a substance use one shared helper, `episteme_api.substance_family(p_substance_key bigint)`. It is the SQL form of `_scope.substance_scope` (the moiety family: salts, esters).
- **Bounded results (designer):** there are two layers.
  - Internal functions in schema `episteme_api_internal` return complete results, and the reader has no grant on them. The Python wrappers call these, so parity with today's Python holds and the PBRER harness gets every row.
  - The public `episteme_api` functions call the internal ones with a cap from `result_limit`. When they cut a result, they add one row with `kind = 'note'` that says `truncated: <n> of <total>`.
  - Results are ordered deterministically.
- **Name normalisation (designer):** one SQL function, `episteme_api_internal.norm_name(text)` (NFKC, then `lower`), is the single definition. The Python resolve path calls it instead of its own `norm_name`. The known differences between `casefold` and `lower` (for example `ß`) are a stated, accepted change.
- **No dumps:** functions answer one entity or one query. No function returns a whole table. This follows the docs/10 guidance for `code_current`.
- **Coverage notes:** where the Python lookup adds a coverage note today (for example `<dataset>: status field not found`), the function returns the note as a row with `kind = 'note'`.

### 4.2 Function set

| Function | Replaces | Reads |
|---|---|---|
| `resolve_entity(p_text text, p_id_type text default null)` | `resolve.resolve_entity` | `substance`, `substance_xref` |
| `substance_family(p_substance_key bigint)` | `_scope.substance_scope` | `substance`, `substance_xref` |
| `approval_status(p_substance_key bigint, ...)` | `lookups.approval_status` | `reg_record` (EMA medicines, openFDA Drugs@FDA, Orange Book) |
| `safety_actions(p_substance_key bigint, p_from date, p_to date, ...)` | `lookups.safety_actions` | `reg_record` (EMA DHPC, referrals, PSUSA, FDA enforcement) |
| `current_label(p_substance_key bigint, ...)` | `lookups.current_label` | `label_substance`, `label_section`, `articles` (DailyMed, `ema_epar`) |
| `label_section_text(p_article_id text, p_section text)` | new | `label_section`, `article_body` |
| `trials(p_substance_key bigint, ...)` | `lookups.trials` | AACT tables |
| `faers_counts(p_substance_key bigint, p_from date, p_to date, ...)` | `lookups.faers_counts` | FAERS tables |
| `literature_by_mesh(p_substance_key bigint, p_topic text, p_from_year int, p_to_year int, ...)` | `literature.literature` | `article_mesh`, `mesh_tree`, `articles` |
| `graph_neighbours(p_article_id text, p_kind text, ...)` | `graph_builder.neighbours` (read part) | `article_*` edge tables |
| `search_text(p_query text, p_sources text[] default null, ...)` | new | full-text index (section 5) |
| `lookup_code(p_system text, p_code text)` | `codes.lookup_code` | `code_current` |

### 4.3 Function details

- **FAERS** **(owner)**: `faers_counts` returns report counts per substance family and interval, split by quarter, seriousness and outcome. It also returns counts **per reaction term as FDA publishes them (MedDRA Preferred Term)**.
  - An SOC appears only where a loaded file states it. No MedDRA hierarchy or coding is built or looked up.
  - **Seriousness and outcome splits (owner):** a **new precomputed table**, built with the FAERS signal tables. Each row is one moiety, quarter, seriousness flag and outcome code.
    - It counts **reports**, not reactions, and the suspect role only (primary or secondary suspect) **(designer)**.
    - Building it is one extra heavy job at the next signal rebuild: a runbook step, with a sample run first.
  - **Licence:** the reaction terms are quoted from the public FAERS files, with attribution to FDA. The licence subset follows `LOOKUP_SOURCE_SUBSETS` for FAERS.
- **Literature by MeSH:** this is the v1 behaviour of `literature.literature`, with the **topic** expanded to its descendant descriptors through `mesh_tree` (65,360 rows) **(owner)**.
  - **(designer)**: only the topic is expanded. The substance side keeps the v1 behaviour.
  - The parked minor applies: `article_mesh` needs an index on `lower(descriptor_name)` (drift log, 2026-11-10).
- **Graph neighbours** **(owner)**: `p_kind` is one of `cites`, `cited_by`, `shared_mesh`, `trial`, `label`. A function call goes one hop.
  - In this version every kind is a **live join, with no new edge build** **(owner)**:
    - `cites` and `cited_by` come from the existing citation edges.
    - `trial` comes from AACT `study_references` by PMID (1.16M references in the latest AACT release).
    - `label` returns labels whose linked substance matches one of the article's MeSH chemicals (`article_chemicals` → `substance_xref` → `label_substance`).
    - `shared_mesh` returns articles that share the article's **major-topic** descriptors, ranked by descriptor rarity (fewest articles first), with a cap.
  - Whether to precompute edge tables is decided later, from the results of this version **(owner)**.
- **Label section text** **(owner)**: returns the text slice `[start_offset, end_offset)` of one section of one label, for DailyMed and for the EU labels from the 35 EPAR PDFs (section 6).
- **Parked minors fixed here** (drift log, 2026-11-10):
  - the UNII arm of resolve uses `UNION`, not `OR`, and respects `id_type`;
  - `release` is filled in resolve, FAERS and literature provenance;
  - `source_file` is filled for trials.

### 4.4 Python wrappers

The functions in `src/episteme/reference/` keep their signatures and return types. Internally each one calls its SQL function and maps the rows to `Claim`/`LookupResult`.

Parity test, in pg tests on `episteme_test`: for each lookup, fixed fixture inputs give the same claims through the old query path and through the function. The old query path is kept in the test module only, until parity passes. It is then deleted in the same branch.

## 5. Full-text search **(owner)**

- **Coverage:**
  - Title and abstract for `pubmed`, `pmc`, `openalex`, the preprint source(s), `bookshelf`, `dailymed` and `ema_epar`.
  - The full text for `dailymed` and `ema_epar` only.
  - Not covered: `pubchem` and the structured sources in the `default` partition.
- **Mechanism (designer):**
  - A stored generated `tsvector` column is not used, because it would store text-derived data again (the 2026-11-16 duplication lesson).
  - Instead, an expression GIN index per `article_body` partition, `to_tsvector('english', <title || abstract>)`. For the label partitions the expression covers the full `text`.
  - `search_text` uses the same expression, so the planner uses the index.
  - Ranking is `ts_rank_cd`, with deterministic tie-break on `article_id`.
- **Build:**
  - One partition at a time, `CREATE INDEX CONCURRENTLY`, as a runbook step with a sample run first.
  - Estimated total: 40–70 GB on E:.
  - Free-space check before each build: at least 2× the expected index size free.
  - One heavy job at a time.
- **Query safety:** `websearch_to_tsquery` only, no raw `to_tsquery` input. `p_limit` is bounded.

## 6. `ema_epar` from offline data **(owner)**

1. **The 35 EPAR PDFs** in `<raw>/ema/epar/` are extracted with the existing `extract_epar.py` path and loaded into `articles_ema_epar`/`article_body_ema_epar`.
   - Their SmPC sections go into `label_section` (via the existing `smpc.py`), with `label_substance` links, so `current_label` and `label_section_text` cover these EU products.
   - The extract guards that compare the feed with the documents must allow an offline run with only 35 of 20,342 documents present. **(designer)**: a documented `--offline-subset` mode records the subset size in lineage. It does not change the guards for a full run.
   - The 35 documents are 23 product-information documents (the EU labels, SmPC), 9 assessment reports and 3 RMP summaries. Only the 23 product-information documents give `label_section` rows.
   - **Substance links (owner, from sampling on 2026-11-17):**
     - Method: an exact normalised match of the EMA active substance (from `ema_medicines`) against the names in `substance_xref`, with combinations split on `;`. Each `label_substance` row records the method `ema_name_exact`.
     - Sample result: 29 of 35 documents and 21 of 23 labels link, each to exactly one substance. The misses are vaccines. They stay unlinked, and the load report lists them.
     - **Veterinary** products are loaded as text but **not linked**, so `current_label` for a human substance never returns a veterinary label.
     - No fuzzy matching: the sample showed it would only add wrong links.
2. **One generated article per EU medicine** (2,754) from the loaded `ema_medicines` and `ema_epar_documents` records, with `source = ema_epar` and a distinct article-id prefix (`ema_medicine:<product number>`).
   - Content: medicine name, active substance, INN, ATC, therapeutic area, indication, status and dates, and the list of its EPAR documents (title, type, dates).
   - The text is generated from the feed fields in a fixed template, and lineage names the two feed releases.
   - **(designer)**: these rows count as reference text, with the same licence as the feeds (EMA, credit required). Corpus inclusion follows the existing `ema_epar` licence rules.
3. No EMA request is made. The URL guard and the "no repeated downloads" rule stand.

## 7. Roles and the read-only MCPg instance **(owner)**

- **Roles:**
  - `episteme_api_owner` (NOLOGIN) owns the functions and has `SELECT` on exactly the base tables they read.
  - `episteme_api_reader` (LOGIN) has `USAGE` on `episteme_api`, `EXECUTE` on its functions, and nothing in `episteme`.
  - The `PUBLIC` execute grant is revoked from the functions.
- **Grants:** they live in a migration-side grants file that the operator applies as superuser. MCPg `run_ddl` refuses `GRANT` (0006 note). They are applied to `episteme_test` and `episteme`.
- **The password** is an `.env` key (name only in docs). `config.py` reads it. No value appears in docs or code.
- **MCPg instance:**
  - The runbook gets a new section for a second MCPg instance that connects as `episteme_api_reader`, in restricted mode, with DDL off.
  - `scripts/mcpg-episteme.sh` gets a reader profile, or a sibling script.
  - The existing admin instance is unchanged.
- **Tunables** (`config.py`, `.env.example`, synced into `result_limit`): the default and maximum result cap per function family, and the full-text index build lock timeout.

## 8. DailyMed rejected setIds **(owner)**

The 6 labels that failed identity extraction on 2026-11-10 (`setId is missing or not a UUID`) are discarded. They were never loaded, so no row is deleted.
- The drift log records the decision.
- The DailyMed runbook states that these failures are accepted, not retried.
- **(designer)**: `failed_inputs.json` keeps only the count. The next DailyMed run also writes the file names of failures, so a future discard decision can name them.

## 9. Next spec: semantic search (not built here)

- **Measured (2026-11-17):**
  - E: is 2,000 GB, with 812 GB used (database 793 GB) and 1,188 GB free.
  - Full scope is about 158M vectors: titles and abstracts of the literature sources, label sections, and body chunks of PMC and bookshelf.
- **Space:**
  - 768-dim `halfvec` with a binary-quantized HNSW per partition and re-ranking needs about 310 GB.
  - With `pg_turboquant` at 4 bits it may need about 60–90 GB.
- **The next spec starts with a bake-off on a 1M-abstract sample:**
  - candidates: pgvector `halfvec` HNSW, binary HNSW plus re-ranking, and `pg_turboquant` at 2 and 4 bits (if the extension builds on PostgreSQL 19beta3 on Windows; licence checked);
  - measures: recall against exact search, size, build time and latency.
- **Fixed parts:**
  - The embedding pass runs on rented hardware.
  - The query is embedded by the caller with the same model.
  - `episteme_api` gets `semantic_search` and a hybrid function with the same provenance columns as section 4.1.

## 10. Testing and rollout

- **Tests:**
  - non-pg tests for argument validation and SQL text;
  - pg tests on `episteme_test` for each function: fixtures, parity with the Python path, bounded results with the truncation note, provenance columns present, licence tags;
  - the read-only role cannot `SELECT` a base table, and cannot execute a function in `episteme`, `episteme_api_internal` or the AACT schemas. **(designer)**: built-in `pg_catalog` functions stay available, because MCPg needs them;
  - a function cannot write (it is a `STABLE` function, and a test calls it inside a read-only transaction);
  - `search_text` uses the index (EXPLAIN on the test partition under the same expression).
- **Rollout on the local machine, in order, one heavy job at a time:**
  1. the migration (schema, functions, roles) on `episteme_test`, then on `episteme`;
  2. the grants file as superuser;
  3. EPAR offline load, sample run first;
  4. the full-text indexes, partition by partition, sample first;
  5. the read-only MCPg instance set up by the owner;
  6. a smoke test of each function through that instance.
- **Docs:** docs/13 (built and planned, MCPg, pgvector status), docs/10 (EPAR offline mode, full-text index builds, the second MCPg instance), docs/09 (licence tag), docs/02 (EPAR rows), docs/12 (ema_epar wired), and the drift log.

## 11. Left to the builder

- The exact column list of each function's result type, within the rules of section 4.1.
- Whether a function is written in `sql` or `plpgsql`.
- The internal helper functions.
- The test fixture design.
