from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest import mock


from routing_test_support import fixture_root
ROOT = fixture_root()
SCRIPT = ROOT / "scripts" / 'personal_data_neon_readonly_capability_probe.py'
spec = importlib.util.spec_from_file_location(
    'personal_data_neon_readonly_capability_probe',
    SCRIPT,
)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


API_KEY = "neon-secret-api-key-value"
PASSWORD = "database-secret-password"
PROJECT_ID = "quiet-field-12345678"
BRANCH_ID = "br-production-123456"
ENDPOINT_ID = "ep-production-123456"
ENDPOINT_HOST = f"{ENDPOINT_ID}.eu-central-1.aws.neon.tech"
CONNECTION_HOST = f"{ENDPOINT_ID}-pooler.eu-central-1.aws.neon.tech"
DATABASE = 'personal-data-project'
ROLE = "topology_owner"
DSN = (
    f"postgresql://{ROLE}:{PASSWORD}@{CONNECTION_HOST}:5432/{DATABASE}"
    "?channel_binding=require&sslmode=verify-full&sslrootcert=system"
)


class FakeCursor:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row
        self.queries: list[str] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str) -> None:
        self.queries.append(query)

    def fetchone(self) -> tuple[object, ...]:
        return self.row


class FakeConnection:
    def __init__(self, row: tuple[object, ...] | None = None) -> None:
        self.info = SimpleNamespace(
            host=ENDPOINT_HOST,
            port=5432,
            dbname=DATABASE,
            user=ROLE,
        )
        self.pgconn = SimpleNamespace(ssl_in_use=True)
        self.cursor_value = FakeCursor(
            row
            or (
                DATABASE,
                ROLE,
                "on",
                PROJECT_ID,
                BRANCH_ID,
                ENDPOINT_ID,
                False,
            )
        )
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_value

    def close(self) -> None:
        self.closed = True


class Fixture:
    def __init__(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name) / "Workspace"
        self.secrets = self.root / "secrets"
        self.secrets.mkdir(parents=True)
        self.secrets.chmod(0o700)
        self.health_env = self.secrets / "health.env"
        self.attestation = (
            self.secrets / "topology_financial_neon_target_attestation.json"
        )
        self.health_env.write_text(
            "\n".join(
                (
                    f"HEALTH_DB_DSN={DSN}",
                    f"TOPOLOGY_DB_DSN={DSN}",
                    f"NEON_API_KEY={API_KEY}",
                    f"NEON_PROJECT_ID={PROJECT_ID}",
                    "UNRELATED_VALUE=preserved",
                    "",
                )
            ),
            encoding="utf-8",
        )
        self.health_env.chmod(0o600)
        payload = {
            "schema": probe.ATTESTATION_SCHEMA,
            "project_id": PROJECT_ID,
            "branch_id": BRANCH_ID,
            "endpoint_id": ENDPOINT_ID,
            "endpoint_host": ENDPOINT_HOST,
            "connection_host": CONNECTION_HOST,
            "pooler": True,
            "database": DATABASE,
            "role": ROLE,
            "port": 5432,
            "tls_required": True,
            "generated_at": "2026-08-08T12:00:00Z",
        }
        payload["attestation_sha256"] = probe._attestation_identity_sha256(payload)
        self.attestation.write_text(json.dumps(payload), encoding="utf-8")
        self.attestation.chmod(0o600)

    def close(self) -> None:
        self.temp.cleanup()

    def constants(self):
        return mock.patch.multiple(
            probe,
            WORKSPACE_ROOT=self.root,
            EXPECTED_WORKSPACE_ROOT=self.root,
            SECRETS_DIR=self.secrets,
            HEALTH_ENV=self.health_env,
            ATTESTATION=self.attestation,
        )


def provider_payload(path: str) -> dict[str, object]:
    if path == "auth":
        return {"account_id": "account-id-not-emitted", "auth_method": "api_key_user"}
    if path == f"projects/{PROJECT_ID}":
        return {"project": {"id": PROJECT_ID}}
    if path == f"projects/{PROJECT_ID}/endpoints":
        return {
            "endpoints": [
                {
                    "id": ENDPOINT_ID,
                    "project_id": PROJECT_ID,
                    "branch_id": BRANCH_ID,
                    "host": ENDPOINT_HOST,
                }
            ]
        }
    raise AssertionError(f"unexpected path: {path}")


