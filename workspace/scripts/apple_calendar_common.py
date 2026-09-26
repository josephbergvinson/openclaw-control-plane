#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import stat
import subprocess
import time
from contextlib import redirect_stdout
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


CALENDAR_APP = "Calendar"
CALENDAR_LAUNCH_TIMEOUT_SECONDS = 5.0
# Complete Calendar.app reads routinely take 10-15 seconds on this host even
# when the app is healthy. Leave enough bounded headroom for concurrent agent
# work instead of turning ordinary scheduler pressure into a false outage.
CALENDAR_READ_TIMEOUT_SECONDS = 30.0
CALENDAR_LOCK_PATH = Path("/tmp") / f"openclaw-calendar-{os.getuid()}.lock"
_calendar_owner: ContextVar[dict | None] = ContextVar("calendar_owner", default=None)


def calendar_app_state() -> dict | None:
    """Read process identity without launching Calendar or requiring UI access."""
    if osascript_path() is None:
        return None
    result = run_command([
        "/usr/bin/osascript", "-l", "JavaScript", "-e", '''
ObjC.import("AppKit");
var apps = $.NSRunningApplication.runningApplicationsWithBundleIdentifier("com.apple.iCal");
var count = Number(apps.count);
if (count === 0) {
    JSON.stringify({running: false});
} else if (count === 1) {
    var app = apps.objectAtIndex(0);
    JSON.stringify({running: true, pid: Number(app.processIdentifier),
        launchedAt: Number(app.launchDate.timeIntervalSince1970),
        hidden: Boolean(app.hidden), active: Boolean(app.active)});
} else { JSON.stringify({}); }
'''], timeout_seconds=CALENDAR_LAUNCH_TIMEOUT_SECONDS)
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("running"), bool):
        return None
    if value["running"] and not (
        type(value.get("pid")) is int and value["pid"] > 0
        and type(value.get("launchedAt")) in (int, float) and value["launchedAt"] > 0
        and type(value.get("hidden")) is bool and type(value.get("active")) is bool
    ):
        return None
    return value


def cleanup_calendar_app(owner: dict, succeeded: bool) -> dict:
    """Only normally quit the same, still-unused app that this operation opened."""
    before, opened = owner.get("before"), owner.get("opened")
    if before and before.get("running"):
        return {"status": "preserved", "reason": "preexisting_app"}
    if not succeeded:
        return {"status": "preserved", "reason": "operation_incomplete"}
    if not opened:
        return {"status": "preserved", "reason": "ownership_unverified"}
    # Identity and user-work checks run in the same native invocation as the
    # normal terminate request. Never force quit or discard unsaved changes.
    script = '''
ObjC.import("AppKit");
function finish(expected) {
    var apps = $.NSRunningApplication.runningApplicationsWithBundleIdentifier("com.apple.iCal");
    if (Number(apps.count) === 0) return {status: "closed", reason: "already_exited"};
    if (Number(apps.count) !== 1) return {status: "preserved", reason: "identity_changed"};
    var app = apps.objectAtIndex(0);
    if (Number(app.processIdentifier) !== expected.pid ||
        Number(app.launchDate.timeIntervalSince1970) !== expected.launchedAt)
        return {status: "preserved", reason: "identity_changed"};
    if (Boolean(app.active) || !Boolean(app.hidden))
        return {status: "preserved", reason: "user_interface_in_use"};
    var calendar = Application("Calendar");
    if (calendar.documents().some(d => d.modified()) || calendar.windows().some(w => w.visible()))
        return {status: "preserved", reason: "user_work_present"};
    if (Boolean(app.active) || !Boolean(app.hidden))
        return {status: "preserved", reason: "user_interface_in_use"};
    if (!app.terminate) return {status: "preserved", reason: "quit_declined"};
    var deadline = Date.now() + 2000;
    while (!Boolean(app.terminated) && Date.now() < deadline)
        $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.05));
    return Boolean(app.terminated) ? {status: "closed", reason: "owned_app_quit"}
        : {status: "preserved", reason: "quit_pending"};
}
try { JSON.stringify(finish(EXPECTED)); }
catch (error) { JSON.stringify({status: "preserved", reason: "cleanup_state_unavailable"}); }
'''.replace("EXPECTED", json.dumps({"pid": opened["pid"], "launchedAt": opened["launchedAt"]}))
    result = run_command(["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
                         timeout_seconds=CALENDAR_LAUNCH_TIMEOUT_SECONDS)
    try:
        outcome = json.loads(result.stdout)
        if result.returncode == 0 and outcome.get("status") in ("closed", "preserved"):
            return outcome
    except (ValueError, TypeError, AttributeError):
        pass
    return {"status": "preserved", "reason": "cleanup_unverified"}


def run_calendar_cli(main: Callable[[], int]) -> int:
    """Serialize helper use and release an owned app after verified completion."""
    # One stable per-user advisory lock spans launch, effect/readback and cleanup.
    # Never unlink it: waiters must continue locking the same inode. A crashed
    # helper releases the OS lock and its surviving app becomes preexisting.
    try:
        fd = os.open(CALENDAR_LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        print(json.dumps({"overall": "blocked", "errors": ["calendar_helper_lock_unavailable"],
                          "capabilities": {}}))
        return 2
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            print(json.dumps({"overall": "blocked", "errors": ["calendar_helper_lock_unsafe"],
                              "capabilities": {}}))
            return 2
        deadline = time.monotonic() + CALENDAR_READ_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print(json.dumps({"overall": "blocked", "errors": ["calendar_helper_busy"],
                                      "capabilities": {}}))
                    return 2
                time.sleep(0.05)
        owner = {"before": calendar_app_state()}
        token = _calendar_owner.set(owner)
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                code = main()
            try:
                payload = json.loads(output.getvalue())
            except ValueError:
                print(output.getvalue(), end="")
                return code
            cleanup = cleanup_calendar_app(owner, code == 0 and payload.get("overall") == "ready")
            payload["capabilities"]["calendar_cleanup"] = cleanup
            print(json.dumps(payload, indent=2))
            return code
        except BaseException:
            # Preserve a possibly mutated app on interruption or ambiguous work.
            print(output.getvalue(), end="")
            raise
        finally:
            _calendar_owner.reset(token)
    finally:
        os.close(fd)


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
    owner = _calendar_owner.get()
    if owner is not None:
        if owner["before"] is None:
            return subprocess.CompletedProcess(["open"], 1, stdout="", stderr="calendar initial state unavailable")
        if owner["before"]["running"]:
            return subprocess.CompletedProcess(["open"], 0, stdout="", stderr="")
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    launch_started = time.time() if owner is not None else None
    launch_cp = run_command(
        ["open", "-g", "-j", "-a", CALENDAR_APP],
        timeout_seconds=timeout_seconds,
    )
    launch_finished = time.time() if owner is not None else None
    if launch_cp.returncode != 0:
        return launch_cp
    if owner is not None:
        # Capture before the readiness delay. A later replacement, or an app
        # already launched before our open call, is not ours to terminate.
        opened = calendar_app_state()
        if (opened and opened["running"] and opened["hidden"] and not opened["active"]
                and launch_started <= opened["launchedAt"] <= launch_finished):
            owner["opened"] = opened
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
        return subprocess.CompletedProcess(
            ["osascript"],
            124,
            stdout="",
            stderr=f"calendar launch timed out after {timeout_seconds:g}s",
        )
    return run_osa_lines([f'delay {delay_seconds}'], timeout_seconds=remaining)


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
