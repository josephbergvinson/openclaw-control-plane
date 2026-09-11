"""Shared pure redaction and bounds for existing private cron receipts."""
from __future__ import annotations

import os
import re


SENSITIVE_ENV_KEY = re.compile(
    r'(?:TOKEN|PASSWORD|PASSWD|SECRET|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|COOKIE|AUTH|DSN)',
    re.IGNORECASE,
)
SENSITIVE_ASSIGNMENT = re.compile(
    r'(?i)(\b(?:token|password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|cookie|authorization|dsn)\b\s*[=:]\s*)([^\s,;]+)'
)
BEARER_VALUE = re.compile(r'(?i)(\bBearer\s+)([^\s,;]+)')
URI_CREDENTIALS = re.compile(r'([A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+:)([^\s/@]+)(@)')


def redact_process_text(text: str) -> tuple[str, int]:
    """Redact credential-shaped values while preserving traceback structure."""
    redacted = str(text or '')
    redaction_count = 0
    secret_values = sorted(
        {
            value
            for key, value in os.environ.items()
            if value and len(value) >= 4 and SENSITIVE_ENV_KEY.search(key)
        },
        key=len,
        reverse=True,
    )
    for value in secret_values:
        count = redacted.count(value)
        if count:
            redacted = redacted.replace(value, '[REDACTED_ENV]')
            redaction_count += count
    for pattern, replacement in (
        (BEARER_VALUE, r'\1[REDACTED]'),
        (URI_CREDENTIALS, r'\1[REDACTED]\3'),
        (SENSITIVE_ASSIGNMENT, r'\1[REDACTED]'),
    ):
        redacted, count = pattern.subn(replacement, redacted)
        redaction_count += count
    return redacted, redaction_count


def bounded_utf8_prefix(text: str, limit: int) -> tuple[str, int, bool]:
    encoded = text.encode('utf-8')
    if len(encoded) <= limit:
        return text, len(encoded), False
    retained = encoded[:limit].decode('utf-8', errors='ignore')
    return retained, len(retained.encode('utf-8')), True
