#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Sequence


CALENDAR_APP = "Calendar"
CALENDAR_LAUNCH_TIMEOUT_SECONDS = 5.0
# Complete Calendar.app reads routinely take 10-15 seconds on this host even
# when the app is healthy. Leave enough bounded headroom for concurrent agent
# work instead of turning ordinary scheduler pressure into a false outage.
CALENDAR_READ_TIMEOUT_SECONDS = 30.0


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timeout_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_command(
    argv: Sequence[str],
    *,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess:
    command = list(argv)
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_text(exc.stdout)
        captured_stderr = _timeout_text(exc.stderr).strip()
        timeout_label = f"{timeout_seconds:g}" if timeout_seconds is not None else "unknown"
        message = f"command timed out after {timeout_label}s"
        stderr = f"{captured_stderr}\n{message}" if captured_stderr else message
        return subprocess.CompletedProcess(command, 124, stdout=stdout, stderr=stderr)


def run_osa(
    script: str,
    *,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess:
    return run_command(
        ["osascript", "-e", script],
        timeout_seconds=timeout_seconds,
    )


def run_osa_lines(
    lines: list[str],
    *,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess:
    cmd = ["osascript"]
    for line in lines:
        cmd.extend(["-e", line])
    return run_command(cmd, timeout_seconds=timeout_seconds)


def osascript_path() -> str | None:
    return shutil.which("osascript")


def ensure_calendar_running(
    delay_seconds: int = 2,
    *,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess:
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    launch_cp = run_command(
        ["open", "-a", CALENDAR_APP],
        timeout_seconds=timeout_seconds,
    )
    if launch_cp.returncode != 0:
        return launch_cp
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
        return subprocess.CompletedProcess(
            ["osascript"],
            124,
            stdout="",
            stderr=f"calendar launch timed out after {timeout_seconds:g}s",
        )
    return run_osa_lines([
        f'delay {delay_seconds}',
        f'tell application "{CALENDAR_APP}"',
        'activate',
        'end tell',
    ], timeout_seconds=remaining)


def parse_pipe(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    return [p.strip() for p in text.split("||") if p.strip()]


def parse_lines(raw: str) -> list[str]:
    return [line for line in (raw or "").splitlines() if line.strip()]


def escape_applescript_text(value: str | None) -> str:
    return (value or "").replace('\\', '\\\\').replace('"', '\\"')


def applescript_safe_text_handler_lines() -> list[str]:
    return [
        "on replaceText(sourceText, needle, replacement)",
        "set oldDelimiters to AppleScript's text item delimiters",
        "set AppleScript's text item delimiters to needle",
        "set sourceParts to text items of sourceText",
        "set AppleScript's text item delimiters to replacement",
        "set rendered to sourceParts as text",
        "set AppleScript's text item delimiters to oldDelimiters",
        "return rendered",
        "end replaceText",
        "on safeText(valueText)",
        'if valueText is missing value then return ""',
        "set rendered to valueText as text",
        'set rendered to my replaceText(rendered, "||", "¦¦")',
        'set rendered to my replaceText(rendered, return, " ")',
        'set rendered to my replaceText(rendered, linefeed, " ")',
        "return rendered",
        "end safeText",
    ]


def parse_calendar_read_datetime(iso_value: str) -> datetime:
    normalized = iso_value[:-1] + "+00:00" if iso_value.endswith("Z") else iso_value
    dt = datetime.fromisoformat(normalized)
    return dt.astimezone()


def applescript_date_lines(var_name: str, iso_value: str) -> list[str]:
    """Render the pre-existing wall-clock contract used by Calendar writes."""
    dt = datetime.fromisoformat(iso_value)
    return _applescript_date_component_lines(var_name, dt)


def applescript_read_date_lines(var_name: str, iso_value: str) -> list[str]:
    """Render an absolute read boundary in the host's local Calendar timezone."""
    return _applescript_date_component_lines(
        var_name,
        parse_calendar_read_datetime(iso_value),
    )


def _applescript_date_component_lines(
    var_name: str,
    dt: datetime,
) -> list[str]:
    month_name = dt.strftime("%B")
    return [
        f'set {var_name} to current date',
        # Avoid AppleScript normalizing an invalid intermediate date (for
        # example, changing August 31 directly to February).
        f'set day of {var_name} to 1',
        f'set year of {var_name} to {dt.year}',
        f'set month of {var_name} to {month_name}',
        f'set day of {var_name} to {dt.day}',
        f'set time of {var_name} to {dt.hour * 3600 + dt.minute * 60 + dt.second}',
    ]


def list_calendars(
    *,
    timeout_seconds: float | None = None,
) -> tuple[list[str], str | None]:
    cp = run_osa_lines([
        f'tell application "{CALENDAR_APP}"',
        'set calNames to name of every calendar',
        "set AppleScript's text item delimiters to \"||\"",
        'set outText to calNames as text',
        "set AppleScript's text item delimiters to \"\"",
        'return outText',
        'end tell',
    ], timeout_seconds=timeout_seconds)
    if cp.returncode != 0:
        return [], (cp.stderr or cp.stdout or "calendar listing failed").strip()
    return parse_pipe(cp.stdout), None


def count_events_by_uid(uid: str) -> tuple[int | None, str | None]:
    cp = run_osa_lines([
        f'tell application "{CALENDAR_APP}"',
        f'set targetUid to "{escape_applescript_text(uid)}"',
        'set foundCount to 0',
        'repeat with cal in calendars',
        'try',
        'set evs to (every event of cal whose uid is targetUid)',
        'set foundCount to foundCount + (count of evs)',
        'end try',
        'end repeat',
        'return foundCount as text',
        'end tell',
    ])
    if cp.returncode != 0:
        return None, (cp.stderr or cp.stdout or "uid count failed").strip()
    try:
        return int((cp.stdout or "").strip()), None
    except ValueError:
        return None, f"unexpected count output: {cp.stdout!r}"
