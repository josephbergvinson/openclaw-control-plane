#!/usr/bin/env python3
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from .cron_private_output import bounded_utf8_prefix, redact_process_text
except ImportError:
    from cron_private_output import bounded_utf8_prefix, redact_process_text

LEGACY_RUNTIME_ROOT = Path((str(OPERATOR.require_path('paths.workspace'))))
CANONICAL_RUNTIME_ROOT = (
    OPERATOR.require_path('paths.personal_data_runtime')
    if OPERATOR.get('paths.personal_data_runtime') is not None
    else OPERATOR.require_path('paths.workspace') / 'integrations' / 'personal-data'
)
REPOINT_NOW_TARGETS = set(OPERATOR.get('scheduler.repoint_targets', []))
SHARED_CONTROL_PLANE_KEEP_EXTERNAL = {
    'workspace_integrity_guard.py',
}
AMBIGUOUS_BLOCKERS = set(OPERATOR.get('scheduler.ambiguous_entrypoints', []))
MISSING_ENTRYPOINT_EXIT_CODE = getattr(os, 'EX_NOINPUT', 66)
SUPERVISOR_FAILURE_EXIT_CODE = getattr(os, 'EX_SOFTWARE', 70)
SUPERVISOR_MODE = '--_supervise-cron-child'
SUPERVISOR_READY_TIMEOUT_SECONDS = 10.0
SUPERVISOR_PARENT_POLL_SECONDS = 0.1
SUPERVISOR_TERM_GRACE_SECONDS = 10.0
SUPERVISOR_KILL_GRACE_SECONDS = 5.0
MAX_RETAINED_STDERR_BYTES = 512 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + '\n').encode('utf-8')
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.',
        suffix='.tmp',
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def process_group_alive(process_group_id: int | None) -> bool:
    if not process_group_id:
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class ProcessGroupTermination:
    process_group_alive_before_cleanup: bool
    term_sent: bool
    kill_sent: bool
    process_group_alive_after_cleanup: bool


def _wait_for_process_group_exit(
    proc: subprocess.Popen[bytes],
    process_group_id: int,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        proc.poll()
        if not process_group_alive(process_group_id):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(SUPERVISOR_PARENT_POLL_SECONDS, remaining))


def _terminate_supervised_process_group(
    proc: subprocess.Popen[bytes],
    process_group_id: int,
) -> ProcessGroupTermination:
    process_group_alive_before_cleanup = process_group_alive(process_group_id)
    term_sent = False
    kill_sent = False
    if process_group_alive_before_cleanup:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
            term_sent = True
        except ProcessLookupError:
            pass
    if _wait_for_process_group_exit(proc, process_group_id, SUPERVISOR_TERM_GRACE_SECONDS):
        return ProcessGroupTermination(
            process_group_alive_before_cleanup=process_group_alive_before_cleanup,
            term_sent=term_sent,
            kill_sent=kill_sent,
            process_group_alive_after_cleanup=False,
        )

    try:
        os.killpg(process_group_id, signal.SIGKILL)
        kill_sent = True
    except ProcessLookupError:
        pass
    group_exited = _wait_for_process_group_exit(
        proc,
        process_group_id,
        SUPERVISOR_KILL_GRACE_SECONDS,
    )
    return ProcessGroupTermination(
        process_group_alive_before_cleanup=process_group_alive_before_cleanup,
        term_sent=term_sent,
        kill_sent=kill_sent,
        process_group_alive_after_cleanup=not group_exited,
    )


def _exit_like_child(returncode: int) -> int:
    if returncode >= 0:
        return returncode
    signum = -returncode
    if signum not in (signal.SIGKILL, signal.SIGSTOP):
        signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    return 128 + signum


def _write_supervisor_ready(ready_fd: int, child_pid: int) -> None:
    payload = json.dumps(
        {
            'pid': child_pid,
            'process_group_id': child_pid,
        },
        sort_keys=True,
    ).encode('utf-8') + b'\n'
    offset = 0
    while offset < len(payload):
        offset += os.write(ready_fd, payload[offset:])


