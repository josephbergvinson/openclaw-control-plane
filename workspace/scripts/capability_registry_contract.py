#!/usr/bin/env python3
"""Strict parsing and structural validation for capability registries."""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


MAX_PROBE_DEFAULT_TTL_MS = 86_400_000
MAX_CAPABILITY_STATUS_SLO_DAYS = 365
INTEGRATION_REGISTRY_FIELDS = frozenset(
    {
        "system_aliases",
        "routing_defaults",
        "portfolios",
        "portfolio_routes",
        "credential_handles",
        "routes",
    }
)
INTEGRATION_ROUTE_REQUIRED_FIELDS = frozenset(
    {
        "route_id",
        "system",
        "required_principal",
        "required_account",
        "readiness_status_id",
        "native_lane_kind",
        "operations",
        "provider_adapter",
        "default_contexts",
        "access_probe_id",
        "notes",
    }
)
INTEGRATION_ROUTE_OPTIONAL_FIELDS = frozenset(
    {
        "browser_profile_fallbacks",
        "browser_profile_primary",
        "capability_scope",
    }
)
ROUTING_DEFAULT_FIELDS = frozenset(
    {
        "adaptive_navigation_required",
        "agent_handled_auth_steps",
        "browser_fallback_predicates",
        "browser_profile_fallbacks",
        "browser_secret_entry",
        "fallback_order",
        "human_only_auth_gates",
        "managed_browser_primary",
        "native_lane_class",
        "native_lane_kinds",
    }
)
PROVIDER_ADAPTER_FIELDS = frozenset(
    {
        "browser_evidence",
        "completion_evidence",
        "constraints",
        "identity_policy",
        "native_evidence",
        "operation_policy",
        "request_evidence",
        "required_account_route_field",
        "target_url_evidence",
        "type",
    }
)
INTEGRATION_PROVIDER_ADAPTER_TYPES = frozenset(
    {
        "authenticated_account_route",
        "authenticated_provider_evidence",
    }
)
PORTFOLIO_REQUIRED_FIELDS = frozenset(
    {"portfolio_id", "display_name", "accounts"}
)
PORTFOLIO_OPTIONAL_FIELDS = frozenset({"authoritative_read_sources"})
PORTFOLIO_ACCOUNT_FIELDS = frozenset(
    {
        "account_id",
        "provider",
        "account",
        "provisioning_state",
        "auth_method",
    }
)
PORTFOLIO_AUTHORITATIVE_READ_SOURCE_FIELDS = frozenset(
    {
        "source_id",
        "canonical_url",
        "canonical_local_root",
        "registered_route_ids",
    }
)
PORTFOLIO_PROVISIONING_STATES = frozenset(
    {
        "enrolled",
        "route-registered",
        "unprovisioned",
        "discovery-required",
    }
)
CREDENTIAL_HANDLE_FIELDS = frozenset(
    {
        "handle_id",
        "credential_kind",
        "owner",
        "provisioning_state",
        "account_ids",
        "consumers",
        "browser_binding",
    }
)
CREDENTIAL_OWNER_FIELDS = frozenset({"kind", "ref", "keys"})
CREDENTIAL_CONSUMER_FIELDS = frozenset({"kind", "consumer_id"})
CREDENTIAL_BROWSER_BINDING_FIELDS = frozenset(
    {
        "binding_id",
        "login_hint",
        "allowed_origins",
    }
)
CREDENTIAL_KINDS = frozenset(
    {
        "api-credential-bundle",
        "api-token",
        "browser-password",
        "database-credential-bundle",
        "private-key",
        "provider-oauth",
        "provider-session",
        "provider-unlock-secret",
        "runtime-token",
    }
)
CREDENTIAL_OWNER_KINDS = frozenset(
    {
        "macos-keychain",
        "provider-native",
        "runtime-secret-file",
        "runtime-secret-json",
        "workspace-secret-file",
    }
)
CREDENTIAL_PROVISIONING_STATES = frozenset(
    {"provisioned", "unprovisioned", "discovery-required"}
)
CREDENTIAL_CONSUMER_KINDS = frozenset(
    {"browser-binding", "integration-route", "provider-native", "runtime-config", "script"}
)
PORTFOLIO_ROUTE_FIELDS = frozenset(
    {
        "portfolio_ids",
        "workspace_ids",
        "network_ids",
        "default_for_portfolios",
    }
)
_CANONICAL_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_CANONICAL_IDENTIFIER_PATTERN = re.compile(
    r"[a-z0-9]+(?:[-_][a-z0-9]+)*"
)
_CREDENTIAL_HANDLE_PATTERN = re.compile(
    r"[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*"
)
_BROWSER_CREDENTIAL_BINDING_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_ENV_KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_JSON_POINTER_PATTERN = re.compile(
    r"/(?:[A-Za-z0-9_.-]|~[01])+(?:/(?:[A-Za-z0-9_.-]|~[01])+)*"
)
PROBE_FIELDS = frozenset(
    {
        "probe_id",
        "command",
        "expected_signal",
        "safe_lane",
        "default_ttl",
        "scope",
        "notes",
    }
)
PROBE_SAFE_LANES = frozenset(
    {
        "browser-ui-read",
        "host-os-ui-read",
        "local-read",
        "local-read-costed-api-read",
        "local-read-network-read",
        "local-read-network-read-database-read",
        "local-secret-metadata-read",
    }
)
CAPABILITY_STATUS_REQUIRED_FIELDS = frozenset(
    {
        "capability_id",
        "state",
        "support_class",
        "freshness_class",
        "last_verified_utc",
        "evidence",
        "constraint_or_fallback",
        "domain",
        "slo_max_age_days",
        "config_dependencies",
    }
)
CAPABILITY_STATUS_OPTIONAL_FIELDS = frozenset(
    {"evidence_effects", "evidence_operations"}
)
CAPABILITY_STATUS_STRING_FIELDS = frozenset(
    {
        "capability_id",
        "state",
        "support_class",
        "freshness_class",
        "last_verified_utc",
        "evidence",
        "constraint_or_fallback",
        "domain",
    }
)
_CANONICAL_UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?Z"
)


