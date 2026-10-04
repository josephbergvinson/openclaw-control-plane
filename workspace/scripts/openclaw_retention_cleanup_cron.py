#!/usr/bin/env python3
"""Apply daily host and OpenClaw retention; --preview disables deletion.

Child receipts retain diagnostic detail. --human prints the concise outcome
intended for Discord, including failures even when some cleanup succeeded.
"""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import subprocess
import stat
import time
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    from . import operation_effect_predicate as effect
except ImportError:
    import operation_effect_predicate as effect

WORKSPACE = Path(os.environ.get('WORKSPACE', (str(OPERATOR.require_path('paths.workspace'))))).expanduser()
RUNTIME_RELEASE_RETENTION_SCRIPT = WORKSPACE / 'scripts' / 'openclaw_runtime_release_retention.py'
RUNTIME_PROMOTION_RETENTION_SCRIPT = WORKSPACE / 'scripts' / 'openclaw_runtime_promotion_retention.py'
APPROVAL_A_RETENTION_SCRIPT = WORKSPACE / 'scripts' / 'openclaw_approval_a_retention.py'
HOST_STORAGE_SCRIPT = WORKSPACE / 'scripts' / 'openclaw_storage_prune.py'
RUNTIME_RELEASE_APPLY_ENV = 'OPENCLAW_RUNTIME_RELEASE_PRUNE_APPLY'
RUNTIME_PROMOTION_APPLY_ENV = 'OPENCLAW_RUNTIME_PROMOTION_PRUNE_APPLY'
APPROVAL_A_APPLY_ENV = 'OPENCLAW_APPROVAL_A_RETENTION_APPLY'
ROOT_PYTHON_PREFIX = ('/usr/bin/sudo', '-n', '/usr/bin/python3')
CHILD_TIMEOUT_SECONDS = float(os.environ.get('OPENCLAW_RETENTION_CHILD_TIMEOUT_SECONDS', '300'))
RUNTIME_RELEASE_CHILD_TIMEOUT_SECONDS = float(
    os.environ.get('OPENCLAW_RETENTION_RUNTIME_RELEASE_CHILD_TIMEOUT_SECONDS', '900')
)
APPROVAL_A_CHILD_TIMEOUT_SECONDS = float(
    os.environ.get('OPENCLAW_RETENTION_APPROVAL_A_CHILD_TIMEOUT_SECONDS', '1200')
)
ANNOUNCE_SUCCESS = os.environ.get('OPENCLAW_RETENTION_ANNOUNCE_SUCCESS', '1').strip().lower() in {'1', 'true', 'yes', 'on'}

RESULT_RE = re.compile(r'(?:^|\|)\s*result:\s*([^|]+)')
BLOCKERS_RE = re.compile(r'(?:^|\|)\s*blockers:\s*([^|]+)')
PENDING_RE = re.compile(r'(?:^|\|)\s*pending:\s*([^|]+)')
PRUNED_RE = re.compile(r'(?:^|\|)\s*pruned:\s*([^|]+)')
REMOVED_RE = re.compile(r'(?:^|\|)\s*removed:\s*([^|]+)')
FIELD_RE = re.compile(r'(?:^|\|)\s*([a-zA-Z_]+):\s*([^|]+)')
RETENTION_ALERT_CONTRACT = {
    'objects': ('runtime_releases', 'runtime_promotions', 'approval_a'),
    'metrics': (
        'result',
        'semantic_status',
        'mode',
        'deletion_authorized',
        'runtime_releases',
        'runtime_promotions',
        'approval_a',
        'next',
    ),
}
INLINE_CHILD_FRAGMENT_LIMIT = 512
HOST_STAGE_WALL_SECONDS = 1080
HOST_MAX_PASSES = 2
HOST_PASS_BUDGET_SECONDS = 480


@dataclass(frozen=True)
class ChildResult:
    name: str
    returncode: int
    stdout: str = ''
    stderr: str = ''
    timed_out: bool = False
    attempts: tuple[dict, ...] = ()
    continuation_stop: str | None = None


def squash(text: str) -> str:
    clean = ' '.join((text or '').split())
    return clean


def complete_or_correlation(text: str, limit: int = INLINE_CHILD_FRAGMENT_LIMIT) -> str:
    """Keep a complete fragment or replace it with a stable compact identity.

    Arbitrary prefix slicing can remove the exact path or predicate an operator
    needs. Over-budget diagnostics therefore become an explicit correlation
    token instead of a misleading partial field.
    """

    clean = squash((text or '').replace('|', '/'))
    if len(clean) <= limit:
        return clean
    digest = hashlib.sha256(clean.encode('utf-8')).hexdigest()[:16]
    return (
        'diagnostic_exceeds_inline_budget; '
        f'correlation=sha256:{digest}; characters={len(clean)}'
    )


