#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = WORKTREE_ROOT
ARTIFACT_DIR = WORKTREE_ROOT / "artifacts" / "discord_coding_slices"


def git(*args: str, capture: bool = True) -> str:
    proc = subprocess.run(
        ["/usr/bin/git", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=capture,
        check=True,
    )
    return proc.stdout.strip() if capture else ""


def verify_worktree_path(path: Path) -> tuple[bool, str]:
    try:
        common_dir = subprocess.run(
            ["/usr/bin/git", "rev-parse", "--git-common-dir"],
            cwd=path,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        return False, f"git common-dir lookup failed for {path}: {exc.stderr.strip() or exc}"

    common_dir_path = (path / common_dir).resolve() if not Path(common_dir).is_absolute() else Path(common_dir).resolve()
    expected_common_dir = subprocess.run(
        ["/usr/bin/git", "rev-parse", "--git-common-dir"],
        cwd=WORKTREE_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    expected_common_dir = (
        (WORKTREE_ROOT / expected_common_dir).resolve()
        if not Path(expected_common_dir).is_absolute()
        else Path(expected_common_dir).resolve()
    )
    if common_dir_path != expected_common_dir:
        return False, f"worktree {path} resolves to unexpected git common dir {common_dir_path}"
    return True, str(common_dir_path)


def branch_for_path(path: Path) -> str:
    return subprocess.run(
        ["/usr/bin/git", "branch", "--show-current"],
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def write_state(payload: dict) -> Path:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    slug = payload["branch"].replace("/", "-")
    out = ARTIFACT_DIR / f"{slug}.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def concept_led_operator_contract() -> dict:
    """Default contract for Discord coding asks from non-engineer operators.

    This does not change the Discord workflow. It makes the durable-lane state
    explicit enough that a worker can drive implementation autonomously from a
    conceptual prompt while keeping the operator in the same guide/review role.
    """

    return {
        "operatorProfile": "concept-led, non-engineer-friendly",
        "agentResponsibilities": [
            "translate conceptual intent into a narrow technical objective",
            "state assumptions and acceptance checks before deep implementation",
            "choose the smallest workflow-preserving patch that improves the control plane",
            "surface only meaningful checkpoints, reviewable results, or real blockers",
        ],
        "operatorExpectedInput": [
            "conceptual direction",
            "review of tradeoffs or visible behavior when requested",
            "explicit approval only for external, destructive, or workflow-changing actions",
        ],
        "patchBoundary": {
            "prefer": "small guardrail, helper, or test-backed contract improvement",
            "avoid": [
                "new required user ceremony",
                "new mandatory prompt template",
                "changes to the normal Discord control-plane workflow",
                "broad refactors without a direct liveness or autonomy benefit",
            ],
        },
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Establish, resume, or close out a durable Discord coding slice")
    sub = p.add_subparsers(dest="command", required=True)

    for name in ("start", "resume"):
        lane = sub.add_parser(name)
        lane.add_argument("--worktree", required=True)
        lane.add_argument("--branch", required=True)
        lane.add_argument("--surface", default="control-plane")
        lane.add_argument("--objective", required=True)

    closeout = sub.add_parser("closeout-check", help="run the canonical slice closeout gate")
    closeout.add_argument("gate_args", nargs=argparse.REMAINDER)
    return p.parse_args()


def establish_or_resume(args: argparse.Namespace) -> int:
    worktree = Path(args.worktree).expanduser().resolve()

    ok, detail = verify_worktree_path(worktree)
    if not ok:
        print(json.dumps({"ok": False, "blocker": detail}))
        return 2

    actual_branch = branch_for_path(worktree)
    if actual_branch != args.branch:
        print(
            json.dumps(
                {
                    "ok": False,
                    "blocker": f"branch mismatch: expected {args.branch}, found {actual_branch}",
                }
            )
        )
        return 2

    head = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    payload = {
        "ok": True,
        "mode": args.command,
        "surface": args.surface,
        "objective": args.objective,
        "repoRoot": str(REPO_ROOT),
        "worktree": str(worktree),
        "branch": actual_branch,
        "head": head,
        "resumeCommand": f"cd '{worktree}' && git status --short && PYTHONPATH=src .venv/bin/python -m pytest -q",
        "visibleThreadPolicy": {
            "acknowledgeOnce": True,
            "followups": ["meaningful checkpoint", "reviewable result", "real blocker"],
        },
        "conceptLedOperatorContract": concept_led_operator_contract(),
    }
    artifact = write_state(payload)
    payload["artifact"] = str(artifact)
    print(json.dumps(payload, indent=2))
    return 0


def closeout_check(args: argparse.Namespace) -> int:
    gate_args = args.gate_args
    if gate_args and gate_args[0] == "--":
        gate_args = gate_args[1:]
    gate = Path(__file__).with_name("slice_closeout_gate.py")
    return subprocess.call([sys.executable, str(gate), "gate", *gate_args])


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "closeout-check":
        gate_args = sys.argv[2:]
        if gate_args and gate_args[0] == "--":
            gate_args = gate_args[1:]
        gate = Path(__file__).with_name("slice_closeout_gate.py")
        return subprocess.call([sys.executable, str(gate), "gate", *gate_args])

    args = parse_args()
    if args.command in {"start", "resume"}:
        return establish_or_resume(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
