#!/usr/bin/env python3
"""Read-only Google baseline for one explicitly selected registered account.

Verify OAuth identity and two minimal Gmail/Drive reads through the existing
confined GOG credential transport. Credential-file presence is inventory only;
observed scopes and successful reads never prove send/write/admin acceptance.

Usage: python3 scripts/google_capability_probe.py --account <registered-email>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts import resolve_capability as resolver
except ModuleNotFoundError:  # Direct execution from scripts/.
    import resolve_capability as resolver


REQUIRED_SCOPES = {
    "gmail_modify": "https://www.googleapis.com/auth/gmail.modify",
    "gmail_send": "https://www.googleapis.com/auth/gmail.send",
    "drive": "https://www.googleapis.com/auth/drive",
    "docs": "https://www.googleapis.com/auth/documents",
    "sheets": "https://www.googleapis.com/auth/spreadsheets",
    "youtube_readonly": "https://www.googleapis.com/auth/youtube.readonly",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_argv(account: str, operation: str) -> tuple[str, ...]:
    prefix = (str(resolver.TRUSTED_GOG_BIN), "--account", account,
              "--json", "--no-input", "--gmail-no-send")
    if operation == "gmail-read":
        return prefix + ("gmail", "labels", "list")
    if operation == "drive-read":
        return prefix + ("drive", "ls", "--max", "1")
    raise ValueError("unsupported Google baseline read")


def failure_reason(result: resolver.ProbeCommandResult) -> str:
    """Classify native failures without retaining raw OAuth or provider output."""
    if result.outcome == "timeout" or result.returncode == 124:
        return "probe_timeout"
    if result.outcome != "completed":
        return "probe_transport_unverified"
    if result.returncode == 126:
        return "confined_gog_transport_unavailable"
    text = (result.stderr + "\n" + result.stdout).lower()
    if "invalid_grant" in text or "expired or revoked" in text:
        return "oauth_refresh_rejected"
    if "insufficient" in text and ("scope" in text or "permission" in text):
        return "oauth_scope_missing"
    if result.returncode == 4 and "no auth" in text:
        return "oauth_account_missing"
    if "403" in text or "permission_denied" in text:
        return "provider_access_denied"
    return "native_read_unverified"


def read_evidence(account: str, operation: str, *, transport=None) -> dict[str, Any]:
    """Project CLI completion and collection counts, never provider contents."""
    row: dict[str, Any] = {"account": account, "operation": operation,
                           "status": "unverified", "evidence_operations": []}
    try:
        result = (transport or resolver.subprocess_probe_transport)(
            read_argv(account, operation), resolver.EXACT_PROBE_TIMEOUT_SECONDS)
        if not isinstance(result, resolver.ProbeCommandResult):
            raise ValueError("invalid transport result")
        if not isinstance(result.returncode, int) or isinstance(result.returncode, bool):
            raise ValueError("invalid exit code")
        row["returncode"] = result.returncode
        row["outcome"] = result.outcome
        if result.outcome != "completed" or result.returncode != 0:
            row["reason"] = failure_reason(result)
            return row
        if len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) > resolver.MAX_PROBE_OUTPUT_BYTES:
            raise ValueError("oversized output")
        payload = resolver.loads_json_strict(result.stdout, source="Google baseline read")
        collection = "labels" if operation == "gmail-read" else "files"
        envelope_keys = {collection} | ({"nextPageToken"} if operation == "drive-read" else set())
        if not isinstance(payload, dict) or collection not in payload or not set(payload) <= envelope_keys:
            raise ValueError("unverified collection envelope")
        items = payload[collection]
        if not isinstance(items, list) or not all(
            isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
            for item in items
        ):
            raise ValueError("unverified collection items")
        if "nextPageToken" in payload and not isinstance(payload["nextPageToken"], str):
            raise ValueError("unverified page token")
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError,
            RecursionError, resolver.RegistryContractError):
        row["outcome"] = "response_unverified"
        return row
    row.update(status="ready", evidence_operations=[operation])
    row["collection_counts"] = {collection: len(items)}
    return row


def probe_account(account: str, *, transport=None) -> dict[str, Any]:
    """Run the same account-bound baseline for CLI and recurring health owners."""
    out: dict[str, Any] = {
        "checked_at_utc": now_utc(), "account": account,
        "files": {}, "gog": {}, "capabilities": {}, "overall": "degraded", "errors": [],
        "baseline_operations": ["oauth-identity-read", "gmail-read", "drive-read"],
        "evidence_operations": [], "diagnostic_only": True, "execution_prerequisite": False,
    }
    for name, filename in (("google_client_json", "google-client.json"),
                           ("google_token_json", "google-token.json")):
        path = Path(resolver.ROOT) / "secrets" / filename
        out["files"][name] = {"exists": path.exists(), "role": "inventory_only"}
    capabilities = out["capabilities"]
    capabilities["google_oauth_files_legacy"] = {"status": "unverified", "details": "File presence does not verify any account or permission"}
    capabilities["gmail_send"] = {"account": account, "status": "unverified", "details": "No send performed; OAuth identity, scopes and GETs do not prove send acceptance"}
    for key, scope in REQUIRED_SCOPES.items():
        capabilities[f"scope_{key}"] = {"account": account, "status": "unverified", "scope": scope}
    capabilities["google_auth_lane"] = {"account": account, "status": "unverified"}
    for operation in ("gmail-read", "drive-read"):
        capabilities[operation.replace("-", "_")] = {
            "account": account, "operation": operation, "status": "unverified", "evidence_operations": []}
    try:
        selected = resolver.resolve("google-workspace", "read", requested_account=account)
        preferred = selected.get("preferred_lane")
        if not preferred or selected.get("route_selection_blocker"):
            raise ValueError("account route not selected")
    except (SystemExit, OSError, ValueError, resolver.RegistryContractError):
        out["errors"].append("registered_account_route_unavailable")
    else:
        account = preferred["required_account"]
        out.update(account=account, route_id=preferred["route_id"], principal=preferred["required_principal"])
        for row in capabilities.values():
            if "account" in row:
                row["account"] = account
        captured: list[resolver.ProbeCommandResult] = []

        def capture(argv: tuple[str, ...], timeout: int) -> resolver.ProbeCommandResult:
            result = (transport or resolver.subprocess_probe_transport)(argv, timeout)
            captured.append(result)
            return result

        evidence, diagnostic = resolver.run_exact_registered_probe(preferred, capture)
        out["gog"]["identity_check"] = diagnostic
        if evidence is None:
            out["errors"].append("selected_account_identity_unverified")
            out["identity_reason"] = failure_reason(captured[0]) if captured else "identity_probe_unverified"
            if captured and captured[0].returncode == 0:
                try:
                    payload = resolver.loads_json_strict(captured[0].stdout, source="Google identity failure")
                    matches = [row for row in payload["accounts"] if isinstance(row, dict)
                               and str(row.get("email", "")).casefold() == account.casefold()]
                    if not matches:
                        out["identity_reason"] = "oauth_account_missing"
                    elif len(matches) == 1 and matches[0].get("valid") is False:
                        diagnostic = str(matches[0].get("error", ""))
                        out["identity_reason"] = failure_reason(resolver.ProbeCommandResult(1, "", diagnostic))
                except (ValueError, KeyError, TypeError, resolver.RegistryContractError):
                    pass
        else:
            capabilities["google_auth_lane"].update(status="ready", evidence_operations=["oauth-identity-read"])
            out["evidence_operations"].append("oauth-identity-read")
            payload = resolver.loads_json_strict(captured[0].stdout, source="verified Google identity")
            entry = next(row for row in payload["accounts"]
                         if isinstance(row.get("email"), str) and row["email"].casefold() == account.casefold())
            for key, scope in REQUIRED_SCOPES.items():
                capabilities[f"scope_{key}"]["status"] = "observed" if scope in entry["scopes"] else "not_observed"
            # These are grant checks for the route's mail/Drive/calendar
            # operations, not proof that a write or every Google API works.
            scopes = set(entry["scopes"])
            requirements = {
                "gmail": {"https://mail.google.com/", "https://www.googleapis.com/auth/gmail.modify"},
                "drive": {"https://www.googleapis.com/auth/drive"},
                "calendar": {"https://www.googleapis.com/auth/calendar"},
            }
            out["missing_registered_service_scopes"] = [
                service for service, compatible in requirements.items()
                if f"{service}-write" in preferred["operations"] and not scopes.intersection(compatible)
            ]
            for operation in ("gmail-read", "drive-read"):
                row = read_evidence(account, operation, transport=transport)
                capabilities[operation.replace("-", "_")] = row
                out["evidence_operations"].extend(row["evidence_operations"])
                if row["status"] != "ready":
                    out["errors"].append(f"{operation}_unverified")
            if not out["errors"]:
                out["overall"] = "ready"
    if out["errors"]:
        out["recovery"] = {
            "owner": "registered Google route and existing confined GOG credential loader",
            "next_step": "Inspect the selected account's native auth/operation failure and use its existing supported recovery route; do not substitute another account",
            "auth_changes_attempted": False,
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="Exact registered Google account; no default substitution")
    args = parser.parse_args()
    if not args.account.strip():
        parser.error("--account must name a registered Google account")
    out = probe_account(args.account)
    print(json.dumps(out, indent=2))
    return 0 if out["overall"] == "ready" else 2


if __name__ == "__main__":
    sys.exit(main())
