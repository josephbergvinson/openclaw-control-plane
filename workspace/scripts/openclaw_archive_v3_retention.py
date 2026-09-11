#!/usr/bin/env python3
"""Classify and retain verified OpenClaw archive-v3 weekly backups.

This is the standing archive-v3 retention engine.  It intentionally does not
import or mutate the clone-v2 retention implementation.  Clone-v2 generations
are accepted only as explicitly pinned frozen evidence and are never deletion
candidates.
"""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Iterable
import uuid

try:
    from . import openclaw_independent_backup_receipt as independent_backup_proof
    from . import openclaw_weekly_archive_backup as archive_backup
except ImportError:
    import openclaw_independent_backup_receipt as independent_backup_proof
    import openclaw_weekly_archive_backup as archive_backup


RECEIPT_SCHEMA = "openclaw.archive_v3_retention.receipt.v1"
CLONE_V2_NAME_RE = re.compile(r"^openclaw-backup-(\d{8}T\d{6}Z)$")
CLONE_V2_INCOMPLETE_RE = re.compile(
    r"^\.openclaw-backup-(\d{8}T\d{6}Z)\.incomplete-\d+$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROBE_PREFIX = ".archive-v3-retention-probe-"
PROBE_NAME_RE = re.compile(
    r"^\.archive-v3-retention-probe-\d+-[0-9a-f]{32}$"
)
PROBE_OWNER_FILE = ".openclaw-archive-v3-retention-owner.json"
PROBE_OWNER_SCHEMA = "openclaw.archive_v3_retention.probe_owner.v1"
KEEP_NEWEST_V3 = 2
INCIDENT_STATE_NAME = "incident-state.json"


class RetentionError(RuntimeError):
    """A retention classification, verification, or deletion gate failed."""


class IndependentBackupUnavailable(RetentionError):
    """Current inventory is safe, but external recovery proof is unavailable."""


@dataclass(frozen=True)
class CloneV2Pin:
    name: str
    manifest_sha256: str


@dataclass(frozen=True)
class RetentionConfig:
    producer_config: archive_backup.BackupConfig = field(
        default_factory=archive_backup.BackupConfig
    )
    receipt_dir: Path = Path(
        (str(OPERATOR.require_path('paths.workspace')) + '/artifacts/openclaw_archive_v3_retention/receipts')
    )
    clone_v2_pins: tuple[CloneV2Pin, ...] = ()
    independent_backup_receipt: Path | None = None

    @property
    def backup_root(self) -> Path:
        return self.producer_config.backup_root

    @property
    def expected_device(self) -> int:
        expected = self.producer_config.expected_device
        if expected is None:
            raise RetentionError("retention device identity was not resolved")
        return expected

    @property
    def lock_path(self) -> Path:
        return self.backup_root.parent / ".weekly-backup.lock"


@dataclass(frozen=True)
class Inventory:
    v3_generations: tuple[Path, ...]
    pinned_clone_v2: tuple[dict[str, Any], ...]
    creation_proofs: tuple[dict[str, Any], ...]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RetentionError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalized_pins(config: RetentionConfig) -> dict[str, str]:
    pins: dict[str, str] = {}
    for pin in config.clone_v2_pins:
        digest = pin.manifest_sha256.lower()
        require(
            CLONE_V2_NAME_RE.fullmatch(pin.name) is not None,
            "invalid frozen clone-v2 pin name: {}".format(pin.name),
        )
        require(
            SHA256_RE.fullmatch(digest) is not None,
            "invalid frozen clone-v2 manifest digest: {}".format(pin.name),
        )
        require(
            pin.name not in pins,
            "duplicate frozen clone-v2 pin: {}".format(pin.name),
        )
        pins[pin.name] = digest
    return pins


def resolve_runtime_device_identity(
    config: RetentionConfig,
    *,
    identity_reader=archive_backup.read_volume_identity,
) -> RetentionConfig:
    """Resolve volatile st_dev from the canonical UUID+mount once per run."""

    if config.producer_config.expected_device is not None:
        return config
    identity = identity_reader(config.producer_config)
    require(
        identity.uuid.upper()
        == config.producer_config.expected_volume_uuid.upper(),
        "OWC volume UUID mismatch",
    )
    require(
        archive_backup.absolute(identity.mount)
        == archive_backup.absolute(config.producer_config.owc_volume_mount),
        "OWC mount point mismatch",
    )
    require(identity.writable, "OWC volume is read-only")
    require(identity.owners_enabled, "OWC ownership is disabled")
    return replace(
        config,
        producer_config=replace(
            config.producer_config,
            expected_device=identity.device,
        ),
    )


def read_json_regular_file(path: Path, *, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RetentionError(f"{label} cannot be opened safely") from exc
    try:
        info = os.fstat(descriptor)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            f"{label} is not an owner-private physical file",
        )
        require(info.st_size <= 1024 * 1024, f"{label} exceeds the 1 MiB limit")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            payload = json.load(handle)
    except RetentionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RetentionError(f"{label} is unreadable") from exc
    finally:
        os.close(descriptor)
    require(isinstance(payload, dict), f"{label} is not an object")
    return payload


def validate_independent_backup_receipt(config: RetentionConfig) -> dict[str, Any]:
    receipt_path = config.independent_backup_receipt
    require(
        receipt_path is not None,
        "verified independent backup receipt is required before deletion",
    )
    payload = read_json_regular_file(
        receipt_path,
        label="independent backup receipt",
    )

    def source_identity_reader(path: Path) -> independent_backup_proof.VolumeIdentity:
        require(
            path.resolve(strict=True)
            == config.backup_root.resolve(strict=True),
            "independent backup validator source root mismatch",
        )
        return independent_backup_proof.VolumeIdentity(
            volume_uuid=config.producer_config.expected_volume_uuid.upper(),
            device=config.expected_device,
            mount=str(
                archive_backup.absolute(config.producer_config.owc_volume_mount)
            ),
        )

    try:
        validated = independent_backup_proof.validate_receipt_payload(
            payload,
            source_weekly_root=config.backup_root,
            expected_source_volume_uuid=(
                config.producer_config.expected_volume_uuid
            ),
            expected_source_device=config.expected_device,
            identity_reader=source_identity_reader,
        )
    except independent_backup_proof.IndependentBackupEvidenceError as exc:
        raise RetentionError(str(exc)) from exc
    return {**payload, "validated_evidence": validated}


def retention_input_fingerprint(config: RetentionConfig) -> str:
    rows: list[dict[str, Any]] = []
    if config.backup_root.exists():
        for child in sorted(config.backup_root.iterdir(), key=lambda path: path.name):
            info = child.lstat()
            rows.append(
                {
                    "name": child.name,
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "mode": stat.S_IFMT(info.st_mode),
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                }
            )
    receipt_digest = None
    if config.independent_backup_receipt is not None:
        try:
            receipt_digest = hashlib.sha256(
                config.independent_backup_receipt.read_bytes()
            ).hexdigest()
        except OSError:
            receipt_digest = "unreadable"
    payload = {
        "volume_uuid": config.producer_config.expected_volume_uuid.upper(),
        "mount": str(archive_backup.absolute(config.producer_config.owc_volume_mount)),
        "device": config.expected_device,
        "weekly_root": str(archive_backup.absolute(config.backup_root)),
        "entries": rows,
        "independent_backup_receipt_sha256": receipt_digest,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def incident_state_path(config: RetentionConfig) -> Path:
    return config.receipt_dir / INCIDENT_STATE_NAME


def load_incident_state(config: RetentionConfig) -> dict[str, Any] | None:
    path = incident_state_path(config)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def persist_incident_state(
    config: RetentionConfig,
    *,
    input_fingerprint: str,
    blocker: str,
    status: str = "blocked",
) -> str:
    incident_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "input_fingerprint": input_fingerprint,
                "blocker": blocker,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    archive_backup.atomic_write_json(
        incident_state_path(config),
        {
            "schema_version": "openclaw.archive_v3_retention.incident.v1",
            "status": status,
            "input_fingerprint": input_fingerprint,
            "incident_fingerprint": incident_fingerprint,
            "blocker": blocker,
            "updated_at_utc": utc_now(),
        },
    )
    return incident_fingerprint


def validate_environment(config: RetentionConfig) -> None:
    producer = config.producer_config
    require(
        archive_backup.absolute(config.backup_root)
        == archive_backup.absolute(producer.owc_root / "Backups/weekly"),
        "retention root is not exact OWC Backups/weekly",
    )
    archive_backup.require_physical_directory(
        producer.owc_root,
        config.expected_device,
    )
    archive_backup.require_physical_chain(
        config.backup_root,
        producer.owc_root,
        config.expected_device,
    )
    require(
        not archive_backup.absolute(config.receipt_dir).is_relative_to(
            archive_backup.absolute(config.backup_root)
        ),
        "retention receipts must remain outside the weekly generation root",
    )
    normalized_pins(config)


def validate_generation_tree(
    path: Path,
    *,
    config: RetentionConfig,
) -> dict[str, Any]:
    require(
        path.parent == config.backup_root
        and archive_backup.BACKUP_NAME_RE.fullmatch(path.name) is not None,
        "unsafe archive-v3 generation path",
    )
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and not path.is_symlink()
        and info.st_dev == config.expected_device,
        "archive-v3 generation is symlinked, non-physical, or off-device: "
        "{}".format(path.name),
    )
    actual_names = {child.name for child in path.iterdir()}
    require(
        actual_names == archive_backup.EXPECTED_GENERATION_FILES,
        "archive-v3 generation file set mismatch: {}".format(path.name),
    )
    allocated_bytes = 0
    for child in path.iterdir():
        child_info = child.lstat()
        require(
            stat.S_ISREG(child_info.st_mode)
            and not child.is_symlink()
            and child_info.st_dev == config.expected_device,
            "archive-v3 generation contains a symlinked, non-physical, or "
            "off-device member: {}/{}".format(path.name, child.name),
        )
        allocated_bytes += child_info.st_blocks * 512
    return {
        "generation": path.name,
        "device": info.st_dev,
        "inode": info.st_ino,
        "file_count": len(actual_names),
        "allocated_bytes": allocated_bytes,
        "result": "verified",
    }


def validate_predelete_generation_tree(
    path: Path,
    *,
    config: RetentionConfig,
) -> dict[str, Any]:
    proof = validate_generation_tree(path, config=config)
    archive_backup.generation_file_modes(path)
    proof["phase"] = "immediate-predelete"
    return proof


def validate_pinned_clone(
    path: Path,
    *,
    expected_manifest_sha256: str,
    config: RetentionConfig,
) -> dict[str, Any]:
    require(
        path.parent == config.backup_root
        and CLONE_V2_NAME_RE.fullmatch(path.name) is not None,
        "unsafe frozen clone-v2 path",
    )
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and not path.is_symlink()
        and info.st_dev == config.expected_device,
        "frozen clone-v2 is symlinked, non-physical, or off-device: {}".format(
            path.name
        ),
    )
    manifest = path / "MANIFEST.json"
    manifest_info = manifest.lstat()
    require(
        stat.S_ISREG(manifest_info.st_mode)
        and not manifest.is_symlink()
        and manifest_info.st_dev == config.expected_device,
        "frozen clone-v2 manifest is missing, symlinked, or off-device: "
        "{}".format(path.name),
    )
    actual_digest = archive_backup.sha256_file(manifest)
    require(
        actual_digest == expected_manifest_sha256,
        "frozen clone-v2 manifest pin mismatch: {} expected={} actual={}".format(
            path.name,
            expected_manifest_sha256,
            actual_digest,
        ),
    )
    return {
        "name": path.name,
        "manifest_sha256": actual_digest,
        "classification": "frozen-pinned-never-delete",
        "result": "verified",
    }


