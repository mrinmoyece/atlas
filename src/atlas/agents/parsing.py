"""Turning model prose into validated `Finding` objects.

Structured output is where agent pipelines actually break in production.
The model is asked for JSON; it returns JSON wrapped in prose, or fenced in
markdown, or with a trailing comma, or with `severity: "Critical!"`. A
pipeline that assumes clean JSON works in the demo and pages you at 3am.

The policy here is *lenient extraction, strict validation*:

  * extract the JSON object from prose or code fences,
  * coerce known-sloppy values (case, synonyms, string line numbers),
  * **drop** anything that cannot be validated - never fabricate a default
    severity, because a silently-defaulted "medium" is a lie that reaches a
    decision-maker,
  * report what was dropped, so the failure is visible in metrics instead
    of quietly shrinking the report.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas.domain.types import Category, Confidence, Evidence, Finding, Severity

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

_SEVERITY_SYNONYMS = {
    "critical": Severity.CRITICAL,
    "crit": Severity.CRITICAL,
    "blocker": Severity.CRITICAL,
    "high": Severity.HIGH,
    "major": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "moderate": Severity.MEDIUM,
    "med": Severity.MEDIUM,
    "low": Severity.LOW,
    "minor": Severity.LOW,
    "info": Severity.INFO,
    "informational": Severity.INFO,
    "note": Severity.INFO,
}

_CONFIDENCE_SYNONYMS = {
    "high": Confidence.HIGH,
    "certain": Confidence.HIGH,
    "medium": Confidence.MEDIUM,
    "moderate": Confidence.MEDIUM,
    "low": Confidence.LOW,
    "possible": Confidence.LOW,
    "speculative": Confidence.LOW,
}


class ParseOutcome(BaseModel):
    """Result of parsing one specialist's final message."""

    model_config = ConfigDict(frozen=True)

    findings: tuple[Finding, ...] = ()
    summary: str = ""
    dropped: int = 0
    parse_error: str = ""

    @property
    def ok(self) -> bool:
        return not self.parse_error


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Find the first JSON object in text, tolerating fences and prose."""
    if not text:
        return None
    candidates: list[str] = []
    fenced = _FENCE.findall(text)
    candidates.extend(fenced)
    # Greedy brace scan as a fallback: find the outermost {...} span.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        candidate = candidate.strip()
        for attempt in (candidate, _strip_trailing_commas(candidate)):
            try:
                parsed = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _strip_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _coerce_severity(value: Any) -> Severity | None:
    if isinstance(value, Severity):
        return value
    key = str(value or "").strip().lower().rstrip("!.")
    return _SEVERITY_SYNONYMS.get(key)


def _coerce_confidence(value: Any) -> Confidence:
    key = str(value or "").strip().lower()
    return _CONFIDENCE_SYNONYMS.get(key, Confidence.MEDIUM)


def _coerce_line(value: Any) -> int | None:
    try:
        line = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return line if line >= 1 else None


def _finding_id(category: Category, severity: Any, title: str, path: str, line: int | None) -> str:
    """A content hash, not a random uuid.

    This module sits under a tool layer whose docstring promises "same repo,
    same arguments, same bytes out - without this the eval numbers mean
    nothing", and then stamped every finding with `uuid.uuid4()`. Two
    identical runs produced reports that differed byte-for-byte, which makes
    report diffing useless and any future content-addressed cache wrong.
    Scoring ignores the id, so this never showed up as a failure - it just
    quietly contradicted the guarantee next door.
    """
    seed = f"{category.value}|{getattr(severity, 'value', severity)}|{title}|{path}|{line}"
    return hashlib.sha256(seed.encode()).hexdigest()[:10]


def parse_specialist_output(
    text: str, category: Category, *, source_tool: str = ""
) -> ParseOutcome:
    """Parse a specialist's final message into validated findings."""
    payload = extract_json_object(text)
    if payload is None:
        # No JSON at all: keep the prose as a summary rather than losing the
        # work entirely, but report zero findings - we will not guess.
        return ParseOutcome(
            summary=text.strip()[:1000],
            parse_error="no JSON object found in final message",
        )

    summary = str(payload.get("summary", "")).strip()[:2000]
    raw_findings = payload.get("findings") or []
    if not isinstance(raw_findings, list):
        return ParseOutcome(summary=summary, parse_error="'findings' is not a list")

    findings: list[Finding] = []
    dropped = 0
    for item in raw_findings:
        if not isinstance(item, dict):
            dropped += 1
            continue
        severity = _coerce_severity(item.get("severity"))
        title = str(item.get("title", "")).strip()
        path = str(item.get("path", "")).strip()
        if not severity or not title or not path:
            # Missing severity, title or location -> unverifiable. Drop it.
            dropped += 1
            continue
        evidence = Evidence(
            path=path,
            line=_coerce_line(item.get("line")),
            excerpt=str(item.get("excerpt", ""))[:2000],
            source_tool=source_tool,
        )
        findings.append(
            Finding(
                finding_id=_finding_id(category, severity, title, path, evidence.line),
                category=category,
                severity=severity,
                confidence=_coerce_confidence(item.get("confidence")),
                title=title[:200],
                detail=str(item.get("detail", ""))[:4000],
                remediation=str(item.get("remediation", ""))[:2000],
                rule=str(item.get("rule", "")).strip().lower().replace(" ", "_"),
                evidence=(evidence,),
            )
        )

    return ParseOutcome(findings=tuple(findings), summary=summary, dropped=dropped)
