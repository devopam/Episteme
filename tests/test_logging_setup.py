import json
import logging

from episteme.logging_setup import configure_logging


def test_configure_logging_plain(capsys):
    configure_logging(level="INFO", json_format=False)
    logging.getLogger("episteme.test").info("hello")
    err = capsys.readouterr().err
    assert "hello" in err
    assert "INFO" in err


def test_configure_logging_json(capsys):
    configure_logging(level="DEBUG", json_format=True)
    logging.getLogger("episteme.test").warning("structured", extra={"source": "pubmed"})
    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "structured"
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "episteme.test"
