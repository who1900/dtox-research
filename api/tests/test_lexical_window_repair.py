import sqlite3
import sys
import time

import pytest

from api import source_generation as generation
from api import source_representations as catalog
from api.tests.test_source_generation import corpus, discover
from api.tests.test_source_representations import representation, source


RANK_SQL = 'SELECT rowid,rank FROM representation_fts WHERE representation_fts MATCH ? ORDER BY rank'


def exhaustive_reference(reader, query, limit):
    match = catalog._safe_fts_query(query)
    if not match:
        return []
    groups = {}
    for hit in reader.conn.execute(RANK_SQL, (match,)):
        row = reader.conn.execute('SELECT * FROM representations WHERE id=?', (hit['rowid'],)).fetchone()
        record = catalog._record(row)
        if reader.qualify(record) is not None:
            sid = catalog._canonical(record['source_id'], reader.canonical)
            groups.setdefault(sid, []).append((hit['rank'], record['representation_id']))
    for items in groups.values():
        items.sort()
    heads = sorted(groups, key=lambda sid: (groups[sid][0][0], sid))[:limit]
    boundary = groups[heads[-1]][0][0] if len(heads) == limit else None
    selected = {sid: [item for item in groups[sid] if boundary is None or item[0] <= boundary][:limit]
                for sid in heads}
    result = []
    for depth in range(limit):
        for sid in heads:
            if depth < len(selected[sid]):
                score, rep_id = selected[sid][depth]
                result.append(dict(representation_id=rep_id, score=-score, generation=reader.generation))
                if len(result) == limit:
                    return result
    return result


class Cursor:
    def __init__(self, cursor, call):
        self.cursor, self.call = iter(cursor), call
        self.close_cursor = getattr(cursor, 'close', lambda: None)

    def __iter__(self):
        return self

    def __next__(self):
        row = next(self.cursor)
        self.call['fetched'] += 1
        return row

    def close(self):
        self.call['closed'] = True
        self.close_cursor()


class Connection:
    def __init__(self, connection, prefixes=None):
        self.connection, self.prefixes, self.calls = connection, prefixes, []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        if not sql.startswith('SELECT rowid,rank FROM representation_fts'):
            return self.connection.execute(sql, parameters)
        assert sql == RANK_SQL + ' LIMIT ?'
        assert 'OFFSET' not in sql
        call = dict(match=parameters[0], window=parameters[1], fetched=0, closed=False)
        self.calls.append(call)
        rows = (self.prefixes(parameters[1]) if self.prefixes is not None
                else self.connection.execute(sql, parameters))
        return Cursor(rows, call)


def compare(db, manifest, query='needle', limit=128, **filters):
    with catalog.CatalogReader(db, manifest['generation'], deadline=time.monotonic() + 15, **filters) as reader:
        expected = exhaustive_reference(reader, query, limit)
    with catalog.CatalogReader(db, manifest['generation'], deadline=time.monotonic() + 15, **filters) as reader:
        trace = reader.conn = Connection(reader.conn)
        actual = reader.lexical(query, limit)
        assert actual == expected
        assert len({row['representation_id'] for row in actual}) == len(actual) <= limit
        assert len(reader.metadata) <= 512 and len(reader.qualifiers) <= 128
        assert all(call['closed'] for call in trace.calls)
        return actual, trace.calls


def test_exact_or_expression_and_bm25_match_unbounded_reference(corpus):
    texts = ['vector', 'token', 'sequence', 'vector token sequence', 'unrelated']
    db, _, manifest = corpus(sources=[source(f's{i}') for i in range(5)],
        reps=[representation(f'r{i}', f's{i}', text=text) for i, text in enumerate(texts)])
    rows, calls = compare(db, manifest, 'vector token vector sequence', limit=3)
    assert len(rows) == 3
    assert calls[0]['match'] == '"vector" OR "token" OR "sequence"'
    assert calls[0]['window'] == 129


