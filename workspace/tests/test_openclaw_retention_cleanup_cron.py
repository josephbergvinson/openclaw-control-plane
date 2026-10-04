from __future__ import annotations
try:
    from scripts.operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import openclaw_retention_cleanup_cron as cron


class OpenClawRetentionChildTimeoutTests(unittest.TestCase):
    generic_timeout_env = 'OPENCLAW_RETENTION_CHILD_TIMEOUT_SECONDS'
    runtime_release_timeout_env = 'OPENCLAW_RETENTION_RUNTIME_RELEASE_CHILD_TIMEOUT_SECONDS'
    approval_a_timeout_env = 'OPENCLAW_RETENTION_APPROVAL_A_CHILD_TIMEOUT_SECONDS'

    def configured_timeouts(self) -> list[float]:
        completed = mock.Mock(returncode=0, stdout='NO_REPLY\n', stderr='')
        with mock.patch.object(cron.subprocess, 'run', return_value=completed) as run:
            cron.run_steps()
        return [call.kwargs['timeout'] for call in run.call_args_list]

    def test_default_timeout_routes_only_runtime_releases_to_900_seconds(self) -> None:
        self.addCleanup(importlib.reload, cron)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.generic_timeout_env, None)
            os.environ.pop(self.runtime_release_timeout_env, None)
            os.environ.pop(self.approval_a_timeout_env, None)
            importlib.reload(cron)

            self.assertEqual(
                self.configured_timeouts(),
                [600.0, 900.0, 300.0, 1200.0],
            )

    def test_generic_and_runtime_release_timeout_overrides_route_independently(self) -> None:
        self.addCleanup(importlib.reload, cron)
        with mock.patch.dict(
            os.environ,
            {
                self.generic_timeout_env: '425',
                self.runtime_release_timeout_env: '1200',
                self.approval_a_timeout_env: '2400',
            },
            clear=False,
        ):
            importlib.reload(cron)

            self.assertEqual(
                self.configured_timeouts(),
                [600.0, 1200.0, 425.0, 2400.0],
            )


