from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import openclaw_health_audit_cron as cron


def security_audit_json(findings: list[tuple[str, str, str]]) -> str:
    """Build `openclaw security audit --json` output from (checkId, severity, title).

    Shape verified against the live CLI on 2026-08-13: a top-level `summary`
    with critical/warn/info counts, and `findings[]` whose entries carry
    `checkId`, `severity`, `title`, and prose detail.
    """

    summary = {'critical': 0, 'warn': 0, 'info': 0}
    for _, severity, _ in findings:
        summary[severity] += 1
    return json.dumps(
        {
            'ts': 1786579113330,
            'summary': summary,
            'findings': [
                {'checkId': check_id, 'severity': severity, 'title': title, 'detail': 'detail text'}
                for check_id, severity, title in findings
            ],
            'secretDiagnostics': [],
        }
    )


# Synthetic accepted findings exercise exact-severity matching; these are test inputs, not installed policy.
ACCEPTED_FIVE = [
    ('config.insecure_or_dangerous_flags', 'warn', 'Insecure or dangerous config flag enabled'),
    ('tools.exec.security_full_configured', 'warn', 'Exec security=full is configured'),
    ('tools.exec.auto_allow_skills_enabled', 'warn', 'autoAllowSkills is enabled for exec approvals'),
    (
        'tools.exec.allowlist_interpreter_without_strict_inline_eval',
        'warn',
        'Interpreter allowlist entries are missing strictInlineEval hardening',
    ),
    (
        'security.trust_model.multi_user_heuristic',
        'warn',
        'Potential multi-user setup detected (personal-assistant model warning)',
    ),
]

UNENCRYPTED_VOLUME = (
    'fs.unencrypted_state_volume',
    'warn',
    'Session/state data is on an unencrypted volume',
)

CROSS_AGENT_SESSIONS = (
    'security.trust_model.cross_agent_session_access_default',
    'warn',
    'Agents share Gateway-wide session access (default)',
)

INFO_TWO = [
    ('summary.attack_surface', 'info', 'Attack surface summary'),
    ('gateway.tailscale_serve', 'info', 'Tailscale Serve exposure enabled'),
]

SECURITY_AUDIT_ACCEPTED = security_audit_json(ACCEPTED_FIVE + INFO_TWO)

# Synthetic additional finding tests the same policy across process boundaries.
SECURITY_AUDIT_LIVE = security_audit_json([UNENCRYPTED_VOLUME] + ACCEPTED_FIVE + INFO_TWO)

GATEWAY_STATUS_OK = '''
Runtime: running (pid 93460, state active)
Connectivity probe: ok
Capability: read-only
'''

STATUS_DEEP_OK = '''
OpenClaw status
Gateway service │ LaunchAgent installed · loaded · running
Security audit Summary: 0 critical · 5 warn · 2 info
WARN Insecure or dangerous config flag enabled
WARN Exec security=full is configured
WARN autoAllowSkills is enabled for exec approvals
WARN Interpreter allowlist entries are missing strictInlineEval hardening
WARN Potential multi-user setup detected (personal-assistant model warning)
Health
│ Gateway │ reachable │ 26ms
│ Discord │ OK │ ok
'''

TASK_MAINTENANCE_OK = '''{
  "mode": "apply",
  "maintenance": {
    "tasks": {"reconciled": 0, "recovered": 0, "cleanupStamped": 0, "pruned": 0},
    "taskFlows": {"reconciled": 0, "pruned": 0}
  },
  "auditAfter": {
    "total": 0,
    "errors": 0,
    "warnings": 0,
    "byCode": {"stale_running": 0, "lost": 0, "delivery_failed": 0, "missing_cleanup": 0, "inconsistent_timestamps": 0},
    "taskFlows": {"total": 0, "errors": 0, "warnings": 0, "byCode": {"stale_running": 0}}
  }
}'''

