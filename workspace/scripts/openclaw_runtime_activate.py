#!/usr/bin/env python3
"""Activate one OpenClaw release from a stopped predecessor snapshot.

Same-version activation changes only the selector. Explicit version upgrades
install reviewed config and invoke the candidate's native state migration under
the same start fence. The matching predecessor state and release remain the
sole rollback target; this owner never retries migration or lifecycle work.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import importlib.util
import json
import os
import posixpath
from pathlib import Path
import plistlib
import pwd
import re
import stat
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Protocol

try:
    from .operator_contract import load_operator_contract
except ImportError:  # direct script execution
    from operator_contract import load_operator_contract

OPERATOR = load_operator_contract()


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_COMMAND_BYTES = 1024 * 1024
DEFAULT_HEALTH_TIMEOUT_SECONDS = 900.0
SNAPSHOT_TIMEOUT_SECONDS = 3600.0
START_CONSUMED_NAME = "activation-start-consumed.json"
RETIREMENT_RECEIPT_NAME = "retirement-receipt.json"
HISTORY_VOLUME_CONTRACT = Path(__file__).resolve().parents[1] / "registry" / "external_volume_guard.json"
SCREEN_CAPTURE_ROUTE = "gateway cron command -> Python -> Python new session -> Python new session -> Peekaboo Journal window capture"


def configured_screen_capture_binding() -> Path | None:
    if OPERATOR.get("paths.screen_capture_binding") is None:
        return None
    return OPERATOR.require_path("paths.screen_capture_binding")


def runtime_gateway_port() -> int:
    port = OPERATOR.require_int("runtime.gateway_port")
    if not 1 <= port <= 65535:
        raise ValueError("runtime.gateway_port must be between 1 and 65535")
    return port


def required_bundled_extensions() -> tuple[str, ...]:
    values = OPERATOR.require_list("runtime.required_extensions")
    if not values or any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None for value in values):
        raise ValueError("runtime.required_extensions must contain resolved plugin IDs")
    if len(set(values)) != len(values):
        raise ValueError("runtime.required_extensions must not contain duplicate IDs")
    return tuple(values)


def expected_auth_order_count() -> int:
    count = OPERATOR.require_int("runtime.expected_auth_order_count")
    if count < 1:
        raise ValueError("runtime.expected_auth_order_count must be positive")
    return count


def expected_auth_order_sha256() -> str:
    digest = OPERATOR.require_string("runtime.expected_auth_order_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("runtime.expected_auth_order_sha256 must be lowercase SHA-256")
    return digest


EXEC_APPROVALS_INSPECTION_KEYS = frozenset(
    {
        "schemaVersion",
        "databasePresent",
        "rowPresent",
        "rowCount",
        "currentKeyOnly",
        "legacySourceAbsent",
        "doctorClaimAbsent",
        "schemaValid",
        "projectionsValid",
        "expectedSocketPathMatched",
        "tokenPresent",
        "agentCount",
        "allowlistCount",
        "semanticSha256",
        "rawCasSha256",
        "authProfileStateReadable",
        "authProfileStoreReadable",
        "openAIProfileOrderCount",
        "openAIProfileOrderSha256",
        "openAIProfileOrderCredentialsUsable",
    }
)
EXEC_APPROVALS_REQUIRED_TRUE = (
    "databasePresent",
    "rowPresent",
    "currentKeyOnly",
    "legacySourceAbsent",
    "doctorClaimAbsent",
    "schemaValid",
    "projectionsValid",
    "expectedSocketPathMatched",
    "tokenPresent",
    "authProfileStateReadable",
    "authProfileStoreReadable",
    "openAIProfileOrderCredentialsUsable",
)
EXEC_APPROVALS_SEMANTIC_BINDING_KEYS = (
    "semanticSha256",
    "authProfileStateReadable",
    "authProfileStoreReadable",
    "openAIProfileOrderCount",
    "openAIProfileOrderSha256",
    "openAIProfileOrderCredentialsUsable",
)
BOOTSTRAP_BINDING_KEYS = frozenset(
    {
        "release",
        "releaseDevice",
        "releaseInode",
        "selectorDevice",
        "selectorInode",
        "startedAtUs",
    }
)
COMMAND_EVIDENCE_KEYS = frozenset(
    {
        "purpose",
        "executable",
        "argumentVectorSha256",
        "returnCode",
        "timedOut",
        "durationMs",
        "stdoutBytes",
        "stdoutSha256",
        "stderrBytes",
        "stderrSha256",
    }
)


class ActivationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActivationPaths:
    releases_root: Path
    current_link: Path
    package_link: Path
    bin_link: Path
    gateway_plist: Path
    node_plist: Path
    node: Path
    node_alias: Path
    predecessor_state_dir: Path
    candidate_state_dir: Path
    lock: Path
    result: Path
    operator_uid: int
    command_timeout_seconds: float = 120.0
    health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS
    health_poll_seconds: float = 0.25
    screen_capture_binding: Path | None = None


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_ms: int
    timed_out: bool

    def evidence(self, purpose: str) -> dict[str, Any]:
        return {
            "purpose": purpose,
            "executable": self.argv[0],
            "argumentVectorSha256": sha256_bytes(canonical_json_bytes(list(self.argv))),
            "returnCode": self.returncode,
            "timedOut": self.timed_out,
            "durationMs": self.duration_ms,
            "stdoutBytes": len(self.stdout),
            "stdoutSha256": sha256_bytes(self.stdout),
            "stderrBytes": len(self.stderr),
            "stderrSha256": sha256_bytes(self.stderr),
        }


class ActivationBackend(Protocol):
    command_evidence: list[dict[str, Any]]
    def assert_gateway_and_node_stopped(self) -> None: ...
    def inspect_exec_approvals(self, release: Path) -> dict[str, Any]: ...
    def verify_screen_capture_continuity(self) -> dict[str, Any] | None: ...
    def verify_candidate_discord(self, release: Path, config_path: Path) -> dict[str, Any]: ...
    def migrate_state_once(self, release: Path) -> dict[str, Any]: ...
    def bootstrap_gateway_once(self) -> None: ...
    def bootstrap_node_once(self) -> None: ...
    def bootstrap_bindings(self) -> dict[str, dict[str, Any]]: ...
    def bind_bootstrap_receipt(
        self,
        label: str,
        binding: dict[str, Any],
        loaded: dict[str, Any] | None = None,
    ) -> None: ...
    def verify(self, release: Path, device: int, inode: int) -> dict[str, Any]: ...
    def verify_node(self, release: Path, device: int, inode: int) -> dict[str, Any]: ...


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def require_commit(value: str, label: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ActivationError(f"{label} must be one lowercase 40-hex commit")
    return value


def bootstrap_binding_shape_is_valid(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != BOOTSTRAP_BINDING_KEYS:
        return False
    release = value.get("release")
    return (
        isinstance(release, str)
        and Path(release).is_absolute()
        and all(
            type(value.get(key)) is int and value[key] > 0
            for key in BOOTSTRAP_BINDING_KEYS - {"release"}
        )
    )


def receipt_has_successful_command(
    value: Any,
    purpose: str,
    argv: tuple[str, ...],
) -> bool:
    if not isinstance(value, list):
        return False
    matches = [
        item
        for item in value
        if isinstance(item, dict) and item.get("purpose") == purpose
    ]
    if len(matches) != 1 or set(matches[0]) != COMMAND_EVIDENCE_KEYS:
        return False
    match = matches[0]
    return (
        len(argv) > 0
        and match.get("executable") == argv[0]
        and match.get("argumentVectorSha256")
        == sha256_bytes(canonical_json_bytes(list(argv)))
        and type(match.get("returnCode")) is int
        and match["returnCode"] == 0
        and match.get("timedOut") is False
        and type(match.get("durationMs")) is int
        and match["durationMs"] >= 0
        and all(
            type(match.get(key)) is int and match[key] >= 0
            for key in ("stdoutBytes", "stderrBytes")
        )
        and all(
            isinstance(match.get(key), str)
            and re.fullmatch(r"[0-9a-f]{64}", match[key]) is not None
            for key in ("stdoutSha256", "stderrSha256")
        )
    )


def read_physical(path: Path, label: str, maximum: int = MAX_FILE_BYTES) -> tuple[bytes, os.stat_result]:
    try:
        before = os.lstat(path)
        resolved = path.resolve(strict=True)
        payload = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise ActivationError(f"{label} is unavailable") from exc
    identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_nlink, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or resolved != path
            or identity(before) != identity(after) or len(payload) != before.st_size
            or len(payload) > maximum):
        raise ActivationError(f"{label} identity drift")
    return payload, after


def read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes, os.stat_result]:
    payload, info = read_physical(path, label)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ActivationError(f"{label} must be a JSON object")
    return value, payload, info


def sha256_physical_file(path: Path, label: str) -> tuple[str, os.stat_result]:
    try:
        before = os.lstat(path)
        resolved = path.resolve(strict=True)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ActivationError(f"{label} is unavailable") from exc
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or resolved != path
        or identity(before) != identity(opened)
        or identity(opened) != identity(after)
    ):
        raise ActivationError(f"{label} identity drift")
    return digest.hexdigest(), after



def screen_capture_permission(database: Path, client: Path) -> dict[str, Any]:
    """Read one exact permission prerequisite, never infer effective capture from it."""
    if not database.is_absolute() or database.resolve(strict=True) != database:
        raise ActivationError("ScreenCapture permission database is not physical")
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                "SELECT client_type,auth_value,auth_reason,auth_version,csreq,last_modified "
                "FROM access WHERE service=? AND client=?",
                ("kTCCServiceScreenCapture", str(client)),
            ).fetchmany(2)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ActivationError("ScreenCapture permission is unknown") from exc
    if len(rows) != 1 or rows[0][0] != 1 or rows[0][1] != 2 or not isinstance(rows[0][4], bytes):
        raise ActivationError("ScreenCapture exact client permission is not definitely allowed")
    row = rows[0]
    verify_screen_capture_requirement(client, row[4])
    return {"database": str(database), "clientType": row[0], "authValue": row[1],
            "authReason": row[2], "authVersion": row[3],
            "csreqSha256": sha256_bytes(row[4]), "lastModified": row[5]}


def verify_screen_capture_requirement(client: Path, requirement: bytes) -> None:
    """Check the database requirement against the exact client, not its self-signature."""
    if not requirement or len(requirement) > MAX_COMMAND_BYTES:
        raise ActivationError("ScreenCapture permission requirement is invalid")
    with tempfile.TemporaryDirectory(prefix="openclaw-tcc-requirement-") as directory:
        compiled = Path(directory) / "requirement.bin"
        compiled.write_bytes(requirement)
        compiled.chmod(0o400)
        result = run_bounded(("/usr/bin/codesign", "--verify", "--strict",
                              "--test-requirement", str(compiled), str(client)), 30)
    if result.returncode != 0 or result.timed_out:
        raise ActivationError("ScreenCapture permission requirement does not match the exact executable")


def screen_capture_code_identity(path: Path) -> dict[str, Any]:
    digest, before = sha256_physical_file(path, "ScreenCapture executable")
    verified = run_bounded(("/usr/bin/codesign", "--verify", "--strict", str(path)), 30)
    details = run_bounded(("/usr/bin/codesign", "-d", "--verbose=4", "-r-", str(path)), 30)
    if any(item.returncode != 0 or item.timed_out for item in (verified, details)):
        raise ActivationError("ScreenCapture executable signature is unknown")
    try:
        text = (details.stdout + details.stderr).decode("utf-8", "strict")
        selected = {}
        for prefix in ("designated => ", "Identifier=", "TeamIdentifier=", "CDHash="):
            values = [line for line in text.splitlines() if line.startswith(prefix)]
            if len(values) != 1:
                raise ActivationError("ScreenCapture signing identity is ambiguous")
            selected[prefix] = values[0][len(prefix):]
    except UnicodeError as exc:
        raise ActivationError("ScreenCapture signing identity is unreadable") from exc
    after = path.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise ActivationError("ScreenCapture executable changed during inspection")
    return {"path": str(path), "device": before.st_dev, "inode": before.st_ino,
            "bytes": before.st_size, "mtimeNs": before.st_mtime_ns,
            "sha256": digest, "signingIdentity": selected}


def load_screen_capture_acceptance(path: Path, expected_sha256: str) -> dict[str, Any]:
    proof, payload, _ = read_json(path, "ScreenCapture native acceptance")
    if (re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or sha256_bytes(payload) != expected_sha256
            or proof.get("status") != "current_scheduler_route_capture_and_responsibility_verified"
            or proof.get("route") != SCREEN_CAPTURE_ROUTE
            or proof.get("imageVisuallyVerifiedAsJournal") is not True
            or proof.get("manualProbe") is not True
            or proof.get("scheduledSyncProven") is not False
            or proof.get("producerInvoked") is not False
            or proof.get("tccMutated") is not False):
        raise ActivationError("ScreenCapture native acceptance is missing or not bound")
    pid, identity = proof.get("responsiblePid"), proof.get("responsibleProcessIdentity")
    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(identity, list) or len(identity) != 3
            or not all(isinstance(item, str) for item in identity)
            or not Path(identity[1]).is_absolute()):
        raise ActivationError("ScreenCapture responsible process is unknown")
    process_start_time_us(identity[0])
    files = proof.get("files")
    if (not isinstance(files, list) or len(files) != 2
            or not all(isinstance(row, dict) for row in files)
            or files[0].get("path") != identity[1]):
        raise ActivationError("ScreenCapture responsible executable binding is invalid")
    messages = proof.get("tccdAttributionAndEffectivePermission", [])
    if not isinstance(messages, list):
        raise ActivationError("ScreenCapture native attribution is malformed")
    subject = "from Sub:{" + identity[1] + "}Resp:"
    if not any(isinstance(row, dict) and isinstance(row.get("message"), str)
               and subject in row["message"]
               and f"pid={pid}," in row["message"]
               and "kTCCServiceScreenCapture" in row["message"]
               and "Auth Right: Allowed (System Set)" in row["message"]
               and "DB Action:None" in row["message"] for row in messages):
        raise ActivationError("ScreenCapture effective permission attribution is unknown")
    return proof


def enroll_screen_capture_binding(paths: ActivationPaths, acceptance: Path,
                                  acceptance_sha256: str, database: Path,
                                  output: Path) -> dict[str, Any]:
    """Bind an already performed native capture; never capture or change permission."""
    proof = load_screen_capture_acceptance(acceptance, acceptance_sha256)
    current = process_identity(proof["responsiblePid"])
    if [str(item) for item in current] != proof["responsibleProcessIdentity"]:
        raise ActivationError("ScreenCapture native proof is from a different process")
    files = [screen_capture_code_identity(Path(row["path"])) for row in proof["files"]]
    if Path(files[0]["path"]) != paths.node:
        raise ActivationError("ScreenCapture responsible client differs from activation Node")
    for actual, expected in zip(files, proof["files"]):
        if expected.get("codesignVerified") is not True or any(
                actual[key] != expected.get(key)
                for key in ("path", "device", "inode", "bytes", "mtimeNs", "sha256")):
            raise ActivationError("ScreenCapture executable differs from native proof")
    evidence = screen_capture_runtime_evidence(
        paths.result, paths.node, expected=proof, responsible_pid=proof["responsiblePid"])
    binding = {"schemaVersion": 1, "route": SCREEN_CAPTURE_ROUTE,
               "nativeAcceptancePath": str(acceptance), "nativeAcceptanceSha256": acceptance_sha256,
               "activationReceiptPath": str(paths.result),
               **evidence,
               "responsiblePid": proof["responsiblePid"],
               "responsibleProcessIdentity": proof["responsibleProcessIdentity"],
               "files": files, "permission": screen_capture_permission(database, paths.node),
               "verifiedAt": proof["verifiedAt"], "enrolledAt": utc_now(),
               "scheduledSyncProven": False}
    if ([str(item) for item in process_identity(proof["responsiblePid"])]
            != proof["responsibleProcessIdentity"]
            or screen_capture_permission(database, paths.node) != binding["permission"]):
        raise ActivationError("ScreenCapture native boundary changed during enrollment")
    screen_capture_runtime_evidence(
        paths.result, paths.node, expected=evidence, responsible_pid=proof["responsiblePid"])
    create_immutable_file(output, json.dumps(binding, indent=2, sort_keys=True).encode() + b"\n")
    return binding


def _screen_capture_file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _history_volume_guard():
    # Also work under the activator's isolated (-I -S) interpreter. Reuse the
    # existing metadata reader without enabling its retired liveness probe.
    name = "openclaw_activation_volume_metadata"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("external_volume_guard.py"))
        if spec is None or spec.loader is None:
            raise ActivationError("activation history volume reader is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


def historical_volume_uuid(path: Path, info: os.stat_result) -> str:
    try:
        value = _history_volume_guard().read_volume_uuid(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ActivationError("activation history volume UUID is unavailable") from exc
    if (not isinstance(value, str)
            or re.fullmatch(r"[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}", value) is None
            or value == "00000000-0000-0000-0000-000000000000"
            or _screen_capture_file_identity(os.lstat(path)) != _screen_capture_file_identity(info)):
        raise ActivationError("activation history volume changed during inspection")
    return value


def historical_identity_matches(binding: dict[str, Any], path: Path, info: os.stat_result) -> bool:
    """Historical observation only; never use for process or mutation admission."""
    if (binding.get("path") != str(path) or type(binding.get("device")) is not int
            or type(binding.get("inode")) is not int or binding["inode"] != info.st_ino
            or path.resolve(strict=True) != path):
        return False
    if "volumeUuid" in binding:
        return binding["volumeUuid"] == historical_volume_uuid(path, info)
    # A legacy record has no cross-mount authority. Only the locked retirement
    # owner below can revalidate a legacy release against its sealed inventory.
    return binding["device"] == info.st_dev


def historical_release_identity_matches(record: dict[str, Any], proof: Any, path: Path) -> bool:
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o222
            or record.get("path") != str(path)):
        return False
    if proof is None:
        return historical_identity_matches(record, path, info)
    return (isinstance(proof, dict)
            and set(proof) == {"path", "device", "inode", "volumeUuid", "recordSha256"}
            and proof.get("inode") == record.get("inode")
            and proof.get("recordSha256") == sha256_bytes(canonical_json_bytes(record))
            and historical_identity_matches(proof, path, info))


def retirement_release_identities(receipt: dict[str, Any]) -> dict[str, Any]:
    if "releaseIdentities" not in receipt:
        return {}
    identities = receipt["releaseIdentities"]
    if (not isinstance(identities, dict)
            or set(identities) not in ({"candidate", "selected"}, {"candidate", "selected", "rollback"})
            or any(not isinstance(value, dict) for value in identities.values())):
        raise ActivationError("activation retirement release identities are invalid")
    return identities


def _terminal_release_identity(paths: ActivationPaths, record: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(record.get("path", "")))
    before = os.lstat(path)
    if (path.parent != paths.releases_root or path.resolve(strict=True) != path
            or not stat.S_ISDIR(before.st_mode) or stat.S_IMODE(before.st_mode) & 0o222
            or type(record.get("device")) is not int or type(record.get("inode")) is not int
            or record["inode"] != before.st_ino):
        raise ActivationError("terminal activation release identity drift")
    volume_uuid = historical_volume_uuid(path, before)
    if record["device"] != before.st_dev:
        guard = _history_volume_guard()
        try:
            contract = guard.normalize_contract(HISTORY_VOLUME_CONTRACT)
            mount = Path(contract["mountPoint"])
            metadata = guard.volume_metadata(mount)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            raise ActivationError("terminal activation registered volume is unavailable") from exc
        if (mount.resolve(strict=True) != mount or mount not in paths.releases_root.parents
                or metadata.get("available") is not True or metadata.get("mounted") is not True
                or metadata.get("mountDevice") != before.st_dev
                or metadata.get("volumeUuid") != volume_uuid or contract["volumeUuid"] != volume_uuid):
            raise ActivationError("terminal activation registered volume identity drift")
        observed = validate_release(paths, path, require_commit(record.get("commit"), "terminal release commit"),
                                    require_candidate_native_entrypoints=False)
        if any(record.get(key) != value for key, value in observed.items() if key != "device"):
            raise ActivationError("terminal activation sealed release inventory drift")
    if historical_volume_uuid(path, before) != volume_uuid:
        raise ActivationError("terminal activation volume changed during inspection")
    return {"path": str(path), "device": before.st_dev, "inode": before.st_ino,
            "volumeUuid": volume_uuid, "recordSha256": sha256_bytes(canonical_json_bytes(record))}


def _validate_terminal_candidate_seal(candidate: dict[str, Any]) -> None:
    value, payload, info = read_json(Path(str(candidate.get("sealPath", ""))), "terminal candidate seal")
    if (stat.S_IMODE(info.st_mode) & 0o222 or value.get("schemaVersion") != 1
            or sha256_bytes(payload) != candidate.get("sealSha256")
            or value.get("candidate") != {key: candidate.get(key) for key in
                    ("path", "commit", "device", "inode", "packageVersion", "releaseInventory")}
            or not isinstance(value.get("createdAt"), str)
            or value.get("bundledExtensions") != validate_bundled_extensions(Path(candidate["path"]))):
        raise ActivationError("terminal activation candidate seal provenance drift")


def _read_screen_capture_retirement_json(path: Path, label: str) -> tuple[dict[str, Any], bytes, os.stat_result]:
    """Read a small immutable owner record without opening a device or FIFO."""
    before = os.lstat(path)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o400
            or before.st_size > 256 * 1024 or path.resolve(strict=True) != path):
        raise ActivationError(f"{label} identity drift")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        if _screen_capture_file_identity(opened) != _screen_capture_file_identity(before):
            raise ActivationError(f"{label} changed before reading")
        payload = os.read(descriptor, 256 * 1024 + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (len(payload) != before.st_size
            or _screen_capture_file_identity(after) != _screen_capture_file_identity(before)
            or _screen_capture_file_identity(os.lstat(path)) != _screen_capture_file_identity(before)):
        raise ActivationError(f"{label} changed during reading")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ActivationError(f"{label} must be a JSON object")
    return value, payload, after


def _screen_capture_selected_release(path: Path, selected: dict[str, Any], identity: Any = None) -> os.stat_result:
    selected_link = path.parent / "current"
    selected_path = Path(selected["path"])
    selected_info = os.lstat(selected_path)
    if (not selected_path.is_absolute() or not selected_link.is_symlink()
            or os.readlink(selected_link) != str(selected_path)
            or not stat.S_ISDIR(selected_info.st_mode)
            or selected_path.resolve(strict=True) != selected_path
            or not historical_release_identity_matches(selected, identity, selected_path)):
        raise ActivationError("ScreenCapture selected activation drift")
    return os.lstat(selected_link)


def _screen_capture_restored_evidence(
    path: Path, result: dict[str, Any], payload: bytes, restore_path: Path,
    fence_path: Path, node: Path, responsible_pid: int | None,
    selected_identity: Any = None,
) -> dict[str, str]:
    """Bind the existing restore owner; never promote a stopped or uncertain restore."""
    snapshot = result.get("snapshot")
    if (result.get("outcome") != "snapshot_restore_required"
            or result.get("restoreRequired") is not True or not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("manifestSha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", snapshot["manifestSha256"]) is None
            or restore_path.name != f"activation-restore-result-{snapshot['manifestSha256'][:16]}.json"):
        raise ActivationError("ScreenCapture restore owner is not established")
    restored, restored_bytes, _ = _read_screen_capture_retirement_json(restore_path, "ScreenCapture restore result")
    fence, fence_bytes, _ = _read_screen_capture_retirement_json(fence_path, "ScreenCapture restore start fence")
    start = result.get("startConsumption", {})
    predecessor = snapshot.get("predecessor")
    verification = restored.get("verification")
    bootstraps = restored.get("bootstrapBindings")
    if (type(restored.get("schemaVersion")) is not int or restored["schemaVersion"] != 1
            or restored.get("outcome") != "restored" or restored.get("error") is not None
            or restored.get("restoreApplied") is not True
            or not isinstance(result.get("candidate"), dict)
            or restored.get("failedActivationResultPath") != str(path)
            or restored.get("failedActivationResultSha256") != sha256_bytes(payload)
            or restored.get("candidate") != result.get("candidate")
            or restored.get("snapshot") != snapshot or restored.get("rollback") != predecessor
            or not isinstance(predecessor, dict)
            or not isinstance(start, dict)
            or start.get("path") != str(path.with_name(START_CONSUMED_NAME))
            or start.get("sha256") != sha256_bytes(fence_bytes)
            or start.get("consumedAt") != fence.get("consumedAt")
            or not isinstance(start.get("consumedAt"), str)
            or type(fence.get("schemaVersion")) is not int or fence["schemaVersion"] != 1
            or fence.get("terminalResultPath") != str(path)
            or fence.get("snapshotManifestPath") != snapshot.get("manifestPath")
            or fence.get("snapshotManifestSha256") != snapshot["manifestSha256"]
            or fence.get("candidateSealPath") != result["candidate"].get("sealPath")
            or fence.get("candidateSealSha256") != result["candidate"].get("sealSha256")
            or not isinstance(verification, dict) or set(verification) != {"gateway", "node"}
            or not isinstance(bootstraps, dict) or set(bootstraps) != {OPERATOR.require_string("runtime.gateway_label"), OPERATOR.require_string("runtime.node_label")}
            or not all(bootstrap_binding_shape_is_valid(row) for row in bootstraps.values())):
        raise ActivationError("ScreenCapture restore receipt binding drift")
    gateway = verification.get("gateway")
    loaded = gateway.get("loaded") if isinstance(gateway, dict) else None
    node_loaded = verification.get("node")
    selector = _screen_capture_selected_release(path, predecessor, selected_identity)
    for label, owner in ((OPERATOR.require_string("runtime.gateway_label"), loaded), (OPERATOR.require_string("runtime.node_label"), node_loaded)):
        binding = bootstraps[label]
        if (not isinstance(owner, dict) or owner.get("bootstrapBinding") != binding
                or any(owner.get(key) != value or binding.get(key) != value for key, value in (
                    ("release", predecessor["path"]), ("releaseDevice", predecessor["device"]),
                    ("releaseInode", predecessor["inode"])))
                or (binding["selectorDevice"], binding["selectorInode"])
                != (selector.st_dev, selector.st_ino)):
            raise ActivationError("ScreenCapture restored process binding drift")
    health = gateway.get("health", {})
    if not isinstance(health, dict) or any(not isinstance(health.get(key), dict) or health[key].get("accepted") is not True
           or health[key].get("statusCode") != 200 for key in ("healthz", "readyz")):
        raise ActivationError("ScreenCapture restored gateway health is unproven")
    pid = loaded.get("pid")
    if (type(pid) is not int or pid <= 0 or not isinstance(loaded.get("startToken"), str)
            or (responsible_pid is not None and pid != responsible_pid)
            or process_identity(pid) != (loaded["startToken"], node, 1)):
        raise ActivationError("ScreenCapture restored gateway needs a fresh native route probe")
    return {"activationReceiptSha256": sha256_bytes(payload),
            "restoreReceiptSha256": sha256_bytes(restored_bytes)}


def screen_capture_runtime_evidence(
    path: Path, node: Path, *, expected: dict[str, Any] | None = None,
    responsible_pid: int | None = None,
) -> dict[str, str]:
    """Resolve one activation/restore owner for capture, acceptance, enrollment and use."""
    evidence = _screen_capture_activation_result(
        path, expected.get("activationReceiptSha256") if expected is not None else None,
        node, responsible_pid)
    if expected is not None and any(expected.get(key) != evidence.get(key)
                                    for key in ("activationReceiptSha256", "restoreReceiptSha256")):
        raise ActivationError("ScreenCapture lifecycle changed; perform a fresh native route probe")
    return evidence


def _screen_capture_activation_result(path: Path, expected_sha256: str | None,
                                     node: Path, responsible_pid: int | None) -> dict[str, str]:
    """Read current proof across the activator's exact terminal-receipt retirement.

    A present live result always wins, including a new or invalid result. The
    fallback reads existing owner receipts; it never restores activation controls
    or treats an old archive as current after a later lifecycle operation.
    """
    if os.path.lexists(path):
        result, payload, _ = read_json(path, "current ScreenCapture activation")
        if result.get("outcome") == "activated":
            return {"activationReceiptSha256": sha256_bytes(payload)}
    lock = path.parent / "activation.lock"
    descriptor = -1
    try:
        lock_info = os.lstat(lock)
        if (lock.resolve(strict=True) != lock or not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_nlink != 1 or lock_info.st_uid != os.getuid()
                or stat.S_IMODE(lock_info.st_mode) != 0o600):
            raise ActivationError("ScreenCapture activation lock identity drift")
        descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if _screen_capture_file_identity(os.fstat(descriptor)) != _screen_capture_file_identity(lock_info):
            raise ActivationError("ScreenCapture activation lock changed before opening")
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if _screen_capture_file_identity(os.lstat(lock)) != _screen_capture_file_identity(lock_info):
            raise ActivationError("ScreenCapture activation lock changed during opening")
        if os.path.lexists(path):
            result, payload, _ = _read_screen_capture_retirement_json(path, "current ScreenCapture activation")
            snapshot = result.get("snapshot", {})
            if not isinstance(snapshot, dict):
                raise ActivationError("ScreenCapture restore snapshot is invalid")
            restore_path = path.parent / f"activation-restore-result-{str(snapshot.get('manifestSha256', ''))[:16]}.json"
            return _screen_capture_restored_evidence(
                path, result, payload, restore_path, path.with_name(START_CONSUMED_NAME), node, responsible_pid)
        if os.path.lexists(path.with_name(START_CONSUMED_NAME)):
            raise ActivationError("ScreenCapture activation is not terminal")
        archive = path.parent / "archive"
        archive_info = os.lstat(archive)
        if (not stat.S_ISDIR(archive_info.st_mode) or archive.resolve(strict=True) != archive
                or archive_info.st_uid != os.getuid() or stat.S_IMODE(archive_info.st_mode) != 0o700):
            raise ActivationError("ScreenCapture retirement archive identity drift")
        deadline = time.monotonic() + 2.0
        total_bytes = 0
        rows: list[tuple[datetime, Path, dict[str, Any]]] = []
        for index, generation in enumerate(archive.iterdir()):
            if index >= 4096 or time.monotonic() > deadline:
                raise ActivationError("ScreenCapture retirement inventory exceeds bound")
            info = os.lstat(generation)
            if (not stat.S_ISDIR(info.st_mode) or generation.resolve(strict=True) != generation
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise ActivationError("ScreenCapture retirement generation identity drift")
            receipt_path = generation / RETIREMENT_RECEIPT_NAME
            if not os.path.lexists(receipt_path):
                if any(os.path.lexists(generation / name) for name in (path.name, START_CONSUMED_NAME)):
                    raise ActivationError("ScreenCapture retirement is incomplete")
                continue
            size = os.lstat(receipt_path).st_size
            if size > 256 * 1024 or total_bytes + size > 8 * 1024 * 1024:
                raise ActivationError("ScreenCapture retirement inventory exceeds bound")
            receipt, payload, info = _read_screen_capture_retirement_json(receipt_path, "ScreenCapture retirement receipt")
            total_bytes += len(payload)
            if total_bytes > 8 * 1024 * 1024:
                raise ActivationError("ScreenCapture retirement inventory exceeds bound")
            if (stat.S_IMODE(info.st_mode) != 0o400 or info.st_uid != os.getuid()
                    or not isinstance(receipt, dict) or type(receipt.get("schemaVersion")) is not int
                    or receipt["schemaVersion"] != 1
                    or receipt.get("outcome") not in {"activated", "restored", "restored_stopped", "restored_after_late_verification"}
                    or not isinstance(receipt.get("retiredAt"), str)
                    or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", receipt["retiredAt"]) is None):
                raise ActivationError("ScreenCapture retirement receipt is invalid")
            retired_at = datetime.fromisoformat(receipt["retiredAt"].replace("Z", "+00:00"))
            rows.append((retired_at, generation, receipt))
        if not rows:
            raise ActivationError("ScreenCapture activation retirement is unavailable")
        matching = [row for row in rows if isinstance(row[2].get("result"), dict)
                    and row[2]["result"].get("sha256") == expected_sha256]
        if expected_sha256 is not None and len(matching) != 1:
            raise ActivationError("ScreenCapture retired activation is unavailable or ambiguous")
        latest = max(row[0] for row in rows)
        selected = [row for row in rows if row[0] == latest]
        if len(selected) != 1:
            raise ActivationError("ScreenCapture latest retirement is ambiguous")
        _, generation, receipt = selected[0]
        release_identities = retirement_release_identities(receipt)
        result_path = generation / path.name
        fence_path = generation / START_CONSUMED_NAME
        if any(os.lstat(item).st_size > 256 * 1024 for item in (result_path, fence_path)):
            raise ActivationError("ScreenCapture retired activation exceeds bound")
        result, payload, result_info = _read_screen_capture_retirement_json(result_path, "retired ScreenCapture activation")
        fence, fence_payload, fence_info = _read_screen_capture_retirement_json(fence_path, "retired ScreenCapture start fence")
        for key, actual_path, actual_payload, info in (
                ("result", result_path, payload, result_info),
                ("startFence", fence_path, fence_payload, fence_info)):
            binding = receipt.get(key)
            if (not isinstance(binding, dict) or set(binding) not in (
                    {"path", "sha256", "device", "inode"}, {"path", "sha256", "device", "inode", "volumeUuid"})
                    or binding["path"] != str(actual_path) or binding["sha256"] != sha256_bytes(actual_payload)
                    or not historical_identity_matches(binding, actual_path, info)
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o400):
                raise ActivationError("ScreenCapture retired activation binding drift")
        candidate, start = result.get("candidate"), result.get("startConsumption")
        if ((expected_sha256 is not None and sha256_bytes(payload) != expected_sha256)
                or not isinstance(candidate, dict) or not isinstance(start, dict)
                or receipt.get("candidatePath") != candidate.get("path")
                or receipt.get("candidateCommit") != candidate.get("commit")
                or start.get("path") != str(path.with_name(START_CONSUMED_NAME))
                or start.get("sha256") != sha256_bytes(fence_payload)
                or start.get("consumedAt") != fence.get("consumedAt")):
            raise ActivationError("ScreenCapture effective evidence needs a fresh native route probe")
        if receipt["outcome"] == "activated" and result.get("outcome") == "activated":
            if receipt.get("selectedPath") != candidate.get("path") or "restoreResult" in receipt:
                raise ActivationError("ScreenCapture selected activation drift")
            _screen_capture_selected_release(path, candidate, release_identities.get("selected"))
            evidence = {"activationReceiptSha256": sha256_bytes(payload)}
        elif receipt["outcome"] == "restored":
            restore_ref = receipt.get("restoreResult")
            snapshot = result.get("snapshot", {})
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("predecessor"), dict):
                raise ActivationError("ScreenCapture retired restore snapshot is invalid")
            restore_path = generation / f"activation-restore-result-{str(snapshot.get('manifestSha256', ''))[:16]}.json"
            _, restore_bytes, restore_info = _read_screen_capture_retirement_json(restore_path, "retired ScreenCapture restore")
            if (not isinstance(restore_ref, dict) or set(restore_ref) not in (
                    {"path", "sha256", "device", "inode"}, {"path", "sha256", "device", "inode", "volumeUuid"})
                    or restore_ref.get("sha256") != sha256_bytes(restore_bytes)
                    or not historical_identity_matches(restore_ref, restore_path, restore_info)
                    or receipt.get("selectedPath") != snapshot.get("predecessor", {}).get("path")):
                raise ActivationError("ScreenCapture retired restore binding drift")
            evidence = _screen_capture_restored_evidence(
                path, result, payload, restore_path, fence_path, node, responsible_pid,
                release_identities.get("selected"))
        else:
            raise ActivationError("ScreenCapture effective evidence needs a fresh native route probe")
        after = os.lstat(archive)
        if (time.monotonic() > deadline or os.path.lexists(path)
                or os.path.lexists(path.with_name(START_CONSUMED_NAME))
                or _screen_capture_file_identity(os.lstat(lock)) != _screen_capture_file_identity(lock_info)
                or (archive_info.st_dev, archive_info.st_ino, archive_info.st_mtime_ns, archive_info.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)):
            raise ActivationError("ScreenCapture retirement changed during inspection")
        return evidence
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ActivationError("ScreenCapture activation retirement is unavailable or invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def verify_screen_capture_binding(path: Path, node: Path, *,
                                  require_current_process: bool = False) -> dict[str, Any]:
    binding, payload, info = read_json(path, "ScreenCapture continuity binding")
    if stat.S_IMODE(info.st_mode) != 0o400:
        raise ActivationError("ScreenCapture continuity binding must be immutable")
    if binding.get("schemaVersion") != 1 or binding.get("route") != SCREEN_CAPTURE_ROUTE:
        raise ActivationError("ScreenCapture continuity binding is invalid")
    proof = load_screen_capture_acceptance(Path(binding["nativeAcceptancePath"]),
                                          binding["nativeAcceptanceSha256"])
    files = binding.get("files")
    if (not isinstance(files, list) or len(files) != 2
            or not all(isinstance(row, dict) for row in files)
            or files[0].get("path") != str(node)):
        raise ActivationError("ScreenCapture continuity executable is unknown")
    if (binding.get("responsiblePid") != proof["responsiblePid"]
            or binding.get("responsibleProcessIdentity") != proof["responsibleProcessIdentity"]
            or binding.get("activationReceiptSha256") != proof.get("activationReceiptSha256")
            or binding.get("restoreReceiptSha256") != proof.get("restoreReceiptSha256")):
        raise ActivationError("ScreenCapture continuity proof drift")
    for expected, native in zip(files, proof["files"]):
        if native.get("codesignVerified") is not True or any(
                expected.get(key) != native.get(key)
                for key in ("path", "device", "inode", "bytes", "mtimeNs", "sha256")):
            raise ActivationError("ScreenCapture continuity file differs from native proof")
        if screen_capture_code_identity(Path(expected["path"])) != expected:
            raise ActivationError("ScreenCapture executable or signature drift")
    permission = screen_capture_permission(Path(binding["permission"]["database"]), node)
    if permission != binding["permission"]:
        raise ActivationError("ScreenCapture recorded permission drift")
    current_process = False
    try:
        current_process = ([str(item) for item in process_identity(binding["responsiblePid"])]
                           == binding["responsibleProcessIdentity"])
    except (ActivationError, OSError):
        pass
    if require_current_process:
        screen_capture_runtime_evidence(
            Path(binding["activationReceiptPath"]), node, expected=binding,
            responsible_pid=binding["responsiblePid"])
        if (not current_process
                or [str(item) for item in process_identity(binding["responsiblePid"])]
                != binding["responsibleProcessIdentity"]):
            raise ActivationError("ScreenCapture effective evidence needs a fresh native route probe")
    return {"bindingSha256": sha256_bytes(payload), "nativeAcceptanceSha256": binding["nativeAcceptanceSha256"],
            "protectedCodeIdentityUnchanged": True, "recordedPermissionUnchanged": True,
            "effectiveCaptureVerifiedForCurrentProcess": current_process,
            "effectiveCaptureVerifiedAt": binding["verifiedAt"], "scheduledSyncProven": False}


def verify_screen_capture_capability(reference: Any) -> dict[str, Any]:
    """Current behavioral evidence is distinct from activation preconditions."""
    if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
            or not isinstance(reference["path"], str) or not Path(reference["path"]).is_absolute()
            or not isinstance(reference["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]) is None):
        raise ActivationError("ScreenCapture capability reference is invalid")
    path = Path(reference["path"])
    payload, _ = read_physical(path, "ScreenCapture capability binding")
    if sha256_bytes(payload) != reference["sha256"]:
        raise ActivationError("ScreenCapture capability binding changed")
    return verify_screen_capture_binding(path, OPERATOR.require_path("paths.node_binary"), require_current_process=True)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_immutable_file(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ActivationError(f"immutable output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        os.link(temporary, path)
        os.unlink(temporary)
        fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def atomic_symlink(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
        fsync_directory(link.parent)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def normalized_tree_inventory(root: Path, *, release_root: Path | None = None) -> dict[str, Any]:
    try:
        root_info = os.lstat(root)
    except OSError as exc:
        raise ActivationError("release tree is unavailable") from exc
    if not stat.S_ISDIR(root_info.st_mode) or root.resolve(strict=True) != root:
        raise ActivationError("release tree must be one physical directory")
    boundary = release_root if release_root is not None else root
    if (boundary.resolve(strict=True) != boundary or not boundary.is_dir()
            or (root != boundary and boundary not in root.parents)):
        raise ActivationError("inventory root is outside its physical release")
    rows: list[dict[str, Any]] = []
    for directory, directories, files in os.walk(root):
        directory_path = Path(directory)
        directories.sort()
        files.sort()
        for name in [*directories, *files]:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                target = (path.parent / os.readlink(path)).resolve(strict=True)
                if target != boundary and boundary not in target.parents:
                    raise ActivationError("release symlink escapes the release tree")
                rows.append({"path": relative, "type": "symlink", "target": os.readlink(path)})
            elif stat.S_ISDIR(info.st_mode):
                rows.append({"path": relative, "type": "directory", "mode": stat.S_IMODE(info.st_mode)})
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                digest, stable = sha256_physical_file(path, f"release file {relative}")
                rows.append({"path": relative, "type": "file", "mode": stat.S_IMODE(stable.st_mode), "bytes": stable.st_size, "sha256": digest})
            else:
                raise ActivationError("release tree contains an unsupported entry")
    return {"count": len(rows), "digest": sha256_bytes(canonical_json_bytes(rows))}


def validate_ai_runtime_package(release: Path) -> None:
    package_link = release / "node_modules" / "@openclaw" / "ai"
    try:
        link_info = os.lstat(package_link)
        package_root = package_link.resolve(strict=True)
        package_root.relative_to(release)
    except (OSError, ValueError) as exc:
        raise ActivationError("candidate AI runtime link drift") from exc
    if not stat.S_ISLNK(link_info.st_mode) or package_root == release:
        raise ActivationError("candidate AI runtime link drift")

    package, _, package_info = read_json(
        package_root / "package.json", "candidate AI runtime package"
    )
    exports = package.get("exports")
    if (
        package.get("name") != "@openclaw/ai"
        or stat.S_IMODE(package_info.st_mode) & 0o222
        or not isinstance(exports, dict)
        or not exports
    ):
        raise ActivationError("candidate AI runtime package drift")

    targets: set[str] = set()

    def collect_runtime_targets(value: Any) -> None:
        if isinstance(value, str):
            if not value.startswith("./dist/"):
                raise ActivationError("candidate AI runtime export drift")
            relative = value.removeprefix("./")
            if posixpath.normpath(relative) != relative:
                raise ActivationError("candidate AI runtime export drift")
            targets.add(relative)
            return
        if not isinstance(value, dict) or not value:
            raise ActivationError("candidate AI runtime export drift")
        runtime_condition_seen = False
        for condition, nested in value.items():
            if condition == "types":
                continue
            runtime_condition_seen = True
            collect_runtime_targets(nested)
        if not runtime_condition_seen:
            raise ActivationError("candidate AI runtime export drift")

    for export_value in exports.values():
        collect_runtime_targets(export_value)

    for relative in sorted(targets):
        payload, info = read_physical(
            package_root.joinpath(*relative.split("/")),
            f"candidate AI runtime export {relative}",
        )
        if not payload or stat.S_IMODE(info.st_mode) & 0o222:
            raise ActivationError("candidate AI runtime export drift")


def validate_release(
    paths: ActivationPaths,
    release: Path,
    commit: str,
    *,
    require_candidate_native_entrypoints: bool = True,
) -> dict[str, Any]:
    if not release.is_absolute() or release.parent != paths.releases_root:
        raise ActivationError("candidate release must be a direct releases-root child")
    try:
        release_info = os.lstat(release)
    except OSError as exc:
        raise ActivationError("candidate release is unavailable") from exc
    if (not stat.S_ISDIR(release_info.st_mode) or release.resolve(strict=True) != release
            or stat.S_IMODE(release_info.st_mode) & 0o222):
        raise ActivationError("candidate release identity drift")
    build, _, _ = read_json(release / "dist" / "build-info.json", "candidate build info")
    package, _, _ = read_json(release / "package.json", "candidate package")
    required_entrypoints = [
        release / "dist" / "index.js",
        release / "openclaw.mjs",
    ]
    if require_candidate_native_entrypoints:
        required_entrypoints.extend(
            (
                release / "dist" / "infra" / "exec-approvals-inspection.js",
                release
                / "dist"
                / "extensions"
                / "browser"
                / "chrome-extension"
                / "manifest.json",
                release
                / "dist"
                / "extensions"
                / "browser"
                / "chrome-extension"
                / "background.js",
            )
        )
    for required in required_entrypoints:
        payload, info = read_physical(required, "candidate entrypoint")
        if not payload or stat.S_IMODE(info.st_mode) & 0o222:
            raise ActivationError("candidate entrypoint drift")
    if build.get("commit") != commit or package.get("name") != "openclaw":
        raise ActivationError("candidate source identity drift")
    version = package.get("version")
    if not isinstance(version, str) or not version:
        raise ActivationError("candidate package version drift")
    validate_ai_runtime_package(release)
    return {"path": str(release), "commit": commit, "device": release_info.st_dev,
            "inode": release_info.st_ino, "packageVersion": version,
            "releaseInventory": normalized_tree_inventory(release)}


def validate_bundled_extensions(candidate: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for plugin_id in required_bundled_extensions():
        root = candidate / "dist" / "extensions" / plugin_id
        manifest, manifest_payload, manifest_info = read_json(root / "openclaw.plugin.json", f"candidate {plugin_id} manifest")
        package, package_payload, package_info = read_json(root / "package.json", f"candidate {plugin_id} package")
        index_payload, index_info = read_physical(root / "index.js", f"candidate {plugin_id} entrypoint")
        if (manifest.get("id") != plugin_id or package.get("name") != f"@openclaw/{plugin_id}"
                or not index_payload or any(stat.S_IMODE(item.st_mode) & 0o222 for item in (manifest_info, package_info, index_info))):
            raise ActivationError(f"candidate bundled {plugin_id} artifact drift")
        result[plugin_id] = {"path": str(root), "manifestSha256": sha256_bytes(manifest_payload),
                             "packageSha256": sha256_bytes(package_payload),
                             "entrypointSha256": sha256_bytes(index_payload),
                             # Bundled plugins share this release's pnpm/workspace dependencies.
                             "inventory": normalized_tree_inventory(root, release_root=candidate)}
    return result


def create_candidate_seal(paths: ActivationPaths, candidate: Path, commit: str, output: Path) -> dict[str, Any]:
    commit = require_commit(commit, "candidate commit")
    if not output.is_absolute():
        raise ActivationError("candidate seal output must be absolute")
    candidate_record = validate_release(paths, candidate, commit)
    extensions = validate_bundled_extensions(candidate)
    value = {"schemaVersion": 1, "candidate": candidate_record,
             "bundledExtensions": extensions, "createdAt": utc_now()}
    create_immutable_file(output, json.dumps(value, indent=2, sort_keys=True).encode() + b"\n")
    payload, info = read_physical(output, "candidate seal")
    return {**candidate_record, "sealPath": str(output), "sealSha256": sha256_bytes(payload),
            "sealDevice": info.st_dev, "sealInode": info.st_ino,
            "bundledExtensions": extensions}


def validate_candidate_seal(paths: ActivationPaths, candidate: Path, commit: str, seal_path: Path) -> dict[str, Any]:
    if not seal_path.is_absolute():
        raise ActivationError("candidate seal must be absolute")
    value, payload, info = read_json(seal_path, "candidate seal")
    if stat.S_IMODE(info.st_mode) & 0o222:
        raise ActivationError("candidate seal is writable")
    observed = validate_release(paths, candidate, commit)
    extensions = validate_bundled_extensions(candidate)
    if (value.get("schemaVersion") != 1 or value.get("candidate") != observed
            or value.get("bundledExtensions") != extensions or not isinstance(value.get("createdAt"), str)):
        raise ActivationError("candidate seal surface drift")
    return {**observed, "sealPath": str(seal_path), "sealSha256": sha256_bytes(payload),
            "sealDevice": info.st_dev, "sealInode": info.st_ino,
            "bundledExtensions": extensions}


def validate_exec_approvals_inspection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != EXEC_APPROVALS_INSPECTION_KEYS:
        raise ActivationError("exec approvals inspection contract drift")
    if (
        type(value.get("schemaVersion")) is not int
        or value.get("schemaVersion") != 2
        or type(value.get("rowCount")) is not int
        or value.get("rowCount") != 1
    ):
        raise ActivationError("exec approvals inspection contract drift")
    if any(value.get(field) is not True for field in EXEC_APPROVALS_REQUIRED_TRUE):
        raise ActivationError("exec approvals inspection contract drift")
    for field in ("agentCount", "allowlistCount"):
        count = value.get(field)
        if type(count) is not int or count < 0:
            raise ActivationError("exec approvals inspection contract drift")
    for field in ("semanticSha256", "rawCasSha256", "openAIProfileOrderSha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(value.get(field) or "")) is None:
            raise ActivationError("exec approvals inspection contract drift")
    if (
        type(value.get("openAIProfileOrderCount")) is not int
        or value.get("openAIProfileOrderCount") != expected_auth_order_count()
        or value.get("openAIProfileOrderSha256")
        != expected_auth_order_sha256()
    ):
        raise ActivationError("exec approvals inspection contract drift")
    return dict(value)


def exec_approvals_inspector_binding(
    candidate: dict[str, Any], predecessor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    binding = {
        "candidatePath": candidate["path"],
        "candidateCommit": candidate["commit"],
        "candidateDevice": candidate["device"],
        "candidateInode": candidate["inode"],
        "candidateSealPath": candidate["sealPath"],
        "candidateSealSha256": candidate["sealSha256"],
    }
    if predecessor is not None and candidate["packageVersion"] != predecessor.get("packageVersion"):
        # The target reader may correctly reject the predecessor's old schema.
        # Bind its native reader to the already inventory-bound predecessor,
        # without relaxing either runtime's database guards.
        if any(key not in predecessor for key in ("path", "commit", "device", "inode", "releaseInventory", "packageVersion")):
            raise ActivationError("snapshot predecessor reader binding drift")
        binding["predecessorReader"] = {
            key: predecessor[key]
            for key in ("path", "commit", "device", "inode", "releaseInventory")
        }
    return binding


def stopped_inspection_release(candidate: dict[str, Any], predecessor: dict[str, Any]) -> Path:
    return Path(
        predecessor["path"]
        if candidate["packageVersion"] != predecessor["packageVersion"]
        else candidate["path"]
    )


def validate_snapshot_archive_members(
    paths: ActivationPaths,
    archive_path: Path,
    expected_current_target: str,
) -> None:
    """Reject any archive member outside the four stopped-snapshot surfaces."""
    state_root = str(paths.predecessor_state_dir).lstrip("/")
    gateway_plist = str(paths.gateway_plist).lstrip("/")
    node_plist = str(paths.node_plist).lstrip("/")
    current_link = str(paths.current_link).lstrip("/")
    exact_roots = {state_root, gateway_plist, node_plist, current_link}
    # macOS bsdtar encodes xattrs for an exact top-level surface as its sibling
    # AppleDouble member. These four derived names are metadata for the sealed
    # surfaces, not authority for any additional filesystem path.
    appledouble_roots = {
        posixpath.join(posixpath.dirname(item), f"._{posixpath.basename(item)}")
        for item in exact_roots
    }
    if (
        len(exact_roots) != 4
        or any(not item for item in exact_roots)
        or not Path(expected_current_target).is_absolute()
    ):
        raise ActivationError("stopped snapshot archive inventory drift")

    seen: set[str] = set()
    symlink_members: set[str] = set()
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            for member in archive:
                name = member.name
                parts = name.split("/")
                if (
                    not name
                    or name.startswith("/")
                    or any(part in ("", ".", "..") for part in parts)
                    or name in seen
                    or not (
                        name in exact_roots
                        or name in appledouble_roots
                        or name.startswith(f"{state_root}/")
                    )
                    or not (member.isdir() or member.isfile() or member.issym())
                ):
                    raise ActivationError(
                        "stopped snapshot archive inventory drift"
                    )
                if name == state_root and not member.isdir():
                    raise ActivationError(
                        "stopped snapshot archive inventory drift"
                    )
                if name in (gateway_plist, node_plist) and not member.isfile():
                    raise ActivationError(
                        "stopped snapshot archive inventory drift"
                    )
                if name in appledouble_roots and not member.isfile():
                    raise ActivationError(
                        "stopped snapshot archive inventory drift"
                    )
                if name == current_link and (
                    not member.issym()
                    or member.linkname != expected_current_target
                ):
                    raise ActivationError(
                        "stopped snapshot archive inventory drift"
                    )
                if member.issym() and name.startswith(f"{state_root}/"):
                    target = member.linkname
                    normalized_target = posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), target)
                    )
                    if (
                        not target
                        or (
                            not posixpath.isabs(target)
                            and not (
                                normalized_target == state_root
                                or normalized_target.startswith(f"{state_root}/")
                            )
                        )
                    ):
                        raise ActivationError(
                            "stopped snapshot archive inventory drift"
                        )
                seen.add(name)
                if member.issym():
                    symlink_members.add(name)
    except (OSError, tarfile.TarError, UnicodeError) as exc:
        raise ActivationError("stopped snapshot archive inventory drift") from exc

    if not exact_roots.issubset(seen):
        raise ActivationError("stopped snapshot archive inventory drift")
    if any(
        candidate.startswith(f"{symlink}/")
        for symlink in symlink_members
        for candidate in seen
    ):
        raise ActivationError("stopped snapshot archive inventory drift")


def recovered_invariants_content_view(value: dict[str, Any]) -> dict[str, Any]:
    projected = json.loads(json.dumps(value))
    state_root = projected.get("stateRoot")
    if isinstance(state_root, dict):
        state_root.pop("device", None)
        state_root.pop("inode", None)
    return projected


def validate_runtime_config(config: dict[str, Any]) -> None:
    auth = config.get("auth")
    order = auth.get("order") if isinstance(auth, dict) else None
    profiles = auth.get("profiles") if isinstance(auth, dict) else None
    openai_profile_shadows = (
        [key for key in profiles if key.startswith("openai:")]
        if isinstance(profiles, dict)
        else []
    )
    skill_entries = config.get("skills", {}).get("entries") if isinstance(config.get("skills"), dict) else None
    goplaces = skill_entries.get("goplaces") if isinstance(skill_entries, dict) else None
    plugins = config.get("plugins")
    plugin_entries = plugins.get("entries") if isinstance(plugins, dict) else None
    plugin_load = plugins.get("load") if isinstance(plugins, dict) else None
    if (not isinstance(auth, dict) or "cooldowns" in auth
            or (order is not None and not isinstance(order, dict))
            or (isinstance(order, dict) and "openai" in order)
            or (profiles is not None and not isinstance(profiles, dict))
            or openai_profile_shadows
            or not isinstance(goplaces, dict) or goplaces.get("enabled") is not False or "apiKey" in goplaces
            or not isinstance(plugin_entries, dict)
            or any(not isinstance(plugin_entries.get(item), dict) or plugin_entries[item].get("enabled") is not True for item in required_bundled_extensions())
            or not isinstance(plugin_load, dict) or plugin_load.get("paths") != []):
        raise ActivationError("recovered runtime policy drift")


def validate_recovered_invariants(paths: ActivationPaths) -> dict[str, Any]:
    if paths.predecessor_state_dir != paths.candidate_state_dir:
        raise ActivationError("runtime state root binding drift")
    state_info = os.lstat(paths.candidate_state_dir)
    if not stat.S_ISDIR(state_info.st_mode):
        raise ActivationError("runtime state root identity drift")
    reservation_binding = validate_session_reservations(paths.candidate_state_dir)
    config_path = paths.candidate_state_dir / "openclaw.json"
    config, config_payload, config_info = read_json(config_path, "runtime config")
    if stat.S_IMODE(config_info.st_mode) != 0o600:
        raise ActivationError("runtime config mode drift")
    validate_runtime_config(config)
    return {
        "stateRoot": {"path": str(paths.candidate_state_dir), "device": state_info.st_dev, "inode": state_info.st_ino},
        "sessionReservations": reservation_binding,
        "config": {"path": str(config_path), "sha256": sha256_bytes(config_payload),
                   "legacyOpenAIOrderAbsent": True,
                   "legacyOpenAIProfilesAbsent": True,
                   "bundledPluginIds": list(required_bundled_extensions()), "externalLoadPaths": []},
    }


def validate_session_reservations(state_root: Path) -> dict[str, Any]:
    """Preserve an explicitly declared historical record or its declared absence.

    Fresh installations have no historical quarantine-reservation producer.
    Absence is an explicit adopter decision, never inferred from a missing file.
    Both branches enter the existing snapshot/preflight/postflight/restore binding.
    """
    mode = OPERATOR.require_string("runtime.session_reservations_mode")
    if mode not in ("required", "absent"):
        raise ActivationError("session reservation mode must be required or absent")
    reservations_path = OPERATOR.require_path("paths.session_reservations")
    expected_store = OPERATOR.require_path("paths.session_store")
    for path in (reservations_path, expected_store):
        if (not path.is_relative_to(state_root) or path == state_root
                or not path.resolve(strict=False).is_relative_to(state_root.resolve(strict=True))):
            raise ActivationError("session reservation paths must remain within the state root")
    binding: dict[str, Any] = {
        "mode": mode, "path": str(reservations_path), "storePath": str(expected_store),
    }
    if mode == "absent":
        try:
            os.lstat(reservations_path)
        except FileNotFoundError:
            return {**binding, "exists": False}
        raise ActivationError("session reservation declared absent but an entry exists")
    expected_sha256 = OPERATOR.require_string("runtime.session_reservations_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ActivationError("session reservation digest must be lowercase SHA-256")
    reservations, payload, info = read_json(reservations_path, "session quarantine reservations")
    if (reservations.get("storePath") != str(expected_store)
            or stat.S_IMODE(info.st_mode) != 0o600
            or sha256_bytes(payload) != expected_sha256):
        raise ActivationError("session quarantine reservation store drift")
    return {**binding, "exists": True, "sha256": sha256_bytes(payload)}


def reviewed_upgrade_config(path: Path) -> tuple[dict[str, Any], bytes]:
    if not path.is_absolute():
        raise ActivationError("reviewed upgrade config must be absolute")
    value, payload, info = read_json(path, "reviewed upgrade config")
    if stat.S_IMODE(info.st_mode) != 0o400:
        raise ActivationError("reviewed upgrade config must be immutable")
    validate_runtime_config(value)
    return {"path": str(path), "sha256": sha256_bytes(payload)}, payload


def install_upgrade_config(paths: ActivationPaths, binding: dict[str, Any]) -> None:
    observed, payload = reviewed_upgrade_config(Path(binding["path"]))
    if observed != binding:
        raise ActivationError("reviewed upgrade config drift")
    target = paths.candidate_state_dir / "openclaw.json"
    temporary = target.with_name(f".{target.name}.upgrade-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        fsync_directory(target.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def activation_invariants(snapshot: dict[str, Any], upgrade: dict[str, Any] | None) -> dict[str, Any]:
    expected = snapshot["recoveredInvariants"]
    if upgrade is None:
        return expected
    return {**expected, "config": {**expected["config"], "sha256": upgrade["sha256"]}}


def resolve_upgrade_config(candidate: dict[str, Any], snapshot: dict[str, Any],
                           config_path: Path | None) -> dict[str, Any] | None:
    cross_version = candidate["packageVersion"] != snapshot["predecessor"]["packageVersion"]
    if cross_version != (config_path is not None):
        raise ActivationError("version upgrade requires its reviewed target config only")
    return reviewed_upgrade_config(config_path)[0] if config_path is not None else None


def native_migration_schema_versions(release: Path) -> dict[str, int]:
    entrypoint = release / "dist" / "infra" / "runtime-state-migration.js"
    payload, info = read_physical(entrypoint, "native state migration entrypoint")
    package, _, _ = read_json(release / "package.json", "migration package")
    metadata = package.get("openclaw")
    versions = metadata.get("schemaVersions") if isinstance(metadata, dict) else None
    if (not payload or stat.S_IMODE(info.st_mode) & 0o222
            or not isinstance(versions, dict)
            or any(type(versions.get(key)) is not int or versions[key] < 1
                   for key in ("state", "agent"))):
        raise ActivationError("native state migration contract drift")
    return {key: versions[key] for key in ("state", "agent")}


def validate_support_links(paths: ActivationPaths) -> None:
    expected = {paths.package_link: str(paths.current_link),
                paths.bin_link: str(paths.current_link / "openclaw.mjs"),
                paths.node_alias: str(paths.node.parent.parent)}
    for path, target in expected.items():
        if not path.is_symlink() or os.readlink(path) != target:
            raise ActivationError(f"support link drift: {path}")
    _digest, info = sha256_physical_file(paths.node, "pinned node")
    if (info.st_size <= 0 or not os.access(paths.node, os.X_OK)
            or not stat.S_IMODE(info.st_mode) & 0o111):
        raise ActivationError("pinned node identity drift")


def expected_gateway_arguments(paths: ActivationPaths) -> list[str]:
    return [
        str(paths.node),
        str(paths.package_link / "dist" / "index.js"),
        "gateway",
        "--port",
        str(runtime_gateway_port()),
    ]


def expected_gateway_working_directory(paths: ActivationPaths) -> Path:
    """Resolve the configured operator account home independently of selector placement."""
    return OPERATOR.require_path("paths.host_home")


def validate_plists(paths: ActivationPaths) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, path in (("gateway", paths.gateway_plist), ("node", paths.node_plist)):
        payload, info = read_physical(path, f"{label} plist")
        try:
            value = plistlib.loads(payload)
        except Exception as exc:
            raise ActivationError(f"{label} plist is invalid") from exc
        environment = value.get("EnvironmentVariables") if isinstance(value, dict) else None
        arguments = value.get("ProgramArguments") if isinstance(value, dict) else None
        working_directory = value.get("WorkingDirectory") if isinstance(value, dict) else None
        if (not isinstance(environment, dict) or environment.get("OPENCLAW_STATE_DIR") != str(paths.candidate_state_dir)
                or environment.get("OPENCLAW_SUPERVISOR_MODE") != "external"
                or environment.get("OPENCLAW_SERVICE_REPAIR_POLICY") != "external"
                or not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments)):
            raise ActivationError(f"{label} plist contract drift")
        if label == "gateway":
            if (
                "Program" in value
                or arguments != expected_gateway_arguments(paths)
                or working_directory != str(expected_gateway_working_directory(paths))
                or value.get("RunAtLoad") is not True
                or value.get("KeepAlive") is not True
            ):
                raise ActivationError("gateway plist selector contract drift")
        elif arguments[:2] != [str(paths.node), str(paths.package_link / "dist" / "index.js")]:
            raise ActivationError("node plist selector contract drift")
        result[label] = {"path": str(path), "sha256": sha256_bytes(payload), "mode": stat.S_IMODE(info.st_mode)}
    return result


def expected_stopped_command_evidence(paths: ActivationPaths) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for label in (OPERATOR.require_string("runtime.gateway_label"), OPERATOR.require_string("runtime.node_label")):
        stderr = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for user gui: '
            f"{paths.operator_uid}\n"
        ).encode()
        evidence.append(CommandResult(
            ("/bin/launchctl", "print", f"gui/{paths.operator_uid}/{label}"),
            113, b"", stderr, 0, False,
        ).evidence(f"{label}_launchctl_print"))
    evidence.append(CommandResult(
        ("/usr/sbin/lsof", "-nP", f"-iTCP:{runtime_gateway_port()}", "-sTCP:LISTEN", "-Fpn"),
        1, b"", b"", 0, False,
    ).evidence("gateway_listener_absence"))
    return evidence


def validate_stopped_command_evidence(paths: ActivationPaths, value: Any) -> None:
    if not isinstance(value, list):
        raise ActivationError("stopped snapshot command evidence drift")
    expected = expected_stopped_command_evidence(paths)
    if len(value) != len(expected):
        raise ActivationError("stopped snapshot command evidence drift")
    for observed, required in zip(value, expected):
        if not isinstance(observed, dict):
            raise ActivationError("stopped snapshot command evidence drift")
        duration = observed.get("durationMs")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
            raise ActivationError("stopped snapshot command evidence drift")
        if {key: item for key, item in observed.items() if key != "durationMs"} != {
            key: item for key, item in required.items() if key != "durationMs"
        }:
            raise ActivationError("stopped snapshot command evidence drift")


def load_stopped_snapshot(
    paths: ActivationPaths,
    manifest_path: Path,
    candidate_record: dict[str, Any],
    *,
    verify_predecessor_selector: bool = True,
) -> dict[str, Any]:
    if not manifest_path.is_absolute():
        raise ActivationError("stopped snapshot manifest must be absolute")
    value, manifest_payload, manifest_info = read_json(
        manifest_path, "stopped snapshot manifest"
    )
    archive_path = Path(str(value.get("tarPath", "")))
    g6_path = Path(str(value.get("g6QuiescencePath", "")))
    predecessor = value.get("predecessor")
    expected_manifest_keys = {
        "schemaVersion",
        "tarPath",
        "tarSha256",
        "inventoryCount",
        "g6QuiescencePath",
        "g6QuiescenceSha256",
        "predecessorStateDir",
        "candidateStateDir",
        "currentTarget",
        "predecessor",
        "recoveredInvariants",
        "execApprovalsInspector",
        "execApprovals",
        "gatewayPlistSha256",
        "nodePlistSha256",
    }
    if (stat.S_IMODE(manifest_info.st_mode) != 0o400
            or set(value) != expected_manifest_keys
            or value.get("schemaVersion") != 2
            or value.get("inventoryCount") != 4
            or not archive_path.is_absolute() or not g6_path.is_absolute()
            or not isinstance(predecessor, dict)
            or value.get("predecessorStateDir") != str(paths.predecessor_state_dir)
            or value.get("candidateStateDir") != str(paths.candidate_state_dir)
            or value.get("currentTarget") != predecessor.get("path")
            or value.get("execApprovalsInspector")
            != exec_approvals_inspector_binding(candidate_record, predecessor)
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("tarSha256") or "")) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("g6QuiescenceSha256") or "")) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("gatewayPlistSha256") or "")) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("nodePlistSha256") or "")) is None):
        raise ActivationError("stopped snapshot binding drift")
    approvals = validate_exec_approvals_inspection(value.get("execApprovals"))
    archive_sha, archive_info = sha256_physical_file(
        archive_path, "stopped snapshot archive"
    )
    if stat.S_IMODE(archive_info.st_mode) & 0o222:
        raise ActivationError("stopped snapshot archive is writable")
    g6, g6_payload, g6_info = read_json(g6_path, "stopped snapshot quiescence")
    if (stat.S_IMODE(g6_info.st_mode) != 0o400
            or g6.get("schemaVersion") != 1
            or g6.get("archivePath") != str(archive_path)
            or g6.get("archiveSha256") != archive_sha
            or not isinstance(g6.get("capturedAt"), str)):
        raise ActivationError("stopped snapshot observed quiescence drift")
    validate_stopped_command_evidence(paths, g6.get("preCapture"))
    validate_stopped_command_evidence(paths, g6.get("postCapture"))
    if (value.get("tarSha256") != archive_sha
            or value.get("g6QuiescenceSha256") != sha256_bytes(g6_payload)
            or (verify_predecessor_selector and (
                not paths.current_link.is_symlink()
                or os.readlink(paths.current_link) != predecessor.get("path")
            ))):
        raise ActivationError("stopped snapshot preimage drift")
    validate_snapshot_archive_members(
        paths,
        archive_path,
        str(predecessor.get("path", "")),
    )
    predecessor_path = Path(str(predecessor.get("path", "")))
    predecessor_commit = require_commit(str(predecessor.get("commit", "")), "snapshot predecessor commit")
    observed_predecessor = validate_release(
        paths,
        predecessor_path,
        predecessor_commit,
        require_candidate_native_entrypoints=False,
    )
    expected_invariants = value.get("recoveredInvariants")
    if not isinstance(expected_invariants, dict):
        raise ActivationError("stopped snapshot predecessor or invariant drift")
    if predecessor != observed_predecessor:
        raise ActivationError("stopped snapshot predecessor or invariant drift")
    rollback = {
        **observed_predecessor,
        "source": "cold_recovery_binding",
        "automaticRestartAuthorized": False,
        "snapshotPath": str(archive_path),
        "snapshotSha256": archive_sha,
    }
    return {"schemaVersion": 2,
            "manifestPath": str(manifest_path), "manifestSha256": sha256_bytes(manifest_payload),
            "tarPath": str(archive_path), "tarSha256": archive_sha, "tarBytes": archive_info.st_size,
            "tarDevice": archive_info.st_dev, "tarInode": archive_info.st_ino,
            "inventory": {"count": 4, "digest": archive_sha},
            "g6QuiescencePath": str(g6_path), "g6QuiescenceSha256": sha256_bytes(g6_payload),
            "predecessorStateDir": str(paths.predecessor_state_dir), "candidateStateDir": str(paths.candidate_state_dir),
            "execApprovalsInspector": value["execApprovalsInspector"],
            "execApprovals": approvals,
            "recoveredInvariants": expected_invariants, "predecessor": rollback,
            "gatewayPlistSha256": value["gatewayPlistSha256"], "nodePlistSha256": value["nodePlistSha256"]}


def run_bounded(argv: tuple[str, ...], timeout_seconds: float, *, cwd: Path | None = None,
                env: dict[str, str] | None = None) -> CommandResult:
    started = time.monotonic()
    timed_out = False
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True,
                               cwd=cwd, env=env)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, 15)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
    if len(stdout) > MAX_COMMAND_BYTES or len(stderr) > MAX_COMMAND_BYTES:
        raise ActivationError("lifecycle command output exceeded its bound")
    return CommandResult(argv, process.returncode, stdout, stderr,
                         max(0, int((time.monotonic() - started) * 1000)), timed_out)


def parse_launchctl(
    payload: bytes,
    uid: int,
    label: str,
) -> tuple[int, int, tuple[str, ...], Path, Path | None]:
    prefix = f"gui/{uid}/{label} = {{\n".encode()
    if not payload.startswith(prefix):
        raise ActivationError("launchd service header drift")

    def unique(pattern: bytes, field: str) -> bytes:
        matches = re.findall(pattern, payload)
        if len(matches) != 1:
            raise ActivationError(f"launchd {field} field count drift")
        return matches[0]

    blocks = re.findall(rb"\n\targuments = \{\n((?:\t\t[^\n]+\n)+)\t\}\n", payload)
    if len(blocks) != 1:
        raise ActivationError("launchd arguments field count drift")
    try:
        arguments = tuple(line[2:].decode() for line in blocks[0].splitlines())
        service_path = Path(unique(rb"\n\tpath = ([^\n]+)\n", "path").decode())
        pid = int(unique(rb"\n\tpid = ([0-9]+)\n", "pid"))
        runs = int(unique(rb"\n\truns = ([0-9]+)\n", "runs"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ActivationError("launchd service fields are invalid") from exc
    if pid <= 0 or runs <= 0 or not arguments or not service_path.is_absolute():
        raise ActivationError("launchd service identity is incomplete")
    working_matches = re.findall(
        rb"\n\tworking directory = ([^\n]+)\n",
        payload,
    )
    if label == OPERATOR.require_string("runtime.gateway_label"):
        if len(working_matches) != 1:
            raise ActivationError("launchd working directory field count drift")
        try:
            working_directory: Path | None = Path(working_matches[0].decode())
        except UnicodeDecodeError as exc:
            raise ActivationError("launchd working directory is invalid") from exc
        if not working_directory.is_absolute():
            raise ActivationError("launchd working directory is invalid")
    else:
        working_directory = None
    return pid, runs, arguments, service_path, working_directory


def parse_lsof_cwd(payload: bytes, pid: int) -> tuple[Path, int, int]:
    fields: dict[bytes, list[bytes]] = {key: [] for key in (b"p", b"f", b"D", b"i", b"n")}
    for raw in payload.split(b"\0"):
        if not raw or raw == b"\n":
            continue
        if raw.startswith(b"\n"):
            raw = raw[1:]
        if not raw or raw[:1] not in fields:
            raise ActivationError("loaded process cwd identity has an unexpected field")
        fields[raw[:1]].append(raw[1:])

    def unique(field: bytes) -> bytes:
        values = fields[field]
        if len(values) != 1:
            raise ActivationError("loaded process cwd identity field count drift")
        return values[0]

    try:
        observed_pid = int(unique(b"p"))
        descriptor = unique(b"f")
        device = int(unique(b"D"), 16)
        inode = int(unique(b"i"))
        path = Path(os.fsdecode(unique(b"n")))
    except ValueError as exc:
        raise ActivationError("loaded process cwd identity drift") from exc
    if (observed_pid != pid or descriptor != b"cwd" or device <= 0
            or inode <= 0 or not path.is_absolute()):
        raise ActivationError("loaded process cwd identity drift")
    return path, device, inode


def parse_lsof_listener_pid(payload: bytes) -> int:
    pids: list[int] = []
    listeners = 0
    for field in payload.splitlines():
        if field.startswith(b"p"):
            try:
                pids.append(int(field[1:]))
            except ValueError as exc:
                raise ActivationError("gateway listener PID is invalid") from exc
        elif field.startswith(b"f"):
            if len(field) == 1:
                raise ActivationError("gateway listener descriptor is invalid")
        elif field.startswith(b"n"):
            if field.rsplit(b":", 1)[-1] != str(runtime_gateway_port()).encode():
                raise ActivationError("gateway listener endpoint is invalid")
            listeners += 1
        elif field:
            raise ActivationError("gateway listener identity has an unexpected field")
    if len(pids) != 1 or pids[0] <= 0 or listeners == 0:
        raise ActivationError("gateway listener identity is incomplete")
    return pids[0]


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def process_identity(pid: int) -> tuple[str, Path, int]:
    if sys.platform != "darwin" or pid <= 0:
        raise ActivationError("Darwin process identity is unavailable")
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    information = _ProcBSDInfo()
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                     ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    observed = library.proc_pidinfo(pid, 3, 0, ctypes.byref(information), ctypes.sizeof(information))
    buffer = ctypes.create_string_buffer(4096)
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    path_size = library.proc_pidpath(pid, buffer, len(buffer))
    if (observed != ctypes.sizeof(information) or information.pbi_pid != pid
            or information.pbi_ppid <= 0 or information.pbi_start_tvsec <= 0 or path_size <= 0):
        raise ActivationError("process identity is unavailable")
    try:
        executable = Path(buffer.value.decode()).resolve(strict=True)
    except (UnicodeDecodeError, OSError) as exc:
        raise ActivationError("process executable identity is invalid") from exc
    return (f"darwin:{information.pbi_start_tvsec}:{information.pbi_start_tvusec}",
            executable, information.pbi_ppid)


def process_start_time_us(start_token: str) -> int:
    match = re.fullmatch(r"darwin:([1-9][0-9]*):([0-9]{1,6})", start_token)
    if match is None:
        raise ActivationError("process start identity is invalid")
    seconds = int(match.group(1))
    microseconds = int(match.group(2))
    if microseconds >= 1_000_000:
        raise ActivationError("process start identity is invalid")
    return seconds * 1_000_000 + microseconds


class SystemBackend:
    def __init__(
        self,
        paths: ActivationPaths,
        legacy_exec_approvals_path: Path | None = None,
    ):
        self.paths = paths
        if legacy_exec_approvals_path is None:
            legacy_exec_approvals_path = OPERATOR.require_path("paths.legacy_exec_approvals")
        if not legacy_exec_approvals_path.is_absolute():
            raise ActivationError("legacy exec approvals path must be absolute")
        self._legacy_exec_approvals_path = legacy_exec_approvals_path
        self.command_evidence: list[dict[str, Any]] = []
        self._bootstrap_bindings: dict[str, dict[str, Any]] = {}
        self._receipt_generations: dict[str, tuple[int, str]] = {}

    def _command(self, purpose: str, argv: tuple[str, ...]) -> CommandResult:
        result = run_bounded(argv, self.paths.command_timeout_seconds)
        self.command_evidence.append(result.evidence(purpose))
        if result.returncode != 0 or result.timed_out:
            raise ActivationError(f"{purpose} failed")
        return result

    def verify_screen_capture_continuity(self) -> dict[str, Any] | None:
        if self.paths.screen_capture_binding is None:
            return None
        return verify_screen_capture_binding(self.paths.screen_capture_binding, self.paths.node)

    def migrate_state_once(self, release: Path) -> dict[str, Any]:
        versions = native_migration_schema_versions(release)
        entrypoint = release / "dist" / "infra" / "runtime-state-migration.js"
        source = r"""
