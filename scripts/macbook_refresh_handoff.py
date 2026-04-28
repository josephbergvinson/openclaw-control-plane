#!/usr/bin/env python3
"""Generic handoff refresh helper template.

Adapt this script for a project that maintains a user-facing handoff copy.
Keep source roots and handoff roots distinct, verify backup/deletion gates separately,
and never treat an iCloud/File Provider copy as canonical source unless explicitly declared.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def refresh(source: Path, destination: Path) -> None:
    if not source.exists():
        raise SystemExit(f"source missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise SystemExit(
            f"destination already exists: {destination}\n"
            "This template does not delete or replace existing handoff copies. "
            "Add an explicit backup/verification gate before replacement."
        )
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh a project handoff copy (template).")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    refresh(args.source, args.destination)
    print(f"refreshed {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
