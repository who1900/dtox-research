import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from ops import transport_cached_originals as transport


class CachedOriginalTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        transport.load_dependencies()

    def test_sender_emits_bounded_body_then_validated_trailer_and_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = b"%PDF-1.7\nexample\n"
            source = root / "one.pdf"
            source.write_bytes(body)
            expected = transport.metadata_from_stat(source.stat())
            digest = hashlib.sha256(body).hexdigest()
            item = {"item_id": "1:0", "relative_path": "one.pdf", "bytes": len(body),
                    "expected_sha256": digest, "declared_metadata": expected}
            ident = dict(transport.SOURCE_PIN)
            cfg = {"source": {"root": str(root), "identity": ident},
                   "stream_id": "a" * 32, "limits": {"wire_bytes": transport.MAX_WIRE},
                   "_config_sha256": "b" * 64, "_peer_config_sha256": "c" * 64,
                   "_module_sha256": "d" * 64}
            output = io.BytesIO()
            with patch.object(transport, "SOURCE_ROOT", str(root)), \
                    patch.object(transport, "source_identity", return_value=ident):
                result = transport.send_all(cfg, {}, [item], output)
            incoming = transport.BoundedInput(io.BytesIO(output.getvalue()), transport.MAX_WIRE)
            header = incoming.frame()
            self.assertEqual(header["item_count"], 1)
            framed = incoming.frame()
            self.assertEqual(framed["item_id"], item["item_id"])
            reader = transport.ValidatedBody(incoming, item, framed)
            got = bytearray()
            while True:
                block = reader.read(5)
                if not block:
                    break
                got.extend(block)
            self.assertEqual(bytes(got), body)
            self.assertTrue(reader.validated)
            self.assertEqual(incoming.frame()["kind"], transport.TERMINAL)
            self.assertEqual(incoming.read(1), b"")
            self.assertEqual(result["logical_bytes"], len(body))

    def test_reader_rejects_bad_trailer_before_exposing_eof(self):
        body = b"abc"
        item = {"item_id": "2:0", "bytes": 3,
                "expected_sha256": hashlib.sha256(body).hexdigest()}
        metadata = {"dev": 1, "ino": 2, "size": 3, "mtime_ns": 4, "ctime_ns": 5}
        trailer = {"kind": transport.TRAILER, "item_id": "2:0", "bytes": 3,
                   "sha256": "0" * 64, "metadata": metadata}
        stream = io.BytesIO()
        stream.write(body)
        transport.write_frame(stream, trailer)
        incoming = transport.BoundedInput(io.BytesIO(stream.getvalue()), transport.MAX_WIRE)
        reader = transport.ValidatedBody(incoming, item, {"metadata": metadata})
        self.assertEqual(reader.read(3), body)
        with self.assertRaises(transport.TransportError):
            reader.read(1)
        self.assertFalse(reader.validated)

    def test_cli_rejects_unbound_module_root_in_subprocess(self):
        limits = {"items": transport.MAX_ITEMS, "file_bytes": transport.MAX_FILE,
                  "total_bytes": transport.MAX_TOTAL, "wire_bytes": transport.MAX_WIRE,
                  "plan_bytes": transport.MAX_PLAN, "json_frame_bytes": transport.MAX_JSON_FRAME,
                  "chunk_bytes": transport.CHUNK, "metadata_bytes": transport.MAX_METADATA,
                  "operational_reserve_bytes": transport.OPERATIONAL_RESERVE,
                  "reserve_bytes": transport.RESERVE}
        cfg = {"schema": transport.SCHEMA, "role": "send", "plan_sha256": transport.PLAN_SHA256,
               "inventory_sha256": transport.INVENTORY_SHA256, "stream_id": "e" * 32,
               "plan_path": "/opt/dtox-source-recovery-20261012/sealed-plan.json", "limits": limits,
               "source": {"root": transport.SOURCE_ROOT, "identity": transport.SOURCE_PIN},
               "target": {"store_root": transport.TARGET_STORE, "identity": transport.TARGET_PIN,
                          "job_root": "/opt/dtox-source-recovery-20261012/direct-stream-jobs",
                          "lock_dir": "/opt/dtox-source-recovery-20261012/original-stream-lock"},
               "other_reserved_bytes": transport.OPERATIONAL_RESERVE,
               "code_sha256": {name: "f" * 64 for name in transport.CODE_FILES}}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base / "config.json"
            raw = json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()
            config.write_bytes(raw)
            wrong_root = base / "wrong-root"
            wrong_root.mkdir()
            module_path = Path(transport.__file__).resolve()
            command = [sys.executable, str(module_path), "send", "--config", str(config),
                       "--config-sha256", hashlib.sha256(raw).hexdigest(),
                       "--module-sha256", hashlib.sha256(module_path.read_bytes()).hexdigest(),
                       "--module-root", str(wrong_root), "--peer-config-sha256", "a" * 64]
            completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=15)
            self.assertEqual(completed.returncode, 1)
            self.assertIn("module root does not match script location", completed.stderr)

    def test_remote_command_binds_module_root_and_isolated_python(self):
        cfg = {"stream_id": "a" * 32, "relay": {
            "remote_module": "/opt/dtox-source-recovery-20261012/original-stream-v1/bundle/ops/transport_cached_originals.py",
            "send_config_sha256": "b" * 64, "receive_config_sha256": "c" * 64}}
        command = transport._remote_command(
            cfg, "send", "/opt/dtox-source-recovery-20261012/send.json", "a" * 32, "d" * 64)
        remote = shlex.split(command[-1])
        self.assertIn("-E", remote)
        self.assertIn("-S", remote)
        self.assertIn("--module-root", remote)
        self.assertIn("/opt/dtox-source-recovery-20261012/original-stream-v1/bundle", remote)
        self.assertIn("StrictHostKeyChecking=yes", command)

    def test_staged_package_markers_and_helpers_match_all_four_pins(self):
        source_root = Path(transport.__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            pins = {}
            for relative in transport.CODE_FILES:
                destination = bundle / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if relative.endswith("__init__.py"):
                    destination.write_bytes(b"")
                    pins[relative] = transport.EMPTY_SHA256
                else:
                    shutil.copyfile(source_root / relative, destination)
                    pins[relative] = hashlib.sha256(destination.read_bytes()).hexdigest()
            transport.verify_code_pins(bundle, pins)

    def test_positive_windows_main_relay_config_and_staged_bundle_pins(self):
        source_root = Path(transport.__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            local_root = Path(directory)
            bundle = local_root / "bundle"
            pins = {}
            for relative in transport.CODE_FILES:
                destination = bundle / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if relative.endswith("__init__.py"):
                    destination.write_bytes(b"")
                    pins[relative] = transport.EMPTY_SHA256
                else:
                    shutil.copyfile(source_root / relative, destination)
                    pins[relative] = hashlib.sha256(destination.read_bytes()).hexdigest()
            config = {"schema": transport.SCHEMA, "role": "relay",
                      "plan_sha256": transport.PLAN_SHA256,
                      "inventory_sha256": transport.INVENTORY_SHA256,
                      "stream_id": "a" * 32,
                      "plan_path": str(local_root / "sealed-plan.json"),
                      "limits": {"items": transport.MAX_ITEMS, "file_bytes": transport.MAX_FILE,
                          "total_bytes": transport.MAX_TOTAL, "wire_bytes": transport.MAX_WIRE,
                          "plan_bytes": transport.MAX_PLAN, "json_frame_bytes": transport.MAX_JSON_FRAME,
                          "chunk_bytes": transport.CHUNK, "metadata_bytes": transport.MAX_METADATA,
                          "operational_reserve_bytes": transport.OPERATIONAL_RESERVE,
                          "reserve_bytes": transport.RESERVE},
                      "source": {"root": transport.SOURCE_ROOT, "identity": transport.SOURCE_PIN},
                      "target": {"store_root": transport.TARGET_STORE, "identity": transport.TARGET_PIN,
                          "job_root": "/opt/dtox-source-recovery-20261012/direct-stream-jobs",
                          "lock_dir": "/opt/dtox-source-recovery-20261012/original-stream-lock"},
                      "other_reserved_bytes": transport.OPERATIONAL_RESERVE,
                      "code_sha256": pins,
                      "relay": {"source_config": "/opt/dtox-source-recovery-20261012/send.json",
                          "destination_config": "/opt/dtox-source-recovery-20261012/receive.json",
                          "remote_module": "/opt/dtox-source-recovery-20261012/original-stream-v1/bundle/ops/transport_cached_originals.py",
                          "local_job_root": str(local_root / "relay-jobs"),
                          "send_config_sha256": "b" * 64, "receive_config_sha256": "c" * 64}}
            main_config = local_root / "mainrelay.json"
            main_config.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
            loaded = json.loads(main_config.read_text(encoding="utf-8"))
            self.assertIs(transport.validate_config(loaded, "relay"), loaded)
            transport.verify_code_pins(bundle, loaded["code_sha256"])

    def _receiver_fixture(self, root, corrupt_second=False, trailing=b""):
        store_root = root / "store"
        store = transport.OriginalStore.create(store_root)
        lock_dir = root / "global-lock"
        lock_dir.mkdir()
        lock_dir.chmod(0o700)
        job_parent = root / "jobs"
        job_parent.mkdir()
        bodies = [b"%PDF-1.7\nfixture\n", b"<html><body>fixture</body></html>"]
        kinds = ["PDF", "HTML"]
        items, blobs = [], {}
        frames = io.BytesIO()
        source_config, destination_config, module_sha = "b" * 64, "c" * 64, "d" * 64
        config = {"schema": transport.SCHEMA, "role": "receive", "stream_id": "a" * 32,
                  "plan_sha256": transport.PLAN_SHA256, "inventory_sha256": transport.INVENTORY_SHA256,
                  "source": {"root": transport.SOURCE_ROOT, "identity": transport.SOURCE_PIN},
                  "target": {"store_root": str(store_root), "identity": transport.TARGET_PIN,
                             "job_root": str(job_parent), "lock_dir": str(lock_dir)},
                  "other_reserved_bytes": transport.OPERATIONAL_RESERVE,
                  "limits": {"wire_bytes": transport.MAX_WIRE, "json_frame_bytes": transport.MAX_JSON_FRAME,
                             "metadata_bytes": transport.MAX_METADATA},
                  "code_sha256": {name: "e" * 64 for name in transport.CODE_FILES},
                  "_config_sha256": destination_config, "_peer_config_sha256": source_config,
                  "_module_sha256": module_sha}
        for index, (body, kind) in enumerate(zip(bodies, kinds), 1):
            digest = hashlib.sha256(body).hexdigest()
            extension = "pdf" if kind == "PDF" else "html"
            provenance = {"canonical_id": f"fixture:{index}", "source_url": f"https://example.org/{index}",
                          "retrieval_url": f"https://example.org/{index}.{extension}",
                          "original_kind": kind, "edition": "unknown", "retrieved_at": "UNKNOWN",
                          "raw_sha256": digest}
            item = {"item_id": f"{index}:0", "relative_path": f"{index}.{extension}",
                    "bytes": len(body), "expected_sha256": digest,
                    "declared_metadata": {"dev": index, "ino": index + 10, "size": len(body),
                                          "mtime_ns": index + 20, "ctime_ns": index + 30},
                    "provenance": provenance, "row_sha256": "f" * 64}
            items.append(item)
            blobs[digest] = {"bytes": len(body)}
        plan = {"blobs": blobs}
        transport.write_frame(frames, {"kind": transport.HEADER, "schema": transport.SCHEMA,
            "stream_id": config["stream_id"], "plan_sha256": transport.PLAN_SHA256,
            "inventory_sha256": transport.INVENTORY_SHA256, "source_config_sha256": source_config,
            "destination_config_sha256": destination_config, "module_sha256": module_sha,
            "source_identity": transport.SOURCE_PIN, "item_count": len(items),
            "logical_bytes": sum(x["bytes"] for x in items)})
        for index, (item, body) in enumerate(zip(items, bodies), 1):
            metadata = item["declared_metadata"]
            transport.write_frame(frames, {"kind": transport.ITEM, "item_id": item["item_id"],
                "relative_path": item["relative_path"], "bytes": item["bytes"],
                "expected_sha256": item["expected_sha256"], "metadata": metadata})
            frames.write(body)
            transport.write_frame(frames, {"kind": transport.TRAILER, "item_id": item["item_id"],
                "bytes": item["bytes"], "sha256": ("0" * 64 if corrupt_second and index == 2
                                                       else item["expected_sha256"]),
                "metadata": metadata})
        transport.write_frame(frames, {"kind": transport.TERMINAL, "schema": transport.SCHEMA,
            "stream_id": config["stream_id"], "plan_sha256": transport.PLAN_SHA256,
            "inventory_sha256": transport.INVENTORY_SHA256, "source_config_sha256": source_config,
            "destination_config_sha256": destination_config, "module_sha256": module_sha,
            "sender_exit": 0, "item_count": len(items), "logical_bytes": sum(x["bytes"] for x in items)})
        transport.write_frame(frames, {"kind": "relay_commit", "schema": transport.SCHEMA,
            "stream_id": config["stream_id"], "plan_sha256": transport.PLAN_SHA256,
            "source_config_sha256": source_config, "destination_config_sha256": destination_config,
            "module_sha256": module_sha, "sender_returncode": 0, "item_count": len(items)})
        frames.write(trailing)
        return store, config, plan, items, frames.getvalue(), job_parent, lock_dir, bodies

    def _root_owned_lock_dir(self, lock_dir):
        """Model the receiver's root-owned 0700 lock directory in isolated tests."""
        lock_dir = Path(lock_dir)
        stack = ExitStack()
        if os.name == "nt":
            return stack
        real_lstat = Path.lstat

        def lstat(path):
            info = real_lstat(path)
            if path == lock_dir:
                values = list(info)
                values[4] = 0
                return os.stat_result(values)
            return info

        stack.enter_context(patch.object(transport.os, "geteuid", return_value=0))
        stack.enter_context(patch.object(Path, "lstat", lstat))
        return stack

    def test_receiver_ingests_two_items_with_batch_lock_and_protected_reserve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, cfg, plan, items, wire, jobs, lock_dir, bodies = self._receiver_fixture(root)
            observed_reserves = []
            def capture_reserve(reserve_bytes, additional_bytes=0):
                observed_reserves.append(reserve_bytes)
            store._stream_space = capture_reserve
            with self._root_owned_lock_dir(lock_dir), \
                 patch.object(transport, "MAX_ITEMS", 2), \
                 patch.object(transport, "TARGET_STORE", str(store.root)), \
                 patch.object(transport, "target_identity", return_value=transport.TARGET_PIN), \
                 patch.object(transport.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10**15, 0, 10**15)):
                receipt = transport.receive_all(cfg, plan, items, io.BytesIO(wire), store,
                                                jobs / cfg["stream_id"])
            self.assertEqual(receipt["status"], "COMPLETE")
            self.assertEqual(receipt["logical_items"], 2)
            self.assertEqual(len(observed_reserves), 6)
            fixed_floor = transport.RESERVE + transport.OPERATIONAL_RESERVE + transport.MAX_METADATA
            self.assertEqual(observed_reserves[0], fixed_floor + len(bodies[1]))
            self.assertEqual(observed_reserves[3], fixed_floor)
            self.assertTrue((lock_dir / "run.lock").is_file())
            self.assertTrue((jobs / cfg["stream_id"] / "events" / "00000002.json").is_file())
            self.assertEqual(len(store.references("fixture:1")), 1)
            self.assertEqual(len(store.references("fixture:2")), 1)

    def test_receiver_late_bad_trailer_preserves_first_and_marks_active_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, cfg, plan, items, wire, jobs, _, _ = self._receiver_fixture(root, corrupt_second=True)
            with self._root_owned_lock_dir(cfg["target"]["lock_dir"]), \
                 patch.object(transport, "MAX_ITEMS", 2), \
                 patch.object(transport, "TARGET_STORE", str(store.root)), \
                 patch.object(transport, "target_identity", return_value=transport.TARGET_PIN), \
                 patch.object(transport.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10**15, 0, 10**15)), \
                 patch.object(store, "_stream_space", return_value=None):
                with self.assertRaises(transport.TransportError):
                    transport.receive_all(cfg, plan, items, io.BytesIO(wire), store,
                                          jobs / cfg["stream_id"])
            job = jobs / cfg["stream_id"]
            failure = json.loads((job / "events" / "00000001.json").read_text(encoding="utf-8"))
            receipt = json.loads((job / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["confirmed_journal_items"], 1)
            self.assertEqual(failure["active_item_id"], "2:0")
            self.assertEqual(failure["active_item_outcome"], "UNKNOWN")
            self.assertEqual(receipt["confirmed_journal_items"], 1)
            self.assertEqual(len(store.references("fixture:1")), 1)
            self.assertEqual(len(store.references("fixture:2")), 0)

    def test_receiver_rejects_trailing_wire_after_commits_without_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, cfg, plan, items, wire, jobs, _, _ = self._receiver_fixture(root, trailing=b"x")
            with self._root_owned_lock_dir(cfg["target"]["lock_dir"]), \
                 patch.object(transport, "MAX_ITEMS", 2), \
                 patch.object(transport, "TARGET_STORE", str(store.root)), \
                 patch.object(transport, "target_identity", return_value=transport.TARGET_PIN), \
                 patch.object(transport.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10**15, 0, 10**15)), \
                 patch.object(store, "_stream_space", return_value=None):
                with self.assertRaises(transport.TransportError):
                    transport.receive_all(cfg, plan, items, io.BytesIO(wire), store,
                                          jobs / cfg["stream_id"])
            journal = jobs / cfg["stream_id"]
            receipt = json.loads((journal / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "FAILED")
            self.assertEqual(receipt["confirmed_journal_items"], 2)
            self.assertEqual(len(store.references("fixture:1")), 1)
            self.assertEqual(len(store.references("fixture:2")), 1)


if __name__ == "__main__":
    unittest.main()
