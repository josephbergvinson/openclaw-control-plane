from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from scripts import openclaw_archive_v3_retention as retention
from scripts import openclaw_backup_retention_cleanup as cleanup
from scripts import openclaw_independent_backup_receipt as independent_backup_proof
from scripts import openclaw_weekly_archive_backup as archive_backup
from openclaw_archive_v3_test_fixture import create_v3_fixture
from scripts.openclaw_weekly_backup_integration_proof import (
    write_real_sqlite_fixture,
    write_sqlite_fixture_cli,
)


class OpenClawArchiveV3RetentionTests(unittest.TestCase):
    def fixture(
        self,
        root: Path,
    ) -> tuple[
        archive_backup.BackupConfig,
        archive_backup.VolumeIdentity,
        retention.RetentionConfig,
    ]:
        mount = root / "mount"
        owc = mount / "OpenClaw"
        workspace = owc / "Workspace"
        bare = mount / "ProjectInfrastructure/GitRemotes"
        agents = owc / ".state/OpenClaw/agents"
        sessions = owc / ".state/OpenClaw/Sessions"
        browser = owc / ".state/OpenClaw/Browser"
        media = owc / ".state/OpenClaw/Media"
        state = root / "home/.openclaw"
        for path in (
            workspace / "runbook",
            workspace / "memory",
            bare,
            agents / "main/agent",
            sessions,
            browser,
            media,
            state / "state",
            state / "cron",
            state / "credentials",
        ):
            path.mkdir(parents=True, exist_ok=True)
        for name in archive_backup.POLICY_FILES:
            (workspace / name).write_text(
                "{} fixture\n".format(name),
                encoding="utf-8",
            )
        (workspace / "runbook/restore.md").write_text(
            "restore fixture\n",
            encoding="utf-8",
        )
        (workspace / "memory/fact.md").write_text(
            "memory fixture\n",
            encoding="utf-8",
        )
        (state / "openclaw.json").write_text(
            '{"fixture":true}\n',
            encoding="utf-8",
        )
        (state / "cron/jobs.json").write_text(
            '{"jobs":[]}\n',
            encoding="utf-8",
        )
        (state / "credentials/token.txt").write_text(
            "fixture-sensitive\n",
            encoding="utf-8",
        )
        for database in (
            state / "state/openclaw.sqlite",
            agents / "main/agent/openclaw-agent.sqlite",
        ):
            write_real_sqlite_fixture(database, database.name)
            for suffix in ("-wal", "-shm", "-journal"):
                Path("{}{}".format(database, suffix)).write_bytes(
                    b""
                )
        (sessions / "session.jsonl").write_text(
            '{"session":"fixture"}\n',
            encoding="utf-8",
        )
        (browser / "Preferences").write_text(
            '{"browser":"fixture"}\n',
            encoding="utf-8",
        )
        (media / "item.txt").write_text(
            "media fixture\n",
            encoding="utf-8",
        )
        (agents / "main/sessions").symlink_to(
            sessions,
            target_is_directory=True,
        )
        (state / "agents").symlink_to(agents, target_is_directory=True)
        (state / "browser").symlink_to(browser, target_is_directory=True)
        (state / "media").symlink_to(media, target_is_directory=True)
        subprocess.run(
            [
                "/usr/bin/git",
                "init",
                "--bare",
                str(bare / "OpenClaw/openclaw-workspace.git"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        workspace_logical = root / "home/OpenClawWorkspace"
        workspace_logical.symlink_to(workspace, target_is_directory=True)
        git_logical = root / "home/git-remotes"
        git_logical.symlink_to(bare, target_is_directory=True)
        fake_cli = root / "bin/openclaw"
        write_sqlite_fixture_cli(fake_cli)
        device = owc.stat().st_dev
        producer = archive_backup.BackupConfig(
            owc_root=owc,
            owc_volume_mount=mount,
            expected_volume_uuid="FIXTURE-UUID",
            expected_device=device,
            state_root=state,
            workspace_logical=workspace_logical,
            git_remotes_logical=git_logical,
            backup_root=owc / "Backups/weekly",
            receipt_dir=workspace / "artifacts/archive-backup-receipts",
            min_post_backup_free_bytes=0,
            openclaw_cli=fake_cli,
        )
        identity = archive_backup.VolumeIdentity(
            uuid="FIXTURE-UUID",
            device=device,
            mount=mount,
            writable=True,
            owners_enabled=True,
        )
        independent_receipt = workspace / "artifacts/independent-backup-receipt.json"
        retention_config = retention.RetentionConfig(
            producer_config=producer,
            receipt_dir=workspace / "artifacts/archive-retention-receipts",
            independent_backup_receipt=independent_receipt,
        )
        return producer, identity, retention_config

    @staticmethod
    def identity_reader(identity):
        def read(_config):
            return identity

        return read

    def refresh_independent_receipt(
        self,
        producer: archive_backup.BackupConfig,
    ) -> Path:
        fixture_root = producer.owc_root.parents[1]
        backup_root = fixture_root / "independent-device/weekly"
        restore_root = fixture_root / "isolated-restore/weekly"
        for destination in (backup_root, restore_root):
            if destination.exists():
                shutil.rmtree(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(producer.backup_root, destination)
        source_root = producer.backup_root
        source_device = source_root.stat().st_dev
        identity_rows = {
            str(source_root.resolve()): independent_backup_proof.VolumeIdentity(
                volume_uuid=producer.expected_volume_uuid,
                device=source_device,
                mount=str(producer.owc_volume_mount),
            ),
            str(backup_root.resolve()): independent_backup_proof.VolumeIdentity(
                volume_uuid="FIXTURE-BACKUP-UUID",
                device=source_device,
                mount=str(fixture_root),
            ),
            str(restore_root.resolve()): independent_backup_proof.VolumeIdentity(
                volume_uuid="FIXTURE-RESTORE-UUID",
                device=source_device,
                mount=str(fixture_root),
            ),
        }

        def identity_reader(path: Path):
            return identity_rows[str(Path(path).resolve())]

        def verified_fixture_inventory(path: Path, *, identity_reader):
            inventory = independent_backup_proof.build_tree_inventory(
                path,
                identity_reader=identity_reader,
            )
            absolute = str(Path(path).resolve())
            if absolute == str(backup_root.resolve()):
                return replace(
                    inventory,
                    volume_uuid="FIXTURE-BACKUP-UUID",
                    device=source_device + 100,
                )
            if absolute == str(restore_root.resolve()):
                return replace(
                    inventory,
                    volume_uuid="FIXTURE-RESTORE-UUID",
                    device=source_device + 200,
                )
            return inventory

        receipt_path = (
            producer.workspace_logical.resolve()
            / "artifacts/independent-backup-receipt.json"
        )
        independent_backup_proof.produce_receipt(
            source_weekly_root=source_root,
            backup_weekly_root=backup_root,
            isolated_restore_weekly_root=restore_root,
            output=receipt_path,
            identity_reader=identity_reader,
            inventory_builder=verified_fixture_inventory,
        )
        return receipt_path

    def create_generations(
        self,
        producer: archive_backup.BackupConfig,
        identity: archive_backup.VolumeIdentity,
        count: int,
    ) -> list[Path]:
        generations: list[Path] = []
        for index in range(count):
            code, result = create_v3_fixture(
                producer,
                identity_reader=self.identity_reader(identity),
                now=datetime(
                    2026,
                    7,
                    25,
                    10 + index,
                    0,
                    tzinfo=timezone.utc,
                ),
            )
            self.assertEqual(code, 0, result)
            generations.append(Path(result["destination"]))
        if generations:
            self.refresh_independent_receipt(producer)
        return generations

    def create_clone_pin(
        self,
        config: retention.RetentionConfig,
        *,
        name: str = "openclaw-backup-20260725T090000Z",
    ) -> retention.CloneV2Pin:
        generation = config.backup_root / name
        generation.mkdir(mode=0o700, parents=True)
        manifest = generation / "MANIFEST.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "openclaw.owc_weekly_backup.manifest.v2",
                    "backup_name": name,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        pin = retention.CloneV2Pin(
            name=name,
            manifest_sha256=archive_backup.sha256_file(manifest),
        )
        self.refresh_independent_receipt(config.producer_config)
        return pin

    def test_cli_wait_transition_is_visible_then_unchanged_wait_is_quiet(
        self,
    ) -> None:
        waiting = {
            "result": "waiting_external_backup",
            "retry_suppressed": False,
            "delete_intent_count": 0,
            "protected_v3": ["newest", "second"],
            "candidates": ["oldest"],
            "pinned_clone_v2": [{"name": "frozen"}],
            "removed": [],
            "quarantined": [],
            "incident_fingerprint": "a" * 64,
            "receipt": "/tmp/wait.json",
        }
        first = io.StringIO()
        with mock.patch.object(
            retention,
            "run_retention",
            return_value=(0, waiting),
        ), redirect_stdout(first):
            self.assertEqual(retention.main(["--apply"]), 0)
        self.assertEqual(
            first.getvalue(),
            "Older backups were kept because an independent backup has not yet "
            "been verified. Nothing was removed.\n",
        )

        repeated = io.StringIO()
        with mock.patch.object(
            retention,
            "run_retention",
            return_value=(0, {**waiting, "retry_suppressed": True}),
        ), redirect_stdout(repeated):
            self.assertEqual(retention.main(["--apply"]), 0)
        self.assertEqual(repeated.getvalue(), "NO_REPLY\n")

    def test_two_copy_floor_preserves_both_on_apply(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v3-retention-floor-") as raw:
            producer, identity, config = self.fixture(Path(raw))
            generations = self.create_generations(producer, identity, 2)
            code, report = retention.run_retention(
                apply=True,
                config=config,
            )
            self.assertEqual(code, 0, report)
            self.assertEqual(report["removed"], [])
            self.assertEqual(report["verified_remaining"], 2)
            self.assertTrue(report["two_copy_floor_satisfied"])
            self.assertEqual(
                set(report["protected_v3"]),
                {path.name for path in generations},
            )
            self.assertTrue(all(path.is_dir() for path in generations))
            receipt = Path(report["receipt"])
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(receipt.parent.stat().st_mode),
                0o700,
            )

    def test_fewer_than_two_verified_v3_blocks_nonzero(self) -> None:
        for count in (0, 1):
            with self.subTest(count=count):
                with tempfile.TemporaryDirectory(
                    prefix="v3-retention-floor-block-"
                ) as raw:
                    producer, identity, config = self.fixture(Path(raw))
                    archive_backup.validate_environment(
                        producer,
                        self.identity_reader(identity),
                        create_backup_root=True,
                    )
                    generations = self.create_generations(
                        producer,
                        identity,
                        count,
                    )
                    code, report = retention.run_retention(
                        apply=True,
                        config=config,
                    )
                    self.assertEqual(code, 1, report)
                    self.assertEqual(report["result"], "blocked")
                    self.assertEqual(report["removed"], [])
                    self.assertIn(
                        "two-copy floor is not satisfied",
                        report["blockers"][0],
                    )
                    self.assertTrue(
                        all(path.is_dir() for path in generations)
                    )

    def test_third_generation_deep_verifies_all_and_deletes_oldest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v3-retention-delete-") as raw:
            producer, identity, base_config = self.fixture(Path(raw))
            generations = self.create_generations(producer, identity, 3)
            pin = self.create_clone_pin(base_config)
            config = retention.RetentionConfig(
                producer_config=producer,
                receipt_dir=base_config.receipt_dir,
                clone_v2_pins=(pin,),
                independent_backup_receipt=base_config.independent_backup_receipt,
            )
            dry_code, dry_report = retention.run_retention(
                apply=False,
                config=config,
            )
            self.assertEqual(dry_code, 0, dry_report)
            self.assertEqual(dry_report["removed"], [])
            self.assertEqual(
                dry_report["candidates"],
                [generations[0].name],
            )
            self.assertTrue(all(path.is_dir() for path in generations))
            dry_receipt = json.loads(
                Path(dry_report["receipt"]).read_text(encoding="utf-8")
            )
            self.assertFalse(
                any(
                    row["name"]
                    == "all-v3-full-decompression-and-restore"
                    for row in dry_receipt["phases"]
                )
            )
            code, report = retention.run_retention(
                apply=True,
                config=config,
            )
            self.assertEqual(code, 0, report)
            self.assertEqual(report["removed"], [generations[0].name])
            self.assertFalse(generations[0].exists())
            self.assertTrue(generations[1].is_dir())
            self.assertTrue(generations[2].is_dir())
            self.assertEqual(report["verified_remaining"], 2)
            self.assertEqual(
                report["pinned_clone_v2"][0]["name"],
                pin.name,
            )
            self.assertTrue((config.backup_root / pin.name).is_dir())
            receipt = json.loads(
                Path(report["receipt"]).read_text(encoding="utf-8")
            )
            deep_phase = next(
                row
                for row in receipt["phases"]
                if row["name"]
                == "all-v3-full-decompression-and-restore"
            )
            probe_phase = next(
                row
                for row in receipt["phases"]
                if row["name"]
                == "current-probe-created-before-extraction"
            )
            self.assertLess(
                receipt["phases"].index(probe_phase),
                receipt["phases"].index(deep_phase),
            )
            self.assertRegex(
                Path(probe_phase["path"]).name,
                retention.PROBE_NAME_RE,
            )
            self.assertEqual(
                probe_phase["device"],
                producer.expected_device,
            )
            self.assertGreater(probe_phase["inode"], 0)
            self.assertEqual(
                Path(probe_phase["owner_marker"]).name,
                retention.PROBE_OWNER_FILE,
            )
            self.assertRegex(
                probe_phase["owner_marker_sha256"],
                retention.SHA256_RE,
            )
            self.assertEqual(
                probe_phase["owner_invocation_id"],
                receipt["invocation_id"],
            )
            self.assertEqual(len(deep_phase["generations"]), 3)
            self.assertTrue(
                all(
                    row["result"] == "verified"
                    for row in deep_phase["generations"]
                )
            )
            for generation in deep_phase["generations"]:
                git = next(
                    row
                    for row in generation["archives"]
                    if row["name"] == "git-remotes.tgz"
                )
                self.assertEqual(
                    git["full_isolated_restore"]["git_fsck"]["result"],
                    "verified",
                )

    def test_corruption_blocks_without_deleting_any_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v3-retention-corrupt-") as raw:
            producer, identity, config = self.fixture(Path(raw))
            generations = self.create_generations(producer, identity, 3)
            with (generations[0] / "openclaw-state.tgz").open("ab") as handle:
                handle.write(b"corruption")
            code, report = retention.run_retention(
                apply=True,
                config=config,
            )
            self.assertEqual(code, 1)
            self.assertEqual(report["removed"], [])
            self.assertIn(
                "published archive readback mismatch",
                report["blockers"][0],
            )
            self.assertTrue(all(path.is_dir() for path in generations))

    def test_unknown_incomplete_and_symlink_entries_fail_closed(self) -> None:
        cases = ("unknown", "incomplete", "symlink")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(
                    prefix="v3-retention-entry-"
                ) as raw:
                    producer, _identity, config = self.fixture(Path(raw))
                    archive_backup.validate_environment(
                        producer,
                        self.identity_reader(
                            archive_backup.VolumeIdentity(
                                uuid="FIXTURE-UUID",
                                device=producer.expected_device,
                                mount=producer.owc_volume_mount,
                                writable=True,
                                owners_enabled=True,
                            )
                        ),
                        create_backup_root=True,
                    )
                    if case == "unknown":
                        (config.backup_root / "unexpected.txt").write_text(
                            "unknown\n",
                            encoding="utf-8",
                        )
                    elif case == "incomplete":
                        (
                            config.backup_root
                            / ".openclaw-archive-v3-"
                            "20260725T100000Z.incomplete-123"
                        ).mkdir()
                    else:
                        target = Path(raw) / "outside"
                        target.mkdir()
                        (
                            config.backup_root
                            / "openclaw-archive-v3-20260725T100000Z"
                        ).symlink_to(target, target_is_directory=True)
                    code, report = retention.run_retention(
                        apply=True,
                        config=config,
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(report["removed"], [])
                    self.assertTrue(report["blockers"])

    def test_foreign_device_predelete_gate_retains_every_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v3-retention-device-") as raw:
            producer, identity, config = self.fixture(Path(raw))
            generations = self.create_generations(producer, identity, 3)
            original = retention.validate_predelete_generation_tree

            def reject_oldest(path, *, config):
                if path == generations[0]:
                    raise retention.RetentionError(
                        "archive-v3 generation member crossed expected device"
                    )
                return original(path, config=config)

            with mock.patch.object(
                retention,
                "validate_predelete_generation_tree",
                side_effect=reject_oldest,
            ):
                code, report = retention.run_retention(
                    apply=True,
                    config=config,
                )
            self.assertEqual(code, 1)
            self.assertEqual(report["removed"], [])
            self.assertIn(
                "crossed expected device",
                report["blockers"][0],
            )
            self.assertTrue(all(path.is_dir() for path in generations))

    def test_wrapper_ready_proof_reaches_real_engine_once_and_only_oldest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="v3-retention-wrapper-ready-proof-"
        ) as raw:
            producer, identity, base_config = self.fixture(Path(raw))
            generations = self.create_generations(producer, identity, 3)
            pin = self.create_clone_pin(base_config)
            config = replace(base_config, clone_v2_pins=(pin,))
            invocations: list[list[str]] = []
            reports: list[dict[str, object]] = []

            def real_fixture_engine(argv=None):
                delegated = list(argv or [])
                invocations.append(delegated)
                parsed = retention.build_parser().parse_args(delegated)
                self.assertEqual(len(parsed.pinned_clone_v2), 1)
                fixture_config = replace(
                    config,
                    clone_v2_pins=(pin,),
                    independent_backup_receipt=parsed.independent_backup_receipt,
                )
                code, report = retention.run_retention(
                    apply=parsed.apply,
                    config=fixture_config,
                )
                reports.append(report)
                return code, report

            with mock.patch.object(
                cleanup, "FROZEN_CLONE_V2_PINS", ((pin.name, pin.manifest_sha256),),
            ), redirect_stdout(io.StringIO()):
                code = cleanup.main(
                    [
                        "--apply",
                        "--independent-backup-receipt",
                        str(config.independent_backup_receipt),
                    ],
                    engine_runner=real_fixture_engine,
                )

            self.assertEqual(code, 0, reports)
            self.assertEqual(len(invocations), 1)
            self.assertEqual(invocations[0].count("--apply"), 1)
            self.assertEqual(
                invocations[0].count("--independent-backup-receipt"),
                1,
            )
            self.assertEqual(reports[0]["result"], "verified")
            self.assertEqual(reports[0]["delete_intent_count"], 1)
            self.assertEqual(reports[0]["removed"], [generations[0].name])
            self.assertFalse(generations[0].exists())
            self.assertTrue(generations[1].is_dir())
            self.assertTrue(generations[2].is_dir())
            self.assertTrue((config.backup_root / pin.name).is_dir())

    def test_expected_device_mismatch_blocks_before_inventory(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="v3-retention-root-device-"
        ) as raw:
            producer, identity, base = self.fixture(Path(raw))
            generations = self.create_generations(producer, identity, 2)
            wrong_producer = replace(
                producer,
                expected_device=producer.expected_device + 1,
            )
            config = retention.RetentionConfig(
                producer_config=wrong_producer,
                receipt_dir=base.receipt_dir,
            )
            code, report = retention.run_retention(
                apply=True,
                config=config,
            )
            self.assertEqual(code, 1)
            self.assertEqual(report["removed"], [])
            self.assertIn(
                "physical directory device mismatch",
                report["blockers"][0],
            )
            self.assertTrue(all(path.is_dir() for path in generations))

    def test_runtime_device_is_resolved_from_canonical_uuid_and_mount(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v3-retention-runtime-device-") as raw:
            producer, identity, base = self.fixture(Path(raw))
            unresolved = retention.RetentionConfig(
                producer_config=replace(producer, expected_device=None),
                receipt_dir=base.receipt_dir,
                independent_backup_receipt=base.independent_backup_receipt,
            )

            resolved = retention.resolve_runtime_device_identity(
                unresolved,
                identity_reader=self.identity_reader(identity),
            )

            self.assertEqual(resolved.expected_device, identity.device)
            self.assertEqual(
                resolved.producer_config.expected_volume_uuid,
                identity.uuid,
            )
            self.assertEqual(resolved.producer_config.owc_volume_mount, identity.mount)

    def test_apply_requires_verified_independent_backup_before_deletion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v3-retention-independent-gate-") as raw:
            producer, identity, base = self.fixture(Path(raw))
            generations = self.create_generations(producer, identity, 3)
            config = replace(base, independent_backup_receipt=None)

            code, report = retention.run_retention(apply=True, config=config)

            self.assertEqual(code, 0)
            self.assertEqual(report["result"], "waiting_external_backup")
            self.assertEqual(report["removed"], [])
            self.assertEqual(report["quarantined"], [])
            self.assertEqual(report["delete_intent_count"], 0)
            self.assertFalse(report["deletion_authorized"])
            self.assertIn("independent backup receipt is required", report["blockers"][0])
            self.assertIsNotNone(report["incident_fingerprint"])
            self.assertFalse(report["retry_suppressed"])
            self.assertTrue(report["transition_emitted"])
            self.assertEqual(len(report["protected_v3"]), 2)
            self.assertEqual(report["candidates"], [generations[0].name])
            self.assertTrue(all(path.is_dir() for path in generations))
            incident_path = retention.incident_state_path(config)
            original_incident = incident_path.read_bytes()
            incident = json.loads(original_incident)
            self.assertEqual(incident["status"], "waiting_external_backup")

            repeated_code, repeated_report = retention.run_retention(
                apply=True,
                config=config,
            )
            self.assertEqual(repeated_code, 0)
            self.assertEqual(
                repeated_report["result"], "waiting_external_backup"
            )
            self.assertEqual(repeated_report["removed"], [])
            self.assertEqual(repeated_report["blockers"], report["blockers"])
            self.assertEqual(
                repeated_report["incident_fingerprint"],
                report["incident_fingerprint"],
            )
            self.assertTrue(repeated_report["retry_suppressed"])
            self.assertFalse(repeated_report["transition_emitted"])
            self.assertEqual(
                repeated_report["suppression_reason"],
                "unchanged waiting_external_backup fingerprint; transition already reported",
            )
            self.assertEqual(incident_path.read_bytes(), original_incident)

            changed_config = replace(
                config,
                independent_backup_receipt=base.independent_backup_receipt,
            )
            final_code, final_report = retention.run_retention(
                apply=True,
                config=changed_config,
            )
            self.assertEqual(final_code, 0, final_report)
            self.assertEqual(final_report["removed"], [generations[0].name])

    def test_independent_backup_receipt_requires_canonical_volume_roles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v3-retention-backup-role-") as raw:
            producer, identity, config = self.fixture(Path(raw))
            generations = self.create_generations(producer, identity, 3)
            receipt_path = config.independent_backup_receipt
            self.assertIsNotNone(receipt_path)
            assert receipt_path is not None
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            payload["backup_role"] = "openclaw_primary"
            receipt_path.write_text(json.dumps(payload), encoding="utf-8")
            receipt_path.chmod(0o600)

            code, report = retention.run_retention(apply=True, config=config)

            self.assertEqual(code, 0)
            self.assertEqual(report["result"], "waiting_external_backup")
            self.assertEqual(report["removed"], [])
            self.assertEqual(report["quarantined"], [])
            self.assertEqual(report["delete_intent_count"], 0)
            self.assertIn("backup role mismatch", report["blockers"][0])
            self.assertTrue(all(path.is_dir() for path in generations))

    def test_pin_mismatch_unknown_clone_and_missing_pin_block(self) -> None:
        cases = ("mismatch", "unknown", "missing")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(
                    prefix="v3-retention-pin-"
                ) as raw:
                    producer, identity, base = self.fixture(Path(raw))
                    archive_backup.validate_environment(
                        producer,
                        self.identity_reader(identity),
                        create_backup_root=True,
                    )
                    pin = self.create_clone_pin(base)
                    if case == "mismatch":
                        pins = (
                            retention.CloneV2Pin(
                                pin.name,
                                "0" * 64,
                            ),
                        )
                    elif case == "unknown":
                        pins = ()
                    else:
                        shutil.rmtree(base.backup_root / pin.name)
                        pins = (pin,)
                    config = retention.RetentionConfig(
                        producer_config=producer,
                        receipt_dir=base.receipt_dir,
                        clone_v2_pins=pins,
                    )
                    code, report = retention.run_retention(
                        apply=False,
                        config=config,
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(report["removed"], [])
                    self.assertTrue(report["blockers"])

    def test_deep_failure_on_any_v3_happens_before_first_delete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v3-retention-deep-") as raw:
            producer, identity, config = self.fixture(Path(raw))
            generations = self.create_generations(producer, identity, 3)
            with mock.patch.object(
                retention,
                "deep_verify_all",
                side_effect=retention.RetentionError(
                    "injected newest deep verification failure"
                ),
            ):
                code, report = retention.run_retention(
                    apply=True,
                    config=config,
                )
            self.assertEqual(code, 1)
            self.assertEqual(report["removed"], [])
            self.assertTrue(all(path.is_dir() for path in generations))

    def test_candidate_swap_is_quarantined_without_following_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v3-retention-swap-") as raw:
            root = Path(raw)
            producer, identity, config = self.fixture(root)
            generations = self.create_generations(producer, identity, 3)
            candidate = generations[0]
            moved = root / "retained-original-generation"
            outside = root / "outside-delete-target"
            outside.mkdir()
            sentinel = outside / "must-survive.txt"
            sentinel.write_text("preserve\n", encoding="utf-8")
            original_rename = retention.os.rename
            swapped = False

            def swap_then_rename(source, destination):
                nonlocal swapped
                if Path(source) == candidate and not swapped:
                    original_rename(source, moved)
                    candidate.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return original_rename(source, destination)

            with mock.patch.object(
                retention.os,
                "rename",
                side_effect=swap_then_rename,
            ):
                code, report = retention.run_retention(
                    apply=True,
                    config=config,
                )
            self.assertTrue(swapped)
            self.assertEqual(code, 1)
            self.assertEqual(report["removed"], [])
            self.assertTrue(moved.is_dir())
            self.assertTrue(sentinel.is_file())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
            self.assertEqual(len(report["quarantined"]), 1)
            self.assertTrue(Path(report["quarantined"][0]).is_symlink())

    def test_probe_cleanup_rejects_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="v3-retention-probe-symlink-"
        ) as raw:
            root = Path(raw)
            producer, _identity, config = self.fixture(root)
            producer.backup_root.parent.mkdir(parents=True, exist_ok=True)
            outside = root / "outside-probe-target"
            outside.mkdir()
            sentinel = outside / "must-survive.txt"
            sentinel.write_text("preserve\n", encoding="utf-8")
            probe = producer.backup_root.parent / (
                ".archive-v3-retention-probe-test"
            )
            probe.symlink_to(outside, target_is_directory=True)
            info = probe.lstat()
            with self.assertRaisesRegex(
                retention.RetentionError,
                "unsafe archive-v3 retention probe cleanup path or identity",
            ):
                retention.cleanup_probe_path(
                    probe,
                    config=config,
                    expected_identity=(info.st_dev, info.st_ino),
                )
            self.assertTrue(probe.is_symlink())
            self.assertTrue(sentinel.is_file())

    def test_stale_probe_is_receipted_reconciled_and_does_not_accumulate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="v3-retention-stale-probe-"
        ) as raw:
            producer, identity, config = self.fixture(Path(raw))
            self.create_generations(producer, identity, 2)
            stale = config.backup_root.parent / (
                ".archive-v3-retention-probe-999-"
                "0123456789abcdef0123456789abcdef"
            )
            stale.mkdir()
            stale_info = stale.lstat()
            retention.write_probe_owner(
                stale,
                info=stale_info,
                invocation_id="interrupted-fixture-invocation",
            )
            (stale / "nested").mkdir()
            (stale / "nested/orphan.txt").write_text(
                "interrupted restore\n",
                encoding="utf-8",
            )
            owner_digest = archive_backup.sha256_file(
                stale / retention.PROBE_OWNER_FILE
            )

            code, report = retention.run_retention(
                apply=True,
                config=config,
            )

            self.assertEqual(code, 0, report)
            self.assertFalse(stale.exists())
            receipt = json.loads(
                Path(report["receipt"]).read_text(encoding="utf-8")
            )
            intent = next(
                row
                for row in receipt["phases"]
                if row["name"] == "stale-probe-reconciliation-intent"
            )
            self.assertEqual(
                intent["probes"],
                [
                    {
                        "path": str(stale),
                        "name": stale.name,
                        "device": stale_info.st_dev,
                        "inode": stale_info.st_ino,
                        "owner_marker_sha256": owner_digest,
                        "owner_invocation_id": (
                            "interrupted-fixture-invocation"
                        ),
                        "classification": "owned-stale-restore-probe",
                    }
                ],
            )
            complete = next(
                row
                for row in receipt["phases"]
                if row["name"] == "stale-probe-reconciliation-complete"
            )
            self.assertEqual(complete["removed"], [str(stale)])

            second_code, second_report = retention.run_retention(
                apply=True,
                config=config,
            )
            self.assertEqual(second_code, 0, second_report)
            self.assertEqual(
                [
                    child.name
                    for child in config.backup_root.parent.iterdir()
                    if child.name.startswith(retention.PROBE_PREFIX)
                ],
                [],
            )

    def test_malformed_or_symlinked_stale_probe_fails_closed(self) -> None:
        for case in ("malformed", "unowned", "symlink"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(
                    prefix="v3-retention-stale-probe-block-"
                ) as raw:
                    root = Path(raw)
                    producer, identity, config = self.fixture(root)
                    generations = self.create_generations(
                        producer,
                        identity,
                        2,
                    )
                    outside = root / "outside-stale-probe"
                    outside.mkdir()
                    sentinel = outside / "must-survive.txt"
                    sentinel.write_text("preserve\n", encoding="utf-8")
                    if case == "malformed":
                        probe = config.backup_root.parent / (
                            ".archive-v3-retention-probe-malformed"
                        )
                        probe.mkdir()
                    elif case == "unowned":
                        probe = config.backup_root.parent / (
                            ".archive-v3-retention-probe-999-"
                            "0123456789abcdef0123456789abcdef"
                        )
                        probe.mkdir()
                    else:
                        probe = config.backup_root.parent / (
                            ".archive-v3-retention-probe-999-"
                            "0123456789abcdef0123456789abcdef"
                        )
                        probe.symlink_to(
                            outside,
                            target_is_directory=True,
                        )

                    code, report = retention.run_retention(
                        apply=True,
                        config=config,
                    )

                    self.assertEqual(code, 1, report)
                    self.assertEqual(report["removed"], [])
                    self.assertTrue(probe.exists() or probe.is_symlink())
                    self.assertTrue(sentinel.is_file())
                    self.assertTrue(
                        all(path.is_dir() for path in generations)
                    )

    def test_quarantine_cleanup_uses_parent_anchored_removal_only(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="v3-retention-anchored-delete-"
        ) as raw:
            producer, identity, config = self.fixture(Path(raw))
            generations = self.create_generations(producer, identity, 3)
            original_rmdir = retention.os.rmdir
            rmdir_calls: list[tuple[object, object]] = []

            def require_anchored_rmdir(path, *args, **kwargs):
                rmdir_calls.append((path, kwargs.get("dir_fd")))
                self.assertIsNotNone(kwargs.get("dir_fd"))
                return original_rmdir(path, *args, **kwargs)

            with mock.patch.object(
                retention.shutil,
                "rmtree",
                side_effect=AssertionError(
                    "path-based rmtree must not be used"
                ),
            ), mock.patch.object(
                retention.os,
                "rmdir",
                side_effect=require_anchored_rmdir,
            ):
                code, report = retention.run_retention(
                    apply=True,
                    config=config,
                )

            self.assertEqual(code, 0, report)
            self.assertEqual(report["removed"], [generations[0].name])
            self.assertTrue(rmdir_calls)
            self.assertTrue(
                all(descriptor is not None for _path, descriptor in rmdir_calls)
            )

    def test_shared_lock_blocks_without_touching_generations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v3-retention-lock-") as raw:
            producer, identity, config = self.fixture(Path(raw))
            generations = self.create_generations(producer, identity, 2)
            descriptor = os.open(
                config.lock_path,
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                code, report = retention.run_retention(
                    apply=True,
                    config=config,
                )
            finally:
                os.close(descriptor)
            self.assertEqual(code, 1)
            self.assertEqual(report["removed"], [])
            self.assertIn(
                "weekly backup lock is already held",
                report["blockers"][0],
            )
            self.assertTrue(all(path.is_dir() for path in generations))
            self.assertTrue(Path(report["receipt"]).is_file())


if __name__ == "__main__":
    unittest.main()
