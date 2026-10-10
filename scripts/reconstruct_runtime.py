#!/usr/bin/env python3
"""Check or apply the reference runtime patch to a clean standalone checkout.

Uses only Python's standard library and Git. The explicit native option also
provisions its pinned public release archive. Never installs, builds, commits,
resets a checkout, or changes the selected runtime.
"""

from __future__ import annotations

import argparse
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
ARCHIVE_CHUNK_BYTES = 1024 * 1024


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


def require_digest(value: object, length: int, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise ReconstructionError(f"Manifest requires a full {label}")
    return value


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
    ))


def load_package(directory: Path, *, native_distribution: bool = False) -> SourcePackage:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schemaVersion"] != 1:
        raise ReconstructionError("Unsupported runtime manifest version")
    upstream = manifest["upstream"]
    patch = manifest["patch"]
    name = patch["file"]
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".patch"):
        raise ReconstructionError("Patch must name a file inside the runtime package")
    path = directory / name
    if path.is_symlink():
        raise ReconstructionError("Patch must be a regular package file, not a symlink")
    package = SourcePackage(
        upstream["tag"], upstream["tagObject"], upstream["commit"], upstream["tree"],
        manifest["source"]["normalizedTree"], patch["sha256"], path.read_bytes(),
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
        artifact = provision_native_artifact(checkout, native.artifact)
        if git(checkout, "write-tree") != native.normalized_tree:
            raise ReconstructionError("Native source index changed during provisioning")
        git(checkout, "diff", "--quiet")
        result["nativeDistribution"] = {
            "normalizedTree": native.normalized_tree, "normalizedTreeVerified": True,
            "patchSha256": native.patch_sha256, "artifact": artifact,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="existing clean standalone clone of the pinned upstream tag")
    parser.add_argument("--apply", action="store_true", help="apply and stage the patch after checking; never commit")
    parser.add_argument("--native-distribution", action="store_true", help="with --apply, stage the native delta and provision its verified public archive")
    args = parser.parse_args()
    if args.native_distribution and not args.apply:
        parser.error("--native-distribution requires --apply")
    try:
        result = reconstruct(
            args.checkout, load_package(ROOT / "runtime", native_distribution=args.native_distribution),
            apply=args.apply,
        )
    except (ReconstructionError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"Reconstruction refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
