#!/usr/bin/env python3
'Read-only, exact-account readiness probe for the CompanyAlpha Jira route.'

from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import argparse
import base64
import hashlib
import hmac
import json
import os
import pwd
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Tuple

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
ROUTE_ID = 'jira-company-alpha'
CREDENTIAL_HANDLE_ID = 'jira.company_alpha.api'
CANONICAL_USERNAME = _operator_binding("identifiers.host_user")
CANONICAL_HOME = Path(_operator_binding('paths.host_home'))
DEFAULT_ENV_FILE = Path(
    _operator_binding('paths.routing_company_alpha_jira_env')
)
MAX_ENV_FILE_BYTES = 16 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
EXPECTED_SITE = _operator_binding('services.jira.site')
EXPECTED_PROJECT_KEY = _operator_binding('services.jira.project_key')
EXPECTED_ISSUE_TYPE = "QA Feedback"
REQUIRED_ENV_KEYS = {
    "ATLASSIAN_SITE_URL",
    "ATLASSIAN_EMAIL",
    "ATLASSIAN_API_TOKEN",
}
REQUIRED_FIELD_NAMES = {
    "Summary",
    "Description",
    "Reporter",
    "Platform",
    "Tester Name",
    "Labels",
}
CANONICAL_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]*")


class ProbeFailure(RuntimeError):
    """Expected, redaction-safe readiness failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


Requester = Callable[[str, str, str, str], Tuple[bool, Dict[str, Any]]]


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so origin-bound authorization never propagates."""

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


