#!/usr/bin/env python3
"""Check or apply the reference runtime patch to a clean standalone checkout.

Uses only Python's standard library and Git. The explicit native option also
provisions its pinned public release archive. Never installs, builds, commits,
resets a checkout, or changes the selected runtime.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_RELEASE_PATH = "/josephbergvinson/openclaw-control-plane/releases/download/"
NATIVE_DELTA_PATHS = {"pnpm-lock.yaml", "pnpm-workspace.yaml"}
NATIVE_COMPANION_DELTA_PATHS = {
    "apps/macos/Sources/OpenClaw/NativeCredentialPromptContext.swift",
    "apps/macos/Tests/OpenClawIPCTests/NativeCredentialEntryTests.swift",
    "apps/shared/OpenClawKit/Sources/OpenClawKit/NativeCredentialBinding.swift",
    "apps/shared/OpenClawKit/Tests/OpenClawKitTests/NativeCredentialKeychainPurposeTests.swift",
    "docs/reference/database-schemas/versioning.md",
    "src/state/openclaw-agent-schema.sql",
    "src/trajectory/runtime-store.sqlite.test.ts",
    "src/trajectory/runtime-store.sqlite.ts",
}
ARCHIVE_CHUNK_BYTES = 1024 * 1024
CURRENT_MANIFEST_FIELDS = {
    "schemaVersion", "upstream", "source", "patch", "toolchain",
    "nativeDistribution", "nativeCompanion", "referenceDeployment", "license",
}


class ReconstructionError(Exception):
    """The package or destination does not satisfy the reconstruction contract."""


class ArtifactCleanupError(ReconstructionError):
    def __init__(self, operation_error: BaseException, cleanup_error: Exception):
        super().__init__(f"Native archive operation failed ({operation_error}); temporary cleanup also failed ({cleanup_error})")
        self.operation_error = operation_error
        self.cleanup_error = cleanup_error


@dataclass(frozen=True)
class NativeArtifact:
    file: str
    url: str
    bytes: int
    sha256: str
    sha512: str


@dataclass(frozen=True)
class NativeDistribution:
    source_tree: str
    normalized_tree: str
    patch_sha256: str
    patch: bytes
    artifact: NativeArtifact
    reference_commit: str | None = None
    reference_tree: str | None = None


@dataclass(frozen=True)
class NativeCompanion:
    source_commit: str
    source_tree: str
    distribution_tree: str
    patch_sha256: str | None
    patch: bytes | None
    delta_paths: frozenset[str] = frozenset(NATIVE_COMPANION_DELTA_PATHS)
    normalized_tree: str | None = None
    normalization_paths: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SourcePackage:
    tag: str
    tag_object: str
    base_commit: str
    base_tree: str
    normalized_tree: str
    patch_sha256: str
    patch: bytes
    native: NativeDistribution | None = None
    companion: NativeCompanion | None = None


def require_digest(value: object, length: int, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise ReconstructionError(f"Manifest requires a full {label}")
    return value


def validate_manifest_metadata(manifest: dict, *, native_distribution: bool = False) -> None:
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise ReconstructionError("Unsupported runtime manifest version")
    if set(manifest) - CURRENT_MANIFEST_FIELDS:
        raise ReconstructionError("The current manifest contains fields outside the reconstruction contract")
    sections = ("source", "nativeDistribution", "nativeCompanion") if native_distribution else ("source",)
    for name in sections:
        section = manifest.get(name)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ReconstructionError(f"Manifest {name} must be an object")
        for key in ("referenceCommit", "referenceTree", "sourceCommit", "sourceTree"):
            if key in section:
                require_digest(section[key], 40, f"{name}.{key} SHA-1")
    deployment = manifest.get("referenceDeployment") if native_distribution else None
    if deployment is not None:
        if not isinstance(deployment, dict):
            raise ReconstructionError("Manifest referenceDeployment must be an object")
        # An installed reference can precede the source being reconstructed.
        # Its identity is descriptive and never substitutes for the native tree.
        require_digest(deployment.get("distributionCommit"), 40,
                       "reference deployment distribution commit SHA-1")


def validate_release_url(url: str, filename: str) -> None:
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(?:tgz|tar\.gz)", filename):
        raise ReconstructionError("Native archive must name one regular archive filename")
    if not isinstance(url, str):
        raise ReconstructionError("Native archive requires a bound public release URL")
    parsed = urlsplit(url)
    prefix = PUBLIC_RELEASE_PATH
    if (
        parsed.scheme != "https" or parsed.netloc != "github.com"
        or parsed.query or parsed.fragment or not parsed.path.startswith(prefix)
    ):
        raise ReconstructionError("Native archive must use this repository's public GitHub release URL")
    parts = parsed.path[len(prefix):].split("/")
    if len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", parts[0]) or parts[1] != filename:
        raise ReconstructionError("Native release URL must name the declared archive exactly")


def read_resource_input(path: Path, limit: int) -> bytes:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ReconstructionError("Native resource input must be a physical absolute path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ReconstructionError("Native resource input is not a bounded regular file")
        payload = stream.read(limit + 1)
        after = path.lstat()
        if len(payload) > limit or (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns
        ) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ReconstructionError("Native resource input changed while being read")
        return payload


def unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ReconstructionError("Native provenance has a duplicate JSON key")
        value[key] = item
    return value


def load_native_provenance(path: Path) -> tuple[NativeArtifact, dict, bytes]:
    """Read the existing candidate packet; no active runtime manifest is selected."""
    raw = read_resource_input(path, 256 * 1024)
    value = json.loads(raw, object_pairs_hook=unique_json_object)
    if not isinstance(value, dict) or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise ReconstructionError("Unsupported native provenance schema")
    package = value.get("package")
    if (
        not isinstance(package, dict)
        or package.get("name") != "@openai/codex"
        or package.get("platformPackage") != "@openai/codex"
        or package.get("target") != "aarch64-apple-darwin"
        or not isinstance(package.get("version"), str)
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[A-Za-z0-9.-]+-darwin-arm64",
                        package["version"]) is None
        or type(package.get("bytes")) is not int or package["bytes"] <= 0
    ):
        raise ReconstructionError("Native provenance package identity or size is invalid")
    integrity = package.get("integrity")
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise ReconstructionError("Native provenance requires one canonical SHA-512 SRI")
    try:
        digest = base64.b64decode(integrity[7:], validate=True)
    except (ValueError, binascii.Error) as error:
        raise ReconstructionError("Native provenance SHA-512 SRI is invalid") from error
    if len(digest) != 64 or "sha512-" + base64.b64encode(digest).decode("ascii") != integrity:
        raise ReconstructionError("Native provenance requires one canonical SHA-512 SRI")
    validate_release_url(package.get("publicAssetURL"), package.get("archiveName"))
    artifact = NativeArtifact(
        package["archiveName"], package["publicAssetURL"], package["bytes"],
        require_digest(package.get("sha256"), 64, "native archive SHA-256"), digest.hex(),
    )
    return artifact, package, raw


def yaml_scalar(value: str) -> str:
    """Only the ordinary scalar spellings emitted in the admitted pnpm files."""
    if value.startswith('"'):
        decoded = json.loads(value)
        if not isinstance(decoded, str):
            raise ReconstructionError("Native dependency scalar is not a string")
        return decoded
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if not value or any(char in value for char in "&*!{}[],#\t\r\n"):
        raise ReconstructionError("Native dependency uses an unsupported scalar")
    return value


def yaml_mapping_entry(source: str, section: str, key: str) -> tuple[str, list[str]]:
    """Select one direct entry, refusing duplicate/ambiguous binding spellings.

    This is a narrow admission of pnpm's existing scalar/inline-map form, not a
    general YAML reader. Frozen pnpm remains the dependency graph owner.
    """
    lines = source.splitlines()
    starts = [index for index, line in enumerate(lines) if line == section + ":"]
    if len(starts) != 1:
        raise ReconstructionError("Native dependency mapping is missing or duplicated")
    start = starts[0] + 1
    end = next((index for index in range(start, len(lines))
                if lines[index] and not lines[index].startswith((" ", "#"))), len(lines))
    found = []
    for index in range(start, end):
        line = lines[index]
        if not line.startswith("  ") or line.startswith(("   ", "  #")) or not line.strip():
            continue
        match = re.fullmatch(r"""  ('(?:[^']|'')*'|"(?:[^"\\]|\\.)*"|[^'"\s][^:]*):(?: (.*))?""", line)
        if match is None:
            raise ReconstructionError("Native dependency mapping uses an unsupported key")
        if yaml_scalar(match.group(1)) == key:
            tail = next((cursor for cursor in range(index + 1, end)
                         if lines[cursor].startswith("  ") and not lines[cursor].startswith("   ")
                         and lines[cursor].strip() and not lines[cursor].startswith("  #")), end)
            found.append((match.group(2) or "", lines[index + 1:tail]))
    if len(found) != 1:
        raise ReconstructionError("Native dependency entry is missing or duplicated")
    return found[0]