def child_fragment(text: str, limit: int = INLINE_CHILD_FRAGMENT_LIMIT) -> str:
    """Return one compact fragment safe for pipe-delimited parent status."""

    return complete_or_correlation(text, limit)


def first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    return complete_or_correlation(match.group(1))


def status_fields(text: str) -> dict[str, str]:
    return {
        match.group(1): complete_or_correlation(match.group(2))
        for match in FIELD_RE.finditer(text)
    }


def validate_alert_request_vs_output(
    *,
    requested_objects: tuple[str, ...] | list[str],
    requested_metrics: tuple[str, ...] | list[str],
    output_text: str,
) -> list[str]:
    """Return missing requested objects/metrics from a retention alert line.

    This is deliberately small and source-local: cron alerts must not claim a
    retention object or metric unless the compact STATUS readback includes a
    concrete field for it. The helper is reusable by tests/gates that need to
    compare the user's requested nouns against alert output fields.
    """

    fields = status_fields(output_text)
    field_names = set(fields)
    metric_tokens: set[str] = set(field_names)
    for value in fields.values():
        for part in value.split(','):
            key = part.split('=', 1)[0].strip()
            if key:
                metric_tokens.add(key)

    problems: list[str] = []
    for object_name in requested_objects:
        if object_name not in field_names:
            problems.append(f'requested object missing from alert output: {object_name}')
    for metric in requested_metrics:
        if metric not in metric_tokens:
            problems.append(f'requested metric missing from alert output: {metric}')
    return problems


def metric_summary(fields: dict[str, str]) -> str | None:
    bits: list[str] = []
    for key in (
        'total',
        'protected',
        'retained',
        'candidates',
        'removed',
        'reclaimable',
        'reclaimed',
        'observed_free_space_delta',
    ):
        if key in fields:
            bits.append(f'{key}={fields[key]}')
    if fields.get('plugin_caches'):
        bits.append(f"plugin_caches={fields['plugin_caches']}")
    if fields.get('report'):
        bits.append(f"report={fields['report']}")
    return ', '.join(bits) if bits else None


def exception_tail(text: str) -> str | None:
    for line in reversed((text or '').splitlines()):
        clean = line.strip()
        if not clean or clean.startswith('File '):
            continue
        if re.match(r'^(?:[A-Za-z_][A-Za-z0-9_.]*)?(?:Error|Exception|TimeoutExpired|Warning):', clean):
            return child_fragment(clean)
    return None


def child_summary(child: ChildResult) -> str:
    stdout = child.stdout.strip()
    stderr = child.stderr.strip()
    if child.timed_out:
        tail = exception_tail(stderr) or exception_tail(stdout)
        return child_fragment('timeout' + (f'; last_exception={tail}' if tail else ''))
    if not stdout or stdout == 'NO_REPLY':
        if child.returncode == 0:
            return 'no_action'
        if stderr:
            blocker = exception_tail(stderr) or child_fragment(stderr)
            return child_fragment(f'exit_{child.returncode}; blocker={blocker}')
        return f'exit_{child.returncode}'

    fields = status_fields(stdout)
    result = fields.get('result') or first_match(RESULT_RE, stdout) or stdout.splitlines()[0].strip()
    if child.returncode != 0:
        blocker = (
            fields.get('blockers')
            or first_match(BLOCKERS_RE, stdout)
            or exception_tail(stderr)
            or child_fragment(stderr)
            or f'exit_{child.returncode}'
        )
        return child_fragment(f'{result}; blocker={blocker}')

    metrics = metric_summary(fields)
    pruned = first_match(PRUNED_RE, stdout)
    removed = first_match(REMOVED_RE, stdout)
    pending = first_match(PENDING_RE, stdout)
    detail = metrics or pruned or removed or pending
    if detail:
        return child_fragment(f'{result}; {detail}')
    return child_fragment(result)


