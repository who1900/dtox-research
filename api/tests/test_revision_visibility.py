"""Pure interval/topK model fixtures, not an installed Qdrant compatibility test."""
import copy
import json
import unittest

from api.revision_visibility import VisibilityBlocked, build_visibility_filter


ROOT, T, U, R, FAILED = (c * 64 for c in ("0", "1", "2", "3", "4"))


def build(tokens, **kwargs):
    return build_visibility_filter(tokens, root_token=ROOT, max_tokens=8, max_filter_bytes=8000, **kwargs)


def matches(condition, payload):
    """Small independent evaluator for emitted Filter/nested/match-any/value subset."""
    if "nested" in condition:
        nested = condition["nested"]
        values = payload.get(nested["key"], [])
        return isinstance(values, list) and any(isinstance(v, dict) and matches(nested["filter"], v) for v in values)
    if "key" in condition:
        value = payload.get(condition["key"])
        match = condition["match"]
        if "any" in match:
            return value in match["any"]
        return value == match["value"]
    return (all(matches(c, payload) for c in condition.get("must", []))
            and all(not matches(c, payload) for c in condition.get("must_not", []))
            and ("should" not in condition or any(matches(c, payload) for c in condition["should"])))


def point(*intervals):
    return {"visibility_intervals": list(intervals)}


