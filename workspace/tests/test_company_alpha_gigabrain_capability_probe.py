from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


from routing_test_support import fixture_root
ROOT = fixture_root()
SCRIPT = ROOT / "scripts" / 'company_alpha_gigabrain_capability_probe.py'
SPEC = importlib.util.spec_from_file_location(
    'company_alpha_gigabrain_capability_probe',
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
probe_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe_module)

TEST_IDENTITY = 'analytics@company-alpha.example.invalid'
TEST_DIGEST = hashlib.sha256(TEST_IDENTITY.encode("utf-8")).hexdigest()
EXPECTED_PROBE_FIELDS = {
    "schema",
    "route",
    "system",
    "credential_handle_id",
    "endpoint_origin_expected",
    "credential_contract_bound",
    "endpoint_matches_expected",
    "auth_ok",
    "account_identity_matches_registered",
    "provider_identity_sha256",
    "secrets_redacted",
    "external_mutation",
    "ok",
}


def route_loader() -> tuple[str, Path]:
    return TEST_DIGEST, probe_module.DEFAULT_ENV_FILE


def credential_loader(_path: Path) -> dict[str, str]:
    return {
        'COMPANY_ALPHA_GIGABRAIN_METABASE_URL': probe_module.EXPECTED_ORIGIN,
        'COMPANY_ALPHA_GIGABRAIN_METABASE_USER': TEST_IDENTITY,
        'COMPANY_ALPHA_GIGABRAIN_METABASE_PASSWORD': "private-password",
    }


