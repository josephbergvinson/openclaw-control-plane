"""Isolated synthetic installation for inherited routing behavior tests.

The distributable status remains unverified. These tests install synthetic
ready rows in a temporary directory and never copy provider credentials.
"""
from __future__ import annotations
import atexit
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import hashlib
import sys

_ROOT = None


def fixture_root():
    global _ROOT
    if _ROOT is not None:
        return _ROOT
    source = Path(__file__).resolve().parents[1]
    temporary = tempfile.TemporaryDirectory(prefix="openclaw-routing-tests-")
    atexit.register(temporary.cleanup)
    root = Path(temporary.name)
    for name in ("scripts", "registry", "status", "config"):
        if (source / name).exists():
            shutil.copytree(source / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(source / "tests/fixtures", root / "tests/fixtures")
    values = json.loads((source / "tests/fixtures/routing-operator.json").read_text())
    values["paths"]["workspace"] = str(root)
    values["paths"]["python_binary"] = sys.executable
    node = shutil.which("node")
    if node:
        values["paths"]["node_binary"] = node
    tools = json.loads((source / "tests/fixtures/company_alpha_docs_mcp/remote_tools.json").read_text())["tools"]
    projection = ["name", "inputSchema", "annotations", "execution"]
    projected = [{key: tool.get(key) for key in projection} for tool in tools]
    digest = hashlib.sha256(json.dumps(projected, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    values["services"]["company_alpha_docs"] = {
        "endpoint": "https://docs.company-alpha.example.invalid/mcp",
        "origin": "https://docs.company-alpha.example.invalid",
        "server_name": "CompanyAlpha", "server_version": "1.0.0",
        "search": "search_company_alpha", "read": "query_docs_filesystem_company_alpha",
        "expected_inventory": [tool["name"] for tool in tools],
        "blocked_inventory": ["submit_feedback"], "tool_schema_sha256": digest,
    }
    configuration = root / "operator.json"
    configuration.write_text(json.dumps(values))
    os.environ["OPENCLAW_OPERATOR_CONFIG"] = str(configuration)
    from scripts.operator_contract import OperatorContract
    from scripts import routing_operator_bindings
    routing_operator_bindings._CONTRACT = OperatorContract(values, workspace=root)
    routing_operator_bindings._CONFIG_SELECTION = str(configuration)
    from scripts.routing_operator_bindings import materialize
    for p in (root / "registry").glob("*.json"):
        data = materialize(json.loads(p.read_text()))
        if p.name == "integration_routes.json":
            for handle in data["credential_handles"]:
                handle["provisioning_state"] = "provisioned"
        p.write_text(json.dumps(data, indent=2))
        if p.name in ("integration_routes.json", "probes.json"):
            p.with_suffix(".yaml").write_text(p.read_text())
    for p in (root / "config").glob("*.json"):
        p.write_text(json.dumps(materialize(json.loads(p.read_text())), indent=2))
    status_path = root / "status/capability_status.json"
    status = json.loads((source / "tests/fixtures/routing-capability-status.json").read_text())
    for row in status["capabilities"]:
        row.update(evidence="Synthetic local routing test fixture; no provider observation.")
    status_path.write_text(json.dumps(status))
    for p in (source.parent / "templates").glob("*.example.md"):
        (root / p.name.replace(".example", "")).write_text(p.read_text())
    _ROOT = root
    return root
