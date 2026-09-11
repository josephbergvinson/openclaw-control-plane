#!/usr/bin/env python3
"""Read-only proof for the exact personal Cloudflare account, zone, and Pages project.

Loads credentials from the process environment or ``secrets/cloudflare.env``.
The helper has no deployment or other mutation operation and never prints a
credential value. Preview deployments use provider-native Wrangler directly.
"""

from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = "https://api.cloudflare.com/client/v4"
ROUTE_ID = 'cloudflare-operator-readonly-api'
PROBE_SCHEMA = "openclaw.cloudflare-personal-read.v1"

EXPECTED_ACCOUNT_ID = _operator_binding('services.cloudflare.account_id')
EXPECTED_ACCOUNT_NAME = _operator_binding('services.cloudflare.account_name')
EXPECTED_ZONE_ID = _operator_binding('services.cloudflare.zone_id')
EXPECTED_ZONE_NAME = _operator_binding('services.cloudflare.zone_name')
EXPECTED_PROJECT_ID = _operator_binding('services.cloudflare.project_id')
EXPECTED_PROJECT_NAME = "personal-site"
EXPECTED_PROJECT_SUBDOMAIN = _operator_binding('services.cloudflare.project_subdomain')
EXPECTED_PROJECT_SOURCE_TYPE = "github"
EXPECTED_PROJECT_SOURCE_OWNER = _operator_binding('identifiers.github_username')
EXPECTED_PROJECT_SOURCE_REPOSITORY = "personal-site"
EXPECTED_PRODUCTION_BRANCH = "main"


class CapabilityBlocked(RuntimeError):
    """Value-free, stable failure classification for public probe output."""


def detect_workspace_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == ".worktrees":
            return parent.parent
    return here.parents[1]


WORKSPACE_ROOT = detect_workspace_root()
DEFAULT_ENV_PATH = WORKSPACE_ROOT / "secrets" / "cloudflare.env"


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(env_path: Path) -> dict[str, str]:
    if not env_path.is_absolute():
        env_path = WORKSPACE_ROOT / env_path
    file_values = parse_env_file(env_path)
    return {
        "token": os.environ.get("CLOUDFLARE_API_TOKEN")
        or file_values.get("CLOUDFLARE_API_TOKEN", ""),
        "zone_name": os.environ.get("CLOUDFLARE_ZONE_NAME")
        or file_values.get("CLOUDFLARE_ZONE_NAME", ""),
        "account_name": os.environ.get("CLOUDFLARE_ACCOUNT_NAME")
        or file_values.get("CLOUDFLARE_ACCOUNT_NAME", ""),
    }