def scan_and_verify(config: RetentionConfig) -> Inventory:
    pins = normalized_pins(config)
    seen_pins: set[str] = set()
    v3: list[Path] = []
    clone_rows: list[dict[str, Any]] = []
    creation_rows: list[dict[str, Any]] = []
    for child in sorted(
        config.backup_root.iterdir(),
        key=lambda row: os.fsencode(row.name),
    ):
        info = child.lstat()
        require(
            not stat.S_ISLNK(info.st_mode),
            "weekly root contains a symlink generation or entry: {}".format(
                child.name
            ),
        )
        if archive_backup.BACKUP_NAME_RE.fullmatch(child.name):
            validate_generation_tree(child, config=config)
            proof = archive_backup.verify_published_generation(
                child,
                config=config.producer_config,
            )
            creation_rows.append(
                {
                    "generation": child.name,
                    **proof,
                }
            )
            v3.append(child)
            continue
        if archive_backup.INCOMPLETE_NAME_RE.fullmatch(child.name):
            raise RetentionError(
                "incomplete archive-v3 generation requires classification: "
                "{}".format(child.name)
            )
        if CLONE_V2_INCOMPLETE_RE.fullmatch(child.name):
            raise RetentionError(
                "incomplete clone-v2 generation requires classification: "
                "{}".format(child.name)
            )
        if CLONE_V2_NAME_RE.fullmatch(child.name):
            require(
                child.name in pins,
                "unknown unpinned clone-v2 generation blocks retention: "
                "{}".format(child.name),
            )
            clone_rows.append(
                validate_pinned_clone(
                    child,
                    expected_manifest_sha256=pins[child.name],
                    config=config,
                )
            )
            seen_pins.add(child.name)
            continue
        raise RetentionError(
            "unknown weekly-root entry blocks retention: {}".format(child.name)
        )
    missing_pins = set(pins) - seen_pins
    require(
        not missing_pins,
        "configured frozen clone-v2 pins are missing: {}".format(
            ",".join(sorted(missing_pins))
        ),
    )
    return Inventory(
        v3_generations=tuple(
            sorted(v3, key=lambda row: archive_backup.BACKUP_NAME_RE.fullmatch(row.name).group(1), reverse=True)
        ),
        pinned_clone_v2=tuple(
            sorted(clone_rows, key=lambda row: str(row["name"]))
        ),
        creation_proofs=tuple(
            sorted(
                creation_rows,
                key=lambda row: str(row["generation"]),
                reverse=True,
            )
        ),
    )


