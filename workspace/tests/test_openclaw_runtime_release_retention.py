from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from activation_fixture_support import operator_binding

from scripts import openclaw_runtime_release_retention as retention


def make_release(root: Path, name: str, marker: str = 'payload') -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / 'marker.txt').write_text(marker, encoding='utf-8')
    return path


def write_terminalized_active_operation(
    operation: Path,
    release: Path,
    *,
    terminal_state: str = 'failed',
    terminal_operation_id: str | None = None,
) -> tuple[Path, Path]:
    operation.mkdir(parents=True)
    operation_id = operation.name
    authority = 'a' * 64
    lock = operation / 'operation.lock.json'
    terminal = operation / 'terminal-receipt.json'
    lock.write_text(
        json.dumps(
            {
                'kind': retention.PROMOTION_OPERATION_LOCK_KIND,
                'operationId': operation_id,
                'runId': operation_id,
                'state': 'active',
                'authoritySha256': authority,
                'journalSequence': 0,
                'acquiredAt': '2026-07-31T20:35:00Z',
                'releasePath': str(release),
            }
        ),
        encoding='utf-8',
    )
    terminal.write_text(
        json.dumps(
            {
                'kind': retention.PROMOTION_TERMINAL_RECEIPT_KIND,
                'operationId': terminal_operation_id or operation_id,
                'runId': operation_id,
                'state': terminal_state,
                'authoritySha256': authority,
                'journalSequence': 4,
                'journalHeadSha256': 'b' * 64,
                'recordedAt': '2026-07-31T21:09:39Z',
            }
        ),
        encoding='utf-8',
    )
    return lock, terminal


def write_activation_result(path: Path, candidate: Path, rollback: Path) -> None:
    candidate_info = candidate.lstat()
    rollback_info = rollback.lstat()
    path.write_text(
        json.dumps(
            {
                'statesVisited': ['preflight', 'apply', 'verify', 'terminal'],
                'outcome': 'activated',
                'candidateAttemptCount': 1,
                'rollbackAttemptCount': 0,
                'candidate': {
                    'path': str(candidate),
                    'commit': 'a' * 40,
                    'device': candidate_info.st_dev,
                    'inode': candidate_info.st_ino,
                },
                'rollback': {
                    'path': str(rollback),
                    'commit': 'b' * 40,
                    'device': rollback_info.st_dev,
                    'inode': rollback_info.st_ino,
                    'source': 'healthy_loaded_process',
                },
                'verification': {
                    'loaded': {
                        'release': str(candidate),
                        'releaseDevice': candidate_info.st_dev,
                        'releaseInode': candidate_info.st_ino,
                        'argumentsObservedExact': True,
                    },
                    'health': {
                        'healthz': {'accepted': True, 'statusCode': 200},
                        'readyz': {'accepted': True, 'statusCode': 200},
                    },
                },
                'commandEvidence': [{'purpose': 'gateway_kickstart'}],
                'error': None,
            }
        ),
        encoding='utf-8',
    )