def test_deep_alias_duplicates_cannot_starve_later_head(corpus):
    aliases = {f'alias{i:03}' for i in range(600)}
    sources = [source(sid) for sid in sorted(aliases)] + [source('a'), source('b')]
    reps = [representation(f'r{i:03}', sid) for i, sid in enumerate(sorted(aliases))]
    reps.append(representation('z-b', 'b'))
    db, _, manifest = corpus(sources=sources, reps=reps)
    rows, calls = compare(db, manifest, limit=2,
                          canonical_id=lambda sid: 'a' if sid in aliases else sid)
    assert [row['representation_id'] for row in rows] == ['r000', 'z-b']
    assert [call['window'] for call in calls] == [129, 258, 516, 1032]


@pytest.mark.parametrize('filter_name', ['layer', 'year', 'section', 'element', 'terms', 'status'])
def test_deep_frozen_filter_starvation(corpus, filter_name):
    sources = [source(f'bad{i:03}', year=2000, layers=['other'],
                      status='pending' if filter_name == 'status' else 'done') for i in range(600)]
    sources.append(source('good'))
    reps = [representation(f'r{i:03}', item['source_id'], section_type='introduction', element_type='table')
            for i, item in enumerate(sources[:-1])]
    reps.append(representation('z-good', 'good', section_type='method', element_type='proof'))
    for rep in reps:
        rep['terms'] = ['proof' if rep['source_id'] == 'good' else 'other']
    filters = {'layer': dict(layer='crypto'), 'year': dict(year_from=2010, year_to=2021),
               'section': dict(section_type='method'), 'element': dict(element_type='proof'),
               'terms': dict(terms=['proof']), 'status': {}}[filter_name]
    db, _, manifest = corpus(sources=sources, reps=reps)
    rows, calls = compare(db, manifest, **filters)
    assert [row['representation_id'] for row in rows] == ['z-good']
    assert [call['window'] for call in calls] == [129, 258, 516, 1032]


@pytest.mark.parametrize('count', [0, 1, 128, 129, 130, 258])
def test_all_no_and_exact_window_hits_require_exhaustion(corpus, count):
    db, _, manifest = corpus(sources=[source('a')],
        reps=[representation(f'r{i:03}', 'a') for i in range(count)])
    rows, calls = compare(db, manifest)
    expected_windows, window = [129], 129
    while window <= count:
        window *= 2
        expected_windows.append(window)
    assert [call['window'] for call in calls] == expected_windows
    assert len(rows) == min(count, 128)


def test_empty_expression_does_not_execute_rank_query(corpus):
    db, _, manifest = corpus()
    rows, calls = compare(db, manifest, query='   !!! _ ')
    assert rows == calls == []


@pytest.mark.parametrize('tie_kind', ['canonical', 'representation'])
def test_changing_tie_prefixes_finish_canonical_and_rep_ties(corpus, tie_kind):
    names = [f'z{i:03}' for i in range(260)] + ['a']
    sources = [source(name) for name in names] if tie_kind == 'canonical' else [source('only')]
    reps = [representation(name, name if tie_kind == 'canonical' else 'only') for name in names]
    db, _, manifest = corpus(sources=sources, reps=reps)
    with catalog.CatalogReader(db, manifest['generation'], deadline=time.monotonic() + 5) as reader:
        ids = {row['representation_id']: row['id'] for row in reader.conn.execute('SELECT id,representation_id FROM representations')}

        def prefixes(window):
            if window == 129:
                selected = list(reversed(names[:-1]))[:129]
            elif window == 258:
                selected = names[:258]
            else:
                selected = list(reversed(names))
            return [dict(rowid=ids[name], rank=-1.0) for name in selected]

        trace = reader.conn = Connection(reader.conn, prefixes)
        limit = 128 if tie_kind == 'canonical' else 2
        rows = reader.lexical('needle', limit)
        expected = ['a'] + [f'z{i:03}' for i in range(limit - 1)]
        assert [row['representation_id'] for row in rows] == expected
        assert [call['window'] for call in trace.calls] == [129, 258, 516]
        assert all(call['closed'] for call in trace.calls)