import { pathToFileURL } from "node:url";
try {
  const { migrateRuntimeState } = await import(pathToFileURL(process.env.OPENCLAW_MIGRATION_ENTRYPOINT).href);
  const report = await migrateRuntimeState({
    stateDir: process.env.OPENCLAW_STATE_DIR,
    configPath: process.env.OPENCLAW_CONFIG_PATH,
  });
  process.stdout.write(JSON.stringify(report));
} catch {
  process.stderr.write("Runtime state migration failed.\n");
  process.exitCode = 1;
}
"""
        state = self.paths.candidate_state_dir
        environment = {
            "HOME": str(expected_gateway_working_directory(self.paths)),
            "PATH": str(self.paths.node.parent) + ":/usr/bin:/bin",
            "OPENCLAW_STATE_DIR": str(state),
            "OPENCLAW_CONFIG_PATH": str(state / "openclaw.json"),
            "OPENCLAW_MIGRATION_ENTRYPOINT": str(entrypoint),
            "OPENCLAW_SUPERVISOR_MODE": "external",
            "OPENCLAW_SERVICE_REPAIR_POLICY": "external",
            "TMPDIR": str(state / "tmp"),
            "XDG_CACHE_HOME": str(state / ".cache"),
            "OPENCLAW_LOG_LEVEL": "silent",
        }
        result = run_bounded(
            (str(self.paths.node), "--input-type=module", "--eval", source),
            SNAPSHOT_TIMEOUT_SECONDS, cwd=release, env=environment,
        )
        self.command_evidence.append(result.evidence("native_state_migration"))
        if result.returncode != 0 or result.timed_out:
            raise ActivationError("native state migration failed")
        try:
            report = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ActivationError("native state migration receipt is invalid") from exc
        if (not isinstance(report, dict)
                or set(report) != {"schemaVersion", "status", "stateSchemaVersion", "agentSchemaVersion", "agentDatabaseCount"}
                or type(report.get("schemaVersion")) is not int or report["schemaVersion"] != 1
                or report.get("status") != "completed"
                or type(report.get("stateSchemaVersion")) is not int or report["stateSchemaVersion"] != versions["state"]
                or type(report.get("agentSchemaVersion")) is not int or report["agentSchemaVersion"] != versions["agent"]
                or type(report.get("agentDatabaseCount")) is not int or report["agentDatabaseCount"] < 0):
            raise ActivationError("native state migration receipt contract drift")
        return report

    def _print(self, label: str) -> CommandResult:
        result = run_bounded(
            ("/bin/launchctl", "print", f"gui/{self.paths.operator_uid}/{label}"),
            self.paths.command_timeout_seconds,
        )
        self.command_evidence.append(result.evidence(f"{label}_launchctl_print"))
        return result

    def _service_missing(self, result: CommandResult, label: str) -> bool:
        expected = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for user gui: '
            f"{self.paths.operator_uid}\n"
        ).encode()
        return (not result.timed_out and result.returncode == 113
                and result.stdout == b"" and result.stderr == expected)

    def assert_gateway_and_node_stopped(self) -> None:
        for label in (OPERATOR.require_string("runtime.gateway_label"), OPERATOR.require_string("runtime.node_label")):
            result = self._print(label)
            if not self._service_missing(result, label):
                raise ActivationError(f"{label} exact stopped-state proof failed")
        listener = run_bounded(
            ("/usr/sbin/lsof", "-nP", f"-iTCP:{runtime_gateway_port()}",
             "-sTCP:LISTEN", "-Fpn"),
            self.paths.command_timeout_seconds,
        )
        self.command_evidence.append(listener.evidence("gateway_listener_absence"))
        if (listener.timed_out or listener.returncode != 1
                or listener.stdout != b"" or listener.stderr != b""):
            raise ActivationError("gateway listener exact stopped-state proof failed")

    def inspect_exec_approvals(self, release: Path) -> dict[str, Any]:
        inspector_path = release / "dist" / "infra" / "exec-approvals-inspection.js"
        inspector_payload, inspector_info = read_physical(
            inspector_path, "candidate exec approvals inspector"
        )
        if not inspector_payload or stat.S_IMODE(inspector_info.st_mode) & 0o222:
            raise ActivationError("candidate exec approvals inspector drift")
        source = r"""
