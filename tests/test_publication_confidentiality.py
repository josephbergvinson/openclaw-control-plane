"""Publication checks exercise the bytes that would otherwise leave the job."""

import base64
import gzip
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate_repo.py"
SPEC = importlib.util.spec_from_file_location("publication_validator", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


class PublicationConfidentialityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = ("ghp_" + "A" * 36).encode()

    def inspect(self, payload, name="output.log"):
        path = self.root / name
        path.write_bytes(payload)
        return validator.publication_findings([path])

    def test_ordinary_build_output_passes(self):
        self.assertEqual(self.inspect(b"12 tests passed; authorization: basic ***"), [])

    def test_token_and_binary_metadata_are_detected_without_echo(self):
        for payload in [self.token, b"\0binary-header\0" + self.token + b"\0"]:
            with self.subTest(payload=payload[:10]):
                findings = self.inspect(payload)
                self.assertIn("github-token", [x.category for x in findings])
                self.assertNotIn(self.token.decode(), "\n".join(x.render() for x in findings))

    def test_encoded_source_map_metadata_and_utf16_are_inspected(self):
        encoded = base64.b64encode(self.token)
        for payload in [b'{"sourcesContent":["data:text/plain;base64,' + encoded + b'"]}',
                        ("prefix " + self.token.decode()).encode("utf-16")]:
            with self.subTest(payload=payload[:10]):
                self.assertIn("github-token", [x.category for x in self.inspect(payload)])

    def test_encoded_member_names_and_over_limit_candidates_do_not_escape(self):
        name = "base64," + base64.b64encode(self.token).decode()
        self.assertIn("github-token", [x.category for x in self.inspect(b"safe", name)])
        private_name = "base64," + base64.b64encode(b"nested/credentials.json").decode()
        self.assertIn("private-publication-filename", [x.category for x in self.inspect(b"safe", private_name)])
        self.assertIn("decoding-limit-uninspected", [x.category for x in self.inspect(b"base64," + b"A" * 8193)])

    def test_private_pattern_names_and_values_are_not_printed(self):
        patterns = self.root / "operator-patterns.json"
        patterns.write_text(json.dumps({"private-person-name": "operator-only-value"}))
        path = self.root / "report.log"
        path.write_bytes(b"operator-only-value")
        rendered = "\n".join(x.render() for x in validator.publication_findings([path], patterns))
        self.assertIn("operator-pattern-1", rendered)
        self.assertNotIn("private-person-name", rendered)
        self.assertNotIn("operator-only-value", rendered)

    def test_private_filenames_and_missing_inputs_fail(self):
        self.assertIn("private-publication-filename", [x.category for x in self.inspect(b"ok", "receipt.private.fixture")])
        self.assertIn("missing-publication-input", [x.category for x in validator.publication_findings([self.root / "missing"])])

    def zip_bytes(self, name, data):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(name, data)
        return output.getvalue()

    def test_nested_zip_and_gzip_cannot_hide_a_token(self):
        payload = self.zip_bytes("nested.zip", self.zip_bytes("output.log", self.token))
        for payload in [payload, gzip.compress(self.token)]:
            self.assertIn("github-token", [x.category for x in self.inspect(payload)])

    def test_tar_metadata_is_inspected_without_extraction(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            entry = tarfile.TarInfo("metadata.txt")
            entry.size = len(self.token)
            archive.addfile(entry, io.BytesIO(self.token))
        self.assertIn("github-token", [x.category for x in self.inspect(output.getvalue(), "output.tar")])

    def test_unsafe_or_uninspectable_archives_never_pass(self):
        cases = [(self.zip_bytes("../escape", b"ok"), "output.zip"),
                 (b"not-a-zip", "output.zip"), (b"PK\x03\x04bad", "output.zip")]
        for payload, name in cases:
            with self.subTest(name=name, payload=payload[:8]):
                self.assertTrue(self.inspect(payload, name))
        self.assertFalse((self.root.parent / "escape").exists())

    def test_limits_and_symlinks_are_evidence_gaps(self):
        with patch.object(validator, "PUBLICATION_MAX_BYTES", 4):
            self.assertIn("file-size-limit-uninspected", [x.category for x in self.inspect(b"12345")])
        path = self.root / "link.log"
        path.symlink_to(self.root / "output.log")
        self.assertIn("symlink-uninspected", [x.category for x in validator.publication_findings([path])])

    def test_directory_walk_and_nested_archives_are_bounded(self):
        for index in range(3):
            (self.root / f"output-{index}.log").write_bytes(b"safe")
        with patch.object(validator, "PUBLICATION_MAX_ENTRIES", 2):
            self.assertIn("filesystem-entry-limit-uninspected", [x.category for x in validator.publication_findings([self.root])])
        payload = self.token
        for index in range(5):
            payload = self.zip_bytes(f"nested-{index}.zip", payload)
        self.assertIn("inspection-limit-uninspected", [x.category for x in self.inspect(payload)])

    def test_aggregate_budget_stops_before_reading_the_next_file(self):
        first, second = self.root / "first.log", self.root / "second.log"
        first.write_bytes(b"safe")
        second.write_bytes(b"more")
        original = Path.open
        reads = []
        def record(path, *args, **kwargs):
            if (args and args[0] == "rb") or kwargs.get("mode") == "rb":
                reads.append(path)
            return original(path, *args, **kwargs)
        with patch.object(validator, "PUBLICATION_MAX_TOTAL", 5), patch.object(Path, "open", record):
            findings = validator.publication_findings([first, second])
        self.assertTrue(findings)
        self.assertEqual(reads, [first])

    def test_private_relative_directories_are_checked(self):
        private = self.root / "receipts.private.dir"
        private.mkdir()
        (private / "output.log").write_bytes(b"safe content")
        self.assertIn("private-publication-filename", [x.category for x in validator.publication_findings([self.root])])

    def test_cli_blocks_upload_without_leaking_token(self):
        path = self.root / "output.log"
        path.write_bytes(self.token)
        result = subprocess.run([sys.executable, str(SCRIPT), "--publication-input", str(path)], capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(self.token, result.stdout + result.stderr)
        self.assertIn(b"github-token", result.stderr)

    def test_git_metadata_requires_noreply_for_the_requested_range(self):
        def run(*args):
            return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True)
        run("init", "-q")
        run("-c", "user.name=Example", "-c", "user.email=example@users.noreply.github.com",
            "commit", "--allow-empty", "-qm", "Safe metadata")
        with patch.object(validator, "ROOT", self.root):
            self.assertEqual(validator.publication_git_findings("HEAD"), [])
        run("-c", "user.name=Example", "-c", "user.email=operator@example.invalid",
            "commit", "--allow-empty", "-qm", "Private metadata")
        with patch.object(validator, "ROOT", self.root):
            findings = validator.publication_git_findings("HEAD^..HEAD")
        self.assertEqual([x.category for x in findings], ["non-noreply-git-metadata"])
        self.assertNotIn("operator@example.invalid", findings[0].render())

    def test_every_artifact_upload_requires_its_exact_inputs_to_pass(self):
        workflow = (SCRIPT.parents[1] / ".github/workflows/verify-reference.yml").read_text()
        steps = re.split(r"(?=^      - name: )", workflow, flags=re.M)
        uploads = 0
        for index, step in enumerate(steps):
            if "uses: actions/upload-artifact@" not in step:
                continue
            uploads += 1
            guard = steps[index - 1]
            guard_id = re.search(r"^        id: (.+)$", guard, re.M).group(1)
            self.assertIn(f"steps.{guard_id}.outcome == 'success'", step)
            self.assertIn('scripts/validate_repo.py" "${args[@]}"', guard)
            paths = re.search(r"^          path: \|\n((?:            .*\n)+)", step, re.M).group(1)
            for path in paths.splitlines():
                self.assertIn(path.strip().replace("${{ runner.temp }}", "$RUNNER_TEMP"), guard)
        self.assertGreater(uploads, 0)

    def test_git_commit_text_and_deleted_historical_blobs_are_checked(self):
        def run(*args):
            return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Example",
                                   "-c", "user.email=example@users.noreply.github.com", *args],
                                  capture_output=True, check=True)
        run("init", "-q")
        run("commit", "--allow-empty", "-qm", "Baseline")
        baseline = run("rev-parse", "HEAD").stdout.decode().strip()
        (self.root / "output.log").write_bytes(self.token)
        run("add", "output.log")
        run("commit", "-qm", "Fixture " + self.token.decode())
        run("rm", "output.log")
        run("commit", "-qm", "Remove fixture")
        with patch.object(validator, "ROOT", self.root):
            findings = validator.publication_git_findings(baseline + "..HEAD")
        self.assertGreaterEqual([x.category for x in findings].count("github-token"), 2)
        self.assertNotIn(self.token.decode(), "\n".join(x.render() for x in findings))

    def test_deleted_git_private_filenames_use_the_same_policy(self):
        def run(*args):
            return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Example",
                                   "-c", "user.email=example@users.noreply.github.com", *args],
                                  capture_output=True, check=True)
        run("init", "-q")
        run("commit", "--allow-empty", "-qm", "Baseline")
        baseline = run("rev-parse", "HEAD").stdout.decode().strip()
        for name in [".env", "credentials.json"]:
            (self.root / name).write_bytes(b"unrecognized content")
            run("add", name)
            run("commit", "-qm", "Add fixture")
            run("rm", name)
            run("commit", "-qm", "Remove fixture")
        with patch.object(validator, "ROOT", self.root):
            findings = validator.publication_git_findings(baseline + "..HEAD")
        self.assertEqual([x.category for x in findings].count("private-publication-filename"), 2)


if __name__ == "__main__":
    unittest.main()