def pnpm_dependency_document(source: str) -> str:
    # Same two-document framing as pnpm and the existing source bootstrap owner
    # scripts/lib/pnpm-lockfile-documents.mjs; never inspect the env document as
    # the project's package graph.
    text = source.removeprefix("\ufeff").replace("\r\n", "\n")
    separator = "\n---\n"
    if not text.startswith("---\n"):
        if separator in text:
            raise ReconstructionError("Native lock has an unexpected document separator")
        return text
    boundary = text.find(separator, 4)
    if boundary < 0 or separator in text[boundary + len(separator):]:
        raise ReconstructionError("Native lock must have exactly environment and dependency documents")
    return text[boundary + len(separator):]


def validate_native_dependency_binding(checkout: Path, artifact: NativeArtifact, package: dict) -> dict:
    workspace = read_resource_input(checkout / "pnpm-workspace.yaml", 1024 * 1024)
    lock = read_resource_input(checkout / "pnpm-lock.yaml", 16 * 1024 * 1024)
    lock_source = pnpm_dependency_document(lock.decode("utf-8"))
    version = package["version"].split("-", 1)[0]
    selector = "@openai/codex@" + version + ">@openai/codex-darwin-arm64"
    relative = "native-package-artifacts/" + artifact.file
    for source in (workspace.decode("utf-8"), lock_source):
        selected, children = yaml_mapping_entry(source, "overrides", selector)
        if children and any(line.strip() for line in children):
            raise ReconstructionError("Native override is not a scalar")
        if yaml_scalar(selected) not in ("file:" + relative, "file:./" + relative):
            raise ReconstructionError("Native dependency override differs from provenance")
    selected, children = yaml_mapping_entry(
        lock_source, "packages", "@openai/codex@file:" + relative)
    if selected:
        raise ReconstructionError("Native lock package is not a mapping")
    values = {}
    for line in children:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"    ([A-Za-z]+): (.*)", line)
        if match is None or match.group(1) in values:
            raise ReconstructionError("Native lock package mapping is ambiguous")
        values[match.group(1)] = match.group(2)
    resolution = "{integrity: " + package["integrity"] + ", tarball: file:" + relative + "}"
    if (
        values.get("resolution") != resolution
        or yaml_scalar(values.get("version", "")) != package["version"]
        or values.get("os") != "[darwin]" or values.get("cpu") != "[arm64]"
    ):
        raise ReconstructionError("Native lock resolution, version or platform differs from provenance")
    selected, children = yaml_mapping_entry(
        lock_source, "snapshots", "@openai/codex@file:" + relative)
    if selected or [line.strip() for line in children if line.strip() and not line.lstrip().startswith("#")] != ["optional: true"]:
        raise ReconstructionError("Native lock snapshot must retain the optional platform package")
    return {"pnpm-workspace.yaml": hashlib.sha256(workspace).hexdigest(),
            "pnpm-lock.yaml": hashlib.sha256(lock).hexdigest()}


