#!/usr/bin/env python3
"""Mechanical closeout gate for implementation slices.

The gate is intentionally conservative: a slice can only be reported as
`complete` when the checked-in/source state, standing-branch normalization, and
optional runtime/handoff provenance checks all pass. Otherwise it emits a JSON
result that callers can quote in final reports as incomplete, blocked, or
preserve.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

NON_BLOCKING_DIRTY_CLASSES = {"generated residue", "artifact", "unrelated operational memory drift"}
HOTFIX_MARKER = "live-installed-runtime-hotfix-not-normalized"
SUPERSESSION_REASON_PHRASE = "old branch superseded, not merged"
FORBIDDEN_COMPLETE_RE = re.compile(
    r"\b(done|complete|completed|closed out)\b|\bblocker\s*:\s*none\b",
    re.IGNORECASE,
)


def run_git(root: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_output(root: Path, args: list[str], check: bool = True) -> str:
    return run_git(root, args, check=check).stdout.strip()


def parse_porcelain(output: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        status = line[:2]
        path = line[3:] if len(line) > 2 and line[2] == " " else line[2:].lstrip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries.append({"path": path, "status": status})
    return entries


def load_classification(path: str | None) -> dict[str, dict[str, str]]:
    if not path:
        return {}
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise SystemExit("drift classification must be a JSON object keyed by repo-relative path")
    result: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            result[key] = {"class": value, "reason": ""}
        elif isinstance(value, dict):
            result[key] = {
                "class": str(value.get("class", "")),
                "reason": str(value.get("reason", "")),
            }
        else:
            raise SystemExit(f"invalid classification for {key}")
    return result


def classify_dirty(entries: list[dict[str, str]], classifications: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    dirty: list[dict[str, str]] = []
    for entry in entries:
        path = entry["path"]
        classified = classifications.get(path, {})
        dirty_class = classified.get("class") or infer_dirty_class(path)
        dirty.append(
            {
                "path": path,
                "status": entry["status"],
                "class": dirty_class,
                "reason": classified.get("reason", ""),
                "classified": str(path in classifications).lower(),
            }
        )
    return dirty


def infer_dirty_class(path: str) -> str:
    if path.startswith("dist/") or path.startswith("dist.") or "/dist/" in path:
        return "build/install artifact"
    if path.startswith("artifacts/"):
        return "artifact"
    if path.startswith("audit/"):
        return "test/report change"
    if ".test." in path or path.startswith("test/") or path.startswith("tests/"):
        return "test/report change"
    return "source change"


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    proc = run_git(root, ["merge-base", "--is-ancestor", ancestor, descendant], check=False)
    return proc.returncode == 0


def resolve_commit(root: Path, ref: str) -> str:
    return git_output(root, ["rev-parse", ref])


def validate_supersession_record(
    root: Path,
    args: argparse.Namespace,
    head_commit: str,
) -> tuple[bool, dict[str, Any] | None, list[str]]:
    if not args.supersession_record:
        return False, None, []

    record_path = Path(args.supersession_record)
    problems: list[str] = []
    try:
        record = json.loads(record_path.read_text())
    except Exception as exc:  # pragma: no cover - defensive CLI error path
        return False, None, [f"failed to read supersession record {record_path}: {exc}"]

    target = str(record.get("targetMainCommit", ""))
    source = str(record.get("sourceSupersededCommit", ""))
    changed_files = record.get("changedFiles")
    test_evidence = record.get("testEvidence")
    reason = str(record.get("reason", ""))

    if not target:
        problems.append("supersession record missing targetMainCommit")
    else:
        try:
            target_commit = resolve_commit(root, target)
        except subprocess.CalledProcessError:
            problems.append(f"supersession targetMainCommit not resolvable: {target}")
        else:
            if target_commit != head_commit and not is_ancestor(root, target_commit, head_commit):
                problems.append(
                    f"supersession targetMainCommit {target_commit} is neither HEAD {head_commit} nor an ancestor of HEAD"
                )

    if not source:
        problems.append("supersession record missing sourceSupersededCommit")
    else:
        try:
            resolve_commit(root, source)
        except subprocess.CalledProcessError:
            problems.append(f"supersession sourceSupersededCommit not resolvable: {source}")

    if not isinstance(changed_files, list) or not changed_files or not all(isinstance(item, str) and item for item in changed_files):
        problems.append("supersession record requires a non-empty changedFiles list")

    if not isinstance(test_evidence, list) or not test_evidence:
        problems.append("supersession record requires non-empty testEvidence")
    else:
        for index, item in enumerate(test_evidence):
            if not isinstance(item, dict) or not item.get("command") or not item.get("result"):
                problems.append(f"supersession testEvidence[{index}] requires command and result")

    if SUPERSESSION_REASON_PHRASE not in reason.lower():
        problems.append(f"supersession reason must include '{SUPERSESSION_REASON_PHRASE}'")
    if "not merged wholesale" not in reason.lower():
        problems.append("supersession reason must state old branch was not merged wholesale")
    if "intentionally excluded" not in reason.lower():
        problems.append("supersession reason must state unrelated/conflicting content was intentionally excluded")

    return not problems, record, problems


def branch_exists(root: Path, branch: str) -> bool:
    proc = run_git(root, ["rev-parse", "--verify", "--quiet", branch], check=False)
    return proc.returncode == 0


def branch_divergence(root: Path, branch: str) -> dict[str, int | None]:
    upstream = git_output(root, ["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], check=False)
    if not upstream:
        return {"ahead": None, "behind": None}
    counts = git_output(root, ["rev-list", "--left-right", "--count", f"{upstream}...{branch}"], check=False)
    if not counts:
        return {"ahead": None, "behind": None}
    behind_s, ahead_s = counts.split()
    return {"ahead": int(ahead_s), "behind": int(behind_s)}


def check_runtime(args: argparse.Namespace, head_commit: str) -> tuple[bool | None, str | None]:
    if not args.installed_runtime_required:
        return None, None
    details: list[str] = []
    short = head_commit[:7]
    if args.installed_runtime_path:
        runtime_path = Path(args.installed_runtime_path)
        if not runtime_path.exists():
            return False, f"installed runtime path missing: {runtime_path}"
        try:
            resolved_runtime = runtime_path.resolve()
            resolved_root = Path(args.worktree).resolve()
        except OSError as err:
            return False, f"failed to resolve installed runtime path: {err}"
        if resolved_runtime != resolved_root:
            return False, f"installed runtime path resolves to {resolved_runtime}, not {resolved_root}"
        details.append(f"installed runtime path resolves to {resolved_root}")
    if args.installed_runtime_version_command:
        proc = subprocess.run(
            args.installed_runtime_version_command,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output = proc.stdout.strip()
        if proc.returncode != 0:
            return False, f"installed runtime version command failed: {output}"
        if short not in output and head_commit[:10] not in output:
            return False, f"installed runtime version output does not contain {short}: {output}"
        details.append(output)
    return True, "; ".join(details) if details else None


def build_gate_result(args: argparse.Namespace) -> dict[str, Any]:
    canonical_root = Path(args.canonical_root).resolve()
    worktree = Path(args.worktree).resolve()
    blockers: list[str] = []
    next_actions: list[str] = []

    if not canonical_root.exists():
        blockers.append(f"canonical root does not exist: {canonical_root}")
    if not worktree.exists():
        blockers.append(f"worktree does not exist: {worktree}")
    if blockers:
        return base_result(args, canonical_root, worktree, blockers=blockers)

    git_root = Path(git_output(worktree, ["rev-parse", "--show-toplevel"])).resolve()
    if git_root != worktree:
        blockers.append(f"declared worktree {worktree} does not match git root {git_root}")

    actual_branch = git_output(worktree, ["branch", "--show-current"])
    head_commit = git_output(worktree, ["rev-parse", "HEAD"])
    head_short = head_commit[:10]
    if actual_branch != args.task_branch and actual_branch != args.standing_branch:
        blockers.append(
            f"active branch {actual_branch} is neither task branch {args.task_branch} nor standing branch {args.standing_branch}"
        )
    if not branch_exists(worktree, args.standing_branch):
        blockers.append(f"standing branch missing: {args.standing_branch}")
    if not branch_exists(worktree, args.task_branch):
        blockers.append(f"task branch missing: {args.task_branch}")

    normalized = False
    supersession_record: dict[str, Any] | None = None
    supersession_problems: list[str] = []
    if not blockers:
        normalized = (
            args.task_branch == args.standing_branch
            or is_ancestor(worktree, args.task_branch, args.standing_branch)
            or (actual_branch == args.standing_branch and is_ancestor(worktree, args.task_branch, "HEAD"))
        )
        if not normalized:
            superseded, supersession_record, supersession_problems = validate_supersession_record(
                worktree,
                args,
                head_commit,
            )
            if superseded:
                normalized = True
            else:
                blockers.extend(supersession_problems)
                next_actions.append("normalize verified slice content onto the standing branch or provide valid explicit supersession evidence")

    raw_dirty = git_output(worktree, ["status", "--porcelain=v1"])
    dirty = classify_dirty(parse_porcelain(raw_dirty), load_classification(args.drift_classification))
    blocking_dirty = [
        item
        for item in dirty
        if item["class"] not in NON_BLOCKING_DIRTY_CLASSES or item["classified"] != "true"
    ]
    if blocking_dirty:
        next_actions.append("commit required source/test/report changes or classify/preserve residual drift")

    hotfix_blockers = []
    runtime_dirty = [
        item
        for item in dirty
        if item["path"].startswith("dist/")
        or item["path"].startswith("dist.")
        or item["path"].startswith(".openclaw-cli/")
    ]
    if runtime_dirty and not args.installed_runtime_hotfix_record:
        hotfix_blockers.append(
            "installed/runtime build artifact changed without source-normalized commit or hotfix record"
        )
    if args.installed_runtime_hotfix_record:
        record_text = Path(args.installed_runtime_hotfix_record).read_text()
        if HOTFIX_MARKER not in record_text:
            hotfix_blockers.append(
                f"hotfix record missing marker {HOTFIX_MARKER}: {args.installed_runtime_hotfix_record}"
            )
    blockers.extend(hotfix_blockers)

    authoritative_remote_updated: bool | None = None
    divergence = branch_divergence(worktree, args.standing_branch)
    if args.require_authoritative_remote:
        authoritative_remote_updated = divergence.get("ahead") == 0
        if not authoritative_remote_updated:
            next_actions.append("push/preserve standing branch to authoritative remote")
    elif args.openclaw_source_clone:
        authoritative_remote_updated = None
    else:
        authoritative_remote_updated = divergence.get("ahead") == 0 if divergence.get("ahead") is not None else None

    runtime_ok, runtime_detail = check_runtime(args, head_commit)
    if runtime_ok is False:
        blockers.append(runtime_detail or "installed runtime provenance failed")
        next_actions.append("rebuild/install runtime from normalized source or record hotfix blocker")

    handoff_refreshed: bool | None = None
    if args.handoff_required:
        handoff_refreshed = False
        if args.handoff_marker and Path(args.handoff_marker).exists():
            handoff_refreshed = True
        if not handoff_refreshed:
            next_actions.append("refresh required handoff/build/iCloud mirror")

    preserve_reason = args.preserve_reason or None
    if preserve_reason:
        status = "preserve"
    elif blockers:
        status = "blocked"
    elif not normalized or blocking_dirty or handoff_refreshed is False:
        status = "incomplete"
    else:
        status = "complete"

    if status == "complete" and next_actions:
        status = "incomplete"

    return {
        "closeoutStatus": status,
        "surface": args.surface,
        "canonicalRoot": str(canonical_root),
        "worktree": str(worktree),
        "taskBranch": args.task_branch,
        "standingBranch": args.standing_branch,
        "headCommit": head_short,
        "normalizedOntoStandingBranch": normalized,
        "supersessionRecord": supersession_record,
        "supersessionRecordPath": args.supersession_record,
        "authoritativeRemoteUpdated": authoritative_remote_updated,
        "installedRuntimeBuiltFromNormalizedCommit": runtime_ok,
        "installedRuntimeDetail": runtime_detail,
        "handoffRefreshed": handoff_refreshed,
        "dirtyState": dirty,
        "residualDrift": dirty,
        "preserveReason": preserve_reason,
        "blockers": blockers,
        "nextRequiredAction": "; ".join(next_actions) if next_actions else None,
        "branchDivergence": divergence,
    }


def base_result(args: argparse.Namespace, canonical_root: Path, worktree: Path, blockers: list[str]) -> dict[str, Any]:
    return {
        "closeoutStatus": "blocked",
        "surface": args.surface,
        "canonicalRoot": str(canonical_root),
        "worktree": str(worktree),
        "taskBranch": args.task_branch,
        "standingBranch": args.standing_branch,
        "headCommit": None,
        "normalizedOntoStandingBranch": False,
        "authoritativeRemoteUpdated": None,
        "installedRuntimeBuiltFromNormalizedCommit": None,
        "handoffRefreshed": None,
        "dirtyState": [],
        "residualDrift": [],
        "preserveReason": args.preserve_reason or None,
        "blockers": blockers,
        "nextRequiredAction": "repair closeout gate inputs",
    }


def report_check(args: argparse.Namespace) -> dict[str, Any]:
    gate = json.loads(Path(args.gate_result).read_text())
    report = Path(args.report_text).read_text()
    status = gate.get("closeoutStatus")
    forbidden = bool(FORBIDDEN_COMPLETE_RE.search(report))
    ok = status == "complete" or not forbidden
    blockers = [] if ok else ["final report implies completion/blocker none while closeout gate is not complete"]
    return {
        "ok": ok,
        "closeoutStatus": status,
        "forbiddenCompletionLanguageFound": forbidden,
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Implementation slice closeout gate")
    sub = parser.add_subparsers(dest="command")

    gate = sub.add_parser("gate", help="emit closeout gate JSON")
    gate.add_argument("--surface", required=True)
    gate.add_argument("--canonical-root", required=True)
    gate.add_argument("--worktree", required=True)
    gate.add_argument("--task-branch", required=True)
    gate.add_argument("--standing-branch", required=True)
    gate.add_argument("--require-authoritative-remote", action="store_true")
    gate.add_argument("--openclaw-source-clone", action="store_true")
    gate.add_argument("--installed-runtime-required", action="store_true")
    gate.add_argument("--installed-runtime-path")
    gate.add_argument("--installed-runtime-version-command")
    gate.add_argument("--installed-runtime-hotfix-record")
    gate.add_argument("--handoff-required", action="store_true")
    gate.add_argument("--handoff-marker")
    gate.add_argument("--drift-classification")
    gate.add_argument("--supersession-record")
    gate.add_argument("--preserve-reason")

    report = sub.add_parser("report-check", help="validate final report text against gate JSON")
    report.add_argument("--gate-result", required=True)
    report.add_argument("--report-text", required=True)

    args = parser.parse_args()
    if args.command == "gate":
        result = build_gate_result(args)
    elif args.command == "report-check":
        result = report_check(args)
    else:
        parser.print_help(sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.command == "report-check" and not result.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
