import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from eval.splade_torch_backend import (
    DEFAULT_CACHE_DIR,
    DEFAULT_MAX_LENGTH,
    DEFAULT_MODEL_SOURCE,
    pool_splade_numpy,
    sparse_embeddings,
    SpladeTorchBackend,
    TorchSpladeEncoder,
)


class SpladeTorchPoolingTests(unittest.TestCase):
    def test_pool_uses_relu_log1p_mask_and_max(self):
        logits = np.array([
            [[-2.0, 1.0, 0.0, 2.0], [3.0, 0.0, 4.0, -1.0], [100.0] * 4]
        ])
        mask = np.array([[1, 1, 0]])
        got = pool_splade_numpy(logits, mask)
        expected = np.log1p(np.array([[3.0, 1.0, 4.0, 2.0]]))
        np.testing.assert_allclose(got, expected)

    def test_sparse_vectors_are_positive_finite_and_sorted(self):
        got = sparse_embeddings(np.array([[0.5, 0.0, np.nan, 2.0, -1.0, np.inf]]))
        self.assertEqual(got[0].indices, [0, 3])
        self.assertEqual(got[0].values, [0.5, 2.0])

    def test_sparse_vectors_accept_single_vector(self):
        got = sparse_embeddings(np.array([0.0, 1.25]))
        self.assertEqual((got[0].indices, got[0].values), ([1], [1.25]))

    def test_pool_rejects_incompatible_shapes(self):
        with self.assertRaises(ValueError):
            pool_splade_numpy(np.zeros((1, 2, 3)), np.ones((1, 1)))

    def test_default_source_is_explicit_hugging_face_source(self):
        self.assertEqual(DEFAULT_MODEL_SOURCE, "prithivida/Splade_PP_en_v1")

    def test_production_length_and_local_cache_defaults(self):
        self.assertEqual(DEFAULT_MAX_LENGTH, 512)
        self.assertEqual(
            DEFAULT_CACHE_DIR,
            "C:/Users/User/Documents/work/Claude/.scratch_dtox/splade-20261003/models",
        )

    def test_torch_encoder_compatibility_alias(self):
        self.assertIs(TorchSpladeEncoder, SpladeTorchBackend)

    def test_precision_validation_does_not_load_model(self):
        with self.assertRaisesRegex(ValueError, "fp16"):
            TorchSpladeEncoder(precision="fp32")

    def test_batch_size_argument_keeps_single_item_inference(self):
        encoder = TorchSpladeEncoder.__new__(TorchSpladeEncoder)
        seen = []
        encoder._encode_one = lambda text: seen.append(text) or text
        self.assertEqual(list(encoder.embed(["a", "b"], batch_size=8)), ["a", "b"])
        self.assertEqual(seen, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
