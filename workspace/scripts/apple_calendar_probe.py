#!/usr/bin/env python3
"""
Read-only capability probe for local Apple Calendar access on macOS.
"""

from __future__ import annotations

import json
import sys

from apple_calendar_common import (
    CALENDAR_LAUNCH_TIMEOUT_SECONDS,
    CALENDAR_READ_TIMEOUT_SECONDS,
    ensure_calendar_running,
    now_utc,
    osascript_path,
    parse_pipe,
    run_osa_lines,
)

def main() -> int:
    out: dict = {
        "checked_at_utc": now_utc(),
        "capabilities": {},
        "overall": "blocked",
        "errors": [],
    }

    osa_path = osascript_path()
    out["capabilities"]["osascript"] = {
        "status": "ready" if osa_path else "blocked",
        "path": osa_path,
    }
    if not osa_path:
        out["errors"].append("osascript_not_found")
        print(json.dumps(out, indent=2))
        return 2

    launch_cp = ensure_calendar_running(
        timeout_seconds=CALENDAR_LAUNCH_TIMEOUT_SECONDS,
    )
    if launch_cp.returncode != 0:
        timed_out = launch_cp.returncode == 124
        out["capabilities"]["calendar_launch"] = {
            "status": "blocked",
            "timed_out": timed_out,
            "error": (launch_cp.stderr or launch_cp.stdout or "unknown error").strip(),
        }
        out["errors"].append("calendar_launch_timed_out" if timed_out else "calendar_launch_failed")
        print(json.dumps(out, indent=2))
        return 2
    out["capabilities"]["calendar_launch"] = {"status": "ready"}

    # Calendar discovery and readability come from one object snapshot. A
    # separate name query can race account/calendar changes and falsely report
    # a complete read.
    count_cp = run_osa_lines([
        'tell application "Calendar"',
        'set rows to {}',
        'repeat with cal in calendars',
        'set calName to ""',
        'try',
        'set calName to name of cal as text',
        'if calName contains "||" or calName contains return or calName contains linefeed then error number -1700',
        'set evs to (every event of cal whose start date ≥ (current date) and start date < ((current date) + 7 * days))',
        'set end of rows to (calName & ":" & (count of evs as text))',
        'on error',
        'if calName is "" or calName contains "||" or calName contains return or calName contains linefeed then',
        'set end of rows to "__OPENCLAW_CALENDAR_ROW_ERROR__"',
        'else',
        'set end of rows to (calName & ":ERR")',
        'end if',
        'end try',
        'end repeat',
        "set AppleScript's text item delimiters to \"||\"",
        'set outText to rows as text',
        "set AppleScript's text item delimiters to \"\"",
        'return outText',
        'end tell',
    ], timeout_seconds=CALENDAR_READ_TIMEOUT_SECONDS)
    if count_cp.returncode != 0:
        timed_out = count_cp.returncode == 124
        diagnostic = (count_cp.stderr or count_cp.stdout or "unknown error").strip()
        out["capabilities"]["calendar_list"] = {
            "status": "blocked",
            "timed_out": timed_out,
            "error": diagnostic,
        }
        out["capabilities"]["calendar_read_next_7d"] = {
            "status": "blocked",
            "timed_out": timed_out,
            "error": diagnostic,
        }
        out["errors"].append("calendar_snapshot_timed_out" if timed_out else "calendar_snapshot_failed")
        print(json.dumps(out, indent=2))
        return 2

    rows = parse_pipe(count_cp.stdout)
    calendars: list[str] = []
    parsed: dict[str, dict] = {}
    readable = 0
    total_events = 0
    structurally_complete = True
    for row in rows:
        if row == "__OPENCLAW_CALENDAR_ROW_ERROR__":
            structurally_complete = False
            continue
        if ":" not in row:
            structurally_complete = False
            continue
        name, val = row.rsplit(":", 1)
        name = name.strip()
        val = val.strip()
        if not name or name in parsed:
            structurally_complete = False
            continue
        calendars.append(name)
        if val == "ERR":
            parsed[name] = {"status": "error"}
        else:
            try:
                n = int(val)
                parsed[name] = {"status": "ready", "events_next_7d": n}
                readable += 1
                total_events += n
            except ValueError:
                parsed[name] = {"status": "error"}
                structurally_complete = False

    complete = (
        structurally_complete
        and len(calendars) > 0
        and readable == len(calendars)
    )
    out["capabilities"]["calendar_list"] = {
        "status": "ready" if complete else "blocked",
        "count": len(calendars),
        "calendars": calendars,
    }
    out["capabilities"]["calendar_read_next_7d"] = {
        "status": "ready" if complete else "blocked",
        "readable_calendars": readable,
        "total_events_next_7d": total_events,
        "calendar_event_counts": parsed,
    }
    if not complete:
        out["errors"].append("calendar_read_incomplete")
    out["overall"] = "ready" if complete else "blocked"
    print(json.dumps(out, indent=2))
    return 0 if out["overall"] == "ready" else 2


if __name__ == "__main__":
    sys.exit(main())
