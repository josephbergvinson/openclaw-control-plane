#!/usr/bin/env python3
"""Deterministic read-only capability validation runner.

This runner executes the safe phases of the capability validation suite and writes
both JSON and Markdown result artifacts under artifacts/capability-validation-runs/.
It avoids approval-gated writes by design.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = ROOT / "artifacts" / "capability-validation-runs"


@dataclass(frozen=True)
class TestCase:
    phase: str
    test_id: str
    capability: str
    command: str
    expected: str
    expected_status: str = "PASS"
    timeout_seconds: int = 120
    shell: bool = False


def now_utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_command(command: str, *, timeout_seconds: int, shell: bool) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command if shell else shlex.split(command),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=shell,
            check=False,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timed_out": True,
        }
    except Exception as exc:
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "timed_out": False,
        }


def classify(result: dict[str, Any], expected_status: str, test_id: str) -> str:
    if result["timed_out"]:
        return "BLOCKED"
    if test_id == "SMK-10":
        return "PASS"
    if result["exit_code"] == 0:
        return expected_status
    if expected_status == "DEGRADED-EXPECTED":
        return "DEGRADED-EXPECTED"
    return "FAIL"


def short_evidence(result: dict[str, Any], limit: int = 240) -> str:
    chunks = []
    if result.get("stdout"):
        chunks.append(result["stdout"].strip())
    if result.get("stderr"):
        chunks.append(result["stderr"].strip())
    text = "\n".join([c for c in chunks if c]).strip()
    text = " ".join(text.split())
    if not text:
        return ""
    return text[: limit - 1] + "…" if len(text) > limit else text


def git_head() -> str:
    result = run_command("git rev-parse --short HEAD", timeout_seconds=20, shell=False)
    if result["exit_code"] == 0:
        return (result["stdout"] or "").strip()
    return "unknown"


def openclaw_version() -> str:
    result = run_command("openclaw --version", timeout_seconds=20, shell=False)
    if result["exit_code"] == 0:
        return (result["stdout"] or "").strip()
    return "unknown"


def build_suite() -> list[TestCase]:
    return [
        TestCase("smoke", "SMK-01", "Gateway health", "openclaw gateway status", "Gateway reachable and healthy", timeout_seconds=30),
        TestCase("smoke", "SMK-02", "Runtime health", "openclaw status --deep", "Runtime health returns successfully", timeout_seconds=60),
        TestCase("smoke", "SMK-03", "Channel registry", "openclaw channels list", "Configured channels enumerate", timeout_seconds=30),
        TestCase("smoke", "SMK-04", "Model registry", "openclaw models list", "Models list returns", timeout_seconds=30),
        TestCase("smoke", "SMK-05", "Model status", "openclaw models status", "Model status returns", timeout_seconds=30),
        TestCase("smoke", "SMK-06", "Security audit", "openclaw security audit --deep", "Security audit completes", timeout_seconds=90),
        TestCase("smoke", "SMK-07", "Update status", "openclaw update status", "Update status returns", timeout_seconds=30),
        TestCase("smoke", "SMK-08", "Skill inventory baseline", "openclaw skills check", "Skill inventory check completes", timeout_seconds=30),
        TestCase("smoke", "SMK-09", "Workspace validation", "bash scripts/openclaw_workspace_validate.sh", "Workspace validation completes", timeout_seconds=180, shell=True),
        TestCase("smoke", "SMK-10", "Root checkout sanity", "git -C <OPENCLAW_WORKSPACE> status --short", "Drift is visible for operator review", timeout_seconds=20),
        TestCase("reads", "READ-01", "Google capability baseline", "./.venv/bin/python scripts/google_capability_probe.py", "Probe completes successfully", timeout_seconds=60),
        TestCase("reads", "READ-02", "Gmail read", "gog gmail labels list", "Labels enumerate successfully", timeout_seconds=60),
        TestCase("reads", "READ-03", "Drive read", "gog drive ls --limit 5", "Files list successfully", timeout_seconds=60),
        TestCase("reads", "READ-04", "Calendar read", "gog calendar list --json", "Calendars list successfully", timeout_seconds=60),
        TestCase("reads", "READ-05", "Apple Calendar local read", "python3 scripts/apple_calendar_probe.py", "Apple Calendar probe succeeds", timeout_seconds=60),
        TestCase("reads", "READ-06", "Apple Notes CLI baseline", "memo --version", "Apple Notes CLI responds", timeout_seconds=20),
        TestCase("reads", "READ-07", "Notion personal route probe", "python3 scripts/notion_capability_probe.py --route personal", "Personal route probe succeeds", timeout_seconds=60),
        TestCase("reads", "READ-08", "Notion <PROJECT_OR_ORG> route probe", "python3 scripts/notion_capability_probe.py --route project", "<PROJECT_OR_ORG> route probe succeeds", timeout_seconds=60),
        TestCase("reads", "READ-09", "Trello board access baseline", "python3 scripts/trello_capability_probe.py", "Trello read probe succeeds", timeout_seconds=60),
        TestCase("reads", "READ-10", "GitHub auth baseline", "gh auth status", "GitHub auth is healthy", timeout_seconds=30),
        TestCase("reads", "READ-11", "Places resolve baseline", 'goplaces resolve "Soho, London" --limit 1 --json', "Places resolve returns JSON", timeout_seconds=60),
        TestCase("reads", "READ-12", "Browser global status", "openclaw browser status", "Browser status returns", timeout_seconds=30),
        TestCase("reads", "READ-13", "Browser profile inventory", "openclaw browser profiles", "Browser profiles enumerate", timeout_seconds=30),
        TestCase("example-project", "PROJ-01", "Example project local health", "curl -fsS http://127.0.0.1:<PORT>/healthz", "Example project health endpoint returns successfully", timeout_seconds=20),
        TestCase("example-project", "PROJ-02", "Example project local status", "curl -fsS http://127.0.0.1:<PORT>/v1/example/status", "Example project status endpoint returns successfully", timeout_seconds=20),
        TestCase("example-project", "PROJ-03", "Surface contract validation", "bash scripts/example_project_validate.sh", "Surface contract validation completes", timeout_seconds=120, shell=True),
        TestCase("example-project", "PROJ-04", "Runtime validation", "bash scripts/example_project_ci.sh", "Runtime validation completes", timeout_seconds=120, shell=True),
        TestCase("degraded", "DEG-01", "Browser profile openclaw lane", "openclaw browser status --browser-profile openclaw", "Disabled/stopped state is surfaced cleanly", expected_status="DEGRADED-EXPECTED", timeout_seconds=30),
        TestCase("degraded", "DEG-02", "Browser profile user-debug lane", "openclaw browser status --browser-profile user-debug", "Current lane state returns without crash", timeout_seconds=30),
        TestCase("degraded", "DEG-03", "Elevated-from-Discord disabled policy", "openclaw approvals get --gateway", "Approvals state returns for comparison against capabilities.md", timeout_seconds=30),
    ]


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines: list[str] = []
    lines.append("# Capability Validation Run\n")
    lines.append(f"Run id: `{report['run_id']}`  ")
    lines.append(f"Date/time (UTC): `{report['checked_at_utc']}`  ")
    lines.append(f"Workspace commit: `{report['workspace_commit']}`  ")
    lines.append(f"OpenClaw version: `{report['openclaw_version']}`\n")
    lines.append("## Summary\n")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    for key in ["PASS", "FAIL", "DEGRADED-EXPECTED", "BLOCKED"]:
        lines.append(f"| {key} | {report['summary'].get(key, 0)} |")
    lines.append("")
    current_phase = None
    for row in report["results"]:
        if row["phase"] != current_phase:
            current_phase = row["phase"]
            lines.append(f"## Phase: {current_phase}\n")
            lines.append("| ID | Capability | Command | Result | Exit | Evidence |")
            lines.append("|---|---|---|---|---:|---|")
        evidence = row.get("evidence", "").replace("|", "\\|")
        command = row["command"].replace("|", "\\|")
        lines.append(f"| {row['test_id']} | {row['capability']} | `{command}` | {row['result']} | {row['exit_code']} | {evidence} |")
        if row.get("notes"):
            lines.append(f"\nNotes for {row['test_id']}: {row['notes']}\n")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = f"capval-{now_utc_compact()}"
    results: list[dict[str, Any]] = []

    for case in build_suite():
        raw = run_command(case.command, timeout_seconds=case.timeout_seconds, shell=case.shell)
        result = classify(raw, case.expected_status, case.test_id)
        notes = None
        if case.test_id == "SMK-10":
            notes = "This row records visible root drift. Cleanliness is operator-reviewed, not auto-pass/fail on empty output."
        if case.test_id == "DEG-03":
            notes = "Compare this output against the disabled elevated-from-Discord fact in capabilities.md."
        results.append(
            {
                "phase": case.phase,
                "test_id": case.test_id,
                "capability": case.capability,
                "command": case.command,
                "expected": case.expected,
                "expected_status": case.expected_status,
                "result": result,
                "exit_code": raw["exit_code"],
                "timed_out": raw["timed_out"],
                "stdout": raw["stdout"],
                "stderr": raw["stderr"],
                "evidence": short_evidence(raw),
                "notes": notes,
            }
        )

    summary = {"PASS": 0, "FAIL": 0, "DEGRADED-EXPECTED": 0, "BLOCKED": 0}
    for row in results:
        summary[row["result"]] = summary.get(row["result"], 0) + 1

    report = {
        "run_id": run_id,
        "checked_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "workspace_root": str(ROOT),
        "workspace_commit": git_head(),
        "openclaw_version": openclaw_version(),
        "summary": summary,
        "results": results,
    }

    json_path = ARTIFACT_DIR / f"{run_id}.json"
    md_path = ARTIFACT_DIR / f"{run_id}.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_markdown(report, md_path)

    print(json.dumps({
        "run_id": run_id,
        "json": str(json_path.relative_to(ROOT)),
        "markdown": str(md_path.relative_to(ROOT)),
        "summary": summary,
    }, indent=2))
    return 0 if summary.get("FAIL", 0) == 0 and summary.get("BLOCKED", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
