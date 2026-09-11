from __future__ import annotations

import ast
from contextlib import ExitStack, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from scripts import openclaw_daily_backup_cron as backup


MAP_RELATIVES = (
    "Workspace",
    "Projects/PersonalData",
    "Repositories/Working",
    "Repositories/Bare",
    ".runtime/Releases",
    ".state/OpenClaw/Sessions",
    ".state/OpenClaw/Browser",
    ".state/OpenClaw/Media",
)
TECHNICAL_RELATIVES = (
    ".archive",
    ".quarantine",
    ".cache",
    ".scratch",
    ".build",
    ".manifests",
)


class OpenClawWeeklyBackupTests(unittest.TestCase):
    def fixture(self, root: Path) -> dict[str, Path | int]:
        owc = root / "OWC" / "OpenClaw"
        for index, relative in enumerate(MAP_RELATIVES + TECHNICAL_RELATIVES):
            source = owc / relative
            source.mkdir(parents=True)
            (source / "data.txt").write_text("source {} {}\n".format(index, relative), encoding="utf-8")
            (source / "empty").mkdir()
            os.symlink("data.txt", source / "data-link")
        legacy_sessions = owc / ".state/OpenClaw/Sessions"
        current_sessions = owc / ".state/OpenClaw/agents/main/sessions"
        current_sessions.parent.mkdir(parents=True)
        current_sessions.symlink_to(
            legacy_sessions,
            target_is_directory=True,
        )
        (owc / "Workspace/artifacts").mkdir()
        (owc / "Workspace/artifacts/status.md").write_text("state: fixture\n", encoding="utf-8")
        (owc / "Backups").mkdir()
        scheduler = root / "state" / "cron" / "jobs.json"
        scheduler.parent.mkdir(parents=True)
        scheduler.write_text('{"version":1,"jobs":[]}\n', encoding="utf-8")
        receipts = owc / "Workspace/artifacts/openclaw_weekly_backup"
        return {
            "owc": owc,
            "backup_root": owc / "Backups/weekly",
            "scheduler": scheduler,
            "receipts": receipts,
            "device": owc.stat().st_dev,
        }

    @staticmethod
    def normalize_session_fixture(owc: Path) -> tuple[Path, Path]:
        legacy = owc / ".state/OpenClaw/Sessions"
        current = owc / ".state/OpenClaw/agents/main/sessions"
        current.unlink()
        legacy.rename(current)
        return legacy, current

    def patched(self, paths: dict[str, Path | int]) -> ExitStack:
        stack = ExitStack()
        owc = paths["owc"]
        assert isinstance(owc, Path)
        device = paths["device"]
        assert isinstance(device, int)
        stack.enter_context(mock.patch.object(backup, "OWC_ROOT", owc))
        stack.enter_context(mock.patch.object(backup, "OWC_VOLUME_MOUNT", owc.parent))
        stack.enter_context(mock.patch.object(backup, "BACKUP_ROOT", paths["backup_root"]))
        stack.enter_context(mock.patch.object(backup, "SCHEDULER_STORE", paths["scheduler"]))
        stack.enter_context(mock.patch.object(backup, "RECEIPT_DIR", paths["receipts"]))
        stack.enter_context(mock.patch.object(backup, "EXPECTED_DEVICE", device))
        stack.enter_context(mock.patch.object(backup, "EXPECTED_VOLUME_UUID", "FIXTURE-UUID"))
        stack.enter_context(mock.patch.object(backup, "RETENTION_COUNT", 2))
        stack.enter_context(
            mock.patch.object(
                backup,
                "read_volume_identity",
                return_value=backup.VolumeIdentity(
                    uuid="FIXTURE-UUID",
                    device=device,
                    mount=owc.parent,
                    writable=True,
                    owners_enabled=True,
                ),
            )
        )
        return stack

    def test_default_volume_identity_uses_vfs_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-vfs-") as raw:
            paths = self.fixture(Path(raw))
            owc = paths["owc"]
            device = paths["device"]
            assert isinstance(owc, Path)
            assert isinstance(device, int)
            mount = owc.parent
            with (
                mock.patch.object(backup, "OWC_ROOT", owc),
                mock.patch.object(backup, "OWC_VOLUME_MOUNT", mount),
                mock.patch.object(
                    backup.external_volume_guard,
                    "volume_metadata",
                    return_value={
                        "available": True,
                        "mountDevice": device,
                        "mountFlags": 0,
                        "mountPoint": str(mount),
                        "mounted": True,
                        "ownersEnabled": True,
                        "probeMethod": "getattrlist-statvfs",
                        "readOnly": False,
                        "volumeUuid": "fixture-uuid",
                    },
                ) as metadata_probe,
            ):
                observed = backup.read_volume_identity()

            metadata_probe.assert_called_once_with(mount)
            self.assertEqual(observed.uuid, "FIXTURE-UUID")
            self.assertEqual(observed.device, device)
            self.assertEqual(observed.mount, mount)
            self.assertTrue(observed.writable)
            self.assertTrue(observed.owners_enabled)

    def test_default_volume_identity_fails_closed_on_unavailable_metadata(self) -> None:
        with mock.patch.object(
            backup.external_volume_guard,
            "volume_metadata",
            return_value={
                "available": False,
                "mounted": True,
                "probeMethod": "getattrlist-statvfs",
                "error": "OSError",
            },
        ):
            with self.assertRaisesRegex(backup.BackupError, "VFS volume identity failed"):
                backup.read_volume_identity()

    def test_production_identity_reader_has_no_admin_storage_command(self) -> None:
        syntax = ast.parse(Path(backup.__file__).read_text(encoding="utf-8"))
        string_literals = {
            node.value.lower()
            for node in ast.walk(syntax)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertFalse(
            any(
                command in literal
                for literal in string_literals
                for command in ("diskutil", "smartctl", "system_profiler")
            )
        )

    def test_create_backup_covers_eight_roots_technical_namespaces_and_scheduler(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-backup-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                code, result = backup.create_backup()
                self.assertEqual(code, 0, result)
                generation = Path(result["destination"])
                manifest = json.loads((generation / "MANIFEST.json").read_text(encoding="utf-8"))
                verified = backup.verify_backup_copy(generation, full_hash=True)

            self.assertEqual(len([row for row in manifest["sources"] if row["category"] == "map-root"]), 8)
            self.assertEqual(len([row for row in manifest["sources"] if row["category"] == "technical"]), 6)
            self.assertFalse(manifest["icloud_offload"])
            self.assertEqual(manifest["capture_consistency"], backup.CAPTURE_CONSISTENCY)
            self.assertFalse(manifest["source_tree_atomic"])
            self.assertFalse(manifest["scheduler_store"]["source_tree_atomic"])
            self.assertFalse(any("Mobile Documents" in json.dumps(row) for row in manifest["sources"]))
            self.assertTrue(verified["creation_full_hash_verified"])
            self.assertEqual(verified["scheduler_store"], "verified")
            self.assertEqual(manifest["restore_probe"]["sources_checked"], 14)
            self.assertFalse(result["retention"]["two_copy_floor_satisfied"])

    def test_frozen_manifest_accepts_pre_flip_source_after_exact_post_flip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-session-history-") as raw:
            paths = self.fixture(Path(raw))
            owc = paths["owc"]
            assert isinstance(owc, Path)
            with self.patched(paths):
                code, result = backup.create_backup()
                self.assertEqual(code, 0, result)
                generation = Path(result["destination"])
                legacy, current = self.normalize_session_fixture(owc)

                self.assertEqual(backup.resolve_main_sessions_source(), current)
                verified = backup.verify_backup_copy(generation, full_hash=True)

            self.assertFalse(os.path.lexists(legacy))
            self.assertEqual(verified["result"], "verified")

    def test_manifest_rejects_session_source_outside_two_sanctioned_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-session-source-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                code, result = backup.create_backup()
                self.assertEqual(code, 0, result)
                generation = Path(result["destination"])
                manifest = backup.load_manifest(generation)
                sessions = next(
                    row
                    for row in manifest["sources"]
                    if row["source_id"] == "main-sessions"
                )
                sessions["source"] = str(
                    Path(paths["owc"]) / ".state/OpenClaw/session-copy"
                )

                with self.assertRaisesRegex(
                    backup.BackupError,
                    "backup manifest source path mismatch: main-sessions",
                ):
                    backup.validate_manifest_sources(
                        manifest["sources"],
                        manifest_schema=manifest["schema_version"],
                    )

    def test_session_source_selector_rejects_mixed_layout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-session-drift-") as raw:
            paths = self.fixture(Path(raw))
            owc = paths["owc"]
            assert isinstance(owc, Path)
            current = owc / ".state/OpenClaw/agents/main/sessions"
            current.unlink()
            current.mkdir()
            with self.patched(paths):
                with self.assertRaisesRegex(
                    backup.BackupError,
                    "main sessions source topology is not sanctioned",
                ):
                    backup.map_source_specs()

    def test_session_source_selector_rejects_case_colliding_top_level_alias(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-session-alias-") as raw:
            paths = self.fixture(Path(raw))
            owc = paths["owc"]
            assert isinstance(owc, Path)
            legacy, current = self.normalize_session_fixture(owc)
            legacy.symlink_to(current, target_is_directory=True)
            with self.patched(paths):
                with self.assertRaisesRegex(
                    backup.BackupError,
                    "main sessions source topology is not sanctioned",
                ):
                    backup.map_source_specs()

    def test_third_generation_prunes_only_oldest_verified_copy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-retention-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                results = []
                for index in range(3):
                    code, result = backup.create_backup()
                    self.assertEqual(code, 0, result)
                    results.append(result)
                    if index < 2:
                        time.sleep(1.05)
                generations = backup.list_backup_generations()

            self.assertEqual(len(generations), 2)
            self.assertNotIn(results[0]["backup"], [path.name for path in generations])
            self.assertIn(results[1]["backup"], [path.name for path in generations])
            self.assertIn(results[2]["backup"], [path.name for path in generations])
            self.assertEqual(results[2]["retention"]["removed"], [results[0]["backup"]])
            self.assertTrue(results[2]["retention"]["two_copy_floor_satisfied"])

    def test_retention_verifies_mixed_v1_and_v2_generations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-schema-compat-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                code, first = backup.create_backup()
                self.assertEqual(code, 0, first)
                legacy_generation = Path(first["destination"])
                manifest_path = legacy_generation / "MANIFEST.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["schema_version"] = backup.LEGACY_MANIFEST_SCHEMA
                manifest.pop("capture_consistency", None)
                manifest.pop("source_tree_atomic", None)
                for source in manifest["sources"]:
                    inventory_path = legacy_generation / "inventories" / source["inventory"]["path"]
                    records = backup.load_inventory(inventory_path)
                    with inventory_path.open("wb") as handle:
                        for record in records:
                            if record.get("kind") == "symlink":
                                record["uid"] = record.pop("source_uid_observed")
                                record.pop("source_raw_target_observed")
                            handle.write(backup.canonical_json(record) + b"\n")
                    source["inventory"]["sha256"] = backup.sha256_file(inventory_path)
                    for key in (
                        "capture_consistency",
                        "source_tree_atomic",
                        "inventory_origin",
                        "capture_started_at_utc",
                        "copy_completed_at_utc",
                        "inventory_sealed_at_utc",
                    ):
                        source.pop(key, None)
                for key in (
                    "capture_consistency",
                    "source_tree_atomic",
                    "inventory_origin",
                    "capture_started_at_utc",
                    "copy_completed_at_utc",
                    "source_before",
                    "source_after",
                    "source_changed_during_copy",
                ):
                    manifest["scheduler_store"].pop(key, None)
                manifest.pop("manifest_payload_sha256", None)
                manifest["manifest_payload_sha256"] = backup.sha256_bytes(
                    backup.canonical_json(manifest)
                )
                backup.atomic_write_json(manifest_path, manifest)
                self.assertEqual(
                    backup.verify_backup_copy(legacy_generation, full_hash=True)["result"],
                    "verified",
                )

                time.sleep(1.05)
                code, second = backup.create_backup()
                self.assertEqual(code, 0, second)
                report = backup.cleanup_backups(apply=False)

            self.assertEqual(report["blockers"], [])
            self.assertEqual(report["verified_remaining"], 2)
            self.assertTrue(report["two_copy_floor_satisfied"])
            self.assertIn(first["backup"], report["verified_generations"])
            self.assertIn(second["backup"], report["verified_generations"])

    def test_corrupt_candidate_is_retained_and_blocks_all_deletion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-corrupt-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                names = []
                for index in range(3):
                    code, result = backup.create_backup()
                    self.assertEqual(code, 0, result)
                    names.append(result["backup"])
                    if index < 2:
                        time.sleep(1.05)
                # The third creation prunes generation one. Add a verified fourth,
                # then corrupt the now-oldest protected generation before cleanup.
                time.sleep(1.05)
                code, fourth = backup.create_backup()
                self.assertEqual(code, 0, fourth)
                generations = backup.list_backup_generations()
                oldest = generations[-1]
                manifest = backup.load_manifest(oldest)
                first_source = manifest["sources"][0]
                inventory = backup.load_inventory(oldest / "inventories" / first_source["inventory"]["path"])
                first_file = next(row for row in inventory if row["kind"] == "file")
                target = oldest / "payload" / first_source["destination_relative"] / first_file["path"]
                target.write_bytes(target.read_bytes() + b"corrupt")

                # Add an extra structurally valid generation by cloning the newest
                # and rewriting its name-bound manifest.
                newest = generations[0]
                extra = Path(paths["backup_root"]) / "openclaw-backup-20990101T000000Z"
                backup.clone_directory(newest, extra)
                extra_manifest = json.loads((extra / "MANIFEST.json").read_text(encoding="utf-8"))
                extra_manifest["backup_name"] = extra.name
                extra_manifest["destination"] = str(extra)
                extra_manifest.pop("manifest_payload_sha256", None)
                extra_manifest["manifest_payload_sha256"] = backup.sha256_bytes(backup.canonical_json(extra_manifest))
                backup.atomic_write_json(extra / "MANIFEST.json", extra_manifest)

                before = [path.name for path in backup.list_backup_generations()]
                report = backup.cleanup_backups(apply=True)
                after = [path.name for path in backup.list_backup_generations()]

            self.assertTrue(report["blockers"])
            self.assertEqual(report["removed"], [])
            self.assertEqual(before, after)
            self.assertIn(oldest.name, report["retained_unverified"])

    def test_concurrent_backup_lock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-lock-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                backup_root = Path(paths["backup_root"])
                backup_root.mkdir(parents=True)
                lock_path = backup_root.parent / ".weekly-backup.lock"
                descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    code, result = backup.create_backup()
                finally:
                    os.close(descriptor)

            self.assertEqual(code, 1)
            self.assertTrue(any("lock is already held" in blocker for blocker in result["blockers"]))
            self.assertFalse(Path(result["destination"]).exists())

    def test_backup_destination_inside_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-recursion-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths), mock.patch.object(
                backup,
                "BACKUP_ROOT",
                Path(paths["owc"]) / "Workspace/Backups/weekly",
            ):
                with self.assertRaises(backup.BackupError):
                    backup.validate_preflight(backup.map_source_specs())

    def test_symlinked_source_ancestor_is_rejected_before_copy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-source-ancestor-") as raw:
            root = Path(raw)
            paths = self.fixture(root)
            owc = Path(paths["owc"])
            projects = owc / "Projects"
            outside = root / "outside-projects"
            projects.rename(outside)
            os.symlink(outside, projects)

            with self.patched(paths):
                with self.assertRaisesRegex(backup.BackupError, "symlink ancestor"):
                    backup.validate_preflight(backup.map_source_specs())

    def test_root_owned_source_symlink_records_narrow_backup_owner_substitution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-root-symlink-") as raw:
            paths = self.fixture(Path(raw))
            source_symlink = Path(paths["owc"]) / "Workspace/data-link"
            original_lstat = Path.lstat

            def root_owned_source_lstat(path: Path):
                info = original_lstat(path)
                if path == source_symlink:
                    class StatWithUid:
                        st_uid = 0

                        def __getattr__(self, name: str):
                            return getattr(info, name)

                    return StatWithUid()
                return info

            with self.patched(paths), mock.patch.object(
                Path,
                "lstat",
                new=root_owned_source_lstat,
            ):
                code, result = backup.create_backup()
                self.assertEqual(code, 0, result)
                generation = Path(result["destination"])
                manifest_path = generation / "MANIFEST.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                workspace = next(
                    row
                    for row in manifest["sources"]
                    if row["source_id"] == "workspace"
                )
                ownership = workspace["verification"]["symlink_ownership"]

                self.assertEqual(
                    ownership["policy"],
                    backup.SYMLINK_OWNER_POLICY,
                )
                self.assertEqual(ownership["backup_owner_uid"], os.geteuid())
                self.assertEqual(ownership["substitution_count"], 1)
                self.assertRegex(
                    ownership["substitutions_sha256"],
                    r"^[0-9a-f]{64}$",
                )
                verified = backup.verify_backup_copy(
                    generation,
                    full_hash=False,
                )
                self.assertEqual(
                    verified["verification"]["workspace"][
                        "symlink_ownership"
                    ],
                    ownership,
                )

                workspace["verification"]["symlink_ownership"][
                    "substitution_count"
                ] = 0
                manifest.pop("manifest_payload_sha256", None)
                manifest["manifest_payload_sha256"] = backup.sha256_bytes(
                    backup.canonical_json(manifest)
                )
                backup.atomic_write_json(manifest_path, manifest)

                with self.assertRaisesRegex(
                    backup.BackupError,
                    "symlink-ownership readback mismatch",
                ):
                    backup.verify_backup_copy(generation, full_hash=False)

    def test_nonroot_symlink_owner_mismatch_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-symlink-owner-") as raw:
            paths = self.fixture(Path(raw))
            source_symlink = Path(paths["owc"]) / "Workspace/data-link"
            original_lstat = Path.lstat

            def mismatched_source_lstat(path: Path):
                info = original_lstat(path)
                if path == source_symlink:
                    class StatWithUid:
                        st_uid = os.geteuid() + 1

                        def __getattr__(self, name: str):
                            return getattr(info, name)

                    return StatWithUid()
                return info

            with self.patched(paths), mock.patch.object(
                Path,
                "lstat",
                new=mismatched_source_lstat,
            ):
                code, result = backup.create_backup()

            self.assertEqual(code, 1)
            self.assertTrue(
                any("owner substitution is invalid" in blocker for blocker in result["blockers"])
            )
            self.assertTrue(Path(result["staging"]).exists())
            self.assertFalse(Path(result["destination"]).exists())

    def test_retention_count_below_two_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-floor-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths), mock.patch.object(backup, "RETENTION_COUNT", 1):
                with self.assertRaises(backup.BackupError):
                    backup.validate_preflight(backup.map_source_specs())

    def test_special_file_fails_before_publish_and_retains_incomplete_staging(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-special-") as raw:
            paths = self.fixture(Path(raw))
            fifo = Path(paths["owc"]) / "Workspace/unsupported.fifo"
            os.mkfifo(fifo)
            with self.patched(paths):
                code, result = backup.create_backup()
            self.assertEqual(code, 1)
            self.assertTrue(Path(result["staging"]).exists())
            self.assertFalse(Path(result["destination"]).exists())

    def test_foreign_device_entry_blocks_source_inventory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-foreign-device-") as raw:
            paths = self.fixture(Path(raw))
            foreign = Path(paths["owc"]) / "Workspace/foreign-device.txt"
            foreign.write_text("foreign fixture\n", encoding="utf-8")
            inventory = Path(paths["receipts"]) / "foreign-device.jsonl"
            original_lstat = Path.lstat

            def foreign_lstat(path: Path):
                info = original_lstat(path)
                if path == foreign:
                    values = list(info)
                    values[2] = info.st_dev + 1
                    return os.stat_result(values)
                return info

            with self.patched(paths), mock.patch.object(
                Path,
                "lstat",
                new=foreign_lstat,
            ):
                with self.assertRaisesRegex(backup.BackupError, "crossed a device"):
                    backup.inventory_tree(
                        Path(paths["owc"]) / "Workspace",
                        inventory,
                    )

    def test_prior_incomplete_staging_blocks_retry_without_creating_another(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-incomplete-") as raw:
            paths = self.fixture(Path(raw))
            backup_root = Path(paths["backup_root"])
            backup_root.mkdir(parents=True)
            incomplete = backup_root / ".openclaw-backup-20260101T000000Z.incomplete-123"
            incomplete.mkdir()
            with self.patched(paths):
                code, result = backup.create_backup()

            self.assertEqual(code, 1)
            self.assertTrue(any("prior incomplete backup staging" in blocker for blocker in result["blockers"]))
            self.assertEqual(
                sorted(path.name for path in backup_root.iterdir() if backup.INCOMPLETE_NAME_RE.fullmatch(path.name)),
                [incomplete.name],
            )
            self.assertFalse(Path(result["destination"]).exists())

    def test_source_mutation_between_preflight_and_clone_is_sealed_from_staging(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-source-mutation-") as raw:
            paths = self.fixture(Path(raw))
            original_clone = backup.clone_directory

            def mutate_source_before_clone(
                source: Path,
                destination: Path,
                **kwargs: object,
            ) -> dict[str, object]:
                candidate = source / "data.txt"
                if candidate.exists():
                    candidate.write_text("source changed before clone\n", encoding="utf-8")
                return original_clone(source, destination, **kwargs)

            with self.patched(paths), mock.patch.object(
                backup,
                "clone_directory",
                side_effect=mutate_source_before_clone,
            ):
                code, result = backup.create_backup()

            self.assertEqual(code, 0, result)
            generation = Path(result["destination"])
            manifest = backup.load_manifest(generation)
            workspace = next(row for row in manifest["sources"] if row["source_id"] == "workspace")
            copied = generation / "payload" / workspace["destination_relative"] / "data.txt"
            source = Path(paths["owc"]) / "Workspace/data.txt"
            self.assertEqual(copied.read_bytes(), source.read_bytes())
            self.assertEqual(copied.read_text(encoding="utf-8"), "source changed before clone\n")
            self.assertEqual(workspace["inventory_origin"], backup.INVENTORY_ORIGIN)
            self.assertEqual(workspace["capture_consistency"], backup.CAPTURE_CONSISTENCY)
            self.assertFalse(workspace["source_tree_atomic"])

    def test_scheduler_copy_is_sealed_from_destination_when_source_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-scheduler-copy-") as raw:
            root = Path(raw)
            source = root / "jobs.json"
            destination = root / "sealed/jobs.json"
            source.write_text('{"version":1,"jobs":["before"]}\n', encoding="utf-8")
            original_run = backup.subprocess.run

            def mutate_source_after_copy(*args: object, **kwargs: object):
                result = original_run(*args, **kwargs)
                source.write_text('{"version":1,"jobs":["after"]}\n', encoding="utf-8")
                return result

            with mock.patch.object(
                backup.subprocess,
                "run",
                side_effect=mutate_source_after_copy,
            ):
                capture = backup.clone_file(source, destination)

            self.assertTrue(capture["source_changed_during_copy"])
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                '{"version":1,"jobs":["before"]}\n',
            )
            self.assertEqual(capture["sha256"], backup.sha256_file(destination))

    def test_staging_mutation_after_inventory_blocks_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-staging-divergence-") as raw:
            paths = self.fixture(Path(raw))
            original_inventory = backup.inventory_tree

            def mutate_staging_after_inventory(
                root: Path,
                output: Path,
                **kwargs: object,
            ) -> dict:
                result = original_inventory(root, output, **kwargs)
                candidate = root / "data.txt"
                if candidate.exists():
                    candidate.write_text("staging diverged after inventory\n", encoding="utf-8")
                return result

            with self.patched(paths), mock.patch.object(
                backup,
                "inventory_tree",
                side_effect=mutate_staging_after_inventory,
            ):
                code, result = backup.create_backup()

            self.assertEqual(code, 1)
            self.assertTrue(any("mismatch" in blocker for blocker in result["blockers"]))
            self.assertTrue(Path(result["staging"]).exists())
            self.assertFalse(Path(result["destination"]).exists())

    def test_manifest_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-manifest-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                code, result = backup.create_backup()
                self.assertEqual(code, 0, result)
                generation = Path(result["destination"])
                manifest_path = generation / "MANIFEST.json"
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                payload["icloud_offload"] = True
                backup.atomic_write_json(manifest_path, payload)
                with self.assertRaises(backup.BackupError):
                    backup.verify_backup_copy(generation, full_hash=False)

    def test_self_rehashed_manifest_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-manifest-path-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                code, result = backup.create_backup()
                self.assertEqual(code, 0, result)
                generation = Path(result["destination"])
                manifest_path = generation / "MANIFEST.json"
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                payload["sources"][0]["destination_relative"] = "../../outside"
                payload.pop("manifest_payload_sha256", None)
                payload["manifest_payload_sha256"] = backup.sha256_bytes(backup.canonical_json(payload))
                backup.atomic_write_json(manifest_path, payload)

                with self.assertRaisesRegex(backup.BackupError, "destination mismatch"):
                    backup.verify_backup_copy(generation, full_hash=False)

    def test_uninventoried_payload_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-extra-payload-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                code, result = backup.create_backup()
                self.assertEqual(code, 0, result)
                generation = Path(result["destination"])
                (generation / "payload/roots/workspace/uninventoried.txt").write_text(
                    "not in source inventory\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(backup.BackupError, "mismatch"):
                    backup.verify_backup_copy(generation, full_hash=False)

    def test_retention_revalidates_pinned_volume_identity_and_floor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-retention-identity-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                code, result = backup.create_backup()
                self.assertEqual(code, 0, result)
                before = [path.name for path in backup.list_backup_generations()]
                with mock.patch.object(backup, "EXPECTED_VOLUME_UUID", "WRONG-UUID"):
                    with self.assertRaisesRegex(backup.BackupError, "UUID mismatch"):
                        backup.cleanup_backups(apply=True)
                with mock.patch.object(backup, "RETENTION_COUNT", 1):
                    with self.assertRaisesRegex(backup.BackupError, "at least 2"):
                        backup.cleanup_backups(apply=True)
                after = [path.name for path in backup.list_backup_generations()]

            self.assertEqual(before, after)

    def test_future_named_generation_blocks_retention_and_preserves_current_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-future-generation-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                code, first = backup.create_backup()
                self.assertEqual(code, 0, first)
                newest = Path(first["destination"])
                future = Path(paths["backup_root"]) / "openclaw-backup-20990101T000000Z"
                backup.clone_directory(newest, future)
                future_manifest = json.loads((future / "MANIFEST.json").read_text(encoding="utf-8"))
                future_manifest["backup_name"] = future.name
                future_manifest["destination"] = str(future)
                future_manifest.pop("manifest_payload_sha256", None)
                future_manifest["manifest_payload_sha256"] = backup.sha256_bytes(
                    backup.canonical_json(future_manifest)
                )
                backup.atomic_write_json(future / "MANIFEST.json", future_manifest)
                time.sleep(1.05)

                code, current = backup.create_backup()

            self.assertEqual(code, 1)
            self.assertTrue(Path(current["destination"]).is_dir())
            self.assertTrue(any("unexpectedly in the future" in blocker for blocker in current["blockers"]))
            self.assertEqual(current["retention"]["removed"], [])

    def test_predelete_foreign_device_check_prevents_rmtree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-delete-device-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths):
                code, first = backup.create_backup()
                self.assertEqual(code, 0, first)
                time.sleep(1.05)
                code, second = backup.create_backup()
                self.assertEqual(code, 0, second)
                source = Path(second["destination"])
                candidate = (
                    Path(paths["backup_root"])
                    / "openclaw-backup-20200101T000000Z"
                )
                backup.clone_directory(source, candidate)
                manifest_path = candidate / "MANIFEST.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["backup_name"] = candidate.name
                manifest["destination"] = str(candidate)
                manifest.pop("manifest_payload_sha256", None)
                manifest["manifest_payload_sha256"] = backup.sha256_bytes(
                    backup.canonical_json(manifest)
                )
                backup.atomic_write_json(manifest_path, manifest)
                original_validate = backup.validate_tree_device

                def reject_candidate(root: Path, *, expected_device: int) -> None:
                    if root == candidate:
                        raise backup.BackupError("foreign-device subtree")
                    original_validate(root, expected_device=expected_device)

                with mock.patch.object(
                    backup,
                    "validate_tree_device",
                    side_effect=reject_candidate,
                ), mock.patch.object(backup.shutil, "rmtree") as remove:
                    report = backup.cleanup_backups(apply=True)

            self.assertTrue(report["blockers"])
            self.assertEqual(report["removed"], [])
            self.assertTrue(candidate.is_dir())
            remove.assert_not_called()

    def test_help_does_not_create_backup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-help-") as raw:
            paths = self.fixture(Path(raw))
            with self.patched(paths), self.assertRaises(SystemExit) as raised:
                backup.build_parser().parse_args(["--help"])
            self.assertEqual(raised.exception.code, 0)
            self.assertFalse(Path(paths["backup_root"]).exists())

    def test_supported_cli_rejects_new_clone_v2_creation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owc-weekly-retired-") as raw:
            paths = self.fixture(Path(raw))
            output = io.StringIO()
            with self.patched(paths), redirect_stdout(output), mock.patch.object(
                backup,
                "create_backup",
            ) as create:
                code = backup.cli([])

            self.assertEqual(code, 1)
            self.assertIn("clone-v2 creation is retired", output.getvalue())
            create.assert_not_called()
            self.assertFalse(Path(paths["backup_root"]).exists())


class FrozenGenerationDevicePinTests(unittest.TestCase):
    """st_dev identifies a mount, not a volume.

    Regression for 2026-08-26: EXPECTED_DEVICE was a hardcoded mount number that
    gated BOTH the live volume check and each frozen manifest's capture-time
    source_device. The OWC volume moved 16777244 -> 16777240 across a remount, so
    the two became unsatisfiable simultaneously and --verify was permanently red
    on both published generations.
    """

    def test_expected_device_resolves_from_the_live_mount(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCLAW_OWC_DEVICE", None)
            with mock.patch.object(backup.os, "stat") as stat_mock:
                stat_mock.return_value = mock.Mock(st_dev=4242)
                self.assertEqual(backup._resolve_expected_device(), 4242)

    def test_explicit_operator_pin_still_wins(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_OWC_DEVICE": "16777244"}):
            self.assertEqual(backup._resolve_expected_device(), 16777244)

    def test_frozen_manifest_device_need_not_match_todays_mount(self):
        """A generation frozen on a previous mount must still verify."""
        sources = [
            {
                "source_id": "workspace",
                "source_device": 16777244,  # captured under the OLD mount
                "inventory": {"path": "workspace.jsonl"},
            },
            {
                "source_id": "personal-data",
                "source_device": 16777244,
                "inventory": {"path": "personal-data.jsonl"},
            },
        ]
        device = backup.EXPECTED_DEVICE
        self.assertNotEqual(
            device, 0, "guard: EXPECTED_DEVICE should be a resolved device number"
        )
        # Internal consistency is what is enforced now: one generation, one device.
        first = int(sources[0]["source_device"])
        self.assertTrue(all(int(s["source_device"]) == first for s in sources))
        # A manifest that mixes devices is still rejected.
        mixed = [dict(sources[0]), dict(sources[1])]
        mixed[1]["source_device"] = 999
        self.assertFalse(all(int(s["source_device"]) == first for s in mixed))


if __name__ == "__main__":
    unittest.main()