def provision_native_from_provenance(checkout: Path, provenance: Path) -> dict:
    """Hydrate one bound dependency only; never apply source or select a runtime."""
    checkout = checkout.expanduser().resolve(strict=True)
    artifact, package, raw = load_native_provenance(provenance)
    before = validate_native_dependency_binding(checkout, artifact, package)
    result = provision_native_artifact(checkout, artifact)
    if read_resource_input(provenance, 256 * 1024) != raw or (
        validate_native_dependency_binding(checkout, artifact, package) != before
    ):
        raise ReconstructionError("Native resource/dependency binding changed during provisioning")
    return {"status": "artifact-provisioned-without-source-or-runtime-selection",
            "provenance": {"path": str(provenance), "sha256": hashlib.sha256(raw).hexdigest()},
            "dependencyInputs": before, "artifact": result}


def load_native_distribution(directory: Path, manifest: dict, source_tree: str) -> NativeDistribution:
    native = manifest.get("nativeDistribution")
    if not isinstance(native, dict):
        raise ReconstructionError("The runtime manifest has no bound native distribution")
    base = require_digest(native["sourceTree"], 40, "native source tree SHA-1")
    normalized = require_digest(native["normalizedTree"], 40, "native normalized tree SHA-1")
    if base != source_tree:
        raise ReconstructionError("Native distribution source tree must match the normalized runtime source")
    patch = native["patch"]
    name = patch["file"]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.patch", name):
        raise ReconstructionError("Native patch must name a file inside the runtime package")
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise ReconstructionError("Native patch must be a regular package file")
    raw = path.read_bytes()
    digest = require_digest(patch["sha256"], 64, "native patch SHA-256")
    if type(patch["bytes"]) is not int or patch["bytes"] != len(raw) or hashlib.sha256(raw).hexdigest() != digest:
        raise ReconstructionError("Native patch size or SHA-256 does not match the manifest")
    artifact = native["artifact"]
    filename = artifact["file"]
    if type(artifact["bytes"]) is not int or artifact["bytes"] <= 0:
        raise ReconstructionError("Native archive requires an exact positive byte count")
    validate_release_url(artifact["url"], filename)
    return NativeDistribution(base, normalized, digest, raw, NativeArtifact(
        filename, artifact["url"], artifact["bytes"],
        require_digest(artifact["sha256"], 64, "native archive SHA-256"),
        require_digest(artifact["sha512"], 128, "native archive SHA-512"),
    ), native.get("referenceCommit"), native.get("referenceTree"))


