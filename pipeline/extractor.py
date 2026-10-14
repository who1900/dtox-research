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

if __package__:
    from .pdf_memory import NATIVE_JOB_SLOTS
else:
    from pdf_memory import NATIVE_JOB_SLOTS


class PDFConversionError(RuntimeError):
    """Retryable conversion failure; never evidence of abstract-only fulltext."""

    def __init__(self, reason, returncode=None):
        self.reason = reason
        self.returncode = returncode
        super().__init__(reason if returncode is None else f"{reason} (exit={returncode})")


_PDF_CONVERSION_SCRIPT = """
import pathlib
import sys
import json
sys.path.insert(0, sys.argv[3])
sys.path.insert(0, sys.argv[4])
from pdf_memory import bind_parent_lifetime, verify_native_build
bind_parent_lifetime(int(sys.argv[5]))
try:
    verify_native_build(sys.argv[4])
except Exception:
    raise SystemExit(86)
import pymupdf4llm
unicode_diagnostics = None
if sys.argv[6] == "1":
    import fitz
    from pdf_unicode import normalize_pdf_bytes
    raw = pathlib.Path(sys.argv[1]).read_bytes()
    derived, unicode_diagnostics = normalize_pdf_bytes(raw)
    document = fitz.open(stream=derived, filetype="pdf")
    text = pymupdf4llm.to_markdown(document, show_progress=False)
    page_count = document.page_count
    page_text_chars = sum(len(page.get_text("text")) for page in document)
    document.close()
else:
    text = pymupdf4llm.to_markdown(sys.argv[1], show_progress=False)
    page_count = page_text_chars = None
    if sys.argv[8] == "1":
        import fitz
        with fitz.open(sys.argv[1]) as document:
            page_count = document.page_count
            page_text_chars = sum(len(page.get_text("text")) for page in document)
if not isinstance(text, str):
    raise TypeError("PDF converter did not return Markdown")
with pathlib.Path(sys.argv[2]).open("w", encoding="utf-8", newline="") as output:
    output.write(text)
if sys.argv[8] == "1":
    metadata = {"page_count": page_count, "page_text_chars": page_text_chars,
                "markdown_chars": len(text), "markdown_replacement_characters": text.count("\ufffd"),
                "unicode_normalization": unicode_diagnostics}
    with pathlib.Path(sys.argv[7]).open("w", encoding="utf-8", newline="") as output:
        json.dump(metadata, output, ensure_ascii=True, sort_keys=True)
"""


def convert_pdf_isolated(pdf_path, timeout, *, native_build=None,
                         normalize_unicode=False, return_diagnostics=False):
    """Convert one PDF in a fresh interpreter, retaining layout/OCR defaults.

    No service import or fork of its threads; only a successful child exit may
    publish the complete UTF-8 file. IPC pipes do not buffer Markdown or logs.
    The caller supplies its existing fetch timeout, independently of download.
    POSIX conversion groups are killed/reaped even on timeout or interruption;
    no page, character, memory, model or feature limits are introduced. Native
    scratch requires the source-verified row-block dependency build. Native
    jobs share one slot; download workers and all document features remain.
    """
    import math
    import os
    import signal
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("PDF conversion timeout must be finite and positive")
    if native_build is None:
        native_build = Path(__file__).resolve().parent / "native_build/dtox-directional-nn-row-block-v1"
    native_build = str(Path(native_build).resolve())
    try:
        pdf_path = str(Path(pdf_path).resolve())
        with NATIVE_JOB_SLOTS, tempfile.TemporaryDirectory(prefix="dtox-pdf-") as directory:
            output = Path(directory) / "fulltext.md"
            metadata = Path(directory) / "metadata.json"
            process = None
            try:
                process = subprocess.Popen(
                    [sys.executable, "-c", _PDF_CONVERSION_SCRIPT, pdf_path, str(output),
                     str(Path(__file__).resolve().parent), native_build, str(os.getpid()),
                     "1" if normalize_unicode else "0", str(metadata),
                     "1" if return_diagnostics else "0"],
                    # The fork is selected only in the fresh native interpreter.
                    cwd=directory, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, start_new_session=(os.name == "posix"),
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
                )
                try:
                    returncode = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    raise PDFConversionError("conversion_timeout") from exc
                if returncode:
                    if returncode == 86:
                        raise PDFConversionError("converter_build_unverified", returncode)
                    raise PDFConversionError("conversion_failed", returncode)
                try:
                    with output.open("r", encoding="utf-8", newline="") as markdown:
                        text = markdown.read()
                    if return_diagnostics:
                        import json
                        details = json.loads(metadata.read_text(encoding="utf-8"))
                        return text, details
                    return text
                except (OSError, UnicodeError) as exc:
                    raise PDFConversionError("invalid_output") from exc
            finally:
                if process is not None:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    elif process.poll() is None:
                        process.kill()
                    process.wait()
    except OSError as exc:
        raise PDFConversionError("conversion_io_failure") from exc