def _read_receipt_for_supervisor(receipt_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(receipt_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        payload = {
            'schema_version': 'openclaw.cron_python_entrypoint.process_receipt.v2',
            'started_at': utc_now(),
            'business_effect': None,
        }
    return payload


def _claim_supervisor_receipt(receipt_path: Path | None, target_pid: int) -> None:
    if receipt_path is None:
        return
    payload = _read_receipt_for_supervisor(receipt_path)
    payload['receipt_owner'] = 'supervisor'
    payload['status'] = 'running'
    payload.setdefault('child', {}).update(
        {
            'pid': target_pid,
            'process_group_id': target_pid,
            'exit_code': None,
            'signal': None,
        }
    )
    payload['supervisor'] = {
        'pid': os.getpid(),
        'process_group_id': os.getpgrp(),
    }
    payload['updated_at'] = utc_now()
    atomic_write_json(receipt_path, payload)


def _terminalize_supervisor_receipt(
    receipt_path: Path | None,
    *,
    status: str,
    reason: str | None,
    target: subprocess.Popen[bytes] | None,
    termination: ProcessGroupTermination | None,
    started_monotonic: float,
) -> None:
    if receipt_path is None:
        return
    payload = _read_receipt_for_supervisor(receipt_path)
    target_pid = target.pid if target is not None else None
    target_returncode = target.returncode if target is not None else None
    payload['receipt_owner'] = 'supervisor'
    payload['status'] = status
    payload.setdefault('child', {}).update(
        {
            'pid': target_pid,
            'process_group_id': target_pid,
            'exit_code': target_returncode,
            'signal': -target_returncode
            if target_returncode is not None and target_returncode < 0
            else None,
        }
    )
    payload['supervisor'] = {
        'pid': os.getpid(),
        'process_group_id': os.getpgrp(),
    }
    payload.setdefault('termination', {}).update(
        {
            'requested': termination is not None,
            'reason': reason,
            'term_sent': termination.term_sent if termination is not None else False,
            'kill_sent': termination.kill_sent if termination is not None else False,
        }
    )
    residual_alive = (
        termination.process_group_alive_after_cleanup
        if termination is not None
        else process_group_alive(target_pid)
    )
    payload['residual_process'] = {
        'checked': True,
        'process_group_alive_before_cleanup': (
            termination.process_group_alive_before_cleanup
            if termination is not None
            else residual_alive
        ),
        'process_group_alive': residual_alive,
        'process_group_alive_after_cleanup': residual_alive,
    }
    payload['duration_seconds'] = round(time.monotonic() - started_monotonic, 6)
    payload['ended_at'] = utc_now()
    payload['updated_at'] = utc_now()
    atomic_write_json(receipt_path, payload)


def run_child_supervisor(argv: list[str]) -> int:
    started_monotonic = time.monotonic()
    supervisor_parser = argparse.ArgumentParser(add_help=False)
    supervisor_parser.add_argument('--parent-pid', required=True, type=int)
    supervisor_parser.add_argument('--ready-fd', required=True, type=int)
    supervisor_parser.add_argument('--cwd', required=True)
    supervisor_parser.add_argument('--receipt-path')
    supervisor_parser.add_argument('command', nargs=argparse.REMAINDER)
    supervisor_args = supervisor_parser.parse_args(argv)
    receipt_path = Path(supervisor_args.receipt_path) if supervisor_args.receipt_path else None
    command = supervisor_args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        os.close(supervisor_args.ready_fd)
        _terminalize_supervisor_receipt(
            receipt_path,
            status='supervisor_launch_failed',
            reason='missing_supervised_command',
            target=None,
            termination=None,
            started_monotonic=started_monotonic,
        )
        return SUPERVISOR_FAILURE_EXIT_CODE

    # A direct kernel parent relationship cannot be satisfied by a recycled PID:
    # once this process is reparented, getppid() stops matching permanently.
    if os.getppid() != supervisor_args.parent_pid:
        os.close(supervisor_args.ready_fd)
        _terminalize_supervisor_receipt(
            receipt_path,
            status='terminated_parent_lost',
            reason='parent_process_lost_before_child_launch',
            target=None,
            termination=None,
            started_monotonic=started_monotonic,
        )
        return 124

    forwarded_signal: int | None = None

    def request_termination(signum, frame) -> None:  # type: ignore[no-untyped-def]
        nonlocal forwarded_signal
        if forwarded_signal is None:
            forwarded_signal = signum

    signal.signal(signal.SIGTERM, request_termination)
    signal.signal(signal.SIGINT, request_termination)

    try:
        target = subprocess.Popen(
            command,
            cwd=supervisor_args.cwd,
            start_new_session=True,
        )
    except OSError as exc:
        os.close(supervisor_args.ready_fd)
        _terminalize_supervisor_receipt(
            receipt_path,
            status='supervisor_launch_failed',
            reason='target_launch_failed',
            target=None,
            termination=None,
            started_monotonic=started_monotonic,
        )
        print(
            f'CRON_CHILD_SUPERVISOR_LAUNCH_FAILED errno={exc.errno}',
            file=sys.stderr,
            flush=True,
        )
        return SUPERVISOR_FAILURE_EXIT_CODE

    if os.getppid() != supervisor_args.parent_pid:
        os.close(supervisor_args.ready_fd)
        termination = _terminate_supervised_process_group(target, target.pid)
        _terminalize_supervisor_receipt(
            receipt_path,
            status='terminated_parent_lost',
            reason='parent_process_lost',
            target=target,
            termination=termination,
            started_monotonic=started_monotonic,
        )
        return 124 if not termination.process_group_alive_after_cleanup else 125

    try:
        _claim_supervisor_receipt(receipt_path, target.pid)
    except Exception as exc:
        # Receipt durability is required, but a receipt I/O failure must never
        # let the already-launched target outlive its supervisor.
        try:
            os.close(supervisor_args.ready_fd)
        except OSError:
            pass
        termination = _terminate_supervised_process_group(target, target.pid)
        print(
            'CRON_CHILD_SUPERVISOR_RECEIPT_CLAIM_FAILED '
            f'type={type(exc).__name__}',
            file=sys.stderr,
            flush=True,
        )
        return (
            SUPERVISOR_FAILURE_EXIT_CODE
            if not termination.process_group_alive_after_cleanup
            else 125
        )
    try:
        _write_supervisor_ready(supervisor_args.ready_fd, target.pid)
    except OSError:
        termination = _terminate_supervised_process_group(target, target.pid)
        _terminalize_supervisor_receipt(
            receipt_path,
            status='terminated_parent_lost',
            reason='parent_process_lost',
            target=target,
            termination=termination,
            started_monotonic=started_monotonic,
        )
        return 124 if not termination.process_group_alive_after_cleanup else 125
    finally:
        os.close(supervisor_args.ready_fd)

    while True:
        if os.getppid() != supervisor_args.parent_pid:
            termination = _terminate_supervised_process_group(target, target.pid)
            _terminalize_supervisor_receipt(
                receipt_path,
                status='terminated_parent_lost',
                reason='parent_process_lost',
                target=target,
                termination=termination,
                started_monotonic=started_monotonic,
            )
            return 124 if not termination.process_group_alive_after_cleanup else 125

        if forwarded_signal is not None:
            termination = _terminate_supervised_process_group(target, target.pid)
            parent_lost = os.getppid() != supervisor_args.parent_pid
            _terminalize_supervisor_receipt(
                receipt_path,
                status=(
                    'terminated_parent_lost'
                    if parent_lost
                    else f'terminated_signal_{forwarded_signal}'
                ),
                reason='parent_process_lost' if parent_lost else f'terminated_signal_{forwarded_signal}',
                target=target,
                termination=termination,
                started_monotonic=started_monotonic,
            )
            if termination.process_group_alive_after_cleanup:
                return 125
            if target.returncode is not None:
                return _exit_like_child(target.returncode)
            return 128 + forwarded_signal

        returncode = target.poll()
        if returncode is not None:
            if process_group_alive(target.pid):
                termination = _terminate_supervised_process_group(target, target.pid)
                parent_lost = os.getppid() != supervisor_args.parent_pid
                _terminalize_supervisor_receipt(
                    receipt_path,
                    status=(
                        'terminated_parent_lost'
                        if parent_lost
                        else 'residual_process_group_after_child_exit'
                    ),
                    reason=(
                        'parent_process_lost'
                        if parent_lost
                        else 'residual_process_group_after_child_exit'
                    ),
                    target=target,
                    termination=termination,
                    started_monotonic=started_monotonic,
                )
                return 125
            _terminalize_supervisor_receipt(
                receipt_path,
                status='completed' if returncode == 0 else 'failed',
                reason=None,
                target=target,
                termination=None,
                started_monotonic=started_monotonic,
            )
            return _exit_like_child(returncode)

        time.sleep(SUPERVISOR_PARENT_POLL_SECONDS)


if len(sys.argv) > 1 and sys.argv[1] == SUPERVISOR_MODE:
    raise SystemExit(run_child_supervisor(sys.argv[2:]))


def squash(text: str, limit: int = 240) -> str:
    clean = ' '.join((text or '').strip().split())
    return clean[:limit] if len(clean) > limit else clean


def process_output_evidence(stdout: str, stderr: str) -> dict[str, Any]:
    redacted_stderr, redaction_count = redact_process_text(stderr)
    redacted_bytes = redacted_stderr.encode('utf-8')
    retained, retained_bytes, truncated = bounded_utf8_prefix(
        redacted_stderr,
        MAX_RETAINED_STDERR_BYTES,
    )
    traceback_detected = 'Traceback (most recent call last):' in redacted_stderr
    return {
        'capture_owner': 'cron_python_entrypoint_wrapper',
        'stdout_present': bool(stdout),
        'stderr': {
            'present': bool(stderr),
            'original_bytes': len(stderr.encode('utf-8')),
            'redacted_bytes': len(redacted_bytes),
            'retained_bytes': retained_bytes,
            'redacted_sha256': hashlib.sha256(redacted_bytes).hexdigest(),
            'redaction_applied': redaction_count > 0,
            'redaction_count': redaction_count,
            'truncated': truncated,
            'truncation_limit_bytes': MAX_RETAINED_STDERR_BYTES,
            'traceback_detected': traceback_detected,
            'full_redacted_traceback_retained': traceback_detected and not truncated,
            'content_redacted': retained,
        },
    }


MACHINE_STDOUT_PREFIXES = ('EFFECT_PREDICATES ',)


def forwarded_stdout(stdout: str) -> str:
    """The child's stdout minus the machine lines this wrapper has already
    consumed into the process receipt. What remains is the operator message
    the cron job announces verbatim."""

    kept = [
        line
        for line in stdout.splitlines()
        if not line.startswith(MACHINE_STDOUT_PREFIXES)
    ]
    text = '\n'.join(kept).strip('\n')
    return text + '\n' if text else ''


def parse_business_effect(stdout: str) -> dict[str, Any] | None:
    effect_lines = [
        line[len('EFFECT_PREDICATES '):]
        for line in stdout.splitlines()
        if line.startswith('EFFECT_PREDICATES ')
    ]
    malformed_reason = 'effect_predicate_line_unparsable'
    if effect_lines:
        raw_predicates: Any = effect_lines[-1]
    else:
        # JSON-mode actions (notably the P&L panel) must keep stdout as one
        # machine-readable document, so their predicates live at the top-level
        # ``business_effect`` key instead of on a second EFFECT_PREDICATES line.
        try:
            document = json.loads(stdout.strip())
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(document, dict) or 'business_effect' not in document:
            return None
        raw_predicates = document.get('business_effect')
        malformed_reason = 'business_effect_json_unparsable'
    try:
        predicates = json.loads(raw_predicates) if isinstance(raw_predicates, str) else raw_predicates
        if not isinstance(predicates, list) or not predicates:
            raise ValueError('predicate payload must be a non-empty JSON array')
        if not all(
            isinstance(item, dict)
            and isinstance(item.get('satisfied'), bool)
            and isinstance(item.get('reason'), str)
            for item in predicates
        ):
            raise ValueError('predicate records require boolean satisfied and string reason')
    except (json.JSONDecodeError, ValueError, TypeError):
        return {
            'satisfied': None,
            'reason': malformed_reason,
        }
    first_unsatisfied = next(
        (item for item in predicates if not item['satisfied']),
        None,
    )
    return {
        'satisfied': first_unsatisfied is None,
        'reason': first_unsatisfied['reason'] if first_unsatisfied else 'satisfied',
        'observed': [item.get('observed_value') for item in predicates],
        'threshold': [item.get('threshold') for item in predicates],
        'predicates': predicates,
    }


def resolve_target(target: Path) -> Path:
    if not target.is_absolute():
        return target
    if target.name in SHARED_CONTROL_PLANE_KEEP_EXTERNAL:
        return target
    if target.name in AMBIGUOUS_BLOCKERS:
        return target
    legacy_scripts_root = LEGACY_RUNTIME_ROOT / 'scripts'
    try:
        target.relative_to(legacy_scripts_root)
    except ValueError:
        return target
    if target.name not in REPOINT_NOW_TARGETS:
        return target
    candidate = CANONICAL_RUNTIME_ROOT / 'scripts' / target.name
    if candidate.is_file():
        return candidate
    return target


parser = argparse.ArgumentParser(description='Run a Python cron entrypoint with missing-file and empty-output guards.')
parser.add_argument('--script', required=True, help='Absolute path to the target Python script')
parser.add_argument('--what', help='Human-readable job description for alerts')
parser.add_argument('--cwd', help='Working directory for the child script')
parser.add_argument('--receipt-dir', help='Directory for a machine-readable child process receipt')
parser.add_argument(
    '--setenv',
    action='append',
    default=[],
    metavar='KEY=VALUE',
    help=(
        'Set KEY in this process (and therefore the child) before dispatch. '
        'Repeatable. Exists because exec allowlist mode rejects arbitrary '
        'env-var overrides supplied through an /usr/bin/env wrapper, so a '
        'scheduler payload cannot prefix the command with "env KEY=VALUE".'
    ),
)
args, script_args = parser.parse_known_args()

for assignment in args.setenv:
    key, sep, value = str(assignment).partition('=')
    key = key.strip()
    if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
        print(f'CRON_ENTRYPOINT_FAIL invalid --setenv {assignment!r}', file=sys.stderr, flush=True)
        raise SystemExit(64)
    os.environ[key] = value

if script_args and script_args[0] == '--':
    script_args = script_args[1:]

requested_target = Path(args.script)
resolved_target = resolve_target(requested_target)
what = args.what or requested_target.name

receipt_path: Path | None = None
receipt: dict[str, Any] = {
    'schema_version': 'openclaw.cron_python_entrypoint.process_receipt.v2',
    'receipt_owner': 'wrapper',
    'what': what,
    'requested_script': str(requested_target),
    'resolved_script': str(resolved_target),
    'started_at': utc_now(),
    'status': 'starting',
    'child': {
        'pid': None,
        'process_group_id': None,
        'exit_code': None,
        'signal': None,
    },
    'business_effect': None,
    'supervisor': {
        'pid': None,
        'process_group_id': None,
    },
    'termination': {
        'requested': False,
        'reason': None,
        'term_sent': False,
        'kill_sent': False,
    },
    'residual_process': {
        'checked': False,
        'process_group_alive': None,
    },
}
if args.receipt_dir:
    receipt_dir = Path(args.receipt_dir).expanduser()
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    receipt_path = receipt_dir / f'cron-entrypoint-{timestamp}-{os.getpid()}.json'


def write_receipt() -> None:
    if receipt_path is None:
        return
    receipt['updated_at'] = utc_now()
    atomic_write_json(receipt_path, receipt)


def sync_receipt_from_disk() -> bool:
    if receipt_path is None or not receipt_path.is_file():
        return False
    try:
        persisted = json.loads(receipt_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(persisted, dict):
        return False
    receipt.clear()
    receipt.update(persisted)
    return True


def persist_process_output_receipt(stdout: str, stderr: str) -> None:
    if receipt_path is None:
        return
    sync_receipt_from_disk()
    receipt['process_output'] = process_output_evidence(stdout, stderr)
    receipt['business_effect'] = parse_business_effect(stdout)
    receipt['updated_at'] = utc_now()
    write_receipt()


def receipt_is_terminal() -> bool:
    return (
        receipt.get('receipt_owner') == 'supervisor'
        and isinstance(receipt.get('ended_at'), str)
        and receipt.get('status') not in {'starting', 'launching', 'running'}
    )


def reclaim_terminal_receipt_after_supervisor_exit(status: str, reason: str | None) -> None:
    wrapper_termination = dict(receipt.get('termination', {}))
    sync_receipt_from_disk()
    if receipt_is_terminal():
        return
    receipt['receipt_owner'] = 'wrapper_reclaimed_after_supervisor_exit'
    receipt['status'] = status
    receipt.setdefault('child', {}).update(
        {
            'pid': target_process_group_id,
            'process_group_id': target_process_group_id,
        }
    )
    receipt.setdefault('supervisor', {}).update(
        {
            'pid': proc.pid if proc is not None else None,
            'process_group_id': proc.pid if proc is not None else None,
            'exit_code': proc.returncode if proc is not None else None,
            'signal': -proc.returncode
            if proc is not None and proc.returncode is not None and proc.returncode < 0
            else None,
        }
    )
    receipt.setdefault('termination', {}).update(wrapper_termination)
    receipt['termination'].update({'requested': reason is not None, 'reason': reason})
    residual_alive = process_group_alive(target_process_group_id)
    receipt['residual_process'] = {
        'checked': True,
        'process_group_alive': residual_alive,
        'process_group_alive_after_cleanup': residual_alive,
    }
    receipt['ended_at'] = utc_now()
    write_receipt()


if not resolved_target.is_file():
    receipt['status'] = 'missing_entrypoint'
    receipt['ended_at'] = utc_now()
    write_receipt()
    print('CRON_ENTRYPOINT_MISSING')
    print(
        f"STATUS | what: {what} | result: missing_entrypoint | blockers: {squash(str(resolved_target))} not found | next: restore the canonical runtime entrypoint or repoint this cron job"
    )
    raise SystemExit(MISSING_ENTRYPOINT_EXIT_CODE)

cmd = [sys.executable, str(resolved_target), *script_args]
proc: subprocess.Popen[str] | None = None
target_process_group_id: int | None = None


def emit_entrypoint_fail(result: str, blocker: str, next_step: str) -> None:
    redacted_blocker, _ = redact_process_text(blocker)
    print('CRON_ENTRYPOINT_FAIL', flush=True)
    print(
        f"STATUS | what: {what} | result: {result} | blockers: {squash(redacted_blocker)} | next: {next_step}",
        flush=True,
    )


def terminate_child(reason: str) -> tuple[str, str]:
    global proc, target_process_group_id
    child_stdout = ''
    child_stderr = ''
    receipt['termination'].update({'requested': True, 'reason': reason})
    if proc is not None:
        try:
            if process_group_alive(proc.pid):
                os.killpg(proc.pid, signal.SIGTERM)
            child_stdout, child_stderr = proc.communicate(timeout=12)
        except subprocess.TimeoutExpired:
            if process_group_alive(target_process_group_id):
                os.killpg(target_process_group_id, signal.SIGKILL)
                receipt['termination']['kill_sent'] = True
            if process_group_alive(proc.pid):
                os.killpg(proc.pid, signal.SIGKILL)
            child_stdout, child_stderr = proc.communicate(timeout=5)
        except Exception as exc:
            child_stderr = str(exc)
        reclaim_terminal_receipt_after_supervisor_exit(reason, reason)
        persist_process_output_receipt(child_stdout, child_stderr)
    return child_stdout, child_stderr


def handle_sigterm(signum, frame) -> None:  # type: ignore[no-untyped-def]
    child_stdout, child_stderr = terminate_child(f'terminated_signal_{signum}')
    child_stdout = forwarded_stdout(child_stdout)
    if child_stdout.strip():
        sys.stdout.write(child_stdout)
        if not child_stdout.endswith('\n'):
            sys.stdout.write('\n')
    else:
        emit_entrypoint_fail(
            f'terminated_signal_{signum}',
            child_stderr or f'entrypoint received signal {signum} before child produced stdout',
            'inspect cron timeout and target script runtime; rerun manually after fixing timeout or target-script blocker',
        )
    raise SystemExit(124)


signal.signal(signal.SIGTERM, handle_sigterm)

receipt['status'] = 'launching'
write_receipt()
child_cwd = args.cwd or str(resolved_target.parent)
ready_read_fd, ready_write_fd = os.pipe()
supervisor_receipt_args = (
    []
    if receipt_path is None
    else [
        '--receipt-path',
        str(receipt_path),
    ]
)
try:
    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            SUPERVISOR_MODE,
            '--parent-pid',
            str(os.getpid()),
            '--ready-fd',
            str(ready_write_fd),
            '--cwd',
            child_cwd,
            *supervisor_receipt_args,
            '--',
            *cmd,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=(ready_write_fd,),
    )
finally:
    os.close(ready_write_fd)

receipt['supervisor']['pid'] = proc.pid
receipt['supervisor']['process_group_id'] = proc.pid
try:
    ready_fds, _, _ = select.select(
        [ready_read_fd],
        [],
        [],
        SUPERVISOR_READY_TIMEOUT_SECONDS,
    )
    if not ready_fds:
        raise TimeoutError('child supervisor readiness timed out')
    ready_raw = os.read(ready_read_fd, 4096)
    if not ready_raw:
        raise RuntimeError('child supervisor exited before readiness')
    ready_payload = json.loads(ready_raw.decode('utf-8'))
    child_pid = ready_payload.get('pid')
    child_process_group_id = ready_payload.get('process_group_id')
    if (
        not isinstance(child_pid, int)
        or child_pid <= 0
        or not isinstance(child_process_group_id, int)
        or child_process_group_id != child_pid
    ):
        raise ValueError('child supervisor emitted invalid readiness')
    target_process_group_id = child_process_group_id
except (OSError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
    child_stdout, child_stderr = terminate_child('supervisor_handshake_failed')
    if child_stdout.strip():
        sys.stdout.write(child_stdout)
    emit_entrypoint_fail(
        'supervisor_handshake_failed',
        child_stderr or str(exc),
        'inspect the generic cron child supervisor before rerunning this cron job',
    )
    raise SystemExit(SUPERVISOR_FAILURE_EXIT_CODE)
finally:
    os.close(ready_read_fd)

stdout, stderr = proc.communicate()
reclaim_terminal_receipt_after_supervisor_exit(
    'completed' if proc.returncode == 0 else 'failed',
    None,
)
persist_process_output_receipt(stdout, stderr)
residual_process = process_group_alive(target_process_group_id)
if residual_process:
    try:
        os.killpg(target_process_group_id, signal.SIGKILL)
        receipt['termination'].update(
            {
                'requested': True,
                'reason': 'residual_process_group_after_child_exit',
                'kill_sent': True,
            }
        )
    except ProcessLookupError:
        residual_process = False
    receipt['receipt_owner'] = 'wrapper_reclaimed_after_supervisor_exit'
    receipt['residual_process'] = {
        'checked': True,
        'process_group_alive_before_cleanup': True,
        'process_group_alive': process_group_alive(target_process_group_id),
        'process_group_alive_after_cleanup': process_group_alive(target_process_group_id),
    }
    receipt['status'] = 'residual_process_group_after_child_exit'
    receipt['ended_at'] = utc_now()
    write_receipt()
    emit_entrypoint_fail(
        'residual_process_group_after_child_exit',
        'child process group remained alive after the direct child exited',
        'inspect the target script process lifecycle before rerunning this cron job',
    )
    raise SystemExit(125)

if receipt.get('status') == 'residual_process_group_after_child_exit':
    emit_entrypoint_fail(
        'residual_process_group_after_child_exit',
        'child process group remained alive after the direct child exited',
        'inspect the target script process lifecycle before rerunning this cron job',
    )
    raise SystemExit(125)

public_stdout = forwarded_stdout(stdout)
if proc.returncode == 0:
    if public_stdout:
        sys.stdout.write(public_stdout)
    else:
        print('NO_REPLY')
    raise SystemExit(0)

if public_stdout.strip():
    sys.stdout.write(public_stdout)
else:
    emit_entrypoint_fail(
        f'exit_{proc.returncode}',
        stderr or f'command exited with code {proc.returncode}',
        'inspect script stdout/stderr and rerun manually',
    )
raise SystemExit(proc.returncode)
