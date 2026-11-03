"""Regulatory reference sources (EMA feeds, openFDA reference datasets).

One config file, ``feeds.toml``, holds one entry per dataset. A dataset of a known
format needs a config entry and a ``sources.env`` key; a new format needs one parser
in ``parsers.py``.
"""
