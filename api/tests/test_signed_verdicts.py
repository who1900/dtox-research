import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from unittest.mock import patch, MagicMock

from fastapi import HTTPException

from api import main


def _fake_embed(text, *args, **kwargs):
    # a tiny fixed, L2-normalised-enough vector; only used so _canonical_claim
    # doesn't need a real embedding service to open a claim node
    return [1.0, 0.0, 0.0]


def _attestor_response(status_code, payload, issued_at="2026-09-26T03:00:00Z"):
    payload = dict(payload)
    if status_code == 200:
        payload.setdefault("issued_at_epoch_ms", int(datetime.fromisoformat(issued_at).timestamp() * 1000))
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

    def _node_count(self):
        if not os.path.exists(self.db_path):
            return 0
        conn = sqlite3.connect(self.db_path)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='claim_nodes'").fetchone():
                return 0
            return conn.execute("SELECT COUNT(*) FROM claim_nodes").fetchone()[0]
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
        self.assertTrue(result["new_claim_node"])
        self.assertEqual(self._node_count(), 1)
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
        before = self._node_count()
        with patch.object(main.requests, "post", return_value=bad_sig), \
             patch.object(main, "embed_query", side_effect=AssertionError("failed signatures must not embed")) as embed:
            result = main.adjudicate_signed(body, x_api_key="anything")

        self.assertEqual(result["results"][0]["status"], 400)
        self.assertIn("error", result["results"][0])
        self.assertFalse(result["new_claim_node"])
        self.assertEqual(self._node_count(), before)
        embed.assert_not_called()
        self.assertEqual(self._rows(claim), [])

    def test_prepare_message_does_not_create_a_database_or_node(self):
        body = main.VerdictMessageBody(claim="read-only preparation", paper_id="paper-a",
                                       verdict="asserts", issued_at="2026-09-26T03:00:00Z",
                                       evidence_sha256="a" * 64)
        message = {"message": "exact signable message", "claim_id": main._norm_claim(body.claim)}
        with patch.object(main.requests, "post", return_value=_attestor_response(200, message)) as post, \
             patch.object(main, "embed_query", side_effect=AssertionError("preview must not embed")) as embed:
            result = main.verdict_message(body)
        self.assertEqual(result["message"], message["message"])
        self.assertFalse(os.path.exists(self.db_path))
        embed.assert_not_called()
        self.assertEqual(post.call_args.kwargs["json"], {
            "claim_id": message["claim_id"], "claim_text": body.claim, "paper_id": body.paper_id,
            "verdict": body.verdict, "issued_at": body.issued_at, "evidence_sha256": body.evidence_sha256})

    def test_prepare_and_signed_write_preserve_existing_canonical_links(self):
        old, alias = "canonical protocol claim", "alias protocol claim"
        conn = main._judgments_conn()
        try:
            target, _ = main._canonical_claim(conn, old)
            conn.execute("INSERT INTO claim_links VALUES (?,?,?,?,?)",
                         (main._norm_claim(alias), target, "reader", "same question", "2026-09-26"))
            conn.commit()
        finally:
            conn.close()
        before = self._node_count()
        prepared = main.VerdictMessageBody(claim=alias, paper_id="paper-a", verdict="asserts",
                                           issued_at="2026-09-26T03:00:00Z")
        with patch.object(main.requests, "post", return_value=_attestor_response(200, {"message": "exact"})) as post, \
             patch.object(main, "embed_query", side_effect=AssertionError("linked preview must not embed")) as embed:
            self.assertEqual(main.verdict_message(prepared)["message"], "exact")
            self.assertEqual(post.call_args.kwargs["json"]["claim_id"], target)
            embed.assert_not_called()
        self.assertEqual(self._node_count(), before)
        body = main.AdjudicateSignedBody(claim=alias, judgments=[main.SignedJudgmentItem(
            id="paper-a", verdict="asserts", reviewer="WalletLink", signature="SigLink",
            issued_at=prepared.issued_at)])
        with patch.object(main.requests, "post", return_value=_attestor_response(
                200, {"attestation": "PdaLink", "signature": "TxLink"})) as post, \
             patch.object(main, "embed_query", side_effect=AssertionError("linked write needs no new vector")) as embed:
            result = main.adjudicate_signed(body)
        embed.assert_not_called()
        self.assertEqual(post.call_args.kwargs["json"]["claim_id"], target)
        self.assertEqual(result["claim_id"], target)
        self.assertFalse(result["new_claim_node"])
        self.assertEqual(self._node_count(), before)
        self.assertEqual(self._rows(alias)[0]["claim_id"], target)

    def test_successful_new_node_prepares_vector_after_attestor_and_before_writer_transaction(self):
        claim = "new node network boundary"
        body = main.AdjudicateSignedBody(claim=claim, judgments=[main.SignedJudgmentItem(
            id="paper-a", verdict="asserts", reviewer="WalletBoundary", signature="SigBoundary",
            issued_at="2026-09-26T03:00:00Z")])
        opened, events = [], []
        original_conn, original_canonical = main._judgments_conn, main._canonical_claim
        def connect():
            conn = original_conn()
            opened.append(conn)
            return conn
        def attest(*args, **kwargs):
            self.assertFalse(any(conn.in_transaction for conn in opened))
            events.append("attestor")
            return _attestor_response(200, {"attestation": "PdaBoundary", "signature": "TxBoundary"})
        def embed(text, *args, **kwargs):
            self.assertEqual(events, ["attestor"])
            self.assertFalse(any(conn.in_transaction for conn in opened))
            events.append("embed")
            return [1.0, 0.0, 0.0]
        def canonical(conn, text, **kwargs):
            self.assertTrue(conn.in_transaction)
            self.assertEqual(events, ["attestor", "embed"])
            self.assertEqual(kwargs["prepared_vector"], [1.0, 0.0, 0.0])
            events.append("local_insert")
            return original_canonical(conn, text, **kwargs)
        with patch.object(main, "_judgments_conn", side_effect=connect), \
             patch.object(main.requests, "post", side_effect=attest), \
             patch.object(main, "embed_query", side_effect=embed) as embedding, \
             patch.object(main, "_canonical_claim", side_effect=canonical):
            result = main.adjudicate_signed(body)
        self.assertEqual(events, ["attestor", "embed", "local_insert"])
        embedding.assert_called_once_with(claim)
        self.assertTrue(result["new_claim_node"])
        conn = main._judgments_conn()
        try:
            node = conn.execute("SELECT claim_id, vector FROM claim_nodes").fetchone()
            self.assertEqual(node["claim_id"], main._norm_claim(claim))
            self.assertEqual(json.loads(node["vector"]), [1.0, 0.0, 0.0])
        finally:
            conn.close()

    def test_embedding_unavailable_keeps_valid_signed_exact_text_record_without_a_node(self):
        body = main.AdjudicateSignedBody(claim="embedding fallback", judgments=[main.SignedJudgmentItem(
            id="paper-a", verdict="asserts", reviewer="WalletFallback", signature="SigFallback",
            issued_at="2026-09-26T03:00:00Z")])
        with patch.object(main.requests, "post", return_value=_attestor_response(
                200, {"attestation": "PdaFallback", "signature": "TxFallback"})), \
             patch.object(main, "embed_query", side_effect=HTTPException(503, "embedding unavailable")) as embed:
            result = main.adjudicate_signed(body)
        embed.assert_called_once_with(body.claim)
        self.assertFalse(result["new_claim_node"])
        self.assertEqual(self._node_count(), 0)
        self.assertEqual(self._rows(body.claim)[0]["claim_id"], main._norm_claim(body.claim))

    def test_failed_local_write_rolls_back_node_and_judgment_together(self):
        claim = "atomic signed write"
        body = main.AdjudicateSignedBody(claim=claim, judgments=[main.SignedJudgmentItem(
            id="paper-a", verdict="asserts", reviewer="WalletAtomic", signature="SigAtomic",
            issued_at="2026-09-26T03:00:00Z")])
        with patch.object(main.requests, "post", return_value=_attestor_response(
                200, {"attestation": "PdaAtomic", "signature": "TxAtomic"})), \
             patch.object(main, "_canonical_claim", side_effect=RuntimeError("node write failed")):
            with self.assertRaisesRegex(RuntimeError, "node write failed"):
                main.adjudicate_signed(body)
        self.assertEqual(self._node_count(), 0)
        self.assertEqual(self._rows(claim), [])

    def test_missing_attestation_cannot_create_a_claim_node(self):
        body = main.AdjudicateSignedBody(claim="unproven successful response", judgments=[main.SignedJudgmentItem(
            id="paper-a", verdict="asserts", reviewer="WalletUnproven", signature="SigUnproven",
            issued_at="2026-09-26T03:00:00Z")])
        with patch.object(main.requests, "post", return_value=_attestor_response(200, {"signature": "TxOnly"})):
            with self.assertRaises(HTTPException) as caught:
                main.adjudicate_signed(body)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self._node_count(), 0)
        self.assertEqual(self._rows(body.claim), [])

    def test_link_change_during_attestation_never_stores_under_a_different_signable_id(self):
        body = main.AdjudicateSignedBody(claim="claim whose link changes", judgments=[main.SignedJudgmentItem(
            id="paper-a", verdict="asserts", reviewer="WalletRaceLink", signature="SigRaceLink",
            issued_at="2026-09-26T03:00:00Z")])
        conn = main._judgments_conn()
        try:
            target, _ = main._canonical_claim(conn, "existing target")
        finally:
            conn.close()
        def attest(*args, **kwargs):
            self.assertEqual(kwargs["json"]["claim_id"], main._norm_claim(body.claim))
            conn = main._judgments_conn()
            try:
                conn.execute("INSERT INTO claim_links VALUES (?,?,?,?,?)",
                             (main._norm_claim(body.claim), target, "reader", "same question", "2026-10-02"))
                conn.commit()
            finally:
                conn.close()
            return _attestor_response(200, {"attestation": "PdaRaceLink", "signature": "TxRaceLink"})
        with patch.object(main.requests, "post", side_effect=attest):
            with self.assertRaises(HTTPException) as caught:
                main.adjudicate_signed(body)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self._node_count(), 1)
        self.assertEqual(self._rows(body.claim), [])

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
        self.assertEqual(self._node_count(), 0)
        self.assertEqual(self._rows(claim), [])

    def test_two_distinct_wallets_leave_the_verdict_pending(self):
        claim = "the protocol tolerates byzantine faults under partial synchrony"
        attest_a = _attestor_response(200, {"attestation": "PdaA", "signature": "TxA"})
        attest_b = _attestor_response(200, {"attestation": "PdaB", "signature": "TxB"},
                                      issued_at="2026-09-26T03:01:00Z")

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
        repeated_attest = _attestor_response(200, {"attestation": "PdaC", "signature": "TxC2"},
                                             issued_at=body_again.judgments[0].issued_at)
        with patch.object(main.requests, "post", return_value=repeated_attest):
            result = main.adjudicate_signed(body_again, x_api_key="anything")

        status = result["papers"]["2402.00002"]["status"]
        self.assertNotEqual(status, "confirmed_prior_art")
        self.assertEqual(status, "read_once")
        rows = self._rows(claim)
        self.assertEqual(len(rows), 1)  # upsert, not a second row

    def test_attestor_conflict_is_per_item_and_batch_continues(self):
        claim = "stale signed verdict must not abort a batch"
        current = main.SignedJudgmentItem(
            id="2402.00006", verdict="does_not_assert", reviewer="Wallet409",
            signature="CurrentSig", issued_at="2026-09-26T03:01:00Z")
        with patch.object(main.requests, "post", return_value=_attestor_response(
                200, {"attestation": "PdaCurrent", "signature": "TxCurrent"}, current.issued_at)):
            main.adjudicate_signed(main.AdjudicateSignedBody(claim=claim, judgments=[current]))
        before = dict(self._rows(claim)[0])
        body = main.AdjudicateSignedBody(claim=claim, judgments=[
            main.SignedJudgmentItem(id=current.id, verdict="asserts", reviewer=current.reviewer,
                                   signature="StaleSig", issued_at="2026-09-26T03:00:00Z"),
            main.SignedJudgmentItem(id="2402.00007", verdict="asserts", reviewer="WalletNext",
                                   signature="NextSig", issued_at="2026-09-26T03:00:00Z")])
        with patch.object(main.requests, "post", side_effect=[
                _attestor_response(409, {"error": "stale or conflicting verdict"}),
                _attestor_response(200, {"attestation": "PdaNext", "signature": "TxNext"})]) as post:
            result = main.adjudicate_signed(body)
        self.assertEqual(post.call_count, 2)
        self.assertEqual([item["status"] for item in result["results"]], [409, 200])
        self.assertEqual(result["results"][0]["error"], "stale or conflicting verdict")
        rows = {row["paper_id"]: dict(row) for row in self._rows(claim)}
        self.assertEqual(rows[current.id], before)
        self.assertEqual(rows["2402.00007"]["attestation_tx"], "TxNext")

    def test_delayed_older_response_cannot_replace_newer_signed_record(self):
        claim = "response receipt order must not roll back signed verdicts"
        older = main.SignedJudgmentItem(
            id="2402.00008", verdict="asserts", reason="older reason", reviewer="WalletRace",
            signature="OldSig", issued_at="2026-09-26T09:00:00+06:00")
        newer = main.SignedJudgmentItem(
            id=older.id, verdict="does_not_assert", reason="newer reason", reviewer=older.reviewer,
            signature="NewSig", issued_at="2026-09-26T03:01:00Z")
        old_requested, release_old = Event(), Event()

        def attest(url, headers, json, timeout):
            if json["signature"] == older.signature:
                old_requested.set()
                if not release_old.wait(5):
                    raise AssertionError("newer response never committed")
                return _attestor_response(200, {"attestation": "PdaRace", "signature": "TxOld"}, older.issued_at)
            return _attestor_response(200, {"attestation": "PdaRace", "signature": "TxNew"}, newer.issued_at)

        with patch.object(main.requests, "post", side_effect=attest):
            with ThreadPoolExecutor(max_workers=1) as executor:
                old_future = executor.submit(main.adjudicate_signed,
                                             main.AdjudicateSignedBody(claim=claim, judgments=[older]))
                try:
                    self.assertTrue(old_requested.wait(5))
                    new_result = main.adjudicate_signed(main.AdjudicateSignedBody(claim=claim, judgments=[newer]))
                    before = dict(self._rows(claim)[0])
                finally:
                    release_old.set()
                old_result = old_future.result(timeout=5)
        self.assertEqual(new_result["results"][0]["status"], 200)
        self.assertEqual(old_result["results"][0]["status"], 200)
        self.assertEqual(dict(self._rows(claim)[0]), before)
        self.assertEqual(before["verdict"], "does_not_assert")
        self.assertEqual(before["reason"], "newer reason")
        self.assertEqual(before["attestation_tx"], "TxNew")
        self.assertEqual(before["signed_issued_at_epoch_ms"], 1790391660000)
        self.assertEqual(old_result["papers"][older.id]["counts"], {"does_not_assert": 1})

    def test_equal_time_retry_preserves_all_metadata_and_transaction(self):
        claim = "exact attestation reuse must preserve the local transaction"
        item = main.SignedJudgmentItem(
            id="2402.00009", verdict="asserts", reason="original reason", reviewer="WalletRetry",
            signature="RetrySig", issued_at="2026-09-26T03:00:00Z")
        body = main.AdjudicateSignedBody(claim=claim, layer="web3", judgments=[item])
        with patch.object(main.requests, "post", return_value=_attestor_response(
                200, {"attestation": "PdaRetry", "signature": "TxOriginal", "reused": False})):
            main.adjudicate_signed(body)
        before = dict(self._rows(claim)[0])
        body.layer = "ai-agents"
        item.reason = "unsigned reason changed on retry"
        with patch.object(main.requests, "post", return_value=_attestor_response(
                200, {"attestation": "PdaRetry", "signature": None, "reused": True})):
            result = main.adjudicate_signed(body)
        self.assertEqual(result["results"][0]["status"], 200)
        self.assertEqual(dict(self._rows(claim)[0]), before)
        self.assertEqual(self._rows(claim)[0]["attestation_tx"], "TxOriginal")

    def test_unverified_epoch_metadata_cannot_write_a_signed_record(self):
        for value in (None, True, "1790391600000", 1790391600000.0, 8640000000000001):
            with self.subTest(value=value):
                claim = f"invalid ordering metadata {value!r}"
                body = main.AdjudicateSignedBody(claim=claim, judgments=[main.SignedJudgmentItem(
                    id="2402.00010", verdict="asserts", reviewer="WalletInvalid",
                    signature="InvalidMetaSig", issued_at="2026-09-26T03:00:00Z")])
                response = _attestor_response(200, {"attestation": "PdaInvalid", "signature": "TxInvalid",
                                                   "issued_at_epoch_ms": value})
                with patch.object(main.requests, "post", return_value=response):
                    with self.assertRaises(HTTPException) as caught:
                        main.adjudicate_signed(body)
                self.assertEqual(caught.exception.status_code, 503)
                self.assertEqual(self._node_count(), 0)
                self.assertEqual(self._rows(claim), [])

    def test_signed_epoch_migration_is_nullable_idempotent_and_upgrades_legacy(self):
        claim = "legacy signed row gains ordering without destructive migration"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE claim_judgments (claim_norm TEXT NOT NULL, claim_text TEXT NOT NULL, "
                         "paper_id TEXT NOT NULL, verdict TEXT NOT NULL, reason TEXT, judged_by TEXT NOT NULL, "
                         "judged_at TEXT NOT NULL, PRIMARY KEY (claim_norm, paper_id, judged_by))")
            conn.execute("INSERT INTO claim_judgments VALUES (?,?,?,?,?,?,?)",
                         (main._norm_claim(claim), claim, "2402.00011", "asserts", "legacy reason",
                          "wallet:WalletLegacy", "2026-09-26T03:00:00Z"))
            conn.commit()
        finally:
            conn.close()
        for _ in range(2):
            conn = main._judgments_conn()
            try:
                columns = conn.execute("PRAGMA table_info(claim_judgments)").fetchall()
                column = [row for row in columns if row["name"] == "signed_issued_at_epoch_ms"]
                self.assertEqual(len(column), 1)
                self.assertEqual(column[0]["type"], "INTEGER")
                self.assertEqual(column[0]["notnull"], 0)
                row = conn.execute("SELECT * FROM claim_judgments").fetchone()
                self.assertIsNone(row["signed_issued_at_epoch_ms"])
                self.assertEqual(row["reason"], "legacy reason")
            finally:
                conn.close()
        body = main.AdjudicateSignedBody(claim=claim, judgments=[main.SignedJudgmentItem(
            id="2402.00011", verdict="does_not_assert", reviewer="WalletLegacy",
            signature="LegacySig", issued_at="2026-09-26T03:00:00Z")])
        with patch.object(main.requests, "post", return_value=_attestor_response(
                200, {"attestation": "PdaLegacy", "signature": "TxLegacy"})):
            main.adjudicate_signed(body)
        rows = self._rows(claim)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "does_not_assert")
        self.assertEqual(rows[0]["signed_issued_at_epoch_ms"], 1790391600000)

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
