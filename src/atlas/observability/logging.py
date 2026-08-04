"""Structured JSON logging with automatic secret redaction.

The redaction filter is not decoration. Agents log tool arguments and model
metadata; a repository under analysis may contain an API key that the agent
happily quotes into a finding excerpt, which then lands in your log
aggregator forever. Redacting at the logging boundary is the only place
that catches every path, because it does not rely on each call site
remembering.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

# Patterns are deliberately broad: a false-positive redaction costs nothing,
# a missed credential costs an incident.
_SECRET_PATTERNS = [
    re.compile(r"(sk-ant-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(sk-[A-Za-z0-9]{20,})"),
    re.compile(r"(ghp_[A-Za-z0-9]{20,})"),
    re.compile(r"(AKIA[0-9A-Z]{12,})"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
    # The opening quote of the *value* must stay inside group 1, otherwise
    # redaction eats it and leaves an unbalanced quote - which turns a
    # perfectly good JSON log line into unparseable text. Subtle, and only
    # visible once something downstream tries to json.loads() the output.
    re.compile(
        r"(?i)(\"?(?:password|passwd|secret|token|api[_-]?key)\"?\s*[:=]\s*[\"']?)"
        r"[^\s\"',}]{6,}"
    ),
]

REDACTED = "***REDACTED***"


def redact(text: str) -> str:
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) + REDACTED) if m.lastindex else REDACTED, out)
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload.update(ctx)
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
