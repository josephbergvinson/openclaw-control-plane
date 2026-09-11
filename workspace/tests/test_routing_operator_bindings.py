"""Adopter binding regressions; no credentials or provider transports are used."""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
from scripts.operator_contract import ContractError, OperatorContract
from scripts import routing_operator_bindings as bindings


class RoutingOperatorBindingTests(unittest.TestCase):
    def test_missing_binding_is_selected_route_local_and_never_calls_transport(self):
        contract = OperatorContract({"paths": {"python_binary": sys.executable}}, workspace=WORKSPACE)
        with patch.object(bindings, "_CONTRACT", contract):
            resolver = runpy.run_path(str(WORKSPACE / "scripts/resolve_capability.py"))
            apple = resolver["resolve"]("apple-calendar", "read", required_operation="calendar-list",
                requested_principal="operator", requested_account="local-apple-calendar")
            self.assertTrue(apple["execution_guard"]["allowed"])
            selected = dict(apple["preferred_lane"], required_account=bindings.binding("identifiers.accounts.personal_google"))
            def forbidden(*_args):
                self.fail("a missing account must fail before any provider call")
            evidence, diagnostics = resolver["run_exact_registered_probe"](selected, forbidden)
            self.assertIsNone(evidence)
            self.assertEqual(diagnostics["result"], "blocked_operator_binding_missing")
            self.assertFalse(diagnostics["attempted"])

    def test_commands_preserve_literal_argument_boundaries(self):
        values = {"paths": {"openclaw_cli": "/tmp/an installation/cli"},
                  "services": {"discord": {"channel_id": "123; printf unsafe"}}}
        with patch.object(bindings, "_CONTRACT", OperatorContract(values)):
            result = bindings.materialize({"$operator_argv": ["${operator:paths.openclaw_cli}",
                {"$operator_prefix": "channel:", "value": "${operator:services.discord.channel_id}"}]})
        self.assertEqual(shlex.split(result), ["/tmp/an installation/cli", "channel:123; printf unsafe"])
        with self.assertRaises(ContractError):
            bindings.materialize({"$operator_prefix": "$(", "value": "anything"})

    def test_provider_identity_absent_from_argv_is_required_before_transport(self):
        with patch.object(bindings, "_CONTRACT", OperatorContract({})):
            resolver = runpy.run_path(str(WORKSPACE / "scripts/resolve_capability.py"))
            for probe_id in ("trello-personal-api-probe", "cloudflare-operator-readonly-probe"):
                def forbidden(*_args):
                    self.fail("missing provider identity must not contact the provider")
                evidence, diagnostics = resolver["run_exact_registered_probe"]({"probe_id": probe_id}, forbidden)
                self.assertIsNone(evidence)
                self.assertEqual(diagnostics["result"], "blocked_operator_binding_missing")
                self.assertFalse(diagnostics["attempted"])

    def test_discord_channels_and_optional_manual_probes_are_bound(self):
        probes = json.loads((WORKSPACE / "registry/probes.json").read_text())["probes"]
        for scope in ("company_alpha", "company_beta", "operations"):
            selected = [p for p in probes if p["probe_id"] == "discord-source-" + scope.replace("_", "-") + "-team-probe"]
            self.assertEqual(len(selected), 1)
            encoded = json.dumps(selected[0]["command"])
            self.assertIn("services.discord." + scope + ".channel_id", encoded)
            self.assertNotIn("channel:operator-owned-value", encoded)
        digitalocean = next(p for p in probes if p["probe_id"] == "digitalocean-doctl-account-probe")
        self.assertEqual(digitalocean["command"]["$operator_argv"], ["${operator:paths.doctl_binary}", "account", "get"])
        gmail = next(p for p in probes if p["probe_id"] == "gmail-company-alpha-coordinator-route-probe")
        self.assertIsInstance(gmail["command"], str)
        self.assertIn("no executable exact probe", gmail["command"])

    def test_python_string_reader_rejects_control_characters_and_placeholders(self):
        for value in ("CHANGEME", "replace_me", "name\tbad", "name\x7fbad", "<operator>"):
            with self.subTest(value=value), patch.object(bindings, "_CONTRACT", OperatorContract({"identifiers": {"host_user": value}})):
                with self.assertRaises(ContractError):
                    bindings.require_resolved(bindings.binding("identifiers.host_user"))

    def test_confined_child_keeps_explicit_configuration_without_ambient_secrets(self):
        configuration = "/tmp/synthetic-operator-config.json"
        with patch.object(bindings, "_CONFIG_SELECTION", configuration):
            resolver = runpy.run_path(str(WORKSPACE / "scripts/resolve_capability.py"))
            with patch.dict(os.environ, {"AMBIENT_TEST_SECRET": "synthetic-do-not-inherit"}):
                result = resolver["subprocess_probe_transport"]((sys.executable, "-E", "-s", "-c",
                    'import json,os; print(json.dumps({"config":os.environ.get("OPENCLAW_OPERATOR_CONFIG"), "secret":os.environ.get("AMBIENT_TEST_SECRET")}))'), 5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"config": configuration, "secret": None})

    def node_read(self, values, expression):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for the adapter contract tests")
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "operator.json"
            config.write_text(json.dumps(values))
            script = "import * as b from " + json.dumps((WORKSPACE / "scripts/routing_operator_bindings.mjs").as_uri()) + ";\n" + expression
            return subprocess.run([node, "--input-type=module", "--eval", script],
                env=dict(os.environ, OPENCLAW_OPERATOR_CONFIG=str(config)), text=True, capture_output=True, timeout=10)

    def test_node_schema_rejection_and_derived_defaults(self):
        for values in ([], {"schema_version": 2}, {"schema_version": True}):
            self.assertNotEqual(self.node_read(values, "").returncode, 0)
        result = self.node_read({}, 'console.log(JSON.stringify([b.binding("paths.workspace"), b.binding("paths.host_home")]))')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [str(WORKSPACE), str(Path.home())])

    def test_node_selected_strings_and_typed_references(self):
        for value in ("CHANGEME", "replace_me", "name\tbad", "name\x7fbad", "<operator>"):
            self.assertNotEqual(self.node_read({"identifiers": {"host_user": value}},
                'b.requireResolved(b.binding("identifiers.host_user"));').returncode, 0)
        result = self.node_read({"services": {"inventory": ["read", "search"]}},
            'console.log(JSON.stringify(b.materializeOperatorValue("${operator:services.inventory}")))')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["read", "search"])


if __name__ == "__main__":
    unittest.main()