class RegistryContractError(ValueError):
    """A registry cannot be interpreted without ambiguity or trusted as policy."""


class CapabilityStatusContractError(RegistryContractError):
    """A capability status row violates one named part of the shared schema."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def parse_explicit_utc_timestamp(value: Any, *, source: str) -> datetime:
    """Parse one canonical ISO timestamp that explicitly ends in UTC ``Z``."""

    error = (
        f"invalid {source}: timestamp must be an explicit UTC ISO timestamp "
        "ending in Z; canonical UTC ISO timestamp grammar is "
        "YYYY-MM-DDTHH:MM:SS[.fraction]Z"
    )
    if not isinstance(value, str) or _CANONICAL_UTC_TIMESTAMP_PATTERN.fullmatch(
        value
    ) is None:
        raise RegistryContractError(error)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RegistryContractError(error) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RegistryContractError(error)
    return parsed


def parse_evidence_operations(value: Any, *, source: str) -> list[str]:
    """Parse the exact registered operations supported by one evidence row."""

    if (
        not isinstance(value, list)
        or not all(
            isinstance(operation, str)
            and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", operation) is not None
            for operation in value
        )
        or len(value) != len(set(value))
    ):
        raise RegistryContractError(
            f"invalid {source}: evidence_operations must be a unique list of "
            "canonical operation slugs"
        )
    return list(value)


def validate_capability_status_record(
    record: Any,
    *,
    source: str,
) -> dict[str, Any]:
    """Parse one complete capability status row before readiness decisions."""

    if not isinstance(record, dict):
        raise CapabilityStatusContractError(
            "schema", f"invalid {source}: status record must be an object"
        )
    fields = set(record)
    allowed_fields = (
        CAPABILITY_STATUS_REQUIRED_FIELDS | CAPABILITY_STATUS_OPTIONAL_FIELDS
    )
    missing = sorted(CAPABILITY_STATUS_REQUIRED_FIELDS - fields)
    unexpected = sorted(fields - allowed_fields)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise CapabilityStatusContractError(
            "schema",
            f"invalid {source}: status fields must match the canonical schema "
            f"({'; '.join(details)})",
        )

    for field in CAPABILITY_STATUS_STRING_FIELDS:
        value = record[field]
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
        ):
            raise CapabilityStatusContractError(
                "field",
                f"invalid {source}: {field} must be one canonical nonblank string",
            )

    try:
        parse_explicit_utc_timestamp(
            record["last_verified_utc"],
            source=source,
        )
    except RegistryContractError as exc:
        raise CapabilityStatusContractError("timestamp", str(exc)) from exc

    slo_max_age_days = record["slo_max_age_days"]
    try:
        finite_slo = math.isfinite(slo_max_age_days)
    except (TypeError, OverflowError):
        finite_slo = False
    if (
        not isinstance(slo_max_age_days, (int, float))
        or isinstance(slo_max_age_days, bool)
        or not finite_slo
        or slo_max_age_days <= 0
        or slo_max_age_days > MAX_CAPABILITY_STATUS_SLO_DAYS
    ):
        raise CapabilityStatusContractError(
            "slo",
            f"invalid {source}: slo_max_age_days must be a positive finite "
            f"number no greater than {MAX_CAPABILITY_STATUS_SLO_DAYS}",
        )

    config_dependencies = record["config_dependencies"]
    if (
        not isinstance(config_dependencies, list)
        or not all(
            isinstance(dependency, str)
            and dependency.strip()
            and dependency == dependency.strip()
            for dependency in config_dependencies
        )
        or len(config_dependencies) != len(set(config_dependencies))
    ):
        raise CapabilityStatusContractError(
            "config_dependencies",
            f"invalid {source}: config_dependencies must be a unique list of "
            "canonical nonblank strings",
        )

    evidence_effects = record.get("evidence_effects")
    if evidence_effects is not None and (
        not isinstance(evidence_effects, list)
        or not evidence_effects
        or not all(
            isinstance(effect, str) and effect in {"read", "mutation"}
            for effect in evidence_effects
        )
        or len(evidence_effects) != len(set(evidence_effects))
    ):
        raise CapabilityStatusContractError(
            "evidence_effects",
            f"invalid {source}: evidence_effects must be a unique non-empty "
            "subset of read/mutation",
        )

    validated = dict(record)
    validated["config_dependencies"] = list(config_dependencies)
    if evidence_effects is not None:
        validated["evidence_effects"] = list(evidence_effects)
    if "evidence_operations" in record:
        try:
            validated["evidence_operations"] = parse_evidence_operations(
                record["evidence_operations"], source=source
            )
        except RegistryContractError as exc:
            raise CapabilityStatusContractError(
                "evidence_operations", str(exc)
            ) from exc
    return validated


def _reject_json_constant(value: str) -> None:
    raise RegistryContractError(f"non-standard JSON numeric constant {value!r}")


def parse_probe_default_ttl_ms(value: Any, *, source: str) -> int:
    """Parse the bounded integer-millisecond freshness contract for one probe."""

    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > MAX_PROBE_DEFAULT_TTL_MS
    ):
        raise RegistryContractError(
            f"invalid {source}: default_ttl must be an integer between 1 and "
            f"{MAX_PROBE_DEFAULT_TTL_MS} milliseconds"
        )
    return value


def validate_probe_record(record: Any, *, source: str) -> None:
    """Validate one complete, canonical, read-only probe contract."""

    if not isinstance(record, dict):
        raise RegistryContractError(f"invalid {source}: probe must be an object")
    fields = set(record)
    if fields != PROBE_FIELDS:
        missing = sorted(PROBE_FIELDS - fields)
        unexpected = sorted(fields - PROBE_FIELDS)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise RegistryContractError(
            f"invalid {source}: probe fields must match the canonical contract "
            f"({'; '.join(details)})"
        )
    for field in (
        "probe_id",
        "command",
        "expected_signal",
        "safe_lane",
        "scope",
        "notes",
    ):
        value = record[field]
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
        ):
            raise RegistryContractError(
                f"invalid {source}: {field} must be one canonical nonblank string"
            )
    for field in ("probe_id", "scope"):
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", record[field]) is None:
            raise RegistryContractError(
                f"invalid {source}: {field} must be a canonical lowercase slug"
            )
    if record["safe_lane"] not in PROBE_SAFE_LANES:
        raise RegistryContractError(
            f"invalid {source}: safe_lane is not a reviewed read-only lane"
        )
    parse_probe_default_ttl_ms(record["default_ttl"], source=source)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryContractError(f"duplicate JSON mapping key {key!r}")
        result[key] = value
    return result


def loads_json_strict(text: str, *, source: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RegistryContractError) as exc:
        raise RegistryContractError(f"invalid {source}: {exc}") from exc


def loads_yaml_strict(text: str, *, source: str) -> Any:
    # JSON-only probe helpers run with Python user packages disabled. Keep the
    # optional YAML dependency outside their import path, while YAML callers
    # still fail closed if PyYAML is unavailable.
    try:
        import yaml
        from yaml.constructor import ConstructorError
        from yaml.nodes import MappingNode
    except ModuleNotFoundError as exc:
        raise RegistryContractError(
            f"invalid {source}: PyYAML is unavailable"
        ) from exc

    class UniqueKeySafeLoader(yaml.SafeLoader):
        """Safe loader rejecting duplicate mapping keys at every depth."""

    def construct_unique_mapping(
        loader: UniqueKeySafeLoader,
        node: MappingNode,
        deep: bool = False,
    ) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None,
                None,
                f"expected a mapping node, found {node.id}",
                node.start_mark,
            )
        loader.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as exc:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable mapping key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise RegistryContractError(
                    f"duplicate YAML mapping key {key!r}"
                )
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueKeySafeLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_unique_mapping,
    )
    try:
        return yaml.load(text, Loader=UniqueKeySafeLoader)
    except (yaml.YAMLError, RegistryContractError) as exc:
        raise RegistryContractError(f"invalid {source}: {exc}") from exc


def load_json_strict(path: Path, *, source: str | None = None) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryContractError(
            f"unable to read {source or str(path)}"
        ) from exc
    try:
        from scripts.routing_operator_bindings import materialize
    except ModuleNotFoundError:
        from routing_operator_bindings import materialize
    return materialize(loads_json_strict(text, source=source or str(path)))


def load_yaml_strict(path: Path, *, source: str | None = None) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryContractError(
            f"unable to read {source or str(path)}"
        ) from exc
    return loads_yaml_strict(text, source=source or str(path))


def canonical_json_bytes(value: Any) -> bytes:
    """Return a type-exact canonical encoding for semantic twin comparison."""

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RegistryContractError("value is not canonically serializable") from exc
    return encoded


def _canonical_nonblank_string(value: Any, *, source: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise RegistryContractError(
            f"invalid {source}: value must be one canonical nonblank string"
        )
    return value


def _canonical_slug(value: Any, *, source: str) -> str:
    parsed = _canonical_nonblank_string(value, source=source)
    if _CANONICAL_SLUG_PATTERN.fullmatch(parsed) is None:
        raise RegistryContractError(
            f"invalid {source}: value must be one canonical lowercase slug"
        )
    return parsed


def _canonical_identifier(value: Any, *, source: str) -> str:
    """Validate a lower-case identifier whose upstream vocabulary uses `_`."""

    parsed = _canonical_nonblank_string(value, source=source)
    if _CANONICAL_IDENTIFIER_PATTERN.fullmatch(parsed) is None:
        raise RegistryContractError(
            f"invalid {source}: value must be one canonical lowercase identifier"
        )
    return parsed


def _unique_canonical_string_list(
    value: Any,
    *,
    source: str,
    slugs: bool = False,
    identifiers: bool = False,
    nonempty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise RegistryContractError(
            f"invalid {source}: value must be a {qualifier}list"
        )
    parsed = [
        (
            (
                _canonical_slug(item, source=f"{source}[{position}]")
                if slugs
                else _canonical_identifier(
                    item, source=f"{source}[{position}]"
                )
            )
            if slugs or identifiers
            else _canonical_nonblank_string(item, source=f"{source}[{position}]")
        )
        for position, item in enumerate(value)
    ]
    if len(parsed) != len(set(parsed)):
        raise RegistryContractError(f"invalid {source}: values must be unique")
    return parsed


def _validate_provider_adapter(
    adapter: Any,
    *,
    source: str,
    has_mutation: bool,
    has_ui_mutation: bool,
) -> None:
    if adapter is None:
        if has_ui_mutation:
            raise RegistryContractError(
                f"invalid {source}: authenticated-UI mutations require an "
                "authenticated_account_route provider adapter"
            )
        return
    if not isinstance(adapter, dict):
        raise RegistryContractError(
            f"invalid {source}: provider_adapter must be an object or null"
        )
    unexpected = sorted(set(adapter) - PROVIDER_ADAPTER_FIELDS)
    if unexpected:
        raise RegistryContractError(
            f"invalid {source}: provider_adapter has unexpected field(s) "
            + ", ".join(unexpected)
        )

    adapter_type = adapter.get("type")
    if adapter_type not in INTEGRATION_PROVIDER_ADAPTER_TYPES:
        raise RegistryContractError(
            f"invalid {source}: unsupported provider_adapter.type "
            f"{adapter_type!r}"
        )
    if adapter.get("required_account_route_field") != "required_account":
        raise RegistryContractError(
            f"invalid {source}: provider_adapter.required_account_route_field "
            "must be required_account"
        )
    identity_policy = adapter.get("identity_policy")
    if not isinstance(identity_policy, dict):
        raise RegistryContractError(
            f"invalid {source}: provider_adapter.identity_policy must be an object"
        )
    if identity_policy.get("required") is not True:
        raise RegistryContractError(
            f"invalid {source}: provider_adapter.identity_policy.required must "
            "be true"
        )
    if identity_policy.get("normalizer") not in {"exact", "casefold"}:
        raise RegistryContractError(
            f"invalid {source}: provider_adapter.identity_policy.normalizer "
            "must be exact or casefold"
        )
    if adapter_type == "authenticated_provider_evidence" and has_mutation:
        raise RegistryContractError(
            f"invalid {source}: authenticated_provider_evidence is read-only"
        )
    if has_ui_mutation and adapter_type != "authenticated_account_route":
        raise RegistryContractError(
            f"invalid {source}: authenticated-UI mutations require an "
            "authenticated_account_route provider adapter"
        )

    native_evidence = adapter.get("native_evidence")
    if not isinstance(native_evidence, dict):
        raise RegistryContractError(
            f"invalid {source}: provider_adapter.native_evidence must be an object"
        )
    digest_required = identity_policy.get(
        "provider_account_digest_required", False
    )
    if not isinstance(digest_required, bool):
        raise RegistryContractError(
            f"invalid {source}: provider_account_digest_required must be boolean"
        )
    account_digest = native_evidence.get("provider_account_id_sha256")
    if digest_required and account_digest is None:
        raise RegistryContractError(
            f"invalid {source}: provider_account_id_sha256 is required by the "
            "route identity policy"
        )
    if not digest_required and account_digest is not None:
        raise RegistryContractError(
            f"invalid {source}: provider_account_id_sha256 is forbidden unless "
            "the route identity policy requires it"
        )
    if account_digest is not None and (
        not isinstance(account_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", account_digest) is None
    ):
        raise RegistryContractError(
            f"invalid {source}: provider_account_id_sha256 must be a lowercase "
            "SHA-256 digest"
        )


def validate_integration_route_record(
    route: Any,
    *,
    source: str,
    native_lane_kinds: set[str],
) -> None:
    """Validate one route-local selection, operation, and identity contract."""

    if not isinstance(route, dict):
        raise RegistryContractError(f"invalid {source}: route must be an object")
    missing = sorted(INTEGRATION_ROUTE_REQUIRED_FIELDS - set(route))
    unexpected = sorted(
        set(route)
        - INTEGRATION_ROUTE_REQUIRED_FIELDS
        - INTEGRATION_ROUTE_OPTIONAL_FIELDS
    )
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise RegistryContractError(
            f"invalid {source}: route fields do not match the schema ("
            + "; ".join(details)
            + ")"
        )

    _canonical_slug(route.get("route_id"), source=f"{source}.route_id")
    _canonical_slug(route.get("system"), source=f"{source}.system")
    _canonical_slug(
        route.get("required_principal"), source=f"{source}.required_principal"
    )
    _canonical_nonblank_string(
        route.get("required_account"), source=f"{source}.required_account"
    )
    _canonical_nonblank_string(route.get("notes"), source=f"{source}.notes")
    _canonical_slug(
        route.get("access_probe_id"), source=f"{source}.access_probe_id"
    )
    _unique_canonical_string_list(
        route.get("default_contexts"),
        source=f"{source}.default_contexts",
    )

    readiness_status_id = route.get("readiness_status_id")
    if readiness_status_id is not None:
        _canonical_nonblank_string(
            readiness_status_id, source=f"{source}.readiness_status_id"
        )

    operations = route.get("operations")
    if not isinstance(operations, dict) or not operations:
        raise RegistryContractError(
            f"invalid {source}: operations must be a non-empty mapping"
        )
    has_native_operation = False
    has_mutation = False
    has_ui_mutation = False
    for operation, specification in operations.items():
        operation_source = f"{source}.operations[{operation!r}]"
        _canonical_slug(operation, source=f"{operation_source}.name")
        if not isinstance(specification, dict):
            raise RegistryContractError(
                f"invalid {operation_source}: operation contract must be an object"
            )
        lane = specification.get("lane")
        effect = specification.get("effect")
        expected_fields = (
            {"lane", "effect", "readiness_status_id"}
            if effect == "mutation"
            else {"lane", "effect"}
        )
        if set(specification) != expected_fields:
            raise RegistryContractError(
                f"invalid {operation_source}: operation fields do not match its "
                "effect contract"
            )
        if lane not in {"declared_native", "authenticated_ui"}:
            raise RegistryContractError(
                f"invalid {operation_source}: unsupported operation lane {lane!r}"
            )
        if effect not in {"read", "mutation"}:
            raise RegistryContractError(
                f"invalid {operation_source}: unsupported operation effect "
                f"{effect!r}"
            )
        if effect == "mutation":
            _canonical_nonblank_string(
                specification.get("readiness_status_id"),
                source=f"{operation_source}.readiness_status_id",
            )
            has_mutation = True
            has_ui_mutation = has_ui_mutation or lane == "authenticated_ui"
        has_native_operation = has_native_operation or lane == "declared_native"

    native_lane_kind = route.get("native_lane_kind")
    if native_lane_kind is not None and native_lane_kind not in native_lane_kinds:
        raise RegistryContractError(
            f"invalid {source}: native_lane_kind {native_lane_kind!r} is not "
            "declared by routing_defaults.native_lane_kinds"
        )
    if has_native_operation and native_lane_kind is None:
        raise RegistryContractError(
            f"invalid {source}: declared-native operations require native_lane_kind"
        )

    _validate_provider_adapter(
        route.get("provider_adapter"),
        source=source,
        has_mutation=has_mutation,
        has_ui_mutation=has_ui_mutation,
    )


def _validate_portfolios(
    payload: Any, *, source: str, route_ids: set[str]
) -> tuple[set[str], set[str]]:
    if not isinstance(payload, list) or not payload:
        raise RegistryContractError(
            f"invalid {source}: portfolios must be a non-empty list"
        )
    portfolio_ids: set[str] = set()
    account_ids: set[str] = set()
    for position, portfolio in enumerate(payload):
        portfolio_source = f"{source}.portfolios[{position}]"
        fields = set(portfolio) if isinstance(portfolio, dict) else set()
        missing = sorted(PORTFOLIO_REQUIRED_FIELDS - fields)
        unexpected = sorted(
            fields - PORTFOLIO_REQUIRED_FIELDS - PORTFOLIO_OPTIONAL_FIELDS
        )
        if not isinstance(portfolio, dict) or missing or unexpected:
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unexpected:
                details.append("unexpected " + ", ".join(unexpected))
            raise RegistryContractError(
                f"invalid {portfolio_source}: fields must match the portfolio "
                "schema"
                + (f" ({'; '.join(details)})" if details else "")
            )
        portfolio_id = _canonical_slug(
            portfolio.get("portfolio_id"), source=f"{portfolio_source}.portfolio_id"
        )
        if portfolio_id in portfolio_ids:
            raise RegistryContractError(
                f"invalid {source}: duplicate portfolio_id {portfolio_id!r}"
            )
        portfolio_ids.add(portfolio_id)
        _canonical_nonblank_string(
            portfolio.get("display_name"),
            source=f"{portfolio_source}.display_name",
        )
        accounts = portfolio.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            raise RegistryContractError(
                f"invalid {portfolio_source}.accounts: value must be a non-empty list"
            )
        for account_position, account in enumerate(accounts):
            account_source = f"{portfolio_source}.accounts[{account_position}]"
            if not isinstance(account, dict) or set(account) != PORTFOLIO_ACCOUNT_FIELDS:
                raise RegistryContractError(
                    f"invalid {account_source}: fields must match the portfolio "
                    "account schema"
                )
            account_id = _canonical_slug(
                account.get("account_id"), source=f"{account_source}.account_id"
            )
            if account_id in account_ids:
                raise RegistryContractError(
                    f"invalid {source}: duplicate account_id {account_id!r}"
                )
            account_ids.add(account_id)
            _canonical_slug(
                account.get("provider"), source=f"{account_source}.provider"
            )
            _canonical_slug(
                account.get("auth_method"), source=f"{account_source}.auth_method"
            )
            state = account.get("provisioning_state")
            if state not in PORTFOLIO_PROVISIONING_STATES:
                raise RegistryContractError(
                    f"invalid {account_source}.provisioning_state: unsupported "
                    f"state {state!r}"
                )
            account_value = account.get("account")
            if account_value is None:
                if state != "discovery-required":
                    raise RegistryContractError(
                        f"invalid {account_source}.account: only discovery-required "
                        "identities may omit the public account identifier"
                    )
            else:
                _canonical_nonblank_string(
                    account_value, source=f"{account_source}.account"
                )

        authoritative_sources = portfolio.get("authoritative_read_sources", [])
        if not isinstance(authoritative_sources, list):
            raise RegistryContractError(
                f"invalid {portfolio_source}.authoritative_read_sources: value "
                "must be a list"
            )
        seen_source_ids: set[str] = set()
        for source_position, source_record in enumerate(authoritative_sources):
            source_record_source = (
                f"{portfolio_source}.authoritative_read_sources[{source_position}]"
            )
            if (
                not isinstance(source_record, dict)
                or set(source_record)
                != PORTFOLIO_AUTHORITATIVE_READ_SOURCE_FIELDS
            ):
                raise RegistryContractError(
                    f"invalid {source_record_source}: fields must match the "
                    "authoritative read source schema"
                )
            source_id = _canonical_slug(
                source_record.get("source_id"),
                source=f"{source_record_source}.source_id",
            )
            if source_id in seen_source_ids:
                raise RegistryContractError(
                    f"invalid {portfolio_source}.authoritative_read_sources: "
                    f"duplicate source_id {source_id!r}"
                )
            seen_source_ids.add(source_id)
            canonical_url = source_record.get("canonical_url")
            if canonical_url is not None:
                canonical_url = _canonical_nonblank_string(
                    canonical_url,
                    source=f"{source_record_source}.canonical_url",
                )
                parsed_url = urllib.parse.urlsplit(canonical_url)
                if (
                    parsed_url.scheme != "https"
                    or not parsed_url.netloc
                    or parsed_url.username is not None
                    or parsed_url.password is not None
                    or parsed_url.query
                    or parsed_url.fragment
                ):
                    raise RegistryContractError(
                        f"invalid {source_record_source}.canonical_url: value must "
                        "be one exact HTTPS locator without credentials, query, or "
                        "fragment"
                    )
            canonical_local_root = source_record.get("canonical_local_root")
            if canonical_local_root is not None and (
                not isinstance(canonical_local_root, str)
                or canonical_local_root != canonical_local_root.strip()
                or not canonical_local_root.startswith("/")
                or ".." in canonical_local_root.split("/")
            ):
                raise RegistryContractError(
                    f"invalid {source_record_source}.canonical_local_root: value "
                    "must be null or one absolute normalized path"
                )
            if canonical_url is None and canonical_local_root is None:
                raise RegistryContractError(
                    f"invalid {source_record_source}: at least one exact source "
                    "locator is required"
                )
            registered_route_ids = _unique_canonical_string_list(
                source_record.get("registered_route_ids"),
                source=f"{source_record_source}.registered_route_ids",
                slugs=True,
            )
            unknown_route_ids = sorted(set(registered_route_ids) - route_ids)
            if unknown_route_ids:
                raise RegistryContractError(
                    f"invalid {source_record_source}.registered_route_ids: unknown "
                    "route(s) " + ", ".join(unknown_route_ids)
                )
    return portfolio_ids, account_ids


def _validate_credential_handles(
    payload: Any,
    *,
    source: str,
    account_ids: set[str],
    route_ids: set[str],
) -> None:
    if not isinstance(payload, list) or not payload:
        raise RegistryContractError(
            f"invalid {source}.credential_handles: value must be a non-empty list"
        )

    handle_ids: set[str] = set()
    browser_binding_ids: set[str] = set()
    for position, handle in enumerate(payload):
        handle_source = f"{source}.credential_handles[{position}]"
        if not isinstance(handle, dict) or set(handle) != CREDENTIAL_HANDLE_FIELDS:
            raise RegistryContractError(
                f"invalid {handle_source}: fields must match the credential-handle schema"
            )

        handle_id = _canonical_nonblank_string(
            handle.get("handle_id"), source=f"{handle_source}.handle_id"
        )
        if (
            len(handle_id) > 96
            or _CREDENTIAL_HANDLE_PATTERN.fullmatch(handle_id) is None
        ):
            raise RegistryContractError(
                f"invalid {handle_source}.handle_id: value must be one lower-case "
                "dot-separated credential alias no longer than 96 characters"
            )
        if handle_id in handle_ids:
            raise RegistryContractError(
                f"invalid {source}.credential_handles: duplicate handle_id {handle_id!r}"
            )
        handle_ids.add(handle_id)

        credential_kind = handle.get("credential_kind")
        if credential_kind not in CREDENTIAL_KINDS:
            raise RegistryContractError(
                f"invalid {handle_source}.credential_kind: unsupported kind "
                f"{credential_kind!r}"
            )
        provisioning_state = handle.get("provisioning_state")
        if provisioning_state not in CREDENTIAL_PROVISIONING_STATES:
            raise RegistryContractError(
                f"invalid {handle_source}.provisioning_state: unsupported state "
                f"{provisioning_state!r}"
            )

        owner = handle.get("owner")
        if not isinstance(owner, dict) or set(owner) != CREDENTIAL_OWNER_FIELDS:
            raise RegistryContractError(
                f"invalid {handle_source}.owner: fields must match the owner schema"
            )
        owner_kind = owner.get("kind")
        if owner_kind not in CREDENTIAL_OWNER_KINDS:
            raise RegistryContractError(
                f"invalid {handle_source}.owner.kind: unsupported owner "
                f"{owner_kind!r}"
            )
        owner_ref = _canonical_nonblank_string(
            owner.get("ref"), source=f"{handle_source}.owner.ref"
        )
        owner_keys = _unique_canonical_string_list(
            owner.get("keys"),
            source=f"{handle_source}.owner.keys",
            nonempty=True,
        )
        if owner_kind in {
            "workspace-secret-file",
            "runtime-secret-file",
            "runtime-secret-json",
        }:
            owner_path = Path(owner_ref)
            if (
                not owner_path.is_absolute()
                or ".." in owner_path.parts
                or str(owner_path) != owner_ref
            ):
                raise RegistryContractError(
                    f"invalid {handle_source}.owner.ref: secret owner path must "
                    "be absolute, canonical, and name one file"
                )
            if owner_kind == "runtime-secret-json":
                if owner_path.suffix != ".json":
                    raise RegistryContractError(
                        f"invalid {handle_source}.owner.ref: runtime-secret-json "
                        "must name one JSON file"
                    )
                invalid_keys = [
                    key for key in owner_keys
                    if _JSON_POINTER_PATTERN.fullmatch(key) is None
                ]
            else:
                if owner_path.suffix not in {"", ".env"}:
                    raise RegistryContractError(
                        f"invalid {handle_source}.owner.ref: secret-file owner "
                        "must name an env file"
                    )
                invalid_keys = [
                    key for key in owner_keys
                    if _ENV_KEY_PATTERN.fullmatch(key) is None
                ]
            if invalid_keys:
                raise RegistryContractError(
                    f"invalid {handle_source}.owner.keys: invalid secret key "
                    f"coordinate(s) {invalid_keys!r}"
                )

        parsed_account_ids = _unique_canonical_string_list(
            handle.get("account_ids"),
            source=f"{handle_source}.account_ids",
            slugs=True,
        )
        unknown_accounts = sorted(set(parsed_account_ids) - account_ids)
        if unknown_accounts:
            raise RegistryContractError(
                f"invalid {handle_source}.account_ids: unknown account(s) "
                + ", ".join(unknown_accounts)
            )

        consumers = handle.get("consumers")
        if not isinstance(consumers, list) or not consumers:
            raise RegistryContractError(
                f"invalid {handle_source}.consumers: value must be a non-empty list"
            )
        seen_consumers: set[tuple[str, str]] = set()
        binding_consumers: list[str] = []
        for consumer_position, consumer in enumerate(consumers):
            consumer_source = (
                f"{handle_source}.consumers[{consumer_position}]"
            )
            if (
                not isinstance(consumer, dict)
                or set(consumer) != CREDENTIAL_CONSUMER_FIELDS
            ):
                raise RegistryContractError(
                    f"invalid {consumer_source}: fields must match the consumer schema"
                )
            consumer_kind = consumer.get("kind")
            if consumer_kind not in CREDENTIAL_CONSUMER_KINDS:
                raise RegistryContractError(
                    f"invalid {consumer_source}.kind: unsupported consumer kind "
                    f"{consumer_kind!r}"
                )
            consumer_id = _canonical_nonblank_string(
                consumer.get("consumer_id"),
                source=f"{consumer_source}.consumer_id",
            )
            consumer_key = (str(consumer_kind), consumer_id)
            if consumer_key in seen_consumers:
                raise RegistryContractError(
                    f"invalid {handle_source}.consumers: duplicate consumer "
                    f"{consumer_key!r}"
                )
            seen_consumers.add(consumer_key)
            if consumer_kind == "integration-route" and consumer_id not in route_ids:
                raise RegistryContractError(
                    f"invalid {consumer_source}.consumer_id: unknown route "
                    f"{consumer_id!r}"
                )
            if consumer_kind == "browser-binding":
                binding_consumers.append(consumer_id)

        browser_binding = handle.get("browser_binding")
        if browser_binding is None:
            if binding_consumers:
                raise RegistryContractError(
                    f"invalid {handle_source}: browser-binding consumer requires "
                    "browser_binding metadata"
                )
            continue
        if (
            not isinstance(browser_binding, dict)
            or set(browser_binding) != CREDENTIAL_BROWSER_BINDING_FIELDS
        ):
            raise RegistryContractError(
                f"invalid {handle_source}.browser_binding: fields must match the "
                "browser-binding schema"
            )
        if credential_kind != "browser-password" or owner_kind != "macos-keychain":
            raise RegistryContractError(
                f"invalid {handle_source}.browser_binding: only a Keychain-owned "
                "browser-password may define a binding"
            )
        binding_id = _canonical_nonblank_string(
            browser_binding.get("binding_id"),
            source=f"{handle_source}.browser_binding.binding_id",
        )
        if _BROWSER_CREDENTIAL_BINDING_PATTERN.fullmatch(binding_id) is None:
            raise RegistryContractError(
                f"invalid {handle_source}.browser_binding.binding_id: value must "
                "match the runtime browser binding alias grammar"
            )
        if binding_id in browser_binding_ids:
            raise RegistryContractError(
                f"invalid {source}.credential_handles: duplicate browser binding "
                f"{binding_id!r}"
            )
        browser_binding_ids.add(binding_id)
        if binding_consumers != [binding_id]:
            raise RegistryContractError(
                f"invalid {handle_source}.consumers: browser binding must have one "
                "matching browser-binding consumer"
            )
        login_hint = browser_binding.get("login_hint")
        if login_hint is not None:
            _canonical_nonblank_string(
                login_hint,
                source=f"{handle_source}.browser_binding.login_hint",
            )
        origins = _unique_canonical_string_list(
            browser_binding.get("allowed_origins"),
            source=f"{handle_source}.browser_binding.allowed_origins",
            nonempty=True,
        )
        for origin in origins:
            parsed_origin = urllib.parse.urlsplit(origin)
            if (
                parsed_origin.scheme not in {"https", "chrome-extension"}
                or not parsed_origin.netloc
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise RegistryContractError(
                    f"invalid {handle_source}.browser_binding.allowed_origins: "
                    f"{origin!r} is not one exact allowed origin"
                )


def _validate_portfolio_routes(
    payload: Any,
    *,
    source: str,
    routes: list[dict[str, Any]],
    portfolio_ids: set[str],
) -> None:
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "selection_only",
            "portfolio_source",
            "ambiguity_resolution",
            "by_route_id",
        }
        or payload.get("selection_only") is not True
        or payload.get("portfolio_source") != "trusted_project_or_session"
        or payload.get("ambiguity_resolution")
        != "read_account_set_or_ask_one_concise_question"
        or not isinstance(payload.get("by_route_id"), dict)
    ):
        raise RegistryContractError(
            f"invalid {source}.portfolio_routes: expected one selection-only "
            "trusted-project-or-session index with bounded read-account-set "
            "or concise mutation ambiguity resolution"
        )
    route_by_id = {route["route_id"]: route for route in routes}
    bindings = payload["by_route_id"]
    if set(bindings) != set(route_by_id):
        missing = sorted(set(route_by_id) - set(bindings))
        unknown = sorted(set(bindings) - set(route_by_id))
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise RegistryContractError(
            f"invalid {source}.portfolio_routes: route coverage is not exact "
            f"({'; '.join(details)})"
        )

    defaults: set[tuple[str, str]] = set()
    for route_id, binding in bindings.items():
        binding_source = f"{source}.portfolio_routes.by_route_id[{route_id!r}]"
        if not isinstance(binding, dict) or set(binding) != PORTFOLIO_ROUTE_FIELDS:
            raise RegistryContractError(
                f"invalid {binding_source}: fields must match the compact "
                "selection schema"
            )
        parsed = {
            field: _unique_canonical_string_list(
                binding.get(field),
                source=f"{binding_source}.{field}",
                slugs=True,
                nonempty=field == "portfolio_ids",
            )
            for field in PORTFOLIO_ROUTE_FIELDS
        }
        unknown_portfolios = sorted(set(parsed["portfolio_ids"]) - portfolio_ids)
        if unknown_portfolios:
            raise RegistryContractError(
                f"invalid {binding_source}.portfolio_ids: unknown portfolio(s) "
                + ", ".join(unknown_portfolios)
            )
        if not set(parsed["default_for_portfolios"]).issubset(
            parsed["portfolio_ids"]
        ):
            raise RegistryContractError(
                f"invalid {binding_source}.default_for_portfolios: defaults must "
                "be a subset of portfolio_ids"
            )
        system = route_by_id[route_id]["system"]
        for portfolio_id in parsed["default_for_portfolios"]:
            default_key = (system, portfolio_id)
            if default_key in defaults:
                raise RegistryContractError(
                    f"invalid {source}.portfolio_routes: multiple defaults for "
                    f"system {system!r} and portfolio {portfolio_id!r}"
                )
            defaults.add(default_key)


def _assert_registry_document(
    payload: Any,
    *,
    source: str,
    collection: str,
    key: str,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RegistryContractError(f"invalid {source}: registry must be an object")
    records = payload.get(collection)
    if not isinstance(records, list):
        raise RegistryContractError(
            f"invalid {source}: {collection} must be a list"
        )
    seen: set[str] = set()
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise RegistryContractError(
                f"invalid {source}: {collection}[{position}] must be an object"
            )
        record_id = record.get(key)
        if (
            not isinstance(record_id, str)
            or not record_id.strip()
            or record_id != record_id.strip()
        ):
            raise RegistryContractError(
                f"invalid {source}: {collection}[{position}].{key} must be one "
                "canonical nonblank string"
            )
        if record_id in seen:
            raise RegistryContractError(
                f"invalid {source}: duplicate {key} {record_id!r}"
            )
        seen.add(record_id)
    try:
        canonical_json_bytes(payload)
    except RegistryContractError as exc:
        raise RegistryContractError(f"invalid {source}: {exc}") from exc
    return records


def assert_integration_registry_contract(
    payload: Any,
    *,
    source: str,
) -> None:
    """Validate the complete structural contract without a whole-file pin."""

    routes = _assert_registry_document(
        payload,
        source=source,
        collection="routes",
        key="route_id",
    )
    if set(payload) != INTEGRATION_REGISTRY_FIELDS:
        raise RegistryContractError(
            f"invalid {source}: top-level fields must match the integration "
            "registry schema"
        )
    routing_defaults = payload.get("routing_defaults")
    if (
        not isinstance(routing_defaults, dict)
        or set(routing_defaults) != ROUTING_DEFAULT_FIELDS
    ):
        raise RegistryContractError(
            f"invalid {source}: routing_defaults fields must match the schema"
        )
    native_lane_kinds = set(
        _unique_canonical_string_list(
            routing_defaults.get("native_lane_kinds"),
            source=f"{source}.routing_defaults.native_lane_kinds",
            identifiers=True,
            nonempty=True,
        )
    )
    for position, route in enumerate(routes):
        validate_integration_route_record(
            route,
            source=f"{source}.routes[{position}]",
            native_lane_kinds=native_lane_kinds,
        )

    route_accounts: set[tuple[str, str]] = set()
    for route in routes:
        account_key = (route["system"], route["required_account"])
        if account_key in route_accounts:
            raise RegistryContractError(
                f"invalid {source}: system/account route identity must be exact; "
                f"duplicate {account_key!r}"
            )
        route_accounts.add(account_key)

    systems = {route["system"] for route in routes}
    system_aliases = payload.get("system_aliases")
    if not isinstance(system_aliases, dict):
        raise RegistryContractError(
            f"invalid {source}: system_aliases must be an object"
        )
    for alias, targets in system_aliases.items():
        _canonical_slug(alias, source=f"{source}.system_aliases key")
        parsed_targets = _unique_canonical_string_list(
            targets,
            source=f"{source}.system_aliases[{alias!r}]",
            slugs=True,
            nonempty=True,
        )
        unknown_targets = sorted(set(parsed_targets) - systems)
        if unknown_targets:
            raise RegistryContractError(
                f"invalid {source}.system_aliases[{alias!r}]: unknown system(s) "
                + ", ".join(unknown_targets)
            )

    portfolio_ids, account_ids = _validate_portfolios(
        payload.get("portfolios"),
        source=source,
        route_ids={route["route_id"] for route in routes},
    )
    _validate_credential_handles(
        payload.get("credential_handles"),
        source=source,
        account_ids=account_ids,
        route_ids={route["route_id"] for route in routes},
    )
    _validate_portfolio_routes(
        payload.get("portfolio_routes"),
        source=source,
        routes=routes,
        portfolio_ids=portfolio_ids,
    )


def assert_probe_registry_contract(
    payload: Any,
    *,
    source: str,
) -> None:
    """Validate probe document and every read-only probe record."""

    probes = _assert_registry_document(
        payload,
        source=source,
        collection="probes",
        key="probe_id",
    )
    for probe in probes:
        validate_probe_record(
            probe,
            source=f"{source} probe {probe.get('probe_id')!r}",
        )