def open_url_no_redirect(
    request: urllib.request.Request, *, timeout: int
) -> Any:
    return NO_REDIRECT_OPENER.open(request, timeout=timeout)


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
    max_bytes: int = MAX_ENV_FILE_BYTES,
) -> str:
    """Read one exact regular file through a no-follow, descriptor-bound handle."""

    if path != expected_path or not path.is_absolute():
        raise ProbeFailure("credential_path_mismatch")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    components = path.parts
    if not components or components[0] != "/" or any(
        component in {"", ".", ".."} for component in components[1:]
    ):
        raise ProbeFailure("credential_path_mismatch")

    parent_descriptor: int | None = None
    try:
        parent_descriptor = os.open("/", directory_flags)
        for component in components[1:-1]:
            next_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=parent_descriptor,
            )
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
        descriptor = os.open(
            components[-1],
            file_flags,
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise ProbeFailure("credential_open_failed") from exc
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
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
        if before.st_size > max_bytes:
            raise ProbeFailure("credential_file_too_large")

        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
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
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise ProbeFailure("credential_changed_during_read")
        if len(raw) != after.st_size:
            raise ProbeFailure("credential_changed_during_read")
    finally:
        os.close(descriptor)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeFailure("credential_not_utf8") from exc


def parse_env_text(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
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


def load_registered_route_contract() -> Tuple[Dict[str, Any], str, Path]:
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
    if route.get("required_account") != urllib.parse.urlsplit(EXPECTED_SITE).hostname:
        raise ProbeFailure("registry_site_binding_mismatch")
    credential_matches = []
    for handle in payload.get("credential_handles", []):
        if not isinstance(handle, dict):
            continue
        consumers = handle.get("consumers")
        if not isinstance(consumers, list):
            continue
        if {
            "kind": "integration-route",
            "consumer_id": ROUTE_ID,
        } in consumers:
            credential_matches.append(handle)
    if len(credential_matches) != 1:
        raise ProbeFailure("registry_credential_handle_not_unique")
    credential_handle = credential_matches[0]
    credential_owner = credential_handle.get("owner")
    if (
        credential_handle.get("handle_id") != CREDENTIAL_HANDLE_ID
        or credential_handle.get("credential_kind") != "api-credential-bundle"
        or credential_handle.get("provisioning_state") != "provisioned"
        or credential_handle.get("browser_binding") is not None
        or not isinstance(credential_owner, dict)
        or credential_owner.get("kind") != "runtime-secret-file"
        or credential_owner.get("ref") != str(DEFAULT_ENV_FILE)
        or set(credential_owner.get("keys", [])) != REQUIRED_ENV_KEYS
    ):
        raise ProbeFailure("registry_credential_binding_mismatch")
    if route.get("operations", {}).get("issue-create") != {
        "lane": "authenticated_ui",
        "effect": "mutation",
        "readiness_status_id": 'Mutation readiness — `jira-company-alpha`',
    }:
        raise ProbeFailure("registry_operation_binding_mismatch")
    try:
        digest = route["provider_adapter"]["native_evidence"][
            "provider_account_id_sha256"
        ]
    except (KeyError, TypeError) as exc:
        raise ProbeFailure("registry_identity_binding_missing") from exc
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ProbeFailure("registry_identity_binding_invalid")
    return route, digest, DEFAULT_ENV_FILE


def load_credentials(path: Path) -> Dict[str, str]:
    text = read_owner_private_text(
        path,
        expected_path=DEFAULT_ENV_FILE,
        expected_uid=canonical_owner_uid(),
    )
    values = parse_env_text(text)
    if values["ATLASSIAN_SITE_URL"].rstrip("/") != EXPECTED_SITE:
        raise ProbeFailure("credential_site_mismatch")
    return values


def request_json(
    site: str, email: str, token: str, path: str
) -> Tuple[bool, Dict[str, Any]]:
    if site.rstrip("/") != EXPECTED_SITE:
        return False, {"error": "site_mismatch"}
    parsed_path = urllib.parse.urlsplit(path)
    if not path.startswith("/") or parsed_path.scheme or parsed_path.netloc:
        return False, {"error": "request_path_invalid"}
    auth = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
    request = urllib.request.Request(
        site.rstrip("/") + path,
        headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with open_url_no_redirect(request, timeout=30) as response:
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
            source="Jira provider response",
        )
    except (UnicodeDecodeError, RegistryContractError):
        return False, {"error": "invalid_json"}
    if not isinstance(payload, dict):
        return False, {"error": "unexpected_payload_type"}
    return True, payload


def _request_error(payload: Mapping[str, Any]) -> Dict[str, Any]:
    error_code = payload.get("error")
    if error_code not in {
        "http_error",
        "network_error",
        "response_too_large",
        "invalid_json",
        "unexpected_payload_type",
    }:
        error_code = "provider_read_failed"
    error = {"error": error_code}
    status_code = payload.get("status")
    if isinstance(status_code, int):
        error["status"] = status_code
    return error


def canonical_provider_id(value: Any) -> bool:
    """Accept only canonical ASCII provider IDs used by Jira metadata."""

    return bool(
        isinstance(value, str)
        and CANONICAL_PROVIDER_ID.fullmatch(value) is not None
    )


def probe(
    *,
    route_loader: Callable[
        [], Tuple[Dict[str, Any], str, Path]
    ] = load_registered_route_contract,
    credential_loader: Callable[[Path], Dict[str, str]] = load_credentials,
    requester: Requester = request_json,
) -> Tuple[bool, Dict[str, Any]]:
    result: Dict[str, Any] = {
        "route": ROUTE_ID,
        "system": "jira",
        "credential_handle_id": CREDENTIAL_HANDLE_ID,
        "site_expected": EXPECTED_SITE,
        "project_key_expected": EXPECTED_PROJECT_KEY,
        "issue_type_expected": EXPECTED_ISSUE_TYPE,
    }
    try:
        _route, expected_account_digest, credential_path = route_loader()
        credentials = credential_loader(credential_path)
    except ProbeFailure as exc:
        result.update({"ok": False, "error": exc.code})
        return False, result

    site = credentials["ATLASSIAN_SITE_URL"].rstrip("/")
    result["credential_contract_bound"] = True
    result["site_matches_expected"] = site == EXPECTED_SITE
    if site != EXPECTED_SITE:
        result.update({"ok": False, "error": "credential_site_mismatch"})
        return False, result

    me_ok, me = requester(
        site,
        credentials["ATLASSIAN_EMAIL"],
        credentials["ATLASSIAN_API_TOKEN"],
        "/rest/api/3/myself",
    )
    result["auth_ok"] = me_ok
    if not me_ok:
        result["auth_error"] = _request_error(me)
        result["ok"] = False
        return False, result
    account_id = me.get("accountId")
    if not canonical_provider_id(account_id):
        result.update({"ok": False, "error": "provider_account_id_missing"})
        return False, result
    actual_digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    identity_matches = hmac.compare_digest(actual_digest, expected_account_digest)
    result["account_identity_matches_registered"] = identity_matches
    if not identity_matches:
        result.update({"ok": False, "error": "provider_account_mismatch"})
        return False, result
    result["account_type"] = me.get("accountType")

    project_ok, project = requester(
        site,
        credentials["ATLASSIAN_EMAIL"],
        credentials["ATLASSIAN_API_TOKEN"],
        f"/rest/api/3/project/{EXPECTED_PROJECT_KEY}",
    )
    result["project_ok"] = project_ok
    if not project_ok:
        result["project_error"] = _request_error(project)
        result["ok"] = False
        return False, result
    if project.get("key") != EXPECTED_PROJECT_KEY:
        result.update({"ok": False, "error": "project_identity_mismatch"})
        return False, result
    project_id = project.get("id")
    if not canonical_provider_id(project_id):
        result.update({"ok": False, "error": "project_identity_mismatch"})
        return False, result
    result["project_id"] = project_id
    result["project_key"] = project.get("key")
    result["project_name"] = project.get("name")

    meta_path = (
        "/rest/api/3/issue/createmeta?"
        + urllib.parse.urlencode(
            {
                "projectKeys": EXPECTED_PROJECT_KEY,
                "expand": "projects.issuetypes.fields",
            }
        )
    )
    meta_ok, meta = requester(
        site,
        credentials["ATLASSIAN_EMAIL"],
        credentials["ATLASSIAN_API_TOKEN"],
        meta_path,
    )
    result["createmeta_ok"] = meta_ok
    if not meta_ok:
        result["createmeta_error"] = _request_error(meta)
        result["ok"] = False
        return False, result

    matching_projects = [
        project_meta
        for project_meta in meta.get("projects", [])
        if isinstance(project_meta, dict)
        and project_meta.get("key") == EXPECTED_PROJECT_KEY
    ]
    if len(matching_projects) != 1:
        result.update({"ok": False, "error": "createmeta_project_not_unique"})
        return False, result
    project_meta = matching_projects[0]
    if project_meta.get("id") != project_id:
        result.update(
            {"ok": False, "error": "createmeta_project_identity_mismatch"}
        )
        return False, result
    matching_issue_types = [
        candidate
        for candidate in project_meta.get("issuetypes", [])
        if isinstance(candidate, dict) and candidate.get("name") == EXPECTED_ISSUE_TYPE
    ]
    result["qa_feedback_issue_type_ok"] = len(matching_issue_types) == 1
    if len(matching_issue_types) != 1:
        result.update({"ok": False, "error": "qa_feedback_issue_type_not_unique"})
        return False, result

    issue_type = matching_issue_types[0]
    issue_type_id = issue_type.get("id")
    if not canonical_provider_id(issue_type_id):
        result.update({"ok": False, "error": "qa_feedback_issue_type_id_invalid"})
        return False, result
    fields = issue_type.get("fields", {})
    if not isinstance(fields, dict):
        result.update({"ok": False, "error": "qa_feedback_fields_invalid"})
        return False, result
    required_field_ids: Dict[str, list[str]] = {
        name: [] for name in REQUIRED_FIELD_NAMES
    }
    for field_id, field in fields.items():
        if not isinstance(field, dict):
            continue
        field_name = field.get("name")
        if field_name in REQUIRED_FIELD_NAMES:
            required_field_ids[field_name].append(field_id)

    visible_fields = {
        name for name, field_ids in required_field_ids.items() if field_ids
    }
    missing_fields = sorted(REQUIRED_FIELD_NAMES - visible_fields)
    duplicate_fields = sorted(
        name for name, field_ids in required_field_ids.items() if len(field_ids) > 1
    )
    result["qa_feedback_issue_type_id"] = issue_type_id
    result["visible_required_field_names"] = sorted(
        REQUIRED_FIELD_NAMES & visible_fields
    )
    result["missing_required_field_names"] = missing_fields
    result["duplicate_required_field_names"] = duplicate_fields
    if missing_fields:
        result["ok"] = False
        return False, result
    if duplicate_fields:
        result.update(
            {"ok": False, "error": "qa_feedback_required_field_not_unique"}
        )
        return False, result
    invalid_field_ids = sorted(
        name
        for name, field_ids in required_field_ids.items()
        if len(field_ids) == 1
        and not canonical_provider_id(field_ids[0])
    )
    if invalid_field_ids:
        result.update(
            {
                "ok": False,
                "error": "qa_feedback_required_field_id_invalid",
                "invalid_required_field_id_names": invalid_field_ids,
            }
        )
        return False, result
    result["field_ids"] = {
        name: required_field_ids[name][0] for name in sorted(REQUIRED_FIELD_NAMES)
    }
    result["ok"] = True
    return bool(result["ok"]), result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Probe the exact CompanyAlpha Jira API route without mutating Jira'
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    parse_args(argv)
    try:
        ok, result = probe()
    except Exception:  # pragma: no cover - fail closed without leaking local/provider data
        ok = False
        result = {"route": ROUTE_ID, "system": "jira", "ok": False, "error": "internal_error"}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
