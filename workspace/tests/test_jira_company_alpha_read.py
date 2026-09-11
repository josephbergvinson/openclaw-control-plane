from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import hashlib
import importlib.util
import io
import json
import subprocess
import urllib.parse
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from routing_test_support import fixture_root
ROOT = fixture_root()
SCRIPT = ROOT / "scripts" / 'jira_company_alpha_read.py'
SPEC = importlib.util.spec_from_file_location('jira_company_alpha_read', SCRIPT)
assert SPEC is not None and SPEC.loader is not None
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


ACCOUNT_ID = "registered-provider-account"


def route_loader():
    digest = hashlib.sha256(ACCOUNT_ID.encode("utf-8")).hexdigest()
    return {}, digest, reader.DEFAULT_ENV_FILE


def credential_loader(_path: Path) -> dict[str, str]:
    return {
        "ATLASSIAN_SITE_URL": reader.EXPECTED_SITE,
        "ATLASSIAN_EMAIL": "private@example.invalid",
        "ATLASSIAN_API_TOKEN": "private-token",
    }


def adf(text: str) -> dict[str, object]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def issue_payload(key: str, *, summary: str | None = None) -> dict[str, object]:
    number = key.split("-", 1)[1]
    return {
        "id": f"10{number}",
        "key": key,
        "self": f"https://company_alpha.atlassian.net/rest/api/3/issue/{key}",
        "expand": "renderedFields",
        "fields": {
            "summary": summary or f"Summary for {key}",
            "description": adf(f"Description for {key}"),
            "issuetype": {
                "id": "10001",
                "name": "Task",
                "self": 'https://jira.example.invalid/rest/api/3/issuetype/10001',
                "iconUrl": "https://cdn.example.invalid/issuetype.png",
            },
            "status": {
                "id": "10002",
                "name": "In Progress",
                "self": 'https://jira.example.invalid/rest/api/3/status/10002',
                "statusCategory": {"id": 4, "name": "In Progress"},
            },
            "priority": {
                "id": "10003",
                "name": "High",
                "self": 'https://jira.example.invalid/rest/api/3/priority/10003',
                "iconUrl": "https://cdn.example.invalid/priority.svg",
            },
            "assignee": {
                "accountId": "assignee-1",
                "displayName": "Assigned Person",
                "active": True,
                "emailAddress": "ignored@example.invalid",
                "self": 'https://jira.example.invalid/rest/api/3/user/assignee-1',
                "avatarUrls": {
                    "48x48": "https://avatar.example.invalid/assignee-1.png"
                },
            },
            "reporter": {
                "accountId": "reporter-1",
                "displayName": "Reporting Person",
                "active": True,
            },
            "labels": ["tokenomics", "current"],
            "components": [{"id": "10004", "name": "Docs"}],
            "fixVersions": [{"id": "10005", "name": "Next"}],
            "resolution": None,
            "created": "2026-09-01T10:00:00.000+0000",
            "updated": "2026-09-02T10:00:00.000+0000",
            "duedate": "2026-09-10",
            "parent": None,
            "customfield_12345": {
                "type": "project-specific-record",
                "value": f"Custom context for {key}",
            },
            "secretProviderExtension": "ignored",
        },
        "providerSecret": "ignored",
    }


def comment_payload(comment_id: str, text: str) -> dict[str, object]:
    return {
        "id": comment_id,
        "author": {
            "accountId": f"author-{comment_id}",
            "displayName": f"Author {comment_id}",
            "active": True,
            "emailAddress": "ignored@example.invalid",
        },
        "body": adf(text),
        "created": "2026-09-02T11:00:00.000+0000",
        "updated": "2026-09-02T11:01:00.000+0000",
        "rawProviderField": "ignored",
    }


