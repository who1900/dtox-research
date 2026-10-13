"""Offline tests for strict AST-selected release candidate construction."""

import ast
import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ops.prepare_audit_fix_patch import PatchBuildError, build_candidate, main


class PrepareAuditFixPatchTests(unittest.TestCase):
    def setUp(self):
        self.baseline = (
            "# baseline header\n"
            "from typing import Optional\n"
            "BASE_CONSTANT = 'keep'\n"
            "\n"
            "def before():\n"
            "    return 'baseline-before'\n"
            "\n"
            "@mcp.tool()\n"
            "def replace(value: str) -> dict:\n"
            "    return {'old': value}\n"
            "\n"
            "def untouched():\n"
            "    return 'baseline-untouched'\n"
        )
        self.current = (
            "from typing import Optional\n"
            "CURRENT_CONSTANT = 'must-not-copy'\n"
            "\n"
            "def _new_helper(value: Optional[str] = None) -> str:\n"
            "    return value or 'helper'\n"
            "\n"
            "@mcp.tool()\n"
            "def replace(value: str) -> dict:\n"
            "    return {'new': value}\n"
            "\n"
            "def untouched():\n"
            "    return 'current-untouched'\n"
        )

    def test_splices_only_selected_top_level_functions_and_preserves_baseline_text(self):
        candidate = build_candidate(self.baseline, self.current,
                                    ["replace"], ["_new_helper"])
        self.assertTrue(candidate.startswith("# baseline header\nfrom typing import Optional\n"))
        self.assertIn("BASE_CONSTANT = 'keep'\n", candidate)
        self.assertNotIn("CURRENT_CONSTANT", candidate)
        self.assertIn("def untouched():\n    return 'baseline-untouched'\n", candidate)
        self.assertNotIn("current-untouched", candidate)
        self.assertLess(candidate.index("def _new_helper"), candidate.index("@mcp.tool()"))
        self.assertIn("@mcp.tool()\ndef replace(value: str) -> dict:", candidate)
        self.assertNotIn("\r", candidate)

        current_functions = ast.parse(self.current).body
        candidate_functions = ast.parse(candidate).body
        selected = {"_new_helper", "replace"}
        current_ast = {node.name: ast.dump(node, include_attributes=False)
                       for node in current_functions if isinstance(node, ast.FunctionDef)
                       and node.name in selected}
        candidate_ast = {node.name: ast.dump(node, include_attributes=False)
                         for node in candidate_functions if isinstance(node, ast.FunctionDef)
                         and node.name in selected}
        self.assertEqual(candidate_ast, current_ast)

    def test_preserves_baseline_crlf_and_normalizes_only_selected_snippets(self):
        baseline = self.baseline.replace("\n", "\r\n")
        candidate = build_candidate(baseline, self.current, ["replace"], ["_new_helper"])
        self.assertIn("# baseline header\r\nfrom typing import Optional\r\n", candidate)
        self.assertIn("def _new_helper(value: Optional[str] = None) -> str:\r\n", candidate)
        self.assertNotIn("\n", candidate.replace("\r\n", ""))

    def test_marker_insertion_is_unique_top_level_and_before_selected_definition(self):
        baseline = self.baseline.replace(
            "@mcp.tool()\n",
            "# AUDIT_INSERTION_POINT\n\n@mcp.tool()\n",
            1,
        )
        candidate = build_candidate(baseline, self.current, ["replace"],
                                    ["_new_helper"], marker="# AUDIT_INSERTION_POINT")
        self.assertLess(candidate.index("def _new_helper"),
                        candidate.index("# AUDIT_INSERTION_POINT"))
        with self.assertRaises(PatchBuildError):
            build_candidate(baseline + "# AUDIT_INSERTION_POINT\n", self.current,
                            ["replace"], ["_new_helper"], marker="# AUDIT_INSERTION_POINT")

    def test_duplicate_top_level_names_are_refused(self):
        duplicate_baseline = self.baseline + "\ndef replace(value):\n    return value\n"
        with self.assertRaisesRegex(PatchBuildError, "duplicate top-level"):
            build_candidate(duplicate_baseline, self.current, ["replace"], ["_new_helper"])

        duplicate_current = self.current + "\ndef _new_helper(value):\n    return value\n"
        with self.assertRaisesRegex(PatchBuildError, "duplicate top-level"):
            build_candidate(self.baseline, duplicate_current, ["replace"], ["_new_helper"])

    def test_nested_functions_do_not_satisfy_top_level_selection(self):
        current = self.current.replace(
            "def _new_helper(value: Optional[str] = None) -> str:\n"
            "    return value or 'helper'\n",
            "def container():\n"
            "    def _new_helper(value: Optional[str] = None) -> str:\n"
            "        return value or 'helper'\n"
            "    return _new_helper()\n",
        )
        with self.assertRaisesRegex(PatchBuildError, "absent from current"):
            build_candidate(self.baseline, current, ["replace"], ["_new_helper"])

    def test_new_helper_must_be_absent_from_baseline_and_names_must_be_unique(self):
        baseline = self.baseline + "\ndef _new_helper():\n    return None\n"
        with self.assertRaisesRegex(PatchBuildError, "already exists"):
            build_candidate(baseline, self.current, ["replace"], ["_new_helper"])
        with self.assertRaisesRegex(PatchBuildError, "must not be repeated"):
            build_candidate(self.baseline, self.current, ["replace", "replace"], [])

    def test_cli_writes_only_new_candidate_and_emits_no_source_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline_path = root / "main.before.py"
            current_path = root / "current.py"
            output_path = root / "candidate.py"
            baseline_path.write_text(self.baseline, encoding="utf-8", newline="")
            current_path.write_text(self.current, encoding="utf-8", newline="")
            expected_hash = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
            args = ["--repo", str(root), "--baseline-file", str(baseline_path),
                    "--expected-baseline-sha256", expected_hash, "--current", str(current_path),
                    "--output", str(output_path), "--replace-function", "replace",
                    "--new-helper", "_new_helper"]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(args), 0)
            self.assertTrue(output_path.is_file())
            self.assertIn("def _new_helper", output_path.read_text(encoding="utf-8"))
            self.assertNotIn(str(current_path), stdout.getvalue())
            self.assertNotIn("must-not-copy", output_path.read_text(encoding="utf-8"))

    def test_baseline_file_hash_mismatch_fails_without_candidate_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline_path = root / "baseline.py"
            current_path = root / "current.py"
            output_path = root / "candidate.py"
            baseline_path.write_text(self.baseline, encoding="utf-8", newline="")
            current_path.write_text(self.current, encoding="utf-8", newline="")
            args = ["--repo", str(root), "--baseline-file", str(baseline_path),
                    "--expected-baseline-sha256", "0" * 64, "--current", str(current_path),
                    "--output", str(output_path), "--replace-function", "replace",
                    "--new-helper", "_new_helper"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 1)
            self.assertFalse(output_path.exists())

    def test_baseline_file_and_git_path_modes_cannot_be_mixed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline_path = root / "baseline.py"
            current_path = root / "current.py"
            output_path = root / "candidate.py"
            baseline_path.write_text(self.baseline, encoding="utf-8", newline="")
            current_path.write_text(self.current, encoding="utf-8", newline="")
            args = ["--repo", str(root), "--baseline-file", str(baseline_path),
                    "--baseline-path", "mcp/server.py", "--expected-baseline-sha256", "0" * 64,
                    "--current", str(current_path), "--output", str(output_path),
                    "--replace-function", "replace"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 1)
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
