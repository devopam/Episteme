"""Shared schema constants and row helpers (extraction contract v1.1)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.2"
# Schema changelog:
#   1.1  contract v1.1 (2026-08-31 sample audit): PMC provenance cols, license norm, status rules.
#   1.2  (SP1-α, 2026-09-02): SOURCES extended to the full Phase-0 roadmap list.
#        The row shape stays PROVISIONAL — refined per-source against real data before full load
#        (roadmap §4.2). Bump this + append a line on every refinement.

# Minimum text length for extract_status=ok when abstract/body absent
MIN_OK_TEXT_LEN = 200

SOURCES = (
    "pubmed", "pmc", "bookshelf",
    "europepmc_preprint", "europepmc_manuscript", "europepmc_lite",
    "apollo", "guidelines",
    "chembl", "uniprot", "pubchem", "clinvar", "reactome", "mesh", "ontologies", "openalex",
)

SUBSETS = ("commercial", "text_mining", "open_metadata", "other")

EXTRACT_STATUSES = ("ok", "empty", "partial", "dropped")

ARTICLE_COLUMNS = [
    "id",
    "source",
    "source_file",
    "source_record_id",
    "pmid",
    "pmcid",
    "doi",
    "title",
    "abstract",
    "body_text",
    "text",
    "authors",
    "journal",
    "year",
    "mesh",
    "publication_types",
    "language",
    "license",
    "license_url",
    "license_raw",
    "subset",
    "is_retracted",
    "extract_status",
    "extract_notes",
    "retrieved_at",
    "content_hash",
    "pmc_version",
    "is_manuscript",
    "is_historical_ocr",
    "pdf_url",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_whitespace(s: str) -> str:
    s = s.replace("\x00", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def build_text(
    title: str | None,
    abstract: str | None,
    body_text: str | None = None,
) -> str:
    parts = [p for p in (title, abstract, body_text) if p and str(p).strip()]
    return normalize_whitespace("\n\n".join(parts))


def content_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def decide_extract_status(
    *,
    text: str | None,
    abstract: str | None,
    body_text: str | None,
    has_id: bool,
) -> tuple[str, str | None]:
    """Return (extract_status, extract_notes)."""
    t = (text or "").strip()
    abs_ok = bool(abstract and str(abstract).strip())
    body_ok = bool(body_text and str(body_text).strip())

    if not t and not abs_ok and not body_ok:
        if has_id:
            return "empty", "no_title_abstract_body"
        return "dropped", "no_id_no_text"

    if abs_ok or body_ok or len(t) >= MIN_OK_TEXT_LEN:
        return "ok", None

    return "partial", f"short_text_len={len(t)}"


def normalize_license(raw: str | None) -> tuple[str, str | None, str | None]:
    """
    Returns (license, license_url, license_raw).
    license is a short normalised code.
    """
    if not raw or not str(raw).strip():
        return "unknown", None, None

    s = str(raw).strip()
    url = None
    m = re.search(r"https?://creativecommons\.org/licenses/[^\s)\"']+", s, re.I)
    if m:
        url = m.group(0).rstrip(".,;")

    u = s.upper()
    # Order matters: NC before plain BY
    if re.search(r"\bCC0\b", u) or "CC-0" in u:
        return "CC0", url, s[:300]
    if "BY-NC-ND" in u or "BY-NC-ND" in u.replace(" ", ""):
        return "CC BY-NC-ND", url, s[:300]
    if "BY-NC-SA" in u:
        return "CC BY-NC-SA", url, s[:300]
    if "BY-NC" in u:
        return "CC BY-NC", url, s[:300]
    if "BY-SA" in u:
        return "CC BY-SA", url, s[:300]
    if "BY-ND" in u:
        return "CC BY-ND", url, s[:300]
    if re.search(r"\bCC\s*BY\b", u) or "CREATIVE COMMONS ATTRIBUTION" in u:
        return "CC BY", url, s[:300]
    if "TEXT MINING" in u or "TEXT-MINING" in u or "FAIR USE" in u:
        return "text_mining", url, s[:300]

    return "unknown", url, s[:300]


def subset_from_license(license_code: str, *, default: str = "open_metadata") -> str:
    if license_code in ("CC0", "CC BY", "CC BY-SA", "CC BY-ND"):
        return "commercial"
    if license_code.startswith("CC BY-NC") or license_code == "text_mining":
        return "text_mining"
    return default


def empty_article_row() -> dict[str, Any]:
    return {c: None for c in ARTICLE_COLUMNS}


def finalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Fill text, status, hash, retrieved_at if missing."""
    out = empty_article_row()
    out.update({k: v for k, v in row.items() if k in out})

    if not out.get("text"):
        text = build_text(out.get("title"), out.get("abstract"), out.get("body_text"))
        out["text"] = text or None

    has_id = bool(out.get("id") or out.get("pmid") or out.get("pmcid") or out.get("doi"))
    status, notes = decide_extract_status(
        text=out.get("text"),
        abstract=out.get("abstract"),
        body_text=out.get("body_text"),
        has_id=has_id,
    )
    if not out.get("extract_status"):
        out["extract_status"] = status
    if notes and not out.get("extract_notes"):
        out["extract_notes"] = notes

    if not out.get("content_hash"):
        out["content_hash"] = content_hash(out.get("text"))

    if not out.get("retrieved_at"):
        out["retrieved_at"] = utc_now_iso()

    if out.get("is_retracted") is None:
        out["is_retracted"] = False

    if not out.get("subset"):
        out["subset"] = "open_metadata"

    # year partition sentinel
    if out.get("year") is None:
        pass  # leave null; writer maps to 0 for partition path

    return out
