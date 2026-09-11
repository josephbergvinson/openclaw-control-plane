from __future__ import annotations

import fcntl
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from activation_fixture_support import operator_binding

from scripts import openclaw_approval_a_retention as retention


class ApprovalARetentionTests(unittest.TestCase):
    def make_config(
        self,
        base: Path,
        *,
        create_root: bool = True,
    ) -> retention.RetentionConfig:
        root = base / "OpenClawApprovalA"
        if create_root:
            root.mkdir(mode=0o711)
            lock = root / retention.LOCK_NAME
            lock.write_text(
                json.dumps(
                    {
                        "schema_version": (
                            "openclaw.approval-a.outer-lock-proof.v1"
                        ),
                        "pid": 999999,
                        "token": "0" * 64,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lock.chmod(0o600)
            run_root = root / "A-20260715-01"
            run_root.mkdir(mode=0o711)
            (run_root / "candidate.bin").write_bytes(b"stale-candidate")
        return retention.RetentionConfig(
            execution_root=root,
            artifact_root=base / "artifacts",
            expected_uid=os.getuid(),
            # BSD directory creation inherits the parent's group. The short
            # macOS Unix-socket fixture root is under /private/tmp (wheel),
            # while the user's default temp root may inherit the login group.
            # Bind the intended fixture owner, not the process's primary group.
            expected_gid=base.stat().st_gid,
            require_root=False,
        )

    def test_dry_run_reports_candidate_without_deleting_root(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="approval-a-retention-preview-"
        ) as raw:
            config = self.make_config(Path(raw))

            rc, marker, detail, report_path = retention.run(
                apply=False,
                config=config,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(marker, "APPROVAL_A_RETENTION_DRY_RUN")
            self.assertIn("result: dry_run_candidates", detail)
            self.assertTrue(config.execution_root.exists())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "dry_run_complete")
            self.assertEqual(payload["summary"]["candidate_count"], 1)
            self.assertEqual(payload["root"]["state"], "would_remove")
            self.assertEqual(
                payload["root"]["children"],
                ["A-20260715-01", retention.LOCK_NAME],
            )

    def test_apply_deletes_exact_root_and_second_run_is_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="approval-a-retention-apply-"
        ) as raw:
            config = self.make_config(Path(raw))

            rc, marker, detail, report_path = retention.run(
                apply=True,
                config=config,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(marker, "APPROVAL_A_RETENTION_OK")
            self.assertIn("result: removed_stale_approval_a", detail)
            self.assertFalse(os.path.lexists(config.execution_root))
            self.assertEqual(
                list(
                    config.execution_root.parent.glob(
                        f"{retention.RETIRING_PREFIX}*"
                    )
                ),
                [],
            )
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "apply_complete")
            self.assertEqual(payload["summary"]["removed_count"], 1)
            self.assertEqual(payload["root"]["state"], "removed")
            self.assertFalse(payload["root"]["after_exists"])

            second_rc, second_marker, second_detail, second_report = (
                retention.run(apply=True, config=config)
            )
            self.assertEqual(second_rc, 0)
            self.assertEqual(second_marker, "APPROVAL_A_RETENTION_OK")
            self.assertIn("result: approval_a_absent", second_detail)
            second_payload = json.loads(
                second_report.read_text(encoding="utf-8")
            )
            self.assertEqual(second_payload["summary"]["removed_count"], 0)

    def test_apply_clears_immutable_fixture_before_removal(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="approval-a-retention-immutable-"
        ) as raw:
            config = self.make_config(Path(raw))
            fixture = (
                config.execution_root
                / "A-20260715-01"
                / "candidate.bin"
            )
            set_flags = subprocess.run(
                [str(retention.CHFLAGS), "uchg", str(fixture)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(set_flags.returncode, 0, set_flags.stderr)
            self.assertTrue(
                fixture.lstat().st_flags & stat.UF_IMMUTABLE
            )
            try:
                rc, marker, detail, _ = retention.run(
                    apply=True,
                    config=config,
                )
            finally:
                if os.path.lexists(config.execution_root):
                    subprocess.run(
                        [
                            str(retention.CHFLAGS),
                            "-R",
                            "-P",
                            "noschg,nouchg",
                            str(config.execution_root),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )

            self.assertEqual(rc, 0)
            self.assertEqual(marker, "APPROVAL_A_RETENTION_OK")
            self.assertIn("result: removed_stale_approval_a", detail)
            self.assertFalse(os.path.lexists(config.execution_root))

    def test_chflags_failure_preserves_stage_and_retry_converges(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="approval-a-retention-chflags-failure-"
        ) as raw:
            config = self.make_config(Path(raw))
            real_run = retention.subprocess.run
            observed_chflags: list[list[str]] = []

            def fail_chflags(
                argv: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                if argv and argv[0] == str(retention.CHFLAGS):
                    observed_chflags.append(argv)
                    return subprocess.CompletedProcess(
                        argv,
                        1,
                        stdout="",
                        stderr="Operation not permitted",
                    )
                return real_run(argv, **kwargs)

            with mock.patch.object(
                retention.subprocess,
                "run",
                side_effect=fail_chflags,
            ):
                rc, marker, detail, report_path = retention.run(
                    apply=True,
                    config=config,
                )

            self.assertEqual(rc, 1)
            self.assertEqual(marker, "APPROVAL_A_RETENTION_BLOCKED")
            self.assertIn("immutable flag clearing failed", detail)
            self.assertFalse(os.path.lexists(config.execution_root))
            staged = list(
                config.execution_root.parent.glob(
                    f"{retention.RETIRING_PREFIX}*"
                )
            )
            self.assertEqual(len(staged), 1)
            self.assertEqual(
                observed_chflags,
                [
                    [
                        str(retention.CHFLAGS),
                        "-R",
                        "-P",
                        "noschg,nouchg",
                        str(staged[0]),
                    ]
                ],
            )
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["root"]["state"], "remove_failed")
            self.assertEqual(payload["root"]["renamed_path"], str(staged[0]))

            retry_rc, retry_marker, retry_detail, _ = retention.run(
                apply=True,
                config=config,
            )
            self.assertEqual(retry_rc, 0)
            self.assertEqual(retry_marker, "APPROVAL_A_RETENTION_OK")
            self.assertIn("result: removed_stale_approval_a", retry_detail)
            self.assertFalse(staged[0].exists())

    def test_held_singleton_lock_protects_active_root(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="approval-a-retention-active-"
        ) as raw:
            config = self.make_config(Path(raw))
            with (config.execution_root / retention.LOCK_NAME).open(
                "r+b"
            ) as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    rc, marker, detail, report_path = retention.run(
                        apply=True,
                        config=config,
                    )
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, "APPROVAL_A_RETENTION_OK")
            self.assertIn("result: active_approval_a_protected", detail)
            self.assertTrue(config.execution_root.exists())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["root"]["state"], "active_protected")
            self.assertEqual(payload["summary"]["removed_count"], 0)

    def test_unexpected_direct_child_blocks_and_preserves_everything(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="approval-a-retention-unknown-"
        ) as raw:
            config = self.make_config(Path(raw))
            unexpected = config.execution_root / "do-not-classify"
            unexpected.mkdir()

            rc, marker, detail, report_path = retention.run(
                apply=True,
                config=config,
            )

            self.assertEqual(rc, 1)
            self.assertEqual(marker, "APPROVAL_A_RETENTION_BLOCKED")
            self.assertIn("unexpected Approval A direct child", detail)
            self.assertTrue(config.execution_root.exists())
            self.assertTrue(unexpected.exists())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "apply_blocked")
            self.assertEqual(payload["summary"]["removed_count"], 0)

    def test_symlink_execution_root_is_rejected_without_following_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="approval-a-retention-symlink-"
        ) as raw:
            base = Path(raw)
            config = self.make_config(base, create_root=False)
            target = base / "unrelated"
            target.mkdir()
            (target / "sentinel").write_text("preserve", encoding="utf-8")
            config.execution_root.symlink_to(target, target_is_directory=True)

            rc, marker, detail, _ = retention.run(
                apply=True,
                config=config,
            )

            self.assertEqual(rc, 1)
            self.assertEqual(marker, "APPROVAL_A_RETENTION_BLOCKED")
            self.assertIn("execution root identity is invalid", detail)
            self.assertEqual(
                (target / "sentinel").read_text(encoding="utf-8"),
                "preserve",
            )
            self.assertTrue(config.execution_root.is_symlink())

    def test_apply_finishes_helper_owned_interrupted_retirement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="approval-a-retention-resume-"
        ) as raw:
            base = Path(raw)
            config = self.make_config(base, create_root=False)
            operation_id = (
                "approval-a-retention-20260726T120000000000Z-"
                "12345-0123456789abcdef0123456789abcdef"
            )
            staged = retention.retiring_path(config, operation_id)
            staged.mkdir(mode=0o711)
            (staged / "partial.bin").write_bytes(b"partial")

            rc, marker, detail, report_path = retention.run(
                apply=True,
                config=config,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(marker, "APPROVAL_A_RETENTION_OK")
            self.assertIn("result: removed_stale_approval_a", detail)
            self.assertFalse(staged.exists())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["removed_count"], 1)
            self.assertEqual(
                payload["interrupted_retirements"][0]["state"],
                "removed",
            )

    def test_interrupted_delete_is_reported_and_next_run_converges(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="approval-a-retention-interrupt-"
        ) as raw:
            config = self.make_config(Path(raw))
            with mock.patch.object(
                retention.shutil,
                "rmtree",
                side_effect=OSError("simulated interruption"),
            ):
                rc, marker, detail, report_path = retention.run(
                    apply=True,
                    config=config,
                )

            self.assertEqual(rc, 1)
            self.assertEqual(marker, "APPROVAL_A_RETENTION_BLOCKED")
            self.assertIn("simulated interruption", detail)
            self.assertFalse(os.path.lexists(config.execution_root))
            staged = list(
                config.execution_root.parent.glob(
                    f"{retention.RETIRING_PREFIX}*"
                )
            )
            self.assertEqual(len(staged), 1)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "apply_blocked")
            self.assertEqual(payload["root"]["state"], "remove_failed")
            self.assertEqual(payload["root"]["renamed_path"], str(staged[0]))

            retry_rc, retry_marker, retry_detail, _ = retention.run(
                apply=True,
                config=config,
            )
            self.assertEqual(retry_rc, 0)
            self.assertEqual(retry_marker, "APPROVAL_A_RETENTION_OK")
            self.assertIn("result: removed_stale_approval_a", retry_detail)
            self.assertFalse(staged[0].exists())

    def test_cli_help_and_unknown_flags_do_not_enter_retention(self) -> None:
        for argv, expected in ((["--help"], 0), (["--a"], 2)):
            with self.subTest(argv=argv):
                with mock.patch.object(retention, "run") as run:
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaises(SystemExit) as raised:
                            retention.main(argv)
                self.assertEqual(raised.exception.code, expected)
                run.assert_not_called()

    def test_cli_requires_root_before_classifying_live_paths(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="approval-a-retention-root-gate-"
        ) as raw:
            config = self.make_config(Path(raw))
            config = retention.RetentionConfig(
                execution_root=config.execution_root,
                artifact_root=config.artifact_root,
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
                require_root=True,
            )
            with mock.patch.object(retention.os, "geteuid", return_value=502):
                rc, marker, detail, _ = retention.run(
                    apply=True,
                    config=config,
                )

            self.assertEqual(rc, 1)
            self.assertEqual(marker, "APPROVAL_A_RETENTION_BLOCKED")
            self.assertIn("requires root", detail)
            self.assertTrue(config.execution_root.exists())


if __name__ == "__main__":
    unittest.main()
