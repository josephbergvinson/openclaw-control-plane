"""Whole-generation archive/restore uses synthetic stores, never live homes."""
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile

import pytest

from test_openclaw_runtime_activate import (
    CANDIDATE_COMMIT, FakeBackend, StoppedRestoreBackend, activate_module,
    fixture, restore, run,
)


def prepare_recovery(fixture):
    external_agent = fixture.root / "external agent"
    native_home = fixture.root / "native home"
    sqlite_home = fixture.root / "separate sqlite home"
    companion = fixture.root / "Companion.app"
    include = fixture.root / "included-config.json"
    for root in (external_agent, native_home, sqlite_home, companion):
        root.mkdir()
        (root / "preserved.txt").write_text(root.name)
    (native_home / "sessions").mkdir()
    (native_home / "sessions" / "rollout.jsonl").write_text('{"type":"accepted history"}\n')
    include.write_text('{"account":"fixture@example.invalid"}\n')
    # Seven independently stored database families. These are deliberately
    # synthetic SQL fixtures: this proves complete host restoration, not native
    # Codex migration or wire compatibility, which have separate qualification.
    families = ("state", "logs", "goals", "memories", "memories-v2", "queue", "thread-history")
    for family in families:
        with sqlite3.connect(sqlite_home / f"{family}.sqlite") as db:
            db.execute("CREATE TABLE original(value TEXT)")
            db.execute("INSERT INTO original VALUES (?)", (family,))
    fixture.paths = replace(fixture.paths, recovery_roots=(
        external_agent, native_home, sqlite_home, include, companion,
        native_home / "sessions", fixture.paths.predecessor_state_dir / "agents",
    ))
    output = fixture.root / "whole-generation"
    fixture.snapshot = output / "stopped-snapshot.json"
    receipt = activate_module.create_stopped_snapshot(
        fixture.paths, output, fixture.snapshot, fixture.candidate,
        CANDIDATE_COMMIT, fixture.seal, FakeBackend(fixture),
    )
    return receipt, sqlite_home, native_home, include, families


def test_archive_and_restore_cover_external_agents_native_seven_stores_and_companion(fixture):
    receipt, sqlite_home, native_home, include, families = prepare_recovery(fixture)
    assert receipt["schemaVersion"] == 3
    assert receipt["inventory"]["count"] == 9
    assert len(receipt["recoveryRoots"]) == 5
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == "snapshot_restore_required"
    for family in families:
        with sqlite3.connect(sqlite_home / f"{family}.sqlite") as db:
            db.execute("ALTER TABLE original RENAME TO upgraded")
    (sqlite_home / "newly-created-after-migration.sqlite").write_bytes(b"upgraded state")
    (native_home / "sessions" / "rollout.jsonl").write_text("post-upgrade history\n")
    include.write_text("post-upgrade configuration\n")
    outcome = restore(fixture, StoppedRestoreBackend(fixture))
    assert outcome["outcome"] == "restored"
    assert len(outcome["quarantinedFailedSurfaces"]) == 9
    for family in families:
        with sqlite3.connect(sqlite_home / f"{family}.sqlite") as db:
            assert db.execute("SELECT value FROM original").fetchone() == (family,)
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert not (sqlite_home / "newly-created-after-migration.sqlite").exists()
    assert "accepted history" in (native_home / "sessions" / "rollout.jsonl").read_text()
    assert json.loads(include.read_text())["account"] == "fixture@example.invalid"
    quarantine = sqlite_home.with_name(f"{sqlite_home.name}.failed-{CANDIDATE_COMMIT[:12]}")
    assert (quarantine / "newly-created-after-migration.sqlite").is_file()


