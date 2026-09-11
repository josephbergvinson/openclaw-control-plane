#!/usr/bin/env python3
"""Quarantine one abandoned weekly-backup staging generation truthfully.

Dry-run is the default. Apply never deletes data or edits the OpenClaw task
database: it writes immutable intent/completion evidence around one atomic,
same-volume rename out of the live weekly root.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Callable
import uuid

try:
    from . import openclaw_weekly_archive_backup as archive_backup
except ImportError:
    import openclaw_weekly_archive_backup as archive_backup


INTENT_SCHEMA = "openclaw.weekly_backup.reconciliation_intent.v1"
COMPLETION_SCHEMA = "openclaw.weekly_backup.reconciliation_receipt.v1"
FAILURE_SCHEMA = "openclaw.weekly_backup.reconciliation_failure.v1"
PREVIEW_SCHEMA = "openclaw.weekly_backup.reconciliation_preview.v1"
PROCESS_RECEIPT_SCHEMA = "openclaw.cron_python_entrypoint.process_receipt.v2"
PROCESS_RECEIPT_WHAT = (
    "run bounded three-archive OWC weekly OpenClaw recovery set"
)
CORRECTED_OUTCOME = "failed_not_published"
QUARANTINE_NAME_RE = re.compile(
    r"^\.openclaw-archive-v[34]-\d{8}T\d{6}Z\.incomplete-\d+"
    r"\.quarantine-[0-9a-f]{32}$"
)
NATIVE_PUBLICATION_NAME_RE = re.compile(
    r"^\.openclaw-backup-publish-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}"
    r"-[89ab][0-9a-f]{3}-[0-9a-f]{12}-[A-Za-z0-9]{6}$"
)


class ReconciliationError(RuntimeError):
    """An identity, liveness, evidence, or mutation invariant failed."""


@dataclass(frozen=True)
class ReconciliationConfig:
    weekly_root: Path
    staging: Path
    quarantine_root: Path
    receipt_dir: Path
    phase_receipt: Path
    process_receipt: Path
    producer_script: Path
    openclaw_cli: Path
    task_id: str
    task_run_id: str
    job_id: str


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReconciliationError(message)


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


def normalized_absolute(path: Path, label: str) -> Path:
    require(path.is_absolute(), "{} must be absolute".format(label))
    normalized = Path(os.path.abspath(path))
    require(path == normalized, "{} must not contain dot segments".format(label))
    return normalized


def load_physical_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    normalized_absolute(path, label)
    before = path.lstat()
    require(
        stat.S_ISREG(before.st_mode),
        "{} is not a physical regular file".format(label),
    )
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        require(
            stat.S_ISREG(opened.st_mode)
            and (opened.st_dev, opened.st_ino)
            == (before.st_dev, before.st_ino),
            "{} identity changed between lstat and open".format(label),
        )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read()
        after = os.fstat(descriptor)
        require(
            (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            == (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ),
            "{} changed while it was read".format(label),
        )
        rebound = path.lstat()
        require(
            (rebound.st_dev, rebound.st_ino)
            == (opened.st_dev, opened.st_ino),
            "{} path changed while it was read".format(label),
        )
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationError("{} is not valid JSON".format(label)) from exc
    require(isinstance(payload, dict), "{} JSON must be an object".format(label))
    return payload, sha256_bytes(raw)


def read_task_via_cli(openclaw_cli: Path, task_id: str) -> dict[str, Any]:
    normalized_absolute(openclaw_cli, "OpenClaw CLI")
    resolved_cli = openclaw_cli.resolve(strict=True)
    info = resolved_cli.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and os.access(openclaw_cli, os.X_OK),
        "OpenClaw CLI does not resolve to an executable physical file",
    )
    completed = subprocess.run(
        [str(openclaw_cli), "tasks", "show", task_id, "--json"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    require(
        completed.returncode == 0,
        "OpenClaw task inspection failed with exit {}".format(
            completed.returncode
        ),
    )
    streams = [
        (name, raw.strip())
        for name, raw in (
            ("stdout", completed.stdout),
            ("stderr", completed.stderr),
        )
        if raw.strip()
    ]
    require(
        len(streams) == 1,
        "OpenClaw task inspection did not return exactly one JSON stream",
    )
    try:
        payload = json.loads(streams[0][1])
    except json.JSONDecodeError as exc:
        raise ReconciliationError(
            "OpenClaw task inspection {} did not return JSON".format(
                streams[0][0]
            )
        ) from exc
    require(isinstance(payload, dict), "OpenClaw task JSON must be an object")
    return payload


def parse_utc(value: Any, label: str) -> datetime:
    require(isinstance(value, str), "{} timestamp is missing".format(label))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReconciliationError("{} timestamp is invalid".format(label)) from exc
    require(parsed.tzinfo is not None, "{} timestamp has no timezone".format(label))
    return parsed.astimezone(timezone.utc)


def integer(value: Any, label: str, *, positive: bool = False) -> int:
    require(
        isinstance(value, int) and not isinstance(value, bool),
        "{} must be an integer".format(label),
    )
    if positive:
        require(value > 0, "{} must be positive".format(label))
    return value


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def validate_phase_receipt(
    payload: dict[str, Any],
    *,
    run_id: str,
    child_pid: int,
    final: Path,
) -> None:
    require(
        payload.get("schema_version") == archive_backup.PHASE_RECEIPT_SCHEMA,
        "phase receipt schema mismatch",
    )
    require(payload.get("run_id") == run_id, "phase receipt run id mismatch")
    invocation_id = payload.get("invocation_id")
    require(
        isinstance(invocation_id, str)
        and re.fullmatch(
            re.escape(run_id) + r"-" + str(child_pid) + r"-[0-9a-f]{32}",
            invocation_id,
        )
        is not None,
        "phase receipt invocation identity mismatch",
    )
    require(payload.get("destination") == str(final), "phase destination mismatch")
    require(payload.get("status") == "in_progress", "phase receipt is not abandoned")
    require(payload.get("current_phase") != "terminal", "phase receipt is terminal")
    require(payload.get("completed_at_utc") is None, "phase receipt claims completion")
    require(payload.get("result") is None, "phase receipt claims a result")
    phases = payload.get("phases")
    require(isinstance(phases, list) and phases, "phase receipt has no verified phase")
    require(
        isinstance(phases[0], dict)
        and phases[0].get("name") == "preflight"
        and phases[0].get("status") == "verified",
        "phase receipt preflight proof is missing",
    )


def validate_process_receipt(
    payload: dict[str, Any],
    *,
    process_receipt_path: Path,
    producer_script: Path,
    child_pid: int,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    require(
        payload.get("schema_version") == PROCESS_RECEIPT_SCHEMA,
        "process receipt schema mismatch",
    )
    require(
        payload.get("receipt_owner") == "supervisor"
        and payload.get("what") == PROCESS_RECEIPT_WHAT,
        "process receipt owner or operation mismatch",
    )
    require(
        payload.get("requested_script") == str(producer_script)
        and payload.get("resolved_script") == str(producer_script),
        "process receipt producer script mismatch",
    )
    require(
        payload.get("status") == "terminated_parent_lost",
        "process receipt is not a parent-loss termination",
    )
    child = payload.get("child")
    supervisor = payload.get("supervisor")
    termination = payload.get("termination")
    residual = payload.get("residual_process")
    require(isinstance(child, dict), "process child identity is missing")
    require(isinstance(supervisor, dict), "process supervisor identity is missing")
    require(isinstance(termination, dict), "process termination proof is missing")
    require(isinstance(residual, dict), "process residual proof is missing")
    child_pgid = integer(child.get("process_group_id"), "child pgid", positive=True)
    supervisor_pid = integer(supervisor.get("pid"), "supervisor pid", positive=True)
    supervisor_pgid = integer(
        supervisor.get("process_group_id"), "supervisor pgid", positive=True
    )
    owner_match = re.fullmatch(
        r"cron-entrypoint-\d{8}T\d{12}Z-(\d+)\.json",
        process_receipt_path.name,
    )
    require(owner_match is not None, "process receipt owner filename is invalid")
    owner_pid = int(owner_match.group(1))
    require(child.get("pid") == child_pid, "process child pid mismatch")
    require(
        child.get("exit_code") == -15 and child.get("signal") == 15,
        "process child was not terminated by SIGTERM",
    )
    require(
        termination.get("requested") is True
        and termination.get("reason") == "parent_process_lost"
        and termination.get("term_sent") is True,
        "process parent-loss termination proof is incomplete",
    )
    require(
        residual.get("checked") is True
        and residual.get("process_group_alive_after_cleanup") is False,
        "process residual-group absence was not proved",
    )
    require(payload.get("business_effect") is None, "process receipt claims an effect")
    started_at = parse_utc(payload.get("started_at"), "process started_at")
    ended_at = parse_utc(payload.get("ended_at"), "process ended_at")
    require(ended_at >= started_at, "process receipt timestamps are reversed")
    return (owner_pid, child_pid, supervisor_pid), (child_pgid, supervisor_pgid)


def validate_task(
    payload: dict[str, Any],
    *,
    config: ReconciliationConfig,
    process_receipt: dict[str, Any],
) -> None:
    require(payload.get("taskId") == config.task_id, "task id mismatch")
    require(payload.get("runtime") == "cron", "task runtime is not cron")
    require(payload.get("sourceId") == config.job_id, "task job id mismatch")
    require(payload.get("runId") == config.task_run_id, "task run id mismatch")
    require(payload.get("status") == "succeeded", "task is not the false-success row")
    require(
        payload.get("terminalOutcome") in {None, "succeeded"},
        "task already carries a non-success semantic outcome",
    )
    task_end = integer(payload.get("endedAt"), "task endedAt", positive=True)
    process_end = parse_utc(process_receipt.get("ended_at"), "process ended_at")
    process_end_ms = int(process_end.timestamp() * 1000)
    require(
        process_end_ms <= task_end <= process_end_ms + 5 * 60_000,
        "task/process terminal timestamps do not reconcile",
    )


def tree_inventory(root: Path, expected_device: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs.sort(key=os.fsencode)
        files.sort(key=os.fsencode)
        for name in [*dirs, *files]:
            path = current_path / name
            info = path.lstat()
            require(
                info.st_dev == expected_device,
                "staging tree crossed a device boundary: {}".format(path),
            )
            relative = path.relative_to(root).as_posix()
            row: dict[str, Any] = {
                "path": relative,
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": stat.S_IMODE(info.st_mode),
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "type": (
                    "directory"
                    if stat.S_ISDIR(info.st_mode)
                    else "file"
                    if stat.S_ISREG(info.st_mode)
                    else "symlink"
                    if stat.S_ISLNK(info.st_mode)
                    else "unsupported"
                ),
            }
            require(row["type"] != "unsupported", "unsupported staging entry type")
            if row["type"] == "symlink":
                row["target"] = os.readlink(path)
            rows.append(row)
    return rows


def validate_staging_inventory(
    inventory: list[dict[str, Any]], *, staging_name: str,
) -> list[str]:
    top_level = {
        str(row["path"]): row
        for row in inventory
        if "/" not in str(row["path"])
    }
    allowed = set(archive_backup.EXPECTED_GENERATION_FILES) | {".restore-probe"}
    # Native publication uses this exact private directory and temporary file
    # beside the requested archive. An interrupted child may leave them behind;
    # retain them only inside the already validated v4 incomplete generation.
    native_publications = {
        name for name in top_level
        if staging_name.startswith(".openclaw-archive-v4-")
        and NATIVE_PUBLICATION_NAME_RE.fullmatch(name)
    }
    require(len(native_publications) <= 1, "multiple native publication directories")
    allowed |= native_publications
    require(top_level, "staging inventory is empty")
    require(
        set(top_level) <= allowed,
        "staging inventory contains an unknown top-level entry",
    )
    for name, row in top_level.items():
        expected_type = "directory" if name == ".restore-probe" or name in native_publications else "file"
        require(
            row.get("type") == expected_type,
            "staging inventory top-level type mismatch: {}".format(name),
        )
        if name in native_publications:
            require(row.get("mode") == 0o700, "native publication directory is not private")
    for row in inventory:
        owner, separator, descendant = str(row["path"]).partition("/")
        if owner in native_publications and separator:
            require(
                descendant == "archive.tar.gz.tmp"
                and row.get("type") == "file"
                and row.get("mode") == 0o600,
                "native publication residue has an unexpected member or type",
            )
    require(
        all(str(row["path"]).split("/", 1)[0] in top_level for row in inventory),
        "staging inventory contains an orphaned descendant",
    )
    return sorted(top_level)


def incomplete_children(
    weekly_root: Path,
    expected_device: int,
) -> list[dict[str, Any]]:
    before = weekly_root.lstat()
    require(
        stat.S_ISDIR(before.st_mode) and before.st_dev == expected_device,
        "weekly root is not a physical same-device directory",
    )
    descriptor = os.open(
        weekly_root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        require(
            stat.S_ISDIR(opened.st_mode)
            and (opened.st_dev, opened.st_ino)
            == (before.st_dev, before.st_ino),
            "weekly root identity changed before incomplete scan",
        )
        rows: list[dict[str, Any]] = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if archive_backup.INCOMPLETE_NAME_RE.fullmatch(entry.name) is None:
                    continue
                info = entry.stat(follow_symlinks=False)
                rows.append(
                    {
                        "name": entry.name,
                        "device": info.st_dev,
                        "inode": info.st_ino,
                        "type": (
                            "directory"
                            if stat.S_ISDIR(info.st_mode)
                            else "file"
                            if stat.S_ISREG(info.st_mode)
                            else "symlink"
                            if stat.S_ISLNK(info.st_mode)
                            else "unsupported"
                        ),
                    }
                )
        rebound = weekly_root.lstat()
        require(
            (rebound.st_dev, rebound.st_ino)
            == (opened.st_dev, opened.st_ino),
            "weekly root path changed during incomplete scan",
        )
        return sorted(rows, key=lambda row: str(row["name"]))
    finally:
        os.close(descriptor)


def require_sole_incomplete(
    config: ReconciliationConfig,
    staging_identity: tuple[int, int],
) -> list[dict[str, Any]]:
    rows = incomplete_children(config.weekly_root, staging_identity[0])
    require(
        len(rows) == 1
        and rows[0]["name"] == config.staging.name
        and rows[0]["type"] == "directory"
        and (rows[0]["device"], rows[0]["inode"]) == staging_identity,
        "target staging is not the sole incomplete generation: {}".format(
            ",".join(str(row["name"]) for row in rows) or "none"
        ),
    )
    return rows


def validate_paths(
    config: ReconciliationConfig,
) -> tuple[str, int, Path, os.stat_result]:
    for label, path in (
        ("weekly root", config.weekly_root),
        ("staging", config.staging),
        ("quarantine root", config.quarantine_root),
        ("receipt directory", config.receipt_dir),
        ("phase receipt", config.phase_receipt),
        ("process receipt", config.process_receipt),
        ("producer script", config.producer_script),
    ):
        normalized_absolute(path, label)
    weekly_info = config.weekly_root.lstat()
    staging_info = config.staging.lstat()
    producer_info = config.producer_script.lstat()
    require(
        stat.S_ISREG(producer_info.st_mode)
        and not config.producer_script.is_symlink(),
        "producer script is not a physical regular file",
    )
    uuid_pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    require(
        re.fullmatch(uuid_pattern, config.task_id) is not None,
        "task id is not a lowercase UUID",
    )
    require(
        re.fullmatch(uuid_pattern, config.job_id) is not None,
        "job id is not a lowercase UUID",
    )
    require(
        re.fullmatch(
            r"cron:" + re.escape(config.job_id) + r":\d+",
            config.task_run_id,
        )
        is not None,
        "task run id does not bind the cron job",
    )
    require(
        stat.S_ISDIR(weekly_info.st_mode) and not config.weekly_root.is_symlink(),
        "weekly root is not a physical directory",
    )
    require(
        config.staging.parent == config.weekly_root
        and stat.S_ISDIR(staging_info.st_mode)
        and not config.staging.is_symlink()
        and staging_info.st_dev == weekly_info.st_dev,
        "staging is not one physical same-device weekly-root child",
    )
    require(
        stat.S_IMODE(staging_info.st_mode) == 0o700,
        "staging directory mode is not 0700",
    )
    match = archive_backup.INCOMPLETE_NAME_RE.fullmatch(config.staging.name)
    require(
        match is not None,
        "staging name is not a supported incomplete archive generation",
    )
    run_id = match.group(1)
    pid_match = re.search(r"\.incomplete-(\d+)$", config.staging.name)
    require(pid_match is not None, "staging pid is missing")
    child_pid = int(pid_match.group(1))
    final = config.weekly_root / config.staging.name[1:].split(".incomplete-", 1)[0]
    require(not os.path.lexists(final), "published generation already exists")
    require(
        config.quarantine_root.parent == config.weekly_root.parent
        and config.quarantine_root != config.weekly_root,
        "quarantine root must be a distinct weekly-root sibling",
    )
    if os.path.lexists(config.quarantine_root):
        quarantine_info = config.quarantine_root.lstat()
        require(
            stat.S_ISDIR(quarantine_info.st_mode)
            and not config.quarantine_root.is_symlink()
            and quarantine_info.st_dev == weekly_info.st_dev,
            "quarantine root is not a physical same-device directory",
        )
    else:
        parent_info = config.quarantine_root.parent.lstat()
        require(
            stat.S_ISDIR(parent_info.st_mode)
            and not config.quarantine_root.parent.is_symlink()
            and parent_info.st_dev == weekly_info.st_dev,
            "quarantine parent is not a physical same-device directory",
        )
    return run_id, child_pid, final, staging_info


def acquire_existing_backup_lock(weekly_root: Path, expected_device: int) -> int:
    lock_path = weekly_root.parent / ".weekly-backup.lock"
    before = lock_path.lstat()
    require(
        stat.S_ISREG(before.st_mode) and before.st_dev == expected_device,
        "weekly backup lock is not a physical same-device file",
    )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(lock_path, flags)
    try:
        opened = os.fstat(descriptor)
        require(
            stat.S_ISREG(opened.st_mode)
            and opened.st_dev == expected_device
            and (opened.st_dev, opened.st_ino)
            == (before.st_dev, before.st_ino),
            "weekly backup lock identity changed between lstat and open",
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rebound = lock_path.lstat()
        require(
            (rebound.st_dev, rebound.st_ino)
            == (opened.st_dev, opened.st_ino),
            "weekly backup lock path changed after lock acquisition",
        )
    except BlockingIOError as exc:
        os.close(descriptor)
        raise ReconciliationError("weekly backup lock is held") from exc
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def ensure_private_directory(path: Path, expected_device: int) -> None:
    existed = os.path.lexists(path)
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    if not existed:
        os.chmod(path, 0o700)
        archive_backup.fsync_dir(path.parent)
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and not path.is_symlink()
        and info.st_dev == expected_device
        and stat.S_IMODE(info.st_mode) == 0o700,
        "private output directory invariant failed",
    )


def json_receipt_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def write_json_exclusive(path: Path, payload: dict[str, Any]) -> str:
    raw = json_receipt_bytes(payload)
    temporary = path.with_name(".{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        opened = os.fstat(descriptor)
        os.link(temporary, path, follow_symlinks=False)
        linked = path.lstat()
        require(
            stat.S_ISREG(linked.st_mode)
            and (linked.st_dev, linked.st_ino)
            == (opened.st_dev, opened.st_ino),
            "exclusive receipt target identity mismatch",
        )
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            readback = handle.read()
        require(readback == raw, "exclusive receipt readback mismatch")
        after = os.fstat(descriptor)
        require(
            (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            == (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ),
            "exclusive receipt changed during readback",
        )
        archive_backup.fsync_dir(path.parent)
        rebound = path.lstat()
        require(
            stat.S_ISREG(rebound.st_mode)
            and (rebound.st_dev, rebound.st_ino)
            == (opened.st_dev, opened.st_ino),
            "exclusive receipt target changed before durable readback",
        )
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return sha256_bytes(raw)


def observe_post_state(
    *,
    config: ReconciliationConfig,
    target: Path,
    final: Path,
    staging_identity: tuple[int, int],
) -> dict[str, Any]:
    observation_errors: list[str] = []

    def inspect(path: Path, label: str) -> tuple[bool | None, bool | None]:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False, False
        except Exception as exc:
            observation_errors.append(
                "{}: {}: {}".format(label, type(exc).__name__, exc)
            )
            return None, None
        return True, (
            stat.S_ISDIR(info.st_mode)
            and (info.st_dev, info.st_ino) == staging_identity
        )

    staging_present, staging_matches = inspect(config.staging, "staging")
    quarantine_present, quarantine_matches = inspect(target, "quarantine")
    final_present, final_matches = inspect(final, "published_destination")
    try:
        remaining_names: list[str] | None = [
            str(row["name"])
            for row in incomplete_children(
                config.weekly_root,
                staging_identity[0],
            )
        ]
    except Exception as exc:
        remaining_names = None
        observation_errors.append(
            "remaining_incomplete_staging: {}: {}".format(
                type(exc).__name__,
                exc,
            )
        )
    source_locations = [
        label
        for label, matches in (
            ("staging", staging_matches),
            ("quarantine", quarantine_matches),
            ("published_destination", final_matches),
        )
        if matches is True
    ]
    if source_locations:
        source_retained: bool | None = True
    elif all(
        matches is False
        for matches in (staging_matches, quarantine_matches, final_matches)
    ):
        source_retained = False
    else:
        source_retained = None
    return {
        "staging_present_after": staging_present,
        "staging_identity_matches_after": staging_matches,
        "quarantine_present_after": quarantine_present,
        "quarantine_identity_matches_after": quarantine_matches,
        "published_destination_present_after": final_present,
        "published_destination_identity_matches_after": final_matches,
        "remaining_incomplete_staging": remaining_names,
        "source_locations_after": source_locations,
        "source_retained": source_retained,
        "future_rerun_unblocked": (
            remaining_names == [] and final_present is False
        ),
        "post_state_observation_errors": observation_errors,
    }


def quarantine_staging(
    *,
    config: ReconciliationConfig,
    target: Path,
    final: Path,
    staging_identity: tuple[int, int],
) -> None:
    require(final.parent == config.weekly_root, "final destination parent mismatch")
    source_parent_before = config.weekly_root.lstat()
    target_parent_before = config.quarantine_root.lstat()
    source_parent_fd = os.open(
        config.weekly_root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        target_parent_fd = os.open(
            config.quarantine_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except Exception:
        os.close(source_parent_fd)
        raise
    try:
        source_parent_opened = os.fstat(source_parent_fd)
        target_parent_opened = os.fstat(target_parent_fd)
        require(
            stat.S_ISDIR(source_parent_opened.st_mode)
            and (source_parent_opened.st_dev, source_parent_opened.st_ino)
            == (source_parent_before.st_dev, source_parent_before.st_ino)
            and source_parent_opened.st_dev == staging_identity[0],
            "weekly source parent identity or device changed before quarantine",
        )
        require(
            stat.S_ISDIR(target_parent_opened.st_mode)
            and (target_parent_opened.st_dev, target_parent_opened.st_ino)
            == (target_parent_before.st_dev, target_parent_before.st_ino)
            and target_parent_opened.st_dev == staging_identity[0],
            "quarantine parent identity or device changed before quarantine",
        )
        current = os.stat(
            config.staging.name,
            dir_fd=source_parent_fd,
            follow_symlinks=False,
        )
        require(
            stat.S_ISDIR(current.st_mode)
            and (current.st_dev, current.st_ino) == staging_identity,
            "staging identity changed before quarantine",
        )
        try:
            os.stat(target.name, dir_fd=target_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ReconciliationError("quarantine target already exists")
        source_parent_rebound = config.weekly_root.lstat()
        target_parent_rebound = config.quarantine_root.lstat()
        require(
            (source_parent_rebound.st_dev, source_parent_rebound.st_ino)
            == (source_parent_opened.st_dev, source_parent_opened.st_ino)
            and (target_parent_rebound.st_dev, target_parent_rebound.st_ino)
            == (target_parent_opened.st_dev, target_parent_opened.st_ino),
            "quarantine parent path changed before atomic rename",
        )
        try:
            os.stat(
                final.name,
                dir_fd=source_parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise ReconciliationError(
                "published destination appeared before quarantine"
            )
        os.rename(
            config.staging.name,
            target.name,
            src_dir_fd=source_parent_fd,
            dst_dir_fd=target_parent_fd,
        )
        os.fsync(source_parent_fd)
        os.fsync(target_parent_fd)
    finally:
        os.close(target_parent_fd)
        os.close(source_parent_fd)


def reconcile(
    config: ReconciliationConfig,
    *,
    apply: bool,
    task_reader: Callable[[Path, str], dict[str, Any]] = read_task_via_cli,
    pid_alive: Callable[[int], bool] = process_exists,
    pgid_alive: Callable[[int], bool] = process_group_exists,
    reconciliation_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    run_id, child_pid, final, staging_info = validate_paths(config)
    phase_payload, phase_sha = load_physical_json(config.phase_receipt, "phase receipt")
    process_payload, process_sha = load_physical_json(
        config.process_receipt, "process receipt"
    )
    task_payload = task_reader(config.openclaw_cli, config.task_id)
    task_sha = sha256_bytes(canonical_json(task_payload))
    validate_phase_receipt(
        phase_payload,
        run_id=run_id,
        child_pid=child_pid,
        final=final,
    )
    pids, pgids = validate_process_receipt(
        process_payload,
        process_receipt_path=config.process_receipt,
        producer_script=config.producer_script,
        child_pid=child_pid,
    )
    validate_task(task_payload, config=config, process_receipt=process_payload)
    require(
        all(not pid_alive(pid) for pid in pids),
        "recorded process pid is still live",
    )
    require(
        all(not pgid_alive(pgid) for pgid in pgids),
        "recorded process group is still live",
    )

    lock_fd = acquire_existing_backup_lock(config.weekly_root, staging_info.st_dev)
    try:
        locked_run_id, locked_child_pid, locked_final, locked_staging_info = (
            validate_paths(config)
        )
        require(
            (locked_run_id, locked_child_pid, locked_final)
            == (run_id, child_pid, final)
            and (locked_staging_info.st_dev, locked_staging_info.st_ino)
            == (staging_info.st_dev, staging_info.st_ino),
            "staging identity changed before locked reconciliation",
        )
        require(
            all(not pid_alive(pid) for pid in pids),
            "recorded process pid became live before reconciliation",
        )
        require(
            all(not pgid_alive(pgid) for pgid in pgids),
            "recorded process group became live before reconciliation",
        )
        _, locked_phase_sha = load_physical_json(
            config.phase_receipt,
            "phase receipt",
        )
        _, locked_process_sha = load_physical_json(
            config.process_receipt,
            "process receipt",
        )
        locked_task_sha = sha256_bytes(
            canonical_json(task_reader(config.openclaw_cli, config.task_id))
        )
        require(
            (locked_phase_sha, locked_process_sha, locked_task_sha)
            == (phase_sha, process_sha, task_sha),
            "reconciliation evidence changed before locked reconciliation",
        )
        staging_identity = (staging_info.st_dev, staging_info.st_ino)
        incomplete_before = require_sole_incomplete(
            config,
            staging_identity,
        )
        inventory = tree_inventory(config.staging, staging_info.st_dev)
        staging_top_level = validate_staging_inventory(inventory, staging_name=config.staging.name)
        inventory_staging_info = config.staging.lstat()
        require(
            (inventory_staging_info.st_dev, inventory_staging_info.st_ino)
            == staging_identity,
            "staging identity changed during locked inventory",
        )
        inventory_sha = sha256_bytes(canonical_json(inventory))
        reconciliation_id = reconciliation_id or uuid.uuid4().hex
        require(
            re.fullmatch(r"[0-9a-f]{32}", reconciliation_id) is not None,
            "reconciliation id must be 32 lowercase hex characters",
        )
        target = config.quarantine_root / (
            config.staging.name + ".quarantine-" + reconciliation_id
        )
        require(
            QUARANTINE_NAME_RE.fullmatch(target.name) is not None,
            "unsafe quarantine target name",
        )
        evidence = {
            "phase_receipt": str(config.phase_receipt),
            "phase_receipt_sha256": phase_sha,
            "process_receipt": str(config.process_receipt),
            "process_receipt_sha256": process_sha,
            "task_evidence_sha256": task_sha,
            "staging_inventory_sha256": inventory_sha,
        }
        common = {
            "reconciliation_id": reconciliation_id,
            "observed_at_utc": utc_now(),
            "job_id": config.job_id,
            "task_id": config.task_id,
            "task_run_id": config.task_run_id,
            "backup_run_id": run_id,
            "staging": str(config.staging),
            "quarantine": str(target),
            "published_destination": str(final),
            "staging_identity": {
                "device": staging_info.st_dev,
                "inode": staging_info.st_ino,
            },
            "incomplete_staging_before": [
                str(row["name"]) for row in incomplete_before
            ],
            "staging_top_level_entries": staging_top_level,
            "evidence": evidence,
            "historical_task_observed_status": "succeeded",
            "corrected_outcome": CORRECTED_OUTCOME,
            "supersedes_historical_success_claim": True,
            "task_registry_mutated": False,
            "sqlite_mutated": False,
            "source_retained": True,
            "delete_performed": False,
        }
        preview = {
            "schema_version": PREVIEW_SCHEMA,
            "mode": "apply" if apply else "dry_run",
            "status": "validated",
            **common,
            "future_rerun_unblocked_after_apply": True,
        }
        if not apply:
            return 0, preview

        ensure_private_directory(config.quarantine_root, staging_info.st_dev)
        receipt_parent_info = config.receipt_dir.parent.lstat()
        require(
            stat.S_ISDIR(receipt_parent_info.st_mode)
            and not config.receipt_dir.parent.is_symlink(),
            "receipt parent is not a physical directory",
        )
        receipt_parent_device = receipt_parent_info.st_dev
        ensure_private_directory(config.receipt_dir, receipt_parent_device)
        intent_path = config.receipt_dir / (
            "weekly-backup-reconciliation-{}-intent.json".format(
                reconciliation_id
            )
        )
        completion_path = config.receipt_dir / (
            "weekly-backup-reconciliation-{}-completed.json".format(
                reconciliation_id
            )
        )
        failure_path = config.receipt_dir / (
            "weekly-backup-reconciliation-{}-failed.json".format(
                reconciliation_id
            )
        )
        require(
            not any(
                os.path.lexists(path)
                for path in (intent_path, completion_path, failure_path)
            ),
            "reconciliation receipt path already exists",
        )
        intent_payload = {
            "schema_version": INTENT_SCHEMA,
            "status": "intent_recorded",
            "recorded_at_utc": utc_now(),
            **common,
        }
        require_sole_incomplete(config, staging_identity)
        intent_sha = sha256_bytes(json_receipt_bytes(intent_payload))
        intent_published = False
        completion_payload: dict[str, Any] | None = None
        completion_expected_sha: str | None = None
        try:
            written_intent_sha = write_json_exclusive(
                intent_path,
                intent_payload,
            )
            require(
                written_intent_sha == intent_sha,
                "intent receipt hash readback mismatch",
            )
            intent_published = True
            quarantine_staging(
                config=config,
                target=target,
                final=final,
                staging_identity=staging_identity,
            )
            require(
                not os.path.lexists(config.staging),
                "staging remains after quarantine",
            )
            target_info = target.lstat()
            require(
                stat.S_ISDIR(target_info.st_mode)
                and not target.is_symlink()
                and (target_info.st_dev, target_info.st_ino)
                == (staging_info.st_dev, staging_info.st_ino),
                "quarantine identity readback mismatch",
            )
            require(
                tree_inventory(target, staging_info.st_dev) == inventory,
                "quarantine inventory readback mismatch",
            )
            remaining_incomplete = incomplete_children(
                config.weekly_root,
                staging_info.st_dev,
            )
            require(
                not remaining_incomplete,
                "weekly root still contains an incomplete generation after "
                "quarantine: {}".format(
                    ",".join(
                        str(row["name"])
                        for row in remaining_incomplete
                    )
                ),
            )
            require(not os.path.lexists(final), "published destination appeared")
            completion_payload = {
                "schema_version": COMPLETION_SCHEMA,
                "status": "completed",
                "completed_at_utc": utc_now(),
                **common,
                "intent_receipt": str(intent_path),
                "intent_receipt_sha256": intent_sha,
                "staging_present_after": False,
                "staging_identity_matches_after": False,
                "quarantine_present_after": True,
                "quarantine_identity_matches_after": True,
                "published_destination_present_after": False,
                "published_destination_identity_matches_after": False,
                "remaining_incomplete_staging": [],
                "source_locations_after": ["quarantine"],
                "future_rerun_unblocked": True,
                "post_state_observation_errors": [],
            }
            completion_expected_sha = sha256_bytes(
                json_receipt_bytes(completion_payload)
            )
            completion_sha = write_json_exclusive(
                completion_path, completion_payload
            )
            require(
                completion_sha == completion_expected_sha,
                "completion receipt hash readback mismatch",
            )
            return 0, {
                **completion_payload,
                "completion_receipt": str(completion_path),
                "completion_receipt_sha256": completion_sha,
            }
        except Exception as exc:
            intent_observed_sha: str | None = None
            intent_observation_error: str | None = None
            try:
                if os.path.lexists(intent_path):
                    _intent_payload, intent_observed_sha = load_physical_json(
                        intent_path,
                        "intent receipt",
                    )
            except Exception as intent_observation_exc:
                intent_observation_error = "{}: {}".format(
                    type(intent_observation_exc).__name__,
                    intent_observation_exc,
                )
            intent_published = intent_published or intent_observed_sha == intent_sha
            completion_observed_sha: str | None = None
            completion_observation_error: str | None = None
            try:
                if os.path.lexists(completion_path):
                    (
                        _completion_payload,
                        completion_observed_sha,
                    ) = load_physical_json(
                        completion_path,
                        "completion receipt",
                    )
                    completion_info = completion_path.lstat()
                    require(
                        stat.S_IMODE(completion_info.st_mode) == 0o600,
                        "completion receipt mode is not 0600",
                    )
            except Exception as completion_observation_exc:
                completion_observation_error = "{}: {}".format(
                    type(completion_observation_exc).__name__,
                    completion_observation_exc,
                )
            completion_published = (
                completion_expected_sha is not None
                and completion_observed_sha == completion_expected_sha
            )
            post_state = observe_post_state(
                config=config,
                target=target,
                final=final,
                staging_identity=staging_identity,
            )
            completion_recovery_error: str | None = None
            if completion_published and completion_payload is not None:
                completion_post_state_verified = (
                    post_state["staging_present_after"] is False
                    and post_state["staging_identity_matches_after"] is False
                    and post_state["quarantine_present_after"] is True
                    and post_state["quarantine_identity_matches_after"] is True
                    and post_state["published_destination_present_after"] is False
                    and post_state[
                        "published_destination_identity_matches_after"
                    ]
                    is False
                    and post_state["remaining_incomplete_staging"] == []
                    and post_state["source_locations_after"] == ["quarantine"]
                    and post_state["source_retained"] is True
                    and post_state["future_rerun_unblocked"] is True
                    and post_state["post_state_observation_errors"] == []
                )
                if completion_post_state_verified:
                    try:
                        archive_backup.fsync_dir(completion_path.parent)
                        (
                            _recovered_payload,
                            recovered_completion_sha,
                        ) = load_physical_json(
                            completion_path,
                            "completion receipt",
                        )
                        require(
                            recovered_completion_sha == completion_expected_sha,
                            "completion receipt changed during recovery",
                        )
                    except Exception as completion_recovery_exc:
                        completion_recovery_error = "{}: {}".format(
                            type(completion_recovery_exc).__name__,
                            completion_recovery_exc,
                        )
                    else:
                        return 0, {
                            **completion_payload,
                            "completion_receipt": str(completion_path),
                            "completion_receipt_sha256": (
                                recovered_completion_sha
                            ),
                            "completion_receipt_recovered_after_error": True,
                        }
                else:
                    completion_recovery_error = (
                        "post-state no longer supports completion recovery"
                    )
            primary_error = "{}: {}".format(type(exc).__name__, exc)
            failure_payload = {
                "schema_version": FAILURE_SCHEMA,
                "status": "failed",
                "failed_at_utc": utc_now(),
                **common,
                "intent_receipt": str(intent_path),
                "intent_receipt_sha256": intent_sha,
                "intent_receipt_published": intent_published,
                "intent_receipt_observed_sha256": intent_observed_sha,
                "intent_receipt_observation_error": intent_observation_error,
                "completion_receipt": str(completion_path),
                "completion_receipt_attempted": completion_payload is not None,
                "completion_receipt_expected_sha256": completion_expected_sha,
                "completion_receipt_published": completion_published,
                "completion_receipt_observed_sha256": (
                    completion_observed_sha
                ),
                "completion_receipt_observation_error": (
                    completion_observation_error
                ),
                "completion_receipt_recovery_error": (
                    completion_recovery_error
                ),
                "terminal_receipt_state": (
                    "completion_observed_then_failure_recorded"
                    if completion_published
                    else "failure_only"
                ),
                "error": primary_error,
                **post_state,
            }
            try:
                failure_sha = write_json_exclusive(failure_path, failure_payload)
            except Exception as failure_receipt_exc:
                raise ReconciliationError(
                    "{}; failure receipt write failed: {}: {}".format(
                        primary_error,
                        type(failure_receipt_exc).__name__,
                        failure_receipt_exc,
                    )
                ) from exc
            failure_payload["failure_receipt"] = str(failure_path)
            failure_payload["failure_receipt_sha256"] = failure_sha
            raise ReconciliationError(primary_error) from exc
    finally:
        os.close(lock_fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weekly-root", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--quarantine-root", type=Path, required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--phase-receipt", type=Path, required=True)
    parser.add_argument("--process-receipt", type=Path, required=True)
    parser.add_argument("--producer-script", type=Path, required=True)
    parser.add_argument("--openclaw-cli", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-run-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = ReconciliationConfig(
        weekly_root=args.weekly_root,
        staging=args.staging,
        quarantine_root=args.quarantine_root,
        receipt_dir=args.receipt_dir,
        phase_receipt=args.phase_receipt,
        process_receipt=args.process_receipt,
        producer_script=args.producer_script,
        openclaw_cli=args.openclaw_cli,
        task_id=args.task_id,
        task_run_id=args.task_run_id,
        job_id=args.job_id,
    )
    try:
        code, result = reconcile(config, apply=bool(args.apply))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": FAILURE_SCHEMA,
                    "status": "blocked",
                    "error": "{}: {}".format(type(exc).__name__, exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
