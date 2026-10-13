import contextlib
import importlib.util
import unittest
from unittest.mock import patch

from pipeline import extractor


@unittest.skipUnless(importlib.util.find_spec("pylatexenc"), "pylatexenc is not installed")
class LatexConverterRecoveryTests(unittest.TestCase):
    def test_malformed_href_recovers_only_its_bounded_node(self):
        figure = "\n".join([
            r"\begin{figure*}[t]",
            r"\begin{minipage}{.9\linewidth}",
            r"\caption{Nested {caption} braces with inline \verb|50%|}",
            r"\includegraphics{plot.pdf} % commented fake \end{figure*}",
            r"\end{minipage}",
            r"\end{figure*}",
        ])
        equation = r"\begin{equation}x^2 + y^2 = z^2\end{equation}"
        source = "\n".join([
            r"\newcommand{\kv}{\texttt{SnapKV}\xspace}",
            r"\section{Results}",
            "Figure discussion before the next section.",
            figure,
            "Text after the figure remains in Results.",
            r"\section{Discussions}",
            r"\label{sec: conclusion}",
            r"\kv preserves the important cache tokens. This paragraph describes the method.",
            r"\href{https://example.org} The converter must retain this malformed link locally.",
            r"Despite the method's strengths, this limitation remains important.",
            equation,
            "The final discussion sentence remains after the formula.",
        ])

        with patch.object(extractor, "_parse_deadline",
                          side_effect=lambda _seconds: contextlib.nullcontext()):
            chunks, mode = extractor.build_chunks(source)

        self.assertEqual(mode, "pylatexenc_recovered")
        figure_chunks = [chunk for chunk in chunks if chunk["element_type"] == "figure"]
        self.assertEqual(len(figure_chunks), 1)
        self.assertIn(r"\caption{Nested {caption} braces with inline \verb|50%|}",
                      figure_chunks[0]["text"])
        self.assertNotIn("commented fake", figure_chunks[0]["text"])

        discussion = [chunk for chunk in chunks if chunk["section_title"] == "Discussions"]
        prose = "\n".join(chunk["text"] for chunk in discussion if chunk["element_type"] == "prose")
        equations = [chunk["text"] for chunk in discussion if chunk["element_type"] == "equation"]
        self.assertIn(r"\kv", prose)
        self.assertIn(r"\href{https://example.org}", prose)
        self.assertIn("This paragraph describes the method", prose)
        self.assertIn("this limitation remains important", prose)
        self.assertIn("final discussion sentence remains after the formula", prose)
        self.assertEqual(equations, [equation])

    def test_recovered_mode_is_not_used_for_clean_prose(self):
        source = r"\section{Discussions}" + "\nA clean discussion paragraph."
        with patch.object(extractor, "_parse_deadline",
                          side_effect=lambda _seconds: contextlib.nullcontext()):
            chunks, mode = extractor.build_chunks(source)

        self.assertEqual(mode, "pylatexenc")
        self.assertEqual([chunk["text"] for chunk in chunks], ["A clean discussion paragraph."])


if __name__ == "__main__":
    unittest.main()
