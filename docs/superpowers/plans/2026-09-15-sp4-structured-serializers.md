# SP4 — Structured Serializers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the downloaded structured/tabular sources (`chembl`, `uniprot`, `pubchem`, `clinvar`, `reactome`, `mesh`, `ontologies`, `openalex`) into `episteme.articles` prose rows via template-based serialization, one source at a time, each gated by a field-shape report and a schema sign-off; plus a MeSH descriptor-tree graph and a general `postgres_loader` hardening against cross-`source_file` id collisions.

**Architecture:** Three phases on one branch. **Phase A** does groundwork: hardens `postgres_loader` against id collisions across repeated full-dump releases (the normal re-run shape for this whole source class), wires `run_pipeline.sh`'s `serialize` stage + `STRUCTURED_SOURCES` dispatch chain (proven against a `chembl` scaffold), and adds `episteme.mesh_hierarchy` + `graph_builder`'s fourth derivation. **Phase B** writes eight `serialize_<source>.py` modules in a common shape, each wired into `run_pipeline.sh`. **Phase C** does the full-branch sweep + drift-log entry.

**Tech Stack:** Python 3.10+ (DuckDB for SQL-shaped reads — `sqlite_scanner`/CSV/TSV, `defusedxml` streaming `iterparse` for MeSH's descriptor XML, `pronto` for OBO/OWL ontologies, `polars` lazy where DuckDB doesn't fit — no pandas per roadmap §4.10), PostgreSQL 19beta3 (`localhost:5433`, partitioned per ADR-0002), bash (`_lib/common.sh` `exec "$PY" -m` wrappers), pytest (`pg` / `not pg` marks).

**Spec:** `docs/superpowers/specs/2026-09-15-sp4-structured-serializers-design.md` (SP4 spec-delta) — refines `docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md` §3/§4.2/§4.10/§4.11 (frozen). Read both. SP2's merged plan/spec (`docs/superpowers/plans/2026-09-08-sp2-literature-extractors.md`, `docs/superpowers/specs/2026-09-08-sp2-literature-extractors-design.md`) is the shape template for every Phase-B task — read `extract_pubmed.py` (`src/episteme/data/pubmed/extract_pubmed.py`) as the canonical module idiom before writing any `serialize_<source>.py`.

## Global Constraints

