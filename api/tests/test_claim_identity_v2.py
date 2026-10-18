import hashlib
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from api import main


class ClaimIdentityV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "judgments.db")
        for mock in (patch.object(main, "JUDGMENTS_DB_PATH", self.path),
                     patch.object(main, "embed_query", return_value=[1.0]),
                     patch.object(main, "auth_and_limit", return_value=("reader", {}))):
            mock.start()
            self.addCleanup(mock.stop)

    def test_exact_utf8_identity_and_repeat(self):
        claims = ["X=1", "x=1", "x>1", "x<1", "x≤1", "安全", "危険", "é", "e\u0301", "x =1", "x=1 "]
        conn = main._judgments_conn()
        try:
            ids = []
            for text in claims:
                expected = "claim:v2:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
                self.assertEqual(main._norm_claim(text), expected)
                self.assertEqual(main._canonical_claim(conn, text), (expected, True))
                self.assertEqual(main._canonical_claim(conn, text), (expected, False))
                ids.append(expected)
            self.assertEqual(len(set(ids)), len(claims))
        finally:
            conn.close()

    def test_legacy_collision_is_not_reused_and_explicit_read_preserves_records(self):
        conn = main._judgments_conn()
        try:
            conn.execute("INSERT INTO claim_nodes VALUES (?,?,?,?,?)", ("x 1", "x 1", "X>1", "[1.0]", "old"))
            conn.execute("INSERT INTO claim_judgments (claim_norm,claim_text,claim_id,paper_id,verdict,judged_by,judged_at,attestation_pda) VALUES (?,?,?,?,?,?,?,?)",
                         ("x 1", "X>1", "x 1", "paper", "asserts", "wallet:old", "old", "old-pda"))
            conn.execute("INSERT INTO claim_links VALUES (?,?,?,?,?)", ("old alias", "x 1", "reader", "explicit", "old"))
            conn.commit()
            before = [tuple(row) for row in conn.execute("SELECT * FROM claim_judgments")]
            for text in ("X>1", "x<1", "X=1"):
                self.assertEqual(main._read_claim_id(conn, text), main._norm_claim(text))
            self.assertEqual(main._follow_claim_links(conn, "old alias"), "x 1")
            main._canonical_claim(conn, "X>1")
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM claim_judgments WHERE claim_norm='x 1'")], before)
        finally:
            conn.close()
        legacy = main.legacy_claim("x 1")
        self.assertEqual(legacy["identity_status"], "potentially_ambiguous")
        self.assertEqual(legacy["judgments"][0]["attestation_pda"], "old-pda")
        self.assertEqual(legacy["links"][0]["from_claim"], "old alias")
        self.assertEqual(main._prior_readings("x<1", ["paper"]), {})

    def test_v2_explicit_link_is_followed(self):
        conn = main._judgments_conn()
        try:
            alias = main._norm_claim("X=1")
            conn.execute("INSERT INTO claim_links VALUES (?,?,?,?,?)", (alias, "legacy explicit target", "reader", "read both", "now"))
            conn.commit()
            self.assertEqual(main._read_claim_id(conn, "X=1"), "legacy explicit target")
            self.assertEqual(main._read_claim_id(conn, "x=1"), main._norm_claim("x=1"))
        finally:
            conn.close()

    def test_new_api_uses_only_v2_internal_routes_and_does_not_write_on_old_attestor(self):
        from unittest.mock import MagicMock
        from fastapi import HTTPException
        response = MagicMock(status_code=404)
        response.json.return_value = {"error": "not found"}
        with patch.object(main.requests, "post", return_value=response) as post:
            with self.assertRaises(HTTPException) as error:
                main.verdict_message(main.VerdictMessageBody(claim="X=1", paper_id="paper", verdict="asserts"))
            self.assertEqual(error.exception.status_code, 502)
            self.assertTrue(post.call_args.args[0].endswith("/v2/message"))
            with self.assertRaises(HTTPException) as error:
                main.adjudicate_signed(main.AdjudicateSignedBody(claim="X=1", judgments=[main.SignedJudgmentItem(
                    id="paper", verdict="asserts", reviewer="old", signature="old", issued_at="2026-10-18T00:00:00Z")]))
            self.assertEqual(error.exception.status_code, 503)
            self.assertTrue(post.call_args.args[0].endswith("/v2/attest"))
        self.assertFalse(os.path.exists(self.path))

    def test_invalid_unicode_is_rejected_instead_of_replacement_hash(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as error:
            main._norm_claim("\ud800")
        self.assertEqual(error.exception.status_code, 400)

    def test_cross_language_utf8_golden_vector(self):
        self.assertEqual(main._norm_claim("X\u22641; \u5b89\u5168; e\u0301\U0001f600"),
                         "claim:v2:a4cf66e8fda8bf53cd20af97875007cead14624f040c3849d98e14f76908fdc9")
