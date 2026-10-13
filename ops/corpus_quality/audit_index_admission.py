"""Read-only audit of frozen and receipt-owned points against live admission policy."""
import argparse
import hashlib
import json
from collections import OrderedDict
from pathlib import Path
import sqlite3
import time
import urllib.request
import uuid

from api import boilerplate
from ops import repair_section_labels as labels
from ops import restore_cached_points as restore
from ops import restore_corpus_points as driver
from pipeline import extractor

POLICY_SHA256 = 'b827f11ceee3404af6c2ec3e4dccd50e89cb6eef961bc929857245ff4d191893'
OPEN = 'UNVERIFIED_SOURCE'


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def policy_identity():
    path = Path(boilerplate.__file__).resolve()
    raw = path.read_bytes().replace(b'\r\n', b'\n')
    digest = sha256(raw)
    if digest != POLICY_SHA256:
        raise RuntimeError('admission policy source SHA256 differs from production pin')
    if not boilerplate.BOILERPLATE_FILTER or not extractor.BOILERPLATE_FILTER:
        raise RuntimeError('boilerplate admission filter is disabled in this process')
    if extractor.is_boilerplate_chunk is not boilerplate.is_boilerplate_chunk:
        raise RuntimeError('pipeline extractor is not using the pinned API policy function')
    return digest


def occurrences(text, target):
    count, start = 0, text.find(target)
    while start >= 0:
        count += 1
        start = text.find(target, start + 1)
    return count


def exact_source_matches(chunk, parsed, elements):
    """Count exact, typed source occurrences without applying admission policy."""
    target = chunk.get('text') if isinstance(chunk, dict) else None
    element_type = chunk.get('element_type', 'prose') if isinstance(chunk, dict) else None
    if not isinstance(target, str) or not target:
        return 0
    if elements is not None:
        count = sum(occurrences(e['text'], target) for e in elements
                    if isinstance(e, dict) and e.get('element_type', 'prose') == element_type
                    and isinstance(e.get('text'), str))
        if count:
            return count
    return sum(occurrences(c['text'], target) for c in parsed
               if isinstance(c, dict) and c.get('element_type', 'prose') == element_type
               and isinstance(c.get('text'), str))


def classify_candidate(chunk, parsed, elements):
    """Return outcome/reason; excluded is only asserted with exact source proof."""
    admitted = extractor.should_embed(chunk)
    count = exact_source_matches(chunk, parsed, elements)
    if not admitted and count != 1:
        return 'EXCLUDED_SOURCE_UNVERIFIED', 'current_policy_excludes_but_source_interval_not_unique', count
    if count != 1:
        return OPEN, 'source_interval_not_unique', count
    if admitted:
        if restore.source_proven(chunk, parsed, elements):
            return 'CURRENTELIGIBLE', 'current_policy_admits_unique_source', count
        return OPEN, 'current_source_proof_rejected', count
    if boilerplate.is_boilerplate_chunk(chunk.get('text'), chunk.get('section_title'),
                                        chunk.get('section_type'), chunk.get('element_type')):
        return 'EXCLUDED', 'boilerplate_policy', count
    return 'EXCLUDED', 'current_extractor_filter', count


