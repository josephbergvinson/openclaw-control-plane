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
        with mock.patch.dict(os.environ, inherited, clear=False):
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


if __name__ == '__main__':
    unittest.main()