def verification_specs() -> tuple[archive_backup.ArchiveSpec, ...]:
    return (
        archive_backup.ArchiveSpec(
            filename="openclaw-state.tgz",
            role="state",
            roots=(),
            required_members=frozenset(
                {
                    ".openclaw",
                    ".openclaw/openclaw.json",
                    ".openclaw/cron/jobs.json",
                    ".openclaw/agents/main/sessions",
                    ".openclaw/browser",
                    ".openclaw/media",
                }
            ),
            required_probe_groups=frozenset(
                {"state-internal", "sessions", "browser", "media"}
            ),
        ),
        archive_backup.ArchiveSpec(
            filename="openclaw-workspace-policy.tgz",
            role="policy",
            roots=(),
            required_members=frozenset(
                {*archive_backup.POLICY_FILES, "runbook", "memory"}
            ),
            required_probe_groups=frozenset({"policy"}),
        ),
        archive_backup.ArchiveSpec(
            filename="git-remotes.tgz",
            role="git-remotes",
            roots=(),
            required_members=frozenset({"git-remotes"}),
            required_probe_groups=frozenset({"git-remotes"}),
        ),
    )


def remove_owned_tree(
    path: Path,
    *,
    expected_parent: Path,
    expected_device: int,
    expected_identity: tuple[int, int],
    name_allowed: bool,
    purpose: str,
) -> None:
    try:
        archive_backup.remove_owned_directory_tree(
            path,
            expected_parent=expected_parent,
            expected_device=expected_device,
            expected_identity=expected_identity,
            name_allowed=name_allowed,
            purpose=purpose,
        )
    except archive_backup.BackupError as exc:
        raise RetentionError(str(exc)) from exc


