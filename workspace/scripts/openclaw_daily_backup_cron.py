#!/usr/bin/env python3
"""Verify retained clone-v2 recovery proofs.

Clone-v2 creation is retired from the supported CLI. This module retains its
full verifier and retention parser for existing historical generations. Current
local creation is owned by openclaw_weekly_archive_backup.py and its native-v4
archive/restore contract. Do not use the retained create_backup compatibility
function for new production generations; it supports historical fixture readback.
"""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Iterable, Iterator

try:
    from . import external_volume_guard
except ImportError:  # Direct script execution places this directory on sys.path.
    import external_volume_guard


SCHEMA = "openclaw.weekly_backup.phase_receipt.v2"
LEGACY_MANIFEST_SCHEMA = "openclaw.owc_weekly_backup.manifest.v1"
MANIFEST_SCHEMA = "openclaw.owc_weekly_backup.manifest.v2"
SUPPORTED_MANIFEST_SCHEMAS = frozenset({LEGACY_MANIFEST_SCHEMA, MANIFEST_SCHEMA})
RETENTION_SCHEMA = "openclaw.owc_weekly_backup.retention.v1"
CAPTURE_CONSISTENCY = "destination-self-consistent-source-tree-non-atomic-v1"
INVENTORY_ORIGIN = "private-staging-copy-after-clone"
OWC_ROOT = Path(os.environ.get("OPENCLAW_OWC_ROOT", (str(OPERATOR.require_path('paths.agent_storage_root')))))
OWC_VOLUME_MOUNT = Path(
    os.environ.get("OPENCLAW_OWC_VOLUME_MOUNT", str(OWC_ROOT.parent))
).expanduser()
EXPECTED_VOLUME_UUID = os.environ.get("OPENCLAW_OWC_VOLUME_UUID", OPERATOR.require_string('volumes.expected_uuid'))
def _resolve_expected_device() -> int:
    """Resolve the current mount device; UUID remains the durable identity check."""
    override = os.environ.get("OPENCLAW_OWC_DEVICE")
    if override:
        return int(override)
    try:
        return os.stat(OWC_VOLUME_MOUNT).st_dev
    except OSError:
        return -1  # An absent configured mount cannot match an admissible device.


EXPECTED_DEVICE = _resolve_expected_device()
BACKUP_ROOT = Path(
    os.environ.get("OPENCLAW_WEEKLY_BACKUP_ROOT", str(OWC_ROOT / "Backups/weekly"))
).expanduser()
RETENTION_COUNT = int(os.environ.get("OPENCLAW_WEEKLY_RETENTION_COUNT", "2"))
RECEIPT_DIR = Path(
    os.environ.get(
        "OPENCLAW_BACKUP_RECEIPT_DIR",
        (str(OPERATOR.require_path('paths.workspace')) + '/artifacts/openclaw_weekly_backup'),
    )
).expanduser()
SCHEDULER_STORE = Path(
    os.environ.get("OPENCLAW_SCHEDULER_STORE", (str(OPERATOR.require_path('paths.host_home')) + '/.openclaw/cron/jobs.json'))
).expanduser()
CP = Path("/bin/cp")
BACKUP_NAME_RE = re.compile(r"^openclaw-backup-(\d{8}T\d{6}Z)$")
INCOMPLETE_NAME_RE = re.compile(r"^\.openclaw-backup-(\d{8}T\d{6}Z)\.incomplete-\d+$")
MAX_FUTURE_GENERATION_SKEW_SECONDS = 300
SYMLINK_OWNER_POLICY = "root-source-to-backup-owner-v1"
ANNOUNCE_SUCCESS = os.environ.get("OPENCLAW_BACKUP_ANNOUNCE_SUCCESS", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


class BackupError(RuntimeError):
    """A backup prerequisite or verification invariant failed."""


@dataclass(frozen=True)
class VolumeIdentity:
    uuid: str
    device: int
    mount: Path
    writable: bool
    owners_enabled: bool


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    source: Path
    destination_relative: Path
    category: str
    required: bool = True


def main_sessions_source_paths() -> tuple[Path, Path]:
    """Return the only two session-store paths admitted by Map V1 history."""

    state_root = OWC_ROOT / ".state/OpenClaw"
    return (
        state_root / "Sessions",
        state_root / "agents/main/sessions",
    )


def resolve_main_sessions_source() -> Path:
    """Select the physical session tree from either sanctioned topology.

    Before beta.3 normalization, ``Sessions`` is the physical directory and
    ``agents/main/sessions`` is its absolute symlink. After normalization the
    lowercase agent path is physical and no top-level legacy path exists. No
    mixed or third layout is an admissible backup source.
    """

    legacy, current = main_sessions_source_paths()
    require_physical_directory_chain(
        current.parent,
        OWC_ROOT,
        expected_device=EXPECTED_DEVICE,
    )
    try:
        current_info = current.lstat()
    except OSError as exc:
        raise BackupError("main sessions source topology is incomplete") from exc
    try:
        legacy_info: os.stat_result | None = legacy.lstat()
    except FileNotFoundError:
        legacy_info = None
    except OSError as exc:
        raise BackupError("main sessions source topology is incomplete") from exc

    def is_physical_directory(path: Path, info: os.stat_result) -> bool:
        return stat.S_ISDIR(info.st_mode) and not path.is_symlink()

    def is_exact_alias(
        path: Path,
        info: os.stat_result,
        target: Path,
    ) -> bool:
        if not stat.S_ISLNK(info.st_mode):
            return False
        try:
            raw_target = Path(os.readlink(path))
            resolved = path.resolve(strict=True)
        except OSError:
            return False
        return (
            raw_target.is_absolute()
            and raw_target == target
            and resolved == target.resolve(strict=True)
        )

    if (
        legacy_info is not None
        and is_physical_directory(legacy, legacy_info)
        and is_exact_alias(
            current,
            current_info,
            legacy,
        )
    ):
        return legacy
    if legacy_info is None and is_physical_directory(current, current_info):
        return current
    raise BackupError("main sessions source topology is not sanctioned")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{}.{}.tmp".format(path.name, os.getpid()))
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_dir(path.parent)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BackupError(message)


def is_within(path: Path, parent: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(parent)))
        return True
    except ValueError:
        return False


def require_physical_directory_chain(
    path: Path,
    root: Path,
    *,
    expected_device: int,
) -> None:
    root_absolute = Path(os.path.abspath(root))
    path_absolute = Path(os.path.abspath(path))
    try:
        relative = path_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise BackupError("path is outside the declared physical root: {}".format(path)) from exc
    current = root_absolute
    for part in (Path(".").parts if relative == Path(".") else relative.parts):
        if part != ".":
            current = current / part
        info = current.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and not current.is_symlink(),
            "backup path has a non-directory or symlink ancestor: {}".format(current),
        )
        require(
            info.st_dev == expected_device,
            "backup path ancestor device mismatch: {}".format(current),
        )


