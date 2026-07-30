"""
extractor.py -- LaTeX-aware fulltext extraction for dtox-research.

Produces structured chunks from a paper's combined LaTeX source:
  - sections classified into a canonical taxonomy (3-layer classifier)
  - special environments (equation/algorithm/code/table/figure) captured as
    first-class element chunks (raw LaTeX preserved)
  - prose chunked by paragraph into ~1500-3500 char pieces

Falls back to a light heuristic parse (no pylatexenc) if the source is
malformed or pylatexenc raises, and to a single abstract chunk if there is
no usable text at all (handled by caller, not here).
"""

import re

CANONICAL_TYPES = [
    "introduction", "related_work", "method", "experiments",
    "analysis", "limitations", "conclusion", "appendix", "other",
]

# --- chunk size limit (embed-small tokenizes with max_length=512, truncation=True;
# anything longer than that silently loses its tail from the embedding). No
# transformers/tokenizers package is available in this venv, so length is
# estimated with a conservative chars-heuristic threshold rather than exact
# token counts (chars/4 ~= tokens, so 1700 chars ~= 425 tokens, comfortably
# under the 512 limit). Any prose piece or element (table/equation/algorithm/
# code) longer than this gets split into overlapping parts. ---
CHUNK_CHAR_LIMIT = 1700
CHUNK_OVERLAP_RATIO = 0.15

# --- embed-time filter (task: don't embed low-value chunks). Chunking still
# runs on the whole paper (we need full structure in chunk_cache/state.db);
# this only controls what gets embedded + upserted to Qdrant. Kept as two
# separate constants, combined with AND, so either axis can be widened/
# narrowed independently: today only figure captions inside related_work/
# conclusion/appendix are skipped (a caption with no figure is useless), all
# other content in those sections (prose discussion, tables, etc.) is kept.
SKIP_SECTION_TYPES = {"related_work", "conclusion", "appendix"}
SKIP_ELEMENT_TYPES = {"figure"}


def should_embed(chunk):
    """False if this chunk should be skipped for embedding/Qdrant upsert."""
    return not (
        chunk.get("section_type") in SKIP_SECTION_TYPES
        and chunk.get("element_type") in SKIP_ELEMENT_TYPES
    )

# --- Layer 1: keyword/regex classification -----------------------------
# Proof-style sections come first: they are structurally appendix material and
# must never fall through to the positional rule, which used to label
# "Proofs of Theorem 1" as an introduction just because it came first.
LAYER1_PATTERNS = [
    ("appendix", re.compile(r"\b(proof|proofs|lemma|theorem|corollary|derivation|derivations)\b", re.I)),
    ("appendix", re.compile(r"appendix|supplement", re.I)),
    ("limitations", re.compile(r"limitation|threat|weakness", re.I)),
    ("related_work", re.compile(r"related|prior work|literature", re.I)),
    ("conclusion", re.compile(r"conclu|future work|summary", re.I)),
    ("introduction", re.compile(r"introduc|overview|motivation", re.I)),
    ("analysis", re.compile(r"analysis|discussion|ablation", re.I)),
    ("experiments", re.compile(r"experiment|evaluation|result|benchmark|empirical|dataset|setup", re.I)),
    ("method", re.compile(r"method|approach|model|architecture|algorithm|methodology|framework|system|proposed|design|network", re.I)),
]

# --- Layer 3: semantic anchor texts (embedded once, cached) -------------
ANCHOR_TEXTS = {
    "introduction": "Introduction. Overview. Motivation for the problem.",
    "related_work": "Related work. Prior work. Literature review. Background research.",
    "method": "Method. Approach. Model architecture. Proposed algorithm. Framework. System design.",
    "experiments": "Experiments. Evaluation. Results. Benchmarks. Empirical study. Setup and dataset.",
    "analysis": "Analysis. Discussion. Ablation study.",
    "limitations": "Limitations. Threats to validity. Weaknesses.",
    "conclusion": "Conclusion. Concluding remarks. Future work. Summary.",
    "appendix": "Appendix. Supplementary material.",
}
# Calibrated on real section titles: genuine headings score >=0.755 against
# their anchor ("Model Architecture" 0.858, "Related Work" 0.813, "Experimental
# Setup" 0.755) while off-type headings top out at 0.665 ("Maximum likelihood
# estimator" 0.665, "Analyzing partition function" 0.623, "Proofs of Theorem 1"
# 0.610). 0.72 sits in that gap: below it we honestly say "other" instead of
# forcing a confident-looking but wrong label.
SEMANTIC_THRESHOLD = 0.72

