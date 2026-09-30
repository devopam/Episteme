#!/usr/bin/env python3
"""Acquisition: europepmc_preprint — per-ID REST harvest of Europe PMC preprint
full-text XML.

Background (2026-09-08 spike): EBI **discontinued** the bulk Europe PMC preprint
feed. ``ftp.ebi.ac.uk/pub/databases/pmc/preprints/`` now holds only
``pprid.txt.gz`` (~73k ids like ``PPR7001``) + a privacy notice — no range
archives. Preprint full-text is now per-ID:
``GET {europepmc_base}/{PPRid}/fullTextXML``.

So this downloader:

1. GETs ``{europepmc_preprint_base}/pprid.txt.gz`` (the small bulk id list),
   gunzips it, and splits it into ``PPR...`` ids.
2. For each id not already done, GETs ``{europepmc_base}/{id}/fullTextXML`` and
   writes ``raw_dir/{id}.xml`` (bytes).
3. Records each completed id in ``raw_dir/.harvest_state`` (a newline list) so a
   re-run resumes. An id also counts as done if ``raw_dir/{id}.xml`` exists.

Retry / error policy per id:
  * 200            -> write ``{id}.xml``, append id to ``.harvest_state``.
  * 429 / 503      -> exponential backoff ``min(2**attempt, 60)`` s, 5 retries,
                      then count as an error and move on.
  * 404            -> count as an error and skip; the id is NOT appended to
                      ``.harvest_state`` (preprint withdrawn / no full text yet —
                      a later run retries it).
  * other non-200  -> count as an error and skip.

``--since`` (ISO date): the Europe PMC REST id-list endpoint has NO server-side
"modified since" filter for this bulk id file, so ``--since`` is accepted but is
a **documented no-op** — it emits a WARN and changes nothing.

``--dry-run``: best-effort fetch of the (small) id list, print
``would harvest <N> ids from <base>``, and return without writing anything to
disk. If the id list cannot be fetched (EBI unreachable) it reports ``0`` and
still returns 0 — a dry-run must be side-effect-free and must not fail on the
network.

No ``os.environ`` reads — endpoints come from ``episteme.config.get_settings()``.
"""

from __future__ import annotations

import argparse
import gzip
import sys
import time
from pathlib import Path

import requests

from episteme.config import get_settings

_STATE_FILE = ".harvest_state"
_RETRY_STATUSES = (429, 503)
_RETRY_BUDGET = 5
_BACKOFF_CAP = 60


def _id_sort_key(ppr_id: str) -> int:
    """Numeric part of a ``PPR{n}`` id (0 if unparseable)."""
    try:
        return int(ppr_id[3:])
    except (ValueError, IndexError):
        return 0


def _fetch_id_list() -> list[str]:
    """GET + gunzip ``{europepmc_preprint_base}/pprid.txt.gz`` -> ``PPR...`` ids,
    **newest first** (highest numeric id first).

    ``pprid.txt.gz`` is published in ascending id order and only recent preprints
    carry a JATS full text in Europe PMC — an ascending harvest would burn tens of
    thousands of sequential 404s before fetching anything. Sorting descending
    makes ``--max-files N`` return N usable preprints promptly; the
    ``.harvest_state`` + on-disk resume checks are order-independent, so this
    only changes *which* N a capped run gets, not correctness.
    """
    base = get_settings().europepmc_preprint_base
    resp = requests.get(f"{base}/pprid.txt.gz", timeout=60)
    resp.raise_for_status()
    text = gzip.decompress(resp.content).decode("utf-8", errors="replace")
    ids: list[str] = []
    for line in text.splitlines():
        tok = line.strip()
        if tok:
            ids.append(tok)
    ids.sort(key=_id_sort_key, reverse=True)
    return ids


def _load_state(raw_dir: Path) -> set[str]:
    state = raw_dir / _STATE_FILE
    if not state.is_file():
        return set()
    return {ln.strip() for ln in state.read_text(encoding="utf-8").splitlines() if ln.strip()}


def _append_state(raw_dir: Path, ppr_id: str) -> None:
    with (raw_dir / _STATE_FILE).open("a", encoding="utf-8") as fh:
        fh.write(f"{ppr_id}\n")


