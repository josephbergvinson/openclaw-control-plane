from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from test_openclaw_runtime_activate import (
    CANDIDATE_COMMIT, FakeBackend, activate_module as activation, fixture,
)


@pytest.fixture
def capture_binding(fixture, monkeypatch):
    node = fixture.paths.node
    tool = fixture.root / "peekaboo"
    tool.write_bytes(b"fixture native capture tool")
    identities = {}
    for path in (node, tool):
        info = path.stat()
        identities[str(path)] = {
            "path": str(path), "device": info.st_dev, "inode": info.st_ino,
            "bytes": info.st_size, "mtimeNs": info.st_mtime_ns,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "signingIdentity": {"Identifier=": path.name, "CDHash=": "a" * 40},
        }
    monkeypatch.setattr(activation, "screen_capture_code_identity", lambda path: dict(identities[str(path)]))
    monkeypatch.setattr(activation, "verify_screen_capture_requirement", lambda client, requirement: None)
    monkeypatch.setattr(activation, "process_identity", lambda pid: ("darwin:20:1", node, 1))
    active = fixture.root / "current-activation.json"
    active.write_text(json.dumps({"outcome": "activated"}))
    database = fixture.root / "permission.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE access(service,client,client_type,auth_value,auth_reason,auth_version,csreq,last_modified)")
        connection.execute("INSERT INTO access VALUES(?,?,?,?,?,?,?,?)", (
            "kTCCServiceScreenCapture", str(node), 1, 2, 4, 1, b"fixture requirement", 123,
        ))
    proof = {
        "status": "current_scheduler_route_capture_and_responsibility_verified",
        "verifiedAt": "2026-09-12T04:45:00Z", "route": activation.SCREEN_CAPTURE_ROUTE,
        "imageVisuallyVerifiedAsJournal": True, "manualProbe": True,
        "scheduledSyncProven": False, "producerInvoked": False, "tccMutated": False,
        "responsiblePid": 123, "responsibleProcessIdentity": ["darwin:20:1", str(node), "1"],
        "files": [{**value, "codesignVerified": True} for value in identities.values()],
        "activationReceiptSha256": hashlib.sha256(active.read_bytes()).hexdigest(),
        "tccdAttributionAndEffectivePermission": [{
            "message": "Handling access request to kTCCServiceScreenCapture, from Sub:{"
            + str(node) + "}Resp:{pid=123,} Auth Right: Allowed (System Set),DB Action:None",
        }],
    }
    source = fixture.root / "native-proof.json"
    source.write_text(json.dumps(proof))
    output = fixture.root / "capture-binding.json"
    activation.enroll_screen_capture_binding(
        replace(fixture.paths, result=active), source,
        hashlib.sha256(source.read_bytes()).hexdigest(), database, output,
    )
    return output, identities, database, source, active


def test_exact_native_capture_binding_checks_current_permission_read_only(fixture, capture_binding):
    path, _, database, _, _ = capture_binding
    before = database.read_bytes()
    result = activation.verify_screen_capture_binding(path, fixture.paths.node, require_current_process=True)
    assert result["protectedCodeIdentityUnchanged"] is True
    assert result["recordedPermissionUnchanged"] is True
    assert result["effectiveCaptureVerifiedForCurrentProcess"] is True
    assert result["scheduledSyncProven"] is False
    assert database.read_bytes() == before


@pytest.mark.parametrize("returncode,timed_out", [(1, False), (0, True)])
def test_tcc_requirement_failure_is_not_replaced_by_valid_self_signature(tmp_path, monkeypatch,
                                                                       returncode, timed_out):
    client = tmp_path / "node"
    requirement = b"compiled TCC requirement"
    seen = []
    def check(argv, timeout):
        assert argv[:4] == ("/usr/bin/codesign", "--verify", "--strict", "--test-requirement")
        assert argv[-1] == str(client)
        seen.append(Path(argv[4]))
        assert seen[0].read_bytes() == requirement
        return activation.CommandResult(argv, returncode, b"", b"", 1, timed_out)
    monkeypatch.setattr(activation, "run_bounded", check)
    with pytest.raises(activation.ActivationError, match="does not match the exact executable"):
        activation.verify_screen_capture_requirement(client, requirement)
    assert not seen[0].exists()


def test_permission_row_is_checked_against_its_exact_client_and_blob(fixture, capture_binding, monkeypatch):
    _, _, database, _, _ = capture_binding
    seen = []
    monkeypatch.setattr(activation, "verify_screen_capture_requirement",
                        lambda client, blob: seen.append((client, blob)))
    activation.screen_capture_permission(database, fixture.paths.node)
    assert seen == [(fixture.paths.node, b"fixture requirement")]


@pytest.mark.parametrize("field,value", [("inode", 999), ("sha256", "f" * 64), ("mtimeNs", 999),
                                        ("signingIdentity", {"Identifier=": "different"})])
