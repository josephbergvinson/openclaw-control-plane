#!/usr/bin/env python3
'Secret-silent, target-attested read-only Neon/Postgres capability probe.\n\nThe probe has no mutation code path. It reads the fixed owner-private PersonalDataProject\ncredential and attestation files, performs only Neon GET requests, and opens a\nPostgres session with ``default_transaction_read_only=on`` before issuing one\nidentity-only SELECT. Secret values and DSNs are never accepted as arguments or\nincluded in output or errors.\n'

from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Callable, Iterator, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

import psycopg


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_WORKSPACE_ROOT = Path(_operator_binding('paths.workspace'))
SECRETS_DIR = WORKSPACE_ROOT / "secrets"
HEALTH_ENV = SECRETS_DIR / "health.env"
ATTESTATION = SECRETS_DIR / "topology_financial_neon_target_attestation.json"
HOST_CA_BUNDLE = Path("/etc/ssl/cert.pem")
API_ROOT = "https://console.neon.tech/api/v2"
ROUTE_ID = 'personal-data-neon-postgres-readonly'
ATTESTATION_SCHEMA = "topology.financial_neon_target_attestation.v1"
OUTPUT_SCHEMA = 'openclaw.personal-data-neon-postgres-readonly-probe.v1'
MAX_PRIVATE_FILE_BYTES = 1_000_000
MAX_API_BYTES = 2_000_000

PROJECT_ID_RE = re.compile(r"^[a-z0-9-]{1,60}$")
BRANCH_ID_RE = re.compile(r"^br-[A-Za-z0-9-]{3,128}$")
ENDPOINT_ID_RE = re.compile(r"^ep-[A-Za-z0-9-]{3,128}$")
DATABASE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+\.neon\.tech$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

IDENTITY_QUERY = """
SELECT
    current_database(),
    current_user,
    current_setting('transaction_read_only'),
    current_setting('neon.project_id', true),
    current_setting('neon.branch_id', true),
    current_setting('neon.endpoint_id', true),
    COALESCE((
        SELECT ssl
        FROM pg_stat_ssl
        WHERE pid = pg_backend_pid()
    ), false)
""".strip()