def run_child(
    name: str,
    script: Path,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = CHILD_TIMEOUT_SECONDS,
    command_prefix: tuple[str, ...] | None = None,
) -> ChildResult:
    prefix = command_prefix or (sys.executable,)
    argv = [*prefix, str(script), *(args or [])]
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(WORKSPACE),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        def decoded(value: str | bytes | None) -> str:
            return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else (value or '')
        return ChildResult(name=name, returncode=124, stdout=decoded(exc.stdout), stderr=decoded(exc.stderr), timed_out=True)
    except Exception as exc:  # pragma: no cover - defensive cron path
        return ChildResult(name=name, returncode=1, stderr=f'{type(exc).__name__}: {exc}')
    return ChildResult(name=name, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def retire_completed_activation(*, apply: bool) -> str | None:
    """Retire terminal controls through the activator's locked, checked API.

    This never starts, restores, or restarts a runtime. Preview leaves all
    controls untouched. A pending/invalid operation blocks payload cleanup.
    """
    if not apply:
        return None
    try:
        from . import openclaw_runtime_activate as activation
    except ImportError:
        import openclaw_runtime_activate as activation
    paths = activation.live_paths()
    if not os.path.lexists(paths.result):
        if os.path.lexists(activation._start_fence_path(paths)):
            raise ValueError('activation start exists without a terminal result')
        return None
    record, _, _ = activation.read_json(paths.result, 'daily terminal activation result')
    if (record.get('outcome') != 'activated' or record.get('restoreRequired') is not False
            or record.get('statesVisited') != ['preflight', 'apply', 'verify', 'terminal']
            or record.get('error') is not None):
        raise ValueError('activation is not a completed successful operation')
    archive = paths.result.parent / 'archive' / f'daily-retention-{uuid.uuid4().hex}'
    activation.retire_terminal_receipts(paths, archive)
    return str(archive / activation.RETIREMENT_RECEIPT_NAME)


def host_continuation_controls() -> tuple:
    """Pin bounded source/selector controls; each pass still acquires native locks."""
    current = OPERATOR.require_path('paths.runtime_current_link')
    result = OPERATOR.require_path('paths.activation_result')
    operator_source = Path(os.environ.get('OPENCLAW_OPERATOR_CONFIG',
        str(Path(__file__).resolve().parents[1] / 'operator.json')))
    paths = [HOST_STORAGE_SCRIPT, Path(__file__).absolute(), operator_source,
             OPERATOR.require_path('paths.volume_guard_contract'),
             HOST_STORAGE_SCRIPT.resolve().parents[1] / 'registry/external_volume_guard.json', current,
             result, result.with_name('activation-start-consumed.json')]
    controls = []
    for path in paths:
        try:
            value = path.lstat()
        except FileNotFoundError:
            controls.append((str(path), None))
            continue
        controls.append((str(path), value.st_dev, value.st_ino, value.st_mode,
                         value.st_size, value.st_mtime_ns, value.st_ctime_ns,
                         os.readlink(path) if stat.S_ISLNK(value.st_mode) else None))
    return tuple(controls)


def host_effect_plan_bindings(report: dict) -> bool:
    """Do not admit a successor from effects outside the producer's guarded plans."""
    plans = {}
    for field in ('candidates',):
        rows = report.get(field)
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('path'), str) or row['path'] in plans:
                return False
            plans[row['path']] = row
    for row in report.get('removed', []):
        if not isinstance(row, dict):
            return False
        original = row.get('original_path')
        planned = plans.get(original)
        if not planned or row.get('kind') != planned.get('kind'):
            return False
        expected = (planned.get('device'), planned.get('inode'))
        if (any(type(value) is not int for value in expected)
                or (row.get('device'), row.get('inode')) != expected
                or row.get('captured_inode_removed') is not True):
            return False
    for field, plan_field in (('removed_simulators', 'simulator_plan'), ('archived_logs', 'log_rotation_plan')):
        plans_for_stage = {row['path']: row for row in report.get(plan_field, [])}
        for row in report.get(field, []):
            planned = plans_for_stage.get(row['path'])
            if not planned:
                return False
            if field == 'removed_simulators' and any(row.get(key) != planned.get(key) for key in ('device', 'inode', 'id')):
                return False
    return True


