#!/usr/bin/env python3
"""Produce an archive-v3 independent-backup receipt from physical evidence.

The production CLI accepts roots, not caller assertions.  It derives volume
identities, hashes every physical file in the source, backup, and isolated
restore trees, requires the three inventories to match, and only then writes
the owner-private receipt consumed by archive-v3 retention.
"""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable

try:
    from . import external_volume_guard
except ImportError:  # Direct script execution places this directory on sys.path.
    import external_volume_guard


SCHEMA = "openclaw.independent_backup_receipt.v1"
EVIDENCE_SCHEMA = "openclaw.independent_backup_evidence.v1"
PRODUCER = "scripts/openclaw_independent_backup_receipt.py"
DEFAULT_SOURCE_ROOT = Path(
    (str(OPERATOR.require_path('paths.archive_root')))
)
DEFAULT_OUTPUT = Path(
    (str(OPERATOR.require_path('paths.workspace')) + '/artifacts/openclaw_archive_v3_retention/independent-backup-receipt.json')
)
MAX_RECEIPT_AGE = timedelta(days=8)
MAX_FUTURE_SKEW = timedelta(minutes=5)
SHA256_CHUNK = 8 * 1024 * 1024


class IndependentBackupEvidenceError(RuntimeError):
    """The supplied physical evidence cannot authorize a receipt."""


@dataclass(frozen=True)
class VolumeIdentity:
    volume_uuid: str
    device: int
    mount: str


@dataclass(frozen=True)
class TreeInventory:
    root: str
    volume_uuid: str
    device: int
    mount: str
    inventory_sha256: str
    entry_count: int
    file_count: int
    directory_count: int
    total_bytes: int


def require(condition: bool, message: str) -> None:
    if not condition:
        raise IndependentBackupEvidenceError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        require(
            stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
            f"inventory member is not one physical regular file: {path}",
        )
        while True:
            block = os.read(descriptor, SHA256_CHUNK)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        require(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
            f"inventory member changed during readback: {path}",
        )
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _mount_point_for_path(path: Path) -> Path:
    """Find the containing mounted filesystem without invoking admin tools."""

    current = Path(path).resolve(strict=True)
    if not current.is_dir():
        current = current.parent
    while not os.path.ismount(current):
        parent = current.parent
        require(parent != current, f"evidence volume mount is unavailable: {path}")
        current = parent
    return current


def _vfs_volume_identity(path: Path) -> VolumeIdentity:
    mount = _mount_point_for_path(path)
    try:
        metadata = external_volume_guard.volume_metadata(mount)
    except Exception as exc:
        raise IndependentBackupEvidenceError(
            "VFS volume identity probe failed"
        ) from exc
    require(
        metadata.get("available") is True and metadata.get("mounted") is True,
        "VFS volume identity is unavailable: {}".format(
            metadata.get("reason") or metadata.get("error") or "unknown"
        ),
    )
    volume_uuid = str(metadata.get("volumeUuid") or "").strip().upper()
    mount_point = str(metadata.get("mountPoint") or "").strip()
    mount_device = metadata.get("mountDevice")
    require(bool(volume_uuid), "evidence volume UUID is unavailable")
    require(bool(mount_point), "evidence volume mount is unavailable")
    require(isinstance(mount_device, int), "evidence volume device is unavailable")
    require(
        Path(os.path.abspath(mount_point)) == Path(os.path.abspath(mount)),
        "evidence volume mount changed during identity probe",
    )
    require(
        path.stat().st_dev == mount_device,
        "evidence path device does not match VFS volume identity",
    )
    return VolumeIdentity(
        volume_uuid=volume_uuid,
        device=mount_device,
        mount=str(mount),
    )


