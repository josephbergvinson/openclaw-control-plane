from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib.util
import inspect
import io
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "openclaw_runtime_activate.py"
PREDECESSOR_COMMIT = "1" * 40
CANDIDATE_COMMIT = "2" * 40
SENSITIVE_TEST_PROFILE_IDS = (
    "openai:owner-one@example.invalid",
    "openai:owner-two@example.invalid",
    "openai:owner-three@example.invalid",
)
LEGACY_OPENAI_AUTH_ORDER_COUNT = 2
LEGACY_OPENAI_AUTH_ORDER_SHA256 = (
    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
)


def approval_report(
    *, semantic_sha256: str = "a" * 64, raw_cas_sha256: str = "b" * 64
) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "databasePresent": True,
        "rowPresent": True,
        "rowCount": 1,
        "currentKeyOnly": True,
        "legacySourceAbsent": True,
        "doctorClaimAbsent": True,
        "schemaValid": True,
        "projectionsValid": True,
        "expectedSocketPathMatched": True,
        "tokenPresent": True,
        "agentCount": 1,
        "allowlistCount": 64,
        "semanticSha256": semantic_sha256,
        "rawCasSha256": raw_cas_sha256,
        "authProfileStateReadable": True,
        "authProfileStoreReadable": True,
        "openAIProfileOrderCount": activate_module.expected_auth_order_count(),
        "openAIProfileOrderSha256": activate_module.expected_auth_order_sha256(),
        "openAIProfileOrderCredentialsUsable": True,
    }


def load_module():
    spec = importlib.util.spec_from_file_location("openclaw_activate_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


activate_module = load_module()


def chmod_tree(root: Path, writable: bool) -> None:
    for directory, directories, files in os.walk(root, topdown=False):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o644 if writable else (0o555 if path.name == "openclaw.mjs" else 0o444))
        for name in directories:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o755 if writable else 0o555)
    root.chmod(0o755 if writable else 0o555)


def make_release(
    root: Path,
    name: str,
    commit: str,
    *,
    include_candidate_native_entrypoints: bool = True,
) -> Path:
    release = root / name
    (release / "dist").mkdir(parents=True)
    (release / "dist" / "build-info.json").write_text(json.dumps({"commit": commit}) + "\n")
    (release / "dist" / "index.js").write_text("export {};\n")
    (release / "openclaw.mjs").write_text("#!/usr/bin/env node\n")
    (release / "package.json").write_text(json.dumps({"name": "openclaw", "version": "2026.8.1-beta.3"}) + "\n")
    (release / "NOTICE").write_text(f"release {name}\n")
    if include_candidate_native_entrypoints:
        chrome_extension = (
            release / "dist" / "extensions" / "browser" / "chrome-extension"
        )
        chrome_extension.mkdir(parents=True)
        (chrome_extension / "manifest.json").write_text(
            json.dumps({"manifest_version": 3, "name": "OpenClaw Browser Relay"})
            + "\n"
        )
        (chrome_extension / "background.js").write_text("export {};\n")
    ai_package = release / "packages" / "ai"
    ai_dist = ai_package / "dist"
    ai_internal = ai_dist / "internal"
    ai_internal.mkdir(parents=True)
    (ai_package / "package.json").write_text(
        json.dumps(
            {
                "name": "@openclaw/ai",
                "exports": {
                    ".": {
                        "import": "./dist/index.mjs",
                        "default": "./dist/index.mjs",
                    },
                    "./internal/openai-responses-payload-policy": {
                        "import": "./dist/internal/openai-responses-payload-policy.mjs",
                        "default": "./dist/internal/openai-responses-payload-policy.mjs",
                    },
                },
            }
        )
        + "\n"
    )
    (ai_dist / "index.mjs").write_text("export {};\n")
    (ai_internal / "openai-responses-payload-policy.mjs").write_text(
        "export {};\n"
    )
    ai_link = release / "node_modules" / "@openclaw" / "ai"
    ai_link.parent.mkdir(parents=True)
    ai_link.symlink_to("../../packages/ai")
    if include_candidate_native_entrypoints:
        # Match the current sealed SDK/Doctor leaves. Snapshot tests supply the
        # process boundary through FakeBackend; do not fake migration success.
        inspector = release / "dist" / "plugin-sdk" / "exec-approvals-runtime.js"
        inspector.parent.mkdir(parents=True)
        inspector.write_text(
            "export function inspectExecApprovalsState() { return "
            f"{json.dumps(approval_report(), separators=(',', ':'))}; }}\n"
        )
        (inspector.parent / "security-runtime.js").write_text(
            "export function resolvePreferredOpenClawTmpDir() { "
            'throw new Error("synthetic security boundary requires the test backend"); }\n'
        )
        doctor = release / "dist" / "commands" / "doctor.worker.js"
        doctor.parent.mkdir(parents=True)
        doctor.write_text(
            'throw new Error("synthetic Doctor boundary requires the test backend");\n'
        )
    for plugin_id in activate_module.required_bundled_extensions():
        extension = release / "dist" / "extensions" / plugin_id
        extension.mkdir(parents=True)
        (extension / "index.js").write_text(f"export const id = {plugin_id!r};\n")
        (extension / "openclaw.plugin.json").write_text(json.dumps({"id": plugin_id}) + "\n")
        (extension / "package.json").write_text(json.dumps({"name": f"@openclaw/{plugin_id}", "version": "2026.8.1-beta.3"}) + "\n")
    chmod_tree(release, False)
    return release


@dataclass
class Fixture:
    root: Path
    paths: object
    predecessor: Path
    candidate: Path
    seal: Path
    snapshot: Path
    legacy_exec_approvals_path: Path
    external_marker: Path
    config_bytes: bytes


class FakeBackend:
    def __init__(self, fixture: Fixture, *, gateway_failure: bool = False,
                 node_bootstrap_failure: bool = False,
                 node_verification_failure: bool = False,
                 discord_verification_failure: bool = False,
                 approval_reports: list[dict[str, object]] | None = None):
        self.fixture = fixture
        self.gateway_failure = gateway_failure
        self.node_bootstrap_failure = node_bootstrap_failure
        self.node_verification_failure = node_verification_failure
        self.discord_verification_failure = discord_verification_failure
        self.command_evidence: list[dict[str, object]] = []
        self.gateway_boots = 0
        self.node_boots = 0
        self.verifications = 0
        self.stopped_checks = 0
        self.inspection_calls = 0
        self.events: list[str] = []
        self.approval_reports = approval_reports or [approval_report()]
        self.install_records = {"whatsapp": {"source": "npm", "version": "0"}}
        self._bootstrap_bindings: dict[str, dict[str, object]] = {}
        self.receipt_bindings: list[tuple[str, bool]] = []

    def _bootstrap_binding(self) -> dict[str, object]:
        selector_info = os.lstat(self.fixture.paths.current_link)
        release = self.fixture.paths.current_link.resolve(strict=True)
        release_info = os.lstat(release)
        return {
            "release": str(release),
            "releaseDevice": release_info.st_dev,
            "releaseInode": release_info.st_ino,
            "selectorDevice": selector_info.st_dev,
            "selectorInode": selector_info.st_ino,
            "startedAtUs": 1,
        }

    def _bootstrap_evidence(self, label: str, returncode: int):
        plist = (
            self.fixture.paths.gateway_plist
            if label == activate_module.OPERATOR.require_string('runtime.gateway_label')
            else self.fixture.paths.node_plist
        )
        prefix = "gateway" if label == activate_module.OPERATOR.require_string('runtime.gateway_label') else "node"
        argv = (
            "/bin/launchctl",
            "bootstrap",
            f"gui/{self.fixture.paths.operator_uid}",
            str(plist),
        )
        return activate_module.CommandResult(
            argv,
            returncode,
            b"",
            b"",
            1,
            False,
        ).evidence(f"{prefix}_bootstrap")

    def assert_gateway_and_node_stopped(self):
        self.stopped_checks += 1
        self.events.append("stopped")
        self.command_evidence.extend(
            activate_module.expected_stopped_command_evidence(self.fixture.paths)
        )

    def verify_screen_capture_continuity(self):
        return None

    def inspect_exec_approvals(self, release):
        self.events.append("inspect")
        index = min(self.inspection_calls, len(self.approval_reports) - 1)
        self.inspection_calls += 1
        assert release == self.fixture.candidate
        self.command_evidence.append(
            activate_module.CommandResult(
                ("/fake/node", "inspect"), 0, b"{}", b"", 1, False
            ).evidence("exec_approvals_inspection")
        )
        return activate_module.validate_exec_approvals_inspection(
            json.loads(json.dumps(self.approval_reports[index]))
        )

    def verify_candidate_discord(self, release, config_path):
        before = json.loads(json.dumps(self.install_records))
        if self.discord_verification_failure or "discord" in self.install_records:
            raise activate_module.ActivationError("private Discord verification diagnostic")
        assert json.loads(json.dumps(self.install_records)) == before
        assert config_path.read_bytes() == self.fixture.config_bytes
        return {
            "installRecordAbsent": True,
            "installedIds": sorted(self.install_records),
            "configSha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "origin": "bundled",
            "source": str(release / "dist" / "extensions" / "discord" / "index.js"),
        }

    def bootstrap_gateway_once(self):
        self.gateway_boots += 1
        if self.gateway_failure:
            self.command_evidence.append(
                self._bootstrap_evidence(activate_module.OPERATOR.require_string('runtime.gateway_label'), 1)
            )
            raise activate_module.ActivationError("private gateway diagnostic")
        self.command_evidence.append(
            self._bootstrap_evidence(activate_module.OPERATOR.require_string('runtime.gateway_label'), 0)
        )
        self._bootstrap_bindings[activate_module.OPERATOR.require_string('runtime.gateway_label')] = (
            self._bootstrap_binding()
        )

    def bootstrap_node_once(self):
        self.node_boots += 1
        if self.node_bootstrap_failure:
            self.command_evidence.append(
                self._bootstrap_evidence(activate_module.OPERATOR.require_string('runtime.node_label'), 1)
            )
            raise activate_module.ActivationError("private node bootstrap diagnostic")
        self.command_evidence.append(
            self._bootstrap_evidence(activate_module.OPERATOR.require_string('runtime.node_label'), 0)
        )
        self._bootstrap_bindings[activate_module.OPERATOR.require_string('runtime.node_label')] = (
            self._bootstrap_binding()
        )

    def bootstrap_bindings(self):
        return {
            label: dict(binding)
            for label, binding in self._bootstrap_bindings.items()
        }

    def bind_bootstrap_receipt(self, label, binding, loaded=None):
        self._bootstrap_bindings[label] = dict(binding)
        self.receipt_bindings.append((label, loaded is not None))

    def verify(self, release, device, inode):
        self.verifications += 1
        info = os.lstat(release)
        assert (info.st_dev, info.st_ino) == (device, inode)
        binding = self._bootstrap_bindings[activate_module.OPERATOR.require_string('runtime.gateway_label')]
        return {
            "loaded": {"release": str(release), "releaseDevice": device,
                       "releaseInode": inode, "pid": 100, "runs": 1,
                       "startToken": "darwin:2:0",
                       "bootstrapBinding": dict(binding)},
            "health": {"healthz": {"accepted": True, "statusCode": 200}, "readyz": {"accepted": True, "statusCode": 200}},
        }

    def verify_node(self, release, device, inode):
        self.verifications += 1
        info = os.lstat(release)
        assert (info.st_dev, info.st_ino) == (device, inode)
        if self.node_verification_failure:
            raise activate_module.ActivationError("private node diagnostic")
        binding = self._bootstrap_bindings[activate_module.OPERATOR.require_string('runtime.node_label')]
        return {"release": str(release), "releaseDevice": device,
                "releaseInode": inode, "pid": 200, "runs": 1,
                "startToken": "darwin:2:0",
                "bootstrapBinding": dict(binding)}


class StoppedRestoreBackend(FakeBackend):
    def _print(self, label):
        paths = self.fixture.paths
        argv = ("/bin/launchctl", "print", f"gui/{paths.operator_uid}/{label}")
        stderr = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for user gui: '
            f"{paths.operator_uid}\n"
        ).encode()
        result = activate_module.CommandResult(argv, 113, b"", stderr, 1, False)
        self.command_evidence.append(result.evidence(f"{label}_launchctl_print"))
        return result

    def _service_missing(self, result, label):
        paths = self.fixture.paths
        expected = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for user gui: '
            f"{paths.operator_uid}\n"
        ).encode()
        return result.returncode == 113 and result.stdout == b"" and result.stderr == expected


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch) -> Fixture:
    # The real Darwin volume query is an environmental boundary. These owner
    # tests also run on Linux; remount tests vary this UUID explicitly below.
    if hasattr(activate_module, "_history_volume_guard"):
        monkeypatch.setattr(activate_module._history_volume_guard(), "read_volume_uuid",
                            lambda _: "11111111-2222-3333-4444-555555555555")
    releases = tmp_path / "runtime" / "Releases"
    releases.mkdir(parents=True)
    predecessor = make_release(
        releases,
        "openclaw-predecessor",
        PREDECESSOR_COMMIT,
        include_candidate_native_entrypoints=False,
    )
    candidate = make_release(releases, "openclaw-candidate", CANDIDATE_COMMIT)
    runtime = tmp_path / "runtime"
    current = runtime / "current"
    current.symlink_to(predecessor)
    gateway_working_directory = current.parent.parent
    cli = tmp_path / "cli"
    package_link = cli / "lib" / "node_modules" / "openclaw"
    package_link.parent.mkdir(parents=True)
    package_link.symlink_to(current)
    bin_link = cli / "bin" / "openclaw"
    bin_link.parent.mkdir(parents=True)
    bin_link.symlink_to(current / "openclaw.mjs")
    node_root = cli / "tools" / "node-v24.15.0"
    node = node_root / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o555)
    node_alias = cli / "tools" / "node"
    node_alias.symlink_to(node_root)

    state = tmp_path / "owc-state"
    state.mkdir()
    legacy_exec_approvals_path = tmp_path / "legacy-state" / "exec-approvals.json"
    sessions = state / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    reservations = sessions / "sessions.json.quarantine-reservations.json"
    reservations.write_text(json.dumps({"storePath": str(sessions / "sessions.json")}) + "\n")
    reservations.chmod(0o600)
    from scripts.operator_contract import OperatorContract
    configured = {section: activate_module.OPERATOR.get(section, {}) for section in ("paths", "runtime", "identifiers")}
    configured["paths"].update({"host_home": str(tmp_path), "session_reservations": str(reservations), "session_store": str(sessions / "sessions.json"), "legacy_exec_approvals": str(legacy_exec_approvals_path)})
    configured["runtime"].update({"session_reservations_mode": "required", "session_reservations_sha256": hashlib.sha256(reservations.read_bytes()).hexdigest()})
    monkeypatch.setattr(activate_module, "OPERATOR", OperatorContract(configured))
    config = {
        "auth": {},
        "skills": {"entries": {"goplaces": {"enabled": False}}},
        "plugins": {
            "entries": {plugin_id: {"enabled": True} for plugin_id in activate_module.required_bundled_extensions()},
            "load": {"paths": []},
        },
    }
    config_path = state / "openclaw.json"
    config_path.write_text(json.dumps(config, sort_keys=True) + "\n")
    config_path.chmod(0o600)
    external_marker = state / "unchanged-external-package.marker"
    external_marker.write_text("preserve exact external package\n")
    external_runtime_assets = tmp_path / "external-runtime-assets"
    external_runtime_assets.mkdir()
    external_runtime_sentinel = external_runtime_assets / "sentinel.txt"
    external_runtime_sentinel.write_text("preserve external target bytes and inode\n")
    (state / "external-runtime-assets").symlink_to(
        external_runtime_assets,
        target_is_directory=True,
    )

    gateway_plist = tmp_path / "gateway.plist"
    node_plist = tmp_path / "node.plist"
    common_environment = {
        "OPENCLAW_STATE_DIR": str(state),
        "OPENCLAW_SUPERVISOR_MODE": "external",
        "OPENCLAW_SERVICE_REPAIR_POLICY": "external",
    }
    gateway_plist.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": common_environment,
                "ProgramArguments": [
                    str(node),
                    str(package_link / "dist" / "index.js"),
                    "gateway",
                    "--port",
                    str(activate_module.runtime_gateway_port()),
                ],
                "WorkingDirectory": str(gateway_working_directory),
                "RunAtLoad": True,
                "KeepAlive": True,
            }
        )
    )
    node_plist.write_bytes(plistlib.dumps({"EnvironmentVariables": common_environment, "ProgramArguments": [str(node), str(package_link / "dist" / "index.js"), "node", "run"]}))
    gateway_plist.chmod(0o600)
    node_plist.chmod(0o600)
    subprocess.run(
        [
            "/usr/bin/xattr",
            "-w",
            "com.openclaw.snapshot-test",
            "preserve exact gateway metadata",
            str(gateway_plist),
        ],
        check=True,
        capture_output=True,
    )
    paths = activate_module.ActivationPaths(
        releases, current, package_link, bin_link, gateway_plist, node_plist,
        node, node_alias, state, state, runtime / "activation.lock",
        runtime / "activation-result.json", os.getuid(), 1.0, 1.0, 0.01,
    )
    seal = tmp_path / "candidate-seal.json"
    activate_module.create_candidate_seal(paths, candidate, CANDIDATE_COMMIT, seal)
    archive = tmp_path / "stopped-snapshot.tar"
    subprocess.run(
        [
            "/usr/bin/tar", "--xattrs", "--acls", "--fflags", "-cpf",
            str(archive), "-C", "/",
            str(state).lstrip("/"),
            str(gateway_plist).lstrip("/"),
            str(node_plist).lstrip("/"),
            str(current).lstrip("/"),
        ],
        check=True,
        capture_output=True,
    )
    archive.chmod(0o400)
    g6 = tmp_path / "stopped-quiescence.json"
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    stopped_evidence = activate_module.expected_stopped_command_evidence(paths)
    g6.write_text(json.dumps({
        "schemaVersion": 1,
        "capturedAt": "2026-08-27T00:00:00Z",
        "archivePath": str(archive),
        "archiveSha256": archive_sha256,
        "preCapture": stopped_evidence,
        "postCapture": stopped_evidence,
    }, indent=2, sort_keys=True) + "\n")
    g6.chmod(0o400)
    predecessor_record = activate_module.validate_release(
        paths,
        predecessor,
        PREDECESSOR_COMMIT,
        require_candidate_native_entrypoints=False,
    )
    invariants = activate_module.validate_recovered_invariants(paths)
    plists = activate_module.validate_plists(paths)
    snapshot = tmp_path / "stopped-snapshot.json"
    snapshot.write_text(json.dumps({
        "schemaVersion": 2,
        "tarPath": str(archive),
        "tarSha256": archive_sha256,
        "inventoryCount": 4,
        "g6QuiescencePath": str(g6),
        "g6QuiescenceSha256": hashlib.sha256(g6.read_bytes()).hexdigest(),
        "predecessorStateDir": str(state),
        "candidateStateDir": str(state),
        "currentTarget": str(predecessor),
        "predecessor": predecessor_record,
        "recoveredInvariants": invariants,
        "execApprovalsInspector": activate_module.exec_approvals_inspector_binding(
            activate_module.validate_candidate_seal(
                paths, candidate, CANDIDATE_COMMIT, seal
            )
        ),
        "execApprovals": approval_report(),
        "gatewayPlistSha256": plists["gateway"]["sha256"],
        "nodePlistSha256": plists["node"]["sha256"],
    }, indent=2, sort_keys=True) + "\n")
    snapshot.chmod(0o400)
    item = Fixture(
        tmp_path,
        paths,
        predecessor,
        candidate,
        seal,
        snapshot,
        legacy_exec_approvals_path,
        external_marker,
        config_path.read_bytes(),
    )
    yield item
    chmod_tree(predecessor, True)
    chmod_tree(candidate, True)


def run(fixture: Fixture, backend: FakeBackend | None = None):
    return activate_module.activate(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal,
        fixture.snapshot, backend or FakeBackend(fixture),
    )


@pytest.mark.parametrize(
    "drift",
    [
        "python-supervisor",
        "wrong-working-directory",
        "wrong-port",
        "wrong-entrypoint",
        "extra-argument",
        "program-override",
        "run-at-load-disabled",
        "keep-alive-disabled",
    ],
)
def test_gateway_plist_requires_exact_direct_launchd_node_contract(
    fixture: Fixture,
    drift: str,
):
    value = plistlib.loads(fixture.paths.gateway_plist.read_bytes())
    if drift == "python-supervisor":
        value["ProgramArguments"] = [
            "/usr/bin/python3",
            str(fixture.paths.current_link),
            str(fixture.paths.node),
            "gateway",
        ]
    elif drift == "wrong-working-directory":
        value["WorkingDirectory"] = str(fixture.candidate)
    elif drift == "wrong-port":
        value["ProgramArguments"][-1] = "18790"
    elif drift == "wrong-entrypoint":
        value["ProgramArguments"][1] = str(
            fixture.paths.current_link / "openclaw.mjs"
        )
    elif drift == "extra-argument":
        value["ProgramArguments"].append("--unexpected")
    elif drift == "program-override":
        value["Program"] = str(fixture.paths.node)
    elif drift == "run-at-load-disabled":
        value["RunAtLoad"] = False
    else:
        value["KeepAlive"] = False
    fixture.paths.gateway_plist.write_bytes(plistlib.dumps(value))

    with pytest.raises(
        activate_module.ActivationError,
        match="gateway plist selector contract drift",
    ):
        activate_module.validate_plists(fixture.paths)


def sealed_candidate(fixture: Fixture) -> dict[str, object]:
    return activate_module.validate_candidate_seal(
        fixture.paths,
        fixture.candidate,
        CANDIDATE_COMMIT,
        fixture.seal,
    )


def restore_result_path(fixture: Fixture) -> Path:
    digest = hashlib.sha256(fixture.snapshot.read_bytes()).hexdigest()[:16]
    return fixture.paths.result.parent / f"activation-restore-result-{digest}.json"


def restore(fixture: Fixture, backend: StoppedRestoreBackend):
    return activate_module.restore_failed_activation(
        fixture.paths,
        fixture.candidate,
        CANDIDATE_COMMIT,
        fixture.seal,
        fixture.snapshot,
        restore_result_path(fixture),
        backend,
    )


def test_seal_binds_only_required_bundled_extensions(fixture: Fixture):
    seal = json.loads(fixture.seal.read_text())
    assert set(seal["bundledExtensions"]) == {"discord", "codex"}
    discord = seal["bundledExtensions"]["discord"]
    assert discord["path"] == str(fixture.candidate / "dist" / "extensions" / "discord")
    assert discord["entrypointSha256"] == hashlib.sha256(
        (fixture.candidate / "dist" / "extensions" / "discord" / "index.js").read_bytes()
    ).hexdigest()
    assert stat.S_IMODE(os.lstat(fixture.seal).st_mode) == 0o400


def test_seal_accepts_complete_ai_runtime_export_closure(fixture: Fixture):
    candidate = sealed_candidate(fixture)

    assert candidate["path"] == str(fixture.candidate)
    assert (
        fixture.candidate
        / "node_modules"
        / "@openclaw"
        / "ai"
        / "dist"
        / "internal"
        / "openai-responses-payload-policy.mjs"
    ).resolve(strict=True).is_file()


@pytest.mark.parametrize("plugin_id", ["discord", "codex"])
@pytest.mark.parametrize("target", ["../../../node_modules", "../../.."])
def test_seal_preserves_in_release_bundled_dependency_links(fixture: Fixture, plugin_id: str, target: str):
    root = fixture.candidate / "dist" / "extensions" / plugin_id
    root.chmod(0o755)
    (root / "node_modules").symlink_to(target)
    root.chmod(0o555)
    seal_path = fixture.root / f"{plugin_id}-dependencies-seal.json"
    activate_module.create_candidate_seal(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, seal_path
    )
    activate_module.validate_candidate_seal(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, seal_path
    )
    # The shared target is still bound by the full-release inventory.
    dependency = fixture.candidate / "packages" / "ai" / "dist" / "index.mjs"
    dependency.chmod(0o644)
    dependency.write_text("export const changed = true;\n")
    dependency.chmod(0o444)
    with pytest.raises(activate_module.ActivationError, match="candidate seal surface drift"):
        activate_module.validate_candidate_seal(
            fixture.paths, fixture.candidate, CANDIDATE_COMMIT, seal_path
        )


def test_bundled_inventory_rejects_dependency_links_outside_release(fixture: Fixture):
    root = fixture.candidate / "dist" / "extensions" / "discord"
    root.chmod(0o755)
    (root / "node_modules").symlink_to(fixture.predecessor / "node_modules")
    root.chmod(0o555)
    with pytest.raises(activate_module.ActivationError, match="release symlink escapes"):
        activate_module.validate_bundled_extensions(fixture.candidate)


def test_bundled_inventory_rejects_missing_dependency_target(fixture: Fixture):
    root = fixture.candidate / "dist" / "extensions" / "discord"
    root.chmod(0o755)
    (root / "node_modules").symlink_to("../../../missing-dependencies")
    root.chmod(0o555)
    with pytest.raises(FileNotFoundError):
        activate_module.validate_bundled_extensions(fixture.candidate)


def test_seal_rejects_missing_ai_runtime_export_entry(fixture: Fixture):
    target = (
        fixture.candidate
        / "packages"
        / "ai"
        / "dist"
        / "internal"
        / "openai-responses-payload-policy.mjs"
    )
    target.parent.chmod(0o755)
    target.unlink()
    target.parent.chmod(0o555)

    with pytest.raises(
        activate_module.ActivationError,
        match="candidate AI runtime export .* is unavailable",
    ):
        activate_module.create_candidate_seal(
            fixture.paths,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.root / "missing-ai-runtime-seal.json",
        )


@pytest.mark.parametrize(
    ("export_name", "export_value", "missing_relative"),
    [
        (
            "./node-only",
            {"node": "./dist/node-only.mjs", "default": "./dist/index.mjs"},
            "dist/node-only.mjs",
        ),
        (
            "./nested",
            {
                "node": {
                    "import": "./dist/internal/nested-node.mjs",
                    "default": "./dist/index.mjs",
                },
                "default": "./dist/index.mjs",
            },
            "dist/internal/nested-node.mjs",
        ),
    ],
)
def test_seal_rejects_missing_conditional_ai_runtime_export_entry(
    fixture: Fixture,
    export_name: str,
    export_value: object,
    missing_relative: str,
):
    package_path = fixture.candidate / "packages" / "ai" / "package.json"
    package = json.loads(package_path.read_text())
    package["exports"][export_name] = export_value
    package_path.chmod(0o600)
    package_path.write_text(json.dumps(package) + "\n")
    package_path.chmod(0o444)

    with pytest.raises(
        activate_module.ActivationError,
        match=rf"candidate AI runtime export {re.escape(missing_relative)} is unavailable",
    ):
        activate_module.create_candidate_seal(
            fixture.paths,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.root / f"missing-{export_name.removeprefix('./')}-seal.json",
        )


def test_activation_accepts_absent_artifact_retention(fixture: Fixture):
    config = json.loads((fixture.paths.candidate_state_dir / "openclaw.json").read_text())
    assert "artifact-retention" not in config["plugins"]["entries"]
    assert not (fixture.candidate / "dist" / "extensions" / "artifact-retention").exists()

    result = run(fixture)

    assert result["outcome"] == "activated"
    assert result["candidate"]["bundledExtensions"].keys() == {"discord", "codex"}