def test_recovery_root_rebinding_cannot_reinterpret_an_existing_generation(fixture):
    prepare_recovery(fixture)
    fixture.paths = replace(fixture.paths, recovery_roots=())
    backend = FakeBackend(fixture)
    assert run(fixture, backend)["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0


def test_recovery_roots_cannot_encompass_activation_control_or_escape_through_symlink(fixture):
    for root in (Path("/"), fixture.root, fixture.paths.releases_root,
                 fixture.paths.releases_root / "nested-snapshot-content"):
        paths = replace(fixture.paths, recovery_roots=(root,))
        with pytest.raises(activate_module.ActivationError):
            activate_module.normalized_recovery_roots(paths)
    link = fixture.root / "linked-native-home"
    link.symlink_to(fixture.root / "external-runtime-assets", target_is_directory=True)
    with pytest.raises(activate_module.ActivationError, match="physical"):
        activate_module.recovery_root_inventory(replace(fixture.paths, recovery_roots=(link,)))


def test_open_file_observation_does_not_admit_snapshot_when_native_writer_is_open(fixture, monkeypatch):
    prepare_recovery(fixture)
    def fake_run(argv, _timeout):
        if argv[0] == "/bin/launchctl":
            label = argv[-1].split("/")[-1]
            payload = ("Bad request.\n" f'Could not find service "{label}" in domain for user gui: '
                       f"{fixture.paths.operator_uid}\n").encode()
            return activate_module.CommandResult(argv, 113, b"", payload, 1, False)
        if "-iTCP:18789" in argv:
            return activate_module.CommandResult(argv, 1, b"", b"", 1, False)
        return activate_module.CommandResult(argv, 0, b"p123\nnative-writer.sqlite\n", b"", 1, False)
    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    with pytest.raises(activate_module.ActivationError, match="not quiescent"):
        backend.assert_gateway_and_node_stopped()
    assert backend.command_evidence[-1]["purpose"].startswith("external_recovery_root_")


def declare_browser_recovery(fixture):
    owned = activate_module.BrowserRecoveryRoot
    fixture.paths = replace(fixture.paths, recovery_roots=(
        owned("browser-native-launchers"),
        owned("browser-native-manifest", "chrome"),
        owned("browser-native-manifest", "chromium"),
        owned("browser-store-request", "chrome"),
    ))
    targets = {spec.owner + (":" + spec.product if spec.product else ""):
               activate_module.browser_recovery_target(spec)[0]
               for spec in fixture.paths.recovery_roots}
    return targets


def write_browser_pair(fixture, targets, product="chrome", generation="original", *,
                       manifest=None, browser_profile=None):
    manifest = manifest if manifest is not None else targets[f"browser-native-manifest:{product}"]
    directory = targets["browser-native-launchers"]
    manifest.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(parents=True, exist_ok=True)
    quote = lambda value: "'" + str(value).replace("'", "'\"'\"'") + "'"
    suffix = hashlib.sha256(str(manifest).encode()).hexdigest()[:16]
    base = directory / f"{activate_module.BROWSER_HOST_NAME}.{suffix}.sh"
    origin = f"chrome-extension://{activate_module.BROWSER_STORE_ID}/"
    command = [f"/fixture/{generation}/node", f"/fixture/{generation}/native-host-entry.js",
               "--manifest", str(manifest), "--launcher", str(base), "--expected-origin", origin]
    if browser_profile is not None:
        command.extend(("--browser-profile", browser_profile))
    canonical = ("#!/bin/sh\n" + activate_module.BROWSER_LAUNCHER_MARKER + "\n" +
                 f"export OPENCLAW_STATE_DIR={quote(fixture.paths.predecessor_state_dir)}\n" +
                 f"export OPENCLAW_CONFIG_PATH={quote(fixture.paths.predecessor_state_dir / 'openclaw.json')}\n" +
                 "exec " + " ".join(quote(item) for item in command) + ' "$@"\n')
    launcher = base.with_name(base.stem + "." + hashlib.sha256(canonical.encode()).hexdigest() + ".sh")
    content = canonical.replace(f" '--launcher' {quote(base)}", f" '--launcher' {quote(launcher)}")
    launcher.write_text(content)
    launcher.chmod(0o700)
    manifest.write_text(json.dumps({"name": activate_module.BROWSER_HOST_NAME,
                                    "description": "OpenClaw browser extension bootstrap",
                                    "path": str(launcher), "type": "stdio", "allowed_origins": [origin]}) + "\n")
    manifest.chmod(0o600)
    return launcher


def write_store_request(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"external_update_url": "https://clients2.google.com/service/update2/crx",
                                 "openclawOwnership": "browser-store-install-v1"}) + "\n")
    target.chmod(0o600)


