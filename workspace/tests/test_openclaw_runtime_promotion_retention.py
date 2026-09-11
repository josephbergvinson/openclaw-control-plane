from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from activation_fixture_support import operator_binding

from scripts import openclaw_runtime_promotion_retention as retention
from scripts import openclaw_runtime_retention_metadata as retention_metadata


def make_artifact(root: Path, name: str, marker: str = 'payload', *, mtime: datetime | None = None) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / 'status.md').write_text(marker, encoding='utf-8')
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path / 'status.md', (ts, ts))
        os.utime(path, (ts, ts))
    return path


class RuntimePromotionRetentionTests(unittest.TestCase):
    def test_dry_run_writes_report_and_does_not_remove_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            now = datetime(2026, 7, 8, tzinfo=timezone.utc)
            old = make_artifact(promotions, 'runtime-promotion-old', mtime=now - timedelta(days=10))

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()):
                rc, marker, detail, report = retention.run(apply=False, keep_latest=0, min_age_days=3)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_PROMOTION_RETENTION_DRY_RUN')
            self.assertIn('result: dry_run_candidates', detail)
            self.assertEqual(report.parent, artifact_root)
            self.assertTrue((artifact_root / 'latest.json').exists())
            self.assertTrue(old.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['schema'], 'openclaw.runtime_promotion_retention.v2')
            self.assertEqual(payload['summary']['total_artifacts'], 1)
            self.assertEqual(payload['summary']['candidate_count'], 1)
            self.assertEqual(payload['summary']['removed_count'], 0)
            self.assertEqual(payload['candidates'][0]['action'], 'would_remove')

    def test_apply_removes_only_old_unretained_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            now = datetime(2026, 7, 8, tzinfo=timezone.utc)
            old = make_artifact(promotions, 'runtime-promotion-old', mtime=now - timedelta(days=10))
            recent = make_artifact(promotions, 'runtime-rebuild-recent', mtime=now - timedelta(days=1))

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()):
                rc, marker, detail, report = retention.run(apply=True, keep_latest=0, min_age_days=3)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_PROMOTION_RETENTION_OK')
            self.assertIn('result: removed_old_promotion_artifacts', detail)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['retained_count'], 1)
            self.assertEqual(payload['summary']['candidate_count'], 1)
            self.assertEqual(payload['summary']['removed_count'], 1)
            self.assertEqual(payload['removed'], ['runtime-promotion-old'])
            self.assertIn('younger-than-3d', payload['retained'][0]['reasons'])

    def test_keep_latest_and_current_runtime_reference_protect_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            now = datetime(2026, 7, 8, tzinfo=timezone.utc)
            old = make_artifact(promotions, 'runtime-promotion-old', mtime=now - timedelta(days=10))
            referenced = make_artifact(
                promotions,
                'runtime-promotion-referenced',
                marker='promoted /Users/operator/.openclaw-runtime/releases/openclaw-current-release',
                mtime=now - timedelta(days=9),
            )
            newest = make_artifact(promotions, 'runtime-rebuild-newest', mtime=now - timedelta(days=8))

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'current_release_tokens', return_value={'openclaw-current-release'}):
                rc, marker, _detail, report = retention.run(apply=True, keep_latest=1, min_age_days=3)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_PROMOTION_RETENTION_OK')
            self.assertFalse(old.exists())
            self.assertTrue(referenced.exists())
            self.assertTrue(newest.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            retained = {entry['name']: entry['reasons'] for entry in payload['retained']}
            self.assertIn('latest-1', retained['runtime-rebuild-newest'])
            self.assertIn('mentions-current-runtime-release', retained['runtime-promotion-referenced'])

    def test_unexpected_path_refusal_for_symlinked_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            promotions.mkdir()
            outside = make_artifact(base / 'outside', 'runtime-promotion-outside')
            (promotions / 'runtime-promotion-bad').symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                ValueError,
                'must be a physical directory',
            ):
                retention.list_promotion_records(promotions)

    def test_ignores_unmanaged_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            now = datetime(2026, 7, 8, tzinfo=timezone.utc)
            ignored = make_artifact(promotions, 'manual-notes', mtime=now - timedelta(days=10))

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()):
                rc, marker, detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_PROMOTION_RETENTION_OK')
            self.assertIn('result: no_prunable_promotion_artifacts', detail)
            self.assertTrue(ignored.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['total_artifacts'], 0)

    def test_aged_current_producer_families_prune_without_permanent_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            now = datetime(2026, 7, 25, tzinfo=timezone.utc)
            rebuild = make_artifact(
                promotions,
                'rebuild-to-abc123-20260720T010203Z',
                mtime=now - timedelta(days=5),
            )
            browser = make_artifact(
                promotions,
                'browser-control-runtime-promotion-def456',
                mtime=now - timedelta(days=5),
            )
            ignored = make_artifact(
                promotions,
                'repair-b446-selfcontained-20260720T010203Z',
                mtime=now - timedelta(days=5),
            )

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()):
                rc, marker, detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_PROMOTION_RETENTION_OK')
            self.assertIn('result: removed_old_promotion_artifacts', detail)
            self.assertFalse(rebuild.exists())
            self.assertFalse(browser.exists())
            self.assertTrue(ignored.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['total_artifacts'], 2)
            self.assertEqual(payload['summary']['retained_count'], 0)
            self.assertEqual(payload['summary']['candidate_count'], 2)
            self.assertEqual(payload['summary']['removed_count'], 2)
            self.assertEqual(
                payload['policy']['automatic_age_prune_prefixes'],
                ['runtime-promotion-', 'runtime-rebuild-', 'rebuild-to-', 'browser-control-runtime-promotion-'],
            )

    def test_mapped_legacy_operation_dependency_does_not_block_age_prune(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            now = datetime(2026, 7, 25, tzinfo=timezone.utc)
            artifact = make_artifact(
                promotions,
                'browser-control-runtime-promotion-abc123',
                mtime=now - timedelta(days=5),
            )
            (artifact / 'retention-manifest.json').write_text(
                json.dumps({'classification': 'safe_to_prune_now', 'run': artifact.name}),
                encoding='utf-8',
            )
            (artifact / 'operation.lock.json').write_text(
                json.dumps(
                    {
                        'state': 'build-claimed',
                        'previousReleasePath': '/Users/operator/.openclaw-runtime/releases/openclaw-previous',
                    }
                ),
                encoding='utf-8',
            )
            old_timestamp = (now - timedelta(days=5)).timestamp()
            os.utime(artifact, (old_timestamp, old_timestamp))

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()):
                rc, marker, _detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_PROMOTION_RETENTION_OK')
            self.assertFalse(artifact.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['candidate_count'], 1)
            self.assertEqual(payload['summary']['removed_count'], 1)
            self.assertEqual(
                payload['operation_classifications'][0]['classification'],
                'legacy_retired_driver_lock',
            )

    def test_canonical_terminal_lock_requires_and_matches_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            operation = Path(raw) / 'canonical-terminal'
            operation.mkdir()
            lock_path = operation / 'operation.lock.json'
            lock = {
                'kind': retention_metadata.PROMOTION_OPERATION_LOCK_KIND,
                'state': 'completed',
                'operationId': operation.name,
                'runId': 'run-canonical',
                'authoritySha256': 'a' * 64,
                'journalSequence': 2,
                'acquiredAt': '2026-08-07T10:00:00Z',
            }
            lock_path.write_text(json.dumps(lock), encoding='utf-8')
            terminal_path = operation / 'terminal-receipt.json'
            terminal_path.write_text(
                json.dumps(
                    {
                        'kind': retention_metadata.PROMOTION_TERMINAL_RECEIPT_KIND,
                        'state': 'completed',
                        'operationId': operation.name,
                        'runId': 'run-canonical',
                        'authoritySha256': 'a' * 64,
                        'journalSequence': 2,
                        'journalHeadSha256': 'b' * 64,
                        'recordedAt': '2026-08-07T10:01:00Z',
                    }
                ),
                encoding='utf-8',
            )

            classification = retention_metadata.classify_operation_lock(
                lock_path,
                lock,
                load_json=lambda path: json.loads(path.read_text(encoding='utf-8')),
            )

            self.assertEqual(classification['classification'], 'terminalized_operation_lock')
            terminal = json.loads(terminal_path.read_text(encoding='utf-8'))
            terminal['state'] = 'failed'
            terminal_path.write_text(json.dumps(terminal), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'does not match canonical lock state'):
                retention_metadata.classify_operation_lock(
                    lock_path,
                    lock,
                    load_json=lambda path: json.loads(path.read_text(encoding='utf-8')),
                )

    def test_unknown_non_active_operation_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / 'unknown' / 'operation.lock.json'
            lock_path.parent.mkdir()
            with self.assertRaisesRegex(ValueError, 'nor a mapped retired-driver state'):
                retention_metadata.classify_operation_lock(
                    lock_path,
                    {'state': 'new-unreviewed-terminal-vocabulary'},
                    load_json=lambda _path: {},
                )

    def test_active_operation_without_terminal_receipt_hard_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            now = datetime(2026, 7, 25, tzinfo=timezone.utc)
            artifact = make_artifact(
                promotions,
                'runtime-rebuild-active',
                mtime=now - timedelta(days=5),
            )
            (artifact / 'operation.lock.json').write_text(
                json.dumps({'state': 'active'}),
                encoding='utf-8',
            )

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()):
                with self.assertRaisesRegex(
                    ValueError,
                    'active promotion operation blocks runtime release retention',
                ):
                    retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertTrue(artifact.exists())

    def test_non_active_explicit_dependency_retention_protects_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            now = datetime(2026, 7, 25, tzinfo=timezone.utc)
            artifact = make_artifact(
                promotions,
                'runtime-rebuild-explicit-retention',
                mtime=now - timedelta(days=5),
            )
            (artifact / 'operation.lock.json').write_text(
                json.dumps(
                    {
                        'state': 'terminal-success',
                        'previousReleasePath': '/Users/operator/.openclaw-runtime/releases/openclaw-previous',
                        'previousReleaseDisposition': 'retain_until_manual_archive_window',
                    }
                ),
                encoding='utf-8',
            )

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()):
                rc, marker, _detail, report = retention.run(
                    apply=True,
                    keep_latest=0,
                    min_age_days=0,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_PROMOTION_RETENTION_OK')
            self.assertTrue(artifact.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertIn(
                'operation-lock-previousReleasePath-explicit-retention:retain_until_manual_archive_window',
                payload['retained'][0]['reasons'],
            )

    def test_explicit_whole_directory_prune_disposition_allows_current_family_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            now = datetime(2026, 7, 25, tzinfo=timezone.utc)
            artifact = make_artifact(
                promotions,
                'rebuild-to-explicitly-prunable-20260720T010203Z',
                mtime=now - timedelta(days=5),
            )
            (artifact / 'retention-manifest.json').write_text(
                json.dumps({'classification': 'safe_to_prune_now', 'run': artifact.name}),
                encoding='utf-8',
            )
            (artifact / 'operation.lock.json').write_text(
                json.dumps(
                    {
                        'previousReleasePath': '/Users/operator/.openclaw-runtime/releases/openclaw-previous',
                        'previousReleaseDisposition': 'safe_to_prune_now',
                    }
                ),
                encoding='utf-8',
            )
            old_timestamp = (now - timedelta(days=5)).timestamp()
            os.utime(artifact, (old_timestamp, old_timestamp))

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()):
                rc, marker, detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_PROMOTION_RETENTION_OK')
            self.assertIn('result: removed_old_promotion_artifacts', detail)
            self.assertFalse(artifact.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(payload['summary']['candidate_count'], 1)
            self.assertEqual(payload['summary']['removed_count'], 1)

    def test_retain_long_term_manifest_protects_legacy_managed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            now = datetime(2026, 7, 25, tzinfo=timezone.utc)
            artifact = make_artifact(
                promotions,
                'runtime-promotion-long-term',
                mtime=now - timedelta(days=5),
            )
            (artifact / 'retention-manifest.json').write_text(
                json.dumps({'classification': 'retain_long_term', 'run': artifact.name}),
                encoding='utf-8',
            )
            old_timestamp = (now - timedelta(days=5)).timestamp()
            os.utime(artifact, (old_timestamp, old_timestamp))

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()):
                rc, marker, _detail, report = retention.run(apply=True, keep_latest=0, min_age_days=0)

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_PROMOTION_RETENTION_OK')
            self.assertTrue(artifact.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertIn('retention-manifest-retain-long-term', payload['retained'][0]['reasons'])

    def test_symlinked_durable_metadata_protects_current_artifact_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            outside = base / 'outside-retention-manifest.json'
            outside.write_text(
                json.dumps({'classification': 'safe_to_prune_now'}),
                encoding='utf-8',
            )
            now = datetime(2026, 7, 25, tzinfo=timezone.utc)
            artifact = make_artifact(
                promotions,
                'rebuild-to-symlinked-metadata-20260720T010203Z',
                mtime=now - timedelta(days=5),
            )
            (artifact / 'retention-manifest.json').symlink_to(outside)

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=now), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()):
                rc, marker, _detail, report = retention.run(
                    apply=True,
                    keep_latest=0,
                    min_age_days=0,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(marker, 'RUNTIME_PROMOTION_RETENTION_OK')
            self.assertTrue(artifact.exists())
            payload = json.loads(report.read_text(encoding='utf-8'))
            self.assertTrue(
                any(
                    reason.startswith(
                        'retention-manifest.json-invalid-fail-closed:'
                    )
                    for reason in payload['retained'][0]['reasons']
                )
            )

    def test_metadata_replaced_after_open_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            metadata = base / 'retention-manifest.json'
            metadata.write_text(
                json.dumps({'classification': 'safe_to_prune_now'}),
                encoding='utf-8',
            )
            outside = base / 'outside.json'
            outside.write_text(
                json.dumps({'classification': 'safe_to_prune_now'}),
                encoding='utf-8',
            )
            original_open = retention.os.open
            replaced = False

            def open_then_replace(path, flags, *args, **kwargs):
                nonlocal replaced
                descriptor = original_open(path, flags, *args, **kwargs)
                if not replaced and Path(path) == metadata:
                    metadata.unlink()
                    metadata.symlink_to(outside)
                    replaced = True
                return descriptor

            with mock.patch.object(
                retention.os,
                'open',
                side_effect=open_then_replace,
            ):
                payload, error = retention.load_metadata_object(metadata)

            self.assertTrue(replaced)
            self.assertIsNone(payload)
            self.assertEqual(
                error,
                'retention-manifest.json-invalid-fail-closed:'
                'not-a-physical-file',
            )

    def test_candidate_replaced_by_symlink_before_prune_is_not_removed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            outside = make_artifact(
                base / 'outside',
                'runtime-promotion-outside',
                mtime=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
            candidate = make_artifact(
                promotions,
                'runtime-promotion-candidate',
                mtime=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
            record = retention.list_promotion_records(promotions)[0]
            shutil.rmtree(candidate)
            candidate.symlink_to(outside, target_is_directory=True)

            with operator_binding(retention, 'paths.runtime_promotions_root',
                promotions,
            ), mock.patch.object(retention.shutil, 'rmtree') as remove:
                retention.prune_records([record], apply=True)

            remove.assert_not_called()
            self.assertTrue(candidate.is_symlink())
            self.assertTrue(outside.is_dir())
            self.assertIn(
                'promotion candidate identity changed',
                record.remove_error or '',
            )

    def test_candidate_replaced_by_physical_directory_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            candidate = make_artifact(
                promotions,
                'runtime-promotion-physical-replacement',
                mtime=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
            record = retention.list_promotion_records(promotions)[0]
            shutil.rmtree(candidate)
            replacement = make_artifact(
                promotions,
                'runtime-promotion-physical-replacement',
                marker='replacement',
                mtime=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )

            with operator_binding(retention, 'paths.runtime_promotions_root',
                promotions,
            ), mock.patch.object(retention.shutil, 'rmtree') as remove:
                retention.prune_records([record], apply=True)

            remove.assert_not_called()
            self.assertTrue(replacement.exists())
            self.assertIn(
                'promotion candidate identity changed',
                record.remove_error or '',
            )

    def test_full_metadata_rescan_blocks_new_retention_dependency_before_delete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact = make_artifact(
                promotions,
                'runtime-promotion-metadata-drift',
                mtime=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
            with mock.patch.object(
                retention,
                'current_release_tokens',
                return_value=set(),
            ):
                records = retention.list_promotion_records(promotions)
                retention.protect_records(
                    records,
                    now=datetime(2026, 7, 25, tzinfo=timezone.utc),
                    keep_latest=0,
                    min_age_days=0,
                )
                expected = retention.inventory_fingerprint(records)

                def reclassify():
                    (artifact / 'retention-manifest.json').write_text(
                        json.dumps({'classification': 'retain_long_term'}),
                        encoding='utf-8',
                    )
                    fresh = retention.list_promotion_records(promotions)
                    retention.protect_records(
                        fresh,
                        now=datetime(2026, 7, 25, tzinfo=timezone.utc),
                        keep_latest=0,
                        min_age_days=0,
                    )
                    return fresh

                with operator_binding(retention, 'paths.runtime_promotions_root',
                    promotions,
                ), mock.patch.object(retention.shutil, 'rmtree') as remove:
                    retention.prune_records(
                        records,
                        apply=True,
                        reclassify=reclassify,
                        expected_inventory=expected,
                    )

            remove.assert_not_called()
            self.assertTrue(artifact.exists())
            self.assertIn(
                'inventory or directory metadata changed',
                records[0].remove_error or '',
            )

    def test_interruption_preserves_atomic_in_progress_per_item_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            candidate = make_artifact(
                promotions,
                'runtime-promotion-interrupted',
                mtime=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
            observed: list[dict[str, object]] = []
            original_atomic_write = retention.atomic_write_json

            def capture(path: Path, payload: dict[str, object]) -> None:
                original_atomic_write(path, payload)
                if path != artifact_root / 'latest.json':
                    observed.append(json.loads(path.read_text(encoding='utf-8')))

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=datetime(2026, 7, 25, tzinfo=timezone.utc)), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()), \
                 mock.patch.object(retention, 'atomic_write_json', side_effect=capture), \
                 mock.patch.object(retention.shutil, 'rmtree', side_effect=KeyboardInterrupt('simulated kill')):
                with self.assertRaises(KeyboardInterrupt):
                    retention.run(
                        apply=True,
                        keep_latest=0,
                        min_age_days=0,
                    )

            self.assertTrue(candidate.exists())
            latest = json.loads(
                (artifact_root / 'latest.json').read_text(encoding='utf-8')
            )
            self.assertEqual(latest['state'], 'apply_in_progress')
            self.assertFalse(latest['terminal'])
            self.assertEqual(
                latest['receipts'][0]['state'],
                'removal_in_progress',
            )
            self.assertEqual(
                [payload['receipts'][0]['state'] for payload in observed],
                ['pending', 'removal_in_progress'],
            )
            self.assertEqual(list(artifact_root.glob('.*.tmp')), [])

    def test_report_paths_are_uuid_unique_even_with_identical_clock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            promotions = base / 'runtime_promotions'
            artifact_root = base / 'reports'
            make_artifact(
                promotions,
                'runtime-promotion-report-identity',
                mtime=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
            fixed = datetime(2026, 7, 25, tzinfo=timezone.utc)

            with operator_binding(retention, 'paths.runtime_promotions_root', promotions), \
                 operator_binding(retention, 'paths.runtime_promotion_retention_artifacts', artifact_root), \
                 operator_binding(retention, 'paths.workspace', base), \
                 mock.patch.object(retention, 'utc_now', return_value=fixed), \
                 mock.patch.object(retention, 'current_release_tokens', return_value=set()):
                first = retention.run(
                    apply=False,
                    keep_latest=0,
                    min_age_days=0,
                )[3]
                second = retention.run(
                    apply=False,
                    keep_latest=0,
                    min_age_days=0,
                )[3]

            self.assertNotEqual(first, second)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_release_and_promotion_helpers_share_one_nonblocking_writer_lock(self) -> None:
        from scripts import openclaw_runtime_release_retention as release_retention

        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            with operator_binding(release_retention, 'paths.runtime_retention_lock', base / 'retention.lock'), \
                 operator_binding(retention, 'paths.runtime_retention_lock', base / 'retention.lock'):
                held = release_retention.acquire_shared_retention_lock()
                try:
                    with self.assertRaisesRegex(
                        ValueError,
                        'another runtime retention writer',
                    ):
                        retention.acquire_shared_retention_lock()
                finally:
                    held.close()


if __name__ == '__main__':
    unittest.main()