def host_budget_continuable(child: ChildResult) -> bool:
    if child.returncode != 1 or child.timed_out:
        return False
    try:
        report = host_report(child)
        if (report.get('schema') != 'openclaw.storage_prune.v1' or report.get('mode') != 'apply'
                or report.get('terminal') is not True or report.get('status') != 'partial'):
            return False
        errors = report.get('errors')
        rows = report.get('skipped', []) + report.get('deferred', [])
        if not isinstance(errors, list) or not errors or errors != [row for row in rows if row.get('error')]:
            return False
        unfinished = [row for row in rows if row.get('error') or row.get('deferred') or row.get('not_started')]
        for row in unfinished:
            if (row.get('cause') != 'execution_budget' or row.get('deferred') is not True
                    or row.get('partially_removed') is not False
                    or row.get('effects') not in ('none', 'restored_zero')
                    or row.get('state') not in ('not_started', 'restored')
                    or type(row.get('target_count')) is not int or row['target_count'] not in (0, 1)
                    or not isinstance(row.get('stage'), str)
                    or (row['target_count'] == 1 and (not isinstance(row.get('path'), str) or not Path(row['path']).is_absolute()))
                    or row.get('removed_entries', row.get('removed_files', 0)) != 0):
                return False
            if row['state'] == 'restored':
                value = Path(row['path']).lstat()
                if row.get('effects') != 'restored_zero' or row.get('restored_identity') != {'device':value.st_dev, 'inode':value.st_ino}:
                    return False
            elif row.get('effects') != 'none' or row.get('not_started') is not True:
                return False
        try:
            from . import openclaw_storage_prune as storage
        except ImportError:
            import openclaw_storage_prune as storage
        if (not isinstance(report.get('summary'), dict)
                or any(type(value) is not int or value < 0 for value in report['summary'].values())
                or report['summary'] != storage.cleanup_summary(report)):
            return False
        if not host_effect_plan_bindings(report) or any(not predicate['satisfied'] for predicate in validate_host_effects(report)):
            return False
        # A new pass is useful only after actual, independently verified progress.
        counts, logs, _ = verified_cleanup_counts([child])
        return sum(count for _, count in counts) + logs > 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def run_host_stage(*, apply: bool) -> ChildResult:
    deadline = time.monotonic() + HOST_STAGE_WALL_SECONDS
    try:
        controls = host_continuation_controls()
    except (OSError, ValueError):
        controls = None
    attempts = []
    stop = None
    child = ChildResult('host_storage', 1, stderr='host stage wall budget exhausted before admission')
    for index in range(HOST_MAX_PASSES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop = 'host_stage_wall_budget'
            break
        budget = min(HOST_PASS_BUDGET_SECONDS, max(1, int(remaining)))
        child = run_child('host_storage', HOST_STORAGE_SCRIPT,
                          [*(['--apply'] if apply else []), '--json', '--budget-seconds', str(budget)],
                          timeout_seconds=min(600, remaining))
        attempts.append(vars(child))
        if not apply or not host_budget_continuable(child):
            stop = 'terminal' if child.returncode == 0 and not child.timed_out else 'not_safe_to_continue'
            break
        if index + 1 == HOST_MAX_PASSES:
            stop = 'host_pass_limit'
            break
        try:
            if controls is None or host_continuation_controls() != controls:
                stop = 'controls_changed_or_unavailable'
                break
        except (OSError, ValueError):
            stop = 'controls_changed_or_unavailable'
            break
    # Each pass is a fresh subprocess with ordinary discovery, admission and
    # custody. No earlier manifest or deletion instruction is ever replayed.
    return ChildResult(child.name, child.returncode, child.stdout, child.stderr,
                       child.timed_out, tuple(attempts), stop)


def run_steps(*, apply: bool = False) -> list[ChildResult]:
    try:
        activation_receipt = retire_completed_activation(apply=apply)
    except Exception as exc:
        return [ChildResult(name, 1, stderr=f'activation retirement blocked: {type(exc).__name__}: {exc}')
                for name in ('host_storage', 'runtime_releases', 'runtime_promotions', 'approval_a')]
    child_args = ['--apply'] if apply else []
    results = [
        run_host_stage(apply=apply),
        run_child(
            'runtime_releases',
            RUNTIME_RELEASE_RETENTION_SCRIPT,
            child_args,
            {
                'OPENCLAW_RUNTIME_RELEASE_RETENTION_ANNOUNCE_NOOP': '1',
                RUNTIME_RELEASE_APPLY_ENV: '0',
            },
            timeout_seconds=RUNTIME_RELEASE_CHILD_TIMEOUT_SECONDS,
        ),
        run_child(
            'runtime_promotions',
            RUNTIME_PROMOTION_RETENTION_SCRIPT,
            child_args,
            {
                'OPENCLAW_RUNTIME_PROMOTION_RETENTION_ANNOUNCE_NOOP': '1',
                RUNTIME_PROMOTION_APPLY_ENV: '0',
            },
        ),
        run_child(
            'approval_a',
            APPROVAL_A_RETENTION_SCRIPT,
            child_args,
            {
                APPROVAL_A_APPLY_ENV: '0',
            },
            timeout_seconds=APPROVAL_A_CHILD_TIMEOUT_SECONDS,
            command_prefix=ROOT_PYTHON_PREFIX,
        ),
    ]
    if activation_receipt:
        child = results[1]
        results[1] = ChildResult(child.name, child.returncode,
            child.stdout + f'\nACTIVATION_RETIREMENT_REPORT: {activation_receipt}\n',
            child.stderr, child.timed_out)
    return results


DEFAULT_RUN_STEPS = run_steps


REPORT_SCHEMAS = {
    'runtime_releases': 'openclaw.runtime_release_retention.v2',
    'runtime_promotions': 'openclaw.runtime_promotion_retention.v2',
    'approval_a': 'openclaw.approval_a_retention.v1',
}


def host_report(child: ChildResult) -> dict:
    report = json.loads(child.stdout)
    if not isinstance(report, dict):
        raise ValueError('storage cleanup receipt is not an object')
    return report


def validate_host_report(report: dict, *, apply: bool) -> list[dict]:
    results = [
        effect.readback_matches('host_storage_schema', expected='openclaw.storage_prune.v1', actual=report.get('schema')),
        effect.readback_matches('host_storage_mode', expected='apply' if apply else 'preview', actual=report.get('mode')),
        effect.readback_matches('host_storage_terminal', expected=True, actual=report.get('terminal')),
        effect.readback_matches('host_storage_status', expected='ok', actual=report.get('status')),
        effect.readback_matches('host_storage_errors', expected=[], actual=report.get('errors')),
    ]
    if apply:
        unfinished = [row for row in report.get('skipped', []) + report.get('deferred', []) if isinstance(row, dict)
                      and (row.get('not_started') is True or row.get('deferred') is True)]
        results.append(effect.readback_matches(
            'host_storage_unfinished_eligible_records', expected=0, actual=len(unfinished),
        ))
    summary = report.get('summary')
    if summary is not None:
        try:
            from . import openclaw_storage_prune as storage
        except ImportError:
            import openclaw_storage_prune as storage
        results.append(effect.readback_matches('host_storage_count_types', expected=True,
            actual=isinstance(summary, dict) and all(type(value) is int and value >= 0 for value in summary.values())))
        results.append(effect.readback_matches('host_storage_counts', expected=storage.cleanup_summary(report), actual=summary))
    results.extend(validate_host_effects(report))
    return results


def host_path_present(value: str) -> bool:
    # lexists returns False for some unreadable paths. Only ENOENT establishes
    # absence here; a broken symlink is present and other errors stay unknown.
    try:
        Path(value).lstat()
    except FileNotFoundError:
        return False
    return True


def validate_host_effects(report: dict) -> list[dict]:
    """Read completed effects independently, without treating backlog as completion."""
    results = []
    seen = set()
    for index, row in enumerate(report.get('removed', [])):
        value = row.get('path') if isinstance(row, dict) else row
        if not isinstance(value, str) or not Path(value).is_absolute():
            results.append(effect.unreadable(f'host_storage_removed:{index}', 'missing absolute removed path'))
            continue
        if value in seen:
            results.append(effect.unreadable(f'host_storage_removed:{index}', 'duplicate removed target'))
            continue
        seen.add(value)
        results.append(effect.readback_matches(f'host_storage_removed:{index}', expected=False, actual=host_path_present(value)))
    for index, row in enumerate(report.get('removed_simulators', [])):
        value = row.get('path') if isinstance(row, dict) else None
        if not isinstance(value, str) or not Path(value).is_absolute():
            results.append(effect.unreadable(f'host_simulator_removed:{index}', 'missing absolute removed path'))
            continue
        results.append(effect.readback_matches(f'host_simulator_removed:{index}',
            expected=False, actual=host_path_present(value)))
    for index, row in enumerate(report.get('archived_logs', [])):
        try:
            path = Path(row['archive'])
            current = path.lstat()
            valid = (path.is_absolute() and stat.S_ISREG(current.st_mode)
                     and type(row['bytes']) is int and current.st_size == row['bytes']
                     and row.get('captured_inode_removed') is True
                     and isinstance(row.get('sha256'), str)
                     and re.fullmatch(r'[0-9a-f]{64}', row['sha256']) is not None)
            results.append(effect.readback_matches(f'host_log_archived:{index}', expected=True, actual=valid))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            results.append(effect.unreadable(f'host_log_archived:{index}', str(exc)))
    return results


def validate_child_retention_report(
    name: str,
    report: dict,
    *,
    apply: bool,
) -> list[dict]:
    expected_mode = 'apply' if apply else 'dry-run'
    results = [
        effect.readback_matches(
            f'{name}_report_schema',
            expected=REPORT_SCHEMAS[name],
            actual=report.get('schema'),
        ),
        effect.readback_matches(
            f'{name}_report_mode',
            expected=expected_mode,
            actual=report.get('mode'),
        ),
        effect.readback_matches(
            f'{name}_report_terminal',
            expected=True,
            actual=report.get('terminal'),
        ),
        effect.readback_matches(
            f'{name}_report_errors',
            expected=[],
            actual=report.get('errors'),
        ),
    ]
    summary = report.get('summary') if isinstance(report.get('summary'), dict) else {}
    results.append(
        effect.readback_matches(
            f'{name}_error_count',
            expected=0,
            actual=summary.get('error_count'),
        )
    )

    records: list[dict] = []
    for key in ('receipts', 'plugin_cache_receipts', 'protected', 'retained', 'interrupted_retirements'):
        value = report.get(key)
        if isinstance(value, list):
            records.extend(item for item in value if isinstance(item, dict))
    root = report.get('root')
    if isinstance(root, dict):
        records.append(root)

    removed_records = [record for record in records if record.get('state') == 'removed']
    for index, record in enumerate(removed_records):
        record_name = str(record.get('name') or record.get('path') or index)
        if 'after_exists' in record:
            results.append(
                effect.readback_matches(
                    f'{name}_removed_after_exists:{record_name}',
                    expected=False,
                    actual=record.get('after_exists'),
                )
            )
        path_value = record.get('path')
        if path_value:
            target = Path(str(path_value))
            results.append(
                effect.readback_matches(
                    f'{name}_removed_path_absent:{record_name}',
                    expected=False,
                    actual=target.exists() or target.is_symlink(),
                )
            )

    for key in ('protected', 'retained'):
        value = report.get(key)
        if not isinstance(value, list):
            continue
        for index, record in enumerate(value):
            if not isinstance(record, dict) or not record.get('path'):
                continue
            target = Path(str(record['path']))
            results.append(
                effect.object_present(
                    f'{name}_{key}:{record.get("name", index)}',
                    str(target),
                    present=target.exists() and not target.is_symlink(),
                    readable=True,
                )
            )

    removed_names = report.get('removed')
    if isinstance(removed_names, list):
        results.append(
            effect.readback_matches(
                f'{name}_removed_count',
                expected=summary.get('removed_count'),
                actual=len(removed_names),
            )
        )
    return results


def load_retention_effects(results: list[ChildResult], *, apply: bool) -> list[dict]:
    predicates: list[dict] = []
    for child in results:
        if child.name == 'host_storage':
            try:
                predicates.extend(validate_host_report(host_report(child), apply=apply))
                for attempt in child.attempts[:-1]:
                    predicates.extend(validate_host_effects(json.loads(attempt['stdout'])))
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                predicates.append(effect.unreadable('host_storage_report', str(exc)))
            continue
        report_value = status_fields(child.stdout).get('report')
        if not report_value:
            predicates.append(effect.unreadable(f'{child.name}_report', 'child output omitted report path'))
            continue
        report_path = Path(report_value)
        if not report_path.is_absolute():
            report_path = WORKSPACE / report_path
        try:
            report = json.loads(report_path.read_text(encoding='utf-8'))
            if not isinstance(report, dict):
                raise ValueError('report is not a JSON object')
            predicates.extend(validate_child_retention_report(child.name, report, apply=apply))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            predicates.append(
                effect.unreadable(
                    f'{child.name}_report',
                    f'{type(exc).__name__}: {exc}',
                )
            )
    return predicates


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Apply OpenClaw runtime release, promotion, and Approval A '
            'retention. Use --preview to inspect without deleting.'
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        '--human', action='store_true',
        help='Print a concise human-readable outcome; keep diagnostics in the run receipt.',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help=(
            'Retained for callers that pass it explicitly. Applying is the '
            'default; this flag changes nothing on its own.'
        ),
    )
    parser.add_argument(
        '--preview',
        action='store_true',
        help=(
            'Run every child helper in dry-run mode and delete nothing. '
            'Without this flag the run applies verified retention.'
        ),
    )
    args = parser.parse_args(argv)
    # Applying is the default. A nightly job that quietly previews is
    # indistinguishable from a working one until the disk fills up, so the
    # non-deleting mode has to be the one you ask for by name.
    args.apply = not args.preview
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = 'apply' if args.apply else 'preview'
    results = run_steps(apply=args.apply)
    effect_results = (
        load_retention_effects(results, apply=args.apply)
        if run_steps is DEFAULT_RUN_STEPS
        else []
    )
    failed = [child for child in results if child.returncode != 0 or child.timed_out]
    if args.human:
        return emit_human_result(results, effect_results, apply=args.apply)
    summaries = {child.name: child_summary(child) for child in results}

    if failed:
        status_line = (
            'STATUS | what: openclaw retention cleanup | result: retention_blocked'
            + ' | semantic_status: failed'
            + f' | mode: {mode}'
            + f' | deletion_authorized: {str(args.apply).lower()}'
            + f" | runtime_releases: {summaries['runtime_releases']}"
            + f" | runtime_promotions: {summaries['runtime_promotions']}"
            + f" | approval_a: {summaries['approval_a']}"
            + ' | next: inspect the blocked retention step and rerun this same cron job after repair'
        )
        contract_problems = validate_alert_request_vs_output(
            requested_objects=RETENTION_ALERT_CONTRACT['objects'],
            requested_metrics=RETENTION_ALERT_CONTRACT['metrics'],
            output_text=status_line,
        )
        print('OPENCLAW_RETENTION_BLOCKED')
        if contract_problems:
            print('STATUS | what: openclaw retention cleanup | result: retention_alert_contract_blocked | semantic_status: failed | blockers: ' + '; '.join(contract_problems) + ' | next: repair retention alert fields')
        else:
            print(status_line)
        return 1

    if not ANNOUNCE_SUCCESS:
        if effect_results and any(not result.get('satisfied') for result in effect_results):
            print('OPENCLAW_RETENTION_EFFECT_UNSATISFIED')
            return effect.emit(
                effect_results,
                what='run OpenClaw runtime release, promotion, and Approval A retention cleanup',
            )
        print('NO_REPLY')
        return 0

    status_line = (
        'STATUS | what: openclaw retention cleanup | result: retention_ok'
        + ' | semantic_status: ok'
        + f' | mode: {mode}'
        + f' | deletion_authorized: {str(args.apply).lower()}'
        + f" | runtime_releases: {summaries['runtime_releases']}"
        + f" | runtime_promotions: {summaries['runtime_promotions']}"
        + f" | approval_a: {summaries['approval_a']}"
        + (
            ' | next: none'
            if args.apply
            else ' | next: review preview reports; rerun with --apply only when approved'
        )
    )
    contract_problems = validate_alert_request_vs_output(
        requested_objects=RETENTION_ALERT_CONTRACT['objects'],
        requested_metrics=RETENTION_ALERT_CONTRACT['metrics'],
        output_text=status_line,
    )
    if contract_problems:
        print('OPENCLAW_RETENTION_BLOCKED')
        print('STATUS | what: openclaw retention cleanup | result: retention_alert_contract_blocked | semantic_status: failed | blockers: ' + '; '.join(contract_problems) + ' | next: repair retention alert fields')
        return 1

    if effect_results and any(not result.get('satisfied') for result in effect_results):
        print('OPENCLAW_RETENTION_EFFECT_UNSATISFIED')
        return effect.emit(
            effect_results,
            what='run OpenClaw runtime release, promotion, and Approval A retention cleanup',
        )

    print('OPENCLAW_RETENTION_OK')
    print(status_line)
    if effect_results:
        effect_code = effect.emit(
            effect_results,
            what='run OpenClaw runtime release, promotion, and Approval A retention cleanup',
        )
        if effect_code != 0:
            return effect_code
    return 0


