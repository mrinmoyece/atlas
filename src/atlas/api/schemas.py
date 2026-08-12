"""API DTOs.

Kept separate from domain types so the wire contract can stay stable while
the domain evolves, and so internal fields (raw evidence excerpts, agent
metadata) are not leaked by accident. Input validation is strict: an agent
platform's request body is a place where a caller can otherwise smuggle
oversized or malformed values into a prompt.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from atlas.domain.types import Finding

VALID_CATEGORIES = {"security", "architecture", "dependency", "delivery"}
VALID_PATTERNS = {"react", "plan_execute", "reflexion", "rewoo"}


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # reject unknown fields outright

    repo: str = Field(min_length=1, max_length=128)
    # None means "let the graph choose": procedural memory first, then the
    # graph's deterministic default. An explicit value always wins.
    pattern: str | None = None
    categories: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("repo")
    @classmethod
    def _safe_repo(cls, v: str) -> str:
        # Defence in depth: reject traversal-shaped names before they reach
        # path resolution.
        if "/" in v or "\\" in v or ".." in v or v.startswith("."):
            raise ValueError("repo must be a plain directory name")
        if not all(c.isalnum() or c in "-_." for c in v):
            raise ValueError("repo contains unsupported characters")
        return v

    @field_validator("pattern")
    @classmethod
    def _known_pattern(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_PATTERNS:
            raise ValueError(f"pattern must be one of {sorted(VALID_PATTERNS)}")
        return v

    @field_validator("categories")
    @classmethod
    def _known_categories(cls, v: list[str]) -> list[str]:
        unknown = set(v) - VALID_CATEGORIES
        if unknown:
            raise ValueError(f"unknown categories: {sorted(unknown)}")
        # Deduplicate while preserving order so duplicate inputs do not
        # cause a graph-construction 500 (LangGraph rejects duplicate node names).
        seen: set[str] = set()
        deduped: list[str] = []
        for cat in v:
            if cat not in seen:
                seen.add(cat)
                deduped.append(cat)
        return deduped


class EvidenceView(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    line: int | None = None
    excerpt: str = ""


class FindingView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    category: str
    severity: str
    confidence: str
    title: str
    detail: str
    remediation: str
    rule: str
    evidence: list[EvidenceView]

    @classmethod
    def from_finding(cls, finding: Finding) -> FindingView:
        return cls(
            id=finding.finding_id,
            category=finding.category.value,
            severity=finding.severity.value,
            confidence=finding.confidence.value,
            title=finding.title,
            detail=finding.detail,
            remediation=finding.remediation,
            rule=finding.rule,
            evidence=[
                EvidenceView(path=e.path, line=e.line, excerpt=e.excerpt[:400])
                for e in finding.evidence
            ],
        )


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str
    verdict: str
    findings: list[FindingView]
    summaries: dict[str, str]
    counts: dict[str, int]
    tokens_used: int
    cost_usd: float
    duration_ms: int
    errors: dict[str, str] = Field(default_factory=dict)
    request_id: str = ""


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    version: str
    auth_configured: bool


class ErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    error: str
    request_id: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
