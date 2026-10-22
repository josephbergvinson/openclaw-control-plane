"""Private projected-state restoration uses the existing physical owner.

All roots, stores and receipts are synthetic. No native model/database migration
or application/service activation runs, and full-generation acceptance is separate.
"""
from dataclasses import replace
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from test_openclaw_runtime_activate import CANDIDATE_COMMIT, FakeBackend, activate_module, fixture


class ScopedBackend(FakeBackend):
    def __init__(self, item, paths):
        super().__init__(item)
        self.paths = paths

    def assert_gateway_and_node_stopped(self):
        self.stopped_checks += 1
        self.command_evidence.extend(activate_module.expected_stopped_command_evidence(self.paths))


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    return activate_module._rehearsal_reference(path)


def prepare_projection(item, *, root_name="private projected state", candidate_commit=CANDIDATE_COMMIT):
    root = item.root / root_name
    root.mkdir(mode=0o700)
    native = root / "agent" / "native"
    native.mkdir(parents=True, mode=0o700)
    sqlite = root / "agent" / "sqlite"
    sqlite.mkdir(mode=0o700)
    # Byte fixtures, not SQL databases. The native initializer is a separate gate.
    families = ("state_5.sqlite", "logs_2.sqlite", "goals_1.sqlite", "memories_1.sqlite",
                "memories_v2_1.sqlite", "queue_1.sqlite", "thread_history_1.sqlite")
    for name in families:
        if name != "memories_v2_1.sqlite":
            (sqlite / name).write_bytes(("original " + name).encode())
    (sqlite / "state_5.sqlite-wal").write_bytes(b"preserved WAL")
    (sqlite / "state_5.sqlite-shm").write_bytes(b"preserved SHM")
    (root / "openclaw.json").write_bytes(b'{"synthetic":"copied config"}\n')
    (root / "owned-launcher-copy.sh").write_bytes(b"opaque original path bytes; never rebound\n")
    subprocess.run(["/usr/bin/xattr", "-w", "com.openclaw.projected-fixture", "preserved root xattr", str(root)], check=True, capture_output=True)
    original = activate_module._rehearsal_reference(item.snapshot)
    projection = write_json(item.root / "projection.json", {
        "schemaVersion": 1, "operation": "prepareUpdateCandidateRehearsal",
        "candidateSourceCommit": candidate_commit, "baselineSnapshot": original,
        "result": {"stateDir": str(root), "configPath": str(root / "openclaw.json")},
    })
    descriptor = write_json(item.root / "descriptor.json", {
        "schemaVersion": 1, "candidateSourceCommit": candidate_commit,
        "baselineSnapshot": original, "nativeProjectionReceipt": projection,
        "copiedStateRoot": str(root),
        "copyIdentity": activate_module._rehearsal_directory(root, item.paths.operator_uid),
        "expectedHomeIds": ["agent-fixture"], "homes": [{
            "id": "agent-fixture", "copiedCodexHome": str(native), "copiedSqliteHome": str(sqlite),
            "originalCodexHome": str(item.root / "original native"),
            "originalSqliteHome": str(item.root / "original sqlite"),
        }],
    })
    augmented = replace(item.paths, recovery_roots=(*item.paths.recovery_roots, root))
    backend = ScopedBackend(item, augmented)
    return root, sqlite, descriptor, backend


def capture(item, descriptor, backend):
    return activate_module.create_projected_rehearsal_snapshot(
        item.paths, item.root / "private recovery proof", item.candidate,
        CANDIDATE_COMMIT, item.seal, descriptor, backend,
    )


def restore_projection(item, captured, descriptor, backend):
    output = Path(captured["projectedRecoverySnapshot"]["path"]).parent / "projected-restore-result.json"
    return activate_module.restore_projected_rehearsal(
        item.paths, item.candidate, CANDIDATE_COMMIT, item.seal,
        captured["projectedRecoverySnapshot"], descriptor, output, backend,
    )


