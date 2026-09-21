import re
from pathlib import Path

WRAPPER = (
    Path(__file__).resolve().parents[1] / "scripts" / "data" / "uniprot" / "download_uniprot.sh"
)


def _files() -> list[str]:
    text = WRAPPER.read_text(encoding="utf-8")
    m = re.search(r"^files=\((.*?)\)\s*$", text, re.M)
    assert m, "files=(…) array not found"
    return m.group(1).split()


def test_fasta_precedes_the_941mb_xml_and_is_reachable_with_a_small_cap():
    files = _files()
    assert files.index("uniprot_sprot.fasta.gz") < files.index("uniprot_sprot.xml.gz")
    assert files.index("uniprot_sprot.fasta.gz") <= 3  # reachable with --max-files 4
