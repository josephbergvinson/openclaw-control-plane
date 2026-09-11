#!/usr/bin/env python3
"""Bounded read-only Notion search and recursive page-content access."""

from __future__ import annotations

import argparse
import json
import urllib.parse
import uuid
from typing import Callable, Dict, List, Mapping, Sequence, Tuple

try:
    from scripts.notion_capability_probe import (
        CredentialSource,
        ProbeFailure,
        load_registered_credential,
        load_registered_credential_sources,
        notion_request,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from notion_capability_probe import (  # type: ignore[no-redef]
        CredentialSource,
        ProbeFailure,
        load_registered_credential,
        load_registered_credential_sources,
        notion_request,
    )


DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100
MAX_SEARCH_QUERY_BYTES = 1_000
DEFAULT_MAX_BLOCKS = 200
MAX_BLOCKS = 1_000
DEFAULT_MAX_DEPTH = 4
MAX_DEPTH = 8
MAX_CURSOR_BYTES = 1_024
MAX_PROVIDER_REQUESTS = 64
MAX_OUTPUT_TEXT_CHARACTERS = 128 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
MAX_FIELD_CHARACTERS = 16 * 1024
MAX_URL_CHARACTERS = 4 * 1024
MAX_RESULTS_PER_RESPONSE = 100
MAX_QUERY_RESULT_BYTES = 256 * 1024
MAX_BLOCK_RESULT_BYTES = 256 * 1024
MAX_PAGE_RESULT_BYTES = 256 * 1024
SAFE_TRANSPORT_FAILURE_CODES = frozenset(
    {
        "notion_credential_invalid",
        "notion_http_error",
        "notion_redirect_rejected",
        "notion_request_body_invalid",
        "notion_request_method_invalid",
        "notion_request_path_invalid",
        "notion_request_too_large",
        "notion_response_content_type_invalid",
        "notion_response_json_invalid",
        "notion_response_length_invalid",
        "notion_response_shape_invalid",
        "notion_response_status_invalid",
        "notion_response_too_large",
        "notion_transport_failed",
        "notion_transport_invalid",
    }
)

NotionRequest = Callable[..., Tuple[bool, Dict[str, object]]]


class NotionReadFailure(RuntimeError):
    """One stable, value-free failure at the reader boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ValueFreeArgumentParser(argparse.ArgumentParser):
    """Convert parser diagnostics into one stable public error code."""

    def error(self, _message: str) -> None:
        raise NotionReadFailure("invalid_arguments")


class ReadBudget:
    """Shared bounds for one page traversal and its serialized content."""

    def __init__(
        self,
        *,
        remaining_blocks: int,
        remaining_text_characters: int = MAX_OUTPUT_TEXT_CHARACTERS,
    ) -> None:
        self.remaining_blocks = remaining_blocks
        self.remaining_text_characters = remaining_text_characters
        self.request_count = 0
        self.truncated = False
        self.expanded_block_ids: set[str] = set()
        self.seen_block_ids: set[str] = set()

    def reserve_request(self) -> bool:
        if self.request_count >= MAX_PROVIDER_REQUESTS:
            self.truncated = True
            return False
        self.request_count += 1
        return True

    def take_text(self, value: object, *, maximum: int) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        allowed = min(maximum, self.remaining_text_characters)
        if allowed <= 0:
            self.truncated = True
            return None
        if len(value) > allowed:
            self.truncated = True
        selected = value[:allowed]
        self.remaining_text_characters -= len(selected)
        return selected


def _bounded_integer(value: int, *, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotionReadFailure(code)
    if value < minimum or value > maximum:
        raise NotionReadFailure(code)
    return value


def _canonical_page_id(value: str, *, require_canonical: bool) -> str:
    if not isinstance(value, str):
        raise NotionReadFailure("page_id_invalid")
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise NotionReadFailure("page_id_invalid") from exc
    if require_canonical and value != canonical:
        raise NotionReadFailure("page_id_not_canonical")
    return canonical


def _canonical_data_source_id(value: str) -> str:
    if not isinstance(value, str):
        raise NotionReadFailure("data_source_id_invalid")
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise NotionReadFailure("data_source_id_invalid") from exc
    if value != canonical:
        raise NotionReadFailure("data_source_id_not_canonical")
    return canonical


def _search_query(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise NotionReadFailure("search_query_invalid")
    query = value.strip()
    if (
        not query
        or len(query.encode("utf-8")) > MAX_SEARCH_QUERY_BYTES
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in query)
    ):
        raise NotionReadFailure("search_query_invalid")
    return query


def _route_credential(
    route: str,
    credential_sources: Mapping[str, CredentialSource] | None,
) -> Tuple[CredentialSource, str]:
    if route not in {"personal", 'company-alpha'}:
        raise NotionReadFailure("route_invalid")
    try:
        sources = (
            load_registered_credential_sources()
            if credential_sources is None
            else credential_sources
        )
        source = sources[route]
        if not isinstance(source, CredentialSource):
            raise ProbeFailure("credential_source_invalid")
        credential = load_registered_credential(source)
    except (KeyError, TypeError, ValueError, ProbeFailure) as exc:
        code = exc.code if isinstance(exc, ProbeFailure) else "credential_source_missing"
        raise NotionReadFailure(code) from exc
    return source, credential


def _provider_request(
    request: NotionRequest,
    credential: str,
    path: str,
    *,
    method: str = "GET",
    body: Mapping[str, object] | None = None,
) -> Dict[str, object]:
    try:
        ok, payload = request(credential, path, method=method, body=body)
    except Exception:
        raise NotionReadFailure("notion_request_failed") from None
    if not ok:
        raw_code = payload.get("error") if isinstance(payload, dict) else None
        code = (
            raw_code
            if isinstance(raw_code, str) and raw_code in SAFE_TRANSPORT_FAILURE_CODES
            else "notion_request_failed"
        )
        raise NotionReadFailure(code)
    if not isinstance(payload, dict):
        raise NotionReadFailure("notion_response_shape_invalid")
    return payload


def _provider_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise NotionReadFailure("notion_response_shape_invalid")
    try:
        return _canonical_page_id(value, require_canonical=False)
    except NotionReadFailure as exc:
        raise NotionReadFailure("notion_response_shape_invalid") from exc


def _response_results(payload: Mapping[str, object]) -> List[object]:
    results = payload.get("results")
    if not isinstance(results, list) or len(results) > MAX_RESULTS_PER_RESPONSE:
        raise NotionReadFailure("notion_response_shape_invalid")
    return results


def _next_cursor(
    payload: Mapping[str, object],
    seen: set[str],
) -> str | None:
    has_more = payload.get("has_more")
    cursor = payload.get("next_cursor")
    if not isinstance(has_more, bool):
        raise NotionReadFailure("notion_response_cursor_invalid")
    if not has_more:
        if cursor is not None:
            raise NotionReadFailure("notion_response_cursor_invalid")
        return None
    if (
        not isinstance(cursor, str)
        or not cursor
        or len(cursor.encode("utf-8")) > MAX_CURSOR_BYTES
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in cursor)
        or cursor in seen
    ):
        raise NotionReadFailure("notion_response_cursor_invalid")
    seen.add(cursor)
    return cursor


def _raw_rich_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    parts: List[str] = []
    for item in value:
        if not isinstance(item, dict):
            raise NotionReadFailure("notion_response_shape_invalid")
        plain = item.get("plain_text")
        if not isinstance(plain, str):
            text = item.get("text")
            plain = text.get("content") if isinstance(text, dict) else None
        if plain is None:
            continue
        if not isinstance(plain, str):
            raise NotionReadFailure("notion_response_shape_invalid")
        parts.append(plain)
    return "".join(parts)


def _page_title(payload: Mapping[str, object]) -> str:
    direct_title = payload.get("title")
    if isinstance(direct_title, list):
        title = _raw_rich_text(direct_title)
        if title:
            return title
    properties = payload.get("properties")
    if not isinstance(properties, dict):
        return ""
    for property_value in properties.values():
        if not isinstance(property_value, dict):
            continue
        if property_value.get("type") == "title":
            title = _raw_rich_text(property_value.get("title"))
            if title:
                return title
    return ""


def _project_search_result(
    value: object,
    budget: ReadBudget,
) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise NotionReadFailure("notion_response_shape_invalid")
    object_type = value.get("object")
    if object_type not in {"page", "data_source", "database"}:
        raise NotionReadFailure("notion_response_shape_invalid")
    result: Dict[str, object] = {
        "object": object_type,
        "id": _provider_uuid(value.get("id")),
    }
    title = budget.take_text(_page_title(value), maximum=MAX_FIELD_CHARACTERS)
    if title is not None:
        result["title"] = title
    url = budget.take_text(value.get("url"), maximum=MAX_URL_CHARACTERS)
    if url is not None:
        result["url"] = url
    last_edited_time = budget.take_text(value.get("last_edited_time"), maximum=128)
    if last_edited_time is not None:
        result["last_edited_time"] = last_edited_time
    return result


def _copy_json_value(value: object, *, code: str) -> object:
    """Copy one provider value through strict JSON without interpreting its schema."""

    try:
        serialized = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        copied = json.loads(serialized)
    except (RecursionError, TypeError, ValueError) as exc:
        raise NotionReadFailure(code) from exc
    if copied != value:
        raise NotionReadFailure(code)
    return copied


def _copy_json_object(value: object) -> Dict[str, object]:
    copied = _copy_json_value(value, code="notion_properties_invalid")
    if not isinstance(copied, dict):
        raise NotionReadFailure("notion_properties_invalid")
    return copied


def _assert_json_bound(
    value: Mapping[str, object],
    *,
    maximum: int,
    invalid_code: str,
    too_large_code: str,
) -> None:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise NotionReadFailure(invalid_code) from exc
    if len(encoded) > maximum:
        raise NotionReadFailure(too_large_code)


def _project_query_result(
    value: object,
    budget: ReadBudget,
    *,
    data_source_id: str,
) -> Dict[str, object]:
    if not isinstance(value, dict) or value.get("object") not in {
        "page",
        "data_source",
    }:
        raise NotionReadFailure("notion_query_result_invalid")
    projected = _project_search_result(value, budget)
    projected.pop("url", None)
    if value["object"] == "page":
        parent = value.get("parent")
        if (
            not isinstance(parent, dict)
            or parent.get("type") != "data_source_id"
            or _provider_uuid(parent.get("data_source_id")) != data_source_id
        ):
            raise NotionReadFailure("notion_query_identity_mismatch")
        for key in ("archived", "in_trash"):
            field = value.get(key)
            if field is not None:
                if not isinstance(field, bool):
                    raise NotionReadFailure("notion_query_result_invalid")
                projected[key] = field
    projected["properties"] = _copy_json_object(value.get("properties"))
    _assert_json_bound(
        projected,
        maximum=MAX_QUERY_RESULT_BYTES,
        invalid_code="notion_query_result_invalid",
        too_large_code="notion_query_result_too_large",
    )
    return projected


def search(
    route: str,
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    credential_sources: Mapping[str, CredentialSource] | None = None,
    request: NotionRequest | None = None,
) -> Dict[str, object]:
    """Search one registry-bound Notion workspace with bounded pagination."""

    normalized_query = _search_query(query)
    result_limit = _bounded_integer(
        limit,
        minimum=1,
        maximum=MAX_SEARCH_LIMIT,
        code="search_limit_invalid",
    )
    source, credential = _route_credential(route, credential_sources)
    request_function = notion_request if request is None else request
    budget = ReadBudget(remaining_blocks=0)
    results: List[Dict[str, object]] = []
    seen_result_ids: set[str] = set()
    cursor: str | None = None
    seen_cursors: set[str] = set()
    provider_has_more = False

    while len(results) < result_limit:
        if not budget.reserve_request():
            break
        body: Dict[str, object] = {
            "query": normalized_query,
            "page_size": min(MAX_RESULTS_PER_RESPONSE, result_limit - len(results)),
        }
        if cursor is not None:
            body["start_cursor"] = cursor
        payload = _provider_request(
            request_function,
            credential,
            "/v1/search",
            method="POST",
            body=body,
        )
        raw_results = _response_results(payload)
        remaining = result_limit - len(results)
        for raw_result in raw_results[:remaining]:
            projected = _project_search_result(raw_result, budget)
            result_id = projected["id"]
            if result_id in seen_result_ids:
                raise NotionReadFailure("notion_response_result_duplicate")
            seen_result_ids.add(result_id)
            results.append(projected)
        cursor = _next_cursor(payload, seen_cursors)
        provider_has_more = cursor is not None or len(raw_results) > remaining
        if len(raw_results) > remaining:
            budget.truncated = True
        if cursor is None:
            break
        if len(results) >= result_limit:
            budget.truncated = True
            break

    if cursor is not None or provider_has_more:
        budget.truncated = True
    output: Dict[str, object] = {
        "ok": True,
        "route": route,
        "credential_handle_id": source.handle_id,
        "operation": "search",
        "query": normalized_query,
        "result_count": len(results),
        "results": results,
        "truncated": budget.truncated,
    }
    _assert_output_bound(output)
    return output


def query_data_source(
    route: str,
    data_source_id: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    credential_sources: Mapping[str, CredentialSource] | None = None,
    request: NotionRequest | None = None,
) -> Dict[str, object]:
    """Query one canonical data source with bounded result pagination."""

    canonical_id = _canonical_data_source_id(data_source_id)
    result_limit = _bounded_integer(
        limit,
        minimum=1,
        maximum=MAX_SEARCH_LIMIT,
        code="query_limit_invalid",
    )
    source, credential = _route_credential(route, credential_sources)
    request_function = notion_request if request is None else request
    budget = ReadBudget(remaining_blocks=0)
    results: List[Dict[str, object]] = []
    seen_result_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None

    while len(results) < result_limit:
        if not budget.reserve_request():
            break
        body: Dict[str, object] = {
            "page_size": min(MAX_RESULTS_PER_RESPONSE, result_limit - len(results)),
        }
        if cursor is not None:
            body["start_cursor"] = cursor
        payload = _provider_request(
            request_function,
            credential,
            f"/v1/data_sources/{canonical_id}/query",
            method="POST",
            body=body,
        )
        raw_results = _response_results(payload)
        remaining = result_limit - len(results)
        for raw_result in raw_results[:remaining]:
            projected = _project_query_result(
                raw_result,
                budget,
                data_source_id=canonical_id,
            )
            result_id = projected["id"]
            if result_id in seen_result_ids:
                raise NotionReadFailure("notion_response_result_duplicate")
            seen_result_ids.add(result_id)
            results.append(projected)
        cursor = _next_cursor(payload, seen_cursors)
        if len(raw_results) > remaining:
            budget.truncated = True
        if cursor is None:
            break
        if len(results) >= result_limit:
            budget.truncated = True
            break

    if cursor is not None:
        budget.truncated = True
    output: Dict[str, object] = {
        "ok": True,
        "route": route,
        "credential_handle_id": source.handle_id,
        "operation": "query",
        "data_source_id": canonical_id,
        "result_count": len(results),
        "results": results,
        "truncated": budget.truncated,
    }
    _assert_output_bound(output)
    return output


def _block_text(value: Mapping[str, object], block_type: str) -> str:
    block_value = value.get(block_type)
    if not isinstance(block_value, dict):
        return ""
    rich_text = _raw_rich_text(block_value.get("rich_text"))
    if rich_text:
        return rich_text
    if block_type in {"child_page", "child_database"}:
        title = block_value.get("title")
        return title if isinstance(title, str) else ""
    if block_type == "equation":
        expression = block_value.get("expression")
        return expression if isinstance(expression, str) else ""
    if block_type == "table_row":
        cells = block_value.get("cells")
        if not isinstance(cells, list):
            return ""
        return " | ".join(_raw_rich_text(cell) for cell in cells)
    url = block_value.get("url")
    if block_type in {"bookmark", "embed", "link_preview"} and isinstance(url, str):
        return url
    return ""


def _project_block(
    value: object,
    budget: ReadBudget,
) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise NotionReadFailure("notion_response_shape_invalid")
    block_type = value.get("type")
    has_children = value.get("has_children")
    if (
        value.get("object") != "block"
        or not isinstance(block_type, str)
        or not isinstance(has_children, bool)
    ):
        raise NotionReadFailure("notion_response_shape_invalid")
    block_id = _provider_uuid(value.get("id"))
    if block_id in budget.seen_block_ids:
        raise NotionReadFailure("notion_response_block_duplicate")
    budget.seen_block_ids.add(block_id)
    result: Dict[str, object] = {
        "id": block_id,
        "type": block_type,
        "has_children": has_children,
    }
    if block_type not in value:
        raise NotionReadFailure("notion_response_shape_invalid")
    result[block_type] = _copy_json_value(
        value[block_type],
        code="notion_block_content_invalid",
    )
    text = budget.take_text(
        _block_text(value, block_type),
        maximum=MAX_FIELD_CHARACTERS,
    )
    if text is not None:
        result["text"] = text
    _assert_json_bound(
        result,
        maximum=MAX_BLOCK_RESULT_BYTES,
        invalid_code="notion_block_content_invalid",
        too_large_code="notion_block_content_too_large",
    )
    return result


def _read_children(
    request: NotionRequest,
    credential: str,
    parent_id: str,
    *,
    depth: int,
    max_depth: int,
    budget: ReadBudget,
) -> List[Dict[str, object]]:
    children: List[Dict[str, object]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while budget.remaining_blocks > 0:
        if not budget.reserve_request():
            break
        query: Dict[str, object] = {
            "page_size": min(MAX_RESULTS_PER_RESPONSE, budget.remaining_blocks),
        }
        if cursor is not None:
            query["start_cursor"] = cursor
        path = f"/v1/blocks/{parent_id}/children?{urllib.parse.urlencode(query)}"
        payload = _provider_request(request, credential, path)
        raw_results = _response_results(payload)
        for raw_block in raw_results:
            if budget.remaining_blocks <= 0:
                budget.truncated = True
                break
            projected = _project_block(raw_block, budget)
            budget.remaining_blocks -= 1
            if projected["has_children"]:
                block_id = projected["id"]
                if depth >= max_depth or budget.remaining_blocks <= 0:
                    projected["children_truncated"] = True
                    budget.truncated = True
                elif block_id in budget.expanded_block_ids:
                    raise NotionReadFailure("notion_response_cycle_invalid")
                else:
                    budget.expanded_block_ids.add(block_id)
                    nested = _read_children(
                        request,
                        credential,
                        block_id,
                        depth=depth + 1,
                        max_depth=max_depth,
                        budget=budget,
                    )
                    projected["children"] = nested
                    if budget.truncated and not nested:
                        projected["children_truncated"] = True
            children.append(projected)
        cursor = _next_cursor(payload, seen_cursors)
        if cursor is None:
            break
        if budget.remaining_blocks <= 0:
            budget.truncated = True
            break
    return children


def page(
    route: str,
    page_id: str,
    *,
    max_blocks: int = DEFAULT_MAX_BLOCKS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    credential_sources: Mapping[str, CredentialSource] | None = None,
    request: NotionRequest | None = None,
) -> Dict[str, object]:
    """Read one canonical page id and its bounded recursive block content."""

    canonical_id = _canonical_page_id(page_id, require_canonical=True)
    block_limit = _bounded_integer(
        max_blocks,
        minimum=1,
        maximum=MAX_BLOCKS,
        code="max_blocks_invalid",
    )
    depth_limit = _bounded_integer(
        max_depth,
        minimum=1,
        maximum=MAX_DEPTH,
        code="max_depth_invalid",
    )
    source, credential = _route_credential(route, credential_sources)
    request_function = notion_request if request is None else request
    budget = ReadBudget(remaining_blocks=block_limit)
    if not budget.reserve_request():  # pragma: no cover - fresh budget invariant
        raise NotionReadFailure("provider_request_budget_exhausted")
    metadata = _provider_request(
        request_function,
        credential,
        f"/v1/pages/{canonical_id}",
    )
    if metadata.get("object") != "page" or _provider_uuid(metadata.get("id")) != canonical_id:
        raise NotionReadFailure("notion_page_identity_mismatch")
    page_result: Dict[str, object] = {
        "id": canonical_id,
        "properties": _copy_json_object(metadata.get("properties")),
    }
    title = budget.take_text(_page_title(metadata), maximum=MAX_FIELD_CHARACTERS)
    if title is not None:
        page_result["title"] = title
    url = budget.take_text(metadata.get("url"), maximum=MAX_URL_CHARACTERS)
    if url is not None:
        page_result["url"] = url
    last_edited_time = budget.take_text(metadata.get("last_edited_time"), maximum=128)
    if last_edited_time is not None:
        page_result["last_edited_time"] = last_edited_time
    _assert_json_bound(
        page_result,
        maximum=MAX_PAGE_RESULT_BYTES,
        invalid_code="notion_page_result_invalid",
        too_large_code="notion_page_result_too_large",
    )
    budget.expanded_block_ids.add(canonical_id)
    children = _read_children(
        request_function,
        credential,
        canonical_id,
        depth=1,
        max_depth=depth_limit,
        budget=budget,
    )
    output: Dict[str, object] = {
        "ok": True,
        "route": route,
        "credential_handle_id": source.handle_id,
        "operation": "page",
        "page": page_result,
        "block_count": block_limit - budget.remaining_blocks,
        "blocks": children,
        "truncated": budget.truncated,
    }
    _assert_output_bound(output)
    return output


def _assert_output_bound(payload: Mapping[str, object]) -> None:
    _serialize_output(payload)


def _serialize_output(payload: Mapping[str, object]) -> str:
    try:
        serialized = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        encoded = serialized.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise NotionReadFailure("output_invalid") from exc
    if len(encoded) + 1 > MAX_OUTPUT_BYTES:
        raise NotionReadFailure("output_too_large")
    return serialized


def _integer_argument(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = ValueFreeArgumentParser(description="Read a configured Notion workspace")
    parser.add_argument("--route", required=True, choices=["personal", 'company-alpha'])
    commands = parser.add_subparsers(dest="command", required=True)

    search_parser = commands.add_parser("search", help="Search visible pages")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--limit", type=_integer_argument, default=DEFAULT_SEARCH_LIMIT)

    query_parser = commands.add_parser("query", help="Query a data source")
    query_parser.add_argument("data_source_id")
    query_parser.add_argument(
        "--limit",
        type=_integer_argument,
        default=DEFAULT_SEARCH_LIMIT,
    )

    page_parser = commands.add_parser("page", help="Read a page and its child blocks")
    page_parser.add_argument("page_id")
    page_parser.add_argument(
        "--max-blocks",
        type=_integer_argument,
        default=DEFAULT_MAX_BLOCKS,
    )
    page_parser.add_argument(
        "--max-depth",
        type=_integer_argument,
        default=DEFAULT_MAX_DEPTH,
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.command == "search":
            result = search(args.route, args.query, limit=args.limit)
        elif args.command == "query":
            result = query_data_source(
                args.route,
                args.data_source_id,
                limit=args.limit,
            )
        elif args.command == "page":
            result = page(
                args.route,
                args.page_id,
                max_blocks=args.max_blocks,
                max_depth=args.max_depth,
            )
        else:  # pragma: no cover - argparse invariant
            raise NotionReadFailure("command_invalid")
        print(_serialize_output(result))
        return 0
    except NotionReadFailure as exc:
        print(json.dumps({"ok": False, "error": exc.code}, sort_keys=True))
        return 1
    except Exception:  # pragma: no cover - last-resort value-free boundary
        print(json.dumps({"ok": False, "error": "notion_reader_failed"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
