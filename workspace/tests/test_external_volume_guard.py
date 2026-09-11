from __future__ import annotations
try:
    from scripts.operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import ast
import hashlib
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "external_volume_guard.py"
PLIST = OPERATOR.require_path('paths.workspace') / 'launchd' / 'com.openclaw.external-volume-guard.plist'
UUID = "11111111-2222-4333-8444-555555555555"
SENTINEL = bytes(range(256)) * 16
SENTINEL_SHA256 = hashlib.sha256(SENTINEL).hexdigest()


def load_guard_module():
    spec = importlib.util.spec_from_file_location("external_volume_guard_under_test", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fixture_contract(path: Path, *, mount: str = "/Volumes/Fixture OWC") -> Path:
    write_json(
        path,
        {
            "kind": "openclaw.owc-volume-guard.contract.v1",
            "schemaVersion": 2,
            "mountPoint": mount,
            "statePath": str(path.parent / "observer-state.json"),
            "incidentDirectory": str(path.parent / "incidents"),
            "lockPath": str(path.parent / "probe.lock"),
            "volumeUuid": UUID,
            "livenessProbe": {
                "path": f"{mount}/.openclaw-volume-guard-liveness-v1.bin",
                "sizeBytes": len(SENTINEL),
                "sha256": SENTINEL_SHA256,
                "timeoutSeconds": 10,
            },
            "protectedPaths": [
                {"rootId": "workspace", "logicalPath": "/fixture/workspace"},
                {"rootId": "runtime", "logicalPath": "/fixture/runtime"},
            ],
        },
    )
    return path


def fixture_probe(path: Path, *, healthy: bool, state: str | None = None) -> Path:
    write_json(
        path,
        {
            "kind": "openclaw.owc-volume-guard.probe.v1",
            "capturedAt": "2099-01-01T00:00:00Z",
            "expected": {
                "mountPoint": "/Volumes/Fixture OWC",
                "volumeUuid": UUID,
            },
            "observed": {
                "mountDevice": 999999,
                "volume": {"available": healthy},
                "protectedPaths": [],
            },
            "state": state or ("healthy" if healthy else "dismounted"),
            "healthy": healthy,
            "writesAllowed": healthy,
            "failSafeActive": not healthy,
        },
    )
    return path


def run_helper(contract: Path, probe: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--contract",
            str(contract),
            "--fixture-mode",
            "--fixture-probe-json",
            str(probe),
            *args,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def commit_probe_marker(guard, contract: dict, probe_id: str) -> None:
    descriptor = guard.acquire_probe_lock(contract)
    assert descriptor is not None
    try:
        guard.write_probe_lock_marker(
            descriptor,
            probe_id=probe_id,
            result_committed=True,
        )
    finally:
        os.close(descriptor)


def test_retired_cli_fails_closed_before_contract_or_volume_access(capsys) -> None:
    guard = load_guard_module()

    def unexpected_access(*_args, **_kwargs):
        raise AssertionError("retired CLI attempted contract or volume access")

    guard.normalize_contract = unexpected_access
    commands = [
        ["--contract", "/Volumes/Fixture must not be read/contract.json", "inspect"],
        [
            "--contract",
            "/Volumes/Fixture must not be read/contract.json",
            "assert-write",
            "--target",
            "/Volumes/Fixture must not be read/target",
        ],
        [
            "--contract",
            "/Volumes/Fixture must not be read/contract.json",
            "observe",
            "--state-file",
            "/internal/state.json",
            "--incident-dir",
            "/internal/incidents",
        ],
    ]

    for argv in commands:
        assert guard.main(argv) == 3
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "retired-no-active-probe"
        assert payload["healthy"] is False
        assert payload["writesAllowed"] is False
        assert payload["failSafeActive"] is True
        assert payload["storageTouched"] is False


def test_inspect_admits_only_healthy_pinned_probe(tmp_path: Path) -> None:
    guard = load_guard_module()
    contract = guard.normalize_contract(fixture_contract(tmp_path / "contract.json"))
    healthy = guard.validate_fixture_probe(
        fixture_probe(tmp_path / "healthy.json", healthy=True),
        contract,
    )
    assert healthy["writesAllowed"] is True

    dismounted = guard.validate_fixture_probe(
        fixture_probe(tmp_path / "dismounted.json", healthy=False),
        contract,
    )
    assert dismounted["state"] == "dismounted"
    assert dismounted["writesAllowed"] is False
    assert dismounted["failSafeActive"] is True


def test_observe_persists_detection_and_recovery_off_volume(tmp_path: Path) -> None:
    guard = load_guard_module()
    contract_path = fixture_contract(tmp_path / "contract.json")
    contract = guard.normalize_contract(contract_path)
    state = tmp_path / "internal" / "state.json"
    incidents = tmp_path / "internal" / "incidents"
    release = tmp_path / "stable-release"
    release.mkdir()

    first = guard.observe(
        probe=guard.validate_fixture_probe(
            fixture_probe(tmp_path / "healthy-1.json", healthy=True),
            contract,
        ),
        state_file=state,
        incident_dir=incidents,
        expected_release_path=release,
    )
    assert first["incident"] is None

    lost = guard.observe(
        probe=guard.validate_fixture_probe(
            fixture_probe(tmp_path / "lost.json", healthy=False),
            contract,
        ),
        state_file=state,
        incident_dir=incidents,
        expected_release_path=release,
    )
    lost_incident = json.loads(Path(lost["incident"]).read_text())
    assert lost_incident["eventType"] == "dismount-detected"
    assert lost_incident["failSafeBehavior"] == {
        "automaticFallbackPath": False,
        "automaticRemountAttempted": False,
        "serviceRestartAttempted": False,
        "writesAllowedWhileUnhealthy": False,
    }

    recovered = guard.observe(
        probe=guard.validate_fixture_probe(
            fixture_probe(tmp_path / "healthy-2.json", healthy=True),
            contract,
        ),
        state_file=state,
        incident_dir=incidents,
        expected_release_path=release,
    )
    recovery_incident = json.loads(Path(recovered["incident"]).read_text())
    assert recovery_incident["eventType"] == "dismount-recovery"
    assert recovery_incident["affectedIssue"] == "dismounted"
    assert recovery_incident["transition"] == "recovered"
    assert recovery_incident["dismountDetected"] is False
    assert recovery_incident["recoveryVerified"] is True
    # The fixture intentionally declares a synthetic OWC device distinct from
    # the temp filesystem, so target-device verification remains false here.
    assert recovery_incident["sameTargetReleaseVerified"] is False
    assert json.loads(state.read_text())["activeDismount"] is False


def test_observer_rejects_state_or_incident_paths_on_guarded_mount(tmp_path: Path) -> None:
    guard = load_guard_module()
    mount = tmp_path / "fixture-owc"
    mount.mkdir()
    contract_path = fixture_contract(tmp_path / "contract.json", mount=str(mount))
    contract = guard.normalize_contract(contract_path)
    probe_path = tmp_path / "probe.json"
    probe = json.loads(fixture_probe(probe_path, healthy=False).read_text())
    probe["expected"]["mountPoint"] = str(mount)
    try:
        guard.observe(
            probe=probe,
            state_file=mount / "state.json",
            incident_dir=mount / "incidents",
            expected_release_path=None,
        )
    except guard.GuardError as exc:
        assert "must be off OWC" in str(exc)
    else:
        raise AssertionError("observer accepted state on the guarded mount")


def test_fixture_probe_cannot_bypass_runtime_lock_or_drive_recovery(tmp_path: Path) -> None:
    guard = load_guard_module()
    contract_path = fixture_contract(tmp_path / "contract.json")
    contract = guard.normalize_contract(contract_path)
    state_path = Path(contract["statePath"])
    incidents = Path(contract["incidentDirectory"])
    stalled = guard.probe_stalled(
        contract,
        started_at=guard.utc_now(),
        timeout_seconds=10,
        probe_id="active-stall",
    )
    guard.observe(
        probe=stalled,
        state_file=state_path,
        incident_dir=incidents,
        expected_release_path=None,
    )
    before_state = state_path.read_bytes()
    before_incidents = sorted(incidents.iterdir())
    forged_path = fixture_probe(tmp_path / "forged-healthy.json", healthy=True)
    forged = json.loads(forged_path.read_text())
    forged["observed"]["protectedPaths"] = [
        {
            **row,
            "exists": True,
            "onExpectedDevice": True,
            "underExpectedMount": True,
        }
        for row in contract["protectedPaths"]
    ]
    write_json(forged_path, forged)
    lock_fd = guard.acquire_probe_lock(contract)
    assert lock_fd is not None
    try:
        attempts = [
            run_helper(contract_path, forged_path, "inspect"),
            run_helper(
                contract_path,
                forged_path,
                "assert-write",
                "--target",
                "/fixture/workspace/output.dat",
            ),
            run_helper(
                contract_path,
                forged_path,
                "observe",
                "--state-file",
                str(state_path),
                "--incident-dir",
                str(incidents),
            ),
        ]
    finally:
        os.close(lock_fd)
    assert all(result.returncode == 2 for result in attempts)
    assert all("error:" in result.stderr for result in attempts)
    assert all('"accepted": true' not in result.stdout.lower() for result in attempts)
    assert state_path.read_bytes() == before_state
    assert sorted(incidents.iterdir()) == before_incidents


def test_dismount_persists_when_bare_mount_has_no_trusted_device(
    tmp_path: Path,
) -> None:
    guard = load_guard_module()
    bare_mount = tmp_path / "Volumes" / "Fixture OWC"
    bare_mount.mkdir(parents=True)
    internal = tmp_path / "internal"
    probe = {
        "kind": guard.KIND,
        "capturedAt": guard.utc_now(),
        "expected": {"mountPoint": str(bare_mount), "volumeUuid": UUID},
        "observed": {
            "mountDevice": None,
            "volume": {"available": False},
            "protectedPaths": [],
        },
        "state": "dismounted",
        "healthy": False,
        "writesAllowed": False,
        "failSafeActive": True,
    }
    result = guard.observe(
        probe=probe,
        state_file=internal / "state.json",
        incident_dir=internal / "incidents",
        expected_release_path=None,
    )
    assert result["incident"] is not None
    persisted = json.loads((internal / "state.json").read_text())
    assert persisted["activeDismount"] is True
    assert persisted["activeIssue"] == "dismounted"


def test_unhealthy_probe_still_fences_an_owc_backed_evidence_path(tmp_path: Path) -> None:
    guard = load_guard_module()
    guarded_mount = tmp_path / "guarded"
    guarded_mount.mkdir()
    probe = {
        "expected": {"mountPoint": "/Volumes/Fixture OWC"},
        "observed": {"mountDevice": guarded_mount.stat().st_dev},
        "healthy": False,
    }
    with mock.patch.object(Path, "stat", return_value=guarded_mount.stat()):
        try:
            guard.assert_off_volume_path(tmp_path / "state.json", probe)
        except guard.GuardError as exc:
            assert "OWC device" in str(exc)
        else:
            raise AssertionError("unhealthy OWC-backed evidence path was accepted")


def test_launchagent_runs_only_internal_helper_contract_state_and_logs() -> None:
    with PLIST.open("rb") as handle:
        payload = plistlib.load(handle)
    arguments = payload["ProgramArguments"]
    assert arguments[:3] == [
        str(OPERATOR.require_path('paths.node_binary')),
        str(OPERATOR.require_path('paths.volume_guard_launcher')),
        str(OPERATOR.require_path('paths.volume_guard_helper')),
    ]
    rendered = json.dumps(payload, sort_keys=True)
    assert (str(OPERATOR.require_path('paths.data_root'))) not in rendered
    assert str(OPERATOR.require_path('paths.volume_guard_state')) in rendered
    assert payload["EnvironmentVariables"] == {"NODE_OPTIONS": "", "OPENCLAW_INTERNAL_PYTHON": str(OPERATOR.require_path("paths.python_binary"))}
    assert payload["Disabled"] is True
    assert "StartInterval" not in payload
    assert payload["ThrottleInterval"] == 15
    assert payload["ExitTimeOut"] == 5
    assert payload["AbandonProcessGroup"] is False


def test_probe_stall_timeout_precedes_launch_throttle() -> None:
    guard = load_guard_module()
    with PLIST.open("rb") as handle:
        payload = plistlib.load(handle)
    assert 0 < guard.PROBE_STALL_TIMEOUT_SECONDS < payload["ThrottleInterval"]


def test_persistent_probe_has_no_storage_admin_command_path() -> None:
    syntax = ast.parse(HELPER.read_text(encoding="utf-8"))
    imported_modules = {
        alias.name.split(".")[0]
        for node in ast.walk(syntax)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (
            node.names
            if isinstance(node, ast.Import)
            else [ast.alias(node.module or "")]
        )
    }
    string_literals = {
        node.value.lower()
        for node in ast.walk(syntax)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "subprocess" not in imported_modules
    assert not any(
        command in literal
        for literal in string_literals
        for command in ("diskutil", "smartctl", "system_profiler")
    )


def test_getattrlist_uuid_buffer_parses_exact_volume_uuid() -> None:
    guard = load_guard_module()
    raw = (20).to_bytes(4, sys.byteorder) + uuid.UUID(UUID).bytes
    assert guard.uuid_from_getattrlist_buffer(raw) == UUID


def test_getattrlist_uuid_buffer_rejects_invalid_lengths() -> None:
    guard = load_guard_module()
    for raw in (
        b"truncated",
        (19).to_bytes(4, sys.byteorder) + uuid.UUID(UUID).bytes,
        (21).to_bytes(4, sys.byteorder) + uuid.UUID(UUID).bytes,
    ):
        try:
            guard.uuid_from_getattrlist_buffer(raw)
        except guard.GuardError:
            pass
        else:  # pragma: no cover - the assertion explains the required failure.
            raise AssertionError("invalid attribute buffer was accepted")


def test_probe_timeout_rejects_nonfinite_contract_and_override_values(
    tmp_path: Path,
) -> None:
    guard = load_guard_module()
    for index, value in enumerate((float("nan"), float("inf"), float("-inf"))):
        contract_path = fixture_contract(tmp_path / f"contract-{index}.json")
        payload = json.loads(contract_path.read_text())
        payload["livenessProbe"]["timeoutSeconds"] = value
        write_json(contract_path, payload)
        try:
            guard.normalize_contract(contract_path)
        except guard.GuardError as exc:
            assert "liveness probe contract is invalid" in str(exc)
        else:
            raise AssertionError(f"non-finite contract timeout was accepted: {value!r}")

    direct_contract = {"livenessProbe": {"timeoutSeconds": 10}}
    for value in (float("nan"), float("inf"), float("-inf")):
        with mock.patch.object(
            guard.os,
            "fork",
            side_effect=AssertionError("invalid timeout reached fork"),
        ):
            try:
                guard.capture_probe_supervised(
                    direct_contract,
                    on_stall=lambda _probe: None,
                    timeout_seconds=value,
                )
            except guard.GuardError as exc:
                assert "finite and positive" in str(exc)
            else:
                raise AssertionError(f"non-finite timeout override was accepted: {value!r}")


def test_capture_probe_uses_mounted_volume_metadata_and_fails_closed(tmp_path: Path) -> None:
    guard = load_guard_module()
    mount = tmp_path / "fixture-owc"
    workspace = mount / "OpenClaw" / "Workspace"
    runtime = mount / "OpenClaw" / "Runtime"
    workspace.mkdir(parents=True)
    runtime.mkdir(parents=True)
    contract = {
        "path": str(tmp_path / "contract.json"),
        "sha256": "fixture-contract-sha256",
        "mountPoint": str(mount),
        "volumeUuid": UUID,
        "livenessProbe": {
            "path": str(mount / ".openclaw-volume-guard-liveness-v1.bin"),
            "sizeBytes": len(SENTINEL),
            "sha256": SENTINEL_SHA256,
            "timeoutSeconds": 10,
        },
        "protectedPaths": [
            {"rootId": "workspace", "logicalPath": str(workspace)},
            {"rootId": "runtime", "logicalPath": str(runtime)},
        ],
    }

    with (
        mock.patch.object(guard.os.path, "ismount", return_value=True),
        mock.patch.object(guard, "read_volume_uuid", return_value=UUID),
        mock.patch.object(guard.os, "statvfs", return_value=SimpleNamespace(f_flag=0)),
        mock.patch.object(
            guard,
            "read_liveness_probe",
            return_value={
                "available": True,
                "device": mount.stat().st_dev,
                "probeMethod": "uncached-pread",
                "sha256": SENTINEL_SHA256,
                "sizeBytes": len(SENTINEL),
            },
        ),
    ):
        healthy = guard.capture_probe(contract)
    assert healthy["healthy"] is True
    assert healthy["observed"]["volume"] == {
        "available": True,
        "filesystemType": "apfs",
        "mountDevice": mount.stat().st_dev,
        "mountFlags": 0,
        "mountPoint": str(mount),
        "mounted": True,
        "ownersEnabled": True,
        "probeMethod": "getattrlist-statvfs",
        "readOnly": False,
        "volumeUuid": UUID,
    }
    assert healthy["observed"]["liveness"]["available"] is True

    with (
        mock.patch.object(guard.os.path, "ismount", return_value=True),
        mock.patch.object(guard, "read_volume_uuid", return_value=UUID),
        mock.patch.object(guard.os, "statvfs", return_value=SimpleNamespace(f_flag=0)),
        mock.patch.object(
            guard,
            "read_liveness_probe",
            side_effect=guard.GuardError("fixture read failed"),
        ),
    ):
        io_fault = guard.capture_probe(contract)
    assert io_fault["healthy"] is False
    assert io_fault["state"] == "io-fault"
    assert io_fault["writesAllowed"] is False
    assert io_fault["observed"]["mountDevice"] == mount.stat().st_dev

    with (
        mock.patch.object(guard.os.path, "ismount", return_value=True),
        mock.patch.object(
            guard,
            "read_volume_uuid",
            return_value="00000000-0000-0000-0000-000000000000",
        ),
        mock.patch.object(guard.os, "statvfs", return_value=SimpleNamespace(f_flag=0)),
        mock.patch.object(guard, "read_liveness_probe"),
    ):
        wrong_uuid = guard.capture_probe(contract)
    assert wrong_uuid["healthy"] is False
    assert wrong_uuid["state"] == "identity-mismatch"
    assert wrong_uuid["writesAllowed"] is False
    assert wrong_uuid["observed"]["mountDevice"] is None

    with (
        mock.patch.object(guard.os.path, "ismount", return_value=True),
        mock.patch.object(guard, "read_volume_uuid", return_value=UUID),
        mock.patch.object(
            guard.os,
            "statvfs",
            return_value=SimpleNamespace(f_flag=guard.os.ST_RDONLY),
        ),
        mock.patch.object(guard, "read_liveness_probe"),
    ):
        read_only = guard.capture_probe(contract)
    assert read_only["healthy"] is False
    assert read_only["state"] == "identity-mismatch"
    assert read_only["writesAllowed"] is False

    with mock.patch.object(guard.os.path, "ismount", return_value=False):
        not_mounted = guard.capture_probe(contract)
    assert not_mounted["healthy"] is False
    assert not_mounted["state"] == "dismounted"
    assert not_mounted["observed"]["volume"]["reason"] == "not-a-mounted-filesystem"

    with (
        mock.patch.object(
            guard,
            "volume_metadata",
            return_value={
                "available": False,
                "mounted": None,
                "mountPoint": str(mount),
                "error": "OSError",
                "errno": 5,
            },
        ),
        mock.patch.object(
            guard,
            "current_mount_device",
            side_effect=OSError(5, "fixture final mount query failed"),
        ),
    ):
        initial_mount_error = guard.capture_probe(contract)
    assert initial_mount_error["state"] == "io-fault"
    assert initial_mount_error["observed"]["mountExists"] is None

    with (
        mock.patch.object(guard.os.path, "ismount", return_value=True),
        mock.patch.object(guard, "read_volume_uuid", return_value=UUID),
        mock.patch.object(guard.os, "statvfs", return_value=SimpleNamespace(f_flag=0)),
        mock.patch.object(
            guard,
            "current_mount_device",
            side_effect=[mount.stat().st_dev, mount.stat().st_dev, OSError(5, "final")],
        ),
        mock.patch.object(
            guard,
            "read_liveness_probe",
            return_value={"available": True},
        ),
    ):
        final_mount_error = guard.capture_probe(contract)
    assert final_mount_error["state"] == "io-fault"
    assert final_mount_error["observed"]["finalMountError"]["errno"] == 5

    with (
        mock.patch.object(guard.os.path, "ismount", return_value=True),
        mock.patch.object(
            guard,
            "read_volume_uuid",
            side_effect=OSError(5, "fixture metadata failure"),
        ),
    ):
        metadata_error = guard.capture_probe(contract)
    assert metadata_error["healthy"] is False
    assert metadata_error["state"] == "io-fault"
    assert metadata_error["observed"]["volume"]["errno"] == 5
    assert metadata_error["observed"]["mountDevice"] is None
    nested_device = metadata_error["observed"]["volume"]["mountDevice"]
    with mock.patch.object(
        Path,
        "stat",
        return_value=SimpleNamespace(st_dev=nested_device),
    ):
        try:
            guard.assert_off_volume_path(tmp_path / "internal" / "state.json", metadata_error)
        except guard.GuardError as exc:
            assert "OWC device" in str(exc)
        else:
            raise AssertionError("mounted metadata fault bypassed off-volume fencing")
    event_probe = json.loads(json.dumps(metadata_error))
    event_probe["observed"]["volume"]["mountDevice"] = nested_device + 1
    observed = guard.observe(
        probe=event_probe,
        state_file=tmp_path / "internal" / "state.json",
        incident_dir=tmp_path / "internal" / "incidents",
        expected_release_path=None,
    )
    assert observed["result"] == "blocked-fail-safe"
    assert observed["incident"] is not None
    incident = json.loads(Path(observed["incident"]).read_text())
    assert incident["eventType"] == "io-fault-detected"
    assert incident["storageFaultDetected"] is True

    outside = tmp_path / "outside-route"
    outside.mkdir()
    route_mismatch_contract = {
        **contract,
        "protectedPaths": [
            {"rootId": "outside", "logicalPath": str(outside)},
        ],
    }
    with (
        mock.patch.object(guard.os.path, "ismount", return_value=True),
        mock.patch.object(guard, "read_volume_uuid", return_value=UUID),
        mock.patch.object(guard.os, "statvfs", return_value=SimpleNamespace(f_flag=0)),
        mock.patch.object(
            guard,
            "read_liveness_probe",
            return_value={
                "available": True,
                "device": mount.stat().st_dev,
                "probeMethod": "uncached-pread",
                "sha256": SENTINEL_SHA256,
                "sizeBytes": len(SENTINEL),
            },
        ),
    ):
        route_mismatch = guard.capture_probe(route_mismatch_contract)
    assert route_mismatch["healthy"] is False
    assert route_mismatch["state"] == "route-mismatch"
    assert route_mismatch["writesAllowed"] is False


def test_filesystem_type_uses_darwin_statfs_metadata(tmp_path: Path) -> None:
    guard = load_guard_module()
    assert guard.filesystem_type(tmp_path) == "apfs"


def test_volume_metadata_rejects_mount_device_change(tmp_path: Path) -> None:
    guard = load_guard_module()
    mount = tmp_path / "fixture-owc"
    mount.mkdir()
    first_device = mount.stat().st_dev
    with (
        mock.patch.object(
            guard,
            "current_mount_device",
            side_effect=[first_device, first_device + 1],
        ),
        mock.patch.object(guard, "read_volume_uuid", return_value=UUID),
        mock.patch.object(guard.os, "statvfs", return_value=SimpleNamespace(f_flag=0)),
    ):
        metadata = guard.volume_metadata(mount)
    assert metadata["available"] is False
    assert metadata["reason"] == "mount-identity-changed-during-probe"


def test_volume_metadata_does_not_claim_dismount_on_mount_query_error(
    tmp_path: Path,
) -> None:
    guard = load_guard_module()
    mount = tmp_path / "fixture-owc"
    mount.mkdir()
    with mock.patch.object(
        guard,
        "current_mount_device",
        side_effect=OSError(5, "fixture mount query failure"),
    ):
        metadata = guard.volume_metadata(mount)
    assert metadata["available"] is False
    assert metadata["mounted"] is None
    assert metadata["errno"] == 5

    with (
        mock.patch.object(
            guard,
            "current_mount_device",
            side_effect=[mount.stat().st_dev, OSError(5, "fixture recheck failure")],
        ),
        mock.patch.object(
            guard,
            "read_volume_uuid",
            side_effect=OSError(5, "fixture metadata failure"),
        ),
    ):
        recheck_error = guard.volume_metadata(mount)
    assert recheck_error["available"] is False
    assert recheck_error["mounted"] is None
    assert recheck_error["mountRecheckErrno"] == 5


def test_liveness_probe_uses_uncached_positioned_read_and_pinned_content(tmp_path: Path) -> None:
    guard = load_guard_module()
    mount = tmp_path / "fixture-owc"
    mount.mkdir()
    sentinel = mount / "liveness.bin"
    sentinel.write_bytes(SENTINEL)
    contract = {
        "mountPoint": str(mount),
        "livenessProbe": {
            "path": str(sentinel),
            "sizeBytes": len(SENTINEL),
            "sha256": SENTINEL_SHA256,
            "timeoutSeconds": 10,
        },
    }
    with mock.patch.object(guard.fcntl, "fcntl", return_value=0) as cache_control:
        result = guard.read_liveness_probe(contract, expected_device=sentinel.stat().st_dev)
    assert result["available"] is True
    assert result["probeMethod"] == "uncached-pread"
    assert result["sha256"] == SENTINEL_SHA256
    assert cache_control.call_args_list == [
        mock.call(mock.ANY, guard.F_RDAHEAD, 0),
        mock.call(mock.ANY, guard.F_NOCACHE, 1),
    ]

    contract["livenessProbe"]["sha256"] = "0" * 64
    with mock.patch.object(guard.fcntl, "fcntl", return_value=0):
        try:
            guard.read_liveness_probe(contract, expected_device=sentinel.stat().st_dev)
        except guard.GuardError as exc:
            assert "hash mismatch" in str(exc)
        else:
            raise AssertionError("liveness sentinel hash mismatch was accepted")


def test_process_supervisor_persists_stall_kills_worker_and_never_accepts_late_result(
    tmp_path: Path,
) -> None:
    guard = load_guard_module()
    state = tmp_path / "internal" / "state.json"
    incidents = tmp_path / "internal" / "incidents"
    state.parent.mkdir()
    contract = {
        "path": str(tmp_path / "contract.json"),
        "sha256": "fixture-contract-sha256",
        "mountPoint": "/Volumes/Fixture OWC",
        "volumeUuid": UUID,
        "statePath": str(state),
        "incidentDirectory": str(incidents),
        "lockPath": str(tmp_path / "internal" / "probe.lock"),
        "livenessProbe": {
            "path": "/Volumes/Fixture OWC/liveness.bin",
            "sizeBytes": len(SENTINEL),
            "sha256": SENTINEL_SHA256,
            "timeoutSeconds": 0.01,
        },
        "protectedPaths": [],
    }

    def blocked_capture(_contract):
        time.sleep(60)
        return {
            "kind": guard.KIND,
            "capturedAt": guard.utc_now(),
            "expected": {
                "mountPoint": contract["mountPoint"],
                "volumeUuid": UUID,
            },
            "observed": {
                "mountDevice": 999999,
                "mountStable": True,
                "volume": {"available": True},
                "liveness": {"available": True},
                "protectedPaths": [],
            },
            "state": "healthy",
            "healthy": True,
            "writesAllowed": True,
            "failSafeActive": False,
        }

    def persist_stall(probe):
        result = guard.observe(
            probe=probe,
            state_file=state,
            incident_dir=incidents,
            expected_release_path=None,
        )
        persisted = json.loads(state.read_text())
        incident = json.loads(Path(result["incident"]).read_text())
        assert persisted["activeIssue"] == "probe-stalled"
        assert persisted["activeDismount"] is False
        assert incident["eventType"] == "probe-stall-detected"
        assert incident["affectedIssue"] == "probe-stalled"
        assert incident["transition"] == "detected"
        assert incident["stallDetected"] is True
        assert incident["dismountDetected"] is False
        assert probe["observed"]["mountDevice"] is None

    started = time.monotonic()
    final_probe, stalled = guard.capture_probe_supervised(
        contract,
        on_stall=persist_stall,
        capture=blocked_capture,
        timeout_seconds=0.01,
    )
    elapsed = time.monotonic() - started
    assert stalled is True
    assert elapsed < 2
    assert final_probe["state"] == "probe-stalled"
    assert final_probe["healthy"] is False
    assert final_probe["writesAllowed"] is False
    assert final_probe["observed"]["supervision"]["timedOut"] is True
    assert final_probe["observed"]["supervision"]["workerReaped"] is True

    repeated = guard.observe(
        probe=final_probe,
        state_file=state,
        incident_dir=incidents,
        expected_release_path=None,
    )
    assert repeated["incident"] is None
    assert json.loads(state.read_text())["activeIssue"] == "probe-stalled"


def test_process_supervisor_persists_completed_result_before_releasing_lock(
    tmp_path: Path,
) -> None:
    guard = load_guard_module()
    internal = tmp_path / "internal"
    internal.mkdir()
    contract = {
        "path": str(tmp_path / "contract.json"),
        "sha256": "fixture-contract-sha256",
        "mountPoint": "/Volumes/Fixture OWC",
        "volumeUuid": UUID,
        "statePath": str(internal / "state.json"),
        "incidentDirectory": str(internal / "incidents"),
        "lockPath": str(internal / "probe.lock"),
        "livenessProbe": {"timeoutSeconds": 1},
        "protectedPaths": [],
    }
    healthy = {
        "kind": guard.KIND,
        "capturedAt": guard.utc_now(),
        "expected": {"mountPoint": contract["mountPoint"], "volumeUuid": UUID},
        "observed": {"volume": {}, "liveness": {}, "protectedPaths": []},
        "state": "healthy",
        "healthy": True,
        "writesAllowed": True,
        "failSafeActive": False,
    }
    callback_saw_lock = False

    def persist_while_locked(_probe):
        nonlocal callback_saw_lock
        contender = guard.acquire_probe_lock(contract)
        callback_saw_lock = contender is None
        if contender is not None:
            os.close(contender)

    probe, timed_out = guard.capture_probe_supervised(
        contract,
        on_stall=lambda _probe: None,
        on_result=persist_while_locked,
        capture=lambda _contract: healthy,
        timeout_seconds=1,
    )
    assert timed_out is False
    assert probe["healthy"] is True
    assert callback_saw_lock is True
    reusable = guard.acquire_probe_lock(contract)
    assert reusable is not None
    try:
        marker = guard.read_probe_lock_marker(reusable)
    finally:
        os.close(reusable)
    assert marker["kind"] == guard.LOCK_MARKER_KIND
    assert marker["resultCommitted"] is True
    assert marker["probeId"] == probe["observed"]["supervision"]["probeId"]


def test_result_persistence_failure_invalidates_previous_healthy_admission(
    tmp_path: Path,
) -> None:
    guard = load_guard_module()
    contract_path = fixture_contract(tmp_path / "contract.json")
    contract = guard.normalize_contract(contract_path)
    previous_probe_id = "previous-healthy-probe"
    previous_probe = {
        "kind": guard.KIND,
        "capturedAt": guard.utc_now(),
        "contract": {"path": contract["path"], "sha256": contract["sha256"]},
        "expected": {
            "mountPoint": contract["mountPoint"],
            "volumeUuid": contract["volumeUuid"],
        },
        "observed": {
            "mountDevice": 777,
            "volume": {"available": True, "mounted": True},
            "liveness": {"available": True},
            "protectedPaths": [
                {
                    **row,
                    "exists": True,
                    "onExpectedDevice": True,
                    "underExpectedMount": True,
                }
                for row in contract["protectedPaths"]
            ],
            "supervision": {
                "probeId": previous_probe_id,
                "timedOut": False,
                "workerStillRunning": False,
                "workerReaped": True,
            },
        },
        "state": "healthy",
        "healthy": True,
        "writesAllowed": True,
        "failSafeActive": False,
    }
    write_json(
        Path(contract["statePath"]),
        {
            "kind": guard.STATE_KIND,
            "updatedAt": guard.utc_now(),
            "activeIssue": None,
            "activeDismount": False,
            "lastProbe": previous_probe,
            "lastIncident": None,
        },
    )
    commit_probe_marker(guard, contract, previous_probe_id)
    assert guard.admission_probe(contract)["healthy"] is True

    completed_unhealthy = guard.blocked_probe(
        contract,
        state="io-fault",
        reason="fixture-completed-io-fault",
    )

    def persistence_failure(_probe):
        raise OSError("fixture state persistence failure")

    try:
        guard.capture_probe_supervised(
            contract,
            on_stall=lambda _probe: None,
            on_result=persistence_failure,
            capture=lambda _contract: completed_unhealthy,
            timeout_seconds=1,
        )
    except OSError as exc:
        assert "fixture state persistence failure" in str(exc)
    else:
        raise AssertionError("supervisor ignored result persistence failure")

    rejected = guard.admission_probe(contract)
    assert rejected["healthy"] is False
    assert rejected["state"] == "observer-state-uncommitted"
    marker_fd = guard.acquire_probe_lock(contract)
    assert marker_fd is not None
    try:
        marker = guard.read_probe_lock_marker(marker_fd)
    finally:
        os.close(marker_fd)
    assert marker["resultCommitted"] is False
    assert marker["probeId"] != previous_probe_id


def test_process_supervisor_returns_bounded_if_killed_child_remains_unreaped(
    tmp_path: Path,
) -> None:
    guard = load_guard_module()
    internal = tmp_path / "internal"
    internal.mkdir()
    pid_file = internal / "worker.pid"
    contract = {
        "path": str(tmp_path / "contract.json"),
        "sha256": "fixture-contract-sha256",
        "mountPoint": "/Volumes/Fixture OWC",
        "volumeUuid": UUID,
        "statePath": str(internal / "state.json"),
        "incidentDirectory": str(internal / "incidents"),
        "lockPath": str(internal / "probe.lock"),
        "livenessProbe": {"timeoutSeconds": 0.01},
        "protectedPaths": [],
    }

    def blocked_capture(_contract):
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(60)
        raise AssertionError("blocked worker unexpectedly returned")

    started = time.monotonic()
    with mock.patch.object(guard.os, "kill", return_value=None):
        probe, timed_out = guard.capture_probe_supervised(
            contract,
            on_stall=lambda _probe: None,
            capture=blocked_capture,
            timeout_seconds=0.01,
        )
    assert time.monotonic() - started < 1
    assert timed_out is True
    assert probe["observed"]["supervision"]["workerReaped"] is False
    assert guard.acquire_probe_lock(contract) is None

    worker_pid = int(pid_file.read_text(encoding="utf-8"))
    os.kill(worker_pid, 9)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        reaped, _ = guard._waitpid_nohang(worker_pid)
        if reaped:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("fixture worker was not reaped")
    reusable = guard.acquire_probe_lock(contract)
    assert reusable is not None
    os.close(reusable)


def test_admission_reads_fresh_hash_bound_state_without_volume_probe(tmp_path: Path) -> None:
    guard = load_guard_module()
    contract_path = fixture_contract(tmp_path / "contract.json")
    contract = guard.normalize_contract(contract_path)
    state_path = Path(contract["statePath"])
    healthy_probe = {
        "kind": guard.KIND,
        "capturedAt": guard.utc_now(),
        "contract": {"path": contract["path"], "sha256": contract["sha256"]},
        "expected": {
            "mountPoint": contract["mountPoint"],
            "volumeUuid": contract["volumeUuid"],
        },
        "observed": {
            "mountDevice": 777,
            "volume": {"available": True, "mounted": True},
            "liveness": {"available": True},
            "protectedPaths": [
                {
                    **row,
                    "exists": True,
                    "onExpectedDevice": True,
                    "underExpectedMount": True,
                }
                for row in contract["protectedPaths"]
            ],
            "supervision": {
                "probeId": "fresh-healthy-probe",
                "timedOut": False,
                "workerStillRunning": False,
                "workerReaped": True,
            },
        },
        "state": "healthy",
        "healthy": True,
        "writesAllowed": True,
        "failSafeActive": False,
    }
    write_json(
        state_path,
        {
            "kind": guard.STATE_KIND,
            "updatedAt": guard.utc_now(),
            "activeIssue": None,
            "activeDismount": False,
            "lastProbe": healthy_probe,
            "lastIncident": None,
        },
    )
    commit_probe_marker(guard, contract, "fresh-healthy-probe")

    with mock.patch.object(
        guard,
        "capture_probe",
        side_effect=AssertionError("admission must not touch the volume"),
    ):
        admitted = guard.admission_probe(contract)
    assert admitted["healthy"] is True
    assert admitted["observed"]["admission"]["statePath"] == str(state_path)


def test_admission_rejects_stale_state_and_busy_probe_lock(tmp_path: Path) -> None:
    guard = load_guard_module()
    contract_path = fixture_contract(tmp_path / "contract.json")
    contract = guard.normalize_contract(contract_path)
    stale = {
        "kind": guard.STATE_KIND,
        "updatedAt": (
            datetime.now(timezone.utc)
            - timedelta(seconds=guard.ADMISSION_STATE_MAX_AGE_SECONDS + 5)
        ).isoformat().replace("+00:00", "Z"),
        "activeIssue": None,
        "lastProbe": {},
    }
    write_json(Path(contract["statePath"]), stale)
    rejected = guard.admission_probe(contract)
    assert rejected["healthy"] is False
    assert rejected["state"] == "observer-state-stale"

    lock_fd = guard.acquire_probe_lock(contract)
    assert lock_fd is not None
    try:
        busy = guard.admission_probe(contract)
    finally:
        os.close(lock_fd)
    assert busy["healthy"] is False
    assert busy["state"] == "probe-in-progress"


def test_assert_write_uses_verified_route_evidence_without_statting_target(
    tmp_path: Path,
) -> None:
    guard = load_guard_module()
    contract_path = fixture_contract(tmp_path / "contract.json")
    contract = guard.normalize_contract(contract_path)
    probe = {
        "healthy": True,
        "writesAllowed": True,
        "observed": {
            "protectedPaths": [
                {
                    **contract["protectedPaths"][0],
                    "exists": True,
                    "onExpectedDevice": True,
                    "underExpectedMount": True,
                }
            ]
        },
    }
    nonexistent = Path("/fixture/workspace/new/deep/output.json")
    with mock.patch.object(Path, "stat", side_effect=AssertionError("no target stat")):
        accepted = guard.assert_write_target(nonexistent, probe, contract)
    assert accepted["accepted"] is True
    assert accepted["protectedRootId"] == "workspace"
    outside = guard.assert_write_target(Path("/fixture/other/output.json"), probe, contract)
    assert outside["accepted"] is False
    assert outside["reason"] == "target_outside_protected_roots"


def test_probe_stall_is_idempotent_and_can_transition_to_dismount(tmp_path: Path) -> None:
    guard = load_guard_module()
    state = tmp_path / "internal" / "state.json"
    incidents = tmp_path / "internal" / "incidents"
    contract = {
        "path": str(tmp_path / "contract.json"),
        "sha256": "fixture-contract-sha256",
        "mountPoint": "/Volumes/Fixture OWC",
        "volumeUuid": UUID,
        "protectedPaths": [],
    }
    healthy_path = fixture_probe(tmp_path / "healthy-before.json", healthy=True)
    healthy_before = json.loads(healthy_path.read_text())
    healthy_before["observed"]["supervision"] = {
        "probeId": "healthy-before",
        "timedOut": False,
        "workerReaped": True,
        "workerStillRunning": False,
    }
    baseline = guard.observe(
        probe=healthy_before,
        state_file=state,
        incident_dir=incidents,
        expected_release_path=None,
    )
    assert baseline["incident"] is None

    stalled = guard.probe_stalled(
        contract,
        started_at="2099-01-01T00:00:00Z",
        timeout_seconds=10,
    )
    first = guard.observe(
        probe=stalled,
        state_file=state,
        incident_dir=incidents,
        expected_release_path=None,
    )
    second = guard.observe(
        probe=stalled,
        state_file=state,
        incident_dir=incidents,
        expected_release_path=None,
    )
    assert first["incident"] is not None
    assert second["incident"] is None

    dismounted_path = fixture_probe(tmp_path / "dismounted.json", healthy=False)
    dismounted = json.loads(dismounted_path.read_text())
    dismounted["observed"]["mountDevice"] = None
    dismounted["observed"]["volume"] = {
        "available": False,
        "mounted": False,
        "reason": "not-a-mounted-filesystem",
    }
    transitioned = guard.observe(
        probe=dismounted,
        state_file=state,
        incident_dir=incidents,
        expected_release_path=None,
    )
    incident = json.loads(Path(transitioned["incident"]).read_text())
    assert incident["eventType"] == "dismount-detected"
    persisted = json.loads(state.read_text())
    assert persisted["activeIssue"] == "dismounted"
    assert persisted["activeDismount"] is True

    healthy_after = json.loads(fixture_probe(tmp_path / "healthy-after.json", healthy=True).read_text())
    healthy_after["observed"]["supervision"] = {
        "probeId": "healthy-after",
        "timedOut": False,
        "workerReaped": True,
        "workerStillRunning": False,
    }
    recovered = guard.observe(
        probe=healthy_after,
        state_file=state,
        incident_dir=incidents,
        expected_release_path=None,
    )
    recovery = json.loads(Path(recovered["incident"]).read_text())
    assert recovery["eventType"] == "dismount-recovery"
    assert recovery["previousIssue"] == "dismounted"
    assert recovery["activeIssue"] is None
    assert recovery["previousProbeId"] is None
    assert recovery["currentProbeId"] == "healthy-after"
    assert recovery["transition"] == "recovered"
    assert recovery["dismountDetected"] is False


def test_route_mismatch_is_fail_closed_but_not_mislabeled_as_storage_fault(
    tmp_path: Path,
) -> None:
    guard = load_guard_module()
    probe_path = fixture_probe(
        tmp_path / "route-mismatch.json",
        healthy=False,
        state="route-mismatch",
    )
    probe = json.loads(probe_path.read_text())
    result = guard.observe(
        probe=probe,
        state_file=tmp_path / "internal" / "state.json",
        incident_dir=tmp_path / "internal" / "incidents",
        expected_release_path=None,
    )
    incident = json.loads(Path(result["incident"]).read_text())
    assert incident["eventType"] == "route-mismatch-detected"
    assert incident["storageFaultDetected"] is False


def test_legacy_observed_probe_transitions_to_new_probe_shape(tmp_path: Path) -> None:
    guard = load_guard_module()
    contract_path = fixture_contract(tmp_path / "contract.json")
    contract = guard.normalize_contract(contract_path)
    state = tmp_path / "internal" / "state.json"
    incidents = tmp_path / "internal" / "incidents"
    legacy_path = fixture_probe(tmp_path / "legacy.json", healthy=True)
    legacy = json.loads(legacy_path.read_text())
    legacy["observed"]["diskutil"] = legacy["observed"].pop("volume")
    first = guard.observe(
        probe=legacy,
        state_file=state,
        incident_dir=incidents,
        expected_release_path=None,
    )
    assert first["incident"] is None

    lost = guard.observe(
        probe=guard.validate_fixture_probe(
            fixture_probe(tmp_path / "new-lost.json", healthy=False),
            contract,
        ),
        state_file=state,
        incident_dir=incidents,
        expected_release_path=None,
    )
    incident = json.loads(Path(lost["incident"]).read_text())
    assert "diskutil" in incident["previousProbe"]["observed"]
    assert "volume" in incident["currentProbe"]["observed"]
