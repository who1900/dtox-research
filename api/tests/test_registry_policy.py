import inspect
import unittest

from api.registry_policy import judgment_status


class RegistryPolicyTests(unittest.TestCase):
    def test_compatible_signature(self):
        signature = inspect.signature(judgment_status)
        self.assertEqual(list(signature.parameters), ["counts", "readers", "models", "quorum"])
        self.assertIsNone(signature.parameters["readers"].default)
        self.assertIsNone(signature.parameters["models"].default)
        self.assertEqual(signature.parameters["quorum"].default, 2)

    def test_basic_statuses(self):
        cases = [
            ({}, "unread"),
            ({"asserts": 0, "does_not_assert": 0, "partial": 0}, "unread"),
            ({"asserts": 1}, "read_once"),
            ({"does_not_assert": 1}, "read_once"),
            ({"partial": 5}, "read_once"),
            ({"asserts": 1, "does_not_assert": 1}, "contested"),
            ({"asserts": 10, "does_not_assert": 1}, "contested"),
            ({"asserts": 1, "does_not_assert": 10}, "contested"),
        ]
        for counts, expected in cases:
            with self.subTest(counts=counts):
                self.assertEqual(judgment_status(counts), expected)

    def test_unspecified_models_never_settle(self):
        for models in (None, [], [None, "", " "], ["unspecified"],
                       [" Unspecified ", "UNSPECIFIED", "", None]):
            for verdict in ("asserts", "does_not_assert"):
                with self.subTest(models=models, verdict=verdict):
                    self.assertEqual(judgment_status({verdict: 20}, models=models), "read_once")

    def test_distinct_wallets_are_pending_not_independent(self):
        for verdict in ("asserts", "does_not_assert"):
            for models in (None, [], ["unspecified"]):
                with self.subTest(verdict=verdict, models=models):
                    self.assertEqual(judgment_status(
                        {verdict: 3}, ["wallet:A", "wallet:B", "wallet:C"], models),
                        "read_once")

    def test_single_string_cannot_create_character_based_model_diversity(self):
        for model, expected in (("unspecified", "read_once"),
                                ("model-a", "agreed_same_model")):
            with self.subTest(model=model):
                self.assertEqual(judgment_status({"asserts": 2}, models=model), expected)

    def test_distinct_api_keys_without_models_are_pending(self):
        self.assertEqual(judgment_status({"asserts": 2}, ["key:A", "key:B"]), "read_once")

    def test_reader_pseudonyms_in_models_never_supply_model_diversity(self):
        for models in (["key:A", "key:B"], ["wallet:A", "wallet:B"],
                       ["key:A", "wallet:B"], [" KEY:A ", " Wallet:B "],
                       ["key:A", "wallet:B", "unspecified"], "key:A"):
            for verdict in ("asserts", "does_not_assert"):
                with self.subTest(models=models, verdict=verdict):
                    self.assertEqual(judgment_status({verdict: 10}, models=models), "read_once")

    def test_reader_pseudonym_cannot_be_the_second_trusted_model(self):
        for pseudonym in ("key:A", "wallet:B"):
            with self.subTest(pseudonym=pseudonym):
                self.assertEqual(judgment_status(
                    {"asserts": 2}, models=["model-a", pseudonym]), "agreed_same_model")

    def test_same_known_model_agreement(self):
        for verdict in ("asserts", "does_not_assert"):
            for models in (["model-a"], ["model-a", "model-a"],
                           ["model-a", "unspecified"], ["model-a", " model-a "]):
                with self.subTest(verdict=verdict, models=models):
                    self.assertEqual(judgment_status({verdict: 2}, models=models),
                                     "agreed_same_model")

    def test_trusted_model_diversity_settles_both_verdicts(self):
        for verdict, expected in (("asserts", "confirmed_prior_art"),
                                  ("does_not_assert", "ruled_out")):
            with self.subTest(verdict=verdict):
                self.assertEqual(judgment_status(
                    {verdict: 2}, ["key:A", "key:B"], ["model-a", "model-b"]), expected)

    def test_diversity_without_numerical_quorum_is_pending(self):
        for verdict in ("asserts", "does_not_assert"):
            with self.subTest(verdict=verdict):
                self.assertEqual(judgment_status(
                    {verdict: 2}, models=["model-a", "model-b"], quorum=3), "read_once")

    def test_custom_quorum_with_diversity(self):
        self.assertEqual(judgment_status(
            {"asserts": 3}, models=["model-a", "model-b"], quorum=3), "confirmed_prior_art")

    def test_conflict_takes_precedence_over_quorum_and_diversity(self):
        self.assertEqual(judgment_status(
            {"asserts": 3, "does_not_assert": 1}, models=["model-a", "model-b"]), "contested")

    def test_partial_does_not_supply_decisive_quorum(self):
        self.assertEqual(judgment_status(
            {"asserts": 1, "partial": 10}, models=["model-a", "model-b"]), "read_once")

    def test_inputs_are_not_mutated(self):
        counts, readers, models = {"asserts": 2}, ["wallet:A", "wallet:B"], ["unspecified"]
        judgment_status(counts, readers, models)
        self.assertEqual(counts, {"asserts": 2})
        self.assertEqual(readers, ["wallet:A", "wallet:B"])
        self.assertEqual(models, ["unspecified"])

    def test_invalid_quorum(self):
        for quorum in (0, -1, 1.5, True, "2", None):
            with self.subTest(quorum=quorum):
                with self.assertRaises(ValueError):
                    judgment_status({}, quorum=quorum)


if __name__ == "__main__":
    unittest.main()
