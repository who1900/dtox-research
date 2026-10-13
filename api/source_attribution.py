"""Validation and resolution helpers for citable source URLs."""
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit


def valid_source_url(value):
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(ord(char) < 32 or char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        parsed.port
    except (ValueError, UnicodeError):
        return None
    return value


def stored_source_url(metadata):
    metadata = metadata or {}
    for key in ("source_url", "link", "pdf_url", "landing_page_url", "url"):
        value = valid_source_url(metadata.get(key))
        if value:
            return value
    return None


def normalize_title(value):
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


@lru_cache(maxsize=1)
def _catalog():
    path = Path(__file__).with_name("whitepaper_sources.json")
    try:
        with path.open(encoding="utf-8") as handle:
            entries = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(entries, list):
        return {}
    if any(not isinstance(entry, dict)
           or not isinstance(entry.get("id"), str)
           or not entry["id"].startswith("wp:")
           or not isinstance(entry.get("title"), str)
           or (entry.get("url") is not None and not isinstance(entry.get("url"), str))
           for entry in entries):
        return {}
    return {entry["id"]: entry for entry in entries}


def curated_source(paper_id, title):
    pid = str(paper_id or "")
    if not pid.startswith("wp:"):
        return None
    entry = _catalog().get(pid)
    if not entry or normalize_title(title) != normalize_title(entry.get("title")):
        return None
    url = valid_source_url(entry.get("url"))
    if not url:
        return None
    return {"url": url, "origin": "curated_historical_list",
            "source_url_status": entry.get("source_url_status", "syntax_only_source_not_verified"),
            "legacy_id_alias": entry.get("legacy_id_alias")}
