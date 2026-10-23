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
import binascii
import gzip
import hashlib
import importlib.util
import io
import json
import re
import stat
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterable
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
    "examples/native-run.example.json",
    "schemas/native-run.schema.json",
}

MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
MERMAID_HEADERS = ("flowchart", "graph", "stateDiagram", "sequenceDiagram", "classDiagram")

# This validation owner also checks the exact files about to be published. The
# optional operator patterns stay outside the public checkout and hosted logs.
PUBLICATION_PATTERNS = {
    "github-token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{40,255})\b",
    "private-key": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "openai-key": rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,255}\b",
    "google-refresh-token": rb"\b1//[A-Za-z0-9_-]{40,255}\b",
}
PUBLICATION_MAX_BYTES = 128 * 1024 * 1024
PUBLICATION_MAX_TOTAL = 512 * 1024 * 1024
PUBLICATION_MAX_ENTRIES = 4096
PUBLICATION_MAX_DEPTH = 3
PUBLICATION_MAX_DECODED = 256
ENCODED_VALUE = re.compile(
    rb"(?:base64[,\s:=]+|(?:encoded|payload|secret|token)[A-Za-z0-9_]*[\s:=]+[\"'])"
    rb"([A-Za-z0-9+/_-]{16,8192}={0,2})", re.I)


@dataclass(frozen=True)
class PublicationFinding:
    category: str
    identity: str

    def render(self) -> str:
        # Never echo the suspect content or its filename into a public CI log.
        return f"publication: {self.category} (sha256:{self.identity})"


def private_publication_name(name: str) -> bool:
    return ".private." in name or Path(name).name in {".env", "credentials.json"}


