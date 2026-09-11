from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import copy
import hashlib
import http.server
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from routing_test_support import fixture_root
ROOT = fixture_root()
SCRIPT = ROOT / "scripts" / 'jira_company_alpha_capability_probe.py'
SPEC = importlib.util.spec_from_file_location('jira_company_alpha_capability_probe', SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe_module)


def route_loader_for(account_id: str):
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    return lambda: ({}, digest, probe_module.DEFAULT_ENV_FILE)


def credential_loader(_path: Path) -> dict[str, str]:
    return {
        "ATLASSIAN_SITE_URL": probe_module.EXPECTED_SITE,
        "ATLASSIAN_EMAIL": "private@example.invalid",
        "ATLASSIAN_API_TOKEN": "private-token",
    }


def metadata_payload(*, include_labels: bool = True, include_attachment: bool = False):
    names = {
        "summary": "Summary",
        "description": "Description",
        "reporter": "Reporter",
        "platform": "Platform",
        "tester": "Tester Name",
    }
    if include_labels:
        names["labels"] = "Labels"
    if include_attachment:
        names["attachment"] = "Attachment"
    return {
        "projects": [
            {
                "id": "10000",
                "key": probe_module.EXPECTED_PROJECT_KEY,
                "issuetypes": [
                    {
                        "id": "10001",
                        "name": probe_module.EXPECTED_ISSUE_TYPE,
                        "fields": {
                            field_id: {"name": name}
                            for field_id, name in names.items()
                        },
                    }
                ],
            }
        ]
    }


