from __future__ import annotations
try:
    from scripts.operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from scripts import gateway_autoheal_cron as cron


def safe_restart_payload(*, result: str = 'scheduled', pid: int = 4242) -> dict:
    safe = result != 'deferred'
    counts = {field: 0 for field in cron.SAFE_RESTART_COUNT_FIELDS}
    blockers = []
    if not safe:
        counts['queueSize'] = 1
        blockers = [
            {
                'kind': 'queue',
                'count': 1,
                'message': '1 queued or active operation(s)',
            }
        ]
    counts['totalActive'] = sum(counts.values())
    payload = {
        'ok': True,
        'result': result,
        'message': {
            'scheduled': 'safe restart requested; gateway will restart momentarily',
            'deferred': 'safe restart requested; gateway will restart after active work drains',
            'coalesced': 'safe restart request joined an existing pending gateway restart',
        }.get(result, 'unknown restart result'),
        'preflight': {
            'safe': safe,
            'counts': counts,
            'blockers': blockers,
            'summary': 'safe to restart now' if safe else 'restart deferred',
        },
        'restart': {
            'ok': True,
            'pid': pid,
            'signal': 'SIGUSR1',
            'delayMs': 0,
            'mode': 'emit',
            'coalesced': result == 'coalesced',
            'cooldownMsApplied': 0,
            'emitHooksQueued': False,
        },
    }
    if blockers:
        payload['warnings'] = ['restart deferred']
    return payload


def safe_restart_output(*, result: str = 'scheduled', pid: int = 4242) -> str:
    return json.dumps(safe_restart_payload(result=result, pid=pid))


def gateway_status_output(pid: int) -> str:
    return json.dumps(
        {
            'service': {'runtime': {'status': 'running', 'pid': pid}},
            'port': {
                'port': 18789,
                'status': 'busy',
                'listeners': [{'pid': pid}],
            },
            'rpc': {'ok': True},
        }
    )