@pytest.mark.parametrize(
    "relative",
    ["plugin-sdk/exec-approvals-runtime.js", "plugin-sdk/security-runtime.js", "commands/doctor.worker.js"],
)
def test_seal_requires_release_native_exec_approvals_inspector(
    fixture: Fixture, relative: str
):
    inspector = fixture.candidate / "dist" / relative
    inspector.parent.chmod(0o755)
    inspector.unlink()
    inspector.parent.chmod(0o555)

    with pytest.raises(
        activate_module.ActivationError,
        match="candidate entrypoint is unavailable",
    ):
        activate_module.create_candidate_seal(
            fixture.paths,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.root / "missing-inspector-seal.json",
        )


@pytest.mark.parametrize("filename", ["manifest.json", "background.js"])
def test_seal_requires_packaged_chrome_extension_file(
    fixture: Fixture,
    filename: str,
):
    required = (
        fixture.candidate
        / "dist"
        / "extensions"
        / "browser"
        / "chrome-extension"
        / filename
    )
    required.parent.chmod(0o755)
    required.unlink()
    required.parent.chmod(0o555)

    with pytest.raises(
        activate_module.ActivationError,
        match="candidate entrypoint is unavailable",
    ):
        activate_module.create_candidate_seal(
            fixture.paths,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.root / f"missing-chrome-extension-{filename}-seal.json",
        )


def test_seal_entrypoint_streams_release_files_larger_than_read_bound(
    monkeypatch, capsys, fixture: Fixture
):
    large = fixture.candidate / "large-sparse-asset.bin"
    fixture.candidate.chmod(0o755)
    with large.open("wb") as handle:
        handle.truncate(activate_module.MAX_FILE_BYTES + 1)
    large.chmod(0o444)
    fixture.candidate.chmod(0o555)
    fixture.seal.unlink()
    monkeypatch.setattr(activate_module, "live_paths", lambda: fixture.paths)

    exit_code = activate_module.main([
        "seal",
        "--candidate-release", str(fixture.candidate),
        "--source-commit", CANDIDATE_COMMIT,
        "--output", str(fixture.seal),
    ])

    receipt = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert receipt["releaseInventory"] == json.loads(fixture.seal.read_text())["candidate"]["releaseInventory"]
    assert receipt["releaseInventory"]["count"] == activate_module.normalized_tree_inventory(fixture.candidate)["count"]
    assert large.stat().st_size > activate_module.MAX_FILE_BYTES


def test_activation_preflight_streams_pinned_node_larger_than_read_bound(fixture: Fixture):
    fixture.paths.node.chmod(0o755)
    with fixture.paths.node.open("r+b") as handle:
        handle.truncate(activate_module.MAX_FILE_BYTES + 1)
    fixture.paths.node.chmod(0o555)

    result = run(fixture)

    assert fixture.paths.node.stat().st_size > activate_module.MAX_FILE_BYTES
    assert result["outcome"] == "activated"


def test_already_migrated_activation_boots_each_job_once_and_preserves_state(fixture: Fixture):
    backend = FakeBackend(fixture)
    marker = fixture.external_marker.read_bytes()
    result = run(fixture, backend)
    assert result["outcome"] == "activated"
    assert result["statesVisited"] == ["preflight", "apply", "verify", "terminal"]
    assert result["candidateAttemptCount"] == 1
    assert result["rollbackAttemptCount"] == 0
    assert result["firstBootAttempted"] is True
    assert result["restoreRequired"] is False
    assert backend.gateway_boots == 1
    assert backend.node_boots == 1
    assert os.readlink(fixture.paths.current_link) == str(fixture.candidate)
    assert fixture.external_marker.read_bytes() == marker
    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes
    assert result["verification"]["bundledExtensions"]["discord"] == result["candidate"]["bundledExtensions"]["discord"]
    assert result["verification"]["pluginSelection"]["origin"] == "bundled"
    assert result["verification"]["pluginSelection"]["installRecordAbsent"] is True
    assert result["verification"]["pluginSelection"]["installedIds"] == ["whatsapp"]
    approvals = result["verification"]["execApprovals"]
    assert approvals["authProfileStateReadable"] is True
    assert approvals["authProfileStoreReadable"] is True
    assert (
        approvals["openAIProfileOrderCount"]
        == activate_module.expected_auth_order_count()
    )
    assert (
        approvals["openAIProfileOrderSha256"]
        == activate_module.expected_auth_order_sha256()
    )
    assert approvals["openAIProfileOrderCredentialsUsable"] is True
    assert "openAIProfileOrder" not in approvals
    config = json.loads(
        (fixture.paths.candidate_state_dir / "openclaw.json").read_text()
    )
    assert "openai" not in config["auth"].get("order", {})
    assert not any(
        key.startswith("openai:") for key in config["auth"].get("profiles", {})
    )
    for path in (fixture.paths.result, fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)):
        info = os.lstat(path)
        assert stat.S_IMODE(info.st_mode) == 0o400
        assert info.st_nlink == 1
        payload = path.read_text()
        assert all(
            profile_id not in payload
            for profile_id in SENSITIVE_TEST_PROFILE_IDS
        )


def test_openai_auth_order_attestation_is_value_free_in_reports_and_receipts(
    fixture: Fixture,
):
    result = run(fixture)

    assert result["outcome"] == "activated"
    report = result["verification"]["execApprovals"]
    assert (
        report["openAIProfileOrderCount"]
        == activate_module.expected_auth_order_count()
    )
    assert (
        report["openAIProfileOrderSha256"]
        == activate_module.expected_auth_order_sha256()
    )
    assert "openAIProfileOrder" not in report
    surfaces = (
        json.dumps(report, sort_keys=True),
        fixture.snapshot.read_text(),
        fixture.paths.result.read_text(),
        (
            fixture.candidate
            / "dist"
            / "plugin-sdk"
            / "exec-approvals-runtime.js"
        ).read_text(),
    )
    assert all(
        profile_id not in surface
        for profile_id in SENSITIVE_TEST_PROFILE_IDS
        for surface in surfaces
    )


def test_expected_openai_auth_order_binding_is_value_free():
    assert activate_module.expected_auth_order_count() == 3
    assert activate_module.expected_auth_order_sha256() == (
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    )
    assert not hasattr(activate_module, "EXPECTED_OPENAI_AUTH_ORDER")


def test_activation_accepts_unrelated_install_records_without_mutation(fixture: Fixture):
    backend = FakeBackend(fixture)
    backend.install_records["owner-local-plugin"] = {
        "source": "npm",
        "version": "1.0.0",
    }
    before = json.loads(json.dumps(backend.install_records))

    result = run(fixture, backend)

    assert result["outcome"] == "activated"
    assert backend.install_records == before
    assert result["verification"]["pluginSelection"]["installedIds"] == [
        "owner-local-plugin",
        "whatsapp",
    ]


def test_terminal_receipt_retirement_moves_exact_pair_and_preserves_inodes(fixture: Fixture):
    result = run(fixture)
    assert result["outcome"] == "activated"
    fence_path = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    result_info = os.lstat(fixture.paths.result)
    fence_info = os.lstat(fence_path)
    archive_dir = fixture.paths.result.parent / "archive" / "prior-success"

    receipt = activate_module.retire_terminal_receipts(fixture.paths, archive_dir)

    assert not fixture.paths.result.exists()
    assert not fence_path.exists()
    archived_result = archive_dir / fixture.paths.result.name
    archived_fence = archive_dir / fence_path.name
    assert os.lstat(archived_result).st_ino == result_info.st_ino
    assert os.lstat(archived_fence).st_ino == fence_info.st_ino
    assert receipt["result"]["path"] == str(archived_result)
    assert receipt["startFence"]["path"] == str(archived_fence)
    retirement = archive_dir / activate_module.RETIREMENT_RECEIPT_NAME
    assert stat.S_IMODE(retirement.stat().st_mode) == 0o400


def simulate_release_remount(fixture, monkeypatch):
    original = os.lstat
    releases = {fixture.candidate, fixture.predecessor}
    current_device = original(fixture.candidate).st_dev + 4

    def remounted(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path) in releases:
            values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
            values["st_dev"] = current_device
            return SimpleNamespace(**values)
        return info

    monkeypatch.setattr(os, "lstat", remounted)
    if hasattr(activate_module, "_history_volume_guard"):
        guard = activate_module._history_volume_guard()
        monkeypatch.setattr(guard, "normalize_contract", lambda _: {
            "mountPoint": str(fixture.root), "volumeUuid": "11111111-2222-3333-4444-555555555555"})
        monkeypatch.setattr(guard, "volume_metadata", lambda _: {
            "available": True, "mounted": True, "mountDevice": current_device,
            "volumeUuid": "11111111-2222-3333-4444-555555555555"})
    return current_device


def test_terminal_receipt_retirement_revalidates_legacy_release_after_remount(fixture, monkeypatch):
    result = run(fixture)
    original = {path: path.read_bytes() for path in (
        fixture.paths.result, fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME),
        fixture.seal, fixture.snapshot)}
    current_device = simulate_release_remount(fixture, monkeypatch)
    archive = fixture.paths.result.parent / "archive" / "after-remount"

    retired = activate_module.retire_terminal_receipts(fixture.paths, archive)

    assert retired["outcome"] == "activated"
    identity = retired["releaseIdentities"]["selected"]
    assert identity["device"] == current_device != result["candidate"]["device"]
    assert identity["inode"] == result["candidate"]["inode"]
    assert identity["volumeUuid"] == "11111111-2222-3333-4444-555555555555"
    assert identity["recordSha256"] == activate_module.sha256_bytes(activate_module.canonical_json_bytes(result["candidate"]))
    for path, payload in original.items():
        assert (path if path in (fixture.seal, fixture.snapshot) else archive / path.name).read_bytes() == payload


@pytest.mark.parametrize("corruption", ["volume", "inode", "inventory", "seal", "snapshot", "missing-volume"])
def test_terminal_receipt_remount_rejects_unproven_identity_before_moving(fixture, monkeypatch, corruption):
    run(fixture)
    simulate_release_remount(fixture, monkeypatch)
    guard = activate_module._history_volume_guard()
    if corruption == "volume":
        monkeypatch.setattr(guard, "read_volume_uuid", lambda _: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")
    elif corruption == "missing-volume":
        monkeypatch.setattr(guard, "read_volume_uuid", lambda _: "")
    elif corruption == "inventory":
        notice = fixture.candidate / "NOTICE"
        notice.chmod(0o644); notice.write_text("changed bytes"); notice.chmod(0o444)
    elif corruption == "inode":
        prior = os.lstat
        def replaced_inode(path, *args, **kwargs):
            info = prior(path, *args, **kwargs)
            if Path(path) == fixture.candidate:
                values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
                values["st_ino"] += 1
                return SimpleNamespace(**values)
            return info
        monkeypatch.setattr(os, "lstat", replaced_inode)
    else:
        path = fixture.seal if corruption == "seal" else fixture.snapshot
        path.chmod(0o600); path.write_text(path.read_text() + " "); path.chmod(0o400)
    before = fixture.paths.result.read_bytes()
    fence = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    fence_before = fence.read_bytes()
    archive = fixture.paths.result.parent / "archive" / "unproven-remount"
    with pytest.raises(activate_module.ActivationError):
        activate_module.retire_terminal_receipts(fixture.paths, archive)
    assert not archive.exists()
    assert fixture.paths.result.read_bytes() == before and fence.read_bytes() == fence_before


def test_remount_does_not_relax_candidate_seal_mutation_admission(fixture, monkeypatch):
    simulate_release_remount(fixture, monkeypatch)
    with pytest.raises(activate_module.ActivationError, match="seal surface drift"):
        activate_module.validate_candidate_seal(fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal)


def test_retirement_rolls_back_moves_if_mount_changes_during_archival(fixture, monkeypatch):
    run(fixture)
    fence = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    before = {path: path.read_bytes() for path in (fixture.paths.result, fence)}
    original_rename = os.rename
    archive = fixture.paths.result.parent / "archive" / "racing-mount"
    def rename_and_remount(source, destination):
        original_rename(source, destination)
        if source == fixture.paths.result:
            simulate_release_remount(fixture, monkeypatch)
    monkeypatch.setattr(os, "rename", rename_and_remount)
    with pytest.raises(activate_module.ActivationError, match="changed during retirement"):
        activate_module.retire_terminal_receipts(fixture.paths, archive)
    assert all(path.read_bytes() == payload for path, payload in before.items())
    assert not (archive / activate_module.RETIREMENT_RECEIPT_NAME).exists()


def test_daily_retention_reaches_children_after_legacy_remount_retirement(fixture, monkeypatch):
    from scripts import openclaw_retention_cleanup_cron as cron
    from scripts import openclaw_runtime_activate as live_owner
    run(fixture)
    simulate_release_remount(fixture, monkeypatch)
    monkeypatch.setattr(live_owner, "live_paths", lambda: fixture.paths)
    calls = []
    def child(name, *args, **kwargs):
        calls.append(name)
        return cron.ChildResult(name, 0)
    monkeypatch.setattr(cron, "run_child", child)
    results = cron.run_steps(apply=True)
    assert {row.name for row in results} == {"host_storage", "runtime_releases", "runtime_promotions", "approval_a"}
    assert len(calls) == 4 and all(row.returncode == 0 for row in results)
    assert not fixture.paths.result.exists()


def _bind_public_retention_fixture(retention, fixture, monkeypatch):
    from scripts.operator_contract import OperatorContract
    configured = {name: retention.OPERATOR.get(name, {}) for name in ("paths", "runtime", "identifiers")}
    configured["paths"].update({
        "runtime_releases_root": str(fixture.paths.releases_root),
        "runtime_current_link": str(fixture.paths.current_link),
        "cli_root": str(fixture.root / "cli"), "state_root": str(fixture.paths.candidate_state_dir),
        "host_home": str(fixture.root / "home"), "activation_result": str(fixture.paths.result),
        "activation_lock": str(fixture.paths.lock), "runtime_promotions_root": str(fixture.root / "promotions"),
        "runtime_release_retention_artifacts": str(fixture.root / "retention-reports"),
    })
    monkeypatch.setattr(retention, "OPERATOR", OperatorContract(configured))


def test_release_retention_consumes_durable_retirement_identity(fixture, monkeypatch):
    from scripts import openclaw_runtime_release_retention as retention
    run(fixture)
    archive = fixture.paths.result.parent / "archive" / "retention-history"
    activate_module.retire_terminal_receipts(fixture.paths, archive)
    simulate_release_remount(fixture, monkeypatch)
    monkeypatch.setattr(retention, "effective_activation_result_path", lambda: fixture.paths.result)
    _bind_public_retention_fixture(retention, fixture, monkeypatch)
    monkeypatch.setattr(retention, "LAST_PROMOTION_OPERATION_CLASSIFICATIONS", [])
    assert retention.collect_unfinished_release_refs() == []
    assert retention.LAST_TERMINAL_ARCHIVE_NOTES == []
    path = archive / activate_module.RETIREMENT_RECEIPT_NAME
    receipt = json.loads(path.read_text())
    receipt["result"]["volumeUuid"] = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    path.chmod(0o600); path.write_text(json.dumps(receipt)); path.chmod(0o400)
    refs = retention.collect_unfinished_release_refs()
    assert {row.path for row in refs} == {fixture.candidate, fixture.predecessor}
    assert retention.LAST_TERMINAL_ARCHIVE_NOTES


class RetentionBackend(FakeBackend):
    def verify(self, release, device, inode):
        value = super().verify(release, device, inode)
        value["loaded"]["argumentsObservedExact"] = True
        return value


def classify_retired_fixture(fixture, monkeypatch):
    from scripts import openclaw_runtime_release_retention as retention
    from scripts import openclaw_runtime_activate as live_owner
    monkeypatch.setattr(live_owner, "live_paths", lambda: fixture.paths)
    _bind_public_retention_fixture(retention, fixture, monkeypatch)
    monkeypatch.setattr(retention.Path, "home", lambda: fixture.root / "home")
    # Environmental host observations only; use the real source classification,
    # locks, symlink references, immutable archive reader and report writer.
    monkeypatch.setattr(retention, "collect_launchagent_refs", lambda: [])
    monkeypatch.setattr(retention, "collect_process_refs", lambda **kwargs: [])
    rc, _, _, report_path = retention.run(apply=False, keep_latest=0, min_age_days=0)
    return rc, json.loads(report_path.read_text())


def test_retired_fixture_keeps_browser_dependency_validation_fail_closed(fixture, monkeypatch):
    run(fixture, RetentionBackend(fixture))
    retire_fixture_daily(fixture, monkeypatch)
    native_root = fixture.paths.candidate_state_dir / "browser" / "native-messaging"
    native_root.mkdir(parents=True, mode=0o700)
    launcher = native_root / "ai.openclaw.browser_bootstrap.invalid.sh"
    launcher.write_text(
        "#!/bin/sh\n# OpenClaw native messaging bootstrap v1\nexec /unverifiable\n"
    )
    launcher.chmod(0o700)
    rc, report = classify_retired_fixture(fixture, monkeypatch)
    assert rc == 1 and report["state"] == "classification_blocked"
    assert any("unverifiable owned browser native-host dependency" in row["error"]
               for row in report["errors"])
    assert all(path.exists() for path in (fixture.candidate, fixture.predecessor))


def retire_fixture_daily(fixture, monkeypatch):
    from scripts import openclaw_retention_cleanup_cron as cron
    from scripts import openclaw_runtime_activate as live_owner
    monkeypatch.setattr(live_owner, "live_paths", lambda: fixture.paths)
    receipt_path = cron.retire_completed_activation(apply=True)
    assert receipt_path is not None and not fixture.paths.result.exists()
    return Path(receipt_path)


def next_retention_fixture(fixture, name):
    candidate = make_release(fixture.paths.releases_root, name, CANDIDATE_COMMIT)
    seal = fixture.root / f"{name}-seal.json"
    activate_module.create_candidate_seal(fixture.paths, candidate, CANDIDATE_COMMIT, seal)
    snapshot = json.loads(fixture.snapshot.read_text())
    snapshot["currentTarget"] = str(fixture.candidate)
    snapshot["predecessor"] = activate_module.validate_release(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT,
        require_candidate_native_entrypoints=False)
    snapshot["execApprovalsInspector"] = activate_module.exec_approvals_inspector_binding(
        activate_module.validate_candidate_seal(fixture.paths, candidate, CANDIDATE_COMMIT, seal),
        snapshot["predecessor"])
    archive = fixture.root / f"{name}-snapshot.tar"
    subprocess.run([
        "/usr/bin/tar", "--xattrs", "--acls", "--fflags", "-cpf", str(archive), "-C", "/",
        *[str(path).lstrip("/") for path in (
            fixture.paths.predecessor_state_dir, fixture.paths.gateway_plist,
            fixture.paths.node_plist, fixture.paths.current_link)],
    ], check=True, capture_output=True)
    archive.chmod(0o400)
    quiescence = json.loads(Path(snapshot["g6QuiescencePath"]).read_text())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    quiescence.update(archivePath=str(archive), archiveSha256=digest)
    quiescence_path = fixture.root / f"{name}-quiescence.json"
    quiescence_path.write_text(json.dumps(quiescence))
    quiescence_path.chmod(0o400)
    snapshot.update(tarPath=str(archive), tarSha256=digest,
                    g6QuiescencePath=str(quiescence_path),
                    g6QuiescenceSha256=hashlib.sha256(quiescence_path.read_bytes()).hexdigest())
    snapshot_path = fixture.root / f"{name}-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot))
    snapshot_path.chmod(0o400)
    return replace(fixture, predecessor=fixture.candidate, candidate=candidate,
                        seal=seal, snapshot=snapshot_path)


def test_daily_retirement_classification_preserves_only_current_rollback(fixture, monkeypatch):
    run(fixture, RetentionBackend(fixture))
    historical = retire_fixture_daily(fixture, monkeypatch)
    successor = next_retention_fixture(fixture, "openclaw-next")
    candidate = successor.candidate
    try:
        assert run(successor, RetentionBackend(successor))["outcome"] == "activated"
        current = retire_fixture_daily(successor, monkeypatch)
        assert historical != current
        rc, report = classify_retired_fixture(successor, monkeypatch)
        assert rc == 0
        rows = {row["name"]: row for row in report["receipts"]}
        assert rows[candidate.name]["state"] == "skipped_protected"
        assert rows[fixture.candidate.name]["state"] == "skipped_protected"
        assert "activation-retirement:rollback" in rows[fixture.candidate.name]["protection_reasons"]
        assert rows[fixture.predecessor.name]["state"] == "would_remove"
        assert all(path.exists() for path in (candidate, fixture.candidate, fixture.predecessor))
    finally:
        chmod_tree(candidate, True)


@pytest.mark.parametrize("corruption", [
    "missing-receipt", "missing-result", "missing-fence", "tampered-result", "tampered-fence",
    "pending-fence", "receipt-mode", "result-mode", "rollback-volume",
    "selected-changed", "selected-identity", "rollback-identity", "ambiguous", "new-live-invalid",
])
def test_retired_current_binding_mismatch_blocks_normal_classification(fixture, monkeypatch, corruption):
    run(fixture, RetentionBackend(fixture))
    receipt_path = retire_fixture_daily(fixture, monkeypatch)
    generation = receipt_path.parent
    receipt = json.loads(receipt_path.read_text())
    if corruption == "missing-receipt":
        receipt_path.unlink()
    elif corruption == "missing-result":
        (generation / fixture.paths.result.name).unlink()
    elif corruption == "missing-fence":
        (generation / activate_module.START_CONSUMED_NAME).unlink()
    elif corruption == "pending-fence":
        fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME).write_text("{}")
    elif corruption == "receipt-mode":
        receipt_path.chmod(0o600)
    elif corruption == "result-mode":
        (generation / fixture.paths.result.name).chmod(0o600)
    elif corruption == "rollback-volume":
        receipt["releaseIdentities"]["rollback"]["volumeUuid"] = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        receipt_path.chmod(0o600)
        receipt_path.write_text(json.dumps(receipt))
        receipt_path.chmod(0o400)
    elif corruption in {"tampered-result", "tampered-fence"}:
        name = fixture.paths.result.name if corruption == "tampered-result" else activate_module.START_CONSUMED_NAME
        path = generation / name
        path.chmod(0o600); path.write_bytes(path.read_bytes() + b" "); path.chmod(0o400)
    elif corruption == "selected-changed":
        fixture.paths.current_link.unlink(); fixture.paths.current_link.symlink_to(fixture.predecessor)
    elif corruption in {"selected-identity", "rollback-identity"}:
        role = "selected" if corruption == "selected-identity" else "rollback"
        receipt["releaseIdentities"][role]["inode"] += 1
        receipt_path.chmod(0o600); receipt_path.write_text(json.dumps(receipt)); receipt_path.chmod(0o400)
    elif corruption == "ambiguous":
        other = generation.parent / "same-time-owner"
        other.mkdir(mode=0o700)
        (other / activate_module.RETIREMENT_RECEIPT_NAME).write_text(json.dumps(receipt))
        (other / activate_module.RETIREMENT_RECEIPT_NAME).chmod(0o400)
    elif corruption == "new-live-invalid":
        fixture.paths.result.write_text(json.dumps({"outcome": "pending"}))
        fixture.paths.result.chmod(0o400)
    rc, report = classify_retired_fixture(fixture, monkeypatch)
    assert rc == 1
    assert report["state"] == "classification_blocked"
    assert all(path.exists() for path in (fixture.candidate, fixture.predecessor))


def test_retired_current_rollback_survives_supported_volume_remount(fixture, monkeypatch):
    run(fixture, RetentionBackend(fixture))
    retire_fixture_daily(fixture, monkeypatch)
    simulate_release_remount(fixture, monkeypatch)
    rc, report = classify_retired_fixture(fixture, monkeypatch)
    assert rc == 0
    rows = {row["name"]: row for row in report["receipts"]}
    assert all(row["state"] == "skipped_protected" for row in rows.values())
    assert "activation-retirement:rollback" in rows[fixture.predecessor.name]["protection_reasons"]


def test_retired_restore_does_not_protect_failed_candidate_as_rollback(fixture, monkeypatch):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == "snapshot_restore_required"
    assert restore(fixture, StoppedRestoreBackend(fixture))["outcome"] == "restored"
    archive = fixture.paths.result.parent / "archive" / "restored-history"
    activate_module.retire_terminal_receipts(fixture.paths, archive)
    rc, report = classify_retired_fixture(fixture, monkeypatch)
    assert rc == 0
    rows = {row["name"]: row for row in report["receipts"]}
    assert rows[fixture.predecessor.name]["state"] == "skipped_protected"
    assert rows[fixture.candidate.name]["state"] == "would_remove"



@pytest.mark.parametrize("corruption", [None, "not-applied", "failed-hash", "snapshot", "node",
                                        "error", "outcome", "bootstrap", "fence", "recovery-ambiguous",
                                        "health", "loaded", "node-owner"])
def test_nested_retired_restore_preserves_selected_release_recovery(fixture, monkeypatch, corruption):
    run(fixture, RetentionBackend(fixture))
    retire_fixture_daily(fixture, monkeypatch)
    selected = next_retention_fixture(fixture, "openclaw-selected")
    failed = None
    try:
        assert run(selected, RetentionBackend(selected))["outcome"] == "activated"
        recovery_receipt_path = retire_fixture_daily(selected, monkeypatch)
        failed = next_retention_fixture(selected, "openclaw-failed")
        assert run(failed, FakeBackend(failed, gateway_failure=True))["outcome"] == "snapshot_restore_required"
        assert restore(failed, StoppedRestoreBackend(failed))["outcome"] == "restored"
        archive = failed.paths.result.parent / "archive" / "nested-restored"
        receipt = activate_module.retire_terminal_receipts(failed.paths, archive)
        if corruption == "recovery-ambiguous":
            duplicate = archive.parent / "ambiguous-selected-recovery"
            duplicate.mkdir(mode=0o700)
            duplicate_receipt = duplicate / activate_module.RETIREMENT_RECEIPT_NAME
            duplicate_receipt.write_bytes(recovery_receipt_path.read_bytes())
            duplicate_receipt.chmod(0o400)
        elif corruption is not None:
            path = Path(receipt["restoreResult"]["path"])
            value = json.loads(path.read_text())
            if corruption == "not-applied": value["restoreApplied"] = False
            elif corruption == "failed-hash": value["failedActivationResultSha256"] = "0" * 64
            elif corruption == "snapshot": value["snapshot"]["manifestSha256"] = "0" * 64
            elif corruption == "node": value["verification"]["node"] = None
            elif corruption == "error": value["error"] = "predecessor_boot_failed"
            elif corruption == "outcome": value["outcome"] = "restored_boot_failed"
            elif corruption == "bootstrap": value["bootstrapBindings"][activate_module.OPERATOR.require_string('runtime.gateway_label')]["selectorInode"] += 1
            elif corruption == "health": value["verification"]["gateway"]["health"]["readyz"]["accepted"] = False
            elif corruption == "loaded": value["verification"]["gateway"]["loaded"]["releaseInode"] += 1
            elif corruption == "node-owner": value["verification"]["node"]["releaseInode"] += 1
            elif corruption == "fence":
                path = archive / activate_module.START_CONSUMED_NAME
                value = json.loads(path.read_text())
                value["candidateSealSha256"] = "0" * 64
            path.chmod(0o600); path.write_text(json.dumps(value)); path.chmod(0o400)
            key = "startFence" if corruption == "fence" else "restoreResult"
            receipt[key]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            receipt_path = archive / activate_module.RETIREMENT_RECEIPT_NAME
            receipt_path.chmod(0o600); receipt_path.write_text(json.dumps(receipt)); receipt_path.chmod(0o400)
        rc, report = classify_retired_fixture(failed, monkeypatch)
        if corruption is not None:
            assert rc == 1 and report["state"] == "classification_blocked"
        else:
            assert rc == 0
            rows = {row["name"]: row for row in report["receipts"]}
            assert rows[selected.candidate.name]["state"] == "skipped_protected"
            assert rows[selected.predecessor.name]["state"] == "skipped_protected"
            assert "activation-retirement:rollback" in rows[selected.predecessor.name]["protection_reasons"]
            assert rows[fixture.predecessor.name]["state"] == "would_remove"
            assert rows[failed.candidate.name]["state"] == "would_remove"
        assert all(path.exists() for path in (fixture.predecessor, fixture.candidate, selected.candidate, failed.candidate))
    finally:
        chmod_tree(selected.candidate, True)
        if failed is not None: chmod_tree(failed.candidate, True)