def test_strictly_worse_rank_is_a_complete_boundary_witness(corpus):
    db, _, manifest = corpus(reps=[representation(f'a{i:03}', 'a') for i in range(129)] + [representation('b', 'b')])
    with catalog.CatalogReader(db, manifest['generation'], deadline=time.monotonic() + 5) as reader:
        ids = {row['representation_id']: row['id'] for row in reader.conn.execute('SELECT id,representation_id FROM representations')}
        hits = [dict(rowid=ids[f'a{i:03}'], rank=-2.0) for i in range(129)]
        hits += [dict(rowid=ids['b'], rank=-1.0), dict(rowid=-999, rank=0.0)]
        trace = reader.conn = Connection(reader.conn, lambda window: hits[:window])
        rows = reader.lexical('needle', 2)
        assert [row['representation_id'] for row in rows] == ['a000', 'b']
        assert [call['window'] for call in trace.calls] == [129, 258]
        assert trace.calls[-1]['fetched'] == 131


def test_retained_candidates_alternates_and_caches_are_bounded(corpus):
    sources = [source('a')] + [source(f's{i:03}') for i in range(129)]
    reps = [representation(f'a{i:03}', 'a') for i in range(400)]
    reps += [representation(item['source_id'], item['source_id']) for item in sources[1:]]
    db, _, manifest = corpus(sources=sources, reps=reps)
    observed = []

    def profile(frame, event, arg):
        if frame.f_code is catalog.CatalogReader.lexical.__code__ and event == 'return':
            candidates = frame.f_locals['candidates']
            observed.append((len(candidates), [len(items) for items in candidates.values()],
                             all(len(items) == len({rep_id for _, rep_id in items}) for items in candidates.values())))

    previous = sys.getprofile()
    try:
        sys.setprofile(profile)
        rows, calls = compare(db, manifest)
    finally:
        sys.setprofile(previous)
    assert len(rows) == 128 and len(calls) > 1
    heads, lengths, unique = observed[0]
    assert heads == 128 and max(lengths) == 128 and sum(lengths) <= 128 * 128 and unique


@pytest.mark.parametrize('failure', ['deadline', 'sqlite_interrupt'])
def test_incomplete_second_prefix_is_partial_without_leaking_lexical(corpus, monkeypatch, failure):
    _, manifest_path, _ = corpus(reps=[representation(f'a{i:03}', 'a') for i in range(260)])
    enter, execute = catalog.CatalogReader.__enter__, Connection.execute
    calls = []

    def fail_second(self, sql, parameters=()):
        if sql == RANK_SQL + ' LIMIT ?':
            calls.append(parameters[1])
            if parameters[1] == 258:
                if failure == 'deadline':
                    raise catalog._Deadline()
                exc = sqlite3.OperationalError('interrupted')
                exc.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
                raise exc
        return execute(self, sql, parameters)

    def traced_enter(self):
        enter(self)
        self.conn = Connection(self.conn)
        return self

    monkeypatch.setattr(Connection, 'execute', fail_second)
    monkeypatch.setattr(catalog.CatalogReader, '__enter__', traced_enter)
    result = discover(manifest_path, lambda *args: [dict(representation_id='a000', score=1)])
    assert calls == [129, 258]
    assert result['partial'] and result['diagnostics']['reasons'] == ['lexical_deadline']
    assert [row['source_id'] for row in result['sources']] == ['a']
    assert result['sources'][0]['channel_ranks'] == {'dense': 1}


def test_engine_uses_channel_budget_and_restores_overall_deadline(corpus, monkeypatch):
    _, manifest_path, _ = corpus()
    lexical, hydrated = [], []
    hydrate = catalog.CatalogReader.hydrate

    def lexical_probe(self, query):
        lexical.append(self.deadline)
        assert self.deadline - time.monotonic() > 0.5
        return []

    def hydrate_probe(self, ids):
        hydrated.append(self.deadline)
        return hydrate(self, ids)

    monkeypatch.setattr(catalog.CatalogReader, 'lexical', lexical_probe)
    monkeypatch.setattr(catalog.CatalogReader, 'hydrate', hydrate_probe)
    result = discover(manifest_path, timeout=1)
    assert not result['partial']
    assert len(lexical) == 1 and hydrated
    assert all(deadline - lexical[0] == pytest.approx(0.1) for deadline in hydrated)
