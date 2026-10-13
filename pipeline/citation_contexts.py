"""Raw, pre-conversion citation leads; no TeX execution or work equivalence inference.

Offsets are half-open Python string (Unicode code point) indices. Source hashes
use UTF-8; the bibliography hash covers sorted caller-supplied (path, raw) pairs
as compact UTF-8 JSON, including duplicates. Paths are opaque provenance labels.
Paragraphs are delimited by blank lines, explicit \\par, or bibliography bounds.
Inputs must be strings and a list/tuple of at most 1024 (path, raw) tuples;
their combined UTF-8 size (including paths) cannot exceed 8 MiB. Entries, stored
provenance, commands and key mentions each have a 10000 count limit; stored
entry_raw bytes have an 8 MiB budget. The materializing wrapper additionally
bounds emitted provenance at 10000 and emitted bytes / complete compact JSON
at 8 MiB (including repeated provenance paths). Streaming has no aggregate
output budget. Bounds raise ValueError, never truncate. No complete-source
coverage is claimed.
arXiv version removal is producer-side canonicalization, not work matching:
target_id omits vN; provenance keeps requested_id and requested_version (N or
None). Ambiguity/duplicate checks compare the requested IDs BEFORE normalization.
"""

import hashlib
import json
import re


_LIMIT = 8 * 1024 * 1024
_MAX_ITEMS = 10000
_ARXIV = r"(?:\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}|[a-z]+(?:-[a-z]+)?(?:\.[a-z]{2})?/\d{2}(?:0[1-9]|1[0-2])\d{3})(?:v[1-9]\d*)?"
_BOUND = r"(?![\w/-]|\.\w)"
_IDS = re.compile(
    r"(?<![\w/])(?:https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/(?P<arxiv_url>" + _ARXIV + r")(?:\.pdf)?"
    r"|arxiv\s*:\s*(?P<arxiv>" + _ARXIV + r")"
    r"|https?://eprint\.iacr\.org/(?P<iacr>\d{4}/[1-9]\d*)(?:\.pdf)?)" + _BOUND
    + r"|(?<![\w/])(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)(?P<doi>10\.\d{4,9}/[^\s{}<>\"\\]+)", re.I)
_DOI = re.compile(r"10\.\d{4,9}/[^\s{}<>\"\\]+", re.I)
_LEX = re.compile(r"%|\\(?:begin\s*\{(?P<env>verbatim\*?|Verbatim|lstlisting|minted|code|comment)\}"
                  r"|(?P<inline>verb\*?|lstinline)(?![A-Za-z@])|[A-Za-z@]+|[\s\S])")
_ITEM = re.compile(r"\\bibitem(?![A-Za-z@])")
_ENV = re.compile(r"\\begin\s*\{thebibliography\}")
_END = re.compile(r"\\end\s*\{thebibliography\}")
_CITE = re.compile(r"\\(?P<name>citep|citet|cite|parencite|textcite|nocite)(?![A-Za-z@])\*?")


def _byte_size(text, limit=_LIMIT):
    if len(text) > limit:
        raise ValueError("input/result exceeds 8 MiB budget")
    size = 0
    for pos in range(0, len(text), 65536):
        size += len(text[pos:pos + 65536].encode("utf-8"))
        if size > limit:
            raise ValueError("input/result exceeds 8 MiB budget")
    return size


def _group(text, pos, bib=False):
    closing = {"{": "}", "[": "]", "(": ")"}
    if pos >= len(text) or text[pos] not in closing:
        return None
    stack, quote, i = [closing[text[pos]]], False, pos + 1
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if bib and len(stack) == 1 and char == '"':
            quote = not quote
        elif not quote:
            if char == "{" or (stack[-1] == "]" and char == "["):
                stack.append(closing[char])
            elif char == stack[-1]:
                stack.pop()
                if not stack:
                    return i + 1
            elif char == "}":
                return None
        i += 1
    return None


def _arguments(text, pos):
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos == len(text) or text[pos] != "[":
            break
        end = _group(text, pos)
        if end is None:
            return None
        pos = end
    end = _group(text, pos) if pos < len(text) and text[pos] == "{" else None
    return (pos + 1, end - 1, end) if end is not None else None


def _parts(text, start, end):
    depth, quote, i, left = 0, False, start, start
    while i < end:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == '"' and depth == 0:
            quote = not quote
        elif not quote:
            depth += (char == "{") - (char == "}")
            if char == "," and depth == 0:
                yield left, i
                left = i + 1
        i += 1
    yield left, end


