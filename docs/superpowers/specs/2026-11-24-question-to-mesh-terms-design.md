# Question to standard terms: MeSH entry terms, MedCPT term suggestions, confirmed searches

Status: owner direction of 2026-11-24 (brainstorm). Replaces the "semantic search" item of
`docs/13-graphrag-design.md` section 6.2 as the next lookup feature. Every rule of
`2026-11-17-graphrag-api-layer-design.md`, `2026-11-24-api-layer-perf-design.md` and
`2026-11-24-api-no-timeouts-design.md` stays unless this document names a change.

## 1. Intent

A pharmacovigilance medical reviewer or a QPPV asks a question in their own words: an
acronym ("SJS", "DILI"), a lay phrase ("heart attack", "liver damage"), or a description
("skin peeling after the antibiotic"). The literature and the labels use the standard terms
("Stevens-Johnson Syndrome", "Chemical and Drug Induced Liver Injury"). `search_text` matches
words, so a question in the reviewer's words finds little.

The feature translates the question into standard MeSH terms, lets the reviewer confirm the
terms, and then runs the existing exact searches on the confirmed terms.

**Owner rule (2026-11-24): every search states the standard terms it used, so that the
result has full provenance and the search can be repeated exactly.** A missed record must be
traceable to the terms that were searched.

## 2. Goals, non-goals, anti-goals

Goals:
- Translate a question to MeSH descriptors with a stated basis for each term.
- Exact first: the question's words against MeSH descriptor names and entry terms (synonyms,
  acronyms, lay terms, older names).
- When the exact step leaves the question (or a part of it) unmatched: MedCPT suggests the
  nearest descriptors, with a score.
- **Confirm first** (owner decision): no search runs on a suggested term until the reviewer
  accepts it. Exact matches are shown for confirmation too (one confirm step for the whole
  term list).
- Searches on confirmed terms return the usual article rows plus rows that state the terms,
  the basis of each term and the exact query text that ran.

Non-goals:
- Vector search over articles (the 158M-vector "option C"). Not built; it cannot state the
  terms it used. TurboQuant / `pg_turbovec` is not needed at this scale (section 5.3).
- MedDRA, UMLS, CHV: not used (licence decisions of 2026-11-17). MeSH is the vocabulary.
- MeSH Supplementary Concept Records (`supp2026`): substances go through `resolve_entity`.
- An embedding tool in MCPg (owner: embedding is data preparation, not a database tool).
- Answer generation. The feature returns terms and records, not prose.

Anti-goals (a failure even if it works):
- A search that runs on a term the reviewer did not accept.
- A result that does not state which terms and which query text produced it.
- A suggestion shown without its basis (model, model version, MeSH release, score).
- A different result for the same confirmed terms on the same data release.

## 3. Constraints

- PostgreSQL 19 (upgrade from 19beta3 to 19.0 in progress on 2026-11-24), pgvector 0.8.7.
  Windows laptop, 16 GB RAM, 4 CPU threads; one heavy job at a time.
- The reader path stays SQL only: `episteme_api_reader` through the reader MCPg instance,
  restricted mode, 30 s `statement_timeout`. No function writes.
- `config.py` is the only `os.environ` reader; new tunables go there; caps go through
  `API_RESULT_LIMITS` and `api_settings sync`.
- `torch` and `transformers` are already dependencies.
- Restartability and `input_key` rules apply to the new load steps.

## 4. Approaches considered

- **A. Exact translation only.** Exact and repeatable; a free description finds no term.
- **B. Exact translation plus MedCPT suggestions over the vocabulary (chosen by the owner).**
  Vectors for about 30k descriptors and their entry terms, not for articles. Every search
  runs on stated, confirmed terms.
- **C. Article vectors at full scope (158M, TurboQuant).** Finds similar records directly,
  but the search uses no terms, so it cannot meet the provenance rule; needs rented hardware
  and 60 to 300 GB of index. Rejected.

Where the question encoder runs (owner discussion):
- In MCPg (`embed_text` tool): rejected by the owner. MCPg stays a generic database tool.
- **In Episteme, as a small MCP tool (chosen).** Episteme owns the model and the vocabulary,
  as it owns every loader. The batch embedding of the vocabulary is a load step.
- By the caller: rejected; an LLM agent cannot run MedCPT.

## 5. Design

### 5.1 Vocabulary tables (migration 0018, `mesh` graph phase)

- `episteme.mesh_term (descriptor_ui text, term text, term_norm text, term_kind text,
  is_preferred boolean, concept_ui text, source_file text)`.
  - From desc2026: the `DescriptorName` (`term_kind = 'name'`) and every `Term/String` of every
    `Concept` (`'entry'`). `term_norm`: lower case, punctuation to space, spaces collapsed
    (one SQL-and-Python-identical rule; a test checks both give the same result).
  - Index on `term_norm`; filled by the existing `mesh` graph phase, DELETE by `source_file`,
    then COPY (as `mesh_qualifier` in 0017). One audit row with the other mesh tables.