def cleanup_probe_path(
    path: Path,
    *,
    config: RetentionConfig,
    expected_identity: tuple[int, int],
) -> None:
    remove_owned_tree(
        path,
        expected_parent=config.backup_root.parent,
        expected_device=config.expected_device,
        expected_identity=expected_identity,
        name_allowed=PROBE_NAME_RE.fullmatch(path.name) is not None,
        purpose="archive-v3 retention probe",
    )


def write_probe_owner(
    path: Path,
    *,
    info: os.stat_result,
    invocation_id: str,
) -> dict[str, Any]:
    payload = {
        "schema_version": PROBE_OWNER_SCHEMA,
        "invocation_id": invocation_id,
        "path": str(path),
        "name": path.name,
        "device": info.st_dev,
        "inode": info.st_ino,
        "created_at_utc": utc_now(),
    }
    archive_backup.atomic_write_json(path / PROBE_OWNER_FILE, payload)
    return payload


def classify_owned_probe(
    path: Path,
    *,
    config: RetentionConfig,
) -> dict[str, Any]:
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and not path.is_symlink()
        and info.st_dev == config.expected_device,
        "archive-v3 retention probe sibling is symlinked, non-physical, "
        "or off-device: {}".format(path.name),
    )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        descriptor_info = os.fstat(descriptor)
        require(
            stat.S_ISDIR(descriptor_info.st_mode)
            and archive_backup.same_file_identity(
                descriptor_info,
                (info.st_dev, info.st_ino),
            ),
            "archive-v3 retention probe identity changed during "
            "classification: {}".format(path.name),
        )
        marker_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            marker_flags |= os.O_NOFOLLOW
        try:
            marker_descriptor = os.open(
                PROBE_OWNER_FILE,
                marker_flags,
                dir_fd=descriptor,
            )
        except FileNotFoundError as exc:
            raise RetentionError(
                "archive-v3 retention probe ownership marker is missing: "
                "{}".format(path.name)
            ) from exc
        try:
            marker_info = os.fstat(marker_descriptor)
            require(
                stat.S_ISREG(marker_info.st_mode)
                and marker_info.st_dev == config.expected_device
                and stat.S_IMODE(marker_info.st_mode) == 0o600
                and 0 < marker_info.st_size <= 64 * 1024,
                "archive-v3 retention probe ownership marker is unsafe: "
                "{}".format(path.name),
            )
            marker_payload = os.read(
                marker_descriptor,
                marker_info.st_size + 1,
            )
            require(
                len(marker_payload) == marker_info.st_size
                and os.read(marker_descriptor, 1) == b"",
                "archive-v3 retention probe ownership marker changed during "
                "classification: {}".format(path.name),
            )
        finally:
            os.close(marker_descriptor)
    finally:
        os.close(descriptor)
    try:
        owner = json.loads(marker_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetentionError(
            "archive-v3 retention probe ownership marker is unreadable: "
            "{}".format(path.name)
        ) from exc
    require(
        isinstance(owner, dict)
        and owner.get("schema_version") == PROBE_OWNER_SCHEMA
        and owner.get("path") == str(path)
        and owner.get("name") == path.name
        and owner.get("device") == info.st_dev
        and owner.get("inode") == info.st_ino
        and isinstance(owner.get("invocation_id"), str)
        and bool(owner["invocation_id"]),
        "archive-v3 retention probe ownership marker mismatch: {}".format(
            path.name
        ),
    )
    return {
        "path": str(path),
        "name": path.name,
        "device": info.st_dev,
        "inode": info.st_ino,
        "owner_marker_sha256": archive_backup.sha256_bytes(marker_payload),
        "owner_invocation_id": owner["invocation_id"],
        "classification": "owned-stale-restore-probe",
    }


def reconcile_stale_probe_paths(
    config: RetentionConfig,
    *,
    receipt: RetentionReceipt,
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for child in sorted(
        config.backup_root.parent.iterdir(),
        key=lambda row: os.fsencode(row.name),
    ):
        if not child.name.startswith(PROBE_PREFIX):
            continue
        require(
            PROBE_NAME_RE.fullmatch(child.name) is not None,
            "malformed archive-v3 retention probe sibling blocks "
            "reconciliation: {}".format(child.name),
        )
        rows.append(classify_owned_probe(child, config=config))
    receipt.phase(
        "stale-probe-reconciliation-intent",
        probes=list(rows),
        result_if_interrupted=(
            "receipt identifies every exact owned path/device/inode; "
            "re-run under the shared lock"
        ),
    )
    for row in rows:
        path = Path(str(row["path"]))
        cleanup_probe_path(
            path,
            config=config,
            expected_identity=(
                int(row["device"]),
                int(row["inode"]),
            ),
        )
    receipt.phase(
        "stale-probe-reconciliation-complete",
        removed=[str(row["path"]) for row in rows],
    )
    return tuple(rows)


def deep_verify_all(
    generations: Iterable[Path],
    *,
    config: RetentionConfig,
    receipt: RetentionReceipt,
) -> tuple[dict[str, Any], ...]:
    probe_parent = config.backup_root.parent / (
        "{}{}-{}".format(
            PROBE_PREFIX,
            os.getpid(),
            uuid.uuid4().hex,
        )
    )
    probe_parent.mkdir(mode=0o700)
    os.chmod(probe_parent, 0o700)
    probe_info = probe_parent.lstat()
    require(
        stat.S_ISDIR(probe_info.st_mode)
        and not probe_parent.is_symlink()
        and probe_info.st_dev == config.expected_device,
        "retention restore-probe root is not a physical OWC directory",
    )
    owner = write_probe_owner(
        probe_parent,
        info=probe_info,
        invocation_id=str(receipt.payload["invocation_id"]),
    )
    archive_backup.fsync_dir(probe_parent.parent)
    rows: list[dict[str, Any]] = []
    specs = verification_specs()
    try:
        receipt.phase(
            "current-probe-created-before-extraction",
            path=str(probe_parent),
            device=probe_info.st_dev,
            inode=probe_info.st_ino,
            owner_marker=str(probe_parent / PROBE_OWNER_FILE),
            owner_marker_sha256=archive_backup.sha256_file(
                probe_parent / PROBE_OWNER_FILE
            ),
            owner_invocation_id=owner["invocation_id"],
            classification="owned-current-restore-probe",
            cleanup=(
                "same-invocation finally; next invocation exact-identity "
                "reconciliation after abrupt process loss"
            ),
        )
        for generation in generations:
            manifest = archive_backup.load_manifest(generation)
            archive_rows = {
                row["name"]: row for row in manifest["archives"]
            }
            require(
                len(manifest["archives"])
                == len(archive_backup.EXPECTED_ARCHIVE_NAMES)
                and len(archive_rows)
                == len(archive_backup.EXPECTED_ARCHIVE_NAMES)
                and set(archive_rows)
                == set(archive_backup.EXPECTED_ARCHIVE_NAMES),
                "deep verification archive record set mismatch: {}".format(
                    generation.name
                ),
            )
            budget = archive_backup.restore_budget_from_archive_records(
                manifest["archives"]
            )
            free_before = shutil.disk_usage(config.backup_root).free
            require(
                free_before
                >= (
                    config.producer_config.min_post_backup_free_bytes
                    + budget["total_bytes"]
                ),
                "insufficient OWC headroom for deep retention verification: "
                "{} free={} required={}".format(
                    generation.name,
                    free_before,
                    (
                        config.producer_config.min_post_backup_free_bytes
                        + budget["total_bytes"]
                    ),
                ),
            )
            generation_probe = probe_parent / generation.name
            generation_probe.mkdir(mode=0o700)
            generation_probe_info = generation_probe.lstat()
            proofs: list[dict[str, Any]] = []
            try:
                for spec in specs:
                    proofs.append(
                        archive_backup.verify_archive(
                            generation / spec.filename,
                            spec,
                            archive_rows[spec.filename],
                            probe_root=generation_probe,
                            expected_device=config.expected_device,
                            config=config.producer_config,
                        )
                    )
                require(
                    shutil.disk_usage(config.backup_root).free
                    >= config.producer_config.min_post_backup_free_bytes,
                    "OWC free space crossed the configured floor during deep "
                    "retention verification",
                )
                rows.append(
                    {
                        "generation": generation.name,
                        "headroom_budget": budget,
                        "archives": proofs,
                        "result": "verified",
                    }
                )
            finally:
                remove_owned_tree(
                    generation_probe,
                    expected_parent=probe_parent,
                    expected_device=config.expected_device,
                    expected_identity=(
                        generation_probe_info.st_dev,
                        generation_probe_info.st_ino,
                    ),
                    name_allowed=(
                        archive_backup.BACKUP_NAME_RE.fullmatch(
                            generation_probe.name
                        )
                        is not None
                    ),
                    purpose="archive-v3 generation restore probe",
                )
    finally:
        cleanup_probe_path(
            probe_parent,
            config=config,
            expected_identity=(probe_info.st_dev, probe_info.st_ino),
        )
    return tuple(rows)


def delete_verified_generation(
    path: Path,
    *,
    config: RetentionConfig,
    quarantine: Path,
) -> None:
    proof = validate_predelete_generation_tree(path, config=config)
    require(
        quarantine.parent == config.backup_root
        and quarantine.name.startswith(
            ".archive-v3-retention-delete-{}-".format(path.name)
        )
        and not os.path.lexists(quarantine),
        "unsafe or colliding archive-v3 deletion quarantine",
    )
    os.rename(path, quarantine)
    archive_backup.fsync_dir(config.backup_root)
    require(
        not os.path.lexists(path),
        "archive-v3 source still exists after quarantine: {}".format(path.name),
    )
    quarantine_info = quarantine.lstat()
    require(
        stat.S_ISDIR(quarantine_info.st_mode)
        and not quarantine.is_symlink()
        and (
            quarantine_info.st_dev,
            quarantine_info.st_ino,
        )
        == (proof["device"], proof["inode"]),
        "archive-v3 deletion quarantine identity mismatch: {}".format(
            path.name
        ),
    )
    archive_backup.generation_file_modes(quarantine)
    remove_owned_tree(
        quarantine,
        expected_parent=config.backup_root,
        expected_device=config.expected_device,
        expected_identity=(
            quarantine_info.st_dev,
            quarantine_info.st_ino,
        ),
        name_allowed=quarantine.name.startswith(
            ".archive-v3-retention-delete-{}-".format(path.name)
        ),
        purpose="archive-v3 deletion quarantine",
    )


def acquire_shared_lock(config: RetentionConfig) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(config.lock_path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_dev == config.expected_device,
            "weekly backup lock is non-physical or off-device",
        )
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RetentionError(
                "weekly backup lock is already held"
            ) from exc
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


class RetentionReceipt:
    def __init__(self, config: RetentionConfig, *, apply: bool):
        archive_backup.ensure_private_directory(config.receipt_dir)
        invocation_id = "{}-{}-{}".format(
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            os.getpid(),
            uuid.uuid4().hex,
        )
        self.path = config.receipt_dir / (
            "openclaw-archive-v3-retention-{}.json".format(invocation_id)
        )
        self.payload: dict[str, Any] = {
            "schema_version": RECEIPT_SCHEMA,
            "invocation_id": invocation_id,
            "started_at_utc": utc_now(),
            "updated_at_utc": utc_now(),
            "apply": apply,
            "status": "in_progress",
            "current_phase": "initializing",
            "phases": [],
            "blockers": [],
            "removed": [],
        }
        self.write()

    def write(self) -> None:
        self.payload["updated_at_utc"] = utc_now()
        archive_backup.atomic_write_json(self.path, self.payload)

    def phase(self, name: str, **details: Any) -> None:
        self.payload["current_phase"] = name
        self.payload["phases"].append(
            {
                "name": name,
                "at_utc": utc_now(),
                "result": "verified",
                **details,
            }
        )
        self.write()

    def finish(
        self,
        *,
        status: str,
        result: str,
        blockers: list[str],
        **details: Any,
    ) -> None:
        self.payload.update(
            {
                "status": status,
                "result": result,
                "blockers": list(blockers),
                "current_phase": "terminal",
                "completed_at_utc": utc_now(),
                **details,
            }
        )
        self.write()


def run_retention(
    *,
    apply: bool,
    config: RetentionConfig = RetentionConfig(),
) -> tuple[int, dict[str, Any]]:
    previous_umask = os.umask(0o077)
    descriptor: int | None = None
    removed: list[str] = []
    delete_attempted: dict[str, Path] = {}
    receipt: RetentionReceipt | None = None
    protected_names: list[str] = []
    candidate_names: list[str] = []
    pinned_clone_v2: list[dict[str, Any]] = []
    input_fingerprint: str | None = None
    try:
        receipt = RetentionReceipt(config, apply=apply)
        config = resolve_runtime_device_identity(config)
        validate_environment(config)
        descriptor = acquire_shared_lock(config)
        input_fingerprint = retention_input_fingerprint(config)
        receipt.phase(
            "canonical-volume-identity",
            volume_uuid=config.producer_config.expected_volume_uuid,
            mount=str(config.producer_config.owc_volume_mount),
            device=config.expected_device,
        )
        reconcile_stale_probe_paths(config, receipt=receipt)
        inventory = scan_and_verify(config)
        pinned_clone_v2 = list(inventory.pinned_clone_v2)
        generations = list(inventory.v3_generations)
        require(
            len(generations) >= KEEP_NEWEST_V3,
            "archive-v3 two-copy floor is not satisfied: verified={} "
            "required={}".format(
                len(generations),
                KEEP_NEWEST_V3,
            ),
        )
        protected = generations[:KEEP_NEWEST_V3]
        candidates = sorted(
            generations[KEEP_NEWEST_V3:],
            key=lambda row: row.name,
        )
        protected_names = [path.name for path in protected]
        candidate_names = [path.name for path in candidates]
        receipt.phase(
            "creation-proof-full-sha-readback",
            weekly_root=str(config.backup_root),
            expected_device=config.expected_device,
            v3_generations=[path.name for path in generations],
            protected_v3=protected_names,
            candidates=candidate_names,
            creation_proofs=list(inventory.creation_proofs),
            pinned_clone_v2=list(inventory.pinned_clone_v2),
        )

        deep_rows: tuple[dict[str, Any], ...] = ()
        independent_backup: dict[str, Any] | None = None
        if apply and candidates:
            try:
                independent_backup = validate_independent_backup_receipt(config)
            except RetentionError as exc:
                raise IndependentBackupUnavailable(str(exc)) from exc
            receipt.phase(
                "independent-backup-gate",
                receipt=str(config.independent_backup_receipt),
                backup_device=independent_backup["backup_device"],
                verified_at_utc=independent_backup["verified_at_utc"],
            )
            deep_rows = deep_verify_all(
                generations,
                config=config,
                receipt=receipt,
            )
            require(
                len(deep_rows) == len(generations)
                and {
                    str(row["generation"]) for row in deep_rows
                }
                == {path.name for path in generations}
                and all(row.get("result") == "verified" for row in deep_rows),
                "not every archive-v3 generation passed deep verification",
            )
            receipt.phase(
                "all-v3-full-decompression-and-restore",
                generations=list(deep_rows),
            )
            predelete_rows = [
                validate_predelete_generation_tree(
                    candidate,
                    config=config,
                )
                for candidate in candidates
            ]
            receipt.phase(
                "predelete-physical-tree-validation",
                candidates=predelete_rows,
                deep_verification_sha256=archive_backup.sha256_bytes(
                    archive_backup.canonical_json(list(deep_rows))
                ),
            )
            for candidate in candidates:
                quarantine = config.backup_root / (
                    ".archive-v3-retention-delete-{}-{}".format(
                        candidate.name,
                        uuid.uuid4().hex,
                    )
                )
                delete_attempted[candidate.name] = quarantine
                receipt.phase(
                    "delete-intent-{}".format(candidate.name),
                    source=str(candidate),
                    quarantine=str(quarantine),
                    verification_predicate=(
                        "all-v3-deep-verified-and-candidate-physical-tree-"
                        "verified"
                    ),
                )
                delete_verified_generation(
                    candidate,
                    config=config,
                    quarantine=quarantine,
                )
                removed.append(candidate.name)
                receipt.phase(
                    "delete-{}".format(candidate.name),
                    removed=list(removed),
                    protected_v3=protected_names,
                )

        final_inventory = scan_and_verify(config)
        remaining_names = [
            path.name for path in final_inventory.v3_generations
        ]
        require(
            all(name in remaining_names for name in protected_names),
            "a protected archive-v3 generation is missing after retention",
        )
        expected_remaining = [
            path.name
            for path in inventory.v3_generations
            if path.name not in removed
        ]
        require(
            remaining_names == expected_remaining,
            "archive-v3 post-retention inventory mismatch",
        )
        report = {
            "schema_version": RECEIPT_SCHEMA,
            "apply": apply,
            "weekly_root": str(config.backup_root),
            "expected_device": config.expected_device,
            "total_v3_before": len(generations),
            "protected_v3": protected_names,
            "candidates": candidate_names,
            "removed": list(removed),
            "remaining_v3": remaining_names,
            "verified_remaining": len(remaining_names),
            "two_copy_floor_satisfied": len(remaining_names)
            >= KEEP_NEWEST_V3,
            "pinned_clone_v2": list(final_inventory.pinned_clone_v2),
            "retention_performed": bool(removed),
            "apply_requested": apply,
            "deletion_authorized": bool(
                apply and candidates and independent_backup
            ),
            "delete_intent_count": len(delete_attempted),
            "independent_backup_verified": bool(independent_backup),
            "independent_backup_evidence": (
                independent_backup.get("validated_evidence")
                if independent_backup
                else None
            ),
            "blockers": [],
            "result": "verified",
        }
        receipt.finish(
            status="completed",
            result="verified",
            blockers=[],
            **{
                key: value
                for key, value in report.items()
                if key not in {"schema_version", "blockers", "result"}
            },
        )
        if apply:
            incident_state_path(config).unlink(missing_ok=True)
        report["receipt"] = str(receipt.path)
        return 0, report
    except Exception as exc:
        for name, quarantine in delete_attempted.items():
            if (
                name not in removed
                and not os.path.lexists(config.backup_root / name)
                and not os.path.lexists(quarantine)
            ):
                removed.append(name)
        waiting_external_backup = (
            isinstance(exc, IndependentBackupUnavailable)
            and apply
            and input_fingerprint is not None
            and not delete_attempted
            and not removed
        )
        blocker = (
            "RetentionError: {}".format(exc)
            if waiting_external_backup
            else "{}: {}".format(type(exc).__name__, exc)
        )
        incident_fingerprint = None
        retry_suppressed = False
        suppression_reason = None
        if apply and input_fingerprint is not None:
            prior_incident = load_incident_state(config)
            retry_suppressed = bool(
                waiting_external_backup
                and prior_incident
                and prior_incident.get("schema_version")
                == "openclaw.archive_v3_retention.incident.v1"
                and prior_incident.get("status") == "waiting_external_backup"
                and prior_incident.get("input_fingerprint")
                == input_fingerprint
                and prior_incident.get("blocker") == blocker
                and isinstance(prior_incident.get("incident_fingerprint"), str)
            )
            if retry_suppressed:
                incident_fingerprint = str(
                    prior_incident["incident_fingerprint"]
                )
                suppression_reason = (
                    "unchanged waiting_external_backup fingerprint; "
                    "transition already reported"
                )
            else:
                try:
                    incident_fingerprint = persist_incident_state(
                        config,
                        input_fingerprint=input_fingerprint,
                        blocker=blocker,
                        status=(
                            "waiting_external_backup"
                            if waiting_external_backup
                            else "blocked"
                        ),
                    )
                except Exception:
                    incident_fingerprint = None
        result = (
            "waiting_external_backup"
            if waiting_external_backup
            else "blocked"
        )
        terminal_status = (
            "waiting_external_backup"
            if waiting_external_backup
            else "failed"
        )
        return_code = 0 if waiting_external_backup else 1
        report = {
            "schema_version": RECEIPT_SCHEMA,
            "apply": apply,
            "apply_requested": apply,
            "weekly_root": str(config.backup_root),
            "expected_device": config.producer_config.expected_device,
            "protected_v3": protected_names,
            "candidates": candidate_names,
            "pinned_clone_v2": pinned_clone_v2,
            "removed": list(removed),
            "quarantined": [
                str(path)
                for path in delete_attempted.values()
                if os.path.lexists(path)
            ],
            "retention_performed": bool(removed),
            "deletion_authorized": False,
            "delete_intent_count": len(delete_attempted),
            "independent_backup_verified": False,
            "incident_fingerprint": incident_fingerprint,
            "input_fingerprint": input_fingerprint,
            "retry_suppressed": retry_suppressed,
            "suppression_reason": suppression_reason,
            "transition_emitted": bool(
                waiting_external_backup and not retry_suppressed
            ),
            "blockers": [blocker],
            "result": result,
        }
        if receipt is not None:
            try:
                receipt.finish(
                    status=terminal_status,
                    result=result,
                    blockers=[blocker],
                    apply_requested=apply,
                    protected_v3=protected_names,
                    candidates=candidate_names,
                    pinned_clone_v2=pinned_clone_v2,
                    removed=list(removed),
                    quarantined=report["quarantined"],
                    retention_performed=bool(removed),
                    deletion_authorized=False,
                    delete_intent_count=len(delete_attempted),
                    independent_backup_verified=False,
                    incident_fingerprint=incident_fingerprint,
                    input_fingerprint=input_fingerprint,
                    retry_suppressed=retry_suppressed,
                    suppression_reason=suppression_reason,
                    transition_emitted=report["transition_emitted"],
                )
            except Exception as receipt_exc:
                report["blockers"].append(
                    "receipt terminalization failed: {}: {}".format(
                        type(receipt_exc).__name__,
                        receipt_exc,
                    )
                )
                report["result"] = "blocked"
                return_code = 1
            report["receipt"] = str(receipt.path)
        return return_code, report
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.umask(previous_umask)


def parse_pin(value: str) -> CloneV2Pin:
    try:
        name, digest = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "pin must be NAME=MANIFEST_SHA256"
        ) from exc
    pin = CloneV2Pin(name=name, manifest_sha256=digest.lower())
    if (
        CLONE_V2_NAME_RE.fullmatch(pin.name) is None
        or SHA256_RE.fullmatch(pin.manifest_sha256) is None
    ):
        raise argparse.ArgumentTypeError(
            "pin must use an exact clone-v2 name and 64-character SHA-256"
        )
    return pin


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "after every v3 passes fresh deep verification, delete all but "
            "the newest two v3 generations"
        ),
    )
    parser.add_argument(
        "--independent-backup-receipt",
        type=Path,
        help=(
            "verified receipt for a backup on a different physical device; "
            "required whenever --apply has deletion candidates"
        ),
    )
    parser.add_argument(
        "--pinned-clone-v2",
        action="append",
        type=parse_pin,
        default=[],
        metavar="NAME=MANIFEST_SHA256",
        help=(
            "exact frozen clone-v2 generation/manifest pin; repeat for every "
            "clone-v2 generation present"
        ),
    )
    return parser


