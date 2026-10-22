#!/usr/bin/env python3
"""Explicit, private Journal capture through the existing scheduler command route.

capture creates one disabled temporary job, runs it once and removes it. accept
requires the SHA-256 of the image after visual inspection. Neither mode exports,
syncs or edits Journal, changes TCC, restarts the runtime or replays a real job.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

try:
    from scripts import openclaw_runtime_activate as activation
except ModuleNotFoundError:
    import openclaw_runtime_activate as activation

def peekaboo_binary() -> Path:
    return activation.OPERATOR.require_path("paths.peekaboo_binary")


def python_binary() -> Path:
    return activation.OPERATOR.require_path("paths.python_binary")
SCRIPT = Path(__file__).resolve()


def stage_arguments(root: Path, number: int) -> list[str]:
    config = Path(os.environ.get("OPENCLAW_OPERATOR_CONFIG", str(SCRIPT.parents[1] / "operator.json")))
    if not config.is_absolute():
        raise activation.ActivationError("operator configuration path must be absolute")
    return [str(python_binary()), "-B", str(SCRIPT), "stage", "--directory", str(root),
            "--number", str(number), "--operator-config", str(config.resolve(strict=True))]


def save(path: Path, value: object) -> None:
    activation.create_immutable_file(path, json.dumps(value, indent=2, sort_keys=True).encode() + b"\n")


def identity(pid: int) -> list[str]:
    return [str(item) for item in activation.process_identity(pid)]


def responsible(pid: int) -> int:
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    query = library.responsibility_get_pid_responsible_for_pid
    query.argtypes, query.restype = [ctypes.c_int], ctypes.c_int
    value = query(pid)
    if value <= 0:
        raise activation.ActivationError("native responsibility is unavailable")
    return value


def private_directory(root: Path) -> None:
    if (not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir()
            or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise activation.ActivationError("capture directory must be physical, private and operator-owned")


def select_journal_window(root: Path) -> dict:
    """Pin one observed Journal body; never fall back to a window index."""
    result = activation.run_bounded(
        (str(peekaboo_binary()), "list", "windows", "--app", "com.apple.journal",
         "--include-details", "bounds,ids,off_screen", "--no-remote", "--json"), 10)
    activation.create_immutable_file(root / "window-list-stdout.json", result.stdout)
    activation.create_immutable_file(root / "window-list-stderr.log", result.stderr)
    if result.returncode != 0 or result.timed_out:
        raise activation.ActivationError("Journal window metadata is unavailable")
    document = json.loads(result.stdout)
    data = document.get("data", {}) if isinstance(document, dict) else {}
    if not isinstance(data, dict):
        raise activation.ActivationError("Journal window metadata has the wrong application")
    application = data.get("targetApplication", {})
    windows = data.get("windows")
    if (not isinstance(application, dict) or application.get("bundleIdentifier") != "com.apple.journal"
            or type(application.get("processIdentifier")) is not int
            or application["processIdentifier"] <= 0 or not isinstance(windows, list)):
        raise activation.ActivationError("Journal window metadata has the wrong application")
    candidates = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        bounds = window.get("bounds")
        size = bounds[1] if isinstance(bounds, list) and len(bounds) == 2 else None
        if (type(window.get("windowID")) is not int or window["windowID"] <= 0
                or window.get("isMainWindow") is not True or window.get("isMinimized") is not False
                or window.get("offScreen") is not False or not isinstance(window.get("title"), str)
                or not window["title"].casefold().startswith("journal")
                or not isinstance(size, list) or len(size) != 2
                or any(type(value) not in (int, float) or not 32 < value <= 32768
                       or not float(value).is_integer() for value in size)):
            continue
        candidates.append({"applicationPid": application["processIdentifier"],
                           "windowId": window["windowID"], "width": int(size[0]), "height": int(size[1])})
    if len(candidates) != 1:
        raise activation.ActivationError("Journal body window is not uniquely ready")
    save(root / "journal-window.json", candidates[0])
    return candidates[0]


def journal_image_evidence(root: Path, image: bytes) -> dict:
    """Match the producer's actual window and PNG size to the observed body."""
    target, _, _ = activation.read_json(root / "journal-window.json", "observed Journal window")
    if (set(target) != {"applicationPid", "windowId", "width", "height"}
            or any(type(target.get(key)) is not int or target[key] <= 0 for key in target)
            or not 32 < target["width"] <= 32768 or not 32 < target["height"] <= 32768):
        raise activation.ActivationError("observed Journal window is invalid")
    document = json.loads((root / "stage-2-stdout.log").read_text())
    data = document.get("data", {}) if isinstance(document, dict) else {}
    files = data.get("files") if isinstance(data, dict) else None
    if (not isinstance(document, dict) or document.get("success") is not True
            or not isinstance(files, list) or len(files) != 1
            or not isinstance(files[0], dict) or type(files[0].get("window_id")) is not int
            or files[0]["window_id"] != target["windowId"]
            or files[0].get("path") != str(root / "journal.png")
            or files[0].get("mime_type") != "image/png"):
        raise activation.ActivationError("Journal image producer selected a different window")
    if (len(image) < 24 or not image.startswith(b"\x89PNG\r\n\x1a\n")
            or image[8:16] != b"\x00\x00\x00\rIHDR"):
        raise activation.ActivationError("Journal capture did not create a PNG with dimensions")
    dimensions = {"width": int.from_bytes(image[16:20], "big"),
                  "height": int.from_bytes(image[20:24], "big")}
    if any(dimensions[key] != target[key] for key in ("width", "height")):
        raise activation.ActivationError("Journal PNG dimensions differ from the observed body window")
    return {"journalWindow": target, "imageDimensions": dimensions, "imageWindowIdVerified": True}