class JiraCredentialCustodyTests(unittest.TestCase):
    def test_default_path_is_physical_and_cli_rejects_overrides(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": "/tmp/ambient-home"}):
            self.assertEqual(
                probe_module.DEFAULT_ENV_FILE,
                Path(
                    _operator_binding('paths.routing_company_alpha_jira_env')
                ),
            )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            probe_module.parse_args(["--env-file", "/tmp/wrong.env"])

    def test_descriptor_reader_accepts_only_exact_owner_private_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            valid = root / "valid.env"
            valid.write_text("secret", encoding="utf-8")
            valid.chmod(0o600)
            self.assertEqual(
                probe_module.read_owner_private_text(
                    valid,
                    expected_path=valid,
                    expected_uid=os.getuid(),
                ),
                "secret",
            )

            cases: list[tuple[str, Path, dict[str, Any], str]] = []
            wrong_path = root / "wrong-path.env"
            wrong_path.write_text("secret", encoding="utf-8")
            wrong_path.chmod(0o600)
            cases.append(
                (
                    "path",
                    wrong_path,
                    {"expected_path": valid, "expected_uid": os.getuid()},
                    "credential_path_mismatch",
                )
            )

            symlink = root / "symlink.env"
            symlink.symlink_to(valid)
            cases.append(
                (
                    "symlink",
                    symlink,
                    {"expected_path": symlink, "expected_uid": os.getuid()},
                    "credential_open_failed",
                )
            )

            hardlink_source = root / "hardlink-source.env"
            hardlink_source.write_text("secret", encoding="utf-8")
            hardlink_source.chmod(0o600)
            hardlink = root / "hardlink.env"
            os.link(hardlink_source, hardlink)
            cases.append(
                (
                    "hardlink",
                    hardlink,
                    {"expected_path": hardlink, "expected_uid": os.getuid()},
                    "credential_link_count_mismatch",
                )
            )

            wrong_mode = root / "wrong-mode.env"
            wrong_mode.write_text("secret", encoding="utf-8")
            wrong_mode.chmod(0o640)
            cases.append(
                (
                    "mode",
                    wrong_mode,
                    {"expected_path": wrong_mode, "expected_uid": os.getuid()},
                    "credential_mode_mismatch",
                )
            )

            directory_path = root / "directory.env"
            directory_path.mkdir(mode=0o700)
            cases.append(
                (
                    "regular",
                    directory_path,
                    {"expected_path": directory_path, "expected_uid": os.getuid()},
                    "credential_not_regular",
                )
            )

            oversized = root / "oversized.env"
            oversized.write_text("x" * 9, encoding="utf-8")
            oversized.chmod(0o600)
            cases.append(
                (
                    "size",
                    oversized,
                    {
                        "expected_path": oversized,
                        "expected_uid": os.getuid(),
                        "max_bytes": 8,
                    },
                    "credential_file_too_large",
                )
            )

            cases.append(
                (
                    "owner",
                    valid,
                    {"expected_path": valid, "expected_uid": os.getuid() + 1},
                    "credential_owner_mismatch",
                )
            )

            for name, path, kwargs, expected_code in cases:
                with self.subTest(name=name), self.assertRaises(
                    probe_module.ProbeFailure
                ) as raised:
                    probe_module.read_owner_private_text(path, **kwargs)
                self.assertEqual(raised.exception.code, expected_code)

    def test_descriptor_reader_rejects_symlink_in_parent_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            physical = root / "physical"
            physical.mkdir(mode=0o700)
            credential = physical / "credential.env"
            credential.write_text("secret", encoding="utf-8")
            credential.chmod(0o600)
            alias = root / "alias"
            alias.symlink_to(physical, target_is_directory=True)
            aliased_credential = alias / credential.name

            with self.assertRaises(probe_module.ProbeFailure) as raised:
                probe_module.read_owner_private_text(
                    aliased_credential,
                    expected_path=aliased_credential,
                    expected_uid=os.getuid(),
                )

        self.assertEqual(raised.exception.code, "credential_open_failed")

    def test_env_parser_requires_the_one_registered_key_set(self) -> None:
        valid = "\n".join(
            (
                'ATLASSIAN_SITE_URL=https://jira.example.invalid',
                "ATLASSIAN_EMAIL='private@example.invalid'",
                'ATLASSIAN_API_TOKEN="private-token"',
            )
        )
        parsed = probe_module.parse_env_text(valid)
        self.assertEqual(set(parsed), probe_module.REQUIRED_ENV_KEYS)
        for invalid in (
            valid + "\nEXTRA=value",
            valid + "\nATLASSIAN_EMAIL=duplicate",
            valid.replace("ATLASSIAN_API_TOKEN=", "MALFORMED "),
        ):
            with self.subTest(invalid=invalid.splitlines()[-1]), self.assertRaises(
                probe_module.ProbeFailure
            ):
                probe_module.parse_env_text(invalid)

    def test_registry_resolves_the_one_typed_jira_owner(self) -> None:
        route, digest, path = probe_module.load_registered_route_contract()
        self.assertEqual(route["route_id"], probe_module.ROUTE_ID)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(path, probe_module.DEFAULT_ENV_FILE)
        self.assertNotIn("credential_ref_name", route)

    def test_registry_rejects_missing_duplicate_and_rebound_jira_handles(self) -> None:
        registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )

        missing = copy.deepcopy(registry)
        handle = next(
            row
            for row in missing["credential_handles"]
            if row["handle_id"] == probe_module.CREDENTIAL_HANDLE_ID
        )
        handle["consumers"] = [
            consumer
            for consumer in handle["consumers"]
            if consumer
            != {"kind": "integration-route", "consumer_id": probe_module.ROUTE_ID}
        ]

        duplicate = copy.deepcopy(registry)
        duplicate_handle = copy.deepcopy(
            next(
                row
                for row in duplicate["credential_handles"]
                if row["handle_id"] == probe_module.CREDENTIAL_HANDLE_ID
            )
        )
        duplicate_handle["handle_id"] = 'jira.company_alpha.api-copy'
        duplicate["credential_handles"].append(duplicate_handle)

        rebound = copy.deepcopy(registry)
        handle = next(
            row
            for row in rebound["credential_handles"]
            if row["handle_id"] == probe_module.CREDENTIAL_HANDLE_ID
        )
        handle["owner"]["ref"] = (
            '/operator/installation/secrets/other.env'
        )

        for name, payload, expected in (
            ("missing", missing, "registry_credential_handle_not_unique"),
            ("duplicate", duplicate, "registry_credential_handle_not_unique"),
            ("rebound", rebound, "registry_credential_binding_mismatch"),
        ):
            with self.subTest(name=name), mock.patch.object(
                probe_module,
                "load_json_strict",
                return_value=payload,
            ), self.assertRaises(probe_module.ProbeFailure) as raised:
                probe_module.load_registered_route_contract()
            self.assertEqual(raised.exception.code, expected)