def validate_companion_paths(paths: frozenset[str]) -> None:
    """Admit only an explicit finite set of ordinary source paths."""
    if not isinstance(paths, frozenset) or not paths:
        raise ReconstructionError("Companion delta requires a nonempty exact source-path set")
    for name in paths:
        if (not isinstance(name, str) or not name.startswith(("apps/", "src/", "extensions/", "test/", "docs/"))
                or "\\" in name or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or any(part in ("", ".", "..", ".git", "node_modules") for part in name.split("/"))):
            raise ReconstructionError("Companion delta contains an unsupported source path")


def validate_companion_normalization(companion: NativeCompanion) -> None:
    if companion.normalized_tree is None:
        if companion.normalization_paths:
            raise ReconstructionError("Companion normalization paths require a normalized tree")
        return
    require_digest(companion.normalized_tree, 40, "normalized companion tree SHA-1")
    if companion.patch is None or companion.normalized_tree == companion.source_tree:
        raise ReconstructionError("Companion normalization requires a distinct bound replay tree and delta")
    validate_companion_paths(companion.normalization_paths)


def load_native_companion(directory: Path, manifest: dict, native: NativeDistribution) -> NativeCompanion:
    companion = manifest.get("nativeCompanion")
    if not isinstance(companion, dict) or companion.get("platform") != "darwin-arm64":
        raise ReconstructionError("The runtime manifest has no bound macOS companion")
    source_commit = require_digest(companion.get("sourceCommit"), 40, "companion source commit SHA-1")
    source_tree = require_digest(companion.get("sourceTree"), 40, "companion source tree SHA-1")
    normalized_tree, normalization_paths = None, frozenset()
    if "normalizedTree" in companion or "normalization" in companion:
        normalized_tree = require_digest(companion.get("normalizedTree"), 40,
                                         "normalized companion tree SHA-1")
        normalization = companion.get("normalization")
        if (not isinstance(normalization, dict) or set(normalization) != {"scope", "paths"}
                or not isinstance(normalization.get("scope"), str) or not normalization["scope"].strip()):
            raise ReconstructionError("Companion normalized tree requires its explicit fixture normalization")
        declared = normalization["paths"]
        source = manifest.get("source")
        source_normalization = source.get("normalization") if isinstance(source, dict) else None
        source_paths = source_normalization.get("paths") if isinstance(source_normalization, dict) else None
        if (not isinstance(declared, list) or not declared or not all(isinstance(name, str) for name in declared)
                or declared != sorted(set(declared)) or declared != source_paths):
            raise ReconstructionError("Companion fixture paths must exactly match the declared source normalization")
        normalization_paths = frozenset(declared)
        validate_companion_paths(normalization_paths)
    if "delta" not in companion:
        if normalized_tree is not None:
            raise ReconstructionError("Companion normalization requires a bound delta")
        if (source_commit != native.reference_commit or source_tree != native.reference_tree
                or source_tree != native.normalized_tree):
            raise ReconstructionError("Companion without a delta must exactly match the native distribution commit and tree")
        return NativeCompanion(source_commit, source_tree, native.normalized_tree, None, None)
    delta = companion["delta"]
    if not isinstance(delta, dict) or set(delta) not in ({"distributionTree", "patch"},
                                                       {"distributionTree", "patch", "paths"}):
        raise ReconstructionError("Companion reconstruction requires one bound finite delta")
    paths = frozenset(NATIVE_COMPANION_DELTA_PATHS)
    if "paths" in delta:
        declared = delta["paths"]
        if (not isinstance(declared, list) or not declared or not all(isinstance(path, str) for path in declared)
                or declared != sorted(set(declared))):
            raise ReconstructionError("Companion delta paths must be a sorted unique nonempty list")
        paths = frozenset(declared)
    validate_companion_paths(paths)
    base = require_digest(delta["distributionTree"], 40, "companion distribution tree SHA-1")
    if base != native.normalized_tree:
        raise ReconstructionError("Companion base must match the normalized native distribution")
    patch = delta["patch"]
    if not isinstance(patch, dict) or set(patch) != {"file", "bytes", "sha256"}:
        raise ReconstructionError("Companion delta requires one exact patch binding")
    name = patch["file"]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.patch", name):
        raise ReconstructionError("Companion patch must name a file inside the runtime package")
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise ReconstructionError("Companion patch must be a regular package file")
    raw = path.read_bytes()
    digest = require_digest(patch["sha256"], 64, "companion patch SHA-256")
    if type(patch["bytes"]) is not int or patch["bytes"] <= 0 or patch["bytes"] != len(raw) or hashlib.sha256(raw).hexdigest() != digest:
        raise ReconstructionError("Companion patch size or SHA-256 does not match the manifest")
    result = NativeCompanion(source_commit, source_tree, base, digest, raw, paths,
                             normalized_tree, normalization_paths)
    validate_companion_normalization(result)
    return result


