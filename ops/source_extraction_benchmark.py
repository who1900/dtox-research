"""Offline, sealed, whole-document extraction experiment; never imports production code."""
import argparse
from collections import Counter, defaultdict
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import stat
import subprocess
import sys
import time
import unicodedata


SCHEMA = "dtox-source-extraction-benchmark-1"
ENGINES = ("pymupdf", "docling")
CLASSES = ("encoding", "layout", "math", "tables", "scans", "source", "healthy")
CHECKS = ("snippet", "ordered_anchors", "formula", "table_cell", "section")
QUALITY_CHECKS = ("snippet", "ordered_anchors")
CLASS_CHECK = dict.fromkeys(CLASSES, "snippet")
CLASS_CHECK["layout"] = "ordered_anchors"
SEMANTIC_CHECK = dict(math="formula", tables="table_cell", source="section")
NOT_MEASURED = "NOT_MEASURED"
UNKNOWN = "INCONCLUSIVE_NOT_MEASURED"
OUTPUT_READ_LIMITS = {"json_bytes": 64 * 1024 * 1024, "log_bytes": 16 * 1024 * 1024,
                      "artifact_bytes": 64 * 1024 * 1024, "max_entries": 4096}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "duplicate JSON key: " + key)
        result[key] = value
    return result


def bounded_bytes(path, limit):
    path = local(path)
    before = path.lstat()
    need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "nonregular/multiply-linked output forbidden")
    need(not getattr(before, "st_file_attributes", 0) & 0x400, "reparse output forbidden")
    need(before.st_size <= limit, "output exceeds declared read bound")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) |
                 getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        need(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1 and opened.st_size <= limit and
             (before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino), "output changed before safe read")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        need(len(data) <= limit and (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) ==
             (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "output changed/exceeded bound during safe read")
    return data


def read_json(path, limit=OUTPUT_READ_LIMITS["json_bytes"]):
    return json.loads(bounded_bytes(path, limit), object_pairs_hook=unique_keys,
                      parse_constant=lambda value: need(False, "non-finite JSON: " + value))


def local(path):
    need(isinstance(path, (str, Path)) and str(path), "local path required")
    raw = str(path)
    need(not raw.startswith(("//", "\\\\")) and "://" not in raw, "network path forbidden")
    path = Path(path).absolute()
    for part in (path, *path.parents):
        need(not part.is_symlink() and not (hasattr(part, "is_junction") and part.is_junction()),
             "linked path forbidden: " + raw)
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        need(not getattr(info, "st_file_attributes", 0) & 0x400, "reparse path forbidden: " + raw)
    return Path(os.path.abspath(path))


def file_sha(path):
    path = local(path)
    before = path.stat()
    need(path.is_file(), "not a regular file")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    after = path.stat()
    need((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
         (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
         "file changed while hashing")
    return hasher.hexdigest()


def pin_sha(pin):
    path = local(pin["path"])
    if pin.get("kind", "file") == "file":
        return file_sha(path)
    need(pin["kind"] == "tree" and path.is_dir(), "invalid tree pin")
    entries = []
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            entries.append([child.relative_to(path).as_posix(), "symlink", os.readlink(child)])
        elif child.is_dir():
            entries.append([child.relative_to(path).as_posix(), "directory"])
        else:
            entries.append([child.relative_to(path).as_posix(), file_sha(child)])
    return sha(encoded(entries))


def verify_pin(pin):
    need(digest(pin.get("sha256")), "pin requires SHA-256")
    need(pin_sha(pin) == pin["sha256"], "pin mismatch: " + pin["path"])


def contained(path, root):
    return path == root or root in path.parents


def create_output(path, forbidden=()):
    path = local(path)
    for item in forbidden:
        other = local(item)
        need(not contained(path, other) and not contained(other, path), "output overlaps protected input")
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    (path / "LOCK").write_text("exclusive experiment directory; never resume/retry in place\n", encoding="utf-8")
    return path


def write_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(encoded(value))


def evidence_field(value):
    need(isinstance(value, dict) and value.get("status") in
         ("established", "missing", "unestablished"), "explicit evidence state required")
    need("value" in value, "evidence value required, including null")
    if value["status"] == "established":
        need(value["value"] is not None and value["value"] != "", "established evidence is empty")


def environment_snapshot(packages):
    versions = {}
    for name in packages:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else None
    return {"architecture": platform.machine(), "cpu_count": os.cpu_count(), "ram_bytes": ram,
            "python_executable": str(Path(sys.executable).absolute()), "python_version": platform.python_version(),
            "venv": sys.prefix, "packages": versions}


def validate_config(config):
    need(config.get("schema") == SCHEMA, "config schema mismatch")
    need(config.get("job_runtime") == "infinity" and config.get("automatic_retry") is False,
         "job must be unlimited and without automatic retry")
    need(set(config["engines"]) == set(ENGINES), "only current PyMuPDF versus Docling")
    baseline = config["baseline_contract"]
    need(baseline.get("comparison_kind") in ("current_source_equivalent", "exact_deployed_build")
         and bool(baseline.get("description")) and bool(baseline.get("source_pins")), "explicit current baseline provenance required")
    need(baseline.get("runtime_monkeypatch") is False, "baseline must not introduce runtime monkeypatches")
    timeouts = baseline["internal_timeouts_seconds"]
    need(all(k in timeouts and (timeouts[k] is None or type(timeouts[k]) in (int, float) and
                               math.isfinite(timeouts[k]) and timeouts[k] > 0) for k in ("arxiv_pdf", "other_pdf")),
         "explicit existing baseline timeouts (or null) required")
    environment = config["environment"]
    need(set(environment) == {"architecture", "cpu_count", "ram_bytes", "python_executable", "python_version", "venv", "packages"},
         "record actual environment fields from environment subcommand")
    need(isinstance(environment["architecture"], str) and environment["architecture"] and
         type(environment["cpu_count"]) is int and environment["cpu_count"] > 0, "invalid expected hardware")
    need(environment["ram_bytes"] is None or type(environment["ram_bytes"]) is int and environment["ram_bytes"] > 0, "invalid expected RAM")
    need(bool(environment["packages"]) and all(isinstance(v, str) and v for v in environment["packages"].values()), "explicit package versions required")
    sandbox = config["sandbox"]
    identity = sandbox.get("execution_identity", {"mode": "unprivileged"})
    need(identity == {"mode": "unprivileged"} or
         identity == {"mode": "root_setup_fixed_uid", "uid": 65534, "gid": 65534}, "unsupported execution identity")
    if fixed_identity(config):
        helper = sandbox.get("setpriv", {})
        need(helper.get("path") == "/usr/bin/setpriv" and digest(helper.get("sha256")),
             "root setup requires pinned /usr/bin/setpriv")
    bounds = sandbox.get("output_read_limits", OUTPUT_READ_LIMITS)
    need(set(bounds) == set(OUTPUT_READ_LIMITS) and all(type(v) is int and v > 0 for v in bounds.values()),
         "positive bounded output read limits required")
    need(PurePosixPath(sandbox["launcher"]["path"]).name == "bwrap", "bubblewrap launcher required")
    need(bool(sandbox["readonly"]), "explicit runtime mounts required")
    need(bool(sandbox.get("forbidden_roots")), "declare production/runtime/storage roots excluded from sandbox and outputs")
    for mount in sandbox["os_readonly"]:
        path = PurePosixPath(mount["path"])
        need(any(path == root or root in path.parents for root in map(PurePosixPath,
             ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc/ld.so.cache", "/etc/fonts", "/etc/ssl/certs"))),
             "OS runtime mount outside standard OS roots")
        need(digest(mount["identity_receipt"].get("sha256")), "OS metadata/package receipt pin required")
    for pin in [sandbox["launcher"], *sandbox["readonly"]]:
        need(digest(pin.get("sha256")) and PurePosixPath(pin["path"]).is_absolute(), "absolute runtime pin required")
        need(PurePosixPath(pin["path"]) != PurePosixPath("/") and
             str(local(pin["path"])) != str(Path.home()), "broad mount forbidden")
    for name, engine in config["engines"].items():
        argv = engine["argv"]
        need(isinstance(argv, list) and all(isinstance(a, str) and a for a in argv), "argv list required")
        need(PurePosixPath(argv[0]).is_absolute() and PurePosixPath(argv[0]).name.lower() not in
             ("sh", "bash", "dash", "zsh", "cmd", "cmd.exe", "powershell.exe", "pwsh", "env"),
             "no shell runner")
        need(all(argv.count(token) == 1 for token in ("{input}", "{output}", "{config}", "{request}")),
             "runner needs exactly one input, output, config and public request argument")
        need(all("{" not in a and "}" not in a for a in argv if a not in
                 ("{input}", "{output}", "{config}", "{request}")), "unknown argv placeholder")
        need(engine.get("whole_document") is True and bool(engine.get("description")), "whole-document attestation required")
        need(engine.get("output_scope") in ("pages", "document"), "predeclare output locator scope")
        need(engine.get("role") == ("exact_current_baseline" if name == "pymupdf" else "candidate"), "engine role mismatch")
        need(digest(engine.get("dependency_manifest_sha256")), "dependency receipt pin required")
        need(bool(engine.get("pins")) and digest(engine["config"].get("sha256")), "runner/config pins required")
    for pin in baseline["source_pins"]:
        need(digest(pin.get("sha256")) and pin in config["engines"]["pymupdf"]["pins"], "baseline source pin absent from verified runner artifacts")
    limits = config["document_limits"]
    need(bool(limits.get("label")), "explicit limit label required")
    need(set(limits) == {"label", "wall_seconds", "cpu_seconds", "address_space_bytes"}, "unknown or missing limit")
    for key in ("wall_seconds", "cpu_seconds", "address_space_bytes"):
        value = limits[key]
        need(value is None or (type(value) in (int, float) and math.isfinite(value) and value > 0), "invalid explicit limit")
        if key != "wall_seconds" and value is not None:
            need(type(value) is int, "CPU/address-space limits must be integers")
    gates = config["gates"]
    gate_fields = {"expected_physical_documents", "min_documents_per_class_split",
                        "min_reviewed_checks_per_document", "min_observed_check_ratio",
                        "max_healthy_regressions", "max_class_regressions", "min_candidate_check_gain"}
    need(gate_fields <= set(gates) <= gate_fields | {"required_classes", "min_semantic_checks_per_class_split"},
         "gate fields must be predeclared")
    required = gates.get("required_classes", list(CLASSES))
    need(isinstance(required, list) and required and all(isinstance(c, str) for c in required) and
         len(required) == len(set(required)) and set(required) <= set(CLASSES), "invalid frozen required_classes")
    semantic_counts = gates.get("min_semantic_checks_per_class_split", {})
    need(isinstance(semantic_counts, dict) and set(semantic_counts) <= set(SEMANTIC_CHECK) and
         all(type(v) is int and v > 0 for v in semantic_counts.values()), "invalid predeclared semantic coverage counts")
    for key in ("expected_physical_documents", "min_documents_per_class_split", "min_reviewed_checks_per_document"):
        need(type(gates[key]) is int and gates[key] > 0, "positive gate count required")
    need(type(gates["max_healthy_regressions"]) is int and gates["max_healthy_regressions"] >= 0, "invalid regression gate")
    need(type(gates["max_class_regressions"]) is int and gates["max_class_regressions"] >= 0, "invalid class regression gate")
    need(type(gates["min_candidate_check_gain"]) is int and gates["min_candidate_check_gain"] > 0, "positive candidate gain required")
    thresholds = gates["min_observed_check_ratio"]
    need("all" in thresholds and set(thresholds) <= {"all", *CLASSES}, "candidate ratio thresholds keyed by class; all fallback required")
    for value in thresholds.values():
        need(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1, "invalid ratio gate")


def validate_source_image(ref, original_sha256, page_count=None):
    need(ref.get("type") in SEMANTIC_CHECK.values(), "source_image is semantic-only")
    image = ref.get("expected_image", {})
    need(isinstance(image, dict) and isinstance(image.get("path"), str) and
         Path(image["path"]).is_absolute() and digest(image.get("sha256")), "absolute gold image path and SHA required")
    local(image["path"])
    need(digest(original_sha256) and ref.get("original_sha256") == original_sha256,
         "gold original SHA mismatch")
    need(type(ref.get("page")) is int and ref["page"] > 0 and
         (page_count is None or ref["page"] <= page_count), "gold physical page outside source")
    need(isinstance(ref.get("source_locator"), str) and ref["source_locator"].strip(), "gold source_locator required")
    need(isinstance(ref.get("expected"), str) and ref["expected"].strip(), "descriptive gold expectation required")
    review = ref.get("review", {})
    need(review.get("original_sha256") == original_sha256 and review.get("image_sha256") == image["sha256"],
         "gold source review binding mismatch")


def gold_image_pins(manifest):
    return [ref["expected_image"] for doc in manifest["documents"] for ref in doc["references"]
            if ref.get("expected_kind") == "source_image"]


def validate_manifest(manifest):
    need(manifest.get("schema") == SCHEMA and bool(manifest.get("documents")), "manifest schema/documents required")
    sampling = manifest["sampling"]
    need(sampling.get("selected_before_extraction") is True and sampling.get("success_independent") is True
         and bool(sampling.get("method")) and digest(sampling.get("inventory_sha256")), "pre-extraction sampling provenance required")
    seen = {}
    for doc in manifest["documents"]:
        identity = doc["document_id"]
        need(isinstance(identity, str) and identity.strip(), "document ID required")
        if identity in seen:
            need(encoded(doc) == encoded(seen[identity]), "conflicting duplicate document ID: " + identity)
        seen[identity] = doc
        need(isinstance(doc["classes"], list) and bool(doc["classes"]) and set(doc["classes"]) <= set(CLASSES), "invalid classes")
        for key in ("source_uri", "source_version", "page_count", "real_document"):
            evidence_field(doc[key])
        for key in ("source_uri", "source_version"):
            if doc[key]["status"] == "established":
                need(isinstance(doc[key]["value"], str) and doc[key]["value"].strip(), "source URI/version must be strings")
        original = doc["original"]
        need(original.get("status") in ("available", "missing", "unreadable", "unestablished"), "original status required")
        need("path" in original and "sha256" in original, "original path/hash must be explicit, including null")
        need(original["sha256"] is None or digest(original["sha256"]), "invalid original SHA")
        if original["path"] is not None:
            need(Path(original["path"]).is_absolute(), "absolute original path required")
        if original["status"] == "available":
            need(original["path"] is not None and digest(original["sha256"]), "available original requires path and SHA")
        if doc["page_count"]["status"] == "established":
            need(type(doc["page_count"]["value"]) is int and doc["page_count"]["value"] > 0, "invalid page count")
        need(doc.get("source_kind") in ("arxiv_pdf", "other_pdf"), "explicit baseline timeout/source kind required")
        need(isinstance(doc["exceptions"], list) and all(isinstance(x, str) and x.strip() for x in doc["exceptions"]), "explicit exceptions list required")
        refs = doc["references"]
        need(isinstance(refs, list) and len({r["check_id"] for r in refs}) == len(refs), "duplicate reference ID")
        for ref in refs:
            need(ref["type"] in CHECKS and ref.get("normalization") in ("exact", "nfc_whitespace"), "unsupported reference check")
            need(type(ref.get("page")) is int and ref["page"] > 0 and bool(ref.get("locator")), "page-grounded locator required")
            need(ref.get("review", {}).get("status") in ("verified", "unreviewed", "missing"), "explicit review status required")
            need(ref.get("content_scope") in (None, "body", "title", "other"), "invalid reference content_scope")
            expected = ref["expected"]
            if ref["type"] == "ordered_anchors":
                need(isinstance(expected, list) and len(expected) >= 2 and all(isinstance(x, str) and x.strip() for x in expected), "ordered anchors required")
            else:
                need(isinstance(expected, str) and bool(expected.strip()), "nonempty expected string required")
            need(ref.get("expected_kind") in (None, "source_image"), "unsupported expected_kind")
            if ref.get("expected_kind") == "source_image":
                validate_source_image(ref, original["sha256"], doc["page_count"]["value"]
                                      if doc["page_count"]["status"] == "established" else None)
            elif ref["type"] == "table_cell":
                need(isinstance(ref.get("table_id"), str) and all(type(ref.get(k)) is int and ref[k] >= 0 for k in ("row", "column")), "table coordinates required")
            if ref["type"] == "section" and ref.get("expected_kind") != "source_image":
                need(isinstance(ref.get("title"), str) and bool(ref["title"].strip()), "section title required")
    return seen


def input_state(doc):
    original = doc["original"]
    if original["status"] != "available":
        return {"status": "INPUT_" + original["status"].upper(), "error": "declared by frozen manifest"}
    try:
        actual = file_sha(original["path"])
        return {"status": "READY" if actual == original["sha256"] else "INPUT_SHA_MISMATCH", "observed_sha256": actual}
    except (OSError, ValueError) as exc:
        return {"status": "INPUT_UNREADABLE", "error": f"{type(exc).__name__}: {exc}"}


def stratified_assignments(docs, groups):
    strata = defaultdict(list)
    for key, identities in groups.items():
        stratum = "control" if any("healthy" in docs[identity]["classes"] for identity in identities) else "challenge"
        strata[stratum].append(key)
    assignments = {}
    for stratum in sorted(strata):
        ordered = sorted(strata[stratum], key=lambda key: (sha(min(groups[key]).encode()), min(groups[key])))
        boundary = len(ordered) * 4 // 5
        for index, key in enumerate(ordered):
            for identity in groups[key]:
                assignments[identity] = {"physical_id": sha(key.encode()), "representative": min(groups[key]),
                                         "split": "development" if index < boundary else "holdout"}
    return assignments


def freeze(manifest, config, protocol_paths=()):
    docs = validate_manifest(manifest)
    validate_config(config)
    for pin in gold_image_pins(manifest):
        need(file_sha(pin["path"]) == pin["sha256"], "gold image SHA mismatch at seal")
    states = {identity: input_state(doc) for identity, doc in docs.items()}
    groups = defaultdict(list)
    for identity, doc in docs.items():
        # Actual readable bytes deduplicate even incorrectly declared/missing identity.
        key = states[identity].get("observed_sha256") or doc["original"]["sha256"] or "unknown:" + identity
        groups[key].append(identity)
    assignments = stratified_assignments(docs, groups)
    counts = {"manifest_rows": len(manifest["documents"]), "unique_document_ids": len(docs),
              "duplicate_id_rows": len(manifest["documents"]) - len(docs),
              "physical_documents": len(groups), "physical_alias_ids": len(docs) - len(groups),
              "physical_split_counts": dict(Counter(assignments[min(groups[k])]["split"] for k in groups)),
              "document_split_counts": dict(Counter(a["split"] for a in assignments.values()))}
    payload = {"schema": SCHEMA, "manifest": manifest, "config": config, "input_states": states,
               "assignments": assignments, "counts": counts, "harness_sha256": file_sha(__file__),
               "protocol_paths": [str(local(p)) for p in protocol_paths],
               "split_policy": "physical groups stratified by any healthy alias (control), otherwise challenge; within each stratum rank SHA256(min document ID), document ID tie-break; floor(4*N_stratum/5) development, remainder holdout; aliases inseparable"}
    return {"payload": json.loads(encoded(payload)), "sha256": sha(encoded(payload))}


def load_seal(path, expected_sha):
    seal = read_json(local(path))
    need(digest(expected_sha) and seal["sha256"] == expected_sha == sha(encoded(seal["payload"])), "seal SHA mismatch")
    payload = seal["payload"]
    need(payload["harness_sha256"] == file_sha(__file__), "harness changed; new protocol required")
    validate_manifest(payload["manifest"])
    validate_config(payload["config"])
    return payload


def execution_preflight(payload, seal_path, output):
    need(sys.platform.startswith("linux"), "execution requires Linux bubblewrap; Windows supports seal/grade/tests only")
    config = payload["config"]
    check_collector_identity(config)
    for pin in gold_image_pins(payload["manifest"]):
        need(file_sha(pin["path"]) == pin["sha256"], "gold image changed after seal")
    mounts = config["sandbox"]["readonly"]
    os_mounts = config["sandbox"]["os_readonly"]
    roots = [local(pin["path"]) for pin in mounts] + [Path(m["path"]).absolute() for m in os_mounts]
    forbidden = [local(p) for p in config["sandbox"]["forbidden_roots"]]
    need(not any(contained(local(output), p) or contained(p, local(output)) for p in forbidden), "output overlaps production root")
    protected = [local(seal_path), local(output), *(local(p) for p in payload["protocol_paths"]),
                 *(local(p["path"]) for p in gold_image_pins(payload["manifest"]))] + [local(d["original"]["path"]) for d in
                 payload["manifest"]["documents"] if d["original"]["path"] is not None]
    for root in roots:
        need(not any(contained(p, root) or contained(root, p) or contained(p, root.resolve()) or contained(root.resolve(), p)
                     for p in [*protected, *forbidden]), "runtime mount exposes experiment/source/production data")
        need(str(root) not in ("/proc", "/dev", "/tmp", "/output", "/input"), "reserved mount")
    pins = [config["sandbox"]["launcher"], *mounts]
    if fixed_identity(config):
        helper = config["sandbox"]["setpriv"]
        need(any(contained(local(helper["path"]), root) for root in roots), "setpriv outside readonly runtime mounts")
        pins.append(helper)
    for engine in config["engines"].values():
        pins.extend([engine["config"], *engine["pins"]])
        for path in [engine["argv"][0], engine["config"]["path"], *(pin["path"] for pin in engine["pins"])]:
            need(any(contained(Path(path).absolute(), root) for root in roots) and
                 any(contained(Path(path).resolve(), root) for root in roots), "runner/config outside sealed runtime mounts")
    for pin in pins:
        verify_pin(pin)
    if fixed_identity(config):
        for engine in config["engines"].values():
            need(read_json(engine["config"]["path"]).get("execution_identity_uid") == 65534,
                 "root setup requires backend execution_identity_uid=65534")
    for mount in os_mounts:
        verify_pin(mount["identity_receipt"])
        receipt = read_json(mount["identity_receipt"]["path"])
        need(receipt.get("schema") == "dtox-os-runtime-identity-1" and mount["path"] in receipt["mounts"]
             and receipt["architecture"] == config["environment"]["architecture"] and receipt["identity_files"], "invalid OS identity receipt")
        for pin in receipt["identity_files"]:
            need(not any(contained(local(pin["path"]), p) for p in forbidden), "OS identity file is production data")
            verify_pin(pin)
    actual = environment_snapshot(config["environment"]["packages"])
    need(actual == config["environment"], "actual environment differs from sealed config")
    return actual


def synthetic_mount_parents(config):
    mounts = {PurePosixPath(pin["path"]) for pin in
              [*config["sandbox"]["os_readonly"], *config["sandbox"]["readonly"]]}
    parents = {parent for mount in mounts for parent in mount.parents
               if parent != PurePosixPath("/") and
               not any(parent == root or root in parent.parents for root in mounts)}
    return sorted(parents, key=lambda path: (len(path.parts), str(path)))


def sandbox_argv(config, name, original, output):
    engine = config["engines"][name]
    namespaces = (["--unshare-ipc", "--unshare-pid", "--unshare-net", "--unshare-uts", "--unshare-cgroup"]
                  if fixed_identity(config) else ["--unshare-all"])
    argv = [config["sandbox"]["launcher"]["path"], *namespaces, "--die-with-parent", "--new-session",
            "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/output/home",
            "--setenv", "TMPDIR", "/output/tmp", "--setenv", "PYTHONDONTWRITEBYTECODE", "1"]
    argv.extend(["--cap-drop", "ALL"])
    if fixed_identity(config):
        for capability in ("CAP_SETUID", "CAP_SETGID", "CAP_SETPCAP"):
            argv.extend(["--cap-add", capability])
        for parent in synthetic_mount_parents(config):
            argv.extend(["--perms", "0755", "--dir", str(parent)])
    for pin in [*config["sandbox"]["os_readonly"], *config["sandbox"]["readonly"]]:
        argv.extend(["--ro-bind", pin["path"], pin["path"]])
    argv.extend(["--proc", "/proc", "--dev", "/dev"])
    if fixed_identity(config):
        argv.extend(["--perms", "1777", "--tmpfs", "/tmp", "--perms", "0755", "--dir", "/input"])
    else:
        argv.extend(["--tmpfs", "/tmp", "--dir", "/input"])
    argv.extend([
                 "--ro-bind", str(original), "/input/original", "--bind", str(output), "/output",
                 "--chdir", "/" if fixed_identity(config) else "/output", "--"])
    if fixed_identity(config):
        argv.extend([config["sandbox"]["setpriv"]["path"], "--reuid=65534", "--regid=65534",
                     "--clear-groups", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
                     "--no-new-privs", "--"])
    substitutions = {"{input}": "/input/original", "{output}": "/output/extraction.json",
                     "{config}": engine["config"]["path"], "{request}": "/output/request.json"}
    return argv + [substitutions.get(a, a) for a in engine["argv"]]


def fixed_identity(config):
    return config["sandbox"].get("execution_identity", {}).get("mode") == "root_setup_fixed_uid"


def check_collector_identity(config):
    euid = getattr(os, "geteuid", lambda: None)()
    need(euid == 0 if fixed_identity(config) else euid != 0,
         "collector must be root only for declared root_setup_fixed_uid; parser never root")


def private_root_directory(path):
    info = local(path).stat()
    need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
         "root collector output parent must be root-owned/private 0700")


def output_inventory(directory, bounds):
    directory = local(directory)
    count = 0
    pending = [directory]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                need(count <= bounds["max_entries"], "output inventory exceeds declared bound")
                path = local(entry.path)
                info = path.lstat()
                need(not getattr(info, "st_file_attributes", 0) & 0x400, "reparse output forbidden")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    continue
                need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "nonregular/multiply-linked output forbidden")
                key = "log_bytes" if entry.name in ("stdout.log", "stderr.log") else "json_bytes" if entry.name.endswith(".json") else "artifact_bytes"
                need(info.st_size <= bounds[key], "output exceeds declared read bound")


def safe_output_sha(path, limit):
    return sha(bounded_bytes(path, limit))


def run_command(argv, directory, limits):
    """One launch. wait4 metrics are child rusage, not total concurrent tree RSS."""
    import resource

    def apply_limits():
        if limits["cpu_seconds"] is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (limits["cpu_seconds"], limits["cpu_seconds"]))
        if limits["address_space_bytes"] is not None:
            resource.setrlimit(resource.RLIMIT_AS, (limits["address_space_bytes"], limits["address_space_bytes"]))

    started = time.monotonic()
    result = {"elapsed_seconds": None, "cpu_seconds": None, "peak_rss_bytes": None,
              "metrics_scope": "Linux wait4 launcher/descendant-accounted child rusage; peak RSS is not concurrent tree sum",
              "limits": limits, "exit_code": None, "status": "LAUNCH_ERROR", "error": None}
    process = None
    try:
        with (directory / "stdout.log").open("xb") as stdout, (directory / "stderr.log").open("xb") as stderr:
            process = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       cwd=directory, env={"PATH": "/usr/bin:/bin"}, close_fds=True,
                                       start_new_session=True, preexec_fn=apply_limits)
            exceeded = False
            while True:
                pid, status, usage = os.wait4(process.pid, os.WNOHANG)
                if pid:
                    process.returncode = os.waitstatus_to_exitcode(status)
                    result.update(exit_code=process.returncode, cpu_seconds=usage.ru_utime + usage.ru_stime,
                                  peak_rss_bytes=usage.ru_maxrss * 1024,
                                  status="DECLARED_WALL_LIMIT" if exceeded else "OK" if process.returncode == 0 else "PROCESS_ERROR")
                    if process.returncode == -signal.SIGKILL and not exceeded:
                        result["error"] = "SIGKILL: cause unknown (not automatically classified as OOM/CPU limit)"
                    break
                if not exceeded and limits["wall_seconds"] is not None and time.monotonic() - started >= limits["wall_seconds"]:
                    exceeded = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                time.sleep(0.05)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.returncode is None:
                process.wait()
        result["elapsed_seconds"] = time.monotonic() - started
    return result


def collect(payload, seal_sha, output, executor=run_command, observed_environment=None, preflight_costs=None):
    docs = validate_manifest(payload["manifest"])
    config = payload["config"]
    check_collector_identity(config)
    privileged_setup = fixed_identity(config)
    bounds = config["sandbox"].get("output_read_limits", OUTPUT_READ_LIMITS)
    if privileged_setup:
        private_root_directory(output)
    index = {"schema": SCHEMA, "seal_sha256": seal_sha, "counts": payload["counts"], "runs": [],
             "observed_environment": observed_environment,
             "preflight_costs": preflight_costs,
             "execution_identity": config["sandbox"].get("execution_identity", {"mode": "unprivileged"}),
             "output_read_limits": bounds,
             "job_runtime": "infinity", "automatic_retry": False, "complete": False}
    representatives = sorted({a["representative"] for a in payload["assignments"].values()}, key=lambda x: (sha(x.encode()), x))
    for position, identity in enumerate(representatives):
        doc = docs[identity]
        physical_id = payload["assignments"][identity]["physical_id"]
        order = ENGINES if position % 2 == 0 else ENGINES[::-1]
        for name in order:
            directory = output / physical_id / name
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            (directory / "home").mkdir(mode=0o700)
            (directory / "tmp").mkdir(mode=0o700)
            result = {"document_id": identity, "physical_id": physical_id, "engine": name,
                      "order": list(order), "position": position, "status": "NOT_RUN", "error": None,
                      "elapsed_seconds": None, "cpu_seconds": None, "peak_rss_bytes": None,
                      "limits": config["document_limits"], "output_sha256": None}
            request = {"document_id": identity, "original_sha256": doc["original"]["sha256"],
                       "source_uri": doc["source_uri"], "source_version": doc["source_version"],
                       "source_kind": doc["source_kind"], "whole_document": True,
                       "baseline_internal_timeout_seconds": config["baseline_contract"]["internal_timeouts_seconds"][doc["source_kind"]]}
            write_json(directory / "request.json", request)
            if privileged_setup:
                os.chmod(directory / "request.json", 0o444, follow_symlinks=False)
            result["request"] = request
            before = input_state(doc)
            result["input_before"] = before
            if payload["input_states"][identity]["status"] != "READY":
                result.update(status="FROZEN_INPUT_UNAVAILABLE", error=payload["input_states"][identity])
            elif before["status"] != "READY":
                result.update(status="INPUT_CHANGED_OR_UNREADABLE", error=before)
            else:
                argv = sandbox_argv(config, name, doc["original"]["path"], directory)
                result["argv"] = argv
                try:
                    if privileged_setup:
                        for fresh in (directory / "home", directory / "tmp", directory):
                            os.chown(fresh, 65534, 65534)
                    result.update(executor(argv, directory, config["document_limits"]))
                except Exception as exc:
                    result.update(status="RUNNER_EXCEPTION", error=f"{type(exc).__name__}: {exc}")
                finally:
                    if privileged_setup:
                        # Reclaim only this freshly created bind root, never a
                        # recursive chown or a parser-supplied path.
                        os.chown(directory, 0, 0, follow_symlinks=False)
                        os.chmod(directory, 0o700, follow_symlinks=False)
                result["input_after"] = input_state(doc)
                if result["input_after"] != before:
                    result.update(status="ORIGINAL_CHANGED", error="input hash/state changed; experiment invalid")
            result["logs"] = {p: None for p in ("stdout.log", "stderr.log")}
            try:
                output_inventory(directory, bounds)
                path = directory / "extraction.json"
                if path.exists():
                    result["output_sha256"] = safe_output_sha(path, bounds["json_bytes"])
                elif result["status"] == "OK":
                    result.update(status="OUTPUT_MISSING", error="runner exited without extraction.json")
                for log_name in result["logs"]:
                    if (directory / log_name).exists():
                        result["logs"][log_name] = safe_output_sha(directory / log_name, bounds["log_bytes"])
            except (OSError, ValueError) as exc:
                result.update(status="INVALID_OUTPUT", error=f"unsafe/unbounded parser output: {exc}", output_sha256=None)
            record = directory / "result.json"
            write_json(record, result)
            index["runs"].append({"physical_id": physical_id, "engine": name,
                                  "record": record.relative_to(output).as_posix(), "sha256": file_sha(record)})
    index["complete"] = True
    write_json(output / "collection.json", index)
    return file_sha(output / "collection.json")


def normalized(text, mode):
    need(isinstance(text, str), "output text must be a string")
    return text if mode == "exact" else " ".join(unicodedata.normalize("NFC", text).split())


def reviewed(ref, doc):
    review = ref["review"]
    return (review["status"] == "verified" and review.get("method") == "independent_page_review"
            and bool(review.get("reviewer")) and bool(review.get("evidence_uri"))
            and review.get("original_sha256") == doc["original"]["sha256"]
            and digest(review.get("original_sha256")))


def healthy_reviewed(doc):
    review = doc.get("healthy_control_review", {})
    valid = (review.get("status") == "verified" and review.get("method") in
             ("independent_sampled_source_pages_review", "independent_whole_document_review")
            and bool(review.get("reviewer")) and bool(review.get("evidence_uri"))
            and digest(doc["original"]["sha256"]) and review.get("original_sha256") == doc["original"]["sha256"]
            and type(review.get("two_column")) is bool and doc["page_count"]["status"] == "established")
    if not valid:
        return False
    body = [ref for ref in doc["references"] if ref.get("content_scope") == "body" and
            ref["type"] in QUALITY_CHECKS and reviewed(ref, doc) and 1 <= ref["page"] <= doc["page_count"]["value"]]
    return (len(body) >= 2 and len({ref["page"] for ref in body}) >= 2 and
            (not review["two_column"] or any(ref["type"] == "ordered_anchors" for ref in body)))


def evaluate(ref, page):
    need(ref.get("expected_kind") != "source_image", "source image requires independent visual output review")
    norm = lambda text: normalized(text, ref["normalization"])
    expected = ref["expected"]
    kind = ref["type"]
    if kind == "ordered_anchors":
        text, cursor = norm(page["text"]), 0
        for anchor in expected:
            anchor = norm(anchor)
            if text.count(anchor) != 1:
                return False
            at = text.find(anchor, cursor)
            if at < 0:
                return False
            cursor = at + len(anchor)
        return True
    if kind == "snippet":
        return norm(page["text"]).count(norm(expected)) == 1
    raise ValueError("semantic fidelity requires independent output/page review, not backend fields")


def reference_sha(ref):
    return sha(encoded({k: v for k, v in ref.items() if k not in ("review", "check_id", "locator")}))


def semantic_review_index(payload, bundle):
    if bundle is None:
        return {}
    need(bundle.get("schema") == "dtox-independent-output-reviews-1" and
         bundle.get("seal_sha256") == sha(encoded(payload)), "foreign semantic review bundle/seal")
    allowed = {(doc["document_id"], ref["check_id"], engine)
               for doc in payload["manifest"]["documents"] for ref in doc["references"]
               if ref["type"] not in QUALITY_CHECKS for engine in ENGINES}
    index = {}
    need(isinstance(bundle.get("reviews"), list), "semantic review list required")
    for entry in bundle["reviews"]:
        key = (entry["document_id"], entry["check_id"], entry["engine"])
        need(key in allowed and key not in index, "foreign/duplicate semantic review")
        need(entry.get("status") in ("verified", "unreviewed", "missing"), "explicit output review state required")
        need(entry.get("outcome") in ("PASS", "FAIL", NOT_MEASURED), "invalid semantic review outcome")
        index[key] = entry
    return index


def semantic_outcome(entry, doc, ref, run):
    if not entry or entry["status"] != "verified" or entry["outcome"] == NOT_MEASURED:
        return NOT_MEASURED, "independent output review missing/unreviewed"
    valid = (entry.get("method") == "independent_output_page_review" and
             all(isinstance(entry.get(k), str) and entry[k].strip() for k in
                 ("reviewer", "evidence_uri", "source_locator", "output_locator")) and
             entry.get("original_sha256") == doc["original"]["sha256"] and
             digest(entry.get("output_sha256")) and
             entry["output_sha256"] == run["record"].get("output_sha256") and
             entry.get("reference_sha256") == reference_sha(ref) and
             type(entry.get("source_page")) is int and entry["source_page"] == ref["page"])
    if ref.get("expected_kind") == "source_image":
        valid = valid and entry.get("expected_image_sha256") == ref["expected_image"]["sha256"] and \
            entry.get("source_locator") == ref["source_locator"] and entry.get("full_declared_region_reviewed") is True
        try:
            valid = valid and file_sha(ref["expected_image"]["path"]) == ref["expected_image"]["sha256"]
        except (OSError, ValueError):
            valid = False
    return (entry["outcome"], None) if valid else (NOT_MEASURED, "independent output review binding/method invalid")


def check_counts(checks):
    measured = [c for c in checks if c["outcome"] in ("PASS", "FAIL")]
    passed = sum(c["outcome"] == "PASS" for c in measured)
    return {"declared": len(checks), "missing": len(checks) - len(measured),
            "passed_over_declared": ratio(passed, len(checks)),
            "observed_reviewed_checks": ratio(passed, len(measured))}


def load_runs(collection, collection_sha, seal_sha, read_limits=None):
    bounds = read_limits or OUTPUT_READ_LIMITS
    collection = local(collection)
    need(safe_output_sha(collection, bounds["json_bytes"]) == collection_sha, "collection SHA mismatch")
    index = read_json(collection, bounds["json_bytes"])
    need(index["schema"] == SCHEMA and index["seal_sha256"] == seal_sha and index["complete"] is True, "incomplete/foreign collection")
    runs = {}
    for binding in index["runs"]:
        path = local(collection.parent / binding["record"])
        need(contained(path, collection.parent) and path != collection.parent, "result outside collection")
        need(safe_output_sha(path, bounds["json_bytes"]) == binding["sha256"], "result SHA mismatch")
        record = read_json(path, bounds["json_bytes"])
        key = (binding["physical_id"], binding["engine"])
        need(key not in runs and (record["physical_id"], record["engine"]) == key, "duplicate/mismatched run")
        for log, expected in record["logs"].items():
            need(log in ("stdout.log", "stderr.log"), "invalid log path")
            if expected is not None:
                need(safe_output_sha(path.parent / log, bounds["log_bytes"]) == expected, "log changed")
        pages, error, scope, coverage = None, None, None, None
        if record["status"] == "OK":
            try:
                output = path.parent / "extraction.json"
                output_inventory(path.parent, bounds)
                need(safe_output_sha(output, bounds["json_bytes"]) == record["output_sha256"], "extraction SHA mismatch")
                value = read_json(output, bounds["json_bytes"])
                need(("pages" in value) != ("document" in value), "exactly one output scope required")
                scope = "pages" if "pages" in value else "document"
                if scope == "pages":
                    pages = {page["page"]: page for page in value["pages"]}
                    need(len(pages) == len(value["pages"]) and all(type(k) is int and k > 0 for k in pages), "invalid/duplicate output pages")
                else:
                    pages = {"document": value["document"]}
                    coverage = value["coverage"]
                    need(coverage.get("whole_document") is True and type(coverage.get("page_count")) is int,
                         "whole-document runner coverage metadata required")
                for page in pages.values():
                    need(isinstance(page["text"], str), "page text required")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                pages, error = None, f"{type(exc).__name__}: {exc}"
        runs[key] = {"record": record, "pages": pages, "output_error": error,
                     "output_scope": scope, "coverage": coverage}
    return runs


def ratio(passed, denominator):
    return {"numerator": passed, "denominator": denominator,
            "ratio": passed / denominator if denominator else None}


def physical_checks(rows):
    merged = {}
    severity = {"PASS": 0, "FAIL": 1, "UNKNOWN": 2, NOT_MEASURED: 2}
    for row in rows:
        for check in row["checks"]:
            key = (row["physical_id"], check["reference_sha256"])
            if key not in merged or severity[check["outcome"]] > severity[merged[key]["outcome"]]:
                merged[key] = check
    return merged


def grade(payload, runs, semantic_reviews=None):
    docs = validate_manifest(payload["manifest"])
    gates = payload["config"]["gates"]
    required_classes = set(gates.get("required_classes", CLASSES))
    expected_runs = {(a["physical_id"], e) for a in payload["assignments"].values() for e in ENGINES}
    need(set(runs) <= expected_runs, "foreign collection runs")
    output_reviews = semantic_review_index(payload, semantic_reviews)
    rows, blockers = [], []
    if payload["counts"]["physical_documents"] != gates["expected_physical_documents"]:
        blockers.append("physical sample size differs from predeclared gate")
    for identity, doc in sorted(docs.items()):
        assignment = payload["assignments"][identity]
        missing = [key for key in ("source_uri", "page_count", "real_document")
                   if doc[key]["status"] != "established"]
        if doc["real_document"]["value"] is not True:
            missing.append("real_document_not_verified")
        if "healthy" in doc["classes"] and not healthy_reviewed(doc):
            missing.append("healthy_control_not_independently_verified")
        if missing or doc["exceptions"] or payload["input_states"][identity]["status"] != "READY":
            blockers.append(identity + ": missing identity/real-document/page evidence, input unavailable or explicit exception")
        refs = doc["references"]
        verified = [ref for ref in refs if reviewed(ref, doc)]
        reference_gate = sum(r["type"] in QUALITY_CHECKS for r in verified) >= gates["min_reviewed_checks_per_document"] and len(verified) == len(refs)
        reference_gate = reference_gate and all(any(r["type"] == CLASS_CHECK[cls] for r in verified) for cls in doc["classes"])
        missing_semantic_types = sorted({SEMANTIC_CHECK[cls] for cls in doc["classes"] if cls in SEMANTIC_CHECK} -
                                        {r["type"] for r in verified})
        reference_gate = reference_gate and not missing_semantic_types
        if not reference_gate:
            blockers.append(identity + ": references missing/unreviewed/unbound")
        for engine in ENGINES:
            run = runs.get((assignment["physical_id"], engine))
            pages = run["pages"] if run else None
            page_count = doc["page_count"]["value"] if doc["page_count"]["status"] == "established" else None
            scope = run["output_scope"] if run else None
            whole = pages is not None and page_count is not None and scope == payload["config"]["engines"][engine]["output_scope"]
            if whole:
                whole = (set(pages) == set(range(1, page_count + 1)) if scope == "pages" else
                         run["coverage"]["page_count"] == page_count)
            checks = []
            for ref in refs:
                semantic = ref["type"] not in QUALITY_CHECKS
                entry = output_reviews.get((identity, ref["check_id"], engine)) if semantic else None
                outcome, error = NOT_MEASURED if semantic else "UNKNOWN", None
                if reviewed(ref, doc) and whole and 1 <= ref["page"] <= page_count:
                    try:
                        if semantic:
                            outcome, error = semantic_outcome(entry, doc, ref, run)
                        else:
                            outcome = "PASS" if evaluate(ref, pages[ref["page"] if scope == "pages" else "document"]) else "FAIL"
                    except (KeyError, TypeError, ValueError) as exc:
                        error = f"{type(exc).__name__}: {exc}"
                checks.append({"check_id": ref["check_id"], "type": ref["type"], "page": ref["page"],
                               "reference_sha256": reference_sha(ref),
                               "measurement": "semantic_independent_review" if semantic else "markdown_quality",
                               "outcome": outcome, "error": error, "independent_review": entry})
            observed = [c for c in checks if c["outcome"] in ("PASS", "FAIL")]
            passed = sum(c["outcome"] == "PASS" for c in observed)
            quality = check_counts([c for c in checks if c["type"] in QUALITY_CHECKS])
            semantics = check_counts([c for c in checks if c["type"] not in QUALITY_CHECKS])
            status = (UNKNOWN if not reference_gate or not whole or missing or doc["exceptions"] or
                      len(observed) != len(refs) else "PASS" if passed == len(refs) else "FAIL")
            rows.append({"document_id": identity, **assignment, "classes": doc["classes"], "engine": engine,
                         "original_sha256": doc["original"]["sha256"], "source_uri": doc["source_uri"],
                         "source_version": doc["source_version"], "historical_corpus_version_identity_proven": False,
                         "status": status, "execution_status": run["record"]["status"] if run else "MISSING_RUN",
                         "output_error": run["output_error"] if run else None, "whole_document_observed": whole,
                         "output_locator_scope": scope, "source_reference_scope": "independently reviewed source page",
                         "control_scope": "checked passages/pages NOT wholepaper healthy" if "healthy" in doc["classes"] else None,
                         "control_review_method": doc.get("healthy_control_review", {}).get("method") if "healthy" in doc["classes"] else None,
                         "control_body_pages": sorted({r["page"] for r in verified if r.get("content_scope") == "body" and
                                                        r["type"] in QUALITY_CHECKS}) if "healthy" in doc["classes"] else [],
                         "completeness": {"scope": "observed page count/coverage/status NOT text completeness",
                                          "expected_page_count": page_count,
                                          "observed_page_count": (run.get("coverage") or {}).get("page_count") if scope == "document" else
                                          len(pages) if pages is not None else None,
                                          "coverage_observed": whole, "text_completeness_proven": False},
                         "resources": {key: run["record"].get(key) if run else None for key in
                                       ("elapsed_seconds", "cpu_seconds", "peak_rss_bytes", "metrics_scope", "exit_code")},
                         "references_declared": len(refs), "references_reviewed": len(verified), "checks": checks,
                         "quality_checks": quality, "semantic_checks": semantics,
                         "missing_semantic_required_types": missing_semantic_types,
                         "semantic_failures": sum(c["outcome"] == "FAIL" for c in checks if c["type"] not in QUALITY_CHECKS),
                         "capability": {kind: {"records_present": any(bool(page.get(kind)) for page in (pages or {}).values()),
                                               "quality_inference": False} for kind in ("formulas", "tables", "sections")},
                         "observed_reviewed_checks": quality["observed_reviewed_checks"], "exceptions": doc["exceptions"]})
    summaries = []
    for split in ("all", "development", "holdout"):
        for cls in ("all", *CLASSES):
            for engine in ENGINES:
                subset = [r for r in rows if r["engine"] == engine and (split == "all" or r["split"] == split)
                          and (cls == "all" or cls in r["classes"])]
                physical = {r["physical_id"] for r in subset}
                # Count all distinct alias references, identical physical checks only once.
                checks = list(physical_checks(subset).values())
                observed = [c for c in checks if c["outcome"] in ("PASS", "FAIL")]
                quality = check_counts([c for c in checks if c["type"] in QUALITY_CHECKS])
                semantics = check_counts([c for c in checks if c["type"] not in QUALITY_CHECKS])
                check_ratio = quality["observed_reviewed_checks"]
                summary = {"split": split, "class": cls, "engine": engine, "logical_documents": len(subset),
                           "physical_documents": len(physical), "document_pass": ratio(sum(all(r["status"] == "PASS" for r in subset if r["physical_id"] == p) for p in physical), len(physical)),
                           "status_counts": dict(Counter(r["status"] for r in subset)), "checks_declared": len(checks),
                           "checks_unknown": len(checks) - len(observed), "observed_reviewed_checks": check_ratio}
                summary.update(quality_checks=quality, semantic_checks=semantics,
                               class_semantic_checks=check_counts([c for c in checks if c["type"] == SEMANTIC_CHECK.get(cls)]),
                               measurement_status=NOT_MEASURED if not physical else UNKNOWN if any(r["status"] == UNKNOWN for r in subset) else "MEASURED_OBSERVED_CHECKS",
                               missing_semantic_required_documents=sum(bool(r["missing_semantic_required_types"]) for r in subset),
                               semantic_failures=sum(r["semantic_failures"] for r in subset))
                summaries.append(summary)
                if cls in required_classes | {"healthy"} and split != "all" and len(physical) < gates["min_documents_per_class_split"]:
                    blockers.append(f"{split}/{cls}: insufficient physical class coverage")
                threshold = gates["min_observed_check_ratio"].get(cls, gates["min_observed_check_ratio"]["all"])
                if subset and (any(r["status"] == UNKNOWN for r in subset) or check_ratio["ratio"] is None or
                               engine == "docling" and (check_ratio["ratio"] < threshold or summary["semantic_failures"])):
                    blockers.append(f"{engine}/{split}/{cls}: measurement/quality gate failed")
    healthy_regressions, gain = 0, 0
    paired = {e: physical_checks([r for r in rows if r["engine"] == e]) for e in ENGINES}
    healthy = {r["physical_id"] for r in rows if "healthy" in r["classes"]}
    for key, left in paired["pymupdf"].items():
        right = paired["docling"][key]
        if left["outcome"] in ("PASS", "FAIL") and right["outcome"] in ("PASS", "FAIL"):
            gain += int(right["outcome"] == "PASS") - int(left["outcome"] == "PASS")
            if key[0] in healthy and left["outcome"] == "PASS" and right["outcome"] != "PASS":
                healthy_regressions += 1
    if healthy_regressions > gates["max_healthy_regressions"]:
        blockers.append("healthy-control regression gate failed")
    if gain < gates["min_candidate_check_gain"]:
        blockers.append("candidate paired-check improvement gate failed")
    healthy_gate = healthy_regressions <= gates["max_healthy_regressions"]
    for summary in summaries:
        if summary["class"] == "healthy" and summary["split"] != "all":
            healthy_gate = healthy_gate and summary["physical_documents"] >= gates["min_documents_per_class_split"]
            healthy_gate = healthy_gate and not summary["status_counts"].get(UNKNOWN)
            healthy_gate = healthy_gate and (summary["engine"] != "docling" or not summary["semantic_failures"])
            if summary["engine"] == "docling":
                healthy_gate = healthy_gate and summary["observed_reviewed_checks"]["ratio"] is not None and \
                    summary["observed_reviewed_checks"]["ratio"] >= gates["min_observed_check_ratio"].get("healthy", gates["min_observed_check_ratio"]["all"])
    class_decisions = []
    for cls in CLASSES:
        issues = []
        present = any(cls in doc["classes"] for doc in docs.values())
        if not present:
            issues.append("class absent from frozen sample: NOT_MEASURED")
        if cls not in required_classes:
            issues.append("class outside frozen required_classes: NOT_ACCEPTED")
        if payload["counts"]["physical_documents"] != gates["expected_physical_documents"]:
            issues.append("frozen sample-size gate failed")
        if not healthy_gate:
            issues.append("independent healthy-control/holdout gate failed")
        for summary in summaries:
            if summary["class"] != cls or summary["split"] == "all":
                continue
            if summary["physical_documents"] < gates["min_documents_per_class_split"]:
                issues.append(summary["split"] + ": insufficient class coverage")
            if summary["status_counts"].get(UNKNOWN):
                issues.append(summary["split"] + "/" + summary["engine"] + ": incomplete measurement")
            minimum = gates.get("min_semantic_checks_per_class_split", {}).get(cls)
            if minimum is not None and (summary["class_semantic_checks"]["declared"] < minimum or
                                        summary["class_semantic_checks"]["missing"]):
                issues.append(summary["split"] + ": predeclared semantic coverage gate failed")
            if summary["engine"] == "docling" and summary["semantic_failures"]:
                issues.append(summary["split"] + ": independently reviewed semantic failure")
            if summary["engine"] == "docling" and (summary["observed_reviewed_checks"]["ratio"] is None or
                summary["observed_reviewed_checks"]["ratio"] < gates["min_observed_check_ratio"].get(cls, gates["min_observed_check_ratio"]["all"])):
                issues.append(summary["split"] + ": candidate quality threshold failed")
        per_engine = {e: physical_checks([r for r in rows if r["engine"] == e and cls in r["classes"]]) for e in ENGINES}
        class_gain, regressions = 0, 0
        for key, left in per_engine["pymupdf"].items():
            right = per_engine["docling"][key]
            if left["outcome"] in ("PASS", "FAIL") and right["outcome"] in ("PASS", "FAIL"):
                class_gain += int(right["outcome"] == "PASS") - int(left["outcome"] == "PASS")
                regressions += int(left["outcome"] == "PASS" and right["outcome"] == "FAIL")
        if (cls != "healthy" and class_gain < gates["min_candidate_check_gain"]) or regressions > gates["max_class_regressions"]:
            issues.append("predeclared class improvement/no-regression gate failed")
        class_decisions.append({"class": cls, "candidate_check_gain": class_gain, "regressions": regressions,
                                "required": cls in required_classes,
                                "measurement_status": NOT_MEASURED if not present else UNKNOWN if any(
                                    s["class"] == cls and s["status_counts"].get(UNKNOWN) for s in summaries) else "MEASURED_OBSERVED_CHECKS",
                                "exception": "CLASS_ABSENT_FROM_FROZEN_SAMPLE" if not present else None,
                                "blockers": sorted(set(issues)), "decision": "NOT_ACCEPTED" if not present or cls not in required_classes else
                                "NO_WINNER" if issues else "DOCLING_ELIGIBLE_FOR_CLASS"})
    eligible = [c["class"] for c in class_decisions if not c["blockers"]]
    decision = ("DOCLING_PASSES_PREDECLARED_GATES" if len(eligible) == len(CLASSES) and not blockers else
                "CLASS_SPECIFIC_CANDIDATES" if eligible else "NO_WINNER")
    return {"schema": SCHEMA, "counts": payload["counts"], "documents": rows, "summaries": summaries,
            "execution_identity": payload["config"]["sandbox"].get("execution_identity", {"mode": "unprivileged"}),
            "measurement_status": UNKNOWN if any(r["status"] == UNKNOWN for r in rows) else "MEASURED_OBSERVED_CHECKS",
            "blockers": sorted(set(blockers)), "healthy_regressions": healthy_regressions, "candidate_paired_check_gain": gain,
            "decision": decision, "class_decisions": class_decisions, "eligible_classes": eligible,
            "required_classes": sorted(required_classes),
            "healthy_gate_status": "PASS" if healthy_gate else "NOT_ACCEPTED",
            "required_route_status": "PASS" if healthy_gate and required_classes <= set(eligible) else "NOT_ACCEPTED",
            "universal_winner": decision == "DOCLING_PASSES_PREDECLARED_GATES",
            "control_scope": "checked passages/pages NOT wholepaper healthy",
            "quality_scope": "observed independently reviewed page-grounded checks, NOT full-document precision/recall",
            "quality_gain_scope": "paired frozen Markdown checks and independently reviewed semantic checks; native capabilities excluded",
            "semantic_review_bundle_sha256": sha(encoded(semantic_reviews)) if semantic_reviews is not None else None,
            "cost_scope": {"per_document": "whole cold wrapper process: validation, hashing, imports, model loading, conversion, export",
                           "asset_hashing": {"policy": "unchanged per-document full hashing plus outer preflight",
                                             "included_in_per_document_metrics": True,
                                             "separately_measured": False, "elapsed_seconds": None,
                                             "status": NOT_MEASURED},
                           "inference_only_metrics_available": False},
            "publication_authorized": False, "backend_self_grades_used": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    environment = commands.add_parser("environment")
    environment.add_argument("--packages", nargs="+", required=True)
    seal = commands.add_parser("seal")
    seal.add_argument("--manifest", required=True)
    seal.add_argument("--config", required=True)
    seal.add_argument("--output", required=True)
    for command in ("collect", "grade"):
        sub = commands.add_parser(command)
        sub.add_argument("--seal", required=True)
        sub.add_argument("--seal-sha256", required=True)
        sub.add_argument("--output", required=True)
        if command == "grade":
            sub.add_argument("--collection", required=True)
            sub.add_argument("--collection-sha256", required=True)
            sub.add_argument("--semantic-reviews")
            sub.add_argument("--semantic-reviews-sha256")
    args = parser.parse_args(argv)
    if args.command == "environment":
        print(json.dumps(environment_snapshot(args.packages)))
    elif args.command == "seal":
        manifest, config = read_json(local(args.manifest)), read_json(local(args.config))
        sealed = freeze(manifest, config, (args.manifest, args.config))
        forbidden = [args.manifest, args.config, *config["sandbox"]["forbidden_roots"],
                     *(p["path"] for p in gold_image_pins(manifest)),
                     *(d["original"]["path"] for d in manifest["documents"] if d["original"]["path"])]
        output = create_output(args.output, forbidden)
        write_json(output / "seal.json", sealed)
        print(json.dumps({"seal_sha256": sealed["sha256"], "counts": sealed["payload"]["counts"]}))
    else:
        payload = load_seal(args.seal, args.seal_sha256)
        forbidden = [args.seal, *payload["config"]["sandbox"]["forbidden_roots"],
                     *(p["path"] for p in gold_image_pins(payload["manifest"])),
                     *(d["original"]["path"] for d in payload["manifest"]["documents"] if d["original"]["path"])]
        if args.command == "collect":
            started, cpu_started = time.monotonic(), time.process_time()
            actual = execution_preflight(payload, args.seal, args.output)
            costs = {"scope": "outer runtime/model/OS-identity/environment preflight; not extraction",
                     "elapsed_seconds": time.monotonic() - started,
                     "cpu_seconds": time.process_time() - cpu_started}
            output = create_output(args.output, forbidden)
            print(json.dumps({"collection_sha256": collect(payload, args.seal_sha256, output, observed_environment=actual,
                                                          preflight_costs=costs)}))
        else:
            need(bool(args.semantic_reviews) == bool(args.semantic_reviews_sha256), "review path and SHA must be paired")
            reviews = None
            if args.semantic_reviews:
                need(safe_output_sha(args.semantic_reviews, OUTPUT_READ_LIMITS["json_bytes"]) == args.semantic_reviews_sha256,
                     "semantic review file SHA mismatch")
                reviews = read_json(local(args.semantic_reviews))
            runs = load_runs(args.collection, args.collection_sha256, args.seal_sha256,
                             payload["config"]["sandbox"].get("output_read_limits", OUTPUT_READ_LIMITS))
            output = create_output(args.output, [*forbidden, args.collection, *([args.semantic_reviews] if reviews is not None else [])])
            report = grade(payload, runs, reviews)
            report["semantic_review_file_sha256"] = args.semantic_reviews_sha256
            report["cost_scope"]["outer_preflight"] = read_json(local(args.collection)).get("preflight_costs")
            report["seal_sha256"] = args.seal_sha256
            report["collection_sha256"] = args.collection_sha256
            write_json(output / "quality.json", report)
            print(json.dumps({"decision": report["decision"], "quality_sha256": file_sha(output / "quality.json")}))


if __name__ == "__main__":
    main()
