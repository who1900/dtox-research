import argparse
import hashlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _load_candidate(api_dir, candidate_path):
    package = types.ModuleType("api")
    package.__path__ = [str(api_dir)]
    package.__package__ = "api"
    sys.modules["api"] = package

    spec = importlib.util.spec_from_file_location("api.main", candidate_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load candidate module: {candidate_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["api.main"] = module
    package.main = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(
        description="Run a unittest module against candidate api.main and production API modules."
    )
    parser.add_argument("--api-dir", required=True, help="Directory containing production API modules")
    parser.add_argument("--candidate", required=True, help="Candidate api.main source file")
    parser.add_argument("--tests", required=True, help="Dedicated unittest module to execute")
    parser.add_argument("--expected-hash", help="Optional expected candidate SHA-256")
    args = parser.parse_args()

    api_dir = Path(args.api_dir).resolve(strict=True)
    candidate_path = Path(args.candidate).resolve(strict=True)
    tests_path = Path(args.tests).resolve(strict=True)
    candidate_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    if args.expected_hash and candidate_hash.lower() != args.expected_hash.lower():
        parser.error(f"candidate SHA-256 mismatch: expected {args.expected_hash}, got {candidate_hash}")

    _load_candidate(api_dir, candidate_path)
    test_spec = importlib.util.spec_from_file_location("paper_timeout_candidate_tests", tests_path)
    if test_spec is None or test_spec.loader is None:
        parser.error(f"cannot load test module: {tests_path}")
    test_module = importlib.util.module_from_spec(test_spec)
    test_spec.loader.exec_module(test_module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_module)
    count = suite.countTestCases()
    if count == 0:
        parser.error(f"no unittest.TestCase tests discovered in {tests_path}")

    print(f"candidate_sha256={candidate_hash}")
    print(f"discovered_tests={count}")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
