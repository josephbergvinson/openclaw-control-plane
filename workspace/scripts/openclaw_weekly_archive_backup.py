#!/usr/bin/env python3
"""Build the bounded three-archive OWC weekly OpenClaw recovery set.

This is the standing successor to the retired broad Map V1 clone producer and
has a deliberately small payload contract:

* ``openclaw-state.tgz`` is the supported native OpenClaw state archive,
  including online snapshots of retained canonical agent databases;
* ``openclaw-workspace-policy.tgz`` contains selected policy/runbook/memory;
* ``git-remotes.tgz`` contains the physical local bare remotes.

The destination is OWC-local, publication is atomic, and this producer never
deletes a backup generation or writes to iCloud.
"""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
from typing import Any, Callable, Iterable, Iterator
import uuid

try:
    from . import external_volume_guard, operation_effect_predicate as effect
    from .cron_private_output import bounded_utf8_prefix, redact_process_text
except ImportError:  # Direct script execution places this directory on sys.path.
    import external_volume_guard
    import operation_effect_predicate as effect
    from cron_private_output import bounded_utf8_prefix, redact_process_text


PHASE_RECEIPT_SCHEMA = "openclaw.weekly_archive_backup.phase_receipt.v3"
MANIFEST_SCHEMA = "openclaw.owc_weekly_backup.manifest.v3"
NATIVE_MANIFEST_SCHEMA = "openclaw.owc_weekly_backup.manifest.v4"
NATIVE_STATE_FORMAT = "openclaw-native-archive-v1"
CAPTURE_CONSISTENCY = "archive-self-consistent-source-tree-non-atomic-v1"
PAYLOAD_CONTRACT = "three-gzip-tarballs-v1"
EXPECTED_ARCHIVE_NAMES = (
    "openclaw-state.tgz",
    "openclaw-workspace-policy.tgz",
    "git-remotes.tgz",
)
EXPECTED_GENERATION_FILES = frozenset(
    {
        *EXPECTED_ARCHIVE_NAMES,
        "INDEX.md",
        "MANIFEST.json",
        "SHA256SUMS.txt",
    }
)
POLICY_FILES = (
    "AGENTS.md",
    "TOOLS.md",
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "POLICY_CHANGELOG.md",
)
VOLATILE_SUFFIXES = (".lock", ".sock", ".tmp")
UPDATE_WRAPPER_OPERATIONAL_LOG_ARCHIVE_PATTERN = (
    r"\.openclaw/logs/update-wrapper-\d{8}T\d{6}Z\.log"
)
UPDATE_WRAPPER_OPERATIONAL_LOG_ARCHIVE_RE = re.compile(
    r"^{}$".format(UPDATE_WRAPPER_OPERATIONAL_LOG_ARCHIVE_PATTERN)
)
BROWSER_VOLATILE_CACHE_DIRECTORY_NAMES = (
    "AutofillAiModelCache",
    "Cache",
    "CacheStorage",
    "Code Cache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GPUCache",
    "GPUPersistentCache",
    "GraphiteDawnCache",
    "GrShaderCache",
    "ScriptCache",
    "ShaderCache",
    "cache",
    "component_crx_cache",
    "extensions_crx_cache",
    "optimization_guide_hint_cache_store",
)
BROWSER_MANAGED_USER_DATA_ARCHIVE_ROOTS = (
    PurePosixPath(".openclaw/browser/openclaw/user-data"),
    PurePosixPath(".openclaw/browser/chrome-automation-profile"),
)
BROWSER_PROFILE_NAME_PATTERN = (
    r"(?:Default|Guest Profile|System Profile|Profile [1-9][0-9]*)"
)
BROWSER_PROFILE_NAME_RE = re.compile(
    r"^{}$".format(BROWSER_PROFILE_NAME_PATTERN)
)
BROWSER_EXTENSION_ID_PATTERN = r"[a-p]{32}"
BROWSER_EXTENSION_ID_RE = re.compile(
    r"^{}$".format(BROWSER_EXTENSION_ID_PATTERN)
)
BROWSER_GLOBAL_CACHE_RELATIVES = frozenset(
    PurePosixPath(name)
    for name in (
        "GPUPersistentCache",
        "GrShaderCache",
        "GraphiteDawnCache",
        "ShaderCache",
        "component_crx_cache",
        "extensions_crx_cache",
    )
)
BROWSER_PROFILE_CACHE_RELATIVES = frozenset(
    PurePosixPath(name)
    for name in (
        "AutofillAiModelCache",
        "Cache",
        "Code Cache",
        "DawnGraphiteCache",
        "DawnWebGPUCache",
        "GPUCache",
        "optimization_guide_hint_cache_store",
    )
)
BROWSER_PROFILE_NESTED_CACHE_RELATIVES = frozenset(
    {
        PurePosixPath("Service Worker/CacheStorage"),
        PurePosixPath("Service Worker/ScriptCache"),
        PurePosixPath("Shared Dictionary/cache"),
    }
)
BROWSER_EXTENSION_CACHE_RELATIVES = frozenset(
    PurePosixPath(name)
    for name in (
        "Cache",
        "Code Cache",
        "DawnGraphiteCache",
        "DawnWebGPUCache",
        "GPUCache",
        "Shared Dictionary/cache",
    )
)
BROWSER_NESTED_CACHE_PATTERNS = (
    "WebStorage/<decimal>/CacheStorage",
    (
        "Storage/ext/<extension-id-a-p-32>/def/"
        "<extension-cache-relative>"
    ),
)
RECONSTRUCTIBLE_RUNTIME_CACHE_ARCHIVE_ROOTS = frozenset(
    {PurePosixPath(".openclaw/plugin-runtime-deps")}
)
RECONSTRUCTIBLE_RUNTIME_CACHE_POLICY = (
    "stat-physical-same-device-directory-then-exclude-before-descent"
)
RECONSTRUCTIBLE_RUNTIME_CACHE_RESTORE_RATIONALE = (
    "derived bundled-plugin dependencies are regenerated from the installed "
    "exact OpenClaw release by runtime dependency staging or openclaw doctor; "
    "they are not authoritative user state"
)
GIT_REGULAR_BASENAME_EXCLUSIONS = (".DS_Store",)
BACKUP_NAME_RE = re.compile(r"^openclaw-archive-v[34]-(\d{8}T\d{6}Z)$")
INCOMPLETE_NAME_RE = re.compile(
    r"^\.openclaw-archive-v[34]-(\d{8}T\d{6}Z)\.incomplete-\d+$"
)
PROBE_MAX_BYTES = 4 * 1024 * 1024
RESTORE_METADATA_BASE_BYTES = 64 * 1024 * 1024
RESTORE_METADATA_PER_ENTRY_BYTES = 4096
DEFAULT_MIN_POST_BACKUP_FREE_BYTES = 30 * 1024**3
GZIP_COMPRESSLEVEL = 6
SQLITE_SNAPSHOT_ARCHIVE_ROOT = PurePosixPath(
    ".openclaw/sqlite-snapshots"
)
SQLITE_DATABASE_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")
STATE_SQLITE_DATABASE_ARCHIVE_RE = re.compile(
    r"^(?:\.openclaw/state/openclaw\.sqlite|"
    r"\.openclaw/agents/[^/]+/agent/openclaw-agent\.sqlite)"
    r"(?:-wal|-shm|-journal)?$"
)
# Bound the source-sized snapshot repository, the stock command's private
# staging copy, and one additional verification/copy-sized transient, with a
# fourth source-sized margin. The fixed 64 MiB metadata allowance is added by
# authoritative_sqlite_source_inventory().
SQLITE_SNAPSHOT_PEAK_SOURCE_MULTIPLIER = 4
SQLITE_CREATE_TIMEOUT_SECONDS = 1800
SQLITE_INSPECT_TIMEOUT_SECONDS = 300
MAX_NATIVE_DIAGNOSTIC_BYTES = 16 * 1024


def default_state_root() -> Path:
    configured = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
    expected = OPERATOR.require_path('paths.state_root')
    if configured and Path(configured) != expected:
        raise ValueError('OPENCLAW_STATE_DIR disagrees with the operator contract')
    return expected


class BackupError(RuntimeError):
    """A backup safety, creation, or verification invariant failed."""

    def __init__(self, message: str, *, native_command_diagnostic: dict[str, Any] | None = None):
        super().__init__(message)
        # Never interpolate child output into the exception or public effect.
        self.native_command_diagnostic = native_command_diagnostic


@dataclass(frozen=True)
class VolumeIdentity:
    uuid: str
    device: int
    mount: Path
    writable: bool
    owners_enabled: bool


@dataclass(frozen=True)
class SessionStoreRoute:
    layout: str
    logical_path: Path
    agents_logical: Path
    agents_physical: Path
    authoritative_directory: Path
    compatibility_alias: Path | None

    def as_manifest_record(self) -> dict[str, str | None]:
        return {
            "layout": self.layout,
            "logical_path": str(self.logical_path),
            "agents_parent_logical": str(self.agents_logical),
            "agents_parent_physical": str(self.agents_physical),
            "authoritative_directory": str(self.authoritative_directory),
            "compatibility_alias": (
                str(self.compatibility_alias)
                if self.compatibility_alias is not None
                else None
            ),
        }


@dataclass(frozen=True)
class BackupConfig:
    owc_root: Path = Path((str(OPERATOR.require_path('paths.agent_storage_root'))))
    owc_volume_mount: Path = Path((str(OPERATOR.require_path('paths.data_root'))))
    expected_volume_uuid: str = OPERATOR.require_string('volumes.expected_uuid')
    # st_dev is a mounted-session identity, not durable volume identity. It may
    # change after dismount/remount. Production pins UUID+mount and captures
    # the current device once per run; tests/manual diagnostics may provide an
    # explicit expected device to prove mismatch handling.
    expected_device: int | None = None
    state_root: Path = field(default_factory=default_state_root)
    workspace_logical: Path = Path((str(OPERATOR.require_path('paths.workspace'))))
    git_remotes_logical: Path = Path((str(OPERATOR.require_path('paths.git_history_root'))))
    backup_root: Path = Path(
        (str(OPERATOR.require_path('paths.archive_root')))
    )
    receipt_dir: Path = Path(
        (str(OPERATOR.require_path('paths.workspace')) + '/artifacts/openclaw_weekly_archive_backup/receipts')
    )
    min_post_backup_free_bytes: int = DEFAULT_MIN_POST_BACKUP_FREE_BYTES
    git_binary: Path = Path("/usr/bin/git")
    openclaw_cli: Path = Path((str(OPERATOR.require_path('paths.openclaw_cli'))))

    @property
    def workspace_physical(self) -> Path:
        return self.owc_root / "Workspace"

    @property
    def git_remotes_physical(self) -> Path:
        return self.owc_volume_mount / "ProjectInfrastructure/GitRemotes"

    @property
    def agents_logical(self) -> Path:
        return self.state_root / "agents"

    @property
    def agents_physical(self) -> Path:
        return self.owc_root / ".state/OpenClaw/agents"

    @property
    def sessions_logical(self) -> Path:
        return self.agents_logical / "main/sessions"

    @property
    def sessions_legacy_physical(self) -> Path:
        return self.owc_root / ".state/OpenClaw/Sessions"

    @property
    def sessions_current_physical(self) -> Path:
        return self.agents_physical / "main/sessions"

    @property
    def browser_logical(self) -> Path:
        return self.state_root / "browser"

    @property
    def browser_physical(self) -> Path:
        return self.owc_root / ".state/OpenClaw/Browser"

    @property
    def media_logical(self) -> Path:
        return self.state_root / "media"

    @property
    def media_physical(self) -> Path:
        return self.owc_root / ".state/OpenClaw/Media"

    @property
    def state_shadow_exclusions(self) -> frozenset[Path]:
        return frozenset(
            {
                self.agents_physical
                / "main/sessions.pre-owc-openclaw-sessions",
                self.state_root / "browser.pre-owc-openclaw-browser",
                self.state_root / "media.pre-owc-openclaw-media",
            }
        )


@dataclass(frozen=True)
class Expansion:
    logical: Path
    physical: Path
    probe_group: str | None
    expected_device: int


@dataclass(frozen=True)
class RootInput:
    source: Path
    archive_name: PurePosixPath
    expected_device: int
    expansions: dict[Path, Expansion] = field(default_factory=dict)
    exact_exclusions: frozenset[Path] = frozenset()


@dataclass(frozen=True)
class ArchiveSpec:
    filename: str
    role: str
    roots: tuple[RootInput, ...]
    required_members: frozenset[str]
    required_probe_groups: frozenset[str]


@dataclass(frozen=True)
class SqliteSnapshotSet:
    repository: Path
    agent_ids: tuple[str, ...]
    snapshot_paths: tuple[Path, ...]

    def expected_snapshots(self) -> tuple[dict[str, Any], ...]:
        require(
            len(self.snapshot_paths) == len(self.agent_ids) + 1,
            "SQLite snapshot set does not cover global and configured agents",
        )
        rows = [
            {
                "snapshot_id": path.name,
                "role": "agent",
                "agent_id": agent_id,
            }
            for agent_id, path in zip(
                self.agent_ids,
                self.snapshot_paths[:-1],
            )
        ]
        rows.append(
            {
                "snapshot_id": self.snapshot_paths[-1].name,
                "role": "global",
                "agent_id": None,
            }
        )
        return tuple(rows)

    def as_manifest_record(
        self,
        *,
        result: str = "created",
    ) -> dict[str, Any]:
        return {
            "command_contract": (
                "openclaw backup sqlite create --json"
            ),
            "verification_contract": (
                "openclaw backup sqlite verify <snapshot> --json"
            ),
            "archive_repository_root": (
                SQLITE_SNAPSHOT_ARCHIVE_ROOT.as_posix()
            ),
            "global_snapshot_count": 1,
            "agent_ids": list(self.agent_ids),
            "snapshot_count": len(self.snapshot_paths),
            "snapshot_ids": [path.name for path in self.snapshot_paths],
            "snapshots": list(self.expected_snapshots()),
            "result": result,
        }


@dataclass(frozen=True)
class MemberSource:
    source: Path
    archive_name: str
    info: os.stat_result
    expected_device: int
    probe_group: str | None
    parent_fd: int | None = None
    entry_name: str | None = None


@dataclass
class WalkMetrics:
    entries: int = 0
    files: int = 0
    directories: int = 0
    symlinks: int = 0
    logical_bytes: int = 0
    allocated_bytes: int = 0
    excluded_exact: int = 0
    excluded_volatile: int = 0
    excluded_update_wrapper_operational_logs: int = 0
    excluded_update_wrapper_operational_log_paths: list[str] = field(
        default_factory=list
    )
    excluded_browser_cache_subtrees: int = 0
    excluded_browser_cache_paths: list[str] = field(default_factory=list)
    excluded_reconstructible_runtime_cache_subtrees: int = 0
    excluded_reconstructible_runtime_cache_paths: list[str] = field(
        default_factory=list
    )
    excluded_unix_sockets: int = 0
    excluded_git_finder_metadata: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries": self.entries,
            "files": self.files,
            "directories": self.directories,
            "symlinks": self.symlinks,
            "logical_bytes": self.logical_bytes,
            "allocated_bytes": self.allocated_bytes,
            "excluded_exact": self.excluded_exact,
            "excluded_volatile": self.excluded_volatile,
            "excluded_update_wrapper_operational_logs": (
                self.excluded_update_wrapper_operational_logs
            ),
            "excluded_update_wrapper_operational_log_paths_sha256": (
                sha256_bytes(
                    canonical_json(
                        sorted(
                            self.excluded_update_wrapper_operational_log_paths
                        )
                    )
                )
            ),
            "excluded_browser_cache_subtrees": (
                self.excluded_browser_cache_subtrees
            ),
            "excluded_browser_cache_paths_sha256": sha256_bytes(
                canonical_json(sorted(self.excluded_browser_cache_paths))
            ),
            "excluded_reconstructible_runtime_cache_subtrees": (
                self.excluded_reconstructible_runtime_cache_subtrees
            ),
            "excluded_reconstructible_runtime_cache_paths": sorted(
                self.excluded_reconstructible_runtime_cache_paths
            ),
            "excluded_reconstructible_runtime_cache_paths_sha256": (
                sha256_bytes(
                    canonical_json(
                        sorted(
                            self.excluded_reconstructible_runtime_cache_paths
                        )
                    )
                )
            ),
            "excluded_unix_sockets": self.excluded_unix_sockets,
            "excluded_git_finder_metadata": (
                self.excluded_git_finder_metadata
            ),
        }


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BackupError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


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


def same_file_identity(
    info: os.stat_result,
    expected: tuple[int, int],
) -> bool:
    return (info.st_dev, info.st_ino) == expected


