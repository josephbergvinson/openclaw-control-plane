#!/usr/bin/env python3
"""Check or apply the reference runtime patch to a clean standalone checkout.

Uses only Python's standard library and Git. Never fetches, installs, builds,
commits, resets a checkout, or changes the selected runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReconstructionError(Exception):
    """The package or destination does not satisfy the reconstruction contract."""


@dataclass(frozen=True)
class SourcePackage:
    tag: str
    tag_object: str
    base_commit: str
    base_tree: str
    normalized_tree: str
    patch_sha256: str
    patch: bytes


def load_package(directory: Path) -> SourcePackage:
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
    return package


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
    return {
        "status": "applied-and-staged" if apply else "checked-without-applying",
        "upstreamCommit": package.base_commit,
        "normalizedTree": package.normalized_tree,
        "normalizedTreeVerified": apply,
        "patchSha256": package.patch_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="existing clean standalone clone of the pinned upstream tag")
    parser.add_argument("--apply", action="store_true", help="apply and stage the patch after checking; never commit")
    args = parser.parse_args()
    try:
        result = reconstruct(args.checkout, load_package(ROOT / "runtime"), apply=args.apply)
    except (ReconstructionError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"Reconstruction refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