- **Branch:** `sp4-structured-serializers` (already checked out; the spec landed on it as `2cc0e01`). Base `main` @ `9ef079a`.
- **`config.py` is the ONLY module in `src/episteme/` that reads `os.environ` / `os.getenv`.** Audit run-id comes from `get_settings().run_id` (`EPISTEME_RUN_ID`), never a direct env read in a serializer. All 8 structured sources' endpoint vars already exist in `scripts/data/_lib/sources.env` (SP3) — no new `config.py` fields expected; flag and add one only if a real task genuinely needs it.
- **`article_schema.ARTICLE_COLUMNS` is untouched by SP4** — no new columns. Every SP4 row fits the existing v1.4 shape: `container_id`/`book_meta` = `None` on every row (no book-shaped source this phase); `pmid`/`pmcid`/`doi` = `None` except `openalex` (frequently has a real `doi`); sparse bib fields (`title`/`journal`/`year`/`authors`) filled only where the record genuinely carries them. `"chembl","uniprot","pubchem","clinvar","reactome","mesh","ontologies","openalex"` are ALREADY in `article_schema.SOURCES` (added ahead of time in SP1-α) — confirm, do not re-add.
- **`id = f"{source}:{native_id}"`** for every row (frozen, roadmap §SP4). `subset` always via `article_schema.normalize_license`/`subset_from_license` — **except** `uniprot`, which MUST hardcode `subset="text_mining"` regardless of what `subset_from_license` would compute (CC BY-ND currently normalizes to `"CC BY-ND"` → `subset_from_license` returns `"commercial"` — this is EXACTLY the interpretation the user's ruling rejects; do not call `subset_from_license`'s return value through unchanged for uniprot's final `subset` field, mirror SP2's `europepmc_manuscript` hardcode-with-comment pattern instead).
- **Every serializer:** importable `serialize_<source>(raw_dir, processed_dir, *, max_files=0, force=False, workers=1, verbose=False) -> {"inputs","ok","failed","rows"}` + `main(argv)` with `--raw-dir --processed-dir --max-files --force --workers --verbose --report` (mirrors roadmap §4.7's frozen extractor CLI, `serialize_` in place of `extract_`). `--report` prints a field-shape table (null-rate per column, `extract_status` histogram, `subset`/`license` breakdown) and writes NO rows/markers/audit, **honoring `--max-files`** (SP2 Task-5 M1 lesson, applied from the start here — do not repeat the pubmed gap that had to be backported at whole-branch review).
- **Per-source gate:** a source's task does NOT complete until its field-shape `--report` output is in the ledger with a human sign-off line (`Ledger: <source> field-shape signed-off — <who> — <date>`), then a real end-to-end runs against `episteme_test` (`serialize --max-files N` on real or genuinely-sliced-real upstream data, `load`, and — `mesh` only — `graph`). Full-scale multi-GB runs (ChEMBL full SQLite, PubChem, OpenAlex snapshot) may stop at "downloaded + verified against a trimmed real slice, full run = ops" — but ATTEMPT the full run first if the budget allows (SP2's `europepmc_id_mappings` completed a full 42.3M-row real load; don't reach for the slice-only fallback as a default).
- **`postgres_loader.load_source_file`'s `articles` DELETE gets an id-collision guard** (Task 1) BEFORE any serializer lands — every serializer's own real end-to-end proof benefits from this being in place first.
- **Migrations are idempotent** (`IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`), tracked (when reachable) in `episteme._migrations`. On this PostgreSQL build, `migrate_database.sh` dies at `migrations/0001` (pgvector not installed) and never reaches later migrations automatically — `migrations/0003_mesh_hierarchy.sql` must be applied directly (`psql -d <db> -f ...`) and say so in its own header comment (SP2's whole-branch-review Fix 2 lesson — state this at the point of use, not buried in a drift-log sub-clause).
- **`pytest -q -m "not pg"` stays green** (currently 113 passed / 1 skipped — confirm exact count at Task 1 start via `git log`/a fresh run, the SP2 merge may have shifted it slightly). SP4 adds unit tests per serializer + a loader-hardening test + a `mesh_hierarchy` migration test. **`pytest -q` (pg, `.env` sourced)** currently 131 passed / 2 skipped; SP4 adds the mesh-hierarchy `pg` end-to-end + the loader-hardening `pg` test + a `pg` test per serializer that needs one.
- **`pg`-marked tests target `episteme_test`** (`TEST_PG_DSN` from `.env`), NEVER the real `episteme` DB. Any manual DB verification uses `PGDATABASE=episteme_test`. `tests/conftest.py`'s hardened DB-isolation guard (SP2 whole-branch-review Fix 4) and `tests/data/conftest.py`'s DSN-validation guard (Fix 5) already cover this project-wide — no new guard needed.
- **`bash -n` clean on every `.sh` touched;** `shellcheck --severity=warning` (CI) clean; `ruff` clean (pre-commit enforces).
- **Wrapper placement:** `run_pipeline.sh`'s dispatch builds `$HERE/$SOURCE/${STAGE}_${SOURCE}.sh` using the literal `$SOURCE` token. None of the 8 structured sources nest under a parent grouping directory (unlike `europepmc/*`), so every wrapper is flat at `scripts/data/<source>/` — the same directory `download_<source>.sh` (SP3) already lives in. Confirm this at Task 2, do not assume a flat-vs-nested split is needed (SP2 Tasks 7/8 hit that split for `europepmc_*` sources specifically; it does not apply here).
- **Commits end with:**
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```

---

## File-structure map

**Created:**
- `src/episteme/data/db/migrations/0003_mesh_hierarchy.sql` — `episteme.mesh_hierarchy` table + unique index.
- `src/episteme/data/chembl/__init__.py`, `src/episteme/data/chembl/serialize_chembl.py`.
- `src/episteme/data/uniprot/__init__.py`, `src/episteme/data/uniprot/serialize_uniprot.py`.
- `src/episteme/data/pubchem/__init__.py`, `src/episteme/data/pubchem/serialize_pubchem.py`.
- `src/episteme/data/clinvar/__init__.py`, `src/episteme/data/clinvar/serialize_clinvar.py`.
- `src/episteme/data/reactome/__init__.py`, `src/episteme/data/reactome/serialize_reactome.py`.
- `src/episteme/data/mesh/__init__.py`, `src/episteme/data/mesh/serialize_mesh.py`.
- `src/episteme/data/ontologies/__init__.py`, `src/episteme/data/ontologies/serialize_ontologies.py`.
- `src/episteme/data/openalex/__init__.py`, `src/episteme/data/openalex/serialize_openalex.py`.
- `scripts/data/<source>/{serialize,load}_<source>.sh` for all 8 sources; `scripts/data/mesh/graph_mesh.sh` additionally.
- `tests/data/test_serialize_{chembl,uniprot,pubchem,clinvar,reactome,mesh,ontologies,openalex}.py`, `tests/test_migrations_0003.py` (or added to the existing `tests/test_migrations.py` — implementer's call, see Task 3), `tests/data/test_mesh_hierarchy_end_to_end.py` (`pg`).
- `tests/fixtures/sp4/<source>/…` — one tiny real-shaped input per source.

**Modified:**
- `src/episteme/data/postgres_loader.py` — id-collision DELETE guard.
- `src/episteme/data/graph_builder.py` — `_GRAPH_SOURCES` gains `"mesh"`; fourth derivation (`mesh_hierarchy`); `neighbours` gains `kind="mesh_parent"`/`kind="mesh_child"`.
- `scripts/data/run_pipeline.sh` — `serialize` stage added to the `STAGE` case list + dry-run reject list; `STRUCTURED_SOURCES`/`_is_structured` allow-list; early-validation `serialize)` arm; `load)` arm extended to accept structured sources; `extract|graph)` split so `graph` also allows `mesh`; dispatch `else` block gains a `serialize)` arm and restructures `all)` for structured sources.
- `tests/data/test_postgres_loader.py` — new id-collision test (+ the file's pre-existing relative schema-file opens — apply the now-established `REPO_ROOT`-absolute-path fix while touching this file, matching the standard the SP2 whole-branch-review fix wave already set project-wide; not the task's primary point, but cheap and consistent).
- `tests/data/test_graph_builder.py` — new mesh_hierarchy derivation test (or a new file — implementer's call, see Task 4).
- `docs/project-incubation-baseline.md` — drift-log entry (Task 13).

---

### Task 1: `postgres_loader` — cross-`source_file` id-collision hardening

**Files:**
- Modify: `src/episteme/data/postgres_loader.py`
- Test: `tests/data/test_postgres_loader.py`

**Interfaces:**
- No signature change to `load_source_file(conn, *, source, staging_path, run_id) -> dict`. Behavior change only: a pre-existing row sharing an incoming shard's `id` under a DIFFERENT `source_file` is now deleted before the fresh COPY, not left to silently coexist.

- [ ] **Step 1: Write the failing test** — add to `tests/data/test_postgres_loader.py`, a new test function:

```python
def test_id_collision_across_source_files_is_replaced(pg_conn, tmp_path, monkeypatch):
    """A full-dump re-release under a NEW source_file (e.g. chembl_35.db ->
    chembl_36.db) reusing a native id from the OLD file must replace the old
    row, not coexist with it -- the normal re-run shape for periodic
    full-dump sources (roadmap SP4 spec section 2)."""
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import importlib
    import episteme.config as cfg
    importlib.reload(cfg)
    cfg.get_settings.cache_clear()

    _setup_schema(pg_conn)  # existing helper in this file

    from episteme.data import postgres_loader
    from episteme.data.article_schema import empty_article_row, finalize_row
    from episteme.data.staging_writer import write_rows

    # release A: chembl_35.db carries chembl:CHEMBL25
    row_a = finalize_row({
        **empty_article_row(),
        "id": "chembl:CHEMBL25",
        "source": "chembl",
        "source_file": "chembl_35.db",
        "source_record_id": "CHEMBL25",
        "text": "Compound CHEMBL25 exhibits binding activity old-value.",
        "license": "CC BY-SA",
        "subset": "commercial",
    })
    write_a = write_rows([row_a], tmp_path / "02_processed", source="chembl", source_file="chembl_35.db")
    postgres_loader.load_source_file(
        pg_conn, source="chembl", staging_path=Path(write_a["paths"][0]), run_id="rA"
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE id='chembl:CHEMBL25'")
        assert cur.fetchone()[0] == 1

    # release B: chembl_36.db -- a NEW source_file, but the SAME native id,
    # with different content (the normal shape of a re-release).
    row_b = finalize_row({
        **empty_article_row(),
        "id": "chembl:CHEMBL25",
        "source": "chembl",
        "source_file": "chembl_36.db",
        "source_record_id": "CHEMBL25",
        "text": "Compound CHEMBL25 exhibits binding activity new-value.",
        "license": "CC BY-SA",
        "subset": "commercial",
    })
    write_b = write_rows([row_b], tmp_path / "02_processed", source="chembl", source_file="chembl_36.db")
    postgres_loader.load_source_file(
        pg_conn, source="chembl", staging_path=Path(write_b["paths"][0]), run_id="rB"
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        # exactly ONE row for this id -- the old chembl_35.db row must be gone
        cur.execute("SELECT count(*) FROM episteme.articles WHERE id='chembl:CHEMBL25'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT source_file FROM episteme.articles WHERE id='chembl:CHEMBL25'")
        assert cur.fetchone()[0] == "chembl_36.db"
        cur.execute("SELECT text FROM episteme.article_body WHERE article_id='chembl:CHEMBL25'")
        assert "new-value" in cur.fetchone()[0]
        # no orphaned article_body row from the old release
        cur.execute(
            "SELECT count(*) FROM episteme.article_body ab "
            "JOIN episteme.articles a ON a.id = ab.article_id "
            "WHERE ab.article_id = 'chembl:CHEMBL25'"
        )
        assert cur.fetchone()[0] == 1
```
Also apply the `REPO_ROOT`-absolute-path fix to this file's `_setup_schema` (currently `open("src/episteme/data/db/extensions.sql")` — a bare relative path; SP2's whole-branch-review fix wave already fixed this exact pattern in `test_bookshelf_end_to_end.py`/`test_load_id_mappings.py`/`test_enrich_from_lite.py` — mirror that: `REPO_ROOT = Path(__file__).resolve().parents[2]`, `open(REPO_ROOT / "src/episteme/data/db/extensions.sql")`).

- [ ] **Step 2: Run — verify it fails.** `set -a; . ./.env; set +a; .venv/Scripts/python.exe -m pytest -q tests/data/test_postgres_loader.py::test_id_collision_across_source_files_is_replaced` → the second `count(*)` assertion fails (2 rows survive, not 1).

- [ ] **Step 3: Implement the fix.** In `load_source_file`, immediately BEFORE the existing per-`source_file` DELETE loop (the block currently commented `# (d) articles, per source_file, so _lineage gets per-file counts.`), add a new id-collision guard:

```python
        # (d0) id-collision guard: a periodic full-dump re-release (SP4's
        # normal re-run shape) may reuse a native id under a DIFFERENT
        # source_file than the one that first wrote it. Delete any such row
        # BEFORE the per-file loop below, so a stale row under an old
        # filename never survives alongside the fresh one, and so the
        # per-file _lineage counts (loop below) aren't inflated by rows this
        # step already removed. Scoped by source, same as every other delete
        # here (source_file basenames are not globally unique across sources).
        cur.execute(
            "DELETE FROM episteme.articles "
            "WHERE id = ANY(%s) AND source = %s AND NOT (source_file = ANY(%s))",
            (deleted_ids, source, source_files),
        )
        deleted += cur.rowcount
```
(`deleted_ids` and `source_files` are already computed above this point in the function — reuse them, do not recompute.) Note this delete does NOT touch `article_body` — the existing `article_body` delete above it (step (c) in the function) already has its own `OR (article_id = ANY(%s) AND source = %s)` clause keyed on the incoming shard's ids, which independently catches this same case for `article_body`. Verify this reasoning by re-reading the `article_body` DELETE block before writing this change — do not duplicate an `article_body` delete here.

- [ ] **Step 4: Run — verify it passes.** Same command as Step 2 → green. Then the FULL existing test in this file (`test_load_and_idempotent_replace`) must still pass unchanged — the new delete is a no-op whenever ids don't collide across files, which is every pre-existing test's shape.

- [ ] **Step 5: Run the full `pg` suite** to confirm no regression anywhere else that touches `postgres_loader` (e.g. any SP2 test relying on load semantics): `set -a; . ./.env; set +a; .venv/Scripts/python.exe -m pytest -q`.

- [ ] **Step 6: Commit** — `fix(sp4): postgres_loader guards against cross-source_file id collisions`

---

### Task 2: `run_pipeline.sh` — `serialize` stage + `STRUCTURED_SOURCES` dispatch chain

**Files:**
- Modify: `scripts/data/run_pipeline.sh`
- Create: `src/episteme/data/chembl/__init__.py`, `src/episteme/data/chembl/serialize_chembl.py` (minimal scaffold — Task 5 fills the body)
- Create: `scripts/data/chembl/serialize_chembl.sh`, `scripts/data/chembl/load_chembl.sh`
- Test: additions to `tests/test_run_pipeline_dispatch.py`

**Interfaces:**
- Produces: `run_pipeline.sh <structured-source> serialize|load|all` dispatches per-source wrappers; `run_pipeline.sh mesh graph` dispatches `graph_mesh.sh` (created in Task 9, guarded by `[ -f ]` here same as every not-yet-landed wrapper in this codebase's history). `STRUCTURED_SOURCES` / `_is_structured` set in `run_pipeline.sh`.
- Consumes: nothing new from other tasks.

**Controller-verified current state** (read the full current `scripts/data/run_pipeline.sh` before editing — every line number below may have shifted slightly since this plan was written; locate by the quoted surrounding text, not a hardcoded line number):

1. Top-level `STAGE` validation: `case "$STAGE" in all|download|extract|load|graph|materialize|enrich) ;; *) usage ;; esac`
2. Dry-run reject case list: `case "$STAGE" in extract|load|graph|enrich|materialize|all) [dry-run die] ;; esac`
3. `LIT_SOURCES="pubmed apollo europepmc_manuscript europepmc_preprint guidelines bookshelf"` + `_is_lit()` helper, defined near the `WRAPPER` table.
4. Non-pmc/non-corpus early-validation block, with `download|all)`, `load)` (already special-cases `europepmc_id_mappings`), `extract|graph)`, `enrich)` (already special-cases `europepmc_lite`), and a final `*) die ...` arm.
5. Dispatch `else` block (the non-pmc/non-corpus execution branch), with `download)`, `extract)`, `load)` (already special-cases `europepmc_id_mappings`), `graph)`, `enrich)` (already special-cases `europepmc_lite`), `all)` arms.
6. Args construction section: `lit_extract_args=()` (built from `$MAX_FILES`/`$FORCE`, no `--reason`) is the exact template for a new `struct_serialize_args`.

- [ ] **Step 1: Failing test** — add to `tests/test_run_pipeline_dispatch.py` (follow this file's existing pattern — read a couple of its current tests first, e.g. `test_bookshelf_extract_dry_run_dies_3`/`test_bookshelf_extract_dispatches` for the shape, and SP2's Task-11-review lesson: assert the exact stderr substring, not just the return code):

```python
def test_chembl_serialize_dry_run_dies_3(tmp_path):
    proc = _run(["chembl", "serialize", "--dry-run"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "writes the DB" in proc.stderr

def test_chembl_serialize_dispatches(tmp_path):
    # no data -> the scaffold exits 0 (nothing to do); NOT 3 (dispatch bug)
    proc = _run(["chembl", "serialize", "--max-files", "1"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode in (0, 1), proc.stderr[-2000:]

def test_uniprot_serialize_dry_run_dies_3(tmp_path):
    # a second structured source with no wrapper yet -> caught by the
    # [ -f ] guard, not a dispatch-logic bug (rc 3 either way, but this
    # proves _is_structured("uniprot") is true and the arm is reached)
    proc = _run(["uniprot", "serialize"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "not yet implemented" in proc.stderr or "wrapper not found" in proc.stderr

def test_mesh_graph_wrapper_not_found_dies_3(tmp_path):
    # mesh graph is allowed by the dispatch guard but graph_mesh.sh doesn't
    # exist until Task 9 -- proves the mesh-specific graph carve-out is wired
    # without needing the real wrapper yet.
    proc = _run(["mesh", "graph"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "wrapper not found" in proc.stderr

def test_pubchem_serialize_is_not_a_literature_source(tmp_path):
    # a structured source must NOT be reachable via the SP2 _is_lit gate
    proc = _run(["pubchem", "extract"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "not in SP2" in proc.stderr or "not wired" in proc.stderr
```

- [ ] **Step 2: Run — verify it fails** (`serialize` is not yet a recognized `STAGE`, or `pubchem` may currently die with a different message than expected — run and confirm the actual pre-fix failure mode, don't assume).

- [ ] **Step 3: `run_pipeline.sh` edits.** Apply exactly these six changes; preserve every other line, comment, and guard byte-for-byte:

  **(a) Top-level STAGE validation** — add `serialize`:
  ```sh
  case "$STAGE" in
      all|download|extract|load|graph|materialize|enrich|serialize) ;;
      *) usage ;;
  esac
  ```

  **(b) Dry-run reject case list** — add `serialize`:
  ```sh
  case "$STAGE" in
      extract|load|graph|enrich|materialize|serialize|all)
          [ "${EPISTEME_DRY_RUN:-0}" != "1" ] \
              || die "$SOURCE $STAGE writes the DB — --dry-run is supported on 'download' only" 3 ;;
  esac
  ```

  **(c) `STRUCTURED_SOURCES`/`_is_structured`** — add near the existing `LIT_SOURCES`/`_is_lit` definition:
  ```sh
  STRUCTURED_SOURCES="chembl uniprot pubchem clinvar reactome mesh ontologies openalex"
  _is_structured() { case " $STRUCTURED_SOURCES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
  ```

  **(d) Early-validation block** — modify the existing `load)` arm to also accept structured sources, split `extract|graph)` so `graph` allows `mesh` too, and add a new `serialize)` arm:
  ```sh
  load)
      if [ "$SOURCE" = "europepmc_id_mappings" ]; then
          idmapw="$HERE/europepmc/id_mappings/load_europepmc_id_mappings.sh"
          [ -f "$idmapw" ] || die "wrapper not found: $idmapw (not yet implemented?)" 3
      else
          _is_lit "$SOURCE" || _is_structured "$SOURCE" \
              || die "$SOURCE $STAGE is not in SP2/SP4 (literature/structured)" 3
          litw="$HERE/$SOURCE/load_$SOURCE.sh"
          [ -f "$litw" ] || die "wrapper not found: $litw (not yet implemented?)" 3
      fi
      ;;
  extract)
      _is_lit "$SOURCE" || die "$SOURCE $STAGE is not in SP2 — SP4 (structured serialize)" 3
      litw="$HERE/$SOURCE/extract_$SOURCE.sh"
      [ -f "$litw" ] || die "wrapper not found: $litw (not yet implemented?)" 3
      ;;
  graph)
      _is_lit "$SOURCE" || [ "$SOURCE" = "mesh" ] \
          || die "$SOURCE graph is not wired" 3
      litw="$HERE/$SOURCE/graph_$SOURCE.sh"
      [ -f "$litw" ] || die "wrapper not found: $litw (not yet implemented?)" 3
      ;;
  serialize)
      _is_structured "$SOURCE" || die "$SOURCE $STAGE is not in SP4 — SP2 (literature) territory" 3
      structw="$HERE/$SOURCE/serialize_$SOURCE.sh"
      [ -f "$structw" ] || die "wrapper not found: $structw (not yet implemented?)" 3
      ;;
  ```
  (Keep the existing `enrich)` arm and the final `*)` catch-all unchanged. Pre-declare `structw="${structw:-}"` alongside the existing `wpath`/`idmapw`/`litw` declarations later in the file, same `set -u`-safety pattern SP2 established.)

  **(e) Args construction** — add near `lit_extract_args`:
  ```sh
  # SP4 structured `serialize` wrappers accept only --max-files N and --force --
  # NOT --reason, mirroring lit_extract_args' shape. `load` reuses load_args;
  # `graph` (mesh only) gets no extra args, same as the lit graph arm.
  struct_serialize_args=()
  [ -n "$MAX_FILES" ] && struct_serialize_args+=(--max-files "$MAX_FILES")
  [ "$FORCE" = "1" ] && struct_serialize_args+=(--force)
  ```

  **(f) Dispatch `else` block** — modify the existing `load)` arm's structured-source path is already covered by the flat `$HERE/$SOURCE/load_$SOURCE.sh` else-branch (no change needed there — it already dispatches generically for any non-`europepmc_id_mappings` source). Add a new `serialize)` arm and restructure `all)`:
  ```sh
  serialize) run_stage serialize bash "$HERE/$SOURCE/serialize_$SOURCE.sh" "${struct_serialize_args[@]}" ;;
  ```
  and replace the existing `all)` arm's body with:
  ```sh
  all)
      run_stage download bash "$wpath" "${wrapper_args[@]}"
      if _is_structured "$SOURCE"; then
          run_stage serialize bash "$HERE/$SOURCE/serialize_$SOURCE.sh" "${struct_serialize_args[@]}"
          run_stage load bash "$HERE/$SOURCE/load_$SOURCE.sh" "${load_args[@]}"
          if [ "$SOURCE" = "mesh" ]; then
              run_stage graph bash "$HERE/$SOURCE/graph_$SOURCE.sh"
          fi
      elif _is_lit "$SOURCE"; then
          run_stage extract bash "$HERE/$SOURCE/extract_$SOURCE.sh" "${lit_extract_args[@]}"
          run_stage load bash "$HERE/$SOURCE/load_$SOURCE.sh" "${load_args[@]}"
          run_stage graph bash "$HERE/$SOURCE/graph_$SOURCE.sh"
      else
          log INFO "$SOURCE: only 'download' is wired (extract/serialize = SP2/SP4)"
      fi
      ;;
  ```

- [ ] **Step 4: `usage()`** — add `serialize` to the stage enumeration string.

- [ ] **Step 5: chembl scaffold.** Create `src/episteme/data/chembl/__init__.py` (empty) and `src/episteme/data/chembl/serialize_chembl.py`:
```python
"""SP4 chembl serializer -- SCAFFOLD (Task 2). Task 5 fills the body."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path


def serialize_chembl(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Scaffold: returns an empty result. Tolerant of a missing raw dir."""
    Path(raw_dir)  # real read lands in Task 5
    return {"inputs": 0, "ok": 0, "failed": 0, "rows": 0}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m episteme.data.chembl.serialize_chembl")
    p.add_argument("--raw-dir", type=Path, default=None)
    p.add_argument("--processed-dir", type=Path, default=None)
    p.add_argument("--max-files", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--report", action="store_true")
    args = p.parse_args(argv)
    res = serialize_chembl(
        args.raw_dir or Path("01_raw/chembl"),
        args.processed_dir or Path("02_processed"),
        max_files=args.max_files, force=args.force,
        workers=args.workers, verbose=args.verbose,
    )
    print(f"chembl serialize (scaffold): {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```
(Type-annotate `raw_dir: Path, processed_dir: Path` from the start — SP2's bookshelf scaffold omitted these and it was a whole-branch-review Minor finding; don't repeat it.)

- [ ] **Step 6: chembl wrappers.** `scripts/data/chembl/serialize_chembl.sh` → `exec "$PY" -m episteme.data.chembl.serialize_chembl "$@"`; `scripts/data/chembl/load_chembl.sh` → `exec "$PY" -m episteme.data.load_articles --source chembl "$@"`. Copy the header/idiom from any SP2 flat lit-source wrapper (e.g. `scripts/data/pubmed/extract_pubmed.sh`) verbatim, `s/pubmed/chembl/` and `s/extract/serialize/`. `chmod +x` + `git update-index --chmod=+x` both. `bash -n` clean.

- [ ] **Step 7: Run** — the 5 new dispatch tests + the full existing `test_run_pipeline_dispatch.py` suite green. `bash -n scripts/data/run_pipeline.sh` clean.

- [ ] **Step 8: Commit** — `feat(sp4): run_pipeline.sh structured-serializer stage chain + chembl scaffold`

---

### Task 3: `episteme.mesh_hierarchy` — migration 0003

**Files:**
- Create: `src/episteme/data/db/migrations/0003_mesh_hierarchy.sql`
- Test: additions to `tests/test_migrations.py` (the existing SP2 file — add a `not pg` version-agnostic check if one fits its existing pattern, plus a `pg` structural check; follow that file's existing style)

**Interfaces:**
- Produces: `episteme.mesh_hierarchy (parent_descriptor_ui text, child_descriptor_ui text, source_file text)`, unique index on `(parent_descriptor_ui, child_descriptor_ui)`. Consumed by Task 4 (`graph_builder`).

- [ ] **Step 1: Write the failing test** — add to `tests/test_migrations.py`:

```python
def test_migration_0003_mesh_hierarchy_table(sa_conn):
    with sa_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.mesh_hierarchy')")
        assert cur.fetchone()[0] is not None
        cur.execute("SELECT to_regclass('episteme.mesh_hierarchy_uq')")
        assert cur.fetchone()[0] is not None
```
(Reuse this file's existing `sa_conn` fixture — it should already apply `0001` best-effort and skip cleanly if unreachable; check the existing fixture's exact behavior before assuming, since `0001` fails on this build — the fixture likely applies `0002` directly already, per SP2's Task 2. Extend it to also apply `0003` the same way, or add a second fixture-application step — match the existing file's pattern exactly, don't invent a new one.)

- [ ] **Step 2: Run — verify it fails.**

- [ ] **Step 3: Write `0003_mesh_hierarchy.sql`:**
```sql
-- SP4 migration 0003 -- episteme.mesh_hierarchy (MeSH descriptor parent/child
-- tree-number edges). Idempotent. NOTE (2026-09): on this build
-- migrate_database.sh dies at 0001 (pgvector not installed) and never reaches
-- this file automatically. Apply directly:
--   psql -d <db> -f src/episteme/data/db/migrations/0003_mesh_hierarchy.sql
-- (idempotent -- safe to re-run). Untracked in episteme._migrations when
-- applied this way (mirrors 0002's identical caveat, see that file's header).

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
(Grant includes `DELETE` from the start — SP2's Task-13 whole-branch review caught `article_parts` missing it on the first pass; apply that lesson directly rather than re-discover it here.)

- [ ] **Step 4: Apply to `episteme_test` and run.** `set -a; . ./.env; set +a; PGPASSWORD=$EPISTEME_SYS_ADMIN_PASSWORD psql -h $PGHOST -p $PGPORT -U episteme_sys_admin -d episteme_test -f src/episteme/data/db/migrations/0003_mesh_hierarchy.sql`. Then `pytest -q -m pg tests/test_migrations.py` → green.

- [ ] **Step 5: Commit** — `feat(sp4): episteme.mesh_hierarchy migration 0003`

---

### Task 4: `graph_builder` — MeSH hierarchy derivation

**Files:**
- Modify: `src/episteme/data/graph_builder.py`
- Test: additions to `tests/data/test_graph_builder.py` (mirror the file's existing `MIGRATION_0002 = REPO_ROOT / "..."` + `cur.execute(MIGRATION_0002.read_text(...))` pattern for `MIGRATION_0003`)

**Interfaces:**
- Consumes: `episteme.mesh_hierarchy` (Task 3). `articles` rows with `source="mesh"` (from Task 9 — but this task's own test seeds synthetic rows directly, same as SP2 Task 3 proved `article_parts` before any real extractor existed).
- Produces: `build(conn, *, source="mesh", raw_dir, run_id)` return dict gains `"mesh_hierarchy": int`; `neighbours(conn, id, kind="mesh_parent"/"mesh_child")`.

**Design ruling (controller, resolving spec §8 open item 3):** the pmc cites/mesh derivation already re-parses raw JATS XML directly from `raw_dir` rather than persisting citation/keyword data in the DB first — the MeSH hierarchy derivation follows the same architectural pattern rather than inventing a new persisted "extra fields" column on `articles`. `graph_builder` re-parses the raw MeSH descriptor XML under `raw_dir` (matched to `mesh` articles rows' distinct `source_file`s) to extract every descriptor's `DescriptorUI` + `TreeNumberList`, computes parent/child edges by tree-number-prefix matching **within that same file's full descriptor set** (a MeSH release ships its whole vocabulary in one release file, so this needs no cross-file join), and DELETE-by-`source_file` + INSERTs — same idempotency shape as the `article_parts` derivation.

- [ ] **Step 1: Write the failing test** — add to `tests/data/test_graph_builder.py`:

```python
MIGRATION_0003 = REPO_ROOT / "src/episteme/data/db/migrations/0003_mesh_hierarchy.sql"

def test_build_populates_mesh_hierarchy(pg_conn, tmp_path, monkeypatch):
    _setup_schema(pg_conn)  # existing helper
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0002.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0003.read_text(encoding="utf-8"))
    pg_conn.commit()

    # a tiny real-shaped MeSH descriptor XML: D003920 (child) under D003924's
    # tree number prefix (parent), per NLM's DescriptorRecordSet shape.
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "desc2026.xml").write_text(
        '<DescriptorRecordSet>'
        '<DescriptorRecord><DescriptorUI>D003924</DescriptorUI>'
        '<DescriptorName><String>Diabetes Mellitus</String></DescriptorName>'
        '<TreeNumberList><TreeNumber>C18.452.394.750</TreeNumber></TreeNumberList>'
        '</DescriptorRecord>'
        '<DescriptorRecord><DescriptorUI>D003920</DescriptorUI>'
        '<DescriptorName><String>Diabetes Mellitus, Type 2</String></DescriptorName>'
        '<TreeNumberList><TreeNumber>C18.452.394.750.149</TreeNumber></TreeNumberList>'
        '</DescriptorRecord>'
        '</DescriptorRecordSet>',
        encoding="utf-8",
    )

    # seed the loaded articles rows graph_builder reads to find source_files
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file, source_record_id) "
            "VALUES (%s, 'mesh', %s, %s), (%s, 'mesh', %s, %s)",
            ("mesh:D003924", "desc2026.xml", "D003924",
             "mesh:D003920", "desc2026.xml", "D003920"),
        )
    pg_conn.commit()

    from episteme.data.graph_builder import build, neighbours
    res = build(pg_conn, source="mesh", raw_dir=raw, run_id="t")
    pg_conn.commit()
    assert res["mesh_hierarchy"] == 1
    assert neighbours(pg_conn, "D003924", kind="mesh_child") == ["D003920"]
    assert neighbours(pg_conn, "D003920", kind="mesh_parent") == ["D003924"]

    # idempotency: a second build over the same source_file must not duplicate.
    res2 = build(pg_conn, source="mesh", raw_dir=raw, run_id="t2")
    pg_conn.commit()
    assert res2["mesh_hierarchy"] == 1
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.mesh_hierarchy")
        assert cur.fetchone()[0] == 1
```

- [ ] **Step 2: Run — verify it fails** (`"mesh" not in _GRAPH_SOURCES` → `NotImplementedError`; `kind not in (...)` → `ValueError`).

- [ ] **Step 3: Implement.**

`_GRAPH_SOURCES` gains `"mesh"`:
```python
_GRAPH_SOURCES = (
    "pmc", "pubmed", "apollo", "europepmc_manuscript", "europepmc_preprint",
    "guidelines", "bookshelf", "mesh",
)
```
(Adding `mesh` to this tuple also lets it through the existing parts-phase — harmless: mesh rows have `container_id IS NULL`, so that phase's `SELECT ... WHERE container_id IS NOT NULL` naturally returns 0 rows for `source="mesh"`. No guard needed there.)

New insert constant near `_PARTS_INSERT`:
```python
_MESH_HIERARCHY_INSERT = (
    "INSERT INTO episteme.mesh_hierarchy (parent_descriptor_ui, child_descriptor_ui, source_file) "
    "VALUES (%s, %s, %s) ON CONFLICT (parent_descriptor_ui, child_descriptor_ui) DO NOTHING"
)
```

New helper, mirroring `_local`'s shape (reuse `_local` itself, already defined in this file — do not duplicate it):
```python
def _parse_mesh_descriptors(xml_path: Path) -> dict[str, list[str]]:
    """Return {descriptor_ui: [tree_number, ...]} for every
    <DescriptorRecord> in one MeSH descriptor release file. A parse failure
    yields an empty dict (mirrors _parse_jats's failure mode)."""
    try:
        root = ET.parse(str(xml_path)).getroot()
    except Exception:  # noqa: BLE001 -- a malformed file must not abort the build
        return {}
    out: dict[str, list[str]] = {}
    for rec in root.iter():
        if _local(rec.tag) != "DescriptorRecord":
            continue
        ui = None
        trees: list[str] = []
        for el in rec.iter():
            tag = _local(el.tag)
            if tag == "DescriptorUI" and ui is None:
                ui = (el.text or "").strip()
            elif tag == "TreeNumber":
                txt = (el.text or "").strip()
                if txt:
                    trees.append(txt)
        if ui:
            out[ui] = trees
    return out
```
(Check `ET`'s import at the top of `graph_builder.py` — confirm it's already `defusedxml.ElementTree` per this file's existing security posture, not bare `xml.etree`; reuse whatever is already imported, do not add a second XML import.)

New derivation, added to `build()` after the existing parts-phase, gated `if source == "mesh":`:
```python
    total_mesh_hierarchy = 0
    if source == "mesh":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT source_file FROM episteme.articles WHERE source = 'mesh'"
            )
            mesh_files = [r[0] for r in cur.fetchall()]

        for src_file in sorted(mesh_files):
            xml_path = None
            for cand in Path(raw_dir).rglob(src_file):
                xml_path = cand
                break
            if xml_path is None:
                continue

            descriptors = _parse_mesh_descriptors(xml_path)
            # tree number -> descriptor UI, to resolve a child's parent prefix
            tree_to_ui = {t: ui for ui, trees in descriptors.items() for t in trees}

            edges: list[tuple[str, str, str]] = []
            seen: set[tuple[str, str]] = set()
            for ui, trees in descriptors.items():
                for tree in trees:
                    if "." not in tree:
                        continue  # top-level descriptor, no parent
                    parent_tree = tree.rsplit(".", 1)[0]
                    parent_ui = tree_to_ui.get(parent_tree)
                    if not parent_ui or parent_ui == ui:
                        continue
                    key = (parent_ui, ui)
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append((parent_ui, ui, src_file))

            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM episteme.mesh_hierarchy WHERE source_file = %s", (src_file,)
                )
                if edges:
                    cur.executemany(_MESH_HIERARCHY_INSERT, edges)

            audit_trail.record(
                "graph_commit", conn=conn, object=f"mesh {src_file}",
                rows_affected=len(edges), run_id=run_id,
            )
            total_mesh_hierarchy += len(edges)
