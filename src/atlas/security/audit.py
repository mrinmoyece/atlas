"""Audit log - who did what, when, and what it cost.

Distinct from application logging on purpose. Application logs are for
debugging and get sampled, rotated and dropped. An audit log answers
compliance questions ("who ran an analysis against that repository?") and
must be append-only, complete, and free of secrets.

Properties implemented:

  * **Append-only in-process**, with an optional JSONL sink. Entries are
    never mutated or deleted through this API.
  * **Hash-chained, sequence-numbered, and length-anchored.** Each entry
    carries the previous entry's hash plus its own monotonic `seq`, both
    covered by the entry hash. Three attacks, three defences:
      - editing an entry     -> its hash no longer matches (chain check)
      - deleting from the
        middle               -> seq no longer equals index
      - truncating the tail  -> the remaining chain is *perfectly valid*
                                and seq still equals index, so neither of
                                the above catches it. Only a separately
                                held high-water mark of "how many entries
                                have ever been written" does.
    `written_count` is that mark. Honest scope: an attacker who can mutate
    process memory can also reset the counter. Real tamper-*proofing* needs
    an off-box anchor (append to a WORM bucket, or publish the head hash to
    a separate system). This is tamper-*evidence*, and LIMITATIONS.md says
    so plainly.
  * **Redacted by construction.** Values pass through the same secret
    redaction used by logging before being recorded.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas.observability.logging import redact

GENESIS = "0" * 64


class AuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int = 0
    timestamp: str
    actor: str
    action: str
    resource: str = ""
    outcome: str = "success"
    detail: dict[str, Any] = {}
    prev_hash: str = GENESIS
    entry_hash: str = ""

    def compute_hash(self) -> str:
        payload = json.dumps(
            {
                "seq": self.seq,
                "timestamp": self.timestamp,
                "actor": self.actor,
                "action": self.action,
                "resource": self.resource,
                "outcome": self.outcome,
                "detail": self.detail,
                "prev_hash": self.prev_hash,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


_SENSITIVE_KEYS = {"token", "password", "passwd", "secret", "api_key", "apikey", "authorization"}


def _redact_tree(value: Any) -> Any:
    """Recursively redact: sensitive *keys* are replaced wholesale, and every
    string value is scanned for credential-shaped content."""
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if k.lower() in _SENSITIVE_KEYS else _redact_tree(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_tree(v) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value


class AuditLog:
    def __init__(self, sink: Path | None = None) -> None:
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()
        self._sink = sink
        # Monotonic high-water mark. Never decremented, so a shortened log
        # is detectable even when the surviving chain is internally valid.
        self._written_count = 0

    def record(
        self,
        *,
        actor: str,
        action: str,
        resource: str = "",
        outcome: str = "success",
        **detail: Any,
    ) -> AuditEntry:
        with self._lock:
            prev = self._entries[-1].entry_hash if self._entries else GENESIS
            # Redact per-value rather than by round-tripping the whole JSON
            # blob: string-level redaction can alter delimiters, and an audit
            # record that fails to parse is worse than one that leaks nothing.
            safe_detail = _redact_tree(detail)
            entry = AuditEntry(
                seq=len(self._entries),
                timestamp=datetime.now(timezone.utc).isoformat(),
                actor=actor,
                action=action,
                resource=resource,
                outcome=outcome,
                detail=safe_detail,
                prev_hash=prev,
            )
            entry = entry.model_copy(update={"entry_hash": entry.compute_hash()})
            self._entries.append(entry)
            self._written_count += 1
            if self._sink is not None:
                self._sink.parent.mkdir(parents=True, exist_ok=True)
                with self._sink.open("a") as f:
                    f.write(entry.model_dump_json() + "\n")
            return entry

    @property
    def written_count(self) -> int:
        """Entries ever written. Compare with `len()` to detect truncation."""
        return self._written_count

    def verify(self) -> bool:
        """Recompute the chain and check nothing was removed.

        False means an entry was altered, removed from the middle, or
        truncated from the end.
        """
        prev = GENESIS
        with self._lock:
            if len(self._entries) != self._written_count:
                return False  # entries were removed
            for index, entry in enumerate(self._entries):
                if entry.seq != index:
                    return False  # gap or truncation
                if entry.prev_hash != prev:
                    return False
                if entry.entry_hash != entry.compute_hash():
                    return False
                prev = entry.entry_hash
        return True

    def __iter__(self) -> Iterator[AuditEntry]:
        with self._lock:
            return iter(list(self._entries))

    def __len__(self) -> int:
        return len(self._entries)

    def tail(self, n: int = 20) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries[-n:])