def require_safe_relative(value: str, label: str) -> Path:
    relative = Path(value)
    require(
        value == relative.as_posix()
        and value not in {"", "."}
        and not relative.is_absolute()
        and ".." not in relative.parts,
        "{} is not a safe relative path: {}".format(label, value),
    )
    return relative


def inventory_target(root: Path, relative: str) -> Path:
    require(root.is_dir() and not root.is_symlink(), "inventory root is not a physical directory")
    if relative == ".":
        return root
    rel = require_safe_relative(relative, "inventory path")
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        require(
            current.is_dir() and not current.is_symlink(),
            "inventory path has a non-directory or symlink ancestor: {}".format(current),
        )
    target = root / rel
    require(is_within(target, root), "inventory target escapes backup payload")
    return target


def read_volume_identity() -> VolumeIdentity:
    try:
        metadata = external_volume_guard.volume_metadata(OWC_VOLUME_MOUNT)
    except Exception as exc:
        raise BackupError("VFS volume identity probe failed") from exc
    require(
        metadata.get("available") is True and metadata.get("mounted") is True,
        "VFS volume identity failed: {}".format(
            metadata.get("reason") or metadata.get("error") or "unavailable"
        ),
    )
    mount_device = metadata.get("mountDevice")
    volume_uuid = metadata.get("volumeUuid")
    mount_point = metadata.get("mountPoint")
    read_only = metadata.get("readOnly")
    owners_enabled = metadata.get("ownersEnabled")
    require(isinstance(mount_device, int), "VFS volume identity has no stable device")
    require(
        isinstance(volume_uuid, str) and bool(volume_uuid.strip()),
        "VFS volume identity has no UUID",
    )
    require(
        isinstance(mount_point, str) and bool(mount_point.strip()),
        "VFS volume identity has no mount point",
    )
    require(isinstance(read_only, bool), "VFS volume read-only state is unavailable")
    require(isinstance(owners_enabled, bool), "VFS volume ownership state is unavailable")
    return VolumeIdentity(
        uuid=volume_uuid.strip().upper(),
        device=mount_device,
        mount=Path(mount_point),
        writable=not read_only,
        owners_enabled=owners_enabled,
    )


def declared_source_specs(
    *,
    include_optional: bool,
    main_sessions_source: Path | None = None,
) -> list[SourceSpec]:
    selected_sessions = main_sessions_source or resolve_main_sessions_source()
    require(
        selected_sessions in main_sessions_source_paths(),
        "main sessions source is outside the sanctioned path set",
    )
    rows = [
        SourceSpec("workspace", OWC_ROOT / "Workspace", Path("roots/workspace"), "map-root"),
        SourceSpec("personal-data", OWC_ROOT / "Projects/PersonalData", Path("roots/personal-data"), "map-root"),
        SourceSpec("working-repositories", OWC_ROOT / "Repositories/Working", Path("roots/working-repositories"), "map-root"),
        SourceSpec("bare-remotes", OWC_ROOT / "Repositories/Bare", Path("roots/bare-remotes"), "map-root"),
        SourceSpec("runtime-releases", OWC_ROOT / ".runtime/Releases", Path("roots/runtime-releases"), "map-root"),
        SourceSpec("main-sessions", selected_sessions, Path("roots/main-sessions"), "map-root"),
        SourceSpec("managed-browser", OWC_ROOT / ".state/OpenClaw/Browser", Path("roots/managed-browser"), "map-root"),
        SourceSpec("media", OWC_ROOT / ".state/OpenClaw/Media", Path("roots/media"), "map-root"),
        SourceSpec("archive", OWC_ROOT / ".archive", Path("technical/archive"), "technical"),
        SourceSpec("quarantine", OWC_ROOT / ".quarantine", Path("technical/quarantine"), "technical"),
        SourceSpec("cache", OWC_ROOT / ".cache", Path("technical/cache"), "technical"),
        SourceSpec("scratch", OWC_ROOT / ".scratch", Path("technical/scratch"), "technical"),
        SourceSpec("build", OWC_ROOT / ".build", Path("technical/build"), "technical"),
        SourceSpec("manifests", OWC_ROOT / ".manifests", Path("technical/manifests"), "technical"),
    ]
    if include_optional:
        rows.append(
            SourceSpec(
                "migration-review",
                OWC_ROOT / ".migration-review",
                Path("technical/migration-review"),
                "classified-residual",
                required=False,
            )
        )
    return rows


def map_source_specs() -> list[SourceSpec]:
    return declared_source_specs(include_optional=(OWC_ROOT / ".migration-review").exists())


def validate_backup_environment(*, create_root: bool, allow_missing_root: bool = False) -> VolumeIdentity:
    require(RETENTION_COUNT >= 2, "OPENCLAW_WEEKLY_RETENTION_COUNT must be at least 2")
    identity = read_volume_identity()
    require(identity.uuid == EXPECTED_VOLUME_UUID, "OWC volume UUID mismatch")
    require(identity.device == EXPECTED_DEVICE, "OWC device mismatch")
    require(str(identity.mount), "OWC mount point is missing")
    require(
        Path(os.path.abspath(identity.mount)) == Path(os.path.abspath(OWC_VOLUME_MOUNT)),
        "OWC mount point mismatch",
    )
    require(OWC_ROOT.stat().st_dev == identity.device, "OWC root is not on the pinned device")
    require(identity.writable, "OWC volume is read-only")
    require(identity.owners_enabled, "OWC ownership is disabled")
    require(OWC_ROOT.is_dir() and not OWC_ROOT.is_symlink(), "canonical OWC root is not a physical directory")
    expected_backup_root = OWC_ROOT / "Backups/weekly"
    require(
        Path(os.path.abspath(BACKUP_ROOT)) == Path(os.path.abspath(expected_backup_root)),
        "weekly backup root is not the exact OWC Backups/weekly path",
    )
    backups_parent = OWC_ROOT / "Backups"
    if create_root:
        backups_parent.mkdir(mode=0o700, exist_ok=True)
        require(
            backups_parent.is_dir() and not backups_parent.is_symlink(),
            "OWC Backups parent is not a physical directory",
        )
        BACKUP_ROOT.mkdir(mode=0o700, exist_ok=True)
    else:
        require(
            backups_parent.is_dir() and not backups_parent.is_symlink(),
            "OWC Backups parent is not a physical directory",
        )
    if BACKUP_ROOT.exists():
        require(
            BACKUP_ROOT.is_dir() and not BACKUP_ROOT.is_symlink(),
            "weekly backup root is not a physical directory",
        )
        require(BACKUP_ROOT.stat().st_dev == identity.device, "weekly backup root device mismatch")
    else:
        require(allow_missing_root, "weekly backup root is missing")
    return identity