```
Add `"mesh_hierarchy": total_mesh_hierarchy` to the function's return dict. Update the module/function docstrings to mention the fourth derivation (mirror how SP2's docstrings were updated when the third derivation landed).

`neighbours()`: extend the guard tuple to include `"mesh_parent"`, `"mesh_child"`; extend `_neighbours_cte` with two new branches:
```python
    if kind == "mesh_parent":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT parent_descriptor_ui FROM episteme.mesh_hierarchy "
                "WHERE child_descriptor_ui = %s ORDER BY parent_descriptor_ui",
                (pmid,),
            )
            return [r[0] for r in cur.fetchall()]

    if kind == "mesh_child":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT child_descriptor_ui FROM episteme.mesh_hierarchy "
                "WHERE parent_descriptor_ui = %s ORDER BY child_descriptor_ui",
                (pmid,),
            )
            return [r[0] for r in cur.fetchall()]
```
(`neighbours`'s `pmid` parameter name is generic — SP2's `part` kind already reused it for non-pmid ids; keep that established convention rather than renaming.) These two kinds stay CTE-only, never routed through SQL/PGQ — same as `part`.

- [ ] **Step 4: Run — verify it passes.** Both new tests green; the full existing `tests/data/test_graph_builder.py` suite green (no regression to the pmc/parts derivations).

- [ ] **Step 5: Commit** — `feat(sp4): graph_builder derives mesh_hierarchy + kind="mesh_parent"/"mesh_child" neighbours`

---

## Phase B — the eight source serializers

> Each Phase-B task follows the same shape. Read the spec's §4.2 row for the source + `extract_pubmed.py` (`src/episteme/data/pubmed/extract_pubmed.py`) as the template for module structure (`process_one`, `_best_effort_audit`, `_run_report`, `main`). The task is NOT complete until the field-shape `--report` is signed off in the ledger and one real end-to-end has run against `episteme_test`.

### Task 5: `chembl` — reconcile into `serialize_chembl.py`

**Files:**
- Modify: `src/episteme/data/chembl/serialize_chembl.py` (fill the Task-2 scaffold)
- Test: `tests/data/test_serialize_chembl.py`, fixture `tests/fixtures/sp4/chembl/sample.db` (a tiny SQLite file with 2-3 rows spanning `compound_structures`/`activities`/`target_dictionary`, or whatever the real ChEMBL release's actual table names turn out to be — **inspect the real downloaded SQLite schema during implementation**, do not assume table/column names from the spec's illustrative example)

**Interfaces:** `serialize_chembl(raw_dir, processed_dir, *, max_files=0, force=False, workers=1, verbose=False) -> {"inputs","ok","failed","rows"}` + `main(argv)` per the Global Constraints CLI. Unit of work = one ChEMBL SQLite release file.

- [ ] **Step 1: Build the fixture.** Either hand-build a minimal SQLite file with 2-3 realistic rows (matching whatever the real release's schema turns out to be — inspect it first via `duckdb.sql("SELECT * FROM sqlite_scan('<real path>', 'sqlite_master')")` or similar, if a real download is available; otherwise construct a plausible schema from ChEMBL's public documentation and note the assumption in your report for the sign-off step to confirm/correct against real data), or take a real small slice if a real ChEMBL download is available and small enough to trim.

- [ ] **Step 2: Write the failing test.**
```python
def test_chembl_serialize_rows(tmp_path):
    res = serialize_chembl(FX, tmp_path)
    assert res["inputs"] == 1 and res["rows"] >= 1
    shards = list((tmp_path / "staging" / "chembl").glob("*.*"))
    assert shards
    df = pl.read_parquet(shards[0]) if shards[0].suffix == ".parquet" else pl.read_ndjson(shards[0])
    r = df.to_dicts()[0]
    assert r["source"] == "chembl"
    assert r["id"].startswith("chembl:")
    assert r["subset"] == "commercial"  # CC BY-SA 3.0
    assert r["container_id"] is None and r["book_meta"] is None