- `episteme.mesh_term_vec (descriptor_ui text, text_kind text, text text, embedding
  halfvec(768), model text, model_revision text, mesh_release text)` with an HNSW index
  (`halfvec_ip_ops`; MedCPT uses inner product).
  - One row per descriptor: name plus the preferred concept's scope note, embedded with the
    MedCPT **Article** encoder (the asymmetric model: articles side).
  - One row per entry term (`text_kind = 'entry'`), same encoder.
  - Filled by a new step `run_pipeline.sh mesh embed`: reads `mesh_term` and the descriptor
    XML, embeds in batches on CPU, writes with COPY. Restartable per batch (checkpoint keyed
    on `input_key` of the descriptor file plus the model revision). `--limit 2000` embeds 2,000
    descriptors as a trial (not `--sample`: the sample gate has no rules for this stage). Expected size: a few hundred thousand rows, under 2 GB with the index; a full
    run of a few hours on the laptop CPU (the plan measures it on the sample first).
- The graph phase warns (does not exit 0 silently) when it finds no raw MeSH file
  (ride-along item, section 8).

### 5.2 SQL functions (migration 0018; `episteme_api` and `episteme_api_internal`)

Owned by `episteme_api_owner`, `SECURITY DEFINER`, `STABLE`, `search_path` locked, as 0015.

- `match_terms(p_question text)` returns term rows: `kind` (`term`, `note`), `descriptor_ui`,
  `descriptor_name`, `matched_text` (the question words), `matched_term` (the MeSH term),
  `term_kind`, `basis` (`exact`), `score` (null), `mesh_release`.
  - Exact step: normalize the question with the `term_norm` rule; find every MeSH term whose
    `term_norm` occurs in the question as a run of whole words (longest match first, a word
    used by one match only). Up to `match_ngram_max` words per term (new config family).
  - Returns the unmatched words in one `note` row (`unmatched: <words>`), so the caller knows
    when to ask for suggestions.
- `suggest_terms(p_vector halfvec(768), p_model_revision text)` returns term rows with
  `basis = 'medcpt'` and `score` (inner product), best first, at most the `suggest` cap (new
  family, default 10, max 50).
  - Refuses (one `note` row, no terms) when `p_model_revision` is not the revision stored in
    `mesh_term_vec`: a vector from another model must not be compared.
  - Groups entry-term hits by descriptor (best score per descriptor).
- `search_terms(p_descriptor_uis text[], p_sources text[] DEFAULT NULL)` returns article rows.
  - For each descriptor: one tsquery built from its name and its entry terms
    (`websearch_to_tsquery` per term, joined with OR), run through the same code path as
    `search_text`.
  - Each article row's `relation` holds the descriptor UI it matched (one row per article,
    the first descriptor in input order wins; the others are listed in its `note`).
  - One `note` row per descriptor: `terms used: <UI> <name>; query: <the exact tsquery text>`.
  - **Repeatable, never partial (owner decision 2026-11-24, option 2a).** The candidates of each
    descriptor and source are taken in `article_id` order, at most `search_scan` per partition,
    then ranked; a capped set is stated in a note (`capped: first <n> matches by article_id`).
    The same confirmed terms on the same data release give the same rows. When the 0017 time
    budget is spent, `search_terms` returns no article rows: one note names the descriptors
    and sources not finished and asks for a search with fewer terms. A partial result is
    never returned. (`search_text` itself keeps its 0017 behaviour.)
- `literature_by_mesh` is unchanged; it already takes a descriptor name as `p_topic`. The
  MCP tool calls it with the confirmed descriptor names.
- The settings sync, the roles file and the grants change as 0015 says (new tables in
  `c_read`; new functions EXECUTE to the reader).

### 5.3 Why pgvector and not TurboQuant here

The vocabulary index has a few hundred thousand vectors. pgvector `halfvec` HNSW at this size
is small, exact enough (the bake-off: recall 0.95 to 0.98 at 250k with `ef_search` 100 to
200), and works today. `suggest_terms` sets `hnsw.ef_search` to the `suggest_ef_search` config
value (default 200) for the call. A suggestion is a proposal for the reviewer, not a search
result, so ANN recall does not decide what is searched. TurboQuant is not needed; the
`pg_turbovec` build trial stays a separate, optional item.

### 5.4 The Episteme MCP tool (`episteme.reference.question_mcp`)

A small MCP server (stdio), started by the reviewer's agent next to the reader MCPg instance.
New dependency: the `mcp` Python package. It connects as `episteme_api_reader` (password from
the `.env` key `EPISTEME_API_READER_PASSWORD` via `config.py`), so it can do nothing the reader
cannot do.

Tools:
- `translate_question(question)`:
  1. calls `match_terms`;
  2. when a `note` row reports unmatched words: embeds the question with the MedCPT **Query**
     encoder (CPU, loaded once per process) and calls `suggest_terms`;
  3. returns one list: exact terms first, then suggestions, each with its basis, score, model
     revision and MeSH release, plus a `confirmation_id` (a hash of the question, the term
     list and the MeSH release).
