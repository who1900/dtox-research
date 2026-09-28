import pathlib
import sys
import types
import unittest

# Same import fixup as test_spec_niche.py: pipeline/ on sys.path, fcntl stubbed
# on non-POSIX hosts. Test-only; nothing under pipeline/ is changed.
_PIPELINE_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "pipeline")
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

if "fcntl" not in sys.modules:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fcntl")
        stub.flock = lambda *a, **kw: None
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        sys.modules["fcntl"] = stub

from pipeline import service  # noqa: E402

DOC = "\\documentclass{article}\n\\begin{document}\n%s\n\\end{document}\n"


def build(files):
    return service.assemble_latex_source("0000.00000", files)


class AssembleLatexTests(unittest.TestCase):
    def test_nested_folder(self):
        text = build([
            ("main.tex", DOC % "\\input{sections/intro}"),
            ("sections/intro.tex", "INTRO_BODY"),
        ])
        self.assertIn("INTRO_BODY", text)
        self.assertNotIn("\\input", text)

    def test_deep_recursion(self):
        files = [("main.tex", DOC % "\\input{a/l1}")]
        for i in range(1, 6):
            files.append((f"a/l{i}.tex", f"L{i} \\input{{l{i+1}}}"))
        files.append(("a/l6.tex", "L6"))
        self.assertIn("L6", build(files))  # depth 6, old limit was 2

    def test_extension_and_dot_slash(self):
        text = build([
            ("./main.tex", DOC % "\\input{./sec/a.tex} \\input{sec/b}"),
            ("./sec/a.tex", "AAA"),
            ("./sec/b.tex", "BBB"),
        ])
        self.assertIn("AAA", text)
        self.assertIn("BBB", text)

    def test_input_without_braces(self):
        text = build([
            ("main.tex", DOC % "before\n\\input body\nafter"),
            ("body.tex", "BODY"),
        ])
        self.assertIn("BODY", text)
        self.assertIn("after", text)

    def test_extensionless_file(self):
        text = build([
            ("main.tex", DOC % "\\input{body}"),
            ("body", "PLAIN_BODY"),
        ])
        self.assertIn("PLAIN_BODY", text)

    def test_include_and_subfile(self):
        text = build([
            ("main.tex", DOC % "\\include{chap1}\n\\subfile{sub/chap2}"),
            ("chap1.tex", "C1"),
            ("sub/chap2.tex", "\\documentclass[../main.tex]{subfiles}\\begin{document}C2\\end{document}"),
        ])
        self.assertIn("C1", text)
        self.assertIn("C2", text)

    def test_import_and_subimport(self):
        text = build([
            ("main.tex", DOC % "\\import{parts/}{intro}\n\\subimport{parts}{outro}"),
            ("parts/intro.tex", "IN"),
            ("parts/outro.tex", "OUT"),
        ])
        self.assertIn("IN", text)
        self.assertIn("OUT", text)

    def test_same_basename_different_dirs(self):
        text = build([
            ("main.tex", DOC % "\\input{a/intro}\n\\input{b/intro}"),
            ("a/intro.tex", "FROM_A"),
            ("b/intro.tex", "FROM_B"),
        ])
        self.assertIn("FROM_A", text)
        self.assertIn("FROM_B", text)

    def test_relative_sibling_beats_root_name(self):
        text = build([
            ("main.tex", DOC % "\\input{a/x}"),
            ("a/x.tex", "\\input{intro}"),
            ("a/intro.tex", "LOCAL"),
            ("intro.tex", "ROOT"),
        ])
        self.assertIn("LOCAL", text)
        self.assertNotIn("ROOT", text)

    def test_basename_fallback_only_if_unique(self):
        ok = build([("main.tex", DOC % "\\input{intro}"), ("deep/intro.tex", "UNIQ")])
        self.assertIn("UNIQ", ok)
        with self.assertLogs(service.log, level="WARNING"):
            amb = build([
                ("main.tex", DOC % "\\input{intro}"),
                ("a/intro.tex", "A"),
                ("b/intro.tex", "B"),
            ])
        self.assertNotIn("\\input", amb)
        self.assertNotRegex(amb, r"\bA\b|\bB\b")

    def test_cycle_terminates(self):
        with self.assertLogs(service.log, level="WARNING"):
            text = build([
                ("main.tex", DOC % "\\input{a}"),
                ("a.tex", "AA \\input{b}"),
                ("b.tex", "BB \\input{a}"),
            ])
        self.assertIn("AA", text)
        self.assertIn("BB", text)

    def test_unresolved_logged_and_emptied(self):
        with self.assertLogs(service.log, level="WARNING") as cm:
            text = build([("main.tex", DOC % "x \\input{missing} y")])
        self.assertIn("missing", "\n".join(cm.output))
        self.assertIn("x  y", text)

    def test_commented_include_is_not_expanded(self):
        text = build([
            ("main.tex", DOC % "%\\input{old}\n\\input{new}"),
            ("old.tex", "OLD"),
            ("new.tex", "NEW"),
        ])
        self.assertIn("NEW", text)
        self.assertNotIn("OLD", text)

    def test_includegraphics_not_treated_as_include(self):
        body = "\\includegraphics{fig} \\inputencoding{utf8}"
        text = build([("main.tex", DOC % body), ("fig.tex", "FIG")])
        self.assertIn(body, text)
        self.assertNotIn("FIG", text)

    def test_main_is_file_including_most_others(self):
        appendix = DOC % "APPENDIX_ONLY_" + "x" * 5000
        text = build([
            ("appendix/standalone.tex", appendix),
            ("paper.tex", DOC % "\\input{s1}\\input{s2}"),
            ("s1.tex", "S1"),
            ("s2.tex", "S2"),
        ])
        self.assertIn("S1", text)
        self.assertNotIn("APPENDIX_ONLY_", text)

    def test_main_tie_picks_longest(self):
        text = build([
            ("short.tex", DOC % "short"),
            ("long.tex", DOC % ("long" * 100)),
        ])
        self.assertIn("longlong", text)

    def test_main_requires_begin_document_when_available(self):
        text = build([
            ("macros.tex", "\\documentclass{article}\n\\newcommand{\\x}{y}" + "z" * 500),
            ("paper.tex", DOC % "REAL"),
        ])
        self.assertIn("REAL", text)

    def test_no_documentclass_falls_back_to_longest(self):
        text = build([("a.tex", "short"), ("b.tex", "a much longer body")])
        self.assertEqual(text, "a much longer body")

    def test_article_without_input_unchanged(self):
        src = DOC % "\\section{Intro} hello \\includegraphics{f.png} \\cite{x}"
        self.assertEqual(build([("paper.tex", src)]), src)


if __name__ == "__main__":
    unittest.main()