class OpenClawRetentionCleanupCronTests(unittest.TestCase):
    def run_main(
        self,
        children: list[cron.ChildResult],
        *,
        apply: bool = False,
    ) -> tuple[int, list[str]]:
        buf = io.StringIO()
        with mock.patch.object(cron, 'run_steps', return_value=children):
            with redirect_stdout(buf):
                rc = cron.main(['--apply'] if apply else [])
        return rc, buf.getvalue().strip().splitlines()

    def test_help_exits_without_running_retention_children(self) -> None:
        buf = io.StringIO()
        with mock.patch.object(cron, 'run_steps') as run_steps:
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as raised:
                    cron.main(['--help'])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn('usage:', buf.getvalue())
        self.assertIn('--apply', buf.getvalue())
        run_steps.assert_not_called()

    def test_abbreviated_or_unknown_flags_exit_without_running_children(self) -> None:
        for argv in (['--a'], ['--app'], ['--unknown']):
            with self.subTest(argv=argv):
                with mock.patch.object(cron, 'run_steps') as run_steps:
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaises(SystemExit) as raised:
                            cron.main(argv)

                self.assertEqual(raised.exception.code, 2)
                run_steps.assert_not_called()

    def test_no_argument_main_applies_by_default(self) -> None:
        children = [
            cron.ChildResult('runtime_releases', 0, 'NO_REPLY\n'),
            cron.ChildResult('runtime_promotions', 0, 'NO_REPLY\n'),
            cron.ChildResult('approval_a', 0, 'NO_REPLY\n'),
        ]
        buf = io.StringIO()
        with mock.patch.object(
            cron,
            'run_steps',
            return_value=children,
        ) as run_steps:
            with redirect_stdout(buf):
                rc = cron.main([])

        self.assertEqual(rc, 0)
        run_steps.assert_called_once_with(apply=True)
        self.assertIn('mode: apply', buf.getvalue())
        self.assertIn('next: none', buf.getvalue())

    def test_preview_argument_is_required_to_skip_deletion(self) -> None:
        children = [
            cron.ChildResult('runtime_releases', 0, 'NO_REPLY\n'),
            cron.ChildResult('runtime_promotions', 0, 'NO_REPLY\n'),
            cron.ChildResult('approval_a', 0, 'NO_REPLY\n'),
        ]
        buf = io.StringIO()
        with mock.patch.object(
            cron,
            'run_steps',
            return_value=children,
        ) as run_steps:
            with redirect_stdout(buf):
                rc = cron.main(['--preview'])

        self.assertEqual(rc, 0)
        run_steps.assert_called_once_with(apply=False)
        self.assertIn('mode: preview', buf.getvalue())
        self.assertIn('deletion_authorized: false', buf.getvalue())

    def test_explicit_apply_argument_still_selects_apply(self) -> None:
        children = [
            cron.ChildResult('runtime_releases', 0, 'NO_REPLY\n'),
            cron.ChildResult('runtime_promotions', 0, 'NO_REPLY\n'),
            cron.ChildResult('approval_a', 0, 'NO_REPLY\n'),
        ]
        buf = io.StringIO()
        with mock.patch.object(
            cron,
            'run_steps',
            return_value=children,
        ) as run_steps:
            with redirect_stdout(buf):
                rc = cron.main(['--apply'])

        self.assertEqual(rc, 0)
        run_steps.assert_called_once_with(apply=True)
        self.assertIn('mode: apply', buf.getvalue())
        self.assertIn('next: none', buf.getvalue())

    def test_success_output_is_one_short_status_line_for_noop_sibling_steps(self) -> None:
        rc, output = self.run_main([
            cron.ChildResult('runtime_releases', 0, 'NO_REPLY\n'),
            cron.ChildResult('runtime_promotions', 0, 'NO_REPLY\n'),
            cron.ChildResult('approval_a', 0, 'NO_REPLY\n'),
        ])

        self.assertEqual(rc, 0)
        self.assertEqual(output[0], 'OPENCLAW_RETENTION_OK')
        self.assertEqual(len(output), 2)
        self.assertIn('result: retention_ok', output[1])
        self.assertIn('semantic_status: ok', output[1])
        self.assertIn('mode: apply', output[1])
        self.assertIn('deletion_authorized: true', output[1])
        self.assertIn('runtime_releases: no_action', output[1])
        self.assertIn('runtime_promotions: no_action', output[1])
        self.assertIn('approval_a: no_action', output[1])
        self.assertLessEqual(len(output[1]), 360)

    def test_success_summarizes_sibling_prune_results_without_raw_long_lines(self) -> None:
        rc, output = self.run_main(
            [
                cron.ChildResult(
                    'runtime_releases',
                    0,
                    'RUNTIME_RELEASE_RETENTION_OK\nSTATUS | result: removed_unprotected_releases | mode: apply | total: 3 | protected: 1 | candidates: 2 | removed: 2 | plugin_caches: total=4,protected=3,candidates=1,removed=1 | reclaimable: 1.25GiB | report: artifacts/runtime_release_retention/latest.json | gateway_restart: not_performed\n',
                ),
                cron.ChildResult(
                    'runtime_promotions',
                    0,
                    'RUNTIME_PROMOTION_RETENTION_OK\nSTATUS | result: removed_old_promotion_artifacts | mode: apply | total: 6 | retained: 4 | candidates: 2 | removed: 2 | reclaimable: 0.75GiB | report: artifacts/runtime_promotion_retention/latest.json | gateway_restart: not_performed\n',
                ),
                cron.ChildResult(
                    'approval_a',
                    0,
                    'APPROVAL_A_RETENTION_OK\nSTATUS | result: removed_stale_approval_a | mode: apply | total: 1 | candidates: 0 | removed: 1 | reclaimed: 18.50GiB | report: artifacts/approval_a_retention/latest.json | gateway_restart: not_performed\n',
                ),
            ],
            apply=True,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(output[0], 'OPENCLAW_RETENTION_OK')
        self.assertIn('mode: apply', output[1])
        self.assertIn('runtime_releases: removed_unprotected_releases', output[1])
        self.assertIn('runtime_promotions: removed_old_promotion_artifacts', output[1])
        self.assertIn('approval_a: removed_stale_approval_a', output[1])
        self.assertIn('retained=4', output[1])
        self.assertIn('removed=2', output[1])
        self.assertIn('reclaimable=1.25GiB', output[1])
        self.assertIn('plugin_caches=total=4,protected=3,candidates=1,removed=1', output[1])
        self.assertIn('reclaimed=18.50GiB', output[1])
        self.assertIn('report=artifacts/runtime_release_retention/latest.json', output[1])
        self.assertIn('report=artifacts/runtime_promotion_retention/latest.json', output[1])
        self.assertIn('report=artifacts/approval_a_retention/latest.json', output[1])
        self.assertLessEqual(len(output[1]), 900)

    def test_success_preserves_unmeasured_sizes_and_observed_free_space_delta(self) -> None:
        for delta in ('-0.01GiB', '+0.25GiB', 'unmeasured'):
            with self.subTest(observed_free_space_delta=delta):
                rc, output = self.run_main(
                    [
                        cron.ChildResult(
                            'runtime_releases',
                            0,
                            'RUNTIME_RELEASE_RETENTION_OK\n'
                            'STATUS | result: removed_unprotected_releases | mode: apply'
                            ' | total: 3 | protected: 1 | candidates: 2 | removed: 2'
                            ' | reclaimable: unmeasured'
                            f' | observed_free_space_delta: {delta}'
                            ' | report: artifacts/runtime_release_retention/latest.json\n',
                        ),
                        cron.ChildResult('runtime_promotions', 0, 'NO_REPLY\n'),
                        cron.ChildResult('approval_a', 0, 'NO_REPLY\n'),
                    ],
                    apply=True,
                )

                self.assertEqual(rc, 0)
                self.assertEqual(output[0], 'OPENCLAW_RETENTION_OK')
                self.assertEqual(
                    cron.status_fields(output[1])['runtime_releases'],
                    'removed_unprotected_releases; total=3, protected=1, candidates=2, '
                    f'removed=2, reclaimable=unmeasured, observed_free_space_delta={delta}, '
                    'report=artifacts/runtime_release_retention/latest.json',
                )
                self.assertNotIn('reclaimed=', output[1])
                self.assertEqual(
                    cron.validate_alert_request_vs_output(
                        requested_objects=['runtime_releases'],
                        requested_metrics=['removed', 'reclaimable', 'observed_free_space_delta'],
                        output_text=output[1],
                    ),
                    [],
                )

    def test_blocked_output_stays_compact_and_returns_failure(self) -> None:
        rc, output = self.run_main([
            cron.ChildResult(
                'runtime_releases',
                1,
                'RUNTIME_RELEASE_RETENTION_BLOCKED\nSTATUS | result: retention_blocked | blockers: protected-release-metadata-unreadable-with-long-detail | next: wait\n',
            ),
            cron.ChildResult('runtime_promotions', 0, 'NO_REPLY\n'),
            cron.ChildResult('approval_a', 0, 'NO_REPLY\n'),
        ])

        self.assertEqual(rc, 1)
        self.assertEqual(output[0], 'OPENCLAW_RETENTION_BLOCKED')
        self.assertIn('result: retention_blocked', output[1])
        self.assertIn('semantic_status: failed', output[1])
        self.assertIn('mode: apply', output[1])
        self.assertIn('deletion_authorized: true', output[1])
        self.assertIn('runtime_releases: retention_blocked; blocker=protected-release-metadata-unreadable', output[1])
        self.assertIn('runtime_promotions: no_action', output[1])
        self.assertIn('approval_a: no_action', output[1])
        self.assertLessEqual(len(output[1]), 460)

    def test_empty_stdout_failure_includes_bounded_sanitized_stderr_blocker(self) -> None:
        child = cron.ChildResult(
            'runtime_releases',
            1,
            stdout='',
            stderr=(
                'runtime release helper failed\n\t'
                '| mode: injected | result: injected '
                + ('diagnostic-detail ' * 8)
            ),
        )

        summary = cron.child_summary(child)

        self.assertTrue(
            summary.startswith(
                'exit_1; blocker=runtime release helper failed '
                '/ mode: injected / result: injected'
            )
        )
        self.assertNotIn('\n', summary)
        self.assertNotIn('\t', summary)
        self.assertNotIn('|', summary)
        self.assertLessEqual(len(summary), cron.INLINE_CHILD_FRAGMENT_LIMIT)

        rc, output = self.run_main([
            child,
            cron.ChildResult('runtime_promotions', 0, 'NO_REPLY\n'),
            cron.ChildResult('approval_a', 0, 'NO_REPLY\n'),
        ])

        self.assertEqual(rc, 1)
        self.assertEqual(output[0], 'OPENCLAW_RETENTION_BLOCKED')
        self.assertIn('result: retention_blocked', output[1])
        self.assertIn(f'runtime_releases: {summary}', output[1])
        self.assertIn('runtime_promotions: no_action', output[1])
        self.assertIn('approval_a: no_action', output[1])
        fields = cron.status_fields(output[1])
        self.assertEqual(fields['result'], 'retention_blocked')
        self.assertEqual(fields['semantic_status'], 'failed')
        self.assertEqual(fields['mode'], 'apply')
        self.assertEqual(fields['deletion_authorized'], 'true')

    def test_failure_formatter_preserves_complete_actionable_field(self) -> None:
        blocker = (
            ('ValueError: operation lock releasePath must resolve to the direct release child ' + str(OPERATOR.require_path('paths.agent_storage_root')) + '/.runtime/Releases/openclaw-2026.4.24-6d642e5bf17-003430ff-3607-4e7f-8b6a--selfcontained before retention can continue')
        )
        child = cron.ChildResult(
            'runtime_releases',
            1,
            stdout='',
            stderr=blocker,
        )

        summary = cron.child_summary(child)

        self.assertEqual(summary, f'exit_1; blocker={blocker}')
        self.assertNotIn('diagnostic_exceeds_inline_budget', summary)

    def test_over_budget_fragment_uses_correlation_instead_of_partial_field(self) -> None:
        diagnostic = 'ValueError: ' + ('sensitive-actionable-segment ' * 80)

        summary = cron.child_fragment(diagnostic)

        self.assertIn('diagnostic_exceeds_inline_budget', summary)
        self.assertIn('correlation=sha256:', summary)
        self.assertNotIn('sensitive-actionable-segment', summary)

    def test_failure_summary_prefers_final_exception_class_and_message(self) -> None:
        child = cron.ChildResult(
            'runtime_releases',
            1,
            stdout='',
            stderr=(
                'Traceback (most recent call last):\n'
                '  File "scripts/openclaw_runtime_release_retention.py", line 344, in collect_process_refs\n'
                'ValueError: cannot inspect authoritative open-file references via global_lsof_field_scan_exact_release_root_filter: TimeoutExpired: command timed out\n'
            ),
        )

        summary = cron.child_summary(child)

        self.assertIn('ValueError: cannot inspect authoritative open-file references', summary)
        self.assertIn('TimeoutExpired', summary)
        self.assertNotIn('line 344', summary)
        self.assertNotIn('|', summary)

    def test_timeout_summary_includes_final_exception_when_available(self) -> None:
        child = cron.ChildResult(
            'runtime_releases',
            124,
            stdout='',
            stderr='Traceback...\nTimeoutExpired: command timed out after 60 seconds\n',
            timed_out=True,
        )

        summary = cron.child_summary(child)

        self.assertIn('timeout; last_exception=TimeoutExpired: command timed out', summary)
        self.assertNotIn('|', summary)

    def test_failed_stdout_fallback_is_pipe_neutral_before_parent_status_interpolation(self) -> None:
        child = cron.ChildResult(
            'runtime_releases',
            1,
            stdout='unstructured child failure | mode: injected\n',
            stderr='child diagnostic',
        )

        summary = cron.child_summary(child)

        self.assertEqual(
            summary,
            'unstructured child failure / mode: injected; blocker=child diagnostic',
        )
        self.assertNotIn('|', summary)

        rc, output = self.run_main([
            child,
            cron.ChildResult('runtime_promotions', 0, 'NO_REPLY\n'),
            cron.ChildResult('approval_a', 0, 'NO_REPLY\n'),
        ])

        self.assertEqual(rc, 1)
        self.assertEqual(output[0], 'OPENCLAW_RETENTION_BLOCKED')
        fields = cron.status_fields(output[1])
        self.assertEqual(fields['result'], 'retention_blocked')
        self.assertEqual(fields['semantic_status'], 'failed')
        self.assertEqual(fields['mode'], 'apply')
        self.assertEqual(fields['deletion_authorized'], 'true')


class OpenClawRetentionApplyBoundaryTests(unittest.TestCase):
    def child_calls(
        self,
        *,
        apply: bool,
        inherited_apply: str = '0',
    ):
        completed = mock.Mock(returncode=0, stdout='NO_REPLY\n', stderr='')
        inherited = {
            cron.RUNTIME_RELEASE_APPLY_ENV: inherited_apply,
            cron.RUNTIME_PROMOTION_APPLY_ENV: inherited_apply,
            cron.APPROVAL_A_APPLY_ENV: inherited_apply,
        }
        with mock.patch.dict(os.environ, inherited, clear=False), mock.patch.object(cron, 'retire_completed_activation', return_value=None):
            with mock.patch.object(
                cron.subprocess,
                'run',
                return_value=completed,
            ) as run:
                cron.run_steps(apply=apply)
        return run.call_args_list

    def test_default_preview_passes_no_apply_flag_and_neutralizes_apply_env(self) -> None:
        calls = self.child_calls(apply=False, inherited_apply='1')

        self.assertEqual(len(calls), 4)
        self.assertIn('openclaw_storage_prune.py', calls[0].args[0][1])
        self.assertIn('--json', calls[0].args[0])
        calls = calls[1:]
        for call in calls:
            self.assertNotIn('--apply', call.args[0])
        self.assertEqual(
            calls[0].kwargs['env'][cron.RUNTIME_RELEASE_APPLY_ENV],
            '0',
        )
        self.assertEqual(
            calls[1].kwargs['env'][cron.RUNTIME_PROMOTION_APPLY_ENV],
            '0',
        )
        self.assertEqual(
            calls[2].kwargs['env'][cron.APPROVAL_A_APPLY_ENV],
            '0',
        )
        self.assertEqual(
            tuple(calls[2].args[0][:3]),
            cron.ROOT_PYTHON_PREFIX,
        )

    def test_report_readback_accepts_valid_zero_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            protected = Path(raw) / 'protected'
            protected.mkdir()
            report = {
                'schema': cron.REPORT_SCHEMAS['runtime_releases'],
                'mode': 'apply',
                'terminal': True,
                'errors': [],
                'summary': {'error_count': 0, 'removed_count': 0},
                'receipts': [],
                'protected': [{'name': 'protected', 'path': str(protected)}],
                'removed': [],
            }
            predicates = cron.validate_child_retention_report(
                'runtime_releases', report, apply=True
            )
            self.assertTrue(all(item['satisfied'] for item in predicates), predicates)

    def test_report_readback_checks_exact_removed_and_protected_identities(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            protected = root / 'protected'
            protected.mkdir()
            removed = root / 'removed'
            report = {
                'schema': cron.REPORT_SCHEMAS['runtime_promotions'],
                'mode': 'apply',
                'terminal': True,
                'errors': [],
                'summary': {'error_count': 0, 'removed_count': 1},
                'receipts': [
                    {
                        'name': 'removed',
                        'path': str(removed),
                        'state': 'removed',
                        'after_exists': False,
                    }
                ],
                'retained': [{'name': 'protected', 'path': str(protected)}],
                'removed': ['removed'],
            }
            predicates = cron.validate_child_retention_report(
                'runtime_promotions', report, apply=True
            )
            self.assertTrue(all(item['satisfied'] for item in predicates), predicates)

            removed.mkdir()
            predicates = cron.validate_child_retention_report(
                'runtime_promotions', report, apply=True
            )
            self.assertTrue(any(not item['satisfied'] for item in predicates))

    def test_report_readback_accepts_null_sizes_without_weakening_effect_checks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            protected = root / 'protected'
            protected.mkdir()
            retained = root / 'retained'
            retained.mkdir()
            report = {
                'schema': 'openclaw.runtime_release_retention.v2',
                'mode': 'apply',
                'terminal': True,
                'errors': [],
                'summary': {
                    'error_count': 0,
                    'removed_count': 1,
                    'removed_bytes': None,
                    'reclaimable_bytes': None,
                    'before_free_bytes': None,
                    'after_free_bytes': None,
                    'observed_free_space_delta_bytes': None,
                },
                'receipts': [{
                    'name': 'removed',
                    'path': str(root / 'removed'),
                    'state': 'removed',
                    'after_exists': False,
                    'before_size_bytes': None,
                }],
                'protected': [{
                    'name': 'protected', 'path': str(protected),
                    'size_bytes': None, 'size_gib': None,
                }],
                'retained': [{
                    'name': 'retained', 'path': str(retained),
                    'size_bytes': None, 'size_gib': None,
                }],
                'removed': ['removed'],
                'filesystem_space': {
                    'path': str(root),
                    'before': {'status': 'failed', 'free_bytes': None, 'error': 'fixture failure'},
                    'after': {'status': 'failed', 'free_bytes': None, 'error': 'fixture failure'},
                },
            }
            # Exercise the JSON null contract, not only an in-memory fixture.
            report = json.loads(json.dumps(report))
            predicates = cron.validate_child_retention_report(
                'runtime_releases', report, apply=True
            )
            self.assertTrue(all(item['satisfied'] for item in predicates), predicates)

            invalid_fields = (
                ('summary', 'removed_count', None, 'runtime_releases_removed_count'),
                ('summary', 'removed_count', '1', 'runtime_releases_removed_count'),
                ('summary', 'removed_count', 2, 'runtime_releases_removed_count'),
                ('receipts', 'after_exists', True, 'runtime_releases_removed_after_exists:removed'),
                ('receipts', 'path', str(protected), 'runtime_releases_removed_path_absent:removed'),
                ('protected', 'path', str(root / 'missing'), 'runtime_releases_protected:protected'),
                ('retained', 'path', str(root / 'missing'), 'runtime_releases_retained:retained'),
            )
            for section, key, value, failed_name in invalid_fields:
                with self.subTest(section=section, key=key, value=value):
                    invalid_report = json.loads(json.dumps(report))
                    record = invalid_report[section]
                    if isinstance(record, list):
                        record = record[0]
                    record[key] = value
                    predicates = cron.validate_child_retention_report(
                        'runtime_releases', invalid_report, apply=True
                    )
                    self.assertEqual(
                        [item['name'] for item in predicates if not item['satisfied']],
                        [failed_name],
                    )

    def test_explicit_apply_passes_apply_flag_but_not_apply_env_authority(self) -> None:
        calls = self.child_calls(apply=True, inherited_apply='1')

        self.assertEqual(len(calls), 4)
        self.assertIn('openclaw_storage_prune.py', calls[0].args[0][1])
        self.assertIn('--json', calls[0].args[0])
        calls = calls[1:]
        for call in calls:
            self.assertEqual(call.args[0].count('--apply'), 1)
        self.assertEqual(
            calls[0].kwargs['env'][cron.RUNTIME_RELEASE_APPLY_ENV],
            '0',
        )
        self.assertEqual(
            calls[1].kwargs['env'][cron.RUNTIME_PROMOTION_APPLY_ENV],
            '0',
        )
        self.assertEqual(
            calls[2].kwargs['env'][cron.APPROVAL_A_APPLY_ENV],
            '0',
        )
        self.assertEqual(
            tuple(calls[2].args[0][:3]),
            cron.ROOT_PYTHON_PREFIX,
        )


class OpenClawRetentionEntrypointIntegrationTests(unittest.TestCase):
    def test_exact_cron_entrypoint_preserves_wrapper_semantic_failure(self) -> None:
        repository = Path(cron.__file__).resolve().parents[1]
        blocker = (
            'ValueError: active promotion operation has mismatched terminal receipt '
            'at artifacts/runtime_promotions/operation-123/terminal-receipt.json'
        )
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            receipt_dir = base / 'receipts'
            fixture = base / 'retention_wrapper_fixture.py'
            fixture.write_text(
                textwrap.dedent(
                    f'''\
                    import sys
                    sys.path.insert(0, {str(repository)!r})
                    from scripts import openclaw_retention_cleanup_cron as cron

                    cron.run_steps = lambda apply: [
                        cron.ChildResult('runtime_releases', 1, stdout='', stderr={blocker!r}),
                        cron.ChildResult('runtime_promotions', 0, stdout='NO_REPLY\\n'),
                        cron.ChildResult('approval_a', 0, stdout='NO_REPLY\\n'),
                    ]
                    raise SystemExit(cron.main(['--apply']))
                    '''
                ),
                encoding='utf-8',
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(repository / 'scripts' / 'cron_python_entrypoint.py'),
                    '--receipt-dir',
                    str(receipt_dir),
                    '--script',
                    str(fixture),
                    '--what',
                    'retention integration fixture',
                    '--cwd',
                    str(repository),
                ],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn('OPENCLAW_RETENTION_BLOCKED', completed.stdout)
            self.assertIn('semantic_status: failed', completed.stdout)
            self.assertIn('deletion_authorized: true', completed.stdout)
            self.assertIn(blocker, completed.stdout)
            self.assertNotIn('diagnostic_exceeds_inline_budget', completed.stdout)
            receipts = list(receipt_dir.glob('cron-entrypoint-*.json'))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text(encoding='utf-8'))
            self.assertEqual(receipt['status'], 'failed')
            self.assertEqual(receipt['child']['exit_code'], 1)


class OpenClawRetentionAlertContractTests(unittest.TestCase):
    def test_wrapper_alert_contract_requires_execution_mode(self) -> None:
        problems = cron.validate_alert_request_vs_output(
            requested_objects=cron.RETENTION_ALERT_CONTRACT['objects'],
            requested_metrics=cron.RETENTION_ALERT_CONTRACT['metrics'],
            output_text=(
                'STATUS | result: retention_ok | '
                'runtime_releases: no_action | '
                'runtime_promotions: no_action | '
                'approval_a: no_action | next: none'
            ),
        )

        self.assertIn(
            'requested metric missing from alert output: mode',
            problems,
        )

    def test_alert_contract_accepts_requested_objects_and_metrics_in_status_line(self) -> None:
        problems = cron.validate_alert_request_vs_output(
            requested_objects=['runtime_releases', 'runtime_promotions'],
            requested_metrics=['result', 'removed', 'reclaimable', 'next'],
            output_text=(
                'STATUS | result: retention_ok | '
                'runtime_releases: removed_unprotected_releases; total=3, removed=2, reclaimable=1GiB | '
                'runtime_promotions: no_action | next: none'
            ),
        )

        self.assertEqual(problems, [])

    def test_alert_contract_rejects_requested_object_absent_from_output(self) -> None:
        problems = cron.validate_alert_request_vs_output(
            requested_objects=['runtime_releases', 'runtime_promotions'],
            requested_metrics=['result', 'next'],
            output_text='STATUS | result: retention_ok | runtime_releases: no_action | next: none',
        )

        self.assertIn('requested object missing from alert output: runtime_promotions', problems)

    def test_alert_contract_rejects_requested_metric_absent_from_output(self) -> None:
        problems = cron.validate_alert_request_vs_output(
            requested_objects=['runtime_releases'],
            requested_metrics=['removed', 'reclaimable'],
            output_text='STATUS | result: retention_ok | runtime_releases: removed_unprotected_releases; total=3 | next: none',
        )

        self.assertIn('requested metric missing from alert output: removed', problems)
        self.assertIn('requested metric missing from alert output: reclaimable', problems)


class HostBudgetContinuationTests(unittest.TestCase):
    def setUp(self):
        from scripts import openclaw_storage_prune as storage
        self.storage = storage
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def payload(self, *, remaining=1, removed=1):
        rows = [self.budget_row(str(self.root / f'pending-{i}'), 'host_candidates')
                for i in range(remaining)]
        report = {'schema': 'openclaw.storage_prune.v1', 'mode': 'apply', 'terminal': True,
                  'status': 'partial' if rows else 'ok', 'skipped': [], 'deferred': rows,
                  'errors': list(rows), 'removed': [{'path': str(self.root / f'removed-{i}'),
                    'original_path': str(self.root / f'removed-{i}'), 'kind': 'fixture-backup',
                    'device':1, 'inode':i+1, 'captured_inode_removed':True, 'allocated_bytes':4096} for i in range(removed)],
                  'candidates':[{'path':str(self.root / f'removed-{i}'), 'kind':'fixture-backup',
                      'device':1, 'inode':i+1} for i in range(removed)]}
        report['summary'] = {'removed_items':removed, 'removed_cache_files':0,
            'deferred_targets':remaining, 'deferred_stages':1 if remaining else 0,
            'budget_deferred_targets':remaining, 'budget_deferred_stages':1 if remaining else 0,
            'uncertain_effects':0, 'unclassified_deferred_stages':0, 'reclaimed_allocated_bytes':4096 * removed}
        return report

    @staticmethod
    def budget_row(path, stage):
        return {'path':path, 'stage':stage, 'cause':'execution_budget',
            'reason':'execution budget exhausted', 'state':'not_started', 'effects':'none',
            'deferred':True, 'not_started':True, 'error':True, 'target_count':1,
            'partially_removed':False, 'removed_entries':0}

    @staticmethod
    def child(report, **kwargs):
        return cron.ChildResult('host_storage', 1 if report['status'] != 'ok' else 0,
                                json.dumps(report), **kwargs)

    def run_host(self, children, *, times=None):
        with mock.patch.object(cron, 'run_child', side_effect=children) as child, \
             mock.patch.object(cron, 'host_continuation_controls', return_value=('same',)), \
             mock.patch.object(cron.time, 'monotonic', side_effect=times) if times else mock.patch.object(cron.time, 'monotonic', return_value=100):
            result = cron.run_host_stage(apply=True)
        return result, child.call_args_list

    def test_fresh_second_pass_preserves_first_receipt_and_final_completion(self):
        first = self.child(self.payload())
        second = self.child(self.payload(remaining=0))
        result, calls = self.run_host([first, second])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, second.stdout)
        self.assertEqual([item['stdout'] for item in result.attempts], [first.stdout, second.stdout])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args, calls[1].args)
        self.assertNotIn('--manifest', calls[1].args[2])

    def test_second_budget_partial_remains_nonzero_without_third_pass(self):
        result, calls = self.run_host([self.child(self.payload()), self.child(self.payload())])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.continuation_stop, 'host_pass_limit')
        self.assertEqual(len(calls), 2)

    def test_remaining_wall_budget_clips_timeout_and_normal_work_budget(self):
        result, calls = self.run_host([self.child(self.payload()), self.child(self.payload(remaining=0))],
                                      times=[100, 100, 1040])
        self.assertEqual(calls[0].kwargs['timeout_seconds'], 600)
        self.assertEqual(calls[1].kwargs['timeout_seconds'], 140)
        self.assertEqual(calls[1].args[2][-1], '140')
        self.assertEqual(result.returncode, 0)

    def test_timeout_malformed_unknown_partial_and_no_progress_stop(self):
        for variant in ('timeout', 'malformed', 'unknown', 'partial', 'no_progress', 'false_count'):
            with self.subTest(variant=variant):
                report = self.payload(removed=0 if variant == 'no_progress' else 1)
                if variant in ('unknown', 'partial'):
                    report['deferred'][0]['effects'] = variant
                if variant == 'false_count':
                    report['summary']['deferred_targets'] = 0
                child = self.child(report, timed_out=variant == 'timeout')
                if variant == 'malformed':
                    child = cron.ChildResult('host_storage', 1, '{bad')
                result, calls = self.run_host([child])
                self.assertEqual(len(calls), 1)
                self.assertEqual(result.returncode, 1)

    def test_control_change_stops_before_second_discovery(self):
        first = self.child(self.payload())
        with mock.patch.object(cron, 'run_child', return_value=first) as child, \
             mock.patch.object(cron, 'host_continuation_controls', side_effect=[('before',), ('after',)]):
            result = cron.run_host_stage(apply=True)
        self.assertEqual(child.call_count, 1)
        self.assertEqual(result.continuation_stop, 'controls_changed_or_unavailable')

    def test_completed_effect_must_be_independently_absent(self):
        report = self.payload()
        Path(report['removed'][0]['path']).mkdir()
        self.assertFalse(cron.host_budget_continuable(self.child(report)))

    def test_restored_zero_requires_matching_current_identity(self):
        path = self.root / 'restored'
        path.mkdir()
        value = path.stat()
        report = self.payload()
        report['deferred'][0].update(path=str(path), state='restored', effects='restored_zero',
            not_started=False, restored_identity={'device':value.st_dev, 'inode':value.st_ino})
        self.assertTrue(cron.host_budget_continuable(self.child(report)))
        report['deferred'][0]['restored_identity']['inode'] += 1
        self.assertFalse(cron.host_budget_continuable(self.child(report)))

    def test_predecessor_budget_does_not_mask_later_failed_effect(self):
        first = self.child(self.payload())
        final = self.child(self.payload(remaining=0))
        result, _ = self.run_host([first, final])
        Path(json.loads(first.stdout)['removed'][0]['path']).mkdir()
        with mock.patch.object(cron, 'final_free_space'):
            with self.assertRaisesRegex(ValueError, 'effects changed'):
                cron.human_success_summary([result], apply=True)

    def test_final_backlog_counts_26_host_and_two_simulator_targets(self):
        report = self.payload(remaining=26)
        report['deferred'].extend(self.budget_row(str(self.root / f'simulator-{i}'), 'simulators')
                                  for i in range(2))
        report['errors'] = list(report['deferred'])
        report['summary'].update(deferred_targets=28, budget_deferred_targets=28,
                                 deferred_stages=2, budget_deferred_stages=2)
        self.assertEqual(report['summary']['deferred_targets'], 28)
        self.assertEqual(report['summary']['deferred_stages'], 2)
        checks = cron.validate_host_report(report, apply=True)
        unfinished = next(item for item in checks if item['name'] == 'host_storage_unfinished_eligible_records')
        self.assertEqual(unfinished['observed_value'], 28)
        self.assertFalse(unfinished['satisfied'])

    def test_run_steps_continues_only_host_stage_with_fresh_invocations(self):
        hosts = iter([self.child(self.payload()), self.child(self.payload(remaining=0))])
        calls = []
        def child(name, script, *args, **kwargs):
            calls.append((name, script, args, kwargs))
            return next(hosts) if name == 'host_storage' else cron.ChildResult(name, 0, 'NO_REPLY')
        with mock.patch.object(cron, 'run_child', side_effect=child), \
             mock.patch.object(cron, 'retire_completed_activation', return_value=None), \
             mock.patch.object(cron, 'host_continuation_controls', return_value=('same',)):
            results = cron.run_steps(apply=True)
        self.assertEqual([call[0] for call in calls],
            ['host_storage', 'host_storage', 'runtime_releases', 'runtime_promotions', 'approval_a'])
        self.assertEqual(results[0].returncode, 0)

    def test_unbound_completed_path_cannot_authorize_continuation(self):
        report = self.payload()
        report['removed'][0]['original_path'] = str(self.root / 'outside-plan')
        self.assertFalse(cron.host_budget_continuable(self.child(report)))

    def test_broken_symlink_is_not_a_completed_removal(self):
        report = self.payload()
        Path(report['removed'][0]['path']).symlink_to(self.root / 'missing-target')
        self.assertFalse(cron.host_budget_continuable(self.child(report)))

    def test_unreadable_completed_path_is_not_absence(self):
        report = self.payload()
        with mock.patch.object(cron.Path, 'lstat', side_effect=PermissionError('unreadable')):
            self.assertFalse(cron.host_budget_continuable(self.child(report)))

    def test_unavailable_controls_permit_first_guarded_pass_but_prevent_continuation(self):
        from scripts.operator_contract import ContractError
        for phase in ('initial', 'readback'):
            for error_type in (ValueError, ContractError):
                with self.subTest(phase=phase, error_type=error_type.__name__):
                    first = self.child(self.payload())
                    error = error_type('fixture control is unavailable')
                    controls = [error] if phase == 'initial' else [('same',), error]
                    with mock.patch.object(cron, 'run_child', return_value=first) as child, \
                         mock.patch.object(cron, 'host_continuation_controls', side_effect=controls), \
                         mock.patch.object(cron.time, 'monotonic', return_value=100):
                        result = cron.run_host_stage(apply=True)
                    self.assertEqual(child.call_count, 1)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, first.stdout)
                    self.assertEqual(len(result.attempts), 1)
                    self.assertEqual(result.continuation_stop, 'controls_changed_or_unavailable')

    def test_portable_controls_bind_separate_activation_result_fence_and_config_source(self):
        current = self.root / 'selectors/current'
        current.parent.mkdir()
        current.symlink_to(self.root / 'releases/selected')
        result = self.root / 'activation-controls/activation-result.json'
        result.parent.mkdir()
        result.write_text('{}')
        consumed = result.with_name('activation-start-consumed.json')
        consumed.write_text('{}')
        config = self.root / 'configuration/operator.json'
        config.parent.mkdir()
        guard = self.root / 'guards/volume.json'
        guard.parent.mkdir()
        guard.write_text('{}')
        script = self.root / 'source/storage-prune.py'
        script.parent.mkdir()
        script.write_text('# fixture producer')
        config.write_text(json.dumps({'paths': {
            'runtime_current_link': str(current), 'activation_result': str(result),
            'volume_guard_contract': str(guard)}}))
        from scripts.operator_contract import load_operator_contract
        operator = load_operator_contract(config)
        with mock.patch.object(cron, 'OPERATOR', operator), \
             mock.patch.object(cron, 'HOST_STORAGE_SCRIPT', script), \
             mock.patch.dict(os.environ, {'OPENCLAW_OPERATOR_CONFIG': str(config)}):
            before = cron.host_continuation_controls()
            paths = {row[0] for row in before}
            self.assertTrue({str(current), str(result), str(consumed), str(config),
                             str(guard), str(script)}.issubset(paths))
            self.assertNotIn(str(current.parent / 'activation-result.json'), paths)
            self.assertNotIn(str(current.parent / 'activation-start-consumed.json'), paths)
            result.write_text('{"changed":true}')
            after_result = cron.host_continuation_controls()
            self.assertNotEqual(before, after_result)
            config.write_text(config.read_text() + '\n')
            self.assertNotEqual(after_result, cron.host_continuation_controls())


if __name__ == '__main__':
    unittest.main()
