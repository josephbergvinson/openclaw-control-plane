#!/usr/bin/env python3
"""Reject stale literal source targets in reconstructed-runtime workflow steps.

This is a path admission check, not a semantic test replacement. Native owners
still run their tests and plans; removing an obsolete target needs a reviewed
behavior-to-owner mapping.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOWS = ("verify-reference.yml",)
STEP = re.compile(r"^(\s*)- (?:name|uses|run|id):")
SOURCE_PATH = re.compile(r"(?<![\w.-])(?:src|extensions|test|packages|scripts)/[\w./+-]+\.(?:[cm]?[jt]sx?|json|py)\b")


def runtime_targets(text: str) -> set[str]:
    """Read only native-checkout steps; public helper paths keep their own owner."""
    steps: list[list[str]] = []
    current: list[str] = []
    indent: int | None = None
    for line in text.splitlines():
        match = STEP.match(line)
        if match and (indent is None or len(match[1]) <= indent):
            if current:
                steps.append(current)
            current = []
            indent = len(match[1])
        current.append(line)
    if current:
        steps.append(current)
    targets: set[str] = set()
    for lines in steps:
        step = "\n".join(lines)
        if not re.search(r"working-directory:\s*.*runner\.temp.*openclaw-reference", step):
            continue
        for match in SOURCE_PATH.finditer(step):
            prefix = step[max(0, match.start() - 40):match.start()]
            if prefix.endswith(("$GITHUB_WORKSPACE/", "${GITHUB_WORKSPACE}/")):
                continue
            targets.add(match[0])
    return targets


def missing_targets(checkout: Path, targets: set[str]) -> list[str]:
    checkout = checkout.resolve(strict=True)
    return sorted(target for target in targets
                  if not (checkout / target).resolve().is_relative_to(checkout)
                  or not (checkout / target).is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--workflow", action="append", type=Path)
    args = parser.parse_args()
    workflows = args.workflow or [ROOT / ".github/workflows" / name for name in DEFAULT_WORKFLOWS]
    targets = set().union(*(runtime_targets(path.read_text()) for path in workflows))
    if not targets:
        parser.error("no literal runtime targets were discovered")
    missing = missing_targets(args.checkout, targets)
    if missing:
        print("Runtime workflow targets are absent; resolve their behavior owners before publication:", file=sys.stderr)
        print("\n".join(missing), file=sys.stderr)
        return 1
    print(f"Verified {len(targets)} literal runtime workflow targets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
