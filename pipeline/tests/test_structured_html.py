import hashlib
import unittest

from pipeline.structured_html import HTMLLimitError, project_search_text
from pipeline.whitepapers import html_to_structured, html_to_text


class SearchProjectionTests(unittest.TestCase):
    def project(self, html):
        return project_search_text(html_to_structured(html.encode("utf-8")))

    def test_exact_tex_order_context_and_immutable_ir(self):
        import copy
        ir = html_to_structured(b'<h2 id="s">Section</h2><p id="p">Before <math id="m">'
                                b'<semantics><mi>x</mi><annotation encoding="application/x-tex">'
                                b'x_{i}^{2}</annotation></semantics></math> after.</p>')
        before = copy.deepcopy(ir)
        out = project_search_text(ir)
        self.assertEqual(ir, before)
        self.assertEqual(out, project_search_text(ir))
        self.assertIn('Before x_{i}^{2} after.', out['text'])
        self.assertNotIn('[[math:', out['text'])
        math = next(s for s in out['segments'] if s['kind'] == 'math')
        self.assertEqual(math['source_dom_id'], 'm')
        self.assertEqual(math['context']['section_ids'], ['block-1'])
        self.assertEqual(out['text'][math['start']:math['end']], 'x_{i}^{2}')
        self.assertTrue(out['quality_candidate'])
        self.assertFalse(out['publication_authorized'])
        for segment in out['segments']:
            self.assertEqual(segment['raw_sha256'], ir['raw_sha256'])
            self.assertIn('source_span', segment)

    def test_grid_spans_caption_nested_table_math_once(self):
        out = self.project('<table id="t"><caption>Results</caption><tr><th scope="col" colspan="2">'
                           'Score</th></tr><tr><td><math><annotation encoding="application/x-tex">'
                           'a+b</annotation></math></td><td><table><tr><th scope="col">Nested</th>'
                           '</tr><tr><td>7</td></tr></table></td></tr></table>')
        self.assertEqual(out['text'].count('a+b'), 1)
        self.assertEqual(out['text'].count('Score'), 1)
        self.assertEqual(out['text'].count('Results'), 1)
        self.assertEqual(out['text'].count('Table table-2:'), 1)
        self.assertIn('Row 1, column 1; span 1x2; header;', out['text'])
        self.assertTrue(any(s.get('cell_id') and s.get('math_id') for s in out['segments']))

    def test_ambiguous_and_unsupported_not_candidates(self):
        for html, code in [('<math><mi>x</mi></math>', 'unsupported_math_without_tex'),
                           ('<math><annotation encoding="application/x-tex">x</annotation>'
                            '<annotation encoding="application/x-tex">y</annotation></math>', 'ambiguous_tex'),
                           ('<table><tr><td>1</td></tr></table>', 'no_explicit_th_headers'),
                           ('<p><img alt="not transcribed"></p>', 'nontext_content_not_transcribed')]:
            with self.subTest(code=code):
                out = self.project(html)
                self.assertFalse(out['quality_candidate'])
                self.assertIn(code, [i['code'] for i in out['issues']])

    def test_malformed_missing_duplicate_and_caps(self):
        import copy
        out = self.project('<table><tr><th>x</th><td colspan="bad">y</td></tr></table>')
        self.assertFalse(out['quality_candidate'])
        ir = html_to_structured(b'<p><math><mi>x</mi></math></p>')
        broken = copy.deepcopy(ir)
        broken['math'] = []
        self.assertIn('missing_structure', [i['code'] for i in project_search_text(broken)['issues']])
        ir['math'].append(copy.deepcopy(ir['math'][0]))
        with self.assertRaises(ValueError):
            project_search_text(ir)
        for limits in ({'max_chars': 1}, {'max_segments': 1}):
            with self.assertRaises(HTMLLimitError):
                project_search_text(html_to_structured(b'<p>abc</p>'), **limits)

    def test_scripts_pre_and_subsup_context(self):
        out = self.project('<script>BAD</script><p id="p">x<sub>i</sub><sup>2</sup></p>'
                           '<pre>  a\n    b # comment</pre>')
        self.assertNotIn('BAD', out['text'])
        self.assertIn('x_{i}^{2}', out['text'])
        self.assertIn('  a\n    b # comment', out['text'])