class JiraSearchReaderTests(unittest.TestCase):
    def test_search_is_account_bound_project_confined_and_updated_desc(self) -> None:
        calls: list[tuple[str, str, str, str]] = []

        def requester(site: str, email: str, token: str, path: str):
            calls.append((site, email, token, path))
            if path == "/rest/api/3/myself":
                return True, {"accountId": ACCOUNT_ID, "displayName": "ignored"}
            return True, {
                "issues": [issue_payload('ALPHA-42')],
                "isLast": True,
                "providerSecret": "ignored",
            }

        result = reader.search_issues(
            'roadmap "escape" OR project = COMPANY_BETA',
            limit=7,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["project_key"], _operator_binding('services.jira.project_key'))
        self.assertEqual(result["ordering"], "updated-desc")
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            result["issues"],
            [
                {
                    "key": 'ALPHA-42',
                    "summary": 'Summary for ALPHA-42',
                    "issue_type": "Task",
                    "status": "In Progress",
                    "priority": "High",
                    "assignee": "Assigned Person",
                    "labels": ["tokenomics", "current"],
                    "updated": "2026-09-02T10:00:00.000+0000",
                }
            ],
        )
        rendered = json.dumps(result)
        for provider_field in (
            "fields",
            "self",
            "iconUrl",
            "avatarUrls",
            "statusCategory",
            "accountId",
            "emailAddress",
            "description",
            "customfield_12345",
            "providerSecret",
        ):
            with self.subTest(provider_field=provider_field):
                self.assertNotIn(provider_field, rendered)

        self.assertEqual(calls[0][3], "/rest/api/3/myself")
        parsed = urllib.parse.urlsplit(calls[1][3])
        self.assertEqual(parsed.path, "/rest/api/3/search/jql")
        params = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        self.assertEqual(
            params,
            {
                "jql": [
                    'project = ALPHA AND text ~ "roadmap \\"escape\\" OR project = COMPANY_BETA" ORDER BY updated DESC'
                ],
                "maxResults": ["7"],
                "fields": [",".join(reader.SEARCH_FIELDS)],
            },
        )
        self.assertIn("%20", calls[1][3])
        self.assertNotIn("+", calls[1][3])
        self.assertNotIn("private", calls[1][3])

    def test_search_defaults_to_ten_but_accepts_an_explicit_bounded_limit(
        self,
    ) -> None:
        search_paths: list[str] = []

        def requester(_site: str, _email: str, _token: str, path: str):
            if path == "/rest/api/3/myself":
                return True, {"accountId": ACCOUNT_ID}
            search_paths.append(path)
            return True, {"issues": [], "isLast": True}

        reader.search_issues(
            "current",
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )
        reader.search_issues(
            "current",
            limit=17,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        limits = [
            urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["maxResults"][0]
            for path in search_paths
        ]
        self.assertEqual(reader.DEFAULT_SEARCH_LIMIT, 10)
        self.assertEqual(limits, ["10", "17"])

    def test_search_preserves_nullable_scalar_summary_fields(self) -> None:
        issue = issue_payload('ALPHA-9')
        issue["fields"]["priority"] = None
        issue["fields"]["assignee"] = None

        def requester(_site: str, _email: str, _token: str, path: str):
            if path == "/rest/api/3/myself":
                return True, {"accountId": ACCOUNT_ID}
            return True, {"issues": [issue], "isLast": True}

        result = reader.search_issues(
            "current",
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertIsNone(result["issues"][0]["priority"])
        self.assertIsNone(result["issues"][0]["assignee"])

    def test_search_paginates_opaque_tokens_and_stops_at_requested_limit(self) -> None:
        search_paths: list[str] = []

        def requester(_site: str, _email: str, _token: str, path: str):
            if path == "/rest/api/3/myself":
                return True, {"accountId": ACCOUNT_ID}
            search_paths.append(path)
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            if "nextPageToken" not in params:
                return True, {
                    "issues": [issue_payload('ALPHA-1')],
                    "nextPageToken": "opaque token/?&=",
                    "isLast": False,
                }
            self.assertEqual(params["nextPageToken"], ["opaque token/?&="])
            return True, {
                "issues": [issue_payload('ALPHA-2')],
                "isLast": True,
            }

        result = reader.search_issues(
            "tokenomics",
            limit=2,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertEqual(
            [item["key"] for item in result["issues"]],
            ['ALPHA-1', 'ALPHA-2'],
        )
        self.assertEqual(len(search_paths), 2)
        self.assertIn("nextPageToken=opaque%20token%2F%3F%26%3D", search_paths[1])
        self.assertIn("maxResults=2", search_paths[0])
        self.assertIn("maxResults=1", search_paths[1])

    def test_search_rejects_cross_project_duplicate_and_stalled_pages(self) -> None:
        cases = (
            (
                "cross-project",
                [{"issues": [issue_payload("OTHER-1")], "isLast": True}],
                "issue_key_invalid",
            ),
            (
                "duplicate",
                [
                    {
                        "issues": [issue_payload('ALPHA-1')],
                        "nextPageToken": "next",
                        "isLast": False,
                    },
                    {"issues": [issue_payload('ALPHA-1')], "isLast": True},
                ],
                "provider_pagination_invalid",
            ),
            (
                "stalled",
                [
                    {
                        "issues": [],
                        "nextPageToken": "next",
                        "isLast": False,
                    }
                ],
                "provider_pagination_invalid",
            ),
        )
        for name, pages, expected in cases:
            page_iterator = iter(pages)

            def requester(_site: str, _email: str, _token: str, path: str):
                if path == "/rest/api/3/myself":
                    return True, {"accountId": ACCOUNT_ID}
                return True, next(page_iterator)

            with self.subTest(name=name), self.assertRaises(
                reader.ReaderFailure
            ) as raised:
                reader.search_issues(
                    "current",
                    limit=2,
                    route_loader=route_loader,
                    credential_loader=credential_loader,
                    requester=requester,
                )
            self.assertEqual(raised.exception.code, expected)

    def test_search_rejects_injection_prone_or_unbounded_inputs_before_credentials(
        self,
    ) -> None:
        route = mock.Mock(side_effect=AssertionError("must not load credentials"))
        cases = (
            ("", 1, "search_text_invalid"),
            (" leading", 1, "search_text_invalid"),
            ("line\nbreak", 1, "search_text_invalid"),
            ("x" * (reader.MAX_SEARCH_TEXT_BYTES + 1), 1, "search_text_invalid"),
            ("valid", 0, "limit_invalid"),
            ("valid", reader.MAX_SEARCH_LIMIT + 1, "limit_invalid"),
        )
        for text, limit, expected in cases:
            with self.subTest(text=text[:12], limit=limit), self.assertRaises(
                reader.ReaderFailure
            ) as raised:
                reader.search_issues(text, limit=limit, route_loader=route)
            self.assertEqual(raised.exception.code, expected)
        route.assert_not_called()

    def test_account_mismatch_stops_before_project_content_read(self) -> None:
        calls: list[str] = []

        def requester(_site: str, _email: str, _token: str, path: str):
            calls.append(path)
            return True, {"accountId": "wrong-provider-account"}

        with self.assertRaises(reader.ReaderFailure) as raised:
            reader.search_issues(
                "current",
                route_loader=route_loader,
                credential_loader=credential_loader,
                requester=requester,
            )
        self.assertEqual(raised.exception.code, "provider_account_mismatch")
        self.assertEqual(calls, ["/rest/api/3/myself"])


class JiraIssueReaderTests(unittest.TestCase):
    def test_exact_issue_preserves_all_fields_and_reads_latest_comments(
        self,
    ) -> None:
        calls: list[str] = []
        expected_issue = issue_payload('ALPHA-123')

        def requester(_site: str, _email: str, _token: str, path: str):
            calls.append(path)
            if path == "/rest/api/3/myself":
                return True, {"accountId": ACCOUNT_ID}
            if path.startswith('/rest/api/3/issue/ALPHA-123?'):
                return True, expected_issue
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            start = int(params["startAt"][0])
            if start == 0:
                return True, {
                    "startAt": 0,
                    "maxResults": 2,
                    "total": 3,
                    "comments": [
                        comment_payload("3", "Newest comment"),
                        comment_payload("2", "Middle comment"),
                    ],
                }
            return True, {
                "startAt": 2,
                "maxResults": 1,
                "total": 3,
                "comments": [comment_payload("1", "Oldest comment")],
            }

        result = reader.read_issue(
            'ALPHA-123',
            comments_limit=3,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )

        self.assertEqual(result["issue"]["key"], 'ALPHA-123')
        self.assertEqual(result["issue"]["fields"], expected_issue["fields"])
        self.assertEqual(
            result["issue"]["fields"]["customfield_12345"],
            {
                "type": "project-specific-record",
                "value": 'Custom context for ALPHA-123',
            },
        )
        self.assertEqual(result["comments_ordering"], "created-desc")
        self.assertEqual(
            [comment["body"] for comment in result["comments"]],
            [adf("Newest comment"), adf("Middle comment"), adf("Oldest comment")],
        )
        issue_path = calls[1]
        self.assertEqual(
            urllib.parse.urlsplit(issue_path).path,
            '/rest/api/3/issue/ALPHA-123',
        )
        self.assertEqual(
            urllib.parse.parse_qs(urllib.parse.urlsplit(issue_path).query),
            {"fields": [reader.ALL_ISSUE_FIELDS]},
        )
        for path, start, maximum in ((calls[2], "0", "3"), (calls[3], "2", "1")):
            parsed = urllib.parse.urlsplit(path)
            self.assertEqual(parsed.path, '/rest/api/3/issue/ALPHA-123/comment')
            self.assertEqual(
                urllib.parse.parse_qs(parsed.query),
                {"startAt": [start], "maxResults": [maximum], "orderBy": ["-created"]},
            )

    def test_zero_comment_limit_skips_comment_endpoint(self) -> None:
        calls: list[str] = []

        def requester(_site: str, _email: str, _token: str, path: str):
            calls.append(path)
            if path == "/rest/api/3/myself":
                return True, {"accountId": ACCOUNT_ID}
            return True, issue_payload('ALPHA-5')

        result = reader.read_issue(
            'ALPHA-5',
            comments_limit=0,
            route_loader=route_loader,
            credential_loader=credential_loader,
            requester=requester,
        )
        self.assertEqual(result["comments"], [])
        self.assertEqual(len(calls), 2)

    def test_issue_rejects_noncanonical_keys_and_pagination(self) -> None:
        route = mock.Mock(side_effect=AssertionError("must not load credentials"))
        for key in (
            'ALPHA-0',
            'ALPHA-01',
            "sauce-1",
            'ALPHA-1/../OTHER-1',
            'ALPHA-1?fields=*',
            "OTHER-1",
        ):
            with self.subTest(key=key), self.assertRaises(reader.ReaderFailure):
                reader.read_issue(key, route_loader=route)
        route.assert_not_called()

        def requester(_site: str, _email: str, _token: str, path: str):
            if path == "/rest/api/3/myself":
                return True, {"accountId": ACCOUNT_ID}
            if path.startswith('/rest/api/3/issue/ALPHA-1?'):
                return True, issue_payload('ALPHA-1')
            return True, {
                "startAt": 1,
                "total": 2,
                "comments": [comment_payload("1", "comment")],
            }

        with self.assertRaises(reader.ReaderFailure) as raised:
            reader.read_issue(
                'ALPHA-1',
                comments_limit=2,
                route_loader=route_loader,
                credential_loader=credential_loader,
                requester=requester,
            )
        self.assertEqual(raised.exception.code, "provider_pagination_invalid")

    def test_selected_provider_content_is_copied_strictly_and_bounded(self) -> None:
        payload = {"body": adf("Current provider content")}
        self.assertEqual(reader._bounded_copy(payload), payload)
        oversized = {"body": "x" * (reader.MAX_ITEM_BYTES + 1)}
        with self.assertRaises(reader.ReaderFailure) as raised:
            reader._bounded_copy(oversized)
        self.assertEqual(raised.exception.code, "provider_item_too_large")


class JiraReaderFailureContractTests(unittest.TestCase):
    def test_registry_and_resolver_expose_only_the_bounded_read_operations(self) -> None:
        registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        route = next(
            row for row in registry["routes"] if row["route_id"] == reader.ROUTE_ID
        )
        self.assertEqual(
            route["operations"]["issue-search"],
            {"lane": "declared_native", "effect": "read"},
        )
        self.assertEqual(
            route["operations"]["issue-read"],
            {"lane": "declared_native", "effect": "read"},
        )
        handle = next(
            row
            for row in registry["credential_handles"]
            if row["handle_id"] == reader.jira_probe.CREDENTIAL_HANDLE_ID
        )
        self.assertIn(
            {"kind": "script", "consumer_id": 'scripts/jira_company_alpha_read.py'},
            handle["consumers"],
        )

        for operation in ("issue-search", "issue-read"):
            result = subprocess.run(
                [
                    "python3",
                    str(ROOT / "scripts" / "resolve_capability.py"),
                    "--system",
                    "jira",
                    "--intent",
                    "read",
                    "--context",
                    'company-alpha',
                    "--required-operation",
                    operation,
                    "--principal",
                    'company-alpha',
                    "--account",
                    _operator_binding('services.jira.hostname'),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(
                payload["preferred_lane"]["route_id"], reader.ROUTE_ID
            )
            self.assertEqual(
                payload["operation_contract"]["classification"], "native_supported"
            )

    def test_reader_reuses_redirect_refusing_bounded_strict_transport(self) -> None:
        self.assertIs(reader.request_json, reader.jira_probe.request_json)
        self.assertEqual(
            reader.MAX_PROVIDER_RESPONSE_BYTES,
            reader.jira_probe.MAX_PROVIDER_RESPONSE_BYTES,
        )
        self.assertTrue(
            any(
                isinstance(handler, reader.jira_probe.NoRedirectHandler)
                for handler in reader.jira_probe.NO_REDIRECT_OPENER.handlers
            )
        )

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit: int) -> bytes:
                return b'{"issues":[],"issues":[{"key":"ALPHA-1"}]}'

        with mock.patch.object(
            reader.jira_probe, "open_url_no_redirect", return_value=Response()
        ):
            ok, payload = reader.request_json(
                reader.EXPECTED_SITE,
                "private@example.invalid",
                "private-token",
                "/rest/api/3/search/jql?jql=project%20%3D%20ALPHA",
            )
        self.assertFalse(ok)
        self.assertEqual(payload, {"error": "invalid_json"})

        class OversizedResponse(Response):
            def read(self, _limit: int) -> bytes:
                return b"x" * (reader.MAX_PROVIDER_RESPONSE_BYTES + 1)

        with mock.patch.object(
            reader.jira_probe,
            "open_url_no_redirect",
            return_value=OversizedResponse(),
        ):
            ok, payload = reader.request_json(
                reader.EXPECTED_SITE,
                "private@example.invalid",
                "private-token",
                "/rest/api/3/search/jql?jql=project%20%3D%20ALPHA",
            )
        self.assertFalse(ok)
        self.assertEqual(payload, {"error": "response_too_large"})

    def test_provider_failures_never_reveal_secret_or_raw_error(self) -> None:
        secret = "super-secret-provider-body"

        def requester(_site: str, _email: str, _token: str, path: str):
            if path == "/rest/api/3/myself":
                return True, {"accountId": ACCOUNT_ID}
            return False, {
                "error": "http_error",
                "status": 403,
                "raw": secret,
                "errorMessages": [secret],
            }

        with self.assertRaises(reader.ReaderFailure) as raised:
            reader.search_issues(
                "current",
                route_loader=route_loader,
                credential_loader=credential_loader,
                requester=requester,
            )
        self.assertEqual(raised.exception.code, "provider_http_error")
        self.assertNotIn(secret, str(raised.exception))

        output = io.StringIO()
        with mock.patch.object(
            reader,
            "search_issues",
            side_effect=RuntimeError(secret),
        ), redirect_stdout(output):
            rc = reader.main(["search", "--text", "current"])
        self.assertEqual(rc, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "ok": False,
                "route": 'jira-company-alpha',
                "operation": "search",
                "error": "internal_error",
            },
        )
        self.assertNotIn(secret, output.getvalue())

    def test_cli_contract_is_strict_and_value_free(self) -> None:
        args = reader.parse_args(["search", "--text", "current", "--limit", "3"])
        self.assertEqual(
            (args.operation, args.text, args.limit),
            ("search", "current", 3),
        )
        args = reader.parse_args(["issue", 'ALPHA-7', "--comments-limit", "0"])
        self.assertEqual(
            (args.operation, args.key, args.comments_limit),
            ("issue", 'ALPHA-7', 0),
        )

        for argv in (
            [],
            ["search", "--text", "private-input", "--limit", "0"],
            ["issue", "OTHER-1"],
            ["issue", 'ALPHA-1', "--comments-limit", "101"],
            ["search", "--text", "current", "--unknown", "private-input"],
        ):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(reader.ReaderFailure):
                    reader.parse_args(argv)

    def test_output_is_bounded_without_echoing_oversized_content(self) -> None:
        secret = "private-provider-content"
        oversized = {
            "ok": True,
            "issues": [secret + ("x" * reader.MAX_OUTPUT_BYTES)],
        }
        with mock.patch.object(reader, "search_issues", return_value=oversized):
            output = io.StringIO()
            with redirect_stdout(output):
                rc = reader.main(["search", "--text", "current"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(output.getvalue())["error"], "output_too_large")
        self.assertNotIn(secret, output.getvalue())


if __name__ == "__main__":
    unittest.main()
