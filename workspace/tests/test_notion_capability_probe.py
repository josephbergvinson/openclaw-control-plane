from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import copy
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


from routing_test_support import fixture_root
ROOT = fixture_root()
SCRIPT = ROOT / "scripts" / "notion_capability_probe.py"
SPEC = importlib.util.spec_from_file_location("notion_capability_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe_module)


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str,
        status: int = 200,
        content_type: str = "application/json",
        content_length: str | None = None,
    ) -> None:
        self._body = io.BytesIO(body)
        self._url = url
        self.status = status
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, amount: int) -> bytes:
        return self._body.read(amount)


class FakeOpener:
    def __init__(self, response=None, *, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request, *, timeout: int):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class NotionTransportTests(unittest.TestCase):
    def test_post_is_exact_origin_bounded_json_with_required_headers(self) -> None:
        url = "https://api.notion.com/v1/search"
        opener = FakeOpener(FakeResponse(b'{"object":"list"}', url=url))

        ok, payload = probe_module.notion_request(
            "private-token",
            "/v1/search",
            method="POST",
            body={"query": "Roadmap", "page_size": 5},
            opener=opener,
        )

        self.assertTrue(ok)
        self.assertEqual(payload, {"object": "list"})
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, url)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.data,
            b'{"page_size":5,"query":"Roadmap"}',
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
        self.assertEqual(
            request.get_header("Notion-version"),
            probe_module.NOTION_VERSION,
        )
        self.assertEqual(timeout, probe_module.NOTION_REQUEST_TIMEOUT_SECONDS)

    def test_redirect_and_cross_origin_paths_are_rejected_without_following(self) -> None:
        redirected = FakeOpener(
            FakeResponse(
                b'{"object":"list"}',
                url="https://example.invalid/private",
            )
        )
        ok, payload = probe_module.notion_request(
            "private-token",
            "/v1/search",
            opener=redirected,
        )
        self.assertFalse(ok)
        self.assertEqual(payload, {"error": "notion_redirect_rejected"})

        unopened = FakeOpener()
        ok, payload = probe_module.notion_request(
            "private-token",
            "https://example.invalid/v1/search",
            opener=unopened,
        )
        self.assertFalse(ok)
        self.assertEqual(payload, {"error": "notion_request_path_invalid"})
        self.assertEqual(unopened.calls, [])
        self.assertIsNone(
            probe_module._RejectRedirects().redirect_request(
                mock.Mock(),
                mock.Mock(),
                302,
                "redirect",
                mock.Mock(),
                "https://example.invalid/private",
            )
        )

    def test_ambiguous_or_oversized_json_is_rejected(self) -> None:
        url = "https://api.notion.com/v1/search"
        cases = (
            (
                "duplicate-key",
                FakeResponse(b'{"object":"list","object":"page"}', url=url),
                "notion_response_json_invalid",
            ),
            (
                "non-object",
                FakeResponse(b"[]", url=url),
                "notion_response_shape_invalid",
            ),
            (
                "declared-too-large",
                FakeResponse(
                    b"{}",
                    url=url,
                    content_length=str(probe_module.MAX_NOTION_RESPONSE_BYTES + 1),
                ),
                "notion_response_too_large",
            ),
        )
        for name, response, expected in cases:
            with self.subTest(name=name):
                ok, payload = probe_module.notion_request(
                    "private-token",
                    "/v1/search",
                    opener=FakeOpener(response),
                )
                self.assertFalse(ok)
                self.assertEqual(payload, {"error": expected})

        with mock.patch.object(probe_module, "MAX_NOTION_RESPONSE_BYTES", 4):
            ok, payload = probe_module.notion_request(
                "private-token",
                "/v1/search",
                opener=FakeOpener(FakeResponse(b"12345", url=url)),
            )
        self.assertFalse(ok)
        self.assertEqual(payload, {"error": "notion_response_too_large"})

        unopened = FakeOpener()
        with mock.patch.object(probe_module, "MAX_NOTION_REQUEST_BYTES", 4):
            ok, payload = probe_module.notion_request(
                "private-token",
                "/v1/search",
                method="POST",
                body={"query": "long"},
                opener=unopened,
            )
        self.assertFalse(ok)
        self.assertEqual(payload, {"error": "notion_request_too_large"})
        self.assertEqual(unopened.calls, [])

    def test_http_failure_does_not_expose_provider_body_or_credential(self) -> None:
        url = "https://api.notion.com/v1/search"
        failure = urllib.error.HTTPError(
            url,
            401,
            "private-provider-message",
            {},
            io.BytesIO(b'{"message":"private-body-value"}'),
        )

        ok, payload = probe_module.notion_request(
            "private-token",
            "/v1/search",
            opener=FakeOpener(error=failure),
        )

        self.assertFalse(ok)
        self.assertEqual(payload, {"error": "notion_http_error", "status": 401})
        encoded = json.dumps(payload)
        self.assertNotIn("private-provider-message", encoded)
        self.assertNotIn("private-body-value", encoded)
        self.assertNotIn("private-token", encoded)


