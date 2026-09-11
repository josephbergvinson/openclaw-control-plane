#!/usr/bin/env python3
'Read-only exact-identity and saved-question access for CompanyAlpha Gigabrain.'

from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import argparse
import hashlib
import hmac
import json
import os
import pwd
import re
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Optional, Tuple

try:
    from scripts.capability_registry_contract import (
        RegistryContractError,
        assert_integration_registry_contract,
        load_json_strict,
        loads_json_strict,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from capability_registry_contract import (  # type: ignore[no-redef]
        RegistryContractError,
        assert_integration_registry_contract,
        load_json_strict,
        loads_json_strict,
    )

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "registry" / "integration_routes.json"
ROUTE_ID = 'company-alpha-gigabrain-metabase-api'
SYSTEM_ID = 'company-alpha-gigabrain'
CREDENTIAL_HANDLE_ID = 'metabase.company_alpha.api'
SCHEMA = 'openclaw.company-alpha-gigabrain-metabase-probe.v1'
READ_SCHEMA = 'openclaw.company-alpha-gigabrain-metabase-read.v1'
CANONICAL_USERNAME = _operator_binding("identifiers.host_user")
CANONICAL_HOME = Path(_operator_binding('paths.host_home'))
DEFAULT_ENV_FILE = Path(
    _operator_binding('paths.routing_company_alpha_gigabrain_metabase_env')
)
EXPECTED_ORIGIN = _operator_binding('services.company_alpha_analytics.origin')
USER_AGENT = 'OpenClaw-CompanyAlpha-Gigabrain/1.0'
MAX_ENV_FILE_BYTES = 16 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 1024 * 1024
MAX_METADATA_RESULTS = 50
MAX_QUERY_ROWS = 100
REQUIRED_ENV_KEYS = {
    'COMPANY_ALPHA_GIGABRAIN_METABASE_URL',
    'COMPANY_ALPHA_GIGABRAIN_METABASE_USER',
    'COMPANY_ALPHA_GIGABRAIN_METABASE_PASSWORD',
}
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class ProbeFailure(RuntimeError):
    """Expected, redaction-safe readiness failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


Requester = Callable[
    [str, str, str, Optional[Mapping[str, Any]], Optional[str]],
    Tuple[bool, Any],
]


class AuthenticatedSession(NamedTuple):
    origin: str
    session_token: str
    provider_identity_sha256: str
    private_values: tuple[str, ...]


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the session credential remains origin-bound."""

    def redirect_request(
        self,
        _request: urllib.request.Request,
        _file_pointer: Any,
        _code: int,
        _message: str,
        _headers: Mapping[str, str],
        _new_url: str,
    ) -> None:
        return None


NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirectHandler())


def canonical_owner_uid() -> int:
    try:
        owner = pwd.getpwnam(CANONICAL_USERNAME)
    except KeyError as exc:
        raise ProbeFailure("canonical_owner_missing") from exc
    if Path(owner.pw_dir) != CANONICAL_HOME:
        raise ProbeFailure("canonical_home_mismatch")
    return owner.pw_uid


def read_owner_private_text(
    path: Path,
    *,
    expected_path: Path,
    expected_uid: int,
) -> str:
    """Read one exact regular file through a no-follow descriptor."""

    if path != expected_path or not path.is_absolute():
        raise ProbeFailure("credential_path_mismatch")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as exc:
        raise ProbeFailure("credential_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProbeFailure("credential_not_regular")
        if before.st_uid != expected_uid:
            raise ProbeFailure("credential_owner_mismatch")
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise ProbeFailure("credential_mode_mismatch")
        if before.st_nlink != 1:
            raise ProbeFailure("credential_link_count_mismatch")
        if before.st_size > MAX_ENV_FILE_BYTES:
            raise ProbeFailure("credential_file_too_large")
        raw = os.read(descriptor, MAX_ENV_FILE_BYTES + 1)
        if len(raw) > MAX_ENV_FILE_BYTES:
            raise ProbeFailure("credential_file_too_large")
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_uid",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) != after.st_size
            or any(
                getattr(before, field) != getattr(after, field)
                for field in stable_fields
            )
        ):
            raise ProbeFailure("credential_changed_during_read")
    finally:
        os.close(descriptor)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeFailure("credential_not_utf8") from exc


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match:
            raise ProbeFailure("credential_env_malformed")
        key, value = match.groups()
        if key in values:
            raise ProbeFailure("credential_env_duplicate_key")
        value = value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ProbeFailure("credential_env_malformed")
            value = value[1:-1]
        values[key] = value
    if set(values) != REQUIRED_ENV_KEYS or not all(values.values()):
        raise ProbeFailure("credential_env_invalid_key_set")
    return values