class RevisionVisibilityTests(unittest.TestCase):
    def test_nested_shape_no_has_id_or_top_level_retirement(self):
        result = build([T, ROOT])
        self.assertEqual(result, {"must": [{"nested": {"key": "visibility_intervals", "filter": {
            "must": [{"key": "born", "match": {"any": [ROOT, T]}}],
            "must_not": [{"key": "retired", "match": {"any": [ROOT, T]}}]}}}]})

    def test_staging_preserves_parent_and_hides_target_before_topk(self):
        parent = point({"born": ROOT, "retired": T})
        target = point({"born": T})
        self.assertTrue(matches(build([ROOT]), parent))
        self.assertFalse(matches(build([ROOT]), target))
        self.assertFalse(matches(build([ROOT]), point({"born": FAILED})))

    def test_failed_stage_followed_by_unrelated_commit_never_leaks(self):
        parent = point({"born": ROOT, "retired": T})
        failed = point({"born": T})
        for committed in ([ROOT], [ROOT, U], [ROOT, U, R]):
            self.assertTrue(matches(build(committed), parent))
            self.assertFalse(matches(build(committed), failed))
        self.assertLess(T, U)
        self.assertTrue(T <= max([ROOT, U]))  # ordinal watermark would leak this aborted hole

    def test_commit_switches_both_families_with_same_sqlite_snapshot(self):
        families = {name: {"parent": point({"born": ROOT, "retired": T}), "target": point({"born": T})}
                    for name in ("fulltext", "coarse")}
        old = {"tokens": (ROOT,), "epoch": 7, "pointer": "parent", "fts": "parent"}
        new = {"tokens": (ROOT, T), "epoch": 8, "pointer": "target", "fts": "target"}
        for snapshot in (old, new):
            for rows in families.values():
                visible = [revision for revision, payload in rows.items() if matches(build(snapshot["tokens"]), payload)]
                self.assertEqual(visible, [snapshot["pointer"]])
                self.assertEqual(visible, [snapshot["fts"]])
        self.assertNotEqual(old["epoch"], new["epoch"])  # mixed metadata/FTS workers must be rejected

    def test_fresh_rollback_interval_restores_parent_only_after_commit(self):
        parent = point({"born": ROOT, "retired": T}, {"born": R})
        target = point({"born": T, "retired": R})
        before, after = build([ROOT, T]), build([ROOT, T, R])
        self.assertFalse(matches(before, parent))
        self.assertTrue(matches(before, target))
        self.assertTrue(matches(after, parent))
        self.assertFalse(matches(after, target))

    def test_failed_rollback_remains_invisible_after_other_paper_commit(self):
        parent = point({"born": ROOT, "retired": T}, {"born": R})
        target = point({"born": T, "retired": R})
        committed = build([ROOT, T, U])
        self.assertFalse(matches(committed, parent))
        self.assertTrue(matches(committed, target))

    def test_cross_element_and_top_level_negation_counterexamples(self):
        committed = [ROOT, T, R]
        restored = point({"born": ROOT, "retired": T}, {"born": R})
        self.assertTrue(matches(build(committed), restored))
        wrong_top_level = (any(i["born"] in committed for i in restored["visibility_intervals"])
                           and not any(i.get("retired") in committed for i in restored["visibility_intervals"]))
        self.assertFalse(wrong_top_level)
        inactive = point({"born": ROOT, "retired": T}, {"born": FAILED})
        wrong_cross_element = (any(i["born"] in committed for i in inactive["visibility_intervals"])
                               and any(i.get("retired") not in committed for i in inactive["visibility_intervals"]))
        self.assertTrue(wrong_cross_element)
        self.assertFalse(matches(build(committed), inactive))

    def test_inactive_crowding_postfilter_starves_but_prefilter_does_not(self):
        candidates = [(100 - i, "inactive" + str(i), point({"born": FAILED})) for i in range(20)]
        active = [(0.8, "a", point({"born": ROOT})), (0.7, "b", point({"born": ROOT}))]
        ranked = sorted(candidates + active, reverse=True)
        condition = build([ROOT])
        postfiltered = [row for row in ranked[:2] if matches(condition, row[2])]
        prefiltered = [row for row in ranked if matches(condition, row[2])][:2]
        self.assertEqual(postfiltered, [])
        self.assertEqual([(r[0], r[1]) for r in prefiltered], [(0.8, "a"), (0.7, "b")])

    def test_unmarked_legacy_and_missing_born_do_not_bypass(self):
        for payload in ({}, point(), point({}), point({"retired": T}), point({"born": FAILED})):
            self.assertFalse(matches(build([ROOT]), payload))
        for interval in ({"born": ROOT}, {"born": ROOT, "retired": None}, {"born": ROOT, "retired": T}):
            self.assertTrue(matches(build([ROOT]), point(interval)))

    def test_pending_concurrency_requires_writer_lease_not_filter_magic(self):
        parent_overwritten = point({"born": ROOT, "retired": U})
        committed_t = point({"born": T})
        self.assertTrue(matches(build([ROOT, T]), parent_overwritten))
        self.assertTrue(matches(build([ROOT, T]), committed_t))  # forbidden concurrent retirement overwrite

    def test_aborted_token_recycling_is_forbidden_authority_transition(self):
        failed = point({"born": FAILED})
        self.assertFalse(matches(build([ROOT, U]), failed))
        self.assertTrue(matches(build([ROOT, U, FAILED]), failed))  # builder cannot authenticate a false SQLite snapshot

    def test_base_or_facets_are_anded_unchanged_not_weakened(self):
        base = {"should": [{"key": "layer", "match": {"value": "math"}},
                           {"key": "layer", "match": {"value": "code"}}],
                "must_not": [{"key": "source", "match": {"value": "excluded"}}]}
        original = copy.deepcopy(base)
        result = build([ROOT], base_filter=base)
        self.assertEqual(base, original)
        self.assertEqual(result["must"][0], base)
        self.assertTrue(matches(result, {**point({"born": ROOT}), "layer": "math"}))
        self.assertFalse(matches(result, {**point({"born": FAILED}), "layer": "math"}))
        self.assertFalse(matches(result, {**point({"born": ROOT}), "layer": "other"}))
        self.assertFalse(matches(result, {**point({"born": ROOT}), "layer": "math", "source": "excluded"}))

    def test_input_copy_and_deterministic_snapshot_filters(self):
        tokens = [T, ROOT]
        base = {"must": [{"key": "layer", "match": {"value": "math"}}]}
        result = build(tokens, base_filter=base)
        self.assertEqual(result, build(list(reversed(tokens)), base_filter=base))
        tokens.append(U)
        base["must"][0]["match"]["value"] = "changed"
        self.assertEqual(result["must"][0]["must"][0]["match"]["value"], "math")
        self.assertNotIn(U, result["must"][1]["nested"]["filter"]["must"][0]["match"]["any"])

    def test_caps_are_explicit_and_no_truncation_or_duplicate_hiding(self):
        for tokens in ([], [ROOT, ROOT], [ROOT, "invalid"], [ROOT, True], [ROOT, "A" * 64], [T], "not-an-array"):
            with self.subTest(tokens=tokens), self.assertRaises(VisibilityBlocked):
                build(tokens)
        with self.assertRaises(VisibilityBlocked):
            build_visibility_filter([ROOT, T], root_token=ROOT, max_tokens=1, max_filter_bytes=8000)
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(VisibilityBlocked):
                build_visibility_filter([ROOT], root_token=ROOT, max_tokens=value, max_filter_bytes=8000)

    def test_utf8_byte_cap_includes_base_and_both_token_lists(self):
        base = {"must": [{"key": "text", "match": {"value": "текст"}}]}
        result = build([ROOT, T], base_filter=base)
        size = len(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(build_visibility_filter([ROOT, T], root_token=ROOT, max_tokens=2,
                                              max_filter_bytes=size, base_filter=base), result)
        with self.assertRaises(VisibilityBlocked):
            build_visibility_filter([ROOT, T], root_token=ROOT, max_tokens=2,
                                    max_filter_bytes=size - 1, base_filter=base)
        for base in ([], {"unexpected": []}, {"must": [{"range": float("nan")}]}, {"must": [{"value": object()}]}):
            with self.subTest(base=base), self.assertRaises(VisibilityBlocked):
                build([ROOT], base_filter=base)


if __name__ == "__main__":
    unittest.main()
