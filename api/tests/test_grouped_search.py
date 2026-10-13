import unittest
from unittest.mock import Mock

import requests

from api.grouped_search import grouped_hits


class GroupedSearchTests(unittest.TestCase):
    def setUp(self):
        self.hit_a = {"id": "a1", "score": 0.91, "payload": {"arxiv_id": "a"}}
        self.hit_b = {"id": "b1", "score": 0.87, "payload": {"arxiv_id": "b"}}
        self.groups = [{"id": "a", "hits": [self.hit_a]},
                       {"id": "b", "hits": [self.hit_b]}]
        self.response = Mock()
        self.response.json.return_value = {"result": {"groups": self.groups}}
        self.post = Mock(return_value=self.response)

    def test_returns_best_hit_from_each_paper_and_preserves_request(self):
        qfilter = {"must": [{"key": "layer", "match": {"value": "web3"}}]}
        hits = grouped_hits(self.post, "http://qdrant/", "papers_fulltext",
                            [0.1, 0.2], qfilter, 2, 9)

        self.assertEqual(hits, [self.hit_a, self.hit_b])
        self.post.assert_called_once_with(
            "http://qdrant/collections/papers_fulltext/points/search/groups",
            json={"vector": [0.1, 0.2], "filter": qfilter,
                  "group_by": "arxiv_id", "limit": 2, "group_size": 1,
                  "with_payload": True, "with_vector": False},
            timeout=9,
        )
        self.response.raise_for_status.assert_called_once_with()

    def test_passes_requested_group_size(self):
        grouped_hits(self.post, "http://qdrant", "papers_fulltext", [0.1],
                     None, 3, 5, group_size=2)

        body = self.post.call_args.kwargs["json"]
        self.assertEqual(body["group_size"], 2)
        self.assertNotIn("filter", body)

    def test_missing_groups_returns_empty_list(self):
        self.response.json.return_value = {"result": {"groups": []}}

        self.assertEqual(grouped_hits(self.post, "http://qdrant", "papers_fulltext",
                                      [0.1], None, 1, 5), [])

    def test_malformed_response_raises_value_error(self):
        for payload in ({}, {"result": None}, {"result": {}},
                        {"result": {"groups": None}},
                        {"result": {"groups": [{"hits": None}]}}):
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                with self.assertRaises(ValueError):
                    grouped_hits(self.post, "http://qdrant", "papers_fulltext",
                                 [0.1], None, 1, 5)

    def test_nonpositive_group_size_rejected_without_request(self):
        with self.assertRaises(ValueError):
            grouped_hits(self.post, "http://qdrant", "papers_fulltext",
                         [0.1], None, 1, 5, group_size=0)
        self.post.assert_not_called()

    def test_nonpositive_limit_rejected_without_request(self):
        with self.assertRaises(ValueError):
            grouped_hits(self.post, "http://qdrant", "papers_fulltext",
                         [0.1], None, 0, 5)
        self.post.assert_not_called()

    def test_http_failure_propagates(self):
        error = requests.HTTPError("Qdrant error")
        self.response.raise_for_status.side_effect = error

        with self.assertRaises(requests.HTTPError) as caught:
            grouped_hits(self.post, "http://qdrant", "papers_fulltext",
                         [0.1], None, 1, 5)
        self.assertIs(caught.exception, error)


if __name__ == "__main__":
    unittest.main()
