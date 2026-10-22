from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("validate_repo", ROOT / "scripts" / "validate_repo.py")
assert SPEC and SPEC.loader
VALIDATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VALIDATOR
SPEC.loader.exec_module(VALIDATOR)

PROVENANCE_LEVELS = {"policy-only", "helper-backed", "runtime-backed", "live-proven"}


class RepositoryTests(unittest.TestCase):
    def test_repository_checks_pass(self) -> None:
        files = VALIDATOR.candidate_files()
        findings = []
        findings.extend(VALIDATOR.validate_tree())
        findings.extend(VALIDATOR.validate_json(files))
        findings.extend(VALIDATOR.validate_examples_against_schemas())
        findings.extend(VALIDATOR.validate_links(files))
        findings.extend(VALIDATOR.validate_mermaid(files))
        findings.extend(VALIDATOR.validate_diagram_references())
        findings.extend(VALIDATOR.validate_headings())
        self.assertEqual([], [finding.render() for finding in findings])

    def test_documentation_is_contiguously_numbered(self) -> None:
        numbers = sorted(
            int(match.group(1))
            for path in (ROOT / "docs").glob("*.md")
            if (match := re.match(r"(\d{2})-", path.name))
        )
        self.assertEqual(list(range(len(numbers))), numbers)

    def test_release_validation_rejects_a_coherently_stale_reference(self) -> None:
        manifest = json.loads((ROOT / "runtime/manifest.json").read_text())
        source = manifest["source"]["referenceCommit"]
        distribution = manifest["nativeDistribution"]["referenceCommit"]
        for expected_source, expected_distribution, expected_code, message in (
            (source, distribution, 0, ""),
            ("0" * 40, distribution, 1, "stale source reference"),
            (source, "0" * 40, 1, "stale distribution reference"),
        ):
            with self.subTest(source=expected_source, distribution=expected_distribution):
                result = subprocess.run([
                    sys.executable, "-B", str(ROOT / "scripts/validate_repo.py"),
                    "--expected-source-commit", expected_source,
                    "--expected-distribution-commit", expected_distribution,
                ], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(expected_code, result.returncode, result.stderr)
                if message:
                    self.assertIn(message, result.stderr)
                    self.assertNotIn("checks passed", result.stdout)

    def test_expected_companion_reference_requires_source_and_reconstructable_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime").mkdir()
            path = root / "runtime/manifest.json"
            current = {"source": {"referenceCommit": "1" * 40},
                       "nativeDistribution": {"referenceCommit": "2" * 40},
                       "nativeCompanion": {"sourceCommit": "3" * 40, "delta": {}}}
            with patch.object(VALIDATOR, "ROOT", root):
                path.write_text(json.dumps(current))
                self.assertEqual([], VALIDATOR.validate_expected_reference("1" * 40, "2" * 40))
                self.assertEqual([], VALIDATOR.validate_expected_reference("1" * 40, "2" * 40, "3" * 40))
                findings = VALIDATOR.validate_expected_reference("1" * 40, "2" * 40, "4" * 40)
                self.assertTrue(any("stale companion" in finding.label for finding in findings))
                del current["nativeCompanion"]["delta"]
                path.write_text(json.dumps(current))
                findings = VALIDATOR.validate_expected_reference("1" * 40, "2" * 40, "3" * 40)
                self.assertTrue(any("no reconstructable delta" in finding.label for finding in findings))
                del current["nativeCompanion"]
                path.write_text(json.dumps(current))
                findings = VALIDATOR.validate_expected_reference(None, None, "3" * 40)
                self.assertTrue(any("stale companion" in finding.label for finding in findings))

    def test_expected_identity_companion_requires_the_exact_distribution_commit_and_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime").mkdir()
            path = root / "runtime/manifest.json"
            current = {"source": {"referenceCommit": "1" * 40},
                       "nativeDistribution": {"referenceCommit": "2" * 40, "referenceTree": "3" * 40,
                                              "normalizedTree": "3" * 40},
                       "nativeCompanion": {"sourceCommit": "2" * 40, "sourceTree": "3" * 40}}
            with patch.object(VALIDATOR, "ROOT", root):
                path.write_text(json.dumps(current))
                self.assertTrue(VALIDATOR.companion_uses_native_source(current))
                self.assertEqual([], VALIDATOR.validate_expected_reference("1" * 40, "2" * 40, "2" * 40))
                invalid = []
                for section, field in (("nativeCompanion", "sourceCommit"), ("nativeCompanion", "sourceTree"),
                                       ("nativeDistribution", "referenceCommit"), ("nativeDistribution", "referenceTree"),
                                       ("nativeDistribution", "normalizedTree")):
                    for value in ("4" * 40, None, "moving-reference"):
                        broken = json.loads(json.dumps(current))
                        broken[section][field] = value
                        invalid.append(broken)
                for broken in invalid:
                    path.write_text(json.dumps(broken))
                    self.assertFalse(VALIDATOR.companion_uses_native_source(broken))
                    findings = VALIDATOR.validate_expected_reference("1" * 40, "2" * 40, "2" * 40)
                    self.assertTrue(findings)
                broken = json.loads(json.dumps(current))
                broken["nativeCompanion"]["delta"] = None
                self.assertFalse(VALIDATOR.companion_uses_native_source(broken))

    def test_readme_reaches_the_complete_documentation_index(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("](docs/README.md)", readme)
        index = (ROOT / "docs/README.md").read_text(encoding="utf-8")
        for path in sorted((ROOT / "docs").glob("[0-9][0-9]-*.md")):
            self.assertIn(f"]({path.name})", index, path.name)
        self.assertIn("](history/README.md)", index)

    def test_native_notices_reject_stale_archive_and_source_patch_bindings(self) -> None:
        native = json.loads((ROOT / "runtime/manifest.json").read_text())["nativeDistribution"]
        artifact = SimpleNamespace(**native["artifact"])
        provenance = ROOT / "runtime" / native["provenanceFile"]
        original = json.loads(provenance.read_text())
        raw_patch = (provenance.parent / original["source"]["patch"]["path"]).read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "runtime/bound-native"
            packet.mkdir(parents=True)
            source_patch = packet / original["source"]["patch"]["path"]
            source_patch.write_bytes(raw_patch)
            path = packet / "notice.json"
            with patch.object(VALIDATOR, "ROOT", root):
                for key, value in (
                    ("archiveName", "old-package.tgz"), ("publicAssetURL", "https://example.com/old"),
                    ("bytes", 0), ("sha256", "0" * 64), ("integrity", "sha512-stale"),
                ):
                    with self.subTest(field=key):
                        stale = copy.deepcopy(original)
                        stale["package"][key] = value
                        path.write_text(json.dumps(stale))
                        findings = VALIDATOR.validate_native_provenance(artifact, "bound-native/notice.json")
                        self.assertTrue(any(key in finding.label for finding in findings))
                path.write_text(json.dumps(original))
                source_patch.write_bytes(raw_patch + b"\n")
                self.assertTrue(any("source patch" in finding.label
                                    for finding in VALIDATOR.validate_native_provenance(artifact, "bound-native/notice.json")))

    def test_native_notices_follow_the_bound_packet_without_a_version_specific_path(self) -> None:
        native = json.loads((ROOT / 'runtime/manifest.json').read_text())['nativeDistribution']
        artifact = SimpleNamespace(**native['artifact'])
        provenance = ROOT / 'runtime' / native['provenanceFile']
        original = json.loads(provenance.read_text())
        raw_patch = (provenance.parent / original['source']['patch']['path']).read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            packet = root / 'runtime/renamed-native'
            packet.mkdir(parents=True)
            original['source']['patch']['path'] = 'ported-source.patch'
            (packet / 'ported-source.patch').write_bytes(raw_patch)
            notice = packet / 'notice.json'
            notice.write_text(json.dumps(original))
            with patch.object(VALIDATOR, 'ROOT', root):
                self.assertEqual([], VALIDATOR.validate_native_provenance(artifact, 'renamed-native/notice.json'))
                for invalid in (None, '/notice.json', '../notice.json', 'renamed-native/../notice.json', 'renamed-native/notice.txt'):
                    with self.subTest(path=invalid):
                        self.assertTrue(VALIDATOR.validate_native_provenance(artifact, invalid))
                link = packet / 'linked.json'
                link.symlink_to(notice)
                self.assertTrue(VALIDATOR.validate_native_provenance(artifact, 'renamed-native/linked.json'))

    def test_capability_provenance_defines_all_four_levels(self) -> None:
        text = (ROOT / "docs" / "03-capability-provenance.md").read_text(encoding="utf-8").casefold()
        for level in PROVENANCE_LEVELS:
            self.assertIn(level, text, level)

    def test_core_contract_separations_are_documented(self) -> None:
        agents = (ROOT / "templates" / "AGENTS.example.md").read_text(encoding="utf-8").casefold()
        tools = (ROOT / "templates" / "TOOLS.example.md").read_text(encoding="utf-8").casefold()
        for phrase in ["inline", "durable", "precedence", "approval"]:
            self.assertIn(phrase, agents, phrase)
        for phrase in ["canonical", "worktree", "verification"]:
            self.assertIn(phrase, tools, phrase)

    def test_every_example_has_a_schema(self) -> None:
        for path in sorted((ROOT / "examples").glob("*.example.json")):
            stem = path.name.split(".example.json")[0]
            self.assertTrue((ROOT / "schemas" / f"{stem}.schema.json").is_file(), stem)

    def test_native_run_example_separates_execution_from_delivery(self) -> None:
        task = json.loads((ROOT / "examples" / "native-run.example.json").read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "schemas" / "native-run.schema.json").read_text(encoding="utf-8"))
        self.assertEqual([], VALIDATOR.check_instance(task, schema))
        self.assertEqual("terminal", task["status"])
        self.assertEqual("ok", task["outcome"]["status"])
        self.assertEqual("pending", task["deliveryStatus"])

    def test_native_run_schema_rejects_invented_runtime_states(self) -> None:
        task = json.loads((ROOT / "examples" / "native-run.example.json").read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "schemas" / "native-run.schema.json").read_text(encoding="utf-8"))
        task["status"] = "complete"
        self.assertTrue(VALIDATOR.check_instance(task, schema))


if __name__ == "__main__":
    unittest.main()
