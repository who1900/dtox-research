"""Derive paper-profile labels from headings proven against the same source."""

from pipeline import extractor


_TEXT_ID_FIELDS = ("chunk_index", "text", "element_type", "has_math", "has_algorithm")


def _sections(source_text, document_kind):
    result = []
    extractor.build_chunks_markdown(source_text, section_sink=result, document_kind=document_kind)
    return result


def _same_text_identity(cache, parsed):
    if len(cache) != len(parsed):
        return False
    for old, new in zip(cache, parsed):
        if not isinstance(old, dict) or any(old.get(field) != new.get(field) for field in _TEXT_ID_FIELDS):
            return False
    return True


def candidate(cache, source_text):
    """Return source-grounded labels while preserving every cached text/ID field.

    Section text from the legacy spec parser must exactly match section text
    from the paper parser in source order. Cached headings must then match a
    legacy source heading (raw or cleaned) with one resulting paper label pair.
    """
    base = {"source_heading_verified": False, "exact_text_ids": False,
            "full_label_map": [], "changes": [], "status": "unverified"}
    if not isinstance(cache, list) or not cache or any(not isinstance(chunk, dict) for chunk in cache):
        base["status"] = "invalid_cache"
        return base
    indices = [chunk.get("chunk_index") for chunk in cache]
    if any(type(index) is not int or index < 0 for index in indices) or len(set(indices)) != len(indices):
        base["status"] = "invalid_cache_indices"
        return base
    try:
        old_sections = _sections(source_text, "spec")
        paper_sections = _sections(source_text, "paper")
        paper_chunks, _ = extractor.build_chunks_markdown(source_text, document_kind="paper")
    except Exception as exc:
        base.update(status="parse_error", error=type(exc).__name__)
        return base
    if (len(old_sections) != len(paper_sections)
            or any(old["text"] != new["text"] for old, new in zip(old_sections, paper_sections))):
        base["status"] = "source_section_text_mismatch"
        return base

    exact_text_ids = _same_text_identity(cache, paper_chunks)
    if exact_text_ids:
        full_labels, changes = [], []
        for chunk, parsed in zip(cache, paper_chunks):
            item = {"chunk_index": chunk["chunk_index"],
                    "section_title": parsed["section_title"],
                    "section_type": parsed["section_type"]}
            full_labels.append(item)
            if (chunk.get("section_title", "") != item["section_title"]
                    or chunk.get("section_type", "other") != item["section_type"]):
                changes.append(item.copy())
        base.update(source_heading_verified=True, exact_text_ids=True,
                    full_label_map=full_labels, changes=changes,
                    status="candidate" if changes else "no_label_changes")
        return base

    label_pairs_by_old_heading = {}
    for old, new in zip(old_sections, paper_sections):
        key = old["section_title"]
        label_pairs_by_old_heading.setdefault(key, set()).add(
            (new["section_title"], new["section_type"]))

    exact_paper_labels = {}
    for section in paper_sections:
        exact_paper_labels.setdefault(section["section_title"], set()).add(
            (section["section_title"], section["section_type"]))

    full_labels, changes = [], []
    for chunk in cache:
        cached_title = chunk.get("section_title", "")
        # Prefer the canonical paper-profile heading when the cached title is
        # an exact source-derived title. The legacy spec-profile bridge can
        # collapse distinct headings after normalization, so it is only a
        # fallback for older cache titles that do not equal a paper title.
        matches = set(exact_paper_labels.get(cached_title, ()))
        if not matches:
            cleaned_cache_title = extractor._clean_scientific_heading(cached_title)
            for old_title, pairs in label_pairs_by_old_heading.items():
                if (cached_title == old_title
                        or cleaned_cache_title == extractor._clean_scientific_heading(old_title)):
                    matches.update(pairs)
        if len(matches) != 1:
            base["status"] = "unmatched_or_ambiguous_cached_heading"
            return base
        title, section_type = next(iter(matches))
        item = {"chunk_index": chunk.get("chunk_index"), "section_title": title,
                "section_type": section_type}
        full_labels.append(item)
        if (chunk.get("section_title", "") != title
                or chunk.get("section_type", "other") != section_type):
            changes.append(item.copy())

    base.update(source_heading_verified=True, exact_text_ids=False,
                full_label_map=full_labels, changes=changes,
                status="candidate" if changes else "no_label_changes")
    return base
