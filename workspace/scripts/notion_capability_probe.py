#!/usr/bin/env python3
'Nondestructive Notion route probe for personal and CompanyAlpha integrations.\n\nDefault behavior checks route health via GET /v1/users/me using the configured\nintegration token(s) and prints a redacted JSON summary. Optionally, callers can\npass --page-id to verify whether a specific page is visible to the selected route.\n\nNo secrets are printed.\n'

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Tuple

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

NOTION_VERSION = "2025-09-03"
ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "registry" / "integration_routes.json"
ROUTE_IDS: Dict[str, str] = {
    "personal": "notion-personal",
    'company-alpha': 'notion-company-alpha',
}
CREDENTIAL_HANDLE_IDS: Dict[str, str] = {
    "personal": "notion.personal.api",
    'company-alpha': 'notion.company_alpha.api',
}
MAX_CREDENTIAL_FILE_BYTES = 64 * 1024
MAX_NOTION_REQUEST_BYTES = 64 * 1024
MAX_NOTION_RESPONSE_BYTES = 2 * 1024 * 1024
NOTION_API_ORIGIN = "https://api.notion.com"
NOTION_REQUEST_TIMEOUT_SECONDS = 20
ENV_ASSIGNMENT_PATTERN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)")


class ProbeFailure(RuntimeError):
    """Expected, value-free credential-route failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CredentialSource(NamedTuple):
    """One exact registry-bound owner coordinate for a Notion token."""

    handle_id: str
    owner_kind: str
    owner_path: Path
    env_name: str


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Keep every credential-bearing request on the exact Notion API origin."""

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


NOTION_NO_REDIRECT_OPENER = urllib.request.build_opener(_RejectRedirects())


def _strict_json_object(raw: bytes) -> Dict[str, object]:
    """Decode one UTF-8 JSON object while rejecting duplicate keys."""

    try:
        text = raw.decode("utf-8")
        payload = loads_json_strict(
            text,
            source="Notion provider response",
        )
    except (UnicodeDecodeError, RegistryContractError) as exc:
        raise ProbeFailure("notion_response_json_invalid") from exc
    if not isinstance(payload, dict):
        raise ProbeFailure("notion_response_shape_invalid")
    return payload