try:
    from boilerplate import BOILERPLATE_FILTER, is_boilerplate_chunk
except ImportError:
    try:
        from api.boilerplate import BOILERPLATE_FILTER, is_boilerplate_chunk
    except ImportError:
        BOILERPLATE_FILTER = None
        is_boilerplate_chunk = None

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
    if is_boilerplate_chunk is None:
        raise RuntimeError("index eligibility requires the boilerplate policy module")
    if (chunk.get("section_type") in SKIP_SECTION_TYPES
            and chunk.get("element_type") in SKIP_ELEMENT_TYPES):
        return False
    # preamble / author block / venue header / checklist / references
    if BOILERPLATE_FILTER and is_boilerplate_chunk(
            chunk.get("text"), chunk.get("section_title"),
            chunk.get("section_type"), chunk.get("element_type")):
        return False
    return True

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

_LATEX_TOKEN_RE = re.compile(
    r"(?P<literal>\\begin\{(?P<env>verbatim\*?|Verbatim\*?|lstlisting\*?|minted\*?)\}"
    r"[\s\S]*?(?:\\end\{(?P=env)\}|\Z)|"
    r"\\(?:verb\*?|Verb\*?|lstinline\*?(?:\[[^\]\n]*\])?)"
    r"(?![A-Za-z])(?P<delimiter>[^\s])(?:(?!(?P=delimiter))[^\n])*(?P=delimiter))|"
    r"\\[^\n]|(?P<comment>%[^\n]*)")


def _literal_spans(text):
    return [match for match in _LATEX_TOKEN_RE.finditer(text) if match.group("literal")]


def strip_comments(text):
    out = []
    pos = 0
    for match in _LATEX_TOKEN_RE.finditer(text):
        if match.group("comment"):
            out.append(text[pos:match.start()])
            pos = match.end()
    out.append(text[pos:])
    return "".join(out)


SECTION_RE = re.compile(r"\\(section|subsection)\*?\{([^}]*)\}")


def split_sections_raw(text):
    """Returns list of (title, raw_body). Falls back to whole doc as one section."""
    literals = iter(_literal_spans(text))
    literal = next(literals, None)
    matches = []
    for match in SECTION_RE.finditer(text):
        while literal is not None and literal.end() <= match.start():
            literal = next(literals, None)
        if literal is None or match.start() < literal.start():
            matches.append(match)
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


_LATEX_ENV_TOKEN_RE = re.compile(
    r"\\(?P<action>begin|end)\{(?P<name>[^{}\s]+)\}|\\[^\n]|%[^\n]*")


def _literal_container_spans(raw_body, literals):
    stack = []
    spans = []
    literal_index = 0
    pos = 0
    env_types = [(EQUATION_ENVS, "equation"), (ALGORITHM_ENVS, "algorithm"),
                 (TABLE_ENVS, "table"), (FIGURE_ENVS, "figure")]
    while True:
        match = _LATEX_ENV_TOKEN_RE.search(raw_body, pos)
        if match is None:
            break
        while literal_index < len(literals) and literals[literal_index].end() <= match.start():
            literal_index += 1
        if literal_index < len(literals) and literals[literal_index].start() <= match.start():
            pos = literals[literal_index].end()
            continue
        pos = match.end()
        action = match.group("action")
        if action is None:
            continue
        name = match.group("name")
        if action == "begin":
            element_type = next((kind for names, kind in env_types if _base_env(name) in names), None)
            stack.append((name, match.start(), literal_index, element_type))
            continue
        for index in range(len(stack) - 1, -1, -1):
            if stack[index][0] == name:
                _, start, first_literal, element_type = stack[index]
                del stack[index:]
                if element_type is not None and literal_index > first_literal:
                    spans.append((start, match.end(), element_type))
                break
    return spans