def stage(root: Path, number: int) -> None:
    """Preserve recovery -> daily -> driver Python session boundaries."""
    private_directory(root)
    pid = os.getpid()
    row = {"stage": number, "pid": pid, "ppid": os.getppid(), "pgid": os.getpgrp(),
           "responsiblePid": responsible(pid), "identity": identity(pid)}
    save(root / f"stage-{number}.json", row)
    if number < 2:
        argv = stage_arguments(root, number + 1)
    else:
        target = select_journal_window(root)
        argv = [str(peekaboo_binary()), "image", "--app", f"PID:{target['applicationPid']}",
                "--window-id", str(target["windowId"]), "--mode", "window", "--format", "png",
                "--path", str(root / "journal.png"), "--no-remote", "--json"]
    with (root / f"stage-{number}-stdout.log").open("xb") as stdout, \
            (root / f"stage-{number}-stderr.log").open("xb") as stderr:
        child = subprocess.Popen(argv, stdout=stdout, stderr=stderr, start_new_session=number < 2)
        def stop(_signal, _frame):
            child.terminate()  # Each Python stage forwards termination to its own child.
        previous = signal.signal(signal.SIGTERM, stop)
        try:
            save(root / f"child-{number}.json", {"pid": child.pid, "responsiblePid": responsible(child.pid),
                                                "parentPid": pid})
            try:
                code = child.wait(timeout=25 if number == 2 else 40 + (2 - number) * 5)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                raise activation.ActivationError("capture stage timed out")
            if code != 0:
                raise activation.ActivationError("capture stage failed")
        finally:
            signal.signal(signal.SIGTERM, previous)
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def cli(root: Path, label: str, *args: str, timeout: int = 30) -> dict:
    paths = activation.live_paths()
    environment = dict(os.environ, OPENCLAW_STATE_DIR=str(paths.candidate_state_dir))
    environment["PATH"] = str(paths.node.parent) + os.pathsep + environment.get("PATH", "")
    result = activation.run_bounded((str(paths.bin_link), "cron", *args), timeout,
                                    env=environment)
    activation.create_immutable_file(root / f"{label}-stdout.json", result.stdout)
    activation.create_immutable_file(root / f"{label}-stderr.log", result.stderr)
    if result.returncode != 0 or result.timed_out:
        raise activation.ActivationError(f"{label} failed; reconcile the retained command receipt before retrying")
    return json.loads(result.stdout)


