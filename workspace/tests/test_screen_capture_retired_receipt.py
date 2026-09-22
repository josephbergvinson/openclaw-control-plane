from __future__ import annotations

import hashlib
import json
import fcntl
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_openclaw_runtime_activate import activate_module as activation, fixture, run
from test_screen_capture_continuity import capture_binding


@pytest.fixture
def retired_capture(fixture, capture_binding):
    _, _, _, source, _ = capture_binding
    run(fixture)
    proof = json.loads(source.read_text())
    proof["activationReceiptSha256"] = hashlib.sha256(fixture.paths.result.read_bytes()).hexdigest()
    source.write_text(json.dumps(proof))
    output = fixture.root / "retirement-bound-capture.json"
    activation.enroll_screen_capture_binding(
        fixture.paths, source, hashlib.sha256(source.read_bytes()).hexdigest(),
        capture_binding[2], output,
    )
    activation.verify_screen_capture_binding(output, fixture.paths.node, require_current_process=True)
    archive = fixture.paths.result.parent / "archive" / "original-cleanup"
    retirement = activation.retire_terminal_receipts(fixture.paths, archive)
    assert not fixture.paths.result.exists()
    return output, archive, retirement


def test_original_retirement_preserves_current_capture_without_new_probe(fixture, retired_capture):
    binding, archive, _ = retired_capture
    before = {p: p.read_bytes() for p in [binding, *archive.iterdir()]}
    result = activation.verify_screen_capture_binding(
        binding, fixture.paths.node, require_current_process=True,
    )
    assert result["effectiveCaptureVerifiedForCurrentProcess"] is True
    assert result["scheduledSyncProven"] is False
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not fixture.paths.result.exists()


def test_retirement_does_not_rescue_changed_process(fixture, retired_capture, monkeypatch):
    binding, _, _ = retired_capture
    monkeypatch.setattr(activation, "process_identity", lambda _: ("darwin:21:2", fixture.paths.node, 1))
    with pytest.raises(activation.ActivationError):
        activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)


@pytest.mark.parametrize("mutation", ["hash", "inode", "path", "outcome", "timestamp", "schema", "mode", "symlink"])
def test_wrong_or_malformed_retirement_cannot_restore_capture(fixture, retired_capture, mutation):
    binding, archive, _ = retired_capture
    path = archive / activation.RETIREMENT_RECEIPT_NAME
    value = json.loads(path.read_text())
    if mutation in {"hash", "inode", "path"}:
        value["result"][{"hash": "sha256", "inode": "inode", "path": "path"}[mutation]] = {
            "hash": "0" * 64, "inode": 999, "path": str(archive / "other.json"),
        }[mutation]
    elif mutation == "outcome":
        value["outcome"] = "restored"
    elif mutation == "timestamp":
        value["retiredAt"] = "not-a-time"
    elif mutation == "schema":
        value["schemaVersion"] = True
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    if mutation != "mode":
        path.chmod(0o400)
    if mutation == "symlink":
        replacement = archive / "moved-receipt.json"
        path.rename(replacement)
        path.symlink_to(replacement)
    with pytest.raises(activation.ActivationError):
        activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)


def test_live_new_activation_takes_precedence_over_matching_archive(fixture, retired_capture):
    binding, _, _ = retired_capture
    fixture.paths.result.write_text(json.dumps({"outcome": "activated", "generation": "new"}))
    with pytest.raises(activation.ActivationError):
        activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)


def test_changed_selected_release_cannot_use_old_capture(fixture, retired_capture):
    binding, _, _ = retired_capture
    fixture.paths.current_link.unlink()
    fixture.paths.current_link.symlink_to(fixture.predecessor)
    with pytest.raises(activation.ActivationError):
        activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)


