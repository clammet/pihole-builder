"""Check VM input isolation and image transfer/publication integrity."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pihole_builder.config import DEFAULTS
from pihole_builder.macos import publish, source_archive, temporary_vm


class TemporaryVmTests(unittest.TestCase):
    def args(self):
        return SimpleNamespace(vm_name="pihole-build-test", cpus=2, memory=2, disk=20)

    def test_owned_vm_is_deleted_after_success_failure_or_interrupt(self):
        for error in (None, ValueError("build failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), patch("pihole_builder.macos.run", return_value="") as run:
                try:
                    with temporary_vm(self.args(), "aarch64"):
                        if error is not None:
                            raise error
                except (ValueError, KeyboardInterrupt) as raised:
                    self.assertIs(raised, error)
                run.assert_any_call("limactl", "delete", "--force", "--yes", "pihole-build-test")

    def test_failed_start_still_deletes_created_vm(self):
        def invoke(*args, **kwargs):
            if args[1] == "start":
                raise subprocess.CalledProcessError(1, args)
            return ""
        with patch("pihole_builder.macos.run", side_effect=invoke) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                with temporary_vm(self.args(), "aarch64"):
                    self.fail("A VM that failed to start must not run a build")
            run.assert_any_call("limactl", "delete", "--force", "--yes", "pihole-build-test")

    def test_existing_vm_is_never_started_or_deleted(self):
        existing = json.dumps({"name": "pihole-build-test"})
        with patch("pihole_builder.macos.run", return_value=existing) as run:
            with self.assertRaisesRegex(ValueError, "already exists"):
                with temporary_vm(self.args(), "aarch64"):
                    self.fail("An existing VM must not be reused")
            self.assertEqual(run.call_count, 1)

    def test_failed_creation_does_not_claim_ownership(self):
        def invoke(*args, **kwargs):
            if args[1] == "create":
                raise subprocess.CalledProcessError(1, args)
            return ""
        with patch("pihole_builder.macos.run", side_effect=invoke) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                with temporary_vm(self.args(), "aarch64"):
                    self.fail("Creation failed")
            self.assertFalse(any(call.args[1] in ("start", "delete") for call in run.call_args_list))


class TransferTests(unittest.TestCase):
    def fixture(self, root):
        staging = root / "staging"
        staging.mkdir()
        data = b"test disk image"
        digest = hashlib.sha256(data).hexdigest()
        (staging / "image.img").write_bytes(data)
        (staging / "image.img.json").write_text(json.dumps({"image_sha256": digest}))
        (staging / "image.img.sha256").write_text(f"{digest}  image.img\n")
        return staging, digest

    def test_verified_image_is_published_with_correct_checksum_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging, digest = self.fixture(root)
            output = root / "custom name.img"
            publish(staging, output)
            self.assertEqual(output.read_bytes(), b"test disk image")
            self.assertEqual(output.with_suffix(".img.sha256").read_text(),
                             f"{digest}  custom name.img\n")
            self.assertEqual(json.loads(output.with_suffix(".img.json").read_text())["image_sha256"], digest)

    def test_corrupt_transfer_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging, _ = self.fixture(root)
            (staging / "image.img").write_bytes(b"corrupted")
            output = root / "result.img"
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                publish(staging, output)
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".img.json").exists())

    def test_missing_checksum_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging, _ = self.fixture(root)
            (staging / "image.img.sha256").write_text("")
            output = root / "result.img"
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                publish(staging, output)
            self.assertFalse(output.exists())

    def test_existing_output_is_preserved_and_partial_sidecars_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging, _ = self.fixture(root)
            output = root / "result.img"
            output.write_bytes(b"existing image")
            with self.assertRaises(FileExistsError):
                publish(staging, output)
            self.assertEqual(output.read_bytes(), b"existing image")
            self.assertFalse(output.with_suffix(".img.sha256").exists())
            self.assertFalse(output.with_suffix(".img.json").exists())

    def test_source_archive_excludes_unrelated_secrets_and_large_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build.py").write_text("# builder\n")
            for name in ("pihole_builder", "runtime", "tests"):
                (root / name).mkdir()
                (root / name / "source.py").write_text("# source\n")
            (root / "pihole_builder/__pycache__").mkdir()
            (root / "pihole_builder/__pycache__/cached.pyc").write_bytes(b"cache")
            (root / "other-secret.json").write_text("private")
            (root / "dist").mkdir()
            (root / "dist/old.img").write_bytes(b"old image")
            config = copy.deepcopy(DEFAULTS)
            config["admin_password"] = 'literal-$HOME-$(false)-"quoted"'
            archive_path = root / "source.tar"
            with patch("pihole_builder.macos.REPO", root):
                source_archive(archive_path, config)
            with tarfile.open(archive_path) as archive:
                names = archive.getnames()
                self.assertNotIn("other-secret.json", names)
                self.assertNotIn("dist/old.img", names)
                self.assertFalse(any("__pycache__" in name for name in names))
                self.assertTrue(all(not Path(name).is_absolute() for name in names))
                self.assertEqual(json.load(archive.extractfile("config.json")), config)
                self.assertEqual(archive.getmember("config.json").mode, 0o600)