```

- [ ] **Step 3: Run — verify it fails** (scaffold returns 0 rows).

- [ ] **Step 4: Implement.** DuckDB `sqlite_scanner` extension (`duckdb.sql("INSTALL sqlite; LOAD sqlite;")` once, or per-call — check whether the extension needs an explicit install step in this environment or ships pre-installed with the pinned DuckDB version; note either way in your report) to read the release file's bioactivity-shaped table(s); build one row per bioactivity record with `id=f"chembl:{chembl_id}"`, `source_record_id=chembl_id`, `text=<template sentence per spec §4.2>`, `license`/`subset` via `article_schema.normalize_license("CC BY-SA 3.0")`/`subset_from_license(...)` (should resolve to `"commercial"` — the spec's table already confirms this arm exists, no `normalize_license` change expected). `title`/`journal`/`year`/`authors` = `None` (ChEMBL bioactivity records have no bibliographic shape). Row assembly via `article_schema.finalize_row`. Batch → `staging_writer.write_rows(rows, processed_dir, source="chembl", source_file=path.name)`. Per-file `checkpoint_markers.mark_success`/`mark_failed`, best-effort `audit_trail.record("extract_commit", ...)` degrading to `mirror_only` (copy `extract_pubmed.py`'s `_best_effort_audit` shape verbatim). `--report` early-returns before any write, honors `--max-files`.

- [ ] **Step 5: Run — unit test green.**

- [ ] **Step 6: `--report` + real end-to-end.** `.venv/Scripts/python.exe -m episteme.data.chembl.serialize_chembl --raw-dir tests/fixtures/sp4/chembl --report` → paste table into the ledger. Best-effort real: `set -a; . ./.env; set +a; PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh chembl download --max-files 1` (ChEMBL's default download mode is SQLite+SDF+chemreps — likely large; attempt the full pull first per Global Constraints, fall back to a genuine partial/sliced real file if impractical within budget, same as SP2's `id_mappings`/`lite_metadata` precedent), then `serialize --max-files 20`, `load`. Confirm `episteme_test.articles` has `source='chembl'` rows, `subset='commercial'`, `_audit` chain intact. `git clean -fdx 01_raw/chembl 02_processed` after.

- [ ] **Step 7: Ledger sign-off** — `Ledger: chembl field-shape signed-off — <controller> — <date>`.

- [ ] **Step 8: Commit** — `feat(sp4): chembl serializer -> episteme.articles`

---

### Task 6: `uniprot` — `serialize_uniprot.py`

**Files:**
- Create: `src/episteme/data/uniprot/__init__.py`, `src/episteme/data/uniprot/serialize_uniprot.py`
- Create: `scripts/data/uniprot/{serialize,load}_uniprot.sh` (copy Task 5's chembl wrapper shape, `s/chembl/uniprot/`)
- Test: `tests/data/test_serialize_uniprot.py`, fixture `tests/fixtures/sp4/uniprot/sample.fasta` (2-3 real-shaped Swiss-Prot FASTA entries — the pre-restructure prototype's `serialize_structured_sources.py::process_uniprot_fasta`/`serialize_uniprot` (`src/episteme/data/curate/serialize_structured_sources.py`) has a real example header to model the fixture on, e.g. `>sp|P68871|HBB_HUMAN Hemoglobin subunit beta OS=Homo sapiens OX=9606 GN=HBB PE=1 SV=2`)

**Interfaces:** same shape as Task 5. Unit of work = one Swiss-Prot FASTA file.

- [ ] **Step 1: Build the fixture** — 2-3 entries, real UniProt accession/header shape (headers vary in which `OS=`/`OX=`/`GN=`/`PE=`/`SV=` keys are present — include at least one entry missing `GN=` to prove the parser degrades gracefully).

- [ ] **Step 2: Write the failing test** — mirrors Task 5's shape; additionally assert `r["subset"] == "text_mining"` (NOT `"commercial"`, per the Global Constraints ruling) and `r["license"] == "CC BY-ND"` (whatever `normalize_license` actually returns for the real UniProt licence string — confirm at implementation, this is the `license` field, not `subset`) even though `subset` is hardcoded away from what `subset_from_license` alone would compute.

- [ ] **Step 3: Run — verify it fails.**

- [ ] **Step 4: Implement.** Port and tighten the prototype's header-parsing logic (`serialize_structured_sources.py::serialize_uniprot`, lines ~91-130) — the `OS=`/`OX=`/`GN=`/`PE=`/`SV=` marker-splitting approach is sound, but its exception-swallowing fallback (`except Exception: return {"id": "uniprot_unknown", ...}`) must NOT survive into SP4's version: a genuinely malformed header should `mark_failed` for that record (or the whole file, per this codebase's existing granularity — check whether `extract_pubmed.py`'s error handling is per-record or per-file and match it), never silently emit an `"uniprot_unknown"`-id row. **`subset` MUST be hardcoded `"text_mining"`** with a code comment explaining why (mirror `extract_europepmc_manuscripts.py`'s `subset="text_mining"` hardcode pattern and its comment, from SP2 Task 8) — do NOT call `subset_from_license(code)` and use its return value for the final `subset` field, since `code` will be `"CC BY-ND"` and `subset_from_license("CC BY-ND")` currently returns `"commercial"`, which is exactly the interpretation this task exists to avoid. Still call `normalize_license(<real licence string>)` to populate `license`/`license_url`/`license_raw` correctly — only the `subset` assignment is overridden. `text = f"Protein {entry_name} ({description}) in organism {organism} is encoded by gene {gene}. Its amino acid sequence is: {sequence}"` (the prototype's template, kept — tighten wording at sign-off if real data suggests it). `id = f"uniprot:{accession}"`.

  At implementation, check whether the richer `.dat`/XML flat-file UniProt format (carrying function/disease/subcellular-location annotations FASTA headers lack) is reachable via the existing `download_uniprot.sh` wrapper — if easily available, prefer it for materially better prose; if not, FASTA-only is acceptable for this task, note the gap in your report (spec §8 open item 5).

- [ ] **Step 5: Run — unit test green.**

- [ ] **Step 6: `--report` + real end-to-end.** Same shape as Task 5. `subset='text_mining'` must be verified against `episteme_test`, not just the fixture.

- [ ] **Step 7: Ledger sign-off.**

- [ ] **Step 8: Commit** — `feat(sp4): uniprot serializer -> episteme.articles (text_mining subset)`

---

### Task 7: `pubchem` — `serialize_pubchem.py`

**Files:**
- Create: `src/episteme/data/pubchem/__init__.py`, `src/episteme/data/pubchem/serialize_pubchem.py`
- Create: `scripts/data/pubchem/{serialize,load}_pubchem.sh`
- Test: `tests/data/test_serialize_pubchem.py`, fixture `tests/fixtures/sp4/pubchem/sample.tsv` (2-3 rows, real `compound_extras` column shape — **inspect a real downloaded file first**, PubChem's property-TSV column names/order are not guessed here)

**Interfaces:** same shape as Task 5. Unit of work = one PubChem `compound_extras` TSV file.

- [ ] **Step 1-3:** same TDD shape as Task 5, adapted for PubChem's real column names (CID/SMILES/IUPAC-name/formula/synonyms — confirm exact column headers against a real file, `download_pubchem.sh compound_extras` mode).

- [ ] **Step 4: Implement.** DuckDB TSV read; 1 row per CID; `id=f"pubchem:{cid}"`; `text` template, e.g. *"Compound CID {cid} ({iupac_name}) has molecular formula {formula} and canonical SMILES {smiles}."* Licence: public-domain (NIH) — attempt `normalize_license` against PubChem's real licence string; if no existing arm matches, this is spec §8 open item 2 — flag it in your report rather than silently defaulting; a conservative `unknown`→`open_metadata` fallback (the existing default behavior) is acceptable pending that follow-up decision, do NOT add a new `normalize_license` arm without flagging it first (mirrors the discipline SP2's apollo/guidelines tasks used for their own licence questions).

- [ ] **Step 5-6:** same shape as Task 5.

- [ ] **Step 7: Ledger sign-off.**

- [ ] **Step 8: Commit** — `feat(sp4): pubchem serializer -> episteme.articles`

---

### Task 8: `clinvar` — `serialize_clinvar.py`

**Files:**
- Create: `src/episteme/data/clinvar/__init__.py`, `src/episteme/data/clinvar/serialize_clinvar.py`
- Create: `scripts/data/clinvar/{serialize,load}_clinvar.sh`
- Test: `tests/data/test_serialize_clinvar.py`, fixture `tests/fixtures/sp4/clinvar/sample.tsv` (2-3 rows, real `variant_summary.txt.gz` column shape — inspect a real file first)

**Interfaces:** same shape as Task 5. Unit of work = one `variant_summary.txt.gz` file (default `download_clinvar.sh` mode).

- [ ] **Step 1-3:** same TDD shape as Task 5.

- [ ] **Step 4: Implement.** DuckDB TSV read (gzip-transparent — confirm DuckDB's CSV reader handles `.gz` directly, or decompress first via `gzip.open` and pipe to DuckDB, whichever is simpler in this codebase's existing idiom); 1 row per variant record; `id=f"clinvar:{variation_id_or_accession}"` (confirm ClinVar's real primary key column — likely `VariationID` or `RCVaccession`, check the real file); `text` template, e.g. *"Variant {name} is classified {clinical_significance} for {phenotype_list}, per ClinVar accession {rcv_accession}."* Licence: public-domain (NCBI).

- [ ] **Step 5-8:** same shape as Task 5. Commit — `feat(sp4): clinvar serializer -> episteme.articles`

---

### Task 9: `reactome` — `serialize_reactome.py`

**Files:**
- Create: `src/episteme/data/reactome/__init__.py`, `src/episteme/data/reactome/serialize_reactome.py`
- Create: `scripts/data/reactome/{serialize,load}_reactome.sh`
- Test: `tests/data/test_serialize_reactome.py`, fixture `tests/fixtures/sp4/reactome/sample.tsv`

**Interfaces:** same shape as Task 5. Unit of work = one Reactome pathway-relationship or gene/protein-mapping file (Reactome's flat release directory ships several files — pick the one(s) that best support pathway-description + gene→pathway-membership prose, confirmed against the real release listing).

- [ ] **Step 1-3:** same TDD shape as Task 5.

- [ ] **Step 4: Implement.** DuckDB TSV read; 1 row per pathway; `id=f"reactome:{pathway_id}"`; `text` template combining pathway description + (where the mapping file supports it) gene/protein membership sentences. Licence: CC0 → `subset="commercial"` via the existing `normalize_license` `CC0` arm — no code change expected, confirm against the real licence string at sign-off.

- [ ] **Step 5-8:** same shape as Task 5. Commit — `feat(sp4): reactome serializer -> episteme.articles`

---

### Task 10: `mesh` — `serialize_mesh.py` (+ proves `graph mesh` end-to-end)

**Files:**
- Create: `src/episteme/data/mesh/__init__.py`, `src/episteme/data/mesh/serialize_mesh.py`
- Create: `scripts/data/mesh/{serialize,load,graph}_mesh.sh` (the only structured source with a `graph_` wrapper — the `[ -f ]` guard in Task 2's `run_pipeline.sh` edit was already satisfied for `serialize`/`load` by every other source's own wrappers; `graph_mesh.sh` closes the last gap Task 2 deliberately left open)
- Test: `tests/data/test_serialize_mesh.py`, fixture `tests/fixtures/sp4/mesh/sample.xml` (a small `DescriptorRecordSet`, same shape as Task 4's synthetic test XML — reuse that shape, ideally the EXACT same content so Task 4's `pg` graph test and this task's `--report` fixture describe the same tiny real-shaped descriptor tree)

**Interfaces:** same shape as Task 5, PLUS this is the task that makes `graph_builder`'s Task 4 derivation exercisable end-to-end against real data for the first time.

- [ ] **Step 1: Build/reuse the fixture.** If a real NLM MeSH descriptor XML is downloadable within budget, use a real small slice (2-3 descriptors spanning one parent/child pair) instead of the synthetic content — note in your report which you used and why.

- [ ] **Step 2: Write the failing test.**
```python
def test_mesh_serialize_rows(tmp_path):
    res = serialize_mesh(FX, tmp_path)
    assert res["rows"] >= 1
    shards = list((tmp_path / "staging" / "mesh").glob("*.*"))
    df = pl.read_parquet(shards[0]) if shards[0].suffix == ".parquet" else pl.read_ndjson(shards[0])
    rows = df.to_dicts()
    r = next(x for x in rows if x["source_record_id"] == "D003924")
    assert r["id"] == "mesh:D003924"
    assert "Diabetes Mellitus" in (r["text"] or r["title"] or "")
    assert r["container_id"] is None