class CompanyAlphaGigabrainCapabilityProbeTests(unittest.TestCase):
    def test_exact_identity_probe_compares_full_identities_without_emitting_them(
        self,
    ) -> None:
        calls: list[tuple[str, str, str | None]] = []

        def requester(origin, method, path, body, session_token):
            calls.append((method, path, session_token))
            self.assertEqual(origin, probe_module.EXPECTED_ORIGIN)
            if path == "/api/session":
                self.assertEqual(body["username"], TEST_IDENTITY)
                self.assertEqual(body["password"], "private-password")
                return True, {"id": "private-session-token"}
            self.assertEqual(path, "/api/user/current")
            self.assertIsNone(body)
            return True, {"email": TEST_IDENTITY.upper()}

        ok, result = probe_module.probe(
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertTrue(ok)
        self.assertEqual(set(result), EXPECTED_PROBE_FIELDS)
        self.assertNotIn("operation", result)
        self.assertTrue(result["account_identity_matches_registered"])
        self.assertEqual(result["provider_identity_sha256"], TEST_DIGEST)
        self.assertEqual(
            calls,
            [
                ("POST", "/api/session", None),
                ("GET", "/api/user/current", "private-session-token"),
            ],
        )
        public = json.dumps(result, sort_keys=True)
        for private_value in (
            TEST_IDENTITY,
            TEST_IDENTITY.upper(),
            "private-password",
            "private-session-token",
        ):
            self.assertNotIn(private_value, public)

    def test_provider_identity_mismatch_fails_closed_without_a_digest(self) -> None:
        def requester(_origin, _method, path, _body, _session_token):
            if path == "/api/session":
                return True, {"id": "private-session-token"}
            return True, {"email": 'other@company-alpha.example.invalid'}

        ok, result = probe_module.probe(
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertFalse(ok)
        self.assertEqual(result["error"], "provider_account_mismatch")
        self.assertFalse(result["account_identity_matches_registered"])
        self.assertNotIn("provider_identity_sha256", result)
        public = json.dumps(result, sort_keys=True)
        self.assertNotIn(TEST_IDENTITY, public)
        self.assertNotIn('other@company-alpha.example.invalid', public)

    def test_metadata_read_filters_and_bounds_visible_saved_questions(self) -> None:
        calls: list[tuple[str, str, object, str | None]] = []

        def requester(origin, method, path, body, session_token):
            calls.append((method, path, body, session_token))
            self.assertEqual(origin, probe_module.EXPECTED_ORIGIN)
            if path == "/api/session":
                return True, {"id": "private-session-token"}
            if path == "/api/user/current":
                return True, {"email": TEST_IDENTITY}
            self.assertEqual(path, "/api/card")
            return True, [
                {
                    "id": 104,
                    "name": "Daily Capture by Wallet",
                    "display": "table",
                    "archived": False,
                    "collection_id": 7,
                    "database_id": 2,
                },
                {
                    "id": 105,
                    "name": "Daily Volume by Pool",
                    "display": "line",
                    "archived": False,
                    "collection_id": 7,
                    "database_id": 2,
                },
                {
                    "id": 106,
                    "name": "Weekly Retention",
                    "display": "bar",
                    "archived": True,
                    "collection_id": None,
                    "database_id": 2,
                },
            ]

        ok, result = probe_module.metadata_read(
            search="daily",
            limit=1,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertTrue(ok)
        self.assertEqual(result["operation"], "metadata-read")
        self.assertEqual(result["visible_card_count"], 3)
        self.assertEqual(result["matched_card_count"], 2)
        self.assertEqual([card["id"] for card in result["cards"]], [104])
        self.assertTrue(result["truncated"])
        self.assertFalse(result["external_mutation"])
        self.assertEqual(
            [(method, path) for method, path, _body, _token in calls],
            [
                ("POST", "/api/session"),
                ("GET", "/api/user/current"),
                ("GET", "/api/card"),
            ],
        )
        public = json.dumps(result, sort_keys=True)
        for private_value in (
            TEST_IDENTITY,
            "private-password",
            "private-session-token",
        ):
            self.assertNotIn(private_value, public)

    def test_metadata_read_rejects_malformed_provider_rows(self) -> None:
        def requester(_origin, _method, path, _body, _session_token):
            if path == "/api/session":
                return True, {"id": "private-session-token"}
            if path == "/api/user/current":
                return True, {"email": TEST_IDENTITY}
            return True, [
                {
                    "id": True,
                    "name": "Malformed card",
                    "display": "table",
                    "archived": False,
                    "collection_id": 7,
                    "database_id": 2,
                }
            ]

        ok, result = probe_module.metadata_read(
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertFalse(ok)
        self.assertEqual(result["error"], "provider_metadata_invalid")
        self.assertNotIn("cards", result)

    def test_query_saved_card_returns_bounded_rows_from_visible_card(self) -> None:
        calls: list[tuple[str, str, object, str | None]] = []

        def requester(origin, method, path, body, session_token):
            calls.append((method, path, body, session_token))
            self.assertEqual(origin, probe_module.EXPECTED_ORIGIN)
            if path == "/api/session":
                return True, {"id": "private-session-token"}
            if path == "/api/user/current":
                return True, {"email": TEST_IDENTITY}
            if path == "/api/card":
                return True, [
                    {
                        "id": 104,
                        "name": "Daily Capture by Wallet",
                        "display": "table",
                        "archived": False,
                        "collection_id": 7,
                        "database_id": 2,
                    }
                ]
            self.assertEqual(path, "/api/card/104/query")
            self.assertEqual(body, {"parameters": []})
            return True, {
                "status": "completed",
                "row_count": 2,
                "cached": False,
                "data": {
                    "cols": [
                        {"display_name": "Day"},
                        {"name": "Capture"},
                    ],
                    "rows": [["2026-09-03", 0.42], ["2026-09-04", 0.45]],
                },
            }

        ok, result = probe_module.query_saved_card(
            card_id=104,
            max_rows=1,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertTrue(ok)
        self.assertEqual(result["operation"], "query-readonly")
        self.assertEqual(result["card"]["id"], 104)
        self.assertEqual(result["columns"], ["Day", "Capture"])
        self.assertEqual(result["rows"], [["2026-09-03", 0.42]])
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["returned_row_count"], 1)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["external_mutation"])
        self.assertEqual(
            [(method, path) for method, path, _body, _token in calls],
            [
                ("POST", "/api/session"),
                ("GET", "/api/user/current"),
                ("GET", "/api/card"),
                ("POST", "/api/card/104/query"),
            ],
        )

    def test_query_rejects_failed_status_and_wrong_row_width(self) -> None:
        def run(payload):
            def requester(_origin, _method, path, _body, _session_token):
                if path == "/api/session":
                    return True, {"id": "private-session-token"}
                if path == "/api/user/current":
                    return True, {"email": TEST_IDENTITY}
                if path == "/api/card":
                    return True, [
                        {
                            "id": 104,
                            "name": "Daily Capture by Wallet",
                            "display": "table",
                            "archived": False,
                            "collection_id": 7,
                            "database_id": 2,
                        }
                    ]
                return True, payload

            return probe_module.query_saved_card(
                card_id=104,
                route_loader=route_loader,
                credential_loader=credential_loader,
                requester=requester,
            )

        ok, result = run(
            {
                "status": "failed",
                "row_count": 1,
                "cached": False,
                "data": {
                    "cols": [{"display_name": "Value"}],
                    "rows": [[1]],
                },
            }
        )
        self.assertFalse(ok)
        self.assertEqual(result["error"], "provider_query_unsatisfied")

        ok, result = run(
            {
                "status": "completed",
                "row_count": 1,
                "cached": None,
                "data": {
                    "cols": [
                        {"display_name": "Day"},
                        {"display_name": "Value"},
                    ],
                    "rows": [[1]],
                },
            }
        )
        self.assertFalse(ok)
        self.assertEqual(result["error"], "provider_query_invalid")

    def test_provider_reported_remaining_rows_are_marked_truncated(self) -> None:
        def requester(_origin, _method, path, _body, _session_token):
            if path == "/api/session":
                return True, {"id": "private-session-token"}
            if path == "/api/user/current":
                return True, {"email": TEST_IDENTITY}
            if path == "/api/card":
                return True, [
                    {
                        "id": 104,
                        "name": "Daily Capture by Wallet",
                        "display": "table",
                        "archived": False,
                        "collection_id": 7,
                        "database_id": 2,
                    }
                ]
            return True, {
                "status": "completed",
                "row_count": 4,
                "cached": None,
                "data": {
                    "cols": [{"display_name": "Value"}],
                    "rows": [[1], [2]],
                },
            }

        ok, result = probe_module.query_saved_card(
            card_id=104,
            max_rows=3,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertTrue(ok)
        self.assertEqual(result["row_count"], 4)
        self.assertEqual(result["returned_row_count"], 2)
        self.assertTrue(result["truncated"])

    def test_query_missing_card_fails_before_provider_query(self) -> None:
        calls: list[str] = []

        def requester(_origin, _method, path, _body, _session_token):
            calls.append(path)
            if path == "/api/session":
                return True, {"id": "private-session-token"}
            if path == "/api/user/current":
                return True, {"email": TEST_IDENTITY}
            if path == "/api/card":
                return True, []
            self.fail("query endpoint must not run for an invisible card")

        ok, result = probe_module.query_saved_card(
            card_id=104,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertFalse(ok)
        self.assertEqual(result["error"], "provider_card_not_visible")
        self.assertNotIn("rows", result)
        self.assertEqual(
            calls,
            ["/api/session", "/api/user/current", "/api/card"],
        )

    def test_query_payload_with_private_credential_fails_value_free(self) -> None:
        def requester(_origin, _method, path, _body, _session_token):
            if path == "/api/session":
                return True, {"id": "private-session-token"}
            if path == "/api/user/current":
                return True, {"email": TEST_IDENTITY}
            if path == "/api/card":
                return True, [
                    {
                        "id": 104,
                        "name": "Credential-adjacent test",
                        "display": "table",
                        "archived": False,
                        "collection_id": 7,
                        "database_id": 2,
                    }
                ]
            return True, {
                "status": "completed",
                "row_count": 1,
                "cached": False,
                "data": {
                    "cols": [{"display_name": "Value"}],
                    "rows": [["private-password"]],
                },
            }

        ok, result = probe_module.query_saved_card(
            card_id=104,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertFalse(ok)
        self.assertEqual(result["error"], "credential_exposure_detected")
        public = json.dumps(result, sort_keys=True)
        self.assertNotIn("private-password", public)
        self.assertNotIn(TEST_IDENTITY, public)
        self.assertNotIn("private-session-token", public)

    def test_request_contract_rejects_unapproved_paths_and_missing_session(self) -> None:
        self.assertEqual(
            probe_module.request_json(
                probe_module.EXPECTED_ORIGIN,
                "POST",
                "/api/card/104",
                {"name": "mutation"},
                "token",
            ),
            (False, {"error": "request_contract_mismatch"}),
        )
        self.assertEqual(
            probe_module.request_json(
                probe_module.EXPECTED_ORIGIN,
                "POST",
                "/api/session",
                {"username": "user", "password": "pass", "extra": "value"},
                None,
            ),
            (False, {"error": "request_contract_mismatch"}),
        )
        self.assertEqual(
            probe_module.request_json(
                probe_module.EXPECTED_ORIGIN,
                "POST",
                "/api/card/104/query",
                {"parameters": [{"mutation": True}]},
                "token",
            ),
            (False, {"error": "request_contract_mismatch"}),
        )

    def test_function_level_bounds_reject_bool_without_provider_access(self) -> None:
        def requester(*_args):
            self.fail("invalid bounds must fail before provider access")

        ok, result = probe_module.metadata_read(limit=True, requester=requester)
        self.assertFalse(ok)
        self.assertEqual(result["error"], "metadata_limit_invalid")

        ok, result = probe_module.query_saved_card(
            card_id=True,
            requester=requester,
        )
        self.assertFalse(ok)
        self.assertEqual(result["error"], "card_id_invalid")

        ok, result = probe_module.query_saved_card(
            card_id=104,
            max_rows=True,
            requester=requester,
        )
        self.assertFalse(ok)
        self.assertEqual(result["error"], "query_row_limit_invalid")

    def test_redaction_traverses_nested_strings_and_casefolds_identities(self) -> None:
        session = probe_module.AuthenticatedSession(
            origin=probe_module.EXPECTED_ORIGIN,
            session_token="token-with-quote-\"",
            provider_identity_sha256=TEST_DIGEST,
            private_values=(
                TEST_IDENTITY,
                "private-password",
                "token-with-quote-\"",
            ),
        )
        with self.assertRaisesRegex(
            probe_module.ProbeFailure,
            "credential_exposure_detected",
        ):
            probe_module.ensure_credential_secrets_redacted(
                {"nested": [{"value": f"prefix {TEST_IDENTITY.upper()} suffix"}]},
                session,
            )
        with self.assertRaisesRegex(
            probe_module.ProbeFailure,
            "credential_exposure_detected",
        ):
            probe_module.ensure_credential_secrets_redacted(
                {"nested": [{"value": "token-with-quote-\""}]},
                session,
            )
        self.assertEqual(
            probe_module.request_json(
                probe_module.EXPECTED_ORIGIN,
                "GET",
                "/api/card",
                None,
                None,
            ),
            (False, {"error": "request_contract_mismatch"}),
        )

    def test_registered_route_owns_digest_and_physical_credential_handle(self) -> None:
        digest, credential_path = probe_module.load_registered_route_contract()

        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(credential_path, probe_module.DEFAULT_ENV_FILE)
        self.assertNotEqual(digest, hashlib.sha256(b"company-alpha-gigabrain").hexdigest())

    def test_registry_status_and_mechanics_expose_bounded_read_modes(self) -> None:
        registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        route = next(
            row
            for row in registry["routes"]
            if row["route_id"] == probe_module.ROUTE_ID
        )
        self.assertEqual(
            route["operations"],
            {
                "status-read": {"lane": "declared_native", "effect": "read"},
                "metadata-read": {"lane": "declared_native", "effect": "read"},
                "query-readonly": {"lane": "declared_native", "effect": "read"},
            },
        )
        self.assertIn("already-visible saved question", route["notes"])

        status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        readiness = next(
            row
            for row in status["capabilities"]
            if row["capability_id"] == route["readiness_status_id"]
        )
        self.assertEqual(
            readiness["evidence_operations"],
            ["status-read", "metadata-read", "query-readonly"],
        )
        self.assertIn("Synthetic", readiness["evidence"])
        self.assertNotIn(TEST_IDENTITY, json.dumps(readiness, sort_keys=True))

        helper_source = (ROOT / "scripts/company_alpha_gigabrain_capability_probe.py").read_text()
        self.assertIn('"--metadata"', helper_source)
        self.assertIn('"--query-card"', helper_source)

    def test_private_file_reader_rejects_symlink_and_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source.env"
            source.write_text("private", encoding="utf-8")
            source.chmod(0o600)
            self.assertEqual(
                probe_module.read_owner_private_text(
                    source,
                    expected_path=source,
                    expected_uid=os.getuid(),
                ),
                "private",
            )

            symlink = root / "symlink.env"
            symlink.symlink_to(source)
            with self.assertRaisesRegex(
                probe_module.ProbeFailure,
                "credential_open_failed",
            ):
                probe_module.read_owner_private_text(
                    symlink,
                    expected_path=symlink,
                    expected_uid=os.getuid(),
                )

            hardlink = root / "hardlink.env"
            os.link(source, hardlink)
            with self.assertRaisesRegex(
                probe_module.ProbeFailure,
                "credential_link_count_mismatch",
            ):
                probe_module.read_owner_private_text(
                    hardlink,
                    expected_path=hardlink,
                    expected_uid=os.getuid(),
                )


if __name__ == "__main__":
    unittest.main()
