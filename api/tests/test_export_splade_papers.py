import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from eval import export_splade_papers as exporter


class ExportSpladePapersTests(unittest.TestCase):
    def test_primary_layer_uses_priority_and_builder_tech_alias(self):
        self.assertEqual(exporter.primary_layer("builder-tech,web3"), "web3")
        self.assertEqual(exporter.primary_layer("other,builder-tech"), "builder-tech")
        self.assertEqual(exporter.primary_layer("llm-slm,ai-agents"), "agents")
        self.assertIsNone(exporter.primary_layer("other,unknown"))

    def test_seeded_hash_is_stable_and_seed_sensitive(self):
        self.assertEqual(exporter.rank_key("stage-a", "p1"), exporter.rank_key("stage-a", "p1"))
        self.assertNotEqual(exporter.rank_key("stage-a", "p1"), exporter.rank_key("other", "p1"))

    def test_reverse_alias_map_is_built_in_one_pass_and_deduplicates(self):
        class Mapping(dict):
            visits = 0

            def items(self):
                for item in super().items():
                    self.visits += 1
                    yield item

        twins = Mapping({f"alias-{i}": "canonical" for i in range(1000)})
        reverse = exporter.reverse_aliases(twins)
        self.assertEqual(twins.visits, len(twins))
        self.assertEqual(reverse["canonical"][0], "alias-0")
        self.assertEqual(len(reverse["canonical"]), 1000)

    def test_readonly_population_schema_does_not_require_in_citations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE papers(arxiv_id TEXT,title TEXT,abstract TEXT,layers TEXT,year INTEGER,status TEXT)")
            conn.executemany("INSERT INTO papers VALUES(?,?,?,?,?,?)", [
                ("a-alias", "Alias", "x" * 100, "builder-tech", 2024, "done"),
                ("canonical", "Canonical", "y" * 100, "builder-tech", 2024, "done"),
            ])
            conn.commit()
            conn.close()
            selected, counts, quotas, _ = exporter._load_population(
                path, {"a-alias": "canonical"}, {"canonical": ("a-alias",)}, set(), "seed",
                10**20)
            self.assertEqual(counts["builder-tech"], 2)
            self.assertEqual(quotas["builder-tech"], 1)
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["arxiv_id"], "canonical")
            self.assertNotIn("in_citations", selected[0])

    def test_qdrant_collection_path_is_papers_coarse_base(self):
        session = Mock()
        session.post.return_value.json.return_value = {"result": []}
        exporter._retrieve(session, "http://127.0.0.1:16335", ["point"], 10**20)
        self.assertIn("/collections/papers_coarse_base/points", session.post.call_args.args[0])

    def test_twins_input_validation(self):
        self.assertEqual(exporter.validate_twins({"canonical": {"a": "b"}}), {"a": "b"})
        for invalid in ({}, {"canonical": []}, {"canonical": {"a": 1}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                exporter.validate_twins(invalid)

    def test_frozen_fixture_query_set_stays_at_33(self):
        queries, _, _ = exporter.fixture_data(exporter.FIXTURES)
        self.assertEqual(len(queries), 33)


if __name__ == "__main__":
    unittest.main()