def remove_directory_contents_fd(
    descriptor: int,
    *,
    expected_device: int,
    purpose: str,
) -> None:
    """Remove only entries reached below an already-verified directory fd."""
    with os.scandir(descriptor) as iterator:
        names = sorted(
            (entry.name for entry in iterator),
            key=os.fsencode,
        )
    for name in names:
        before = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        require(
            before.st_dev == expected_device,
            "{} contains an off-device entry".format(purpose),
        )
        if stat.S_ISDIR(before.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            child_descriptor = os.open(
                name,
                flags,
                dir_fd=descriptor,
            )
            try:
                child_info = os.fstat(child_descriptor)
                child_identity = (child_info.st_dev, child_info.st_ino)
                require(
                    stat.S_ISDIR(child_info.st_mode)
                    and child_info.st_dev == expected_device
                    and same_file_identity(before, child_identity),
                    "{} child identity changed before traversal".format(
                        purpose
                    ),
                )
                remove_directory_contents_fd(
                    child_descriptor,
                    expected_device=expected_device,
                    purpose=purpose,
                )
                current = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                require(
                    stat.S_ISDIR(current.st_mode)
                    and same_file_identity(current, child_identity)
                    and same_file_identity(
                        os.fstat(child_descriptor),
                        child_identity,
                    ),
                    "{} child identity changed before final removal".format(
                        purpose
                    ),
                )
                os.rmdir(name, dir_fd=descriptor)
            finally:
                os.close(child_descriptor)
        else:
            current = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            require(
                same_file_identity(
                    current,
                    (before.st_dev, before.st_ino),
                ),
                "{} entry identity changed before unlink".format(purpose),
            )
            os.unlink(name, dir_fd=descriptor)
        os.fsync(descriptor)


def remove_owned_directory_tree(
    path: Path,
    *,
    expected_parent: Path,
    expected_device: int,
    expected_identity: tuple[int, int],
    name_allowed: bool,
    purpose: str,
) -> None:
    """Delete one exact owned tree without resolving deletion by full path."""
    if not os.path.lexists(path):
        return
    require(
        path.parent == expected_parent and name_allowed,
        "unsafe {} cleanup path or identity".format(purpose),
    )
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    child_flags = parent_flags
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
        child_flags |= os.O_NOFOLLOW
    parent_before = expected_parent.lstat()
    parent_descriptor = os.open(expected_parent, parent_flags)
    try:
        parent_info = os.fstat(parent_descriptor)
        require(
            stat.S_ISDIR(parent_info.st_mode)
            and parent_info.st_dev == expected_device
            and same_file_identity(
                parent_before,
                (parent_info.st_dev, parent_info.st_ino),
            ),
            "unsafe {} parent path or identity".format(purpose),
        )
        entry_info = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        require(
            stat.S_ISDIR(entry_info.st_mode)
            and entry_info.st_dev == expected_device
            and same_file_identity(entry_info, expected_identity),
            "unsafe {} cleanup path or identity".format(purpose),
        )
        descriptor = os.open(
            path.name,
            child_flags,
            dir_fd=parent_descriptor,
        )
        try:
            info = os.fstat(descriptor)
            require(
                stat.S_ISDIR(info.st_mode)
                and info.st_dev == expected_device
                and same_file_identity(info, expected_identity),
                "unsafe {} cleanup path or identity".format(purpose),
            )
            current = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            require(
                stat.S_ISDIR(current.st_mode)
                and same_file_identity(current, expected_identity),
                "unsafe {} cleanup path or identity".format(purpose),
            )
            remove_directory_contents_fd(
                descriptor,
                expected_device=expected_device,
                purpose=purpose,
            )
            current = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            require(
                stat.S_ISDIR(current.st_mode)
                and same_file_identity(current, expected_identity)
                and same_file_identity(os.fstat(descriptor), expected_identity),
                "{} identity changed before final removal".format(purpose),
            )
            os.rmdir(path.name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        finally:
            os.close(descriptor)
        try:
            os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise BackupError(
                "{} still exists after cleanup".format(purpose)
            )
    finally:
        os.close(parent_descriptor)


def atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{}.{}.tmp".format(path.name, os.getpid()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_bytes(
        path,
        json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and not path.is_symlink(),
        "sensitive output parent is not a physical directory: {}".format(
            path
        ),
    )
    os.chmod(path, 0o700)
    require(
        stat.S_IMODE(path.lstat().st_mode) == 0o700,
        "sensitive output parent mode is not 0700: {}".format(path),
    )


class PhaseReceipt:
    def __init__(self, config: BackupConfig, run_id: str, destination: Path):
        ensure_private_directory(config.receipt_dir)
        invocation_id = "{}-{}-{}".format(
            run_id,
            os.getpid(),
            uuid.uuid4().hex,
        )
        self.path = (
            config.receipt_dir
            / "openclaw-weekly-archive-backup-{}.json".format(invocation_id)
        )
        self.payload: dict[str, Any] = {
            "schema_version": PHASE_RECEIPT_SCHEMA,
            "run_id": run_id,
            "invocation_id": invocation_id,
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

    def finish(
        self,
        status_value: str,
        result: str,
        blockers: list[str],
        **details: Any,
    ) -> None:
        self.payload.update(
            {
                "status": status_value,
                "result": result,
                "blockers": list(blockers),
                "current_phase": "terminal",
                "completed_at_utc": utc_now(),
                **details,
            }
        )
        self.write()


def read_volume_identity(config: BackupConfig) -> VolumeIdentity:
    metadata = external_volume_guard.volume_metadata(config.owc_volume_mount)
    require(
        metadata.get("available") is True and metadata.get("mounted") is True,
        "volume metadata identity failed: {}".format(
            metadata.get("reason") or metadata.get("error") or "unavailable"
        ),
    )
    mount_device = metadata.get("mountDevice")
    require(isinstance(mount_device, int), "volume metadata has no stable mount device")
    volume_uuid = metadata.get("volumeUuid")
    mount_point = metadata.get("mountPoint")
    owners_enabled = metadata.get("ownersEnabled")
    require(
        isinstance(volume_uuid, str) and bool(volume_uuid),
        "volume metadata has no UUID",
    )
    require(
        isinstance(mount_point, str) and bool(mount_point),
        "volume metadata has no mount point",
    )
    require(isinstance(owners_enabled, bool), "volume ownership metadata is unavailable")
    return VolumeIdentity(
        uuid=volume_uuid.upper(),
        device=mount_device,
        mount=Path(mount_point),
        writable=metadata.get("readOnly") is False,
        owners_enabled=owners_enabled,
    )


def absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def require_physical_directory(path: Path, expected_device: int) -> os.stat_result:
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and not path.is_symlink(),
        "expected a physical directory: {}".format(path),
    )
    require(
        info.st_dev == expected_device,
        "physical directory device mismatch: {}".format(path),
    )
    return info


def require_physical_chain(
    path: Path,
    root: Path,
    expected_device: int,
) -> None:
    root_absolute = absolute(root)
    path_absolute = absolute(path)
    try:
        relative = path_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise BackupError(
            "path is outside its declared physical root: {}".format(path)
        ) from exc
    current = root_absolute
    require_physical_directory(current, expected_device)
    for part in relative.parts:
        current = current / part
        require_physical_directory(current, expected_device)


def validate_managed_directory(
    logical: Path,
    physical: Path,
    *,
    physical_root: Path,
    config: BackupConfig,
    identity: VolumeIdentity,
) -> None:
    logical_info = logical.lstat()
    if stat.S_ISDIR(logical_info.st_mode) and not logical.is_symlink():
        require_physical_chain(logical, physical_root, identity.device)
        require_physical_chain(physical, physical_root, identity.device)
        require(
            os.path.samestat(logical_info, physical.lstat()),
            "managed physical directory target mismatch: {} expected={}".format(
                logical,
                physical,
            ),
        )
        return
    require(
        stat.S_ISLNK(logical_info.st_mode),
        "managed logical path is neither its physical target nor a symlink: "
        "{}".format(logical),
    )
    raw_target = Path(os.readlink(logical))
    require(
        raw_target.is_absolute() and raw_target == physical,
        "managed symlink target mismatch: {} expected={} actual={}".format(
            logical,
            physical,
            raw_target,
        ),
    )
    require_physical_chain(physical, physical_root, identity.device)
    expected_resolved = physical.resolve(strict=True)
    require(
        expected_resolved
        == (
            physical_root.resolve(strict=True)
            / physical.relative_to(physical_root)
        ),
        "managed physical target escapes its declared root: {}".format(physical),
    )
    require(
        logical.resolve(strict=True) == expected_resolved,
        "managed logical path does not resolve to its exact target: {}".format(
            logical
        ),
    )


def resolve_session_store_route(
    config: BackupConfig,
    identity: VolumeIdentity,
) -> SessionStoreRoute:
    """Bind the real agents-parent route and one of two session layouts."""

    validate_managed_directory(
        config.agents_logical,
        config.agents_physical,
        physical_root=config.owc_root,
        config=config,
        identity=identity,
    )
    legacy = config.sessions_legacy_physical
    current = config.sessions_current_physical
    require_physical_chain(current.parent, config.owc_root, identity.device)
    try:
        current_info = current.lstat()
    except OSError as exc:
        raise BackupError("session store route is incomplete") from exc
    try:
        legacy_info: os.stat_result | None = legacy.lstat()
    except FileNotFoundError:
        legacy_info = None
    except OSError as exc:
        raise BackupError("session store route is incomplete") from exc

    legacy_physical = (
        legacy_info is not None
        and stat.S_ISDIR(legacy_info.st_mode)
        and not legacy.is_symlink()
    )
    current_physical = stat.S_ISDIR(current_info.st_mode) and not current.is_symlink()
    if legacy_physical and not current_physical:
        validate_managed_directory(
            current,
            legacy,
            physical_root=config.owc_root,
            config=config,
            identity=identity,
        )
        return SessionStoreRoute(
            layout="legacy-uppercase-physical-v1",
            logical_path=config.sessions_logical,
            agents_logical=config.agents_logical,
            agents_physical=config.agents_physical,
            authoritative_directory=legacy,
            compatibility_alias=current,
        )
    if current_physical and legacy_info is None:
        return SessionStoreRoute(
            layout="beta3-lowercase-physical-no-alias-v2",
            logical_path=config.sessions_logical,
            agents_logical=config.agents_logical,
            agents_physical=config.agents_physical,
            authoritative_directory=current,
            compatibility_alias=None,
        )
    raise BackupError("session store route is not a sanctioned layout")


def validate_environment(
    config: BackupConfig,
    identity_reader: Callable[[BackupConfig], VolumeIdentity],
    *,
    create_backup_root: bool,
) -> VolumeIdentity:
    require(
        absolute(config.backup_root)
        == absolute(config.owc_root / "Backups/weekly"),
        "backup root is not exact OWC Backups/weekly",
    )
    require(
        config.min_post_backup_free_bytes >= 0,
        "minimum post-backup free bytes is negative",
    )
    identity = identity_reader(config)
    require(
        identity.uuid.upper() == config.expected_volume_uuid.upper(),
        "OWC volume UUID mismatch",
    )
    if config.expected_device is not None:
        require(identity.device == config.expected_device, "OWC device mismatch")
    require(
        absolute(identity.mount) == absolute(config.owc_volume_mount),
        "OWC mount point mismatch",
    )
    require(identity.writable, "OWC volume is read-only")
    require(identity.owners_enabled, "OWC ownership is disabled")
    require_physical_directory(config.owc_volume_mount, identity.device)
    require_physical_directory(config.owc_root, identity.device)

    backups_parent = config.owc_root / "Backups"
    if create_backup_root:
        backups_parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(backups_parent, 0o700)
        config.backup_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(config.backup_root, 0o700)
    require_physical_chain(backups_parent, config.owc_root, identity.device)
    require_physical_chain(config.backup_root, config.owc_root, identity.device)

    state_info = config.state_root.lstat()
    require(
        stat.S_ISDIR(state_info.st_mode) and not config.state_root.is_symlink(),
        "OpenClaw state root is not a physical directory",
    )
    validate_managed_directory(
        config.workspace_logical,
        config.workspace_physical,
        physical_root=config.owc_root,
        config=config,
        identity=identity,
    )
    validate_managed_directory(
        config.git_remotes_logical,
        config.git_remotes_physical,
        physical_root=config.owc_volume_mount,
        config=config,
        identity=identity,
    )
    resolve_session_store_route(config, identity)
    for logical, physical in (
        (config.browser_logical, config.browser_physical),
        (config.media_logical, config.media_physical),
    ):
        validate_managed_directory(
            logical,
            physical,
            physical_root=config.owc_root,
            config=config,
            identity=identity,
        )

    for name in POLICY_FILES:
        selected = config.workspace_physical / name
        info = selected.lstat()
        require(
            stat.S_ISREG(info.st_mode) and not selected.is_symlink(),
            "required policy file is missing or not physical: {}".format(
                selected
            ),
        )
    for name in ("runbook", "memory"):
        require_physical_directory(
            config.workspace_physical / name,
            identity.device,
        )
    return identity


def safe_agent_id(value: Any) -> str:
    require(
        isinstance(value, str)
        and value != ""
        and value not in {".", ".."}
        and "\\" not in value
        and PurePosixPath(value).parts == (value,),
        "OpenClaw agents list returned an unsafe agent id",
    )
    return value


def resolve_openclaw_cli(config: BackupConfig) -> Path:
    require(
        config.openclaw_cli.is_absolute(),
        "OpenClaw CLI path is not absolute",
    )
    try:
        resolved = config.openclaw_cli.resolve(strict=True)
    except OSError as exc:
        raise BackupError("OpenClaw CLI cannot be resolved") from exc
    info = resolved.lstat()
    require(
        stat.S_ISREG(info.st_mode) and os.access(resolved, os.X_OK),
        "OpenClaw CLI does not resolve to an executable physical file",
    )
    return resolved


def run_openclaw_json(
    cli: Path,
    config: BackupConfig,
    arguments: tuple[str, ...],
    *,
    label: str,
    timeout_seconds: int,
    scratch_root: Path | None = None,
) -> Any:
    environment = dict(os.environ)
    environment["OPENCLAW_STATE_DIR"] = str(config.state_root)
    environment["LC_ALL"] = "C"
    if scratch_root is not None:
        require_physical_directory(scratch_root, config.backup_root.stat().st_dev)
        require(stat.S_IMODE(scratch_root.stat().st_mode) == 0o700,
                "native backup scratch must be private")
        environment["TMPDIR"] = str(scratch_root)
    try:
        completed = subprocess.run(
            [str(cli), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            "OpenClaw CLI {} timed out".format(label)
        ) from exc
    except OSError as exc:
        raise BackupError(
            "OpenClaw CLI {} could not start".format(label)
        ) from exc
    if completed.returncode != 0:
        redacted, redaction_count = redact_process_text(completed.stderr)
        retained, retained_bytes, truncated = bounded_utf8_prefix(redacted, MAX_NATIVE_DIAGNOSTIC_BYTES)
        raise BackupError(
            "OpenClaw CLI {} failed with exit {}".format(label, completed.returncode),
            native_command_diagnostic={
                "operation": label,
                "exit_code": completed.returncode,
                # stdout can be a partial config/archive JSON result; do not retain it.
                "stderr": {"content_redacted": retained, "retained_bytes": retained_bytes,
                           "truncated": truncated, "redaction_count": redaction_count},
            },
        )
    require(
        completed.stdout.strip() != "" and completed.stderr.strip() == "",
        "OpenClaw CLI {} did not return one clean JSON stream".format(label),
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BackupError(
            "OpenClaw CLI {} returned malformed JSON".format(label)
        ) from exc


def configured_agent_ids(
    config: BackupConfig,
    *,
    cli: Path | None = None,
) -> tuple[str, ...]:
    command_cli = cli or resolve_openclaw_cli(config)
    agents_payload = run_openclaw_json(
        command_cli,
        config,
        ("agents", "list", "--json"),
        label="agents list",
        timeout_seconds=SQLITE_INSPECT_TIMEOUT_SECONDS,
    )
    require(
        isinstance(agents_payload, list) and len(agents_payload) > 0,
        "OpenClaw agents list returned no configured agents",
    )
    agent_ids: list[str] = []
    for row in agents_payload:
        require(
            isinstance(row, dict),
            "OpenClaw agents list row contract mismatch",
        )
        agent_ids.append(safe_agent_id(row.get("id")))
    require(
        len(agent_ids) == len(set(agent_ids)),
        "OpenClaw agents list returned duplicate agent ids",
    )
    return tuple(sorted(agent_ids, key=os.fsencode))


def validate_sqlite_snapshot_create_result(
    payload: Any,
    *,
    repository: Path,
    expected_device: int,
) -> Path:
    require(
        isinstance(payload, dict)
        and payload.get("ok") is True
        and isinstance(payload.get("snapshotPath"), str),
        "OpenClaw SQLite snapshot result contract mismatch",
    )
    repository_path = absolute(repository.resolve(strict=True))
    snapshot_path = Path(payload["snapshotPath"])
    require(
        snapshot_path.is_absolute(),
        "OpenClaw SQLite snapshot path is not absolute",
    )
    try:
        resolved_snapshot = absolute(snapshot_path.resolve(strict=True))
    except OSError as exc:
        raise BackupError("OpenClaw SQLite snapshot path is missing") from exc
    require(
        resolved_snapshot.parent == repository_path,
        "OpenClaw SQLite snapshot escaped its repository",
    )
    snapshot_info = resolved_snapshot.lstat()
    require(
        stat.S_ISDIR(snapshot_info.st_mode)
        and not resolved_snapshot.is_symlink()
        and snapshot_info.st_dev == expected_device
        and stat.S_IMODE(snapshot_info.st_mode) == 0o700,
        "OpenClaw SQLite snapshot directory is not private and physical",
    )
    require(
        set(child.name for child in resolved_snapshot.iterdir())
        == {"manifest.json", "database.sqlite"},
        "OpenClaw SQLite snapshot directory has unexpected contents",
    )
    for name in ("manifest.json", "database.sqlite"):
        member = resolved_snapshot / name
        member_info = member.lstat()
        require(
            stat.S_ISREG(member_info.st_mode)
            and not member.is_symlink()
            and member_info.st_dev == expected_device
            and member_info.st_nlink == 1
            and stat.S_IMODE(member_info.st_mode) == 0o600,
            "OpenClaw SQLite snapshot member is not physical",
        )
    return resolved_snapshot


def create_sqlite_snapshot_repository(
    config: BackupConfig,
    repository: Path,
    *,
    expected_device: int,
) -> SqliteSnapshotSet:
    require(
        not os.path.lexists(repository),
        "SQLite snapshot repository staging path already exists",
    )
    cli = resolve_openclaw_cli(config)
    ordered_agent_ids = configured_agent_ids(config, cli=cli)
    snapshot_paths: list[Path] = []
    for agent_id in ordered_agent_ids:
        payload = run_openclaw_json(
            cli,
            config,
            (
                "backup",
                "sqlite",
                "create",
                "--agent",
                agent_id,
                "--repository",
                str(repository),
                "--json",
            ),
            label="backup sqlite create agent",
            timeout_seconds=SQLITE_CREATE_TIMEOUT_SECONDS,
        )
        snapshot_paths.append(
            validate_sqlite_snapshot_create_result(
                payload,
                repository=repository,
                expected_device=expected_device,
            )
        )
    global_payload = run_openclaw_json(
        cli,
        config,
        (
            "backup",
            "sqlite",
            "create",
            "--global",
            "--repository",
            str(repository),
            "--json",
        ),
        label="backup sqlite create global",
        timeout_seconds=SQLITE_CREATE_TIMEOUT_SECONDS,
    )
    snapshot_paths.append(
        validate_sqlite_snapshot_create_result(
            global_payload,
            repository=repository,
            expected_device=expected_device,
        )
    )
    repository_info = repository.lstat()
    require(
        stat.S_ISDIR(repository_info.st_mode)
        and not repository.is_symlink()
        and repository_info.st_dev == expected_device
        and stat.S_IMODE(repository_info.st_mode) == 0o700,
        "OpenClaw SQLite repository is not private and physical",
    )

    return SqliteSnapshotSet(
        repository=absolute(repository),
        agent_ids=ordered_agent_ids,
        snapshot_paths=tuple(snapshot_paths),
    )


def authoritative_sqlite_paths(
    config: BackupConfig,
    agent_ids: Iterable[str],
) -> frozenset[Path]:
    databases = [config.state_root / "state/openclaw.sqlite"]
    databases.extend(
        config.state_root
        / "agents"
        / safe_agent_id(agent_id)
        / "agent/openclaw-agent.sqlite"
        for agent_id in agent_ids
    )
    return frozenset(
        Path("{}{}".format(database, suffix))
        for database in databases
        for suffix in SQLITE_DATABASE_SIDECAR_SUFFIXES
    )


def authoritative_sqlite_source_paths(
    config: BackupConfig,
    agent_ids: Iterable[str],
) -> frozenset[Path]:
    """Return the physical traversal paths for live SQLite exclusion."""

    databases = [config.state_root / "state/openclaw.sqlite"]
    databases.extend(
        config.agents_physical
        / safe_agent_id(agent_id)
        / "agent/openclaw-agent.sqlite"
        for agent_id in agent_ids
    )
    return frozenset(
        Path("{}{}".format(database, suffix))
        for database in databases
        for suffix in SQLITE_DATABASE_SIDECAR_SUFFIXES
    )


def sqlite_snapshot_payload_bytes(
    sqlite_snapshots: SqliteSnapshotSet,
) -> int:
    return sum(
        (snapshot_path / name).lstat().st_size
        for snapshot_path in sqlite_snapshots.snapshot_paths
        for name in ("manifest.json", "database.sqlite")
    )


def sqlite_snapshot_verification_transient_bytes(
    sqlite_snapshots: SqliteSnapshotSet,
) -> int:
    return max(
        (snapshot_path / "database.sqlite").lstat().st_size
        for snapshot_path in sqlite_snapshots.snapshot_paths
    )


def discover_sqlite_database_entries(
    directory: Path,
    database_name: str,
) -> set[Path]:
    expected_names = {
        "{}{}".format(database_name, suffix).casefold(): "{}{}".format(
            database_name,
            suffix,
        )
        for suffix in SQLITE_DATABASE_SIDECAR_SUFFIXES
    }
    with os.scandir(directory) as entries:
        actual_entries = sorted(
            entries,
            key=lambda row: os.fsencode(row.name),
        )
    discovered: set[Path] = set()
    for entry in actual_entries:
        expected_name = expected_names.get(entry.name.casefold())
        if expected_name is None:
            continue
        # A case-insensitive filesystem can resolve a differently-cased name
        # through the canonical spelling even though Path equality and archive
        # member matching remain lexical. Bind the real directory entry.
        require(
            entry.name == expected_name,
            "OpenClaw SQLite database or sidecar has non-canonical casing",
        )
        discovered.add(directory / entry.name)
    return discovered


def authoritative_sqlite_source_inventory(
    config: BackupConfig,
    agent_ids: tuple[str, ...] | None,
) -> dict[str, Any]:
    # New native captures own every retained canonical database. Explicit IDs
    # remain supported only for the historical v3 fixture/read contract.
    capture_retained = agent_ids is None
    agent_ids = agent_ids or ()
    retained_ids: list[str] = []
    expected_paths = authoritative_sqlite_source_paths(config, agent_ids)
    required_databases = {
        config.state_root / "state/openclaw.sqlite",
        *(
            config.agents_physical
            / agent_id
            / "agent/openclaw-agent.sqlite"
            for agent_id in agent_ids
        ),
    }
    state_device = config.state_root.lstat().st_dev
    state_directory = config.state_root / "state"
    require_physical_directory(state_directory, state_device)
    discovered = discover_sqlite_database_entries(
        state_directory,
        "openclaw.sqlite",
    )

    agents_root = config.agents_physical
    agents_device = agents_root.lstat().st_dev
    require_physical_directory(agents_root, agents_device)
    root_identities = [[str(root), root.stat().st_dev, root.stat().st_ino]
                       for root in (config.state_root, state_directory, agents_root)]
    with os.scandir(agents_root) as entries:
        agent_entries = sorted(entries, key=lambda row: os.fsencode(row.name))
    for entry in agent_entries:
        entry_info = entry.stat(follow_symlinks=False)
        require(not stat.S_ISLNK(entry_info.st_mode),
                "an agent storage root is a symbolic link")
        if not stat.S_ISDIR(entry_info.st_mode):
            continue
        agent_directory = agents_root / entry.name / "agent"
        if not os.path.lexists(agent_directory):
            continue
        agent_directory_info = agent_directory.lstat()
        require(not stat.S_ISLNK(agent_directory_info.st_mode),
                "an agent database directory is a symbolic link")
        if not stat.S_ISDIR(agent_directory_info.st_mode):
            continue
        entries = discover_sqlite_database_entries(agent_directory, "openclaw-agent.sqlite")
        if entries and capture_retained:
            require(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", entry.name) is not None,
                    "an agent storage identity is noncanonical")
            require(agent_directory / "openclaw-agent.sqlite" in entries,
                    "an authoritative SQLite sidecar has no database")
            retained_ids.append(entry.name)
        discovered.update(entries)

    if capture_retained:
        agent_ids = tuple(sorted(retained_ids))
        expected_paths = authoritative_sqlite_source_paths(config, agent_ids)

    require(
        required_databases <= discovered,
        "a configured OpenClaw SQLite database is missing",
    )
    require(
        discovered <= expected_paths,
        "an unconfigured OpenClaw SQLite database or sidecar is present",
    )
    for agent_id in agent_ids:
        for directory in (config.agents_physical / agent_id,
                          config.agents_physical / agent_id / "agent"):
            info = require_physical_directory(directory, agents_device)
            root_identities.append([str(directory), info.st_dev, info.st_ino])
    logical_bytes = 0
    database_identities: list[list[Any]] = []
    for path in sorted(discovered, key=lambda row: os.fsencode(str(row))):
        info = path.lstat()
        expected_device = (
            agents_device
            if absolute(path).is_relative_to(absolute(config.agents_physical))
            else state_device
        )
        require(
            stat.S_ISREG(info.st_mode)
            and not path.is_symlink()
            and info.st_dev == expected_device
            and info.st_nlink == 1,
            "an authoritative OpenClaw SQLite path is not a physical file",
        )
        logical_bytes += info.st_size
        if path.name in {"openclaw.sqlite", "openclaw-agent.sqlite"}:
            database_identities.append([str(path), info.st_dev, info.st_ino])
    peak_bytes = (
        RESTORE_METADATA_BASE_BYTES
        + SQLITE_SNAPSHOT_PEAK_SOURCE_MULTIPLIER * logical_bytes
    )
    archive_names = sorted(
        state_archive_name(config, path) for path in discovered
    )
    return {
        ("retained_agent_count" if capture_retained else "configured_agent_count"): len(agent_ids),
        "source_file_count": len(discovered),
        "source_logical_bytes": logical_bytes,
        "source_paths_sha256": sha256_bytes(canonical_json(archive_names)),
        "database_identities": database_identities,
        "root_identities": root_identities,
        "snapshot_peak_multiplier": (
            SQLITE_SNAPSHOT_PEAK_SOURCE_MULTIPLIER
        ),
        "snapshot_peak_headroom_bytes": peak_bytes,
    }


def state_archive_name(config: BackupConfig, path: Path) -> str:
    try:
        relative = path.relative_to(config.state_root)
    except ValueError:
        try:
            relative = Path("agents") / path.relative_to(
                config.agents_physical
            )
        except ValueError as exc:
            raise BackupError("SQLite state path escaped the state roots") from exc
    return (PurePosixPath(".openclaw") / relative.as_posix()).as_posix()


def is_authoritative_sqlite_archive_path(archive_name: str) -> bool:
    return (
        STATE_SQLITE_DATABASE_ARCHIVE_RE.fullmatch(archive_name.casefold())
        is not None
    )


def is_sqlite_snapshot_archive_member(archive_name: str) -> bool:
    root = SQLITE_SNAPSHOT_ARCHIVE_ROOT.as_posix()
    return archive_name == root or archive_name.startswith(root + "/")


def is_sqlite_snapshot_database_member(archive_name: str) -> bool:
    return (
        is_sqlite_snapshot_archive_member(archive_name)
        and PurePosixPath(archive_name).name == "database.sqlite"
    )


def safe_archive_name(value: str) -> str:
    name = PurePosixPath(value)
    require(
        value == name.as_posix()
        and value not in {"", "."}
        and not name.is_absolute()
        and ".." not in name.parts,
        "unsafe archive member path: {}".format(value),
    )
    return value


def member_probe_group(role: str, archive_name: str) -> str:
    if role == "state":
        if (
            archive_name == SQLITE_SNAPSHOT_ARCHIVE_ROOT.as_posix()
            or archive_name.startswith(
                SQLITE_SNAPSHOT_ARCHIVE_ROOT.as_posix() + "/"
            )
        ):
            return "sqlite-snapshots"
        if archive_name == ".openclaw/openclaw.json":
            return "state-internal"
        if archive_name.startswith(".openclaw/agents/main/sessions/"):
            return "sessions"
        if archive_name.startswith(".openclaw/browser/"):
            return "browser"
        if archive_name.startswith(".openclaw/media/"):
            return "media"
        return "state-other"
    if role == "policy":
        return "policy"
    if role == "git-remotes":
        return "git-remotes"
    raise BackupError("unknown archive role: {}".format(role))


def is_volatile_regular(path: Path, info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and path.name.endswith(VOLATILE_SUFFIXES)


def is_exact_update_wrapper_operational_log(archive_name: str) -> bool:
    return (
        UPDATE_WRAPPER_OPERATIONAL_LOG_ARCHIVE_RE.fullmatch(archive_name)
        is not None
    )


def is_explicit_browser_cache_subtree(archive_name: str) -> bool:
    path = PurePosixPath(archive_name)
    for user_data_root in BROWSER_MANAGED_USER_DATA_ARCHIVE_ROOTS:
        try:
            relative = path.relative_to(user_data_root)
        except ValueError:
            continue
        if relative in BROWSER_GLOBAL_CACHE_RELATIVES:
            return True
        parts = relative.parts
        if len(parts) < 2 or BROWSER_PROFILE_NAME_RE.fullmatch(
            parts[0]
        ) is None:
            return False
        profile_relative = PurePosixPath(*parts[1:])
        if (
            profile_relative in BROWSER_PROFILE_CACHE_RELATIVES
            or profile_relative
            in BROWSER_PROFILE_NESTED_CACHE_RELATIVES
        ):
            return True
        profile_parts = profile_relative.parts
        if (
            len(profile_parts) == 3
            and profile_parts[0] == "WebStorage"
            and profile_parts[1].isdecimal()
            and profile_parts[2] == "CacheStorage"
        ):
            return True
        if (
            len(profile_parts) >= 5
            and profile_parts[:2] == ("Storage", "ext")
            and BROWSER_EXTENSION_ID_RE.fullmatch(profile_parts[2])
            is not None
            and profile_parts[3] == "def"
            and PurePosixPath(*profile_parts[4:])
            in BROWSER_EXTENSION_CACHE_RELATIVES
        ):
            return True
        return False
    return False


def is_reconstructible_runtime_cache_subtree(archive_name: str) -> bool:
    return (
        PurePosixPath(archive_name)
        in RECONSTRUCTIBLE_RUNTIME_CACHE_ARCHIVE_ROOTS
    )


def is_reconstructible_runtime_cache_descendant(archive_name: str) -> bool:
    path = PurePosixPath(archive_name)
    return any(
        root in path.parents
        for root in RECONSTRUCTIBLE_RUNTIME_CACHE_ARCHIVE_ROOTS
    )


def open_directory_anchored(
    path_or_name: Path | str,
    *,
    expected_device: int,
    expected_info: os.stat_result,
    parent_fd: int | None = None,
) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path_or_name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        require(
            stat.S_ISDIR(opened.st_mode)
            and opened.st_dev == expected_device
            and (opened.st_dev, opened.st_ino)
            == (expected_info.st_dev, expected_info.st_ino),
            "source directory identity changed before anchored traversal: "
            "{}".format(path_or_name),
        )
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _walk_directory(
    *,
    directory: Path,
    directory_fd: int,
    archive_directory: PurePosixPath,
    expected_device: int,
    role: str,
    expansions: dict[Path, Expansion],
    exact_exclusions: frozenset[Path],
    metrics: WalkMetrics,
) -> Iterator[MemberSource]:
    try:
        with os.scandir(directory_fd) as iterator:
            children = sorted(
                iterator,
                key=lambda row: os.fsencode(row.name),
            )
    except OSError as exc:
        raise BackupError(
            "cannot enumerate required source directory {}: {}".format(
                directory,
                exc,
            )
        ) from exc
    for child in children:
        path = directory / child.name
        if path in exact_exclusions:
            metrics.excluded_exact += 1
            continue
        expansion = expansions.get(path)
        archive_component = (
            expansion.logical.name if expansion is not None else child.name
        )
        archive_name = safe_archive_name(
            (archive_directory / archive_component).as_posix()
        )
        require(
            role != "state"
            or not is_authoritative_sqlite_archive_path(archive_name),
            "state traversal reached an authoritative live SQLite path",
        )
        explicit_browser_cache = (
            role == "state"
            and is_explicit_browser_cache_subtree(archive_name)
        )
        reconstructible_runtime_cache = (
            role == "state"
            and is_reconstructible_runtime_cache_subtree(archive_name)
        )
        try:
            info = os.stat(
                child.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise BackupError(
                "cannot stat required source member {}: {}".format(path, exc)
            ) from exc
        # Root-owned update wrapper transcripts are operational diagnostics,
        # not state needed to recover OpenClaw. Exclude only a physical
        # same-device regular file at the wrapper's exact timestamped log
        # path; lookalikes, symlinks, directories, and every other unreadable
        # state member remain required and therefore fail closed.
        if (
            role == "state"
            and is_exact_update_wrapper_operational_log(archive_name)
            and stat.S_ISREG(info.st_mode)
        ):
            require(
                info.st_dev == expected_device,
                "update wrapper operational log crossed a device boundary: "
                "{}".format(path),
            )
            metrics.excluded_update_wrapper_operational_logs += 1
            metrics.excluded_update_wrapper_operational_log_paths.append(
                archive_name
            )
            continue
        # Chromium owns these derived cache trees while the browser is live.
        # A precise managed-layout match is not enough: no-follow stat must
        # also prove a physical same-device directory. Cache-like files and
        # symlinks remain ordinary required members. A proven cache directory
        # is excluded before descent, so its churning descendants are never
        # enumerated into the deterministic tar member contract.
        if explicit_browser_cache and stat.S_ISDIR(info.st_mode):
            require(
                info.st_dev == expected_device,
                "browser cache subtree crossed a device boundary: {}".format(
                    path
                ),
            )
            metrics.excluded_volatile += 1
            metrics.excluded_browser_cache_subtrees += 1
            metrics.excluded_browser_cache_paths.append(archive_name)
            continue
        # Bundled plugin dependencies are a version-keyed runtime cache, not
        # recovery state. Exclude only the exact top-level cache after a
        # no-follow stat proves that it is a physical same-device directory.
        # A file, symlink, or cross-device replacement remains visible and
        # therefore cannot be silently omitted from the recovery set.
        if reconstructible_runtime_cache and stat.S_ISDIR(info.st_mode):
            require(
                info.st_dev == expected_device,
                "reconstructible runtime cache crossed a device boundary: "
                "{}".format(path),
            )
            metrics.excluded_reconstructible_runtime_cache_subtrees += 1
            metrics.excluded_reconstructible_runtime_cache_paths.append(
                archive_name
            )
            continue
        if (
            role == "git-remotes"
            and child.name in GIT_REGULAR_BASENAME_EXCLUSIONS
            and stat.S_ISREG(info.st_mode)
        ):
            metrics.excluded_git_finder_metadata += 1
            continue
        if role == "state" and stat.S_ISSOCK(info.st_mode):
            metrics.excluded_unix_sockets += 1
            continue
        if expansion is not None:
            target_info = expansion.physical.lstat()
            require(
                stat.S_ISDIR(target_info.st_mode)
                and not expansion.physical.is_symlink()
                and target_info.st_dev == expansion.expected_device,
                "approved expansion target changed: {}".format(
                    expansion.physical
                ),
            )
            if stat.S_ISLNK(info.st_mode):
                require(
                    os.readlink(child.name, dir_fd=directory_fd)
                    == str(expansion.physical),
                    "approved expansion symlink target changed: {}".format(
                        path
                    ),
                )
                target_fd, opened_target = open_directory_anchored(
                    expansion.physical,
                    expected_device=expansion.expected_device,
                    expected_info=target_info,
                )
            else:
                require(
                    stat.S_ISDIR(info.st_mode)
                    and info.st_dev == expansion.expected_device
                    and os.path.samestat(info, target_info),
                    "approved expansion physical target changed: {}".format(
                        path
                    ),
                )
                target_fd, opened_target = open_directory_anchored(
                    child.name,
                    expected_device=expansion.expected_device,
                    expected_info=info,
                    parent_fd=directory_fd,
                )
            try:
                metrics.entries += 1
                metrics.directories += 1
                yield MemberSource(
                    source=expansion.physical,
                    archive_name=archive_name,
                    info=opened_target,
                    expected_device=expansion.expected_device,
                    probe_group=expansion.probe_group,
                )
                yield from _walk_directory(
                    directory=expansion.physical,
                    directory_fd=target_fd,
                    archive_directory=PurePosixPath(archive_name),
                    expected_device=expansion.expected_device,
                    role=role,
                    expansions=expansions,
                    exact_exclusions=exact_exclusions,
                    metrics=metrics,
                )
            finally:
                os.close(target_fd)
            continue

        if is_volatile_regular(path, info):
            metrics.excluded_volatile += 1
            continue
        if stat.S_ISDIR(info.st_mode):
            require(
                info.st_dev == expected_device,
                "source tree crossed a device boundary: {}".format(path),
            )
            child_fd, opened_child = open_directory_anchored(
                child.name,
                expected_device=expected_device,
                expected_info=info,
                parent_fd=directory_fd,
            )
            try:
                metrics.entries += 1
                metrics.directories += 1
                yield MemberSource(
                    source=path,
                    archive_name=archive_name,
                    info=opened_child,
                    expected_device=expected_device,
                    probe_group=None,
                )
                yield from _walk_directory(
                    directory=path,
                    directory_fd=child_fd,
                    archive_directory=PurePosixPath(archive_name),
                    expected_device=expected_device,
                    role=role,
                    expansions=expansions,
                    exact_exclusions=exact_exclusions,
                    metrics=metrics,
                )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(info.st_mode):
            require(
                info.st_dev == expected_device,
                "source file crossed a device boundary: {}".format(path),
            )
            metrics.entries += 1
            metrics.files += 1
            metrics.logical_bytes += info.st_size
            metrics.allocated_bytes += info.st_blocks * 512
            yield MemberSource(
                source=path,
                archive_name=archive_name,
                info=info,
                expected_device=expected_device,
                probe_group=member_probe_group(role, archive_name),
                parent_fd=directory_fd,
                entry_name=child.name,
            )
        elif stat.S_ISLNK(info.st_mode):
            metrics.entries += 1
            metrics.symlinks += 1
            yield MemberSource(
                source=path,
                archive_name=archive_name,
                info=info,
                expected_device=expected_device,
                probe_group=None,
                parent_fd=directory_fd,
                entry_name=child.name,
            )
        else:
            raise BackupError(
                "unsupported required source entry type: {}".format(path)
            )


def iter_root_members(
    root: RootInput,
    *,
    role: str,
    metrics: WalkMetrics,
) -> Iterator[MemberSource]:
    info = root.source.lstat()
    require(not root.source.is_symlink(), "archive source root is a symlink")
    require(
        info.st_dev == root.expected_device,
        "archive source root device mismatch: {}".format(root.source),
    )
    archive_name = safe_archive_name(root.archive_name.as_posix())
    if stat.S_ISDIR(info.st_mode):
        root_fd, opened_root = open_directory_anchored(
            root.source,
            expected_device=root.expected_device,
            expected_info=info,
        )
        try:
            metrics.entries += 1
            metrics.directories += 1
            yield MemberSource(
                source=root.source,
                archive_name=archive_name,
                info=opened_root,
                expected_device=root.expected_device,
                probe_group=None,
            )
            yield from _walk_directory(
                directory=root.source,
                directory_fd=root_fd,
                archive_directory=root.archive_name,
                expected_device=root.expected_device,
                role=role,
                expansions=root.expansions,
                exact_exclusions=root.exact_exclusions,
                metrics=metrics,
            )
        finally:
            os.close(root_fd)
    elif stat.S_ISREG(info.st_mode):
        require(
            not is_volatile_regular(root.source, info),
            "required top-level policy file has a volatile suffix",
        )
        parent_info = root.source.parent.lstat()
        parent_fd, _opened_parent = open_directory_anchored(
            root.source.parent,
            expected_device=root.expected_device,
            expected_info=parent_info,
        )
        try:
            anchored_info = os.stat(
                root.source.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            require(
                stat.S_ISREG(anchored_info.st_mode)
                and (anchored_info.st_dev, anchored_info.st_ino)
                == (info.st_dev, info.st_ino),
                "top-level source file identity changed: {}".format(
                    root.source
                ),
            )
            metrics.entries += 1
            metrics.files += 1
            metrics.logical_bytes += anchored_info.st_size
            metrics.allocated_bytes += anchored_info.st_blocks * 512
            yield MemberSource(
                source=root.source,
                archive_name=archive_name,
                info=anchored_info,
                expected_device=root.expected_device,
                probe_group=member_probe_group(role, archive_name),
                parent_fd=parent_fd,
                entry_name=root.source.name,
            )
        finally:
            os.close(parent_fd)
    else:
        raise BackupError(
            "archive source root has unsupported type: {}".format(root.source)
        )


def iter_archive_members(
    spec: ArchiveSpec,
    metrics: WalkMetrics,
) -> Iterator[MemberSource]:
    seen_casefold: set[str] = set()
    for root in sorted(
        spec.roots,
        key=lambda row: os.fsencode(row.archive_name.as_posix()),
    ):
        for member in iter_root_members(root, role=spec.role, metrics=metrics):
            folded = member.archive_name.casefold()
            require(
                folded not in seen_casefold,
                "case-folding archive member collision: {}".format(
                    member.archive_name
                ),
            )
            seen_casefold.add(folded)
            yield member


def build_archive_specs(
    config: BackupConfig,
    identity: VolumeIdentity,
    sqlite_snapshots: SqliteSnapshotSet | None = None,
    *,
    session_route: SessionStoreRoute | None = None,
) -> tuple[ArchiveSpec, ...]:
    state_device = config.state_root.lstat().st_dev
    selected_session_route = session_route or resolve_session_store_route(
        config,
        identity,
    )
    expansions = {
        config.agents_logical: Expansion(
            config.agents_logical,
            config.agents_physical,
            None,
            identity.device,
        ),
        config.browser_logical: Expansion(
            config.browser_logical,
            config.browser_physical,
            "browser",
            identity.device,
        ),
        config.media_logical: Expansion(
            config.media_logical,
            config.media_physical,
            "media",
            identity.device,
        ),
    }
    for expansion in tuple(expansions.values()):
        if absolute(expansion.physical).is_relative_to(
            absolute(config.state_root)
        ):
            expansions.setdefault(expansion.physical, expansion)
    if selected_session_route.layout == "legacy-uppercase-physical-v1":
        expansions[config.sessions_current_physical] = Expansion(
            config.sessions_current_physical,
            config.sessions_legacy_physical,
            "sessions",
            identity.device,
        )
    policy_roots = tuple(
        RootInput(
            source=config.workspace_physical / name,
            archive_name=PurePosixPath(name),
            expected_device=identity.device,
        )
        for name in (*POLICY_FILES, "runbook", "memory")
    )
    state_roots: tuple[RootInput, ...] = (
        RootInput(
            source=config.state_root,
            archive_name=PurePosixPath(".openclaw"),
            expected_device=state_device,
            expansions=expansions,
            exact_exclusions=(
                config.state_shadow_exclusions
                if sqlite_snapshots is None
                else config.state_shadow_exclusions
                | authoritative_sqlite_source_paths(
                    config,
                    sqlite_snapshots.agent_ids,
                )
            ),
        ),
    )
    state_required_members = {
        ".openclaw",
        ".openclaw/openclaw.json",
        ".openclaw/agents/main/sessions",
        ".openclaw/browser",
        ".openclaw/media",
    }
    state_required_probe_groups = {
        "state-internal",
        "sessions",
        "browser",
        "media",
    }
    if sqlite_snapshots is not None:
        reserved_source_path = (
            config.state_root
            / SQLITE_SNAPSHOT_ARCHIVE_ROOT.relative_to(
                PurePosixPath(".openclaw")
            ).as_posix()
        )
        require(
            not os.path.lexists(reserved_source_path),
            "live state collides with the reserved SQLite snapshot archive root",
        )
        state_roots += (
            RootInput(
                source=sqlite_snapshots.repository,
                archive_name=SQLITE_SNAPSHOT_ARCHIVE_ROOT,
                expected_device=identity.device,
            ),
        )
        state_required_members.add(SQLITE_SNAPSHOT_ARCHIVE_ROOT.as_posix())
        state_required_probe_groups.add("sqlite-snapshots")
        for snapshot_path in sqlite_snapshots.snapshot_paths:
            archive_root = SQLITE_SNAPSHOT_ARCHIVE_ROOT / snapshot_path.name
            state_required_members.update(
                {
                    archive_root.as_posix(),
                    (archive_root / "manifest.json").as_posix(),
                    (archive_root / "database.sqlite").as_posix(),
                }
            )
    else:
        # beta.3 persists cron jobs in the global SQLite database. Preserve a
        # legacy jobs.json through ordinary traversal when present, but require
        # it only for historical no-snapshot archive specifications.
        state_required_members.add(".openclaw/cron/jobs.json")
    return (
        ArchiveSpec(
            filename="git-remotes.tgz",
            role="git-remotes",
            roots=(
                RootInput(
                    source=config.git_remotes_physical,
                    archive_name=PurePosixPath("git-remotes"),
                    expected_device=identity.device,
                ),
            ),
            required_members=frozenset({"git-remotes"}),
            required_probe_groups=frozenset({"git-remotes"}),
        ),
        ArchiveSpec(
            filename="openclaw-state.tgz",
            role="state",
            roots=state_roots,
            required_members=frozenset(state_required_members),
            required_probe_groups=frozenset(state_required_probe_groups),
        ),
        ArchiveSpec(
            filename="openclaw-workspace-policy.tgz",
            role="policy",
            roots=policy_roots,
            required_members=frozenset(
                {*POLICY_FILES, "runbook", "memory"}
            ),
            required_probe_groups=frozenset({"policy"}),
        ),
    )


def estimate_archives(
    specs: Iterable[ArchiveSpec],
) -> dict[str, Any]:
    archives: dict[str, Any] = {}
    total_upper = 0
    full_restore_payload_upper = 0
    probe_payload_upper = 0
    sqlite_snapshot_selective_restore_upper = 0
    sqlite_verifier_copy_upper = 0
    restore_entry_count = 0
    for spec in specs:
        metrics = WalkMetrics()
        tar_upper = 10 * 1024
        for member in iter_archive_members(spec, metrics):
            tar_info = tarinfo_for_member(member)
            tar_upper += len(
                tar_info.tobuf(
                    format=tarfile.PAX_FORMAT,
                    encoding="utf-8",
                    errors="surrogateescape",
                )
            )
            if stat.S_ISREG(member.info.st_mode):
                tar_upper += (
                    (member.info.st_size + tarfile.BLOCKSIZE - 1)
                    // tarfile.BLOCKSIZE
                ) * tarfile.BLOCKSIZE
                if (
                    spec.role == "state"
                    and is_sqlite_snapshot_archive_member(
                        member.archive_name
                    )
                ):
                    sqlite_snapshot_selective_restore_upper += (
                        member.info.st_size
                    )
                    if is_sqlite_snapshot_database_member(
                        member.archive_name
                    ):
                        sqlite_verifier_copy_upper = max(
                            sqlite_verifier_copy_upper,
                            member.info.st_size,
                        )
        # `TarInfo.tobuf(PAX_FORMAT)` includes every normal/PAX header. The
        # remaining bound is the padded regular-file payload plus the tar
        # end record. One-percent gzip framing/deflate expansion allowance is
        # substantially above zlib's documented compress-bound overhead.
        gzip_upper = tar_upper + (tar_upper + 99) // 100 + 64 * 1024
        archives[spec.filename] = {
            "source": metrics.as_dict(),
            "tar_upper_bytes": tar_upper,
            "gzip_upper_bytes": gzip_upper,
        }
        total_upper += gzip_upper
        probe_payload_upper += (
            len(spec.required_probe_groups) * PROBE_MAX_BYTES
        )
        if spec.role in {"policy", "git-remotes"}:
            full_restore_payload_upper += metrics.logical_bytes
            restore_entry_count += metrics.entries
    restore_metadata_upper = (
        RESTORE_METADATA_BASE_BYTES
        + restore_entry_count * RESTORE_METADATA_PER_ENTRY_BYTES
    )
    restore_budget = (
        full_restore_payload_upper
        + probe_payload_upper
        + sqlite_snapshot_selective_restore_upper
        + sqlite_verifier_copy_upper
        + restore_metadata_upper
    )
    return {
        "archives": archives,
        "archive_output_upper_bytes": total_upper,
        "restore_probe_budget_bytes": restore_budget,
        "restore_budget_components": {
            "full_restore_payload_upper_bytes": (
                full_restore_payload_upper
            ),
            "probe_payload_upper_bytes": probe_payload_upper,
            "sqlite_snapshot_selective_restore_bytes": (
                sqlite_snapshot_selective_restore_upper
            ),
            "sqlite_snapshot_verification_transient_bytes": (
                sqlite_verifier_copy_upper
            ),
            "metadata_upper_bytes": restore_metadata_upper,
            "full_restore_entry_count": restore_entry_count,
        },
    }


class HashingReader:
    def __init__(
        self,
        handle: Any,
        *,
        headroom_check: Callable[[], None] | None = None,
    ):
        self.handle = handle
        self.headroom_check = headroom_check
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        if self.headroom_check is not None:
            self.headroom_check()
        payload = self.handle.read(size)
        if payload:
            self.digest.update(payload)
            self.bytes_read += len(payload)
        return payload

    def hexdigest(self) -> str:
        return self.digest.hexdigest()


def tarinfo_for_member(
    member: MemberSource,
    *,
    file_info: os.stat_result | None = None,
) -> tarfile.TarInfo:
    info = file_info or member.info
    tar_info = tarfile.TarInfo(member.archive_name)
    tar_info.mode = stat.S_IMODE(info.st_mode)
    tar_info.uid = info.st_uid
    tar_info.gid = info.st_gid
    tar_info.uname = ""
    tar_info.gname = ""
    tar_info.mtime = int(info.st_mtime)
    if stat.S_ISDIR(info.st_mode):
        tar_info.type = tarfile.DIRTYPE
        tar_info.size = 0
    elif stat.S_ISREG(info.st_mode):
        tar_info.type = tarfile.REGTYPE
        tar_info.size = info.st_size
    elif stat.S_ISLNK(info.st_mode):
        tar_info.type = tarfile.SYMTYPE
        if member.parent_fd is not None and member.entry_name is not None:
            before = os.stat(
                member.entry_name,
                dir_fd=member.parent_fd,
                follow_symlinks=False,
            )
            require(
                stat.S_ISLNK(before.st_mode)
                and (before.st_dev, before.st_ino)
                == (member.info.st_dev, member.info.st_ino),
                "source symlink identity changed before read: {}".format(
                    member.source
                ),
            )
            tar_info.linkname = os.readlink(
                member.entry_name,
                dir_fd=member.parent_fd,
            )
            after = os.stat(
                member.entry_name,
                dir_fd=member.parent_fd,
                follow_symlinks=False,
            )
            require(
                (after.st_dev, after.st_ino, after.st_mtime_ns)
                == (before.st_dev, before.st_ino, before.st_mtime_ns),
                "source symlink changed during read: {}".format(
                    member.source
                ),
            )
        else:
            tar_info.linkname = os.readlink(member.source)
        tar_info.size = 0
    else:
        raise BackupError(
            "unsupported archive member type: {}".format(member.source)
        )
    return tar_info


def member_contract_record(info: tarfile.TarInfo) -> dict[str, Any]:
    if info.isdir():
        kind = "directory"
    elif info.isfile():
        kind = "file"
    elif info.issym():
        kind = "symlink"
    else:
        raise BackupError("unsupported tar member type: {}".format(info.name))
    record: dict[str, Any] = {
        "name": safe_archive_name(info.name),
        "kind": kind,
        "mode": info.mode,
        "uid": info.uid,
        "gid": info.gid,
        "mtime": int(info.mtime),
        "size": info.size,
    }
    if kind == "symlink":
        record["linkname"] = info.linkname
    return record


def choose_probe(
    probes: dict[str, dict[str, Any]],
    *,
    group: str | None,
    member_name: str,
    size: int,
    sha256: str,
) -> None:
    if group is None or size > PROBE_MAX_BYTES:
        return
    preferred = (
        group == "state-internal"
        and member_name == ".openclaw/openclaw.json"
    ) or (group == "policy" and member_name == "AGENTS.md")
    if group not in probes or preferred:
        probes[group] = {
            "member": member_name,
            "size": size,
            "sha256": sha256,
        }


def create_archive(
    destination: Path,
    spec: ArchiveSpec,
    *,
    expected_device: int,
    minimum_stream_free_bytes: int = 0,
) -> dict[str, Any]:
    require(
        not os.path.lexists(destination),
        "archive destination already exists: {}".format(destination),
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    metrics = WalkMetrics()
    member_digest = hashlib.sha256()
    probes: dict[str, dict[str, Any]] = {}
    source_churn: list[str] = []
    sqlite_snapshot_selective_restore_bytes = 0
    sqlite_verifier_copy_bytes = 0
    def require_stream_headroom() -> None:
        require(
            shutil.disk_usage(destination.parent).free
            >= minimum_stream_free_bytes,
            "OWC free space crossed the streaming archive floor",
        )

    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=GZIP_COMPRESSLEVEL,
                fileobj=raw,
                mtime=0,
            ) as compressed:
                with tarfile.open(
                    mode="w|",
                    fileobj=compressed,
                    format=tarfile.PAX_FORMAT,
                    dereference=False,
                ) as archive:
                    for member in iter_archive_members(spec, metrics):
                        require_stream_headroom()
                        if stat.S_ISREG(member.info.st_mode):
                            open_flags = os.O_RDONLY
                            if hasattr(os, "O_NOFOLLOW"):
                                open_flags |= os.O_NOFOLLOW
                            file_descriptor = os.open(
                                (
                                    member.entry_name
                                    if member.entry_name is not None
                                    else member.source
                                ),
                                open_flags,
                                dir_fd=member.parent_fd,
                            )
                            try:
                                opened = os.fstat(file_descriptor)
                                require(
                                    stat.S_ISREG(opened.st_mode)
                                    and (
                                        opened.st_dev,
                                        opened.st_ino,
                                    )
                                    == (
                                        member.info.st_dev,
                                        member.info.st_ino,
                                    ),
                                    "source file identity changed before read: "
                                    "{}".format(member.source),
                                )
                                tar_info = tarinfo_for_member(
                                    member,
                                    file_info=opened,
                                )
                                metrics.logical_bytes += (
                                    opened.st_size - member.info.st_size
                                )
                                metrics.allocated_bytes += (
                                    opened.st_blocks - member.info.st_blocks
                                ) * 512
                                if (
                                    spec.role == "state"
                                    and is_sqlite_snapshot_archive_member(
                                        member.archive_name
                                    )
                                ):
                                    sqlite_snapshot_selective_restore_bytes += (
                                        opened.st_size
                                    )
                                    if is_sqlite_snapshot_database_member(
                                        member.archive_name
                                    ):
                                        sqlite_verifier_copy_bytes = max(
                                            sqlite_verifier_copy_bytes,
                                            opened.st_size,
                                        )
                                member_digest.update(
                                    canonical_json(
                                        member_contract_record(tar_info)
                                    )
                                    + b"\n"
                                )
                                with os.fdopen(
                                    file_descriptor,
                                    "rb",
                                    closefd=False,
                                ) as source_handle:
                                    hashing = HashingReader(
                                        source_handle,
                                        headroom_check=(
                                            require_stream_headroom
                                        ),
                                    )
                                    archive.addfile(tar_info, hashing)
                                require(
                                    hashing.bytes_read == opened.st_size,
                                    "source file short read: {}".format(
                                        member.source
                                    ),
                                )
                                choose_probe(
                                    probes,
                                    group=member.probe_group,
                                    member_name=member.archive_name,
                                    size=opened.st_size,
                                    sha256=hashing.hexdigest(),
                                )
                                after = os.fstat(file_descriptor)
                                if (
                                    after.st_size,
                                    after.st_mtime_ns,
                                ) != (
                                    opened.st_size,
                                    opened.st_mtime_ns,
                                ):
                                    source_churn.append(member.archive_name)
                            finally:
                                os.close(file_descriptor)
                        else:
                            tar_info = tarinfo_for_member(member)
                            member_digest.update(
                                canonical_json(
                                    member_contract_record(tar_info)
                                )
                                + b"\n"
                            )
                            archive.addfile(tar_info)
            raw.flush()
            os.fsync(raw.fileno())
    except Exception:
        try:
            os.unlink(destination)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)

    info = destination.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and not destination.is_symlink()
        and info.st_dev == expected_device,
        "created archive is not a physical OWC file",
    )
    require(
        stat.S_IMODE(info.st_mode) == 0o600,
        "created archive permissions are not 0600",
    )
    missing_probes = spec.required_probe_groups - set(probes)
    require(
        not missing_probes,
        "archive has no bounded restore candidate for: {}".format(
            ",".join(sorted(missing_probes))
        ),
    )
    return {
        "name": spec.filename,
        "role": spec.role,
        "sha256": sha256_file(destination),
        "logical_bytes": info.st_size,
        "allocated_bytes": info.st_blocks * 512,
        "archive_to_source_logical_ratio": (
            round(info.st_size / metrics.logical_bytes, 6)
            if metrics.logical_bytes
            else None
        ),
        "source_to_archive_logical_ratio": (
            round(metrics.logical_bytes / info.st_size, 6)
            if info.st_size
            else None
        ),
        "member_contract_sha256": member_digest.hexdigest(),
        "source": metrics.as_dict(),
        "capture_consistency": CAPTURE_CONSISTENCY,
        "source_tree_atomic": False,
        "source_churn_count": len(source_churn),
        "source_churn_paths_sha256": sha256_bytes(
            canonical_json(source_churn)
        ),
        "restore_candidates": probes,
        "sqlite_snapshot_selective_restore_bytes": (
            sqlite_snapshot_selective_restore_bytes
        ),
        "sqlite_snapshot_verification_transient_bytes": (
            sqlite_verifier_copy_bytes
        ),
    }


def restore_budget_from_archive_records(
    archive_records: Iterable[dict[str, Any]],
) -> dict[str, int]:
    records = list(archive_records)
    full_restore_records = [
        row
        for row in records
        if row.get("role") in {"policy", "git-remotes"}
        or row.get("format") == NATIVE_STATE_FORMAT
    ]
    full_restore_payload = sum(
        int(row["source"]["logical_bytes"])
        for row in full_restore_records
    )
    full_restore_entries = sum(
        int(row["source"]["entries"])
        for row in full_restore_records
    )
    probe_payload = sum(
        int(candidate["size"])
        for row in records
        for candidate in row.get("restore_candidates", {}).values()
    )
    sqlite_snapshot_payload = sum(
        int(row.get("sqlite_snapshot_selective_restore_bytes", 0))
        for row in records
    )
    sqlite_verification_transient = sum(
        int(row.get("sqlite_snapshot_verification_transient_bytes", 0))
        for row in records
    )
    metadata = (
        RESTORE_METADATA_BASE_BYTES
        + full_restore_entries * RESTORE_METADATA_PER_ENTRY_BYTES
    )
    return {
        "full_restore_payload_bytes": full_restore_payload,
        "probe_payload_bytes": probe_payload,
        "sqlite_snapshot_selective_restore_bytes": sqlite_snapshot_payload,
        "sqlite_snapshot_verification_transient_bytes": (
            sqlite_verification_transient
        ),
        "metadata_bytes": metadata,
        "full_restore_entry_count": full_restore_entries,
        "total_bytes": (
            full_restore_payload
            + probe_payload
            + sqlite_snapshot_payload
            + sqlite_verification_transient
            + metadata
        ),
    }


def verify_member_scope(spec: ArchiveSpec, names: set[str]) -> None:
    require(
        spec.required_members <= names,
        "archive is missing required members: {}".format(
            ",".join(sorted(spec.required_members - names))
        ),
    )
    if spec.role == "state":
        require(
            all(name == ".openclaw" or name.startswith(".openclaw/") for name in names),
            "state archive contains a member outside .openclaw",
        )
        require(
            not any(
                is_authoritative_sqlite_archive_path(name)
                for name in names
            ),
            "state archive contains an authoritative live SQLite path",
        )
        forbidden = (
            ".openclaw/agents/main/sessions.pre-owc-openclaw-sessions",
            ".openclaw/browser.pre-owc-openclaw-browser",
            ".openclaw/media.pre-owc-openclaw-media",
        )
        for prefix in forbidden:
            require(
                not any(
                    name == prefix or name.startswith(prefix + "/")
                    for name in names
                ),
                "state archive contains excluded pre-OWC shadow: {}".format(
                    prefix
                ),
            )
        require(
            not any(
                is_reconstructible_runtime_cache_descendant(name)
                for name in names
            ),
            "state archive contains a reconstructible runtime cache descendant",
        )
    elif spec.role == "policy":
        allowed_roots = frozenset({*POLICY_FILES, "runbook", "memory"})
        require(
            all(name.split("/", 1)[0] in allowed_roots for name in names),
            "policy archive contains an unselected workspace member",
        )
    elif spec.role == "git-remotes":
        require(
            all(name == "git-remotes" or name.startswith("git-remotes/") for name in names),
            "git-remotes archive contains a member outside git-remotes",
        )


def safe_restore_target(root: Path, archive_name: str) -> Path:
    safe_archive_name(archive_name)
    current = root
    parts = PurePosixPath(archive_name).parts
    for part in parts[:-1]:
        current = current / part
        if os.path.lexists(current):
            info = current.lstat()
            require(
                stat.S_ISDIR(info.st_mode) and not current.is_symlink(),
                "restore parent is not a physical directory: {}".format(
                    current
                ),
            )
        else:
            current.mkdir(mode=0o700)
    target = current / parts[-1]
    require(
        absolute(target).is_relative_to(absolute(root)),
        "restore target escapes its isolated root",
    )
    return target


def create_full_restore_member(
    root: Path,
    member: tarfile.TarInfo,
) -> tuple[Path, Any | None]:
    target = safe_restore_target(root, member.name)
    require(
        not os.path.lexists(target),
        "isolated restore target collision: {}".format(target),
    )
    if member.isdir():
        target.mkdir(mode=0o700)
        return target, None
    if member.issym():
        os.symlink(member.linkname, target)
        return target, None
    require(member.isfile(), "unsupported full-restore member type")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return target, os.fdopen(descriptor, "wb")


def verify_restored_bare_repositories(
    restored_root: Path,
    *,
    config: BackupConfig,
) -> dict[str, Any]:
    repository_root = restored_root / "git-remotes"
    require(
        repository_root.is_dir() and not repository_root.is_symlink(),
        "restored git-remotes root is missing or unsafe",
    )
    repositories: list[Path] = []
    for directory, child_directories, files in os.walk(
        repository_root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        current_info = current.lstat()
        require(
            stat.S_ISDIR(current_info.st_mode) and not current.is_symlink(),
            "restored git tree contains a non-physical directory: {}".format(
                current.relative_to(repository_root)
            ),
        )
        for name in [*child_directories, *files]:
            child = current / name
            child_info = child.lstat()
            require(
                not stat.S_ISLNK(child_info.st_mode),
                "restored git tree contains a symlink and is not isolated: "
                "{}".format(child.relative_to(repository_root)),
            )
        head = current / "HEAD"
        config_file = current / "config"
        objects = current / "objects"
        refs = current / "refs"
        looks_like_bare = (
            current != repository_root
            and head.is_file()
            and config_file.is_file()
            and objects.is_dir()
            and refs.is_dir()
        )
        if looks_like_bare:
            repositories.append(current)
    require(repositories, "restored git archive contains no bare repository")
    relative_names: list[str] = []
    for repository in sorted(
        repositories,
        key=lambda path: os.fsencode(path.relative_to(repository_root).as_posix()),
    ):
        head = repository / "HEAD"
        config_file = repository / "config"
        objects = repository / "objects"
        refs = repository / "refs"
        require(
            stat.S_ISREG(head.lstat().st_mode)
            and stat.S_ISREG(config_file.lstat().st_mode)
            and stat.S_ISDIR(objects.lstat().st_mode)
            and stat.S_ISDIR(refs.lstat().st_mode),
            "restored bare repository control/object graph is incomplete: "
            "{}".format(repository.relative_to(repository_root)),
        )
        alternates = repository / "objects/info/alternates"
        require(
            not os.path.lexists(alternates),
            "restored bare repository depends on external object alternates: "
            "{}".format(repository.relative_to(repository_root)),
        )
        git_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        git_environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C",
            }
        )
        proc = subprocess.run(
            [
                str(config.git_binary),
                "--no-replace-objects",
                "--git-dir",
                str(repository),
                "fsck",
                "--full",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_environment,
            check=False,
            timeout=300,
        )
        require(
            proc.returncode == 0,
            "git fsck failed for restored bare repository {}: {}".format(
                repository.relative_to(repository_root),
                proc.stderr.decode(errors="replace").strip(),
            ),
        )
        relative_names.append(
            repository.relative_to(repository_root).as_posix()
        )
    return {
        "repository_count": len(relative_names),
        "repository_names_sha256": sha256_bytes(
            canonical_json(relative_names)
        ),
        "command": "git --git-dir <isolated-restored-repo> fsck --full",
        "result": "verified",
    }


def validate_verified_sqlite_snapshot(
    payload: Any,
    *,
    snapshot_path: Path,
    role: str,
    agent_id: str | None,
    expected_device: int,
) -> dict[str, Any]:
    require(
        isinstance(payload, dict)
        and payload.get("ok") is True
        and isinstance(payload.get("snapshotPath"), str)
        and isinstance(payload.get("manifest"), dict),
        "OpenClaw SQLite verification result contract mismatch",
    )
    expected_path = absolute(snapshot_path.resolve(strict=True))
    returned_path = Path(payload["snapshotPath"])
    require(
        returned_path.is_absolute()
        and absolute(returned_path.resolve(strict=True)) == expected_path,
        "OpenClaw SQLite verifier returned a different snapshot path",
    )
    snapshot_info = expected_path.lstat()
    require(
        stat.S_ISDIR(snapshot_info.st_mode)
        and not expected_path.is_symlink()
        and snapshot_info.st_dev == expected_device
        and stat.S_IMODE(snapshot_info.st_mode) == 0o700,
        "extracted SQLite snapshot is not a private physical directory",
    )
    require(
        set(child.name for child in expected_path.iterdir())
        == {"manifest.json", "database.sqlite"},
        "extracted SQLite snapshot has unexpected contents",
    )
    manifest_path = expected_path / "manifest.json"
    artifact_path = expected_path / "database.sqlite"
    for member_path in (manifest_path, artifact_path):
        member_info = member_path.lstat()
        require(
            stat.S_ISREG(member_info.st_mode)
            and not member_path.is_symlink()
            and member_info.st_dev == expected_device
            and member_info.st_nlink == 1
            and stat.S_IMODE(member_info.st_mode) == 0o600,
            "extracted SQLite snapshot member is not a private physical file",
        )
    try:
        on_disk_manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(
            "packaged SQLite snapshot manifest is unreadable"
        ) from exc
    require(
        isinstance(on_disk_manifest, dict)
        and on_disk_manifest == payload["manifest"],
        "packaged SQLite manifest differs from the stock verifier result",
    )
    manifest = on_disk_manifest
    database = manifest.get("database")
    artifact = manifest.get("artifact")
    expected_database_keys = (
        {"role", "basename", "userVersion", "agentId"}
        if role == "agent"
        else {"role", "basename", "userVersion"}
    )
    require(
        set(manifest)
        == {
            "schemaVersion",
            "snapshotId",
            "createdAt",
            "database",
            "artifact",
        }
        and manifest.get("schemaVersion") == 1
        and manifest.get("snapshotId") == expected_path.name
        and isinstance(manifest.get("createdAt"), str)
        and isinstance(database, dict)
        and set(database) == expected_database_keys
        and isinstance(artifact, dict)
        and set(artifact) == {"path", "sha256", "sizeBytes"}
        and artifact.get("path") == "database.sqlite"
        and isinstance(artifact.get("sizeBytes"), int)
        and not isinstance(artifact.get("sizeBytes"), bool)
        and artifact["sizeBytes"] >= 0
        and isinstance(artifact.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is not None,
        "packaged SQLite snapshot manifest contract mismatch",
    )
    expected_basename = (
        "openclaw.sqlite" if role == "global" else "openclaw-agent.sqlite"
    )
    require(
        database.get("role") == role
        and database.get("basename") == expected_basename
        and isinstance(database.get("userVersion"), int)
        and not isinstance(database.get("userVersion"), bool)
        and database["userVersion"] >= 0,
        "packaged SQLite snapshot database identity mismatch",
    )
    if role == "agent":
        require(
            database.get("agentId") == agent_id,
            "packaged SQLite snapshot agent identity mismatch",
        )
    else:
        require(
            agent_id is None,
            "packaged global SQLite snapshot carries an agent identity",
        )
    require(
        artifact_path.stat().st_size == artifact["sizeBytes"]
        and sha256_file(artifact_path) == artifact["sha256"],
        "packaged SQLite artifact differs from its verified manifest",
    )
    return {
        "snapshot_id": expected_path.name,
        "role": role,
        "agent_id": agent_id,
        "artifact_size_bytes": artifact["sizeBytes"],
        "artifact_sha256": artifact["sha256"],
        "manifest_sha256": sha256_file(manifest_path),
        "result": "verified",
    }


def verify_packaged_sqlite_snapshots(
    repository: Path,
    sqlite_snapshots: SqliteSnapshotSet,
    *,
    config: BackupConfig,
    expected_device: int,
) -> list[dict[str, Any]]:
    repository_info = repository.lstat()
    require(
        stat.S_ISDIR(repository_info.st_mode)
        and not repository.is_symlink()
        and repository_info.st_dev == expected_device
        and stat.S_IMODE(repository_info.st_mode) == 0o700,
        "extracted SQLite repository is not private and physical",
    )
    expected_rows = sqlite_snapshots.expected_snapshots()
    require(
        {child.name for child in repository.iterdir()}
        == {row["snapshot_id"] for row in expected_rows},
        "packaged SQLite repository snapshot set mismatch",
    )
    cli = resolve_openclaw_cli(config)
    verification: list[dict[str, Any]] = []
    for row in expected_rows:
        snapshot_path = repository / row["snapshot_id"]
        payload = run_openclaw_json(
            cli,
            config,
            (
                "backup",
                "sqlite",
                "verify",
                str(snapshot_path),
                "--json",
            ),
            label="backup sqlite verify",
            timeout_seconds=SQLITE_INSPECT_TIMEOUT_SECONDS,
        )
        verification.append(
            validate_verified_sqlite_snapshot(
                payload,
                snapshot_path=snapshot_path,
                role=row["role"],
                agent_id=row["agent_id"],
                expected_device=expected_device,
            )
        )
    return verification


def verify_archive(
    path: Path,
    spec: ArchiveSpec,
    expected: dict[str, Any],
    *,
    probe_root: Path,
    expected_device: int,
    config: BackupConfig,
    sqlite_snapshots: SqliteSnapshotSet | None = None,
) -> dict[str, Any]:
    if spec.role == "state" and expected.get("format") == NATIVE_STATE_FORMAT:
        return verify_native_state_archive(
            path, expected, config=config, probe_root=probe_root,
            expected_device=expected_device,
        )
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and not path.is_symlink()
        and info.st_dev == expected_device,
        "archive verification path is not a physical OWC file",
    )
    require(stat.S_IMODE(info.st_mode) == 0o600, "archive mode is not 0600")
    require(info.st_size == expected["logical_bytes"], "archive size mismatch")
    require(sha256_file(path) == expected["sha256"], "archive SHA-256 mismatch")

    names: set[str] = set()
    folded_names: set[str] = set()
    member_digest = hashlib.sha256()
    restored: dict[str, dict[str, Any]] = {}
    expected_candidates = expected["restore_candidates"]
    by_member = {
        str(row["member"]): (group, row)
        for group, row in expected_candidates.items()
    }
    full_restore_root: Path | None = None
    full_restore_files = 0
    if spec.role in {"policy", "git-remotes"}:
        full_restore_root = (
            probe_root
            / "{}-full".format(spec.filename.removesuffix(".tgz"))
        )
        ensure_private_directory(full_restore_root)
    sqlite_restore_root: Path | None = None
    expected_sqlite_members: set[str] = set()
    observed_sqlite_members: set[str] = set()
    if sqlite_snapshots is not None:
        require(
            spec.role == "state",
            "SQLite snapshot verification was requested for a non-state archive",
        )
        sqlite_restore_root = probe_root / "packaged-sqlite-snapshots"
        ensure_private_directory(sqlite_restore_root)
        expected_sqlite_members.add(SQLITE_SNAPSHOT_ARCHIVE_ROOT.as_posix())
        for row in sqlite_snapshots.expected_snapshots():
            archive_root = (
                SQLITE_SNAPSHOT_ARCHIVE_ROOT / row["snapshot_id"]
            )
            expected_sqlite_members.update(
                {
                    archive_root.as_posix(),
                    (archive_root / "manifest.json").as_posix(),
                    (archive_root / "database.sqlite").as_posix(),
                }
            )
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                name = safe_archive_name(member.name)
                folded = name.casefold()
                require(
                    folded not in folded_names,
                    "archive case-folding collision: {}".format(name),
                )
                folded_names.add(folded)
                names.add(name)
                record = member_contract_record(member)
                member_digest.update(canonical_json(record) + b"\n")
                require(
                    member.isdir() or member.isfile() or member.issym(),
                    "archive contains an unsupported member type: {}".format(
                        name
                    ),
                )
                if (
                    spec.role == "state"
                    and is_reconstructible_runtime_cache_subtree(name)
                ):
                    require(
                        member.isfile() or member.issym(),
                        "state archive contains the reconstructible runtime "
                        "cache root as a directory",
                    )
                full_restore_handle = None
                if full_restore_root is not None:
                    _full_target, full_restore_handle = (
                        create_full_restore_member(
                            full_restore_root,
                            member,
                        )
                    )
                    if member.isfile():
                        full_restore_files += 1
                sqlite_restore_handle = None
                if (
                    sqlite_restore_root is not None
                    and (
                        name == SQLITE_SNAPSHOT_ARCHIVE_ROOT.as_posix()
                        or name.startswith(
                            SQLITE_SNAPSHOT_ARCHIVE_ROOT.as_posix() + "/"
                        )
                    )
                ):
                    require(
                        name in expected_sqlite_members,
                        "state archive contains an unexpected SQLite snapshot member",
                    )
                    observed_sqlite_members.add(name)
                    if name == SQLITE_SNAPSHOT_ARCHIVE_ROOT.as_posix():
                        require(
                            member.isdir(),
                            "SQLite snapshot archive root is not a directory",
                        )
                    else:
                        relative = PurePosixPath(name).relative_to(
                            SQLITE_SNAPSHOT_ARCHIVE_ROOT
                        )
                        target = sqlite_restore_root.joinpath(*relative.parts)
                        if member.isdir():
                            require(
                                len(relative.parts) == 1,
                                "SQLite snapshot archive has an unexpected directory",
                            )
                            target.mkdir(mode=0o700)
                        else:
                            require(
                                member.isfile()
                                and len(relative.parts) == 2
                                and relative.parts[-1]
                                in {"manifest.json", "database.sqlite"},
                                "SQLite snapshot archive member type is unsafe",
                            )
                            descriptor = os.open(
                                target,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                            )
                            os.fchmod(descriptor, 0o600)
                            sqlite_restore_handle = os.fdopen(
                                descriptor,
                                "wb",
                            )
                if member.isfile():
                    extracted = archive.extractfile(member)
                    require(
                        extracted is not None,
                        "archive regular member cannot be read: {}".format(
                            name
                        ),
                    )
                    digest = hashlib.sha256()
                    bytes_read = 0
                    candidate = by_member.get(name)
                    probe_handle = None
                    if candidate is not None:
                        group, _expected_candidate = candidate
                        probe_path = (
                            probe_root
                            / spec.filename.removesuffix(".tgz")
                            / "{}.probe".format(group)
                        )
                        probe_path.parent.mkdir(
                            mode=0o700,
                            parents=True,
                            exist_ok=True,
                        )
                        probe_descriptor = os.open(
                            probe_path,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                        )
                        probe_handle = os.fdopen(probe_descriptor, "wb")
                    try:
                        while True:
                            block = extracted.read(1024 * 1024)
                            if not block:
                                break
                            digest.update(block)
                            bytes_read += len(block)
                            if probe_handle is not None:
                                probe_handle.write(block)
                            if full_restore_handle is not None:
                                full_restore_handle.write(block)
                            if sqlite_restore_handle is not None:
                                sqlite_restore_handle.write(block)
                        if probe_handle is not None:
                            probe_handle.flush()
                            os.fsync(probe_handle.fileno())
                        if full_restore_handle is not None:
                            full_restore_handle.flush()
                            os.fsync(full_restore_handle.fileno())
                        if sqlite_restore_handle is not None:
                            sqlite_restore_handle.flush()
                            os.fsync(sqlite_restore_handle.fileno())
                    finally:
                        if probe_handle is not None:
                            probe_handle.close()
                        if full_restore_handle is not None:
                            full_restore_handle.close()
                        if sqlite_restore_handle is not None:
                            sqlite_restore_handle.close()
                    require(
                        bytes_read == member.size,
                        "archive member size readback mismatch: {}".format(
                            name
                        ),
                    )
                    if candidate is not None:
                        group, expected_candidate = candidate
                        require(
                            bytes_read == expected_candidate["size"]
                            and digest.hexdigest()
                            == expected_candidate["sha256"],
                            "restore probe mismatch: {}:{}".format(
                                spec.filename,
                                group,
                            ),
                        )
                        restored[group] = {
                            "member": name,
                            "size": bytes_read,
                            "sha256": digest.hexdigest(),
                            "result": "verified",
                        }
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise BackupError(
            "archive decompression/member verification failed: {}: {}".format(
                path,
                exc,
            )
        ) from exc

    require(
        member_digest.hexdigest() == expected["member_contract_sha256"],
        "archive member contract digest mismatch",
    )
    verify_member_scope(spec, names)
    sqlite_verification: list[dict[str, Any]] = []
    if sqlite_restore_root is not None:
        require(
            observed_sqlite_members == expected_sqlite_members,
            "packaged SQLite snapshot member coverage mismatch",
        )
        require(sqlite_snapshots is not None, "SQLite snapshot set is missing")
        sqlite_verification = verify_packaged_sqlite_snapshots(
            sqlite_restore_root,
            sqlite_snapshots,
            config=config,
            expected_device=expected_device,
        )
    missing_probes = spec.required_probe_groups - set(restored)
    require(
        not missing_probes,
        "restore probes were not verified: {}".format(
            ",".join(sorted(missing_probes))
        ),
    )
    full_restore: dict[str, Any] = {
        "performed": full_restore_root is not None,
        "regular_files_restored": full_restore_files,
        "result": "verified" if full_restore_root is not None else "not-required",
    }
    if spec.role == "policy":
        require(full_restore_root is not None, "policy restore root missing")
        for required in spec.required_members:
            require(
                os.path.lexists(full_restore_root / required),
                "full policy restore is missing required member: {}".format(
                    required
                ),
            )
    if spec.role == "git-remotes":
        require(full_restore_root is not None, "git restore root missing")
        full_restore["git_fsck"] = verify_restored_bare_repositories(
            full_restore_root,
            config=config,
        )
    return {
        "name": spec.filename,
        "sha256": expected["sha256"],
        "members": len(names),
        "member_contract_sha256": member_digest.hexdigest(),
        "restore_probes": restored,
        "sqlite_snapshot_verification": sqlite_verification,
        "full_isolated_restore": full_restore,
        "full_decompression": True,
        "result": "verified",
    }


def generation_file_modes(path: Path) -> None:
    require(
        stat.S_IMODE(path.lstat().st_mode) == 0o700,
        "generation directory mode is not 0700",
    )
    actual = {child.name for child in path.iterdir()}
    require(
        actual == EXPECTED_GENERATION_FILES,
        "generation file set mismatch: expected={} actual={}".format(
            ",".join(sorted(EXPECTED_GENERATION_FILES)),
            ",".join(sorted(actual)),
        ),
    )
    for child in path.iterdir():
        info = child.lstat()
        require(
            stat.S_ISREG(info.st_mode)
            and not child.is_symlink()
            and stat.S_IMODE(info.st_mode) == 0o600,
            "generation metadata/payload is not a physical 0600 file: {}".format(
                child
            ),
        )


def write_generation_metadata(
    staging: Path,
    *,
    config: BackupConfig,
    run_id: str,
    preflight: dict[str, Any],
    archive_records: list[dict[str, Any]],
    verification: list[dict[str, Any]],
    sqlite_snapshots: SqliteSnapshotSet,
    session_route: SessionStoreRoute,
) -> dict[str, Any]:
    require(
        len(archive_records) == len(EXPECTED_ARCHIVE_NAMES),
        "archive record count does not match the three-payload contract",
    )
    by_name = {row["name"]: row for row in archive_records}
    require(
        set(by_name) == set(EXPECTED_ARCHIVE_NAMES),
        "archive record set does not match the three-payload contract",
    )
    require(
        len(verification) == len(EXPECTED_ARCHIVE_NAMES)
        and {row.get("name") for row in verification}
        == set(EXPECTED_ARCHIVE_NAMES),
        "verification record set does not match the three-payload contract",
    )
    state_verification = next(
        row
        for row in verification
        if row.get("name") == "openclaw-state.tgz"
    )
    sqlite_verification = state_verification.get(
        "sqlite_snapshot_verification"
    )
    expected_sqlite_identities = sqlite_snapshots.expected_snapshots()
    require(
        len(sqlite_snapshots.snapshot_paths)
        == len(sqlite_snapshots.agent_ids) + 1
        and state_verification.get("restore_probes", {})
        .get("sqlite-snapshots", {})
        .get("result")
        == "verified",
        "SQLite snapshots were not covered by state verification",
    )
    require(
        isinstance(sqlite_verification, list)
        and len(sqlite_verification) == len(expected_sqlite_identities)
        and all(
            isinstance(actual, dict)
            and actual.get("snapshot_id") == expected["snapshot_id"]
            and actual.get("role") == expected["role"]
            and actual.get("agent_id") == expected["agent_id"]
            and actual.get("result") == "verified"
            and isinstance(actual.get("artifact_size_bytes"), int)
            and isinstance(actual.get("artifact_sha256"), str)
            and re.fullmatch(
                r"[0-9a-f]{64}",
                actual["artifact_sha256"],
            )
            is not None
            and isinstance(actual.get("manifest_sha256"), str)
            and re.fullmatch(
                r"[0-9a-f]{64}",
                actual["manifest_sha256"],
            )
            is not None
            for actual, expected in zip(
                sqlite_verification,
                expected_sqlite_identities,
            )
        ),
        "SQLite packaged verification coverage is incomplete",
    )
    checksum_lines = [
        "{}  {}".format(by_name[name]["sha256"], name)
        for name in EXPECTED_ARCHIVE_NAMES
    ]
    atomic_write_bytes(
        staging / "SHA256SUMS.txt",
        ("\n".join(checksum_lines) + "\n").encode("utf-8"),
    )
    index_lines = [
        "# OpenClaw OWC Weekly Archive Backup",
        "",
        "Created (UTC): {}".format(run_id),
        "Payload contract: {}".format(PAYLOAD_CONTRACT),
        "Capture consistency: {}".format(CAPTURE_CONSISTENCY),
        "Sensitive state: yes; OWC volume encryption is not provided by gzip",
        "iCloud offload: disabled",
        "",
        "## Payloads",
    ]
    for name in EXPECTED_ARCHIVE_NAMES:
        row = by_name[name]
        index_lines.append(
            "- {} ({} bytes; sha256 {})".format(
                name,
                row["logical_bytes"],
                row["sha256"],
            )
        )
    index_lines.extend(
        [
            "",
            "SQLite database capture: openclaw backup sqlite",
            "SQLite snapshots: {} (global + {} agents)".format(
                len(sqlite_snapshots.snapshot_paths),
                len(sqlite_snapshots.agent_ids),
            ),
            "Live SQLite/WAL files in state tar: excluded",
        ]
    )
    atomic_write_bytes(
        staging / "INDEX.md",
        ("\n".join(index_lines) + "\n").encode("utf-8"),
    )
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "backup_name": "openclaw-archive-v3-{}".format(run_id),
        "created_at_utc": utc_now(),
        "status": "verified",
        "destination": str(
            config.backup_root / "openclaw-archive-v3-{}".format(run_id)
        ),
        "payload_contract": PAYLOAD_CONTRACT,
        "archive_names": list(EXPECTED_ARCHIVE_NAMES),
        "capture_consistency": CAPTURE_CONSISTENCY,
        "source_tree_atomic": False,
        "same_device_local_recovery": True,
        "independent_disaster_recovery": False,
        "icloud_offload": False,
        "contains_sensitive_state": True,
        "archive_encryption": "none",
        "sqlite_backup": sqlite_snapshots.as_manifest_record(
            result="verified"
        ),
        "permissions": {
            "generation": "0700",
            "payload_and_metadata": "0600",
        },
        "source_contract": {
            "state_root": str(config.state_root),
            "expanded_managed_roots": {
                str(config.agents_logical): str(config.agents_physical),
                str(config.browser_logical): str(config.browser_physical),
                str(config.media_logical): str(config.media_physical),
            },
            "session_store_route": session_route.as_manifest_record(),
            "session_store_sanctioned_layouts": [
                "legacy-uppercase-physical-v1",
                "beta3-lowercase-physical-no-alias-v2",
            ],
            "state_exact_exclusions": sorted(
                str(path)
                for path in (
                    config.state_shadow_exclusions
                    | authoritative_sqlite_source_paths(
                        config,
                        sqlite_snapshots.agent_ids,
                    )
                )
            ),
            "authoritative_sqlite_live_archive_exclusions": sorted(
                state_archive_name(config, path)
                for path in authoritative_sqlite_paths(
                    config,
                    sqlite_snapshots.agent_ids,
                )
            ),
            "authoritative_sqlite_live_archive_pattern": (
                STATE_SQLITE_DATABASE_ARCHIVE_RE.pattern
            ),
            "sqlite_snapshot_archive_root": (
                SQLITE_SNAPSHOT_ARCHIVE_ROOT.as_posix()
            ),
            "volatile_regular_suffix_exclusions": list(VOLATILE_SUFFIXES),
            "update_wrapper_operational_log_archive_pattern": (
                UPDATE_WRAPPER_OPERATIONAL_LOG_ARCHIVE_PATTERN
            ),
            "update_wrapper_operational_log_policy": (
                "exclude-physical-same-device-regular-files-only-at-"
                ".openclaw/logs/update-wrapper-<UTC-basic>.log"
            ),
            "browser_volatile_cache_directory_names": list(
                BROWSER_VOLATILE_CACHE_DIRECTORY_NAMES
            ),
            "browser_managed_user_data_archive_roots": [
                path.as_posix()
                for path in BROWSER_MANAGED_USER_DATA_ARCHIVE_ROOTS
            ],
            "browser_profile_name_pattern": (
                BROWSER_PROFILE_NAME_PATTERN
            ),
            "browser_global_cache_relatives": sorted(
                path.as_posix()
                for path in BROWSER_GLOBAL_CACHE_RELATIVES
            ),
            "browser_profile_cache_relatives": sorted(
                path.as_posix()
                for path in BROWSER_PROFILE_CACHE_RELATIVES
            ),
            "browser_profile_nested_cache_relatives": sorted(
                path.as_posix()
                for path in BROWSER_PROFILE_NESTED_CACHE_RELATIVES
            ),
            "browser_extension_id_pattern": (
                BROWSER_EXTENSION_ID_PATTERN
            ),
            "browser_extension_cache_relatives": sorted(
                path.as_posix()
                for path in BROWSER_EXTENSION_CACHE_RELATIVES
            ),
            "browser_nested_cache_patterns": list(
                BROWSER_NESTED_CACHE_PATTERNS
            ),
            "browser_volatile_cache_policy": (
                "stat-physical-same-device-directory-then-exclude-"
                "before-descent-or-member-traversal"
            ),
            "reconstructible_runtime_cache_archive_roots": sorted(
                path.as_posix()
                for path in RECONSTRUCTIBLE_RUNTIME_CACHE_ARCHIVE_ROOTS
            ),
            "reconstructible_runtime_cache_policy": (
                RECONSTRUCTIBLE_RUNTIME_CACHE_POLICY
            ),
            "reconstructible_runtime_cache_restore_rationale": (
                RECONSTRUCTIBLE_RUNTIME_CACHE_RESTORE_RATIONALE
            ),
            "policy_files": list(POLICY_FILES),
            "policy_directories": ["runbook", "memory"],
            "git_remotes": str(config.git_remotes_physical),
            "git_regular_basename_exclusions": list(
                GIT_REGULAR_BASENAME_EXCLUSIONS
            ),
            "nested_symlink_policy": "store-link-never-traverse",
        },
        "preflight": preflight,
        "archives": archive_records,
        "verification": verification,
        "retention": {
            "performed": False,
            "policy": "separate verified retention lane",
            "minimum_verified_generations": 2,
        },
    }
    manifest["manifest_payload_sha256"] = sha256_bytes(
        canonical_json(manifest)
    )
    atomic_write_json(staging / "MANIFEST.json", manifest)
    return manifest


def load_manifest(path: Path) -> dict[str, Any]:
    manifest_path = path / "MANIFEST.json"
    require(
        manifest_path.is_file() and not manifest_path.is_symlink(),
        "generation manifest is missing or is a symlink",
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(
        isinstance(payload, dict)
        and payload.get("schema_version") in {MANIFEST_SCHEMA, NATIVE_MANIFEST_SCHEMA}
        and payload.get("status") == "verified",
        "generation manifest schema/status mismatch",
    )
    expected_digest = payload.get("manifest_payload_sha256")
    without_digest = dict(payload)
    without_digest.pop("manifest_payload_sha256", None)
    require(
        expected_digest == sha256_bytes(canonical_json(without_digest)),
        "generation manifest self-digest mismatch",
    )
    require(
        payload.get("archive_names") == list(EXPECTED_ARCHIVE_NAMES),
        "generation manifest archive set mismatch",
    )
    require(
        payload.get("icloud_offload") is False
        and payload.get("source_tree_atomic") is False
        and payload.get("capture_consistency") == CAPTURE_CONSISTENCY,
        "generation manifest safety classification mismatch",
    )
    return payload


def verify_published_generation(
    path: Path,
    *,
    config: BackupConfig,
) -> dict[str, Any]:
    require(
        path.parent == config.backup_root
        and BACKUP_NAME_RE.fullmatch(path.name) is not None,
        "published generation path is outside the exact backup root",
    )
    require_physical_directory(path, config.backup_root.stat().st_dev)
    generation_file_modes(path)
    manifest = load_manifest(path)
    native = manifest["schema_version"] == NATIVE_MANIFEST_SCHEMA
    require(path.name.startswith("openclaw-archive-v4-" if native else "openclaw-archive-v3-"),
            "generation name and manifest version disagree")
    require(
        manifest.get("backup_name") == path.name
        and manifest.get("destination") == str(path),
        "published generation manifest identity mismatch",
    )
    archive_rows = {
        row["name"]: row
        for row in manifest.get("archives", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    require(
        isinstance(manifest.get("archives"), list)
        and len(manifest["archives"]) == len(EXPECTED_ARCHIVE_NAMES)
        and len(archive_rows) == len(EXPECTED_ARCHIVE_NAMES)
        and set(archive_rows) == set(EXPECTED_ARCHIVE_NAMES),
        "published generation archive records mismatch",
    )
    verification_rows = {
        row["name"]: row
        for row in manifest.get("verification", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    require(
        isinstance(manifest.get("verification"), list)
        and len(manifest["verification"]) == len(EXPECTED_ARCHIVE_NAMES)
        and len(verification_rows) == len(EXPECTED_ARCHIVE_NAMES)
        and set(verification_rows) == set(EXPECTED_ARCHIVE_NAMES),
        "published generation verification records mismatch",
    )
    sqlite_backup = manifest.get("sqlite_backup")
    if native:
        require(sqlite_backup is None
                and manifest.get("payload_contract") == "native-state-and-selective-supplements-v1"
                and manifest.get("native_backup") == native_backup_contract(),
                "native generation backup contract mismatch")
    if sqlite_backup is not None:
        require(
            isinstance(sqlite_backup, dict),
            "published generation SQLite snapshot proof is incomplete",
        )
        agent_ids = sqlite_backup.get("agent_ids")
        snapshot_ids = sqlite_backup.get("snapshot_ids")
        snapshot_count = sqlite_backup.get("snapshot_count")
        snapshots = sqlite_backup.get("snapshots")
        require(
            sqlite_backup.get("command_contract")
            == "openclaw backup sqlite create --json"
            and sqlite_backup.get("verification_contract")
            == "openclaw backup sqlite verify <snapshot> --json"
            and sqlite_backup.get("archive_repository_root")
            == SQLITE_SNAPSHOT_ARCHIVE_ROOT.as_posix()
            and sqlite_backup.get("result") == "verified"
            and isinstance(agent_ids, list)
            and agent_ids
            and len(agent_ids) == len(set(agent_ids))
            and all(
                isinstance(agent_id, str)
                and safe_agent_id(agent_id) == agent_id
                for agent_id in agent_ids
            )
            and isinstance(snapshot_count, int)
            and not isinstance(snapshot_count, bool)
            and snapshot_count == len(agent_ids) + 1
            and sqlite_backup.get("global_snapshot_count") == 1
            and isinstance(snapshot_ids, list)
            and len(snapshot_ids) == snapshot_count
            and len(snapshot_ids) == len(set(snapshot_ids))
            and all(
                isinstance(snapshot_id, str)
                and safe_archive_name(snapshot_id) == snapshot_id
                and "/" not in snapshot_id
                for snapshot_id in snapshot_ids
            )
            and isinstance(snapshots, list)
            and len(snapshots) == snapshot_count
            and all(
                isinstance(row, dict)
                and set(row) == {"snapshot_id", "role", "agent_id"}
                and row.get("snapshot_id") == snapshot_ids[index]
                and (
                    (
                        index < len(agent_ids)
                        and row.get("role") == "agent"
                        and row.get("agent_id") == agent_ids[index]
                    )
                    or (
                        index == len(agent_ids)
                        and row.get("role") == "global"
                        and row.get("agent_id") is None
                    )
                )
                for index, row in enumerate(snapshots)
            ),
            "published generation SQLite snapshot proof is incomplete",
        )
    required_probes = {
        "openclaw-state.tgz": {
            "state-internal",
            "sessions",
            "browser",
            "media",
        },
        "openclaw-workspace-policy.tgz": {"policy"},
        "git-remotes.tgz": {"git-remotes"},
    }
    if sqlite_backup is not None:
        required_probes["openclaw-state.tgz"].add("sqlite-snapshots")
    if native:
        required_probes["openclaw-state.tgz"] = {"native-state"}
    for name, row in verification_rows.items():
        require(
            row.get("sha256") == archive_rows[name].get("sha256")
            and row.get("result") == "verified"
            and row.get("full_decompression") is True
            and isinstance(row.get("restore_probes"), dict)
            and set(row["restore_probes"]) >= required_probes[name]
            and all(
                isinstance(probe, dict)
                and probe.get("result") == "verified"
                for probe in row["restore_probes"].values()
            ),
            "published generation creation verification is incomplete: "
            "{}".format(name),
        )
        full_restore = row.get("full_isolated_restore")
        require(
            isinstance(full_restore, dict),
            "published generation full-restore proof is missing: {}".format(
                name
            ),
        )
        if name == "openclaw-state.tgz":
            if native:
                require(archive_rows[name].get("format") == NATIVE_STATE_FORMAT
                        and full_restore.get("performed") is True
                        and full_restore.get("result") == "verified"
                        and row.get("native_verified") is True,
                        "native state archive verification/restore is incomplete")
                continue
            require(
                full_restore.get("performed") is False
                and full_restore.get("result") == "not-required",
                "state archive full-restore classification mismatch",
            )
            if sqlite_backup is not None:
                sqlite_verification = row.get(
                    "sqlite_snapshot_verification"
                )
                require(
                    isinstance(sqlite_verification, list)
                    and len(sqlite_verification)
                    == sqlite_backup["snapshot_count"]
                    and all(
                        isinstance(actual, dict)
                        and actual.get("snapshot_id")
                        == sqlite_backup["snapshots"][index]["snapshot_id"]
                        and actual.get("role")
                        == sqlite_backup["snapshots"][index]["role"]
                        and actual.get("agent_id")
                        == sqlite_backup["snapshots"][index]["agent_id"]
                        and actual.get("result") == "verified"
                        and isinstance(
                            actual.get("artifact_size_bytes"),
                            int,
                        )
                        and not isinstance(
                            actual.get("artifact_size_bytes"),
                            bool,
                        )
                        and actual["artifact_size_bytes"] >= 0
                        and isinstance(
                            actual.get("artifact_sha256"),
                            str,
                        )
                        and re.fullmatch(
                            r"[0-9a-f]{64}",
                            actual["artifact_sha256"],
                        )
                        is not None
                        and isinstance(
                            actual.get("manifest_sha256"),
                            str,
                        )
                        and re.fullmatch(
                            r"[0-9a-f]{64}",
                            actual["manifest_sha256"],
                        )
                        is not None
                        for index, actual in enumerate(
                            sqlite_verification
                        )
                    ),
                    "published SQLite packaged verification proof is incomplete",
                )
        else:
            require(
                full_restore.get("performed") is True
                and full_restore.get("result") == "verified",
                "published generation full restore is incomplete: {}".format(
                    name
                ),
            )
        if name == "git-remotes.tgz":
            git_fsck = full_restore.get("git_fsck")
            require(
                isinstance(git_fsck, dict)
                and git_fsck.get("result") == "verified"
                and int(git_fsck.get("repository_count", 0)) > 0,
                "published generation git fsck proof is incomplete",
            )
    checksum_text = (path / "SHA256SUMS.txt").read_text(encoding="utf-8")
    expected_checksum_text = "\n".join(
        "{}  {}".format(archive_rows[name]["sha256"], name)
        for name in EXPECTED_ARCHIVE_NAMES
    ) + "\n"
    require(
        checksum_text == expected_checksum_text,
        "published checksum index mismatch",
    )
    for name in EXPECTED_ARCHIVE_NAMES:
        archive = path / name
        require(
            archive.stat().st_size == archive_rows[name]["logical_bytes"]
            and sha256_file(archive) == archive_rows[name]["sha256"],
            "published archive readback mismatch: {}".format(name),
        )
    return {
        "backup": path.name,
        "manifest_sha256": sha256_file(path / "MANIFEST.json"),
        "archive_count": len(EXPECTED_ARCHIVE_NAMES),
        "full_decompression_proven_at_creation": True,
        "retention_performed": False,
        "result": "verified",
    }


def independent_published_effect_readback(generation: Path) -> list[dict[str, Any]]:
    """Re-open the exact published generation without producer in-memory state."""
    results: list[dict[str, Any]] = []
    generation_present = generation.is_dir() and not generation.is_symlink()
    try:
        require(
            generation_present,
            "published generation is missing, not a directory, or is a symlink",
        )
        manifest = load_manifest(generation)
        archive_rows = {
            row["name"]: row
            for row in manifest.get("archives", [])
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
        verification_rows = {
            row["name"]: row
            for row in manifest.get("verification", [])
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
        readable = True
    except (OSError, ValueError, json.JSONDecodeError, BackupError) as exc:
        manifest = {}
        archive_rows = {}
        verification_rows = {}
        readable = False
        manifest_error = "{}: {}".format(type(exc).__name__, exc)

    results.append(
        effect.object_present(
            "published_generation",
            str(generation),
            present=generation_present,
            readable=readable,
        )
    )
    if not readable:
        results.append(effect.unreadable("published_manifest", manifest_error))
        return results

    for name in EXPECTED_ARCHIVE_NAMES:
        try:
            archive_row = archive_rows[name]
            verification_row = verification_rows[name]
            require(
                archive_row.get("sha256") == verification_row.get("sha256")
                and archive_row.get("member_contract_sha256")
                == verification_row.get("member_contract_sha256"),
                "manifest archive/verification rows disagree for {}".format(name),
            )
            archive_path = generation / name
            member_count = 0
            member_digest = hashlib.sha256()
            with tarfile.open(archive_path, mode="r:gz") as archive:
                for member in archive:
                    safe_archive_name(member.name)
                    member_digest.update(canonical_json(member_contract_record(member)) + b"\n")
                    member_count += 1
            expected = {
                "members": verification_row.get("members"),
                "member_contract_sha256": archive_row.get("member_contract_sha256"),
                "sha256": archive_row.get("sha256"),
            }
            actual = {
                "members": member_count,
                "member_contract_sha256": member_digest.hexdigest(),
                "sha256": sha256_file(archive_path),
            }
            results.append(
                effect.readback_matches(
                    "published_archive:{}".format(name),
                    expected=expected,
                    actual=actual,
                )
            )
        except (KeyError, OSError, ValueError, tarfile.TarError, EOFError, BackupError) as exc:
            results.append(
                effect.unreadable(
                    "published_archive:{}".format(name),
                    "{}: {}".format(type(exc).__name__, exc),
                )
            )
    return results


def native_backup_contract() -> dict[str, Any]:
    return {
        "format": NATIVE_STATE_FORMAT,
        "create": "openclaw backup create --no-include-workspace --verify --json",
        "verify": "openclaw backup verify <archive> --json",
        "restore": "openclaw backup restore <archive> --target <fresh-directory> --json",
        "result": "verified",
    }


def native_archive_record(path: Path, expected_device: int) -> dict[str, Any]:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and info.st_dev == expected_device
            and stat.S_IMODE(info.st_mode) == 0o600,
            "native state archive is not a private physical file")
    digest = hashlib.sha256()
    names: set[str] = set()
    logical_bytes = 0
    # This reads only the completed archive, never walks live state again.
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = safe_archive_name(member.name)
            require(name not in names, "native state archive has duplicate members")
            names.add(name)
            digest.update(canonical_json(member_contract_record(member)) + b"\n")
            if member.isfile():
                logical_bytes += member.size
    return {
        "name": path.name, "role": "state", "format": NATIVE_STATE_FORMAT,
        "sha256": sha256_file(path), "logical_bytes": info.st_size,
        "allocated_bytes": info.st_blocks * 512,
        "members": len(names), "member_contract_sha256": digest.hexdigest(),
        "source": {"logical_bytes": logical_bytes, "entries": len(names)},
        "restore_candidates": {},
    }


def verify_native_state_archive(
    path: Path, expected: dict[str, Any], *, config: BackupConfig,
    probe_root: Path, expected_device: int,
) -> dict[str, Any]:
    observed = native_archive_record(path, expected_device)
    require(observed == expected, "native state archive changed before verification")
    budget = restore_budget_from_archive_records([expected])["total_bytes"]
    require(shutil.disk_usage(probe_root).free >= config.min_post_backup_free_bytes + budget,
            "insufficient OWC headroom for native restore verification")
    scratch = probe_root / "native-verify-scratch"
    ensure_private_directory(scratch)
    target = probe_root / "native-state-restore"
    require(not os.path.lexists(target), "native restore target is not fresh")
    cli = resolve_openclaw_cli(config)
    verified = run_openclaw_json(
        cli, config, ("backup", "verify", str(path), "--json"),
        label="native archive verify", timeout_seconds=SQLITE_CREATE_TIMEOUT_SECONDS,
        scratch_root=scratch,
    )
    require(isinstance(verified, dict) and verified.get("ok") is True
            and verified.get("archivePath") == str(path),
            "native archive verification result mismatch")
    restored = run_openclaw_json(
        cli, config, ("backup", "restore", str(path), "--target", str(target), "--json"),
        label="native archive restore", timeout_seconds=SQLITE_CREATE_TIMEOUT_SECONDS,
        scratch_root=scratch,
    )
    require(isinstance(restored, dict) and restored.get("ok") is True
            and restored.get("archivePath") == str(path)
            and restored.get("targetPath") == str(target),
            "native archive restore result mismatch")
    require_physical_directory(target, expected_device)
    after = path.lstat()
    require(stat.S_ISREG(after.st_mode) and after.st_nlink == 1
            and after.st_dev == expected_device and stat.S_IMODE(after.st_mode) == 0o600
            and sha256_file(path) == expected["sha256"],
            "native state archive changed during verification")
    require(shutil.disk_usage(probe_root).free >= config.min_post_backup_free_bytes,
            "OWC free space crossed the configured floor during native restore")
    return {
        "name": path.name, "sha256": expected["sha256"],
        "members": expected["members"],
        "member_contract_sha256": expected["member_contract_sha256"],
        "native_verified": True, "full_decompression": True,
        "restore_probes": {"native-state": {"result": "verified"}},
        "full_isolated_restore": {"performed": True, "result": "verified"},
        "result": "verified",
    }


def write_native_generation_metadata(
    staging: Path, *, config: BackupConfig, final: Path, preflight: dict[str, Any],
    archive_records: list[dict[str, Any]], verification: list[dict[str, Any]],
) -> dict[str, Any]:
    by_name = {row["name"]: row for row in archive_records}
    require(set(by_name) == set(EXPECTED_ARCHIVE_NAMES)
            and len(archive_records) == len(EXPECTED_ARCHIVE_NAMES),
            "native generation archive set mismatch")
    atomic_write_bytes(staging / "SHA256SUMS.txt", (
        "\n".join("{}  {}".format(by_name[name]["sha256"], name)
                  for name in EXPECTED_ARCHIVE_NAMES) + "\n"
    ).encode())
    atomic_write_bytes(staging / "INDEX.md", (
        "# OpenClaw Weekly Backup\n\n"
        "State: native OpenClaw archive; restore with openclaw backup restore.\n"
        "Supplements: selected Workspace policy/memory and bare Git remotes.\n"
        "Contains sensitive data; local recovery only, not independent disaster recovery.\n"
        "No runtime activation, source deletion, or retention was performed.\n"
    ).encode())
    manifest = {
        "schema_version": NATIVE_MANIFEST_SCHEMA, "backup_name": final.name,
        "created_at_utc": utc_now(), "status": "verified", "destination": str(final),
        "payload_contract": "native-state-and-selective-supplements-v1",
        "archive_names": list(EXPECTED_ARCHIVE_NAMES),
        "capture_consistency": CAPTURE_CONSISTENCY, "source_tree_atomic": False,
        "same_device_local_recovery": True, "independent_disaster_recovery": False,
        "icloud_offload": False, "contains_sensitive_state": True,
        "archive_encryption": "none", "native_backup": native_backup_contract(),
        "permissions": {"generation": "0700", "payload_and_metadata": "0600"},
        "source_contract": {
            "state_root": str(config.state_root),
            "state_owner": "openclaw backup create --no-include-workspace",
            "policy_files": list(POLICY_FILES), "policy_directories": ["runbook", "memory"],
            "git_remotes": str(config.git_remotes_physical),
        },
        "preflight": preflight, "archives": archive_records, "verification": verification,
        "retention": {"performed": False, "policy": "separate verified retention lane",
                      "minimum_verified_generations": 2},
    }
    manifest["manifest_payload_sha256"] = sha256_bytes(canonical_json(manifest))
    atomic_write_json(staging / "MANIFEST.json", manifest)
    return manifest


def create_backup(
    config: BackupConfig = BackupConfig(), *,
    identity_reader: Callable[[BackupConfig], VolumeIdentity] = read_volume_identity,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    run_id = timestamp.strftime("%Y%m%dT%H%M%SZ")
    final = config.backup_root / "openclaw-archive-v4-{}".format(run_id)
    staging = config.backup_root / ".openclaw-archive-v4-{}.incomplete-{}".format(run_id, os.getpid())
    previous_umask = os.umask(0o077)
    descriptor: int | None = None
    receipt: PhaseReceipt | None = None
    try:
        receipt = PhaseReceipt(config, run_id, final)
        identity = validate_environment(config, identity_reader, create_backup_root=True)
        require(config.state_root.resolve() == config.agents_physical.parent.resolve(),
                "native state root must own the canonical agents directory")
        lock_path = config.backup_root.parent / ".weekly-backup.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("weekly backup lock is already held") from exc
        prior_incomplete = sorted(child.name for child in config.backup_root.iterdir()
                                  if INCOMPLETE_NAME_RE.fullmatch(child.name))
        require(not prior_incomplete, "prior incomplete staging requires classification: {}".format(
            ",".join(prior_incomplete)))
        require(not os.path.lexists(final), "backup generation collision")
        require(not os.path.lexists(staging), "backup staging collision")
        source_inventory = authoritative_sqlite_source_inventory(config, None)
        # Native owns state traversal/capture. The existing narrow supplements
        # are estimated separately and never copy state a second time.
        specs = tuple(spec for spec in build_archive_specs(config, identity) if spec.role != "state")
        estimate = estimate_archives(specs)
        required = (config.min_post_backup_free_bytes
                    + source_inventory["snapshot_peak_headroom_bytes"]
                    + estimate["archive_output_upper_bytes"]
                    + estimate["restore_probe_budget_bytes"])
        require(shutil.disk_usage(config.backup_root).free >= required,
                "insufficient OWC headroom before native backup")
        staging.mkdir(mode=0o700)
        fsync_dir(staging.parent)
        probe_root = staging / ".restore-probe"
        probe_root.mkdir(mode=0o700)
        probe_info = require_physical_directory(probe_root, identity.device)
        native_scratch = probe_root / "native-create-scratch"
        native_scratch.mkdir(mode=0o700)
        preflight = {"source_inventory": source_inventory,
                     "minimum_post_backup_free_bytes": config.min_post_backup_free_bytes,
                     "required_free_before_bytes": required,
                     "native_backup": native_backup_contract()}
        receipt.phase("preflight", "verified", preflight=preflight)
        native_path = staging / "openclaw-state.tgz"
        created = run_openclaw_json(
            resolve_openclaw_cli(config), config,
            ("backup", "create", "--no-include-workspace", "--verify",
             "--json", "--output", str(native_path)),
            label="native archive create", timeout_seconds=SQLITE_CREATE_TIMEOUT_SECONDS,
            scratch_root=native_scratch,
        )
        require(isinstance(created, dict) and created.get("verified") is True
                and created.get("dryRun") is False and created.get("onlyConfig") is False
                and created.get("includeWorkspace") is False
                and created.get("archivePath") == str(native_path)
                and isinstance(created.get("skipped"), list)
                and all(isinstance(item, dict) and item.get("reason") in {"covered", "regenerable", "missing"}
                        and not (item.get("reason") == "missing" and item.get("kind") in {"state", "config", "agent"})
                        for item in created["skipped"])
                and isinstance(created.get("assets"), list)
                and any(isinstance(asset, dict) and asset.get("kind") == "state"
                        and asset.get("sourcePath") == str(config.state_root.resolve())
                        for asset in created["assets"]),
                "native state capture result or source identity mismatch")
        require(shutil.disk_usage(config.backup_root).free >= config.min_post_backup_free_bytes,
                "OWC free space crossed the configured floor during native backup")
        records = [native_archive_record(native_path, identity.device)]
        for spec in specs:
            records.append(create_archive(
                staging / spec.filename, spec, expected_device=identity.device,
                minimum_stream_free_bytes=config.min_post_backup_free_bytes + RESTORE_METADATA_BASE_BYTES,
            ))
        budget = restore_budget_from_archive_records(records)
        require(shutil.disk_usage(config.backup_root).free
                >= config.min_post_backup_free_bytes + budget["total_bytes"],
                "OWC post-archive free space cannot cover restore verification and the configured floor")
        proofs: list[dict[str, Any]] = []
        try:
            proofs.append(verify_native_state_archive(
                native_path, records[0], config=config, probe_root=probe_root,
                expected_device=identity.device,
            ))
            by_name = {row["name"]: row for row in records}
            for spec in specs:
                proofs.append(verify_archive(
                    staging / spec.filename, spec, by_name[spec.filename],
                    probe_root=probe_root, expected_device=identity.device, config=config,
                ))
        finally:
            remove_owned_directory_tree(
                probe_root, expected_parent=staging, expected_device=identity.device,
                expected_identity=(probe_info.st_dev, probe_info.st_ino),
                name_allowed=probe_root.name == ".restore-probe", purpose="producer restore probe",
            )
        final_inventory = authoritative_sqlite_source_inventory(config, None)
        require(all(final_inventory[field] == source_inventory[field]
                    for field in ("database_identities", "root_identities")),
                "canonical database inventory changed during native backup")
        require(shutil.disk_usage(config.backup_root).free >= config.min_post_backup_free_bytes,
                "OWC free space crossed the configured floor before publication")
        manifest = write_native_generation_metadata(
            staging, config=config, final=final, preflight=preflight,
            archive_records=records, verification=proofs,
        )
        generation_file_modes(staging)
        require(load_manifest(staging) == manifest, "staging manifest readback mismatch")
        os.rename(staging, final)
        fsync_dir(config.backup_root)
        published = verify_published_generation(final, config=config)
        receipt.finish("completed", "verified", [], backup=final.name,
                       manifest=str(final / "MANIFEST.json"), verification=published,
                       retention_performed=False)
        return 0, {"backup": final.name, "destination": str(final), "receipt": str(receipt.path),
                   "manifest": str(final / "MANIFEST.json"), "verification": published,
                   "retention_performed": False, "blockers": [], "result": "verified"}
    except Exception as exc:
        blockers = ["{}: {}".format(type(exc).__name__, exc)]
        if receipt is not None:
            diagnostic = exc.native_command_diagnostic if isinstance(exc, BackupError) else None
            receipt.finish("failed", "blocked", blockers, source_retained=True,
                           incomplete_staging_retained=staging.exists(), staging=str(staging),
                           retention_performed=False,
                           **({"native_command_diagnostic": diagnostic} if diagnostic is not None else {}))
        return 1, {"backup": final.name, "destination": str(final),
                   "receipt": str(receipt.path) if receipt else "",
                   "staging": str(staging), "blockers": blockers,
                   "retention_performed": False, "result": "blocked"}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.umask(previous_umask)


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


def emit_backup_effect(results: list[dict[str, Any]], *, what: str) -> int:
    """Keep existing effect evaluation and the supervisor's private receipt line.

    cron_python_entrypoint consumes EFFECT_PREDICATES into its mode-0600
    receipt and removes it from delivery. The legacy STATUS prose is not a
    user message; these backup owners provide their own concise outcome.
    """
    captured = io.StringIO()
    with redirect_stdout(captured):
        code = effect.emit(results, what=what)
    for line in captured.getvalue().splitlines():
        if line.startswith("EFFECT_PREDICATES "):
            print(line)
    return code


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    code, result = create_backup()
    if code != 0:
        emit_backup_effect(
            [effect.unreadable("weekly_backup", "Backup failed; consult its private phase receipt if available.")],
            what="create weekly backup",
        )
        print("The weekly OpenClaw backup did not complete. Existing backups were kept.")
        return code
    effect_code = emit_backup_effect(
        independent_published_effect_readback(Path(result["destination"])),
        what="publish bounded three-archive OWC weekly recovery set",
    )
    if effect_code != 0:
        print("The weekly backup failed its final verification. It is not confirmed usable.")
        return effect_code
    print("The weekly OpenClaw backup is complete and its restore checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