def test_terminal_receipt_retirement_archives_historical_different_profile_binding(
    fixture: Fixture,
):
    result = run(fixture)
    manifest = json.loads(fixture.snapshot.read_text())
    manifest_approvals = manifest["execApprovals"]
    manifest_approvals["openAIProfileOrderCount"] = LEGACY_OPENAI_AUTH_ORDER_COUNT
    manifest_approvals["openAIProfileOrderSha256"] = (
        LEGACY_OPENAI_AUTH_ORDER_SHA256
    )
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)
    manifest_sha256 = hashlib.sha256(fixture.snapshot.read_bytes()).hexdigest()

    fence_path = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    fence = json.loads(fence_path.read_text())
    fence["snapshotManifestSha256"] = manifest_sha256
    fence_path.chmod(0o600)
    fence_path.write_text(json.dumps(fence, indent=2, sort_keys=True) + "\n")
    fence_path.chmod(0o400)

    result["snapshot"]["manifestSha256"] = manifest_sha256
    result["snapshot"]["execApprovals"] = dict(manifest_approvals)
    result["verification"]["execApprovals"] = dict(manifest_approvals)
    result["startConsumption"]["sha256"] = hashlib.sha256(
        fence_path.read_bytes()
    ).hexdigest()
    result_path = fixture.paths.result
    result_path.chmod(0o600)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result_path.chmod(0o400)
    archive_dir = result_path.parent / "archive" / "historical-different-profile"

    receipt = activate_module.retire_terminal_receipts(
        fixture.paths,
        archive_dir,
    )

    archived = json.loads((archive_dir / result_path.name).read_text())
    archived_approvals = archived["snapshot"]["execApprovals"]
    assert receipt["outcome"] == "activated"
    assert archived["snapshot"]["manifestSha256"] == manifest_sha256
    assert archived["startConsumption"]["sha256"] == hashlib.sha256(
        (archive_dir / fence_path.name).read_bytes()
    ).hexdigest()
    assert archived_approvals == manifest_approvals
    assert archived["verification"]["execApprovals"] == manifest_approvals
    assert archived_approvals["openAIProfileOrderCount"] == (
        LEGACY_OPENAI_AUTH_ORDER_COUNT
    )
    assert (
        archived_approvals["openAIProfileOrderSha256"]
        == LEGACY_OPENAI_AUTH_ORDER_SHA256
    )


def test_terminal_receipt_retirement_accepts_exact_bound_legacy_schema(fixture: Fixture):
    result = run(fixture)
    result_path = fixture.paths.result
    fence_path = result_path.with_name(activate_module.START_CONSUMED_NAME)
    candidate = result["candidate"]
    snapshot = result["snapshot"]
    bootstrap_path = fixture.root / "legacy-bootstrap.json"
    bootstrap_sha = "b" * 64
    legacy_fence = {
        "schemaVersion": 2,
        "candidatePath": candidate["path"],
        "candidateCommit": candidate["commit"],
        "candidateDevice": candidate["device"],
        "candidateInode": candidate["inode"],
        "candidateSealPath": candidate["sealPath"],
        "candidateSealSha256": candidate["sealSha256"],
        "snapshotManifestPath": snapshot["manifestPath"],
        "snapshotManifestSha256": snapshot["manifestSha256"],
        "bootstrapManifestPath": str(bootstrap_path),
        "bootstrapManifestSha256": bootstrap_sha,
        "consumedAt": "2026-08-27T01:00:00Z",
    }
    fence_path.chmod(0o600)
    fence_path.write_text(json.dumps(legacy_fence, indent=2, sort_keys=True) + "\n")
    fence_path.chmod(0o400)
    result["activationInputs"] = {
        "candidateSealPath": candidate["sealPath"],
        "day0SnapshotManifestPath": snapshot["manifestPath"],
        "externalPluginBootstrapManifestPath": str(bootstrap_path),
        "externalPluginBootstrapManifestSha256": bootstrap_sha,
    }
    result["startConsumption"] = {
        "path": str(fence_path),
        "sha256": hashlib.sha256(fence_path.read_bytes()).hexdigest(),
        "mode": 0o400,
        "nlink": 1,
        "consumedAt": legacy_fence["consumedAt"],
    }
    result_path.chmod(0o600)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result_path.chmod(0o400)

    archive_dir = result_path.parent / "archive" / "legacy-success"
    receipt = activate_module.retire_terminal_receipts(fixture.paths, archive_dir)

    assert receipt["candidateCommit"] == candidate["commit"]
    assert json.loads((archive_dir / fence_path.name).read_text()) == legacy_fence


def test_terminal_receipt_retirement_archives_restored_failed_attempt_and_clears_namespace(
    fixture: Fixture,
):
    failed = run(fixture, FakeBackend(fixture, gateway_failure=True))
    assert failed["outcome"] == "snapshot_restore_required"
    fence_path = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    restore_path = restore_result_path(fixture)
    restored = restore(fixture, StoppedRestoreBackend(fixture))
    assert restored["outcome"] == "restored"
    original_inodes = {
        path.name: os.lstat(path).st_ino
        for path in (fixture.paths.result, fence_path, restore_path)
    }
    archive_dir = fixture.paths.result.parent / "archive" / "restored-failure"

    receipt = activate_module.retire_terminal_receipts(fixture.paths, archive_dir)

    assert receipt["outcome"] == "restored"
    assert receipt["selectedPath"] == str(fixture.predecessor)
    assert receipt["restoreResult"]["path"] == str(archive_dir / restore_path.name)
    for path in (fixture.paths.result, fence_path, restore_path):
        assert not path.exists()
        assert os.lstat(archive_dir / path.name).st_ino == original_inodes[path.name]


def test_terminal_receipt_retirement_late_verifies_boot_failed_restore(fixture: Fixture):
    failed = run(fixture, FakeBackend(fixture, gateway_failure=True))
    assert failed["outcome"] == "snapshot_restore_required"
    restored = restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )
    assert restored["outcome"] == "restored_boot_failed"
    result_path = fixture.paths.result
    fence_path = result_path.with_name(activate_module.START_CONSUMED_NAME)
    restore_path = restore_result_path(fixture)
    original_inodes = {
        path.name: os.lstat(path).st_ino
        for path in (result_path, fence_path, restore_path)
    }
    archive_dir = result_path.parent / "archive" / "late-restored-failure"
    backend = FakeBackend(fixture)

    receipt = activate_module.retire_terminal_receipts(
        fixture.paths,
        archive_dir,
        backend,
    )

    assert receipt["outcome"] == "restored_after_late_verification"
    assert receipt["lateVerification"]["gateway"]["health"]["readyz"]["accepted"] is True
    assert receipt["lateVerification"]["node"]["release"] == str(fixture.predecessor)
    assert backend.verifications == 2
    assert backend.gateway_boots == backend.node_boots == 0
    assert backend.receipt_bindings == [
        (activate_module.OPERATOR.require_string('runtime.gateway_label'), True),
        (activate_module.OPERATOR.require_string('runtime.node_label'), False),
    ]
    for path in (result_path, fence_path, restore_path):
        assert not path.exists()
        assert os.lstat(archive_dir / path.name).st_ino == original_inodes[path.name]


@pytest.mark.parametrize("failure", ["gateway", "node"])
def test_terminal_receipt_retirement_late_verification_failure_preserves_trio(
    monkeypatch,
    fixture: Fixture,
    failure: str,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    assert restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )["outcome"] == "restored_boot_failed"
    result_path = fixture.paths.result
    fence_path = result_path.with_name(activate_module.START_CONSUMED_NAME)
    restore_path = restore_result_path(fixture)
    original_inodes = {
        path: os.lstat(path).st_ino for path in (result_path, fence_path, restore_path)
    }
    archive_dir = result_path.parent / "archive" / f"failed-late-{failure}"
    backend = FakeBackend(fixture)

    def fail_verification(*_args, **_kwargs):
        raise activate_module.ActivationError(f"injected late {failure} failure")

    monkeypatch.setattr(
        backend,
        "verify" if failure == "gateway" else "verify_node",
        fail_verification,
    )
    with pytest.raises(activate_module.ActivationError, match=f"late {failure} failure"):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            archive_dir,
            backend,
        )

    for path, inode in original_inodes.items():
        assert os.lstat(path).st_ino == inode
        assert not (archive_dir / path.name).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outcome", "restore_failed_preimage_restored"),
        ("error", None),
        ("restoreApplied", False),
        ("failedActivationResultSha256", "0" * 64),
    ],
)
def test_terminal_receipt_retirement_rejects_unbound_boot_failure_before_late_verify(
    fixture: Fixture,
    field: str,
    value: object,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    assert restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )["outcome"] == "restored_boot_failed"
    restore_path = restore_result_path(fixture)
    receipt = json.loads(restore_path.read_text())
    receipt[field] = value
    restore_path.chmod(0o600)
    restore_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    restore_path.chmod(0o400)
    backend = FakeBackend(fixture)

    with pytest.raises(activate_module.ActivationError, match="restore receipt binding drift"):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / f"unbound-{field}",
            backend,
        )

    assert backend.verifications == 0


def test_terminal_receipt_retirement_rejects_partial_old_gateway_verification(
    fixture: Fixture,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    assert restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )["outcome"] == "restored_boot_failed"
    restore_path = restore_result_path(fixture)
    receipt = json.loads(restore_path.read_text())
    receipt["verification"]["gateway"] = {"stale": True}
    restore_path.chmod(0o600)
    restore_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    restore_path.chmod(0o400)
    backend = FakeBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="restore receipt binding drift",
    ):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / "partial-old-verification",
            backend,
        )

    assert backend.verifications == 0


@pytest.mark.parametrize(
    "verification",
    [
        {"gateway": None, "node": {"impossible": True}},
        {"gateway": {"stale": True}, "node": {"impossible": True}},
    ],
)
def test_terminal_receipt_retirement_rejects_impossible_boot_failure_verification_shape(
    fixture: Fixture,
    verification: dict[str, object],
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    assert restore(
        fixture,
        StoppedRestoreBackend(fixture, gateway_failure=True),
    )["outcome"] == "restored_boot_failed"
    restore_path = restore_result_path(fixture)
    receipt = json.loads(restore_path.read_text())
    receipt["verification"] = verification
    restore_path.chmod(0o600)
    restore_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    restore_path.chmod(0o400)
    backend = FakeBackend(fixture)

    with pytest.raises(activate_module.ActivationError, match="restore receipt binding drift"):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / "impossible-verification-shape",
            backend,
        )

    assert backend.verifications == 0


def test_terminal_receipt_retirement_restores_bound_trio_on_post_move_failure(
    monkeypatch, fixture: Fixture,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    restore(fixture, StoppedRestoreBackend(fixture))
    result_path = fixture.paths.result
    fence_path = result_path.with_name(activate_module.START_CONSUMED_NAME)
    restore_path = restore_result_path(fixture)
    original_inodes = {
        path: os.lstat(path).st_ino for path in (result_path, fence_path, restore_path)
    }
    original_read = activate_module.read_physical

    def fail_restore_read(path, label, maximum=activate_module.MAX_FILE_BYTES):
        if label == "archived activation restore result":
            raise activate_module.ActivationError("injected restored trio readback failure")
        return original_read(path, label, maximum)

    monkeypatch.setattr(activate_module, "read_physical", fail_restore_read)
    archive_dir = result_path.parent / "archive" / "failed-restored-retirement"
    with pytest.raises(activate_module.ActivationError, match="restored trio readback"):
        activate_module.retire_terminal_receipts(fixture.paths, archive_dir)

    for path, inode in original_inodes.items():
        assert os.lstat(path).st_ino == inode
        assert not (archive_dir / path.name).exists()


def test_terminal_receipt_retirement_restores_pair_on_post_move_failure(
    monkeypatch, fixture: Fixture
):
    result = run(fixture)
    assert result["outcome"] == "activated"
    result_path = fixture.paths.result
    fence_path = result_path.with_name(activate_module.START_CONSUMED_NAME)
    result_inode = os.lstat(result_path).st_ino
    fence_inode = os.lstat(fence_path).st_ino
    original_read = activate_module.read_physical

    def fail_archived_read(path, label, maximum=activate_module.MAX_FILE_BYTES):
        if label == "archived activation result":
            raise activate_module.ActivationError("injected archived readback failure")
        return original_read(path, label, maximum)

    monkeypatch.setattr(activate_module, "read_physical", fail_archived_read)
    archive_dir = result_path.parent / "archive" / "failed-retirement"
    with pytest.raises(activate_module.ActivationError, match="injected archived"):
        activate_module.retire_terminal_receipts(fixture.paths, archive_dir)

    assert os.lstat(result_path).st_ino == result_inode
    assert os.lstat(fence_path).st_ino == fence_inode
    assert not (archive_dir / result_path.name).exists()
    assert not (archive_dir / fence_path.name).exists()


@pytest.mark.parametrize(
    "drift",
    ["reservation", "goplaces", "profile-order", "load-path", "discord", "codex"],
)
def test_protected_migrated_invariants_fail_before_selector_or_boot(fixture: Fixture, drift: str):
    paths = fixture.paths
    approval_reports = None
    if drift == "reservation":
        path = paths.candidate_state_dir / activate_module.SESSION_QUARANTINE_RESERVATIONS
        path.write_text('{"storePath":"/wrong"}\n')
    elif drift == "profile-order":
        report = approval_report()
        report["openAIProfileOrderCount"] = 4
        approval_reports = [report]
    else:
        path = paths.candidate_state_dir / "openclaw.json"
        value = json.loads(path.read_text())
        if drift == "goplaces":
            value["skills"]["entries"]["goplaces"]["apiKey"] = "forbidden"
        elif drift == "load-path":
            value["plugins"]["load"]["paths"] = ["/external/discord"]
        else:
            value["plugins"]["entries"][drift]["enabled"] = False
        path.write_text(json.dumps(value) + "\n")
    backend = FakeBackend(fixture, approval_reports=approval_reports)
    result = run(fixture, backend)
    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(paths.current_link) == str(fixture.predecessor)


def test_legacy_openai_config_auth_order_shadow_fails_before_selector_or_boot(
    fixture: Fixture,
):
    config_path = fixture.paths.candidate_state_dir / "openclaw.json"
    config = json.loads(config_path.read_text())
    config["auth"]["order"] = {"openai": list(SENSITIVE_TEST_PROFILE_IDS)}
    config_path.write_text(json.dumps(config) + "\n")
    backend = FakeBackend(fixture)

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)


def test_legacy_openai_config_profile_shadow_fails_before_selector_or_boot(
    fixture: Fixture,
):
    config_path = fixture.paths.candidate_state_dir / "openclaw.json"
    config = json.loads(config_path.read_text())
    config["auth"]["profiles"] = {
        "openai:chatgpt-default": {"provider": "openai", "mode": "oauth"}
    }
    config_path.write_text(json.dumps(config) + "\n")
    backend = FakeBackend(fixture)

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)


def test_non_openai_config_auth_entries_remain_supported(fixture: Fixture):
    config_path = fixture.paths.candidate_state_dir / "openclaw.json"
    config = json.loads(config_path.read_text())
    config["auth"] = {
        "order": {"anthropic": ["anthropic:claude-cli"]},
        "profiles": {
            "anthropic:claude-cli": {
                "provider": "anthropic",
                "mode": "token",
            }
        },
    }
    config_path.write_text(json.dumps(config) + "\n")
    fixture.config_bytes = config_path.read_bytes()
    snapshot = json.loads(fixture.snapshot.read_text())
    snapshot["recoveredInvariants"] = activate_module.validate_recovered_invariants(
        fixture.paths
    )
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    result = run(fixture)

    assert result["outcome"] == "activated"
    approvals = result["verification"]["execApprovals"]
    assert (
        approvals["openAIProfileOrderCount"]
        == activate_module.expected_auth_order_count()
    )
    assert (
        approvals["openAIProfileOrderSha256"]
        == activate_module.expected_auth_order_sha256()
    )
    assert "openAIProfileOrder" not in approvals


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("openAIProfileOrderCount", 4),
        ("openAIProfileOrderSha256", "c" * 64),
    ],
    ids=("count", "digest"),
)
def test_authoritative_openai_auth_order_drift_fails_before_selector_or_boot(
    fixture: Fixture,
    field: str,
    value: object,
):
    report = approval_report()
    report[field] = value
    backend = FakeBackend(fixture, approval_reports=[report])

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *((field, False) for field in activate_module.EXEC_APPROVALS_REQUIRED_TRUE),
        ("schemaVersion", 1),
        ("schemaVersion", True),
        ("rowCount", 0),
        ("rowCount", True),
        ("agentCount", -1),
        ("agentCount", True),
        ("allowlistCount", -1),
        ("allowlistCount", False),
        ("semanticSha256", "not-a-digest"),
        ("rawCasSha256", "F" * 64),
        ("openAIProfileOrderCount", 4),
        ("openAIProfileOrderCount", True),
        ("openAIProfileOrderSha256", "not-a-digest"),
        ("openAIProfileOrderSha256", "F" * 64),
    ],
)
def test_exec_approvals_contract_drift_fails_before_selector_or_boot(
    fixture: Fixture, field: str, value: object
):
    report = approval_report()
    report[field] = value
    backend = FakeBackend(fixture, approval_reports=[report])

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)
    assert not fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME).exists()


@pytest.mark.parametrize("shape", ["missing", "extra", "raw-order"])
def test_exec_approvals_contract_rejects_nonexact_keys_before_apply(
    fixture: Fixture, shape: str
):
    report = approval_report()
    if shape == "missing":
        report.pop("tokenPresent")
    elif shape == "raw-order":
        report["openAIProfileOrder"] = list(SENSITIVE_TEST_PROFILE_IDS)
    else:
        report["socketToken"] = "private-value-must-not-escape"
    backend = FakeBackend(fixture, approval_reports=[report])

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert "private-value-must-not-escape" not in json.dumps(result)
    assert all(
        profile_id not in json.dumps(result)
        for profile_id in SENSITIVE_TEST_PROFILE_IDS
    )
    assert backend.gateway_boots == backend.node_boots == 0


@pytest.mark.parametrize("plugin_id", activate_module.required_bundled_extensions())
def test_required_candidate_extension_drift_fails_before_boot(
    fixture: Fixture, plugin_id: str
):
    target = fixture.candidate / "dist" / "extensions" / plugin_id / "index.js"
    target.chmod(0o600)
    target.write_text("export const drift = true;\n")
    target.chmod(0o444)
    backend = FakeBackend(fixture)
    result = run(fixture, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0


def test_noncritical_candidate_asset_drift_fails_before_boot(fixture: Fixture):
    target = fixture.candidate / "NOTICE"
    target.chmod(0o600)
    target.write_text("drift\n")
    target.chmod(0o444)
    backend = FakeBackend(fixture)
    result = run(fixture, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0


def test_gateway_boot_failure_requires_snapshot_restore_without_retry(fixture: Fixture):
    backend = FakeBackend(fixture, gateway_failure=True)
    result = run(fixture, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert result["restoreRequired"] is True
    assert result["error"] == "activation_failed"
    assert "private gateway diagnostic" not in json.dumps(result)
    assert backend.gateway_boots == 1
    assert backend.node_boots == 0


def test_exec_approvals_raw_cas_drift_fails_before_fence(fixture: Fixture):
    backend = FakeBackend(
        fixture,
        approval_reports=[approval_report(raw_cas_sha256="c" * 64)],
    )

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0
    assert not fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME).exists()


def test_postboot_exec_approvals_raw_cas_drift_activates_when_semantics_match(
    fixture: Fixture,
):
    backend = FakeBackend(
        fixture,
        approval_reports=[
            approval_report(),
            approval_report(raw_cas_sha256="c" * 64),
        ],
    )

    result = run(fixture, backend)

    assert result["outcome"] == "activated"
    assert result["restoreRequired"] is False
    assert result["verification"]["execApprovals"]["rawCasSha256"] == "c" * 64
    assert backend.gateway_boots == backend.node_boots == 1


def test_postboot_exec_approvals_semantic_drift_requires_restore(
    fixture: Fixture,
):
    backend = FakeBackend(
        fixture,
        approval_reports=[
            approval_report(),
            approval_report(semantic_sha256="c" * 64),
        ],
    )

    result = run(fixture, backend)

    assert result["outcome"] == "snapshot_restore_required"
    assert result["restoreRequired"] is True
    assert backend.gateway_boots == backend.node_boots == 1


def test_postboot_openai_auth_order_drift_requires_restore(fixture: Fixture):
    drifted = approval_report()
    drifted["openAIProfileOrderSha256"] = "c" * 64
    backend = FakeBackend(
        fixture,
        approval_reports=[approval_report(), drifted],
    )

    result = run(fixture, backend)

    assert result["outcome"] == "snapshot_restore_required"
    assert result["restoreRequired"] is True
    assert backend.gateway_boots == backend.node_boots == 1


def test_activated_result_replay_rejects_openai_auth_order_drift(
    fixture: Fixture,
):
    assert run(fixture)["outcome"] == "activated"
    drifted = approval_report()
    drifted["openAIProfileOrderSha256"] = "c" * 64
    backend = FakeBackend(fixture, approval_reports=[drifted])

    with pytest.raises(
        activate_module.ActivationError,
        match="exec approvals inspection contract drift",
    ):
        run(fixture, backend)

    assert backend.gateway_boots == backend.node_boots == 0


@pytest.mark.parametrize("snapshot_format", ["legacy-raw", "fresh-gzip"])
def test_failed_activation_restore_quarantines_and_boots_predecessor_once(
    fixture: Fixture, snapshot_format: str
):
    if snapshot_format == "fresh-gzip":
        output_root = fixture.root / "compressed-restore-snapshot"
        manifest = output_root / "stopped-snapshot.json"
        activate_module.create_stopped_snapshot(
            fixture.paths, output_root, manifest, fixture.candidate,
            CANDIDATE_COMMIT, fixture.seal, FakeBackend(fixture),
        )
        fixture = replace(fixture, snapshot=manifest)
    snapshot = activate_module.load_stopped_snapshot(
        fixture.paths, fixture.snapshot, sealed_candidate(fixture)
    )
    archive = Path(snapshot["tarPath"])
    with archive.open("rb") as stream:
        assert (stream.read(2) == b"\x1f\x8b") is (snapshot_format == "fresh-gzip")
    assert snapshot["tarSha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert archive.name == "stopped-snapshot.tar"
    expected_files = {}
    for path in (
        fixture.paths.predecessor_state_dir / "openclaw.json", fixture.external_marker,
        fixture.paths.gateway_plist, fixture.paths.node_plist,
    ):
        info = path.stat()
        expected_files[path] = (
            path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid,
        )
    gateway_xattr = subprocess.run(
        ["/usr/bin/xattr", "-p", "com.openclaw.snapshot-test", str(fixture.paths.gateway_plist)],
        check=True, capture_output=True,
    ).stdout
    external_runtime_assets = fixture.root / "external-runtime-assets"
    external_runtime_sentinel = external_runtime_assets / "sentinel.txt"
    sentinel_before = (
        external_runtime_sentinel.read_bytes(),
        os.lstat(external_runtime_sentinel).st_dev,
        os.lstat(external_runtime_sentinel).st_ino,
        stat.S_IMODE(os.lstat(external_runtime_sentinel).st_mode),
        os.lstat(external_runtime_sentinel).st_nlink,
    )
    failed_backend = FakeBackend(fixture, gateway_failure=True)
    failed = run(fixture, failed_backend)
    assert failed["outcome"] == "snapshot_restore_required"

    backend = StoppedRestoreBackend(fixture)
    output = restore_result_path(fixture)
    receipt = restore(fixture, backend)

    assert receipt["outcome"] == "restored"
    assert receipt["restoreApplied"] is True
    assert (
        receipt["snapshot"]["execApprovals"]["openAIProfileOrderCount"]
        == activate_module.expected_auth_order_count()
    )
    assert (
        receipt["snapshot"]["execApprovals"]["openAIProfileOrderSha256"]
        == activate_module.expected_auth_order_sha256()
    )
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)
    assert os.readlink(
        fixture.paths.predecessor_state_dir / "external-runtime-assets"
    ) == str(external_runtime_assets)
    assert (
        external_runtime_sentinel.read_bytes(),
        os.lstat(external_runtime_sentinel).st_dev,
        os.lstat(external_runtime_sentinel).st_ino,
        stat.S_IMODE(os.lstat(external_runtime_sentinel).st_mode),
        os.lstat(external_runtime_sentinel).st_nlink,
    ) == sentinel_before
    assert backend.gateway_boots == 1
    assert backend.node_boots == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o400
    assert all(
        Path(path).exists() or Path(path).is_symlink()
        for path in receipt["quarantinedFailedSurfaces"]
    )
    assert len(receipt["quarantinedFailedSurfaces"]) == 4
    assert all("exec-approvals.json" not in path for path in receipt["quarantinedFailedSurfaces"])
    for path, expected in expected_files.items():
        info = path.stat()
        assert (
            path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid,
        ) == expected
    assert subprocess.run(
        ["/usr/bin/xattr", "-p", "com.openclaw.snapshot-test", str(fixture.paths.gateway_plist)],
        check=True, capture_output=True,
    ).stdout == gateway_xattr


def test_historical_different_profile_snapshot_cannot_start_activation(
    fixture: Fixture,
):
    manifest = json.loads(fixture.snapshot.read_text())
    manifest["execApprovals"]["openAIProfileOrderCount"] = (
        LEGACY_OPENAI_AUTH_ORDER_COUNT
    )
    manifest["execApprovals"]["openAIProfileOrderSha256"] = (
        LEGACY_OPENAI_AUTH_ORDER_SHA256
    )
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)
    backend = FakeBackend(fixture)

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.inspection_calls == 0
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)
    assert not fixture.paths.result.with_name(
        activate_module.START_CONSUMED_NAME
    ).exists()


def test_historical_different_profile_snapshot_cannot_restore_failed_activation(
    fixture: Fixture,
):
    assert run(
        fixture,
        FakeBackend(fixture, gateway_failure=True),
    )["outcome"] == "snapshot_restore_required"
    manifest = json.loads(fixture.snapshot.read_text())
    manifest["execApprovals"]["openAIProfileOrderCount"] = (
        LEGACY_OPENAI_AUTH_ORDER_COUNT
    )
    manifest["execApprovals"]["openAIProfileOrderSha256"] = (
        LEGACY_OPENAI_AUTH_ORDER_SHA256
    )
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)
    backend = StoppedRestoreBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="exec approvals inspection contract drift",
    ):
        restore(fixture, backend)

    assert backend.stopped_checks == 0
    assert backend.inspection_calls == 0
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.candidate)