TASK_MAINTENANCE_TERMINAL_HISTORY = '''{
  "mode": "apply",
  "maintenance": {
    "tasks": {"reconciled": 0, "recovered": 0, "cleanupStamped": 0, "pruned": 3},
    "taskFlows": {"reconciled": 0, "pruned": 21}
  },
  "auditAfter": {
    "total": 7,
    "errors": 4,
    "warnings": 3,
    "byCode": {"stale_queued": 0, "stale_running": 0, "lost": 4, "delivery_failed": 1, "missing_cleanup": 0, "inconsistent_timestamps": 2},
    "taskFlows": {"total": 0, "errors": 0, "warnings": 0, "byCode": {"restore_failed": 0, "stale_running": 0, "stale_waiting": 0, "stale_blocked": 0, "cancel_stuck": 0, "missing_linked_tasks": 0, "blocked_task_missing": 0, "inconsistent_timestamps": 0}}
  }
}'''

TASK_MAINTENANCE_BLOCKED_FLOWS = '''{
  "mode": "apply",
  "maintenance": {
    "tasks": {"reconciled": 0, "recovered": 0, "cleanupStamped": 0, "pruned": 3},
    "taskFlows": {"reconciled": 0, "pruned": 21}
  },
  "auditAfter": {
    "total": 16,
    "errors": 4,
    "warnings": 12,
    "byCode": {"stale_queued": 0, "stale_running": 0, "lost": 4, "delivery_failed": 1, "missing_cleanup": 0, "inconsistent_timestamps": 2},
    "taskFlows": {"total": 9, "errors": 0, "warnings": 9, "byCode": {"restore_failed": 0, "stale_running": 0, "stale_waiting": 0, "stale_blocked": 9, "cancel_stuck": 0, "missing_linked_tasks": 0, "blocked_task_missing": 0, "inconsistent_timestamps": 0}}
  }
}'''

TASK_MAINTENANCE_UNKNOWN_CODE = '''{
  "mode": "apply",
  "maintenance": {
    "tasks": {"reconciled": 0, "recovered": 0, "cleanupStamped": 0, "pruned": 0},
    "taskFlows": {"reconciled": 0, "pruned": 0}
  },
  "auditAfter": {
    "total": 2,
    "errors": 2,
    "warnings": 0,
    "byCode": {"stale_queued": 0, "stale_running": 0, "lost": 0, "delivery_failed": 0, "missing_cleanup": 0, "inconsistent_timestamps": 0, "orphaned_admission": 2},
    "taskFlows": {"total": 0, "errors": 0, "warnings": 0, "byCode": {"stale_running": 0}}
  }
}'''

TASK_MAINTENANCE_RESIDUE = '''{
  "mode": "apply",
  "maintenance": {
    "tasks": {"reconciled": 0, "recovered": 0, "cleanupStamped": 0, "pruned": 0},
    "taskFlows": {"reconciled": 0, "pruned": 0}
  },
  "auditAfter": {
    "total": 436,
    "errors": 9,
    "warnings": 427,
    "byCode": {"stale_running": 1, "lost": 8, "delivery_failed": 1, "missing_cleanup": 0, "inconsistent_timestamps": 426},
    "taskFlows": {"total": 0, "errors": 0, "warnings": 0, "byCode": {"stale_running": 0}}
  }
}'''


