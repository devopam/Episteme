# SP4 — Structured Serializers (spec-delta)

**Parent:** `docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md` — this is the
SP4 spec-delta. Roadmap §3 (SP4 charter), §4.2 (`article_schema` refinement
protocol), §4.10 (library choices — DuckDB for SQL-shaped transforms), and §4.11
(idempotency & restart, frozen) are the contract this refines. Resolves roadmap
§6 open items 1 (OpenAlex scope) and 2 (UniProt licence). Records the decisions
from the 2026-09-15 brainstorm.

**Depends on:** SP1 (merged, PR #1) — `article_schema` v1.4, `staging_writer`,
`checkpoint_markers`, `postgres_loader`, `load_articles`, `graph_builder`,
`corpus_materializer`, `audit_trail`, `db/schema.sql` + `migrations/0001`.
SP2 (merged, PR #3) — `migrations/0002` (`container_id`/`book_meta`/
`article_parts`), `graph_builder`'s multi-derivation shape, `run_pipeline.sh`'s
`LIT_SOURCES`-gated stage-chain pattern (the template SP4's `STRUCTURED_SOURCES`
chain copies), `tests/conftest.py`'s DB-isolation guards. SP3 (merged, PR #2) —
`_lib` fetch engine, `sources.env`, `download_<source>.sh` for all 8 structured
sources (chembl, uniprot, pubchem, clinvar, reactome, mesh, ontologies, openalex).

**Scope in one line:** turn the downloaded structured/tabular sources —
`chembl`, `uniprot`, `pubchem`, `clinvar`, `reactome`, `mesh`, `ontologies`,
`openalex` (bounded biomedical subset) — into `episteme.articles` prose rows
via template-based serialization, one source at a time, each gated by a
field-shape report + schema sign-off. Plus a `mesh_hierarchy` descriptor-tree
graph and a general loader hardening against cross-`source_file` `id`
collisions on repeated full-dump releases. **No SP2 literature work (merged);
no SFT/eval datasets; no `chunks`/RAG; no LLM-based prose rewriting.**

---

## 1. Architecture — one branch, one PR, three phases

Mirrors SP2's shape exactly. One branch `sp4-structured-serializers`, one PR,
subagent-driven-development, three phases:

- **Phase A** — groundwork proven against a small fixture: the general
  cross-`source_file` id-collision hardening in `postgres_loader` (§2),
  `run_pipeline.sh`'s `serialize` stage + `STRUCTURED_SOURCES` dispatch chain
  (§5), and the `mesh_hierarchy` table + migration (§3).
- **Phase B** — eight `serialize_<source>.py` modules, each following the
  common shape (§4), each wired into `run_pipeline.sh`'s chain, each gated by
  a field-shape report + real end-to-end proof before its task is done (§6).
  `mesh` additionally gets a `graph_mesh.sh` stage (the only structured source
  that does, this phase).
- **Phase C** — full-branch sweep + drift-log entry, mirroring SP2 Task 13.

No new top-level architecture decision is needed beyond SP2's established
pattern — SP4 is "the same machine, different fuel": literature extractors
pulled prose out of documents; structured serializers generate prose from
tabular/graph records. The `episteme.articles` contract, the `--report`
sign-off gate, the DELETE+COPY idempotency model, and the SDD execution
process all carry over unchanged.

---

## 2. Loader hardening — cross-`source_file` id collisions (Phase A)

### 2.1 The gap

`postgres_loader.load_source_file`'s idempotency (§4.11, frozen) is scoped to
one `source_file`: re-running the same file safely replaces its rows
(`DELETE FROM episteme.articles WHERE source_file = ANY(%s) AND source = %s`).
`episteme.articles` carries **no unique constraint on `id`** — nothing in the
database stops two *different* `source_file`s from writing rows that share an
`id`. SP2 hit exactly this shape once (Task 8, `europepmc_manuscript`: two
archive-format families over the same accession range collided on `id` until
caught and fixed with a per-format id suffix) — there, it was one source's
local bug. SP4 makes the underlying hazard structural rather than incidental:
every one of the 8 targets is a periodic full-dump release (ChEMBL ships a new
full SQLite release per version, ClinVar republishes `variant_summary`
weekly, UniProt/PubChem/Reactome/MeSH/ontologies/OpenAlex all republish full
snapshots under new filenames) — "the same native records reappearing under a
new `source_file`" is the *normal* re-run shape for this whole source class,
not an edge case.

### 2.2 The fix — extend the existing belt-and-braces pattern

`article_body`'s delete already guards against this (`postgres_loader.py`,
current):
```sql
DELETE FROM episteme.article_body
 WHERE article_id IN (
         SELECT id FROM episteme.articles WHERE source_file = ANY(%s) AND source = %s
       )
    OR (article_id = ANY(%s) AND source = %s)
```
the `articles` delete immediately above it does not carry the same `OR`
clause. Fix: add it.