def test_restore_inspects_preimage_and_extracted_state_only_after_stopped_proofs(
    monkeypatch, fixture: Fixture
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    backend = StoppedRestoreBackend(fixture)
    original_run = activate_module.run_bounded

    def track_extract(argv, timeout, **kwargs):
        if argv[0] == "/usr/bin/tar":
            backend.events.append("extract")
        return original_run(argv, timeout, **kwargs)

    monkeypatch.setattr(activate_module, "run_bounded", track_extract)

    receipt = restore(fixture, backend)

    assert receipt["outcome"] == "restored"
    assert backend.events[:5] == [
        "stopped",
        "inspect",
        "extract",
        "stopped",
        "inspect",
    ]


def test_restore_rejects_extracted_exec_approvals_raw_drift_and_restores_preimage(
    fixture: Fixture,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    live_paths = (
        fixture.paths.predecessor_state_dir,
        fixture.paths.gateway_plist,
        fixture.paths.node_plist,
        fixture.paths.current_link,
    )
    original_inodes = [os.lstat(path).st_ino for path in live_paths]
    backend = StoppedRestoreBackend(
        fixture,
        approval_reports=[
            approval_report(),
            approval_report(raw_cas_sha256="c" * 64),
            approval_report(),
        ],
    )

    receipt = restore(fixture, backend)

    assert receipt["outcome"] == "restore_failed_preimage_restored"
    assert receipt["restoreApplied"] is False
    assert [os.lstat(path).st_ino for path in live_paths] == original_inodes
    assert backend.gateway_boots == backend.node_boots == 0


def test_failed_activation_restore_preserves_partial_extract_and_restores_original_inodes(
    monkeypatch, fixture: Fixture
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    live_paths = (
        fixture.paths.predecessor_state_dir,
        fixture.paths.gateway_plist,
        fixture.paths.node_plist,
        fixture.paths.current_link,
    )
    original_inodes = [os.lstat(path).st_ino for path in live_paths]
    original_run = activate_module.run_bounded

    def partial_extract(argv, timeout, **kwargs):
        if argv[0] != "/usr/bin/tar":
            return original_run(argv, timeout, **kwargs)
        live_paths[0].mkdir()
        (live_paths[0] / "partial.marker").write_text("partial\n")
        live_paths[1].write_text("partial gateway\n")
        live_paths[2].write_text("partial node\n")
        live_paths[3].symlink_to(fixture.candidate)
        return activate_module.CommandResult(tuple(argv), 2, b"", b"partial", 1, False)

    monkeypatch.setattr(activate_module, "run_bounded", partial_extract)
    backend = StoppedRestoreBackend(fixture)
    receipt = restore(fixture, backend)

    assert receipt["outcome"] == "restore_failed_preimage_restored"
    assert receipt["restoreApplied"] is False
    assert backend.gateway_boots == backend.node_boots == 0
    assert [os.lstat(path).st_ino for path in live_paths] == original_inodes
    suffix = CANDIDATE_COMMIT[:12]
    partials = [path.with_name(f"{path.name}.partial-{suffix}") for path in live_paths]
    assert all(path.exists() or path.is_symlink() for path in partials)


def test_failed_activation_restore_post_rename_fsync_failure_restores_moved_inode(
    monkeypatch, fixture: Fixture
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    state = fixture.paths.predecessor_state_dir
    original_inode = os.lstat(state).st_ino
    quarantine = state.with_name(f"{state.name}.failed-{CANDIDATE_COMMIT[:12]}")
    original_fsync = activate_module.fsync_directory
    injected = False

    def fail_first_fsync(path):
        nonlocal injected
        if (not injected and path == state.parent and quarantine.exists()
                and not state.exists()):
            injected = True
            raise OSError("injected post-rename fsync failure")
        return original_fsync(path)

    monkeypatch.setattr(activate_module, "fsync_directory", fail_first_fsync)
    receipt = restore(fixture, StoppedRestoreBackend(fixture))

    assert receipt["outcome"] == "restore_failed_preimage_restored"
    assert os.lstat(state).st_ino == original_inode
    assert not state.with_name(f"{state.name}.failed-{CANDIDATE_COMMIT[:12]}").exists()


def test_failed_activation_restore_quarantine_collision_precedes_lifecycle(
    monkeypatch, fixture: Fixture
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    collision = fixture.paths.predecessor_state_dir.with_name(
        f"{fixture.paths.predecessor_state_dir.name}.failed-{CANDIDATE_COMMIT[:12]}"
    )
    collision.mkdir()
    calls: list[tuple[str, ...]] = []

    def record_call(argv, _timeout, **_kwargs):
        calls.append(tuple(argv))
        raise AssertionError("lifecycle must not run after a quarantine collision")

    monkeypatch.setattr(activate_module, "run_bounded", record_call)
    with pytest.raises(activate_module.ActivationError, match="quarantine collision"):
        restore(fixture, StoppedRestoreBackend(fixture))
    assert calls == []
    assert not restore_result_path(fixture).exists()


def test_failed_activation_restore_rejects_third_selector_before_lifecycle(
    monkeypatch, fixture: Fixture
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    third = make_release(
        fixture.paths.releases_root,
        "openclaw-unrelated",
        "3" * 40,
    )
    fixture.paths.current_link.unlink()
    fixture.paths.current_link.symlink_to(third)
    calls: list[tuple[str, ...]] = []

    def record_call(argv, _timeout, **_kwargs):
        calls.append(tuple(argv))
        raise AssertionError("lifecycle must not run for an unrelated selector")

    monkeypatch.setattr(activate_module, "run_bounded", record_call)
    with pytest.raises(activate_module.ActivationError, match="selector target drift"):
        restore(fixture, StoppedRestoreBackend(fixture))
    assert calls == []


def test_failed_activation_restore_boot_failure_is_typed_and_not_retried(fixture: Fixture):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    backend = StoppedRestoreBackend(fixture, gateway_failure=True)
    receipt = restore(fixture, backend)

    assert receipt["outcome"] == "restored_boot_failed"
    assert receipt["error"] == "predecessor_boot_failed"
    assert receipt["restoreApplied"] is True
    assert backend.gateway_boots == 1
    assert backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)


@pytest.mark.parametrize("service_returncode", [0, 2])
def test_failed_activation_restore_requires_exact_stopped_precondition_without_state_move(
    monkeypatch, fixture: Fixture, service_returncode: int,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    state_inode = os.lstat(fixture.paths.predecessor_state_dir).st_ino

    calls: list[tuple[str, ...]] = []

    def service_not_stopped(argv, _timeout, **_kwargs):
        calls.append(tuple(argv))
        assert argv[0:2] == ("/bin/launchctl", "print")
        return activate_module.CommandResult(
            tuple(argv), service_returncode,
            b"loaded\n" if service_returncode == 0 else b"",
            b"ambiguous\n" if service_returncode else b"",
            1, False,
        )

    monkeypatch.setattr(activate_module, "run_bounded", service_not_stopped)
    backend = activate_module.SystemBackend(fixture.paths)
    with pytest.raises(activate_module.ActivationError, match="stopped-state proof failed"):
        restore(fixture, backend)

    assert os.lstat(fixture.paths.predecessor_state_dir).st_ino == state_inode
    assert len(calls) == 1
    assert all("bootout" not in call for call in calls)
    assert not restore_result_path(fixture).exists()


def test_stale_discord_install_record_fails_before_fence_without_mutation(fixture: Fixture):
    backend = FakeBackend(fixture)
    backend.install_records["discord"] = {
        "source": "npm",
        "installPath": "/old/discord",
    }
    before = json.loads(json.dumps(backend.install_records))

    result = run(fixture, backend)

    assert backend.install_records == before
    assert result["outcome"] == "failed_before_apply"
    assert result["statesVisited"] == ["preflight", "terminal"]
    assert result["restoreRequired"] is False
    assert result["error"] == "preflight_failed"
    assert "private Discord verification diagnostic" not in json.dumps(result)
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)
    assert backend.gateway_boots == backend.node_boots == 0
    fence = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    assert not fence.exists()
    assert not fixture.paths.result.exists()

    del backend.install_records["discord"]
    retry = run(fixture, backend)

    assert retry["outcome"] == "activated"
    assert backend.gateway_boots == backend.node_boots == 1


def test_activated_result_replay_reverifies_live_candidate_without_boot(fixture: Fixture):
    first = run(fixture)
    backend = FakeBackend(fixture)
    replay = run(fixture, backend)
    assert replay == first
    assert backend.gateway_boots == backend.node_boots == 0
    assert backend.verifications == 2
    assert backend.receipt_bindings == [
        (activate_module.OPERATOR.require_string('runtime.gateway_label'), True),
        (activate_module.OPERATOR.require_string('runtime.node_label'), True),
    ]


def test_activated_result_replay_rejects_mutable_receipt_before_verification(
    fixture: Fixture,
):
    run(fixture)
    fixture.paths.result.chmod(0o600)
    backend = FakeBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="existing activation result binding drift",
    ):
        run(fixture, backend)

    assert backend.inspection_calls == backend.verifications == 0


def test_activated_result_replay_accepts_raw_cas_change_with_same_semantics(
    fixture: Fixture,
):
    first = run(fixture)
    backend = FakeBackend(
        fixture,
        approval_reports=[approval_report(raw_cas_sha256="c" * 64)],
    )

    replay = run(fixture, backend)

    assert replay == first
    assert backend.inspection_calls == 1
    assert backend.gateway_boots == backend.node_boots == 0


def test_activated_result_replay_rejects_exec_approvals_semantic_drift(
    fixture: Fixture,
):
    run(fixture)
    backend = FakeBackend(
        fixture,
        approval_reports=[approval_report(semantic_sha256="c" * 64)],
    )

    with pytest.raises(
        activate_module.ActivationError,
        match="exec approvals semantic drift",
    ):
        run(fixture, backend)

    assert backend.gateway_boots == backend.node_boots == 0


def test_orphaned_fence_terminalizes_without_second_boot(fixture: Fixture):
    candidate = activate_module.validate_candidate_seal(fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal)
    snapshot = activate_module.load_stopped_snapshot(
        fixture.paths, fixture.snapshot, candidate
    )
    fence = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    bindings = {
        "schemaVersion": 1,
        "candidateSealPath": str(fixture.seal),
        "candidateSealSha256": candidate["sealSha256"],
        "snapshotManifestPath": str(fixture.snapshot),
        "snapshotManifestSha256": snapshot["manifestSha256"],
        "terminalResultPath": str(fixture.paths.result),
        "consumedAt": activate_module.utc_now(),
    }
    activate_module.create_immutable_file(fence, json.dumps(bindings, indent=2, sort_keys=True).encode() + b"\n")
    backend = FakeBackend(fixture)
    result = run(fixture, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert result["error"] == "start_already_consumed"
    assert backend.gateway_boots == backend.node_boots == 0


def test_fixed_lock_contention_does_not_write_result(fixture: Fixture):
    descriptor = os.open(fixture.paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(activate_module.ActivationError, match="holds the fixed lock"):
            run(fixture)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert not fixture.paths.result.exists()


def test_snapshot_archive_drift_fails_before_boot(fixture: Fixture):
    manifest = json.loads(fixture.snapshot.read_text())
    archive = Path(manifest["tarPath"])
    archive.chmod(0o600)
    archive.write_bytes(b"tampered\n")
    archive.chmod(0o400)
    backend = FakeBackend(fixture)
    result = run(fixture, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0


def test_different_version_without_reviewed_upgrade_input_is_rejected(fixture: Fixture):
    package = fixture.candidate / "package.json"
    package.chmod(0o600)
    value = json.loads(package.read_text())
    value["version"] = "2026.9.0"
    package.write_text(json.dumps(value) + "\n")
    package.chmod(0o444)
    fixture.seal.chmod(0o600)
    fixture.seal.unlink()
    activate_module.create_candidate_seal(fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal)
    backend = FakeBackend(fixture)
    result = run(fixture, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0


class UpgradeBackend(FakeBackend):
    """Only the native migration/process boundary is fake; snapshots use real tar."""

    def __init__(self, fixture, *, migration_failure=None, **kwargs):
        super().__init__(fixture, **kwargs)
        self.migration_failure = migration_failure
        self.migrations = 0
        self.inspected_releases = []

    def inspect_exec_approvals(self, release):
        version = (self.fixture.paths.candidate_state_dir / "schema.version").read_text()
        if version not in ("17", "19"):
            raise activate_module.ActivationError("partial migration is not runtime readable")
        assert release == (self.fixture.predecessor if version == "17" else self.fixture.candidate)
        self.inspected_releases.append(release)
        self.events.append("inspect")
        report = approval_report(raw_cas_sha256=("b" if version == "17" else "c") * 64)
        if version == "19" and self.migration_failure == "auth-drift":
            report["semanticSha256"] = "d" * 64
        return activate_module.validate_exec_approvals_inspection(report)

    def migrate_state_once(self, release):
        assert release == self.fixture.candidate
        assert activate_module._start_fence_path(self.fixture.paths).exists()
        assert self.fixture.paths.current_link.resolve() == self.fixture.predecessor
        self.migrations += 1
        self.events.append("migrate")
        state = self.fixture.paths.candidate_state_dir
        (state / "schema.version").write_text("19")
        if self.migration_failure in ("partial", "invalid-config"):
            (state / "schema.version").write_text("partial")
            if self.migration_failure == "invalid-config":
                (state / "openclaw.json").write_text("invalid intermediate config")
            raise activate_module.ActivationError("private migration diagnostic")
        if self.migration_failure == "config-drift":
            config = state / "openclaw.json"
            config.write_bytes(config.read_bytes() + b"\n")
        return {"schemaVersion": 1, "status": "completed", "stateSchemaVersion": 15,
                "agentSchemaVersion": 19, "agentDatabaseCount": 1}

    def verify_candidate_discord(self, release, config_path):
        assert (self.fixture.paths.candidate_state_dir / "schema.version").read_text() == "19"
        return {"origin": "bundled", "configSha256": hashlib.sha256(config_path.read_bytes()).hexdigest()}


@pytest.fixture
def upgrade_fixture(fixture):
    chmod_tree(fixture.predecessor, True)
    inspector = fixture.predecessor / "dist" / "infra" / "exec-approvals-inspection.js"
    inspector.parent.mkdir(parents=True)
    inspector.write_text("export function inspectExecApprovalsState() {}\n")
    chmod_tree(fixture.predecessor, False)
    chmod_tree(fixture.candidate, True)
    package = fixture.candidate / "package.json"
    package.write_text(json.dumps({"name": "openclaw", "version": "2026.9.2",
                                   "openclaw": {"schemaVersions": {"state": 15, "agent": 19}}}) + "\n")
    # The current candidate already carries its Doctor leaf; the existing
    # UpgradeBackend owns synthetic migration behavior, not a retired export.
    assert (fixture.candidate / "dist" / "commands" / "doctor.worker.js").is_file()
    chmod_tree(fixture.candidate, False)
    fixture.seal = fixture.root / "upgrade-seal.json"
    activate_module.create_candidate_seal(fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal)
    (fixture.paths.candidate_state_dir / "schema.version").write_text("17")
    output = fixture.root / "upgrade-snapshot"
    fixture.snapshot = output / "stopped-snapshot.json"
    backend = UpgradeBackend(fixture)
    activate_module.create_stopped_snapshot(
        fixture.paths, output, fixture.snapshot, fixture.candidate,
        CANDIDATE_COMMIT, fixture.seal, backend,
    )
    assert backend.inspected_releases == [fixture.predecessor, fixture.predecessor]
    assert backend.migrations == backend.gateway_boots == backend.node_boots == 0
    config = json.loads(fixture.config_bytes)
    config["agents"] = {"defaults": {"thinkingDefault": "ultra"}}
    target = fixture.root / "reviewed-upgrade-config.json"
    target.write_text(json.dumps(config, sort_keys=True) + "\n")
    target.chmod(0o400)
    return fixture, target


def run_upgrade(fixture, target, backend):
    return activate_module.activate(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal,
        fixture.snapshot, backend, upgrade_config=target,
    )


def test_upgrade_preserves_old_snapshot_and_uses_native_readers_in_schema_order(upgrade_fixture):
    fixture, target = upgrade_fixture
    backend = UpgradeBackend(fixture)
    result = run_upgrade(fixture, target, backend)
    assert result["outcome"] == "activated"
    assert backend.migrations == backend.gateway_boots == backend.node_boots == 1
    assert backend.inspected_releases == [fixture.predecessor, fixture.candidate, fixture.candidate]
    assert result["snapshot"]["recoveredInvariants"]["config"]["sha256"] == hashlib.sha256(fixture.config_bytes).hexdigest()
    assert result["verification"]["recoveredInvariants"]["config"]["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result["firstBootAttempted"] is True
    assert stat.S_IMODE((fixture.paths.candidate_state_dir / "openclaw.json").stat().st_mode) == 0o600
    repeated = UpgradeBackend(fixture)
    assert run_upgrade(fixture, target, repeated) == result
    assert repeated.migrations == repeated.gateway_boots == repeated.node_boots == 0


@pytest.mark.parametrize("failure", ["partial", "invalid-config", "config-drift", "auth-drift"])
def test_upgrade_migration_failure_is_fenced_before_boot_and_restorable(upgrade_fixture, failure):
    fixture, target = upgrade_fixture
    backend = UpgradeBackend(fixture, migration_failure=failure)
    result = run_upgrade(fixture, target, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert result["firstBootAttempted"] is False
    assert backend.migrations == 1
    assert backend.gateway_boots == backend.node_boots == 0
    repeated = UpgradeBackend(fixture)
    assert run_upgrade(fixture, target, repeated) == result
    assert repeated.migrations == repeated.gateway_boots == 0
    restored_backend = UpgradeBackend(fixture)
    receipt = restore(fixture, restored_backend)
    assert receipt["outcome"] == "restored"
    assert restored_backend.inspected_releases == [fixture.predecessor]
    assert restored_backend.gateway_boots == restored_backend.node_boots == 1
    assert (fixture.paths.candidate_state_dir / "schema.version").read_text() == "17"
    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes
    assert "private migration diagnostic" not in json.dumps(result)


def test_upgrade_config_write_occurs_after_fence_and_failure_is_restorable(monkeypatch, upgrade_fixture):
    fixture, target = upgrade_fixture
    install = activate_module.install_upgrade_config
    def fail_after_write(paths, binding):
        assert activate_module._start_fence_path(paths).exists()
        install(paths, binding)
        raise OSError("injected post-config-write failure")
    monkeypatch.setattr(activate_module, "install_upgrade_config", fail_after_write)
    backend = UpgradeBackend(fixture)
    result = run_upgrade(fixture, target, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert result["firstBootAttempted"] is False
    assert backend.migrations == backend.gateway_boots == 0
    assert restore(fixture, UpgradeBackend(fixture))["outcome"] == "restored"


@pytest.mark.parametrize("after_success", [False, True])
def test_upgrade_restore_after_boot_preserves_effects_and_does_not_replay(upgrade_fixture, after_success):
    fixture, target = upgrade_fixture
    backend = UpgradeBackend(fixture, gateway_failure=not after_success)
    result = run_upgrade(fixture, target, backend)
    assert result["firstBootAttempted"] is True
    marker = fixture.paths.candidate_state_dir / "completed-after-upgrade"
    marker.write_text("delivery already completed; never replay from restored pending queue")
    if after_success:
        with pytest.raises(activate_module.ActivationError, match="restore binding drift"):
            restore(fixture, UpgradeBackend(fixture))
    recovery = UpgradeBackend(fixture)
    receipt = activate_module.restore_failed_activation(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal,
        fixture.snapshot, restore_result_path(fixture), recovery, after_success=after_success,
    )
    assert receipt["outcome"] == "restored_stopped"
    assert receipt["restoreApplied"] is True
    assert recovery.gateway_boots == recovery.node_boots == recovery.migrations == 0
    quarantine = Path(receipt["quarantinedFailedSurfaces"][0])
    assert (quarantine / marker.name).read_text() == "delivery already completed; never replay from restored pending queue"
    assert (quarantine / "schema.version").read_text() == "19"
    assert fixture.paths.current_link.resolve() == fixture.predecessor
    assert (fixture.paths.candidate_state_dir / "schema.version").read_text() == "17"
    with pytest.raises(activate_module.ActivationError, match="already exists"):
        activate_module.restore_failed_activation(
            fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal,
            fixture.snapshot, restore_result_path(fixture), recovery, after_success=after_success,
        )
    retirement = activate_module.retire_terminal_receipts(
        fixture.paths, fixture.paths.result.parent / "archive" / "restored-upgrade", recovery,
    )
    assert retirement["outcome"] == "restored_stopped"
    assert recovery.gateway_boots == recovery.node_boots == 0


@pytest.mark.parametrize("invalid", ["missing", "writable", "symlink", "policy"])
def test_upgrade_rejects_missing_or_unreviewed_config_before_fence(upgrade_fixture, invalid):
    fixture, target = upgrade_fixture
    if invalid == "missing":
        target = None
    elif invalid == "writable":
        target.chmod(0o600)
    elif invalid == "symlink":
        link = target.with_name("linked-config.json")
        link.symlink_to(target)
        target = link
    else:
        target.chmod(0o600)
        config = json.loads(target.read_bytes())
        config["auth"]["cooldowns"] = {}
        target.write_text(json.dumps(config))
        target.chmod(0o400)
    backend = UpgradeBackend(fixture)
    result = run_upgrade(fixture, target, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.migrations == backend.gateway_boots == 0
    assert not activate_module._start_fence_path(fixture.paths).exists()
    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes


def test_upgrade_snapshot_rejects_substituted_predecessor_reader(upgrade_fixture):
    fixture, _target = upgrade_fixture
    value = json.loads(fixture.snapshot.read_bytes())
    value["execApprovalsInspector"]["predecessorReader"]["commit"] = "f" * 40
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(value))
    fixture.snapshot.chmod(0o400)
    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.load_stopped_snapshot(fixture.paths, fixture.snapshot, sealed_candidate(fixture))


def test_upgrade_retirement_rejects_malformed_snapshot_hash_without_mutation(upgrade_fixture):
    fixture, target = upgrade_fixture
    result = run_upgrade(fixture, target, UpgradeBackend(fixture))
    result["snapshot"]["manifestSha256"] = None
    fixture.paths.result.chmod(0o600)
    fixture.paths.result.write_text(json.dumps(result))
    fixture.paths.result.chmod(0o400)
    archive = fixture.paths.result.parent / "archive" / "invalid-upgrade"
    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.retire_terminal_receipts(fixture.paths, archive)
    assert fixture.paths.result.exists()
    assert activate_module._start_fence_path(fixture.paths).exists()
    assert not archive.exists()


def test_orphaned_upgrade_fence_never_assumes_no_boot_or_repeats_migration(upgrade_fixture):
    fixture, target = upgrade_fixture
    assert run_upgrade(fixture, target, UpgradeBackend(fixture, migration_failure="partial"))["outcome"] == "snapshot_restore_required"
    fixture.paths.result.unlink()
    backend = UpgradeBackend(fixture)
    result = run_upgrade(fixture, target, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert result["firstBootAttempted"] is None
    assert backend.migrations == backend.gateway_boots == backend.node_boots == 0
    recovery = UpgradeBackend(fixture)
    assert restore(fixture, recovery)["outcome"] == "restored_stopped"
    assert recovery.gateway_boots == recovery.node_boots == 0


def test_partial_upgrade_restore_extraction_failure_preserves_unreadable_preimage(monkeypatch, upgrade_fixture):
    fixture, target = upgrade_fixture
    assert run_upgrade(fixture, target, UpgradeBackend(fixture, migration_failure="invalid-config"))["outcome"] == "snapshot_restore_required"
    original_inode = fixture.paths.candidate_state_dir.stat().st_ino
    def failed_extract(argv, _timeout, **_kwargs):
        assert argv[0] == "/usr/bin/tar" and "-xpf" in argv
        return activate_module.CommandResult(argv, 1, b"", b"private extraction error", 1, False)
    monkeypatch.setattr(activate_module, "run_bounded", failed_extract)
    recovery = UpgradeBackend(fixture)
    receipt = restore(fixture, recovery)
    assert receipt["outcome"] == "restore_failed_preimage_restored"
    assert fixture.paths.candidate_state_dir.stat().st_ino == original_inode
    assert (fixture.paths.candidate_state_dir / "schema.version").read_text() == "partial"
    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_text() == "invalid intermediate config"
    assert recovery.inspected_releases == []
    assert recovery.gateway_boots == recovery.node_boots == 0


@pytest.mark.parametrize("failure", [None, "nonzero", "timeout", "malformed", "advisory", "maintenance-refusal", "config-refusal", "wrong-mode", "stdout-only", "non-object"])
@pytest.mark.parametrize("tmp_alias", [False, True])
def test_native_migration_invoker_uses_one_native_doctor_and_protected_result(monkeypatch, upgrade_fixture, failure, tmp_alias):
    fixture, _target = upgrade_fixture
    # Native macOS temporary roots may be returned through /tmp -> /private/tmp.
    # The native writer receives that admitted path; the immutable receipt binds
    # its physical leaf, rather than rejecting the platform's directory alias.
    native_tmp = fixture.root
    if tmp_alias:
        native_tmp = fixture.root / "native-result-alias"
        native_tmp.symlink_to(fixture.root, target_is_directory=True)
    report = {"status": "ok", "configHash": "unchanged"}
    if failure == "advisory":
        report["status"] = "advisory"
    elif failure == "maintenance-refusal":
        report["maintenanceRefusal"] = {"kind": "data-at-risk", "reason": "active-mutation"}
    elif failure == "config-refusal":
        report["configWriteRefusal"] = {"reason": "conflict", "keys": ["agents"]}
    if failure == "non-object":
        report = []
    calls = []
    def fake_run(argv, timeout, *, cwd, env):
        calls.append((argv, timeout, cwd, env))
        assert env["OPENCLAW_STATE_DIR"] == str(fixture.paths.candidate_state_dir)
        assert env["OPENCLAW_CONFIG_PATH"] == str(fixture.paths.candidate_state_dir / "openclaw.json")
        assert env["XDG_CACHE_HOME"] == str(fixture.paths.candidate_state_dir / ".cache")
        assert env["OPENCLAW_LOG_LEVEL"] == "silent"
        assert env["OPENCLAW_UPDATE_IN_PROGRESS"] == "1"
        assert "OPENCLAW_MIGRATION_ENTRYPOINT" not in env
        assert env["OPENCLAW_SUPERVISOR_MODE"] == env["OPENCLAW_SERVICE_REPAIR_POLICY"] == "external"
        if "OPENCLAW_NATIVE_SECURITY_SDK" in env:
            assert env["OPENCLAW_NATIVE_SECURITY_SDK"] == str(
                fixture.candidate / "dist" / "plugin-sdk" / "security-runtime.js"
            )
            assert argv[:3] == (str(fixture.paths.node), "--input-type=module", "--eval")
            assert "resolvePreferredOpenClawTmpDir" in argv[-1]
            return activate_module.CommandResult(argv, 0, json.dumps({"root": str(native_tmp)}).encode(), b"", 1, False)
        assert argv == (str(fixture.paths.node), str(fixture.candidate / "openclaw.mjs"),
                        "doctor", "--repair", "--non-interactive", "--no-workspace-suggestions")
        target = Path(env["OPENCLAW_UPDATE_POST_INSTALL_DOCTOR_RESULT_PATH"])
        assert target.parent == native_tmp
        if failure != "stdout-only":
            target.write_bytes(b"malformed" if failure == "malformed" else json.dumps(report).encode())
            target.chmod(0o644 if failure == "wrong-mode" else 0o600)
        return activate_module.CommandResult(
            argv, 2 if failure == "nonzero" else 0,
            (json.dumps({"status": "ok"}).encode() if failure == "stdout-only" else
             b"Doctor stdout privateDiagnostic must-not-escape is not its success receipt"),
            b"private diagnostic never exposed", 1, failure == "timeout",
        )
    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    if failure is None:
        observed = backend.migrate_state_once(fixture.candidate)
        assert observed["schemaVersion"] == 2
        assert observed["status"] == "completed"
        assert observed["declaredSchemaVersions"] == {"state": 15, "agent": 19}
        assert observed["doctor"] == report
        assert Path(observed["doctorResultPath"]).stat().st_mode & 0o777 == 0o400
        assert Path(observed["doctorResultPath"]).parent == fixture.root
        assert observed["doctorResultSha256"] == hashlib.sha256(
            Path(observed["doctorResultPath"]).read_bytes()
        ).hexdigest()
    else:
        with pytest.raises(activate_module.ActivationError) as error:
            backend.migrate_state_once(fixture.candidate)
        for sensitive_text in ("private diagnostic never exposed", "privateDiagnostic", "must-not-escape"):
            assert sensitive_text not in str(error.value)
    assert len(calls) == 2
    assert len(backend.command_evidence) == 2
    for command_evidence, call in zip(backend.command_evidence, calls):
        assert set(command_evidence) == activate_module.COMMAND_EVIDENCE_KEYS
        # Keep the original value-free command envelope checks for both owners.
        assert command_evidence["executable"] == call[0][0]
    evidence = json.dumps(backend.command_evidence)
    assert "private diagnostic never exposed" not in evidence
    assert "privateDiagnostic" not in evidence
    assert "must-not-escape" not in evidence


@pytest.mark.parametrize("declared_version", [0, True, "19"])
def test_native_migration_invoker_rejects_invalid_declared_schema_before_commands(
    monkeypatch, upgrade_fixture, declared_version
):
    fixture, _target = upgrade_fixture
    package = fixture.candidate / "package.json"
    package.chmod(0o644)
    value = json.loads(package.read_text())
    value["openclaw"]["schemaVersions"]["agent"] = declared_version
    package.write_text(json.dumps(value, sort_keys=True) + "\n")
    package.chmod(0o444)
    calls = []
    def unexpected_run(argv, _timeout, **_kwargs):
        calls.append(argv)
        raise AssertionError("invalid declared schema must fail before native commands")
    monkeypatch.setattr(activate_module, "run_bounded", unexpected_run)
    backend = activate_module.SystemBackend(fixture.paths)
    with pytest.raises(activate_module.ActivationError, match="native state migration contract drift"):
        backend.migrate_state_once(fixture.candidate)
    assert calls == []
    assert backend.command_evidence == []


def test_reduced_source_has_no_initial_cutover_migration_or_plugin_install_contract():
    source = SCRIPT.read_text()
    for retired in (
        "ExternalPluginBootstrapBinding",
        "load_external_plugin_bootstrap_manifest",
        "validate_external_plugin_bootstrap",
        "validate_main_session_sqlite_migration",
        "normalize_expected_beta3_imported_cron_job",
        "EXPECTED_RELEASE_PACKAGE_INVENTORY",
        "EXPECTED_CONFIG_EVIDENCE_SHA256",
        "EXPECTED_OPENAI_PROFILE_ORDER",
        "@openclaw/discord@2026.8.1-beta.3",
        "coordinator",
        "adjudication",
        "operation UUID",
    ):
        assert retired not in source
    # SQLite is confined to the exact read-only TCC query, never state migration.
    permission_source = inspect.getsource(activate_module.screen_capture_permission)
    assert "sqlite3" not in source.replace(permission_source, "").replace("import sqlite3\n", "")
    assert 'modes.add_parser("activate")' in source
    assert 'modes.add_parser("prepare")' not in source
    assert 'modes.add_parser("start")' not in source


def test_lifecycle_bootstrap_vectors_are_exact_and_nonretrying(monkeypatch, fixture: Fixture):
    calls = []
    def fake_run(argv, timeout):
        calls.append(argv)
        return activate_module.CommandResult(argv, 0, b"", b"", 1, False)
    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    backend.bootstrap_gateway_once()
    backend.bootstrap_node_once()
    domain = f"gui/{fixture.paths.operator_uid}"
    assert calls == [
        ("/bin/launchctl", "enable", f"{domain}/{activate_module.OPERATOR.require_string('runtime.gateway_label')}"),
        ("/bin/launchctl", "bootstrap", domain, str(fixture.paths.gateway_plist)),
        ("/bin/launchctl", "enable", f"{domain}/{activate_module.OPERATOR.require_string('runtime.node_label')}"),
        ("/bin/launchctl", "bootstrap", domain, str(fixture.paths.node_plist)),
    ]


def test_failed_enable_does_not_create_bootstrap_receipt_authority(
    monkeypatch,
    fixture: Fixture,
):
    monkeypatch.setattr(
        activate_module,
        "run_bounded",
        lambda argv, _timeout: command_result(argv, returncode=1),
    )
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(activate_module.ActivationError, match="gateway_enable failed"):
        backend.bootstrap_gateway_once()

    assert backend.bootstrap_bindings() == {}


@pytest.mark.parametrize(
    "tamper",
    ["boolean-return", "wrong-executable", "wrong-digest", "missing", "extra"],
)
def test_successful_command_receipt_rejects_malformed_or_relabelled_evidence(
    fixture: Fixture,
    tamper: str,
):
    argv = (
        "/bin/launchctl",
        "bootstrap",
        f"gui/{fixture.paths.operator_uid}",
        str(fixture.paths.node_plist),
    )
    evidence = activate_module.CommandResult(
        argv,
        0,
        b"",
        b"",
        1,
        False,
    ).evidence("node_bootstrap")
    if tamper == "boolean-return":
        evidence["returnCode"] = False
    elif tamper == "wrong-executable":
        evidence["executable"] = "/usr/bin/true"
    elif tamper == "wrong-digest":
        evidence["argumentVectorSha256"] = "0" * 64
    elif tamper == "missing":
        del evidence["stderrSha256"]
    else:
        evidence["unexpected"] = True

    assert activate_module.receipt_has_successful_command(
        [evidence],
        "node_bootstrap",
        argv,
    ) is False


def test_clear_failed_bootstrap_does_not_create_receipt_authority(
    monkeypatch,
    fixture: Fixture,
):
    calls = 0

    def fake_run(argv, _timeout):
        nonlocal calls
        calls += 1
        return command_result(argv, returncode=0 if calls == 1 else 1)

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(activate_module.ActivationError, match="gateway_bootstrap failed"):
        backend.bootstrap_gateway_once()

    assert backend.bootstrap_bindings() == {}


def test_timed_out_bootstrap_retains_only_the_ambiguous_attempt_binding(
    monkeypatch,
    fixture: Fixture,
):
    calls = 0

    def fake_run(argv, _timeout):
        nonlocal calls
        calls += 1
        return command_result(
            argv,
            returncode=0 if calls == 1 else 124,
            timed_out=calls == 2,
        )

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(activate_module.ActivationError, match="gateway_bootstrap failed"):
        backend.bootstrap_gateway_once()

    binding = backend.bootstrap_bindings()[activate_module.OPERATOR.require_string('runtime.gateway_label')]
    assert binding["release"] == str(fixture.predecessor)
    assert binding["releaseDevice"] == os.lstat(fixture.predecessor).st_dev
    assert binding["releaseInode"] == os.lstat(fixture.predecessor).st_ino


def launchctl_payload(fixture: Fixture, label: str, *, pid: int = 100,
                      runs: int = 1, arguments: list[str] | None = None,
                      service_path: Path | None = None,
                      working_directory: Path | None = None) -> bytes:
    plist_path = fixture.paths.gateway_plist if label == activate_module.OPERATOR.require_string('runtime.gateway_label') else fixture.paths.node_plist
    persisted = plistlib.loads(plist_path.read_bytes())
    argv = arguments or persisted["ProgramArguments"]
    path = service_path or plist_path
    if label == activate_module.OPERATOR.require_string('runtime.gateway_label'):
        working_directory = working_directory or Path(persisted["WorkingDirectory"])
    argument_lines = "".join(f"\t\t{item}\n" for item in argv)
    working_line = (
        f"\tworking directory = {working_directory}\n"
        if working_directory is not None
        else ""
    )
    return (
        f"gui/{fixture.paths.operator_uid}/{label} = {{\n"
        f"\tpath = {path}\n"
        f"{working_line}"
        "\targuments = {\n"
        f"{argument_lines}"
        "\t}\n"
        f"\tpid = {pid}\n"
        f"\truns = {runs}\n"
        "}\n"
    ).encode()


def cwd_payload(release: Path, pid: int) -> bytes:
    info = os.lstat(release)
    return (f"p{pid}\0fcwd\0D{info.st_dev:x}\0i{info.st_ino}\0n{release}\0\n").encode()


def start_token_after_bootstrap(
    backend: activate_module.SystemBackend,
    label: str,
) -> str:
    started_at_us = backend._bootstrap_bindings[label]["startedAtUs"] + 1
    return f"darwin:{started_at_us // 1_000_000}:{started_at_us % 1_000_000}"


def test_lsof_cwd_parser_accepts_exact_real_null_newline_bytes(fixture: Fixture):
    pid = 4321
    info = os.lstat(fixture.candidate)
    payload = (
        f"p{pid}\0\nfcwd\0D{info.st_dev:x}\0i{info.st_ino}\0"
        f"n{fixture.candidate}\0\n"
    ).encode()

    assert activate_module.parse_lsof_cwd(payload, pid) == (
        fixture.candidate,
        info.st_dev,
        info.st_ino,
    )


def command_result(argv, returncode=0, stdout=b"", stderr=b"", timed_out=False):
    return activate_module.CommandResult(tuple(argv), returncode, stdout, stderr, 1, timed_out)


def install_system_backend_observation_mocks(
    monkeypatch,
    fixture: Fixture,
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, _timeout, **_kwargs):
        argv = tuple(argv)
        calls.append(argv)
        if argv[:2] == ("/bin/launchctl", "print"):
            label = argv[-1].rsplit("/", 1)[-1]
            pid = 100 if label == activate_module.OPERATOR.require_string('runtime.gateway_label') else 200
            return command_result(
                argv,
                stdout=launchctl_payload(fixture, label, pid=pid),
            )
        if "-iTCP:18789" in argv:
            return command_result(
                argv,
                stdout=b"p100\nf9\nn127.0.0.1:18789\n",
            )
        if argv[0] == "/usr/sbin/lsof" and "-d" in argv:
            return command_result(
                argv,
                stdout=cwd_payload(
                    activate_module.expected_gateway_working_directory(
                        fixture.paths
                    ),
                    100,
                ),
            )
        raise AssertionError(f"unexpected command: {argv}")

    bodies = {
        "/healthz": b'{"ok":true,"status":"live"}',
        "/readyz": b'{"ready":true,"failing":[],"uptimeMs":1}',
    }

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def read(self, _maximum):
            return self.payload

    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.path = None

        def request(self, _method, path):
            self.path = path

        def getresponse(self):
            return Response(bodies[self.path])

        def close(self):
            pass

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    monkeypatch.setattr(activate_module.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda pid: (
            "darwin:2:0",
            fixture.paths.node.resolve(strict=True),
            1,
        ),
    )
    return calls


def test_fresh_system_backend_replays_activated_receipt_without_lifecycle(
    monkeypatch,
    fixture: Fixture,
):
    first = run(fixture)
    calls = install_system_backend_observation_mocks(monkeypatch, fixture)
    backend = activate_module.SystemBackend(fixture.paths)
    monkeypatch.setattr(
        backend,
        "inspect_exec_approvals",
        lambda _release: approval_report(),
    )

    replay = activate_module.activate(
        fixture.paths,
        fixture.candidate,
        CANDIDATE_COMMIT,
        fixture.seal,
        fixture.snapshot,
        backend,
    )

    assert replay == first
    assert set(backend.bootstrap_bindings()) == {
        activate_module.OPERATOR.require_string('runtime.gateway_label'),
        activate_module.OPERATOR.require_string('runtime.node_label'),
    }
    assert all("enable" not in call and "bootstrap" not in call for call in calls)


def test_fresh_system_backend_receipt_binding_rejects_later_generation(
    monkeypatch,
    fixture: Fixture,
):
    first = run(fixture)
    install_system_backend_observation_mocks(monkeypatch, fixture)
    backend = activate_module.SystemBackend(
        replace(fixture.paths, health_timeout_seconds=0.0)
    )
    loaded = first["verification"]["loaded"]
    backend.bind_bootstrap_receipt(
        activate_module.OPERATOR.require_string('runtime.gateway_label'),
        loaded["bootstrapBinding"],
        loaded,
    )
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: (
            "darwin:3:0",
            fixture.paths.node.resolve(strict=True),
            1,
        ),
    )
    info = os.lstat(fixture.candidate)

    with pytest.raises(
        activate_module.ActivationError,
        match="receipt generation changed",
    ):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            info.st_dev,
            info.st_ino,
        )


def test_fresh_system_backend_late_verifies_bound_restore_without_lifecycle(
    monkeypatch,
    fixture: Fixture,
):
    assert run(
        fixture,
        FakeBackend(fixture, gateway_failure=True),
    )["outcome"] == "snapshot_restore_required"
    restored = restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )
    assert restored["outcome"] == "restored_boot_failed"
    assert set(restored["bootstrapBindings"]) == {
        activate_module.OPERATOR.require_string('runtime.gateway_label'),
        activate_module.OPERATOR.require_string('runtime.node_label'),
    }
    calls = install_system_backend_observation_mocks(monkeypatch, fixture)
    backend = activate_module.SystemBackend(fixture.paths)
    archive_dir = (
        fixture.paths.result.parent / "archive" / "real-system-late-restore"
    )

    receipt = activate_module.retire_terminal_receipts(
        fixture.paths,
        archive_dir,
        backend,
    )

    assert receipt["outcome"] == "restored_after_late_verification"
    assert receipt["lateVerification"]["gateway"]["loaded"]["pid"] == 100
    assert receipt["lateVerification"]["node"]["pid"] == 200
    assert all("enable" not in call and "bootstrap" not in call for call in calls)


def test_gateway_first_restore_failure_without_node_binding_is_not_retirable(
    fixture: Fixture,
):
    assert run(
        fixture,
        FakeBackend(fixture, gateway_failure=True),
    )["outcome"] == "snapshot_restore_required"
    restored = restore(
        fixture,
        StoppedRestoreBackend(fixture, gateway_failure=True),
    )
    assert restored["outcome"] == "restored_boot_failed"
    assert restored["bootstrapBindings"] == {}
    backend = FakeBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="restore receipt binding drift",
    ):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / "gateway-first-failure",
            backend,
        )

    assert backend.verifications == 0


def test_clear_node_bootstrap_rejection_cannot_be_retired_by_a_later_node(
    fixture: Fixture,
):
    assert run(
        fixture,
        FakeBackend(fixture, gateway_failure=True),
    )["outcome"] == "snapshot_restore_required"
    restored = restore(
        fixture,
        StoppedRestoreBackend(fixture, node_bootstrap_failure=True),
    )
    assert restored["outcome"] == "restored_boot_failed"
    assert set(restored["bootstrapBindings"]) == {
        activate_module.OPERATOR.require_string('runtime.gateway_label'),
    }
    backend = FakeBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="restore receipt binding drift",
    ):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / "clear-node-rejection",
            backend,
        )

    assert backend.verifications == 0


def test_ambiguous_node_bootstrap_without_observed_generation_is_not_retirable(
    fixture: Fixture,
):
    assert run(
        fixture,
        FakeBackend(fixture, gateway_failure=True),
    )["outcome"] == "snapshot_restore_required"
    restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )
    restore_path = restore_result_path(fixture)
    restored = json.loads(restore_path.read_text())
    node_evidence = next(
        item
        for item in restored["commandEvidence"]
        if item.get("purpose") == "node_bootstrap"
    )
    node_evidence["returnCode"] = 124
    node_evidence["timedOut"] = True
    restore_path.chmod(0o600)
    restore_path.write_text(json.dumps(restored, indent=2, sort_keys=True) + "\n")
    restore_path.chmod(0o400)
    backend = FakeBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="restore receipt binding drift",
    ):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / "ambiguous-node",
            backend,
        )

    assert backend.verifications == 0


def test_activated_replay_rejects_malformed_receipt_binding_before_verification(
    monkeypatch,
    fixture: Fixture,
):
    result = run(fixture)
    result["verification"]["loaded"]["bootstrapBinding"]["startedAtUs"] = "bad"
    fixture.paths.result.chmod(0o600)
    fixture.paths.result.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    fixture.paths.result.chmod(0o400)
    backend = activate_module.SystemBackend(fixture.paths)
    monkeypatch.setattr(
        backend,
        "inspect_exec_approvals",
        lambda _release: approval_report(),
    )

    with pytest.raises(
        activate_module.ActivationError,
        match="bootstrap receipt binding drift",
    ):
        activate_module.activate(
            fixture.paths,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.seal,
            fixture.snapshot,
            backend,
        )


def test_snapshot_capture_accepts_legacy_predecessor_and_observes_stopped(
    monkeypatch, fixture: Fixture
):
    assert not (
        fixture.predecessor / "dist" / "infra" / "exec-approvals-inspection.js"
    ).exists()
    assert not (
        fixture.predecessor
        / "dist"
        / "extensions"
        / "browser"
        / "chrome-extension"
    ).exists()
    calls = []
    backend = FakeBackend(fixture)

    def fake_run(argv, _timeout, **_kwargs):
        calls.append(argv)
        if argv[0] == "/usr/bin/tar":
            backend.events.append("tar")
            subprocess.run(argv, check=True, capture_output=True)
            return command_result(argv)
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    output_root = fixture.root / "observed-snapshot"
    manifest = output_root / "stopped-snapshot.json"

    receipt = activate_module.create_stopped_snapshot(
        fixture.paths,
        output_root,
        manifest,
        fixture.candidate,
        CANDIDATE_COMMIT,
        fixture.seal,
        backend,
    )

    assert receipt["manifestPath"] == str(manifest)
    assert receipt["tarVolumeUuid"] == "11111111-2222-3333-4444-555555555555"
    assert json.loads(manifest.read_text())["tarVolumeUuid"] == receipt["tarVolumeUuid"]
    assert [item[0] for item in calls] == ["/usr/bin/tar"]
    assert calls[0][4] == "-czpf"
    archive = output_root / "stopped-snapshot.tar"
    with archive.open("rb") as stream:
        assert stream.read(2) == b"\x1f\x8b"
    assert receipt["tarPath"] == str(archive)
    assert receipt["tarSha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert calls[0][8:] == (
        str(fixture.paths.predecessor_state_dir).lstrip("/"),
        str(fixture.paths.gateway_plist).lstrip("/"),
        str(fixture.paths.node_plist).lstrip("/"),
        str(fixture.paths.current_link).lstrip("/"),
    )
    assert "exec-approvals.json" not in "\0".join(calls[0])
    assert backend.events == ["stopped", "inspect", "tar", "stopped", "inspect"]
    g6 = json.loads((output_root / "stopped-quiescence.json").read_text())
    activate_module.validate_stopped_command_evidence(fixture.paths, g6["preCapture"])
    activate_module.validate_stopped_command_evidence(fixture.paths, g6["postCapture"])
    assert stat.S_IMODE((output_root / "stopped-snapshot.tar").stat().st_mode) == 0o400
    assert stat.S_IMODE((output_root / "stopped-quiescence.json").stat().st_mode) == 0o400
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o400


@pytest.mark.parametrize("with_uuid", [False, True])
def test_snapshot_old_and_uuid_shapes_keep_activation_and_restore_equality(fixture: Fixture, with_uuid: bool):
    if with_uuid:
        value = json.loads(fixture.snapshot.read_text())
        value["tarVolumeUuid"] = "11111111-2222-3333-4444-555555555555"
        fixture.snapshot.chmod(0o600)
        fixture.snapshot.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        fixture.snapshot.chmod(0o400)
    observed = activate_module.load_stopped_snapshot(fixture.paths, fixture.snapshot, sealed_candidate(fixture))
    assert ("tarVolumeUuid" in observed) is with_uuid
    failed = run(fixture, FakeBackend(fixture, gateway_failure=True))
    assert failed["outcome"] == "snapshot_restore_required"
    assert failed["snapshot"] == observed
    restored = restore(fixture, StoppedRestoreBackend(fixture))
    assert restored["outcome"] == "restored"


@pytest.mark.parametrize("uuid_value", ["invalid", "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"])
def test_snapshot_uuid_shape_rejects_invalid_or_different_volume(fixture: Fixture, uuid_value: str):
    value = json.loads(fixture.snapshot.read_text())
    value["tarVolumeUuid"] = uuid_value
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(value) + "\n")
    fixture.snapshot.chmod(0o400)
    with pytest.raises(activate_module.ActivationError, match="snapshot.*(binding|volume).*drift"):
        activate_module.load_stopped_snapshot(fixture.paths, fixture.snapshot, sealed_candidate(fixture))


def test_snapshot_rejects_raw_cas_drift_before_manifest_publication(
    monkeypatch, fixture: Fixture
):
    backend = FakeBackend(
        fixture,
        approval_reports=[
            approval_report(),
            approval_report(raw_cas_sha256="c" * 64),
        ],
    )

    def fake_run(argv, _timeout, **_kwargs):
        if argv[0] != "/usr/bin/tar":
            raise AssertionError(f"unexpected command: {argv}")
        backend.events.append("tar")
        Path(argv[5]).write_bytes(b"test stopped archive\n")
        return command_result(argv)

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    output_root = fixture.root / "drifted-snapshot"
    manifest = output_root / "stopped-snapshot.json"

    with pytest.raises(
        activate_module.ActivationError,
        match="protected surface drift",
    ):
        activate_module.create_stopped_snapshot(
            fixture.paths,
            output_root,
            manifest,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.seal,
            backend,
        )

    assert backend.events == ["stopped", "inspect", "tar", "stopped", "inspect"]
    assert not manifest.exists()


def test_snapshot_rejects_self_attested_quiescence(fixture: Fixture):
    manifest = json.loads(fixture.snapshot.read_text())
    g6 = Path(manifest["g6QuiescencePath"])
    g6.chmod(0o600)
    g6.write_text('{"gatewayStopped":true,"nodeStopped":true}\n')
    g6.chmod(0o400)
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps({
        **manifest,
        "g6QuiescenceSha256": hashlib.sha256(g6.read_bytes()).hexdigest(),
    }, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    with pytest.raises(activate_module.ActivationError, match="observed quiescence"):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


def test_snapshot_rejects_writable_manifest(fixture: Fixture):
    fixture.snapshot.chmod(0o600)
    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


def test_snapshot_rejects_inventory_count_drift(fixture: Fixture):
    manifest = json.loads(fixture.snapshot.read_text())
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps({
        **manifest,
        "inventoryCount": 5,
    }, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


def test_snapshot_rejects_extra_archive_member_even_with_rebound_hashes(
    fixture: Fixture,
):
    manifest = json.loads(fixture.snapshot.read_text())
    archive = Path(manifest["tarPath"])
    extra = fixture.root / "out-of-scope-snapshot-member.txt"
    extra.write_text("must never be restored\n")
    archive.chmod(0o600)
    archive.unlink()
    subprocess.run(
        [
            "/usr/bin/tar",
            "-cpf",
            str(archive),
            "-C",
            "/",
            str(fixture.paths.predecessor_state_dir).lstrip("/"),
            str(fixture.paths.gateway_plist).lstrip("/"),
            str(fixture.paths.node_plist).lstrip("/"),
            str(fixture.paths.current_link).lstrip("/"),
            str(extra).lstrip("/"),
        ],
        check=True,
        capture_output=True,
    )
    archive.chmod(0o400)
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()

    g6 = Path(manifest["g6QuiescencePath"])
    g6_value = json.loads(g6.read_text())
    g6_value["archiveSha256"] = archive_sha256
    g6.chmod(0o600)
    g6.write_text(json.dumps(g6_value, indent=2, sort_keys=True) + "\n")
    g6.chmod(0o400)

    manifest["tarSha256"] = archive_sha256
    manifest["g6QuiescenceSha256"] = hashlib.sha256(g6.read_bytes()).hexdigest()
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    with pytest.raises(
        activate_module.ActivationError,
        match="archive inventory drift",
    ):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


def test_snapshot_archive_accepts_appledouble_for_exact_surface(
    fixture: Fixture,
):
    archive = fixture.root / "appledouble-snapshot.tar"
    with tarfile.open(archive, "w") as output:
        for path in (
            fixture.paths.predecessor_state_dir,
            fixture.paths.gateway_plist,
            fixture.paths.node_plist,
            fixture.paths.current_link,
        ):
            output.add(path, arcname=str(path).lstrip("/"), recursive=True)
        gateway_appledouble = tarfile.TarInfo(
            str(
                fixture.paths.gateway_plist.with_name(
                    f"._{fixture.paths.gateway_plist.name}"
                )
            ).lstrip("/")
        )
        gateway_appledouble.size = 5
        output.addfile(gateway_appledouble, io.BytesIO(b"xattr"))

    activate_module.validate_snapshot_archive_members(
        fixture.paths,
        archive,
        str(fixture.predecessor),
    )


@pytest.mark.parametrize("tar_flags", ["-cpf", "-czpf"])
def test_snapshot_archive_preserves_native_state_hardlinks(fixture: Fixture, tar_flags: str):
    state = fixture.paths.predecessor_state_dir
    regular = state / "hardlink-source"
    linked = state / "hardlink-copy"
    regular.write_bytes(b"native stopped snapshot hardlink\n")
    os.link(regular, linked)
    relative_link = state / "relative-state-link"
    relative_link.symlink_to(regular.name)
    absolute_link = state / "absolute-state-link"
    absolute_link.symlink_to("/private/snapshot-test-target")
    archive = fixture.root / "native-hardlink-snapshot.tar"
    subprocess.run(
        [
            "/usr/bin/tar", "--xattrs", "--acls", "--fflags", tar_flags,
            str(archive), "-C", "/",
            *(str(path).lstrip("/") for path in (
                state, fixture.paths.gateway_plist,
                fixture.paths.node_plist, fixture.paths.current_link,
            )),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    with tarfile.open(archive) as captured:
        pair = [captured.getmember(str(path).lstrip("/")) for path in (regular, linked)]
        regular_member = next(member for member in pair if member.isfile())
        hardlink_member = next(member for member in pair if member.islnk())
        assert hardlink_member.linkname == regular_member.name
        assert hardlink_member.size == 0
        assert captured.getmember(str(relative_link).lstrip("/")).issym()
        assert captured.getmember(str(absolute_link).lstrip("/")).issym()

    activate_module.validate_snapshot_archive_members(
        fixture.paths, archive, str(fixture.predecessor)
    )
    restored = fixture.root / "restored-native-hardlinks"
    restored.mkdir()
    subprocess.run(
        [
            "/usr/bin/tar", "--xattrs", "--acls", "--fflags", "-xpf",
            str(archive), "-C", str(restored),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    restored_regular = restored / str(regular).lstrip("/")
    restored_linked = restored / str(linked).lstrip("/")
    assert restored_regular.read_bytes() == regular.read_bytes() == restored_linked.read_bytes()
    assert restored_regular.stat().st_ino == restored_linked.stat().st_ino
    for path in (relative_link, absolute_link, fixture.paths.current_link):
        assert os.readlink(restored / str(path).lstrip("/")) == os.readlink(path)


def test_snapshot_archive_large_valid_inventory_has_depth_bounded_work(fixture: Fixture):
    state = str(fixture.paths.predecessor_state_dir).lstrip("/")
    archive = fixture.root / "large-valid-inventory.tar"
    entries = [
        (state, tarfile.DIRTYPE, ""),
        (str(fixture.paths.gateway_plist).lstrip("/"), tarfile.REGTYPE, ""),
        (str(fixture.paths.node_plist).lstrip("/"), tarfile.REGTYPE, ""),
        (str(fixture.paths.current_link).lstrip("/"), tarfile.SYMTYPE, str(fixture.predecessor)),
    ]
    for index in range(1_000):
        entries.extend([
            (f"{state}/branch-{index}/regular", tarfile.REGTYPE, ""),
            (f"{state}/branch-{index}/alias", tarfile.SYMTYPE, "/private/metadata-target"),
        ])
    with tarfile.open(archive, "w") as output:
        for name, kind, target in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = target
            output.addfile(member)

    # Count executed validator operations, not elapsed time. Stop a regressed
    # Cartesian scan early rather than turning the regression into a slow test.
    budget = 32 * sum(name.count("/") + 1 for name, _, _ in entries)
    operations = 0
    validator_file = activate_module.validate_snapshot_archive_members.__code__.co_filename

    def count_work(frame, event, arg):
        nonlocal operations
        if event == "line" and frame.f_code.co_filename == validator_file:
            operations += 1
            assert operations <= budget, "archive validation exceeded total-path-depth work bound"
        return count_work

    previous_trace = sys.gettrace()
    try:
        sys.settrace(count_work)
        activate_module.validate_snapshot_archive_members(
            fixture.paths, archive, str(fixture.predecessor)
        )
    finally:
        sys.settrace(previous_trace)
    assert operations > 0


@pytest.mark.parametrize("case", ["link-first", "descendant-first", "duplicate"])
def test_snapshot_archive_rejects_unordered_link_descendants_and_duplicates(
    fixture: Fixture, case: str,
):
    state = str(fixture.paths.predecessor_state_dir).lstrip("/")
    archive = fixture.root / "unordered-symlink-descendant.tar"
    link = (f"{state}/alias", tarfile.SYMTYPE, "/private/metadata-target")
    child = (f"{state}/alias/deep/child", tarfile.REGTYPE, "")
    entries = [
        (state, tarfile.DIRTYPE, ""),
        (str(fixture.paths.gateway_plist).lstrip("/"), tarfile.REGTYPE, ""),
        (str(fixture.paths.node_plist).lstrip("/"), tarfile.REGTYPE, ""),
        (str(fixture.paths.current_link).lstrip("/"), tarfile.SYMTYPE, str(fixture.predecessor)),
        *([child, child] if case == "duplicate" else
          [child, link] if case == "descendant-first" else [link, child]),
    ]
    with tarfile.open(archive, "w") as output:
        for name, kind, target in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = target
            output.addfile(member)
    with pytest.raises(activate_module.ActivationError, match="archive inventory drift"):
        activate_module.validate_snapshot_archive_members(
            fixture.paths, archive, str(fixture.predecessor)
        )


def test_snapshot_archive_link_prefix_sibling_is_not_a_descendant(fixture: Fixture):
    state = fixture.paths.predecessor_state_dir
    (state / "alias").symlink_to("/private/metadata-target")
    sibling = state / "alias-sibling"
    sibling.mkdir()
    (sibling / "child").write_text("ordinary state\n")
    archive = fixture.root / "link-prefix-sibling.tar"
    with tarfile.open(archive, "w") as output:
        for path in (
            state, fixture.paths.gateway_plist, fixture.paths.node_plist, fixture.paths.current_link,
        ):
            output.add(path, arcname=str(path).lstrip("/"), recursive=True)
    activate_module.validate_snapshot_archive_members(
        fixture.paths, archive, str(fixture.predecessor)
    )


@pytest.mark.parametrize(
    "case",
    [
        "absolute-target", "outside-target", "sibling-prefix-target",
        "parent-traversal-target", "dot-target", "empty-component-target",
        "empty-target", "forward-target", "directory-target", "symlink-target",
        "hardlink-chain", "self-target", "hardlink-payload",
        "descendant-below-hardlink", "descendant-before-hardlink",
        "target-below-symlink", "gateway-hardlink", "current-hardlink", "fifo",
    ],
)
def test_snapshot_archive_rejects_unsafe_state_hardlinks(fixture: Fixture, case: str):
    state = str(fixture.paths.predecessor_state_dir).lstrip("/")
    source = f"{state}/linked"
    target = f"{state}/target"
    regular = (target, tarfile.REGTYPE, "", b"target bytes")
    linked = (source, tarfile.LNKTYPE, target, b"")
    entries = [regular, linked]
    unsafe_targets = {
        "absolute-target": f"/{target}",
        "outside-target": "private/outside",
        "sibling-prefix-target": f"{state}-sibling/target",
        "parent-traversal-target": f"{state}/../target",
        "dot-target": f"{state}/./target",
        "empty-component-target": f"{state}//target",
        "empty-target": "",
    }
    if case in unsafe_targets:
        entries[1] = (source, tarfile.LNKTYPE, unsafe_targets[case], b"")
    elif case == "forward-target":
        entries.reverse()
    elif case == "directory-target":
        entries[0] = (target, tarfile.DIRTYPE, "", b"")
    elif case == "symlink-target":
        entries[0] = (target, tarfile.SYMTYPE, "relative-target", b"")
    elif case == "hardlink-chain":
        entries = [
            (f"{state}/regular", tarfile.REGTYPE, "", b"target bytes"),
            (target, tarfile.LNKTYPE, f"{state}/regular", b""), linked,
        ]
    elif case == "self-target":
        entries = [(source, tarfile.LNKTYPE, source, b"")]
    elif case == "hardlink-payload":
        entries[1] = (source, tarfile.LNKTYPE, target, b"x")
    elif case in ("descendant-below-hardlink", "descendant-before-hardlink"):
        child = (f"{source}/child", tarfile.REGTYPE, "", b"child bytes")
        entries.insert(2 if case == "descendant-below-hardlink" else 1, child)
    elif case == "target-below-symlink":
        entries = [
            (f"{state}/alias", tarfile.SYMTYPE, "/private/snapshot-test-target", b""),
            (f"{state}/alias/target", tarfile.REGTYPE, "", b"target bytes"),
            (source, tarfile.LNKTYPE, f"{state}/alias/target", b""),
        ]
    elif case in ("gateway-hardlink", "current-hardlink"):
        source = str(
            fixture.paths.gateway_plist if case == "gateway-hardlink"
            else fixture.paths.current_link
        ).lstrip("/")
        entries[1] = (source, tarfile.LNKTYPE, target, b"")
    elif case == "fifo":
        entries = [(source, tarfile.FIFOTYPE, "", b"")]

    archive = fixture.root / f"unsafe-hardlink-{case}.tar"
    surfaces = [
        (state, tarfile.DIRTYPE, "", b""),
        (str(fixture.paths.gateway_plist).lstrip("/"), tarfile.REGTYPE, "", b"gateway"),
        (str(fixture.paths.node_plist).lstrip("/"), tarfile.REGTYPE, "", b"node"),
        (str(fixture.paths.current_link).lstrip("/"), tarfile.SYMTYPE, str(fixture.predecessor), b""),
    ]
    if case in ("gateway-hardlink", "current-hardlink"):
        surfaces = [entry for entry in surfaces if entry[0] != source]
    with tarfile.open(archive, "w") as output:
        for name, kind, linkname, payload in surfaces + entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = linkname
            member.size = len(payload)
            output.addfile(member, io.BytesIO(payload) if payload else None)

    with pytest.raises(activate_module.ActivationError, match="archive inventory drift"):
        activate_module.validate_snapshot_archive_members(
            fixture.paths, archive, str(fixture.predecessor)
        )


@pytest.mark.parametrize(
    ("target", "accepted"),
    [
        ("/example/forbidden/.ssh", True),
        ("../../outside-state", False),
        ("nested/target.txt", True),
    ],
)
def test_snapshot_archive_state_symlink_allows_bound_absolute_target(
    fixture: Fixture,
    target: str,
    accepted: bool,
):
    nested = fixture.paths.predecessor_state_dir / "nested"
    nested.mkdir()
    (nested / "target.txt").write_text("internal target\n")
    (fixture.paths.predecessor_state_dir / "state-link").symlink_to(target)
    archive = fixture.root / "state-symlink-snapshot.tar"
    subprocess.run(
        [
            "/usr/bin/tar",
            "-cpf",
            str(archive),
            "-C",
            "/",
            str(fixture.paths.predecessor_state_dir).lstrip("/"),
            str(fixture.paths.gateway_plist).lstrip("/"),
            str(fixture.paths.node_plist).lstrip("/"),
            str(fixture.paths.current_link).lstrip("/"),
        ],
        check=True,
        capture_output=True,
    )

    if accepted:
        activate_module.validate_snapshot_archive_members(
            fixture.paths,
            archive,
            str(fixture.predecessor),
        )
    else:
        with pytest.raises(
            activate_module.ActivationError,
            match="archive inventory drift",
        ):
            activate_module.validate_snapshot_archive_members(
                fixture.paths,
                archive,
                str(fixture.predecessor),
            )


def test_snapshot_archive_rejects_member_beneath_absolute_state_symlink(
    fixture: Fixture,
):
    state_link = fixture.paths.predecessor_state_dir / "state-link"
    state_link.symlink_to("/example/forbidden/.ssh")
    archive = fixture.root / "state-symlink-descendant-snapshot.tar"
    with tarfile.open(archive, "w") as output:
        for path in (
            fixture.paths.predecessor_state_dir,
            fixture.paths.gateway_plist,
            fixture.paths.node_plist,
            fixture.paths.current_link,
        ):
            output.add(path, arcname=str(path).lstrip("/"), recursive=True)
        injected = tarfile.TarInfo(f"{str(state_link).lstrip('/')}/injected")
        injected.size = 1
        output.addfile(injected, io.BytesIO(b"x"))

    archive.chmod(0o400)
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = json.loads(fixture.snapshot.read_text())
    g6 = Path(manifest["g6QuiescencePath"])
    g6_value = json.loads(g6.read_text())
    g6_value["archivePath"] = str(archive)
    g6_value["archiveSha256"] = archive_sha256
    g6.chmod(0o600)
    g6.write_text(json.dumps(g6_value, indent=2, sort_keys=True) + "\n")
    g6.chmod(0o400)
    manifest["tarPath"] = str(archive)
    manifest["tarSha256"] = archive_sha256
    manifest["g6QuiescenceSha256"] = hashlib.sha256(g6.read_bytes()).hexdigest()
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    with pytest.raises(
        activate_module.ActivationError,
        match="archive inventory drift",
    ):
        activate_module.load_stopped_snapshot(
            fixture.paths,
            fixture.snapshot,
            sealed_candidate(fixture),
        )


def test_snapshot_rejects_exec_approvals_inspector_candidate_drift(
    fixture: Fixture,
):
    manifest = json.loads(fixture.snapshot.read_text())
    manifest["execApprovalsInspector"]["candidateSealSha256"] = "0" * 64
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


def test_snapshot_rejects_retired_schema_v1(fixture: Fixture):
    manifest = json.loads(fixture.snapshot.read_text())
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(
        json.dumps(
            {**manifest, "schemaVersion": 1}, indent=2, sort_keys=True
        )
        + "\n"
    )
    fixture.snapshot.chmod(0o400)

    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


@pytest.mark.parametrize("inspector_kind", ["native-sdk", "historical-infra"])
def test_system_exec_approvals_inspector_uses_exact_candidate_and_private_paths(
    monkeypatch, fixture: Fixture, inspector_kind: str
):
    inspector = fixture.candidate / "dist" / "plugin-sdk" / "exec-approvals-runtime.js"
    if inspector_kind == "historical-infra":
        # Historical releases remain readable through the existing fallback;
        # default current candidates must exercise the published SDK path.
        legacy = fixture.candidate / "dist" / "infra" / "exec-approvals-inspection.js"
        chmod_tree(fixture.candidate, True)
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(inspector.read_bytes())
        chmod_tree(fixture.candidate, False)
        inspector = legacy
    calls: list[tuple[tuple[str, ...], Path | None, dict[str, str] | None]] = []
    monkeypatch.setenv(
        "OPENCLAW_UNRELATED_PRIVATE_VALUE",
        "ambient-private-value-must-not-reach-inspector",
    )

    def fake_run(argv, _timeout, *, cwd=None, env=None):
        calls.append((tuple(argv), cwd, env))
        return command_result(
            argv,
            stdout=json.dumps(approval_report(), separators=(",", ":")).encode(),
        )

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(
        fixture.paths, fixture.legacy_exec_approvals_path
    )

    receipt = backend.inspect_exec_approvals(fixture.candidate)

    assert receipt == approval_report()
    assert len(calls) == 1
    argv, cwd, environment = calls[0]
    assert argv[:3] == (
        str(fixture.paths.node),
        "--input-type=module",
        "--eval",
    )
    assert cwd == fixture.candidate
    assert environment is not None
    assert environment["OPENCLAW_STATE_DIR"] == str(
        fixture.paths.candidate_state_dir
    )
    assert environment["OPENCLAW_APPROVALS_INSPECTOR"] == str(inspector)
    assert 'typeof module.inspectExecApprovalsState !== "function"' in argv[3]
    assert "module.inspectExecApprovalsState({" in argv[3]
    assert environment["OPENCLAW_LEGACY_EXEC_APPROVALS_PATH"] == str(
        fixture.legacy_exec_approvals_path
    )
    assert set(environment) == {
        "OPENCLAW_STATE_DIR",
        "OPENCLAW_APPROVALS_INSPECTOR",
        "OPENCLAW_LEGACY_EXEC_APPROVALS_PATH",
    }
    assert "OPENCLAW_UNRELATED_PRIVATE_VALUE" not in environment
    assert str(fixture.legacy_exec_approvals_path) not in "\0".join(argv)
    assert backend.command_evidence[0]["purpose"] == "exec_approvals_inspection"


@pytest.mark.parametrize(
    "failure",
    [
        "nonzero",
        "timeout",
        "stderr",
        "malformed",
        "value-bearing",
        "raw-order",
    ],
)
def test_system_exec_approvals_inspector_fails_value_free(
    monkeypatch, fixture: Fixture, failure: str
):
    private = b"private-token-value-must-not-escape"

    def fake_run(argv, _timeout, *, cwd=None, env=None):
        assert cwd == fixture.candidate
        assert env is not None
        if failure == "nonzero":
            return command_result(argv, returncode=2, stderr=private)
        if failure == "timeout":
            return command_result(
                argv, returncode=-15, stderr=private, timed_out=True
            )
        if failure == "stderr":
            return command_result(
                argv,
                stdout=json.dumps(approval_report()).encode(),
                stderr=private,
            )
        if failure == "malformed":
            return command_result(argv, stdout=private)
        report = approval_report()
        if failure == "raw-order":
            report["openAIProfileOrder"] = list(SENSITIVE_TEST_PROFILE_IDS)
        else:
            report["socketToken"] = private.decode()
        return command_result(argv, stdout=json.dumps(report).encode())

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(
        fixture.paths, fixture.legacy_exec_approvals_path
    )

    with pytest.raises(activate_module.ActivationError) as caught:
        backend.inspect_exec_approvals(fixture.candidate)

    assert private.decode() not in str(caught.value)
    assert private.decode() not in json.dumps(backend.command_evidence)
    assert all(
        profile_id not in str(caught.value)
        and profile_id not in json.dumps(backend.command_evidence)
        for profile_id in SENSITIVE_TEST_PROFILE_IDS
    )


def _candidate_discord_cli_reports(fixture):
    source = fixture.candidate / "dist" / "extensions" / "discord" / "index.js"
    index = {"version": 1, "installRecords": {"whatsapp": {"source": "npm"}}, "diagnostics": []}
    return (
        {"state": "fresh", "current": copy.deepcopy(index), "persisted": copy.deepcopy(index)},
        {"plugin": {"id": "discord", "origin": "bundled", "source": str(source),
                    "status": "loaded", "enabled": True, "error": None, "channelIds": ["discord"]},
         "diagnostics": []},
    )


def test_system_candidate_discord_verifies_bundled_loader_without_mutation(
    monkeypatch, fixture: Fixture
):
    registry, inspection = _candidate_discord_cli_reports(fixture)
    captured = []

    def fake_run(argv, _timeout, *, cwd=None, env=None):
        captured.append({"argv": argv, "cwd": cwd, "env": env})
        report = registry if argv[2:] == ("plugins", "registry", "--json") else inspection
        return command_result(argv, stdout=json.dumps(report).encode())

    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", "/private/tmp/poisoned-openclaw.json")
    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    result = backend.verify_candidate_discord(
        fixture.candidate, fixture.paths.candidate_state_dir / "openclaw.json"
    )
    assert result == {
        "installRecordAbsent": True,
        "installedIds": ["whatsapp"],
        "configSha256": hashlib.sha256(fixture.config_bytes).hexdigest(),
        "origin": "bundled",
        "source": inspection["plugin"]["source"],
    }
    prefix = (str(fixture.paths.node), str(fixture.candidate / "openclaw.mjs"))
    assert [call["argv"] for call in captured] == [
        (*prefix, "plugins", "registry", "--json"),
        (*prefix, "plugins", "inspect", "discord", "--runtime", "--json"),
    ]
    assert [row["purpose"] for row in backend.command_evidence] == [
        "candidate_discord_registry", "candidate_discord_verification",
    ]
    # The native CLI owns these reads; no retired inline module/facade loader.
    assert all("--eval" not in call["argv"] and "--input-type=module" not in call["argv"]
               for call in captured)
    for call in captured:
        assert call["cwd"] == fixture.candidate
        assert call["env"]["OPENCLAW_STATE_DIR"] == str(fixture.paths.candidate_state_dir)
        assert call["env"]["OPENCLAW_CONFIG_PATH"] == str(fixture.paths.candidate_state_dir / "openclaw.json")
    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes


@pytest.mark.parametrize(
    ("drift", "message"),
    [("source", "source selection drift"), ("status", "source selection drift"),
     ("ambiguous-install", "source selection drift"), ("runtime-error", "source selection drift"),
     ("current-record", "stale Discord install record"), ("persisted-record", "stale Discord install record"),
     ("missing-records", "install-record proof drift"), ("state-shape", "install-record proof drift")],
)
def test_system_candidate_discord_rejects_noncanonical_bundled_selection(
    monkeypatch, fixture: Fixture, drift: str, message: str
):
    registry, inspection = _candidate_discord_cli_reports(fixture)
    if drift == "source":
        inspection["plugin"]["source"] = str(fixture.root / "external-discord" / "index.js")
    elif drift == "status":
        inspection["plugin"]["status"] = "failed"
    elif drift == "ambiguous-install":
        inspection["install"] = {"source": "npm"}
    elif drift == "runtime-error":
        inspection["diagnostics"] = [{"level": "error", "message": "Private diagnostic"}]
    elif drift == "current-record":
        registry["current"]["installRecords"]["discord"] = {"source": "npm"}
    elif drift == "persisted-record":
        registry["persisted"]["installRecords"]["discord"] = {"source": "npm"}
    elif drift == "missing-records":
        registry["current"].pop("installRecords")
    elif drift == "state-shape":
        registry["state"] = {}

    calls = []
    def fake_run(argv, _timeout, *, cwd=None, env=None):
        calls.append(argv)
        report = registry if argv[2:] == ("plugins", "registry", "--json") else inspection
        return command_result(argv, stdout=json.dumps(report).encode())

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    with pytest.raises(activate_module.ActivationError, match=message):
        backend.verify_candidate_discord(
            fixture.candidate, fixture.paths.candidate_state_dir / "openclaw.json"
        )
    assert len(calls) == (1 if drift in {"current-record", "persisted-record", "missing-records", "state-shape"} else 2)
    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes


@pytest.mark.parametrize("cli_drift", [None, "unsupported", "malformed", "stale-record", "config-change"])
def test_system_candidate_discord_executes_canonical_cli_with_real_node(
    fixture: Fixture, cli_drift: str | None
):
    pinned_node = activate_module.OPERATOR.require_path('paths.node_binary')
    node = pinned_node if pinned_node.is_file() else Path(shutil.which("node") or "")
    if not node.is_file():
        pytest.skip("real Node executable unavailable")

    chmod_tree(fixture.candidate, True)
    # This confined CLI fixture proves the actual transport and no-write checks;
    # the built candidate's genuine native plugin owners have a separate gate.
    cli = fixture.candidate / "openclaw.mjs"
    cli.write_text(
        'import fs from "node:fs";\n'
        'import path from "node:path";\n'
        'import { fileURLToPath } from "node:url";\n'
        f'const drift = {json.dumps(cli_drift)};\n'
        'const root = path.dirname(fileURLToPath(import.meta.url));\n'
        'const state = process.env.OPENCLAW_STATE_DIR;\n'
        'const configPath = process.env.OPENCLAW_CONFIG_PATH;\n'
        'const config = JSON.parse(fs.readFileSync(configPath, "utf8"));\n'
        'const records = JSON.parse(fs.readFileSync(path.join(state, "installed-records.json"), "utf8"));\n'
        'const args = process.argv.slice(2);\n'
        'fs.appendFileSync(path.join(state, "cli-invocations.jsonl"), JSON.stringify(args) + "\\n");\n'
        'if (drift === "unsupported") process.exit(7);\n'
        'if (drift === "malformed") { console.log("not JSON"); process.exit(0); }\n'
        'if (JSON.stringify(args) === JSON.stringify(["plugins", "registry", "--json"])) {\n'
        '  const installRecords = drift === "stale-record" ? {...records, discord: {source: "npm"}} : records;\n'
        '  const index = {version: 1, installRecords, diagnostics: []};\n'
        '  console.log(JSON.stringify({state: "fresh", current: index, persisted: index}));\n'
        '} else if (JSON.stringify(args) === JSON.stringify(["plugins", "inspect", "discord", "--runtime", "--json"])) {\n'
        '  if (drift === "config-change") fs.appendFileSync(configPath, " ");\n'
        '  console.log(JSON.stringify({plugin: {id: "discord", origin: "bundled",\n'
        '    source: path.join(root, "dist/extensions/discord/index.js"), status: "loaded",\n'
        '    enabled: config.plugins?.entries?.discord?.enabled !== false, error: null, channelIds: ["discord"]}, diagnostics: []}));\n'
        '} else process.exit(9);\n'
    )
    records = {"whatsapp": {"source": "npm", "installPath": "/managed/whatsapp"}}
    records_path = fixture.paths.candidate_state_dir / "installed-records.json"
    records_bytes = (json.dumps(records) + "\n").encode()
    records_path.write_bytes(records_bytes)
    paths = replace(fixture.paths, node=node.resolve(strict=True))
    decoy = fixture.root / "decoy.json"
    decoy.write_text('{"plugins":{"entries":{"discord":{"enabled":false}}}}\n')
    original_config_path = os.environ.get("OPENCLAW_CONFIG_PATH")
    os.environ["OPENCLAW_CONFIG_PATH"] = str(decoy)
    try:
        backend = activate_module.SystemBackend(paths)
        if cli_drift:
            message = {"unsupported": "verification failed", "malformed": "receipt malformed",
                       "stale-record": "stale Discord install record", "config-change": "source selection drift"}[cli_drift]
            with pytest.raises(activate_module.ActivationError, match=message):
                backend.verify_candidate_discord(fixture.candidate, paths.candidate_state_dir / "openclaw.json")
        else:
            receipt = backend.verify_candidate_discord(fixture.candidate, paths.candidate_state_dir / "openclaw.json")
            assert receipt["installRecordAbsent"] is True
            assert receipt["installedIds"] == ["whatsapp"]
    finally:
        if original_config_path is None:
            os.environ.pop("OPENCLAW_CONFIG_PATH", None)
        else:
            os.environ["OPENCLAW_CONFIG_PATH"] = original_config_path

    invocations = [json.loads(line) for line in (paths.candidate_state_dir / "cli-invocations.jsonl").read_text().splitlines()]
    assert invocations == ([['plugins', 'registry', '--json'], ['plugins', 'inspect', 'discord', '--runtime', '--json']]
                           if cli_drift in {None, 'config-change'} else [['plugins', 'registry', '--json']])
    assert records_path.read_bytes() == records_bytes
    assert (paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes + (b" " if cli_drift == "config-change" else b"")


def test_system_stopped_check_requires_exact_launchctl_service_not_found(monkeypatch, fixture: Fixture):
    seen = []

    def fake_run(argv, _timeout):
        if argv[0] == "/usr/sbin/lsof":
            return command_result(argv, 1)
        label = argv[-1].rsplit("/", 1)[-1]
        seen.append(label)
        stderr = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for user gui: '
            f"{fixture.paths.operator_uid}\n"
        ).encode()
        return command_result(argv, 113, stderr=stderr)

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    backend.assert_gateway_and_node_stopped()

    assert seen == [activate_module.OPERATOR.require_string('runtime.gateway_label'), activate_module.OPERATOR.require_string('runtime.node_label')]
    assert [item["purpose"] for item in backend.command_evidence] == [
        f"{activate_module.OPERATOR.require_string('runtime.gateway_label')}_launchctl_print",
        f"{activate_module.OPERATOR.require_string('runtime.node_label')}_launchctl_print",
        "gateway_listener_absence",
    ]


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [(1, b"Could not find service\n"), (113, b"permission denied\n"), (0, b"")],
)
def test_system_stopped_check_rejects_ambiguous_nonzero_or_loaded_service(
    monkeypatch, fixture: Fixture, returncode: int, stderr: bytes
):
    monkeypatch.setattr(
        activate_module,
        "run_bounded",
        lambda argv, _timeout: command_result(argv, returncode, stderr=stderr),
    )
    backend = activate_module.SystemBackend(fixture.paths)
    with pytest.raises(activate_module.ActivationError, match="exact stopped-state proof"):
        backend.assert_gateway_and_node_stopped()


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [(0, b"p999\nf9\nn127.0.0.1:18789\n", b""),
     (1, b"unexpected\n", b""), (2, b"", b"tool failure\n")],
)
def test_system_stopped_check_rejects_listener_or_ambiguous_lsof(
    monkeypatch, fixture: Fixture, returncode: int, stdout: bytes, stderr: bytes
):
    def fake_run(argv, _timeout):
        if argv[0] == "/usr/sbin/lsof":
            return command_result(argv, returncode, stdout=stdout, stderr=stderr)
        label = argv[-1].rsplit("/", 1)[-1]
        missing = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for user gui: '
            f"{fixture.paths.operator_uid}\n"
        ).encode()
        return command_result(argv, 113, stderr=missing)

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    with pytest.raises(activate_module.ActivationError, match="listener exact stopped-state"):
        backend.assert_gateway_and_node_stopped()


def verify_system_gateway_release(
    monkeypatch,
    fixture: Fixture,
    release: Path,
    gateway_pid: int,
) -> tuple[dict[str, object], dict[str, bytes]]:
    activate_module.atomic_symlink(fixture.paths.current_link, release)

    def fake_run(argv, _timeout):
        if argv[:2] == ("/bin/launchctl", "print"):
            return command_result(
                argv,
                stdout=launchctl_payload(
                    fixture, activate_module.OPERATOR.require_string('runtime.gateway_label'), pid=gateway_pid
                ),
            )
        if "-iTCP:18789" in argv:
            return command_result(
                argv, stdout=f"p{gateway_pid}\nf9\nn127.0.0.1:18789\n".encode()
            )
        return command_result(
            argv,
            stdout=cwd_payload(
                activate_module.expected_gateway_working_directory(fixture.paths),
                gateway_pid,
            ),
        )

    bodies = {
        "/healthz": b'{"ok":true,"status":"live"}',
        "/readyz": b'{"ready":true,"failing":[],"uptimeMs":1}',
    }

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def read(self, _maximum):
            return self.payload

    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.path = None

        def request(self, _method, path):
            self.path = path

        def getresponse(self):
            return Response(bodies[self.path])

        def close(self):
            pass

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    monkeypatch.setattr(activate_module.http.client, "HTTPConnection", Connection)
    backend = activate_module.SystemBackend(fixture.paths)
    backend.bootstrap_gateway_once()

    def identity(pid):
        assert pid == gateway_pid
        return (
            start_token_after_bootstrap(backend, activate_module.OPERATOR.require_string('runtime.gateway_label')),
            fixture.paths.node.resolve(strict=True),
            1,
        )

    monkeypatch.setattr(activate_module, "process_identity", identity)
    result = backend.verify(
        release,
        os.lstat(release).st_dev,
        os.lstat(release).st_ino,
    )
    return result, bodies


def test_system_gateway_verification_binds_service_listener_generation_and_json(
    monkeypatch, fixture: Fixture
):
    gateway_pid = 100
    result, bodies = verify_system_gateway_release(
        monkeypatch,
        fixture,
        fixture.candidate,
        gateway_pid,
    )

    assert result["loaded"]["pid"] == gateway_pid
    assert "supervisorPid" not in result["loaded"]
    assert "directSupervisorChild" not in result["loaded"]
    assert result["loaded"]["directLaunchdOwner"] is True
    assert result["loaded"]["listenerPidMatchesLaunchdPid"] is True
    assert result["loaded"]["executable"] == str(fixture.paths.node.resolve(strict=True))
    assert result["loaded"]["argumentsObservedExact"] is True
    working_directory = activate_module.expected_gateway_working_directory(fixture.paths)
    working_directory_info = os.lstat(working_directory)
    assert result["loaded"]["workingDirectory"] == str(working_directory)
    assert result["loaded"]["workingDirectoryDevice"] == working_directory_info.st_dev
    assert result["loaded"]["workingDirectoryInode"] == working_directory_info.st_ino
    assert result["loaded"]["bootstrapBinding"]["release"] == str(fixture.candidate)
    assert result["health"]["healthz"]["bodySha256"] == hashlib.sha256(bodies["/healthz"]).hexdigest()
    assert result["health"]["readyz"]["bodySha256"] == hashlib.sha256(bodies["/readyz"]).hexdigest()


def test_system_restore_verifier_accepts_snapshot_bound_direct_predecessor(
    monkeypatch, fixture: Fixture
):
    gateway_pid = 101
    result, bodies = verify_system_gateway_release(
        monkeypatch,
        fixture,
        fixture.predecessor,
        gateway_pid,
    )

    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)
    assert result["loaded"]["release"] == str(fixture.predecessor)
    assert result["loaded"]["pid"] == gateway_pid
    assert result["loaded"]["directLaunchdOwner"] is True
    assert result["loaded"]["listenerPidMatchesLaunchdPid"] is True
    assert result["loaded"]["executable"] == str(fixture.paths.node.resolve(strict=True))
    assert result["health"]["healthz"]["bodySha256"] == hashlib.sha256(
        bodies["/healthz"]
    ).hexdigest()
    assert result["health"]["readyz"]["bodySha256"] == hashlib.sha256(
        bodies["/readyz"]
    ).hexdigest()


def test_system_node_verification_binds_entrypoint_executable_and_generation(
    monkeypatch, fixture: Fixture
):
    activate_module.atomic_symlink(fixture.paths.current_link, fixture.candidate)
    node_pid = 300
    monkeypatch.setattr(
        activate_module,
        "run_bounded",
        lambda argv, _timeout: command_result(
            argv,
            stdout=launchctl_payload(
                fixture, activate_module.OPERATOR.require_string('runtime.node_label'), pid=node_pid, runs=2
            ),
        ),
    )
    backend = activate_module.SystemBackend(fixture.paths)
    backend.bootstrap_node_once()
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda pid: (
            start_token_after_bootstrap(backend, activate_module.OPERATOR.require_string('runtime.node_label')),
            fixture.paths.node.resolve(strict=True),
            1,
        ),
    )
    info = os.lstat(fixture.candidate)

    result = backend.verify_node(fixture.candidate, info.st_dev, info.st_ino)

    assert result["pid"] == node_pid
    assert result["runs"] == 2
    assert result["startToken"] == start_token_after_bootstrap(
        backend,
        activate_module.OPERATOR.require_string('runtime.node_label'),
    )
    assert result["executable"] == str(fixture.paths.node.resolve(strict=True))
    assert result["argumentsObservedExact"] is True


def test_system_health_rejects_arbitrary_http_200_json(monkeypatch, fixture: Fixture):
    class Response:
        status = 200

        def read(self, _maximum):
            return b'{"status":"fine"}'

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, _method, _path):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(activate_module.http.client, "HTTPConnection", Connection)
    backend = activate_module.SystemBackend(fixture.paths)
    assert backend._probe("/healthz")["accepted"] is False
    assert backend._probe("/readyz")["accepted"] is False


def test_system_loaded_service_rejects_argv_or_definition_path_drift(monkeypatch, fixture: Fixture):
    persisted = plistlib.loads(fixture.paths.gateway_plist.read_bytes())["ProgramArguments"]
    drifted = [*persisted, "--unexpected"]

    monkeypatch.setattr(
        activate_module,
        "run_bounded",
        lambda argv, _timeout: command_result(
            argv,
            stdout=launchctl_payload(
                fixture,
                activate_module.OPERATOR.require_string('runtime.gateway_label'),
                arguments=drifted,
                service_path=fixture.root / "wrong.plist",
            ),
        ),
    )
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: ("generation", fixture.paths.node.resolve(strict=True), 1),
    )
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(activate_module.ActivationError, match="does not match its plist"):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            os.lstat(fixture.candidate).st_dev,
            os.lstat(fixture.candidate).st_ino,
        )


