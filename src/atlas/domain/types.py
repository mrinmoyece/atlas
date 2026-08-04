"""Domain types.

Design rule: **every Finding must carry Evidence**. An agent that says
"this codebase has SQL injection" without a file and line is unfalsifiable,
and unfalsifiable output cannot be evaluated. Requiring evidence at the type
level is what makes the golden-dataset scoring in `evals/` possible at all -
we can only compute precision and recall because every claim points at a
specific location we can check against planted ground truth.

These types are also deliberately independent of LangGraph and of any model
vendor: the orchestration layer can be swapped without touching them.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Severity(str, Enum):
    """Business impact if the finding is real. Ordered for sorting."""

    CRITICAL = "critical"  # exploitable now / blocks the deal
    HIGH = "high"  # must fix before production
    MEDIUM = "medium"  # should fix this quarter
    LOW = "low"  # cleanup
    INFO = "info"  # context, not a defect

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}


class Confidence(str, Enum):
    """How sure the agent is. Kept separate from severity on purpose:
    "probably a critical issue" and "definitely a minor issue" are very
    different triage decisions, and collapsing them loses information."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Category(str, Enum):
    """Which specialist owns this dimension. One specialist per category
    keeps subagent contexts small and their prompts narrow."""

    SECURITY = "security"
    ARCHITECTURE = "architecture"
    DEPENDENCY = "dependency"
    DELIVERY = "delivery"


class Evidence(BaseModel):
    """A pointer to the thing that justifies a finding. Immutable."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(description="Repo-relative file path")
    line: int | None = Field(default=None, ge=1)
    excerpt: str = Field(default="", max_length=2000)
    source_tool: str = Field(default="", description="Tool that produced this evidence")

    def locator(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path


class Finding(BaseModel):
    """One defect or risk. The atomic unit of a due-diligence report."""

    model_config = ConfigDict(frozen=True)

    finding_id: str
    category: Category
    severity: Severity
    confidence: Confidence = Confidence.MEDIUM
    title: str = Field(max_length=200)
    detail: str = ""
    evidence: tuple[Evidence, ...] = ()
    remediation: str = ""
    # Free-form tag used by eval scoring to match against ground truth
    # (e.g. "sql_injection", "hardcoded_secret"). Kept as a plain string so
    # new issue classes don't require a code change to be scoreable.
    rule: str = ""

    def is_grounded(self) -> bool:
        """A finding with no evidence cannot be verified, so it cannot be
        scored - and should not be shown to a human as fact."""
        return bool(self.evidence)


class SpecialistResult(BaseModel):
    """What one specialist agent returns to the supervisor.

    Note what is NOT here: the specialist's full transcript. The supervisor
    receives findings and a short summary only. That asymmetry is the whole
    point of context isolation - see docs/adr/0003-context-isolation.md.
    """

    model_config = ConfigDict(frozen=True)

    category: Category
    findings: tuple[Finding, ...] = ()
    summary: str = ""
    tokens_used: int = 0
    cost_usd: float = 0.0
    model_calls: int = 0
    steps: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class DueDiligenceReport(BaseModel):
    """The merged deliverable."""

    model_config = ConfigDict(frozen=True)

    repo: str
    findings: tuple[Finding, ...] = ()
    summaries: dict[str, str] = Field(default_factory=dict)
    verdict: str = ""
    tokens_used: int = 0
    cost_usd: float = 0.0
    model_calls: int = 0
    duration_ms: int = 0
    errors: dict[str, str] = Field(default_factory=dict)

    def by_severity(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (-f.severity.rank, f.category.value))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.severity.value] = out.get(f.severity.value, 0) + 1
        return out

    def blocking(self) -> list[Finding]:
        """Findings that would block an acquisition or adoption decision."""
        return [f for f in self.findings if f.severity in (Severity.CRITICAL, Severity.HIGH)]