def snapshot_browser(fixture):
    output = fixture.root / "browser-generation"
    fixture.snapshot = output / "stopped-snapshot.json"
    return activate_module.create_stopped_snapshot(
        fixture.paths, output, fixture.snapshot, fixture.candidate,
        CANDIDATE_COMMIT, fixture.seal, FakeBackend(fixture),
    )


@pytest.mark.parametrize("baseline", ["mixed", "absent", "still-absent", "currently-missing"])
def test_owned_browser_snapshot_restores_exact_presence_without_touching_other_registrations(fixture, baseline):
    targets = declare_browser_recovery(fixture)
    manifest = targets["browser-native-manifest:chrome"]
    manifest.parent.mkdir(parents=True)
    unrelated = manifest.with_name("another.application.json")
    unrelated.write_bytes(b"unrelated host registration\n")
    original_launcher = write_browser_pair(fixture, targets) if baseline in ("mixed", "currently-missing") else None
    original = manifest.read_bytes() if original_launcher else None
    receipt = snapshot_browser(fixture)
    assert receipt["schemaVersion"] == 4
    assert receipt["inventory"]["count"] == 8
    rows = receipt["recoveryRoots"]
    assert sum(row["present"] for row in rows) == (2 if original_launcher else 0)
    with tarfile.open(receipt["tarPath"]) as archive:
        archived = {member.name for member in archive}
    for row in rows:
        assert (row["path"].lstrip("/") in archived) == row["present"]
    assert str(unrelated).lstrip("/") not in archived
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == "snapshot_restore_required"
    if baseline == "currently-missing":
        manifest.unlink()
        shutil.rmtree(targets["browser-native-launchers"])
    elif baseline != "still-absent":
        if original_launcher:
            original_launcher.unlink()
        write_browser_pair(fixture, targets, generation="candidate")
        write_browser_pair(fixture, targets, product="chromium", generation="candidate")
        write_store_request(targets["browser-store-request:chrome"])
    outcome = restore(fixture, StoppedRestoreBackend(fixture))
    assert outcome["outcome"] == "restored"
    assert unrelated.read_bytes() == b"unrelated host registration\n"
    assert not targets["browser-store-request:chrome"].exists()
    assert not targets["browser-native-manifest:chromium"].exists()
    if original_launcher:
        assert manifest.read_bytes() == original
        assert list(targets["browser-native-launchers"].iterdir()) == [original_launcher]
    else:
        assert not os.path.lexists(manifest)
        assert not os.path.lexists(targets["browser-native-launchers"])
    assert all(str(unrelated) != item for item in outcome["quarantinedFailedSurfaces"])
    assert len(outcome["quarantinedFailedSurfaces"]) == (4 if baseline in ("still-absent", "currently-missing") else 8)


@pytest.mark.parametrize("declaration", [
    {"owner": "browser-native-manifest", "product": "arbitrary"},
    {"owner": "browser-native-manifest", "product": "chrome", "path": "/not/an/owned/leaf"},
    {"owner": "browser-native-launchers", "product": "chrome"},
    {"owner": "browser-store-request", "product": "chromium"},
    {"owner": "arbitrary-absent", "path": "/not/an/owned/leaf"},
])
def test_absent_browser_declarations_are_fixed_owned_surfaces_not_path_strings(fixture, declaration):
    with pytest.raises(activate_module.ActivationError):
        activate_module.parse_recovery_root(declaration)


@pytest.mark.parametrize("escape", ["parent-symlink", "unsafe-parent-mode", "dangling-leaf", "hardlink",
                                    "foreign", "foreign-launcher", "wrong-content-hash", "other-state-launcher"])
