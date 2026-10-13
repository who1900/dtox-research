import hashlib
import time

import pytest

from api import source_representations as catalog
from api.tests.test_source_generation import corpus, discover
from api.tests.test_source_representations import representation, source


OWNER_REV = 'a' * 64
TARGET_REV = 'b' * 64


def canonical(sid):
    return 'target' if sid == 'owner' else sid


def point(owner='owner', revision=OWNER_REV, text='needle'):
    return dict(id='native-' + owner, score=1,
                payload=dict(arxiv_id=owner, text=text, publication_revision=revision))


def alias_corpus(corpus, target_revision=TARGET_REV, owner_revision=OWNER_REV, extra=()):
    owner = source('owner', year=1990, layers=['other'])
    target = source('target')
    owner['publication_revision'], target['publication_revision'] = owner_revision, target_revision
    return corpus(sources=[owner, target], reps=[representation('owner-rep', 'owner'), *extra])


def visible(records, timeout):
    return {record['source_id']: record['publication_revision'] for record in records}


@pytest.mark.parametrize('target_revision', [None, TARGET_REV])
def test_independent_owner_and_target_revisions_require_both_visible(corpus, target_revision):
    _, manifest_path, _ = alias_corpus(corpus, target_revision)
    checked = []

    def visibility(records, timeout):
        checked.extend(records)
        return visible(records, timeout)

    result = discover(manifest_path, query_owned=lambda *args: [point()], hybrid=False,
                      canonical_id=canonical, visible_sources=visibility, layer='crypto', year_from=2010)
    assert not result['partial'] and result['diagnostics']['reasons'] == []
    assert {r['source_id']: r['publication_revision'] for r in checked} == {
        'owner': OWNER_REV, 'target': target_revision}
    slot, = result['sources']
    assert slot['source_id'] == 'target' and slot['publication_revision'] == target_revision
    assert slot['year'] == 2020 and slot['layers'] == ['crypto']
    assert slot['evidence_state'] == 'not_read' and slot['verification_required'] is True
    rep, = slot['representations']
    assert rep['indexed_source_id'] == rep['origin_source_id'] == 'owner'
    assert rep['target_source_id'] == 'target' and rep['origin_publication_revision'] == OWNER_REV
    assert rep['origin_revision'] == hashlib.sha256(b'owner-artifact').hexdigest()
    assert rep['locator'] == representation('owner-rep', 'owner')['locator']
    witness, = rep['native_provenance']
    assert witness['publication_revision'] == OWNER_REV
    assert witness['text_sha256'] == hashlib.sha256(b'needle').hexdigest()
    assert rep['evidence_state'] == 'not_read'
    assert slot['dense_physical_ranks'] == {'dense_native': 1}


@pytest.mark.parametrize('target_revision', [None, TARGET_REV])
def test_standalone_and_engine_without_callback_preserve_strict_default(corpus, target_revision):
    db, manifest_path, manifest = alias_corpus(corpus, target_revision)
    with catalog.CatalogReader(db, manifest['generation'], deadline=time.monotonic() + 2,
                              canonical_id=canonical) as reader:
        assert reader._independent_alias_revisions is False
        assert reader.owned_payload(point()['payload']) == []
    result = discover(manifest_path, query_owned=lambda *args: [point()], hybrid=False, canonical_id=canonical)
    assert result['partial'] and result['sources'] == []
    assert result['diagnostics']['reasons'] == ['native_unmatched_payload']


@pytest.mark.parametrize('invalid', ['owner_stale', 'target_stale', 'owner_missing', 'target_missing',
                                     'owner_staged', 'target_staged', 'owner_ambiguous', 'target_ambiguous'])
def test_each_identity_must_have_exact_visibility(corpus, invalid):
    _, manifest_path, _ = alias_corpus(corpus)

    def visibility(records, timeout):
        live = visible(records, timeout)
        sid, failure = invalid.split('_')
        if failure == 'missing':
            del live[sid]
        elif failure == 'stale':
            live[sid] = 'f' * 64
        elif failure == 'staged':
            live[sid] = dict(revision=live[sid], state='staged')
        else:
            live[sid] = [live[sid], 'f' * 64]
        return live

    result = discover(manifest_path, query_owned=lambda *args: [point()], hybrid=False,
                      canonical_id=canonical, visible_sources=visibility)
    assert result['partial'] and not result['sources']
    assert result['diagnostics']['reasons'] == ['publication_not_visible']