def test_private_rehearsal_restores_complete_projected_root_without_activation(fixture):
    root, sqlite, descriptor, backend = prepare_projection(fixture)
    captured = capture(fixture, descriptor, backend)
    validate_snapshot, validate_restore = activate_module.projected_rehearsal_validators(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal, backend)
    admitted = validate_snapshot(captured["projectedRecoverySnapshot"], descriptor)
    assert admitted["originalSourceBaseline"] == json.loads(Path(descriptor["path"]).read_text())["baselineSnapshot"]
    selected = json.loads(Path(captured["projectedRecoverySnapshot"]["path"]).read_text())
    with __import__("tarfile").open(selected["tarPath"]) as archive:
        assert any(member.name == str(root).lstrip("/") for member in archive)
    original = {path: (path.read_bytes(), path.stat().st_ino) for path in
                (fixture.paths.gateway_plist, fixture.paths.node_plist, fixture.paths.predecessor_state_dir / "openclaw.json")}
    (sqlite / "state_5.sqlite").write_bytes(b"synthetic new schema")
    (sqlite / "memories_v2_1.sqlite").write_bytes(b"must disappear")
    (sqlite / "state_5.sqlite-wal").unlink()
    (root / "new-output").mkdir()
    (root / "new-output" / "tail").write_bytes(b"post-migration tail")
    (root / "openclaw.json").write_bytes(b"new config")
    outcome = restore_projection(fixture, captured, descriptor, backend)
    assert outcome["outcome"] == "restored_stopped"
    assert outcome["restoreApplied"] is True
    assert outcome["preimageRestored"] is None  # Compensation is not success evidence.
    assert outcome["scope"] == "private-projected-state-only"
    assert (sqlite / "state_5.sqlite").read_bytes() == b"original state_5.sqlite"
    assert (sqlite / "state_5.sqlite-wal").read_bytes() == b"preserved WAL"
    assert not (sqlite / "memories_v2_1.sqlite").exists()
    assert not (root / "new-output").exists()
    assert (root / "openclaw.json").read_bytes() == b'{"synthetic":"copied config"}\n'
    assert subprocess.run(["/usr/bin/xattr", "-p", "com.openclaw.projected-fixture", str(root)], check=True, capture_output=True).stdout == b"preserved root xattr\n"
    assert (root / "owned-launcher-copy.sh").read_bytes() == b"opaque original path bytes; never rebound\n"
    for path, facts in original.items():
        assert (path.read_bytes(), path.stat().st_ino) == facts
    assert backend.gateway_boots == backend.node_boots == 0
    assert not activate_module._start_fence_path(fixture.paths).exists()
    receipt = activate_module._rehearsal_reference(Path(captured["projectedRecoverySnapshot"]["path"]).parent / "projected-restore-result.json")
    assert validate_restore(receipt, captured["projectedRecoverySnapshot"])["restoreApplied"] is True


@pytest.mark.parametrize("failure", ["extract", "rename-fsync", "post-extract-original-change"])
def test_private_restore_compensates_with_current_preimage_and_no_service_start(fixture, monkeypatch, failure):
    root, sqlite, descriptor, backend = prepare_projection(fixture)
    captured = capture(fixture, descriptor, backend)
    (sqlite / "state_5.sqlite").write_bytes(b"migrated current preimage")
    actual_run, actual_sync = activate_module.run_bounded, activate_module.fsync_directory
    extracted = False
    def run(argv, timeout, **options):
        nonlocal extracted
        if "-xpf" in argv:
            extracted = True
            if failure == "extract":
                return activate_module.CommandResult(argv, 1, b"", b"synthetic extract failure", 1, False)
            result = actual_run(argv, timeout, **options)
            if failure == "post-extract-original-change":
                (fixture.paths.predecessor_state_dir / "unchanged-external-package.marker").write_bytes(b"outside writer")
            return result
        return actual_run(argv, timeout, **options)
    failed = False
    def sync(path):
        nonlocal failed
        if failure == "rename-fsync" and path == root.parent and not failed:
            failed = True
            raise OSError("synthetic durability failure")
        return actual_sync(path)
    monkeypatch.setattr(activate_module, "run_bounded", run)
    monkeypatch.setattr(activate_module, "fsync_directory", sync)
    outcome = restore_projection(fixture, captured, descriptor, backend)
    assert outcome["restoreApplied"] is False
    assert outcome["preimageRestored"] is True
    assert outcome["outcome"] == "restore_failed_preimage_restored"
    assert (sqlite / "state_5.sqlite").read_bytes() == b"migrated current preimage"
    assert backend.gateway_boots == backend.node_boots == 0
    assert extracted == (failure != "rename-fsync")


