from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from test_openclaw_runtime_activate import activate_module as activation, fixture
from test_screen_capture_continuity import capture_binding


@pytest.fixture
def candidate_grants(fixture, capture_binding):
    binding, identities, database, _, _ = capture_binding
    candidate = fixture.root / "new-managed-node"
    candidate.write_bytes(b"synthetic future Node binary")
    candidate.chmod(0o555)
    info = candidate.stat()
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    identities[str(candidate)] = {
        "path": str(candidate), "device": info.st_dev, "inode": info.st_ino,
        "bytes": info.st_size, "mtimeNs": info.st_mtime_ns, "sha256": digest,
        "signingIdentity": {"Identifier=": "node", "CDHash=": "b" * 40}}
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO access VALUES(?,?,?,?,?,?,?,?)", (
            "kTCCServiceScreenCapture", str(candidate), 1, 2, 4, 1, b"candidate screen requirement", 200))
    return binding, candidate, digest, database


def add_fda(database, candidate, *, auth=2, requirement=b"candidate FDA requirement"):
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO access VALUES(?,?,?,?,?,?,?,?)", (
            "kTCCServiceSystemPolicyAllFiles", str(candidate), 1, auth, 4, 1, requirement, 201))


def enroll(fixture, candidate_grants, output):
    binding, candidate, digest, database = candidate_grants
    return activation.enroll_node_toolchain_screen_capture_precondition(
        binding, fixture.paths.node, candidate, digest, database, output)


@pytest.mark.parametrize("grant", ["missing", "denied", "other_client", "wrong_requirement"])
def test_future_node_cannot_enroll_with_screen_capture_only_or_invalid_filesystem_grant(
        fixture, candidate_grants, monkeypatch, grant):
    _, candidate, _, database = candidate_grants
    if grant == "denied": add_fda(database, candidate, auth=0)
    elif grant == "other_client": add_fda(database, fixture.paths.node)
    elif grant == "wrong_requirement":
        add_fda(database, candidate, requirement=b"mismatched candidate requirement")
        def requirement(client, compiled):
            if compiled == b"mismatched candidate requirement":
                raise activation.ActivationError("Full Disk Access code requirement mismatch")
        monkeypatch.setattr(activation, "verify_screen_capture_requirement", requirement)
    before = database.read_bytes()
    output = fixture.root / "future-precondition.json"
    with pytest.raises(activation.ActivationError, match="Full Disk Access"):
        enroll(fixture, candidate_grants, output)
    assert not output.exists() and database.read_bytes() == before


def test_future_node_v2_binds_exact_fda_grant_and_never_claims_effective_access(
        fixture, candidate_grants, monkeypatch):
    _, candidate, _, database = candidate_grants
    add_fda(database, candidate)
    calls = []
    monkeypatch.setattr(activation, "verify_screen_capture_requirement", lambda node, req: calls.append((node, req)))
    before = database.read_bytes()
    output = fixture.root / "future-precondition.json"
    receipt = enroll(fixture, candidate_grants, output)
    assert receipt["schemaVersion"] == 2
    assert receipt["filesystemPermission"]["service"] == "kTCCServiceSystemPolicyAllFiles"
    assert receipt["filesystemPermission"]["authValue"] == 2
    assert receipt["effectiveFilesystemAccessVerified"] is False
    assert receipt["effectiveCaptureVerified"] is False and receipt["scheduledSyncProven"] is False
    assert (candidate, b"candidate FDA requirement") in calls
    assert database.read_bytes() == before and output.stat().st_mode & 0o777 == 0o400
    verified = activation.verify_node_toolchain_screen_capture_precondition(
        output, candidate, expected_sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    assert verified["recordedCandidateFilesystemPermissionVerified"] is True
    assert verified["effectiveFilesystemAccessVerified"] is False


def test_future_node_filesystem_grant_revocation_invalidates_v2_without_editing_receipt(
        fixture, candidate_grants):
    _, candidate, _, database = candidate_grants
    add_fda(database, candidate)
    output = fixture.root / "future-precondition.json"
    enroll(fixture, candidate_grants, output)
    before = output.read_bytes()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE access SET auth_value=0 WHERE service=? AND client=?", (
            "kTCCServiceSystemPolicyAllFiles", str(candidate)))
    with pytest.raises(activation.ActivationError, match="Full Disk Access"):
        activation.verify_node_toolchain_screen_capture_precondition(
            output, candidate, expected_sha256=hashlib.sha256(before).hexdigest())
    assert output.read_bytes() == before


def test_historical_v1_read_still_works_without_candidate_fda_and_never_grants_forward_access(
        fixture, candidate_grants):
    binding, candidate, digest, database = candidate_grants
    facts = activation._node_toolchain_screen_capture_precondition_facts(
        binding, fixture.paths.node, candidate, digest, database)
    output = fixture.root / "historical-v1-precondition.json"
    activation.create_immutable_file(output, json.dumps({**facts, "enrolledAt": "2026-10-21T00:00:00Z"}).encode())
    before = output.read_bytes()
    result = activation.verify_node_toolchain_screen_capture_precondition(
        output, candidate, expected_sha256=hashlib.sha256(before).hexdigest())
    assert result["recordedCandidatePermissionVerified"] is True
    assert result["recordedCandidateFilesystemPermissionVerified"] is False
    assert output.read_bytes() == before
