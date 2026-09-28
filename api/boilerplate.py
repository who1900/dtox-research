"""
boilerplate.py -- detector for chunks that carry no paper content.

Shared by the search API (a junk chunk must not stand in for its paper in the
results) and the extraction pipeline (junk chunks are not embedded). Pure
functions, stdlib only, so both sides can import it flat or as api.boilerplate.

Deliberately conservative: a missed junk chunk costs one weak result, a dropped
real paragraph costs the paper its evidence. Every text rule therefore also
requires a low share of English function words, which prose never has and
preambles, author blocks and bibliographies always lack.
"""

import os
import re

BOILERPLATE_FILTER = os.getenv("BOILERPLATE_FILTER", "1") == "1"

# equations, algorithms, code and tables are dense in symbols by nature
PROTECTED_ELEMENTS = {"equation", "algorithm", "code", "table", "figure"}

_STOPWORDS = frozenset("""
the of and to in is are we a an that for on with as by this it be from which
can our these such not or at was were has have been their its also than more
but into using used use between each both where when how while over under
""".split())
_WORD_RE = re.compile(r"[A-Za-z]+")

_MACRO_DEF_RE = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand|def|newtheorem|usepackage|"
    r"documentclass|setlength|DeclareMathOperator|makeatletter|hypersetup|"
    r"newenvironment|let)\b")
_MACRO_PARAM_RE = re.compile(r"#\d")
_TEX_DIMENSION_RE = re.compile(r"(?<![\w.])=?\s*\d*\.?\d+\s?(?:pt|em|ex|cm|mm)\b")
# \newtheorem{thm}{Theorem} flattened by the extractor: "thmTheorem lemLemma"
_MACRO_NAME_RE = re.compile(r"\b([a-z]{3,8})([A-Z][a-z]+)\b")
# \makeatletter internals: "@Gin@width", "plus -4@ minus -4@"
_MAKEATLETTER_RE = re.compile(r"@[A-Za-z]{2,}@|\bplus\s*-?\d+@")

_EMAIL_RE = re.compile(
    r"(?:[\w.\-]*(?:\.\.\.)?|\})@[\w\-]+(?:\.[\w\-]+)+|\bE-?mail\s*:")
_AFFILIATION_RE = re.compile(
    r"\b(?:Universit\w*|Institut\w*|Laborator\w*|Department|School of|College|"
    r"Academy|Research|Inc\.|Ltd\.?|GmbH|Corporation|Google|Microsoft|Meta AI)\b")
_CAPITALIZED_RE = re.compile(r"\b[A-Z][a-z]+\b")
# the rule line \maketitle leaves under a title/author/date block
_TITLE_RULE_RE = re.compile(r"={20,}|\*{5,}")

# one of these alone gives a venue header away
_ACM_STRONG = re.compile(
    r"printacmref|printfolios|@shellescape|acmcopyright|draftminted|"
    r"ACM[- ]Reference[- ]Format|\bacmart\b", re.I)
_ACM_MARKERS = [re.compile(p, re.I) for p in (
    r"\bISBN\b|\b97[89]-\d", r"10\.1145/", r"\bCCS '\d\d\b", r"Proceedings of",
    r"Permission to make digital")]

_BIB_MARKERS = [re.compile(p) for p in (
    r"\\bibitem", r"arXiv preprint", r"\bIn Proc(?:eedings|\.)", r"\bpp\.\s*\d+",
    r"Advances in Neural Information Processing Systems", r"\bJournal of\b",
    r"(?m)^\s*\[\d+\]\s")]
# natbib entry label: "[Duan et al.(2024b)Duan, Wang, ...]"
_NATBIB_LABEL_RE = re.compile(r"\[[^\]\n]{0,80}\(\d{4}[a-z]?\)[^\]\n]*\]")

_CHECKLIST_LABELS = ("Question:", "Answer:", "Justification:", "Guidelines:")
_JUNK_TITLE_RE = re.compile(
    r"^(?:references?|reference list|bibliography|acknowledge?ments?|"
    r"(?:\w+ ){0,2}checklist)$", re.I)
_TITLE_NOISE_RE = re.compile(
    r"\\[A-Za-z]+\*?|[{}$*]|^\s*(?:[A-Z]|\d+(?:\.\d+)*)[.)]?\s+")


def _stopword_ratio(text):
    words = _WORD_RE.findall(text)
    if not words:
        return 0.0
    return sum(1 for w in words if w.lower() in _STOPWORDS) / len(words)


def _clean_title(title):
    return _TITLE_NOISE_RE.sub("", title or "").strip(" .:*")


def is_boilerplate_chunk(text, section_title="", section_type="", element_type=None):
    """True if the chunk is preamble/macros, an author block, a venue header,
    a conference checklist or a reference list rather than paper content."""
    if element_type in PROTECTED_ELEMENTS:
        return False
    text = (text or "").strip()
    if not text:
        return False

    if _JUNK_TITLE_RE.match(_clean_title(section_title)):
        return True

    # QA datasets also use Question:/Answer:, so "Justification:" is required
    if "Justification:" in text and sum(label in text for label in _CHECKLIST_LABELS) >= 2:
        return True

    ratio = _stopword_ratio(text)
    if ratio >= 0.25:
        return False

    if sum(1 for rx in _BIB_MARKERS if rx.search(text)) >= 3 and ratio < 0.22:
        return True
    if ratio < 0.1 and _NATBIB_LABEL_RE.search(text):
        return True

    if _ACM_STRONG.search(text) or sum(1 for rx in _ACM_MARKERS if rx.search(text)) >= 2:
        return True

    # preamble: macro definitions and parameters, or (only when the text is
    # otherwise wordless) layout dimensions such as "=0pt", "501em"
    macro_hits = (len(_MACRO_DEF_RE.findall(text)) * 2
                  + len(_MACRO_PARAM_RE.findall(text)))
    dimension_hits = len(_TEX_DIMENSION_RE.findall(text))
    if (macro_hits >= 3 and ratio < 0.15) or (macro_hits + dimension_hits >= 4 and ratio < 0.08):
        return True
    if len(_MAKEATLETTER_RE.findall(text)) >= 2:
        return True
    if sum(1 for m in _MACRO_NAME_RE.finditer(text)
           if m.group(2).lower().startswith(m.group(1))) >= 3:
        return True

    if len(text) < 800 and _TITLE_RULE_RE.search(text):
        return True

    if _EMAIL_RE.search(text) and ratio < 0.2:
        return True
    if ratio < 0.12:
        words = _WORD_RE.findall(text)
        if (len(_AFFILIATION_RE.findall(text)) >= 2 and words
                and len(_CAPITALIZED_RE.findall(text)) / len(words) > 0.4):
            return True

    return False
