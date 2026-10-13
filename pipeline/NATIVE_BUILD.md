# Native PDF dependency build

The root `requirements.txt` declares the existing runtime trio:
`pymupdf4llm==1.28.0`, `pymupdf==1.28.0`, `pymupdf_layout==1.28.0`.
These pins alone do **not** provide the row-block NN fix. A separately built,
reviewed static fork is mandatory; no runtime function monkey-patching.

Build in a staged release using the target platform's already installed matching
dependencies. No install or model download is required by the steps below.
Do not copy Windows native binaries to Linux. NumPy/ONNX/native ABI and model
identity remain separate release gates; the trio pins do not prove equivalence.

From the staged repository root, create a new, non-existing output directory:

```sh
python -c 'from importlib import metadata; from pipeline.pdf_memory import build_dependency_patch; root=metadata.distribution("pymupdf_layout").locate_file("pymupdf"); print(build_dependency_patch(root, "pipeline/native_build/dtox-directional-nn-row-block-v1", metadata.version("pymupdf_layout")))'
python -c 'import sys; from pathlib import Path; root=Path("pipeline/native_build/dtox-directional-nn-row-block-v1").resolve(); sys.path.insert(0,str(root)); from pipeline.pdf_memory import verify_native_build; print(verify_native_build(root))'
```

`locate_file("pymupdf")` resolves the shared package tree, including the main
bindings, layout code and bundled resources. The builder copies that tree into
the separate release artifact and replaces only the directional NN function.
It never edits installed site-packages. Unknown version/source fails closed.

Expected upstream `common_util.py` SHA256:
`f51244f60096f8b01181b330f14d02a5a1f9c9f6755a63e115d553cb80628680`.
Expected patched SHA256:
`a6ced24e14c96bb073d0fee6dc0af45b4893ec0eddb895e96ed4213a1bc6b5d9`.

Package the fork with `extractor.py`, `pdf_memory.py` and its build identity.
Before rollout, verify the entire copied Linux tree/bindings/model/config hashes
once against the accepted release manifest, allowing only the reviewed NN change
and its upstream sidecar. This whole-release gate belongs to deployment review;
the per-document guard checks the selected fork path, trio and exact NN source.
Neither check initializes native models. Do not ship the wrapper without its fork.

The parent retains its existing interpreter/dependencies. Only fresh native
children select the fixed relative fork path. The Linux child binds SIGKILL to
parent death with `prctl`, checking parent PID before and after registration,
before native imports. This protects the direct child, not arbitrary descendants
or graceful parent reap. Windows behavior is unchanged. Native jobs serialize;
each conversion timeout starts after obtaining its slot. No document features,
OCR defaults, pages or model selection are disabled.

Linux parent-death QA, with no PDF/model conversion and a <=5 s real harness:

```sh
python -m unittest pipeline.tests.test_pdf_parent_death -v
```

The real test is skipped on Windows. Accepted Linux-native/POSIX/OCR verification
is separate from Windows evidence; do not infer global OOM closure from this build.