def _mask(raw, diagnostics, path):
    chars, pos = list(raw), 0
    while match := _LEX.search(raw, pos):
        start, end = match.span()
        if match.group() == "%":
            newline = raw.find("\n", end)
            end = len(raw) if newline < 0 else newline + 1
        elif match.group("env"):
            stop = re.compile(r"\\end\s*\{" + re.escape(match.group("env")) + r"\}").search(raw, end)
            end = stop.end() if stop else len(raw)
            if not stop:
                diagnostics.append(dict(code="unbalanced_literal", path=path, start=start))
        elif match.group("inline"):
            if raw[end:end + 1] == "[" and match.group("inline") == "lstinline":
                end = _group(raw, end) or len(raw)
            stop = raw.find(raw[end], end + 1) if end < len(raw) and not raw[end].isspace() else -1
            if stop < 0 or "\n" in raw[end:stop]:
                diagnostics.append(dict(code="unbalanced_literal", path=path, start=start))
                end = len(raw)
            else:
                end = stop + 1
        else:
            if match.group() == "\\\\":
                chars[start:end] = "  "
            pos = end
            continue
        chars[start:end] = " " * (end - start)
        pos = end
    return "".join(chars)


def _identifiers(text, field="body", archive=""):
    found = [("doi:" + text.lower(), text)] if field == "doi" and _DOI.fullmatch(text) else []
    for match in _IDS.finditer(text):
        name = match.lastgroup
        kind = "arxiv" if name.startswith("arxiv") else name
        value = match.group(name)
        # Sentence punctuation is outside a free-text DOI, not an explicit field.
        if kind == "doi" and field == "body":
            value = value.rstrip(".,;")
        found.append((kind + ":" + value.lower(), match.group()))
    if field == "eprint" and re.fullmatch(_ARXIV, text, re.I):
        if "/" in text or archive.lower() == "arxiv":
            found.append(("arxiv:" + text.lower(), text))
    return sorted(set(found))


def _bbl(text, raw, path, add, diagnostics):
    pos, stop_index = 0, 0
    stops = [match.start() for match in _END.finditer(text)] + [len(text)]
    while match := _ITEM.search(text, pos):
        args = _arguments(text, match.end())
        if args is None:
            diagnostics.append(dict(code="malformed_bibitem", path=path, start=match.start()))
            break
        a, b, pos = args
        following = _ITEM.search(text, pos)
        while stops[stop_index] < pos:
            stop_index += 1
        end = min(following.start() if following else len(text), stops[stop_index])
        key = text[a:b].strip()
        if not key:
            diagnostics.append(dict(code="malformed_bibitem", path=path, start=match.start()))
        else:
            add(key, path, raw, match.start(), end, [("body", text[pos:end], "")])
        pos = end


def _bib(text, raw, path, add, diagnostics):
    pattern, pos = re.compile(r"@([A-Za-z]+)\s*([{(])"), 0
    while match := pattern.search(text, pos):
        end = _group(text, match.end() - 1, bib=True)
        if end is None:
            diagnostics.append(dict(code="unbalanced_bibtex", path=path, start=match.start()))
            key = text[match.end():].split(",", 1)[0].strip()
            if key and match.group(1).lower() not in ("comment", "string", "preamble"):
                add(key, path, raw, match.start(), len(text), [])
            break
        pos = end
        if match.group(1).lower() in ("comment", "string", "preamble"):
            continue
        parts = list(_parts(text, match.end(), end - 1))
        key = text[slice(*parts[0])].strip()
        fields, invalid = {}, not key or len(parts) == 1
        for a, b in parts[1:]:
            chunk = text[a:b].strip()
            if not chunk:
                continue
            field = re.fullmatch(r"([A-Za-z][\w-]*)\s*=\s*([\s\S]+)", chunk)
            if not field:
                invalid = True
                continue
            name, value = field.group(1).lower(), field.group(2).strip()
            if name in fields:
                invalid = True
            if value.startswith("{") and _group(value, 0) == len(value):
                value = value[1:-1].strip()
            elif re.fullmatch(r'"(?:\\.|[^"\\])*"', value):
                value = value[1:-1].strip()
            elif not value.isdecimal():
                # Bare macros / concatenations are not explicit identifier values.
                if name in ("url", "doi", "eprint", "archiveprefix"):
                    invalid = True
                value = ""
            fields[name] = value
        if invalid:
            diagnostics.append(dict(code="malformed_bibtex", path=path, start=match.start()))
        values = [(name, value, fields.get("archiveprefix", "")) for name, value in fields.items()
                  if name in ("url", "doi", "eprint")]
        if key:
            add(key, path, raw, match.start(), end, [] if invalid else values)