# The positional rule (first section = intro, last = conclusion) only holds for
# generically-named sections. A section with a real, specific title is
# classified on its own merits.
_GENERIC_TITLE_RE = re.compile(
    r"^\s*(\d+[.)]?\s*)?(introduction|intro|overview|motivation|background|"
    r"conclusion|conclusions|concluding remarks|summary|discussion|preliminaries)?\s*$",
    re.I,
)


def _is_generic_title(title):
    """True for empty/unnamed sections or plain boilerplate headings."""
    return bool(_GENERIC_TITLE_RE.match(title or ""))

_anchor_vectors_cache = None  # dict[type] -> vector, filled lazily


def get_anchor_vectors(embed_fn):
    """embed_fn(list[str]) -> list[vector]. Cached across calls in-process."""
    global _anchor_vectors_cache
    if _anchor_vectors_cache is None:
        types = list(ANCHOR_TEXTS.keys())
        vecs = embed_fn([ANCHOR_TEXTS[t] for t in types])
        _anchor_vectors_cache = dict(zip(types, vecs))
    return _anchor_vectors_cache


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def classify_section(title, idx, total, embed_fn=None):
    """3-layer classification. embed_fn is optional; if None, layer 3 is skipped."""
    title = title or ""
    for section_type, pattern in LAYER1_PATTERNS:
        if pattern.search(title):
            return section_type, "layer1"
    if _is_generic_title(title):
        if idx == 0:
            return "introduction", "layer2"
        if idx == total - 1:
            return "conclusion", "layer2"
    if embed_fn is not None and title.strip():
        try:
            anchors = get_anchor_vectors(embed_fn)
            title_vec = embed_fn([title])[0]
            best_type, best_score = None, -1.0
            for t, v in anchors.items():
                score = _cosine(title_vec, v)
                if score > best_score:
                    best_type, best_score = t, score
            if best_score > SEMANTIC_THRESHOLD:
                return best_type, "layer3"
        except Exception:
            pass
    return "other", "none"


# ---------------------------------------------------------------------------
# Comment stripping / section splitting (regex-based, robust to broken TeX)
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"(?<!\\)%.*")


def strip_comments(text):
    lines = text.split("\n")
    out = []
    for line in lines:
        out.append(_COMMENT_RE.sub("", line))
    return "\n".join(out)


SECTION_RE = re.compile(r"\\(section|subsection)\*?\{([^}]*)\}")


def split_sections_raw(text):
    """Returns list of (title, raw_body). Falls back to whole doc as one section."""
    matches = list(SECTION_RE.finditer(text))
    if not matches:
        return [("full text", text)]
    sections = []
    for i, m in enumerate(matches):
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((title, body))
    return sections if sections else [("full text", text)]


# ---------------------------------------------------------------------------
# Paragraph chunking (shared by prose and legacy fallback)
# ---------------------------------------------------------------------------

def chunk_paragraphs(body, target_min=1500, target_max=3500):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    chunks = []
    cur = ""
    for p in paragraphs:
        if len(cur) + len(p) + 2 <= target_max:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
            if len(p) > target_max:
                for i in range(0, len(p), target_max):
                    chunks.append(p[i:i + target_max])
                cur = ""
            else:
                cur = p
    if cur:
        chunks.append(cur)
    return chunks if chunks else ([body] if body.strip() else [])