@pytest.mark.parametrize("drift", ["descriptor", "source", "root-identity", "original"])
def test_private_restore_rejects_scope_drift_before_quarantine(fixture, monkeypatch, drift):
    root, _, descriptor, backend = prepare_projection(fixture)
    captured = capture(fixture, descriptor, backend)
    if drift == "descriptor":
        Path(descriptor["path"]).write_text('{}\n')
    elif drift == "source":
        actual = activate_module._rehearsal_reference
        monkeypatch.setattr(activate_module, "_rehearsal_reference",
                            lambda path: {"path": str(path), "sha256": "0" * 64}
                            if path == Path(activate_module.__file__).resolve() else actual(path))
    elif drift == "root-identity":
        root.rename(root.with_name("retained private root"))
        root.mkdir(mode=0o700)
    else:
        (fixture.paths.predecessor_state_dir / "unchanged-external-package.marker").write_bytes(b"outside writer")
    with pytest.raises(activate_module.ActivationError):
        restore_projection(fixture, captured, descriptor, backend)
    assert not root.with_name(root.name + ".failed-" + captured["projectedRecoverySnapshot"]["sha256"][:12]).exists()
    assert backend.gateway_boots == backend.node_boots == 0


def test_projected_snapshot_rejects_cross_root_hardlink_dependency(fixture):
    root, _, descriptor, backend = prepare_projection(fixture)
    os.link(fixture.paths.predecessor_state_dir / "unchanged-external-package.marker", root / "linked-original")
    with pytest.raises(activate_module.ActivationError, match="hardlink"):
        capture(fixture, descriptor, backend)


@pytest.mark.parametrize("name", ["private[*] root", "private? root", "private\\root"])
def test_projected_snapshot_rejects_tar_pattern_root_spelling(fixture, name):
    _, _, descriptor, backend = prepare_projection(fixture, root_name=name)
    with pytest.raises(activate_module.ActivationError, match="literally"):
        capture(fixture, descriptor, backend)


def test_projected_snapshot_rejects_unobserved_stopped_scope(fixture):
    _, _, descriptor, _ = prepare_projection(fixture)
    with pytest.raises(activate_module.ActivationError, match="command evidence"):
        capture(fixture, descriptor, FakeBackend(fixture))


def test_projected_capture_rejects_coherent_descriptor_and_projection_for_another_candidate(fixture):
    _, _, descriptor, backend = prepare_projection(fixture, candidate_commit="3" * 40)
    with pytest.raises(activate_module.ActivationError, match="sealed runtime candidate"):
        capture(fixture, descriptor, backend)
    assert not (fixture.root / "private recovery proof").exists()
    assert backend.gateway_boots == backend.node_boots == 0


def test_projected_loader_rejects_coherently_rebound_native_candidate_before_quarantine(fixture):
    root, sqlite, descriptor, backend = prepare_projection(fixture)
    captured = capture(fixture, descriptor, backend)
    native = json.loads(Path(descriptor["path"]).read_text())
    projection_path = Path(native["nativeProjectionReceipt"]["path"])
    projection = json.loads(projection_path.read_text())
    projection["candidateSourceCommit"] = native["candidateSourceCommit"] = "3" * 40
    native["nativeProjectionReceipt"] = write_json(projection_path, projection)
    descriptor = write_json(Path(descriptor["path"]), native)
    binding_path = Path(captured["binding"]["path"])
    binding = json.loads(binding_path.read_text())
    binding["nativeDescriptor"] = descriptor
    binding["nativeProjectionReceipt"] = native["nativeProjectionReceipt"]
    binding_path.chmod(0o600)
    write_json(binding_path, binding)
    binding_path.chmod(0o400)
    state = sqlite / "state_5.sqlite"
    before = state.read_bytes(), state.stat().st_ino
    validate_snapshot, _ = activate_module.projected_rehearsal_validators(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal, backend)
    with pytest.raises(activate_module.ActivationError, match="sealed runtime candidate"):
        validate_snapshot(captured["projectedRecoverySnapshot"], descriptor)
    with pytest.raises(activate_module.ActivationError, match="sealed runtime candidate"):
        restore_projection(fixture, captured, descriptor, backend)
    assert (state.read_bytes(), state.stat().st_ino) == before
    assert not root.with_name(root.name + ".failed-" + captured["projectedRecoverySnapshot"]["sha256"][:12]).exists()
    assert backend.gateway_boots == backend.node_boots == 0
