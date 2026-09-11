#!/usr/bin/env python3
"""Retire an inactive root-owned Approval A execution root.

Dry-run is the default. Apply mode acquires the canonical Approval A singleton
lock without waiting, renames the captured root beside itself, then removes
only that captured identity. Immutable flags are cleared without following
symlinks after the rename and before removal. A held lock protects an active
producer. A free lock admits stale islands even when crashed-producer metadata
still says ``active``. Exact helper-owned rename residue is convergently
removed later.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

try:
    from .operator_contract import load_operator_contract
except ImportError:  # direct script execution
    from operator_contract import load_operator_contract

OPERATOR = load_operator_contract()


LOCK_NAME = "candidate-build.lock"
APPLY_ENV = "OPENCLAW_APPROVAL_A_RETENTION_APPLY"
RUN_NAME_RE = re.compile(r"^A-[A-Za-z0-9][A-Za-z0-9._:-]{0,125}$")
OPERATION_ID_RE = re.compile(
    r"^approval-a-retention-\d{8}T\d{12}Z-\d+-[0-9a-f]{32}$"
)
RETIRING_PREFIX = ".OpenClawApprovalA-retiring-"
RMTREE_AVOIDS_SYMLINK_ATTACKS = bool(
    getattr(shutil.rmtree, "avoids_symlink_attacks", False)
)
CHFLAGS = Path("/usr/bin/chflags")
CHFLAGS_TIMEOUT_SECONDS = 900
CAPTURED_IDENTITY_FIELDS = ("device", "inode", "uid", "gid", "mode")


class RetentionError(RuntimeError):
    """The exact Approval A retirement boundary could not be proven."""


class ActiveProducer(RetentionError):
    """The Approval A singleton lock is held."""


@dataclass(frozen=True)
class RetentionConfig:
    execution_root: Path = field(default_factory=lambda: OPERATOR.require_path("paths.approval_a_execution_root"))
    artifact_root: Path = field(default_factory=lambda: (OPERATOR.require_path("paths.workspace") / "artifacts" / "approval_a_retention"))
    expected_uid: int = 0
    expected_gid: int = 0
    root_mode: int = 0o711
    lock_mode: int = 0o600
    run_mode: int = 0o711
    bootstrap_mode: int = 0o700
    require_root: bool = True


@dataclass
class OpenedRoot:
    root_fd: int
    lock: BinaryIO
    identity: dict[str, int]
    lock_identity: dict[str, int]
    children: list[str]

    def close(self) -> None:
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
        finally:
            self.lock.close()
            os.close(self.root_fd)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def identity(value: os.stat_result) -> dict[str, int]:
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "uid": int(value.st_uid),
        "gid": int(value.st_gid),
        "mode": stat.S_IMODE(value.st_mode),
        "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
    }


def same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def free_bytes(path: Path) -> int:
    value = os.statvfs(path)
    return int(value.f_bavail * value.f_frsize)


def du_bytes(path: Path) -> tuple[int | None, str | None]:
    try:
        proc = subprocess.run(
            ["/usr/bin/du", "-sk", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, (
            f"du_exit_{proc.returncode}:"
            f"{' '.join(proc.stderr.split())[:200]}"
        )
    try:
        return int(proc.stdout.splitlines()[0].split()[0]) * 1024, None
    except (IndexError, ValueError) as exc:
        return None, f"invalid_du_output:{type(exc).__name__}"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def operation_details(
    artifact_root: Path,
) -> tuple[str, str, Path]:
    started = utc_now()
    operation_id = (
        "approval-a-retention-"
        f"{started.strftime('%Y%m%dT%H%M%S%fZ')}-"
        f"{os.getpid()}-{uuid.uuid4().hex}"
    )
    return operation_id, utc_text(started), artifact_root / f"{operation_id}.json"


def expected_realpath(path: Path) -> Path:
    return Path(os.path.realpath(path.parent)) / path.name


def physical_directory(
    path: Path,
    *,
    config: RetentionConfig,
    expected_mode: int,
    label: str,
) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise RetentionError(
            f"cannot lstat {label}: {type(exc).__name__}: {exc}"
        ) from exc
    if (
        not stat.S_ISDIR(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or Path(os.path.realpath(path)) != expected_realpath(path)
        or value.st_uid != config.expected_uid
        or value.st_gid != config.expected_gid
        or stat.S_IMODE(value.st_mode) != expected_mode
    ):
        raise RetentionError(f"{label} identity is invalid: {path}")
    return value


def direct_children(
    root_fd: int,
    *,
    config: RetentionConfig,
    lock_identity: dict[str, int],
) -> list[str]:
    try:
        names = sorted(os.listdir(root_fd))
    except OSError as exc:
        raise RetentionError(
            f"cannot list Approval A root: {type(exc).__name__}: {exc}"
        ) from exc
    if LOCK_NAME not in names:
        raise RetentionError("Approval A root lacks its singleton lock")
    for name in names:
        value = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if name == LOCK_NAME:
            if identity(value) != lock_identity:
                raise RetentionError("Approval A lock changed during inventory")
            continue
        expected_mode = (
            config.bootstrap_mode
            if name == "bootstrap-bundles"
            else config.run_mode
            if RUN_NAME_RE.fullmatch(name)
            else None
        )
        if expected_mode is None:
            raise RetentionError(
                f"unexpected Approval A direct child blocks retention: {name}"
            )
        if (
            not stat.S_ISDIR(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or value.st_uid != config.expected_uid
            or value.st_gid != config.expected_gid
            or stat.S_IMODE(value.st_mode) != expected_mode
        ):
            raise RetentionError(
                f"Approval A direct child identity is invalid: {name}"
            )
    return names


def open_inactive_root(config: RetentionConfig) -> OpenedRoot:
    root_info = physical_directory(
        config.execution_root,
        config=config,
        expected_mode=config.root_mode,
        label="Approval A execution root",
    )
    root_fd = os.open(
        config.execution_root,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    lock_fd = -1
    lock: BinaryIO | None = None
    try:
        opened_root = os.fstat(root_fd)
        if not same_object(root_info, opened_root):
            raise RetentionError("Approval A root changed while opening")
        lock_fd = os.open(
            LOCK_NAME,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        opened_lock = os.fstat(lock_fd)
        named_lock = os.stat(
            LOCK_NAME,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        if (
            not same_object(opened_lock, named_lock)
            or not stat.S_ISREG(opened_lock.st_mode)
            or opened_lock.st_nlink != 1
            or opened_lock.st_uid != config.expected_uid
            or opened_lock.st_gid != config.expected_gid
            or stat.S_IMODE(opened_lock.st_mode) != config.lock_mode
        ):
            raise RetentionError("Approval A singleton lock identity is invalid")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ActiveProducer("Approval A singleton lock is held") from exc
        lock = os.fdopen(lock_fd, "r+b", closefd=True)
        lock_fd = -1
        lock_identity = identity(opened_lock)
        return OpenedRoot(
            root_fd=root_fd,
            lock=lock,
            identity=identity(opened_root),
            lock_identity=lock_identity,
            children=direct_children(
                root_fd,
                config=config,
                lock_identity=lock_identity,
            ),
        )
    except Exception:
        if lock_fd >= 0:
            os.close(lock_fd)
        if lock is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()
        os.close(root_fd)
        raise


def retiring_path(config: RetentionConfig, operation_id: str) -> Path:
    if OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise RetentionError("Approval A retention operation id is invalid")
    return config.execution_root.parent / f"{RETIRING_PREFIX}{operation_id}"


def interrupted_retirements(
    config: RetentionConfig,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name in sorted(os.listdir(config.execution_root.parent)):
        if not name.startswith(RETIRING_PREFIX):
            continue
        operation_id = name[len(RETIRING_PREFIX) :]
        if OPERATION_ID_RE.fullmatch(operation_id) is None:
            continue
        path = config.execution_root.parent / name
        value = physical_directory(
            path,
            config=config,
            expected_mode=config.root_mode,
            label="interrupted Approval A retirement",
        )
        logical_bytes, size_error = du_bytes(path)
        records.append(
            {
                "path": str(path),
                "identity": identity(value),
                "logical_bytes": logical_bytes,
                "size_error": size_error,
                "state": "would_remove",
                "error": None,
            }
        )
    return records


def captured_directory_matches(
    path: Path,
    expected: dict[str, int],
) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
        return False
    actual = identity(value)
    return all(
        actual[field] == expected[field]
        for field in CAPTURED_IDENTITY_FIELDS
    )


def clear_immutable_flags(
    path: Path,
    expected: dict[str, int],
) -> None:
    """Clear immutable flags only within an exact captured directory tree."""

    if not captured_directory_matches(path, expected):
        raise RetentionError(
            f"retirement candidate identity changed before chflags: {path}"
        )
    argv = [
        str(CHFLAGS),
        "-R",
        "-P",
        "noschg,nouchg",
        str(path),
    ]
    try:
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=CHFLAGS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RetentionError(
            f"immutable flag clearing timed out for {path}"
        ) from exc
    except OSError as exc:
        raise RetentionError(
            "cannot execute immutable flag clearing for "
            f"{path}: {type(exc).__name__}: {exc}"
        ) from exc
    if proc.returncode != 0:
        diagnostic = " ".join(proc.stderr.split())[:300]
        raise RetentionError(
            "immutable flag clearing failed for "
            f"{path}: exit_{proc.returncode}: {diagnostic or 'no stderr'}"
        )
    if not captured_directory_matches(path, expected):
        raise RetentionError(
            f"retirement candidate identity changed after chflags: {path}"
        )


def remove_record(record: dict[str, Any]) -> None:
    path = Path(record["path"])
    if not captured_directory_matches(path, record["identity"]):
        raise RetentionError(
            f"retirement candidate identity changed: {path}"
        )
    if not RMTREE_AVOIDS_SYMLINK_ATTACKS:
        raise RetentionError("platform rmtree is not symlink-attack resistant")
    clear_immutable_flags(path, record["identity"])
    shutil.rmtree(path)
    if os.path.lexists(path):
        raise RetentionError(f"retirement candidate remains: {path}")


def relpath(path: Path) -> str:
    try:
        return path.relative_to(OPERATOR.require_path("paths.workspace")).as_posix()
    except ValueError:
        return str(path)


def build_report(
    *,
    config: RetentionConfig,
    operation_id: str,
    started_at: str,
    apply: bool,
    state: str,
    terminal: bool,
    root: dict[str, Any],
    interrupted: list[dict[str, Any]],
    errors: list[str],
    before_free: int | None,
    after_free: int | None,
) -> dict[str, Any]:
    records = [
        *([root] if root["state"] != "absent" else []),
        *interrupted,
    ]
    removed = [item for item in records if item["state"] == "removed"]
    candidates = [
        item
        for item in records
        if item["state"]
        in {"would_remove", "pending", "removal_in_progress", "renamed"}
    ]
    delta = (
        after_free - before_free
        if before_free is not None and after_free is not None
        else None
    )
    return {
        "schema": "openclaw.approval_a_retention.v1",
        "operation_id": operation_id,
        "created_at_utc": started_at,
        "updated_at_utc": utc_text(),
        "mode": "apply" if apply else "dry-run",
        "state": state,
        "terminal": terminal,
        "execution_root": str(config.execution_root),
        "policy": {
            "producer_coordination": (
                "candidate-build.lock must be acquired nonblocking"
            ),
            "stale_claim_handling": (
                "stale active text does not override a free singleton lock"
            ),
            "deletion": "rename exact captured root, then remove that identity",
            "immutable_flags": (
                "clear schg and uchg recursively without following symlinks"
            ),
            "gateway_restart": "not_performed",
        },
        "summary": {
            "total_count": len(records),
            "candidate_count": len(candidates),
            "removed_count": len(removed),
            "error_count": len(errors),
            "logical_candidate_bytes": sum(
                int(item.get("logical_bytes") or 0) for item in candidates
            ),
            "logical_removed_bytes": sum(
                int(item.get("logical_bytes") or 0) for item in removed
            ),
            "before_free_bytes": before_free,
            "after_free_bytes": after_free,
            "observed_free_space_delta_bytes": delta,
        },
        "root": root,
        "interrupted_retirements": interrupted,
        "errors": errors,
        "gateway_restart": "not_performed",
    }


def write_report(
    report_path: Path,
    payload: dict[str, Any],
) -> None:
    atomic_write_json(report_path, payload)
    atomic_write_json(report_path.parent / "latest.json", payload)


def status_line(
    payload: dict[str, Any],
    *,
    report_path: Path,
) -> tuple[str, str]:
    summary = payload["summary"]
    errors = payload["errors"]
    root_state = payload["root"]["state"]
    if errors:
        marker, result = "APPROVAL_A_RETENTION_BLOCKED", "retention_blocked"
    elif root_state == "active_protected":
        marker, result = (
            "APPROVAL_A_RETENTION_OK",
            "active_approval_a_protected",
        )
    elif payload["mode"] == "dry-run" and summary["candidate_count"]:
        marker, result = (
            "APPROVAL_A_RETENTION_DRY_RUN",
            "dry_run_candidates",
        )
    elif payload["mode"] == "apply" and summary["removed_count"]:
        marker, result = (
            "APPROVAL_A_RETENTION_OK",
            "removed_stale_approval_a",
        )
    else:
        marker, result = "APPROVAL_A_RETENTION_OK", "approval_a_absent"
    reclaimed = summary["observed_free_space_delta_bytes"]
    reclaimed_text = (
        "unknown"
        if reclaimed is None
        else f"{reclaimed / (1024 ** 3):.2f}GiB"
    )
    detail = (
        f"STATUS | result: {result} | mode: {payload['mode']} "
        f"| total: {summary['total_count']} "
        f"| candidates: {summary['candidate_count']} "
        f"| removed: {summary['removed_count']} "
        f"| reclaimed: {reclaimed_text} "
        f"| report: {relpath(report_path)} "
        "| gateway_restart: not_performed"
    )
    if errors:
        detail += " | blockers: " + "; ".join(errors)
    return marker, detail


def run(
    *,
    apply: bool,
    config: RetentionConfig | None = None,
) -> tuple[int, str, str, Path]:
    config = config if config is not None else RetentionConfig()
    operation_id, started_at, report_path = operation_details(
        config.artifact_root
    )
    root: dict[str, Any] = {
        "path": str(config.execution_root),
        "before_exists": os.path.lexists(config.execution_root),
        "identity": None,
        "lock_identity": None,
        "children": [],
        "logical_bytes": None,
        "size_error": None,
        "state": "absent",
        "renamed_path": None,
        "replacement_root_detected": False,
        "after_exists": os.path.lexists(config.execution_root),
        "error": None,
    }
    interrupted: list[dict[str, Any]] = []
    errors: list[str] = []
    opened: OpenedRoot | None = None
    before_free: int | None = None
    after_free: int | None = None

    def persist(state: str, *, terminal: bool) -> dict[str, Any]:
        payload = build_report(
            config=config,
            operation_id=operation_id,
            started_at=started_at,
            apply=apply,
            state=state,
            terminal=terminal,
            root=root,
            interrupted=interrupted,
            errors=errors,
            before_free=before_free,
            after_free=after_free,
        )
        write_report(report_path, payload)
        return payload

    try:
        if config.require_root and os.geteuid() != 0:
            raise RetentionError("Approval A retention requires root")
        before_free = free_bytes(config.execution_root.parent)
        interrupted = interrupted_retirements(config)
        if os.path.lexists(config.execution_root):
            try:
                opened = open_inactive_root(config)
            except ActiveProducer:
                root["state"] = "active_protected"
            else:
                logical_bytes, size_error = du_bytes(config.execution_root)
                root.update(
                    {
                        "identity": opened.identity,
                        "lock_identity": opened.lock_identity,
                        "children": opened.children,
                        "logical_bytes": logical_bytes,
                        "size_error": size_error,
                        "state": "pending" if apply else "would_remove",
                    }
                )

        if apply and root["state"] != "active_protected":
            persist("apply_in_progress", terminal=False)
            for record in interrupted:
                record["state"] = "removal_in_progress"
                persist("apply_in_progress", terminal=False)
                try:
                    remove_record(record)
                    record["state"] = "removed"
                except Exception as exc:
                    record["state"] = "remove_failed"
                    record["error"] = f"{type(exc).__name__}: {exc}"
                    errors.append(
                        f"{Path(record['path']).name}:{record['error']}"
                    )

            if opened is not None:
                if not captured_directory_matches(
                    config.execution_root,
                    opened.identity,
                ):
                    raise RetentionError(
                        "Approval A root changed before retirement"
                    )
                named_lock = os.stat(
                    LOCK_NAME,
                    dir_fd=opened.root_fd,
                    follow_symlinks=False,
                )
                if identity(named_lock) != opened.lock_identity:
                    raise RetentionError(
                        "Approval A lock changed before retirement"
                    )
                staged = retiring_path(config, operation_id)
                if os.path.lexists(staged):
                    raise RetentionError(
                        f"retirement staging path already exists: {staged}"
                    )
                os.rename(config.execution_root, staged)
                staged_info = staged.lstat()
                captured_fields = ("device", "inode", "uid", "gid", "mode")
                staged_identity = identity(staged_info)
                if any(
                    staged_identity[field] != opened.identity[field]
                    for field in captured_fields
                ):
                    raise RetentionError(
                        "renamed Approval A root identity changed"
                    )
                root.update(
                    {
                        "state": "renamed",
                        "renamed_path": str(staged),
                        "replacement_root_detected": os.path.lexists(
                            config.execution_root
                        ),
                    }
                )
                persist("apply_in_progress", terminal=False)
                remove_record(
                    {
                        "path": str(staged),
                        "identity": staged_identity,
                    }
                )
                root["state"] = "removed"
                root["after_exists"] = os.path.lexists(
                    config.execution_root
                )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        errors.append(message)
        if root["state"] not in {"absent", "active_protected", "removed"}:
            root["state"] = "remove_failed"
            root["error"] = message
    finally:
        if opened is not None:
            opened.close()

    try:
        after_free = free_bytes(config.execution_root.parent)
    except OSError as exc:
        errors.append(f"after_free_space:{type(exc).__name__}: {exc}")
    terminal_state = (
        "apply_blocked"
        if errors
        else "apply_complete"
        if apply
        else "dry_run_complete"
    )
    payload = persist(terminal_state, terminal=True)
    marker, detail = status_line(payload, report_path=report_path)
    return (
        1 if marker == "APPROVAL_A_RETENTION_BLOCKED" else 0,
        marker,
        detail,
        report_path,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply stale Approval A root retention.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Remove an inactive exact Approval A root and helper-owned "
            "interrupted retirement staging."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rc, marker, detail, _ = run(
        apply=bool(args.apply or os.environ.get(APPLY_ENV) == "1")
    )
    print(marker)
    print(detail)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
