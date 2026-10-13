"""In-memory tests only: no PDF authoring, fixtures, output, or cleanup."""

import copy
import io
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, IndirectObject, NameObject

from ops import pdf_unicode_repair as repair


def cmap(pairs="<0001> <fffd>\n<0002> <0041>"):
    return ("1 begincodespacerange\n<0000> <ffff>\nendcodespacerange\n"
            + str(len(pairs.splitlines())) + " beginbfchar\n" + pairs + "\nendbfchar\n").encode()


class Glyph:
    def __init__(self, empty=False, shift=0):
        self.empty, self.shift = empty, shift

    def draw(self, pen):
        if not self.empty:
            pen.moveTo((self.shift, 0))
            pen.lineTo((30, 40))
            pen.lineTo((50, 0))
            pen.closePath()


class Font(dict):
    def __init__(self, tables=None, empty=False, shift=0):
        super().__init__(tables or {})
        self.update(head=NS(unitsPerEm=1000), hmtx=NS(metrics={"target": (600, 4)}), glyf=NS())
        self.glyph = Glyph(empty, shift)

    def getGlyphSet(self):
        return {"target": self.glyph}

    def getGlyphOrder(self):
        return ["target"]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def unicode_table(values):
    return NS(isUnicode=lambda: True, cmap=values)


def reference_font(kind=4, extension=False, target="target"):
    direct = {102: "f", 108: "l"}
    sub = NS(ligatures={"f": [NS(LigGlyph=target, Component=["l"])]}) if kind == 4 else NS(mapping={"f": target})
    if extension:
        sub = NS(ExtensionLookupType=kind, ExtSubTable=sub)
    return Font({"cmap": NS(tables=[unicode_table(direct)]),
                 "GSUB": NS(table=NS(LookupList=NS(Lookup=[NS(LookupType=7 if extension else kind, SubTable=[sub])])))} )


def reference_index(font=None):
    font = font or reference_font()
    fp = repair.fingerprint(font, "target")
    match = {"reference_path": "explicit.ttf", "reference_sha256": "reference-hash", "member_index": 0,
             "glyph_id": 0, "glyph_name": "target", "fingerprint": fp,
             "semantic_proofs": repair.semantic_proofs(font).get("target", [])}
    return {"manifest": [], "index": {fp["sha256"]: [match]}}


def stream(data):
    obj = DecodedStreamObject()
    obj.set_data(data)
    return obj


def dictionary(**values):
    result = DictionaryObject()
    for key, value in values.items():
        result[NameObject("/" + key)] = NameObject(value) if isinstance(value, str) else value
    return result


def pdf_font(to_unicode=None):
    descendant = dictionary(Subtype="/CIDFontType2", FontDescriptor=dictionary(FontFile2=stream(b"fake font")))
    return dictionary(Subtype="/Type0", Encoding="/Identity-H", DescendantFonts=ArrayObject([descendant]),
                      ToUnicode=to_unicode if to_unicode is not None else stream(cmap()))


def document(fonts):
    owner = PdfWriter()
    return NS(pages=[dictionary(Resources=dictionary(Font=dictionary(**fonts)))],
              _add_object=Mock(side_effect=owner._add_object))


def test_ligature_other_than_ti_and_extension_proof_source_binding():
    references = reference_index(reference_font(extension=True))
    result = repair.prove_glyph(Font(), 0, references, "embedded", "source")
    assert result["unicode"] == "fl"
    assert result["source_sha256"] == "source"
    assert result["embedded_font_sha256"] == "embedded"
    proof = result["matches"][0]["semantic_proofs"][0]
    assert (proof["lookup_type"], proof["effective_type"], proof["components"]) == (7, 4, ["f", "l"])


@pytest.mark.parametrize("extension", [False, True])
def test_single_substitution_direct_unicode_only(extension):
    font = reference_font(kind=1, extension=extension)
    assert repair.prove_glyph(Font(), 0, reference_index(font), "e", "s")["unicode"] == "f"
    font["GSUB"].table.LookupList.Lookup.append(NS(LookupType=1, SubTable=[NS(mapping={"target": "chain"})]))
    assert "chain" not in repair.semantic_proofs(font)


def test_all_unicode_subtables_must_be_unique():
    font = reference_font()
    font["cmap"].tables.append(unicode_table({70: "f"}))
    assert "f" not in repair.unique_cmap(font)
    assert "target" not in repair.semantic_proofs(font)
    font["cmap"].tables = [unicode_table({102: "f"}), unicode_table({102: "other"})]
    assert repair.unique_cmap(font) == {}