class OpenClawHealthAuditCronTests(unittest.TestCase):
    def assert_public_prose(self, output: str) -> None:
        self.assertTrue(output.strip())
        self.assertEqual(len(output.strip().splitlines()), 1)
        for forbidden in (
            'STATUS |',
            'OPENCLAW_HEALTH_AUDIT_',
            'Gateway Exec',
            'NO_REPLY',
            '/Users/',
            '/Volumes/',
            '⚠️',
            '🧩',
        ):
            self.assertNotIn(forbidden, output)

    def test_daily_compacts_accepted_security_findings(self):
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 0)
        self.assertEqual(
            output,
            "OpenClaw's daily health audit passed. The gateway and Discord are reachable, "
            'the security audit found no new unapproved issues, and task-ledger maintenance completed cleanly.',
        )
        self.assert_public_prose(output)
        self.assertNotIn('WARN Insecure', output)

    def test_gateway_failure_is_a_real_reader_facing_blocker(self):
        run_results = [
            cron.CommandResult(1, 'private gateway diagnostic'),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn('gateway RPC check failed', output)
        self.assertNotIn('private gateway diagnostic', output)
        self.assert_public_prose(output)

    def test_weekly_success_is_natural_prose(self):
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main(['--weekly'])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 0)
        self.assertIn("OpenClaw's weekly deep health audit passed", output)
        self.assert_public_prose(output)

    def test_unaccepted_security_findings_alert(self):
        audit = security_audit_json(
            [('gateway.public_bind', 'critical', 'Gateway is bound to a public interface')]
            + ACCEPTED_FIVE
            + INFO_TWO
        )
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, audit),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn("OpenClaw's daily health audit needs attention", output)
        self.assertIn('security audit found new or changed findings', output)
        self.assertNotIn('gateway.public_bind', output)
        self.assert_public_prose(output)

    def test_terminal_history_inside_retention_does_not_alert(self):
        """Finished rows waiting out their cleanup window are not the operator's problem."""
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_TERMINAL_HISTORY),
            cron.CommandResult(0, TASK_MAINTENANCE_TERMINAL_HISTORY),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 0)
        self.assertIn("OpenClaw's daily health audit passed", output)
        self.assertNotIn('flow_pruned', output)
        self.assertNotIn('retained_history', output)

    def test_weekly_reports_distinct_normal_and_deep_security_issues(self):
        normal = security_audit_json([
            ('models.weak_tier', 'warn', 'private model configuration'),
        ])
        deep = security_audit_json([
            (CROSS_AGENT_SESSIONS[0], 'critical', 'private account configuration'),
        ])
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=[
                 cron.CommandResult(0, GATEWAY_STATUS_OK),
                 cron.CommandResult(0, STATUS_DEEP_OK),
                 cron.CommandResult(0, normal),
                 cron.CommandResult(0, deep),
                 cron.CommandResult(0, TASK_MAINTENANCE_OK),
                 cron.CommandResult(0, TASK_MAINTENANCE_OK),
             ]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main(['--weekly'])
        self.assertEqual(rc, 1)
        self.assertIn('configured model tier (warning)', buf.getvalue())
        self.assertIn('shared access to conversations between agents (critical)', buf.getvalue())
        self.assertNotIn('private', buf.getvalue())
        self.assert_public_prose(buf.getvalue())

    def test_blocked_task_flows_are_reported_because_nothing_prunes_them(self):
        """A blocked TaskFlow is not history: it is residue that only grows.

        Maintenance prunes a flow only once its status is succeeded, failed,
        cancelled, or lost. 'blocked' is none of those, so a blocked flow is
        never pruned and its finding never ages out. Counting it as terminal
        history would hide a set that can only get larger.
        """
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_BLOCKED_FLOWS),
            cron.CommandResult(0, TASK_MAINTENANCE_BLOCKED_FLOWS),
            cron.CommandResult(0, TASK_MAINTENANCE_BLOCKED_FLOWS),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn('task-ledger maintenance left active residue', output)
        # The finished task rows alongside them are still not named.
        self.assertNotIn('lost=', output)
        self.assert_public_prose(output)

    def test_an_unrecognised_finding_code_is_reported_not_dropped(self):
        """A code this script has never heard of must not pass as history."""
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_UNKNOWN_CODE),
            cron.CommandResult(0, TASK_MAINTENANCE_UNKNOWN_CODE),
            cron.CommandResult(0, TASK_MAINTENANCE_UNKNOWN_CODE),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn('task-ledger maintenance left active residue', output)
        self.assertNotIn('orphaned_admission', output)

    def test_repairable_residue_after_two_apply_passes_still_alerts(self):
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_RESIDUE),
            cron.CommandResult(0, TASK_MAINTENANCE_RESIDUE),
            cron.CommandResult(0, TASK_MAINTENANCE_RESIDUE),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn('task-ledger maintenance left active residue', output)
        self.assertNotIn('stale_running', output)
        self.assertNotIn('lost=', output)
        self.assertNotIn('inconsistent_timestamps', output)

    def test_second_apply_pass_runs_when_the_first_leaves_repairable_residue(self):
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_RESIDUE),
            cron.CommandResult(0, TASK_MAINTENANCE_RESIDUE),
            cron.CommandResult(0, TASK_MAINTENANCE_TERMINAL_HISTORY),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results) as runner:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 0)
        self.assertIn("OpenClaw's daily health audit passed", output)
        apply_calls = [
            call for call in runner.call_args_list if '--apply' in call.args[0]
        ]
        self.assertEqual(len(apply_calls), 2)
        self.assertNotIn('flow_pruned', output)

    def test_missing_openclaw_binary_reports_prereq_missing(self):
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn('could not run because the local OpenClaw command was unavailable', output)
        self.assertNotIn('/Users/', output)
        self.assert_public_prose(output)

    def test_real_entrypoint_preserves_audit_status_and_public_output(self):
        """The scheduler's real child path must not turn a failed audit green."""
        root = Path(__file__).resolve().parents[1]
        baseline = {
            'gateway status': (0, GATEWAY_STATUS_OK),
            'status --deep': (0, STATUS_DEEP_OK),
            'security audit --json': (0, SECURITY_AUDIT_LIVE),
            'security audit --deep --json': (0, SECURITY_AUDIT_LIVE),
            'tasks maintenance --json': (0, TASK_MAINTENANCE_OK),
            'tasks maintenance --apply --json': (0, TASK_MAINTENANCE_TERMINAL_HISTORY),
        }
        cases = [
            ('accepted warnings and terminal history', None, None, 0, 'health audit passed'),
            ('accepted rendered fallback', 'security audit --json', (1, 'private diagnostic'), 0, 'health audit passed'),
            ('gateway failure', 'gateway status', (7, 'private diagnostic'), 1, 'gateway RPC check failed'),
            ('runtime timeout', 'status --deep', (124, 'private diagnostic'), 1, 'full runtime and Discord check did not complete'),
            ('maintenance failure', 'tasks maintenance --apply --json', (7, 'private diagnostic'), 1, 'task-ledger maintenance did not complete'),
            ('deep audit failure', 'security audit --deep --json', (7, 'private diagnostic'), 1, 'deep security check did not complete'),
            ('intentional shared sessions', 'security audit --json',
             (0, security_audit_json([CROSS_AGENT_SESSIONS] + ACCEPTED_FIVE)),
             0, 'health audit passed'),
            ('model tier warning', 'security audit --json',
             (0, security_audit_json([
                 ('models.weak_tier', 'warn', 'private diagnostic /Users/private-model'),
                 CROSS_AGENT_SESSIONS,
             ])), 1, 'configured model tier (warning)'),
            ('shared sessions escalation', 'security audit --json',
             (0, security_audit_json([
                 (CROSS_AGENT_SESSIONS[0], 'critical', 'private diagnostic /Users/private-agent'),
             ])), 1, 'shared access to conversations between agents (critical)'),
            ('deep model tier warning', 'security audit --deep --json',
             (0, security_audit_json([
                 ('models.weak_tier', 'warn', 'private diagnostic /Users/private-model'),
             ])), 1, 'configured model tier (warning)'),
            ('unknown private finding', 'security audit --json',
             (0, security_audit_json([
                 ('private diagnostic /Users/private-account', 'critical',
                  'private diagnostic /Volumes/private-config'),
             ])), 1, 'an unrecognized security finding (critical)'),
        ]
        for weekly in (False, True):
            for label, command, replacement, expected_code, expected_text in cases:
                if command == 'security audit --deep --json' and not weekly:
                    continue
                with self.subTest(weekly=weekly, case=label), tempfile.TemporaryDirectory() as raw:
                    tmp = Path(raw)
                    responses = dict(baseline)
                    if command:
                        responses[command] = replacement
                    fake_cli = tmp / 'openclaw-fixture'
                    fake_cli.write_text(
                        '#!/usr/bin/env python3\nimport sys\n'
                        f'responses = {responses!r}\n'
                        'code, output = responses[" ".join(sys.argv[1:])]\n'
                        'print(output)\nraise SystemExit(code)\n',
                        encoding='utf-8',
                    )
                    fake_cli.chmod(0o700)
                    receipts = tmp / 'receipts'
                    fixture_operator = json.loads(Path(os.environ['OPENCLAW_OPERATOR_CONFIG']).read_text())
                    fixture_operator['paths']['openclaw_cli'] = str(fake_cli)
                    fixture_operator_path = tmp / 'operator.json'
                    fixture_operator_path.write_text(json.dumps(fixture_operator))
                    proc = subprocess.run(
                        [
                            sys.executable, str(root / 'scripts/cron_python_entrypoint.py'),
                            '--script', str(root / 'scripts/openclaw_health_audit_cron.py'),
                            '--receipt-dir', str(receipts), '--cwd', str(tmp),
                            *(['--weekly'] if weekly else []),
                        ],
                        env={**os.environ, 'OPENCLAW_BIN': str(fake_cli), 'OPENCLAW_OPERATOR_CONFIG': str(fixture_operator_path)},
                        capture_output=True, text=True, timeout=20, check=False,
                    )
                    self.assertEqual(proc.returncode, expected_code, proc.stdout + proc.stderr)
                    self.assertIn(expected_text, proc.stdout)
                    self.assert_public_prose(proc.stdout)
                    self.assertNotIn('private diagnostic', proc.stdout)
                    self.assertNotIn('models.weak_tier', proc.stdout)
                    self.assertNotIn(CROSS_AGENT_SESSIONS[0], proc.stdout)
                    self.assertEqual(proc.stderr, '')
                    receipt_paths = list(receipts.glob('*.json'))
                    self.assertEqual(len(receipt_paths), 1)
                    receipt = json.loads(receipt_paths[0].read_text(encoding='utf-8'))
                    self.assertEqual(receipt['child']['exit_code'], expected_code)
                    self.assertEqual(receipt['status'], 'completed' if expected_code == 0 else 'failed')


