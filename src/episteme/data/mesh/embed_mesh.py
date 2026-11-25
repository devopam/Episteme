"""MeSH embed stage: MedCPT article-encoder vectors of the MeSH vocabulary.

Reads ``episteme.mesh_term`` (filled by the mesh graph phase) and the descriptor prose of
``episteme.article_body`` (source ``mesh``), encodes them and writes ``episteme.mesh_term_vec``.

Rows per descriptor:

* one ``descriptor`` row: the pair ``(name, scope note)``;
* one ``entry`` row per entry term: the pair ``(term, '')``.

The ``text`` column of ``mesh_term_vec`` keeps the title string (descriptor name or entry term),
which is what a reviewer sees. The scope note is encoder input only.

``vocabulary_texts`` is the single builder of these texts. ``embed`` encodes exactly what it
returns. Restart rule: a descriptor whose rows already exist for the same ``mesh_release`` and
``model_revision`` is skipped. Rows of another release or revision are deleted first. Each batch
of descriptors is one transaction, so a kill leaves whole descriptors only. The restart does
not use an ``input_key`` checkpoint: the input is a database table, and the rows already in
``mesh_term_vec`` are the checkpoint.

Audit (GxP): the stale delete and each batch write one ``episteme._audit`` row in the same
transaction as their change (``new_value.step`` = ``delete_stale`` or ``batch``), so a run that
stops midway leaves a record of every committed change. A summary row (``step`` = ``summary``)
closes a run that finished. All rows have the object ``mesh embed``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from itertools import groupby
from typing import Any

from episteme import audit_trail

DEFAULT_BATCH = 32
AUDIT_OBJECT = "mesh embed"

# The sentence serialize_mesh._build_text appends to every descriptor text.
_BOILERPLATE = " As defined in the NLM Medical Subject Headings (MeSH) thesaurus."

_COLUMNS = "descriptor_ui, text_kind, text, embedding, model, model_revision, mesh_release"


def scope_note_of(name: str, text: str | None) -> str:
    """The scope note inside ``episteme.article_body.text`` of one MeSH descriptor.

    The serializer builds ``"<name>. <scope note> As defined in ... thesaurus."`` (the scope
    note and its leading space are omitted when absent). Strip the leading name and the
    closing sentence. Return an empty string when no scope note remains.
    """
    body = " ".join((text or "").split())
    lead = f"{name}."
    if body.startswith(lead):
        body = body[len(lead) :]
    if body.endswith(_BOILERPLATE):
        body = body[: -len(_BOILERPLATE)]
    elif body.strip() == _BOILERPLATE.strip():
        body = ""
    return body.strip()


def _release(conn: Any) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT mesh_release FROM episteme.mesh_term ORDER BY 1")
        rels = [r[0] for r in cur.fetchall()]
    if len(rels) != 1:
        raise RuntimeError(
            f"episteme.mesh_term must hold exactly one MeSH release, found {rels}; "
            "run the mesh graph phase first"
        )
    return rels[0]


def vocabulary_texts(conn: Any) -> list[tuple[str, str, str, str]]:
    """All vocabulary texts as ``(descriptor_ui, text_kind, title, text)``.

    ``(title, text)`` is the pair given to ``encode_articles``. Order: descriptor_ui, then the
    descriptor row, then the entry rows by term. A descriptor needs a ``name`` row in
    ``mesh_term``; one without it is not listed.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.descriptor_ui, t.term, b.text "
            "FROM (SELECT DISTINCT ON (descriptor_ui) descriptor_ui, term "
            "      FROM episteme.mesh_term WHERE term_kind = 'name' "
            "      ORDER BY descriptor_ui, is_preferred DESC, term) t "
            "LEFT JOIN episteme.article_body b "
            "  ON b.article_id = 'mesh:' || t.descriptor_ui AND b.source = 'mesh' "
            "ORDER BY t.descriptor_ui"
        )
        names = cur.fetchall()
        cur.execute(
            "SELECT DISTINCT descriptor_ui, term FROM episteme.mesh_term "
            "WHERE term_kind = 'entry' ORDER BY descriptor_ui, term"
        )
        entries = cur.fetchall()
    by_ui: dict[str, list[str]] = {
        ui: [r[1] for r in grp] for ui, grp in groupby(entries, key=lambda r: r[0])
    }
    out: list[tuple[str, str, str, str]] = []
    for ui, name, text in names:
        out.append((ui, "descriptor", name, scope_note_of(name, text)))
        for term in by_ui.get(ui, []):
            out.append((ui, "entry", term, ""))
    return out


