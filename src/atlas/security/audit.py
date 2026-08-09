"""Audit log - who did what, when, and what it cost.

Distinct from application logging on purpose. Application logs are for
debugging and get sampled, rotated and dropped. An audit log answers
compliance questions ("who ran an analysis against that repository?") and
must be append-only, complete, and free of secrets.

Properties implemented:

  * **Bounded in-process tail**, with an optional durable JSONL sink. Entries
    are never mutated through this API; old in-memory entries are evicted only
    after their hash becomes the retained tail's verification anchor.
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
    `written_count` is that mark for the retained tail. Honest scope: an attacker who can mutate
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
import os
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
    def __init__(self, sink: Path | None = None, *, max_memory_entries: int = 10_000) -> None:
        if max_memory_entries < 1:
            raise ValueError("max_memory_entries must be positive")
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()
        self._sink = sink
        self._max_memory_entries = max_memory_entries
        self._anchor_hash = GENESIS
        self._anchor_seq = 0
        # Monotonic high-water mark. Never decremented, so a shortened log
        # is detectable even when the surviving chain is internally valid.
        self._written_count = 0
        if self._sink is not None:
            self._restore()

    def _restore(self) -> None:
        """Validate and restore the retained tail from an existing JSONL sink."""
        if self._sink is None or not self._sink.exists():
            return
        previous = GENESIS
        expected_seq = 0
        with self._sink.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    entry = AuditEntry.model_validate_json(line)
                except Exception as exc:
                    raise ValueError(f"invalid audit entry at {self._sink}:{line_number}") from exc
                if (
                    entry.seq != expected_seq
                    or entry.prev_hash != previous
                    or entry.entry_hash != entry.compute_hash()
                ):
                    raise ValueError(f"invalid audit chain at {self._sink}:{line_number}")
                previous = entry.entry_hash
                expected_seq += 1
                self._entries.append(entry)
                if len(self._entries) > self._max_memory_entries:
                    evicted = self._entries.pop(0)
                    self._anchor_hash = evicted.entry_hash
                    self._anchor_seq = evicted.seq + 1
        self._written_count = expected_seq

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
            prev = self._entries[-1].entry_hash if self._entries else self._anchor_hash
            # Redact per-value rather than by round-tripping the whole JSON
            # blob: string-level redaction can alter delimiters, and an audit
            # record that fails to parse is worse than one that leaks nothing.
            safe_detail = _redact_tree(detail)
            entry = AuditEntry(
                seq=self._written_count,
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
            if len(self._entries) > self._max_memory_entries:
                evicted = self._entries.pop(0)
                self._anchor_hash = evicted.entry_hash
                self._anchor_seq = evicted.seq + 1
            if self._sink is not None:
                self._sink.parent.mkdir(parents=True, exist_ok=True)
                with self._sink.open("a", encoding="utf-8") as f:
                    f.write(entry.model_dump_json() + "\n")
                    f.flush()
                    os.fsync(f.fileno())
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
        prev = self._anchor_hash
        with self._lock:
            expected_length = min(self._written_count, self._max_memory_entries)
            expected_start = self._written_count - expected_length
            if len(self._entries) != expected_length or self._anchor_seq != expected_start:
                return False  # entries were removed
            for offset, entry in enumerate(self._entries):
                if entry.seq != expected_start + offset:
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
