import os
import sys
import tempfile
import pandas as pd
import pytest
from episteme.data.curate.serialize_structured_sources import (
    serialize_uniprot,
    process_chembl_csv,
    filter_and_process_openmedtext,
)
from episteme.data.curate.deduplicate_corpus import get_shingles
from episteme.data.curate.decontaminate_benchmarks import normalize_text, get_ngrams


def test_serialize_uniprot():
    """Verify UniProt fasta header parsing and prose serialization."""
    header = ">sp|P68871|HBB_HUMAN Hemoglobin subunit beta OS=Homo sapiens OX=9606 GN=HBB PE=1 SV=2"
    seq = "VHLTPEEKSAV"
    res = serialize_uniprot(header, seq)
    
    assert res["id"] == "uniprot_P68871"
    assert "Hemoglobin subunit beta" in res["text"]
    assert "Homo sapiens" in res["text"]
    assert "HBB" in res["text"]
    assert "VHLTPEEKSAV" in res["text"]


def test_process_chembl_csv():
    """Verify ChEMBL compound serialization mapping smiles and targets."""
    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
        f.write("chembl_id,canonical_smiles,standard_type,standard_value,standard_units,target_pref_name\n")
        f.write("CHEMBL25,CCN(CC)CCO,IC50,5.4,nM,Acetylcholinesterase\n")
        csv_path = f.name
        
    try:
        res = process_chembl_csv(csv_path)
        assert len(res) == 1
        assert res[0]["id"] == "chembl_CHEMBL25"
        assert "CCN(CC)CCO" in res[0]["text"]
        assert "Acetylcholinesterase" in res[0]["text"]
        assert "5.4 nM" in res[0]["text"]
    finally:
        os.remove(csv_path)


def test_filter_openmedtext_license():
    """Verify that Non-Commercial licensed directories are skipped."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create an open subfolder
        open_dir = os.path.join(temp_dir, "cc_by")
        os.makedirs(open_dir)
        with open(os.path.join(open_dir, "doc1.txt"), "w") as f:
            f.write("This is open medical text.")
            
        # Create a non-commercial subfolder
        nc_dir = os.path.join(temp_dir, "cc_by_nc")
        os.makedirs(nc_dir)
        with open(os.path.join(nc_dir, "doc2.txt"), "w") as f:
            f.write("This is restricted text.")
            
        res = filter_and_process_openmedtext(temp_dir)
        
        # Verify doc1 is loaded and doc2 is skipped
        assert len(res) == 1
        assert res[0]["id"] == "openmedtext_doc1.txt"
        assert "open medical text" in res[0]["text"]


def test_shingle_generation():
    """Verify word shingle generation logic for deduplication."""
    text = "Acute myocarditis symptoms include chest pain."
    shingles = get_shingles(text, n=2)
    
    # 2-grams should be:
    # "acute myocarditis", "myocarditis symptoms", "symptoms include", "include chest", "chest pain"
    assert "acute myocarditis" in shingles
    assert "chest pain" in shingles
    assert len(shingles) == 5


def test_decontamination_utilities():
    """Verify decontamination text normalization and n-gram extraction."""
    text = "Severe hyperkalemia, clinical sign!"
    words = normalize_text(text)
    assert words == ["severe", "hyperkalemia", "clinical", "sign"]
    
    ngrams = get_ngrams(words, n=3)
    assert len(ngrams) == 2
    assert ("severe", "hyperkalemia", "clinical") in ngrams
