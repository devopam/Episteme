"""Graph relations of one PubMed ``<PubmedArticle>`` (schema 1.6).

Each function reads only its own element path, by direct children -- never
``iter()`` over ``PubmedData``, which also walks every cited reference's
``ArticleIdList`` (the pre-1.6 DOI bug).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from episteme.data.article_schema import cap_list_elements, cap_text
from episteme.data.graph_files import RELATIONS

_ORCID = re.compile(r"(\d{4})-?(\d{4})-?(\d{4})-?(\d{3}[\dX])$")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _kids(el, name: str) -> list:
    return [] if el is None else [c for c in el if _local(c.tag) == name]


def _kid(el, name: str):
    k = _kids(el, name)
    return k[0] if k else None


def _path(el, *names: str):
    for n in names:
        el = _kid(el, n)
        if el is None:
            return None
    return el


def _text(el) -> str | None:
    if el is None:
        return None
    t = "".join(el.itertext()).strip()
    return t or None


def _ids(id_list) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in _kids(id_list, "ArticleId"):
        v = (a.text or "").strip()
        k = (a.get("IdType") or "").lower()
        if v and k and k not in out:
            out[k] = v
    return out


def own_article_ids(art) -> dict[str, str]:
    return _ids(_path(art, "PubmedData", "ArticleIdList"))


def normalize_orcid(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().upper().rstrip("/")
    s = s.rsplit("/", 1)[-1]
    m = _ORCID.search(s)
    if not m or len(s.replace("-", "")) != 16:
        return None
    return "-".join(m.groups())


def retracted(pub_types: list[str], comments: list[dict[str, Any]]) -> bool:
    if "Retracted Publication" in pub_types:
        return True
    return any(c.get("ref_type") == "RetractionIn" for c in comments)


def _mesh(medline, pmid, sf):
    rows = []
    for mh in _kids(_kid(medline, "MeshHeadingList"), "MeshHeading"):
        d = _kid(mh, "DescriptorName")
        if d is None:
            continue
        quals = _kids(mh, "QualifierName")
        major = d.get("MajorTopicYN") == "Y" or any(q.get("MajorTopicYN") == "Y" for q in quals)
        rows.append(
            {
                "pmid": pmid,
                "descriptor_ui": d.get("UI"),
                "descriptor_name": cap_text(_text(d)),
                "major_topic": major,
                "qualifier_uis": [q.get("UI") for q in quals if q.get("UI")],
                "qualifiers": cap_list_elements([_text(q) for q in quals if _text(q)]),
                "source_file": sf,
            }
        )
    return rows


def _references(ref_list):
    for c in ref_list if ref_list is not None else []:
        n = _local(c.tag)
        if n == "Reference":
            yield c
        elif n == "ReferenceList":
            yield from _references(c)


def _cites(art, pmid, sf):
    rows = []
    for ref in _references(_path(art, "PubmedData", "ReferenceList")):
        ids = _ids(_kid(ref, "ArticleIdList"))
        rows.append(
            {
                "src_pmid": pmid,
                "dst_pmid": ids.get("pubmed"),
                "dst_doi": ids.get("doi"),
                "ref_text": cap_text(_text(_kid(ref, "Citation"))),
                "source_file": sf,
            }
        )
    return rows


def _comments(medline, pmid, sf):
    rows = []
    for cc in _kids(_kid(medline, "CommentsCorrectionsList"), "CommentsCorrections"):
        rows.append(
            {
                "pmid": pmid,
                "ref_type": cc.get("RefType"),
                "ref_pmid": _text(_kid(cc, "PMID")),
                "ref_text": cap_text(_text(_kid(cc, "RefSource"))),
                "source_file": sf,
            }
        )
    return rows


def _chemicals(medline, pmid, sf):
    rows = []
    for ch in _kids(_kid(medline, "ChemicalList"), "Chemical"):
        name = _kid(ch, "NameOfSubstance")
        reg = _text(_kid(ch, "RegistryNumber"))
        rows.append(
            {
                "pmid": pmid,
                "substance_ui": None if name is None else name.get("UI"),
                "name": cap_text(_text(name)),
                "registry_number": None if reg in (None, "0") else reg,
                "source_file": sf,
            }
        )
    return rows


def _keywords(medline, pmid, sf):
    rows = []
    for kl in _kids(medline, "KeywordList"):
        owner = kl.get("Owner")
        for k in _kids(kl, "Keyword"):
            text = _text(k)
            if text:
                rows.append(
                    {
                        "pmid": pmid,
                        "keyword": cap_text(text),
                        "owner": owner,
                        "major_topic": k.get("MajorTopicYN") == "Y",
                        "source_file": sf,
                    }
                )
    return rows


def _authors(medline, pmid, sf, counters):
    rows = []
    authors = _kids(_path(medline, "Article", "AuthorList"), "Author")
    for pos, a in enumerate(authors, start=1):
        orcid = None
        for ident in _kids(a, "Identifier"):
            if (ident.get("Source") or "").upper() == "ORCID":
                orcid = normalize_orcid(ident.text)
                if orcid is None:
                    counters["orcid_dropped"] += 1
                break
        affs = [_text(_kid(ai, "Affiliation")) for ai in _kids(a, "AffiliationInfo")]
        rows.append(
            {
                "pmid": pmid,
                "position": pos,
                "last_name": cap_text(_text(_kid(a, "LastName"))),
                "fore_name": cap_text(_text(_kid(a, "ForeName"))),
                "initials": _text(_kid(a, "Initials")),
                "collective_name": cap_text(_text(_kid(a, "CollectiveName"))),
                "orcid": orcid,
                "affiliations": cap_list_elements([x for x in affs if x]) or None,
                "source_file": sf,
            }
        )
    return rows


def extract_relations(art, pmid: str, source_file: str):
    counters: Counter[str] = Counter({"orcid_dropped": 0, "edges_skipped_no_pmid": 0})
    edges: dict[str, list[dict[str, Any]]] = {r: [] for r in RELATIONS}
    if not pmid:
        counters["edges_skipped_no_pmid"] += 1
        return edges, dict(counters)
    medline = _kid(art, "MedlineCitation")
    edges["mesh"] = _mesh(medline, pmid, source_file)
    edges["cites"] = _cites(art, pmid, source_file)
    edges["comments"] = _comments(medline, pmid, source_file)
    edges["chemicals"] = _chemicals(medline, pmid, source_file)
    edges["keywords"] = _keywords(medline, pmid, source_file)
    edges["authors"] = _authors(medline, pmid, source_file, counters)
    return edges, dict(counters)
