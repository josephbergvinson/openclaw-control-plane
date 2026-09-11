#!/usr/bin/env python3
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
import fnmatch
import json
import subprocess
from pathlib import Path
from typing import Any


WORKTREE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO_ROOT = Path((str(OPERATOR.require_path('paths.workspace'))))
DEFAULT_POLICY_PATH = WORKTREE_ROOT / "control" / "root_drift_policy.json"


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def git_status_porcelain(root: Path) -> list[str]:
    proc = subprocess.run(
        ["/usr/bin/git", "status", "--porcelain=v1"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def git_branch(root: Path) -> str:
    return subprocess.run(
        ["/usr/bin/git", "branch", "--show-current"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def parse_porcelain_line(line: str) -> dict[str, str]:
    if line.startswith("?? "):
        return {"status": "??", "path": line[3:]}
    return {"status": line[:2], "path": line[3:] if len(line) > 3 else line}


def match_rule(path: str, policy: dict[str, Any]) -> dict[str, Any] | None:
    for rule in policy.get("classes", []):
        for pattern in rule.get("paths", []):
            if fnmatch.fnmatch(path, pattern):
                return rule
    return None


def path_is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def provenance_for_path(path: str, repo_root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    abs_path = (repo_root / path).resolve()
    findings: list[str] = []
    blockers: list[str] = []

    for lane in policy.get("protectedLanes", []):
        lane_path = Path(lane["worktreePath"]).resolve()
        if path_is_inside(abs_path, lane_path):
            blockers.append(f"path resolves inside protected lane {lane['name']}")

    return {
        "path": path,
        "findings": findings,
        "blockers": blockers,
        "ambiguous": bool(blockers),
    }


def classify_item(item: dict[str, str], repo_root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    path = item["path"]
    rule = match_rule(path, policy)
    provenance = provenance_for_path(path, repo_root, policy)
    classification = rule["class"] if rule else policy["normalization"]["unmatchedDefaultClass"]
    reason = rule.get("reason") if rule else "unmatched path under fail-closed policy"

    if provenance["ambiguous"] and policy["normalization"].get("failClosedOnAmbiguous", False):
        classification = "blocker"
        reason = "; ".join(provenance["blockers"]) or reason

    return {
        "path": path,
        "status": item["status"],
        "classification": classification,
        "reason": reason,
        "provenance": provenance,
    }


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[str]] = {
        "root-local": [],
        "normalize-to-root": [],
        "safe-remove": [],
        "retained-state": [],
        "blocker": [],
    }
    tracked: list[str] = []
    untracked: list[str] = []
    for item in items:
        buckets.setdefault(item["classification"], []).append(item["path"])
        if item["status"] == "??":
            untracked.append(item["path"])
        else:
            tracked.append(item["path"])
    return {
        "isClean": not items,
        "tracked": tracked,
        "untracked": untracked,
        "hasTrackedDrift": bool(tracked),
        "hasUntrackedDrift": bool(untracked),
        "classifications": buckets,
        "hasBlockers": bool(buckets.get("blocker")),
    }


def current_state(root: Path = DEFAULT_REPO_ROOT, policy_path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    policy = load_policy(policy_path)
    lines = git_status_porcelain(root)
    parsed = [parse_porcelain_line(line) for line in lines]
    items = [classify_item(item, root, policy) for item in parsed]
    summary = summarize(items)
    summary["repoRoot"] = str(root)
    summary["policyPath"] = str(policy_path)
    summary["branch"] = git_branch(root)
    summary["items"] = items
    summary["porcelain"] = lines
    return summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Emit machine-readable root drift state")
    p.add_argument("--root", default=str(DEFAULT_REPO_ROOT))
    p.add_argument("--policy", default=str(DEFAULT_POLICY_PATH))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    print(json.dumps(current_state(Path(args.root), Path(args.policy)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