class JiraExactAccountProbeTests(unittest.TestCase):
    def _run(
        self,
        account_id: str,
        *,
        labels: bool = True,
        attachment: bool = False,
        metadata: dict[str, Any] | None = None,
        project_id: str = "10000",
    ):
        calls: list[str] = []

        def requester(_site: str, _email: str, _token: str, path: str):
            calls.append(path)
            if path == "/rest/api/3/myself":
                return True, {"accountId": account_id, "accountType": "atlassian"}
            if path == f"/rest/api/3/project/{probe_module.EXPECTED_PROJECT_KEY}":
                return True, {
                    "id": project_id,
                    "key": probe_module.EXPECTED_PROJECT_KEY,
                    "name": 'CompanyAlpha',
                }
            if path.startswith("/rest/api/3/issue/createmeta?"):
                return True, metadata or metadata_payload(
                    include_labels=labels,
                    include_attachment=attachment,
                )
            raise AssertionError(f"unexpected read path: {path}")

        ok, result = probe_module.probe(
            route_loader=route_loader_for(account_id),
            credential_loader=credential_loader,
            requester=requester,
        )
        return ok, result, calls

    def test_exact_account_and_labels_metadata_pass_using_read_endpoints_only(self) -> None:
        ok, result, calls = self._run("registered-provider-account")
        self.assertTrue(ok)
        self.assertTrue(result["account_identity_matches_registered"])
        self.assertEqual(result["missing_required_field_names"], [])
        self.assertEqual(
            calls,
            [
                "/rest/api/3/myself",
                '/rest/api/3/project/ALPHA',
                '/rest/api/3/issue/createmeta?projectKeys=ALPHA&expand=projects.issuetypes.fields',
            ],
        )
        self.assertNotIn("Attachment", probe_module.REQUIRED_FIELD_NAMES)
        self.assertIn("Labels", probe_module.REQUIRED_FIELD_NAMES)

    def test_attachment_does_not_substitute_for_labels(self) -> None:
        ok, result, _calls = self._run(
            "registered-provider-account", labels=False, attachment=True
        )
        self.assertFalse(ok)
        self.assertEqual(result["missing_required_field_names"], ["Labels"])

    def test_duplicate_required_field_name_fails_closed(self) -> None:
        metadata = metadata_payload()
        fields = metadata["projects"][0]["issuetypes"][0]["fields"]
        fields["labels-duplicate"] = {"name": "Labels"}

        ok, result, _calls = self._run(
            "registered-provider-account",
            metadata=metadata,
        )

        self.assertFalse(ok)
        self.assertEqual(result["error"], "qa_feedback_required_field_not_unique")
        self.assertEqual(result["duplicate_required_field_names"], ["Labels"])
        self.assertNotIn("field_ids", result)

    def test_provider_ids_must_be_canonical_nonblank_strings(self) -> None:
        ok, result, calls = self._run("   ")
        self.assertFalse(ok)
        self.assertEqual(result["error"], "provider_account_id_missing")
        self.assertEqual(calls, ["/rest/api/3/myself"])

        ok, result, _calls = self._run(
            "registered-provider-account",
            project_id="   ",
        )
        self.assertFalse(ok)
        self.assertEqual(result["error"], "project_identity_mismatch")

        whitespace_issue_type = metadata_payload()
        whitespace_issue_type["projects"][0]["issuetypes"][0]["id"] = "   "
        ok, result, _calls = self._run(
            "registered-provider-account",
            metadata=whitespace_issue_type,
        )
        self.assertFalse(ok)
        self.assertEqual(result["error"], "qa_feedback_issue_type_id_invalid")

        whitespace_field = metadata_payload()
        fields = whitespace_field["projects"][0]["issuetypes"][0]["fields"]
        fields["   "] = fields.pop("summary")
        ok, result, _calls = self._run(
            "registered-provider-account",
            metadata=whitespace_field,
        )
        self.assertFalse(ok)
        self.assertEqual(result["error"], "qa_feedback_required_field_id_invalid")
        self.assertEqual(result["invalid_required_field_id_names"], ["Summary"])

    def test_provider_ids_reject_nul_control_format_and_noncanonical_grammar(self) -> None:
        for value in (
            "account\x00id",
            "account\x1fid",
            "account\u200did",
            "account id",
            "account/id",
        ):
            with self.subTest(value=repr(value)):
                self.assertFalse(probe_module.canonical_provider_id(value))
        for value in (
            "712020:abcdef-1234",
            "10000",
            "customfield_10042",
            "account.name@example.com",
        ):
            with self.subTest(valid=value):
                self.assertTrue(probe_module.canonical_provider_id(value))

        ok, result, calls = self._run("account\x00id")
        self.assertFalse(ok)
        self.assertEqual(result["error"], "provider_account_id_missing")
        self.assertEqual(calls, ["/rest/api/3/myself"])

        format_field = metadata_payload()
        fields = format_field["projects"][0]["issuetypes"][0]["fields"]
        fields["customfield_10\u200d042"] = fields.pop("summary")
        ok, result, _calls = self._run(
            "registered-provider-account",
            metadata=format_field,
        )
        self.assertFalse(ok)
        self.assertEqual(result["error"], "qa_feedback_required_field_id_invalid")

    def test_createmeta_project_id_must_match_exact_project_read(self) -> None:
        metadata = metadata_payload()
        metadata["projects"][0]["id"] = "different-project-id"

        ok, result, _calls = self._run(
            "registered-provider-account",
            metadata=metadata,
        )

        self.assertFalse(ok)
        self.assertEqual(result["error"], "createmeta_project_identity_mismatch")
        self.assertNotIn("field_ids", result)

    def test_wrong_myself_identity_fails_before_project_or_metadata_reads(self) -> None:
        calls: list[str] = []

        def requester(_site: str, _email: str, _token: str, path: str):
            calls.append(path)
            return True, {"accountId": "wrong-provider-account"}

        ok, result = probe_module.probe(
            route_loader=route_loader_for("registered-provider-account"),
            credential_loader=credential_loader,
            requester=requester,
        )
        self.assertFalse(ok)
        self.assertEqual(result["error"], "provider_account_mismatch")
        self.assertEqual(calls, ["/rest/api/3/myself"])
        rendered = json.dumps(result, sort_keys=True)
        for forbidden in (
            "wrong-provider-account",
            "registered-provider-account",
            "private@example.invalid",
            "private-token",
            hashlib.sha256(b"registered-provider-account").hexdigest(),
        ):
            self.assertNotIn(forbidden, rendered)

    def test_provider_error_payload_is_redacted(self) -> None:
        ok, result = probe_module.probe(
            route_loader=route_loader_for("registered-provider-account"),
            credential_loader=credential_loader,
            requester=lambda *_args: (
                False,
                {"error": "http_error", "status": 401, "raw": "private-token"},
            ),
        )
        self.assertFalse(ok)
        self.assertEqual(result["auth_error"], {"error": "http_error", "status": 401})
        self.assertNotIn("private-token", json.dumps(result))

    def test_network_helper_is_get_only_and_has_no_request_body(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit: int) -> bytes:
                return b'{"accountId":"provider-account"}'

        with mock.patch.object(
            probe_module, "open_url_no_redirect", return_value=Response()
        ) as open_url:
            ok, payload = probe_module.request_json(
                probe_module.EXPECTED_SITE,
                "private@example.invalid",
                "private-token",
                "/rest/api/3/myself",
            )
        self.assertTrue(ok)
        self.assertEqual(payload, {"accountId": "provider-account"})
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(
            request.full_url,
            'https://jira.example.invalid/rest/api/3/myself',
        )

    def test_network_helper_rejects_duplicate_json_keys_at_any_depth(self) -> None:
        class Response:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit: int) -> bytes:
                return self.body

        bodies = (
            b'{"accountId":"wrong","accountId":"registered"}',
            b'{"accountId":"registered","nested":{"id":"first","id":"second"}}',
        )
        for body in bodies:
            with self.subTest(body=body), mock.patch.object(
                probe_module, "open_url_no_redirect", return_value=Response(body)
            ):
                ok, payload = probe_module.request_json(
                    probe_module.EXPECTED_SITE,
                    "private@example.invalid",
                    "private-token",
                    "/rest/api/3/myself",
                )

            self.assertFalse(ok)
            self.assertEqual(payload, {"error": "invalid_json"})

    def test_network_helper_rejects_non_standard_json_numeric_constants(self) -> None:
        class Response:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit: int) -> bytes:
                return self.body

        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            body = b'{"accountId":"registered","extra":' + constant + b"}"
            with self.subTest(constant=constant), mock.patch.object(
                probe_module, "open_url_no_redirect", return_value=Response(body)
            ):
                ok, payload = probe_module.request_json(
                    probe_module.EXPECTED_SITE,
                    "private@example.invalid",
                    "private-token",
                    "/rest/api/3/myself",
                )

            self.assertFalse(ok)
            self.assertEqual(payload, {"error": "invalid_json"})

    def test_redirects_never_receive_authorization(self) -> None:
        sink_authorization: list[str | None] = []

        class SinkHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                sink_authorization.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"accountId":"redirected-provider-account"}')

            def log_message(self, _format: str, *_args: object) -> None:
                return

        sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
        sink_url = f"http://127.0.0.1:{sink.server_address[1]}"

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(int(self.path.rsplit("/", 1)[-1]))
                self.send_header("Location", sink_url + "/redirected")
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        source = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        source_url = f"http://127.0.0.1:{source.server_address[1]}"
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (sink, source)
        ]
        for thread in threads:
            thread.start()
        try:
            with mock.patch.object(probe_module, "EXPECTED_SITE", source_url):
                for status in (301, 302, 303, 307, 308):
                    with self.subTest(status=status):
                        ok, payload = probe_module.request_json(
                            source_url,
                            "private@example.invalid",
                            "private-token",
                            f"/redirect/{status}",
                        )
                        self.assertFalse(ok)
                        self.assertEqual(
                            payload,
                            {"error": "http_error", "status": status},
                        )
                        self.assertEqual(sink_authorization, [])
        finally:
            for server in (source, sink):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)

    def test_authorization_is_confined_to_the_exact_registered_site(self) -> None:
        with mock.patch.object(probe_module, "open_url_no_redirect") as open_url:
            ok, payload = probe_module.request_json(
                "https://other.example.invalid",
                "private@example.invalid",
                "private-token",
                "/rest/api/3/myself",
            )

        self.assertFalse(ok)
        self.assertEqual(payload, {"error": "site_mismatch"})
        open_url.assert_not_called()


class OperationRegistryReductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry_json = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        cls.registry_yaml = yaml.safe_load(
            (ROOT / "registry" / "integration_routes.yaml").read_text(
                encoding="utf-8"
            )
        )
        cls.routes = {
            route["route_id"]: route for route in cls.registry_json["routes"]
        }
        cls.credential_handles = {
            row["handle_id"]: row
            for row in cls.registry_json["credential_handles"]
        }

    def test_unowned_mutation_executor_and_parallel_journal_surface_are_absent(self) -> None:
        for relative in (
            "scripts/capability_effect_contract.py",
            "scripts/execute_capability_effect.py",
            "scripts/google_calendar_effect_adapter.py",
            "scripts/jira_issue_effect_adapter.py",
        ):
            self.assertFalse((ROOT / relative).exists(), relative)
        self.assertNotIn(
            "native_effect_executor", self.registry_json["routing_defaults"]
        )
        for route in self.registry_json["routes"]:
            self.assertNotIn("effect_adapter", route, route["route_id"])

    def test_operation_truth_has_no_silent_native_or_account_fallback(self) -> None:
        yaml_routes = {
            route["route_id"]: route for route in self.registry_yaml["routes"]
        }
        self.assertEqual(self.routes, yaml_routes)
        self.assertEqual(
            self.registry_json["routing_defaults"],
            self.registry_yaml["routing_defaults"],
        )
        jira = self.routes['jira-company-alpha']
        self.assertEqual(
            self.credential_handles['jira.company_alpha.api']["owner"],
            {
                "kind": "runtime-secret-file",
                "ref": str(probe_module.DEFAULT_ENV_FILE),
                "keys": [
                    "ATLASSIAN_SITE_URL",
                    "ATLASSIAN_EMAIL",
                    "ATLASSIAN_API_TOKEN",
                ],
            },
        )
        self.assertNotIn("credential_ref_name", jira)
        self.assertNotIn("$HOME", jira["readiness_status_id"])
        self.assertEqual(
            jira["operations"]["issue-create"],
            {
                "lane": "authenticated_ui",
                "effect": "mutation",
                "readiness_status_id": 'Mutation readiness — `jira-company-alpha`',
            },
        )
        for retired in (
            "native_supported_operations",
            "authenticated_ui_required_operations",
            "authenticated_ui_mutation_operations",
            "operation_effects",
            "identity_assurance",
        ):
            self.assertNotIn(retired, jira)
        self.assertFalse(
            jira["provider_adapter"]["native_evidence"][
                "completion_receipt_supported"
            ]
        )
        self.assertRegex(
            jira["provider_adapter"]["native_evidence"][
                "provider_account_id_sha256"
            ],
            r"^[0-9a-f]{64}$",
        )

        for route_id in (
            "google-calendar-personal-gog",
            'google-calendar-company-alpha-coordinator-gog',
        ):
            calendar = self.routes[route_id]
            self.assertNotIn("event-update", calendar["operations"])

        forms = self.routes['google-forms-company-alpha-coordinator-gog']
        self.assertEqual(
            forms["operations"]["form-response-submit"]["lane"],
            "authenticated_ui",
        )
        self.assertEqual(
            forms["operations"]["form-response-submit"]["effect"],
            "mutation",
        )


if __name__ == "__main__":
    unittest.main()
