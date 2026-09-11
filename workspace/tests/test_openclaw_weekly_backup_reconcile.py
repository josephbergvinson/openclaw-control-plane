from __future__ import annotations

from dataclasses import replace
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from scripts import openclaw_weekly_backup_reconcile as reconcile


TASK_ID = "725dcdc4-0003-4712-8d96-85df4ccc3b12"
JOB_ID = "00000000-0000-4000-8000-000000000001"
TASK_RUN_ID = "cron:{}:1786248900015".format(JOB_ID)
BACKUP_RUN_ID = "20260809T041548Z"
CHILD_PID = 4984
RECONCILIATION_ID = "a" * 32
NATIVE_PUBLICATION = ".openclaw-backup-publish-512812c4-0003-4712-8d96-85df4ccc3b12-Ab1xY9"


class WeeklyBackupReconcileTests(unittest.TestCase):
    def fixture(
        self, root: Path
    ) -> tuple[
        reconcile.ReconciliationConfig,
        dict[str, object],
    ]:
        backups = root / "Backups"
        weekly = backups / "weekly"
        weekly.mkdir(parents=True, mode=0o700)
        os.chmod(weekly, 0o700)
        lock = backups / ".weekly-backup.lock"
        lock.write_bytes(b"")
        os.chmod(lock, 0o600)
        staging = weekly / (
            ".openclaw-archive-v3-{}.incomplete-{}".format(
                BACKUP_RUN_ID,
                CHILD_PID,
            )
        )
        staging.mkdir(mode=0o700)
        for name, payload in (
            ("git-remotes.tgz", b"partial git archive\n"),
            ("openclaw-state.tgz", b"partial state archive\n"),
        ):
            (staging / name).write_bytes(payload)
            os.chmod(staging / name, 0o600)

        artifacts = root / "artifacts"
        artifacts.mkdir()
        phase_path = artifacts / "phase.json"
        phase_payload = {
            "schema_version": "openclaw.weekly_archive_backup.phase_receipt.v3",
            "run_id": BACKUP_RUN_ID,
            "invocation_id": "{}-{}-{}".format(
                BACKUP_RUN_ID,
                CHILD_PID,
                "b" * 32,
            ),
            "started_at_utc": "2026-08-09T04:15:48.293816Z",
            "updated_at_utc": "2026-08-09T04:40:43.257260Z",
            "status": "in_progress",
            "current_phase": "archive-git-remotes",
            "destination": str(
                weekly / "openclaw-archive-v3-{}".format(BACKUP_RUN_ID)
            ),
            "phases": [
                {
                    "name": "preflight",
                    "status": "verified",
                    "at_utc": "2026-08-09T04:39:13.917929Z",
                },
                {
                    "name": "archive-git-remotes",
                    "status": "created",
                    "at_utc": "2026-08-09T04:40:43.257176Z",
                },
            ],
            "blockers": [],
        }
        self.write_json(phase_path, phase_payload)

        process_path = (
            artifacts
            / "cron-entrypoint-20260809T041546023701Z-4969.json"
        )
        producer_script = root / "scripts/openclaw_weekly_archive_backup.py"
        producer_script.parent.mkdir()
        producer_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        os.chmod(producer_script, 0o755)
        process_payload = {
            "schema_version": "openclaw.cron_python_entrypoint.process_receipt.v2",
            "receipt_owner": "supervisor",
            "what": "run bounded three-archive OWC weekly OpenClaw recovery set",
            "requested_script": str(producer_script),
            "resolved_script": str(producer_script),
            "started_at": "2026-08-09T04:15:46.023171Z",
            "ended_at": "2026-08-09T06:10:46.246333Z",
            "updated_at": "2026-08-09T06:10:46.246421Z",
            "status": "terminated_parent_lost",
            "child": {
                "pid": CHILD_PID,
                "process_group_id": CHILD_PID,
                "exit_code": -15,
                "signal": 15,
            },
            "supervisor": {
                "pid": 4970,
                "process_group_id": 4970,
            },
            "termination": {
                "requested": True,
                "reason": "parent_process_lost",
                "term_sent": True,
                "kill_sent": False,
            },
            "residual_process": {
                "checked": True,
                "process_group_alive": False,
                "process_group_alive_after_cleanup": False,
            },
            "business_effect": None,
        }
        self.write_json(process_path, process_payload)

        openclaw_cli = root / "bin/openclaw"
        openclaw_cli.parent.mkdir()
        openclaw_cli.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        os.chmod(openclaw_cli, 0o755)
        config = reconcile.ReconciliationConfig(
            weekly_root=weekly,
            staging=staging,
            quarantine_root=backups / "quarantine",
            receipt_dir=artifacts / "reconciliations",
            phase_receipt=phase_path,
            process_receipt=process_path,
            producer_script=producer_script,
            openclaw_cli=openclaw_cli,
            task_id=TASK_ID,
            task_run_id=TASK_RUN_ID,
            job_id=JOB_ID,
        )
        task = {
            "taskId": TASK_ID,
            "runtime": "cron",
            "sourceId": JOB_ID,
            "runId": TASK_RUN_ID,
            "label": "OpenClaw Weekly Backup",
            "status": "succeeded",
            "endedAt": 1786255855078,
        }
        return config, task

    @staticmethod
    def write_json(path: Path, payload: dict[str, object]) -> None:
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)

    @staticmethod
    def task_reader(task: dict[str, object]):
        return lambda _cli, _task_id: dict(task)

    def call(
        self,
        config: reconcile.ReconciliationConfig,
        task: dict[str, object],
        *,
        apply: bool,
        pid_alive=lambda _pid: False,
        pgid_alive=lambda _pgid: False,
    ):
        return reconcile.reconcile(
            config,
            apply=apply,
            task_reader=self.task_reader(task),
            pid_alive=pid_alive,
            pgid_alive=pgid_alive,
            reconciliation_id=RECONCILIATION_ID,
        )

    def test_dry_run_validates_without_writing_or_moving(self) -> None:
        with tempfile.TemporaryDirectory(prefix="weekly-reconcile-dry-") as raw:
            config, task = self.fixture(Path(raw))

            code, result = self.call(config, task, apply=False)

            self.assertEqual(code, 0)
            self.assertEqual(result["schema_version"], reconcile.PREVIEW_SCHEMA)
            self.assertEqual(result["mode"], "dry_run")
            self.assertEqual(result["corrected_outcome"], "failed_not_published")
            self.assertTrue(result["supersedes_historical_success_claim"])
            self.assertFalse(result["task_registry_mutated"])
            self.assertFalse(result["sqlite_mutated"])
            self.assertTrue(config.staging.is_dir())
            self.assertFalse(config.quarantine_root.exists())
            self.assertFalse(config.receipt_dir.exists())

    def test_native_v4_staging_uses_its_own_final_and_quarantine_identity(self) -> None:
        for apply in (False, True):
            with self.subTest(apply=apply), tempfile.TemporaryDirectory() as raw:
                config, task = self.fixture(Path(raw))
                staging = config.staging.with_name(config.staging.name.replace("-v3-", "-v4-"))
                config.staging.rename(staging)
                config = replace(config, staging=staging)
                phase = json.loads(config.phase_receipt.read_text())
                phase["destination"] = phase["destination"].replace("-v3-", "-v4-")
                self.write_json(config.phase_receipt, phase)
                code, result = self.call(config, task, apply=apply)
                self.assertEqual(code, 0, result)
                if apply:
                    quarantines = list(config.quarantine_root.iterdir())
                    self.assertEqual(len(quarantines), 1)
                    self.assertTrue(reconcile.QUARANTINE_NAME_RE.fullmatch(quarantines[0].name))
                    self.assertEqual((quarantines[0] / "openclaw-state.tgz").read_bytes(), b"partial state archive\n")
                else:
                    self.assertTrue(staging.is_dir())

    def test_v4_native_publication_residue_is_preserved_in_quarantine(self) -> None:
        for apply in (False, True):
            with self.subTest(apply=apply), tempfile.TemporaryDirectory() as raw:
                config, task = self.fixture(Path(raw))
                staging = config.staging.with_name(config.staging.name.replace("-v3-", "-v4-"))
                config.staging.rename(staging)
                config = replace(config, staging=staging)
                phase = json.loads(config.phase_receipt.read_text())
                phase["destination"] = phase["destination"].replace("-v3-", "-v4-")
                self.write_json(config.phase_receipt, phase)
                native = staging / NATIVE_PUBLICATION
                native.mkdir(mode=0o700)
                archive = native / "archive.tar.gz.tmp"
                archive.write_bytes(b"partial native archive\n")
                archive.chmod(0o600)
                before = (archive.stat().st_dev, archive.stat().st_ino, archive.read_bytes())
                code, result = self.call(config, task, apply=apply)
                self.assertEqual(code, 0, result)
                owner = next(config.quarantine_root.iterdir()) if apply else staging
                retained = owner / NATIVE_PUBLICATION / archive.name
                self.assertEqual((retained.stat().st_dev, retained.stat().st_ino, retained.read_bytes()), before)
                self.assertFalse((config.weekly_root / staging.name[1:].split(".incomplete-", 1)[0]).exists())

    def test_native_publication_residue_does_not_broaden_v3_or_allow_escapes(self) -> None:
        for invalid in ("v3", "name", "top-symlink", "child-symlink", "descendant"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                config, task = self.fixture(root)
                if invalid != "v3":
                    staging = config.staging.with_name(config.staging.name.replace("-v3-", "-v4-"))
                    config.staging.rename(staging)
                    config = replace(config, staging=staging)
                    phase = json.loads(config.phase_receipt.read_text())
                    phase["destination"] = phase["destination"].replace("-v3-", "-v4-")
                    self.write_json(config.phase_receipt, phase)
                outside = root / "outside"
                outside.mkdir()
                (outside / "keep").write_bytes(b"outside retained\n")
                native = config.staging / (".openclaw-backup-publish-anything" if invalid == "name" else NATIVE_PUBLICATION)
                if invalid == "top-symlink":
                    native.symlink_to(outside, target_is_directory=True)
                else:
                    native.mkdir(mode=0o700)
                    if invalid == "child-symlink":
                        (native / "archive.tar.gz.tmp").symlink_to(outside / "keep")
                    elif invalid == "descendant":
                        (native / "unowned-child").mkdir()
                with self.assertRaises(reconcile.ReconciliationError):
                    self.call(config, task, apply=True)
                self.assertTrue(config.staging.is_dir())
                self.assertFalse(config.quarantine_root.exists())
                self.assertEqual((outside / "keep").read_bytes(), b"outside retained\n")

    def test_post_state_keeps_source_retention_unknown_on_final_read_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-observation-error-"
        ) as raw:
            config, _task = self.fixture(Path(raw))
            staging_info = config.staging.lstat()
            held_source = config.staging.with_name("held-partial-source")
            config.staging.rename(held_source)
            final = config.weekly_root / (
                "openclaw-archive-v3-{}".format(BACKUP_RUN_ID)
            )
            target = config.quarantine_root / (
                config.staging.name + ".quarantine-" + RECONCILIATION_ID
            )
            original_lstat = Path.lstat

            def fail_final_lstat(path: Path):
                if path == final:
                    raise PermissionError("injected final observation failure")
                return original_lstat(path)

            with mock.patch.object(Path, "lstat", new=fail_final_lstat):
                observed = reconcile.observe_post_state(
                    config=config,
                    target=target,
                    final=final,
                    staging_identity=(
                        staging_info.st_dev,
                        staging_info.st_ino,
                    ),
                )

            self.assertFalse(observed["staging_present_after"])
            self.assertFalse(observed["quarantine_present_after"])
            self.assertIsNone(observed["published_destination_present_after"])
            self.assertIsNone(
                observed["published_destination_identity_matches_after"]
            )
            self.assertEqual(observed["remaining_incomplete_staging"], [])
            self.assertEqual(observed["source_locations_after"], [])
            self.assertIsNone(observed["source_retained"])
            self.assertFalse(observed["future_rerun_unblocked"])
            self.assertEqual(len(observed["post_state_observation_errors"]), 1)
            self.assertIn(
                "injected final observation failure",
                observed["post_state_observation_errors"][0],
            )

    def test_task_cli_accepts_the_single_json_stream_used_by_openclaw(self) -> None:
        with tempfile.TemporaryDirectory(prefix="weekly-reconcile-cli-") as raw:
            cli = Path(raw) / "openclaw"
            task = {"taskId": TASK_ID, "status": "succeeded"}
            cli.write_text(
                "#!/bin/sh\nprintf '%s\\n' '" + json.dumps(task) + "' >&2\n",
                encoding="utf-8",
            )
            os.chmod(cli, 0o755)

            self.assertEqual(reconcile.read_task_via_cli(cli, TASK_ID), task)

            cli.write_text(
                "#!/bin/sh\nprintf 'noise\\n'\nprintf '%s\\n' '"
                + json.dumps(task)
                + "' >&2\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "exactly one JSON stream",
            ):
                reconcile.read_task_via_cli(cli, TASK_ID)

    def test_apply_quarantines_atomically_and_writes_append_only_receipts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="weekly-reconcile-apply-") as raw:
            config, task = self.fixture(Path(raw))
            before = config.staging.lstat()

            code, result = self.call(config, task, apply=True)

            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "completed")
            self.assertFalse(config.staging.exists())
            quarantine = Path(result["quarantine"])
            self.assertTrue(quarantine.is_dir())
            after = quarantine.lstat()
            self.assertEqual(
                (after.st_dev, after.st_ino),
                (before.st_dev, before.st_ino),
            )
            self.assertEqual(
                {child.name for child in quarantine.iterdir()},
                {"git-remotes.tgz", "openclaw-state.tgz"},
            )
            self.assertTrue(result["future_rerun_unblocked"])
            self.assertEqual(
                result["incomplete_staging_before"],
                [config.staging.name],
            )
            self.assertEqual(result["remaining_incomplete_staging"], [])
            intent_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-intent.json".format(
                    RECONCILIATION_ID
                )
            )
            completion_path = Path(result["completion_receipt"])
            self.assertTrue(intent_path.is_file())
            self.assertTrue(completion_path.is_file())
            self.assertEqual(stat.S_IMODE(intent_path.stat().st_mode), 0o600)
            intent_raw = intent_path.read_bytes()
            completion_raw = completion_path.read_bytes()
            completion = json.loads(completion_raw)
            self.assertEqual(completion["schema_version"], reconcile.COMPLETION_SCHEMA)
            self.assertEqual(
                completion["intent_receipt_sha256"],
                hashlib.sha256(intent_raw).hexdigest(),
            )
            self.assertEqual(
                result["completion_receipt_sha256"],
                hashlib.sha256(completion_raw).hexdigest(),
            )
            self.assertFalse(completion["task_registry_mutated"])
            self.assertFalse(completion["sqlite_mutated"])
            self.assertFalse(completion["delete_performed"])

    def test_rejects_a_second_incomplete_generation_before_intent(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-second-residue-"
        ) as raw:
            config, task = self.fixture(Path(raw))
            second = config.weekly_root / (
                ".openclaw-archive-v3-20260808T041548Z.incomplete-6000"
            )
            second.mkdir(mode=0o700)

            with self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "not the sole incomplete generation",
            ):
                self.call(config, task, apply=True)

            self.assertTrue(config.staging.is_dir())
            self.assertTrue(second.is_dir())
            self.assertFalse(config.receipt_dir.exists())

    def test_post_move_second_residue_writes_truthful_failure_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-post-move-residue-"
        ) as raw:
            config, task = self.fixture(Path(raw))
            second = config.weekly_root / (
                ".openclaw-archive-v3-20260808T041548Z.incomplete-6000"
            )
            original_quarantine = reconcile.quarantine_staging

            def quarantine_then_inject(**kwargs):
                original_quarantine(**kwargs)
                second.mkdir(mode=0o700)

            with mock.patch.object(
                reconcile,
                "quarantine_staging",
                side_effect=quarantine_then_inject,
            ), self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "still contains an incomplete generation",
            ):
                self.call(config, task, apply=True)

            failure_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-failed.json".format(
                    RECONCILIATION_ID
                )
            )
            completion_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-completed.json".format(
                    RECONCILIATION_ID
                )
            )
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            self.assertEqual(failure["schema_version"], reconcile.FAILURE_SCHEMA)
            self.assertFalse(failure["staging_present_after"])
            self.assertTrue(failure["quarantine_present_after"])
            self.assertFalse(failure["published_destination_present_after"])
            self.assertEqual(
                failure["remaining_incomplete_staging"],
                [second.name],
            )
            self.assertEqual(failure["post_state_observation_errors"], [])
            self.assertEqual(failure["source_locations_after"], ["quarantine"])
            self.assertTrue(failure["source_retained"])
            self.assertFalse(failure["future_rerun_unblocked"])
            self.assertTrue(second.is_dir())
            self.assertFalse(completion_path.exists())

    def test_exdev_rename_writes_truthful_failure_receipt(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-exdev-"
        ) as raw:
            config, task = self.fixture(Path(raw))
            before = config.staging.lstat()

            with mock.patch.object(
                reconcile.os,
                "rename",
                side_effect=OSError(errno.EXDEV, "injected cross-device move"),
            ), self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "injected cross-device move",
            ):
                self.call(config, task, apply=True)

            failure_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-failed.json".format(
                    RECONCILIATION_ID
                )
            )
            completion_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-completed.json".format(
                    RECONCILIATION_ID
                )
            )
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            after = config.staging.lstat()
            self.assertEqual(
                (after.st_dev, after.st_ino),
                (before.st_dev, before.st_ino),
            )
            self.assertTrue(failure["intent_receipt_published"])
            self.assertTrue(failure["staging_present_after"])
            self.assertTrue(failure["staging_identity_matches_after"])
            self.assertFalse(failure["quarantine_present_after"])
            self.assertFalse(failure["published_destination_present_after"])
            self.assertEqual(
                failure["remaining_incomplete_staging"],
                [config.staging.name],
            )
            self.assertEqual(failure["source_locations_after"], ["staging"])
            self.assertTrue(failure["source_retained"])
            self.assertFalse(failure["future_rerun_unblocked"])
            self.assertEqual(failure["post_state_observation_errors"], [])
            self.assertFalse(completion_path.exists())

    def test_final_created_after_intent_blocks_move_truthfully(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-late-final-"
        ) as raw:
            config, task = self.fixture(Path(raw))
            final = config.weekly_root / (
                "openclaw-archive-v3-{}".format(BACKUP_RUN_ID)
            )
            original_quarantine = reconcile.quarantine_staging

            def create_final_then_quarantine(**kwargs):
                final.mkdir(mode=0o700)
                original_quarantine(**kwargs)

            with mock.patch.object(
                reconcile,
                "quarantine_staging",
                side_effect=create_final_then_quarantine,
            ), self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "published destination appeared before quarantine",
            ):
                self.call(config, task, apply=True)

            failure_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-failed.json".format(
                    RECONCILIATION_ID
                )
            )
            completion_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-completed.json".format(
                    RECONCILIATION_ID
                )
            )
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            self.assertTrue(config.staging.is_dir())
            self.assertTrue(final.is_dir())
            self.assertTrue(failure["intent_receipt_published"])
            self.assertTrue(failure["staging_identity_matches_after"])
            self.assertFalse(failure["quarantine_present_after"])
            self.assertTrue(failure["published_destination_present_after"])
            self.assertFalse(
                failure["published_destination_identity_matches_after"]
            )
            self.assertEqual(
                failure["remaining_incomplete_staging"],
                [config.staging.name],
            )
            self.assertEqual(failure["source_locations_after"], ["staging"])
            self.assertTrue(failure["source_retained"])
            self.assertFalse(failure["future_rerun_unblocked"])
            self.assertFalse(completion_path.exists())

    def test_post_link_intent_failure_writes_terminal_failure_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-intent-fsync-"
        ) as raw:
            config, task = self.fixture(Path(raw))
            original_fsync_dir = reconcile.archive_backup.fsync_dir
            calls = 0

            def fail_intent_directory_fsync(path: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError(errno.EIO, "injected post-link intent fsync")
                original_fsync_dir(path)

            with mock.patch.object(
                reconcile.archive_backup,
                "fsync_dir",
                side_effect=fail_intent_directory_fsync,
            ), self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "injected post-link intent fsync",
            ):
                self.call(config, task, apply=True)

            intent_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-intent.json".format(
                    RECONCILIATION_ID
                )
            )
            failure_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-failed.json".format(
                    RECONCILIATION_ID
                )
            )
            completion_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-completed.json".format(
                    RECONCILIATION_ID
                )
            )
            intent_raw = intent_path.read_bytes()
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            self.assertTrue(failure["intent_receipt_published"])
            self.assertEqual(
                failure["intent_receipt_observed_sha256"],
                hashlib.sha256(intent_raw).hexdigest(),
            )
            self.assertIsNone(failure["intent_receipt_observation_error"])
            self.assertTrue(failure["staging_identity_matches_after"])
            self.assertFalse(failure["quarantine_present_after"])
            self.assertEqual(
                failure["remaining_incomplete_staging"],
                [config.staging.name],
            )
            self.assertEqual(failure["source_locations_after"], ["staging"])
            self.assertTrue(failure["source_retained"])
            self.assertFalse(failure["future_rerun_unblocked"])
            self.assertFalse(completion_path.exists())

    def test_post_link_completion_failure_recovers_exact_receipt_as_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-completion-fsync-"
        ) as raw:
            config, task = self.fixture(Path(raw))
            original_fsync_dir = reconcile.archive_backup.fsync_dir
            calls = 0

            def fail_completion_directory_fsync_once(path: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise OSError(
                        errno.EIO,
                        "injected post-link completion fsync",
                    )
                original_fsync_dir(path)

            with mock.patch.object(
                reconcile.archive_backup,
                "fsync_dir",
                side_effect=fail_completion_directory_fsync_once,
            ):
                code, result = self.call(config, task, apply=True)

            completion_path = Path(result["completion_receipt"])
            failure_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-failed.json".format(
                    RECONCILIATION_ID
                )
            )
            completion_raw = completion_path.read_bytes()
            self.assertEqual(code, 0)
            self.assertTrue(result["completion_receipt_recovered_after_error"])
            self.assertEqual(
                result["completion_receipt_sha256"],
                hashlib.sha256(completion_raw).hexdigest(),
            )
            self.assertFalse(config.staging.exists())
            self.assertTrue(Path(result["quarantine"]).is_dir())
            self.assertFalse(failure_path.exists())

    def test_persistent_completion_recovery_failure_records_dual_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-completion-dual-"
        ) as raw:
            config, task = self.fixture(Path(raw))
            original_fsync_dir = reconcile.archive_backup.fsync_dir
            calls = 0

            def fail_completion_and_recovery_fsync(path: Path) -> None:
                nonlocal calls
                calls += 1
                if calls in {4, 5}:
                    raise OSError(
                        errno.EIO,
                        "injected persistent completion fsync",
                    )
                original_fsync_dir(path)

            with mock.patch.object(
                reconcile.archive_backup,
                "fsync_dir",
                side_effect=fail_completion_and_recovery_fsync,
            ), self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "injected persistent completion fsync",
            ):
                self.call(config, task, apply=True)

            completion_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-completed.json".format(
                    RECONCILIATION_ID
                )
            )
            failure_path = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-failed.json".format(
                    RECONCILIATION_ID
                )
            )
            completion_raw = completion_path.read_bytes()
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            self.assertTrue(failure["completion_receipt_attempted"])
            self.assertTrue(failure["completion_receipt_published"])
            self.assertEqual(
                failure["completion_receipt_expected_sha256"],
                hashlib.sha256(completion_raw).hexdigest(),
            )
            self.assertEqual(
                failure["completion_receipt_observed_sha256"],
                hashlib.sha256(completion_raw).hexdigest(),
            )
            self.assertIsNone(failure["completion_receipt_observation_error"])
            self.assertIn(
                "injected persistent completion fsync",
                failure["completion_receipt_recovery_error"],
            )
            self.assertEqual(
                failure["terminal_receipt_state"],
                "completion_observed_then_failure_recorded",
            )
            self.assertFalse(failure["staging_present_after"])
            self.assertTrue(failure["quarantine_identity_matches_after"])
            self.assertEqual(failure["remaining_incomplete_staging"], [])
            self.assertTrue(failure["future_rerun_unblocked"])

    def test_locked_inventory_rejects_a_racing_unknown_top_level_entry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-inventory-race-"
        ) as raw:
            config, task = self.fixture(Path(raw))
            original_inventory = reconcile.tree_inventory
            injected = False

            def inventory_after_injection(root: Path, expected_device: int):
                nonlocal injected
                if root == config.staging and not injected:
                    injected = True
                    (root / "unexpected.bin").write_bytes(b"racing residue\n")
                return original_inventory(root, expected_device)

            with mock.patch.object(
                reconcile,
                "tree_inventory",
                side_effect=inventory_after_injection,
            ), self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "unknown top-level entry",
            ):
                self.call(config, task, apply=False)

            self.assertTrue(injected)
            self.assertTrue((config.staging / "unexpected.bin").is_file())
            self.assertFalse(config.receipt_dir.exists())

    def test_rejects_cross_device_quarantine_and_final_collision(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-cross-device-"
        ) as raw:
            config, task = self.fixture(Path(raw))
            config.quarantine_root.mkdir(mode=0o700)
            original_lstat = Path.lstat

            def cross_device_lstat(path: Path):
                info = original_lstat(path)
                if path == config.quarantine_root:
                    fields = list(info)
                    fields[2] = info.st_dev + 1
                    return os.stat_result(fields)
                return info

            with mock.patch.object(
                Path,
                "lstat",
                new=cross_device_lstat,
            ), self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "quarantine root is not a physical same-device directory",
            ):
                self.call(config, task, apply=False)

        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-final-collision-"
        ) as raw:
            config, task = self.fixture(Path(raw))
            final = config.weekly_root / (
                "openclaw-archive-v3-{}".format(BACKUP_RUN_ID)
            )
            final.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "published generation already exists",
            ):
                self.call(config, task, apply=False)
            self.assertTrue(config.staging.is_dir())
            self.assertFalse(config.receipt_dir.exists())

    def test_rejects_receipt_and_lock_identity_swaps(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-receipt-swap-"
        ) as raw:
            config, _task = self.fixture(Path(raw))
            receipt = config.phase_receipt
            original = receipt.with_name("phase-original.json")
            real_open = reconcile.os.open
            swapped = False

            def open_after_receipt_swap(path, flags, *args, **kwargs):
                nonlocal swapped
                if Path(path) == receipt and not swapped:
                    swapped = True
                    receipt.rename(original)
                    self.write_json(receipt, {"replacement": True})
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                reconcile.os,
                "open",
                side_effect=open_after_receipt_swap,
            ), self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "identity changed between lstat and open",
            ):
                reconcile.load_physical_json(receipt, "phase receipt")
            self.assertTrue(swapped)

        with tempfile.TemporaryDirectory(
            prefix="weekly-reconcile-lock-swap-"
        ) as raw:
            config, _task = self.fixture(Path(raw))
            lock_path = config.weekly_root.parent / ".weekly-backup.lock"
            original = lock_path.with_name(".weekly-backup.lock.original")
            real_open = reconcile.os.open
            swapped = False

            def open_after_lock_swap(path, flags, *args, **kwargs):
                nonlocal swapped
                if Path(path) == lock_path and not swapped:
                    swapped = True
                    lock_path.rename(original)
                    lock_path.write_bytes(b"replacement lock\n")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                reconcile.os,
                "open",
                side_effect=open_after_lock_swap,
            ), self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "lock identity changed between lstat and open",
            ):
                reconcile.acquire_existing_backup_lock(
                    config.weekly_root,
                    config.weekly_root.stat().st_dev,
                )
            self.assertTrue(swapped)

    def test_rejects_mismatched_evidence_and_live_owners(self) -> None:
        cases = (
            ("task", "task id mismatch"),
            ("phase", "phase receipt run id mismatch"),
            ("process", "not a parent-loss termination"),
            ("process_operation", "owner or operation mismatch"),
            ("pid", "process pid is still live"),
            ("pgid", "process group is still live"),
        )
        for case, expected in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                prefix="weekly-reconcile-reject-"
            ) as raw:
                config, task = self.fixture(Path(raw))
                pid_alive = lambda _pid: False
                pgid_alive = lambda _pgid: False
                if case == "task":
                    task["taskId"] = "00000000-0000-0000-0000-000000000000"
                elif case == "phase":
                    payload = json.loads(config.phase_receipt.read_text())
                    payload["run_id"] = "20260808T041548Z"
                    self.write_json(config.phase_receipt, payload)
                elif case == "process":
                    payload = json.loads(config.process_receipt.read_text())
                    payload["status"] = "completed"
                    self.write_json(config.process_receipt, payload)
                elif case == "process_operation":
                    payload = json.loads(config.process_receipt.read_text())
                    payload["what"] = "run some other operation"
                    self.write_json(config.process_receipt, payload)
                elif case == "pid":
                    pid_alive = lambda pid: pid == 4969
                else:
                    pgid_alive = lambda pgid: pgid == CHILD_PID

                with self.assertRaisesRegex(reconcile.ReconciliationError, expected):
                    self.call(
                        config,
                        task,
                        apply=False,
                        pid_alive=pid_alive,
                        pgid_alive=pgid_alive,
                    )
                self.assertTrue(config.staging.is_dir())

    def test_rejects_held_lock_unsafe_path_and_existing_intent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="weekly-reconcile-lock-") as raw:
            config, task = self.fixture(Path(raw))
            lock_path = config.weekly_root.parent / ".weekly-backup.lock"
            with lock_path.open("rb") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(
                    reconcile.ReconciliationError,
                    "weekly backup lock is held",
                ):
                    self.call(config, task, apply=False)
            self.assertTrue(config.staging.is_dir())

        with tempfile.TemporaryDirectory(prefix="weekly-reconcile-path-") as raw:
            config, task = self.fixture(Path(raw))
            unsafe = replace(
                config,
                quarantine_root=Path(raw) / "not-a-sibling/quarantine",
            )
            with self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "quarantine root must be a distinct weekly-root sibling",
            ):
                self.call(unsafe, task, apply=False)

        with tempfile.TemporaryDirectory(prefix="weekly-reconcile-intent-") as raw:
            config, task = self.fixture(Path(raw))
            config.receipt_dir.mkdir(mode=0o700)
            intent = config.receipt_dir / (
                "weekly-backup-reconciliation-{}-intent.json".format(
                    RECONCILIATION_ID
                )
            )
            intent.write_text("preserve existing receipt\n", encoding="utf-8")
            with self.assertRaisesRegex(
                reconcile.ReconciliationError,
                "reconciliation receipt path already exists",
            ):
                self.call(config, task, apply=True)
            self.assertTrue(config.staging.is_dir())
            self.assertEqual(
                intent.read_text(encoding="utf-8"),
                "preserve existing receipt\n",
            )


if __name__ == "__main__":
    unittest.main()
