#!/usr/bin/env python3
"""Copy the policy templates into a workspace directory.

Copies every ``templates/*.example.*`` file to the destination with the ``.example``
segment removed. Existing files are never overwritten unless ``--force`` is given. The
copy then lists the angle-bracket names you need to fill in for your own environment.

    python3 scripts/install_templates.py <workspace-root>
    python3 scripts/install_templates.py <workspace-root> --dry-run
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
PLACEHOLDER_RE = re.compile(r"<[a-z][a-z0-9-]*>")


def target_name(source: Path) -> str:
    return source.name.replace(".example", "", 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("destination", type=Path, help="workspace directory to populate")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--dry-run", action="store_true", help="report actions without writing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    destination: Path = args.destination.expanduser()

    if not destination.is_dir():
        print(f"destination is not a directory: {destination}", file=sys.stderr)
        return 2

    sources = sorted(p for p in TEMPLATES.iterdir() if p.is_file() and ".example" in p.name)
    if not sources:
        print("no templates found", file=sys.stderr)
        return 2

    copied = skipped = 0
    placeholders: set[str] = set()

    for source in sources:
        target = destination / target_name(source)
        if target.exists() and not args.force:
            print(f"skip   {target.name} (exists; use --force to overwrite)")
            skipped += 1
            continue
        placeholders.update(PLACEHOLDER_RE.findall(source.read_text(encoding="utf-8")))
        if args.dry_run:
            print(f"would copy {source.name} -> {target}")
        else:
            shutil.copy2(source, target)
            print(f"copy   {source.name} -> {target.name}")
        copied += 1

    print(f"\n{copied} copied, {skipped} skipped")
    if placeholders and not args.dry_run:
        print("\nValues you need to fill in:")
        for name in sorted(placeholders):
            print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