def extract_elements(raw_body, diagnostics=None):
    literals = _literal_spans(raw_body)
    if not literals:
        if diagnostics is None:
            return _extract_elements_latex(raw_body)
        return _extract_elements_latex(raw_body, diagnostics=diagnostics)
    protected = [(literal.start(), literal.end(), "code") for literal in literals]
    protected.extend(_literal_container_spans(raw_body, literals))
    protected.sort(key=lambda span: (span[0], -span[1]))
    elements = []
    pos = 0
    for start, end, element_type in protected:
        if start < pos:
            continue
        if start > pos:
            if diagnostics is None:
                elements.extend(_extract_elements_latex(raw_body[pos:start]))
            else:
                elements.extend(_extract_elements_latex(raw_body[pos:start], diagnostics=diagnostics))
        raw = raw_body[start:end]
        elements.append((element_type, table_essence(raw) if element_type == "table" else raw))
        pos = end
    if pos < len(raw_body):
        if diagnostics is None:
            elements.extend(_extract_elements_latex(raw_body[pos:]))
        else:
            elements.extend(_extract_elements_latex(raw_body[pos:], diagnostics=diagnostics))
    return elements


def _extract_elements_latex(raw_body, diagnostics=None):
    """Returns list of (element_type, text) in order, using pylatexenc.
    element_type in {prose, equation, algorithm, code, table, figure}.
    Raises on hard pylatexenc failure (caller should catch and fall back)."""
    from pylatexenc.latexwalker import (
        LatexWalker, LatexEnvironmentNode, LatexGroupNode, LatexMathNode, LatexMacroNode,
    )
    from pylatexenc.latex2text import LatexNodes2Text

    converter = LatexNodes2Text(math_mode="verbatim")
    elements = []
    prose_buffer = []

    def flush_prose():
        if not prose_buffer:
            return
        nodes = list(prose_buffer)
        prose_buffer.clear()

        def note_recovery():
            if diagnostics is not None:
                diagnostics["recovered_nodes"] = diagnostics.get("recovered_nodes", 0) + 1

        def source_span(node, following=()):
            start = getattr(node, "pos", None)
            length = getattr(node, "len", None)
            end = start + length if isinstance(start, int) and isinstance(length, int) else None
            if isinstance(node, LatexMacroNode) and isinstance(end, int):
                argd = getattr(node, "nodeargd", None)
                for argument in getattr(argd, "argnlist", ()) or ():
                    arg_pos = getattr(argument, "pos", None)
                    arg_len = getattr(argument, "len", None)
                    if isinstance(arg_pos, int) and isinstance(arg_len, int) and arg_pos >= end:
                        end = max(end, arg_pos + arg_len)
            for argument in following:
                arg_pos = getattr(argument, "pos", None)
                arg_len = getattr(argument, "len", None)
                if isinstance(arg_pos, int) and isinstance(arg_len, int) and arg_pos >= end:
                    if not raw_body[end:arg_pos].strip():
                        end = arg_pos + arg_len
            if isinstance(start, int) and isinstance(end, int) and end > start:
                return raw_body[start:end]
            try:
                raw = node.latex_verbatim()
            except Exception:
                raw = ""
            if raw:
                return raw
            if isinstance(start, int) and isinstance(length, int):
                return raw_body[start:start + length]
            return ""

        def convert_individually():
            pieces = []
            index = 0
            while index < len(nodes):
                node = nodes[index]
                unknown_macro = (
                    isinstance(node, LatexMacroNode)
                    and converter.latex_context.get_macro_spec(node.macroname) is None
                )
                failed = unknown_macro
                if unknown_macro:
                    note_recovery()
                else:
                    try:
                        pieces.append(converter.nodelist_to_text([node]))
                    except Exception:
                        failed = True
                        note_recovery()
                if failed:
                    max_groups = 2 if isinstance(node, LatexMacroNode) else 0
                    following = []
                    next_index = index + 1
                    end = getattr(node, "pos", 0) + getattr(node, "len", 0)
                    while (len(following) < max_groups and next_index < len(nodes)
                           and isinstance(nodes[next_index], LatexGroupNode)):
                        group = nodes[next_index]
                        group_pos = getattr(group, "pos", None)
                        if (not isinstance(group_pos, int) or group_pos < end
                                or raw_body[end:group_pos].strip()):
                            break
                        following.append(group)
                        end = group_pos + getattr(group, "len", 0)
                        next_index += 1
                    pieces.append(source_span(node, following))
                    index = next_index
                else:
                    index += 1
            return "".join(pieces).strip()

        has_unknown_macro = any(
            isinstance(node, LatexMacroNode)
            and converter.latex_context.get_macro_spec(node.macroname) is None
            for node in nodes
        )
        if has_unknown_macro:
            text = convert_individually()
        else:
            try:
                text = converter.nodelist_to_text(nodes).strip()
            except Exception:
                note_recovery()
                text = convert_individually()
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
                previous = prose_buffer[-1] if prose_buffer else None
                previous_end = (getattr(previous, "pos", -1) + getattr(previous, "len", 0)
                                if previous is not None else -1)
                if isinstance(previous, LatexMacroNode) and previous_end == getattr(node, "pos", None):
                    try:
                        converter.nodelist_to_text([previous])
                    except Exception:
                        prose_buffer.append(node)
                    else:
                        walk(node.nodelist)
                else:
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