@pytest.mark.parametrize("ambiguity", ["aliases", "conflicting_subtables"])
@pytest.mark.parametrize("kind", [1, 4])
def test_ambiguous_direct_cmap_target_cannot_be_rescued_by_gsub(ambiguity, kind):
    font = reference_font(kind=kind, extension=True)
    if ambiguity == "aliases":
        font["cmap"].tables.append(unicode_table({97: "target", 98: "target"}))
    else:
        font["cmap"].tables.extend([unicode_table({97: "target"}), unicode_table({97: "other"})])
    assert "target" not in repair.unique_cmap(font)
    assert "target" not in repair.semantic_proofs(font)
    with pytest.raises(repair.RepairError, match="ambiguous or unknown Unicode"):
        repair.prove_glyph(Font(), 0, reference_index(font), "embedded", "source")


@pytest.mark.parametrize("kind", [1, 4])
@pytest.mark.parametrize("unknown_source", ["missing", "ambiguous"])
def test_unknown_incoming_gsub_branch_poisons_target(kind, unknown_source):
    font = reference_font(kind=kind, extension=True)
    if unknown_source == "ambiguous":
        font["cmap"].tables.append(unicode_table({97: "unknown", 98: "unknown"}))
    sub = font["GSUB"].table.LookupList.Lookup[0].SubTable[0].ExtSubTable
    if kind == 1:
        sub.mapping["unknown"] = "target"
    else:
        sub.ligatures["unknown"] = [NS(LigGlyph="target", Component=["l"])]
    for direct_target in (False, True):
        if direct_target:
            font["cmap"].tables.append(unicode_table({120: "target"}))
        assert "target" not in repair.semantic_proofs(font)
        with pytest.raises(repair.RepairError, match="ambiguous or unknown Unicode"):
            repair.prove_glyph(Font(), 0, reference_index(font), "e", "s")


@pytest.mark.parametrize("kind", [2, 3])
def test_unsupported_gsub_outputs_poison_known_target(kind):
    font = reference_font(kind=1)
    font["cmap"].tables.append(unicode_table({120: "target"}))
    sub = NS(mapping={"unknown": ["other", "target"]}) if kind == 2 else NS(alternates={"unknown": ["other", "target"]})
    font["GSUB"].table.LookupList.Lookup.append(NS(
        LookupType=7, SubTable=[NS(ExtensionLookupType=kind, ExtSubTable=sub)]))
    assert "target" not in repair.semantic_proofs(font)
    with pytest.raises(repair.RepairError, match="ambiguous or unknown Unicode"):
        repair.prove_glyph(Font(), 0, reference_index(font), "e", "s")


@pytest.mark.parametrize("change", ["unknown", "conflict", "outline", "no_gsub", "empty", "metrics", "upem"])
def test_no_guessing(change):
    embedded, references = Font(), reference_index()
    match = next(iter(references["index"].values()))[0]
    if change == "unknown":
        references["index"] = {}
    elif change == "conflict":
        second = copy.deepcopy(match)
        second["semantic_proofs"] = [{"unicode": "ff"}]
        references["index"][match["fingerprint"]["sha256"]].append(second)
    elif change == "outline":
        embedded.glyph.shift = 1
    elif change == "no_gsub":
        match["semantic_proofs"] = []
    elif change == "empty":
        embedded.glyph.empty = True
    elif change == "metrics":
        embedded["hmtx"].metrics["target"] = (601, 4)
    else:
        embedded["head"].unitsPerEm = 2048
    with pytest.raises(repair.RepairError):
        repair.prove_glyph(embedded, 0, references, "e", "s")


def test_one_unknown_matching_glyph_blocks_known_match():
    references = reference_index()
    matches = next(iter(references["index"].values()))
    unknown = copy.deepcopy(matches[0])
    unknown["semantic_proofs"] = []
    matches.append(unknown)
    with pytest.raises(repair.RepairError, match="unknown"):
        repair.prove_glyph(Font(), 0, references, "e", "s")


def test_one_reference_with_conflicting_semantic_derivations_rejected():
    references = reference_index()
    match = next(iter(references["index"].values()))[0]
    match["semantic_proofs"].append({"unicode": "ff"})
    with pytest.raises(repair.RepairError, match="ambiguous"):
        repair.prove_glyph(Font(), 0, references, "e", "s")


def test_composite_outlines_are_decomposed():
    class Composite:
        def draw(self, pen):
            pen.addComponent("base", (1, 0, 0, 1, 0, 0))
    font = Font()
    composite_set = {"target": Composite(), "base": Glyph()}
    assert repair.fingerprint(font, "target", composite_set) == repair.fingerprint(font, "target")


