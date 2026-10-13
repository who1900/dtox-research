"""Build a review candidate by splicing only explicitly selected top-level functions."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


class PatchBuildError(ValueError):
    """Raised when the requested function-only splice is ambiguous or unsafe."""


def _line_starts(source):
    starts = [0]
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\r":
            index += 2 if index + 1 < len(source) and source[index + 1] == "\n" else 1
            starts.append(index)
        elif char == "\n":
            index += 1
            starts.append(index)
        else:
            index += 1
    return starts


def _line_offset(starts, line_number, source_length):
    if line_number <= 0:
        raise PatchBuildError("invalid AST line number")
    return starts[line_number - 1] if line_number <= len(starts) else source_length


def _top_level_functions(source, label):
    try:
        module = ast.parse(source, filename=label)
    except SyntaxError as error:
        raise PatchBuildError(f"{label} is not valid Python") from error
    functions = [node for node in module.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    by_name = {}
    for node in functions:
        if node.name in by_name:
            raise PatchBuildError(f"duplicate top-level function: {node.name}")
        by_name[node.name] = node
    return by_name


def _function_span(source, starts, node):
    first_line = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
    start = _line_offset(starts, first_line, len(source))
    end = _line_offset(starts, node.end_lineno + 1, len(source))
    return start, end


def _newline(source):
    match = re.search(r"\r\n|\n|\r", source)
    return match.group(0) if match else "\n"


def _normalize_snippet(snippet, newline):
    return snippet.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def _selected_source(source, starts, node, newline):
    start, end = _function_span(source, starts, node)
    snippet = _normalize_snippet(source[start:end], newline)
    return snippet.rstrip("\r\n")


def _validate_names(replacements, helpers):
    if not replacements:
        raise PatchBuildError("at least one existing function replacement is required")
    if len(set(replacements)) != len(replacements) or len(set(helpers)) != len(helpers):
        raise PatchBuildError("function names must not be repeated")
    if set(replacements) & set(helpers):
        raise PatchBuildError("a name cannot be both replaced and inserted")
    for name in replacements + helpers:
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise PatchBuildError("function names must be Python identifiers")


def build_candidate(baseline, current, replacements, helpers=(), marker=None,
                    expected_baseline_sha256=None):
    """Splice selected AST function source while preserving all other baseline text."""
    replacements, helpers = list(replacements), list(helpers)
    _validate_names(replacements, helpers)
    if expected_baseline_sha256 is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_baseline_sha256):
            raise PatchBuildError("expected baseline SHA-256 must contain 64 hex characters")
        actual_hash = hashlib.sha256(baseline.encode("utf-8")).hexdigest()
        if actual_hash.lower() != expected_baseline_sha256.lower():
            raise PatchBuildError("baseline SHA-256 does not match expected value")
    baseline_nodes = _top_level_functions(baseline, "baseline")
    current_nodes = _top_level_functions(current, "current")
    missing_baseline = [name for name in replacements if name not in baseline_nodes]
    if missing_baseline:
        raise PatchBuildError("replacement function absent from baseline")
    missing_current = [name for name in replacements + helpers if name not in current_nodes]
    if missing_current:
        raise PatchBuildError("selected function absent from current source")
    existing_helpers = [name for name in helpers if name in baseline_nodes]
    if existing_helpers:
        raise PatchBuildError("new helper already exists in baseline")

    baseline_starts = _line_starts(baseline)
    current_starts = _line_starts(current)
    newline = _newline(baseline)
    replace_spans = {}
    for name in replacements:
        replace_spans[name] = _function_span(baseline, baseline_starts, baseline_nodes[name])
    earliest = min(start for start, _ in replace_spans.values())

    if marker is None:
        insertion = earliest
    else:
        if not marker or "\n" in marker or "\r" in marker:
            raise PatchBuildError("insertion marker must be one non-empty source line")
        matches = [match.start() for match in re.finditer(re.escape(marker), baseline)]
        if len(matches) != 1:
            raise PatchBuildError("insertion marker must occur exactly once in baseline")
        insertion = matches[0]
        line_start = baseline.rfind("\n", 0, insertion) + 1
        line_end = baseline.find("\n", insertion)
        line_end = len(baseline) if line_end < 0 else line_end
        marker_line = baseline[line_start:line_end].rstrip("\r")
        if marker_line.strip() != marker or line_start != insertion:
            raise PatchBuildError("insertion marker must be an unindented whole line")
        if insertion > earliest:
            raise PatchBuildError("insertion marker must precede selected functions")
        for node in baseline_nodes.values():
            start, end = _function_span(baseline, baseline_starts, node)
            if start < insertion < end:
                raise PatchBuildError("insertion marker cannot be inside a function")

    operations = []
    for name in replacements:
        start, end = replace_spans[name]
        snippet = _selected_source(current, current_starts, current_nodes[name], newline) + newline
        operations.append((start, end, snippet, name))

    if helpers:
        helper_snippets = [_selected_source(current, current_starts, current_nodes[name], newline)
                           for name in helpers]
        helper_block = (newline * 2).join(helper_snippets) + newline * 2
        operations.append((insertion, insertion, helper_block, "<helpers>"))

    operations.sort(key=lambda item: (item[0], 0 if item[1] == item[0] else 1))
    output = []
    cursor = 0
    for start, end, snippet, _name in operations:
        if start < cursor or end < start:
            raise PatchBuildError("selected function spans overlap")
        output.append(baseline[cursor:start])
        output.append(snippet)
        cursor = end
    output.append(baseline[cursor:])
    candidate = "".join(output)

    candidate_nodes = _top_level_functions(candidate, "candidate")
    for name in replacements + helpers:
        if ast.dump(candidate_nodes[name], include_attributes=False) != \
                ast.dump(current_nodes[name], include_attributes=False):
            raise PatchBuildError("selected function AST changed during splice")
    return candidate


def _read_git_baseline(repo, baseline_ref, baseline_path):
    path = PurePosixPath(baseline_path.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PatchBuildError("baseline path must be repository-relative")
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", baseline_ref):
        raise PatchBuildError("baseline-ref must be a commit hash")
    verified = subprocess.run(
        ["git", "rev-parse", "--verify", f"{baseline_ref}^{{commit}}"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    content = subprocess.run(
        ["git", "show", f"{verified}:{path.as_posix()}"],
        cwd=repo, check=True, capture_output=True,
    ).stdout
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PatchBuildError("baseline source must be UTF-8") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    baseline_source = parser.add_mutually_exclusive_group(required=True)
    baseline_source.add_argument("--baseline-ref", help="Git commit hash for the baseline")
    baseline_source.add_argument("--baseline-file", type=Path,
                                 help="private baseline source file")
    parser.add_argument("--baseline-path", help="Git source path relative to the repository")
    parser.add_argument("--expected-baseline-sha256", required=True)
    parser.add_argument("--current", required=True, type=Path, help="current source file")
    parser.add_argument("--output", required=True, type=Path, help="new candidate path; must not exist")
    parser.add_argument("--replace-function", action="append", default=[], metavar="NAME")
    parser.add_argument("--new-helper", action="append", default=[], metavar="NAME")
    parser.add_argument("--marker", help="optional unique top-level baseline line before replacements")
    args = parser.parse_args(argv)
    try:
        repo = args.repo.resolve()
        current_path = args.current if args.current.is_absolute() else repo / args.current
        output_path = args.output if args.output.is_absolute() else repo / args.output
        current_path = current_path.resolve()
        output_path = output_path.resolve()
        if current_path == output_path or output_path.exists():
            raise PatchBuildError("output must be a new path distinct from current source")
        if args.baseline_ref:
            if not args.baseline_path:
                raise PatchBuildError("baseline-path is required with baseline-ref")
            baseline = _read_git_baseline(repo, args.baseline_ref, args.baseline_path)
        else:
            if args.baseline_path:
                raise PatchBuildError("baseline-path cannot be combined with baseline-file")
            baseline_path = args.baseline_file
            if not baseline_path.is_absolute():
                baseline_path = repo / baseline_path
            try:
                baseline = baseline_path.read_bytes().decode("utf-8")
            except UnicodeDecodeError as error:
                raise PatchBuildError("baseline source must be UTF-8") from error
        with current_path.open("r", encoding="utf-8", newline="") as source_file:
            current = source_file.read()
        candidate = build_candidate(baseline, current, args.replace_function,
                                    args.new_helper, args.marker,
                                    args.expected_baseline_sha256)
        with output_path.open("x", encoding="utf-8", newline="") as output_file:
            output_file.write(candidate)
    except (PatchBuildError, OSError, subprocess.SubprocessError, UnicodeDecodeError):
        print(json.dumps({"status": "error", "error": "candidate preparation failed"}))
        return 1
    print(json.dumps({
        "status": "prepared", "replaced_functions": args.replace_function,
        "inserted_helpers": args.new_helper,
        "candidate_sha256": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
