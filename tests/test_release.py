import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "deploy" / "install.py"
SPEC = importlib.util.spec_from_file_location("manager_install", MODULE)
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class BundleIntegrityTests(unittest.TestCase):
    def test_release_cannot_replace_unresolved_breakout_worker(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(INSTALL, 'PREFIX', Path(folder)):
            directory = Path(folder) / 'state/data/breakout' / ('a' * 32)
            directory.mkdir(parents=True)
            (directory / 'status.json').write_text(json.dumps({'status': 'failed'}))
            (directory / 'manifest.json').write_text(json.dumps({'status': 'staged_pending_reboot'}))
            with self.assertRaises(RuntimeError):
                INSTALL.ensure_no_breakout_in_progress()

    def test_manifest_recovery_failure_overrides_old_success_status(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(INSTALL, 'PREFIX', Path(folder)):
            directory = Path(folder) / 'state/data/breakout' / ('a' * 32)
            directory.mkdir(parents=True)
            (directory / 'status.json').write_text(json.dumps({'status': 'succeeded'}))
            (directory / 'manifest.json').write_text(json.dumps({'status': 'rollback_write_failed'}))
            with self.assertRaises(RuntimeError):
                INSTALL.ensure_no_breakout_in_progress()

    def test_missing_current_operation_status_blocks_release(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(INSTALL, 'PREFIX', Path(folder)):
            directory = Path(folder) / 'state/data/breakout'
            directory.mkdir(parents=True)
            (directory / 'current.json').write_text(json.dumps({'id': 'a' * 32}))
            with self.assertRaises(ValueError):
                INSTALL.ensure_no_breakout_in_progress()

    def fixture(self, root):
        entries = {"server/app.py": b"# runtime\n", "server/auth.py": b"# auth\n",
                   "server/sonic.py": b"# adapter\n", "deploy/launch.py": b"# launch\n",
                   "shared/catalog.json": b"{}", "ui/dist/index.html": b"<!doctype html>"}
        for name, content in entries.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        manifest = {"product": "sonic-web-manager", "version": "0.1.0", "files": {
            name: hashlib.sha256(content).hexdigest() for name, content in entries.items()}}
        (root / "MANIFEST.json").write_text(json.dumps(manifest))
        return manifest

    def test_valid_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = self.fixture(root)
            self.assertEqual(INSTALL.validate_bundle(root), manifest)

    def test_new_release_requires_terminal_runtime_but_old_rollback_remains_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = self.fixture(root)
            manifest["version"] = "0.4.0"
            (root / "MANIFEST.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Incomplete bundle"):
                INSTALL.validate_bundle(root)
            for name in ("server/metrics.py", "server/console.py", "deploy/console_child.py"):
                (root / name).write_bytes(b"# runtime\n")
                manifest["files"][name] = hashlib.sha256(b"# runtime\n").hexdigest()
            (root / "MANIFEST.json").write_text(json.dumps(manifest))
            self.assertEqual(INSTALL.validate_bundle(root), manifest)

    def test_tampered_runtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            (root / "server/app.py").write_text("changed")
            with self.assertRaisesRegex(ValueError, "integrity failure"):
                INSTALL.validate_bundle(root)

    def test_manifest_cannot_refer_outside_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "bundle"
            root.mkdir()
            manifest = self.fixture(root)
            outside = root.parent / "outside"
            outside.write_bytes(b"outside")
            manifest["files"]["../outside"] = hashlib.sha256(b"outside").hexdigest()
            (root / "MANIFEST.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Unsafe bundle path"):
                INSTALL.validate_bundle(root)

    def test_wrong_product_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = self.fixture(root)
            manifest["product"] = "another-application"
            (root / "MANIFEST.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "product mismatch"):
                INSTALL.validate_bundle(root)

    def test_noncanonical_paths_and_invalid_digests_rejected(self):
        for name in ("/server/app.py", "server/./app.py", "server//app.py", "server\\app.py", "C:/app.py"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                manifest = self.fixture(root)
                manifest["files"][name] = "0" * 64
                (root / "MANIFEST.json").write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, "Unsafe bundle path"):
                    INSTALL.validate_bundle(root)

    def test_parent_symlink_cannot_redirect_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            (root / "server").rename(root / "renamed-server")
            try:
                (root / "server").symlink_to(root / "renamed-server", target_is_directory=True)
            except OSError:
                self.skipTest("Symlinks require platform privileges")
            with self.assertRaisesRegex(ValueError, "Unsafe bundle path"):
                INSTALL.validate_bundle(root)

    def test_atomic_write_does_not_follow_destination_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            protected = root / "protected"
            protected.write_text("keep")
            target = root / "service.json"
            try:
                target.symlink_to(protected)
            except OSError:
                self.skipTest("Symlinks require platform privileges")
            with self.assertRaises(ValueError):
                INSTALL.atomic_write(target, "changed")
            self.assertEqual(protected.read_text(), "keep")

    def test_atomic_write_replaces_valid_regular_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "service.json"
            INSTALL.atomic_write(target, '{"port":8443}')
            self.assertEqual(json.loads(target.read_text()), {"port": 8443})
            INSTALL.atomic_write(target, '{"port":8444}')
            self.assertEqual(json.loads(target.read_text()), {"port": 8444})


if __name__ == "__main__":
    unittest.main()