def test_ttc_enumerates_every_member_and_hashes_container():
    fonts = [reference_font(kind=1), reference_font(kind=1)]
    data = b"ttcf synthetic container"
    with patch.object(repair, "TTCollection", return_value=NS(fonts=fonts)):
        references = repair.build_references([("explicit.ttc", data)])
    assert [item["member_index"] for item in references["manifest"]] == [0, 1]
    assert {item["sha256"] for item in references["manifest"]} == {repair.sha256(data)}
    assert len(repair.prove_glyph(Font(), 0, references, "e", "s")["matches"]) == 2


def test_only_exact_rhs_replaced_and_valid_mapping_preserved():
    before = b"% literal <fffd> must stay\n" + cmap()
    after = repair.replace_mappings(before, {1: "fl"})
    assert after == before.replace(b"<0001> <fffd>", b"<0001> <0066006c>")
    assert repair.parse_cmap(after)[1]["text"] == "A"
    with pytest.raises(repair.RepairError):
        repair.replace_mappings(before, {2: "B"})


@pytest.mark.parametrize("text,invalid", [("\x00", True), ("\x01", True), ("\x1f", True), ("\ufffd", True),
                                         ("\t\n\r", False), ("normal", False)])
def test_forbidden_characters(text, invalid):
    assert repair.bad_unicode(text) is invalid
    if invalid:
        with pytest.raises(repair.RepairError):
            repair.replace_mappings(cmap(), {1: text})


@pytest.mark.parametrize("data", [cmap("<01> <fffd>"), cmap("<0001> <d800>"),
                                cmap("<0001> <fffd>\n<0001> <0041>"),
                                cmap().replace(b"<0000> <ffff>", b"<0000> <0000>"),
                                cmap() + b"1 beginbfrange <0003> <0005> <0041> endbfrange",
                                cmap() + b"/Other usecmap", cmap().replace(b"2 beginbfchar", b"3 beginbfchar")])
def test_cmap_rejects_unsupported_or_invalid(data):
    with pytest.raises(repair.RepairError):
        repair.parse_cmap(data)


def test_cid_map_bounds_identity_and_binary():
    assert repair.cid_to_gid(9, {}) == 9
    assert repair.cid_to_gid(9, {"/CIDToGIDMap": "/Identity"}) == 9
    assert repair.cid_to_gid(1, dictionary(CIDToGIDMap=stream(b"\0\0\0\x07"))) == 7
    for cid, data in [(2, b"\0\0\0\x07"), (0, b"\0"), (-1, b"\0\0")]:
        with pytest.raises(repair.RepairError):
            repair.cid_to_gid(cid, dictionary(CIDToGIDMap=stream(data)))


def test_nested_shared_resources_and_cycles_visit_font_per_page():
    font = pdf_font()
    nested = dictionary(Font=dictionary(F=font))
    form = dictionary(Subtype="/Form", Resources=nested)
    nested[NameObject("/XObject")] = dictionary(Loop=form)
    root = dictionary(XObject=dictionary(A=form, B=form))
    doc = NS(pages=[dictionary(Resources=root), dictionary(Resources=root)])
    collected = repair.collect_fonts(doc)
    assert len(collected) == 1
    assert [loc["page"] for loc in collected[0][1]] == [1, 2]
    assert "/XObject//A" in collected[0][1][0]["path"]


def test_isolates_shared_tounicode_without_changing_valid_font():
    shared = stream(cmap())
    first, second = pdf_font(shared), pdf_font(shared)
    second[NameObject("/Encoding")] = NameObject("/Unsupported")
    doc = document({"One": first, "Two": second})
    first["/DescendantFonts"][0][NameObject("/CIDToGIDMap")] = stream(b"\0\0\0\0")
    with patch.object(repair, "TTFont", return_value=Font()):
        report = repair.repair_document(doc, reference_index(), "bound-source")
    assert report["status"] == "partial"
    assert report["scope"] == "supported_explicit_tounicode_mappings_only"
    assert len(report["changes"]) == 1
    assert second["/ToUnicode"] is shared
    assert shared.get_data() == cmap()
    assert first["/ToUnicode"] is not shared
    assert repair.verify_candidate(doc, report) == 1
    report["source_sha256"] = "different-source"
    with pytest.raises(repair.RepairError, match="proof/source"):
        repair.verify_candidate(doc, report)


def test_valid_font_is_unchanged_and_no_changes_never_verify():
    font = pdf_font(stream(cmap("<0001> <0041>")))
    original = font["/ToUnicode"]
    report = repair.repair_document(document({"F": font}), reference_index(), "s")
    assert report["status"] == "no_verified_changes"
    assert font["/ToUnicode"] is original
    with pytest.raises(repair.RepairError, match="no verified"):
        repair.verify_candidate(document({"F": font}), report)