def build_chunks(raw_text, embed_fn=None, section_sink=None, element_sink=None):
    """Main entry point. Returns (chunks, extractor_mode) where extractor_mode
    is 'pylatexenc' or 'legacy'. chunks: list of dicts with keys
    chunk_index, section_type, section_title, element_type, has_math,
    has_algorithm, text. Optional section_sink receives verified unsplit sections."""
    text = strip_comments(raw_text)
    sections = split_sections_raw(text)
    total = len(sections)

    chunks = []
    canonical_sections = [] if section_sink is not None else None
    canonical_elements = [] if element_sink is not None else None
    idx = 0
    mode = "pylatexenc"
    diagnostics = {"recovered_nodes": 0}
    for s_idx, (title, body) in enumerate(sections):
        section_type, _layer = classify_section(title, s_idx, total, embed_fn=embed_fn)
        section_elements = [] if section_sink is not None else None
        try:
            elements = extract_elements(body, diagnostics=diagnostics)
        except Exception:
            mode = "legacy"
            elements = [("prose", body)]

        for etype, etext in elements:
            if not etext or not etext.strip():
                continue
            if canonical_elements is not None:
                canonical_elements.append({"section_title": title, "section_type": section_type,
                                           "element_type": etype, "text": etext.strip() if etype == "prose" else etext})
            if section_elements is not None:
                section_elements.append(etext.strip() if etype == "prose" else etext)
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
        if section_elements:
            canonical_sections.append({"section_title": title, "section_type": section_type,
                                       "text": "\n\n".join(section_elements)})

    if mode == "pylatexenc" and diagnostics["recovered_nodes"]:
        mode = "pylatexenc_recovered"

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

    if section_sink is not None and mode != "legacy":
        section_sink.extend(canonical_sections)
    if element_sink is not None and mode != "legacy":
        element_sink.extend(canonical_elements)
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
_MD_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*\r?$", re.MULTILINE)
_MD_FENCE_START_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n", re.MULTILINE)


def _markdown_fences(text):
    spans = []
    pos = 0
    while True:
        start = _MD_FENCE_START_RE.search(text, pos)
        if start is None:
            break
        fence = start.group(1)
        end_re = re.compile(r"^[ \t]{0,3}" + re.escape(fence[0])
                            + "{" + str(len(fence)) + r",}[ \t]*\r?$", re.MULTILINE)
        end = end_re.search(text, start.end())
        pos = end.end() if end else len(text)
        spans.append((start.start(), pos))
    return spans

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


def classify_scientific_markdown_heading(title):
    title = (title or "").strip()
    # Preserve the existing Markdown contract for abstracts. References and
    # acknowledgements stay in the appendix channel.
    if re.fullmatch(r"abstract", title, re.I):
        return "introduction"
    if re.fullmatch(r"(?:references|bibliography|acknowledg(?:e)?ments?)", title, re.I):
        return "appendix"
    if re.fullmatch(r"background", title, re.I):
        return "related_work"
    for section_type, pattern in LAYER1_PATTERNS:
        if pattern.search(title):
            return section_type
    return "other"


def _clean_scientific_heading(title):
    # PDF converters often retain printed section numbers and emphasis markup.
    title = (title or "").strip()
    for marker in ("**", "__", "*", "_"):
        if title.startswith(marker) and title.endswith(marker) and len(title) > 2 * len(marker):
            title = title[len(marker):-len(marker)].strip()
            break
    return re.sub(r"^\s*\d+(?:\.\d+)*[.)]?\s+", "", title).strip()