def final_free_space() -> dict[str, int]:
    try:
        from . import openclaw_storage_prune as storage
    except ImportError:
        import openclaw_storage_prune as storage
    storage.require_owc_identity()
    space = storage.free_space()
    storage.require_owc_identity()
    if any(type(space.get(key)) is not int or space[key] < 0 for key in ('internal', 'owc')):
        raise ValueError('final both-drive free space is unavailable')
    return space


def verified_cleanup_counts(results: list[ChildResult]) -> tuple[list[tuple[str, int]], int, int]:
    """Count only independently confirmed effects, including earlier partial passes."""
    counts = {'backup archive': 0, 'other item': 0, 'disposable simulator': 0,
              'runtime': 0, 'promotion folder': 0, 'retired execution folder': 0}
    rotated_logs = reclaimed_bytes = 0
    seen = set()
    for child in results:
        if child.name == 'host_storage':
            attempts = child.attempts or (vars(child),)
            for attempt in attempts:
                try:
                    report = json.loads(attempt['stdout'])
                    if not isinstance(report, dict):
                        continue
                    for field in ('removed', 'removed_simulators', 'archived_logs'):
                        for row in report.get(field, []):
                            value = row if isinstance(row, dict) else {'path': row}
                            key = (field, value.get('kind'), value.get('path'), value.get('device'), value.get('inode'))
                            if key in seen or any(not p['satisfied'] for p in validate_host_effects({field: [row]})):
                                continue
                            seen.add(key)
                            allocated = value.get('allocated_bytes')
                            if type(allocated) is int and allocated > 0:
                                reclaimed_bytes += allocated
                            if field == 'archived_logs':
                                rotated_logs += 1
                            elif field == 'removed_simulators':
                                counts['disposable simulator'] += 1
                            elif (value.get('kind') == 'unused-owc-backup-file'
                                  and Path(value.get('original_path', value['path'])).suffix in ('.tar', '.tgz', '.gz', '.zip')):
                                counts['backup archive'] += 1
                            else:
                                counts['other item'] += 1
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    continue
        elif child.name in REPORT_SCHEMAS:
            try:
                path = Path(status_fields(child.stdout)['report'])
                report = json.loads((path if path.is_absolute() else WORKSPACE / path).read_text())
                if any(not p['satisfied'] for p in validate_child_retention_report(child.name, report, apply=True)):
                    continue
                count = report['summary']['removed_count']
                if type(count) is not int or count < 0:
                    continue
                counts[{'runtime_releases': 'runtime', 'runtime_promotions': 'promotion folder',
                        'approval_a': 'retired execution folder'}[child.name]] += count
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
    return list(counts.items()), rotated_logs, reclaimed_bytes