def test_owned_browser_inventory_refuses_symlinks_hardlinks_and_foreign_content(fixture, escape):
    targets = declare_browser_recovery(fixture)
    target = targets["browser-store-request:chrome"]
    if escape == "parent-symlink":
        outside = fixture.root / "other-application"
        outside.mkdir()
        target.parent.parent.mkdir(parents=True)
        target.parent.symlink_to(outside, target_is_directory=True)
    elif escape == "unsafe-parent-mode":
        target.parent.mkdir(parents=True)
        target.parent.chmod(0o777)
    elif escape == "dangling-leaf":
        target.parent.mkdir(parents=True)
        target.symlink_to(fixture.root / "not-present")
    elif escape in ("foreign-launcher", "wrong-content-hash", "other-state-launcher"):
        launcher = write_browser_pair(fixture, targets)
        if escape == "foreign-launcher":
            launcher.write_text("#!/bin/sh\n# unowned launcher\n")
        elif escape == "wrong-content-hash":
            launcher.write_text(launcher.read_text().replace("/fixture/original/node", "/fixture/changed/node"))
        else:
            launcher.write_text(launcher.read_text().replace(str(fixture.paths.predecessor_state_dir), "/fixture/another-state"))
    else:
        write_store_request(target)
        if escape == "hardlink":
            os.link(target, fixture.root / "other-owner-hardlink")
        else:
            target.write_text('{"external_update_url":"https://example.invalid/foreign"}')
    with pytest.raises(activate_module.ActivationError):
        activate_module.recovery_root_inventory(fixture.paths)


def test_owned_browser_absence_cannot_be_swallowed_by_broader_recovery_directory(fixture):
    targets = declare_browser_recovery(fixture)
    for extra in (targets["browser-native-manifest:chrome"].parent, fixture.paths.current_link):
        paths = replace(fixture.paths, recovery_roots=(*fixture.paths.recovery_roots, extra))
        with pytest.raises(activate_module.ActivationError):
            activate_module.normalized_recovery_roots(paths)


def test_browser_snapshot_parent_incarnation_and_typed_binding_cannot_be_reinterpreted(fixture):
    targets = declare_browser_recovery(fixture)
    target = targets["browser-store-request:chrome"]
    target.parent.mkdir(parents=True)
    receipt = snapshot_browser(fixture)
    rows = receipt["recoveryRoots"]
    changed = json.loads(json.dumps(rows))
    changed[0]["present"] = "false"
    with pytest.raises(activate_module.ActivationError):
        activate_module.validate_recovery_inventory(fixture.paths, changed, 4)
    with pytest.raises(activate_module.ActivationError):
        activate_module.validate_recovery_inventory(fixture.paths, rows, 3)
    target.parent.rename(target.parent.with_name("retired-external-extensions"))
    target.parent.mkdir()
    with pytest.raises(activate_module.ActivationError, match="ancestor binding drift"):
        activate_module.validate_recovery_inventory(fixture.paths, rows, 4)


def test_foreign_current_registration_refuses_before_any_restore_rename(fixture):
    targets = declare_browser_recovery(fixture)
    snapshot_browser(fixture)
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == "snapshot_restore_required"
    write_store_request(targets["browser-store-request:chrome"])
    target = targets["browser-store-request:chrome"]
    target.write_bytes(b"foreign current request\n")
    state_identity = fixture.paths.predecessor_state_dir.stat().st_ino
    with pytest.raises(activate_module.ActivationError):
        restore(fixture, StoppedRestoreBackend(fixture))
    assert target.read_bytes() == b"foreign current request\n"
    assert fixture.paths.predecessor_state_dir.stat().st_ino == state_identity
    assert not fixture.paths.predecessor_state_dir.with_name(
        fixture.paths.predecessor_state_dir.name + ".failed-" + CANDIDATE_COMMIT[:12]).exists()


@pytest.mark.parametrize("member_suffix", ["", "/injected", "/._injected"])
def test_absent_owned_root_has_no_lawful_archive_member(fixture, member_suffix):
    targets = declare_browser_recovery(fixture)
    receipt = snapshot_browser(fixture)
    altered = fixture.root / "injected-browser.tar"
    with tarfile.open(receipt["tarPath"]) as original, tarfile.open(altered, "w") as archive:
        for member in original:
            archive.addfile(member, original.extractfile(member) if member.isfile() else None)
        member = tarfile.TarInfo(str(targets["browser-native-launchers"]).lstrip("/") + member_suffix)
        member.mode = 0o600
        member.uid = fixture.paths.operator_uid
        member.size = 4
        archive.addfile(member, io.BytesIO(b"evil"))
    with pytest.raises(activate_module.ActivationError):
        activate_module.validate_snapshot_archive_members(
            fixture.paths, altered, str(fixture.predecessor), receipt["recoveryRoots"],
        )