def _markdown_elements(text):
    """Keep explicit source tables and display math typed, outside code fences."""
    fences = _markdown_fences(text)
    intervals = [(start, end, "code") for start, end in fences]
    patterns = (
        ("equation", re.compile(r"^[ \t]*\$\$[ \t]*\n.*?^[ \t]*\$\$[ \t]*(?=\n|$)", re.M | re.S)),
        ("equation", re.compile(r"\\\[.*?\\\]", re.S)),
        ("table", re.compile(r"<table\b[^>]*>.*?</table\s*>", re.I | re.S)),
        ("table", re.compile(
            r"^[ \t]*\|[^\n]+\|[ \t]*\n"
            r"[ \t]*\|(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*(?:\n|$)"
            r"(?:[ \t]*\|[^\n]+\|[ \t]*(?:\n|$))*", re.M)),
    )
    for kind, pattern in patterns:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < right and end > left for left, right, _ in intervals):
                continue
            intervals.append((start, end, kind))
    position = 0
    for start, end, kind in sorted(intervals):
        if start > position:
            yield "prose", text[position:start]
        yield kind, text[start:end]
        position = end
    if position < len(text):
        yield "prose", text[position:]


def build_chunks_markdown(text, section_sink=None, document_kind="spec", element_sink=None):
    """Chunk Markdown by heading using its paper or specification profile.

    Returns (chunks, mode) with the same shape build_chunks() produces, so the
    rest of the pipeline treats specs and papers identically. Optional
    section_sink receives source-ordered sections before chunk splitting.
    """
    if document_kind not in {"spec", "paper"}:
        raise ValueError("document_kind must be 'spec' or 'paper'")
    body = text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4:]

    sections = []
    fences = _markdown_fences(body)
    fence_index = 0
    matches = []
    for match in _MD_HEADING_RE.finditer(body):
        while fence_index < len(fences) and fences[fence_index][1] <= match.start():
            fence_index += 1
        if fence_index == len(fences) or match.start() < fences[fence_index][0]:
            matches.append(match)
    abstract_index = next((i for i, match in enumerate(matches)
                           if _clean_scientific_heading(match.group(2)).casefold() == "abstract"), None)
    if not matches:
        sections.append(("Document", "other", body))
    else:
        prefix = ""
        if matches[0].start() > 0:
            prefix = body[:matches[0].start()].strip()
            if prefix:
                if document_kind == "paper":
                    sections.append(("Document preamble", "other", prefix))
                else:
                    sections.append(("Abstract", "introduction", prefix))
        stack = []
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            raw_title = m.group(2).strip()
            title = (_clean_scientific_heading(raw_title) if document_kind == "paper" else raw_title)
            section_type = (classify_scientific_markdown_heading(title) if document_kind == "paper"
                            else classify_markdown_heading(title))
            # A converted paper may put its title and author block in headings.
            # Everything preceding an explicit Abstract is front matter, even
            # when the paper title contains words such as Models or Evaluation.
            if document_kind == "paper" and abstract_index is not None and i < abstract_index:
                section_type = "other"
            if stack and (section_type == "other" or stack[-1][1] == "limitations"):
                section_type = stack[-1][1]
            stack.append((level, section_type))
            section_body = body[m.end():end]
            sections.append((title, section_type, section_body))

    chunks = []
    canonical_sections = [] if section_sink is not None else None
    canonical_elements = [] if element_sink is not None else None
    index = 0
    for section_title, section_type, section_body in sections:
        section_elements = [] if section_sink is not None else None
        elements = _markdown_elements(section_body)
        for element_type, element_text in elements:
            if canonical_elements is not None and element_text.strip():
                canonical_elements.append({"section_title": section_title, "section_type": section_type,
                                           "element_type": element_type,
                                           "text": element_text.strip() if element_type == "prose" else element_text})
            if section_elements is not None and element_text.strip():
                section_elements.append(element_text.strip() if element_type == "prose" else element_text)
            parts = [piece for piece in split_long_text(element_text.strip()) if piece.strip()]
            for pi, piece in enumerate(parts):
                if not piece.strip():
                    continue
                chunks.append({
                    "chunk_index": index, "section_type": section_type,
                    "section_title": section_title, "element_type": element_type,
                    "has_math": element_type == "equation" or (element_type in {"prose", "table"} and bool(MATH_INLINE_RE.search(piece))),
                    "has_algorithm": element_type == "code",
                    "part_index": pi, "part_total": len(parts), "text": piece.strip(),
                })
                index += 1
        if section_elements:
            canonical_sections.append({"section_title": section_title, "section_type": section_type,
                                       "text": "\n\n".join(section_elements)})

    if section_sink is not None:
        section_sink.extend(canonical_sections)
    if element_sink is not None:
        element_sink.extend(canonical_elements)
    return chunks, "markdown"
