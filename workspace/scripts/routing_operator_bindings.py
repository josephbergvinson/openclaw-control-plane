"""Expand adopter-owned routing coordinates without probing any provider.

Missing optional integrations remain visibly unresolved while the registry is
being inspected. Only the selected route is required before a probe. Commands
are represented as argv arrays in the distributable registry, then rendered to
the original registry command string for its existing exact-argv validation.
"""
from __future__ import annotations

import re
import shlex
import hashlib
import os
from typing import Any

try:
    from scripts.operator_contract import ContractError, load_operator_contract
except ModuleNotFoundError:
    from operator_contract import ContractError, load_operator_contract

_REFERENCE = re.compile(r"\$\{operator:([A-Za-z0-9_.-]+)\}")
_UNCONFIGURED_DIGESTS: set[str] = set()
_CONTRACT = load_operator_contract()
_CONFIG_SELECTION = os.environ.get("OPENCLAW_OPERATOR_CONFIG")


def selected_config_path() -> str | None:
    """Preserve the validated selector in confined adapter child processes."""
    return _CONFIG_SELECTION


def require_probe_identity(probe_id: str) -> None:
    """Require provider identities that are checked inside a probe's parser.

    These coordinates do not all appear in the command argv or selected lane.
    Missing configuration must be detected before the provider is contacted.
    """
    required = {
        "trello-personal-api-probe": (
            "services.trello.username", "services.trello.board_name", "services.trello.board_id"),
        "cloudflare-operator-readonly-probe": (
            "services.cloudflare.account_id", "services.cloudflare.account_name",
            "services.cloudflare.zone_id", "services.cloudflare.zone_name",
            "services.cloudflare.project_id", "services.cloudflare.project_subdomain",
            "identifiers.github_username"),
        "jira-company-alpha-probe": ("services.jira.provider_identity_sha256",),
        "company-alpha-gigabrain-metabase-api-probe": (
            "services.company_alpha_analytics.provider_identity_sha256",),
        "x-api-operator-x-xurl-probe": ("services.x.user_id",),
        "openclaw-hedera-mainnet-test-signer-probe": ("identifiers.host_user",),
        "openclaw-hedera-testnet-test-signer-probe": ("identifiers.host_user",),
    }
    for key in required.get(probe_id, ()):
        require_resolved(binding(key))


def binding(key: str) -> str:
    contract = _CONTRACT
    marker = "${operator:" + key + "}"
    try:
        value = str(contract.require_path(key)) if key.startswith("paths.") else contract.require_string(key)
    except ContractError:
        return marker
    if not isinstance(value, str) or not value:
        return marker
    return value


def materialize(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$operator_prefix", "value"}:
            if value["$operator_prefix"] != "channel:":
                raise ContractError("unsupported operator argument prefix")
            rendered = materialize(value["value"])
            if not isinstance(rendered, str):
                raise ContractError("operator prefixed argument must be a string")
            return "channel:" + rendered
        if set(value) == {"$operator_env", "value"}:
            name = value["$operator_env"]
            if not isinstance(name, str) or re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) is None:
                raise ContractError("operator environment binding has an invalid name")
            rendered = materialize(value["value"])
            if not isinstance(rendered, str):
                raise ContractError("operator environment value must be a string")
            return name + "=" + rendered
        if set(value) == {"$operator_argv"}:
            argv = value["$operator_argv"]
            if not isinstance(argv, list):
                raise ContractError("operator command must contain only string arguments")
            rendered = [materialize(v) for v in argv]
            if not all(isinstance(item, str) for item in rendered):
                raise ContractError("operator command must contain only string arguments")
            return shlex.join(rendered)
        return {key: materialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [materialize(item) for item in value]
    if isinstance(value, str):
        match = _REFERENCE.fullmatch(value)
        if match:
            key = match.group(1)
            try:
                return _CONTRACT.render(value)
            except ContractError:
                if key.endswith("_sha256"):
                    digest = hashlib.sha256(("unconfigured-operator-binding:" + key).encode()).hexdigest()
                    _UNCONFIGURED_DIGESTS.add(digest)
                    return digest
                if key.startswith("paths."):
                    suffix = ".json" if key.endswith("_json") else ""
                    return "/__unconfigured_operator__/" + key.replace(".", "/") + suffix
                if key.startswith("services.source_urls.") or key.endswith((".origin", ".endpoint", ".site")):
                    return "https://unconfigured.invalid/" + key.rsplit(".", 1)[-1]
                if key == "services.linear.workspace_id":
                    return "unconfigured-operator-linear-workspace"
                return value
        if "${operator:" in value:
            raise ContractError("operator references must occupy the whole registry value")
    return value


def require_resolved(value: Any) -> None:
    """Reject a selected route with missing bindings before its provider call."""
    if isinstance(value, dict):
        for item in value.values():
            require_resolved(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            require_resolved(item)
    elif str(value) in _UNCONFIGURED_DIGESTS or any(marker in str(value) for marker in (
        "${operator:", "/__unconfigured_operator__/", "https://unconfigured.invalid/"
        , "unconfigured-operator-"
    )):
        raise ContractError("selected route has an unresolved operator binding")
