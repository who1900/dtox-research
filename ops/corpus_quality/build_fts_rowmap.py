"""Build a resumable operational ID/rowid index; never copy corpus text."""
import argparse
import json
from pathlib import Path
import sqlite3
import time


def run(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or not source.is_file():
        raise ValueError('row map must be separate from the existing FTS database')
    output.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(output)
    dst.executescript('''
      PRAGMA journal_mode=WAL;
      PRAGMA synchronous=NORMAL;
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS rows(rowid INTEGER PRIMARY KEY,point_id TEXT NOT NULL,arxiv_id TEXT NOT NULL);
    ''')
    prior = dict(dst.execute('SELECT key,value FROM meta'))
    st = source.stat()
    identity = json.dumps([str(source), st.st_dev, st.st_ino])
    if prior and prior.get('source_identity') != identity:
        raise RuntimeError('row map belongs to a different source file')
    ceiling = int(prior.get('ceiling') or src.execute('SELECT MAX(rowid) FROM chunks').fetchone()[0] or 0)
    with dst:
        dst.executemany('INSERT OR IGNORE INTO meta VALUES (?,?)',
                        [('source_identity', identity), ('ceiling', str(ceiling)), ('complete', '0')])
    start = time.monotonic()
    cursor = dst.execute('SELECT COALESCE(MAX(rowid),0) FROM rows').fetchone()[0]
    stream = src.execute('SELECT rowid,point_id,arxiv_id FROM chunks WHERE rowid>? AND rowid<=? ORDER BY rowid',
                         (cursor, ceiling))
    while True:
        batch = stream.fetchmany(20000)
        if not batch:
            break
        if any(not isinstance(p, str) or not isinstance(a, str) for _, p, a in batch):
            raise RuntimeError('FTS row lacks an explicit point/paper identity')
        with dst:
            dst.executemany('INSERT INTO rows VALUES (?,?,?)', batch)
            dst.execute("INSERT OR REPLACE INTO meta VALUES ('cursor',?)", (str(batch[-1][0]),))
        if batch[-1][0] // 1000000 != cursor // 1000000:
            print(json.dumps({'phase': 'scan', 'rowid': batch[-1][0], 'ceiling': ceiling}), flush=True)
        cursor = batch[-1][0]
    src.close()
    dst.execute('CREATE INDEX IF NOT EXISTS rows_paper ON rows(arxiv_id)')
    with dst:
        dst.execute("UPDATE meta SET value='1' WHERE key='complete'")
    count = dst.execute('SELECT COUNT(*) FROM rows').fetchone()[0]
    dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    dst.close()
    result = {'complete': True, 'rows': count, 'max_rowid': cursor, 'source_max_rowid': ceiling,
              'seconds': round(time.monotonic() - start, 3), 'corpus_writes': False,
              'text_copied': False, 'output_bytes': output.stat().st_size}
    print(json.dumps(result), flush=True)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    run(args.source, args.output)
