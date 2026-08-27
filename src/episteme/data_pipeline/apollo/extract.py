#!/usr/bin/env python3
"""Extract ApolloCorpus pretrain text into Episteme JSONL schema."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterator

PRETRAIN_SUBSETS = {
    "medicalBook",
    "medicalGuideline",
    "medicalPaper",
    "medicalWeb",
    "medicalWiki",
}

TEXT_FILE_RE = re.compile(
    r"^(?P<subset>medicalBook|medicalGuideline|medicalPaper|medicalWeb|medicalWiki)"
    r"_(?P<lang>[a-z]{2})_text\.json$",
    re.IGNORECASE,
)

LICENSE = "Apache-2.0"
SOURCE = "apollo"


def find_text_files(root: Path) -> list[Path]:
    files = []
    for p in root.rglob("*_text.json"):
        if TEXT_FILE_RE.match(p.name):
            files.append(p)
    return sorted(files)


def parse_filename(path: Path) -> tuple[str, str]:
    m = TEXT_FILE_RE.match(path.name)
    if not m:
        raise ValueError(f"Unexpected filename: {path.name}")
    return m.group("subset"), m.group("lang").lower()


def load_json_list(path: Path) -> list[Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list in {path}")
    return data


def normalise_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def iter_records(
    path: Path,
    subset: str,
    language: str,
    min_chars: int = 50,
) -> Iterator[dict]:
    items = load_json_list(path)
    for idx, item in enumerate(items):
        if not isinstance(item, str):
            continue
        text = normalise_text(item)
        if len(text) < min_chars:
            continue
        yield {
            "id": f"apollo_{subset}_{language}_{idx:08d}",
            "source": SOURCE,
            "language": language,
            "text": text,
            "title": None,
            "license": LICENSE,
            "metadata": {
                "apollo_subset": subset,
                "original_file": path.name,
                "original_index": idx,
            },
        }


def run(input_dir: Path, output_jsonl: Path, min_chars: int = 50, subsets: set[str] | None = None) -> None:
    allowed = subsets or PRETRAIN_SUBSETS
    if not input_dir.is_dir():
        raise SystemExit(f"Input directory not found: {input_dir}")

    files = find_text_files(input_dir)
    if not files:
        raise SystemExit(
            f"No matching *_text.json files under {input_dir}. "
            "Did you unzip ApolloCorpus.zip?"
        )

    output_jsonl.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    by_lang: dict[str, int] = {}
    by_subset: dict[str, int] = {}

    with output_jsonl.open("w", encoding="utf-8") as out:
        for path in files:
            subset, language = parse_filename(path)
            if subset not in allowed:
                continue
            print(f"→ {path}  [{subset} / {language}]")
            n = 0
            for rec in iter_records(path, subset, language, min_chars=min_chars):
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
            total += n
            by_lang[language] = by_lang.get(language, 0) + n
            by_subset[subset] = by_subset.get(subset, 0) + n
            print(f"   wrote {n:,} records")

    print(f"\nTotal records: {total:,}")
    print(f"Output: {output_jsonl}")
    print("By language:", dict(sorted(by_lang.items())))
    print("By subset:", dict(sorted(by_subset.items())))


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract ApolloCorpus → JSONL")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--min-chars", type=int, default=50)
    parser.add_argument("--subsets", nargs="*", default=None)
    args = parser.parse_args()

    subsets = set(args.subsets) if args.subsets else None
    run(args.input_dir, args.output_jsonl, min_chars=args.min_chars, subsets=subsets)


if __name__ == "__main__":
    main()
