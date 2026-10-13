import json
from pathlib import Path
import tempfile
import unittest

from ops.corpus_quality import adopt_orphan_documents as helper


class OrphanOperationalHelperTests(unittest.TestCase):
    def test_literal_state_defaults_and_expression_rejection(self):
        self.assertEqual(helper.state_defaults([(0,'count','INTEGER',1,'0',0),(1,'note','TEXT',0,None,0)]),{'count':0,'note':None})
        with self.assertRaises(RuntimeError):helper.state_defaults([(0,'count','INTEGER',1,'abs(-1)',0)])

    def test_source_metadata_uses_exact_summary_and_verified_frontmatter(self):
        raw=b'---\nsimd: "0048"\ntitle: Exact title\ncreated: 2023-05-14\n---\n\n## Summary\n\nExact summary.\nSecond line.\n\n## Motivation\nOther text.'
        payload=dict(title='Exact title',year=2023,layers=['web3'])
        self.assertEqual(helper.source_metadata(raw,'simd:0048',[payload]),('Exact title',2023,'Exact summary.\nSecond line.'))
        with self.assertRaises(RuntimeError):helper.source_metadata(raw,'simd:0048',[dict(payload,year=2024)])

    def gate(self,active,fail=False):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/'orphan-journals').mkdir();calls=[]
            state={'active':active}
            def systemctl(*args):
                calls.append(args)
                if args[0]=='show':return 'active' if state['active'] else 'inactive'
                state['active']=args[0]=='start'
            try:
                with helper.reader_maintenance(base,'simd:0048',systemctl,lambda:calls.append(('health',)),'api'):
                    if fail:raise RuntimeError('unrecovered')
            except RuntimeError:pass
            marker=json.loads((base/'orphan-journals'/'simd-0048-maintenance.json').read_text())
            return calls,state['active'],marker

    def test_gate_restores_only_previously_active_service(self):
        calls,active,marker=self.gate(True)
        self.assertTrue(active);self.assertEqual(marker['phase'],'restored')
        self.assertIn(('stop','api'),calls);self.assertIn(('start','api'),calls)
        calls,active,marker=self.gate(False)
        self.assertFalse(active);self.assertNotIn(('start','api'),calls)

    def test_unrecovered_failure_keeps_readers_closed(self):
        calls,active,marker=self.gate(True,True)
        self.assertFalse(active);self.assertEqual(marker['phase'],'closed')
        self.assertNotIn(('start','api'),calls)

    def test_crash_marker_preserves_original_service_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/'orphan-journals').mkdir()
            marker=base/'orphan-journals'/'simd-0048-maintenance.json'
            marker.write_text(json.dumps(dict(phase='closed',previously_active=True)))
            calls=[]
            def systemctl(*args):
                calls.append(args)
                return 'inactive'
            with helper.reader_maintenance(base,'simd:0048',systemctl,lambda:None,'api'):pass
            self.assertIn(('start','api'),calls)


if __name__=='__main__':unittest.main()
