from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
