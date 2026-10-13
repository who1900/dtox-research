"""Offline unsplit section collector preserves the existing chunk contract."""
import contextlib
import importlib.util
import unittest
from unittest.mock import patch

from pipeline import extractor


class SectionSinkTests(unittest.TestCase):
    def test_latex_long_protected_elements_preserve_whitespace_repetition_and_chunks(self):
        code = "  \\begin{verbatim}\n" + "    emit(value)\n" * 220 + "\\end{verbatim}  "
        algorithm = "\\begin{algorithm}\n" + "  repeat step\n" * 220 + "\\end{algorithm}"
        equation = "\\begin{equation}\n  x + x = 2x  \n\\end{equation}"
        elements = [("prose", "Before"), ("code", code), ("algorithm", algorithm),
                    ("equation", equation), ("prose", "After")]
        sink = []
        source = "\\section{Method}\nSource"
        with patch.object(extractor, "extract_elements", return_value=elements) as parse:
            default = extractor.build_chunks(source)
            none = extractor.build_chunks(source, section_sink=None)
            parse.reset_mock()
            collected = extractor.build_chunks(source, section_sink=sink)
        parse.assert_called_once()
        self.assertEqual(default, none)
        self.assertEqual(default, collected)
        self.assertEqual(len(collected), 2)
        self.assertEqual(sink, [{"section_title": "Method", "section_type": "method",
                                "text": "\n\n".join(text for _kind, text in elements)}])
        for kind in ("code", "algorithm"):
            self.assertGreater(len([chunk for chunk in collected[0] if chunk["element_type"] == kind]), 1)
        self.assertEqual([chunk["text"] for chunk in collected[0] if chunk["element_type"] == "equation"],
                         [equation])
        self.assertTrue(all("canonical_text" not in chunk for chunk in collected[0]))

    def test_source_section_order_and_classification_are_unchanged(self):
        sink = []
        source = "\\section{Results}\nFirst\n\\section{Method}\nSecond"
        with patch.object(extractor, "extract_elements", side_effect=lambda body, **kwargs: [("prose", body)]):
            chunks, mode = extractor.build_chunks(source, section_sink=sink)
        self.assertEqual(mode, "pylatexenc")
        self.assertEqual([item["section_title"] for item in sink], ["Results", "Method"])
        self.assertEqual([item["section_type"] for item in sink], ["experiments", "method"])
        self.assertEqual([item["text"] for item in sink], ["First", "Second"])
        self.assertEqual([chunk["chunk_index"] for chunk in chunks], [0, 1])

    def test_legacy_parse_never_exposes_partial_verified_sink(self):
        sentinel = {"text": "existing"}
        sink = [sentinel]
        source = "\\section{Method}\nFirst\n\\section{Results}\nSecond"
        with patch.object(extractor, "extract_elements", side_effect=[[("prose", "First")], ValueError("failed")]):
            chunks, mode = extractor.build_chunks(source, section_sink=sink)
        self.assertEqual(mode, "legacy")
        self.assertTrue(chunks)
        self.assertEqual(sink, [sentinel])

    def test_last_resort_legacy_does_not_fill_sink(self):
        sink = []
        with patch.object(extractor, "extract_elements", return_value=[]):
            chunks, mode = extractor.build_chunks("\\section{Method}\nSome text", section_sink=sink)
        self.assertEqual(mode, "legacy")
        self.assertTrue(chunks)
        self.assertEqual(sink, [])

    def test_recovered_mode_fills_sink_from_same_parse(self):
        sink = []

        def recovered(body, diagnostics):
            diagnostics["recovered_nodes"] += 1
            return [("prose", "Recovered"), ("equation", "x = x")]

        with patch.object(extractor, "extract_elements", side_effect=recovered) as parse:
            _chunks, mode = extractor.build_chunks("\\section{Method}\nText", section_sink=sink)
        parse.assert_called_once()
        self.assertEqual(mode, "pylatexenc_recovered")
        self.assertEqual(sink[0]["text"], "Recovered\n\nx = x")

    def test_markdown_unsplit_code_keeps_internal_whitespace_and_repetition(self):
        code = "```python\n" + "  emit(value)\n" * 220 + "```"
        source = "# Method\nBefore\n\n" + code + "\n\nAfter\n# Results\nOutcome"
        sink = []
        default = extractor.build_chunks_markdown(source)
        collected = extractor.build_chunks_markdown(source, section_sink=sink)
        self.assertEqual(default, collected)
        self.assertEqual(default, extractor.build_chunks_markdown(source, section_sink=None))
        self.assertEqual([section["section_title"] for section in sink], ["Method", "Results"])
        self.assertEqual(sink[0]["text"], "Before\n\n" + code + "\n\nAfter")
        self.assertEqual(sink[1]["text"], "Outcome")
        self.assertGreater(len([chunk for chunk in collected[0] if chunk["element_type"] == "code"]), 1)

    def test_real_protected_latex_path_collects_raw_elements(self):
        code = "\\begin{verbatim}\n" + "  x % 2\n" * 240 + "\\end{verbatim}"
        algorithm = "\\begin{algorithm}\n" + "  step\n" * 350 + "\\end{algorithm}"
        equation = "\\begin{equation}\n  x=x  \n\\end{equation}"
        source = "\\section{Method}\nBefore\n" + code + "\n" + algorithm + "\n" + equation + "\nAfter"
        sink = []

        converter = (contextlib.nullcontext() if importlib.util.find_spec("pylatexenc") else
                     patch.object(extractor, "_extract_elements_latex",
                                  side_effect=lambda body, **kwargs: [("prose", body.strip())]))
        with converter, \
                patch.object(extractor, "_parse_deadline", side_effect=lambda _: contextlib.nullcontext()):
            baseline_sections = extractor.split_sections_raw(extractor.strip_comments(source))
            baseline_elements = extractor.extract_elements(baseline_sections[0][1])
            expected = "\n\n".join(text.strip() if kind == "prose" else text
                                   for kind, text in baseline_elements if text and text.strip())
            default = extractor.build_chunks(source)
            collected = extractor.build_chunks(source, section_sink=sink)
        self.assertEqual(default, collected)
        self.assertNotEqual(collected[1], "legacy")
        text = sink[0]["text"]
        self.assertEqual(text, expected)
        self.assertEqual(len(text), len(expected))
        offsets = []
        for raw in ("Before", code, algorithm, equation, "After"):
            self.assertIn(raw, text)
            self.assertEqual(text.count(raw), 1)
            offset = text.index(raw)
            self.assertEqual(text[offset:offset + len(raw)], raw)
            offsets.append(offset)
        self.assertEqual(offsets, sorted(offsets))


if __name__ == "__main__":
    unittest.main()