def _fetch_one(ppr_id: str) -> tuple[str, bytes | None]:
    """GET ``{europepmc_base}/{id}/fullTextXML`` with 429/503 backoff.

    Returns ``("ok", body)`` on 200, else ``("error", None)`` (404 / exhausted
    retries / other non-200).
    """
    url = f"{get_settings().europepmc_base}/{ppr_id}/fullTextXML"
    for attempt in range(_RETRY_BUDGET + 1):
        try:
            resp = requests.get(url, timeout=30)
        except requests.RequestException:
            if attempt < _RETRY_BUDGET:
                time.sleep(min(2**attempt, _BACKOFF_CAP))
                continue
            return "error", None
        if resp.status_code == 200:
            return "ok", resp.content
        if resp.status_code in _RETRY_STATUSES and attempt < _RETRY_BUDGET:
            time.sleep(min(2**attempt, _BACKOFF_CAP))
            continue
        # 404 (withdrawn / no full text), exhausted retries, or other non-200
        return "error", None
    return "error", None


def download_preprints(
    raw_dir: Path | str,
    *,
    max_files: int = 0,
    since: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Harvest Europe PMC preprint full-text XML into ``raw_dir``.

    Args:
        raw_dir: destination directory for ``{id}.xml`` + ``.harvest_state``.
        max_files: cap on *newly fetched* ids this run (0 = no cap).
        since: accepted but a **no-op** (WARN) — the bulk id feed has no
            server-side "modified since" filter.
        dry_run: fetch only the id list, print ``would harvest N ids``, write
            nothing. Offline-safe: reports 0 if the id list can't be fetched.

    Returns:
        ``{"ids": int, "fetched": int, "skipped": int, "errors": int}`` where
        ``skipped`` counts ids that were already done when this run started
        (on a full re-run that is every id scanned).
    """
    raw_dir = Path(raw_dir)
    base = get_settings().europepmc_preprint_base

    if since:
        print(
            f"WARN: --since={since!r} is a no-op for the Europe PMC bulk preprint "
            "id feed (pprid.txt.gz has no server-side 'modified since' filter); "
            "ignoring.",
            file=sys.stderr,
        )

    if dry_run:
        try:
            n = len(_fetch_id_list())
        except Exception as exc:  # noqa: BLE001 — dry-run must not fail on the network
            print(f"WARN: could not fetch id list for dry-run ({exc}); reporting 0")
            n = 0
        print(f"would harvest {n} ids from {base}")
        return {"ids": n, "fetched": 0, "skipped": 0, "errors": 0}

    raw_dir.mkdir(parents=True, exist_ok=True)
    ids = _fetch_id_list()
    done = _load_state(raw_dir)

    fetched = skipped = errors = 0
    for ppr_id in ids:
        if max_files and fetched >= max_files:
            break
        if ppr_id in done or (raw_dir / f"{ppr_id}.xml").exists():
            skipped += 1
            continue
        status, body = _fetch_one(ppr_id)
        if status == "ok" and body is not None:
            (raw_dir / f"{ppr_id}.xml").write_bytes(body)
            _append_state(raw_dir, ppr_id)
            fetched += 1
        else:
            errors += 1

    print(f"europepmc_preprint: ids={len(ids)} fetched={fetched} skipped={skipped} errors={errors}")
    return {"ids": len(ids), "fetched": fetched, "skipped": skipped, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Europe PMC preprint full-text REST harvest (per-ID; the bulk feed is gone)",
    )
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=get_settings().raw_root / "europepmc" / "preprints",
    )
    p.add_argument("--max-files", type=int, default=0, help="cap on newly fetched ids (0 = no cap)")
    p.add_argument("--since", default=None, help="ISO date — ACCEPTED BUT A NO-OP (see --help)")
    p.add_argument(
        "--dry-run",
        "--dry_run",
        dest="dry_run",
        action="store_true",
        help="fetch the id list only, print the count, write nothing",
    )
    args = p.parse_args(argv)

    download_preprints(
        args.raw_dir,
        max_files=args.max_files,
        since=args.since,
        dry_run=args.dry_run,
    )
    # A per-id 404/429/503 is an expected, individually-logged outcome (a
    # withdrawn preprint, a rate limit) — never fatal to the pipeline stage. In
    # particular, a *resumed* harvest where every id in the traversal is already
    # done except a residual handful of persistently-404ing ids would otherwise
    # report fetched=0 errors>0 and wrongly fail an idempotent no-op re-run.
    # download_preprints() already printed the ids/fetched/skipped/errors line;
    # a real failure (id-list fetch unreachable, bad config) raises out of
    # download_preprints() before this point and surfaces as a non-zero exit
    # via the uncaught exception, not via this return value.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