- `search_confirmed(confirmation_id, sources?, substance_key?, from_year?, to_year?)`:
  - **The person confirms, not the agent (owner decision 2026-11-24, option 1a).** The tool
    asks the human through MCP elicitation: it shows every proposed term with its basis
    (exact match or MedCPT score, concept relation) and the person ticks the terms to accept.
    The agent cannot pass an accepted list. When the client does not support elicitation, or
    the person declines or cancels, the tool runs no search and says so.
  - refuses an unknown `confirmation_id`;
  - runs `search_terms`, and `literature_by_mesh` per descriptor when `substance_key` is
    given;
  - returns the rows plus a provenance block: the question, every proposed term with its
    basis, the accepted terms, the rejected terms, the exact queries, the data release ids.
- A local search log (JSON lines, one entry per `translate_question` and per
  `search_confirmed`), path from config (`EPISTEME_QUESTION_LOG_DIR`). The reader stays
  read-only; the log is the record that the reviewer accepted the terms. Each confirmation
  entry records `confirmed_by: "user via MCP elicitation"`, the accepted and the rejected
  terms, and the elicitation result (`accept`, `decline`, `cancel`).

The MCP tool keeps proposals in memory for the session (`confirmation_id` to term list); the
log keeps them for audit.

### 5.5 Model

- `ncbi/MedCPT-Query-Encoder` for questions, `ncbi/MedCPT-Article-Encoder` for the vocabulary
  (both public domain, 768 dims, inner product). The model revision (the Hugging Face commit
  hash) is pinned in `config.py` and stored with every vector and every suggestion.
- The models are downloaded once into the Hugging Face cache by an explicit operator step
  (runbook), not on first use.

## 6. Acceptance

- A list of at least 30 reviewer-style questions with the descriptors a reviewer expects
  (acronyms, lay phrases, descriptions; the plan drafts it, the owner may correct it), kept
  in `tests/reference/data/`.
- Reported: share of questions where the exact step finds every expected descriptor; share
  where the expected descriptors are in the top 10 suggestions; time of `translate_question`
  (target under 5 s warm on the laptop) and of `search_confirmed` (no timeout; the 0017 time
  budget applies).
- Repeat check: the same confirmed terms on the same release give the same rows and the same
  query text twice, including a search that hits the `search_scan` cap.
- Confirmation check: `search_confirmed` runs no search when the elicitation is declined or
  cancelled, or when the client has no elicitation support.
- Through the reader instance only, as the 0016 and 0017 acceptance runs.

## 7. Builder's calls (not owner decisions)

- Article encoder for the vocabulary, query encoder for questions; scope note in the
  descriptor text.
- `search_terms` ORs the entry terms of a descriptor into its query (the article may use any
  of them); the note states the exact query.
- The search log is a local file, not a database table (the reader cannot write).
- Defaults: `match_ngram_max` 8, `suggest` (10, 50), `suggest_ef_search` 200.
- **Concepts.** `mesh_term` keeps the terms of every concept of a descriptor, with a column
  `concept_relation` (`preferred`, `NRW`, `BRD`, `REL` from the concept's
  `ConceptRelationList`, null for the preferred concept). `match_terms` maps any of them to the
  descriptor (as NLM's own indexing and PubMed's term mapping do) and returns the relation, so a
  match through a narrower concept is visible. `search_terms` ORs the terms of every concept,
  and the note lists them; the reviewer sees the widening before confirming.
- **Release identity.** `mesh_term` and `mesh_term_vec` get a `mesh_release` column taken from
  the descriptor file name (`desc2026` -> `2026`). One release is kept: the load of a new
  release replaces all `mesh_term` rows, and `mesh embed` refuses to run while
  `mesh_term_vec.mesh_release` differs from `mesh_term.mesh_release` until it has re-embedded.
  A `confirmation_id` includes the release; after a reload, old ids are refused.
- **PostgreSQL version.** The plan targets 19.0 (after the 2026-11-24 upgrade). The
  `server_version` guard in CLAUDE.md, the scripts and the tests accepts `19.x` from 19.0 on.
- **MCP restart.** Proposals live in memory; after a restart an unknown `confirmation_id` is
  refused and the reviewer translates again. The log is write-only (append, one JSON object per
  line, never read back by the tool).

## 8. Ride-along items (owner 2026-11-24)

- Runbook section 5.19.1 step 13.4 names `EPISTEME_RAW_ROOT=D:/episteme/01_raw` for
  `run_pipeline.sh mesh graph`.
- The graph phase warns when it finds no raw file (section 5.1).
- Drift log: the 0017 rollout results (16 reader calls, no timeout; 3 calls at 14 to 20 s
  accepted by the owner; the 10 s tuning parked) and the PostgreSQL 19.0 upgrade.