@pytest.mark.parametrize("working_directory", ["missing", "resolved-release"])
def test_system_loaded_gateway_requires_operator_home_working_directory(
    monkeypatch,
    fixture: Fixture,
    working_directory: str,
):
    activate_module.atomic_symlink(fixture.paths.current_link, fixture.candidate)
    payload = launchctl_payload(
        fixture,
        activate_module.OPERATOR.require_string('runtime.gateway_label'),
        working_directory=fixture.candidate,
    )
    if working_directory == "missing":
        payload = payload.replace(
            f"\tworking directory = {fixture.candidate}\n".encode(),
            b"",
        )

    def fake_run(argv, _timeout):
        if argv[:2] == ("/bin/launchctl", "print"):
            return command_result(argv, stdout=payload)
        return command_result(
            argv,
            stdout=cwd_payload(
                activate_module.expected_gateway_working_directory(fixture.paths),
                100,
            ),
        )

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: (
            "gateway-generation",
            fixture.paths.node.resolve(strict=True),
            1,
        ),
    )
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(activate_module.ActivationError):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            os.lstat(fixture.candidate).st_dev,
            os.lstat(fixture.candidate).st_ino,
        )


def test_system_loaded_gateway_rejects_process_cwd_outside_operator_home(
    monkeypatch,
    fixture: Fixture,
):
    activate_module.atomic_symlink(fixture.paths.current_link, fixture.candidate)

    def fake_run(argv, _timeout):
        if argv[:2] == ("/bin/launchctl", "print"):
            return command_result(
                argv,
                stdout=launchctl_payload(
                    fixture,
                    activate_module.OPERATOR.require_string('runtime.gateway_label'),
                ),
            )
        return command_result(argv, stdout=cwd_payload(fixture.candidate, 100))

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: (
            "gateway-generation",
            fixture.paths.node.resolve(strict=True),
            1,
        ),
    )
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(
        activate_module.ActivationError,
        match="gateway working directory identity drift",
    ):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            os.lstat(fixture.candidate).st_dev,
            os.lstat(fixture.candidate).st_ino,
        )


