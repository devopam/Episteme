"""EDQM Standard Terms, every class the API lists.

The request signature is the one the owner recorded: ``X-STAPI-KEY`` is
``login|signature``, and the signature is the last 22 characters of
Base64(HMAC-SHA512(secret, ``VERB&URI&HOST&Date``)).

``/classes`` names the classes. ``full_data_by_class/{class}/1/1?tags=1``
returns every term of that class, traditional and non-traditional. ``1`` in
the second and third path positions means no further class filter.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from pathlib import Path

from defusedxml.ElementTree import parse as safe_parse

from episteme.data.refmeta.model import CodeRelease, CodeRow

PAGE = 200
_CODE = re.compile(r"[A-Za-z0-9_-]+")
_VERSIONS_PATH = "/standardterms/api/v1/versions"
_SKIP = {"classes.json", "versions.json"}


def stapi_key(login: str, secret: str, *, verb: str, uri: str, host: str, date: str) -> str:
    message = f"{verb}&{uri}&{host}&{date}".encode()
    signature = base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha512).digest())
    return f"{login}|{signature.decode('ascii')[-22:]}"


def classes_uri(*, limit: int = PAGE, offset: int = 0) -> str:
    return f"/standardterms/api/v1/classes?limit={limit}&offset={offset}"


def class_data_uri(code: str, *, tags: bool = True) -> str:
    """``full_data_by_class`` has no paging (the API guide lists no limit/offset for
    it, and the server ignores them: every call returns the whole class)."""
    if not _CODE.fullmatch(code):
        raise ValueError(f"EDQM class code {code!r} is not a safe path segment")
    suffix = "?tags=1" if tags else ""
    return f"/standardterms/api/v1/full_data_by_class/{code}/1/1{suffix}"


def versions_uri() -> str:
    return _VERSIONS_PATH


def content_rows(payload: bytes) -> list:
    """The ``content`` array of one EDQM JSON response."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("EDQM response is not JSON") from exc
    if isinstance(data, list):
        return data
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, list):
        raise ValueError("EDQM response has no content list")
    return content


def class_codes(rows: list) -> list[str]:
    codes: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("EDQM /classes row is not an object")
        code = str(item.get("code") or "").strip()
        if not _CODE.fullmatch(code):
            raise ValueError(f"EDQM class code {code!r} is not a safe path segment")
        if code not in codes:
            codes.append(code)
    if not codes:
        raise ValueError("EDQM /classes returned no classes")
    return sorted(codes)


def parse_edqm_dir(
    directory: Path, *, notice: str, source_file: str, release_id: str
) -> CodeRelease:
    files = sorted(
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix in {".json", ".xml"} and p.name not in _SKIP
    )
    if not files:
        raise ValueError(f"{directory}: no EDQM term files")
    rows: list[CodeRow] = []
    for path in files:
        rows.extend(_read_json(path) if path.suffix == ".json" else _read_xml(path))
    version = release_id.split("-", 1)[0]
    return CodeRelease(
        system_key="edqm",
        release_id=release_id,
        version=version,
        licence_notice=notice,
        source_file=source_file,
        codes=tuple(rows),
    )


def _read_json(path: Path) -> list[CodeRow]:
    rows: list[CodeRow] = []
    for item in content_rows(path.read_bytes()):
        if not isinstance(item, dict):
            continue
        term_id = str(item.get("code") or item.get("id") or item.get("term_id") or "").strip()
        if not term_id:
            continue
        kind = str(item.get("class") or path.stem).strip() or path.stem
        extra = dict(item)
        extra["file"] = path.name
        rows.append(
            CodeRow(
                code=f"{kind}:{term_id}",
                display=_json_name(item),
                status=(str(item["status"]).strip() if item.get("status") else None),
                extra=extra,
            )
        )
    return rows


def _json_name(item: dict) -> str | None:
    # full_data_by_class rows carry the English term in "english" (2026-11-16);
    # "translations" holds every language, so it is only a fallback.
    for key in ("english", "term", "name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("translations", "terms", "names"):
        value = item.get(key)
        if isinstance(value, list):
            english = _english_entry(value)
            if english:
                return english
    return None


def _english_entry(entries: list) -> str | None:
    ranked: list[tuple[int, str]] = []
    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            ranked.append((1, entry.strip()))
            continue
        if not isinstance(entry, dict):
            continue
        lang = str(entry.get("language") or entry.get("lang") or entry.get("isocode") or "")
        text = entry.get("term") or entry.get("name")
        if isinstance(text, str) and text.strip():
            rank = 0 if lang.lower().startswith("en") else 1
            ranked.append((rank, text.strip()))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][1]


def _read_xml(path: Path) -> list[CodeRow]:
    root = safe_parse(str(path), forbid_dtd=True).getroot()
    rows: list[CodeRow] = []
    for node in root.iter():
        term_id = node.get("id") or node.get("term_id")
        if not term_id:
            continue
        kind = node.get("class") or node.get("class_code") or path.stem
        name = node.get("name") or _english(node)
        if not name:
            continue
        rows.append(
            CodeRow(
                code=f"{kind}:{term_id}",
                display=name,
                extra={"class": kind, "term_id": term_id, "file": path.name},
            )
        )
    if not rows:
        raise ValueError(f"{path}: no terms (expected an id and an English name)")
    return rows


def _english(node) -> str | None:
    texts: list[tuple[int, str]] = []
    for child in node.iter():
        if child is node or not (child.text and child.text.strip()):
            continue
        lang = child.get("lang") or child.get("{http://www.w3.org/XML/1998/namespace}lang") or ""
        rank = 0 if lang.lower().startswith("en") else 1
        texts.append((rank, child.text.strip()))
    if not texts:
        return None
    texts.sort()
    return texts[0][1]
