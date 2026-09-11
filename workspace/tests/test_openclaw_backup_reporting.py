from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import openclaw_archive_v3_retention as retention
from scripts import openclaw_backup_retention_cleanup as cleanup
from scripts import openclaw_weekly_archive_backup as backup


class BackupReportingTests(unittest.TestCase):
    def capture(self, action):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = action()
        self.assertEqual(stderr.getvalue(), "")
        lines = stdout.getvalue().splitlines()
        public = "\n".join(line for line in lines if not line.startswith("EFFECT_PREDICATES "))
        effects = [json.loads(line.split(" ", 1)[1]) for line in lines
                   if line.startswith("EFFECT_PREDICATES ")]
        for detail in ("STATUS", "BACKUP_OK", "RETENTION_", "/private/", "provider-secret"):
            self.assertNotIn(detail, public)
        return code, public, effects

    def test_weekly_success_only_reports_after_final_readback(self):
        with mock.patch.object(backup, "create_backup", return_value=(0, {
            "destination": "/private/generation", "backup": "private-name",
            "receipt": "/private/receipt", "blockers": [],
        })), mock.patch.object(backup, "independent_published_effect_readback", return_value=[
            backup.effect.readback_matches("published", expected=True, actual=True),
        ]) as readback:
            code, public, effects = self.capture(lambda: backup.main([]))
        self.assertEqual(code, 0)
        self.assertEqual(public, "The weekly OpenClaw backup is complete and its restore checks passed.")
        readback.assert_called_once_with(Path("/private/generation"))
        self.assertTrue(effects[0][0]["satisfied"])

    def test_weekly_failed_creation_keeps_details_private_and_exit_code(self):
        with mock.patch.object(backup, "create_backup", return_value=(1, {
            "destination": "/private/generation", "backup": "private-name",
            "receipt": "", "blockers": ["provider-secret /private/failure"],
        })), mock.patch.object(backup, "independent_published_effect_readback") as readback:
            code, public, effects = self.capture(lambda: backup.main([]))
        self.assertEqual(code, 1)
        self.assertEqual(public, "The weekly OpenClaw backup did not complete. Existing backups were kept.")
        readback.assert_not_called()
        self.assertNotIn("provider-secret", json.dumps(effects))
        self.assertIn("if available", effects[0][0]["detail"])

    def test_weekly_failed_final_readback_never_announces_success(self):
        with mock.patch.object(backup, "create_backup", return_value=(0, {
            "destination": "/private/generation", "receipt": "/private/receipt",
        })), mock.patch.object(backup, "independent_published_effect_readback", return_value=[
            backup.effect.unreadable("published", "provider-secret /private/missing"),
        ]):
            code, public, effects = self.capture(lambda: backup.main([]))
        self.assertEqual(code, 4)
        self.assertEqual(public, "The weekly backup failed its final verification. It is not confirmed usable.")
        self.assertFalse(effects[0][0]["satisfied"])

    def test_retention_outcomes_have_concise_messages_without_changing_status(self):
        cases = [
            (0, {"apply": True, "result": "verified", "removed": []}, "NO_REPLY"),
            (0, {"apply": True, "result": "verified", "removed": ["old-one"]},
             "Backup maintenance removed 1 older backup and kept the newest two verified backups."),
            (0, {"apply": False, "result": "verified", "candidates": ["one", "two"]},
             "2 older backups are eligible for cleanup. Nothing was removed."),
            (0, {"result": "waiting_external_backup", "retry_suppressed": False},
             "Older backups were kept because an independent backup has not yet been verified. Nothing was removed."),
            (0, {"result": "waiting_external_backup", "retry_suppressed": True}, "NO_REPLY"),
            (1, {"result": "blocked", "removed": []},
             "Backup maintenance could not complete its safety checks. No backups were removed."),
            (1, {"result": "blocked", "removed": ["old-one", "old-two"]},
             "Backup maintenance stopped after removing 2 older backups. The remaining backups are not yet fully verified."),
            (1, {"result": "blocked", "quarantined": ["/private/partial"]},
             "Backup maintenance stopped with an older backup set aside. Cleanup and verification are incomplete."),
        ]
        for expected_code, report, expected_message in cases:
            with self.subTest(report=report), mock.patch.object(retention, "run_retention", return_value=(
                expected_code, {"protected_v3": [], "remaining_v3": [], "candidates": [], "removed": [],
                                "pinned_clone_v2": [], "two_copy_floor_satisfied": True,
                                **report, "blockers": ["provider-secret /private/failure"],
                                "receipt": "/private/receipt"},
            )):
                code, public, _ = self.capture(lambda: retention.main(["--apply"] if report.get("apply") else []))
                self.assertEqual(code, expected_code)
                self.assertEqual(public, expected_message)

    def test_retention_wrapper_checks_persisted_receipt_before_no_change_or_success(self):
        with tempfile.TemporaryDirectory(prefix="backup-reporting-") as raw:
            root = Path(raw)
            for name, _ in cleanup.FROZEN_CLONE_V2_PINS:
                (root / name).mkdir()
            report = {
                "result": "verified", "apply": True, "weekly_root": str(root),
                "removed": [], "protected_v3": [], "remaining_v3": [],
                "pinned_clone_v2": [{"name": name, "manifest_sha256": digest, "result": "verified"}
                                    for name, digest in cleanup.FROZEN_CLONE_V2_PINS],
                "receipt": str(root / "receipt.json"),
            }
            Path(report["receipt"]).write_text(json.dumps(report))
            runner = lambda _args: (0, report)
            code, public, effects = self.capture(lambda: cleanup.main(["--apply"], engine_runner=runner))
            self.assertEqual((code, public), (0, "NO_REPLY"))
            self.assertTrue(all(item["satisfied"] for item in effects[0]))

            # The in-memory report still claims success; the durable receipt disagrees.
            Path(report["receipt"]).write_text(json.dumps({**report, "result": "blocked"}))
            code, public, effects = self.capture(lambda: cleanup.main(["--apply"], engine_runner=runner))
            self.assertEqual(code, 4)
            self.assertEqual(public, "Backup maintenance failed its final verification. Its outcome is not confirmed.")
            self.assertFalse(all(item["satisfied"] for item in effects[0]))

    def test_real_cron_entrypoint_reports_failure_without_private_effect_detail(self):
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="backup-reporting-cron-") as raw:
            root = Path(raw)
            launcher = root / "fixture_backup.py"
            launcher.write_text(
                "import sys\nfrom unittest import mock\n"
                + "sys.path.insert(0, " + repr(str(source_root)) + ")\n"
                + "from scripts import openclaw_weekly_archive_backup as backup\n"
                + "with mock.patch.object(backup, 'create_backup', return_value=(1, "
                  "{'backup': 'private-name', 'receipt': '/private/receipt', "
                  "'blockers': ['provider-secret /private/failure']})):\n"
                  "    raise SystemExit(backup.main())\n"
            )
            completed = subprocess.run([
                sys.executable, str(source_root / "scripts/cron_python_entrypoint.py"),
                "--receipt-dir", str(root / "receipts"), "--script", str(launcher),
                "--what", "fixture weekly backup", "--cwd", str(root),
            ], capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertEqual(completed.stdout, "The weekly OpenClaw backup did not complete. Existing backups were kept.\n")
            self.assertEqual(completed.stderr, "")
            receipts = list((root / "receipts").glob("*.json"))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text())
            self.assertFalse(receipt["business_effect"]["satisfied"])
            self.assertNotIn("provider-secret", json.dumps(receipt["business_effect"]))
            self.assertEqual(receipts[0].stat().st_mode & 0o777, 0o600)

    def test_native_failure_diagnostics_are_private_bounded_and_redacted(self):
        source_root = Path(__file__).resolve().parents[1]
        native_program = (
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "with pathlib.Path(__file__).with_suffix('.calls').open('a') as f: f.write('call\\n')\n"
            "print('private-config-stdout-must-not-be-retained')\n"
            "print('Archive symbolic link target must be relative: /private/fixture/cache', file=sys.stderr)\n"
            "print(os.environ['OPENCLAW_FIXTURE_TOKEN'], file=sys.stderr)\n"
            "print('Bearer synthetic-bearer-credential password=synthetic-password', file=sys.stderr)\n"
            "print('postgres://fixture:synthetic-uri-password@example.invalid/db', file=sys.stderr)\n"
            "print('é' * 20000, file=sys.stderr)\n"
            "raise SystemExit(7)\n"
        )
        for supervised in (False, True):
            with self.subTest(supervised=supervised), tempfile.TemporaryDirectory(prefix="native-diagnostics-") as raw:
                root = Path(raw)
                launcher = root / "fixture_backup.py"
                launcher.write_text(
                    "import sys\nfrom pathlib import Path\nfrom unittest import mock\n"
                    + "sys.path[:0] = " + repr([str(source_root), str(source_root / "tests")]) + "\n"
                    + "from scripts import openclaw_weekly_archive_backup as backup\n"
                    + "from test_openclaw_native_weekly_backup import NativeWeeklyBackupTests\n"
                    + "root = Path(" + repr(str(root)) + ")\n"
                    + "config, identity = NativeWeeklyBackupTests().fixture(root)\n"
                    + "config.openclaw_cli.write_text(" + repr(native_program) + ")\n"
                    + "config.openclaw_cli.chmod(0o700)\n"
                    + "create = backup.create_backup\n"
                    + "with mock.patch.object(backup, 'create_backup', side_effect=lambda: create(config, identity_reader=lambda _: identity)):\n"
                    + "    raise SystemExit(backup.main())\n"
                )
                command = [sys.executable, str(launcher)]
                if supervised:
                    command = [sys.executable, str(source_root / "scripts/cron_python_entrypoint.py"),
                               "--receipt-dir", str(root / "process-receipts"),
                               "--script", str(launcher), "--what", "fixture weekly backup",
                               "--cwd", str(root)]
                completed = subprocess.run(command, capture_output=True, text=True, check=False,
                                           env={**os.environ, "OPENCLAW_FIXTURE_TOKEN": "synthetic-env-credential"})
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertEqual(completed.stderr, "")
                self.assertIn("The weekly OpenClaw backup did not complete.", completed.stdout)
                for private in ("/private/fixture", "synthetic-", "private-config-stdout", "Archive symbolic"):
                    self.assertNotIn(private, completed.stdout)
                self.assertEqual((root / "bin/openclaw.calls").read_text(), "call\n")
                receipts = list((root / "OWC/OpenClaw/Workspace/artifacts/archive-backup-receipts").glob("*.json"))
                self.assertEqual(len(receipts), 1)
                phase = json.loads(receipts[0].read_text())
                self.assertEqual(phase["status"], "failed")
                diagnostic = phase["native_command_diagnostic"]
                self.assertEqual(diagnostic["operation"], "native archive create")
                self.assertEqual(diagnostic["exit_code"], 7)
                self.assertTrue(diagnostic["stderr"]["truncated"])
                self.assertLessEqual(len(diagnostic["stderr"]["content_redacted"].encode()), 16 * 1024)
                self.assertIn("Archive symbolic link target must be relative", diagnostic["stderr"]["content_redacted"])
                self.assertIn("[REDACTED", diagnostic["stderr"]["content_redacted"])
                for private in ("synthetic-env-credential", "synthetic-bearer-credential",
                                "synthetic-password", "synthetic-uri-password", "private-config-stdout"):
                    self.assertNotIn(private, json.dumps(phase))
                self.assertEqual(receipts[0].stat().st_mode & 0o777, 0o600)
                self.assertFalse(Path(phase["destination"]).exists())
                if supervised:
                    process_receipt = json.loads(next((root / "process-receipts").glob("*.json")).read_text())
                    self.assertFalse(process_receipt["business_effect"]["satisfied"])
                    self.assertNotIn("/private/fixture", json.dumps(process_receipt))

    def test_real_cron_retention_outcomes_do_not_leak_internal_reports(self):
        source_root = Path(__file__).resolve().parents[1]
        cases = [
            (1, {"result": "blocked", "removed": []},
             "Backup maintenance could not complete its safety checks. No backups were removed."),
            (0, {"result": "verified", "removed": ["private-old-name"]},
             "Backup maintenance removed 1 older backup and kept the newest two verified backups."),
            (0, {"result": "verified", "removed": []}, "NO_REPLY"),
            (0, {"result": "waiting_external_backup", "removed": []},
             "Older backups were kept because an independent backup has not yet been verified. Nothing was removed."),
            (0, {"result": "waiting_external_backup", "retry_suppressed": True}, "NO_REPLY"),
        ]
        for expected_code, fields, expected_message in cases:
            with self.subTest(fields=fields), tempfile.TemporaryDirectory(prefix="retention-reporting-cron-") as raw:
                root = Path(raw)
                for name, _ in cleanup.FROZEN_CLONE_V2_PINS:
                    (root / name).mkdir()
                for name in ("new-one", "new-two"):
                    (root / name).mkdir()
                report = {
                    "apply": True, "weekly_root": str(root), "protected_v3": ["new-one", "new-two"],
                    "remaining_v3": ["new-one", "new-two"],
                    "pinned_clone_v2": [{"name": name, "manifest_sha256": digest, "result": "verified"}
                                        for name, digest in cleanup.FROZEN_CLONE_V2_PINS],
                    "blockers": ["provider-secret /private/failure"],
                    "receipt": str(root / "owner-receipt.json"), **fields,
                }
                Path(report["receipt"]).write_text(json.dumps(report))
                launcher = root / "fixture_retention.py"
                launcher.write_text(
                    "import sys\nfrom unittest import mock\n"
                    + "sys.path.insert(0, " + repr(str(source_root)) + ")\n"
                    + "from scripts import openclaw_backup_retention_cleanup as cleanup\n"
                    + "with mock.patch.object(cleanup.archive_retention, 'run_from_args', return_value="
                    + repr((expected_code, report)) + "):\n"
                      "    raise SystemExit(cleanup.main(['--apply']))\n"
                )
                completed = subprocess.run([
                    sys.executable, str(source_root / "scripts/cron_python_entrypoint.py"),
                    "--receipt-dir", str(root / "receipts"), "--script", str(launcher),
                    "--what", "fixture backup maintenance", "--cwd", str(root),
                ], capture_output=True, text=True, check=False)
                self.assertEqual(completed.returncode, expected_code, completed.stderr)
                self.assertEqual(completed.stdout, expected_message + "\n")
                self.assertEqual(completed.stderr, "")
                process_receipt = json.loads(next((root / "receipts").glob("*.json")).read_text())
                if not fields.get("retry_suppressed"):
                    self.assertEqual(process_receipt["business_effect"]["satisfied"], expected_code == 0)
                if expected_code != 0:
                    self.assertIn("provider-secret", json.dumps(process_receipt["business_effect"]))


if __name__ == "__main__":
    unittest.main()