def split_long_text(text, limit=CHUNK_CHAR_LIMIT, overlap_ratio=CHUNK_OVERLAP_RATIO):
    """Split text into pieces no longer than `limit` chars, with ~overlap_ratio
    overlap carried from the tail of one piece into the start of the next so
    meaning at a cut boundary isn't lost. Tries paragraph boundaries first,
    falls back to sentence boundaries, then hard character slicing as a last
    resort. Returns [text] unchanged if it already fits.

    Used for BOTH prose pieces and non-prose elements (table/equation/
    algorithm/code) -- element_type text can be far longer than prose (a big
    LaTeX table dumped verbatim, for instance), and was previously never
    split at all.
    """
    if len(text) <= limit:
        return [text]
    overlap = max(int(limit * overlap_ratio), 0)
    step = max(limit - overlap, 1)

    def hard_split(s):
        # character-level split, stepping forward by (limit - overlap) each
        # time; always makes forward progress, no recursion.
        out = []
        pos = 0
        while pos < len(s):
            out.append(s[pos:pos + limit])
            if pos + limit >= len(s):
                break
            pos += step
        return out

    units = [u for u in re.split(r"\n\s*\n", text) if u.strip()]
    if len(units) <= 1:
        units = [u for u in re.split(r"(?<=[.!?])\s+", text) if u.strip()]
    if len(units) <= 1:
        return hard_split(text)

    # Iterative packing (no recursion, so no risk of blowing the stack on
    # pathological input like a table row or a single giant run-on sentence):
    # greedily fill `cur` up to `limit`, carrying `overlap` chars of the
    # previous piece's tail into the next one. Any individual unit bigger
    # than `limit` on its own is hard-split in place instead of recursing.
    pieces = []
    cur = ""
    for u in units:
        if len(u) > limit:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.extend(hard_split(u))
            continue
        candidate = f"{cur}\n\n{u}" if cur else u
        if len(candidate) <= limit:
            cur = candidate
            continue
        pieces.append(cur)
        tail = cur[-overlap:] if overlap else ""
        candidate2 = f"{tail}\n\n{u}" if tail else u
        cur = candidate2 if len(candidate2) <= limit else u
    if cur:
        pieces.append(cur)
    return pieces if pieces else [text]


# ---------------------------------------------------------------------------
# pylatexenc-based environment extraction
# ---------------------------------------------------------------------------

EQUATION_ENVS = {"equation", "align", "gather", "eqnarray", "displaymath", "multline", "flalign"}
ALGORITHM_ENVS = {"algorithm", "algorithmic", "algorithm2e"}
CODE_ENVS = {"lstlisting", "verbatim", "minted", "Verbatim"}
TABLE_ENVS = {"table", "tabular", "tabularx", "longtable"}

# A `table*` environment is mostly not the table: \fontsize, \setlength,
# \rotatebox and \scalebox wrap the thing before a single measurement appears.
# Captured verbatim it splits into several chunks of which the first holds the
# caption and no numbers -- 52% of the table chunks in the index carry no
# measurable content at all, and a request for "just the tables" spent its whole
# budget on column headers. Keeping the caption and the tabular body gives one
# chunk that is worth embedding and worth reading.
_TABULAR_RE = re.compile(
    r"\\begin\{(tabular\*?|tabularx|longtable|array)\}.*?\\end\{\1\}", re.S)
_CAPTION_START_RE = re.compile(r"\\caption\*?\s*\{")