def validate_preflight(
    specs: Iterable[SourceSpec],
    *,
    create_backup_root: bool = False,
) -> dict[str, Any]:
    identity = validate_backup_environment(
        create_root=create_backup_root,
        allow_missing_root=not create_backup_root,
    )

    seen_destinations: set[str] = set()
    rows: list[dict[str, Any]] = []
    for spec in specs:
        if not spec.source.exists() and not spec.required:
            continue
        require_physical_directory_chain(
            spec.source,
            OWC_ROOT,
            expected_device=identity.device,
        )
        require(spec.source.is_dir() and not spec.source.is_symlink(), "backup source is not a physical directory: {}".format(spec.source))
        info = spec.source.stat()
        require(info.st_dev == identity.device, "backup source device mismatch: {}".format(spec.source))
        require(not is_within(BACKUP_ROOT, spec.source), "backup destination would recurse into source: {}".format(spec.source))
        destination_key = str(spec.destination_relative).casefold()
        require(destination_key not in seen_destinations, "duplicate backup destination: {}".format(spec.destination_relative))
        seen_destinations.add(destination_key)
        rows.append(
            {
                "source_id": spec.source_id,
                "source": str(spec.source),
                "destination_relative": str(spec.destination_relative),
                "category": spec.category,
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
            }
        )
    require(len([row for row in rows if row["category"] == "map-root"]) == 8, "backup preflight does not cover all eight Map V1 roots")
    require(len([row for row in rows if row["category"] == "technical"]) == 6, "backup preflight does not cover the required technical namespaces")
    require(not any(row["source"].endswith("/Backups") for row in rows), "Backups namespace must be excluded from recursive backup")
    return {
        "volume": {
            "uuid": identity.uuid,
            "device": identity.device,
            "mount": str(identity.mount),
            "writable": identity.writable,
            "owners_enabled": identity.owners_enabled,
        },
        "sources": rows,
        "capture_consistency": CAPTURE_CONSISTENCY,
        "source_tree_atomic": False,
        "inventory_origin": INVENTORY_ORIGIN,
        "retention_count": RETENTION_COUNT,
        "backup_root": str(BACKUP_ROOT),
        "same_device_local_recovery": True,
        "independent_disaster_recovery": False,
        "explicit_exclusions": [
            str(OWC_ROOT / "Backups"),
            (str(OPERATOR.require_path('paths.agent_storage_root')) + 'Storage'),
            (str(OPERATOR.require_path('paths.host_home')) + '/.codex/sessions'),
            "credentials",
            "configuration database",
            "task database",
            "logs",
            "LaunchAgents",
            "CLI",
            "Node runtime",
            "iCloud",
        ],
    }


def entry_kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    raise BackupError("unsupported backup entry mode: {:o}".format(mode))


def iter_tree(
    root: Path,
    *,
    expected_device: int | None = None,
) -> Iterator[tuple[Path, str]]:
    root_info = root.lstat()
    if expected_device is not None:
        require(
            root_info.st_dev == expected_device,
            "backup tree root device mismatch: {}".format(root),
        )
    yield root, "."
    stack: list[tuple[Path, Path]] = [(root, Path("."))]
    while stack:
        directory, relative = stack.pop()
        children = sorted(os.scandir(directory), key=lambda row: os.fsencode(row.name), reverse=True)
        for child in children:
            child_path = Path(child.path)
            child_relative = relative / child.name
            info = child_path.lstat()
            if expected_device is not None and not stat.S_ISLNK(info.st_mode):
                require(
                    info.st_dev == expected_device,
                    "backup tree crossed a device boundary: {}".format(child_path),
                )
            yield child_path, str(child_relative)
            if stat.S_ISDIR(info.st_mode):
                stack.append((child_path, child_relative))


def validate_tree_device(root: Path, *, expected_device: int) -> None:
    for _path, _relative in iter_tree(root, expected_device=expected_device):
        pass


def inventory_tree(
    root: Path,
    output: Path,
    *,
    symlink_source_provenance: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(".{}.{}.tmp".format(output.name, os.getpid()))
    entries = 0
    files = 0
    directories = 0
    symlinks = 0
    content_bytes = 0
    with temporary.open("w", encoding="utf-8") as handle:
        for path, relative in iter_tree(root, expected_device=EXPECTED_DEVICE):
            info = path.lstat()
            kind = entry_kind(info.st_mode)
            record: dict[str, Any] = {
                "path": relative,
                "kind": kind,
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "mtime_ns": info.st_mtime_ns,
            }
            if kind == "file":
                record["size"] = info.st_size
                record["sha256"] = sha256_file(path)
                files += 1
                content_bytes += info.st_size
            elif kind == "directory":
                directories += 1
            else:
                raw_target = os.readlink(path)
                record["raw_target"] = raw_target
                if symlink_source_provenance is not None:
                    require(
                        relative in symlink_source_provenance,
                        "backup captured an unobserved source symlink: {}".format(path),
                    )
                    provenance = symlink_source_provenance[relative]
                    require(
                        provenance.get("raw_target") == raw_target,
                        "backup source symlink target changed during clone: {}".format(path),
                    )
                    record["source_uid_observed"] = int(provenance["uid"])
                    record["source_raw_target_observed"] = str(provenance["raw_target"])
                symlinks += 1
            handle.write(canonical_json(record).decode("utf-8") + "\n")
            entries += 1
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)
    fsync_dir(output.parent)
    return {
        "path": str(output.name),
        "sha256": sha256_file(output),
        "entries": entries,
        "files": files,
        "directories": directories,
        "symlinks": symlinks,
        "content_bytes": content_bytes,
    }


def load_inventory(path: Path) -> list[dict[str, Any]]:
    return list(iter_inventory(path))


