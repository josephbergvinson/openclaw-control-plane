from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_openclaw_runtime_activate import (
    CANDIDATE_COMMIT, FakeBackend, activate_module as activation, fixture,
)


@pytest.fixture
def capture_binding(fixture, monkeypatch, request):
    node = fixture.paths.node
    tool = fixture.root / "peekaboo"
    tool.write_bytes(b"fixture native capture tool")
    identities = {}
    for path in (node, tool):
        info = path.stat()
        identities[str(path)] = {
            "path": str(path), "device": getattr(request, "param", info.st_dev), "inode": info.st_ino,
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


@pytest.mark.parametrize("capture_binding", [16777234], indirect=True)
@pytest.mark.parametrize("roles", [("node",), ("tool",), ("node", "tool")])
def test_device_renumber_preserves_durable_code_and_unchanged_native_evidence(
        fixture, capture_binding, roles):
    path, identities, database, source, _ = capture_binding
    before = {item: item.read_bytes() for item in (path, source, database)}
    for role in roles:
        client = fixture.paths.node if role == "node" else fixture.root / "peekaboo"
        identities[str(client)]["device"] = 16777229
    result = activation.verify_screen_capture_binding(path, fixture.paths.node, require_current_process=True)
    assert result["protectedCodeIdentityUnchanged"] is True
    assert result["recordedPermissionUnchanged"] is True
    assert result["effectiveCaptureVerifiedForCurrentProcess"] is True
    assert result["scheduledSyncProven"] is False
    assert {item: item.read_bytes() for item in before} == before
    assert {row["device"] for row in json.loads(path.read_bytes())["files"]} == {16777234}


@pytest.mark.parametrize("value", [None, True, "16777229", 0, -1, "missing"])
def test_device_renumber_does_not_admit_malformed_current_identity(
        fixture, capture_binding, value):
    path, identities, _, _, _ = capture_binding
    current = identities[str(fixture.paths.node)]
    if value == "missing":
        current.pop("device")
    else:
        current["device"] = value
    with pytest.raises(activation.ActivationError, match="executable or signature drift"):
        activation.verify_screen_capture_binding(path, fixture.paths.node)


def test_device_renumber_does_not_admit_unbound_extra_identity_field(fixture, capture_binding):
    path, identities, _, _, _ = capture_binding
    identities[str(fixture.paths.node)]["device"] = 16777229
    identities[str(fixture.paths.node)]["volumeUuid"] = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    with pytest.raises(activation.ActivationError, match="executable or signature drift"):
        activation.verify_screen_capture_binding(path, fixture.paths.node)


@pytest.mark.parametrize("capture_binding", [16777234], indirect=True)
def test_same_bytes_signed_replacement_is_rejected_after_device_renumber(
        fixture, capture_binding, monkeypatch):
    path, identities, _, source, _ = capture_binding
    before = {item: item.read_bytes() for item in (path, source)}
    client = fixture.paths.node
    old_info = client.stat()
    replacement = client.with_name("replacement-node")
    replacement.write_bytes(client.read_bytes())
    os.utime(replacement, ns=(old_info.st_atime_ns, old_info.st_mtime_ns))
    replacement.replace(client)
    assert client.stat().st_ino != old_info.st_ino

    def current_identity(observed):
        identity = dict(identities[str(observed)])
        info = observed.stat()
        identity.update(device=16777229, inode=info.st_ino, bytes=info.st_size,
                        mtimeNs=info.st_mtime_ns,
                        sha256=hashlib.sha256(observed.read_bytes()).hexdigest())
        return identity

    monkeypatch.setattr(activation, "screen_capture_code_identity", current_identity)
    with pytest.raises(activation.ActivationError, match="executable or signature drift"):
        activation.verify_screen_capture_binding(path, fixture.paths.node)
    assert {item: item.read_bytes() for item in before} == before


def test_device_change_during_native_code_inspection_is_still_rejected(monkeypatch):
    before = SimpleNamespace(st_dev=16777234, st_ino=17, st_size=3,
                             st_mtime_ns=4, st_ctime_ns=5)
    after = SimpleNamespace(**{**vars(before), "st_dev": 16777229})

    class InspectedPath:
        def __str__(self):
            return "/fixture/native-client"

        def stat(self):
            return after

    client = InspectedPath()
    monkeypatch.setattr(activation, "sha256_physical_file", lambda path, label: ("a" * 64, before))

    def codesign(argv, timeout):
        details = (b"designated => fixture\nIdentifier=fixture\n"
                   b"TeamIdentifier=fixture\nCDHash=fixture\n")
        return activation.CommandResult(argv, 0, details if "-d" in argv else b"", b"", 1, False)

    monkeypatch.setattr(activation, "run_bounded", codesign)
    with pytest.raises(activation.ActivationError, match="changed during inspection"):
        activation.screen_capture_code_identity(client)


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


@pytest.mark.parametrize("role", ["node", "tool"])
@pytest.mark.parametrize("field,value", [("path", "/different"), ("inode", 999), ("bytes", 999),
                                        ("sha256", "f" * 64), ("mtimeNs", 999),
                                        ("signingIdentity", {"Identifier=": "different"})])
def test_code_or_signature_drift_rejects_old_native_proof(fixture, capture_binding, role, field, value):
    path, identities, _, _, _ = capture_binding
    for identity in identities.values():
        identity["device"] = 16777229
    client = fixture.paths.node if role == "node" else fixture.root / "peekaboo"
    identities[str(client)][field] = value
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
    path, identities, _, _, _ = capture_binding
    for identity in identities.values():
        identity["device"] = 16777229
    monkeypatch.setattr(activation, "process_identity", lambda pid: ("darwin:21:2", fixture.paths.node, 1))
    result = activation.verify_screen_capture_binding(path, fixture.paths.node)
    assert result["effectiveCaptureVerifiedForCurrentProcess"] is False
    assert result["scheduledSyncProven"] is False
    with pytest.raises(activation.ActivationError, match="fresh native route probe"):
        activation.verify_screen_capture_binding(path, fixture.paths.node, require_current_process=True)


def test_new_activation_invalidates_current_behavior_even_if_old_pid_remains(fixture, capture_binding):
    path, identities, _, _, active = capture_binding
    for identity in identities.values():
        identity["device"] = 16777229
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
    values["paths"]["node_binary"] = str(fixture.root / "obsolete-compatibility-node")
    from scripts.operator_contract import OperatorContract
    monkeypatch.setattr(activation, "OPERATOR", OperatorContract(values))
    monkeypatch.setattr(activation, "live_paths", lambda: fixture.paths)
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


def _canonical_refresh(fixture, capture_binding):
    path, _, database, source, active = capture_binding
    paths = replace(fixture.paths, result=active, screen_capture_binding=path)
    archive_root = fixture.root / "capture-predecessor-custody"
    archive_root.mkdir(mode=0o700)
    archive = archive_root / "old-binding.json"
    before = path.read_bytes()
    proof = json.loads(source.read_text())
    current = {"outcome": "activated", "restoreRequired": False, "verification": {"loaded": {
        "pid": proof["responsiblePid"], "startToken": proof["responsibleProcessIdentity"][0],
        "executable": str(paths.node)}}}
    active.write_text(json.dumps(current))
    proof["activationReceiptSha256"] = hashlib.sha256(active.read_bytes()).hexdigest()
    proof["verifiedAt"] = "2026-10-22T01:00:00Z"
    source.write_text(json.dumps(proof))
    return paths, source, database, archive, before


def _refresh(paths, source, database, archive, before):
    return activation.enroll_screen_capture_binding(
        paths, source, hashlib.sha256(source.read_bytes()).hexdigest(), database,
        paths.screen_capture_binding, replace_binding_sha256=hashlib.sha256(before).hexdigest(),
        replaced_binding_archive=archive)


def test_explicit_canonical_capture_refresh_archives_exact_old_bytes_and_current_grant(
        fixture, capture_binding):
    paths, source, database, archive, before = _canonical_refresh(fixture, capture_binding)
    grant_before = database.read_bytes()
    result = _refresh(paths, source, database, archive, before)
    assert archive.read_bytes() == before
    assert archive.stat().st_mode & 0o777 == 0o400
    assert paths.screen_capture_binding.stat().st_mode & 0o777 == 0o400
    assert result["replacedBinding"] == {
        "path": str(paths.screen_capture_binding), "sha256": hashlib.sha256(before).hexdigest(),
        "archivePath": str(archive), "archiveSha256": hashlib.sha256(before).hexdigest()}
    assert json.loads(paths.screen_capture_binding.read_text()) == result
    assert paths.screen_capture_binding.read_bytes() != before
    assert database.read_bytes() == grant_before
    assert activation.verify_screen_capture_binding(
        paths.screen_capture_binding, paths.node, require_current_process=True
    )["effectiveCaptureVerifiedForCurrentProcess"] is True


def test_default_enrollment_still_cannot_overwrite_existing_canonical_binding(fixture, capture_binding):
    paths, source, database, archive, before = _canonical_refresh(fixture, capture_binding)
    with pytest.raises(activation.ActivationError, match="already exists"):
        activation.enroll_screen_capture_binding(
            paths, source, hashlib.sha256(source.read_bytes()).hexdigest(), database,
            paths.screen_capture_binding)
    assert paths.screen_capture_binding.read_bytes() == before and not archive.exists()


@pytest.mark.parametrize("mutation", ["stale_sha", "noncanonical", "missing_sha", "missing_archive",
                                      "symlink", "writable", "archive_exists"])
def test_canonical_capture_refresh_rejects_bad_custody_before_replacement(
        fixture, capture_binding, mutation):
    paths, source, database, archive, before = _canonical_refresh(fixture, capture_binding)
    output, expected = paths.screen_capture_binding, hashlib.sha256(before).hexdigest()
    if mutation == "stale_sha": expected = "0" * 64
    elif mutation == "noncanonical": output = fixture.root / "other-binding.json"
    elif mutation == "missing_sha": expected = None
    elif mutation == "missing_archive": archive = None
    elif mutation == "symlink":
        original = output.with_name("original-capture-binding.json")
        output.rename(original)
        output.symlink_to(original)
    elif mutation == "writable": output.chmod(0o600)
    elif mutation == "archive_exists": activation.create_immutable_file(archive, b"retained earlier custody")
    with pytest.raises(activation.ActivationError):
        activation.enroll_screen_capture_binding(
            paths, source, hashlib.sha256(source.read_bytes()).hexdigest(), database, output,
            replace_binding_sha256=expected, replaced_binding_archive=archive)
    assert paths.screen_capture_binding.read_bytes() == before
    if archive is not None and mutation != "archive_exists": assert not archive.exists()


@pytest.mark.parametrize("mutation", ["process", "permission", "activation", "code", "gateway"])
def test_canonical_capture_refresh_rejects_stale_fresh_capture_without_replacing(
        fixture, capture_binding, monkeypatch, mutation):
    paths, source, database, archive, before = _canonical_refresh(fixture, capture_binding)
    if mutation == "process":
        monkeypatch.setattr(activation, "process_identity", lambda pid: ("darwin:21:1", paths.node, 1))
    elif mutation == "permission":
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE access SET auth_value=0")
    elif mutation == "gateway":
        current = json.loads(paths.result.read_text())
        current["verification"]["loaded"]["pid"] = 999
        paths.result.write_text(json.dumps(current))
        proof = json.loads(source.read_text())
        proof["activationReceiptSha256"] = hashlib.sha256(paths.result.read_bytes()).hexdigest()
        source.write_text(json.dumps(proof))
    elif mutation == "activation": paths.result.write_text(json.dumps({"outcome": "activated", "later": True}))
    elif mutation == "code": capture_binding[1][str(paths.node)]["sha256"] = "0" * 64
    with pytest.raises(activation.ActivationError):
        _refresh(paths, source, database, archive, before)
    assert paths.screen_capture_binding.read_bytes() == before and not archive.exists()


def test_canonical_capture_refresh_honors_existing_activation_lock(fixture, capture_binding):
    paths, source, database, archive, before = _canonical_refresh(fixture, capture_binding)
    descriptor = activation._acquire_lock(paths)
    try:
        with pytest.raises(activation.ActivationError, match="another activator"):
            _refresh(paths, source, database, archive, before)
    finally:
        activation._release_lock(descriptor)
    assert paths.screen_capture_binding.read_bytes() == before and not archive.exists()


@pytest.mark.parametrize("mutation", ["preimage", "lock", "grant", "successor"])
def test_canonical_capture_refresh_rechecks_cas_lock_and_grant_before_atomic_publication(
        fixture, capture_binding, monkeypatch, mutation):
    paths, source, database, archive, before = _canonical_refresh(fixture, capture_binding)
    create = activation.create_immutable_file
    observed = []
    def gated_create(path, payload):
        create(path, payload)
        if path.name.startswith("." + paths.screen_capture_binding.name + ".capture-enroll-"):
            observed.append(path)
            if mutation == "preimage":
                paths.screen_capture_binding.chmod(0o600)
                paths.screen_capture_binding.write_bytes(before + b" ")
                paths.screen_capture_binding.chmod(0o400)
            elif mutation == "lock":
                paths.lock.unlink()
                paths.lock.write_bytes(b"different lock inode")
                paths.lock.chmod(0o600)
            elif mutation == "successor":
                path.chmod(0o600)
                path.write_bytes(b"unadmitted staged replacement")
                path.chmod(0o400)
            else:
                monkeypatch.setattr(activation, "screen_capture_permission", lambda *args: {"grant": "changed"})
    monkeypatch.setattr(activation, "create_immutable_file", gated_create)
    with pytest.raises(activation.ActivationError, match="changed|drift"):
        _refresh(paths, source, database, archive, before)
    assert len(observed) == 1 and not observed[0].exists()
    assert archive.read_bytes() == before and archive.stat().st_mode & 0o777 == 0o400
    assert paths.screen_capture_binding.read_bytes() == before + (b" " if mutation == "preimage" else b"")


def test_enrollment_cli_exposes_only_explicit_paired_canonical_refresh_options():
    args = activation.build_parser().parse_args([
        "screen-capture-enroll", "--acceptance", "/fixture/proof.json", "--acceptance-sha256", "a" * 64,
        "--permission-database", "/fixture/permission.sqlite", "--output", "/fixture/canonical.json",
        "--replace-binding-sha256", "b" * 64, "--replaced-binding-archive", "/fixture/archive/old.json"])
    assert args.replace_binding_sha256 == "b" * 64
    assert args.replaced_binding_archive == Path("/fixture/archive/old.json")


def test_canonical_capture_refresh_does_not_overwrite_drift_during_final_native_proof(
        fixture, capture_binding, monkeypatch):
    paths, source, database, archive, before = _canonical_refresh(fixture, capture_binding)
    create, permission = activation.create_immutable_file, activation.screen_capture_permission
    staged, mutated = [], []
    def observe_staging(path, payload):
        create(path, payload)
        if path.name.startswith("." + paths.screen_capture_binding.name + ".capture-enroll-"):
            staged.append(path)
    def grant_with_concurrent_preimage_change(*args):
        grant = permission(*args)
        if staged and not mutated:
            paths.screen_capture_binding.chmod(0o600)
            paths.screen_capture_binding.write_bytes(before + b" ")
            paths.screen_capture_binding.chmod(0o400)
            mutated.append(True)
        return grant
    monkeypatch.setattr(activation, "create_immutable_file", observe_staging)
    monkeypatch.setattr(activation, "screen_capture_permission", grant_with_concurrent_preimage_change)
    with pytest.raises(activation.ActivationError, match="predecessor changed before publication"):
        _refresh(paths, source, database, archive, before)
    assert mutated == [True] and len(staged) == 1 and not staged[0].exists()
    assert archive.read_bytes() == before and archive.stat().st_mode & 0o777 == 0o400
    assert paths.screen_capture_binding.read_bytes() == before + b" "


def test_focused_capability_runner_uses_the_same_selected_node(
        fixture, capture_binding, monkeypatch, capsys):
    from scripts import capability_validation_runner as runner
    path = capture_binding[0]
    monkeypatch.setitem(sys.modules, "scripts.openclaw_runtime_activate", activation)
    configured_path = activation.OPERATOR.require_path
    def stale_seed(key):
        if key == "paths.node_binary":
            return fixture.root / "obsolete-compatibility-node"
        return configured_path(key)
    monkeypatch.setattr(activation.OPERATOR, "require_path", stale_seed)
    monkeypatch.setattr(activation, "live_paths", lambda: fixture.paths)
    monkeypatch.setattr(sys, "argv", ["capability-validation", "--screen-capture-binding", str(path)])
    assert runner.main() == 0
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "verified"
    assert value["effectiveCaptureVerifiedForCurrentProcess"] is True
    assert value["scheduledSyncProven"] is False