def _balanced_braces(text, open_pos):
    """Content of the {...} group starting at open_pos, honouring nesting."""
    depth = 0
    for i in range(open_pos, len(text)):
        if text[i] == "{" and (i == 0 or text[i - 1] != "\\"):
            depth += 1
        elif text[i] == "}" and text[i - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[open_pos + 1:i]
    return ""


def table_essence(raw):
    """Caption plus the tabular body, dropping the layout scaffolding."""
    bodies = [m.group(0) for m in _TABULAR_RE.finditer(raw or "")]
    if not bodies:
        return raw  # nothing recognisable inside: keep it whole rather than lose it
    caption = ""
    m = _CAPTION_START_RE.search(raw)
    if m:
        caption = " ".join(_balanced_braces(raw, m.end() - 1).split())
    parts = ([f"\\caption{{{caption}}}"] if caption else []) + bodies
    return "\n".join(parts)
FIGURE_ENVS = {"figure", "wrapfigure"}


def _base_env(name):
    return name[:-1] if name.endswith("*") else name


class LatexParseTimeout(Exception):
    """pylatexenc exceeded its time budget on pathological input."""


# One paper with deeply nested braces can keep pylatexenc grinding for hours
# (observed: a single file stalled the whole pipeline for 11.5h at ~70% CPU,
# with systemd none the wiser because the process was alive). pylatexenc is
# pure Python, so SIGALRM lands between bytecodes and does interrupt it.
LATEX_PARSE_TIMEOUT_SEC = 90


def _parse_deadline(seconds):
    """Context manager arming SIGALRM; no-op off the main thread."""
    import contextlib
    import signal
    import threading

    @contextlib.contextmanager
    def _ctx():
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        def _fire(_signum, _frame):
            raise LatexParseTimeout(f"latex parse exceeded {seconds}s")

        old = signal.signal(signal.SIGALRM, _fire)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    return _ctx()


def extract_elements(raw_body):
    """Returns list of (element_type, text) in order, using pylatexenc.
    element_type in {prose, equation, algorithm, code, table, figure}.
    Raises on hard pylatexenc failure (caller should catch and fall back)."""
    from pylatexenc.latexwalker import (
        LatexWalker, LatexEnvironmentNode, LatexGroupNode, LatexMathNode,
    )
    from pylatexenc.latex2text import LatexNodes2Text

    converter = LatexNodes2Text(math_mode="verbatim")
    elements = []
    prose_buffer = []

    def flush_prose():
        if not prose_buffer:
            return
        try:
            text = converter.nodelist_to_text(list(prose_buffer)).strip()
        except Exception:
            text = ""
        prose_buffer.clear()
        if text:
            elements.append(("prose", text))

    def walk(nodelist):
        if not nodelist:
            return
        for node in nodelist:
            if node is None:
                continue
            if isinstance(node, LatexEnvironmentNode):
                base = _base_env(node.environmentname or "")
                try:
                    raw = node.latex_verbatim()
                except Exception:
                    raw = ""
                if base in EQUATION_ENVS:
                    flush_prose(); elements.append(("equation", raw))
                elif base in ALGORITHM_ENVS:
                    flush_prose(); elements.append(("algorithm", raw))
                elif base in CODE_ENVS:
                    flush_prose(); elements.append(("code", raw))
                elif base in TABLE_ENVS:
                    flush_prose(); elements.append(("table", table_essence(raw)))
                elif base in FIGURE_ENVS:
                    flush_prose(); elements.append(("figure", raw))
                else:
                    walk(node.nodelist)
            elif isinstance(node, LatexMathNode):
                displaytype = getattr(node, "displaytype", "inline")
                if displaytype == "display":
                    try:
                        raw = node.latex_verbatim()
                    except Exception:
                        raw = ""
                    flush_prose(); elements.append(("equation", raw))
                else:
                    prose_buffer.append(node)
            elif isinstance(node, LatexGroupNode):
                walk(node.nodelist)
            else:
                prose_buffer.append(node)

    with _parse_deadline(LATEX_PARSE_TIMEOUT_SEC):
        walker = LatexWalker(raw_body)
        nodelist, _, _ = walker.get_latex_nodes()
        walk(nodelist)
    flush_prose()
    return elements


# ---------------------------------------------------------------------------
# Top-level: build ordered chunk dicts for a whole paper
# ---------------------------------------------------------------------------

MATH_INLINE_RE = re.compile(r"\$[^$]+\$")


def build_chunks(raw_text, embed_fn=None):
    """Main entry point. Returns (chunks, extractor_mode) where extractor_mode
    is 'pylatexenc' or 'legacy'. chunks: list of dicts with keys
    chunk_index, section_type, section_title, element_type, has_math,
    has_algorithm, text."""
    text = strip_comments(raw_text)
    sections = split_sections_raw(text)
    total = len(sections)

    chunks = []
    idx = 0
    mode = "pylatexenc"
    for s_idx, (title, body) in enumerate(sections):
        section_type, _layer = classify_section(title, s_idx, total, embed_fn=embed_fn)
        try:
            elements = extract_elements(body)
        except Exception:
            mode = "legacy"
            elements = [("prose", body)]

        for etype, etext in elements:
            if not etext or not etext.strip():
                continue
            # coarse split first (paragraph grouping for prose, whole element
            # for everything else), then split_long_text enforces the hard
            # embed-model size limit on every resulting piece.
            pieces = chunk_paragraphs(etext) if etype == "prose" else [etext.strip()]
            for piece in pieces:
                if not piece.strip():
                    continue
                subpieces = [sp for sp in split_long_text(piece) if sp.strip()]
                part_total = len(subpieces)
                for part_index, sub in enumerate(subpieces):
                    chunks.append({
                        "chunk_index": idx,
                        "section_type": section_type,
                        "section_title": title,
                        "element_type": etype,
                        "has_math": etype == "equation" or (etype == "prose" and bool(MATH_INLINE_RE.search(sub))),
                        "has_algorithm": etype == "algorithm",
                        "part_index": part_index,
                        "part_total": part_total,
                        "text": sub,
                    })
                    idx += 1

    if not chunks and text.strip():
        # last-resort: whole doc as prose, no structure found
        for piece in chunk_paragraphs(text):
            subpieces = [sp for sp in split_long_text(piece) if sp.strip()]
            part_total = len(subpieces)
            for part_index, sub in enumerate(subpieces):
                chunks.append({
                    "chunk_index": idx,
                    "section_type": "other",
                    "section_title": "full text",
                    "element_type": "prose",
                    "has_math": bool(MATH_INLINE_RE.search(sub)),
                    "has_algorithm": False,
                    "part_index": part_index,
                    "part_total": part_total,
                    "text": sub,
                })
                idx += 1
        mode = "legacy"

    return chunks, mode

# ---------------------------------------------------------------------------
# Code repositories
# ---------------------------------------------------------------------------
# About 60% of papers link their implementation, and for a practitioner that
# link is half the value of the paper. Sources are already cached, so this is
# a pure re-read: no network, no re-embedding.
_REPO_RE = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(github\.com|gitlab\.com|huggingface\.co|bitbucket\.org)/"
    r"([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)",
    re.IGNORECASE,
)
# LaTeX line-wrapping and trailing punctuation leak into naive URL grabs
_REPO_TRAILING = ".,;:)}]|\\"


