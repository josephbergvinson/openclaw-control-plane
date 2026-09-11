#!/usr/bin/env python3
"""Render an installed workspace's host templates into a new review directory.

This writes files only. It does not install, load, schedule or execute them.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import sys


def contained_file(workspace: Path, relative: str) -> Path:
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts or not part.parts:
        raise ValueError("template path must be workspace-relative without parent traversal")
    path = workspace / part
    if not path.is_file() or path.resolve() != path:
        raise ValueError(f"template must be a regular file without symlink traversal: {relative}")
    return path


def load_contract(workspace: Path, operator_file: Path | None = None):
    if not workspace.is_absolute() or workspace.resolve() != workspace:
        raise ValueError("workspace must be an absolute directory without symlink traversal")
    module_file = contained_file(workspace, "scripts/operator_contract.py")
    spec = importlib.util.spec_from_file_location("installed_operator_contract", module_file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    contract = module.load_operator_contract(operator_file or workspace / "operator.json")
    if contract.require_path("paths.workspace") != workspace:
        raise ValueError("operator workspace does not match the selected installation")
    return contract


def plan(workspace: Path, operator_file: Path | None = None) -> dict[Path, bytes]:
    contract = load_contract(workspace, operator_file)
    manifest = json.loads(contained_file(workspace, "host-templates.json").read_text())
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or not isinstance(manifest.get("templates"), list) or not manifest["templates"]):
        raise ValueError("host-templates.json must declare schema_version 1 and a nonempty templates list")
    outputs: dict[Path, bytes] = {}
    for item in manifest["templates"]:
        if not isinstance(item, dict) or set(item) != {"source", "target", "format"}:
            raise ValueError("invalid host template descriptor")
        if any(not isinstance(item[key], str) for key in item):
            raise ValueError("template descriptor fields must be strings")
        target = Path(item["target"])
        if target.is_absolute() or ".." in target.parts or not target.parts or target in outputs or str(target) == "materialization-manifest.json":
            raise ValueError("invalid or duplicate output target")
        rendered = contract.render(json.loads(contained_file(workspace, item["source"]).read_text()))
        if re.search(r"<[^>]+>|\$\{|\{\{|\b(?:REPLACE_ME|CHANGEME)\b", json.dumps(rendered), re.I):
            raise ValueError(f"unresolved value in host template: {item['source']}")
        if item["format"] == "plist":
            if not isinstance(rendered, dict):
                raise ValueError("launch definition must be an object")
            validate_staged_plist(rendered)
            outputs[target] = plistlib.dumps(rendered, sort_keys=True)
        elif item["format"] == "json":
            outputs[target] = (json.dumps(rendered, indent=2) + "\n").encode()
        else:
            raise ValueError("unsupported host template format")
    return outputs


def validate_staged_plist(value: dict) -> None:
    if value.get("Disabled") is not True:
        raise ValueError("staged launch definitions must declare Disabled=true")
    # Retired definitions deliberately omit every launch trigger. Absence is
    # accepted; an explicit true/nonboolean value is not a disabled staging file.
    if "RunAtLoad" in value and value["RunAtLoad"] is not False:
        raise ValueError("staged launch definitions must omit RunAtLoad or set it false")
    # KeepAlive is retained where it belongs to a reviewed service definition.
    # RunAtLoad=false alone does not disable that trigger; Disabled=true above
    # is required for every staged definition, including keepawake services.


def runtime_service_plan(workspace: Path, operator_file: Path | None = None) -> dict[Path, bytes]:
    """Stage the two exact service definitions used by the lifecycle owner.

    No selector, installed plist, launchd state or native runtime state is changed.
    The adopter deliberately enables the reviewed definitions at first install.
    """
    contract = load_contract(workspace, operator_file)
    node = contract.require_path("paths.node_binary")
    package = contract.require_path("paths.runtime_package_link")
    state = contract.require_path("paths.state_root")
    home = contract.require_path("paths.host_home")
    port = contract.require_int("runtime.gateway_port")
    if not 1 <= port <= 65535:
        raise ValueError("runtime.gateway_port must be between 1 and 65535")
    environment = {
        "HOME": str(home), "PATH": str(node.parent) + ":/usr/bin:/bin:/usr/sbin:/sbin",
        "OPENCLAW_STATE_DIR": str(state), "OPENCLAW_CONFIG_PATH": str(state / "openclaw.json"),
        "OPENCLAW_OPERATOR_CONFIG": str(operator_file or workspace / "operator.json"),
        "OPENCLAW_SUPERVISOR_MODE": "external", "OPENCLAW_SERVICE_REPAIR_POLICY": "external",
    }
    outputs: dict[Path, bytes] = {}
    labels = set()
    for role in ("gateway", "node"):
        label = contract.require_string(f"runtime.{role}_label")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label) or label in labels:
            raise ValueError("runtime service labels must be distinct launchd labels")
        if label != f"ai.openclaw.{role}":
            raise ValueError("first-install staging requires native default service labels ai.openclaw.gateway and ai.openclaw.node")
        labels.add(label)
        installed = contract.require_path(f"paths.{role}_plist")
        if installed.name != label + ".plist":
            raise ValueError("runtime plist filename must match its launchd label")
        if installed.parent != home / "Library/LaunchAgents":
            raise ValueError("first-install runtime plists must use the operator's native Library/LaunchAgents directory")
        arguments = [str(node), str(package / "dist/index.js")]
        arguments += (["gateway", "--port", str(port)] if role == "gateway" else
                      ["node", "run", "--host", "127.0.0.1", "--port", str(port)])
        value = {
            "Label": label, "ProgramArguments": arguments, "WorkingDirectory": str(home),
            "EnvironmentVariables": environment, "Disabled": True, "RunAtLoad": False,
            "KeepAlive": False, "StandardOutPath": str(state / "logs" / f"{role}.stdout.log"),
            "StandardErrorPath": str(state / "logs" / f"{role}.stderr.log"),
        }
        validate_staged_plist(value)
        outputs[Path("launchd") / installed.name] = plistlib.dumps(value, sort_keys=True)
    return outputs


def write_outputs(output: Path, outputs: dict[Path, bytes]) -> None:
    if (not output.is_absolute() or ".." in output.parts or output.exists()
            or output.is_symlink() or not output.parent.is_dir()
            or output.parent.resolve() != output.parent):
        raise ValueError("output must be a new absolute directory under an existing physical parent")
    output.mkdir(mode=0o700)
    rows = []
    for relative, payload in sorted(outputs.items()):
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(payload)
        rows.append({"path": str(relative), "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    receipt = output / "materialization-manifest.json"
    with os.fdopen(os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump({"schema_version": 1, "status": "rendered", "installed": False, "services_started": False, "files": rows}, stream, indent=2)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--operator-config", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--runtime-services", action="store_true", help="Stage only gateway/node definitions, disabled and not installed")
    args = parser.parse_args(argv)
    try:
        outputs = (runtime_service_plan if args.runtime_services else plan)(args.workspace, args.operator_config)
        if not args.dry_run:
            write_outputs(args.output, outputs)
        print(json.dumps({"status": "planned" if args.dry_run else "rendered", "files": len(outputs), "installed": False}))
        return 0
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
