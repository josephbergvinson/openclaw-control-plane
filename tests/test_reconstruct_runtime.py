from __future__ import annotations

import base64
import contextlib
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
            "patch": {"file": "source.patch", "sha256": package.patch_sha256,
                      "bytes": len(package.patch)},
            "nativeDistribution": {
                "sourceTree": package.normalized_tree, "normalizedTree": package.native.normalized_tree,
                "patch": {"file": "native.patch", "sha256": package.native.patch_sha256, "bytes": len(package.native.patch)},
                "artifact": vars(package.native.artifact),
            },
        }
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(package, RUNTIME.load_package(directory, native_distribution=True))
        for field, value in (("bytes", len(package.patch) + 1), ("sha256", "0" * 64)):
            with self.subTest(source_patch_field=field):
                broken = json.loads(json.dumps(manifest))
                broken["patch"][field] = value
                manifest_path.write_text(json.dumps(broken))
                with self.assertRaisesRegex(RUNTIME.ReconstructionError, "Patch size or SHA-256"):
                    RUNTIME.load_package(directory)
        broken = json.loads(json.dumps(manifest))
        broken["nativeDistribution"]["sourceTree"] = "moving-reference"
        manifest_path.write_text(json.dumps(broken))
        self.assertEqual(self.package, RUNTIME.load_package(directory))
        with self.assertRaisesRegex(RUNTIME.ReconstructionError, "SHA-1"):
            RUNTIME.load_package(directory, native_distribution=True)
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

    def test_current_metadata_keeps_history_out_and_scopes_deployment_separately(self) -> None:
        source, distribution = "1" * 40, "2" * 40
        manifest = {
            "schemaVersion": 1, "source": {"referenceCommit": source},
            "nativeDistribution": {"referenceCommit": distribution},
            "referenceDeployment": {"distributionCommit": distribution},
        }
        RUNTIME.validate_manifest_metadata(manifest, native_distribution=True)
        RUNTIME.validate_manifest_metadata(
            {**manifest, "referenceDeployment": {"distributionCommit": "3" * 40}},
            native_distribution=True,
        )
        for change in (
            {"historicalQualification": {"status": "pending"}},
            {"source": {"referenceCommit": "main"}},
            {"referenceDeployment": {}},
            {"referenceDeployment": {"distributionCommit": "main"}},
            {"referenceDeployment": {"distributionCommit": "g" * 40}},
            {"referenceDeployment": []},
        ):
            with self.subTest(change=change):
                with self.assertRaises(RUNTIME.ReconstructionError):
                    RUNTIME.validate_manifest_metadata({**manifest, **change}, native_distribution=True)


    def test_actual_candidate_packet_uses_the_existing_resource_contract(self) -> None:
        path = ROOT / "runtime/native160/native160-provenance.json"
        artifact, package, raw = RUNTIME.load_native_provenance(path)
        self.assertEqual(package["archiveName"], artifact.file)
        self.assertEqual(package["publicAssetURL"], artifact.url)
        self.assertEqual(package["bytes"], artifact.bytes)
        self.assertEqual(package["sha256"], artifact.sha256)
        self.assertEqual(package["integrity"], "sha512-" + base64.b64encode(
            bytes.fromhex(artifact.sha512)).decode("ascii"))
        self.assertEqual(path.read_bytes(), raw)

    def candidate_provenance(self):
        artifact, contents = self.native_artifact()
        version = "0.160.0-openclaw.fixture-darwin-arm64"
        integrity = "sha512-" + base64.b64encode(bytes.fromhex(artifact.sha512)).decode("ascii")
        package = {
            "name": "@openai/codex", "platformPackage": "@openai/codex",
            "version": version, "target": "aarch64-apple-darwin",
            "archiveName": artifact.file, "publicAssetURL": artifact.url,
            "bytes": artifact.bytes, "sha256": artifact.sha256, "integrity": integrity,
        }
        provenance = Path(self.temporary.name).resolve() / "native160-provenance.json"
        provenance.write_text(json.dumps({"schemaVersion": 1, "package": package}))
        selector = "@openai/codex@0.160.0>@openai/codex-darwin-arm64"
        relative = "native-package-artifacts/" + artifact.file
        override = "  " + json.dumps(selector) + ": " + json.dumps("file:./" + relative) + "\n"
        workspace = "packages: []\noverrides:\n" + override
        lock = (
            "lockfileVersion: '9.0'\noverrides:\n" + override
            + "packages:\n  '@openai/codex@file:" + relative + "':\n"
            + "    resolution: {integrity: " + integrity + ", tarball: file:" + relative + "}\n"
            + "    version: " + version + "\n    cpu: [arm64]\n    os: [darwin]\n"
            + "snapshots:\n  '@openai/codex@file:" + relative + "':\n    optional: true\n"
        )
        (self.checkout / "pnpm-workspace.yaml").write_text(workspace)
        environment = "---\nlockfileVersion: '9.0'\npackages:\n  environment@1: {}\nsnapshots:\n  environment@1: {}\n---\n"
        (self.checkout / "pnpm-lock.yaml").write_text(environment + lock)
        return artifact, contents, provenance

    def test_artifact_only_cli_uses_existing_owner_without_source_or_selection(self) -> None:
        artifact, contents, provenance = self.candidate_provenance()
        before = {name: (self.checkout / name).read_bytes()
                  for name in ("runtime.txt", "pnpm-workspace.yaml", "pnpm-lock.yaml")}
        head, index = self.run_git("rev-parse", "HEAD"), self.run_git("write-tree")
        selector = Path(self.temporary.name) / "current"
        selector.symlink_to(self.checkout, target_is_directory=True)
        opener = Mock()
        opener.open.return_value = io.BytesIO(contents)
        with patch.object(RUNTIME, "build_opener", return_value=opener), (
            patch.object(RUNTIME, "load_package", side_effect=AssertionError("active manifest loaded"))
        ), patch.object(RUNTIME, "reconstruct", side_effect=AssertionError("source reconstruction")), (
            patch.object(RUNTIME, "git", side_effect=AssertionError("artifact mode ran Git"))
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, RUNTIME.main([
                str(self.checkout), "--native-artifact-provenance", str(provenance),
            ]))
        result = json.loads(output.getvalue())
        self.assertEqual("artifact-provisioned-without-source-or-runtime-selection", result["status"])
        self.assertEqual("downloaded", result["artifact"]["source"])
        self.assertEqual(contents, (self.checkout / "native-package-artifacts" / artifact.file).read_bytes())
        self.assertEqual(before, {name: (self.checkout / name).read_bytes() for name in before})
        self.assertEqual(head, self.run_git("rev-parse", "HEAD"))
        self.assertEqual(index, self.run_git("write-tree"))
        self.assertEqual(self.checkout.resolve(), selector.resolve())
        self.assertTrue(selector.is_symlink())
        with patch.object(RUNTIME, "build_opener") as repeated:
            self.assertEqual("existing", RUNTIME.provision_native_from_provenance(
                self.checkout, provenance)["artifact"]["source"])
            repeated.assert_not_called()

    def test_artifact_only_rejects_source_options_before_any_owner_runs(self) -> None:
        _, _, provenance = self.candidate_provenance()
        for flag in ("--apply", "--native-distribution"):
            with self.subTest(flag=flag), contextlib.ExitStack() as stack:
                owner = stack.enter_context(patch.object(RUNTIME, "provision_native_from_provenance"))
                source = stack.enter_context(patch.object(RUNTIME, "reconstruct"))
                stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                with self.assertRaises(SystemExit) as refusal:
                    RUNTIME.main([str(self.checkout), "--native-artifact-provenance", str(provenance), flag])
                self.assertEqual(2, refusal.exception.code)
                owner.assert_not_called()
                source.assert_not_called()

    def test_candidate_provenance_refuses_invalid_sri_size_url_and_identity_before_fetch(self) -> None:
        _, _, provenance = self.candidate_provenance()
        original = json.loads(provenance.read_text())
        changes = [
            ("bytes", True), ("bytes", 0), ("bytes", -1),
            ("integrity", "sha512-" + base64.b64encode(b"short").decode("ascii")),
            ("integrity", original["package"]["integrity"] + " "),
            ("integrity", original["package"]["integrity"] + " " + original["package"]["integrity"]),
            ("integrity", "sha256-" + "a" * 64), ("sha256", "A" * 64),
            ("publicAssetURL", original["package"]["publicAssetURL"] + "?token=fixture"),
            ("publicAssetURL", original["package"]["publicAssetURL"].replace(
                "josephbergvinson/openclaw-control-plane", "other/repository")),
            ("archiveName", "../archive.tgz"), ("target", "x86_64-apple-darwin"),
            ("name", "other"), ("version", "not-a-bound-platform-version"),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                modified = json.loads(json.dumps(original))
                modified["package"][key] = value
                provenance.write_text(json.dumps(modified))
                with patch.object(RUNTIME, "build_opener") as opener:
                    with self.assertRaises(RUNTIME.ReconstructionError):
                        RUNTIME.provision_native_from_provenance(self.checkout, provenance)
                    opener.assert_not_called()
                self.assertFalse((self.checkout / "native-package-artifacts").exists())
        provenance.write_text('{"schemaVersion":1,"schemaVersion":1,"package":{}}')
        with self.assertRaisesRegex(RUNTIME.ReconstructionError, "duplicate JSON key"):
            RUNTIME.load_native_provenance(provenance)

    def test_candidate_binding_refuses_wrong_or_ambiguous_workspace_and_lock_before_fetch(self) -> None:
        artifact, _, provenance = self.candidate_provenance()
        workspace, lock = self.checkout / "pnpm-workspace.yaml", self.checkout / "pnpm-lock.yaml"
        original_workspace, original_lock = workspace.read_text(), lock.read_text()
        changes = [
            (workspace, original_workspace.replace(artifact.file, "another.tgz")),
            (workspace, original_workspace + "overrides:\n"),
            (workspace, original_workspace + original_workspace.split("overrides:\n")[1]),
            (lock, original_lock.replace("integrity: sha512-", "integrity: sha256-")),
            (lock, original_lock.replace("optional: true", "optional: false")),
            (lock, original_lock.replace("os: [darwin]", "os: [linux]")),
            (lock, original_lock.replace("cpu: [arm64]", "cpu: [x64]")),
            (lock, original_lock.replace("version: 0.160.0-openclaw.fixture", "version: 0.160.0-openclaw.other")),
        ]
        for target, changed in changes:
            with self.subTest(target=target.name, changed=changed):
                workspace.write_text(original_workspace)
                lock.write_text(original_lock)
                target.write_text(changed)
                with patch.object(RUNTIME, "build_opener") as opener:
                    with self.assertRaises(RUNTIME.ReconstructionError):
                        RUNTIME.provision_native_from_provenance(self.checkout, provenance)
                    opener.assert_not_called()
                self.assertFalse((self.checkout / "native-package-artifacts").exists())

    def test_candidate_inputs_are_rechecked_after_the_existing_download_owner(self) -> None:
        artifact, contents, provenance = self.candidate_provenance()
        lock = self.checkout / "pnpm-lock.yaml"
        original = lock.read_text()
        opener = Mock()
        def change_binding(*args, **kwargs):
            lock.write_text(original.replace("optional: true", "optional: false"))
            return io.BytesIO(contents)
        opener.open.side_effect = change_binding
        with patch.object(RUNTIME, "build_opener", return_value=opener):
            with self.assertRaises(RUNTIME.ReconstructionError):
                RUNTIME.provision_native_from_provenance(self.checkout, provenance)
        self.assertIn("optional: false", lock.read_text())
        self.assertEqual(contents, (self.checkout / "native-package-artifacts" / artifact.file).read_bytes())



if __name__ == "__main__":
    unittest.main()
