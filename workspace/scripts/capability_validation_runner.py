#!/usr/bin/env python3
"""Check an enrolled Journal capture binding without running provider probes.

This export contains the source installation's focused ScreenCapture mode. Its
legacy host-specific multi-provider suite is outside this portable entrypoint.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screen-capture-binding", type=Path, required=True)
    args = parser.parse_args()
    try:
        try:
            from scripts.openclaw_runtime_activate import OPERATOR, verify_screen_capture_binding
        except ModuleNotFoundError:
            from openclaw_runtime_activate import OPERATOR, verify_screen_capture_binding
        evidence = verify_screen_capture_binding(args.screen_capture_binding,
                                                 OPERATOR.require_path("paths.node_binary"),
                                                 require_current_process=True)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        print(json.dumps({"status": "unknown", "nativeCaptureRequired": True,
                          "scheduledSyncProven": False}))
        return 1
    print(json.dumps({"status": "verified", **evidence}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
