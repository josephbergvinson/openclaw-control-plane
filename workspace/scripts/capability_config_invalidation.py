#!/usr/bin/env python3
"""Report capability rows invalidated by later OpenClaw config changes."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS = ROOT / "status" / "capability_status.json"
IGNORED_KEYS = {"meta.lastTouchedAt"}  # Written with every config update; not a capability input.
RELOAD_LINE = re.compile(
    r"^(?P<timestamp>\S+) \[reload\] config change detected; evaluating reload "
    r"\((?P<keys>[^)]*)\)\s*$"
)
LOG_LINE_TIMESTAMP = re.compile(r"^(?P<timestamp>\S+)")


def parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_key(value: str) -> str:
    return re.sub(r"\.+", ".", re.sub(r"\[([^\]]+)\]", r".\1", value.strip())).strip(".")


def keys_overlap(changed_key: str, dependency: str) -> bool:
    changed = normalize_key(changed_key)
    required = normalize_key(dependency)
    return bool(
        changed
        and required
        and (
            changed == required
            or changed.startswith(required + ".")
            or required.startswith(changed + ".")
        )
    )


def read_changes(log_paths: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for path in log_paths:
        record: dict[str, Any] = {
            "path": str(path),
            "exists": path.is_file(),
            "reload_lines": 0,
            "first_observed_at": None,
            "last_observed_at": None,
            "first_changed_at": None,
            "last_changed_at": None,
        }
        if not path.is_file():
            coverage.append(record)
            continue
        observed_timestamps: list[tuple[datetime, str]] = []
        changed_timestamps: list[tuple[datetime, str]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            timestamp_match = LOG_LINE_TIMESTAMP.match(line)
            if timestamp_match:
                observed_at_text = timestamp_match.group("timestamp")
                observed_at = parse_timestamp(observed_at_text)
                if observed_at is not None:
                    observed_timestamps.append((observed_at, observed_at_text))
            match = RELOAD_LINE.match(line)
            if not match:
                continue
            changed_at = parse_timestamp(match.group("timestamp"))
            if changed_at is None:
                continue
            keys = [key.strip() for key in match.group("keys").split(",") if key.strip()]
            for key in keys:
                if key in IGNORED_KEYS:
                    continue
                changes.append(
                    {
                        "changed_at": changed_at,
                        "changed_at_text": match.group("timestamp"),
                        "key": key,
                        "log": str(path),
                    }
                )
            record["reload_lines"] += 1
            changed_timestamps.append((changed_at, match.group("timestamp")))
        if observed_timestamps:
            observed_timestamps.sort(key=lambda item: item[0])
            record["first_observed_at"] = observed_timestamps[0][1]
            record["last_observed_at"] = observed_timestamps[-1][1]
        if changed_timestamps:
            changed_timestamps.sort(key=lambda item: item[0])
            record["first_changed_at"] = changed_timestamps[0][1]
            record["last_changed_at"] = changed_timestamps[-1][1]
        coverage.append(record)
    changes.sort(key=lambda change: (change["changed_at"], change["key"], change["log"]))
    return changes, coverage


def report_invalidations(status_path: Path, log_paths: list[Path]) -> dict[str, Any]:
    status = json.loads(status_path.read_text(encoding="utf-8"))
    changes, coverage = read_changes(log_paths)
    invalidated: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    coverage_by_capability: list[dict[str, Any]] = []
    observed_bounds = [
        (
            parse_timestamp(item["first_observed_at"]),
            parse_timestamp(item["last_observed_at"]),
        )
        for item in coverage
        if item["exists"] and item["first_observed_at"] and item["last_observed_at"]
    ]
    earliest_observed = min((start for start, _end in observed_bounds if start), default=None)
    latest_observed = max((end for _start, end in observed_bounds if end), default=None)
    all_logs_present = bool(coverage) and all(item["exists"] for item in coverage)
    for capability in status.get("capabilities", []):
        if "screen_capture_binding" in capability:
            try:
                try:
                    from scripts.openclaw_runtime_activate import verify_screen_capture_capability
                except ModuleNotFoundError:
                    from openclaw_runtime_activate import verify_screen_capture_capability
                verify_screen_capture_capability(capability["screen_capture_binding"])
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                invalidated.append({
                    "capability_id": capability.get("capability_id"),
                    "domain": capability.get("domain", "unknown"),
                    "last_verified_utc": capability.get("last_verified_utc"),
                    "invalidating_changes": [], "invalidated": True,
                    "reason": "ScreenCapture native identity, permission or process evidence is not current",
                })
                continue
        dependencies = capability.get("config_dependencies", [])
        if not isinstance(dependencies, list) or not dependencies:
            continue
        verified_at = parse_timestamp(capability.get("last_verified_utc", ""))
        if verified_at is None:
            continue
        history_reaches_verification = bool(
            all_logs_present and earliest_observed and earliest_observed <= verified_at
        )
        matches: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for change in changes:
            if change["changed_at"] <= verified_at:
                continue
            for dependency in dependencies:
                if isinstance(dependency, str) and keys_overlap(change["key"], dependency):
                    identity = (change["changed_at_text"], change["key"], change["log"])
                    if identity not in seen:
                        seen.add(identity)
                        matches.append(
                            {
                                "changed_at": change["changed_at_text"],
                                "key": change["key"],
                                "log": change["log"],
                            }
                        )
                    break
        if matches:
            invalidated.append(
                {
                    "capability_id": capability["capability_id"],
                    "domain": capability.get("domain", "unknown"),
                    "last_verified_utc": capability.get("last_verified_utc"),
                    "invalidating_changes": matches,
                    "invalidated": True,
                }
            )
        capability_coverage = {
            "capability_id": capability["capability_id"],
            "last_verified_utc": capability.get("last_verified_utc"),
            "earliest_log_timestamp": (
                earliest_observed.isoformat().replace("+00:00", "Z")
                if earliest_observed
                else None
            ),
            "latest_log_timestamp": (
                latest_observed.isoformat().replace("+00:00", "Z") if latest_observed else None
            ),
            "complete": history_reaches_verification,
            "reason": (
                None
                if history_reaches_verification
                else "available log history does not reach the capability verification time"
            ),
        }
        coverage_by_capability.append(capability_coverage)
        if not matches and not history_reaches_verification:
            unknown.append(
                {
                    "capability_id": capability["capability_id"],
                    "domain": capability.get("domain", "unknown"),
                    "last_verified_utc": capability.get("last_verified_utc"),
                    "reason": capability_coverage["reason"],
                }
            )
    return {
        "schema": "openclaw.capability_config_invalidation.v1",
        "status": str(status_path),
        "log_coverage": coverage,
        "coverage_by_capability": coverage_by_capability,
        "coverage_complete": all_logs_present and all(
            item["complete"] for item in coverage_by_capability
        ),
        "invalidated": invalidated,
        "unknown": unknown,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", action="append", type=Path, dest="logs", required=True)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-invalidated", action="store_true")
    args = parser.parse_args()
    payload = report_invalidations(args.status, args.logs)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        gaps = sum(not item["exists"] for item in payload["log_coverage"])
        print(
            f"CAPABILITY_CONFIG_INVALIDATION_OK count={len(payload['invalidated'])} "
            f"unknown={len(payload['unknown'])} log_coverage_gaps={gaps}"
        )
    if args.fail_on_invalidated and payload["invalidated"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
