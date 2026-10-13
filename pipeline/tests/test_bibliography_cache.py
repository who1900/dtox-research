"""Offline AST fixtures: no service import, network, parser or model dependencies."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from api.tests.test_latex_inputs import DOC, load_service


class BibliographyCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = load_service()
        self.service.LATEX_CACHE_DIR = Path(self.temp.name)
        self.paper = "fixture"
        self.text = DOC % "Текст \\bibliography{refs}"
        self.files = [{"path": "paper/refs.bib", "text": "@article{x, title={引用}}\n"}]
        self.source = self.service.latex_cache_path(self.paper)
        self.sidecar = self.source.with_name("references.json")

    def write(self, files=None):
        self.service.write_latex_cache(self.paper, self.text, self.files if files is None else files)

    def signed(self, manifest):
        manifest.pop("manifest_sha256", None)
        manifest["manifest_sha256"] = hashlib.sha256(self.service._bibliography_json(manifest)).hexdigest()
        return self.service._bibliography_json(manifest)

    def test_utf8_round_trip_hashes_and_no_source_pollution(self):
        self.write()
        manifest = json.loads(self.sidecar.read_bytes())
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(self.signed(dict(manifest)), self.sidecar.read_bytes())
        self.assertIn("引用".encode(), self.sidecar.read_bytes())
        self.assertEqual(self.service.read_latex_bibliography(self.paper), self.files)
        self.assertEqual(self.service.read_latex_cache(self.paper), self.text)
        self.assertTrue(self.source.read_text(encoding="utf-8").endswith("\\end{document}\n"))

    def test_two_argument_call_missing_binding_and_explicit_empty(self):
        self.service.write_latex_cache(self.paper, self.text)
        self.assertFalse(self.sidecar.exists())
        self.assertEqual(self.service.read_latex_bibliography(self.paper), [])
        self.write()
        user_file = self.source.with_name("user.bib")
        user_file.write_bytes(b"user-owned")
        self.write([])
        self.assertEqual(json.loads(self.sidecar.read_bytes())["files"], [])
        self.assertEqual(self.service.read_latex_bibliography(self.paper), [])
        self.assertEqual(user_file.read_bytes(), b"user-owned")

    def test_changed_or_missing_source_and_manifest_tamper_return_empty(self):
        self.write()
        self.service.write_latex_cache(self.paper, "different source")
        self.assertEqual(self.service.read_latex_bibliography(self.paper), [])
        self.write()
        manifest = json.loads(self.sidecar.read_bytes())
        manifest["files"][0]["text"] = "tampered"
        self.sidecar.write_bytes(json.dumps(manifest).encode())
        self.assertEqual(self.service.read_latex_bibliography(self.paper), [])
        self.write()
        self.source.unlink()
        self.assertEqual(self.service.read_latex_bibliography(self.paper), [])

    def test_malformed_utf8_duplicate_json_keys_and_file_paths(self):
        self.write()
        original = self.sidecar.read_bytes()
        for raw in (b"{", b"\xff", b"[]", original.replace(b'"version":1', b'"version":1,"version":1')):
            with self.subTest(raw=raw[:30]):
                self.sidecar.write_bytes(raw)
                self.assertEqual(self.service.read_latex_bibliography(self.paper), [])
        manifest = json.loads(original)
        for changes in ({"version": True}, {"version": 2}, {"source_sha256": "0" * 64},
                        {"files": self.files * 2}, {"files": [{"path": "../refs.bib", "text": "x"}]},
                        {"files": [{"path": "refs.bib", "text": 3}]}):
            with self.subTest(changes=changes):
                self.sidecar.write_bytes(self.signed(dict(manifest, **changes)))
                self.assertEqual(self.service.read_latex_bibliography(self.paper), [])

    def test_writer_rejects_duplicates_and_unsafe_paths_before_source_write(self):
        for files in (self.files * 2, [{"path": "../refs.bib", "text": "x"}],
                      [{"path": "/refs.bib", "text": "x"}], [{"path": "C:/refs.bib", "text": "x"}],
                      [{"path": "./refs.bib", "text": "x"}], [{"path": "refs.tex", "text": "x"}]):
            with self.subTest(files=files), self.assertRaises(ValueError):
                self.write(files)
            self.assertFalse(self.source.exists())

    def test_file_total_count_and_json_limits_are_byte_bounded(self):
        self.write()
        for name, limit in (("LATEX_BIB_MAX_FILES", 0), ("LATEX_BIB_MAX_FILE_BYTES", 1),
                            ("LATEX_BIB_MAX_TOTAL_BYTES", 1), ("LATEX_BIB_MAX_JSON_BYTES", 10)):
            with self.subTest(name=name), patch.object(self.service, name, limit):
                self.assertEqual(self.service.read_latex_bibliography(self.paper), [])
                with self.assertRaises(ValueError):
                    self.write()
        with patch.object(self.service, "LATEX_BIB_MAX_FILE_BYTES", 2), self.assertRaises(ValueError):
            self.write([{"path": "refs.bib", "text": "引"}])

    def test_sidecar_atomic_replace_and_failure_cleanup(self):
        self.write()
        original = self.sidecar.read_bytes()
        real_replace = self.service.os.replace

        def observe(temporary, target):
            if Path(target) == self.sidecar:
                self.assertEqual(self.sidecar.read_bytes(), original)
                self.assertEqual(json.loads(Path(temporary).read_bytes())["files"], [])
            else:
                self.assertEqual(Path(target), self.source)
                self.assertEqual(json.loads(self.sidecar.read_bytes())["files"], [])
                self.assertEqual(Path(temporary).read_bytes(), self.source.read_bytes())
            return real_replace(temporary, target)

        with patch.object(self.service.os, "replace", side_effect=observe) as replace:
            self.write([])
            self.assertEqual(replace.call_count, 2)
        self.assertEqual(list(self.source.parent.glob(".*.tmp")), [])
        before = self.sidecar.read_bytes()
        with patch.object(self.service.os, "replace", side_effect=OSError("fixture")), self.assertRaises(OSError):
            self.write()
        self.assertEqual(self.sidecar.read_bytes(), before)
        self.assertEqual(list(self.source.parent.glob(".*.tmp")), [])

    def test_serialization_failures_do_not_mutate_cache(self):
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                real_json = self.service._bibliography_json
                count = 0

                def fail(value):
                    nonlocal count
                    count += 1
                    if count == fail_at:
                        raise ValueError("serialization fixture")
                    return real_json(value)

                with patch.object(self.service, "_bibliography_json", side_effect=fail), self.assertRaises(ValueError):
                    self.write()
                self.assertFalse(self.source.parent.exists())
        self.write()
        before = self.source.read_bytes(), self.sidecar.read_bytes()
        with patch.object(self.service, "_bibliography_json", side_effect=ValueError("fixture")), self.assertRaises(ValueError):
            self.service.write_latex_cache(self.paper, "replacement", [])
        self.assertEqual((self.source.read_bytes(), self.sidecar.read_bytes()), before)

    def test_first_publish_failure_is_not_a_cache_hit(self):
        with patch.object(self.service.os, "replace", side_effect=OSError("fixture")), self.assertRaises(OSError):
            self.write()
        self.assertIsNone(self.service.read_latex_cache(self.paper))
        self.assertEqual(self.service.read_latex_bibliography(self.paper), [])
        self.assertFalse(self.sidecar.exists())
        self.assertEqual(list(self.source.parent.glob(".*.tmp")), [])

    def test_source_publish_failure_keeps_previous_source_or_no_cache(self):
        real_replace = self.service.os.replace

        def fail_source(temporary, target):
            if Path(target) == self.source:
                raise OSError("source publish fixture")
            return real_replace(temporary, target)

        with patch.object(self.service.os, "replace", side_effect=fail_source), self.assertRaises(OSError):
            self.write()
        self.assertIsNone(self.service.read_latex_cache(self.paper))
        self.assertTrue(self.sidecar.exists())
        self.assertEqual(self.service.read_latex_bibliography(self.paper), [])
        self.write()
        before = self.source.read_bytes()
        with patch.object(self.service.os, "replace", side_effect=fail_source), self.assertRaises(OSError):
            self.service.write_latex_cache(self.paper, "replacement", [])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.service.read_latex_bibliography(self.paper), [])
        self.assertEqual(list(self.source.parent.glob(".*.tmp")), [])

    def test_two_argument_atomic_write_preserves_legacy_bytes(self):
        text = "LF\nCRLF\r\nЮникод\ud800\n"
        expected = Path(self.temp.name) / "legacy.tex"
        expected.write_text(text, encoding="utf-8", errors="ignore")
        self.service.write_latex_cache(self.paper, text)
        self.assertEqual(self.source.read_bytes(), expected.read_bytes())
        with patch.object(self.service.os, "replace", side_effect=OSError("fixture")), self.assertRaises(OSError):
            self.service.write_latex_cache(self.paper, "replacement")
        self.assertEqual(self.source.read_bytes(), expected.read_bytes())

    def fetch_archive(self, members):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, text in members:
                data = text.encode("utf-8")
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        self.service.arxiv_gate = Mock()
        self.service.ARXIV_CONTACT_UA = "offline-fixture"
        self.service.FULLTEXT_MAX_RETRIES = 1
        session = Mock()
        session.get.return_value = types.SimpleNamespace(status_code=200, content=buffer.getvalue())
        return self.service.fetch_latex_source(self.paper, session=session)

    def test_fetch_retains_only_referenced_artifacts_and_never_redownloads_cache(self):
        result = self.fetch_archive([("paper/main.tex", self.text), ("paper/refs.bib", self.files[0]["text"]),
                                     ("paper/main.bbl", "COMPILED"), ("unused.bib", "UNUSED")])
        self.assertEqual(result, (self.text, "latex"))
        self.assertEqual([f["path"] for f in self.service.read_latex_bibliography(self.paper)],
                         ["paper/refs.bib", "paper/main.bbl"])
        self.sidecar.unlink()
        session = Mock()
        session.get.side_effect = AssertionError("no implicit fetch")
        self.assertEqual(self.service.read_latex_bibliography(self.paper), [])
        self.assertEqual(self.service.fetch_latex_source(self.paper, session), (self.text, "latex"))
        session.get.assert_not_called()

    def test_archive_member_and_aggregate_bounds(self):
        for name, limit in (("LATEX_BIB_MAX_FILES", 0), ("LATEX_BIB_MAX_FILE_BYTES", 2),
                            ("LATEX_BIB_MAX_TOTAL_BYTES", 2)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                self.service.LATEX_CACHE_DIR = Path(directory)
                with patch.object(self.service, name, limit), self.assertLogs(self.service.log, level="WARNING"):
                    self.assertEqual(self.fetch_archive([("main.tex", self.text), ("refs.bib", "引")]),
                                     (self.text, "latex"))
                    self.assertEqual(self.service.read_latex_bibliography(self.paper), [])


if __name__ == "__main__":
    unittest.main()
