"""One-off exact-image index retirement. Caller owns locks, maintenance and files.

The archive is immutable input to deletion. Metadata exclusions are retained.
No schema setup, vacuum, service control or automatic recovery occurs here.
"""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

from ops.replace_published_labels import _scroll


class RetirementError(RuntimeError):
    pass


COLLECTIONS = ('papers_fulltext', 'papers_coarse_base')
TABLES = {
    'state': ('paper_publications', 'paper_active', 'publication_vector_claims',
              'paper_index_sync', 'paper_index_sync_progress'),
    'fts': ('chunks', 'pipeline_fts_points', 'pipeline_fts_rows',
            'pipeline_fts_papers', 'dtox_cached_point_receipts'),
    'papers_fts': ('papers_fts', 'pipeline_fts_rows', 'pipeline_fts_papers'),
}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _connect(path, ids):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.execute('CREATE TEMP TABLE retirement_ids(id TEXT PRIMARY KEY)')
    conn.executemany('INSERT INTO retirement_ids VALUES(?)', [(x,) for x in ids])
    return conn


def _tables(conn, family):
    installed = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [t for t in TABLES[family] if t in installed]


def _rows(conn, table):
    if table == 'dtox_cached_point_receipts':
        # Include receipts for uncatalogued raw FTS rows as well.
        predicate = ('t.point_id IN (SELECT c.point_id FROM chunks c JOIN retirement_ids s '
                     'ON c.arxiv_id=s.id) OR t.point_id IN '
                     '(SELECT p.point_id FROM pipeline_fts_points p JOIN retirement_ids s ON p.arxiv_id=s.id)')
    else:
        predicate = 't.arxiv_id IN (SELECT id FROM retirement_ids)'
    return conn.execute('SELECT t.rowid,t.* FROM ' + _q(table) +
                        ' t WHERE ' + predicate + ' ORDER BY t.rowid').fetchall()


def _count(client, collection):
    result = client.count(collection_name=collection, exact=True)
    return result['count'] if isinstance(result, dict) else result.count


def _points(client, collection, ids):
    result = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = pool.map(lambda aid: _scroll(client, collection, aid), ids)
        for aid, snapshot in zip(ids, snapshots):
            for pid, point in snapshot.items():
                if point['payload'].get('arxiv_id') != aid or str(pid) in result:
                    raise RetirementError('foreign or duplicate point ownership')
                if point['vector'] is None:
                    raise RetirementError('missing point vectors')
                result[str(pid)] = point
    return result


def export_indexes(ids, paths, client, output):
    """Export SQL values (including native BLOBs) and full point JSONL images.

    paths must provide state, fts, papers_fts. output must not exist.
    Caller must keep writers locked from export through deletion.
    """
    ids = sorted(set(ids))
    if not ids or any(not isinstance(x, str) or not x for x in ids):
        raise RetirementError('nonempty exact paper ID scope required')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    manifest = dict(schema=1, ids=ids, paths={k: str(Path(paths[k]).resolve()) for k in TABLES},
                    tables=[], collections={}, hashes={})
    archive = sqlite3.connect(output / 'rows.sqlite')
    try:
        for family in TABLES:
            with closing(_connect(paths[family], ids)) as conn:
                if family == 'state':
                    excluded = {r[0] for r in conn.execute("SELECT arxiv_id FROM papers WHERE status='excluded' AND arxiv_id IN (SELECT id FROM retirement_ids)")}
                    if excluded != set(ids):
                        raise RetirementError('scope contains missing or nonexcluded metadata')
                for table in _tables(conn, family):
                    columns = [r[1] for r in conn.execute('PRAGMA table_info(' + _q(table) + ')')]
                    schema = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
                    name = 'image_' + str(len(manifest['tables']))
                    archive.execute('CREATE TABLE ' + name + '(' + ','.join(_q(c) for c in ['__rowid'] + columns) + ')')
                    rows = _rows(conn, table)
                    archive.executemany('INSERT INTO ' + name + ' VALUES(' + ','.join('?' for _ in range(len(columns)+1)) + ')', rows)
                    manifest['tables'].append(dict(family=family, table=table, archive=name,
                                                   columns=columns, schema=schema, count=len(rows),
                                                   total=conn.execute('SELECT count(*) FROM ' + _q(table)).fetchone()[0]))
        archive.commit()
    finally:
        archive.close()
    for collection in COLLECTIONS:
        points = _points(client, collection, ids)
        with gzip.open(output / (collection + '.jsonl.gz'), 'wt', encoding='utf-8', compresslevel=1) as stream:
            for pid in sorted(points):
                stream.write(_json(points[pid]) + '\n')
        manifest['collections'][collection] = dict(count=len(points), total=_count(client, collection))
    for filename in ['rows.sqlite'] + [c + '.jsonl.gz' for c in COLLECTIONS]:
        manifest['hashes'][filename] = _sha(output / filename)
    (output / 'manifest.json').write_text(_json(manifest), encoding='utf-8')
    verify_archive(output)
    return manifest


