from __future__ import annotations

import re
import urllib.parse


SECRET_RE = re.compile(
    r"(?i)(session|token|signature|password|heslo|key)=([^&\s]+)"
)


def safe_url(value: str | None) -> str | None:
    if not value:
        return value
    parsed = urllib.parse.urlsplit(value)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def redact(value: object) -> str:
    return SECRET_RE.sub(r"\1=[REDACTED]", str(value))
