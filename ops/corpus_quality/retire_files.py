"""Exact, receipt-backed local file and ORIGINAL metadata retirement."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing

from pipeline.document_cache import safe_id
from pipeline.originals import SCHEMA as ORIGINAL_SCHEMA


class RetirementFileError(RuntimeError):
    pass


def _phase_matches(status, phase):
    return phase == status or (status == 'pending' and phase == 'deferred')


def _provisional_status(status):
    return status in {'pending', 'deferred'}


def _hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RetirementFileError('candidate is not a plain regular file: ' + str(path))
    return path


def _inside(path, roots):
    path = Path(path).resolve(strict=False)
    for root in roots:
        root = Path(root).resolve(strict=True)
        if path == root or root in path.parents:
            return path
    raise RetirementFileError('candidate escapes configured roots: ' + str(path))


def _reject_link_below(path, root):
    path, root = Path(path), Path(root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RetirementFileError('candidate is outside its lexical root') from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RetirementFileError('symlink below configured root: ' + str(current))


def _file_record(path, roots, owner, kind, lexical_root=None):
    if lexical_root is not None:
        _reject_link_below(path, lexical_root)
    path = _inside(_regular(path), roots)
    st = path.stat()
    return dict(path=str(path), owner=owner, kind=kind, size=st.st_size,
                blocks=getattr(st, 'st_blocks', None), sha256=_hash(path))


def _load_journal(path):
    raw = Path(path).read_bytes()
    if raw.startswith(b'\x1f\x8b'):
        raw = gzip.decompress(raw)
    obj = json.loads(raw)
    if (not isinstance(obj, dict) or obj.get('schema') != 1
            or obj.get('kind') != 'published-label-replacement'
            or obj.get('operation') != 'whole-document'
            or not isinstance(obj.get('paper'), dict)
            or not isinstance(obj['paper'].get('arxiv_id'), str)
            or not isinstance(obj.get('phase'), str)):
        raise RetirementFileError('invalid native whole-document journal')
    return obj


def _journal_identity(journal):
    return journal['paper']['arxiv_id'], journal['phase']


def _original_schema(conn):
    expected = {sql.split(' (', 1)[0].split()[-1]: sql for sql in ORIGINAL_SCHEMA}
    actual = {name: sql for name, sql in conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if actual != expected:
        raise RetirementFileError('unexpected ORIGINAL manifest schema')


def _original_snapshot(conn, ids):
    marks = ','.join('?' for _ in ids)
    originals = conn.execute(f'SELECT * FROM originals WHERE canonical_id IN ({marks}) ORDER BY id', ids).fetchall()
    queues = conn.execute(f'SELECT * FROM acquisition_queue WHERE canonical_id IN ({marks}) ORDER BY canonical_id,binding_sha256', ids).fetchall()
    transitions = conn.execute(f'SELECT * FROM transitions WHERE canonical_id IN ({marks}) ORDER BY id', ids).fetchall()
    original_ids = [r[0] for r in originals]
    if original_ids:
        ids_marks = ','.join('?' for _ in original_ids)
        outside_queue = conn.execute(f'SELECT 1 FROM acquisition_queue WHERE original_id IN ({ids_marks}) AND canonical_id NOT IN ({marks}) LIMIT 1', (*original_ids, *ids)).fetchone()
        outside_transition = conn.execute(f'SELECT 1 FROM transitions WHERE original_id IN ({ids_marks}) AND canonical_id NOT IN ({marks}) LIMIT 1', (*original_ids, *ids)).fetchone()
        if outside_queue or outside_transition:
            raise RetirementFileError('excluded ORIGINAL row is referenced by another canonical ID')
    hashes = sorted({r[7] for r in originals})
    hash_set, id_set = set(hashes), set(ids)
    owners = {}
    for digest, owner in conn.execute('SELECT raw_sha256,canonical_id FROM originals'):
        if digest in hash_set:
            owners.setdefault(digest, set()).add(owner)
    sizes = dict(conn.execute('SELECT sha256,size FROM blobs'))
    exclusive = []
    for digest in hashes:
        refs = owners.get(digest, set())
        if refs and refs <= id_set:
            if digest not in sizes:
                raise RetirementFileError('ORIGINAL blob manifest row missing')
            exclusive.append(dict(sha256=digest, size=sizes[digest]))
    return dict(originals=[list(r) for r in originals], queues=[list(r) for r in queues],
                transitions=[list(r) for r in transitions], exclusive_blobs=exclusive)


def plan_files(ids, data_root, repair_base, originals_root, output_metadata_dir):
    """Snapshot exact local candidates and ORIGINAL SQL before-images."""
    ids = sorted(set(ids))
    if not ids or any(not isinstance(x, str) or not x for x in ids):
        raise RetirementFileError('nonempty canonical ID scope required')
    data = Path(data_root).resolve(strict=True)
    repair = Path(repair_base).resolve(strict=True)
    orig = Path(originals_root).resolve(strict=True)
    out = Path(output_metadata_dir).resolve(strict=True)
    roots = [data, repair, orig]
    for folder in ('chunk_cache', 'section_cache', 'fulltext_cache', 'latex_cache', 'publication_cache'):
        lexical = data / folder
        if lexical.exists():
            if not lexical.is_dir():
                raise RetirementFileError('configured cache root is not a directory: ' + str(lexical))
            physical = lexical.resolve(strict=True)
            if physical not in roots:
                roots.append(physical)
    state = data / 'state.db'
    if not state.is_file():
        raise RetirementFileError('state.db required for safe_id collision check')
    with closing(sqlite3.connect(state)) as conn:
        all_ids = [r[0] for r in conn.execute('SELECT arxiv_id FROM papers')]
        excluded = set(r[0] for r in conn.execute(
            "SELECT arxiv_id FROM papers WHERE status='excluded'"))
        if not set(ids) <= excluded:
            raise RetirementFileError('all targets must retain excluded metadata tombstones')
    names = {}
    for aid in all_ids:
        names.setdefault(safe_id(aid), []).append(aid)
    for aid in ids:
        if names.get(safe_id(aid)) != [aid] and set(names.get(safe_id(aid), [])) != {aid}:
            raise RetirementFileError('safe_id collision for ' + aid)

    candidates, dirs, missing_provisional_journals = [], [], []
    for aid in ids:
        name = safe_id(aid)
        for folder, suffix in (('chunk_cache', '.json'), ('section_cache', '.json'),
                               ('fulltext_cache', '.tex')):
            path = data / folder / (name + suffix)
            if path.exists():
                candidates.append(_file_record(path, roots, aid, folder, data / folder))
        latex = data / 'latex_cache' / name
        if latex.exists():
            if latex.is_symlink() or not latex.is_dir():
                raise RetirementFileError('unsafe LaTeX cache path')
            dirs.append(str(latex.resolve()))
            for filename in ('source.tex', 'references.json'):
                path = latex / filename
                if path.exists():
                    candidates.append(_file_record(path, roots, aid, 'latex_cache', data / 'latex_cache'))
            unknown = [p for p in latex.iterdir() if p.name not in {'source.tex', 'references.json'}]
            if unknown:
                raise RetirementFileError('unknown LaTeX cache entries')

        stage = repair / 'source-stage' / name
        if stage.exists():
            if stage.is_symlink() or not stage.is_dir():
                raise RetirementFileError('unsafe source-stage path')
            for root, subdirs, files in os.walk(stage, followlinks=False):
                rp = Path(root)
                rel = rp.relative_to(stage)
                allowed_dirs = ({'originals', 'sources'} if not rel.parts
                                else {re.sub(r'[^A-Za-z0-9._-]', '_', aid)} if rel.parts == ('sources',)
                                else set())
                if not set(subdirs) <= allowed_dirs:
                    raise RetirementFileError('unknown source-stage directory')
                dirs.extend(str((rp / d).resolve()) for d in subdirs)
                if any((rp / d).is_symlink() for d in subdirs):
                    raise RetirementFileError('symlink in source-stage')
                for fn in files:
                    path = rp / fn
                    if path.is_symlink() or not path.is_file():
                        raise RetirementFileError('unsafe source-stage entry')
                    relative = path.relative_to(stage).parts
                    valid_original = (len(relative) == 2 and relative[0] == 'originals'
                                      and re.fullmatch(r'[a-f0-9]{64}', relative[1]))
                    valid_source = (len(relative) == 3 and relative[0] == 'sources'
                                    and relative[1] == re.sub(r'[^A-Za-z0-9._-]', '_', aid)
                                    and re.fullmatch(r'[a-f0-9]{64}\.(?:tex|md)', relative[2]))
                    if not (valid_original or valid_source):
                        raise RetirementFileError('unknown source-stage file')
                    record = _file_record(path, roots, aid, 'source-stage')
                    expected_hash = relative[1] if valid_original else relative[2].rsplit('.', 1)[0]
                    if record['sha256'] != expected_hash:
                        raise RetirementFileError('source-stage hash name mismatch')
                    candidates.append(record)
            dirs.append(str(stage.resolve()))

    # Read the durable repair queue; its selected journal pointer determines ownership.
    queue_db = repair / 'whole-document-replacement.sqlite'
    if queue_db.exists():
        with closing(sqlite3.connect(queue_db)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'targets' not in tables:
                raise RetirementFileError('repair queue has no targets table')
            for aid in ids:
                row = conn.execute('SELECT status,journal_path FROM targets WHERE paper_id=?', (aid,)).fetchone()
                if not row or not row[1]:
                    continue
                status, selected = row
                if status in {'staging', 'prepared', 'applying'}:
                    raise RetirementFileError('unresolved repair journal state for ' + aid)
                selected_path = _inside(selected, [repair])
                options = [selected_path]
                for sibling in (selected_path.with_suffix(''), selected_path.with_suffix('.json'),
                                selected_path.with_suffix('.json.gz')):
                    if sibling not in options and sibling.exists():
                        options.append(sibling)
                if not selected_path.is_file() and not _provisional_status(status):
                    raise RetirementFileError('selected repair journal is missing for ' + aid)
                existing = [path for path in options if path.is_file()]
                if not existing and _provisional_status(status):
                    missing_provisional_journals.append(dict(paper_id=aid, path=str(selected_path), status=status))
                    continue
                for path in options:
                    if not path.is_file():
                        continue
                    journal = _load_journal(path)
                    journal_id, phase = _journal_identity(journal)
                    if journal_id != aid or not _phase_matches(status, phase):
                        raise RetirementFileError('journal ownership/phase mismatch for ' + aid)
                    candidates.append(_file_record(path, [repair], aid, 'journal'))

    # Protect retained publication paths named by any nonexcluded owner or retained journal.
    protected = set()
    owned_publications = []
    retained_artifacts = []
    queue_db = repair / 'whole-document-replacement.sqlite'
    if queue_db.exists():
        with closing(sqlite3.connect(queue_db)) as conn:
            for owner, status, selected in conn.execute('SELECT paper_id,status,journal_path FROM targets'):
                if owner in ids or not selected:
                    continue
                path = _inside(selected, [repair])
                if not path.is_file() and not _provisional_status(status):
                    raise RetirementFileError('retained selected journal is missing: ' + owner)
                if not path.is_file() and _provisional_status(status):
                    missing_provisional_journals.append(dict(paper_id=owner, path=str(path), status=status))
                    continue
                journal = _load_journal(path)
                journal_id, phase = _journal_identity(journal)
                if journal_id != owner or not _phase_matches(status, phase):
                    raise RetirementFileError('retained journal ownership/phase mismatch: ' + owner)
                retained_artifacts.extend(journal['paper'].get('retained_artifacts', []))
    pub_db = data / 'state.db'
    with closing(sqlite3.connect(pub_db)) as conn:
        if 'paper_publications' in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            for owner, path in conn.execute('SELECT arxiv_id,artifact_path FROM paper_publications'):
                if owner not in ids:
                    protected.add(str(Path(path).resolve(strict=False)))
                else:
                    owned_publications.append((owner, path))
    for owner, value in owned_publications:
        path = Path(value)
        if path.exists():
            candidates.append(_file_record(path, roots, owner, 'publication_cache', data / 'publication_cache'))
    for rec in candidates:
        if rec['kind'] == 'journal':
            journal = _load_journal(rec['path'])
            for item in journal['paper'].get('retained_artifacts', []):
                if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                    raise RetirementFileError('invalid retained_artifacts journal entry')
                path = Path(item['path'])
                if path.exists():
                    artifact = _file_record(path, roots, rec['owner'], 'publication_cache',
                                             data / 'publication_cache')
                    if item.get('sha256') is not None and item['sha256'] != artifact['sha256']:
                        raise RetirementFileError('retained publication artifact hash mismatch')
                    candidates.append(artifact)
    for item in retained_artifacts:
        protected.add(str(Path(item['path']).resolve(strict=False)))
    by_path = {}
    for item in candidates:
        old = by_path.get(item['path'])
        if old and (old['sha256'], old['size']) != (item['sha256'], item['size']):
            raise RetirementFileError('same candidate path has inconsistent preimage')
        by_path[item['path']] = item
    candidates = list(by_path.values())
    candidates = [r for r in candidates if r['path'] not in protected]

    manifest_path = orig / 'manifest.sqlite3'
    with closing(sqlite3.connect(manifest_path)) as conn:
        _original_schema(conn)
        originals = _original_snapshot(conn, ids)
    cas_preserved = []
    exclusive_hashes = {r['sha256'] for r in originals['exclusive_blobs']}
    for digest in sorted({row[7] for row in originals['originals']}):
        blob_path = orig / 'blobs' / digest
        manifest_row = next((r for r in originals['exclusive_blobs'] if r['sha256'] == digest), None)
        if manifest_row is None:
            with closing(sqlite3.connect(manifest_path)) as conn:
                size_row = conn.execute('SELECT size FROM blobs WHERE sha256=?', (digest,)).fetchone()
            if size_row is None:
                raise RetirementFileError('ORIGINAL blob manifest row missing')
            size = size_row[0]
        else:
            size = manifest_row['size']
        record = _file_record(blob_path, [orig], 'cas:' + digest, 'cas_preserved')
        if record['size'] != size or record['sha256'] != digest:
            raise RetirementFileError('ORIGINAL CAS hash/size mismatch: ' + digest)
        if digest in exclusive_hashes:
            record['kind'] = 'cas_exclusive'
            candidates.append(record)
        else:
            cas_preserved.append(record)
    # Shared CAS bytes stay live; only exclusively excluded owners supply candidates.
    plan = dict(schema=1, ids=ids, roots=[str(p) for p in roots], files=candidates,
                directories=sorted(set(dirs), key=lambda x: (x.count(os.sep), x), reverse=True),
                originals=originals, originals_manifest=str(manifest_path), cas_preserved=cas_preserved,
                missing_provisional_journals=missing_provisional_journals,
                created_for_review=True)
    target = out / 'file-retirement-plan.json'
    target.write_text(json.dumps(plan, sort_keys=True, ensure_ascii=False, indent=2), encoding='utf-8')
    return plan


def delete_files(plan):
    """Delete unchanged scoped files/ORIGINAL rows and preserve shared CAS blobs."""
    if isinstance(plan, (str, Path)):
        plan = json.loads(Path(plan).read_text(encoding='utf-8'))
    if plan.get('schema') != 1 or not plan.get('created_for_review'):
        raise RetirementFileError('invalid file retirement plan')
    roots = [Path(p).resolve(strict=True) for p in plan['roots']]
    for item in plan['files']:
        path = _inside(item['path'], roots)
        if not path.exists():
            continue
        _regular(path)
        st = path.stat()
        if st.st_size != item['size'] or _hash(path) != item['sha256']:
            raise RetirementFileError('file changed since plan: ' + str(path))
    for item in plan.get('cas_preserved', []):
        path = _inside(item['path'], roots)
        if not path.exists() or _hash(_regular(path)) != item['sha256'] or path.stat().st_size != item['size']:
            raise RetirementFileError('preserved ORIGINAL CAS changed since plan: ' + str(path))
    manifest = Path(plan['originals_manifest']).resolve(strict=True)
    with closing(sqlite3.connect(manifest)) as conn:
        _original_schema(conn)
        current = _original_snapshot(conn, plan['ids'])
        if current != plan['originals']:
            raise RetirementFileError('ORIGINAL SQL changed since plan')
        ids = plan['ids']
        marks = ','.join('?' for _ in ids)
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute(f'DELETE FROM transitions WHERE canonical_id IN ({marks})', ids)
            conn.execute(f'DELETE FROM acquisition_queue WHERE canonical_id IN ({marks})', ids)
            conn.execute(f'DELETE FROM originals WHERE canonical_id IN ({marks})', ids)
            retained_hashes = {r[0] for r in conn.execute('SELECT DISTINCT raw_sha256 FROM originals')}
            for blob in plan['originals']['exclusive_blobs']:
                if blob['sha256'] in retained_hashes:
                    raise RetirementFileError('CAS became shared during deletion')
                conn.execute('DELETE FROM blobs WHERE sha256=?', (blob['sha256'],))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    for item in plan['files']:
        path = Path(item['path'])
        if path.exists():
            path.unlink()
    for value in plan['directories']:
        path = _inside(value, roots)
        if path.exists():
            try:
                path.rmdir()
            except OSError as exc:
                raise RetirementFileError('directory is not empty after planned unlink: ' + str(path)) from exc
    return dict(files_deleted=len(plan['files']), original_rows=len(plan['originals']['originals']),
                cas_blobs_deleted=len(plan['originals']['exclusive_blobs']))