def removal_detail(results: list[ChildResult]) -> tuple[str, int, int]:
    counts, logs, reclaimed = verified_cleanup_counts(results)
    return ', '.join(f'{count:,} {label}{"s" if count != 1 else ""}'
                     for label, count in counts if count), logs, reclaimed


def human_partial_summary(results: list[ChildResult], unsatisfied: list[dict]) -> str:
    labels = {'host_storage': 'Mac and OWC cleanup', 'runtime_releases': 'runtime cleanup',
              'runtime_promotions': 'promotion cleanup', 'approval_a': 'retired execution cleanup'}
    failed = {child.name for child in results if child.returncode != 0 or child.timed_out}
    affected_names = failed | {name for name in labels
        if any(str(item.get('name', '')).startswith(name + '_') for item in unsatisfied)}
    affected = ', '.join(labels.get(name, name) for name in sorted(affected_names)) or 'cleanup verification'
    detail, logs, reclaimed = removal_detail(results)
    progress = ' Verified completed cleanup: removed ' + detail + '.' if detail else ''
    if reclaimed:
        amount = f'{reclaimed / 1024**3:,.2f} GiB' if reclaimed >= 1024**3 else f'{reclaimed / 1024:,.1f} KiB'
        progress += f' Verified host removals reclaimed {amount}.'
    if logs:
        progress += f' Rotated {logs} log{"s" if logs != 1 else ""}.'
    pending = ''
    try:
        host = next(child for child in results if child.name == 'host_storage')
        report = host_report(host)
        summary = report.get('summary')
        try:
            from . import openclaw_storage_prune as storage
        except ImportError:
            import openclaw_storage_prune as storage
        if summary == storage.cleanup_summary(report) and summary['deferred_stages']:
            if summary['deferred_targets']:
                pending = f" {summary['deferred_targets']:,} targets remain deferred across {summary['deferred_stages']} stages."
            else:
                pending = f" Cleanup remains deferred in {summary['deferred_stages']} stages; the remaining target count is unverified."
            if summary['unclassified_deferred_stages']:
                pending += f" {summary['unclassified_deferred_stages']} stages still need target discovery."
    except (OSError, ValueError, TypeError, KeyError, StopIteration, AttributeError):
        pass
    try:
        space = final_free_space()
        free = f' Mac: {space["internal"] / 1024**3:.1f} GiB free; OWC: {space["owc"] / 1024**3:,.0f} GiB free.'
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ImportError, subprocess.SubprocessError):
        free = ' Current free space could not be verified.'
    return f'Daily cleanup incomplete: {affected} did not finish.' + progress + pending + ' Remaining cleanup is unfinished.' + free


