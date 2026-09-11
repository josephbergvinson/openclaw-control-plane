"""Contained native-CLI contract fixtures; not proof of a live native backup."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from unittest import mock

from scripts import openclaw_weekly_archive_backup as backup
from scripts import openclaw_archive_v3_retention as retention
from openclaw_archive_v3_test_fixture import create_v3_fixture
import test_openclaw_weekly_archive_backup as legacy_tests


class NativeProvider:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.restored_values = {}

    def __call__(self, cli, config, arguments, **options):
        self.calls.append(arguments)
        if arguments == ("agents", "list", "--json"):
            return [{"id": "main"}]
        command = arguments[1]
        if self.fail == command:
            raise backup.BackupError("native fixture {} failed".format(command))
        scratch = options["scratch_root"]
        assert scratch.is_dir() and scratch.stat().st_mode & 0o777 == 0o700
        assert scratch.is_relative_to(config.backup_root.parent)
        if command == "create":
            assert "--no-include-workspace" in arguments
            assert "--verify" in arguments
            output = Path(arguments[arguments.index("--output") + 1])
            content = scratch / "content"
            content.mkdir()
            state = content / "native/payload/state"
            state.mkdir(parents=True)
            shutil.copyfile(config.state_root / "openclaw.json", state / "openclaw.json")
            sources = [config.state_root / "state/openclaw.sqlite"]
            sources += sorted(config.agents_physical.glob("*/agent/openclaw-agent.sqlite"))
            for source in sources:
                relative = source.relative_to(config.state_root)
                target = state / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect("file:{}?mode=ro".format(source), uri=True)) as src:
                    with closing(sqlite3.connect(target)) as dest:
                        src.backup(dest)
                target.chmod(0o600)
            (content / "native/manifest.json").write_text(json.dumps({
                "schemaVersion": 1,
                "assets": [{"kind": "state", "sourcePath": str(config.state_root),
                            "archivePath": "native/payload/state"}],
            }))
            with tarfile.open(output, "w:gz") as archive:
                archive.add(content / "native", arcname="native")
            output.chmod(0o600)
            return {"archivePath": str(output), "archiveRoot": "native", "verified": True,
                    "dryRun": False, "includeWorkspace": False, "onlyConfig": False,
                    "assets": [{"kind": "state", "sourcePath": str(config.state_root.resolve())}],
                    "skipped": [], "skippedVolatileCount": 0}
        archive_path = Path(arguments[2])
        if command == "verify":
            with tarfile.open(archive_path) as archive:
                assert archive.getmember("native/manifest.json")
            return {"ok": True, "archivePath": str(archive_path), "archiveRoot": "native"}
        if command == "restore":
            target = Path(arguments[arguments.index("--target") + 1])
            assert not target.exists(), "native restore must receive a fresh target"
            target.mkdir()
            with tarfile.open(archive_path) as archive:
                # Only this fixture's generated tree reaches this test provider.
                archive.extractall(target)
            for db in target.glob("native/payload/state/agents/*/agent/openclaw-agent.sqlite"):
                with closing(sqlite3.connect(db)) as conn:
                    if db.parts[-3] == "retained":
                        self.restored_values["retained"] = conn.execute("SELECT value FROM markers").fetchall()
            return {"ok": True, "archivePath": str(archive_path), "archiveRoot": "native",
                    "targetPath": str(target)}
        raise AssertionError(arguments)


class NativeWeeklyBackupTests(unittest.TestCase):
    def fixture(self, root):
        config, identity = legacy_tests.OpenClawWeeklyArchiveBackupTests().fixture(root)
        physical = config.owc_root / ".state/OpenClaw"
        (physical / "state").mkdir(exist_ok=True)
        shutil.copyfile(config.state_root / "openclaw.json", physical / "openclaw.json")
        shutil.copyfile(config.state_root / "state/openclaw.sqlite", physical / "state/openclaw.sqlite")
        (physical / "cron").mkdir()
        shutil.copyfile(config.state_root / "cron/jobs.json", physical / "cron/jobs.json")
        for logical, target in (("browser", "Browser"), ("media", "Media")):
            if not (physical / logical).exists():
                (physical / logical).symlink_to(physical / target, target_is_directory=True)
        return replace(config, state_root=physical), identity

    def run_backup(self, config, identity, provider):
        with mock.patch.object(backup, "run_openclaw_json", side_effect=provider):
            return backup.create_backup(config, identity_reader=lambda _: identity,
                                        now=datetime(2026, 9, 8, 12, tzinfo=timezone.utc))

    def test_retained_agent_wal_uses_native_create_and_fresh_restore(self):
        with tempfile.TemporaryDirectory() as raw:
            config, identity = self.fixture(Path(raw))
            source = config.agents_physical / "retained/agent/openclaw-agent.sqlite"
            source.parent.mkdir(parents=True)
            with closing(sqlite3.connect(source)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE markers(value TEXT)")
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute("INSERT INTO markers VALUES ('committed-in-wal')")
                writer.commit()
                self.assertGreater(Path(str(source) + "-wal").stat().st_size, 0)
                provider = NativeProvider()
                code, result = self.run_backup(config, identity, provider)
            self.assertEqual(code, 0, result)
            self.assertEqual(provider.restored_values["retained"], [("committed-in-wal",)])
            self.assertEqual([args[1] for args in provider.calls], ["create", "verify", "restore"])
            manifest = backup.load_manifest(Path(result["destination"]))
            self.assertEqual(manifest["schema_version"], "openclaw.owc_weekly_backup.manifest.v4")
            self.assertFalse(manifest["retention"]["performed"])
            self.assertNotIn("sqlite_backup", manifest)
            self.assertTrue(source.exists())

    def test_native_failure_never_publishes_or_deletes_source(self):
        for command in ("create", "verify", "restore"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as raw:
                config, identity = self.fixture(Path(raw))
                source = config.state_root / "state/openclaw.sqlite"
                before = source.read_bytes()
                provider = NativeProvider()
                provider.fail = command
                code, result = self.run_backup(config, identity, provider)
                self.assertEqual(code, 1)
                self.assertIn("native fixture {} failed".format(command), str(result["blockers"]))
                self.assertFalse(Path(result["destination"]).exists())
                self.assertEqual(source.read_bytes(), before)
                self.assertFalse(result["retention_performed"])

    def test_legacy_and_native_retention_are_sorted_by_capture_time_not_version(self):
        with tempfile.TemporaryDirectory() as raw:
            config, identity = self.fixture(Path(raw))
            provider = NativeProvider()
            code, native = self.run_backup(config, identity, provider)
            self.assertEqual(code, 0, native)
            code, legacy = create_v3_fixture(
                config, identity_reader=lambda _: identity,
                now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 0, legacy)
            settings = retention.RetentionConfig(
                producer_config=replace(config, expected_device=identity.device),
                receipt_dir=config.receipt_dir / "retention",
            )
            inventory = retention.scan_and_verify(settings)
            self.assertEqual([path.name for path in inventory.v3_generations],
                             [legacy["backup"], native["backup"]])
            with mock.patch.object(backup, "run_openclaw_json", side_effect=provider):
                proof = retention.deep_verify_all(
                    inventory.v3_generations, config=settings,
                    receipt=retention.RetentionReceipt(settings, apply=False),
                )
            self.assertEqual(len(proof), 2)
            self.assertTrue(all(row["result"] == "verified" for row in proof))
            self.assertTrue(Path(native["destination"]).is_dir())
            self.assertTrue(Path(legacy["destination"]).is_dir())

    def test_source_database_added_during_capture_prevents_publication(self):
        with tempfile.TemporaryDirectory() as raw:
            config, identity = self.fixture(Path(raw))
            provider = NativeProvider()
            def changed(*args, **kwargs):
                result = provider(*args, **kwargs)
                if args[2][1] == "restore":
                    target = config.agents_physical / "late/agent/openclaw-agent.sqlite"
                    target.parent.mkdir(parents=True)
                    with closing(sqlite3.connect(target)) as database:
                        database.execute("CREATE TABLE late(value TEXT)")
                return result
            code, result = self.run_backup(config, identity, changed)
            self.assertEqual(code, 1)
            self.assertIn("inventory changed", str(result["blockers"]))
            self.assertFalse(Path(result["destination"]).exists())

    def test_sidecar_without_database_and_hardlink_fail_before_native(self):
        for invalid in ("sidecar", "hardlink", "symlink"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as raw:
                config, identity = self.fixture(Path(raw))
                target = config.agents_physical / "bad/agent/openclaw-agent.sqlite"
                target.parent.mkdir(parents=True)
                if invalid == "sidecar":
                    Path(str(target) + "-wal").write_bytes(b"unowned-sidecar")
                elif invalid == "hardlink":
                    os.link(config.agents_physical / "main/agent/openclaw-agent.sqlite", target)
                else:
                    target.symlink_to(config.agents_physical / "main/agent/openclaw-agent.sqlite")
                provider = NativeProvider()
                code, result = self.run_backup(config, identity, provider)
                self.assertEqual(code, 1)
                self.assertEqual(provider.calls, [])
                self.assertFalse(Path(result["destination"]).exists())

    def test_invalid_configuration_cannot_be_mislabeled_complete(self):
        with tempfile.TemporaryDirectory() as raw:
            config, identity = self.fixture(Path(raw))
            provider = NativeProvider()
            def unresolved(*args, **kwargs):
                result = provider(*args, **kwargs)
                if args[2][1] == "create":
                    result["skipped"] = [{"kind": "agent", "reason": "unresolved"}]
                return result
            code, result = self.run_backup(config, identity, unresolved)
            self.assertEqual(code, 1)
            self.assertFalse(Path(result["destination"]).exists())

    def test_legacy_state_root_cannot_omit_retained_databases(self):
        with tempfile.TemporaryDirectory() as raw:
            config, identity = legacy_tests.OpenClawWeeklyArchiveBackupTests().fixture(Path(raw))
            provider = NativeProvider()
            code, result = self.run_backup(config, identity, provider)
            self.assertEqual(code, 1)
            self.assertIn("native state root must own the canonical agents directory", str(result["blockers"]))
            self.assertEqual(provider.calls, [])

    def test_native_manifest_rejects_mixed_payload_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            config, identity = self.fixture(Path(raw))
            code, result = self.run_backup(config, identity, NativeProvider())
            self.assertEqual(code, 0, result)
            generation = Path(result["destination"])
            manifest = backup.load_manifest(generation)
            manifest["payload_contract"] = backup.PAYLOAD_CONTRACT
            manifest.pop("manifest_payload_sha256")
            manifest["manifest_payload_sha256"] = backup.sha256_bytes(backup.canonical_json(manifest))
            backup.atomic_write_json(generation / "MANIFEST.json", manifest)
            with self.assertRaisesRegex(backup.BackupError, "native generation backup contract mismatch"):
                backup.verify_published_generation(generation, config=config)

    def test_lock_and_insufficient_space_prevent_native_invocation(self):
        for failure in ("lock", "headroom"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                config, identity = self.fixture(Path(raw))
                provider = NativeProvider()
                config.backup_root.mkdir(parents=True)
                lock = config.backup_root.parent / ".weekly-backup.lock"
                with lock.open("w") as handle:
                    if failure == "lock":
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        config = replace(config, min_post_backup_free_bytes=10**30)
                    code, result = self.run_backup(config, identity, provider)
                self.assertEqual(code, 1)
                self.assertEqual(provider.calls, [])
                self.assertFalse(Path(result["destination"]).exists())

    def test_restore_result_must_bind_fresh_target(self):
        with tempfile.TemporaryDirectory() as raw:
            config, identity = self.fixture(Path(raw))
            provider = NativeProvider()
            def wrong_target(*args, **kwargs):
                result = provider(*args, **kwargs)
                if args[2][1] == "restore":
                    result["targetPath"] = str(config.state_root)
                return result
            code, result = self.run_backup(config, identity, wrong_target)
            self.assertEqual(code, 1)
            self.assertIn("restore result mismatch", str(result["blockers"]))
            self.assertFalse(Path(result["destination"]).exists())

    def test_source_database_replaced_during_capture_prevents_publication(self):
        with tempfile.TemporaryDirectory() as raw:
            config, identity = self.fixture(Path(raw))
            provider = NativeProvider()
            source = config.state_root / "state/openclaw.sqlite"
            def replaced(*args, **kwargs):
                result = provider(*args, **kwargs)
                if args[2][1] == "restore":
                    original = source.with_name("original.sqlite")
                    source.rename(original)
                    shutil.copyfile(original, source)
                return result
            code, result = self.run_backup(config, identity, replaced)
            self.assertEqual(code, 1)
            self.assertIn("inventory changed", str(result["blockers"]))
            self.assertFalse(Path(result["destination"]).exists())

    def test_normal_wal_activity_does_not_require_an_atomic_source_tree(self):
        with tempfile.TemporaryDirectory() as raw:
            config, identity = self.fixture(Path(raw))
            provider = NativeProvider()
            with closing(sqlite3.connect(config.state_root / "state/openclaw.sqlite")) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE baseline_write(value TEXT)")
                writer.commit()
                def source_write(*args, **kwargs):
                    result = provider(*args, **kwargs)
                    if args[2][1] == "create":
                        writer.execute("CREATE TABLE later_write(value TEXT)")
                        writer.commit()
                    return result
                code, result = self.run_backup(config, identity, source_write)
            self.assertEqual(code, 0, result)
            manifest = backup.load_manifest(Path(result["destination"]))
            self.assertFalse(manifest["source_tree_atomic"])

    def test_cli_transport_binds_private_scratch_and_does_not_retry_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            config, _identity = self.fixture(Path(raw))
            config.backup_root.mkdir(parents=True)
            scratch = config.backup_root / ".fixture-scratch"
            scratch.mkdir(mode=0o700)
            cli = Path(raw) / "fixture-cli"
            cli.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "scratch = pathlib.Path(os.environ['TMPDIR'])\n"
                "with (scratch / 'calls').open('a') as f: f.write('call\\n')\n"
                "if sys.argv[1] == 'fail':\n"
                "    print('private fixture detail', file=sys.stderr)\n"
                "    sys.exit(7)\n"
                "print(json.dumps({'state': os.environ['OPENCLAW_STATE_DIR'], 'scratch': str(scratch)}))\n"
            )
            cli.chmod(0o700)
            payload = backup.run_openclaw_json(cli, config, ("pass",), label="fixture",
                                               timeout_seconds=10, scratch_root=scratch)
            self.assertEqual(payload, {"state": str(config.state_root), "scratch": str(scratch)})
            with self.assertRaisesRegex(backup.BackupError, "fixture failed with exit 7") as caught:
                backup.run_openclaw_json(cli, config, ("fail",), label="fixture",
                                         timeout_seconds=10, scratch_root=scratch)
            self.assertNotIn("private fixture detail", str(caught.exception))
            self.assertEqual((scratch / "calls").read_text(), "call\ncall\n")
