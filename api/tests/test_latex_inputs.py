import ast
import gzip
import hashlib
import io
import json
import logging
import os
import pathlib
import posixpath
import re
import tarfile
import tempfile
import types
import unittest


def load_service():
    source = pathlib.Path(__file__).resolve().parents[2] / "pipeline" / "service.py"
    names = {"safe_id", "latex_cache_path", "read_latex_cache", "write_latex_cache",
             "read_latex_bibliography", "_is_commented", "assemble_latex_source", "fetch_latex_source",
             "_bibliography_path", "_bibliography_files", "_bibliography_json",
             "_bibliography_unique_object", "_latex_source_sha256"}
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body
             if (isinstance(node, ast.FunctionDef) and node.name in names)
             or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name)
                 and (t.id in {"_INC_RE", "LATEX_MAX_INCLUDE_DEPTH"} or t.id.startswith("LATEX_BIB_"))
                 for t in node.targets))]
    module = types.ModuleType("latex_fixture")
    module.__dict__.update(globals(), Path=pathlib.Path, log=logging.getLogger("latex_fixture"),
                           requests=types.SimpleNamespace(Session=object, RequestException=OSError))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), module.__dict__)
    return module


service = load_service()

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

    def test_bibliography_is_separate_and_main_relative(self):
        source = DOC % "\\input{section}\n\\bibliography{refs}"
        files = [("paper/main.tex", source), ("paper/section.tex", "BODY"),
                 ("paper/refs.bib", "LOCAL_BIB"), ("refs.bib", "ROOT_BIB"),
                 ("paper/main.bbl", "COMPILED_BIB"), ("unused.bib", "UNUSED")]
        sink = []
        text = service.assemble_latex_source("fixture", files, bibliography_sink=sink)
        self.assertEqual(text, source.replace("\\input{section}", "BODY"))
        self.assertEqual([f["path"] for f in sink], ["paper/refs.bib", "paper/main.bbl"])
        self.assertTrue(text.endswith("\\end{document}\n"))
        self.assertNotIn("BIB", text)

    def test_expanded_declarations_root_and_unique_basename(self):
        files = [("paper/main.tex", DOC % "\\input{section}"),
                 ("paper/section.tex", "\\addbibresource[location=local]{refs.bib}\\bibliography{other}"),
                 ("refs.bib", "ROOT"), ("deep/other.bib", "UNIQUE"),
                 ("deep/other.bbl", "COMPILED"), ("unused.tex", "\\bibliography{unused}"),
                 ("unused.bib", "UNUSED")]
        sink = []
        service.assemble_latex_source("fixture", files, bibliography_sink=sink)
        self.assertEqual([f["text"] for f in sink], ["ROOT", "UNIQUE", "COMPILED"])

    def test_ambiguous_duplicate_and_unsafe_resources_fail_closed(self):
        for resources, ref in [([("a/refs.bib", "A"), ("b/refs.bib", "B")], "refs"),
                               ([("refs.bib", "A"), ("./refs.bib", "B")], "refs"),
                               ([("../refs.bib", "ESCAPE")], "refs"),
                               ([("refs.bib", "A")], "/refs"),
                               ([("refs.bib", "A")], "../../refs")]:
            with self.subTest(ref=ref, resources=resources), self.assertLogs(service.log, level="WARNING"):
                sink = []
                service.assemble_latex_source("fixture", [("main.tex", DOC % ("\\bibliography{%s}" % ref))]
                                              + resources, bibliography_sink=sink)
                self.assertEqual(sink, [])

    def test_comments_and_inlined_bibitems_do_not_require_artifacts(self):
        for body in ("%\\bibliography{refs}\nBODY", "\\bibitem{x} INLINE\\bibliography{refs}"):
            sink = []
            source = DOC % body
            self.assertEqual(service.assemble_latex_source("fixture", [("main.tex", source),
                             ("refs.bib", "EXTRA"), ("main.bbl", "EXTRA")], bibliography_sink=sink), source)
            self.assertEqual(sink, [])

    def test_only_chosen_main_selects_bibliography(self):
        sink = []
        files = [("short.tex", DOC % "\\bibliography{unused}"),
                 ("main.tex", DOC % ("BODY" * 100 + "\\bibliography{refs,refs}")),
                 ("refs.bib", "KEEP"), ("unused.bib", "DROP")]
        service.assemble_latex_source("fixture", files, bibliography_sink=sink)
        self.assertEqual(sink, [{"path": "refs.bib", "text": "KEEP"}])

    def test_parent_reference_and_main_bbl_root_priority(self):
        sink = []
        files = [("paper/main.tex", DOC % "\\bibliography{../refs}"),
                 ("refs.bib", "ROOT_BIB"), ("main.bbl", "ROOT_BBL"), ("other/main.bbl", "OTHER")]
        service.assemble_latex_source("fixture", files, bibliography_sink=sink)
        self.assertEqual([f["text"] for f in sink], ["ROOT_BIB", "ROOT_BBL"])


if __name__ == "__main__":
    unittest.main()
