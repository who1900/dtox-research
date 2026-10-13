import time

import pytest

from api import source_generation as generation
from api import source_representations as catalog
from api.tests.test_source_generation import corpus, discover, native_point
from api.tests.test_source_representations import representation, source


@pytest.mark.parametrize("filters,status", [
    ({"layer": "other"}, "done"),
    ({"year_from": 2021}, "done"),
    ({"year_to": 2019}, "done"),
    ({"section_type": "conclusion"}, "done"),
    ({"element_type": "table"}, "done"),
    ({"terms": ["absent"]}, "done"),
    ({}, "pending"),
])
def test_exact_but_filtered_is_not_unmatched(corpus, filters, status):
    rep = representation("a", "a", section_type="method", element_type="proof")
    rep["terms"] = ["needle"]
    db, manifest_path, manifest = corpus(sources=[source("a", status=status)], reps=[rep])
    point = native_point("old-a", "a")
    with catalog.CatalogReader(db, manifest["generation"], deadline=time.monotonic() + 2,
                              **filters) as reader:
        assert reader.owned_payload(point["payload"]) is catalog._NATIVE_INELIGIBLE
    result = discover(manifest_path, query_owned=lambda *args: [point], hybrid=False, **filters)
    assert result["status"] == "ready" and not result["partial"]
    assert result["sources"] == []
    assert result["diagnostics"]["native_filtered"] == 1
    assert result["diagnostics"]["reasons"] == []
    assert result["diagnostics"]["exhaustive"] is False


@pytest.mark.parametrize("invalid", ["text", "revision", "missing_revision"])
def test_excluding_filter_cannot_mask_integrity_miss(corpus, monkeypatch, invalid):
    db, manifest_path, manifest = corpus()
    point = native_point("old-a", "a")
    if invalid == "text":
        point["payload"]["text"] = "different whole text"
    elif invalid == "revision":
        point["payload"]["publication_revision"] = "0" * 64
    else:
        del point["payload"]["publication_revision"]
    with catalog.CatalogReader(db, manifest["generation"], deadline=time.monotonic() + 2,
                              layer="other") as reader:
        monkeypatch.setattr(reader, "qualify", lambda record: pytest.fail("integrity miss reached qualify"))
        if invalid == "missing_revision":
            with pytest.raises(ValueError, match="revision missing"):
                reader.owned_payload(point["payload"])
        else:
            assert reader.owned_payload(point["payload"]) == []
    result = discover(manifest_path, query_owned=lambda *args: [point], hybrid=False, layer="other")
    assert result["partial"] and not result["sources"]
    assert result["diagnostics"]["native_filtered"] == 0
    expected = "native_payload_invalid" if invalid == "missing_revision" else "native_unmatched_payload"
    assert result["diagnostics"]["reasons"] == [expected]


def test_outside_alias_revision_and_mixed_outcomes_stay_distinct(corpus, monkeypatch):
    sources = [source("a"), source("alias"), source("b"), source("c", layers=["other"])]
    reps = [representation(s["source_id"], s["source_id"]) for s in sources]
    db, manifest_path, manifest = corpus(sources=sources, reps=reps)
    canonical = lambda sid: "a" if sid == "alias" else sid
    with catalog.CatalogReader(db, manifest["generation"], deadline=time.monotonic() + 2,
                              canonical_id=canonical, layer="crypto") as reader:
        monkeypatch.setattr(reader, "qualify", lambda record: pytest.fail("revision mismatch reached qualify"))
        assert reader.owned_payload(native_point("outside", "fresh")["payload"]) is None
        assert reader.owned_payload(native_point("alias-point", "alias")["payload"]) == []
    points = [native_point("outside", "fresh"), native_point("alias-point", "alias"),
              native_point("filtered", "c"), native_point("valid", "b")]
    result = discover(manifest_path, query_owned=lambda *args: points, hybrid=False,
                      canonical_id=canonical, layer="crypto")
    assert [row["source_id"] for row in result["sources"]] == ["b"]
    assert result["partial"]
    assert result["diagnostics"]["reasons"] == ["native_unmatched_payload"]
    assert result["diagnostics"]["native_outside_generation"] == 1
    assert result["diagnostics"]["native_filtered"] == 1