```sql
DELETE FROM episteme.articles
 WHERE (source_file = ANY(%s) AND source = %s)
    OR (id = ANY(%s) AND source = %s)
```
(`%s` params: incoming `source_files` list, `source`, incoming shard's `id`
list, `source` — same four-parameter shape the `article_body` delete already
uses.) This must run **before** the per-file `articles` DELETE loop that
follows it (order matters: the id-collision delete needs to see rows from
*other* `source_file`s that the per-file loop won't touch).

This is: (a) shared code — fixes the hazard once for every source, including
retroactively hardening SP1–SP3 sources that happen to hit it in production;
(b) backward-compatible — a no-op wherever ids never collide across files,
which is every current source's actual behavior; (c) the same pattern already
shipped, tested, and reviewed for `article_body` — not a new invented
mechanism; (d) directly closes the hole SP4's release-shaped sources are most
exposed to.

### 2.3 Test

A new `pg` test in `tests/data/test_postgres_loader.py` (existing file):
load a shard for `source_file="a.csv"` containing `id="chembl:CHEMBL25"`,
commit; load a second shard `source_file="b.csv"` — different `source_file`,
same `id`, different content — commit; assert `episteme.articles` has
exactly ONE row for `id="chembl:CHEMBL25"`, holding `b.csv`'s content (the
later load wins, matching DELETE+re-INSERT semantics elsewhere). This is
Phase A's first task, landed and tested before any of the 8 serializers are
written, so every serializer's own real end-to-end proof benefits from it.

---

## 3. `mesh_hierarchy` — descriptor-tree graph (Phase A)

### 3.1 Why a new table

`episteme.article_mesh` (existing, SP1) is shaped `(pmid, descriptor_ui,
descriptor_name, major_topic, qualifiers, source_file)` — an **article↔term**
edge table. MeSH's own controlled-vocabulary structure is a **term↔term**
parent/child hierarchy (via `TreeNumberList` prefix relationships, e.g.
`C04.588.443.550`'s immediate parent is the descriptor whose own tree number
is `C04.588.443`) — a different shape entirely. Reusing `article_mesh` for
this would conflate the two; a dedicated table keeps both correct.

### 3.2 `migrations/0003_mesh_hierarchy.sql`

Idempotent, mirrors `0002`'s style:

```sql
-- SP4 migration 0003 -- episteme.mesh_hierarchy (descriptor parent/child edges).
-- Idempotent. NOTE: on this build migrate_database.sh dies at 0001 (pgvector
-- not installed) and never reaches this file automatically -- apply directly:
--   psql -d <db> -f src/episteme/data/db/migrations/0003_mesh_hierarchy.sql
-- (idempotent -- safe to re-run). Untracked in episteme._migrations when
-- applied this way (see 0002's identical caveat).

CREATE TABLE IF NOT EXISTS episteme.mesh_hierarchy (
    parent_descriptor_ui text NOT NULL,
    child_descriptor_ui  text NOT NULL,
    source_file          text NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS mesh_hierarchy_uq
    ON episteme.mesh_hierarchy (parent_descriptor_ui, child_descriptor_ui);
CREATE INDEX IF NOT EXISTS mesh_hierarchy_src_idx
    ON episteme.mesh_hierarchy (source_file);

GRANT SELECT, INSERT, DELETE ON episteme.mesh_hierarchy TO episteme_app;
```

