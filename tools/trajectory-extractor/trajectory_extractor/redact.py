"""Scrub secrets and personal data out of a trajectory before it leaves the machine.

Trajectories carry whatever the agent saw: absolute paths with usernames, host
names, and any environment variable a shell step happened to echo. Redaction
runs before parameterization so that placeholders are derived from already
clean values.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "<REDACTED>"
USER_PLACEHOLDER = "<USER>"
EMAIL_PLACEHOLDER = "<EMAIL>"

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)"
    r"(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&|)]+)"
)

_AUTH_HEADER = re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)\S+")

_VENDOR_TOKEN = re.compile(
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bxox[baprs]-[A-Za-z0-9\-]{10,}"
)

_URL_CREDENTIALS = re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")

# Home directories keep their shape so path parameterization still works;
# only the account name is replaced.
_HOME_DIR = re.compile(r"(/home/|/Users/|[A-Za-z]:\\Users\\)([^/\\\s\"':,;]+)")

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


def redact_text(text: str) -> str:
    """Return ``text`` with secrets and personal identifiers replaced."""
    if not text:
        return text

    result = _SECRET_ASSIGNMENT.sub(rf"\1\2{REDACTED}", text)
    result = _AUTH_HEADER.sub(rf"\1{REDACTED}", result)
    result = _VENDOR_TOKEN.sub(REDACTED, result)
    result = _URL_CREDENTIALS.sub(rf"\1{REDACTED}@", result)
    # Home directories before e-mail, so `<USER>@host` is never produced.
    result = _HOME_DIR.sub(rf"\g<1>{USER_PLACEHOLDER}", result)
    result = _EMAIL.sub(EMAIL_PLACEHOLDER, result)
    return result


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside arbitrary JSON-shaped data."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value