class RuntimeReleaseRetentionTests(unittest.TestCase):
    def test_activation_lock_blocks_retention_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / 'activation.lock'
            lock_path.touch(mode=0o600)
            with lock_path.open('r+b') as holder, \
                 operator_binding(retention, 'paths.activation_lock', lock_path), \
                 mock.patch.object(retention, '_run_locked') as run_locked:
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, 'activation is in progress'):
                    retention.run(apply=False, keep_latest=0, min_age_days=0)
                run_locked.assert_not_called()

    def test_valid_activation_result_protects_candidate_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'Releases'
            candidate = make_release(releases, 'openclaw-candidate')
            rollback = make_release(releases, 'openclaw-rollback')
            result = base / 'activation-result.json'
            write_activation_result(result, candidate, rollback)

            refs = retention.collect_activation_result_refs(result, releases)
            records = retention.list_release_records(releases)
            retention.protect_records(records, refs)

            self.assertEqual(
                {(ref.source, ref.path) for ref in refs},
                {
                    ('activation-result:candidate', candidate.resolve()),
                    ('activation-result:rollback', rollback.resolve()),
                },
            )
            self.assertTrue(all(record.protected for record in records))

    def test_invalid_activation_result_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'Releases'
            candidate = make_release(releases, 'openclaw-candidate')
            rollback = make_release(releases, 'openclaw-rollback')
            result = base / 'activation-result.json'
            write_activation_result(result, candidate, rollback)
            payload = json.loads(result.read_text(encoding='utf-8'))
            payload['rollback']['inode'] += 1
            result.write_text(json.dumps(payload), encoding='utf-8')

            with self.assertRaisesRegex(ValueError, 'rollback release identity drifted'):
                retention.collect_activation_result_refs(result, releases)

    def test_dry_run_writes_report_and_does_not_remove_unprotected_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            old = make_release(releases, 'openclaw-2026.1.1-old')

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=[]):
                rc, marker, detail, report = retention.run(apply=False, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_DRY_RUN')
            self.assertIn('result: dry_run_candidates', detail)
            self.assertTrue(old.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['mode'], 'dry-run')
            self.assertFalse(payload['deletion_authorized'])
            self.assertEqual(payload['state'], 'dry_run_complete')
            self.assertTrue(payload['terminal'])
            self.assertEqual(payload['summary']['candidate_count'], 1)
            self.assertEqual(payload['candidates'][0]['action'], 'would_remove')

    def test_release_inventory_skips_reporting_only_size_probe_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            releases = Path(raw) / 'releases'
            make_release(releases, 'openclaw-size-probe-not-required')
            with mock.patch.object(retention, 'MEASURE_RELEASE_SIZE', False), \
                 mock.patch.object(retention, 'du_bytes') as size_probe:
                records = retention.list_release_records(releases)

            self.assertEqual(len(records), 1)
            self.assertIsNone(records[0].size_bytes)
            size_probe.assert_not_called()

    def test_unknown_sizes_remain_null_while_fixture_removal_reports_signed_free_delta(self) -> None:
        for after_blocks in (3, 5, 7):
            with self.subTest(after_blocks=after_blocks), tempfile.TemporaryDirectory() as raw:
                base = Path(raw)
                releases = base / 'releases'
                old = make_release(releases, 'openclaw-unmeasured')
                samples = [
                    mock.Mock(f_bavail=blocks, f_frsize=1024 ** 3)
                    for blocks in (5, after_blocks)
                ]
                with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                     operator_binding(retention, 'paths.runtime_release_retention_artifacts', base / 'artifacts'), \
                     operator_binding(retention, 'paths.workspace', base), \
                     mock.patch.object(retention, 'MEASURE_RELEASE_SIZE', False), \
                     mock.patch.object(retention, 'du_bytes') as size_probe, \
                     mock.patch.object(retention.os, 'statvfs', side_effect=samples) as sample, \
                     mock.patch.object(retention, 'collect_references', return_value=[]):
                    rc, marker, detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

                self.assertEqual(rc, 0)
                self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
                self.assertFalse(old.exists())
                size_probe.assert_not_called()
                self.assertEqual(sample.call_args_list, [mock.call(releases), mock.call(releases)])
                payload = json.loads(report.read_text(encoding='utf-8'))
                self.assertEqual(payload['schema'], 'openclaw.runtime_release_retention.v2')
                self.assertEqual(payload['summary']['removed_count'], 1)
                self.assertIsNone(payload['summary']['removed_bytes'])
                self.assertIsNone(payload['summary']['reclaimable_bytes'])
                self.assertIsNone(payload['before']['releases'][0]['size_bytes'])
                self.assertIsNone(payload['receipts'][0]['before_size_bytes'])
                self.assertIsNone(payload['candidates'][0]['size_bytes'])
                self.assertIsNone(payload['candidates'][0]['size_gib'])
                self.assertEqual(payload['summary']['before_free_bytes'], 5 * 1024 ** 3)
                self.assertEqual(payload['summary']['after_free_bytes'], after_blocks * 1024 ** 3)
                self.assertEqual(
                    payload['summary']['observed_free_space_delta_bytes'],
                    (after_blocks - 5) * 1024 ** 3,
                )
                self.assertEqual(payload['filesystem_space']['before']['status'], 'measured')
                self.assertEqual(payload['filesystem_space']['after']['status'], 'measured')
                self.assertIn('reclaimable: unmeasured', detail)
                self.assertIn(f'observed_free_space_delta: {after_blocks - 5:.2f}GiB', detail)

    def test_size_aggregates_distinguish_empty_measured_zero_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            releases = Path(raw) / 'releases'
            make_release(releases, 'openclaw-size-a')
            make_release(releases, 'openclaw-size-b')
            records = retention.list_release_records(releases)
            self.assertEqual(retention.total_size_bytes([]), 0)
            records[0].size_bytes = 0
            records[1].size_bytes = 1024
            self.assertEqual(retention.total_size_bytes(records[:1]), 0)
            self.assertEqual(retention.total_size_bytes(records), 1024)
            records[1].size_bytes = None
            self.assertIsNone(retention.total_size_bytes(records))
            self.assertEqual(retention.format_gib(None), 'unmeasured')
            self.assertEqual(retention.format_gib(0), '0.00GiB')
            self.assertEqual(retention.format_gib(1024 ** 3), '1.00GiB')

    def test_failed_free_space_sample_is_not_zero_or_a_deletion_guard(self) -> None:
        for failed_index in (0, 1):
            with self.subTest(failed_index=failed_index), tempfile.TemporaryDirectory() as raw:
                base = Path(raw)
                releases = base / 'releases'
                old = make_release(releases, 'openclaw-reporting-only')
                samples = [mock.Mock(f_bavail=5, f_frsize=1024 ** 3)] * 2
                samples[failed_index] = OSError('fixture sample unavailable')
                with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                     operator_binding(retention, 'paths.runtime_release_retention_artifacts', base / 'artifacts'), \
                     operator_binding(retention, 'paths.workspace', base), \
                     mock.patch.object(retention.os, 'statvfs', side_effect=samples) as sample, \
                     mock.patch.object(retention, 'collect_references', return_value=[]):
                    rc, marker, detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

                self.assertEqual(rc, 0)
                self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
                self.assertFalse(old.exists())
                self.assertEqual(sample.call_count, 2)
                payload = json.loads(report.read_text(encoding='utf-8'))
                phase = ('before', 'after')[failed_index]
                self.assertEqual(payload['filesystem_space'][phase]['status'], 'failed')
                self.assertIn('OSError', payload['filesystem_space'][phase]['error'])
                self.assertIsNone(payload['summary'][f'{phase}_free_bytes'])
                self.assertIsNone(payload['summary']['observed_free_space_delta_bytes'])
                self.assertIn('observed_free_space_delta: unmeasured', detail)
                self.assertEqual(payload['summary']['removed_count'], 1)

    def test_dry_run_with_zero_candidates_explicitly_proves_no_deletion_authority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            retained = make_release(releases, 'openclaw-retained')
            refs = [retention.Reference('fixture-protected', retained.resolve())]

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=refs):
                rc, marker, detail, report = retention.run(apply=False, keep_latest=0, min_age_days=0)

            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            self.assertIn('deletion_authorized: false', detail)
            self.assertFalse(payload['deletion_authorized'])
            self.assertEqual(payload['summary']['candidate_count'], 0)
            self.assertEqual(payload['summary']['removed_count'], 0)
            self.assertEqual(payload['summary']['reclaimable_bytes'], 0)
            self.assertEqual(payload['summary']['removed_bytes'], 0)
            self.assertTrue(retained.exists())

    def test_default_window_preserves_recent_and_latest_releases(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            first = make_release(releases, 'openclaw-recent-a')
            second = make_release(releases, 'openclaw-recent-b')

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=[]):
                rc, marker, _detail, report = retention.run(apply=True)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['policy']['keep_latest'], 2)
            self.assertEqual(payload['policy']['min_age_days'], 3)
            self.assertEqual(payload['summary']['candidate_count'], 0)
            for item in payload['protected']:
                self.assertIn('younger-than-3d', item['reasons'])

    def test_unreadable_reference_still_fails_closed(self) -> None:
        """Only unnameable strings are treated as absent references.

        ENAMETOOLONG means the string is not a name any filesystem can hold.
        A permission or I/O error means the opposite: the reference may well
        name a live release, and we could not read it. Answering "not a path"
        there would drop the realpath match and let the run delete something
        a dependency still points at, so the probe must keep raising.
        """
        denied = PermissionError(errno.EACCES, 'Permission denied')
        with mock.patch.object(Path, 'exists', side_effect=denied):
            with self.assertRaises(PermissionError):
                retention.reference_names_a_real_path(Path('/some/unreadable/ref'))

        with mock.patch.object(
            Path,
            'exists',
            side_effect=OSError(errno.ENAMETOOLONG, 'File name too long'),
        ):
            self.assertFalse(retention.reference_names_a_real_path(Path('x' * 12000)))

    def test_unnameable_process_argv_reference_does_not_abort_classification(self) -> None:
        """One long command line on the machine must not stop retention.

        `ps` argv references are whole command lines. Probing one that exceeds
        the filesystem name limit raises ENAMETOOLONG, which used to escape
        classification and mark every candidate remove_failed.
        """
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            first = make_release(releases, 'openclaw-2026.1.1-first')
            second = make_release(releases, 'openclaw-2026.1.1-second')
            # A command line far past PATH_MAX, exactly as a long agent
            # invocation appears in `ps -axo pid=,command=` output.
            oversized = '12334 claude -p ' + ('x' * 12000)
            references = [retention.Reference(f'process-argv:{oversized[:180]}', Path(oversized))]

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=references):
                rc, marker, _detail, report = retention.run(
                    apply=True,
                    keep_latest=0,
                    min_age_days=0,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            self.assertEqual(sum(path.exists() for path in (first, second)), 0)
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['state'], 'apply_complete')
            self.assertEqual(payload['summary']['error_count'], 0)
            self.assertEqual(payload['summary']['removed_count'], 2)

    def test_release_named_in_a_long_command_line_stays_protected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            live = make_release(releases, 'openclaw-2026.1.1-live')
            stale = make_release(releases, 'openclaw-2026.1.1-stale')
            oversized = f'12334 node {live}/dist/index.js gateway ' + ('x' * 12000)
            references = [retention.Reference(f'process-argv:{oversized[:180]}', Path(oversized))]

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=references):
                rc, marker, _detail, report = retention.run(
                    apply=True,
                    keep_latest=0,
                    min_age_days=0,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            # String containment still protects the release the process is running.
            self.assertTrue(live.exists())
            self.assertFalse(stale.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['error_count'], 0)
            self.assertEqual(payload['summary']['removed_count'], 1)

    def test_apply_drains_every_release_candidate_and_rescans_before_each(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            first = make_release(releases, 'openclaw-2026.1.1-first')
            second = make_release(releases, 'openclaw-2026.1.1-second')

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=[]) as collector:
                rc, marker, _detail, report = retention.run(
                    apply=True,
                    keep_latest=0,
                    min_age_days=0,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            # One pre-cache baseline, one post-cache baseline, and one complete
            # authoritative rescan per candidate. Cache writers do not share a
            # generation token, so each release still gets a fresh cache scan;
            # the old extra post-delete rebaseline is no longer needed.
            self.assertEqual(collector.call_count, 4)
            self.assertEqual(sum(path.exists() for path in (first, second)), 0)
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertTrue(payload['terminal'])
            self.assertEqual(payload['state'], 'apply_complete')
            self.assertEqual(payload['summary']['removed_count'], 2)
            self.assertEqual(payload['summary']['deferred_count'], 0)
            self.assertEqual(payload['summary']['in_progress_count'], 0)

    def test_apply_honors_an_explicit_mutation_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            first = make_release(releases, 'openclaw-2026.1.1-first')
            second = make_release(releases, 'openclaw-2026.1.1-second')

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'MAX_MUTATION_ATTEMPTS_PER_RUN', 1), \
                 mock.patch.object(retention, 'collect_references', return_value=[]):
                rc, marker, _detail, report = retention.run(
                    apply=True,
                    keep_latest=0,
                    min_age_days=0,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            self.assertEqual(sum(path.exists() for path in (first, second)), 1)
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['removed_count'], 1)
            self.assertEqual(payload['summary']['deferred_count'], 1)

    def test_apply_stops_cleanly_when_the_wall_clock_budget_is_spent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            first = make_release(releases, 'openclaw-2026.1.1-first')
            second = make_release(releases, 'openclaw-2026.1.1-second')

            # An already-expired deadline must defer every candidate and still
            # write a terminal report, instead of running until the cron child
            # timeout kills the process with no report at all.
            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'mutation_deadline', return_value=-1.0), \
                 mock.patch.object(retention, 'collect_references', return_value=[]):
                rc, marker, _detail, report = retention.run(
                    apply=True,
                    keep_latest=0,
                    min_age_days=0,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            self.assertEqual(sum(path.exists() for path in (first, second)), 2)
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertTrue(payload['terminal'])
            self.assertEqual(payload['state'], 'apply_complete')
            self.assertEqual(payload['summary']['removed_count'], 0)
            self.assertEqual(payload['summary']['deferred_count'], 2)

    def test_apply_deadline_starts_before_initial_release_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            tracker = mock.Mock()
            deadline = mock.Mock(return_value=321.0)
            inventory = mock.Mock(return_value=[])
            tracker.attach_mock(deadline, 'deadline')
            tracker.attach_mock(inventory, 'inventory')

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'mutation_deadline', deadline), \
                 mock.patch.object(retention, 'list_release_records', inventory), \
                 mock.patch.object(retention, 'collect_references', return_value=[]):
                retention._run_locked(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(
                tracker.mock_calls[:2],
                [
                    mock.call.deadline(),
                    mock.call.inventory(releases, deadline=321.0),
                ],
            )

    def test_initial_scan_timeout_writes_terminal_blocked_report_without_delete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            cli_root = base / 'cli'
            make_release(releases, 'openclaw-timeout-target')
            cli_root.mkdir()

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 operator_binding(retention, 'paths.cli_root', cli_root), \
                 mock.patch.object(retention, 'collect_current_symlink_ref', return_value=[]), \
                 mock.patch.object(retention, 'collect_activation_result_refs', return_value=[]), \
                 mock.patch.object(retention, 'collect_launchagent_refs', return_value=[]), \
                 mock.patch.object(
                     retention.subprocess,
                     'run',
                     side_effect=retention.subprocess.TimeoutExpired(
                         cmd=['/usr/bin/find', str(cli_root)],
                         timeout=0.5,
                     ),
                 ) as scan, \
                 mock.patch.object(retention.shutil, 'rmtree') as remove:
                rc, marker, _detail, report = retention.run(
                    apply=True,
                    keep_latest=0,
                    min_age_days=0,
                )

            self.assertEqual(rc, 1)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_BLOCKED')
            remove.assert_not_called()
            self.assertEqual(scan.call_args.args[0][0], '/usr/bin/find')
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertTrue(payload['terminal'])
            self.assertEqual(payload['state'], 'classification_blocked')
            self.assertEqual(payload['summary']['run_error_count'], 1)
            self.assertIn(
                'initial authoritative classification failed',
                payload['errors'][0]['error'],
            )
            self.assertIn('TimeoutExpired', payload['errors'][0]['error'])

    def test_references_appearing_after_the_rebaseline_still_block_deletion(self) -> None:
        """A reference appearing between classification and deletion wins."""
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            release = make_release(releases, 'openclaw-moving-reference-target')
            now = datetime(2026, 8, 1, tzinfo=timezone.utc)
            old = (now - timedelta(days=5)).timestamp()
            os.utime(release, (old, old))

            calls = {'n': 0}

            def shifting_references(**_kwargs):
                # Empty through classification and the pre-delete scan, then a
                # new reference appears in the per-record rescan.
                calls['n'] += 1
                if calls['n'] <= 2:
                    return []
                return [retention.Reference('late-arriving-reference', release)]

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'collect_references', side_effect=shifting_references):
                _rc, _marker, _detail, report = retention.run(
                    apply=True,
                    keep_latest=0,
                    min_age_days=3,
                )

            self.assertTrue(release.exists(), 'a release whose references moved must survive')
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['removed_count'], 0)

    def test_apply_removes_a_sealed_readonly_release(self) -> None:
        """The promotion seals releases read-only; retention must still reclaim them.

        release_sealed leaves a release's directories as dr-xr-xr-x. Unlinking a
        file needs write permission on its PARENT directory, so shutil.rmtree
        failed on every sealed release with PermissionError on the first
        dot-file it reached. That is why release retention had never actually
        removed one.
        """
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            release = make_release(releases, 'openclaw-sealed-target')
            nested = release / 'extensions'
            nested.mkdir(parents=True, exist_ok=True)
            (nested / '.npmignore').write_text('sealed\n', encoding='utf-8')
            now = datetime(2026, 8, 1, tzinfo=timezone.utc)
            old = (now - timedelta(days=5)).timestamp()
            os.utime(release, (old, old))
            # Seal it exactly as the promotion does: read-only directories.
            os.chmod(nested, 0o555)
            os.chmod(release, 0o555)
            try:
                with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                     operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                     operator_binding(retention, 'paths.workspace', base), \
                     mock.patch.object(retention, 'utc_now', return_value=now), \
                     mock.patch.object(retention, 'collect_references', return_value=[]):
                    _rc, _marker, _detail, report = retention.run(
                        apply=True,
                        keep_latest=0,
                        min_age_days=3,
                    )
                self.assertFalse(release.exists(), 'a sealed release must still be reclaimable')
                payload = json.loads(report.read_text(encoding='utf-8'))
                self.assertEqual(payload['summary']['removed_count'], 1)
                self.assertEqual(payload['summary']['error_count'], 0)
            finally:
                for path in (nested, release):
                    if path.exists():
                        os.chmod(path, 0o755)

    def test_write_access_restore_rejects_child_swapped_to_external_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            release = base / 'openclaw-candidate'
            child = release / 'child'
            outside = base / 'outside'
            child.mkdir(parents=True)
            outside.mkdir()
            (outside / 'private.txt').write_text('outside\n', encoding='utf-8')
            os.chmod(child, 0o555)
            os.chmod(outside, 0o555)
            outside_mode = stat.S_IMODE(outside.stat().st_mode)
            real_open = os.open
            swapped = False

            def swap_then_open(
                path,
                flags,
                mode=0o777,
                *,
                dir_fd=None,
            ):
                nonlocal swapped
                if path == child.name and dir_fd is not None and not swapped:
                    swapped = True
                    child.rmdir()
                    child.symlink_to(outside, target_is_directory=True)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            try:
                with mock.patch.object(retention.os, 'open', side_effect=swap_then_open):
                    with self.assertRaisesRegex(
                        ValueError,
                        'cannot open verified child directory',
                    ):
                        retention.restore_directory_write_access(release)
                self.assertTrue(swapped)
                self.assertTrue(child.is_symlink())
                self.assertEqual(stat.S_IMODE(outside.stat().st_mode), outside_mode)
            finally:
                os.chmod(outside, 0o755)

    def test_symlink_scan_timeout_is_capped_by_remaining_run_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            completed = mock.Mock(returncode=0, stdout=b'', stderr=b'')
            with mock.patch.object(retention.time, 'monotonic', return_value=100.0), \
                 mock.patch.object(retention.subprocess, 'run', return_value=completed) as run:
                self.assertEqual(
                    retention.collect_symlink_refs([root], deadline=101.25),
                    [],
                )
            self.assertEqual(run.call_args.kwargs['timeout'], 1.25)

            with mock.patch.object(retention.time, 'monotonic', return_value=102.0), \
                 mock.patch.object(retention.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'run deadline exhausted'):
                    retention.collect_symlink_refs([root], deadline=101.25)
            run.assert_not_called()

    def test_symlink_scan_deadline_is_checked_after_find(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            link = root / 'link'
            completed = mock.Mock(
                returncode=0,
                stdout=os.fsencode(link) + b'\0',
                stderr=b'',
            )
            with mock.patch.object(
                retention.time,
                'monotonic',
                side_effect=[100.0, 101.0],
            ), mock.patch.object(
                retention.subprocess,
                'run',
                return_value=completed,
            ), mock.patch.object(retention, 'realpath') as resolve:
                with self.assertRaisesRegex(ValueError, 'deadline exhausted after find'):
                    retention.collect_symlink_refs([root], deadline=101.0)
            resolve.assert_not_called()

    def test_symlink_scan_deadline_is_checked_before_each_realpath(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / 'first'
            second = root / 'second'
            completed = mock.Mock(
                returncode=0,
                stdout=os.fsencode(first) + b'\0' + os.fsencode(second) + b'\0',
                stderr=b'',
            )
            with mock.patch.object(
                retention.time,
                'monotonic',
                side_effect=[100.0, 100.25, 100.5, 101.0],
            ), mock.patch.object(
                retention.subprocess,
                'run',
                return_value=completed,
            ), mock.patch.object(
                retention,
                'realpath',
                side_effect=lambda path: Path(path),
            ) as resolve:
                with self.assertRaisesRegex(ValueError, 'deadline exhausted while resolving'):
                    retention.collect_symlink_refs([root], deadline=101.0)
            resolve.assert_called_once_with(first)

    def test_reference_collection_never_traverses_legacy_plugin_cache_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cli_root = Path(raw) / 'cli'
            cli_root.mkdir()
            with operator_binding(retention, 'paths.cli_root', cli_root), \
                 mock.patch.object(retention, 'collect_current_symlink_ref', return_value=[]), \
                 mock.patch.object(retention, 'collect_activation_result_refs', return_value=[]), \
                 mock.patch.object(retention, 'collect_launchagent_refs', return_value=[]), \
                 mock.patch.object(retention, 'collect_process_refs', return_value=[]), \
                 mock.patch.object(retention, 'collect_promotion_dependency_refs', return_value=[]), \
                 mock.patch.object(retention, 'collect_unfinished_release_refs', return_value=[]) as unfinished, \
                 mock.patch.object(retention, 'collect_symlink_refs', return_value=[]) as scan:
                self.assertEqual(retention.collect_references(deadline=123.0), [])

            unfinished.assert_called_once_with()
            scan.assert_called_once_with(
                [cli_root],
                required_roots=[cli_root],
                deadline=123.0,
            )
            self.assertNotIn(
                'plugin-runtime-deps',
                repr(scan.call_args_list),
            )

    def test_process_scan_timeouts_are_capped_by_one_run_deadline(self) -> None:
        clean_ps = mock.Mock(returncode=0, stdout='', stderr='')
        empty_lsof = mock.Mock(returncode=1, stdout='', stderr='')
        with mock.patch.object(
            retention.time,
            'monotonic',
            side_effect=[100.0, 100.25, 101.0, 101.1],
        ), mock.patch.object(
            retention.subprocess,
            'run',
            side_effect=[clean_ps, empty_lsof],
        ) as run:
            self.assertEqual(retention.collect_process_refs(deadline=102.0), [])

        self.assertEqual(run.call_args_list[0].kwargs['timeout'], 1.75)
        self.assertEqual(run.call_args_list[1].kwargs['timeout'], 1.0)

    def test_failed_release_rescan_blocks_every_later_delete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            first = make_release(releases, 'openclaw-first')
            second = make_release(releases, 'openclaw-second')
            scans = [[], [], TimeoutError('deadline expired during cache-root scan')]

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(
                     retention,
                     'collect_references',
                     side_effect=scans,
                 ) as collector, \
                 mock.patch.object(retention.shutil, 'rmtree') as remove:
                rc, marker, _detail, report = retention.run(
                    apply=True,
                    keep_latest=0,
                    min_age_days=0,
                )

            self.assertEqual(rc, 1)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_BLOCKED')
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual(collector.call_count, 3)
            remove.assert_not_called()
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['error_count'], 2)
            self.assertIn(
                'authoritative reference rescan failed before delete',
                payload['receipts'][0]['error'],
            )
            self.assertIn(
                'further release deletion blocked',
                payload['receipts'][1]['error'],
            )

    def test_apply_removes_unprotected_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            old = make_release(releases, 'openclaw-2026.1.1-old')

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=[]):
                rc, marker, detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            self.assertIn('result: removed_unprotected_releases', detail)
            self.assertFalse(old.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['state'], 'apply_complete')
            self.assertTrue(payload['terminal'])
            self.assertEqual(payload['summary']['removed_count'], 1)
            self.assertEqual(payload['removed'], ['openclaw-2026.1.1-old'])
            self.assertEqual(payload['receipts'][0]['state'], 'removed')

    def test_current_symlink_reference_protects_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            current = make_release(releases, 'openclaw-current')
            old = make_release(releases, 'openclaw-old')

            refs = [retention.Reference('current-runtime-symlink', current.resolve())]
            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=refs):
                rc, marker, detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 0)
            self.assertTrue(current.exists())
            self.assertFalse(old.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['protected_count'], 1)
            self.assertEqual(payload['protected'][0]['name'], 'openclaw-current')
            self.assertEqual(payload['protected'][0]['state'], 'skipped_protected')
            self.assertIn('current-runtime-symlink', payload['protected'][0]['reasons'])

    def test_non_active_unresolved_paths_expire_but_explicit_retention_remains(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            promotions = base / 'runtime_promotions'
            previous = make_release(releases, 'openclaw-previous')
            rollback = make_release(releases, 'openclaw-rollback')
            explicitly_retained = make_release(releases, 'openclaw-explicitly-retained')

            completed = promotions / 'browser-control-runtime-promotion-abc123'
            completed.mkdir(parents=True)
            (completed / 'operation.lock.json').write_text(
                json.dumps(
                    {
                        'state': 'completed_live_proof',
                        'previousReleasePath': str(previous),
                        'rollbackReleasePath': str(rollback),
                        'stableReleasePath': str(explicitly_retained),
                        'stableReleaseDisposition': 'retain_until_manual_archive_window',
                    }
                ),
                encoding='utf-8',
            )

            refs = retention.collect_promotion_dependency_refs(promotions, releases)
            records = retention.list_release_records(releases)
            retention.protect_records(records, refs)
            by_name = {record.name: record for record in records}

            self.assertFalse(by_name[previous.name].protected)
            self.assertFalse(by_name[rollback.name].protected)
            self.assertTrue(by_name[explicitly_retained.name].protected)
            self.assertEqual(len(refs), 1)
            self.assertIn(
                'promotion-operation-lock:browser-control-runtime-promotion-abc123:stableReleasePath:retain_until_manual_archive_window',
                by_name[explicitly_retained.name].protected_reasons,
            )

    def test_matching_failed_terminal_receipt_reconciles_stale_active_lock_without_permanent_release_ref(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            promotions = base / 'runtime_promotions'
            release = make_release(releases, 'openclaw-terminal-failed')
            operation = promotions / 'operation-123'
            write_terminalized_active_operation(operation, release)

            refs = retention.collect_promotion_dependency_refs(promotions, releases)
            records = retention.list_release_records(releases)
            retention.protect_records(records, refs)

            self.assertEqual(refs, [])
            self.assertFalse(records[0].protected)
            self.assertEqual(
                retention.LAST_PROMOTION_OPERATION_CLASSIFICATIONS,
                [
                    {
                        'operation_root': operation.name,
                        'operation_lock_path': str(operation / 'operation.lock.json'),
                        'lock_state': 'active',
                        'classification': 'terminalized_stale_active_lock',
                        'terminal_receipt_path': str(operation / 'terminal-receipt.json'),
                        'terminal_state': 'failed',
                        'operation_id': operation.name,
                        'run_id': operation.name,
                        'terminal_recorded_at': '2026-07-31T21:09:39Z',
                    }
                ],
            )

    def test_genuinely_active_operation_without_terminal_receipt_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            promotions = base / 'runtime_promotions'
            release = make_release(releases, 'openclaw-active')
            operation = promotions / 'active-operation'
            lock, terminal = write_terminalized_active_operation(operation, release)
            terminal.unlink()

            with self.assertRaisesRegex(
                ValueError,
                'active promotion operation blocks runtime release retention',
            ):
                retention.collect_promotion_dependency_refs(promotions, releases)

            self.assertTrue(lock.exists())
            self.assertTrue(release.exists())

    def test_mismatched_or_malformed_terminal_receipt_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            promotions = base / 'runtime_promotions'
            release = make_release(releases, 'openclaw-ambiguous')
            operation = promotions / 'ambiguous-operation'
            _, terminal = write_terminalized_active_operation(
                operation,
                release,
                terminal_operation_id='different-operation',
            )

            with self.assertRaisesRegex(ValueError, 'operationId/runId does not match'):
                retention.collect_promotion_dependency_refs(promotions, releases)

            terminal.write_text('{not-json', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'cannot read promotion protection metadata'):
                retention.collect_promotion_dependency_refs(promotions, releases)

            self.assertTrue(release.exists())

    def test_release_root_alias_normalizes_only_by_direct_child_realpath(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'physical-releases'
            promotions = base / 'runtime_promotions'
            release = make_release(releases, 'openclaw-physical')
            releases_alias = base / 'configured-releases'
            releases_alias.symlink_to(releases, target_is_directory=True)
            operation = promotions / 'completed-operation'
            operation.mkdir(parents=True)
            (operation / 'operation.lock.json').write_text(
                json.dumps(
                    {
                        'state': 'completed',
                        'releasePath': str(release),
                    }
                ),
                encoding='utf-8',
            )

            refs = retention.collect_promotion_dependency_refs(
                promotions,
                releases_alias,
            )

            self.assertEqual(refs, [])
            with self.assertRaisesRegex(ValueError, 'must resolve to a direct'):
                retention.normalize_release_dependency_path(
                    str(base / 'outside' / 'openclaw-escape'),
                    source='fixture',
                    releases_root=releases_alias,
                )

    def test_retain_long_term_manifest_dependency_protects_referenced_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            promotions = base / 'runtime_promotions'
            retained_release = make_release(releases, 'openclaw-retain-long-term')
            partial_build = make_release(releases, '.openclaw-partial-build')
            artifact = promotions / 'rebuild-to-abc123-20260725T010203Z'
            artifact.mkdir(parents=True)
            (artifact / 'retention-manifest.json').write_text(
                json.dumps(
                    {
                        'kind': 'openclaw.runtime-promotion.retention-manifest.v1',
                        'release': {'path': str(retained_release), 'class': 'retain_long_term'},
                        'partialBuild': {'path': str(partial_build), 'class': 'retain_long_term'},
                        'evidenceRoot': {'path': str(artifact), 'class': 'retain_long_term'},
                    }
                ),
                encoding='utf-8',
            )

            refs = retention.collect_promotion_dependency_refs(promotions, releases)
            records = retention.list_release_records(releases)
            retention.protect_records(records, refs)

            self.assertEqual(len(refs), 1)
            self.assertTrue(records[0].protected)
            self.assertIn(
                'promotion-retention-manifest:rebuild-to-abc123-20260725T010203Z:retain_long_term',
                records[0].protected_reasons,
            )

    def test_invalid_promotion_dependency_metadata_fails_closed_before_prune(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            release = make_release(releases, 'openclaw-protected-by-failure')
            artifact = promotions / 'rebuild-to-invalid-20260725T010203Z'
            artifact.mkdir(parents=True)
            (artifact / 'operation.lock.json').write_text('{not-json', encoding='utf-8')

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_current_symlink_ref', return_value=[]), \
                 mock.patch.object(retention, 'collect_launchagent_refs', return_value=[]), \
                 mock.patch.object(retention, 'collect_symlink_refs', return_value=[]), \
                 mock.patch.object(retention, 'collect_process_refs', return_value=[]), \
                 mock.patch.object(retention.shutil, 'rmtree') as remove:
                rc, marker, _detail, report = retention.run(
                    apply=True,
                    keep_latest=0,
                    min_age_days=0,
                )

            self.assertEqual(rc, 1)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_BLOCKED')
            remove.assert_not_called()
            self.assertTrue(release.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertTrue(payload['terminal'])
            self.assertEqual(payload['state'], 'classification_blocked')
            self.assertIn(
                'cannot read promotion protection metadata',
                payload['errors'][0]['error'],
            )

    def test_symlinked_promotion_dependency_metadata_fails_closed_before_prune(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            promotions = base / 'runtime_promotions'
            release = make_release(releases, 'openclaw-protected-by-symlink-failure')
            artifact = promotions / 'rebuild-to-symlink-20260725T010203Z'
            artifact.mkdir(parents=True)
            outside = base / 'outside-operation-lock.json'
            outside.write_text('{}', encoding='utf-8')
            (artifact / 'operation.lock.json').symlink_to(outside)

            with self.assertRaisesRegex(
                ValueError,
                'must be a physical regular file',
            ):
                retention.collect_promotion_dependency_refs(
                    promotions,
                    releases,
                )

            self.assertTrue(release.exists())

    def test_promotion_metadata_replaced_by_symlink_before_open_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            metadata = base / 'operation.lock.json'
            outside = base / 'outside.json'
            metadata.write_text('{}', encoding='utf-8')
            outside.write_text('{}', encoding='utf-8')
            real_open = os.open

            def replace_then_open(path: Path, flags: int) -> int:
                metadata.unlink()
                metadata.symlink_to(outside)
                return real_open(path, flags)

            with mock.patch.object(retention.os, 'open', side_effect=replace_then_open):
                with self.assertRaisesRegex(ValueError, 'must be a physical regular file'):
                    retention.load_json_object(metadata)

    def test_existing_release_symlink_is_inventoried_and_fail_closed_protected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            releases.mkdir()
            outside = make_release(base / 'outside', 'openclaw-outside')
            link = releases / 'openclaw-bad'
            link.symlink_to(outside, target_is_directory=True)

            records = retention.list_release_records(releases)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].entry_type, retention.SYMLINK_ENTRY)
            self.assertEqual(records[0].link_path, link)
            self.assertEqual(records[0].raw_target, str(outside))
            self.assertTrue(records[0].target_exists)
            self.assertEqual(records[0].realpath, outside.resolve())
            self.assertEqual(records[0].protected_reasons, [retention.SYMLINK_PROTECTION_REASON])

    def test_intentional_symlinked_releases_root_preserves_child_lstat_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            physical_root = base / 'physical-releases'
            physical_release = make_release(physical_root, 'openclaw-physical')
            dangling_target = base / 'missing-archive'
            (physical_root / 'openclaw-dangling').symlink_to(dangling_target, target_is_directory=True)
            releases = base / 'releases'
            releases.symlink_to(physical_root, target_is_directory=True)

            records = retention.list_release_records(releases)
            by_name = {record.name: record for record in records}

            self.assertEqual(by_name['openclaw-physical'].entry_type, retention.PHYSICAL_DIRECTORY_ENTRY)
            self.assertEqual(by_name['openclaw-physical'].realpath, physical_release.resolve())
            self.assertEqual(by_name['openclaw-dangling'].entry_type, retention.SYMLINK_ENTRY)
            self.assertEqual(by_name['openclaw-dangling'].link_path, releases / 'openclaw-dangling')
            self.assertEqual(by_name['openclaw-dangling'].raw_target, str(dangling_target))
            self.assertFalse(by_name['openclaw-dangling'].target_exists)
            self.assertTrue(by_name['openclaw-dangling'].protected)

    def test_dangling_ea6_symlink_is_reported_protected_and_never_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            releases.mkdir()
            name = 'openclaw-2026.4.24-ea6c0e1286-20260713T111944Z-selfcontained'
            link = releases / name
            raw_target = base / 'retired-runtime-archives' / name
            link.symlink_to(raw_target, target_is_directory=True)
            dependency_source = 'promotion-operation-lock:rebuild-to-c7:previousReleasePath'
            refs = [retention.Reference(dependency_source, link)]

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=refs), \
                 mock.patch.object(retention.shutil, 'rmtree') as remove:
                rc, marker, detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            remove.assert_not_called()
            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            self.assertIn('result: no_unprotected_releases', detail)
            self.assertTrue(os.path.lexists(link))
            self.assertFalse(link.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['physical_directory_count'], 0)
            self.assertEqual(payload['summary']['symlink_count'], 1)
            self.assertEqual(payload['summary']['candidate_count'], 0)
            self.assertEqual(payload['summary']['protected_count'], 1)
            receipt = payload['receipts'][0]
            self.assertEqual(receipt['entry_type'], retention.SYMLINK_ENTRY)
            self.assertEqual(receipt['link_path'], str(link))
            self.assertEqual(receipt['raw_target'], str(raw_target))
            self.assertFalse(receipt['target_exists'])
            self.assertTrue(receipt['after_exists'])
            self.assertIn(retention.SYMLINK_PROTECTION_REASON, receipt['protection_reasons'])
            self.assertIn(dependency_source, receipt['protection_reasons'])

    def test_lexical_dependency_match_survives_dangling_symlink_retarget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            releases.mkdir()
            link = releases / 'openclaw-dangling'
            link.symlink_to(base / 'missing-a', target_is_directory=True)
            record = retention.list_release_records(releases)[0]
            link.unlink()
            link.symlink_to(base / 'missing-b', target_is_directory=True)
            source = 'promotion-retention-manifest:rebuild-to-abc:retain_long_term'

            retention.protect_records([record], [retention.Reference(source, link)])

            self.assertNotEqual(record.realpath, retention.realpath(link))
            self.assertIn(source, record.protected_reasons)

    def test_prune_defense_never_calls_rmtree_for_symlink_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            releases.mkdir()
            link = releases / 'openclaw-dangling'
            link.symlink_to(base / 'missing', target_is_directory=True)
            record = retention.list_release_records(releases)[0]
            record.protected_reasons.clear()

            with mock.patch.object(retention.shutil, 'rmtree') as remove:
                retention.prune_records([record], apply=True, persist=lambda: None)

            remove.assert_not_called()
            self.assertTrue(os.path.lexists(link))
            self.assertTrue(record.protected)
            self.assertEqual(record.action_state, 'skipped_protected')

    def test_platform_without_symlink_safe_rmtree_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            candidate = make_release(releases, 'openclaw-candidate')
            record = retention.list_release_records(releases)[0]

            with mock.patch.object(retention, 'RMTREE_AVOIDS_SYMLINK_ATTACKS', False), \
                 mock.patch.object(retention.shutil, 'rmtree') as remove:
                retention.prune_records([record], apply=True, persist=lambda: None)

            remove.assert_not_called()
            self.assertTrue(candidate.exists())
            self.assertEqual(record.action_state, 'remove_failed')
            self.assertIn('lacks symlink-attack resistance', record.remove_error or '')

    def test_physical_candidate_replaced_by_dangling_symlink_is_not_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            candidate = make_release(releases, 'openclaw-replaced')
            record = retention.list_release_records(releases)[0]
            (candidate / 'marker.txt').unlink()
            candidate.rmdir()
            candidate.symlink_to(base / 'missing', target_is_directory=True)

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 mock.patch.object(retention.shutil, 'rmtree') as remove:
                retention.prune_records([record], apply=True, persist=lambda: None)

            remove.assert_not_called()
            self.assertTrue(os.path.lexists(candidate))
            self.assertEqual(record.action_state, 'remove_failed')
            self.assertTrue(record.after_exists)
            self.assertIn('no longer a physical release directory', record.remove_error or '')

    def test_unexpected_non_directory_release_entry_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            releases = Path(raw) / 'releases'
            releases.mkdir()
            unexpected = releases / 'openclaw-not-a-directory'
            unexpected.write_text('not a runtime release', encoding='utf-8')

            with self.assertRaisesRegex(ValueError, 'unexpected release entry type'):
                retention.list_release_records(releases)

    def test_noop_state_is_safe_and_reports_no_unprotected_releases(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            current = make_release(releases, 'openclaw-current')
            refs = [retention.Reference('current-runtime-symlink', current.resolve())]

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=refs):
                rc, marker, detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            self.assertIn('result: no_unprotected_releases', detail)
            self.assertTrue(current.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['candidate_count'], 0)
            self.assertEqual(payload['summary']['removed_count'], 0)

    def test_interruption_leaves_atomic_in_progress_report_and_per_release_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            old = make_release(releases, 'openclaw-interrupted')
            observed_payloads: list[dict[str, object]] = []
            original_atomic_write = retention.atomic_write_json

            def capture_atomic_write(path: Path, payload: dict[str, object]) -> None:
                original_atomic_write(path, payload)
                if path != artifact_root / 'latest.json':
                    observed_payloads.append(json.loads(path.read_text(encoding='utf-8')))

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=[]), \
                 mock.patch.object(retention, 'atomic_write_json', side_effect=capture_atomic_write), \
                 mock.patch.object(retention.shutil, 'rmtree', side_effect=KeyboardInterrupt('simulated kill')):
                with self.assertRaises(KeyboardInterrupt):
                    retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertTrue(old.exists())
            latest = json.loads((artifact_root / 'latest.json').read_text(encoding='utf-8'))
            self.assertEqual(latest['state'], 'apply_in_progress')
            self.assertFalse(latest['terminal'])
            self.assertEqual(latest['receipts'][0]['state'], 'removal_in_progress')
            self.assertEqual(latest['filesystem_space']['before']['status'], 'measured')
            self.assertEqual(latest['filesystem_space']['after']['status'], 'pending')
            self.assertIsNone(latest['summary']['after_free_bytes'])
            self.assertIsNone(latest['summary']['observed_free_space_delta_bytes'])
            self.assertEqual(
                [payload['state'] for payload in observed_payloads],
                [
                    'classification_in_progress',
                    'apply_in_progress',
                    'apply_in_progress',
                ],
            )
            self.assertEqual(
                [
                    payload['receipts'][0]['state']
                    for payload in observed_payloads
                    if payload['receipts']
                ],
                ['pending', 'removal_in_progress'],
            )
            self.assertEqual(list(artifact_root.glob('.*.tmp')), [])

    def test_remove_failure_is_receipted_before_terminal_blocked_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            old = make_release(releases, 'openclaw-failure')

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=[]), \
                 mock.patch.object(retention.shutil, 'rmtree', side_effect=OSError('simulated failure')):
                rc, marker, detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 1)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_BLOCKED')
            self.assertIn('result: remove_errors', detail)
            self.assertTrue(old.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['state'], 'apply_blocked')
            self.assertTrue(payload['terminal'])
            self.assertEqual(payload['receipts'][0]['state'], 'remove_failed')
            self.assertIn('simulated failure', payload['receipts'][0]['error'])

    def test_partial_directory_is_reclassified_live_and_resumed_without_stale_report_trust(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            partial = make_release(releases, 'openclaw-partial', marker='remaining')
            (partial / 'already-removed.txt').write_text('old', encoding='utf-8')
            (partial / 'already-removed.txt').unlink()
            artifact_root.mkdir()
            (artifact_root / 'latest.json').write_text(
                json.dumps({'state': 'apply_in_progress', 'terminal': False, 'receipts': [{'name': partial.name, 'state': 'removal_in_progress'}]}),
                encoding='utf-8',
            )

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_references', return_value=[]):
                rc, marker, _, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            self.assertFalse(partial.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['state'], 'apply_complete')
            self.assertEqual(payload['before']['release_count'], 1)
            self.assertEqual(payload['receipts'][0]['state'], 'removed')
            self.assertEqual(payload['receipts'][0]['after_exists'], False)

    def test_physical_candidate_replacement_at_same_name_is_not_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            candidate = make_release(releases, 'openclaw-replaced-physical', marker='old')
            record = retention.list_release_records(releases)[0]
            (candidate / 'marker.txt').unlink()
            candidate.rmdir()
            replacement = make_release(
                releases,
                'openclaw-replaced-physical',
                marker='new',
            )

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 mock.patch.object(retention.shutil, 'rmtree') as remove:
                retention.prune_records(
                    [record],
                    apply=True,
                    persist=lambda: None,
                )

            remove.assert_not_called()
            self.assertTrue(replacement.exists())
            self.assertEqual(record.action_state, 'remove_failed')
            self.assertIn('device/inode changed', record.remove_error or '')

    def test_authoritative_process_scan_failure_blocks_instead_of_failing_open(self) -> None:
        failed_ps = mock.Mock(returncode=2, stdout='', stderr='ps failed')

        with mock.patch.object(
            retention.subprocess,
            'run',
            return_value=failed_ps,
        ):
            with self.assertRaisesRegex(
                ValueError,
                'process argv scan failed',
            ):
                retention.collect_process_refs()

    def test_authoritative_process_scan_timeout_blocks_instead_of_failing_open(self) -> None:
        with mock.patch.object(
            retention.subprocess,
            'run',
            side_effect=retention.subprocess.TimeoutExpired(
                cmd=['/bin/ps'],
                timeout=20,
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                'authoritative process argv references',
            ):
                retention.collect_process_refs()

    def test_lsof_explicit_empty_result_is_accepted_as_zero_open_file_refs(self) -> None:
        clean_ps = mock.Mock(returncode=0, stdout='', stderr='')
        empty_lsof = mock.Mock(returncode=1, stdout='', stderr='')

        with mock.patch.object(
            retention.subprocess,
            'run',
            side_effect=[clean_ps, empty_lsof],
        ) as run:
            self.assertEqual(retention.collect_process_refs(), [])
        self.assertEqual(
            run.call_args_list[0].kwargs['timeout'],
            retention.PROCESS_ARGV_SCAN_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            run.call_args_list[1].kwargs['timeout'],
            retention.OPEN_FILE_SCAN_TIMEOUT_SECONDS,
        )
        self.assertNotIn('+D', run.call_args_list[1].args[0])
        self.assertEqual(
            retention.OPEN_FILE_SCAN_TIMEOUT_SECONDS,
            60,
        )

    def test_lsof_macos_mixed_match_exit_one_with_field_records_is_authoritative(self) -> None:
        clean_ps = mock.Mock(returncode=0, stdout='', stderr='')
        mixed_lsof = mock.Mock(
            returncode=1,
            stdout=(
                'p522\n'
                'cnode\n'
                'fcwd\n'
                f"n{retention.OPERATOR.require_path('paths.runtime_releases_root')}/openclaw-current\n"
            ),
            stderr='',
        )

        with mock.patch.object(
            retention.subprocess,
            'run',
            side_effect=[clean_ps, mixed_lsof],
        ):
            refs = retention.collect_process_refs()

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].source, 'lsof:node:522')
        self.assertEqual(
            str(refs[0].path),
            str(retention.OPERATOR.require_path('paths.runtime_releases_root') / 'openclaw-current'),
        )

    def test_size_probe_timeout_does_not_walk_large_release_tree(self) -> None:
        with mock.patch.object(
            retention.subprocess,
            'run',
            side_effect=retention.subprocess.TimeoutExpired(cmd=['/usr/bin/du'], timeout=retention.SIZE_PROBE_TIMEOUT_SECONDS),
        ), mock.patch.object(retention.os, 'walk') as walk:
            self.assertIsNone(retention.du_bytes(Path('/tmp/openclaw-large-release')))
        walk.assert_not_called()

    def test_size_probe_rejects_failed_invalid_and_negative_results(self) -> None:
        for returncode, output in ((1, '12\tfixture'), (0, ''), (0, 'invalid'),
                                   (0, '-1\tfixture'), (0, '1.5\tfixture'),
                                   (0, '1\tfixture\n2\tother')):
            with self.subTest(returncode=returncode, output=output), mock.patch.object(
                retention.subprocess, 'run',
                return_value=mock.Mock(returncode=returncode, stdout=output),
            ), mock.patch.object(retention.os, 'walk') as walk:
                self.assertIsNone(retention.du_bytes(Path('/fixture-release')))
                walk.assert_not_called()
        for blocks in (0, 7):
            with self.subTest(blocks=blocks), mock.patch.object(
                retention.subprocess, 'run',
                return_value=mock.Mock(returncode=0, stdout=f'{blocks}\tfixture\n'),
            ):
                self.assertEqual(retention.du_bytes(Path('/fixture-release')), blocks * 1024)

    def test_global_lsof_scan_filters_exact_release_root_and_handles_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'runtime releases'
            target = make_release(releases, 'openclaw-kept')
            other = base / 'other' / 'openclaw-kept'
            other.mkdir(parents=True)
            clean_ps = mock.Mock(returncode=0, stdout='', stderr='')
            global_lsof = mock.Mock(
                returncode=0,
                stdout=(
                    'p522\n'
                    'cnode\n'
                    'n' + str(target / 'file with spaces.txt') + '\n'
                    'p523\n'
                    'cnode\n'
                    'n' + str(other / 'outside.txt') + '\n'
                    'p524\n'
                    'cnode\n'
                    'nprotocol: not a file\n'
                ),
                stderr='',
            )

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 mock.patch.object(retention.subprocess, 'run', side_effect=[clean_ps, global_lsof]) as run:
                refs = retention.collect_process_refs()

            self.assertEqual([str(ref.path) for ref in refs], [str(target / 'file with spaces.txt')])
            self.assertNotIn('+D', run.call_args_list[1].args[0])
            self.assertEqual(retention.LAST_PROCESS_SCAN_REPORT['method'], retention.OPEN_FILE_SCAN_METHOD)
            self.assertEqual(retention.LAST_PROCESS_SCAN_REPORT['lsof_matched_name_count'], 1)

    def test_global_lsof_timeout_blocks_instead_of_failing_open(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            releases = Path(raw) / 'releases'
            releases.mkdir()
            clean_ps = mock.Mock(returncode=0, stdout='', stderr='')
            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 mock.patch.object(
                     retention.subprocess,
                     'run',
                     side_effect=[
                         clean_ps,
                         retention.subprocess.TimeoutExpired(
                             cmd=['/usr/sbin/lsof'],
                             timeout=retention.OPEN_FILE_SCAN_TIMEOUT_SECONDS,
                         ),
                     ],
                 ):
                with self.assertRaisesRegex(ValueError, 'global_lsof_field_scan'):
                    retention.collect_process_refs()
            self.assertEqual(retention.LAST_PROCESS_SCAN_REPORT['status'], 'error')
            self.assertIn('TimeoutExpired', retention.LAST_PROCESS_SCAN_REPORT['error'])

    def test_report_records_process_scan_backend_and_parsed_open_file_refs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            release = make_release(releases, 'openclaw-kept')
            clean_ps = mock.Mock(returncode=0, stdout='', stderr='')
            global_lsof = mock.Mock(
                returncode=0,
                stdout='p522\ncnode\nn' + str(release / 'held.txt') + '\n',
                stderr='',
            )

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'collect_current_symlink_ref', return_value=[]), \
                 mock.patch.object(retention, 'collect_launchagent_refs', return_value=[]), \
                 mock.patch.object(retention, 'collect_symlink_refs', return_value=[]), \
                 mock.patch.object(retention, 'collect_promotion_dependency_refs', return_value=[]), \
                 mock.patch.object(retention.subprocess, 'run', side_effect=[clean_ps, global_lsof]):
                rc, marker, _detail, report = retention.run(apply=False, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_OK')
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['process_scan']['backend'], 'lsof')
            self.assertEqual(payload['process_scan']['method'], retention.OPEN_FILE_SCAN_METHOD)
            self.assertEqual(payload['process_scan']['parsed_references'][0]['path'], str(release / 'held.txt'))
            self.assertEqual(payload['summary']['protected_count'], 1)

    def test_all_physical_promotion_metadata_dirs_and_release_path_variants_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            promotions = base / 'runtime_promotions'
            release = make_release(releases, 'openclaw-release')
            stable = make_release(releases, 'openclaw-stable')
            final = make_release(releases, 'openclaw-final')
            manual = make_release(releases, 'openclaw-manual')
            explicitly_safe = make_release(releases, 'openclaw-safe')
            repair = promotions / 'repair-b446-source-format'
            repair.mkdir(parents=True)
            (repair / 'operation.lock.json').write_text(
                json.dumps(
                    {
                        'releasePath': str(release),
                        'stableReleasePath': str(stable),
                        'nestedCloseout': {
                            'finalReleasePath': str(final),
                        },
                        'previousReleasePath': str(explicitly_safe),
                        'previousReleaseDisposition': 'safe_to_prune_now',
                    }
                ),
                encoding='utf-8',
            )
            (repair / 'retention-manifest.json').write_text(
                json.dumps(
                    {
                        'items': [
                            {
                                'path': str(manual),
                                'class': 'retain_until_manual_prune',
                            }
                        ]
                    }
                ),
                encoding='utf-8',
            )

            refs = retention.collect_promotion_dependency_refs(
                promotions,
                releases,
            )
            records = retention.list_release_records(releases)
            retention.protect_records(records, refs)
            by_name = {record.name: record for record in records}

            for expired in (release, stable, final, explicitly_safe):
                self.assertFalse(by_name[expired.name].protected)
            self.assertTrue(by_name[manual.name].protected)
            self.assertEqual(len(refs), 1)

    def test_fresh_reference_rescan_blocks_delete_and_is_receipted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            releases = base / 'releases'
            artifact_root = base / 'artifacts'
            candidate = make_release(releases, 'openclaw-reference-drift')
            new_ref = retention.Reference(
                'promotion-operation-lock:new:releasePath',
                candidate,
            )

            with operator_binding(retention, 'paths.runtime_releases_root', releases), \
                 operator_binding(retention, 'paths.runtime_release_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(
                     retention,
                     'collect_references',
                     side_effect=[[], [], [new_ref]],
                 ):
                rc, marker, _detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 1)
            self.assertEqual(marker, 'RUNTIME_RELEASE_RETENTION_BLOCKED')
            self.assertTrue(candidate.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['state'], 'apply_blocked')
            self.assertIn(
                'candidate became protected',
                payload['receipts'][0]['error'],
            )


if __name__ == '__main__':
    unittest.main()
