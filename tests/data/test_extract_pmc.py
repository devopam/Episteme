from pathlib import Path

from episteme.data.pmc.extract_pmc import extract_pmc

FIX = Path(__file__).parent / "fixtures" / "pmc"


def test_extract_pmc_produces_one_ok_row(tmp_path):
    processed = tmp_path / "02_processed"
    res = extract_pmc(FIX, processed, max_files=1)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] >= 1
    # success marker written
    marks = list((processed / "_ops" / "pmc" / "success").glob("*.ok"))
    assert marks
    # staging shard written
    shards = list((processed / "staging" / "pmc").glob("*"))
    assert shards


def test_extract_pmc_row_fields(tmp_path):
    import json

    processed = tmp_path / "02_processed"
    extract_pmc(FIX, processed, max_files=1)
    shard = next((processed / "staging" / "pmc").glob("*"))
    # read the shard back (parquet or jsonl)
    if shard.suffix == ".parquet":
        import pyarrow.parquet as pq

        rows = pq.read_table(shard).to_pylist()
    else:
        rows = [json.loads(line) for line in shard.read_text().splitlines()]
    r = rows[0]
    assert r["source"] == "pmc"
    assert r["pmcid"] in ("PMC13525906", "PMCFIX0001") or r["pmcid"].startswith("PMC")
    assert "acetylcholinesterase" in (r["text"] or "").lower()
    assert r["license"] == "CC BY"
    assert r["subset"] == "commercial"
    assert r["extract_status"] == "ok"
