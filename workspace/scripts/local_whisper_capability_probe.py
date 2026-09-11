#!/usr/bin/env python3
"""Probe the local Whisper.cpp audio transcription lane.

Read-only by default: verifies installed paths and OpenClaw config wiring.
With --audio, runs the configured wrapper against a supplied audio file and
checks that non-empty transcript text is produced.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from typing import Any

CONFIG_PATH = pathlib.Path.home() / ".openclaw" / "openclaw.json"
WRAPPER_PATH = pathlib.Path.home() / ".local" / "bin" / "openclaw-whisper-transcribe"
WHISPER_CLI_PATH = pathlib.Path.home() / ".local" / "bin" / "whisper-cli"
MODEL_PATH = pathlib.Path.home() / ".local" / "share" / "whisper-cpp" / "ggml-small.en.bin"


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def configured_model_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    models = (
        config.get("tools", {})
        .get("media", {})
        .get("audio", {})
        .get("models", [])
    )
    return [entry for entry in models if isinstance(entry, dict)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", help="Optional audio file for end-to-end local transcription proof")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    config = load_config()
    entries = configured_model_entries(config)
    configured = any(
        entry.get("type") == "cli"
        and entry.get("command") == str(WRAPPER_PATH)
        and entry.get("args") == ["{{MediaPath}}"]
        for entry in entries
    )

    checks: dict[str, Any] = {
        "wrapper_exists_executable": WRAPPER_PATH.is_file() and bool(WRAPPER_PATH.stat().st_mode & 0o111),
        "whisper_cli_exists_executable": WHISPER_CLI_PATH.is_file() and bool(WHISPER_CLI_PATH.stat().st_mode & 0o111),
        "model_exists_nonempty": MODEL_PATH.is_file() and MODEL_PATH.stat().st_size > 0,
        "config_points_to_wrapper": configured,
        "config_path": str(CONFIG_PATH),
        "wrapper_path": str(WRAPPER_PATH),
        "whisper_cli_path": str(WHISPER_CLI_PATH),
        "model_path": str(MODEL_PATH),
    }

    transcript_chars = None
    if args.audio:
        audio = pathlib.Path(args.audio)
        checks["audio_exists"] = audio.is_file()
        if audio.is_file():
            try:
                result = subprocess.run(
                    [str(WRAPPER_PATH), str(audio)],
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=args.timeout,
                )
                transcript = result.stdout.strip()
                transcript_chars = len(transcript)
                checks["audio_transcription_exit_zero"] = result.returncode == 0
                checks["audio_transcription_nonempty"] = bool(transcript)
                if result.returncode != 0:
                    checks["audio_transcription_error_tail"] = result.stderr[-500:]
            except Exception as exc:
                checks["audio_transcription_exception"] = f"{type(exc).__name__}: {exc}"

    ok = all(value is True for key, value in checks.items() if key.endswith(("executable", "nonempty", "wrapper", "exists", "zero")))
    payload = {
        "ok": ok,
        "capability": "local-whisper-audio-transcription",
        "provider": "whisper.cpp",
        "model": "ggml-small.en.bin",
        "checks": checks,
    }
    if transcript_chars is not None:
        payload["transcript_chars"] = transcript_chars
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
