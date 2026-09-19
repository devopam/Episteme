import importlib
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "sp4"

SOURCES = [
    "chembl",
    "uniprot",
    "pubchem",
    "clinvar",
    "reactome",
    "mesh",
    "ontologies",
    "openalex",
]


@pytest.mark.parametrize("source", SOURCES)
def test_same_named_inputs_in_two_directories_are_both_processed(source, tmp_path):
    mod = importlib.import_module(f"episteme.data.{source}.serialize_{source}")
    fn = getattr(mod, f"serialize_{source}")
    raw = tmp_path / "raw"
    for d in ("a", "b"):
        shutil.copytree(FIX / source, raw / d)
    res = fn(raw, tmp_path / "processed")
    assert res["inputs"] == 2, res
    assert res["ok"] == 2 and res["failed"] == 0, res
    success = tmp_path / "processed" / "_ops" / source / "success"
    markers = sorted(p.name for p in success.glob("*.ok"))
    assert len(markers) == 2 and markers[0] != markers[1], markers
    assert all(m.startswith(("a__", "b__")) for m in markers), markers
