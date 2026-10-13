import json
from pathlib import Path
import tempfile
import unittest

from api import boilerplate
from ops.corpus_quality import audit_index_admission as audit
from pipeline import extractor


class AdmissionAuditTests(unittest.TestCase):
    def test_pinned_production_policy_is_enabled(self):
        self.assertEqual(audit.policy_identity(), audit.POLICY_SHA256)
        self.assertTrue(extractor.BOILERPLATE_FILTER)
        self.assertIs(extractor.is_boilerplate_chunk, boilerplate.is_boilerplate_chunk)

    def test_exclusion_requires_unique_exact_typed_source(self):
        text = 'We thank the reviewers and acknowledge their helpful comments.'
        chunk = {'text': text, 'section_title': 'Acknowledgments', 'section_type': 'other', 'element_type': 'prose'}
        elements = [{'text': text, 'section_title': 'Acknowledgments', 'section_type': 'other', 'element_type': 'prose'}]
        outcome, reason, count = audit.classify_candidate(chunk, [chunk], elements)
        self.assertEqual((outcome, reason, count), ('EXCLUDED', 'boilerplate_policy', 1))

        duplicate = elements + [dict(elements[0])]
        outcome, reason, count = audit.classify_candidate(chunk, [chunk], duplicate)
        self.assertEqual((outcome, reason, count),
                         ('EXCLUDED_SOURCE_UNVERIFIED',
                          'current_policy_excludes_but_source_interval_not_unique', 2))

        admitted = {'text': 'A useful scientific result.', 'section_title': 'Results',
                    'section_type': 'body', 'element_type': 'prose'}
        outcome, reason, count = audit.classify_candidate(admitted, [admitted], [admitted, dict(admitted)])
        self.assertEqual((outcome, reason, count), (audit.OPEN, 'source_interval_not_unique', 2))

    def test_prepared_journal_is_not_accepted_as_origin(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'journal.json'
            path.write_text(json.dumps({
                'schema': 1, 'kind': 'restore-cached-points', 'phase': 'prepared',
                'arxiv_id': 'paper-1', 'operation_id': 'op-1',
                'source_sha256': 'source', 'cache_sha256': 'cache',
                'points': [{'id': 'point-1', 'vector': [0.1] * 384,
                            'payload': {'arxiv_id': 'paper-1', 'chunk_index': 7,
                                        'text': 'text', 'element_type': 'prose'}}]}),
                encoding='utf-8')
            result = audit._journal_point(path, 'paper-1', 7, {})
            self.assertEqual(len(result), 4)
            self.assertIsNone(result[1])
            self.assertEqual(result[0], 'op-1')
            self.assertEqual(result[2:], ('source', 'cache'))
            self.assertEqual(audit._journal_point(Path(td) / 'missing.json', 'paper-1', 7, {}),
                             (None, None, None, None))

    def test_journal_cache_keeps_compact_point_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            cache = audit.OrderedDict()
            for i in range(8):
                path = Path(td) / f'{i}.json'
                path.write_text(json.dumps({
                    'schema': 1, 'kind': 'restore-cached-points', 'phase': 'applied',
                    'arxiv_id': f'paper-{i}', 'operation_id': f'op-{i}',
                    'source_sha256': 'source', 'cache_sha256': 'cache',
                    'points': [{'id': f'point-{i}', 'vector': [0.1] * 384,
                                'payload': {'arxiv_id': f'paper-{i}', 'chunk_index': i,
                                            'text': 'text', 'element_type': 'prose'}}]}),
                    encoding='utf-8')
                result = audit._journal_point(path, f'paper-{i}', i, cache)
                self.assertEqual(len(result), 4)
                self.assertEqual(result[1], {'id': f'point-{i}', 'payload': {
                    'arxiv_id': f'paper-{i}', 'chunk_index': i,
                    'text': 'text', 'element_type': 'prose'}})
            self.assertEqual(len(cache), 6)
            self.assertNotIn('vector', repr(cache))

    def test_protected_elements_stay_admitted_even_under_boilerplate_heading(self):
        text = 'x=1'
        chunk = {'text': text, 'section_title': 'References', 'section_type': 'other', 'element_type': 'table'}
        outcome, reason, count = audit.classify_candidate(chunk, [chunk], [chunk])
        self.assertEqual((outcome, reason, count), ('CURRENTELIGIBLE', 'current_policy_admits_unique_source', 1))


if __name__ == '__main__':
    unittest.main()
