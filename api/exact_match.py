"""Exact-name lookup: identifiers and titles named inside a query.

A vector search answers "EIP-7702" with whatever text is nearest, and a long
query that names HotStuff dilutes the name among its own keywords. Asking for a
document by its identifier or its title is not a subject query, so it is
resolved separately, by string rules over the corpus's own titles, and the
document is put at the top of the results.
"""
import re
from collections import Counter

_WORD = re.compile(r"[a-z0-9]+")

# a document id spelled inside free text
_EIP = re.compile(r"\b(?:eip|erc)[\s:\-]*(\d{1,5})\b", re.I)
_SIMD = re.compile(r"\bsimd[\s:\-]*(\d{1,4})\b", re.I)
_ARXIV = re.compile(r"(?<![\d./])(\d{4}\.\d{4,5})(?:v\d+)?(?![\d.]*\d)", re.I)
_IACR = re.compile(r"(?:iacr[\s:]*|eprint(?:\.iacr\.org)?[\s:/]*)(\d{4}/\d{1,5})\b", re.I)
_PREFIXED = re.compile(r"\b(?:wp|gh|oa|pmlr|acl|hal|doi):[^\s,;()\"']+", re.I)
_WHITEPAPER = re.compile(r"\b(?:white\s?paper|yellow\s?paper|litepaper|lite\s?paper)\b", re.I)

# a title head that is one word must be rare among titles; several words are
# rare by construction but still must not be shared by many documents
MAX_SINGLE_TOKEN_DF = 80
MAX_HEAD_DOCS = 8
MAX_RARE_TOKEN_DF = 20
MIN_NAME_CITES = 25
MIN_PHRASE_CITES = 300
MIN_SINGLE_TOKEN_LEN = 6
MAX_HEAD_TOKENS = 6
MIN_FULL_TITLE_TOKENS = 3
MAX_FULL_TITLE_TOKENS = 14

_FTS_SMALL = frozenset("a an and as at by for in is of on or the to with".split())

_GENERIC = frozenset("""
attention transformer transformers blockchain blockchains bitcoin ethereum solana
agents agent learning language models model security privacy consensus network
""".split())


def tokens(text):
    return _WORD.findall((text or "").lower())


def extract_ids(query):
    """Document ids the query spells out, in order of appearance, unchecked."""
    text = query or ""
    found = []

    def add(pos, doc_id):
        if doc_id not in [d for _, d in found]:
            found.append((pos, doc_id))

    for m in _EIP.finditer(text):
        add(m.start(), f"eip:{int(m.group(1))}")
    for m in _SIMD.finditer(text):
        add(m.start(), f"simd:{int(m.group(1)):04d}")
    for m in _IACR.finditer(text):
        add(m.start(), f"iacr:{m.group(1)}")
    for m in _ARXIV.finditer(text):
        add(m.start(), m.group(1))
    for m in _PREFIXED.finditer(text):
        raw = m.group(0).rstrip(".")
        prefix, _, rest = raw.partition(":")
        add(m.start(), f"{prefix.lower()}:{rest}")
    found.sort()
    return [d for _, d in found]


def _head(title):
    """The name before the first colon or spaced dash, else the whole title."""
    for sep in (":", " - ", " -- "):
        if sep in title:
            return title.split(sep, 1)[0]
    return title


