"""Check exact native baseline/candidate verdicts without turning arbitrary red green."""

import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

BASE_FILE = "test/isolated-queue/adopted-drain-steering.unit-dormant-boundary.test.ts"
FENCE_FILE = "test/isolated-queue/adopted-drain-steering.owner-fifo.test.ts"
BASE_NAMES = {
    "isolated adopted-drain steering > preserves source ownership and steering for " + name
    for name in ("empty-control", "adopted-current", "older-ready", "different-authority")
}
FENCE_NAMES = {
    "adopted-source owner and FIFO fences > " + name
    for name in (
        "does not inject an adopted-source correction into a same-key successor created after preparation",
        "keeps an ordinary predecessor that arrives after preparation ahead of the correction",
        "does not reuse steering authority after the retained source is aborted",
        "lets a parked correction steer before an ordinary waiter appended later",
        "falls back in place if the active owner changes while the correction is parked",
        "retains distinct in-flight collected sources as a blocker even after their queue items are consumed",
    )
}
TARGET = "isolated adopted-drain steering > preserves source ownership and steering for adopted-current"
TARGET_FAILURE = (
    'AssertionError: expected "vi.fn()" to be called 1 times, but got 0 times'
    "\n \u276f " + BASE_FILE + ":836:26"
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(phase, native_exit, xml_bytes, log_bytes):
    require(phase in ("baseline", "candidate"), "unknown phase")
    baseline = phase == "baseline"
    require(native_exit == (1 if baseline else 0), "unexpected native exit")
    root = ET.fromstring(xml_bytes)
    count = 4 if baseline else 10
    failures = 1 if baseline else 0
    for name, expected in (("tests", count), ("failures", failures), ("errors", 0)):
        require(int(root.attrib[name]) == expected, "unexpected JUnit " + name)
    require(int(root.attrib.get("skipped", 0)) == 0, "skipped cases")
    cases = list(root.iter("testcase"))
    require(len(cases) == count, "incorrect testcase count")
    names = [case.attrib["name"] for case in cases]
    require(set(names) == (BASE_NAMES if baseline else BASE_NAMES | FENCE_NAMES), "incorrect case identities")
    require(len(set(names)) == len(names), "duplicate cases")
    for case in cases:
        name = case.attrib["name"]
        require(case.attrib.get("classname") == (BASE_FILE if name in BASE_NAMES else FENCE_FILE), "incorrect case file")
        require(not case.findall("error") and not case.findall("skipped"), "case error or skip")
        actual = case.findall("failure")
        if baseline and name == TARGET:
            require(len(actual) == 1 and (actual[0].text or "").strip() == TARGET_FAILURE, "different baseline failure")
        else:
            require(not actual, "unexpected failed control/candidate case")
    require(len(list(root.iter("failure"))) == failures, "unexpected nested failures")
    require(not list(root.iter("error")) and not list(root.iter("skipped")), "nested error or skip")
    log = re.sub(r"\x1b\[[0-9;]*m", "", log_bytes.decode("utf-8"))
    reports = [json.loads(line.removeprefix("[vitest:run] ")) for line in log.splitlines() if line.startswith("[vitest:run] ")]
    require(len(reports) == 1, "missing or repeated native run report")
    report = reports[0]
    require(report.get("reason") == ("failed" if baseline else "passed"), "unexpected native run reason")
    require(report.get("files") == (1 if baseline else 2) and report.get("unhandledErrors") == 0, "native file count or unhandled error")
    if baseline:
        require(log.rstrip().endswith("[test] FAILED (exit 1)"), "native baseline did not terminate normally")
    return {"phase": phase, "classification": "expected-baseline-assertion" if baseline else "candidate-ten-cases-passed", "nativeExitCode": native_exit, "tests": count, "passed": count - failures, "failed": failures, "errors": 0}


def main():
    phase, exit_text, xml_name, log_name, receipt_name = sys.argv[1:]
    receipt = {"phase": phase, "nativeExitCode": int(exit_text), "classification": "rejected"}
    try:
        xml_bytes, log_bytes = Path(xml_name).read_bytes(), Path(log_name).read_bytes()
        receipt.update(verify(phase, int(exit_text), xml_bytes, log_bytes))
        receipt["junitSha256"] = hashlib.sha256(xml_bytes).hexdigest()
        receipt["logSha256"] = hashlib.sha256(log_bytes).hexdigest()
    except (ValueError, KeyError, OSError, ET.ParseError) as error:
        receipt["reason"] = str(error)
        Path(receipt_name).write_text(json.dumps(receipt, indent=2) + "\n")
        raise SystemExit("Native result rejected: " + str(error)) from error
    Path(receipt_name).write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