def human_success_summary(results: list[ChildResult], *, apply: bool) -> str:
    if apply:
        for child in results:
            if child.name == 'host_storage':
                if any(not row['satisfied'] for row in validate_host_report(host_report(child), apply=True)):
                    raise ValueError('host effects changed before summary')
                for attempt in child.attempts[:-1]:
                    if any(not row['satisfied'] for row in validate_host_effects(json.loads(attempt['stdout']))):
                        raise ValueError('earlier host effects changed before summary')
            elif child.name in REPORT_SCHEMAS:
                value = status_fields(child.stdout).get('report')
                if not value:
                    raise ValueError('runtime effect report missing before summary')
                path = Path(value)
                report = json.loads((path if path.is_absolute() else WORKSPACE / path).read_text())
                if any(not row['satisfied'] for row in validate_child_retention_report(child.name, report, apply=True)):
                    raise ValueError('runtime effects changed before summary')
    detail, rotated_logs, _ = removal_detail(results) if apply else ('', 0, 0)
    space = final_free_space()
    if apply:
        actions = ['removed ' + detail] if detail else []
        if rotated_logs:
            actions.append(f'rotated {rotated_logs} log{"s" if rotated_logs != 1 else ""}')
        action = '; '.join(actions) if actions else 'no eligible items needed removal'
        text = 'Daily cleanup completed: ' + action + '.'
    else:
        text = 'Daily cleanup preview completed; no files were removed.'
    return text + f' Mac: {space["internal"] / 1024**3:.1f} GiB free; OWC: {space["owc"] / 1024**3:,.0f} GiB free.'


