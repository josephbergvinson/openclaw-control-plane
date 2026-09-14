#!/usr/bin/env python3
"""Resolve an exact capability route or bounded portfolio read routes.

This is a read-only execution primitive over the structured capability registry.
By default it returns the exact route or registered portfolio read set, probes,
constraints, and lane-order evidence an operator/agent must inspect. An explicit
``--run-exact-probe`` may execute one supported registry-bound read-only probe
without a shell; it never persists or accepts caller-supplied probe receipts.
Provider APIs, connectors, MCP integrations, and supported CLIs are all
first-class declared-native routes.
``--compact`` projects only the selected lanes and requested operation for agent
consumption; it does not change resolution, probe execution, or exit status.
"""

from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import argparse
import hashlib
import json
import os
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:
    from scripts.capability_registry_contract import (
        RegistryContractError,
        assert_integration_registry_contract,
        load_json_strict,
        loads_json_strict,
        parse_evidence_operations,
        parse_explicit_utc_timestamp,
        parse_probe_default_ttl_ms,
        validate_capability_status_record,
        validate_probe_record,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from capability_registry_contract import (  # type: ignore[no-redef]
        RegistryContractError,
        assert_integration_registry_contract,
        load_json_strict,
        loads_json_strict,
        parse_evidence_operations,
        parse_explicit_utc_timestamp,
        parse_probe_default_ttl_ms,
        validate_capability_status_record,
        validate_probe_record,
    )

ROOT = Path(__file__).resolve().parents[1]
TRUSTED_GOG_ENV_FILE = Path(
    _operator_binding('paths.gog_env_file')
)
TRUSTED_GOG_BIN = Path(_operator_binding('paths.gog_binary'))

INTENT_ALIASES = {
    "read": "read",
    "search": "read_search",
    "read_search": "read_search",
    "write": "write",
    "create": "write",
    "update": "write",
    "send": "write",
    "mutate": "write",
}

WRITE_INTENTS = {"write", "create", "update", "send", "mutate"}
READ_INTENTS = {"read", "search", "read_search"}
OPERATION_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


@dataclass(frozen=True)
class ResolvedProbe:
    probe_id: str
    command: str
    expected_signal: str
    safe_lane: str
    notes: str
    default_ttl_ms: int
    scope: str


@dataclass(frozen=True)
class AuthoritativeProbeEvidence:
    """Runtime-owned result from one exact registered account probe."""

    route_id: str
    probe_id: str
    principal: str
    account: str
    state: str
    verified_at_utc: str
    evidence_effects: tuple[str, ...]
    evidence_operations: tuple[str, ...]


@dataclass(frozen=True)
class ProbeCommandResult:
    """Redaction-safe result returned by the exact probe transport."""

    returncode: int
    stdout: str
    stderr: str = ""
    outcome: str = "completed"


@dataclass(frozen=True)
class NormalizedRouteSelectors:
    """Explicit selection metadata; none of these fields grants authority."""

    context: str
    portfolio: str
    workspace: str
    network: str
    principal: str
    account: str
    context_supplied: bool
    portfolio_supplied: bool
    workspace_supplied: bool
    network_supplied: bool
    principal_supplied: bool
    account_supplied: bool
    context_canonical: bool
    portfolio_canonical: bool
    workspace_canonical: bool
    network_canonical: bool
    principal_canonical: bool
    account_canonical: bool


@dataclass(frozen=True)
class RouteSelection:
    preferred: dict[str, Any] | None
    blocker: str | None
    state: str
    mode: str
    system_candidate_route_ids: tuple[str, ...]
    matched_route_ids: tuple[str, ...]
    applied_selectors: tuple[str, ...]
    overridden_selectors: tuple[str, ...]
    missing_discriminators: tuple[str, ...]


ProbeTransport = Callable[[tuple[str, ...], int], ProbeCommandResult]
MAX_PROBE_OUTPUT_BYTES = 1024 * 1024
EXACT_PROBE_TIMEOUT_SECONDS = 60
PROBE_TERMINATE_GRACE_SECONDS = 1.0
PROBE_GROUP_POLL_SECONDS = 0.01
EXACT_PROBE_BASE_ENVIRONMENT = {
    "HOME": _operator_binding('paths.host_home'),
    "LC_ALL": "C",
    "LOGNAME": _operator_binding("identifiers.host_user"),
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "TMPDIR": "/tmp",
    "USER": _operator_binding("identifiers.host_user"),
}
GOOGLE_CALENDAR_PROBE_IDS = frozenset(
    {
        "google-calendar-personal-gog-probe",
        'google-calendar-company-alpha-coordinator-gog-probe',
    }
)
GOG_AUTH_LIST_PROBE_ARGV = (
    _operator_binding('paths.gog_binary'),
    "auth",
    "list",
    "--check",
    "--json",
)
APPLE_CALENDAR_LOCAL_PROBE_ID = "apple-calendar-local-probe"
APPLE_CALENDAR_LOCAL_PROBE_ARGV = (
    _operator_binding('paths.python_binary'),
    "-E",
    "-s",
    "scripts/apple_calendar_probe.py",
)
JIRA_COMPANY_ALPHA_PROBE_ID = 'jira-company-alpha-probe'
JIRA_COMPANY_ALPHA_PROBE_ARGV = (
    _operator_binding('paths.python_binary'),
    "-E",
    "-s",
    'scripts/jira_company_alpha_capability_probe.py',
)
GOOGLE_SEARCH_CONSOLE_PERSONAL_PROBE_ID = (
    "google-search-console-personal-gog-probe"
)
GOOGLE_SEARCH_CONSOLE_PERSONAL_DECLARED_PROBE_ARGV = (
    "python3",
    "scripts/google_search_console_probe.py",
    "--account",
    _operator_binding('identifiers.accounts.personal_google'),
    "--receipt-path",
    "artifacts/CapabilityReceipts/google-search-console-personal.json",
    "--json",
)
GOOGLE_SEARCH_CONSOLE_PERSONAL_PROBE_ARGV = (
    _operator_binding('paths.python_binary'),
    "-E",
    "-s",
    *GOOGLE_SEARCH_CONSOLE_PERSONAL_DECLARED_PROBE_ARGV[1:],
)
GOOGLE_SEARCH_CONSOLE_PERSONAL_SUCCESS_SIGNAL_FIELDS = frozenset(
    {
        "schema",
        "producer",
        "checked_at_utc",
        "account",
        "credential_env",
        "operation",
        "mutating",
        "evidence_role",
        "authoritative",
        "completion_claim_allowed",
        "page_indexing_example_table_verified",
        "status",
        "reason_code",
        "property_accessible",
        "requested_property_domain",
        "gog_exit_code",
        "matching_property_count",
        "authorized_matching_property_count",
        "property_selection_required",
        "authorized_property_types",
        "evidence",
    }
)
NOTION_ROUTE_PROBES = {
    "notion-personal-probe": {
        "route_id": "notion-personal",
        "principal": 'operator',
        "account": 'operator-personal-notion',
        "route": "personal",
        "credential_handle_id": "notion.personal.api",
    },
    'notion-company-alpha-probe': {
        "route_id": 'notion-company-alpha',
        "principal": 'company-alpha',
        "account": 'company-alpha-team-notion',
        "route": 'company-alpha',
        "credential_handle_id": 'notion.company_alpha.api',
    },
}
NOTION_WORKSPACE_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
JIRA_REQUIRED_FIELD_NAMES = (
    "Description",
    "Labels",
    "Platform",
    "Reporter",
    "Summary",
    "Tester Name",
)
JIRA_SUCCESS_SIGNAL_FIELDS = frozenset(
    {
        "route",
        "system",
        "credential_handle_id",
        "site_expected",
        "project_key_expected",
        "issue_type_expected",
        "credential_contract_bound",
        "site_matches_expected",
        "auth_ok",
        "account_identity_matches_registered",
        "account_type",
        "project_ok",
        "project_id",
        "project_key",
        "project_name",
        "createmeta_ok",
        "qa_feedback_issue_type_ok",
        "qa_feedback_issue_type_id",
        "visible_required_field_names",
        "missing_required_field_names",
        "duplicate_required_field_names",
        "field_ids",
        "ok",
    }
)
JIRA_PROVIDER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]*")
GIGABRAIN_PROBE_ID = 'company-alpha-gigabrain-metabase-api-probe'
GIGABRAIN_PROBE_ARGV = (
    _operator_binding('paths.python_binary'),
    "-E",
    "-s",
    'scripts/company_alpha_gigabrain_capability_probe.py',
    "--json",
)
GIGABRAIN_SUCCESS_SIGNAL_FIELDS = frozenset(
    {
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
)
TRUSTED_OPENCLAW_NODE_BIN = Path(
    _operator_binding('paths.node_binary')
)
COMPANY_ALPHA_WALLETCONNECT_PROBE_ARGV = (
    str(TRUSTED_OPENCLAW_NODE_BIN),
    'scripts/company_alpha_walletconnect_agent.mjs',
    "--probe",
    "--json",
)
COMPANY_ALPHA_WALLETCONNECT_ROUTE_PROBES = {
    'company-alpha-walletconnect-testnet-probe': {
        "route_id": 'company-alpha-walletconnect-testnet',
        "account_id": _operator_binding('services.wallets.hedera_testnet.account_id'),
        "chain_id": "hedera:testnet",
    },
    'company-alpha-walletconnect-mainnet-probe': {
        "route_id": 'company-alpha-walletconnect-mainnet',
        "account_id": _operator_binding('services.wallets.hedera_mainnet.account_id'),
        "chain_id": "hedera:mainnet",
    },
}
COMPANY_ALPHA_DOCS_PROBE_ID = 'company-alpha-docs-mcp-readonly-probe'
COMPANY_ALPHA_DOCS_ROUTE_ID = 'company-alpha-docs-mcp-readonly'
COMPANY_ALPHA_DOCS_DECLARED_PROBE_ARGV = (
    "python3",
    'scripts/company_alpha_docs_mcp_adapter.py',
    "--manifest",
    'config/company_alpha_docs_mcp.json',
    "--probe",
)
COMPANY_ALPHA_DOCS_PROBE_ARGV = (
    _operator_binding('paths.python_binary'),
    "-E",
    "-s",
    *COMPANY_ALPHA_DOCS_DECLARED_PROBE_ARGV[1:],
)
CLOUDFLARE_PROBE_ID = 'cloudflare-operator-readonly-probe'
CLOUDFLARE_DECLARED_PROBE_ARGV = (
    "python3",
    "scripts/cloudflare_capability_probe.py",
)
CLOUDFLARE_PROBE_ARGV = (
    _operator_binding('paths.python_binary'),
    "-E",
    "-s",
    "scripts/cloudflare_capability_probe.py",
)
TRELLO_PROBE_ID = "trello-personal-api-probe"
TRELLO_DECLARED_PROBE_ARGV = (
    "python3",
    "scripts/trello_capability_probe.py",
    "--board-id",
    _operator_binding('services.trello.board_id'),
)
TRELLO_PROBE_ARGV = (
    _operator_binding('paths.python_binary'),
    "-E",
    "-s",
    "scripts/trello_capability_probe.py",
    "--board-id",
    _operator_binding('services.trello.board_id'),
)
GITHUB_PROBE_ID = "github-personal-cli-probe"
GITHUB_PROBE_ARGV = (
    _operator_binding('paths.github_binary'),
    "auth",
    "status",
    "--hostname",
    "github.com",
)
HEDERA_SIGNER_ROUTE_PROBES = {
    "openclaw-hedera-mainnet-test-signer-probe": {
        "route_id": "openclaw-hedera-mainnet-test-signer",
        "system": "hedera-mainnet-test-signer",
        "account_id": _operator_binding('services.wallets.hedera_mainnet.account_id'),
        "network": "mainnet",
        "ledger": "hedera-mainnet",
        "schema": "openclaw.hedera_mainnet_test_signer_probe.v1",
        "classification": (
            "general_openclaw_hedera_mainnet_disposable_test_signer"
        ),
        "script": "scripts/hedera_mainnet_test_signer_probe.mjs",
        "secret_path": (
            _operator_binding('paths.routing_openclaw_hedera_mainnet_test_account_env')
        ),
        "secret_variables": (
            "OPENCLAW_HEDERA_MAINNET_TEST_ACCOUNT_ID",
            "OPENCLAW_HEDERA_MAINNET_TEST_PRIVATE_KEY",
            "OPENCLAW_HEDERA_MAINNET_TEST_KEY_TYPE",
        ),
        "key_type": "ED25519",
        "private_key_length": 96,
    },
    "openclaw-hedera-testnet-test-signer-probe": {
        "route_id": "openclaw-hedera-testnet-test-signer",
        "system": "hedera-testnet-test-signer",
        "account_id": _operator_binding('services.wallets.hedera_testnet.account_id'),
        "network": "testnet",
        "ledger": "hedera-testnet",
        "schema": "openclaw.hedera_testnet_test_signer_probe.v1",
        "classification": (
            "general_openclaw_hedera_testnet_disposable_test_signer"
        ),
        "script": "scripts/hedera_testnet_test_signer_probe.mjs",
        "secret_path": (
            _operator_binding('paths.routing_openclaw_hedera_testnet_test_account_env')
        ),
        "secret_variables": (
            "OPENCLAW_HEDERA_TESTNET_TEST_ACCOUNT_ID",
            "OPENCLAW_HEDERA_TESTNET_TEST_PRIVATE_KEY",
        ),
        "key_type": "ECDSA_SECP256K1",
        "private_key_length": 64,
    },
}
X_API_PROBE_ID = 'x-api-operator-x-xurl-probe'
X_API_DECLARED_PROBE_ARGV = (
    _operator_binding('paths.routing_xurl'),
    "whoami",
)
X_API_PROBE_ARGV = (
    str(TRUSTED_OPENCLAW_NODE_BIN),
    (
        _operator_binding('paths.routing_cli_js')
    ),
    "whoami",
)
TRUSTED_OPENCLAW_CLI = _operator_binding('paths.openclaw_cli')
OPENCLAW_RUNTIME_STATE_DIR = _operator_binding('paths.state_root')
DISCORD_SOURCE_NODE_BIN = Path(
    _operator_binding('paths.node_binary')
)
# Keep readiness output bounded: beta.3 can exit before a large piped guild
# inventory flushes. Task-specific lists, reads, and searches remain available.
DISCORD_SOURCE_ROUTE_PROBES = {
    'discord-source-company-alpha-team-probe': {
        "route_id": 'discord-source-company-alpha-team',
        "principal": 'company-alpha',
        "guild_id": _operator_binding('services.discord.company_alpha.guild_id'),
        "channel_id": _operator_binding('services.discord.company_alpha.channel_id'),
    },
    'discord-source-company-beta-team-probe': {
        "route_id": 'discord-source-company-beta-team',
        "principal": 'company-beta',
        "guild_id": _operator_binding('services.discord.company_beta.guild_id'),
        "channel_id": _operator_binding('services.discord.company_beta.channel_id'),
    },
    'discord-source-operations-team-probe': {
        "route_id": 'discord-source-operations-team',
        "principal": 'operations',
        "guild_id": _operator_binding('services.discord.operations.guild_id'),
        "channel_id": _operator_binding('services.discord.operations.channel_id'),
    },
}
DISCORD_SNOWFLAKE_PATTERN = re.compile(r"[0-9]{17,20}")
MERCURY_COMPANY_BETA_PROBE_ID = 'mercury-company-beta-mcp-oauth-probe'
MERCURY_COMPANY_BETA_SERVER_ID = 'mercury-company-beta'
MERCURY_COMPANY_BETA_SERVER_URL = "https://mcp.mercury.com/mcp"
MERCURY_COMPANY_BETA_DECLARED_PROBE_ARGV = (
    "openclaw",
    "mcp",
    "probe",
    MERCURY_COMPANY_BETA_SERVER_ID,
    "--json",
)
MERCURY_COMPANY_BETA_PROBE_ARGV = (
    "/usr/bin/env",
    f"OPENCLAW_STATE_DIR={OPENCLAW_RUNTIME_STATE_DIR}",
    str(DISCORD_SOURCE_NODE_BIN),
    TRUSTED_OPENCLAW_CLI,
    *MERCURY_COMPANY_BETA_DECLARED_PROBE_ARGV[1:],
)
MERCURY_COMPANY_BETA_TOOL_NAMES = frozenset(
    {
        'mercury-company-beta__getAccount',
        'mercury-company-beta__getAccountCards',
        'mercury-company-beta__getAccountStatements',
        'mercury-company-beta__getAccounts',
        'mercury-company-beta__getAttachment',
        'mercury-company-beta__getCard',
        'mercury-company-beta__getCurrentDate',
        'mercury-company-beta__getCustomer',
        'mercury-company-beta__getInvoice',
        'mercury-company-beta__getOrganization',
        'mercury-company-beta__getRecipient',
        'mercury-company-beta__getRecipientInvite',
        'mercury-company-beta__getRecipients',
        'mercury-company-beta__getSafeRequest',
        'mercury-company-beta__getSafeRequests',
        'mercury-company-beta__getTransaction',
        'mercury-company-beta__getTransactionById',
        'mercury-company-beta__getTransferMoneyApprovalRequest',
        'mercury-company-beta__getTreasury',
        'mercury-company-beta__getTreasuryTransactions',
        'mercury-company-beta__getUser',
        'mercury-company-beta__getUsers',
        'mercury-company-beta__getWebhook',
        'mercury-company-beta__getWebhooks',
        'mercury-company-beta__listCards',
        'mercury-company-beta__listCategories',
        'mercury-company-beta__listCredit',
        'mercury-company-beta__listCustomers',
        'mercury-company-beta__listInvoiceAttachments',
        'mercury-company-beta__listInvoices',
        'mercury-company-beta__listMerchants',
        'mercury-company-beta__listRecipientInvites',
        'mercury-company-beta__listRecipientsAttachments',
        'mercury-company-beta__listSendMoneyApprovalRequests',
        'mercury-company-beta__listTransactions',
        'mercury-company-beta__listTransferMoneyApprovalRequests',
    }
)
PERSONAL_DATA_NEON_PROBE_ID = 'personal-data-neon-postgres-readonly-probe'
PERSONAL_DATA_NEON_DECLARED_PROBE_ARGV = (
    "python3",
    'scripts/personal_data_neon_readonly_capability_probe.py',
    "--json",
)
PERSONAL_DATA_NEON_PROBE_ARGV = (
    _operator_binding('paths.python_binary'),
    'scripts/personal_data_neon_readonly_capability_probe.py',
    "--json",
)


def load_json(rel: str) -> dict[str, Any]:
    path = ROOT / rel
    try:
        payload = load_json_strict(path, source=rel)
    except RegistryContractError as exc:
        raise SystemExit(str(exc)) from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"invalid {rel}: top-level value must be an object")
    return payload


def unique_record_index(
    records: Any,
    *,
    key: str,
    source: str,
) -> dict[str, dict[str, Any]]:
    """Parse one canonical record list and reject ambiguity before indexing."""

    if not isinstance(records, list):
        raise SystemExit(f"invalid {source}: expected a list of records")
    index: dict[str, dict[str, Any]] = {}
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise SystemExit(
                f"invalid {source}: record {position} must be an object"
            )
        value = record.get(key)
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
        ):
            raise SystemExit(
                f"invalid {source}: record {position} has no canonical {key}"
            )
        if value in index:
            raise SystemExit(f"invalid {source}: duplicate {key} {value!r}")
        index[value] = record
    return index


def normalize_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def normalize_intent(value: str) -> str:
    raw = normalize_slug(value).replace("-", "_")
    return INTENT_ALIASES.get(raw, raw)


def system_candidates(system: str, aliases: dict[str, Any]) -> list[str]:
    slug = normalize_slug(system)
    declared = aliases.get(slug)
    if not isinstance(declared, list):
        return [slug]
    candidates = [normalize_slug(str(value)) for value in declared if str(value).strip()]
    return candidates or [slug]


def field_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            stripped = stripped[1:-1]
        return [part.strip().strip('"\'').lower() for part in stripped.split(",") if part.strip()]
    return []


def normalize_route_selectors(
    *,
    context: str,
    portfolio: str,
    workspace: str,
    network: str,
    principal: str,
    account: str,
) -> NormalizedRouteSelectors:
    stripped_account = account.strip()
    normalized_portfolio = normalize_slug(portfolio)
    normalized_workspace = normalize_slug(workspace)
    normalized_network = normalize_slug(network)
    normalized_principal = normalize_slug(principal)
    return NormalizedRouteSelectors(
        context=normalize_slug(context),
        portfolio=normalized_portfolio,
        workspace=normalized_workspace,
        network=normalized_network,
        principal=normalized_principal,
        account=stripped_account,
        context_supplied=bool(context),
        portfolio_supplied=bool(portfolio),
        workspace_supplied=bool(workspace),
        network_supplied=bool(network),
        principal_supplied=bool(principal),
        account_supplied=bool(account),
        context_canonical=bool(not context or context == normalize_slug(context)),
        portfolio_canonical=bool(
            not portfolio or portfolio == normalized_portfolio
        ),
        workspace_canonical=bool(
            not workspace or workspace == normalized_workspace
        ),
        network_canonical=bool(not network or network == normalized_network),
        principal_canonical=bool(
            not principal or principal == normalized_principal
        ),
        account_canonical=bool(not account or account == stripped_account),
    )


def portfolio_route_index(
    registry: dict[str, Any],
    *,
    route_ids: set[str],
) -> dict[str, dict[str, frozenset[str]]]:
    """Normalize selection metadata after the shared registry contract passes."""

    try:
        bindings = registry["portfolio_routes"]["by_route_id"]
    except (KeyError, TypeError) as exc:
        raise SystemExit(
            "invalid registry/integration_routes.json: portfolio route index absent"
        ) from exc
    if not isinstance(bindings, dict) or set(bindings) != route_ids:
        raise SystemExit(
            "invalid registry/integration_routes.json: portfolio route coverage "
            "must exactly match declared routes"
        )
    result: dict[str, dict[str, frozenset[str]]] = {}
    fields = (
        "portfolio_ids",
        "workspace_ids",
        "network_ids",
        "default_for_portfolios",
    )
    for route_id, binding in bindings.items():
        if not isinstance(binding, dict):
            raise SystemExit(
                "invalid registry/integration_routes.json: portfolio route "
                f"{route_id!r} selection metadata must be an object"
            )
        normalized: dict[str, frozenset[str]] = {}
        for field in fields:
            values = binding.get(field)
            if not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ):
                raise SystemExit(
                    "invalid registry/integration_routes.json: portfolio route "
                    f"{route_id!r} field {field!r} must be a string list"
                )
            normalized[field] = frozenset(values)
        result[route_id] = normalized
    return result


def portfolio_authoritative_read_sources(
    registry: dict[str, Any],
    *,
    portfolio: str,
) -> tuple[dict[str, Any], ...]:
    """Return the exact public/local source locators bound to one portfolio."""

    portfolios = registry.get("portfolios")
    if not portfolio or not isinstance(portfolios, list):
        return ()
    selected = next(
        (
            row
            for row in portfolios
            if isinstance(row, dict) and row.get("portfolio_id") == portfolio
        ),
        None,
    )
    if not isinstance(selected, dict):
        return ()
    sources = selected.get("authoritative_read_sources", [])
    if not isinstance(sources, list):
        return ()
    return tuple(
        {
            "source_id": source["source_id"],
            "canonical_url": source["canonical_url"],
            "canonical_local_root": source["canonical_local_root"],
            "registered_route_ids": list(source["registered_route_ids"]),
        }
        for source in sources
        if isinstance(source, dict)
    )


def credential_handle_route_index(
    registry: dict[str, Any],
    *,
    route_ids: set[str],
) -> dict[str, tuple[str, ...]]:
    """Project opaque credential handles onto their declared route consumers."""

    handles_by_route: dict[str, list[str]] = {
        route_id: [] for route_id in route_ids
    }
    for handle in registry["credential_handles"]:
        handle_id = handle["handle_id"]
        for consumer in handle["consumers"]:
            if consumer["kind"] != "integration-route":
                continue
            handles_by_route[consumer["consumer_id"]].append(handle_id)
    return {
        route_id: tuple(sorted(handle_ids))
        for route_id, handle_ids in handles_by_route.items()
    }