class GatewayStatusParsingTests(unittest.TestCase):
    def test_current_connectivity_probe_label_is_healthy(self) -> None:
        self.assertTrue(cron.gateway_status_healthy(GATEWAY_STATUS_OK))

    def test_rollback_rpc_probe_label_remains_healthy(self) -> None:
        self.assertTrue(
            cron.gateway_status_healthy('RPC probe: OK\nRuntime: running (pid 123, state active)')
        )

    def test_probe_without_running_runtime_is_not_healthy(self) -> None:
        self.assertFalse(cron.gateway_status_healthy('Connectivity probe: OK\nRuntime: stopped'))



class SecurityFailurePresentationTests(unittest.TestCase):
    def test_summary_mismatch_does_not_present_an_untrusted_subset(self):
        data = json.loads(security_audit_json([
            ('models.weak_tier', 'warn', 'private model configuration'),
        ]))
        data['summary']['warn'] = 2
        output = json.dumps(data)
        _, blocker = cron.classify_security(output, '')
        self.assertEqual(
            cron.public_security_problem(output, '', blocker),
            'the security audit result was incomplete',
        )

    def test_unknown_findings_remain_visible_but_never_copy_private_fields(self):
        data = json.loads(security_audit_json([
            (f'private diagnostic {index}', 'warn', '/Users/private-account')
            for index in range(6)
        ]))
        data['findings'][0]['severity'] = '/Volumes/private-severity'
        data['summary']['warn'] = 5
        output = json.dumps(data)
        _, blocker = cron.classify_security(output, '')
        message = cron.public_security_problem(output, '', blocker)
        self.assertIn('an unrecognized security finding (unrecognized severity)', message)
        self.assertIn('3 additional findings', message)
        self.assertNotIn('private', message)
        self.assertLess(len(message), 300)


