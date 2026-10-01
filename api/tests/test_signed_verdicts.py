import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from fastapi import HTTPException

from api import main


def _fake_embed(text, *args, **kwargs):
    # a tiny fixed, L2-normalised-enough vector; only used so _canonical_claim
    # doesn't need a real embedding service to open a claim node
    return [1.0, 0.0, 0.0]


def _attestor_response(status_code, payload):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = str(payload)
    return resp


class SignedVerdictsTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # _judgments_conn creates it fresh
        self._patches = [
            patch.object(main, "JUDGMENTS_DB_PATH", self.db_path),
            patch.object(main, "embed_query", side_effect=_fake_embed),
            patch.object(main, "auth_and_limit", return_value=("test-key", {})),
            patch.object(main, "ATTESTOR_INTERNAL_TOKEN", "test-token"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass
        for ext in ("-wal", "-shm"):
            try:
                os.remove(self.db_path + ext)
            except OSError:
                pass

    def _rows(self, claim_text):
        conn = main._judgments_conn()
        try:
            return conn.execute(
                "SELECT * FROM claim_judgments WHERE claim_text=?", (claim_text,)
            ).fetchall()
        finally:
            conn.close()

    def test_successful_signed_verdict_is_recorded_with_attestation(self):
        claim = "Reed-Solomon codes achieve the Singleton bound"
        body = main.AdjudicateSignedBody(
            claim=claim,
            judgments=[main.SignedJudgmentItem(
                id="2401.12345", verdict="asserts", reviewer="Reviewer111",
                signature="Sig111", issued_at="2026-09-26T03:00:00Z",
            )],
        )
        attest_ok = _attestor_response(200, {
            "attestation": "AttestationPda111", "signature": "TxSig111",
            "explorer_url": "https://explorer.solana.com/tx/TxSig111?cluster=devnet",
        })
        with patch.object(main.requests, "post", return_value=attest_ok) as post:
            result = main.adjudicate_signed(body, x_api_key="anything")

        self.assertEqual(result["results"][0]["status"], 200)
        self.assertEqual(result["results"][0]["attestation_pda"], "AttestationPda111")
        rows = self._rows(claim)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["judged_by"], "wallet:Reviewer111")
        self.assertEqual(rows[0]["attestation_pda"], "AttestationPda111")
        self.assertEqual(rows[0]["attestation_tx"], "TxSig111")
        # attestor was called with the internal token, not a hardcoded one
        self.assertEqual(post.call_args.kwargs["headers"]["X-Internal-Token"], "test-token")

    def test_invalid_signature_is_not_recorded(self):
        claim = "a claim nobody has signed correctly"
        body = main.AdjudicateSignedBody(
            claim=claim,
            judgments=[main.SignedJudgmentItem(
                id="2401.99999", verdict="asserts", reviewer="Reviewer222",
                signature="BadSig", issued_at="2026-09-26T03:00:00Z",
            )],
        )
        bad_sig = _attestor_response(400, {"error": "signature does not verify"})
        with patch.object(main.requests, "post", return_value=bad_sig):
            result = main.adjudicate_signed(body, x_api_key="anything")

        self.assertEqual(result["results"][0]["status"], 400)
        self.assertIn("error", result["results"][0])
        self.assertEqual(self._rows(claim), [])

    def test_attestor_unavailable_raises_503_and_writes_nothing(self):
        claim = "a claim attestor never sees"
        body = main.AdjudicateSignedBody(
            claim=claim,
            judgments=[main.SignedJudgmentItem(
                id="2401.11111", verdict="asserts", reviewer="Reviewer333",
                signature="Sig333", issued_at="2026-09-26T03:00:00Z",
            )],
        )
        with patch.object(main.requests, "post",
                          side_effect=main.requests.RequestException("connection refused")):
            with self.assertRaises(HTTPException) as ctx:
                main.adjudicate_signed(body, x_api_key="anything")

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(self._rows(claim), [])

    def test_two_distinct_wallets_leave_the_verdict_pending(self):
        claim = "the protocol tolerates byzantine faults under partial synchrony"
        attest_a = _attestor_response(200, {"attestation": "PdaA", "signature": "TxA"})
        attest_b = _attestor_response(200, {"attestation": "PdaB", "signature": "TxB"})

        body_a = main.AdjudicateSignedBody(
            claim=claim,
            judgments=[main.SignedJudgmentItem(
                id="2402.00001", verdict="asserts", reviewer="WalletA",
                signature="SigA", issued_at="2026-09-26T03:00:00Z",
            )],
        )
        body_b = main.AdjudicateSignedBody(
            claim=claim,
            judgments=[main.SignedJudgmentItem(
                id="2402.00001", verdict="asserts", reviewer="WalletB",
                signature="SigB", issued_at="2026-09-26T03:01:00Z",
            )],
        )
        with patch.object(main.requests, "post", return_value=attest_a):
            main.adjudicate_signed(body_a, x_api_key="anything")
        with patch.object(main.requests, "post", return_value=attest_b):
            result = main.adjudicate_signed(body_b, x_api_key="anything")

        status = result["papers"]["2402.00001"]["status"]
        self.assertEqual(status, "read_once")
        self.assertNotIn(status, {"confirmed_prior_art", "ruled_out", "agreed_same_model"})
        self.assertEqual(result["papers"]["2402.00001"]["readers"], 2)
        self.assertEqual(result["papers"]["2402.00001"]["counts"], {"asserts": 2})
        self.assertEqual({r["judged_by_model"] for r in self._rows(claim)}, {"unspecified"})
        onchain = result["papers"]["2402.00001"]["onchain"]
        reviewers = {o["reviewer"] for o in onchain}
        self.assertEqual(reviewers, {"WalletA", "WalletB"})

    def test_many_wallets_cannot_confirm_or_rule_out_without_model_diversity(self):
        for verdict in ("asserts", "does_not_assert"):
            with self.subTest(verdict=verdict):
                claim = f"wallet-only agreement for verdict {verdict} remains pending"
                body = main.AdjudicateSignedBody(
                    claim=claim,
                    judgments=[main.SignedJudgmentItem(
                        id="2402.00003", verdict=verdict, reviewer=f"Wallet{i}",
                        signature=f"Sig{i}", issued_at="2026-09-26T03:00:00Z",
                    ) for i in range(5)],
                )
                responses = [_attestor_response(200, {
                    "attestation": f"Pda{i}", "signature": f"Tx{i}",
                }) for i in range(5)]
                with patch.object(main.requests, "post", side_effect=responses) as post:
                    result = main.adjudicate_signed(body, x_api_key="anything")

                paper = result["papers"]["2402.00003"]
                self.assertEqual(post.call_count, 5)
                self.assertTrue(all(item["status"] == 200 for item in result["results"]))
                self.assertGreater(paper["readers"], result["quorum"])
                self.assertEqual(paper["counts"], {verdict: 5})
                self.assertEqual(paper["status"], "read_once")
                self.assertNotIn(paper["status"], {"confirmed_prior_art", "ruled_out",
                                                 "agreed_same_model"})
                self.assertEqual(len(paper["onchain"]), 5)
                rows = self._rows(claim)
                self.assertEqual(len({r["judged_by"] for r in rows}), 5)
                self.assertEqual({r["judged_by_model"] for r in rows}, {"unspecified"})

    def test_conflicting_signed_wallets_remain_contested(self):
        claim = "signed wallet identities cannot resolve contradictory readings"
        body = main.AdjudicateSignedBody(
            claim=claim,
            judgments=[main.SignedJudgmentItem(
                id="2402.00004", verdict=verdict, reviewer=f"Wallet{i}",
                signature=f"Sig{i}", issued_at="2026-09-26T03:00:00Z",
            ) for i, verdict in enumerate(("asserts", "does_not_assert"))],
        )
        attest = _attestor_response(200, {"attestation": "PdaConflict", "signature": "TxConflict"})
        with patch.object(main.requests, "post", return_value=attest):
            result = main.adjudicate_signed(body, x_api_key="anything")
        paper = result["papers"]["2402.00004"]
        self.assertEqual(paper["status"], "contested")
        self.assertEqual(paper["counts"], {"asserts": 1, "does_not_assert": 1})
        self.assertEqual(paper["readers"], 2)

    def test_same_wallet_twice_does_not_confirm_on_its_own(self):
        claim = "a claim only one wallet ever reviews"
        attest = _attestor_response(200, {"attestation": "PdaC", "signature": "TxC"})

        body = main.AdjudicateSignedBody(
            claim=claim,
            judgments=[main.SignedJudgmentItem(
                id="2402.00002", verdict="asserts", reviewer="WalletC",
                signature="SigC1", issued_at="2026-09-26T03:00:00Z",
            )],
        )
        body_again = main.AdjudicateSignedBody(
            claim=claim,
            judgments=[main.SignedJudgmentItem(
                id="2402.00002", verdict="asserts", reviewer="WalletC",
                signature="SigC2", issued_at="2026-09-26T03:05:00Z",
            )],
        )
        with patch.object(main.requests, "post", return_value=attest):
            main.adjudicate_signed(body, x_api_key="anything")
        with patch.object(main.requests, "post", return_value=attest):
            result = main.adjudicate_signed(body_again, x_api_key="anything")

        status = result["papers"]["2402.00002"]["status"]
        self.assertNotEqual(status, "confirmed_prior_art")
        self.assertEqual(status, "read_once")
        rows = self._rows(claim)
        self.assertEqual(len(rows), 1)  # upsert, not a second row

    def test_unverified_legacy_model_names_cannot_settle_read_paths(self):
        for verdict in ("asserts", "does_not_assert"):
            with self.subTest(verdict=verdict):
                claim = f"legacy self-declared model diversity for {verdict} is untrusted"
                conn = main._judgments_conn()
                try:
                    claim_id, _ = main._canonical_claim(conn, claim)
                    for reader, model in (("key:LegacyA", "self-declared-model-a"),
                                          ("key:LegacyB", "self-declared-model-b")):
                        conn.execute(
                            "INSERT INTO claim_judgments "
                            "(claim_norm, claim_text, claim_id, paper_id, verdict, "
                            "judged_by, judged_by_model, judged_at) VALUES (?,?,?,?,?,?,?,?)",
                            (main._norm_claim(claim), claim, claim_id, "2402.00005",
                             verdict, reader, model, "2026-09-26T03:00:00Z"),
                        )
                    conn.commit()
                finally:
                    conn.close()
                readings = main._prior_readings(claim, ["2402.00005"])
                _, registry = main._registry_for_claim(claim)
                for name, records in (("prior_readings", readings), ("registry", registry)):
                    with self.subTest(read_path=name):
                        self.assertEqual(records["2402.00005"]["status"], "read_once")
                        self.assertEqual(records["2402.00005"]["counts"], {verdict: 2})


if __name__ == "__main__":
    unittest.main()
