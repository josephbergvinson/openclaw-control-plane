from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from scripts import openclaw_independent_backup_receipt as proof


class IndependentBackupReceiptTests(unittest.TestCase):
    def fixture(self, root: Path):
        source = root / "source/weekly"
        backup = root / "backup/weekly"
        restore = root / "restore/weekly"
        (source / "generation-a").mkdir(parents=True)
        (source / "generation-a/MANIFEST.json").write_text(
            '{"generation":"a"}\n', encoding="utf-8"
        )
        (source / "generation-a/archive.tgz").write_bytes(b"archive-a")
        shutil.copytree(source, backup)
        shutil.copytree(source, restore)
        actual_device = source.stat().st_dev
        rows = {
            str(source.resolve()): proof.VolumeIdentity(
                "SOURCE-UUID", actual_device, str(root)
            ),
            str(backup.resolve()): proof.VolumeIdentity(
                "BACKUP-UUID", actual_device, str(root)
            ),
            str(restore.resolve()): proof.VolumeIdentity(
                "RESTORE-UUID", actual_device, str(root)
            ),
        }

        def identity_reader(path: Path):
            return rows[str(Path(path).resolve())]

        def verified_fixture_adapter(path: Path, *, identity_reader):
            inventory = proof.build_tree_inventory(
                path,
                identity_reader=identity_reader,
            )
            absolute = str(Path(path).resolve())
            if absolute == str(backup.resolve()):
                return replace(
                    inventory,
                    volume_uuid="BACKUP-UUID",
                    device=actual_device + 10,
                )
            if absolute == str(restore.resolve()):
                return replace(
                    inventory,
                    volume_uuid="RESTORE-UUID",
                    device=actual_device + 20,
                )
            return inventory

        return (
            source,
            backup,
            restore,
            actual_device,
            identity_reader,
            verified_fixture_adapter,
        )

    def test_default_identity_reader_uses_vfs_volume_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="independent-proof-vfs-") as raw:
            weekly = Path(raw) / "weekly"
            weekly.mkdir()
            (weekly / "generation.json").write_text("{}\n", encoding="utf-8")
            device = weekly.stat().st_dev
            observed_mounts: list[Path] = []

            def metadata(mount: Path) -> dict[str, object]:
                observed_mounts.append(mount)
                return {
                    "available": True,
                    "mountDevice": device,
                    "mountPoint": str(mount),
                    "mounted": True,
                    "ownersEnabled": True,
                    "probeMethod": "getattrlist-statvfs",
                    "readOnly": False,
                    "volumeUuid": "fixture-vfs-uuid",
                }

            with mock.patch.object(
                proof.external_volume_guard,
                "volume_metadata",
                side_effect=metadata,
            ) as metadata_probe:
                inventory = proof.build_tree_inventory(weekly)

            self.assertEqual(metadata_probe.call_count, 1)
            self.assertEqual(len(observed_mounts), 1)
            self.assertTrue(os.path.ismount(observed_mounts[0]))
            self.assertEqual(inventory.volume_uuid, "FIXTURE-VFS-UUID")
            self.assertEqual(inventory.device, device)
            self.assertEqual(inventory.mount, str(observed_mounts[0]))

    def test_default_identity_reader_fails_closed_on_unavailable_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="independent-proof-vfs-fail-") as raw:
            weekly = Path(raw) / "weekly"
            weekly.mkdir()
            (weekly / "generation.json").write_text("{}\n", encoding="utf-8")
            with mock.patch.object(
                proof.external_volume_guard,
                "volume_metadata",
                return_value={
                    "available": False,
                    "mounted": True,
                    "probeMethod": "getattrlist-statvfs",
                    "error": "OSError",
                },
            ):
                with self.assertRaisesRegex(
                    proof.IndependentBackupEvidenceError,
                    "VFS volume identity is unavailable",
                ):
                    proof.build_tree_inventory(weekly)

    def test_production_identity_reader_has_no_admin_storage_command(self) -> None:
        syntax = ast.parse(Path(proof.__file__).read_text(encoding="utf-8"))
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

    def test_verified_fixture_evidence_produces_private_derived_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="independent-proof-") as raw:
            root = Path(raw)
            (
                source,
                backup,
                restore,
                source_device,
                identity_reader,
                adapter,
            ) = self.fixture(root)
            output = root / "artifacts/receipt.json"
            payload = proof.produce_receipt(
                source_weekly_root=source,
                backup_weekly_root=backup,
                isolated_restore_weekly_root=restore,
                output=output,
                identity_reader=identity_reader,
                inventory_builder=adapter,
                now=lambda: "2026-08-03T20:00:00Z",
            )

            info = output.lstat()
            self.assertTrue(stat.S_ISREG(info.st_mode))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(payload["producer"], proof.PRODUCER)
            self.assertEqual(payload["source_device"], source_device)
            self.assertEqual(payload["backup_device"], source_device + 10)
            self.assertEqual(
                payload["source_inventory_sha256"],
                payload["backup_inventory_sha256"],
            )
            self.assertEqual(
                payload["source_inventory_sha256"],
                payload["restore_inventory_sha256"],
            )
            validated = proof.validate_receipt_payload(
                json.loads(output.read_text(encoding="utf-8")),
                source_weekly_root=source,
                expected_source_volume_uuid="SOURCE-UUID",
                expected_source_device=source_device,
                identity_reader=identity_reader,
                now=lambda: datetime(
                    2026, 8, 3, 20, 1, tzinfo=timezone.utc
                ),
            )
            self.assertEqual(
                validated["evidence_sha256"], payload["evidence_sha256"]
            )

    def test_mismatched_restore_refuses_without_writing_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="independent-proof-mismatch-") as raw:
            root = Path(raw)
            source, backup, restore, _device, identity_reader, adapter = self.fixture(root)
            (restore / "generation-a/archive.tgz").write_bytes(b"different")
            output = root / "artifacts/receipt.json"

            with self.assertRaisesRegex(
                proof.IndependentBackupEvidenceError,
                "inventories do not match",
            ):
                proof.produce_receipt(
                    source_weekly_root=source,
                    backup_weekly_root=backup,
                    isolated_restore_weekly_root=restore,
                    output=output,
                    identity_reader=identity_reader,
                    inventory_builder=adapter,
                )
            self.assertFalse(output.exists())

    def test_production_shape_refuses_same_device_backup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="independent-proof-device-") as raw:
            root = Path(raw)
            source, backup, restore, device, _reader, _adapter = self.fixture(root)

            def same_device_reader(path: Path):
                names = {
                    str(source.resolve()): "SOURCE-UUID",
                    str(backup.resolve()): "BACKUP-UUID",
                    str(restore.resolve()): "RESTORE-UUID",
                }
                return proof.VolumeIdentity(
                    names[str(Path(path).resolve())],
                    device,
                    str(root),
                )

            with self.assertRaisesRegex(
                proof.IndependentBackupEvidenceError,
                "same device",
            ):
                proof.produce_receipt(
                    source_weekly_root=source,
                    backup_weekly_root=backup,
                    isolated_restore_weekly_root=restore,
                    output=root / "receipt.json",
                    identity_reader=same_device_reader,
                )

    def test_legacy_caller_assertion_fixture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="independent-proof-legacy-") as raw:
            root = Path(raw)
            source, _backup, _restore, device, identity_reader, _adapter = self.fixture(root)
            legacy = {
                "schema_version": proof.SCHEMA,
                "verified": True,
                "independent_backup": True,
                "source_volume_uuid": "SOURCE-UUID",
                "source_device": device,
                "source_role": "openclaw_primary",
                "backup_volume_uuid": "BACKUP-UUID",
                "backup_device": device + 1,
                "backup_role": "independent_recovery",
                "covered_weekly_root": str(source),
                "verified_at_utc": "2026-08-03T20:00:00Z",
            }
            with self.assertRaisesRegex(
                proof.IndependentBackupEvidenceError,
                "producer mismatch",
            ):
                proof.validate_receipt_payload(
                    legacy,
                    source_weekly_root=source,
                    expected_source_volume_uuid="SOURCE-UUID",
                    expected_source_device=device,
                    identity_reader=identity_reader,
                    now=lambda: datetime(
                        2026, 8, 3, 20, 1, tzinfo=timezone.utc
                    ),
                )


if __name__ == "__main__":
    unittest.main()