def test_failed_extract_restores_candidate_present_and_absent_browser_preimage(fixture, monkeypatch):
    targets = declare_browser_recovery(fixture)
    original_launcher = write_browser_pair(fixture, targets)
    snapshot_browser(fixture)
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == "snapshot_restore_required"
    original_launcher.unlink()
    candidate_launcher = write_browser_pair(fixture, targets, generation="candidate")
    candidate_content = candidate_launcher.read_bytes()
    manifest = targets["browser-native-manifest:chrome"]
    candidate_manifest = manifest.read_bytes()
    write_store_request(targets["browser-store-request:chrome"])
    real_run = activate_module.run_bounded
    def fail_after_extract(argv, timeout, **kwargs):
        result = real_run(argv, timeout, **kwargs)
        if argv[0] == "/usr/bin/tar" and "-xpf" in argv:
            return activate_module.CommandResult(argv, 1, result.stdout, b"fixture extract failure", 1, False)
        return result
    monkeypatch.setattr(activate_module, "run_bounded", fail_after_extract)
    outcome = restore(fixture, StoppedRestoreBackend(fixture))
    assert outcome["outcome"] == "restore_failed_preimage_restored"
    assert outcome["restoreApplied"] is False
    assert outcome["preimageRestored"] is True
    assert manifest.read_bytes() == candidate_manifest
    assert candidate_launcher.read_bytes() == candidate_content
    assert targets["browser-store-request:chrome"].exists()
    assert not targets["browser-native-manifest:chromium"].exists()


@pytest.mark.parametrize("failed_surface", ["state", "browser-launchers"])
def test_successful_quarantine_rename_is_compensated_when_its_fsync_fails(fixture, monkeypatch, failed_surface):
    targets = declare_browser_recovery(fixture)
    # An all-absent baseline may be followed by a partial install: preserve the
    # current candidate's owned pair, while two other typed leaves stay absent.
    snapshot_browser(fixture)
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == "snapshot_restore_required"
    launcher = write_browser_pair(fixture, targets, generation="candidate")
    manifest = targets["browser-native-manifest:chrome"]
    manifest_bytes = manifest.read_bytes()
    launcher_bytes = launcher.read_bytes()
    identities = {path: path.lstat().st_ino for path in (
        fixture.paths.predecessor_state_dir, targets["browser-native-launchers"], manifest, launcher,
    )}
    target = (fixture.paths.predecessor_state_dir if failed_surface == "state"
              else targets["browser-native-launchers"])
    quarantine = target.with_name(target.name + ".failed-" + CANDIDATE_COMMIT[:12])
    original_fsync = activate_module.fsync_directory
    failures = []
    def fail_only_after_successful_rename(directory):
        if not failures and Path(directory) == target.parent and quarantine.exists() and not target.exists():
            failures.append(target)
            raise OSError("fixture durability failure after successful rename")
        return original_fsync(directory)
    monkeypatch.setattr(activate_module, "fsync_directory", fail_only_after_successful_rename)
    backend = StoppedRestoreBackend(fixture)
    outcome = restore(fixture, backend)
    assert failures == [target]
    assert outcome["outcome"] == "restore_failed_preimage_restored"
    assert outcome["restoreApplied"] is False
    assert outcome["preimageRestored"] is True
    assert {path: path.lstat().st_ino for path in identities} == identities
    assert manifest.read_bytes() == manifest_bytes
    assert launcher.read_bytes() == launcher_bytes
    assert not quarantine.exists()
    assert not os.path.lexists(targets["browser-native-manifest:chromium"])
    assert not os.path.lexists(targets["browser-store-request:chrome"])
    assert all(item["purpose"] != "stopped_snapshot_restore" for item in backend.command_evidence)


