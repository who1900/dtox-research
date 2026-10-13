"""Resumable ownership reconciliation; never changes FTS content or frozen map.

The caller must freeze every FTS writer throughout the run and all resumes.
Per-paper completeness is published only with final global completeness.
"""
import json
import math
from pathlib import Path
import sqlite3
import time

from pipeline.fts_catalog import ensure_catalog


class ReconciliationBlocked(RuntimeError):
    pass


def _reject(conn, sql, message):
    if conn.execute(sql).fetchone() is not None:
        raise ReconciliationBlocked(message)


def run(fts_db, rowmap_db, *, batch_size=50000, max_seconds=60,
        writers_frozen=False):
    """Fill bounded batches and return checkpoint/status; safely callable again."""
    if not writers_frozen:
        raise ReconciliationBlocked('all FTS writers must be frozen, including between resumes')
    if type(batch_size) is not int or not 1 <= batch_size <= 200000:
        raise ValueError('batch_size must be 1..200000')
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError('positive finite max_seconds required')
    fts_path, map_path = Path(fts_db).resolve(), Path(rowmap_db).resolve()
    if not fts_path.is_file() or not map_path.is_file() or fts_path == map_path:
        raise ReconciliationBlocked('existing distinct database files required')
    conn = sqlite3.connect(fts_path.as_uri()+'?mode=rw', uri=True, timeout=60)
    # UUID inserts touch many index pages. Keep a bounded batch in memory
    # instead of repeatedly spilling the same pages with SQLite's 2 MiB cache.
    conn.execute('PRAGMA cache_size=-131072')
    conn.execute('PRAGMA temp_store=MEMORY')
    conn.execute('ATTACH DATABASE ? AS frozen', (map_path.as_uri()+'?mode=ro',))
    deadline = time.monotonic()+max_seconds
    batches = 0
    try:
        meta = dict(conn.execute('SELECT key,value FROM frozen.meta'))
        stat = fts_path.stat()
        if (meta.get('complete') != '1'
                or json.loads(meta.get('source_identity', 'null')) != [str(fts_path), stat.st_dev, stat.st_ino]):
            raise ReconciliationBlocked('complete frozen map bound to this FTS file required')
        ms = map_path.stat()
        binding = json.dumps([str(map_path), ms.st_dev, ms.st_ino, ms.st_size, ms.st_mtime_ns])
        conn.execute('BEGIN IMMEDIATE')
        ensure_catalog(conn)
        conn.execute('CREATE TABLE IF NOT EXISTS dtox_fts_reconciliation(singleton INTEGER PRIMARY KEY CHECK(singleton=1), binding TEXT NOT NULL,last_rowid INTEGER NOT NULL,phase TEXT NOT NULL)')
        prior = conn.execute('SELECT binding,last_rowid,phase FROM dtox_fts_reconciliation WHERE singleton=1').fetchone()
        if prior and prior[0] != binding:
            raise ReconciliationBlocked('frozen reconciliation input changed')
        if not prior:
            conn.execute("INSERT INTO dtox_fts_reconciliation VALUES(1,?,0,'filling')", (binding,))
            conn.execute('UPDATE pipeline_fts_scope SET complete=0 WHERE singleton=1')
        # An empty receipt relation permits reconciliation before any restoration.
        receipt_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='dtox_cached_point_receipts' AND type='table'").fetchone()
        if receipt_exists:
            receipt_relation = 'main.dtox_cached_point_receipts'
        else:
            conn.execute('CREATE TEMP TABLE empty_receipts(point_id TEXT,row_id INTEGER)')
            receipt_relation = 'temp.empty_receipts'
        conn.commit()
        conn.execute('CREATE TEMP TABLE batch(row_id INTEGER PRIMARY KEY,point_id TEXT NOT NULL UNIQUE,arxiv_id TEXT NOT NULL)')
        version = conn.execute('PRAGMA data_version').fetchone()[0]
        while time.monotonic() < deadline:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('PRAGMA data_version').fetchone()[0] != version:
                raise ReconciliationBlocked('another connection wrote FTS during reconciliation')
            checkpoint, phase = conn.execute('SELECT last_rowid,phase FROM dtox_fts_reconciliation WHERE singleton=1').fetchone()
            if phase == 'complete':
                conn.commit()
                return dict(phase='complete', last_rowid=checkpoint, batches=batches)
            conn.execute('DELETE FROM batch')
            try:
                conn.execute('INSERT INTO batch SELECT rowid,point_id,arxiv_id FROM chunks WHERE rowid>? ORDER BY rowid LIMIT ?', (checkpoint,batch_size))
            except sqlite3.IntegrityError as exc:
                raise ReconciliationBlocked('duplicate or null live identities in batch') from exc
            size, maximum = conn.execute('SELECT COUNT(*),MAX(row_id) FROM batch').fetchone()
            if not size:
                _finalize(conn, receipt_relation)
                conn.commit()
                return dict(phase='complete', last_rowid=checkpoint, batches=batches)
            _reject(conn, 'SELECT 1 FROM batch b LEFT JOIN frozen.rows m ON m.rowid=b.row_id '
                    'WHERE m.rowid IS NOT NULL AND (m.point_id IS NOT b.point_id OR m.arxiv_id IS NOT b.arxiv_id) LIMIT 1', 'frozen row identity conflict')
            _reject(conn, 'SELECT 1 FROM batch b LEFT JOIN frozen.rows m ON m.rowid=b.row_id '
                    'LEFT JOIN '+receipt_relation+' r ON r.point_id=b.point_id AND r.row_id=b.row_id '
                    'LEFT JOIN pipeline_fts_points p ON p.point_id=b.point_id AND p.row_id=b.row_id AND p.arxiv_id=b.arxiv_id '
                    'WHERE m.rowid IS NULL AND (r.point_id IS NULL OR p.point_id IS NULL) LIMIT 1', 'unproven post-census live row')
            # Keep the bounded batch outermost; a normal JOIN can make SQLite
            # rescan the growing global catalog for every batch.
            _reject(conn, 'SELECT 1 FROM batch b CROSS JOIN pipeline_fts_points p ON p.point_id=b.point_id OR p.row_id=b.row_id '
                    'WHERE p.point_id!=b.point_id OR p.row_id!=b.row_id OR p.arxiv_id!=b.arxiv_id LIMIT 1', 'existing ownership conflict')
            try:
                conn.execute('INSERT OR IGNORE INTO pipeline_fts_points SELECT point_id,arxiv_id,row_id FROM batch')
                conn.execute("INSERT OR IGNORE INTO pipeline_fts_rows SELECT 'chunks',arxiv_id,row_id FROM batch")
            except sqlite3.IntegrityError as exc:
                raise ReconciliationBlocked('duplicate global point identity') from exc
            conn.execute('UPDATE dtox_fts_reconciliation SET last_rowid=? WHERE singleton=1', (maximum,))
            conn.commit()
            batches += 1
        checkpoint = conn.execute('SELECT last_rowid FROM dtox_fts_reconciliation WHERE singleton=1').fetchone()[0]
        return dict(phase='filling', last_rowid=checkpoint, batches=batches)
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def _finalize(conn, receipts):
    """Full bidirectional checks under the final write lock, then publish scope."""
    _reject(conn, 'SELECT 1 FROM chunks c LEFT JOIN pipeline_fts_points p ON p.row_id=c.rowid '
            'WHERE p.point_id IS NULL OR p.point_id!=c.point_id OR p.arxiv_id!=c.arxiv_id LIMIT 1', 'live/catalog mismatch')
    _reject(conn, 'SELECT 1 FROM pipeline_fts_points p LEFT JOIN chunks c ON c.rowid=p.row_id '
            'WHERE c.rowid IS NULL OR c.point_id!=p.point_id OR c.arxiv_id!=p.arxiv_id LIMIT 1', 'orphan point ownership')
    _reject(conn, "SELECT 1 FROM pipeline_fts_rows r LEFT JOIN pipeline_fts_points p ON p.row_id=r.row_id AND p.arxiv_id=r.arxiv_id WHERE r.table_name='chunks' AND p.point_id IS NULL LIMIT 1", 'orphan row ownership')
    _reject(conn, "SELECT 1 FROM pipeline_fts_points p LEFT JOIN pipeline_fts_rows r ON r.row_id=p.row_id AND r.arxiv_id=p.arxiv_id AND r.table_name='chunks' WHERE r.row_id IS NULL LIMIT 1", 'missing row ownership')
    _reject(conn, 'SELECT 1 FROM frozen.rows m LEFT JOIN chunks c ON c.rowid=m.rowid '
            'WHERE c.rowid IS NULL OR c.point_id IS NOT m.point_id OR c.arxiv_id IS NOT m.arxiv_id LIMIT 1', 'frozen row removed or changed')
    _reject(conn, 'SELECT 1 FROM chunks c LEFT JOIN frozen.rows m ON m.rowid=c.rowid '
            'LEFT JOIN '+receipts+' r ON r.point_id=c.point_id AND r.row_id=c.rowid '
            'WHERE m.rowid IS NULL AND r.point_id IS NULL LIMIT 1', 'live row lacks frozen or receipt provenance')
    _reject(conn, 'SELECT 1 FROM '+receipts+' r LEFT JOIN pipeline_fts_points p ON p.point_id=r.point_id AND p.row_id=r.row_id '
            'WHERE p.point_id IS NULL LIMIT 1', 'orphan restoration receipt')
    _reject(conn, 'SELECT 1 FROM '+receipts+' GROUP BY point_id HAVING COUNT(*)!=1 LIMIT 1', 'duplicate restoration receipt')
    _reject(conn, "SELECT 1 FROM pipeline_fts_papers m LEFT JOIN pipeline_fts_points p ON p.arxiv_id=m.arxiv_id WHERE m.table_name='chunks' AND p.point_id IS NULL LIMIT 1", 'orphan paper ownership')
    conn.execute("INSERT OR IGNORE INTO pipeline_fts_papers SELECT DISTINCT 'chunks',arxiv_id FROM pipeline_fts_points")
    conn.execute('UPDATE pipeline_fts_scope SET complete=1 WHERE singleton=1')
    conn.execute("UPDATE dtox_fts_reconciliation SET phase='complete' WHERE singleton=1")
