#!/usr/bin/env python3
"""
Create a calendar event in Apple Calendar via AppleScript.
"""

from __future__ import annotations

import argparse
import json
import sys

from apple_calendar_common import applescript_date_lines, count_events_by_uid, ensure_calendar_running, escape_applescript_text, list_calendars, now_utc, osascript_path, parse_pipe, run_osa_lines


def create_event(title: str, start_iso: str, end_iso: str, calendar_name: str | None, location: str | None) -> tuple[dict | None, str | None]:
    escaped_title = escape_applescript_text(title)
    escaped_location = escape_applescript_text(location)
    cal_selector = f'calendar "{escape_applescript_text(calendar_name)}"' if calendar_name else 'first calendar'
    properties = f'{{summary:"{escaped_title}", start date:startDate, end date:endDate' + (f', location:"{escaped_location}"' if location else '') + '}'
    cp = run_osa_lines([
        'tell application "Calendar"',
        f'set targetCal to {cal_selector}',
        *applescript_date_lines('startDate', start_iso),
        *applescript_date_lines('endDate', end_iso),
        f'set ev to make new event at end of events of targetCal with properties {properties}',
        'return (name of targetCal as text) & "||" & (uid of ev as text) & "||" & (summary of ev as text)',
        'end tell',
    ])
    if cp.returncode != 0:
        return None, (cp.stderr or cp.stdout or "event creation failed").strip()
    parts = parse_pipe(cp.stdout)
    if len(parts) != 3:
        return None, f"unexpected create output: {cp.stdout!r}"
    read_count, count_err = count_events_by_uid(parts[1])
    if count_err:
        return None, count_err
    if read_count != 1:
        return None, f"event not persisted as expected; read_count={read_count}"
    return {"calendar": parts[0], "uid": parts[1], "title": parts[2], "start": start_iso, "end": end_iso, "location": location or "", "read_count_after_create": read_count}, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True, nargs="+")
    parser.add_argument("--start", required=True, help="Local datetime, e.g. 2026-03-26T13:00:00")
    parser.add_argument("--end", required=True, help="Local datetime, e.g. 2026-03-26T15:00:00")
    parser.add_argument("--calendar")
    parser.add_argument("--location")
    args = parser.parse_args()

    out: dict = {
        "checked_at_utc": now_utc(),
        "requested": {
            "title": " ".join(args.title),
            "start": args.start,
            "end": args.end,
            "calendar": args.calendar,
            "location": args.location,
        },
        "capabilities": {},
        "overall": "blocked",
        "errors": [],
    }

    osa_path = osascript_path()
    out["capabilities"]["osascript"] = {"status": "ready" if osa_path else "blocked", "path": osa_path}
    if not osa_path:
        out["errors"].append("osascript_not_found")
        print(json.dumps(out, indent=2))
        return 2

    launch_cp = ensure_calendar_running()
    if launch_cp.returncode != 0:
        out["capabilities"]["calendar_launch"] = {"status": "blocked", "error": (launch_cp.stderr or launch_cp.stdout or "unknown error").strip()}
        out["errors"].append("calendar_launch_failed")
        print(json.dumps(out, indent=2))
        return 2
    out["capabilities"]["calendar_launch"] = {"status": "ready"}

    calendars, list_err = list_calendars()
    if list_err:
        out["capabilities"]["calendar_list"] = {"status": "blocked", "error": list_err}
        out["errors"].append("calendar_list_failed")
        print(json.dumps(out, indent=2))
        return 2
    out["capabilities"]["calendar_list"] = {"status": "ready" if calendars else "degraded", "count": len(calendars), "calendars": calendars}

    target_calendar = args.calendar or (calendars[0] if calendars else None)
    created, create_err = create_event(" ".join(args.title), args.start, args.end, target_calendar, args.location)
    if create_err:
        out["capabilities"]["create_event"] = {"status": "blocked", "calendar": target_calendar, "error": create_err}
        out["errors"].append("create_event_failed")
        print(json.dumps(out, indent=2))
        return 2

    out["capabilities"]["create_event"] = {"status": "ready", **created}
    out["overall"] = "ready"
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
