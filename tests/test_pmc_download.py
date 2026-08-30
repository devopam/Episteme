import os
import sys
from unittest.mock import patch, MagicMock
import tempfile
import pytest

# Add scripts directory to path to import download_pmc_oa_comm
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
from download_pmc_oa_comm import query_esearch, get_metadata, download_pmc_commercial


def test_query_esearch():
    """Test NCBI ESearch query function with a small mock/limit."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "esearchresult": {
            "idlist": ["12345", "67890"]
        }
    }
    
    with patch("requests.get", return_value=mock_response):
        res = query_esearch("test_query", limit=2)
        assert res == ["12345", "67890"]


def test_get_metadata():
    """Test S3 metadata fetching with version fallback."""
    mock_res_404 = MagicMock()
    mock_res_404.status_code = 404
    
    mock_res_200 = MagicMock()
    mock_res_200.status_code = 200
    mock_res_200.json.return_value = {"pmcid": "PMC12345", "version": 2}
    
    # We mock requests.get to return 404 on first call (v1) and 200 on second (v2)
    with patch("requests.get", side_effect=[mock_res_404, mock_res_200]):
        meta, version_id = get_metadata("12345")
        assert meta == {"pmcid": "PMC12345", "version": 2}
        assert version_id == "PMC12345.2"


def test_download_pmc_commercial_dry_run():
    """Test full downloader run in dry_run mode."""
    with tempfile.TemporaryDirectory() as temp_dir:
        with patch("download_pmc_oa_comm.query_esearch", return_value=["12345"]):
            mock_meta = {
                "pmcid": "PMC12345",
                "version": 1,
                "xml_url": "s3://pmc-oa-opendata/PMC12345.1/PMC12345.1.xml?md5=abc",
                "text_url": "s3://pmc-oa-opendata/PMC12345.1/PMC12345.1.txt?md5=def"
            }
            with patch("download_pmc_oa_comm.get_metadata", return_value=(mock_meta, "PMC12345.1")):
                # Run download in dry run mode
                count = download_pmc_commercial(
                    output_dir=temp_dir,
                    formats=["xml", "txt"],
                    limit=1,
                    threads=1,
                    dry_run=True
                )
                assert count == 1