def load_registered_route_contract() -> tuple[str, Path]:
    try:
        payload = load_json_strict(
            REGISTRY_PATH,
            source="registry/integration_routes.json",
        )
        assert_integration_registry_contract(
            payload,
            source="registry/integration_routes.json",
        )
    except (OSError, RegistryContractError) as exc:
        raise ProbeFailure("registry_unreadable") from exc
    matches = [
        route
        for route in payload.get("routes", [])
        if isinstance(route, dict) and route.get("route_id") == ROUTE_ID
    ]
    if len(matches) != 1:
        raise ProbeFailure("registry_route_not_unique")
    route = matches[0]
    expected_operations = {
        "status-read": {"lane": "declared_native", "effect": "read"},
        "metadata-read": {"lane": "declared_native", "effect": "read"},
        "query-readonly": {"lane": "declared_native", "effect": "read"},
    }
    if (
        route.get("system") != SYSTEM_ID
        or route.get("required_principal") != 'company-alpha'
        or route.get("required_account") != 'company-alpha-gigabrain'
        or route.get("operations") != expected_operations
    ):
        raise ProbeFailure("registry_route_binding_mismatch")
    try:
        expected_digest = route["provider_adapter"]["native_evidence"][
            "provider_account_id_sha256"
        ]
    except (KeyError, TypeError) as exc:
        raise ProbeFailure("registry_identity_binding_missing") from exc
    if not isinstance(expected_digest, str) or re.fullmatch(
        r"[0-9a-f]{64}", expected_digest
    ) is None:
        raise ProbeFailure("registry_identity_binding_invalid")

    credential_matches = []
    for handle in payload.get("credential_handles", []):
        if not isinstance(handle, dict):
            continue
        consumers = handle.get("consumers")
        if isinstance(consumers, list) and {
            "kind": "integration-route",
            "consumer_id": ROUTE_ID,
        } in consumers:
            credential_matches.append(handle)
    if len(credential_matches) != 1:
        raise ProbeFailure("registry_credential_handle_not_unique")
    handle = credential_matches[0]
    owner = handle.get("owner")
    if (
        handle.get("handle_id") != CREDENTIAL_HANDLE_ID
        or handle.get("credential_kind") != "api-credential-bundle"
        or handle.get("provisioning_state") != "provisioned"
        or handle.get("browser_binding") is not None
        or not isinstance(owner, dict)
        or owner.get("kind") != "runtime-secret-file"
        or owner.get("ref") != str(DEFAULT_ENV_FILE)
        or set(owner.get("keys", [])) != REQUIRED_ENV_KEYS
    ):
        raise ProbeFailure("registry_credential_binding_mismatch")
    return expected_digest, DEFAULT_ENV_FILE


def load_credentials(path: Path) -> dict[str, str]:
    return parse_env_text(
        read_owner_private_text(
            path,
            expected_path=DEFAULT_ENV_FILE,
            expected_uid=canonical_owner_uid(),
        )
    )


def canonical_email(value: Any) -> str | None:
    if not isinstance(value, str) or value != value.strip():
        return None
    normalized = value.casefold()
    if (
        not normalized
        or "\x00" in normalized
        or EMAIL_PATTERN.fullmatch(normalized) is None
    ):
        return None
    return normalized