class TitleIndex:
    """Normalised title phrases -> document ids, built once from the corpus."""

    def __init__(self, rows):
        # rows: (id, title, citation_count)
        self.heads = {}
        self.fulls = {}
        self.wp = {}
        self.cites = {}
        self.df = Counter()
        prepared = []
        for doc_id, title, cites in rows:
            if not title:
                continue
            toks = tokens(title)
            if not toks:
                continue
            self.df.update(set(toks))
            self.cites[doc_id] = cites or 0
            prepared.append((doc_id, title, toks))
        for doc_id, title, toks in prepared:
            if doc_id.startswith("wp:"):
                self._add_wp(doc_id, title, toks)
                continue
            if doc_id.startswith("gh:") or doc_id.startswith("eip:") or doc_id.startswith("simd:"):
                # bare file/section names ("cpi", "index") and spec titles are
                # reached by id, not by a title match
                continue
            head = tokens(_head(title))
            if 0 < len(head) <= MAX_HEAD_TOKENS:
                self.heads.setdefault(tuple(head), []).append(doc_id)
            if MIN_FULL_TITLE_TOKENS <= len(toks) <= MAX_FULL_TITLE_TOKENS:
                self.fulls.setdefault(tuple(toks), []).append(doc_id)

    def _add_wp(self, doc_id, title, toks):
        aliases = {tuple(tokens(_head(title))), tuple(toks)}
        slug = doc_id.split(":", 1)[1].replace("_", "-").split("-")
        aliases.add(tuple(t for t in slug if t))
        if slug and slug[0]:
            aliases.add((slug[0].lower(),))
        for alias in aliases:
            if alias:
                self.wp.setdefault(alias, []).append(doc_id)
        # a titled whitepaper also answers to its title head as a proper name
        self.heads.setdefault(tuple(tokens(_head(title))), []).append(doc_id)

    def _best(self, ids):
        # a copy with real text and its own record before an OpenAlex stub
        return sorted(ids, key=lambda d: (d.startswith("oa:"), -self.cites.get(d, 0)))

    def _head_ok(self, key, ids, originals, content_count):
        """Is this title head a name, or just words that happen to open a title?"""
        if len(ids) > MAX_HEAD_DOCS:
            return False
        best = max(self.cites.get(d, 0) for d in ids)
        if len(key) == 1:
            word = key[0]
            if word in _GENERIC or len(word) < MIN_SINGLE_TOKEN_LEN:
                return False
            return self.df.get(word, 0) <= MAX_SINGLE_TOKEN_DF and (
                best >= MIN_NAME_CITES or any(d.startswith(("wp:", "eip:", "simd:")) for d in ids))
        if sum(len(t) for t in key) < 8:
            return False
        if min(self.df.get(t, 0) for t in key) <= MAX_RARE_TOKEN_DF:
            return True
        # a phrase made of common words ("Generative Agents") is a name only when
        # the query writes it as one: capitalised, prominent, and most of the query
        capitalised = all(w[:1].isupper() or w.lower() in _FTS_SMALL for w in originals)
        return capitalised and best >= MIN_PHRASE_CITES and len(key) / max(1, content_count) >= 0.5

    def match(self, query, limit=2):
        """[(doc_id, "title match")] for titles or names the query contains."""
        original = re.findall(r"[A-Za-z0-9]+", query or "")
        qt = [w.lower() for w in original]
        if not qt:
            return []
        content_count = sum(1 for w in qt if w not in _FTS_SMALL and len(w) > 2) or 1
        hits = []  # (span length, doc_id)
        seen = set()

        def take(ids, span):
            for doc_id in self._best(ids)[:1]:
                if doc_id not in seen:
                    seen.add(doc_id)
                    hits.append((span, doc_id))

        for n in range(min(len(qt), MAX_FULL_TITLE_TOKENS), 0, -1):
            for i in range(len(qt) - n + 1):
                key = tuple(qt[i:i + n])
                if n >= MIN_FULL_TITLE_TOKENS and key in self.fulls:
                    take(self.fulls[key], n)
                if key in self.heads and n <= MAX_HEAD_TOKENS:
                    ids = self.heads[key]
                    if self._head_ok(key, ids, original[i:i + n], content_count):
                        take(ids, n)
        if _WHITEPAPER.search(query or ""):
            for n in range(min(len(qt), 6), 0, -1):
                for i in range(len(qt) - n + 1):
                    key = tuple(qt[i:i + n])
                    if key in self.wp:
                        take(self.wp[key], n + 10)
        hits.sort(key=lambda t: -t[0])
        return [(d, "title match") for _, d in hits[:limit]]
