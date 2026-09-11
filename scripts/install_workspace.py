#!/usr/bin/env python3
"""Install this reference into a new workspace without starting any services.

Copies the complete workspace implementation and policy templates, writes private
operator bindings and renders the runtime preferences. Existing destinations are
refused. No subprocesses, providers, package installation or service actions run.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reference_operator_contract", ROOT / "workspace/scripts/operator_contract.py")
assert SPEC and SPEC.loader
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)
SKIP_PARTS = {"__pycache__", ".git", ".pytest_cache"}


def source_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_PARTS for part in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            raise ValueError(f"source symlink is not allowed: {path.relative_to(ROOT)}")
        if path.is_file():
            files.append(path)
    return files


def collect_files() -> dict[Path, tuple[bytes, int]]:
    files: dict[Path, tuple[bytes, int]] = {}
    origins: dict[Path, Path] = {}

    def add(target: Path, source: Path) -> None:
        if target in files or target.is_absolute() or ".." in target.parts:
            raise ValueError(f"invalid or duplicate installation target: {target}")
        mode = 0o700 if source.stat().st_mode & 0o111 else 0o600
        files[target] = (source.read_bytes(), mode)
        origins[source.resolve()] = target

    for source in source_files(ROOT / "workspace"):
        add(source.relative_to(ROOT / "workspace"), source)
    for source in source_files(ROOT / "templates"):
        if ".example." in source.name:
            add(Path(source.name.replace(".example.", ".", 1)), source)
        elif source.name == "analytical-workbook.md":
            add(Path("templates") / source.name, source)
        elif source.name == "workspace.gitignore":
            add(Path(".gitignore"), source)
    # Documentation is part of the installed dependency closure. Rebase its
    # actual Markdown links using the same source-to-target mapping as code,
    # so a runbook remains usable after the reference checkout is removed.
    for directory in ("docs", "diagrams", "runtime", "config", "examples", "schemas"):
        for source in source_files(ROOT / directory):
            add(source.relative_to(ROOT), source)
        origins[(ROOT / directory).resolve()] = Path(directory)
    origins[(ROOT / "workspace").resolve()] = Path(".")
    for name in ("reconstruct_runtime.py", "materialize_host.py"):
        add(Path("scripts") / name, ROOT / "scripts" / name)
    for name in ("LICENSE", "SECURITY.md", "CONTRIBUTING.md"):
        add(Path(name), ROOT / name)
    add(Path("REFERENCE.md"), ROOT / "README.md")
    for source, target in origins.items():
        if target.suffix.lower() != ".md":
            continue
        payload, mode = files[target]
        def rebase(match):
            link = match.group(1)
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
                return match.group(0)
            destination = (source.parent / unquote(parsed.path)).resolve()
            mapped = origins.get(destination)
            if mapped is None and destination.is_dir():
                for ancestor in destination.parents:
                    if ancestor in origins and ancestor.is_dir():
                        mapped = origins[ancestor] / destination.relative_to(ancestor)
                        break
            if mapped is None:
                return match.group(0)
            replacement = os.path.relpath(mapped, target.parent)
            if parsed.query:
                replacement += "?" + parsed.query
            if parsed.fragment:
                replacement += "#" + parsed.fragment
            return match.group(0).replace(link, replacement, 1)
        body = re.sub(r"\]\(([^)]+)\)", rebase, payload.decode())
        files[target] = (body.encode(), mode)
    return files


def make_plan(destination: Path, operator_file: Path | None = None) -> dict[Path, tuple[bytes, int]]:
    if not destination.is_absolute() or ".." in destination.parts:
        raise ValueError("destination must be an absolute path without parent traversal")
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists; choose a new workspace")
    if not destination.parent.is_dir() or destination.parent.resolve() != destination.parent:
        raise ValueError("destination parent must exist and must not traverse symlinks")
    if operator_file is None:
        values = {"schema_version": 1, "paths": {"workspace": str(destination), "host_home": str(Path.home())}}
    else:
        if not operator_file.is_absolute() or operator_file.is_symlink():
            raise ValueError("operator configuration must be an absolute regular file")
        values = json.loads(operator_file.read_text(encoding="utf-8"))
    contract = CONTRACT.OperatorContract(values, workspace=destination)
    if contract.require_path("paths.workspace") != destination:
        raise ValueError("operator paths.workspace must equal the installation destination")
    # Persist the derived binding too; readers must agree after this script exits.
    values.setdefault("paths", {})["workspace"] = str(destination)
    contract = CONTRACT.OperatorContract(values, workspace=destination)
    files = collect_files()
    preferences = json.loads((ROOT / "config/openclaw.preferences.json").read_text(encoding="utf-8"))
    generated = {
        Path("operator.json"): values,
        Path("config/openclaw.json"): contract.render(preferences),
    }
    for target, value in generated.items():
        if target in files:
            raise ValueError(f"generated target collides with bundle source: {target}")
        files[target] = ((json.dumps(value, indent=2) + "\n").encode(), 0o600)
    # Human instructions may include argument placeholders. Only these three
    # host-path tokens are install bindings; do not rewrite arbitrary prose/code.
    bindings = {
        "<artifact-root>": str(destination / "artifacts"),
        "<legacy-state-root>": contract.get("paths.legacy_state_root"),
        "<project-data-root>": contract.get("paths.project_data_root"),
    }
    for name in ["AGENTS.md", "TOOLS.md"]:
        path = Path(name)
        payload, mode = files[path]
        body = payload.decode()
        for token, value in bindings.items():
            if value is not None:
                if token != "<artifact-root>":
                    key = "paths.legacy_state_root" if token == "<legacy-state-root>" else "paths.project_data_root"
                    value = str(contract.require_path(key))
                body = body.replace(token, value)
        files[path] = (body.encode(), mode)
    return files


def install(destination: Path, files: dict[Path, tuple[bytes, int]]) -> dict:
    # Exclusive creation claims a new destination; no existing workspace is changed.
    destination.mkdir(mode=0o700)
    rows = []
    try:
        for relative, (payload, mode) in sorted(files.items()):
            target = destination / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
            rows.append({"path": str(relative), "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "mode": oct(mode)})
        receipt = {"schema_version": 1, "status": "installed", "services_started": False, "files": rows}
        target = destination / "installation-manifest.json"
        with target.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2)
            stream.write("\n")
        target.chmod(0o600)
        return receipt
    except Exception:
        # Preserve an incomplete installation for inspection; never recursively
        # remove a path after an unexpected failure or race with another writer.
        raise RuntimeError(f"installation incomplete; inspect the new directory: {destination}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--operator-config", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        files = make_plan(args.destination, args.operator_config)
        if args.dry_run:
            print(json.dumps({"status": "planned", "files": len(files), "services_started": False}))
        else:
            receipt = install(args.destination, files)
            print(json.dumps({"status": receipt["status"], "files": len(receipt["files"]), "services_started": False}))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
