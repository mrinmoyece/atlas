"""LLM-as-judge, with the bias controls that make it worth anything.

Ground-truth scoring (scoring.py) answers "did it find the planted bugs".
It cannot answer "is this report well-written, correctly prioritised, and
free of hallucinated detail". That needs a judge - and a naive judge is
worse than none, because it produces a confident number that measures the
judge's biases rather than the agent's quality.

Controls implemented here:

  * **Groundedness without a model.** Every claimed file path is verified
    to exist in the repository. No judge is needed to catch a citation of a
    file that isn't there, and this catches the most damaging hallucination
    class deterministically and for free.
  * **Anchored criteria.** Each score is computed from a stated property
    (share of citations that resolve, share with line numbers, share of the
    report rated critical) rather than a free-form 1-10 impression.
  * **Position-bias control for pairwise comparison.** `pairwise_with_position_control`
    compares A-then-B and B-then-A and reports whether the winner survived
    the swap. A judge that changes its mind when you reorder the inputs is
    measuring position, not quality, so those pairs are marked
    untrustworthy rather than averaged in.

The default judge is deterministic and rule-based, so CI stays free and
reproducible. A model-backed judge implements the same `Judge` protocol;
the pairwise control exists because that is when position bias becomes
possible - it is a no-op for the deterministic judge by construction, and
`PairwiseResult.order_agreement` says so explicitly.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from atlas.domain.types import DueDiligenceReport, Finding


class JudgeScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    groundedness: float
    prioritisation: float
    actionability: float
    signal: float
    justification: str = ""

    @property
    def overall(self) -> float:
        return round(
            (self.groundedness + self.prioritisation + self.actionability + self.signal) / 4,
            4,
        )


class Judge(Protocol):
    name: str

    def score(self, report: DueDiligenceReport, repo_root: Path) -> JudgeScore: ...


class DeterministicJudge:
    """Rule-based judge: no model, no cost, no variance.

    Every criterion is computed from properties we can verify, so this is
    reproducible in CI. It is intentionally *not* a stand-in for human
    judgement of prose quality - that is what the optional LLM judge adds.
    """

    name = "deterministic"

    def score(self, report: DueDiligenceReport, repo_root: Path) -> JudgeScore:
        findings = report.findings
        if not findings:
            # An empty report on a clean repo is correct; on a dirty repo the
            # ground-truth scorer will punish it. The judge stays neutral.
            return JudgeScore(
                groundedness=5,
                prioritisation=3,
                actionability=3,
                signal=5,
                justification="no findings reported",
            )

        grounded = sum(1 for f in findings if f.is_grounded())
        real_paths = sum(1 for f in findings if _citations_exist(f, repo_root))
        with_line = sum(1 for f in findings if any(e.line for e in f.evidence))
        actionable = sum(1 for f in findings if len(f.remediation.strip()) > 20)
        critical_share = sum(1 for f in findings if f.severity.value == "critical") / len(findings)

        groundedness = 5 * (real_paths / len(findings)) * (0.7 + 0.3 * with_line / len(findings))
        # Everything-is-critical is the classic LLM failure; penalise it.
        prioritisation = (
            5.0 if critical_share <= 0.4 else max(1.0, 5.0 - 8 * (critical_share - 0.4))
        )
        actionability = 1 + 4 * (actionable / len(findings))
        signal = 1 + 4 * (grounded / len(findings))

        return JudgeScore(
            groundedness=round(groundedness, 3),
            prioritisation=round(prioritisation, 3),
            actionability=round(actionability, 3),
            signal=round(signal, 3),
            justification=(
                f"{real_paths}/{len(findings)} citations resolve to real files; "
                f"{with_line} have line numbers; critical share {critical_share:.0%}"
            ),
        )


class PairwiseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    winner: str
    order_agreement: bool
    position_bias_possible: bool
    detail: str = ""


class PairwiseComparator(Protocol):
    """A comparator sees BOTH reports at once, which is what makes position
    bias possible - and therefore what makes controlling for it meaningful."""

    name: str
    order_sensitive: bool

    def compare(
        self,
        first: tuple[str, DueDiligenceReport],
        second: tuple[str, DueDiligenceReport],
        repo_root: Path,
    ) -> str:
        """Return the name of the better report, given this presentation order."""
        ...


#: Returned instead of an arbitrary winner when two reports score equally.
TIE = "tie"
#: Scores are rounded to 4dp, so anything closer than this is equality.
TIE_EPSILON = 1e-6


class ScoreBasedComparator:
    """Wraps a scalar `Judge` into a comparator.

    `order_sensitive = False`: it scores each report independently, so the
    presentation order provably cannot affect the result. Stating that in
    the type is more honest than running a "control" that can only ever
    return agreement.
    """

    order_sensitive = False

    def __init__(self, judge: Judge) -> None:
        self._judge = judge
        self.name = f"scores({judge.name})"

    def compare(self, first, second, repo_root: Path) -> str:
        """Return the winner's name, or `TIE` when the scores are equal.

        The `a >= b` tie-break was positional, which quietly undid the very
        property this class advertises. Two identical reports produced
        `order A/B -> A; order B/A -> B`, so every tie came back
        `order_agreement=False` - "untrustworthy, exclude from aggregate
        claims" - from a comparator that declares itself provably
        order-insensitive. Ties are common: the deterministic judge is a
        four-criterion average over small integers, so equal scores are the
        normal case, not an edge case.
        """
        (a_name, a_report), (b_name, b_report) = first, second
        a = self._judge.score(a_report, repo_root).overall
        b = self._judge.score(b_report, repo_root).overall
        if abs(a - b) < TIE_EPSILON:
            return TIE
        return a_name if a > b else b_name


def pairwise_with_position_control(
    comparator: PairwiseComparator,
    left: tuple[str, DueDiligenceReport],
    right: tuple[str, DueDiligenceReport],
    repo_root: Path,
) -> PairwiseResult:
    """Compare A-then-B and B-then-A; report whether the winner survived.

    `order_agreement=False` means the judgement is untrustworthy for that
    pair and must be excluded from aggregate claims - the entire reason to
    run it twice. For an order-insensitive comparator the swap is a
    formality, and `position_bias_possible` records that so nobody mistakes
    guaranteed agreement for evidence of an unbiased judge.
    """
    forward = comparator.compare(left, right, repo_root)
    backward = comparator.compare(right, left, repo_root)
    # A tie in both directions IS agreement - the comparator said the same
    # thing twice. Treating it as disagreement was how a positional
    # tie-break leaked back out as a trust signal.
    return PairwiseResult(
        winner=forward,
        order_agreement=forward == backward,
        position_bias_possible=getattr(comparator, "order_sensitive", True),
        detail=f"order A/B -> {forward}; order B/A -> {backward}",
    )


def _citations_exist(finding: Finding, repo_root: Path) -> bool:
    """Deterministic hallucination check: does the cited file actually exist?"""
    for evidence in finding.evidence:
        candidate = (repo_root / evidence.path.removeprefix("./")).resolve()
        # Path ancestry, not string prefix - see RepoToolkit._resolve.
        try:
            inside = candidate.is_relative_to(repo_root.resolve())
        except (OSError, ValueError):  # pragma: no cover
            inside = False
        if inside and candidate.exists():
            return True
    return False


def hallucination_rate(reports: Sequence[DueDiligenceReport], repo_root: Path) -> float:
    """Share of findings citing files that do not exist. Reported alongside
    quality because a high-recall agent that invents paths is not good."""
    findings = [f for r in reports for f in r.findings]
    if not findings:
        return 0.0
    bad = sum(1 for f in findings if not _citations_exist(f, repo_root))
    return round(bad / len(findings), 4)