@pytest.mark.parametrize('invalid', ['exception', 'not_dict'])
def test_visibility_error_never_releases_candidate(corpus, invalid):
    _, manifest_path, _ = alias_corpus(corpus)

    def visibility(records, timeout):
        if invalid == 'exception':
            raise RuntimeError('unknown journal')
        return []

    result = discover(manifest_path, query_owned=lambda *args: [point()], hybrid=False,
                      canonical_id=canonical, visible_sources=visibility)
    assert result['partial'] and not result['sources']
    expected = 'visibility_failed:' + ('RuntimeError' if invalid == 'exception' else 'ValueError')
    assert result['diagnostics']['reasons'] == [expected]


@pytest.mark.parametrize('explicit', [False, True])
def test_legacy_target_requires_explicit_null_not_missing(corpus, explicit):
    _, manifest_path, _ = alias_corpus(corpus, target_revision=None)
    live = {'owner': OWNER_REV, **({'target': None} if explicit else {})}
    result = discover(manifest_path, query_owned=lambda *args: [point()], hybrid=False,
                      canonical_id=canonical, visible_sources=lambda *args: live)
    assert bool(result['sources']) is explicit
    assert result['partial'] is not explicit
    assert result['diagnostics']['reasons'] == ([] if explicit else ['publication_not_visible'])


@pytest.mark.parametrize('missing', [False, True])
def test_legacy_owner_explicit_null_is_not_missing_revision(corpus, missing):
    _, manifest_path, _ = alias_corpus(corpus, owner_revision=None)
    native = point(revision=None)
    if missing:
        del native['payload']['publication_revision']
    result = discover(manifest_path, query_owned=lambda *args: [native], hybrid=False,
                      canonical_id=canonical, visible_sources=visible)
    assert result['partial'] is missing
    if missing:
        assert not result['sources'] and result['diagnostics']['reasons'] == ['native_payload_invalid']
    else:
        rep = result['sources'][0]['representations'][0]
        assert rep['origin_publication_revision'] is None
        assert rep['origin_publication_state'] == 'legacy_unmarked'
        assert result['sources'][0]['publication_revision'] == TARGET_REV


@pytest.mark.parametrize('invalid', ['text', 'control', 'owner_revision', 'missing_revision'])
def test_alias_optin_does_not_relax_payload_integrity(corpus, invalid):
    _, manifest_path, _ = alias_corpus(corpus)
    native = point()
    if invalid == 'text':
        native['payload']['text'] = 'different whole text'
    elif invalid == 'control':
        native['payload']['text'] = '\x1bneedle'
    elif invalid == 'owner_revision':
        native['payload']['publication_revision'] = TARGET_REV
    else:
        del native['payload']['publication_revision']
    result = discover(manifest_path, query_owned=lambda *args: [native], hybrid=False,
                      canonical_id=canonical, visible_sources=visible)
    assert result['partial'] and not result['sources']
    expected = 'native_payload_invalid' if invalid in ('control', 'missing_revision') else 'native_unmatched_payload'
    assert result['diagnostics']['reasons'] == [expected]


@pytest.mark.parametrize('missing', ['physical_owner', 'canonical_target'])
def test_optin_never_falls_back_from_unknown_identity(corpus, missing):
    if missing == 'physical_owner':
        target = source('target')
        target['publication_revision'] = OWNER_REV
        _, manifest_path, _ = corpus(sources=[target], reps=[representation('target-rep', 'target')])
    else:
        owner = source('owner')
        owner['publication_revision'] = OWNER_REV
        _, manifest_path, _ = corpus(sources=[owner], reps=[representation('owner-rep', 'owner')])
    result = discover(manifest_path, query_owned=lambda *args: [point()], hybrid=False,
                      canonical_id=canonical, visible_sources=visible)
    assert result['partial'] and result['sources'] == []
    assert result['diagnostics']['reasons'] == ['native_unmatched_payload']