@pytest.mark.parametrize("changed_bytes", [False, True])
def test_later_retirement_or_duplicate_archive_cannot_rescue_old_proof(fixture, retired_capture, changed_bytes):
    binding, archive, _ = retired_capture
    later = archive.parent / "later-activation"
    later.mkdir(mode=0o700)
    receipt = json.loads((archive / activation.RETIREMENT_RECEIPT_NAME).read_text())
    receipt["retiredAt"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    for key, name in [("result", fixture.paths.result.name), ("startFence", activation.START_CONSUMED_NAME)]:
        payload = (archive / name).read_bytes()
        if changed_bytes and key == "result":
            value = json.loads(payload)
            value["generation"] = "later"
            payload = json.dumps(value).encode()
        p = later / name
        p.write_bytes(payload)
        p.chmod(0o400)
        info = p.stat()
        receipt[key] = {"path": str(p), "sha256": hashlib.sha256(payload).hexdigest(),
                        "device": info.st_dev, "inode": info.st_ino}
    p = later / activation.RETIREMENT_RECEIPT_NAME
    p.write_text(json.dumps(receipt))
    p.chmod(0o400)
    with pytest.raises(activation.ActivationError):
        activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)


@pytest.mark.parametrize("kind", ["lock", "fence", "incomplete_archive"])
def test_inflight_lifecycle_cannot_promote_retired_capture(fixture, retired_capture, kind):
    binding, archive, _ = retired_capture
    descriptor = None
    try:
        if kind == "lock":
            descriptor = os.open(fixture.paths.lock, os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif kind == "fence":
            fixture.paths.result.with_name(activation.START_CONSUMED_NAME).write_text("{}")
        else:
            incomplete = archive.parent / "incomplete"
            incomplete.mkdir(mode=0o700)
            (incomplete / fixture.paths.result.name).write_bytes((archive / fixture.paths.result.name).read_bytes())
        with pytest.raises(activation.ActivationError):
            activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def test_process_rollover_during_retired_lookup_is_rejected(fixture, retired_capture, monkeypatch):
    binding, _, _ = retired_capture
    identities = iter([("darwin:20:1", fixture.paths.node, 1), ("darwin:21:2", fixture.paths.node, 1)])
    monkeypatch.setattr(activation, "process_identity", lambda _: next(identities))
    with pytest.raises(activation.ActivationError):
        activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)


@pytest.mark.parametrize("name", ["retirement-receipt.json", "activation-result.json", "activation-start-consumed.json"])
def test_oversized_archive_record_is_rejected_before_body_read(fixture, retired_capture, monkeypatch, name):
    binding, archive, _ = retired_capture
    oversized = archive / name
    oversized.chmod(0o600)
    oversized.write_bytes(b"x" * (256 * 1024 + 1))
    oversized.chmod(0o400)
    real_open = os.open

    def checked_open(path, *args, **kwargs):
        assert path != oversized, "oversized record was read before rejection"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", checked_open)
    with pytest.raises(activation.ActivationError):
        activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)


@pytest.mark.parametrize("name", ["activation.lock", "retirement-receipt.json", "activation-result.json", "activation-start-consumed.json"])
def test_nonregular_retirement_authority_is_rejected_before_open(fixture, retired_capture, monkeypatch, name):
    binding, archive, _ = retired_capture
    target = fixture.paths.lock if name == "activation.lock" else archive / name
    target.unlink()
    os.mkfifo(target, 0o600 if name == "activation.lock" else 0o400)
    real_open = os.open

    def checked_open(path, *args, **kwargs):
        assert path != target, "nonregular authority was opened before rejection"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", checked_open)
    with pytest.raises(activation.ActivationError):
        activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)


@pytest.mark.parametrize("name", ["activation.lock", "retirement-receipt.json"])
def test_fifo_replacement_between_inspection_and_open_is_nonblocking(fixture, retired_capture, monkeypatch, name):
    binding, archive, _ = retired_capture
    target = fixture.paths.lock if name == "activation.lock" else archive / name
    real_open = os.open
    replaced = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == target:
            assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
            target.unlink()
            os.mkfifo(target, 0o600 if name == "activation.lock" else 0o400)
            replaced = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(activation.ActivationError):
        activation.verify_screen_capture_binding(binding, fixture.paths.node, require_current_process=True)
    assert replaced
