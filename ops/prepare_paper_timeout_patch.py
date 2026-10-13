import argparse
import ast
import difflib
import hashlib
from pathlib import Path


FUNCTIONS = ("_rank_papers", "find_papers", "paper_facets")


def _read_source(path):
    raw = Path(path).read_bytes()
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _function_spans(source, path):
    tree = ast.parse(source, filename=str(path))
    lines = source.split("\n")
    spans = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS:
            if node.name in spans:
                raise ValueError(f"Multiple definitions of {node.name} in {path}")
            start = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
            spans[node.name] = (start - 1, node.end_lineno,
                                "\n".join(lines[start - 1:node.end_lineno]))
    missing = set(FUNCTIONS) - spans.keys()
    if missing:
        raise ValueError(f"Missing functions in {path}: {', '.join(sorted(missing))}")
    return spans


def _outside_functions(source, path):
    lines = source.split("\n")
    spans = _function_spans(source, path)
    for name in sorted(FUNCTIONS, key=lambda item: spans[item][0], reverse=True):
        start, end, _ = spans[name]
        del lines[start:end]
    return lines


def build_candidate(baseline_path, current_path, expected_hash):
    baseline_raw = Path(baseline_path).read_bytes()
    actual_hash = hashlib.sha256(baseline_raw).hexdigest()
    if actual_hash.lower() != expected_hash.lower():
        raise ValueError(f"Baseline SHA-256 mismatch: expected {expected_hash}, got {actual_hash}")

    baseline = baseline_raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    current = _read_source(current_path)
    base_spans = _function_spans(baseline, baseline_path)
    current_spans = _function_spans(current, current_path)
    lines = baseline.split("\n")
    for name in sorted(FUNCTIONS, key=lambda item: base_spans[item][0], reverse=True):
        start, end, _ = base_spans[name]
        replacement = current_spans[name][2]
        lines[start:end] = replacement.split("\n")
    candidate = "\n".join(lines)
    candidate_spans = _function_spans(candidate, "candidate")
    for name in FUNCTIONS:
        if candidate_spans[name][2] != current_spans[name][2]:
            raise ValueError(f"Generated candidate function differs from current: {name}")
    if _outside_functions(baseline, baseline_path) != _outside_functions(candidate, "candidate"):
        raise ValueError("Generated candidate changes source outside the selected functions")
    return baseline, candidate, actual_hash


def main():
    parser = argparse.ArgumentParser(
        description="Build a narrow candidate and unified patch from three current API functions."
    )
    parser.add_argument("--baseline", required=True, help="Copied production main.py")
    parser.add_argument("--current", required=True, help="Local current api/main.py")
    parser.add_argument("--expected-hash", required=True, help="Expected raw baseline SHA-256")
    parser.add_argument("--candidate", required=True, help="Candidate output path")
    parser.add_argument("--patch", required=True, help="Unified diff output path")
    args = parser.parse_args()

    baseline, candidate, baseline_hash = build_candidate(
        args.baseline, args.current, args.expected_hash
    )
    candidate_path = Path(args.candidate)
    patch_path = Path(args.patch)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(candidate, encoding="utf-8", newline="\n")
    diff = difflib.unified_diff(
        baseline.split("\n"),
        candidate.split("\n"),
        fromfile=str(args.baseline),
        tofile=str(args.candidate),
    )
    patch_path.write_text("\n".join(diff), encoding="utf-8", newline="\n")
    candidate_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    print(f"baseline_sha256={baseline_hash}")
    print(f"candidate_sha256={candidate_hash}")
    print(f"candidate={candidate_path}")
    print(f"patch={patch_path}")


if __name__ == "__main__":
    main()
