#!/usr/bin/env python3
"""
Create a calendar event in Apple Calendar via AppleScript.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from apple_calendar_common import (
    CALENDAR_LAUNCH_TIMEOUT_SECONDS,
    CALENDAR_READ_TIMEOUT_SECONDS,
    applescript_date_lines,
    ensure_calendar_running,
    escape_applescript_text,
    list_calendars,
    now_utc,
    osascript_path,
    run_osa_lines,
)


def validate_request(title: str, start_iso: str, end_iso: str) -> None:
    if not title.strip():
        raise ValueError("title_required")
    # Preserve the established write contract: supplied components are host-local
    # wall time. Source offsets must be converted before invoking this helper.
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).replace(tzinfo=None)
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00")).replace(tzinfo=None)
    if end <= start or start.microsecond or end.microsecond:
        raise ValueError("invalid_local_date_window")


def target_lines(calendar_name: str, start_iso: str, end_iso: str) -> list[str]:
    return [
        'considering case',
        'tell application "Calendar"',
        f'set matches to every calendar whose name is "{escape_applescript_text(calendar_name)}"',
        'if (count of matches) is not 1 then error "target_calendar_not_unique"',
        'set targetCal to item 1 of matches',
        f'if (name of targetCal as text) is not "{escape_applescript_text(calendar_name)}" then error "target_calendar_name_mismatch"',
        *applescript_date_lines('startDate', start_iso),
        *applescript_date_lines('endDate', end_iso),
    ]


def create_event(
    title: str, start_iso: str, end_iso: str, calendar_name: str | None,
    location: str | None, description: str | None = None, join_url: str | None = None,
) -> tuple[dict | None, str | None]:
    try:
        validate_request(title, start_iso, end_iso)
    except ValueError as exc:
        return None, str(exc)
    if not calendar_name:
        return None, "target_calendar_required"
    fields = {"location": location, "description": description, "url": join_url}
    properties = f'{{summary:"{escape_applescript_text(title)}", start date:startDate, end date:endDate'
    for field, value in fields.items():
        if value is not None:
            properties += f', {field}:"{escape_applescript_text(value)}"'
    properties += '}'
    lines = target_lines(calendar_name, start_iso, end_iso)
    lines += [
        f'set matchingEvents to every event of targetCal whose summary is "{escape_applescript_text(title)}" and start date is startDate and end date is endDate',
        'if (count of matchingEvents) > 1 then error "matching_event_not_unique"',
        'if (count of matchingEvents) is 1 then',
        'set ev to item 1 of matchingEvents',
        'set outcome to "reused"',
        'else',
        f'set ev to make new event at end of events of targetCal with properties {properties}',
        'set outcome to "created"',
        'end if',
        'return outcome & "||" & (uid of ev as text)',
        'end tell',
        'end considering',
    ]
    cp = run_osa_lines(lines, timeout_seconds=CALENDAR_READ_TIMEOUT_SECONDS)
    if cp.returncode != 0:
        # A timed-out Apple event may already have committed. Never retry here.
        return {"mutation_outcome": "unverified"}, (
            (cp.stderr or cp.stdout or "event creation failed").strip()
            + "; reconcile the exact calendar/title/start/end before retrying"
        )
    parts = cp.stdout.strip().split("||")
    if len(parts) != 2 or parts[0] not in {"created", "reused"} or not parts[1].strip():
        return {"mutation_outcome": "unverified"}, "unexpected create receipt; reconcile before retrying"
    outcome, uid = parts[0], parts[1].strip()
    result = {"calendar": calendar_name, "uid": uid, "mutation_outcome": outcome}
    readback = target_lines(calendar_name, start_iso, end_iso) + [
        f'set matchingEvents to every event of targetCal whose uid is "{escape_applescript_text(uid)}"',
        'if (count of matchingEvents) is not 1 then error "readback_uid_not_unique"',
        'set ev to item 1 of matchingEvents',
        f'if (summary of ev as text) is not "{escape_applescript_text(title)}" then error "readback_title_mismatch"',
        'if (start date of ev) is not startDate then error "readback_start_mismatch"',
        'if (end date of ev) is not endDate then error "readback_end_mismatch"',
    ]
    for field, value in fields.items():
        if value is not None:
            readback += [
                f'set actualValue to {field} of ev',
                'if actualValue is missing value then set actualValue to ""',
                f'if (actualValue as text) is not "{escape_applescript_text(value)}" then error "readback_{field}_mismatch"',
            ]
    readback += ['return "verified"', 'end tell', 'end considering']
    verified = run_osa_lines(readback, timeout_seconds=CALENDAR_READ_TIMEOUT_SECONDS)
    if verified.returncode != 0 or verified.stdout.strip() != "verified":
        return result, (
            (verified.stderr or "event readback did not verify all requested fields").strip()
            + "; preserve this UID and reconcile it; do not create a replacement"
        )
    result.update({
        "title": title, "start": start_iso, "end": end_iso,
        "verified_fields": ["title", "start", "end", *[key for key, value in fields.items() if value is not None]],
        "read_count_after_create": 1,
        **{key: value for key, value in fields.items() if value is not None},
    })
    return result, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True, nargs="+")
    parser.add_argument("--start", required=True, help="Local datetime, e.g. 2026-03-26T13:00:00")
    parser.add_argument("--end", required=True, help="Local datetime, e.g. 2026-03-26T15:00:00")
    parser.add_argument("--calendar")
    parser.add_argument("--location")
    parser.add_argument("--description", help="Source joining notes or dial-in details")
    parser.add_argument("--url", dest="join_url", help="Verified meeting joining URL")
    args = parser.parse_args()

    out: dict = {
        "checked_at_utc": now_utc(),
        "requested": {
            "title": " ".join(args.title),
            "start": args.start,
            "end": args.end,
            "calendar": args.calendar,
            "location": args.location,
            "description": args.description,
            "url": args.join_url,
        },
        "capabilities": {},
        "overall": "blocked",
        "errors": [],
    }

    try:
        validate_request(" ".join(args.title), args.start, args.end)
    except ValueError as exc:
        out["errors"].append(str(exc))
        print(json.dumps(out, indent=2))
        return 2

    osa_path = osascript_path()
    out["capabilities"]["osascript"] = {"status": "ready" if osa_path else "blocked", "path": osa_path}
    if not osa_path:
        out["errors"].append("osascript_not_found")
        print(json.dumps(out, indent=2))
        return 2

    launch_cp = ensure_calendar_running(timeout_seconds=CALENDAR_LAUNCH_TIMEOUT_SECONDS)
    if launch_cp.returncode != 0:
        out["capabilities"]["calendar_launch"] = {"status": "blocked", "error": (launch_cp.stderr or launch_cp.stdout or "unknown error").strip()}
        out["errors"].append("calendar_launch_failed")
        print(json.dumps(out, indent=2))
        return 2
    out["capabilities"]["calendar_launch"] = {"status": "ready"}

    calendars, list_err = list_calendars(timeout_seconds=CALENDAR_READ_TIMEOUT_SECONDS)
    if list_err:
        out["capabilities"]["calendar_list"] = {"status": "blocked", "error": list_err}
        out["errors"].append("calendar_list_failed")
        print(json.dumps(out, indent=2))
        return 2
    out["capabilities"]["calendar_list"] = {"status": "ready" if calendars else "degraded", "count": len(calendars), "calendars": calendars}

    target_calendar = args.calendar or (calendars[0] if calendars else None)
    created, create_err = create_event(" ".join(args.title), args.start, args.end, target_calendar, args.location, args.description, args.join_url)
    if create_err:
        out["capabilities"]["create_event"] = {"status": "blocked", "calendar": target_calendar, **(created or {}), "error": create_err}
        out["errors"].append("create_event_failed")
        print(json.dumps(out, indent=2))
        return 2

    out["capabilities"]["create_event"] = {"status": "ready", **created}
    out["overall"] = "ready"
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