class SecurityFindingsAreNamedAndAcceptedPerSeverity(unittest.TestCase):
    """
    2026-08-08 and 2026-08-10: the operator's only signal was
    "unaccepted_security_audit_findings critical=0 warn=6 info=2". The one fact
    he needed — that the volume holding session transcripts and credentials is
    unencrypted — was in the audit and not in the alert.
    """

    def classify(self, findings, **kwargs):
        return cron.classify_security(security_audit_json(findings), '', **kwargs)

    def test_a_new_unaccepted_warning_is_named(self):
        new = ('gateway.auth_token_missing', 'warn', 'Gateway is reachable without an auth token')
        note, blocker = self.classify([new] + ACCEPTED_FIVE + [UNENCRYPTED_VOLUME] + INFO_TWO)
        self.assertIsNotNone(blocker)
        self.assertIn('gateway.auth_token_missing', blocker)
        self.assertIn('warn=7', blocker)
        self.assertIn('critical=0 warn=7 info=2', note)

    def test_explicit_fixture_volume_warning_acceptance_is_severity_bound(self):
        """Authorised 2026-07-23 in the OWC primary-storage amendment."""
        note, blocker = self.classify([UNENCRYPTED_VOLUME] + ACCEPTED_FIVE + INFO_TWO)
        self.assertIsNone(blocker)
        self.assertEqual(note, 'accepted_warnings_ignored=6 info=2')

    def test_fixture_shared_access_warning_is_accepted_only_at_warn(self):
        note, blocker = self.classify([CROSS_AGENT_SESSIONS] + ACCEPTED_FIVE)
        self.assertIsNone(blocker)
        self.assertEqual(note, 'accepted_warnings_ignored=6 info=0')
        for severity in ('critical', 'high'):
            with self.subTest(severity=severity):
                findings = [cron.SecurityFinding(
                    CROSS_AGENT_SESSIONS[0], severity, CROSS_AGENT_SESSIONS[2],
                )]
                self.assertEqual(cron.unaccepted_security_findings(findings), findings)

    def test_cross_agent_acceptance_does_not_absorb_a_model_tier_warning(self):
        _, blocker = self.classify([
            CROSS_AGENT_SESSIONS,
            ('models.weak_tier', 'warn', 'Some configured models are below recommended tiers'),
        ])
        self.assertIsNotNone(blocker)
        self.assertIn('models.weak_tier', blocker)
        self.assertNotIn(CROSS_AGENT_SESSIONS[0], blocker)

    def test_cross_agent_rendered_acceptance_keeps_severity_boundary(self):
        for severity, accepted in (('WARN', True), ('CRITICAL', False)):
            with self.subTest(severity=severity):
                _, blocker = cron.classify_security(
                    None, f'Security audit\n{severity} {CROSS_AGENT_SESSIONS[2]}\n',
                )
                self.assertEqual(blocker is None, accepted)

    def test_an_accepted_title_demoted_to_info_cannot_absorb_a_new_warning(self):
        """The defect that made a new warning invisible.

        Five accepted titles are present in the output, one of them at info.
        The old code counted accepted titles anywhere at any severity (5) and
        compared that against the warn total (5), concluded everything was
        ruled on, and reported nothing — while a genuinely new warning sat in
        that warn total.
        """
        demoted = ('tools.exec.security_full_configured', 'info', 'Exec security=full is configured')
        new = ('fs.world_readable_config', 'warn', 'Config file is world-readable')
        findings = [demoted, new] + [f for f in ACCEPTED_FIVE if f[0] != demoted[0]]

        self.assertEqual(sum(1 for f in findings if f[1] == 'warn'), 5)
        self.assertEqual(
            sum(1 for f in findings if f[0] in cron.ACCEPTED_SECURITY_FINDINGS),
            5,
            'the old accepted-title count must equal the warn total, or this test does not reproduce the bug',
        )

        note, blocker = self.classify(findings)
        self.assertIsNotNone(blocker)
        self.assertIn('fs.world_readable_config', blocker)
        self.assertNotIn('tools.exec.security_full_configured', blocker)

    def test_an_accepted_finding_escalated_to_critical_is_still_reported(self):
        escalated = (
            'fs.unencrypted_state_volume',
            'critical',
            'Session/state data is on an unencrypted volume',
        )
        note, blocker = self.classify([escalated] + ACCEPTED_FIVE + INFO_TWO)
        self.assertIsNotNone(blocker)
        self.assertIn('critical=1', blocker)
        self.assertIn('fs.unencrypted_state_volume', blocker)

    def test_an_accepted_check_id_survives_rendered_title_drift(self):
        drifted = (
            'config.insecure_or_dangerous_flags',
            'warn',
            'Insecure or dangerous configuration is enabled',
        )
        note, blocker = self.classify(
            [drifted] + [finding for finding in ACCEPTED_FIVE if finding[0] != drifted[0]] + INFO_TWO
        )
        self.assertIsNone(blocker)
        self.assertEqual(note, 'accepted_warnings_ignored=5 info=2')

    def test_an_accepted_title_with_a_different_check_id_is_not_suppressed(self):
        impostor = (
            'gateway.unrelated_warning',
            'warn',
            'Exec security=full is configured',
        )
        _, blocker = self.classify([impostor] + INFO_TWO)
        self.assertIsNotNone(blocker)
        self.assertIn('gateway.unrelated_warning', blocker)

    def test_a_finding_at_a_severity_this_script_never_heard_of_is_not_dropped(self):
        """A new severity band must not read as "nothing bad".

        The severity vocabulary belongs to the CLI, not to this script. If the
        audit grows a band between "warn" and "critical", a parser that only
        knows critical/warn/info counts zero of everything it recognises and
        reports the run healthy — the same silence the 08-08 and 08-10 alerts
        were about, with the finding removed entirely instead of unnamed.
        """
        payload = {
            'summary': {'critical': 0, 'warn': 0, 'info': 0},
            'findings': [
                {
                    'checkId': 'gateway.creds_world_readable',
                    'severity': 'high',
                    'title': 'Gateway credentials are world-readable',
                }
            ],
        }
        note, blocker = cron.classify_security(json.dumps(payload), '')
        self.assertIsNotNone(blocker)
        self.assertIn('gateway.creds_world_readable', blocker)
        # Counted apart from warn so "critical=0 warn=0" cannot read as clean.
        self.assertIn('other=1', blocker)
        self.assertIn('other=1', note)

    def test_a_summary_band_with_no_parsed_findings_blocks_the_run(self):
        """The audit reporting a band this parser produced nothing for."""
        payload = {'summary': {'critical': 0, 'warn': 0, 'info': 0, 'high': 2}, 'findings': []}
        note, blocker = cron.classify_security(json.dumps(payload), '')
        self.assertIsNotNone(blocker)
        self.assertIn('security_findings_incomplete', blocker)
        self.assertIn('high:summary=2,parsed=0', blocker)

    def test_six_unaccepted_warnings_name_what_fits_and_count_the_rest(self):
        """emit() trims a blocker to 120 chars; the line has to stay self-describing."""
        findings = [UNENCRYPTED_VOLUME] + ACCEPTED_FIVE
        with mock.patch.object(cron, 'ACCEPTED_SECURITY_FINDINGS', {}):
            _, blocker = self.classify(findings + INFO_TWO)

        self.assertLessEqual(len(blocker), 120)
        self.assertEqual(blocker, cron.trim(blocker, 120), 'blocker must survive emit() untouched')
        self.assertEqual(
            blocker,
            'unaccepted_security_audit_findings critical=0 warn=6 '
            'fs.unencrypted_state_volume config.insecure_or_dangerous_flags +4',
        )

    def test_the_weekly_deep_scope_fits_the_same_budget(self):
        findings = [UNENCRYPTED_VOLUME] + ACCEPTED_FIVE
        with mock.patch.object(cron, 'ACCEPTED_SECURITY_FINDINGS', {}):
            _, blocker = self.classify(findings, prefix='deep_')

        self.assertTrue(blocker.startswith('deep_unaccepted_security_audit_findings'))
        self.assertLessEqual(len(blocker), 120)

    def test_criticals_are_named_before_warnings(self):
        critical = ('gateway.public_bind', 'critical', 'Gateway is bound to a public interface')
        with mock.patch.object(cron, 'ACCEPTED_SECURITY_FINDINGS', {}):
            _, blocker = self.classify(ACCEPTED_FIVE + [critical])

        names = blocker.split('warn=5 ', 1)[1]
        self.assertTrue(names.startswith('gateway.public_bind'), names)

    def test_falls_back_to_rendered_titles_when_json_is_unavailable(self):
        """No JSON must mean fewer details, never silence."""
        note, blocker = cron.classify_security(None, STATUS_DEEP_OK)
        self.assertIsNone(blocker)
        # The degraded source is stated rather than passed off as the JSON audit.
        self.assertEqual(note, 'accepted_warnings_ignored=5 info=2 source=status_text')

        text = STATUS_DEEP_OK.replace(
            '0 critical · 5 warn · 2 info', '0 critical · 6 warn · 2 info'
        ).replace(
            'WARN Insecure or dangerous config flag enabled',
            'WARN Insecure or dangerous config flag enabled\nWARN Gateway is reachable without an auth token',
        )
        note, blocker = cron.classify_security(None, text)
        self.assertIsNotNone(blocker)
        self.assertIn('Gateway is reachable without an auth token', blocker)

    def test_a_findings_list_that_disagrees_with_the_summary_is_not_trusted(self):
        """A subset we happen to understand must not read as the whole audit."""
        payload = json.loads(security_audit_json(ACCEPTED_FIVE + INFO_TWO))
        payload['summary']['warn'] = 6
        note, blocker = cron.classify_security(json.dumps(payload), '')
        self.assertIsNotNone(blocker)
        self.assertIn('security_findings_incomplete', blocker)

    def test_an_unusable_audit_is_reported_rather_than_passed(self):
        note, blocker = cron.classify_security(None, 'gateway is fine, nothing to see')
        self.assertEqual(note, 'not_reported')
        self.assertEqual(blocker, 'security_findings_unavailable')