def connect_ro(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class QdrantReadOnly:
    def retrieve(self, collection, ids):
        request = urllib.request.Request(
            'http://127.0.0.1:6333/collections/' + collection + '/points',
            data=json.dumps({'ids': ids, 'with_payload': True, 'with_vector': False}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.load(response)
        if body.get('status') != 'ok':
            raise RuntimeError('Qdrant returned non-ok response')
        return body.get('result', [])


def init_audit(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    conn.execute('''CREATE TABLE IF NOT EXISTS papers(
      paper_id TEXT PRIMARY KEY,input_fingerprint TEXT,ledger_fingerprint TEXT,source_sha256 TEXT,cache_sha256 TEXT,
      frozen_cache_sha256 TEXT,
      policy_sha256 TEXT NOT NULL,extractor_sha256 TEXT NOT NULL,source_location TEXT,
      parsed_chunks INTEGER,source_elements INTEGER,diagnostic TEXT,scanned_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS candidates(
      paper_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,ledger_status TEXT NOT NULL,
      outcome TEXT NOT NULL,reason TEXT NOT NULL,source_outcome TEXT NOT NULL,source_reason TEXT NOT NULL,
      source_occurrences INTEGER NOT NULL,
      text_sha256 TEXT NOT NULL,element_type TEXT,receipt_state TEXT,PRIMARY KEY(paper_id,chunk_index))''')
    conn.execute('CREATE INDEX IF NOT EXISTS candidates_outcome ON candidates(outcome,ledger_status)')
    conn.commit()
    return conn


def _ledger_fingerprint(targets):
    material = [[int(t['chunk_index']), t['status'], t['journal_path']] for t in targets]
    return sha256(json.dumps(material, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def _journal_point(path, paper_id, chunk_index, cache):
    if not path or not Path(path).is_file():
        return None, None, None, None
    key = str(Path(path).resolve())
    if key not in cache:
        plan = json.loads(Path(path).read_text(encoding='utf-8'))
        _, plan_id, points = driver._operation(plan)
        grouped = {}
        for point in points:
            payload = point.get('payload') or {}
            idx = payload.get('chunk_index')
            if idx is None:
                continue
            # Retain only the identity fields needed for point/receipt comparison.
            compact = {'id': point.get('id'), 'payload': {
                field: payload[field] for field in
                ('arxiv_id', 'chunk_index', 'text', 'section_type', 'element_type')
                if field in payload}}
            grouped.setdefault(idx, []).append(compact)
        if plan.get('kind') == 'restore-cached-points':
            operation_id = plan.get('operation_id')
            source_sha, cache_sha = plan.get('source_sha256'), plan.get('cache_sha256')
        elif plan.get('kind') == 'published-label-replacement':
            paper = plan.get('paper', {})
            proof = paper.get('addition_proof') or {}
            operation_id = paper.get('addition_operation_id')
            source_sha, cache_sha = proof.get('expected_source_sha256'), proof.get('expected_cache_sha256')
        else:
            operation_id, source_sha, cache_sha = None, None, None
        cache[key] = (operation_id, plan_id, grouped, source_sha, cache_sha,
                      plan.get('phase') == 'applied')
        if isinstance(cache, OrderedDict):
            cache.move_to_end(key)
            while len(cache) > 6:
                cache.popitem(last=False)
    operation_id, plan_id, points, source_sha, cache_sha, applied = cache[key]
    if isinstance(cache, OrderedDict):
        cache.move_to_end(key)
    # A prepared journal cannot establish ownership of a published point.
    if (not applied or plan_id != paper_id or chunk_index not in points
            or len(points[chunk_index]) != 1):
        return operation_id, None, source_sha, cache_sha
    return operation_id, points[chunk_index][0], source_sha, cache_sha


def _fts_receipt(fts, operation_id, point_id, paper_id, payload):
    if not operation_id or not labels._exists(fts, 'dtox_cached_point_receipts'):
        return 'receipt_missing'
    owners = fts.execute('SELECT arxiv_id,row_id FROM pipeline_fts_points WHERE point_id=?', (point_id,)).fetchall()
    if len(owners) != 1 or owners[0]['arxiv_id'] != paper_id:
        return 'fts_owner_missing'
    row_id = owners[0]['row_id']
    receipts = fts.execute('SELECT row_id FROM dtox_cached_point_receipts WHERE operation_id=? AND point_id=?',
                           (operation_id, point_id)).fetchall()
    records = fts.execute("SELECT 1 FROM pipeline_fts_rows WHERE table_name='chunks' AND arxiv_id=? AND row_id=?",
                          (paper_id, row_id)).fetchall()
    row = labels._fts_tuple(fts, row_id)
    if (len(receipts) != 1 or receipts[0][0] != row_id or len(records) != 1 or row is None
            or row[1] != point_id or row[2] != paper_id or row[0] != payload.get('text')
            or row[3] != payload.get('section_type')
            or row[4] != payload.get('element_type', 'prose')):
        return 'fts_receipt_mismatch'
    return 'valid'


def audit(args):
    policy_sha = policy_identity()
    extractor_path = Path(extractor.__file__).resolve()
    extractor_sha = sha256(extractor_path.read_bytes().replace(b'\r\n', b'\n'))
    census = connect_ro(args.census_db)
    projection = connect_ro(args.projection_db)
    ledger_ro = connect_ro(args.point_run_db)
    fts = connect_ro(args.fts_db)
    rowmap = connect_ro(args.rowmap_db)
    cm, pm, rm = map(driver._meta, (census, projection, rowmap))
    if (cm.get('run_complete') != '1' or pm.get('run_complete') != '1' or rm.get('complete') != '1'
            or pm.get('census_identity') != driver._identity(args.census_db)
            or pm.get('rowmap_identity') != driver._identity(args.rowmap_db)):
        raise RuntimeError('completed census/projection/rowmap identities required')
    restore._catalog(fts)
    out = init_audit(args.audit_db)
    scanned, skipped, verified = 0, 0, 0
    try:
        papers = ledger_ro.execute('SELECT paper_id,COUNT(*) AS n FROM targets GROUP BY paper_id ORDER BY paper_id')
        target_total = ledger_ro.execute('SELECT COUNT(*) FROM targets').fetchone()[0]
        paper_total = ledger_ro.execute('SELECT COUNT(DISTINCT paper_id) FROM targets').fetchone()[0]
        verified_total = ledger_ro.execute("SELECT COUNT(*) FROM targets WHERE status='verified'").fetchone()[0]
        selected_papers = set(getattr(args, 'paper', None) or [])
        out.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)', [
            ('full_run_complete', '0'), ('scoped_run_complete', '0'),
            ('policy_sha256', policy_sha), ('extractor_sha256', extractor_sha),
            ('frozen_target_count', str(target_total))])
        out.commit()
        for target_paper in papers:
            if args.max_papers is not None and scanned >= args.max_papers:
                break
            aid = target_paper['paper_id']
            if selected_papers and aid not in selected_papers:
                continue
            row = census.execute('SELECT * FROM papers WHERE paper_id=?', (aid,)).fetchone()
            proj = projection.execute('SELECT * FROM papers WHERE paper_id=?', (aid,)).fetchone()
            targets = ledger_ro.execute('SELECT chunk_index,status,journal_path FROM targets WHERE paper_id=? ORDER BY chunk_index', (aid,)).fetchall()
            ledger_fingerprint = _ledger_fingerprint(targets)
            if row is None or proj is None:
                diagnostic = 'census_or_projection_row_missing'
                out.execute('INSERT OR REPLACE INTO papers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (aid, None, ledger_fingerprint, None, None, None, policy_sha, extractor_sha, None, 0, 0, diagnostic, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
                for t in targets:
                    out.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (aid, t['chunk_index'], t['status'], OPEN, diagnostic, OPEN, diagnostic, 0, '', None, None))
                out.commit(); scanned += 1
                continue
            try:
                projection_indices = json.loads(proj['cache_backed_missing_current_indices_json'])
                projected = (proj['projection_status'] == 'cache_backed_current_projection_missing'
                             and proj['census_fingerprint'] == row['input_fingerprint']
                             and proj['cache_sha256'] == row['cache_sha256']
                             and {int(t['chunk_index']) for t in targets}.issubset(set(projection_indices)))
            except Exception:
                projected = False
            if not projected:
                diagnostic = 'frozen_projection_binding_mismatch'
                out.execute('INSERT OR REPLACE INTO papers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (aid, row['input_fingerprint'], ledger_fingerprint, None, None, row['cache_sha256'], policy_sha, extractor_sha,
                     row['source_location'], 0, 0, diagnostic, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
                for t in targets:
                    out.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (aid, t['chunk_index'], t['status'], OPEN, diagnostic, OPEN, diagnostic, 0, '',
                         None, None if t['status'] == 'verified' else 'not_applicable'))
                out.commit(); scanned += 1
                continue
            try:
                cache_path, cache_sha, source_path, source, by_index, route = driver._inputs(dict(row), args)
                source_sha = sha256(source)
                previous = out.execute('SELECT input_fingerprint,ledger_fingerprint,source_sha256,cache_sha256,frozen_cache_sha256,policy_sha256,extractor_sha256,source_location FROM papers WHERE paper_id=?', (aid,)).fetchone()
                candidate_count = out.execute('SELECT COUNT(*) FROM candidates WHERE paper_id=?', (aid,)).fetchone()[0]
                if (previous is not None and tuple(previous) == (row['input_fingerprint'], ledger_fingerprint,
                        source_sha, cache_sha, row['cache_sha256'], policy_sha, extractor_sha,
                        row['source_location']) and candidate_count == len(targets)):
                    skipped += 1
                    continue
                chunks, elements = restore.parse_source_views(source, route)
                diag = None
            except Exception as exc:
                cache_sha = source_sha = ''
                chunks, elements, by_index = [], None, {}
                diag = type(exc).__name__ + ': ' + str(exc)[:220]
            out.execute('INSERT OR REPLACE INTO papers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (aid, row['input_fingerprint'], ledger_fingerprint, source_sha, cache_sha, row['cache_sha256'], policy_sha, extractor_sha,
                 row['source_location'], len(chunks), len(elements or []), diag,
                 time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
            for t in targets:
                idx = t['chunk_index']
                chunk = by_index.get(idx)
                if diag or chunk is None:
                    outcome, reason, occurrences_count = OPEN, diag or 'cache_index_missing', 0
                else:
                    outcome, reason, occurrences_count = classify_candidate(chunk, chunks, elements)
                out.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (aid, idx, t['status'], outcome, reason, outcome, reason, occurrences_count,
                     sha256(chunk['text'].encode('utf-8')) if chunk and isinstance(chunk.get('text'), str) else '',
                     chunk.get('element_type', 'prose') if chunk else None,
                     None if t['status'] == 'verified' else 'not_applicable'))
            out.commit()
            scanned += 1
            if scanned % 100 == 0:
                print(json.dumps({'papers_scanned': scanned}), flush=True)

        # Verified additions need a live payload and a direct point-scoped FTS receipt check.
        audited_targets = {tuple(r) for r in out.execute('SELECT paper_id,chunk_index FROM candidates')}
        verified_targets = [t for t in ledger_ro.execute("SELECT paper_id,chunk_index,journal_path FROM targets WHERE status='verified' ORDER BY paper_id,chunk_index")
                            if (t['paper_id'], t['chunk_index']) in audited_targets]
        journal_cache = OrderedDict()
        id_data = {}
        for t in verified_targets:
            aid, idx = t['paper_id'], t['chunk_index']
            pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{aid}#{idx}'))
            try:
                operation_id, expected, journal_source_sha, journal_cache_sha = _journal_point(t['journal_path'], aid, idx, journal_cache)
                id_data[pid] = (aid, idx, operation_id, expected, t['journal_path'], journal_source_sha, journal_cache_sha)
            except Exception:
                id_data[pid] = (aid, idx, None, None, t['journal_path'], None, None)
        ids = list(id_data)
        client = QdrantReadOnly()
        for start in range(0, len(ids), args.qdrant_batch):
            batch = ids[start:start + args.qdrant_batch]
            live = {}
            try:
                for point in client.retrieve('papers_fulltext', batch):
                    live[str(point.get('id'))] = point
            except Exception as exc:
                diagnostic = 'qdrant_read_error:' + type(exc).__name__
                out.executemany('UPDATE candidates SET outcome=?,reason=?,receipt_state=? WHERE paper_id=? AND chunk_index=?',
                    [(OPEN, diagnostic, diagnostic, id_data[pid][0], id_data[pid][1]) for pid in batch])
                out.commit()
                continue
            for pid in batch:
                aid, idx, operation_id, expected, journal_path, journal_source_sha, journal_cache_sha = id_data[pid]
                point = live.get(pid)
                receipt_state = 'valid'
                if expected is None:
                    receipt_state = 'journal_missing_or_mismatch'
                elif point is None:
                    receipt_state = 'qdrant_point_missing'
                else:
                    payload = point.get('payload') or {}
                    expected_payload = expected.get('payload', {})
                    if (expected.get('id') != pid or point.get('id') != pid or payload.get('arxiv_id') != aid
                            or payload.get('chunk_index') != idx
                            or payload.get('text') != expected_payload.get('text')
                            or payload.get('element_type', 'prose') != expected_payload.get('element_type', 'prose')):
                        receipt_state = 'payload_identity_mismatch'
                    else:
                        receipt_state = _fts_receipt(fts, operation_id, pid, aid, payload)
                        existing = out.execute('SELECT source_occurrences,text_sha256,source_outcome,element_type FROM candidates WHERE paper_id=? AND chunk_index=?',
                                                (aid, idx)).fetchone()
                        paper_hashes = out.execute('SELECT source_sha256,cache_sha256,frozen_cache_sha256 FROM papers WHERE paper_id=?',
                                                   (aid,)).fetchone()
                        if (receipt_state != 'valid' or existing is None or existing[0] != 1
                                or existing[2] not in ('CURRENTELIGIBLE', 'EXCLUDED')
                                or payload.get('element_type', 'prose') != existing[3]
                                or paper_hashes is None or journal_source_sha != paper_hashes[0]
                                or journal_cache_sha not in (paper_hashes[1], paper_hashes[2])
                                or not isinstance(payload.get('text'), str)
                                or sha256(payload['text'].encode('utf-8')) != existing[1]):
                            out.execute('UPDATE candidates SET outcome=?,reason=? WHERE paper_id=? AND chunk_index=?',
                                (OPEN, 'verified_payload_receipt_or_source_binding_unverified', aid, idx))
                        else:
                            admitted = extractor.should_embed(payload)
                            outcome = 'CURRENTELIGIBLE' if admitted else 'EXCLUDED'
                            reason = ('current_policy_admits_unique_source' if admitted else
                                      'boilerplate_policy' if boilerplate.is_boilerplate_chunk(
                                          payload.get('text'), payload.get('section_title'),
                                          payload.get('section_type'), payload.get('element_type'))
                                      else 'current_extractor_filter')
                            out.execute('UPDATE candidates SET outcome=?,reason=?,source_occurrences=? WHERE paper_id=? AND chunk_index=?',
                                (outcome, reason, existing[0], aid, idx))
                if receipt_state != 'valid':
                    out.execute('UPDATE candidates SET outcome=?,reason=? WHERE paper_id=? AND chunk_index=?',
                                (OPEN, 'verified_point_audit:' + receipt_state, aid, idx))
                out.execute('UPDATE candidates SET receipt_state=? WHERE paper_id=? AND chunk_index=?',
                            (receipt_state, aid, idx))
            out.commit()
            verified += len(batch)
            if verified % 2000 == 0:
                print(json.dumps({'verified_points_checked': verified}), flush=True)

        full_scope = (not selected_papers and args.max_papers is None
                      and out.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] == target_total)
        out.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)', [
            ('full_run_complete', str(int(full_scope))), ('scoped_run_complete', '1')])
        out.commit()
        counts = [dict(outcome=r[0], source_outcome=r[1], reason=r[2], ledger_status=r[3], receipt_state=r[4], candidates=r[5], papers=r[6])
                  for r in out.execute("SELECT outcome,source_outcome,reason,ledger_status,COALESCE(receipt_state,'-') AS receipt_state,COUNT(*),COUNT(DISTINCT paper_id) FROM candidates GROUP BY outcome,source_outcome,reason,ledger_status,receipt_state ORDER BY outcome,source_outcome,reason,ledger_status,receipt_state")]
        samples = [dict(r) for r in out.execute('SELECT paper_id,chunk_index,ledger_status,outcome,reason,source_outcome,source_reason,receipt_state FROM candidates ORDER BY outcome,paper_id,chunk_index LIMIT 24')]
        print(json.dumps({'policy_sha256': policy_sha, 'extractor_sha256': extractor_sha,
                          'frozen_target_count': target_total, 'frozen_paper_count': paper_total,
                          'verified_target_count': verified_total,
                          'papers_scanned': scanned, 'papers_reused_from_audit_db': skipped,
                          'verified_points_attempted': verified,
                          'outcomes': counts, 'sample_ids': samples,
                          'full_corpus_quality_complete': False}, ensure_ascii=False))
    finally:
        out.close(); ledger_ro.close(); projection.close(); census.close(); fts.close(); rowmap.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, default=Path('/opt/dtox-corpus-quality-20261013'))
    p.add_argument('--paper', action='append', help='Optional exact document IDs for a bounded canary')
    p.add_argument('--root', type=Path, default=Path('/opt/dtox-research'))
    p.add_argument('--audit-db', type=Path, default=Path('/opt/dtox-corpus-quality-20261013/index-admission-audit.sqlite'))
    p.add_argument('--census-db', type=Path, default=Path('/opt/dtox-corpus-quality-20261013/census.sqlite'))
    p.add_argument('--projection-db', type=Path, default=Path('/opt/dtox-corpus-quality-20261013/projection.sqlite'))
    p.add_argument('--rowmap-db', type=Path, default=Path('/opt/dtox-corpus-quality-20261013/fts-rowmap.sqlite'))
    p.add_argument('--point-run-db', type=Path, default=Path('/opt/dtox-corpus-quality-20261013/point-run.sqlite'))
    p.add_argument('--fts-db', type=Path, default=Path('/opt/dtox-research/fts.db'))
    p.add_argument('--cache-dir', type=Path, default=Path('/opt/dtox-research/chunk_cache'))
    p.add_argument('--fulltext-cache-dir', type=Path, default=Path('/opt/dtox-research/fulltext_cache'))
    p.add_argument('--latex-cache-dir', type=Path, default=Path('/opt/dtox-research/latex_cache'))
    p.add_argument('--label-run-db', type=Path, default=Path('/opt/dtox-corpus-quality-20261013/label-run.sqlite'))
    p.add_argument('--qdrant-batch', type=int, default=100)
    p.add_argument('--max-papers', type=int, help='Optional paper limit for a resumable canary scan')
    audit(p.parse_args())


if __name__ == '__main__':
    main()
