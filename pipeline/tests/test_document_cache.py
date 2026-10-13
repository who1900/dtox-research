import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pipeline import document_cache as dc


def chunk(text, index=0, title="Method", **fields):
    return dict(text=text, chunk_index=index, section_title=title,
                section_type="method", element_type="prose", **fields)


class DocumentCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = json.dumps([chunk("before\n\nafter")]).encode()
        self.source = b"before middle after"

    def manifest(self, chunks=None, paper_id="fixture"):
        return dc.make_section_manifest(paper_id, chunks or [chunk("before middle after")],
                                        self.cache, self.source, "pylatexenc", "rev-4")

    def test_source_order_is_not_uuid_or_chunk_index_order(self):
        chunks = [chunk("before", 90), chunk("middle", 200), chunk("after", 0)]
        result = self.manifest(chunks)
        self.assertEqual(result["sections"][0]["text"], "before\n\nmiddle\n\nafter")
        self.assertEqual(json.loads(self.cache)[0]["text"], "before\n\nafter")
        self.assertEqual(result["coverage"], "parsed_source")

    def test_section_title_uses_bounded_schema_limit_not_512_character_cutoff(self):
        title = "A long source heading " * 30
        chunks = [chunk("body", title=title)]
        cache_bytes = json.dumps(chunks).encode()

        manifest = dc.make_section_manifest(
            "fixture", chunks, cache_bytes, b"body", "pylatexenc", "rev-4")

        self.assertEqual(manifest["sections"][0]["section_title"], title)
        dc.validate_section_manifest(manifest, "fixture", cache_bytes, b"body")

    def test_repeated_split_parts_rejected_without_overlap_guess(self):
        for kind in ("prose", "code"):
            parts = [dict(chunk("A" * 1700, part_index=i, part_total=2), element_type=kind) for i in range(2)]
            with self.subTest(kind=kind), self.assertRaises(dc.ManifestInvalid):
                self.manifest(parts)
        text = "A" * 3145
        self.assertEqual(self.manifest([chunk(text)])["sections"][0]["text"], text)

    def test_single_protected_block_preserves_whitespace(self):
        text = "    indented\n\tcode\n  "
        result = self.manifest([dict(chunk(text), element_type="code")])
        self.assertEqual(result["sections"][0]["text"], text)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO required")
    def test_fifo_open_without_writer_is_nonblocking_and_rejected(self):
        path = self.root / "fifo"
        os.mkfifo(path)
        code = ("import sys; from pipeline.document_cache import read_bounded, ManifestInvalid\n"
                "try: read_bounded(sys.argv[1])\nexcept ManifestInvalid: sys.exit(0)\n"
                "sys.exit(1)")
        result = subprocess.run([sys.executable, "-c", code, str(path)], timeout=2, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_normalize_lf_and_preserve_section_order(self):
        result = self.manifest([chunk("one\r\ntwo", title="Z"), chunk("three", title="A")])
        self.assertEqual([s["section_title"] for s in result["sections"]], ["Z", "A"])
        self.assertEqual(result["sections"][0]["text"], "one\ntwo")

    def test_safe_id_matches_pipeline_colon_slash_translation(self):
        self.assertEqual(dc.safe_id("pmlr:v1/paper"), "pmlr_v1_paper")
        for value in ("../x", "x/../a", "C:/a", "CON", "x\\y", "x//a", "x/"):
            with self.subTest(value=value), self.assertRaises(dc.ManifestInvalid):
                dc.safe_id(value)

    def test_safe_id_preserves_known_acl_lrec_url_as_historical_cache_name(self):
        paper_id = "acl:https://lrec.elra.info/lrec2026-main-001"
        self.assertEqual(dc.safe_id(paper_id), "acl_https___lrec.elra.info_lrec2026-main-001")
        workshop_id = "acl:https://lrec.elra.info/lrec2026-ws-bucc-01"
        self.assertEqual(dc.safe_id(workshop_id), "acl_https___lrec.elra.info_lrec2026-ws-bucc-01")

    def test_safe_id_rejects_other_acl_urls_and_path_traversal(self):
        values = (
            "acl:https://example.com/lrec2026-main-001",
            "acl:https://lrec.elra.info.evil.test/lrec2026-main-001",
            "acl:https://user@lrec.elra.info/lrec2026-main-001",
            "acl:https://lrec.elra.info:443/lrec2026-main-001",
            "acl:https://lrec.elra.info/a/../lrec2026-main-001",
            "acl:https://lrec.elra.info/lrec2026-main-001?source=other",
            "acl:https://lrec.elra.info/lrec2026-ws-bucc-001",
            "acl:https://lrec.elra.info/lrec2027-ws-bucc-01",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(dc.ManifestInvalid):
                dc.safe_id(value)

    def test_atomic_publish_keeps_list_cache_bytes_unchanged(self):
        cache_path, target = dc.document_paths(self.root, "fixture")
        cache_path.parent.mkdir()
        cache_path.write_bytes(self.cache)
        dc.publish_section_manifest(target, self.manifest())
        self.assertEqual(dc.load_section_manifest(self.root, "fixture"), self.manifest())
        self.assertEqual(cache_path.read_bytes(), self.cache)
        self.assertEqual(list(target.parent.glob(".section-*")), [])

    def test_explicit_physical_cache_directories_load_validated_pair(self):
        cache_dir = self.root / "migrated" / "chunks"
        section_dir = self.root / "migrated" / "sections"
        cache_dir.mkdir(parents=True)
        section_dir.mkdir()
        cache_path, manifest_path = dc.document_paths(
            self.root, "fixture", cache_dir=cache_dir, section_cache_dir=section_dir)
        cache_path.write_bytes(self.cache)
        manifest_path.write_bytes(dc.manifest_bytes(self.manifest()))

        self.assertEqual(dc.load_section_manifest(
            self.root, "fixture", cache_dir=cache_dir, section_cache_dir=section_dir), self.manifest())
        cache_path.write_bytes(b"[]")
        with self.assertRaisesRegex(dc.ManifestInvalid, "hash mismatch"):
            dc.load_section_manifest(
                self.root, "fixture", cache_dir=cache_dir, section_cache_dir=section_dir)

    def test_default_cache_path_still_rejects_symlinked_directory(self):
        cache_dir = self.root / "physical-chunks"
        cache_dir.mkdir()
        configured = self.root / "chunk_cache"
        try:
            configured.symlink_to(cache_dir, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaisesRegex(dc.ManifestInvalid, "symlinked document path"):
            dc.document_paths(self.root, "fixture")

    def test_hash_and_identity_validation(self):
        manifest = self.manifest()
        for changes in ({"schema": 2}, {"paper_id": "other"}, {"coverage": "complete"},
                        {"parser_mode": "fallback"}, {"cache_sha256": "0" * 64}):
            bad = dict(manifest, **changes)
            with self.subTest(changes=changes), self.assertRaises(dc.ManifestInvalid):
                dc.validate_section_manifest(bad, "fixture", self.cache, self.source)

    def test_nonsequential_section_order_rejected(self):
        bad = copy.deepcopy(self.manifest())
        bad["sections"][0]["source_order"] = 3
        with self.assertRaises(dc.ManifestInvalid):
            dc.validate_section_manifest(bad, "fixture")

    def test_byte_bound_and_symlink_guard(self):
        path = self.root / "small"
        path.write_bytes(b"1234")
        with self.assertRaises(dc.ManifestInvalid):
            dc.read_bounded(path, limit=3)
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(dc.ManifestInvalid):
            dc.read_bounded(path)

    def test_replace_failure_keeps_old_manifest(self):
        _, target = dc.document_paths(self.root, "fixture")
        old = self.manifest()
        dc.publish_section_manifest(target, old)
        with patch.object(dc.os, "replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                dc.publish_section_manifest(target, self.manifest([chunk("new")]))
        self.assertEqual(json.loads(target.read_bytes()), old)
        self.assertEqual(list(target.parent.glob(".section-*")), [])
