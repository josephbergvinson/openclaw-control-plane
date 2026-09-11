"""Apply the reviewed overlay only to its exact reconstructed source generation."""

import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from reconstruct_runtime import git


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply(checkout, bundle):
    manifest = json.loads((bundle / "candidate-overlay.json").read_text())
    # Reconstruction stages the normalized tree without committing it. Verify the
    # same upstream HEAD, exact staged tree, and unchanged tracked worktree.
    if git(checkout, "rev-parse", "HEAD") != manifest["upstreamCommit"]:
        raise ValueError("candidate checkout is not based on the pinned upstream commit")
    git(checkout, "diff", "--cached", "--quiet", manifest["normalizedTree"], "--")
    git(checkout, "diff", "--quiet", "--")
    patch = bundle / "candidate.patch"
    if digest(patch) != manifest["patchSha256"]:
        raise ValueError("candidate patch hash differs")
    baseline = checkout / "test/isolated-queue"
    for name, key in (
        ("adopted-drain-steering.unit-dormant-boundary.test.ts", "baselineFixtureSha256"),
        ("vitest.queue-unit-dormant-boundary.config.ts", "baselineConfigSha256"),
    ):
        if digest(baseline / name) != manifest[key]:
            raise ValueError("baseline fixture/config changed: " + name)
    for entry in manifest["files"]:
        if digest(checkout / entry["path"]) != entry["originalSha256"]:
            raise ValueError("candidate base differs: " + entry["path"])
    git(checkout, "apply", "--check", "--whitespace=error-all", str(patch))
    git(checkout, "apply", "--whitespace=error-all", str(patch))
    for entry in manifest["files"]:
        if digest(checkout / entry["path"]) != entry["candidateSha256"]:
            raise ValueError("candidate result differs: " + entry["path"])
    print(json.dumps({"candidatePatchSha256": manifest["patchSha256"], "filesVerified": len(manifest["files"]), "status": "overlay applied to disposable checkout"}))


if __name__ == "__main__":
    apply(Path(sys.argv[1]).resolve(), Path(__file__).resolve().parent)