def test_code_or_signature_drift_rejects_old_native_proof(fixture, capture_binding, field, value):
    path, identities, _, _, _ = capture_binding
    identities[str(fixture.paths.node)][field] = value
    with pytest.raises(activation.ActivationError, match="executable or signature drift"):
        activation.verify_screen_capture_binding(path, fixture.paths.node)


@pytest.mark.parametrize("sql", ["UPDATE access SET auth_value=0", "DELETE FROM access",
                                  "UPDATE access SET csreq=X'12'", "UPDATE access SET last_modified=124"])
def test_revoked_missing_or_changed_permission_invalidates(fixture, capture_binding, sql):
    path, _, database, _, _ = capture_binding
    with sqlite3.connect(database) as connection:
        connection.execute(sql)
    with pytest.raises(activation.ActivationError, match="permission"):
        activation.verify_screen_capture_binding(path, fixture.paths.node)


def test_process_rollover_preserves_only_identity_preconditions(fixture, capture_binding, monkeypatch):
    path, _, _, _, _ = capture_binding
    monkeypatch.setattr(activation, "process_identity", lambda pid: ("darwin:21:2", fixture.paths.node, 1))
    result = activation.verify_screen_capture_binding(path, fixture.paths.node)
    assert result["effectiveCaptureVerifiedForCurrentProcess"] is False
    assert result["scheduledSyncProven"] is False
    with pytest.raises(activation.ActivationError, match="fresh native route probe"):
        activation.verify_screen_capture_binding(path, fixture.paths.node, require_current_process=True)


def test_new_activation_invalidates_current_behavior_even_if_old_pid_remains(fixture, capture_binding):
    path, _, _, _, active = capture_binding
    active.write_text(json.dumps({"outcome": "activated", "generation": "new"}))
    with pytest.raises(activation.ActivationError, match="fresh native route probe"):
        activation.verify_screen_capture_binding(path, fixture.paths.node, require_current_process=True)


@pytest.mark.parametrize("field,value", [("imageVisuallyVerifiedAsJournal", False),
                                        ("scheduledSyncProven", True), ("producerInvoked", True),
                                        ("responsibleProcessIdentity", []),
                                        ("tccdAttributionAndEffectivePermission", [])])
def test_unproven_effective_capture_cannot_be_enrolled(fixture, capture_binding, field, value):
    _, _, _, source, _ = capture_binding
    proof = json.loads(source.read_text())
    proof[field] = value
    source.write_text(json.dumps(proof))
    with pytest.raises(activation.ActivationError):
        activation.load_screen_capture_acceptance(source, hashlib.sha256(source.read_bytes()).hexdigest())


def test_binding_cannot_replace_proven_executable_with_an_unrelated_current_one(fixture, capture_binding):
    path, identities, _, _, _ = capture_binding
    payload = json.loads(path.read_text())
    identities[str(fixture.paths.node)]["sha256"] = "f" * 64
    payload["files"][0]["sha256"] = "f" * 64
    path.chmod(0o600)
    path.write_text(json.dumps(payload))
    path.chmod(0o400)
    with pytest.raises(activation.ActivationError, match="differs from native proof"):
        activation.verify_screen_capture_binding(path, fixture.paths.node)


