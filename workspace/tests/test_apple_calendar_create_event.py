from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import apple_calendar_create_event as create  # noqa: E402
import apple_calendar_common as common  # noqa: E402

START = "2026-09-23T09:30:00"
END = "2026-09-23T10:00:00"


def completed(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["fixture"], code, stdout=stdout, stderr=stderr)


class CalendarCreateTests(unittest.TestCase):
    def invoke(self, title="Planning", location="Room A", description="Join notes", join_url="https://meet.google.com/example", results=None):
        with patch.object(create, "run_osa_lines", side_effect=results or [completed(stdout="created||uid-one"), completed(stdout="verified")]) as run:
            value, error = create.create_event(title, START, END, "Work", location, description, join_url)
        return value, error, run.call_args_list

    def compile_lines(self, lines):
        compiler = Path("/usr/bin/osacompile")
        if not compiler.is_file():
            self.skipTest("osacompile is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            command = [str(compiler), "-o", str(Path(directory) / "calendar.scpt")]
            for line in lines:
                command.extend(["-e", line])
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_create_carries_full_details_and_independently_reads_exact_uid(self):
        value, error, calls = self.invoke(description='First line\nDial "123" || note \\ detail')
        self.assertIsNone(error)
        self.assertEqual(value["uid"], "uid-one")
        self.assertEqual(value["mutation_outcome"], "created")
        self.assertEqual(value["verified_fields"], ["title", "start", "end", "location", "description", "url"])
        self.assertEqual(len(calls), 2)
        first, second = ["\n".join(call.args[0]) for call in calls]
        self.assertIn('every calendar whose name is "Work"', first)
        self.assertIn('if (count of matches) is not 1', first)
        self.assertIn('whose summary is "Planning" and start date is startDate and end date is endDate', first)
        self.assertIn('url:"https://meet.google.com/example"', first)
        self.assertIn('description:', first)
        self.assertIn('every event of targetCal whose uid is "uid-one"', second)
        for field in ["title", "start", "end", "location", "description", "url"]:
            self.assertIn(f"readback_{field}_mismatch", second)
        self.assertNotIn("properties of", first + second)
        self.assertNotIn("id of targetCal", first + second)
        self.assertNotIn("make new event", second)
        for call in calls:
            self.assertEqual(call.kwargs, {"timeout_seconds": common.CALENDAR_READ_TIMEOUT_SECONDS})
            self.compile_lines(call.args[0])

    def test_existing_same_identity_is_reused_and_still_verified(self):
        value, error, calls = self.invoke(results=[completed(stdout="reused||same-uid"), completed(stdout="verified")])
        self.assertIsNone(error)
        self.assertEqual(value["mutation_outcome"], "reused")
        self.assertEqual(value["uid"], "same-uid")
        lines = calls[0].args[0]
        self.assertLess(lines.index('set outcome to "reused"'), lines.index('else'))
        self.assertIn('if (count of matchingEvents) > 1 then error "matching_event_not_unique"', lines)
        self.assertEqual(len(calls), 2)

    def test_existing_joining_detail_conflict_keeps_uid_and_never_creates_replacement(self):
        value, error, calls = self.invoke(results=[completed(stdout="reused||same-uid"), completed(1, stderr="readback_description_mismatch")])
        self.assertEqual(value["uid"], "same-uid")
        self.assertEqual(value["mutation_outcome"], "reused")
        self.assertIn("do not create a replacement", error)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("make new event", "\n".join(calls[1].args[0]))

    def test_create_timeout_is_ambiguous_and_is_not_retried(self):
        value, error, calls = self.invoke(results=[completed(124, stderr="timed out")])
        self.assertEqual(value["mutation_outcome"], "unverified")
        self.assertIn("reconcile the exact calendar/title/start/end", error)
        self.assertEqual(len(calls), 1)

    def test_postwrite_readback_failure_retains_the_created_uid(self):
        value, error, calls = self.invoke(results=[completed(stdout="created||uid-one"), completed(124, stderr="timed out")])
        self.assertEqual(value, {"calendar": "Work", "uid": "uid-one", "mutation_outcome": "created"})
        self.assertIn("preserve this UID", error)
        self.assertEqual(len(calls), 2)

    def test_optional_omission_preserves_existing_unrequested_notes(self):
        value, error, calls = self.invoke(location=None, description=None, join_url=None,
                                        results=[completed(stdout="reused||same-uid"), completed(stdout="verified")])
        self.assertIsNone(error)
        self.assertEqual(value["verified_fields"], ["title", "start", "end"])
        for call in calls:
            self.assertNotIn("description", "\n".join(call.args[0]))
            self.assertNotIn("url:", "\n".join(call.args[0]))

    def test_invalid_dates_and_missing_target_fail_before_apple_events(self):
        with patch.object(create, "run_osa_lines") as run:
            self.assertIsNotNone(create.create_event("Planning", END, START, "Work", None)[1])
            self.assertIsNotNone(create.create_event("Planning", START, END, None, None)[1])
        run.assert_not_called()

    def test_cli_keeps_bounded_launch_list_and_mutation_identity_on_error(self):
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["create", "--title", "Planning", "--start", START, "--end", END, "--calendar", "Work", "--url", "https://example.com/join", "--description", "Dial in"]),
            patch.object(create, "osascript_path", return_value="/usr/bin/osascript"),
            patch.object(create, "ensure_calendar_running", return_value=completed()) as launch,
            patch.object(create, "list_calendars", return_value=(["Work"], None)) as calendars,
            patch.object(create, "create_event", return_value=({"uid": "kept", "mutation_outcome": "created"}, "readback failed")) as write,
            redirect_stdout(output),
        ):
            self.assertEqual(create.main(), 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["capabilities"]["create_event"]["uid"], "kept")
        launch.assert_called_once_with(timeout_seconds=common.CALENDAR_LAUNCH_TIMEOUT_SECONDS)
        calendars.assert_called_once_with(timeout_seconds=common.CALENDAR_READ_TIMEOUT_SECONDS)
        write.assert_called_once_with("Planning", START, END, "Work", None, "Dial in", "https://example.com/join")



if __name__ == "__main__":
    unittest.main()