def test_system_loaded_gateway_rejects_non_pinned_executable(
    monkeypatch,
    fixture: Fixture,
):
    activate_module.atomic_symlink(fixture.paths.current_link, fixture.candidate)

    def fake_run(argv, _timeout):
        if argv[:2] == ("/bin/launchctl", "print"):
            return command_result(
                argv,
                stdout=launchctl_payload(
                    fixture,
                    activate_module.OPERATOR.require_string('runtime.gateway_label'),
                ),
            )
        return command_result(
            argv,
            stdout=cwd_payload(
                activate_module.expected_gateway_working_directory(fixture.paths),
                100,
            ),
        )

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    backend.bootstrap_gateway_once()
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: (
            start_token_after_bootstrap(backend, activate_module.OPERATOR.require_string('runtime.gateway_label')),
            Path("/usr/bin/python3").resolve(strict=True),
            1,
        ),
    )

    with pytest.raises(
        activate_module.ActivationError,
        match="node service process identity drift",
    ):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            os.lstat(fixture.candidate).st_dev,
            os.lstat(fixture.candidate).st_ino,
        )


def test_system_loaded_gateway_rejects_selector_retarget_with_stable_pid(
    monkeypatch,
    fixture: Fixture,
):
    activate_module.atomic_symlink(fixture.paths.current_link, fixture.predecessor)

    def fake_run(argv, _timeout):
        if argv[:2] == ("/bin/launchctl", "print"):
            return command_result(
                argv,
                stdout=launchctl_payload(
                    fixture,
                    activate_module.OPERATOR.require_string('runtime.gateway_label'),
                ),
            )
        return command_result(
            argv,
            stdout=cwd_payload(
                activate_module.expected_gateway_working_directory(fixture.paths),
                100,
            ),
        )

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    backend.bootstrap_gateway_once()
    start_token = start_token_after_bootstrap(backend, activate_module.OPERATOR.require_string('runtime.gateway_label'))
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: (start_token, fixture.paths.node.resolve(strict=True), 1),
    )

    activate_module.atomic_symlink(fixture.paths.current_link, fixture.candidate)

    with pytest.raises(
        activate_module.ActivationError,
        match="loaded ai.openclaw.gateway bootstrap selector changed",
    ):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            os.lstat(fixture.candidate).st_dev,
            os.lstat(fixture.candidate).st_ino,
        )


