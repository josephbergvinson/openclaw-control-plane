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


@dataclass(frozen=True)
class ChildResult:
    name: str
    returncode: int
    stdout: str = ''
    stderr: str = ''
    timed_out: bool = False


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


def run_steps(*, apply: bool = False) -> list[ChildResult]:
    try:
        activation_receipt = retire_completed_activation(apply=apply)
    except Exception as exc:
        return [ChildResult(name, 1, stderr=f'activation retirement blocked: {type(exc).__name__}: {exc}')
                for name in ('host_storage', 'runtime_releases', 'runtime_promotions', 'approval_a')]
    child_args = ['--apply'] if apply else []
    results = [
        run_child(
            'host_storage', HOST_STORAGE_SCRIPT,
            [*child_args, '--json', '--budget-seconds', '480'],
            timeout_seconds=600,
        ),
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
    for index, row in enumerate(report.get('removed', [])):
        value = row.get('path') if isinstance(row, dict) else row
        if not isinstance(value, str) or not Path(value).is_absolute():
            results.append(effect.unreadable(f'host_storage_removed:{index}', 'missing absolute removed path'))
            continue
        results.append(effect.readback_matches(f'host_storage_removed:{index}', expected=False, actual=os.path.lexists(value)))
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
            except (ValueError, TypeError) as exc:
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


def emit_human_result(results: list[ChildResult], predicates: list[dict], *, apply: bool) -> int:
    """Persist the complete outcome before publishing any success sentence."""
    failed_names = {child.name for child in results if child.returncode != 0 or child.timed_out}
    unsatisfied = [item for item in predicates if not item.get('satisfied')]
    receipt_root = WORKSPACE / 'artifacts' / 'maintenance_retention'
    receipt_path = receipt_root / f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.json'
    payload = {
        'schema': 'openclaw.maintenance_retention.v1',
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'mode': 'apply' if apply else 'preview', 'terminal': True,
        'status': 'failed' if failed_names or unsatisfied else 'ok',
        'children': [vars(child) for child in results], 'predicates': predicates,
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
    labels = {'host_storage': 'Mac and OWC cleanup', 'runtime_releases': 'runtime cleanup',
              'runtime_promotions': 'promotion cleanup', 'approval_a': 'retired execution cleanup'}
    if failed_names or unsatisfied:
        affected = ', '.join(labels.get(name, name) for name in sorted(failed_names))
        if not affected:
            affected = 'cleanup verification'
        print(f'Daily cleanup needs attention: {affected} did not finish successfully. Completed cleanup is recorded; the next daily run will retry.')
        return 1
    host = next((child for child in results if child.name == 'host_storage'), None)
    freed = ''
    if host:
        try:
            report = host_report(host)
            space = report.get('after_free_bytes', {})
            internal, owc = space.get('internal'), space.get('owc')
            if isinstance(internal, (float, int)) and isinstance(owc, (float, int)):
                freed = f' Free space: Mac {internal / 1024**3:.1f} GiB; OWC {owc / 1024**3:.0f} GiB.'
        except (ValueError, TypeError):
            pass  # The receipt validator, not presentation, determines success.
    print(('Daily cleanup completed.' if apply else 'Daily cleanup preview completed; no files were removed.') + freed)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
