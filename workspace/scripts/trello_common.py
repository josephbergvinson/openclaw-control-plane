#!/usr/bin/env python3
"""Shared Trello helpers for workspace scripts."""

from __future__ import annotations

from shlex import join as _operator_command
try:
    from scripts.routing_operator_bindings import binding as _operator_binding
except ModuleNotFoundError:
    from routing_operator_bindings import binding as _operator_binding


import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

def detect_workspace_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == ".worktrees":
            return parent.parent
    return here.parents[1]


WORKSPACE_ROOT = detect_workspace_root()
DEFAULT_STATE_DIR = Path(
    os.environ.get(
        "OPENCLAW_STATE_DIR",
        _operator_binding('paths.state_root'),
    )
)
DEFAULT_ENV_PATH = DEFAULT_STATE_DIR / ".env"
MAX_ERROR_BODY_BYTES = 64 * 1024
DEFAULT_RETRY_AFTER_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 60.0


class TrelloError(RuntimeError):
    pass


def _discard_http_error(error: HTTPError) -> None:
    """Bound provider-controlled error bytes and always close the response."""

    try:
        error.read(MAX_ERROR_BODY_BYTES + 1)
    except Exception:
        pass
    finally:
        try:
            error.close()
        except Exception:
            pass


def _retry_after_seconds(value: object) -> float:
    """Return a bounded retry delay without reflecting provider input."""

    try:
        parsed = float(value) if value is not None else DEFAULT_RETRY_AFTER_SECONDS
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_RETRY_AFTER_SECONDS
    if not math.isfinite(parsed) or parsed < 0:
        return DEFAULT_RETRY_AFTER_SECONDS
    return min(parsed, MAX_RETRY_AFTER_SECONDS)


def _retry_after_header(error: HTTPError) -> object:
    """Read Retry-After defensively from an untrusted response."""

    try:
        return error.headers.get("Retry-After") if error.headers else None
    except Exception:
        return None



def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values



def load_auth(*, env_path: str | Path = DEFAULT_ENV_PATH) -> tuple[str, str]:
    path = Path(env_path)
    if not path.is_absolute():
        path = WORKSPACE_ROOT / path
    file_values = parse_env_file(path)
    key = os.environ.get("TRELLO_API_KEY") or file_values.get(
        "TRELLO_API_KEY", ""
    )
    token = os.environ.get("TRELLO_TOKEN") or file_values.get("TRELLO_TOKEN", "")
    if not key or not token:
        raise TrelloError(
            "missing Trello credentials; set TRELLO_API_KEY/TRELLO_TOKEN "
            f"or populate {path}"
        )
    return key, token


@dataclass
class TrelloClient:
    key: str
    token: str
    timeout: int = 30
    retry_limit: int = 6

    def request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        query_params = dict(params or {})
        query_params["key"] = self.key
        query_params["token"] = self.token
        query = urlencode(query_params, doseq=True)
        url = f"https://api.trello.com/1{path}?{query}"
        request = Request(url, headers={"Accept": "application/json"}, method=method)

        failure_message: str | None = None
        for attempt in range(self.retry_limit):
            retry_delay: float | None = None
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read().decode("utf-8")
                    return json.loads(payload) if payload else {}
            except HTTPError as exc:
                status_code = exc.code if isinstance(exc.code, int) else None
                retry_after = _retry_after_header(exc)
                _discard_http_error(exc)
                if status_code == 429 and attempt + 1 < self.retry_limit:
                    retry_delay = _retry_after_seconds(retry_after)
                elif status_code is not None:
                    failure_message = (
                        f"Trello API request failed with HTTP {status_code}"
                    )
                else:
                    failure_message = "Trello API request failed"
                # Trello may echo the full request target in its response body.
                # That target contains the API key and token query parameters,
                # so raw exceptions and detail must not cross this boundary.
            except URLError:
                failure_message = "Trello API request failed"

            if retry_delay is not None:
                time.sleep(retry_delay)
                continue
            if failure_message is not None:
                break

        if failure_message is None:
            failure_message = "Trello API request failed"
        # Raise after every provider exception handler has exited so neither a
        # raw cause nor an implicit exception context can reach callers.
        raise TrelloError(failure_message)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params)

    def post(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, params)

    def put(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("PUT", path, params)

    def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("DELETE", path, params)



def build_client(*, env_path: str | Path = DEFAULT_ENV_PATH) -> TrelloClient:
    key, token = load_auth(env_path=env_path)
    return TrelloClient(key=key, token=token)



def extract_card_code(name: str) -> str | None:
    head = (name or "").split(" ", 1)[0].strip()
    if head and any(ch.isdigit() for ch in head):
        return head
    return None



def labels_by_name(labels: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(label.get("name")): label for label in labels if label.get("name")}



def cards_by_code(cards: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for card in cards:
        code = extract_card_code(str(card.get("name", "")))
        if code:
            out[code] = card
    return out