def _citation_commands(source, visible, left, right, diagnostics, charge=None):
    pos = left
    while match := _CITE.search(visible, pos, right):
        if charge:
            charge("commands")
        args = _arguments(visible, match.end())
        if args is None or args[2] > right:
            if diagnostics is not None:
                diagnostics.append(dict(code="malformed_citation", start=match.start(), end=right))
            break
        a, b, pos = args
        if match.group("name") == "nocite":
            continue
        keys = []
        for ka, kb in _parts(visible, a, b):
            if charge:
                charge("keys")
            ka += len(visible[ka:kb]) - len(visible[ka:kb].lstrip())
            kb = ka + len(visible[ka:kb].rstrip())
            keys.append(dict(key=visible[ka:kb], start=ka, end=kb, raw=source[ka:kb]))
        if any(not key["key"] for key in keys):
            if diagnostics is not None:
                diagnostics.append(dict(code="malformed_citation", start=match.start(), end=pos))
            continue
        yield match, pos, keys


class _CitationContextStream:
    def __init__(self, contexts, source_sha256, bibliography_sha256, diagnostics):
        self._contexts = contexts
        self.source_sha256 = source_sha256
        self.bibliography_sha256 = bibliography_sha256
        self.diagnostics = diagnostics

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._contexts)


def stream_citation_contexts(source, bibliography_files=()):
    """Eagerly validate inputs; return a one-shot iterator of legacy-shaped contexts.

    Diagnostics finish accumulating on exhaustion. Shared provenance lists are
    read-only to callers; copying/serializing an entire stream is not bounded here.
    """
    if not isinstance(source, str):
        raise TypeError("source must be str")
    if not isinstance(bibliography_files, (list, tuple)):
        raise TypeError("bibliography_files must be a finite list or tuple")
    if len(bibliography_files) > 1024:
        raise ValueError("bibliography_files exceeds 1024 entries")
    total = _byte_size(source)
    for pair in bibliography_files:
        if not isinstance(pair, tuple) or len(pair) != 2 or not all(isinstance(v, str) for v in pair):
            raise TypeError("bibliography entries must be (pathstr, rawstr) tuples")
        for value in pair:
            total += _byte_size(value, _LIMIT - total)
    files = sorted(bibliography_files)
    diagnostics, entries = [], {}
    counts = dict(entries=0, stored_provenance=0, commands=0, keys=0)
    budgets = dict(stored_bytes=0)

    def charge(name, count=1):
        counts[name] += count
        if counts[name] > _MAX_ITEMS:
            raise ValueError("result exceeds 10000 " + name)

    def charge_bytes(name, size):
        budgets[name] += size
        if budgets[name] > _LIMIT:
            raise ValueError("result exceeds 8 MiB " + name)

    def add(key, path, raw, start, end, values):
        charge("entries")
        entry_raw = raw[start:end]
        entry_bytes = _byte_size(entry_raw)
        identifiers = {}
        for field, value, archive in values:
            for target, literal in _identifiers(value, field, archive):
                charge("stored_provenance")
                charge_bytes("stored_bytes", entry_bytes)
                version = re.search(r"v([1-9]\d*)$", target) if target.startswith("arxiv:") else None
                canonical = target[:version.start()] if version else target
                identifiers.setdefault(target, []).append(dict(
                    path=path, origin="source" if path is None else "bibliography_file",
                    entry_start=start, entry_end=end, entry_raw=entry_raw,
                    field=field, identifier_raw=literal, target_id=canonical,
                    requested_id=target, requested_version=int(version[1]) if version else None))
        entries.setdefault(key, []).append(identifiers)
        if len(identifiers) > 1:
            diagnostics.append(dict(code="ambiguous_identifiers", key=key, path=path,
                                    start=start, target_ids=sorted(identifiers)))

    visible = _mask(source, diagnostics, None)
    _bbl(visible, source, None, add, diagnostics)
    for path, raw in files:
        text = _mask(raw, diagnostics, path)
        if path.lower().endswith(".bib"):
            _bib(text, raw, path, add, diagnostics)
        elif path.lower().endswith(".bbl"):
            _bbl(text, raw, path, add, diagnostics)
        else:
            diagnostics.append(dict(code="unsupported_bibliography_file", path=path))
    resolved, reasons = {}, {}
    for key, candidates in entries.items():
        signatures = {tuple(sorted(candidate)) for candidate in candidates}
        if len(signatures) > 1:
            reasons[key] = "conflicting_duplicate_key"
            diagnostics.append(dict(code=reasons[key], key=key))
        elif any(len(candidate) > 1 for candidate in candidates):
            reasons[key] = "ambiguous_identifiers"
        elif not candidates[0]:
            reasons[key] = "no_exact_identifier"
        else:
            target = next(iter(candidates[0]))
            provenance = [p for candidate in candidates for p in candidate[target]]
            resolved[key] = provenance[0]["target_id"], provenance

    ranges, pos = [], 0
    while match := _ENV.search(visible, pos):
        stop = _END.search(visible, match.end())
        pos = stop.end() if stop else len(source)
        ranges.append((match.start(), pos))
        if not stop:
            diagnostics.append(dict(code="unbalanced_bibliography", start=match.start()))
    for match in _ITEM.finditer(visible):
        if not any(a <= match.start() < b for a, b in ranges):
            ranges.append((match.start(), len(source)))
            break
    chars = list(visible)
    for a, b in ranges:
        chars[a:b] = " " * (b - a)
    visible = "".join(chars)
    cuts = {0, len(source)}
    for match in re.finditer(r"\r?\n[ \t]*\r?\n|\\par(?![A-Za-z@])", visible):
        cuts.update(match.span())
    for a, b in ranges:
        cuts.update((a, b))
    cuts = sorted(cuts)
    for left, right in zip(cuts, cuts[1:]):
        for _ in _citation_commands(source, visible, left, right, None, charge):
            pass

    def contexts():
        for left, right in zip(cuts, cuts[1:]):
            citations, leads = [], []
            for match, pos, keys in _citation_commands(source, visible, left, right, diagnostics):
                citations.append(dict(start=match.start(), end=pos, raw=source[match.start():pos],
                                      command=match.group("name"), keys=keys))
                for key in keys:
                    name = key["key"]
                    if name not in resolved:
                        diagnostics.append(dict(code="unresolved_reference" if name in entries else "unknown_reference",
                                                key=name, start=key["start"], end=key["end"],
                                                reason=reasons.get(name, "not_supplied")))
                        continue
                    target, provenance = resolved[name]
                    leads.append(dict(key=name, citation_start=match.start(), citation_end=pos,
                                      target_id=target, association="lead", provenance=provenance))
            if citations:
                start = left + len(source[left:right]) - len(source[left:right].lstrip())
                end = left + len(source[left:right].rstrip())
                yield dict(start=start, end=end, raw=source[start:end], search_text=visible[start:end], citations=citations,
                           resolved=leads, may_discuss_multiple_works=len({k["key"] for c in citations for k in c["keys"]}) > 1)

    return _CitationContextStream(
        contexts(), hashlib.sha256(source.encode("utf-8")).hexdigest(),
        hashlib.sha256(json.dumps(files, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest(),
        diagnostics)


def extract_citation_contexts(source: str, bibliography_files=()) -> dict:
    """Materialize citation leads with the original aggregate output budgets."""
    stream = stream_citation_contexts(source, bibliography_files)
    contexts, emitted_provenance, emitted_bytes = [], 0, 0
    for context in stream:
        for lead in context["resolved"]:
            provenance = lead["provenance"]
            emitted_provenance += len(provenance)
            if emitted_provenance > _MAX_ITEMS:
                raise ValueError("result exceeds 10000 emitted_provenance")
            emitted_bytes += sum(_byte_size(p["entry_raw"]) for p in provenance)
            if emitted_bytes > _LIMIT:
                raise ValueError("result exceeds 8 MiB emitted_bytes")
        contexts.append(context)
        if len(contexts) > 10000:
            raise ValueError("source exceeds 10000 citation contexts")
    result = dict(schema_version=1, source_sha256=stream.source_sha256,
                  bibliography_sha256=stream.bibliography_sha256,
                  contexts=contexts, diagnostics=stream.diagnostics)
    remaining = _LIMIT
    for chunk in json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).iterencode(result):
        remaining -= _byte_size(chunk, remaining)
    return result