def _vec_text(vec: Any) -> str:
    return "[" + ",".join(f"{float(x):.8g}" for x in vec) + "]"


def embed(
    conn: Any,
    encoder: Any,
    *,
    limit: int | None = None,
    batch: int = DEFAULT_BATCH,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Embed the vocabulary. Returns ``release``, ``deleted_stale``, ``embedded``, ``skipped_done``.

    ``embedded`` and ``skipped_done`` count descriptors. ``limit`` caps the descriptors embedded
    in this run (all rows of each). ``batch`` is the number of descriptors per transaction.
    """
    if batch < 1:
        raise ValueError("batch must be at least 1")
    release = _release(conn)
    model, revision = encoder.model_id, encoder.revision

    basis = {"release": release, "model": model, "model_revision": revision}

    def audit(step: str, rows: int, **extra: Any) -> None:
        audit_trail.record(
            "graph_commit",
            conn=conn,
            object=AUDIT_OBJECT,
            rows_affected=rows,
            new_value={"step": step, **basis, **extra},
            run_id=run_id,
        )

    # deferred_mirror: a JSONL mirror line is written only after its row commits.
    with audit_trail.deferred_mirror(conn):
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM episteme.mesh_term_vec "
                "WHERE mesh_release IS DISTINCT FROM %s OR model_revision IS DISTINCT FROM %s",
                (release, revision),
            )
            deleted_stale = cur.rowcount
            cur.execute("SELECT DISTINCT descriptor_ui FROM episteme.mesh_term_vec")
            done = {r[0] for r in cur.fetchall()}
        # The delete and its audit row commit together.
        audit("delete_stale", deleted_stale, deleted_stale=deleted_stale)
        audit_trail.commit(conn)

    rows = vocabulary_texts(conn)
    groups = [(ui, list(g)) for ui, g in groupby(rows, key=lambda r: r[0])]
    skipped_done = sum(1 for ui, _ in groups if ui in done)
    todo = [g for g in groups if g[0] not in done]
    if limit is not None:
        todo = todo[: max(limit, 0)]

    embedded = 0
    with audit_trail.deferred_mirror(conn):
        for start in range(0, len(todo), batch):
            chunk = todo[start : start + batch]
            flat = [r for _, grp in chunk for r in grp]
            vecs = encoder.encode_articles([(r[2], r[3]) for r in flat])
            if len(vecs) != len(flat):
                raise RuntimeError(f"encoder returned {len(vecs)} vectors for {len(flat)} texts")
            with conn.cursor() as cur:
                with cur.copy(f"COPY episteme.mesh_term_vec ({_COLUMNS}) FROM STDIN") as cp:
                    for (ui, kind, title, _text), vec in zip(flat, vecs, strict=True):
                        cp.write_row((ui, kind, title, _vec_text(vec), model, revision, release))
            # The batch rows and their audit row commit together.
            audit(
                "batch",
                len(flat),
                descriptors=len(chunk),
                first_descriptor=chunk[0][0],
                last_descriptor=chunk[-1][0],
            )
            audit_trail.commit(conn)
            embedded += len(chunk)

        audit(
            "summary",
            embedded,
            embedded=embedded,
            skipped_done=skipped_done,
            deleted_stale=deleted_stale,
        )
        audit_trail.commit(conn)
    return {
        "release": release,
        "deleted_stale": deleted_stale,
        "embedded": embedded,
        "skipped_done": skipped_done,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="embed_mesh", description=__doc__.splitlines()[0])
    p.add_argument("--limit", type=int, default=None, help="embed at most N descriptors (trial)")
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="descriptors per transaction")
    args = p.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        p.error("--limit must be at least 1")
    if args.batch < 1:
        p.error("--batch must be at least 1")

    from episteme.config import get_settings
    from episteme.data.db.connection import connection
    from episteme.reference.medcpt import MedCPTEncoder

    run_id = get_settings().run_id or (
        f"mesh-embed-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    encoder = MedCPTEncoder.article()
    try:
        with connection() as conn:
            res = embed(conn, encoder, limit=args.limit, batch=args.batch, run_id=run_id)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"done: release={res['release']} embedded={res['embedded']} "
        f"skipped_done={res['skipped_done']} deleted_stale={res['deleted_stale']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
