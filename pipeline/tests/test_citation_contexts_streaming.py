"""Offline streaming contracts; no producer, DB, network or deployment."""

import hashlib
import json
import tracemalloc
import unittest

from pipeline.citation_contexts import extract_citation_contexts, stream_citation_contexts


LIMIT = 8 * 1024 * 1024


class CitationContextStreamingTests(unittest.TestCase):
    def test_independent_legacy_shape_raw_spans_masking_order_and_hashes(self):
        source = ('Intro\r\n\r\n  Текст \\citep[see]{ k , z }. '
                  '\\verb|\\cite{ghost}| % скрыто\r\nAfter\r\n\r\nNext \\textcite{z}.')
        bib = ('@misc{k,doi={10.1234/AbC}}\n'
               '@misc{z,url={https://arxiv.org/abs/2401.12345v2}}')
        files = [('refs.bib', bib)]

        def provenance(key, target, requested, version, field, literal):
            start = bib.index('@misc{' + key)
            end = bib.index('\n', start) if key == 'k' else len(bib)
            return dict(path='refs.bib', origin='bibliography_file', entry_start=start,
                        entry_end=end, entry_raw=bib[start:end], field=field,
                        identifier_raw=literal, target_id=target,
                        requested_id=requested, requested_version=version)

        references = {
            'k': ('doi:10.1234/abc', [provenance('k', 'doi:10.1234/abc', 'doi:10.1234/abc',
                                               None, 'doi', '10.1234/AbC')]),
            'z': ('arxiv:2401.12345', [provenance('z', 'arxiv:2401.12345', 'arxiv:2401.12345v2',
                                                2, 'url', 'https://arxiv.org/abs/2401.12345v2')]),
        }
        expected = []
        for command, start, end, names in [
                ('citep', source.index('Текст'), source.index('After') + 5, ['k', 'z']),
                ('textcite', source.index('Next'), len(source), ['z'])]:
            cs = source.index('\\' + command, start)
            ce = source.index('}', cs) + 1
            keys = []
            cursor = source.index('{', cs) + 1
            for name in names:
                ka = source.index(name, cursor, ce)
                keys.append(dict(key=name, start=ka, end=ka + len(name), raw=name))
                cursor = ka + len(name)
            raw = source[start:end]
            search = raw
            for masked in ['\\verb|\\cite{ghost}|', '% скрыто\r\n']:
                search = search.replace(masked, ' ' * len(masked))
            expected.append(dict(
                start=start, end=end, raw=raw, search_text=search,
                citations=[dict(start=cs, end=ce, raw=source[cs:ce], command=command, keys=keys)],
                resolved=[dict(key=name, citation_start=cs, citation_end=ce,
                               target_id=references[name][0], association='lead',
                               provenance=references[name][1]) for name in names],
                may_discuss_multiple_works=len(names) > 1))
        stream = stream_citation_contexts(source, files)
        self.assertEqual(list(stream), expected)
        self.assertEqual(stream.diagnostics, [])
        self.assertEqual(stream.source_sha256, hashlib.sha256(source.encode()).hexdigest())
        bib_raw = json.dumps(files, ensure_ascii=False, separators=(',', ':')).encode()
        self.assertEqual(stream.bibliography_sha256, hashlib.sha256(bib_raw).hexdigest())
        legacy = extract_citation_contexts(source, files)
        self.assertEqual(legacy, dict(schema_version=1, contexts=expected, diagnostics=[],
                                     source_sha256=stream.source_sha256,
                                     bibliography_sha256=stream.bibliography_sha256))

    def test_repeated_cite_fanout_preserves_all_occurrences_and_multiplicity(self):
        source = ('Occurrence \\cite{k}.\r\n\r\n' * 101).rstrip()
        bib = '@misc{k,doi={10.1234/a}}\n' * 100
        files = [('refs.bib', bib)]
        stream = stream_citation_contexts(source, files)
        shared, contexts, occurrences = None, 0, 0
        for context in stream:
            contexts += 1
            self.assertEqual(len(context['citations']), 1)
            self.assertEqual(context['raw'], source[context['start']:context['end']])
            lead, = context['resolved']
            provenance = lead['provenance']
            self.assertEqual(len(provenance), 100)
            self.assertEqual(len({p['entry_start'] for p in provenance}), 100)
            if shared is None:
                shared = provenance
            self.assertIs(provenance, shared)
            occurrences += len(provenance)
        self.assertEqual((contexts, occurrences), (101, 10100))
        self.assertEqual(stream.diagnostics, [])
        with self.assertRaisesRegex(ValueError, '10000 emitted_provenance'):
            extract_citation_contexts(source, files)

    def test_expanded_entry_bytes_over_8mib_do_not_accumulate_in_stream(self):
        bib = '@misc{k,doi={10.1234/a},title={' + 'x' * 50000 + '}}'
        source = '\\cite{k}\n\n' * 256
        files = [('refs.bib', bib)]
        self.assertLess(len(source.encode()) + len(bib.encode()) + 8, LIMIT)
        tracemalloc.start()
        try:
            stream = stream_citation_contexts(source, files)
            count, expanded = 0, 0
            for context in stream:
                count += 1
                expanded += len(json.dumps(context, ensure_ascii=False).encode())
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(count, 256)
        self.assertGreater(expanded, LIMIT)
        self.assertLess(peak, LIMIT)
        with self.assertRaisesRegex(ValueError, '8 MiB emitted_bytes'):
            extract_citation_contexts(source, files)

    def test_repeated_paths_over_whole_json_limit_are_streamable(self):
        files = [('p' * (1024 * 1024) + '.bib', '@misc{k,doi={10.1234/a}}')]
        source = '\\cite{k}\n\n' * 9
        stream = stream_citation_contexts(source, files)
        count, expanded = 0, 0
        for context in stream:
            count += 1
            expanded += len(json.dumps(context, ensure_ascii=False).encode())
        self.assertEqual(count, 9)
        self.assertGreater(expanded, LIMIT)
        with self.assertRaisesRegex(ValueError, '8 MiB budget'):
            extract_citation_contexts(source, files)

    def test_conflict_versions_comments_and_final_diagnostics_match_legacy(self):
        bib = ('% @misc{hidden,doi={10.1234/hidden}}\n'
               '@misc{same,eprint={2401.12345v1},archivePrefix={arXiv}}\n'
               '@misc{same,eprint={2401.12345v2},archivePrefix={arXiv}}\n'
               '@misc{amb,doi={10.1234/a},url={https://arxiv.org/abs/2401.12345}}\n'
               '@misc{macro,url=link}\n'
               '@misc{good,url={https://arxiv.org/abs/2401.12345v2}}')
        source = ('% \\cite{hidden}\n\\cite{same,amb,macro,missing,good}\n\n'
                  '\\cite{good,}')
        files = [('z.txt', 'unsupported'), ('a.bib', bib)]
        stream = stream_citation_contexts(source, files)
        diagnostics = stream.diagnostics
        self.assertEqual([d['code'] for d in diagnostics],
                         ['ambiguous_identifiers', 'malformed_bibtex',
                          'unsupported_bibliography_file', 'conflicting_duplicate_key'])
        contexts = list(stream)
        self.assertIs(diagnostics, stream.diagnostics)
        self.assertEqual([d['code'] for d in diagnostics],
                         ['ambiguous_identifiers', 'malformed_bibtex',
                          'unsupported_bibliography_file', 'conflicting_duplicate_key',
                          'unresolved_reference', 'unresolved_reference', 'unresolved_reference',
                          'unknown_reference', 'malformed_citation'])
        lead, = contexts[0]['resolved']
        self.assertEqual(lead['target_id'], 'arxiv:2401.12345')
        self.assertEqual(lead['provenance'][0]['requested_id'], 'arxiv:2401.12345v2')
        self.assertEqual(lead['provenance'][0]['requested_version'], 2)
        legacy = extract_citation_contexts(source, list(reversed(files)))
        self.assertEqual(contexts, legacy['contexts'])
        self.assertEqual(diagnostics, legacy['diagnostics'])
        self.assertEqual(stream.bibliography_sha256, legacy['bibliography_sha256'])

    def test_iterator_is_one_shot_and_diagnostics_are_incremental(self):
        stream = stream_citation_contexts('\\cite{first}\n\n\\cite{second}')
        self.assertIs(iter(stream), stream)
        self.assertEqual(stream.diagnostics, [])
        next(stream)
        self.assertEqual([d['key'] for d in stream.diagnostics], ['first'])
        self.assertEqual(len(list(stream)), 1)
        self.assertEqual([d['key'] for d in stream.diagnostics], ['first', 'second'])
        self.assertEqual(list(stream), [])

    def test_input_guards_are_eager_without_iteration(self):
        for source, files, error in [
                (None, (), TypeError), ('', iter(()), TypeError),
                ('', [['a.bib', '']], TypeError),
                ('x' * (LIMIT + 1), (), ValueError),
                ('я' * (LIMIT // 2 + 1), (), ValueError),
                ('', [('a.bib', '')] * 1025, ValueError),
                ('x' * LIMIT, [('a.bib', '')], ValueError)]:
            with self.subTest(error=error, length=len(source) if isinstance(source, str) else None):
                with self.assertRaises(error):
                    stream_citation_contexts(source, files)

    def test_commands_keys_entries_and_stored_budgets_stay_eager(self):
        for source, files, message in [
                ('\\cite{k}' * 10001, (), '10000 commands'),
                ('\\cite{' + ','.join(['k'] * 10001) + '}', (), '10000 keys'),
                ('', [('a.bib', '@misc{k,title={none}}' * 10001)], '10000 entries'),
                ('', [('a.bib', '@misc{k,doi={10.1234/a},url={https://doi.org/10.1234/a}}' * 5001)],
                 '10000 stored_provenance'),
                ('', [('a.bib', '@misc{k,doi={10.1234/a},url={https://doi.org/10.1234/a},title={'
                                 + 'x' * (LIMIT // 2) + '}}')], '8 MiB stored_bytes')]:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    stream_citation_contexts(source, files)


if __name__ == '__main__':
    unittest.main()
