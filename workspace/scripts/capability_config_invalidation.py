#!/usr/bin/env python3
"""Report capability rows invalidated by later OpenClaw config changes."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.openclaw_cli_common import build_openclaw_env, resolve_openclaw_bin
except ModuleNotFoundError:
    from openclaw_cli_common import build_openclaw_env, resolve_openclaw_bin


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS = ROOT / "status" / "capability_status.json"
NATIVE_HISTORY_SOURCE = "gateway:openclaw.changes.list"
NATIVE_PAGE_LIMIT = 200
MAX_NATIVE_PAGES = 4
NATIVE_TIMEOUT_SECONDS = 30
NATIVE_COVERAGE_REASON = (
    "bounded, retained, best-effort native history cannot certify complete changes since verification"
)
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


def read_native_page(params: dict[str, Any]) -> dict[str, Any]:
    """Use the supported, value-redacted journal projection, never the raw store."""
    binary = resolve_openclaw_bin()
    if not binary:
        raise OSError("native_cli_unavailable")
    result = subprocess.run(
        [binary, "gateway", "call", "openclaw.changes.list", "--params", json.dumps(params), "--json"],
        capture_output=True, text=True, timeout=NATIVE_TIMEOUT_SECONDS, env=build_openclaw_env(),
    )
    # Child diagnostics and summaries are private, including failures during suspension.
    if result.returncode or len(result.stdout) > 4_000_000:
        raise ValueError("native_history_unavailable")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("native_history_invalid")
    return payload


def read_native_changes() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    record: dict[str, Any] = {
        "path": NATIVE_HISTORY_SOURCE, "source": "native_change_history", "exists": False,
        "pages": 0, "reload_lines": 0, "first_observed_at": None, "last_observed_at": None,
        "first_changed_at": None, "last_changed_at": None, "complete": False,
        "reason": NATIVE_COVERAGE_REASON, "stop_reason": "retained_history_exhausted",
    }
    timestamps: list[datetime] = []
    cursor = None
    seen_cursors: set[str] = set()
    for _ in range(MAX_NATIVE_PAGES):
        try:
            page = read_native_page({"limit": NATIVE_PAGE_LIMIT, **({"beforeCursor": cursor} if cursor else {})})
            if not isinstance(page, dict):
                raise ValueError("native_history_invalid")
            entries = page.get("entries")
            next_cursor = page.get("nextCursor")
            if (not isinstance(entries, list) or len(entries) > NATIVE_PAGE_LIMIT
                    or (next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor))):
                raise ValueError("native_history_invalid")
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            record["stop_reason"] = "native_history_unavailable"
            break
        record["exists"] = True
        record["pages"] += 1
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("kind"), str)
                    or entry["kind"] not in {"operation", "config-write", "external-edit"}):
                continue
            at = entry.get("at")
            if isinstance(at, bool) or not isinstance(at, (int, float)):
                continue
            try:
                if not math.isfinite(at):
                    continue
                timestamp = datetime.fromtimestamp(at / 1000, timezone.utc)
            except (ValueError, OverflowError, OSError):
                continue
            timestamps.append(timestamp)
            paths = entry.get("changedPaths")
            if not isinstance(paths, list):
                continue
            for key in paths:
                # The owner caps paths with this sentinel. Neither it nor summary prose is a key.
                if (not isinstance(key, str) or not key.strip() or key != key.strip()
                        or re.fullmatch(r"…\+\d+ more", key) or key in IGNORED_KEYS):
                    continue
                changes.append({"changed_at": timestamp, "changed_at_text": timestamp.isoformat().replace("+00:00", "Z"),
                                "key": key, "log": NATIVE_HISTORY_SOURCE})
        # Empty pages may still have a continuation after the native raw-scan budget.
        # Do not stop on timestamps: producer clocks and journal sequence can differ.
        if next_cursor is None:
            break
        if next_cursor in seen_cursors:
            record["stop_reason"] = "repeated_cursor"
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        record["stop_reason"] = "page_limit"
    if timestamps:
        record["first_observed_at"] = min(timestamps).isoformat().replace("+00:00", "Z")
        record["last_observed_at"] = max(timestamps).isoformat().replace("+00:00", "Z")
    if changes:
        record["first_changed_at"] = min(change["changed_at"] for change in changes).isoformat().replace("+00:00", "Z")
        record["last_changed_at"] = max(change["changed_at"] for change in changes).isoformat().replace("+00:00", "Z")
    changes.sort(key=lambda change: (change["changed_at"], change["key"], change["log"]))
    return changes, [record]


def report_invalidations(status_path: Path, log_paths: list[Path] | None = None) -> dict[str, Any]:
    status = json.loads(status_path.read_text(encoding="utf-8"))
    native = log_paths is None
    changes, coverage = read_native_changes() if native else read_changes(log_paths)
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
            not native and all_logs_present and earliest_observed and latest_observed
            and earliest_observed <= verified_at <= latest_observed
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
                else NATIVE_COVERAGE_REASON if native
                else "available historical log window does not include the capability verification time"
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
        "coverage_scope": "native_bounded_retained_history" if native else "historical_log_window",
        "log_coverage": coverage,
        "coverage_by_capability": coverage_by_capability,
        "coverage_complete": not native and all_logs_present and all(
            item["complete"] for item in coverage_by_capability
        ),
        "invalidated": invalidated,
        "unknown": unknown,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", action="append", type=Path, dest="logs",
                        help="Inspect an explicit historical log window instead of current native history")
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
            f"unknown={len(payload['unknown'])} log_coverage_gaps={gaps} "
            f"coverage_complete={str(payload['coverage_complete']).lower()} "
            f"coverage_scope={payload['coverage_scope']}"
        )
    if args.fail_on_invalidated and payload["invalidated"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
