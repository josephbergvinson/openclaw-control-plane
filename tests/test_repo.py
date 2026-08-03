from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

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

    def test_readme_indexes_every_chapter(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for path in sorted((ROOT / "docs").glob("*.md")):
            self.assertIn(f"docs/{path.name}", readme, path.name)

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

    def test_status_example_matches_its_schema(self) -> None:
        status = json.loads((ROOT / "examples" / "durable-status.example.json").read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "schemas" / "durable-status.schema.json").read_text(encoding="utf-8"))
        self.assertTrue(set(schema["required"]).issubset(status))
        self.assertEqual([], [key for key in status if key not in schema["properties"]])
        self.assertIn(status["state"], schema["properties"]["state"]["enum"])
        self.assertIn(status["health"], schema["properties"]["health"]["enum"])

    def test_write_lease_is_a_separate_record_binding_the_status_artifact(self) -> None:
        lease = json.loads((ROOT / "examples" / "write-lease.example.json").read_text(encoding="utf-8"))
        required = json.loads((ROOT / "schemas" / "write-lease.schema.json").read_text(encoding="utf-8"))["required"]
        status = json.loads((ROOT / "examples" / "durable-status.example.json").read_text(encoding="utf-8"))
        self.assertTrue(set(required).issubset(lease))
        self.assertEqual({"path", "device", "inode"}, set(lease["protects_status_artifact"]))
        self.assertEqual([], [key for key in status if "lease" in key])


if __name__ == "__main__":
    unittest.main()