def extract_repo_urls(raw_source, limit=5):
    """Return de-duplicated repository URLs mentioned in a paper's source."""
    if not raw_source:
        return []
    seen, out = set(), []
    for host, owner, repo in _REPO_RE.findall(raw_source):
        repo = repo.rstrip(_REPO_TRAILING)
        if not repo or repo.lower() in ("blob", "tree", "issues"):
            continue
        url = f"https://{host.lower()}/{owner}/{repo}"
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Markdown chunking (protocol specifications: EIP, Solana SIMD)
# ---------------------------------------------------------------------------
# Specs are markdown, not LaTeX, so pylatexenc would flatten them into one
# blob. Their headings are also more informative than a paper's: "Specification"
# is the implementable part, "Security Considerations" is the caveats section.
_MD_HEADING_RE = re.compile(r"^\s{0,3}(#{1,4})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_MD_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.DOTALL)

# Spec headings map onto the paper taxonomy fairly directly.
_MD_SECTION_MAP = [
    ("method", re.compile(r"specification|reference implementation|implementation|interface|api", re.I)),
    ("limitations", re.compile(r"security consideration|privacy consideration|risk|caveat", re.I)),
    ("analysis", re.compile(r"rationale|backwards compatibility|discussion|alternative", re.I)),
    ("experiments", re.compile(r"test case|benchmark|evaluation|measurement", re.I)),
    ("introduction", re.compile(r"abstract|motivation|summary|simple summary|overview", re.I)),
    ("appendix", re.compile(r"appendix|copyright|reference|citation", re.I)),
]


def classify_markdown_heading(title):
    for section_type, pattern in _MD_SECTION_MAP:
        if pattern.search(title or ""):
            return section_type
    return "other"


def build_chunks_markdown(text):
    """Chunk a markdown spec by heading, keeping fenced code as its own element.

    Returns (chunks, mode) with the same shape build_chunks() produces, so the
    rest of the pipeline treats specs and papers identically.
    """
    body = text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4:]

    sections = []
    matches = list(_MD_HEADING_RE.finditer(body))
    if not matches:
        sections.append(("Document", body))
    else:
        if matches[0].start() > 0:
            head = body[:matches[0].start()].strip()
            if head:
                sections.append(("Abstract", head))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            sections.append((m.group(2).strip(), body[m.end():end]))

    chunks = []
    index = 0
    for section_title, section_body in sections:
        section_type = classify_markdown_heading(section_title)

        # fenced code first, so a long listing is never split mid-block
        code_blocks = []
        def _stash(match):
            code_blocks.append(match.group(0))
            return "\n"
        prose = _MD_FENCE_RE.sub(_stash, section_body)

        for piece in split_long_text(prose):
            if not piece.strip():
                continue
            chunks.append({
                "chunk_index": index, "section_type": section_type,
                "section_title": section_title, "element_type": "prose",
                "has_math": False, "has_algorithm": False,
                "part_index": 0, "part_total": 1, "text": piece.strip(),
            })
            index += 1

        for block in code_blocks:
            parts = split_long_text(block)
            for pi, piece in enumerate(parts):
                if not piece.strip():
                    continue
                chunks.append({
                    "chunk_index": index, "section_type": section_type,
                    "section_title": section_title, "element_type": "code",
                    "has_math": False, "has_algorithm": True,
                    "part_index": pi, "part_total": len(parts), "text": piece.strip(),
                })
                index += 1

    return chunks, "markdown"