```

- [ ] **Step 3: Run — verify it fails.**

- [ ] **Step 4: Implement.** `defusedxml.ElementTree.iterparse` (matches the security posture `jats.py`/`graph_builder.py` already established — NOT bare `xml.etree`) streaming over the descriptor XML, `elem.clear()` per `DescriptorRecord` (this file can be large — memory-safe streaming matters here as much as it did for `enrich_from_lite.py`'s 821MB single-XML-document case in SP2 Task 12, same pattern). 1 row per descriptor: `id=f"mesh:{descriptor_ui}"`, `source_record_id=descriptor_ui`, `title=<DescriptorName>`, `text=<DescriptorName> + ScopeNote (if present) -> prose>`. Licence: public-domain (NLM). Note: this module does NOT need to persist `TreeNumberList` into the row — `graph_builder`'s Task 4 derivation re-parses the raw XML independently for the hierarchy edges (the design ruling in Task 4). Keep this module's own responsibility scoped to producing `articles` rows only.

- [ ] **Step 5: Run — unit test green.**

- [ ] **Step 6: `graph_mesh.sh` wrapper.** `exec "$PY" -m episteme.data.graph_builder --source mesh --raw-dir "${EPISTEME_RAW_ROOT:-./01_raw}/mesh" "$@"` — same shape as every SP2 `graph_<source>.sh` wrapper (`graph_builder.main()` takes only `--source`/`--raw-dir`, no extra args forwarded from `run_pipeline.sh`, per Task 2's dispatch — the `all` chain already calls this with no extra args). `bash -n` clean.

- [ ] **Step 7: `--report` + real end-to-end, including `graph`.** `.venv/Scripts/python.exe -m episteme.data.mesh.serialize_mesh --raw-dir tests/fixtures/sp4/mesh --report`. Best-effort real: `mesh download --max-files 1`, `serialize --max-files 20`, `load`, **`graph`** — confirm `episteme_test.mesh_hierarchy` gets real parent/child edges from a real (or genuinely-sliced-real) MeSH descriptor file, and `neighbours(kind="mesh_parent"/"mesh_child")` returns real data. This is the proof the spec's §3.3/§6 design actually composes end-to-end, not just in Task 4's synthetic test.

- [ ] **Step 8: Ledger sign-off.**

- [ ] **Step 9: Commit** — `feat(sp4): mesh serializer -> episteme.articles + mesh_hierarchy graph proof`

---

### Task 11: `ontologies` — `serialize_ontologies.py`

**Files:**
- Create: `src/episteme/data/ontologies/__init__.py`, `src/episteme/data/ontologies/serialize_ontologies.py`
- Create: `scripts/data/ontologies/{serialize,load}_ontologies.sh`
- Test: `tests/data/test_serialize_ontologies.py`, fixture `tests/fixtures/sp4/ontologies/sample.obo` (a tiny real-shaped OBO file — GO/HPO/MONDO/UCUM's fixed download list is in `download_ontologies.sh`, confirm which of the four is easiest to source a tiny real slice from)

**Interfaces:** same shape as Task 5. Unit of work = one ontology file (one of GO/HPO/MONDO/UCUM per run — `--raw-dir` points at whichever files exist under it; the serializer processes every `.obo`/`.owl` file it finds there, one `source_file` per ontology).

- [ ] **Step 1-3:** same TDD shape as Task 5.

- [ ] **Step 4: Implement.** `pronto.Ontology(path)` per file; 1 row per term: `id=f"ontologies:{term.id}"` (the term's own CURIE, e.g. `GO:0008150`, IS the native id — do not re-prefix it further), `title=term.name`, `text=f"{term.name}: {term.definition}"` (falling back to just the name if no definition). Licence: each of the four ontologies has its own licence — `normalize_license` called per-file against whichever licence string the file itself declares (OBO files carry a `data-version`/license header — check the real format); flag any ontology whose licence string doesn't resolve via an existing `normalize_license` arm rather than guessing (spec §8 open item 4). **No hierarchy graph table for ontologies this phase** (spec §7 non-goals) — do not attempt a `graph_ontologies.sh` or extend `graph_builder` for this source.

- [ ] **Step 5-8:** same shape as Task 5, but the field-shape sign-off must cover all four ontologies' licence findings (§8 item 4), even if only one is used for the fixture/first real e2e — note the other three's status in your report. Commit — `feat(sp4): ontologies serializer -> episteme.articles`

---

### Task 12: `openalex` — `serialize_openalex.py` (bounded biomedical subset)

**Files:**
- Create: `src/episteme/data/openalex/__init__.py`, `src/episteme/data/openalex/serialize_openalex.py`
- Create: `scripts/data/openalex/{serialize,load}_openalex.sh`
- Test: `tests/data/test_serialize_openalex.py`, fixture `tests/fixtures/sp4/openalex/sample.jsonl` (2-3 real-shaped OpenAlex `works_jsonl` records, including at least one with a real `abstract_inverted_index` to prove reconstruction)

**Interfaces:** same shape as Task 5. Unit of work = one `works_jsonl` shard file.

- [ ] **Step 1: Build the fixture** — real or realistic OpenAlex work JSON records (the schema is public — `id`, `doi`, `title`, `publication_year`, `authorships`, `host_venue`/`primary_location`, `abstract_inverted_index`, `concepts`/`topics`).

- [ ] **Step 2: Write the failing test** — additionally assert the abstract reconstruction: a record whose `abstract_inverted_index = {"The": [0], "study": [1], "shows": [2]}` must produce `text`/`abstract` containing `"The study shows"` in the correct word order, not the raw inverted-index dict.

- [ ] **Step 3: Run — verify it fails.**

- [ ] **Step 4: Implement — the bounded biomedical filter (spec §8 open item 1, decided in this task).** Choose and implement ONE concrete filter, documented with a comment explaining the choice: a concept/topic tag match against a biomedical allow-list (OpenAlex's `concepts`/`topics` field carries a controlled taxonomy — filter to concepts under the "Medicine"/"Biology" top-level branches, or an explicit list of biomedical concept IDs) is the recommended default — it's the most directly filterable field OpenAlex provides and doesn't require an external venue/DOI cross-reference list. A rejected (non-biomedical) record is not an error — it's correctly excluded, count it in `inputs` but not `rows`, same disposition as SP2's guidelines QA-shape skip. Also implement the abstract-inverted-index reconstruction (`{word: [positions]}` → sort all `(position, word)` pairs, join in order) as a small pure helper function, unit-testable in isolation. `id=f"openalex:{native_work_id}"` (OpenAlex's own id, e.g. `W2741809807` — strip any `https://openalex.org/` URL prefix if the real field carries one). `doi`/`title`/`year`/`authors`/`journal` populated from the real fields (this is the one SP4 source with a genuinely literature-like bibliographic shape). Licence: CC0 (OpenAlex's stated licence) → confirm against the real dataset's licence field at sign-off, expected `subset="commercial"` via the existing `CC0` arm.