import { pathToFileURL } from "node:url";
const module = await import(
  pathToFileURL(process.env.OPENCLAW_APPROVALS_INSPECTOR).href
);
if (typeof module.inspectExecApprovalsState !== "function") {
  throw new Error("candidate exec approvals inspector export drift");
}
const report = module.inspectExecApprovalsState({
  stateDir: process.env.OPENCLAW_STATE_DIR,
  legacyPath: process.env.OPENCLAW_LEGACY_EXEC_APPROVALS_PATH,
});
process.stdout.write(JSON.stringify(report));
"""
        environment = {
            "OPENCLAW_STATE_DIR": str(self.paths.candidate_state_dir),
            "OPENCLAW_APPROVALS_INSPECTOR": str(inspector_path),
            "OPENCLAW_LEGACY_EXEC_APPROVALS_PATH": str(
                self._legacy_exec_approvals_path
            ),
        }
        result = run_bounded(
            (str(self.paths.node), "--input-type=module", "--eval", source),
            self.paths.command_timeout_seconds,
            cwd=release,
            env=environment,
        )
        self.command_evidence.append(result.evidence("exec_approvals_inspection"))
        if (
            result.returncode != 0
            or result.timed_out
            or result.stderr != b""
        ):
            raise ActivationError("candidate exec approvals inspection failed")
        try:
            receipt = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ActivationError("candidate exec approvals receipt malformed") from exc
        return validate_exec_approvals_inspection(receipt)

    def verify_candidate_discord(self, release: Path, config_path: Path) -> dict[str, Any]:
        """Prove Discord is bundled and has no stale install record, without mutation."""
        before_payload, _ = read_physical(config_path, "runtime config")
        source = r"""
