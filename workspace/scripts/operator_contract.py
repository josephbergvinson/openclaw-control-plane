"""Read the adopter's explicit installation bindings without changing the host.

Only the Workspace location and current user's home have derived defaults.
Other bindings are required by the operation that uses them. Loading this
module never contacts providers, creates directories, or runs commands.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
from typing import Any


class ContractError(ValueError):
    """A required installation binding is absent, unresolved, or invalid."""


_MISSING = object()
_REFERENCE = re.compile(r"\$\{operator:([A-Za-z0-9_.-]+)\}")
_PLACEHOLDER = re.compile(r"<[^>]+>|\$\{|\{\{|\b(?:REPLACE_ME|CHANGEME)\b", re.I)


class OperatorContract:
    def __init__(self, values: dict[str, Any] | None = None, *, workspace: Path | None = None):
        if values is not None and not isinstance(values, dict):
            raise ContractError("operator configuration must be a JSON object")
        self._values = copy.deepcopy(values or {})
        version = self._values.get("schema_version", 1)
        if type(version) is not int or version != 1:
            raise ContractError("unsupported operator schema_version")
        self.workspace = (workspace or Path(__file__).resolve().parents[1]).absolute()

    def get(self, key: str, default: Any = None) -> Any:
        value: Any = self._values
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return copy.deepcopy(value)

    def _require(self, key: str) -> Any:
        value = self.get(key, _MISSING)
        if value is _MISSING or value is None:
            raise ContractError(f"required operator binding is missing: {key}")
        return value

    def require_string(self, key: str) -> str:
        value = self._require(key)
        if not isinstance(value, str) or not value.strip() or _PLACEHOLDER.search(value):
            raise ContractError(f"operator binding must be a resolved nonempty string: {key}")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ContractError(f"operator binding contains a control character: {key}")
        return value

    def require_path(self, key: str) -> Path:
        value = self.get(key, _MISSING)
        if value is _MISSING:
            if key == "paths.workspace":
                return self.workspace
            if key == "paths.host_home":
                return Path.home()
        value = self.require_string(key)
        path = Path(value)
        if not path.is_absolute() or "~" in value or "$" in value:
            raise ContractError(f"operator path must be absolute without expansion: {key}")
        if ".." in path.parts:
            raise ContractError(f"operator path must not contain parent traversal: {key}")
        # Do not resolve symlinks: selectors and compatibility links have identity.
        return path

    def require_int(self, key: str) -> int:
        value = self._require(key)
        if type(value) is not int:
            raise ContractError(f"operator binding must be an integer: {key}")
        return value

    def require_bool(self, key: str) -> bool:
        value = self._require(key)
        if type(value) is not bool:
            raise ContractError(f"operator binding must be a boolean: {key}")
        return value

    def require_list(self, key: str) -> list[Any]:
        value = self._require(key)
        if not isinstance(value, list):
            raise ContractError(f"operator binding must be a list: {key}")
        return value

    def render(self, value: Any) -> Any:
        """Resolve whole-value references in JSON structures; never run a shell."""
        if isinstance(value, list):
            return [self.render(item) for item in value]
        if isinstance(value, dict):
            return {key: self.render(item) for key, item in value.items()}
        if not isinstance(value, str):
            return copy.deepcopy(value)
        match = _REFERENCE.fullmatch(value)
        if match:
            key = match.group(1)
            bound = str(self.require_path(key)) if key.startswith("paths.") else self._require(key)
            # References cannot recursively alias themselves or hide placeholders.
            encoded = json.dumps(bound)
            if _PLACEHOLDER.search(encoded):
                raise ContractError(f"operator reference is unresolved: {key}")
            return bound
        if "${operator:" in value:
            raise ContractError("operator references must occupy the whole JSON value")
        return value


def load_operator_contract(path: str | Path | None = None) -> OperatorContract:
    workspace = Path(__file__).resolve().parents[1]
    explicit = path is not None or "OPENCLAW_OPERATOR_CONFIG" in os.environ
    selected = Path(path if path is not None else os.environ.get(
        "OPENCLAW_OPERATOR_CONFIG", str(workspace / "operator.json")))
    if not selected.is_absolute() or "~" in str(selected) or "$" in str(selected):
        raise ContractError("operator configuration path must be absolute without expansion")
    try:
        raw = selected.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            raise ContractError("explicit operator configuration does not exist") from None
        return OperatorContract(workspace=workspace)
    except OSError as exc:
        raise ContractError(f"cannot read operator configuration: {exc.strerror}") from None
    try:
        values = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ContractError("operator configuration is not valid JSON") from None
    return OperatorContract(values, workspace=workspace)
