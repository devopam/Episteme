"""COPY of one FAERS zip into a generation: rows, dictionaries, progress row, audit row.

One transaction per zip. ``faers.load`` runs it; see that module for the rules.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql

from episteme import audit_trail
from episteme.config import get_settings
from episteme.data import checkpoint_markers
from episteme.data.regulatory.faers.generation import (
    SCHEMA,
    SOURCE,
    GenerationError,
    _ident,
    _put_state,
    generation_table,
)
from episteme.data.regulatory.faers.parse import count_report, iter_reports, report_rows
from episteme.data.regulatory.normalize import fits_index

_REPORT_COLUMNS = (
    "safetyreportid",
    "receive_year",
    "receive_date",
    "receipt_date",
    "source_receive_date",
    "version",
    "serious",
    "serious_death",
    "serious_hospitalization",
    "serious_life_threatening",
    "serious_disabling",
    "serious_congenital",
    "serious_other",
    "country",
    "sex",
    "age_years",
    "weight_kg",
)
_DRUG_COLUMNS = (
    "safetyreportid",
    "seq",
    "receive_year",
    "role",
    "batch_number",
    "batch_norm",
    "route",
    "indication",
)
_REACTION_COLUMNS = ("safetyreportid", "seq", "receive_year", "outcome")
# Estimated bytes of one row beyond its text.
_ROW_OVERHEAD = 64


class Batch:
    """The rows of one COPY round, up to about ``cap_bytes`` of row data."""

    def __init__(self, cap_bytes: int) -> None:
        self.cap_bytes = cap_bytes
        self.clear()

    def clear(self) -> None:
        self.reports: list[dict] = []
        self.drugs: list[dict] = []
        self.reactions: list[dict] = []
        self.bytes = 0

    @staticmethod
    def _size(row: dict) -> int:
        return _ROW_OVERHEAD + sum(len(v) for v in row.values() if isinstance(v, str))

    def add(self, report: dict, drugs: list[dict], reactions: list[dict]) -> None:
        self.reports.append(report)
        self.drugs.extend(drugs)
        self.reactions.extend(reactions)
        self.bytes += self._size(report)
        self.bytes += sum(self._size(r) for r in drugs)
        self.bytes += sum(self._size(r) for r in reactions)

    @property
    def full(self) -> bool:
        return self.bytes >= self.cap_bytes


# ---------------------------------------------------------------------------
# COPY of one zip
# ---------------------------------------------------------------------------
def _entry_md5(alias: str = "") -> str:
    """The key expression of ``faers_drug_entry_uq`` (migration 0010), exactly."""
    a = f"{alias}." if alias else ""
    return (
        f"md5(coalesce({a}product_name, '') || chr(31) || coalesce({a}active_substance, '') "
        f"|| chr(31) || coalesce({a}annotation, ''))"
    )


def _entry_key(drug: dict) -> tuple[str | None, str | None, str | None]:
    return (drug["product_name"], drug["active_substance"], drug["annotation"])


def _create_stage(cur: psycopg.Cursor) -> None:
    cur.execute(
        "CREATE TEMP TABLE _faers_new_entry (k integer, product_name text, "
        "active_substance text, annotation text) ON COMMIT DROP"
    )
    cur.execute("CREATE TEMP TABLE _faers_new_term (term text) ON COMMIT DROP")


def _resolve_entries(cur: psycopg.Cursor, drugs: list[dict], cache: dict) -> None:
    """Put the new combinations of ``drugs`` into the dictionary, and their ids in ``cache``."""
    new = list(dict.fromkeys(k for k in map(_entry_key, drugs) if any(k) and k not in cache))
    if not new:
        return
    cur.execute("TRUNCATE _faers_new_entry")
    with cur.copy(
        "COPY _faers_new_entry (k, product_name, active_substance, annotation) FROM STDIN"
    ) as cp:
        for i, key in enumerate(new):
            cp.write_row((i, *key))
    cur.execute("ANALYZE _faers_new_entry")
    cur.execute(
        f"INSERT INTO {SCHEMA}.faers_drug_entry (product_name, active_substance, annotation) "
        "SELECT product_name, active_substance, annotation FROM _faers_new_entry "
        f"ON CONFLICT (({_entry_md5()})) DO NOTHING"
    )
    cur.execute(
        f"SELECT t.k, e.drug_entry_id FROM _faers_new_entry t "
        f"JOIN {SCHEMA}.faers_drug_entry e ON {_entry_md5('e')} = {_entry_md5('t')}"
    )
    found = cur.fetchall()
    if len(found) != len(new):
        raise GenerationError(f"{len(new)} dictionary entries written, {len(found)} found")
    for k, entry_id in found:
        cache[new[k]] = entry_id


def _usable_term(term: str | None) -> bool:
    return bool(term) and fits_index(term)


def _resolve_terms(cur: psycopg.Cursor, reactions: list[dict], cache: dict) -> None:
    new = list(
        dict.fromkeys(
            r["term"] for r in reactions if _usable_term(r["term"]) and r["term"] not in cache
        )
    )
    if not new:
        return
    cur.execute("TRUNCATE _faers_new_term")
    with cur.copy("COPY _faers_new_term (term) FROM STDIN") as cp:
        for term in new:
            cp.write_row((term,))
    cur.execute("ANALYZE _faers_new_term")
    cur.execute(
        f"INSERT INTO {SCHEMA}.faers_term (term) SELECT term FROM _faers_new_term "
        "ON CONFLICT (term) DO NOTHING"
    )
    cur.execute(
        f"SELECT t.term, e.term_id FROM _faers_new_term t "
        f"JOIN {SCHEMA}.faers_term e ON e.term = t.term"
    )
    cache.update(cur.fetchall())


def _copy_stmt(table: str, columns: Sequence[str]) -> sql.Composed:
    return sql.SQL("COPY {t} ({cols}) FROM STDIN").format(
        t=_ident(table), cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    )


def _flush(
    cur: psycopg.Cursor,
    generation: str,
    source_file: str,
    batch: Batch,
    entries: dict,
    terms: dict,
) -> None:
    """One COPY round. The dictionaries first: the rows carry their ids."""
    _resolve_entries(cur, batch.drugs, entries)
    _resolve_terms(cur, batch.reactions, terms)
    if batch.reports:
        stmt = _copy_stmt(
            generation_table("faers_report", generation), (*_REPORT_COLUMNS, "source_file")
        )
        with cur.copy(stmt) as cp:
            for r in batch.reports:
                cp.write_row((*(r[c] for c in _REPORT_COLUMNS), source_file))
    if batch.drugs:
        stmt = _copy_stmt(
            generation_table("faers_drug", generation),
            (*_DRUG_COLUMNS, "drug_entry_id", "source_file"),
        )
        with cur.copy(stmt) as cp:
            for r in batch.drugs:
                entry_id = entries.get(_entry_key(r))
                cp.write_row((*(r[c] for c in _DRUG_COLUMNS), entry_id, source_file))
    if batch.reactions:
        stmt = _copy_stmt(
            generation_table("faers_reaction", generation),
            (*_REACTION_COLUMNS, "term_id", "source_file"),
        )
        with cur.copy(stmt) as cp:
            for r in batch.reactions:
                term_id = terms.get(r["term"])
                cp.write_row((*(r[c] for c in _REACTION_COLUMNS), term_id, source_file))
    batch.clear()


def _load_zip(
    conn: psycopg.Connection, generation: str, path: Path, key: str, run_id: str
) -> dict[str, int]:
    """Load one zip in one transaction. The caches and the id set last for this zip only."""
    counts: Counter = Counter(
        reports=0, drug_rows=0, reaction_rows=0, no_date=0, no_id=0, duplicates_in_zip=0
    )
    counts.update(long_terms=0, long_batches=0, long_ids=0)
    entries: dict = {}
    terms: dict = {}
    seen: set[str] = set()
    batch = Batch(get_settings().load_batch_bytes)
    try:
        with audit_trail.deferred_mirror(conn):
            with conn.cursor() as cur:
                _create_stage(cur)
                for report in iter_reports(path):
                    row, drugs, reactions = report_rows(report)
                    report_id = row["safetyreportid"]
                    if report_id is None:
                        counts["no_id"] += 1
                        continue
                    if not fits_index(report_id):
                        counts["long_ids"] += 1
                        continue
                    if report_id in seen:
                        counts["duplicates_in_zip"] += 1
                        continue
                    seen.add(report_id)
                    count_report(counts, row)
                    counts["reports"] += 1
                    counts["drug_rows"] += len(drugs)
                    counts["reaction_rows"] += len(reactions)
                    counts["long_terms"] += sum(
                        1 for r in reactions if r["term"] and not _usable_term(r["term"])
                    )
                    counts["long_batches"] += sum(d["batch_norm_too_long"] for d in drugs)
                    batch.add(row, drugs, reactions)
                    if batch.full:
                        _flush(cur, generation, key, batch, entries, terms)
                _flush(cur, generation, key, batch, entries, terms)
                _put_state(cur, generation, "zip", key, dict(counts))
            audit_trail.record(
                "load_commit",
                conn=conn,
                object=key,
                rows_affected=counts["reports"],
                run_id=run_id,
            )
            audit_trail.commit(conn)
    except BaseException:
        audit_trail.rollback(conn)
        raise
    return dict(counts)


def _write_zip_marker(
    processed_dir: Path, path: Path, key: str, generation: str, stats: dict
) -> None:
    """The marker of a zip this generation has loaded; a current one stays as it is."""
    marker = checkpoint_markers.load_success_marker_path(processed_dir, SOURCE, key)
    with contextlib.suppress(OSError, ValueError, AttributeError):
        stored = json.loads(marker.read_text(encoding="utf-8")).get("stats", {})
        if stored.get("generation") == generation:
            return
    st = path.stat()
    checkpoint_markers.write_marker(
        marker,
        {
            "source": SOURCE,
            "source_file": key,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "stats": {**stats, "generation": generation, "file": [st.st_size, st.st_mtime_ns]},
        },
    )
