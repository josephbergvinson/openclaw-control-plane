from __future__ import annotations

import contextlib
import io
import json
import sys
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import google_capability_probe as probe
from scripts import resolve_capability as resolver


ACCOUNT = "personal_google@example.invalid"
OTHER_ACCOUNT = "company_alpha_operator_google@example.invalid"


def identity(account: str = ACCOUNT, **overrides: object) -> dict:
    return {
        "email": account,
        "valid": True,
        "auth": "oauth",
        "client": "default",
        "services": ["gmail", "drive"],
        "scopes": ["email"],
        **overrides,
    }


@contextlib.contextmanager
def configured_google_fixture():
    """Use the routing suite's synthetic registry without changing other tests."""
    from routing_test_support import fixture_root
    from scripts import routing_operator_bindings as bindings
    from scripts.operator_contract import OperatorContract

    with patch.dict(os.environ), patch.object(bindings, "_CONTRACT"), \
            patch.object(bindings, "_CONFIG_SELECTION"):
        root = fixture_root()
    config = root / "operator.json"
    contract = OperatorContract(json.loads(config.read_text()), workspace=root)
    binary = Path(contract.require_path("paths.gog_binary"))
    with patch.object(bindings, "_CONTRACT", contract), \
            patch.object(bindings, "_CONFIG_SELECTION", str(config)), \
            patch.object(resolver, "ROOT", root), \
            patch.object(resolver, "TRUSTED_GOG_BIN", binary), \
            patch.object(resolver, "GOG_AUTH_LIST_PROBE_ARGV",
                         (str(binary), "auth", "list", "--check", "--json")):
        yield root


class GoogleCapabilityProbeTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(configured_google_fixture())

    def invoke(self, accounts: list[dict], *, account: str = ACCOUNT,
               results: dict[str, resolver.ProbeCommandResult] | None = None):
        calls: list[tuple[str, ...]] = []
        results = results or {}

        def transport(argv, timeout):
            calls.append(argv)
            if argv == resolver.GOG_AUTH_LIST_PROBE_ARGV:
                operation = "identity"
                payload = {"accounts": accounts}
            elif "gmail" in argv:
                operation = "gmail"
                payload = {"labels": [{"id": "INBOX"}]}
            elif "drive" in argv:
                operation = "drive"
                payload = {"files": []}
            else:
                self.fail(f"Unexpected provider command: {argv}")
            return results.get(operation, resolver.ProbeCommandResult(0, json.dumps(payload)))

        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", ["google_capability_probe.py", "--account", account]),
            patch.object(resolver, "subprocess_probe_transport", side_effect=transport),
            patch.object(probe.Path, "exists", return_value=True),
            contextlib.redirect_stdout(stdout),
        ):
            code = probe.main()
        return code, json.loads(stdout.getvalue()), calls

    def test_oauth_store_and_reads_do_not_prove_send_or_other_scopes(self):
        code, result, _ = self.invoke([identity()])
        self.assertEqual(code, 0)
        self.assertNotEqual(result["capabilities"]["gmail_send"]["status"], "ready")
        for key in probe.REQUIRED_SCOPES:
            self.assertNotEqual(result["capabilities"][f"scope_{key}"]["status"], "ready")

    def test_read_commands_are_bound_to_the_selected_account(self):
        code, result, calls = self.invoke([identity()])
        self.assertEqual(code, 0)
        self.assertEqual(result.get("account"), ACCOUNT)
        reads = [argv for argv in calls if "gmail" in argv or "drive" in argv]
        self.assertEqual(len(reads), 2)
        for argv in reads:
            self.assertIn("--account", argv)
            self.assertEqual(argv[argv.index("--account") + 1], ACCOUNT)
            self.assertIn("--gmail-no-send", argv)

    def test_other_default_account_cannot_satisfy_selected_account_identity(self):
        code, result, calls = self.invoke([identity(OTHER_ACCOUNT)])
        self.assertEqual(code, 2)
        self.assertNotEqual(result["capabilities"]["google_auth_lane"]["status"], "ready")
        self.assertEqual(calls, [resolver.GOG_AUTH_LIST_PROBE_ARGV])

    def test_observed_scopes_are_facts_and_do_not_verify_send_or_writes(self):
        code, result, _ = self.invoke([identity(scopes=list(probe.REQUIRED_SCOPES.values()))])
        self.assertEqual(code, 0)
        self.assertEqual(result["capabilities"]["gmail_send"]["status"], "unverified")
        for key in probe.REQUIRED_SCOPES:
            self.assertEqual(result["capabilities"][f"scope_{key}"]["status"], "observed")
        self.assertEqual(result["evidence_operations"], ["oauth-identity-read", "gmail-read", "drive-read"])
        self.assertTrue(result["diagnostic_only"])
        self.assertFalse(result["execution_prerequisite"])
        self.assertEqual(result["files"]["google_token_json"]["role"], "inventory_only")
        self.assertEqual(result["capabilities"]["google_oauth_files_legacy"]["status"], "unverified")

    def test_identity_missing_expired_duplicate_or_unrelated_is_not_account_readiness(self):
        cases = [[], [identity(valid=False)], [identity(), identity()], [identity(OTHER_ACCOUNT)]]
        for accounts in cases:
            with self.subTest(accounts=accounts):
                code, result, calls = self.invoke(accounts)
                self.assertEqual(code, 2)
                self.assertEqual(result["account"], ACCOUNT)
                self.assertEqual(result["capabilities"]["gmail_read"]["evidence_operations"], [])
                self.assertEqual(calls, [resolver.GOG_AUTH_LIST_PROBE_ARGV])
                self.assertFalse(result["recovery"]["auth_changes_attempted"])

    def test_nonmatching_identity_metadata_does_not_break_selected_entry(self):
        code, result, _ = self.invoke([{"valid": False}, identity()])
        self.assertEqual(code, 0)
        self.assertEqual(result["account"], ACCOUNT)

    def test_provider_identity_case_matches_the_exact_registered_route(self):
        code, result, calls = self.invoke([identity(ACCOUNT.upper())])
        self.assertEqual(code, 0)
        self.assertEqual(result["account"], ACCOUNT)
        for argv in calls[1:]:
            self.assertEqual(argv[argv.index("--account") + 1], ACCOUNT)

    def test_unregistered_account_never_reaches_provider_or_default_account(self):
        code, result, calls = self.invoke([identity()], account="unknown@example.invalid")
        self.assertEqual(code, 2)
        self.assertEqual(result["errors"], ["registered_account_route_unavailable"])
        self.assertEqual(calls, [])

    def test_no_implicit_account_cli(self):
        for argv in (["google_capability_probe.py"],
                     ["google_capability_probe.py", "--account", ""],
                     ["google_capability_probe.py", "--account", "   "]):
            with self.subTest(argv=argv), patch.object(sys, "argv", argv), \
                    patch.object(resolver, "subprocess_probe_transport") as transport, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exit_context:
                    probe.main()
                self.assertEqual(exit_context.exception.code, 2)
                transport.assert_not_called()

    def test_identity_failures_are_bounded_and_do_not_echo_provider_payload(self):
        private_marker = "synthetic-provider-secret"
        cases = [
            resolver.ProbeCommandResult(124, private_marker, private_marker, "timeout"),
            resolver.ProbeCommandResult(125, private_marker, private_marker, "output_too_large"),
            resolver.ProbeCommandResult(0, "x" * (resolver.MAX_PROBE_OUTPUT_BYTES + 1)),
            resolver.ProbeCommandResult(0, '{"accounts":[],"accounts":[]}'),
            resolver.ProbeCommandResult(0, private_marker),
            resolver.ProbeCommandResult(403, private_marker, private_marker),
        ]
        for response in cases:
            with self.subTest(outcome=response.outcome, code=response.returncode):
                code, result, calls = self.invoke([identity()], results={"identity": response})
                self.assertEqual(code, 2)
                self.assertEqual(calls, [resolver.GOG_AUTH_LIST_PROBE_ARGV])
                self.assertNotIn(private_marker, json.dumps(result))

    def test_drive_denial_preserves_successful_gmail_read_without_write_inference(self):
        code, result, _ = self.invoke([identity(scopes=list(probe.REQUIRED_SCOPES.values()))],
                                      results={"drive": resolver.ProbeCommandResult(403, "", "synthetic-provider-secret")})
        self.assertEqual(code, 2)
        self.assertEqual(result["capabilities"]["gmail_read"]["status"], "ready")
        self.assertEqual(result["capabilities"]["drive_read"]["status"], "unverified")
        self.assertEqual(result["capabilities"]["drive_read"]["account"], ACCOUNT)
        self.assertEqual(result["evidence_operations"], ["oauth-identity-read", "gmail-read"])
        self.assertNotIn("synthetic-provider-secret", json.dumps(result))

    def test_bare_result_list_is_not_an_admitted_collection_envelope(self):
        code, result, _ = self.invoke([identity()], results={"drive": resolver.ProbeCommandResult(0, '[{"id":"synthetic-private-file"}]')})
        self.assertEqual(code, 2)
        self.assertEqual(result["capabilities"]["drive_read"]["evidence_operations"], [])
        self.assertNotIn("collection_counts", result["capabilities"]["drive_read"])
        self.assertNotIn("synthetic-private-file", json.dumps(result))

    def test_unknown_success_json_is_not_a_collection_read(self):
        for payload in ({}, [], {"status": "reauth required"},
                        {"nextPageToken": "synthetic-page-token"},
                        {"files": [], "status": "reauth required"}):
            with self.subTest(payload=payload):
                code, result, _ = self.invoke([identity()], results={"drive": resolver.ProbeCommandResult(0, json.dumps(payload))})
                self.assertEqual(code, 2)
                self.assertEqual(result["capabilities"]["drive_read"]["evidence_operations"], [])
                self.assertNotIn("synthetic-page-token", json.dumps(result))

    def test_collection_items_need_real_service_ids(self):
        for collection in ([{}], ["unknown"], [{"id": ""}], [{"id": None}],
                           [{"id": "synthetic-private-file"}, {"name": "synthetic-private-name"}]):
            with self.subTest(collection=collection):
                code, result, _ = self.invoke([identity()], results={"drive": resolver.ProbeCommandResult(0, json.dumps({"files": collection}))})
                self.assertEqual(code, 2)
                self.assertEqual(result["capabilities"]["drive_read"]["evidence_operations"], [])
                self.assertNotIn("synthetic-private-name", json.dumps(result))

    def test_operation_collection_cannot_be_swapped_or_invalidly_typed(self):
        for operation, payload in (("gmail", {"files": []}), ("drive", {"labels": []}),
                                   ("drive", {"files": {}}), ("drive", {"files": None}),
                                   ("drive", {"files": [], "nextPageToken": 3})):
            with self.subTest(operation=operation, payload=payload):
                code, result, _ = self.invoke([identity()], results={operation: resolver.ProbeCommandResult(0, json.dumps(payload))})
                self.assertEqual(code, 2)
                key = "gmail_read" if operation == "gmail" else "drive_read"
                self.assertEqual(result["capabilities"][key]["evidence_operations"], [])

    def test_genuinely_empty_expected_collections_are_successful_reads(self):
        code, result, _ = self.invoke([identity()], results={
            "gmail": resolver.ProbeCommandResult(0, '{"labels":[]}'),
            "drive": resolver.ProbeCommandResult(0, '{"files":[],"nextPageToken":""}'),
        })
        self.assertEqual(code, 0)
        self.assertEqual(result["capabilities"]["gmail_read"]["collection_counts"], {"labels": 0})
        self.assertEqual(result["capabilities"]["drive_read"]["collection_counts"], {"files": 0})

    def test_read_errors_malformed_or_oversized_output_never_become_read_evidence(self):
        cases = [
            resolver.ProbeCommandResult(0, '{"error":"synthetic-provider-secret"}'),
            resolver.ProbeCommandResult(0, '{"success":false}'),
            resolver.ProbeCommandResult(0, "null"),
            resolver.ProbeCommandResult(0, '{"files":[],"files":[]}'),
            resolver.ProbeCommandResult(0, "synthetic-provider-secret"),
            resolver.ProbeCommandResult(0, "x" * (resolver.MAX_PROBE_OUTPUT_BYTES + 1)),
            resolver.ProbeCommandResult(0, "{}", "", "timeout"),
            resolver.ProbeCommandResult(False, "{}"),
        ]
        for response in cases:
            with self.subTest(outcome=response.outcome, code=response.returncode):
                code, result, _ = self.invoke([identity()], results={"drive": response})
                self.assertEqual(code, 2)
                self.assertEqual(result["capabilities"]["drive_read"]["evidence_operations"], [])
                self.assertNotIn("synthetic-provider-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
