"""Bind the reviewed disposable source to a candidate-only native-check manifest."""

import json
from pathlib import Path
import shutil
import sys

from apply_candidate import digest, git


def remove_fixture(checkout, bundle):
    relative = Path("test/isolated-queue")
    owned = checkout / relative
    if owned.is_symlink() or not owned.is_dir():
        raise ValueError("expected owned isolated fixture directory")
    if git(checkout, "ls-files", "--", str(relative)):
        raise ValueError("isolated fixture directory contains tracked source")
    for original in (bundle / relative).iterdir():
        copied = owned / original.name
        if not original.is_file() or copied.is_symlink() or digest(copied) != digest(original):
            raise ValueError("isolated fixture copy changed")
    # Only the disposable copied fixtures and their owned transform caches are
    # removed. The reference bundle and completed result artifacts remain.
    shutil.rmtree(owned)
    print("Removed verified untracked isolated fixtures before native source qualification")


def check_files(checkout, overlay):
    for entry in overlay["files"]:
        if digest(checkout / entry["path"]) != entry["candidateSha256"]:
            raise ValueError("candidate file changed: " + entry["path"])


def verify(checkout, bundle, candidate_manifest):
    overlay = json.loads((bundle / "candidate-overlay.json").read_text())
    manifest = json.loads(candidate_manifest.read_text())
    if manifest.get("candidateQualification", {}).get("status") != "unpromoted":
        raise ValueError("candidate-only manifest required")
    if manifest["upstream"]["commit"] != overlay["upstreamCommit"]:
        raise ValueError("candidate upstream differs")
    if manifest["candidateQualification"]["baselineNormalizedTree"] != overlay["normalizedTree"]:
        raise ValueError("candidate baseline differs")
    if manifest["candidateQualification"]["overlayManifestSha256"] != digest(bundle / "candidate-overlay.json"):
        raise ValueError("candidate overlay differs")
    if git(checkout, "rev-parse", "HEAD^{tree}") != manifest["source"]["normalizedTree"]:
        raise ValueError("candidate committed tree differs")
    git(checkout, "diff", "--quiet", "HEAD", "--")
    git(checkout, "diff", "--cached", "--quiet", "HEAD", "--")
    check_files(checkout, overlay)


def prepare(checkout, bundle, candidate_manifest, source_receipt):
    for output in (candidate_manifest, source_receipt):
        if output.exists() or output == checkout or checkout in output.parents:
            raise ValueError("candidate evidence requires fresh paths outside the checkout")
    overlay = json.loads((bundle / "candidate-overlay.json").read_text())
    reference = json.loads((bundle.parents[1] / "manifest.json").read_text())
    if reference["upstream"]["commit"] != overlay["upstreamCommit"] or reference["source"]["normalizedTree"] != overlay["normalizedTree"]:
        raise ValueError("deployed reference and candidate base differ")
    if git(checkout, "rev-parse", "HEAD") != overlay["upstreamCommit"]:
        raise ValueError("candidate HEAD differs from pinned upstream")
    git(checkout, "diff", "--cached", "--quiet", overlay["normalizedTree"], "--")
    paths = [entry["path"] for entry in overlay["files"]]
    changed = git(checkout, "diff", "--name-only", "--").splitlines()
    if len(paths) != len(set(paths)) or sorted(changed) != sorted(paths):
        raise ValueError("candidate has unexpected or missing tracked changes")
    if git(checkout, "diff", "--summary", "--"):
        raise ValueError("candidate changed tracked file modes or identities")
    check_files(checkout, overlay)
    git(checkout, "add", "--", *paths)
    git(checkout, "diff", "--quiet", "--")
    tree = git(checkout, "write-tree")
    git(checkout, "-c", "user.name=Candidate qualification", "-c", "user.email=candidate-qualification@users.noreply.github.com", "-c", "commit.gpgsign=false", "commit", "-m", "Qualify the reviewed combined runtime candidate")
    # The native adapter needs upstream commit and final tree. Do not carry the
    # baseline's deployed-commit labels or patch statistics onto the candidate.
    manifest = {
        "schemaVersion": 1,
        "upstream": reference["upstream"],
        "source": {"version": reference["source"]["version"], "normalizedTree": tree},
        "toolchain": reference["toolchain"],
        "candidateQualification": {
            "status": "unpromoted",
            "baselineNormalizedTree": overlay["normalizedTree"],
            "referenceManifestSha256": digest(bundle.parents[1] / "manifest.json"),
            "overlayManifestSha256": digest(bundle / "candidate-overlay.json"),
            "steeringPatchSha256": overlay["patchSha256"],
            "additionalPatches": overlay.get("additionalPatches", []),
        },
    }
    candidate_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    verify(checkout, bundle, candidate_manifest)
    source_receipt.write_text(json.dumps({"head": git(checkout, "rev-parse", "HEAD"), "tree": tree, "candidateManifestSha256": digest(candidate_manifest), "overlayManifestSha256": digest(bundle / "candidate-overlay.json"), "filesVerified": len(paths), "deployedManifestChanged": False}, indent=2) + "\n")
    print("Reviewed source frozen for candidate qualification: " + tree)


if __name__ == "__main__":
    mode, checkout_name, *arguments = sys.argv[1:]
    checkout, bundle = Path(checkout_name).resolve(), Path(__file__).resolve().parent
    if mode == "remove-fixture" and not arguments:
        remove_fixture(checkout, bundle)
        raise SystemExit(0)
    manifest_name, *rest = arguments
    candidate_manifest = Path(manifest_name).resolve()
    if mode == "prepare" and len(rest) == 1:
        prepare(checkout, bundle, candidate_manifest, Path(rest[0]).resolve())
    elif mode == "verify" and not rest:
        verify(checkout, bundle, candidate_manifest)
    else:
        raise SystemExit("Use prepare CHECKOUT MANIFEST RECEIPT or verify CHECKOUT MANIFEST")
