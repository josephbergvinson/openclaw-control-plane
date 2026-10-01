from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("materialize_host", ROOT / "scripts/materialize_host.py")
assert SPEC and SPEC.loader
HOST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOST)


class HostMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        (self.workspace / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "workspace/scripts/operator_contract.py", self.workspace / "scripts/operator_contract.py")
        (self.workspace / "operator.json").write_text(json.dumps({"paths": {"workspace": str(self.workspace), "node_binary": "/example/node"}}))
        self.source = self.workspace / "launch.template.json"
        self.source.write_text(json.dumps({"Label": "org.example.agent", "Disabled": True, "RunAtLoad": False, "ProgramArguments": ["${operator:paths.node_binary}", "example.mjs"]}))
        self.manifest = self.workspace / "host-templates.json"
        self.entries = [{"source": "launch.template.json", "target": "launchd/agent.plist", "format": "plist"}]
        self.manifest.write_text(json.dumps({"schema_version": 1, "templates": self.entries}))

    def test_renders_plist_into_separate_directory_only(self):
        output = self.root / "review"
        data = HOST.plan(self.workspace)
        HOST.write_outputs(output, data)
        plist = plistlib.loads((output / "launchd/agent.plist").read_bytes())
        self.assertEqual("/example/node", plist["ProgramArguments"][0])
        self.assertFalse(plist["RunAtLoad"])
        self.assertTrue(plist["Disabled"])
        self.assertFalse((self.workspace / "launchd").exists())
        receipt = json.loads((output / "materialization-manifest.json").read_text())
        self.assertFalse(receipt["installed"])
        self.assertFalse(receipt["services_started"])

    def test_missing_binding_prevents_any_output(self):
        self.source.write_text(json.dumps({"RunAtLoad": False, "ProgramArguments": ["${operator:paths.absent}"]}))
        with self.assertRaises(ValueError):
            HOST.plan(self.workspace)

    def test_parent_traversal_rejected(self):
        self.entries[0]["target"] = "../escape"
        self.manifest.write_text(json.dumps({"schema_version": 1, "templates": self.entries}))
        with self.assertRaises(ValueError):
            HOST.plan(self.workspace)

    def test_active_launch_on_load_rejected(self):
        self.source.write_text(json.dumps({"Disabled": True, "RunAtLoad": True}))
        with self.assertRaisesRegex(ValueError, "RunAtLoad"):
            HOST.plan(self.workspace)

    def test_disabled_is_required_even_when_run_at_load_is_false(self):
        for disabled in (None, False, 1, "true"):
            with self.subTest(disabled=disabled):
                value = {"RunAtLoad": False, "KeepAlive": True}
                if disabled is not None:
                    value["Disabled"] = disabled
                self.source.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, "Disabled"):
                    HOST.plan(self.workspace)

    def test_disabled_retired_definition_can_omit_all_triggers(self):
        self.source.write_text(json.dumps({"Label": "org.example.retired", "Disabled": True}))
        value = plistlib.loads(HOST.plan(self.workspace)[Path("launchd/agent.plist")])
        self.assertTrue(value["Disabled"])
        self.assertNotIn("RunAtLoad", value)
        self.assertNotIn("KeepAlive", value)

    def test_disabled_keepalive_definition_preserves_its_reviewed_trigger(self):
        self.source.write_text(json.dumps({"Disabled": True, "RunAtLoad": False, "KeepAlive": True}))
        value = plistlib.loads(HOST.plan(self.workspace)[Path("launchd/agent.plist")])
        self.assertTrue(value["Disabled"])
        self.assertTrue(value["KeepAlive"])

    def runtime_contract(self):
        home = self.root / "home"
        return {
            "paths": {
                "workspace": str(self.workspace), "host_home": str(home),
                "node_binary": str(self.root / "node-pinned/bin/node"),
                "runtime_package_link": str(self.root / "package"),
                "state_root": str(self.root / "state"),
                "gateway_plist": str(home / "Library/LaunchAgents/ai.openclaw.gateway.plist"),
                "node_plist": str(home / "Library/LaunchAgents/ai.openclaw.node.plist"),
            },
            "runtime": {"gateway_label": "ai.openclaw.gateway", "node_label": "ai.openclaw.node", "gateway_port": 19001},
        }

    def test_runtime_only_mode_stages_exact_native_contract_without_maintenance_bindings(self):
        contract = self.runtime_contract()
        (self.workspace / "operator.json").write_text(json.dumps(contract))
        self.manifest.unlink()
        outputs = HOST.runtime_service_plan(self.workspace)
        self.assertEqual(2, len(outputs))
        for role, arguments in (("gateway", ["gateway", "--port", "19001"]),
                                ("node", ["node", "run", "--host", "127.0.0.1", "--port", "19001"])):
            value = plistlib.loads(outputs[Path(f"launchd/ai.openclaw.{role}.plist")])
            self.assertEqual([contract["paths"]["node_binary"], str(self.root / "package/dist/index.js"), *arguments], value["ProgramArguments"])
            self.assertEqual(contract["paths"]["host_home"], value["WorkingDirectory"])
            self.assertEqual(contract["paths"]["state_root"], value["EnvironmentVariables"]["OPENCLAW_STATE_DIR"])
            self.assertEqual("external", value["EnvironmentVariables"]["OPENCLAW_SUPERVISOR_MODE"])
            self.assertEqual("external", value["EnvironmentVariables"]["OPENCLAW_SERVICE_REPAIR_POLICY"])
            self.assertTrue(value["Disabled"])
            self.assertFalse(value["RunAtLoad"])
            self.assertFalse(value["KeepAlive"])
            self.assertFalse(Path(contract["paths"][f"{role}_plist"]).exists())

    def test_runtime_service_identity_must_be_addressable_by_native_commands(self):
        for key, value in (("gateway_label", "org.example.custom-gateway"), ("node_label", "org.example.custom-node")):
            with self.subTest(key=key):
                contract = self.runtime_contract()
                contract["runtime"][key] = value
                (self.workspace / "operator.json").write_text(json.dumps(contract))
                with self.assertRaisesRegex(ValueError, "native default service labels"):
                    HOST.runtime_service_plan(self.workspace)

    def test_runtime_rejects_invalid_port_and_unaddressable_plist(self):
        cases = [("runtime", "gateway_port", 0), ("runtime", "gateway_port", 65536),
                 ("paths", "gateway_plist", str(self.root / "elsewhere/ai.openclaw.gateway.plist")),
                 ("paths", "node_plist", str(self.root / "home/Library/LaunchAgents/wrong.plist"))]
        for section, key, value in cases:
            with self.subTest(key=key, value=value):
                contract = self.runtime_contract()
                contract[section][key] = value
                (self.workspace / "operator.json").write_text(json.dumps(contract))
                with self.assertRaises(ValueError):
                    HOST.runtime_service_plan(self.workspace)

    def first_install_fixture(self):
        contract = self.runtime_contract()
        contract["paths"].update({
            "runtime_current_link": str(self.root / "runtime/current"),
            "openclaw_cli": str(self.root / "bin/openclaw"),
            "runtime_node_alias": str(self.root / "node-alias"),
        })
        (self.workspace / "operator.json").write_text(json.dumps(contract))
        node = Path(contract["paths"]["node_binary"])
        node.parent.mkdir(parents=True)
        node.write_text("#!/bin/sh\nexit 99\n")
        node.chmod(0o700)
        Path(contract["paths"]["state_root"]).mkdir()
        release = self.root / "releases/initial"
        release.mkdir(parents=True)
        (release / "openclaw.mjs").write_text("// fixture entrypoint; never executed\n")
        (release / "dist").mkdir()
        (release / "dist/index.js").write_text("// fixture entrypoint; never executed\n")
        staged = self.root / "runtime-review"
        HOST.write_outputs(staged, HOST.runtime_service_plan(self.workspace))
        return contract, release, staged

    def run_documented_preparation(self, release, staged):
        guide = (ROOT / "docs/17-adoption-guide.md").read_text()
        snippet = guide.split("<<'PYCODE'\n", 1)[1].split("\nPYCODE", 1)[0]
        return subprocess.run([sys.executable, "-", str(self.workspace), str(release), str(staged)],
                              input=snippet, capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                                   "OPENCLAW_OPERATOR_CONFIG": str(self.workspace / "operator.json")})

    def test_documented_first_install_produces_the_actual_activator_link_and_plist_contract(self):
        contract, release, staged = self.first_install_fixture()
        result = self.run_documented_preparation(release, staged)
        self.assertEqual(0, result.returncode, result.stderr)
        paths = contract["paths"]
        self.assertEqual(str(release), os.readlink(paths["runtime_current_link"]))
        for role in ("gateway", "node"):
            value = plistlib.loads(Path(paths[f"{role}_plist"]).read_bytes())
            self.assertFalse(value["Disabled"])
            self.assertTrue(value["RunAtLoad"])
            self.assertTrue(value["KeepAlive"])
            self.assertEqual(0o600, Path(paths[f"{role}_plist"]).stat().st_mode & 0o777)
        # Invoke the exported validators against these real fixture files. No
        # backend, seal, service, socket, provider or executable is invoked.
        shutil.copy2(ROOT / "workspace/scripts/openclaw_runtime_activate.py", self.workspace / "scripts/openclaw_runtime_activate.py")
        check = """from types import SimpleNamespace
import openclaw_runtime_activate as a
c = a.OPERATOR
p = SimpleNamespace(**{name: c.require_path(key) for name, key in {
 'current_link': 'paths.runtime_current_link', 'package_link': 'paths.runtime_package_link',
 'bin_link': 'paths.openclaw_cli', 'node_alias': 'paths.runtime_node_alias',
 'node': 'paths.node_binary', 'gateway_plist': 'paths.gateway_plist',
 'node_plist': 'paths.node_plist', 'candidate_state_dir': 'paths.state_root',
}.items()})
a.validate_support_links(p)
assert set(a.validate_plists(p)) == {'gateway', 'node'}
"""
        checked = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True,
                                 env={"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                                      "PYTHONPATH": str(self.workspace / "scripts"),
                                      "OPENCLAW_OPERATOR_CONFIG": str(self.workspace / "operator.json")})
        self.assertEqual(0, checked.returncode, checked.stderr)
        default_marker = Path(paths["host_home"]) / ".openclaw/disable-launchagent"
        canonical_marker = Path(paths["state_root"]) / "disable-launchagent"
        self.assertTrue(default_marker.is_file())
        self.assertFalse(default_marker.is_symlink())
        self.assertTrue(canonical_marker.is_file())
        # A manual/default-profile launch may precede the login environment.
        # Its physical native marker survives the canonical volume being absent.
        state = Path(paths["state_root"])
        away = state.with_name(state.name + "-unmounted")
        state.rename(away)
        try:
            self.assertTrue(default_marker.is_file())
            self.assertFalse(canonical_marker.exists())
        finally:
            away.rename(state)
        before = Path(paths["gateway_plist"]).read_bytes()
        repeated = self.run_documented_preparation(release, staged)
        self.assertNotEqual(0, repeated.returncode)
        self.assertIn("already exists", repeated.stderr)
        self.assertEqual(before, Path(paths["gateway_plist"]).read_bytes())

    def test_documented_first_install_refuses_a_dangling_existing_selector_before_writes(self):
        contract, release, staged = self.first_install_fixture()
        alias = Path(contract["paths"]["runtime_node_alias"])
        alias.symlink_to(self.root / "absent")
        result = self.run_documented_preparation(release, staged)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("already exists", result.stderr)
        self.assertFalse(os.path.lexists(contract["paths"]["runtime_current_link"]))
        self.assertFalse(Path(contract["paths"]["gateway_plist"]).exists())
        self.assertEqual(str(self.root / "absent"), os.readlink(alias))

    def test_documented_first_install_refuses_overlapping_contract_paths_before_writes(self):
        contract, release, staged = self.first_install_fixture()
        contract["paths"]["openclaw_cli"] = contract["paths"]["runtime_package_link"]
        (self.workspace / "operator.json").write_text(json.dumps(contract))
        result = self.run_documented_preparation(release, staged)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("must be distinct", result.stderr)
        self.assertFalse(os.path.lexists(contract["paths"]["runtime_current_link"]))

    def test_existing_output_is_untouched(self):
        output = self.root / "review"
        output.mkdir()
        sentinel = output / "keep"
        sentinel.write_text("preserve")
        with self.assertRaises(ValueError):
            HOST.write_outputs(output, HOST.plan(self.workspace))
        self.assertEqual([sentinel], list(output.iterdir()))

    def test_source_symlink_is_rejected(self):
        self.source.unlink()
        external = self.root / "external.json"
        external.write_text('{}')
        self.source.symlink_to(external)
        with self.assertRaises(ValueError):
            HOST.plan(self.workspace)


if __name__ == "__main__":
    unittest.main()