class ProbeError(RuntimeError):
    """Stable, value-free probe failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a bearer token away from the fixed Neon API origin."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


API_OPENER = urllib.request.build_opener(_NoRedirectHandler())


@dataclass(frozen=True)
class PrivateDocument:
    payload: bytes = field(repr=False)
    mode: int
    uid: int
    dev: int
    ino: int
    nlink: int


@dataclass(frozen=True)
class Attestation:
    attestation_sha256: str
    project_id: str
    branch_id: str
    endpoint_id: str
    endpoint_host: str
    connection_host: str
    database: str
    role: str
    port: int


@dataclass(frozen=True)
class ProbeContext:
    attestation: Attestation
    api_key: str = field(repr=False)
    dsn: str = field(repr=False)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _validate_workspace_and_secret_directory() -> None:
    workspace = Path(os.path.abspath(str(WORKSPACE_ROOT)))
    expected = Path(os.path.abspath(str(EXPECTED_WORKSPACE_ROOT)))
    if workspace != expected:
        raise ProbeError("workspace_physical_root_mismatch")
    try:
        workspace_details = os.lstat(workspace)
        secret_details = os.lstat(SECRETS_DIR)
    except OSError as exc:
        raise ProbeError("private_directory_unavailable") from exc
    if (
        stat.S_ISLNK(workspace_details.st_mode)
        or not stat.S_ISDIR(workspace_details.st_mode)
        or workspace_details.st_uid != os.geteuid()
    ):
        raise ProbeError("workspace_physical_custody_invalid")
    if (
        stat.S_ISLNK(secret_details.st_mode)
        or not stat.S_ISDIR(secret_details.st_mode)
        or secret_details.st_uid != os.geteuid()
        or stat.S_IMODE(secret_details.st_mode) != 0o700
    ):
        raise ProbeError("private_directory_custody_invalid")
    if Path(os.path.abspath(str(SECRETS_DIR))) != workspace / "secrets":
        raise ProbeError("private_directory_path_invalid")


def _private_read(path: Path) -> PrivateDocument:
    expanded = Path(os.path.abspath(str(path)))
    if expanded.parent != Path(os.path.abspath(str(SECRETS_DIR))):
        raise ProbeError("private_file_path_invalid")
    try:
        initial = os.lstat(expanded)
    except OSError as exc:
        raise ProbeError("private_file_unavailable") from exc
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise ProbeError("private_file_type_invalid")
    if (
        initial.st_uid != os.geteuid()
        or stat.S_IMODE(initial.st_mode) != 0o600
        or initial.st_nlink != 1
    ):
        raise ProbeError("private_file_custody_invalid")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(expanded, flags)
    except OSError as exc:
        raise ProbeError("private_file_open_failed") from exc
    try:
        details = os.fstat(descriptor)
        current = os.lstat(expanded)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
            or (details.st_dev, details.st_ino) != (initial.st_dev, initial.st_ino)
            or (details.st_dev, details.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ProbeError("private_file_custody_changed")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_PRIVATE_FILE_BYTES:
                raise ProbeError("private_file_too_large")
            chunks.append(chunk)
    except ProbeError:
        raise
    except OSError as exc:
        raise ProbeError("private_file_read_failed") from exc
    finally:
        os.close(descriptor)
    return PrivateDocument(
        payload=b"".join(chunks),
        mode=stat.S_IMODE(details.st_mode),
        uid=details.st_uid,
        dev=details.st_dev,
        ino=details.st_ino,
        nlink=details.st_nlink,
    )


def _parse_env(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise ProbeError("credential_env_encoding_invalid") from exc
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key in values:
            raise ProbeError("credential_env_duplicate_key")
        values[key] = value
    return values


def _attestation_identity_sha256(payload: Mapping[str, Any]) -> str:
    material = {
        key: payload[key]
        for key in (
            "schema",
            "project_id",
            "branch_id",
            "endpoint_id",
            "endpoint_host",
            "connection_host",
            "pooler",
            "database",
            "role",
            "port",
            "tls_required",
            "generated_at",
        )
    }
    return hashlib.sha256(canonical_json(material)).hexdigest()


def _parse_attestation(document: PrivateDocument) -> Attestation:
    try:
        payload = json.loads(document.payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProbeError("attestation_payload_invalid") from exc
    expected_keys = {
        "schema",
        "project_id",
        "branch_id",
        "endpoint_id",
        "endpoint_host",
        "connection_host",
        "pooler",
        "database",
        "role",
        "port",
        "tls_required",
        "generated_at",
        "attestation_sha256",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_keys:
        raise ProbeError("attestation_fields_invalid")

    project_id = str(payload.get("project_id") or "")
    branch_id = str(payload.get("branch_id") or "")
    endpoint_id = str(payload.get("endpoint_id") or "")
    endpoint_host = str(payload.get("endpoint_host") or "").lower().rstrip(".")
    connection_host = str(payload.get("connection_host") or "").lower().rstrip(".")
    database = str(payload.get("database") or "")
    role = str(payload.get("role") or "")
    digest = str(payload.get("attestation_sha256") or "")
    generated_at = str(payload.get("generated_at") or "")
    try:
        parsed_timestamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProbeError("attestation_timestamp_invalid") from exc
    if parsed_timestamp.tzinfo is None:
        raise ProbeError("attestation_timestamp_invalid")

    if (
        payload.get("schema") != ATTESTATION_SCHEMA
        or PROJECT_ID_RE.fullmatch(project_id) is None
        or BRANCH_ID_RE.fullmatch(branch_id) is None
        or ENDPOINT_ID_RE.fullmatch(endpoint_id) is None
        or HOST_RE.fullmatch(endpoint_host) is None
        or HOST_RE.fullmatch(connection_host) is None
        or endpoint_host.split(".", 1)[0] != endpoint_id
        or connection_host != f"{endpoint_id}-pooler.{endpoint_host.split('.', 1)[1]}"
        or payload.get("pooler") is not True
        or DATABASE_NAME_RE.fullmatch(database) is None
        or DATABASE_NAME_RE.fullmatch(role) is None
        or isinstance(payload.get("port"), bool)
        or payload.get("port") != 5432
        or payload.get("tls_required") is not True
        or SHA256_RE.fullmatch(digest) is None
        or _attestation_identity_sha256(payload) != digest
    ):
        raise ProbeError("attestation_contract_invalid")
    return Attestation(
        attestation_sha256=digest,
        project_id=project_id,
        branch_id=branch_id,
        endpoint_id=endpoint_id,
        endpoint_host=endpoint_host,
        connection_host=connection_host,
        database=database,
        role=role,
        port=5432,
    )


def _validate_dsn(dsn: str, attestation: Attestation) -> None:
    try:
        parsed = urllib.parse.urlsplit(dsn)
        port = parsed.port or 5432
        role = urllib.parse.unquote(parsed.username or "")
        database = urllib.parse.unquote(parsed.path.lstrip("/"))
    except (TypeError, ValueError) as exc:
        raise ProbeError("database_dsn_invalid") from exc
    host = str(parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.password
        or parsed.fragment
        or "," in host
        or "/" in database
        or host != attestation.connection_host
        or role != attestation.role
        or database != attestation.database
        or port != attestation.port
    ):
        raise ProbeError("database_dsn_attestation_mismatch")

    query: dict[str, str] = {}
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key in query:
            raise ProbeError("database_dsn_duplicate_option")
        if key not in {"sslmode", "sslrootcert", "channel_binding"}:
            raise ProbeError("database_dsn_option_unsupported")
        query[key] = value
    if (
        query.get("sslmode", "").lower() != "verify-full"
        or query.get("sslrootcert") != "system"
        or query.get("channel_binding", "require").lower() != "require"
    ):
        raise ProbeError("database_dsn_tls_contract_invalid")


def load_context() -> ProbeContext:
    _validate_workspace_and_secret_directory()
    env_document = _private_read(HEALTH_ENV)
    attestation_document = _private_read(ATTESTATION)
    values = _parse_env(env_document.payload)
    attestation = _parse_attestation(attestation_document)
    required = {"HEALTH_DB_DSN", "TOPOLOGY_DB_DSN", "NEON_API_KEY", "NEON_PROJECT_ID"}
    if not required.issubset(values) or not all(values[key] for key in required):
        raise ProbeError("credential_env_required_key_missing")
    if values["HEALTH_DB_DSN"] != values["TOPOLOGY_DB_DSN"]:
        raise ProbeError("database_dsn_routes_diverged")
    if (
        values["NEON_PROJECT_ID"] != attestation.project_id
        or len(values["NEON_API_KEY"]) < 12
    ):
        raise ProbeError("neon_credential_authority_invalid")
    _validate_dsn(values["TOPOLOGY_DB_DSN"], attestation)
    return ProbeContext(
        attestation=attestation,
        api_key=values["NEON_API_KEY"],
        dsn=values["TOPOLOGY_DB_DSN"],
    )


def neon_get(api_key: str, path: str) -> Mapping[str, Any]:
    request = urllib.request.Request(
        f"{API_ROOT}/{path.lstrip('/')}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": 'openclaw-personal-data-neon-readonly-probe/1',
        },
        method="GET",
    )
    try:
        with API_OPENER.open(request, timeout=20) as response:
            if int(response.status) != 200:
                raise ProbeError("neon_api_status_invalid")
            raw = response.read(MAX_API_BYTES + 1)
    except ProbeError:
        raise
    except urllib.error.HTTPError as exc:
        raise ProbeError(f"neon_api_http_{int(exc.code)}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ProbeError("neon_api_transport_failed") from exc
    if len(raw) > MAX_API_BYTES:
        raise ProbeError("neon_api_payload_too_large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProbeError("neon_api_payload_invalid") from exc
    if not isinstance(payload, Mapping):
        raise ProbeError("neon_api_payload_invalid")
    return payload


def prove_neon_identity(
    context: ProbeContext,
    *,
    api_getter: Callable[[str, str], Mapping[str, Any]] = neon_get,
) -> dict[str, Any]:
    attestation = context.attestation
    auth_payload = api_getter(context.api_key, "auth")
    if (
        not isinstance(auth_payload.get("account_id"), str)
        or not auth_payload["account_id"]
        or not isinstance(auth_payload.get("auth_method"), str)
        or not auth_payload["auth_method"]
    ):
        raise ProbeError("neon_auth_identity_invalid")

    project_path = f"projects/{urllib.parse.quote(attestation.project_id, safe='')}"
    project_payload = api_getter(context.api_key, project_path)
    project = project_payload.get("project")
    if not isinstance(project, Mapping) or project.get("id") != attestation.project_id:
        raise ProbeError("neon_project_identity_mismatch")

    endpoints_payload = api_getter(context.api_key, f"{project_path}/endpoints")
    endpoints = endpoints_payload.get("endpoints")
    if not isinstance(endpoints, list):
        raise ProbeError("neon_endpoint_payload_invalid")
    matches = [
        endpoint
        for endpoint in endpoints
        if isinstance(endpoint, Mapping)
        and endpoint.get("id") == attestation.endpoint_id
        and endpoint.get("project_id") == attestation.project_id
        and endpoint.get("branch_id") == attestation.branch_id
        and str(endpoint.get("host") or "").lower().rstrip(".")
        == attestation.endpoint_host
    ]
    if len(matches) != 1:
        raise ProbeError("neon_endpoint_identity_mismatch")
    return {
        "http_methods": ["GET"],
        "auth_identity_verified": True,
        "project_identity_verified": True,
        "endpoint_identity_verified": True,
    }


def _validate_host_ca_bundle() -> None:
    try:
        initial = os.lstat(HOST_CA_BUNDLE)
    except OSError as exc:
        raise ProbeError("host_ca_bundle_unavailable") from exc
    if (
        stat.S_ISLNK(initial.st_mode)
        or not stat.S_ISREG(initial.st_mode)
        or initial.st_uid != 0
        or stat.S_IMODE(initial.st_mode) & 0o022
        or initial.st_size <= 0
    ):
        raise ProbeError("host_ca_bundle_custody_invalid")


@contextmanager
def _bound_host_ca_bundle() -> Iterator[None]:
    _validate_host_ca_bundle()
    prior_present = "SSL_CERT_FILE" in os.environ
    prior_value = os.environ.get("SSL_CERT_FILE")
    os.environ["SSL_CERT_FILE"] = str(HOST_CA_BUNDLE)
    try:
        yield
    finally:
        if prior_present and prior_value is not None:
            os.environ["SSL_CERT_FILE"] = prior_value
        else:
            os.environ.pop("SSL_CERT_FILE", None)


def _direct_endpoint_dsn(dsn: str, attestation: Attestation) -> str:
    """Replace only the attested pooler host with its attested direct endpoint."""
    try:
        parsed = urllib.parse.urlsplit(dsn)
        username = urllib.parse.quote(
            urllib.parse.unquote(parsed.username or ""), safe=""
        )
        password = urllib.parse.quote(
            urllib.parse.unquote(parsed.password or ""), safe=""
        )
    except (TypeError, ValueError) as exc:
        raise ProbeError("database_dsn_invalid") from exc
    if not username or not password:
        raise ProbeError("database_dsn_invalid")
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            f"{username}:{password}@{attestation.endpoint_host}:{attestation.port}",
            parsed.path,
            parsed.query,
            "",
        )
    )


def connect_read_only(dsn: str, attestation: Attestation) -> Any:
    direct_dsn = _direct_endpoint_dsn(dsn, attestation)
    with _bound_host_ca_bundle():
        return psycopg.connect(
            direct_dsn,
            connect_timeout=20,
            autocommit=True,
            application_name='openclaw-personal-data-neon-readonly-probe',
            options=(
                "-c default_transaction_read_only=on "
                "-c statement_timeout=10000 "
                "-c lock_timeout=5000"
            ),
        )


def prove_postgres_identity(
    context: ProbeContext,
    *,
    connector: Callable[[str, Attestation], Any] = connect_read_only,
) -> dict[str, Any]:
    attestation = context.attestation
    connection: Any = None
    try:
        connection = connector(context.dsn, attestation)
        info = connection.info
        ssl_in_use = bool(connection.pgconn.ssl_in_use)
        transport_checks = {
            "endpoint_host_verified": (
                str(info.host or "").lower().rstrip(".")
                == attestation.endpoint_host
            ),
            "server_port_verified": int(info.port or 0) == attestation.port,
            "connection_database_verified": str(info.dbname or "") == attestation.database,
            "connection_role_verified": str(info.user or "") == attestation.role,
            "libpq_tls_verified": ssl_in_use,
        }
        with connection.cursor() as cursor:
            cursor.execute(IDENTITY_QUERY)
            row = cursor.fetchone()
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) != 7:
            raise ProbeError("postgres_identity_result_invalid")
        (
            database,
            role,
            transaction_read_only,
            project_id,
            branch_id,
            endpoint_id,
            pg_stat_ssl,
        ) = row
        identity_checks = {
            "database_identity_verified": database == attestation.database,
            "role_identity_verified": role == attestation.role,
            "project_identity_verified": project_id == attestation.project_id,
            "branch_identity_verified": branch_id == attestation.branch_id,
            "endpoint_identity_verified": endpoint_id == attestation.endpoint_id,
            "transaction_read_only": transaction_read_only == "on",
            "tls_verified": ssl_in_use,
        }
        # Neon terminates client TLS at its proxy layer, so pg_stat_ssl can be
        # false even while libpq proves the verified TLS transport in use. Keep
        # the server-side observation visible without treating it as the TLS
        # authority; the attested verify-full DSN plus ssl_in_use is the gate.
        observations = {"pg_stat_ssl_reported": pg_stat_ssl is True}
        checks = {**transport_checks, **identity_checks, **observations}
        if not all({**transport_checks, **identity_checks}.values()):
            raise ProbeError("postgres_attested_identity_mismatch")
        return checks
    except ProbeError:
        raise
    except Exception as exc:
        raise ProbeError("postgres_readonly_probe_failed") from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def run_probe(
    *,
    api_getter: Callable[[str, str], Mapping[str, Any]] = neon_get,
    connector: Callable[[str, Attestation], Any] = connect_read_only,
) -> tuple[int, dict[str, Any]]:
    output: dict[str, Any] = {
        "schema": OUTPUT_SCHEMA,
        "route_id": ROUTE_ID,
        "checked_at_utc": utc_now(),
        "status": "blocked",
        "reason_code": None,
        "capability_scope": "read_only",
        "mutating": False,
        "secrets_emitted": False,
        "dsn_emitted": False,
        "unsupported_operations": [
            "write",
            "dml",
            "ddl",
            "migration",
            "provider_mutation",
        ],
    }
    try:
        context = load_context()
        provider = prove_neon_identity(context, api_getter=api_getter)
        postgres = prove_postgres_identity(context, connector=connector)
        output.update(
            {
                "status": "ready",
                "reason_code": None,
                "custody": {
                    "physical_workspace_verified": True,
                    "secrets_directory_mode": "0700",
                    "credential_file_mode": "0600",
                    "attestation_file_mode": "0600",
                    "single_link_private_files": True,
                },
                "attestation": {
                    "sha256": context.attestation.attestation_sha256,
                    "identity_bound": True,
                },
                "neon_api": provider,
                "postgres": postgres,
            }
        )
        return 0, output
    except ProbeError as exc:
        output["reason_code"] = exc.code
        return 2, output
    except Exception:
        output["reason_code"] = "unexpected_probe_failure"
        return 2, output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Probe the fixed PersonalDataProject Neon/Postgres route read-only'
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (default; retained for explicit registry callers)",
    )
    parser.parse_args(argv)
    code, output = run_probe()
    json.dump(output, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