def load_package(directory: Path, *, native_distribution: bool = False,
                 native_companion: bool = False) -> SourcePackage:
    if native_companion and not native_distribution:
        raise ReconstructionError("Companion reconstruction requires the native distribution")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    validate_manifest_metadata(manifest, native_distribution=native_distribution)
    upstream = manifest["upstream"]
    patch = manifest["patch"]
    name = patch["file"]
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".patch"):
        raise ReconstructionError("Patch must name a file inside the runtime package")
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise ReconstructionError("Patch must be a regular package file, not a symlink")
    raw = path.read_bytes()
    digest = require_digest(patch["sha256"], 64, "patch SHA-256")
    if type(patch["bytes"]) is not int or patch["bytes"] != len(raw) or hashlib.sha256(raw).hexdigest() != digest:
        raise ReconstructionError("Patch size or SHA-256 does not match the manifest")
    package = SourcePackage(
        upstream["tag"], upstream["tagObject"], upstream["commit"], upstream["tree"],
        manifest["source"]["normalizedTree"], digest, raw,
    )
    for value in (package.tag_object, package.base_commit, package.base_tree, package.normalized_tree):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ReconstructionError("Manifest requires full Git SHA-1 object IDs")
    if not isinstance(package.tag, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", package.tag):
        raise ReconstructionError("Manifest requires an explicit stable version tag")
    if not isinstance(package.patch_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", package.patch_sha256):
        raise ReconstructionError("Manifest requires a full patch SHA-256")
    if native_distribution:
        package = SourcePackage(
            package.tag, package.tag_object, package.base_commit, package.base_tree,
            package.normalized_tree, package.patch_sha256, package.patch,
            load_native_distribution(directory, manifest, package.normalized_tree),
        )
    if native_companion:
        assert package.native is not None
        package = SourcePackage(
            package.tag, package.tag_object, package.base_commit, package.base_tree,
            package.normalized_tree, package.patch_sha256, package.patch, package.native,
            load_native_companion(directory, manifest, package.native),
        )
    return package


class PublicAssetRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "release-assets.githubusercontent.com" or parsed.fragment:
            raise ReconstructionError("Native release redirected outside GitHub's public asset host")
        return super().redirect_request(request, response, code, message, headers, url)


def verify_archive(stream: BinaryIO, artifact: NativeArtifact, output: BinaryIO | None = None) -> None:
    sha256, sha512 = hashlib.sha256(), hashlib.sha512()
    size = 0
    while chunk := stream.read(ARCHIVE_CHUNK_BYTES):
        size += len(chunk)
        if size > artifact.bytes:
            raise ReconstructionError("Native archive exceeds the declared byte count")
        sha256.update(chunk)
        sha512.update(chunk)
        if output is not None:
            output.write(chunk)
    if size != artifact.bytes or sha256.hexdigest() != artifact.sha256 or sha512.hexdigest() != artifact.sha512:
        raise ReconstructionError("Native archive size, SHA-256 or SHA-512 does not match the manifest")


def verify_existing_archive(directory_fd: int, artifact: NativeArtifact) -> bool:
    try:
        fd = os.open(artifact.file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ReconstructionError("Existing native archive is not a regular file")
        verify_archive(stream, artifact)
        after = os.stat(artifact.file, dir_fd=directory_fd, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ReconstructionError("Existing native archive changed while being verified")
    return True


def same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return stat.S_ISREG(second.st_mode) and (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def remove_owned_temporary(directory_fd: int, name: str, owned: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not same_file(owned, current):
        raise ReconstructionError("Native temporary archive name changed; the replacement was retained")
    os.unlink(name, dir_fd=directory_fd)


def provision_native_artifact(checkout: Path, artifact: NativeArtifact) -> dict:
    validate_release_url(artifact.url, artifact.file)
    directory = checkout / "native-package-artifacts"
    directory.mkdir(exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = "." + artifact.file + "." + uuid.uuid4().hex + ".partial"
    try:
        if verify_existing_archive(directory_fd, artifact):
            source = "existing"
        else:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            owned = os.fstat(fd)
            operation_error = None
            try:
                with os.fdopen(fd, "wb") as output:
                    # No proxy credentials, authentication handlers, or cookies are inherited.
                    opener = build_opener(ProxyHandler({}), PublicAssetRedirects())
                    request = Request(artifact.url, headers={"Accept": "application/octet-stream"})
                    with opener.open(request, timeout=60) as response:
                        verify_archive(response, artifact, output)
                        output.flush()
                        os.fchmod(output.fileno(), 0o644)
                        os.fsync(output.fileno())
                    if not same_file(owned, os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)):
                        raise ReconstructionError("Native temporary archive changed before publication")
                    try:
                        # Linking never replaces a concurrently created destination.
                        os.link(temporary, artifact.file, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
                        exposed = os.stat(artifact.file, dir_fd=directory_fd, follow_symlinks=False)
                        if not same_file(os.fstat(output.fileno()), exposed):
                            raise ReconstructionError("Published native archive is not the verified temporary file")
                        source = "downloaded"
                    except FileExistsError:
                        source = "existing"
                    if not verify_existing_archive(directory_fd, artifact):
                        raise ReconstructionError("Native archive disappeared during publication")
            except BaseException as error:
                operation_error = error
                raise
            finally:
                try:
                    remove_owned_temporary(directory_fd, temporary, owned)
                except Exception as cleanup_error:
                    if operation_error is not None:
                        raise ArtifactCleanupError(operation_error, cleanup_error) from operation_error
                    raise
        exposed_directory = directory.lstat()
        owned_directory = os.fstat(directory_fd)
        if (exposed_directory.st_dev, exposed_directory.st_ino) != (owned_directory.st_dev, owned_directory.st_ino):
            raise ReconstructionError("Native archive directory changed during provisioning")
    finally:
        os.close(directory_fd)
    return {
        "path": "native-package-artifacts/" + artifact.file,
        "bytes": artifact.bytes, "sha256": artifact.sha256, "sha512": artifact.sha512,
        "verified": True, "source": source,
    }


def git(checkout: Path, *args: str, data: bytes | None = None) -> str:
    # Inherited Git redirection must not move writes outside the supplied checkout.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    command = [
        "git", "-c", "protocol.allow=never", "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null", "-c", "core.fileMode=true", *args,
    ]
    result = subprocess.run(command, cwd=checkout, env=env, input=data, capture_output=True)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReconstructionError(f"Git {args[0]} failed: {detail}")
    return result.stdout.decode("utf-8").strip()


def reconstruct(checkout: Path, package: SourcePackage, *, apply: bool = False) -> dict:
    if package.companion is not None and package.native is None:
        raise ReconstructionError("Companion reconstruction requires the native distribution")
    if package.native is not None and not apply:
        raise ReconstructionError("Native distribution requires --apply; no source or archive was changed")
    checkout = checkout.expanduser().resolve(strict=True)
    if not checkout.is_dir() or not (checkout / ".git").is_dir() or (checkout / ".git").is_symlink():
        raise ReconstructionError("Use a standalone clone with its own .git directory")
    if Path(git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
        raise ReconstructionError("Supply the checkout root, not a nested directory")
    if Path(git(checkout, "rev-parse", "--absolute-git-dir")).resolve() != checkout / ".git":
        raise ReconstructionError("The checkout must own its Git directory")
    if (checkout / ".git" / "objects" / "info" / "alternates").exists():
        raise ReconstructionError("Shared object stores are unsupported; use an independent clone")
    if hashlib.sha256(package.patch).hexdigest() != package.patch_sha256:
        raise ReconstructionError("Patch SHA-256 does not match the manifest")
    if package.native is not None:
        native = package.native
        if native.source_tree != package.normalized_tree or hashlib.sha256(native.patch).hexdigest() != native.patch_sha256:
            raise ReconstructionError("Native delta does not match its normalized source or SHA-256")
        changes = git(checkout, "apply", "--numstat", "-z", "-", data=native.patch).split("\0")
        paths = []
        for change in filter(None, changes):
            fields = change.split("\t")
            if len(fields) != 3 or not all(value.isdigit() for value in fields[:2]):
                raise ReconstructionError("Native delta must contain ordinary text changes")
            paths.append(fields[2])
        if len(paths) != 2 or set(paths) != NATIVE_DELTA_PATHS:
            raise ReconstructionError("Native delta must change only pnpm-lock.yaml and pnpm-workspace.yaml")
    if package.companion is not None:
        companion = package.companion
        validate_companion_normalization(companion)
        if companion.distribution_tree != package.native.normalized_tree:
            raise ReconstructionError("Companion delta does not match its native distribution or SHA-256")
        for value in (companion.source_commit, companion.source_tree):
            require_digest(value, 40, "companion Git object SHA-1")
        if companion.patch is None:
            if (companion.patch_sha256 is not None
                    or companion.source_commit != package.native.reference_commit
                    or companion.source_tree != package.native.reference_tree
                    or companion.source_tree != package.native.normalized_tree):
                raise ReconstructionError("Companion without a delta must exactly match the native distribution commit and tree")
        else:
            if not companion.patch or hashlib.sha256(companion.patch).hexdigest() != companion.patch_sha256:
                raise ReconstructionError("Companion delta does not match its native distribution or SHA-256")
            changes = git(checkout, "apply", "--numstat", "-z", "-", data=companion.patch).split("\0")
            paths = []
            for change in filter(None, changes):
                fields = change.split("\t")
                if len(fields) != 3 or not all(value.isdigit() for value in fields[:2]):
                    raise ReconstructionError("Companion delta must contain ordinary text changes")
                paths.append(fields[2])
            validate_companion_paths(companion.delta_paths)
            if len(paths) != len(companion.delta_paths) or set(paths) != companion.delta_paths:
                raise ReconstructionError("Companion delta must change exactly its declared source paths")
    if git(checkout, "rev-parse", "HEAD") != package.base_commit:
        raise ReconstructionError("Checkout HEAD is not the pinned upstream commit")
    if git(checkout, "rev-parse", "HEAD^{tree}") != package.base_tree:
        raise ReconstructionError("Upstream source tree does not match the manifest")
    tag = "refs/tags/" + package.tag
    if git(checkout, "rev-parse", tag) != package.tag_object:
        raise ReconstructionError("Upstream tag object does not match the manifest")
    if git(checkout, "rev-parse", tag + "^{commit}") != package.base_commit:
        raise ReconstructionError("Upstream tag does not resolve to the pinned commit")
    if git(checkout, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"):
        raise ReconstructionError("Checkout must be clean, including untracked and ignored files")
    git(checkout, "apply", "--check", "--index", "--whitespace=error-all", "-", data=package.patch)
    if apply:
        git(checkout, "apply", "--index", "--whitespace=error-all", "-", data=package.patch)
        tree = git(checkout, "write-tree")
        if tree != package.normalized_tree:
            raise ReconstructionError("Applied tree differs from the manifest; inspect this isolated checkout")
        git(checkout, "diff", "--quiet")
    result = {
        "status": "applied-and-staged" if apply else "checked-without-applying",
        "upstreamCommit": package.base_commit,
        "normalizedTree": package.normalized_tree,
        "normalizedTreeVerified": apply,
        "patchSha256": package.patch_sha256,
    }
    if package.native is not None:
        native = package.native
        git(checkout, "apply", "--check", "--index", "--whitespace=error-all", "-", data=native.patch)
        git(checkout, "apply", "--index", "--whitespace=error-all", "-", data=native.patch)
        if git(checkout, "write-tree") != native.normalized_tree:
            raise ReconstructionError("Native applied tree differs from the manifest; inspect this isolated checkout")
        git(checkout, "diff", "--quiet")
        result["nativeDistribution"] = {
            "normalizedTree": native.normalized_tree, "normalizedTreeVerified": True,
            "patchSha256": native.patch_sha256,
        }
        final_tree = native.normalized_tree
        if package.companion is not None:
            companion = package.companion
            if companion.patch is not None:
                git(checkout, "apply", "--check", "--index", "--whitespace=error-all", "-", data=companion.patch)
                git(checkout, "apply", "--index", "--whitespace=error-all", "-", data=companion.patch)
            final_tree = companion.normalized_tree or companion.source_tree
            if git(checkout, "write-tree") != final_tree:
                raise ReconstructionError("Companion applied tree differs from the manifest; inspect this isolated checkout")
            git(checkout, "diff", "--quiet")
            result["nativeCompanion"] = {
                "sourceCommit": companion.source_commit, "sourceTree": companion.source_tree,
                "normalizedTree": final_tree, "normalizedTreeVerified": True,
            }
            if companion.normalization_paths:
                result["nativeCompanion"]["normalizationPaths"] = sorted(companion.normalization_paths)
            if companion.patch is not None:
                result["nativeCompanion"]["patchSha256"] = companion.patch_sha256
        artifact = provision_native_artifact(checkout, native.artifact)
        if git(checkout, "write-tree") != final_tree:
            raise ReconstructionError("Companion source index changed during provisioning" if package.companion is not None
                                      else "Native source index changed during provisioning")
        git(checkout, "diff", "--quiet")
        result["nativeDistribution"]["artifact"] = artifact
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="existing clean standalone clone of the pinned upstream tag")
    parser.add_argument("--apply", action="store_true", help="apply and stage the patch after checking; never commit")
    parser.add_argument("--native-distribution", action="store_true", help="with --apply, stage the native delta and provision its verified public archive")
    parser.add_argument("--native-companion", action="store_true",
                        help="with --apply --native-distribution, verify the bound macOS companion source, applying its finite delta if needed")
    parser.add_argument("--native-artifact-provenance", type=Path,
                        help="provision only the existing packet-bound local dependency; never apply source or select a runtime")
    args = parser.parse_args(argv)
    if args.native_artifact_provenance is not None and (args.apply or args.native_distribution or args.native_companion):
        parser.error("--native-artifact-provenance cannot be combined with source reconstruction options")
    if args.native_distribution and not args.apply:
        parser.error("--native-distribution requires --apply")
    if args.native_companion and not (args.apply and args.native_distribution):
        parser.error("--native-companion requires --apply and --native-distribution")
    try:
        if args.native_artifact_provenance is not None:
            result = provision_native_from_provenance(args.checkout, args.native_artifact_provenance)
        else:
            result = reconstruct(
                args.checkout, load_package(ROOT / "runtime", native_distribution=args.native_distribution,
                                            native_companion=args.native_companion),
                apply=args.apply,
            )
    except (ReconstructionError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"Reconstruction refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