class GatewayAutohealCronTests(unittest.TestCase):
    def test_listener_identity_rejects_supervisor_listener_split(self):
        payload = json.loads(gateway_status_output(4242))
        payload['service']['runtime']['pid'] = 9000

        self.assertIsNone(cron.gateway_listener_pid(json.dumps(payload)))

    def test_missing_openclaw_bin_reports_runtime_prereq_missing(self):
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value=None):
            buf, details = io.StringIO(), io.StringIO()
            with redirect_stdout(buf), redirect_stderr(details):
                rc = cron.main()

        self.assertEqual(rc, 1)
        self.assertEqual(buf.getvalue(), 'The gateway health check could not run because the OpenClaw command is unavailable.\n')
        self.assertEqual(json.loads(details.getvalue())['result'], 'runtime_prereq_missing')
        self.assertIn('configured OpenClaw executable is unavailable', details.getvalue())
        self.assertIn((str(OPERATOR.require_path('paths.openclaw_cli'))), details.getvalue())

    def test_healthy_main_is_quiet_and_never_requests_restart(self):
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/fixture/openclaw'), \
             mock.patch.object(cron, 'probe_health', return_value=(True, 'healthy')), \
             mock.patch.object(cron, 'request_safe_restart') as restart, \
             mock.patch.object(cron, 'observe_replacement') as observe:
            buf, details = io.StringIO(), io.StringIO()
            with redirect_stdout(buf), redirect_stderr(details):
                self.assertEqual(cron.main(), 0)
        self.assertEqual(buf.getvalue(), 'OpenClaw gateway check passed. The gateway is responding normally; no restart was needed.\n')
        self.assertEqual(details.getvalue(), '')
        restart.assert_not_called()
        observe.assert_not_called()

    def test_probe_health_accepts_status_deep_fallback(self):
        run_results = [
            (0, 'Gateway probe incomplete'),
            (0, 'Gateway service      │ LaunchAgent installed · loaded · running\nHealth\n│ Gateway  │ reachable │ 26ms'),
        ]
        with mock.patch.object(cron, 'probe_readyz', return_value=(False, 'readyz_failed=fixture')), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            healthy, summary = cron.probe_health((str(OPERATOR.require_path('paths.openclaw_cli'))))

        self.assertTrue(healthy)
        self.assertIn('status_deep_ok=', summary)

    def test_probe_health_accepts_readyz_without_cli_calls(self):
        with mock.patch.object(cron, 'probe_readyz', return_value=(True, 'readyz_ok=true')), \
             mock.patch.object(cron, 'run') as runner:
            healthy, summary = cron.probe_health((str(OPERATOR.require_path('paths.openclaw_cli'))))

        self.assertTrue(healthy)
        self.assertEqual(summary, 'readyz_ok=true')
        runner.assert_not_called()

    def test_run_returns_bounded_timeout_without_retry(self):
        with mock.patch.object(
            cron.subprocess,
            'run',
            side_effect=cron.subprocess.TimeoutExpired(
                cmd=['openclaw', 'gateway', 'status'],
                timeout=cron.COMMAND_TIMEOUT_SECONDS,
            ),
        ) as runner:
            rc, output = cron.run(['openclaw', 'gateway', 'status'])

        self.assertEqual(rc, 124)
        self.assertIn('TimeoutExpired after 60s', output)
        self.assertEqual(runner.call_count, 1)

    def test_safe_restart_accepts_exact_scheduled_deferred_and_coalesced_contracts(self):
        for result in ('scheduled', 'deferred', 'coalesced'):
            with self.subTest(result=result), mock.patch.object(
                cron,
                'run',
                return_value=(0, safe_restart_output(result=result)),
            ):
                rc, _output, request = cron.request_safe_restart('/opt/openclaw')

            self.assertEqual(rc, 0)
            self.assertTrue(request['requestAccepted'])
            self.assertEqual(request['requestStatus'], result)
            self.assertEqual(request['targetPid'], 4242)

    def test_safe_restart_rejects_inconsistent_or_incomplete_acknowledgements(self):
        scheduled_coalesced = safe_restart_payload(result='scheduled')
        scheduled_coalesced['restart']['coalesced'] = True
        coalesced_not_marked = safe_restart_payload(result='coalesced')
        coalesced_not_marked['restart']['coalesced'] = False
        missing_counts = safe_restart_payload(result='scheduled')
        del missing_counts['preflight']['counts']
        malformed_restart = safe_restart_payload(result='scheduled')
        malformed_restart['restart'] = 'not-an-object'
        windows_supervisor_mode = safe_restart_payload(result='scheduled')
        windows_supervisor_mode['restart']['mode'] = 'supervisor'

        for payload in (
            scheduled_coalesced,
            coalesced_not_marked,
            missing_counts,
            malformed_restart,
            windows_supervisor_mode,
        ):
            with self.subTest(payload=payload), mock.patch.object(
                cron,
                'run',
                return_value=(0, json.dumps(payload)),
            ):
                _rc, _output, request = cron.request_safe_restart('/opt/openclaw')

            self.assertFalse(request['requestAccepted'])

    def test_main_recovers_after_restart_when_postcheck_healthy(self):
        restart_output = safe_restart_output(pid=4242)
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value=(str(OPERATOR.require_path('paths.openclaw_cli')))), \
             mock.patch.object(cron, 'probe_health', return_value=(False, 'gateway_status_rc=1')), \
             mock.patch.object(cron, 'observe_replacement', return_value=(True, 'replacement_pid=4343; readyz_ok=true')) as observer, \
             mock.patch.object(cron, 'run', return_value=(0, restart_output)) as runner:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main()

        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), 'OpenClaw recovered after a safe gateway restart. The replacement is responding normally.\n')
        runner.assert_called_once_with(
            [
                (str(OPERATOR.require_path('paths.openclaw_cli'))),
                'gateway',
                'restart',
                '--json',
                '--safe',
            ]
        )
        observer.assert_called_once_with(
            (str(OPERATOR.require_path('paths.openclaw_cli'))),
            previous_pid=4242,
        )

    def test_main_rejects_ambiguous_restart_acknowledgement_without_retry(self):
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value=(str(OPERATOR.require_path('paths.openclaw_cli')))), \
             mock.patch.object(cron, 'probe_health', side_effect=[(False, 'gateway_status_rc=1'), (False, 'still unhealthy')]), \
             mock.patch.object(cron, 'run', return_value=(0, '{"ok":true}')) as runner:
            buf, details = io.StringIO(), io.StringIO()
            with redirect_stdout(buf), redirect_stderr(details):
                rc = cron.main()

        self.assertEqual(rc, 1)
        self.assertEqual(buf.getvalue(), 'The gateway was unhealthy and recovery could not be confirmed.\n')
        self.assertIn('restart_request_accepted=false', details.getvalue())
        self.assertEqual(runner.call_count, 1)

    def test_main_does_not_treat_request_acknowledgement_as_completed_recovery(self):
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value=(str(OPERATOR.require_path('paths.openclaw_cli')))), \
             mock.patch.object(cron, 'probe_health', return_value=(False, 'gateway_status_rc=1')), \
             mock.patch.object(cron, 'run', return_value=(0, safe_restart_output(result='deferred', pid=4242))), \
             mock.patch.object(
                 cron,
                 'observe_replacement',
                 return_value=(False, 'restart request acknowledged but gateway pid remains 4242'),
             ) as observer:
            buf, details = io.StringIO(), io.StringIO()
            with redirect_stdout(buf), redirect_stderr(details):
                rc = cron.main()

        self.assertEqual(rc, 1)
        self.assertEqual(buf.getvalue(), 'The gateway was unhealthy and recovery could not be confirmed.\n')
        self.assertIn('restart_request_accepted=true', details.getvalue())
        self.assertIn('restart_request_status=deferred', details.getvalue())
        self.assertIn('gateway pid remains 4242', details.getvalue())
        observer.assert_called_once()

    def test_observe_replacement_rejects_health_from_the_old_gateway_pid(self):
        with mock.patch.object(cron, 'run', return_value=(0, gateway_status_output(4242))), \
             mock.patch.object(cron, 'probe_health') as health, \
             mock.patch.object(cron.time, 'monotonic', side_effect=[0.0, 0.0, 421.0]), \
             mock.patch.object(cron.time, 'sleep') as sleeper:
            recovered, summary = cron.observe_replacement(
                (str(OPERATOR.require_path('paths.openclaw_cli'))),
                previous_pid=4242,
            )

        self.assertFalse(recovered)
        self.assertIn('gateway pid remains 4242', summary)
        health.assert_not_called()
        sleeper.assert_not_called()

    def test_observe_replacement_requires_new_direct_launchd_pid_and_health(self):
        with mock.patch.object(cron, 'run', return_value=(0, gateway_status_output(4343))) as runner, \
             mock.patch.object(cron, 'probe_health', return_value=(True, 'readyz_ok=true')) as health, \
             mock.patch.object(cron.time, 'monotonic', return_value=0.0):
            recovered, summary = cron.observe_replacement(
                (str(OPERATOR.require_path('paths.openclaw_cli'))),
                previous_pid=4242,
            )

        self.assertTrue(recovered)
        self.assertEqual(summary, 'replacement_pid=4343; readyz_ok=true')
        runner.assert_called_once_with(
            [(str(OPERATOR.require_path('paths.openclaw_cli'))), 'gateway', 'status', '--json'],
            timeout_seconds=cron.COMMAND_TIMEOUT_SECONDS,
        )
        health.assert_called_once_with(
            (str(OPERATOR.require_path('paths.openclaw_cli'))),
            deadline=cron.RESTART_OBSERVE_TIMEOUT_SECONDS,
        )

    def test_observe_replacement_caps_nested_health_probe_at_absolute_deadline(self):
        clock = {'now': 0.0}
        run_calls = []

        def monotonic():
            return clock['now']

        def bounded_run(command, *, timeout_seconds):
            self.assertGreater(timeout_seconds, 0)
            self.assertLessEqual(clock['now'] + timeout_seconds, 5.0)
            run_calls.append((command, timeout_seconds))
            clock['now'] += 4.5
            return 0, gateway_status_output(4343)

        def bounded_readyz(_url=cron.READYZ_URL, *, timeout_seconds):
            self.assertGreater(timeout_seconds, 0)
            self.assertLessEqual(clock['now'] + timeout_seconds, 5.0)
            clock['now'] += timeout_seconds
            return False, 'readyz_failed=TimeoutError'

        with mock.patch.object(cron, 'run', side_effect=bounded_run), \
             mock.patch.object(cron, 'probe_readyz', side_effect=bounded_readyz), \
             mock.patch.object(cron.time, 'monotonic', side_effect=monotonic), \
             mock.patch.object(cron.time, 'sleep') as sleeper:
            recovered, summary = cron.observe_replacement(
                (str(OPERATOR.require_path('paths.openclaw_cli'))),
                previous_pid=4242,
                timeout_seconds=5,
            )

        self.assertFalse(recovered)
        self.assertEqual(clock['now'], 5.0)
        self.assertIn('health_probe_deadline_exhausted', summary)
        self.assertEqual(len(run_calls), 1)
        sleeper.assert_not_called()

    def test_observe_replacement_caps_poll_sleep_at_absolute_deadline(self):
        clock = {'now': 0.0}

        def monotonic():
            return clock['now']

        def bounded_run(_command, *, timeout_seconds):
            self.assertEqual(timeout_seconds, 1.25)
            clock['now'] += 1.0
            return 0, gateway_status_output(4242)

        def bounded_sleep(seconds):
            self.assertEqual(seconds, 0.25)
            self.assertLessEqual(clock['now'] + seconds, 1.25)
            clock['now'] += seconds

        with mock.patch.object(cron, 'run', side_effect=bounded_run) as runner, \
             mock.patch.object(cron, 'probe_health') as health, \
             mock.patch.object(cron.time, 'monotonic', side_effect=monotonic), \
             mock.patch.object(cron.time, 'sleep', side_effect=bounded_sleep) as sleeper:
            recovered, summary = cron.observe_replacement(
                (str(OPERATOR.require_path('paths.openclaw_cli'))),
                previous_pid=4242,
                timeout_seconds=1.25,
            )

        self.assertFalse(recovered)
        self.assertEqual(clock['now'], 1.25)
        self.assertIn('gateway pid remains 4242', summary)
        self.assertEqual(runner.call_count, 1)
        health.assert_not_called()
        sleeper.assert_called_once_with(0.25)

    def test_gateway_listener_pid_rejects_any_listener_without_valid_pid(self):
        payload = json.loads(gateway_status_output(4343))
        payload['port']['listeners'].append({'address': '127.0.0.1:18789'})
        self.assertIsNone(cron.gateway_listener_pid(json.dumps(payload)))

    def test_real_supervisor_delivers_outcomes_and_retains_failure_details_privately(self):
        source_root = Path(__file__).resolve().parents[1]
        cases = [
            ('healthy', True, False, None, 0, 'OpenClaw gateway check passed. The gateway is responding normally; no restart was needed.'),
            ('recovered', False, True, None, 0,
             'OpenClaw recovered after a safe gateway restart. The replacement is responding normally.'),
            ('unverified', False, False, None, 1,
             'The gateway was unhealthy and recovery could not be confirmed.'),
            ('request-failed', False, False, 'private-fixture-request-failure', 1,
             'The gateway was unhealthy, but its safe restart request could not be confirmed. Recovery is not verified.'),
        ]
        for name, healthy, recovered, request_error, expected_code, expected_message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix='autoheal-reporting-') as raw:
                root = Path(raw)
                launcher = root / 'fixture_autoheal.py'
                launcher.write_text(
                    "import sys\nfrom unittest import mock\n"
                    + 'sys.path.insert(0, ' + repr(str(source_root)) + ')\n'
                    + 'from scripts import gateway_autoheal_cron as cron\n'
                    + "with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/fixture/openclaw'), "
                    + "mock.patch.object(cron, 'probe_health', return_value="
                    + repr((healthy, 'private-fixture-precheck')) + '), '
                    + "mock.patch.object(cron, 'request_safe_restart', "
                    + ('side_effect=RuntimeError(' + repr(request_error) + ')' if request_error else
                       'return_value=' + repr((0, 'private-fixture-restart', {
                           'requestAccepted': True, 'targetPid': 4242, 'requestStatus': 'deferred',
                       }))) + ') as restart, '
                    + "mock.patch.object(cron, 'observe_replacement', return_value="
                    + repr((recovered, 'private-fixture-postcheck')) + ') as observe:\n'
                      '    result = cron.main()\n'
                    + '    assert restart.call_count == ' + str(0 if healthy else 1) + '\n'
                    + '    assert observe.call_count == ' + str(0 if healthy or request_error else 1) + '\n'
                      '    raise SystemExit(result)\n'
                )
                completed = subprocess.run([
                    sys.executable, str(source_root / 'scripts/cron_python_entrypoint.py'),
                    '--receipt-dir', str(root / 'receipts'), '--script', str(launcher),
                    '--what', cron.WHAT, '--cwd', str(root),
                ], capture_output=True, text=True, check=False)
                self.assertEqual(completed.returncode, expected_code, completed.stderr)
                self.assertEqual(completed.stdout, expected_message + '\n')
                self.assertEqual(completed.stderr, '')
                receipt_path = next((root / 'receipts').glob('*.json'))
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt['child']['exit_code'], expected_code)
                self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
                private_stderr = receipt['process_output']['stderr']
                self.assertEqual(private_stderr['present'], expected_code != 0)
                if expected_code:
                    self.assertIn('private-fixture-', private_stderr['content_redacted'])


if __name__ == '__main__':
    unittest.main()
