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


@pytest.mark.parametrize("returncode", [0, 1])
def test_stage2_uses_local_screenshot_only_and_retains_failure(tmp_path, monkeypatch, returncode):
    tmp_path.chmod(0o700)
    configured_peekaboo = tmp_path / "configured-peekaboo"
    monkeypatch.setattr(probe, "peekaboo_binary", lambda: configured_peekaboo)
    monkeypatch.setattr(probe, "identity", lambda pid: [str(pid), "/private/process", "1"])
    monkeypatch.setattr(probe, "responsible", lambda pid: 12)
    calls = []
    waits = []
    child = SimpleNamespace(pid=99, wait=lambda timeout: waits.append(timeout) or returncode,
                            poll=lambda: returncode, terminate=lambda: None, kill=lambda: None)
    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        return child
    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    metadata_calls = []
    def metadata(argv, timeout):
        metadata_calls.append((list(argv), timeout))
        return SimpleNamespace(returncode=0, timed_out=False, stdout=json.dumps(window_metadata()).encode(), stderr=b"")
    monkeypatch.setattr(probe.activation, "run_bounded", metadata)
    if returncode:
        with pytest.raises(probe.activation.ActivationError, match="capture stage failed"):
            probe.stage(tmp_path, 2)
    else:
        probe.stage(tmp_path, 2)
    assert len(calls) == 1
    assert calls[0][0] == [str(configured_peekaboo), "image", "--app", "PID:41", "--window-id", "700", "--mode", "window",
                           "--format", "png", "--path", str(tmp_path / "journal.png"),
                           "--no-remote", "--json"]
    assert metadata_calls == [([str(configured_peekaboo), "list", "windows", "--app", "com.apple.journal",
                                "--include-details", "bounds,ids,off_screen", "--no-remote", "--json"], 10)]
    assert json.loads((tmp_path / "journal-window.json").read_text()) == window_target()
    assert calls[0][1]["start_new_session"] is False
    assert waits == [25]
    assert not (tmp_path / "capture.json").exists()
    assert not (tmp_path / "acceptance.json").exists()


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
    image.write_bytes(png(918, 750))
    write_target_and_image_metadata(tmp_path)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    active = tmp_path / "active.json"
    active.write_text('{"outcome":"activated"}')
    process = ["darwin:20:1", "/private/node", "1"]
    proof = {"status": "awaiting_visual_acceptance", "imageSha256": digest,
             "responsiblePid": 12, "responsibleProcessIdentity": process,
             "activationReceiptSha256": hashlib.sha256(active.read_bytes()).hexdigest(),
             "journalWindow": window_target(), "imageDimensions": {"width": 918, "height": 750},
             "imageWindowIdVerified": True}
    probe.save(tmp_path / "capture.json", proof)
    monkeypatch.setattr(probe, "identity", lambda pid: process)
    monkeypatch.setattr(probe.activation, "live_paths", lambda: SimpleNamespace(result=active, node=Path(process[1])))
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
    with pytest.raises(probe.activation.ActivationError, match="lifecycle changed"):
        probe.accept(root, digest)
    assert not (root / "acceptance.json").exists()


def window_target():
    return {"applicationPid": 41, "windowId": 700, "width": 918, "height": 750}


def window_metadata():
    return {"data": {"targetApplication": {"bundleIdentifier": "com.apple.journal", "processIdentifier": 41},
                     "windows": [{"windowID": 699, "title": "Journal", "bounds": [[0, 0], [1920, 30]],
                                  "isMainWindow": False, "isMinimized": False, "offScreen": False},
                                 {"windowID": 700, "title": "Journal", "bounds": [[40, 50], [918, 750]],
                                  "isMainWindow": True, "isMinimized": False, "offScreen": False}]}}


def png(width, height):
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")


def write_target_and_image_metadata(root, window_id=700):
    probe.save(root / "journal-window.json", window_target())
    (root / "stage-2-stdout.log").write_text(json.dumps({"success": True, "data": {"files": [
        {"window_id": window_id, "path": str(root / "journal.png"), "mime_type": "image/png"}]}}))