No partitioning — MeSH has ~30K descriptors (small relative to
`article_cites`/`article_mesh`'s per-article scale); a plain table is
sufficient. Grant includes `DELETE` from the start (SP2's Task-13 review
caught `article_parts` missing this — apply the lesson here directly rather
than re-discover it).

### 3.3 `graph_builder` — fourth derivation

`graph_builder.build()` gains a fourth, source-scoped derivation (alongside
pmc cites/mesh, the SP2 `article_parts` derivation, and this one), triggered
only for `source="mesh"`: parse each `mesh` articles row's stored
`TreeNumberList` (carried in the row — see §4.7's `mesh` per-source table)
and for every tree number, compute its immediate-parent prefix; if that
prefix matches another descriptor's own tree number, emit a
`(parent_descriptor_ui, child_descriptor_ui)` edge. Idempotent
DELETE-by-`source_file` + INSERT `ON CONFLICT (parent_descriptor_ui,
child_descriptor_ui) DO NOTHING`, same pattern as `article_parts`.
`neighbours(kind="mesh_parent")` / `neighbours(kind="mesh_child")` added
alongside the existing `kind` values.

---

## 4. The eight source serializers (Phase B)

### 4.1 Common shape

Every `serialize_<source>.py`:

```python
def serialize_<source>(raw_dir, processed_dir, *, max_files=0, force=False,
                       workers=1, verbose=False) -> dict
    # {"inputs", "ok", "failed", "rows"}
def main(argv: list[str] | None = None) -> int
    # --raw-dir --processed-dir --max-files --force --workers --verbose --report
```

Identical contract to SP2's `extract_<source>()` shape (§4.7 of the roadmap,
frozen) — `serialize_` is simply the structured-source verb per roadmap §4.1's
frozen `scripts/data/` layout, in place of `extract_`. `--report` prints a
field-shape table and writes NO rows/markers/audit, honoring `--max-files`
(SP2's Task-5 M1 lesson, applied here from the start rather than backported
later).

**Reading layer** (roadmap §4.10, frozen): DuckDB for anything SQL-shaped —
ChEMBL's SQLite release via `sqlite_scanner`, ClinVar's TSV and PubChem's
property tables via DuckDB's native CSV/TSV reader — letting each serializer
express row selection as SQL rather than hand-rolled parsing loops. Streaming
native parsers where DuckDB doesn't fit: `defusedxml.iterparse` for the MeSH
descriptor XML (matching the security posture `jats.py`/SP2 established —
never bare `xml.etree`), a small line-oriented FASTA parser for UniProt (the
pre-restructure prototype's `serialize_structured_sources.py::
process_uniprot_fasta`/`serialize_uniprot` header-splitting logic is a
reasonable starting point to port and tighten, not a from-scratch build), and
`pronto` (MIT-licensed, the standard lightweight OBO/OWL reader) for the
`ontologies` group — reused for MeSH's own descriptor-tree parsing only if its
XML shape doesn't fit `pronto`'s OBO/OWL model (MeSH ships as a bespoke XML
schema, not OBO — `defusedxml.iterparse` is the primary path there; confirmed
per-source at implementation).

**Prose generation:** template-based declarative sentences, no LLM rewriting
(matches SP2's precedent and roadmap §5 non-goals) — e.g. *"Compound CHEMBL25
(SMILES `CC(=O)Oc1ccccc1C(=O)O`) exhibits IC50 = 1.2 nM against Cyclooxygenase-2
(ChEMBL bioactivity record)."* Exact wording tightened per-source at
field-shape sign-off, same as SP2's apollo drop-heuristic / guidelines subset
decisions.

**Identity/licence conventions** (roadmap §SP4, frozen): `id =
f"{source}:{native_id}"`; `subset` always computed via
`article_schema.normalize_license`/`subset_from_license` — never a hardcoded
literal, except where a source's own governance call mandates one (uniprot,
below; mirrors SP2's `europepmc_manuscript` `text_mining` hardcode pattern,
with the same "comment explaining why, verified never `commercial`" bar).
Sparse bib fields (`title`, `journal`, `year`, `authors`) populated only where
the structured record genuinely carries them — mostly `NULL` for compound/
variant/pathway records, same posture as SP2's bookshelf part rows.
`container_id`/`book_meta` = `None` on every SP4 row (no book-shaped source
in this batch). `pmid`/`pmcid`/`doi` = `None` except `openalex`, which
frequently carries a real `doi`.

**Idempotency/fixtures:** one unit of work = one input file/table slice
(matching roadmap §4.11's frozen "unit of work = one input file" — for
ChEMBL's SQLite release this means one release-version file, not one row);
hand-built small fixture per source for unit tests; a real end-to-end proof
against genuine (possibly partial/sliced) upstream data before sign-off —
full-scale runs on the multi-GB sources (ChEMBL full SQLite, PubChem,
OpenAlex snapshot) are pre-authorized to stop at "downloaded + verified
against a trimmed real slice," full run = ops, matching SP2's Task 11/12
precedent.

### 4.2 Per-source table

| Source | Raw format (from SP3 download) | Unit of work / prose content | Licence / `subset` |
|---|---|---|---|
| **chembl** | SQLite release (default `download_chembl.sh` mode) + SDF + chemreps | DuckDB `sqlite_scanner` joining `compound_structures`/`activities`/`target_dictionary`; 1 row per bioactivity record | CC BY-SA 3.0 — `normalize_license`'s existing `BY-SA` arm → `commercial`. No `article_schema` change needed. |
| **uniprot** | Swiss-Prot FASTA (curated set only, no TrEMBL, per `download_uniprot.sh`) | 1 row per protein entry, header + sequence → prose. Implementer checks whether the richer `.dat`/XML flat-file format (function/disease/subcellular-location annotations) is also reachable via the existing download wrapper — better prose than FASTA-header-only if so; not a blocker if not. | CC BY-ND 4.0 → **`subset="text_mining"` hardcoded** (user ruling, 2026-09-15): serializing sequences to prose is arguably a "derivative" an ND licence forbids; conservative posture, excluded from the commercial shard, mirrors `europepmc_manuscript`'s pattern. `license_raw` carries the real licence string; `normalize_license` is still called for the `(code, url, raw)` tuple but `subset` is NOT taken from `subset_from_license`'s result — same shape as the manuscript hardcode. |
| **pubchem** | `compound_extras` mode — per-compound property TSVs (CID/SMILES/IUPAC name/formula/synonyms) | DuckDB TSV read; 1 row per CID | Public-domain (NIH). May need a small `normalize_license` addition if no existing arm fits a PubChem licence string — flagged for implementation, not decided here; default `unknown`→`open_metadata` if genuinely no match, same conservative-fallback posture as every other source. |
| **clinvar** | `variant_summary.txt.gz` (tsv, default `download_clinvar.sh` mode) | DuckDB TSV read; 1 row per variant record | Public-domain (NCBI). |
| **reactome** | Flat release directory (pathway relationship + gene/protein mapping files) | 1 row per pathway description; gene→pathway membership sentences where the mapping files support it | CC0 (Reactome's stated licence) → `commercial` via `normalize_license`'s existing `CC0` arm. |
| **mesh** | NLM descriptor XML | 1 row per descriptor (`DescriptorName` + `ScopeNote` → prose); row also carries the raw `TreeNumberList` for `graph_builder`'s hierarchy derivation (§3.3) — **not** a new `article_schema` column; a source-local field folded into the prose-generation step, not persisted structurally (confirm at implementation whether it needs a transient carry-through field or can be re-derived from `raw_dir` directly inside `graph_builder`; either is acceptable, the loaded `articles` row itself does not need a new column). | Public-domain (NLM). |
| **ontologies** (GO/HPO/MONDO/UCUM) | OBO/OWL, fixed item list, no MODE | `pronto` per term → prose (`name` + `def`/scope). Flat rows only — **no** hierarchy graph table for ontologies this phase (§3 scopes the graph stage to `mesh` alone; a generalized ontology-hierarchy table is a plausible SP4+ follow-up, not in scope here). | Each ontology's own licence (GO/HPO/MONDO are typically CC-BY variants; UCUM's licence is distinct) — decided per-file at each ontology's own field-shape sign-off, since "ontologies" is one source token covering four independently-licensed vocabularies. |
| **openalex** | `works_jsonl` mode, **bounded biomedical subset** (user ruling, 2026-09-15 — resolves roadmap §6 item 1): filter to biomedical-relevant OA works not already covered by `pmc`/`pubmed` (exact filter — subject/concept tag match, venue allow-list, or a DOI-prefix/PMID-absence check — decided at this task's own design pass, not fixed here) | 1 row per work — title/authors/year/venue/DOI are real, populated fields (closest to a "literature" shape of the 8). Abstract requires reconstruction: OpenAlex ships `abstract_inverted_index` (a word→position-list map), not plain text — reconstruct to a string before it enters `text`/`abstract`. | CC0 (OpenAlex's stated licence) → `commercial` via the existing `CC0` arm, confirmed at sign-off against the real dataset's licence field (same "read the real card, don't assume" discipline SP2's guidelines task used). |

---

## 5. `run_pipeline.sh` wiring & orchestration (Phase A)

Mirrors SP2's `LIT_SOURCES`/`_is_lit` pattern exactly, substituting the
structured-source verb:

```sh
STRUCTURED_SOURCES="chembl uniprot pubchem clinvar reactome mesh ontologies openalex"
_is_structured() { case " $STRUCTURED_SOURCES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
```

Add `serialize` to the `STAGE` case list (`all|download|extract|load|graph|
materialize|enrich|serialize`). Non-pmc, non-corpus dispatch (both the
early-validation block and the dispatch `else` block) gains a `serialize)`
arm structurally identical to SP2's `extract)` arm: `_is_structured "$SOURCE"
|| die "$SOURCE serialize is not in SP4 — <other territory>" 3`, dispatch to
`$HERE/$SOURCE/serialize_$SOURCE.sh`. `load` reuses the existing generic
`load)` arm unchanged (`load_articles.py --source <structured-source>` needs
no source-specific branching — it already works for any `source` whose
`ARTICLE_COLUMNS`-shaped shard exists in staging). `graph` is gated: only
`mesh` gets a `graph_mesh.sh` wrapper; every other structured source's
`graph` stage stays unwired (`die 3`), matching `europepmc_manuscript`'s
precedent from SP2 (a source can legitimately have no graph contribution).
`<structured-source> all` chains `download → serialize → load` (+ `graph` only
for `mesh`), mirroring SP2's lit-source `all` chain shape.

Wrapper placement follows SP2's established (twice-independently-discovered,
Tasks 7 and 8) rule: `run_pipeline.sh`'s dispatch builds
`$HERE/$SOURCE/${STAGE}_${SOURCE}.sh` using the literal `$SOURCE` token — so
`serialize_<source>.sh`/`load_<source>.sh`/`graph_mesh.sh` must live wherever
that literal-token path resolves, which for all 8 structured sources (none of
which nest under a parent grouping directory the way `europepmc/*` do) is
simply flat at `scripts/data/<source>/` — the same directory SP3's
`download_<source>.sh` already lives in. No flat-vs-nested split is expected
here (unlike europepmc's sources) — confirmed at Phase A, not assumed.

`usage()` gets no new alias line (SP4 adds no `corpus`-shaped carve-out) but
should list `serialize` in its stage enumeration.

---

## 6. Testing & sign-off (inherited from SP2, unchanged)

Same discipline as SP2: unit tests (`not pg`) per serializer against a
hand-built fixture; a `--report` field-shape table; a human/controller
sign-off recorded in the SDD ledger (`Ledger: <source> field-shape signed-off
— <who> — <date>`); a real end-to-end proof (`serialize --max-files N` on a
real, possibly-partial download, `load`, and — for `mesh` only — `graph`)
against `episteme_test` before a task is marked complete. `pg`-marked tests
target `episteme_test` exclusively (`tests/conftest.py`'s hardened
DB-isolation guard, SP2 Task-review fix wave, already covers this
project-wide — no new guard needed for SP4). `mesh`'s hierarchy derivation
gets its own `pg` end-to-end test proving both the `mesh_hierarchy` rows and
`neighbours(kind="mesh_parent"/"mesh_child")`, same shape as SP2's bookshelf
`article_parts` proof (§3.3 of the SP2 spec).

---

## 7. Non-goals

- Any `serialize_`/`articles` rows for `dailymed`, `openfda`, `aact`
  (roadmap §5, frozen — unchanged by SP4).
- Cross-source deduplication beyond `corpus_materializer`'s existing
  `content_hash` near-dup pass (roadmap §5).
- LLM-based or model-assisted prose generation/rewriting — template-based
  sentences only, matching SP2's precedent.
- A generalized ontology-hierarchy graph table (GO/HPO/MONDO/UCUM parent/
  child edges) — `mesh_hierarchy` is scoped to MeSH alone this phase; a
  follow-up if a future phase needs cross-ontology graph traversal.
- Full-scale production runs of the multi-GB sources (ChEMBL full SQLite,
  PubChem, OpenAlex snapshot, the full MeSH XML if larger than a task's time
  budget) — each task's real end-to-end proof against a genuine partial/
  sliced download is the gate; full-corpus ingestion is ops, same posture as
  SP2's `id_mappings`/`lite_metadata` precedent (though `id_mappings` itself
  *did* complete its full 42.3M-row load — SP4 tasks should attempt the same
  and only fall back to a slice if genuinely impractical within budget).
- Stream 2 / RAG / knowledge-layer design (`pgvector`/`pg_search` usage,
  multi-tenant RBAC) — Phase 1 (roadmap §5, frozen).
- Model training changes.

---

## 8. Open items carried forward

| # | Item | Resolution point |
|---|---|---|
| 1 | Exact OpenAlex biomedical-subset filter (subject/concept tags vs. venue allow-list vs. DOI/PMID-absence check) | `openalex`'s own task, at implementation |
| 2 | `pubchem` licence string → `normalize_license` arm, if none currently matches | `pubchem`'s own task, at field-shape sign-off |
| 3 | Whether `mesh`'s `TreeNumberList` needs a transient carry-through field or is re-derivable by `graph_builder` directly from `raw_dir` | `mesh`'s own task, at implementation |
| 4 | Each of the four `ontologies` vocabularies' (GO/HPO/MONDO/UCUM) individual licence → `subset` mapping | `ontologies`' own task, at field-shape sign-off (four separate calls, one source token) |
| 5 | Whether UniProt's richer `.dat`/XML flat-file format is reachable via the existing SP3 download wrapper (better prose than FASTA-header-only) | `uniprot`'s own task, at implementation — not a blocker if unavailable |
| 6 | A generalized ontology-hierarchy graph table (deferred, not scoped this phase) | future SP, if RAG/traversal needs it |

---

## Document control

| Version | Date | Notes |
|---|---|---|
| v1 | 2026-09-15 | Initial SP4 spec-delta from the 2026-09-15 brainstorm. |
