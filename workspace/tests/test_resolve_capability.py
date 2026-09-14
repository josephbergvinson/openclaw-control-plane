from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import contextlib
import hashlib
import io
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

from routing_test_support import fixture_root
ROOT = fixture_root()
RESOLVER = ROOT / "scripts" / "resolve_capability.py"


class ResolveCapabilityTests(unittest.TestCase):
    def _with_route_binding(self, args: tuple[str, ...]) -> list[str]:
        bound = list(args)
        if "--system" not in bound:
            return bound
        registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        slug = lambda value: re.sub(
            r"[^a-z0-9]+", "-", value.strip().lower()
        ).strip("-")
        system = slug(bound[bound.index("--system") + 1])
        systems = registry["system_aliases"].get(system, [system])
        context = (
            slug(bound[bound.index("--context") + 1])
            if "--context" in bound
            else ""
        )
        operation = (
            slug(bound[bound.index("--required-operation") + 1])
            if "--required-operation" in bound
            else ""
        )
        matches = [
            route
            for route in registry["routes"]
            if slug(route["system"]) in {slug(value) for value in systems}
        ]
        operation_matches = [
            route
            for route in matches
            if operation and operation in route.get("operations", {})
        ]
        if operation_matches:
            matches = operation_matches
        if "--account" in bound:
            requested_account = bound[bound.index("--account") + 1].strip()
            matches = [
                route
                for route in matches
                if route.get("required_account") == requested_account
            ]
        elif "--portfolio" in bound or "--workspace" in bound or "--network" in bound:
            bindings = registry.get("portfolio_routes", {}).get("by_route_id", {})
            for flag, field in (
                ("--portfolio", "portfolio_ids"),
                ("--workspace", "workspace_ids"),
                ("--network", "network_ids"),
            ):
                if flag not in bound:
                    continue
                requested = slug(bound[bound.index(flag) + 1])
                matches = [
                    route
                    for route in matches
                    if requested
                    in bindings.get(route["route_id"], {}).get(field, [])
                ]
        elif "--principal" in bound:
            requested_principal = slug(bound[bound.index("--principal") + 1])
            matches = [
                route
                for route in matches
                if slug(route.get("required_principal", "")) == requested_principal
            ]
        elif context:
            matches = [
                route
                for route in matches
                if context
                in {slug(value) for value in route.get("default_contexts", [])}
            ]
        if len(matches) != 1:
            return bound
        route = matches[0]
        if "--principal" not in bound:
            bound.extend(("--principal", route["required_principal"]))
        if "--account" not in bound:
            bound.extend(("--account", route["required_account"]))
        return bound

    def run_resolver_result(
        self,
        *args: str,
        auto_bind: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        resolved_args = self._with_route_binding(args) if auto_bind else list(args)
        return subprocess.run(
            [sys.executable, str(RESOLVER), *resolved_args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_resolver(self, *args: str) -> dict:
        result = self.run_resolver_result(*args)
        payload = json.loads(result.stdout)
        if result.returncode != 0:
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn(
                payload["execution_guard"]["blocker_code"],
                {
                    "blocked_browser_runtime_evidence_required",
                },
                result.stdout + result.stderr,
            )
        return payload

    @staticmethod
    def search_console_ready_signal() -> dict:
        return {
            "schema": "openclaw.google-search-console-probe.v1",
            "producer": "google_search_console_probe.py",
            "checked_at_utc": "2026-09-04T12:00:00Z",
            "account": _operator_binding('identifiers.accounts.personal_google'),
            "credential_env": (
                _operator_binding('paths.gog_env_file')
            ),
            "operation": "sites.list",
            "mutating": False,
            "evidence_role": "audit_only",
            "authoritative": False,
            "completion_claim_allowed": False,
            "page_indexing_example_table_verified": False,
            "status": "ready",
            "reason_code": None,
            "property_accessible": True,
            "requested_property_domain": None,
            "gog_exit_code": 0,
            "matching_property_count": 1,
            "authorized_matching_property_count": 1,
            "property_selection_required": True,
            "authorized_property_types": ["domain"],
            "evidence": (
                "exact-account Search Console sites.list returned authorized "
                "property access; this audit signal does not prove task completion "
                "or Page Indexing example-table inspection"
            ),
        }

    def test_generic_routing_defaults_and_policy_twins_are_bound(self) -> None:
        json_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(encoding="utf-8")
        )
        yaml_registry = yaml.safe_load(
            (ROOT / "registry" / "integration_routes.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            json_registry["routing_defaults"], yaml_registry["routing_defaults"]
        )
        self.assertEqual(
            json_registry["system_aliases"], yaml_registry["system_aliases"]
        )
        required_route_fields = {
            "required_principal",
            "required_account",
            "readiness_status_id",
            "access_probe_id",
            "native_lane_kind",
            "operations",
            "provider_adapter",
        }
        google_workspace_mutations = {
            "gmail-write",
            "calendar-write",
            "drive-write",
            "docs-write",
            "slides-write",
            "sheets-write",
            "forms-write",
            "meet-write",
            "appscript-write",
            "searchconsole-write",
        }
        expected_mutations = {
            "google-workspace-personal-gog": google_workspace_mutations,
            'google-workspace-company-alpha-coordinator-gog': google_workspace_mutations,
            'google-workspace-company-alpha-operator-gog': google_workspace_mutations,
            'google-workspace-company-beta-admin': google_workspace_mutations
            | {"admin-console-write", "workspace-domain-settings"},
            'google-workspace-company-beta-operator': google_workspace_mutations,
            'google-workspace-operations-gog': google_workspace_mutations,
            "google-gmail-personal-gog": {"gmail-draft", "gmail-send"},
            'google-gmail-company-alpha-coordinator': {"gmail-draft", "gmail-send"},
            "notion-personal": {"page-create", "page-update", "page-archive"},
            'notion-company-alpha': {"page-create", "page-update", "page-archive"},
            'jira-company-alpha': {"issue-create", "issue-update", "comment-create"},
            "digitalocean-openclaw-doctl": {"app-deploy"},
            'company-alpha-local-dev-docker': {"stack-start", "stack-stop"},
            "openclaw-hedera-mainnet-test-signer": {"transaction-sign"},
            "openclaw-hedera-testnet-test-signer": {"transaction-sign"},
            'company-alpha-walletconnect-testnet': {"request-execute"},
            'company-alpha-walletconnect-mainnet': {"request-execute"},
            'google-gmail-company-alpha-operator': {"gmail-draft"},
            "google-calendar-personal-gog": {"event-create", "event-delete"},
            'google-calendar-company-alpha-coordinator-gog': {"event-create", "event-delete"},
            "google-forms-personal-gog": {
                "form-create",
                "form-update",
                "form-publish",
                "form-response-submit",
            },
            'google-forms-company-alpha-coordinator-gog': {
                "form-create",
                "form-update",
                "form-publish",
                "form-response-submit",
            },
            "apple-calendar-local": {"event-create"},
            "trello-personal-api": {
                "card-create",
                "card-update",
                "card-label-add",
                "card-label-remove",
                "card-delete",
                "label-create",
            },
            "github-personal-cli": {
                "issue-create",
                "issue-delete",
                "issue-update",
                "issue-comment",
                "pull-request-create",
                "pull-request-update",
                "pull-request-comment",
            },
            'linear-company-beta-mcp': {
                "issue-create",
                "issue-update",
                "issue-cancel",
                "comment-create",
            },
            'cloudflare-operator-readonly-api': {
                "pages-preview-deploy"
            },
            'vercel-company-beta-browser': {"preview-deploy", "preview-delete"},
        }
        for route in json_registry["routes"]:
            self.assertEqual(required_route_fields - set(route), set(), route["route_id"])
            for retired in (
                "native_supported_operations",
                "authenticated_ui_required_operations",
                "authenticated_ui_mutation_operations",
                "operation_effects",
                "identity_assurance",
            ):
                self.assertNotIn(retired, route)
            operations = route["operations"]
            mutation_operations = {
                operation
                for operation, spec in operations.items()
                if spec["effect"] == "mutation"
            }
            self.assertEqual(
                mutation_operations,
                expected_mutations.get(route["route_id"], set()),
                route["route_id"],
            )
            for spec in operations.values():
                self.assertIn(spec["lane"], {"declared_native", "authenticated_ui"})
                self.assertIn(spec["effect"], {"read", "mutation"})
                if spec["effect"] == "mutation":
                    self.assertIsInstance(spec["readiness_status_id"], str)
                    self.assertTrue(spec["readiness_status_id"].strip())
        defaults = json_registry["routing_defaults"]
        self.assertEqual(defaults["native_lane_class"], "declared_native")
        self.assertEqual(
            defaults["fallback_order"],
            [
                "declared_native_route",
                "declared_probe_or_readiness_check",
                "persistent_managed_browser",
                "stock_extension_existing_session_browser",
                "host_os_ui_last_resort",
            ],
        )
        self.assertEqual(defaults["managed_browser_primary"]["profile_id"], "openclaw")
        self.assertEqual(defaults["managed_browser_primary"]["transport"], "managed_cdp")
        self.assertTrue(defaults["managed_browser_primary"]["persistent"])
        self.assertEqual(
            defaults["browser_profile_fallbacks"],
            [
                {
                    "profile_id": "chrome",
                    "automatic": True,
                    "requires_explicit_existing_session_handoff": False,
                }
            ],
        )
        self.assertEqual(defaults["browser_secret_entry"], "opaque_secret_broker_only")
        self.assertEqual(
            json_registry["system_aliases"]["existing-chrome-session"],
            ["openclaw-browser-chrome-session"],
        )
        route_ids = {route["route_id"] for route in json_registry["routes"]}
        self.assertIn("openclaw-browser-chrome-session", route_ids)
        self.assertNotIn("openclaw-browser-user-session", route_ids)
        handle_ids = {
            handle["handle_id"] for handle in json_registry["credential_handles"]
        }
        self.assertIn("browser.chrome.session", handle_ids)
        self.assertNotIn("browser.user.session", handle_ids)

        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        tools = (ROOT / "TOOLS.md").read_text(encoding="utf-8")
        self.assertIn("Prefer the supported native/upstream capability", agents)
        self.assertIn("human_only_auth_gates", json_registry["routing_defaults"])
        self.assertTrue(defaults["managed_browser_primary"]["persistent"])
        self.assertEqual(defaults["browser_secret_entry"], "opaque_secret_broker_only")

    def test_native_subagents_use_current_policy_and_probe_diagnostics(self) -> None:
        policy = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("native subagent sessions receive `AGENTS.md`", policy)
        payload = self.run_resolver("--system", "apple-calendar", "--intent", "read", "--required-operation", "calendar-list")
        self.assertFalse(payload["authoritative_probe_evidence"]["valid"])
        self.assertFalse(payload["route_readiness"]["live_probe_authoritative_passed"])

    def test_declarative_provider_operations_and_cloudflare_probe_match_yaml(self) -> None:
        json_routes = {
            row["route_id"]: row
            for row in json.loads(
                (ROOT / "registry" / "integration_routes.json").read_text(
                    encoding="utf-8"
                )
            )["routes"]
        }
        yaml_routes = {
            row["route_id"]: row
            for row in yaml.safe_load(
                (ROOT / "registry" / "integration_routes.yaml").read_text(
                    encoding="utf-8"
                )
            )["routes"]
        }
        json_probes = {
            row["probe_id"]: row
            for row in json.loads(
                (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
            )["probes"]
        }
        yaml_probes = {
            row["probe_id"]: row
            for row in yaml.safe_load(
                (ROOT / "registry" / "probes.yaml").read_text(encoding="utf-8")
            )["probes"]
        }

        gsc = json_routes["google-search-console-personal-gog"]
        self.assertEqual(gsc, yaml_routes["google-search-console-personal-gog"])
        self.assertEqual(gsc["operations"]["sites-list"]["lane"], "declared_native")
        self.assertEqual(
            gsc["operations"]["page-indexing-example-urls"]["lane"],
            "authenticated_ui",
        )
        adapter = gsc["provider_adapter"]
        self.assertEqual(adapter["type"], "authenticated_provider_evidence")
        self.assertEqual(
            adapter["request_evidence"]["property_domain"]["normalizer"],
            "dns_domain",
        )
        self.assertEqual(
            adapter["completion_evidence"]["required_for_operations"],
            ["page-indexing-example-urls"],
        )
        self.assertFalse(adapter["completion_evidence"]["resolver_can_verify"])

        personal_gmail = json_routes["google-gmail-personal-gog"]
        work_gmail = json_routes['google-gmail-company-alpha-coordinator']
        self.assertEqual(personal_gmail, yaml_routes["google-gmail-personal-gog"])
        self.assertEqual(work_gmail, yaml_routes['google-gmail-company-alpha-coordinator'])
        self.assertEqual(personal_gmail["required_account"], _operator_binding('identifiers.accounts.personal_google'))
        self.assertEqual(work_gmail["required_account"], _operator_binding('identifiers.accounts.company_alpha_coordinator_google'))
        self.assertEqual(
            personal_gmail["provider_adapter"], work_gmail["provider_adapter"]
        )
        self.assertEqual(
            personal_gmail["provider_adapter"]["type"],
            "authenticated_account_route",
        )
        gmail_url_evidence = personal_gmail["provider_adapter"][
            "target_url_evidence"
        ]
        self.assertEqual(
            gmail_url_evidence["path_capture_pattern"],
            r"(?:^|/)mail/u/([0-9]+)(?:/|$)",
        )
        self.assertEqual(
            gmail_url_evidence["captured_value_format"],
            "non_negative_integer",
        )
        self.assertEqual(gmail_url_evidence["captured_value_max_length"], 10)

        cloudflare_route_id = 'cloudflare-operator-readonly-api'
        cloudflare_probe_id = 'cloudflare-operator-readonly-probe'
        self.assertEqual(json_routes[cloudflare_route_id], yaml_routes[cloudflare_route_id])
        self.assertEqual(
            json_routes[cloudflare_route_id]["capability_scope"],
            "read-preview-deploy",
        )
        self.assertEqual(
            set(json_routes[cloudflare_route_id]["operations"]),
            {
                "token-verify",
                "account-read",
                "zone-read",
                "pages-project-read",
                "pages-deployment-read",
                "pages-preview-deploy",
            },
        )
        self.assertNotIn('${operator:', json_probes[cloudflare_probe_id]['command'])
        self.assertNotIn('${operator:', json_probes[cloudflare_probe_id]['command'])
        self.assertEqual(json_probes[cloudflare_probe_id], yaml_probes[cloudflare_probe_id])
        self.assertEqual(json_probes[cloudflare_probe_id]["scope"], cloudflare_route_id)

        neon_route_id = 'personal-data-neon-postgres-readonly'
        neon_probe_id = 'personal-data-neon-postgres-readonly-probe'
        self.assertEqual(json_routes[neon_route_id], yaml_routes[neon_route_id])
        self.assertEqual(json_routes[neon_route_id]["capability_scope"], "read_only")
        self.assertEqual(json_probes[neon_probe_id], yaml_probes[neon_probe_id])
        self.assertEqual(json_probes[neon_probe_id]["scope"], neon_route_id)

    def test_provider_evidence_guard_interprets_route_metadata_without_provider_branch(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        adapter = {
            "type": "authenticated_provider_evidence",
            "required_account_route_field": "tenant_identity",
            "identity_policy": {"required": True, "normalizer": "casefold"},
            "operation_policy": {"required": True},
            "request_evidence": {
                "site_domain": {
                    "normalizer": "dns_domain",
                    "output_key": "required_site_domain",
                    "validity_output_key": "required_site_domain_valid",
                }
            },
            "native_evidence": {"probe_receipt_authority": "none"},
            "browser_evidence": {
                "completion_authority": "runtime_authenticated_evidence_only",
                "allowed_decision": "collect_acme_evidence",
            },
            "completion_evidence": {
                "required_for_operations": ["inspect-record"],
                "resolver_can_verify": False,
                "prohibited_claims": ["finished"],
            },
        }
        guard = resolver["build_declarative_provider_guard"](
            preferred={
                "route_id": "acme-owner-route",
                "tenant_identity": "Owner@Example.com",
                "browser_profile_primary": {
                    "profile_id": "openclaw",
                    "transport": "managed_cdp",
                },
            },
            adapter=adapter,
            candidate_lane="browser",
            api_probe_state="not_run",
            api_probe_account="",
            browser_account="owner@example.com",
            request_evidence={"site_domain": "Example.COM."},
            user_requested_ui_state=False,
            explicit_existing_session_handoff=False,
            operation_contract={
                "required_operation": "inspect-record",
                "classification": "authenticated_ui_required",
                "authenticated_ui_required": True,
                "native_supported": False,
                "classification_source": "route_registry",
            },
        )

        self.assertFalse(guard["allowed"])
        self.assertEqual(
            guard["blocker_code"], "blocked_browser_runtime_evidence_required"
        )
        self.assertEqual(guard["required_account"], "owner@example.com")
        self.assertEqual(guard["required_site_domain"], "example.com")
        self.assertTrue(guard["required_site_domain_valid"])
        self.assertEqual(
            guard["prohibited_claims_without_provider_evidence"], ["finished"]
        )

        provider_mismatch = resolver["build_declarative_provider_guard"](
            preferred={
                "route_id": "acme-owner-route",
                "tenant_identity": "Owner@Example.com",
            },
            adapter=adapter,
            candidate_lane="declared_api",
            api_probe_state="failed",
            api_probe_account="other@example.com",
            browser_account="",
            request_evidence={"site_domain": "example.com"},
            user_requested_ui_state=False,
            explicit_existing_session_handoff=False,
            operation_contract={
                "required_operation": "inspect-record",
                "classification": "native_supported",
                "authenticated_ui_required": False,
                "native_supported": True,
                "classification_source": "route_registry",
            },
        )
        self.assertFalse(provider_mismatch["allowed"])
        self.assertEqual(
            provider_mismatch["blocker_code"],
            "blocked_provider_account_route_mismatch",
        )

        provider_state_without_account = resolver[
            "build_declarative_provider_guard"
        ](
            preferred={
                "route_id": "acme-owner-route",
                "tenant_identity": "Owner@Example.com",
            },
            adapter=adapter,
            candidate_lane="declared_api",
            api_probe_state="passed",
            api_probe_account="",
            browser_account="",
            request_evidence={"site_domain": "example.com"},
            user_requested_ui_state=False,
            explicit_existing_session_handoff=False,
            operation_contract={
                "required_operation": "inspect-record",
                "classification": "native_supported",
                "authenticated_ui_required": False,
                "native_supported": True,
                "classification_source": "route_registry",
            },
        )
        self.assertTrue(provider_state_without_account["allowed"])

        digest_required_adapter = json.loads(json.dumps(adapter))
        digest_required_adapter["identity_policy"][
            "provider_account_digest_required"
        ] = True
        missing_digest = resolver["build_declarative_provider_guard"](
            preferred={
                "route_id": "acme-owner-route",
                "tenant_identity": "Owner@Example.com",
            },
            adapter=digest_required_adapter,
            candidate_lane="declared_api",
            api_probe_state="not_run",
            api_probe_account="",
            browser_account="",
            request_evidence={"site_domain": "example.com"},
            user_requested_ui_state=False,
            explicit_existing_session_handoff=False,
            operation_contract={
                "required_operation": "inspect-record",
                "classification": "native_supported",
                "authenticated_ui_required": False,
                "native_supported": True,
                "classification_source": "route_registry",
            },
        )
        self.assertFalse(missing_digest["adapter_contract_valid"])
        self.assertFalse(missing_digest["allowed"])
        self.assertEqual(
            missing_digest["blocker_code"],
            "blocked_provider_adapter_invalid_contract",
        )

    def test_account_route_guard_interprets_route_metadata_without_provider_branch(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        adapter = {
            "type": "authenticated_account_route",
            "required_account_route_field": "tenant_identity",
            "identity_policy": {"required": True, "normalizer": "casefold"},
            "native_evidence": {
                "passed_states": ["passed"],
                "exhausted_states": ["failed", "unavailable"],
                "account_mismatch_blocker_code": "blocked_acme_route_unverified",
                "account_mismatch_decision": "block_mismatched_acme_account",
            },
            "browser_evidence": {
                "native_not_exhausted_blocker_code": "blocked_acme_native_not_exhausted",
                "account_mismatch_blocker_code": "blocked_acme_route_unverified",
                "allowed_decision": "allow_acme_browser_fallback",
            },
            "target_url_evidence": {
                "path_capture_pattern": r"(?:^|/)session/([0-9]+)(?:/|$)",
                "capture_group": 1,
                "captured_value_format": "non_negative_integer",
                "captured_value_max_length": 10,
                "matched_output_key": "has_session_slot",
                "captured_output_key": "session_slot",
                "constant_outputs": {"session_slot_is_identity": False},
            },
        }
        common = {
            "preferred": {
                "route_id": "acme-owner-route",
                "tenant_identity": "owner@example.com",
            },
            "adapter": adapter,
            "request_evidence": {},
            "user_requested_ui_state": False,
            "explicit_existing_session_handoff": False,
            "operation_contract": {
                "native_unsupported": False,
                "authenticated_ui_required": False,
                "operation_registered": True,
                "intent_known": True,
                "intent_effect_mismatch": False,
            },
            "routing_defaults": {},
        }

        mismatched = resolver["build_declarative_route_guard"](
            **common,
            candidate_lane="declared_api",
            api_probe_state="passed",
            api_probe_account="other@example.com",
            target_url="",
            browser_account="",
        )
        self.assertFalse(mismatched["allowed"])
        self.assertEqual(mismatched["blocker_code"], "blocked_acme_route_unverified")

        caller_failure_fallback = resolver["build_declarative_route_guard"](
            **common,
            candidate_lane="browser",
            api_probe_state="unavailable",
            api_probe_account="",
            target_url="",
            browser_account="owner@example.com",
        )
        self.assertFalse(caller_failure_fallback["allowed"])
        self.assertEqual(
            caller_failure_fallback["blocker_code"],
            "blocked_browser_runtime_evidence_required",
        )
        self.assertFalse(
            caller_failure_fallback["api_probe_claims_are_authoritative"]
        )
        self.assertFalse(
            caller_failure_fallback["api_route_exhausted_for_browser_fallback"]
        )
        self.assertFalse(caller_failure_fallback["browser_account_verified"])
        self.assertFalse(caller_failure_fallback["selected_lane_verified"])
        self.assertFalse(caller_failure_fallback["completion_claim_allowed"])
        self.assertEqual(
            caller_failure_fallback["matched_browser_fallback_predicates"],
            ["native_route_unavailable"],
        )

        failed_hint_fallback = resolver["build_declarative_route_guard"](
            **common,
            candidate_lane="browser",
            api_probe_state="failed",
            api_probe_account="",
            target_url="",
            browser_account="owner@example.com",
        )
        self.assertFalse(failed_hint_fallback["allowed"])
        self.assertEqual(
            failed_hint_fallback["matched_browser_fallback_predicates"],
            ["native_probe_failed"],
        )
        self.assertFalse(failed_hint_fallback["api_probe_claims_are_authoritative"])
        self.assertFalse(
            failed_hint_fallback["api_route_exhausted_for_browser_fallback"]
        )
        self.assertFalse(failed_hint_fallback["browser_account_verified"])

        browser = resolver["build_declarative_route_guard"](
            **{**common, "user_requested_ui_state": True},
            candidate_lane="browser",
            api_probe_state="unavailable",
            api_probe_account="",
            target_url="https://console.example/session/4/?token=secret#private",
            browser_account="owner@example.com",
        )
        self.assertFalse(browser["allowed"])
        self.assertEqual(
            browser["decision"],
            "require_runtime_verified_browser_route_and_account",
        )
        self.assertTrue(browser["browser_account_matches_required"])
        self.assertFalse(browser["browser_account_verified"])
        self.assertFalse(browser["selected_lane_verified"])
        self.assertFalse(browser["api_probe_claims_are_authoritative"])
        self.assertTrue(browser["api_probe_receipts_are_audit_only"])
        self.assertTrue(browser["target_url_evidence"]["has_session_slot"])
        self.assertEqual(browser["target_url_evidence"]["session_slot"], "4")
        self.assertFalse(browser["completion_claim_allowed"])
        self.assertNotIn("secret", json.dumps(browser))

        unsupported_operation = resolver["build_declarative_route_guard"](
            **{
                **common,
                "operation_contract": {
                    "native_unsupported": True,
                    "authenticated_ui_required": False,
                    "operation_registered": True,
                    "intent_known": True,
                    "intent_effect_mismatch": False,
                },
            },
            candidate_lane="browser",
            api_probe_state="not_run",
            api_probe_account="",
            target_url="",
            browser_account="owner@example.com",
        )
        self.assertFalse(unsupported_operation["allowed"])
        self.assertEqual(
            unsupported_operation["matched_browser_fallback_predicates"],
            ["native_operation_unsupported"],
        )
        self.assertFalse(unsupported_operation["browser_account_verified"])

        unsafe_path = resolver["build_declarative_route_guard"](
            **{**common, "user_requested_ui_state": True},
            candidate_lane="browser",
            api_probe_state="not_run",
            api_probe_account="",
            target_url="https://console.example/session/private-token/next",
            browser_account="owner@example.com",
        )
        self.assertFalse(unsafe_path["target_url_evidence"]["has_session_slot"])
        self.assertIsNone(unsafe_path["target_url_evidence"]["session_slot"])
        self.assertNotIn("private-token", json.dumps(unsafe_path))

        exact_adapter = json.loads(json.dumps(adapter))
        exact_adapter["identity_policy"]["normalizer"] = "exact"
        case_mismatch = resolver["build_declarative_route_guard"](
            **{
                **common,
                "adapter": exact_adapter,
                "preferred": {
                    "route_id": "acme-case-sensitive-route",
                    "tenant_identity": "Owner-Case",
                },
                "user_requested_ui_state": True,
            },
            candidate_lane="browser",
            api_probe_state="not_run",
            api_probe_account="",
            target_url="",
            browser_account="owner-case",
        )
        self.assertFalse(case_mismatch["browser_account_matches_required"])
        self.assertFalse(case_mismatch["allowed"])
        self.assertEqual(case_mismatch["blocker_code"], "blocked_acme_route_unverified")

        invalid_adapter = json.loads(json.dumps(adapter))
        invalid_adapter["native_evidence"]["passed_states"] = []
        invalid_contract = resolver["build_declarative_route_guard"](
            **{**common, "adapter": invalid_adapter},
            candidate_lane="declared_api",
            api_probe_state="passed",
            api_probe_account="owner@example.com",
            target_url="",
            browser_account="",
        )
        self.assertFalse(invalid_contract["adapter_contract_valid"])
        self.assertFalse(invalid_contract["allowed"])
        self.assertEqual(
            invalid_contract["blocker_code"],
            "blocked_provider_adapter_invalid_contract",
        )

        invalid_digest_adapter = json.loads(json.dumps(adapter))
        invalid_digest_adapter["native_evidence"][
            "provider_account_id_sha256"
        ] = "not-a-provider-account-digest"
        invalid_digest_contract = resolver["build_declarative_route_guard"](
            **{**common, "adapter": invalid_digest_adapter},
            candidate_lane="declared_api",
            api_probe_state="passed",
            api_probe_account="owner@example.com",
            target_url="",
            browser_account="",
        )
        self.assertFalse(invalid_digest_contract["adapter_contract_valid"])
        self.assertFalse(invalid_digest_contract["allowed"])
        self.assertIsNone(
            invalid_digest_contract["required_provider_account_id_sha256"]
        )
        self.assertNotIn(
            "not-a-provider-account-digest",
            json.dumps(invalid_digest_contract),
        )

        resolver_source = RESOLVER.read_text(encoding="utf-8")
        self.assertNotIn("build_gmail_route_guard", resolver_source)
        self.assertNotIn('normalize_slug(system) == "gmail"', resolver_source)
        self.assertNotIn('"gmail": ["gmail"]', resolver_source)
        self.assertNotIn("SYSTEM_ALIASES", resolver_source)
        self.assertNotIn("GENERIC_READINESS_TOKENS", resolver_source)
        self.assertNotIn("def route_score", resolver_source)
        self.assertNotIn("infer_from_route_text", resolver_source)

    def test_x_read_search_prefers_declared_api_research_lane_before_browser(self) -> None:
        payload = self.run_resolver(
            "--system",
            "x",
            "--intent",
            "read_search",
            "--context",
            "x-research",
            "--required-operation",
            "search-read",
        )

        self.assertEqual(payload["schema"], "openclaw.resolve_capability.v1")
        self.assertEqual(payload["systems_considered"], ["x-api"])
        self.assertIsNotNone(payload["preferred_lane"])
        self.assertEqual(payload["preferred_lane"]["route_id"], 'x-api-operator-x-xurl')
        self.assertIn("probe_command", payload["preferred_lane"])
        self.assertEqual(payload["fallback_order"][0], "declared_native_route")
        self.assertIn("browser", payload["fallback_order"][2])
        self.assertIn("Failure reports must name", " ".join(payload["constraints"]))
        self.assertFalse(payload["browser_fallback_gate"]["allowed"])
        self.assertFalse(payload["browser_fallback_gate"]["eligible"])
        self.assertEqual(payload["browser_fallback_gate"]["matched_predicates"], [])
        self.assertEqual(
            payload["browser_fallback_gate"]["decision"],
            "stay_on_declared_native_route",
        )

        defaults = payload["routing_defaults"]
        self.assertEqual(defaults["native_lane_class"], "declared_native")
        self.assertEqual(defaults["managed_browser_primary"]["profile_id"], "openclaw")
        self.assertEqual(defaults["managed_browser_primary"]["transport"], "managed_cdp")
        self.assertTrue(defaults["managed_browser_primary"]["persistent"])
        self.assertTrue(defaults["browser_profile_fallbacks"][0]["automatic"])
        self.assertIn("captcha", defaults["human_only_auth_gates"])

    def test_native_x_browser_decision_is_independent_of_readiness_age(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        probe_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        base_status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        status_id = (
            'X API read route — `operator-x` via `xurl` (`openclaw-research` app)'
        )
        timestamps = (
            (
                datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                False,
            ),
            ("2000-01-01T00:00:00Z", True),
        )

        for timestamp, expected_slo_breached in timestamps:
            with self.subTest(timestamp=timestamp):
                status = json.loads(json.dumps(base_status))
                row = next(
                    item
                    for item in status["capabilities"]
                    if item["capability_id"] == status_id
                )
                row["last_verified_utc"] = timestamp

                def fake_load_json(relative: str) -> dict:
                    return {
                        "registry/integration_routes.json": route_registry,
                        "registry/probes.json": probe_registry,
                        "status/capability_status.json": status,
                    }[relative]

                resolver["resolve"].__globals__["load_json"] = fake_load_json
                payload = resolver["resolve"](
                    "x",
                    "read_search",
                    "x-research",
                    required_operation="search-read",
                    requested_principal='operator',
                    requested_account=_operator_binding('identifiers.x_username'),
                )

                self.assertEqual(
                    payload["route_readiness"]["slo_breached"],
                    expected_slo_breached,
                )
                self.assertTrue(payload["execution_guard"]["allowed"])
                gate = payload["browser_fallback_gate"]
                self.assertFalse(gate["eligible"])
                self.assertFalse(gate["allowed"])
                self.assertEqual(gate["matched_predicates"], [])
                self.assertEqual(gate["decision"], "stay_on_declared_native_route")

    def test_failed_api_predicate_keeps_managed_default_then_stock_chrome(self) -> None:
        payload = self.run_resolver(
            "--system",
            "notion",
            "--intent",
            "read",
            "--context",
            "work",
            "--required-operation",
            "page-read",
            "--candidate-lane",
            "browser",
            "--browser-account",
            'company-alpha-team-notion',
            "--api-probe-state",
            "unavailable",
        )

        gate = payload["browser_fallback_gate"]
        self.assertTrue(gate["eligible"])
        self.assertFalse(gate["allowed"])
        self.assertEqual(
            gate["decision"],
            "require_runtime_verified_browser_route_and_account",
        )
        self.assertEqual(gate["preferred_browser_profile"], "openclaw")
        self.assertEqual(gate["browser_profile_order"], ["openclaw", "chrome"])
        self.assertEqual(
            gate["automatic_browser_profile_fallbacks"], ["chrome"]
        )
        self.assertEqual(gate["attended_browser_profile_fallbacks"], [])
        self.assertFalse(gate["user_browser_attach_required"])
        self.assertIn("preferred_api_failed", gate["required_predicate"])

    def test_failed_or_unavailable_caller_hint_cannot_downgrade_native_execution(self) -> None:
        cases = (
            ("failed", "native_probe_failed"),
            ("unavailable", "native_route_unavailable"),
        )
        for state, predicate in cases:
            with self.subTest(state=state):
                result = self.run_resolver_result(
                    "--system",
                    "notion",
                    "--intent",
                    "read",
                    "--context",
                    "work",
                    "--required-operation",
                    "page-read",
                    "--browser-account",
                    'company-alpha-team-notion',
                    "--api-probe-state",
                    state,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertTrue(payload["execution_guard"]["allowed"])
                self.assertEqual(
                    payload["execution_guard"]["decision"],
                    "use_declared_native_route",
                )
                self.assertIsNone(payload["execution_guard"]["blocker_code"])
                self.assertTrue(payload["route_readiness"]["diagnostic_only"])
                self.assertFalse(
                    payload["route_readiness"]["execution_prerequisite"]
                )
                self.assertTrue(payload["browser_fallback_gate"]["eligible"])
                self.assertFalse(payload["browser_fallback_gate"]["allowed"])
                self.assertIn(
                    predicate,
                    payload["browser_fallback_gate"]["matched_predicates"],
                )

    def test_generic_unsupported_operation_selects_persistent_managed_openclaw(self) -> None:
        payload = self.run_resolver(
            "--system",
            "notion",
            "--intent",
            "read",
            "--context",
            "work",
            "--candidate-lane",
            "browser",
            "--browser-account",
            'company-alpha-team-notion',
            "--required-operation",
            "page-read",
            "--native-operation-support",
            "unsupported",
        )

        gate = payload["browser_fallback_gate"]
        self.assertTrue(gate["eligible"])
        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["preferred_browser_profile"], "openclaw")
        self.assertTrue(gate["persistent_profile"])
        self.assertIn("native_operation_unsupported", gate["matched_predicates"])
        self.assertFalse(gate["automatic_user_profile_fallback_allowed"])
        self.assertEqual(gate["browser_secret_entry"], "opaque_secret_broker_only")
        self.assertIn("one_time_2fa_or_mfa", gate["human_only_auth_gates"])
        self.assertFalse(payload["execution_guard"]["allowed"])

    def test_generic_authenticated_ui_predicate_is_provider_neutral(self) -> None:
        payload = self.run_resolver(
            "--system",
            "notion",
            "--intent",
            "read",
            "--context",
            "personal",
            "--required-operation",
            "page-read",
            "--candidate-lane",
            "browser",
            "--browser-account",
            'operator-personal-notion',
            "--authenticated-ui-required",
        )

        self.assertEqual(
            payload["operation_contract"]["classification"],
            "native_supported",
        )
        self.assertTrue(
            payload["operation_contract"]["caller_authenticated_ui_required"]
        )
        self.assertNotIn(
            "authenticated_ui_required",
            payload["browser_fallback_gate"]["matched_predicates"],
        )
        self.assertEqual(
            payload["execution_guard"]["decision"],
            "require_runtime_verified_browser_route_and_account",
        )

    def test_generic_browser_fallback_without_predicate_fails_closed(self) -> None:
        result = self.run_resolver_result(
            "--system",
            "notion",
            "--intent",
            "read",
            "--context",
            "personal",
            "--required-operation",
            "page-read",
            "--candidate-lane",
            "browser",
            "--browser-account",
            'operator-personal-notion',
        )
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        guard = json.loads(result.stdout)["execution_guard"]
        self.assertEqual(
            guard["blocker_code"],
            "blocked_browser_runtime_evidence_required",
        )
        self.assertFalse(json.loads(result.stdout)["browser_fallback_gate"]["eligible"])

    def test_browser_requires_matching_route_account_without_provider_adapter(self) -> None:
        for browser_account in ("", "other-notion"):
            with self.subTest(browser_account=browser_account or "missing"):
                arguments = [
                    "--system",
                    "notion",
                    "--intent",
                    "read",
                    "--context",
                    "work",
                    "--required-operation",
                    "page-read",
                    "--candidate-lane",
                    "browser",
                    "--api-probe-state",
                    "unavailable",
                ]
                if browser_account:
                    arguments.extend(["--browser-account", browser_account])
                result = self.run_resolver_result(*arguments)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertFalse(payload["execution_guard"]["allowed"])
                self.assertFalse(payload["browser_fallback_gate"]["allowed"])
                self.assertEqual(
                    payload["execution_guard"]["blocker_code"],
                    "blocked_browser_runtime_evidence_required",
                )

        matched = self.run_resolver(
            "--system",
            "notion",
            "--intent",
            "read",
            "--context",
            "work",
            "--required-operation",
            "page-read",
            "--candidate-lane",
            "browser",
            "--api-probe-state",
            "unavailable",
            "--browser-account",
            'company-alpha-team-notion',
        )
        self.assertFalse(matched["browser_fallback_gate"]["allowed"])
        self.assertFalse(matched["execution_guard"]["allowed"])

    def test_native_redirect_cannot_authorize_browser_without_route_account(self) -> None:
        routes = (
            ("notion", "work", "page-read", 'company-alpha-team-notion'),
            (
                "gmail",
                'company-alpha-coordinator',
                "gmail-read",
                _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
            ),
        )
        for system, context, operation, required_account in routes:
            for browser_account in ("", f"wrong::{required_account}"):
                with self.subTest(
                    system=system,
                    browser_account=browser_account or "missing",
                ):
                    arguments = [
                        "--system",
                        system,
                        "--intent",
                        "read",
                        "--context",
                        context,
                        "--required-operation",
                        operation,
                        "--candidate-lane",
                        "browser",
                        "--api-probe-state",
                        "unavailable",
                    ]
                    if browser_account:
                        arguments.extend(["--browser-account", browser_account])
                    result = self.run_resolver_result(*arguments)
                    self.assertEqual(
                        result.returncode, 3, result.stdout + result.stderr
                    )
                    payload = json.loads(result.stdout)
                    self.assertFalse(payload["execution_guard"]["allowed"])
                    self.assertFalse(payload["browser_fallback_gate"]["allowed"])
                    self.assertFalse(
                        payload["identity_contract"][
                            "browser_account_matches_required"
                        ]
                    )

    def test_neon_postgres_aliases_prefer_exact_readonly_native_route(self) -> None:
        for alias in ("neon", "postgres", "postgresql"):
            with self.subTest(alias=alias):
                payload = self.run_resolver(
                    "--system",
                    alias,
                    "--intent",
                    "read",
                    "--context",
                    'personal-data-project',
                    "--required-operation",
                    "query-readonly",
                )
                preferred = payload["preferred_lane"]
                self.assertEqual(
                    preferred["route_id"], 'personal-data-neon-postgres-readonly'
                )
                self.assertEqual(
                    preferred["probe_id"],
                    'personal-data-neon-postgres-readonly-probe',
                )
                self.assertEqual(preferred["capability_scope"], "read_only")
                self.assertIn(
                    'personal_data_neon_readonly_capability_probe.py',
                    preferred["probe_command"],
                )
                self.assertEqual(
                    payload["operation_contract"]["classification"],
                    "native_supported",
                )
                self.assertTrue(payload["execution_guard"]["allowed"])
                self.assertIsNone(payload["execution_guard"]["blocker_code"])
                self.assertTrue(payload["route_readiness"]["diagnostic_only"])
                self.assertFalse(
                    payload["route_readiness"]["execution_prerequisite"]
                )
                self.assertFalse(payload["browser_fallback_gate"]["allowed"])

    def test_neon_postgres_readonly_route_rejects_unregistered_write(self) -> None:
        for alias in ("neon", "postgres", "postgresql"):
            with self.subTest(alias=alias):
                result = self.run_resolver_result(
                    "--system",
                    alias,
                    "--intent",
                    "write",
                    "--context",
                    'personal-data-project',
                )
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertTrue(payload["operation_contract"]["unknown_mutation"])
                self.assertEqual(
                    payload["execution_guard"]["blocker_code"],
                    "blocked_unknown_mutation_operation",
                )

    def test_arbitrary_service_without_route_cannot_use_browser_fallback(self) -> None:
        native_result = self.run_resolver_result(
            "--system",
            "example-service",
            "--intent",
            "read",
        )
        self.assertEqual(native_result.returncode, 2, native_result.stdout + native_result.stderr)
        native_payload = json.loads(native_result.stdout)
        self.assertEqual(native_payload["systems_considered"], ["example-service"])
        self.assertIsNone(native_payload["preferred_lane"])
        self.assertEqual(native_payload["checked_lanes"], [])
        self.assertTrue(
            native_payload["operation_contract"]["native_route_unavailable"]
        )
        self.assertEqual(
            native_payload["operation_contract"]["classification_source"],
            "route_registry_absence",
        )
        self.assertEqual(
            native_payload["browser_fallback_gate"]["matched_predicates"],
            ["native_route_unavailable"],
        )
        self.assertFalse(native_payload["browser_fallback_gate"]["allowed"])
        self.assertEqual(
            native_payload["browser_fallback_gate"]["preferred_browser_profile"],
            "openclaw",
        )
        self.assertFalse(native_payload["execution_guard"]["allowed"])
        self.assertNotEqual(
            native_payload["execution_guard"]["decision"],
            "use_declared_native_route",
        )

        browser_result = self.run_resolver_result(
            "--system",
            "example-service",
            "--intent",
            "read",
            "--candidate-lane",
            "browser",
        )
        self.assertEqual(browser_result.returncode, 2, browser_result.stdout)
        browser_payload = json.loads(browser_result.stdout)
        self.assertFalse(browser_payload["execution_guard"]["allowed"])
        self.assertEqual(
            browser_payload["browser_fallback_gate"]["decision"],
            "stay_on_declared_native_route",
        )

    def test_generic_company_alpha_context_does_not_choose_one_gmail_account(self) -> None:
        result = self.run_resolver_result(
            "--system",
            "gmail",
            "--intent",
            "read",
            "--context",
            'company-alpha',
            "--required-operation",
            "gmail-read",
            auto_bind=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsNone(payload["preferred_lane"])
        self.assertEqual(
            payload["route_selection_blocker"], "blocked_no_exact_context_route"
        )

    def test_gmail_u_index_does_not_authorize_browser_before_api_probe(self) -> None:
        result = self.run_resolver_result(
            "--system",
            "gmail",
            "--intent",
            "read",
            "--context",
            'company-alpha-coordinator',
            "--required-operation",
            "gmail-read",
            "--candidate-lane",
            "browser",
            "--target-url",
            "https://mail.google.com/mail/u/0/#search/example",
        )
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        guard = payload["route_guard"]
        self.assertFalse(guard["allowed"])
        self.assertEqual(guard["blocker_code"], "blocked_preferred_gmail_api_route_not_exhausted")
        self.assertTrue(guard["target_url_evidence"]["has_session_account_index"])
        self.assertEqual(guard["target_url_evidence"]["session_account_index"], "0")
        self.assertFalse(guard["target_url_evidence"]["session_account_index_is_identity"])
        self.assertNotIn("#search/example", result.stdout)

        unsafe_path = self.run_resolver_result(
            "--system",
            "gmail",
            "--intent",
            "read",
            "--context",
            'company-alpha-coordinator',
            "--required-operation",
            "gmail-read",
            "--candidate-lane",
            "browser",
            "--target-url",
            "https://mail.google.com/mail/u/private-path-secret/inbox",
        )
        self.assertEqual(unsafe_path.returncode, 3)
        unsafe_guard = json.loads(unsafe_path.stdout)["route_guard"]
        self.assertFalse(
            unsafe_guard["target_url_evidence"]["has_session_account_index"]
        )
        self.assertIsNone(
            unsafe_guard["target_url_evidence"]["session_account_index"]
        )
        self.assertNotIn("private-path-secret", unsafe_path.stdout)

    def test_gmail_browser_fallback_blocks_wrong_account_after_api_failure(self) -> None:
        result = self.run_resolver_result(
            "--system",
            "gmail",
            "--intent",
            "read",
            "--context",
            'company-alpha-coordinator',
            "--required-operation",
            "gmail-read",
            "--candidate-lane",
            "browser",
            "--api-probe-state",
            "failed",
            "--browser-account",
            _operator_binding('identifiers.accounts.personal_google'),
        )
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        guard = json.loads(result.stdout)["route_guard"]
        self.assertEqual(guard["blocker_code"], "blocked_requested_gmail_route_unverified")
        self.assertFalse(guard["browser_account_verified"])

    def test_gmail_failure_hint_allows_evidence_collection_without_verification(self) -> None:
        caller_only = self.run_resolver_result(
            "--system",
            "gmail",
            "--intent",
            "read",
            "--context",
            'company-alpha-coordinator',
            "--required-operation",
            "gmail-read",
            "--candidate-lane",
            "browser",
            "--api-probe-state",
            "unavailable",
            "--browser-account",
            _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
        )
        self.assertEqual(caller_only.returncode, 3, caller_only.stdout + caller_only.stderr)
        caller_payload = json.loads(caller_only.stdout)
        caller_guard = caller_payload["route_guard"]
        self.assertFalse(caller_guard["allowed"])
        self.assertTrue(caller_guard["browser_account_matches_required"])
        self.assertFalse(caller_guard["browser_account_verified"])
        self.assertFalse(caller_guard["api_probe_claims_are_authoritative"])
        self.assertFalse(caller_guard["api_route_exhausted_for_browser_fallback"])
        self.assertFalse(caller_guard["selected_lane_verified"])
        self.assertFalse(caller_guard["completion_claim_allowed"])
        self.assertEqual(
            caller_guard["matched_browser_fallback_predicates"],
            ["native_route_unavailable"],
        )
        self.assertFalse(caller_payload["browser_fallback_gate"]["allowed"])
        self.assertFalse(caller_payload["execution_guard"]["allowed"])
        self.assertEqual(
            caller_payload["execution_guard"]["blocker_code"],
            "blocked_browser_runtime_evidence_required",
        )

        payload = self.run_resolver(
            "--system",
            "gmail",
            "--intent",
            "read",
            "--context",
            'company-alpha-coordinator',
            "--required-operation",
            "gmail-read",
            "--candidate-lane",
            "browser",
            "--api-probe-state",
            "unavailable",
            "--browser-account",
            _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
            "--authenticated-ui-required",
        )
        guard = payload["route_guard"]
        self.assertFalse(guard["allowed"])
        self.assertTrue(guard["browser_account_matches_required"])
        self.assertFalse(guard["browser_account_verified"])
        self.assertFalse(guard["selected_lane_verified"])
        self.assertEqual(
            guard["decision"],
            "require_runtime_verified_browser_route_and_account",
        )
        self.assertEqual(
            guard["matched_browser_fallback_predicates"],
            ["native_route_unavailable"],
        )
        self.assertFalse(guard["completion_claim_allowed"])
        self.assertEqual(
            guard["browser_profile_primary"],
            {"profile_id": "openclaw", "transport": "managed_cdp"},
        )
        self.assertFalse(guard["automatic_user_profile_fallback_allowed"])

    def test_gmail_failed_or_unavailable_native_hint_does_not_select_browser(self) -> None:
        for state in ("failed", "unavailable"):
            with self.subTest(state=state):
                result = self.run_resolver_result(
                    "--system",
                    "gmail",
                    "--intent",
                    "read",
                    "--context",
                    'company-alpha-coordinator',
                    "--required-operation",
                    "gmail-read",
                    "--api-probe-state",
                    state,
                    "--api-probe-account",
                    _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
                    "--browser-account",
                    _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                guard = payload["route_guard"]
                self.assertTrue(payload["execution_guard"]["allowed"])
                self.assertEqual(
                    payload["execution_guard"]["decision"],
                    "use_declared_native_route",
                )
                self.assertIsNone(payload["execution_guard"]["blocker_code"])
                self.assertTrue(payload["browser_fallback_gate"]["eligible"])
                self.assertFalse(payload["browser_fallback_gate"]["allowed"])
                self.assertFalse(guard["api_probe_claims_are_authoritative"])
                self.assertFalse(guard["api_probe_account_verified"])
                self.assertFalse(guard["api_route_exhausted_for_browser_fallback"])
                self.assertFalse(guard["route_verified"])
                self.assertTrue(guard["allowed"])
                self.assertFalse(guard["selected_lane_verified"])
                self.assertFalse(guard["completion_claim_allowed"])

    def test_gmail_api_probe_requires_exact_account_identity_before_verification(self) -> None:
        for state in ("not_run", "passed", "failed", "unavailable"):
            with self.subTest(state=state):
                mismatched = self.run_resolver_result(
                    "--system",
                    "gmail",
                    "--intent",
                    "read",
                    "--context",
                    'company-alpha-coordinator',
                    "--required-operation",
                    "gmail-read",
                    "--api-probe-state",
                    state,
                    "--api-probe-account",
                    _operator_binding('identifiers.accounts.personal_google'),
                )
                self.assertEqual(
                    mismatched.returncode,
                    3,
                    mismatched.stdout + mismatched.stderr,
                )
                mismatch_payload = json.loads(mismatched.stdout)
                mismatch_guard = mismatch_payload["route_guard"]
                self.assertFalse(mismatch_guard["route_verified"])
                self.assertFalse(mismatch_guard["api_probe_account_verified"])
                self.assertEqual(
                    mismatch_guard["blocker_code"],
                    "blocked_requested_gmail_route_unverified",
                )
                self.assertFalse(mismatch_payload["execution_guard"]["allowed"])
                self.assertEqual(
                    mismatch_payload["execution_guard"]["blocker_code"],
                    "blocked_requested_gmail_route_unverified",
                )

        passed_without_account = self.run_resolver_result(
            "--system",
            "gmail",
            "--intent",
            "read",
            "--context",
            'company-alpha-coordinator',
            "--required-operation",
            "gmail-read",
            "--api-probe-state",
            "passed",
        )
        self.assertEqual(
            passed_without_account.returncode,
            0,
            passed_without_account.stdout + passed_without_account.stderr,
        )
        self.assertTrue(
            json.loads(passed_without_account.stdout)["execution_guard"]["allowed"]
        )

        verified = self.run_resolver(
            "--system",
            "gmail",
            "--intent",
            "read",
            "--context",
            'company-alpha-coordinator',
            "--required-operation",
            "gmail-read",
            "--api-probe-state",
            "passed",
            "--api-probe-account",
            _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
        )
        verified_guard = verified["route_guard"]
        self.assertTrue(verified_guard["declared_api_probe_account_matches_required"])
        self.assertFalse(verified_guard["route_verified"])
        self.assertFalse(verified_guard["api_probe_account_verified"])
        self.assertFalse(verified_guard["api_probe_claims_are_authoritative"])
        self.assertTrue(verified_guard["api_probe_receipts_are_audit_only"])
        self.assertTrue(verified_guard["live_probe_required"])
        self.assertFalse(verified_guard["completion_claim_allowed"])

    def test_browser_identity_diagnostics_share_the_adapter_normalizer(self) -> None:
        result = self.run_resolver_result(
            "--system",
            "gmail",
            "--intent",
            "read",
            "--context",
            'company-alpha-coordinator',
            "--required-operation",
            "gmail-read",
            "--candidate-lane",
            "browser",
            "--api-probe-state",
            "unavailable",
            "--browser-account",
            _operator_binding('identifiers.accounts.company_alpha_coordinator_google').upper(),
        )
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(
            payload["identity_contract"]["browser_account_matches_required"]
        )
        self.assertTrue(payload["route_guard"]["browser_account_matches_required"])
        self.assertEqual(
            payload["identity_contract"]["browser_account"],
            _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
        )

    def test_search_console_aliases_resolve_exact_personal_route(self) -> None:
        for alias in ("gsc", "searchconsole", "search-console", "google-search-console"):
            with self.subTest(alias=alias):
                payload = self.run_resolver(
                    "--system",
                    alias,
                    "--intent",
                    "read",
                    "--context",
                    "search-console",
                    "--required-operation",
                    "sites-list",
                    "--property-domain",
                    _operator_binding('services.cloudflare.zone_name'),
                    "--issue-label",
                    "Page with redirect",
                )

                preferred = payload["preferred_lane"]
                self.assertEqual(preferred["route_id"], "google-search-console-personal-gog")
                self.assertEqual(preferred["probe_id"], "google-search-console-personal-gog-probe")
                self.assertEqual(preferred["readiness"]["state"], "ready")
                self.assertEqual(
                    preferred["readiness"]["evidence_operations"],
                    ["sites-list"],
                )
                self.assertIsNone(preferred["property_domain"])
                self.assertIsNone(preferred["issue_label"])
                self.assertNotIn("Page with redirect", preferred["route_notes"])
                self.assertEqual(
                    preferred["browser_profile_primary"],
                    {"profile_id": "openclaw", "transport": "managed_cdp"},
                )
                self.assertEqual(
                    preferred["browser_profile_fallbacks"],
                    [
                        {
                            "profile_id": "chrome",
                            "automatic": True,
                            "requires_explicit_existing_session_handoff": False,
                        }
                    ],
                )
                self.assertEqual(payload["route_guard"]["required_account"], _operator_binding('identifiers.accounts.personal_google'))
                self.assertEqual(
                    payload["route_guard"]["required_property_domain"],
                    _operator_binding('services.cloudflare.zone_name'),
                )
                self.assertEqual(
                    payload["route_guard"]["required_issue_label"],
                    "Page with redirect",
                )
                self.assertEqual(
                    payload["route_guard"]["provider_adapter_type"],
                    preferred["provider_adapter"]["type"],
                )
                self.assertNotIn("legacy_provider_guard", payload)

    def test_search_console_accepts_only_route_declared_generic_evidence(self) -> None:
        payload = self.run_resolver(
            "--system",
            "gsc",
            "--intent",
            "read",
            "--required-operation",
            "sites-list",
            "--route-evidence",
            'property_domain=EXAMPLE.INVALID.',
            "--route-evidence",
            "issue_label=Page with redirect",
        )
        guard = payload["route_guard"]
        self.assertEqual(guard["required_property_domain"], _operator_binding('services.cloudflare.zone_name'))
        self.assertEqual(guard["required_issue_label"], "Page with redirect")
        self.assertEqual(guard["undeclared_request_evidence"], [])

        result = self.run_resolver_result(
            "--system",
            "gsc",
            "--intent",
            "read",
            "--required-operation",
            "sites-list",
            "--route-evidence",
            "undeclared_hint=value",
        )
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        rejected_guard = json.loads(result.stdout)["route_guard"]
        self.assertEqual(
            rejected_guard["blocker_code"],
            "blocked_undeclared_provider_evidence_input",
        )
        self.assertEqual(
            rejected_guard["undeclared_request_evidence"], ["undeclared_hint"]
        )

    def test_search_console_page_indexing_examples_reject_api_lane(self) -> None:
        result = self.run_resolver_result(
            "--system",
            "gsc",
            "--intent",
            "read",
            "--context",
            "search-console",
            "--required-operation",
            "page-indexing-example-urls",
            "--property-domain",
            _operator_binding('services.cloudflare.zone_name'),
            "--issue-label",
            "Page with redirect",
        )
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        guard = payload["route_guard"]
        self.assertTrue(guard["ui_only_operation"])
        self.assertFalse(guard["api_supports_required_operation"])
        self.assertEqual(guard["required_issue_label"], "Page with redirect")
        self.assertEqual(guard["blocker_code"], "blocked_required_operation_unsupported_by_api")
        self.assertEqual(
            guard["decision"],
            "use_runtime_authenticated_browser_evidence_lane",
        )
        self.assertFalse(guard["provider_example_evidence_verified"])
        self.assertFalse(guard["completion_claim_allowed"])
        self.assertEqual(guard["unresolved_evidence_disposition"], "blocked_or_unverified")
        self.assertEqual(
            guard["prohibited_claims_without_provider_evidence"],
            ["benign", "fixed", "normal", "complete", "no defect"],
        )

    def test_search_console_route_does_not_persist_request_site_or_issue(self) -> None:
        payload = self.run_resolver(
            "--system",
            "gsc",
            "--intent",
            "read",
            "--required-operation",
            "sites-list",
        )
        preferred = payload["preferred_lane"]
        guard = payload["route_guard"]
        self.assertIsNone(preferred["property_domain"])
        self.assertIsNone(preferred["issue_label"])
        self.assertIsNone(guard["required_property_domain"])
        self.assertIsNone(guard["required_issue_label"])
        self.assertEqual(guard["required_account"], _operator_binding('identifiers.accounts.personal_google'))
        self.assertEqual(guard["browser_profile_order"], ["openclaw", "chrome"])

    def test_search_console_invalid_request_domain_fails_closed(self) -> None:
        result = self.run_resolver_result(
            "--system",
            "gsc",
            "--intent",
            "read",
            "--required-operation",
            "sites-list",
            "--property-domain",
            "https://example.com/",
        )
        self.assertEqual(result.returncode, 3)
        guard = json.loads(result.stdout)["route_guard"]
        self.assertFalse(guard["required_property_domain_valid"])
        self.assertEqual(
            guard["blocker_code"],
            "blocked_invalid_search_console_property_domain",
        )
        self.assertEqual(
            guard["blocker_code"],
            json.loads(result.stdout)["preferred_lane"]["provider_adapter"]
            ["request_evidence"]["property_domain"]["invalid_blocker_code"],
        )

    def test_search_console_ui_only_browser_fallback_requires_exact_account(self) -> None:
        for browser_account in ("", "someone@example.com"):
            with self.subTest(browser_account=browser_account or "missing"):
                args = [
                    "--system",
                    "searchconsole",
                    "--intent",
                    "read",
                    "--context",
                    "search-console",
                    "--required-operation",
                    "page-indexing-example-urls",
                    "--candidate-lane",
                    "browser",
                ]
                if browser_account:
                    args.extend(["--browser-account", browser_account])
                result = self.run_resolver_result(*args)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                guard = json.loads(result.stdout)["route_guard"]
                self.assertFalse(guard["browser_account_verified"])
                self.assertEqual(guard["blocker_code"], "blocked_requested_search_console_route_unverified")

        payload = self.run_resolver(
            "--system",
            "search-console",
            "--intent",
            "read",
            "--context",
            "search-console",
            "--required-operation",
            "page-indexing-example-urls",
            "--candidate-lane",
            "browser",
            "--browser-account",
            _operator_binding('identifiers.accounts.personal_google'),
        )
        guard = payload["route_guard"]
        self.assertFalse(guard["allowed"])
        self.assertTrue(guard["browser_account_matches_required"])
        self.assertFalse(guard["browser_account_verified"])
        self.assertFalse(guard["selected_lane_verified"])
        self.assertEqual(
            guard["decision"],
            "require_runtime_verified_browser_route_and_account",
        )
        self.assertEqual(guard["browser_profile_order"], ["openclaw", "chrome"])
        self.assertEqual(
            guard["browser_profile_primary"],
            {"profile_id": "openclaw", "transport": "managed_cdp"},
        )
        self.assertEqual(
            guard["browser_profile_fallbacks"],
            [
                {
                    "profile_id": "chrome",
                    "automatic": True,
                    "requires_explicit_existing_session_handoff": False,
                }
            ],
        )
        self.assertEqual(guard["automatic_browser_profile_fallbacks"], ["chrome"])
        self.assertEqual(guard["attended_browser_profile_fallbacks"], [])
        self.assertFalse(guard["automatic_user_profile_fallback_allowed"])
        self.assertFalse(guard["explicit_existing_session_handoff"])
        self.assertFalse(guard["user_profile_fallback_eligible"])
        self.assertEqual(payload["browser_fallback_gate"]["preferred_browser_profile"], "openclaw")
        self.assertFalse(
            payload["browser_fallback_gate"]["automatic_user_profile_fallback_allowed"]
        )
        self.assertFalse(guard["completion_claim_allowed"])

        self.assertEqual(
            guard["browser_completion_authority"],
            "runtime_authenticated_evidence_only",
        )
        self.assertFalse(guard["provider_example_evidence_verified"])
        self.assertNotIn("browser_evidence_receipt", guard)

    def test_search_console_chrome_profile_is_automatic_existing_session_fallback(self) -> None:
        payload = self.run_resolver(
            "--system",
            "search-console",
            "--intent",
            "read",
            "--context",
            "personal",
            "--required-operation",
            "page-indexing-example-urls",
            "--candidate-lane",
            "browser",
            "--browser-account",
            _operator_binding('identifiers.accounts.personal_google'),
        )
        guard = payload["route_guard"]
        fallback_gate = payload["browser_fallback_gate"]

        self.assertEqual(guard["browser_profile_order"], ["openclaw", "chrome"])
        self.assertEqual(guard["automatic_browser_profile_fallbacks"], ["chrome"])
        self.assertEqual(guard["attended_browser_profile_fallbacks"], [])
        self.assertFalse(guard["automatic_user_profile_fallback_allowed"])
        self.assertFalse(guard["explicit_existing_session_handoff"])
        self.assertFalse(guard["user_profile_fallback_eligible"])
        self.assertFalse(fallback_gate["automatic_user_profile_fallback_allowed"])
        self.assertFalse(fallback_gate["user_profile_fallback_eligible"])
        self.assertEqual(
            fallback_gate["automatic_browser_profile_fallbacks"], ["chrome"]
        )
        self.assertEqual(fallback_gate["attended_browser_profile_fallbacks"], [])
        self.assertFalse(guard["completion_claim_allowed"])

        generic_ui_request = self.run_resolver(
            "--system",
            "search-console",
            "--intent",
            "read",
            "--context",
            "personal",
            "--user-requested-ui-state",
            "--required-operation",
            "page-indexing-example-urls",
            "--candidate-lane",
            "browser",
            "--browser-account",
            _operator_binding('identifiers.accounts.personal_google'),
        )["route_guard"]
        self.assertFalse(generic_ui_request["explicit_existing_session_handoff"])
        self.assertFalse(generic_ui_request["user_profile_fallback_eligible"])
        self.assertEqual(
            generic_ui_request["automatic_browser_profile_fallbacks"], ["chrome"]
        )

    def test_search_console_missing_operation_fails_closed(self) -> None:
        result = self.run_resolver_result(
            "--system",
            "gsc",
            "--intent",
            "read",
        )
        self.assertEqual(result.returncode, 3)
        guard = json.loads(result.stdout)["route_guard"]
        self.assertFalse(guard["operation_specified"])
        self.assertFalse(guard["allowed"])
        self.assertFalse(guard["completion_claim_allowed"])
        self.assertEqual(guard["blocker_code"], "blocked_unknown_search_console_operation")

    def test_search_console_unknown_operation_fails_closed(self) -> None:
        result = self.run_resolver_result(
            "--system",
            "gsc",
            "--intent",
            "read",
            "--required-operation",
            "page-indexing-example-url",
        )
        self.assertEqual(result.returncode, 3)
        guard = json.loads(result.stdout)["route_guard"]
        self.assertFalse(guard["operation_known"])
        self.assertFalse(guard["completion_claim_allowed"])
        self.assertEqual(guard["blocker_code"], "blocked_unknown_search_console_operation")

    def test_search_console_api_supported_operation_stays_api_first(self) -> None:
        payload = self.run_resolver(
            "--system",
            "google-search-console",
            "--intent",
            "read",
            "--context",
            "search-console",
            "--required-operation",
            "sites-list",
        )
        guard = payload["route_guard"]
        self.assertTrue(guard["api_supports_required_operation"])
        self.assertTrue(guard["api_probe_receipts_are_audit_only"])
        self.assertFalse(guard["api_completion_receipt_supported"])
        self.assertEqual(guard["api_probe_receipt_authority"], "none")
        self.assertEqual(
            guard["decision"], "allow_api_evidence_collection"
        )
        self.assertIsNone(guard["blocker_code"])
        self.assertTrue(guard["allowed"])
        self.assertFalse(guard["completion_claim_allowed"])
        self.assertFalse(payload["browser_fallback_gate"]["allowed"])

        self_attested = self.run_resolver(
            "--system",
            "google-search-console",
            "--intent",
            "read",
            "--required-operation",
            "sites-list",
            "--api-probe-state",
            "passed",
            "--api-probe-account",
            _operator_binding('identifiers.accounts.personal_google'),
        )["route_guard"]
        self.assertFalse(self_attested["api_probe_claims_are_authoritative"])
        self.assertFalse(self_attested["api_probe_account_verified"])
        self.assertFalse(self_attested["api_route_verified"])
        self.assertFalse(self_attested["completion_claim_allowed"])

        caller_receipt = self.run_resolver_result(
            "--system",
            "google-search-console",
            "--intent",
            "read",
            "--required-operation",
            "sites-list",
            "--api-probe-receipt",
            "/tmp/caller-provided-receipt.json",
        )
        self.assertEqual(caller_receipt.returncode, 2)
        self.assertIn("unrecognized arguments: --api-probe-receipt", caller_receipt.stderr)

        browser_result = self.run_resolver_result(
            "--system",
            "google-search-console",
            "--intent",
            "read",
            "--context",
            "search-console",
            "--required-operation",
            "sites-list",
            "--candidate-lane",
            "browser",
            "--browser-account",
            _operator_binding('identifiers.accounts.personal_google'),
        )
        self.assertEqual(browser_result.returncode, 3, browser_result.stdout + browser_result.stderr)
        browser_guard = json.loads(browser_result.stdout)["route_guard"]
        self.assertEqual(
            browser_guard["blocker_code"],
            "blocked_preferred_search_console_api_route_not_exhausted",
        )

        caller_failure = self.run_resolver_result(
            "--system",
            "google-search-console",
            "--intent",
            "read",
            "--required-operation",
            "sites-list",
            "--candidate-lane",
            "browser",
            "--browser-account",
            _operator_binding('identifiers.accounts.personal_google'),
            "--api-probe-state",
            "failed",
        )
        self.assertEqual(caller_failure.returncode, 3)
        fallback_guard = json.loads(caller_failure.stdout)["route_guard"]
        self.assertFalse(fallback_guard["api_route_exhausted_for_browser_fallback"])
        self.assertEqual(
            fallback_guard["blocker_code"],
            "blocked_preferred_search_console_api_route_not_exhausted",
        )
        self.assertFalse(fallback_guard["completion_claim_allowed"])

    def test_search_console_other_api_operations_allow_collection_without_completion(self) -> None:
        for operation in (
            "sites-get",
            "search-analytics-query",
            "sitemaps-list",
            "sitemaps-get",
        ):
            with self.subTest(operation=operation):
                payload = self.run_resolver(
                    "--system",
                    "gsc",
                    "--intent",
                    "read",
                    "--required-operation",
                    operation,
                )
                guard = payload["route_guard"]
                self.assertTrue(guard["api_supports_required_operation"])
                self.assertFalse(guard["api_completion_receipt_supported"])
                self.assertFalse(guard["completion_claim_allowed"])
                self.assertEqual(
                    guard["decision"], "allow_api_evidence_collection"
                )
                self.assertIsNone(guard["blocker_code"])
                self.assertTrue(guard["allowed"])

    def test_notion_work_write_requires_registered_exact_operation(self) -> None:
        missing = self.run_resolver_result(
            "--system",
            "notion",
            "--intent",
            "write",
            "--context",
            "work",
        )
        self.assertEqual(missing.returncode, 3, missing.stdout + missing.stderr)
        missing_payload = json.loads(missing.stdout)
        self.assertTrue(missing_payload["operation_contract"]["unknown_mutation"])
        self.assertEqual(
            missing_payload["execution_guard"]["blocker_code"],
            "blocked_unknown_mutation_operation",
        )

        result = self.run_resolver_result(
            "--system",
            "notion",
            "--intent",
            "write",
            "--context",
            "work",
            "--required-operation",
            "page-create",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["preferred_lane"]["route_id"], 'notion-company-alpha')
        self.assertEqual(payload["preferred_lane"]["probe_id"], 'notion-company-alpha-probe')
        self.assertEqual(payload["operation_contract"]["classification"], "native_supported")
        readiness = payload["mutation_readiness"]
        self.assertEqual(readiness["state"], "ready")
        self.assertTrue(readiness["operation_evidence_suitable"])
        self.assertEqual(
            readiness["authoritative_passed"], not readiness["slo_breached"]
        )
        self.assertTrue(payload["execution_guard"]["allowed"])
        self.assertIsNone(payload["execution_guard"]["blocker_code"])
        self.assertFalse(payload["mutation_readiness"]["execution_prerequisite"])

    def test_unknown_mutation_cannot_be_self_attested_supported(self) -> None:
        for extra_args in (
            ("--native-operation-support", "supported"),
            ("--candidate-lane", "browser", "--user-requested-ui-state"),
        ):
            with self.subTest(extra_args=extra_args):
                result = self.run_resolver_result(
                    "--system",
                    "notion",
                    "--intent",
                    "write",
                    "--context",
                    "work",
                    "--required-operation",
                    "invented-provider-write",
                    *extra_args,
                )
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertTrue(payload["operation_contract"]["unknown_mutation"])
                self.assertFalse(payload["operation_contract"]["operation_registered"])
                self.assertFalse(payload["browser_fallback_gate"]["allowed"])
                self.assertEqual(
                    payload["execution_guard"]["blocker_code"],
                    "blocked_unknown_mutation_operation",
                )

    def test_personal_trello_card_lifecycle_uses_existing_native_route(self) -> None:
        for operation, intent in (
            ("card-create", "write"),
            ("card-read", "read"),
            ("card-delete", "write"),
        ):
            with self.subTest(operation=operation):
                result = self.run_resolver_result(
                    "--system", "trello",
                    "--intent", intent,
                    "--required-operation", operation,
                    "--portfolio", "personal",
                    "--principal", 'operator',
                    "--account", _operator_binding('identifiers.accounts.personal_google'),
                    auto_bind=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                lane = payload["preferred_lane"]
                self.assertEqual(lane["route_id"], "trello-personal-api")
                self.assertEqual(lane["native_lane_kind"], "supported_cli")
                self.assertEqual(lane["credential_handle_ids"], ["trello.personal.api"])
                self.assertIsNone(lane["provider_adapter"])
                self.assertEqual(
                    payload["route_binding"]["required_account"],
                    _operator_binding('identifiers.accounts.personal_google'),
                )
                self.assertTrue(payload["route_binding"]["binding_complete"])
                self.assertEqual(
                    payload["operation_contract"]["classification"], "native_supported"
                )
                self.assertEqual(
                    payload["execution_guard"]["candidate_lane"], "declared_native"
                )
                self.assertTrue(payload["execution_guard"]["allowed"])
                self.assertFalse(payload["exact_probe_execution"]["attempted"])
                self.assertFalse(payload["route_readiness"]["execution_prerequisite"])
                self.assertFalse(payload["mutation_readiness"]["execution_prerequisite"])
                self.assertFalse(payload["browser_fallback_gate"]["allowed"])

    def test_trello_card_lifecycle_does_not_substitute_the_personal_account(self) -> None:
        result = self.run_resolver_result(
            "--system", "trello",
            "--intent", "write",
            "--required-operation", "card-create",
            "--principal", 'operator',
            "--account", _operator_binding('identifiers.accounts.company_alpha_operator_google'),
            auto_bind=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsNone(payload["preferred_lane"])
        self.assertEqual(
            payload["route_selection_blocker"], "blocked_no_exact_account_route"
        )
        self.assertFalse(payload["execution_guard"]["allowed"])

    def test_personal_calendar_default_preserves_explicit_google_selection(self) -> None:
        cases = (
            ("calendar", (), "apple-calendar-local"),
            ("apple-calendar", (), "apple-calendar-local"),
            ("google-calendar", (), "google-calendar-personal-gog"),
            ("gcal", (), "google-calendar-personal-gog"),
            (
                "calendar",
                ("--account", _operator_binding('identifiers.accounts.personal_google')),
                "google-calendar-personal-gog",
            ),
            (
                "calendar",
                ("--workspace", "google-personal"),
                "google-calendar-personal-gog",
            ),
        )
        for system, selectors, expected in cases:
            with self.subTest(system=system, selectors=selectors):
                result = self.run_resolver_result(
                    "--system", system,
                    "--intent", "read",
                    "--required-operation", "event-list",
                    "--portfolio", "personal",
                    *selectors,
                    auto_bind=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["preferred_lane"]["route_id"], expected)

    def test_calendar_routes_are_exact_and_google_event_create_is_native(self) -> None:
        common_google_args = (
            "--system",
            "google-calendar",
            "--intent",
            "create",
            "--context",
            "personal",
            "--required-operation",
            "event-create",
            "--principal",
            'operator',
            "--account",
            _operator_binding('identifiers.accounts.personal_google'),
        )
        google_result = self.run_resolver_result(*common_google_args)
        self.assertEqual(
            google_result.returncode, 0, google_result.stdout + google_result.stderr
        )
        google = json.loads(google_result.stdout)
        self.assertEqual(
            google["preferred_lane"]["route_id"], "google-calendar-personal-gog"
        )
        self.assertEqual(
            google["preferred_lane"]["probe_id"],
            "google-calendar-personal-gog-probe",
        )
        self.assertIn(
            '--account personal_google@example.invalid',
            google["preferred_lane"]["probe_command"],
        )
        self.assertIn("--with-meet", google["preferred_lane"]["route_notes"])
        self.assertEqual(google["preferred_lane"]["native_lane_kind"], "supported_cli")
        self.assertEqual(google["operation_contract"]["classification"], "native_supported")
        self.assertEqual(google["preferred_lane"]["readiness"]["state"], "ready")
        self.assertTrue(google["route_binding"]["binding_complete"])
        self.assertTrue(
            google["mutation_readiness"]["operation_evidence_suitable"]
        )
        self.assertEqual(
            google["mutation_readiness"]["authoritative_passed"],
            not google["mutation_readiness"]["slo_breached"],
        )
        self.assertTrue(google["execution_guard"]["allowed"])
        self.assertIsNone(google["execution_guard"]["blocker_code"])
        self.assertFalse(google["mutation_readiness"]["execution_prerequisite"])
        self.assertFalse(google["browser_fallback_gate"]["allowed"])

        caller_probe_hint = self.run_resolver_result(
            *common_google_args,
            "--api-probe-state",
            "passed",
            "--api-probe-account",
            _operator_binding('identifiers.accounts.personal_google'),
        )
        self.assertEqual(
            caller_probe_hint.returncode,
            0,
            caller_probe_hint.stdout + caller_probe_hint.stderr,
        )
        hinted = json.loads(caller_probe_hint.stdout)
        self.assertFalse(hinted["mutation_readiness"]["caller_probe_state_authoritative"])
        self.assertTrue(hinted["execution_guard"]["allowed"])
        self.assertIsNone(hinted["execution_guard"]["blocker_code"])

        browser_attempt = self.run_resolver_result(
            *common_google_args,
            "--candidate-lane",
            "browser",
            "--api-probe-state",
            "unavailable",
        )
        self.assertEqual(
            browser_attempt.returncode,
            3,
            browser_attempt.stdout + browser_attempt.stderr,
        )
        browser_payload = json.loads(browser_attempt.stdout)
        self.assertFalse(browser_payload["browser_fallback_gate"]["allowed"])
        self.assertEqual(
            browser_payload["execution_guard"]["blocker_code"],
            "blocked_browser_runtime_evidence_required",
        )

        apple_result = self.run_resolver_result(
            "--system",
            "calendar",
            "--intent",
            "create",
            "--context",
            "personal",
            "--required-operation",
            "event-create",
            "--principal",
            'operator',
            "--account",
            "local-apple-calendar",
        )
        self.assertEqual(
            apple_result.returncode,
            0,
            apple_result.stdout + apple_result.stderr,
        )
        apple = json.loads(apple_result.stdout)
        self.assertEqual(
            apple["systems_considered"], ["apple-calendar", "google-calendar"]
        )
        self.assertEqual(apple["preferred_lane"]["route_id"], "apple-calendar-local")
        self.assertEqual(
            apple["preferred_lane"]["readiness_status_id"],
            "Apple Calendar local read (Calendar app via AppleScript)",
        )
        self.assertIn(
            "apple_calendar_probe.py", apple["preferred_lane"]["probe_command"]
        )
        self.assertEqual(apple["operation_contract"]["classification"], "native_supported")
        self.assertFalse(apple["mutation_readiness"]["authoritative_passed"])
        self.assertTrue(apple["mutation_readiness"]["evidence_suitable"])
        self.assertTrue(apple["mutation_readiness"]["slo_breached"])
        self.assertTrue(apple["execution_guard"]["allowed"])
        self.assertIsNone(apple["execution_guard"]["blocker_code"])
        self.assertFalse(apple["mutation_readiness"]["execution_prerequisite"])

        exact_account_override = self.run_resolver_result(
            "--system",
            "google-calendar",
            "--intent",
            "create",
            "--context",
            "personal",
            "--portfolio",
            "personal",
            "--required-operation",
            "event-create",
            "--account",
            _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
            "--principal",
            'company-alpha',
            auto_bind=False,
        )
        self.assertEqual(
            exact_account_override.returncode,
            0,
            exact_account_override.stdout + exact_account_override.stderr,
        )
        override_payload = json.loads(exact_account_override.stdout)
        self.assertEqual(
            override_payload["preferred_lane"]["route_id"],
            'google-calendar-company-alpha-coordinator-gog',
        )
        self.assertEqual(
            override_payload["route_selection"]["mode"], "exact_account"
        )
        self.assertEqual(
            override_payload["route_selection"]["overridden_selectors"],
            ["portfolio", "context"],
        )
        self.assertFalse(
            override_payload["route_binding"]["caller_inputs_authoritative"]
        )
        self.assertTrue(override_payload["execution_guard"]["allowed"])

        conflicting_principal = self.run_resolver_result(
            "--system",
            "google-calendar",
            "--intent",
            "create",
            "--required-operation",
            "event-create",
            "--account",
            _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
            "--principal",
            'operator',
            auto_bind=False,
        )
        self.assertEqual(conflicting_principal.returncode, 2)
        conflict_payload = json.loads(conflicting_principal.stdout)
        self.assertEqual(conflict_payload["route_selection"]["state"], "conflict")
        self.assertEqual(
            conflict_payload["route_selection_blocker"],
            "blocked_conflicting_principal_selector",
        )

    def test_calendar_portfolio_selects_without_repeated_identity_and_wrong_principal_blocks(
        self,
    ) -> None:
        selected = self.run_resolver_result(
            "--system",
            "google-calendar",
            "--intent",
            "read",
            "--portfolio",
            "personal",
            "--required-operation",
            "event-list",
            auto_bind=False,
        )
        self.assertEqual(selected.returncode, 0, selected.stdout + selected.stderr)
        selected_payload = json.loads(selected.stdout)
        self.assertEqual(
            selected_payload["preferred_lane"]["route_id"],
            "google-calendar-personal-gog",
        )
        self.assertEqual(
            selected_payload["route_selection"]["mode"], "explicit_portfolio"
        )
        self.assertTrue(selected_payload["route_selection"]["selection_only"])
        self.assertFalse(selected_payload["route_binding"]["binding_complete"])
        self.assertTrue(
            selected_payload["route_binding"]["route_binding_complete"]
        )
        self.assertFalse(
            selected_payload["route_binding"]["identity_evidence_complete"]
        )
        self.assertFalse(
            selected_payload["route_binding"]["caller_inputs_authoritative"]
        )
        self.assertTrue(selected_payload["execution_guard"]["allowed"])

        wrong_principal = self.run_resolver_result(
            "--system",
            "google-calendar",
            "--intent",
            "read",
            "--portfolio",
            "personal",
            "--principal",
            'company-alpha',
            "--required-operation",
            "event-list",
            auto_bind=False,
        )
        self.assertEqual(
            wrong_principal.returncode,
            2,
            wrong_principal.stdout + wrong_principal.stderr,
        )
        wrong_payload = json.loads(wrong_principal.stdout)
        self.assertIsNone(wrong_payload["preferred_lane"])
        self.assertEqual(
            wrong_payload["route_selection"]["applied_selectors"],
            ["portfolio", "principal"],
        )
        self.assertEqual(
            wrong_payload["route_selection_blocker"],
            "blocked_no_exact_principal_route",
        )
        self.assertFalse(wrong_payload["execution_guard"]["allowed"])

    def test_route_selection_requires_an_exact_registered_context(self) -> None:
        for context, blocker in (
            ("", "blocked_ambiguous_route_context_required"),
            ("not-a-route", "blocked_no_exact_context_route"),
            ("please use my work account", "blocked_invalid_context_selector"),
        ):
            with self.subTest(context=context):
                result = self.run_resolver_result(
                    "--system",
                    "notion",
                    "--intent",
                    "read",
                    "--context",
                    context,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertIsNone(payload["preferred_lane"])
                self.assertEqual(payload["route_selection_blocker"], blocker)
                self.assertFalse(payload["execution_guard"]["allowed"])

    def test_forms_submit_and_jira_create_use_registered_exact_ui_routes(self) -> None:
        personal_forms = self.run_resolver(
            "--system",
            "google-forms",
            "--intent",
            "read",
            "--context",
            "personal",
            "--required-operation",
            "form-get",
        )
        self.assertEqual(
            personal_forms["preferred_lane"]["readiness"]["state"], "ready"
        )
        self.assertEqual(
            personal_forms["route_readiness"]["evidence_operations"],
            ["form-get"],
        )
        self.assertEqual(
            personal_forms["preferred_lane"]["probe_id"],
            "google-forms-personal-gog-probe",
        )
        unknown_readiness_mutation = self.run_resolver_result(
            "--system",
            "google-forms",
            "--intent",
            "create",
            "--context",
            "personal",
            "--required-operation",
            "form-create",
            "--principal",
            'operator',
            "--account",
            _operator_binding('identifiers.accounts.personal_google'),
        )
        self.assertEqual(
            unknown_readiness_mutation.returncode,
            0,
            unknown_readiness_mutation.stdout + unknown_readiness_mutation.stderr,
        )
        unknown_readiness_payload = json.loads(unknown_readiness_mutation.stdout)
        self.assertEqual(unknown_readiness_payload["mutation_readiness"]["state"], "degraded")
        self.assertTrue(unknown_readiness_payload["execution_guard"]["allowed"])
        self.assertFalse(
            unknown_readiness_payload["mutation_readiness"]["execution_prerequisite"]
        )

        for system, context, account, route_id, operation in (
            (
                "google-forms",
                'company-alpha',
                _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
                'google-forms-company-alpha-coordinator-gog',
                "form-response-submit",
            ),
            (
                "jira",
                'company-alpha',
                _operator_binding('services.jira.hostname'),
                'jira-company-alpha',
                "issue-create",
            ),
        ):
            with self.subTest(system=system):
                native = self.run_resolver_result(
                    "--system",
                    system,
                    "--intent",
                    "create",
                    "--context",
                    context,
                    "--required-operation",
                    operation,
                )
                self.assertEqual(native.returncode, 3, native.stdout + native.stderr)
                native_payload = json.loads(native.stdout)
                self.assertEqual(native_payload["preferred_lane"]["route_id"], route_id)
                if system == "google-forms":
                    self.assertEqual(
                        native_payload["preferred_lane"]["probe_id"],
                        'google-forms-company-alpha-coordinator-gog-probe',
                    )
                self.assertEqual(
                    native_payload["operation_contract"]["classification"],
                    "authenticated_ui_required",
                )
                self.assertEqual(
                    native_payload["execution_guard"]["blocker_code"],
                    "blocked_required_operation_requires_authenticated_ui",
                )

                browser_result = self.run_resolver_result(
                    "--system",
                    system,
                    "--intent",
                    "create",
                    "--context",
                    context,
                    "--required-operation",
                    operation,
                    "--candidate-lane",
                    "browser",
                    "--browser-account",
                    account,
                )
                self.assertEqual(
                    browser_result.returncode,
                    3,
                    browser_result.stdout + browser_result.stderr,
                )
                browser = json.loads(browser_result.stdout)
                self.assertFalse(browser["execution_guard"]["allowed"])
                self.assertFalse(browser["browser_fallback_gate"]["allowed"])
                self.assertEqual(
                    browser["browser_fallback_gate"]["decision"],
                    "require_runtime_verified_browser_route_and_account",
                )
                self.assertEqual(browser["route_guard"]["required_account"], account)
                self.assertFalse(browser["route_guard"]["browser_account_verified"])
                self.assertFalse(browser["route_guard"]["selected_lane_verified"])
                self.assertEqual(
                    browser["route_guard"]["blocker_code"],
                    (
                        "blocked_requested_jira_route_unverified"
                        if system == "jira"
                        else "blocked_requested_google_forms_route_unverified"
                    ),
                )
                self.assertFalse(browser["route_guard"]["completion_claim_allowed"])
                if system == "jira":
                    expected_digest = browser["preferred_lane"]["provider_adapter"][
                        "native_evidence"
                    ]["provider_account_id_sha256"]
                    self.assertEqual(
                        browser["route_guard"][
                            "required_provider_account_id_sha256"
                        ],
                        expected_digest,
                    )
                else:
                    self.assertIsNone(
                        browser["route_guard"].get(
                            "required_provider_account_id_sha256"
                        )
                    )

                misdeclared_intent = self.run_resolver_result(
                    "--system",
                    system,
                    "--intent",
                    "read",
                    "--context",
                    context,
                    "--required-operation",
                    operation,
                    "--candidate-lane",
                    "browser",
                    "--browser-account",
                    account,
                )
                self.assertEqual(
                    misdeclared_intent.returncode,
                    3,
                    misdeclared_intent.stdout + misdeclared_intent.stderr,
                )
                misdeclared_payload = json.loads(misdeclared_intent.stdout)
                self.assertTrue(
                    misdeclared_payload["operation_contract"][
                        "operation_declared_mutation"
                    ]
                )
                self.assertTrue(
                    misdeclared_payload["operation_contract"]["mutation_intent"]
                )
                self.assertFalse(
                    misdeclared_payload["execution_guard"]["allowed"]
                )

    def test_existing_chrome_uses_stock_extension_route_and_hashpack_keeps_exact_custody(self) -> None:
        chrome = self.run_resolver(
            "--system",
            "existing-chrome-session",
            "--intent",
            "read",
            "--context",
            "existing-user-session",
            "--required-operation",
            "existing-session-attach-probe",
            "--candidate-lane",
            "browser",
            "--browser-account",
            "profile=chrome",
        )
        self.assertEqual(
            chrome["preferred_lane"]["route_id"], "openclaw-browser-chrome-session"
        )
        self.assertEqual(chrome["preferred_lane"]["readiness"]["state"], "ready")
        self.assertEqual(
            chrome["browser_fallback_gate"]["browser_profile_order"],
            ["openclaw", "chrome"],
        )
        self.assertNotIn("act", chrome["preferred_lane"]["operations"])
        self.assertNotIn("capture", chrome["preferred_lane"]["operations"])

        custody = self.run_resolver(
            "--system",
            "hashpack",
            "--intent",
            "read",
            "--context",
            'company-alpha',
            "--required-operation",
            "credential-presence-probe",
        )
        self.assertEqual(custody["preferred_lane"]["required_account"], _operator_binding('services.wallets.hedera_testnet.account_id'))
        self.assertEqual(custody["operation_contract"]["classification"], "native_supported")

        unbound_ui = self.run_resolver_result(
            "--system",
            "hashpack",
            "--intent",
            "read",
            "--context",
            'company-alpha',
            "--required-operation",
            "wallet-ui-inspect",
            "--candidate-lane",
            "browser",
        )
        self.assertEqual(unbound_ui.returncode, 3, unbound_ui.stdout + unbound_ui.stderr)
        self.assertEqual(
            json.loads(unbound_ui.stdout)["route_guard"]["blocker_code"],
            "blocked_requested_hashpack_account_unverified",
        )

        bound_ui = self.run_resolver(
            "--system",
            "hashpack",
            "--intent",
            "read",
            "--context",
            'company-alpha',
            "--required-operation",
            "wallet-ui-inspect",
            "--candidate-lane",
            "browser",
            "--browser-account",
            _operator_binding('services.wallets.hedera_testnet.account_id'),
        )
        self.assertFalse(bound_ui["execution_guard"]["allowed"])
        self.assertEqual(
            bound_ui["execution_guard"]["blocker_code"],
            "blocked_browser_runtime_evidence_required",
        )
        self.assertFalse(bound_ui["route_guard"]["completion_claim_allowed"])

        signing = self.run_resolver_result(
            "--system",
            "hashpack",
            "--intent",
            "write",
            "--context",
            'company-alpha',
            "--required-operation",
            "wallet-sign",
        )
        self.assertEqual(signing.returncode, 3, signing.stdout + signing.stderr)
        signing_payload = json.loads(signing.stdout)
        self.assertTrue(signing_payload["operation_contract"]["unknown_mutation"])
        self.assertEqual(
            signing_payload["execution_guard"]["blocker_code"],
            "blocked_unknown_mutation_operation",
        )

    def test_readiness_uses_only_exact_status_id(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        exact = {
            "capability_id": "exact-status",
            "state": "ready",
            "last_verified_utc": "2026-08-14T00:00:00Z",
            "evidence": "exact",
        }
        misleading = {
            "capability_id": "other-status",
            "state": "degraded",
            "last_verified_utc": "2026-08-14T00:00:00Z",
            "evidence": "route-id account@example.com all matching prose",
        }
        readiness = resolver["readiness_for_route"](
            {
                "route_id": "route-id",
                "notes": "matching prose",
                "readiness_status_id": "exact-status",
            },
            {"exact-status": exact, "other-status": misleading},
        )
        self.assertEqual(readiness["capability_id"], "exact-status")
        self.assertEqual(readiness["state"], "ready")

    def test_resolver_projects_only_sorted_opaque_credential_handle_ids(self) -> None:
        payload = self.run_resolver(
            "--system",
            "google-calendar",
            "--intent",
            "read",
            "--context",
            "personal",
            "--required-operation",
            "calendar-list",
            "--principal",
            'operator',
            "--account",
            _operator_binding('identifiers.accounts.personal_google'),
        )

        preferred = payload["preferred_lane"]
        self.assertEqual(
            preferred["credential_handle_ids"],
            ["google.personal.oauth.gog", "google.personal.password"],
        )
        selected_checked_lane = next(
            lane
            for lane in payload["checked_lanes"]
            if lane["route_id"] == preferred["route_id"]
        )
        self.assertEqual(selected_checked_lane, preferred)
        for lane in payload["checked_lanes"]:
            self.assertEqual(
                lane["credential_handle_ids"],
                sorted(lane["credential_handle_ids"]),
            )
            self.assertNotIn("credential_ref_name", lane)

        def nested_keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value) | {
                    key
                    for child in value.values()
                    for key in nested_keys(child)
                }
            if isinstance(value, list):
                return {
                    key
                    for child in value
                    for key in nested_keys(child)
                }
            return set()

        self.assertTrue(
            {
                "credential_kind",
                "provisioning_state",
                "owner",
                "ref",
                "keys",
                "consumers",
                "browser_binding",
            }.isdisjoint(nested_keys(preferred))
        )

    def test_credential_provisioning_state_does_not_change_resolution(self) -> None:
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        variant_registry = json.loads(json.dumps(route_registry))
        target_route_id = 'google-gmail-company-alpha-operator'
        target_handles = [
            handle
            for handle in route_registry["credential_handles"]
            if any(
                consumer == {
                    "kind": "integration-route",
                    "consumer_id": target_route_id,
                }
                for consumer in handle["consumers"]
            )
        ]
        self.assertTrue(target_handles)
        for handle in variant_registry["credential_handles"]:
            if any(
                consumer == {
                    "kind": "integration-route",
                    "consumer_id": target_route_id,
                }
                for consumer in handle["consumers"]
            ):
                handle["provisioning_state"] = (
                    "provisioned"
                    if handle["provisioning_state"] == "unprovisioned"
                    else "unprovisioned"
                )

        probe_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        status_registry = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )

        def resolve_with(registry: dict) -> dict:
            resolver = runpy.run_path(str(RESOLVER))

            def fake_load_json(relative: str) -> dict:
                return {
                    "registry/integration_routes.json": registry,
                    "registry/probes.json": probe_registry,
                    "status/capability_status.json": status_registry,
                }[relative]

            resolver["resolve"].__globals__["load_json"] = fake_load_json
            return resolver["resolve"](
                "gmail",
                "read",
                required_operation="gmail-read",
                requested_principal='company-alpha',
                requested_account=_operator_binding('identifiers.accounts.company_alpha_operator_google'),
            )

        baseline = resolve_with(route_registry)
        provisioned = resolve_with(variant_registry)

        def without_dynamic_age(value):
            if isinstance(value, dict):
                return {
                    key: without_dynamic_age(item)
                    for key, item in value.items()
                    if key != "age_days"
                }
            if isinstance(value, list):
                return [without_dynamic_age(item) for item in value]
            return value

        for field in (
            "route_selection",
            "preferred_lane",
            "operation_contract",
            "route_readiness",
            "execution_guard",
            "browser_fallback_gate",
        ):
            self.assertEqual(
                without_dynamic_age(baseline[field]),
                without_dynamic_age(provisioned[field]),
                field,
            )

    def test_cloudflare_read_resolves_capability_probed_readonly_native_route(self) -> None:
        payload = self.run_resolver(
            "--system",
            "cloudflare",
            "--intent",
            "read",
            "--context",
            "site-operations",
            "--required-operation",
            "zone-read",
        )

        preferred = payload["preferred_lane"]
        self.assertEqual(
            preferred["route_id"], 'cloudflare-operator-readonly-api'
        )
        self.assertEqual(
            preferred["probe_id"], 'cloudflare-operator-readonly-probe'
        )
        self.assertEqual(preferred["capability_scope"], "read-preview-deploy")
        self.assertIn("cloudflare_capability_probe.py", preferred["probe_command"])
        self.assertEqual(
            payload["operation_contract"]["classification"], "native_supported"
        )
        self.assertTrue(payload["execution_guard"]["allowed"])
        self.assertIsNone(payload["execution_guard"]["blocker_code"])
        self.assertTrue(payload["route_readiness"]["diagnostic_only"])
        self.assertFalse(payload["route_readiness"]["execution_prerequisite"])
        self.assertIsNone(preferred["provider_adapter"])
        self.assertNotIn("route_guard", payload)
        self.assertNotIn("legacy_provider_guard", payload)

    def test_cloudflare_readonly_route_rejects_unregistered_write(self) -> None:
        result = self.run_resolver_result(
            "--system",
            "cloudflare",
            "--intent",
            "write",
            "--context",
            "site-operations",
        )
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["operation_contract"]["unknown_mutation"])
        self.assertEqual(
            payload["execution_guard"]["blocker_code"],
            "blocked_unknown_mutation_operation",
        )

    def test_cloudflare_route_declares_only_provider_native_preview_mutation(self) -> None:
        payload = self.run_resolver(
            "--system",
            "cloudflare",
            "--intent",
            "write",
            "--portfolio",
            "personal",
            "--required-operation",
            "pages-preview-deploy",
        )
        self.assertEqual(
            payload["preferred_lane"]["route_id"],
            'cloudflare-operator-readonly-api',
        )
        self.assertEqual(
            payload["operation_contract"]["classification"],
            "native_supported",
        )
        self.assertEqual(
            payload["operation_contract"]["operation_effect"], "mutation"
        )
        self.assertTrue(payload["execution_guard"]["allowed"])
        self.assertIn(
            "provider-native Wrangler",
            payload["preferred_lane"]["route_notes"],
        )
        self.assertNotIn(
            "cloudflare_capability_probe.py --operation",
            payload["preferred_lane"]["route_notes"],
        )
        self.assertEqual(
            payload["mutation_readiness"]["readiness_status_id"],
            "Mutation readiness — `cloudflare-personal-pages-preview`",
        )
        self.assertTrue(
            payload["mutation_readiness"]["operation_evidence_suitable"]
        )
        self.assertEqual(
            payload["mutation_readiness"]["authoritative_passed"],
            not payload["mutation_readiness"]["slo_breached"],
        )
        self.assertEqual(
            payload["mutation_readiness"]["evidence_operations"],
            ["pages-preview-deploy"],
        )

        for operation in (
            "pages-production-deploy",
            "dns-write",
            "domain-write",
            "project-config-write",
        ):
            with self.subTest(operation=operation):
                result = self.run_resolver_result(
                    "--system",
                    "cloudflare",
                    "--intent",
                    "write",
                    "--portfolio",
                    "personal",
                    "--required-operation",
                    operation,
                )
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                rejected = json.loads(result.stdout)
                self.assertTrue(rejected["operation_contract"]["unknown_mutation"])
                self.assertEqual(
                    rejected["execution_guard"]["blocker_code"],
                    "blocked_unknown_mutation_operation",
                )

    def test_company_alpha_local_dev_read_resolves_docker_route_and_readonly_probe(self) -> None:
        for alias in ('company-alpha-local-dev', 'company-alpha-local', 'company-alpha-dev', 'company-alpha-docker'):
            with self.subTest(alias=alias):
                payload = self.run_resolver(
                    "--system",
                    alias,
                    "--intent",
                    "read",
                    "--context",
                    'company-alpha',
                    "--required-operation",
                    "stack-status",
                )

                self.assertEqual(payload["preferred_lane"]["route_id"], 'company-alpha-local-dev-docker')
                self.assertEqual(payload["preferred_lane"]["probe_id"], 'company-alpha-local-dev-probe')
                self.assertEqual(payload["preferred_lane"]["probe_safe_lane"], "local-read")
                self.assertIn('company-alpha-local status', payload["preferred_lane"]["probe_command"])
                self.assertIn("up` starts local services", payload["preferred_lane"]["route_notes"])
                self.assertIn("volatile", payload["preferred_lane"]["readiness"]["freshness_class"])

    def test_company_alpha_docs_read_resolves_local_adapter_and_readonly_probe(self) -> None:
        for alias in ('company-alpha-docs', 'company-alpha-mcp', 'company-alpha-docs-mcp-readonly'):
            with self.subTest(alias=alias):
                payload = self.run_resolver(
                    "--system",
                    alias,
                    "--intent",
                    "read",
                    "--context",
                    'company-alpha',
                    "--required-operation",
                    "docs-read",
                )

                preferred = payload["preferred_lane"]
                self.assertEqual(preferred["route_id"], 'company-alpha-docs-mcp-readonly')
                self.assertEqual(preferred["probe_id"], 'company-alpha-docs-mcp-readonly-probe')
                self.assertEqual(preferred["probe_safe_lane"], "local-read-network-read")
                self.assertIn('company_alpha_docs_mcp_adapter.py', preferred["probe_command"])
                self.assertIn("--probe", preferred["probe_command"])
                self.assertIn("source policy remains live-inactive", preferred["route_notes"])
                self.assertIn("tracked separately", preferred["route_notes"])
                self.assertEqual(preferred["readiness"]["state"], "ready")

    def test_unknown_operations_never_execute_in_native_or_browser_lanes(self) -> None:
        cases = (
            (
                "unregistered-browser",
                (
                    "--system",
                    "unregistered-crm-admin",
                    "--intent",
                    "read",
                    "--context",
                    "any",
                    "--required-operation",
                    "delete-all",
                    "--candidate-lane",
                    "browser",
                ),
            ),
            (
                "registered-jira-browser",
                (
                    "--system",
                    "jira",
                    "--intent",
                    "read",
                    "--context",
                    'company-alpha',
                    "--required-operation",
                    "issue-delete",
                    "--candidate-lane",
                    "browser",
                    "--browser-account",
                    _operator_binding('services.jira.hostname'),
                    "--user-requested-ui-state",
                ),
            ),
            (
                "registered-gmail-browser",
                (
                    "--system",
                    "gmail",
                    "--intent",
                    "read",
                    "--context",
                    'operator-company-alpha',
                    "--required-operation",
                    "gmail-send",
                    "--candidate-lane",
                    "browser",
                    "--browser-account",
                    _operator_binding('identifiers.accounts.company_alpha_operator_google'),
                    "--user-requested-ui-state",
                ),
            ),
        )
        for name, arguments in cases:
            with self.subTest(name=name):
                result = self.run_resolver_result(*arguments)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertFalse(payload["operation_contract"]["operation_registered"])
                self.assertFalse(payload["execution_guard"]["allowed"])

    def test_route_operation_effect_cannot_be_downgraded_by_caller_intent(self) -> None:
        cases = (
            ("google-calendar", "personal", "event-delete"),
            ("digitalocean", "openclaw", "app-deploy"),
            ("hedera-mainnet-test-signer", "hedera-mainnet", "transaction-sign"),
        )
        for system, context, operation in cases:
            with self.subTest(system=system, operation=operation):
                result = self.run_resolver_result(
                    "--system",
                    system,
                    "--intent",
                    "read",
                    "--context",
                    context,
                    "--required-operation",
                    operation,
                    "--candidate-lane",
                    "declared_native",
                )
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(
                    payload["operation_contract"]["operation_effect"], "mutation"
                )
                self.assertTrue(payload["operation_contract"]["mutation_intent"])
                self.assertFalse(payload["execution_guard"]["allowed"])
                self.assertEqual(
                    payload["execution_guard"]["blocker_code"],
                    "blocked_operation_effect_intent_mismatch",
                )

    def test_unambiguous_native_read_uses_route_identity_while_readiness_is_diagnostic(
        self,
    ) -> None:
        route_bound = self.run_resolver_result(
            "--system",
            "notion",
            "--intent",
            "read",
            "--context",
            "personal",
            "--required-operation",
            "page-read",
            auto_bind=False,
        )
        self.assertEqual(
            route_bound.returncode,
            0,
            route_bound.stdout + route_bound.stderr,
        )
        route_bound_payload = json.loads(route_bound.stdout)
        self.assertTrue(route_bound_payload["execution_guard"]["allowed"])
        self.assertIsNone(route_bound_payload["execution_guard"]["blocker_code"])
        self.assertFalse(route_bound_payload["route_binding"]["binding_complete"])
        self.assertTrue(
            route_bound_payload["route_binding"]["route_binding_complete"]
        )
        self.assertFalse(
            route_bound_payload["route_binding"]["identity_evidence_complete"]
        )
        self.assertFalse(
            route_bound_payload["route_binding"]["caller_inputs_authoritative"]
        )
        self.assertTrue(
            route_bound_payload["route_binding"]["selection_inputs_consistent"]
        )

        diagnostic = self.run_resolver_result(
            "--system",
            "notion",
            "--intent",
            "read",
            "--context",
            "personal",
            "--required-operation",
            "page-read",
        )
        self.assertEqual(
            diagnostic.returncode,
            0,
            diagnostic.stdout + diagnostic.stderr,
        )
        diagnostic_payload = json.loads(diagnostic.stdout)
        self.assertTrue(diagnostic_payload["execution_guard"]["allowed"])
        self.assertIsNone(
            diagnostic_payload["execution_guard"]["blocker_code"]
        )
        self.assertTrue(
            diagnostic_payload["route_readiness"]["diagnostic_only"]
        )
        self.assertFalse(
            diagnostic_payload["route_readiness"]["execution_prerequisite"]
        )

        resolver = runpy.run_path(str(RESOLVER))
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        probe_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        row = next(
            item
            for item in status["capabilities"]
            if item["capability_id"]
            == "Notion API route — personal workspace (`NOTION_API_KEY`)"
        )
        row.update(
            {
                "state": "degraded",
                "last_verified_utc": "2000-01-01T00:00:00Z",
                "evidence_effects": ["read"],
                "evidence_operations": ["page-read"],
            }
        )

        def fake_load_json(relative: str) -> dict:
            return {
                "registry/integration_routes.json": route_registry,
                "registry/probes.json": probe_registry,
                "status/capability_status.json": status,
            }[relative]

        resolver["resolve"].__globals__["load_json"] = fake_load_json
        stale = resolver["resolve"](
            "notion",
            "read",
            "personal",
            required_operation="page-read",
            requested_principal='operator',
            requested_account='operator-personal-notion',
        )
        self.assertFalse(stale["route_readiness"]["authoritative_passed"])
        self.assertTrue(stale["route_readiness"]["diagnostic_only"])
        self.assertFalse(stale["route_readiness"]["execution_prerequisite"])
        self.assertTrue(stale["execution_guard"]["allowed"])

        fresh_time = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        row.update(
            {
                "state": "ready",
                "last_verified_utc": fresh_time,
            }
        )
        ready = resolver["resolve"](
            "notion",
            "read",
            "personal",
            required_operation="page-read",
            requested_principal='operator',
            requested_account='operator-personal-notion',
        )
        self.assertTrue(ready["route_readiness"]["authoritative_passed"])
        self.assertTrue(ready["execution_guard"]["allowed"])
        self.assertIsNone(ready["execution_guard"]["blocker_code"])

    def test_native_read_ignores_noncanonical_diagnostic_timestamps(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        probe_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        row = next(
            item
            for item in status["capabilities"]
            if item["capability_id"] == "Google Calendar read via `gog` CLI"
        )
        def fake_load_json(relative: str) -> dict:
            return {
                "registry/integration_routes.json": route_registry,
                "registry/probes.json": probe_registry,
                "status/capability_status.json": status,
            }[relative]

        resolver["resolve"].__globals__["load_json"] = fake_load_json
        invalid_timestamps = (
            (datetime.now(timezone.utc) - timedelta(minutes=30))
            .replace(microsecond=0, tzinfo=None)
            .isoformat(),
            "2026-08-14Q23:41Z",
            "2026-08-14T23:41Z",
            "2026-08-14\x0023:41:00Z",
            "2026-08-14\u200d23:41:00Z",
            "2026-08-14/23:41:00Z",
            "2026-08-14 23:41:00Z",
            "2026-08-14T23:41:00,5Z",
        )
        for timestamp in invalid_timestamps:
            with self.subTest(timestamp=repr(timestamp)):
                row.update(
                    {
                        "state": "ready",
                        "last_verified_utc": timestamp,
                    }
                )
                payload = resolver["resolve"](
                    "google-calendar",
                    "read",
                    "personal",
                    required_operation="event-list",
                    requested_principal='operator',
                    requested_account=_operator_binding('identifiers.accounts.personal_google'),
                )
                self.assertTrue(payload["execution_guard"]["allowed"])
                self.assertEqual(payload["route_readiness"]["state"], "unknown")
                self.assertFalse(
                    payload["route_readiness"]["authoritative_passed"]
                )

    def test_browser_caller_flags_never_authorize_unverified_runtime_lane(self) -> None:
        result = self.run_resolver_result(
            "--system",
            "gmail",
            "--intent",
            "read",
            "--context",
            'company-alpha-coordinator',
            "--required-operation",
            "gmail-read",
            "--candidate-lane",
            "browser",
            "--api-probe-state",
            "unavailable",
            "--browser-account",
            _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
            "--authenticated-ui-required",
        )
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["execution_guard"]["blocker_code"],
            "blocked_browser_runtime_evidence_required",
        )
        self.assertFalse(payload["browser_fallback_gate"]["allowed"])
        self.assertFalse(
            payload["browser_fallback_gate"]["caller_route_inputs_authoritative"]
        )
        self.assertFalse(payload["route_guard"]["route_verified"])
        self.assertFalse(payload["route_guard"]["browser_account_verified"])
        self.assertFalse(payload["route_guard"]["selected_lane_verified"])

    def test_ui_only_routes_allow_evidence_collection_without_claiming_completion(
        self,
    ) -> None:
        cases = (
            (
                "openclaw-browser-chrome-session",
                (
                    "--system", "existing-chrome-session",
                    "--intent", "read",
                    "--required-operation", "existing-session-attach-probe",
                    "--principal", 'operator',
                    "--account", "profile=chrome",
                    "--browser-account", "profile=chrome",
                ),
            ),
        )
        for route_id, args in cases:
            with self.subTest(route_id=route_id):
                result = self.run_resolver_result(
                    *args,
                    "--candidate-lane", "browser",
                    auto_bind=False,
                )
                self.assertEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )
                payload = json.loads(result.stdout)
                self.assertEqual(payload["preferred_lane"]["route_id"], route_id)
                gate = payload["browser_fallback_gate"]
                self.assertTrue(gate["allowed"])
                self.assertEqual(
                    gate["decision"], "allow_browser_evidence_collection"
                )
                self.assertEqual(gate["scope"], "evidence_collection_only")
                self.assertFalse(gate["provider_operation_allowed"])
                self.assertFalse(gate["account_identity_verified"])
                self.assertFalse(gate["completion_claim_allowed"])
                self.assertEqual(
                    gate["browser_profile_order"], ["openclaw", "chrome"]
                )
                self.assertEqual(
                    gate["automatic_browser_profile_fallbacks"], ["chrome"]
                )
                self.assertEqual(gate["attended_browser_profile_fallbacks"], [])
                self.assertFalse(payload["execution_guard"]["allowed"])
                self.assertEqual(
                    payload["execution_guard"]["blocker_code"],
                    "blocked_browser_runtime_evidence_required",
                )
                route_guard = payload.get("route_guard")
                if isinstance(route_guard, dict):
                    self.assertFalse(route_guard["browser_account_verified"])
                    self.assertFalse(route_guard["selected_lane_verified"])
                    self.assertFalse(route_guard["completion_claim_allowed"])

        retired_profile = self.run_resolver_result(
            "--system", "existing-chrome-session",
            "--intent", "read",
            "--required-operation", "existing-session-attach-probe",
            "--principal", 'operator',
            "--account", "profile=user",
            "--candidate-lane", "browser",
            "--browser-account", "profile=user",
            auto_bind=False,
        )
        self.assertEqual(
            retired_profile.returncode,
            2,
            retired_profile.stdout + retired_profile.stderr,
        )
        self.assertFalse(
            json.loads(retired_profile.stdout)["browser_fallback_gate"]["allowed"]
        )

        mismatched_account = self.run_resolver_result(
            "--system", "gmail",
            "--intent", "read",
            "--required-operation", "gmail-read",
            "--principal", 'company-alpha',
            "--account", _operator_binding('identifiers.accounts.company_alpha_operator_google'),
            "--candidate-lane", "browser",
            "--browser-account", _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
            auto_bind=False,
        )
        self.assertEqual(
            mismatched_account.returncode,
            3,
            mismatched_account.stdout + mismatched_account.stderr,
        )
        self.assertFalse(
            json.loads(mismatched_account.stdout)["browser_fallback_gate"]["allowed"]
        )

    def test_hybrid_google_routes_keep_browser_only_surfaces_runtime_verified(
        self,
    ) -> None:
        cases = (
            (
                'google-workspace-company-beta-admin',
                "google-workspace",
                "read",
                "admin-console-read",
                'company-beta',
                _operator_binding('identifiers.accounts.company_beta_admin_google'),
            ),
            (
                'google-workspace-company-beta-admin',
                "google-workspace",
                "write",
                "admin-console-write",
                'company-beta',
                _operator_binding('identifiers.accounts.company_beta_admin_google'),
            ),
            (
                'google-workspace-company-beta-admin',
                "google-workspace",
                "write",
                "workspace-domain-settings",
                'company-beta',
                _operator_binding('identifiers.accounts.company_beta_admin_google'),
            ),
            (
                'google-workspace-company-beta-operator',
                "google-workspace",
                "read",
                "workspace-read",
                'company-beta',
                _operator_binding('identifiers.accounts.company_beta_operator_google'),
            ),
            (
                'google-gmail-company-alpha-operator',
                "gmail",
                "write",
                "gmail-draft",
                'company-alpha',
                _operator_binding('identifiers.accounts.company_alpha_operator_google'),
            ),
        )
        for route_id, system, intent, operation, principal, account in cases:
            with self.subTest(route_id=route_id, operation=operation):
                result = self.run_resolver_result(
                    "--system",
                    system,
                    "--intent",
                    intent,
                    "--required-operation",
                    operation,
                    "--principal",
                    principal,
                    "--account",
                    account,
                    "--browser-account",
                    account,
                    "--candidate-lane",
                    "browser",
                    auto_bind=False,
                )
                self.assertEqual(
                    result.returncode, 3, result.stdout + result.stderr
                )
                payload = json.loads(result.stdout)
                self.assertEqual(payload["preferred_lane"]["route_id"], route_id)
                self.assertEqual(
                    payload["operation_contract"]["classification"],
                    "authenticated_ui_required",
                )
                gate = payload["browser_fallback_gate"]
                self.assertTrue(gate["eligible"])
                self.assertFalse(gate["allowed"])
                self.assertFalse(gate["provider_operation_allowed"])
                self.assertFalse(gate["completion_claim_allowed"])
                self.assertEqual(
                    payload["execution_guard"]["blocker_code"],
                    "blocked_browser_runtime_evidence_required",
                )

    def test_native_cli_uses_exact_binding_without_treating_caller_probe_hints_as_evidence(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        probe_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        row = next(
            item
            for item in status["capabilities"]
            if item["capability_id"]
            == 'Gmail mutation readiness — personal `personal_google@example.invalid` via `gog`'
        )
        row.update(
            {
                "state": "ready",
                "last_verified_utc": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "evidence_effects": ["mutation"],
                "evidence_operations": ["gmail-send"],
            }
        )

        def fake_load_json(relative: str) -> dict:
            return {
                "registry/integration_routes.json": route_registry,
                "registry/probes.json": probe_registry,
                "status/capability_status.json": status,
            }[relative]

        resolver["resolve"].__globals__["load_json"] = fake_load_json

        def forbidden_transport(_argv: tuple[str, ...], _timeout: int):
            self.fail("caller declarations must not invoke a probe transport")

        cli_args = [
            "--system",
            "gmail",
            "--intent",
            "send",
            "--context",
            "personal",
            "--required-operation",
            "gmail-send",
            "--principal",
            'operator',
            "--account",
            _operator_binding('identifiers.accounts.personal_google'),
        ]
        for caller_probe_args in (
            [],
            [
                "--api-probe-state",
                "passed",
                "--api-probe-account",
                _operator_binding('identifiers.accounts.personal_google'),
            ],
        ):
            with self.subTest(caller_probe_args=caller_probe_args):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    returncode = resolver["main"](
                        cli_args + caller_probe_args,
                        probe_transport=forbidden_transport,
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(returncode, 0)
                self.assertTrue(payload["execution_guard"]["allowed"])
                self.assertIsNone(payload["execution_guard"]["blocker_code"])
                self.assertTrue(payload["route_guard"]["allowed"])
                self.assertFalse(payload["route_guard"]["route_verified"])
                self.assertFalse(
                    payload["route_guard"]["api_probe_claims_are_authoritative"]
                )
                gate = payload["browser_fallback_gate"]
                self.assertFalse(gate["eligible"])
                self.assertFalse(gate["allowed"])
                self.assertEqual(gate["matched_predicates"], [])
                self.assertEqual(gate["decision"], "stay_on_declared_native_route")

    def test_exact_probe_cli_executes_bound_argv_and_authorizes_only_its_operation(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        calls: list[tuple[tuple[str, ...], int]] = []

        def transport(argv: tuple[str, ...], timeout: int):
            calls.append((argv, timeout))
            return result_type(
                returncode=0,
                stdout=json.dumps(
                    {
                        "calendars": [
                            {
                                "id": _operator_binding('identifiers.accounts.personal_google'),
                                "primary": True,
                                "accessRole": "owner",
                            }
                        ]
                    }
                ),
            )

        base_args = [
            "--system",
            "google-calendar",
            "--intent",
            "read",
            "--context",
            "personal",
            "--required-operation",
            "calendar-list",
            "--principal",
            'operator',
            "--account",
            _operator_binding('identifiers.accounts.personal_google'),
        ]

        def invoke(args: list[str]) -> tuple[int, dict]:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                returncode = resolver["main"](args, probe_transport=transport)
            return returncode, json.loads(stdout.getvalue())

        unprobed_code, unprobed = invoke(base_args)
        self.assertEqual(unprobed_code, 0)
        self.assertEqual(calls, [])
        self.assertTrue(unprobed["execution_guard"]["allowed"])
        self.assertEqual(unprobed["route_readiness"]["state"], "ready")
        self.assertEqual(
            unprobed["route_readiness"]["authoritative_passed"],
            not unprobed["route_readiness"]["slo_breached"],
        )
        self.assertFalse(unprobed["route_readiness"]["execution_prerequisite"])

        allowed_code, allowed = invoke(base_args + ["--run-exact-probe"])
        self.assertEqual(allowed_code, 0)
        self.assertEqual(
            calls,
            [
                (
                    (
                        _operator_binding('paths.gog_binary'),
                        "calendar",
                        "calendars",
                        "--account",
                        _operator_binding('identifiers.accounts.personal_google'),
                        "--json",
                        "--max",
                        "250",
                        "--all",
                        "--no-input",
                    ),
                    60,
                )
            ],
        )
        self.assertTrue(allowed["execution_guard"]["allowed"])
        self.assertTrue(allowed["authoritative_probe_evidence"]["valid"])
        self.assertTrue(allowed["route_readiness"]["live_probe_authoritative_passed"])
        self.assertEqual(allowed["exact_probe_execution"]["result"], "passed")

        cross_operation_args = [
            "event-list" if value == "calendar-list" else value
            for value in base_args
        ]
        cross_code, cross_operation = invoke(
            cross_operation_args + ["--run-exact-probe"]
        )
        self.assertEqual(cross_code, 0)
        self.assertTrue(cross_operation["execution_guard"]["allowed"])
        self.assertFalse(
            cross_operation["route_readiness"]["live_probe_authoritative_passed"]
        )
        self.assertIsNone(cross_operation["execution_guard"]["blocker_code"])

    def test_search_console_exact_probe_uses_registered_argv_without_authority_inflation(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        calls: list[tuple[tuple[str, ...], int]] = []
        signal = self.search_console_ready_signal() | {
            "matching_property_count": 3,
            "authorized_matching_property_count": 2,
            "authorized_property_types": ["domain", "url-prefix"],
        }

        def transport(argv: tuple[str, ...], timeout: int):
            calls.append((argv, timeout))
            return result_type(returncode=0, stdout=json.dumps(signal))

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            returncode = resolver["main"](
                [
                    "--system",
                    "google-search-console",
                    "--intent",
                    "read",
                    "--context",
                    "personal",
                    "--required-operation",
                    "sites-list",
                    "--principal",
                    'operator',
                    "--account",
                    _operator_binding('identifiers.accounts.personal_google'),
                    "--run-exact-probe",
                ],
                probe_transport=transport,
            )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(returncode, 0)
        self.assertEqual(
            calls,
            [
                (
                    (
                        _operator_binding('paths.python_binary'),
                        "-E",
                        "-s",
                        "scripts/google_search_console_probe.py",
                        "--account",
                        _operator_binding('identifiers.accounts.personal_google'),
                        "--receipt-path",
                        "artifacts/CapabilityReceipts/google-search-console-personal.json",
                        "--json",
                    ),
                    60,
                )
            ],
        )
        self.assertEqual(payload["exact_probe_execution"]["result"], "passed")
        self.assertEqual(
            payload["authoritative_probe_evidence"]["evidence_effects"],
            ["read"],
        )
        self.assertEqual(
            payload["authoritative_probe_evidence"]["evidence_operations"],
            ["sites-list"],
        )
        self.assertTrue(
            payload["route_readiness"]["live_probe_authoritative_passed"]
        )
        self.assertEqual(
            payload["route_guard"]["api_probe_receipt_authority"], "none"
        )
        self.assertFalse(payload["route_guard"]["completion_claim_allowed"])
        self.assertFalse(
            payload["route_guard"]["provider_example_evidence_verified"]
        )

    def test_search_console_exact_probe_rejects_command_and_signal_mismatch(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        payload = self.run_resolver(
            "--system",
            "google-search-console",
            "--intent",
            "read",
            "--context",
            "personal",
            "--required-operation",
            "sites-list",
        )
        preferred = payload["preferred_lane"]
        signal = self.search_console_ready_signal()

        self.assertEqual(
            resolver["exact_probe_argv"](preferred),
            resolver["GOOGLE_SEARCH_CONSOLE_PERSONAL_PROBE_ARGV"],
        )
        drifted_command = dict(preferred)
        drifted_command["probe_command"] += " --property-domain example.com"
        self.assertIsNone(resolver["exact_probe_argv"](drifted_command))
        self.assertEqual(
            resolver["parse_exact_probe_signal"](
                preferred, json.dumps(signal)
            ),
            (("read",), ("sites-list",)),
        )
        self.assertIsNone(
            resolver["parse_exact_probe_signal"](preferred, "not-json")
        )
        self.assertIsNone(
            resolver["parse_exact_probe_signal"](
                preferred,
                json.dumps(signal | {"account": "someone@example.com"}),
            )
        )
        for inflated in (
            {"authoritative": True},
            {"completion_claim_allowed": True},
            {"page_indexing_example_table_verified": True},
            {"property_uri": "sc-domain:example.com"},
        ):
            with self.subTest(inflated=inflated):
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred, json.dumps(signal | inflated)
                    )
                )
        self.assertIsNone(
            resolver["parse_exact_probe_signal"](
                preferred, json.dumps(signal), "unexpected stderr"
            )
        )

    def test_gog_oauth_probe_executes_fixed_auth_list_and_binds_exact_account(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        calls: list[tuple[tuple[str, ...], int]] = []
        required_account = _operator_binding('identifiers.accounts.personal_google')
        valid_account = {
            "email": required_account,
            "auth": "oauth",
            "client": "default",
            "created_at": "2026-08-30T12:00:00Z",
            "scopes": ["scope-a", "scope-b"],
            "services": ["drive", "gmail"],
            "subject": required_account,
            "valid": True,
        }
        probe_payload = {
            "accounts": [
                valid_account,
                valid_account
                | {
                    "email": _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
                    "subject": _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
                },
            ]
        }

        def transport(argv: tuple[str, ...], timeout: int):
            calls.append((argv, timeout))
            return result_type(returncode=0, stdout=json.dumps(probe_payload))

        args = [
            "--system",
            "google-workspace",
            "--intent",
            "read",
            "--context",
            "personal",
            "--required-operation",
            "gmail-read",
            "--principal",
            'operator',
            "--account",
            required_account,
            "--run-exact-probe",
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            returncode = resolver["main"](args, probe_transport=transport)
        payload = json.loads(stdout.getvalue())

        self.assertEqual(returncode, 0)
        self.assertEqual(
            calls,
            [
                (
                    (
                        _operator_binding('paths.gog_binary'),
                        "auth",
                        "list",
                        "--check",
                        "--json",
                    ),
                    60,
                )
            ],
        )
        self.assertTrue(payload["execution_guard"]["allowed"])
        self.assertEqual(payload["exact_probe_execution"]["result"], "passed")
        self.assertTrue(payload["authoritative_probe_evidence"]["valid"])
        self.assertEqual(
            payload["authoritative_probe_evidence"]["evidence_operations"],
            ["oauth-identity-read"],
        )
        self.assertFalse(
            payload["route_readiness"]["live_probe_authoritative_passed"]
        )

        preferred = payload["preferred_lane"]
        drifted_command = dict(preferred)
        drifted_command["probe_command"] += " --account unexpected@example.com"
        self.assertIsNone(resolver["exact_probe_argv"](drifted_command))
        for invalid_payload in (
            {"accounts": [valid_account | {"valid": False}]},
            {"accounts": [valid_account, valid_account]},
            {"accounts": [valid_account | {"auth": "password"}]},
            {
                "accounts": [
                    valid_account
                    | {
                        "email": "someone@example.com",
                        "subject": "someone@example.com",
                    }
                ]
            },
        ):
            with self.subTest(invalid_payload=invalid_payload):
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(invalid_payload),
                    )
                )

    def test_exact_jira_probe_cli_uses_sealed_helper_and_strict_success_signal(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        calls: list[tuple[tuple[str, ...], int]] = []
        signal = {
            "route": 'jira-company-alpha',
            "system": "jira",
            "credential_handle_id": 'jira.company_alpha.api',
            "site_expected": _operator_binding('services.jira.site'),
            "project_key_expected": _operator_binding('services.jira.project_key'),
            "issue_type_expected": "QA Feedback",
            "credential_contract_bound": True,
            "site_matches_expected": True,
            "auth_ok": True,
            "account_identity_matches_registered": True,
            "account_type": "atlassian",
            "project_ok": True,
            "project_id": "10000",
            "project_key": _operator_binding('services.jira.project_key'),
            "project_name": 'CompanyAlpha',
            "createmeta_ok": True,
            "qa_feedback_issue_type_ok": True,
            "qa_feedback_issue_type_id": "10001",
            "visible_required_field_names": [
                "Description",
                "Labels",
                "Platform",
                "Reporter",
                "Summary",
                "Tester Name",
            ],
            "missing_required_field_names": [],
            "duplicate_required_field_names": [],
            "field_ids": {
                "Description": "description",
                "Labels": "labels",
                "Platform": "customfield_1",
                "Reporter": "reporter",
                "Summary": "summary",
                "Tester Name": "customfield_2",
            },
            "ok": True,
        }

        def transport(argv: tuple[str, ...], timeout: int):
            calls.append((argv, timeout))
            return result_type(returncode=0, stdout=json.dumps(signal))

        def invoke() -> tuple[int, dict]:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                returncode = resolver["main"](
                    [
                        "--system",
                        "jira",
                        "--intent",
                        "read",
                        "--context",
                        'company-alpha',
                        "--required-operation",
                        "project-read",
                        "--principal",
                        'company-alpha',
                        "--account",
                        _operator_binding('services.jira.hostname'),
                        "--run-exact-probe",
                    ],
                    probe_transport=transport,
                )
            return returncode, json.loads(stdout.getvalue())

        allowed_code, allowed = invoke()
        self.assertEqual(allowed_code, 0)
        self.assertEqual(
            calls,
            [
                (
                    (
                        _operator_binding('paths.python_binary'),
                        "-E",
                        "-s",
                        'scripts/jira_company_alpha_capability_probe.py',
                    ),
                    60,
                )
            ],
        )
        self.assertTrue(allowed["execution_guard"]["allowed"])
        self.assertTrue(allowed["route_guard"]["allowed"])
        self.assertTrue(allowed["route_guard"]["route_verified"])
        self.assertEqual(
            allowed["authoritative_probe_evidence"]["evidence_operations"],
            ["identity-read", "issue-create-metadata-read", "project-read"],
        )

        unsafe_lane = dict(allowed["preferred_lane"])
        unsafe_lane["probe_safe_lane"] = "local-read"
        self.assertIsNone(resolver["exact_probe_argv"](unsafe_lane))
        self.assertIsNone(
            resolver["parse_exact_probe_signal"](
                unsafe_lane,
                json.dumps(signal),
            )
        )

        signal["account_identity_matches_registered"] = False
        diagnostic_code, diagnostic = invoke()
        self.assertEqual(diagnostic_code, 0)
        self.assertTrue(diagnostic["execution_guard"]["allowed"])
        self.assertFalse(diagnostic["authoritative_probe_evidence"]["valid"])
        self.assertEqual(
            diagnostic["exact_probe_execution"]["result"],
            "blocked_exact_probe_signal_unverified",
        )

    def test_registered_read_probe_adapters_parse_only_exercised_operations(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        trusted_node = _operator_binding('paths.node_binary')
        discord_node = _operator_binding('paths.node_binary')
        xurl_cli = (
            _operator_binding('paths.routing_cli_js')
        )
        openclaw_cli = _operator_binding('paths.openclaw_cli')
        runtime_state_dir = resolver["OPENCLAW_RUNTIME_STATE_DIR"]

        x_signal = {
            "data": {
                "created_at": "2022-02-03T22:04:47.000Z",
                "description": "Co-Founder",
                "id": _operator_binding('services.x.user_id'),
                "name": 'Operator Operator',
                "profile_image_url": (
                    "https://pbs.twimg.com/profile_images/123/avatar_normal.jpg"
                ),
                "public_metrics": {
                    "followers_count": 1,
                    "following_count": 2,
                    "like_count": 3,
                    "listed_count": 4,
                    "media_count": 5,
                    "tweet_count": 6,
                },
                "subscription_type": "Premium",
                "username": _operator_binding('identifiers.x_username'),
                "verified": True,
                "verified_type": "blue",
            }
        }
        gigabrain_signal = {
            "schema": 'openclaw.company-alpha-gigabrain-metabase-probe.v1',
            "route": 'company-alpha-gigabrain-metabase-api',
            "system": 'company-alpha-gigabrain',
            "credential_handle_id": 'metabase.company_alpha.api',
            "endpoint_origin_expected": _operator_binding('services.company_alpha_analytics.origin'),
            "credential_contract_bound": True,
            "endpoint_matches_expected": True,
            "auth_ok": True,
            "account_identity_matches_registered": True,
            "provider_identity_sha256": (
                _operator_binding('services.company_alpha_analytics.provider_identity_sha256')
            ),
            "secrets_redacted": True,
            "external_mutation": False,
            "ok": True,
        }

        def discord_signal(guild_id: str, channel_id: str) -> dict:
            return {
                "action": "channel-info",
                "channel": "discord",
                "dryRun": False,
                "handledBy": "plugin",
                "payload": {
                    "ok": True,
                    "channel": {
                        "id": channel_id,
                        "type": 0,
                        "guild_id": guild_id,
                        "name": "general",
                        "position": 0,
                        "permission_overwrites": [],
                    },
                },
            }

        topology_signal = {
            "schema": 'openclaw.personal-data-neon-postgres-readonly-probe.v1',
            "route_id": 'personal-data-neon-postgres-readonly',
            "checked_at_utc": "2026-09-02T19:46:45Z",
            "status": "ready",
            "reason_code": None,
            "capability_scope": "read_only",
            "mutating": False,
            "secrets_emitted": False,
            "dsn_emitted": False,
            "unsupported_operations": [
                "write",
                "dml",
                "ddl",
                "migration",
                "provider_mutation",
            ],
            "custody": {
                "physical_workspace_verified": True,
                "secrets_directory_mode": "0700",
                "credential_file_mode": "0600",
                "attestation_file_mode": "0600",
                "single_link_private_files": True,
            },
            "attestation": {"sha256": "a" * 64, "identity_bound": True},
            "neon_api": {
                "auth_identity_verified": True,
                "project_identity_verified": True,
                "endpoint_identity_verified": True,
                "http_methods": ["GET"],
            },
            "postgres": {
                "endpoint_host_verified": True,
                "server_port_verified": True,
                "connection_database_verified": True,
                "connection_role_verified": True,
                "libpq_tls_verified": True,
                "database_identity_verified": True,
                "role_identity_verified": True,
                "project_identity_verified": True,
                "branch_identity_verified": True,
                "endpoint_identity_verified": True,
                "transaction_read_only": True,
                "tls_verified": True,
                "pg_stat_ssl_reported": False,
            },
        }
        cases = (
            {
                "name": "x-whoami",
                "route_id": 'x-api-operator-x-xurl',
                "args": [
                    "--system",
                    "x-api",
                    "--intent",
                    "read",
                    "--required-operation",
                    "timeline-read",
                    "--principal",
                    'operator',
                    "--account",
                    _operator_binding('identifiers.x_username'),
                ],
                "argv": (trusted_node, xurl_cli, "whoami"),
                "signal": x_signal,
                "operations": [],
                "invalidate": lambda value: value["data"].update(
                    {"username": "another-user"}
                ),
            },
            {
                "name": 'company-alpha-discord-source-channel',
                "route_id": 'discord-source-company-alpha-team',
                "args": [
                    "--system",
                    "discord-source",
                    "--intent",
                    "read",
                    "--required-operation",
                    "channel-info",
                    "--principal",
                    'company-alpha',
                    "--account",
                    _operator_binding('services.discord.company_alpha.guild_id'),
                ],
                "argv": (
                    "/usr/bin/env",
                    f"OPENCLAW_STATE_DIR={runtime_state_dir}",
                    discord_node,
                    openclaw_cli,
                    "message",
                    "channel",
                    "info",
                    "--channel",
                    "discord",
                    "--target",
                    "channel:" + _operator_binding("services.discord.company_alpha.channel_id"),
                    "--json",
                ),
                "signal": discord_signal(
                    _operator_binding('services.discord.company_alpha.guild_id'), _operator_binding('services.discord.company_alpha.channel_id')
                ),
                "operations": ["channel-info"],
                "invalidate": lambda value: value["payload"]["channel"].update(
                    {"guild_id": _operator_binding('services.discord.company_beta.guild_id')}
                ),
            },
            {
                "name": 'company-beta-discord-source-channel',
                "route_id": 'discord-source-company-beta-team',
                "args": [
                    "--system",
                    "discord-source",
                    "--intent",
                    "read",
                    "--required-operation",
                    "channel-info",
                    "--principal",
                    'company-beta',
                    "--account",
                    _operator_binding('services.discord.company_beta.guild_id'),
                ],
                "argv": (
                    "/usr/bin/env",
                    f"OPENCLAW_STATE_DIR={runtime_state_dir}",
                    discord_node,
                    openclaw_cli,
                    "message",
                    "channel",
                    "info",
                    "--channel",
                    "discord",
                    "--target",
                    "channel:" + _operator_binding("services.discord.company_beta.channel_id"),
                    "--json",
                ),
                "signal": discord_signal(
                    _operator_binding('services.discord.company_beta.guild_id'), _operator_binding('services.discord.company_beta.channel_id')
                ),
                "operations": ["channel-info"],
                "invalidate": lambda value: value["payload"]["channel"].update(
                    {"guild_id": _operator_binding('services.discord.company_alpha.guild_id')}
                ),
            },
            {
                "name": 'operations-discord-source-channel',
                "route_id": 'discord-source-operations-team',
                "args": [
                    "--system",
                    "discord-source",
                    "--intent",
                    "read",
                    "--required-operation",
                    "channel-info",
                    "--principal",
                    'operations',
                    "--account",
                    _operator_binding('services.discord.operations.guild_id'),
                ],
                "argv": (
                    "/usr/bin/env",
                    f"OPENCLAW_STATE_DIR={runtime_state_dir}",
                    discord_node,
                    openclaw_cli,
                    "message",
                    "channel",
                    "info",
                    "--channel",
                    "discord",
                    "--target",
                    "channel:" + _operator_binding("services.discord.operations.channel_id"),
                    "--json",
                ),
                "signal": discord_signal(
                    _operator_binding('services.discord.operations.guild_id'), _operator_binding('services.discord.operations.channel_id')
                ),
                "operations": ["channel-info"],
                "invalidate": lambda value: value["payload"]["channel"].update(
                    {"guild_id": _operator_binding('services.discord.company_beta.guild_id')}
                ),
            },
            {
                "name": 'company-alpha-gigabrain-exact-identity',
                "route_id": 'company-alpha-gigabrain-metabase-api',
                "args": [
                    "--system",
                    'company-alpha-gigabrain',
                    "--intent",
                    "read",
                    "--required-operation",
                    "status-read",
                    "--principal",
                    'company-alpha',
                    "--account",
                    'company-alpha-gigabrain',
                ],
                "argv": (
                    _operator_binding('paths.python_binary'),
                    "-E",
                    "-s",
                    'scripts/company_alpha_gigabrain_capability_probe.py',
                    "--json",
                ),
                "signal": gigabrain_signal,
                "operations": ["status-read"],
                "invalidate": lambda value: value.update(
                    {"account_identity_matches_registered": False}
                ),
            },
            {
                "name": 'personal-data-neon-readonly',
                "route_id": 'personal-data-neon-postgres-readonly',
                "args": [
                    "--system",
                    'personal-data-neon-postgres',
                    "--intent",
                    "read",
                    "--required-operation",
                    "query-readonly",
                    "--principal",
                    'personal-data-project',
                    "--account",
                    'personal-data-neon-project',
                ],
                "argv": (
                    _operator_binding('paths.python_binary'),
                    'scripts/personal_data_neon_readonly_capability_probe.py',
                    "--json",
                ),
                "signal": topology_signal,
                "operations": ["query-readonly"],
                "invalidate": lambda value: value["postgres"].update(
                    {"transaction_read_only": False}
                ),
            },
        )

        for case in cases:
            with self.subTest(case=case["name"]):
                calls: list[tuple[tuple[str, ...], int]] = []

                def transport(argv: tuple[str, ...], timeout: int):
                    calls.append((argv, timeout))
                    return result_type(
                        returncode=0,
                        stdout=json.dumps(case["signal"]),
                    )

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    returncode = resolver["main"](
                        case["args"] + ["--run-exact-probe"],
                        probe_transport=transport,
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(returncode, 0)
                self.assertEqual(
                    payload["preferred_lane"]["route_id"], case["route_id"]
                )
                self.assertEqual(calls, [(case["argv"], 60)])
                self.assertEqual(payload["exact_probe_execution"]["result"], "passed")
                self.assertTrue(payload["authoritative_probe_evidence"]["valid"])
                self.assertEqual(
                    payload["authoritative_probe_evidence"]["evidence_operations"],
                    case["operations"],
                )
                drifted = dict(payload["preferred_lane"])
                drifted["probe_command"] += " --unexpected"
                self.assertIsNone(resolver["exact_probe_argv"](drifted))

                invalid = json.loads(json.dumps(case["signal"]))
                case["invalidate"](invalid)
                preferred = payload["preferred_lane"]
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(invalid),
                    )
                )
                if case["name"] in {
                    'company-alpha-discord-source-channel',
                    'company-beta-discord-source-channel',
                }:
                    wrong_channel = json.loads(json.dumps(case["signal"]))
                    wrong_channel["payload"]["channel"]["id"] = (
                        "999999999999999999"
                    )
                    self.assertIsNone(
                        resolver["parse_exact_probe_signal"](
                            preferred,
                            json.dumps(wrong_channel),
                        )
                    )
                if case["name"] == 'company-alpha-gigabrain-exact-identity':
                    wrong_digest = json.loads(json.dumps(case["signal"]))
                    wrong_digest["provider_identity_sha256"] = "0" * 64
                    self.assertIsNone(
                        resolver["parse_exact_probe_signal"](
                            preferred,
                            json.dumps(wrong_digest),
                        )
                    )
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(case["signal"]),
                        "unexpected stderr",
                    )
                )
                if case["name"] == 'personal-data-neon-readonly':
                    strict_boolean_fields = (
                        ("custody", "physical_workspace_verified"),
                        ("custody", "single_link_private_files"),
                        ("neon_api", "auth_identity_verified"),
                        ("neon_api", "project_identity_verified"),
                        ("neon_api", "endpoint_identity_verified"),
                    )
                    for section, field in strict_boolean_fields:
                        with self.subTest(
                            topology_boolean_section=section,
                            topology_boolean_field=field,
                        ):
                            integer_boolean = json.loads(
                                json.dumps(case["signal"])
                            )
                            integer_boolean[section][field] = 1
                            self.assertIsNone(
                                resolver["parse_exact_probe_signal"](
                                    preferred,
                                    json.dumps(integer_boolean),
                                )
                            )

        def forbidden_linear_transport(_argv: tuple[str, ...], _timeout: int):
            self.fail("local MCP enrollment must not become account/team evidence")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            returncode = resolver["main"](
                [
                    "--system",
                    "linear",
                    "--intent",
                    "read",
                    "--required-operation",
                    "identity-read",
                    "--principal",
                    'company-beta',
                    "--account",
                    _operator_binding('identifiers.accounts.company_beta_operator_google'),
                    "--run-exact-probe",
                ],
                probe_transport=forbidden_linear_transport,
            )
        linear = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertFalse(linear["exact_probe_execution"]["supported"])
        self.assertFalse(linear["exact_probe_execution"]["attempted"])
        self.assertEqual(
            linear["exact_probe_execution"]["result"],
            "blocked_exact_probe_parser_unavailable",
        )
        self.assertFalse(linear["authoritative_probe_evidence"]["provided"])

    def test_company_alpha_docs_exact_probe_binds_stable_read_contract(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        schema_digest = "a" * 64
        signal = {
            "schema_version": 'openclaw.company_alpha_docs_mcp.probe.v1',
            "ok": True,
            "route_id": 'company-alpha-docs-mcp-readonly',
            "local_tools": ["docs_search", "docs_read"],
            "remote_tool_schema_digest": schema_digest,
            "mutation_attempt_count": 0,
            "local_writes_performed": False,
            "future_probe_metadata": "accepted",
            "receipt": {
                "schema_version": 'openclaw.company_alpha_docs_mcp.receipt.v1',
                "route_id": 'company-alpha-docs-mcp-readonly',
                "remote_tool_schema_digest": schema_digest,
                "remote_tool_invoked": None,
                "egress_class": "public_only",
                "mutation_attempt_count": 0,
                "result": "healthy",
                "future_receipt_metadata": "accepted",
            },
        }
        calls: list[tuple[tuple[str, ...], int]] = []

        def transport(argv: tuple[str, ...], timeout: int):
            calls.append((argv, timeout))
            return result_type(returncode=0, stdout=json.dumps(signal))

        args = [
            "--system",
            'company-alpha-docs',
            "--intent",
            "read",
            "--portfolio",
            'company-alpha',
            "--required-operation",
            "docs-search",
            "--principal",
            'company-alpha',
            "--account",
            'company-alpha-public-docs',
            "--run-exact-probe",
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            returncode = resolver["main"](args, probe_transport=transport)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertEqual(
            calls,
            [
                (
                    (
                        _operator_binding('paths.python_binary'),
                        "-E",
                        "-s",
                        'scripts/company_alpha_docs_mcp_adapter.py',
                        "--manifest",
                        'config/company_alpha_docs_mcp.json',
                        "--probe",
                    ),
                    60,
                )
            ],
        )
        self.assertEqual(payload["exact_probe_execution"]["result"], "passed")
        self.assertEqual(
            payload["authoritative_probe_evidence"]["evidence_effects"],
            ["read"],
        )
        self.assertEqual(
            payload["authoritative_probe_evidence"]["evidence_operations"],
            ["docs-read", "docs-search"],
        )

        preferred = payload["preferred_lane"]
        drifted_command = dict(preferred)
        drifted_command["probe_command"] += " --unexpected"
        self.assertIsNone(resolver["exact_probe_argv"](drifted_command))

        for section, field, value in (
            (None, "route_id", "another-route"),
            (None, "local_tools", ["docs_search", "docs_read", "submit_feedback"]),
            (None, "mutation_attempt_count", 1),
            (None, "local_writes_performed", True),
            ("receipt", "route_id", "another-route"),
            ("receipt", "remote_tool_invoked", 'search_company_alpha'),
            ("receipt", "egress_class", "private"),
            ("receipt", "result", "degraded"),
        ):
            invalid = json.loads(json.dumps(signal))
            target = invalid if section is None else invalid[section]
            target[field] = value
            self.assertIsNone(
                resolver["parse_exact_probe_signal"](
                    preferred,
                    json.dumps(invalid),
                )
            )

    def test_discord_exact_probe_uses_current_managed_node_version(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        node = Path(resolver["DISCORD_SOURCE_NODE_BIN"])
        state_dir = resolver["OPENCLAW_RUNTIME_STATE_DIR"]
        self.assertEqual(
            node,
            Path(
                _operator_binding('paths.node_binary')
            ),
        )
        version = subprocess.run(
            [str(node), "--version"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertGreaterEqual(int(version.stdout.strip().lstrip("v").split(".")[0]), 24)

        for contract in resolver["DISCORD_SOURCE_ROUTE_PROBES"].values():
            payload = resolver["resolve"](
                "discord-source",
                "read",
                required_operation="channel-info",
                requested_principal=contract["principal"],
                requested_account=contract["guild_id"],
            )
            argv = resolver["exact_probe_argv"](payload["preferred_lane"])
            self.assertIsNotNone(argv)
            self.assertEqual(
                argv[:4],
                (
                    "/usr/bin/env",
                    f"OPENCLAW_STATE_DIR={state_dir}",
                    str(node),
                    resolver["TRUSTED_OPENCLAW_CLI"],
                ),
            )

    def test_probe_transport_keeps_host_token_out_of_state_bound_probe(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        transport = resolver["subprocess_probe_transport"]
        state_dir = resolver["OPENCLAW_RUNTIME_STATE_DIR"]

        with mock.patch.dict(
            os.environ,
            {"OPENCLAW_GATEWAY_TOKEN": "host-token-must-not-cross"},
            clear=False,
        ):
            child = transport(
                (
                    "/usr/bin/env",
                    f"OPENCLAW_STATE_DIR={state_dir}",
                    _operator_binding('paths.python_binary'),
                    "-E",
                    "-s",
                    "-c",
                    (
                        "import json, os; "
                        "print(json.dumps({"
                        "'state_dir': os.environ.get('OPENCLAW_STATE_DIR'), "
                        "'host_token_present': 'OPENCLAW_GATEWAY_TOKEN' in os.environ"
                        "}))"
                    ),
                ),
                5,
            )

        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(
            json.loads(child.stdout),
            {"state_dir": state_dir, "host_token_present": False},
        )

    def test_probe_transport_blocks_hostile_python_startup_and_stdin(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        transport = resolver["subprocess_probe_transport"]
        result_type = resolver["ProbeCommandResult"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pythonpath_root = root / "pythonpath"
            pythonpath_root.mkdir()
            pythonpath_marker = root / "sitecustomize-imported"
            (pythonpath_root / "sitecustomize.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(pythonpath_marker)!r}).write_text('imported', encoding='utf-8')\n",
                encoding="utf-8",
            )
            user_base = root / "user-base"
            user_site = user_base / "lib" / "python" / "site-packages"
            user_site.mkdir(parents=True)
            user_site_marker = root / "usercustomize-imported"
            (user_site / "usercustomize.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(user_site_marker)!r}).write_text('imported', encoding='utf-8')\n",
                encoding="utf-8",
            )
            hostile = {
                "PYTHONPATH": str(pythonpath_root),
                "PYTHONUSERBASE": str(user_base),
                "PYTHONSTARTUP": str(root / "startup.py"),
            }
            with mock.patch.dict(os.environ, hostile, clear=False):
                legacy = subprocess.run(
                    [_operator_binding('paths.python_binary'), "-c", "pass"],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(legacy.returncode, 0)
                self.assertTrue(
                    pythonpath_marker.exists(),
                    "hostile PYTHONPATH sitecustomize control did not fire",
                )
                self.assertTrue(
                    user_site_marker.exists(),
                    "hostile user-site usercustomize control did not fire",
                )
                pythonpath_marker.unlink()
                user_site_marker.unlink()

                child = transport(
                    (
                        _operator_binding('paths.python_binary'),
                        "-E",
                        "-s",
                        "-c",
                        (
                            "import json, os, sys; "
                            "print(json.dumps({"
                            "'pythonpath': 'PYTHONPATH' in os.environ, "
                            "'pythonuserbase': 'PYTHONUSERBASE' in os.environ, "
                            "'pythonstartup': 'PYTHONSTARTUP' in os.environ, "
                            "'stdin': sys.stdin.read()}))"
                        ),
                    ),
                    5,
                )

            self.assertIsInstance(child, result_type)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertFalse(pythonpath_marker.exists())
            self.assertFalse(user_site_marker.exists())
            payload = json.loads(child.stdout)
            self.assertFalse(payload["pythonpath"])
            self.assertFalse(payload["pythonuserbase"])
            self.assertFalse(payload["pythonstartup"])
            self.assertEqual(payload["stdin"], "")

    def test_probe_transport_bounds_output_and_cleans_timed_out_process_group(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        transport = resolver["subprocess_probe_transport"]

        oversized = transport(
            (
                _operator_binding('paths.python_binary'),
                "-E",
                "-s",
                "-c",
                (
                    "import os; "
                    f"os.write(1, b'x' * ({resolver['MAX_PROBE_OUTPUT_BYTES']} + 1))"
                ),
            ),
            5,
        )
        self.assertEqual(oversized.returncode, 125)
        self.assertEqual(oversized.outcome, "output_too_large")
        self.assertLessEqual(
            len(oversized.stdout.encode("utf-8"))
            + len(oversized.stderr.encode("utf-8")),
            resolver["MAX_PROBE_OUTPUT_BYTES"],
        )

        timed_out = transport(
            (
                _operator_binding('paths.python_binary'),
                "-E",
                "-s",
                "-c",
                (
                    "import os, subprocess, time; "
                    "print(os.getpid(), flush=True); "
                    "subprocess.Popen(['/bin/sleep', '30']); "
                    "time.sleep(30)"
                ),
            ),
            0.2,
        )
        self.assertEqual(timed_out.returncode, 124)
        self.assertEqual(timed_out.outcome, "timeout")
        process_group = int(timed_out.stdout.strip())
        with self.assertRaises(ProcessLookupError):
            os.killpg(process_group, 0)

    def test_exact_jira_helper_boots_without_user_site_packages(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        child = resolver["subprocess_probe_transport"](
            resolver['JIRA_COMPANY_ALPHA_PROBE_ARGV'] + ("--help",),
            5,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn("usage:", child.stdout.lower())

    def test_calendar_transport_uses_only_confined_keyring_unlock(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        transport = resolver["subprocess_probe_transport"]
        transport_globals = transport.__globals__
        secret = "test-keyring-unlock-never-print"

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "gog.env"
            env_file.write_text(
                "GOG_ACCOUNT=operator@gmail.com\n"
                f"GOG_KEYRING_PASSWORD={secret}\n",
                encoding="utf-8",
            )
            env_file.chmod(0o600)
            fake_gog = Path(directory) / "gog"
            fake_gog.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "print(json.dumps({"
                f"'unlock_loaded': os.environ.get('GOG_KEYRING_PASSWORD') == {secret!r}, "
                "'account_present': 'GOG_ACCOUNT' in os.environ, "
                "'pythonpath_present': 'PYTHONPATH' in os.environ, "
                "'stdin': sys.stdin.read()}))\n",
                encoding="utf-8",
            )
            fake_gog.chmod(0o700)
            overrides = {
                "TRUSTED_GOG_BIN": fake_gog,
                "TRUSTED_GOG_ENV_FILE": env_file,
            }
            with mock.patch.dict(transport_globals, overrides, clear=False):
                with mock.patch.dict(
                    os.environ,
                    {"PYTHONPATH": str(Path(directory) / "hostile")},
                    clear=False,
                ):
                    child = transport(
                        (str(fake_gog),),
                        5,
                    )

                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertNotIn(secret, child.stdout)
                payload = json.loads(child.stdout)
                self.assertTrue(payload["unlock_loaded"])
                self.assertFalse(payload["account_present"])
                self.assertFalse(payload["pythonpath_present"])
                self.assertEqual(payload["stdin"], "")

                env_file.chmod(0o644)
                blocked = transport(
                    (str(fake_gog),),
                    5,
                )
                self.assertEqual(blocked.returncode, 126)

                env_file.write_text(
                    "GOG_ACCOUNT=operator@gmail.com\n"
                    f"GOG_KEYRING_PASSWORD={secret}\n"
                    f"GOG_KEYRING_PASSWORD={secret}\n",
                    encoding="utf-8",
                )
                env_file.chmod(0o600)
                duplicate_key = transport((str(fake_gog),), 5)
                self.assertEqual(duplicate_key.returncode, 126)

                env_file.write_text(
                    "GOG_ACCOUNT=operator@gmail.com\n"
                    f"GOG_KEYRING_PASSWORD={secret}\n",
                    encoding="utf-8",
                )
                fake_gog.chmod(0o722)
                unsafe_binary = transport((str(fake_gog),), 5)
                self.assertEqual(unsafe_binary.returncode, 126)

    def test_coordinator_calendar_probe_selects_sealed_account_not_env_default(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        calls: list[tuple[str, ...]] = []

        def transport(argv: tuple[str, ...], _timeout: int):
            calls.append(argv)
            return result_type(
                returncode=0,
                stdout=json.dumps(
                    {
                        "calendars": [
                            {
                                "id": _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
                                "primary": True,
                                "accessRole": "owner",
                            }
                        ]
                    }
                ),
            )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            returncode = resolver["main"](
                [
                    "--system",
                    "google-calendar",
                    "--intent",
                    "write",
                    "--context",
                    'company-alpha',
                    "--required-operation",
                    "event-create",
                    "--principal",
                    'company-alpha',
                    "--account",
                    _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
                    "--run-exact-probe",
                ],
                probe_transport=transport,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertTrue(payload["execution_guard"]["allowed"])
        self.assertEqual(len(calls), 1)
        account_index = calls[0].index("--account") + 1
        self.assertEqual(calls[0][account_index], _operator_binding('identifiers.accounts.company_alpha_coordinator_google'))

    def test_apple_calendar_exact_probe_admits_read_only_route(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        calls: list[tuple[str, ...]] = []
        probe_payload = {
            "checked_at_utc": "2026-08-28T01:00:00Z",
            "capabilities": {
                "osascript": {"status": "ready", "path": "/usr/bin/osascript"},
                "calendar_launch": {"status": "ready"},
                "calendar_list": {
                    "status": "ready",
                    "count": 2,
                    "calendars": ["Home", "Work"],
                },
                "calendar_read_next_7d": {
                    "status": "ready",
                    "readable_calendars": 2,
                    "total_events_next_7d": 3,
                    "calendar_event_counts": {
                        "Home": {"status": "ready", "events_next_7d": 1},
                        "Work": {"status": "ready", "events_next_7d": 2},
                    },
                },
            },
            "overall": "ready",
            "errors": [],
        }

        def transport(argv: tuple[str, ...], _timeout: int):
            calls.append(argv)
            return result_type(returncode=0, stdout=json.dumps(probe_payload))

        args = [
            "--system",
            "apple-calendar",
            "--intent",
            "read",
            "--context",
            "personal",
            "--required-operation",
            "event-list",
            "--principal",
            'operator',
            "--account",
            "local-apple-calendar",
            "--run-exact-probe",
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            returncode = resolver["main"](args, probe_transport=transport)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertTrue(payload["execution_guard"]["allowed"])
        self.assertEqual(payload["exact_probe_execution"]["result"], "passed")
        self.assertEqual(
            payload["authoritative_probe_evidence"]["evidence_operations"],
            ["calendar-list", "event-list"],
        )
        self.assertEqual(
            calls,
            [
                (
                    _operator_binding('paths.python_binary'),
                    "-E",
                    "-s",
                    "scripts/apple_calendar_probe.py",
                )
            ],
        )

        calls.clear()
        mismatched = list(args)
        mismatched[mismatched.index("local-apple-calendar")] = "icloud"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            mismatch_code = resolver["main"](
                mismatched,
                probe_transport=transport,
            )
        mismatch = json.loads(stdout.getvalue())
        self.assertEqual(mismatch_code, 2)
        self.assertEqual(calls, [])
        self.assertIsNone(mismatch["preferred_lane"])
        self.assertEqual(
            mismatch["execution_guard"]["blocker_code"],
            "blocked_no_exact_account_route",
        )

        preferred = {
            "probe_id": "apple-calendar-local-probe",
        }
        for invalid in (
            probe_payload | {"overall": "degraded"},
            probe_payload | {"errors": ["calendar_read_failed"]},
            probe_payload
            | {
                "capabilities": probe_payload["capabilities"]
                | {
                    "calendar_read_next_7d": probe_payload["capabilities"][
                        "calendar_read_next_7d"
                    ]
                    | {"total_events_next_7d": 4}
                }
            },
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(invalid),
                    )
                )

    def test_notion_exact_probes_bind_owner_route_and_parse_identity_only(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        cases = (
            {
                "route": "personal",
                "route_id": "notion-personal",
                "principal": 'operator',
                "account": 'operator-personal-notion',
                "handle_id": "notion.personal.api",
                "workspace_id": "11111111-1111-1111-1111-111111111111",
                "workspace_name": "Personal workspace",
            },
            {
                "route": 'company-alpha',
                "route_id": 'notion-company-alpha',
                "principal": 'company-alpha',
                "account": 'company-alpha-team-notion',
                "handle_id": 'notion.company_alpha.api',
                "workspace_id": "22222222-2222-2222-2222-222222222222",
                "workspace_name": 'CompanyAlpha workspace',
            },
        )

        for case in cases:
            with self.subTest(route=case["route"]):
                calls: list[tuple[tuple[str, ...], int]] = []
                probe_payload = [
                    {
                        "route": case["route"],
                        "credential_handle_id": case["handle_id"],
                        "credential_owner_bound": True,
                        "route_ok": True,
                        "bot_name": "OpenClaw Notion bot",
                        "owner_type": "workspace",
                        "workspace_name": case["workspace_name"],
                        "workspace_id": case["workspace_id"],
                        "ok": True,
                    }
                ]

                def transport(argv: tuple[str, ...], timeout: int):
                    calls.append((argv, timeout))
                    return result_type(
                        returncode=0,
                        stdout=json.dumps(probe_payload),
                    )

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    returncode = resolver["main"](
                        [
                            "--system",
                            "notion",
                            "--intent",
                            "read",
                            "--context",
                            case["route"],
                            "--required-operation",
                            "page-read",
                            "--principal",
                            case["principal"],
                            "--account",
                            case["account"],
                            "--run-exact-probe",
                        ],
                        probe_transport=transport,
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(returncode, 0)
                self.assertEqual(
                    calls,
                    [
                        (
                            (
                                "python3",
                                "scripts/notion_capability_probe.py",
                                "--route",
                                case["route"],
                            ),
                            60,
                        )
                    ],
                )
                self.assertEqual(
                    payload["preferred_lane"]["route_id"],
                    case["route_id"],
                )
                self.assertEqual(
                    payload["exact_probe_execution"]["result"],
                    "passed",
                )
                self.assertTrue(payload["authoritative_probe_evidence"]["valid"])
                self.assertEqual(
                    payload["authoritative_probe_evidence"]["evidence_operations"],
                    ["identity-read"],
                )

                preferred = payload["preferred_lane"]
                drifted_command = dict(preferred)
                drifted_command["probe_command"] += " --page-id unexpected"
                self.assertIsNone(resolver["exact_probe_argv"](drifted_command))
                drifted_account = dict(preferred)
                drifted_account["required_account"] = "wrong-account"
                self.assertIsNone(resolver["exact_probe_argv"](drifted_account))
                for field, invalid in (
                    ("credential_handle_id", "notion.wrong.api"),
                    ("credential_owner_bound", False),
                    ("workspace_id", "not-a-workspace-id"),
                    ("route", "wrong-route"),
                ):
                    drifted = json.loads(json.dumps(probe_payload))
                    drifted[0][field] = invalid
                    with self.subTest(route=case["route"], invalid=field):
                        self.assertIsNone(
                            resolver["parse_exact_probe_signal"](
                                preferred,
                                json.dumps(drifted),
                            )
                        )

    def test_github_exact_probe_accepts_active_keyring_identity_from_stderr(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        calls: list[tuple[tuple[str, ...], int]] = []
        signal = 'github.com\n  ✓ Logged in to github.com account operator (keyring)\n  - Active account: true\n  - Git operations protocol: https\n'

        def transport(argv: tuple[str, ...], timeout: int):
            calls.append((argv, timeout))
            return result_type(returncode=0, stdout="", stderr=signal)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            returncode = resolver["main"](
                [
                    "--system",
                    "github",
                    "--intent",
                    "read",
                    "--portfolio",
                    "personal",
                    "--required-operation",
                    "repo-read",
                    "--principal",
                    'operator',
                    "--account",
                    _operator_binding('identifiers.github_username'),
                    "--run-exact-probe",
                ],
                probe_transport=transport,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertEqual(
            calls,
            [
                (
                    (
                        _operator_binding('paths.github_binary'),
                        "auth",
                        "status",
                        "--hostname",
                        "github.com",
                    ),
                    60,
                )
            ],
        )
        self.assertEqual(payload["exact_probe_execution"]["result"], "passed")
        self.assertTrue(payload["authoritative_probe_evidence"]["valid"])
        self.assertEqual(
            payload["authoritative_probe_evidence"]["evidence_operations"],
            ["identity-read"],
        )
        self.assertFalse(
            payload["route_readiness"]["live_probe_authoritative_passed"]
        )

        preferred = payload["preferred_lane"]
        for invalid in (
            signal.replace("Active account: true", "Active account: false"),
            signal.replace(_operator_binding('identifiers.github_username'), "someone-else"),
            signal.replace("Git operations protocol: https", "Git operations protocol: ssh"),
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        "",
                        invalid,
                    )
                )
        self.assertIsNone(
            resolver["parse_exact_probe_signal"](
                preferred,
                signal,
                signal,
            )
        )
        mismatched_multi_account = 'github.com\n  ✓ Logged in to github.com account operator (keyring)\n  - Active account: false\n  - Git operations protocol: ssh\n  ✓ Logged in to github.com account someone-else (environment)\n  - Active account: true\n  - Git operations protocol: https\n'
        self.assertIsNone(
            resolver["parse_exact_probe_signal"](
                preferred,
                "",
                mismatched_multi_account,
            )
        )
        plaintext_token = signal + "  - Token: ghp_plaintext-example\n"
        self.assertIsNone(
            resolver["parse_exact_probe_signal"](
                preferred,
                "",
                plaintext_token,
            )
        )
        other_account_plaintext_token = 'github.com\n  ✓ Logged in to github.com account someone-else (keyring)\n  - Active account: false\n  - Git operations protocol: ssh\n  - Token: ghp_plaintext-secret\n  ✓ Logged in to github.com account operator (keyring)\n  - Active account: true\n  - Git operations protocol: https\n'
        self.assertIsNone(
            resolver["parse_exact_probe_signal"](
                preferred,
                "",
                other_account_plaintext_token,
            )
        )
        redacted_metadata = signal + (
            "  - Token: gho_************************************\n"
            "  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'\n"
        )
        self.assertEqual(
            resolver["parse_exact_probe_signal"](
                preferred,
                "",
                redacted_metadata,
            ),
            (("read",), ("identity-read",)),
        )

        oversized = result_type(
            returncode=0,
            stdout="",
            stderr=signal + ("x" * resolver["MAX_PROBE_OUTPUT_BYTES"]),
        )
        evidence, diagnostics = resolver["run_exact_registered_probe"](
            preferred,
            lambda _argv, _timeout: oversized,
        )
        self.assertIsNone(evidence)
        self.assertEqual(
            diagnostics["result"],
            "blocked_exact_probe_output_too_large",
        )

    def test_cloudflare_and_trello_exact_probes_bind_strict_read_evidence(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        cases = (
            {
                "name": "cloudflare",
                "args": [
                    "--system",
                    "cloudflare",
                    "--intent",
                    "read",
                    "--portfolio",
                    "personal",
                    "--required-operation",
                    "zone-read",
                    "--principal",
                    'operator',
                    "--account",
                    _operator_binding('services.cloudflare.zone_name'),
                    "--run-exact-probe",
                ],
                "argv": (
                    _operator_binding('paths.python_binary'),
                    "-E",
                    "-s",
                    "scripts/cloudflare_capability_probe.py",
                ),
                "operations": [
                    "token-verify",
                    "account-read",
                    "zone-read",
                    "pages-project-read",
                ],
                "signal": {
                    "schema": "openclaw.cloudflare-personal-read.v1",
                    "route_id": 'cloudflare-operator-readonly-api',
                    "status": "ready",
                    "reason_code": None,
                    "identity": {
                        "account_id": _operator_binding('services.cloudflare.account_id'),
                        "account_name": _operator_binding('services.cloudflare.account_name'),
                    },
                    "zone": {
                        "id": _operator_binding('services.cloudflare.zone_id'),
                        "name": _operator_binding('services.cloudflare.zone_name'),
                        "status": "active",
                        "type": "full",
                        "account_id": _operator_binding('services.cloudflare.account_id'),
                    },
                    "project": {
                        "id": _operator_binding('services.cloudflare.project_id'),
                        "name": "personal-site",
                        "subdomain": _operator_binding('services.cloudflare.project_subdomain'),
                        "production_branch": "main",
                        "source": {
                            "type": "github",
                            "owner": _operator_binding('identifiers.github_username'),
                            "repository": "personal-site",
                        },
                    },
                },
                "invalid": ("project", "production_branch", "preview"),
            },
            {
                "name": "trello",
                "args": [
                    "--system",
                    "trello",
                    "--intent",
                    "read",
                    "--portfolio",
                    "personal",
                    "--required-operation",
                    "board-read",
                    "--principal",
                    'operator',
                    "--account",
                    _operator_binding('identifiers.accounts.personal_google'),
                    "--run-exact-probe",
                ],
                "argv": (
                    _operator_binding('paths.python_binary'),
                    "-E",
                    "-s",
                    "scripts/trello_capability_probe.py",
                    "--board-id",
                    _operator_binding('services.trello.board_id'),
                ),
                "operations": ["board-read", "member-read"],
                "signal": {
                    "overall": "ready",
                    "auth": {
                        "status": "ready",
                        "member_id": "1234567890abcdef12345678",
                        "username": _operator_binding('services.trello.username'),
                        "full_name": 'Operator Operator',
                        "url": 'https://trello.com/u/' + _operator_binding('services.trello.username'),
                    },
                    "board": {
                        "status": "ready",
                        "id": "1234567890abcdef12345679",
                        "name": _operator_binding('services.trello.board_name'),
                        "url": (
                            'https://trello.com/b/' + _operator_binding('services.trello.board_id') + '/fixture-project-management'
                        ),
                        "closed": False,
                    },
                    "errors": [],
                },
                "invalid": ("board", "closed", True),
            },
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                calls: list[tuple[tuple[str, ...], int]] = []

                def transport(argv: tuple[str, ...], timeout: int):
                    calls.append((argv, timeout))
                    return result_type(
                        returncode=0,
                        stdout=json.dumps(case["signal"]),
                    )

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    returncode = resolver["main"](
                        case["args"],
                        probe_transport=transport,
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(returncode, 0)
                self.assertEqual(calls, [(case["argv"], 60)])
                self.assertEqual(
                    payload["exact_probe_execution"]["result"],
                    "passed",
                )
                self.assertTrue(
                    payload["route_readiness"]["live_probe_authoritative_passed"]
                )
                self.assertEqual(
                    payload["authoritative_probe_evidence"]["evidence_operations"],
                    case["operations"],
                )

                preferred = payload["preferred_lane"]
                drifted_command = dict(preferred)
                drifted_command["probe_command"] += " --unexpected"
                self.assertIsNone(resolver["exact_probe_argv"](drifted_command))
                invalid = json.loads(json.dumps(case["signal"]))
                section, field, value = case["invalid"]
                invalid[section][field] = value
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(invalid),
                    )
                )
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(case["signal"]),
                        "unexpected provider diagnostics",
                    )
                )
                if case["name"] == "trello":
                    invalid = json.loads(json.dumps(case["signal"]))
                    invalid["auth"]["url"] = (
                        'https://trello.com/operator'
                    )
                    self.assertIsNone(
                        resolver["parse_exact_probe_signal"](
                            preferred,
                            json.dumps(invalid),
                        )
                    )
                    invalid = json.loads(json.dumps(case["signal"]))
                    invalid["auth"]["url"] = (
                        'https://trello.com/u/\noperator'
                    )
                    self.assertIsNone(
                        resolver["parse_exact_probe_signal"](
                            preferred,
                            json.dumps(invalid),
                        )
                    )
                    invalid = json.loads(json.dumps(case["signal"]))
                    invalid["auth"]["member_id"] = "x"
                    self.assertIsNone(
                        resolver["parse_exact_probe_signal"](
                            preferred,
                            json.dumps(invalid),
                        )
                    )
                if case["name"] == "cloudflare":
                    nested_json = "[" * 1100 + "0" + "]" * 1100
                    evidence, diagnostics = resolver[
                        "run_exact_registered_probe"
                    ](
                        preferred,
                        lambda _argv, _timeout: result_type(
                            returncode=0,
                            stdout=nested_json,
                        ),
                    )
                    self.assertIsNone(evidence)
                    self.assertEqual(
                        diagnostics["result"],
                        "blocked_exact_probe_signal_unverified",
                    )

    def test_hedera_signer_exact_probes_bind_identity_custody_and_zero_effects(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        trusted_node = _operator_binding('paths.node_binary')
        cases = (
            {
                "network": "mainnet",
                "account": _operator_binding('services.wallets.hedera_mainnet.account_id'),
                "script": "scripts/hedera_mainnet_test_signer_probe.mjs",
                "schema": "openclaw.hedera_mainnet_test_signer_probe.v1",
                "classification": (
                    "general_openclaw_hedera_mainnet_disposable_test_signer"
                ),
                "secret": "openclaw-hedera-mainnet-test-account.env",
                "variables": [
                    "OPENCLAW_HEDERA_MAINNET_TEST_ACCOUNT_ID",
                    "OPENCLAW_HEDERA_MAINNET_TEST_PRIVATE_KEY",
                    "OPENCLAW_HEDERA_MAINNET_TEST_KEY_TYPE",
                ],
                "key_type": "ED25519",
                "private_key_length": 96,
                "public_key": "ab" * 32,
                "evm": None,
            },
            {
                "network": "testnet",
                "account": _operator_binding('services.wallets.hedera_testnet.account_id'),
                "script": "scripts/hedera_testnet_test_signer_probe.mjs",
                "schema": "openclaw.hedera_testnet_test_signer_probe.v1",
                "classification": (
                    "general_openclaw_hedera_testnet_disposable_test_signer"
                ),
                "secret": "openclaw-hedera-testnet-test-account.env",
                "variables": [
                    "OPENCLAW_HEDERA_TESTNET_TEST_ACCOUNT_ID",
                    "OPENCLAW_HEDERA_TESTNET_TEST_PRIVATE_KEY",
                ],
                "key_type": "ECDSA_SECP256K1",
                "private_key_length": 64,
                "public_key": "02" + ("ab" * 32),
                "evm": "0x" + ("12" * 20),
            },
        )
        for case in cases:
            with self.subTest(network=case["network"]):
                public_key = case["public_key"]
                fingerprint = hashlib.sha256(
                    bytes.fromhex(public_key)
                ).hexdigest()
                key = {
                    "type": case["key_type"],
                    "derived_public_key": public_key,
                    "derived_public_key_fingerprint_sha256": fingerprint,
                    "authoritative_public_key": public_key,
                    "authoritative_public_key_fingerprint_sha256": fingerprint,
                    "matches_authoritative_account": True,
                    "derived_evm_alias": case["evm"],
                    "mirror_evm_address": (
                        case["evm"]
                        if case["evm"] is not None
                        else "0x" + ("34" * 20)
                    ),
                }
                if case["network"] == "testnet":
                    key["evm_alias_matches"] = True
                boundaries = {
                    "transaction_executed": False,
                    "signing_executed": False,
                    "funds_moved": False,
                    "default_process_inheritance": False,
                }
                if case["network"] == "testnet":
                    boundaries["raw_private_key_output"] = False
                value_lengths = {
                    case["variables"][0]: len(case["account"]),
                    case["variables"][1]: case["private_key_length"],
                }
                if case["network"] == "mainnet":
                    value_lengths[case["variables"][2]] = len(case["key_type"])
                secret_size = sum(
                    len(name) + 1 + length + 1
                    for name, length in value_lengths.items()
                )
                signal = {
                    "schema": case["schema"],
                    "ok": True,
                    "checked_at": (
                        datetime.now(timezone.utc)
                        .isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z")
                    ),
                    "network": case["network"],
                    "ledger": f"hedera-{case['network']}",
                    "account_id": case["account"],
                    "classification": case["classification"],
                    "secret_file": {
                        "path": _operator_binding("paths.routing_openclaw_hedera_" + case["network"] + "_test_account_env"),
                        "owner": _operator_binding("identifiers.host_user"),
                        "uid": os.getuid(),
                        "gid": 20,
                        "mode": "0600",
                        "regular_file": True,
                        "symlink": False,
                        "inode": "123456",
                        "size_bytes": secret_size,
                        "variable_names": case["variables"],
                        "value_lengths": value_lengths,
                    },
                    "sdk": {"package": "@hashgraph/sdk", "version": "2.81.0"},
                    "key": key,
                    "balance": {
                        "tinybar": "123456789",
                        "hbar": "1.23456789",
                        "mirror_timestamp": "1788282000.000000000",
                    },
                    "boundaries": boundaries,
                }
                calls: list[tuple[tuple[str, ...], int]] = []

                def transport(argv: tuple[str, ...], timeout: int):
                    calls.append((argv, timeout))
                    return result_type(returncode=0, stdout=json.dumps(signal))

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    returncode = resolver["main"](
                        [
                            "--system",
                            f"hedera-{case['network']}",
                            "--intent",
                            "read",
                            "--portfolio",
                            "openclaw-testing",
                            "--workspace",
                            'company-alpha-testing',
                            "--network",
                            f"hedera-{case['network']}",
                            "--required-operation",
                            "account-read",
                            "--principal",
                            "openclaw-testing",
                            "--account",
                            case["account"],
                            "--run-exact-probe",
                        ],
                        probe_transport=transport,
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(returncode, 0)
                self.assertEqual(
                    calls,
                    [((trusted_node, case["script"], "--json"), 60)],
                )
                self.assertEqual(
                    payload["exact_probe_execution"]["result"],
                    "passed",
                )
                self.assertTrue(
                    payload["route_readiness"]["live_probe_authoritative_passed"]
                )
                self.assertEqual(
                    payload["authoritative_probe_evidence"]["evidence_operations"],
                    ["account-read"],
                )

                preferred = payload["preferred_lane"]
                invalid = json.loads(json.dumps(signal))
                invalid["boundaries"]["signing_executed"] = True
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(invalid),
                    )
                )
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(signal),
                        "unexpected provider diagnostics",
                    )
                )
                for field, value in (
                    ("uid", 0 if os.getuid() != 0 else 1),
                    ("size_bytes", 1),
                ):
                    invalid = json.loads(json.dumps(signal))
                    invalid["secret_file"][field] = value
                    self.assertIsNone(
                        resolver["parse_exact_probe_signal"](
                            preferred,
                            json.dumps(invalid),
                        )
                    )
                invalid = json.loads(json.dumps(signal))
                invalid["secret_file"]["value_lengths"][case["variables"][1]] = 1
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(invalid),
                    )
                )
                invalid = json.loads(json.dumps(signal))
                invalid["checked_at"] = "1970-01-01T00:00:00Z"
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(invalid),
                    )
                )
                invalid = json.loads(json.dumps(signal))
                invalid["balance"]["tinybar"] = "1" * 20
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(invalid),
                    )
                )
                if case["network"] == "testnet":
                    invalid = json.loads(json.dumps(signal))
                    uncompressed = "04" + invalid["key"]["derived_public_key"][2:]
                    invalid["key"]["derived_public_key"] = uncompressed
                    invalid["key"]["authoritative_public_key"] = uncompressed
                    fingerprint = hashlib.sha256(bytes.fromhex(uncompressed)).hexdigest()
                    invalid["key"]["derived_public_key_fingerprint_sha256"] = fingerprint
                    invalid["key"]["authoritative_public_key_fingerprint_sha256"] = fingerprint
                    self.assertIsNone(
                        resolver["parse_exact_probe_signal"](
                            preferred,
                            json.dumps(invalid),
                        )
                    )
                invalid = json.loads(json.dumps(signal))
                invalid["key"]["derived_public_key_fingerprint_sha256"] = "0" * 64
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        json.dumps(invalid),
                    )
                )

    def test_unsupported_exact_probe_is_diagnostic_without_blocking_native_route(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))

        def forbidden_transport(_argv: tuple[str, ...], _timeout: int):
            self.fail("an unsupported probe parser must not invoke transport")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            returncode = resolver["main"](
                [
                    "--system",
                    "google-forms",
                    "--intent",
                    "read",
                    "--context",
                    "personal",
                    "--required-operation",
                    "form-get",
                    "--principal",
                    'operator',
                    "--account",
                    _operator_binding('identifiers.accounts.personal_google'),
                    "--run-exact-probe",
                ],
                probe_transport=forbidden_transport,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertTrue(payload["execution_guard"]["allowed"])
        self.assertFalse(payload["exact_probe_execution"]["supported"])
        self.assertEqual(
            payload["exact_probe_execution"]["result"],
            "blocked_exact_probe_parser_unavailable",
        )

    def test_walletconnect_exact_probe_binds_both_network_routes_and_zero_effects(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        probe_result = subprocess.run(
            [
                _operator_binding('paths.node_binary'),
                'tests/fixtures/wallet-capability-fixture.mjs',
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            probe_result.returncode,
            0,
            probe_result.stdout + probe_result.stderr,
        )
        command = (
            _operator_command([_operator_binding('paths.node_binary'), 'scripts/company_alpha_walletconnect_agent.mjs', '--probe', '--json'])
        )
        for network, account in (
            ("testnet", _operator_binding('services.wallets.hedera_testnet.account_id')),
            ("mainnet", _operator_binding('services.wallets.hedera_mainnet.account_id')),
        ):
            with self.subTest(network=network):
                route_id = f"company-alpha-walletconnect-{network}"
                preferred = {
                    "route_id": route_id,
                    "system": route_id,
                    "required_principal": 'company-alpha',
                    "required_account": account,
                    "probe_id": f"{route_id}-probe",
                    "probe_safe_lane": "local-read",
                    "probe_command": command,
                }
                self.assertEqual(
                    resolver["exact_probe_argv"](preferred),
                    (
                        _operator_binding('paths.node_binary'),
                        'scripts/company_alpha_walletconnect_agent.mjs',
                        "--probe",
                        "--json",
                    ),
                )
                self.assertEqual(
                    resolver["parse_exact_probe_signal"](
                        preferred,
                        probe_result.stdout,
                    ),
                    (("read",), ("capability-probe",)),
                )

        drifted = json.loads(probe_result.stdout)
        drifted["zero_effects"]["session_paired"] = True
        self.assertIsNone(
            resolver["parse_exact_probe_signal"](
                {
                    "probe_id": 'company-alpha-walletconnect-testnet-probe',
                },
                json.dumps(drifted),
            )
        )

        drifted = json.loads(probe_result.stdout)
        drifted["capability"]["controls"]["semantic_calldata_decoder"] = True
        self.assertIsNone(
            resolver["parse_exact_probe_signal"](
                {
                    "probe_id": 'company-alpha-walletconnect-mainnet-probe',
                },
                json.dumps(drifted),
            )
        )

        drifted = json.loads(probe_result.stdout)
        drifted["capability"]["supported_chains"] = ["hedera:testnet"]
        self.assertIsNone(
            resolver["parse_exact_probe_signal"](
                {
                    "probe_id": 'company-alpha-walletconnect-mainnet-probe',
                },
                json.dumps(drifted),
            )
        )

    def test_walletconnect_routes_bind_to_company_alpha_not_test_harness(self) -> None:
        for network, account in (
            ("testnet", _operator_binding('services.wallets.hedera_testnet.account_id')),
            ("mainnet", _operator_binding('services.wallets.hedera_mainnet.account_id')),
        ):
            with self.subTest(network=network):
                route_id = f"company-alpha-walletconnect-{network}"
                result = self.run_resolver_result(
                    "--system",
                    route_id,
                    "--intent",
                    "read",
                    "--portfolio",
                    'company-alpha',
                    "--workspace",
                    'company-alpha-testing',
                    "--network",
                    f"hedera-{network}",
                    "--required-operation",
                    "capability-probe",
                    "--principal",
                    'company-alpha',
                    "--account",
                    account,
                    auto_bind=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    result.stdout + result.stderr,
                )
                payload = json.loads(result.stdout)
                self.assertEqual(payload["route_selection"]["state"], "selected")
                self.assertEqual(payload["preferred_lane"]["route_id"], route_id)
                self.assertEqual(
                    payload["route_binding"]["required_principal"], 'company-alpha'
                )
                self.assertTrue(payload["route_binding"]["binding_complete"])
                self.assertTrue(payload["route_binding"]["principal_matches"])
                self.assertTrue(payload["route_binding"]["account_matches"])

    def test_exact_probe_signal_and_ttl_fail_closed(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        preferred = {
            "route_id": "google-calendar-personal-gog",
            "probe_id": "google-calendar-personal-gog-probe",
            "probe_scope": "google-calendar-personal-gog",
            "probe_ttl_days": 1.0,
            "required_principal": 'operator',
            "required_account": _operator_binding('identifiers.accounts.personal_google'),
        }
        stale = resolver["AuthoritativeProbeEvidence"](
            route_id=preferred["route_id"],
            probe_id=preferred["probe_id"],
            principal=preferred["required_principal"],
            account=preferred["required_account"],
            state="passed",
            verified_at_utc=(
                datetime.now(timezone.utc) - timedelta(days=2)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            evidence_effects=("read",),
            evidence_operations=("calendar-list",),
        )
        contract = resolver["authoritative_probe_evidence_contract"](
            preferred, stale
        )
        self.assertFalse(contract["fresh"])
        self.assertFalse(contract["valid"])

        for payload in (
            {
                "calendars": [
                    {
                        "id": _operator_binding('identifiers.accounts.company_alpha_coordinator_google'),
                        "primary": True,
                        "accessRole": "owner",
                    }
                ]
            },
            {
                "calendars": [
                    {
                        "id": _operator_binding('identifiers.accounts.personal_google'),
                        "primary": True,
                        "accessRole": "reader",
                    }
                ]
            },
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(
                    resolver["parse_exact_probe_signal"](
                        preferred | {"probe_id": preferred["probe_id"]},
                        json.dumps(payload),
                    )
                )

    def test_exact_probe_and_sealed_operation_admit_calendar_mutation(
        self,
    ) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        result_type = resolver["ProbeCommandResult"]
        calls: list[tuple[str, ...]] = []

        def transport(argv: tuple[str, ...], _timeout: int):
            calls.append(argv)
            return result_type(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "id": _operator_binding('identifiers.accounts.personal_google'),
                                "primary": True,
                                "accessRole": "writer",
                            }
                        ]
                    }
                ),
            )

        def invoke(
            operation: str,
            *,
            account: str = _operator_binding('identifiers.accounts.personal_google'),
            run_probe: bool = True,
        ) -> tuple[int, dict]:
            args = [
                "--system",
                "google-calendar",
                "--intent",
                "write",
                "--context",
                "personal",
                "--required-operation",
                operation,
                "--principal",
                'operator',
                "--account",
                account,
            ]
            if run_probe:
                args.append("--run-exact-probe")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                returncode = resolver["main"](
                    args,
                    probe_transport=transport,
                )
            return returncode, json.loads(stdout.getvalue())

        default_code, default = invoke("event-create", run_probe=False)
        allowed_code, allowed = invoke("event-create")
        calls_after_allowed = list(calls)
        unknown_code, unknown = invoke("event-update")
        no_match_code, no_match = invoke(
            "event-create", account="nobody@example.invalid"
        )
        self.assertEqual(default_code, 0)
        self.assertTrue(default["execution_guard"]["allowed"])
        self.assertIsNone(default["execution_guard"]["blocker_code"])
        self.assertFalse(default["mutation_readiness"]["execution_prerequisite"])
        self.assertEqual(allowed_code, 0)
        self.assertEqual(unknown_code, 3)
        self.assertEqual(no_match_code, 2)
        self.assertEqual(calls, calls_after_allowed)
        self.assertIsNone(allowed["preferred_lane"]["provider_adapter"])
        self.assertNotIn("route_guard", allowed)
        self.assertTrue(allowed["execution_guard"]["allowed"])
        self.assertEqual(allowed["mutation_readiness"]["state"], "ready")
        self.assertFalse(allowed["mutation_readiness"]["execution_prerequisite"])
        self.assertTrue(allowed["mutation_readiness"]["completion_evidence_only"])
        self.assertEqual(
            allowed["authoritative_probe_evidence"]["evidence_operations"],
            ["calendar-list"],
        )
        self.assertFalse(unknown["execution_guard"]["allowed"])
        self.assertEqual(
            unknown["execution_guard"]["blocker_code"],
            "blocked_unknown_mutation_operation",
        )
        self.assertFalse(no_match["execution_guard"]["allowed"])
        self.assertEqual(
            no_match["execution_guard"]["blocker_code"],
            "blocked_no_exact_account_route",
        )

    def test_native_read_treats_malformed_status_as_missing_diagnostic(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        probe_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        base_status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        status_id = "Notion API route — personal workspace (`NOTION_API_KEY`)"
        fresh_time = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

        def load_with(status: dict) -> None:
            def fake_load_json(relative: str) -> dict:
                return {
                    "registry/integration_routes.json": route_registry,
                    "registry/probes.json": probe_registry,
                    "status/capability_status.json": status,
                }[relative]

            resolver["resolve"].__globals__["load_json"] = fake_load_json

        for invalid_slo in ("not-a-number", 366):
            with self.subTest(invalid_slo=invalid_slo):
                malformed = json.loads(json.dumps(base_status))
                malformed_row = next(
                    item
                    for item in malformed["capabilities"]
                    if item["capability_id"] == status_id
                )
                malformed_row.update(
                    {
                        "state": "ready",
                        "last_verified_utc": fresh_time,
                        "slo_max_age_days": invalid_slo,
                        "evidence_effects": ["read"],
                        "evidence_operations": ["page-read"],
                    }
                )
                load_with(malformed)
                diagnostic = resolver["resolve"](
                    "notion",
                    "read",
                    "personal",
                    required_operation="page-read",
                    requested_principal='operator',
                    requested_account='operator-personal-notion',
                )
                self.assertTrue(diagnostic["execution_guard"]["allowed"])
                self.assertEqual(diagnostic["route_readiness"]["state"], "unknown")
                self.assertFalse(diagnostic["route_readiness"]["exact_status_row"])
                self.assertTrue(diagnostic["route_readiness"]["diagnostic_only"])

        cross_operation = json.loads(json.dumps(base_status))
        cross_operation_row = next(
            item
            for item in cross_operation["capabilities"]
            if item["capability_id"] == status_id
        )
        cross_operation_row.update(
            {
                "state": "ready",
                "last_verified_utc": fresh_time,
                "evidence_effects": ["mutation"],
                "evidence_operations": ["page-update"],
            }
        )
        load_with(cross_operation)
        diagnostic = resolver["resolve"](
            "notion",
            "read",
            "personal",
            required_operation="page-read",
            requested_principal='operator',
            requested_account='operator-personal-notion',
        )
        self.assertFalse(diagnostic["route_readiness"]["evidence_suitable"])
        self.assertFalse(diagnostic["route_readiness"]["authoritative_passed"])
        self.assertTrue(diagnostic["route_readiness"]["diagnostic_only"])
        self.assertFalse(diagnostic["route_readiness"]["execution_prerequisite"])
        self.assertTrue(diagnostic["execution_guard"]["allowed"])
        self.assertIsNone(diagnostic["execution_guard"]["blocker_code"])

    def test_jira_operation_and_adapter_current_values_are_explicit(self) -> None:
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        jira = next(
            route
            for route in route_registry["routes"]
            if route["route_id"] == 'jira-company-alpha'
        )
        self.assertEqual(
            jira["operations"]["issue-create"],
            {
                "lane": "authenticated_ui",
                "effect": "mutation",
                "readiness_status_id": 'Mutation readiness — `jira-company-alpha`',
            },
        )
        adapter = jira["provider_adapter"]
        self.assertEqual(adapter["type"], "authenticated_account_route")
        self.assertEqual(adapter["required_account_route_field"], "required_account")
        self.assertEqual(
            adapter["identity_policy"],
            {
                "required": True,
                "normalizer": "casefold",
                "provider_account_digest_required": True,
            },
        )
        self.assertEqual(
            adapter["browser_evidence"]["completion_authority"],
            "runtime_authenticated_evidence_only",
        )
        self.assertEqual(
            adapter["browser_evidence"]["account_mismatch_blocker_code"],
            "blocked_requested_jira_route_unverified",
        )

    def test_mutation_readiness_is_typed_completion_diagnostic_not_permission(self) -> None:
        notion = self.run_resolver_result(
            "--system",
            "notion",
            "--intent",
            "write",
            "--context",
            "work",
            "--required-operation",
            "page-create",
        )
        self.assertEqual(notion.returncode, 0, notion.stdout + notion.stderr)
        notion_payload = json.loads(notion.stdout)
        self.assertEqual(
            notion_payload["mutation_readiness"]["readiness_status_id"],
            'Mutation readiness — `notion-company-alpha`',
        )
        self.assertTrue(
            notion_payload["mutation_readiness"]["operation_evidence_suitable"]
        )
        self.assertEqual(
            notion_payload["mutation_readiness"]["authoritative_passed"],
            not notion_payload["mutation_readiness"]["slo_breached"],
        )
        self.assertTrue(notion_payload["execution_guard"]["allowed"])
        self.assertFalse(
            notion_payload["mutation_readiness"]["execution_prerequisite"]
        )

        gmail = self.run_resolver_result(
            "--system",
            "gmail",
            "--intent",
            "write",
            "--context",
            "personal",
            "--required-operation",
            "gmail-send",
        )
        self.assertEqual(gmail.returncode, 0, gmail.stdout + gmail.stderr)
        gmail_payload = json.loads(gmail.stdout)
        self.assertEqual(
            gmail_payload["mutation_readiness"]["readiness_status_id"],
            'Gmail mutation readiness — personal `personal_google@example.invalid` via `gog`',
        )
        self.assertFalse(gmail_payload["mutation_readiness"]["authoritative_passed"])
        self.assertTrue(gmail_payload["execution_guard"]["allowed"])

        apple = self.run_resolver_result(
            "--system",
            "apple-calendar",
            "--intent",
            "create",
            "--context",
            "personal",
            "--required-operation",
            "event-create",
        )
        self.assertEqual(apple.returncode, 0, apple.stdout + apple.stderr)
        apple_payload = json.loads(apple.stdout)
        self.assertTrue(apple_payload["mutation_readiness"]["evidence_suitable"])
        self.assertTrue(apple_payload["mutation_readiness"]["slo_breached"])
        self.assertFalse(apple_payload["mutation_readiness"]["authoritative_passed"])
        self.assertTrue(apple_payload["execution_guard"]["allowed"])

        resolver = runpy.run_path(str(RESOLVER))
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        probe_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        base_status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        status_id = 'Mutation readiness — `notion-company-alpha`'
        fresh_time = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        fresh_time = fresh_time.replace("+00:00", "Z")
        for evidence_effects, expected_allowed in (
            ("mutation", False),
            (["read"], False),
            (["mutation"], True),
        ):
            with self.subTest(evidence_effects=evidence_effects):
                status = json.loads(json.dumps(base_status))
                row = next(
                    item
                    for item in status["capabilities"]
                    if item["capability_id"] == status_id
                )
                row.update(
                    {
                        "state": "ready",
                        "last_verified_utc": fresh_time,
                        "evidence_effects": evidence_effects,
                        "evidence_operations": ["page-create"],
                    }
                )

                def fake_load_json(relative: str) -> dict:
                    return {
                        "registry/integration_routes.json": route_registry,
                        "registry/probes.json": probe_registry,
                        "status/capability_status.json": status,
                    }[relative]

                resolver["resolve"].__globals__["load_json"] = fake_load_json
                resolve_args = {
                    "required_operation": "page-create",
                    "requested_principal": 'company-alpha',
                    "requested_account": 'company-alpha-team-notion',
                }
                payload = resolver["resolve"](
                    "notion", "write", "work", **resolve_args
                )
                self.assertTrue(payload["execution_guard"]["allowed"])
                self.assertEqual(
                    payload["mutation_readiness"]["evidence_suitable"],
                    expected_allowed,
                )
                self.assertFalse(
                    payload["mutation_readiness"]["execution_prerequisite"]
                )
                self.assertIsNone(payload["execution_guard"]["blocker_code"])

        status = json.loads(json.dumps(base_status))
        row = next(
            item
            for item in status["capabilities"]
            if item["capability_id"] == status_id
        )
        row.update(
            {
                "state": "ready",
                "last_verified_utc": fresh_time,
                "evidence_effects": ["mutation"],
                "evidence_operations": ["page-create"],
            }
        )
        drifted_probes = json.loads(json.dumps(probe_registry))
        notion_probe_id = next(
            route["access_probe_id"]
            for route in route_registry["routes"]
            if route["route_id"] == 'notion-company-alpha'
        )
        next(
            probe
            for probe in drifted_probes["probes"]
            if probe["probe_id"] == notion_probe_id
        )["scope"] = "notion"

        def fake_drifted_probe_load_json(relative: str) -> dict:
            return {
                "registry/integration_routes.json": route_registry,
                "registry/probes.json": drifted_probes,
                "status/capability_status.json": status,
            }[relative]

        resolver["resolve"].__globals__["load_json"] = fake_drifted_probe_load_json
        drifted = resolver["resolve"](
            "notion",
            "write",
            "work",
            required_operation="page-create",
            requested_principal='company-alpha',
            requested_account='company-alpha-team-notion',
        )
        self.assertTrue(drifted["execution_guard"]["allowed"])
        self.assertFalse(drifted["mutation_readiness"]["probe_scope_exact"])
        self.assertFalse(drifted["mutation_readiness"]["authoritative_passed"])

    def test_mutation_readiness_evidence_is_bound_to_exact_operation(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        probe_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        status_id = 'Mutation readiness — `notion-company-alpha`'
        row = next(
            item
            for item in status["capabilities"]
            if item["capability_id"] == status_id
        )
        row.update(
            {
                "state": "ready",
                "last_verified_utc": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "evidence_effects": ["mutation"],
                "evidence_operations": ["page-update"],
                "evidence": "exact page-update mutation and readback only",
            }
        )

        def fake_load_json(relative: str) -> dict:
            return {
                "registry/integration_routes.json": route_registry,
                "registry/probes.json": probe_registry,
                "status/capability_status.json": status,
            }[relative]

        resolver["resolve"].__globals__["load_json"] = fake_load_json
        page_update = resolver["resolve"](
            "notion",
            "write",
            "work",
            required_operation="page-update",
            requested_principal='company-alpha',
            requested_account='company-alpha-team-notion',
        )
        page_create = resolver["resolve"](
            "notion",
            "write",
            "work",
            required_operation="page-create",
            requested_principal='company-alpha',
            requested_account='company-alpha-team-notion',
        )

        self.assertTrue(page_update["mutation_readiness"]["authoritative_passed"])
        self.assertTrue(page_update["execution_guard"]["allowed"])
        self.assertIsNone(page_update["execution_guard"]["blocker_code"])
        self.assertEqual(
            page_update["mutation_readiness"]["evidence_operations"],
            ["page-update"],
        )
        self.assertFalse(page_create["mutation_readiness"]["evidence_suitable"])
        self.assertTrue(page_create["execution_guard"]["allowed"])
        self.assertIsNone(page_create["execution_guard"]["blocker_code"])
        self.assertFalse(page_create["mutation_readiness"]["execution_prerequisite"])
        for invalid_operations in (
            "page-update",
            ["page-update", "page-update"],
        ):
            with self.subTest(invalid_operations=invalid_operations):
                row["evidence_operations"] = invalid_operations
                malformed = resolver["resolve"](
                    "notion",
                    "write",
                    "work",
                    required_operation="page-update",
                    requested_principal='company-alpha',
                    requested_account='company-alpha-team-notion',
                )
                self.assertTrue(malformed["execution_guard"]["allowed"])
                self.assertEqual(
                    malformed["mutation_readiness"]["state"], "unknown"
                )
                self.assertFalse(
                    malformed["mutation_readiness"]["evidence_suitable"]
                )

        row["evidence_operations"] = ["page-delete"]
        unregistered = resolver["resolve"](
            "notion",
            "write",
            "work",
            required_operation="page-update",
            requested_principal='company-alpha',
            requested_account='company-alpha-team-notion',
        )
        self.assertFalse(unregistered["mutation_readiness"]["evidence_suitable"])
        self.assertTrue(unregistered["execution_guard"]["allowed"])

    def test_jira_digest_and_browser_identity_guard_use_current_route_values(self) -> None:
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        jira = next(
            route
            for route in route_registry["routes"]
            if route["route_id"] == 'jira-company-alpha'
        )
        expected_digest = (
            _operator_binding('services.jira.provider_identity_sha256')
        )
        self.assertEqual(
            jira["provider_adapter"]["native_evidence"][
                "provider_account_id_sha256"
            ],
            expected_digest,
        )
        result = self.run_resolver_result(
            "--system",
            "jira",
            "--intent",
            "create",
            "--required-operation",
            "issue-create",
            "--account",
            _operator_binding('services.jira.hostname'),
            "--candidate-lane",
            "browser",
            "--browser-account",
            "wrong.atlassian.net",
        )
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["route_guard"]["required_provider_account_id_sha256"],
            expected_digest,
        )
        self.assertEqual(
            payload["route_guard"]["blocker_code"],
            "blocked_requested_jira_route_unverified",
        )
        self.assertFalse(payload["route_guard"]["browser_account_verified"])
        self.assertFalse(payload["execution_guard"]["allowed"])

    def test_invalid_probe_ttl_becomes_missing_diagnostic(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        probe_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        next(
            probe
            for probe in probe_registry["probes"]
            if probe["probe_id"] == "google-calendar-personal-gog-probe"
        )["default_ttl"] = 0

        def fake_load_json(relative: str) -> dict:
            return {
                "registry/integration_routes.json": route_registry,
                "registry/probes.json": probe_registry,
                "status/capability_status.json": status,
            }[relative]

        resolver["resolve"].__globals__["load_json"] = fake_load_json
        payload = resolver["resolve"](
            "google-calendar",
            "write",
            "personal",
            required_operation="event-create",
            requested_principal='operator',
            requested_account=_operator_binding('identifiers.accounts.personal_google'),
        )
        self.assertTrue(payload["execution_guard"]["allowed"])
        self.assertIsNone(payload["preferred_lane"]["probe_command"])
        self.assertIsNone(payload["preferred_lane"]["probe_ttl_days"])
        self.assertFalse(payload["exact_probe_execution"]["supported"])

    def test_operation_and_route_identity_inputs_require_exact_canonical_labels(self) -> None:
        for operation in ("PAGE_READ", "page.read", " page-read!!! "):
            with self.subTest(operation=operation):
                result = self.run_resolver_result(
                    "--system",
                    "notion",
                    "--intent",
                    "read",
                    "--context",
                    "personal",
                    "--required-operation",
                    operation,
                )
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["required_operation"], operation)
                self.assertFalse(
                    payload["operation_contract"]["operation_label_canonical"]
                )
                self.assertFalse(
                    payload["operation_contract"]["operation_registered"]
                )
                self.assertEqual(
                    payload["execution_guard"]["blocker_code"],
                    "blocked_unknown_operation",
                )

        identity_cases = (
            (
                'OPERATOR!!!',
                'operator-personal-notion',
                "principal_evidence_canonical",
                "blocked_invalid_principal_selector",
            ),
            (
                'operator',
                ' operator-personal-notion ',
                "account_evidence_canonical",
                "blocked_invalid_account_selector",
            ),
        )
        for principal, account, canonical_field, blocker in identity_cases:
            with self.subTest(principal=principal, account=account):
                result = self.run_resolver_result(
                    "--system",
                    "notion",
                    "--intent",
                    "read",
                    "--context",
                    "personal",
                    "--required-operation",
                    "page-read",
                    "--principal",
                    principal,
                    "--account",
                    account,
                    auto_bind=False,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertFalse(payload["route_binding"][canonical_field])
                self.assertFalse(payload["route_binding"]["binding_complete"])
                self.assertEqual(payload["route_selection"]["state"], "invalid_selector")
                self.assertEqual(payload["execution_guard"]["blocker_code"], blocker)

    def test_valid_probe_ttl_change_needs_no_aggregate_registry_hash_update(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        base_probes = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        route = next(
            row
            for row in route_registry["routes"]
            if row["route_id"] == "google-calendar-personal-gog"
        )
        status_id = route["operations"]["event-create"]["readiness_status_id"]
        mutation_row = next(
            row
            for row in status["capabilities"]
            if row["capability_id"] == status_id
        )
        mutation_row.update(
            {
                "state": "ready",
                "last_verified_utc": (
                    datetime.now(timezone.utc) - timedelta(hours=2)
                )
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "evidence_effects": ["mutation"],
                "evidence_operations": ["event-create"],
            }
        )

        def install_probe_registry(probe_registry: dict) -> None:
            def fake_load_json(relative: str) -> dict:
                return {
                    "registry/integration_routes.json": route_registry,
                    "registry/probes.json": probe_registry,
                    "status/capability_status.json": status,
                }[relative]

            resolver["resolve"].__globals__["load_json"] = fake_load_json

        install_probe_registry(base_probes)
        baseline = resolver["resolve"](
            "google-calendar",
            "write",
            "personal",
            required_operation="event-create",
            requested_principal='operator',
            requested_account=_operator_binding('identifiers.accounts.personal_google'),
        )
        self.assertTrue(baseline["execution_guard"]["allowed"])
        self.assertTrue(baseline["mutation_readiness"]["slo_breached"])

        downgraded = json.loads(json.dumps(base_probes))
        probe = next(
            row
            for row in downgraded["probes"]
            if row["probe_id"] == route["access_probe_id"]
        )
        self.assertEqual(probe["default_ttl"], 3_600_000)
        probe["default_ttl"] = 86_400_000
        install_probe_registry(downgraded)
        updated = resolver["resolve"](
            "google-calendar",
            "write",
            "personal",
            required_operation="event-create",
            requested_principal='operator',
            requested_account=_operator_binding('identifiers.accounts.personal_google'),
        )
        self.assertTrue(updated["execution_guard"]["allowed"])
        self.assertFalse(updated["mutation_readiness"]["slo_breached"])
        self.assertEqual(
            updated["mutation_readiness"]["effective_slo_max_age_days"], 1.0
        )

    def test_malformed_probe_contract_becomes_missing_diagnostic(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        probe_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        next(
            row
            for row in probe_registry["probes"]
            if row["probe_id"] == "notion-personal-probe"
        )["command"] = None

        def fake_load_json(relative: str) -> dict:
            return {
                "registry/integration_routes.json": route_registry,
                "registry/probes.json": probe_registry,
                "status/capability_status.json": status,
            }[relative]

        resolver["resolve"].__globals__["load_json"] = fake_load_json
        payload = resolver["resolve"](
            "notion",
            "read",
            "personal",
            required_operation="page-read",
            requested_principal='operator',
            requested_account='operator-personal-notion',
        )
        self.assertTrue(payload["execution_guard"]["allowed"])
        self.assertIsNone(payload["preferred_lane"]["probe_command"])
        self.assertFalse(payload["exact_probe_execution"]["supported"])

        duplicate_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        selected_probe = next(
            row
            for row in duplicate_registry["probes"]
            if row["probe_id"] == "notion-personal-probe"
        )
        duplicate_registry["probes"].append(json.loads(json.dumps(selected_probe)))

        def fake_duplicate_load_json(relative: str) -> dict:
            return {
                "registry/integration_routes.json": route_registry,
                "registry/probes.json": duplicate_registry,
                "status/capability_status.json": status,
            }[relative]

        resolver["resolve"].__globals__["load_json"] = fake_duplicate_load_json
        duplicate = resolver["resolve"](
            "notion",
            "read",
            "personal",
            required_operation="page-read",
            requested_principal='operator',
            requested_account='operator-personal-notion',
        )
        self.assertTrue(duplicate["execution_guard"]["allowed"])
        self.assertIsNone(duplicate["preferred_lane"]["probe_command"])
        self.assertFalse(duplicate["exact_probe_execution"]["supported"])

    def test_unavailable_diagnostic_documents_do_not_veto_bound_routes(self) -> None:
        route_registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        probe_registry = json.loads(
            (ROOT / "registry" / "probes.json").read_text(encoding="utf-8")
        )
        status = json.loads(
            (ROOT / "status" / "capability_status.json").read_text(
                encoding="utf-8"
            )
        )
        for unavailable in (
            "registry/probes.json",
            "status/capability_status.json",
        ):
            with self.subTest(unavailable=unavailable):
                resolver = runpy.run_path(str(RESOLVER))

                def fake_load_json(relative: str) -> dict:
                    if relative == unavailable:
                        raise SystemExit(f"unavailable diagnostic: {relative}")
                    return {
                        "registry/integration_routes.json": route_registry,
                        "registry/probes.json": probe_registry,
                        "status/capability_status.json": status,
                    }[relative]

                resolver["resolve"].__globals__["load_json"] = fake_load_json
                payload = resolver["resolve"](
                    "notion",
                    "read",
                    "personal",
                    required_operation="page-read",
                    requested_principal='operator',
                    requested_account='operator-personal-notion',
                )
                self.assertTrue(payload["execution_guard"]["allowed"])
                if unavailable == "registry/probes.json":
                    self.assertIsNone(payload["preferred_lane"]["probe_command"])
                else:
                    self.assertEqual(payload["route_readiness"]["state"], "unknown")

    def test_duplicate_records_are_rejected_before_indexing(self) -> None:
        resolver = runpy.run_path(str(RESOLVER))
        duplicate = [{"route_id": "same"}, {"route_id": "same"}]
        with self.assertRaises(SystemExit):
            resolver["unique_record_index"](
                duplicate,
                key="route_id",
                source="registry/integration_routes.json",
            )


class CompactOutputTests(unittest.TestCase):
    """Exercise the CLI presentation boundary without contacting providers."""

    def setUp(self) -> None:
        self.resolver = runpy.run_path(str(RESOLVER))

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 14, 12, tzinfo=timezone.utc)

        clock = mock.patch.dict(
            self.resolver["main"].__globals__, {"datetime": FixedDatetime}
        )
        clock.start()
        self.addCleanup(clock.stop)

    def invoke(self, args, transport=None):
        def no_probe(argv, timeout):
            self.fail("Presentation alone must not run a provider probe")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.resolver["main"](
                args, probe_transport=transport or no_probe
            )
        return code, json.loads(stdout.getvalue()), stdout.getvalue()

    def assert_decision_unchanged(self, full, compact):
        for field in (
            "route_selection", "route_selection_blocker", "route_binding",
            "execution_guard", "route_guard", "identity_contract",
            "exact_probe_execution", "authoritative_probe_evidence",
            "route_readiness", "mutation_readiness", "browser_fallback_gate",
            "fallback_order", "constraints", "portfolio_authoritative_read_sources",
        ):
            self.assertEqual(full.get(field), compact.get(field), field)
        for field, value in compact["operation_contract"].items():
            if field != "operations":
                self.assertEqual(full["operation_contract"][field], value, field)

    def test_default_cli_and_resolve_keep_full_diagnostic_contract(self):
        args = ["--system", "google-workspace", "--intent", "read",
                "--required-operation", "calendar-read", "--portfolio", "company-beta"]
        code, full, text = self.invoke(args)
        expected = self.resolver["resolve"](
            "google-workspace", "read", "", required_operation="calendar-read",
            requested_portfolio="company-beta",
        )
        self.assertEqual(code, 0)
        self.assertEqual(full, expected)
        self.assertEqual(text, json.dumps(expected, indent=2, sort_keys=True) + "\n")
        self.assertEqual(self.invoke(args + ["--json"]), (code, full, text))
        self.assertEqual(len(full["checked_lanes"]), 6)
        self.assertNotIn("output_detail", full)

    def test_compact_source_read_set_and_destination_preserve_exact_routing(self):
        cases = [
            (["--system", "google-workspace", "--intent", "read",
              "--required-operation", "calendar-read", "--portfolio", "company-beta"],
             {_operator_binding("identifiers.accounts.company_beta_admin_google"), _operator_binding("identifiers.accounts.company_beta_operator_google")}, {"google-company-beta"}),
            (["--system", "google-calendar", "--intent", "write",
              "--required-operation", "event-create", "--portfolio", "personal",
              "--account", _operator_binding("identifiers.accounts.personal_google")],
             {_operator_binding("identifiers.accounts.personal_google")}, {"google-personal"}),
            (["--system", "apple-calendar", "--intent", "write",
              "--required-operation", "event-create", "--portfolio", "personal",
              "--account", "local-apple-calendar"],
             {"local-apple-calendar"}, {"apple-local"}),
        ]
        for args, accounts, workspaces in cases:
            with self.subTest(system=args[1]):
                code, full, text = self.invoke(args)
                compact_code, compact, compact_text = self.invoke(args + ["--compact"])
                self.assertEqual(compact_code, code)
                self.assertEqual(code, 0)
                self.assert_decision_unchanged(full, compact)
                self.assertEqual(compact["output_detail"], "compact")
                self.assertNotIn("checked_lanes", compact)
                lanes = ([compact["preferred_lane"]] if compact["preferred_lane"]
                         else compact["portfolio_read_set"]["lanes"])
                self.assertEqual({lane["required_account"] for lane in lanes}, accounts)
                self.assertEqual({value for lane in lanes for value in lane["workspace_ids"]}, workspaces)
                originals = {lane["route_id"]: lane for lane in full["checked_lanes"]}
                for lane in lanes:
                    original = originals[lane["route_id"]]
                    for field in ("required_principal", "required_account", "workspace_ids",
                                  "network_ids", "provider_adapter", "credential_handle_ids"):
                        self.assertEqual(lane[field], original[field])
                    self.assertEqual(lane["operations"], {
                        full["required_operation"]: original["operations"][full["required_operation"]]
                    })
                if compact["portfolio_read_set"]["applies"]:
                    self.assertLess(len(compact_text.encode()), len(text.encode()) // 2)

    def test_compact_omits_raw_status_evidence_without_mutating_full_payload(self):
        _, payload, _ = self.invoke([
            "--system", "google-calendar", "--intent", "read",
            "--required-operation", "event-list",
            "--account", _operator_binding("identifiers.accounts.personal_google"),
        ])
        payload["preferred_lane"]["readiness"]["evidence"] = "private-selected-status"
        for lane in payload["checked_lanes"]:
            lane["readiness"]["evidence"] = "private-unselected-status"
        original = json.dumps(payload, sort_keys=True)
        compact = self.resolver["compact_output"](payload)
        output = json.dumps(compact)
        self.assertNotIn("private-selected-status", output)
        self.assertNotIn("private-unselected-status", output)
        self.assertEqual(json.dumps(payload, sort_keys=True), original)
        self.assert_decision_unchanged(payload, compact)

    def test_compact_keeps_failure_and_browser_fallback_exit_status(self):
        base = ["--system", "google-calendar", "--intent", "read",
                "--required-operation", "event-list"]
        cases = [
            (base, 2),
            (base + ["--account", _operator_binding("identifiers.accounts.personal_google"), "--workspace", "google-company-beta"], 2),
            (["--system", "google-calendar", "--intent", "write",
              "--required-operation", "unregistered-mutation",
              "--account", _operator_binding("identifiers.accounts.personal_google")], 3),
            (base + ["--account", _operator_binding("identifiers.accounts.personal_google"),
                     "--candidate-lane", "browser", "--native-operation-support", "unsupported"], 3),
        ]
        for args, expected_code in cases:
            with self.subTest(args=args):
                code, full, _ = self.invoke(args)
                compact_code, compact, _ = self.invoke(args + ["--compact"])
                self.assertEqual(code, expected_code)
                self.assertEqual(compact_code, code)
                self.assert_decision_unchanged(full, compact)
                self.assertFalse(compact["browser_fallback_gate"]["provider_operation_allowed"])
                self.assertFalse(compact["browser_fallback_gate"]["completion_claim_allowed"])

    def test_compact_preserves_probe_outcomes_and_never_echoes_provider_body(self):
        args = ["--system", "google-calendar", "--intent", "read",
                "--required-operation", "calendar-list",
                "--account", _operator_binding("identifiers.accounts.personal_google"), "--run-exact-probe"]
        cases = [(0, "completed", "passed"),
                 (1, "completed", "blocked_exact_probe_failed"),
                 (1, "timeout", "blocked_exact_probe_timeout")]
        for returncode, outcome, expected in cases:
            calls = []

            def transport(argv, timeout):
                calls.append((argv, timeout))
                return self.resolver["ProbeCommandResult"](
                    returncode=returncode, outcome=outcome,
                    stdout=json.dumps({"calendars": [{
                        "id": _operator_binding("identifiers.accounts.personal_google"), "primary": True,
                        "accessRole": "owner", "summary": "private-calendar-title",
                    }]}),
                )

            with self.subTest(outcome=expected):
                code, full, _ = self.invoke(args, transport)
                compact_code, compact, text = self.invoke(args + ["--compact"], transport)
                self.assertEqual(compact_code, code)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0], calls[1])
                self.assertEqual(compact["exact_probe_execution"]["result"], expected)
                self.assertNotIn("private-calendar-title", text)
                self.assert_decision_unchanged(full, compact)


if __name__ == "__main__":
    unittest.main()