def test_system_listener_rejects_pid_distinct_from_launchd_owner(
    monkeypatch, fixture: Fixture
):
    listener_pid = 200
    monkeypatch.setattr(
        activate_module,
        "run_bounded",
        lambda argv, _timeout: command_result(
            argv, stdout=f"p{listener_pid}\nf9\nn127.0.0.1:18789\n".encode()
        ),
    )
    backend = activate_module.SystemBackend(fixture.paths)
    with pytest.raises(
        activate_module.ActivationError,
        match="listener PID does not match launchd PID",
    ):
        backend._listener(100)


def test_system_gateway_verification_reacquires_replaced_launchd_generation(
    monkeypatch, fixture: Fixture
):
    backend = activate_module.SystemBackend(fixture.paths)
    first = {
        "release": str(fixture.candidate),
        "releaseDevice": 1,
        "releaseInode": 2,
        "pid": 100,
        "runs": 1,
        "startToken": "gateway-one",
        "executable": str(fixture.paths.node),
        "servicePath": str(fixture.paths.gateway_plist),
        "argumentsObservedExact": True,
        "argumentVectorSha256": "a" * 64,
        "bootstrapBinding": {
            "release": str(fixture.candidate),
            "releaseDevice": 1,
            "releaseInode": 2,
            "selectorDevice": 3,
            "selectorInode": 4,
            "startedAtUs": 5,
        },
    }
    second = {
        **first,
        "pid": 101,
        "runs": 2,
        "startToken": "gateway-two",
    }
    loaded = iter([first, second, second])
    monkeypatch.setattr(backend, "_loaded", lambda *_args: dict(next(loaded)))

    def listener(launchd_pid):
        if launchd_pid == 100:
            raise activate_module.ActivationError(
                "gateway listener PID does not match launchd PID"
            )
        return launchd_pid

    monkeypatch.setattr(
        backend,
        "_listener",
        listener,
    )
    monkeypatch.setattr(
        backend,
        "_wait_health",
        lambda _deadline: {"healthz": {"accepted": True}, "readyz": {"accepted": True}},
    )

    result = backend.verify(fixture.candidate, 1, 2)

    assert result["loaded"]["pid"] == 101
    assert result["loaded"]["runs"] == 2


def test_system_gateway_verification_rejects_generation_that_never_stabilizes(
    monkeypatch, fixture: Fixture
):
    backend = activate_module.SystemBackend(
        replace(fixture.paths, health_timeout_seconds=0.5)
    )
    loaded = {
        "release": str(fixture.candidate),
        "releaseDevice": 1,
        "releaseInode": 2,
        "pid": 100,
        "runs": 1,
        "startToken": "gateway",
        "executable": str(fixture.paths.node),
        "servicePath": str(fixture.paths.gateway_plist),
        "argumentsObservedExact": True,
        "argumentVectorSha256": "a" * 64,
        "bootstrapBinding": {
            "release": str(fixture.candidate),
            "releaseDevice": 1,
            "releaseInode": 2,
            "selectorDevice": 3,
            "selectorInode": 4,
            "startedAtUs": 5,
        },
    }
    monotonic = iter([0.0, 1.0])
    generations = iter(
        [
            dict(loaded),
            {**loaded, "startToken": "replacement-gateway"},
        ]
    )
    monkeypatch.setattr(backend, "_loaded", lambda *_args: next(generations))
    monkeypatch.setattr(backend, "_listener", lambda launchd_pid: launchd_pid)
    monkeypatch.setattr(
        backend,
        "_wait_health",
        lambda _deadline: {"healthz": {"accepted": True}, "readyz": {"accepted": True}},
    )
    monkeypatch.setattr(activate_module.time, "monotonic", lambda: next(monotonic))

    with pytest.raises(activate_module.ActivationError, match="stable gateway generation"):
        backend.verify(fixture.candidate, 1, 2)


# Explicit Node transitions use the existing snapshot/fence/activation/restore
# owner. Only OS ScreenCapture facts are isolated; filesystem effects are real
# within the disposable fixture, with no real launchctl or Node process.
_node_transition_system_backend = activate_module.SystemBackend


class NodeTransitionBackend(StoppedRestoreBackend):
    def __init__(self, fixture, *, paths=None, **kwargs):
        super().__init__(fixture, **kwargs)
        self.paths = fixture.paths if paths is None else paths
        self.boot_nodes = []

    def use_runtime_paths(self, paths):
        _node_transition_system_backend.use_runtime_paths(self, paths)

    def verify_screen_capture_continuity(self):
        return _node_transition_system_backend.verify_screen_capture_continuity(self)

    def bootstrap_gateway_once(self):
        plists = activate_module.validate_plists(self.paths)
        assert all(plists[label]["sha256"] for label in ("gateway", "node"))
        activate_module.validate_support_links(self.paths)
        self.boot_nodes.append(self.paths.node)
        super().bootstrap_gateway_once()

    def bootstrap_node_once(self):
        self.boot_nodes.append(self.paths.node)
        super().bootstrap_node_once()


@pytest.fixture
def node_transition_fixture(fixture, monkeypatch):
    predecessor_node = fixture.paths.node
    candidate_node = predecessor_node.parent.parent.parent / "node-v24.22.1" / "bin" / "node"
    candidate_node.parent.mkdir(parents=True)
    candidate_node.write_text("#!/bin/sh\nprintf 'candidate-node-fixture\\n'\n")
    candidate_node.chmod(0o555)
    binding = fixture.paths.predecessor_state_dir / "screen-capture-binding.json"
    binding.write_text('{"syntheticPredecessor":true}\n')
    binding.chmod(0o400)
    prior_sha = hashlib.sha256(binding.read_bytes()).hexdigest()
    new_sha = hashlib.sha256(candidate_node.read_bytes()).hexdigest()
    precondition = fixture.root / "candidate-screen-capture-precondition.json"
    precondition.write_text(json.dumps({
        "schemaVersion": 2,
        "kind": "node_toolchain_screen_capture_precondition",
        "predecessorNode": str(predecessor_node),
        "predecessorBinding": {"path": str(binding), "sha256": prior_sha},
        "candidateNode": str(candidate_node), "candidateSha256": new_sha,
        "effectiveCaptureVerified": False, "scheduledSyncProven": False,
        "effectiveFilesystemAccessVerified": False,
        "filesystemPermission": {"service": "kTCCServiceSystemPolicyAllFiles", "authValue": 2},
        "tccMutated": False,
    }, sort_keys=True) + "\n")
    precondition.chmod(0o400)
    precondition_sha = hashlib.sha256(precondition.read_bytes()).hexdigest()

    def old_binding(path, node, **_):
        assert path == binding and node == predecessor_node
        assert hashlib.sha256(path.read_bytes()).hexdigest() == prior_sha
        return {"bindingSha256": prior_sha, "effectiveCaptureVerified": True}

    fixture.node_transition_precondition_calls = []
    def candidate_precondition(path, node, *, expected_sha256):
        fixture.node_transition_precondition_calls.append({
            "path": path, "node": node, "sha256": expected_sha256,
            "fenceExists": activate_module._start_fence_path(fixture.paths).exists(),
            "selected": fixture.paths.current_link.resolve()})
        assert path == precondition and node == candidate_node
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise activate_module.ActivationError("fixture candidate precondition digest drift")
        return {
            "kind": "node_toolchain_screen_capture_precondition",
            "preconditionSha256": expected_sha256, "candidateNodeSha256": new_sha,
            "predecessorBindingSha256": prior_sha,
            "candidateCodeIdentityVerified": True,
            "recordedCandidatePermissionVerified": True,
            "recordedCandidateFilesystemPermissionVerified": True,
            "effectiveFilesystemAccessVerified": False,
            "effectiveCaptureVerified": False, "scheduledSyncProven": False,
        }

    monkeypatch.setattr(activate_module, "verify_screen_capture_binding", old_binding)
    monkeypatch.setattr(activate_module, "verify_node_toolchain_screen_capture_precondition",
                        candidate_precondition, raising=False)
    node_command_alias = fixture.root / ".local/bin/node"
    node_prefix_alias = fixture.root / ".local/opt/node-current"
    node_command_alias.parent.mkdir(parents=True)
    node_prefix_alias.parent.mkdir(parents=True)
    node_command_alias.symlink_to(predecessor_node)
    node_prefix_alias.symlink_to(predecessor_node.parent.parent)
    cli_wrapper = fixture.root / ".local/bin/openclaw"
    cli_wrapper.write_text(f'#!/bin/sh\nexec {predecessor_node} "{fixture.root}/host-cli-fixture.mjs" "$@"\n')
    cli_wrapper.chmod(0o755)
    wrapper_before = cli_wrapper.read_bytes()
    wrapper_after = wrapper_before.replace(
        ("exec " + str(predecessor_node) + " ").encode(),
        ("exec " + str(fixture.paths.node_alias / "bin/node") + " ").encode(), 1)
    transition_type = getattr(activate_module, "NodeToolchainTransition", SimpleNamespace)
    transition = transition_type(candidate=candidate_node,
        predecessor_sha256=hashlib.sha256(predecessor_node.read_bytes()).hexdigest(),
        candidate_sha256=new_sha, candidate_screen_capture_precondition=precondition,
        candidate_screen_capture_precondition_sha256=precondition_sha,
        compatibility_aliases=((node_command_alias, str(predecessor_node)),
                               (node_prefix_alias, str(predecessor_node.parent.parent))),
        cli_wrapper_predecessor_sha256=hashlib.sha256(wrapper_before).hexdigest(),
        cli_wrapper_candidate_sha256=hashlib.sha256(wrapper_after).hexdigest(),
        cli_wrapper_mode=0o755)
    fixture.paths = replace(fixture.paths, screen_capture_binding=binding, operator_home=fixture.root)
    if "node_toolchain_transition" in activate_module.ActivationPaths.__dataclass_fields__:
        fixture.paths = replace(fixture.paths, node_toolchain_transition=transition)
    else:
        # RED uses the original normal owner, which ignores this test-only
        # declaration. It must capture/boot normally, then fail on observed
        # four-base inventory and old executable, not on an absent new API.
        object.__setattr__(fixture.paths, "node_toolchain_transition", transition)
    for plist in (fixture.paths.gateway_plist, fixture.paths.node_plist):
        value = plistlib.loads(plist.read_bytes())
        value["EnvironmentVariables"]["PATH"] = f"{predecessor_node.parent}:/usr/bin:/bin"
        value["EnvironmentVariables"]["UNRELATED_SENTINEL"] = "preserve"
        plist.write_bytes(plistlib.dumps(value))
    fixture.node_transition_original = {
        "gateway": fixture.paths.gateway_plist.read_bytes(),
        "node": fixture.paths.node_plist.read_bytes(),
        "predecessorNode": predecessor_node.read_bytes(),
        "candidateNode": candidate_node.read_bytes(),
        "alias": os.readlink(fixture.paths.node_alias),
        "compatibilityAliases": {str(node_command_alias): os.readlink(node_command_alias),
                                 str(node_prefix_alias): os.readlink(node_prefix_alias)},
        "wrapper": wrapper_before, "wrapperAfter": wrapper_after,
    }
    output = fixture.root / "node-transition-snapshot"
    fixture.snapshot = output / "stopped-snapshot.json"
    activate_module.create_stopped_snapshot(
        fixture.paths, output, fixture.snapshot, fixture.candidate,
        CANDIDATE_COMMIT, fixture.seal, NodeTransitionBackend(fixture))
    return fixture



def test_node_transition_snapshot_is_fifth_base_member(node_transition_fixture):
    item = node_transition_fixture
    snapshot = json.loads(item.snapshot.read_text())
    assert snapshot["schemaVersion"] == 5 and snapshot["inventoryCount"] == 8
    assert snapshot["recoveryRoots"] == []
    assert activate_module.snapshot_members(item.paths)[4] == item.paths.node_alias
    with tarfile.open(snapshot["tarPath"]) as archive:
        member = archive.getmember(str(item.paths.node_alias).lstrip("/"))
        assert member.issym() and member.linkname == item.node_transition_original["alias"]
    assert snapshot["nodeToolchain"]["candidatePlists"]["gateway"]["predecessorSha256"] == snapshot["gatewayPlistSha256"]
    assert len(activate_module.snapshot_members(item.paths)) == 8
    assert set(snapshot["nodeToolchain"]["cliWrapper"]) == {"path", "mode", "predecessorSha256", "sha256"}
    with tarfile.open(snapshot["tarPath"]) as archive:
        for path, target in item.node_transition_original["compatibilityAliases"].items():
            member = archive.getmember(path.lstrip("/"))
            assert member.issym() and member.linkname == target
        member = archive.getmember(str(item.root / ".local/bin/openclaw").lstrip("/"))
        assert member.isfile() and archive.extractfile(member).read() == item.node_transition_original["wrapper"]


def test_node_transition_applies_one_matched_pair_and_idempotent_receipt(node_transition_fixture):
    item = node_transition_fixture
    backend = NodeTransitionBackend(item)
    result = run(item, backend)
    new_node = item.paths.node_toolchain_transition.candidate
    assert result["outcome"] == "activated"
    assert backend.boot_nodes == [new_node, new_node]
    assert (backend.gateway_boots, backend.node_boots) == (1, 1)
    assert os.readlink(item.paths.node_alias) == str(new_node.parent.parent)
    assert item.paths.node.read_bytes() == item.node_transition_original["predecessorNode"]
    assert new_node.read_bytes() == item.node_transition_original["candidateNode"]
    for plist in (item.paths.gateway_plist, item.paths.node_plist):
        value = plistlib.loads(plist.read_bytes())
        assert value["ProgramArguments"][0] == str(new_node)
        assert value["EnvironmentVariables"]["PATH"] == f"{new_node.parent}:/usr/bin:/bin"
        assert value["EnvironmentVariables"]["UNRELATED_SENTINEL"] == "preserve"
    assert result["verification"]["screenCaptureContinuityPending"] is True
    assert result["verification"]["screenCapturePrecondition"]["effectiveCaptureVerified"] is False
    assert "screenCaptureContinuity" not in result["verification"]
    calls = item.node_transition_precondition_calls
    assert calls[0]["fenceExists"] is False and calls[0]["selected"] == item.predecessor
    assert calls[-1]["fenceExists"] is True and calls[-1]["selected"] == item.candidate
    assert all(row["sha256"] == item.paths.node_toolchain_transition.candidate_screen_capture_precondition_sha256
               for row in calls)
    controlled = [item.paths.node_alias, item.root / ".local/bin/node",
                  item.root / ".local/opt/node-current", item.root / ".local/bin/openclaw"]
    inodes = [path.lstat().st_ino for path in controlled]
    assert run(item, backend) == result
    assert [path.lstat().st_ino for path in controlled] == inodes
    assert (backend.gateway_boots, backend.node_boots) == (1, 1)
    assert (item.root / ".local/bin/openclaw").read_bytes() == item.node_transition_original["wrapperAfter"]
    assert os.readlink(item.root / ".local/bin/node") == str(item.paths.node_alias / "bin/node")
    assert os.readlink(item.root / ".local/opt/node-current") == str(item.paths.node_alias)


@pytest.mark.parametrize("drift", ["old-digest", "new-digest", "alias", "plist", "precondition"])
def test_node_transition_rejects_drift_before_start_fence(node_transition_fixture, drift):
    item = node_transition_fixture
    transition = item.paths.node_toolchain_transition
    if drift == "old-digest":
        item.paths = replace(item.paths, node_toolchain_transition=replace(transition, predecessor_sha256="0" * 64))
    elif drift == "new-digest":
        item.paths = replace(item.paths, node_toolchain_transition=replace(transition, candidate_sha256="0" * 64))
    elif drift == "alias":
        item.paths.node_alias.unlink()
        item.paths.node_alias.symlink_to(transition.candidate.parent.parent)
    elif drift == "plist":
        value = plistlib.loads(item.paths.node_plist.read_bytes())
        value["EnvironmentVariables"]["UNRELATED_SENTINEL"] = "drift"
        item.paths.node_plist.write_bytes(plistlib.dumps(value))
    else:
        item.paths = replace(item.paths, node_toolchain_transition=replace(
            transition, candidate_screen_capture_precondition_sha256="0" * 64))
    backend = NodeTransitionBackend(item)
    result = run(item, backend)
    assert result["outcome"] == "failed_before_apply"
    assert (backend.gateway_boots, backend.node_boots) == (0, 0)
    assert not activate_module._start_fence_path(item.paths).exists()
    assert item.paths.current_link.resolve() == item.predecessor


@pytest.mark.parametrize("failure", ["gateway", "node"])
def test_node_transition_boot_failure_restores_old_pair_without_replay(node_transition_fixture, failure):
    item = node_transition_fixture
    backend = NodeTransitionBackend(item, gateway_failure=failure == "gateway",
                                    node_bootstrap_failure=failure == "node")
    result = run(item, backend)
    assert result["outcome"] == "snapshot_restore_required"
    recovery = NodeTransitionBackend(item)
    restored = restore(item, recovery)
    assert restored["outcome"] == "restored_stopped"
    assert (recovery.gateway_boots, recovery.node_boots) == (0, 0)
    assert os.readlink(item.paths.node_alias) == item.node_transition_original["alias"]
    assert item.paths.gateway_plist.read_bytes() == item.node_transition_original["gateway"]
    assert item.paths.node_plist.read_bytes() == item.node_transition_original["node"]
    assert item.paths.current_link.resolve() == item.predecessor


def test_node_transition_permission_loss_does_not_block_snapshot_rollback(node_transition_fixture, monkeypatch):
    item = node_transition_fixture
    assert run(item, NodeTransitionBackend(item))["outcome"] == "activated"
    def revoked(*args, **kwargs):
        raise activate_module.ActivationError("candidate grant revoked after boot")
    monkeypatch.setattr(activate_module, "verify_node_toolchain_screen_capture_precondition", revoked)
    recovery = NodeTransitionBackend(item)
    restored = activate_module.restore_failed_activation(
        item.paths, item.candidate, CANDIDATE_COMMIT, item.seal, item.snapshot,
        restore_result_path(item), recovery, after_success=True)
    assert restored["outcome"] == "restored_stopped"
    assert restored["afterSuccessfulUpgrade"] is True
    assert recovery.boot_nodes == []
    assert os.readlink(item.paths.node_alias) == item.node_transition_original["alias"]
    assert item.paths.gateway_plist.read_bytes() == item.node_transition_original["gateway"]
    assert activate_module.terminal_restore_outcome(
        item.paths, json.loads(item.paths.result.read_text()), item.paths.result.read_bytes(),
        restored, restore_result_path(item).stat()) == "restored_stopped"


