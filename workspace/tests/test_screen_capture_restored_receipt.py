from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from test_openclaw_runtime_activate import (
    FakeBackend, StoppedRestoreBackend, activate_module as activation,
    fixture, restore, restore_result_path, run,
)
from test_screen_capture_continuity import capture_binding
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import journal_screen_capture_acceptance as probe


def rewrite(path, value):
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    path.chmod(0o400)


@pytest.fixture
def restored_capture(fixture, capture_binding, monkeypatch):
    _, _, database, source, _ = capture_binding
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == "snapshot_restore_required"
    assert restore(fixture, StoppedRestoreBackend(fixture))["outcome"] == "restored"
    monkeypatch.setattr(activation, "process_identity", lambda pid: ("darwin:2:0", fixture.paths.node, 1))
    evidence = activation.screen_capture_runtime_evidence(fixture.paths.result, fixture.paths.node)
    proof = json.loads(source.read_text())
    proof.update(evidence, responsiblePid=100,
                 responsibleProcessIdentity=["darwin:2:0", str(fixture.paths.node), "1"])
    proof["tccdAttributionAndEffectivePermission"][0]["message"] = proof["tccdAttributionAndEffectivePermission"][0]["message"].replace("pid=123,", "pid=100,")
    source.write_text(json.dumps(proof))
    output = fixture.root / "restored-capture-binding.json"
    activation.enroll_screen_capture_binding(
        fixture.paths, source, hashlib.sha256(source.read_bytes()).hexdigest(), database, output)
    return output, source, evidence


@pytest.mark.parametrize("retired", [False, True])
def test_exact_restored_capture_survives_only_its_own_retirement(fixture, restored_capture, retired):
    binding, _, evidence = restored_capture
    original = {p.name: p.read_bytes() for p in (
        fixture.paths.result, fixture.paths.result.with_name(activation.START_CONSUMED_NAME),
        restore_result_path(fixture))}
    parent = fixture.paths.result.parent
    if retired:
        parent = parent / "archive" / "restored-capture"
        receipt = activation.retire_terminal_receipts(fixture.paths, parent)
        assert receipt["outcome"] == "restored"
        assert receipt["restoreResult"]["sha256"] == evidence["restoreReceiptSha256"]
    result = activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)
    assert result["effectiveCaptureVerifiedForCurrentProcess"] is True
    assert result["scheduledSyncProven"] is False
    assert all((parent / name).read_bytes() == content for name, content in original.items())
    if retired:
        assert not fixture.paths.result.exists()


@pytest.mark.parametrize("mutation", [
    "stopped", "boot_failed", "not_applied", "failed_hash", "snapshot", "rollback",
    "gateway_health", "bootstrap_selector", "missing_node", "missing_restore", "restore_mode",
])
def test_fresh_capture_cannot_promote_incomplete_or_unbound_restore(fixture, restored_capture, mutation):
    path = restore_result_path(fixture)
    value = json.loads(path.read_text())
    if mutation == "stopped":
        value["outcome"] = "restored_stopped"
    elif mutation == "boot_failed":
        value["outcome"] = "restored_boot_failed"
    elif mutation == "not_applied":
        value["restoreApplied"] = False
    elif mutation == "failed_hash":
        value["failedActivationResultSha256"] = "0" * 64
    elif mutation == "snapshot":
        value["snapshot"]["manifestSha256"] = "0" * 64
    elif mutation == "rollback":
        value["rollback"]["commit"] = "0" * 40
    elif mutation == "gateway_health":
        value["verification"]["gateway"]["health"]["readyz"]["accepted"] = False
    elif mutation == "bootstrap_selector":
        value["bootstrapBindings"][activation.OPERATOR.require_string("runtime.gateway_label")]["selectorInode"] += 1
    elif mutation == "missing_node":
        value["verification"]["node"] = None
    if mutation == "missing_restore":
        path.unlink()
    else:
        rewrite(path, value)
    if mutation == "restore_mode":
        path.chmod(0o600)
    with pytest.raises(activation.ActivationError):
        activation.screen_capture_runtime_evidence(fixture.paths.result, fixture.paths.node)


@pytest.mark.parametrize("retired", [False, True])
def test_restored_gateway_rollover_and_wrong_responsible_pid_are_rejected(
    fixture, restored_capture, monkeypatch, retired,
):
    binding, _, _ = restored_capture
    if retired:
        activation.retire_terminal_receipts(fixture.paths, fixture.paths.result.parent / "archive" / "restored-capture")
    with pytest.raises(activation.ActivationError):
        activation.screen_capture_runtime_evidence(fixture.paths.result, fixture.paths.node, responsible_pid=999)
    monkeypatch.setattr(activation, "process_identity", lambda pid: ("darwin:3:0", fixture.paths.node, 1))
    with pytest.raises(activation.ActivationError):
        activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)


def test_failed_activation_hash_alone_cannot_enroll_restored_capture(fixture, restored_capture):
    _, _, evidence = restored_capture
    with pytest.raises(activation.ActivationError, match="lifecycle changed"):
        activation.screen_capture_runtime_evidence(
            fixture.paths.result, fixture.paths.node,
            expected={"activationReceiptSha256": evidence["activationReceiptSha256"]}, responsible_pid=100)


def test_restored_capture_rejects_new_live_generation_and_inflight_lifecycle(fixture, restored_capture):
    binding, _, _ = restored_capture
    descriptor = os.open(fixture.paths.lock, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(activation.ActivationError):
            activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)
    finally:
        os.close(descriptor)
    activation.retire_terminal_receipts(fixture.paths, fixture.paths.result.parent / "archive" / "restored-capture")
    fixture.paths.result.write_text(json.dumps({"outcome": "activated", "generation": "later"}))
    with pytest.raises(activation.ActivationError):
        activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)


def test_accept_uses_the_same_bound_restore_owner(fixture, restored_capture, monkeypatch):
    _, source, evidence = restored_capture
    monkeypatch.setattr(probe, "activation", activation)
    monkeypatch.setattr(activation, "live_paths", lambda: fixture.paths)
    root = fixture.root / "pending-restored-capture"
    root.mkdir(mode=0o700)
    image = root / "journal.png"
    image.write_bytes(b"visually reviewed private Journal fixture")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    proof = json.loads(source.read_text())
    proof.update(status="awaiting_visual_acceptance", imageSha256=digest)
    probe.save(root / "capture.json", proof)
    accepted = probe.accept(root, digest)
    assert accepted["scheduledSyncProven"] is False
    accepted_proof = json.loads((root / "acceptance.json").read_text())
    assert accepted_proof["restoreReceiptSha256"] == evidence["restoreReceiptSha256"]
    assert accepted_proof["activationReceiptSha256"] == evidence["activationReceiptSha256"]


def test_capture_checks_restore_owner_before_scheduling(fixture, restored_capture, monkeypatch):
    monkeypatch.setattr(probe, "activation", activation)
    monkeypatch.setattr(activation, "live_paths", lambda: fixture.paths)
    path = restore_result_path(fixture)
    value = json.loads(path.read_text())
    value["outcome"] = "restored_stopped"
    rewrite(path, value)
    monkeypatch.setattr(probe, "cli", lambda *a, **k: pytest.fail("unproven restore reached scheduler"))
    with pytest.raises(activation.ActivationError):
        probe.capture(fixture.root / "rejected-capture")