class RunTimeoutIsAFailureNotACrash(unittest.TestCase):
    """
    2026-08-12: an unguarded subprocess.TimeoutExpired propagated out of run(),
    killed the script with a traceback (exit 125), and cron recorded
    lastRunStatus=ok / consecutiveErrors=0. A run in which ZERO health checks
    executed was indistinguishable from a clean one. Same crash took the weekly
    on 2026-08-09.
    """

    def test_a_timed_out_command_reports_failure(self) -> None:
        result = cron.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=2,
        )
        self.assertEqual(result.returncode, cron.COMMAND_TIMEOUT_RETURNCODE)
        self.assertIn("timed out after 2s", result.output)

    def test_a_normal_command_is_unaffected(self) -> None:
        result = cron.run([sys.executable, "-c", "print('fine')"], timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.output, "fine")

if __name__ == '__main__':
    unittest.main()


def test_maintenance_receipts_reject_silent_success_and_stale_runs():
    now = 100_000_000
    jobs = [dict(id=job_id, enabled=True, state=dict(lastRunAtMs=now, lastRunStatus="ok", lastDeliveryStatus="delivered")) for job_id in cron.MAINTENANCE_JOB_IDS]
    jobs[0]["state"]["lastDeliveryStatus"] = "not-delivered"
    jobs[1]["state"]["lastRunAtMs"] = 1
    with mock.patch.object(cron, "run", side_effect=[cron.CommandResult(0, '{"enabled": true}'), cron.CommandResult(0, json.dumps({"jobs": jobs}))]):
        problems = cron.maintenance_problems("fixture", now_ms=now)
    assert problems == ["daily retention has no confirmed report delivery", "workspace cleanup has no recent run"]


def test_maintenance_scheduler_disabled_is_not_healthy():
    with mock.patch.object(cron, "run", return_value=cron.CommandResult(0, '{"enabled": false}')) as runner:
        assert cron.maintenance_problems("fixture") == ["the scheduler is unavailable or disabled"]
    assert runner.call_count == 1


def test_maintenance_receipts_reject_future_timestamp_and_malformed_state():
    now = 100_000_000
    jobs = [dict(id=job_id, enabled=True, state=dict(lastRunAtMs=now, lastRunStatus="ok", lastDeliveryStatus="delivered")) for job_id in cron.MAINTENANCE_JOB_IDS]
    jobs[0]["state"]["lastRunAtMs"] = now + 301_000
    jobs[1]["state"] = ["broken"]
    with mock.patch.object(cron, "run", side_effect=[cron.CommandResult(0, '{"enabled": true}'), cron.CommandResult(0, json.dumps({"jobs": jobs}))]):
        assert cron.maintenance_problems("fixture", now_ms=now) == ["daily retention has no recent run", "workspace cleanup has an unreadable run record"]