def publication_findings(paths: list[Path], pattern_file: Path | None = None,
                         payloads: Iterable[tuple[str, bytes]] = ()) -> list[PublicationFinding]:
    patterns = {name: re.compile(value, re.I) for name, value in PUBLICATION_PATTERNS.items()}
    if pattern_file is not None:
        private = json.loads(pattern_file.read_text(encoding="utf-8"))
        if not isinstance(private, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                  for k, v in private.items()):
            raise ValueError("publication patterns must be a JSON object of regular expressions")
        # Labels from a private pattern file can themselves contain private data.
        patterns.update({f"operator-pattern-{index}": re.compile(value.encode(), re.I)
                         for index, value in enumerate(private.values(), 1)})
    findings: list[PublicationFinding] = []
    total = entries = decoded = 0

    def finding(category: str, data: bytes) -> None:
        findings.append(PublicationFinding(category, hashlib.sha256(data).hexdigest()))

    def inspect(name: str, data: bytes, depth: int = 0) -> None:
        nonlocal total, entries, decoded
        total += len(data)
        entries += 1
        if (len(data) > PUBLICATION_MAX_BYTES or total > PUBLICATION_MAX_TOTAL
                or entries > PUBLICATION_MAX_ENTRIES or depth > PUBLICATION_MAX_DEPTH):
            finding("inspection-limit-uninspected", data)
            return
        if private_publication_name(name):
            finding("private-publication-filename", name.encode())
        for category, pattern in patterns.items():
            if pattern.search(data):
                finding(category, data)
            elif pattern.search(name.encode()):
                finding(category, name.encode())
        # UTF-16 and explicit encoded payloads occur in embedded metadata and
        # source maps. Decode bounded candidates; unknown encodings are not a
        # guarantee of absence. No extracted payload is executed or written.
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            inspect("decoded-text", data.decode("utf-16", errors="replace").encode(), depth + 1)
        for view, metadata in [(data, False), (name.encode(), True)]:
            for match in ENCODED_VALUE.finditer(view):
                decoded += 1
                if (decoded > PUBLICATION_MAX_DECODED
                        or (match.end() < len(view)
                            and view[match.end()] in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-=")):
                    finding("decoding-limit-uninspected", view)
                    return
                value = match.group(1)
                try:
                    plain = base64.b64decode(value + b"=" * (-len(value) % 4), altchars=b"-_", validate=True)
                except (ValueError, binascii.Error):
                    continue
                if metadata:
                    inspect(plain.decode("utf-8", errors="replace"), b"", depth + 1)
                else:
                    inspect("decoded-payload", plain, depth + 1)
        try:
            if data.startswith(b"PK\x03\x04"):
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    members = archive.infolist()
                    if len(members) + entries > PUBLICATION_MAX_ENTRIES:
                        finding("archive-entry-limit-uninspected", data)
                        return
                    # Admit all metadata before decompressing any member.
                    if sum(m.file_size for m in members) + total > PUBLICATION_MAX_TOTAL:
                        finding("archive-size-limit-uninspected", data)
                        return
                    seen: set[str] = set()
                    for member in members:
                        parts = member.filename.rstrip("/").split("/")
                        mode = stat.S_IFMT(member.external_attr >> 16)
                        if (member.filename in seen or member.filename.startswith(("/", "\\"))
                                or "\\" in member.filename or any(p in {"", ".", ".."} for p in parts)
                                or re.match(r"^[A-Za-z]:", member.filename)
                                or mode not in {0, stat.S_IFREG, stat.S_IFDIR}
                                or member.flag_bits & 1 or member.file_size > PUBLICATION_MAX_BYTES
                                or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                                or member.file_size > max(1, member.compress_size) * 1000):
                            finding("unsafe-archive-uninspected", data)
                            return
                        seen.add(member.filename)
                    for member in members:
                        if not member.is_dir():
                            inspect(member.filename, archive.read(member), depth + 1)
            elif data.startswith(b"\x1f\x8b"):
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                    plain = stream.read(PUBLICATION_MAX_BYTES + 1)
                inspect(name.removesuffix(".gz"), plain, depth + 1)
            elif len(data) > 262 and data[257:262] == b"ustar":
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                    seen_tar: set[str] = set()
                    for member in archive:
                        parts = member.name.rstrip("/").split("/")
                        if (member.name in seen_tar or member.name.startswith(("/", "\\"))
                                or "\\" in member.name or any(p in {"", ".", ".."} for p in parts)
                                or re.match(r"^[A-Za-z]:", member.name)
                                or (not member.isfile() and not member.isdir())
                                or member.size > PUBLICATION_MAX_BYTES
                                or len(seen_tar) + entries >= PUBLICATION_MAX_ENTRIES
                                or total + member.size > PUBLICATION_MAX_TOTAL):
                            finding("unsafe-archive-uninspected", data)
                            return
                        seen_tar.add(member.name)
                        if member.isfile():
                            stream = archive.extractfile(member)
                            if stream is None:
                                finding("archive-member-uninspected", data)
                                return
                            with stream:
                                inspect(member.name, stream.read(PUBLICATION_MAX_BYTES + 1), depth + 1)
            elif name.lower().endswith((".zip", ".gz", ".tgz", ".tar", ".zst", ".7z")):
                finding("unsupported-archive-uninspected", data)
        except (OSError, EOFError, ValueError, zipfile.BadZipFile, tarfile.TarError):
            finding("invalid-archive-uninspected", data)

    filesystem_entries = 0
    for path in paths:
        if path.is_symlink():
            finding("symlink-uninspected", str(path).encode())
            continue
        directory_input = path.is_dir()
        candidates = path.rglob("*") if directory_input else [path]
        for candidate in candidates:
            filesystem_entries += 1
            if filesystem_entries > PUBLICATION_MAX_ENTRIES:
                finding("filesystem-entry-limit-uninspected", str(path).encode())
                return findings
            relative_name = candidate.relative_to(path).as_posix() if directory_input else candidate.name
            if candidate.is_symlink():
                finding("symlink-uninspected", str(candidate).encode())
            elif candidate.is_file():
                remaining = min(PUBLICATION_MAX_BYTES, max(0, PUBLICATION_MAX_TOTAL - total))
                size = candidate.stat().st_size
                if size > PUBLICATION_MAX_BYTES:
                    finding("file-size-limit-uninspected", str(candidate).encode())
                    continue
                if size > remaining:
                    finding("aggregate-size-limit-uninspected", relative_name.encode())
                    return findings
                with candidate.open("rb") as stream:
                    data = stream.read(remaining + 1)
                if len(data) > remaining:
                    finding("file-growth-uninspected", relative_name.encode())
                    return findings
                inspect(relative_name, data)
            elif candidate.is_dir():
                if private_publication_name(relative_name):
                    finding("private-publication-filename", relative_name.encode())
            elif not candidate.is_dir():
                finding("missing-publication-input", str(candidate).encode())
    for name, data in payloads:
        inspect(name, data)
    return findings


def publication_git_findings(revision_range: str, pattern_file: Path | None = None) -> list[PublicationFinding]:
    # Inspect the actual newly reachable objects, including files introduced and
    # deleted before the final tree. Filenames and commit messages are data too.
    process = subprocess.Popen(["git", "rev-list", "--objects", "--no-object-names",
                                "--max-count=4097", "--end-of-options", revision_range, "--"],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    objects: list[bytes] = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            oid = line.strip()
            if not re.fullmatch(rb"(?:[a-f0-9]{40}|[a-f0-9]{64})", oid):
                raise ValueError("unrecognized Git object identity")
            if len(objects) >= PUBLICATION_MAX_ENTRIES:
                return [PublicationFinding("git-object-limit-uninspected", hashlib.sha256(oid).hexdigest())]
            objects.append(oid)
        if process.wait() != 0:
            raise ValueError("Git publication range unavailable")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
    if not objects:
        return []
    descriptors = subprocess.run(["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                                 input=b"\n".join(objects) + b"\n", cwd=ROOT,
                                 capture_output=True, check=True).stdout.splitlines()
    if len(descriptors) != len(objects):
        raise ValueError("Git object inventory mismatch")
    admitted = []
    total = 0
    for oid, descriptor in zip(objects, descriptors):
        fields = descriptor.split()
        if len(fields) != 3 or fields[0] != oid or fields[1] not in {b"commit", b"blob", b"tree", b"tag"}:
            raise ValueError("unrecognized Git object descriptor")
        size = int(fields[2])
        total += size
        if size > PUBLICATION_MAX_BYTES or total > PUBLICATION_MAX_TOTAL:
            return [PublicationFinding("git-size-limit-uninspected", hashlib.sha256(oid).hexdigest())]
        admitted.append((oid.decode(), fields[1].decode(), size))
    findings: list[PublicationFinding] = []

    def object_payloads():
        for oid, kind, size in admitted:
            data = subprocess.run(["git", "cat-file", kind, oid], cwd=ROOT,
                                  capture_output=True, check=True).stdout
            if len(data) != size:
                raise ValueError("Git object size changed")
            if kind == "tree":
                offset = count = 0
                while offset < len(data):
                    count += 1
                    if count > PUBLICATION_MAX_ENTRIES:
                        findings.append(PublicationFinding("git-tree-entry-limit-uninspected", hashlib.sha256(data).hexdigest()))
                        break
                    end = data.find(b"\0", offset)
                    if end < 0 or b" " not in data[offset:end]:
                        raise ValueError("unrecognized Git tree metadata")
                    entry = data[offset:end].split(b" ", 1)[1]
                    if private_publication_name(entry.decode("utf-8", errors="replace")):
                        findings.append(PublicationFinding("private-publication-filename", hashlib.sha256(entry).hexdigest()))
                    offset = end + 1 + len(oid) // 2
                if offset > len(data):
                    raise ValueError("truncated Git tree identity")
            if kind == "commit":
                emails = subprocess.run(["git", "show", "-s", "--format=%ae%x00%ce", oid],
                                        cwd=ROOT, capture_output=True, check=True).stdout.strip().split(b"\0")
                if len(emails) != 2 or any(not re.fullmatch(
                        rb"(?:[A-Za-z0-9+_.-]+@users\.noreply\.github\.com|noreply@github\.com)", email)
                        for email in emails):
                    findings.append(PublicationFinding("non-noreply-git-metadata", hashlib.sha256(data).hexdigest()))
            yield "git-" + kind, data

    findings.extend(publication_findings([], pattern_file, object_payloads()))
    return findings


@dataclass(frozen=True)
class Finding:
    path: Path
    label: str
    line: int | None = None

    def render(self) -> str:
        rel = self.path.relative_to(ROOT)
        return f"{rel}:{self.line}: {self.label}" if self.line else f"{rel}: {self.label}"


def companion_uses_native_source(manifest: dict) -> bool:
    companion, native = manifest.get("nativeCompanion"), manifest.get("nativeDistribution")
    if not isinstance(companion, dict) or not isinstance(native, dict) or "delta" in companion:
        return False
    values = (companion.get("sourceCommit"), companion.get("sourceTree"),
              native.get("referenceCommit"), native.get("referenceTree"), native.get("normalizedTree"))
    return (all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) for value in values)
            and values[0] == values[2] and values[1] == values[3] == values[4])


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
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
            companion = current.get("nativeCompanion")
            has_companion_source = ((isinstance(companion, dict) and "delta" in companion)
                                    or companion_uses_native_source(current))
            runtime = package.load_package(ROOT / "runtime", native_distribution=True,
                                           native_companion=has_companion_source)
            if runtime.native is not None:
                native_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["nativeDistribution"]
                findings.extend(validate_native_provenance(runtime.native.artifact, native_manifest.get("provenanceFile")))
        except (package.ReconstructionError, OSError, ValueError, KeyError, TypeError) as error:
            findings.append(Finding(manifest_path, f"invalid reconstruction package: {error}"))
    for path in ROOT.rglob("*"):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.is_symlink():
            findings.append(Finding(path, "symlinks are not allowed in this tree"))
    return findings


def validate_native_provenance(artifact, provenance_file: str | None = None) -> list[Finding]:
    manifest = ROOT / "runtime/manifest.json"
    if (not isinstance(provenance_file, str) or not provenance_file
            or Path(provenance_file).is_absolute()
            or any(part in {"", ".", ".."} for part in provenance_file.split("/"))
            or Path(provenance_file).suffix != ".json"):
        return [Finding(manifest, "native provenance must name a confined runtime package JSON file")]
    path = ROOT / "runtime" / provenance_file
    try:
        if path.is_symlink() or not path.resolve().is_relative_to((ROOT / "runtime").resolve()):
            return [Finding(path, "native provenance escapes the runtime package")]
        provenance = json.loads(path.read_text(encoding="utf-8"))
        expected = {
            "archiveName": artifact.file, "publicAssetURL": artifact.url,
            "bytes": artifact.bytes, "sha256": artifact.sha256,
            "integrity": "sha512-" + base64.b64encode(bytes.fromhex(artifact.sha512)).decode("ascii"),
        }
        findings = [Finding(path, f"native provenance differs from reconstruction artifact: {key}")
                    for key, value in expected.items() if provenance["package"].get(key) != value]
        patch = provenance["source"]["patch"]
        if (not isinstance(patch["path"], str) or Path(patch["path"]).name != patch["path"]
                or not patch["path"].endswith(".patch") or (path.parent / patch["path"]).is_symlink()):
            return findings + [Finding(path, "native source patch must name the adjacent source patch")]
        raw = (path.parent / patch["path"]).read_bytes()
        if len(raw) != patch["bytes"] or hashlib.sha256(raw).hexdigest() != patch["sha256"]:
            findings.append(Finding(path, "native source patch size or SHA-256 differs from provenance"))
        return findings
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return [Finding(path, "native provenance cannot be verified")]


def validate_expected_reference(source_commit: str | None, distribution_commit: str | None,
                                companion_commit: str | None = None) -> list[Finding]:
    if source_commit is None and distribution_commit is None and companion_commit is None:
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
        if companion_commit is not None:
            companion = manifest.get("nativeCompanion")
            if not isinstance(companion, dict) or companion.get("sourceCommit") != companion_commit:
                findings.append(Finding(path, f"stale companion reference: expected reviewed commit {companion_commit}"))
            elif not isinstance(companion.get("delta"), dict) and not companion_uses_native_source(manifest):
                findings.append(Finding(path, "expected companion source has no reconstructable delta or exact native identity"))
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
    parser.add_argument("--publication-input", type=Path, action="append", default=[],
                        help="inspect exact outgoing files/directories instead of structural checks")
    parser.add_argument("--publication-patterns", type=Path,
                        help="private operator JSON patterns; never commit or upload this file")
    parser.add_argument("--publication-git-range", help="also require NoReply metadata in the outgoing Git range")
    parser.add_argument("--expected-source-commit", type=exact_commit, metavar="SHA",
                        help="require the public package to match this reviewed canonical source commit")
    parser.add_argument("--expected-distribution-commit", type=exact_commit, metavar="SHA",
                        help="require the public package to match this reviewed distribution commit")
    parser.add_argument("--expected-companion-commit", type=exact_commit, metavar="SHA",
                        help="require the separately reconstructable companion to match this reviewed source commit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.publication_patterns and not args.publication_input:
        print("publication: private patterns require explicit publication inputs", file=sys.stderr)
        return 1
    if args.publication_input or args.publication_git_range:
        try:
            publication = publication_findings(args.publication_input, args.publication_patterns)
            if args.publication_git_range:
                publication.extend(publication_git_findings(args.publication_git_range, args.publication_patterns))
        except (OSError, ValueError, re.error, subprocess.CalledProcessError):
            print("publication: inspection failed; output remains unqualified", file=sys.stderr)
            return 1
        for finding in publication:
            print(finding.render(), file=sys.stderr)
        if not publication and not args.quiet:
            print("publication checks passed for the supplied inputs; unknown encodings and visual content require review")
        return 1 if publication else 0
    files = candidate_files()

    findings = validate_tree()
    findings.extend(validate_expected_reference(args.expected_source_commit, args.expected_distribution_commit,
                                                args.expected_companion_commit))
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