def attribution(raw: str, pid: int, tool_pid: int, node: Path) -> list[dict]:
    rows = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = item.get("eventMessage", "")
        if f"pid={pid}," in message and str(node) in message:
            rows.append({"timestamp": item.get("timestamp"), "message": message})
    if not any("AttributionChain:" in row["message"] and f"pid={tool_pid}," in row["message"]
               and "identifier=boo.peekaboo.peekaboo" in row["message"] for row in rows):
        raise activation.ActivationError("TCC did not attribute this Peekaboo process to the responsible Node")
    subject = "from Sub:{" + str(node) + "}Resp:"
    if not any(subject in row["message"] and "kTCCServiceScreenCapture" in row["message"]
               and "Auth Right: Allowed (System Set)" in row["message"]
               and "DB Action:None" in row["message"] for row in rows):
        raise activation.ActivationError("effective ScreenCapture authorization is unproven")
    return rows


def capture(root: Path) -> dict:
    root.mkdir(mode=0o700)  # A new directory and new disabled job for each explicit invocation.
    private_directory(root)
    paths = activation.live_paths()
    runtime_evidence = activation.screen_capture_runtime_evidence(paths.result, paths.node)
    if subprocess.run(("/usr/bin/pgrep", "-x", "Journal"), stdout=subprocess.DEVNULL).returncode:
        raise activation.ActivationError("open the existing Journal app before explicit capture")
    files = [activation.screen_capture_code_identity(path) for path in (paths.node, peekaboo_binary())]
    job_id = None
    observer = None
    with (root / "tccd.ndjson").open("xb") as output, (root / "tccd-stderr.log").open("xb") as errors:
        try:
            argv = stage_arguments(root, 0)
            job = cli(root, "job-add", "add", "--name", "journal-capture-" + uuid.uuid4().hex,
                      "--disabled", "--every", "1d", "--command-argv", json.dumps(argv),
                      "--command-cwd", str(root), "--timeout-seconds", "70", "--no-deliver", "--json")
            job_id = job.get("id")
            if not isinstance(job_id, str) or re.fullmatch(r"[0-9a-f-]{36}", job_id) is None:
                raise activation.ActivationError("temporary job identity is unknown; inspect job-add receipt")
            if job.get("enabled") is not False or job.get("payload", {}).get("argv") != argv:
                raise activation.ActivationError("temporary disabled job did not match capture")
            predicate = ('process == "tccd" AND (eventMessage CONTAINS[c] "ScreenCapture" '
                         'OR eventMessage CONTAINS[c] "AUTHREQ" OR eventMessage CONTAINS[c] "AttributionChain")')
            observer = subprocess.Popen(("/usr/bin/sudo", "-n", "/usr/bin/log", "stream", "--timeout", "2m",
                                         "--style", "ndjson", "--level", "debug", "--predicate", predicate),
                                        stdout=output, stderr=errors)
            time.sleep(0.25)
            if observer.poll() is not None:
                raise activation.ActivationError("read-only TCC observer failed before capture dispatch")
            result = cli(root, "job-run", "run", job_id, "--wait", "--wait-timeout", "90s", "--json", timeout=100)
            if result.get("completed") is not True or result.get("completionStatus") != "succeeded":
                raise activation.ActivationError("manual capture did not reach successful scheduler completion")
        finally:
            if observer is not None:
                observer.terminate()
                try:
                    observer.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    observer.kill()
                    observer.wait()
            if job_id is not None:
                removed = cli(root, "job-remove", "rm", job_id, "--json")
                if removed.get("removed") is not True:
                    raise activation.ActivationError("temporary job removal is unverified")
    stages = [json.loads((root / f"stage-{i}.json").read_text()) for i in range(3)]
    children = [json.loads((root / f"child-{i}.json").read_text()) for i in range(3)]
    pid = stages[0]["responsiblePid"]
    current = identity(pid)
    if Path(current[1]) != paths.node or stages[0]["ppid"] != pid:
        raise activation.ActivationError("probe was not launched by the activated Node scheduler")
    for number, row in enumerate(stages):
        if row["responsiblePid"] != pid or children[number]["responsiblePid"] != pid:
            raise activation.ActivationError("capture process responsibility changed")
        if number and (row["pid"] != children[number - 1]["pid"]
                       or row["ppid"] != stages[number - 1]["pid"] or row["pgid"] != row["pid"]):
            raise activation.ActivationError("capture Python session ancestry differs")
    messages = attribution((root / "tccd.ndjson").read_text(), pid, children[2]["pid"], paths.node)
    image, _ = activation.read_physical(root / "journal.png", "private Journal image")
    image_evidence = journal_image_evidence(root, image)
    if files != [activation.screen_capture_code_identity(path) for path in (paths.node, peekaboo_binary())]:
        raise activation.ActivationError("capture executable changed during probe")
    activation.screen_capture_runtime_evidence(
        paths.result, paths.node, expected=runtime_evidence, responsible_pid=pid)
    proof = {"status": "awaiting_visual_acceptance", "verifiedAt": activation.utc_now(),
             "route": activation.SCREEN_CAPTURE_ROUTE, "responsiblePid": pid,
             "responsibleProcessIdentity": current,
             "files": [{**row, "codesignVerified": True} for row in files],
             **runtime_evidence, **image_evidence,
             "tccdAttributionAndEffectivePermission": messages,
             "imageSha256": activation.sha256_bytes(image), "imagePath": str(root / "journal.png"),
             "manualProbe": True, "temporaryJobRemoved": True,
             "scheduledSyncProven": False, "producerInvoked": False, "tccMutated": False}
    save(root / "capture.json", proof)
    return {"status": proof["status"], "imagePath": proof["imagePath"], "imageSha256": proof["imageSha256"]}