def require_physical_root(path: Path) -> tuple[Path, os.stat_result]:
    requested = Path(os.path.abspath(path))
    require(os.path.lexists(requested), f"evidence root is missing: {requested}")
    absolute = requested.resolve(strict=True)
    info = absolute.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and not absolute.is_symlink(),
        f"evidence root is not a physical directory: {absolute}",
    )
    return absolute, info


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def build_tree_inventory(
    root: Path,
    *,
    identity_reader: Callable[[Path], VolumeIdentity] = _vfs_volume_identity,
) -> TreeInventory:
    absolute, root_info = require_physical_root(root)
    identity = identity_reader(absolute)
    require(
        identity.device == root_info.st_dev,
        f"evidence root device does not match derived volume identity: {absolute}",
    )
    rows: list[dict[str, Any]] = []
    file_count = 0
    directory_count = 0
    total_bytes = 0

    def walk(directory: Path, relative: Path) -> None:
        nonlocal file_count, directory_count, total_bytes
        try:
            children = sorted(os.scandir(directory), key=lambda row: row.name)
        except OSError as exc:
            raise IndependentBackupEvidenceError(
                f"evidence directory cannot be read: {directory}"
            ) from exc
        for child in children:
            path = Path(child.path)
            member_relative = relative / child.name
            info = child.stat(follow_symlinks=False)
            require(
                info.st_dev == identity.device,
                f"evidence tree crosses a physical device: {path}",
            )
            require(not stat.S_ISLNK(info.st_mode), f"evidence tree contains a symlink: {path}")
            if stat.S_ISDIR(info.st_mode):
                directory_count += 1
                rows.append(
                    {
                        "path": member_relative.as_posix(),
                        "type": "directory",
                        "mode": stat.S_IMODE(info.st_mode),
                    }
                )
                walk(path, member_relative)
            elif stat.S_ISREG(info.st_mode):
                file_count += 1
                total_bytes += info.st_size
                rows.append(
                    {
                        "path": member_relative.as_posix(),
                        "type": "file",
                        "mode": stat.S_IMODE(info.st_mode),
                        "size": info.st_size,
                        "sha256": sha256_file(path),
                    }
                )
            else:
                raise IndependentBackupEvidenceError(
                    f"evidence tree contains an unsupported member: {path}"
                )

    walk(absolute, Path())
    require(rows, f"evidence tree is empty: {absolute}")
    return TreeInventory(
        root=str(absolute),
        volume_uuid=identity.volume_uuid.upper(),
        device=identity.device,
        mount=identity.mount,
        inventory_sha256=hashlib.sha256(canonical_json(rows)).hexdigest(),
        entry_count=len(rows),
        file_count=file_count,
        directory_count=directory_count,
        total_bytes=total_bytes,
    )


def atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_info = path.parent.lstat()
    require(
        stat.S_ISDIR(parent_info.st_mode)
        and not path.parent.is_symlink()
        and parent_info.st_uid == os.geteuid(),
        "receipt output parent is not an owner-controlled physical directory",
    )
    if os.path.lexists(path):
        existing = path.lstat()
        require(
            stat.S_ISREG(existing.st_mode)
            and not path.is_symlink()
            and existing.st_uid == os.geteuid()
            and existing.st_nlink == 1
            and stat.S_IMODE(existing.st_mode) == 0o600,
            "existing receipt output is not an owner-private physical file",
        )
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def produce_receipt(
    *,
    source_weekly_root: Path,
    backup_weekly_root: Path,
    isolated_restore_weekly_root: Path,
    output: Path,
    identity_reader: Callable[[Path], VolumeIdentity] = _vfs_volume_identity,
    inventory_builder: Callable[..., TreeInventory] = build_tree_inventory,
    now: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    source_root, _ = require_physical_root(source_weekly_root)
    backup_root, _ = require_physical_root(backup_weekly_root)
    restore_root, _ = require_physical_root(isolated_restore_weekly_root)
    for left, right, label in (
        (source_root, backup_root, "source and backup roots overlap"),
        (source_root, restore_root, "source and restore roots overlap"),
        (backup_root, restore_root, "backup and restore roots overlap"),
    ):
        require(
            not _is_relative_to(left, right) and not _is_relative_to(right, left),
            label,
        )

    source = inventory_builder(source_root, identity_reader=identity_reader)
    backup = inventory_builder(backup_root, identity_reader=identity_reader)
    restore = inventory_builder(restore_root, identity_reader=identity_reader)
    require(
        source.device != backup.device,
        "backup evidence is on the same device as the source",
    )
    require(
        source.volume_uuid != backup.volume_uuid,
        "backup evidence volume UUID matches the source",
    )
    require(
        source.inventory_sha256 == backup.inventory_sha256 == restore.inventory_sha256,
        "source, backup, and isolated restore inventories do not match",
    )
    require(
        source.entry_count == backup.entry_count == restore.entry_count
        and source.file_count == backup.file_count == restore.file_count
        and source.total_bytes == backup.total_bytes == restore.total_bytes,
        "source, backup, and isolated restore inventory metrics do not match",
    )

    evidence = {
        "schema_version": EVIDENCE_SCHEMA,
        "source": asdict(source),
        "backup": asdict(backup),
        "isolated_restore": asdict(restore),
    }
    evidence_sha256 = hashlib.sha256(canonical_json(evidence)).hexdigest()
    payload: dict[str, Any] = {
        "schema_version": SCHEMA,
        "producer": PRODUCER,
        "verified": True,
        "independent_backup": True,
        "source_volume_uuid": source.volume_uuid,
        "source_device": source.device,
        "source_role": "openclaw_primary",
        "backup_volume_uuid": backup.volume_uuid,
        "backup_device": backup.device,
        "backup_role": "independent_recovery",
        "restore_volume_uuid": restore.volume_uuid,
        "restore_device": restore.device,
        "restore_role": "isolated_readback",
        "covered_weekly_root": str(source_root),
        "verified_at_utc": now(),
        "source_inventory_sha256": source.inventory_sha256,
        "backup_inventory_sha256": backup.inventory_sha256,
        "restore_inventory_sha256": restore.inventory_sha256,
        "evidence_sha256": evidence_sha256,
        "evidence": evidence,
    }
    output_absolute = Path(os.path.abspath(output))
    require(
        not any(
            _is_relative_to(output_absolute, root)
            for root in (source_root, backup_root, restore_root)
        ),
        "receipt output must remain outside every evidence tree",
    )
    atomic_write_private_json(output_absolute, payload)
    info = output_absolute.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and not output_absolute.is_symlink()
        and info.st_uid == os.geteuid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600,
        "receipt output is not an owner-private physical file",
    )
    return payload


