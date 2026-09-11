#!/usr/bin/env python3
"""Read-only OWC mount guard plus off-volume transition receipts.

The live observer must run from an internal installed copy with an internal
contract snapshot, state file, incident directory, and logs. It never remounts,
restarts services, or edits OWC-backed state. At a deadline it may SIGKILL only
its disposable probe child. An unhealthy probe means writes are not admitted;
recovery is evidence only and does not authorize lifecycle work.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import select
import stat
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

KIND = "openclaw.owc-volume-guard.probe.v1"
INCIDENT_KIND = "openclaw.owc-volume-guard.incident.v1"
STATE_KIND = "openclaw.owc-volume-guard.state.v1"
LOCK_MARKER_KIND = "openclaw.owc-volume-guard.probe-lock.v1"
DEFAULT_CONTRACT = Path(__file__).resolve().parents[1] / 'registry' / 'external_volume_guard.json'
ATTR_BIT_MAP_COUNT = 5
ATTR_VOL_UUID = 0x00040000
ATTR_VOL_INFO = 0x80000000
MNT_IGNORE_OWNERSHIP = 0x00200000
VOLUME_UUID_BUFFER_SIZE = 20
F_RDAHEAD = 45
F_NOCACHE = 48
PROBE_STALL_TIMEOUT_SECONDS = 10.0
ADMISSION_STATE_MAX_AGE_SECONDS = 40.0
SUPERVISOR_POLL_SECONDS = 0.05
SUPERVISOR_REAP_GRACE_SECONDS = 0.25
MAX_CHILD_ENVELOPE_BYTES = 1024 * 1024
ACTIVE_PROBE_RETIRED = True
ACTIVE_PROBE_RETIRED_AT = "2026-08-08T08:46:49Z"
ACTIVE_PROBE_RETIREMENT_REASON = (
    "active OWC liveness probing retired after its uncached 4 KiB read was "
    "identified as the NVMe command that timed out before the 2026-08-08 detach"
)


class GuardError(RuntimeError):
    pass


class AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


class FsId(ctypes.Structure):
    _fields_ = [("value", ctypes.c_int32 * 2)]


class StatFs(ctypes.Structure):
    """Darwin 64-bit ``struct statfs`` from ``sys/mount.h``."""

    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", FsId),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GuardError(f"JSON object required: {path}")
    return payload


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def normalize_contract(path: Path) -> dict[str, Any]:
    payload = read_json(path.expanduser().resolve(strict=True))
    if payload.get("kind") != "openclaw.owc-volume-guard.contract.v1":
        raise GuardError("unsupported OWC guard contract kind")
    mount = Path(str(payload.get("mountPoint", ""))).expanduser()
    state_path = Path(str(payload.get("statePath", ""))).expanduser()
    incident_directory = Path(
        str(payload.get("incidentDirectory", ""))
    ).expanduser()
    lock_path = Path(str(payload.get("lockPath", ""))).expanduser()
    volume_uuid = str(payload.get("volumeUuid", "")).upper()
    liveness = payload.get("livenessProbe")
    protected = payload.get("protectedPaths")
    if (
        payload.get("schemaVersion") != 2
        or not mount.is_absolute()
        or not state_path.is_absolute()
        or not incident_directory.is_absolute()
        or not lock_path.is_absolute()
        or not volume_uuid
        or not isinstance(liveness, dict)
        or not isinstance(protected, list)
        or not protected
        or any(not isinstance(row, dict) for row in protected)
    ):
        raise GuardError("OWC guard contract is incomplete")
    for label, internal_path in (
        ("admission state", state_path),
        ("incident directory", incident_directory),
        ("probe lock", lock_path),
    ):
        try:
            internal_path.relative_to(mount)
        except ValueError:
            pass
        else:
            raise GuardError(f"OWC guard {label} must be off the guarded mount")
    liveness_path = Path(str(liveness.get("path", ""))).expanduser()
    liveness_size = liveness.get("sizeBytes")
    liveness_sha256 = str(liveness.get("sha256", "")).lower()
    liveness_timeout = liveness.get("timeoutSeconds")
    if (
        not liveness_path.is_absolute()
        or not isinstance(liveness_size, int)
        or isinstance(liveness_size, bool)
        or liveness_size <= 0
        or len(liveness_sha256) != 64
        or any(character not in "0123456789abcdef" for character in liveness_sha256)
        or not isinstance(liveness_timeout, (int, float))
        or isinstance(liveness_timeout, bool)
        or not math.isfinite(float(liveness_timeout))
        or float(liveness_timeout) <= 0
        or float(liveness_timeout) >= 15
    ):
        raise GuardError("OWC guard liveness probe contract is invalid")
    normalized_paths: list[dict[str, str]] = []
    for row in protected:
        root_id = row.get("rootId")
        logical_path = row.get("logicalPath")
        if not isinstance(root_id, str) or not root_id:
            raise GuardError("protected path rootId is missing")
        if not isinstance(logical_path, str) or not Path(logical_path).is_absolute():
            raise GuardError(f"protected path is invalid: {root_id}")
        normalized_paths.append({"rootId": root_id, "logicalPath": logical_path})
    return {
        "path": str(path.expanduser().resolve(strict=True)),
        "sha256": sha256_bytes(path.expanduser().resolve(strict=True).read_bytes()),
        "mountPoint": str(mount),
        "statePath": str(state_path),
        "incidentDirectory": str(incident_directory),
        "lockPath": str(lock_path),
        "volumeUuid": volume_uuid,
        "livenessProbe": {
            "path": str(liveness_path),
            "sizeBytes": liveness_size,
            "sha256": liveness_sha256,
            "timeoutSeconds": float(liveness_timeout),
        },
        "protectedPaths": normalized_paths,
    }


def uuid_from_getattrlist_buffer(payload: bytes) -> str:
    if len(payload) < VOLUME_UUID_BUFFER_SIZE:
        raise GuardError("volume UUID attribute buffer is truncated")
    returned_size = int.from_bytes(payload[:4], sys.byteorder)
    if returned_size < VOLUME_UUID_BUFFER_SIZE or returned_size > len(payload):
        raise GuardError("volume UUID attribute buffer length is invalid")
    return str(uuid.UUID(bytes=payload[4:20])).upper()


def read_volume_uuid(mount: Path) -> str:
    if sys.platform != "darwin":
        raise GuardError("volume UUID metadata probe requires macOS")
    attributes = AttrList()
    attributes.bitmapcount = ATTR_BIT_MAP_COUNT
    attributes.volattr = ATTR_VOL_INFO | ATTR_VOL_UUID
    output = ctypes.create_string_buffer(VOLUME_UUID_BUFFER_SIZE)
    libc = ctypes.CDLL(None, use_errno=True)
    getattrlist = libc.getattrlist
    getattrlist.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(AttrList),
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_ulong,
    ]
    getattrlist.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = getattrlist(
        os.fsencode(mount),
        ctypes.byref(attributes),
        ctypes.byref(output),
        ctypes.sizeof(output),
        0,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(mount))
    return uuid_from_getattrlist_buffer(output.raw)


def filesystem_type(mount: Path) -> str:
    """Read the mounted filesystem type through Darwin ``statfs(2)``."""

    if sys.platform != "darwin":
        raise GuardError("filesystem type metadata probe requires macOS")
    record = StatFs()
    libc = ctypes.CDLL(None, use_errno=True)
    statfs_call = libc.statfs
    statfs_call.argtypes = [ctypes.c_char_p, ctypes.POINTER(StatFs)]
    statfs_call.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = statfs_call(os.fsencode(mount), ctypes.byref(record))
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(mount))
    try:
        observed = bytes(record.f_fstypename).split(b"\0", 1)[0].decode("ascii").casefold()
    except UnicodeDecodeError as exc:
        raise GuardError("filesystem type metadata is not ASCII") from exc
    if not observed:
        raise GuardError("filesystem type metadata is empty")
    return observed


def current_mount_device(mount: Path) -> int | None:
    if not mount.exists() or not os.path.ismount(mount):
        return None
    return mount.stat().st_dev


def volume_metadata(mount: Path) -> dict[str, Any]:
    if not mount.exists():
        return {
            "available": False,
            "mountPoint": str(mount),
            "mounted": False,
            "probeMethod": "getattrlist-statvfs",
            "reason": "mount-path-missing",
        }
    try:
        initial_device = current_mount_device(mount)
    except OSError as exc:
        return {
            "available": False,
            "mountPoint": str(mount),
            "mounted": None,
            "probeMethod": "getattrlist-statvfs",
            "error": type(exc).__name__,
            "errno": exc.errno,
        }
    if initial_device is None:
        return {
            "available": False,
            "mountPoint": str(mount),
            "mounted": False,
            "probeMethod": "getattrlist-statvfs",
            "reason": "not-a-mounted-filesystem",
        }
    try:
        volume_uuid = read_volume_uuid(mount)
        observed_filesystem_type = filesystem_type(mount)
        mount_flags = os.statvfs(mount).f_flag
        read_only = bool(mount_flags & os.ST_RDONLY)
        final_device = current_mount_device(mount)
    except (GuardError, OSError) as exc:
        try:
            current_device = current_mount_device(mount)
            still_mounted = current_device is not None
            recheck_error: dict[str, Any] | None = None
        except OSError as recheck_exc:
            current_device = None
            still_mounted = None
            recheck_error = {
                "error": type(recheck_exc).__name__,
                "errno": recheck_exc.errno,
            }
        result: dict[str, Any] = {
            "available": False,
            "mountDevice": current_device if still_mounted is True else None,
            "mountPoint": str(mount),
            "mounted": still_mounted,
            "probeMethod": "getattrlist-statvfs",
            "error": type(exc).__name__,
        }
        if isinstance(exc, OSError) and exc.errno is not None:
            result["errno"] = exc.errno
        if recheck_error is not None:
            result["mountRecheckError"] = recheck_error["error"]
            result["mountRecheckErrno"] = recheck_error["errno"]
        return result
    if final_device is None or final_device != initial_device:
        return {
            "available": False,
            "initialMountDevice": initial_device,
            "mountDevice": final_device,
            "mountPoint": str(mount),
            "mounted": final_device is not None,
            "probeMethod": "getattrlist-statvfs",
            "reason": "mount-identity-changed-during-probe",
        }
    return {
        "available": True,
        "mountDevice": final_device,
        "mountPoint": str(mount),
        "mounted": True,
        "filesystemType": observed_filesystem_type,
        "mountFlags": mount_flags,
        "ownersEnabled": not bool(mount_flags & MNT_IGNORE_OWNERSHIP),
        "probeMethod": "getattrlist-statvfs",
        "readOnly": read_only,
        "volumeUuid": volume_uuid,
    }


def read_liveness_probe(
    contract: dict[str, Any],
    *,
    expected_device: int,
) -> dict[str, Any]:
    """Perform one pinned ordinary-file read with cache and read-ahead disabled."""

    configured = contract.get("livenessProbe")
    if not isinstance(configured, dict):
        raise GuardError("liveness probe is missing from the normalized contract")
    mount = Path(contract["mountPoint"])
    path = Path(str(configured["path"]))
    size_bytes = int(configured["sizeBytes"])
    expected_sha256 = str(configured["sha256"])
    mount_resolved = mount.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved != mount_resolved and mount_resolved not in resolved.parents:
        raise GuardError("liveness sentinel resolves outside the pinned mount")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise GuardError("liveness sentinel is not a regular file")
        if info.st_dev != expected_device:
            raise GuardError("liveness sentinel is not on the pinned mount device")
        if info.st_size != size_bytes:
            raise GuardError("liveness sentinel size mismatch")
        fcntl.fcntl(descriptor, F_RDAHEAD, 0)
        fcntl.fcntl(descriptor, F_NOCACHE, 1)
        payload = os.pread(descriptor, size_bytes, 0)
        if len(payload) != size_bytes:
            raise GuardError("liveness sentinel read length mismatch")
        observed_sha256 = sha256_bytes(payload)
        if observed_sha256 != expected_sha256:
            raise GuardError("liveness sentinel hash mismatch")
        final_info = os.fstat(descriptor)
        if final_info.st_dev != info.st_dev or final_info.st_ino != info.st_ino:
            raise GuardError("liveness sentinel identity changed during probe")
    finally:
        os.close(descriptor)
    return {
        "available": True,
        "device": info.st_dev,
        "path": str(path),
        "probeMethod": "uncached-pread",
        "readAheadEnabled": False,
        "dataCacheEnabled": False,
        "sha256": observed_sha256,
        "sizeBytes": size_bytes,
    }


def expected_release_evidence(
    path: Path,
    *,
    mount_realpath: str | None,
    expected_device: int | None,
) -> dict[str, Any]:
    requested = path.expanduser().absolute()
    evidence: dict[str, Any] = {"requestedPath": str(requested), "accepted": False}
    if mount_realpath is None or expected_device is None:
        evidence["reason"] = "mount-identity-unavailable"
        return evidence
    try:
        resolved = requested.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        evidence.update({"error": type(exc).__name__, "errno": exc.errno})
        return evidence
    under_mount = resolved == Path(mount_realpath) or Path(mount_realpath) in resolved.parents
    evidence.update(
        {
            "accepted": info.st_dev == expected_device and under_mount,
            "device": info.st_dev,
            "resolvedPath": str(resolved),
            "underExpectedMount": under_mount,
        }
    )
    return evidence


def capture_probe(
    contract: dict[str, Any],
    *,
    expected_release_path: Path | None = None,
) -> dict[str, Any]:
    mount = Path(contract["mountPoint"])
    volume = volume_metadata(mount)
    mounted_status = volume.get("mounted")
    mount_exists: bool | None = (
        True if mounted_status is True else False if mounted_status is False else None
    )
    candidate_mount_device = volume.get("mountDevice")
    if not isinstance(candidate_mount_device, int):
        candidate_mount_device = None
    mount_realpath: str | None = None
    if mount_exists is True:
        try:
            mount_realpath = str(mount.resolve(strict=True))
        except OSError:
            mount_exists = False
    routes: list[dict[str, Any]] = []
    for expected in contract["protectedPaths"]:
        logical = Path(expected["logicalPath"])
        row: dict[str, Any] = {
            "rootId": expected["rootId"],
            "logicalPath": str(logical),
            "lexists": os.path.lexists(logical),
            "exists": logical.exists(),
            "isSymlink": logical.is_symlink(),
        }
        if logical.is_symlink():
            try:
                row["rawTarget"] = os.readlink(logical)
            except OSError:
                row["rawTarget"] = None
        if logical.exists():
            try:
                resolved = logical.resolve(strict=True)
                info = resolved.stat()
                row.update(
                    {
                        "resolvedPath": str(resolved),
                        "device": info.st_dev,
                        "onExpectedDevice": candidate_mount_device is not None
                        and info.st_dev == candidate_mount_device,
                        "underExpectedMount": mount_realpath is not None
                        and (
                            str(resolved) == mount_realpath
                            or str(resolved).startswith(f"{mount_realpath}{os.sep}")
                        ),
                    }
                )
            except OSError as exc:
                row["error"] = type(exc).__name__
        routes.append(row)

    volume_identity_matches = (
        volume.get("available") is True
        and volume.get("mounted") is True
        and volume.get("mountPoint") == contract["mountPoint"]
        and volume.get("volumeUuid") == contract["volumeUuid"]
    )
    volume_matches = volume_identity_matches and volume.get("readOnly") is False
    routes_match = bool(routes) and all(
        row.get("exists") is True
        and row.get("onExpectedDevice") is True
        and row.get("underExpectedMount") is True
        for row in routes
    )
    liveness: dict[str, Any] = {
        "available": False,
        "probeMethod": "uncached-pread",
        "reason": "volume-or-route-validation-failed",
    }
    if volume_matches and routes_match and candidate_mount_device is not None:
        try:
            liveness = read_liveness_probe(
                contract,
                expected_device=candidate_mount_device,
            )
        except (GuardError, OSError) as exc:
            liveness = {
                "available": False,
                "probeMethod": "uncached-pread",
                "error": type(exc).__name__,
                "reason": str(exc),
            }
            if isinstance(exc, OSError) and exc.errno is not None:
                liveness["errno"] = exc.errno
    try:
        final_mount_device = current_mount_device(mount)
        final_mount_error: dict[str, Any] | None = None
    except OSError as exc:
        final_mount_device = None
        final_mount_error = {
            "error": type(exc).__name__,
            "errno": exc.errno,
        }
    mount_stable = (
        candidate_mount_device is not None
        and final_mount_device == candidate_mount_device
    )
    mount_device = (
        candidate_mount_device
        if mount_stable and volume_identity_matches
        else None
    )
    liveness_matches = liveness.get("available") is True
    healthy = (
        mount_exists is True
        and mount_stable
        and volume_matches
        and routes_match
        and liveness_matches
    )
    if mounted_status is False or (
        candidate_mount_device is not None
        and final_mount_device is None
        and final_mount_error is None
    ):
        state = "dismounted"
    elif mounted_status is not True or final_mount_error is not None:
        state = "io-fault"
    elif not mount_stable:
        state = "identity-mismatch"
    elif volume.get("available") is not True:
        state = "io-fault"
    elif not volume_matches:
        state = "identity-mismatch"
    elif not routes_match:
        state = "route-mismatch"
    elif not liveness_matches:
        state = "io-fault"
    else:
        state = "healthy"
    release_evidence = None
    if expected_release_path is not None:
        release_evidence = expected_release_evidence(
            expected_release_path,
            mount_realpath=mount_realpath,
            expected_device=mount_device,
        )
    return {
        "kind": KIND,
        "capturedAt": utc_now(),
        "contract": {"path": contract["path"], "sha256": contract["sha256"]},
        "expected": {
            "mountPoint": contract["mountPoint"],
            "volumeUuid": contract["volumeUuid"],
        },
        "observed": {
            "mountExists": mount_exists,
            "mountRealpath": mount_realpath,
            "mountDevice": mount_device,
            "mountStable": mount_stable,
            "finalMountError": final_mount_error,
            "volume": volume,
            "liveness": liveness,
            "protectedPaths": routes,
            "expectedRelease": release_evidence,
        },
        "state": state,
        "healthy": healthy,
        "writesAllowed": healthy,
        "failSafeActive": not healthy,
    }


def probe_stalled(
    contract: dict[str, Any],
    *,
    started_at: str,
    timeout_seconds: float,
    probe_id: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": KIND,
        "capturedAt": utc_now(),
        "contract": {"path": contract["path"], "sha256": contract["sha256"]},
        "expected": {
            "mountPoint": contract["mountPoint"],
            "volumeUuid": contract["volumeUuid"],
        },
        "observed": {
            "mountExists": None,
            "mountRealpath": None,
            "mountDevice": None,
            "mountStable": None,
            "volume": {
                "available": False,
                "mounted": None,
                "probeMethod": "supervised-vfs-probe",
                "reason": "probe-timeout",
            },
            "liveness": {
                "available": False,
                "probeMethod": "supervised-volume-probe",
                "reason": "probe-stage-unknown-timeout",
            },
            "protectedPaths": [],
            "expectedRelease": None,
            "supervision": {
                "probeStartedAt": started_at,
                "probeId": probe_id or str(uuid.uuid4()),
                "timeoutSeconds": timeout_seconds,
                "singleFlight": True,
                "timedOut": True,
                "workerReaped": False,
                "workerStillRunning": True,
            },
        },
        "state": "probe-stalled",
        "healthy": False,
        "writesAllowed": False,
        "failSafeActive": True,
    }


def blocked_probe(
    contract: dict[str, Any],
    *,
    state: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "kind": KIND,
        "capturedAt": utc_now(),
        "contract": {"path": contract["path"], "sha256": contract["sha256"]},
        "expected": {
            "mountPoint": contract["mountPoint"],
            "volumeUuid": contract["volumeUuid"],
        },
        "observed": {
            "mountExists": None,
            "mountRealpath": None,
            "mountDevice": None,
            "mountStable": None,
            "volume": {
                "available": False,
                "mounted": None,
                "probeMethod": "off-volume-admission-state",
                "reason": reason,
            },
            "liveness": {
                "available": False,
                "probeMethod": "off-volume-admission-state",
                "reason": reason,
            },
            "protectedPaths": [],
            "expectedRelease": None,
        },
        "state": state,
        "healthy": False,
        "writesAllowed": False,
        "failSafeActive": True,
    }


def acquire_probe_lock(contract: dict[str, Any]) -> int | None:
    """Acquire the one host-local lock without waiting or touching OWC."""

    path = Path(contract["lockPath"])
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise GuardError(f"probe lock parent must be an existing physical directory: {path.parent}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise GuardError("probe lock must be a user-owned regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            os.close(descriptor)
            return None
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return descriptor


def write_probe_lock_marker(
    descriptor: int,
    *,
    probe_id: str,
    result_committed: bool,
) -> None:
    payload = json.dumps(
        {
            "kind": LOCK_MARKER_KIND,
            "probeId": probe_id,
            "resultCommitted": result_committed,
            "updatedAt": utc_now(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise GuardError("probe lock marker write made no progress")
        view = view[written:]
    os.fsync(descriptor)


def read_probe_lock_marker(descriptor: int) -> dict[str, Any] | None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = os.read(descriptor, 4097)
    if not payload or len(payload) > 4096:
        return None
    try:
        marker = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return marker if isinstance(marker, dict) else None


def _child_probe_envelope(
    write_fd: int,
    contract: dict[str, Any],
    capture: Callable[[dict[str, Any]], dict[str, Any]],
    probe_id: str,
) -> None:
    try:
        envelope: dict[str, Any] = {
            "ok": True,
            "probe": capture(contract),
            "probeId": probe_id,
            "workerPid": os.getpid(),
        }
        exit_code = 0
    except BaseException as exc:
        envelope = {
            "ok": False,
            "errorType": type(exc).__name__,
            "error": str(exc),
            "probeId": probe_id,
            "workerPid": os.getpid(),
        }
        exit_code = 70
    payload = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    view = memoryview(payload)
    while view:
        try:
            written = os.write(write_fd, view)
        except InterruptedError:
            continue
        except BrokenPipeError:
            break
        view = view[written:]
    try:
        os.close(write_fd)
    except OSError:
        pass
    os._exit(exit_code)


def _waitpid_nohang(pid: int) -> tuple[bool, int | None]:
    try:
        completed_pid, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True, None
    return completed_pid == pid, status if completed_pid == pid else None


def capture_probe_supervised(
    contract: dict[str, Any],
    *,
    on_stall: Callable[[dict[str, Any]], None],
    on_result: Callable[[dict[str, Any]], None] | None = None,
    capture: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    timeout_seconds: float | None = None,
) -> tuple[dict[str, Any], bool]:
    """Run one volume probe in a killable child under a global nonblocking lock.

    Once the deadline is crossed, the child's result is permanently ineligible.
    The parent records the stall before sending SIGKILL and only polls waitpid
    with WNOHANG; a kernel-stuck child keeps the inherited lock until it exits.
    """

    capture_once = capture or capture_probe
    configured_timeout = float(contract["livenessProbe"]["timeoutSeconds"])
    deadline_seconds = configured_timeout if timeout_seconds is None else float(timeout_seconds)
    if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
        raise GuardError("probe stall timeout must be finite and positive")
    started_at = utc_now()
    started_monotonic = time.monotonic()
    probe_id = str(uuid.uuid4())
    lock_fd = acquire_probe_lock(contract)
    if lock_fd is None:
        return (
            blocked_probe(
                contract,
                state="probe-in-progress",
                reason="global-probe-lock-busy",
            ),
            False,
        )
    try:
        # Invalidate every previously committed observer result before the
        # child can touch OWC.  This marker lives on internal storage and is
        # deliberately fsynced while the global lock is held.  A parent crash,
        # an orphaned child, or a persistence failure therefore leaves
        # admission fail-closed even after the inherited lock is released.
        write_probe_lock_marker(
            lock_fd,
            probe_id=probe_id,
            result_committed=False,
        )
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            pid = os.fork()
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            raise
        if pid == 0:  # pragma: no cover - exercised through parent-visible results.
            try:
                os.close(read_fd)
                _child_probe_envelope(write_fd, contract, capture_once, probe_id)
            finally:
                os._exit(71)

        os.close(write_fd)
        pipe_open = True
        buffer = bytearray()
        status: int | None = None
        timed_out = False
        stalled_probe: dict[str, Any] | None = None
        callback_error: BaseException | None = None
        kill_sent = False
        worker_reaped = False
        reap_deadline: float | None = None

        def close_pipe() -> None:
            nonlocal pipe_open
            if pipe_open:
                os.close(read_fd)
                pipe_open = False

        def start_timeout(*, child_already_reaped: bool) -> None:
            nonlocal timed_out, stalled_probe, callback_error, kill_sent, reap_deadline
            timed_out = True
            stalled_probe = probe_stalled(
                contract,
                started_at=started_at,
                timeout_seconds=deadline_seconds,
                probe_id=probe_id,
            )
            close_pipe()
            try:
                on_stall(stalled_probe)
            except BaseException as exc:
                callback_error = exc
            if not child_already_reaped:
                try:
                    os.kill(pid, 9)
                    kill_sent = True
                except ProcessLookupError:
                    pass
                reap_deadline = time.monotonic() + SUPERVISOR_REAP_GRACE_SECONDS

        try:
            while True:
                reaped, observed_status = _waitpid_nohang(pid)
                elapsed = time.monotonic() - started_monotonic
                if reaped:
                    worker_reaped = True
                    status = observed_status
                    if not timed_out and elapsed >= deadline_seconds:
                        start_timeout(child_already_reaped=True)
                    break

                if not timed_out and elapsed >= deadline_seconds:
                    start_timeout(child_already_reaped=False)

                if timed_out:
                    if reap_deadline is not None and time.monotonic() >= reap_deadline:
                        break
                    time.sleep(SUPERVISOR_POLL_SECONDS)
                    continue

                wait_seconds = min(
                    SUPERVISOR_POLL_SECONDS,
                    max(0.0, deadline_seconds - elapsed),
                )
                readable, _, _ = select.select([read_fd], [], [], wait_seconds)
                if readable:
                    while True:
                        try:
                            chunk = os.read(read_fd, 65536)
                        except BlockingIOError:
                            break
                        except InterruptedError:
                            continue
                        if not chunk:
                            break
                        buffer.extend(chunk)

            if pipe_open and worker_reaped:
                while True:
                    try:
                        chunk = os.read(read_fd, 65536)
                    except BlockingIOError:
                        break
                    except InterruptedError:
                        continue
                    if not chunk:
                        break
                    buffer.extend(chunk)
        finally:
            close_pipe()

        completed_at = utc_now()
        duration_ms = round((time.monotonic() - started_monotonic) * 1000, 3)
        if timed_out:
            assert stalled_probe is not None
            supervision = stalled_probe["observed"]["supervision"]
            supervision.update(
                {
                    "probeCompletedAt": completed_at,
                    "durationMilliseconds": duration_ms,
                    "workerReaped": worker_reaped,
                    "workerStillRunning": not worker_reaped,
                    "killSent": kill_sent,
                    "lateResultDiscarded": bool(buffer),
                    "waitpidStatus": status,
                }
            )
            if on_result is not None:
                on_result(stalled_probe)
            if callback_error is not None:
                raise callback_error
            if on_result is not None:
                write_probe_lock_marker(
                    lock_fd,
                    probe_id=probe_id,
                    result_committed=True,
                )
            return stalled_probe, True

        if len(buffer) > MAX_CHILD_ENVELOPE_BYTES:
            probe = blocked_probe(
                contract,
                state="probe-error",
                reason="child-probe-envelope-oversized",
            )
        else:
            try:
                envelope = json.loads(buffer.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                envelope = None
            if (
                not isinstance(envelope, dict)
                or envelope.get("probeId") != probe_id
                or envelope.get("workerPid") != pid
                or envelope.get("ok") is not True
            ):
                error_type = envelope.get("errorType") if isinstance(envelope, dict) else None
                error_text = envelope.get("error") if isinstance(envelope, dict) else None
                probe = blocked_probe(
                    contract,
                    state="probe-error",
                    reason=f"child-probe-error:{error_type}:{error_text}",
                )
            else:
                probe = envelope.get("probe")
                if not isinstance(probe, dict) or not isinstance(probe.get("observed"), dict):
                    probe = blocked_probe(
                        contract,
                        state="probe-error",
                        reason="child-probe-record-invalid",
                    )
        probe["observed"]["supervision"] = {
            "probeStartedAt": started_at,
            "probeId": probe_id,
            "probeCompletedAt": completed_at,
            "durationMilliseconds": duration_ms,
            "timeoutSeconds": deadline_seconds,
            "singleFlight": True,
            "timedOut": False,
            "workerReaped": True,
            "workerStillRunning": False,
            "waitpidStatus": status,
        }
        if on_result is not None:
            on_result(probe)
            write_probe_lock_marker(
                lock_fd,
                probe_id=probe_id,
                result_committed=True,
            )
        return probe, False
    finally:
        os.close(lock_fd)


def validate_fixture_probe(path: Path, contract: dict[str, Any]) -> dict[str, Any]:
    payload = read_json(path.expanduser().resolve(strict=True))
    if payload.get("kind") != KIND:
        raise GuardError("fixture probe kind is invalid")
    expected = payload.get("expected")
    if not isinstance(expected, dict) or (
        expected.get("mountPoint") != contract["mountPoint"]
        or str(expected.get("volumeUuid", "")).upper() != contract["volumeUuid"]
    ):
        raise GuardError("fixture probe does not match the contract")
    if payload.get("healthy") is not (payload.get("state") == "healthy"):
        raise GuardError("fixture probe health/state mismatch")
    if payload.get("writesAllowed") is not payload.get("healthy"):
        raise GuardError("fixture probe writesAllowed mismatch")
    if payload.get("failSafeActive") is payload.get("healthy"):
        raise GuardError("fixture probe fail-safe mismatch")
    return payload


def parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise GuardError("UTC timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GuardError("UTC timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise GuardError("UTC timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def admission_probe(contract: dict[str, Any]) -> dict[str, Any]:
    """Read one fresh observer result without issuing any OWC filesystem call."""

    lock_fd = acquire_probe_lock(contract)
    if lock_fd is None:
        return blocked_probe(
            contract,
            state="probe-in-progress",
            reason="global-probe-lock-busy",
        )
    try:
        state_path = Path(contract["statePath"])
        try:
            state = read_json(state_path)
        except (OSError, json.JSONDecodeError, GuardError) as exc:
            return blocked_probe(
                contract,
                state="observer-state-missing",
                reason=f"off-volume-state-unavailable:{type(exc).__name__}",
            )
        try:
            updated_at = parse_utc(state.get("updatedAt"))
        except GuardError:
            return blocked_probe(
                contract,
                state="observer-state-stale",
                reason="off-volume-state-timestamp-invalid",
            )
        age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
        if age_seconds < -5 or age_seconds > ADMISSION_STATE_MAX_AGE_SECONDS:
            return blocked_probe(
                contract,
                state="observer-state-stale",
                reason=f"off-volume-state-age-seconds:{round(age_seconds, 3)}",
            )

        probe = state.get("lastProbe")
        if not isinstance(probe, dict):
            return blocked_probe(
                contract,
                state="observer-state-invalid",
                reason="off-volume-state-has-no-probe",
            )
        probe_contract = probe.get("contract")
        expected = probe.get("expected")
        observed = probe.get("observed")
        issue = probe_issue(probe)
        if (
            state.get("kind") != STATE_KIND
            or not isinstance(probe_contract, dict)
            or probe_contract.get("sha256") != contract["sha256"]
            or not isinstance(expected, dict)
            or expected.get("mountPoint") != contract["mountPoint"]
            or expected.get("volumeUuid") != contract["volumeUuid"]
            or not isinstance(observed, dict)
            or state.get("activeIssue") != issue
        ):
            return blocked_probe(
                contract,
                state="observer-state-invalid",
                reason="off-volume-state-contract-or-issue-mismatch",
            )

        supervision = observed.get("supervision")
        probe_id = supervision.get("probeId") if isinstance(supervision, dict) else None
        marker = read_probe_lock_marker(lock_fd)
        if (
            not isinstance(marker, dict)
            or marker.get("kind") != LOCK_MARKER_KIND
            or marker.get("resultCommitted") is not True
            or not isinstance(probe_id, str)
            or not probe_id
            or marker.get("probeId") != probe_id
        ):
            return blocked_probe(
                contract,
                state="observer-state-uncommitted",
                reason="off-volume-state-has-no-matching-commit-marker",
            )

        healthy = probe.get("healthy") is True
        if healthy:
            routes = observed.get("protectedPaths")
            expected_routes = {
                (row["rootId"], row["logicalPath"])
                for row in contract["protectedPaths"]
            }
            accepted_routes = {
                (row.get("rootId"), row.get("logicalPath"))
                for row in routes
                if isinstance(row, dict)
                and row.get("exists") is True
                and row.get("onExpectedDevice") is True
                and row.get("underExpectedMount") is True
            } if isinstance(routes, list) else set()
            if (
                probe.get("state") != "healthy"
                or probe.get("writesAllowed") is not True
                or probe.get("failSafeActive") is not False
                or not isinstance(observed.get("liveness"), dict)
                or observed["liveness"].get("available") is not True
                or accepted_routes != expected_routes
                or not isinstance(supervision, dict)
                or supervision.get("timedOut") is not False
                or supervision.get("workerReaped") is not True
                or supervision.get("workerStillRunning") is not False
            ):
                return blocked_probe(
                    contract,
                    state="observer-state-invalid",
                    reason="off-volume-healthy-state-evidence-incomplete",
                )
        elif (
            probe.get("writesAllowed") is not False
            or probe.get("failSafeActive") is not True
            or issue is None
        ):
            return blocked_probe(
                contract,
                state="observer-state-invalid",
                reason="off-volume-unhealthy-state-evidence-incomplete",
            )

        observed["admission"] = {
            "probeMethod": "fresh-off-volume-observer-state",
            "statePath": str(state_path),
            "stateUpdatedAt": state["updatedAt"],
            "stateAgeSeconds": round(max(age_seconds, 0.0), 3),
            "globalProbeLockAcquired": True,
        }
        return probe
    finally:
        os.close(lock_fd)


def assert_off_volume_path(path: Path, probe: dict[str, Any]) -> None:
    mount = Path(str((probe.get("expected") or {}).get("mountPoint", ""))).expanduser()
    candidate = path.expanduser().absolute()
    try:
        candidate.relative_to(mount)
    except ValueError:
        pass
    else:
        raise GuardError(f"guard state/incident path must be off OWC: {candidate}")
    existing = candidate.parent
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.exists():
        raise GuardError(f"no existing parent for off-volume path: {candidate}")
    observed = probe.get("observed") or {}
    mount_device = observed.get("mountDevice")
    volume = observed.get("volume")
    if (
        not isinstance(mount_device, int)
        and isinstance(volume, dict)
        and volume.get("mounted") is True
        and isinstance(volume.get("mountDevice"), int)
    ):
        mount_device = volume["mountDevice"]
    if (
        isinstance(mount_device, int)
        and existing.stat().st_dev == mount_device
    ):
        raise GuardError(f"guard state/incident parent is on the OWC device: {existing}")


def load_previous_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = read_json(path)
    if payload.get("kind") != STATE_KIND:
        raise GuardError("existing OWC guard state kind is invalid")
    return payload


def probe_issue(probe: dict[str, Any]) -> str | None:
    if probe.get("healthy") is True:
        return None
    state = str(probe.get("state", ""))
    if state in {
        "probe-stalled",
        "io-fault",
        "dismounted",
        "identity-mismatch",
        "route-mismatch",
    }:
        return state
    return "probe-failed"


def issue_event_prefix(issue: str) -> str:
    if issue == "probe-stalled":
        return "probe-stall"
    if issue == "dismounted":
        return "dismount"
    return issue


def observe(
    *,
    probe: dict[str, Any],
    state_file: Path,
    incident_dir: Path,
    expected_release_path: Path | None,
) -> dict[str, Any]:
    assert_off_volume_path(state_file, probe)
    assert_off_volume_path(incident_dir / "placeholder.json", probe)
    previous = load_previous_state(state_file)
    previous_probe = previous.get("lastProbe") if isinstance(previous, dict) else None
    previous_issue = previous.get("activeIssue") if isinstance(previous, dict) else None
    if not isinstance(previous_issue, str) or not previous_issue:
        previous_issue = "dismounted" if previous and previous.get("activeDismount") else None
    current_issue = probe_issue(probe)
    event_type: str | None = None
    event_issue: str | None = None
    if current_issue is not None and current_issue != previous_issue:
        event_issue = current_issue
        event_type = f"{issue_event_prefix(current_issue)}-detected"
    elif current_issue is None and previous_issue is not None:
        event_issue = previous_issue
        event_type = f"{issue_event_prefix(previous_issue)}-recovery"
    active_dismount = current_issue == "dismounted"

    incident_path: Path | None = None
    if event_type is not None:
        incident_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        incident_path = incident_dir / f"owc-{event_type}-{stamp}.json"
        release_verified = False
        release_text = (
            str(expected_release_path.expanduser().absolute())
            if expected_release_path is not None
            else None
        )
        if expected_release_path is not None:
            release_evidence = (probe.get("observed") or {}).get("expectedRelease")
            release_verified = (
                probe.get("healthy") is True
                and isinstance(release_evidence, dict)
                and release_evidence.get("requestedPath") == release_text
                and release_evidence.get("accepted") is True
            )
        recovering = event_type.endswith("-recovery")
        dismount_event = event_issue == "dismounted"
        stall_event = event_issue == "probe-stalled"
        io_fault_event = event_issue == "io-fault"
        incident = {
            "kind": INCIDENT_KIND,
            "eventType": event_type,
            "capturedAt": utc_now(),
            "activeIssue": current_issue,
            "previousIssue": previous_issue,
            "affectedIssue": event_issue,
            "transition": "recovered" if recovering else "detected",
            "storageFaultDetected": (
                not recovering
                and event_issue in {"probe-stalled", "io-fault", "dismounted"}
            ),
            "dismountDetected": dismount_event and not recovering,
            "stallDetected": stall_event and not recovering,
            "ioFaultDetected": io_fault_event and not recovering,
            "recoveryVerified": recovering and probe.get("healthy") is True,
            "sameTargetReleaseVerified": release_verified,
            "releasePath": release_text,
            "previousProbe": previous_probe,
            "currentProbe": probe,
            "previousProbeId": (
                ((previous_probe or {}).get("observed") or {}).get("supervision") or {}
            ).get("probeId") if isinstance(previous_probe, dict) else None,
            "currentProbeId": (
                ((probe.get("observed") or {}).get("supervision") or {}).get("probeId")
            ),
            "failSafeBehavior": {
                "writesAllowedWhileUnhealthy": False,
                "automaticRemountAttempted": False,
                "serviceRestartAttempted": False,
                "automaticFallbackPath": False,
            },
        }
        atomic_json(incident_path, incident)

    state = {
        "kind": STATE_KIND,
        "updatedAt": utc_now(),
        "activeIssue": current_issue,
        "activeDismount": active_dismount,
        "lastProbe": probe,
        "lastIncident": str(incident_path) if incident_path else (previous or {}).get("lastIncident"),
    }
    atomic_json(state_file, state)
    return {
        "kind": STATE_KIND,
        "result": "healthy" if probe.get("healthy") is True else "blocked-fail-safe",
        "stateFile": str(state_file),
        "incident": str(incident_path) if incident_path else None,
        "probe": probe,
    }


def assert_write_target(
    target: Path,
    probe: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    if probe.get("healthy") is not True or probe.get("writesAllowed") is not True:
        return {
            "accepted": False,
            "target": str(target),
            "reason": "owc_mount_not_healthy",
        }
    normalized = Path(os.path.abspath(os.fspath(target.expanduser())))
    candidates: list[tuple[int, dict[str, str]]] = []
    for row in contract["protectedPaths"]:
        root = Path(row["logicalPath"])
        try:
            normalized.relative_to(root)
        except ValueError:
            continue
        candidates.append((len(root.parts), row))
    if not candidates:
        return {
            "accepted": False,
            "target": str(target),
            "normalizedTarget": str(normalized),
            "reason": "target_outside_protected_roots",
        }
    _, protected_root = max(candidates, key=lambda item: item[0])
    routes = ((probe.get("observed") or {}).get("protectedPaths"))
    evidence = next(
        (
            row
            for row in routes
            if isinstance(row, dict)
            and row.get("rootId") == protected_root["rootId"]
            and row.get("logicalPath") == protected_root["logicalPath"]
        ),
        None,
    ) if isinstance(routes, list) else None
    accepted = (
        isinstance(evidence, dict)
        and evidence.get("exists") is True
        and evidence.get("onExpectedDevice") is True
        and evidence.get("underExpectedMount") is True
    )
    return {
        "accepted": accepted,
        "target": str(target),
        "normalizedTarget": str(normalized),
        "protectedRootId": protected_root["rootId"],
        "protectedRoot": protected_root["logicalPath"],
        "routeEvidence": evidence,
        "reason": "accepted" if accepted else "protected_root_not_on_pinned_owc_mount",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed OWC mount/route guard")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inspect")
    write_parser = subparsers.add_parser("assert-write")
    write_parser.add_argument("--target", required=True, type=Path)
    observe_parser = subparsers.add_parser("observe")
    observe_parser.add_argument("--state-file", required=True, type=Path)
    observe_parser.add_argument("--incident-dir", required=True, type=Path)
    observe_parser.add_argument("--expected-release-path", type=Path)
    return parser


def retired_cli_result(command: str) -> dict[str, Any]:
    """Return a fail-closed result without reading the contract or OWC volume."""

    return {
        "kind": KIND,
        "result": "blocked-fail-safe",
        "state": "retired-no-active-probe",
        "command": command,
        "retiredAt": ACTIVE_PROBE_RETIRED_AT,
        "reason": ACTIVE_PROBE_RETIREMENT_REASON,
        "healthy": False,
        "writesAllowed": False,
        "failSafeActive": True,
        "storageTouched": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # This fence intentionally precedes contract normalization.  The default
    # contract resolves through the OWC-backed workspace, so even parsing it
    # would violate the retirement guarantee during a mounted I/O wedge.
    if ACTIVE_PROBE_RETIRED:
        result = retired_cli_result(args.command)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 3

    try:
        contract = normalize_contract(args.contract)
        if args.command == "observe":
            state_file = args.state_file.expanduser().absolute()
            incident_dir = args.incident_dir.expanduser().absolute()
            persisted_result: dict[str, Any] | None = None
            if state_file != Path(contract["statePath"]) or incident_dir != Path(
                contract["incidentDirectory"]
            ):
                raise GuardError(
                    "observer state/incident arguments must match the installed contract"
                )

            def persist_probe(completed_probe: dict[str, Any]) -> None:
                nonlocal persisted_result
                current_result = observe(
                    probe=completed_probe,
                    state_file=state_file,
                    incident_dir=incident_dir,
                    expected_release_path=args.expected_release_path,
                )
                if (
                    persisted_result is not None
                    and current_result.get("incident") is None
                    and persisted_result.get("incident") is not None
                ):
                    current_result["incident"] = persisted_result["incident"]
                persisted_result = current_result

            probe, _ = capture_probe_supervised(
                contract,
                on_stall=persist_probe,
                on_result=persist_probe,
                capture=lambda normalized: capture_probe(
                    normalized,
                    expected_release_path=args.expected_release_path,
                ),
            )
        else:
            probe = admission_probe(contract)
        if args.command == "inspect":
            result = probe
            accepted = probe.get("healthy") is True
        elif args.command == "assert-write":
            result = assert_write_target(args.target, probe, contract)
            accepted = result["accepted"] is True
        else:
            if probe.get("state") == "probe-in-progress":
                result = probe
            else:
                if persisted_result is None:
                    raise GuardError("supervised observer did not persist its result")
                result = persisted_result
            accepted = probe.get("healthy") is True
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if accepted else 3
    except (GuardError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"kind": KIND, "result": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
