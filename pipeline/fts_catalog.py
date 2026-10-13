"""Exact UUID-to-rowid ownership for the shared FTS projection."""
from contextlib import contextmanager


class CatalogBlocked(RuntimeError):
    pass


@contextmanager
def _savepoint(fts):
    owns_transaction = not fts.in_transaction
    if owns_transaction:
        fts.execute("BEGIN IMMEDIATE")
    fts.execute("SAVEPOINT fts_catalog_write")
    try:
        yield
    except BaseException:
        fts.execute("ROLLBACK TO fts_catalog_write")
        fts.execute("RELEASE fts_catalog_write")
        if owns_transaction:
            fts.rollback()
        raise
    else:
        fts.execute("RELEASE fts_catalog_write")
        if owns_transaction:
            fts.commit()


def ensure_catalog(fts):
    fts.execute("CREATE TABLE IF NOT EXISTS pipeline_fts_papers (table_name TEXT NOT NULL, arxiv_id TEXT NOT NULL, PRIMARY KEY(table_name,arxiv_id))")
    fts.execute("CREATE TABLE IF NOT EXISTS pipeline_fts_rows (table_name TEXT NOT NULL, arxiv_id TEXT NOT NULL, row_id INTEGER NOT NULL, PRIMARY KEY(table_name,arxiv_id,row_id))")
    fts.execute("CREATE INDEX IF NOT EXISTS pipeline_fts_rows_paper ON pipeline_fts_rows(table_name,arxiv_id)")
    fts.execute("CREATE TABLE IF NOT EXISTS pipeline_fts_points (point_id TEXT PRIMARY KEY, arxiv_id TEXT NOT NULL, row_id INTEGER UNIQUE NOT NULL)")
    fts.execute("CREATE INDEX IF NOT EXISTS pipeline_fts_points_paper ON pipeline_fts_points(arxiv_id)")
    fts.execute("CREATE TABLE IF NOT EXISTS pipeline_fts_scope (singleton INTEGER PRIMARY KEY CHECK(singleton=1), complete INTEGER NOT NULL)")
    if not fts.execute("SELECT 1 FROM pipeline_fts_scope WHERE singleton=1").fetchone():
        empty = fts.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is None
        fts.execute("INSERT INTO pipeline_fts_scope VALUES (1,?)", (int(empty),))


def _scope_complete(fts):
    ensure_catalog(fts)
    return bool(fts.execute("SELECT complete FROM pipeline_fts_scope WHERE singleton=1").fetchone()[0])


def _owned_rows(fts, paper):
    """Validate only recorded rowids; unrecorded legacy rows wait for migration."""
    rows = fts.execute("SELECT row_id FROM pipeline_fts_rows WHERE table_name='chunks' AND arxiv_id=?", (paper,)).fetchall()
    mapped_rows = fts.execute("SELECT row_id FROM pipeline_fts_points WHERE arxiv_id=?", (paper,)).fetchall()
    if {r[0] for r in rows} != {r[0] for r in mapped_rows}:
        raise CatalogBlocked("selected-paper row ownership is incomplete")
    for (rid,) in rows:
        actual = fts.execute("SELECT point_id,arxiv_id FROM chunks WHERE rowid=?", (rid,)).fetchone()
        mapped = fts.execute("SELECT arxiv_id,row_id FROM pipeline_fts_points WHERE point_id=?", (str(actual[0]),)).fetchone() if actual else None
        if actual is None or str(actual[1]) != paper or not mapped or tuple(mapped) != (paper, rid):
            raise CatalogBlocked("corrupt or incomplete selected-paper catalog")
    return [r[0] for r in rows]