def cf_get(
    token: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    query = f"?{urllib.parse.urlencode(params or {})}" if params else ""
    request = urllib.request.Request(
        f"{API_BASE}{path}{query}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
            return isinstance(payload, dict), payload
    except urllib.error.HTTPError as exc:
        return False, {"http_status": exc.code}
    except Exception:
        return False, {"transport_error": True}


def require_result(
    token: str,
    path: str,
    *,
    reason_code: str,
    params: dict[str, Any] | None = None,
) -> Any:
    ok, payload = cf_get(token, path, params)
    if not ok or payload.get("success") is not True or "result" not in payload:
        raise CapabilityBlocked(reason_code)
    return payload["result"]


def read_cloudflare_state(token: str) -> dict[str, Any]:
    verified = require_result(
        token,
        "/user/tokens/verify",
        reason_code="token_verify_failed",
    )
    if not isinstance(verified, dict) or verified.get("status") != "active":
        raise CapabilityBlocked("token_inactive")

    accounts = require_result(
        token,
        "/accounts",
        reason_code="account_read_failed",
        params={"per_page": 50},
    )
    exact_accounts = (
        [
            account
            for account in accounts
            if isinstance(account, dict)
            and account.get("id") == EXPECTED_ACCOUNT_ID
            and account.get("name") == EXPECTED_ACCOUNT_NAME
        ]
        if isinstance(accounts, list)
        else []
    )
    if len(exact_accounts) != 1:
        raise CapabilityBlocked("account_identity_mismatch")

    zones = require_result(
        token,
        "/zones",
        reason_code="zone_read_failed",
        params={"name": EXPECTED_ZONE_NAME, "per_page": 10},
    )
    exact_zones = (
        [
            zone
            for zone in zones
            if isinstance(zone, dict)
            and zone.get("id") == EXPECTED_ZONE_ID
            and zone.get("name") == EXPECTED_ZONE_NAME
            and zone.get("status") == "active"
            and zone.get("type") == "full"
            and isinstance(zone.get("account"), dict)
            and zone["account"].get("id") == EXPECTED_ACCOUNT_ID
        ]
        if isinstance(zones, list)
        else []
    )
    if len(exact_zones) != 1:
        raise CapabilityBlocked("zone_identity_mismatch")

    project = require_result(
        token,
        f"/accounts/{EXPECTED_ACCOUNT_ID}/pages/projects/{EXPECTED_PROJECT_NAME}",
        reason_code="pages_project_read_failed",
    )
    source = project.get("source") if isinstance(project, dict) else None
    source_config = source.get("config") if isinstance(source, dict) else None
    if (
        not isinstance(project, dict)
        or project.get("id") != EXPECTED_PROJECT_ID
        or project.get("name") != EXPECTED_PROJECT_NAME
        or project.get("subdomain") != EXPECTED_PROJECT_SUBDOMAIN
        or project.get("production_branch") != EXPECTED_PRODUCTION_BRANCH
        or not isinstance(source, dict)
        or source.get("type") != EXPECTED_PROJECT_SOURCE_TYPE
        or not isinstance(source_config, dict)
        or source_config.get("owner") != EXPECTED_PROJECT_SOURCE_OWNER
        or source_config.get("repo_name") != EXPECTED_PROJECT_SOURCE_REPOSITORY
        or source_config.get("production_branch") != EXPECTED_PRODUCTION_BRANCH
        or source_config.get("deployments_enabled") is not True
        or source_config.get("preview_deployment_setting") != "all"
    ):
        raise CapabilityBlocked("pages_project_identity_mismatch")

    return {
        "identity": {
            "account_id": EXPECTED_ACCOUNT_ID,
            "account_name": EXPECTED_ACCOUNT_NAME,
        },
        "zone": {
            "id": EXPECTED_ZONE_ID,
            "name": EXPECTED_ZONE_NAME,
            "status": "active",
            "type": "full",
            "account_id": EXPECTED_ACCOUNT_ID,
        },
        "project": {
            "id": EXPECTED_PROJECT_ID,
            "name": EXPECTED_PROJECT_NAME,
            "subdomain": EXPECTED_PROJECT_SUBDOMAIN,
            "production_branch": EXPECTED_PRODUCTION_BRANCH,
            "source": {
                "type": EXPECTED_PROJECT_SOURCE_TYPE,
                "owner": EXPECTED_PROJECT_SOURCE_OWNER,
                "repository": EXPECTED_PROJECT_SOURCE_REPOSITORY,
            },
        },
    }


def public_payload(
    *,
    status: str,
    reason_code: str | None,
    state: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema": PROBE_SCHEMA,
        "route_id": ROUTE_ID,
        "status": status,
        "reason_code": reason_code,
        "identity": state.get("identity") if state else None,
        "zone": state.get("zone") if state else None,
        "project": state.get("project") if state else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe the exact personal Cloudflare account, zone, and Pages project"
    )
    parser.add_argument(
        "--env-path",
        default=str(DEFAULT_ENV_PATH),
        help="credential env path (default: canonical Workspace owner)",
    )
    args = parser.parse_args()

    cfg = load_config(Path(args.env_path))
    reason: str | None = None
    if not cfg["token"]:
        reason = "credential_missing"
    elif cfg["zone_name"] and cfg["zone_name"] != EXPECTED_ZONE_NAME:
        reason = "credential_zone_mismatch"
    elif cfg["account_name"] and cfg["account_name"] != EXPECTED_ACCOUNT_NAME:
        reason = "credential_account_mismatch"
    if reason is not None:
        print(
            json.dumps(
                public_payload(status="blocked", reason_code=reason, state=None),
                indent=2,
            )
        )
        return 2

    try:
        state = read_cloudflare_state(cfg["token"])
    except CapabilityBlocked as exc:
        print(
            json.dumps(
                public_payload(status="blocked", reason_code=str(exc), state=None),
                indent=2,
            )
        )
        return 2

    print(
        json.dumps(
            public_payload(status="ready", reason_code=None, state=state),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