def test_duplicate_rep_ids_do_not_consume_mapping_slots(corpus, monkeypatch):
    _, manifest_path, _ = corpus()
    points = [{**native_point("repeat" if i < 20 else f"point-{i:03}", "a"), "score": float(i)}
              for i in range(127)] + [native_point("point-b", "b")]
    captured = []
    original = generation._native_mappings

    def mappings(*args):
        rows = original(*args)
        captured.extend(rows)
        return rows

    monkeypatch.setattr(generation, "_native_mappings", mappings)
    result = discover(manifest_path, query_owned=lambda *args: points, hybrid=False)
    assert {row["source_id"] for row in result["sources"]} == {"a", "b"}
    assert not result["partial"]
    assert len(captured) == 2
    a = next(row for row in captured if row["representation_id"] == "a")
    assert a["score"] == 126
    witnesses = a["native_provenance"]
    assert len(witnesses) == len({w["point_id"] for w in witnesses}) == 8
    assert witnesses[0]["point_id"] == "point-126"
    assert result["diagnostics"]["reasons"] == []
    assert result["diagnostics"]["limits"] == ["dense_native_candidate_limit"]


@pytest.mark.parametrize("alias", [False, True])
def test_canonical_heads_precede_alternates_and_truncation_is_bounded(corpus, monkeypatch, alias):
    sources = [source("a"), source("b")]
    reps = [representation(f"a{i:03}", "a") for i in range(128)] + [representation("b", "b")]
    points = [native_point("point-a", "a"), native_point("point-b", "b")]
    if alias:
        alias_source = source("alias")
        alias_source["publication_revision"] = source("a")["publication_revision"]
        sources.append(alias_source)
        reps.append(representation("000-alias", "alias", text="alias exact chunk"))
        points.append({**native_point("point-alias", "alias", text="alias exact chunk",
                                     revision=source("a")["publication_revision"]), "score": 2})
    _, manifest_path, _ = corpus(sources=sources, reps=reps)
    captured = []
    original = generation._native_mappings

    def mappings(*args):
        rows = original(*args)
        captured.extend(rows)
        return rows

    monkeypatch.setattr(generation, "_native_mappings", mappings)
    canonical = lambda sid: "a" if sid == "alias" else sid
    result = discover(manifest_path, query_owned=lambda *args: points, hybrid=False,
                      canonical_id=canonical)
    assert result["status"] == "ready" and not result["partial"]
    assert result["diagnostics"]["reasons"] == []
    assert result["diagnostics"]["native_alternatives_truncated"] == 1 + int(alias)
    assert result["diagnostics"]["limits"] == ["dense_native_alternative_limit", "dense_native_candidate_limit"]
    assert result["diagnostics"]["retrieval_bounded"] is True
    assert result["diagnostics"]["exhaustive"] is False
    assert {row["source_id"] for row in result["sources"]} == {"a", "b"}
    ids = [row["representation_id"] for row in captured]
    assert len(ids) == len(set(ids)) == 128
    assert ids[:2] == ["000-alias" if alias else "a000", "b"]
    assert ids[2:5] == (["a000", "a001", "a002"] if alias else ["a001", "a002", "a003"])
    assert all(set(row["channel_ranks"]) == {"dense"} for row in result["sources"])


@pytest.mark.parametrize("invalid,expected", [
    ("text", "native_unmatched_payload"),
    ("controls", "native_payload_invalid"),
    ("revision", "native_unmatched_payload"),
    ("missing_revision", "native_payload_invalid"),
    ("exact_overflow", "native_exact_mapping_limit"),
])
def test_normal_alternatives_cannot_mask_integrity_fault(corpus, invalid, expected):
    sources = [source(sid) for sid in ("a", "b", "c")]
    reps = [representation(f"a{i:03}", "a") for i in range(128)]
    reps += [representation(f"b{i:03}", "b") for i in range(2)]
    reps += [representation(f"c{i:03}", "c") for i in range(129 if invalid == "exact_overflow" else 1)]
    _, manifest_path, _ = corpus(sources=sources, reps=reps)
    bad = native_point("point-c", "c")
    if invalid == "text":
        bad["payload"]["text"] = "different whole text"
    elif invalid == "controls":
        bad["payload"]["text"] = "needle\x00invalid"
    elif invalid == "revision":
        bad["payload"]["publication_revision"] = "0" * 64
    elif invalid == "missing_revision":
        del bad["payload"]["publication_revision"]
    points = [native_point("point-a", "a"), native_point("point-b", "b"), bad]
    result = discover(manifest_path, query_owned=lambda *args: points, hybrid=False)
    assert result["status"] == "ready" and result["partial"]
    assert {row["source_id"] for row in result["sources"]} == {"a", "b"}
    assert result["diagnostics"]["reasons"] == [expected]
    assert result["diagnostics"]["native_alternatives_truncated"] == 2
    assert result["diagnostics"]["limits"] == ["dense_native_alternative_limit", "dense_native_candidate_limit"]
    assert result["diagnostics"]["native_outside_generation"] == result["diagnostics"]["native_filtered"] == 0
    assert result["diagnostics"]["retrieval_bounded"] is True
    assert result["diagnostics"]["exhaustive"] is False