def verify_archive(output):
    output = Path(output)
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['schema'] != 1:
        raise RetirementError('unsupported archive')
    for filename, digest in manifest['hashes'].items():
        if _sha(output / filename) != digest:
            raise RetirementError('archive hash mismatch: ' + filename)
    with closing(sqlite3.connect(output / 'rows.sqlite')) as archive:
        if archive.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RetirementError('archive integrity failure')
        for item in manifest['tables']:
            if archive.execute('SELECT count(*) FROM ' + item['archive']).fetchone()[0] != item['count']:
                raise RetirementError('archive row count mismatch')
    for collection, expected in manifest['collections'].items():
        points = _read_points(output, collection)
        if len(points) != expected['count']:
            raise RetirementError('archive point count mismatch')
        if any(p['vector'] is None or p['payload'].get('arxiv_id') not in manifest['ids'] for p in points.values()):
            raise RetirementError('invalid archived point ownership or vectors')
    return manifest


def _read_points(output, collection):
    result = {}
    with gzip.open(Path(output) / (collection + '.jsonl.gz'), 'rt', encoding='utf-8') as stream:
        for line in stream:
            point = json.loads(line)
            pid = str(point['id'])
            if pid in result:
                raise RetirementError('duplicate archived point')
            result[pid] = point
    return result


def delete_indexes(output, client, *, maintenance=False):
    """Delete verified exact before-images. Failure keeps caller's gate closed.

    This function does not resume a partially applied operation automatically.
    Restore/reconcile from the archive before retrying after partial failure.
    """
    if maintenance is not True:
        raise RetirementError('reader and writer maintenance required')
    output = Path(output)
    manifest = verify_archive(output)
    ids = manifest['ids']
    # Complete all preimage checks before the first mutation.
    points = {}
    for collection, expected in manifest['collections'].items():
        points[collection] = _read_points(output, collection)
        if _json(_points(client, collection, ids)) != _json(points[collection]):
            raise RetirementError('Qdrant preimage drift: ' + collection)
        if _count(client, collection) != expected['total']:
            raise RetirementError('Qdrant global count drift')
    with closing(sqlite3.connect(output / 'rows.sqlite')) as archive:
        for family in TABLES:
            with closing(_connect(manifest['paths'][family], ids)) as conn:
                if set(_tables(conn, family)) != {x['table'] for x in manifest['tables'] if x['family'] == family}:
                    raise RetirementError('SQL table inventory drift')
                if family == 'state' and conn.execute("SELECT count(*) FROM papers WHERE status='excluded' AND arxiv_id IN (SELECT id FROM retirement_ids)").fetchone()[0] != len(ids):
                    raise RetirementError('metadata exclusion drift')
                for item in (x for x in manifest['tables'] if x['family'] == family):
                    schema = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (item['table'],)).fetchone()[0]
                    if schema != item['schema'] or conn.execute('SELECT count(*) FROM ' + _q(item['table'])).fetchone()[0] != item['total']:
                        raise RetirementError('SQL schema or global count drift: ' + item['table'])
                    rows = archive.execute('SELECT * FROM ' + item['archive'] + ' ORDER BY __rowid').fetchall()
                    if _rows(conn, item['table']) != rows:
                        raise RetirementError('SQL preimage drift: ' + item['table'])
        for collection, records in points.items():
            values = [p['id'] for p in records.values()]
            for start in range(0, len(values), 200):
                client.delete(collection_name=collection, points_selector=values[start:start+200], wait=True)
        for family in TABLES:
            conn = sqlite3.connect(manifest['paths'][family])
            try:
                with conn:
                    for item in (x for x in manifest['tables'] if x['family'] == family):
                        rowids = archive.execute('SELECT __rowid FROM ' + item['archive']).fetchall()
                        conn.executemany('DELETE FROM ' + _q(item['table']) + ' WHERE rowid=?', rowids)
            finally:
                conn.close()
    for collection, expected in manifest['collections'].items():
        if _points(client, collection, ids) or _count(client, collection) != expected['total'] - expected['count']:
            raise RetirementError('Qdrant deletion verification failed')
    for family in TABLES:
        with closing(_connect(manifest['paths'][family], ids)) as conn:
            for table in _tables(conn, family):
                if _rows(conn, table):
                    raise RetirementError('SQL deletion verification failed: ' + table)
                item = next(x for x in manifest['tables'] if x['family'] == family and x['table'] == table)
                if conn.execute('SELECT count(*) FROM ' + _q(table)).fetchone()[0] != item['total'] - item['count']:
                    raise RetirementError('SQL global count delta mismatch: ' + table)
    return manifest
