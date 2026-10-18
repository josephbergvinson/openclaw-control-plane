#!/usr/bin/env python3
"""Documentation checks for this repository.

Verifies that the tree hangs together: required files exist, JSON parses, every example
carries the fields its schema requires, relative links resolve, Mermaid sources have a
valid header, each diagram is referenced by at least one chapter, and every chapter has
exactly one title.

Checks cover document structure and declared artifact bindings, not live behavior.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".mmd", ".py", ".json", ".sh", ".txt"}
SKIP_PARTS = {".git", "__pycache__", ".pytest_cache"}

REQUIRED = {
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "runtime/manifest.json",
    "scripts/reconstruct_runtime.py",
    "config/openclaw.preferences.json",
    "templates/WRITING.example.md",
    "docs/00-start-here.md",
    "docs/01-glossary.md",
    "docs/03-capability-provenance.md",
    "docs/04-system-architecture.md",
    "docs/06-execution-and-durable-lanes.md",
    "docs/07-worked-example.md",
    "docs/08-memory-and-context.md",
    "docs/10-runtime-releases-and-promotion.md",
    "docs/16-security-and-trust-model.md",
    "docs/17-adoption-guide.md",
    "templates/AGENTS.example.md",
    "templates/TOOLS.example.md",
    "examples/native-task.example.json",
    "schemas/native-task.schema.json",
}

MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
MERMAID_HEADERS = ("flowchart", "graph", "stateDiagram", "sequenceDiagram", "classDiagram")


@dataclass(frozen=True)
class Finding:
    path: Path
    label: str
    line: int | None = None

    def render(self) -> str:
        rel = self.path.relative_to(ROOT)
        return f"{rel}:{self.line}: {self.label}" if self.line else f"{rel}: {self.label}"


def candidate_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return sorted(files)


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def validate_json(files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        if path.suffix.lower() != ".json":
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            findings.append(Finding(path, f"invalid JSON: {exc.msg}", exc.lineno))
    return findings


JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def check_instance(data: object, schema: dict, path: str = "") -> list[str]:
    """Recursively validate one instance against a subset of JSON Schema.

    Covers the keywords these examples actually use: type, enum, const, required,
    properties, additionalProperties, items, minItems, and maxItems. Nested objects and
    array items are checked at every level, so a violation deep inside a record is caught
    rather than passing because the top-level keys happen to be present.
    """
    errors: list[str] = []
    here = path or "<root>"

    expected = schema.get("type")
    if expected:
        allowed = expected if isinstance(expected, list) else [expected]
        python_types = tuple(
            t
            for name in allowed
            for t in (JSON_TYPES.get(name, ()) if isinstance(JSON_TYPES.get(name), tuple) else (JSON_TYPES.get(name),))
            if t is not None
        )
        # bool is a subclass of int in Python; JSON Schema treats them as distinct.
        if python_types and (
            not isinstance(data, python_types)
            or (isinstance(data, bool) and "boolean" not in allowed)
        ):
            return [f"{here}: expected {'/'.join(allowed)}, found {type(data).__name__}"]

    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{here}: value not in enum")
    if "const" in schema and data != schema["const"]:
        errors.append(f"{here}: value does not match const")

    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data:
                errors.append(f"{here}: missing required property '{key}'")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in properties:
                    errors.append(f"{here}: unexpected property '{key}'")
        for key, value in data.items():
            if key in properties:
                errors.extend(check_instance(value, properties[key], f"{path}.{key}" if path else key))

    if isinstance(data, list):
        if "minItems" in schema and len(data) < schema["minItems"]:
            errors.append(f"{here}: fewer than {schema['minItems']} item(s)")
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            errors.append(f"{here}: more than {schema['maxItems']} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(data):
                errors.extend(check_instance(item, item_schema, f"{path}[{index}]"))

    return errors


def validate_examples_against_schemas() -> list[Finding]:
    """Every example must validate against its schema, at every level of nesting."""
    findings: list[Finding] = []
    for example in sorted((ROOT / "examples").glob("*.example.json")):
        stem = example.name.split(".example.json")[0]
        schema_path = ROOT / "schemas" / f"{stem}.schema.json"
        if not schema_path.is_file():
            findings.append(Finding(example, "example has no matching schema"))
            continue
        try:
            data = json.loads(example.read_text(encoding="utf-8"))
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue  # already reported by validate_json
        for error in check_instance(data, schema):
            findings.append(Finding(example, error))
    return findings


def validate_links(files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        if path.suffix.lower() != ".md":
            continue
        text = path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK_RE.finditer(text):
            target = unquote(match.group(1).strip().split()[0].strip("<>"))
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            target_path = target.split("#", 1)[0]
            if target_path and not (path.parent / target_path).resolve().exists():
                findings.append(Finding(path, f"broken relative link: {target_path}", line_number(text, match.start())))
    return findings


def validate_mermaid(files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".md" and text.count("```mermaid") > text.count("```") // 2:
            findings.append(Finding(path, "unbalanced Mermaid fence"))
        if path.suffix.lower() == ".mmd" and not text.lstrip().startswith(MERMAID_HEADERS):
            findings.append(Finding(path, "missing or unsupported Mermaid diagram header", 1))
    return findings


def validate_diagram_references() -> list[Finding]:
    """Each standalone diagram should be referenced by at least one document."""
    findings: list[Finding] = []
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "docs").glob("*.md")))
    corpus += (ROOT / "README.md").read_text(encoding="utf-8")
    for diagram in sorted((ROOT / "diagrams").glob("*.mmd")):
        if diagram.name not in corpus:
            findings.append(Finding(diagram, "diagram is not referenced by any document"))
    return findings


def validate_headings() -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted((ROOT / "docs").glob("*.md")):
        headings = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("# ")]
        if len(headings) != 1:
            findings.append(Finding(path, f"expected exactly one title, found {len(headings)}"))
    return findings


def validate_tree() -> list[Finding]:
    findings: list[Finding] = []
    for rel in sorted(REQUIRED):
        if not (ROOT / rel).is_file():
            findings.append(Finding(ROOT / rel, "required file missing"))
    manifest_path = ROOT / "runtime/manifest.json"
    try:
        patch = json.loads(manifest_path.read_text())["patch"]["file"]
        if not isinstance(patch, str) or Path(patch).name != patch or not patch.endswith(".patch"):
            findings.append(Finding(manifest_path, "patch must name a runtime package file"))
        elif not (ROOT / "runtime" / patch).is_file():
            findings.append(Finding(ROOT / "runtime" / patch, "required manifest patch missing"))
    except (OSError, ValueError, KeyError, TypeError):
        findings.append(Finding(manifest_path, "runtime patch declaration unavailable"))
    try:
        spec = importlib.util.spec_from_file_location(
            "reference_runtime_package", ROOT / "scripts/reconstruct_runtime.py")
        if spec is None or spec.loader is None:
            raise ValueError("reconstruction helper unavailable")
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
    except (OSError, ImportError, ValueError) as error:
        findings.append(Finding(manifest_path, f"reconstruction helper unavailable: {error}"))
    else:
        try:
            runtime = package.load_package(ROOT / "runtime", native_distribution=True)
            if runtime.native is not None:
                findings.extend(validate_native_provenance(runtime.native.artifact))
        except (package.ReconstructionError, OSError, ValueError, KeyError, TypeError) as error:
            findings.append(Finding(manifest_path, f"invalid reconstruction package: {error}"))
    for path in ROOT.rglob("*"):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.is_symlink():
            findings.append(Finding(path, "symlinks are not allowed in this tree"))
    return findings


def validate_native_provenance(artifact) -> list[Finding]:
    path = ROOT / "runtime/native154/native154-provenance.json"
    try:
        provenance = json.loads(path.read_text(encoding="utf-8"))
        expected = {
            "archiveName": artifact.file, "publicAssetURL": artifact.url,
            "bytes": artifact.bytes, "sha256": artifact.sha256,
            "integrity": "sha512-" + base64.b64encode(bytes.fromhex(artifact.sha512)).decode("ascii"),
        }
        findings = [Finding(path, f"native provenance differs from reconstruction artifact: {key}")
                    for key, value in expected.items() if provenance["package"].get(key) != value]
        patch = provenance["source"]["patch"]
        if patch["path"] != "native154-source.patch":
            return findings + [Finding(path, "native source patch must name the adjacent source patch")]
        raw = (path.parent / patch["path"]).read_bytes()
        if len(raw) != patch["bytes"] or hashlib.sha256(raw).hexdigest() != patch["sha256"]:
            findings.append(Finding(path, "native source patch size or SHA-256 differs from provenance"))
        return findings
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return [Finding(path, "native provenance cannot be verified")]


def validate_expected_reference(source_commit: str | None, distribution_commit: str | None) -> list[Finding]:
    if source_commit is None and distribution_commit is None:
        return []
    path = ROOT / "runtime/manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        findings = []
        for section, label, expected in (
            ("source", "source", source_commit),
            ("nativeDistribution", "distribution", distribution_commit),
        ):
            if expected is not None and manifest[section]["referenceCommit"] != expected:
                findings.append(Finding(path, f"stale {label} reference: expected reviewed commit {expected}"))
        return findings
    except (OSError, ValueError, KeyError, TypeError):
        return [Finding(path, "expected release identity cannot be verified")]


def exact_commit(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError("use the full reviewed Git commit SHA-1")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the documentation checks.")
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    parser.add_argument("--expected-source-commit", type=exact_commit, metavar="SHA",
                        help="require the public package to match this reviewed canonical source commit")
    parser.add_argument("--expected-distribution-commit", type=exact_commit, metavar="SHA",
                        help="require the public package to match this reviewed distribution commit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    files = candidate_files()

    findings = validate_tree()
    findings.extend(validate_expected_reference(args.expected_source_commit, args.expected_distribution_commit))
    findings.extend(validate_json(files))
    findings.extend(validate_examples_against_schemas())
    findings.extend(validate_links(files))
    findings.extend(validate_mermaid(files))
    findings.extend(validate_diagram_references())
    findings.extend(validate_headings())

    if findings:
        for finding in findings:
            print(finding.render(), file=sys.stderr)
        print(f"\n{len(findings)} finding(s)", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"checks passed: {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