def accept(root: Path, reviewed_image_sha256: str) -> dict:
    private_directory(root)
    proof, _, _ = activation.read_json(root / "capture.json", "manual capture")
    image, _ = activation.read_physical(root / "journal.png", "visually reviewed image")
    if (proof.get("status") != "awaiting_visual_acceptance"
            or reviewed_image_sha256 != proof.get("imageSha256")
            or reviewed_image_sha256 != hashlib.sha256(image).hexdigest()):
        raise activation.ActivationError("visual confirmation does not bind the captured image")
    image_evidence = journal_image_evidence(root, image)
    if any(proof.get(key) != value for key, value in image_evidence.items()):
        raise activation.ActivationError("visual confirmation does not bind the observed Journal window")
    if identity(proof["responsiblePid"]) != proof["responsibleProcessIdentity"]:
        raise activation.ActivationError("process changed; perform a fresh capture")
    paths = activation.live_paths()
    activation.screen_capture_runtime_evidence(
        paths.result, paths.node, expected=proof, responsible_pid=proof["responsiblePid"])
    proof.update(status="current_scheduler_route_capture_and_responsibility_verified",
                 imageVisuallyVerifiedAsJournal=True)
    output = root / "acceptance.json"
    save(output, proof)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    activation.load_screen_capture_acceptance(output, digest)
    return {"acceptancePath": str(output), "acceptanceSha256": digest, "scheduledSyncProven": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    for mode in ("capture", "accept", "stage"):
        command = modes.add_parser(mode)
        command.add_argument("--directory", type=Path, required=True)
        if mode == "accept":
            command.add_argument("--reviewed-journal-image-sha256", required=True)
        if mode == "stage":
            command.add_argument("--number", type=int, choices=(0, 1, 2), required=True)
            command.add_argument("--operator-config", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.mode == "stage":
            # The scheduler does not inherit the invoking terminal's environment.
            activation.OPERATOR = activation.load_operator_contract(args.operator_config)
            os.environ["OPENCLAW_OPERATOR_CONFIG"] = str(args.operator_config)
            stage(args.directory, args.number)
            return 0
        result = capture(args.directory) if args.mode == "capture" else accept(args.directory, args.reviewed_journal_image_sha256)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