def emit_human_result(results: list[ChildResult], predicates: list[dict], *, apply: bool) -> int:
    """Persist the complete outcome before publishing any success sentence."""
    failed_names = {child.name for child in results if child.returncode != 0 or child.timed_out}
    unsatisfied = [item for item in predicates if not item.get('satisfied')]
    summary = None
    if not failed_names and not unsatisfied:
        try:
            summary = human_success_summary(results, apply=apply)
        except (OSError, ValueError, TypeError, KeyError, StopIteration, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
            unreadable = effect.unreadable('daily_outcome_summary', str(exc))
            predicates = [*predicates, unreadable]
            unsatisfied = [unreadable]
    if failed_names or unsatisfied:
        summary = human_partial_summary(results, unsatisfied)
    receipt_root = WORKSPACE / 'artifacts' / 'maintenance_retention'
    receipt_path = receipt_root / f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.json'
    payload = {
        'schema': 'openclaw.maintenance_retention.v1',
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'mode': 'apply' if apply else 'preview', 'terminal': True,
        'status': 'failed' if failed_names or unsatisfied else 'ok',
        'children': [vars(child) for child in results], 'predicates': predicates,
        'outcome_summary': summary,
    }
    pending = receipt_path.with_suffix('.pending')
    try:
        receipt_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, receipt_path)
    except (OSError, TypeError, ValueError):
        print('Daily cleanup needs attention: its completion record could not be saved. Cleanup may be partial; completion is unverified.')
        return 1
    if failed_names or unsatisfied:
        print(summary)
        return 1
    print(summary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
