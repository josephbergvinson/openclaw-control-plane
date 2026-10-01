#!/usr/bin/env python3
"""Emit validated CI inputs from the runtime manifest; never fetch or install."""

from __future__ import annotations

import json
from pathlib import Path
import re


def load_ci_config(manifest: dict) -> dict[str, str]:
    if manifest["schemaVersion"] != 1:
        raise ValueError("Unsupported runtime manifest version")
    upstream, toolchain = manifest["upstream"], manifest["toolchain"]
    values = {"repository": upstream["repository"], "tag": upstream["tag"],
              "node": toolchain["node"], "pnpm": toolchain["pnpm"]}
    if values["repository"] != "https://github.com/openclaw/openclaw.git":
        raise ValueError("Review a changed upstream repository before CI adoption")
    for key, pattern in {"tag": r"v[0-9]+\.[0-9]+\.[0-9]+",
                         "node": r"[0-9]+\.[0-9]+\.[0-9]+",
                         "pnpm": r"[0-9]+\.[0-9]+\.[0-9]+"}.items():
        if not isinstance(values[key], str) or not re.fullmatch(pattern, values[key]):
            raise ValueError(f"Manifest requires an exact {key} version")
    return values


if __name__ == "__main__":
    manifest = json.loads((Path(__file__).resolve().parents[1] / "runtime/manifest.json").read_text())
    for key, value in load_ci_config(manifest).items():
        print(f"{key}={value}")