def test_node_transition_partial_apply_reuses_exact_old_snapshot(node_transition_fixture, monkeypatch):
    item = node_transition_fixture
    native = activate_module.atomic_symlink
    def fail_alias(link, target):
        if link == item.paths.node_alias:
            raise activate_module.ActivationError("candidate alias write failed")
        return native(link, target)
    monkeypatch.setattr(activate_module, "atomic_symlink", fail_alias)
    backend = NodeTransitionBackend(item)
    result = run(item, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert backend.boot_nodes == []
    monkeypatch.setattr(activate_module, "atomic_symlink", native)
    recovery = NodeTransitionBackend(item)
    restored = restore(item, recovery)
    assert restored["restoreApplied"] is True
    assert item.paths.gateway_plist.read_bytes() == item.node_transition_original["gateway"]
    assert item.paths.node_plist.read_bytes() == item.node_transition_original["node"]
    assert os.readlink(item.paths.node_alias) == item.node_transition_original["alias"]


def test_node_transition_fence_binds_toolchain_and_no_second_start(node_transition_fixture):
    item = node_transition_fixture
    first = run(item, NodeTransitionBackend(item))
    fence_path = activate_module._start_fence_path(item.paths)
    fence = json.loads(fence_path.read_text())
    valid, _ = activate_module.terminal_fence_bindings(
        first, fence, hashlib.sha256(fence_path.read_bytes()).hexdigest(), item.paths.result, fence_path)
    assert valid is True
    tampered = copy.deepcopy(fence)
    tampered["nodeToolchain"]["candidate"]["sha256"] = "0" * 64
    valid, _ = activate_module.terminal_fence_bindings(
        first, tampered, hashlib.sha256(fence_path.read_bytes()).hexdigest(), item.paths.result, fence_path)
    assert valid is False
    item.paths.result.unlink()
    resumed_backend = NodeTransitionBackend(item)
    resumed = run(item, resumed_backend)
    assert resumed["outcome"] == "snapshot_restore_required"
    assert resumed["firstBootAttempted"] is None
    assert resumed_backend.boot_nodes == []


def test_legacy_same_node_snapshot_keeps_four_base_members(fixture):
    assert json.loads(fixture.snapshot.read_text())["schemaVersion"] == 2
    assert len(activate_module.snapshot_members(fixture.paths)) == 4
    assert fixture.paths.node_alias not in activate_module.snapshot_members(fixture.paths)
    assert run(fixture)["outcome"] == "activated"



def test_node_transition_keeps_original_32_external_declarations(node_transition_fixture):
    item = node_transition_fixture
    roots = []
    for index in range(32):
        root = item.root / f"recovery-{index:02d}.private.txt"
        root.write_text(f"preserve original declaration {index}\n")
        root.chmod(0o600)
        roots.append(root)
    item.paths = replace(item.paths, recovery_roots=tuple(roots))
    output = item.root / "node-transition-32-roots-snapshot"
    item.snapshot = output / "stopped-snapshot.json"
    activate_module.create_stopped_snapshot(
        item.paths, output, item.snapshot, item.candidate, CANDIDATE_COMMIT,
        item.seal, NodeTransitionBackend(item))
    snapshot = json.loads(item.snapshot.read_text())
    assert snapshot["inventoryCount"] == 40
    assert [row["path"] for row in snapshot["recoveryRoots"]] == sorted(map(str, roots))
    assert str(item.paths.node_alias) not in [row["path"] for row in snapshot["recoveryRoots"]]
    assert len(activate_module.snapshot_members(item.paths)) == 40


def test_node_transition_permission_preflight_refuses_before_mutation(node_transition_fixture, monkeypatch):
    item = node_transition_fixture
    def denied(*args, **kwargs):
        raise activate_module.ActivationError("candidate exact path has no recorded grant")
    monkeypatch.setattr(activate_module, "verify_node_toolchain_screen_capture_precondition", denied)
    backend = NodeTransitionBackend(item)
    result = run(item, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.boot_nodes == []
    assert not activate_module._start_fence_path(item.paths).exists()
    assert item.paths.gateway_plist.read_bytes() == item.node_transition_original["gateway"]
    assert os.readlink(item.paths.node_alias) == item.node_transition_original["alias"]


def test_node_transition_archive_rejects_unrelated_alias_target(node_transition_fixture):
    item = node_transition_fixture
    manifest = json.loads(item.snapshot.read_text())
    modified = item.root / "wrong-node-alias.tar"
    with tarfile.open(manifest["tarPath"]) as source, tarfile.open(modified, "w") as output:
        for original in source:
            member = copy.copy(original)
            if member.name == str(item.paths.node_alias).lstrip("/"):
                member.linkname = str(item.root / "unrelated-toolchain")
            output.addfile(member, source.extractfile(original) if original.isfile() else None)
    with pytest.raises(activate_module.ActivationError, match="symlink|alias"):
        activate_module.validate_snapshot_archive_members(
            item.paths, modified, str(item.predecessor), manifest["recoveryRoots"])



def test_node_transition_precondition_never_claims_native_acceptance(tmp_path, monkeypatch):
    path = tmp_path / "precondition.json"
    facts = {
        "schemaVersion": 1, "kind": "node_toolchain_screen_capture_precondition",
        "status": "candidate_code_identity_and_recorded_permission_verified",
        "predecessorNode": "/synthetic/old/bin/node",
        "predecessorBinding": {"path": "/synthetic/old-binding.json", "sha256": "a" * 64},
        "candidateNode": "/synthetic/new/bin/node", "candidateSha256": "b" * 64,
        "candidateCodeIdentity": {"sha256": "b" * 64},
        "permission": {"database": "/synthetic/TCC.db"},
        "effectiveCaptureVerified": False, "scheduledSyncProven": False, "tccMutated": False,
    }
    receipt = {**facts, "enrolledAt": "2026-10-21T00:00:00Z"}
    path.write_text(json.dumps(receipt))
    path.chmod(0o400)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(activate_module, "_node_toolchain_screen_capture_precondition_facts",
                        lambda *args: facts)
    verified = activate_module.verify_node_toolchain_screen_capture_precondition(
        path, Path(facts["candidateNode"]), expected_sha256=digest)
    assert verified["kind"] == "node_toolchain_screen_capture_precondition"
    assert verified["effectiveCaptureVerified"] is False and verified["scheduledSyncProven"] is False
    assert "bindingSha256" not in verified
    with pytest.raises(activate_module.ActivationError, match="native|binding|schema|version"):
        activate_module.verify_screen_capture_binding(path, Path(facts["candidateNode"]))


@pytest.mark.parametrize("drift", ["digest", "mutable", "replacement", "facts", "effective"])
def test_node_transition_precondition_pins_immutable_facts(tmp_path, monkeypatch, drift):
    path = tmp_path / "precondition.json"
    facts = {
        "schemaVersion": 1, "kind": "node_toolchain_screen_capture_precondition",
        "status": "candidate_code_identity_and_recorded_permission_verified",
        "predecessorNode": "/synthetic/old/bin/node",
        "predecessorBinding": {"path": "/synthetic/old-binding.json", "sha256": "a" * 64},
        "candidateNode": "/synthetic/new/bin/node", "candidateSha256": "b" * 64,
        "candidateCodeIdentity": {"sha256": "b" * 64},
        "permission": {"database": "/synthetic/TCC.db"},
        "effectiveCaptureVerified": False, "scheduledSyncProven": False, "tccMutated": False,
    }
    receipt = {**facts, "enrolledAt": "2026-10-21T00:00:00Z"}
    path.write_text(json.dumps(receipt))
    path.chmod(0o400)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if drift == "digest":
        digest = "0" * 64
    elif drift == "mutable":
        path.chmod(0o600)
    elif drift in ("replacement", "effective"):
        path.chmod(0o600)
        receipt["effectiveCaptureVerified" if drift == "effective" else "candidateSha256"] = True if drift == "effective" else "c" * 64
        path.write_text(json.dumps(receipt)); path.chmod(0o400)
        if drift == "effective":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        facts = {**facts, "candidateCodeIdentity": {"sha256": "c" * 64}}
    monkeypatch.setattr(activate_module, "_node_toolchain_screen_capture_precondition_facts",
                        lambda *args: facts)
    with pytest.raises(activate_module.ActivationError, match="precondition|permission"):
        activate_module.verify_node_toolchain_screen_capture_precondition(
            path, Path(receipt["candidateNode"]), expected_sha256=digest)



@pytest.fixture
def managed_node_live_paths_fixture(node_transition_fixture, monkeypatch):
    item = node_transition_fixture
    from scripts.operator_contract import OperatorContract
    configured = {name: activate_module.OPERATOR.get(name, {}) for name in ("paths", "runtime", "identifiers")}
    configured["paths"].update({
        "runtime_releases_root": str(item.paths.releases_root), "runtime_current_link": str(item.paths.current_link),
        "runtime_package_link": str(item.paths.package_link), "openclaw_cli": str(item.paths.bin_link),
        "gateway_plist": str(item.paths.gateway_plist), "node_plist": str(item.paths.node_plist),
        "node_binary": str(item.paths.node), "runtime_node_alias": str(item.paths.node_alias),
        "state_root": str(item.paths.candidate_state_dir), "activation_lock": str(item.paths.lock),
        "activation_result": str(item.paths.result), "screen_capture_binding": str(item.paths.screen_capture_binding),
        "host_home": str(item.root),
    })
    configured["runtime"]["recovery_roots"] = []
    monkeypatch.setattr(activate_module, "OPERATOR", OperatorContract(configured))
    monkeypatch.setattr(activate_module.pwd, "getpwnam",
                        lambda _: SimpleNamespace(pw_uid=item.paths.operator_uid, pw_dir=str(item.root)))
    transition = item.paths.node_toolchain_transition
    monkeypatch.setattr(activate_module, "MANAGED_NODE_EXECUTABLE_SHA256", {
        item.paths.node.parent.parent.name: transition.predecessor_sha256,
        transition.candidate.parent.parent.name: transition.candidate_sha256})
    declaration = item.root / "node-transition-declaration.json"
    declaration.write_text(json.dumps({
        "schemaVersion": 1,
        "predecessor": {"path": str(item.paths.node), "sha256": transition.predecessor_sha256},
        "candidate": {"path": str(transition.candidate), "sha256": transition.candidate_sha256},
        "candidateScreenCapturePrecondition": {
            "path": str(transition.candidate_screen_capture_precondition),
            "sha256": transition.candidate_screen_capture_precondition_sha256},
        "compatibilityAliases": [{"path": str(path), "predecessorTarget": target}
                                 for path, target in transition.compatibility_aliases],
        "cliWrapper": {"path": str(item.root / ".local/bin/openclaw"),
                       "mode": transition.cli_wrapper_mode,
                       "predecessorSha256": transition.cli_wrapper_predecessor_sha256,
                       "sha256": transition.cli_wrapper_candidate_sha256},
    }, sort_keys=True) + "\n")
    declaration.chmod(0o400)
    item.node_transition_declaration = declaration
    item.node_transition_declaration_sha = hashlib.sha256(declaration.read_bytes()).hexdigest()
    return item


def test_node_transition_live_paths_tracks_alias_cutover_and_rollback(managed_node_live_paths_fixture):
    item = managed_node_live_paths_fixture
    assert activate_module.live_paths().node == item.paths.node
    assert run(item, NodeTransitionBackend(item))["outcome"] == "activated"
    new_node = item.paths.node_toolchain_transition.candidate
    ordinary = activate_module.live_paths()
    assert ordinary.node == new_node and ordinary.node_toolchain_transition is None
    declared = activate_module.live_paths(
        node_toolchain_transition=item.node_transition_declaration,
        node_toolchain_transition_sha256=item.node_transition_declaration_sha)
    assert declared.node == item.paths.node and declared.node_toolchain_transition == item.paths.node_toolchain_transition
    restored = activate_module.restore_failed_activation(
        declared, item.candidate, CANDIDATE_COMMIT, item.seal, item.snapshot,
        restore_result_path(item), NodeTransitionBackend(item, paths=declared), after_success=True)
    assert restored["outcome"] == "restored_stopped"
    assert activate_module.live_paths().node == item.paths.node


@pytest.mark.parametrize("drift", ["unknown-toolchain", "wrong-binary", "plist-mismatch"])
def test_node_transition_live_paths_fails_closed_on_unsupported_or_mixed_pair(managed_node_live_paths_fixture, drift):
    item = managed_node_live_paths_fixture
    if drift == "unknown-toolchain":
        target = item.paths.node_alias.parent / "node-v99.0.0"
        target.mkdir()
        item.paths.node_alias.unlink(); item.paths.node_alias.symlink_to(target)
    elif drift == "wrong-binary":
        monkeypatch_target = item.paths.node
        monkeypatch_target.chmod(0o755)
        monkeypatch_target.write_text("#!/bin/sh\n# modified executable\nexit 0\n")
        monkeypatch_target.chmod(0o555)
    else:
        item.paths.node_alias.unlink()
        item.paths.node_alias.symlink_to(item.paths.node_toolchain_transition.candidate.parent.parent)
    with pytest.raises(activate_module.ActivationError, match="managed Node|plist"):
        activate_module.live_paths()


def test_node_transition_cli_declaration_restores_explicit_old_paths_after_partial_apply(managed_node_live_paths_fixture):
    item = managed_node_live_paths_fixture
    transition = item.paths.node_toolchain_transition
    value = plistlib.loads(item.paths.gateway_plist.read_bytes())
    value["ProgramArguments"][0] = str(transition.candidate)
    item.paths.gateway_plist.write_bytes(plistlib.dumps(value))
    with pytest.raises(activate_module.ActivationError, match="plist"):
        activate_module.live_paths()
    declared = activate_module.live_paths(
        node_toolchain_transition=item.node_transition_declaration,
        node_toolchain_transition_sha256=item.node_transition_declaration_sha)
    assert declared.node == item.paths.node
    assert declared.node_toolchain_transition.candidate == transition.candidate


@pytest.mark.parametrize("mode", ["snapshot", "activate", "restore", "node-toolchain-preflight"])
def test_node_transition_cli_modes_share_same_pinned_declaration(managed_node_live_paths_fixture, mode):
    item = managed_node_live_paths_fixture
    argv = ["--node-toolchain-transition", str(item.node_transition_declaration),
            "--node-toolchain-transition-sha256", item.node_transition_declaration_sha, mode]
    if mode != "node-toolchain-preflight":
        argv += ["--candidate-release", str(item.candidate), "--expected-commit", CANDIDATE_COMMIT,
                 "--candidate-seal", str(item.seal)]
    if mode == "snapshot":
        argv += ["--output-root", str(item.root / "next-snapshot"),
                 "--manifest", str(item.root / "next-snapshot/stopped-snapshot.json")]
    elif mode in ("activate", "restore"):
        argv += ["--stopped-snapshot-manifest", str(item.snapshot)]
        if mode == "restore":
            argv += ["--output", str(restore_result_path(item))]
    args = activate_module.build_parser().parse_args(argv)
    assert args.node_toolchain_transition == item.node_transition_declaration
    assert args.node_toolchain_transition_sha256 == item.node_transition_declaration_sha
    declared = activate_module.live_paths(node_toolchain_transition=args.node_toolchain_transition,
        node_toolchain_transition_sha256=args.node_toolchain_transition_sha256)
    assert declared.node == item.paths.node
    assert declared.node_toolchain_transition == item.paths.node_toolchain_transition


def test_node_transition_supported_cli_preflight_is_read_only(managed_node_live_paths_fixture, capsys):
    item = managed_node_live_paths_fixture
    before = (os.readlink(item.paths.node_alias), item.paths.gateway_plist.read_bytes(),
              item.paths.node_plist.read_bytes(), os.readlink(item.paths.current_link))
    assert activate_module.main([
        "--node-toolchain-transition", str(item.node_transition_declaration),
        "--node-toolchain-transition-sha256", item.node_transition_declaration_sha,
        "node-toolchain-preflight"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "node_toolchain_preflight_accepted_no_effects"
    assert receipt["effectiveCandidateCaptureVerified"] is False
    assert before == (os.readlink(item.paths.node_alias), item.paths.gateway_plist.read_bytes(),
                      item.paths.node_plist.read_bytes(), os.readlink(item.paths.current_link))
    assert not activate_module._start_fence_path(item.paths).exists()



@pytest.mark.parametrize("live_binding_state", ["new-enrollment", "missing"])
def test_node_transition_rollback_restores_archived_binding_after_live_enrollment_change(
    managed_node_live_paths_fixture, monkeypatch, live_binding_state,
):
    item = managed_node_live_paths_fixture
    assert run(item, NodeTransitionBackend(item))["outcome"] == "activated"
    old_binding = item.paths.screen_capture_binding
    original = old_binding.read_bytes()
    if live_binding_state == "new-enrollment":
        old_binding.chmod(0o600)
        old_binding.write_text('{"newCandidateNativeAcceptance":true}\n')
        old_binding.chmod(0o400)
    else:
        old_binding.unlink()
    def no_current_candidate_admission(*args, **kwargs):
        raise activate_module.ActivationError("candidate static permission/binding no longer current")
    monkeypatch.setattr(activate_module, "verify_node_toolchain_screen_capture_precondition",
                        no_current_candidate_admission)
    declared = activate_module.live_paths(
        node_toolchain_transition=item.node_transition_declaration,
        node_toolchain_transition_sha256=item.node_transition_declaration_sha)
    recovery = NodeTransitionBackend(item, paths=declared)
    restored = activate_module.restore_failed_activation(
        declared, item.candidate, CANDIDATE_COMMIT, item.seal, item.snapshot,
        restore_result_path(item), recovery, after_success=True)
    assert restored["outcome"] == "restored_stopped" and recovery.boot_nodes == []
    assert old_binding.read_bytes() == original
    assert activate_module.live_paths().node == item.paths.node
    assert activate_module.terminal_restore_outcome(
        declared, json.loads(item.paths.result.read_text()), item.paths.result.read_bytes(),
        restored, restore_result_path(item).stat()) == "restored_stopped"



def _assert_original_node_compatibility_controls(item):
    for path, target in item.node_transition_original["compatibilityAliases"].items():
        assert os.readlink(path) == target
    wrapper = item.root / ".local/bin/openclaw"
    assert wrapper.read_bytes() == item.node_transition_original["wrapper"]
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o755


@pytest.mark.parametrize("control", ["command-alias", "prefix-alias", "wrapper-bytes", "wrapper-postimage", "unowned-alias"])
def test_node_transition_rejects_compatibility_control_drift_before_fence(node_transition_fixture, control):
    item = node_transition_fixture
    transition = item.paths.node_toolchain_transition
    if control in ("command-alias", "prefix-alias"):
        alias = item.root / (".local/bin/node" if control == "command-alias" else ".local/opt/node-current")
        alias.unlink(); alias.symlink_to(item.root / "unowned-node")
    elif control == "wrapper-bytes":
        wrapper = item.root / ".local/bin/openclaw"
        wrapper.write_bytes(wrapper.read_bytes() + b"# unexpected change\n")
    elif control == "wrapper-postimage":
        item.paths = replace(item.paths, node_toolchain_transition=replace(
            transition, cli_wrapper_candidate_sha256="0" * 64))
    else:
        item.paths = replace(item.paths, node_toolchain_transition=replace(
            transition, compatibility_aliases=((item.root / "other/node", str(item.paths.node)),
                                               transition.compatibility_aliases[1])))
    backend = NodeTransitionBackend(item)
    result = run(item, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.boot_nodes == [] and not activate_module._start_fence_path(item.paths).exists()
    assert item.paths.current_link.resolve() == item.predecessor


@pytest.mark.parametrize("alias_index", [0, 1])
def test_node_transition_partial_compatibility_alias_apply_restores_exact_preimages(
    node_transition_fixture, monkeypatch, alias_index,
):
    item = node_transition_fixture
    target_alias = item.paths.node_toolchain_transition.compatibility_aliases[alias_index][0]
    native = activate_module.atomic_symlink
    def fail_compatibility(link, target):
        if link == target_alias:
            raise activate_module.ActivationError("fixture compatibility alias replacement failed")
        return native(link, target)
    monkeypatch.setattr(activate_module, "atomic_symlink", fail_compatibility)
    backend = NodeTransitionBackend(item)
    result = run(item, backend)
    assert result["outcome"] == "snapshot_restore_required" and backend.boot_nodes == []
    monkeypatch.setattr(activate_module, "atomic_symlink", native)
    restored = restore(item, NodeTransitionBackend(item))
    assert restored["restoreApplied"] is True
    _assert_original_node_compatibility_controls(item)
    assert item.paths.gateway_plist.read_bytes() == item.node_transition_original["gateway"]
    assert item.paths.node_plist.read_bytes() == item.node_transition_original["node"]


def test_node_transition_partial_wrapper_apply_uses_same_snapshot_restore(node_transition_fixture, monkeypatch):
    item = node_transition_fixture
    wrapper = item.root / ".local/bin/openclaw"
    native = activate_module.os.replace
    def fail_wrapper(source, target):
        if Path(target) == wrapper and ".node-transition-" in Path(source).name:
            raise OSError("fixture wrapper replace failure")
        return native(source, target)
    monkeypatch.setattr(activate_module.os, "replace", fail_wrapper)
    backend = NodeTransitionBackend(item)
    assert run(item, backend)["outcome"] == "snapshot_restore_required"
    assert backend.boot_nodes == []
    monkeypatch.setattr(activate_module.os, "replace", native)
    restored = restore(item, NodeTransitionBackend(item))
    assert restored["restoreApplied"] is True
    _assert_original_node_compatibility_controls(item)
    assert item.paths.gateway_plist.read_bytes() == item.node_transition_original["gateway"]


def test_node_transition_consolidated_aliases_and_cli_use_single_selected_executable(
    managed_node_live_paths_fixture,
):
    item = managed_node_live_paths_fixture
    wrapper = item.root / ".local/bin/openclaw"
    assert run(item, NodeTransitionBackend(item))["outcome"] == "activated"
    environment = {"PATH": f"{item.root}/.local/bin:{item.root}/.local/opt/node-current/bin:/usr/bin:/bin"}
    # These are fixture-only shell stubs, not any real Node/Gateway or UI route.
    shell = subprocess.run(["/bin/sh", "-c", "command -v node; node --version"],
                           env=environment, check=True, capture_output=True, text=True)
    assert shell.stdout.splitlines() == [str(item.root / ".local/bin/node"), "candidate-node-fixture"]
    cli = subprocess.run(["/bin/sh", str(wrapper), "--version"], env=environment,
                         check=True, capture_output=True, text=True)
    assert cli.stdout.strip() == "candidate-node-fixture"
    assert (item.root / ".local/bin/node").resolve() == item.paths.node_toolchain_transition.candidate
    assert (item.root / ".local/opt/node-current/bin/node").resolve() == item.paths.node_toolchain_transition.candidate
    assert activate_module.live_paths().node == item.paths.node_toolchain_transition.candidate
    restored = activate_module.restore_failed_activation(
        item.paths, item.candidate, CANDIDATE_COMMIT, item.seal, item.snapshot,
        restore_result_path(item), NodeTransitionBackend(item), after_success=True)
    assert restored["outcome"] == "restored_stopped"
    _assert_original_node_compatibility_controls(item)
    assert (item.root / ".local/bin/node").resolve() == item.paths.node
    assert (item.root / ".local/opt/node-current/bin/node").resolve() == item.paths.node
    assert activate_module.live_paths().node == item.paths.node


@pytest.mark.parametrize("canonical_first", [False, True])
def test_node_transition_normalizes_only_known_legacy_path_and_restores_original_bytes(
    node_transition_fixture, canonical_first,
):
    item = node_transition_fixture
    canonical = str(item.paths.node_alias / "bin")
    legacy = item.paths.node_alias.parent / "node-v22.22.0" / "bin"
    legacy.mkdir(parents=True)
    legacy_node = legacy / "node"
    legacy_node.write_text("#!/bin/sh\nexit 99\n")
    legacy_node.chmod(0o755)
    assert os.access(legacy_node, os.X_OK)
    first = str(item.root / "unrelated-first")
    last = str(item.root / "unrelated-last")
    managed = [canonical, str(legacy)] if canonical_first else [str(legacy), canonical]
    originals = {}
    for path in (item.paths.gateway_plist, item.paths.node_plist):
        value = plistlib.loads(path.read_bytes())
        value["EnvironmentVariables"]["PATH"] = ":".join([
            str(item.paths.node.parent), first, *managed, last, first, "/usr/bin", "/bin",
        ])
        path.write_bytes(plistlib.dumps(value))
        originals[path] = path.read_bytes()
    output = item.root / "legacy-path-snapshot"
    item.snapshot = output / "stopped-snapshot.json"
    activate_module.create_stopped_snapshot(
        item.paths, output, item.snapshot, item.candidate,
        CANDIDATE_COMMIT, item.seal, NodeTransitionBackend(item))
    assert run(item, NodeTransitionBackend(item))["outcome"] == "activated"
    expected = [str(item.paths.node_toolchain_transition.candidate.parent),
                first, canonical, last, first, "/usr/bin", "/bin"]
    for path in originals:
        parts = plistlib.loads(path.read_bytes())["EnvironmentVariables"]["PATH"].split(":")
        assert parts == expected and str(legacy) not in parts and parts.count(canonical) == 1
    restored = activate_module.restore_failed_activation(
        item.paths, item.candidate, CANDIDATE_COMMIT, item.seal, item.snapshot,
        restore_result_path(item), NodeTransitionBackend(item), after_success=True)
    assert restored["outcome"] == "restored_stopped"
    assert all(path.read_bytes() == original for path, original in originals.items())


def test_legacy_node_precondition_cannot_admit_a_new_first_boot(
        node_transition_fixture, monkeypatch):
    item = node_transition_fixture
    verify = activate_module.verify_node_toolchain_screen_capture_precondition
    def legacy(*args, **kwargs):
        return {**verify(*args, **kwargs), "recordedCandidateFilesystemPermissionVerified": False}
    monkeypatch.setattr(activate_module, "verify_node_toolchain_screen_capture_precondition", legacy)
    backend = NodeTransitionBackend(item)
    result = run(item, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.boot_nodes == [] and not activate_module._start_fence_path(item.paths).exists()
    assert item.paths.gateway_plist.read_bytes() == item.node_transition_original["gateway"]
    assert os.readlink(item.paths.node_alias) == item.node_transition_original["alias"]



def test_legacy_node_precondition_fails_forward_cli_before_any_stop_or_write(
        managed_node_live_paths_fixture, monkeypatch, capsys):
    item = managed_node_live_paths_fixture
    verify = activate_module.verify_node_toolchain_screen_capture_precondition
    def legacy(*args, **kwargs):
        return {**verify(*args, **kwargs), "recordedCandidateFilesystemPermissionVerified": False}
    monkeypatch.setattr(activate_module, "verify_node_toolchain_screen_capture_precondition", legacy)
    before = (os.readlink(item.paths.node_alias), item.paths.gateway_plist.read_bytes(),
              item.paths.node_plist.read_bytes(), os.readlink(item.paths.current_link))
    with pytest.raises(SystemExit) as rejected:
        activate_module.main([
            "--node-toolchain-transition", str(item.node_transition_declaration),
            "--node-toolchain-transition-sha256", item.node_transition_declaration_sha,
            "node-toolchain-preflight"])
    assert rejected.value.code == 2
    assert "Full Disk Access" in capsys.readouterr().err
    assert before == (os.readlink(item.paths.node_alias), item.paths.gateway_plist.read_bytes(),
                      item.paths.node_plist.read_bytes(), os.readlink(item.paths.current_link))
    assert not activate_module._start_fence_path(item.paths).exists()