@pytest.mark.parametrize("scenario", ["menu_only", "two_bodies", "off_screen", "minimized", "wrong_app"])
def test_selection_refuses_missing_or_ambiguous_real_journal_body(tmp_path, monkeypatch, scenario):
    document = window_metadata()
    windows = document["data"]["windows"]
    if scenario == "menu_only":
        document["data"]["windows"] = windows[:1]
        windows[0]["isMainWindow"] = True
    elif scenario == "two_bodies":
        windows.append({**windows[1], "windowID": 701})
    elif scenario == "off_screen":
        windows[1]["offScreen"] = True
    elif scenario == "minimized":
        windows[1]["isMinimized"] = True
    else:
        document["data"]["targetApplication"]["bundleIdentifier"] = "com.example.other"
    monkeypatch.setattr(probe.activation, "run_bounded", lambda *args, **kw: SimpleNamespace(
        returncode=0, timed_out=False, stdout=json.dumps(document).encode(), stderr=b""))
    with pytest.raises(probe.activation.ActivationError, match="Journal (body window|window metadata)"):
        probe.select_journal_window(tmp_path)
    assert not (tmp_path / "journal-window.json").exists()


def test_window_metadata_failure_does_not_start_image_or_retry(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(probe.activation, "run_bounded", lambda *args, **kw:
                        calls.append(args) or SimpleNamespace(returncode=1, timed_out=False,
                                                              stdout=b"", stderr=b"unavailable"))
    with pytest.raises(probe.activation.ActivationError, match="metadata is unavailable"):
        probe.select_journal_window(tmp_path)
    assert len(calls) == 1
    assert not (tmp_path / "journal-window.json").exists()


@pytest.fixture
def completed_capture_route(tmp_path, monkeypatch):
    root = tmp_path / "capture"
    active = tmp_path / "active.json"
    active.write_text('{"outcome":"activated"}')
    node = Path("/private/node")
    monkeypatch.setattr(probe.activation, "live_paths", lambda: SimpleNamespace(result=active, node=node))
    monkeypatch.setattr(probe.activation, "screen_capture_code_identity", lambda path: {})
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kw: SimpleNamespace(
        poll=lambda: None, terminate=lambda: None, wait=lambda timeout: 0))
    monkeypatch.setattr(probe, "identity", lambda pid: ["darwin:20:1", str(node), "1"])
    monkeypatch.setattr(probe, "attribution", lambda *args: [])
    state = {"windowId": 700, "dimensions": (918, 750), "calls": []}
    job_id = "12345678-1234-1234-1234-123456789abc"
    def cli(directory, label, *args, **kw):
        state["calls"].append(label)
        if label == "job-add":
            argv = json.loads(args[args.index("--command-argv") + 1])
            assert "--disabled" in args and "--no-deliver" in args
            return {"id": job_id, "enabled": False, "payload": {"argv": argv}}
        if label == "job-run":
            for number, pid in enumerate((20, 21, 22)):
                probe.save(directory / f"stage-{number}.json",
                           {"pid": pid, "ppid": 12 if number == 0 else pid - 1,
                            "pgid": pid, "responsiblePid": 12})
                probe.save(directory / f"child-{number}.json",
                           {"pid": pid + 1 if number < 2 else 99, "responsiblePid": 12})
            write_target_and_image_metadata(directory, state["windowId"])
            (directory / "journal.png").write_bytes(png(*state["dimensions"]))
            return {"completed": True, "completionStatus": "succeeded"}
        assert label == "job-remove" and args == ("rm", job_id, "--json")
        return {"removed": True}
    monkeypatch.setattr(probe, "cli", cli)
    return root, state


@pytest.mark.parametrize("defect", ["wrong_window", "menu_strip", "zero_dimensions"])
def test_completed_capture_rejects_wrong_window_or_dimensions_and_keeps_cleanup(completed_capture_route, defect):
    root, state = completed_capture_route
    if defect == "wrong_window":
        state["windowId"] = 699
    elif defect == "menu_strip":
        state["dimensions"] = (1920, 30)
    else:
        state["dimensions"] = (0, 0)
    with pytest.raises(probe.activation.ActivationError, match="Journal (image producer|PNG dimensions)"):
        probe.capture(root)
    assert state["calls"] == ["job-add", "job-run", "job-remove"]
    assert not (root / "capture.json").exists()
    assert not (root / "acceptance.json").exists()


def test_completed_exact_window_remains_pending_root_visual_acceptance(completed_capture_route):
    root, state = completed_capture_route
    result = probe.capture(root)
    proof = json.loads((root / "capture.json").read_text())
    assert result["status"] == proof["status"] == "awaiting_visual_acceptance"
    assert proof["journalWindow"] == window_target()
    assert proof["imageDimensions"] == {"width": 918, "height": 750}
    assert proof["imageWindowIdVerified"] is True
    assert proof["temporaryJobRemoved"] is True
    assert proof["scheduledSyncProven"] is False
    assert not (root / "acceptance.json").exists()
