"""Prepare bounded, read-only prose repair plans without modifying old chunks."""

import argparse
import contextlib
import difflib
import hashlib
import json
import re
import signal
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import extractor
from pipeline import document_cache


MAX_NEW_CHUNKS = 20
MIN_MISSING_CHARS = 120
MIN_MISSING_WORDS = 20
MIN_ANCHOR_WORDS = 8
MAX_SECTION_WORDS = 12000
_HEADING_LEVELS = {"section": 1, "subsection": 2, "subsubsection": 3,
                   "paragraph": 4, "subparagraph": 5}
_STRUCTURE_RE = re.compile(
    r"\\(?P<heading>subsubsection|subsection|section|subparagraph|paragraph)(?![A-Za-z@])|"
    r"\\(?P<reference>bibliographystyle|bibliography)(?![A-Za-z@])|"
    r"\\(?P<appendix>appendix)(?![A-Za-z@])|"
    r"\\end\s*\{\s*document\s*\}|"
    r"\\begin\s*\{\s*thebibliography\s*\}|"
    r"\\(?P<unsupported>chapter|part|printbibliography|backmatter|frontmatter)(?![A-Za-z@])"
)
_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)
_METADATA_MACRO_RE = re.compile(
    r"\\(?:label|(?:page|eq|auto)?ref|cite[a-z]*)(?![a-z@])"
    r"(?:\s*\[[^\[\]]*\]){0,2}\s*\{[^{}]*\}"
)
_MACRO_RE = re.compile(r"\\[a-z@]+\*?")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _text_key(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().casefold()


def _is_preserved_structured(chunk):
    return chunk.get("element_type") in {"equation", "algorithm", "code"} or chunk.get("has_algorithm") is True


def _is_unescaped(text, offset):
    cursor = offset - 1
    while cursor >= 0 and text[cursor] == "\\":
        cursor -= 1
    return (offset - cursor - 1) % 2 == 0


def _source_matches_outside_literals(text, pattern):
    spans = extractor._literal_spans(text)
    return [match for match in pattern.finditer(text)
            if _is_unescaped(text, match.start())
            and not any(span.start() <= match.start() < span.end() for span in spans)]


def _group_end(text, start, opening, closing):
    if start >= len(text) or text[start] != opening:
        return None
    depth = 0
    for index in range(start, len(text)):
        if not _is_unescaped(text, index):
            continue
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _heading_argument(text, match):
    cursor = match.end()
    if cursor < len(text) and text[cursor] == "*":
        cursor += 1
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if cursor < len(text) and text[cursor] == "[":
        cursor = _group_end(text, cursor, "[", "]")
        if cursor is None:
            return None, match.end(), "unclosed optional heading argument"
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    end = _group_end(text, cursor, "{", "}")
    if end is None:
        return None, cursor, "unsupported or unclosed heading argument"
    title = text[cursor + 1:end - 1].strip()
    if not title or len(title) > 512 or any(char in title for char in "\\{}"):
        return None, end, "unsupported complex or empty section title"
    return title, end, None


def _source_sections(source):
    text = extractor.strip_comments(source)
    events = []
    skip_until = 0
    region = "main"
    brace_depth = 0
    depth_cursor = 0
    literals = extractor._literal_spans(text)
    literal_index = 0
    for match in _source_matches_outside_literals(text, _STRUCTURE_RE):
        while depth_cursor < match.start():
            while literal_index < len(literals) and depth_cursor >= literals[literal_index].end():
                literal_index += 1
            if literal_index < len(literals) and literals[literal_index].start() <= depth_cursor:
                depth_cursor = literals[literal_index].end()
                continue
            if text[depth_cursor] in "{}" and _is_unescaped(text, depth_cursor):
                brace_depth += 1 if text[depth_cursor] == "{" else -1
            depth_cursor += 1
        if match.start() < skip_until:
            continue
        if brace_depth != 0:
            events.append({"kind": "unsupported", "start": match.start(),
                           "error": "structural command inside an unsupported brace group",
                           "token": match.group(0)})
            continue
        if match.group("heading"):
            title, end, error = _heading_argument(text, match)
            skip_until = end
            key = _text_key(title) if title else None
            if key in {"references", "bibliography"}:
                region = "reference"
            elif title and re.search(r"\b(?:appendix|appendices|supplementary)\b", key):
                region = "appendix"
            events.append({"kind": "unsupported" if error else "heading",
                           "start": match.start(), "body_start": end,
                           "title": title, "key": key, "region": region,
                           "level": _HEADING_LEVELS[match.group("heading")],
                           "error": error, "token": match.group(0)})
        elif match.group("appendix"):
            region = "appendix"
            events.append({"kind": "appendix", "start": match.start(), "token": match.group(0)})
        elif match.group("reference") or match.group(0).startswith("\\begin"):
            region = "reference"
            events.append({"kind": "reference", "start": match.start(), "token": match.group(0)})
        elif match.group("unsupported"):
            events.append({"kind": "unsupported", "start": match.start(),
                           "error": "unsupported structural boundary", "token": match.group(0)})
        else:
            events.append({"kind": "end", "start": match.start(), "token": match.group(0)})
            break

    sections = []
    for index, event in enumerate(events):
        if event["kind"] != "heading":
            continue
        following = events[index + 1] if index + 1 < len(events) else None
        error = None
        if following is None:
            error = "no supported source boundary"
        elif following["kind"] == "unsupported":
            error = following["error"]
        end = following["start"] if following else len(text)
        body = text[event["body_start"]:end].strip()
        sections.append({**event, "body": body, "boundary_error": error,
                         "bounded_source": "\\section{" + event["title"] + "}\n" + body,
                         "source_boundary": {
                             "start_offset": event["start"], "end_offset": end,
                             "end_token": following["token"] if following else None,
                             "body_sha256": _sha256(body.encode("utf-8")),
                             "body_char_count": len(body)}})
    return sections, [event["error"] for event in events if event["kind"] == "unsupported"]


def _bounded_discussions_source(source, section):
    sections, _errors = _source_sections(source)
    matches = [item for item in sections if item["key"] == _text_key(section)]
    if len(matches) != 1:
        return None, f"expected one unambiguous section header, found {len(matches)}", None
    item = matches[0]
    if item["region"] == "reference" or item["boundary_error"]:
        return None, item["boundary_error"] or "reference section is excluded", None
    return item["bounded_source"], None, item["source_boundary"]


def _normalized_with_offsets(text):
    output, offsets = [], []
    pending_space = None
    cursor = 0
    while cursor < len(text):
        end = cursor + 1
        while end < len(text) and unicodedata.combining(text[end]):
            end += 1
        for character in unicodedata.normalize("NFKC", text[cursor:end]).casefold():
            if character.isspace():
                if output:
                    pending_space = (pending_space[0] if pending_space else cursor, end)
            else:
                if pending_space:
                    output.append(" ")
                    offsets.append(pending_space)
                    pending_space = None
                output.append(character)
                offsets.append((cursor, end))
        cursor = end
    normalized = "".join(output)
    if normalized != _text_key(text):
        raise ValueError("unsupported Unicode normalization mapping")
    return normalized, offsets


def _word_stream(text):
    normalized, offsets = _normalized_with_offsets(text)
    masked = list(normalized)
    for pattern in (_METADATA_MACRO_RE, _MACRO_RE):
        for match in pattern.finditer(normalized):
            masked[match.start():match.end()] = " " * (match.end() - match.start())
    matches = list(_WORD_RE.finditer("".join(masked)))
    return [match.group(0) for match in matches], [
        (offsets[match.start()][0], offsets[match.end() - 1][1]) for match in matches
    ]


def _overlap_size(left, right):
    values = list(right) + [None] + list(left[-len(right):])
    prefix = [0] * len(values)
    for index in range(1, len(values)):
        size = prefix[index - 1]
        while size and values[index] != values[size]:
            size = prefix[size - 1]
        if values[index] == values[size]:
            size += 1
        prefix[index] = size
    return prefix[-1] if prefix else 0


def _prose_units(chunks):
    units = []
    previous = None
    for chunk in chunks:
        if chunk.get("element_type", "prose") != "prose" or _is_preserved_structured(chunk):
            previous = None
            continue
        if not isinstance(chunk.get("text"), str):
            raise ValueError("prose text must be a string")
        text = chunk["text"].strip()
        if not text:
            continue
        continuation = (
            previous is not None and isinstance(chunk.get("part_index"), int)
            and chunk.get("part_index", 0) > 0
            and previous.get("part_index", -2) + 1 == chunk["part_index"]
            and chunk.get("part_total") == previous.get("part_total")
        )
        if continuation:
            overlap = _overlap_size(units[-1]["text"], text)
            units[-1]["text"] += text[overlap:] if overlap else "\n\n" + text
        else:
            units.append({"text": text, "template": dict(chunk)})
        previous = chunk
    return units


def _contains_words(haystack, needle):
    return bool(needle) and any(haystack[index:index + len(needle)] == needle
                                for index in range(len(haystack) - len(needle) + 1))


def _substantive_overlap(left, right):
    size = MIN_MISSING_WORDS
    windows = {tuple(left[index:index + size]) for index in range(len(left) - size + 1)
               if sum(map(len, left[index:index + size])) + size - 1 >= MIN_MISSING_CHARS}
    return any(tuple(right[index:index + size]) in windows
               for index in range(len(right) - size + 1))


def _macro_argument_spans(text):
    spans = []
    for match in re.finditer(r"\\[A-Za-z@]+\*?", text):
        if not _is_unescaped(text, match.start()):
            continue
        cursor = match.end()
        for _argument in range(4):
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor >= len(text) or text[cursor] not in "[{":
                break
            opening = text[cursor]
            end = _group_end(text, cursor, opening, "]" if opening == "[" else "}")
            if end is None:
                break
            spans.append((match.start(), end))
            cursor = end
    return spans


def _missing_prose(item, parsed, old_chunks, section_type):
    source_units = _prose_units(parsed)
    cache_chunks = sorted([chunk for chunk in old_chunks
                           if _text_key(str(chunk.get("section_title", ""))) == item["key"]
                           and chunk.get("element_type", "prose") == "prose"
                           and not _is_preserved_structured(chunk)],
                          key=lambda chunk: chunk["chunk_index"])
    old_units = _prose_units(cache_chunks)
    raw_source = "\n\n".join(unit["text"] for unit in source_units)
    raw_old = "\n\n".join(unit["text"] for unit in old_units)
    source_words, word_offsets = _word_stream(raw_source)
    old_words, _old_offsets = _word_stream(raw_old)
    if max(len(source_words), len(old_words)) > MAX_SECTION_WORDS:
        raise ValueError(f"section word count exceeds bounded coverage limit {MAX_SECTION_WORDS}")

    report = {"section_title": item["title"], "section_type": section_type,
              "source_boundary": item["source_boundary"], "state": "no_change",
              "source_words": len(source_words), "cached_words": len(old_words),
              "ignored_representation_spans": 0}
    if not source_words:
        if old_words:
            raise ValueError("cached prose exists but bounded parser produced no prose")
        return [], report

    for chunk in old_chunks:
        if _text_key(str(chunk.get("section_title", ""))) == item["key"]:
            continue
        if (chunk.get("element_type", "prose") != "prose" or _is_preserved_structured(chunk)
                or not isinstance(chunk.get("text"), str)):
            continue
        other_words, _offsets = _word_stream(chunk["text"])
        if _substantive_overlap(source_words, other_words):
            raise ValueError("source prose is retained under different cache section metadata")

    matcher = difflib.SequenceMatcher(None, old_words, source_words, autojunk=True)
    opcodes = matcher.get_opcodes()
    macro_spans = _macro_argument_spans(raw_source) if old_words else []
    ranges = []
    for index, (tag, old_start, old_end, start, end) in enumerate(opcodes):
        if tag == "equal":
            continue
        if start < end:
            raw_start, raw_end = word_offsets[start][0], word_offsets[end - 1][1]
            substantive = end - start >= MIN_MISSING_WORDS and raw_end - raw_start >= MIN_MISSING_CHARS
        else:
            raw_start = raw_end = 0
            substantive = old_end - old_start >= MIN_MISSING_WORDS
        if not substantive:
            report["ignored_representation_spans"] += 1
            continue
        if tag != "insert":
            raise ValueError("substantive cache/source substitution or deletion has no additive loss proof")
        if any(left <= raw_start and raw_end <= right for left, right in macro_spans):
            report["ignored_representation_spans"] += 1
            continue
        if old_words:
            before = opcodes[index - 1] if index else None
            after = opcodes[index + 1] if index + 1 < len(opcodes) else None
            left_anchor = before is not None and before[0] == "equal" and before[4] - before[3] >= MIN_ANCHOR_WORDS
            right_anchor = after is not None and after[0] == "equal" and after[4] - after[3] >= MIN_ANCHOR_WORDS
            if (start > 0 and not left_anchor) or (end < len(source_words) and not right_anchor):
                raise ValueError("missing prose span lacks contiguous retained-word anchors")
        while raw_end < len(raw_source) and raw_source[raw_end] in ".,;:!?)]":
            raw_end += 1
        ranges.append((raw_start, raw_end))

    additions = []
    unit_start = 0
    for unit in source_units:
        unit_end = unit_start + len(unit["text"])
        for start, end in ranges:
            left, right = max(start, unit_start), min(end, unit_end)
            if left >= right:
                continue
            fragment = unit["text"] if not old_words else raw_source[left:right].strip()
            words, _offsets = _word_stream(fragment)
            if len(words) < MIN_MISSING_WORDS or len(fragment) < MIN_MISSING_CHARS:
                report["ignored_representation_spans"] += 1
                continue
            for old in old_chunks:
                if isinstance(old.get("text"), str):
                    retained, _offsets = _word_stream(old["text"])
                    if _contains_words(retained, words) or _substantive_overlap(retained, words):
                        raise ValueError("proposed prose is already retained in a cache chunk")
            pieces = extractor.split_long_text(fragment, overlap_ratio=0)
            for part_index, piece in enumerate(pieces):
                addition = dict(unit["template"])
                addition.update(text=piece, section_title=item["title"], section_type=section_type,
                                element_type="prose", has_algorithm=False,
                                has_math=bool(extractor.MATH_INLINE_RE.search(piece)),
                                part_index=part_index, part_total=len(pieces))
                additions.append(addition)
        unit_start = unit_end + 2
    report["state"] = ("partial" if old_words else "missing") if additions else "no_change"
    report["addition_count"] = len(additions)
    return additions, report


def build_plan(paper_id, source, old_chunks, section=None, max_new_chunks=20,
               source_sha256=None, cache_sha256=None, source_bytes=None, cache_bytes=None):
    blockers = []
    unresolved = []
    if not isinstance(paper_id, str) or not paper_id.strip():
        blockers.append("paper_id must be a non-empty string")
    if not isinstance(source, str) or not source.strip():
        blockers.append("source must be a non-empty string")
    if not isinstance(old_chunks, list) or not all(isinstance(chunk, dict) for chunk in old_chunks):
        blockers.append("chunk cache must be a JSON list of chunk objects")
    if section is not None and (not isinstance(section, str) or not section.strip()):
        blockers.append("section must be a non-empty string or None")
    if isinstance(max_new_chunks, bool) or not isinstance(max_new_chunks, int) or not 1 <= max_new_chunks <= MAX_NEW_CHUNKS:
        blockers.append(f"max_new_chunks must be between 1 and {MAX_NEW_CHUNKS}")
    if blockers:
        return {"status": "blocked", "blockers": blockers, "additions": [], "apply_supported": False,
                "partial_scope": False, "unresolved_sections": []}

    indexes = [chunk.get("chunk_index") for chunk in old_chunks]
    if any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in indexes):
        blockers.append("existing cache contains an invalid chunk_index")
    elif len(indexes) != len(set(indexes)):
        blockers.append("existing cache contains duplicate chunk_index values")
    if any(not isinstance(chunk.get("text"), str) for chunk in old_chunks):
        blockers.append("existing cache contains invalid text")
    if blockers:
        return {"status": "blocked", "blockers": blockers, "additions": [], "apply_supported": False,
                "partial_scope": False, "unresolved_sections": []}

    all_sections, syntax_errors = _source_sections(source)
    all_mode = section is None or _text_key(section) == "all"
    selected = [item for item in all_sections
                if item["region"] != "reference" and (all_mode or item["key"] == _text_key(section))]
    if not selected:
        blockers.append("no uniquely bounded source section matches the selection")
    if all_mode and syntax_errors:
        unresolved.extend({"section_title": None, "scope": "source_structure", "blockers": [error]}
                          for error in syntax_errors)
    counts = {}
    for item in all_sections:
        counts[item["key"]] = counts.get(item["key"], 0) + 1
    if all_mode:
        known_keys = set(counts)
        for chunk in old_chunks:
            if chunk.get("element_type", "prose") == "prose" and str(chunk.get("text", "")).strip():
                key = _text_key(str(chunk.get("section_title", "")))
                if key not in known_keys:
                    unresolved.append({
                        "section_title": chunk.get("section_title"), "scope": "cache_mapping",
                        "chunk_index": chunk["chunk_index"],
                        "blockers": ["cache prose section cannot be mapped to source"],
                    })

    old_structured = [chunk for chunk in old_chunks if _is_preserved_structured(chunk)]
    old_hashes = [_sha256(str(chunk.get("text", "")).encode("utf-8")) for chunk in old_structured]
    additions, reports, modes, safe_chunks = [], [], [], []
    parsed_chunks, parser_mode = [], None
    original_deadline = extractor._parse_deadline
    if not hasattr(signal, "SIGALRM"):
        extractor._parse_deadline = lambda _seconds: contextlib.nullcontext()
    try:
        try:
            parsed_chunks, parser_mode = extractor.build_chunks(source)
        except Exception as exc:
            blockers.append(f"full-source parse failed: {type(exc).__name__}")
        for rank, item in enumerate(all_sections):
            if item not in selected:
                continue
            error = item["boundary_error"]
            if counts[item["key"]] != 1:
                error = "duplicate source section title is ambiguous"
            if error:
                if all_mode:
                    unresolved.append({"section_title": item["title"], "scope": "source_section",
                                       "source_boundary": item["source_boundary"], "blockers": [error]})
                else:
                    blockers.append(f"{item['title']}: {error}")
                reports.append({"section_title": item["title"], "state": "blocked", "blockers": [error]})
                continue
            try:
                section_sink = []
                chunks, mode = extractor.build_chunks(item["bounded_source"], section_sink=section_sink)
                modes.append(mode)
                if mode not in {"pylatexenc", "pylatexenc_recovered"}:
                    raise ValueError(f"bounded target requires pylatexenc parsing, got {mode!r}")
                if any(chunk.get("element_type", "prose") == "prose"
                       and _text_key(str(chunk.get("section_title", ""))) != item["key"] for chunk in chunks):
                    raise ValueError("bounded parser changed source section metadata")
                cached_protected = [(chunk.get("element_type"), chunk.get("has_algorithm") is True,
                                     _sha256(str(chunk.get("text", "")).encode("utf-8")))
                                    for chunk in old_chunks if _is_preserved_structured(chunk)
                                    and _text_key(str(chunk.get("section_title", ""))) == item["key"]]
                bounded_protected = [(chunk.get("element_type"), chunk.get("has_algorithm") is True,
                                      _sha256(str(chunk.get("text", "")).encode("utf-8")))
                                     for chunk in chunks if _is_preserved_structured(chunk)]
                if bounded_protected != cached_protected:
                    raise ValueError("bounded protected sequence differs from cached section")
                if not section_sink or any(_text_key(str(record.get("section_title", ""))) != item["key"]
                                           for record in section_sink):
                    raise ValueError("bounded parser omitted or changed canonical section_sink records")
                section_type = "appendix" if item["region"] == "appendix" else extractor.classify_section(
                    item["title"], rank, len(all_sections))[0]
                missing, report = _missing_prose(item, chunks, old_chunks, section_type)
                additions.extend(missing)
                reports.append(report)
                safe_chunks.extend(dict(record, section_title=item["title"], section_type=section_type)
                                   for record in section_sink)
            except Exception as exc:
                error = str(exc) if isinstance(exc, ValueError) else f"bounded parse failed: {type(exc).__name__}"
                if all_mode:
                    unresolved.append({"section_title": item["title"], "scope": "source_section",
                                       "source_boundary": item["source_boundary"], "blockers": [error]})
                else:
                    blockers.append(f"{item['title']}: {error}")
                reports.append({"section_title": item["title"], "state": "blocked", "blockers": [error]})
    finally:
        extractor._parse_deadline = original_deadline

    if parser_mode not in {"pylatexenc", "pylatexenc_recovered"}:
        blockers.append(f"full source requires pylatexenc parsing, got {parser_mode!r}")
    new_structured = [chunk for chunk in parsed_chunks if _is_preserved_structured(chunk)]
    new_hashes = [_sha256(str(chunk.get("text", "")).encode("utf-8")) for chunk in new_structured]
    if new_hashes != old_hashes or [
            (chunk.get("element_type"), chunk.get("has_algorithm") is True) for chunk in new_structured] != [
            (chunk.get("element_type"), chunk.get("has_algorithm") is True) for chunk in old_structured]:
        blockers.append("equation/algorithm/code sequence differs from cached snapshot")
    if len(additions) > max_new_chunks:
        blockers.append(f"candidate count {len(additions)} exceeds limit {max_new_chunks}")
    keys = [_text_key(chunk["text"]) for chunk in additions]
    if len(keys) != len(set(keys)):
        blockers.append("duplicate missing prose candidates require review")
    next_index = max(indexes, default=-1) + 1
    for offset, chunk in enumerate(additions):
        chunk["chunk_index"] = next_index + offset
    if unresolved and not additions:
        blockers.extend(f"{item['section_title'] or item['scope']}: {error}"
                        for item in unresolved for error in item["blockers"])
    status = "blocked" if blockers else ("dry_run_only" if additions else "no_change")
    section_manifest = None
    if not blockers and safe_chunks:
        source_snapshot = source_bytes if source_bytes is not None else source.encode("utf-8")
        cache_snapshot = cache_bytes if cache_bytes is not None else json.dumps(old_chunks).encode("utf-8")
        if ((source_sha256 is None or _sha256(source_snapshot) == source_sha256)
                and (cache_sha256 is None or _sha256(cache_snapshot) == cache_sha256)):
            updated_cache = json.dumps(old_chunks + additions, ensure_ascii=False, sort_keys=True).encode("utf-8")
            try:
                section_manifest = document_cache.make_section_manifest(
                    paper_id, safe_chunks, updated_cache, source_snapshot, parser_mode,
                    _sha256(document_cache.read_bounded(Path(extractor.__file__))))
                section_manifest["coverage"] = "examined_sections"
            except document_cache.ManifestInvalid as exc:
                blockers.append("canonical section assembly refused: " + str(exc))
                status = "blocked"
    return {
        "status": status, "paper_id": paper_id, "section": None if all_mode else section,
        "parser_mode": parser_mode, "target_parser_mode": modes[0] if len(modes) == 1 else None,
        "target_parser_modes": modes, "section_results": reports,
        "partial_scope": bool(unresolved), "unresolved_sections": unresolved,
        "source_boundary": selected[0]["source_boundary"] if len(selected) == 1 else None,
        "source_sha256": source_sha256 or _sha256(source.encode("utf-8")),
        "cache_sha256": cache_sha256, "old_chunk_count": len(old_chunks),
        "old_max_chunk_index": max(indexes, default=-1),
        "old_structured_hashes": old_hashes, "new_structured_hashes": new_hashes,
        "additions": additions if not blockers else [], "max_new_chunks": max_new_chunks,
        "section_manifest": section_manifest,
        "coverage_min_chars": MIN_MISSING_CHARS, "coverage_min_words": MIN_MISSING_WORDS,
        "apply_supported": False,
        "blockers": list(dict.fromkeys(blockers)) if blockers else [
            "apply is intentionally disabled: Qdrant vectors and the authoritative complete FTS snapshot are not supplied",
            "fts_write replaces all indexed chunks for a paper; additions-only input would drop existing FTS rows",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only bounded missing/partial prose repair plan")
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--section")
    selection.add_argument("--all-sections", action="store_true")
    parser.add_argument("--max-new-chunks", type=int, default=MAX_NEW_CHUNKS)
    args = parser.parse_args(argv)
    source_bytes, cache_bytes = args.source.read_bytes(), args.cache.read_bytes()
    plan = build_plan(args.paper_id, source_bytes.decode("utf-8"), json.loads(cache_bytes),
                      section=None if args.all_sections else args.section,
                      max_new_chunks=args.max_new_chunks,
                      source_sha256=_sha256(source_bytes), cache_sha256=_sha256(cache_bytes),
                      source_bytes=source_bytes, cache_bytes=cache_bytes)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0 if plan.get("status") in {"dry_run_only", "no_change"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
