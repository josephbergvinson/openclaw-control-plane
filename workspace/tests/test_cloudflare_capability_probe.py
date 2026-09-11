from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import io
import json
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from routing_test_support import fixture_root
fixture_root()
from scripts import cloudflare_capability_probe as cloudflare


from routing_test_support import fixture_root
ROOT = fixture_root()
SCRIPT = ROOT / "scripts" / "cloudflare_capability_probe.py"


def provider_payloads() -> dict[str, dict[str, object]]:
    return {
        "/user/tokens/verify": {
            "success": True,
            "result": {"status": "active"},
        },
        "/accounts": {
            "success": True,
            "result": [
                {
                    "id": cloudflare.EXPECTED_ACCOUNT_ID,
                    "name": cloudflare.EXPECTED_ACCOUNT_NAME,
                }
            ],
        },
        "/zones": {
            "success": True,
            "result": [
                {
                    "id": cloudflare.EXPECTED_ZONE_ID,
                    "name": cloudflare.EXPECTED_ZONE_NAME,
                    "status": "active",
                    "type": "full",
                    "account": {"id": cloudflare.EXPECTED_ACCOUNT_ID},
                }
            ],
        },
        (
            f"/accounts/{cloudflare.EXPECTED_ACCOUNT_ID}/pages/projects/"
            f"{cloudflare.EXPECTED_PROJECT_NAME}"
        ): {
            "success": True,
            "result": {
                "id": cloudflare.EXPECTED_PROJECT_ID,
                "name": cloudflare.EXPECTED_PROJECT_NAME,
                "subdomain": cloudflare.EXPECTED_PROJECT_SUBDOMAIN,
                "production_branch": cloudflare.EXPECTED_PRODUCTION_BRANCH,
                "source": {
                    "type": cloudflare.EXPECTED_PROJECT_SOURCE_TYPE,
                    "config": {
                        "owner": cloudflare.EXPECTED_PROJECT_SOURCE_OWNER,
                        "repo_name": cloudflare.EXPECTED_PROJECT_SOURCE_REPOSITORY,
                        "production_branch": cloudflare.EXPECTED_PRODUCTION_BRANCH,
                        "deployments_enabled": True,
                        "preview_deployment_setting": "all",
                    },
                },
            },
        },
    }


class CloudflareCapabilityTests(unittest.TestCase):
    def read_state(self, payloads: dict[str, dict[str, object]] | None = None):
        payloads = payloads or provider_payloads()

        def fake_get(_token, path, params=None):
            self.assertIn(path, payloads)
            if path == "/accounts":
                self.assertEqual(params, {"per_page": 50})
            if path == "/zones":
                self.assertEqual(
                    params,
                    {"name": cloudflare.EXPECTED_ZONE_NAME, "per_page": 10},
                )
            return True, payloads[path]

        with mock.patch.object(cloudflare, "cf_get", side_effect=fake_get):
            return cloudflare.read_cloudflare_state("private-token")

    def test_read_probe_binds_exact_account_zone_and_project(self):
        state = self.read_state()

        self.assertEqual(
            state["identity"],
            {
                "account_id": cloudflare.EXPECTED_ACCOUNT_ID,
                "account_name": cloudflare.EXPECTED_ACCOUNT_NAME,
            },
        )
        self.assertEqual(state["zone"]["id"], cloudflare.EXPECTED_ZONE_ID)
        self.assertEqual(
            state["project"],
            {
                "id": cloudflare.EXPECTED_PROJECT_ID,
                "name": "personal-site",
                "subdomain": _operator_binding('services.cloudflare.project_subdomain'),
                "production_branch": "main",
                "source": {
                    "type": "github",
                    "owner": _operator_binding('identifiers.github_username'),
                    "repository": "personal-site",
                },
            },
        )

    def test_read_probe_fails_closed_on_each_identity_boundary(self):
        cases = (
            ("/accounts", "result", 0, "id", "wrong-account"),
            ("/zones", "result", 0, "id", "wrong-zone"),
            (
                f"/accounts/{cloudflare.EXPECTED_ACCOUNT_ID}/pages/projects/"
                f"{cloudflare.EXPECTED_PROJECT_NAME}",
                "result",
                None,
                "production_branch",
                "not-main",
            ),
        )
        for path, result_key, index, field, value in cases:
            with self.subTest(path=path, field=field):
                payloads = provider_payloads()
                target = payloads[path][result_key]
                if index is not None:
                    target = target[index]
                target[field] = value
                with self.assertRaises(cloudflare.CapabilityBlocked):
                    self.read_state(payloads)

    def test_public_payload_is_strict_and_contains_no_credential(self):
        payload = cloudflare.public_payload(
            status="ready",
            reason_code=None,
            state=self.read_state(),
        )

        self.assertEqual(
            set(payload),
            {
                "schema",
                "route_id",
                "status",
                "reason_code",
                "identity",
                "zone",
                "project",
            },
        )
        self.assertNotIn("private-token", json.dumps(payload))

    def test_http_error_body_is_not_returned(self):
        error = urllib.error.HTTPError(
            "https://api.cloudflare.com/client/v4/accounts",
            403,
            "forbidden",
            {},
            io.BytesIO(b"private-token private provider detail"),
        )
        with mock.patch.object(cloudflare.urllib.request, "urlopen", side_effect=error):
            ok, payload = cloudflare.cf_get("private-token", "/accounts")

        self.assertFalse(ok)
        self.assertEqual(payload, {"http_status": 403})
        self.assertNotIn("private-token", json.dumps(payload))

    def test_helper_exposes_no_preview_or_production_mutation_cli(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--operation", "pages-preview-deploy"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("unrecognized arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
