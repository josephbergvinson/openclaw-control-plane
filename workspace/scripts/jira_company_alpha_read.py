#!/usr/bin/env python3
'Bounded, read-only content access for the exact CompanyAlpha Jira project.'

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Sequence, Tuple

try:
    from scripts import jira_company_alpha_capability_probe as jira_probe
except ModuleNotFoundError:  # Direct execution from scripts/.
    import jira_company_alpha_capability_probe as jira_probe  # type: ignore[no-redef]


ROUTE_ID = jira_probe.ROUTE_ID
EXPECTED_SITE = jira_probe.EXPECTED_SITE
EXPECTED_PROJECT_KEY = jira_probe.EXPECTED_PROJECT_KEY
DEFAULT_ENV_FILE = jira_probe.DEFAULT_ENV_FILE
MAX_PROVIDER_RESPONSE_BYTES = jira_probe.MAX_PROVIDER_RESPONSE_BYTES

ISSUE_KEY = re.compile('ALPHA-[1-9][0-9]*\\Z')
MAX_SEARCH_TEXT_BYTES = 512
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50
DEFAULT_COMMENTS_LIMIT = 20
MAX_COMMENTS_LIMIT = 100
SEARCH_PAGE_SIZE = 25
COMMENTS_PAGE_SIZE = 50
MAX_PROVIDER_PAGES = 4
MAX_ITEM_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_PAGE_TOKEN_BYTES = 2 * 1024

SEARCH_FIELDS = (
    "summary",
    "issuetype",
    "status",
    "priority",
    "assignee",
    "labels",
    "updated",
)
ALL_ISSUE_FIELDS = "*all"
COMMENT_FIELDS = ("author", "body", "created", "updated")


