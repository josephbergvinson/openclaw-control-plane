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

    def test_native_task_example_separates_execution_from_delivery(self) -> None:
        task = json.loads((ROOT / "examples" / "native-task.example.json").read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "schemas" / "native-task.schema.json").read_text(encoding="utf-8"))
        self.assertEqual([], VALIDATOR.check_instance(task, schema))
        self.assertEqual("succeeded", task["status"])
        self.assertEqual("pending", task["deliveryStatus"])

    def test_native_task_schema_rejects_invented_runtime_states(self) -> None:
        task = json.loads((ROOT / "examples" / "native-task.example.json").read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "schemas" / "native-task.schema.json").read_text(encoding="utf-8"))
        task["status"] = "complete"
        self.assertTrue(VALIDATOR.check_instance(task, schema))


if __name__ == "__main__":
    unittest.main()