class PersonalDataProjectNeonReadonlyCapabilityProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_ready_probe_is_get_only_read_only_target_bound_and_secret_silent(self) -> None:
        api_calls: list[tuple[str, str]] = []
        connection = FakeConnection()

        def api_getter(api_key: str, path: str):
            self.assertEqual(api_key, API_KEY)
            api_calls.append(("GET", path))
            return provider_payload(path)

        def connector(dsn: str, attestation):
            self.assertEqual(dsn, DSN)
            self.assertEqual(attestation.endpoint_host, ENDPOINT_HOST)
            return connection

        with self.fixture.constants():
            code, output = probe.run_probe(
                api_getter=api_getter,
                connector=connector,
            )

        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "ready")
        self.assertEqual(output["capability_scope"], "read_only")
        self.assertFalse(output["mutating"])
        self.assertEqual(
            api_calls,
            [
                ("GET", "auth"),
                ("GET", f"projects/{PROJECT_ID}"),
                ("GET", f"projects/{PROJECT_ID}/endpoints"),
            ],
        )
        self.assertEqual(connection.cursor_value.queries, [probe.IDENTITY_QUERY])
        query = connection.cursor_value.queries[0].upper().lstrip()
        self.assertTrue(query.startswith("SELECT"))
        for forbidden in ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"):
            self.assertNotIn(forbidden, query)
        self.assertTrue(output["postgres"]["transaction_read_only"])
        self.assertTrue(output["postgres"]["database_identity_verified"])
        self.assertTrue(output["postgres"]["role_identity_verified"])
        self.assertTrue(output["postgres"]["endpoint_identity_verified"])
        self.assertTrue(output["postgres"]["tls_verified"])
        self.assertFalse(output["postgres"]["pg_stat_ssl_reported"])
        self.assertTrue(connection.closed)

        rendered = json.dumps(output, sort_keys=True)
        for secret in (API_KEY, PASSWORD, DSN, "account-id-not-emitted"):
            self.assertNotIn(secret, rendered)

    def test_connect_read_only_sets_session_guard_before_first_query(self) -> None:
        sentinel = object()
        with (
            mock.patch.object(probe, "_validate_host_ca_bundle"),
            mock.patch.object(probe.psycopg, "connect", return_value=sentinel) as connect,
        ):
            attestation = probe.Attestation(
                attestation_sha256="0" * 64,
                project_id=PROJECT_ID,
                branch_id=BRANCH_ID,
                endpoint_id=ENDPOINT_ID,
                endpoint_host=ENDPOINT_HOST,
                connection_host=CONNECTION_HOST,
                database=DATABASE,
                role=ROLE,
                port=5432,
            )
            result = probe.connect_read_only(DSN, attestation)

        self.assertIs(result, sentinel)
        args, kwargs = connect.call_args
        self.assertEqual(len(args), 1)
        self.assertNotEqual(args[0], DSN)
        self.assertIn(f"@{ENDPOINT_HOST}:5432/", args[0])
        self.assertNotIn(CONNECTION_HOST, args[0])
        self.assertTrue(kwargs["autocommit"])
        self.assertIn("default_transaction_read_only=on", kwargs["options"])
        self.assertEqual(kwargs["connect_timeout"], 20)

    def test_neon_http_helper_constructs_get_and_does_not_follow_redirects(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return b'{"account_id":"opaque","auth_method":"api_key_user"}'

        observed: list[object] = []

        def open_request(request, timeout: int):
            observed.extend((request, timeout))
            return FakeResponse()

        with mock.patch.object(probe.API_OPENER, "open", side_effect=open_request):
            payload = probe.neon_get(API_KEY, "auth")

        request = observed[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(observed[1], 20)
        self.assertEqual(payload["auth_method"], "api_key_user")

    def test_wrong_private_file_mode_fails_before_network_without_secret_echo(self) -> None:
        self.fixture.health_env.chmod(0o644)
        api_getter = mock.Mock()
        connector = mock.Mock()
        with self.fixture.constants():
            code, output = probe.run_probe(
                api_getter=api_getter,
                connector=connector,
            )

        self.assertEqual(code, 2)
        self.assertEqual(output["reason_code"], "private_file_custody_invalid")
        api_getter.assert_not_called()
        connector.assert_not_called()
        rendered = json.dumps(output)
        self.assertNotIn(PASSWORD, rendered)
        self.assertNotIn(API_KEY, rendered)

    def test_symlinked_private_file_is_rejected(self) -> None:
        target = self.fixture.secrets / "health-target.env"
        target.write_text(self.fixture.health_env.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o600)
        self.fixture.health_env.unlink()
        os.symlink(target.name, self.fixture.health_env)

        with self.fixture.constants():
            code, output = probe.run_probe(
                api_getter=mock.Mock(),
                connector=mock.Mock(),
            )

        self.assertEqual(code, 2)
        self.assertEqual(output["reason_code"], "private_file_type_invalid")

    def test_dsn_target_mismatch_fails_before_network_and_never_echoes_dsn(self) -> None:
        wrong_dsn = DSN.replace(CONNECTION_HOST, "wrong.neon.tech")
        self.fixture.health_env.write_text(
            "\n".join(
                (
                    f"HEALTH_DB_DSN={wrong_dsn}",
                    f"TOPOLOGY_DB_DSN={wrong_dsn}",
                    f"NEON_API_KEY={API_KEY}",
                    f"NEON_PROJECT_ID={PROJECT_ID}",
                )
            ),
            encoding="utf-8",
        )
        self.fixture.health_env.chmod(0o600)
        api_getter = mock.Mock()
        connector = mock.Mock()

        with self.fixture.constants():
            code, output = probe.run_probe(
                api_getter=api_getter,
                connector=connector,
            )

        self.assertEqual(code, 2)
        self.assertEqual(output["reason_code"], "database_dsn_attestation_mismatch")
        api_getter.assert_not_called()
        connector.assert_not_called()
        self.assertNotIn(wrong_dsn, json.dumps(output))

    def test_provider_endpoint_mismatch_fails_closed_before_postgres(self) -> None:
        def api_getter(_api_key: str, path: str):
            payload = provider_payload(path)
            if path.endswith("/endpoints"):
                payload = {"endpoints": []}
            return payload

        connector = mock.Mock()
        with self.fixture.constants():
            code, output = probe.run_probe(
                api_getter=api_getter,
                connector=connector,
            )

        self.assertEqual(code, 2)
        self.assertEqual(output["reason_code"], "neon_endpoint_identity_mismatch")
        connector.assert_not_called()

    def test_postgres_identity_mismatch_fails_closed_and_closes_connection(self) -> None:
        connection = FakeConnection(
            row=(
                "wrong_database",
                ROLE,
                "on",
                PROJECT_ID,
                BRANCH_ID,
                ENDPOINT_ID,
                True,
            )
        )
        with self.fixture.constants():
            code, output = probe.run_probe(
                api_getter=lambda _key, path: provider_payload(path),
                connector=lambda _dsn, _attestation: connection,
            )

        self.assertEqual(code, 2)
        self.assertEqual(output["reason_code"], "postgres_attested_identity_mismatch")
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
