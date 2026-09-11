from __future__ import annotations
try:
    from scripts.operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import ast
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import io
import os
from pathlib import Path
import socket
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from scripts import openclaw_weekly_archive_backup as archive_backup
from openclaw_archive_v3_test_fixture import create_v3_fixture
from scripts.openclaw_weekly_backup_integration_proof import (
    write_real_sqlite_fixture,
    write_sqlite_fixture_cli,
)


class OpenClawWeeklyArchiveBackupTests(unittest.TestCase):
    def fixture(
        self,
        root: Path,
        *,
        min_post_backup_free_bytes: int = 0,
        session_layout: str = "legacy-uppercase-physical-v1",
    ) -> tuple[archive_backup.BackupConfig, archive_backup.VolumeIdentity]:
        mount = root / "OWC"
        owc = mount / "OpenClaw"
        workspace = owc / "Workspace"
        bare = mount / "ProjectInfrastructure/GitRemotes"
        agents = owc / ".state/OpenClaw/agents"
        legacy_sessions = owc / ".state/OpenClaw/Sessions"
        current_sessions = agents / "main/sessions"
        browser = owc / ".state/OpenClaw/Browser"
        media = owc / ".state/OpenClaw/Media"
        for directory in (
            workspace,
            bare,
            agents,
            browser,
            media,
        ):
            directory.mkdir(parents=True)
        if session_layout == "legacy-uppercase-physical-v1":
            legacy_sessions.mkdir()
            current_sessions.parent.mkdir(parents=True, exist_ok=True)
            current_sessions.symlink_to(
                legacy_sessions,
                target_is_directory=True,
            )
            sessions = legacy_sessions
        elif session_layout == "beta3-lowercase-physical-no-alias-v2":
            current_sessions.mkdir(parents=True, exist_ok=True)
            sessions = current_sessions
        else:
            raise AssertionError("unsupported session fixture layout")

        for name in archive_backup.POLICY_FILES:
            (workspace / name).write_text(
                "{} fixture\n".format(name),
                encoding="utf-8",
            )
        (workspace / "runbook").mkdir()
        (workspace / "runbook/restore.md").write_text(
            "restore fixture\n",
            encoding="utf-8",
        )
        (workspace / "memory").mkdir()
        (workspace / "memory/fact.md").write_text(
            "memory fixture\n",
            encoding="utf-8",
        )
        (workspace / "unselected.txt").write_text(
            "must not be archived\n",
            encoding="utf-8",
        )

        outside = root / "outside"
        outside.mkdir()
        (outside / "must-not-be-followed.txt").write_text(
            "outside\n",
            encoding="utf-8",
        )
        (workspace / "runbook/nested-link").symlink_to(
            outside,
            target_is_directory=True,
        )

        state = root / "home/.openclaw"
        state.mkdir(parents=True)
        (agents / "main/agent").mkdir(parents=True)
        (state / "agents").symlink_to(agents, target_is_directory=True)
        (state / "state").mkdir()
        (state / "cron").mkdir()
        (state / "openclaw.json").write_text(
            '{"fixture":true}\n',
            encoding="utf-8",
        )
        (state / "cron/jobs.json").write_text(
            '{"version":1,"jobs":[]}\n',
            encoding="utf-8",
        )
        (state / "credentials").mkdir()
        (state / "credentials/token.txt").write_text(
            "sensitive fixture\n",
            encoding="utf-8",
        )
        for database in (
            state / "state/openclaw.sqlite",
            agents / "main/agent/openclaw-agent.sqlite",
        ):
            write_real_sqlite_fixture(database, database.name)
            Path("{}-wal".format(database)).write_bytes(
                b""
            )
            Path("{}-shm".format(database)).write_bytes(
                b""
            )
            Path("{}-journal".format(database)).write_bytes(
                b""
            )
        (state / "node.json.tmp").write_text(
            "volatile fixture\n",
            encoding="utf-8",
        )
        (state / "browser").symlink_to(
            browser,
            target_is_directory=True,
        )
        (state / "media").symlink_to(
            media,
            target_is_directory=True,
        )

        shadows = (
            agents / "main/sessions.pre-owc-openclaw-sessions",
            state / "browser.pre-owc-openclaw-browser",
            state / "media.pre-owc-openclaw-media",
        )
        for shadow in shadows:
            shadow.mkdir(parents=True)
            (shadow / "stale-sensitive.txt").write_text(
                "stale\n",
                encoding="utf-8",
            )

        (sessions / "session.jsonl").write_text(
            '{"session":"fixture"}\n',
            encoding="utf-8",
        )
        (sessions / "nested-link").symlink_to(
            outside,
            target_is_directory=True,
        )
        (browser / "Preferences").write_text(
            '{"browser":"fixture"}\n',
            encoding="utf-8",
        )
        (media / "item.txt").write_text(
            "media fixture\n",
            encoding="utf-8",
        )
        repo = bare / "OpenClaw/openclaw-workspace.git"
        subprocess.run(
            ["/usr/bin/git", "init", "--bare", str(repo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        (repo / "refs/.DS_Store").write_bytes(b"Finder metadata fixture")

        workspace_logical = root / "home/OpenClawWorkspace"
        workspace_logical.symlink_to(workspace, target_is_directory=True)
        git_logical = root / "home/git-remotes"
        git_logical.symlink_to(bare, target_is_directory=True)

        fake_cli = root / "bin/openclaw"
        write_sqlite_fixture_cli(fake_cli)

        device = owc.stat().st_dev
        config = archive_backup.BackupConfig(
            owc_root=owc,
            owc_volume_mount=mount,
            expected_volume_uuid="FIXTURE-UUID",
            expected_device=None,
            state_root=state,
            workspace_logical=workspace_logical,
            git_remotes_logical=git_logical,
            backup_root=owc / "Backups/weekly",
            receipt_dir=workspace / "artifacts/archive-backup-receipts",
            min_post_backup_free_bytes=min_post_backup_free_bytes,
            openclaw_cli=fake_cli,
        )
        identity = archive_backup.VolumeIdentity(
            uuid="FIXTURE-UUID",
            device=device,
            mount=mount,
            writable=True,
            owners_enabled=True,
        )
        return config, identity

    @staticmethod
    def identity_reader(
        identity: archive_backup.VolumeIdentity,
    ):
        return lambda _config: identity

    def test_default_config_requires_the_configured_runtime_state_root(self) -> None:
        expected = archive_backup.OPERATOR.require_path('paths.state_root')
        with mock.patch.dict(os.environ, {'OPENCLAW_STATE_DIR': str(expected)}):
            self.assertEqual(archive_backup.BackupConfig().state_root, expected)
        with mock.patch.dict(os.environ, {'OPENCLAW_STATE_DIR': str(expected / 'other')}):
            with self.assertRaisesRegex(ValueError, 'disagrees'):
                archive_backup.BackupConfig()
        with mock.patch.dict(os.environ, {'OPENCLAW_STATE_DIR': ''}):
            self.assertEqual(archive_backup.BackupConfig().state_root, expected)

    def test_canonical_state_root_accepts_its_physical_agents_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-direct-state-") as raw:
            config, identity = self.fixture(
                Path(raw),
                session_layout="beta3-lowercase-physical-no-alias-v2",
            )
            direct = replace(
                config,
                state_root=config.owc_root / ".state/OpenClaw",
            )

            self.assertEqual(direct.agents_logical, direct.agents_physical)
            route = archive_backup.resolve_session_store_route(
                direct,
                identity,
            )

            self.assertEqual(
                route.layout,
                "beta3-lowercase-physical-no-alias-v2",
            )
            self.assertEqual(route.agents_logical, direct.agents_physical)

    def test_direct_managed_directory_must_match_its_declared_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-direct-mismatch-") as raw:
            config, identity = self.fixture(
                Path(raw),
                session_layout="beta3-lowercase-physical-no-alias-v2",
            )
            wrong_state = config.owc_root / ".state/OtherOpenClaw"
            (wrong_state / "agents").mkdir(parents=True)
            mismatched = replace(config, state_root=wrong_state)

            with self.assertRaisesRegex(
                archive_backup.BackupError,
                "managed physical directory target mismatch",
            ):
                archive_backup.resolve_session_store_route(
                    mismatched,
                    identity,
                )

    def test_canonical_state_root_archives_direct_managed_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-direct-archive-") as raw:
            config, identity = self.fixture(
                Path(raw),
                session_layout="beta3-lowercase-physical-no-alias-v2",
            )
            canonical_state = config.owc_root / ".state/OpenClaw"
            for name in (
                "openclaw.json",
                "cron",
                "state",
                "credentials",
                "node.json.tmp",
            ):
                (config.state_root / name).rename(canonical_state / name)
            (canonical_state / "cron/jobs.json").unlink()
            direct = replace(config, state_root=canonical_state)

            code, result = create_v3_fixture(
                direct,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(code, 0, result)
            with tarfile.open(
                Path(result["destination"]) / "openclaw-state.tgz",
                "r:gz",
            ) as archive:
                names = {member.name for member in archive}
            self.assertIn(".openclaw/agents/main/sessions/session.jsonl", names)
            self.assertIn(".openclaw/browser/Preferences", names)
            self.assertIn(".openclaw/media/item.txt", names)
            self.assertNotIn(".openclaw/cron/jobs.json", names)
            self.assertIn(
                ".openclaw/sqlite-snapshots/fixture-global/database.sqlite",
                names,
            )
            self.assertNotIn(".openclaw/Browser/Preferences", names)
            self.assertNotIn(".openclaw/Media/item.txt", names)

    def test_mounted_session_device_is_derived_after_remount(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-dynamic-device-") as raw:
            config, identity = self.fixture(Path(raw))
            accepted = archive_backup.validate_environment(
                config,
                self.identity_reader(identity),
                create_backup_root=True,
            )
            self.assertEqual(accepted.device, identity.device)

            stale = replace(config, expected_device=identity.device + 1)
            with self.assertRaisesRegex(archive_backup.BackupError, "OWC device mismatch"):
                archive_backup.validate_environment(
                    stale,
                    self.identity_reader(identity),
                    create_backup_root=True,
                )

    def test_post_flip_session_parent_is_archived_through_agents_route(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-beta3-sessions-") as raw:
            config, identity = self.fixture(
                Path(raw),
                session_layout="beta3-lowercase-physical-no-alias-v2",
            )
            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 8, 26, 2, 10, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 0, result)
            generation = Path(result["destination"])
            manifest = json.loads(
                (generation / "MANIFEST.json").read_text(encoding="utf-8")
            )
            source_contract = manifest["source_contract"]
            state_verification = next(
                row
                for row in manifest["verification"]
                if row["name"] == "openclaw-state.tgz"
            )
            self.assertEqual(
                state_verification["restore_probes"]["sessions"]["result"],
                "verified",
            )
            self.assertEqual(
                source_contract["expanded_managed_roots"][
                    str(config.agents_logical)
                ],
                str(config.agents_physical),
            )
            self.assertEqual(
                source_contract["session_store_route"],
                {
                    "layout": "beta3-lowercase-physical-no-alias-v2",
                    "logical_path": str(config.sessions_logical),
                    "agents_parent_logical": str(config.agents_logical),
                    "agents_parent_physical": str(config.agents_physical),
                    "authoritative_directory": str(
                        config.sessions_current_physical
                    ),
                    "compatibility_alias": None,
                },
            )
            with tarfile.open(
                generation / "openclaw-state.tgz",
                "r:gz",
            ) as archive:
                names = {member.name for member in archive}
            self.assertIn(
                ".openclaw/agents/main/sessions/session.jsonl",
                names,
            )
            self.assertNotIn(
                ".openclaw/agents/main/agent/openclaw-agent.sqlite",
                names,
            )

    def test_session_parent_route_rejects_non_exact_parent_and_mixed_layout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-session-parent-drift-") as raw:
            root = Path(raw)
            config, identity = self.fixture(root)
            config.agents_logical.unlink()
            config.agents_logical.symlink_to(
                Path("../../OWC/OpenClaw/.state/OpenClaw/agents"),
                target_is_directory=True,
            )
            with self.assertRaisesRegex(
                archive_backup.BackupError,
                "managed symlink target mismatch",
            ):
                archive_backup.validate_environment(
                    config,
                    self.identity_reader(identity),
                    create_backup_root=True,
                )

        with tempfile.TemporaryDirectory(prefix="openclaw-session-layout-drift-") as raw:
            config, identity = self.fixture(Path(raw))
            config.sessions_current_physical.unlink()
            config.sessions_current_physical.mkdir()
            with self.assertRaisesRegex(
                archive_backup.BackupError,
                "session store route is not a sanctioned layout",
            ):
                archive_backup.resolve_session_store_route(config, identity)

        with tempfile.TemporaryDirectory(prefix="openclaw-session-alias-drift-") as raw:
            config, identity = self.fixture(
                Path(raw),
                session_layout="beta3-lowercase-physical-no-alias-v2",
            )
            config.sessions_legacy_physical.symlink_to(
                config.sessions_current_physical,
                target_is_directory=True,
            )
            with self.assertRaisesRegex(
                archive_backup.BackupError,
                "session store route is not a sanctioned layout",
            ):
                archive_backup.resolve_session_store_route(config, identity)

    def test_manifest_keeps_the_route_selected_for_archive_capture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-session-route-bind-") as raw:
            config, identity = self.fixture(Path(raw))
            original_write = archive_backup.write_generation_metadata

            def flip_after_archive_verification(*args, **kwargs):
                selected = kwargs["session_route"]
                self.assertEqual(
                    selected.layout,
                    "legacy-uppercase-physical-v1",
                )
                config.sessions_current_physical.unlink()
                config.sessions_legacy_physical.rename(
                    config.sessions_current_physical
                )
                return original_write(*args, **kwargs)

            with mock.patch.object(
                archive_backup,
                "write_generation_metadata",
                side_effect=flip_after_archive_verification,
            ):
                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=datetime(2026, 8, 26, 2, 11, tzinfo=timezone.utc),
                )

            self.assertEqual(code, 0, result)
            manifest = json.loads(
                (
                    Path(result["destination"]) / "MANIFEST.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["source_contract"]["session_store_route"]["layout"],
                "legacy-uppercase-physical-v1",
            )
            self.assertEqual(
                archive_backup.resolve_session_store_route(
                    config,
                    identity,
                ).layout,
                "beta3-lowercase-physical-no-alias-v2",
            )

    def test_scheduled_volume_identity_uses_metadata_only_probe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-volume-metadata-") as raw:
            config, _identity = self.fixture(Path(raw))
            device = config.owc_volume_mount.stat().st_dev
            with mock.patch.object(
                archive_backup.external_volume_guard,
                "volume_metadata",
                return_value={
                    "available": True,
                    "mountDevice": device,
                    "mountFlags": 0,
                    "mountPoint": str(config.owc_volume_mount),
                    "mounted": True,
                    "ownersEnabled": True,
                    "probeMethod": "getattrlist-statvfs",
                    "readOnly": False,
                    "volumeUuid": "FIXTURE-UUID",
                },
            ) as metadata_probe:
                observed = archive_backup.read_volume_identity(config)
            metadata_probe.assert_called_once_with(config.owc_volume_mount)
            self.assertEqual(observed.uuid, "FIXTURE-UUID")
            self.assertEqual(observed.device, device)
            self.assertTrue(observed.writable)
            self.assertTrue(observed.owners_enabled)

        syntax = ast.parse(
            Path(archive_backup.__file__).read_text(encoding="utf-8")
        )
        string_literals = {
            node.value.lower()
            for node in ast.walk(syntax)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertFalse(
            any(
                command in literal
                for literal in string_literals
                for command in ("diskutil", "smartctl", "system_profiler")
            )
        )

    def test_sqlite_snapshot_repository_uses_stock_cli_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-sqlite-cli-") as raw:
            config, identity = self.fixture(Path(raw))
            repository = config.owc_root / "sqlite-snapshot-fixture"
            original_run = subprocess.run
            with mock.patch.object(
                archive_backup.subprocess,
                "run",
                wraps=original_run,
            ) as run:
                snapshots = archive_backup.create_sqlite_snapshot_repository(
                    config,
                    repository,
                    expected_device=identity.device,
                )

            self.assertEqual(snapshots.agent_ids, ("main",))
            self.assertEqual(
                {path.name for path in snapshots.snapshot_paths},
                {"fixture-global", "fixture-agent-main"},
            )
            commands = [call.args[0][1:] for call in run.call_args_list]
            self.assertEqual(commands[0], ["agents", "list", "--json"])
            self.assertEqual(
                commands[1],
                [
                    "backup",
                    "sqlite",
                    "create",
                    "--agent",
                    "main",
                    "--repository",
                    str(repository),
                    "--json",
                ],
            )
            self.assertEqual(
                commands[2],
                [
                    "backup",
                    "sqlite",
                    "create",
                    "--global",
                    "--repository",
                    str(repository),
                    "--json",
                ],
            )
            self.assertEqual(len(commands), 3)
            self.assertTrue(
                all(
                    call.kwargs["env"]["OPENCLAW_STATE_DIR"]
                    == str(config.state_root)
                    for call in run.call_args_list
                )
            )

    def test_sqlite_cli_failure_is_value_free_and_never_publishes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-sqlite-fail-") as raw:
            config, identity = self.fixture(Path(raw))

            def fail_snapshot(*_args, **_kwargs):
                raise archive_backup.BackupError(
                    "OpenClaw CLI backup sqlite create failed with exit 2"
                )

            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc),
                sqlite_snapshot_creator=fail_snapshot,
            )
            self.assertEqual(code, 1)
            self.assertEqual(result["result"], "blocked")
            self.assertIn("failed with exit 2", result["blockers"][0])
            self.assertFalse(Path(result["destination"]).exists())
            self.assertTrue(Path(result["staging"]).is_dir())

            secret = "private-child-error-value"
            failed = subprocess.CompletedProcess(
                args=[],
                returncode=2,
                stdout="",
                stderr=secret,
            )
            with mock.patch.object(
                archive_backup.subprocess,
                "run",
                return_value=failed,
            ):
                with self.assertRaises(archive_backup.BackupError) as raised:
                    archive_backup.create_sqlite_snapshot_repository(
                        config,
                        config.owc_root / "failed-sqlite-snapshots",
                        expected_device=identity.device,
                    )
            self.assertNotIn(secret, str(raised.exception))

    def test_child_manifest_is_not_proof_and_packaged_bytes_are_verified(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-sqlite-packaged-verify-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            script = config.openclaw_cli.read_text(encoding="utf-8")
            config.openclaw_cli.write_text(
                script.replace(
                    '"manifest": manifest,',
                    '"manifest": {"untrusted": True},',
                    1,
                ),
                encoding="utf-8",
            )
            os.chmod(config.openclaw_cli, 0o755)
            original_run = subprocess.run
            with mock.patch.object(
                archive_backup.subprocess,
                "run",
                wraps=original_run,
            ) as run:
                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=datetime(
                        2026,
                        8,
                        26,
                        1,
                        30,
                        tzinfo=timezone.utc,
                    ),
                )

            self.assertEqual(code, 0, result)
            verify_commands = [
                call.args[0]
                for call in run.call_args_list
                if len(call.args[0]) >= 4
                and Path(call.args[0][0]).resolve()
                == config.openclaw_cli.resolve()
                and call.args[0][1:4]
                == ["backup", "sqlite", "verify"]
            ]
            self.assertEqual(len(verify_commands), 2)
            self.assertTrue(
                all(
                    command[-1] == "--json"
                    and "packaged-sqlite-snapshots"
                    in Path(command[4]).parts
                    for command in verify_commands
                )
            )
            manifest = archive_backup.load_manifest(
                Path(result["destination"])
            )
            state_verification = next(
                row
                for row in manifest["verification"]
                if row["name"] == "openclaw-state.tgz"
            )
            identities = [
                (
                    row["snapshot_id"],
                    row["role"],
                    row["agent_id"],
                    row["result"],
                )
                for row in state_verification[
                    "sqlite_snapshot_verification"
                ]
            ]
            self.assertEqual(
                identities,
                [
                    ("fixture-agent-main", "agent", "main", "verified"),
                    ("fixture-global", "global", None, "verified"),
                ],
            )

    def test_packaged_sqlite_manifest_artifact_mismatch_blocks_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-sqlite-mismatch-"
        ) as raw:
            config, identity = self.fixture(Path(raw))

            def corrupt_snapshot(*args, **kwargs):
                snapshots = archive_backup.create_sqlite_snapshot_repository(
                    *args,
                    **kwargs,
                )
                artifact = snapshots.snapshot_paths[0] / "database.sqlite"
                with artifact.open("ab") as handle:
                    handle.write(b"mismatched-packaged-byte")
                return snapshots

            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(
                    2026,
                    8,
                    26,
                    1,
                    31,
                    tzinfo=timezone.utc,
                ),
                sqlite_snapshot_creator=corrupt_snapshot,
            )
            self.assertEqual(code, 1)
            self.assertFalse(Path(result["destination"]).exists())
            self.assertTrue(Path(result["staging"]).is_dir())
            self.assertIn("backup sqlite verify failed", result["blockers"][0])

    def test_verifier_manifest_must_equal_packaged_on_disk_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-sqlite-verifier-manifest-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            script = config.openclaw_cli.read_text(encoding="utf-8")
            needle = '"manifest": manifest,'
            second = script.find(needle, script.find(needle) + len(needle))
            self.assertGreater(second, 0)
            script = (
                script[:second]
                + '"manifest": dict(manifest, snapshotId="returned-only"),'
                + script[second + len(needle):]
            )
            config.openclaw_cli.write_text(script, encoding="utf-8")
            os.chmod(config.openclaw_cli, 0o755)

            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(
                    2026,
                    8,
                    26,
                    1,
                    35,
                    tzinfo=timezone.utc,
                ),
            )
            self.assertEqual(code, 1)
            self.assertFalse(Path(result["destination"]).exists())
            self.assertIn(
                "differs from the stock verifier result",
                result["blockers"][0],
            )

    def test_packaged_sqlite_agent_identity_mismatch_blocks_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-sqlite-identity-"
        ) as raw:
            config, identity = self.fixture(Path(raw))

            def relabel_snapshot(*args, **kwargs):
                snapshots = archive_backup.create_sqlite_snapshot_repository(
                    *args,
                    **kwargs,
                )
                manifest_path = (
                    snapshots.snapshot_paths[0] / "manifest.json"
                )
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                manifest["database"]["agentId"] = "different-agent"
                manifest_path.write_text(
                    json.dumps(
                        manifest,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.chmod(manifest_path, 0o600)
                return snapshots

            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(
                    2026,
                    8,
                    26,
                    1,
                    32,
                    tzinfo=timezone.utc,
                ),
                sqlite_snapshot_creator=relabel_snapshot,
            )
            self.assertEqual(code, 1)
            self.assertFalse(Path(result["destination"]).exists())
            self.assertIn(
                "agent identity mismatch",
                result["blockers"][0],
            )

    def test_orphan_agent_database_and_all_sidecars_fail_before_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-sqlite-orphan-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            orphan = (
                config.state_root
                / "agents/orphan/agent/openclaw-agent.sqlite"
            )
            write_real_sqlite_fixture(orphan, "orphan")
            orphan_paths = [
                Path("{}{}".format(orphan, suffix))
                for suffix in archive_backup.SQLITE_DATABASE_SIDECAR_SUFFIXES
            ]
            for sidecar in orphan_paths[1:]:
                sidecar.write_bytes(b"")
            creator = mock.Mock(
                side_effect=AssertionError("snapshot creation must not run")
            )

            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(
                    2026,
                    8,
                    26,
                    1,
                    33,
                    tzinfo=timezone.utc,
                ),
                sqlite_snapshot_creator=creator,
            )
            self.assertEqual(code, 1)
            creator.assert_not_called()
            self.assertIn(
                "unconfigured OpenClaw SQLite database or sidecar",
                result["blockers"][0],
            )
            self.assertTrue(all(path.exists() for path in orphan_paths))
            self.assertFalse(Path(result["destination"]).exists())

    def test_mis_cased_authoritative_sqlite_entries_fail_before_snapshot(
        self,
    ) -> None:
        cases = (
            (
                "global database",
                Path("state/openclaw.sqlite"),
                "OpenClaw.sqlite",
            ),
            (
                "agent database",
                Path("agents/main/agent/openclaw-agent.sqlite"),
                "OpenClaw-Agent.sqlite",
            ),
            (
                "global WAL",
                Path("state/openclaw.sqlite-wal"),
                "openclaw.sqlite-WAL",
            ),
            (
                "agent SHM",
                Path("agents/main/agent/openclaw-agent.sqlite-shm"),
                "openclaw-agent.sqlite-SHM",
            ),
        )
        for label, relative, replacement_name in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory(
                    prefix="openclaw-sqlite-case-"
                ) as raw:
                    config, identity = self.fixture(Path(raw))
                    source = config.state_root / relative
                    intermediate = source.with_name(
                        ".case-rename-{}".format(source.name)
                    )
                    replacement = source.with_name(replacement_name)
                    source.rename(intermediate)
                    intermediate.rename(replacement)
                    self.assertIn(
                        replacement_name,
                        {child.name for child in source.parent.iterdir()},
                    )
                    creator = mock.Mock(
                        side_effect=AssertionError(
                            "snapshot creation must not run"
                        )
                    )

                    code, result = create_v3_fixture(
                        config,
                        identity_reader=self.identity_reader(identity),
                        now=datetime(
                            2026,
                            8,
                            26,
                            1,
                            33,
                            30,
                            tzinfo=timezone.utc,
                        ),
                        sqlite_snapshot_creator=creator,
                    )

                    self.assertEqual(code, 1)
                    creator.assert_not_called()
                    self.assertIn(
                        "SQLite database or sidecar has non-canonical casing",
                        result["blockers"][0],
                    )
                    self.assertFalse(Path(result["destination"]).exists())

    def test_case_only_sqlite_drift_after_snapshot_never_publishes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-sqlite-case-race-"
        ) as raw:
            config, identity = self.fixture(Path(raw))

            def create_then_recase(*args, **kwargs):
                snapshots = (
                    archive_backup.create_sqlite_snapshot_repository(
                        *args,
                        **kwargs,
                    )
                )
                source = config.state_root / "state/openclaw.sqlite"
                intermediate = source.with_name(".case-rename-openclaw.sqlite")
                replacement = source.with_name("OpenClaw.sqlite")
                source.rename(intermediate)
                intermediate.rename(replacement)
                return snapshots

            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(
                    2026,
                    8,
                    26,
                    1,
                    33,
                    31,
                    tzinfo=timezone.utc,
                ),
                sqlite_snapshot_creator=create_then_recase,
            )

            self.assertEqual(code, 1)
            self.assertIn(
                "state traversal reached an authoritative live SQLite path",
                result["blockers"][0],
            )
            self.assertFalse(Path(result["destination"]).exists())

    def test_publication_verifier_rejects_mis_cased_live_sqlite_members(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-sqlite-case-verifier-"
        ) as raw:
            root = Path(raw)
            config, identity = self.fixture(root)
            spec = archive_backup.ArchiveSpec(
                filename="openclaw-state.tgz",
                role="state",
                roots=(),
                required_members=frozenset({".openclaw"}),
                required_probe_groups=frozenset(),
            )
            live_variants = (
                ".openclaw/state/OpenClaw.sqlite",
                ".openclaw/agents/main/agent/OpenClaw-Agent.sqlite",
                ".openclaw/state/openclaw.sqlite-WAL",
                ".openclaw/agents/main/agent/openclaw-agent.sqlite-SHM",
            )
            for index, live_variant in enumerate(live_variants):
                with self.subTest(live_variant=live_variant):
                    injected = root / "injected-{}.tgz".format(index)
                    payload = b"live SQLite bytes must never be admitted\n"
                    with tarfile.open(
                        injected,
                        mode="w:gz",
                        format=tarfile.PAX_FORMAT,
                    ) as archive:
                        root_info = tarfile.TarInfo(".openclaw")
                        root_info.type = tarfile.DIRTYPE
                        root_info.mode = 0o700
                        root_info.mtime = 0
                        archive.addfile(root_info)
                        live_info = tarfile.TarInfo(live_variant)
                        live_info.mode = 0o600
                        live_info.mtime = 0
                        live_info.size = len(payload)
                        archive.addfile(live_info, io.BytesIO(payload))
                    os.chmod(injected, 0o600)

                    member_digest = hashlib.sha256()
                    with tarfile.open(injected, mode="r:gz") as archive:
                        for member in archive:
                            member_digest.update(
                                archive_backup.canonical_json(
                                    archive_backup.member_contract_record(
                                        member
                                    )
                                )
                                + b"\n"
                            )
                    expected = {
                        "logical_bytes": injected.stat().st_size,
                        "sha256": archive_backup.sha256_file(injected),
                        "member_contract_sha256": member_digest.hexdigest(),
                        "restore_candidates": {},
                    }

                    with self.assertRaisesRegex(
                        archive_backup.BackupError,
                        "authoritative live SQLite path",
                    ):
                        archive_backup.verify_archive(
                            injected,
                            spec,
                            expected,
                            probe_root=root / "probe-{}".format(index),
                            expected_device=identity.device,
                            config=config,
                        )

    def test_pre_snapshot_headroom_covers_conservative_source_peak(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-sqlite-headroom-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            agent_ids = archive_backup.configured_agent_ids(config)
            inventory = archive_backup.authoritative_sqlite_source_inventory(
                config,
                agent_ids,
            )
            self.assertGreater(inventory["source_logical_bytes"], 0)
            self.assertEqual(
                inventory["snapshot_peak_headroom_bytes"],
                archive_backup.RESTORE_METADATA_BASE_BYTES
                + archive_backup.SQLITE_SNAPSHOT_PEAK_SOURCE_MULTIPLIER
                * inventory["source_logical_bytes"],
            )
            creator = mock.Mock(
                side_effect=AssertionError("snapshot creation must not run")
            )
            insufficient = mock.Mock(
                free=(
                    config.min_post_backup_free_bytes
                    + inventory["snapshot_peak_headroom_bytes"]
                    - 1
                )
            )
            with mock.patch.object(
                archive_backup.shutil,
                "disk_usage",
                return_value=insufficient,
            ):
                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=datetime(
                        2026,
                        8,
                        26,
                        1,
                        34,
                        tzinfo=timezone.utc,
                    ),
                    sqlite_snapshot_creator=creator,
                )
            self.assertEqual(code, 1)
            creator.assert_not_called()
            self.assertIn(
                "insufficient OWC headroom before SQLite snapshots",
                result["blockers"][0],
            )
            self.assertFalse(Path(result["staging"]).exists())

    def test_creates_exact_three_archives_with_verified_sqlite_snapshots(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-three-archive-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            code_one, first = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc),
            )
            code_two, second = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(code_one, 0, first)
            self.assertEqual(code_two, 0, second)
            first_path = Path(first["destination"])
            second_path = Path(second["destination"])
            self.assertTrue(first_path.name.startswith("openclaw-archive-v3-"))
            self.assertTrue(second_path.name.startswith("openclaw-archive-v3-"))
            self.assertEqual(
                {child.name for child in first_path.iterdir()},
                set(archive_backup.EXPECTED_GENERATION_FILES),
            )
            self.assertTrue(first_path.is_dir())
            self.assertTrue(second_path.is_dir())
            self.assertEqual(
                list(
                    config.backup_root.glob(
                        ".openclaw-archive-v3-*.incomplete-*"
                    )
                ),
                [],
            )
            self.assertEqual(
                stat.S_IMODE(first_path.stat().st_mode),
                0o700,
            )
            for child in first_path.iterdir():
                self.assertTrue(child.is_file())
                self.assertFalse(child.is_symlink())
                self.assertEqual(stat.S_IMODE(child.stat().st_mode), 0o600)

            for name in archive_backup.EXPECTED_ARCHIVE_NAMES:
                if name != "openclaw-state.tgz":
                    self.assertEqual(
                        archive_backup.sha256_file(first_path / name),
                        archive_backup.sha256_file(second_path / name),
                        name,
                    )

            manifest = json.loads(
                (first_path / "MANIFEST.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["payload_contract"],
                "three-gzip-tarballs-v1",
            )
            self.assertEqual(
                manifest["archive_names"],
                list(archive_backup.EXPECTED_ARCHIVE_NAMES),
            )
            self.assertFalse(manifest["icloud_offload"])
            self.assertFalse(manifest["source_tree_atomic"])
            self.assertTrue(manifest["contains_sensitive_state"])
            self.assertFalse(manifest["retention"]["performed"])
            self.assertEqual(
                manifest["sqlite_backup"]["command_contract"],
                "openclaw backup sqlite create --json",
            )
            self.assertEqual(
                manifest["sqlite_backup"]["agent_ids"],
                ["main"],
            )
            self.assertEqual(
                manifest["sqlite_backup"]["snapshot_count"],
                2,
            )
            self.assertEqual(
                manifest["source_contract"][
                    "git_regular_basename_exclusions"
                ],
                [".DS_Store"],
            )
            self.assertIn(
                "free_after_archive_payloads_bytes",
                manifest["preflight"]["headroom"],
            )
            self.assertIn(
                "actual_restore_verification_budget",
                manifest["preflight"]["headroom"],
            )
            self.assertTrue(
                all(
                    row["archive_to_source_logical_ratio"] is not None
                    and row["source_to_archive_logical_ratio"] is not None
                    for row in manifest["archives"]
                )
            )
            self.assertTrue(
                all(
                    row["full_decompression"]
                    for row in manifest["verification"]
                )
            )
            git_verification = next(
                row
                for row in manifest["verification"]
                if row["name"] == "git-remotes.tgz"
            )
            self.assertTrue(
                git_verification["full_isolated_restore"]["performed"]
            )
            self.assertEqual(
                git_verification["full_isolated_restore"]["git_fsck"][
                    "repository_count"
                ],
                1,
            )
            git_archive = next(
                row
                for row in manifest["archives"]
                if row["name"] == "git-remotes.tgz"
            )
            self.assertEqual(
                git_archive["source"]["excluded_git_finder_metadata"],
                1,
            )
            state_verification = next(
                row
                for row in manifest["verification"]
                if row["name"] == "openclaw-state.tgz"
            )
            self.assertEqual(
                state_verification["restore_probes"]["sqlite-snapshots"][
                    "result"
                ],
                "verified",
            )
            with tarfile.open(
                first_path / "openclaw-state.tgz",
                "r:gz",
            ) as archive:
                state_names = {member.name for member in archive}
            self.assertIn(
                ".openclaw/sqlite-snapshots/fixture-global/database.sqlite",
                state_names,
            )
            self.assertIn(".openclaw/cron/jobs.json", state_names)
            self.assertIn(
                ".openclaw/sqlite-snapshots/fixture-agent-main/database.sqlite",
                state_names,
            )
            for live_name in (
                ".openclaw/state/openclaw.sqlite",
                ".openclaw/state/openclaw.sqlite-wal",
                ".openclaw/state/openclaw.sqlite-shm",
                ".openclaw/state/openclaw.sqlite-journal",
                ".openclaw/agents/main/agent/openclaw-agent.sqlite",
                ".openclaw/agents/main/agent/openclaw-agent.sqlite-wal",
                ".openclaw/agents/main/agent/openclaw-agent.sqlite-shm",
                ".openclaw/agents/main/agent/openclaw-agent.sqlite-journal",
            ):
                self.assertNotIn(live_name, state_names)
            with tarfile.open(
                first_path / "git-remotes.tgz",
                "r:gz",
            ) as archive:
                self.assertFalse(
                    any(
                        member.name.endswith("/.DS_Store")
                        for member in archive
                    )
                )
            policy_verification = next(
                row
                for row in manifest["verification"]
                if row["name"]
                == "openclaw-workspace-policy.tgz"
            )
            self.assertTrue(
                policy_verification["full_isolated_restore"]["performed"]
            )
            self.assertEqual(first["verification"]["archive_count"], 3)
            self.assertFalse(first["retention_performed"])
            self.assertEqual(
                (first_path / "INDEX.md")
                .read_text(encoding="utf-8")
                .count("Payload contract:"),
                1,
            )
            receipt_parent = Path(first["receipt"]).parent
            self.assertEqual(
                stat.S_IMODE(receipt_parent.stat().st_mode),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE(Path(first["receipt"]).stat().st_mode),
                0o600,
            )
            receipt = json.loads(
                Path(first["receipt"]).read_text(encoding="utf-8")
            )
            headroom_phase = next(
                row
                for row in receipt["phases"]
                if row["name"] == "post-archive-headroom"
            )
            self.assertIn(
                "free_after_archive_payloads_bytes",
                headroom_phase["headroom"],
            )
            self.assertEqual(len(headroom_phase["compression"]), 3)

    def test_member_scope_expands_only_exact_live_roots(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="v3-scope-",
            dir=tempfile.gettempdir(),
        ) as raw:
            config, identity = self.fixture(Path(raw))
            socket_path = config.state_root / "e.sock"
            live_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                live_socket.bind(str(socket_path))
                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=datetime(
                        2026,
                        7,
                        25,
                        18,
                        1,
                        tzinfo=timezone.utc,
                    ),
                )
            finally:
                live_socket.close()
                socket_path.unlink(missing_ok=True)
            self.assertEqual(code, 0, result)
            generation = Path(result["destination"])
            manifest = json.loads(
                (generation / "MANIFEST.json").read_text(encoding="utf-8")
            )
            state_archive = next(
                row
                for row in manifest["archives"]
                if row["name"] == "openclaw-state.tgz"
            )
            self.assertEqual(
                state_archive["source"]["excluded_unix_sockets"],
                1,
            )

            with tarfile.open(
                generation / "openclaw-state.tgz",
                "r:gz",
            ) as archive:
                state_members = {row.name: row for row in archive}
            self.assertIn(
                ".openclaw/agents/main/sessions/session.jsonl",
                state_members,
            )
            self.assertIn(
                ".openclaw/browser/Preferences",
                state_members,
            )
            self.assertIn(
                ".openclaw/media/item.txt",
                state_members,
            )
            self.assertIn(
                ".openclaw/credentials/token.txt",
                state_members,
            )
            self.assertNotIn(".openclaw/node.json.tmp", state_members)
            self.assertNotIn(
                ".openclaw/e.sock",
                state_members,
            )
            self.assertTrue(
                state_members[
                    ".openclaw/agents/main/sessions/nested-link"
                ].issym()
            )
            self.assertNotIn(
                ".openclaw/agents/main/sessions/nested-link/"
                "must-not-be-followed.txt",
                state_members,
            )
            self.assertFalse(
                any(".pre-owc-openclaw-" in name for name in state_members)
            )

            with tarfile.open(
                generation / "openclaw-workspace-policy.tgz",
                "r:gz",
            ) as archive:
                policy_members = {row.name: row for row in archive}
            self.assertIn("AGENTS.md", policy_members)
            self.assertIn("runbook/restore.md", policy_members)
            self.assertIn("memory/fact.md", policy_members)
            self.assertTrue(policy_members["runbook/nested-link"].issym())
            self.assertNotIn("unselected.txt", policy_members)
            self.assertNotIn(
                "runbook/nested-link/must-not-be-followed.txt",
                policy_members,
            )

            with tarfile.open(
                generation / "git-remotes.tgz",
                "r:gz",
            ) as archive:
                git_names = {row.name for row in archive}
            self.assertIn(
                "git-remotes/OpenClaw/openclaw-workspace.git/HEAD",
                git_names,
            )

    def test_explicit_browser_cache_subtree_is_excluded_before_descent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-browser-cache-churn-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            cache_root = (
                config.browser_physical
                / "openclaw/user-data/Default/Code Cache/js"
            )
            cache_root.mkdir(parents=True)
            churn_member = cache_root / "9b46c864bdb50511_0"
            churn_member.write_bytes(b"derived browser cache\n")
            nested_cache_root = (
                config.browser_physical
                / "openclaw/user-data/Default/WebStorage/27/CacheStorage"
            )
            nested_cache_root.mkdir(parents=True)
            nested_churn_member = nested_cache_root / "cache-entry"
            nested_churn_member.write_bytes(
                b"derived nested browser cache\n"
            )
            unsupported_cache = (
                config.browser_physical
                / "unmanaged/Default/Code Cache"
            )
            unsupported_cache.mkdir(parents=True)
            (unsupported_cache / "must-retain.txt").write_text(
                "unsupported layout is required\n",
                encoding="utf-8",
            )
            profile_one = (
                config.browser_physical
                / "openclaw/user-data/Profile 1"
            )
            profile_one.mkdir(parents=True)
            (profile_one / "Code Cache").write_text(
                "cache-like regular file is required\n",
                encoding="utf-8",
            )
            profile_two = (
                config.browser_physical
                / "openclaw/user-data/Profile 2"
            )
            profile_two.mkdir(parents=True)
            cache_like_target = (
                config.browser_physical / "cache-like-symlink-target"
            )
            cache_like_target.write_text(
                "cache-like symlink target\n",
                encoding="utf-8",
            )
            (profile_two / "GPUCache").symlink_to(cache_like_target)
            churn_member_names = {
                churn_member.name,
                nested_churn_member.name,
            }
            original_stat = archive_backup.os.stat
            original_open = archive_backup.os.open
            churn_stat_attempts = 0
            churn_open_attempts = 0

            def disappear_after_enumeration(path, *args, **kwargs):
                nonlocal churn_stat_attempts
                if (
                    path in churn_member_names
                    and kwargs.get("dir_fd") is not None
                ):
                    churn_stat_attempts += 1
                    if path == churn_member.name:
                        churn_member.unlink(missing_ok=True)
                    else:
                        nested_churn_member.unlink(missing_ok=True)
                    raise FileNotFoundError(path)
                return original_stat(path, *args, **kwargs)

            def replace_before_open(path, flags, *args, **kwargs):
                nonlocal churn_open_attempts
                if (
                    path in churn_member_names
                    and kwargs.get("dir_fd") is not None
                ):
                    churn_open_attempts += 1
                    target = (
                        churn_member
                        if path == churn_member.name
                        else nested_churn_member
                    )
                    replacement = target.with_suffix(".replacement")
                    target.rename(replacement)
                    target.write_bytes(b"replacement cache inode\n")
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                archive_backup.os,
                "stat",
                side_effect=disappear_after_enumeration,
            ), mock.patch.object(
                archive_backup.os,
                "open",
                side_effect=replace_before_open,
            ):
                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=datetime(
                        2026,
                        7,
                        25,
                        18,
                        10,
                        tzinfo=timezone.utc,
                    ),
                )

            self.assertEqual(code, 0, result)
            self.assertEqual(churn_stat_attempts, 0)
            self.assertEqual(churn_open_attempts, 0)
            recognized_cache_roots = (
                (
                    ".openclaw/browser/openclaw/user-data/"
                    "GrShaderCache"
                ),
                (
                    ".openclaw/browser/openclaw/user-data/"
                    "Default/Code Cache"
                ),
                (
                    ".openclaw/browser/openclaw/user-data/"
                    "Profile 12/GPUCache"
                ),
                (
                    ".openclaw/browser/openclaw/user-data/"
                    "System Profile/Cache"
                ),
                (
                    ".openclaw/browser/openclaw/user-data/"
                    "Guest Profile/DawnWebGPUCache"
                ),
                (
                    ".openclaw/browser/chrome-automation-profile/"
                    "Default/Service Worker/CacheStorage"
                ),
                (
                    ".openclaw/browser/chrome-automation-profile/"
                    "Default/WebStorage/27/CacheStorage"
                ),
                (
                    ".openclaw/browser/chrome-automation-profile/"
                    "Default/Shared Dictionary/cache"
                ),
                (
                    ".openclaw/browser/chrome-automation-profile/"
                    "Default/Storage/ext/"
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/def/Code Cache"
                ),
            )
            for cache_root in recognized_cache_roots:
                self.assertTrue(
                    archive_backup.is_explicit_browser_cache_subtree(
                        cache_root
                    )
                )
            unrecognized_paths = (
                (
                    ".openclaw/browser/unmanaged/Default/"
                    "Code Cache"
                ),
                (
                    ".openclaw/browser/openclaw/user-data/"
                    "Profile X/Code Cache"
                ),
                (
                    ".openclaw/browser/openclaw/user-data/"
                    "Profile 0/Code Cache"
                ),
                (
                    ".openclaw/browser/openclaw/user-data/"
                    "Default/Code Cache/js/member"
                ),
                (
                    ".openclaw/browser/openclaw/user-data/"
                    "Default/IndexedDB"
                ),
                (
                    ".openclaw/browser/chrome-automation-profile/"
                    "Default/WebStorage/not-a-number/CacheStorage"
                ),
                (
                    ".openclaw/browser/chrome-automation-profile/"
                    "Default/Storage/ext/not-an-extension-id/def/Cache"
                ),
                ".openclaw/cron/Code Cache",
            )
            for path in unrecognized_paths:
                self.assertFalse(
                    archive_backup.is_explicit_browser_cache_subtree(path),
                    path,
                )
            generation = Path(result["destination"])
            manifest = json.loads(
                (generation / "MANIFEST.json").read_text(encoding="utf-8")
            )
            state_record = next(
                row
                for row in manifest["archives"]
                if row["name"] == "openclaw-state.tgz"
            )
            self.assertEqual(
                state_record["source"][
                    "excluded_browser_cache_subtrees"
                ],
                2,
            )
            expected_exclusions = [
                ".openclaw/browser/openclaw/user-data/Default/Code Cache",
                (
                    ".openclaw/browser/openclaw/user-data/Default/"
                    "WebStorage/27/CacheStorage"
                ),
            ]
            expected_exclusions.sort()
            self.assertEqual(
                state_record["source"][
                    "excluded_browser_cache_paths_sha256"
                ],
                archive_backup.sha256_bytes(
                    archive_backup.canonical_json(expected_exclusions)
                ),
            )
            self.assertEqual(
                manifest["source_contract"][
                    "browser_volatile_cache_policy"
                ],
                (
                    "stat-physical-same-device-directory-then-exclude-"
                    "before-descent-or-member-traversal"
                ),
            )
            self.assertEqual(
                manifest["source_contract"][
                    "browser_extension_cache_relatives"
                ],
                sorted(
                    path.as_posix()
                    for path in (
                        archive_backup.BROWSER_EXTENSION_CACHE_RELATIVES
                    )
                ),
            )
            with tarfile.open(
                generation / "openclaw-state.tgz",
                "r:gz",
            ) as archive:
                members = {member.name: member for member in archive}
            names = set(members)
            self.assertFalse(
                any(
                    name == exclusion
                    or name.startswith(exclusion + "/")
                    for name in names
                    for exclusion in expected_exclusions
                )
            )
            self.assertIn(".openclaw/browser/Preferences", names)
            self.assertIn(
                ".openclaw/browser/unmanaged/Default/Code Cache",
                names,
            )
            self.assertIn(
                (
                    ".openclaw/browser/unmanaged/Default/"
                    "Code Cache/must-retain.txt"
                ),
                names,
            )
            self.assertIn(
                ".openclaw/browser/openclaw/user-data/Profile 1/Code Cache",
                names,
            )
            self.assertIn(
                ".openclaw/browser/openclaw/user-data/Profile 2/GPUCache",
                names,
            )
            self.assertTrue(
                members[
                    (
                        ".openclaw/browser/openclaw/user-data/"
                        "Profile 1/Code Cache"
                    )
                ].isfile()
            )
            self.assertTrue(
                members[
                    (
                        ".openclaw/browser/openclaw/user-data/"
                        "Profile 2/GPUCache"
                    )
                ].issym()
            )

    def test_reconstructible_plugin_runtime_cache_is_excluded_before_descent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-plugin-runtime-cache-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            cache_root = config.state_root / "plugin-runtime-deps"
            cached_module = (
                cache_root
                / "openclaw-fixture/node_modules/example/index.js"
            )
            cached_module.parent.mkdir(parents=True)
            cached_module.write_text(
                "derived runtime dependency\n",
                encoding="utf-8",
            )
            lookalike = config.state_root / "plugin-runtime-deps-archive"
            lookalike.mkdir()
            (lookalike / "must-retain.txt").write_text(
                "not the exact reconstructible root\n",
                encoding="utf-8",
            )

            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 7, 25, 18, 12, tzinfo=timezone.utc),
            )

            self.assertEqual(code, 0, result)
            self.assertTrue(
                archive_backup.is_reconstructible_runtime_cache_subtree(
                    ".openclaw/plugin-runtime-deps"
                )
            )
            self.assertFalse(
                archive_backup.is_reconstructible_runtime_cache_subtree(
                    ".openclaw/plugin-runtime-deps/node_modules"
                )
            )
            self.assertFalse(
                archive_backup.is_reconstructible_runtime_cache_subtree(
                    ".openclaw/plugin-runtime-deps-archive"
                )
            )
            generation = Path(result["destination"])
            manifest = json.loads(
                (generation / "MANIFEST.json").read_text(encoding="utf-8")
            )
            state_record = next(
                row
                for row in manifest["archives"]
                if row["name"] == "openclaw-state.tgz"
            )
            expected_paths = [".openclaw/plugin-runtime-deps"]
            self.assertEqual(
                state_record["source"][
                    "excluded_reconstructible_runtime_cache_subtrees"
                ],
                1,
            )
            self.assertEqual(
                state_record["source"][
                    "excluded_reconstructible_runtime_cache_paths"
                ],
                expected_paths,
            )
            self.assertEqual(
                state_record["source"][
                    "excluded_reconstructible_runtime_cache_paths_sha256"
                ],
                archive_backup.sha256_bytes(
                    archive_backup.canonical_json(expected_paths)
                ),
            )
            source_contract = manifest["source_contract"]
            self.assertEqual(
                source_contract[
                    "reconstructible_runtime_cache_archive_roots"
                ],
                expected_paths,
            )
            self.assertEqual(
                source_contract["reconstructible_runtime_cache_policy"],
                archive_backup.RECONSTRUCTIBLE_RUNTIME_CACHE_POLICY,
            )
            self.assertIn(
                "not authoritative user state",
                source_contract[
                    "reconstructible_runtime_cache_restore_rationale"
                ],
            )
            with tarfile.open(
                generation / "openclaw-state.tgz",
                "r:gz",
            ) as archive:
                names = {member.name for member in archive}
            self.assertFalse(
                any(
                    name == expected_paths[0]
                    or name.startswith(expected_paths[0] + "/")
                    for name in names
                )
            )
            self.assertIn(
                ".openclaw/plugin-runtime-deps-archive/must-retain.txt",
                names,
            )

    def test_plugin_runtime_cache_file_or_symlink_is_not_silently_excluded(
        self,
    ) -> None:
        for entry_type in ("file", "symlink"):
            with self.subTest(entry_type=entry_type), tempfile.TemporaryDirectory(
                prefix="openclaw-plugin-runtime-cache-nondirectory-"
            ) as raw:
                config, identity = self.fixture(Path(raw))
                cache_path = config.state_root / "plugin-runtime-deps"
                if entry_type == "file":
                    cache_path.write_text(
                        "cache-like regular file is required\n",
                        encoding="utf-8",
                    )
                else:
                    target = Path(raw) / "outside/plugin-runtime-deps"
                    target.mkdir(parents=True)
                    (target / "outside.txt").write_text(
                        "must not be followed\n",
                        encoding="utf-8",
                    )
                    cache_path.symlink_to(target, target_is_directory=True)

                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=datetime(
                        2026,
                        7,
                        25,
                        18,
                        13 if entry_type == "file" else 14,
                        tzinfo=timezone.utc,
                    ),
                )

                self.assertEqual(code, 0, result)
                generation = Path(result["destination"])
                manifest = json.loads(
                    (generation / "MANIFEST.json").read_text(
                        encoding="utf-8"
                    )
                )
                state_record = next(
                    row
                    for row in manifest["archives"]
                    if row["name"] == "openclaw-state.tgz"
                )
                self.assertEqual(
                    state_record["source"][
                        "excluded_reconstructible_runtime_cache_subtrees"
                    ],
                    0,
                )
                self.assertEqual(
                    state_record["source"][
                        "excluded_reconstructible_runtime_cache_paths"
                    ],
                    [],
                )
                with tarfile.open(
                    generation / "openclaw-state.tgz",
                    "r:gz",
                ) as archive:
                    member = archive.getmember(
                        ".openclaw/plugin-runtime-deps"
                    )
                    names = {row.name for row in archive.getmembers()}
                self.assertEqual(member.isfile(), entry_type == "file")
                self.assertEqual(member.issym(), entry_type == "symlink")
                self.assertNotIn(
                    ".openclaw/plugin-runtime-deps/outside.txt",
                    names,
                )

    def test_verifier_rejects_injected_plugin_runtime_cache_descendant(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-plugin-runtime-cache-injected-"
        ) as raw:
            root = Path(raw)
            config, identity = self.fixture(root)
            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 7, 25, 18, 15, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 0, result)
            generation = Path(result["destination"])
            manifest = json.loads(
                (generation / "MANIFEST.json").read_text(encoding="utf-8")
            )
            expected = dict(
                next(
                    row
                    for row in manifest["archives"]
                    if row["name"] == "openclaw-state.tgz"
                )
            )
            state_spec = next(
                spec
                for spec in archive_backup.build_archive_specs(
                    config,
                    identity,
                )
                if spec.role == "state"
            )
            original = generation / "openclaw-state.tgz"
            injected = root / "openclaw-state-injected.tgz"
            payload = root / "injected-runtime-cache.js"
            payload.write_bytes(b"injected derived cache payload\n")
            injected_name = (
                ".openclaw/plugin-runtime-deps/"
                "injected/node_modules/example/index.js"
            )
            with tarfile.open(original, mode="r:gz") as source, tarfile.open(
                injected,
                mode="w:gz",
                format=tarfile.PAX_FORMAT,
            ) as destination:
                for member in source:
                    extracted = source.extractfile(member) if member.isfile() else None
                    try:
                        destination.addfile(member, extracted)
                    finally:
                        if extracted is not None:
                            extracted.close()
                injected_info = tarfile.TarInfo(injected_name)
                injected_info.mode = 0o600
                injected_info.mtime = 0
                injected_info.size = payload.stat().st_size
                with payload.open("rb") as handle:
                    destination.addfile(injected_info, handle)
            os.chmod(injected, 0o600)

            member_digest = hashlib.sha256()
            with tarfile.open(injected, mode="r:gz") as archive:
                for member in archive:
                    member_digest.update(
                        archive_backup.canonical_json(
                            archive_backup.member_contract_record(member)
                        )
                        + b"\n"
                    )
            expected["logical_bytes"] = injected.stat().st_size
            expected["sha256"] = archive_backup.sha256_file(injected)
            expected["member_contract_sha256"] = member_digest.hexdigest()

            with self.assertRaisesRegex(
                archive_backup.BackupError,
                "reconstructible runtime cache descendant",
            ):
                archive_backup.verify_archive(
                    injected,
                    state_spec,
                    expected,
                    probe_root=root / "injected-probe",
                    expected_device=identity.device,
                    config=config,
                )

    def test_exact_update_wrapper_operational_logs_are_excluded_before_read(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-update-wrapper-logs-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            logs = config.state_root / "logs"
            logs.mkdir()
            legacy_unreadable = (
                logs / "update-wrapper-20260402T175004Z.log"
            )
            second_transcript = (
                logs / "update-wrapper-20260405T020903Z.log"
            )
            legacy_unreadable.write_text(
                "legacy root-owned update transcript\n",
                encoding="utf-8",
            )
            second_transcript.write_text(
                "second update transcript\n",
                encoding="utf-8",
            )
            required_gateway_log = logs / "gateway.log"
            required_gateway_log.write_text(
                "required operational state\n",
                encoding="utf-8",
            )
            near_match = logs / "update-wrapper-current.log"
            near_match.write_text(
                "near match remains required\n",
                encoding="utf-8",
            )
            link_target = logs / "update-wrapper-link-target.txt"
            link_target.write_text("link target\n", encoding="utf-8")
            matching_symlink = (
                logs / "update-wrapper-20260725T220000Z.log"
            )
            matching_symlink.symlink_to(link_target.name)
            outside_logs = config.state_root / "recovery"
            outside_logs.mkdir()
            outside_match = (
                outside_logs / "update-wrapper-20260725T220001Z.log"
            )
            outside_match.write_text(
                "same basename shape outside logs is required\n",
                encoding="utf-8",
            )
            original_open = archive_backup.os.open
            unreadable_open_attempts = 0

            def deny_legacy_transcript_open(path, flags, *args, **kwargs):
                nonlocal unreadable_open_attempts
                if (
                    path == legacy_unreadable.name
                    and kwargs.get("dir_fd") is not None
                    and not flags & getattr(os, "O_DIRECTORY", 0)
                ):
                    unreadable_open_attempts += 1
                    raise PermissionError(13, "Permission denied", path)
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                archive_backup.os,
                "open",
                side_effect=deny_legacy_transcript_open,
            ):
                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=datetime(
                        2026,
                        7,
                        25,
                        22,
                        0,
                        tzinfo=timezone.utc,
                    ),
                )

            self.assertEqual(code, 0, result)
            self.assertEqual(unreadable_open_attempts, 0)
            generation = Path(result["destination"])
            manifest = json.loads(
                (generation / "MANIFEST.json").read_text(encoding="utf-8")
            )
            state_record = next(
                row
                for row in manifest["archives"]
                if row["name"] == "openclaw-state.tgz"
            )
            self.assertEqual(
                state_record["source"][
                    "excluded_update_wrapper_operational_logs"
                ],
                2,
            )
            expected_exclusions = sorted(
                [
                    ".openclaw/logs/{}".format(legacy_unreadable.name),
                    ".openclaw/logs/{}".format(second_transcript.name),
                ]
            )
            self.assertEqual(
                state_record["source"][
                    "excluded_update_wrapper_operational_log_paths_sha256"
                ],
                archive_backup.sha256_bytes(
                    archive_backup.canonical_json(expected_exclusions)
                ),
            )
            self.assertEqual(
                manifest["source_contract"][
                    "update_wrapper_operational_log_policy"
                ],
                (
                    "exclude-physical-same-device-regular-files-only-at-"
                    ".openclaw/logs/update-wrapper-<UTC-basic>.log"
                ),
            )
            with tarfile.open(
                generation / "openclaw-state.tgz",
                "r:gz",
            ) as archive:
                members = {member.name: member for member in archive}
            self.assertNotIn(
                ".openclaw/logs/{}".format(legacy_unreadable.name),
                members,
            )
            self.assertNotIn(
                ".openclaw/logs/{}".format(second_transcript.name),
                members,
            )
            self.assertIn(".openclaw/logs/gateway.log", members)
            self.assertIn(
                ".openclaw/logs/update-wrapper-current.log",
                members,
            )
            self.assertIn(
                (
                    ".openclaw/recovery/"
                    "update-wrapper-20260725T220001Z.log"
                ),
                members,
            )
            self.assertTrue(
                members[
                    (
                        ".openclaw/logs/"
                        "update-wrapper-20260725T220000Z.log"
                    )
                ].issym()
            )

    def test_unreadable_required_member_outside_exact_log_policy_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-unreadable-required-state-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            required = config.state_root / "recovery-state.json"
            required.write_text('{"required":true}\n', encoding="utf-8")
            original_open = archive_backup.os.open
            denied = False

            def deny_required_member_open(path, flags, *args, **kwargs):
                nonlocal denied
                if (
                    path == required.name
                    and kwargs.get("dir_fd") is not None
                    and not flags & getattr(os, "O_DIRECTORY", 0)
                ):
                    denied = True
                    raise PermissionError(13, "Permission denied", path)
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                archive_backup.os,
                "open",
                side_effect=deny_required_member_open,
            ):
                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=datetime(
                        2026,
                        7,
                        25,
                        22,
                        1,
                        tzinfo=timezone.utc,
                    ),
                )

            self.assertTrue(denied)
            self.assertEqual(code, 1)
            self.assertIn("Permission denied", result["blockers"][0])
            self.assertFalse(Path(result["destination"]).exists())

    def test_nonvolatile_member_disappearance_and_identity_change_fail_closed(
        self,
    ) -> None:
        with self.subTest("disappeared-after-enumeration"):
            with tempfile.TemporaryDirectory(
                prefix="openclaw-nonvolatile-disappeared-"
            ) as raw:
                config, identity = self.fixture(Path(raw))
                required = config.state_root / "must-retain.json"
                required.write_text('{"required":true}\n', encoding="utf-8")
                original_stat = archive_backup.os.stat

                def disappear_after_enumeration(path, *args, **kwargs):
                    if (
                        path == required.name
                        and kwargs.get("dir_fd") is not None
                    ):
                        required.unlink(missing_ok=True)
                        raise FileNotFoundError(path)
                    return original_stat(path, *args, **kwargs)

                with mock.patch.object(
                    archive_backup.os,
                    "stat",
                    side_effect=disappear_after_enumeration,
                ):
                    code, result = create_v3_fixture(
                        config,
                        identity_reader=self.identity_reader(identity),
                        now=datetime(
                            2026,
                            7,
                            25,
                            18,
                            11,
                            tzinfo=timezone.utc,
                        ),
                    )

                self.assertEqual(code, 1)
                self.assertIn(
                    "cannot stat required source member",
                    result["blockers"][0],
                )
                self.assertFalse(Path(result["destination"]).exists())

        with self.subTest("identity-changed-before-open"):
            with tempfile.TemporaryDirectory(
                prefix="openclaw-nonvolatile-replaced-"
            ) as raw:
                config, identity = self.fixture(Path(raw))
                required = config.state_root / "must-retain.json"
                required.write_text('{"required":true}\n', encoding="utf-8")
                moved = config.state_root / "must-retain.original"
                original_open = archive_backup.os.open
                swapped = False

                def replace_before_open(path, flags, *args, **kwargs):
                    nonlocal swapped
                    if (
                        not swapped
                        and path == required.name
                        and kwargs.get("dir_fd") is not None
                        and not flags & getattr(os, "O_DIRECTORY", 0)
                    ):
                        required.rename(moved)
                        required.write_text(
                            '{"replacement":true}\n',
                            encoding="utf-8",
                        )
                        swapped = True
                    return original_open(path, flags, *args, **kwargs)

                with mock.patch.object(
                    archive_backup.os,
                    "open",
                    side_effect=replace_before_open,
                ):
                    code, result = create_v3_fixture(
                        config,
                        identity_reader=self.identity_reader(identity),
                        now=datetime(
                            2026,
                            7,
                            25,
                            18,
                            12,
                            tzinfo=timezone.utc,
                        ),
                    )

                self.assertTrue(swapped)
                self.assertEqual(code, 1)
                self.assertIn(
                    "source file identity changed before read",
                    result["blockers"][0],
                )
                self.assertFalse(Path(result["destination"]).exists())

    def test_wrong_managed_target_fails_without_publication(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-three-archive-target-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            wrong = Path(raw) / "wrong-browser"
            wrong.mkdir()
            config.browser_logical.unlink()
            config.browser_logical.symlink_to(
                wrong,
                target_is_directory=True,
            )
            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 7, 25, 18, 2, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 1)
            self.assertEqual(result["result"], "blocked")
            self.assertIn(
                "managed symlink target mismatch",
                result["blockers"][0],
            )
            self.assertFalse(Path(result["destination"]).exists())

    def test_directory_swap_cannot_escape_anchored_traversal(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="v3-race-",
            dir=tempfile.gettempdir(),
        ) as raw:
            root = Path(raw)
            config, identity = self.fixture(root)
            victim = config.state_root / "victim"
            victim.mkdir()
            (victim / "inside.txt").write_text(
                "inside\n",
                encoding="utf-8",
            )
            outside = root / "outside-race"
            outside.mkdir()
            (outside / "outside-secret.txt").write_text(
                "outside\n",
                encoding="utf-8",
            )
            original_open = archive_backup.os.open
            swapped = False
            victim_directory_opens = 0

            def open_then_swap(path, flags, *args, **kwargs):
                nonlocal swapped, victim_directory_opens
                descriptor = original_open(
                    path,
                    flags,
                    *args,
                    **kwargs,
                )
                if (
                    path == "victim"
                    and kwargs.get("dir_fd") is not None
                    and flags & getattr(os, "O_DIRECTORY", 0)
                ):
                    victim_directory_opens += 1
                    if not swapped and victim_directory_opens == 2:
                        moved = config.state_root / "victim-opened"
                        victim.rename(moved)
                        victim.symlink_to(outside, target_is_directory=True)
                        swapped = True
                return descriptor

            with mock.patch.object(
                archive_backup.os,
                "open",
                side_effect=open_then_swap,
            ):
                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=datetime(
                        2026,
                        7,
                        25,
                        18,
                        8,
                        tzinfo=timezone.utc,
                    ),
                )

            self.assertTrue(swapped)
            self.assertEqual(code, 0, result)
            with tarfile.open(
                Path(result["destination"]) / "openclaw-state.tgz",
                "r:gz",
            ) as archive:
                names = {member.name for member in archive}
            self.assertIn(".openclaw/victim/inside.txt", names)
            self.assertNotIn(
                ".openclaw/victim/outside-secret.txt",
                names,
            )

    def test_shared_lock_blocks_concurrent_producer(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-three-archive-lock-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            archive_backup.validate_environment(
                config,
                self.identity_reader(identity),
                create_backup_root=True,
            )
            run_at = datetime(
                2026,
                7,
                25,
                18,
                3,
                tzinfo=timezone.utc,
            )
            run_id = run_at.strftime("%Y%m%dT%H%M%SZ")
            existing_receipt = archive_backup.PhaseReceipt(
                config,
                run_id,
                config.backup_root / "openclaw-archive-v3-{}".format(
                    run_id
                ),
            )
            existing_receipt.phase("winner", "running")
            existing_payload = existing_receipt.path.read_bytes()
            lock_path = config.backup_root.parent / ".weekly-backup.lock"
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=run_at,
                )
            finally:
                os.close(descriptor)
            self.assertEqual(code, 1)
            self.assertIn(
                "weekly backup lock is already held",
                result["blockers"][0],
            )
            self.assertFalse(Path(result["destination"]).exists())
            self.assertNotEqual(
                Path(result["receipt"]),
                existing_receipt.path,
            )
            self.assertEqual(
                existing_receipt.path.read_bytes(),
                existing_payload,
            )

    def test_headroom_gate_and_unsupported_type_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-three-archive-headroom-"
        ) as raw:
            root = Path(raw)
            config, identity = self.fixture(
                root,
                min_post_backup_free_bytes=10**30,
            )
            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 7, 25, 18, 4, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 1)
            self.assertIn(
                "insufficient OWC headroom",
                result["blockers"][0],
            )
            self.assertFalse(Path(result["destination"]).exists())

    def test_restore_budget_is_dynamic_and_git_alternates_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-three-archive-restore-budget-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            snapshots = archive_backup.create_sqlite_snapshot_repository(
                config,
                config.owc_root / "restore-budget-snapshots",
                expected_device=identity.device,
            )
            specs = archive_backup.build_archive_specs(
                config,
                identity,
                snapshots,
            )
            estimate = archive_backup.estimate_archives(specs)
            components = estimate["restore_budget_components"]
            self.assertGreater(
                components["full_restore_payload_upper_bytes"],
                0,
            )
            self.assertGreater(
                components["probe_payload_upper_bytes"],
                0,
            )
            self.assertEqual(
                estimate["restore_probe_budget_bytes"],
                components["full_restore_payload_upper_bytes"]
                + components["probe_payload_upper_bytes"]
                + components[
                    "sqlite_snapshot_selective_restore_bytes"
                ]
                + components[
                    "sqlite_snapshot_verification_transient_bytes"
                ]
                + components["metadata_upper_bytes"],
            )
            self.assertEqual(
                components["sqlite_snapshot_selective_restore_bytes"],
                archive_backup.sqlite_snapshot_payload_bytes(snapshots),
            )
            self.assertEqual(
                components[
                    "sqlite_snapshot_verification_transient_bytes"
                ],
                archive_backup.sqlite_snapshot_verification_transient_bytes(
                    snapshots
                ),
            )

            restored = Path(raw) / "isolated-restore"
            repository = restored / "git-remotes/alternate.git"
            subprocess.run(
                ["/usr/bin/git", "init", "--bare", str(repository)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            alternates = repository / "objects/info/alternates"
            alternates.write_text(
                "/tmp/not-an-isolated-object-store\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                archive_backup.BackupError,
                "depends on external object alternates",
            ):
                archive_backup.verify_restored_bare_repositories(
                    restored,
                    config=config,
                )

            symlinked = Path(raw) / "symlinked-restore"
            symlinked_repository = (
                symlinked / "git-remotes/symlinked-objects.git"
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "init",
                    "--bare",
                    str(symlinked_repository),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            external_objects = Path(raw) / "external-git-objects"
            (symlinked_repository / "objects").rename(external_objects)
            (symlinked_repository / "objects").symlink_to(
                external_objects,
                target_is_directory=True,
            )
            with self.assertRaisesRegex(
                archive_backup.BackupError,
                "contains a symlink and is not isolated",
            ):
                archive_backup.verify_restored_bare_repositories(
                    symlinked,
                    config=config,
                )

            nested_symlinked = Path(raw) / "nested-symlinked-restore"
            nested_repository = (
                nested_symlinked / "git-remotes/nested-symlink.git"
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "init",
                    "--bare",
                    str(nested_repository),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            nested_external = Path(raw) / "nested-external-object"
            nested_external.write_text("not isolated\n", encoding="utf-8")
            (nested_repository / "objects/info/external-link").symlink_to(
                nested_external,
            )
            with self.assertRaisesRegex(
                archive_backup.BackupError,
                "contains a symlink and is not isolated",
            ):
                archive_backup.verify_restored_bare_repositories(
                    nested_symlinked,
                    config=config,
                )

    def test_post_archive_budget_uses_actual_records_after_source_growth(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-three-archive-growth-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            original_create = archive_backup.create_archive
            injected = False

            def create_with_growth(*args, **kwargs):
                nonlocal injected
                if not injected:
                    injected = True
                    (config.workspace_physical / "runbook/growth.bin").write_bytes(
                        b"x" * (1024 * 1024)
                    )
                return original_create(*args, **kwargs)

            with mock.patch.object(
                archive_backup,
                "create_archive",
                side_effect=create_with_growth,
            ):
                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=datetime(
                        2026,
                        7,
                        25,
                        18,
                        7,
                        tzinfo=timezone.utc,
                    ),
                )

            self.assertEqual(code, 0, result)
            manifest = json.loads(
                (Path(result["destination"]) / "MANIFEST.json").read_text(
                    encoding="utf-8"
                )
            )
            estimated = manifest["preflight"]["size_estimate"][
                "restore_budget_components"
            ]["full_restore_payload_upper_bytes"]
            actual = manifest["preflight"]["headroom"][
                "actual_restore_verification_budget"
            ]["full_restore_payload_bytes"]
            self.assertGreaterEqual(actual - estimated, 1024 * 1024)

        with tempfile.TemporaryDirectory(
            prefix="openclaw-three-archive-special-"
        ) as raw:
            root = Path(raw)
            config, identity = self.fixture(root)
            os.mkfifo(config.state_root / "unsupported.fifo")
            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 7, 25, 18, 5, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 1)
            self.assertIn(
                "unsupported required source entry type",
                result["blockers"][0],
            )
            self.assertFalse(Path(result["destination"]).exists())

    def test_published_readback_detects_archive_corruption(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-three-archive-corrupt-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 7, 25, 18, 6, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 0, result)
            generation = Path(result["destination"])
            with (generation / "git-remotes.tgz").open("ab") as handle:
                handle.write(b"corruption")
            with self.assertRaisesRegex(
                archive_backup.BackupError,
                "published archive readback mismatch",
            ):
                archive_backup.verify_published_generation(
                    generation,
                    config=config,
                )

    def test_published_readback_rejects_empty_or_duplicate_proof_rows(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-three-archive-manifest-proof-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(
                    2026,
                    7,
                    25,
                    18,
                    9,
                    tzinfo=timezone.utc,
                ),
            )
            self.assertEqual(code, 0, result)
            generation = Path(result["destination"])
            manifest_path = generation / "MANIFEST.json"
            original = json.loads(manifest_path.read_text(encoding="utf-8"))

            def write_manifest(payload):
                payload = dict(payload)
                payload.pop("manifest_payload_sha256", None)
                payload["manifest_payload_sha256"] = (
                    archive_backup.sha256_bytes(
                        archive_backup.canonical_json(payload)
                    )
                )
                manifest_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                os.chmod(manifest_path, 0o600)

            empty = dict(original)
            empty["verification"] = []
            write_manifest(empty)
            with self.assertRaisesRegex(
                archive_backup.BackupError,
                "verification records mismatch",
            ):
                archive_backup.verify_published_generation(
                    generation,
                    config=config,
                )

            duplicate = dict(original)
            duplicate["archives"] = [
                original["archives"][0],
                original["archives"][0],
                original["archives"][2],
            ]
            write_manifest(duplicate)
            with self.assertRaisesRegex(
                archive_backup.BackupError,
                "archive records mismatch",
            ):
                archive_backup.verify_published_generation(
                    generation,
                    config=config,
                )

    def test_failed_restore_gate_never_publishes_partial_generation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-three-archive-atomic-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            (config.media_physical / "item.txt").unlink()
            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 7, 25, 18, 7, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 1)
            self.assertIn(
                "no bounded restore candidate for: media",
                result["blockers"][0],
            )
            self.assertFalse(Path(result["destination"]).exists())
            staging = Path(result["staging"])
            self.assertTrue(staging.is_dir())
            self.assertTrue(
                staging.name.startswith(".openclaw-archive-v3-")
            )
            self.assertRegex(staging.name, r"\.incomplete-\d+$")
            second_code, second = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(
                    2026,
                    7,
                    25,
                    18,
                    8,
                    tzinfo=timezone.utc,
                ),
            )
            self.assertEqual(second_code, 1)
            self.assertIn(
                "prior incomplete staging requires classification",
                second["blockers"][0],
            )
            self.assertTrue(staging.is_dir())
            self.assertFalse(Path(second["destination"]).exists())
            self.assertEqual(
                list(
                    config.backup_root.glob(
                        ".openclaw-archive-v3-*.incomplete-*"
                    )
                ),
                [staging],
            )

    def test_restore_probe_cleanup_uses_parent_anchored_removal_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="openclaw-three-archive-anchored-cleanup-"
        ) as raw:
            config, identity = self.fixture(Path(raw))
            original_rmdir = archive_backup.os.rmdir
            rmdir_calls: list[tuple[object, object]] = []

            def require_anchored_rmdir(path, *args, **kwargs):
                rmdir_calls.append((path, kwargs.get("dir_fd")))
                self.assertIsNotNone(kwargs.get("dir_fd"))
                return original_rmdir(path, *args, **kwargs)

            with mock.patch.object(
                archive_backup.shutil,
                "rmtree",
                side_effect=AssertionError(
                    "path-based rmtree must not be used"
                ),
            ), mock.patch.object(
                archive_backup.os,
                "rmdir",
                side_effect=require_anchored_rmdir,
            ):
                code, result = create_v3_fixture(
                    config,
                    identity_reader=self.identity_reader(identity),
                    now=datetime(
                        2026,
                        7,
                        25,
                        18,
                        9,
                        tzinfo=timezone.utc,
                    ),
                )

            self.assertEqual(code, 0, result)
            self.assertTrue(rmdir_calls)
            self.assertTrue(
                all(descriptor is not None for _path, descriptor in rmdir_calls)
            )
            self.assertFalse(
                (
                    Path(result["destination"]) / ".restore-probe"
                ).exists()
            )

    def test_independent_published_readback_detects_post_publish_corruption(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-three-archive-readback-") as raw:
            config, identity = self.fixture(Path(raw))
            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 7, 25, 18, 10, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 0, result)
            generation = Path(result["destination"])

            intact = archive_backup.independent_published_effect_readback(generation)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    archive_backup.effect.emit(
                        intact,
                        what="publish bounded three-archive OWC weekly recovery set",
                    ),
                    0,
                )

            archive = generation / archive_backup.EXPECTED_ARCHIVE_NAMES[0]
            payload = archive.read_bytes()
            archive.write_bytes(payload[: max(1, len(payload) // 2)])
            corrupted = archive_backup.independent_published_effect_readback(generation)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    archive_backup.effect.emit(
                        corrupted,
                        what="publish bounded three-archive OWC weekly recovery set",
                    ),
                    4,
                )
            predicate = next(
                item
                for item in corrupted
                if item["name"] == "published_archive:openclaw-state.tgz"
            )
            self.assertFalse(predicate["satisfied"])

    def test_independent_published_readback_rejects_generation_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openclaw-three-archive-readback-link-") as raw:
            config, identity = self.fixture(Path(raw))
            code, result = create_v3_fixture(
                config,
                identity_reader=self.identity_reader(identity),
                now=datetime(2026, 7, 25, 18, 11, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 0, result)
            alias = Path(raw) / "generation-link"
            alias.symlink_to(Path(result["destination"]), target_is_directory=True)

            predicates = archive_backup.independent_published_effect_readback(alias)

            published = next(
                item for item in predicates if item["name"] == "published_generation"
            )
            self.assertFalse(published["satisfied"])
            self.assertEqual(published["reason"], "object_absent")


if __name__ == "__main__":
    unittest.main()
