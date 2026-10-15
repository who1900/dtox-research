"""Exact row-block layout NN and a build-time-only, source-guarded patch.

No runtime replacement of imported functions or mutation of site-packages.
"""
import ast
import hashlib
import inspect
import os
import threading
from pathlib import Path


UPSTREAM_VERSION = "1.28.0"
UPSTREAM_SHA256 = "f51244f60096f8b01181b330f14d02a5a1f9c9f6755a63e115d553cb80628680"
PATCH_REVISION = "dtox-directional-nn-row-block-v1"
ORT_THREAD_PATCH_REVISION = "dtox-onnx-session-threads-v1"
ONNX_SESSION_UPSTREAM_SHA256 = "ab441d52104a06055206694553d5132b084fed538ef2ba40ecc46b613a3d6b63"
ORT_THREADS_ENV = "DTOX_NATIVE_ORT_THREADS"
ORT_THREADS_DEFAULT = 2
SCRATCH_BYTES = 64 * 1024 * 1024
_native_jobs = int(os.getenv("DTOX_NATIVE_PDF_JOBS", "1"))
if _native_jobs < 1:
    raise ValueError("DTOX_NATIVE_PDF_JOBS must be positive")
NATIVE_JOB_SLOTS = threading.BoundedSemaphore(_native_jobs)


def bind_parent_lifetime(expected_parent_pid):
    """Linux fresh-child only: fail closed on death before/during registration."""
    import sys
    if sys.platform != "linux":
        return
    import ctypes
    import os
    import signal

    if expected_parent_pid <= 0 or os.getppid() != expected_parent_pid:
        raise SystemExit(87)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG failed")
    if os.getppid() != expected_parent_pid:
        raise SystemExit(87)


