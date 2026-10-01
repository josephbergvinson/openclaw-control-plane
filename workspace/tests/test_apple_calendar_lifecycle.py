from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import apple_calendar_common as common  # noqa: E402

OWNED = {"running": True, "pid": 123, "launchedAt": 1234.5, "hidden": True, "active": False}


def completed(stdout="", code=0):
    return subprocess.CompletedProcess(["fixture"], code, stdout=stdout, stderr="")


class CalendarLifecycleTests(unittest.TestCase):
    def test_unavailable_native_host_does_not_attempt_process_probe(self):
        with patch.object(common, "osascript_path", return_value=None), \
             patch.object(common, "run_command") as run:
            self.assertIsNone(common.calendar_app_state())
        run.assert_not_called()

    def invoke(self, before, code=0, launch_code=0, opened=None):
        output = io.StringIO()

        def main():
            result = common.ensure_calendar_running(timeout_seconds=5)
            result_code = code if result.returncode == 0 else 2
            print(json.dumps({"overall": "ready" if result_code == 0 else "blocked",
                              "capabilities": {}, "errors": []}))
            return result_code

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(common, "CALENDAR_LOCK_PATH", Path(directory) / "calendar.lock"),
            patch.object(common, "calendar_app_state", side_effect=[before, opened or OWNED]),
            patch.object(common.time, "time", side_effect=[1234.0, 1235.0]),
            patch.object(common, "run_command", side_effect=[completed(code=launch_code),
                         completed(json.dumps({"status": "closed", "reason": "owned_app_quit"}))]) as command,
            patch.object(common, "run_osa_lines", return_value=completed()) as osa,
            redirect_stdout(output),
        ):
            result = common.run_calendar_cli(main)
        return result, json.loads(output.getvalue()), command.call_args_list, osa.call_args_list

    def test_owned_hidden_app_is_closed_after_verified_success(self):
        code, payload, calls, osa = self.invoke({"running": False})
        self.assertEqual(code, 0)
        self.assertEqual(payload["capabilities"]["calendar_cleanup"]["status"], "closed")
        self.assertEqual(calls[0].args[0], ["open", "-g", "-j", "-a", "Calendar"])
        self.assertIn('"pid": 123', calls[1].args[0][-1])
        self.assertEqual(osa[0].args[0], ["delay 2"])

    def test_preexisting_app_is_neither_hidden_activated_nor_quit(self):
        code, payload, calls, osa = self.invoke({**OWNED, "hidden": False})
        self.assertEqual(code, 0)
        self.assertEqual(payload["capabilities"]["calendar_cleanup"]["reason"], "preexisting_app")
        self.assertEqual(calls, [])
        self.assertEqual(osa, [])

    def test_failed_or_ambiguous_write_preserves_app_and_result(self):
        code, payload, calls, _ = self.invoke({"running": False}, code=2)
        self.assertEqual(code, 2)
        self.assertEqual(payload["overall"], "blocked")
        self.assertEqual(payload["capabilities"]["calendar_cleanup"]["reason"], "operation_incomplete")
        self.assertEqual(len(calls), 1)

    def test_unknown_initial_state_never_launches_or_quits(self):
        code, payload, calls, _ = self.invoke(None)
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 0)
        self.assertEqual(payload["capabilities"]["calendar_cleanup"]["status"], "preserved")

    def test_failed_launch_does_not_claim_app_ownership(self):
        code, payload, calls, _ = self.invoke({"running": False}, launch_code=1)
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(payload["capabilities"]["calendar_cleanup"]["status"], "preserved")

    def test_launch_interval_does_not_claim_preexisting_or_replacement_app(self):
        for changes in [{"launchedAt": 1233.0}, {"launchedAt": 1236.0},
                        {"hidden": False}, {"active": True}]:
            with self.subTest(changes=changes):
                code, payload, calls, _ = self.invoke({"running": False}, opened={**OWNED, **changes})
                self.assertEqual(code, 0)
                self.assertEqual(len(calls), 1)
                self.assertEqual(payload["capabilities"]["calendar_cleanup"]["reason"], "ownership_unverified")

    def test_cleanup_uncertainty_does_not_change_success_into_mutation_retry(self):
        with patch.object(common, "run_command", return_value=completed("", code=124)):
            outcome = common.cleanup_calendar_app({"before": {"running": False}, "opened": OWNED}, True)
        self.assertEqual(outcome, {"status": "preserved", "reason": "cleanup_unverified"})

    def native_cleanup(self, changes=None, *, modified=False, visible=False, decline=False):
        # Execute the actual native guard script against AppKit/scripting stubs.
        # No Apple events or live UI are accessed by this regression suite.
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable for JavaScript guard tests")
        with patch.object(common, "run_command", return_value=completed('{"status":"preserved"}')) as run:
            common.cleanup_calendar_app({"before": {"running": False}, "opened": OWNED}, True)
        script = run.call_args.args[0][-1]
        state = {**OWNED, **(changes or {})}
        fixture = '''
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let terminated = false, terminateCalls = 0;
const app = {processIdentifier: input.state.pid,
  launchDate: {timeIntervalSince1970: input.state.launchedAt},
  hidden: input.state.hidden, active: input.state.active,
  get terminate() { terminateCalls++; terminated = !input.decline; return !input.decline; },
  get terminated() { return terminated; }};
const context = { ObjC: {import() {}}, $: {NSRunningApplication: {
  runningApplicationsWithBundleIdentifier() {return {count: 1, objectAtIndex() {return app}};}}},
  Application() {return {documents() {return [{modified() {return input.modified}}]},
    windows() {return [{visible() {return input.visible}}]}}} };
const result = JSON.parse(vm.runInNewContext(input.script, context));
process.stdout.write(JSON.stringify({result, terminateCalls}));
'''
        result = subprocess.run([node, "-e", fixture], input=json.dumps({"script": script, "state": state,
                                "modified": modified, "visible": visible, "decline": decline}),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_native_guard_uses_normal_termination_for_same_unused_app(self):
        result = self.native_cleanup()
        self.assertEqual(result, {"result": {"status": "closed", "reason": "owned_app_quit"}, "terminateCalls": 1})

    def test_native_guard_preserves_user_takeover_unsaved_work_and_replacement(self):
        cases = [
            ({"changes": {"active": True}}, "user_interface_in_use"),
            ({"changes": {"hidden": False}}, "user_interface_in_use"),
            ({"modified": True}, "user_work_present"),
            ({"visible": True}, "user_work_present"),
            ({"changes": {"pid": 456}}, "identity_changed"),
            ({"changes": {"launchedAt": 5678}}, "identity_changed"),
        ]
        for kwargs, reason in cases:
            with self.subTest(kwargs=kwargs):
                result = self.native_cleanup(**kwargs)
                self.assertEqual(result, {"result": {"status": "preserved", "reason": reason}, "terminateCalls": 0})

    def test_native_guard_respects_app_declining_normal_quit(self):
        result = self.native_cleanup(decline=True)
        self.assertEqual(result["result"], {"status": "preserved", "reason": "quit_declined"})
        self.assertEqual(result["terminateCalls"], 1)

    def test_overlapping_cli_operations_are_serialized_through_cleanup(self):
        fixture = '''
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import apple_calendar_common as common
common.CALENDAR_LOCK_PATH = Path(sys.argv[2]) / "calendar.lock"
common.calendar_app_state = lambda: {"running": False}
def mark(value):
    with open(sys.argv[3], "a") as stream: stream.write(value + "\\n")
def cleanup(owner, succeeded):
    mark(sys.argv[4] + " cleanup")
    return {"status": "closed"}
common.cleanup_calendar_app = cleanup
def main():
    mark(sys.argv[4] + " start")
    time.sleep(0.2)
    print(json.dumps({"overall": "ready", "capabilities": {}}))
    return 0
raise SystemExit(common.run_calendar_cli(main))
'''
        with tempfile.TemporaryDirectory() as directory:
            journal = str(Path(directory) / "events")
            processes = [subprocess.Popen([sys.executable, "-c", fixture, str(SCRIPTS), directory, journal, name],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                         for name in ["first", "second"]]
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout)["capabilities"]["calendar_cleanup"]["status"], "closed")
            lines = Path(journal).read_text().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].split()[0], lines[1].split()[0])
        self.assertEqual(lines[2].split()[0], lines[3].split()[0])
        self.assertEqual([line.split()[1] for line in lines], ["start", "cleanup", "start", "cleanup"])

    def test_unsafe_lock_symlink_fails_before_calendar_access(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("preserve")
            (Path(directory) / "calendar.lock").symlink_to(target)
            output = io.StringIO()
            with patch.object(common, "CALENDAR_LOCK_PATH", Path(directory) / "calendar.lock"), \
                 patch.object(common, "calendar_app_state") as state, redirect_stdout(output):
                code = common.run_calendar_cli(lambda: self.fail("operation must not run"))
            self.assertEqual(code, 2)
            state.assert_not_called()
            self.assertEqual(target.read_text(), "preserve")

    def test_busy_helper_reports_bounded_wait_without_touching_app(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.lock"
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                output = io.StringIO()
                with patch.object(common, "CALENDAR_LOCK_PATH", path), \
                     patch.object(common.time, "monotonic", side_effect=[0, 31]), \
                     patch.object(common, "calendar_app_state") as state, redirect_stdout(output):
                    code = common.run_calendar_cli(lambda: self.fail("operation must wait"))
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output.getvalue())["errors"], ["calendar_helper_busy"])
                state.assert_not_called()
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