@pytest.mark.parametrize("profile,browser_profile", [("openclaw", None), ("a" * 64, "a" * 64)])
def test_managed_profile_manifest_is_covered_by_full_state_and_restored_with_launcher(
        fixture, profile, browser_profile):
    owned = activate_module.BrowserRecoveryRoot
    fixture.paths = replace(fixture.paths, recovery_roots=(owned("browser-native-launchers"),))
    directory = activate_module.browser_recovery_target(owned("browser-native-launchers"))[0]
    targets = {"browser-native-launchers": directory}
    manifest = (fixture.paths.predecessor_state_dir / "browser" / profile / "user-data" /
                "NativeMessagingHosts" / f"{activate_module.BROWSER_HOST_NAME}.json")
    launcher = write_browser_pair(fixture, targets, manifest=manifest, browser_profile=browser_profile)
    manifest_before, launcher_before = manifest.read_bytes(), launcher.read_bytes()
    receipt = snapshot_browser(fixture)
    assert [row["path"] for row in receipt["recoveryRoots"]] == [str(directory)]
    assert str(manifest) not in {row["path"] for row in receipt["recoveryRoots"]}
    with tarfile.open(receipt["tarPath"]) as archive:
        member = archive.getmember(str(manifest).lstrip("/"))
        assert member.isfile() and member.uid == fixture.paths.operator_uid and member.mode == 0o600
        assert archive.extractfile(member).read() == manifest_before
        assert archive.extractfile(str(launcher).lstrip("/")).read() == launcher_before
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == "snapshot_restore_required"
    launcher.unlink()
    candidate_launcher = write_browser_pair(
        fixture, targets, generation="candidate", manifest=manifest, browser_profile=browser_profile)
    assert manifest.read_bytes() != manifest_before
    outcome = restore(fixture, StoppedRestoreBackend(fixture))
    assert outcome["outcome"] == "restored"
    assert manifest.read_bytes() == manifest_before
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert launcher.read_bytes() == launcher_before
    assert launcher.stat().st_mode & 0o777 == 0o700
    assert not candidate_launcher.exists()


@pytest.mark.parametrize("failure", ["profile-mismatch", "invalid-profile", "wrong-leaf",
                                     "extra-depth", "other-state", "wrong-hash",
                                     "wrong-origin", "non-executable"])
def test_managed_profile_launcher_admission_keeps_exact_namespace_and_owned_grammar(fixture, failure):
    owned = activate_module.BrowserRecoveryRoot
    fixture.paths = replace(fixture.paths, recovery_roots=(owned("browser-native-launchers"),))
    targets = {"browser-native-launchers":
               activate_module.browser_recovery_target(owned("browser-native-launchers"))[0]}
    profile = "bad_profile" if failure == "invalid-profile" else "openclaw"
    state = (fixture.paths.predecessor_state_dir.with_name("other-state")
             if failure == "other-state" else fixture.paths.predecessor_state_dir)
    parent = state / "browser" / profile / "user-data" / "NativeMessagingHosts"
    if failure == "extra-depth":
        parent /= "nested"
    manifest = parent / ("foreign.json" if failure == "wrong-leaf"
                         else f"{activate_module.BROWSER_HOST_NAME}.json")
    launcher = write_browser_pair(
        fixture, targets, manifest=manifest,
        browser_profile="other" if failure == "profile-mismatch" else None)
    if failure == "wrong-hash":
        launcher.write_text(launcher.read_text().replace("/fixture/original/node", "/fixture/changed/node"))
    elif failure == "wrong-origin":
        launcher.write_text(launcher.read_text().replace("chrome-extension://", "foreign-extension://"))
    elif failure == "non-executable":
        launcher.chmod(0o600)
    with pytest.raises(activate_module.ActivationError):
        activate_module.recovery_root_inventory(fixture.paths)


@pytest.mark.parametrize("product", tuple(activate_module.BROWSER_PRODUCT_ROOTS))
@pytest.mark.parametrize("declared", [False, True])
def test_external_browser_manifests_still_require_each_exact_typed_declaration(fixture, product, declared):
    owned = activate_module.BrowserRecoveryRoot
    specs = (owned("browser-native-launchers"),)
    if declared:
        specs += (owned("browser-native-manifest", product),)
    fixture.paths = replace(fixture.paths, recovery_roots=specs)
    targets = {"browser-native-launchers":
               activate_module.browser_recovery_target(owned("browser-native-launchers"))[0]}
    manifest = activate_module.browser_recovery_target(owned("browser-native-manifest", product))[0]
    write_browser_pair(fixture, targets, manifest=manifest)
    if declared:
        rows = activate_module.recovery_root_inventory(fixture.paths)
        assert {row["path"] for row in rows} == {str(targets["browser-native-launchers"]), str(manifest)}
    else:
        with pytest.raises(activate_module.ActivationError, match="not inventoried"):
            activate_module.recovery_root_inventory(fixture.paths)
