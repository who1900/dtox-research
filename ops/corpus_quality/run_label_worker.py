"""Apply all corpus candidates for one repair class in maintenance batches."""
import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
import time
import urllib.request
import uuid

from ops import repair_corpus_labels, restore_corpus_points

UNIT = 'dtox-research-api-oracle-20261012.service'


def _file_identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    return json.dumps([str(path), stat.st_dev, stat.st_ino, stat.st_size,
                       stat.st_mtime_ns], separators=(',', ':'))


class Qdrant:
    def request(self, path, body, method='POST'):
        req = urllib.request.Request('http://127.0.0.1:6333' + path,
              data=json.dumps(body, ensure_ascii=False).encode(), headers={'Content-Type': 'application/json'},method=method)
        with urllib.request.urlopen(req, timeout=60) as response:
            value = json.load(response)
        if value.get('status') != 'ok':
            raise RuntimeError('Qdrant returned non-ok response')
        return value['result']

    def scroll(self, *, collection_name, scroll_filter, limit, with_payload, with_vectors, offset=None):
        body = {'filter': scroll_filter, 'limit': min(limit, 200),
                'with_payload': with_payload, 'with_vector': with_vectors}
        if offset is not None:
            body['offset'] = offset
        page = self.request('/collections/' + collection_name + '/points/scroll', body)
        return page['points'], page.get('next_page_offset')

    def set_payload(self, *, collection_name, payload, points, wait):
        return self.request('/collections/' + collection_name + '/points/payload?wait=true',
                            {'payload': payload, 'points': points})

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        return self.request('/collections/' + collection_name + '/points',
                            {'ids': ids, 'with_payload': with_payload, 'with_vector': with_vectors})

    def upsert(self, *, collection_name, points, wait):
        return self.request('/collections/' + collection_name + '/points?wait=true', {'points': points}, method='PUT')

    def delete(self, *, collection_name, points_selector, wait):
        return self.request('/collections/' + collection_name + '/points/delete?wait=true', {'points': points_selector})


