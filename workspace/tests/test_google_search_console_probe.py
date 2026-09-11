from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import argparse
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml

from routing_test_support import fixture_root
ROOT = fixture_root()
PROBE = ROOT / "scripts" / "google_search_console_probe.py"
ACCOUNT = _operator_binding('identifiers.accounts.personal_google')
DOMAIN = _operator_binding('services.cloudflare.zone_name')
PROPERTY_URI = f"https://{DOMAIN}/"
SECRET_MARKER = "never-print-this-keyring-secret"
TRUSTED_ENV = Path(_operator_binding('paths.gog_env_file'))
TRUSTED_GOG = Path(_operator_binding('paths.gog_binary'))

SPEC = importlib.util.spec_from_file_location("google_search_console_probe", PROBE)
assert SPEC is not None and SPEC.loader is not None
PROBE_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE_MODULE)


class GoogleSearchConsoleProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.env_file = self.root / "gog.env"
        self.env_file.write_text(
            f"UNRELATED_RUNTIME_KEY=kept-out-of-child-env\nGOG_KEYRING_PASSWORD={SECRET_MARKER}\n",
            encoding="utf-8",
        )
        self.env_file.chmod(0o600)
        self.gog = self.root / "gog"
        self.gog.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import os
                import sys

                mode = "ready"
                if mode == "scope_missing":
                    print("403 insufficientPermissions", file=sys.stderr)
                    raise SystemExit(6)
                if mode == "wrong_property":
                    print(json.dumps({{"sites": [{{"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}}]}}))
                    raise SystemExit(0)
                if mode == "domain_property":
                    print(json.dumps({{"sites": [{{"siteUrl": "sc-domain:{DOMAIN}", "permissionLevel": "siteOwner"}}]}}))
                    raise SystemExit(0)
                if mode == "ambiguous":
                    print(json.dumps({{"sites": [
                        {{"siteUrl": "https://{DOMAIN}/", "permissionLevel": "siteOwner"}},
                        {{"siteUrl": "sc-domain:{DOMAIN}", "permissionLevel": "siteFullUser"}}
                    ]}}))
                    raise SystemExit(0)
                if mode == "unverified":
                    print(json.dumps({{"sites": [{{"siteUrl": "https://{DOMAIN}/", "permissionLevel": "siteUnverifiedUser"}}]}}))
                    raise SystemExit(0)
                if mode == "environment_pinned":
                    if os.environ.get("HOME") != "/tmp/openclaw-routing-fixture/host_home":
                        print("HOME was not pinned", file=sys.stderr)
                        raise SystemExit(17)
                    if os.environ.get("GOG_ACCOUNT") != "{ACCOUNT}":
                        print("route account was not pinned", file=sys.stderr)
                        raise SystemExit(19)
                    redirected = [
                        key
                        for key in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
                        if key in os.environ
                    ]
                    if redirected:
                        print("XDG credential location leaked", file=sys.stderr)
                        raise SystemExit(18)
                print(json.dumps({{"sites": [{{"siteUrl": "https://{DOMAIN}/", "permissionLevel": "siteOwner"}}]}}))
                """
            ),
            encoding="utf-8",
        )
        self.gog.chmod(0o700)
        self.original_env_file = PROBE_MODULE.TRUSTED_ENV_FILE
        self.original_gog_bin = PROBE_MODULE.TRUSTED_GOG_BIN
        self.original_receipt_dir = PROBE_MODULE.TRUSTED_RECEIPT_DIR
        PROBE_MODULE.TRUSTED_ENV_FILE = self.env_file
        PROBE_MODULE.TRUSTED_GOG_BIN = self.gog
        PROBE_MODULE.TRUSTED_RECEIPT_DIR = self.root / "receipts"

    def tearDown(self) -> None:
        PROBE_MODULE.TRUSTED_ENV_FILE = self.original_env_file
        PROBE_MODULE.TRUSTED_GOG_BIN = self.original_gog_bin
        PROBE_MODULE.TRUSTED_RECEIPT_DIR = self.original_receipt_dir
        self.tempdir.cleanup()

    def select_mode(self, mode: str) -> None:
        if mode == "ready":
            PROBE_MODULE.TRUSTED_GOG_BIN = self.gog
            return
        selected = self.root / f"gog-{mode}"
        selected.write_text(
            self.gog.read_text(encoding="utf-8").replace(
                'mode = "ready"',
                f"mode = {mode!r}",
            ),
            encoding="utf-8",
        )
        selected.chmod(0o700)
        PROBE_MODULE.TRUSTED_GOG_BIN = selected

    def run_probe(
        self,
        *,
        mode: str = "ready",
        account: str = ACCOUNT,
        property_domain: str | None = DOMAIN,
    ) -> tuple[int, dict]:
        self.select_mode(mode)
        exit_code, payload = PROBE_MODULE.run_probe(
            argparse.Namespace(
                account=account,
                property_domain=property_domain,
                timeout_seconds=5.0,
            )
        )
        serialized = json.dumps(payload)
        self.assertNotIn(SECRET_MARKER, serialized)
        return exit_code, payload

    def test_cli_has_no_caller_override_for_gog_or_credential_env(self) -> None:
        self.assertEqual(self.original_gog_bin, TRUSTED_GOG)
        self.assertEqual(self.original_env_file, TRUSTED_ENV)
        parser_destinations = {action.dest for action in PROBE_MODULE.build_parser()._actions}
        self.assertNotIn("gog_bin", parser_destinations)
        self.assertNotIn("env_file", parser_destinations)
        self.assertNotIn("property_uri", parser_destinations)
        self.assertNotIn("property_type", parser_destinations)

        completed = subprocess.run(
            [
                sys.executable,
                str(PROBE),
                "--account",
                ACCOUNT,
                "--property-domain",
                DOMAIN,
                "--gog-bin",
                str(self.gog),
                "--env-file",
                str(self.env_file),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unrecognized arguments", completed.stderr)

    def test_caller_home_and_xdg_cannot_redirect_gog_credential_store(self) -> None:
        redirected_environment = {
            "HOME": str(self.root / "redirected-home"),
            "XDG_CACHE_HOME": str(self.root / "redirected-cache"),
            "XDG_CONFIG_HOME": str(self.root / "redirected-config"),
            "XDG_DATA_HOME": str(self.root / "redirected-data"),
        }
        with mock.patch.dict(os.environ, redirected_environment, clear=False):
            exit_code, payload = self.run_probe(mode="environment_pinned")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ready")

    def test_ready_account_and_property_is_audit_only(self) -> None:
        exit_code, payload = self.run_probe()
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["property_accessible"])
        self.assertEqual(payload["account"], ACCOUNT)
        self.assertEqual(payload["requested_property_domain"], DOMAIN)
        self.assertEqual(payload["property_uri"], PROPERTY_URI)
        self.assertEqual(payload["property_type"], "url-prefix")
        self.assertEqual(payload["permission_level"], "siteOwner")
        self.assertFalse(payload["property_selection_required"])
        self.assertEqual(payload["producer"], "google_search_console_probe.py")
        self.assertEqual(payload["evidence_role"], "audit_only")
        self.assertFalse(payload["authoritative"])
        self.assertFalse(payload["completion_claim_allowed"])
        self.assertFalse(payload["page_indexing_example_table_verified"])
        self.assertIn("does not prove task completion", payload["evidence"])

    def test_ready_and_blocked_results_are_atomically_written_mode_0600(self) -> None:
        ready_receipt = PROBE_MODULE.TRUSTED_RECEIPT_DIR / "ready.json"
        exit_code, payload = self.run_probe()
        self.assertEqual(exit_code, 0)
        PROBE_MODULE.write_receipt(str(ready_receipt), payload)
        self.assertEqual(json.loads(ready_receipt.read_text(encoding="utf-8")), payload)
        self.assertEqual(stat.S_IMODE(ready_receipt.stat().st_mode), 0o600)

        blocked_receipt = PROBE_MODULE.TRUSTED_RECEIPT_DIR / "blocked.json"
        exit_code, payload = self.run_probe(mode="scope_missing")
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "blocked")
        PROBE_MODULE.write_receipt(str(blocked_receipt), payload)
        self.assertEqual(json.loads(blocked_receipt.read_text(encoding="utf-8")), payload)
        self.assertEqual(stat.S_IMODE(blocked_receipt.stat().st_mode), 0o600)

    def test_receipt_path_cannot_overwrite_an_arbitrary_owned_file(self) -> None:
        outside = self.root / "outside.json"
        outside.write_text("keep me\n", encoding="utf-8")

        with self.assertRaises(PROBE_MODULE.ProbeBlocked) as blocked:
            PROBE_MODULE.write_receipt(str(outside), {"status": "blocked"})

        self.assertEqual(blocked.exception.code, "receipt_path_outside_trusted_root")
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep me\n")

    def test_403_is_classified_as_oauth_scope_missing(self) -> None:
        exit_code, payload = self.run_probe(mode="scope_missing")
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["reason_code"], "oauth_scope_missing")

    def test_wrong_mode_and_symlink_block_before_gog(self) -> None:
        self.env_file.chmod(0o644)
        exit_code, payload = self.run_probe()
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["reason_code"], "credential_env_wrong_mode")

        target = self.root / "real.env"
        self.env_file.rename(target)
        self.env_file.symlink_to(target)
        exit_code, payload = self.run_probe()
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["reason_code"], "credential_env_symlink")

    def test_stale_default_account_does_not_override_exact_route_account(self) -> None:
        self.env_file.write_text(
            f"GOG_ACCOUNT=someone@example.com\nGOG_KEYRING_PASSWORD={SECRET_MARKER}\n",
            encoding="utf-8",
        )
        self.env_file.chmod(0o600)
        exit_code, payload = self.run_probe()
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["account"], ACCOUNT)

    def test_wrong_property_is_blocked(self) -> None:
        exit_code, payload = self.run_probe(mode="wrong_property")
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["reason_code"], "property_not_accessible")

    def test_domain_and_url_prefix_property_forms_are_both_supported(self) -> None:
        exit_code, payload = self.run_probe(mode="domain_property")
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["property_uri"], f"sc-domain:{DOMAIN}")
        self.assertEqual(payload["property_type"], "domain")

        exit_code, payload = self.run_probe()
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["property_uri"], PROPERTY_URI)
        self.assertEqual(payload["property_type"], "url-prefix")

    def test_multiple_authorized_forms_for_requested_site_are_ambiguous(self) -> None:
        exit_code, payload = self.run_probe(mode="ambiguous")
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["reason_code"], "property_ambiguous")
        self.assertEqual(payload["authorized_matching_property_count"], 2)
        self.assertEqual(payload["matching_property_types"], ["domain", "url-prefix"])

    def test_account_inventory_can_discover_multiple_properties_without_selecting_one(self) -> None:
        exit_code, payload = self.run_probe(
            mode="ambiguous",
            property_domain=None,
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["property_selection_required"])
        self.assertEqual(payload["authorized_matching_property_count"], 2)
        self.assertEqual(payload["authorized_property_types"], ["domain", "url-prefix"])
        self.assertNotIn("property_uri", payload)
        self.assertNotIn("permission_level", payload)

    def test_unverified_matching_property_is_not_ready(self) -> None:
        exit_code, payload = self.run_probe(mode="unverified")
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["reason_code"], "property_permission_insufficient")

    def test_caller_cannot_switch_the_configured_account(self) -> None:
        with mock.patch.object(PROBE_MODULE.subprocess, "run") as run:
            exit_code, payload = self.run_probe(account="someone@example.com")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["reason_code"], "requested_account_not_configured")
        run.assert_not_called()

    def test_invalid_requested_domain_blocks_before_gog(self) -> None:
        with mock.patch.object(PROBE_MODULE.subprocess, "run") as run:
            exit_code, payload = self.run_probe(property_domain="https://example.com/")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["reason_code"], "requested_property_domain_invalid")
        run.assert_not_called()

    def test_registry_route_is_reusable_and_probe_domain_is_optional(self) -> None:
        routes_json = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(encoding="utf-8")
        )["routes"]
        routes_yaml = yaml.safe_load(
            (ROOT / "registry" / "integration_routes.yaml").read_text(encoding="utf-8")
        )["routes"]
        route_json = next(
            route
            for route in routes_json
            if route["route_id"] == PROBE_MODULE.CONFIGURED_ROUTE_ID
        )
        route_yaml = next(
            route
            for route in routes_yaml
            if route["route_id"] == PROBE_MODULE.CONFIGURED_ROUTE_ID
        )
        self.assertEqual(route_json, route_yaml)
        self.assertEqual(route_json["required_account"], ACCOUNT)
        self.assertEqual(
            route_json["browser_profile_primary"],
            {"profile_id": "openclaw", "transport": "managed_cdp"},
        )
        self.assertNotIn("property_domain", route_json)
        self.assertNotIn("property_uri", route_json)
        self.assertNotIn("property_type", route_json)
        self.assertNotIn("issue_label", route_json)

        probes = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )["probes"]
        probe = next(
            record
            for record in probes
            if record["probe_id"] == "google-search-console-personal-gog-probe"
        )
        self.assertNotIn("--property-domain", probe["command"])
        self.assertIn("callers may add --property-domain", probe["expected_signal"])

    def test_gog_os_error_is_sanitized(self) -> None:
        invalid_gog = self.root / "invalid-gog"
        invalid_gog.write_text("not an executable image\n", encoding="utf-8")
        invalid_gog.chmod(0o700)
        PROBE_MODULE.TRUSTED_GOG_BIN = invalid_gog

        exit_code, payload = PROBE_MODULE.run_probe(
            argparse.Namespace(
                account=ACCOUNT,
                property_domain=DOMAIN,
                timeout_seconds=5.0,
            )
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["reason_code"], "gog_execution_failed")
        self.assertNotIn(SECRET_MARKER, json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