def run_from_args(argv: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(argv)
    config = RetentionConfig(
        clone_v2_pins=tuple(args.pinned_clone_v2),
        independent_backup_receipt=args.independent_backup_receipt,
    )
    return run_retention(apply=args.apply, config=config)


def report_message(code: int, report: dict[str, Any]) -> str:
    """Describe the actual outcome without exposing the private diagnostics."""
    removed = len(report.get("removed", []))
    if code != 0:
        if removed:
            return (
                "Backup maintenance stopped after removing {} older {}. "
                "The remaining backups are not yet fully verified."
            ).format(removed, "backup" if removed == 1 else "backups")
        if report.get("quarantined"):
            return (
                "Backup maintenance stopped with an older backup set aside. "
                "Cleanup and verification are incomplete."
            )
        return "Backup maintenance could not complete its safety checks. No backups were removed."
    if report.get("result") == "waiting_external_backup":
        if report.get("retry_suppressed") is True:
            return "NO_REPLY"
        return (
            "Older backups were kept because an independent backup has not yet "
            "been verified. Nothing was removed."
        )
    if not report.get("apply", report.get("apply_requested")):
        candidates = len(report.get("candidates", []))
        if not candidates:
            return "No older backups need cleanup. Nothing was removed."
        return "{} older {} eligible for cleanup. Nothing was removed.".format(
            candidates, "backup is" if candidates == 1 else "backups are",
        )
    if not removed:
        return "NO_REPLY"
    return (
        "Backup maintenance removed {} older {} and kept the newest two verified backups."
    ).format(removed, "backup" if removed == 1 else "backups")


def main(argv: list[str] | None = None) -> int:
    code, report = run_from_args(argv)
    print(report_message(code, report))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