def request_json(
    origin: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None,
    session_token: str | None,
) -> tuple[bool, Any]:
    if origin != EXPECTED_ORIGIN:
        return False, {"error": "origin_mismatch"}
    allowed = (method, path) in {
        ("POST", "/api/session"),
        ("GET", "/api/user/current"),
        ("GET", "/api/card"),
    } or (
        method == "POST"
        and re.fullmatch(r"/api/card/[1-9][0-9]*/query", path) is not None
    )
    if not allowed:
        return False, {"error": "request_contract_mismatch"}
    if (method == "POST") != (body is not None):
        return False, {"error": "request_contract_mismatch"}
    if path == "/api/session":
        if session_token is not None or not isinstance(body, Mapping):
            return False, {"error": "request_contract_mismatch"}
        session_body = dict(body)
        if set(session_body) != {"username", "password"} or any(
            not isinstance(session_body[field], str) or not session_body[field]
            for field in ("username", "password")
        ):
            return False, {"error": "request_contract_mismatch"}
    elif not isinstance(session_token, str) or not session_token.strip():
        return False, {"error": "request_contract_mismatch"}
    elif method == "POST" and dict(body or {}) != {"parameters": []}:
        return False, {"error": "request_contract_mismatch"}
    data = json.dumps(dict(body)).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if session_token is not None:
        headers["X-Metabase-Session"] = session_token
    request = urllib.request.Request(
        origin + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with NO_REDIRECT_OPENER.open(request, timeout=30) as response:
            raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return False, {"error": "http_error", "status": exc.code}
    except (OSError, TimeoutError, urllib.error.URLError):
        return False, {"error": "network_error"}
    if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        return False, {"error": "response_too_large"}
    try:
        payload = loads_json_strict(
            raw.decode("utf-8"),
            source="Metabase provider response",
        )
    except (UnicodeDecodeError, RegistryContractError):
        return False, {"error": "invalid_json"}
    if not isinstance(payload, (dict, list)):
        return False, {"error": "unexpected_payload_type"}
    return True, payload


def base_result(*, schema: str, operation: str | None) -> dict[str, Any]:
    result = {
        "schema": schema,
        "route": ROUTE_ID,
        "system": SYSTEM_ID,
        "credential_handle_id": CREDENTIAL_HANDLE_ID,
        "endpoint_origin_expected": EXPECTED_ORIGIN,
        "credential_contract_bound": False,
        "endpoint_matches_expected": False,
        "auth_ok": False,
        "account_identity_matches_registered": False,
        "credential_secrets_redacted": True,
        "external_mutation": False,
        "ok": False,
    }
    if operation is not None:
        result["operation"] = operation
    return result


def fail_closed_result(
    result: Mapping[str, Any],
    *,
    schema: str,
    operation: str | None,
    error: str,
) -> dict[str, Any]:
    safe = base_result(schema=schema, operation=operation)
    for field in (
        "credential_contract_bound",
        "endpoint_matches_expected",
        "auth_ok",
        "account_identity_matches_registered",
    ):
        safe[field] = result.get(field) is True
    safe["error"] = error
    return safe


def open_exact_session(
    result: dict[str, Any],
    *,
    route_loader: Callable[[], tuple[str, Path]] = load_registered_route_contract,
    credential_loader: Callable[[Path], dict[str, str]] = load_credentials,
    requester: Requester = request_json,
) -> AuthenticatedSession:
    expected_digest, credential_path = route_loader()
    credentials = credential_loader(credential_path)
    result["credential_contract_bound"] = True

    origin = credentials['COMPANY_ALPHA_GIGABRAIN_METABASE_URL'].rstrip("/")
    result["endpoint_matches_expected"] = origin == EXPECTED_ORIGIN
    if origin != EXPECTED_ORIGIN:
        raise ProbeFailure("credential_origin_mismatch")

    configured_identity = canonical_email(
        credentials['COMPANY_ALPHA_GIGABRAIN_METABASE_USER']
    )
    if configured_identity is None:
        raise ProbeFailure("credential_identity_invalid")
    session_ok, session = requester(
        origin,
        "POST",
        "/api/session",
        {
            "username": credentials['COMPANY_ALPHA_GIGABRAIN_METABASE_USER'],
            "password": credentials['COMPANY_ALPHA_GIGABRAIN_METABASE_PASSWORD'],
        },
        None,
    )
    if not session_ok or not isinstance(session, dict):
        raise ProbeFailure("provider_auth_failed")
    session_token = session.get("id")
    if not isinstance(session_token, str) or not session_token.strip():
        raise ProbeFailure("provider_session_missing")
    result["auth_ok"] = True

    current_ok, current_user = requester(
        origin,
        "GET",
        "/api/user/current",
        None,
        session_token,
    )
    if not current_ok or not isinstance(current_user, dict):
        raise ProbeFailure("provider_identity_read_failed")
    provider_identity = canonical_email(current_user.get("email"))
    if provider_identity is None:
        raise ProbeFailure("provider_identity_missing")

    configured_matches_provider = hmac.compare_digest(
        configured_identity,
        provider_identity,
    )
    provider_digest = hashlib.sha256(provider_identity.encode("utf-8")).hexdigest()
    registered_matches_provider = hmac.compare_digest(
        provider_digest,
        expected_digest,
    )
    result["account_identity_matches_registered"] = bool(
        configured_matches_provider and registered_matches_provider
    )
    if not result["account_identity_matches_registered"]:
        raise ProbeFailure("provider_account_mismatch")

    return AuthenticatedSession(
        origin=origin,
        session_token=session_token,
        provider_identity_sha256=provider_digest,
        private_values=(
            credentials['COMPANY_ALPHA_GIGABRAIN_METABASE_USER'],
            credentials['COMPANY_ALPHA_GIGABRAIN_METABASE_PASSWORD'],
            provider_identity,
            session_token,
        ),
    )


def ensure_credential_secrets_redacted(
    result: Mapping[str, Any],
    session: AuthenticatedSession,
) -> None:
    pending: list[Any] = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if any(private and private in value for private in session.private_values):
                raise ProbeFailure("credential_exposure_detected")
            if any(
                canonical_email(private) is not None
                and private.casefold() in value.casefold()
                for private in session.private_values
            ):
                raise ProbeFailure("credential_exposure_detected")
        elif isinstance(value, Mapping):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)


