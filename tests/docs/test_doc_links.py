"""Doc cross-references resolve, and no numbered doc is an orphan. Read-only."""

from __future__ import annotations

import re

from ._repo import REPO

DOC_FILES = sorted((REPO / "docs").glob("*.md")) + [REPO / "README.md"]
NUMBERED = sorted((REPO / "docs").glob("[0-9][0-9]-*.md"))

_FENCE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.S | re.M)
_REF = re.compile(
    r"`((?:docs/)?(?:superpowers/[\w./-]+/)?[\w-]+\.md)`"  # backticked path
    r"|\]\((?!https?:|mailto:)([^)#\s]+\.md)"  # markdown link target
)


def _prose(path) -> str:
    """File text with fenced code blocks removed (paths there are examples, not links)."""
    return _FENCE.sub("", path.read_text(encoding="utf-8"))


def _resolves(source, ref: str) -> bool:
    candidates = [REPO / ref, source.parent / ref, REPO / "docs" / ref]
    return any(c.is_file() for c in candidates)


def _refs(path):
    for m in _REF.finditer(_prose(path)):
        yield m.group(1) or m.group(2)


def test_relative_doc_references_resolve():
    bad = []
    for f in DOC_FILES:
        for ref in _refs(f):
            # Backticked bare names are only checked when they look like repo docs.
            if m := re.fullmatch(r"[\w-]+\.md", ref):
                if not re.match(r"\d\d-", m.group(0)):
                    continue
            if not _resolves(f, ref):
                bad.append((f.name, ref))
    assert not bad, bad


def test_no_orphan_numbered_docs():
    referenced = set()
    for f in DOC_FILES:
        for ref in _refs(f):
            name = ref.rsplit("/", 1)[-1]
            if name != f.name:
                referenced.add(name)
    orphans = [d.name for d in NUMBERED if d.name not in referenced]
    assert not orphans, orphans