class NotionCredentialHandleTests(unittest.TestCase):
    def make_source(
        self,
        path: Path,
        *,
        route: str = "personal",
    ):
        if route == "personal":
            return probe_module.CredentialSource(
                "notion.personal.api",
                "workspace-secret-file",
                path,
                "NOTION_API_KEY",
            )
        return probe_module.CredentialSource(
            'notion.company_alpha.api',
            "runtime-secret-file",
            path,
            'NOTION_API_KEY_COMPANY_ALPHA',
        )

    def write_owner(self, path: Path, text: str, *, mode: int = 0o600) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)

    def test_registered_routes_resolve_to_exact_typed_owners(self) -> None:
        self.assertEqual(
            probe_module.load_registered_credential_sources(),
            {
                "personal": probe_module.CredentialSource(
                    "notion.personal.api",
                    "workspace-secret-file",
                    Path(
                        _operator_binding('paths.routing_notion_env')
                    ),
                    "NOTION_API_KEY",
                ),
                'company-alpha': probe_module.CredentialSource(
                    'notion.company_alpha.api',
                    "runtime-secret-file",
                    Path(
                        _operator_binding('paths.gog_env_file')
                    ),
                    'NOTION_API_KEY_COMPANY_ALPHA',
                ),
            },
        )

    def test_probe_loads_selected_owner_and_ignores_inherited_env(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as tmp:
            owner = Path(tmp) / "notion.env"
            self.write_owner(owner, "NOTION_API_KEY=owner-token-value\n")
            sources = {"personal": self.make_source(owner)}
            with mock.patch.dict(
                os.environ,
                {"NOTION_API_KEY": "inherited-token-value"},
                clear=True,
            ), mock.patch.object(
                probe_module,
                "notion_request",
                return_value=(
                    True,
                    {
                        "name": "Personal bot",
                        "bot": {
                            "owner": {"type": "workspace"},
                            "workspace_name": "Personal workspace",
                            "workspace_id": "11111111-1111-1111-1111-111111111111",
                        },
                    },
                ),
            ) as request:
                ok, result = probe_module.probe_route(
                    "personal",
                    credential_sources=sources,
                )

        self.assertTrue(ok)
        self.assertEqual(result["credential_handle_id"], "notion.personal.api")
        self.assertTrue(result["credential_owner_bound"])
        self.assertNotIn("env", result)
        self.assertNotIn("NOTION_API_KEY", json.dumps(result))
        self.assertNotIn("owner-token-value", json.dumps(result))
        self.assertNotIn("inherited-token-value", json.dumps(result))
        self.assertEqual(request.call_args.args[0], "owner-token-value")

    def test_missing_declared_key_is_value_free_and_does_not_call_provider(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as tmp:
            owner = Path(tmp) / "runtime.env"
            self.write_owner(owner, "UNRELATED_KEY=private-value\n")
            with mock.patch.object(probe_module, "notion_request") as request:
                ok, result = probe_module.probe_route(
                    'company-alpha',
                    credential_sources={
                        'company-alpha': self.make_source(
                            owner,
                            route='company-alpha',
                        )
                    },
                )

        self.assertFalse(ok)
        self.assertEqual(
            result,
            {
                "route": 'company-alpha',
                "credential_handle_id": 'notion.company_alpha.api',
                "credential_owner_bound": False,
                "ok": False,
                "error": "credential_owner_key_missing",
            },
        )
        self.assertNotIn("private-value", json.dumps(result))
        request.assert_not_called()

    def test_unsafe_owner_files_fail_closed_before_provider_access(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as tmp:
            root = Path(tmp)
            wrong_mode = root / "wrong-mode.env"
            self.write_owner(wrong_mode, "NOTION_API_KEY=secret\n", mode=0o644)

            linked = root / "linked.env"
            self.write_owner(linked, "NOTION_API_KEY=secret\n")
            os.link(linked, root / "second-link.env")

            symlink = root / "symlink.env"
            symlink.symlink_to(wrong_mode)

            duplicate = root / "duplicate.env"
            self.write_owner(
                duplicate,
                "NOTION_API_KEY=first\nNOTION_API_KEY=second\n",
            )

            for name, path, expected in (
                ("wrong-mode", wrong_mode, "credential_owner_mode_invalid"),
                ("multiple-links", linked, "credential_owner_link_count_invalid"),
                ("symlink", symlink, "credential_owner_not_regular"),
                ("duplicate-key", duplicate, "credential_owner_env_duplicate_key"),
            ):
                with self.subTest(name=name), mock.patch.object(
                    probe_module,
                    "notion_request",
                ) as request:
                    ok, result = probe_module.probe_route(
                        "personal",
                        credential_sources={"personal": self.make_source(path)},
                    )
                self.assertFalse(ok)
                self.assertFalse(result["credential_owner_bound"])
                self.assertEqual(result["error"], expected)
                self.assertNotIn("secret", json.dumps(result))
                request.assert_not_called()

    def test_missing_duplicate_and_wrong_shape_handles_fail_closed(self) -> None:
        registry = json.loads(
            (ROOT / "registry" / "integration_routes.json").read_text(
                encoding="utf-8"
            )
        )
        personal_consumer = {
            "kind": "integration-route",
            "consumer_id": "notion-personal",
        }

        missing = copy.deepcopy(registry)
        handle = next(
            row
            for row in missing["credential_handles"]
            if row["handle_id"] == "notion.personal.api"
        )
        handle["consumers"] = [
            consumer
            for consumer in handle["consumers"]
            if consumer != personal_consumer
        ]

        duplicate = copy.deepcopy(registry)
        duplicate_handle = copy.deepcopy(
            next(
                row
                for row in duplicate["credential_handles"]
                if row["handle_id"] == "notion.personal.api"
            )
        )
        duplicate_handle["handle_id"] = "notion.personal.api-copy"
        duplicate["credential_handles"].append(duplicate_handle)

        wrong_shape = copy.deepcopy(registry)
        handle = next(
            row
            for row in wrong_shape["credential_handles"]
            if row["handle_id"] == "notion.personal.api"
        )
        handle["credential_kind"] = "api-credential-bundle"

        wrong_handle = copy.deepcopy(registry)
        handle = next(
            row
            for row in wrong_handle["credential_handles"]
            if row["handle_id"] == "notion.personal.api"
        )
        handle["handle_id"] = "notion.personal.unexpected"

        for name, payload, expected in (
            ("missing", missing, "registry_credential_handle_not_unique"),
            ("duplicate", duplicate, "registry_credential_handle_not_unique"),
            ("wrong-shape", wrong_shape, "registry_credential_binding_invalid"),
            ("wrong-handle", wrong_handle, "registry_credential_binding_invalid"),
        ):
            with self.subTest(name=name), mock.patch.object(
                probe_module,
                "load_json_strict",
                return_value=payload,
            ), self.assertRaises(probe_module.ProbeFailure) as raised:
                probe_module.load_registered_credential_sources()
            self.assertEqual(raised.exception.code, expected)


if __name__ == "__main__":
    unittest.main()