import fs from "node:fs";
import { pathToFileURL } from "node:url";
const root = process.env.OPENCLAW_CANDIDATE_ROOT;
const moduleAt = (path) => import(pathToFileURL(`${root}/${path}`).href);
const loadUniqueFacade = async (prefix, requiredExports) => {
  // Split ESM chunks use .mjs in 2026.9.3; retain .js predecessor support.
  const candidates = fs.readdirSync(`${root}/dist`)
    .filter((name) =>
      name.startsWith(prefix) && (name.endsWith(".js") || name.endsWith(".mjs")),
    );
  const matches = [];
  for (const name of candidates) {
    const module = await moduleAt(`dist/${name}`);
    if (requiredExports.every((key) => typeof module[key] === "function")) {
      matches.push(module);
    }
  }
  if (matches.length !== 1) {
    throw new Error(`candidate ${prefix} facade selection drift`);
  }
  return matches[0];
};
const { loadConfig } = await moduleAt("dist/config/config.js");
const recordsApi = await loadUniqueFacade("installed-plugin-index-records-", [
  "loadInstalledPluginIndexInstallRecords",
]);
const { loadPluginRegistryHandle } = await moduleAt("dist/plugins/loader.js");
const config = loadConfig({ pin: false });
const installed = await recordsApi.loadInstalledPluginIndexInstallRecords();
if (Object.hasOwn(installed, "discord")) {
  throw new Error("candidate retains a stale Discord install record");
}
const report = loadPluginRegistryHandle({
  config,
  cache: false,
  onlyPluginIds: ["discord"],
  throwOnLoadError: false,
});
const plugin = report.plugins.find((item) => item.id === "discord");
process.stdout.write(JSON.stringify({
  discordInstallRecordAbsent: true,
  installedIds: Object.keys(installed).sort(),
  plugin,
}));
"""
        environment = dict(os.environ)
        environment["OPENCLAW_STATE_DIR"] = str(self.paths.candidate_state_dir)
        environment["OPENCLAW_CONFIG_PATH"] = str(config_path)
        environment["OPENCLAW_CANDIDATE_ROOT"] = str(release)
        result = run_bounded(
            (str(self.paths.node), "--input-type=module", "--eval", source),
            self.paths.command_timeout_seconds, cwd=release, env=environment,
        )
        self.command_evidence.append(result.evidence("candidate_discord_verification"))
        if result.returncode != 0 or result.timed_out:
            raise ActivationError("candidate Discord verification failed")
        try:
            receipt = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ActivationError("candidate Discord receipt malformed") from exc
        after_payload, _ = read_physical(config_path, "runtime config")
        installed_ids = receipt.get("installedIds") if isinstance(receipt, dict) else None
        plugin = receipt.get("plugin") if isinstance(receipt, dict) else None
        expected_source = release / "dist" / "extensions" / "discord" / "index.js"
        if (before_payload != after_payload
                or receipt.get("discordInstallRecordAbsent") is not True
                or not isinstance(installed_ids, list)
                or any(not isinstance(plugin_id, str) for plugin_id in installed_ids)
                or installed_ids != sorted(set(installed_ids))
                or "discord" in installed_ids
                or not isinstance(plugin, dict) or plugin.get("origin") != "bundled"
                or Path(str(plugin.get("source", ""))) != expected_source
                or plugin.get("status") != "loaded" or plugin.get("enabled") is not True
                or plugin.get("error") not in (None, "")
                or plugin.get("channelIds") != ["discord"]):
            raise ActivationError("candidate Discord source selection drift")
        return {"installRecordAbsent": True,
                "installedIds": installed_ids,
                "configSha256": sha256_bytes(after_payload), "origin": "bundled",
                "source": str(expected_source)}

    def _selector_binding(self) -> dict[str, Any]:
        try:
            selector_info = os.lstat(self.paths.current_link)
            if not stat.S_ISLNK(selector_info.st_mode):
                raise ActivationError("current selector is not a symlink")
            release = self.paths.current_link.resolve(strict=True)
            release_info = os.lstat(release)
        except OSError as exc:
            raise ActivationError("current selector identity is unavailable") from exc
        if release.parent != self.paths.releases_root:
            raise ActivationError("current selector target is outside the releases root")
        return {
            "release": str(release),
            "releaseDevice": release_info.st_dev,
            "releaseInode": release_info.st_ino,
            "selectorDevice": selector_info.st_dev,
            "selectorInode": selector_info.st_ino,
        }

    def _bootstrap(self, label: str, plist: Path, prefix: str) -> None:
        domain = f"gui/{self.paths.operator_uid}"
        before = self._selector_binding()
        self._command(f"{prefix}_enable", ("/bin/launchctl", "enable", f"{domain}/{label}"))
        started_at_us = time.time_ns() // 1_000
        # Record the exact selector immediately before consuming bootstrap.
        # If bootstrap returns ambiguously after starting the service, the
        # immutable restore receipt can still bind a later read-only check to
        # this one attempt. A failed enable does not create launch authority.
        self._bootstrap_bindings[label] = {
            **before,
            "startedAtUs": started_at_us,
        }
        self._receipt_generations.pop(label, None)
        bootstrap_argv = (
            "/bin/launchctl",
            "bootstrap",
            domain,
            str(plist),
        )
        bootstrap = run_bounded(
            bootstrap_argv,
            self.paths.command_timeout_seconds,
        )
        self.command_evidence.append(
            bootstrap.evidence(f"{prefix}_bootstrap")
        )
        if bootstrap.returncode != 0 or bootstrap.timed_out:
            if not bootstrap.timed_out:
                self._bootstrap_bindings.pop(label, None)
            raise ActivationError(f"{prefix}_bootstrap failed")
        after = self._selector_binding()
        if after != before:
            raise ActivationError(f"{label} bootstrap selector changed")

    def bootstrap_gateway_once(self) -> None:
        self._bootstrap(OPERATOR.require_string("runtime.gateway_label"), self.paths.gateway_plist, "gateway")

    def bootstrap_node_once(self) -> None:
        self._bootstrap(OPERATOR.require_string("runtime.node_label"), self.paths.node_plist, "node")

    def bootstrap_bindings(self) -> dict[str, dict[str, Any]]:
        return {
            label: dict(binding)
            for label, binding in self._bootstrap_bindings.items()
        }

    def _validate_receipt_bootstrap_binding(
        self,
        label: str,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            label not in (OPERATOR.require_string("runtime.gateway_label"), OPERATOR.require_string("runtime.node_label"))
            or not bootstrap_binding_shape_is_valid(binding)
        ):
            raise ActivationError("bootstrap receipt binding drift")
        release_value = binding.get("release")
        if (
            binding["startedAtUs"] > time.time_ns() // 1_000
        ):
            raise ActivationError("bootstrap receipt binding drift")
        release = Path(release_value)
        try:
            release_info = os.lstat(release)
        except OSError as exc:
            raise ActivationError("bootstrap receipt binding drift") from exc
        if (
            not release.is_absolute()
            or release.parent != self.paths.releases_root
            or not stat.S_ISDIR(release_info.st_mode)
            or (release_info.st_dev, release_info.st_ino)
            != (binding["releaseDevice"], binding["releaseInode"])
            or self._selector_binding()
            != {
                key: binding[key]
                for key in (
                    "release",
                    "releaseDevice",
                    "releaseInode",
                    "selectorDevice",
                    "selectorInode",
                )
            }
        ):
            raise ActivationError("bootstrap receipt binding drift")
        return dict(binding)

    def bind_bootstrap_receipt(
        self,
        label: str,
        binding: dict[str, Any],
        loaded: dict[str, Any] | None = None,
    ) -> None:
        validated = self._validate_receipt_bootstrap_binding(label, binding)
        existing = self._bootstrap_bindings.get(label)
        if existing is not None and existing != validated:
            raise ActivationError("bootstrap receipt binding drift")
        generation: tuple[int, str] | None = None
        if loaded is not None:
            pid = loaded.get("pid")
            start_token = loaded.get("startToken")
            if (
                loaded.get("bootstrapBinding") != validated
                or loaded.get("release") != validated["release"]
                or loaded.get("releaseDevice") != validated["releaseDevice"]
                or loaded.get("releaseInode") != validated["releaseInode"]
                or type(pid) is not int
                or pid <= 0
                or not isinstance(start_token, str)
            ):
                raise ActivationError("bootstrap receipt generation drift")
            started_at_us = process_start_time_us(start_token)
            if (
                started_at_us < validated["startedAtUs"]
                or started_at_us > time.time_ns() // 1_000
            ):
                raise ActivationError("bootstrap receipt generation drift")
            generation = (pid, start_token)
        self._bootstrap_bindings[label] = validated
        if generation is None:
            self._receipt_generations.pop(label, None)
        else:
            self._receipt_generations[label] = generation

    def _validate_bootstrap_binding(
        self,
        label: str,
        release: Path,
        device: int,
        inode: int,
        pid: int,
        start_token: str,
    ) -> dict[str, Any]:
        binding = self._bootstrap_bindings.get(label)
        if binding is None:
            raise ActivationError(f"loaded {label} service lacks bootstrap binding")
        if self._selector_binding() != {
            key: binding[key]
            for key in (
                "release",
                "releaseDevice",
                "releaseInode",
                "selectorDevice",
                "selectorInode",
            )
        }:
            raise ActivationError(f"loaded {label} bootstrap selector changed")
        if (
            binding["release"] != str(release)
            or binding["releaseDevice"] != device
            or binding["releaseInode"] != inode
        ):
            raise ActivationError(f"loaded {label} bootstrap release mismatch")
        started_at_us = process_start_time_us(start_token)
        if started_at_us < binding["startedAtUs"] or started_at_us > time.time_ns() // 1_000:
            raise ActivationError(f"loaded {label} process predates its bootstrap")
        receipt_generation = self._receipt_generations.get(label)
        if receipt_generation is not None and receipt_generation != (pid, start_token):
            raise ActivationError(f"loaded {label} receipt generation changed")
        return dict(binding)

    def _capture_loaded(self, label: str, release: Path, device: int,
                        inode: int) -> dict[str, Any]:
        printed = self._print(label)
        if printed.returncode != 0 or printed.timed_out:
            raise ActivationError("launchd service is unavailable")
        pid, runs, arguments, service_path, loaded_working_directory = parse_launchctl(
            printed.stdout, self.paths.operator_uid, label
        )
        plist_path = self.paths.gateway_plist if label == OPERATOR.require_string("runtime.gateway_label") else self.paths.node_plist
        plist_payload, _ = read_physical(plist_path, f"{label} plist")
        try:
            persisted = plistlib.loads(plist_payload)
        except Exception as exc:
            raise ActivationError(f"{label} plist is invalid") from exc
        persisted_arguments = persisted.get("ProgramArguments") if isinstance(persisted, dict) else None
        working_directory = persisted.get("WorkingDirectory") if isinstance(persisted, dict) else None
        if (service_path != plist_path or not isinstance(persisted_arguments, list)
                or not all(isinstance(item, str) and item for item in persisted_arguments)
                or tuple(persisted_arguments) != arguments):
            raise ActivationError(f"loaded {label} service does not match its plist")
        release_info = os.lstat(release)
        if (release_info.st_dev, release_info.st_ino) != (device, inode):
            raise ActivationError("loaded release identity mismatch")
        if label == OPERATOR.require_string("runtime.gateway_label"):
            expected_working_directory = expected_gateway_working_directory(self.paths)
            try:
                working_directory_info = os.lstat(expected_working_directory)
            except OSError as exc:
                raise ActivationError("gateway working directory is unavailable") from exc
            if not stat.S_ISDIR(working_directory_info.st_mode):
                raise ActivationError("gateway working directory is unavailable")
            cwd = self._command(
                f"{label}_cwd",
                ("/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-d", "cwd", "-F0pfinD"),
            )
            if parse_lsof_cwd(cwd.stdout, pid) != (
                expected_working_directory,
                working_directory_info.st_dev,
                working_directory_info.st_ino,
            ):
                raise ActivationError("gateway working directory identity drift")
        start_token, executable, _parent_pid = process_identity(pid)
        bootstrap_binding = self._validate_bootstrap_binding(
            label,
            release,
            device,
            inode,
            pid,
            start_token,
        )
        try:
            entrypoint = Path(arguments[1]).resolve(strict=True)
            pinned_node = self.paths.node.resolve(strict=True)
        except (IndexError, OSError) as exc:
            raise ActivationError("node service entrypoint identity is unavailable") from exc
        if entrypoint != release / "dist" / "index.js" or executable != pinned_node:
            raise ActivationError("node service process identity drift")
        if label == OPERATOR.require_string("runtime.gateway_label") and (
            list(arguments) != expected_gateway_arguments(self.paths)
            or working_directory != str(expected_gateway_working_directory(self.paths))
            or loaded_working_directory != expected_gateway_working_directory(self.paths)
        ):
            raise ActivationError("gateway direct launchd ownership drift")
        return {
            "release": str(release), "releaseDevice": device, "releaseInode": inode,
            "pid": pid, "runs": runs, "startToken": start_token,
            "executable": str(executable), "servicePath": str(service_path),
            "argumentsObservedExact": True,
            "argumentVectorSha256": sha256_bytes(canonical_json_bytes(list(arguments))),
            "bootstrapBinding": bootstrap_binding,
            **(
                {
                    "workingDirectory": str(expected_gateway_working_directory(self.paths)),
                    "workingDirectoryDevice": working_directory_info.st_dev,
                    "workingDirectoryInode": working_directory_info.st_ino,
                }
                if label == OPERATOR.require_string("runtime.gateway_label")
                else {}
            ),
        }

    def _loaded(self, label: str, release: Path, device: int, inode: int,
                deadline: float | None = None) -> dict[str, Any]:
        deadline = deadline or (time.monotonic() + self.paths.health_timeout_seconds)
        last_error: BaseException | None = None
        while True:
            try:
                return self._capture_loaded(label, release, device, inode)
            except (ActivationError, OSError) as exc:
                last_error = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(min(self.paths.health_poll_seconds,
                           max(0.0, deadline - time.monotonic())))
        raise ActivationError("loaded release identity did not converge") from last_error

    def _listener(self, launchd_pid: int) -> int:
        observed = self._command(
            "gateway_listener_owner",
            ("/usr/sbin/lsof", "-nP", f"-iTCP:{runtime_gateway_port()}",
             "-sTCP:LISTEN", "-Fpn"),
        )
        listener_pid = parse_lsof_listener_pid(observed.stdout)
        if listener_pid != launchd_pid:
            raise ActivationError("gateway listener PID does not match launchd PID")
        return listener_pid

    def _probe(self, path: str) -> dict[str, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", runtime_gateway_port(), timeout=2)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            status_code = response.status
            payload = response.read(64 * 1024 + 1)
            if len(payload) > 64 * 1024:
                raise ActivationError("gateway health payload exceeded its bound")
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ActivationError("gateway health payload is not an object")
            accepted = status_code == 200
            if path == "/healthz":
                accepted = accepted and value == {"ok": True, "status": "live"}
            else:
                accepted = accepted and value.get("ready") is True and value.get("failing") == []
            return {"accepted": accepted, "statusCode": status_code,
                    "bodyBytes": len(payload), "bodySha256": sha256_bytes(payload)}
        except (OSError, TimeoutError, http.client.HTTPException,
                UnicodeDecodeError, json.JSONDecodeError, ActivationError):
            return {"accepted": False, "statusCode": None,
                    "bodyBytes": 0, "bodySha256": None}
        finally:
            connection.close()

    def _wait_health(self, deadline: float) -> dict[str, Any]:
        attempts = 0
        while True:
            attempts += 1
            health = {"healthz": self._probe("/healthz"),
                      "readyz": self._probe("/readyz")}
            if all(item["accepted"] for item in health.values()):
                return {"attempts": attempts, **health}
            if time.monotonic() >= deadline:
                raise ActivationError("gateway health did not converge")
            time.sleep(min(self.paths.health_poll_seconds,
                           max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _generation(value: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(value[key] for key in (
            "pid", "runs", "startToken", "executable", "servicePath",
            "argumentVectorSha256", "releaseDevice", "releaseInode",
            "bootstrapBinding",
        ))

    def verify(self, release: Path, device: int, inode: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.paths.health_timeout_seconds
        last_error: BaseException | None = None
        while True:
            try:
                first = self._loaded(OPERATOR.require_string("runtime.gateway_label"), release, device, inode, deadline)
                self._listener(first["pid"])
                health = self._wait_health(deadline)
                second = self._loaded(OPERATOR.require_string("runtime.gateway_label"), release, device, inode, deadline)
                self._listener(second["pid"])
                if self._generation(first) != self._generation(second):
                    raise ActivationError(
                        "gateway generation changed during health verification"
                    )
                return {
                    "loaded": {
                        **second,
                        "directLaunchdOwner": True,
                        "listenerPidMatchesLaunchdPid": True,
                    },
                    "health": health,
                }
            except (ActivationError, OSError) as exc:
                last_error = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(min(self.paths.health_poll_seconds,
                           max(0.0, deadline - time.monotonic())))
        raise ActivationError("stable gateway generation did not converge") from last_error

    def verify_node(self, release: Path, device: int, inode: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.paths.health_timeout_seconds
        first = self._loaded(OPERATOR.require_string("runtime.node_label"), release, device, inode, deadline)
        second = self._loaded(OPERATOR.require_string("runtime.node_label"), release, device, inode, deadline)
        if self._generation(first) != self._generation(second):
            raise ActivationError("node generation changed during startup verification")
        return second


def _start_fence_path(paths: ActivationPaths) -> Path:
    return paths.result.with_name(START_CONSUMED_NAME)


def _acquire_lock(paths: ActivationPaths) -> int:
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise ActivationError("another activator holds the fixed lock") from exc
    return descriptor


def _release_lock(descriptor: int) -> None:
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


def create_stopped_snapshot(
    paths: ActivationPaths,
    output_root: Path,
    manifest_path: Path,
    candidate: Path,
    expected_commit: str,
    candidate_seal: Path,
    backend: ActivationBackend,
) -> dict[str, Any]:
    if (not output_root.is_absolute() or not manifest_path.is_absolute()
            or manifest_path.parent != output_root
            or manifest_path.name != "stopped-snapshot.json"):
        raise ActivationError("stopped snapshot output binding drift")
    descriptor = _acquire_lock(paths)
    try:
        expected_commit = require_commit(expected_commit, "candidate commit")
        if output_root.exists() or output_root.is_symlink():
            raise ActivationError("stopped snapshot output root already exists")
        output_root.mkdir(mode=0o700)
        os.chmod(output_root, 0o700)
        fsync_directory(output_root.parent)

        if not paths.current_link.is_symlink():
            raise ActivationError("current selector is not a symlink")
        predecessor = Path(os.readlink(paths.current_link))
        candidate_record = validate_candidate_seal(
            paths, candidate, expected_commit, candidate_seal
        )
        build, _, _ = read_json(
            predecessor / "dist" / "build-info.json", "snapshot predecessor build info"
        )
        commit = require_commit(str(build.get("commit", "")), "snapshot predecessor commit")
        predecessor_record = validate_release(
            paths,
            predecessor,
            commit,
            require_candidate_native_entrypoints=False,
        )
        plists = validate_plists(paths)

        evidence_start = len(backend.command_evidence)
        backend.assert_gateway_and_node_stopped()
        pre_capture = list(backend.command_evidence[evidence_start:])
        validate_stopped_command_evidence(paths, pre_capture)
        invariants = validate_recovered_invariants(paths)
        inspection_release = stopped_inspection_release(candidate_record, predecessor_record)
        approvals = backend.inspect_exec_approvals(inspection_release)

        archive_path = output_root / "stopped-snapshot.tar"
        members = (
            paths.predecessor_state_dir,
            paths.gateway_plist,
            paths.node_plist,
            paths.current_link,
        )
        if any(not member.is_absolute() for member in members):
            raise ActivationError("stopped snapshot member path drift")
        tar_argv = (
            "/usr/bin/tar", "--xattrs", "--acls", "--fflags", "-cpf",
            str(archive_path), "-C", "/", *(str(member).lstrip("/") for member in members),
        )
        archived = run_bounded(tar_argv, SNAPSHOT_TIMEOUT_SECONDS)
        backend.command_evidence.append(archived.evidence("stopped_snapshot_archive"))
        if archived.returncode != 0 or archived.timed_out:
            raise ActivationError("stopped snapshot archive failed")
        os.chmod(archive_path, 0o400)
        archive_descriptor = os.open(archive_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(archive_descriptor)
        finally:
            os.close(archive_descriptor)
        archive_sha, archive_info = sha256_physical_file(
            archive_path, "stopped snapshot archive"
        )
        if stat.S_IMODE(archive_info.st_mode) != 0o400:
            raise ActivationError("stopped snapshot archive mode drift")

        post_start = len(backend.command_evidence)
        backend.assert_gateway_and_node_stopped()
        post_capture = list(backend.command_evidence[post_start:])
        validate_stopped_command_evidence(paths, post_capture)
        post_approvals = backend.inspect_exec_approvals(inspection_release)
        post_invariants = validate_recovered_invariants(paths)
        post_plists = validate_plists(paths)
        if (
            post_approvals != approvals
            or post_approvals["rawCasSha256"] != approvals["rawCasSha256"]
            or post_invariants != invariants
            or post_plists != plists
            or not paths.current_link.is_symlink()
            or os.readlink(paths.current_link) != str(predecessor)
        ):
            raise ActivationError("stopped snapshot protected surface drift")

        g6_path = output_root / "stopped-quiescence.json"
        g6 = {
            "schemaVersion": 1,
            "capturedAt": utc_now(),
            "archivePath": str(archive_path),
            "archiveSha256": archive_sha,
            "preCapture": pre_capture,
            "postCapture": post_capture,
        }
        create_immutable_file(
            g6_path, json.dumps(g6, indent=2, sort_keys=True).encode() + b"\n"
        )
        g6_payload, _ = read_physical(g6_path, "stopped snapshot quiescence")
        manifest = {
            "schemaVersion": 2,
            "tarPath": str(archive_path),
            "tarSha256": archive_sha,
            "inventoryCount": len(members),
            "g6QuiescencePath": str(g6_path),
            "g6QuiescenceSha256": sha256_bytes(g6_payload),
            "predecessorStateDir": str(paths.predecessor_state_dir),
            "candidateStateDir": str(paths.candidate_state_dir),
            "currentTarget": str(predecessor),
            "predecessor": predecessor_record,
            "recoveredInvariants": invariants,
            "execApprovalsInspector": exec_approvals_inspector_binding(
                candidate_record, predecessor_record
            ),
            "execApprovals": approvals,
            "gatewayPlistSha256": plists["gateway"]["sha256"],
            "nodePlistSha256": plists["node"]["sha256"],
        }
        create_immutable_file(
            manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n",
        )
        observed = load_stopped_snapshot(paths, manifest_path, candidate_record)
        return {
            **observed,
            "commandEvidence": list(backend.command_evidence),
        }
    finally:
        _release_lock(descriptor)


def retire_terminal_receipts(
    paths: ActivationPaths,
    archive_dir: Path,
    backend: ActivationBackend | None = None,
) -> dict[str, Any]:
    archive_root = paths.result.parent / "archive"
    if (not archive_dir.is_absolute() or archive_dir.parent != archive_root
            or archive_dir.name in ("", ".", "..")):
        raise ActivationError("activation receipt archive binding drift")
    descriptor = _acquire_lock(paths)
    try:
        result, result_payload, result_info = read_json(
            paths.result, "terminal activation result"
        )
        fence_path = _start_fence_path(paths)
        fence, fence_payload, fence_info = read_json(
            fence_path, "terminal activation start fence"
        )
        candidate = result.get("candidate")
        start = result.get("startConsumption")
        snapshot = result.get("snapshot")
        activation_inputs = result.get("activationInputs")
        candidate_path = Path(str(candidate.get("path", ""))) if isinstance(candidate, dict) else Path()
        common_fence_bound = (
            isinstance(candidate, dict)
            and isinstance(snapshot, dict)
            and isinstance(start, dict)
            and start.get("path") == str(fence_path)
            and start.get("sha256") == sha256_bytes(fence_payload)
            and start.get("mode") == 0o400
            and start.get("nlink") == 1
            and start.get("consumedAt") == fence.get("consumedAt")
            and fence.get("candidateSealSha256") == candidate.get("sealSha256")
            and fence.get("snapshotManifestSha256") == snapshot.get("manifestSha256")
        )
        current_fence_bound = (
            common_fence_bound
            and fence.get("schemaVersion") == 1
            and fence.get("terminalResultPath") == str(paths.result)
            and fence.get("candidateSealPath") == candidate.get("sealPath")
            and fence.get("snapshotManifestPath") == snapshot.get("manifestPath")
            and fence.get("upgrade") == result.get("upgrade")
        )
        legacy_fence_bound = (
            common_fence_bound
            and fence.get("schemaVersion") == 2
            and isinstance(activation_inputs, dict)
            and fence.get("candidatePath") == candidate.get("path")
            and fence.get("candidateCommit") == candidate.get("commit")
            and fence.get("candidateDevice") == candidate.get("device")
            and fence.get("candidateInode") == candidate.get("inode")
            and fence.get("candidateSealPath") == candidate.get("sealPath")
            and fence.get("snapshotManifestPath") == snapshot.get("manifestPath")
            and fence.get("candidateSealPath") == activation_inputs.get("candidateSealPath")
            and fence.get("snapshotManifestPath")
            == activation_inputs.get("day0SnapshotManifestPath")
            and fence.get("bootstrapManifestPath")
            == activation_inputs.get("externalPluginBootstrapManifestPath")
            and fence.get("bootstrapManifestSha256")
            == activation_inputs.get("externalPluginBootstrapManifestSha256")
        )
        restore_path: Path | None = None
        restore_payload: bytes | None = None
        restore_info: os.stat_result | None = None
        late_verification: dict[str, Any] | None = None
        late_verification_required = False
        retirement_outcome: str
        selected_record: dict[str, Any] | None
        cross_version = (
            isinstance(candidate, dict) and isinstance(snapshot, dict)
            and isinstance(snapshot.get("predecessor"), dict)
            and candidate.get("packageVersion") != snapshot["predecessor"].get("packageVersion")
        )
        if cross_version and (not isinstance(snapshot.get("manifestSha256"), str)
                              or re.fullmatch(r"[0-9a-f]{64}", snapshot["manifestSha256"]) is None):
            raise ActivationError("terminal activation restore receipt binding drift")
        restored_successful_upgrade = (
            cross_version and result.get("outcome") == "activated"
            and isinstance(result.get("upgrade"), dict)
            and (paths.result.parent / f"activation-restore-result-{snapshot['manifestSha256'][:16]}.json").exists()
        )
        if (result.get("outcome") == "activated"
                and result.get("restoreRequired") is False
                and not restored_successful_upgrade):
            retirement_outcome = "activated"
            selected_record = candidate if isinstance(candidate, dict) else None
            terminal_bound = current_fence_bound or legacy_fence_bound
        elif (((result.get("outcome") == "snapshot_restore_required"
                 and result.get("restoreRequired") is True) or restored_successful_upgrade)
                and current_fence_bound
                and isinstance(snapshot, dict)):
            snapshot_manifest_sha = snapshot.get("manifestSha256")
            if (not isinstance(snapshot_manifest_sha, str)
                    or re.fullmatch(r"[0-9a-f]{64}", snapshot_manifest_sha) is None):
                raise ActivationError("terminal activation restore receipt binding drift")
            expected_restore_name = (
                f"activation-restore-result-{snapshot_manifest_sha[:16]}.json"
            )
            restore_path = paths.result.parent / expected_restore_name
            restore, restore_payload, restore_info = read_json(
                restore_path, "terminal activation restore result"
            )
            predecessor = snapshot.get("predecessor")
            verification = restore.get("verification")
            bootstrap_bindings = restore.get("bootstrapBindings")
            restore_command_evidence = restore.get("commandEvidence")
            if (
                not isinstance(verification, dict)
                or not isinstance(bootstrap_bindings, dict)
            ):
                raise ActivationError("terminal activation restore receipt binding drift")
            prior_gateway = verification.get("gateway")
            prior_node = verification.get("node")
            stopped_upgrade_restore = (
                cross_version and restore.get("outcome") == "restored_stopped"
                and result.get("firstBootAttempted") is not False
                and restore.get("afterSuccessfulUpgrade") == restored_successful_upgrade
                and restore.get("error") is None
                and prior_gateway is None and prior_node is None
                and bootstrap_bindings == {}
                and receipt_has_successful_command(
                    restore_command_evidence, "stopped_snapshot_restore",
                    ("/usr/bin/tar", "--xattrs", "--acls", "--fflags", "-xpf", snapshot["tarPath"], "-C", "/"),
                )
            )
            expected_bootstrap_labels = {OPERATOR.require_string("runtime.gateway_label"), OPERATOR.require_string("runtime.node_label")}
            bootstrap_bindings_well_formed = (
                set(bootstrap_bindings) == expected_bootstrap_labels
                and all(
                    bootstrap_binding_shape_is_valid(bootstrap_bindings[label])
                    for label in expected_bootstrap_labels
                )
            )
            restore_bootstraps_succeeded = (
                receipt_has_successful_command(
                    restore_command_evidence,
                    "gateway_bootstrap",
                    (
                        "/bin/launchctl",
                        "bootstrap",
                        f"gui/{paths.operator_uid}",
                        str(paths.gateway_plist),
                    ),
                )
                and receipt_has_successful_command(
                    restore_command_evidence,
                    "node_bootstrap",
                    (
                        "/bin/launchctl",
                        "bootstrap",
                        f"gui/{paths.operator_uid}",
                        str(paths.node_plist),
                    ),
                )
            )
            if (stat.S_IMODE(restore_info.st_mode) != 0o400
                    or restore.get("schemaVersion") != 1
                    or restore.get("restoreApplied") is not True
                    or restore.get("failedActivationResultPath") != str(paths.result)
                    or restore.get("failedActivationResultSha256")
                    != sha256_bytes(result_payload)
                    or restore.get("candidate") != candidate
                    or restore.get("snapshot") != snapshot
                    or restore.get("rollback") != predecessor
                    or set(verification) != {"gateway", "node"}
                    or not (bootstrap_bindings_well_formed or stopped_upgrade_restore)
                    or not (prior_gateway is None or isinstance(prior_gateway, dict))
                    or not (prior_node is None or isinstance(prior_node, dict))):
                raise ActivationError("terminal activation restore receipt binding drift")
            if stopped_upgrade_restore:
                retirement_outcome = "restored_stopped"
            elif (restore.get("outcome") == "restored"
                    and restore.get("error") is None
                    and isinstance(prior_gateway, dict)
                    and isinstance(prior_node, dict)
                    and restore_bootstraps_succeeded
                    and isinstance(prior_gateway.get("loaded"), dict)
                    and prior_gateway["loaded"].get("bootstrapBinding")
                    == bootstrap_bindings[OPERATOR.require_string("runtime.gateway_label")]
                    and prior_node.get("bootstrapBinding")
                    == bootstrap_bindings[OPERATOR.require_string("runtime.node_label")]):
                retirement_outcome = "restored"
            elif (restore.get("outcome") == "restored_boot_failed"
                    and restore.get("error") == "predecessor_boot_failed"
                    and isinstance(prior_gateway, dict)
                    and isinstance(prior_gateway.get("loaded"), dict)
                    and prior_gateway["loaded"].get("bootstrapBinding")
                    == bootstrap_bindings[OPERATOR.require_string("runtime.gateway_label")]
                    and restore_bootstraps_succeeded
                    and prior_node is None):
                if backend is None:
                    raise ActivationError("late restore verification backend is unavailable")
                retirement_outcome = "restored_after_late_verification"
                late_verification_required = True
            else:
                raise ActivationError("terminal activation restore receipt binding drift")
            selected_record = predecessor if isinstance(predecessor, dict) else None
            terminal_bound = True
        else:
            retirement_outcome = ""
            selected_record = None
            terminal_bound = False
        selected_path = (
            Path(str(selected_record.get("path", "")))
            if isinstance(selected_record, dict) else Path()
        )
        if (stat.S_IMODE(result_info.st_mode) != 0o400
                or stat.S_IMODE(fence_info.st_mode) != 0o400
                or not isinstance(candidate, dict)
                or not candidate_path.is_absolute()
                or not isinstance(selected_record, dict)
                or not selected_path.is_absolute()
                or not paths.current_link.is_symlink()
                or os.readlink(paths.current_link) != str(selected_path)
                or not terminal_bound):
            raise ActivationError("terminal activation receipt binding drift")
        release_records = {"candidate": candidate, "selected": selected_record}
        if isinstance(result.get("rollback"), dict):
            release_records["rollback"] = result["rollback"]
        release_identities: dict[str, Any] = {}
        observed_releases: dict[str, Any] = {}
        for role, record in release_records.items():
            key = sha256_bytes(canonical_json_bytes(record))
            if key not in observed_releases:
                observed_releases[key] = _terminal_release_identity(paths, record)
            release_identities[role] = observed_releases[key]
        if any(identity["device"] != release_records[role]["device"]
               for role, identity in release_identities.items()):
            _validate_terminal_candidate_seal(candidate)
            manifest, payload, info = read_json(Path(snapshot["manifestPath"]), "terminal stopped snapshot")
            if (stat.S_IMODE(info.st_mode) & 0o222
                    or sha256_bytes(payload) != snapshot["manifestSha256"]
                    or not isinstance(manifest.get("predecessor"), dict)
                    or any(snapshot["predecessor"].get(key) != value
                           for key, value in manifest["predecessor"].items())):
                raise ActivationError("terminal activation snapshot provenance drift")
        if late_verification_required:
            assert backend is not None
            backend.bind_bootstrap_receipt(
                OPERATOR.require_string("runtime.gateway_label"),
                bootstrap_bindings[OPERATOR.require_string("runtime.gateway_label")],
                prior_gateway["loaded"],
            )
            backend.bind_bootstrap_receipt(
                OPERATOR.require_string("runtime.node_label"),
                bootstrap_bindings[OPERATOR.require_string("runtime.node_label")],
            )
            gateway = backend.verify(
                selected_path, selected_record["device"], selected_record["inode"]
            )
            node = backend.verify_node(
                selected_path, selected_record["device"], selected_record["inode"]
            )
            late_verification = {
                "gateway": gateway,
                "node": node,
                "commandEvidence": list(backend.command_evidence),
            }

        archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        archive_root_info = os.lstat(archive_root)
        if (not stat.S_ISDIR(archive_root_info.st_mode)
                or archive_root.resolve(strict=True) != archive_root
                or stat.S_IMODE(archive_root_info.st_mode) != 0o700
                or archive_root_info.st_dev != result_info.st_dev
                or (restore_info is not None
                    and restore_info.st_dev != result_info.st_dev)
                or archive_dir.exists() or archive_dir.is_symlink()):
            raise ActivationError("activation receipt archive root drift")
        archive_dir.mkdir(mode=0o700)
        os.chmod(archive_dir, 0o700)
        archived_result = archive_dir / paths.result.name
        archived_fence = archive_dir / fence_path.name
        archived_restore = archive_dir / restore_path.name if restore_path else None
        moved_fence = False
        moved_result = False
        moved_restore = False
        retirement_path = archive_dir / RETIREMENT_RECEIPT_NAME
        committed = False
        try:
            os.rename(fence_path, archived_fence)
            moved_fence = True
            os.rename(paths.result, archived_result)
            moved_result = True
            if restore_path is not None and archived_restore is not None:
                os.rename(restore_path, archived_restore)
                moved_restore = True
            fsync_directory(paths.result.parent)
            fsync_directory(archive_dir)
            archived_result_payload, archived_result_info = read_physical(
                archived_result, "archived activation result"
            )
            archived_fence_payload, archived_fence_info = read_physical(
                archived_fence, "archived activation start fence"
            )
            archived_restore_payload: bytes | None = None
            archived_restore_info: os.stat_result | None = None
            if archived_restore is not None:
                archived_restore_payload, archived_restore_info = read_physical(
                    archived_restore, "archived activation restore result"
                )
            if (paths.result.exists() or paths.result.is_symlink()
                    or fence_path.exists() or fence_path.is_symlink()
                    or (restore_path is not None
                        and (restore_path.exists() or restore_path.is_symlink()))
                    or archived_result_payload != result_payload
                    or archived_fence_payload != fence_payload
                    or archived_restore_payload != restore_payload
                    or (archived_result_info.st_dev, archived_result_info.st_ino)
                    != (result_info.st_dev, result_info.st_ino)
                    or (archived_fence_info.st_dev, archived_fence_info.st_ino)
                    != (fence_info.st_dev, fence_info.st_ino)
                    or (archived_restore_info is not None and restore_info is not None
                        and (archived_restore_info.st_dev, archived_restore_info.st_ino)
                        != (restore_info.st_dev, restore_info.st_ino))):
                raise ActivationError("activation receipt retirement drift")
            receipt = {
                "schemaVersion": 1,
                "retiredAt": utc_now(),
                "outcome": retirement_outcome,
                "candidatePath": str(candidate_path),
                "candidateCommit": candidate.get("commit"),
                "selectedPath": str(selected_path),
                "releaseIdentities": release_identities,
                "result": {
                    "path": str(archived_result),
                    "sha256": sha256_bytes(result_payload),
                    "device": archived_result_info.st_dev,
                    "inode": archived_result_info.st_ino,
                    "volumeUuid": historical_volume_uuid(archived_result, archived_result_info),
                },
                "startFence": {
                    "path": str(archived_fence),
                    "sha256": sha256_bytes(fence_payload),
                    "device": archived_fence_info.st_dev,
                    "inode": archived_fence_info.st_ino,
                    "volumeUuid": historical_volume_uuid(archived_fence, archived_fence_info),
                },
            }
            if (archived_restore is not None and archived_restore_info is not None
                    and restore_payload is not None):
                receipt["restoreResult"] = {
                    "path": str(archived_restore),
                    "sha256": sha256_bytes(restore_payload),
                    "device": archived_restore_info.st_dev,
                    "inode": archived_restore_info.st_ino,
                    "volumeUuid": historical_volume_uuid(archived_restore, archived_restore_info),
                }
            # Stable history identifiers do not relax this operation's race fence.
            for identity in release_identities.values():
                release_path = Path(identity["path"])
                info = os.lstat(release_path)
                if ((info.st_dev, info.st_ino) != (identity["device"], identity["inode"])
                        or not historical_identity_matches(identity, release_path, info)):
                    raise ActivationError("terminal activation release changed during retirement")
            if not paths.current_link.is_symlink() or os.readlink(paths.current_link) != str(selected_path):
                raise ActivationError("terminal activation selector changed during retirement")
            if late_verification is not None:
                receipt["lateVerification"] = late_verification
            create_immutable_file(
                retirement_path,
                json.dumps(receipt, indent=2, sort_keys=True).encode() + b"\n",
            )
            retirement_payload, retirement_info = read_physical(
                retirement_path, "activation retirement receipt"
            )
            if stat.S_IMODE(retirement_info.st_mode) != 0o400:
                raise ActivationError("activation retirement receipt mode drift")
            committed = True
            return {
                **receipt,
                "retirementReceipt": {
                    "path": str(retirement_path),
                    "sha256": sha256_bytes(retirement_payload),
                    "mode": 0o400,
                },
            }
        except BaseException:
            if not committed:
                failed_receipt = archive_dir / f"failed-{RETIREMENT_RECEIPT_NAME}"
                if retirement_path.exists() and not failed_receipt.exists():
                    os.rename(retirement_path, failed_receipt)
                if (moved_restore and archived_restore is not None
                        and restore_path is not None and archived_restore.exists()
                        and not restore_path.exists()):
                    os.rename(archived_restore, restore_path)
                if moved_result and archived_result.exists() and not paths.result.exists():
                    os.rename(archived_result, paths.result)
                if moved_fence and archived_fence.exists() and not fence_path.exists():
                    os.rename(archived_fence, fence_path)
                fsync_directory(paths.result.parent)
                fsync_directory(archive_dir)
            raise
    finally:
        _release_lock(descriptor)


def restore_failed_activation(
    paths: ActivationPaths,
    candidate: Path,
    expected_commit: str,
    candidate_seal: Path,
    snapshot_manifest: Path,
    output: Path,
    backend: SystemBackend,
    *, after_success: bool = False,
) -> dict[str, Any]:
    if not output.is_absolute() or output.parent != paths.result.parent:
        raise ActivationError("activation restore output binding drift")
    expected_commit = require_commit(expected_commit, "candidate commit")
    descriptor = _acquire_lock(paths)
    try:
        if output.exists() or output.is_symlink():
            raise ActivationError("activation restore result already exists")
        candidate_record = validate_candidate_seal(
            paths, candidate, expected_commit, candidate_seal
        )
        snapshot = load_stopped_snapshot(
            paths,
            snapshot_manifest,
            candidate_record,
            verify_predecessor_selector=False,
        )
        expected_output_name = (
            f"activation-restore-result-{snapshot['manifestSha256'][:16]}.json"
        )
        if output.name != expected_output_name:
            raise ActivationError("activation restore output binding drift")
        result, _result_payload, result_info = read_json(
            paths.result, "failed activation result"
        )
        fence_path = _start_fence_path(paths)
        fence, fence_payload, fence_info = read_json(
            fence_path, "failed activation start fence"
        )
        cross_version = candidate_record["packageVersion"] != snapshot["predecessor"]["packageVersion"]
        successful_upgrade = (
            after_success and cross_version and result.get("outcome") == "activated"
            and result.get("restoreRequired") is False
        )
        failed_activation = (
            not after_success and result.get("outcome") == "snapshot_restore_required"
            and result.get("restoreRequired") is True
        )
        if (stat.S_IMODE(result_info.st_mode) != 0o400
                or stat.S_IMODE(fence_info.st_mode) != 0o400
                or not (failed_activation or successful_upgrade)
                or fence.get("upgrade") != result.get("upgrade")
                or cross_version != isinstance(result.get("upgrade"), dict)
                or result.get("candidate") != candidate_record
                or result.get("snapshot") != snapshot
                or result.get("startConsumption", {}).get("path") != str(fence_path)
                or result.get("startConsumption", {}).get("sha256")
                != sha256_bytes(fence_payload)
                or fence.get("schemaVersion") != 1
                or fence.get("terminalResultPath") != str(paths.result)
                or fence.get("candidateSealPath") != candidate_record["sealPath"]
                or fence.get("candidateSealSha256") != candidate_record["sealSha256"]
                or fence.get("snapshotManifestPath") != snapshot["manifestPath"]
                or fence.get("snapshotManifestSha256") != snapshot["manifestSha256"]):
            raise ActivationError("failed activation restore binding drift")

        predecessor = snapshot["predecessor"]

        def terminal(
            outcome: str,
            error: str | None,
            *,
            restore_applied: bool,
            quarantines: tuple[Path, ...] = (),
            gateway: dict[str, Any] | None = None,
            node: dict[str, Any] | None = None,
            preimage_restored: bool | None = None,
        ) -> dict[str, Any]:
            receipt = {
                "schemaVersion": 1,
                "finishedAt": utc_now(),
                "outcome": outcome,
                "error": error,
                "restoreApplied": restore_applied,
                "failedActivationResultPath": str(paths.result),
                "failedActivationResultSha256": sha256_bytes(_result_payload),
                "candidate": candidate_record,
                "snapshot": snapshot,
                "rollback": predecessor,
                "quarantinedFailedSurfaces": [str(path) for path in quarantines],
                "preimageRestored": preimage_restored,
                "verification": {"gateway": gateway, "node": node},
                "bootstrapBindings": backend.bootstrap_bindings(),
                "commandEvidence": list(backend.command_evidence),
            }
            if cross_version:
                receipt["afterSuccessfulUpgrade"] = successful_upgrade
            create_immutable_file(
                output, json.dumps(receipt, indent=2, sort_keys=True).encode() + b"\n"
            )
            return receipt

        live_paths = (
            paths.predecessor_state_dir,
            paths.gateway_plist,
            paths.node_plist,
            paths.current_link,
        )
        suffix = expected_commit[:12]
        quarantines = tuple(
            item.with_name(f"{item.name}.failed-{suffix}") for item in live_paths
        )
        partials = tuple(
            item.with_name(f"{item.name}.partial-{suffix}") for item in live_paths
        )
        lawful_selector_targets = {
            str(candidate_record["path"]),
            str(predecessor["path"]),
        }

        def observe_surfaces(surface_paths: tuple[Path, ...]) -> tuple[dict[str, Any], ...]:
            observed: list[dict[str, Any]] = []
            for index, path in enumerate(surface_paths):
                try:
                    info = os.lstat(path)
                except OSError as exc:
                    raise ActivationError("activation restore live surface unavailable") from exc
                item: dict[str, Any] = {
                    "path": str(path),
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "mode": stat.S_IMODE(info.st_mode),
                    "nlink": info.st_nlink,
                }
                if index == 0:
                    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
                        raise ActivationError("activation restore state root drift")
                    item["kind"] = "directory"
                elif index in (1, 2):
                    payload, physical_info = read_physical(
                        path, "activation restore file surface"
                    )
                    if (not stat.S_ISREG(info.st_mode)
                            or (physical_info.st_dev, physical_info.st_ino)
                            != (info.st_dev, info.st_ino)):
                        raise ActivationError("activation restore file surface drift")
                    item.update({
                        "kind": "file",
                        "sha256": sha256_bytes(payload),
                        "bytes": len(payload),
                    })
                else:
                    if not path.is_symlink():
                        raise ActivationError("activation restore selector drift")
                    target = os.readlink(path)
                    if target not in lawful_selector_targets:
                        raise ActivationError("activation restore selector target drift")
                    item.update({"kind": "symlink", "target": target})
                observed.append(item)
            return tuple(observed)

        def observe_live_preimage() -> tuple[
            tuple[dict[str, Any], ...], dict[str, Any], dict[str, Any]
        ]:
            if cross_version:
                # A failed migration may leave mixed schemas or invalid config.
                # Preserve the complete physical preimage by rename; requiring
                # either runtime to parse it would make recovery impossible.
                return observe_surfaces(live_paths), {}, {}
            return (
                observe_surfaces(live_paths),
                validate_recovered_invariants(paths),
                backend.inspect_exec_approvals(candidate),
            )

        # No live preimage, including approvals state, is read until the caller's
        # stopped seam is independently re-proven inside this locked command.
        backend.assert_gateway_and_node_stopped()
        if any(path.exists() or path.is_symlink() for path in (*quarantines, *partials)):
            raise ActivationError("activation restore quarantine collision")
        preimage_surfaces, preimage_invariants, preimage_approvals = (
            observe_live_preimage()
        )

        moved_count = 0
        restore_applied = False
        post_extract_stopped = False
        try:
            for path, quarantine in zip(live_paths, quarantines):
                os.rename(path, quarantine)
                moved_count += 1
                fsync_directory(path.parent)
            extracted = run_bounded(
                (
                    "/usr/bin/tar", "--xattrs", "--acls", "--fflags", "-xpf",
                    snapshot["tarPath"], "-C", "/",
                ),
                SNAPSHOT_TIMEOUT_SECONDS,
            )
            backend.command_evidence.append(
                extracted.evidence("stopped_snapshot_restore")
            )
            if extracted.returncode != 0 or extracted.timed_out:
                raise ActivationError("stopped snapshot extraction failed")
            backend.assert_gateway_and_node_stopped()
            post_extract_stopped = True
            restored_snapshot = load_stopped_snapshot(
                paths,
                snapshot_manifest,
                candidate_record,
            )
            validate_support_links(paths)
            restored_invariants = validate_recovered_invariants(paths)
            restored_plists = validate_plists(paths)
            restored_approvals = backend.inspect_exec_approvals(
                stopped_inspection_release(candidate_record, predecessor)
            )
            if (
                restored_snapshot != snapshot
                or recovered_invariants_content_view(restored_invariants)
                != recovered_invariants_content_view(snapshot["recoveredInvariants"])
                or restored_approvals != snapshot["execApprovals"]
                or any(
                    restored_plists[label]["sha256"]
                    != snapshot[f"{label}PlistSha256"]
                    for label in ("gateway", "node")
                )
            ):
                raise ActivationError("restored snapshot binding drift")
            restore_applied = True
        except BaseException:
            cleanup_failed = False
            if not post_extract_stopped:
                try:
                    backend.assert_gateway_and_node_stopped()
                    post_extract_stopped = True
                except BaseException:
                    cleanup_failed = True
            if post_extract_stopped:
                for index in range(moved_count - 1, -1, -1):
                    live = live_paths[index]
                    quarantine = quarantines[index]
                    partial = partials[index]
                    try:
                        if live.exists() or live.is_symlink():
                            os.rename(live, partial)
                        if quarantine.exists() or quarantine.is_symlink():
                            os.rename(quarantine, live)
                        fsync_directory(live.parent)
                    except BaseException:
                        cleanup_failed = True
            preimage_confirmed = False
            if not cleanup_failed:
                try:
                    backend.assert_gateway_and_node_stopped()
                    observed_surfaces, observed_invariants, observed_approvals = (
                        observe_live_preimage()
                    )
                    preimage_confirmed = (
                        observed_surfaces == preimage_surfaces
                        and observed_invariants == preimage_invariants
                        and observed_approvals == preimage_approvals
                    )
                except BaseException:
                    preimage_confirmed = False
            return terminal(
                (
                    "restore_failed_preimage_restored"
                    if preimage_confirmed
                    else "restore_failed_preimage_unconfirmed"
                ),
                (
                    "snapshot_restore_failed"
                    if preimage_confirmed
                    else "snapshot_restore_failed_preimage_unconfirmed"
                ),
                restore_applied=False,
                preimage_restored=preimage_confirmed,
            )

        release = Path(predecessor["path"])
        outcome = "restored"
        error = None
        gateway: dict[str, Any] | None = None
        node: dict[str, Any] | None = None
        if cross_version and (successful_upgrade or result.get("firstBootAttempted") is not False):
            # Restoring an older queue can resurrect effects completed since the
            # snapshot. Quarantine keeps the evidence; it does not make replay
            # safe. The operator reconciles those effects before a native start.
            return terminal(
                "restored_stopped", None, restore_applied=True, quarantines=quarantines,
            )
        try:
            backend.bootstrap_gateway_once()
            gateway = backend.verify(
                release, predecessor["device"], predecessor["inode"]
            )
            backend.bootstrap_node_once()
            node = backend.verify_node(
                release, predecessor["device"], predecessor["inode"]
            )
        except BaseException:
            outcome = "restored_boot_failed"
            error = "predecessor_boot_failed"
        return terminal(
            outcome,
            error,
            restore_applied=restore_applied,
            quarantines=quarantines,
            gateway=gateway,
            node=node,
        )
    finally:
        _release_lock(descriptor)


def _fence_view(path: Path, bindings: dict[str, Any]) -> dict[str, Any]:
    value, payload, info = read_json(path, "activation start fence")
    if (
        {key: item for key, item in value.items() if key != "consumedAt"}
        != {key: item for key, item in bindings.items() if key != "consumedAt"}
        or not isinstance(value.get("consumedAt"), str)
        or stat.S_IMODE(info.st_mode) != 0o400
    ):
        raise ActivationError("activation start fence binding drift")
    return {"path": str(path), "sha256": sha256_bytes(payload), "mode": 0o400,
            "nlink": 1, "consumedAt": value["consumedAt"]}


def _result_base() -> dict[str, Any]:
    return {"schemaVersion": 1, "statesVisited": ["preflight"], "outcome": None,
            "candidateAttemptCount": 0, "rollbackAttemptCount": 0,
            "firstBootAttempted": False, "restoreRequired": False,
            "candidate": None, "rollback": None, "snapshot": None,
            "startConsumption": None, "verification": None, "commandEvidence": [],
            "error": None, "startedAt": utc_now(), "finishedAt": None}


def _terminal_result(
    result: dict[str, Any],
    backend: ActivationBackend,
    outcome: str,
    error: str | None,
) -> dict[str, Any]:
    return {**result, "statesVisited": [*result["statesVisited"], "terminal"],
            "outcome": outcome, "commandEvidence": list(backend.command_evidence),
            "error": error, "finishedAt": utc_now()}


def _finish(paths: ActivationPaths, result: dict[str, Any], backend: ActivationBackend, outcome: str, error: str | None) -> dict[str, Any]:
    terminal = _terminal_result(result, backend, outcome, error)
    create_immutable_file(paths.result, json.dumps(terminal, indent=2, sort_keys=True).encode() + b"\n")
    return terminal


def activate(paths: ActivationPaths, candidate: Path, expected_commit: str,
             candidate_seal: Path, snapshot_manifest: Path,
             backend: ActivationBackend, *, upgrade_config: Path | None = None) -> dict[str, Any]:
    expected_commit = require_commit(expected_commit, "candidate commit")
    result = _result_base()
    descriptor = _acquire_lock(paths)
    try:
        if paths.result.exists() or paths.result.is_symlink():
            existing, _, existing_info = read_json(paths.result, "activation result")
            if stat.S_IMODE(existing_info.st_mode) != 0o400:
                raise ActivationError("existing activation result binding drift")
            candidate_record = validate_candidate_seal(paths, candidate, expected_commit, candidate_seal)
            snapshot = load_stopped_snapshot(
                paths,
                snapshot_manifest,
                candidate_record,
                verify_predecessor_selector=False,
            )
            upgrade = resolve_upgrade_config(candidate_record, snapshot, upgrade_config)
            if existing.get("candidate") != candidate_record or existing.get("snapshot") != snapshot:
                raise ActivationError("existing activation result binding drift")
            if existing.get("upgrade") != upgrade:
                raise ActivationError("existing activation upgrade binding drift")
            if existing.get("outcome") == "activated":
                verification = existing.get("verification")
                gateway_loaded = (
                    verification.get("loaded")
                    if isinstance(verification, dict)
                    else None
                )
                node_loaded = (
                    verification.get("node")
                    if isinstance(verification, dict)
                    else None
                )
                if (
                    not isinstance(gateway_loaded, dict)
                    or not isinstance(gateway_loaded.get("bootstrapBinding"), dict)
                    or not isinstance(node_loaded, dict)
                    or not isinstance(node_loaded.get("bootstrapBinding"), dict)
                ):
                    raise ActivationError("activated result verification binding drift")
                if not paths.current_link.is_symlink() or os.readlink(paths.current_link) != str(candidate):
                    raise ActivationError("activated result no longer matches selector")
                if validate_recovered_invariants(paths) != activation_invariants(snapshot, upgrade):
                    raise ActivationError("activated result protected invariant drift")
                plists = validate_plists(paths)
                if any(
                    plists[label]["sha256"] != snapshot[f"{label}PlistSha256"]
                    for label in ("gateway", "node")
                ):
                    raise ActivationError("activated result protected invariant drift")
                approvals = backend.inspect_exec_approvals(candidate)
                if any(
                    approvals[field] != snapshot["execApprovals"][field]
                    for field in EXEC_APPROVALS_SEMANTIC_BINDING_KEYS
                ):
                    raise ActivationError("activated result exec approvals semantic drift")
                backend.bind_bootstrap_receipt(
                    OPERATOR.require_string("runtime.gateway_label"),
                    gateway_loaded["bootstrapBinding"],
                    gateway_loaded,
                )
                backend.bind_bootstrap_receipt(
                    OPERATOR.require_string("runtime.node_label"),
                    node_loaded["bootstrapBinding"],
                    node_loaded,
                )
                backend.verify(candidate, candidate_record["device"], candidate_record["inode"])
                backend.verify_node(candidate, candidate_record["device"], candidate_record["inode"])
                backend.verify_screen_capture_continuity()
            return existing
        fence_path = _start_fence_path(paths)
        if fence_path.exists() or fence_path.is_symlink():
            candidate_record = validate_candidate_seal(
                paths, candidate, expected_commit, candidate_seal
            )
            snapshot = load_stopped_snapshot(
                paths,
                snapshot_manifest,
                candidate_record,
                verify_predecessor_selector=False,
            )
            upgrade = resolve_upgrade_config(candidate_record, snapshot, upgrade_config)
            result.update(
                {
                    "candidate": candidate_record,
                    "rollback": snapshot["predecessor"],
                    "snapshot": snapshot,
                    "candidateAttemptCount": 1,
                    "firstBootAttempted": True,
                    "restoreRequired": True,
                }
            )
            bindings = {
                "schemaVersion": 1,
                "candidateSealPath": str(candidate_seal),
                "candidateSealSha256": candidate_record["sealSha256"],
                "snapshotManifestPath": str(snapshot_manifest),
                "snapshotManifestSha256": snapshot["manifestSha256"],
                "terminalResultPath": str(paths.result),
                "consumedAt": "validated-from-fence",
            }
            if upgrade is not None:
                bindings["upgrade"] = upgrade
                result["upgrade"] = upgrade
                # A crash after consumption cannot establish whether bootstrap
                # occurred. Restoration must not assume there were no effects.
                result["firstBootAttempted"] = None
            result["startConsumption"] = _fence_view(fence_path, bindings)
            return _finish(
                paths,
                result,
                backend,
                "snapshot_restore_required",
                "start_already_consumed",
            )
        try:
            candidate_record = validate_candidate_seal(paths, candidate, expected_commit, candidate_seal)
            snapshot = load_stopped_snapshot(
                paths,
                snapshot_manifest,
                candidate_record,
                verify_predecessor_selector=False,
            )
            predecessor = snapshot["predecessor"]
            result.update({"candidate": candidate_record, "rollback": predecessor, "snapshot": snapshot})
            upgrade = resolve_upgrade_config(candidate_record, snapshot, upgrade_config)
            if upgrade is not None:
                result["upgrade"] = upgrade
                native_migration_schema_versions(candidate)
            backend.assert_gateway_and_node_stopped()
            if (
                not paths.current_link.is_symlink()
                or os.readlink(paths.current_link) != predecessor["path"]
            ):
                raise ActivationError("stopped snapshot preimage drift")
            approvals = backend.inspect_exec_approvals(stopped_inspection_release(candidate_record, predecessor))
            if approvals != snapshot["execApprovals"]:
                raise ActivationError("stopped exec approvals raw CAS drift")
            validate_support_links(paths)
            plists = validate_plists(paths)
            if any(
                plists[label]["sha256"] != snapshot[f"{label}PlistSha256"]
                for label in ("gateway", "node")
            ):
                raise ActivationError("protected plist drift")
            if validate_recovered_invariants(paths) != snapshot["recoveredInvariants"]:
                raise ActivationError("protected invariant drift")
            screen_capture = backend.verify_screen_capture_continuity()
            if screen_capture is not None:
                result["screenCapturePreflight"] = screen_capture
            plugin_selection = (
                backend.verify_candidate_discord(candidate, paths.candidate_state_dir / "openclaw.json")
                if upgrade is None else None
            )
        except BaseException:
            # A rejected preflight has consumed no activation authority. Return a
            # typed result to the caller, but reserve the fixed immutable result
            # namespace for attempts that actually consumed the start fence.
            return _terminal_result(result, backend, "failed_before_apply", "preflight_failed")
        bindings = {"schemaVersion": 1, "candidateSealPath": str(candidate_seal),
                    "candidateSealSha256": candidate_record["sealSha256"],
                    "snapshotManifestPath": str(snapshot_manifest),
                    "snapshotManifestSha256": snapshot["manifestSha256"],
                    "terminalResultPath": str(paths.result), "consumedAt": utc_now()}
        if upgrade is not None:
            bindings["upgrade"] = upgrade
        create_immutable_file(fence_path, json.dumps(bindings, indent=2, sort_keys=True).encode() + b"\n")
        result.update({"startConsumption": _fence_view(fence_path, bindings),
                       "candidateAttemptCount": 1, "firstBootAttempted": upgrade is None})
        result["statesVisited"].append("apply")
        try:
            if upgrade is not None:
                install_upgrade_config(paths, upgrade)
                result["migrationAttempted"] = True
                result["migration"] = backend.migrate_state_once(candidate)
                backend.assert_gateway_and_node_stopped()
                migrated_approvals = backend.inspect_exec_approvals(candidate)
                migrated_plists = validate_plists(paths)
                if (
                    validate_recovered_invariants(paths) != activation_invariants(snapshot, upgrade)
                    or any(migrated_approvals[key] != snapshot["execApprovals"][key]
                           for key in EXEC_APPROVALS_SEMANTIC_BINDING_KEYS)
                    or any(migrated_plists[label]["sha256"] != snapshot[f"{label}PlistSha256"]
                           for label in ("gateway", "node"))
                ):
                    raise ActivationError("post-migration protected invariant drift")
                plugin_selection = backend.verify_candidate_discord(
                    candidate, paths.candidate_state_dir / "openclaw.json"
                )
            atomic_symlink(paths.current_link, candidate)
            result["firstBootAttempted"] = True
            backend.bootstrap_gateway_once()
            result["statesVisited"].append("verify")
            gateway = backend.verify(candidate, candidate_record["device"], candidate_record["inode"])
            backend.bootstrap_node_once()
            node = backend.verify_node(candidate, candidate_record["device"], candidate_record["inode"])
            invariants = validate_recovered_invariants(paths)
            plists = validate_plists(paths)
            approvals = backend.inspect_exec_approvals(candidate)
            extensions = validate_bundled_extensions(candidate)
            if (
                invariants != activation_invariants(snapshot, upgrade)
                or any(
                    approvals[field] != snapshot["execApprovals"][field]
                    for field in EXEC_APPROVALS_SEMANTIC_BINDING_KEYS
                )
                or any(
                    plists[label]["sha256"]
                    != snapshot[f"{label}PlistSha256"]
                    for label in ("gateway", "node")
                )
            ):
                raise ActivationError("post-boot protected invariant drift")
            screen_capture_after = backend.verify_screen_capture_continuity()
            if ((screen_capture is None) != (screen_capture_after is None)
                    or (screen_capture is not None and screen_capture_after["bindingSha256"]
                        != screen_capture["bindingSha256"])):
                raise ActivationError("ScreenCapture continuity binding changed during activation")
            result["verification"] = {**gateway, "node": node,
                                      "recoveredInvariants": invariants,
                                      "execApprovals": approvals,
                                      "bundledExtensions": extensions,
                                      "pluginSelection": plugin_selection,
                                      "persistedSurface": True}
            if screen_capture_after is not None:
                result["verification"]["screenCaptureContinuity"] = screen_capture_after
            return _finish(paths, result, backend, "activated", None)
        except BaseException:
            result["restoreRequired"] = True
            return _finish(paths, result, backend, "snapshot_restore_required", "activation_failed")
    finally:
        _release_lock(descriptor)


def live_paths() -> ActivationPaths:
    account = pwd.getpwnam(OPERATOR.require_string("identifiers.host_user"))
    if OPERATOR.require_path("paths.host_home").resolve() != Path(account.pw_dir).resolve():
        raise ActivationError("operator home does not match the configured account")
    uid = account.pw_uid
    return ActivationPaths(OPERATOR.require_path("paths.runtime_releases_root"), OPERATOR.require_path("paths.runtime_current_link"), OPERATOR.require_path("paths.runtime_package_link"),
                           OPERATOR.require_path("paths.openclaw_cli"), OPERATOR.require_path("paths.gateway_plist"), OPERATOR.require_path("paths.node_plist"),
                           OPERATOR.require_path("paths.node_binary"), OPERATOR.require_path("paths.runtime_node_alias"), OPERATOR.require_path("paths.state_root"),
                           OPERATOR.require_path("paths.state_root"), OPERATOR.require_path("paths.activation_lock"), OPERATOR.require_path("paths.activation_result"), uid,
                           screen_capture_binding=configured_screen_capture_binding())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    seal = modes.add_parser("seal")
    seal.add_argument("--candidate-release", type=Path, required=True)
    seal.add_argument("--source-commit", required=True)
    seal.add_argument("--output", type=Path, required=True)
    snapshot = modes.add_parser("snapshot")
    snapshot.add_argument("--output-root", type=Path, required=True)
    snapshot.add_argument("--manifest", type=Path, required=True)
    snapshot.add_argument("--candidate-release", type=Path, required=True)
    snapshot.add_argument("--expected-commit", required=True)
    snapshot.add_argument("--candidate-seal", type=Path, required=True)
    retire = modes.add_parser("retire-receipts")
    retire.add_argument("--archive-dir", type=Path, required=True)
    restore = modes.add_parser("restore")
    restore.add_argument("--candidate-release", type=Path, required=True)
    restore.add_argument("--expected-commit", required=True)
    restore.add_argument("--candidate-seal", type=Path, required=True)
    restore.add_argument("--stopped-snapshot-manifest", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    restore.add_argument("--after-success", action="store_true",
                         help="Explicit stopped recovery of a successful version upgrade; never boots the restored queue")
    screen_enroll = modes.add_parser("screen-capture-enroll",
                                    help="Bind an already verified native route receipt; never capture")
    screen_enroll.add_argument("--acceptance", type=Path, required=True)
    screen_enroll.add_argument("--acceptance-sha256", required=True)
    screen_enroll.add_argument("--permission-database", type=Path, required=True)
    screen_enroll.add_argument("--output", type=Path, required=True)
    screen_check = modes.add_parser("screen-capture-check", help="Read current identity and permission prerequisites")
    screen_check.add_argument("--binding", type=Path, required=True)
    screen_check.add_argument("--require-current-process", action="store_true")
    activation = modes.add_parser("activate")
    activation.add_argument("--candidate-release", type=Path, required=True)
    activation.add_argument("--expected-commit", required=True)
    activation.add_argument("--candidate-seal", type=Path, required=True)
    activation.add_argument("--stopped-snapshot-manifest", type=Path, required=True)
    activation.add_argument("--upgrade-config", type=Path,
                            help="Reviewed immutable target config; required only for a version upgrade")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        paths = live_paths()
        if os.geteuid() != paths.operator_uid:
            raise ActivationError("activation tooling must run as the operator user")
        if args.mode in ("seal", "snapshot", "activate", "restore") and not args.candidate_release.is_absolute():
            raise ActivationError("candidate release must be absolute")
        if args.mode == "screen-capture-enroll":
            receipt = enroll_screen_capture_binding(paths, args.acceptance, args.acceptance_sha256,
                                                   args.permission_database, args.output)
        elif args.mode == "screen-capture-check":
            receipt = verify_screen_capture_binding(args.binding, paths.node,
                                                    require_current_process=args.require_current_process)
        elif args.mode == "seal":
            receipt = create_candidate_seal(paths, args.candidate_release, args.source_commit, args.output)
        elif args.mode == "snapshot":
            receipt = create_stopped_snapshot(
                paths,
                args.output_root,
                args.manifest,
                args.candidate_release,
                args.expected_commit,
                args.candidate_seal,
                SystemBackend(paths),
            )
        elif args.mode == "retire-receipts":
            receipt = retire_terminal_receipts(
                paths, args.archive_dir, SystemBackend(paths)
            )
        elif args.mode == "restore":
            receipt = restore_failed_activation(
                paths, args.candidate_release, args.expected_commit,
                args.candidate_seal, args.stopped_snapshot_manifest,
                args.output, SystemBackend(paths), after_success=args.after_success,
            )
        else:
            receipt = activate(paths, args.candidate_release, args.expected_commit,
                               args.candidate_seal, args.stopped_snapshot_manifest,
                               SystemBackend(paths), upgrade_config=args.upgrade_config)
    except (ActivationError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if (
        args.mode in ("seal", "snapshot", "retire-receipts", "screen-capture-enroll", "screen-capture-check")
        or receipt["outcome"] in ("activated", "restored", "restored_stopped")
    ) else 3


if __name__ == "__main__":
    raise SystemExit(main())
