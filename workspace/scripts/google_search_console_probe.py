#!/usr/bin/env python3
"""Secret-safe, read-only readiness probe for personal Google Search Console."""

from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

try:
    from scripts.capability_registry_contract import (
        RegistryContractError,
        assert_integration_registry_contract,
        load_json_strict,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from capability_registry_contract import (  # type: ignore[no-redef]
        RegistryContractError,
        assert_integration_registry_contract,
        load_json_strict,
    )

ROOT = Path(__file__).resolve().parents[1]
TRUSTED_ENV_FILE = Path(
    _operator_binding('paths.gog_env_file')
)
TRUSTED_GOG_BIN = Path(_operator_binding('paths.gog_binary'))
TRUSTED_RECEIPT_DIR = ROOT / "artifacts" / "CapabilityReceipts"
TRUSTED_ROUTE_REGISTRY = ROOT / "registry" / "integration_routes.json"
TRUSTED_HOME = _operator_binding('paths.host_home')
CONFIGURED_ROUTE_ID = "google-search-console-personal-gog"
GOG_KEYRING_PASSWORD_KEY = "GOG_KEYRING_PASSWORD"
SAFE_CHILD_ENV_KEYS = {
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
}
ACCEPTED_PERMISSION_LEVELS = {"siteOwner", "siteFullUser"}


class ProbeBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class SearchConsoleRouteBinding(NamedTuple):
    account: str


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def load_confined_gog_keyring_env(path: Path) -> dict[str, str]:
    """Load only the canonical owner-private gog keyring environment."""

    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ProbeBlocked("credential_env_missing", "owner-private gog environment is missing") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ProbeBlocked("credential_env_symlink", "owner-private gog environment must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise ProbeBlocked("credential_env_not_regular", "owner-private gog environment is not a regular file")
    if metadata.st_uid != os.getuid():
        raise ProbeBlocked("credential_env_wrong_owner", "owner-private gog environment owner does not match the probe user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ProbeBlocked("credential_env_wrong_mode", "owner-private gog environment must have mode 0600")
    if metadata.st_nlink != 1:
        raise ProbeBlocked("credential_env_multiple_links", "owner-private gog environment must have exactly one link")
    if metadata.st_size > 4096:
        raise ProbeBlocked("credential_env_too_large", "owner-private gog environment exceeds the bounded size")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProbeBlocked("credential_env_unavailable", "owner-private gog environment could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or opened.st_size > 4096
        ):
            raise ProbeBlocked("credential_env_changed", "owner-private gog environment changed while opening")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            text = handle.read(4097)
    except (OSError, UnicodeError) as exc:
        raise ProbeBlocked("credential_env_unreadable", "owner-private gog environment could not be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(text.encode("utf-8")) > 4096:
        raise ProbeBlocked("credential_env_too_large", "owner-private gog environment exceeds the bounded size")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ProbeBlocked(
                "credential_env_invalid",
                f"owner-private gog environment has an invalid assignment at line {line_number}",
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key != GOG_KEYRING_PASSWORD_KEY:
            continue
        if key in values:
            raise ProbeBlocked(
                "credential_env_duplicate_key",
                f"owner-private gog environment repeats key {key!r}",
            )
        value = parse_env_value(raw_value)
        if not value or "\x00" in value:
            raise ProbeBlocked(
                "credential_env_invalid_value",
                f"owner-private gog environment has an invalid value for {key!r}",
            )
        values[key] = value

    if GOG_KEYRING_PASSWORD_KEY not in values:
        raise ProbeBlocked(
            "credential_env_unlock_missing",
            "owner-private gog environment lacks the keyring unlock value",
        )
    return values


def load_confined_env(path: Path) -> dict[str, str]:
    return load_confined_gog_keyring_env(path)


def validate_confined_gog_binary(path: Path) -> Path:
    """Accept only the fixed, owner-controlled, non-writable gog executable."""

    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ProbeBlocked("gog_binary_unavailable", "the gog executable is unavailable") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ProbeBlocked("gog_binary_invalid", "the gog executable must be a regular non-symlink file")
    if metadata.st_uid != os.getuid():
        raise ProbeBlocked("gog_binary_wrong_owner", "the gog executable owner does not match the probe user")
    if metadata.st_nlink != 1:
        raise ProbeBlocked("gog_binary_multiple_links", "the gog executable must have exactly one link")
    if mode & 0o022 or not mode & stat.S_IXUSR:
        raise ProbeBlocked("gog_binary_unsafe_mode", "the gog executable has an unsafe mode")
    return path


def resolve_gog_binary() -> Path:
    """Return only the operator-installed gog binary; callers cannot replace it."""
    return validate_confined_gog_binary(TRUSTED_GOG_BIN)


def iter_site_entries(value: Any):
    if isinstance(value, dict):
        if isinstance(value.get("siteUrl"), str):
            yield value
        for child in value.values():
            yield from iter_site_entries(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_site_entries(child)


def parse_route_binding(route: Any) -> SearchConsoleRouteBinding:
    if not isinstance(route, dict):
        raise ProbeBlocked(
            "route_config_invalid",
            "configured Search Console route is not an object",
        )
    account = str(route.get("required_account", "")).strip().lower()
    if not account:
        raise ProbeBlocked(
            "route_account_missing",
            "configured Search Console route lacks a required account",
        )

    return SearchConsoleRouteBinding(account=account)


def load_configured_route_binding() -> SearchConsoleRouteBinding:
    try:
        payload = load_json_strict(
            TRUSTED_ROUTE_REGISTRY,
            source="registry/integration_routes.json",
        )
        assert_integration_registry_contract(
            payload,
            source="registry/integration_routes.json",
        )
    except (OSError, RegistryContractError) as exc:
        raise ProbeBlocked(
            "route_registry_unavailable",
            "configured Search Console route registry could not be read",
        ) from exc
    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(routes, list):
        raise ProbeBlocked(
            "route_registry_invalid",
            "configured Search Console route registry lacks a routes list",
        )
    matches = [
        route
        for route in routes
        if isinstance(route, dict) and route.get("route_id") == CONFIGURED_ROUTE_ID
    ]
    if len(matches) != 1:
        raise ProbeBlocked(
            "route_binding_not_unique",
            "configured Search Console route must appear exactly once",
        )
    return parse_route_binding(matches[0])


def resolve_requested_binding(
    args: argparse.Namespace,
    configured: SearchConsoleRouteBinding,
) -> SearchConsoleRouteBinding:
    requested_account = args.account.strip().lower()
    if requested_account != configured.account:
        raise ProbeBlocked(
            "requested_account_not_configured",
            "requested account does not match the configured Search Console route",
        )

    return configured


def normalize_requested_domain(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    domain = value.strip().lower().rstrip(".")
    if any(character in domain for character in "/:@") or "." not in domain:
        raise ProbeBlocked(
            "requested_property_domain_invalid",
            "requested Search Console site domain is invalid",
        )
    return domain


def classify_site_property(site_url: str) -> tuple[str, str] | None:
    value = site_url.strip()
    if value.lower().startswith("sc-domain:"):
        domain = value[len("sc-domain:") :].strip().lower().rstrip(".")
        return ("domain", domain) if domain else None
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    ):
        return "url-prefix", parsed.hostname.lower().rstrip(".")
    return None


def find_authorized_site_properties(
    payload: Any,
    property_domain: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matching_by_uri: dict[str, dict[str, Any]] = {}
    for entry in iter_site_entries(payload):
        site_url = str(entry.get("siteUrl", "")).strip()
        classified = classify_site_property(site_url)
        if classified is None:
            continue
        property_type, domain = classified
        if property_domain is not None and domain != property_domain:
            continue
        candidate = {
            "siteUrl": site_url,
            "propertyType": property_type,
            "permissionLevel": str(entry.get("permissionLevel", "")).strip(),
        }
        existing = matching_by_uri.get(site_url)
        if existing is None or (
            existing["permissionLevel"] not in ACCEPTED_PERMISSION_LEVELS
            and candidate["permissionLevel"] in ACCEPTED_PERMISSION_LEVELS
        ):
            matching_by_uri[site_url] = candidate
    matching = list(matching_by_uri.values())
    authorized = [
        entry
        for entry in matching
        if entry["permissionLevel"] in ACCEPTED_PERMISSION_LEVELS
    ]
    return matching, authorized


def classify_gog_failure(returncode: int, stderr: str, stdout: str) -> str:
    combined = f"{stderr}\n{stdout}".lower()
    if "insufficientpermissions" in combined or "insufficient permissions" in combined:
        return "oauth_scope_missing"
    if "invalid_grant" in combined or "token" in combined and "revoked" in combined:
        return "oauth_token_invalid"
    if returncode == 124:
        return "gog_timeout"
    if "403" in combined:
        return "search_console_probe_forbidden"
    return "search_console_probe_failed"


def run_probe(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    result: dict[str, Any] = {
        "schema": "openclaw.google-search-console-probe.v1",
        "producer": "google_search_console_probe.py",
        "checked_at_utc": now_utc(),
        "account": args.account.strip().lower(),
        "credential_env": str(TRUSTED_ENV_FILE),
        "operation": "sites.list",
        "mutating": False,
        "evidence_role": "audit_only",
        "authoritative": False,
        "completion_claim_allowed": False,
        "page_indexing_example_table_verified": False,
        "status": "blocked",
        "reason_code": None,
        "property_accessible": False,
    }
    try:
        configured = load_configured_route_binding()
        binding = resolve_requested_binding(args, configured)
        requested_domain = normalize_requested_domain(
            getattr(args, "property_domain", None)
        )
        result.update(
            {
                "account": binding.account,
                "requested_property_domain": requested_domain,
            }
        )
        confined = load_confined_env(TRUSTED_ENV_FILE)
        gog_binary = resolve_gog_binary()
        child_env = {
            key: value
            for key in SAFE_CHILD_ENV_KEYS
            if (value := os.environ.get(key)) is not None
        }
        child_env.update(
            {
                "HOME": TRUSTED_HOME,
                "GOG_ACCOUNT": binding.account,
                "GOG_KEYRING_PASSWORD": confined["GOG_KEYRING_PASSWORD"],
            }
        )
        try:
            completed = subprocess.run(
                [
                    str(gog_binary),
                    "searchconsole",
                    "sites",
                    "list",
                    "--account",
                    binding.account,
                    "--json",
                    "--no-input",
                    "--wrap-untrusted",
                ],
                cwd=ROOT,
                env=child_env,
                text=True,
                capture_output=True,
                timeout=args.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProbeBlocked("gog_timeout", "Search Console sites probe timed out") from exc
        except OSError as exc:
            raise ProbeBlocked(
                "gog_execution_failed",
                "Search Console gog process could not be started",
            ) from exc
        result["gog_exit_code"] = completed.returncode
        if completed.returncode != 0:
            reason_code = classify_gog_failure(
                completed.returncode,
                completed.stderr,
                completed.stdout,
            )
            raise ProbeBlocked(reason_code, "Search Console sites probe did not succeed")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ProbeBlocked("gog_invalid_json", "Search Console sites probe returned invalid JSON") from exc
        matching_properties, authorized_properties = find_authorized_site_properties(
            payload,
            requested_domain,
        )
        result["matching_property_count"] = len(matching_properties)
        result["authorized_matching_property_count"] = len(authorized_properties)
        if not matching_properties:
            raise ProbeBlocked(
                "property_not_accessible",
                (
                    "no Search Console property matched the requested site"
                    if requested_domain
                    else "the account has no recognized Search Console properties"
                ),
            )
        if not authorized_properties:
            raise ProbeBlocked(
                "property_permission_insufficient",
                "matching Search Console properties lack owner or full-user permission",
            )
        if requested_domain and len(authorized_properties) != 1:
            result["matching_property_types"] = sorted(
                {entry["propertyType"] for entry in authorized_properties}
            )
            raise ProbeBlocked(
                "property_ambiguous",
                "multiple authorized Search Console properties match the requested site",
            )
        result.update(
            {
                "status": "ready",
                "reason_code": None,
                "property_accessible": True,
                "property_selection_required": requested_domain is None,
                "authorized_property_types": sorted(
                    {entry["propertyType"] for entry in authorized_properties}
                ),
                "evidence": (
                    "exact-account Search Console sites.list returned authorized property access; "
                    "this audit signal does not prove task completion or Page Indexing example-table inspection"
                ),
            }
        )
        if requested_domain:
            property_entry = authorized_properties[0]
            result.update(
                {
                    "property_uri": property_entry["siteUrl"],
                    "property_type": property_entry["propertyType"],
                    "permission_level": property_entry["permissionLevel"],
                }
            )
        return 0, result
    except ProbeBlocked as exc:
        result.update({"status": "blocked", "reason_code": exc.code, "detail": exc.detail})
        return 2, result


def write_receipt(path_value: str, payload: dict[str, Any]) -> None:
    """Atomically persist an owner-private audit receipt with no authority role."""
    requested = Path(path_value).expanduser()
    lexical_path = requested if requested.is_absolute() else ROOT / requested
    if lexical_path.is_symlink():
        raise ProbeBlocked(
            "receipt_path_unsafe",
            "probe receipt path must not be a symlink",
        )
    receipt_root = TRUSTED_RECEIPT_DIR.resolve()
    path = lexical_path.resolve()
    if path == receipt_root or receipt_root not in path.parents:
        raise ProbeBlocked(
            "receipt_path_outside_trusted_root",
            "probe receipt path must remain under artifacts/CapabilityReceipts",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ProbeBlocked(
                "receipt_path_unsafe",
                "probe receipt path must be a regular non-symlink file",
            )
        if metadata.st_uid != os.getuid():
            raise ProbeBlocked(
                "receipt_path_wrong_owner",
                "probe receipt path owner does not match the probe user",
            )

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            os.fchmod(temporary.fileno(), 0o600)
            json.dump(payload, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument(
        "--property-domain",
        help="Requested site-domain assertion; property URI/type are discovered from sites.list",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--receipt-path",
        help=(
            "Atomically write the sanitized result as an owner-private mode-0600 "
            "audit receipt; it grants no authority or completion claim"
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON; retained for explicit callers")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exit_code, payload = run_probe(args)
    if args.receipt_path:
        try:
            write_receipt(args.receipt_path, payload)
        except ProbeBlocked as exc:
            payload = {
                **payload,
                "status": "blocked",
                "reason_code": exc.code,
                "detail": exc.detail,
            }
            exit_code = 2
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
