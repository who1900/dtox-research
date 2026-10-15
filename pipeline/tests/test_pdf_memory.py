"""Vector-only exactness, allocation shape and build/scheduling contracts."""
import ast
import hashlib
import json
import math
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from pipeline import pdf_memory


class PDFMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((Path(__file__).parent / "fixtures/pdf_layout_1_28_oracle.json").read_text(encoding="utf-8"))
        assert cls.fixture["sources"]["common_util.py"]["sha256"] == pdf_memory.UPSTREAM_SHA256
        from importlib import metadata
        try:
            path = str(metadata.distribution("pymupdf_layout").locate_file("pymupdf/layout/common_util.py"))
            cls.source = Path(path).read_bytes()
            if hashlib.sha256(cls.source).hexdigest() != pdf_memory.UPSTREAM_SHA256:
                raise AssertionError("dense oracle source identity mismatch")
        except metadata.PackageNotFoundError:
            cls.source = None
        oracle = cls.fixture["sources"]["common_util.py"]["functions"]["get_edge_by_directional_nn"]
        tree = ast.parse(oracle)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "get_edge_by_directional_nn")
        ns = {"np": np, "math": math}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "versioned-dense-oracle", "exec"), ns)
        cls.dense = staticmethod(ns[node.name])

    def compare(self, boxes, threshold=50000, gap=0.3):
        with np.errstate(all="ignore"):
            old = self.dense(boxes, threshold, gap)
            for block in (1, 2, 7, 128):
                budget = len(boxes) * (16 * boxes.dtype.itemsize + 32) * block
                new = pdf_memory.get_edge_by_directional_nn(boxes, threshold, gap,
                                                            scratch_bytes=budget)
                self.assertEqual(old, new)
                self.assertEqual(list(old[1]), list(new[1]))
                np.testing.assert_array_equal(np.array(old[0], dtype=np.int64).T,
                                              np.array(new[0], dtype=np.int64).T)

    def test_seeded_dtype_geometry_and_block_boundaries(self):
        rng = np.random.default_rng(731)
        for dtype in (np.float32, np.float64):
            for n in (1, 2, 7, 17, 129):
                boxes = rng.integers(-20, 20, (n, 4)).astype(dtype)
                for gap in (0, 0.3, -0.2, 2):
                    for threshold in (0, 1, 3, 50000):
                        self.compare(boxes, threshold, gap)

    def test_ties_duplicates_degenerate_and_nonfinite(self):
        for dtype in (np.float32, np.float64):
            boxes = np.array([[0, 0, 1, 1], [0, 0, 1, 1], [-2, 0, -1, 1],
                              [2, 0, 3, 1], [0, -2, 1, -1], [0, 2, 1, 3],
                              [0, 2, 1, 3], [1, 1, 1, 1], [4, 4, -4, -4]], dtype=dtype)
            self.compare(boxes)
            for value in (np.inf, -np.inf, np.nan, np.finfo(dtype).max):
                for column in range(4):
                    changed = boxes.copy()
                    changed[2, column] = value
                    self.compare(changed)

    def test_empty_matches_failure(self):
        for fn in (self.dense, pdf_memory.get_edge_by_directional_nn):
            with self.assertRaises(ValueError):
                fn(np.empty((0, 4), dtype=np.float32), 10)

    def test_patch_is_static_guarded_and_unknown_builds_rejected(self):
        if self.source is not None:
            candidate = pdf_memory.patched_source(self.source)
            ns = {}
            node = next(n for n in ast.parse(candidate.decode()).body
                        if isinstance(n, ast.FunctionDef) and n.name == "get_edge_by_directional_nn")
            exec(compile(ast.Module(body=[node], type_ignores=[]), "fork", "exec"), ns)
            boxes = np.arange(68, dtype=np.float32).reshape(17, 4)
            self.assertEqual(self.dense(boxes, 100), ns[node.name](boxes, 100))
        original = self.source or b"unknown source"
        for source, version in ((original + b"\n", "1.28.0"), (original, "1.30.0")):
            with self.assertRaises(ValueError):
                pdf_memory.patched_source(source, version)

    def test_onnx_session_patch_is_source_guarded_and_applies_thread_config(self):
        source = b'''"""
common_util.py

Utils shared by the ONNX runtime users.

"""

import onnxruntime as ort

def make_session(model_path, providers=None):
    """
    Create an ort.InferenceSession with the CPU memory arena disabled.

    By default, ort.InferenceSession uses enable_cpu_mem_arena=True, which
    causes the ONNX Runtime allocator to retain the peak allocation across
    all calls and never release it back to the OS.  When a long-lived process
    processes many PDFs that differ in page size or element count, the arena
    grows without bound and is never reclaimed even after gc.collect().

    Setting enable_cpu_mem_arena=False makes the runtime release allocations
    promptly, keeping RSS stable across documents.
    """
    so = ort.SessionOptions()
    so.enable_cpu_mem_arena = False
    return ort.InferenceSession(model_path, sess_options=so, providers=providers)
'''
        self.assertEqual(hashlib.sha256(source).hexdigest(),
                         pdf_memory.ONNX_SESSION_UPSTREAM_SHA256)
        patched = pdf_memory.patched_onnx_session_source(source)
        compile(patched, "onnx/common_util.py", "exec")

        class SessionOptions:
            def __init__(self):
                self.enable_cpu_mem_arena = True
                self.intra_op_num_threads = 0
                self.inter_op_num_threads = 0

        import sys
        import types
        runtime = types.ModuleType("onnxruntime")
        runtime.SessionOptions = SessionOptions
        runtime.InferenceSession = lambda model_path, *, sess_options, providers: (
            model_path, sess_options, providers)
        namespace = {}
        with patch.dict(sys.modules, {"onnxruntime": runtime}):
            exec(compile(patched, "onnx/common_util.py", "exec"), namespace)
        with patch.dict(os.environ, {"DTOX_NATIVE_ORT_THREADS": "3"}):
            _, configured, providers = namespace["make_session"]("model.onnx", ["CPUExecutionProvider"])
        self.assertFalse(configured.enable_cpu_mem_arena)
        self.assertEqual(configured.intra_op_num_threads, 3)
        self.assertEqual(configured.inter_op_num_threads, 1)
        self.assertEqual(providers, ["CPUExecutionProvider"])
        with patch.dict(os.environ, {"DTOX_NATIVE_ORT_THREADS": "0"}):
            with self.assertRaisesRegex(ValueError, "must be a positive integer"):
                namespace["make_session"]("model.onnx", None)
        for original, version in ((source + b"\n", "1.28.0"), (source, "1.30.0")):
            with self.assertRaises(ValueError):
                pdf_memory.patched_onnx_session_source(original, version)

        import inspect
        import tempfile
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "pymupdf"
            layout = package / "layout"
            onnx = layout / "onnx"
            onnx.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            layout_source = layout / "common_util.py"
            directional_patch = inspect.getsource(pdf_memory.get_edge_by_directional_nn).encode("utf-8")
            layout_source.write_bytes(directional_patch)
            (layout / "common_util.dtox-upstream").write_bytes(b"upstream fixture")
            onnx_source = onnx / "common_util.py"
            onnx_source.write_bytes(source)
            spec = SimpleNamespace(origin=str(package / "__init__.py"))
            with patch("importlib.metadata.version", return_value="1.28.0"), \
                    patch("importlib.util.find_spec", return_value=spec), \
                    patch("pipeline.pdf_memory.patched_source", return_value=directional_patch):
                baseline = pdf_memory.verify_native_build(directory)
                self.assertFalse(baseline["ort_thread_patch_configured"])
                onnx_source.with_name("common_util.dtox-upstream").write_bytes(source)
                onnx_source.write_bytes(patched)
                with patch.dict(os.environ, {"DTOX_NATIVE_ORT_THREADS": "2"}):
                    configured = pdf_memory.verify_native_build(directory)
            self.assertTrue(configured["ort_thread_patch_configured"])
            self.assertEqual(configured["ort_intra_op_threads_default"], 2)
            self.assertEqual(configured["ort_intra_op_threads"], 2)
            self.assertEqual(configured["ort_inter_op_threads"], 1)

    def test_actual_model_input_arrays_and_geometry_branches(self):
        def namespace(nn):
            ns = {"np": np, "math": math, "IGNORE_FEATURE_NAMES": []}
            selected = {
                "common_util.py": {"get_boxes_transform", "get_edge_by_knn", "get_edge_by_alignment",
                                   "get_edge_transform_bbox", "get_edge_transform_bbox_add_alignment", "get_text_pattern"},
                "pymupdf_util_edge.py": {"get_edge_attr", "get_edge_dim", "build_edge_index"},
                "onnx/BoxRFDGNN.py": {"get_rf_features", "get_nn_input_from_datadict"},
            }
            for file, names in selected.items():
                tree = ast.parse("\n\n".join(self.fixture["sources"][file]["functions"].values()))
                nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
                self.assertEqual(len(nodes), len(names))
                exec(compile(ast.Module(body=nodes, type_ignores=[]), file, "exec"), ns)
            ns["get_edge_by_directional_nn"] = nn
            return ns
        old, new = namespace(self.dense), namespace(pdf_memory.get_edge_by_directional_nn)
        rng = np.random.default_rng(891)
        boxes = rng.integers(0, 100, (23, 4)).astype(np.float32)
        data = {"bboxes": boxes.tolist(), "text": ["Item 42 • α"] * len(boxes),
                "custom_features": [{"box_type": "text", "size": 12}] * len(boxes)}
        for edge_type in ("", "SEO"):
            for sampling in ("4D", "4D+AL"):
                cfg = {"data": {"rf_names": ["is_text", "is_image", "size"]},
                       "model": {"sample_k": 2, "option": {"edge_type": edge_type}}}
                for include_knn in (False, True):
                    with np.errstate(all="ignore"):
                        a = old["get_nn_input_from_datadict"](data, cfg, include_knn, sampling)
                        b = new["get_nn_input_from_datadict"](data, cfg, include_knn, sampling)
                    for left, right in zip(a, b):
                        if left is None:
                            self.assertIsNone(right)
                        else:
                            self.assertEqual(left.dtype, right.dtype)
                            np.testing.assert_array_equal(left, right)

    def test_large_vector_only_workspace_shape_and_peak(self):
        import tracemalloc
        n = 4096
        boxes = np.arange(n * 4, dtype=np.float64).reshape(n, 4)
        budget = 2 * 1024 * 1024
        shapes = []
        real_argmin = np.argmin
        def observed(array, *args, **kwargs):
            shapes.append(array.shape)
            return real_argmin(array, *args, **kwargs)
        tracemalloc.start()
        try:
            with patch.object(np, "argmin", new=observed):
                edges, directions = pdf_memory.get_edge_by_directional_nn(boxes, 50000,
                                                                          scratch_bytes=budget)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(edges)
        self.assertEqual(sum(len(v) for v in directions.values()), 4 * n - 6)
        self.assertTrue(all(cols == n and rows <= budget // (n * 160) for rows, cols in shapes))
        self.assertLess(peak, 24 * 1024 * 1024)
        print(f"vector-only N={n} scratch_target={budget} traced_peak={peak} max_rows={max(r for r,c in shapes)}")

    def test_native_slot_serializes_all_jobs_and_releases_on_failure(self):
        from concurrent.futures import ThreadPoolExecutor
        lock = threading.Lock()
        active = peak = finished = 0
        def job(i):
            nonlocal active, peak, finished
            try:
                with pdf_memory.NATIVE_JOB_SLOTS:
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    try:
                        time.sleep(0.01)
                        if i == 2:
                            raise ValueError("fixture failure")
                    finally:
                        with lock:
                            active -= 1
                            finished += 1
            except ValueError:
                pass
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(job, range(9)))
        self.assertEqual((peak, active, finished), (1, 0, 9))

    def test_runtime_guard_rejects_wrong_version_and_missing_fork(self):
        with patch("importlib.metadata.version", return_value="1.30.0"):
            with self.assertRaisesRegex(ValueError, "version"):
                pdf_memory.verify_native_build("missing-build")
        with patch("importlib.metadata.version", return_value="1.28.0"), \
                patch("importlib.util.find_spec", return_value=None):
            with self.assertRaisesRegex(ValueError, "missing or not selected"):
                pdf_memory.verify_native_build("missing-build")