def parse_card_metadata(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProbeFailure("provider_metadata_invalid")
    card_id = payload.get("id")
    name = payload.get("name")
    display = payload.get("display")
    archived = payload.get("archived")
    collection_id = payload.get("collection_id")
    database_id = payload.get("database_id")
    if (
        isinstance(card_id, bool)
        or not isinstance(card_id, int)
        or card_id < 1
        or not isinstance(name, str)
        or name != name.strip()
        or not name
        or len(name) > 1024
        or not isinstance(display, str)
        or display != display.strip()
        or not display
        or not isinstance(archived, bool)
    ):
        raise ProbeFailure("provider_metadata_invalid")
    for value in (collection_id, database_id):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ProbeFailure("provider_metadata_invalid")
    return {
        "id": card_id,
        "name": name,
        "display": display,
        "archived": archived,
        "collection_id": collection_id,
        "database_id": database_id,
    }


def read_visible_cards(
    session: AuthenticatedSession,
    *,
    requester: Requester,
) -> list[dict[str, Any]]:
    cards_ok, payload = requester(
        session.origin,
        "GET",
        "/api/card",
        None,
        session.session_token,
    )
    if not cards_ok:
        raise ProbeFailure("provider_metadata_read_failed")
    if not isinstance(payload, list):
        raise ProbeFailure("provider_metadata_invalid")
    return [parse_card_metadata(row) for row in payload]


def probe(
    *,
    route_loader: Callable[[], tuple[str, Path]] = load_registered_route_contract,
    credential_loader: Callable[[Path], dict[str, str]] = load_credentials,
    requester: Requester = request_json,
) -> tuple[bool, dict[str, Any]]:
    result = base_result(schema=SCHEMA, operation=None)
    # Keep the established probe contract stable for the resolver parser.
    result["secrets_redacted"] = result.pop("credential_secrets_redacted")
    try:
        session = open_exact_session(
            result,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )
    except ProbeFailure as exc:
        result["error"] = exc.code
        return False, result
    result["provider_identity_sha256"] = session.provider_identity_sha256
    result["ok"] = True
    try:
        ensure_credential_secrets_redacted(result, session)
    except ProbeFailure as exc:
        safe = fail_closed_result(
            result,
            schema=SCHEMA,
            operation=None,
            error=exc.code,
        )
        safe["secrets_redacted"] = safe.pop("credential_secrets_redacted")
        return False, safe
    return True, result


def metadata_read(
    *,
    search: str | None = None,
    limit: int = 25,
    route_loader: Callable[[], tuple[str, Path]] = load_registered_route_contract,
    credential_loader: Callable[[Path], dict[str, str]] = load_credentials,
    requester: Requester = request_json,
) -> tuple[bool, dict[str, Any]]:
    result = base_result(schema=READ_SCHEMA, operation="metadata-read")
    try:
        if type(limit) is not int or limit < 1 or limit > MAX_METADATA_RESULTS:
            raise ProbeFailure("metadata_limit_invalid")
        session = open_exact_session(
            result,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )
        cards = read_visible_cards(session, requester=requester)
        query = search.casefold() if search else None
        matches = [
            card
            for card in cards
            if query is None or query in card["name"].casefold()
        ]
        result.update(
            {
                "provider_identity_sha256": session.provider_identity_sha256,
                "visible_card_count": len(cards),
                "matched_card_count": len(matches),
                "cards": matches[:limit],
                "truncated": len(matches) > limit,
                "ok": True,
            }
        )
        ensure_credential_secrets_redacted(result, session)
    except ProbeFailure as exc:
        return False, fail_closed_result(
            result,
            schema=READ_SCHEMA,
            operation="metadata-read",
            error=exc.code,
        )
    return True, result


def query_saved_card(
    *,
    card_id: int,
    max_rows: int = 25,
    route_loader: Callable[[], tuple[str, Path]] = load_registered_route_contract,
    credential_loader: Callable[[Path], dict[str, str]] = load_credentials,
    requester: Requester = request_json,
) -> tuple[bool, dict[str, Any]]:
    result = base_result(schema=READ_SCHEMA, operation="query-readonly")
    try:
        if type(card_id) is not int or card_id < 1:
            raise ProbeFailure("card_id_invalid")
        if type(max_rows) is not int or max_rows < 1 or max_rows > MAX_QUERY_ROWS:
            raise ProbeFailure("query_row_limit_invalid")
        session = open_exact_session(
            result,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )
        cards = read_visible_cards(session, requester=requester)
        card = next((row for row in cards if row["id"] == card_id), None)
        if card is None:
            raise ProbeFailure("provider_card_not_visible")
        query_ok, payload = requester(
            session.origin,
            "POST",
            f"/api/card/{card_id}/query",
            {"parameters": []},
            session.session_token,
        )
        if not query_ok:
            raise ProbeFailure("provider_query_failed")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ProbeFailure("provider_query_invalid")
        data = payload["data"]
        rows = data.get("rows")
        columns = data.get("cols")
        if not isinstance(rows, list) or not isinstance(columns, list):
            raise ProbeFailure("provider_query_invalid")
        column_names: list[str] = []
        for column in columns:
            if not isinstance(column, dict):
                raise ProbeFailure("provider_query_invalid")
            name = column.get("display_name") or column.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ProbeFailure("provider_query_invalid")
            column_names.append(name.strip())
        if any(
            not isinstance(row, list) or len(row) != len(column_names)
            for row in rows
        ):
            raise ProbeFailure("provider_query_invalid")
        if payload.get("status") != "completed" or (
            payload.get("cached") is not None
            and not isinstance(payload.get("cached"), bool)
        ):
            raise ProbeFailure("provider_query_unsatisfied")
        reported_row_count = payload.get("row_count")
        if reported_row_count is not None and (
            isinstance(reported_row_count, bool)
            or not isinstance(reported_row_count, int)
            or reported_row_count < len(rows)
        ):
            raise ProbeFailure("provider_query_invalid")
        total_row_count = (
            reported_row_count if reported_row_count is not None else len(rows)
        )
        returned_row_count = min(len(rows), max_rows)
        result.update(
            {
                "provider_identity_sha256": session.provider_identity_sha256,
                "card": card,
                "query_status": payload.get("status"),
                "cached": payload.get("cached"),
                "row_count": total_row_count,
                "returned_row_count": returned_row_count,
                "columns": column_names,
                "rows": rows[:max_rows],
                "truncated": total_row_count > returned_row_count,
                "ok": True,
            }
        )
        ensure_credential_secrets_redacted(result, session)
    except ProbeFailure as exc:
        return False, fail_closed_result(
            result,
            schema=READ_SCHEMA,
            operation="query-readonly",
            error=exc.code,
        )
    return True, result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Use the exact read-only CompanyAlpha Gigabrain route'
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="accepted for registered callers; output is always JSON",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--metadata",
        action="store_true",
        help="list visible saved-question metadata",
    )
    operation.add_argument(
        "--query-card",
        type=int,
        metavar="CARD_ID",
        help="execute one visible saved question read-only",
    )
    parser.add_argument(
        "--search",
        help="case-insensitive saved-question name filter for --metadata",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help=f"maximum metadata rows (1-{MAX_METADATA_RESULTS})",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=25,
        help=f"maximum saved-query result rows (1-{MAX_QUERY_ROWS})",
    )
    args = parser.parse_args(argv)
    if args.search is not None and not args.metadata:
        parser.error("--search requires --metadata")
    if args.limit < 1 or args.limit > MAX_METADATA_RESULTS:
        parser.error(f"--limit must be between 1 and {MAX_METADATA_RESULTS}")
    if args.max_rows < 1 or args.max_rows > MAX_QUERY_ROWS:
        parser.error(f"--max-rows must be between 1 and {MAX_QUERY_ROWS}")
    if args.query_card is not None and args.query_card < 1:
        parser.error("--query-card must be a positive integer")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.metadata:
            ok, result = metadata_read(search=args.search, limit=args.limit)
        elif args.query_card is not None:
            ok, result = query_saved_card(
                card_id=args.query_card,
                max_rows=args.max_rows,
            )
        else:
            ok, result = probe()
    except Exception:  # pragma: no cover - fail closed without private diagnostics
        ok = False
        operation = (
            "metadata-read"
            if args.metadata
            else "query-readonly"
            if args.query_card is not None
            else None
        )
        result = base_result(
            schema=READ_SCHEMA if operation is not None else SCHEMA,
            operation=operation,
        )
        if operation is None:
            result["secrets_redacted"] = result.pop(
                "credential_secrets_redacted"
            )
        result["error"] = "internal_error"
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
