#!/usr/bin/env python3
"""Business-effect predicates for scheduled ACTION jobs.

A successful process exit proves only that the process did not crash. These
predicates read an authority outside the producing step and assert that the
intended effect is observable there.
"""

from __future__ import annotations

import json
from typing import Any


CONTRACT_VERSION = "operation.effect_predicate.v1"

REASONS = (
    "satisfied",
    "observed_value_unavailable",
    "no_observed_advance",
    "count_did_not_decrease",
    "count_did_not_shrink_by_expected",
    "object_absent",
    "readback_mismatch",
    "threshold_exceeded",
    "financial_daily_sync_failed",
    "plaid_authorization_keeper_failed",
    "wallet_preflight_failed",
    "wallet_daily_authority_absent",
)


def predicate(
    *,
    name: str,
    satisfied: bool,
    reason: str,
    observed_value: Any = None,
    threshold: Any = None,
    fail_closed: bool = False,
    detail: str | None = None,
) -> dict[str, Any]:
    if reason not in REASONS:
        raise ValueError(f"unknown business-effect reason: {reason}")
    return {
        "schema": CONTRACT_VERSION,
        "name": name,
        "satisfied": bool(satisfied),
        "reason": reason,
        "observed_value": observed_value,
        "threshold": threshold,
        "fail_closed": bool(fail_closed),
        "detail": detail,
    }


def unreadable(name: str, detail: str) -> dict[str, Any]:
    return predicate(
        name=name,
        satisfied=False,
        reason="observed_value_unavailable",
        fail_closed=True,
        detail=detail,
    )


def observed_advance(name: str, before: Any, after: Any) -> dict[str, Any]:
    if before is None or after is None:
        return unreadable(name, "before and after observations are required")
    try:
        satisfied = after > before
    except TypeError:
        return unreadable(name, "before and after observations are not comparable")
    return predicate(
        name=name,
        satisfied=satisfied,
        reason="satisfied" if satisfied else "no_observed_advance",
        observed_value={"before": before, "after": after},
        threshold={"operator": ">", "before": before},
    )


def count_decreased(name: str, before: Any, after: Any) -> dict[str, Any]:
    if before is None or after is None:
        return unreadable(name, "before and after counts are required")
    if not isinstance(before, int) or isinstance(before, bool) or not isinstance(after, int) or isinstance(after, bool):
        return unreadable(name, "before and after counts must be integers")
    satisfied = after < before
    return predicate(
        name=name,
        satisfied=satisfied,
        reason="satisfied" if satisfied else "count_did_not_decrease",
        observed_value={"before": before, "after": after},
        threshold={"operator": "<", "before": before},
    )


def count_shrank_by(name: str, before: Any, after: Any, expected_delta: Any) -> dict[str, Any]:
    if before is None or after is None or expected_delta is None:
        return unreadable(name, "before, after, and expected_delta are required")
    values = (before, after, expected_delta)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        return unreadable(name, "counts and expected_delta must be integers")
    satisfied = before - after == expected_delta
    return predicate(
        name=name,
        satisfied=satisfied,
        reason="satisfied" if satisfied else "count_did_not_shrink_by_expected",
        observed_value={"before": before, "after": after, "delta": before - after},
        threshold={"expected_delta": expected_delta},
    )


def object_present(name: str, path_or_id: Any, present: Any, readable: Any) -> dict[str, Any]:
    if path_or_id is None or present is None or readable is None:
        return unreadable(name, "object identity, presence, and readability are required")
    satisfied = bool(present) and bool(readable)
    detail = None
    if not present:
        detail = f"object not present: {path_or_id}"
    elif not readable:
        detail = f"object not readable: {path_or_id}"
    return predicate(
        name=name,
        satisfied=satisfied,
        reason="satisfied" if satisfied else "object_absent",
        observed_value={"id": path_or_id, "present": bool(present), "readable": bool(readable)},
        threshold={"present": True, "readable": True},
        detail=detail,
    )


def readback_matches(name: str, expected: Any, actual: Any) -> dict[str, Any]:
    if expected is None or actual is None:
        return unreadable(name, "expected and actual readback values are required")
    satisfied = actual == expected
    return predicate(
        name=name,
        satisfied=satisfied,
        reason="satisfied" if satisfied else "readback_mismatch",
        observed_value=actual,
        threshold=expected,
        detail=None if satisfied else f"expected {expected!r}, observed {actual!r}",
    )


def emit(results: list[dict[str, Any]], *, what: str) -> int:
    first_unsatisfied = next((result for result in results if not result.get("satisfied")), None)
    outcome = "effect_unsatisfied" if first_unsatisfied else "effect_satisfied"
    predicates = ",".join(
        f"{result.get('name')}={str(bool(result.get('satisfied'))).lower()}" for result in results
    )
    reason = str((first_unsatisfied or {}).get("reason") or "satisfied")
    detail = str((first_unsatisfied or {}).get("detail") or "none")
    print(
        f"STATUS | what: {what} | result: {outcome} | predicates: {predicates} "
        f"| reason: {reason} | next: {detail}"
    )
    print("EFFECT_PREDICATES " + json.dumps(results, sort_keys=True, separators=(",", ":")))
    return 4 if first_unsatisfied else 0
