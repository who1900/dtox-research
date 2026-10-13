import sqlite3
import unittest
from unittest.mock import patch

from api import main


class PaperFtsRankTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE papers_fts USING fts5("
                "arxiv_id UNINDEXED, title, abstract, layers UNINDEXED, year UNINDEXED, "
                "tokenize='porter unicode61')"
            )
        except sqlite3.OperationalError as error:
            self.skipTest(f"SQLite FTS5 unavailable: {error}")
        self.conn.executemany(
            "INSERT INTO papers_fts(arxiv_id, title, abstract, layers, year) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("paper-a", "Oracle Feeds for Smart Contracts",
                 "A durable decentralized oracle design.", "web3", "2021"),
                ("paper-b", "Secure Oracle Networks",
                 "Feeds authenticate signed blockchain data across chains.", "web3", "2024"),
                ("paper-c", "Consensus Under Partial Synchrony",
                 "Oracle protocols can coordinate distributed nodes.", "ai-agents", "2020"),
                ("paper-d", "Durable Nonces on Solana",
                 "Transaction validity and nonce management.", "web3", "2018"),
                ("paper-e", "Oracle Security and Consensus",
                 "Secure feeds for deterministic consensus systems.", "web3", "2023"),
            ],
        )

    def test_hidden_rank_matches_weighted_bm25_ids_scores_filters_and_order(self):
        cases = [
            ("oracle feeds", None, None, None, 20),
            ("oracle feeds", "web3", None, None, 20),
            ("secure oracle", "web3", 2022, 2025, 20),
            ("durable nonce", "web3", 2017, 2022, 1),
            ("consensus oracle", "ai-agents", 2019, 2021, 20),
        ]
        with patch.object(main, "_ro_conn", return_value=self.conn), \
             patch.object(main, "PAPER_LEXICAL_SOURCE_BALANCE", False):
            for query, layer, year_from, year_to, limit in cases:
                with self.subTest(query=query, layer=layer,
                                  year_from=year_from, year_to=year_to):
                    match = main.fts_query_any(query)
                    where = ["papers_fts MATCH ?"]
                    params = [match]
                    if layer:
                        where.append("layers LIKE ?")
                        params.append(f"%{layer}%")
                    if year_from is not None:
                        where.append("CAST(year AS INTEGER) >= ?")
                        params.append(year_from)
                    if year_to is not None:
                        where.append("CAST(year AS INTEGER) <= ?")
                        params.append(year_to)

                    old_rows = self.conn.execute(
                        "SELECT arxiv_id, bm25(papers_fts, 0, 3.0, 1.0, 0, 0) AS rank "
                        f"FROM papers_fts WHERE {' AND '.join(where)} "
                        "ORDER BY rank LIMIT ?",
                        params + [limit],
                    ).fetchall()
                    new_rows = self.conn.execute(
                        "SELECT arxiv_id, rank FROM papers_fts "
                        f"WHERE {' AND '.join(where)} "
                        "AND rank MATCH 'bm25(0, 3.0, 1.0, 0, 0)' ORDER BY rank LIMIT ?",
                        params + [limit],
                    ).fetchall()
                    self.assertEqual(new_rows, old_rows)
                    self.assertEqual(
                        main._paper_bm25(query, layer, year_from, year_to, limit),
                        [row[0] for row in old_rows],
                    )


if __name__ == "__main__":
    unittest.main()