def provider_account_digest_contract(
    identity_policy: dict[str, Any],
    native_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Validate the generic provider-account digest contract."""

    required_value = identity_policy.get(
        "provider_account_digest_required", False
    )
    required = required_value is True
    digest = native_evidence.get("provider_account_id_sha256")
    present = digest is not None
    valid = bool(
        isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
    )
    contract_valid = bool(
        isinstance(required_value, bool)
        and ((not required and not present) or valid)
    )
    return {
        "required": required,
        "digest": digest if valid else None,
        "valid": valid,
        "contract_valid": contract_valid,
    }


def route_identity_contract(
    preferred: dict[str, Any] | None,
    *,
    browser_account: str,
) -> dict[str, Any]:
    """Enforce route-owned account, adapter, and digest invariants."""

    adapter = preferred.get("provider_adapter") if preferred else None
    required_account = (
        preferred.get("required_account") if preferred else None
    )
    adapter_type = adapter.get("type") if isinstance(adapter, dict) else None
    provider_adapter_required = adapter is not None
    provider_account_digest_required = False
    identity_normalizer = "exact"
    mode = "route-account" if adapter is None else None
    adapter_shape_valid = adapter is None or isinstance(adapter, dict)
    adapter_identity_valid = adapter is None
    digest_contract_valid = adapter is None
    if isinstance(adapter, dict):
        identity_policy = adapter.get("identity_policy")
        native_evidence = adapter.get("native_evidence")
        if not isinstance(identity_policy, dict):
            identity_policy = {}
        if not isinstance(native_evidence, dict):
            native_evidence = {}
        declared_normalizer = identity_policy.get("normalizer")
        if declared_normalizer in {"exact", "casefold"}:
            identity_normalizer = str(declared_normalizer)
        adapter_digest_required = identity_policy.get(
            "provider_account_digest_required", False
        )
        provider_account_digest_required = adapter_digest_required is True
        if adapter_type == "authenticated_account_route":
            mode = (
                "adapter-account-digest"
                if provider_account_digest_required
                else "adapter-account"
            )
        elif adapter_type == "authenticated_provider_evidence":
            mode = "adapter-provider-evidence"
        adapter_identity_valid = bool(
            adapter_type
            in {"authenticated_account_route", "authenticated_provider_evidence"}
            and identity_policy.get("required") is True
            and identity_policy.get("normalizer") in {"exact", "casefold"}
            and adapter.get("required_account_route_field")
            == "required_account"
            and isinstance(adapter_digest_required, bool)
        )
        digest_contract_valid = provider_account_digest_contract(
            identity_policy, native_evidence
        )["contract_valid"]

    required_account_valid = bool(
        isinstance(required_account, str)
        and required_account.strip()
        and required_account == required_account.strip()
    )
    normalized_required_account = (
        normalize_route_identity(str(required_account), identity_normalizer)
        if required_account_valid
        else None
    )
    normalized_browser_account = (
        normalize_route_identity(browser_account, identity_normalizer)
        if browser_account and browser_account == browser_account.strip()
        else None
    )
    browser_account_matches_required = bool(
        required_account_valid
        and normalized_browser_account == normalized_required_account
    )
    contract_valid = bool(
        mode is not None
        and adapter_shape_valid
        and adapter_identity_valid
        and digest_contract_valid
        and required_account_valid
    )
    return {
        "mode": mode,
        "provider_adapter_type": adapter_type,
        "valid": contract_valid,
        "provider_adapter_required": provider_adapter_required,
        "provider_account_digest_required": provider_account_digest_required,
        "browser_account_evidence_required": True,
        "browser_account_evidence_provided": normalized_browser_account is not None,
        "browser_account": normalized_browser_account,
        "browser_account_matches_required": browser_account_matches_required,
        "required_account": normalized_required_account,
    }


def route_operations(
    preferred: dict[str, Any] | None,
) -> dict[str, dict[str, str]]:
    """Return the one sealed route-owned operation/lane/effect contract."""

    if preferred is None:
        return {}
    raw = preferred.get("operations")
    route_id = preferred.get("route_id", "<unknown>")
    if not isinstance(raw, dict) or not raw:
        raise SystemExit(f"invalid route operations: {route_id}")
    operations: dict[str, dict[str, str]] = {}
    for operation, spec in raw.items():
        if (
            not isinstance(operation, str)
            or OPERATION_PATTERN.fullmatch(operation) is None
            or not isinstance(spec, dict)
        ):
            raise SystemExit(f"invalid route operation contract: {route_id}")
        lane = spec.get("lane")
        effect = spec.get("effect")
        expected_fields = (
            {"lane", "effect", "readiness_status_id"}
            if effect == "mutation"
            else {"lane", "effect"}
        )
        status_id = spec.get("readiness_status_id")
        if (
            set(spec) != expected_fields
            or lane not in {"declared_native", "authenticated_ui"}
            or effect not in {"read", "mutation"}
            or (
                effect == "mutation"
                and (
                    not isinstance(status_id, str)
                    or not status_id.strip()
                    or status_id != status_id.strip()
                )
            )
        ):
            raise SystemExit(
                f"invalid route operation contract: {route_id}:{operation}"
            )
        operations[operation] = dict(spec)
    return operations


def operation_route_contract(
    preferred: dict[str, Any] | None,
    *,
    intent: str,
    required_operation: str,
    native_operation_support: str,
    authenticated_ui_required: bool,
    native_lane_kinds: set[str] | None = None,
) -> dict[str, Any]:
    """Classify an operation from declarative route fields and current evidence.

    Provider-specific operation names belong in the route registry. The resolver
    interprets only the generic supported/UI-required/read-only contract.
    """

    requested_operation = required_operation or None
    normalized_operation = (
        required_operation
        if OPERATION_PATTERN.fullmatch(required_operation) is not None
        else None
    )
    operation_label_canonical = normalized_operation is not None
    registered_operation_contracts = route_operations(preferred)
    native_supported_operations = {
        operation
        for operation, spec in registered_operation_contracts.items()
        if spec["lane"] == "declared_native"
    }
    ui_required_operations = {
        operation
        for operation, spec in registered_operation_contracts.items()
        if spec["lane"] == "authenticated_ui"
    }
    read_operations = {
        operation
        for operation, spec in registered_operation_contracts.items()
        if spec["effect"] == "read"
    }
    mutation_status_ids = {
        operation: spec["readiness_status_id"]
        for operation, spec in registered_operation_contracts.items()
        if spec["effect"] == "mutation"
    }
    mutation_operations = set(mutation_status_ids)
    capability_scope = (
        normalize_slug(str(preferred.get("capability_scope", "")))
        if preferred
        else ""
    )

    native_lane_kind = (
        normalize_slug(str(preferred.get("native_lane_kind", "")))
        if preferred
        else ""
    )
    declared_native_lane_kinds = native_lane_kinds or set()
    native_route_available = bool(
        preferred
        and native_lane_kind
        and native_lane_kind in declared_native_lane_kinds
    )
    operation_effect = (
        "read"
        if normalized_operation in read_operations
        else (
            "mutation"
            if normalized_operation in mutation_operations
            else None
        )
    )
    operation_registered = operation_effect is not None
    intent_effect = (
        "read"
        if intent in READ_INTENTS
        else "mutation" if intent in WRITE_INTENTS else None
    )
    intent_known = intent_effect is not None
    intent_effect_mismatch = bool(
        operation_effect and intent_effect and operation_effect != intent_effect
    )
    unknown_operation = not operation_registered
    unknown_mutation = bool(intent_effect == "mutation" and unknown_operation)
    operation_declared_mutation = operation_effect == "mutation"
    classification = "unknown"
    source = "unclassified"
    if not preferred:
        source = "route_registry_absence"
    elif unknown_operation:
        source = "missing_or_unregistered_operation"
    elif normalized_operation and normalized_operation in ui_required_operations:
        classification = "authenticated_ui_required"
        source = "route_registry"
    elif not native_route_available:
        classification = "native_route_unavailable"
        source = "route_registry_absence"
    elif native_operation_support == "unsupported":
        classification = "native_unsupported"
        source = "current_native_capability_check"
    elif normalized_operation and normalized_operation in native_supported_operations:
        classification = "native_supported"
        source = "route_registry"

    return {
        "required_operation": requested_operation,
        "operation_label_canonical": operation_label_canonical,
        "operation_registered": operation_registered,
        "unknown_operation": unknown_operation,
        "classification": classification,
        "classification_source": source,
        "native_route_available": native_route_available,
        "native_route_unavailable": not native_route_available,
        "native_lane_kind": native_lane_kind or None,
        "unknown_mutation": unknown_mutation,
        "operation_effect": operation_effect,
        "intent_effect": intent_effect,
        "intent_known": intent_known,
        "intent_effect_mismatch": intent_effect_mismatch,
        "mutation_intent": operation_declared_mutation,
        "operation_declared_mutation": operation_declared_mutation,
        "mutation_readiness_status_id": (
            mutation_status_ids.get(normalized_operation)
            if normalized_operation
            else None
        ),
        "native_supported": classification == "native_supported",
        "native_unsupported": classification == "native_unsupported",
        "authenticated_ui_required": (
            classification == "authenticated_ui_required"
        ),
        "caller_authenticated_ui_required": authenticated_ui_required,
        "native_supported_operations": sorted(native_supported_operations),
        "authenticated_ui_required_operations": sorted(ui_required_operations),
        "operation_effects": {
            "read": sorted(read_operations),
            "mutation": dict(sorted(mutation_status_ids.items())),
        },
        "operations": registered_operation_contracts,
        "capability_scope": capability_scope or None,
    }


def load_probe_index(
    required_probe_ids: set[str] | None = None,
) -> dict[str, ResolvedProbe]:
    """Load usable diagnostic probes without coupling unrelated routes."""

    try:
        payload = load_json("registry/probes.json")
    except SystemExit:
        return {}
    probes = payload.get("probes", [])
    if not isinstance(probes, list):
        return {}
    records: dict[str, dict[str, Any]] = {}
    invalid_ids: set[str] = set()
    for probe in probes:
        if not isinstance(probe, dict):
            continue
        probe_id = probe.get("probe_id")
        if (
            not isinstance(probe_id, str)
            or (required_probe_ids is not None and probe_id not in required_probe_ids)
        ):
            continue
        if probe_id in records:
            invalid_ids.add(probe_id)
            continue
        records[probe_id] = probe
    index: dict[str, ResolvedProbe] = {}
    for probe_id, probe in records.items():
        if probe_id in invalid_ids:
            continue
        try:
            validate_probe_record(
                probe,
                source=f"registry/probes.json probe {probe_id!r}",
            )
            default_ttl_ms = parse_probe_default_ttl_ms(
                probe.get("default_ttl"),
                source=f"registry/probes.json probe {probe_id!r}",
            )
        except RegistryContractError:
            continue
        index[probe_id] = ResolvedProbe(
            probe_id=probe_id,
            command=probe["command"],
            expected_signal=probe["expected_signal"],
            safe_lane=probe["safe_lane"],
            notes=probe["notes"],
            default_ttl_ms=default_ttl_ms,
            scope=probe["scope"],
        )
    return index


def load_status_index(required_status_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Load usable readiness diagnostics for the routes under consideration."""

    try:
        payload = load_json("status/capability_status.json")
    except SystemExit:
        return {}
    rows = payload.get("capabilities", [])
    if not isinstance(rows, list):
        return {}
    candidates: dict[str, dict[str, Any]] = {}
    invalid_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        status_id = row.get("capability_id")
        if not isinstance(status_id, str) or status_id not in required_status_ids:
            continue
        if status_id in candidates:
            invalid_ids.add(status_id)
            continue
        try:
            candidates[status_id] = validate_capability_status_record(
                row,
                source=f"status/capability_status.json row {status_id!r}",
            )
        except RegistryContractError:
            invalid_ids.add(status_id)
    for status_id in invalid_ids:
        candidates.pop(status_id, None)
    return candidates


def capability_age_days(
    last_verified_utc: str,
    now: datetime | None = None,
) -> float | None:
    try:
        parsed = parse_explicit_utc_timestamp(
            last_verified_utc,
            source="last_verified_utc",
        )
    except RegistryContractError:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return None
    age_seconds = (current.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    if age_seconds < 0:
        return None
    return age_seconds / 86_400


def capability_freshness(
    last_verified_utc: str,
    slo_max_age_days: Any,
    *,
    probe_ttl_days: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    age_days = capability_age_days(last_verified_utc, now=now)
    limits: list[float] = []
    if isinstance(slo_max_age_days, (int, float)) and not isinstance(slo_max_age_days, bool):
        if slo_max_age_days >= 0:
            limits.append(float(slo_max_age_days))
    if isinstance(probe_ttl_days, (int, float)) and not isinstance(probe_ttl_days, bool):
        if probe_ttl_days >= 0:
            limits.append(float(probe_ttl_days))
    effective_limit = min(limits) if limits else None
    breached = None if age_days is None or effective_limit is None else age_days > effective_limit
    return {
        "age_days": age_days,
        "slo_breached": breached,
        "effective_slo_max_age_days": effective_limit,
    }


def _probe_process_group_absent(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    except (OSError, PermissionError):
        return False
    return False


def _signal_probe_process_group(process_group: int, signum: int) -> bool:
    try:
        os.killpg(process_group, signum)
    except ProcessLookupError:
        return True
    except (OSError, PermissionError):
        return False
    return True


def _wait_for_probe_process_group_absence(
    process_group: int,
    process: subprocess.Popen[bytes],
) -> bool:
    deadline = time.monotonic() + PROBE_TERMINATE_GRACE_SECONDS
    while True:
        process.poll()
        if _probe_process_group_absent(process_group):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(PROBE_GROUP_POLL_SECONDS)


def _terminate_probe_process_group(process: subprocess.Popen[bytes]) -> bool:
    process_group = process.pid
    if not _probe_process_group_absent(process_group):
        if not _signal_probe_process_group(process_group, signal.SIGTERM):
            return False
        if not _wait_for_probe_process_group_absence(process_group, process):
            if not _signal_probe_process_group(process_group, signal.SIGKILL):
                return False
            if not _wait_for_probe_process_group_absence(process_group, process):
                return False
    try:
        process.wait(timeout=PROBE_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=PROBE_TERMINATE_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return False
    except OSError:
        return False
    return True


def subprocess_probe_transport(
    argv: tuple[str, ...], timeout_seconds: int
) -> ProbeCommandResult:
    """Run one validated probe with bounded output and exact process custody."""

    try:
        child_environment = dict(EXACT_PROBE_BASE_ENVIRONMENT)
        try:
            from scripts.routing_operator_bindings import selected_config_path
        except ModuleNotFoundError:
            from routing_operator_bindings import selected_config_path
        selected_configuration = selected_config_path()
        if selected_configuration is not None:
            child_environment["OPENCLAW_OPERATOR_CONFIG"] = selected_configuration
        if argv and argv[0] == str(TRUSTED_GOG_BIN):
            try:
                from scripts.google_search_console_probe import (
                    load_confined_gog_keyring_env,
                    validate_confined_gog_binary,
                )
            except ModuleNotFoundError:
                try:
                    from google_search_console_probe import (  # type: ignore[no-redef]
                        load_confined_gog_keyring_env,
                        validate_confined_gog_binary,
                    )
                except ModuleNotFoundError:
                    return ProbeCommandResult(returncode=126, stdout="")
            try:
                validate_confined_gog_binary(TRUSTED_GOG_BIN)
                confined = load_confined_gog_keyring_env(TRUSTED_GOG_ENV_FILE)
            except Exception:
                return ProbeCommandResult(returncode=126, stdout="")
            child_environment["GOG_KEYRING_PASSWORD"] = confined[
                "GOG_KEYRING_PASSWORD"
            ]
        process = subprocess.Popen(
            list(argv),
            cwd=ROOT,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return ProbeCommandResult(
            returncode=126,
            stdout="",
            outcome="transport_failed",
        )
    if process.stdout is None or process.stderr is None:
        _terminate_probe_process_group(process)
        return ProbeCommandResult(
            returncode=126,
            stdout="",
            outcome="custody_failed",
        )

    stdout = bytearray()
    stderr = bytearray()
    selector: selectors.BaseSelector | None = None
    outcome = "completed"
    returncode = 126
    try:
        selector = selectors.DefaultSelector()
        for stream, label in (
            (process.stdout, "stdout"),
            (process.stderr, "stderr"),
        ):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, label)
        deadline = time.monotonic() + timeout_seconds
        total_bytes = 0
        while selector.get_map():
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                outcome = "timeout"
                break
            events = selector.select(min(remaining_seconds, 0.1))
            for key, _mask in events:
                remaining_bytes = MAX_PROBE_OUTPUT_BYTES - total_bytes
                try:
                    chunk = os.read(
                        key.fd,
                        min(64 * 1024, remaining_bytes + 1),
                    )
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(chunk) > remaining_bytes:
                    outcome = "output_too_large"
                    break
                total_bytes += len(chunk)
                target = stdout if key.data == "stdout" else stderr
                target.extend(chunk)
            if outcome != "completed":
                break
        if outcome == "completed":
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                outcome = "timeout"
            else:
                try:
                    returncode = process.wait(timeout=remaining_seconds)
                except subprocess.TimeoutExpired:
                    outcome = "timeout"
    except (OSError, ValueError):
        outcome = "transport_failed"
    finally:
        if selector is not None:
            selector.close()
        custody_clean = _terminate_probe_process_group(process)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        if not custody_clean:
            outcome = "custody_failed"

    if outcome == "timeout":
        returncode = 124
    elif outcome == "output_too_large":
        returncode = 125
    elif outcome != "completed":
        returncode = 126
    try:
        decoded_stdout = stdout.decode("utf-8", errors="strict")
        decoded_stderr = stderr.decode("utf-8", errors="strict")
    except UnicodeError:
        return ProbeCommandResult(
            returncode=126,
            stdout="",
            outcome="transport_failed",
        )
    return ProbeCommandResult(
        returncode=returncode,
        stdout=decoded_stdout,
        stderr=decoded_stderr,
        outcome=outcome,
    )


def exact_probe_argv(preferred: dict[str, Any] | None) -> tuple[str, ...] | None:
    """Return argv only for a provider parser with an exact sealed command."""

    if not preferred:
        return None
    probe_id = preferred.get("probe_id")
    command = preferred.get("probe_command")
    if not isinstance(command, str):
        return None
    try:
        parsed = tuple(shlex.split(command, posix=True))
    except ValueError:
        return None
    if probe_id == APPLE_CALENDAR_LOCAL_PROBE_ID:
        if (
            preferred.get("route_id") != "apple-calendar-local"
            or preferred.get("system") != "apple-calendar"
            or preferred.get("required_principal") != 'operator'
            or preferred.get("required_account") != "local-apple-calendar"
            or preferred.get("probe_safe_lane") != "host-os-ui-read"
        ):
            return None
        expected = APPLE_CALENDAR_LOCAL_PROBE_ARGV
    elif probe_id in COMPANY_ALPHA_WALLETCONNECT_ROUTE_PROBES:
        route_contract = COMPANY_ALPHA_WALLETCONNECT_ROUTE_PROBES[probe_id]
        if (
            preferred.get("route_id") != route_contract["route_id"]
            or preferred.get("system") != route_contract["route_id"]
            or preferred.get("required_principal") != 'company-alpha'
            or preferred.get("required_account") != route_contract["account_id"]
            or preferred.get("probe_safe_lane") != "local-read"
        ):
            return None
        expected = COMPANY_ALPHA_WALLETCONNECT_PROBE_ARGV
    elif probe_id in NOTION_ROUTE_PROBES:
        route_contract = NOTION_ROUTE_PROBES[probe_id]
        if (
            preferred.get("route_id") != route_contract["route_id"]
            or preferred.get("system") != "notion"
            or preferred.get("required_principal") != route_contract["principal"]
            or preferred.get("required_account") != route_contract["account"]
            or preferred.get("probe_safe_lane") != "local-read"
        ):
            return None
        expected = (
            "python3",
            "scripts/notion_capability_probe.py",
            "--route",
            route_contract["route"],
        )
    elif probe_id == COMPANY_ALPHA_DOCS_PROBE_ID:
        if (
            preferred.get("route_id") != COMPANY_ALPHA_DOCS_ROUTE_ID
            or preferred.get("system") != 'company-alpha-docs'
            or preferred.get("required_principal") != 'company-alpha'
            or preferred.get("required_account") != 'company-alpha-public-docs'
            or preferred.get("native_lane_kind") != "mcp_integration"
            or preferred.get("probe_safe_lane") != "local-read-network-read"
            or parsed != COMPANY_ALPHA_DOCS_DECLARED_PROBE_ARGV
        ):
            return None
        return COMPANY_ALPHA_DOCS_PROBE_ARGV
    elif probe_id == CLOUDFLARE_PROBE_ID:
        if (
            preferred.get("route_id")
            != 'cloudflare-operator-readonly-api'
            or preferred.get("system") != "cloudflare"
            or preferred.get("required_principal") != 'operator'
            or preferred.get("required_account") != _operator_binding('services.cloudflare.zone_name')
            or preferred.get("native_lane_kind") != "provider_api"
            or preferred.get("probe_safe_lane") != "local-read-network-read"
        ):
            return None
        return (
            CLOUDFLARE_PROBE_ARGV
            if parsed == CLOUDFLARE_DECLARED_PROBE_ARGV
            else None
        )
    elif probe_id == TRELLO_PROBE_ID:
        if (
            preferred.get("route_id") != "trello-personal-api"
            or preferred.get("system") != "trello"
            or preferred.get("required_principal") != 'operator'
            or preferred.get("required_account")
            != _operator_binding('identifiers.accounts.personal_google')
            or preferred.get("native_lane_kind") != "supported_cli"
            or preferred.get("probe_safe_lane") != "local-read-network-read"
        ):
            return None
        return TRELLO_PROBE_ARGV if parsed == TRELLO_DECLARED_PROBE_ARGV else None
    elif probe_id == GITHUB_PROBE_ID:
        if (
            preferred.get("route_id") != "github-personal-cli"
            or preferred.get("system") != "github"
            or preferred.get("required_principal") != 'operator'
            or preferred.get("required_account") != _operator_binding('identifiers.github_username')
            or preferred.get("native_lane_kind") != "supported_cli"
            or preferred.get("probe_safe_lane") != "local-read-network-read"
        ):
            return None
        return GITHUB_PROBE_ARGV if parsed == GITHUB_PROBE_ARGV else None
    elif probe_id in HEDERA_SIGNER_ROUTE_PROBES:
        route_contract = HEDERA_SIGNER_ROUTE_PROBES[probe_id]
        if (
            preferred.get("route_id") != route_contract["route_id"]
            or preferred.get("system") != route_contract["system"]
            or preferred.get("required_principal") != "openclaw-testing"
            or preferred.get("required_account") != route_contract["account_id"]
            or preferred.get("native_lane_kind") != "supported_cli"
            or preferred.get("probe_safe_lane") != "local-read-network-read"
        ):
            return None
        declared = ("node", route_contract["script"], "--json")
        if parsed != declared:
            return None
        return (str(TRUSTED_OPENCLAW_NODE_BIN), route_contract["script"], "--json")
    elif probe_id == X_API_PROBE_ID:
        if (
            preferred.get("route_id") != 'x-api-operator-x-xurl'
            or preferred.get("system") != "x-api"
            or preferred.get("required_principal") != 'operator'
            or preferred.get("required_account") != _operator_binding('identifiers.x_username')
            or preferred.get("native_lane_kind") != "supported_cli"
            or preferred.get("probe_safe_lane") != "local-read-costed-api-read"
            or parsed != X_API_DECLARED_PROBE_ARGV
        ):
            return None
        return X_API_PROBE_ARGV
    elif probe_id in DISCORD_SOURCE_ROUTE_PROBES:
        route_contract = DISCORD_SOURCE_ROUTE_PROBES[probe_id]
        declared = (
            "openclaw",
            "message",
            "channel",
            "info",
            "--channel",
            "discord",
            "--target",
            f"channel:{route_contract['channel_id']}",
            "--json",
        )
        if (
            preferred.get("route_id") != route_contract["route_id"]
            or preferred.get("system") != "discord-source"
            or preferred.get("required_principal") != route_contract["principal"]
            or preferred.get("required_account") != route_contract["guild_id"]
            or preferred.get("native_lane_kind") != "supported_cli"
            or preferred.get("probe_safe_lane") != "local-read-network-read"
            or parsed != declared
        ):
            return None
        return (
            "/usr/bin/env",
            f"OPENCLAW_STATE_DIR={OPENCLAW_RUNTIME_STATE_DIR}",
            str(DISCORD_SOURCE_NODE_BIN),
            TRUSTED_OPENCLAW_CLI,
            *declared[1:],
        )
    elif probe_id == MERCURY_COMPANY_BETA_PROBE_ID:
        if (
            preferred.get("route_id") != 'mercury-company-beta-mcp'
            or preferred.get("system") != "mercury"
            or preferred.get("required_principal") != 'company-beta'
            or preferred.get("required_account") != _operator_binding('services.mercury.organization_name')
            or preferred.get("native_lane_kind") != "mcp_integration"
            or preferred.get("probe_safe_lane") != "local-read-network-read"
            or parsed != MERCURY_COMPANY_BETA_DECLARED_PROBE_ARGV
        ):
            return None
        return MERCURY_COMPANY_BETA_PROBE_ARGV
    elif probe_id == PERSONAL_DATA_NEON_PROBE_ID:
        if (
            preferred.get("route_id") != 'personal-data-neon-postgres-readonly'
            or preferred.get("system") != 'personal-data-neon-postgres'
            or preferred.get("required_principal") != 'personal-data-project'
            or preferred.get("required_account") != 'personal-data-neon-project'
            or preferred.get("native_lane_kind") != "supported_cli"
            or preferred.get("probe_safe_lane")
            != "local-read-network-read-database-read"
            or parsed != PERSONAL_DATA_NEON_DECLARED_PROBE_ARGV
        ):
            return None
        return PERSONAL_DATA_NEON_PROBE_ARGV
    elif probe_id == GIGABRAIN_PROBE_ID:
        if (
            preferred.get("route_id")
            != 'company-alpha-gigabrain-metabase-api'
            or preferred.get("system") != 'company-alpha-gigabrain'
            or preferred.get("required_principal") != 'company-alpha'
            or preferred.get("required_account") != 'company-alpha-gigabrain'
            or preferred.get("native_lane_kind") != "supported_cli"
            or preferred.get("probe_safe_lane") != "local-read-network-read"
            or parsed != GIGABRAIN_PROBE_ARGV
        ):
            return None
        return GIGABRAIN_PROBE_ARGV
    elif probe_id == GOOGLE_SEARCH_CONSOLE_PERSONAL_PROBE_ID:
        if (
            preferred.get("route_id")
            != "google-search-console-personal-gog"
            or preferred.get("system") != "google-search-console"
            or preferred.get("required_principal") != 'operator'
            or preferred.get("required_account")
            != _operator_binding('identifiers.accounts.personal_google')
            or preferred.get("native_lane_kind") != "supported_cli"
            or preferred.get("probe_safe_lane")
            != "local-read-network-read"
            or parsed != GOOGLE_SEARCH_CONSOLE_PERSONAL_DECLARED_PROBE_ARGV
        ):
            return None
        return GOOGLE_SEARCH_CONSOLE_PERSONAL_PROBE_ARGV
    elif preferred.get("probe_safe_lane") != "local-read-network-read":
        return None
    elif parsed == GOG_AUTH_LIST_PROBE_ARGV:
        if (
            preferred.get("native_lane_kind") != "supported_cli"
            or not isinstance(preferred.get("route_id"), str)
            or not isinstance(preferred.get("system"), str)
            or not isinstance(preferred.get("required_principal"), str)
            or not isinstance(preferred.get("required_account"), str)
            or not preferred["route_id"]
            or not preferred["system"]
            or not preferred["required_principal"]
            or not preferred["required_account"]
        ):
            return None
        expected = GOG_AUTH_LIST_PROBE_ARGV
    elif probe_id in GOOGLE_CALENDAR_PROBE_IDS:
        account = preferred.get("required_account")
        if not isinstance(account, str) or not account:
            return None
        expected = (
            str(TRUSTED_GOG_BIN),
            "calendar",
            "calendars",
            "--account",
            account,
            "--json",
            "--max",
            "250",
            "--all",
            "--no-input",
        )
    elif (
        probe_id == JIRA_COMPANY_ALPHA_PROBE_ID
        and preferred.get("route_id") == 'jira-company-alpha'
        and preferred.get("system") == "jira"
        and preferred.get("required_principal") == 'company-alpha'
        and preferred.get("required_account") == _operator_binding('services.jira.hostname')
        and preferred.get("native_lane_kind") == "provider_api"
        and preferred.get("probe_safe_lane") == "local-read-network-read"
    ):
        expected = JIRA_COMPANY_ALPHA_PROBE_ARGV
    else:
        return None
    return expected if parsed == expected else None


def _canonical_nonempty_string(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value == value.strip()
        and "\x00" not in value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _parse_google_search_console_personal_probe_signal(
    preferred: dict[str, Any], payload: Any
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if (
        exact_probe_argv(preferred)
        != GOOGLE_SEARCH_CONSOLE_PERSONAL_PROBE_ARGV
        or not isinstance(payload, dict)
        or set(payload) != GOOGLE_SEARCH_CONSOLE_PERSONAL_SUCCESS_SIGNAL_FIELDS
        or payload.get("schema")
        != "openclaw.google-search-console-probe.v1"
        or payload.get("producer") != "google_search_console_probe.py"
        or payload.get("account") != preferred.get("required_account")
        or payload.get("credential_env") != str(TRUSTED_GOG_ENV_FILE)
        or payload.get("operation") != "sites.list"
        or payload.get("mutating") is not False
        or payload.get("evidence_role") != "audit_only"
        or payload.get("authoritative") is not False
        or payload.get("completion_claim_allowed") is not False
        or payload.get("page_indexing_example_table_verified") is not False
        or payload.get("status") != "ready"
        or payload.get("reason_code") is not None
        or payload.get("property_accessible") is not True
        or payload.get("requested_property_domain") is not None
        or type(payload.get("gog_exit_code")) is not int
        or payload.get("gog_exit_code") != 0
        or type(payload.get("matching_property_count")) is not int
        or payload.get("matching_property_count", 0) < 1
        or type(payload.get("authorized_matching_property_count")) is not int
        or payload.get("authorized_matching_property_count", 0) < 1
        or payload.get("authorized_matching_property_count", 0)
        > payload.get("matching_property_count", 0)
        or payload.get("property_selection_required") is not True
        or payload.get("evidence")
        != (
            "exact-account Search Console sites.list returned authorized "
            "property access; this audit signal does not prove task completion "
            "or Page Indexing example-table inspection"
        )
    ):
        return None
    property_types = payload.get("authorized_property_types")
    if (
        not isinstance(property_types, list)
        or not property_types
        or not all(
            isinstance(property_type, str) for property_type in property_types
        )
        or property_types != sorted(set(property_types))
        or not all(
            property_type in {"domain", "url-prefix"}
            for property_type in property_types
        )
    ):
        return None
    try:
        parse_explicit_utc_timestamp(
            payload.get("checked_at_utc"),
            source="Google Search Console exact probe checked_at_utc",
        )
    except RegistryContractError:
        return None
    return ("read",), ("sites-list",)


def _parse_company_alpha_docs_probe_signal(
    preferred: dict[str, Any], payload: Any
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if exact_probe_argv(preferred) != COMPANY_ALPHA_DOCS_PROBE_ARGV:
        return None
    schema_digest = (
        payload.get("remote_tool_schema_digest")
        if isinstance(payload, dict)
        else None
    )
    receipt = payload.get("receipt") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version")
        != 'openclaw.company_alpha_docs_mcp.probe.v1'
        or payload.get("ok") is not True
        or payload.get("route_id") != COMPANY_ALPHA_DOCS_ROUTE_ID
        or payload.get("local_tools") != ["docs_search", "docs_read"]
        or not isinstance(schema_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", schema_digest) is None
        or type(payload.get("mutation_attempt_count")) is not int
        or payload.get("mutation_attempt_count") != 0
        or payload.get("local_writes_performed") is not False
        or not isinstance(receipt, dict)
        or receipt.get("schema_version")
        != 'openclaw.company_alpha_docs_mcp.receipt.v1'
        or receipt.get("route_id") != COMPANY_ALPHA_DOCS_ROUTE_ID
        or receipt.get("remote_tool_schema_digest") != schema_digest
        or receipt.get("remote_tool_invoked") is not None
        or receipt.get("egress_class") != "public_only"
        or type(receipt.get("mutation_attempt_count")) is not int
        or receipt.get("mutation_attempt_count") != 0
        or receipt.get("result") != "healthy"
    ):
        return None
    return ("read",), ("docs-read", "docs-search")


def _parse_gigabrain_probe_signal(
    preferred: dict[str, Any], payload: Any
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if (
        exact_probe_argv(preferred) != GIGABRAIN_PROBE_ARGV
        or not isinstance(payload, dict)
        or set(payload) != GIGABRAIN_SUCCESS_SIGNAL_FIELDS
        or payload.get("schema")
        != 'openclaw.company-alpha-gigabrain-metabase-probe.v1'
        or payload.get("route") != preferred.get("route_id")
        or payload.get("system") != 'company-alpha-gigabrain'
        or payload.get("credential_handle_id") != 'metabase.company_alpha.api'
        or payload.get("endpoint_origin_expected")
        != _operator_binding('services.company_alpha_analytics.origin')
        or any(
            payload.get(field) is not True
            for field in (
                "credential_contract_bound",
                "endpoint_matches_expected",
                "auth_ok",
                "account_identity_matches_registered",
                "secrets_redacted",
                "ok",
            )
        )
        or payload.get("external_mutation") is not False
    ):
        return None
    adapter = preferred.get("provider_adapter")
    if not isinstance(adapter, dict):
        return None
    identity_policy = adapter.get("identity_policy")
    native_evidence = adapter.get("native_evidence")
    if not isinstance(identity_policy, dict) or not isinstance(
        native_evidence, dict
    ):
        return None
    digest_contract = provider_account_digest_contract(
        identity_policy,
        native_evidence,
    )
    if (
        adapter.get("type") != "authenticated_provider_evidence"
        or digest_contract["required"] is not True
        or digest_contract["contract_valid"] is not True
        or payload.get("provider_identity_sha256")
        != digest_contract["digest"]
    ):
        return None
    return ("read",), ("status-read",)


def _parse_x_api_probe_signal(
    preferred: dict[str, Any], payload: Any
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if exact_probe_argv(preferred) != X_API_PROBE_ARGV:
        return None
    if not isinstance(payload, dict) or set(payload) != {"data"}:
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or set(data) != {
        "created_at",
        "description",
        "id",
        "name",
        "profile_image_url",
        "public_metrics",
        "subscription_type",
        "username",
        "verified",
        "verified_type",
    }:
        return None
    public_metrics = data.get("public_metrics")
    if (
        data.get("username") != _operator_binding('identifiers.x_username')
        or not isinstance(data.get("id"), str)
        or re.fullmatch(r"[0-9]{1,20}", data["id"]) is None
        or not isinstance(public_metrics, dict)
        or set(public_metrics)
        != {
            "followers_count",
            "following_count",
            "like_count",
            "listed_count",
            "media_count",
            "tweet_count",
        }
        or not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in public_metrics.values()
        )
    ):
        return None
    try:
        parse_explicit_utc_timestamp(
            data.get("created_at"), source="X exact probe created_at"
        )
    except RegistryContractError:
        return None
    # whoami proves the exact authenticated account, not a timeline/search read.
    return ("read",), ()


def _parse_discord_source_probe_signal(
    preferred: dict[str, Any], payload: Any
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    probe_id = preferred.get("probe_id")
    if probe_id not in DISCORD_SOURCE_ROUTE_PROBES:
        return None
    if exact_probe_argv(preferred) is None:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "action",
        "channel",
        "dryRun",
        "handledBy",
        "payload",
    }:
        return None
    result = payload.get("payload")
    if (
        payload.get("action") != "channel-info"
        or payload.get("channel") != "discord"
        or payload.get("dryRun") is not False
        or payload.get("handledBy") != "plugin"
        or not isinstance(result, dict)
        or set(result) != {"ok", "channel"}
        or result.get("ok") is not True
    ):
        return None
    route_contract = DISCORD_SOURCE_ROUTE_PROBES[probe_id]
    channel = result.get("channel")
    if (
        not isinstance(channel, dict)
        or not {
            "id",
            "type",
            "guild_id",
            "name",
            "position",
            "permission_overwrites",
        }.issubset(channel)
        or channel.get("id") != route_contract["channel_id"]
        or DISCORD_SNOWFLAKE_PATTERN.fullmatch(channel["id"]) is None
        or channel.get("guild_id") != route_contract["guild_id"]
        or not _canonical_nonempty_string(channel.get("name"))
        or not isinstance(channel.get("type"), int)
        or isinstance(channel.get("type"), bool)
        or not isinstance(channel.get("position"), int)
        or isinstance(channel.get("position"), bool)
        or not isinstance(channel.get("permission_overwrites"), list)
    ):
        return None
    return ("read",), ("channel-info",)


def _parse_mercury_company_beta_probe_signal(
    preferred: dict[str, Any], payload: Any
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if exact_probe_argv(preferred) != MERCURY_COMPANY_BETA_PROBE_ARGV:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "generatedAt",
        "servers",
        "tools",
        "diagnostics",
    }:
        return None
    try:
        parse_explicit_utc_timestamp(
            payload.get("generatedAt"), source="Mercury exact probe generatedAt"
        )
    except RegistryContractError:
        return None
    if payload.get("diagnostics") != []:
        return None
    servers = payload.get("servers")
    if not isinstance(servers, dict) or set(servers) != {MERCURY_COMPANY_BETA_SERVER_ID}:
        return None
    server = servers.get(MERCURY_COMPANY_BETA_SERVER_ID)
    if not isinstance(server, dict) or set(server) != {
        "launch",
        "tools",
        "codexApprovalMode",
        "requestTimeoutMs",
        "supportsParallelToolCalls",
        "listChanged",
    }:
        return None
    tools = payload.get("tools")
    if (
        server.get("launch") != MERCURY_COMPANY_BETA_SERVER_URL
        or server.get("tools") != len(MERCURY_COMPANY_BETA_TOOL_NAMES)
        or server.get("codexApprovalMode") != "auto"
        or server.get("requestTimeoutMs") != 60_000
        or server.get("supportsParallelToolCalls") is not True
        or server.get("listChanged")
        != {"prompts": False, "resources": False, "tools": True}
        or not isinstance(tools, list)
        or len(tools) != len(MERCURY_COMPANY_BETA_TOOL_NAMES)
        or not all(_canonical_nonempty_string(tool) for tool in tools)
        or len(tools) != len(set(tools))
        or set(tools) != MERCURY_COMPANY_BETA_TOOL_NAMES
    ):
        return None
    # This proves only the official authenticated MCP transport and exact
    # read-only catalog. A task-specific Mercury read still proves its result.
    return ("read",), ("capability-probe",)


def _parse_personal_data_neon_probe_signal(
    preferred: dict[str, Any], payload: Any
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if exact_probe_argv(preferred) != PERSONAL_DATA_NEON_PROBE_ARGV:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "route_id",
        "checked_at_utc",
        "status",
        "reason_code",
        "capability_scope",
        "mutating",
        "secrets_emitted",
        "dsn_emitted",
        "unsupported_operations",
        "custody",
        "attestation",
        "neon_api",
        "postgres",
    }:
        return None
    if (
        payload.get("schema")
        != 'openclaw.personal-data-neon-postgres-readonly-probe.v1'
        or payload.get("route_id") != 'personal-data-neon-postgres-readonly'
        or payload.get("status") != "ready"
        or payload.get("reason_code") is not None
        or payload.get("capability_scope") != "read_only"
        or payload.get("mutating") is not False
        or payload.get("secrets_emitted") is not False
        or payload.get("dsn_emitted") is not False
        or payload.get("unsupported_operations")
        != ["write", "dml", "ddl", "migration", "provider_mutation"]
    ):
        return None
    custody = payload.get("custody")
    if (
        not isinstance(custody, dict)
        or set(custody)
        != {
            "physical_workspace_verified",
            "secrets_directory_mode",
            "credential_file_mode",
            "attestation_file_mode",
            "single_link_private_files",
        }
        or custody.get("physical_workspace_verified") is not True
        or custody.get("secrets_directory_mode") != "0700"
        or custody.get("credential_file_mode") != "0600"
        or custody.get("attestation_file_mode") != "0600"
        or custody.get("single_link_private_files") is not True
    ):
        return None
    neon_api = payload.get("neon_api")
    if (
        not isinstance(neon_api, dict)
        or set(neon_api)
        != {
            "auth_identity_verified",
            "project_identity_verified",
            "endpoint_identity_verified",
            "http_methods",
        }
        or neon_api.get("auth_identity_verified") is not True
        or neon_api.get("project_identity_verified") is not True
        or neon_api.get("endpoint_identity_verified") is not True
        or neon_api.get("http_methods") != ["GET"]
    ):
        return None
    try:
        parse_explicit_utc_timestamp(
            payload.get("checked_at_utc"),
            source='PersonalDataProject Neon exact probe checked_at_utc',
        )
    except RegistryContractError:
        return None
    attestation = payload.get("attestation")
    if (
        not isinstance(attestation, dict)
        or set(attestation) != {"sha256", "identity_bound"}
        or attestation.get("identity_bound") is not True
        or not isinstance(attestation.get("sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", attestation["sha256"]) is None
    ):
        return None
    postgres = payload.get("postgres")
    postgres_fields = {
        "endpoint_host_verified",
        "server_port_verified",
        "connection_database_verified",
        "connection_role_verified",
        "libpq_tls_verified",
        "database_identity_verified",
        "role_identity_verified",
        "project_identity_verified",
        "branch_identity_verified",
        "endpoint_identity_verified",
        "transaction_read_only",
        "tls_verified",
        "pg_stat_ssl_reported",
    }
    if (
        not isinstance(postgres, dict)
        or set(postgres) != postgres_fields
        or not isinstance(postgres.get("pg_stat_ssl_reported"), bool)
        or any(
            postgres.get(field) is not True
            for field in postgres_fields - {"pg_stat_ssl_reported"}
        )
    ):
        return None
    return ("read",), ("query-readonly",)


def _parse_github_auth_status_signal(
    preferred: dict[str, Any], stdout: str, stderr: str
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if exact_probe_argv(preferred) != GITHUB_PROBE_ARGV:
        return None
    streams = [value for value in (stdout, stderr) if value.strip()]
    if len(streams) != 1:
        return None
    output = streams[0]
    if "\x00" in output or "\r" in output:
        return None
    lines = output.splitlines()
    hostname_lines = [line for line in lines if line.strip() == "github.com"]
    login_pattern = re.compile(
        r"\s*(?:[^\w\s]\s+)?Logged in to github\.com account "
        r"(?P<account>\S+) \((?P<credential_store>[^()\r\n]+)\)\s*"
    )
    login_blocks = [
        (index, match.group("account"), match.group("credential_store"))
        for index, line in enumerate(lines)
        if (match := login_pattern.fullmatch(line)) is not None
    ]
    target_blocks = [
        (position, index)
        for position, (index, account, credential_store) in enumerate(login_blocks)
        if account == _operator_binding('identifiers.github_username') and credential_store == "keyring"
    ]
    if len(hostname_lines) != 1 or len(target_blocks) != 1:
        return None
    redacted_token_pattern = re.compile(
        r"\s*-\s+Token:\s+(?:gh[a-z]_|github_pat_)\*{8,}\s*"
    )
    if any(
        re.match(r"\s*-\s+Token:", line)
        and redacted_token_pattern.fullmatch(line) is None
        for line in lines
    ):
        return None
    target_position, target_start = target_blocks[0]
    target_end = (
        login_blocks[target_position + 1][0]
        if target_position + 1 < len(login_blocks)
        else len(lines)
    )
    target_lines = lines[target_start + 1 : target_end]
    active_lines = [
        line
        for line in target_lines
        if re.fullmatch(r"\s*-\s+Active account:\s+true\s*", line)
    ]
    protocol_lines = [
        line
        for line in target_lines
        if re.fullmatch(r"\s*-\s+Git operations protocol:\s+https\s*", line)
    ]
    allowed_target_line_patterns = (
        re.compile(r"\s*"),
        re.compile(r"\s*-\s+Active account:\s+true\s*"),
        re.compile(r"\s*-\s+Git operations protocol:\s+https\s*"),
        redacted_token_pattern,
        re.compile(
            r"\s*-\s+Token scopes:\s+'[A-Za-z0-9:_-]+'"
            r"(?:,\s*'[A-Za-z0-9:_-]+')*\s*"
        ),
    )
    if (
        len(active_lines) != 1
        or len(protocol_lines) != 1
        or any(
            not any(pattern.fullmatch(line) for pattern in allowed_target_line_patterns)
            for line in target_lines
        )
    ):
        return None
    return ("read",), ("identity-read",)


def _parse_cloudflare_probe_signal(
    preferred: dict[str, Any], payload: Any
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if exact_probe_argv(preferred) != CLOUDFLARE_PROBE_ARGV:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "route_id",
        "status",
        "reason_code",
        "identity",
        "zone",
        "project",
    }:
        return None
    if (
        payload.get("schema") != "openclaw.cloudflare-personal-read.v1"
        or payload.get("route_id")
        != 'cloudflare-operator-readonly-api'
        or payload.get("status") != "ready"
        or payload.get("reason_code") is not None
        or payload.get("identity")
        != {
            "account_id": _operator_binding('services.cloudflare.account_id'),
            "account_name": _operator_binding('services.cloudflare.account_name'),
        }
        or payload.get("zone")
        != {
            "id": _operator_binding('services.cloudflare.zone_id'),
            "name": _operator_binding('services.cloudflare.zone_name'),
            "status": "active",
            "type": "full",
            "account_id": _operator_binding('services.cloudflare.account_id'),
        }
    ):
        return None
    project = payload.get("project")
    if project != {
        "id": _operator_binding('services.cloudflare.project_id'),
        "name": "personal-site",
        "subdomain": _operator_binding('services.cloudflare.project_subdomain'),
        "production_branch": "main",
        "source": {
            "type": "github",
            "owner": _operator_binding('identifiers.github_username'),
            "repository": "personal-site",
        },
    }:
        return None
    return ("read",), (
        "token-verify",
        "account-read",
        "zone-read",
        "pages-project-read",
    )


def _parse_trello_probe_signal(
    preferred: dict[str, Any], payload: Any
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if exact_probe_argv(preferred) != TRELLO_PROBE_ARGV:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "overall",
        "auth",
        "board",
        "errors",
    }:
        return None
    auth = payload.get("auth")
    board = payload.get("board")
    if (
        payload.get("overall") != "ready"
        or payload.get("errors") != []
        or not isinstance(auth, dict)
        or set(auth) != {"status", "member_id", "username", "full_name", "url"}
        or auth.get("status") != "ready"
        or auth.get("username") != _operator_binding('services.trello.username')
        or not isinstance(board, dict)
        or set(board) != {"status", "id", "name", "url", "closed"}
        or board.get("status") != "ready"
        or board.get("name") != _operator_binding("services.trello.board_name")
        or board.get("closed") is not False
    ):
        return None
    for value in (
        auth.get("member_id"),
        auth.get("full_name"),
        auth.get("url"),
        board.get("id"),
        board.get("url"),
    ):
        if not _canonical_nonempty_string(value):
            return None
    if (
        re.fullmatch(r"[0-9a-f]{24}", str(auth["member_id"])) is None
        or re.fullmatch(r"[0-9a-f]{24}", str(board["id"])) is None
    ):
        return None
    try:
        member_url = urlparse(str(auth["url"]))
        board_url = urlparse(str(board["url"]))
    except ValueError:
        return None
    if (
        member_url.scheme != "https"
        or member_url.netloc != "trello.com"
        or member_url.path != "/u/" + _operator_binding("services.trello.username")
        or member_url.query
        or member_url.fragment
        or board_url.scheme != "https"
        or board_url.netloc != "trello.com"
        or board_url.query
        or board_url.fragment
        or not (
            board_url.path == "/b/" + _operator_binding("services.trello.board_id")
            or board_url.path.startswith("/b/" + _operator_binding("services.trello.board_id") + "/")
        )
    ):
        return None
    return ("read",), ("board-read", "member-read")


def _parse_hedera_signer_probe_signal(
    preferred: dict[str, Any], payload: Any
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    probe_id = preferred.get("probe_id")
    route_contract = HEDERA_SIGNER_ROUTE_PROBES.get(str(probe_id))
    if route_contract is None:
        return None
    expected_argv = (
        str(TRUSTED_OPENCLAW_NODE_BIN),
        route_contract["script"],
        "--json",
    )
    if exact_probe_argv(preferred) != expected_argv:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "ok",
        "checked_at",
        "network",
        "ledger",
        "account_id",
        "classification",
        "secret_file",
        "sdk",
        "key",
        "balance",
        "boundaries",
    }:
        return None
    if (
        payload.get("schema") != route_contract["schema"]
        or payload.get("ok") is not True
        or payload.get("network") != route_contract["network"]
        or payload.get("ledger") != route_contract["ledger"]
        or payload.get("account_id") != route_contract["account_id"]
        or payload.get("classification") != route_contract["classification"]
    ):
        return None
    try:
        checked_at = parse_explicit_utc_timestamp(
            payload.get("checked_at"),
            source=f"{probe_id} checked_at",
        )
    except RegistryContractError:
        return None
    age_seconds = (datetime.now(timezone.utc) - checked_at).total_seconds()
    if age_seconds < -5 or age_seconds > EXACT_PROBE_TIMEOUT_SECONDS + 5:
        return None

    secret_file = payload.get("secret_file")
    expected_variables = list(route_contract["secret_variables"])
    if (
        not isinstance(secret_file, dict)
        or set(secret_file)
        != {
            "path",
            "owner",
            "uid",
            "gid",
            "mode",
            "regular_file",
            "symlink",
            "inode",
            "size_bytes",
            "variable_names",
            "value_lengths",
        }
        or secret_file.get("path") != route_contract["secret_path"]
        or secret_file.get("owner") != _operator_binding("identifiers.host_user")
        or secret_file.get("uid") != os.getuid()
        or secret_file.get("mode") != "0600"
        or secret_file.get("regular_file") is not True
        or secret_file.get("symlink") is not False
        or secret_file.get("variable_names") != expected_variables
    ):
        return None
    for field in ("uid", "gid"):
        value = secret_file.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
    size_bytes = secret_file.get("size_bytes")
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes <= 0
        or re.fullmatch(r"[1-9][0-9]*", str(secret_file.get("inode", "")))
        is None
    ):
        return None
    value_lengths = secret_file.get("value_lengths")
    expected_value_lengths = {
        expected_variables[0]: len(route_contract["account_id"]),
        expected_variables[1]: route_contract["private_key_length"],
    }
    if route_contract["network"] == "mainnet":
        expected_value_lengths[expected_variables[2]] = len(route_contract["key_type"])
    expected_size_bytes = sum(
        len(name) + 1 + length + 1
        for name, length in expected_value_lengths.items()
    )
    if value_lengths != expected_value_lengths or size_bytes != expected_size_bytes:
        return None

    sdk = payload.get("sdk")
    if (
        not isinstance(sdk, dict)
        or set(sdk) != {"package", "version"}
        or sdk.get("package") != "@hashgraph/sdk"
        or not _canonical_nonempty_string(sdk.get("version"))
    ):
        return None

    key = payload.get("key")
    common_key_fields = {
        "type",
        "derived_public_key",
        "derived_public_key_fingerprint_sha256",
        "authoritative_public_key",
        "authoritative_public_key_fingerprint_sha256",
        "matches_authoritative_account",
        "derived_evm_alias",
        "mirror_evm_address",
    }
    expected_key_fields = (
        common_key_fields
        if route_contract["network"] == "mainnet"
        else common_key_fields | {"evm_alias_matches"}
    )
    public_key_length = 64 if route_contract["network"] == "mainnet" else 66
    if (
        not isinstance(key, dict)
        or set(key) != expected_key_fields
        or key.get("type") != route_contract["key_type"]
        or key.get("matches_authoritative_account") is not True
    ):
        return None
    derived_key = key.get("derived_public_key")
    authoritative_key = key.get("authoritative_public_key")
    if (
        not isinstance(derived_key, str)
        or re.fullmatch(rf"[0-9a-f]{{{public_key_length}}}", derived_key) is None
        or authoritative_key != derived_key
        or (
            route_contract["network"] == "testnet"
            and derived_key[:2] not in {"02", "03"}
        )
    ):
        return None
    fingerprint = hashlib.sha256(bytes.fromhex(derived_key)).hexdigest()
    if (
        key.get("derived_public_key_fingerprint_sha256") != fingerprint
        or key.get("authoritative_public_key_fingerprint_sha256") != fingerprint
    ):
        return None
    evm_pattern = re.compile(r"0x[0-9a-f]{40}")
    if route_contract["network"] == "mainnet":
        mirror_evm = key.get("mirror_evm_address")
        if (
            key.get("derived_evm_alias") is not None
            or (
                mirror_evm is not None
                and (
                    not isinstance(mirror_evm, str)
                    or evm_pattern.fullmatch(mirror_evm) is None
                )
            )
        ):
            return None
    else:
        derived_evm = key.get("derived_evm_alias")
        if (
            not isinstance(derived_evm, str)
            or evm_pattern.fullmatch(derived_evm) is None
            or key.get("mirror_evm_address") != derived_evm
            or key.get("evm_alias_matches") is not True
        ):
            return None

    balance = payload.get("balance")
    if not isinstance(balance, dict) or set(balance) != {
        "tinybar",
        "hbar",
        "mirror_timestamp",
    }:
        return None
    tinybar = balance.get("tinybar")
    mirror_timestamp = balance.get("mirror_timestamp")
    if (
        not isinstance(tinybar, str)
        or re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", tinybar) is None
        or not isinstance(mirror_timestamp, str)
        or re.fullmatch(r"[0-9]+\.[0-9]+", mirror_timestamp) is None
    ):
        return None
    tinybar_value = int(tinybar)
    expected_hbar = (
        f"{tinybar_value // 100_000_000}."
        f"{tinybar_value % 100_000_000:08d}"
    )
    if balance.get("hbar") != expected_hbar:
        return None

    expected_boundaries = {
        "transaction_executed": False,
        "signing_executed": False,
        "funds_moved": False,
        "default_process_inheritance": False,
    }
    if route_contract["network"] == "testnet":
        expected_boundaries["raw_private_key_output"] = False
    if payload.get("boundaries") != expected_boundaries:
        return None
    return ("read",), ("account-read",)


def parse_exact_probe_signal(
    preferred: dict[str, Any], stdout: str, stderr: str = ""
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Validate one supported provider response into exact operation evidence."""

    probe_id = preferred.get("probe_id")
    if probe_id == GITHUB_PROBE_ID:
        return _parse_github_auth_status_signal(preferred, stdout, stderr)
    if (
        probe_id in {CLOUDFLARE_PROBE_ID, TRELLO_PROBE_ID}
        or probe_id in HEDERA_SIGNER_ROUTE_PROBES
        or probe_id in {
            X_API_PROBE_ID,
            MERCURY_COMPANY_BETA_PROBE_ID,
            PERSONAL_DATA_NEON_PROBE_ID,
            JIRA_COMPANY_ALPHA_PROBE_ID,
            GIGABRAIN_PROBE_ID,
            GOOGLE_SEARCH_CONSOLE_PERSONAL_PROBE_ID,
        }
        or probe_id in DISCORD_SOURCE_ROUTE_PROBES
    ) and stderr.strip():
        return None
    try:
        payload = loads_json_strict(
            stdout,
            source=f"exact {probe_id or 'unknown'} probe stdout",
        )
    except (RecursionError, RegistryContractError):
        return None
    if probe_id == CLOUDFLARE_PROBE_ID:
        return _parse_cloudflare_probe_signal(preferred, payload)
    if probe_id == TRELLO_PROBE_ID:
        return _parse_trello_probe_signal(preferred, payload)
    if probe_id in HEDERA_SIGNER_ROUTE_PROBES:
        return _parse_hedera_signer_probe_signal(preferred, payload)
    if probe_id == X_API_PROBE_ID:
        return _parse_x_api_probe_signal(preferred, payload)
    if probe_id in DISCORD_SOURCE_ROUTE_PROBES:
        return _parse_discord_source_probe_signal(preferred, payload)
    if probe_id == MERCURY_COMPANY_BETA_PROBE_ID:
        return _parse_mercury_company_beta_probe_signal(preferred, payload)
    if probe_id == PERSONAL_DATA_NEON_PROBE_ID:
        return _parse_personal_data_neon_probe_signal(preferred, payload)
    if probe_id == COMPANY_ALPHA_DOCS_PROBE_ID:
        return _parse_company_alpha_docs_probe_signal(preferred, payload)
    if probe_id == GIGABRAIN_PROBE_ID:
        return _parse_gigabrain_probe_signal(preferred, payload)
    if probe_id == GOOGLE_SEARCH_CONSOLE_PERSONAL_PROBE_ID:
        return _parse_google_search_console_personal_probe_signal(
            preferred, payload
        )
    if probe_id == APPLE_CALENDAR_LOCAL_PROBE_ID:
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"checked_at_utc", "capabilities", "overall", "errors"}
            or payload.get("overall") != "ready"
            or payload.get("errors") != []
        ):
            return None
        try:
            parse_explicit_utc_timestamp(
                payload.get("checked_at_utc"),
                source="Apple Calendar exact probe checked_at_utc",
            )
        except RegistryContractError:
            return None
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != {
            "osascript",
            "calendar_launch",
            "calendar_list",
            "calendar_read_next_7d",
        }:
            return None
        osascript = capabilities.get("osascript")
        calendar_launch = capabilities.get("calendar_launch")
        calendar_list = capabilities.get("calendar_list")
        calendar_read = capabilities.get("calendar_read_next_7d")
        if (
            not isinstance(osascript, dict)
            or set(osascript) != {"status", "path"}
            or osascript.get("status") != "ready"
            or osascript.get("path") != "/usr/bin/osascript"
            or calendar_launch != {"status": "ready"}
            or not isinstance(calendar_list, dict)
            or set(calendar_list) != {"status", "count", "calendars"}
            or calendar_list.get("status") != "ready"
            or not isinstance(calendar_read, dict)
            or set(calendar_read)
            != {
                "status",
                "readable_calendars",
                "total_events_next_7d",
                "calendar_event_counts",
            }
            or calendar_read.get("status") != "ready"
        ):
            return None
        calendars = calendar_list.get("calendars")
        count = calendar_list.get("count")
        readable = calendar_read.get("readable_calendars")
        total_events = calendar_read.get("total_events_next_7d")
        event_counts = calendar_read.get("calendar_event_counts")
        if (
            not isinstance(calendars, list)
            or not calendars
            or not all(
                isinstance(calendar, str)
                and calendar.strip()
                and calendar == calendar.strip()
                for calendar in calendars
            )
            or len(calendars) != len(set(calendars))
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count != len(calendars)
            or not isinstance(readable, int)
            or isinstance(readable, bool)
            or readable <= 0
            or readable > count
            or not isinstance(total_events, int)
            or isinstance(total_events, bool)
            or total_events < 0
            or not isinstance(event_counts, dict)
            or set(event_counts) != set(calendars)
        ):
            return None
        ready_events = 0
        ready_calendars = 0
        for calendar, evidence in event_counts.items():
            if not isinstance(calendar, str) or not isinstance(evidence, dict):
                return None
            if evidence.get("status") == "ready":
                if set(evidence) != {"status", "events_next_7d"}:
                    return None
                events = evidence.get("events_next_7d")
                if (
                    not isinstance(events, int)
                    or isinstance(events, bool)
                    or events < 0
                ):
                    return None
                ready_calendars += 1
                ready_events += events
            elif evidence != {"status": "error"}:
                return None
        if ready_calendars != readable or ready_events != total_events:
            return None
        return ("read",), ("calendar-list", "event-list")
    if probe_id in COMPANY_ALPHA_WALLETCONNECT_ROUTE_PROBES:
        if not isinstance(payload, dict) or set(payload) != {
            "schema",
            "ok",
            "checked_at",
            "capability",
            "production_adapter",
            "zero_effects",
        }:
            return None
        if (
            payload.get("schema")
            != 'openclaw.company_alpha_walletconnect_agent_probe.v1'
            or payload.get("ok") is not True
            or payload.get("zero_effects")
            != {
                "private_key_loaded": False,
                "relay_connected": False,
                "session_paired": False,
                "signature_created": False,
                "transaction_submitted": False,
                "settlement_queried": False,
                "funds_moved": False,
                "execution_attempts": 0,
            }
        ):
            return None
        try:
            parse_explicit_utc_timestamp(
                payload.get("checked_at"),
                source='CompanyAlpha WalletConnect agent probe checked_at',
            )
        except RegistryContractError:
            return None
        capability = payload.get("capability")
        if not isinstance(capability, dict) or set(capability) != {
            "schema",
            "execution_modes",
            "request_schema",
            "receipt_schema",
            "supported_methods",
            "supported_chains",
            "origin_policy",
            "controls",
        }:
            return None
        if (
            capability.get("schema")
            != 'openclaw.company_alpha_walletconnect_agent_capability.v1'
            or capability.get("execution_modes") != ["dry-run", "execute"]
            or capability.get("request_schema")
            != 'openclaw.company_alpha_walletconnect_agent_request.v1'
            or capability.get("receipt_schema")
            != 'openclaw.company_alpha_walletconnect_agent_receipt.v1'
            or capability.get("supported_methods")
            != ["hedera_signAndExecuteTransaction", "hedera_signMessage"]
            or capability.get("supported_chains")
            != ["hedera:testnet", "hedera:mainnet"]
            or capability.get("origin_policy")
            != 'company_alpha_finance_or_loopback'
            or capability.get("controls")
            != {
                "browser_request_used_as_supplied": True,
                "contract_or_router_allowlist": False,
                "amount_or_asset_policy": False,
                "semantic_calldata_decoder": False,
                "private_key_process_bound": True,
                "session_handles_multiple_requests": True,
                "retry_unknown_submission": False,
                "wallet_extension_required": False,
            }
        ):
            return None
        production_adapter = payload.get("production_adapter")
        if production_adapter != {
            "schema": 'openclaw.company_alpha_walletconnect_hedera_adapter.v1',
            "production_factory": "createProductionHederaWalletConnectAdapter",
            "dependency_versions": {
                "hedera_wallet_connect": "2.0.4",
                "hashgraph_sdk": "2.81.0",
                "walletconnect_core": "2.23.0",
                "jiti": "1.21.7",
            },
            "key_loading": "execute_only_process_bound",
            "supported_methods": [
                "hedera_signAndExecuteTransaction",
                "hedera_signMessage",
            ],
            "execution_scope": (
                "site_generated_hedera_wallet_requests_for_selected_account"
            ),
            "settlement_evidence": [
                "sdk_hapi_receipt",
                "mirror_transaction",
                "mirror_contract_result",
                "hashscan_url",
                "selected_account_before_after",
            ],
            "origin_policy": 'company_alpha_finance_or_loopback',
            "supported_networks": ["hedera:testnet", "hedera:mainnet"],
        }:
            return None
        route_contract = COMPANY_ALPHA_WALLETCONNECT_ROUTE_PROBES[probe_id]
        if route_contract["chain_id"] not in capability["supported_chains"]:
            return None
        return ("read",), ("capability-probe",)
    if probe_id in NOTION_ROUTE_PROBES:
        route_contract = NOTION_ROUTE_PROBES[probe_id]
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], dict)
        ):
            return None
        result = payload[0]
        if set(result) != {
            "route",
            "credential_handle_id",
            "credential_owner_bound",
            "route_ok",
            "bot_name",
            "owner_type",
            "workspace_name",
            "workspace_id",
            "ok",
        }:
            return None
        if (
            result.get("route") != route_contract["route"]
            or result.get("credential_handle_id")
            != route_contract["credential_handle_id"]
            or result.get("credential_owner_bound") is not True
            or result.get("route_ok") is not True
            or result.get("owner_type") != "workspace"
            or result.get("ok") is not True
        ):
            return None
        for field in ("bot_name", "workspace_name"):
            value = result.get(field)
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                return None
        workspace_id = result.get("workspace_id")
        if (
            not isinstance(workspace_id, str)
            or NOTION_WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is None
        ):
            return None
        return ("read",), ("identity-read",)
    if exact_probe_argv(preferred) == GOG_AUTH_LIST_PROBE_ARGV:
        if not isinstance(payload, dict):
            return None
        accounts = payload.get("accounts")
        if not isinstance(accounts, list) or not all(
            isinstance(account, dict) for account in accounts
        ):
            return None
        required_account = preferred.get("required_account")
        if not isinstance(required_account, str) or not required_account:
            return None
        matches = [
            account
            for account in accounts
            if isinstance(account.get("email"), str)
            and account["email"].casefold() == required_account.casefold()
        ]
        if len(matches) != 1:
            return None
        account = matches[0]
        services = account.get("services")
        scopes = account.get("scopes")
        if (
            account.get("valid") is not True
            or account.get("auth") != "oauth"
            or not isinstance(account.get("client"), str)
            or not account["client"]
            or not isinstance(services, list)
            or not services
            or not all(isinstance(service, str) and service for service in services)
            or len(services) != len(set(services))
            or not isinstance(scopes, list)
            or not scopes
            or not all(isinstance(scope, str) and scope for scope in scopes)
            or len(scopes) != len(set(scopes))
        ):
            return None
        return ("read",), ("oauth-identity-read",)
    if probe_id == JIRA_COMPANY_ALPHA_PROBE_ID:
        if (
            exact_probe_argv(preferred) != JIRA_COMPANY_ALPHA_PROBE_ARGV
            or not isinstance(payload, dict)
            or set(payload) != JIRA_SUCCESS_SIGNAL_FIELDS
            or payload.get("route") != preferred.get("route_id")
            or payload.get("system") != "jira"
            or payload.get("credential_handle_id") != 'jira.company_alpha.api'
            or payload.get("site_expected")
            != f"https://{preferred.get('required_account')}"
            or payload.get("project_key_expected") != _operator_binding('services.jira.project_key')
            or payload.get("issue_type_expected") != "QA Feedback"
            or payload.get("project_key") != _operator_binding('services.jira.project_key')
            or payload.get("visible_required_field_names")
            != list(JIRA_REQUIRED_FIELD_NAMES)
            or payload.get("missing_required_field_names") != []
            or payload.get("duplicate_required_field_names") != []
            or any(
                payload.get(field) is not True
                for field in (
                    "credential_contract_bound",
                    "site_matches_expected",
                    "auth_ok",
                    "account_identity_matches_registered",
                    "project_ok",
                    "createmeta_ok",
                    "qa_feedback_issue_type_ok",
                    "ok",
                )
            )
        ):
            return None
        for field in (
            "account_type",
            "project_name",
            "project_id",
            "qa_feedback_issue_type_id",
        ):
            value = payload.get(field)
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                return None
        for field in ("project_id", "qa_feedback_issue_type_id"):
            if JIRA_PROVIDER_ID_PATTERN.fullmatch(payload[field]) is None:
                return None
        field_ids = payload.get("field_ids")
        if (
            not isinstance(field_ids, dict)
            or set(field_ids) != set(JIRA_REQUIRED_FIELD_NAMES)
            or not all(
                isinstance(field_id, str)
                and JIRA_PROVIDER_ID_PATTERN.fullmatch(field_id) is not None
                for field_id in field_ids.values()
            )
            or len(set(field_ids.values())) != len(field_ids)
        ):
            return None
        return (
            ("read",),
            ("identity-read", "issue-create-metadata-read", "project-read"),
        )
    if probe_id not in GOOGLE_CALENDAR_PROBE_IDS:
        return None
    if isinstance(payload, list):
        calendars = payload
    elif isinstance(payload, dict):
        carriers = [payload[key] for key in ("calendars", "items") if key in payload]
        if len(carriers) != 1 or not isinstance(carriers[0], list):
            return None
        calendars = carriers[0]
    else:
        return None
    if not all(isinstance(calendar, dict) for calendar in calendars):
        return None
    ids = [calendar.get("id") for calendar in calendars]
    if (
        not all(isinstance(calendar_id, str) for calendar_id in ids)
        or len(ids) != len(set(ids))
    ):
        return None
    primary = [calendar for calendar in calendars if calendar.get("primary") is True]
    if len(primary) != 1:
        return None
    account = preferred.get("required_account")
    if (
        primary[0].get("id") != account
        or primary[0].get("accessRole") not in {"owner", "writer"}
    ):
        return None
    return ("read",), ("calendar-list",)


def run_exact_registered_probe(
    preferred: dict[str, Any] | None,
    transport: ProbeTransport,
) -> tuple[AuthoritativeProbeEvidence | None, dict[str, Any]]:
    """Execute and parse one exact selected probe without persisting evidence."""

    diagnostics: dict[str, Any] = {
        "requested": True,
        "attempted": False,
        "supported": False,
        "result": "blocked_exact_probe_parser_unavailable",
    }
    try:
        from scripts.routing_operator_bindings import require_resolved, require_probe_identity
    except ModuleNotFoundError:
        from routing_operator_bindings import require_resolved, require_probe_identity
    try:
        require_resolved(preferred)
        require_probe_identity(str((preferred or {}).get("probe_id", "")))
    except ValueError:
        diagnostics["result"] = "blocked_operator_binding_missing"
        return None, diagnostics
    argv = exact_probe_argv(preferred)
    if argv is None or preferred is None:
        return None, diagnostics
    diagnostics["supported"] = True
    try:
        require_resolved(argv)
    except ValueError:
        diagnostics["result"] = "blocked_operator_binding_missing"
        return None, diagnostics
    diagnostics["attempted"] = True
    try:
        result = transport(argv, EXACT_PROBE_TIMEOUT_SECONDS)
    except Exception:
        diagnostics["result"] = "blocked_exact_probe_transport_failed"
        return None, diagnostics
    if (
        not isinstance(result, ProbeCommandResult)
        or not isinstance(result.returncode, int)
        or isinstance(result.returncode, bool)
        or not isinstance(result.stdout, str)
        or not isinstance(result.stderr, str)
        or not isinstance(result.outcome, str)
    ):
        diagnostics["result"] = "blocked_exact_probe_transport_invalid"
        return None, diagnostics
    diagnostics["returncode"] = result.returncode
    if result.outcome == "output_too_large":
        diagnostics["result"] = "blocked_exact_probe_output_too_large"
        return None, diagnostics
    if result.outcome == "timeout":
        diagnostics["result"] = "blocked_exact_probe_timeout"
        return None, diagnostics
    if result.outcome != "completed":
        diagnostics["result"] = "blocked_exact_probe_transport_failed"
        return None, diagnostics
    if result.returncode != 0:
        diagnostics["result"] = "blocked_exact_probe_failed"
        return None, diagnostics
    try:
        output_size = len(result.stdout.encode("utf-8")) + len(
            result.stderr.encode("utf-8")
        )
    except UnicodeError:
        diagnostics["result"] = "blocked_exact_probe_output_invalid"
        return None, diagnostics
    if output_size > MAX_PROBE_OUTPUT_BYTES:
        diagnostics["result"] = "blocked_exact_probe_output_too_large"
        return None, diagnostics
    parsed_signal = parse_exact_probe_signal(
        preferred,
        result.stdout,
        result.stderr,
    )
    if parsed_signal is None:
        diagnostics["result"] = "blocked_exact_probe_signal_unverified"
        return None, diagnostics
    evidence_effects, evidence_operations = parsed_signal
    diagnostics["result"] = "passed"
    evidence = AuthoritativeProbeEvidence(
        route_id=str(preferred.get("route_id", "")),
        probe_id=str(preferred.get("probe_id", "")),
        principal=str(preferred.get("required_principal", "")),
        account=str(preferred.get("required_account", "")),
        state="passed",
        verified_at_utc=(
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        evidence_effects=evidence_effects,
        evidence_operations=evidence_operations,
    )
    return evidence, diagnostics


def authoritative_probe_evidence_contract(
    preferred: dict[str, Any] | None,
    evidence: AuthoritativeProbeEvidence | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind runtime probe evidence to one exact sealed route and account."""

    expected_route_id = preferred.get("route_id") if preferred else None
    expected_probe_id = preferred.get("probe_id") if preferred else None
    expected_principal = preferred.get("required_principal") if preferred else None
    expected_account = preferred.get("required_account") if preferred else None
    probe_scope_exact = bool(
        preferred
        and preferred.get("probe_scope") == expected_route_id
    )
    probe_ttl_days = preferred.get("probe_ttl_days") if preferred else None
    provided = isinstance(evidence, AuthoritativeProbeEvidence)
    verified_at_utc = evidence.verified_at_utc if provided else None
    age_days = (
        capability_age_days(verified_at_utc, now=now)
        if isinstance(verified_at_utc, str)
        else None
    )
    ttl_valid = bool(
        isinstance(probe_ttl_days, (int, float))
        and not isinstance(probe_ttl_days, bool)
        and probe_ttl_days > 0
    )
    fresh = bool(
        age_days is not None
        and ttl_valid
        and age_days <= float(probe_ttl_days)
    )
    route_id_exact = bool(provided and evidence.route_id == expected_route_id)
    probe_id_exact = bool(provided and evidence.probe_id == expected_probe_id)
    principal_exact = bool(provided and evidence.principal == expected_principal)
    account_exact = bool(provided and evidence.account == expected_account)
    state_passed = bool(provided and evidence.state == "passed")
    valid = bool(
        provided
        and probe_scope_exact
        and route_id_exact
        and probe_id_exact
        and principal_exact
        and account_exact
        and state_passed
        and fresh
    )
    return {
        "provided": provided,
        "valid": valid,
        "route_id": evidence.route_id if provided else None,
        "probe_id": evidence.probe_id if provided else None,
        "principal": evidence.principal if provided else None,
        "account": evidence.account if provided else None,
        "state": evidence.state if provided else None,
        "verified_at_utc": verified_at_utc,
        "evidence_effects": list(evidence.evidence_effects) if provided else [],
        "evidence_operations": (
            list(evidence.evidence_operations) if provided else []
        ),
        "age_days": age_days,
        "probe_ttl_days": probe_ttl_days if ttl_valid else None,
        "fresh": fresh,
        "route_id_exact": route_id_exact,
        "probe_id_exact": probe_id_exact,
        "probe_scope_exact": probe_scope_exact,
        "principal_exact": principal_exact,
        "account_exact": account_exact,
        "state_passed": state_passed,
        "caller_probe_declarations_authoritative": False,
    }


def readiness_for_status_id(
    readiness_status_id: Any,
    status_by_id: dict[str, dict[str, Any]],
    *,
    probe_ttl_days: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    best = (
        status_by_id.get(str(readiness_status_id))
        if isinstance(readiness_status_id, str) and readiness_status_id
        else None
    )
    if best is None:
        return {
            "state": "unknown",
            "evidence": "no exact readiness status row",
            "readiness_status_id": readiness_status_id,
            "freshness_class": "unknown",
            "domain": "unknown",
            "slo_max_age_days": None,
            "effective_slo_max_age_days": None,
            "age_days": None,
            "slo_breached": None,
            "evidence_effects": [],
            "evidence_operations": [],
        }
    if "screen_capture_binding" in best:
        try:
            try:
                from scripts.openclaw_runtime_activate import verify_screen_capture_capability
            except ModuleNotFoundError:
                from openclaw_runtime_activate import verify_screen_capture_capability
            verify_screen_capture_capability(best["screen_capture_binding"])
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            best = {**best, "state": "unknown", "freshness_class": "reprobe-before-use",
                    "evidence": "ScreenCapture identity, permission or responsible-process evidence is no longer current; use the actual native route acceptance.",
                    "evidence_effects": [], "evidence_operations": []}
    freshness = capability_freshness(
        str(best.get("last_verified_utc", "")),
        best.get("slo_max_age_days"),
        probe_ttl_days=probe_ttl_days,
        now=now,
    )
    raw_evidence_effects = best.get("evidence_effects")
    evidence_effects = (
        list(raw_evidence_effects)
        if isinstance(raw_evidence_effects, list)
        and raw_evidence_effects
        and all(
            isinstance(effect, str) and effect in {"read", "mutation"}
            for effect in raw_evidence_effects
        )
        and len(raw_evidence_effects) == len(set(raw_evidence_effects))
        else []
    )
    try:
        evidence_operations = parse_evidence_operations(
            best.get("evidence_operations"),
            source=f"status row {best.get('capability_id', '<unknown>')!r}",
        )
    except RegistryContractError:
        evidence_operations = []
    return {
        "readiness_status_id": readiness_status_id,
        "state": best.get("state", "unknown"),
        "freshness_class": best.get("freshness_class", "unknown"),
        "last_verified_utc": best.get("last_verified_utc", ""),
        "evidence": best.get("evidence", ""),
        "constraint_or_fallback": best.get("constraint_or_fallback", ""),
        "capability_id": best.get("capability_id", ""),
        "domain": best.get("domain", "unknown"),
        "slo_max_age_days": best.get("slo_max_age_days"),
        "effective_slo_max_age_days": freshness[
            "effective_slo_max_age_days"
        ],
        "age_days": freshness["age_days"],
        "slo_breached": freshness["slo_breached"],
        "evidence_effects": evidence_effects,
        "evidence_operations": evidence_operations,
    }


def readiness_for_route(
    route: dict[str, Any],
    status_by_id: dict[str, dict[str, Any]],
    *,
    probe_ttl_days: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    return readiness_for_status_id(
        route.get("readiness_status_id"),
        status_by_id,
        probe_ttl_days=probe_ttl_days,
        now=now,
    )


def select_preferred_route(
    routes: list[dict[str, Any]],
    *,
    selectors: NormalizedRouteSelectors,
    portfolio_routes: dict[str, dict[str, frozenset[str]]],
) -> RouteSelection:
    """Select by explicit identity and trusted metadata, never prompt prose."""

    ordered = sorted(routes, key=lambda route: str(route.get("route_id", "")))
    system_candidate_ids = tuple(str(route.get("route_id", "")) for route in ordered)

    def missing_discriminators(
        candidates: list[dict[str, Any]],
    ) -> tuple[str, ...]:
        missing: list[str] = []
        if len({str(route.get("required_account", "")) for route in candidates}) > 1:
            missing.append("account")
        if len({normalize_slug(str(route.get("required_principal", ""))) for route in candidates}) > 1:
            missing.append("principal")
        for selector, field in (
            ("portfolio", "portfolio_ids"),
            ("workspace", "workspace_ids"),
            ("network", "network_ids"),
        ):
            values = {
                value
                for route in candidates
                for value in portfolio_routes.get(
                    str(route.get("route_id", "")), {}
                ).get(field, frozenset())
            }
            if len(values) > 1:
                missing.append(selector)
        return tuple(missing)

    def outcome(
        candidates: list[dict[str, Any]],
        *,
        mode: str,
        applied: tuple[str, ...],
        overridden: tuple[str, ...] = (),
        no_match_blocker: str,
        ambiguous_blocker: str = "blocked_ambiguous_exact_route_binding",
    ) -> RouteSelection:
        matched = tuple(
            str(route.get("route_id", ""))
            for route in sorted(
                candidates, key=lambda route: str(route.get("route_id", ""))
            )
        )
        if len(candidates) == 1:
            return RouteSelection(
                candidates[0],
                None,
                "selected",
                mode,
                system_candidate_ids,
                matched,
                applied,
                overridden,
                (),
            )
        return RouteSelection(
            None,
            no_match_blocker if not candidates else ambiguous_blocker,
            "no_match" if not candidates else "ambiguous",
            mode,
            system_candidate_ids,
            matched,
            applied,
            overridden,
            () if not candidates else missing_discriminators(candidates),
        )

    if not ordered:
        return RouteSelection(
            None,
            "blocked_no_declared_route",
            "no_declared_route",
            "none",
            (),
            (),
            (),
            (),
            (),
        )

    invalid = next(
        (
            name
            for name, supplied, canonical in (
                ("account", selectors.account_supplied, selectors.account_canonical),
                ("portfolio", selectors.portfolio_supplied, selectors.portfolio_canonical),
                ("workspace", selectors.workspace_supplied, selectors.workspace_canonical),
                ("network", selectors.network_supplied, selectors.network_canonical),
                ("principal", selectors.principal_supplied, selectors.principal_canonical),
                ("context", selectors.context_supplied, selectors.context_canonical),
            )
            if supplied and not canonical
        ),
        None,
    )
    if invalid:
        return RouteSelection(
            None,
            f"blocked_invalid_{invalid}_selector",
            "invalid_selector",
            "none",
            system_candidate_ids,
            (),
            (),
            (),
            (),
        )

    if selectors.account:
        exact = [
            route
            for route in ordered
            if normalize_route_identity(
                str(route.get("required_account", "")),
                route_identity_normalizer(route),
            )
            == normalize_route_identity(
                selectors.account,
                route_identity_normalizer(route),
            )
        ]
        if len(exact) == 1:
            selected = exact[0]
            binding = portfolio_routes.get(
                str(selected.get("route_id", "")), {}
            )
            context_values = {
                normalize_slug(value)
                for value in field_list(selected.get("default_contexts"))
            }
            conflicts = tuple(
                name
                for name, supplied, matches in (
                    (
                        "workspace",
                        selectors.workspace,
                        selectors.workspace
                        in binding.get("workspace_ids", frozenset()),
                    ),
                    (
                        "network",
                        selectors.network,
                        selectors.network
                        in binding.get("network_ids", frozenset()),
                    ),
                    (
                        "principal",
                        selectors.principal,
                        selectors.principal
                        == normalize_slug(
                            str(selected.get("required_principal", ""))
                        ),
                    ),
                )
                if supplied and not matches
            )
            overridden = tuple(
                name
                for name, supplied, matches in (
                    (
                        "portfolio",
                        selectors.portfolio,
                        selectors.portfolio
                        in binding.get("portfolio_ids", frozenset()),
                    ),
                    (
                        "context",
                        selectors.context,
                        selectors.context in context_values,
                    ),
                )
                if supplied and not matches
            )
            if conflicts:
                return RouteSelection(
                    None,
                    f"blocked_conflicting_{conflicts[0]}_selector",
                    "conflict",
                    "exact_account",
                    system_candidate_ids,
                    (str(selected.get("route_id", "")),),
                    ("account",),
                    overridden,
                    (),
                )
        else:
            overridden = tuple(
                name
                for name, supplied in (
                    ("portfolio", selectors.portfolio),
                    ("workspace", selectors.workspace),
                    ("network", selectors.network),
                    ("principal", selectors.principal),
                    ("context", selectors.context),
                )
                if supplied
            )
        return outcome(
            exact,
            mode="exact_account",
            applied=("account",),
            overridden=overridden,
            no_match_blocker="blocked_no_exact_account_route",
        )

    typed = tuple(
        name
        for name, supplied in (
            ("portfolio", selectors.portfolio),
            ("workspace", selectors.workspace),
            ("network", selectors.network),
        )
        if supplied
    )
    if typed:
        narrowed = ordered
        no_match_selector = typed[0]
        for name, requested, field in (
            ("portfolio", selectors.portfolio, "portfolio_ids"),
            ("workspace", selectors.workspace, "workspace_ids"),
            ("network", selectors.network, "network_ids"),
        ):
            if not requested:
                continue
            before = narrowed
            narrowed = [
                route
                for route in narrowed
                if requested
                in portfolio_routes.get(
                    str(route.get("route_id", "")), {}
                ).get(field, frozenset())
            ]
            if before and not narrowed:
                no_match_selector = name
        if selectors.principal:
            before = narrowed
            narrowed = [
                route
                for route in narrowed
                if normalize_slug(str(route.get("required_principal", "")))
                == selectors.principal
            ]
            if before and not narrowed:
                no_match_selector = "principal"
            applied = typed + ("principal",)
        else:
            applied = typed
        if len(narrowed) > 1 and selectors.portfolio:
            defaults = [
                route
                for route in narrowed
                if selectors.portfolio
                in portfolio_routes.get(
                    str(route.get("route_id", "")), {}
                ).get("default_for_portfolios", frozenset())
            ]
            if len(defaults) == 1:
                narrowed = defaults
        first = typed[0]
        overridden = tuple(
            name
            for name, supplied, matches in (
                (
                    "context",
                    selectors.context,
                    bool(
                        narrowed
                        and all(
                            selectors.context
                            in {
                                normalize_slug(value)
                                for value in field_list(route.get("default_contexts"))
                            }
                            for route in narrowed
                        )
                    ),
                ),
            )
            if supplied and not matches
        )
        return outcome(
            narrowed,
            mode=f"explicit_{first}",
            applied=applied,
            overridden=overridden,
            no_match_blocker=f"blocked_no_exact_{no_match_selector}_route",
        )

    if selectors.principal:
        exact = [
            route
            for route in ordered
            if normalize_slug(str(route.get("required_principal", "")))
            == selectors.principal
        ]
        context_matches = bool(
            exact
            and all(
                selectors.context
                in {
                    normalize_slug(value)
                    for value in field_list(route.get("default_contexts"))
                }
                for route in exact
            )
        )
        return outcome(
            exact,
            mode="exact_principal",
            applied=("principal",),
            overridden=(
                ("context",)
                if selectors.context and not context_matches
                else ()
            ),
            no_match_blocker="blocked_no_exact_principal_route",
        )

    if selectors.context:
        exact = [
            route
            for route in ordered
            if selectors.context
            in {
                normalize_slug(value)
                for value in field_list(route.get("default_contexts"))
            }
        ]
        return outcome(
            exact,
            mode="legacy_context",
            applied=("context",),
            no_match_blocker="blocked_no_exact_context_route",
            ambiguous_blocker="blocked_ambiguous_exact_context_route",
        )

    if len(ordered) == 1:
        return outcome(
            ordered,
            mode="singleton",
            applied=(),
            no_match_blocker="blocked_no_declared_route",
        )
    return RouteSelection(
        None,
        "blocked_ambiguous_route_context_required",
        "ambiguous",
        "none",
        system_candidate_ids,
        system_candidate_ids,
        (),
        (),
        missing_discriminators(ordered),
    )


def portfolio_account_scope_routes(
    routes: list[dict[str, Any]],
    *,
    selectors: NormalizedRouteSelectors,
    portfolio_routes: dict[str, dict[str, frozenset[str]]],
) -> tuple[dict[str, Any], ...]:
    """Return routes inside one explicit portfolio and its typed selectors.

    This deliberately operates on registry metadata, not prompt prose. It is
    used before operation filtering so a mutation cannot silently select the
    only account that happens to advertise the requested write operation.
    """

    if not selectors.portfolio:
        return ()
    scoped: list[dict[str, Any]] = []
    for route in routes:
        binding = portfolio_routes.get(str(route.get("route_id", "")), {})
        if selectors.portfolio not in binding.get("portfolio_ids", frozenset()):
            continue
        if (
            selectors.workspace
            and selectors.workspace
            not in binding.get("workspace_ids", frozenset())
        ):
            continue
        if (
            selectors.network
            and selectors.network not in binding.get("network_ids", frozenset())
        ):
            continue
        if (
            selectors.principal
            and selectors.principal
            != normalize_slug(str(route.get("required_principal", "")))
        ):
            continue
        scoped.append(route)
    return tuple(sorted(scoped, key=lambda route: str(route.get("route_id", ""))))


def discover_portfolio_read_set(
    selection: RouteSelection,
    *,
    routes: list[dict[str, Any]],
    selectors: NormalizedRouteSelectors,
    required_operation: str,
    intent: str,
    ambiguity_resolution: str,
) -> tuple[dict[str, Any], ...]:
    """Resolve account-only read ambiguity to the bounded registry set."""

    if (
        ambiguity_resolution
        != "read_account_set_or_ask_one_concise_question"
        or intent not in READ_INTENTS
        or not selectors.portfolio
        or selectors.account
        or not required_operation
        or selection.state != "ambiguous"
        or selection.missing_discriminators != ("account",)
    ):
        return ()
    matched_ids = set(selection.matched_route_ids)
    matched = tuple(
        sorted(
            (
                route
                for route in routes
                if str(route.get("route_id", "")) in matched_ids
            ),
            key=lambda route: str(route.get("route_id", "")),
        )
    )
    if len(matched) < 2:
        return ()
    accounts: set[str] = set()
    for route in matched:
        account = str(route.get("required_account", "")).strip()
        operations = route.get("operations")
        operation = (
            operations.get(required_operation)
            if isinstance(operations, dict)
            else None
        )
        if (
            not account
            or not isinstance(operation, dict)
            or operation.get("effect") != "read"
            or operation.get("lane") != "declared_native"
        ):
            return ()
        accounts.add(account)
    return matched if len(accounts) > 1 else ()


def route_ambiguity_question(
    selection: RouteSelection,
    *,
    routes: list[dict[str, Any]],
    portfolio_routes: dict[str, dict[str, frozenset[str]]],
) -> str | None:
    """Return one concise question without exposing route or credential internals."""

    if selection.state != "ambiguous":
        return None
    matched_ids = set(selection.matched_route_ids)
    matched = [
        route
        for route in routes
        if str(route.get("route_id", "")) in matched_ids
    ]
    for discriminator in (
        "network",
        "account",
        "workspace",
        "portfolio",
        "principal",
    ):
        if discriminator not in selection.missing_discriminators:
            continue
        if discriminator == "account":
            values = sorted(
                {
                    str(route.get("required_account", "")).strip()
                    for route in matched
                    if str(route.get("required_account", "")).strip()
                }
            )
        elif discriminator == "principal":
            values = sorted(
                {
                    str(route.get("required_principal", "")).strip()
                    for route in matched
                    if str(route.get("required_principal", "")).strip()
                }
            )
        else:
            field = {
                "network": "network_ids",
                "workspace": "workspace_ids",
                "portfolio": "portfolio_ids",
            }[discriminator]
            values = sorted(
                {
                    value
                    for route in matched
                    for value in portfolio_routes.get(
                        str(route.get("route_id", "")), {}
                    ).get(field, frozenset())
                }
            )
        if values:
            return f"Which {discriminator} should I use: {', '.join(values)}?"
    return "Which account should I use?"


def route_required_account(
    preferred: dict[str, Any] | None,
    route_field: str = "required_account",
    *,
    identity_normalizer: str = "exact",
) -> str | None:
    if not preferred:
        return None
    structured_value = preferred.get(route_field)
    structured = (
        normalize_route_identity(structured_value, identity_normalizer)
        if isinstance(structured_value, str)
        else ""
    )
    if structured:
        return structured
    return None


def normalize_route_identity(value: str, normalizer: str) -> str:
    normalized = value.strip()
    if normalizer == "casefold":
        return normalized.casefold()
    if normalizer == "exact":
        return normalized
    raise ValueError(f"unsupported route identity normalizer: {normalizer}")


def route_identity_normalizer(route: dict[str, Any] | None) -> str:
    """Return the route-declared account normalizer used for selection."""

    adapter = route.get("provider_adapter") if isinstance(route, dict) else None
    identity_policy = (
        adapter.get("identity_policy") if isinstance(adapter, dict) else None
    )
    normalizer = (
        identity_policy.get("normalizer")
        if isinstance(identity_policy, dict)
        else None
    )
    return normalizer if normalizer in {"exact", "casefold"} else "exact"


def valid_dns_domain(value: str | None) -> bool:
    if value is None:
        return True
    labels = value.split(".")
    return (
        len(labels) >= 2
        and all(labels)
        and all(
            label[0].isalnum()
            and label[-1].isalnum()
            and all(character.isalnum() or character == "-" for character in label)
            for label in labels
        )
    )


def normalize_route_request_evidence(value: str, normalizer: str) -> str | None:
    normalized = value.strip()
    if normalizer == "dns_domain":
        normalized = normalized.lower().rstrip(".")
    elif normalizer != "trimmed_text":
        raise ValueError(f"unsupported provider-adapter normalizer: {normalizer}")
    return normalized or None


def safe_url_host(value: str) -> str | None:
    if not value.strip():
        return None
    try:
        return (urlparse(value).hostname or "").lower() or None
    except ValueError:
        return None


def parse_route_evidence_argument(value: str) -> tuple[str, str]:
    key, separator, evidence_value = value.partition("=")
    normalized_key = key.strip()
    if not separator or not re.fullmatch(r"[a-z][a-z0-9_]*", normalized_key):
        raise argparse.ArgumentTypeError(
            "route evidence must use a lowercase NAME=VALUE key"
        )
    return normalized_key, evidence_value


def browser_profile_contract(
    preferred: dict[str, Any] | None,
    routing_defaults: dict[str, Any] | None = None,
) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    preferred = preferred or {}
    routing_defaults = routing_defaults or {}
    raw_primary = preferred.get("browser_profile_primary") or routing_defaults.get(
        "managed_browser_primary"
    )
    primary: dict[str, str] | None = None
    if isinstance(raw_primary, dict):
        profile_id = str(raw_primary.get("profile_id", "")).strip()
        transport = str(raw_primary.get("transport", "")).strip()
        if profile_id and transport:
            primary = {"profile_id": profile_id, "transport": transport}

    fallbacks: list[dict[str, Any]] = []
    raw_fallbacks = preferred.get("browser_profile_fallbacks") or routing_defaults.get(
        "browser_profile_fallbacks"
    )
    if isinstance(raw_fallbacks, list):
        for raw_fallback in raw_fallbacks:
            if not isinstance(raw_fallback, dict):
                continue
            profile_id = str(raw_fallback.get("profile_id", "")).strip()
            automatic = raw_fallback.get("automatic")
            requires_explicit_handoff = raw_fallback.get(
                "requires_explicit_existing_session_handoff"
            )
            if (
                profile_id
                and isinstance(automatic, bool)
                and isinstance(requires_explicit_handoff, bool)
            ):
                fallbacks.append(
                    {
                        "profile_id": profile_id,
                        "automatic": automatic,
                        "requires_explicit_existing_session_handoff": (
                            requires_explicit_handoff
                        ),
                    }
                )
    return primary, fallbacks


def browser_profile_selection(
    primary: dict[str, str] | None,
    fallbacks: list[dict[str, Any]],
    *,
    explicit_existing_session_handoff: bool,
) -> dict[str, list[str]]:
    """Project automatic and attended profiles without special-casing a name."""

    automatic = [
        fallback["profile_id"]
        for fallback in fallbacks
        if fallback["automatic"]
        and not fallback["requires_explicit_existing_session_handoff"]
    ]
    attended = [
        fallback["profile_id"]
        for fallback in fallbacks
        if not fallback["automatic"]
        and fallback["requires_explicit_existing_session_handoff"]
    ]
    return {
        "order": (
            ([primary["profile_id"]] if primary else []) + automatic
        ),
        "automatic_fallbacks": automatic,
        "attended_fallbacks": attended,
        "eligible_attended_fallbacks": (
            attended if explicit_existing_session_handoff else []
        ),
    }


def build_declarative_target_url_evidence(
    target_url: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Extract non-sensitive URL evidence using a route-declared contract."""

    provided = bool(target_url.strip())
    try:
        parsed = urlparse(target_url) if provided else None
    except ValueError:
        parsed = None
    host = (parsed.hostname or "").lower() if parsed else ""
    path = parsed.path if parsed else ""
    result: dict[str, Any] = {"provided": provided, "host": host or None}

    pattern = str(contract.get("path_capture_pattern", "")).strip()
    match = re.search(pattern, path, re.IGNORECASE) if pattern else None
    matched_output_key = str(contract.get("matched_output_key", "matched")).strip()
    captured_output_key = str(contract.get("captured_output_key", "captured_value")).strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", matched_output_key):
        raise ValueError("invalid target URL evidence matched output key")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", captured_output_key):
        raise ValueError("invalid target URL evidence captured output key")

    capture_group = contract.get("capture_group", 1)
    if (
        not isinstance(capture_group, int)
        or isinstance(capture_group, bool)
        or capture_group < 0
    ):
        raise ValueError("target URL evidence capture_group must be a non-negative integer")
    try:
        captured_value = match.group(capture_group) if match else None
    except IndexError as exc:
        raise ValueError("target URL evidence capture_group is absent from pattern") from exc
    captured_value_format = str(contract.get("captured_value_format", "")).strip()
    if captured_value_format != "non_negative_integer":
        raise ValueError(
            "target URL evidence captured_value_format must be non_negative_integer"
        )
    captured_value_max_length = contract.get("captured_value_max_length", 10)
    if (
        not isinstance(captured_value_max_length, int)
        or isinstance(captured_value_max_length, bool)
        or captured_value_max_length < 1
        or captured_value_max_length > 32
    ):
        raise ValueError(
            "target URL evidence captured_value_max_length must be between 1 and 32"
        )
    captured_value_is_safe = bool(
        captured_value
        and len(captured_value) <= captured_value_max_length
        and re.fullmatch(r"[0-9]+", captured_value)
    )
    result[matched_output_key] = bool(match) and captured_value_is_safe
    result[captured_output_key] = captured_value if captured_value_is_safe else None

    constant_outputs = contract.get("constant_outputs", {})
    if not isinstance(constant_outputs, dict):
        raise ValueError("target URL evidence constant_outputs must be an object")
    for output_key, output_value in constant_outputs.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(output_key)):
            raise ValueError("invalid target URL evidence constant output key")
        result[str(output_key)] = output_value
    return result


def build_declarative_account_route_guard(
    *,
    preferred: dict[str, Any] | None,
    adapter: dict[str, Any],
    candidate_lane: str,
    api_probe_state: str,
    api_probe_account: str,
    target_url: str,
    browser_account: str,
    user_requested_ui_state: bool,
    operation_contract: dict[str, Any],
    explicit_existing_session_handoff: bool = False,
    routing_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a route-declared exact-account and native-first fallback contract."""

    adapter_type = str(adapter.get("type", "")).strip()
    if adapter_type != "authenticated_account_route":
        raise ValueError(
            f"unsupported account route adapter type: {adapter_type or '<missing>'}"
        )
    identity_policy = adapter.get("identity_policy", {})
    if not isinstance(identity_policy, dict):
        identity_policy = {}
    account_verification_required = bool(identity_policy.get("required", False))
    identity_normalizer = str(identity_policy.get("normalizer", "exact")).strip()
    account_route_field = str(
        adapter.get("required_account_route_field", "required_account")
    ).strip()
    required_account = route_required_account(
        preferred,
        account_route_field or "required_account",
        identity_normalizer=identity_normalizer,
    )

    native_evidence = adapter.get("native_evidence", {})
    if not isinstance(native_evidence, dict):
        native_evidence = {}
    passed_states = set(field_list(native_evidence.get("passed_states")))
    exhausted_states = set(field_list(native_evidence.get("exhausted_states")))
    digest_contract = provider_account_digest_contract(
        identity_policy, native_evidence
    )
    adapter_contract_valid = bool(
        passed_states
        and exhausted_states
        and passed_states.isdisjoint(exhausted_states)
        and digest_contract["contract_valid"]
    )
    browser_evidence = adapter.get("browser_evidence", {})
    if not isinstance(browser_evidence, dict):
        browser_evidence = {}

    normalized_api_probe_account = (
        normalize_route_identity(api_probe_account, identity_normalizer) or None
    )
    normalized_browser_account = (
        normalize_route_identity(browser_account, identity_normalizer) or None
    )
    browser_account_matches_required = bool(
        required_account
        and normalized_browser_account
        and normalized_browser_account == required_account
    )
    target_url_contract = adapter.get("target_url_evidence", {})
    if not isinstance(target_url_contract, dict):
        target_url_contract = {}
    url_evidence = (
        build_declarative_target_url_evidence(target_url, target_url_contract)
        if target_url_contract
        else {
            "provided": bool(target_url.strip()),
            "host": safe_url_host(target_url),
        }
    )
    declared_api_probe_account_matches_required = bool(
        required_account
        and normalized_api_probe_account
        and normalized_api_probe_account == required_account
    )
    matched_browser_fallback_predicates: list[str] = []
    if bool(operation_contract.get("native_unsupported", False)):
        matched_browser_fallback_predicates.append("native_operation_unsupported")
    if api_probe_state == "failed":
        matched_browser_fallback_predicates.append("native_probe_failed")
    if api_probe_state == "unavailable":
        matched_browser_fallback_predicates.append("native_route_unavailable")
    if bool(operation_contract.get("authenticated_ui_required", False)):
        matched_browser_fallback_predicates.append("authenticated_ui_required")
    if user_requested_ui_state:
        matched_browser_fallback_predicates.append("user_requested_ui_state")
    browser_fallback_eligible = bool(
        matched_browser_fallback_predicates
        and operation_contract.get("operation_registered", False)
        and operation_contract.get("intent_known", False)
        and not operation_contract.get("intent_effect_mismatch", False)
    )

    # CLI inputs are caller declarations, not runtime-bound receipts. They can
    # reject an account mismatch, but they cannot verify identity, exhaust the
    # native route, or authorize a completion claim.
    api_probe_account_verified = False
    browser_account_verified = False
    api_route_verified = False
    api_route_exhausted = False

    browser_profile_primary, browser_profile_fallbacks = browser_profile_contract(
        preferred,
        routing_defaults,
    )
    browser_profiles = browser_profile_selection(
        browser_profile_primary,
        browser_profile_fallbacks,
        explicit_existing_session_handoff=explicit_existing_session_handoff,
    )
    user_profile_fallback = next(
        (
            fallback
            for fallback in browser_profile_fallbacks
            if fallback["profile_id"] == "user"
        ),
        None,
    )
    automatic_user_profile_fallback_allowed = bool(
        user_profile_fallback and user_profile_fallback["automatic"]
    )
    user_profile_fallback_eligible = bool(
        user_profile_fallback
        and not user_profile_fallback["automatic"]
        and user_profile_fallback["requires_explicit_existing_session_handoff"]
        and explicit_existing_session_handoff
    )

    allowed = True
    blocker_code: str | None = None
    decision = str(
        native_evidence.get("default_decision", "probe_declared_api_route")
    )
    selected_lane_verified = False
    if not adapter_contract_valid:
        allowed = False
        blocker_code = "blocked_provider_adapter_invalid_contract"
        decision = "block_invalid_account_route_adapter_contract"
    elif account_verification_required and required_account is None:
        allowed = False
        blocker_code = str(
            identity_policy.get(
                "missing_route_account_blocker_code",
                "blocked_provider_adapter_required_account_missing",
            )
        )
        decision = str(
            identity_policy.get(
                "missing_route_account_decision",
                "block_missing_route_account_metadata",
            )
        )
    elif normalized_api_probe_account and not declared_api_probe_account_matches_required:
        allowed = False
        blocker_code = str(
            native_evidence.get("account_mismatch_blocker_code", "")
        ) or None
        decision = str(
            native_evidence.get(
                "account_mismatch_decision",
                "block_mismatched_native_probe_account",
            )
        )
    elif candidate_lane == "browser":
        if normalized_browser_account and not browser_account_matches_required:
            allowed = False
            blocker_code = str(
                browser_evidence.get("account_mismatch_blocker_code", "")
            ) or None
            decision = str(
                browser_evidence.get(
                    "account_mismatch_decision",
                    "block_unverified_browser_account",
                )
            )
        elif not browser_fallback_eligible:
            allowed = False
            blocker_code = str(
                browser_evidence.get("native_not_exhausted_blocker_code", "")
            ) or None
            decision = str(
                browser_evidence.get(
                    "native_not_exhausted_decision",
                    "block_browser_until_ui_fallback_predicate",
                )
            )
        elif not browser_account_matches_required:
            allowed = False
            blocker_code = str(
                browser_evidence.get("account_mismatch_blocker_code", "")
            ) or None
            decision = str(
                browser_evidence.get(
                    "account_mismatch_decision",
                    "block_unverified_browser_account",
                )
            )
        elif bool(operation_contract.get("mutation_intent", False)):
            allowed = False
            blocker_code = str(
                browser_evidence.get("account_mismatch_blocker_code", "")
            ) or "blocked_browser_mutation_account_unverified"
            decision = "block_unverified_browser_mutation_account"
        else:
            decision = str(
                browser_evidence.get(
                    "allowed_decision",
                    "allow_browser_evidence_collection",
                )
            )
        if allowed:
            allowed = False
            blocker_code = "blocked_browser_runtime_evidence_required"
            decision = "require_runtime_verified_browser_route_and_account"

    return {
        "provider_adapter_type": adapter_type,
        "candidate_lane": candidate_lane,
        "selected_route_id": preferred.get("route_id") if preferred else None,
        "required_account": required_account,
        "required_provider_account_id_sha256": (
            digest_contract["digest"]
        ),
        "adapter_contract_valid": adapter_contract_valid,
        "account_verification_required": account_verification_required,
        "provider_account_digest_required": digest_contract["required"],
        "api_probe_state": api_probe_state,
        "api_probe_account": normalized_api_probe_account,
        "declared_api_probe_account_matches_required": (
            declared_api_probe_account_matches_required
        ),
        "api_probe_account_verified": api_probe_account_verified,
        "api_probe_claims_are_authoritative": False,
        "api_probe_receipts_are_audit_only": True,
        "api_completion_receipt_supported": bool(
            native_evidence.get("completion_receipt_supported", False)
        ),
        "live_probe_required": not api_route_verified,
        "route_verified": api_route_verified,
        "api_route_exhausted_for_browser_fallback": api_route_exhausted,
        "browser_account": normalized_browser_account,
        "browser_account_matches_required": browser_account_matches_required,
        "browser_account_verified": browser_account_verified,
        "browser_completion_authority": str(
            browser_evidence.get("completion_authority", "none")
        ),
        "browser_profile_primary": browser_profile_primary,
        "browser_profile_fallbacks": browser_profile_fallbacks,
        "browser_profile_order": browser_profiles["order"],
        "automatic_browser_profile_fallbacks": browser_profiles[
            "automatic_fallbacks"
        ],
        "attended_browser_profile_fallbacks": browser_profiles[
            "attended_fallbacks"
        ],
        "eligible_attended_browser_profile_fallbacks": browser_profiles[
            "eligible_attended_fallbacks"
        ],
        "automatic_user_profile_fallback_allowed": (
            automatic_user_profile_fallback_allowed
        ),
        "explicit_existing_session_handoff": explicit_existing_session_handoff,
        "user_profile_fallback_eligible": user_profile_fallback_eligible,
        "target_url_evidence": url_evidence,
        "matched_browser_fallback_predicates": matched_browser_fallback_predicates,
        "browser_fallback_eligible": browser_fallback_eligible,
        "completion_claim_allowed": False,
        "allowed": allowed,
        "selected_lane_verified": selected_lane_verified,
        "decision": decision,
        "blocker_code": blocker_code,
    }


def build_declarative_provider_guard(
    *,
    preferred: dict[str, Any] | None,
    adapter: dict[str, Any],
    candidate_lane: str,
    api_probe_state: str,
    api_probe_account: str,
    browser_account: str,
    request_evidence: dict[str, str],
    user_requested_ui_state: bool,
    explicit_existing_session_handoff: bool,
    operation_contract: dict[str, Any],
) -> dict[str, Any]:
    """Apply a route-declared provider evidence contract.

    The resolver understands generic identity, operation, request-evidence, and
    completion-authority semantics only. Provider vocabulary, validation
    choices, blocker codes, decisions, and user-facing constraints live in the
    selected integration-route record.
    """

    adapter_type = str(adapter.get("type", "")).strip()
    if adapter_type != "authenticated_provider_evidence":
        raise ValueError(
            f"unsupported provider adapter type: {adapter_type or '<missing>'}"
        )

    identity_policy = adapter.get("identity_policy", {})
    if not isinstance(identity_policy, dict):
        identity_policy = {}
    account_verification_required = bool(identity_policy.get("required", False))
    identity_normalizer = str(identity_policy.get("normalizer", "exact")).strip()
    account_route_field = str(
        adapter.get("required_account_route_field", "required_account")
    ).strip()
    required_account = route_required_account(
        preferred,
        account_route_field or "required_account",
        identity_normalizer=identity_normalizer,
    )
    browser_profile_primary, browser_profile_fallbacks = browser_profile_contract(
        preferred
    )
    browser_profiles = browser_profile_selection(
        browser_profile_primary,
        browser_profile_fallbacks,
        explicit_existing_session_handoff=explicit_existing_session_handoff,
    )
    user_profile_fallback = next(
        (
            fallback
            for fallback in browser_profile_fallbacks
            if fallback["profile_id"] == "user"
        ),
        None,
    )
    automatic_user_profile_fallback_allowed = bool(
        user_profile_fallback and user_profile_fallback["automatic"]
    )
    user_profile_fallback_eligible = bool(
        user_profile_fallback
        and not user_profile_fallback["automatic"]
        and user_profile_fallback["requires_explicit_existing_session_handoff"]
        and explicit_existing_session_handoff
    )
    normalized_api_probe_account = (
        normalize_route_identity(api_probe_account, identity_normalizer) or None
    )
    normalized_browser_account = (
        normalize_route_identity(browser_account, identity_normalizer) or None
    )
    normalized_operation = operation_contract["required_operation"]
    operation_specified = normalized_operation is not None
    operation_known = operation_contract["classification"] != "unknown"
    ui_only_operation = operation_contract["authenticated_ui_required"]
    api_supports_required_operation = operation_contract["native_supported"]

    normalized_request_evidence: dict[str, Any] = {}
    request_evidence_valid = True
    invalid_evidence_spec: dict[str, Any] | None = None
    evidence_contract = adapter.get("request_evidence", {})
    if not isinstance(evidence_contract, dict):
        evidence_contract = {}
    undeclared_request_evidence = sorted(
        set(request_evidence) - {str(key) for key in evidence_contract}
    )
    for input_key, raw_spec in evidence_contract.items():
        if not isinstance(raw_spec, dict):
            continue
        normalizer = str(raw_spec.get("normalizer", "trimmed_text")).strip()
        output_key = str(raw_spec.get("output_key", input_key)).strip() or str(
            input_key
        )
        normalized_value = normalize_route_request_evidence(
            request_evidence.get(str(input_key), ""),
            normalizer,
        )
        normalized_request_evidence[output_key] = normalized_value
        validity_output_key = str(raw_spec.get("validity_output_key", "")).strip()
        if validity_output_key:
            value_valid = (
                valid_dns_domain(normalized_value)
                if normalizer == "dns_domain"
                else True
            )
            normalized_request_evidence[validity_output_key] = value_valid
            if not value_valid and invalid_evidence_spec is None:
                request_evidence_valid = False
                invalid_evidence_spec = raw_spec

    browser_account_matches_required = bool(
        required_account
        and normalized_browser_account
        and normalized_browser_account == required_account
    )
    declared_api_probe_account_matches_required = bool(
        required_account
        and normalized_api_probe_account
        and normalized_api_probe_account == required_account
    )

    native_evidence = adapter.get("native_evidence", {})
    if not isinstance(native_evidence, dict):
        native_evidence = {}
    digest_contract = provider_account_digest_contract(
        identity_policy, native_evidence
    )
    probe_receipt_authority = str(
        native_evidence.get("probe_receipt_authority", "none")
    ).strip()
    # Resolver inputs are caller declarations. Route metadata can describe the
    # required authority, but it cannot elevate those declarations into a
    # receipt. Verified and exhausted states stay false until a runtime-owned
    # receipt channel exists outside this read-only resolver.
    api_probe_claims_are_authoritative = False
    api_probe_account_verified = False
    browser_account_verified = False
    browser_property_verified = False
    api_route_verified = False
    api_route_exhausted = False

    completion_evidence = adapter.get("completion_evidence", {})
    if not isinstance(completion_evidence, dict):
        completion_evidence = {}
    evidence_required_operations = set(
        field_list(completion_evidence.get("required_for_operations"))
    )
    provider_example_evidence_required = bool(
        (
            normalized_operation
            and normalized_operation in evidence_required_operations
        )
        or (
            completion_evidence.get("required_when_authenticated_ui_required", False)
            and ui_only_operation
        )
    )
    provider_example_evidence_verified = False

    allowed = True
    blocker_code: str | None = None
    decision = "probe_declared_api_route"
    selected_lane_verified = False
    operation_policy = adapter.get("operation_policy", {})
    if not isinstance(operation_policy, dict):
        operation_policy = {}
    operation_required = bool(operation_policy.get("required", False))
    browser_evidence = adapter.get("browser_evidence", {})
    if not isinstance(browser_evidence, dict):
        browser_evidence = {}

    if not digest_contract["contract_valid"]:
        allowed = False
        blocker_code = "blocked_provider_adapter_invalid_contract"
        decision = "block_invalid_provider_identity_digest_contract"
    elif undeclared_request_evidence:
        allowed = False
        blocker_code = "blocked_undeclared_provider_evidence_input"
        decision = "block_undeclared_request_evidence"
    elif account_verification_required and required_account is None:
        allowed = False
        blocker_code = str(
            identity_policy.get(
                "missing_route_account_blocker_code",
                "blocked_provider_adapter_required_account_missing",
            )
        )
        decision = str(
            identity_policy.get(
                "missing_route_account_decision",
                "block_missing_route_account_metadata",
            )
        )
    elif normalized_api_probe_account and not declared_api_probe_account_matches_required:
        allowed = False
        blocker_code = str(
            native_evidence.get(
                "account_mismatch_blocker_code",
                "blocked_provider_account_route_mismatch",
            )
        )
        decision = str(
            native_evidence.get(
                "account_mismatch_decision",
                "block_mismatched_native_probe_account",
            )
        )
    elif not request_evidence_valid and invalid_evidence_spec:
        allowed = False
        blocker_code = (
            str(invalid_evidence_spec.get("invalid_blocker_code", "")) or None
        )
        decision = str(
            invalid_evidence_spec.get("invalid_decision", "block_invalid_request_evidence")
        )
    elif candidate_lane == "declared_api":
        if operation_required and not operation_specified:
            allowed = False
            blocker_code = (
                str(operation_policy.get("missing_blocker_code", "")) or None
            )
            decision = str(
                operation_policy.get("missing_decision", "require_explicit_operation")
            )
        elif not operation_known:
            allowed = False
            blocker_code = (
                str(operation_policy.get("unknown_blocker_code", "")) or None
            )
            decision = str(
                operation_policy.get("unknown_decision", "block_unknown_operation")
            )
        elif ui_only_operation:
            allowed = False
            blocker_code = str(
                operation_policy.get("native_unsupported_blocker_code", "")
            ) or None
            decision = str(
                operation_policy.get(
                    "native_unsupported_decision",
                    "use_runtime_authenticated_browser_evidence_lane",
                )
            )
        elif api_supports_required_operation:
            decision = str(
                native_evidence.get(
                    "supported_operation_decision", "allow_native_evidence_collection"
                )
            )
    elif candidate_lane == "browser":
        if (operation_required and not operation_specified) or not operation_known:
            allowed = False
            blocker_code = (
                str(operation_policy.get("unknown_blocker_code", "")) or None
            )
            decision = str(
                operation_policy.get("unknown_decision", "block_unknown_operation")
            )
        elif (
            not ui_only_operation
            and not api_route_exhausted
            and not user_requested_ui_state
        ):
            allowed = False
            blocker_code = str(
                browser_evidence.get("native_not_exhausted_blocker_code", "")
            ) or None
            decision = str(
                browser_evidence.get(
                    "native_not_exhausted_decision",
                    "block_browser_until_native_probe_fails",
                )
            )
        elif not browser_account_matches_required:
            allowed = False
            blocker_code = str(
                browser_evidence.get("account_mismatch_blocker_code", "")
            ) or None
            decision = str(
                browser_evidence.get(
                    "account_mismatch_decision", "block_unverified_browser_account"
                )
            )
        else:
            decision = str(
                browser_evidence.get(
                    "allowed_decision", "allow_browser_evidence_collection"
                )
            )
        if allowed:
            allowed = False
            blocker_code = "blocked_browser_runtime_evidence_required"
            decision = "require_runtime_verified_browser_route_and_account"

    completion_claim_allowed = bool(
        completion_evidence.get("resolver_can_verify", False)
        and provider_example_evidence_verified
    )

    result = {
        "provider_adapter_type": adapter_type,
        "undeclared_request_evidence": undeclared_request_evidence,
        "candidate_lane": candidate_lane,
        "selected_route_id": preferred.get("route_id") if preferred else None,
        "required_account": required_account,
        "required_provider_account_id_sha256": digest_contract["digest"],
        "adapter_contract_valid": digest_contract["contract_valid"],
        "provider_account_digest_required": digest_contract["required"],
        "browser_profile_primary": browser_profile_primary,
        "browser_profile_fallbacks": browser_profile_fallbacks,
        "account_verification_required": account_verification_required,
        "required_operation": normalized_operation,
        "operation_specified": operation_specified,
        "operation_known": operation_known,
        "ui_only_operation": ui_only_operation,
        "api_supports_required_operation": api_supports_required_operation,
        "operation_contract_source": operation_contract["classification_source"],
        "api_probe_receipts_are_audit_only": bool(
            native_evidence.get("probe_receipts_are_audit_only", False)
        ),
        "api_completion_receipt_supported": bool(
            native_evidence.get("completion_receipt_supported", False)
        ),
        "api_probe_state": api_probe_state,
        "api_probe_account": normalized_api_probe_account,
        "declared_api_probe_account_matches_required": declared_api_probe_account_matches_required,
        "api_probe_claims_are_authoritative": api_probe_claims_are_authoritative,
        "api_probe_account_verified": api_probe_account_verified,
        "api_route_verified": api_route_verified,
        "api_route_exhausted_for_browser_fallback": api_route_exhausted,
        "api_probe_receipt_authority": probe_receipt_authority,
        "browser_account": normalized_browser_account,
        "browser_account_matches_required": browser_account_matches_required,
        "browser_account_verified": browser_account_verified,
        "browser_property_verified": browser_property_verified,
        "browser_completion_authority": str(
            browser_evidence.get("completion_authority", "none")
        ),
        "browser_profile_order": browser_profiles["order"],
        "automatic_browser_profile_fallbacks": browser_profiles[
            "automatic_fallbacks"
        ],
        "attended_browser_profile_fallbacks": browser_profiles[
            "attended_fallbacks"
        ],
        "eligible_attended_browser_profile_fallbacks": browser_profiles[
            "eligible_attended_fallbacks"
        ],
        "automatic_user_profile_fallback_allowed": (
            automatic_user_profile_fallback_allowed
        ),
        "explicit_existing_session_handoff": explicit_existing_session_handoff,
        "user_profile_fallback_eligible": user_profile_fallback_eligible,
        "host_os_ui_automation_last_resort": True,
        "provider_example_evidence_required": provider_example_evidence_required,
        "provider_example_evidence_verified": provider_example_evidence_verified,
        "completion_claim_allowed": completion_claim_allowed,
        "unresolved_evidence_disposition": (
            None
            if completion_claim_allowed
            else str(
                completion_evidence.get(
                    "unresolved_disposition", "blocked_or_unverified"
                )
            )
        ),
        "prohibited_claims_without_provider_evidence": (
            field_list(completion_evidence.get("prohibited_claims"))
            if provider_example_evidence_required
            and not provider_example_evidence_verified
            else []
        ),
        "allowed": allowed,
        "selected_lane_verified": selected_lane_verified,
        "decision": decision,
        "blocker_code": blocker_code,
    }
    result.update(normalized_request_evidence)
    return result


def build_declarative_route_guard(
    *,
    preferred: dict[str, Any] | None,
    adapter: dict[str, Any],
    candidate_lane: str,
    api_probe_state: str,
    api_probe_account: str,
    target_url: str,
    browser_account: str,
    request_evidence: dict[str, str],
    user_requested_ui_state: bool,
    explicit_existing_session_handoff: bool,
    operation_contract: dict[str, Any],
    routing_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch a declarative route guard by its provider-neutral adapter type."""

    adapter_type = str(adapter.get("type", "")).strip()
    if adapter_type == "authenticated_account_route":
        return build_declarative_account_route_guard(
            preferred=preferred,
            adapter=adapter,
            candidate_lane=candidate_lane,
            api_probe_state=api_probe_state,
            api_probe_account=api_probe_account,
            target_url=target_url,
            browser_account=browser_account,
            user_requested_ui_state=user_requested_ui_state,
            operation_contract=operation_contract,
            explicit_existing_session_handoff=explicit_existing_session_handoff,
            routing_defaults=routing_defaults,
        )
    return build_declarative_provider_guard(
        preferred=preferred,
        adapter=adapter,
        candidate_lane=candidate_lane,
        api_probe_state=api_probe_state,
        api_probe_account=api_probe_account,
        browser_account=browser_account,
        request_evidence=request_evidence,
        user_requested_ui_state=user_requested_ui_state,
        explicit_existing_session_handoff=explicit_existing_session_handoff,
        operation_contract=operation_contract,
    )


def resolve(
    system: str,
    intent: str,
    context: str = "",
    *,
    candidate_lane: str = "declared_api",
    api_probe_state: str = "not_run",
    api_probe_account: str = "",
    target_url: str = "",
    browser_account: str = "",
    required_operation: str = "",
    property_domain: str = "",
    issue_label: str = "",
    route_evidence: dict[str, str] | None = None,
    native_operation_support: str = "unknown",
    authenticated_ui_required: bool = False,
    requested_principal: str = "",
    requested_account: str = "",
    requested_portfolio: str = "",
    requested_workspace: str = "",
    requested_network: str = "",
    explicit_existing_session_handoff: bool = False,
    user_requested_ui_state: bool = False,
    run_exact_probe: bool = False,
    probe_transport: ProbeTransport | None = None,
) -> dict[str, Any]:
    normalized_intent = normalize_intent(intent)
    selectors = normalize_route_selectors(
        context=context,
        portfolio=requested_portfolio,
        workspace=requested_workspace,
        network=requested_network,
        principal=requested_principal,
        account=requested_account,
    )
    route_registry = load_json("registry/integration_routes.json")
    system_aliases = route_registry.get("system_aliases", {})
    if not isinstance(system_aliases, dict):
        system_aliases = {}
    systems = system_candidates(system, system_aliases)
    route_by_id = unique_record_index(
        route_registry.get("routes", []),
        key="route_id",
        source="registry/integration_routes.json",
    )
    try:
        assert_integration_registry_contract(
            route_registry,
            source="registry/integration_routes.json",
        )
    except RegistryContractError as exc:
        raise SystemExit(str(exc)) from exc
    routes = list(route_by_id.values())
    portfolio_routes = portfolio_route_index(
        route_registry,
        route_ids=set(route_by_id),
    )
    authoritative_read_sources = (
        portfolio_authoritative_read_sources(
            route_registry,
            portfolio=selectors.portfolio,
        )
        if selectors.portfolio_supplied and selectors.portfolio_canonical
        else ()
    )
    credential_handles_by_route = credential_handle_route_index(
        route_registry,
        route_ids=set(route_by_id),
    )
    routing_defaults = route_registry.get("routing_defaults", {})
    requested_system = normalize_slug(system)
    all_matching = [
        route
        for route in routes
        if normalize_slug(str(route.get("system", ""))) in systems
    ]
    normalized_required_operation = normalize_slug(required_operation)

    def operation_matches(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            route
            for route in candidates
            if normalized_required_operation
            and isinstance(route.get("operations"), dict)
            and normalized_required_operation in route["operations"]
        ]

    def operation_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return operation_matches(candidates) or candidates

    direct_matching = [
        route
        for route in all_matching
        if normalize_slug(str(route.get("system", ""))) == requested_system
    ]
    if direct_matching and len(systems) > 1:
        alias_matching = [
            route
            for route in all_matching
            if normalize_slug(str(route.get("system", ""))) != requested_system
        ]
        direct_operation_matching = operation_matches(direct_matching)
        alias_operation_matching = operation_matches(alias_matching)
        used_alias_for_operation = bool(
            normalized_required_operation
            and alias_operation_matching
            and not direct_operation_matching
        )
        if used_alias_for_operation:
            matching = alias_operation_matching
            direct_candidates = direct_matching
        else:
            direct_candidates = direct_operation_matching or direct_matching
        direct_selection = select_preferred_route(
            direct_candidates,
            selectors=selectors,
            portfolio_routes=portfolio_routes,
        )
        if used_alias_for_operation:
            matching = alias_operation_matching
        elif direct_selection.state == "no_match":
            alias_candidates = alias_operation_matching or alias_matching
            alias_selection = select_preferred_route(
                alias_candidates,
                selectors=selectors,
                portfolio_routes=portfolio_routes,
            )
            matching = (
                alias_candidates
                if alias_selection.state != "no_match"
                else direct_candidates
            )
        else:
            matching = direct_candidates
    else:
        matching = operation_candidates(all_matching)
    portfolio_account_routes = portfolio_account_scope_routes(
        all_matching,
        selectors=selectors,
        portfolio_routes=portfolio_routes,
    )
    portfolio_accounts = {
        str(route.get("required_account", "")).strip()
        for route in portfolio_account_routes
        if str(route.get("required_account", "")).strip()
    }
    mutation_requires_exact_account = bool(
        normalized_intent in WRITE_INTENTS
        and selectors.portfolio_supplied
        and selectors.portfolio_canonical
        and selectors.workspace_canonical
        and selectors.network_canonical
        and selectors.principal_canonical
        and not selectors.account_supplied
        and len(portfolio_accounts) > 1
    )
    if mutation_requires_exact_account:
        matching = list(portfolio_account_routes)
    required_probe_ids = {
        str(route.get("access_probe_id", "")).strip()
        for route in matching
        if str(route.get("access_probe_id", "")).strip()
    }
    probes = load_probe_index(required_probe_ids)
    required_status_ids: set[str] = set()
    for route in matching:
        route_status_id = route.get("readiness_status_id")
        if isinstance(route_status_id, str) and route_status_id:
            required_status_ids.add(route_status_id)
        operations = route.get("operations")
        if isinstance(operations, dict):
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                operation_status_id = operation.get("readiness_status_id")
                if isinstance(operation_status_id, str) and operation_status_id:
                    required_status_ids.add(operation_status_id)
    status_by_id = load_status_index(required_status_ids)
    if mutation_requires_exact_account:
        candidate_ids = tuple(
            sorted(str(route.get("route_id", "")) for route in matching)
        )
        applied_selectors = tuple(
            name
            for name, supplied in (
                ("portfolio", selectors.portfolio),
                ("workspace", selectors.workspace),
                ("network", selectors.network),
                ("principal", selectors.principal),
            )
            if supplied
        )
        route_selection = RouteSelection(
            None,
            "blocked_ambiguous_exact_route_binding",
            "ambiguous",
            "explicit_portfolio_mutation",
            candidate_ids,
            candidate_ids,
            applied_selectors,
            (),
            ("account",),
        )
    else:
        route_selection = select_preferred_route(
            matching,
            selectors=selectors,
            portfolio_routes=portfolio_routes,
        )
    portfolio_read_routes = discover_portfolio_read_set(
        route_selection,
        routes=matching,
        selectors=selectors,
        required_operation=normalized_required_operation,
        intent=normalized_intent,
        ambiguity_resolution=str(
            route_registry.get("portfolio_routes", {}).get(
                "ambiguity_resolution", ""
            )
        ),
    )
    if portfolio_read_routes:
        route_selection = RouteSelection(
            None,
            None,
            "portfolio_read_set",
            "explicit_portfolio_read_set",
            route_selection.system_candidate_route_ids,
            tuple(
                str(route.get("route_id", ""))
                for route in portfolio_read_routes
            ),
            route_selection.applied_selectors,
            route_selection.overridden_selectors,
            (),
        )
    ambiguity_question = route_ambiguity_question(
        route_selection,
        routes=matching,
        portfolio_routes=portfolio_routes,
    )
    selected_route = route_selection.preferred
    route_selection_blocker = route_selection.blocker

    checked_lanes: list[dict[str, Any]] = []
    preferred: dict[str, Any] | None = None
    for route in matching:
        selection_binding = portfolio_routes.get(
            str(route.get("route_id", "")), {}
        )
        probe = probes.get(str(route.get("access_probe_id", "")).strip())
        probe_ttl_days = probe.default_ttl_ms / 86_400_000 if probe else None
        readiness = readiness_for_route(route, status_by_id, probe_ttl_days=probe_ttl_days)
        route_id = str(route.get("route_id", ""))
        checked_lane = {
                "route_id": route.get("route_id"),
                "system": route.get("system"),
                "required_principal": route.get("required_principal"),
                "credential_handle_ids": list(
                    credential_handles_by_route.get(route_id, ())
                ),
                "required_account": route.get("required_account"),
                "readiness_status_id": route.get("readiness_status_id"),
                "property_domain": route.get("property_domain"),
                "issue_label": route.get("issue_label"),
                "browser_profile_primary": route.get("browser_profile_primary"),
                "browser_profile_fallbacks": route.get("browser_profile_fallbacks", []),
                "native_lane_kind": route.get("native_lane_kind"),
                "capability_scope": route.get("capability_scope"),
                "operations": route.get("operations"),
                "provider_adapter": route.get("provider_adapter"),
                "default_contexts": route.get("default_contexts", []),
                "portfolio_ids": sorted(
                    selection_binding.get("portfolio_ids", frozenset())
                ),
                "workspace_ids": sorted(
                    selection_binding.get("workspace_ids", frozenset())
                ),
                "network_ids": sorted(
                    selection_binding.get("network_ids", frozenset())
                ),
                "probe_id": route.get("access_probe_id"),
                "probe_command": probe.command if probe else None,
                "probe_expected_signal": probe.expected_signal if probe else None,
                "probe_safe_lane": probe.safe_lane if probe else None,
                "probe_scope": probe.scope if probe else None,
                "probe_ttl_days": probe_ttl_days,
                "route_notes": route.get("notes"),
                "readiness": readiness,
            }
        checked_lanes.append(checked_lane)
        if route is selected_route:
            preferred = checked_lane
    user_requested_ui_state = bool(
        user_requested_ui_state or explicit_existing_session_handoff
    )
    normalized_candidate_lane = (
        "declared_native" if candidate_lane in {"declared_api", "declared_native"} else candidate_lane
    )
    normalized_native_operation_support = (
        native_operation_support
        if native_operation_support in {"unknown", "supported", "unsupported"}
        else "unknown"
    )
    operation_contract = operation_route_contract(
        preferred,
        intent=normalized_intent,
        required_operation=required_operation,
        native_operation_support=normalized_native_operation_support,
        authenticated_ui_required=authenticated_ui_required,
        native_lane_kinds={
            normalize_slug(value)
            for value in field_list(routing_defaults.get("native_lane_kinds"))
        },
    )
    identity_contract = route_identity_contract(
        preferred,
        browser_account=browser_account,
    )
    normalized_requested_principal = selectors.principal
    normalized_requested_account = selectors.account
    principal_evidence_canonical = bool(
        normalized_requested_principal and selectors.principal_canonical
    )
    account_evidence_canonical = bool(
        normalized_requested_account and selectors.account_canonical
    )
    required_principal = normalize_slug(
        str(preferred.get("required_principal", "")) if preferred else ""
    )
    required_account = (
        str(preferred.get("required_account", "")).strip()
        if preferred
        else ""
    )
    principal_evidence_provided = bool(normalized_requested_principal)
    account_evidence_provided = bool(normalized_requested_account)
    principal_matches = bool(
        principal_evidence_provided
        and principal_evidence_canonical
        and required_principal
        and normalized_requested_principal == required_principal
    )
    selected_identity_normalizer = route_identity_normalizer(selected_route)
    account_matches = bool(
        account_evidence_provided
        and account_evidence_canonical
        and required_account
        and normalize_route_identity(
            normalized_requested_account,
            selected_identity_normalizer,
        )
        == normalize_route_identity(
            required_account,
            selected_identity_normalizer,
        )
    )
    overridden_selectors = set(route_selection.overridden_selectors)
    principal_input_consistent = bool(
        not principal_evidence_provided
        or principal_matches
        or "principal" in overridden_selectors
    )
    account_input_consistent = bool(
        not account_evidence_provided or account_matches
    )
    route_binding_complete = bool(required_principal and required_account)
    identity_evidence_complete = bool(
        route_binding_complete and principal_matches and account_matches
    )
    selection_inputs_consistent = bool(
        route_binding_complete
        and principal_input_consistent
        and account_input_consistent
    )
    route_readiness_applies = bool(
        operation_contract["operation_effect"] == "read"
        and normalized_candidate_lane == "declared_native"
    )
    route_readiness = (
        preferred.get("readiness", {}) if isinstance(preferred, dict) else {}
    )
    route_readiness_status_exact = bool(
        isinstance(preferred, dict)
        and isinstance(preferred.get("readiness_status_id"), str)
        and preferred["readiness_status_id"]
        and route_readiness.get("capability_id")
        == preferred["readiness_status_id"]
    )
    route_probe_scope_exact = bool(
        isinstance(preferred, dict)
        and preferred.get("probe_scope") == preferred.get("route_id")
    )
    probe_execution: dict[str, Any] = {
        "requested": run_exact_probe,
        "attempted": False,
        "supported": bool(exact_probe_argv(preferred)),
        "result": (
            "not_requested"
            if not run_exact_probe
            else "blocked_request_binding_unverified"
        ),
    }
    authoritative_probe_evidence: AuthoritativeProbeEvidence | None = None
    if (
        run_exact_probe
        and normalized_candidate_lane == "declared_native"
        and identity_contract["valid"]
        and operation_contract["operation_registered"]
        and operation_contract["intent_known"]
        and not operation_contract["intent_effect_mismatch"]
        and route_binding_complete
        and principal_input_consistent
        and account_input_consistent
    ):
        authoritative_probe_evidence, probe_execution = run_exact_registered_probe(
            preferred,
            probe_transport or subprocess_probe_transport,
        )
    authoritative_probe = authoritative_probe_evidence_contract(
        preferred,
        authoritative_probe_evidence,
    )
    route_operation_evidence_suitable = bool(
        isinstance(operation_contract["required_operation"], str)
        and operation_contract["required_operation"]
        in route_readiness.get("evidence_operations", [])
    )
    route_evidence_suitable = bool(
        "read" in route_readiness.get("evidence_effects", [])
        and route_operation_evidence_suitable
    )
    route_status_readiness_passed = bool(
        route_readiness_applies
        and route_readiness_status_exact
        and route_probe_scope_exact
        and route_readiness.get("state") == "ready"
        and route_readiness.get("slo_breached") is False
        and route_evidence_suitable
    )
    live_probe_operation_evidence_suitable = bool(
        isinstance(operation_contract["required_operation"], str)
        and operation_contract["required_operation"]
        in authoritative_probe.get("evidence_operations", [])
    )
    live_probe_read_evidence_suitable = bool(
        authoritative_probe["valid"]
        and "read" in authoritative_probe.get("evidence_effects", [])
        and live_probe_operation_evidence_suitable
    )
    route_readiness_passed = bool(
        route_readiness_applies
        and route_readiness_status_exact
        and route_probe_scope_exact
        and route_readiness.get("state") in {"ready", "degraded"}
        and (route_status_readiness_passed or live_probe_read_evidence_suitable)
    )
    route_readiness_unproven = bool(
        route_readiness_applies and not route_readiness_passed
    )
    mutation_readiness_applies = bool(
        operation_contract["operation_effect"] == "mutation"
    )
    mutation_readiness = readiness_for_status_id(
        operation_contract["mutation_readiness_status_id"],
        status_by_id,
        probe_ttl_days=(
            preferred.get("probe_ttl_days")
            if isinstance(preferred, dict)
            else None
        ),
    )
    mutation_operation_evidence_suitable = bool(
        isinstance(operation_contract["required_operation"], str)
        and operation_contract["required_operation"]
        in mutation_readiness.get("evidence_operations", [])
    )
    mutation_evidence_suitable = bool(
        "mutation" in mutation_readiness.get("evidence_effects", [])
        and mutation_operation_evidence_suitable
    )
    mutation_readiness_status_exact = bool(
        isinstance(operation_contract["mutation_readiness_status_id"], str)
        and operation_contract["mutation_readiness_status_id"]
        and mutation_readiness.get("capability_id")
        == operation_contract["mutation_readiness_status_id"]
    )
    mutation_readiness_passed = bool(
        mutation_readiness_applies
        and mutation_readiness_status_exact
        and route_probe_scope_exact
        and mutation_readiness.get("state") == "ready"
        and mutation_readiness.get("slo_breached") is False
        and mutation_evidence_suitable
    )
    browser_profile_primary, browser_profile_fallbacks = browser_profile_contract(
        preferred, routing_defaults
    )
    browser_profiles = browser_profile_selection(
        browser_profile_primary,
        browser_profile_fallbacks,
        explicit_existing_session_handoff=explicit_existing_session_handoff,
    )
    user_profile_fallback = next(
        (
            fallback
            for fallback in browser_profile_fallbacks
            if fallback["profile_id"] == "user"
        ),
        None,
    )
    automatic_user_profile_fallback_allowed = bool(
        user_profile_fallback and user_profile_fallback["automatic"]
    )
    user_profile_fallback_eligible = bool(
        user_profile_fallback
        and not user_profile_fallback["automatic"]
        and user_profile_fallback["requires_explicit_existing_session_handoff"]
        and explicit_existing_session_handoff
    )
    matched_browser_fallback_predicates: list[str] = []
    if operation_contract["native_unsupported"]:
        matched_browser_fallback_predicates.append("native_operation_unsupported")
    if api_probe_state == "failed":
        matched_browser_fallback_predicates.append("native_probe_failed")
    if (
        operation_contract["native_route_unavailable"]
        or api_probe_state == "unavailable"
    ):
        matched_browser_fallback_predicates.append("native_route_unavailable")
    if operation_contract["authenticated_ui_required"]:
        matched_browser_fallback_predicates.append("authenticated_ui_required")
    if user_requested_ui_state:
        matched_browser_fallback_predicates.append("user_requested_ui_state")
    browser_fallback_eligible = bool(
        preferred
        and identity_contract["valid"]
        and operation_contract["operation_registered"]
        and operation_contract["intent_known"]
        and not operation_contract["intent_effect_mismatch"]
        and matched_browser_fallback_predicates
    )
    browser_evidence_collection_allowed = bool(
        normalized_candidate_lane == "browser"
        and operation_contract["authenticated_ui_required"]
        and operation_contract["native_route_unavailable"]
        and browser_fallback_eligible
        and identity_contract["browser_account_matches_required"]
        and identity_evidence_complete
        and (
            identity_contract["required_account"] != "profile=user"
            or explicit_existing_session_handoff
        )
    )

    execution_allowed = False
    execution_blocker: str | None = None
    execution_decision = "use_declared_native_route"
    if route_selection_blocker:
        execution_blocker = route_selection_blocker
        execution_decision = "require_one_exact_route_selector"
    elif not identity_contract["valid"]:
        execution_blocker = "blocked_route_identity_contract_invalid"
        execution_decision = "restore_route_identity_contract"
    elif not operation_contract["intent_known"]:
        execution_blocker = "blocked_unknown_intent"
        execution_decision = "require_read_or_mutation_intent"
    elif operation_contract["unknown_operation"]:
        execution_blocker = (
            "blocked_unknown_mutation_operation"
            if operation_contract["unknown_mutation"]
            else "blocked_unknown_operation"
        )
        execution_decision = "require_registered_operation"
    elif operation_contract["intent_effect_mismatch"]:
        execution_blocker = "blocked_operation_effect_intent_mismatch"
        execution_decision = "match_intent_to_registered_operation_effect"
    elif not route_binding_complete:
        execution_blocker = "blocked_route_identity_binding_incomplete"
        execution_decision = "register_exact_principal_and_account_binding"
    elif not principal_input_consistent:
        execution_blocker = "blocked_requested_principal_route_mismatch"
        execution_decision = "select_exact_principal_route"
    elif not account_input_consistent:
        execution_blocker = "blocked_requested_account_route_mismatch"
        execution_decision = "select_exact_account_route"
    elif normalized_candidate_lane == "declared_native":
        if operation_contract["native_route_unavailable"]:
            execution_blocker = "blocked_no_declared_native_route"
            execution_decision = "use_persistent_managed_browser"
        elif operation_contract["authenticated_ui_required"]:
            execution_blocker = "blocked_required_operation_requires_authenticated_ui"
            execution_decision = "use_persistent_managed_browser"
        elif operation_contract["native_unsupported"]:
            execution_blocker = "blocked_required_operation_unsupported_by_native_route"
            execution_decision = "use_persistent_managed_browser"
        else:
            execution_allowed = True
    elif normalized_candidate_lane == "browser":
        execution_blocker = "blocked_browser_runtime_evidence_required"
        execution_decision = "require_runtime_verified_browser_route_and_account"

    default_fallback_order = [
        "declared_native_route",
        "declared_probe_or_readiness_check",
        "persistent_managed_browser",
        "stock_extension_existing_session_browser",
        "host_os_ui_last_resort",
    ]
    fallback_order = routing_defaults.get("fallback_order")
    if not isinstance(fallback_order, list) or not all(
        isinstance(value, str) and value for value in fallback_order
    ):
        fallback_order = default_fallback_order
    payload = {
        "schema": "openclaw.resolve_capability.v1",
        "system_requested": system,
        "systems_considered": systems,
        "intent_requested": intent,
        "intent": normalized_intent,
        "context": context,
        "portfolio": requested_portfolio,
        "workspace": requested_workspace,
        "network": requested_network,
        "required_operation": operation_contract["required_operation"],
        "route_selection_blocker": route_selection_blocker,
        "route_selection": {
            "state": route_selection.state,
            "mode": route_selection.mode,
            "system_candidate_route_ids": list(
                route_selection.system_candidate_route_ids
            ),
            "matched_route_ids": list(route_selection.matched_route_ids),
            "applied_selectors": list(route_selection.applied_selectors),
            "overridden_selectors": list(route_selection.overridden_selectors),
            "missing_discriminators": list(
                route_selection.missing_discriminators
            ),
            "ambiguity_question": ambiguity_question,
            "selection_only": True,
            "portfolio_source": "trusted_project_or_session",
        },
        "preferred_lane": preferred,
        "portfolio_read_set": {
            "applies": False,
            "bounded_by_registry": False,
            "portfolio": None,
            "operation": None,
            "account_bindings": [],
        },
        "portfolio_authoritative_read_sources": {
            "applies": bool(authoritative_read_sources),
            "bounded_by_registry": bool(authoritative_read_sources),
            "portfolio": (
                selectors.portfolio if authoritative_read_sources else None
            ),
            "sources": list(authoritative_read_sources),
        },
        "checked_lanes": checked_lanes,
        "routing_defaults": routing_defaults,
        "operation_contract": operation_contract,
        "identity_contract": identity_contract,
        "route_readiness": {
            "applies": route_readiness_applies,
            "execution_prerequisite": False,
            "diagnostic_only": route_readiness_applies,
            "readiness_status_id": route_readiness.get("readiness_status_id"),
            "state": route_readiness.get("state", "unknown"),
            "exact_status_row": route_readiness_status_exact,
            "exact_probe_id": preferred.get("probe_id") if preferred else None,
            "probe_scope": preferred.get("probe_scope") if preferred else None,
            "probe_scope_exact": route_probe_scope_exact,
            "authoritative_passed": (
                route_readiness_passed if route_readiness_applies else None
            ),
            "status_record_authoritative_passed": (
                route_status_readiness_passed
                if route_readiness_applies
                else None
            ),
            "live_probe_authoritative_passed": (
                live_probe_read_evidence_suitable
                if route_readiness_applies
                else None
            ),
            "evidence_effects": route_readiness.get("evidence_effects", []),
            "evidence_operations": route_readiness.get("evidence_operations", []),
            "operation_evidence_suitable": (
                route_operation_evidence_suitable
                if route_readiness_applies
                else None
            ),
            "evidence_suitable": (
                route_evidence_suitable if route_readiness_applies else None
            ),
            "last_verified_utc": route_readiness.get("last_verified_utc"),
            "age_days": route_readiness.get("age_days"),
            "effective_slo_max_age_days": route_readiness.get(
                "effective_slo_max_age_days"
            ),
            "slo_breached": route_readiness.get("slo_breached"),
        },
        "mutation_readiness": {
            "applies": mutation_readiness_applies,
            "execution_prerequisite": False,
            "completion_evidence_only": mutation_readiness_applies,
            "current_request_completion_authority": False,
            "readiness_status_id": mutation_readiness.get(
                "readiness_status_id"
            ),
            "state": mutation_readiness.get("state", "unknown"),
            "exact_probe_id": preferred.get("probe_id") if preferred else None,
            "probe_scope": preferred.get("probe_scope") if preferred else None,
            "probe_scope_exact": route_probe_scope_exact,
            "exact_status_row": mutation_readiness_status_exact,
            "caller_probe_state_authoritative": False,
            "authoritative_passed": (
                mutation_readiness_passed
                if mutation_readiness_applies
                else None
            ),
            "evidence_effects": mutation_readiness.get("evidence_effects", []),
            "evidence_operations": mutation_readiness.get(
                "evidence_operations", []
            ),
            "operation_evidence_suitable": (
                mutation_operation_evidence_suitable
                if mutation_readiness_applies
                else None
            ),
            "evidence_suitable": (
                mutation_evidence_suitable if mutation_readiness_applies else None
            ),
            "last_verified_utc": mutation_readiness.get("last_verified_utc"),
            "age_days": mutation_readiness.get("age_days"),
            "effective_slo_max_age_days": mutation_readiness.get(
                "effective_slo_max_age_days"
            ),
            "slo_breached": mutation_readiness.get("slo_breached"),
        },
        "fallback_order": fallback_order,
        "constraints": [
            "Prefer the declared API, connector, integration, or supported CLI route and use readiness/probe state as diagnostics, not task authority.",
            "Failure reports must name each checked lane and pass/fail/blocker status.",
            "A ready personal/default route must not satisfy a named workspace/account route.",
            "Use persistent profile=openclaw as the autonomous default; after a registered browser-fallback predicate, stock extension profile=chrome is the automatic existing signed-in tab route.",
            "Navigate managed UI adaptively from observed state; route bindings must not prescribe ephemeral refs or deterministic click chains.",
            "CAPTCHA, passkey/biometric/security-key, one-time 2FA/MFA, password reset/change/recovery, and unexpected privilege are human-only gates.",
        ],
        "failure_report_template": "Checked <system> native route <route_id>: <pass|failed|blocked>; checked managed profile=openclaw: <pass|failed|blocked|not_attempted_reason>; checked stock extension profile=chrome when an existing signed-in tab was required: <pass|failed|not_attempted_reason>.",
        "execution_guard": {
            "candidate_lane": normalized_candidate_lane,
            "selected_route_id": preferred.get("route_id") if preferred else None,
            "allowed": execution_allowed,
            "decision": execution_decision,
            "blocker_code": execution_blocker,
        },
        "route_binding": {
            "required_principal": required_principal or None,
            "required_account": required_account or None,
            "requested_principal": normalized_requested_principal or None,
            "requested_account": normalized_requested_account or None,
            "caller_inputs_authoritative": False,
            "principal_evidence_provided": principal_evidence_provided,
            "account_evidence_provided": account_evidence_provided,
            "principal_evidence_canonical": principal_evidence_canonical,
            "account_evidence_canonical": account_evidence_canonical,
            "principal_matches": principal_matches,
            "account_matches": account_matches,
            "principal_input_consistent": principal_input_consistent,
            "account_input_consistent": account_input_consistent,
            "route_binding_complete": route_binding_complete,
            "identity_evidence_complete": identity_evidence_complete,
            "selection_inputs_consistent": selection_inputs_consistent,
            "binding_complete": identity_evidence_complete,
        },
        "authoritative_probe_evidence": authoritative_probe,
        "exact_probe_execution": probe_execution,
        "browser_fallback_gate": {
            "required_predicate": "native_operation_unsupported_or_preferred_api_failed_or_unavailable_or_authenticated_ui_required_or_user_requested_ui_state",
            "registered_predicates": routing_defaults.get(
                "browser_fallback_predicates", []
            ),
            "matched_predicates": matched_browser_fallback_predicates,
            "api_probe_state": api_probe_state,
            "native_probe_state": api_probe_state,
            "native_operation_support": normalized_native_operation_support,
            "user_requested_ui_state": user_requested_ui_state,
            "required_operation_is_ui_only": operation_contract[
                "authenticated_ui_required"
            ],
            "authenticated_ui_required": operation_contract[
                "authenticated_ui_required"
            ],
            "eligible": browser_fallback_eligible,
            "allowed": browser_evidence_collection_allowed,
            "decision": (
                "allow_browser_evidence_collection"
                if browser_evidence_collection_allowed
                else (
                    "require_runtime_verified_browser_route_and_account"
                    if browser_fallback_eligible
                    else "stay_on_declared_native_route"
                )
            ),
            "scope": (
                "evidence_collection_only"
                if browser_evidence_collection_allowed
                else None
            ),
            "provider_operation_allowed": False,
            "account_identity_verified": False,
            "completion_claim_allowed": False,
            "caller_route_inputs_authoritative": False,
            "preferred_browser_profile": (
                browser_profile_primary["profile_id"]
                if browser_profile_primary
                else None
            ),
            "preferred_browser_transport": (
                browser_profile_primary["transport"]
                if browser_profile_primary
                else None
            ),
            "persistent_profile": bool(
                isinstance(routing_defaults.get("managed_browser_primary"), dict)
                and routing_defaults["managed_browser_primary"].get("persistent")
            ),
            "browser_profile_fallbacks": browser_profile_fallbacks,
            "browser_profile_order": browser_profiles["order"],
            "automatic_browser_profile_fallbacks": browser_profiles[
                "automatic_fallbacks"
            ],
            "attended_browser_profile_fallbacks": browser_profiles[
                "attended_fallbacks"
            ],
            "eligible_attended_browser_profile_fallbacks": browser_profiles[
                "eligible_attended_fallbacks"
            ],
            "automatic_user_profile_fallback_allowed": (
                automatic_user_profile_fallback_allowed
            ),
            "explicit_existing_session_handoff": explicit_existing_session_handoff,
            "user_profile_fallback_eligible": user_profile_fallback_eligible,
            "user_browser_attach_required": False,
            "adaptive_navigation_required": bool(
                routing_defaults.get("adaptive_navigation_required", True)
            ),
            "browser_secret_entry": routing_defaults.get(
                "browser_secret_entry", "opaque_secret_broker_only"
            ),
            "agent_handled_auth_steps": routing_defaults.get(
                "agent_handled_auth_steps", []
            ),
            "human_only_auth_gates": routing_defaults.get(
                "human_only_auth_gates", []
            ),
        },
    }
    provider_adapter = preferred.get("provider_adapter") if preferred else None
    if isinstance(provider_adapter, dict) and identity_contract["valid"]:
        resolved_route_evidence = dict(route_evidence or {})
        adapter_request_evidence = provider_adapter.get("request_evidence", {})
        if isinstance(adapter_request_evidence, dict):
            if "property_domain" in adapter_request_evidence:
                resolved_route_evidence.setdefault("property_domain", property_domain)
            if "issue_label" in adapter_request_evidence:
                resolved_route_evidence.setdefault("issue_label", issue_label)
        provider_guard = build_declarative_route_guard(
            preferred=preferred,
            adapter=provider_adapter,
            candidate_lane=(
                "browser" if normalized_candidate_lane == "browser" else "declared_api"
            ),
            api_probe_state=api_probe_state,
            api_probe_account=api_probe_account,
            target_url=target_url,
            browser_account=browser_account,
            request_evidence=resolved_route_evidence,
            user_requested_ui_state=user_requested_ui_state,
            explicit_existing_session_handoff=explicit_existing_session_handoff,
            operation_contract=operation_contract,
            routing_defaults=routing_defaults,
        )
        if normalized_candidate_lane == "declared_native":
            authoritative_probe_valid = bool(authoritative_probe["valid"])
            provider_guard.update(
                {
                    "api_probe_account_verified": authoritative_probe_valid,
                    "api_route_verified": authoritative_probe_valid,
                    "route_verified": authoritative_probe_valid,
                    "selected_lane_verified": authoritative_probe_valid,
                    "live_probe_required": not authoritative_probe_valid,
                    "authoritative_probe_evidence": authoritative_probe,
                }
            )
        payload["route_guard"] = provider_guard
        provider_browser_eligible = bool(
            provider_guard.get("browser_fallback_eligible", False)
            or operation_contract["authenticated_ui_required"]
            or operation_contract["native_unsupported"]
            or provider_guard["api_route_exhausted_for_browser_fallback"]
            or user_requested_ui_state
        )
        payload["browser_fallback_gate"].update(
            {
                "eligible": provider_browser_eligible,
                "allowed": browser_evidence_collection_allowed,
                "decision": (
                    "allow_browser_evidence_collection"
                    if browser_evidence_collection_allowed
                    else (
                        "require_runtime_verified_browser_route_and_account"
                        if provider_browser_eligible
                        else "stay_on_declared_native_route"
                    )
                ),
                "caller_declared_api_state_is_authoritative": provider_guard[
                    "api_probe_claims_are_authoritative"
                ],
                "probe_receipts_are_audit_only": provider_guard[
                    "api_probe_receipts_are_audit_only"
                ],
                "preferred_browser_profile": (
                    provider_guard["browser_profile_primary"]["profile_id"]
                    if provider_guard["browser_profile_primary"]
                    else None
                ),
                "browser_profile_fallbacks": provider_guard[
                    "browser_profile_fallbacks"
                ],
                "browser_profile_order": provider_guard[
                    "browser_profile_order"
                ],
                "automatic_browser_profile_fallbacks": provider_guard[
                    "automatic_browser_profile_fallbacks"
                ],
                "attended_browser_profile_fallbacks": provider_guard[
                    "attended_browser_profile_fallbacks"
                ],
                "eligible_attended_browser_profile_fallbacks": provider_guard[
                    "eligible_attended_browser_profile_fallbacks"
                ],
                "automatic_user_profile_fallback_allowed": provider_guard[
                    "automatic_user_profile_fallback_allowed"
                ],
                "explicit_existing_session_handoff": provider_guard[
                    "explicit_existing_session_handoff"
                ],
                "user_profile_fallback_eligible": provider_guard[
                    "user_profile_fallback_eligible"
                ],
            }
        )
        if "matched_browser_fallback_predicates" in provider_guard:
            payload["browser_fallback_gate"]["matched_predicates"] = provider_guard[
                "matched_browser_fallback_predicates"
            ]
        if payload["execution_guard"]["allowed"] and not provider_guard["allowed"]:
            payload["execution_guard"].update(
                {
                    "allowed": False,
                    "decision": provider_guard["decision"],
                    "blocker_code": provider_guard["blocker_code"],
                }
            )
        adapter_constraints = provider_adapter.get("constraints", [])
        if isinstance(adapter_constraints, list):
            payload["constraints"].extend(
                str(constraint)
                for constraint in adapter_constraints
                if isinstance(constraint, str) and constraint.strip()
            )
    if (
        not operation_contract["intent_known"]
        or operation_contract["unknown_operation"]
        or operation_contract["intent_effect_mismatch"]
    ):
        payload["browser_fallback_gate"].update(
            {
                "allowed": False,
                "decision": "stay_on_declared_native_route",
            }
        )
        route_guard = payload.get("route_guard")
        if isinstance(route_guard, dict) and route_guard.get("allowed") is True:
            route_guard.update(
                {
                    "allowed": False,
                    "decision": execution_decision,
                    "blocker_code": execution_blocker,
                }
            )
    elif route_readiness_unproven:
        payload["browser_fallback_gate"].update(
            {
                "allowed": False,
                "decision": "stay_on_declared_native_route",
            }
        )
    route_guard = payload.get("route_guard")
    if (
        isinstance(route_guard, dict)
        and route_guard.get("allowed") is True
        and payload["execution_guard"]["allowed"] is False
    ):
        route_guard.update(
            {
                "allowed": False,
                "decision": payload["execution_guard"]["decision"],
                "blocker_code": payload["execution_guard"]["blocker_code"],
            }
        )
    if portfolio_read_routes:
        selected_route_ids = {
            str(route.get("route_id", "")) for route in portfolio_read_routes
        }
        selected_lanes = sorted(
            (
                lane
                for lane in checked_lanes
                if str(lane.get("route_id", "")) in selected_route_ids
            ),
            key=lambda lane: str(lane.get("route_id", "")),
        )
        native_read_set_allowed = bool(
            normalized_candidate_lane == "declared_native"
            and normalized_native_operation_support != "unsupported"
            and not authenticated_ui_required
            and all(
                normalize_slug(str(lane.get("native_lane_kind", "")))
                in {
                    normalize_slug(value)
                    for value in field_list(
                        routing_defaults.get("native_lane_kinds")
                    )
                }
                for lane in selected_lanes
            )
        )
        account_bindings = [
            {
                "route_id": lane["route_id"],
                "required_principal": lane["required_principal"],
                "required_account": lane["required_account"],
                "workspace_ids": lane["workspace_ids"],
            }
            for lane in selected_lanes
        ]
        payload["portfolio_read_set"] = {
            "applies": True,
            "bounded_by_registry": True,
            "portfolio": selectors.portfolio,
            "operation": normalized_required_operation,
            "account_bindings": account_bindings,
        }
        payload["operation_contract"] = operation_route_contract(
            selected_lanes[0],
            intent=normalized_intent,
            required_operation=normalized_required_operation,
            native_operation_support=normalized_native_operation_support,
            authenticated_ui_required=authenticated_ui_required,
            native_lane_kinds={
                normalize_slug(value)
                for value in field_list(routing_defaults.get("native_lane_kinds"))
            },
        )
        payload["operation_contract"]["selection_scope"] = "portfolio_read_set"
        payload["route_readiness"].update(
            {
                "applies": normalized_candidate_lane == "declared_native",
                "diagnostic_only": True,
                "routes": [
                    {
                        "route_id": lane["route_id"],
                        "state": lane["readiness"].get("state", "unknown"),
                        "last_verified_utc": lane["readiness"].get(
                            "last_verified_utc"
                        ),
                    }
                    for lane in selected_lanes
                ],
            }
        )
        payload["execution_guard"].update(
            {
                "selected_route_ids": [
                    binding["route_id"] for binding in account_bindings
                ],
                "allowed": native_read_set_allowed,
                "decision": (
                    "use_declared_native_portfolio_read_routes"
                    if native_read_set_allowed
                    else "require_supported_declared_native_portfolio_read_routes"
                ),
                "blocker_code": (
                    None
                    if native_read_set_allowed
                    else "blocked_portfolio_read_set_not_native"
                ),
            }
        )
        registry_bindings_complete = all(
            bool(binding["required_principal"] and binding["required_account"])
            for binding in account_bindings
        )
        payload["route_binding"].update(
            {
                "required_accounts": [
                    binding["required_account"] for binding in account_bindings
                ],
                "principal_input_consistent": True,
                "account_input_consistent": True,
                "route_binding_complete": registry_bindings_complete,
                "selection_inputs_consistent": True,
                "binding_complete": registry_bindings_complete,
                "binding_source": "registry_portfolio_read_set",
            }
        )
        payload["constraints"].extend(
            [
                "A portfolio read set is bounded to the exact registered account bindings returned here; do not substitute a default account.",
                "A send or mutation across a multi-account portfolio requires one exact account and never inherits this read-set selection.",
            ]
        )
    return payload


def compact_output(payload: dict[str, Any]) -> dict[str, Any]:
    """Present a resolved decision without duplicating the diagnostic catalogue.

    Keep authority, execution/fallback guards and actual probe results intact.
    Registry selection is not execution authority, and a probe never establishes
    completion of the requested operation. The full payload remains the default.
    """

    operation = payload["required_operation"]

    def requested_operations(operations: dict[str, Any]) -> dict[str, Any]:
        return (
            {operation: operations[operation]}
            if operation in operations
            else {}
        )

    def lane_summary(lane: dict[str, Any]) -> dict[str, Any]:
        summary = {
            key: value
            for key, value in lane.items()
            if key not in {
                "readiness", "operations", "default_contexts",
                "probe_expected_signal", "probe_safe_lane", "probe_ttl_days",
            }
        }
        summary["operations"] = requested_operations(lane.get("operations") or {})
        readiness = lane.get("readiness") or {}
        summary["readiness"] = {
            key: readiness[key]
            for key in (
                "state", "last_verified_utc", "slo_breached",
                "constraint_or_fallback", "evidence_effects", "evidence_operations",
            )
            if key in readiness
        }
        return summary

    output = {
        key: value
        for key, value in payload.items()
        if key not in {
            "checked_lanes", "routing_defaults", "failure_report_template",
        }
    }
    output["output_detail"] = "compact"
    preferred = payload["preferred_lane"]
    output["preferred_lane"] = lane_summary(preferred) if preferred else None
    read_set = payload["portfolio_read_set"]
    if read_set["applies"]:
        selected_ids = {binding["route_id"] for binding in read_set["account_bindings"]}
        output["portfolio_read_set"] = {
            **read_set,
            "lanes": [
                lane_summary(lane)
                for lane in payload["checked_lanes"]
                if lane["route_id"] in selected_ids
            ],
        }
    contract = payload["operation_contract"]
    output["operation_contract"] = {
        key: value
        for key, value in contract.items()
        if key not in {
            "operations", "native_supported_operations",
            "authenticated_ui_required_operations", "operation_effects",
        }
    }
    output["operation_contract"]["operations"] = requested_operations(
        contract.get("operations") or {}
    )
    return output


def main(
    argv: list[str] | None = None,
    *,
    probe_transport: ProbeTransport | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Resolve external-system capability route from structured registry")
    parser.add_argument("--system", required=True, help="External system, e.g. x, gmail, notion, drive, discord, github")
    parser.add_argument("--intent", required=True, help="Intent, e.g. read_search, read, write, send")
    parser.add_argument(
        "--context",
        default="",
        help="Deprecated explicit route tag retained for compatible callers; prompt prose is never parsed",
    )
    parser.add_argument(
        "--portfolio",
        default="",
        help="Canonical selection-only portfolio from trusted project/session metadata; never prompt prose and grants no account authority",
    )
    parser.add_argument(
        "--workspace",
        default="",
        help="Canonical trusted workspace or tenant selector; grants no account authority",
    )
    parser.add_argument(
        "--network",
        default="",
        help="Canonical network selector such as hedera-testnet or hedera-mainnet",
    )
    parser.add_argument(
        "--candidate-lane",
        choices=("declared_native", "declared_api", "browser"),
        default="declared_native",
        help="Lane proposed for execution; declared_api is a backward-compatible alias for declared_native",
    )
    parser.add_argument(
        "--api-probe-state",
        choices=("not_run", "passed", "failed", "unavailable"),
        default="not_run",
        help="Caller-reported probe state; a routing hint only until bound live receipt evidence exists",
    )
    parser.add_argument(
        "--api-probe-account",
        default="",
        help="Caller-reported probe account used only to fail closed on mismatch; never identity verification",
    )
    parser.add_argument(
        "--target-url",
        default="",
        help="Optional target URL used only for route-safety classification; sensitive path/query is not echoed",
    )
    parser.add_argument(
        "--browser-account",
        default="",
        help="Expected browser account declaration; this permits evidence collection but never verifies completion",
    )
    parser.add_argument(
        "--required-operation",
        default="",
        help="Exact provider operation required, e.g. page-indexing-example-urls",
    )
    parser.add_argument(
        "--native-operation-support",
        choices=("unknown", "supported", "unsupported"),
        default="unknown",
        help="Current capability check for the required operation on the declared native route",
    )
    parser.add_argument(
        "--authenticated-ui-required",
        action="store_true",
        help="Declare that the required evidence or state exists only in the authenticated provider UI",
    )
    parser.add_argument(
        "--route-evidence",
        action="append",
        default=[],
        type=parse_route_evidence_argument,
        metavar="NAME=VALUE",
        help="Non-secret current-request evidence consumed only when the selected route declares that key; repeat for multiple values",
    )
    parser.add_argument(
        "--property-domain",
        default="",
        help="Optional current-request domain evidence consumed only when the selected route declares that adapter input",
    )
    parser.add_argument(
        "--issue-label",
        default="",
        help="Optional current-request issue evidence consumed only when the selected route declares that adapter input",
    )
    parser.add_argument(
        "--principal",
        default="",
        help="Optional exact principal binding; mismatches fail closed",
    )
    parser.add_argument(
        "--account",
        default="",
        help="Optional exact account, workspace, site, or profile binding; mismatches fail closed",
    )
    parser.add_argument(
        "--existing-session-handoff",
        action="store_true",
        help="Exact operator handoff of an already-open profile=user session; never inferred from prose",
    )
    parser.add_argument(
        "--user-requested-ui-state",
        action="store_true",
        help="Exact request for UI-only state; never inferred from context prose",
    )
    parser.add_argument(
        "--run-exact-probe",
        action="store_true",
        help=(
            "Run the selected registry-bound read-only probe with exact argv and "
            "provider signal validation; no caller receipt is accepted or persisted"
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON (default; kept for explicit callers)")
    parser.add_argument(
        "--compact",
        action="store_true",
        help=(
            "Emit selected routes and the requested operation without the full "
            "candidate/status catalogue; omit for full diagnostics. Does not "
            "change resolution, probes, authority, or exit status"
        ),
    )
    args = parser.parse_args(argv)

    route_evidence: dict[str, str] = {}
    for key, value in args.route_evidence:
        if key in route_evidence and route_evidence[key] != value:
            parser.error(f"conflicting --route-evidence values for {key}")
        route_evidence[key] = value
    for compatibility_key, compatibility_value in (
        ("property_domain", args.property_domain),
        ("issue_label", args.issue_label),
    ):
        if (
            compatibility_value
            and compatibility_key in route_evidence
            and route_evidence[compatibility_key] != compatibility_value
        ):
            parser.error(
                f"conflicting --route-evidence and compatibility flag for {compatibility_key}"
            )

    payload = resolve(
        args.system,
        args.intent,
        args.context,
        candidate_lane=args.candidate_lane,
        api_probe_state=args.api_probe_state,
        api_probe_account=args.api_probe_account,
        target_url=args.target_url,
        browser_account=args.browser_account,
        required_operation=args.required_operation,
        property_domain=args.property_domain,
        issue_label=args.issue_label,
        route_evidence=route_evidence,
        native_operation_support=args.native_operation_support,
        authenticated_ui_required=args.authenticated_ui_required,
        requested_principal=args.principal,
        requested_account=args.account,
        requested_portfolio=args.portfolio,
        requested_workspace=args.workspace,
        requested_network=args.network,
        explicit_existing_session_handoff=args.existing_session_handoff,
        user_requested_ui_state=args.user_requested_ui_state,
        run_exact_probe=args.run_exact_probe,
        probe_transport=probe_transport,
    )
    json.dump(
        compact_output(payload) if args.compact else payload,
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    execution_guard = payload.get("execution_guard")
    browser_fallback_gate = payload.get("browser_fallback_gate")
    browser_evidence_collection_allowed = bool(
        isinstance(browser_fallback_gate, dict)
        and browser_fallback_gate.get("allowed") is True
        and browser_fallback_gate.get("scope") == "evidence_collection_only"
        and browser_fallback_gate.get("provider_operation_allowed") is False
    )
    portfolio_read_set_allowed = bool(
        isinstance(payload.get("portfolio_read_set"), dict)
        and payload["portfolio_read_set"].get("applies") is True
        and isinstance(execution_guard, dict)
        and execution_guard.get("allowed") is True
    )
    if portfolio_read_set_allowed:
        return 0
    if not payload["preferred_lane"] and not browser_evidence_collection_allowed:
        return 2
    if browser_evidence_collection_allowed:
        return 0
    if isinstance(execution_guard, dict) and execution_guard.get("allowed") is False:
        return 3
    route_guard = payload.get("route_guard")
    if isinstance(route_guard, dict) and route_guard.get("allowed") is False:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