def iter_inventory(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            payload = json.loads(raw)
            require(isinstance(payload, dict), "inventory row is not an object: {}:{}".format(path, line_number))
            yield payload


def verify_inventory(
    root: Path,
    inventory: Path,
    expected: dict[str, Any],
    *,
    full_hash: bool,
    manifest_schema: str = MANIFEST_SCHEMA,
    expected_device: int | None = None,
    backup_owner_uid: int | None = None,
) -> dict[str, Any]:
    require(
        manifest_schema in SUPPORTED_MANIFEST_SCHEMAS,
        "unsupported backup manifest schema for inventory verification",
    )
    require(inventory.is_file() and not inventory.is_symlink(), "inventory is missing or is a symlink: {}".format(inventory))
    require(sha256_file(inventory) == expected["sha256"], "inventory digest mismatch: {}".format(inventory))
    entries = 0
    files = 0
    directories = 0
    symlinks = 0
    content_bytes = 0
    restore_candidates: list[dict[str, Any]] = []
    symlink_owner_substitutions: list[dict[str, Any]] = []
    if backup_owner_uid is None:
        backup_owner_uid = BACKUP_ROOT.stat().st_uid
    if expected_device is None:
        expected_device = EXPECTED_DEVICE
    for record, actual in zip_longest(
        iter_inventory(inventory),
        iter_tree(root, expected_device=expected_device),
    ):
        require(record is not None and actual is not None, "backup payload path set does not match its inventory")
        actual_path, actual_relative = actual
        record_relative = str(record.get("path", ""))
        require(record_relative == actual_relative, "backup payload path ordering/set does not match its inventory")
        target = inventory_target(root, record_relative)
        require(target == actual_path, "backup inventory target identity mismatch")
        require(os.path.lexists(target), "backup entry is missing: {}".format(target))
        info = target.lstat()
        kind = entry_kind(info.st_mode)
        require(kind == record.get("kind"), "backup entry type mismatch: {}".format(target))
        require(stat.S_IMODE(info.st_mode) == int(record.get("mode")), "backup entry mode mismatch: {}".format(target))
        recorded_uid = int(record.get("uid"))
        if manifest_schema == LEGACY_MANIFEST_SCHEMA:
            if info.st_uid != recorded_uid:
                require(
                    kind == "symlink"
                    and recorded_uid == 0
                    and info.st_uid == backup_owner_uid,
                    "backup entry uid mismatch: {}".format(target),
                )
                symlink_owner_substitutions.append(
                    {
                        "path": record_relative,
                        "source_uid": recorded_uid,
                        "backup_uid": info.st_uid,
                    }
                )
        else:
            require(info.st_uid == recorded_uid, "backup entry uid mismatch: {}".format(target))
        require(info.st_gid == int(record.get("gid")), "backup entry gid mismatch: {}".format(target))
        require(info.st_mtime_ns == int(record.get("mtime_ns")), "backup entry mtime mismatch: {}".format(target))
        if kind == "file":
            require(info.st_size == int(record.get("size")), "backup file size mismatch: {}".format(target))
            if full_hash:
                require(sha256_file(target) == record.get("sha256"), "backup file checksum mismatch: {}".format(target))
            files += 1
            content_bytes += info.st_size
            if len(restore_candidates) < 4 and info.st_size <= 4 * 1024 * 1024:
                restore_candidates.append(
                    {
                        "path": str(record["path"]),
                        "sha256": str(record["sha256"]),
                        "size": info.st_size,
                    }
                )
        elif kind == "directory":
            directories += 1
        elif kind == "symlink":
            raw_target = os.readlink(target)
            require(raw_target == record.get("raw_target"), "backup symlink target mismatch: {}".format(target))
            if manifest_schema == MANIFEST_SCHEMA:
                require(
                    record.get("source_raw_target_observed") == raw_target,
                    "backup source symlink provenance target mismatch: {}".format(target),
                )
                source_uid = record.get("source_uid_observed")
                require(
                    isinstance(source_uid, int),
                    "backup source symlink provenance uid is missing: {}".format(target),
                )
                if source_uid != info.st_uid:
                    require(
                        source_uid == 0 and info.st_uid == backup_owner_uid,
                        "backup source symlink owner substitution is invalid: {}".format(target),
                    )
                    symlink_owner_substitutions.append(
                        {
                            "path": record_relative,
                            "source_uid": source_uid,
                            "backup_uid": info.st_uid,
                        }
                    )
            symlinks += 1
        entries += 1
    require(entries == int(expected["entries"]), "backup inventory entry-count mismatch")
    require(files == int(expected["files"]), "backup inventory file-count mismatch")
    require(directories == int(expected["directories"]), "backup inventory directory-count mismatch")
    require(symlinks == int(expected["symlinks"]), "backup inventory symlink-count mismatch")
    require(content_bytes == int(expected["content_bytes"]), "backup inventory byte-count mismatch")
    return {
        "entries": entries,
        "files": files,
        "directories": directories,
        "symlinks": symlinks,
        "content_bytes": content_bytes,
        "full_hash": full_hash,
        "restore_candidates": restore_candidates,
        "symlink_ownership": {
            "policy": SYMLINK_OWNER_POLICY,
            "backup_owner_uid": backup_owner_uid,
            "substitution_count": len(symlink_owner_substitutions),
            "substitutions_sha256": sha256_bytes(
                canonical_json(symlink_owner_substitutions)
            ),
        },
        "result": "verified",
    }


def source_tree_preflight(root: Path) -> dict[str, dict[str, Any]]:
    provenance: dict[str, dict[str, Any]] = {}
    for path, relative in iter_tree(root, expected_device=EXPECTED_DEVICE):
        info = path.lstat()
        kind = entry_kind(info.st_mode)
        if kind == "symlink":
            provenance[relative] = {
                "uid": info.st_uid,
                "raw_target": os.readlink(path),
            }
    return provenance


def clone_directory(
    source: Path,
    destination: Path,
    *,
    expected_source_inode: int | None = None,
) -> dict[str, Any]:
    require(not os.path.lexists(destination), "backup clone destination exists: {}".format(destination))
    source_before = source.lstat()
    require(
        stat.S_ISDIR(source_before.st_mode)
        and not source.is_symlink()
        and source_before.st_dev == EXPECTED_DEVICE,
        "backup source root is not the expected physical directory: {}".format(source),
    )
    if expected_source_inode is not None:
        require(
            source_before.st_ino == expected_source_inode,
            "backup source root changed since preflight: {}".format(source),
        )
    symlink_provenance = source_tree_preflight(source)
    capture_started_at_utc = utc_now()
    destination.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(CP), "-c", "-R", "-p", str(source), str(destination)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    require(proc.returncode == 0, "APFS clone failed for {}: {}".format(source, proc.stderr.decode(errors="replace").strip()))
    copy_completed_at_utc = utc_now()
    source_after = source.lstat()
    require(
        stat.S_ISDIR(source_after.st_mode)
        and not source.is_symlink()
        and (source_after.st_dev, source_after.st_ino)
        == (source_before.st_dev, source_before.st_ino),
        "backup source root changed identity during clone: {}".format(source),
    )
    require(destination.is_dir() and not destination.is_symlink(), "APFS clone destination is not a physical directory")
    require(destination.stat().st_dev == source_after.st_dev, "APFS clone crossed devices")
    validate_tree_device(destination, expected_device=EXPECTED_DEVICE)
    return {
        "source_device": source_before.st_dev,
        "source_inode": source_before.st_ino,
        "symlink_source_provenance": symlink_provenance,
        "capture_started_at_utc": capture_started_at_utc,
        "copy_completed_at_utc": copy_completed_at_utc,
    }


def clone_file(source: Path, destination: Path) -> dict[str, Any]:
    source_before = source.lstat()
    require(
        stat.S_ISREG(source_before.st_mode) and not source.is_symlink(),
        "control-state source is missing or is not a physical file: {}".format(source),
    )
    capture_started_at_utc = utc_now()
    destination.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(CP), "-p", str(source), str(destination)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    require(proc.returncode == 0, "control-state copy failed for {}: {}".format(source, proc.stderr.decode(errors="replace").strip()))
    copy_completed_at_utc = utc_now()
    require(
        destination.is_file() and not destination.is_symlink(),
        "control-state destination is missing or is not a physical file",
    )
    source_after = source.lstat()
    destination_payload = json.loads(destination.read_text(encoding="utf-8"))
    require(isinstance(destination_payload, dict), "control-state destination is not a JSON object")
    return {
        "sha256": sha256_file(destination),
        "mode": stat.S_IMODE(destination.stat().st_mode),
        "capture_started_at_utc": capture_started_at_utc,
        "copy_completed_at_utc": copy_completed_at_utc,
        "source_before": {
            "device": source_before.st_dev,
            "inode": source_before.st_ino,
            "size": source_before.st_size,
            "mtime_ns": source_before.st_mtime_ns,
        },
        "source_after": {
            "device": source_after.st_dev,
            "inode": source_after.st_ino,
            "size": source_after.st_size,
            "mtime_ns": source_after.st_mtime_ns,
        },
        "source_changed_during_copy": (
            source_before.st_dev,
            source_before.st_ino,
            source_before.st_size,
            source_before.st_mtime_ns,
        )
        != (
            source_after.st_dev,
            source_after.st_ino,
            source_after.st_size,
            source_after.st_mtime_ns,
        ),
    }


def restore_probe(
    backup_dir: Path,
    source_records: list[dict[str, Any]],
    verification_records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    probe_root = backup_dir / ".restore-probe"
    require(not os.path.lexists(probe_root), "restore probe collision")
    probe_root.mkdir()
    receipts: list[dict[str, Any]] = []
    try:
        for source in source_records:
            source_id = str(source["source_id"])
            verification = verification_records[source_id]
            candidates = verification.get("restore_candidates", [])
            if not candidates:
                receipts.append({"source_id": source_id, "result": "verified-empty-or-no-small-file"})
                continue
            candidate = candidates[0]
            backup_file = backup_dir / "payload" / source["destination_relative"] / candidate["path"]
            restored = probe_root / source_id / Path(candidate["path"]).name
            restored.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [str(CP), "-c", "-p", str(backup_file), str(restored)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            require(proc.returncode == 0, "restore probe copy failed: {}".format(source_id))
            require(sha256_file(restored) == candidate["sha256"], "restore probe checksum mismatch: {}".format(source_id))
            receipts.append(
                {
                    "source_id": source_id,
                    "backup_relative": str(Path(source["destination_relative"]) / candidate["path"]),
                    "size": candidate["size"],
                    "sha256": candidate["sha256"],
                    "result": "verified",
                }
            )
    finally:
        if probe_root.exists():
            shutil.rmtree(probe_root)
            fsync_dir(probe_root.parent)
    return {
        "method": "APFS clone to isolated temporary restore path plus SHA-256 readback",
        "sources_checked": len(receipts),
        "receipts": receipts,
        "result": "verified",
    }


class PhaseReceipt:
    def __init__(self, run_id: str, destination: Path):
        self.path = RECEIPT_DIR / "openclaw-weekly-backup-{}.json".format(run_id)
        self.payload: dict[str, Any] = {
            "schema_version": SCHEMA,
            "run_id": run_id,
            "started_at_utc": utc_now(),
            "updated_at_utc": utc_now(),
            "status": "in_progress",
            "current_phase": "initializing",
            "destination": str(destination),
            "phases": [],
            "blockers": [],
        }
        self.write()

    def write(self) -> None:
        self.payload["updated_at_utc"] = utc_now()
        atomic_write_json(self.path, self.payload)

    def phase(self, name: str, status_value: str, **details: Any) -> None:
        self.payload["current_phase"] = name
        self.payload["phases"].append(
            {
                "name": name,
                "status": status_value,
                "at_utc": utc_now(),
                **details,
            }
        )
        self.write()

    def finish(self, status_value: str, result: str, blockers: list[str], **details: Any) -> None:
        self.payload.update(
            {
                "status": status_value,
                "result": result,
                "blockers": blockers,
                "current_phase": "terminal",
                "completed_at_utc": utc_now(),
                **details,
            }
        )
        self.write()


def load_manifest(backup_dir: Path) -> dict[str, Any]:
    path = backup_dir / "MANIFEST.json"
    require(path.is_file() and not path.is_symlink(), "backup manifest is missing or is a symlink: {}".format(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(payload, dict), "backup manifest is not an object")
    manifest_schema = payload.get("schema_version")
    require(manifest_schema in SUPPORTED_MANIFEST_SCHEMAS, "backup manifest schema mismatch")
    require(payload.get("status") == "verified", "backup manifest is not verified")
    require(payload.get("backup_name") == backup_dir.name, "backup manifest name mismatch")
    require(payload.get("destination") == str(backup_dir), "backup manifest destination mismatch")
    require(payload.get("icloud_offload") is False, "backup manifest unexpectedly enables iCloud")
    require(payload.get("same_device_local_recovery") is True, "backup manifest local-recovery classification mismatch")
    require(payload.get("independent_disaster_recovery") is False, "backup manifest disaster-recovery classification mismatch")
    if manifest_schema == MANIFEST_SCHEMA:
        require(
            payload.get("capture_consistency") == CAPTURE_CONSISTENCY,
            "backup manifest capture consistency is invalid",
        )
        require(
            payload.get("source_tree_atomic") is False,
            "backup manifest source-tree atomicity classification is invalid",
        )
    require(int(payload.get("retention_count", 0)) >= 2, "backup manifest retention floor is below two")
    expected_payload_sha256 = payload.get("manifest_payload_sha256")
    payload_without_digest = dict(payload)
    payload_without_digest.pop("manifest_payload_sha256", None)
    require(
        expected_payload_sha256 == sha256_bytes(canonical_json(payload_without_digest)),
        "backup manifest payload digest mismatch",
    )
    return payload


def validate_manifest_sources(
    source_records: Any,
    *,
    manifest_schema: str,
) -> list[dict[str, Any]]:
    require(isinstance(source_records, list), "backup manifest sources is not a list")
    legacy_sessions, _current_sessions = main_sessions_source_paths()
    expected = {
        spec.source_id: spec
        for spec in declared_source_specs(
            include_optional=True,
            main_sessions_source=legacy_sessions,
        )
    }
    required_ids = {
        source_id
        for source_id, spec in expected.items()
        if spec.required
    }
    source_ids = [str(row.get("source_id", "")) for row in source_records if isinstance(row, dict)]
    require(len(source_ids) == len(source_records), "backup manifest source entry is not an object")
    require(len(source_ids) == len(set(source_ids)), "backup manifest contains duplicate source ids")
    require(set(source_ids) >= required_ids, "backup manifest is missing a required source id")
    require(set(source_ids) <= set(expected), "backup manifest contains an unknown source id")

    manifest_source_device = int(source_records[0].get("source_device", -1)) if source_records else -1

    for source in source_records:
        source_id = str(source["source_id"])
        spec = expected[source_id]
        allowed_source_paths = (
            {str(path) for path in main_sessions_source_paths()}
            if source_id == "main-sessions"
            else {str(spec.source)}
        )
        require(
            source.get("source") in allowed_source_paths,
            "backup manifest source path mismatch: {}".format(source_id),
        )
        require(
            source.get("destination_relative") == spec.destination_relative.as_posix(),
            "backup manifest destination mismatch: {}".format(source_id),
        )
        require(source.get("category") == spec.category, "backup manifest category mismatch: {}".format(source_id))
        # source_device is capture-time provenance, not a live invariant. It is a
        # mount-assigned number frozen when the generation was written, so a
        # frozen generation cannot be required to match today's mount without
        # making every published generation unverifiable after the first
        # remount. What must still hold is that one generation captured exactly
        # one device; volume identity itself is gated by EXPECTED_VOLUME_UUID.
        require(int(source.get("source_device", -1)) > 0, "backup manifest source device is missing: {}".format(source_id))
        require(
            int(source.get("source_device", -1)) == manifest_source_device,
            "backup manifest mixes source devices: {}".format(source_id),
        )
        inventory = source.get("inventory")
        require(isinstance(inventory, dict), "backup manifest inventory is missing: {}".format(source_id))
        if manifest_schema == MANIFEST_SCHEMA:
            require(
                source.get("capture_consistency") == CAPTURE_CONSISTENCY,
                "backup manifest capture consistency mismatch: {}".format(source_id),
            )
            require(
                source.get("source_tree_atomic") is False,
                "backup manifest source-tree atomicity mismatch: {}".format(source_id),
            )
            require(
                source.get("inventory_origin") == INVENTORY_ORIGIN,
                "backup manifest inventory origin mismatch: {}".format(source_id),
            )
        require(
            inventory.get("path") == "{}.jsonl".format(source_id),
            "backup manifest inventory path mismatch: {}".format(source_id),
        )
        verification = source.get("verification")
        require(isinstance(verification, dict), "backup manifest verification is missing: {}".format(source_id))
        require(verification.get("full_hash") is True, "backup source was not fully hashed at creation: {}".format(source_id))
        require(verification.get("result") == "verified", "backup source creation verification is incomplete: {}".format(source_id))
        symlink_ownership = verification.get("symlink_ownership")
        require(
            isinstance(symlink_ownership, dict)
            and symlink_ownership.get("policy") == SYMLINK_OWNER_POLICY
            and int(symlink_ownership.get("backup_owner_uid", -1))
            == BACKUP_ROOT.stat().st_uid
            and int(symlink_ownership.get("substitution_count", -1)) >= 0
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(symlink_ownership.get("substitutions_sha256", "")),
            )
            is not None,
            "backup source symlink-ownership verification is invalid: {}".format(
                source_id
            ),
        )

    require(len([row for row in source_records if row["category"] == "map-root"]) == 8, "backup manifest does not cover eight Map V1 roots")
    require(len([row for row in source_records if row["category"] == "technical"]) == 6, "backup manifest does not cover technical namespaces")
    return source_records


def verify_backup_copy(backup_dir: Path, *, full_hash: bool) -> dict[str, Any]:
    validate_backup_environment(create_root=False)
    require(BACKUP_NAME_RE.fullmatch(backup_dir.name) is not None, "backup directory name is invalid")
    require(backup_dir.is_dir() and not backup_dir.is_symlink(), "backup generation is not a physical directory")
    require(
        Path(os.path.abspath(backup_dir.parent)) == Path(os.path.abspath(BACKUP_ROOT)),
        "backup generation is outside the exact weekly root",
    )
    manifest = load_manifest(backup_dir)
    manifest_schema = str(manifest["schema_version"])
    source_records = validate_manifest_sources(
        manifest.get("sources"),
        manifest_schema=manifest_schema,
    )
    verification: dict[str, Any] = {}
    for source in source_records:
        source_id = str(source["source_id"])
        inventory_record = source.get("inventory") or {}
        inventories_root = backup_dir / "inventories"
        payload_base = backup_dir / "payload"
        require(inventories_root.is_dir() and not inventories_root.is_symlink(), "backup inventories root is not a physical directory")
        require(payload_base.is_dir() and not payload_base.is_symlink(), "backup payload root is not a physical directory")
        inventory = inventory_target(inventories_root, str(inventory_record["path"]))
        payload_root = inventory_target(payload_base, str(source["destination_relative"]))
        verification[source_id] = verify_inventory(
            payload_root,
            inventory,
            inventory_record,
            full_hash=full_hash,
            manifest_schema=manifest_schema,
        )
        require(
            verification[source_id]["symlink_ownership"]
            == source["verification"]["symlink_ownership"],
            "backup symlink-ownership readback mismatch: {}".format(source_id),
        )
    scheduler = manifest.get("scheduler_store") or {}
    require(
        scheduler.get("destination_relative") == "control/scheduler/jobs.json",
        "scheduler-store recovery destination mismatch",
    )
    scheduler_path = inventory_target(backup_dir, "control/scheduler/jobs.json")
    require(
        scheduler_path.is_file() and not scheduler_path.is_symlink(),
        "scheduler-store recovery copy is missing or is a symlink",
    )
    require(sha256_file(scheduler_path) == scheduler.get("sha256"), "scheduler-store recovery checksum mismatch")
    if manifest_schema == MANIFEST_SCHEMA:
        require(
            scheduler.get("capture_consistency") == CAPTURE_CONSISTENCY
            and scheduler.get("source_tree_atomic") is False
            and scheduler.get("inventory_origin") == INVENTORY_ORIGIN,
            "scheduler-store capture semantics are invalid",
        )
    return {
        "backup": backup_dir.name,
        "manifest_sha256": sha256_file(backup_dir / "MANIFEST.json"),
        "source_count": len(source_records),
        "scheduler_store": "verified",
        "full_hash": full_hash,
        "creation_full_hash_verified": True,
        "verification": verification,
        "restore_probe": manifest.get("restore_probe"),
        "result": "verified",
    }


def list_backup_generations() -> list[Path]:
    if not BACKUP_ROOT.exists():
        return []
    rows: list[Path] = []
    for child in BACKUP_ROOT.iterdir():
        if child.is_dir() and not child.is_symlink() and BACKUP_NAME_RE.fullmatch(child.name):
            rows.append(child)
    return sorted(rows, key=lambda row: row.name, reverse=True)


def validate_retention_candidate(path: Path) -> None:
    require(
        path.parent == BACKUP_ROOT and BACKUP_NAME_RE.fullmatch(path.name) is not None,
        "unsafe retention delete path",
    )
    require(
        path.is_dir() and not path.is_symlink(),
        "retention candidate is not a physical directory",
    )
    require(
        path.stat().st_dev == EXPECTED_DEVICE,
        "retention candidate device mismatch",
    )
    validate_tree_device(path, expected_device=EXPECTED_DEVICE)


def cleanup_backups(
    *,
    apply: bool,
    known_verified: dict[str, str] | None = None,
) -> dict[str, Any]:
    validate_backup_environment(create_root=False)
    known = dict(known_verified or {})
    generations = list_backup_generations()
    generation_names = {generation.name for generation in generations}
    require(set(known) <= generation_names, "retention protection names a missing generation")
    for name, manifest_sha256 in known.items():
        require(BACKUP_NAME_RE.fullmatch(name) is not None, "retention protection has an invalid generation name")
        require(
            sha256_file(BACKUP_ROOT / name / "MANIFEST.json") == manifest_sha256,
            "retention protection manifest digest mismatch: {}".format(name),
        )
    full_hash_required = apply and len(generations) > RETENTION_COUNT
    verification: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    latest_allowed = datetime.now(timezone.utc) + timedelta(seconds=MAX_FUTURE_GENERATION_SKEW_SECONDS)
    for generation in generations:
        try:
            match = BACKUP_NAME_RE.fullmatch(generation.name)
            require(match is not None, "backup generation name is invalid")
            generated_at = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            require(
                generated_at <= latest_allowed,
                "backup generation timestamp is unexpectedly in the future",
            )
            verification[generation.name] = (
                {
                    "backup": generation.name,
                    "result": "verified",
                    "full_hash": True,
                    "source": "current-run",
                    "manifest_sha256": known[generation.name],
                }
                if generation.name in known
                else verify_backup_copy(generation, full_hash=full_hash_required)
            )
        except Exception as exc:
            blockers.append("{}:{}".format(generation.name, str(exc)))

    verified_names = [row.name for row in generations if row.name in verification]
    protected = verified_names[:RETENTION_COUNT]
    for name in known:
        if name in verification and name not in protected:
            protected.append(name)
    candidates = [name for name in verified_names if name not in protected]
    removed: list[str] = []
    retained_unverified = [row.name for row in generations if row.name not in verification]

    if apply and not blockers:
        for name in candidates:
            try:
                validate_retention_candidate(BACKUP_ROOT / name)
            except Exception as exc:
                blockers.append("{}:pre-delete-device-check:{}".format(name, exc))

    if apply and not blockers:
        for name in candidates:
            require(len([item for item in verified_names if item not in removed and item != name]) >= RETENTION_COUNT, "retention would cross two-copy floor")
            path = BACKUP_ROOT / name
            validate_retention_candidate(path)
            shutil.rmtree(path)
            fsync_dir(BACKUP_ROOT)
            require(not os.path.lexists(path), "retention deletion did not remove exact generation")
            removed.append(name)

    return {
        "schema_version": RETENTION_SCHEMA,
        "generated_at_utc": utc_now(),
        "apply": apply,
        "verification_mode": "full-content-hash" if full_hash_required else "manifest-inventory-metadata",
        "retention_count": RETENTION_COUNT,
        "total_generations": len(generations),
        "verified_generations": verified_names,
        "protected_generations": protected,
        "candidates": candidates,
        "removed": removed,
        "retained_unverified": retained_unverified,
        "blockers": blockers,
        "verified_remaining": len([item for item in verified_names if item not in removed]),
        "two_copy_floor_satisfied": len([item for item in verified_names if item not in removed]) >= RETENTION_COUNT,
        "result": "blocked" if blockers else "verified",
    }


def create_backup() -> tuple[int, dict[str, Any]]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = BACKUP_ROOT / "openclaw-backup-{}".format(timestamp)
    staging = BACKUP_ROOT / ".openclaw-backup-{}.incomplete-{}".format(timestamp, os.getpid())
    receipt = PhaseReceipt(timestamp, final)
    blockers: list[str] = []
    lock_descriptor: int | None = None
    try:
        validate_backup_environment(create_root=True)
        prior_incomplete = sorted(
            child.name
            for child in BACKUP_ROOT.iterdir()
            if INCOMPLETE_NAME_RE.fullmatch(child.name)
        )
        require(
            not prior_incomplete,
            "prior incomplete backup staging requires classification before retry: {}".format(
                ",".join(prior_incomplete)
            ),
        )
        require(not os.path.lexists(final), "weekly backup generation collision")
        require(not os.path.lexists(staging), "weekly backup staging collision")
        lock_path = BACKUP_ROOT.parent / ".weekly-backup.lock"
        lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("weekly backup lock is already held") from exc

        specs = map_source_specs()
        preflight = validate_preflight(specs)
        receipt.phase("preflight", "verified", preflight=preflight)
        staging.mkdir(mode=0o700)
        (staging / "payload").mkdir()
        (staging / "inventories").mkdir()
        fsync_dir(staging.parent)

        source_records: list[dict[str, Any]] = []
        verification_records: dict[str, dict[str, Any]] = {}
        preflight_sources = {
            str(row["source_id"]): row
            for row in preflight["sources"]
        }
        for spec in specs:
            if not spec.source.exists() and not spec.required:
                continue
            destination = staging / "payload" / spec.destination_relative
            inventory_path = staging / "inventories" / "{}.jsonl".format(spec.source_id)
            preflight_source = preflight_sources[spec.source_id]
            clone_capture = clone_directory(
                spec.source,
                destination,
                expected_source_inode=int(preflight_source["inode"]),
            )
            inventory = inventory_tree(
                destination,
                inventory_path,
                symlink_source_provenance=clone_capture["symlink_source_provenance"],
            )
            inventory_sealed_at_utc = utc_now()
            verification = verify_inventory(
                destination,
                inventory_path,
                inventory,
                full_hash=True,
                manifest_schema=MANIFEST_SCHEMA,
            )
            record = {
                "source_id": spec.source_id,
                "source": str(spec.source),
                "destination_relative": str(spec.destination_relative),
                "category": spec.category,
                "source_device": int(clone_capture["source_device"]),
                "source_inode": int(clone_capture["source_inode"]),
                "copy_method": "APFS clone via /bin/cp -c -R -p, then inventory private staging copy",
                "capture_consistency": CAPTURE_CONSISTENCY,
                "source_tree_atomic": False,
                "inventory_origin": INVENTORY_ORIGIN,
                "capture_started_at_utc": clone_capture["capture_started_at_utc"],
                "copy_completed_at_utc": clone_capture["copy_completed_at_utc"],
                "inventory_sealed_at_utc": inventory_sealed_at_utc,
                "inventory": inventory,
                "verification": {
                    "entries": verification["entries"],
                    "files": verification["files"],
                    "content_bytes": verification["content_bytes"],
                    "full_hash": verification["full_hash"],
                    "symlink_ownership": verification["symlink_ownership"],
                    "result": verification["result"],
                },
            }
            source_records.append(record)
            verification_records[spec.source_id] = verification
            receipt.phase(
                "source-{}".format(spec.source_id),
                "verified",
                source_id=spec.source_id,
                files=verification["files"],
                content_bytes=verification["content_bytes"],
                capture_consistency=CAPTURE_CONSISTENCY,
                source_tree_atomic=False,
                inventory_origin=INVENTORY_ORIGIN,
                capture_started_at_utc=clone_capture["capture_started_at_utc"],
                copy_completed_at_utc=clone_capture["copy_completed_at_utc"],
                inventory_sealed_at_utc=inventory_sealed_at_utc,
            )

        scheduler_destination = staging / "control/scheduler/jobs.json"
        scheduler_capture = clone_file(SCHEDULER_STORE, scheduler_destination)
        scheduler_record = {
            "source": str(SCHEDULER_STORE),
            "destination_relative": "control/scheduler/jobs.json",
            "sha256": scheduler_capture["sha256"],
            "mode": scheduler_capture["mode"],
            "copy_method": "physical file copy via /bin/cp -p, then destination JSON and SHA-256 readback",
            "capture_consistency": CAPTURE_CONSISTENCY,
            "source_tree_atomic": False,
            "inventory_origin": INVENTORY_ORIGIN,
            "capture_started_at_utc": scheduler_capture["capture_started_at_utc"],
            "copy_completed_at_utc": scheduler_capture["copy_completed_at_utc"],
            "source_before": scheduler_capture["source_before"],
            "source_after": scheduler_capture["source_after"],
            "source_changed_during_copy": scheduler_capture["source_changed_during_copy"],
            "result": "verified",
        }
        restore = restore_probe(staging, source_records, verification_records)
        receipt.phase("restore-probe", "verified", restore_probe=restore)
        manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA,
            "backup_name": final.name,
            "created_at_utc": utc_now(),
            "status": "verified",
            "destination": str(final),
            "copy_method": "same-device APFS clones inventoried from private staging plus full SHA-256 readback",
            "capture_consistency": CAPTURE_CONSISTENCY,
            "source_tree_atomic": False,
            "same_device_local_recovery": True,
            "independent_disaster_recovery": False,
            "icloud_offload": False,
            "sources": source_records,
            "scheduler_store": scheduler_record,
            "restore_probe": restore,
            "retention_count": RETENTION_COUNT,
            "exclusions": preflight["explicit_exclusions"],
        }
        manifest["manifest_payload_sha256"] = sha256_bytes(canonical_json(manifest))
        atomic_write_json(staging / "MANIFEST.json", manifest)
        manifest_readback = json.loads((staging / "MANIFEST.json").read_text(encoding="utf-8"))
        require(manifest_readback == manifest, "backup manifest readback mismatch")
        os.rename(staging, final)
        fsync_dir(BACKUP_ROOT)
        require(final.is_dir() and not final.is_symlink(), "published backup is missing")
        verification = verify_backup_copy(final, full_hash=False)
        retention = cleanup_backups(
            apply=True,
            known_verified={final.name: verification["manifest_sha256"]},
        )
        if retention["blockers"]:
            blockers.extend(retention["blockers"])
        require(final.is_dir() and not final.is_symlink(), "retention removed the just-published backup")
        receipt.finish(
            "completed" if not blockers else "completed_with_retention_blocker",
            "verified" if not blockers else "verified_backup_retention_blocked",
            blockers,
            backup=final.name,
            manifest=str(final / "MANIFEST.json"),
            verification=verification,
            retention=retention,
            icloud_offload=False,
        )
        result = {
            "backup": final.name,
            "destination": str(final),
            "receipt": str(receipt.path),
            "manifest": str(final / "MANIFEST.json"),
            "verification": verification,
            "retention": retention,
            "blockers": blockers,
            "result": "verified" if not blockers else "verified_backup_retention_blocked",
        }
        return (0 if not blockers else 1), result
    except Exception as exc:
        blockers.append("{}: {}".format(type(exc).__name__, exc))
        receipt.finish(
            "failed",
            "blocked",
            blockers,
            staging=str(staging),
            source_retained=True,
            incomplete_staging_retained=staging.exists(),
        )
        return 1, {
            "backup": final.name,
            "destination": str(final),
            "receipt": str(receipt.path),
            "staging": str(staging),
            "blockers": blockers,
            "result": "blocked",
        }
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path, help="verify one published backup generation without creating a backup")
    parser.add_argument("--retention-preview", action="store_true", help="classify OWC weekly retention without deleting")
    return parser


def cli(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.verify is not None:
            print(json.dumps(verify_backup_copy(args.verify.expanduser(), full_hash=True), indent=2, sort_keys=True))
            return 0
        if args.retention_preview:
            report = cleanup_backups(apply=False)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if not report["blockers"] else 1
        print("BACKUP_FAIL")
        print(
            "STATUS | result: blocked | blockers: clone-v2 creation is retired "
            "from the supported CLI | next: use "
            "scripts/openclaw_weekly_archive_backup.py for the standing "
            "three-archive backup"
        )
        return 1
    except Exception as exc:
        print("BACKUP_FAIL")
        print("STATUS | result: blocked | blockers: {}: {} | next: inspect the exact OWC backup preflight or verification failure".format(type(exc).__name__, exc))
        return 1

    if code != 0:
        print("BACKUP_FAIL")
        print(
            "STATUS | backup: {backup} | result: {result} | blockers: {blockers} | receipt: {receipt} | next: retain all copies and repair the exact blocker before retry".format(
                backup=result["backup"],
                result=result["result"],
                blockers="; ".join(result["blockers"]),
                receipt=result["receipt"],
            )
        )
        return code
    if ANNOUNCE_SUCCESS:
        print("BACKUP_OK")
        print(
            "STATUS | backup: {backup} | result: verified | destination: {destination} | retained: {retained} | removed: {removed} | receipt: {receipt} | next: none".format(
                backup=result["backup"],
                destination=result["destination"],
                retained=",".join(result["retention"]["protected_generations"]) or result["backup"],
                removed=",".join(result["retention"]["removed"]) or "none",
                receipt=result["receipt"],
            )
        )
    else:
        print("NO_REPLY")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
