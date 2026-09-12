import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import journal_screen_capture_acceptance as probe


def test_scheduler_stage_carries_the_explicit_operator_config(tmp_path, monkeypatch):
    config = tmp_path / "external-operator.json"
    config.write_text('{"schema_version":1}')
    monkeypatch.setenv("OPENCLAW_OPERATOR_CONFIG", str(config))
    argv = probe.stage_arguments(tmp_path / "evidence", 1)
    assert argv[argv.index("--operator-config") + 1] == str(config.resolve())
    assert argv[argv.index("--number") + 1] == "1"
    assert "stage" in argv


def test_attribution_requires_this_capture_process_not_other_activity():
    node = Path("/private/node")
    allow = {"eventMessage": "Handling access request to kTCCServiceScreenCapture, from Sub:{/private/node}Resp:"
             "{pid=12,} Auth Right: Allowed (System Set),DB Action:None"}
    unrelated = {"eventMessage": "AttributionChain: responsible={pid=12, /private/node}, "
                 "requesting={identifier=boo.peekaboo.peekaboo, pid=88,}"}
    with pytest.raises(probe.activation.ActivationError, match="did not attribute this Peekaboo"):
        probe.attribution("\n".join(map(json.dumps, (allow, unrelated))), 12, 99, node)
    matching = {"eventMessage": unrelated["eventMessage"].replace("pid=88,", "pid=99,")}
    rows = probe.attribution("\n".join(map(json.dumps, (allow, matching))), 12, 99, node)
    assert len(rows) == 2
    with pytest.raises(probe.activation.ActivationError, match="authorization is unproven"):
        probe.attribution(json.dumps(matching), 12, 99, node)


def test_failed_manual_run_removes_only_its_disabled_job_without_retry(tmp_path, monkeypatch):
    root = tmp_path / "capture"
    active = tmp_path / "active.json"
    active.write_text('{"outcome":"activated"}')
    paths = SimpleNamespace(result=active, node=Path("/private/node"))
    monkeypatch.setattr(probe.activation, "live_paths", lambda: paths)
    monkeypatch.setattr(probe.activation, "screen_capture_code_identity", lambda path: {})
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kw: SimpleNamespace(returncode=0))
    observer_calls = []
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kw: SimpleNamespace(
        poll=lambda: None, terminate=lambda: observer_calls.append("terminate"),
        wait=lambda timeout: observer_calls.append("wait")))
    calls = []
    job_id = "12345678-1234-1234-1234-123456789abc"
    def cli(root, label, *args, **kw):
        calls.append((label, args))
        if label == "job-add":
            argv = json.loads(args[args.index("--command-argv") + 1])
            assert "--disabled" in args and "--no-deliver" in args
            return {"id": job_id, "enabled": False, "payload": {"argv": argv}}
        if label == "job-run":
            raise probe.activation.ActivationError("scheduler result uncertain")
        return {"removed": True}
    monkeypatch.setattr(probe, "cli", cli)
    with pytest.raises(probe.activation.ActivationError, match="scheduler result uncertain"):
        probe.capture(root)
    assert [label for label, _ in calls] == ["job-add", "job-run", "job-remove"]
    assert calls[-1][1] == ("rm", job_id, "--json")
    assert observer_calls == ["terminate", "wait"]
    assert not (root / "capture.json").exists()


@pytest.fixture
def pending_capture(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    image = tmp_path / "journal.png"
    image.write_bytes(b"a visually inspected image")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    active = tmp_path / "active.json"
    active.write_text('{"outcome":"activated"}')
    process = ["darwin:20:1", "/private/node", "1"]
    proof = {"status": "awaiting_visual_acceptance", "imageSha256": digest,
             "responsiblePid": 12, "responsibleProcessIdentity": process,
             "activationReceiptSha256": hashlib.sha256(active.read_bytes()).hexdigest()}
    probe.save(tmp_path / "capture.json", proof)
    monkeypatch.setattr(probe, "identity", lambda pid: process)
    monkeypatch.setattr(probe.activation, "live_paths", lambda: SimpleNamespace(result=active))
    return tmp_path, digest, active


def test_accept_requires_the_exact_visually_reviewed_image(pending_capture):
    root, digest, _ = pending_capture
    with pytest.raises(probe.activation.ActivationError, match="visual confirmation"):
        probe.accept(root, "0" * 64)
    (root / "journal.png").write_bytes(b"image replaced after visual inspection")
    with pytest.raises(probe.activation.ActivationError, match="visual confirmation"):
        probe.accept(root, digest)
    assert not (root / "acceptance.json").exists()


def test_process_change_requires_fresh_manual_capture(pending_capture, monkeypatch):
    root, digest, _ = pending_capture
    monkeypatch.setattr(probe, "identity", lambda pid: ["darwin:21:1", "/private/node", "1"])
    with pytest.raises(probe.activation.ActivationError, match="process changed"):
        probe.accept(root, digest)
    assert not (root / "acceptance.json").exists()


def test_activation_change_requires_fresh_manual_capture(pending_capture):
    root, digest, active = pending_capture
    active.write_text('{"outcome":"activated","generation":2}')
    with pytest.raises(probe.activation.ActivationError, match="activation changed"):
        probe.accept(root, digest)
    assert not (root / "acceptance.json").exists()
