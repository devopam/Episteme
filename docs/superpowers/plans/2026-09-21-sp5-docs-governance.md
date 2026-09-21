# SP5 Docs & Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the docs match the shipped tree (SP1–SP4.1), add `docs/11` (GxP posture) and `docs/12` (source inventory), and prove the runbook works from a cold read.

**Architecture:** Docs are written from the **code as shipped**, never from older design text; every checkable claim gets a non-pg consistency test in `tests/docs/` that parses code (WRAPPER table, audit event types/columns, licence codes, dispatcher stage rules). One read-only generator script supplies machine-local inventory columns. No pipeline behaviour changes.

**Tech Stack:** Markdown, pytest (non-pg), bash, Python 3.10+ (`episteme.audit_trail`, `episteme.data.article_schema`).

**Spec:** `docs/superpowers/specs/2026-09-21-sp5-docs-governance-design.md` (charter: `docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md` §SP5).

## Global Constraints

- **No pipeline behaviour changes.** Only new files: docs, `tests/docs/*`, `src/episteme/data/source_inventory.py`, `scripts/data/source_inventory.sh`. No `config.py`/schema/serializer edits.
- **Docs are derived from code.** When an older spec/doc disagrees with the code (e.g. the 2026-09-01 spec lists 12 audit event types; the code has 13, including `serialize_commit`), the code wins and the doc says so.
- **No compliance over-claims.** "GxP-ready", never "GxP-compliant". `cdisc_bc` data licence is stated **UNVERIFIED** (COSMoS README: code MIT; only documentation and minutes CC-BY-4.0; nothing stated for `export/`). `public_domain` for MeSH/PubChem/ClinVar is stated as a **source-anchored governance override** (user decision 2026-09-19; PubChem/ClinVar contributor-content risk stated), never derived from source text and never returned by `normalize_license`.
- **The DB-mode guard checks the database NAME only** (`EPISTEME_PRODUCTION_DATABASE`, default `episteme`); it cannot detect a wrong server/instance and does not cover `psql` DDL scripts. Wrong-server risk is process-mitigated (confirm `select current_setting('server_version')` before DB steps). Go-live item: set `EPISTEME_DB_MODE=restricted`.
- **Secrets:** never read, print, or stage `.env`. Docs may name env KEYS, never values. `git add` explicit paths only; the unstaged `.gitignore` `.gstack/` change is unrelated (never stage it). Never `--no-verify`.
- **Agent-run DB/pipeline commands** target `episteme_test`: prefix `PGDATABASE=episteme_test`; before any DB step confirm `show server_version` = `19beta3`. `pg`-marked tests use `TEST_PG_DSN`.
- **Endpoint URLs** only in `scripts/data/_lib/sources.env`; grep gate `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'` must stay empty (the new script must not contain URLs).
- Run `.venv/Scripts/python.exe -m ruff format` and `ruff check` on new `.py` files before committing.
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/docs/conftest.py`, `tests/docs/_repo.py` | shared parsers: repo root, WRAPPER table, stage rules, doc text |
| `docs/12-source-inventory.md` | static inventory table, one row per WRAPPER key |
| `src/episteme/data/source_inventory.py`, `scripts/data/source_inventory.sh` | read-only machine-local columns (last sync, row counts) |
| `docs/11-gxp-data-integrity.md` | GxP-ready posture from the implemented audit trail |
| `docs/07`, `docs/08`, `docs/09`, `docs/10` | rewrites |
| `docs/02`, `README.md`, `docs/project-incubation-baseline.md` | reconciliation + drift entry |
| `tests/docs/test_*.py` | consistency tests per doc |

---

### Task 1: Test helpers and the source inventory (`docs/12`)

**Files:**
- Create: `tests/docs/_repo.py`, `tests/docs/test_source_inventory_doc.py`, `docs/12-source-inventory.md`

**Interfaces:**
- Produces (`tests/docs/_repo.py`): `REPO: Path`, `wrapper_table() -> dict[str, str]` (source → script path relative to `scripts/data`), `lit_sources() -> set[str]`, `structured_sources() -> set[str]`, `doc(name: str) -> str` (text of `docs/<name>`).

- [ ] **Step 1: Write the helpers**

```python
# tests/docs/_repo.py
"""Parsers shared by the docs-vs-code consistency tests. Read-only; no DB, no network."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_RUN = (REPO / "scripts" / "data" / "run_pipeline.sh").read_text(encoding="utf-8")


def wrapper_table() -> dict[str, str]:
    block = re.search(r"declare -A WRAPPER=\((.*?)\n\)", _RUN, re.S)
    assert block, "WRAPPER table not found in run_pipeline.sh"
    return dict(re.findall(r'\[(\w+)\]="([^"]+)"', block.group(1)))


def _words(var: str) -> set[str]:
    m = re.search(rf'^{var}="([^"]*)"', _RUN, re.M)
    assert m, f"{var} not found in run_pipeline.sh"
    return set(m.group(1).split())


def lit_sources() -> set[str]:
    return _words("LIT_SOURCES")


def structured_sources() -> set[str]:
    return _words("STRUCTURED_SOURCES")


def doc(name: str) -> str:
    return (REPO / "docs" / name).read_text(encoding="utf-8")
```

- [ ] **Step 2: Write the failing pinning tests**

```python
# tests/docs/test_source_inventory_doc.py
import re

from tests.docs._repo import REPO, doc, wrapper_table


def _rows() -> dict[str, list[str]]:
    rows = {}
    for line in doc("12-source-inventory.md").splitlines():
        m = re.match(r"\|\s*`(\w+)`\s*\|(.*)\|\s*$", line)
        if m:
            rows[m.group(1)] = [c.strip() for c in m.group(2).split("|")]
    return rows


def test_inventory_covers_exactly_the_wired_sources():
    assert set(_rows()) == set(wrapper_table())


def test_every_inventory_script_exists():
    for src, script in wrapper_table().items():
        assert (REPO / "scripts" / "data" / script).is_file(), src
        assert f"`{script}`" in doc("12-source-inventory.md"), src


def test_licence_caveats_are_recorded():
    text = doc("12-source-inventory.md")
    assert "UNVERIFIED" in text  # cdisc_bc data licence
    assert "governance override" in text  # public_domain for mesh/pubchem/clinvar
```

Also create an empty `tests/docs/__init__.py` (the existing `tests/data` package does the same); all imports in `tests/docs` are relative (`from ._repo import ...`), so change the first import line of `test_source_inventory_doc.py` to `from ._repo import REPO, doc, wrapper_table`.

- [ ] **Step 3: Run to confirm RED** — `.venv/Scripts/python.exe -m pytest tests/docs/test_source_inventory_doc.py -q -p no:cacheprovider` → FAIL (no `docs/12`).

- [ ] **Step 4: Write `docs/12-source-inventory.md`.** Header (purpose, "static columns here; machine-local columns from `scripts/data/source_inventory.sh`"). One table, columns: `| source | class | stages wired | licence (class + basis) | cadence | script |`. One row per WRAPPER key (first cell exactly `` `key` ``; script cell exactly `` `path/from/scripts/data` `` as in the WRAPPER table). Fill from: `run_pipeline.sh` (stages: literature sources = download/extract/load/graph; structured = download/serialize/load, `mesh` also graph; others download-only; `europepmc_id_mappings` load; `europepmc_lite` enrich), `docs/02`, `docs/10`, the serializers' licence docstrings, `article_schema`. Licence column facts that MUST appear: MeSH/PubChem/ClinVar = `public_domain` **governance override** (decision 2026-09-19; PubChem/ClinVar carry submitter-contributed content risk); GO/MONDO (ontologies) `CC BY` → commercial; HPO `unknown`; UniProt `text_mining` is a hardcoded governance override (real licence CC BY 4.0); `cdisc_bc` = **UNVERIFIED** (README: code MIT; only docs/minutes CC-BY-4.0; nothing stated for `export/` data; download-only, no serializer). Below the table: a "How to refresh machine-local columns" section and a "Class definitions" list matching the roadmap spec §1.

- [ ] **Step 5: Run to confirm GREEN**, then ruff format/check `tests/docs`, then commit `docs(sp5): source inventory (docs/12) pinned to the WRAPPER table` with the trailer. Add only the files created.

---

### Task 2: Read-only inventory generator

**Files:**
- Create: `src/episteme/data/source_inventory.py`, `scripts/data/source_inventory.sh` (mode 100755), `tests/docs/test_source_inventory_cli.py`

**Interfaces:**
- Produces: `collect(raw_root: Path, sources: list[str], counts: dict[str, int] | None) -> list[dict]` (keys `source`, `last_sync` (ISO string or `""`), `rows` (int or `None`)), `main(argv=None) -> int`.

- [ ] **Step 1: Failing tests**

```python
# tests/docs/test_source_inventory_cli.py
import os
import subprocess

from episteme.data.source_inventory import collect

from ._repo import REPO


def test_collect_reads_sync_stamp_and_counts(tmp_path):
    d = tmp_path / "chembl"
    d.mkdir()
    (d / ".last_sync").write_text("2026-09-01T00:00:00Z\n", encoding="utf-8")
    (tmp_path / "mesh").mkdir()
    out = {r["source"]: r for r in collect(tmp_path, ["chembl", "mesh", "uniprot"], {"chembl": 7})}
    assert out["chembl"] == {"source": "chembl", "last_sync": "2026-09-01T00:00:00Z", "rows": 7}
    assert out["mesh"]["last_sync"] == "" and out["mesh"]["rows"] is None
    assert out["uniprot"]["last_sync"] == ""  # directory absent


def test_cli_degrades_when_db_unreachable(tmp_path):
    env = {**os.environ, "PGDATABASE": "episteme_test", "PGPORT": "1", "EPISTEME_RAW_ROOT": str(tmp_path)}
    p = subprocess.run(
        [".venv/Scripts/python.exe", "-m", "episteme.data.source_inventory"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
    )
    assert p.returncode == 0, p.stderr[-500:]
    assert "chembl" in p.stdout and "n/a" in p.stdout
```

The stamp file is `<raw_root>/<source>/last_sync_utc.txt` (written by `write_sync_stamp` in `scripts/data/_lib/common.sh`).

- [ ] **Step 2: RED**, then implement: `collect` as specified (stamp file read, stripped; missing → `""`); `main` builds the source list from the `WRAPPER` table by parsing `scripts/data/run_pipeline.sh` (same regex as the test helper), obtains row counts with `select source, count(*) from episteme.articles group by 1` through `episteme.data.db.connection.connection` (read-only SQL; wrap in try/except → all counts `None`, print `n/a`), prints an aligned table `source  last_sync  rows`, always returns 0. It must never write to disk or the DB and contain no URLs. `source_inventory.sh` mirrors `verify_audit_trail.sh` (venv discovery, `load_dotenv`, `exec "$PY" -m episteme.data.source_inventory "$@"`); it does NOT require `EPISTEME_ACTOR`.
- [ ] **Step 3: GREEN**; `bash -n`; grep gate empty; ruff; commit `feat(sp5): read-only source inventory generator`.

---

### Task 3: `docs/11-gxp-data-integrity.md` and its tests

**Files:**
- Create: `docs/11-gxp-data-integrity.md`, `tests/docs/test_gxp_doc.py`

- [ ] **Step 1: Failing tests**

```python
# tests/docs/test_gxp_doc.py
import re

from episteme.audit_trail import EVENT_TYPES, _HASHED_FIELDS

from ._repo import REPO, doc


def _schema_columns() -> list[str]:
    sql = (REPO / "src/episteme/data/db/schema.sql").read_text(encoding="utf-8")
    body = re.search(r"CREATE TABLE episteme\._audit \((.*?)\n\) PARTITION", sql, re.S).group(1)
    return [ln.split()[0] for ln in body.splitlines() if ln.strip() and not ln.strip().startswith(("PRIMARY", "--"))]


def test_every_event_type_is_documented():
    text = doc("11-gxp-data-integrity.md")
    for ev in EVENT_TYPES:
        assert f"`{ev}`" in text, ev


def test_every_audit_column_is_documented():
    text = doc("11-gxp-data-integrity.md")
    for col in _schema_columns():
        assert f"`{col}`" in text, col
    for f in _HASHED_FIELDS:
        assert f"`{f}`" in text, f


def test_boundaries_and_claims():
    text = doc("11-gxp-data-integrity.md")
    assert "GxP-ready" in text
    assert "GxP-compliant" not in text.replace("not GxP-compliant", "").replace('never "GxP-compliant"', "")
    assert "EPISTEME_DB_MODE=restricted" in text  # go-live item
    assert "database name" in text  # guard limit
    assert "verify_audit_trail.sh" in text
    assert (REPO / "scripts/data/verify_audit_trail.sh").is_file()
```

- [ ] **Step 2: RED**, then write the doc from the **implemented** code (read `src/episteme/audit_trail.py`, `schema.sql` §_audit and the GRANT/REVOKE block, `scripts/data/verify_audit_trail.sh`, `src/episteme/data/db/guard.py`, `run_pipeline.sh` audit bracket, the 2026-09-01 spec §3.8 only for ALCOA+ mapping). Required sections: 1 Posture (GxP-ready; what is claimed and not), 2 Two tiers (operational logs vs audit trail; JSONL mirror at `02_processed/_ops/_audit/audit-YYYYMMDD.jsonl`, mirror is written after the DB insert and a mirror failure is loud, `mirror_only` for best-effort events), 3 Audit record schema (table of all 16 columns with ALCOA+ mapping; `seq`/`recorded_at` DB-assigned and excluded from the hash; `_HASHED_FIELDS` order; genesis hash), 4 Event types (all 13, one line each; note `serialize_commit` was added after the original design), 5 Integrity (hash chain, `verify` incl. `mirror_short`, `verify_audit_trail.sh` exit codes; `REVOKE UPDATE, DELETE` on `_audit` and every partition from `episteme_app`; **state plainly** that the design's `chattr +a` and rotate/gzip script are NOT implemented — check `ls scripts/data` and the schema comment "rotation script is not yet …" and report what actually exists), 6 Actor and reason rules (`EPISTEME_ACTOR` required; `--reason` for force/manual/schema events — verify each against code before claiming), 7 DB-mode guard (modes, `EPISTEME_DB_TARGET`, code default `restricted`, checks the database name only, no server identity check, no `psql` DDL coverage; process control: confirm server version), 8 Go-live checklist (set `EPISTEME_DB_MODE=restricted`; review `.env`; confirm audit verify passes), 9 Out of scope / procedural (CSV/validation, RBAC, e-signatures, periodic review, SOPs). Every claim must be verifiable in code; where the older design text and code disagree, the code wins and the doc notes the gap.
- [ ] **Step 3: GREEN**; commit `docs(sp5): GxP data-integrity posture (docs/11) from the implemented audit trail`.

---

### Task 4: `docs/09` extraction contract (licence + storage + identity)

**Files:**
- Modify: `docs/09-extraction-contract.md`
- Create: `tests/docs/test_extraction_contract_doc.py`

- [ ] **Step 1: Failing tests**

```python
# tests/docs/test_extraction_contract_doc.py
import pytest

from episteme.data.article_schema import (
    LICENSE_PUBLIC_DOMAIN,
    normalize_license,
    subset_from_license,
)

from ._repo import doc

SAMPLES = [
    "CC0", "https://creativecommons.org/licenses/by/4.0/",
    "https://creativecommons.org/licenses/by-sa/4.0/", "https://creativecommons.org/licenses/by-nd/4.0/",
    "https://creativecommons.org/licenses/by-nc/4.0/", "https://creativecommons.org/publicdomain/zero/1.0/",
    "text mining", "something unrecognised",
]


@pytest.mark.parametrize("raw", SAMPLES)
def test_normalised_codes_are_in_the_vocabulary(raw):
    code = normalize_license(raw)[0]
    assert f"`{code}`" in doc("09-extraction-contract.md"), code


def test_public_domain_is_documented_as_override():
    text = doc("09-extraction-contract.md")
    assert f"`{LICENSE_PUBLIC_DOMAIN}`" in text
    assert "governance override" in text
    assert subset_from_license(LICENSE_PUBLIC_DOMAIN) == "commercial"
    assert normalize_license("public domain")[0] != LICENSE_PUBLIC_DOMAIN  # never returned by normalize_license


def test_identity_and_storage_terms_present():
    text = doc("09-extraction-contract.md")
    for term in ("input_key", "delete-by-`source_file`", "episteme.articles"):
        assert term in text, term
    assert "Iceberg" not in text.split("## 4.")[1].split("## 5.")[0] or "superseded" in text
```

(The last assertion is intentionally loose; tighten it to whatever the rewritten §4 actually says.)

- [ ] **Step 2: RED**, then edit `docs/09`: §5 `license` row vocabulary and §6.1 table gain `public_domain` (source-anchored governance override for MeSH, PubChem, ClinVar; decision 2026-09-19; **not** produced by `normalize_license`; `license_raw` keeps the real text; contributor-content caveat for PubChem/ClinVar) and creativecommons.org URL recognition; GO/MONDO now `CC BY` → `commercial`, HPO stays `unknown`; uniprot `text_mining` override note; `cdisc_bc` note (download-only, no serializer, licence unverified). §4 storage → Postgres tables with idempotent delete-by-`source_file` and guard (d0) (rows whose `id` reappears under a different `source_file` are replaced); add the structured-source serialisation addendum (frozen `serialize_<src>` contract: return keys `inputs/ok/failed/rows`, `id=f"{source}:{native_id}"`, event `serialize_commit`, records without a native id are skipped and counted `skipped_no_id`) and the shared input-identity rule (`input_key`: raw-dir-relative path joined with `__`, flat layouts keep the basename; SP2 extractors still use basename identity — deferred). §10 entrypoints → real paths under `scripts/data/`; add a `docs/11` cross-reference for audit obligations. Verify every claim against `article_schema.py`, `checkpoint_markers.py`, the serializers and `postgres_loader.py`; do not restate anything you did not check.
- [ ] **Step 3: GREEN**; commit `docs(sp5): extraction contract reflects Postgres storage, public_domain and input identity`.

---

### Task 5: `docs/08` and `docs/07`

**Files:** Modify `docs/08-data-storage-principles.md`, `docs/07-knowledge-graph-lessons.md`; Create `tests/docs/test_storage_docs.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/docs/test_storage_docs.py
from ._repo import REPO, doc


def test_docs_08_marks_iceberg_superseded_and_names_postgres():
    t = doc("08-data-storage-principles.md")
    assert "ADR-0001" in t or "0001-hybrid-storage-architecture" in t
    assert "superseded" in t.lower()
    assert "Postgres" in t and "Parquet" in t


def test_docs_07_uses_postgres_property_tables():
    t = doc("07-knowledge-graph-lessons.md")
    for term in ("article_cites", "article_mesh", "SQL/PGQ"):
        assert term in t, term
    assert "defer the graph DB" not in t


def test_referenced_adrs_exist():
    for name in ("0001-hybrid-storage-architecture.md", "0002-data-model-storage-and-partitioning.md"):
        assert (REPO / "docs" / "adr" / name).is_file()
```

- [ ] **Step 2: RED**, then edit. `docs/08`: rewrite §1 Decision/§3 layered model/§6 layout to the shipped design (Postgres hybrid + Parquet corpus under `03_corpus`; layout of `01_raw`, `02_processed/staging`, `_ops`); mark §4 (Iceberg) and §5 (OpenMetadata-as-store) as superseded by ADR-0001 with a one-paragraph pointer, keeping non-negotiables that still hold. Read ADR-0001/0002, `schema.sql`, `config.py` root settings first. `docs/07`: §3.2 representation choice → Postgres property tables (`article_cites`, `article_mesh`) plus committed SQL/PGQ (note the schema.sql guard: `CREATE PROPERTY GRAPH` is wrapped in a DO block that emits a NOTICE if the grammar rejects it on the current build — state this honestly), drop the "defer the graph DB" stance, keep the "structured published metadata over LLM-extracted triples" thesis and edge priorities; update §7/§8 phasing to the shipped state.
- [ ] **Step 3: GREEN**; commit `docs(sp5): storage and graph docs reflect the shipped Postgres hybrid`.

---

### Task 6: `docs/10` operator runbook rewrite

**Files:** Modify `docs/10-data-sources-runbook.md`; Create `tests/docs/test_runbook_doc.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/docs/test_runbook_doc.py
import re

from ._repo import REPO, doc, lit_sources, structured_sources, wrapper_table

STAGES = {"all", "download", "extract", "load", "graph", "materialize", "enrich", "serialize"}


def _cmds():
    return re.findall(r"run_pipeline\.sh\s+(\w+)\s+(\w+)", doc("10-data-sources-runbook.md"))


def _allowed(src: str, stage: str) -> bool:
    if src == "corpus":
        return stage == "materialize"
    if stage in ("download", "all"):
        return src in wrapper_table()
    if stage in ("extract",):
        return src in lit_sources()
    if stage == "graph":
        return src in lit_sources() or src == "mesh"
    if stage == "serialize":
        return src in structured_sources()
    if stage == "load":
        return src in lit_sources() or src in structured_sources() or src == "europepmc_id_mappings"
    if stage == "enrich":
        return src == "europepmc_lite"
    return False


def test_documented_commands_are_accepted_by_the_dispatcher_rules():
    cmds = _cmds()
    assert cmds, "runbook shows no run_pipeline.sh commands"
    for src, stage in cmds:
        assert stage in STAGES, (src, stage)
        assert _allowed(src, stage), f"runbook documents an unwired command: {src} {stage}"


def test_every_wired_source_has_a_runbook_section_or_inventory_pointer():
    t = doc("10-data-sources-runbook.md")
    for src in wrapper_table():
        assert src in t, src


def test_guard_and_goLive_are_documented():
    t = doc("10-data-sources-runbook.md")
    for term in ("EPISTEME_DB_MODE", "EPISTEME_DB_TARGET", "PGDATABASE_SECONDARY", "episteme_test", "restricted", "12-source-inventory"):
        assert term in t, term


def test_paths_mentioned_exist():
    for p in set(re.findall(r"scripts/data/[\w./-]+\.sh", doc("10-data-sources-runbook.md"))):
        assert (REPO / p).is_file(), p
```

- [ ] **Step 2: RED**, then rewrite `docs/10` as the single operator runbook. Keep the existing per-source known-failure tables and the ops-cadence table where still accurate (verify against wrappers). Required structure: 0 Read this first (pointer to `docs/12` replacing the dated status board; pointer to `docs/11`); 1 Environment and `.env` (KEY names only; `EPISTEME_ACTOR`, `PG*`, `EPISTEME_DATA_ROOT`/`_RAW_ROOT`, `NCBI_API_KEY` named not shown; the no-inline-comment rule for `.env` values because the shell loader does not strip them); 2 Database setup (`scripts/data/db/init_database.sh`, migrations, `episteme` primary / `episteme_test` secondary); 3 The DB-mode guard (modes, `EPISTEME_DB_TARGET`, `restricted` default, guard checks DB name only, agents/CI target `episteme_test`, go-live `EPISTEME_DB_MODE=restricted`); 4 Pipeline model (`run_pipeline.sh <source> <stage> [--dry-run] [--force] [--reason R] [--max-files N]`, stage matrix, rc 3 for unwired combinations, `--dry-run` is download-only, audit bracket `run_start`/`run_end`, `--force` requires `--reason`); 5 One section per source group with first-time and incremental blocks using real commands: literature (pubmed, pmc, apollo, europepmc_*, bookshelf, guidelines), structured (chembl, uniprot, pubchem, clinvar, reactome, mesh, ontologies, openalex incl. bounded `--max-files`), volatile download-only (dailymed, openfda, aact), acquisition (hf_corpus), `cdisc_bc` (download-only, pinned to a COSMoS commit SHA, PROVENANCE.txt, licence UNVERIFIED); 6 Materialize and ingest (`corpus materialize`, `load_articles`); 7 Verify (`verify_audit_trail.sh`, `source_inventory.sh`); 8 Ops cadence; 9 Document control. Every command line inside code fences must be one the dispatcher accepts (the test enforces this).
- [ ] **Step 3: GREEN**; commit `docs(sp5): operator runbook rewrite covering every wired source and the DB-mode guard`.

---

### Task 7: Reconciliations, README, link test

**Files:** Modify `docs/02-data-sources.md`, `README.md`, and any doc the link test flags; Create `tests/docs/test_doc_links.py`.

- [ ] **Step 1: Failing test**

```python
# tests/docs/test_doc_links.py
import re

from ._repo import REPO

DOC_FILES = sorted((REPO / "docs").glob("*.md")) + [REPO / "README.md"]


def test_relative_doc_references_resolve():
    bad = []
    for f in DOC_FILES:
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"`((?:docs/)?\d\d-[\w-]+\.md)`|\]\((?!https?:)([^)#\s]+\.md)", text):
            ref = m.group(1) or m.group(2)
            candidates = [REPO / ref, f.parent / ref, REPO / "docs" / ref]
            if not any(c.is_file() for c in candidates):
                bad.append((f.name, ref))
    assert not bad, bad
```

- [ ] **Step 2: RED** (expect `docs/02` → `11-data-roadmap.md`), then fix: `docs/02` lines ~5 and ~223 — replace `11-data-roadmap.md` with `12-source-inventory.md` (live inventory) and the roadmap spec path `superpowers/specs/2026-09-02-phase0-data-roadmap.md`; keep the "subordinate to this catalog" sense. `README.md`: verify `train_cpt` invocations against `src/episteme/model` (fix if stale), point the data section at `docs/10`, `docs/11`, `docs/12`. Fix any other dangling references the test reports (do not weaken the test).
- [ ] **Step 3: GREEN**; commit `docs(sp5): reconcile docs/02 and README; add doc-link test`.

---

### Task 8: Full sweep, cold-read run, drift entry

**Files:** Modify `docs/project-incubation-baseline.md` (+ doc fixes the cold-read finds); the cold-read transcript goes in the SDD workspace, not the repo. Task 8 is split: the controller runs Step 3 (cold-read agent) between the implementer's Steps 1-2 and Steps 4-5.

- [ ] **Step 1: Suites.** `.venv/Scripts/python.exe -m pytest -q -m "not pg" -p no:cacheprovider` (≈8 min; run in background and wait) → green; record the count. Then `tests/docs` alone → green.
- [ ] **Step 2: Static checks.** `bash -n` on `scripts/data/source_inventory.sh`; the grep gate (Global Constraints) → empty; `ruff check`/`format --check` on `tests/docs` and `src/episteme/data/source_inventory.py`.
- [ ] **Step 3: Cold-read run (controller-dispatched, separate fresh agent — NOT this implementer).** The controller launches it after Step 2; the implementer does not run it. The cold-read agent receives only `docs/10` and the repo, runs (`PGDATABASE=episteme_test`, server 19beta3 confirmed first): `cdisc_bc download --max-files 2`, then one bounded structured source through download → serialize → load (candidate: `mesh`, bounded, or `openalex --max-files 1`), then `verify_audit_trail.sh` and `source_inventory.sh`. It reports every point where `docs/10` was wrong, missing, or ambiguous. Findings become doc fixes committed here (`docs(sp5): fixes from cold-read run`); scratch outputs are cleaned with `git clean -ndx` review first.
- [ ] **Step 4: Drift-log entry** in `docs/project-incubation-baseline.md`, dated, matching the SP4.1 entry style: SP5 landed (spec + plan paths); the new/rewritten docs (07, 08, 09, 10, 11, 12, 02/README reconciliations); the generator and `tests/docs` consistency suite; what the cold-read run found; every gap where older design text and code disagreed (13 audit event types vs the design's 12; `chattr +a` / rotate script status as verified; `_audit` grants); carried-forward deferred items (SP2-extractor identity migration, `uv.lock` staleness, pyeuropepmc upgrade for cryptography alerts, streaming shard writes, UCUM parser, full-scale runs, `cdisc_bc` serializer, ledger minors M-4..M-8 from SP4.1, the `docs/09` open licence questions if any).
- [ ] **Step 5: Commit** `docs(sp5): drift-log entry — docs and governance landed`.
