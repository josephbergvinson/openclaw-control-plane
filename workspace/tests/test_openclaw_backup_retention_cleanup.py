from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from scripts import openclaw_backup_retention_cleanup as cleanup


class OpenClawBackupRetentionCleanupTests(unittest.TestCase):
    def test_preview_delegates_with_both_frozen_clone_v2_pins(self) -> None:
        with mock.patch.object(
            cleanup.archive_retention,
            "run_from_args",
            return_value=(0, {"result": "waiting_external_backup", "retry_suppressed": True}),
        ) as delegated:
            code = cleanup.main([])

        self.assertEqual(code, 0)
        argv = delegated.call_args.args[0]
        self.assertNotIn("--apply", argv)
        self.assertEqual(argv.count("--pinned-clone-v2"), 2)
        for name, digest in cleanup.FROZEN_CLONE_V2_PINS:
            self.assertIn("{}={}".format(name, digest), argv)

    def test_apply_authority_is_one_literal_flag(self) -> None:
        with mock.patch.object(
            cleanup.archive_retention,
            "run_from_args",
            return_value=(0, {"result": "waiting_external_backup", "retry_suppressed": True}),
        ) as delegated:
            code = cleanup.main(["--apply"])

        self.assertEqual(code, 0)
        argv = delegated.call_args.args[0]
        self.assertEqual(argv.count("--apply"), 1)
        self.assertEqual(argv.count("--independent-backup-receipt"), 1)
        self.assertIn(str(cleanup.DEFAULT_INDEPENDENT_BACKUP_RECEIPT), argv)

    def test_explicit_independent_receipt_overrides_default(self) -> None:
        receipt = Path("/tmp/verified-independent-backup-receipt.json")
        with mock.patch.object(
            cleanup.archive_retention,
            "run_from_args",
            return_value=(0, {"result": "waiting_external_backup", "retry_suppressed": True}),
        ) as delegated:
            code = cleanup.main(
                ["--apply", "--independent-backup-receipt", str(receipt)]
            )

        self.assertEqual(code, 0)
        argv = delegated.call_args.args[0]
        self.assertIn(str(receipt), argv)
        self.assertNotIn(str(cleanup.DEFAULT_INDEPENDENT_BACKUP_RECEIPT), argv)

    def test_exact_cron_entrypoint_shape_binds_default_receipt(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="archive-v3-wrapper-cron-") as raw:
            root = Path(raw)
            wrapper = root / "openclaw_backup_retention_cleanup.py"
            shutil.copy2(Path(cleanup.__file__), wrapper)
            shutil.copy2(source_root / 'scripts/operator_contract.py', root / 'operator_contract.py')
            capture = root / "delegated-argv.json"
            (root / "openclaw_archive_v3_retention.py").write_text(
                """from __future__ import annotations
import json
import os
from pathlib import Path

def run_from_args(argv=None):
    Path(os.environ['ARCHIVE_V3_CAPTURE']).write_text(
        json.dumps(list(argv or [])), encoding='utf-8'
    )
    return 0, {'result': 'waiting_external_backup', 'retry_suppressed': True}
""",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["ARCHIVE_V3_CAPTURE"] = str(capture)
            completed = subprocess.run(
                [
                    "/usr/bin/python3",
                    str(source_root / "scripts/cron_python_entrypoint.py"),
                    "--receipt-dir",
                    str(root / "receipts"),
                    "--script",
                    str(wrapper),
                    "--what",
                    "apply verified OWC archive-v3 retention",
                    "--cwd",
                    str(root),
                    "--",
                    "--apply",
                ],
                cwd=source_root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            delegated = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(delegated.count("--apply"), 1)
            self.assertEqual(
                delegated.count("--independent-backup-receipt"), 1
            )
            self.assertIn(
                str(cleanup.DEFAULT_INDEPENDENT_BACKUP_RECEIPT), delegated
            )
            for name, digest in cleanup.FROZEN_CLONE_V2_PINS:
                self.assertIn("{}={}".format(name, digest), delegated)

            quiet_environment = environment.copy()
            quiet_environment["ARCHIVE_V3_OUTPUT"] = "NO_REPLY"
            quiet = subprocess.run(
                [
                    "/usr/bin/python3",
                    str(source_root / "scripts/cron_python_entrypoint.py"),
                    "--receipt-dir",
                    str(root / "quiet-receipts"),
                    "--script",
                    str(wrapper),
                    "--what",
                    "apply verified OWC archive-v3 retention",
                    "--cwd",
                    str(root),
                    "--",
                    "--apply",
                ],
                cwd=source_root,
                env=quiet_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(quiet.returncode, 0, quiet.stderr)
            self.assertEqual(quiet.stdout, "NO_REPLY\n")
            quiet_delegated = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(quiet_delegated.count("--apply"), 1)
            self.assertEqual(
                quiet_delegated.count("--independent-backup-receipt"),
                1,
            )

    def test_abbreviated_flag_is_rejected_before_delegation(self) -> None:
        with mock.patch.object(
            cleanup.archive_retention,
            "run_from_args",
        ) as delegated, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cleanup.main(["--app"])

        self.assertEqual(raised.exception.code, 2)
        delegated.assert_not_called()

    def test_terminal_receipt_readback_accepts_zero_deletion_and_checks_pins(self) -> None:
        with tempfile.TemporaryDirectory(prefix="archive-retention-effect-") as raw:
            weekly_root = Path(raw)
            for name, _digest in cleanup.FROZEN_CLONE_V2_PINS:
                (weekly_root / name).mkdir()
            (weekly_root / "openclaw-archive-v3-new").mkdir()
            receipt = {
                "result": "verified",
                "status": "completed",
                "apply": True,
                "weekly_root": str(weekly_root),
                "removed": [],
                "protected_v3": ["openclaw-archive-v3-new"],
                "remaining_v3": ["openclaw-archive-v3-new"],
                "pinned_clone_v2": [
                    {"name": name, "manifest_sha256": digest, "result": "verified"}
                    for name, digest in cleanup.FROZEN_CLONE_V2_PINS
                ],
            }
            results = cleanup.validate_archive_retention_receipt(
                receipt,
                apply_requested=True,
            )
            self.assertTrue(all(result["satisfied"] for result in results), results)

            receipt["pinned_clone_v2"][0]["manifest_sha256"] = "0" * 64
            results = cleanup.validate_archive_retention_receipt(
                receipt,
                apply_requested=True,
            )
            self.assertTrue(any(not result["satisfied"] for result in results))


if __name__ == "__main__":
    unittest.main()