def _write_snapshot(fts, paper, points, fresh):
    ensure_catalog(fts)
    owned = _owned_rows(fts, paper)
    marked = fts.execute("SELECT 1 FROM pipeline_fts_papers WHERE table_name='chunks' AND arxiv_id=?", (paper,)).fetchone()
    if marked and not owned and points:
        raise CatalogBlocked("known paper has no owned rows")
    if not marked and not fresh:
        raise CatalogBlocked("unmapped legacy paper cannot be replaced")
    old = {}
    for rid in owned:
        pid, = fts.execute("SELECT point_id FROM chunks WHERE rowid=?", (rid,)).fetchone()
        old[str(pid)] = fts.execute("SELECT text,arxiv_id,section_type,element_type,layers,year FROM chunks WHERE rowid=?", (rid,)).fetchone()
    incoming = {}
    for p in points:
        payload = p.get("payload") or {}
        if str(payload.get("arxiv_id") or "") != paper:
            raise CatalogBlocked("snapshot payload arxiv_id mismatch")
        incoming[str(p["id"])] = (payload.get("text") or "", paper, payload.get("section_type"),
            payload.get("element_type"), str(payload.get("layers") or ""), payload.get("year"))
    if marked and not owned and not incoming:
        return True
    if marked and old == incoming:
        return True
    if marked and fresh:
        raise CatalogBlocked("fresh replay differs from the already-owned snapshot")
    for p in points:
        payload = p.get("payload") or {}
        pid = str(p["id"])
        owner = fts.execute("SELECT arxiv_id FROM pipeline_fts_points WHERE point_id=?", (pid,)).fetchone()
        if owner and owner[0] != paper:
            raise CatalogBlocked("point UUID owned by another paper")
    if marked:
        fts.executemany("DELETE FROM chunks WHERE rowid=?", [(rid,) for rid in owned])
        fts.execute("DELETE FROM pipeline_fts_points WHERE arxiv_id=?", (paper,))
        fts.execute("DELETE FROM pipeline_fts_rows WHERE table_name='chunks' AND arxiv_id=?", (paper,))
    for p in points:
        payload = p.get("payload") or {}
        cursor = fts.execute("INSERT INTO chunks(text,point_id,arxiv_id,section_type,element_type,layers,year) VALUES (?,?,?,?,?,?,?)",
            (payload.get("text") or "", str(p["id"]), paper, payload.get("section_type"), payload.get("element_type"), str(payload.get("layers") or ""), payload.get("year")))
        fts.execute("INSERT INTO pipeline_fts_points VALUES (?,?,?)", (str(p["id"]), paper, cursor.lastrowid))
        fts.execute("INSERT INTO pipeline_fts_rows VALUES ('chunks',?,?)", (paper, cursor.lastrowid))
    fts.execute("INSERT OR IGNORE INTO pipeline_fts_papers VALUES ('chunks',?)", (paper,))
    return True


def replace_paper(fts, paper_id, points, fresh=False):
    paper, points = str(paper_id), list(points)
    ids = [str(p["id"]) for p in points]
    if len(ids) != len(set(ids)):
        raise CatalogBlocked("duplicate point IDs in snapshot")
    with _savepoint(fts):
        return _write_snapshot(fts, paper, points, fresh)


def append_points(fts, points):
    points = list(points)
    with _savepoint(fts):
        if not _scope_complete(fts):
            raise CatalogBlocked("partial legacy catalog blocks bulk append")
        inserted = 0
        for p in points:
            pid, payload = str(p["id"]), p.get("payload") or {}
            paper, text = str(payload.get("arxiv_id") or ""), payload.get("text")
            if not paper or not text:
                continue
            prior = fts.execute("SELECT arxiv_id,row_id FROM pipeline_fts_points WHERE point_id=?", (pid,)).fetchone()
            if prior:
                actual = fts.execute("SELECT point_id,text,arxiv_id,section_type,element_type,layers,year FROM chunks WHERE rowid=?", (prior[1],)).fetchone()
                recorded = fts.execute("SELECT 1 FROM pipeline_fts_rows WHERE table_name='chunks' "
                                       "AND arxiv_id=? AND row_id=?", (paper, prior[1])).fetchone()
                projection = (text, paper, payload.get("section_type"), payload.get("element_type"),
                              str(payload.get("layers") or ""), payload.get("year"))
                if prior[0] != paper or actual != (pid,) + projection or not recorded:
                    raise CatalogBlocked("point replay conflicts with catalog")
                continue
            cursor = fts.execute("INSERT INTO chunks(text,point_id,arxiv_id,section_type,element_type,layers,year) VALUES (?,?,?,?,?,?,?)",
                (text, pid, paper, payload.get("section_type"), payload.get("element_type"), str(payload.get("layers") or ""), payload.get("year")))
            fts.execute("INSERT INTO pipeline_fts_points VALUES (?,?,?)", (pid, paper, cursor.lastrowid))
            fts.execute("INSERT OR IGNORE INTO pipeline_fts_papers VALUES ('chunks',?)", (paper,))
            fts.execute("INSERT INTO pipeline_fts_rows VALUES ('chunks',?,?)", (paper, cursor.lastrowid))
            inserted += 1
        return inserted
