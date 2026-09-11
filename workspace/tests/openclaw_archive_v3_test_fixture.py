"""Historical v3 generation fixture; never imported by a production owner.

Retained only to exercise archive-v3 readers and destructive-retention gates
against the original generation contract after native state capture replaces it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import shutil
import stat
from typing import Callable

from scripts import openclaw_weekly_archive_backup as archive_backup


def create_v3_fixture(
    config: archive_backup.BackupConfig = archive_backup.BackupConfig(),
    *,
    identity_reader: Callable[[archive_backup.BackupConfig], archive_backup.VolumeIdentity] = archive_backup.read_volume_identity,
    now: datetime | None = None,
    sqlite_snapshot_creator: Callable[..., archive_backup.SqliteSnapshotSet] = (
        archive_backup.create_sqlite_snapshot_repository
    ),
) -> tuple[int, dict[str, Any]]:
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    run_id = timestamp.strftime("%Y%m%dT%H%M%SZ")
    final = config.backup_root / "openclaw-archive-v3-{}".format(run_id)
    staging = config.backup_root / (
        ".openclaw-archive-v3-{}.incomplete-{}".format(
            run_id,
            os.getpid(),
        )
    )
    previous_umask = os.umask(0o077)
    try:
        receipt = archive_backup.PhaseReceipt(config, run_id, final)
    except Exception as exc:
        os.umask(previous_umask)
        return 1, {
            "backup": final.name,
            "destination": str(final),
            "receipt": str(
                config.receipt_dir
                / "openclaw-weekly-archive-backup-{}.json".format(run_id)
            ),
            "staging": str(staging),
            "blockers": [
                "{}: {}".format(type(exc).__name__, exc)
            ],
            "retention_performed": False,
            "result": "blocked",
        }
    descriptor: int | None = None
    blockers: list[str] = []
    try:
        identity = archive_backup.validate_environment(
            config,
            identity_reader,
            create_backup_root=True,
        )
        session_route = archive_backup.resolve_session_store_route(config, identity)
        lock_path = config.backup_root.parent / ".weekly-backup.lock"
        lock_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, lock_flags, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise archive_backup.BackupError("weekly backup lock is already held") from exc

        prior_incomplete = sorted(
            child.name
            for child in config.backup_root.iterdir()
            if archive_backup.INCOMPLETE_NAME_RE.fullmatch(child.name)
        )
        archive_backup.require(
            not prior_incomplete,
            "prior incomplete staging requires classification: {}".format(
                ",".join(prior_incomplete)
            ),
        )
        archive_backup.require(not os.path.lexists(final), "backup generation collision")
        archive_backup.require(not os.path.lexists(staging), "backup staging collision")

        pre_snapshot_agent_ids = archive_backup.configured_agent_ids(config)
        sqlite_source_inventory = archive_backup.authoritative_sqlite_source_inventory(
            config,
            pre_snapshot_agent_ids,
        )
        usage_before = shutil.disk_usage(config.backup_root)
        required_free_before_snapshots = (
            config.min_post_backup_free_bytes
            + sqlite_source_inventory["snapshot_peak_headroom_bytes"]
        )
        archive_backup.require(
            usage_before.free >= required_free_before_snapshots,
            "insufficient OWC headroom before SQLite snapshots",
        )

        staging.mkdir(mode=0o700)
        os.chmod(staging, 0o700)
        archive_backup.fsync_dir(staging.parent)
        probe_root = staging / ".restore-probe"
        probe_root.mkdir(mode=0o700)
        probe_info = probe_root.lstat()
        archive_backup.require(
            stat.S_ISDIR(probe_info.st_mode)
            and not probe_root.is_symlink()
            and probe_info.st_dev == identity.device,
            "producer restore-probe root is not a physical OWC directory",
        )
        sqlite_repository = probe_root / "sqlite-snapshots"
        receipt.phase(
            "sqlite-snapshot-preflight",
            "verified",
            command_contract=(
                "openclaw backup sqlite create --json"
            ),
            verification_contract=(
                "openclaw backup sqlite verify <snapshot> --json"
            ),
            openclaw_state_dir=str(config.state_root),
            free_before_bytes=usage_before.free,
            minimum_post_backup_free_bytes=(
                config.min_post_backup_free_bytes
            ),
            required_free_before_snapshots_bytes=(
                required_free_before_snapshots
            ),
            source_inventory=sqlite_source_inventory,
        )
        sqlite_snapshots = sqlite_snapshot_creator(
            config,
            sqlite_repository,
            expected_device=identity.device,
        )
        repository_path = archive_backup.absolute(sqlite_repository.resolve(strict=True))
        archive_backup.require(
            archive_backup.absolute(sqlite_snapshots.repository.resolve(strict=True))
            == repository_path
            and sqlite_snapshots.agent_ids == pre_snapshot_agent_ids
            and len(sqlite_snapshots.snapshot_paths)
            == len(sqlite_snapshots.agent_ids) + 1
            and len(
                {path.name for path in sqlite_snapshots.snapshot_paths}
            )
            == len(sqlite_snapshots.snapshot_paths)
            and all(
                archive_backup.absolute(path.resolve(strict=True)).parent
                == repository_path
                for path in sqlite_snapshots.snapshot_paths
            ),
            "SQLite snapshot creator returned incomplete coverage",
        )
        sqlite_manifest_record = sqlite_snapshots.as_manifest_record(
            result="created"
        )
        receipt.phase(
            "sqlite-snapshots",
            "created",
            sqlite_backup=sqlite_manifest_record,
        )

        specs = archive_backup.build_archive_specs(
            config,
            identity,
            sqlite_snapshots,
            session_route=session_route,
        )
        estimate = archive_backup.estimate_archives(specs)
        sqlite_selective_restore_bytes = archive_backup.sqlite_snapshot_payload_bytes(
            sqlite_snapshots
        )
        sqlite_verifier_transient_bytes = (
            archive_backup.sqlite_snapshot_verification_transient_bytes(
                sqlite_snapshots
            )
        )
        archive_backup.require(
            estimate["restore_budget_components"][
                "sqlite_snapshot_selective_restore_bytes"
            ]
            == sqlite_selective_restore_bytes,
            "SQLite selective restore estimate does not bind snapshot bytes",
        )
        archive_backup.require(
            estimate["restore_budget_components"][
                "sqlite_snapshot_verification_transient_bytes"
            ]
            == sqlite_verifier_transient_bytes,
            "SQLite verification estimate does not bind snapshot bytes",
        )
        usage_after_sqlite_snapshots = shutil.disk_usage(config.backup_root)
        required_free = (
            estimate["archive_output_upper_bytes"]
            + estimate["restore_probe_budget_bytes"]
            + config.min_post_backup_free_bytes
        )
        archive_backup.require(
            usage_after_sqlite_snapshots.free >= required_free,
            "insufficient OWC headroom: free={} required={}".format(
                usage_after_sqlite_snapshots.free,
                required_free,
            ),
        )
        preflight = {
            "volume": {
                "uuid": identity.uuid,
                "device": identity.device,
                "mount": str(identity.mount),
                "writable": identity.writable,
                "owners_enabled": identity.owners_enabled,
            },
            "backup_root": str(config.backup_root),
            "payload_contract": archive_backup.PAYLOAD_CONTRACT,
            "capture_consistency": archive_backup.CAPTURE_CONSISTENCY,
            "source_tree_atomic": False,
            "same_device_local_recovery": True,
            "independent_disaster_recovery": False,
            "icloud_offload": False,
            "size_estimate": estimate,
            "headroom": {
                "free_before_bytes": usage_before.free,
                "free_after_sqlite_snapshots_bytes": (
                    usage_after_sqlite_snapshots.free
                ),
                "minimum_post_backup_free_bytes": (
                    config.min_post_backup_free_bytes
                ),
                "required_free_after_sqlite_snapshots_bytes": required_free,
                "required_free_before_sqlite_snapshots_bytes": (
                    required_free_before_snapshots
                ),
            },
            "sqlite_backup": sqlite_manifest_record,
        }
        receipt.phase("preflight", "verified", preflight=preflight)

        archive_records: list[dict[str, Any]] = []
        for spec in specs:
            record = archive_backup.create_archive(
                staging / spec.filename,
                spec,
                expected_device=identity.device,
                minimum_stream_free_bytes=(
                    config.min_post_backup_free_bytes
                    + archive_backup.RESTORE_METADATA_BASE_BYTES
                ),
            )
            archive_records.append(record)
            receipt.phase(
                "archive-{}".format(spec.role),
                "created",
                archive={
                    key: value
                    for key, value in record.items()
                    if key != "restore_candidates"
                },
            )

        usage_after_archives = shutil.disk_usage(config.backup_root)
        actual_restore_budget = archive_backup.restore_budget_from_archive_records(
            archive_records
        )
        archive_backup.require(
            actual_restore_budget[
                "sqlite_snapshot_selective_restore_bytes"
            ]
            == sqlite_selective_restore_bytes,
            "SQLite selective restore budget does not bind archived bytes",
        )
        archive_backup.require(
            actual_restore_budget[
                "sqlite_snapshot_verification_transient_bytes"
            ]
            == sqlite_verifier_transient_bytes,
            "SQLite verification budget does not bind archived bytes",
        )
        archive_backup.require(
            usage_after_archives.free
            >= (
                config.min_post_backup_free_bytes
                + actual_restore_budget["total_bytes"]
            ),
            "OWC post-archive free space cannot cover restore verification "
            "and the configured floor",
        )
        preflight["headroom"]["free_after_archive_payloads_bytes"] = (
            usage_after_archives.free
        )
        preflight["headroom"]["archive_payload_logical_bytes"] = sum(
            int(row["logical_bytes"]) for row in archive_records
        )
        preflight["headroom"]["archive_payload_allocated_bytes"] = sum(
            int(row["allocated_bytes"]) for row in archive_records
        )
        preflight["headroom"][
            "actual_restore_verification_budget"
        ] = actual_restore_budget
        receipt.phase(
            "post-archive-headroom",
            "verified",
            headroom=dict(preflight["headroom"]),
            compression=[
                {
                    "name": row["name"],
                    "archive_to_source_logical_ratio": row[
                        "archive_to_source_logical_ratio"
                    ],
                    "source_to_archive_logical_ratio": row[
                        "source_to_archive_logical_ratio"
                    ],
                }
                for row in archive_records
            ],
        )
        verification: list[dict[str, Any]] = []
        try:
            records_by_name = {
                row["name"]: row for row in archive_records
            }
            for spec in specs:
                archive_backup.require(
                    shutil.disk_usage(config.backup_root).free
                    >= config.min_post_backup_free_bytes,
                    "OWC free space crossed the configured floor during "
                    "restore verification",
                )
                verification.append(
                    archive_backup.verify_archive(
                        staging / spec.filename,
                        spec,
                        records_by_name[spec.filename],
                        probe_root=probe_root,
                        expected_device=identity.device,
                        config=config,
                        sqlite_snapshots=(
                            sqlite_snapshots
                            if spec.role == "state"
                            else None
                        ),
                    )
                )
                archive_backup.require(
                    shutil.disk_usage(config.backup_root).free
                    >= config.min_post_backup_free_bytes,
                    "OWC free space crossed the configured floor during "
                    "restore verification",
                )
        finally:
            archive_backup.remove_owned_directory_tree(
                probe_root,
                expected_parent=staging,
                expected_device=identity.device,
                expected_identity=(
                    probe_info.st_dev,
                    probe_info.st_ino,
                ),
                name_allowed=probe_root.name == ".restore-probe",
                purpose="producer restore probe",
            )
        receipt.phase(
            "full-decompression-and-restore-probes",
            "verified",
            verification=verification,
        )

        manifest = archive_backup.write_generation_metadata(
            staging,
            config=config,
            run_id=run_id,
            preflight=preflight,
            archive_records=archive_records,
            verification=verification,
            sqlite_snapshots=sqlite_snapshots,
            session_route=session_route,
        )
        archive_backup.generation_file_modes(staging)
        archive_backup.require(
            archive_backup.load_manifest(staging) == manifest,
            "staging manifest readback mismatch",
        )
        os.rename(staging, final)
        archive_backup.fsync_dir(config.backup_root)
        published = archive_backup.verify_published_generation(final, config=config)
        receipt.finish(
            "completed",
            "verified",
            [],
            backup=final.name,
            manifest=str(final / "MANIFEST.json"),
            verification=published,
            retention_performed=False,
        )
        return 0, {
            "backup": final.name,
            "destination": str(final),
            "receipt": str(receipt.path),
            "manifest": str(final / "MANIFEST.json"),
            "verification": published,
            "retention_performed": False,
            "blockers": [],
            "result": "verified",
        }
    except Exception as exc:
        blockers.append("{}: {}".format(type(exc).__name__, exc))
        receipt.finish(
            "failed",
            "blocked",
            blockers,
            source_retained=True,
            incomplete_staging_retained=staging.exists(),
            staging=str(staging),
            retention_performed=False,
        )
        return 1, {
            "backup": final.name,
            "destination": str(final),
            "receipt": str(receipt.path),
            "staging": str(staging),
            "blockers": blockers,
            "retention_performed": False,
            "result": "blocked",
        }
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.umask(previous_umask)
