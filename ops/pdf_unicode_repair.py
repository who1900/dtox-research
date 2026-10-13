"""Offline candidate generator, not a global PDF text-cleanliness validator.

Only explicit two-byte Identity-H bfchar mappings are eligible. No fuzzy font
matching, shaping guesses, network access, or changes to the input document.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys

from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTCollection, TTFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, IndirectObject, NameObject


SCOPE = "supported_explicit_tounicode_mappings_only"
LIMIT = 128 * 1024 * 1024
CMAP_LIMIT = 4 * 1024 * 1024
GLYPH_LIMIT = 150000


class RepairError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def bad_unicode(text):
    return any(c == "\ufffd" or (ord(c) < 32 and c not in "\t\n\r") for c in text)


def printable(text):
    return bool(text) and len(text) <= 32 and text.isprintable() and not bad_unicode(text)


def parse_cmap(data):
    """Return source, Unicode, and exact RHS offsets; reject unsupported grammar."""
    if len(data) > CMAP_LIMIT:
        raise RepairError("ToUnicode size limit")
    clean = re.sub(rb"%[^\r\n]*", lambda m: b" " * len(m[0]), data)
    if re.search(rb"\b(?:beginbfrange|begincidchar|begincidrange|usecmap|beginnotdefchar|beginnotdefrange)\b", clean):
        raise RepairError("unsupported CMap operator (including bfrange/usecmap)")
    token = rb"<([0-9A-Fa-f\s]*)>"
    ranges, entries, seen = [], [], set()
    for kind in (b"codespacerange", b"bfchar"):
        blocks = list(re.finditer(rb"(\d+)\s+begin" + kind + rb"\b(.*?)\bend" + kind + rb"\b", clean, re.S))
        if len(re.findall(rb"\bbegin" + kind + rb"\b", clean)) != len(blocks):
            raise RepairError("malformed CMap block")
        for block in blocks:
            body = block[2]
            pairs = list(re.finditer(token + rb"\s+" + token, body))
            if len(pairs) != int(block[1]) or int(block[1]) > 65536:
                raise RepairError("CMap count mismatch or limit")
            remainder = re.sub(token + rb"\s+" + token, b"", body)
            if remainder.strip():
                raise RepairError("malformed CMap pair")
            for pair in pairs:
                try:
                    left = bytes.fromhex(pair[1].decode("ascii"))
                    right = bytes.fromhex(pair[2].decode("ascii"))
                except ValueError as exc:
                    raise RepairError("malformed CMap hex") from exc
                if len(left) != 2:
                    raise RepairError("Identity-H requires exactly two-byte source codes")
                cid = int.from_bytes(left, "big")
                if kind == b"codespacerange":
                    if len(right) != 2 or cid > int.from_bytes(right, "big"):
                        raise RepairError("invalid two-byte codespace range")
                    ranges.append((cid, int.from_bytes(right, "big")))
                    continue
                if cid in seen:
                    raise RepairError("duplicate bfchar source target")
                seen.add(cid)
                try:
                    text = right.decode("utf-16-be", errors="strict")
                except UnicodeError as exc:
                    raise RepairError("invalid UTF-16 destination") from exc
                if not text:
                    raise RepairError("empty CMap destination")
                entries.append({"cid": cid, "text": text, "rhs": right.hex(),
                                "span": (block.start(2) + pair.start(2), block.start(2) + pair.end(2))})
    if not ranges or not entries or len(entries) > 65536:
        raise RepairError("missing codespace/bfchar or mapping limit")
    ordered = sorted(ranges)
    if any(a[1] >= b[0] for a, b in zip(ordered, ordered[1:])):
        raise RepairError("overlapping codespace ranges")
    if any(not any(lo <= e["cid"] <= hi for lo, hi in ranges) for e in entries):
        raise RepairError("source outside CMap codespace")
    return entries


def replace_mappings(data, replacements):
    entries = parse_cmap(data)
    found = {e["cid"]: e for e in entries}
    edits = []
    for cid, value in replacements.items():
        if cid not in found or not bad_unicode(found[cid]["text"]) or not printable(value):
            raise RepairError("replacement must target an invalid mapping with printable Unicode")
        edits.append((*found[cid]["span"], value.encode("utf-16-be").hex().encode("ascii")))
    for start, end, value in sorted(edits, reverse=True):
        data = data[:start] + value + data[end:]
    after = {e["cid"]: e["text"] for e in parse_cmap(data)}
    expected = {e["cid"]: replacements.get(e["cid"], e["text"]) for e in entries}
    if after != expected:
        raise RepairError("replacement verification failed")
    return data


def unique_cmap(font):
    by_code = {}
    for table in font["cmap"].tables if "cmap" in font else []:
        if table.isUnicode():
            for code, glyph in table.cmap.items():
                by_code.setdefault(code, set()).add(glyph)
    by_glyph, conflicted = {}, set()
    for code, glyphs in by_code.items():
        if len(glyphs) != 1:
            conflicted.update(glyphs)
        for glyph in glyphs:
            by_glyph.setdefault(glyph, set()).add(code)
    result = {}
    for glyph, codes in by_glyph.items():
        if glyph not in conflicted and len(codes) == 1:
            code = next(iter(codes))
            if 0 <= code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF and printable(chr(code)):
                result[glyph] = chr(code)
    return result


def semantic_proofs(font):
    direct = unique_cmap(font)
    cmap_glyphs = {glyph for table in (font["cmap"].tables if "cmap" in font else [])
                   if table.isUnicode() for glyph in table.cmap.values()}
    blocked_targets = cmap_glyphs - direct.keys()
    result = {g: [{"kind": "cmap", "unicode": text}] for g, text in direct.items()}
    if "GSUB" not in font:
        return result
    lookup_list = getattr(font["GSUB"].table, "LookupList", None)
    for index, lookup in enumerate(getattr(lookup_list, "Lookup", [])):
        for subindex, original in enumerate(lookup.SubTable):
            kind, sub = lookup.LookupType, original
            if kind == 7:
                kind, sub = original.ExtensionLookupType, original.ExtSubTable
            transitions = []
            if kind == 1:
                transitions = [(target, [source]) for source, target in sub.mapping.items()]
            elif kind == 4:
                transitions = [(lig.LigGlyph, [first] + list(lig.Component))
                               for first, ligatures in sub.ligatures.items() for lig in ligatures]
            elif kind == 2:
                blocked_targets.update(glyph for outputs in sub.mapping.values() for glyph in outputs)
            elif kind == 3:
                blocked_targets.update(glyph for outputs in sub.alternates.values() for glyph in outputs)
            for target, components in transitions:
                if not all(component in direct for component in components):
                    blocked_targets.add(target)
                if target in blocked_targets:
                    continue
                if all(component in direct for component in components):
                    text = "".join(direct[c] for c in components)
                    if printable(text):
                        result.setdefault(target, []).append({
                            "kind": "gsub", "lookup_index": index, "subtable_index": subindex,
                            "lookup_type": lookup.LookupType, "effective_type": kind,
                            "components": components, "component_unicode": [direct[c] for c in components],
                            "unicode": text,
                        })
    return {target: proofs for target, proofs in result.items() if target not in blocked_targets}


def fingerprint(font, glyph, glyph_set=None):
    glyph_set = glyph_set if glyph_set is not None else font.getGlyphSet()
    pen = DecomposingRecordingPen(glyph_set)
    glyph_set[glyph].draw(pen)
    if not any(op in ("lineTo", "curveTo", "qCurveTo") for op, _ in pen.value):
        raise RepairError("empty glyph outline")
    if len(pen.value) > 100000:
        raise RepairError("glyph outline limit")
    advance, lsb = font["hmtx"].metrics[glyph]
    identity = (font["head"].unitsPerEm, advance, lsb, pen.value)
    encoded = json.dumps(identity, separators=(",", ":"), allow_nan=False).encode("ascii")
    return {"sha256": sha256(encoded), "units_per_em": identity[0], "advance": advance,
            "lsb": lsb, "outline": pen.value}


def build_references(sources):
    """Sources are explicit (path, immutable bytes); enumerate every TTC member."""
    index, manifest, glyph_count = {}, [], 0
    for path, data in sources:
        if len(data) > LIMIT:
            raise RepairError("reference size limit")
        digest = sha256(data)
        collection = TTCollection(io.BytesIO(data), lazy=False) if data[:4] == b"ttcf" else None
        fonts = collection.fonts if collection else [TTFont(io.BytesIO(data), lazy=False)]
        if len(fonts) > 32:
            raise RepairError("TTC member limit")
        try:
            for member, font in enumerate(fonts):
                if "glyf" not in font:
                    raise RepairError("only TrueType glyf reference fonts supported")
                manifest.append({"path": str(path), "sha256": digest, "member_index": member})
                proofs, glyph_set = semantic_proofs(font), font.getGlyphSet()
                for gid, glyph in enumerate(font.getGlyphOrder()):
                    glyph_count += 1
                    if glyph_count > GLYPH_LIMIT:
                        raise RepairError("reference glyph budget exceeded")
                    try:
                        fp = fingerprint(font, glyph, glyph_set)
                    except RepairError as exc:
                        if str(exc) == "empty glyph outline":
                            continue
                        raise
                    index.setdefault(fp["sha256"], []).append({
                        "reference_path": str(path), "reference_sha256": digest, "member_index": member,
                        "glyph_id": gid, "glyph_name": glyph, "fingerprint": fp,
                        "semantic_proofs": proofs.get(glyph, []),
                    })
        finally:
            for font in fonts:
                font.close()
    return {"index": index, "manifest": manifest}


def prove_glyph(font, gid, references, embedded_sha256, source_sha256):
    order = font.getGlyphOrder()
    if gid < 0 or gid >= len(order):
        raise RepairError("embedded glyph ID out of bounds")
    fp = fingerprint(font, order[gid])
    matches = references["index"].get(fp["sha256"], [])
    if not matches:
        raise RepairError("no exact outline and metrics match")
    texts, proofs = set(), []
    for match in matches:
        if match["fingerprint"] != fp:
            raise RepairError("fingerprint collision")
        candidates = {p["unicode"] for p in match["semantic_proofs"]}
        if len(candidates) != 1 or not all(printable(t) for t in candidates):
            raise RepairError("matching reference glyph has ambiguous or unknown Unicode")
        texts.update(candidates)
        proofs.append({k: v for k, v in match.items() if k != "fingerprint"})
    if len(texts) != 1:
        raise RepairError("outline matches disagree on Unicode")
    return {"unicode": next(iter(texts)), "embedded_glyph_id": gid, "embedded_glyph_name": order[gid],
            "embedded_font_sha256": embedded_sha256, "source_sha256": source_sha256,
            "fingerprint": {k: v for k, v in fp.items() if k != "outline"}, "matches": proofs}


def cid_to_gid(cid, descendant):
    mapping = descendant.get("/CIDToGIDMap")
    if mapping is None or mapping == "/Identity":
        return cid
    mapping = mapping.get_object()
    if not hasattr(mapping, "get_data"):
        raise RepairError("unsupported CIDToGIDMap")
    data = mapping.get_data()
    if len(data) % 2 or len(data) > 131072 or cid < 0 or 2 * cid + 2 > len(data):
        raise RepairError("CIDToGIDMap bounds or length")
    return int.from_bytes(data[2 * cid:2 * cid + 2], "big")


def collect_fonts(document):
    fonts, locations, budget = {}, {}, [0]
    def resolve(obj):
        return obj.get_object() if hasattr(obj, "get_object") else obj

    def walk(resources, page, path, seen, depth):
        if resources is None:
            return
        resources = resolve(resources)
        key = id(resources)
        if key in seen:
            return
        seen.add(key)
        budget[0] += 1
        if depth > 64 or budget[0] > 20000:
            raise RepairError("resource traversal budget exceeded")
        for name, ref in resolve(resources.get("/Font", {})).items():
            font = resolve(ref)
            font_key = id(font)
            fonts[font_key] = font
            if len(fonts) > 4096:
                raise RepairError("font traversal budget exceeded")
            locations.setdefault(font_key, []).append({"page": page, "path": path + "/Font/" + str(name)})
        for name, ref in resolve(resources.get("/XObject", {})).items():
            form = resolve(ref)
            if form.get("/Subtype") == "/Form":
                walk(form.get("/Resources", resources), page, path + "/XObject/" + str(name), seen, depth + 1)
    if len(document.pages) > 10000:
        raise RepairError("page budget exceeded")
    for number, page in enumerate(document.pages, 1):
        walk(page.get("/Resources"), number, "/Resources", set(), 0)
    return [(font, locations[key]) for key, font in fonts.items()]


def repair_document(document, references, source_sha256):
    report = {"scope": SCOPE, "source_sha256": source_sha256, "references": references["manifest"],
              "status": "no_verified_changes", "fonts": [], "changes": [], "unresolved": [],
              "warnings": ["Not a global repair or semantic-cleanliness assertion. Missing mappings, reader-generated unknowns, and unsupported fonts/CMaps require independent raw-reader audit."]}
    for ordinal, (font, locations) in enumerate(collect_fonts(document)):
        record = {"font_index": ordinal, "locations": locations, "status": "unchanged", "changes": []}
        report["fonts"].append(record)
        try:
            stream = font.get("/ToUnicode")
            if stream is None:
                raise RepairError("missing ToUnicode; no inferred mapping")
            original = stream.get_object().get_data()
            record["old_tounicode_sha256"] = sha256(original)
            entries = parse_cmap(original)
            targets = [e for e in entries if bad_unicode(e["text"])]
            record["invalid_mapping_count"] = len(targets)
            if not targets:
                record["new_tounicode_sha256"] = sha256(original)
                continue
            if font.get("/Subtype") != "/Type0" or font.get("/Encoding") != "/Identity-H":
                raise RepairError("requires Type0 Identity-H")
            descendants = font.get("/DescendantFonts")
            if descendants is None or len(descendants.get_object()) != 1:
                raise RepairError("requires one descendant font")
            descendant = descendants.get_object()[0].get_object()
            if descendant.get("/Subtype") != "/CIDFontType2":
                raise RepairError("requires CIDFontType2")
            descriptor = descendant.get("/FontDescriptor")
            descriptor = descriptor.get_object() if descriptor is not None else {}
            embedded = descriptor.get("/FontFile2")
            if embedded is None:
                raise RepairError("missing embedded FontFile2")
            font_data = embedded.get_object().get_data()
            if len(font_data) > LIMIT:
                raise RepairError("embedded font size limit")
            embedded_sha = sha256(font_data)
            record["embedded_font_sha256"] = embedded_sha
            replacements = {}
            with TTFont(io.BytesIO(font_data), lazy=False) as tt:
                if "glyf" not in tt:
                    raise RepairError("requires embedded TrueType glyf")
                for entry in targets:
                    try:
                        proof = prove_glyph(tt, cid_to_gid(entry["cid"], descendant), references, embedded_sha, source_sha256)
                        replacements[entry["cid"]] = proof["unicode"]
                        change = {"font_index": ordinal, "cid": entry["cid"], "old_unicode": entry["text"],
                                  "old_rhs_hex": entry["rhs"], "new_unicode": proof["unicode"],
                                  "new_rhs_hex": proof["unicode"].encode("utf-16-be").hex(), "proof": proof}
                        record["changes"].append(change)
                    except (RepairError, KeyError, ValueError) as exc:
                        report["unresolved"].append({"font_index": ordinal, "locations": locations, "cid": entry["cid"], "reason": str(exc)})
            if replacements:
                repaired = replace_mappings(original, replacements)
                isolated = DecodedStreamObject()
                isolated.set_data(repaired)
                font[NameObject("/ToUnicode")] = document._add_object(isolated)
                record["new_tounicode_sha256"] = sha256(repaired)
                report["changes"].extend(record["changes"])
                record["status"] = "supported_mappings_repaired"
        except Exception as exc:
            report["unresolved"].append({"font_index": ordinal, "locations": locations, "reason": str(exc)})
            record["status"] = "unsupported_or_unresolved"
    if report["changes"]:
        report["status"] = "partial" if report["unresolved"] else "supported_mappings_repaired"
    return report


def verify_candidate(document, report):
    observed = {}
    for font, locations in collect_fonts(document):
        for location in locations:
            observed[(location["page"], location["path"])] = font
    verified = 0
    for record in report["fonts"]:
        expected_hash = record.get("new_tounicode_sha256", record.get("old_tounicode_sha256"))
        if expected_hash is None:
            continue
        for location in record["locations"]:
            font = observed.get((location["page"], location["path"]))
            if font is None or "/ToUnicode" not in font:
                raise RepairError("candidate font mapping missing")
            if record["changes"] and not isinstance(font.raw_get("/ToUnicode"), IndirectObject):
                raise RepairError("changed ToUnicode stream must be indirect")
            data = font["/ToUnicode"].get_data()
            if sha256(data) != expected_hash:
                raise RepairError("candidate ToUnicode hash mismatch")
            if record["changes"]:
                actual = {e["cid"]: e["text"] for e in parse_cmap(data)}
                for change in record["changes"]:
                    if change["proof"]["source_sha256"] != report["source_sha256"] or actual.get(change["cid"]) != change["new_unicode"]:
                        raise RepairError("candidate proof/source or Unicode mismatch")
        verified += len(record["changes"])
    if not verified or verified != len(report["changes"]):
        raise RepairError("no verified changes or change count mismatch")
    return verified


def validate_paths(input_path, reference_paths, output_path, report_path):
    paths = [Path(os.path.abspath(os.fspath(p))) for p in [input_path, *reference_paths, output_path, report_path]]
    if not reference_paths or len(reference_paths) > 16:
        raise RepairError("require 1..16 explicit reference font files")
    for path in paths:
        if str(path).startswith(("\\\\", "//")) or "://" in str(path):
            raise RepairError("only local filesystem paths supported")
        if os.name == "nt":
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
                raise RepairError("mapped network drives forbidden")
        for part in [path, *path.parents]:
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise RepairError("symlink/junction paths forbidden")
    for index, path in enumerate(paths):
        for other in paths[:index]:
            if os.path.normcase(str(path.resolve())) == os.path.normcase(str(other.resolve())) or (path.exists() and other.exists() and os.path.samefile(path, other)):
                raise RepairError("path alias/collision")
    for path in paths[:-2]:
        if not path.is_file() or path.stat().st_size > LIMIT:
            raise RepairError("missing/nonregular/oversized input or reference")
    for path in paths[-2:]:
        if os.path.lexists(path) or not path.parent.is_dir():
            raise RepairError("output/report must not exist; parent must exist")
    return paths


def read_bounded(path):
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise RepairError("file size limit")
    return data


def exclusive(path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def run(input_path, reference_paths, output_path, report_path):
    paths = validate_paths(input_path, reference_paths, output_path, report_path)
    source_path, destination, receipt = paths[0], paths[-2], paths[-1]
    source = read_bounded(source_path)
    references = build_references([(p, read_bounded(p)) for p in paths[1:-2]])
    reader = PdfReader(io.BytesIO(source), strict=True)
    if reader.is_encrypted:
        raise RepairError("encrypted PDF unsupported")
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    report = repair_document(writer, references, sha256(source))
    report.update({"source_path": str(source_path), "output_path": str(destination),
                   "candidate_created": False, "candidate_verified": False})
    validate_paths(input_path, reference_paths, output_path, report_path)
    if sha256(read_bounded(source_path)) != report["source_sha256"]:
        raise RepairError("input changed during analysis")
    if not report["changes"]:
        with exclusive(receipt) as stream:
            stream.write(json.dumps(report, ensure_ascii=True, indent=2).encode("utf-8"))
        return report
    with exclusive(destination) as output:
        report["candidate_created"] = True
        with exclusive(receipt) as report_stream:
            try:
                writer.write(output)
                output.flush()
                os.fsync(output.fileno())
                candidate = read_bounded(destination)
                report["verified_change_count"] = verify_candidate(PdfReader(io.BytesIO(candidate), strict=True), report)
                report["output_sha256"] = sha256(candidate)
                if sha256(read_bounded(source_path)) != report["source_sha256"]:
                    raise RepairError("input changed during candidate writing")
                report["candidate_verified"] = True
            except Exception as exc:
                report["status"] = "verification_failed"
                report["error"] = str(exc)
                raise
            finally:
                report_stream.write(json.dumps(report, ensure_ascii=True, indent=2).encode("utf-8"))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--reference-font", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    try:
        report = run(args.input, args.reference_font, args.output, args.report)
    except Exception as exc:
        print("Refused/failed (any partials retained): " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "scope": SCOPE, "changes": len(report["changes"])}))
    return 0 if report.get("verified_change_count", 0) else 2


if __name__ == "__main__":
    raise SystemExit(main())