def test_changed_candidate_stream_is_refused():
    font = pdf_font()
    font["/DescendantFonts"][0][NameObject("/CIDToGIDMap")] = stream(b"\0\0\0\0")
    doc = document({"F": font})
    with patch.object(repair, "TTFont", return_value=Font()):
        report = repair.repair_document(doc, reference_index(), "source")
    font["/ToUnicode"].set_data(cmap("<0001> <0066>\n<0002> <0041>"))
    with pytest.raises(repair.RepairError, match="hash mismatch"):
        repair.verify_candidate(doc, report)


def test_changed_tounicode_direct_stream_is_refused():
    font = pdf_font()
    font["/DescendantFonts"][0][NameObject("/CIDToGIDMap")] = stream(b"\0\0\0\0")
    doc = document({"F": font})
    with patch.object(repair, "TTFont", return_value=Font()):
        report = repair.repair_document(doc, reference_index(), "source")
    assert isinstance(font.raw_get("/ToUnicode"), IndirectObject)
    font[NameObject("/ToUnicode")] = font["/ToUnicode"]
    with pytest.raises(repair.RepairError, match="must be indirect"):
        repair.verify_candidate(doc, report)


def test_writer_tounicode_indirection_survives_in_memory_roundtrip():
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    shared = writer._add_object(stream(cmap()))
    first, second = pdf_font(shared), pdf_font(shared)
    second[NameObject("/Encoding")] = NameObject("/Unsupported")
    for font in (first, second):
        descendant = font["/DescendantFonts"][0]
        descriptor = descendant["/FontDescriptor"]
        descriptor[NameObject("/FontFile2")] = writer._add_object(descriptor["/FontFile2"])
        descendant[NameObject("/CIDToGIDMap")] = writer._add_object(stream(b"\0\0\0\0"))
    page[NameObject("/Resources")] = dictionary(Font=dictionary(
        F=writer._add_object(first), G=writer._add_object(second)))
    with patch.object(repair, "TTFont", return_value=Font()):
        report = repair.repair_document(writer, reference_index(), "source")
    assert len(report["changes"]) == 1
    assert isinstance(first.raw_get("/ToUnicode"), IndirectObject)
    assert first.raw_get("/ToUnicode").pdf is writer
    assert second.raw_get("/ToUnicode") == shared
    memory = io.BytesIO()
    writer.write(memory)
    reopened = PdfReader(io.BytesIO(memory.getvalue()), strict=True)
    reopened_fonts = reopened.pages[0]["/Resources"]["/Font"]
    assert isinstance(reopened_fonts["/F"].raw_get("/ToUnicode"), IndirectObject)
    assert reopened_fonts["/G"]["/ToUnicode"].get_data() == cmap()
    assert {e["cid"]: e["text"] for e in repair.parse_cmap(
        reopened_fonts["/F"]["/ToUnicode"].get_data())} == {1: "fl", 2: "A"}
    assert repair.verify_candidate(reopened, report) == 1


def test_missing_tounicode_recorded_not_inferred():
    font = dictionary(Subtype="/Type1")
    report = repair.repair_document(document({"F": font}), reference_index(), "s")
    assert report["unresolved"] and "missing ToUnicode" in report["unresolved"][0]["reason"]
    assert not report["changes"]


def fake_paths(existing_output=False, symlink=False, hardlink=False):
    root = Path.cwd()
    source, reference, output, receipt = [root / name for name in ("fake-input.pdf", "fake-reference.ttf", "fake-output.pdf", "fake-report.json")]
    exists = lambda path: path in (source, reference) or (existing_output and path == output)
    return (source, reference, output, receipt), [
        patch.object(Path, "exists", exists), patch.object(Path, "is_file", lambda p: exists(p)),
        patch.object(Path, "is_dir", lambda p: True), patch.object(Path, "stat", lambda *a, **k: NS(st_size=10)),
        patch.object(Path, "is_symlink", lambda p: symlink and p == output),
        patch.object(Path, "is_junction", lambda p: False), patch.object(Path, "resolve", lambda p: p),
        patch.object(repair.os.path, "lexists", exists), patch.object(repair.os.path, "samefile", lambda a, b: hardlink),
    ]


@pytest.mark.parametrize("mode", ["existing", "alias", "symlink", "hardlink"])
def test_path_refusal_without_filesystem_writes(mode):
    from contextlib import ExitStack
    paths, patches = fake_paths(existing_output=mode == "existing", symlink=mode == "symlink", hardlink=mode == "hardlink")
    source, reference, output, receipt = paths
    if mode == "alias":
        output = source
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        with pytest.raises(repair.RepairError):
            repair.validate_paths(source, [reference], output, receipt)


def test_exclusive_writes_use_excl_and_never_delete_partials():
    with patch.object(repair.os, "open", side_effect=FileExistsError) as opened:
        with pytest.raises(FileExistsError):
            repair.exclusive("nonexistent-placeholder")
    assert opened.call_args.args[1] & repair.os.O_EXCL
