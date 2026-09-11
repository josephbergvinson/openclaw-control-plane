from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import workspace_integrity_guard as guard


class WorkspaceIntegrityGuardTests(unittest.TestCase):
    def test_known_source_edits_are_preserved_without_operator_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'registry').mkdir()
            source = root / 'registry' / 'host_profile.json'
            source.write_text('owner change\n', encoding='utf-8')
            drift, errors = guard.classify_root_drift(
                root, 'control-plane', (' M registry/host_profile.json',)
            )
            self.assertEqual(errors, ())
            self.assertEqual(drift[0].disposition, 'protected_source')
            self.assertEqual(source.read_text(), 'owner change\n')

    def test_unmerged_known_source_still_requires_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            drift, errors = guard.classify_root_drift(
                Path(tmp), 'control-plane', ('UU AGENTS.md',)
            )
            self.assertEqual(errors, ())
            self.assertEqual(drift[0].disposition, guard.OPERATOR_DECISION_DRIFT)

    def test_registered_source_deletion_or_type_change_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            for status in (' D', 'D ', ' T', 'T '):
                with self.subTest(status=status):
                    drift, errors = guard.classify_root_drift(
                        Path(tmp), 'control-plane', (f'{status} AGENTS.md',)
                    )
                    self.assertEqual(errors, ())
                    self.assertEqual(drift[0].disposition, guard.OPERATOR_DECISION_DRIFT)

    def test_clean_check_is_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            surfaces = (guard.SurfaceSpec('control-plane', root, ()),)
            rc, output = self.run_main(surfaces)
            self.assertEqual(rc, 0)
            self.assertEqual(output, ['OpenClaw workspace cleanup passed. No disposable residue needed removal; source edits, saved memory and task work were preserved.'])

    def test_failed_check_cannot_exit_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            surfaces = (guard.SurfaceSpec('control-plane', root, ('missing-required',)),)
            rc, output = self.run_main(surfaces)
            self.assertEqual(rc, 1)
            self.assertIn('failed', ' '.join(output).lower())
            self.assertNotIn('artifact_id', ' '.join(output))

    def create_surface(self, root: Path, required: tuple[str, ...]) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for rel_text in required:
            path = root / rel_text
            if rel_text.endswith('.git') or rel_text == '.git':
                path.mkdir(parents=True, exist_ok=True)
                continue
            if rel_text.endswith('/'):
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'{rel_text}\n', encoding='utf-8')

    def artifact_path(self, root: Path, output: list[str]) -> Path:
        self.assertNotIn('artifact_id:', '\n'.join(output))
        paths = sorted((root / 'artifacts' / 'workspace_integrity').glob('workspace-integrity-*.json'))
        self.assertTrue(paths, 'private check report is missing')
        self.assertEqual(paths[-1].stat().st_mode & 0o777, 0o600)
        return paths[-1]

    def run_main(self, surfaces: tuple[guard.SurfaceSpec, ...], control_root: Path | None = None) -> tuple[int, list[str]]:
        buf = io.StringIO()
        root = control_root or surfaces[0].root
        with mock.patch.object(guard, 'SURFACE_SPECS', surfaces):
            with mock.patch.object(guard, 'CONTROL_PLANE_ROOT', root):
                with mock.patch.object(guard, 'ARTIFACT_ROOT', root / 'artifacts' / 'workspace_integrity'):
                    with redirect_stdout(buf):
                        rc = guard.main()
        return rc, buf.getvalue().strip().splitlines()

    def test_success_records_all_surface_slugs_privately(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            surfaces = (
                guard.SurfaceSpec('control-plane', base / 'control-plane', ('.git', 'scripts/cron_python_entrypoint.py')),
                guard.SurfaceSpec('personal-data-runtime', base / 'personal-data-runtime', ('.git', 'README.md')),
                guard.SurfaceSpec('hk-exporter', base / 'hk-exporter', ('.git', 'README.md')),
                guard.SurfaceSpec('personal-data-public-docs', base / 'personal-data-public-docs', ('.git', 'index.html')),
            )
            for spec in surfaces:
                self.create_surface(spec.root, spec.required)

            rc, output = self.run_main(surfaces)

            self.assertEqual(rc, 0)
            self.assertEqual(output, ['OpenClaw workspace cleanup passed. No disposable residue needed removal; source edits, saved memory and task work were preserved.'])
            artifact_path = self.artifact_path(surfaces[0].root, output)
            self.assertTrue(artifact_path.exists())
            payload = json.loads(artifact_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['marker'], 'WORKSPACE_INTEGRITY_OK')
            self.assertEqual(payload['git_drift'], [])
            self.assertEqual(payload['roots'], [spec.slug for spec in surfaces])

    def test_missing_required_file_reports_surface_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            surfaces = (
                guard.SurfaceSpec('control-plane', base / 'control-plane', ('.git', 'scripts/cron_python_entrypoint.py')),
                guard.SurfaceSpec('personal-data-runtime', base / 'personal-data-runtime', ('.git', 'README.md', 'scripts/personal_data_source_ingest_guard.py')),
            )
            self.create_surface(surfaces[0].root, surfaces[0].required)
            self.create_surface(surfaces[1].root, ('.git', 'README.md'))

            rc, output = self.run_main(surfaces)

            self.assertEqual(rc, 1)
            self.assertIn('required paths are missing in personal-data-runtime', output[0])
            artifact_path = self.artifact_path(surfaces[0].root, output)
            payload = json.loads(artifact_path.read_text(encoding='utf-8'))
            self.assertIn('personal-data-runtime:missing=scripts/personal_data_source_ingest_guard.py', payload['root_blockers'])

    def test_safe_generated_residue_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            surfaces = (guard.SurfaceSpec('control-plane', root, ('.git', 'scripts/cron_python_entrypoint.py')),)
            self.create_surface(root, surfaces[0].required)
            residue = root / 'tests' / '__pycache__'
            residue.mkdir(parents=True)
            (residue / 'sample.pyc').write_bytes(b'cache')

            rc, output = self.run_main(surfaces)

            self.assertEqual(rc, 0)
            self.assertFalse(residue.exists())
            self.assertIn('removed 1 disposable cache folder.', output[0])
            artifact_path = self.artifact_path(root, output)
            payload = json.loads(artifact_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['cleanup']['safe_generated_residue_removed'], ['tests/__pycache__'])
            second_rc, second_output = self.run_main(surfaces)
            self.assertEqual((second_rc, second_output), (0, ['OpenClaw workspace cleanup passed. No disposable residue needed removal; source edits, saved memory and task work were preserved.']))

    def test_virtualenv_cache_is_not_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            surfaces = (guard.SurfaceSpec('control-plane', root, ('.git', 'scripts/cron_python_entrypoint.py')),)
            self.create_surface(root, surfaces[0].required)
            residue = root / '.venv' / 'lib' / 'python3.11' / 'site-packages' / 'pkg' / '__pycache__'
            residue.mkdir(parents=True)
            (residue / 'sample.pyc').write_bytes(b'cache')

            rc, output = self.run_main(surfaces)

            self.assertEqual(rc, 0)
            self.assertTrue(residue.exists())
            self.assertEqual(output, ['OpenClaw workspace cleanup passed. No disposable residue needed removal; source edits, saved memory and task work were preserved.'])

    def test_artifact_tree_and_symlink_alias_are_preserved_without_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            surfaces = (guard.SurfaceSpec('control-plane', root, ('.git', 'scripts/cron_python_entrypoint.py')),)
            self.create_surface(root, surfaces[0].required)
            canonical = root / 'artifacts' / 'RuntimePromotions' / 'release'
            residue = canonical / '.pytest_cache'
            residue.mkdir(parents=True)
            (residue / 'sample').write_text('cache\n', encoding='utf-8')
            alias_parent = root / 'artifacts' / 'runtime_promotions'
            alias_parent.symlink_to(root / 'artifacts' / 'RuntimePromotions', target_is_directory=True)

            scan = guard.find_safe_generated_residue(root)
            self.assertEqual(scan.paths, ())

            rc, output = self.run_main(surfaces)

            self.assertEqual(rc, 0)
            self.assertTrue(residue.exists())
            self.assertEqual(output, ['OpenClaw workspace cleanup passed. No disposable residue needed removal; source edits, saved memory and task work were preserved.'])
            artifact_path = self.artifact_path(root, output)
            payload = json.loads(artifact_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['cleanup']['safe_generated_residue_removed'], [])
            self.assertEqual(payload['cleanup']['safe_generated_residue_cleanup_errors'], [])

    def test_directory_symlink_to_outside_root_is_not_traversed_or_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            outside = Path(tmp) / 'outside'
            surfaces = (guard.SurfaceSpec('control-plane', root, ('.git', 'scripts/cron_python_entrypoint.py')),)
            self.create_surface(root, surfaces[0].required)
            outside_residue = outside / '__pycache__'
            outside_residue.mkdir(parents=True)
            (outside_residue / 'sample.pyc').write_bytes(b'cache')
            (root / 'outside-link').symlink_to(outside, target_is_directory=True)

            rc, output = self.run_main(surfaces)

            self.assertEqual(rc, 0)
            self.assertTrue(outside_residue.exists())
            self.assertEqual(output, ['OpenClaw workspace cleanup passed. No disposable residue needed removal; source edits, saved memory and task work were preserved.'])
            artifact_path = self.artifact_path(root, output)
            payload = json.loads(artifact_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['cleanup']['safe_generated_residue_removed'], [])
            self.assertEqual(payload['cleanup']['safe_generated_residue_cleanup_errors'], [])

    def test_already_absent_generated_residue_race_is_benign(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            self.create_surface(root, ('.git', 'scripts/cron_python_entrypoint.py'))
            residue = root / 'tests' / '.pytest_cache'
            residue.mkdir(parents=True)
            scan = guard.GeneratedResidueScan((residue,), ())

            with mock.patch.object(guard, 'find_safe_generated_residue', return_value=scan):
                with mock.patch.object(guard.shutil, 'rmtree', side_effect=FileNotFoundError()):
                    cleanup = guard.cleanup_safe_generated_residue(root)

            self.assertEqual(cleanup.removed, ())
            self.assertEqual(cleanup.errors, ())
            self.assertEqual(cleanup.attention, ())

    def test_genuine_generated_residue_cleanup_oserror_remains_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            self.create_surface(root, ('.git', 'scripts/cron_python_entrypoint.py'))
            residue = root / 'tests' / '.pytest_cache'
            residue.mkdir(parents=True)
            scan = guard.GeneratedResidueScan((residue,), ())

            with mock.patch.object(guard, 'find_safe_generated_residue', return_value=scan):
                with mock.patch.object(guard.shutil, 'rmtree', side_effect=PermissionError()):
                    cleanup = guard.cleanup_safe_generated_residue(root)

            self.assertEqual(cleanup.removed, ())
            self.assertEqual(cleanup.errors, ('tests/.pytest_cache:PermissionError',))
            self.assertEqual(cleanup.attention, ())

    def test_recovery_backup_artifact_tree_is_preserved_without_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            surfaces = (guard.SurfaceSpec('control-plane', root, ('.git', 'scripts/cron_python_entrypoint.py')),)
            self.create_surface(root, surfaces[0].required)
            residue = (
                root
                / 'artifacts'
                / 'openclaw-recovery-backups'
                / 'recover-openclaw-export-map-20260505-223702'
                / 'bad-dist-openclaw-selfref'
            )
            nested_cache = residue / 'dist' / '__pycache__'
            nested_cache.mkdir(parents=True)
            (nested_cache / 'sample.pyc').write_bytes(b'cache')

            rc, output = self.run_main(surfaces)

            self.assertEqual(rc, 0)
            self.assertTrue(residue.exists())
            self.assertEqual(output, ['OpenClaw workspace cleanup passed. No disposable residue needed removal; source edits, saved memory and task work were preserved.'])
            artifact_path = self.artifact_path(root, output)
            payload = json.loads(artifact_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['cleanup']['safe_generated_residue_removed'], [])
            self.assertEqual(payload['cleanup']['safe_generated_residue_attention'], [])

    def test_scan_oserror_is_reported_as_attention_not_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            surfaces = (guard.SurfaceSpec('control-plane', root, ('.git', 'scripts/cron_python_entrypoint.py')),)
            self.create_surface(root, surfaces[0].required)
            bad_child = root / 'bad-child'
            bad_child.mkdir()
            original_is_dir = guard.os.path.isdir

            def fake_is_dir(path: Path) -> bool:
                if path == bad_child:
                    raise OSError(63, 'File name too long')
                return original_is_dir(path)

            with mock.patch.object(guard.os.path, 'isdir', fake_is_dir):
                rc, output = self.run_main(surfaces)

            self.assertEqual(rc, 0)
            self.assertIn('could not inspect 1 location;', output[0])
            artifact_path = self.artifact_path(root, output)
            payload = json.loads(artifact_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['cleanup']['safe_generated_residue_attention'], ['bad-child:scan_error:OSError'])

    def test_generated_residue_is_removed_while_legacy_dreams_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            root.mkdir(parents=True)
            subprocess.run(['/usr/bin/git', 'init'], cwd=root, check=True, capture_output=True)
            subprocess.run(['/usr/bin/git', 'config', 'user.email', 'test@example.com'], cwd=root, check=True)
            subprocess.run(['/usr/bin/git', 'config', 'user.name', 'Test User'], cwd=root, check=True)
            (root / 'scripts').mkdir()
            (root / 'scripts' / 'cron_python_entrypoint.py').write_text('entry\n', encoding='utf-8')
            subprocess.run(['/usr/bin/git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['/usr/bin/git', 'commit', '-m', 'initial'], cwd=root, check=True, capture_output=True)
            # Classified safe-remove by the drift policy: pure generated residue.
            residue_dir = root / 'Users' / 'stray'
            residue_dir.mkdir(parents=True)
            residue = residue_dir / 'misplaced.txt'
            residue.write_text('residue\n', encoding='utf-8')
            # Retained state must survive the same run untouched.
            (root / 'memory').mkdir()
            keeper = root / 'memory' / '2026-08-12.md'
            keeper.write_text('retained\n', encoding='utf-8')
            # Legacy dreaming data belongs to the memory migration owner, not
            # workspace hygiene, even when it has never been tracked by Git.
            dreams = root / 'memory' / '.dreams' / 'pending' / 'context.md'
            dreams.parent.mkdir(parents=True)
            dreams_bytes = b'Unmigrated memory evidence.\n'
            dreams.write_bytes(dreams_bytes)
            surfaces = (guard.SurfaceSpec('control-plane', root, ('.git', 'scripts/cron_python_entrypoint.py')),)

            rc, output = self.run_main(surfaces)

            self.assertEqual(rc, 0)
            self.assertFalse(residue.exists())
            self.assertTrue(keeper.exists())
            self.assertEqual(dreams.read_bytes(), dreams_bytes)
            self.assertIn('removed 1 generated file.', output[0])
            self.assertNotIn('review', output[0])
            artifact_path = self.artifact_path(root, output)
            payload = json.loads(artifact_path.read_text(encoding='utf-8'))
            dispositions = {item['disposition']: item for item in payload['git_drift']}
            self.assertIn(guard.RECONCILED_DRIFT, dispositions)
            self.assertIn(guard.RETAINED_STATE_DRIFT, dispositions)
            self.assertNotIn(guard.OPERATOR_DECISION_DRIFT, dispositions)
            self.assertEqual(payload['drift_summary'][guard.RECONCILED_DRIFT], 1)
            self.assertEqual(payload['drift_summary'][guard.RETAINED_STATE_DRIFT], 2)
            self.assertEqual(payload['cleanup']['drift_reconcile_errors'], [])

    def test_tracked_and_untracked_git_drift_is_reported_not_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            root.mkdir(parents=True)
            subprocess.run(['/usr/bin/git', 'init'], cwd=root, check=True, capture_output=True)
            subprocess.run(['/usr/bin/git', 'config', 'user.email', 'test@example.com'], cwd=root, check=True)
            subprocess.run(['/usr/bin/git', 'config', 'user.name', 'Test User'], cwd=root, check=True)
            (root / 'scripts').mkdir()
            (root / 'scripts' / 'cron_python_entrypoint.py').write_text('entry\n', encoding='utf-8')
            (root / 'tracked.txt').write_text('before\n', encoding='utf-8')
            subprocess.run(['/usr/bin/git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['/usr/bin/git', 'commit', '-m', 'initial'], cwd=root, check=True, capture_output=True)
            (root / 'tracked.txt').write_text('after\n', encoding='utf-8')
            (root / 'untracked.txt').write_text('new\n', encoding='utf-8')
            surfaces = (guard.SurfaceSpec('control-plane', root, ('.git', 'scripts/cron_python_entrypoint.py')),)

            rc, output = self.run_main(surfaces)

            self.assertEqual(rc, 0)
            self.assertTrue((root / 'tracked.txt').exists())
            self.assertTrue((root / 'untracked.txt').exists())
            self.assertIn('preserved unclassified or conflicted files for review', output[0])
            self.assertNotIn('git_drift=', output[0])
            self.assertNotIn(' M tracked.txt', output[0])
            self.assertNotIn('?? untracked.txt', output[0])
            artifact_path = self.artifact_path(root, output)
            payload = json.loads(artifact_path.read_text(encoding='utf-8'))
            entries = payload['git_drift'][0]['entries']
            self.assertIn(' M tracked.txt', entries)
            self.assertIn('?? untracked.txt', entries)
            self.assertEqual(payload['git_drift'][0]['disposition'], guard.OPERATOR_DECISION_DRIFT)
            # The operator is told exactly which paths are waiting on a decision.
            self.assertIn('tracked.txt, untracked.txt', output[0])
            self.assertIn('Nothing was reverted.', output[0])

    def test_retained_root_state_is_reported_without_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            root.mkdir(parents=True)
            subprocess.run(['/usr/bin/git', 'init'], cwd=root, check=True, capture_output=True)
            subprocess.run(['/usr/bin/git', 'config', 'user.email', 'test@example.com'], cwd=root, check=True)
            subprocess.run(['/usr/bin/git', 'config', 'user.name', 'Test User'], cwd=root, check=True)
            (root / 'scripts').mkdir()
            (root / 'scripts' / 'cron_python_entrypoint.py').write_text('entry\n', encoding='utf-8')
            subprocess.run(['/usr/bin/git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['/usr/bin/git', 'commit', '-m', 'initial'], cwd=root, check=True, capture_output=True)
            (root / 'memory').mkdir()
            (root / 'memory' / '2026-07-17.md').write_text('retained\n', encoding='utf-8')
            surfaces = (guard.SurfaceSpec('control-plane', root, ('.git', 'scripts/cron_python_entrypoint.py')),)

            rc, output = self.run_main(surfaces)

            self.assertEqual(rc, 0)
            self.assertEqual(output, ['OpenClaw workspace cleanup passed. No disposable residue needed removal; source edits, saved memory and task work were preserved.'])
            artifact_path = self.artifact_path(root, output)
            payload = json.loads(artifact_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['result'], 'success')
            self.assertEqual(payload['drift_summary'][guard.ACTIONABLE_DRIFT], 0)
            self.assertEqual(payload['drift_summary'][guard.RETAINED_STATE_DRIFT], 1)
            self.assertEqual(payload['git_drift'][0]['disposition'], guard.RETAINED_STATE_DRIFT)

    def test_registered_nonstanding_worktree_drift_is_protected_not_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'control-plane'
            root.mkdir(parents=True)
            subprocess.run(['/usr/bin/git', 'init'], cwd=root, check=True, capture_output=True)
            subprocess.run(['/usr/bin/git', 'config', 'user.email', 'test@example.com'], cwd=root, check=True)
            subprocess.run(['/usr/bin/git', 'config', 'user.name', 'Test User'], cwd=root, check=True)
            (root / 'scripts').mkdir()
            (root / 'scripts' / 'cron_python_entrypoint.py').write_text('entry\n', encoding='utf-8')
            subprocess.run(['/usr/bin/git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['/usr/bin/git', 'commit', '-m', 'initial'], cwd=root, check=True, capture_output=True)
            lane = Path(tmp) / 'task-lane'
            subprocess.run(['/usr/bin/git', 'worktree', 'add', '-b', 'task-lane', str(lane)], cwd=root, check=True, capture_output=True)
            (lane / 'task.txt').write_text('in progress\n', encoding='utf-8')
            surfaces = (guard.SurfaceSpec('control-plane', root, ('.git', 'scripts/cron_python_entrypoint.py')),)

            with mock.patch.object(guard, 'git_status_entries', wraps=guard.git_status_entries) as git_status:
                rc, output = self.run_main(surfaces)
            git_status.assert_called_once()
            self.assertEqual(git_status.call_args.args[0].resolve(), root.resolve())

            self.assertEqual(rc, 0)
            self.assertEqual(output, ['OpenClaw workspace cleanup passed. No disposable residue needed removal; source edits, saved memory and task work were preserved.'])
            artifact_path = self.artifact_path(root, output)
            payload = json.loads(artifact_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['result'], 'success')
            self.assertEqual(payload['drift_summary'][guard.ACTIONABLE_DRIFT], 0)
            self.assertEqual(payload['drift_summary'][guard.PROTECTED_TASK_LANE_DRIFT], 0)
            self.assertEqual(payload['git_drift'][0]['disposition'], guard.PROTECTED_TASK_LANE_DRIFT)
            self.assertEqual(payload['git_drift'][0]['reasons'], ['registered_nonstanding_worktree_not_scanned'])
            self.assertEqual((lane / 'task.txt').read_text(), 'in progress\n')

    def test_diagnostic_write_failure_is_nonzero_and_value_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            surfaces = (guard.SurfaceSpec('control-plane', root, ()),)
            with mock.patch.object(guard, 'write_artifacts', side_effect=OSError('private diagnostic')):
                rc, output = self.run_main(surfaces)
            self.assertEqual(rc, 1)
            self.assertIn('could not be saved', output[0])
            self.assertNotIn('private diagnostic', output[0])

    def test_git_status_failure_cannot_become_clean_or_operator_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(guard, 'is_git_worktree', return_value=True), \
                 mock.patch.object(guard, 'git_worktree_paths', return_value=(root,)), \
                 mock.patch.object(guard, 'git_status_entries', return_value=('!! git_status_failed=private diagnostic',)):
                drift, errors = guard.git_drift(root)
            self.assertEqual(drift, ())
            self.assertEqual(errors, ('!! git_status_failed=private diagnostic',))


if __name__ == '__main__':
    unittest.main()
