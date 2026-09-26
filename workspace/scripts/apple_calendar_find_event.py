#!/usr/bin/env python3
"""
Find Apple Calendar events by uid and/or title/date window.
"""

from __future__ import annotations

import argparse
import json
import sys

from apple_calendar_common import (
    CALENDAR_LAUNCH_TIMEOUT_SECONDS,
    CALENDAR_READ_TIMEOUT_SECONDS,
    applescript_safe_text_handler_lines,
    applescript_read_date_lines,
    ensure_calendar_running,
    run_calendar_cli,
    escape_applescript_text,
    now_utc,
    osascript_path,
    parse_calendar_read_datetime,
    parse_lines,
    run_osa_lines,
)


ERROR_PREFIX = "__OPENCLAW_CALENDAR_ERROR__"


def validate_query(args: argparse.Namespace) -> str | None:
    has_from = bool(args.from_date)
    has_to = bool(args.to_date)
    if has_from != has_to:
        return "date_window_requires_from_and_to"
    if not (args.uid or args.title or (has_from and has_to)):
        return "query_required"
    if has_from and has_to:
        try:
            start = parse_calendar_read_datetime(args.from_date)
            end = parse_calendar_read_datetime(args.to_date)
        except (TypeError, ValueError):
            return "invalid_date_window"
        if start >= end:
            return "invalid_date_window"
    return None


def candidate_event_line(args: argparse.Namespace) -> str:
    if args.from_date and args.to_date:
        return "set candidateEvents to (every event of cal whose start date < targetTo and end date > targetFrom)"
    if args.uid:
        return "set candidateEvents to (every event of cal whose uid is targetUid)"
    return "set candidateEvents to (every event of cal whose summary is targetTitle)"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid")
    parser.add_argument("--title", nargs="+")
    parser.add_argument("--from-date", dest="from_date")
    parser.add_argument("--to-date", dest="to_date")
    args = parser.parse_args()

    out = {
        "checked_at_utc": now_utc(),
        "query": {
            "uid": args.uid,
            "title": " ".join(args.title) if args.title else None,
            "from_date": args.from_date,
            "to_date": args.to_date,
        },
        "capabilities": {},
        "events": [],
        "overall": "blocked",
        "errors": [],
    }

    validation_error = validate_query(args)
    if validation_error:
        out["errors"].append(validation_error)
        print(json.dumps(out, indent=2))
        return 2

    osa_path = osascript_path()
    out["capabilities"]["osascript"] = {"status": "ready" if osa_path else "blocked", "path": osa_path}
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
            "error": (launch_cp.stderr or launch_cp.stdout or "unknown").strip(),
        }
        out["errors"].append("calendar_launch_timed_out" if timed_out else "calendar_launch_failed")
        print(json.dumps(out, indent=2))
        return 2
    out["capabilities"]["calendar_launch"] = {"status": "ready"}

    filter_lines = []
    if args.uid:
        filter_lines.append(f'set targetUid to "{escape_applescript_text(args.uid)}"')
    if args.title:
        filter_lines.append(f'set targetTitle to "{escape_applescript_text(" ".join(args.title))}"')
    if args.from_date:
        filter_lines.extend(applescript_read_date_lines('targetFrom', args.from_date))
    if args.to_date:
        filter_lines.extend(applescript_read_date_lines('targetTo', args.to_date))

    match_lines = ["set matchesRequestedFields to true"]
    if args.uid:
        match_lines.append("set matchesRequestedFields to matchesRequestedFields and ((uid of ev as text) is targetUid)")
    if args.title:
        match_lines.append("set matchesRequestedFields to matchesRequestedFields and ((summary of ev as text) is targetTitle)")

    cp = run_osa_lines([
        *applescript_safe_text_handler_lines(),
        'tell application "Calendar"',
        *filter_lines,
        'set outLines to {}',
        'repeat with cal in calendars',
        'set calName to "<unknown>"',
        'try',
        'set calName to name of cal as text',
        candidate_event_line(args),
        'repeat with ev in candidateEvents',
        *match_lines,
        'if matchesRequestedFields then',
        'set d to description of ev',
        'set loc to location of ev',
        'set end of outLines to ((my safeText(calName)) & "||" & (my safeText(uid of ev)) & "||" & (my safeText(summary of ev)) & "||" & (my safeText(start date of ev)) & "||" & (my safeText(end date of ev)) & "||" & (my safeText(loc)) & "||" & (my safeText(d)))',
        'end if',
        'end repeat',
        'on error errMsg number errNum',
        f'set end of outLines to ("{ERROR_PREFIX}||" & (my safeText(calName)) & "||" & (errNum as text) & "||" & (my safeText(errMsg)))',
        'end try',
        'end repeat',
        "set AppleScript's text item delimiters to linefeed",
        'set outText to outLines as text',
        "set AppleScript's text item delimiters to \"\"",
        'return outText',
        'end tell',
    ], timeout_seconds=CALENDAR_READ_TIMEOUT_SECONDS)
    if cp.returncode != 0:
        timed_out = cp.returncode == 124
        out["errors"].append("event_query_timed_out" if timed_out else "event_query_failed")
        out["capabilities"]["event_query"] = {
            "status": "blocked",
            "timed_out": timed_out,
            "error": (cp.stderr or cp.stdout or "unknown").strip(),
        }
        print(json.dumps(out, indent=2))
        return 2

    events = []
    calendar_errors = []
    for line in parse_lines(cp.stdout):
        if line.startswith(f"{ERROR_PREFIX}||"):
            parts = line.split("||", 3)
            calendar_errors.append({
                "calendar": parts[1] if len(parts) > 1 else "<unknown>",
                "number": parts[2] if len(parts) > 2 else "unknown",
                "error": parts[3] if len(parts) > 3 else "calendar query failed",
            })
            continue
        parts = line.split("||", 6)
        if len(parts) != 7:
            calendar_errors.append({"calendar": "<unknown>", "number": "parse", "error": "malformed event row"})
            continue
        events.append({"calendar": parts[0], "uid": parts[1], "title": parts[2], "start": parts[3], "end": parts[4], "location": parts[5], "description": parts[6]})

    out["events"] = events
    if calendar_errors:
        out["capabilities"]["event_query"] = {
            "status": "blocked",
            "count": len(events),
            "complete": False,
            "calendar_errors": calendar_errors,
        }
        out["errors"].append("calendar_query_incomplete")
        print(json.dumps(out, indent=2))
        return 2

    out["capabilities"]["event_query"] = {
        "status": "ready",
        "count": len(events),
        "complete": True,
    }
    out["overall"] = "ready"
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(run_calendar_cli(main))