class StructuredHTMLTests(unittest.TestCase):
    def extract(self, text, **limits):
        return html_to_structured(text.encode("utf-8"), **limits)

    def test_legacy_unchanged_opt_in_and_external_gate(self):
        raw = b'<p>A <b>B</b>&nbsp;</p><script>bad()</script>'
        self.assertEqual(html_to_text(raw), ' A B ')
        value = html_to_structured(raw)
        self.assertEqual(value["raw_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(value["status"], "DIAGNOSTIC_ONLY")
        self.assertFalse(value["publication_authorized"])
        self.assertEqual(value["exact_edition_binding"], {
            "required_external_gate": True, "status": "NOT_VERIFIED",
            "document_id_equality_sufficient": False})

    def test_nested_math_single_reference_and_alternatives(self):
        source = ('<section id="s"><h2 id="title">Reward</h2><p id="p">Before '
                  '<math id="m" alttext="gamma to n"><semantics><mrow><mi>γ</mi>'
                  '<math><mi>n</mi></math></mrow><annotation encoding="application/x-tex">'
                  'r(n)=\\gamma^{n}</annotation></semantics></math> after.</p></section>')
        value = self.extract(source)
        self.assertEqual(len(value["math"]), 1)
        math = value["math"][0]
        self.assertEqual(math["tex_annotations"][0]["value"], 'r(n)=\\gamma^{n}')
        self.assertEqual(math["alttext"], 'gamma to n')
        self.assertEqual(math["context"]["section_ids"], ['block-1'])
        self.assertEqual(math["ancestor_dom_ids"], ['s', 'p'])
        self.assertEqual(value["text"].count('[[math:math-1]]'), 1)
        self.assertNotIn('r(n)', value["text"])
        span = math["source_span"]
        self.assertEqual(source[span["start"]:span["end"]], math["mathml"])
        self.assertIn('encoding="application/x-tex"', math["mathml"])

    def test_spans_grid_and_group_headers(self):
        value = self.extract('<table id="t"><caption>Scores</caption><thead>'
                             '<tr><th id="model" rowspan="2" scope="col">Model</th>'
                             '<th id="score" colspan="2" scope="colgroup">Score</th></tr>'
                             '<tr><th id="mean" scope="col">Mean</th><th scope="col">SD</th></tr>'
                             '</thead><tbody><tr><th scope="row">A</th>'
                             '<td headers="mean">1.48</td><td>0.62</td></tr></tbody></table>')
        table = value["tables"][0]
        self.assertEqual(len(table["cells"]), 7)
        self.assertEqual(len(table["grid"]), 3)
        self.assertEqual(table["grid"][0][0], table["grid"][1][0])
        self.assertEqual(table["grid"][0][1], table["grid"][0][2])
        target = next(c for c in table["cells"] if c["text"] == '1.48')
        self.assertEqual((target["row"], target["column"]), (2, 1))
        self.assertEqual(set(target["header_cell_ids"]), {'table-1-cell-2', 'table-1-cell-3', 'table-1-cell-5'})
        self.assertEqual(target["header_binding_status"], 'EXPLICIT')
        self.assertEqual(table["unsupported"], [])

    def test_rowspan_zero_stays_in_its_group(self):
        table = self.extract('<table><tbody><tr><th rowspan="0" scope="rowgroup">A</th><td>1</td></tr>'
                             '<tr><td>2</td></tr></tbody><tbody><tr><td>B</td><td>3</td></tr></tbody></table>')["tables"][0]
        self.assertEqual(table["cells"][0]["row_span"], 2)
        self.assertEqual(table["grid"][0][0], table["grid"][1][0])
        self.assertNotEqual(table["grid"][1][0], table["grid"][2][0])

    def test_nested_table_and_cell_math_are_not_flattened(self):
        value = self.extract('<h1>Chapter</h1><table id="outer"><tr><td id="cell">Text '
                             '<math><annotation encoding="application/x-tex">x^2</annotation></math>'
                             '<table id="inner"><tr><td>9</td></tr></table></td></tr></table>')
        self.assertEqual([t["id"] for t in value["tables"]], ['table-1', 'table-2'])
        self.assertEqual(len(value["tables"][0]["cells"]), 1)
        self.assertEqual(value["tables"][0]["grid"], [['table-1-cell-1']])
        self.assertEqual(value["tables"][1]["grid"], [['table-2-cell-1']])
        self.assertEqual(value["math"][0]["context"]["cell_id"], 'table-1-cell-1')
        self.assertEqual(value["math"][0]["context"]["section_ids"], ['block-1'])
        self.assertIn('[[table:table-2]]', value["tables"][0]["cells"][0]["text"])

    def test_malformed_preserves_raw_and_does_not_invent_grid(self):
        source = '<table id="broken"><tr><td>A</table>'
        value = self.extract(source)
        self.assertTrue(value["diagnostics"])
        self.assertEqual(value["tables"][0]["raw_html"], source)
        self.assertIsNone(value["tables"][0]["grid"])
        self.assertIn('malformed_html', value["tables"][0]["unsupported"])

    def test_invalid_cross_group_and_overlapping_spans(self):
        for source in ('<table><tr><td colspan="bad">A</td></tr></table>',
                       '<table><thead><tr><th rowspan="2">A</th></tr></thead>'
                       '<tbody><tr><td>B</td></tr></tbody></table>',
                       '<table><tr><td>A</td><td rowspan="2">B</td></tr>'
                       '<tr><td colspan="2">C</td></tr></table>'):
            table = self.extract(source)["tables"][0]
            self.assertIsNone(table["grid"])
            self.assertTrue(table["unsupported"])
            self.assertIn(source, table["raw_html"])

    def test_caps_fail_closed(self):
        cases = [('<p>long text</p>', {"max_bytes": 3}),
                 ('<p><i><b>x</b></i></p>', {"max_depth": 2}),
                 ('<p>x<b>y</b></p>', {"max_nodes": 2}),
                 ('<table><tr><td colspan="99">x</td></tr></table>', {"max_grid_slots": 3}),
                 ('<math><mi>x</mi></math>', {"max_raw_chars": 2}),
                 ('<table><tr><th>A</th><th>B</th><th>C</th><th>D</th><th>E</th></tr></table>',
                  {"max_grid_slots": 5})]
        for source, limits in cases:
            with self.subTest(limits=limits), self.assertRaises(HTMLLimitError):
                self.extract(source, **limits)
        with self.assertRaises(ValueError):
            self.extract('<p>x</p>', max_nodes=True)

    def test_scripts_ignored_no_network_and_context_order(self):
        value = self.extract('<head><script src="https://invalid.test/a">bad()</script></head>'
                             '<h1 id="a">A</h1><p>First<script>evil</script><style>bad</style> last</p>'
                             '<h2 id="b">B</h2><pre>if x:\n  y() # comment\n  z()</pre>')
        self.assertEqual([b["kind"] for b in value["blocks"]], ['heading', 'p', 'heading', 'pre'])
        self.assertEqual(value["blocks"][1]["text"], 'First last')
        self.assertEqual(value["blocks"][-1]["section_ids"], ['block-1', 'block-3'])
        self.assertIn('\n  y() # comment\n  z()', value["blocks"][-1]["text"])
        self.assertNotIn('evil', value["text"])
        self.assertNotIn('bad', value["text"])

    def test_unsupported_math_and_image_do_not_invent_tex(self):
        value = self.extract('<p><math alttext="plain alternative"><mfrac><mi>x</mi><mi>y</mi>'
                             '</mfrac></math><img alt="x squared" src="https://invalid.test/img"></p>')
        self.assertEqual(value["math"][0]["status"], 'MATHML_ONLY')
        self.assertEqual(value["math"][0]["tex_annotations"], [])
        self.assertIn('mathml_semantics_not_interpreted', value["math"][0]["unsupported"])
        self.assertEqual(len(value["math"]), 1)
        image = value["blocks"][0]["content"][1]
        self.assertEqual(image["alttext"], 'x squared')
        self.assertTrue(image["unsupported"])

    def test_unicode_offsets_inline_superscripts_and_duplicate_dom_ids(self):
        source = '<h1>Тест</h1><p id="same">R<sup>2</sup></p><p id="same"><math><mi>α</mi></math></p>'
        value = self.extract(source)
        self.assertEqual(len({b["id"] for b in value["blocks"]}), 3)
        self.assertEqual(value["blocks"][1]["text"], 'R[[sup:2]]')
        math = value["math"][0]
        self.assertEqual(source[math["source_span"]["start"]:math["source_span"]["end"]], math["mathml"])


if __name__ == '__main__':
    unittest.main()