def get_edge_by_directional_nn(bbox, dist_threshold, vertical_gap=0.3,
                               *, scratch_bytes=64 * 1024 * 1024):
    import math
    import numpy as np

    n = len(bbox)
    if not n:
        # The dense implementation rejects zero rows at reshape, too.
        raise ValueError("cannot reshape an empty directional NN graph")
    if scratch_bytes <= 0:
        raise ValueError("scratch_bytes must be positive")
    # Conservative allowance: 16 dtype arrays + 32 bool bytes per pair.
    # One complete row is the O(N) floor, not a document/element size cap.
    rows = min(128, max(1, scratch_bytes // (n * (16 * bbox.dtype.itemsize + 32))))
    cy = (bbox[:, 1] + bbox[:, 3]) / 2
    cx = (bbox[:, 0] + bbox[:, 2]) / 2
    height = bbox[:, 3] - bbox[:, 1]
    low = bbox[:, 1] - height * vertical_gap
    high = bbox[:, 3] + height * vertical_gap
    directions = {key: [] for key in
                  ("left", "right", "up", "down", "down_y", "second_up", "second_down")}

    def collect(matrix, key, start):
        valid = np.min(matrix, axis=1) != math.inf
        nearest = np.argmin(matrix, axis=1)
        directions[key].extend((start + int(i), int(nearest[i]))
                               for i in range(len(matrix)) if valid[i])
        return valid, nearest

    for start in range(0, n, rows):
        stop = min(start + rows, n)
        box = bbox[start:stop]
        eye = np.arange(start, stop)[:, None] == np.arange(n)[None, :]
        yf = np.logical_or(cy[None, :] < low[start:stop, None],
                           cy[None, :] > high[start:stop, None])
        xf = np.logical_or(cx[None, :] < box[:, 0, None],
                           cx[None, :] > box[:, 2, None])
        hx = (bbox[None, :, 0] - box[:, 2, None]) ** 2
        hy = (bbox[None, :, 1] - box[:, 1, None]) ** 2
        vx = (bbox[None, :, 0] - box[:, 0, None]) ** 2
        vy = (bbox[None, :, 1] - box[:, 3, None]) ** 2
        for key, mask in (
            ("left", bbox[None, :, 2] <= box[:, 2, None]),
            ("right", bbox[None, :, 0] >= box[:, 0, None]),
        ):
            matrix = hx + hy
            matrix[np.logical_or.reduce((yf, eye, mask))] = math.inf
            collect(matrix, key, start)
        for key, mask in (
            ("up", bbox[None, :, 3] <= box[:, 3, None]),
            ("down", bbox[None, :, 1] >= box[:, 1, None]),
        ):
            matrix = vx + vy
            matrix[np.logical_or(eye, mask)] = math.inf
            valid, nearest = collect(matrix, key, start)
            for i in range(len(matrix)):
                if valid[i]:
                    matrix[i, nearest[i]] = math.inf
            collect(matrix, "second_" + key, start)
        matrix = np.copy(vy)
        matrix[np.logical_or.reduce((eye, xf, bbox[None, :, 3] <= box[:, 3, None]))] = math.inf
        collect(matrix, "down_y", start)
        # Do not retain the previous block while evaluating the next one.
        del eye, yf, xf, hx, hy, vx, vy, matrix, mask, valid, nearest
    raw = []
    for edges in directions.values():
        raw.extend(edges)
    normalized = list(set((min(edge), max(edge)) for edge in raw))
    return [edge for edge in normalized if abs(edge[1] - edge[0]) < dist_threshold], directions


def patched_source(original, version=UPSTREAM_VERSION):
    """Render a reviewed fork file; reject unknown versions and source bytes."""
    if version != UPSTREAM_VERSION or hashlib.sha256(original).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("unsupported native layout build")
    text = original.decode("utf-8")
    nodes = [node for node in ast.parse(text).body
             if isinstance(node, ast.FunctionDef) and node.name == "get_edge_by_directional_nn"]
    if len(nodes) != 1:
        raise ValueError("unknown directional NN source structure")
    node = nodes[0]
    lines = text.splitlines(keepends=True)
    replacement = inspect.getsource(get_edge_by_directional_nn)
    result = "".join(lines[:node.lineno - 1]) + replacement + "".join(lines[node.end_lineno:])
    compile(result, "common_util.py", "exec")
    return result.encode("utf-8")


def _patched_onnx_make_session(model_path, providers=None):
    """Build an ONNX CPU session with an explicit, operator-configurable cap."""
    import os

    so = ort.SessionOptions()
    so.enable_cpu_mem_arena = False
    intra_op_threads = int(os.environ.get("DTOX_NATIVE_ORT_THREADS", "2"))
    if intra_op_threads < 1:
        raise ValueError("DTOX_NATIVE_ORT_THREADS must be a positive integer")
    so.intra_op_num_threads = intra_op_threads
    so.inter_op_num_threads = 1
    return ort.InferenceSession(model_path, sess_options=so, providers=providers)


def patched_onnx_session_source(original, version=UPSTREAM_VERSION):
    """Patch only the verified 1.28.0 ONNX session factory source."""
    if (version != UPSTREAM_VERSION
            or hashlib.sha256(original).hexdigest() != ONNX_SESSION_UPSTREAM_SHA256):
        raise ValueError("unsupported native ONNX session helper")
    text = original.decode("utf-8")
    nodes = [node for node in ast.parse(text).body
             if isinstance(node, ast.FunctionDef) and node.name == "make_session"]
    if len(nodes) != 1:
        raise ValueError("unknown ONNX session factory structure")
    node = nodes[0]
    lines = text.splitlines(keepends=True)
    replacement = inspect.getsource(_patched_onnx_make_session).replace(
        "def _patched_onnx_make_session(", "def make_session(", 1)
    result = "".join(lines[:node.lineno - 1]) + replacement + "".join(lines[node.end_lineno:])
    compile(result, "onnx/common_util.py", "exec")
    return result.encode("utf-8")


def verify_native_build(import_root):
    """Validate before native import; unpatched/unknown builds fail closed."""
    from importlib import metadata
    for package in ("pymupdf", "pymupdf_layout", "pymupdf4llm"):
        if metadata.version(package) != UPSTREAM_VERSION:
            raise ValueError("unsupported native layout version")
    # Resolve the actual import tree, not the original distribution's RECORD.
    from importlib.util import find_spec
    spec = find_spec("pymupdf")
    if spec is None or Path(spec.origin).resolve().parent != Path(import_root).resolve() / "pymupdf":
        raise ValueError("native dependency build missing or not selected")
    source = Path(spec.origin).parent / "layout" / "common_util.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name == "get_edge_by_directional_nn"]
    if len(nodes) != 1:
        raise ValueError("unsupported native layout source")
    lines = source.read_bytes().decode("utf-8").splitlines(keepends=True)
    node = nodes[0]
    actual = "".join(lines[node.lineno - 1:node.end_lineno]).rstrip()
    expected = inspect.getsource(get_edge_by_directional_nn).rstrip()
    if actual != expected:
        raise ValueError("native layout memory patch missing")
    # Reconstitute the exact upstream file from the fixed build artifact.
    original = source.with_name("common_util.dtox-upstream")
    upstream = original.read_bytes()
    if source.read_bytes() != patched_source(upstream):
        raise ValueError("native layout source hash mismatch")
    onnx_source = source.parent / "onnx" / "common_util.py"
    onnx_bytes = onnx_source.read_bytes()
    onnx_upstream = onnx_source.with_name("common_util.dtox-upstream")
    if onnx_upstream.exists():
        onnx_original = onnx_upstream.read_bytes()
        if hashlib.sha256(onnx_original).hexdigest() != ONNX_SESSION_UPSTREAM_SHA256:
            raise ValueError("native ONNX session helper backup hash mismatch")
        if onnx_bytes != patched_onnx_session_source(onnx_original):
            raise ValueError("native ONNX session thread patch mismatch")
        onnx_threads_configured = True
    elif hashlib.sha256(onnx_bytes).hexdigest() == ONNX_SESSION_UPSTREAM_SHA256:
        onnx_threads_configured = False
    else:
        raise ValueError("native ONNX session helper is unknown or unverified")
    ort_intra_op_threads = None
    if onnx_threads_configured:
        try:
            ort_intra_op_threads = int(os.environ.get(ORT_THREADS_ENV, str(ORT_THREADS_DEFAULT)))
        except ValueError as exc:
            raise ValueError(ORT_THREADS_ENV + " must be a positive integer") from exc
        if ort_intra_op_threads < 1:
            raise ValueError(ORT_THREADS_ENV + " must be a positive integer")
    return {"revision": PATCH_REVISION, "upstream_sha256": UPSTREAM_SHA256,
            "patched_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "ort_thread_patch_revision": ORT_THREAD_PATCH_REVISION if onnx_threads_configured else None,
            "ort_thread_patch_configured": onnx_threads_configured,
            "ort_thread_env": ORT_THREADS_ENV if onnx_threads_configured else None,
            "ort_intra_op_threads_default": ORT_THREADS_DEFAULT if onnx_threads_configured else None,
            "ort_intra_op_threads": ort_intra_op_threads,
            "ort_inter_op_threads": 1 if onnx_threads_configured else None,
            "onnx_session_upstream_sha256": ONNX_SESSION_UPSTREAM_SHA256,
            "onnx_session_sha256": hashlib.sha256(onnx_bytes).hexdigest()}


def build_dependency_patch(package_root, output, version=UPSTREAM_VERSION):
    """Materialize a separate reviewable fork tree, never edit the input tree."""
    import difflib
    import json
    import shutil

    package_root, output = Path(package_root).resolve(), Path(output).resolve()
    if output == package_root or package_root in output.parents:
        raise ValueError("fork output must be outside the installed package")
    original = (package_root / "layout/common_util.py").read_bytes()
    patched = patched_source(original, version)
    onnx_original = (package_root / "layout/onnx/common_util.py").read_bytes()
    onnx_patched = patched_onnx_session_source(onnx_original, version)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(package_root, output / "pymupdf",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    target = output / "pymupdf/layout/common_util.py"
    target.with_name("common_util.dtox-upstream").write_bytes(original)
    target.write_bytes(patched)
    onnx_target = output / "pymupdf/layout/onnx/common_util.py"
    onnx_target.with_name("common_util.dtox-upstream").write_bytes(onnx_original)
    onnx_target.write_bytes(onnx_patched)
    diff = "".join(difflib.unified_diff(original.decode().splitlines(True),
                    patched.decode().splitlines(True),
                    "a/pymupdf/layout/common_util.py", "b/pymupdf/layout/common_util.py"))
    diff += "".join(difflib.unified_diff(onnx_original.decode().splitlines(True),
                    onnx_patched.decode().splitlines(True),
                    "a/pymupdf/layout/onnx/common_util.py", "b/pymupdf/layout/onnx/common_util.py"))
    (output / "directional-nn.patch").write_text(diff, encoding="utf-8", newline="")
    identity = {"revision": PATCH_REVISION, "version": version,
                "upstream_sha256": UPSTREAM_SHA256,
                "patched_sha256": hashlib.sha256(patched).hexdigest(),
                "ort_thread_patch_revision": ORT_THREAD_PATCH_REVISION,
                "onnx_session_upstream_sha256": ONNX_SESSION_UPSTREAM_SHA256,
                "onnx_session_patched_sha256": hashlib.sha256(onnx_patched).hexdigest(),
                "ort_thread_env": ORT_THREADS_ENV,
                "ort_intra_op_threads_default": ORT_THREADS_DEFAULT,
                "ort_inter_op_threads": 1,
                "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "scratch_target_bytes": SCRATCH_BYTES, "native_job_slots": _native_jobs,
                "runtime_patch": False, "production_applied": False}
    (output / "build-identity.json").write_text(json.dumps(identity, indent=2), encoding="utf-8")
    return identity