def systemctl(*args):
    p = subprocess.run(['systemctl', *args], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError('systemctl ' + ' '.join(args) + ': ' + p.stderr[:200])
    return p.stdout.strip()


def health():
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8010/v1/health', timeout=3) as response:
                answer = json.load(response)
            if answer.get('status') == 'ok':
                return
        except Exception:
            pass
        time.sleep(.5)
    raise RuntimeError('API health failed after maintenance')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', default='/opt/dtox-corpus-quality-20261013', type=Path)
    p.add_argument('--batch-seconds', default=60, type=float)
    p.add_argument('--batch-pause', default=5, type=float)
    p.add_argument('--mode', choices=['labels','points'], default='labels')
    p.add_argument('--candidate-census', type=Path)
    p.add_argument('--retry-failed', action='store_true',
                   help='retry failed papers from the existing label ledger after journal recovery')
    p.add_argument('--verify-completed', action='store_true',
                   help='re-verify completed papers in a bounded source-proven label subset')
    p.add_argument('--sources', help='Optional comma separated point-restoration cohort')
    args = p.parse_args()
    if args.verify_completed and args.mode != 'labels':
        p.error('--verify-completed is only valid for label repairs')
    base = args.base.resolve()
    root = Path('/opt/dtox-research')
    prefix=('partial-label' if args.candidate_census else 'label') if args.mode=='labels' else ('point-latex' if args.sources=='latex' else 'point')
    # The initial concurrent point cohort is LaTeX; label candidates are PDF
    # records. Later point cohorts run after labels through run_sequence.py.
    with (base / (args.mode+'-worker.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        census = sqlite3.connect((base / 'census.sqlite').as_uri() + '?mode=ro', uri=True)
        meta = dict(census.execute('SELECT key,value FROM meta'))
        universe = census.execute('SELECT COUNT(*) FROM papers').fetchone()[0]
        census.close()
        if meta.get('run_complete') != '1' or universe != 183854:
            raise RuntimeError('full frozen corpus census is not complete')
        candidate_db=args.candidate_census or base/'census.sqlite'
        with sqlite3.connect(candidate_db.as_uri()+'?mode=ro',uri=True) as candidate_conn:
            candidate_meta = dict(candidate_conn.execute('SELECT key,value FROM meta'))
            verified_unresolved = candidate_meta.get('candidate_subset') == 'verified-unresolved-label-retry'
            candidate_filter = "(candidate_status='candidate' AND exact_text_ids=1) OR (candidate_status='heading_label_candidate' AND labels_source_verified=1"
            candidate_filter += " AND candidate_label_map_json!='[]'" if verified_unresolved else " AND candidate_changes_json!='[]'"
            candidate_filter += ")"
            candidates = candidate_conn.execute('SELECT COUNT(*) FROM papers WHERE '+candidate_filter).fetchone()[0]
        if args.retry_failed and not args.verify_completed:
            if (not args.candidate_census
                    or candidate_meta.get('candidate_subset') != 'failed-label-retry'
                    or candidate_meta.get('source_census_identity') != _file_identity(base/'census.sqlite')):
                raise RuntimeError('failed-label retry candidate does not match the current frozen census')
        if args.verify_completed:
            if (not args.candidate_census
                    or not verified_unresolved
                    or candidate_meta.get('source_census_identity') != _file_identity(base/'census.sqlite')
                    or candidate_meta.get('source_verified_unresolved_count') != '403'
                    or candidate_meta.get('source_unresolved_chunk_count') != '1758'
                    or candidate_meta.get('source_bound_selected_count') != '343'
                    or candidate_meta.get('source_bound_resolved_chunk_count') != '1134'
                    or candidates != 343):
                raise RuntimeError('verified-unresolved candidate does not match the frozen census, label ledger, and bounded selection')
            prefix='verified-unresolved-label'
        config = SimpleNamespace(state_db=str((root/'state.db').resolve()), fts_db=str((root/'fts.db').resolve()),
            census_db=str(candidate_db), rowmap_db=str(base/'fts-rowmap.sqlite'),
            cache_dir=str((root/'chunk_cache').resolve()), section_cache_dir=str((root/'section_cache').resolve()),
            fulltext_cache_dir=str((root/'fulltext_cache').resolve()), latex_cache_dir=str((root/'latex_cache').resolve()),
            journal_dir=str(base/'label-journals'), run_db=str(base/'label-run.sqlite'),
            collection='papers_fulltext', limit=0, max_seconds=args.batch_seconds,
            retry_failed=args.retry_failed, verify_completed=args.verify_completed,
            bounded_retry_id=str(uuid.uuid4()) if args.verify_completed and args.retry_failed else None)
        client = Qdrant()
        @contextmanager
        def publication_maintenance():
            if systemctl('show',UNIT,'--value','--property=ActiveState')=='active':
                systemctl('stop',UNIT)
            try:
                yield
            except BaseException:
                # The driver compensates from its journal under the same gate.
                raise
            else:
                systemctl('start',UNIT)
                health()
        config.publication_maintenance=publication_maintenance
        recipe=None
        if args.mode=='points':
            recipe=json.loads((base/'embedding-recipe.json').read_text())
            config=SimpleNamespace(**vars(config))
            config.projection_db=str(base/'projection.sqlite')
            config.label_run_db=str(base/'label-run.sqlite')
            config.run_db=str(base/'point-run.sqlite')
            config.journal_dir=str(base/'point-journals')
            config.batch_size=20
            config.allowed_sources=tuple(args.sources.split(',')) if args.sources else None
        def embedder(texts):
            request=urllib.request.Request(recipe['endpoint'],data=json.dumps({'texts':texts}).encode(),headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request,timeout=120) as response:
                return json.load(response)['vectors']
        batch = 0
        started = time.time()
        while True:
            for unit in ('dtox-research.service','dtox-pipeline.service'):
                status = systemctl('show',unit,'--value','--property=ActiveState')
                if status not in ('inactive','failed'):
                    raise RuntimeError('pipeline writer is active')
            health()
            maintenance = time.monotonic()
            try:
                result = (repair_corpus_labels.run(config, client) if args.mode=='labels'
                          else restore_corpus_points.run(config, client, embedder, recipe))
            except BaseException:
                # A driver fatal error means compensation was not established.
                # Keep readers closed; the next run inspects its durable journal.
                systemctl('stop',UNIT)
                (base/(prefix+'-worker-fatal.json')).write_text(json.dumps({'maintenance_retained':True,'batch':batch})+'\n')
                raise
            if systemctl('show',UNIT,'--value','--property=ActiveState')!='active':
                systemctl('start',UNIT)
            health()
            batch += 1
            pending=result['pending'] if args.mode=='labels' else result['pending_points']
            with sqlite3.connect(config.run_db) as ledger:
                counts = dict(ledger.execute('SELECT status,COUNT(*) FROM '+('papers' if args.mode=='labels' else 'targets')+' GROUP BY status'))
            summary = {'phase':'running' if pending else args.mode+'_complete', 'repair_class':args.mode,
                'corpus_documents':universe,'label_candidates':candidates, 'batch':batch,
                'batch_result':result,'paper_outcomes':counts,
                'last_batch_seconds':round(time.monotonic()-maintenance,3),
                'reader_gate':'published revision transitions only; legacy label/addition updates online',
                'elapsed_seconds':round(time.time()-started,3),
                'whole_corpus_quality_complete':False,'api_health':'ok'}
            repair_corpus_labels._atomic_json(base/(prefix+'-worker-status.json'),summary)
            print(json.dumps(summary),flush=True)
            if not pending:
                break
            time.sleep(args.batch_pause)


if __name__ == '__main__': main()