def _notion_request_url(path: str) -> str:
    """Build a credential-bearing URL only from a local /v1 API path."""

    if not isinstance(path, str):
        raise ProbeFailure("notion_request_path_invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in path):
        raise ProbeFailure("notion_request_path_invalid")
    parsed = urllib.parse.urlsplit(path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not parsed.path.startswith("/v1/")
        or parsed.path.startswith("//")
        or "\\" in parsed.path
    ):
        raise ProbeFailure("notion_request_path_invalid")
    return f"{NOTION_API_ORIGIN}{path}"


def load_registered_credential_sources() -> Dict[str, CredentialSource]:
    """Resolve each Notion route to one exact typed credential owner."""

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

    sources: Dict[str, CredentialSource] = {}
    for route_name, route_id in ROUTE_IDS.items():
        matches = []
        for handle in payload.get("credential_handles", []):
            if not isinstance(handle, dict):
                continue
            consumers = handle.get("consumers")
            if not isinstance(consumers, list):
                continue
            if {
                "kind": "integration-route",
                "consumer_id": route_id,
            } in consumers:
                matches.append(handle)
        if len(matches) != 1:
            raise ProbeFailure("registry_credential_handle_not_unique")
        handle = matches[0]
        owner = handle.get("owner")
        if (
            handle.get("handle_id") != CREDENTIAL_HANDLE_IDS[route_name]
            or handle.get("credential_kind") != "api-token"
            or handle.get("provisioning_state") != "provisioned"
            or handle.get("browser_binding") is not None
            or not isinstance(owner, dict)
            or owner.get("kind")
            not in {"workspace-secret-file", "runtime-secret-file"}
            or not isinstance(owner.get("ref"), str)
            or not isinstance(owner.get("keys"), list)
            or len(owner["keys"]) != 1
        ):
            raise ProbeFailure("registry_credential_binding_invalid")
        sources[route_name] = CredentialSource(
            handle_id=handle["handle_id"],
            owner_kind=owner["kind"],
            owner_path=Path(owner["ref"]),
            env_name=owner["keys"][0],
        )
    return sources


def read_owner_private_text(path: Path) -> str:
    """Read one exact owner-private regular file through a bound descriptor."""

    if (
        not path.is_absolute()
        or any(component in {"", ".", ".."} for component in path.parts[1:])
    ):
        raise ProbeFailure("credential_owner_path_invalid")
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ProbeFailure("credential_owner_unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ProbeFailure("credential_owner_not_regular")
    if before.st_uid != os.getuid():
        raise ProbeFailure("credential_owner_uid_invalid")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise ProbeFailure("credential_owner_mode_invalid")
    if before.st_nlink != 1:
        raise ProbeFailure("credential_owner_link_count_invalid")
    if before.st_size > MAX_CREDENTIAL_FILE_BYTES:
        raise ProbeFailure("credential_owner_too_large")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProbeFailure("credential_owner_unavailable") from exc

    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ProbeFailure("credential_owner_changed")
        chunks: List[bytes] = []
        remaining = MAX_CREDENTIAL_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_CREDENTIAL_FILE_BYTES:
            raise ProbeFailure("credential_owner_too_large")

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
        if any(
            getattr(before, field) != getattr(after, field)
            for field in stable_fields
        ) or len(raw) != after.st_size:
            raise ProbeFailure("credential_owner_changed")
    finally:
        os.close(descriptor)

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeFailure("credential_owner_not_utf8") from exc


def parse_owner_env_key(text: str, env_name: str) -> str:
    """Extract only one declared key without shell evaluation or expansion."""

    seen: set[str] = set()
    selected: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        match = ENV_ASSIGNMENT_PATTERN.fullmatch(line)
        if match is None:
            raise ProbeFailure("credential_owner_env_invalid")
        key, raw_value = match.groups()
        if key in seen:
            raise ProbeFailure("credential_owner_env_duplicate_key")
        seen.add(key)
        value = raw_value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ProbeFailure("credential_owner_env_invalid")
            value = value[1:-1]
        if "\x00" in value:
            raise ProbeFailure("credential_owner_env_invalid")
        if key == env_name:
            selected = value
    if not selected:
        raise ProbeFailure("credential_owner_key_missing")
    return selected


def load_registered_credential(source: CredentialSource) -> str:
    """Load only the registry-declared key from its exact owner-private file."""

    if source.owner_kind not in {"workspace-secret-file", "runtime-secret-file"}:
        raise ProbeFailure("credential_owner_kind_invalid")
    return parse_owner_env_key(
        read_owner_private_text(source.owner_path),
        source.env_name,
    )


def notion_request(
    api_key: str,
    path: str,
    *,
    method: str = "GET",
    body: Mapping[str, object] | None = None,
    opener: object | None = None,
) -> Tuple[bool, Dict[str, object]]:
    """Perform one bounded, no-redirect Notion request with value-free errors."""

    try:
        url = _notion_request_url(path)
        if (
            not isinstance(api_key, str)
            or not api_key
            or "\r" in api_key
            or "\n" in api_key
        ):
            raise ProbeFailure("notion_credential_invalid")
        if method not in {"GET", "POST"}:
            raise ProbeFailure("notion_request_method_invalid")
        if method == "GET" and body is not None:
            raise ProbeFailure("notion_request_body_invalid")
        if method == "POST" and not isinstance(body, Mapping):
            raise ProbeFailure("notion_request_body_invalid")
        encoded_body = (
            json.dumps(
                dict(body),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if body is not None
            else None
        )
        if encoded_body is not None and len(encoded_body) > MAX_NOTION_REQUEST_BYTES:
            raise ProbeFailure("notion_request_too_large")
        request = urllib.request.Request(
            url,
            data=encoded_body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        request_opener = (
            NOTION_NO_REDIRECT_OPENER if opener is None else opener
        )
        open_request = getattr(request_opener, "open", None)
        if not callable(open_request):
            raise ProbeFailure("notion_transport_invalid")
        with open_request(
            request,
            timeout=NOTION_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            response_url = response.geturl()
            if response_url != url:
                raise ProbeFailure("notion_redirect_rejected")
            status = getattr(response, "status", None)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise ProbeFailure("notion_response_status_invalid")
            content_type = response.headers.get("Content-Type", "")
            media_type = (
                content_type.split(";", 1)[0].strip().lower()
                if isinstance(content_type, str)
                else ""
            )
            if media_type != "application/json":
                raise ProbeFailure("notion_response_content_type_invalid")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise ProbeFailure("notion_response_length_invalid") from exc
                if declared_length < 0 or declared_length > MAX_NOTION_RESPONSE_BYTES:
                    raise ProbeFailure("notion_response_too_large")
            raw = response.read(MAX_NOTION_RESPONSE_BYTES + 1)
            if len(raw) > MAX_NOTION_RESPONSE_BYTES:
                raise ProbeFailure("notion_response_too_large")
            return True, _strict_json_object(raw)
    except urllib.error.HTTPError as exc:
        try:
            if 300 <= exc.code < 400:
                return False, {"error": "notion_redirect_rejected"}
            return False, {"error": "notion_http_error", "status": exc.code}
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, {"error": "notion_transport_failed"}
    except ProbeFailure as exc:
        return False, {"error": exc.code}
    except Exception:  # pragma: no cover - last-resort value-free boundary
        return False, {"error": "notion_transport_failed"}


def normalize_page_id(page_id: str) -> str:
    cleaned = (page_id or "").strip()
    if len(cleaned) == 32 and "-" not in cleaned:
        return f"{cleaned[0:8]}-{cleaned[8:12]}-{cleaned[12:16]}-{cleaned[16:20]}-{cleaned[20:32]}"
    return cleaned


def probe_route(
    route: str,
    *,
    page_id: str | None = None,
    credential_sources: Mapping[str, CredentialSource] | None = None,
) -> Tuple[bool, Dict[str, object]]:
    sources = (
        load_registered_credential_sources()
        if credential_sources is None
        else credential_sources
    )
    try:
        source = sources[route]
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbeFailure("credential_source_missing") from exc
    if not isinstance(source, CredentialSource):
        raise ProbeFailure("credential_source_invalid")
    result: Dict[str, object] = {
        "route": route,
        "credential_handle_id": source.handle_id,
    }
    try:
        api_key = load_registered_credential(source)
    except ProbeFailure as exc:
        result.update(
            {
                "credential_owner_bound": False,
                "ok": False,
                "error": exc.code,
            }
        )
        return False, result
    result["credential_owner_bound"] = True

    ok, payload = notion_request(api_key, "/v1/users/me")
    result["route_ok"] = ok
    if ok:
        bot = payload.get("bot") if isinstance(payload.get("bot"), dict) else {}
        owner = bot.get("owner") if isinstance(bot.get("owner"), dict) else {}
        result["bot_name"] = payload.get("name")
        if isinstance(owner, dict):
            result["owner_type"] = owner.get("type")
        if isinstance(bot, dict):
            if bot.get("workspace_name"):
                result["workspace_name"] = bot.get("workspace_name")
            if bot.get("workspace_id"):
                result["workspace_id"] = bot.get("workspace_id")
    else:
        result["route_error"] = payload

    target_ok = ok
    if page_id:
        normalized = normalize_page_id(page_id)
        page_ok, page_payload = notion_request(api_key, f"/v1/pages/{normalized}")
        result["page_id"] = normalized
        result["page_access_ok"] = page_ok
        if page_ok:
            result["page_object"] = page_payload.get("object")
            result["page_url"] = page_payload.get("url")
        else:
            result["page_error"] = page_payload
        target_ok = target_ok and page_ok

    result["ok"] = target_ok
    return target_ok, result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe configured Notion integration routes")
    parser.add_argument(
        "--route",
        choices=["personal", 'company-alpha', "both"],
        default="both",
        help="Which route to probe (default: both)",
    )
    parser.add_argument(
        "--page-id",
        help="Optional page id to verify access for the selected route(s)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected: List[str] = [args.route] if args.route != "both" else ["personal", 'company-alpha']
    outputs: List[Dict[str, object]] = []
    overall_ok = True
    try:
        credential_sources = load_registered_credential_sources()
    except ProbeFailure as exc:
        print(
            json.dumps(
                [{"ok": False, "error": exc.code}],
                indent=2,
            )
        )
        return 1
    for route in selected:
        ok, result = probe_route(
            route,
            page_id=args.page_id,
            credential_sources=credential_sources,
        )
        overall_ok = overall_ok and ok
        outputs.append(result)
    print(json.dumps(outputs, indent=2))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