class ReaderFailure(RuntimeError):
    """Expected read failure carrying only a stable, value-free code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


Requester = Callable[[str, str, str, str], Tuple[bool, Dict[str, Any]]]
RouteLoader = Callable[[], Tuple[Dict[str, Any], str, Path]]
CredentialLoader = Callable[[Path], Dict[str, str]]

# This existing boundary owns exact-site GETs, strict duplicate-key JSON,
# response-size enforcement, and redirect refusal.
request_json = jira_probe.request_json


class ValueFreeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ReaderFailure("invalid_arguments")


def _bounded_string(value: Any, *, code: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise ReaderFailure(code)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ReaderFailure(code) from exc
    if size > max_bytes:
        raise ReaderFailure(code)
    return value


def _search_text(value: Any) -> str:
    text = _bounded_string(
        value,
        code="search_text_invalid",
        max_bytes=MAX_SEARCH_TEXT_BYTES,
    )
    if not text or text != text.strip() or any(ord(char) < 0x20 for char in text):
        raise ReaderFailure("search_text_invalid")
    return text


def _issue_key(value: Any, *, expected: str | None = None) -> str:
    key = _bounded_string(value, code="issue_key_invalid", max_bytes=64)
    if ISSUE_KEY.fullmatch(key) is None or (expected is not None and key != expected):
        raise ReaderFailure("issue_key_invalid")
    return key


def _limit(value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReaderFailure("limit_invalid")
    if not minimum <= value <= maximum:
        raise ReaderFailure("limit_invalid")
    return value


def _urlencode(items: Sequence[tuple[str, str | int]]) -> str:
    return urllib.parse.urlencode(
        items,
        doseq=False,
        safe="",
        quote_via=urllib.parse.quote,
    )


def _search_path(text: str, *, max_results: int, page_token: str | None) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    jql = (
        f'project = {EXPECTED_PROJECT_KEY} AND text ~ "{escaped}" '
        "ORDER BY updated DESC"
    )
    params: list[tuple[str, str | int]] = [
        ("jql", jql),
        ("maxResults", max_results),
        ("fields", ",".join(SEARCH_FIELDS)),
    ]
    if page_token is not None:
        params.append(("nextPageToken", page_token))
    return "/rest/api/3/search/jql?" + _urlencode(params)


def _issue_path(key: str) -> str:
    return (
        f"/rest/api/3/issue/{urllib.parse.quote(key, safe='')}?"
        + _urlencode((("fields", ALL_ISSUE_FIELDS),))
    )


def _comments_path(key: str, *, start_at: int, max_results: int) -> str:
    return (
        f"/rest/api/3/issue/{urllib.parse.quote(key, safe='')}/comment?"
        + _urlencode(
            (
                ("startAt", start_at),
                ("maxResults", max_results),
                ("orderBy", "-created"),
            )
        )
    )


def _provider_failure(payload: Mapping[str, Any]) -> ReaderFailure:
    code = {
        "http_error": "provider_http_error",
        "network_error": "provider_network_error",
        "response_too_large": "provider_response_too_large",
        "invalid_json": "provider_response_invalid",
        "unexpected_payload_type": "provider_response_invalid",
        "site_mismatch": "provider_request_rejected",
        "request_path_invalid": "provider_request_rejected",
    }.get(payload.get("error"), "provider_read_failed")
    return ReaderFailure(code)


def _get(
    requester: Requester,
    site: str,
    email: str,
    token: str,
    path: str,
) -> Dict[str, Any]:
    ok, payload = requester(site, email, token, path)
    if not ok:
        raise _provider_failure(payload)
    if not isinstance(payload, dict):
        raise ReaderFailure("provider_response_invalid")
    return payload


def _exact_account(
    route_loader: RouteLoader,
    credential_loader: CredentialLoader,
    requester: Requester,
) -> tuple[str, str, str]:
    try:
        _route, expected_digest, credential_path = route_loader()
        credentials = credential_loader(credential_path)
    except jira_probe.ProbeFailure as exc:
        code = (
            "route_binding_unavailable"
            if exc.code.startswith("registry_")
            else "credential_unavailable"
        )
        raise ReaderFailure(code) from exc
    try:
        site = credentials["ATLASSIAN_SITE_URL"].rstrip("/")
        email = credentials["ATLASSIAN_EMAIL"]
        token = credentials["ATLASSIAN_API_TOKEN"]
    except (AttributeError, KeyError, TypeError) as exc:
        raise ReaderFailure("credential_unavailable") from exc
    if site != EXPECTED_SITE or not email or not token:
        raise ReaderFailure("credential_unavailable")

    account = _get(requester, site, email, token, "/rest/api/3/myself")
    account_id = account.get("accountId")
    if not jira_probe.canonical_provider_id(account_id):
        raise ReaderFailure("provider_account_invalid")
    actual_digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise ReaderFailure("provider_account_mismatch")
    return site, email, token


def _bounded_copy(value: Any) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ReaderFailure("provider_payload_invalid") from exc
    if len(encoded) > MAX_ITEM_BYTES:
        raise ReaderFailure("provider_item_too_large")
    return json.loads(encoded.decode("utf-8"))


def _search_nested_text(
    value: Any,
    field: str,
    *,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, dict) or not isinstance(value.get(field), str):
        raise ReaderFailure("provider_payload_invalid")
    return value[field]


def _project_search_issue(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("fields"), dict):
        raise ReaderFailure("provider_payload_invalid")
    key = _issue_key(value.get("key"))
    fields = value["fields"]
    summary = fields.get("summary")
    labels = fields.get("labels")
    updated = fields.get("updated")
    if (
        not isinstance(summary, str)
        or not isinstance(labels, list)
        or any(not isinstance(label, str) for label in labels)
        or not isinstance(updated, str)
    ):
        raise ReaderFailure("provider_payload_invalid")
    return _bounded_copy(
        {
            "key": key,
            "summary": summary,
            "issue_type": _search_nested_text(fields.get("issuetype"), "name"),
            "status": _search_nested_text(fields.get("status"), "name"),
            "priority": _search_nested_text(
                fields.get("priority"),
                "name",
                nullable=True,
            ),
            "assignee": _search_nested_text(
                fields.get("assignee"),
                "displayName",
                nullable=True,
            ),
            "labels": labels,
            "updated": updated,
        }
    )


def _project_exact_issue(value: Any, *, expected_key: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("fields"), dict):
        raise ReaderFailure("provider_payload_invalid")
    key = _issue_key(value.get("key"), expected=expected_key)
    return {"key": key, "fields": _bounded_copy(value["fields"])}


def _page_token(value: Any) -> str | None:
    if value is None:
        return None
    token = _bounded_string(
        value,
        code="provider_pagination_invalid",
        max_bytes=MAX_PAGE_TOKEN_BYTES,
    )
    if not token or any(ord(char) < 0x20 for char in token):
        raise ReaderFailure("provider_pagination_invalid")
    return token


def search_issues(
    text: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    route_loader: RouteLoader = jira_probe.load_registered_route_contract,
    credential_loader: CredentialLoader = jira_probe.load_credentials,
    requester: Requester = request_json,
) -> Dict[str, Any]:
    text = _search_text(text)
    limit = _limit(limit, minimum=1, maximum=MAX_SEARCH_LIMIT)
    site, email, token = _exact_account(route_loader, credential_loader, requester)
    issues: list[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_tokens: set[str] = set()
    page_token: str | None = None

    for _ in range(MAX_PROVIDER_PAGES):
        remaining = limit - len(issues)
        if remaining <= 0:
            break
        payload = _get(
            requester,
            site,
            email,
            token,
            _search_path(
                text,
                max_results=min(SEARCH_PAGE_SIZE, remaining),
                page_token=page_token,
            ),
        )
        page = payload.get("issues")
        if not isinstance(page, list) or len(page) > min(SEARCH_PAGE_SIZE, remaining):
            raise ReaderFailure("provider_pagination_invalid")
        for raw_issue in page:
            issue = _project_search_issue(raw_issue)
            if issue["key"] in seen_keys:
                raise ReaderFailure("provider_pagination_invalid")
            seen_keys.add(issue["key"])
            issues.append(issue)

        next_token = _page_token(payload.get("nextPageToken"))
        is_last = payload.get("isLast")
        if is_last is not None and not isinstance(is_last, bool):
            raise ReaderFailure("provider_pagination_invalid")
        if is_last is False and next_token is None:
            raise ReaderFailure("provider_pagination_invalid")
        if len(issues) >= limit or is_last is True or next_token is None:
            page_token = None
            break
        if not page or next_token in seen_tokens:
            raise ReaderFailure("provider_pagination_invalid")
        seen_tokens.add(next_token)
        page_token = next_token
    else:
        if page_token is not None and len(issues) < limit:
            raise ReaderFailure("provider_page_limit_exceeded")

    return {
        "ok": True,
        "route": ROUTE_ID,
        "operation": "search",
        "project_key": EXPECTED_PROJECT_KEY,
        "ordering": "updated-desc",
        "count": len(issues),
        "issues": issues,
    }


def _project_comment(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ReaderFailure("provider_payload_invalid")
    comment_id = value.get("id")
    if not jira_probe.canonical_provider_id(comment_id):
        raise ReaderFailure("provider_payload_invalid")
    selected = {field: value.get(field) for field in COMMENT_FIELDS}
    return {"id": comment_id, **_bounded_copy(selected)}


def read_issue(
    key: str,
    *,
    comments_limit: int = DEFAULT_COMMENTS_LIMIT,
    route_loader: RouteLoader = jira_probe.load_registered_route_contract,
    credential_loader: CredentialLoader = jira_probe.load_credentials,
    requester: Requester = request_json,
) -> Dict[str, Any]:
    key = _issue_key(key, expected=key)
    comments_limit = _limit(
        comments_limit,
        minimum=0,
        maximum=MAX_COMMENTS_LIMIT,
    )
    site, email, token = _exact_account(route_loader, credential_loader, requester)
    issue = _project_exact_issue(
        _get(requester, site, email, token, _issue_path(key)),
        expected_key=key,
    )

    comments: list[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    start_at = 0
    total: int | None = None
    for _ in range(MAX_PROVIDER_PAGES):
        remaining = comments_limit - len(comments)
        if remaining <= 0:
            break
        page_size = min(COMMENTS_PAGE_SIZE, remaining)
        payload = _get(
            requester,
            site,
            email,
            token,
            _comments_path(key, start_at=start_at, max_results=page_size),
        )
        page = payload.get("comments")
        page_start = payload.get("startAt")
        page_total = payload.get("total")
        if (
            not isinstance(page, list)
            or len(page) > page_size
            or isinstance(page_start, bool)
            or not isinstance(page_start, int)
            or page_start != start_at
            or isinstance(page_total, bool)
            or not isinstance(page_total, int)
            or page_total < start_at + len(page)
            or (total is not None and page_total != total)
        ):
            raise ReaderFailure("provider_pagination_invalid")
        total = page_total
        for raw_comment in page:
            comment = _project_comment(raw_comment)
            if comment["id"] in seen_ids:
                raise ReaderFailure("provider_pagination_invalid")
            seen_ids.add(comment["id"])
            comments.append(comment)
        start_at += len(page)
        if start_at >= total or len(comments) >= comments_limit:
            break
        if not page:
            raise ReaderFailure("provider_pagination_invalid")
    else:
        if total is not None and start_at < total and len(comments) < comments_limit:
            raise ReaderFailure("provider_page_limit_exceeded")

    return {
        "ok": True,
        "route": ROUTE_ID,
        "operation": "issue",
        "project_key": EXPECTED_PROJECT_KEY,
        "issue": issue,
        "comments_ordering": "created-desc",
        "comments_count": len(comments),
        "comments": comments,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = ValueFreeArgumentParser(
        description='Read bounded content from the exact CompanyAlpha Jira project'
    )
    commands = parser.add_subparsers(
        dest="operation",
        required=True,
        parser_class=ValueFreeArgumentParser,
    )
    search = commands.add_parser("search")
    search.add_argument("--text", required=True)
    search.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    issue = commands.add_parser("issue")
    issue.add_argument("key")
    issue.add_argument("--comments-limit", type=int, default=DEFAULT_COMMENTS_LIMIT)
    args = parser.parse_args(argv)
    if args.operation == "search":
        _search_text(args.text)
        _limit(args.limit, minimum=1, maximum=MAX_SEARCH_LIMIT)
    else:
        _issue_key(args.key, expected=args.key)
        _limit(args.comments_limit, minimum=0, maximum=MAX_COMMENTS_LIMIT)
    return args


def _render(payload: Mapping[str, Any]) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if len(rendered.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ReaderFailure("output_too_large")
    return rendered


def main(argv: list[str] | None = None) -> int:
    operation: str | None = None
    try:
        args = parse_args(argv)
        operation = args.operation
        result = (
            search_issues(args.text, limit=args.limit)
            if operation == "search"
            else read_issue(args.key, comments_limit=args.comments_limit)
        )
        rendered = _render(result)
    except ReaderFailure as exc:
        rendered = _render(
            {"ok": False, "route": ROUTE_ID, "operation": operation, "error": exc.code}
        )
        print(rendered)
        return 1
    except Exception:
        rendered = _render(
            {
                "ok": False,
                "route": ROUTE_ID,
                "operation": operation,
                "error": "internal_error",
            }
        )
        print(rendered)
        return 1
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