def test_activation_permission_failure_rejects_before_fence_and_boot(fixture):
    backend = FakeBackend(fixture)
    def fail():
        raise activation.ActivationError("permission unknown")
    backend.verify_screen_capture_continuity = fail
    result = activation.activate(fixture.paths, fixture.candidate, CANDIDATE_COMMIT,
                                 fixture.seal, fixture.snapshot, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0
    assert not activation._start_fence_path(fixture.paths).exists()


def test_poststart_permission_failure_uses_existing_restore_required_path(fixture):
    backend = FakeBackend(fixture)
    calls = []
    def verify():
        calls.append(True)
        if len(calls) == 2:
            raise activation.ActivationError("permission changed after bootstrap")
        return {"bindingSha256": "a" * 64, "effectiveCaptureVerifiedForCurrentProcess": False}
    backend.verify_screen_capture_continuity = verify
    result = activation.activate(fixture.paths, fixture.candidate, CANDIDATE_COMMIT,
                                 fixture.seal, fixture.snapshot, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert backend.gateway_boots == backend.node_boots == 1
    assert result["restoreRequired"] is True


def test_replacing_binding_between_preflight_and_poststart_requires_restore(fixture):
    backend = FakeBackend(fixture)
    observations = iter([{"bindingSha256": "a" * 64}, {"bindingSha256": "b" * 64}])
    backend.verify_screen_capture_continuity = lambda: next(observations)
    result = activation.activate(fixture.paths, fixture.candidate, CANDIDATE_COMMIT,
                                 fixture.seal, fixture.snapshot, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert backend.gateway_boots == backend.node_boots == 1


def test_activated_result_rechecks_continuity_without_rewriting_old_receipt(fixture):
    backend = FakeBackend(fixture)
    result = activation.activate(fixture.paths, fixture.candidate, CANDIDATE_COMMIT,
                                 fixture.seal, fixture.snapshot, backend)
    assert result["outcome"] == "activated"
    before = fixture.paths.result.read_bytes()
    def fail():
        raise activation.ActivationError("permission revoked")
    backend.verify_screen_capture_continuity = fail
    with pytest.raises(activation.ActivationError, match="permission revoked"):
        activation.activate(fixture.paths, fixture.candidate, CANDIDATE_COMMIT,
                            fixture.seal, fixture.snapshot, backend)
    assert fixture.paths.result.read_bytes() == before
    assert backend.gateway_boots == backend.node_boots == 1


def test_capability_runner_focused_failure_does_not_invoke_broad_suite(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/capability_validation_runner.py"
    result = subprocess.run([sys.executable, str(script), "--screen-capture-binding", str(tmp_path / "absent.json")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"status": "unknown", "nativeCaptureRequired": True, "scheduledSyncProven": False}


def test_resolver_removes_ready_effects_when_bound_receipt_is_missing(tmp_path):
    from scripts import resolve_capability
    row = {"state": "ready", "last_verified_utc": "2026-09-12T04:45:00Z", "slo_max_age_days": 1,
           "evidence_effects": ["read"], "evidence_operations": ["screen-capture"],
           "screen_capture_binding": {"path": str(tmp_path / "absent.json"), "sha256": "a" * 64}}
    result = resolve_capability.readiness_for_status_id("Journal capture", {"Journal capture": row})
    assert result["state"] == "unknown"
    assert result["evidence_effects"] == result["evidence_operations"] == []


@pytest.mark.parametrize("dependencies", [[], ["tools"]])
def test_invalidation_checks_runtime_binding_without_config_dependencies(tmp_path, dependencies):
    from scripts import capability_config_invalidation
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"capabilities": [{
        "capability_id": "Journal capture", "config_dependencies": dependencies,
        "last_verified_utc": "2026-09-12T04:45:00Z",
        "screen_capture_binding": {"path": str(tmp_path / "absent.json"), "sha256": "a" * 64},
    }]}))
    result = capability_config_invalidation.report_invalidations(status, [])
    assert len(result["invalidated"]) == 1
    assert result["invalidated"][0]["capability_id"] == "Journal capture"
    assert "not current" in result["invalidated"][0]["reason"]
    assert result["unknown"] == []


@pytest.mark.parametrize("reference", [None, {"path": "relative", "sha256": "a" * 64},
                                       {"path": "/absolute", "sha256": "unknown"},
                                       {"path": "/absolute", "sha256": "a" * 64, "ready": True}])
def test_status_contract_rejects_unbound_screen_capture_hints(reference):
    from scripts.capability_registry_contract import validate_capability_status_record, RegistryContractError
    row = {"capability_id": "Journal capture", "state": "ready", "support_class": "supported",
           "freshness_class": "reprobe-before-use", "last_verified_utc": "2026-09-12T04:45:00Z",
           "evidence": "manual native capture", "constraint_or_fallback": "scheduled sync unproven",
           "domain": "control-plane", "slo_max_age_days": 1, "config_dependencies": [],
           "screen_capture_binding": reference}
    with pytest.raises(RegistryContractError):
        validate_capability_status_record(row, source="test")


def test_capability_reference_requires_exact_receipt_digest(fixture, capture_binding, monkeypatch):
    path, _, _, _, _ = capture_binding
    values = {section: activation.OPERATOR.get(section, {}) for section in ("paths", "runtime", "identifiers")}
    values["paths"]["node_binary"] = str(fixture.paths.node)
    from scripts.operator_contract import OperatorContract
    monkeypatch.setattr(activation, "OPERATOR", OperatorContract(values))
    with pytest.raises(activation.ActivationError, match="binding changed"):
        activation.verify_screen_capture_capability({"path": str(path), "sha256": "f" * 64})
    result = activation.verify_screen_capture_capability({
        "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    })
    assert result["effectiveCaptureVerifiedForCurrentProcess"] is True


def test_adopter_bootstrap_absence_and_explicit_guard_binding(tmp_path, monkeypatch):
    from scripts.operator_contract import OperatorContract
    monkeypatch.setattr(activation, "OPERATOR", OperatorContract({"paths": {"screen_capture_binding": None}}))
    assert activation.configured_screen_capture_binding() is None
    binding = tmp_path / "enrolled.json"
    monkeypatch.setattr(activation, "OPERATOR", OperatorContract({"paths": {"screen_capture_binding": str(binding)}}))
    assert activation.configured_screen_capture_binding() == binding


@pytest.mark.parametrize("unresolved", ["relative.json", "<absolute-binding>", ""])
def test_unresolved_adopter_binding_cannot_silently_disable_guard(monkeypatch, unresolved):
    from scripts.operator_contract import OperatorContract
    monkeypatch.setattr(activation, "OPERATOR", OperatorContract({"paths": {"screen_capture_binding": unresolved}}))
    with pytest.raises(ValueError):
        activation.configured_screen_capture_binding()
