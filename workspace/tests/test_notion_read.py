from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


from routing_test_support import fixture_root
ROOT = fixture_root()
SCRIPT = ROOT / "scripts" / "notion_read.py"
SPEC = importlib.util.spec_from_file_location("notion_read", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
reader_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader_module)

PAGE_ID = "11111111-1111-1111-1111-111111111111"
CHILD_ID = "22222222-2222-2222-2222-222222222222"
SIBLING_ID = "33333333-3333-3333-3333-333333333333"
GRANDCHILD_ID = "44444444-4444-4444-4444-444444444444"
DATA_SOURCE_ID = "55555555-5555-5555-5555-555555555555"
PERSON_ID = "66666666-6666-6666-6666-666666666666"
RELATED_ID = "77777777-7777-7777-7777-777777777777"


def rich_text(value: str):
    return [{"type": "text", "plain_text": value}]


def list_payload(results, *, has_more: bool = False, next_cursor=None):
    return {
        "object": "list",
        "results": results,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


class NotionReaderTests(unittest.TestCase):
    def owner_sources(self, root: Path, *, route: str = "personal"):
        owner = root / "notion.env"
        if route == "personal":
            owner.write_text("NOTION_API_KEY=owner-token\n", encoding="utf-8")
            source = reader_module.CredentialSource(
                "notion.personal.api",
                "workspace-secret-file",
                owner,
                "NOTION_API_KEY",
            )
        else:
            owner.write_text(
                'NOTION_API_KEY_COMPANY_ALPHA=owner-token\n',
                encoding="utf-8",
            )
            source = reader_module.CredentialSource(
                'notion.company_alpha.api',
                "runtime-secret-file",
                owner,
                'NOTION_API_KEY_COMPANY_ALPHA',
            )
        owner.chmod(0o600)
        return {route: source}

    def test_registry_and_resolver_bind_reader_to_all_declared_read_operations(
        self,
    ) -> None:
        registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        handles = {
            row["handle_id"]: row for row in registry["credential_handles"]
        }
        reader_consumer = {
            "kind": "script",
            "consumer_id": "scripts/notion_read.py",
        }
        cases = (
            (
                "personal",
                "notion-personal",
                "notion.personal.api",
                'operator',
                'operator-personal-notion',
            ),
            (
                'company-alpha',
                'notion-company-alpha',
                'notion.company_alpha.api',
                'company-alpha',
                'company-alpha-team-notion',
            ),
        )
        routes = {row["route_id"]: row for row in registry["routes"]}
        for context, route_id, handle_id, principal, account in cases:
            with self.subTest(route=route_id):
                self.assertIn(reader_consumer, handles[handle_id]["consumers"])
                for operation in ("page-search", "page-read", "database-query"):
                    self.assertEqual(
                        routes[route_id]["operations"][operation],
                        {"lane": "declared_native", "effect": "read"},
                    )
                    result = subprocess.run(
                        [
                            "python3",
                            str(ROOT / "scripts" / "resolve_capability.py"),
                            "--system",
                            "notion",
                            "--intent",
                            "read",
                            "--context",
                            context,
                            "--required-operation",
                            operation,
                            "--principal",
                            principal,
                            "--account",
                            account,
                        ],
                        cwd=ROOT,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        result.stdout + result.stderr,
                    )
                    payload = json.loads(result.stdout)
                    self.assertEqual(
                        payload["preferred_lane"]["route_id"], route_id
                    )
                    self.assertEqual(
                        payload["operation_contract"]["classification"],
                        "native_supported",
                    )

    def test_search_uses_selected_owner_and_bounded_cursor_pagination(self) -> None:
        calls = []

        def request(credential, path, *, method="GET", body=None):
            calls.append((credential, path, method, body))
            if len(calls) == 1:
                return True, list_payload(
                    [
                        {
                            "object": "page",
                            "id": PAGE_ID,
                            "url": "https://www.notion.so/roadmap",
                            "last_edited_time": "2026-09-03T00:00:00.000Z",
                            "properties": {
                                "Name": {
                                    "type": "title",
                                    "title": rich_text("Roadmap"),
                                }
                            },
                        }
                    ],
                    has_more=True,
                    next_cursor="cursor-2",
                )
            return True, list_payload(
                [
                    {
                        "object": "data_source",
                        "id": CHILD_ID,
                        "title": rich_text("Planning database"),
                    }
                ]
            )

        with tempfile.TemporaryDirectory() as tmp:
            result = reader_module.search(
                "personal",
                "  roadmap  ",
                limit=2,
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )

        self.assertEqual(result["credential_handle_id"], "notion.personal.api")
        self.assertEqual(result["query"], "roadmap")
        self.assertEqual(result["result_count"], 2)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["results"][0]["title"], "Roadmap")
        self.assertEqual(result["results"][1]["title"], "Planning database")
        self.assertEqual(calls[0][0], "owner-token")
        self.assertEqual(calls[0][1:3], ("/v1/search", "POST"))
        self.assertEqual(
            calls[0][3],
            {"query": "roadmap", "page_size": 2},
        )
        self.assertEqual(
            calls[1][3],
            {"query": "roadmap", "page_size": 1, "start_cursor": "cursor-2"},
        )
        self.assertNotIn("owner-token", json.dumps(result))

    def test_search_stops_at_limit_and_marks_more_provider_results_truncated(self) -> None:
        def request(credential, path, *, method="GET", body=None):
            return True, list_payload(
                [
                    {"object": "page", "id": PAGE_ID},
                    {"object": "page", "id": CHILD_ID},
                ],
                has_more=True,
                next_cursor="cursor-2",
            )

        with tempfile.TemporaryDirectory() as tmp:
            result = reader_module.search(
                'company-alpha',
                "tokenomics",
                limit=1,
                credential_sources=self.owner_sources(
                    Path(tmp),
                    route='company-alpha',
                ),
                request=request,
            )

        self.assertEqual(result["result_count"], 1)
        self.assertTrue(result["truncated"])

    def test_query_preserves_full_properties_and_uses_bounded_pagination(self) -> None:
        calls = []

        def request(credential, path, *, method="GET", body=None):
            calls.append((credential, path, method, body))
            if len(calls) == 1:
                return True, list_payload(
                    [
                        {
                            "object": "page",
                            "id": PAGE_ID,
                            "url": "https://provider.example/omitted",
                            "parent": {
                                "type": "data_source_id",
                                "data_source_id": DATA_SOURCE_ID,
                            },
                            "archived": False,
                            "in_trash": False,
                            "properties": {
                                "Name": {
                                    "type": "title",
                                    "title": rich_text("Tokenomics"),
                                },
                                "Status": {
                                    "type": "status",
                                    "status": {
                                        "id": "status-1",
                                        "name": "In progress",
                                        "color": "blue",
                                    },
                                },
                                "Owner": {
                                    "type": "people",
                                    "people": [
                                        {
                                            "id": PERSON_ID,
                                            "name": 'Operator',
                                            "type": "person",
                                            "avatar_url": "omitted",
                                        }
                                    ],
                                },
                                "Due": {
                                    "type": "date",
                                    "date": {
                                        "start": "2026-09-05",
                                        "end": None,
                                        "time_zone": None,
                                    },
                                },
                                "Priority": {
                                    "type": "select",
                                    "select": {
                                        "id": "priority-1",
                                        "name": "High",
                                        "color": "red",
                                    },
                                },
                                "Notes": {
                                    "type": "rich_text",
                                    "rich_text": rich_text("Current decision record"),
                                },
                                "Score": {"type": "number", "number": 9.5},
                                "Ready": {"type": "checkbox", "checkbox": True},
                                "Related": {
                                    "type": "relation",
                                    "relation": [{"id": RELATED_ID}],
                                    "has_more": False,
                                },
                                "Provider expansion": {
                                    "type": "formula",
                                    "formula": {"type": "string", "string": "omitted"},
                                },
                                "Tags": {
                                    "type": "multi_select",
                                    "multi_select": [
                                        {
                                            "id": "tag-1",
                                            "name": "Protocol",
                                            "color": "green",
                                        }
                                    ],
                                },
                                "Contact": {
                                    "type": "email",
                                    "email": "owner@example.test",
                                },
                                "Reference": {
                                    "type": "url",
                                    "url": "https://docs.example.test/record",
                                },
                                "Future provider type": {
                                    "type": "future_property",
                                    "future_property": {
                                        "nested": ["kept", {"value": 7}],
                                    },
                                },
                            },
                        }
                    ],
                    has_more=True,
                    next_cursor="cursor-2",
                )
            return True, list_payload(
                [
                    {
                        "object": "data_source",
                        "id": CHILD_ID,
                        "title": rich_text("Nested record set"),
                        "url": "https://provider.example/omitted",
                        "properties": {
                            "Future schema": {
                                "type": "future_data_source_property",
                                "future_data_source_property": {
                                    "nested": ["kept", {"value": 11}],
                                },
                            },
                        },
                    }
                ]
            )

        with tempfile.TemporaryDirectory() as tmp:
            result = reader_module.query_data_source(
                'company-alpha',
                DATA_SOURCE_ID,
                limit=2,
                credential_sources=self.owner_sources(
                    Path(tmp),
                    route='company-alpha',
                ),
                request=request,
            )

        self.assertEqual(result["operation"], "query")
        self.assertEqual(result["data_source_id"], DATA_SOURCE_ID)
        self.assertEqual(result["result_count"], 2)
        self.assertFalse(result["truncated"])
        page = result["results"][0]
        self.assertNotIn("url", page)
        self.assertEqual(page["properties"]["Status"]["status"]["name"], "In progress")
        self.assertEqual(page["properties"]["Owner"]["people"][0]["id"], PERSON_ID)
        self.assertEqual(page["properties"]["Due"]["date"]["start"], "2026-09-05")
        self.assertEqual(page["properties"]["Priority"]["select"]["name"], "High")
        self.assertEqual(
            page["properties"]["Notes"]["rich_text"],
            rich_text("Current decision record"),
        )
        self.assertEqual(page["properties"]["Score"]["number"], 9.5)
        self.assertTrue(page["properties"]["Ready"]["checkbox"])
        self.assertEqual(
            page["properties"]["Related"]["relation"],
            [{"id": RELATED_ID}],
        )
        self.assertEqual(
            page["properties"]["Provider expansion"]["formula"],
            {"type": "string", "string": "omitted"},
        )
        self.assertEqual(
            page["properties"]["Tags"]["multi_select"][0]["name"],
            "Protocol",
        )
        self.assertEqual(
            page["properties"]["Contact"]["email"],
            "owner@example.test",
        )
        self.assertEqual(
            page["properties"]["Reference"]["url"],
            "https://docs.example.test/record",
        )
        self.assertEqual(
            page["properties"]["Future provider type"],
            {
                "type": "future_property",
                "future_property": {"nested": ["kept", {"value": 7}]},
            },
        )
        self.assertEqual(result["results"][1]["object"], "data_source")
        self.assertEqual(
            result["results"][1]["properties"],
            {
                "Future schema": {
                    "type": "future_data_source_property",
                    "future_data_source_property": {
                        "nested": ["kept", {"value": 11}],
                    },
                },
            },
        )
        self.assertNotIn("url", result["results"][1])
        path = f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        self.assertEqual(calls[0][1:], (path, "POST", {"page_size": 2}))
        self.assertEqual(
            calls[1][1:],
            (path, "POST", {"page_size": 1, "start_cursor": "cursor-2"}),
        )
        self.assertTrue(all(call[0] == "owner-token" for call in calls))
        self.assertNotIn("owner-token", json.dumps(result))

    def test_query_rejects_wrong_parent_identity_and_value_bearing_failure(self) -> None:
        def wrong_parent(credential, path, *, method="GET", body=None):
            return True, list_payload(
                [
                    {
                        "object": "page",
                        "id": PAGE_ID,
                        "parent": {
                            "type": "data_source_id",
                            "data_source_id": CHILD_ID,
                        },
                        "properties": {},
                    }
                ]
            )

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(
            reader_module.NotionReadFailure
        ) as raised:
            reader_module.query_data_source(
                "personal",
                DATA_SOURCE_ID,
                credential_sources=self.owner_sources(Path(tmp)),
                request=wrong_parent,
            )
        self.assertEqual(raised.exception.code, "notion_query_identity_mismatch")

        def failed(credential, path, *, method="GET", body=None):
            return False, {"error": "private_provider_value"}

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(
            reader_module.NotionReadFailure
        ) as raised:
            reader_module.query_data_source(
                "personal",
                DATA_SOURCE_ID,
                credential_sources=self.owner_sources(Path(tmp)),
                request=failed,
            )
        self.assertEqual(raised.exception.code, "notion_request_failed")
        self.assertNotIn("private_provider_value", str(raised.exception))

    def test_query_enforces_per_result_byte_budget(self) -> None:
        def request(credential, path, *, method="GET", body=None):
            return True, list_payload(
                [
                    {
                        "object": "page",
                        "id": PAGE_ID,
                        "parent": {
                            "type": "data_source_id",
                            "data_source_id": DATA_SOURCE_ID,
                        },
                        "properties": {
                            "Large": {
                                "type": "future_property",
                                "future_property": "private-value" * 10,
                            }
                        },
                    }
                ]
            )

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            reader_module,
            "MAX_QUERY_RESULT_BYTES",
            64,
        ), self.assertRaises(reader_module.NotionReadFailure) as raised:
            reader_module.query_data_source(
                "personal",
                DATA_SOURCE_ID,
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )
        self.assertEqual(raised.exception.code, "notion_query_result_too_large")
        self.assertNotIn("private-value", str(raised.exception))

    def test_page_reads_recursive_content_with_one_shared_block_budget(self) -> None:
        calls = []

        def request(credential, path, *, method="GET", body=None):
            calls.append((credential, path, method, body))
            if path == f"/v1/pages/{PAGE_ID}":
                return True, {
                    "object": "page",
                    "id": PAGE_ID,
                    "url": "https://www.notion.so/project",
                    "properties": {
                        "Name": {
                            "type": "title",
                            "title": rich_text("Project brief"),
                        }
                    },
                }
            if path == f"/v1/blocks/{PAGE_ID}/children?page_size=3":
                return True, list_payload(
                    [
                        {
                            "object": "block",
                            "id": CHILD_ID,
                            "type": "toggle",
                            "has_children": True,
                            "toggle": {"rich_text": rich_text("Details")},
                        },
                        {
                            "object": "block",
                            "id": SIBLING_ID,
                            "type": "heading_2",
                            "has_children": False,
                            "heading_2": {"rich_text": rich_text("Decision")},
                        },
                    ]
                )
            if path == f"/v1/blocks/{CHILD_ID}/children?page_size=2":
                return True, list_payload(
                    [
                        {
                            "object": "block",
                            "id": GRANDCHILD_ID,
                            "type": "paragraph",
                            "has_children": False,
                            "paragraph": {"rich_text": rich_text("Ship it")},
                        }
                    ]
                )
            self.fail(f"unexpected request path: {path}")

        with tempfile.TemporaryDirectory() as tmp:
            result = reader_module.page(
                "personal",
                PAGE_ID,
                max_blocks=3,
                max_depth=2,
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )

        self.assertEqual(result["page"]["title"], "Project brief")
        self.assertEqual(result["block_count"], 3)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["blocks"][0]["text"], "Details")
        self.assertEqual(
            result["blocks"][0]["children"][0]["text"],
            "Ship it",
        )
        self.assertEqual(result["blocks"][1]["text"], "Decision")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call[0] == "owner-token" for call in calls))
        self.assertNotIn("owner-token", json.dumps(result))

    def test_page_preserves_properties_for_zero_block_database_entry(self) -> None:
        properties = {
            "Name": {
                "type": "title",
                "title": rich_text("Zero-block project record"),
            },
            "Formula": {
                "type": "formula",
                "formula": {"type": "number", "number": 42},
            },
            "Future provider type": {
                "type": "future_property",
                "future_property": {
                    "nested": ["complete", {"provider_field": True}],
                },
            },
        }

        def request(credential, path, *, method="GET", body=None):
            if path == f"/v1/pages/{PAGE_ID}":
                return True, {
                    "object": "page",
                    "id": PAGE_ID,
                    "properties": properties,
                }
            return True, list_payload([])

        with tempfile.TemporaryDirectory() as tmp:
            result = reader_module.page(
                "personal",
                PAGE_ID,
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )

        self.assertEqual(result["page"]["properties"], properties)
        self.assertEqual(result["page"]["title"], "Zero-block project record")
        self.assertEqual(result["block_count"], 0)
        self.assertEqual(result["blocks"], [])

    def test_page_enforces_per_page_byte_budget(self) -> None:
        def request(credential, path, *, method="GET", body=None):
            if path == f"/v1/pages/{PAGE_ID}":
                return True, {
                    "object": "page",
                    "id": PAGE_ID,
                    "properties": {
                        "Large": {
                            "type": "future_property",
                            "future_property": "private-value" * 10,
                        }
                    },
                }
            self.fail("page result over its cap must fail before child reads")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            reader_module,
            "MAX_PAGE_RESULT_BYTES",
            64,
        ), self.assertRaises(reader_module.NotionReadFailure) as raised:
            reader_module.page(
                "personal",
                PAGE_ID,
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )
        self.assertEqual(raised.exception.code, "notion_page_result_too_large")
        self.assertNotIn("private-value", str(raised.exception))

    def test_page_preserves_unknown_structured_block_payload(self) -> None:
        structured_payload = {
            "checked": True,
            "language": "future-language",
            "caption": rich_text("Exact caption"),
            "file": {"type": "external", "external": {"url": "https://example.test"}},
            "future_nested": ["kept", {"value": 7}],
        }

        def request(credential, path, *, method="GET", body=None):
            if path == f"/v1/pages/{PAGE_ID}":
                return True, {"object": "page", "id": PAGE_ID, "properties": {}}
            return True, list_payload(
                [
                    {
                        "object": "block",
                        "id": CHILD_ID,
                        "type": "future_widget",
                        "has_children": False,
                        "future_widget": structured_payload,
                    }
                ]
            )

        with tempfile.TemporaryDirectory() as tmp:
            result = reader_module.page(
                "personal",
                PAGE_ID,
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )

        self.assertEqual(result["blocks"][0]["future_widget"], structured_payload)

    def test_page_enforces_per_block_byte_budget(self) -> None:
        def request(credential, path, *, method="GET", body=None):
            if path == f"/v1/pages/{PAGE_ID}":
                return True, {"object": "page", "id": PAGE_ID, "properties": {}}
            return True, list_payload(
                [
                    {
                        "object": "block",
                        "id": CHILD_ID,
                        "type": "future_widget",
                        "has_children": False,
                        "future_widget": {"private": "private-value" * 10},
                    }
                ]
            )

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            reader_module,
            "MAX_BLOCK_RESULT_BYTES",
            64,
        ), self.assertRaises(reader_module.NotionReadFailure) as raised:
            reader_module.page(
                "personal",
                PAGE_ID,
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )
        self.assertEqual(raised.exception.code, "notion_block_content_too_large")
        self.assertNotIn("private-value", str(raised.exception))

    def test_page_depth_and_block_limits_return_explicit_truncation(self) -> None:
        calls = []

        def request(credential, path, *, method="GET", body=None):
            calls.append(path)
            if path.startswith("/v1/pages/"):
                return True, {"object": "page", "id": PAGE_ID, "properties": {}}
            return True, list_payload(
                [
                    {
                        "object": "block",
                        "id": CHILD_ID,
                        "type": "toggle",
                        "has_children": True,
                        "toggle": {"rich_text": rich_text("More")},
                    },
                    {
                        "object": "block",
                        "id": SIBLING_ID,
                        "type": "paragraph",
                        "has_children": False,
                        "paragraph": {"rich_text": rich_text("Not returned")},
                    },
                ]
            )

        with tempfile.TemporaryDirectory() as tmp:
            result = reader_module.page(
                "personal",
                PAGE_ID,
                max_blocks=1,
                max_depth=1,
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )

        self.assertEqual(result["block_count"], 1)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["blocks"][0]["children_truncated"])
        self.assertEqual(len(calls), 2)

    def test_page_rejects_provider_identity_mismatch(self) -> None:
        def request(credential, path, *, method="GET", body=None):
            return True, {"object": "page", "id": CHILD_ID}

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(
            reader_module.NotionReadFailure
        ) as raised:
            reader_module.page(
                "personal",
                PAGE_ID,
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )
        self.assertEqual(raised.exception.code, "notion_page_identity_mismatch")

    def test_page_text_is_capped_and_reports_truncation(self) -> None:
        long_title = "x" * (reader_module.MAX_FIELD_CHARACTERS + 1)

        def request(credential, path, *, method="GET", body=None):
            if path.startswith("/v1/pages/"):
                return True, {
                    "object": "page",
                    "id": PAGE_ID,
                    "properties": {
                        "Name": {
                            "type": "title",
                            "title": rich_text(long_title),
                        }
                    },
                }
            return True, list_payload([])

        with tempfile.TemporaryDirectory() as tmp:
            result = reader_module.page(
                "personal",
                PAGE_ID,
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )
        self.assertEqual(len(result["page"]["title"]), reader_module.MAX_FIELD_CHARACTERS)
        self.assertTrue(result["truncated"])

    def test_noncanonical_page_id_and_invalid_limits_fail_before_credentials(self) -> None:
        invalid_calls = (
            lambda: reader_module.page("personal", PAGE_ID.replace("-", "")),
            lambda: reader_module.page("personal", PAGE_ID, max_blocks=0),
            lambda: reader_module.page(
                "personal",
                PAGE_ID,
                max_depth=reader_module.MAX_DEPTH + 1,
            ),
            lambda: reader_module.search("personal", "", limit=1),
            lambda: reader_module.search(
                "personal",
                "query",
                limit=reader_module.MAX_SEARCH_LIMIT + 1,
            ),
            lambda: reader_module.query_data_source(
                "personal",
                DATA_SOURCE_ID.replace("-", ""),
            ),
        )
        with mock.patch.object(
            reader_module,
            "load_registered_credential_sources",
        ) as credentials:
            for invoke in invalid_calls:
                with self.subTest(invoke=invoke), self.assertRaises(
                    reader_module.NotionReadFailure
                ):
                    invoke()
        credentials.assert_not_called()

    def test_provider_failure_is_value_free(self) -> None:
        def request(credential, path, *, method="GET", body=None):
            return False, {"error": "private_secret_from_provider"}

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(
            reader_module.NotionReadFailure
        ) as raised:
            reader_module.search(
                "personal",
                "roadmap",
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )

        self.assertEqual(raised.exception.code, "notion_request_failed")
        self.assertNotIn("private_secret_from_provider", str(raised.exception))

        def raising_request(credential, path, *, method="GET", body=None):
            raise RuntimeError("private_exception_value")

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(
            reader_module.NotionReadFailure
        ) as raised:
            reader_module.search(
                "personal",
                "roadmap",
                credential_sources=self.owner_sources(Path(tmp)),
                request=raising_request,
            )
        self.assertEqual(raised.exception.code, "notion_request_failed")
        self.assertIsNone(raised.exception.__cause__)

    def test_repeated_or_oversized_cursor_is_rejected(self) -> None:
        responses = iter(
            [
                list_payload([], has_more=True, next_cursor="same-cursor"),
                list_payload([], has_more=True, next_cursor="same-cursor"),
            ]
        )

        def request(credential, path, *, method="GET", body=None):
            return True, next(responses)

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(
            reader_module.NotionReadFailure
        ) as raised:
            reader_module.search(
                "personal",
                "roadmap",
                credential_sources=self.owner_sources(Path(tmp)),
                request=request,
            )
        self.assertEqual(raised.exception.code, "notion_response_cursor_invalid")

    def test_cli_shape_and_value_free_failure_output(self) -> None:
        args = reader_module.parse_args(
            [
                "--route",
                'company-alpha',
                "page",
                PAGE_ID,
                "--max-blocks",
                "10",
                "--max-depth",
                "3",
            ]
        )
        self.assertEqual(args.route, 'company-alpha')
        self.assertEqual(args.page_id, PAGE_ID)
        self.assertEqual(args.max_blocks, 10)
        self.assertEqual(args.max_depth, 3)

        query_args = reader_module.parse_args(
            [
                "--route",
                "personal",
                "query",
                DATA_SOURCE_ID,
                "--limit",
                "5",
            ]
        )
        self.assertEqual(query_args.route, "personal")
        self.assertEqual(query_args.data_source_id, DATA_SOURCE_ID)
        self.assertEqual(query_args.limit, 5)

        output = io.StringIO()
        with mock.patch.object(
            reader_module,
            "search",
            side_effect=reader_module.NotionReadFailure("notion_transport_failed"),
        ), redirect_stdout(output):
            status = reader_module.main(
                ["--route", "personal", "search", "--query", "roadmap"]
            )
        self.assertEqual(status, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"ok": False, "error": "notion_transport_failed"},
        )

        output = io.StringIO()
        with redirect_stdout(output):
            status = reader_module.main(
                [
                    "--route",
                    "personal",
                    "search",
                    "--query",
                    "private-query-value",
                    "--limit",
                    "not-an-integer",
                ]
            )
        self.assertEqual(status, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"ok": False, "error": "invalid_arguments"},
        )
        self.assertNotIn("private-query-value", output.getvalue())


if __name__ == "__main__":
    unittest.main()