- [ ] **Step 5: Run — unit test green** (including the abstract-reconstruction assertion).

- [ ] **Step 6: `--report` + real end-to-end.** Best-effort real: `openalex download --max-files 1` (S3 sync per `download_openalex.sh`'s `works_jsonl` mode — a real shard may be large; attempt it, fall back to a genuine partial real file if impractical). `serialize --max-files 20`, `load`. Confirm the biomedical filter's real rejection rate is sane (not 0%, not 100% — report the actual `inputs` vs `rows` split) and that it's not simply re-admitting everything pmc/pubmed already covers (spot-check a handful of loaded rows' DOIs against `episteme_test.articles WHERE source IN ('pmc','pubmed')` for overlap — the spec's stated goal is "not already covered," report what you find even if perfect de-duplication wasn't attempted this task).

- [ ] **Step 7: Ledger sign-off** — must explicitly record the biomedical-filter definition chosen in Step 4, since this is the resolution of spec open item 1.

- [ ] **Step 8: Commit** — `feat(sp4): openalex serializer -> episteme.articles (bounded biomedical subset)`

---

## Phase C — close-out

### Task 13: full sweep + drift log

**Files:** Modify `docs/project-incubation-baseline.md`; touch nothing else except a test fixture if a genuine gap is found (mirror SP2 Task 13's discipline exactly).

- [ ] **Step 1: `not pg` suite** — `.venv/Scripts/python.exe -m pytest -q -m "not pg"` → all green; record the count.
- [ ] **Step 2: `pg` suite** — `set -a; . ./.env; set +a; .venv/Scripts/python.exe -m pytest -q` → green; record the count.
- [ ] **Step 3: `run_pipeline.sh` sweep** — for each of `chembl uniprot pubchem clinvar reactome mesh ontologies openalex`: `serialize --dry-run` → rc 3; `download --dry-run` → rc 0. `mesh graph --dry-run` → rc 3. `mesh extract` → rc 3 (proves `_is_lit`/`_is_structured` don't overlap). Full transcript into the report.
- [ ] **Step 4: `bash -n`** on every new/modified `.sh`.
- [ ] **Step 5: grep gate** — `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'` → empty.
- [ ] **Step 6: Drift log** — one dated entry in `docs/project-incubation-baseline.md` (match the SP2/SP3 entries' style): SP4 landed (plan + spec paths), `postgres_loader` id-collision hardening, `episteme.mesh_hierarchy` + `graph_builder`'s fourth derivation, the eight sources with per-source row counts from the real end-to-ends + their field-shape sign-off dates, the OpenAlex bounded-subset filter definition (resolving spec open item 1) and the UniProt `text_mining` ruling (resolving open item 2) stated explicitly, deferred items (SP5 docs, full-scale ChEMBL/PubChem/OpenAlex runs, ontology licence follow-ups, pubchem's possible new `normalize_license` arm).
- [ ] **Step 7: Commit** — `docs(sp4): drift-log entry — structured serializers landed`

---

## Self-Review

**Spec coverage (`2026-09-15-sp4-structured-serializers-design.md`):**

| Spec item | Task |
|---|---|
| §2 `postgres_loader` id-collision hardening | 1 |
| §3.2 `migrations/0003_mesh_hierarchy.sql` | 3 |
| §3.3 `graph_builder` fourth derivation + `neighbours(kind="mesh_parent"/"mesh_child")` | 4 |
| §4.1 common serializer shape; `--report`+`--max-files` from the start | 5–12 |
| §4.2 chembl | 5 |
| §4.2 uniprot (`text_mining` ruling) | 6 |
| §4.2 pubchem | 7 |
| §4.2 clinvar | 8 |
| §4.2 reactome | 9 |
| §4.2 mesh | 10 |
| §4.2 ontologies | 11 |
| §4.2 openalex (bounded biomedical subset ruling) | 12 |
| §5 `run_pipeline.sh` `serialize` stage + `STRUCTURED_SOURCES` dispatch, wrapper placement | 2 |
| §6 field-shape report + sign-off + real e2e per source | 5–12 (each task's own steps) |
| §7 non-goals (dailymed/openfda/aact, cross-source dedup, LLM rewriting, generalized ontology graph, full-scale-by-default) | not implemented — asserted in 13's drift entry |
| §8 open items 1–6 | resolved in-task (12, 6, 10, 11, 6, deferred) — see per-task notes above |

**Placeholder scan:** Tasks 7–9, 11 give the shared template (Task 5's shape) plus the source-specific prose/licence deltas rather than re-deriving every line — same economy SP2's own plan used for its later extractor tasks. Every task has real test code, real field/status/licence rules, and a concrete fixture description. Column/table-name uncertainties (chembl's real SQLite schema, clinvar's real TSV headers, pubchem's real column names) are explicitly flagged as "inspect the real download, don't assume" rather than guessed — this mirrors SP2's own extractors, most of which hit a real-data structural surprise the plan's initial guess didn't anticipate (bookshelf's NXML shape, the EBI preprint feed's discontinuation) and adapted at implementation; SP4's plan builds that expectation in from the start rather than presenting false certainty.

**Type consistency:** `serialize_<source>(raw_dir: Path, processed_dir: Path, *, max_files: int = 0, force: bool = False, workers: int = 1, verbose: bool = False) -> dict` with keys `{"inputs","ok","failed","rows"}` — identical across Tasks 5–12 and matches `extract_pmc`/every SP2 extractor. `graph_builder.build(conn, *, source, raw_dir, run_id) -> dict` gains `"mesh_hierarchy"`; `neighbours(conn, id, hops=1, kind=...)` — `kind` extended, not changed, consistent with SP2's `"part"` addition. `mesh_hierarchy(parent_descriptor_ui text, child_descriptor_ui text, source_file text)` — consistent Tasks 3/4/10. `id = f"{source}:{native_id}"` consistent across every Phase-B task.

**Risks:** (a) `postgres_loader`'s hardening (Task 1) is the one change in this plan to shared, every-source-depended-on code — low-risk (backward-compatible no-op wherever ids don't collide) but flagged to the user before spec approval and worth an extra-careful task review. (b) `run_pipeline.sh`'s Task 2 surgery is the second-highest-risk item (a load-bearing shared dispatcher SP2's whole-branch review specifically praised for composing correctly under a full stage matrix) — Task 2's brief spells out the exact diff rather than leaving shape-matching to the implementer's judgment, same precision bar SP2's Task 11/12 dispatch exceptions used. (c) Several sources' exact upstream schema (chembl's SQLite tables, clinvar's TSV columns, pubchem's column names) are genuinely unknown until implementation — each task explicitly directs "inspect the real file first," matching the discipline that caught real structural surprises throughout SP2. (d) `openalex`'s biomedical filter and `pubchem`'s/`ontologies`' licence mappings are real open decisions this plan defers into their own tasks rather than resolving upfront — each task's ledger sign-off is where those land, not this plan.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-15-sp4-structured-serializers.md`.

**Subagent-Driven** (standing preference — no mode question): dispatch a fresh implementer per task, task review after each, broad whole-branch review at the end. `pg`-marked tests need `TEST_PG_DSN` from `.env` and the live PostgreSQL from SP1-β (both in place); a task that adds a `pg` test runs it against `episteme_test`.