@pytest.mark.parametrize('kind', ['canonical_passage', 'foreign_context'])
def test_optin_requires_exact_physical_owned_passage(corpus, kind):
    owner, target = source('owner'), source('target')
    owner['publication_revision'], target['publication_revision'] = OWNER_REV, TARGET_REV
    rep = (representation('not-owner', 'target') if kind == 'canonical_passage' else
           representation('not-owner', 'owner', origin='target', kind='citation_context'))
    _, manifest_path, _ = corpus(sources=[owner, target], reps=[rep])
    result = discover(manifest_path, query_owned=lambda *args: [point()], hybrid=False,
                      canonical_id=canonical, visible_sources=visible)
    assert result['partial'] and not result['sources']
    assert result['diagnostics']['reasons'] == ['native_unmatched_payload']


def test_alias_qualification_uses_target_and_retains_filtered_outcome(corpus):
    _, manifest_path, _ = alias_corpus(corpus)
    result = discover(manifest_path, query_owned=lambda *args: [point()], hybrid=False,
                      canonical_id=canonical, visible_sources=visible, layer='other')
    assert not result['partial'] and not result['sources']
    assert result['diagnostics']['native_filtered'] == 1
    assert result['diagnostics']['reasons'] == []


def test_missing_catalog_cache_entry_cannot_be_certified_by_map(corpus, monkeypatch):
    _, manifest_path, _ = alias_corpus(corpus)
    hydrate = catalog.CatalogReader.hydrate
    checked = []

    def evict(self, ids):
        slots = hydrate(self, ids)
        self.metadata.pop('owner', None)
        return slots

    def visibility(records, timeout):
        checked.extend(record['source_id'] for record in records)
        return {'owner': OWNER_REV, 'target': TARGET_REV}

    monkeypatch.setattr(catalog.CatalogReader, 'hydrate', evict)
    result = discover(manifest_path, query_owned=lambda *args: [point()], hybrid=False,
                      canonical_id=canonical, visible_sources=visibility)
    assert 'owner' not in checked and 'target' in checked
    assert result['partial'] and not result['sources']
    assert result['diagnostics']['reasons'] == ['publication_not_visible']


def test_stale_alias_does_not_keep_native_vote_through_live_canonical_rep(corpus):
    original = representation('canonical-summary', 'target', kind='own_summary', text='independent canonical summary')
    _, manifest_path, _ = alias_corpus(corpus, extra=[original])
    result = discover(manifest_path, lambda *args: [dict(representation_id='canonical-summary', score=0.1)],
                      query_owned=lambda *args: [point()], hybrid=False, canonical_id=canonical,
                      visible_sources=lambda *args: {'owner': 'f' * 64, 'target': TARGET_REV})
    assert result['partial'] and result['diagnostics']['reasons'] == ['publication_not_visible']
    slot, = result['sources']
    assert slot['dense_physical_ranks'] == {'dense_derived': 1}
    assert [rep['representation_id'] for rep in slot['representations']] == ['canonical-summary']
    assert slot['evidence_state'] == 'not_read'


def test_true_130_unique_union_is_bounded_with_independent_revisions(corpus):
    sources = [source(sid) for sid in ('owner-a', 'owner-b', 'target-a', 'target-b')]
    for item in sources:
        item['publication_revision'] = OWNER_REV if item['source_id'].startswith('owner') else TARGET_REV
    reps = [representation(f'a{i:03}', 'owner-a') for i in range(128)]
    reps += [representation(f'b{i:03}', 'owner-b') for i in range(2)]
    _, manifest_path, _ = corpus(sources=sources, reps=reps)
    result = discover(manifest_path, query_owned=lambda *args: [point('owner-a'), point('owner-b')],
                      hybrid=False, canonical_id=lambda sid: sid.replace('owner-', 'target-'), visible_sources=visible)
    assert result['status'] == 'ready' and not result['partial']
    assert result['diagnostics']['reasons'] == []
    assert result['diagnostics']['native_alternatives_truncated'] == 2
    assert result['diagnostics']['limits'] == ['dense_native_alternative_limit', 'dense_native_candidate_limit']
    assert result['diagnostics']['retrieval_bounded'] is True
    assert result['diagnostics']['exhaustive'] is False
    assert {slot['source_id'] for slot in result['sources']} == {'target-a', 'target-b'}
