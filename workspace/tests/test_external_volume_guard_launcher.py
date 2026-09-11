from __future__ import annotations
try:
    from scripts.operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import json
import os
import signal
import sys
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "external_volume_guard_launcher.js"
NODE = OPERATOR.require_path('paths.node_binary')
PYTHON = OPERATOR.require_path('paths.python_binary')


def test_launcher_uses_exact_internal_isolated_python(tmp_path: Path) -> None:
    child = tmp_path / "identity.py"
    child.write_text(
        "import json, sys\n"
        "print(json.dumps({'executable': sys.executable, 'prefix': sys.prefix}))\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(NODE), str(LAUNCHER), str(child)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    identity = json.loads(completed.stdout)
    assert Path(identity["executable"]).resolve() == PYTHON.resolve()
    assert Path(identity["prefix"]).resolve() == Path(sys.prefix).resolve()


def test_launcher_preserves_child_exit_status(tmp_path: Path) -> None:
    child = tmp_path / "exit.py"
    child.write_text("raise SystemExit(3)\n", encoding="utf-8")

    completed = subprocess.run(
        [str(NODE), str(LAUNCHER), str(child)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
    )

    assert completed.returncode == 3


def test_launcher_relays_repeated_termination_without_orphan(tmp_path: Path) -> None:
    child = tmp_path / "signals.py"
    child.write_text(
        "import os, signal, sys, time\n"
        "count = 0\n"
        "def terminate(_signal, _frame):\n"
        "    global count\n"
        "    count += 1\n"
        "    print(f'term:{count}', flush=True)\n"
        "    if count >= 2:\n"
        "        raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, terminate)\n"
        "print(os.getpid(), flush=True)\n"
        "while True:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [str(NODE), str(LAUNCHER), str(child)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    try:
        process.send_signal(signal.SIGTERM)
        time.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 0, stderr
    assert "term:1" in stdout
    assert "term:2" in stdout
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        raise AssertionError(f"launcher left child process {child_pid} running")