@pytest.mark.parametrize("count", [128, 129])
def test_exact_lookup_bound_is_not_malformed(corpus, count):
    db, manifest_path, manifest = corpus(sources=[source("a")],
        reps=[representation(f"a{i:03}", "a") for i in range(count)])
    point = native_point("point-a", "a")
    with catalog.CatalogReader(db, manifest["generation"], deadline=time.monotonic() + 2) as reader:
        if count == 129:
            with pytest.raises(catalog._NativeMappingLimit, match="bound exceeded"):
                reader.owned_payload(point["payload"])
        else:
            assert len(reader.owned_payload(point["payload"])) == 128
    result = discover(manifest_path, query_owned=lambda *args: [point], hybrid=False)
    assert result["partial"] is (count == 129)
    assert result["diagnostics"]["reasons"] == (["native_exact_mapping_limit"] if count == 129 else [])
    assert bool(result["sources"]) is (count == 128)


def test_ninth_highest_score_witness_survives_provenance_cap(corpus):
    _, manifest_path, _ = corpus(sources=[source("a")], reps=[representation("a", "a")])
    points = [{**native_point(f"point-{i}", "a"), "score": i / 10} for i in range(9)]
    points.append(dict(points[-1]))
    result = discover(manifest_path, query_owned=lambda *args: points, hybrid=False)
    assert not result["partial"]
    witnesses = result["sources"][0]["representations"][0]["native_provenance"]
    assert [w["point_id"] for w in witnesses] == [f"point-{i}" for i in range(8, 0, -1)]


def test_worst_case_intermediate_bound_caches_and_deadline(monkeypatch):
    class Reader:
        canonical = staticmethod(lambda sid: sid)
        deadline = time.monotonic() + 5
        metadata = {}
        qualifiers = {}

        def owned_payload(self, payload):
            self.metadata.update({f"meta-{i}": None for i in range(514)})
            self.qualifiers.update({f"qualifier-{i}": None for i in range(129)})
            return [payload["arxiv_id"] + f"-rep-{i:03}" for i in range(128)]

    points = [native_point(f"point-{i:03}", f"s{i:03}") for i in range(128)]
    diagnostics = {"native_outside_generation": 0, "native_filtered": 0, "native_alternatives_truncated": 0}
    reasons, limits, checks = set(), set(), []
    original = catalog._check_deadline

    def check(deadline):
        checks.append(deadline)
        original(deadline)

    monkeypatch.setattr(catalog, "_check_deadline", check)
    reader = Reader()
    rows = generation._native_mappings(reader, points, diagnostics, reasons, limits)
    assert len(rows) == len({row["representation_id"] for row in rows}) == 128
    assert [row["representation_id"] for row in rows] == [f"s{i:03}-rep-000" for i in range(128)]
    assert reasons == set()
    assert limits == {"dense_native_alternative_limit"}
    assert diagnostics["native_alternatives_truncated"] == 128 * 128 - 128
    assert 128 * 128 <= len(checks) <= 128 * 128 + 128
    assert len(reader.metadata) <= 512 and len(reader.qualifiers) <= 128
    reader.deadline = time.monotonic() - 1
    with pytest.raises(catalog._Deadline):
        generation._native_mappings(reader, points, diagnostics, set(), set())


def test_defensive_canonical_head_overflow_remains_partial():
    class Reader:
        canonical = staticmethod(lambda sid: sid)
        deadline = time.monotonic() + 2
        metadata = {}
        qualifiers = {}

        def owned_payload(self, payload):
            return [payload["arxiv_id"] + "-rep"]

    points = [native_point(f"point-{i:03}", f"s{i:03}") for i in range(129)]
    with pytest.raises(ValueError, match="invalid native candidates"):
        generation._native_rows(points)
    diagnostics = {"native_outside_generation": 0, "native_filtered": 0, "native_alternatives_truncated": 0}
    reasons, limits = set(), set()
    rows = generation._native_mappings(Reader(), points, diagnostics, reasons, limits)
    assert reasons == {"native_mapping_limit"}
    assert limits == set()
    assert diagnostics["native_alternatives_truncated"] == 0
    assert [row["representation_id"] for row in rows] == [f"s{i:03}-rep" for i in range(128)]
