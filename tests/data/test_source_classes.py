"""Every source is a training source or a reference source, never both, never neither."""

from __future__ import annotations

from episteme.config import get_settings
from episteme.data import article_schema as schema
from episteme.data.regulatory.faers import load as fl


def test_every_source_is_classified_once():
    training, reference = set(schema.TRAINING_SOURCES), set(schema.REFERENCE_SOURCES)
    assert not training & reference
    assert training | reference == set(schema.SOURCES)
    assert len(schema.TRAINING_SOURCES) == len(training)
    assert len(schema.REFERENCE_SOURCES) == len(reference)


def test_reference_sources_are_excluded_from_the_corpus():
    assert set(schema.REFERENCE_SOURCES) <= set(get_settings().corpus_excluded_sources)


def test_faers_signal_refusal_names_force():
    msg = fl.signal_refusal_message(["the bridge matched no drug entry"], "g1234567890")
    assert "--force --reason" in msg
    assert "g1234567890" in msg
    assert "--allow-partial --reason" in msg
