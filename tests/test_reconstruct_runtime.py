from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reconstruct_runtime", ROOT / "scripts/reconstruct_runtime.py")
assert SPEC and SPEC.loader
RUNTIME = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNTIME
SPEC.loader.exec_module(RUNTIME)


class RuntimeReconstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("RUNTIME_RECONSTRUCTION_TEST_ROOT"))
        self.addCleanup(self.temporary.cleanup)
        self.checkout = Path(self.temporary.name) / "checkout"
        self.checkout.mkdir()
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Example Operator")
        self.run_git("config", "user.email", "operator@example.invalid")
        (self.checkout / ".gitignore").write_text("scratch/\n", encoding="utf-8")
        (self.checkout / "runtime.txt").write_text("upstream\n", encoding="utf-8")
        (self.checkout / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        (self.checkout / "pnpm-workspace.yaml").write_text("packages: []\n", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("commit", "-qm", "Fixture upstream")
        self.run_git("tag", "-a", "v2026.9.3", "-m", "Fixture tag")
        base = self.run_git("rev-parse", "HEAD")
        base_tree = self.run_git("rev-parse", "HEAD^{tree}")
        (self.checkout / "runtime.txt").write_text("reference\n", encoding="utf-8")
        self.run_git("add", "runtime.txt")
        normalized_tree = self.run_git("write-tree")
        raw = self.run_git_bytes("diff", "--cached", "--binary", "--full-index")
        self.run_git("restore", "--staged", "--worktree", "--source=HEAD", "--", ".")
        self.package = RUNTIME.SourcePackage(
            "v2026.9.3", self.run_git("rev-parse", "refs/tags/v2026.9.3"),
            base, base_tree, normalized_tree, hashlib.sha256(raw).hexdigest(), raw,
        )

    def run_git_bytes(self, *args: str) -> bytes:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
            cwd=self.checkout, env=env, stderr=subprocess.PIPE,
        )

    def run_git(self, *args: str) -> str:
        return self.run_git_bytes(*args).decode("utf-8").strip()

    def native_artifact(self):
        contents = b"a small native archive fixture\n"
        name = "codex-darwin-arm64-fixture.tgz"
        artifact = RUNTIME.NativeArtifact(
            name, "https://github.com/josephbergvinson/openclaw-control-plane/releases/download/fixture/" + name,
            len(contents), hashlib.sha256(contents).hexdigest(), hashlib.sha512(contents).hexdigest(),
        )
        return artifact, contents

    def native_package(self):
        RUNTIME.reconstruct(self.checkout, self.package, apply=True)
        (self.checkout / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\nnative: true\n")
        (self.checkout / "pnpm-workspace.yaml").write_text("packages: []\nnative: true\n")
        raw = self.run_git_bytes("diff", "--binary", "--full-index")
        self.run_git("add", "pnpm-lock.yaml", "pnpm-workspace.yaml")
        tree = self.run_git("write-tree")
        self.run_git("restore", "--staged", "--worktree", "--source=HEAD", "--", ".")
        artifact, contents = self.native_artifact()
        native = RUNTIME.NativeDistribution(
            self.package.normalized_tree, tree, hashlib.sha256(raw).hexdigest(), raw, artifact,
        )
        return replace(self.package, native=native), contents

    def test_check_leaves_source_and_index_at_upstream(self) -> None:
        result = RUNTIME.reconstruct(self.checkout, self.package)
        self.assertEqual("checked-without-applying", result["status"])
        self.assertFalse(result["normalizedTreeVerified"])
        self.assertEqual("", self.run_git("status", "--porcelain"))
        self.assertEqual(self.package.base_tree, self.run_git("write-tree"))
        self.assertEqual("upstream\n", (self.checkout / "runtime.txt").read_text())

    def test_apply_stages_exact_tree_without_committing_and_refuses_repeat(self) -> None:
        result = RUNTIME.reconstruct(self.checkout, self.package, apply=True)
        self.assertEqual("applied-and-staged", result["status"])
        self.assertTrue(result["normalizedTreeVerified"])
        self.assertEqual(self.package.normalized_tree, self.run_git("write-tree"))
        self.assertEqual(self.package.base_commit, self.run_git("rev-parse", "HEAD"))
        self.assertEqual("reference\n", (self.checkout / "runtime.txt").read_text())
        with self.assertRaisesRegex(RUNTIME.ReconstructionError, "must be clean"):
            RUNTIME.reconstruct(self.checkout, self.package, apply=True)

    def test_wrong_package_bindings_refuse_before_mutation(self) -> None:
        for field, value in (
            ("patch", self.package.patch + b"corrupt"),
            ("base_commit", "0" * 40),
            ("base_tree", "0" * 40),
            ("tag_object", "0" * 40),
        ):
            with self.subTest(field=field):
                with self.assertRaises(RUNTIME.ReconstructionError):
                    RUNTIME.reconstruct(self.checkout, replace(self.package, **{field: value}), apply=True)
                self.assertEqual("", self.run_git("status", "--porcelain"))
                self.assertEqual(self.package.base_tree, self.run_git("write-tree"))

    def test_untracked_ignored_and_unstaged_files_are_all_dirty(self) -> None:
        paths = [self.checkout / "untracked.txt", self.checkout / "scratch/ignored.txt", self.checkout / "runtime.txt"]
        for path in paths:
            with self.subTest(path=path.name):
                path.parent.mkdir(parents=True, exist_ok=True)
                original = path.read_bytes() if path.exists() else None
                path.write_text("operator work\n", encoding="utf-8")
                with self.assertRaisesRegex(RUNTIME.ReconstructionError, "must be clean"):
                    RUNTIME.reconstruct(self.checkout, self.package, apply=True)
                self.assertEqual("operator work\n", path.read_text())
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_shared_object_store_and_nested_path_are_rejected(self) -> None:
        alternate = self.checkout / ".git/objects/info/alternates"
        alternate.write_text("/nonexistent/shared-objects\n", encoding="utf-8")
        with self.assertRaisesRegex(RUNTIME.ReconstructionError, "Shared object stores"):
            RUNTIME.reconstruct(self.checkout, self.package, apply=True)
        alternate.unlink()
        nested = self.checkout / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(RUNTIME.ReconstructionError, "standalone clone"):
            RUNTIME.reconstruct(nested, self.package, apply=True)

    def test_inherited_git_redirection_cannot_change_the_destination(self) -> None:
        wrong_index = Path(self.temporary.name) / "unrelated-index"
        with patch.dict(os.environ, {"GIT_DIR": "/nonexistent/git", "GIT_INDEX_FILE": str(wrong_index)}):
            RUNTIME.reconstruct(self.checkout, self.package, apply=True)
        self.assertFalse(wrong_index.exists())
        self.assertEqual(self.package.normalized_tree, self.run_git("write-tree"))

    def test_wrong_final_tree_is_reported_without_resetting_checkout(self) -> None:
        with self.assertRaisesRegex(RUNTIME.ReconstructionError, "Applied tree differs"):
            RUNTIME.reconstruct(self.checkout, replace(self.package, normalized_tree="0" * 40), apply=True)
        self.assertEqual(self.package.normalized_tree, self.run_git("write-tree"))
        self.assertEqual("reference\n", (self.checkout / "runtime.txt").read_text())

    def test_manifest_cannot_select_a_patch_outside_package(self) -> None:
        directory = Path(self.temporary.name) / "package"
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({
            "schemaVersion": 1, "upstream": {}, "patch": {"file": "../outside.patch"},
        }))
        with self.assertRaisesRegex(RUNTIME.ReconstructionError, "inside the runtime package"):
            RUNTIME.load_package(directory)

    def test_native_apply_verifies_both_trees_and_archive_without_committing(self) -> None:
        package, contents = self.native_package()
        opener = Mock()
        opener.open.return_value = io.BytesIO(contents)
        with patch.object(RUNTIME, "build_opener", return_value=opener):
            result = RUNTIME.reconstruct(self.checkout, package, apply=True)
        self.assertEqual(package.normalized_tree, result["normalizedTree"])
        self.assertEqual(package.native.normalized_tree, self.run_git("write-tree"))
        self.assertEqual(package.base_commit, self.run_git("rev-parse", "HEAD"))
        self.assertEqual("reference\n", (self.checkout / "runtime.txt").read_text())
        artifact = result["nativeDistribution"]["artifact"]
        self.assertTrue(artifact["verified"])
        self.assertEqual("downloaded", artifact["source"])
        self.assertEqual(contents, (self.checkout / artifact["path"]).read_bytes())
        self.assertEqual(package.native.artifact.url, opener.open.call_args.args[0].full_url)
        self.assertEqual({"timeout": 60}, opener.open.call_args.kwargs)
        self.assertEqual([package.native.artifact.file], [p.name for p in (self.checkout / "native-package-artifacts").iterdir()])

    def test_native_invalid_source_delta_and_missing_apply_refuse_before_mutation(self) -> None:
        package, _ = self.native_package()
        extra = package.native.patch + self.package.patch
        invalid = [
            replace(package.native, source_tree="0" * 40),
            replace(package.native, patch=package.native.patch + b"corrupt"),
            replace(package.native, patch=extra, patch_sha256=hashlib.sha256(extra).hexdigest()),
        ]
        with patch.object(RUNTIME, "build_opener") as opener:
            with self.assertRaisesRegex(RUNTIME.ReconstructionError, "requires --apply"):
                RUNTIME.reconstruct(self.checkout, package)
            for native in invalid:
                with self.subTest(native=native.patch_sha256):
                    with self.assertRaises(RUNTIME.ReconstructionError):
                        RUNTIME.reconstruct(self.checkout, replace(package, native=native), apply=True)
                    self.assertEqual("", self.run_git("status", "--porcelain"))
                    self.assertEqual(self.package.base_tree, self.run_git("write-tree"))
            opener.assert_not_called()

    def test_wrong_native_tree_never_downloads_and_preserves_staged_source(self) -> None:
        package, _ = self.native_package()
        with patch.object(RUNTIME, "build_opener") as opener:
            with self.assertRaisesRegex(RUNTIME.ReconstructionError, "Native applied tree differs"):
                RUNTIME.reconstruct(self.checkout, replace(package, native=replace(package.native, normalized_tree="0" * 40)), apply=True)
            opener.assert_not_called()
        self.assertEqual(package.native.normalized_tree, self.run_git("write-tree"))
        self.assertFalse((self.checkout / "native-package-artifacts").exists())

    def test_bad_native_size_or_either_digest_never_exposes_archive(self) -> None:
        artifact, contents = self.native_artifact()
        for response, declared in (
            (contents[:-1], artifact), (contents + b"extra", artifact),
            (contents, replace(artifact, sha256="0" * 64)), (contents, replace(artifact, sha512="0" * 128)),
        ):
            with self.subTest(bytes=len(response), sha512=declared.sha512):
                opener = Mock()
                opener.open.return_value = io.BytesIO(response)
                with patch.object(RUNTIME, "build_opener", return_value=opener):
                    with self.assertRaises(RUNTIME.ReconstructionError):
                        RUNTIME.provision_native_artifact(self.checkout, declared)
                self.assertEqual([], list((self.checkout / "native-package-artifacts").iterdir()))

    def test_existing_native_archive_and_symlink_conflicts_preserve_operator_files(self) -> None:
        artifact, contents = self.native_artifact()
        directory = self.checkout / "native-package-artifacts"
        directory.mkdir()
        target = directory / artifact.file
        target.write_bytes(contents)
        with patch.object(RUNTIME, "build_opener") as opener:
            self.assertEqual("existing", RUNTIME.provision_native_artifact(self.checkout, artifact)["source"])
            target.write_bytes(b"operator archive")
            with self.assertRaises(RUNTIME.ReconstructionError):
                RUNTIME.provision_native_artifact(self.checkout, artifact)
            self.assertEqual(b"operator archive", target.read_bytes())
            target.unlink()
            outside = Path(self.temporary.name) / "operator.txt"
            outside.write_bytes(b"operator work")
            target.symlink_to(outside)
            with self.assertRaises((OSError, RUNTIME.ReconstructionError)):
                RUNTIME.provision_native_artifact(self.checkout, artifact)
            self.assertTrue(target.is_symlink())
            self.assertEqual(b"operator work", outside.read_bytes())
            opener.assert_not_called()

    def test_native_parent_symlink_and_concurrent_destination_are_preserved(self) -> None:
        artifact, contents = self.native_artifact()
        directory = self.checkout / "native-package-artifacts"
        outside = Path(self.temporary.name) / "operator-directory"
        outside.mkdir()
        directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, RUNTIME.ReconstructionError)):
            RUNTIME.provision_native_artifact(self.checkout, artifact)
        self.assertEqual([], list(outside.iterdir()))
        self.assertTrue(directory.is_symlink())
        directory.unlink()
        directory.mkdir()
        target = directory / artifact.file
        opener = Mock()
        def concurrent_file(*args, **kwargs):
            target.write_bytes(b"concurrent operator work")
            return io.BytesIO(contents)
        opener.open.side_effect = concurrent_file
        with patch.object(RUNTIME, "build_opener", return_value=opener):
            with self.assertRaises(RUNTIME.ReconstructionError):
                RUNTIME.provision_native_artifact(self.checkout, artifact)
        self.assertEqual(b"concurrent operator work", target.read_bytes())
        self.assertEqual([artifact.file], [p.name for p in directory.iterdir()])

    def test_replaced_temporary_is_retained_and_never_reported_verified(self) -> None:
        artifact, contents = self.native_artifact()
        directory = self.checkout / "native-package-artifacts"
        opener = Mock()
        replaced = []
        def replace_temporary(*args, **kwargs):
            temporary = next(directory.glob("*.partial"))
            temporary.unlink()
            temporary.write_bytes(b"operator replacement")
            replaced.append(temporary)
            return io.BytesIO(contents)
        opener.open.side_effect = replace_temporary
        with patch.object(RUNTIME, "build_opener", return_value=opener):
            with self.assertRaises(RUNTIME.ArtifactCleanupError) as failed:
                RUNTIME.provision_native_artifact(self.checkout, artifact)
        self.assertIsInstance(failed.exception.operation_error, RUNTIME.ReconstructionError)
        self.assertIsInstance(failed.exception.cleanup_error, RUNTIME.ReconstructionError)
        self.assertEqual(b"operator replacement", replaced[0].read_bytes())
        self.assertFalse((directory / artifact.file).exists())

    def test_cleanup_failure_keeps_the_original_archive_failure(self) -> None:
        artifact, contents = self.native_artifact()
        opener = Mock()
        opener.open.return_value = io.BytesIO(contents[:-1])
        cleanup_error = PermissionError("controlled temporary cleanup failure")
        with patch.object(RUNTIME, "build_opener", return_value=opener), patch.object(RUNTIME.os, "unlink", side_effect=cleanup_error):
            with self.assertRaises(RUNTIME.ArtifactCleanupError) as failed:
                RUNTIME.provision_native_artifact(self.checkout, artifact)
        self.assertIs(failed.exception.cleanup_error, cleanup_error)
        self.assertIs(failed.exception.__cause__, failed.exception.operation_error)
        self.assertRegex(str(failed.exception.operation_error), "does not match the manifest")
        self.assertFalse((self.checkout / "native-package-artifacts" / artifact.file).exists())

    def test_native_manifest_is_optional_and_rejects_unbound_inputs(self) -> None:
        package, _ = self.native_package()
        directory = Path(self.temporary.name) / "package"
        directory.mkdir()
        (directory / "source.patch").write_bytes(package.patch)
        (directory / "native.patch").write_bytes(package.native.patch)
        manifest = {
            "schemaVersion": 1,
            "upstream": {"tag": package.tag, "tagObject": package.tag_object, "commit": package.base_commit, "tree": package.base_tree},
            "source": {"normalizedTree": package.normalized_tree},
            "patch": {"file": "source.patch", "sha256": package.patch_sha256},
            "nativeDistribution": {
                "sourceTree": package.normalized_tree, "normalizedTree": package.native.normalized_tree,
                "patch": {"file": "native.patch", "sha256": package.native.patch_sha256, "bytes": len(package.native.patch)},
                "artifact": vars(package.native.artifact),
            },
        }
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(package, RUNTIME.load_package(directory, native_distribution=True))
        manifest["nativeDistribution"] = None
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(self.package, RUNTIME.load_package(directory))
        with self.assertRaisesRegex(RUNTIME.ReconstructionError, "no bound native distribution"):
            RUNTIME.load_package(directory, native_distribution=True)
        for url in (
            "https://github.com/other/repository/releases/download/fixture/" + package.native.artifact.file,
            package.native.artifact.url + "?token=example", "http://github.com" + RUNTIME.PUBLIC_RELEASE_PATH + "fixture/" + package.native.artifact.file,
        ):
            with self.subTest(url=url):
                with self.assertRaises(RUNTIME.ReconstructionError):
                    RUNTIME.validate_release_url(url, package.native.artifact.file)


if __name__ == "__main__":
    unittest.main()