def _parse_timestamp(value: Any) -> datetime:
    require(isinstance(value, str) and bool(value.strip()), "verification timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IndependentBackupEvidenceError("verification timestamp is invalid") from exc
    require(parsed.tzinfo is not None, "verification timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def validate_receipt_payload(
    payload: dict[str, Any],
    *,
    source_weekly_root: Path,
    expected_source_volume_uuid: str,
    expected_source_device: int,
    identity_reader: Callable[[Path], VolumeIdentity] = _vfs_volume_identity,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    require(
        payload.get("schema_version") == SCHEMA,
        "independent backup receipt schema mismatch",
    )
    require(
        payload.get("producer") == PRODUCER,
        "independent backup receipt producer mismatch",
    )
    require(
        payload.get("verified") is True,
        "independent backup is not verified",
    )
    require(
        payload.get("independent_backup") is True,
        "backup is not independent",
    )
    source_root = Path(source_weekly_root).resolve(strict=True)
    require(
        payload.get("covered_weekly_root") == str(source_root),
        "independent backup receipt does not cover this weekly root",
    )
    source_uuid = str(payload.get("source_volume_uuid") or "").upper()
    backup_uuid = str(payload.get("backup_volume_uuid") or "").upper()
    require(
        source_uuid == expected_source_volume_uuid.upper(),
        "independent backup receipt source UUID mismatch",
    )
    require(
        payload.get("source_device") == expected_source_device,
        "independent backup receipt source device mismatch",
    )
    require(
        payload.get("source_role") == "openclaw_primary",
        "independent backup receipt source role mismatch",
    )
    require(
        bool(backup_uuid) and backup_uuid != source_uuid,
        "independent backup volume UUID is missing or matches the primary volume",
    )
    require(
        isinstance(payload.get("backup_device"), int)
        and payload["backup_device"] != expected_source_device,
        "independent backup device is missing or matches the primary device",
    )
    require(
        payload.get("backup_role") == "independent_recovery",
        "independent backup receipt backup role mismatch",
    )
    require(
        payload.get("restore_role") == "isolated_readback",
        "isolated restore role mismatch",
    )
    verified_at = _parse_timestamp(payload.get("verified_at_utc"))
    age = now().astimezone(timezone.utc) - verified_at
    require(age <= MAX_RECEIPT_AGE, "independent backup receipt is stale")
    require(
        age >= -MAX_FUTURE_SKEW,
        "independent backup receipt timestamp is in the future",
    )

    evidence = payload.get("evidence")
    require(
        isinstance(evidence, dict)
        and evidence.get("schema_version") == EVIDENCE_SCHEMA,
        "independent backup evidence schema mismatch",
    )
    require(
        hashlib.sha256(canonical_json(evidence)).hexdigest()
        == payload.get("evidence_sha256"),
        "independent backup evidence digest mismatch",
    )
    source_evidence = evidence.get("source")
    backup_evidence = evidence.get("backup")
    restore_evidence = evidence.get("isolated_restore")
    require(
        all(
            isinstance(row, dict)
            for row in (
                source_evidence,
                backup_evidence,
                restore_evidence,
            )
        ),
        "independent backup evidence identities are incomplete",
    )
    assert isinstance(source_evidence, dict)
    assert isinstance(backup_evidence, dict)
    assert isinstance(restore_evidence, dict)
    for field, row in (
        ("source_inventory_sha256", source_evidence),
        ("backup_inventory_sha256", backup_evidence),
        ("restore_inventory_sha256", restore_evidence),
    ):
        digest = str(payload.get(field) or "")
        require(
            len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest),
            f"{field} is invalid",
        )
        require(
            row.get("inventory_sha256") == digest,
            f"{field} does not match evidence",
        )
    require(
        payload["source_inventory_sha256"]
        == payload["backup_inventory_sha256"]
        == payload["restore_inventory_sha256"],
        "independent backup evidence inventories do not match",
    )
    require(
        source_evidence.get("root") == str(source_root),
        "source evidence root mismatch",
    )
    require(
        source_evidence.get("volume_uuid") == source_uuid,
        "source evidence UUID mismatch",
    )
    require(
        source_evidence.get("device") == expected_source_device,
        "source evidence device mismatch",
    )
    require(
        backup_evidence.get("volume_uuid") == backup_uuid,
        "backup evidence UUID mismatch",
    )
    require(
        backup_evidence.get("device") == payload.get("backup_device"),
        "backup evidence device mismatch",
    )
    require(
        restore_evidence.get("volume_uuid")
        == str(payload.get("restore_volume_uuid") or "").upper(),
        "restore evidence UUID mismatch",
    )
    require(
        restore_evidence.get("device") == payload.get("restore_device"),
        "restore evidence device mismatch",
    )

    live_source = build_tree_inventory(
        source_root,
        identity_reader=identity_reader,
    )
    require(
        live_source.volume_uuid == source_uuid,
        "live source volume UUID changed",
    )
    require(
        live_source.device == expected_source_device,
        "live source device changed",
    )
    require(
        live_source.inventory_sha256 == payload["source_inventory_sha256"],
        "live weekly inventory changed after independent-backup verification",
    )
    return {
        "verified_at_utc": payload["verified_at_utc"],
        "backup_volume_uuid": backup_uuid,
        "backup_device": payload["backup_device"],
        "source_inventory_sha256": payload["source_inventory_sha256"],
        "backup_inventory_sha256": payload["backup_inventory_sha256"],
        "restore_inventory_sha256": payload["restore_inventory_sha256"],
        "evidence_sha256": payload["evidence_sha256"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source-weekly-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--backup-weekly-root", type=Path, required=True)
    parser.add_argument("--isolated-restore-weekly-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = produce_receipt(
            source_weekly_root=args.source_weekly_root,
            backup_weekly_root=args.backup_weekly_root,
            isolated_restore_weekly_root=args.isolated_restore_weekly_root,
            output=args.output,
        )
    except IndependentBackupEvidenceError as exc:
        print("INDEPENDENT_BACKUP_RECEIPT_BLOCKED")
        print(
            "STATUS | result: blocked_missing_external_backup_evidence | "
            f"blocker: {exc} | output_written: false"
        )
        return 1
    print("INDEPENDENT_BACKUP_RECEIPT_OK")
    print(
        "STATUS | result: verified | output: {} | evidence_sha256: {} | "
        "source_inventory_sha256: {}".format(
            args.output,
            payload["evidence_sha256"],
            payload["source_inventory_sha256"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
